/* global cast */

(function () {
  const CONTROL_NS = "urn:x-cast:io.convergence.control";

  const video = document.getElementById("v");
  const statusEl = document.getElementById("status");

  let ws = null;
  let pc = null;
  let serverUrl = null;
  let reconnects = 0;
  let offerInFlight = false;
  let remoteDescriptionSet = false;
  let pendingRemoteCandidates = [];

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  function cleanupPeer() {
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
    setTimeout(connectSignaling, 500);
  }

  function signalingWsUrl(httpUrl) {
    let u;
    try {
      u = new URL(httpUrl);
    } catch (_) {
      return null;
    }
    const proto = u.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + u.hostname + ":8767";
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

  async function createAndSendOffer() {
    if (!pc || !ws || offerInFlight) return;
    try {
      offerInFlight = true;
      const offer = await pc.createOffer({ offerToReceiveVideo: true });
      await pc.setLocalDescription(offer);
      ws.send(JSON.stringify({ type: "offer", sdp: offer.sdp }));
      setStatus("Offer sent");
    } catch (err) {
      setStatus("Offer error: " + err);
    } finally {
      offerInFlight = false;
    }
  }

  function startPeer() {
    cleanupPeer();
    pc = new RTCPeerConnection({ bundlePolicy: "max-bundle" });

    pc.ontrack = (ev) => {
      if (video.srcObject !== ev.streams[0]) {
        video.srcObject = ev.streams[0];
      }
      video.play().catch(() => {});
      setStatus("Streaming");
    };

    pc.oniceconnectionstatechange = () => {
      setStatus("ICE: " + pc.iceConnectionState);
    };

    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed" || pc.connectionState === "disconnected") {
        scheduleReconnect("peer " + pc.connectionState);
      }
    };

    pc.onicecandidate = (ev) => {
      if (!ev.candidate || !ws) return;
      ws.send(JSON.stringify({
        type: "candidate",
        candidate: ev.candidate.candidate,
        sdpMLineIndex: ev.candidate.sdpMLineIndex,
      }));
    };

    pc.addTransceiver("video", { direction: "recvonly" });
    pc.onnegotiationneeded = () => createAndSendOffer();
    createAndSendOffer();
  }

  function connectSignaling() {
    if (!serverUrl) {
      setStatus("Waiting for sender start command");
      return;
    }

    const wsUrl = signalingWsUrl(serverUrl);
    if (!wsUrl) {
      setStatus("Invalid server URL");
      return;
    }

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      reconnects = 0;
      setStatus("WS open");
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

      if (msg.type === "answer") {
        if (!pc) return;
        try {
          await pc.setRemoteDescription({ type: "answer", sdp: msg.sdp });
          remoteDescriptionSet = true;
          await applyPendingCandidates();
        } catch (err) {
          scheduleReconnect("answer apply failed: " + err);
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
        scheduleReconnect("server error");
      }
    };

    ws.onerror = () => setStatus("WS error");
    ws.onclose = () => scheduleReconnect("ws close");
  }

  function setupCastControl() {
    const context = cast.framework.CastReceiverContext.getInstance();
    const options = new cast.framework.CastReceiverOptions();
    options.disableIdleTimeout = true;

    const bus = context.getCastMessageBus(CONTROL_NS);
    bus.onMessage = (event) => {
      let data = null;
      try {
        data = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      if (!data || data.type !== "start" || !data.url) return;
      serverUrl = data.url;
      setStatus("Starting stream: " + serverUrl);
      scheduleReconnect("new start command");
    };

    context.start(options);
    setStatus("Receiver ready");
  }

  setupCastControl();
})();
