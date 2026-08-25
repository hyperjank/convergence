#!/usr/bin/env python3
"""Textual frontend for convergence."""

from __future__ import annotations

from dataclasses import asdict

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from controller import ActionResult, AppSnapshot, ConvergenceController


class TextPromptScreen(ModalScreen[str | None]):
    CSS = """
    TextPromptScreen {
        align: center middle;
    }
    #prompt-box {
        width: 72;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #prompt-title {
        text-style: bold;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Submit"),
    ]

    def __init__(self, title: str, placeholder: str = "", value: str = "") -> None:
        super().__init__()
        self.title = title
        self.placeholder = placeholder
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Static(self.title, id="prompt-title")
            yield Input(value=self.value, placeholder=self.placeholder, id="prompt-input")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#prompt-input", Input).value)


class PackagePickerScreen(ModalScreen[str | None]):
    CSS = """
    PackagePickerScreen {
        align: center middle;
    }
    #package-box {
        width: 96;
        height: 32;
        border: round $accent;
        background: $surface;
        padding: 1;
    }
    #package-help {
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Launch"),
    ]

    def __init__(self, packages: list[str]) -> None:
        super().__init__()
        self._all_packages = packages

    def compose(self) -> ComposeResult:
        with Vertical(id="package-box"):
            yield Static("Launch Android App", id="package-title")
            yield Input(placeholder="Filter packages", id="package-filter")
            yield DataTable(id="package-table")
            yield Static("Type to filter. Enter launches the selected package.", id="package-help")

    def on_mount(self) -> None:
        table = self.query_one("#package-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Package")
        self._populate(self._all_packages)
        self.query_one("#package-filter", Input).focus()

    def _populate(self, packages: list[str]) -> None:
        table = self.query_one("#package-table", DataTable)
        table.clear(columns=False)
        for package in packages:
            table.add_row(package, key=package)
        if packages:
            table.move_cursor(row=0, column=0)

    def _filtered_packages(self) -> list[str]:
        query = self.query_one("#package-filter", Input).value.strip().lower()
        if not query:
            return self._all_packages
        return [package for package in self._all_packages if query in package.lower()]

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "package-filter":
            self._populate(self._filtered_packages())

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        table = self.query_one("#package-table", DataTable)
        if table.row_count == 0:
            self.dismiss(None)
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        self.dismiss(str(row_key.value) if row_key is not None else None)


class SnapshotLoaded(Message):
    def __init__(self, snapshot: AppSnapshot) -> None:
        self.snapshot = snapshot
        super().__init__()


