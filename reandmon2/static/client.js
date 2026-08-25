/* Minimal WebRTC player with WebSocket signaling. */

(function () {
  const video = document.getElementById("v");
  const msgEl = document.getElementById("msg");
  const statsEl = document.getElementById("stats");
  const mode = (() => {
    try {
      const p = new URLSearchParams(location.search || "");
      const m = (p.get("mode") || "").toLowerCase();
      if (m === "quality" || m === "delay" || m === "custom") return m;
    } catch (_) {}
    return "realtime";
  })();
  const authToken = (() => {
    try {
      const p = new URLSearchParams(location.search || "");
      const t = (p.get("token") || "").trim();
      return t || null;
    } catch (_) {}
    return null;
  })();
  const showStats = (() => {
    try {
      const value = (new URLSearchParams(location.search || "").get("stats") || "").toLowerCase();
      return value === "1" || value === "true" || value === "yes";
    } catch (_) {
      return false;
    }
  })();
  const requestedJitterBufferTargetMs = (() => {
    try {
      const raw = new URLSearchParams(location.search || "").get("jitter_target_ms");
      if (raw === null || raw.trim() === "") return null;
      const value = Number(raw);
      return Number.isFinite(value) && value >= 0 && value <= 4000 ? value : null;
    } catch (_) {
      return null;
    }
  })();
  let wsPort = 8767;

  let ws = null;
  let pc = null;
  let reconnects = 0;
  let offerInFlight = false;
  let remoteDescriptionSet = false;
  let pendingRemoteCandidates = [];
  let waitingForVideoTimer = null;
  let playbackTimer = null;
  let stalledTicks = 0;
  let lastVideoTime = 0;
  let gotVideoTrack = false;
  let telemetryTimer = null;
  let telemetryPrevious = null;
  let renderedFrames = 0;
  let jitterBufferTargetSupported = false;
  let jitterBufferTargetReadbackMs = null;
  let jitterBufferTargetApplied = null;

  if (statsEl && showStats) statsEl.classList.add("visible");

  video.muted = true;
  video.defaultMuted = true;
  video.autoplay = true;
  video.playsInline = true;
  video.controls = false;

  function setStatus(text) {
    if (msgEl) msgEl.textContent = text;
  }

  function clearConnectedStatus() {
    if (!msgEl) return;
    const text = msgEl.textContent || "";
    if (
      text === "track received" ||
      text === "answer recv" ||
      text.startsWith("ICE: connected") ||
      text.startsWith("ICE: completed")
    ) {
      setStatus("");
    }
  }

  function finite(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function round(value, digits) {
    if (!Number.isFinite(value)) return null;
    const scale = Math.pow(10, digits || 0);
    return Math.round(value * scale) / scale;
  }

  function delta(current, previous) {
    if (!Number.isFinite(current) || !Number.isFinite(previous)) return null;
    return Math.max(0, current - previous);
  }

  function averageDelayMs(total, emitted, previousTotal, previousEmitted) {
    const totalDelta = delta(total, previousTotal);
    const emittedDelta = delta(emitted, previousEmitted);
    if (totalDelta !== null && emittedDelta > 0) return (totalDelta / emittedDelta) * 1000;
    if (Number.isFinite(total) && Number.isFinite(emitted) && emitted > 0) {
      return (total / emitted) * 1000;
    }
    return null;
  }

  function formatMetric(value, suffix, digits) {
    return Number.isFinite(value) ? value.toFixed(digits || 0) + suffix : "—";
  }

  function renderTelemetry(sample) {
    if (!statsEl || !showStats) return;
    const resolution = sample.width && sample.height ? sample.width + "×" + sample.height : "—";
    statsEl.textContent = [
      "Convergence receiver · " + resolution,
      "jitter buffer  " + formatMetric(sample.jitterBufferMs, " ms", 1) +
        "  target " + formatMetric(sample.jitterBufferTargetMs, " ms", 1) +
        "  min " + formatMetric(sample.jitterBufferMinimumMs, " ms", 1),
      "receiver target " +
        (sample.jitterBufferTargetSupported === 1
          ? formatMetric(sample.jitterBufferTargetRequestedMs, " ms requested", 0) +
            " / " + formatMetric(sample.jitterBufferTargetReadbackMs, " ms read back", 0)
          : "API unavailable"),
      "fps dec/render " + formatMetric(sample.decodeFps, "", 1) +
        " / " + formatMetric(sample.renderFps, "", 1) +
        "  dropped +" + formatMetric(sample.framesDroppedDelta, "", 0),
      "loss " + formatMetric(sample.packetLossPercent, "%", 2) +
        "  RTT " + formatMetric(sample.rttMs, " ms", 1) +
        "  jitter " + formatMetric(sample.jitterMs, " ms", 1),
      "receive " + formatMetric(sample.bitrateMbps, " Mbit/s", 2),
    ].join("\n");
  }

  function configureReceiverJitterBuffer(receiver) {
    jitterBufferTargetSupported = Boolean(receiver && "jitterBufferTarget" in receiver);
    jitterBufferTargetReadbackMs = null;
    jitterBufferTargetApplied = requestedJitterBufferTargetMs === null ? null : false;
    if (!jitterBufferTargetSupported) return;
    try {
      if (requestedJitterBufferTargetMs !== null) {
        receiver.jitterBufferTarget = requestedJitterBufferTargetMs;
      }
      jitterBufferTargetReadbackMs = finite(receiver.jitterBufferTarget);
      if (requestedJitterBufferTargetMs !== null) {
        jitterBufferTargetApplied = jitterBufferTargetReadbackMs === requestedJitterBufferTargetMs;
      }
    } catch (_) {
      jitterBufferTargetReadbackMs = null;
      jitterBufferTargetApplied = false;
    }
  }

  async function collectTelemetry() {
    const peer = pc;
    if (!peer || !gotVideoTrack || peer.connectionState === "closed") return;
    try {
      const report = await peer.getStats();
      let inbound = null;
      let selectedPair = null;
      let transport = null;
      report.forEach((stat) => {
        if (stat.type === "inbound-rtp" && (stat.kind === "video" || stat.mediaType === "video")) {
          inbound = stat;
        } else if (stat.type === "transport") {
          transport = stat;
        }
      });
      if (transport && transport.selectedCandidatePairId) {
        selectedPair = report.get(transport.selectedCandidatePairId) || null;
      }
      if (!selectedPair) {
        report.forEach((stat) => {
          if (
            !selectedPair &&
            stat.type === "candidate-pair" &&
            stat.state === "succeeded" &&
            (stat.nominated || stat.selected)
          ) {
            selectedPair = stat;
          }
        });
      }
      if (!inbound) return;

      const now = performance.now();
      const previous = telemetryPrevious;
      const elapsedSeconds = previous ? Math.max(0.001, (now - previous.at) / 1000) : null;
      const emitted = finite(inbound.jitterBufferEmittedCount);
      const jitterDelay = finite(inbound.jitterBufferDelay);
      const targetDelay = finite(inbound.jitterBufferTargetDelay);
      const minimumDelay = finite(inbound.jitterBufferMinimumDelay);
      const decoded = finite(inbound.framesDecoded);
      const dropped = finite(inbound.framesDropped);
      const received = finite(inbound.packetsReceived);
      const lost = finite(inbound.packetsLost);
      const bytes = finite(inbound.bytesReceived);
      const renderedDelta = previous ? delta(renderedFrames, previous.renderedFrames) : null;
      const receivedDelta = previous ? delta(received, previous.received) : null;
      const lostDelta = previous ? delta(lost, previous.lost) : null;
      const packetTotal = (receivedDelta || 0) + (lostDelta || 0);

      const sample = {
        type: "telemetry",
        timestampMs: Date.now(),
        width: finite(inbound.frameWidth) || finite(video.videoWidth),
        height: finite(inbound.frameHeight) || finite(video.videoHeight),
        jitterBufferMs: round(averageDelayMs(
          jitterDelay,
          emitted,
          previous && previous.jitterDelay,
          previous && previous.emitted,
        ), 2),
        jitterBufferTargetMs: round(averageDelayMs(
          targetDelay,
          emitted,
          previous && previous.targetDelay,
          previous && previous.emitted,
        ), 2),
        jitterBufferMinimumMs: round(averageDelayMs(
          minimumDelay,
          emitted,
          previous && previous.minimumDelay,
          previous && previous.emitted,
        ), 2),
        jitterBufferTargetSupported: jitterBufferTargetSupported ? 1 : 0,
        jitterBufferTargetRequestedMs: requestedJitterBufferTargetMs,
        jitterBufferTargetReadbackMs: round(jitterBufferTargetReadbackMs, 2),
        jitterBufferTargetApplied:
          jitterBufferTargetApplied === null ? null : (jitterBufferTargetApplied ? 1 : 0),
        decodeFps: round(
          previous && elapsedSeconds ? delta(decoded, previous.decoded) / elapsedSeconds : finite(inbound.framesPerSecond),
          2,
        ),
        renderFps: round(renderedDelta !== null && elapsedSeconds ? renderedDelta / elapsedSeconds : null, 2),
        framesDropped: dropped,
        framesDroppedDelta: previous ? delta(dropped, previous.dropped) : 0,
        packetsLost: lost,
        packetsLostDelta: lostDelta,
        packetLossPercent: packetTotal > 0 ? round((lostDelta / packetTotal) * 100, 3) : 0,
        jitterMs: round(Number.isFinite(finite(inbound.jitter)) ? finite(inbound.jitter) * 1000 : null, 2),
        rttMs: round(
          selectedPair && Number.isFinite(finite(selectedPair.currentRoundTripTime))
            ? finite(selectedPair.currentRoundTripTime) * 1000
            : null,
          2,
        ),
        bitrateMbps: round(
          previous && elapsedSeconds ? (delta(bytes, previous.bytes) * 8) / elapsedSeconds / 1000000 : null,
          3,
        ),
      };

      telemetryPrevious = {
        at: now,
        emitted: emitted,
        jitterDelay: jitterDelay,
        targetDelay: targetDelay,
        minimumDelay: minimumDelay,
        decoded: decoded,
        dropped: dropped,
        received: received,
        lost: lost,
        bytes: bytes,
        renderedFrames: renderedFrames,
      };

      renderTelemetry(sample);
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(sample));
    } catch (_) {}
  }

  function startTelemetry() {
    if (telemetryTimer) return;
    telemetryPrevious = null;
    collectTelemetry();
    telemetryTimer = setInterval(collectTelemetry, 2000);
  }

  function stopTelemetry() {
    if (telemetryTimer) clearInterval(telemetryTimer);
    telemetryTimer = null;
    telemetryPrevious = null;
    if (statsEl) statsEl.textContent = "";
  }

  function countRenderedFrame() {
    renderedFrames += 1;
    if (video && typeof video.requestVideoFrameCallback === "function") {
      video.requestVideoFrameCallback(countRenderedFrame);
    }
  }

  if (video && typeof video.requestVideoFrameCallback === "function") {
    video.requestVideoFrameCallback(countRenderedFrame);
  }

  function ensurePlayback(reason) {
    if (!video || !video.srcObject) return;
    video.muted = true;
    video.defaultMuted = true;
    video.autoplay = true;
    video.playsInline = true;
    video.controls = false;

    const playPromise = video.play();
    if (playPromise && typeof playPromise.catch === "function") {
      playPromise.catch((err) => {
        const name = err && err.name ? err.name : String(err || "unknown");
        setStatus("play blocked: " + name + " (" + reason + ")");
      });
    }
  }

  function startPlaybackWatchdog() {
    if (playbackTimer) return;
    stalledTicks = 0;
    lastVideoTime = video ? video.currentTime || 0 : 0;
    playbackTimer = setInterval(() => {
      if (!video || !video.srcObject) return;
      if (video.paused || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
        ensurePlayback("watchdog");
      }
      const now = video.currentTime || 0;
      const advanced = now > lastVideoTime + 0.05;
      lastVideoTime = now;
      if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && !advanced) {
        stalledTicks += 1;
        ensurePlayback("stalled");
        if (stalledTicks === 8) {
          setStatus("video stalled");
        }
      } else {
        stalledTicks = 0;
      }
    }, 1000);
  }

  function stopPlaybackWatchdog() {
    if (!playbackTimer) return;
    clearInterval(playbackTimer);
    playbackTimer = null;
    stalledTicks = 0;
    lastVideoTime = 0;
  }

  async function loadRuntimeConfig() {
    try {
      const query = authToken ? "?token=" + encodeURIComponent(authToken) : "";
      const resp = await fetch("/__convergence_config" + query, { cache: "no-store" });
      if (!resp.ok) return;
      const cfg = await resp.json();
      const parsedPort = Number(cfg && cfg.ws_port);
      if (Number.isInteger(parsedPort) && parsedPort > 0 && parsedPort < 65536) {
        wsPort = parsedPort;
      }
    } catch (_) {}
  }

  function cleanupPeer() {
    if (waitingForVideoTimer) {
      clearTimeout(waitingForVideoTimer);
      waitingForVideoTimer = null;
    }
    stopPlaybackWatchdog();
    stopTelemetry();
    try {
      video.pause();
      video.srcObject = null;
      video.removeAttribute("src");
      video.load();
    } catch (_) {}
    if (pc) {
      try {
        pc.ontrack = null;
        pc.onicecandidate = null;
        pc.oniceconnectionstatechange = null;
        pc.onconnectionstatechange = null;
        pc.onnegotiationneeded = null;
        pc.close();
      } catch (_) {}
      pc = null;
    }
    offerInFlight = false;
    remoteDescriptionSet = false;
    pendingRemoteCandidates = [];
    gotVideoTrack = false;
  }

  function cleanupWs() {
    if (ws) {
      try {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onclose = null;
        ws.onerror = null;
        ws.close();
      } catch (_) {}
      ws = null;
    }
  }

  function scheduleReconnect(reason) {
    cleanupPeer();
    cleanupWs();
    reconnects += 1;
    setStatus("Reconnect: " + reason + " (" + reconnects + ")");
    if (reconnects >= 6) {
      try {
        location.reload();
      } catch (_) {}
      return;
    }
    setTimeout(connect, 500);
  }

  async function applyPendingCandidates() {
    if (!pc || !remoteDescriptionSet || !pendingRemoteCandidates.length) return;
    const toApply = pendingRemoteCandidates;
    pendingRemoteCandidates = [];
    for (const c of toApply) {
      try {
        await pc.addIceCandidate(c);
      } catch (_) {}
    }
  }

  function scheduleNoVideoWatchdog() {
    if (waitingForVideoTimer) {
      clearTimeout(waitingForVideoTimer);
      waitingForVideoTimer = null;
    }
    const timeoutMs =
      mode === "realtime" ? 8000 :
      mode === "quality" ? 22000 :
      mode === "delay" ? 30000 :
      28000;
    waitingForVideoTimer = setTimeout(() => {
      if (!gotVideoTrack) {
        scheduleReconnect("no video track after answer");
      }
    }, timeoutMs);
  }

  async function createAndSendOffer() {
    if (!pc || !ws || offerInFlight) return;
    try {
      offerInFlight = true;
      const offer = await pc.createOffer({
        offerToReceiveVideo: true,
      });
      await pc.setLocalDescription(offer);
      ws.send(
        JSON.stringify({
          type: "offer",
          sdp: offer.sdp,
        }),
      );
      setStatus("offer sent");
    } catch (err) {
      setStatus("offer error: " + err);
    } finally {
      offerInFlight = false;
    }
  }

  function startPeer() {
    cleanupPeer();
    const rtcConfig = {
      bundlePolicy: "max-bundle",
    };

    try {
      pc = new RTCPeerConnection(rtcConfig);
    } catch (err) {
      setStatus("RTCPeerConnection error: " + err);
      setTimeout(startPeer, 1000);
      return;
    }

    pc.ontrack = (ev) => {
      gotVideoTrack = true;
      configureReceiverJitterBuffer(ev.receiver);
      if (waitingForVideoTimer) {
        clearTimeout(waitingForVideoTimer);
        waitingForVideoTimer = null;
      }
      const stream = ev.streams && ev.streams[0] ? ev.streams[0] : new MediaStream([ev.track]);
      if (video.srcObject !== stream) {
        video.srcObject = stream;
      }
      setStatus("track received");
      ev.track.onunmute = () => ensurePlayback("track unmuted");
      ensurePlayback("track");
      startPlaybackWatchdog();
      startTelemetry();
    };

    pc.oniceconnectionstatechange = () => {
      const state = pc.iceConnectionState;
      if (
        (state === "connected" || state === "completed") &&
        gotVideoTrack &&
        !video.paused &&
        video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
      ) {
        setStatus("");
      } else {
        setStatus("ICE: " + state);
      }
    };

    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed") {
        scheduleReconnect("pc failed");
      }
    };

    pc.onicecandidate = (ev) => {
      if (!ev.candidate || !ws) return;
      ws.send(
        JSON.stringify({
          type: "candidate",
          candidate: ev.candidate.candidate,
          sdpMLineIndex: ev.candidate.sdpMLineIndex,
        }),
      );
    };

    const videoTransceiver = pc.addTransceiver("video", { direction: "recvonly" });
    configureReceiverJitterBuffer(videoTransceiver.receiver);

    pc.onnegotiationneeded = () => {
      createAndSendOffer();
    };

    createAndSendOffer();
  }

  function connect() {
    const scheme = location.protocol === "https:" ? "wss://" : "ws://";
    const tokenQuery = authToken ? "?token=" + encodeURIComponent(authToken) : "";
    const url = scheme + location.hostname + ":" + wsPort + "/" + tokenQuery;
    ws = new WebSocket(url);

    ws.onopen = () => {
      reconnects = 0;
      setStatus("ws open");
      try {
        ws.send(JSON.stringify({ type: "hello" }));
      } catch (_) {}
      startPeer();
    };

    ws.onmessage = async (ev) => {
      let msg = null;
      try {
        msg = JSON.parse(ev.data);
      } catch (_) {
        return;
      }

      if (msg.type === "hello") {
        setStatus("ws hello");
      } else if (msg.type === "answer") {
        if (!pc) return;
        setStatus("answer recv");
        try {
          await pc.setRemoteDescription({ type: "answer", sdp: msg.sdp });
          remoteDescriptionSet = true;
          await applyPendingCandidates();
          scheduleNoVideoWatchdog();
          ensurePlayback("answer");
        } catch (err) {
          setStatus("answer setRemoteDescription error: " + err);
          scheduleReconnect("answer apply failed");
        }
      } else if (msg.type === "candidate") {
        if (!pc) return;
        const candidate = {
          candidate: msg.candidate,
          sdpMLineIndex: msg.sdpMLineIndex,
        };
        if (!remoteDescriptionSet) {
          pendingRemoteCandidates.push(candidate);
          return;
        }
        try {
          await pc.addIceCandidate(candidate);
        } catch (_) {}
      } else if (msg.type === "restart") {
        scheduleReconnect("server restart");
      } else if (msg.type === "error") {
        setStatus("Server error: " + (msg.message || "unknown"));
      }
    };

    ws.onerror = () => {
      setStatus("ws error");
    };

    ws.onclose = (ev) => {
      const code = ev && typeof ev.code === "number" ? ev.code : "?";
      const reason = ev && ev.reason ? ev.reason : "";
      setStatus("ws closed: " + code + " " + reason);
      scheduleReconnect("close");
    };
  }

  function toggleFullscreen(ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    if (!document.fullscreenElement) {
      video.requestFullscreen && video.requestFullscreen();
    } else {
      document.exitFullscreen && document.exitFullscreen();
    }
  }

  function resumePlayback() {
    ensurePlayback("resume");
  }

  video.addEventListener("loadedmetadata", () => ensurePlayback("metadata"));
  video.addEventListener("canplay", () => ensurePlayback("canplay"));
  video.addEventListener("playing", clearConnectedStatus);
  video.addEventListener("pause", () => ensurePlayback("pause"));
  video.addEventListener("pointerup", toggleFullscreen, { passive: false });
  document.addEventListener("fullscreenchange", resumePlayback);
  document.addEventListener("visibilitychange", resumePlayback);

  loadRuntimeConfig().finally(connect);
})();
