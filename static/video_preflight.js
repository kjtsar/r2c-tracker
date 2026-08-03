(function () {
  "use strict";

  const statusElement = document.getElementById("video-preflight");
  if (!statusElement) return;

  const requestId = statusElement.dataset.requestId || "";
  const designator = statusElement.dataset.designator || "";
  const formToken = statusElement.dataset.formToken || "";
  let iceServers = [];
  try {
    iceServers = JSON.parse(statusElement.dataset.iceServers || "[]");
  } catch (_error) {
    iceServers = [];
  }
  if (!window.RTCPeerConnection) {
    statusElement.textContent =
      "This browser does not support the required WebRTC link test.";
    statusElement.dataset.state = "error";
    return;
  }

  const offerUrl =
    `/${encodeURIComponent(designator)}/streams/requests/` +
    `${encodeURIComponent(requestId)}/preflight/offer`;
  const statusUrl =
    `/${encodeURIComponent(designator)}/streams/requests/` +
    `${encodeURIComponent(requestId)}/preflight/status`;
  // Preflight deliberately uses TURN. This avoids spending several seconds
  // trying host/server-reflexive paths and gives the pilot a conservative
  // measurement for the route that works through incident firewalls.
  const peer = new RTCPeerConnection({
    iceServers,
    iceTransportPolicy: "relay",
  });
  const channel = peer.createDataChannel("r2c-preflight", {
    ordered: true,
  });
  channel.binaryType = "arraybuffer";

  let answerApplied = false;
  let finished = false;
  let receivedBytes = 0;
  let acknowledgementPending = false;
  let latestSequence = 0;

  function sendAcknowledgement() {
    acknowledgementPending = false;
    if (channel.readyState !== "open") return;
    channel.send(JSON.stringify({
      type: "ack",
      sequence: latestSequence,
      receivedBytes,
    }));
  }

  function browserTransportState() {
    return (
      `ICE ${peer.iceConnectionState}, ` +
      `peer ${peer.connectionState}, ` +
      `channel ${channel.readyState}`
    );
  }

  peer.addEventListener("iceconnectionstatechange", () => {
    if (answerApplied && !finished) {
      show(
        `Establishing encrypted test channel (${browserTransportState()})…`,
        "connecting",
      );
    }
  });

  peer.addEventListener("connectionstatechange", () => {
    if (answerApplied && !finished && peer.connectionState === "failed") {
      show(`WebRTC connection failed (${browserTransportState()}).`, "error");
    }
  });

  function show(message, state) {
    statusElement.textContent = message;
    statusElement.dataset.state = state;
  }

  function waitForRelayCandidate(timeoutMs) {
    if (peer.localDescription?.sdp.includes(" typ relay ")) {
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const timeout = window.setTimeout(resolve, timeoutMs);
      peer.addEventListener("icecandidate", (event) => {
        if (!event.candidate || event.candidate.candidate.includes(" typ relay ")) {
          window.clearTimeout(timeout);
          window.setTimeout(resolve, 0);
        }
      });
    });
  }

  async function selectedRoute() {
    const report = await peer.getStats();
    let selectedPairId = "";
    report.forEach((entry) => {
      if (entry.type === "transport" && entry.selectedCandidatePairId) {
        selectedPairId = entry.selectedCandidatePairId;
      }
    });
    const pair = selectedPairId ? report.get(selectedPairId) : null;
    const local = pair ? report.get(pair.localCandidateId) : null;
    const remote = pair ? report.get(pair.remoteCandidateId) : null;
    return local?.candidateType === "relay" || remote?.candidateType === "relay"
      ? "Routed"
      : "Direct";
  }

  channel.addEventListener("open", async () => {
    let route = "Link";
    try {
      route = await selectedRoute();
    } catch (_error) {
      // The tablet remains authoritative for the persisted route result.
    }
    show(`${route} connection established; measuring link…`, "probing");
  });

  channel.addEventListener("message", (event) => {
    if (!(event.data instanceof ArrayBuffer) || event.data.byteLength < 4) {
      return;
    }
    latestSequence = new DataView(event.data).getUint32(0);
    receivedBytes += event.data.byteLength;
    // A JSON acknowledgement for every 16 KiB chunk can overwhelm the
    // browser event loop and the reverse side of the same SCTP association,
    // producing a false-low result. A cumulative acknowledgement every 50 ms
    // preserves receiver-authoritative byte accounting without ACK flooding.
    if (!acknowledgementPending && channel.readyState === "open") {
      acknowledgementPending = true;
      window.setTimeout(sendAcknowledgement, 50);
    }
  });

  async function readStatus() {
    const response = await fetch(statusUrl, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error("The link-test status is unavailable.");
    }
    return response.json();
  }

  async function pollStatus() {
    const deadline = Date.now() + 30000;
    while (!finished && Date.now() < deadline) {
      const current = await readStatus();
      if (!answerApplied && current.answerSdp) {
        await peer.setRemoteDescription({
          type: "answer",
          sdp: current.answerSdp,
        });
        answerApplied = true;
        show("Tablet answered; establishing the encrypted test channel…", "connecting");
      }
      if (current.state === "awaiting_approval") {
        const route =
          current.routeKind === "routed" ? "Routed" : "Direct";
        const mbps =
          Number(current.estimatedUplinkBps || 0) / 1000000;
        show(
          `${route} link measured at ${mbps.toFixed(1)} Mbps usable. ` +
          "Waiting for the pilot or visual observer to choose quality.",
          "complete",
        );
        finished = true;
        peer.close();
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
    if (!finished) {
      throw new Error(
        "The tablet did not complete the link test " +
        `(${browserTransportState()}). ` +
        "The request remains available until it expires.",
      );
    }
  }

  async function start() {
    if (!requestId || !designator || !formToken) {
      throw new Error("The link-test request is incomplete.");
    }
    show("Gathering a routed WebRTC path to the tablet…", "starting");
    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    await waitForRelayCandidate(4000);
    if (!peer.localDescription?.sdp.includes(" typ relay ")) {
      throw new Error("A routed TURN candidate was not available.");
    }
    const response = await fetch(offerUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({
        sdp: peer.localDescription.sdp,
        form_token: formToken,
      }),
    });
    if (!response.ok) {
      throw new Error("The tracker rejected the link-test offer.");
    }
    const result = await response.json();
    show(
      result.delivered
        ? "Link-test offer delivered; waiting for the tablet…"
        : "Link-test offer recorded; waiting for the tablet to reconnect…",
      "waiting",
    );
    await pollStatus();
  }

  start().catch((error) => {
    finished = true;
    peer.close();
    show(error.message || "The link test failed.", "error");
  });
})();