class ConvergenceTUI(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }
    #main {
        height: 1fr;
    }
    .pane {
        border: round $panel;
        padding: 0 1;
        height: 1fr;
    }
    #devices-pane {
        width: 34;
    }
    #sessions-pane {
        width: 1fr;
    }
    #info-pane {
        width: 42;
    }
    #summary {
        height: 5;
        border: round $accent;
        padding: 1;
        margin-bottom: 1;
    }
    #actions {
        height: auto;
        margin-top: 1;
    }
    #log {
        height: 12;
        border: round $accent;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("r", "refresh", "Refresh", priority=True),
        Binding("s", "set_source", "Set Source", priority=True),
        Binding("t", "set_target", "Set Target", priority=True),
        Binding("n", "launch_normal", "Launch Normal", priority=True),
        Binding("a", "launch_app", "Launch App", priority=True),
        Binding("m", "toggle_monitor", "Toggle Monitor", priority=True),
        Binding("x", "stop_all", "Stop All", priority=True),
        Binding("k", "stop_session", "Stop Session", priority=True),
        Binding("c", "connect_adb", "ADB Connect", priority=True),
    ]

    def __init__(self, controller: ConvergenceController) -> None:
        super().__init__()
        self.controller = controller
        self.snapshot_data: AppSnapshot | None = None
        self._refreshing = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading convergence state…", id="summary")
        with Horizontal(id="main"):
            with Vertical(classes="pane", id="devices-pane"):
                yield Static("Devices and Targets")
                yield DataTable(id="devices-table")
            with Vertical(classes="pane", id="sessions-pane"):
                yield Static("Active Sessions")
                yield DataTable(id="sessions-table")
            with Vertical(classes="pane", id="info-pane"):
                yield Static("Details")
                yield Static("", id="details")
                yield Static("", id="actions")
        yield RichLog(id="log", wrap=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        devices = self.query_one("#devices-table", DataTable)
        devices.cursor_type = "row"
        devices.add_columns("Kind", "Name", "State")

        sessions = self.query_one("#sessions-table", DataTable)
        sessions.cursor_type = "row"
        sessions.add_columns("ID", "Mode", "Serial", "Package", "Display", "Auto")

        self.set_interval(5, self.action_refresh)
        self.action_refresh()

    def _log_result(self, result: ActionResult) -> None:
        log = self.query_one("#log", RichLog)
        style = "green" if result.ok else "red"
        log.write(f"[{style}]{result.message}[/{style}]")

    def _selected_device_row(self) -> dict | None:
        table = self.query_one("#devices-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        if row_key is None or row_key.value is None:
            return None
        raw = str(row_key.value)
        kind, _, value = raw.partition(":")
        if not kind or not value:
            return None
        if kind == "android":
            return {"kind": "android", "serial": value}
        if kind == "chromecast":
            return {"kind": "chromecast", "name": value}
        return None

    def _selected_session_id(self) -> int | None:
        table = self.query_one("#sessions-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        if row_key is None:
            return None
        value = row_key.value
        return int(value) if value is not None else None

    def _render_snapshot(self, snapshot: AppSnapshot) -> None:
        self.snapshot_data = snapshot
        self.query_one("#summary", Static).update(
            "\n".join(
                [
                    f"Source: {snapshot.app_source_serial or 'none'}",
                    f"Target: {snapshot.monitor_target_type}:{snapshot.monitor_target_value or 'none'}",
                    f"Monitor: {'running' if snapshot.monitor_running else 'stopped'}",
                    f"Sessions: {len(snapshot.sessions)}",
                ]
            )
        )

        device_table = self.query_one("#devices-table", DataTable)
        device_table.clear(columns=False)
        for device in snapshot.android_devices:
            selected = []
            if snapshot.app_source_serial == device.serial:
                selected.append("source")
            if (
                snapshot.monitor_target_type == "android"
                and snapshot.monitor_target_value == device.serial
            ):
                selected.append("target")
            state = device.state
            if selected:
                state = f"{state} [{' '.join(selected)}]"
            label = device.model or device.serial
            device_table.add_row(
                "android",
                label,
                state,
                key=f"android:{device.serial}",
            )
        for cast in snapshot.chromecasts:
            state = "target" if (
                snapshot.monitor_target_type == "chromecast"
                and snapshot.monitor_target_value == cast.name
            ) else ""
            device_table.add_row(
                "chromecast",
                cast.name,
                state,
                key=f"chromecast:{cast.name}",
            )
        if device_table.row_count:
            device_table.move_cursor(row=0, column=0)

        sessions_table = self.query_one("#sessions-table", DataTable)
        sessions_table.clear(columns=False)
        for session in snapshot.sessions:
            sessions_table.add_row(
                str(session.session_id),
                session.mode,
                session.serial,
                session.package or "",
                str(session.display_id or ""),
                session.auto_close_state,
                key=session.session_id,
            )
        if sessions_table.row_count:
            sessions_table.move_cursor(row=0, column=0)

        details = {
            "monitor_url": snapshot.monitor_url,
            "android_devices": len(snapshot.android_devices),
            "chromecasts": len(snapshot.chromecasts),
            "sessions": [asdict(session) for session in snapshot.sessions],
        }
        self.query_one("#details", Static).update(jsonish(details))
        self.query_one("#actions", Static).update(
            "\n".join(
                [
                    "Keys",
                    "r refresh",
                    "s set selected Android row as source",
                    "t set selected row as monitor target",
                    "n launch normal scrcpy",
                    "a choose app and launch virtual display",
                    "m start/stop monitor stream",
                    "k stop selected session",
                    "x stop all sessions",
                    "c adb connect",
                ]
            )
        )

    def _refresh_in_worker(self) -> None:
        try:
            snapshot = self.controller.snapshot()
            self.call_from_thread(self.post_message, SnapshotLoaded(snapshot))
        finally:
            self.call_from_thread(setattr, self, "_refreshing", False)

    def action_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self.run_worker(self._refresh_in_worker, thread=True, exclusive=True)

    def on_snapshot_loaded(self, message: SnapshotLoaded) -> None:
        self._render_snapshot(message.snapshot)

    def action_set_source(self) -> None:
        row = self._selected_device_row()
        if not row or row.get("kind") != "android":
            self._log_result(ActionResult(False, "Select an Android device row first"))
            return
        self._log_result(self.controller.select_app_device(row["serial"]))
        self.action_refresh()

    def action_set_target(self) -> None:
        row = self._selected_device_row()
        if not row:
            self._log_result(ActionResult(False, "Select a device or Chromecast row first"))
            return
        if row.get("kind") == "android":
            result = self.controller.select_monitor_target("android", row["serial"])
        else:
            result = self.controller.select_monitor_target("chromecast", row["name"])
        self._log_result(result)
        self.action_refresh()

    def action_launch_normal(self) -> None:
        self._log_result(self.controller.launch_normal())
        self.action_refresh()

    def action_launch_app(self) -> None:
        result, packages = self.controller.list_launchable_packages()
        if not result.ok:
            self._log_result(result)
            return

        def _launch(package: str | None) -> None:
            if not package:
                return
            self._log_result(self.controller.launch_app(package))
            self.action_refresh()

        self.push_screen(PackagePickerScreen(packages), _launch)

    def action_toggle_monitor(self) -> None:
        if self.snapshot_data and self.snapshot_data.monitor_running:
            result = self.controller.stop_monitor()
        else:
            result = self.controller.start_monitor()
        self._log_result(result)
        self.action_refresh()

    def action_stop_all(self) -> None:
        self._log_result(self.controller.stop_all_scrcpy())
        self.action_refresh()

    def action_stop_session(self) -> None:
        session_id = self._selected_session_id()
        if session_id is None:
            self._log_result(ActionResult(False, "Select a session row first"))
            return
        self._log_result(self.controller.stop_session(session_id))
        self.action_refresh()

    def action_connect_adb(self) -> None:
        def _connect(endpoint: str | None) -> None:
            if endpoint is None:
                return
            self._log_result(self.controller.adb_connect(endpoint))
            self.action_refresh()

        self.push_screen(TextPromptScreen("ADB Connect", "IP:PORT"), _connect)

    def on_unmount(self) -> None:
        self.controller.shutdown()


def jsonish(value: object) -> str:
    import json

    return json.dumps(value, indent=2, sort_keys=True)


def main() -> int:
    from convergence import build_controller

    app = ConvergenceTUI(build_controller())
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
