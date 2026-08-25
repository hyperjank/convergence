# Convergence Chromecast WebRTC Receiver

This folder contains a custom Chromecast receiver app that speaks the same WebRTC signaling protocol as `reandmon2/static/client.js`.

## Files
- `index.html`: CAF receiver shell + fullscreen video element
- `receiver.js`: cast control channel + WebRTC player

## Message Namespace
- `urn:x-cast:io.convergence.control`
- Sender sends JSON: `{"type":"start","url":"http://HOST:8000"}`

## How to use
1. Host `cast_receiver/` over HTTPS (required by Cast receiver hosting).
2. In Google Cast SDK Developer Console:
   - Create/register a Web Receiver application.
   - Set receiver URL to your hosted `index.html`.
   - Publish to your test devices.
   - Copy the receiver App ID.
3. Set app id in convergence config:
   - `~/.config/convergence/config.json`
   - `monitor.chromecast_app_id = "YOUR_APP_ID"`
4. In tray:
   - Select `Monitor Target -> Chromecast: ...`
   - Start monitor stream.

If `chromecast_app_id` is unset, convergence falls back to DashCast.
