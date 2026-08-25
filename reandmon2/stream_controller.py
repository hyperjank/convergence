#!/usr/bin/env python3
import time
import gi
import secrets

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GstWebRTC, GstSdp, GLib
import threading
import subprocess
import signal
import json
import asyncio
import socket
import os
from pathlib import Path
from shutil import which
from urllib.parse import parse_qs, urlencode, urlsplit

try:
    from .config import (
        WS_PORT,
        HTTP_PORT,
        HOST,
        ENCODER_PIPELINES,
        HOST_IP,
    )
    from .portal_session import PortalSession
    from .telemetry import sanitize_receiver_telemetry
    from .ws_server import StaticHttpServer, WebSocketServer
except ImportError:
    from config import (
        WS_PORT,
        HTTP_PORT,
        HOST,
        ENCODER_PIPELINES,
        HOST_IP,
    )
    from portal_session import PortalSession
    from telemetry import sanitize_receiver_telemetry
    from ws_server import StaticHttpServer, WebSocketServer


def _adb_cmd(serial: str | None, args: list[str], timeout: int = 10) -> bool:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def try_adb_launch(
    url: str,
    *,
    serial: str | None = None,
) -> bool:
    """
    Attempt to launch the client browser via adb (no reverse).
    Return True on success; False otherwise.
    """
    from shutil import which

    if which("adb") is None:
        print("Server: adb not found")
        return False

    try:
        print("Server: Launching client via adb...")
        time.sleep(1)

        # Chrome primary
        if _adb_cmd(
            serial,
            [
                "shell",
                "am",
                "start",
                "-n",
                "com.android.chrome/com.google.android.apps.chrome.Main",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                url,
                "--ez",
                "fullscreen",
                "true",
                "--activity-clear-top",
                "--es",
                "com.android.chrome.extra.CREATE_NEW_TAB",
                "false",
            ],
        ):
            print("Server: Opened stream in Chrome.")
            return True

        # Generic VIEW fallback
        if _adb_cmd(
            serial,
            [
                "shell",
                "am",
                "start",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                url,
                "--ez",
                "fullscreen",
                "true",
            ],
        ):
            print("Server: Opened stream with generic VIEW intent.")
            return True

        print("Server: Failed to launch client browser/app via adb.")
        return False
    except Exception as e:
        print("Server: adb error", e)
        return False


