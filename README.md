# convergence

`convergence` is a Linux system-tray application and control surface for:
- launching and managing `scrcpy` Android sessions
- launching Android apps into virtual displays via `scrcpy --new-display`
- streaming your Linux desktop to a browser (WebRTC) and optionally casting that URL to Chromecast

The recovered PyQt tray frontend is the primary interface. It now uses the newer
frontend-neutral controller and backend that were developed alongside the CLI and
optional Textual TUI.

## What it does

- Device management
  - Detect connected Android devices via `adb`
  - Connect to remote devices with `adb connect IP:PORT`
- App source control
  - Launch normal `scrcpy` sessions
  - Launch selected Android packages in virtual display sessions
  - Track app-session state and auto-close app sessions when focus leaves target app
- Monitor streaming
  - Capture desktop using `xdg-desktop-portal` / PipeWire
  - Encode H.264 (auto-select hardware encoder if available)
  - Serve static WebRTC client at `http://<host-ip>:8000`
  - Signal over WebSocket on `ws://<host-ip>:8767`
  - Stream to Android browser via `adb am start VIEW`
  - Cast monitor URL to Chromecast using DashCast
  - Collect receiver-side jitter-buffer, FPS, drop, loss, RTT, and bitrate telemetry
  - Feature-detect `RTCRtpReceiver.jitterBufferTarget` and optionally request a 100 ms low-latency receiver buffer
  - Persist each stream's samples as JSONL under `~/.local/state/convergence/telemetry/`
- Virtual screen
  - Toggle a native Hyprland headless output from the tray
  - Configure it as an adjacent scale-1 display whose native resolution follows the selected 1080p30, 720p30, or 720p60 stream format
  - Send the active window directly to its workspace
  - Stop an active monitor stream before removing the virtual output
  - Remove the virtual output automatically when Convergence exits

## Requirements

Host environment:
- Linux desktop with PipeWire + `xdg-desktop-portal`
- Python 3.10+
- `adb`
- `scrcpy`
- GStreamer with:
  - `gstreamer1.0` core
  - `webrtc` plugin (`webrtcbin`)
  - RTP plugin (`rtph264pay`)
  - H.264 encoder support (`vah264enc`, `vaapih264enc`, `nvh264enc`, or `x264enc`)

Arch Linux packages used by this installation:

```bash
sudo pacman -S --needed \
  android-tools scrcpy python-pyqt6 python-websockets python-pychromecast \
  gst-plugin-pipewire gst-plugins-bad gst-plugins-good gst-plugins-ugly gst-plugin-va
```

Python packages:
- `PyQt6`
- `PyGObject` (`gi`)
- `dbus-python`
- `websockets`
- optional: `pychromecast` (Chromecast integration)
- optional: `textual` (terminal UI)

## Run

From project root:

```bash
python3 convergence.py
python3 convergence.py tray
python3 convergence.py status
python3 convergence.py devices
python3 convergence.py tui
python3 convergence.py start-monitor
python3 convergence.py virtual-screen toggle
python3 convergence.py send-window
python3 convergence.py launch-normal
```

Running with no arguments launches the tray. The CLI remains available for
scripting, debugging, and recovery. Settings live at:

`~/.config/convergence/config.json`

## Configuration

Settings are auto-created with defaults. Main sections:

- `app_source_device`: Android device serial used for app/scrcpy actions
- `monitor`:
  - `target_type`: `android` or `chromecast`
  - `target_value`: device serial or Chromecast name
  - `encoder`: `auto` or explicit encoder name
  - `chromecast_mode`: `realtime`, `quality`, `delay`, `custom`
  - `stream_format`: `1080p30` baseline, `720p30` stability, or `720p60` interactive mode
  - `realtime_queue_depth`: `4` baseline, `2` low latency, or `1` aggressive per leaky queue
  - `telemetry_overlay`: show detailed live receiver statistics on the next stream
  - `receiver_jitter_target_ms`: `null` for the browser's adaptive baseline or a target in milliseconds
  - `custom`: tuning values for custom Chromecast mode
- `scrcpy`:
  - `turn_screen_off`
  - `stay_awake`
  - `resolution` (virtual display, e.g. `1920x1080`)
  - `immersive_poke` (writes Android `policy_control` for app sessions)

## Typical workflow

1. Choose an app source device: `python3 convergence.py select-app-device SERIAL`
2. Choose a monitor target: `python3 convergence.py select-monitor-target android SERIAL`
3. For app mirroring:
   - `python3 convergence.py launch-normal`
   - `python3 convergence.py launch-app PACKAGE`
4. For desktop streaming:
   - Enable `Virtual Display: Off` in the tray
   - Move windows to the virtual workspace normally
   - Choose `Start Cast → TARGET` and select the headless output in the portal picker
5. Optional tools:
   - `python3 convergence.py reset-adb`
   - `python3 convergence.py connect-adb IP:PORT`
   - `python3 convergence.py set-encoder auto`
   - `python3 convergence.py set-cast-mode realtime`
   - Enable `Stream Settings → Show Receiver Telemetry` before starting a stream
   - Choose `Stream Settings → Advanced → Receiver Jitter Buffer → Low latency (100 ms)` for an experimental low-latency stream; use `Adaptive (baseline)` to revert
   - Choose `Stream Settings → Format → 720p30 (stability)` to reduce pixel throughput and realtime bitrate
   - Choose `Stream Settings → Format → 720p60 (interactive)` for shorter frame intervals after 720p30 is stable
   - Choose `Stream Settings → Realtime Queue Depth → 1 frame (aggressive)` for the lowest tested queue ceiling

## Repository layout

- `convergence.py`: entrypoint
- `controller.py`: frontend-neutral orchestration and workflow logic
- `tray_ui.py`: primary PyQt system-tray frontend
- `tui.py`: Textual dashboard frontend
- `device_manager.py`: adb wrapper/discovery
- `scrcpy_manager.py`: scrcpy session lifecycle + app watchdog
- `monitor_manager.py`: wrapper around monitor streaming controller
- `cast_manager.py`: Chromecast discovery and DashCast URL launch
- `settings.py`: persistent config store
- `reandmon2/`: monitor backend and browser client
  - `stream_controller.py`: PipeWire + GStreamer + signaling orchestration
  - `ws_server.py`: static HTTP + WebSocket servers
  - `portal*.py`: desktop portal session management
  - `static/index.html`, `static/client.js`: WebRTC receiver page
- `archived_experiments/`: old/experimental receiver work

## Notes and limitations

- Streaming endpoints bind to `0.0.0.0`.
- WebSocket signaling now requires a per-stream token embedded in the monitor URL query string (`?token=...`).
- Only one active monitor stream target is supported at a time.
- The monitor web client discovers the active WebSocket port from `GET /__convergence_config` (no hardcoded WS port).
- Receiver telemetry is sampled every two seconds, summarized in the tray, and
  written to a per-stream JSONL file. The optional on-receiver overlay is disabled by default.
- Receiver jitter-buffer control is a preference, not a guarantee. Browser support
  is feature-detected and the requested/read-back value is included in telemetry;
  actual jitter-buffer stats may still be unavailable on Chromecast.
- Encoder menus show only registered elements. Intel low-power VA (`vah264lpenc`)
  is available as an experimental constrained-baseline option; a continuous RTP
  watchdog returns to x264 and persists that fallback if hardware output stalls.
- The optional TUI focuses on state visibility, source/target selection, app launch,
  monitor control, and recovery actions.
- Run the smoke tests with `python3 -m unittest discover -s tests -v`.
