"""HTTP and WebSocket server helpers for streaming transport."""
from __future__ import annotations

import asyncio
import inspect
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

import websockets


class StaticContentHandler(SimpleHTTPRequestHandler):
    """Serve files from the repository's static directory."""

    config_provider: Optional[Callable[[], dict]] = None

    def __init__(self, *args, directory: Optional[str] = None, **kwargs):
        static_dir = directory or str(Path(__file__).with_name("static"))
        super().__init__(*args, directory=static_dir, **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/__convergence_config":
            payload: dict = {}
            if callable(self.config_provider):
                try:
                    candidate = self.config_provider()
                    if isinstance(candidate, dict):
                        payload = candidate
                except Exception:
                    payload = {}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class StaticHttpServer:
    """Wrapper around :class:`ThreadingHTTPServer` running in a background thread."""

    def __init__(
        self,
        host: str,
        port: int,
        config_provider: Optional[Callable[[], dict]] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._config_provider = config_provider
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._httpd is not None:
            return
        ThreadingHTTPServer.allow_reuse_address = True
        provider = self._config_provider

        class _Handler(StaticContentHandler):
            config_provider = provider

        self._httpd = ThreadingHTTPServer((self._host, self._port), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._httpd:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        finally:
            self._httpd = None
            if self._thread:
                try:
                    self._thread.join(timeout=2.0)
                finally:
                    self._thread = None


class WebSocketTransport:
    """Thread-safe helper to schedule sends on the active WebSocket connection."""

    def __init__(self) -> None:
        self._conn = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

    def attach(self, conn, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._conn = conn
            self._loop = loop

    def detach(self, conn) -> None:
        with self._lock:
            if self._conn is conn:
                self._conn = None
                self._loop = None

    def has_connection(self) -> bool:
        with self._lock:
            conn = self._conn
            return bool(conn) and not getattr(conn, "closed", False)

    def _schedule(self, payload) -> bool:
        with self._lock:
            conn = self._conn
            loop = self._loop
        if not conn or not loop or getattr(conn, "closed", False):
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(conn.send(payload), loop)
        except Exception as exc:
            print(f"Server: Failed to submit WebSocket send: {exc}")
            return False

        def _done(task):
            try:
                task.result(timeout=0)
            except Exception as err:
                print(f"Server: WebSocket send future error: {err}")

        try:
            fut.add_done_callback(_done)
        except Exception:
            pass
        return True

    def send_bytes(self, data: bytes) -> bool:
        return self._schedule(data)

    def send_json(self, payload: dict) -> bool:
        try:
            message = json.dumps(payload)
        except Exception as exc:
            print(f"Server: Failed to encode JSON payload {payload!r}: {exc}")
            return False
        return self._schedule(message)


class WebSocketServer:
    """Run a WebSocket server and expose its active connection via a transport."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self.transport = WebSocketTransport()

    def start(self, handler: Callable[[object], Awaitable[None]]) -> None:
        if self._thread is not None:
            return

        def _runner():
            try:
                loop = asyncio.new_event_loop()
                self._loop = loop
                asyncio.set_event_loop(loop)

                handler_signature = inspect.signature(handler)
                parameters = list(handler_signature.parameters.values())
                supports_path = any(
                    param.kind in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    )
                    and index >= 1
                    for index, param in enumerate(parameters)
                ) or any(
                    param.kind in (
                        inspect.Parameter.VAR_POSITIONAL,
                        inspect.Parameter.VAR_KEYWORD,
                    )
                    for param in parameters
                )

                async def _connection_handler(websocket):
                    path = getattr(websocket, "path", None)
                    if not path:
                        request = getattr(websocket, "request", None)
                        path = getattr(request, "path", None) if request is not None else None
                    if not path:
                        path = getattr(websocket, "request_uri", None)
                    if not path:
                        path = "/"
                    self.transport.attach(websocket, asyncio.get_running_loop())
                    try:
                        if supports_path:
                            await handler(websocket, path)
                        else:
                            await handler(websocket)
                    finally:
                        self.transport.detach(websocket)

                async def _start_server():
                    return await websockets.serve(
                        _connection_handler, self._host, self._port
                    )

                self._server = loop.run_until_complete(_start_server())
                print(f"Server: WebSocket server started on port {self._port}")
                loop.run_forever()
            except Exception as exc:
                print(f"Server: WebSocket server error: {exc}")
                import traceback
                traceback.print_exc()
            finally:
                server = self._server
                loop = self._loop
                if server is not None:
                    try:
                        server.close()
                        if loop and not loop.is_closed():
                            loop.run_until_complete(server.wait_closed())
                    except Exception:
                        pass
                    self._server = None
                if loop and not loop.is_closed():
                    try:
                        loop.close()
                    except Exception:
                        pass
                self._loop = None

        self._thread = threading.Thread(target=_runner, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        loop = self._loop
        server = self._server
        if not loop:
            return

        def _stop():
            if server:
                try:
                    server.close()
                except Exception:
                    pass
            loop.stop()

        try:
            loop.call_soon_threadsafe(_stop)
        except Exception:
            pass

        if self._thread:
            try:
                self._thread.join(timeout=2.0)
            finally:
                self._thread = None
        self._server = None
        self._loop = None