class StreamController:
    """
    Manages:
      - PipeWire portal session (screen capture)
      - GStreamer WebRTC pipeline
      - HTTP server (static files)
      - WebSocket signaling server
      - GLib main loop and cleanup
    """

    def __init__(
        self,
        adb_serial: str | None = None,
        auto_launch_client: bool = True,
        stream_profile: str = "realtime",
        stream_format: str = "1080p30",
        realtime_queue_depth: int = 4,
        custom_profile: dict | None = None,
        telemetry_overlay: bool = False,
        receiver_jitter_target_ms: int | None = None,
        encoder_fallback_callback=None,
    ):
        Gst.init(None)

        self.portal = PortalSession()
        self.http_server: StaticHttpServer | None = None
        self.ws_server: WebSocketServer | None = None

        self.pw_id = None
        self.screen_width = None
        self.screen_height = None

        self.pipeline: Gst.Pipeline | None = None
        self.webrtc = None

        self.loop = None
        self.glib_thread = None
        self._glib_ready = threading.Event()
        self._glib_thread_id: int | None = None

        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_conn = None
        self._ws_lock = threading.Lock()
        self._ws_generation = 0
        self._restart_scheduled = False
        self._last_restart_time = 0.0
        self._telemetry_lock = threading.Lock()
        self._latest_receiver_telemetry: dict | None = None
        self._telemetry_log_path: Path | None = None
        self._last_telemetry_console_log = 0.0

        self.encoder_override = None
        self.encoder_name, self.encoder_pipeline = self._select_encoder()
        print(f"Server: Selected encoder: {self.encoder_name}")

        self._ice_out_count = 0
        self._ice_in_count = 0
        self._rtp_packet_count = 0
        self._rtp_byte_count = 0
        self._rtp_window_started = time.monotonic()
        self._pipeline_generation = 0
        self._pipeline_rtp_packets_since_start = 0
        self._last_pipeline_rtp_time: float | None = None
        self._payload_pt = 96
        self._ice_handler_id = None
        self._ice_handler_cb = None
        self._pad_added_id = None
        self._ice_handler_target = None
        self.adb_serial = adb_serial
        self.auto_launch_client = auto_launch_client
        self.auth_token: str | None = None
        self.stream_profile = (
            stream_profile if stream_profile in ("realtime", "quality", "delay", "custom") else "realtime"
        )
        self.stream_format = (
            stream_format if stream_format in ("1080p30", "720p30", "720p60") else "1080p30"
        )
        try:
            queue_depth = int(realtime_queue_depth)
        except (TypeError, ValueError):
            queue_depth = 4
        self.realtime_queue_depth = queue_depth if queue_depth in (1, 2, 4) else 4
        self.custom_profile = custom_profile or {}
        self.telemetry_overlay = bool(telemetry_overlay)
        try:
            jitter_target = (
                int(receiver_jitter_target_ms)
                if receiver_jitter_target_ms is not None
                else None
            )
        except (TypeError, ValueError):
            jitter_target = None
        self.receiver_jitter_target_ms = (
            jitter_target if jitter_target is not None and 0 <= jitter_target <= 4000 else None
        )
        self.encoder_fallback_callback = encoder_fallback_callback

    @staticmethod
    def _sanitize_receiver_telemetry(message: dict) -> dict | None:
        return sanitize_receiver_telemetry(message)

    def _handle_receiver_telemetry(self, message: dict) -> None:
        sample = self._sanitize_receiver_telemetry(message)
        if sample is None:
            print("Server: Ignored invalid receiver telemetry")
            return
        sample["receivedAt"] = time.time()
        sample["profile"] = self.stream_profile
        sample["encoder"] = self.encoder_name
        with self._telemetry_lock:
            self._latest_receiver_telemetry = dict(sample)
            log_path = self._telemetry_log_path

        if log_path is not None:
            try:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(sample, sort_keys=True) + "\n")
            except Exception as exc:
                print(f"Server: Failed to persist receiver telemetry: {exc}")

        now = time.monotonic()
        if now - self._last_telemetry_console_log >= 10.0:
            self._last_telemetry_console_log = now
            print(
                "Server: Receiver telemetry "
                f"jb={sample.get('jitterBufferMs')}ms "
                f"fps={sample.get('decodeFps')}/{sample.get('renderFps')} "
                f"drop=+{sample.get('framesDroppedDelta')} "
                f"loss={sample.get('packetLossPercent')}% "
                f"rtt={sample.get('rttMs')}ms"
            )

    def latest_receiver_telemetry(self, max_age_seconds: float = 10.0) -> dict | None:
        with self._telemetry_lock:
            sample = (
                dict(self._latest_receiver_telemetry)
                if self._latest_receiver_telemetry is not None
                else None
            )
        if sample is None:
            return None
        received_at = sample.get("receivedAt")
        if not isinstance(received_at, (int, float)) or time.time() - received_at > max_age_seconds:
            return None
        return sample

    def telemetry_log_path(self) -> str | None:
        with self._telemetry_lock:
            path = self._telemetry_log_path
        return str(path) if path is not None else None

    def _start_telemetry_log(self) -> None:
        state_dir = Path.home() / ".local" / "state" / "convergence" / "telemetry"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = state_dir / f"receiver-{stamp}-{self.stream_profile}.jsonl"
            session = {
                "type": "session",
                "startedAt": time.time(),
                "profile": self.stream_profile,
                "encoder": self.encoder_name,
                "streamFormat": self.stream_format,
                "realtimeQueueDepth": self.realtime_queue_depth,
                "receiverJitterTargetMs": self.receiver_jitter_target_ms,
            }
            path.write_text(json.dumps(session, sort_keys=True) + "\n", encoding="utf-8")
        except Exception as exc:
            print(f"Server: Failed to create telemetry log: {exc}")
            path = None
        with self._telemetry_lock:
            self._telemetry_log_path = path
            self._latest_receiver_telemetry = None

    def get_available_encoders(self) -> list[str]:
        available = []
        for name, _ in ENCODER_PIPELINES:
            if Gst.ElementFactory.find(name) is not None:
                available.append(name)
        return available

    def set_encoder(self, name: str | None) -> bool:
        if name == "auto" or not name:
            self.encoder_override = None
        else:
            available = self.get_available_encoders()
            if name not in available:
                print(f"Server: Requested encoder '{name}' is not available")
                return False
            self.encoder_override = name

        self.encoder_name, self.encoder_pipeline = self._select_encoder()
        print(f"Server: Selected encoder: {self.encoder_name}")
        self._schedule_restart("encoder change")
        return True

    def _select_encoder(self) -> tuple[str, str]:
        if self.encoder_override:
            for name, pipeline in ENCODER_PIPELINES:
                if name == self.encoder_override:
                    return name, pipeline
        for name, pipeline in ENCODER_PIPELINES:
            if Gst.ElementFactory.find(name) is not None:
                return name, pipeline
        return ENCODER_PIPELINES[-1]

    def _encoder_element(self, name: str) -> str:
        bitrate_multiplier = self._profile_bitrate_multiplier()
        if self.stream_format == "720p30":
            bitrate_multiplier *= 0.65
        elif self.stream_format == "720p60":
            bitrate_multiplier *= 0.9
        keyint = self._profile_keyint()
        if self.stream_format == "720p60":
            keyint *= 2

        if name == "vah264enc":
            bitrate = int(8000 * bitrate_multiplier)
            return (
                "vah264enc target-usage=7 rate-control=cbr cabac=false "
                f"bitrate={bitrate} key-int-max={keyint}"
            )
        if name == "vah264lpenc":
            return (
                "vah264lpenc target-usage=7 rate-control=cqp cabac=false "
                f"qpi=24 qpp=26 key-int-max={keyint}"
            )
        if name == "vaapih264enc":
            bitrate = int(8000 * bitrate_multiplier)
            return f"vaapih264enc rate-control=cbr bitrate={bitrate} keyframe-period={keyint}"
        if name == "nvh264enc":
            bitrate = int(20000 * bitrate_multiplier)
            zerolatency = "false" if self.stream_profile == "quality" else "true"
            return f"nvh264enc bitrate={bitrate} iframeinterval={keyint} zerolatency={zerolatency}"
        if name == "x264enc":
            if self.stream_profile == "realtime":
                preset = "ultrafast"
                tune = "zerolatency"
                bframes = 0
                bitrate = int(5000 * bitrate_multiplier)
                lookahead = 0
            elif self.stream_profile == "delay":
                preset = "superfast"
                tune = "zerolatency"
                bframes = 0
                bitrate = int(9000 * bitrate_multiplier)
                lookahead = 0
            else:
                # Keep quality mode smooth enough for live playback while improving compression.
                preset = "veryfast"
                tune = ""
                bframes = 2
                bitrate = int(12000 * bitrate_multiplier)
                lookahead = 20
            tune_part = f"tune={tune} " if tune else ""
            return (
                f"x264enc {tune_part}speed-preset={preset} bitrate={bitrate} "
                f"key-int-max={keyint} bframes={bframes} cabac=false "
                f"rc-lookahead={lookahead} byte-stream=true"
            )
        return f"{name}"

    def start_screen_capture(self):
        self.pw_id, self.screen_width, self.screen_height = self.portal.start()
        print("Server: PipeWire node id", self.pw_id)
        print(f"Server: Screen resolution: {self.screen_width}x{self.screen_height}")

    def stop_screen_capture(self):
        if self.loop and self._glib_ready.is_set():
            try:
                self._call_on_glib_sync(self._stop_pipeline, timeout=2.0)
            except Exception as exc:
                print(f"Server: Failed to stop pipeline on GLib loop: {exc}")
                self._stop_pipeline()
        else:
            self._stop_pipeline()
        self.portal.stop()
        self.pw_id = None
        self.screen_width = None
        self.screen_height = None

    def _build_pipeline(self) -> tuple[Gst.Pipeline, object]:
        if not self.pw_id:
            raise RuntimeError("PipeWire node id (pw_id) is not set - did start_screen_capture() succeed?")

        if self.stream_profile in ("quality", "delay", "custom"):
            qns = self._profile_queue_time_ms() * 1_000_000
            q1 = f"queue max-size-buffers=0 max-size-bytes=0 max-size-time={qns} ! "
            q2 = f"queue max-size-buffers=0 max-size-bytes=0 max-size-time={qns} ! "
        else:
            depth = self.realtime_queue_depth
            q1 = f"queue max-size-buffers={depth} max-size-bytes=0 max-size-time=0 leaky=downstream ! "
            q2 = f"queue max-size-buffers={depth} max-size-bytes=0 max-size-time=0 leaky=downstream ! "

        payload_pt = int(self._payload_pt) if self._payload_pt else 96
        stream_width, stream_height, stream_fps = self._stream_video_parameters()
        scale = ""
        if self.screen_width != stream_width or self.screen_height != stream_height:
            scale = "videoscale ! "
        encoder = self._encoder_element(self.encoder_name)
        h264_caps = "video/x-h264,stream-format=byte-stream,alignment=au ! "
        if self.encoder_name == "x264enc":
            h264_caps = "video/x-h264,profile=baseline,stream-format=byte-stream,alignment=au ! "
        elif self.encoder_name in ("vah264enc", "vah264lpenc"):
            h264_caps = (
                "video/x-h264,profile=constrained-baseline,"
                "stream-format=byte-stream,alignment=au ! "
            )
        payload = (
            f"{encoder} ! "
            "h264parse config-interval=1 ! "
            f"{h264_caps}"
            f"rtph264pay name=payloader config-interval=1 pt={payload_pt} ! "
            f"application/x-rtp,media=video,encoding-name=H264,payload={payload_pt},clock-rate=90000 ! "
        )
        desc = (
            "webrtcbin name=webrtc bundle-policy=max-bundle "
            f"pipewiresrc path={self.pw_id} do-timestamp=true min-buffers=8 max-buffers=64 ! "
            f"{q1}"
            "videoconvert ! "
            f"{scale}"
            f"videorate drop-only=true max-rate={stream_fps} ! "
            f"video/x-raw,format=NV12,width={stream_width},height={stream_height},"
            f"framerate={stream_fps}/1 ! "
            f"{q2}"
            f"{payload}"
            "webrtc."
        )
        print("Server: WebRTC pipeline:", desc)
        pipeline = Gst.parse_launch(desc)
        webrtc = pipeline.get_by_name("webrtc")
        if not webrtc:
            raise RuntimeError("Failed to get webrtcbin from pipeline")
        payloader = pipeline.get_by_name("payloader")
        if payloader is not None:
            src_pad = payloader.get_static_pad("src")
            if src_pad is not None:
                src_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_rtp_buffer)
        try:
            pad = webrtc.get_request_pad("sink_%u")
            if pad:
                self._set_sendonly_on_pad(pad)
        except Exception as exc:
            print(f"Server: Failed to request webrtc sink pad: {exc}")
        self._attach_bus_watch(pipeline)
        return pipeline, webrtc

    def _on_rtp_buffer(self, _pad, info):
        buffer = info.get_buffer()
        if buffer is not None:
            self._pipeline_rtp_packets_since_start += 1
            self._last_pipeline_rtp_time = time.monotonic()
            self._rtp_packet_count += 1
            self._rtp_byte_count += buffer.get_size()
        now = time.monotonic()
        elapsed = now - self._rtp_window_started
        if elapsed >= 5.0:
            mbps = (self._rtp_byte_count * 8) / elapsed / 1_000_000
            print(
                f"Server: RTP outbound {self._rtp_packet_count} packets, "
                f"{mbps:.2f} Mbit/s over {elapsed:.1f}s"
            )
            self._rtp_packet_count = 0
            self._rtp_byte_count = 0
            self._rtp_window_started = now
        return Gst.PadProbeReturn.OK

    def _start_pipeline(self) -> None:
        if self.pipeline:
            return
        self._stop_pipeline()
        pipeline, webrtc = self._build_pipeline()
        self.pipeline = pipeline
        self.webrtc = webrtc
        try:
            latency = self._profile_webrtc_latency_ms()
            self.webrtc.set_property("latency", latency)
            self.webrtc.connect("notify::ice-connection-state", self._log_webrtc_state)
            self.webrtc.connect("notify::connection-state", self._log_webrtc_state)
            self.webrtc.connect("notify::ice-gathering-state", self._log_webrtc_state)
            self._pad_added_id = self.webrtc.connect("pad-added", self._on_webrtc_pad_added)
        except Exception as exc:
            print(f"Server: Failed to configure webrtcbin: {exc}")
        self._pipeline_generation += 1
        generation = self._pipeline_generation
        self._pipeline_rtp_packets_since_start = 0
        self._last_pipeline_rtp_time = None
        pipeline_started_at = time.monotonic()
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to set pipeline to PLAYING")

        def _check_encoder_output():
            if generation != self._pipeline_generation or self.pipeline is not pipeline:
                return False
            if self.encoder_name == "x264enc":
                return False
            now = time.monotonic()
            if now - pipeline_started_at < 8.0:
                return True
            rtp_age = (
                now - self._last_pipeline_rtp_time
                if self._last_pipeline_rtp_time is not None
                else None
            )
            if self._pipeline_rtp_packets_since_start >= 30 and rtp_age is not None and rtp_age < 4.0:
                return True
            failed_encoder = self.encoder_name
            self.encoder_override = "x264enc"
            self.encoder_name, self.encoder_pipeline = self._select_encoder()
            print(
                f"Server: Only {self._pipeline_rtp_packets_since_start} RTP packets from "
                f"{failed_encoder}; last packet age={rtp_age}; "
                "falling back to x264enc"
            )
            if self.encoder_fallback_callback is not None:
                try:
                    self.encoder_fallback_callback(failed_encoder, "x264enc")
                except Exception as exc:
                    print(f"Server: Failed to persist encoder fallback: {exc}")
            self._schedule_restart(f"{failed_encoder} produced no RTP")
            return False

        GLib.timeout_add(2000, _check_encoder_output)

    def _stop_pipeline(self) -> None:
        if self.pipeline:
            try:
                self.pipeline.set_state(Gst.State.NULL)
            finally:
                self._pipeline_generation += 1
                self.pipeline = None
                self.webrtc = None
                self._ice_handler_id = None
                self._ice_handler_target = None
                self._pad_added_id = None

    def _attach_bus_watch(self, pipeline: Gst.Pipeline) -> None:
        bus = pipeline.get_bus()
        if bus is None:
            return
        try:
            bus.add_signal_watch()
        except Exception as exc:
            print(f"Server: bus.add_signal_watch() failed: {exc}")
            return

        def _bus_message(_bus, message):
            try:
                msg_type = message.type
                if msg_type == Gst.MessageType.ERROR:
                    err, dbg = message.parse_error()
                    print(f"Server: GStreamer ERROR: {err}: {dbg}")
                    self._schedule_restart("pipeline error")
                elif msg_type == Gst.MessageType.WARNING:
                    warn, dbg = message.parse_warning()
                    print(f"Server: GStreamer WARNING: {warn}: {dbg}")
            except Exception as exc:
                print(f"Server: exception in bus message handler: {exc}")

        bus.connect("message", _bus_message)

    def _log_webrtc_state(self, element, _param) -> None:
        try:
            ice_state = self.webrtc.get_property("ice-connection-state")
            gather_state = self.webrtc.get_property("ice-gathering-state")
            conn_state = self.webrtc.get_property("connection-state")
            ice_map = {
                0: "new",
                1: "checking",
                2: "connected",
                3: "completed",
                4: "failed",
                5: "disconnected",
                6: "closed",
            }
            gather_map = {0: "new", 1: "gathering", 2: "complete"}
            conn_map = {
                0: "new",
                1: "connecting",
                2: "connected",
                3: "disconnected",
                4: "failed",
                5: "closed",
            }
            ice_name = ice_map.get(int(ice_state), str(ice_state))
            gather_name = gather_map.get(int(gather_state), str(gather_state))
            conn_name = conn_map.get(int(conn_state), str(conn_state))
            print(f"Server: WebRTC state ice={ice_name} gather={gather_name} conn={conn_name}")
            if ice_state == GstWebRTC.WebRTCICEConnectionState.FAILED:
                self._schedule_restart("ice failed")
            if conn_state == GstWebRTC.WebRTCPeerConnectionState.FAILED:
                self._schedule_restart("connection failed")
        except Exception:
            pass

    def _force_sendonly(self) -> None:
        if not self.webrtc:
            return
        try:
            for pad in self.webrtc.pads:
                self._set_sendonly_on_pad(pad)
        except Exception as exc:
            print(f"Server: Failed to set sendonly: {exc}")

    def _set_sendonly_on_pad(self, pad) -> None:
        try:
            name = pad.get_name()
            if not name.startswith("sink_"):
                return
            trans = pad.get_property("transceiver")
            if not trans:
                return
            trans.set_property(
                "direction",
                GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY,
            )
        except Exception as exc:
            print(f"Server: Failed to set sendonly on pad: {exc}")

    def _on_webrtc_pad_added(self, _element, pad) -> None:
        self._set_sendonly_on_pad(pad)

    def _schedule_restart(self, reason: str) -> None:
        now = time.time()
        if self._restart_scheduled or (now - self._last_restart_time) < 2:
            return
        self._restart_scheduled = True

        def _restart():
            self._restart_scheduled = False
            self._last_restart_time = time.time()
            try:
                print(f"Server: Restarting pipeline ({reason})")
                self._stop_pipeline()
                if self._ws_conn and self._ws_loop:
                    asyncio.run_coroutine_threadsafe(
                        self._ws_conn.send(json.dumps({"type": "restart"})),
                        self._ws_loop,
                    )
            except Exception as exc:
                print(f"Server: Pipeline restart failed: {exc}")
            return False

        GLib.idle_add(_restart)

    def _run_on_glib(self, fn, *args) -> None:
        if self._glib_thread_id == threading.get_ident():
            fn(*args)
            return
        if not self.loop or not self._glib_ready.is_set():
            print(f"Server: GLib loop is not ready; skipped {getattr(fn, '__name__', fn)}")
            return

        def _runner():
            try:
                fn(*args)
            except Exception as exc:
                print(f"Server: GLib task {getattr(fn, '__name__', fn)} failed: {exc}")
                import traceback

                traceback.print_exc()
                with self._ws_lock:
                    ws = self._ws_conn
                    ws_loop = self._ws_loop
                if ws is not None and ws_loop is not None:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            ws.send(json.dumps({"type": "error", "message": str(exc)})),
                            ws_loop,
                        )
                    except Exception:
                        pass
            return False

        GLib.idle_add(_runner)

    def _call_on_glib_sync(self, fn, *args, timeout: float = 5.0):
        if self._glib_thread_id == threading.get_ident():
            return fn(*args)
        if not self.loop or not self._glib_ready.is_set():
            raise RuntimeError("GLib loop is not ready")

        done = threading.Event()
        result = {"value": None, "error": None}

        def _runner():
            try:
                result["value"] = fn(*args)
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()
            return False

        GLib.idle_add(_runner)
        if not done.wait(timeout):
            raise TimeoutError(f"Timed out waiting for {getattr(fn, '__name__', fn)} on GLib loop")
        if result["error"] is not None:
            raise result["error"]
        return result["value"]

    def _start_glib_loop(self) -> None:
        if self.glib_thread and self.glib_thread.is_alive():
            if not self._glib_ready.wait(timeout=2.0):
                raise RuntimeError("GLib loop did not become ready")
            return

        self._glib_ready.clear()

        def run_glib():
            self._glib_thread_id = threading.get_ident()
            self.loop = GLib.MainLoop()
            self._glib_ready.set()
            try:
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda *_: self.graceful_exit())
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda *_: self.graceful_exit())
                self.loop.run()
            finally:
                self._glib_ready.clear()
                self._glib_thread_id = None
                self.loop = None

        self.glib_thread = threading.Thread(target=run_glib, daemon=True)
        self.glib_thread.start()
        if not self._glib_ready.wait(timeout=2.0):
            raise RuntimeError("GLib loop did not become ready")

    def _select_payload_from_offer(self, sdp_text: str) -> int:
        h264_pt = None
        for line in sdp_text.splitlines():
            if line.startswith("a=rtpmap:"):
                try:
                    payload_part = line.split(":", 1)[1]
                    pt_str, codec_part = payload_part.split(" ", 1)
                    codec = codec_part.split("/", 1)[0].upper()
                    pt = int(pt_str)
                except Exception:
                    continue
                if codec == "H264":
                    h264_pt = pt
        if h264_pt is not None:
            return h264_pt
        return 96

    def _ensure_pipeline_for_offer(self, sdp_text: str) -> None:
        payload_pt = self._select_payload_from_offer(sdp_text)
        if self._payload_pt != payload_pt:
            print(f"Server: Switching payload pt to {payload_pt}")
            self._payload_pt = payload_pt
            self._stop_pipeline()
        if not self.pipeline:
            self._start_pipeline()
        self._attach_ice_handler()

    def _handle_offer_from_ws(self, sdp_text: str, ws) -> None:
        self._ensure_pipeline_for_offer(sdp_text)
        self._force_sendonly()
        self._handle_offer(sdp_text, ws)

    def _attach_ice_handler(self) -> None:
        if not self.webrtc or not self._ice_handler_cb:
            return
        try:
            if self._ice_handler_id and self._ice_handler_target is self.webrtc:
                self.webrtc.disconnect(self._ice_handler_id)
        except Exception:
            pass
        self._ice_handler_id = self.webrtc.connect("on-ice-candidate", self._ice_handler_cb)
        self._ice_handler_target = self.webrtc

    async def ws_handler(self, ws, path=None):
        if self.auth_token:
            req_path = path or getattr(ws, "path", "") or ""
            if not req_path:
                request = getattr(ws, "request", None)
                req_path = getattr(request, "path", "") if request is not None else ""
            if not req_path:
                req_path = getattr(ws, "request_uri", "") or ""
            token = None
            try:
                token = parse_qs(urlsplit(req_path).query).get("token", [None])[0]
            except Exception:
                token = None
            if token != self.auth_token:
                print("Server: Rejected unauthorized WebSocket client")
                try:
                    await ws.close(code=4001, reason="unauthorized")
                except Exception:
                    pass
                return

        remote = getattr(ws, "remote_address", path)
        print(f"Server: WebSocket connection attempt from {remote}")

        with self._ws_lock:
            if self._ws_conn is not None:
                print("Server: Rejected extra WebSocket client while a stream is active")
                try:
                    await ws.close(code=4002, reason="stream already has a client")
                except Exception:
                    pass
                return
            self._ws_generation += 1
            ws_generation = self._ws_generation
            self._ws_loop = asyncio.get_running_loop()
            self._ws_conn = ws
            self._ice_out_count = 0
            self._ice_in_count = 0

        try:
            await ws.send(json.dumps({"type": "hello"}))
        except Exception as exc:
            print(f"Server: Failed to send hello: {exc}")

        def on_ice_candidate(element, mlineindex, candidate):
            if not self._ws_loop:
                return
            if candidate is None or candidate == "":
                print("Server: ICE candidate out end-of-candidates")
                return
            self._ice_out_count += 1
            if self._ice_out_count <= 3:
                print(f"Server: ICE candidate out #{self._ice_out_count} mline={mlineindex} {candidate}")
            msg = json.dumps({
                "type": "candidate",
                "candidate": candidate,
                "sdpMLineIndex": int(mlineindex),
            })
            asyncio.run_coroutine_threadsafe(ws.send(msg), self._ws_loop)

        self._ice_handler_cb = on_ice_candidate
        self._run_on_glib(self._attach_ice_handler)

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception as exc:
                    print(f"Server: Failed to parse WS message: {exc}")
                    continue

                msg_type = msg.get("type")
                if msg_type == "hello":
                    print("Server: Client hello")
                    continue
                if msg_type == "offer":
                    sdp = msg.get("sdp")
                    if not sdp:
                        print("Server: Offer missing SDP")
                        continue
                    print("Server: Received offer")
                    self._run_on_glib(self._handle_offer_from_ws, sdp, ws)
                elif msg_type == "candidate":
                    cand = msg.get("candidate")
                    mline = msg.get("sdpMLineIndex")
                    if cand is None or mline is None:
                        print("Server: Candidate missing fields")
                        continue
                    try:
                        if not self.webrtc:
                            print("Server: Candidate arrived before WebRTC pipeline was ready")
                            continue
                        self._ice_in_count += 1
                        if self._ice_in_count <= 3:
                            print(f"Server: ICE candidate in #{self._ice_in_count} mline={mline} {cand}")
                        self._run_on_glib(self.webrtc.emit, "add-ice-candidate", int(mline), cand)
                    except Exception as exc:
                        print(f"Server: add-ice-candidate failed: {exc}")
                elif msg_type == "telemetry":
                    self._handle_receiver_telemetry(msg)
                else:
                    print(f"Server: Unknown WS message type: {msg_type}")
        except Exception as exc:
            print(f"Server: WebSocket handler error: {exc}")
        finally:
            try:
                code = getattr(ws, "close_code", None)
                reason = getattr(ws, "close_reason", None)
                print(f"Server: WebSocket disconnected (code={code}, reason={reason})")
            except Exception:
                print("Server: WebSocket disconnected")
            try:
                if self.webrtc and self._ice_handler_id and self._ice_handler_target is self.webrtc:
                    self._run_on_glib(self.webrtc.disconnect, self._ice_handler_id)
            except Exception:
                pass
            with self._ws_lock:
                is_current = self._ws_conn is ws and self._ws_generation == ws_generation
                if is_current:
                    self._ws_conn = None
                    self._ws_loop = None
                    self._ice_handler_cb = None
            if is_current:
                self._run_on_glib(self._stop_pipeline)

    def _handle_offer(self, sdp_text: str, ws) -> None:
        res, sdp_msg = GstSdp.SDPMessage.new()
        if res != GstSdp.SDPResult.OK:
            print("Server: Failed to create SDP message")
            return
        parse_res = GstSdp.sdp_message_parse_buffer(sdp_text.encode("utf-8"), sdp_msg)
        if parse_res != GstSdp.SDPResult.OK:
            print(f"Server: Failed to parse SDP offer ({parse_res})")
            return
        offer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, sdp_msg)
        sent = {"done": False}

        def _send_answer_sdp(sdp_text: str) -> None:
            if sent["done"]:
                return
            if not self._ws_loop or self._ws_conn is not ws:
                return
            sent["done"] = True
            try:
                lines = [line for line in sdp_text.splitlines() if line.startswith("a=candidate")]
                if lines:
                    print(f"Server: Local SDP candidates ({min(len(lines), 3)} shown)")
                    for line in lines[:3]:
                        print(f"Server: {line}")
            except Exception:
                pass
            print("Server: Sending answer")
            asyncio.run_coroutine_threadsafe(
                ws.send(json.dumps({"type": "answer", "sdp": sdp_text})),
                self._ws_loop,
            )

        def _on_remote_set(promise, _):
            try:
                reply = promise.get_reply()
                if reply and reply.has_field("error"):
                    err = reply.get_value("error")
                    print(f"Server: set-remote-description failed: {err}")
                    return
            except Exception:
                print("Server: set-remote-description failed (unknown error)")
                return

            def _on_answer_created(answer_promise, __):
                reply = answer_promise.get_reply()
                answer = reply.get_value("answer") if reply else None
                if not answer:
                    print("Server: create-answer returned no SDP")
                    return
                self.webrtc.emit("set-local-description", answer, Gst.Promise.new())
                text = answer.sdp.as_text()
                try:
                    gather_state = self.webrtc.get_property("ice-gathering-state")
                except Exception:
                    gather_state = None

                if gather_state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
                    _send_answer_sdp(text)
                    return

                def _wait_for_gather():
                    if sent["done"]:
                        return False
                    try:
                        state = self.webrtc.get_property("ice-gathering-state")
                    except Exception:
                        state = None
                    if state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
                        try:
                            local_desc = self.webrtc.get_property("local-description")
                            if local_desc and local_desc.sdp:
                                _send_answer_sdp(local_desc.sdp.as_text())
                                return False
                        except Exception:
                            _send_answer_sdp(text)
                            return False
                    return True

                GLib.timeout_add(200, _wait_for_gather)
                _send_answer_sdp(text)

            answer_promise = Gst.Promise.new_with_change_func(_on_answer_created, None)
            self.webrtc.emit("create-answer", None, answer_promise)

        set_remote_promise = Gst.Promise.new_with_change_func(_on_remote_set, None)
        self.webrtc.emit("set-remote-description", offer, set_remote_promise)

    def start_http_ws(self):
        if not self.http_server:
            self.http_server = StaticHttpServer(HOST, HTTP_PORT, config_provider=self._http_runtime_config)
            self.http_server.start()
        if not self.ws_server:
            self.ws_server = WebSocketServer(HOST, WS_PORT)
            self.ws_server.start(self.ws_handler)

    def stop_http_ws(self):
        if self.ws_server:
            self.ws_server.stop()
            self.ws_server = None
        if self.http_server:
            self.http_server.stop()
            self.http_server = None

    def start_stream(self):
        self.auth_token = secrets.token_urlsafe(24)
        self._start_telemetry_log()
        self._start_glib_loop()
        self.start_screen_capture()
        self.start_http_ws()
        host_ip = self._resolve_host_ip()
        if host_ip and self.auto_launch_client:
            url = self.client_url(host_ip=host_ip)
            if url:
                print(f"Server: Client URL {url}")
                try_adb_launch(url, serial=self.adb_serial)

    def stop_stream(self):
        self.stop_http_ws()
        self.stop_screen_capture()
        if self.loop:
            try:
                self.loop.quit()
            except Exception:
                pass
            self.loop = None
        if self.glib_thread:
            try:
                self.glib_thread.join(timeout=2.0)
            except Exception:
                pass
            self.glib_thread = None
        self.auth_token = None

    def graceful_exit(self) -> bool:
        print("Server: cleaning up...")
        if self.portal.active():
            self.stop_stream()
        return True

    def _resolve_host_ip(self) -> str | None:
        if HOST_IP:
            return HOST_IP
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            sock.close()
            return ip
        except Exception:
            return None

    def _http_runtime_config(self) -> dict:
        return {"ws_port": int(WS_PORT)}

    def client_url(self, host_ip: str | None = None) -> str | None:
        ip = host_ip or self._resolve_host_ip()
        if not ip:
            return None
        query: dict[str, str] = {}
        if self.auth_token:
            query["token"] = self.auth_token
        if self.telemetry_overlay:
            query["stats"] = "1"
        if self.receiver_jitter_target_ms is not None:
            query["jitter_target_ms"] = str(self.receiver_jitter_target_ms)
        encoded = urlencode(query)
        if encoded:
            return f"http://{ip}:{HTTP_PORT}?{encoded}"
        return f"http://{ip}:{HTTP_PORT}"

    def _profile_webrtc_latency_ms(self) -> int:
        if self.stream_profile == "realtime":
            return 250
        if self.stream_profile == "quality":
            return 1300
        if self.stream_profile == "delay":
            return 2200
        return int(self.custom_profile.get("webrtc_latency_ms", 900))

    def _stream_video_parameters(self) -> tuple[int, int, int]:
        if self.stream_format == "720p30":
            return 1280, 720, 30
        if self.stream_format == "720p60":
            return 1280, 720, 60
        return 1920, 1080, 30

    def _profile_queue_time_ms(self) -> int:
        if self.stream_profile == "realtime":
            return 0
        if self.stream_profile == "quality":
            return 8000
        if self.stream_profile == "delay":
            return 15000
        return int(self.custom_profile.get("queue_time_ms", 12000))

    def _profile_bitrate_multiplier(self) -> float:
        if self.stream_profile == "realtime":
            return 1.0
        if self.stream_profile == "quality":
            return 1.8
        if self.stream_profile == "delay":
            return 1.4
        try:
            return float(self.custom_profile.get("bitrate_multiplier", 1.6))
        except Exception:
            return 1.6

    def _profile_keyint(self) -> int:
        if self.stream_profile == "realtime":
            return 15
        if self.stream_profile == "quality":
            return 45
        if self.stream_profile == "delay":
            return 30
        return int(self.custom_profile.get("keyint", 45))
