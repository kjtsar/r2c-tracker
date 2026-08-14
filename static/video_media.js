(function () {
  "use strict";

  const state = document.getElementById("video-media");
  const status = document.getElementById("video-media-status");
  const video = document.getElementById("organization-video-player");
  const audio = document.getElementById("organization-audio-player");
  const voipToggle = document.getElementById("voip-toggle");
  const voipStatus = document.getElementById("voip-status");
  if (!state || !status || !video || !audio || !voipToggle || !voipStatus ||
      !window.RTCPeerConnection) return;

  const requestId = state.dataset.requestId || "";
  const designator = state.dataset.designator || "";
  const formToken = state.dataset.formToken || "";
  let iceServers = [];
  try { iceServers = JSON.parse(state.dataset.iceServers || "[]"); } catch (_error) {}

  const base = `/${encodeURIComponent(designator)}/streams/requests/${encodeURIComponent(requestId)}/media`;
  // Preflight measures the routed path. Keep the authorized media session on
  // that same known-good path instead of starting a second unrestricted ICE
  // negotiation that may select an unusable host or server-reflexive pair.
  const peer = new RTCPeerConnection({
    iceServers: iceServers,
    iceTransportPolicy: "relay",
  });
  let answerApplied = false;
  let startedReported = false;
  let startedReportInFlight = false;
  let endedReported = false;
  let statsTimer = null;
  let serverStateTimer = null;
  let lastDecodedFrames = 0;
  let lastDecodeProgressAt = 0;
  let lastVideoBytes = 0;
  let lastPacketProgressAt = 0;
  let pageLeaving = false;
  let audioTransceiver = null;
  let localAudioStream = null;
  let microphoneEnabled = false;
  let audioBytesSent = 0;
  let audioBytesReceived = 0;
  let videoBytesReceived = 0;
  let lastMetricsReportAt = 0;
  let trackAttachedAt = 0;
  let videoTrackState = "";
  let videoPacketsReceived = 0;
  let videoFramesReceived = 0;
  let videoFramesDecoded = 0;
  let firstFrameShown = false;
  let videoFramesPresented = 0;
  let videoFramesDropped = 0;
  let videoKeyFramesDecoded = 0;
  let videoCodec = "";
  let decoderImplementation = "";
  let terminalReloadTimer = null;
  const metricsSessionId = window.crypto && typeof window.crypto.randomUUID === "function"
    ? window.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;

  function formatBytes(value) {
    if (value >= 1000000) return `${(value / 1000000).toFixed(1)} MB`;
    if (value >= 1000) return `${(value / 1000).toFixed(1)} KB`;
    return `${value} B`;
  }

  function renderAudioCounters(prefix) {
    voipStatus.textContent = `${prefix} • Audio: ${formatBytes(audioBytesSent)} sent, ` +
      `${formatBytes(audioBytesReceived)} received • ` +
      `Video: ${formatBytes(videoBytesReceived)} received`;
  }

  function renderMicrophoneState(enabled, detail) {
    microphoneEnabled = enabled;
    voipToggle.classList.toggle("is-live", enabled);
    voipToggle.setAttribute("aria-pressed", enabled ? "true" : "false");
    voipToggle.setAttribute("aria-label", enabled ? "Turn microphone off" : "Turn microphone on");
    voipToggle.title = detail || (enabled ? "Microphone live" : "Microphone off");
    renderAudioCounters(detail || (enabled ? "Microphone live" : "Microphone off"));
  }

  async function setMicrophoneEnabled(enabled) {
    if (!audioTransceiver) return;
    voipToggle.disabled = true;
    try {
      if (enabled) {
        // Start incoming playback while this click still carries browser user
        // activation. Waiting for getUserMedia first can consume that gesture,
        // leaving the Android audio track connected but inaudible.
        audio.muted = false;
        audio.volume = 1;
        const incomingPlayback = audio.play().catch(function () { return false; });
        localAudioStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
          video: false,
        });
        await audioTransceiver.sender.replaceTrack(localAudioStream.getAudioTracks()[0]);
        await incomingPlayback;
        renderMicrophoneState(true);
      } else {
        await audioTransceiver.sender.replaceTrack(null);
        if (localAudioStream) {
          localAudioStream.getTracks().forEach(function (track) { track.stop(); });
          localAudioStream = null;
        }
        renderMicrophoneState(false);
      }
    } catch (_error) {
      renderMicrophoneState(false, "Microphone permission or audio capture is unavailable");
    } finally {
      voipToggle.disabled = false;
    }
  }

  function show(message, kind) {
    status.textContent = message;
    state.dataset.state = kind;
  }

  function reloadAfterTerminal(delayMs = 250) {
    if (pageLeaving || terminalReloadTimer) return;
    terminalReloadTimer = window.setTimeout(function () {
      pageLeaving = true;
      window.location.reload();
    }, delayMs);
  }

  function waitForRelayCandidate(timeoutMs) {
    if (peer.localDescription?.sdp.includes(" typ relay ")) return Promise.resolve();
    return new Promise(function (resolve) {
      const timeout = window.setTimeout(resolve, timeoutMs);
      peer.addEventListener("icecandidate", function (event) {
        if (!event.candidate || event.candidate.candidate.includes(" typ relay ")) {
          window.clearTimeout(timeout);
          window.setTimeout(resolve, 0);
        }
      });
    });
  }

  async function reportStarted() {
    if (startedReported || startedReportInFlight) return;
    startedReportInFlight = true;
    try {
      const response = await fetch(`${base}/started`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ form_token: formToken }),
      });
      if (!response.ok) throw new Error("Unable to record video start.");
      startedReported = true;
    } finally {
      startedReportInFlight = false;
    }
  }

  function metricsPayload(diagnosticEvent, diagnosticDetail) {
    if (typeof video.getVideoPlaybackQuality === "function") {
      videoFramesPresented = Number(
        video.getVideoPlaybackQuality().totalVideoFrames || 0
      );
    }
    return {
      form_token: formToken,
      metrics_session_id: metricsSessionId,
      audio_bytes_sent: Math.max(0, Math.trunc(audioBytesSent)),
      audio_bytes_received: Math.max(0, Math.trunc(audioBytesReceived)),
      video_bytes_received: Math.max(0, Math.trunc(videoBytesReceived)),
      diagnostic_event: String(diagnosticEvent || "sample").slice(0, 64),
      diagnostic_detail: String(diagnosticDetail || "").slice(0, 400),
      peer_connection_state: peer.connectionState || "",
      ice_connection_state: peer.iceConnectionState || "",
      ice_gathering_state: peer.iceGatheringState || "",
      signaling_state: peer.signalingState || "",
      video_track_state: videoTrackState,
      video_element_ready_state: Number(video.readyState || 0),
      video_element_paused: Boolean(video.paused),
      video_element_width: Number(video.videoWidth || 0),
      video_element_height: Number(video.videoHeight || 0),
      video_packets_received: Math.max(0, Math.trunc(videoPacketsReceived)),
      video_frames_received: Math.max(0, Math.trunc(videoFramesReceived)),
      video_frames_decoded: Math.max(0, Math.trunc(videoFramesDecoded)),
      video_frames_presented: Math.max(0, Math.trunc(videoFramesPresented)),
      video_frames_dropped: Math.max(0, Math.trunc(videoFramesDropped)),
      video_key_frames_decoded: Math.max(0, Math.trunc(videoKeyFramesDecoded)),
      video_codec: videoCodec.slice(0, 120),
      decoder_implementation: decoderImplementation.slice(0, 120),
    };
  }

  async function reportMetrics(force, keepalive, diagnosticEvent, diagnosticDetail) {
    const now = Date.now();
    if (!force && now - lastMetricsReportAt < 5000) return;
    lastMetricsReportAt = now;
    await fetch(`${base}/metrics`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(metricsPayload(diagnosticEvent, diagnosticDetail)),
      keepalive: Boolean(keepalive),
    });
  }

  function reportDiagnostic(event, detail) {
    reportMetrics(true, false, event, detail).catch(function () {});
  }

  async function reportEnded(message) {
    if (endedReported) return;
    await reportMetrics(true, false).catch(function () {});
    endedReported = true;
    window.clearInterval(statsTimer);
    statsTimer = null;
    window.clearInterval(serverStateTimer);
    serverStateTimer = null;
    if (video.srcObject) {
      video.srcObject.getTracks().forEach(function (track) { track.stop(); });
    }
    if (localAudioStream) {
      localAudioStream.getTracks().forEach(function (track) { track.stop(); });
      localAudioStream = null;
    }
    video.srcObject = null;
    audio.srcObject = null;
    video.style.display = "none";
    show(message, "ended");
    peer.close();
    await fetch(`${base}/ended`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ form_token: formToken, reason: message }),
    }).catch(function () {});
    reloadAfterTerminal(1500);
  }

  async function endFromServer(message) {
    if (endedReported) return;
    await reportMetrics(true, false).catch(function () {});
    endedReported = true;
    window.clearInterval(statsTimer);
    statsTimer = null;
    window.clearInterval(serverStateTimer);
    serverStateTimer = null;
    if (video.srcObject) {
      video.srcObject.getTracks().forEach(function (track) { track.stop(); });
    }
    if (localAudioStream) {
      localAudioStream.getTracks().forEach(function (track) { track.stop(); });
      localAudioStream = null;
    }
    video.srcObject = null;
    audio.srcObject = null;
    video.style.display = "none";
    show(message, "ended");
    peer.close();
    reloadAfterTerminal();
  }

  function terminalStatusMessage(current) {
    if (!["redirected", "declined", "stopped", "expired", "e_nosuch_stream"].includes(
      current.state || ""
    )) return "";
    return current.statusMessage || "Video stream stopped.";
  }

  async function inspectServerState() {
    if (endedReported) return;
    const response = await fetch(`${base}/status`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) return;
    const current = await response.json();
    const message = terminalStatusMessage(current);
    if (message) await endFromServer(message);
  }

  async function inspectFrameProgress() {
    if (endedReported) return;
    if (document.hidden) {
      lastDecodeProgressAt = Date.now();
      lastPacketProgressAt = Date.now();
      return;
    }
    const reports = await peer.getStats();
    let decodedFrames = 0;
    let inspectedVideoBytesReceived = 0;
    let inspectedCodecId = "";
    reports.forEach(function (report) {
      if (report.type === "inbound-rtp" && !report.isRemote &&
          (report.kind === "video" || report.mediaType === "video")) {
        decodedFrames += Number(report.framesDecoded || 0);
        inspectedVideoBytesReceived += Number(report.bytesReceived || 0);
        videoPacketsReceived = Number(report.packetsReceived || 0);
        videoFramesReceived = Number(report.framesReceived || 0);
        videoFramesDropped = Number(report.framesDropped || 0);
        videoKeyFramesDecoded = Number(report.keyFramesDecoded || 0);
        inspectedCodecId = String(report.codecId || "");
        decoderImplementation = String(report.decoderImplementation || "");
      }
      if (report.type === "outbound-rtp" && !report.isRemote &&
          (report.kind === "audio" || report.mediaType === "audio")) {
        audioBytesSent = Number(report.bytesSent || 0);
      }
      if (report.type === "inbound-rtp" && !report.isRemote &&
          (report.kind === "audio" || report.mediaType === "audio")) {
        audioBytesReceived = Number(report.bytesReceived || 0);
      }
    });
    if (inspectedCodecId) {
      const codec = reports.get(inspectedCodecId);
      videoCodec = String(codec?.mimeType || codec?.codec || "");
    }
    videoFramesDecoded = decodedFrames;
    videoBytesReceived = inspectedVideoBytesReceived;
    await reportMetrics(false, false).catch(function () {});
    const now = Date.now();
    if (!firstFrameShown && decodedFrames > 0) {
      firstFrameShown = true;
      show("Video is playing.", "playing");
      reportDiagnostic("video_first_frame", `${video.videoWidth}x${video.videoHeight}`);
    }
    if (!startedReported && (videoBytesReceived > 0 || decodedFrames > 0)) {
      await reportStarted().catch(function () {});
    }
    if (videoBytesReceived > lastVideoBytes) {
      lastVideoBytes = videoBytesReceived;
      lastPacketProgressAt = now;
    }
    if (decodedFrames > lastDecodedFrames) {
      lastDecodedFrames = decodedFrames;
      lastDecodeProgressAt = now;
    } else if (lastDecodeProgressAt && now - lastDecodeProgressAt >= 6000 &&
               lastPacketProgressAt && now - lastPacketProgressAt < 15000) {
      show("Video packets are arriving; waiting for decoder recovery…", "connecting");
    }
    if (lastPacketProgressAt && now - lastPacketProgressAt >= 15000) {
      await reportEnded("Video source stopped; the frozen last frame was cleared.");
    } else if (!lastPacketProgressAt && trackAttachedAt &&
               now - trackAttachedAt >= 15000) {
      await reportEnded("No video packets arrived; the media connection was closed.");
    }
    renderAudioCounters(microphoneEnabled ? "Microphone live" : "Microphone off");
  }

  peer.addEventListener("track", function (event) {
    const stream = event.streams && event.streams[0]
      ? event.streams[0]
      : new MediaStream([event.track]);
    if (event.track.kind === "audio") {
      event.track.enabled = true;
      audio.srcObject = stream;
      audio.muted = false;
      audio.volume = 1;
      audio.play().then(function () {
        renderAudioCounters(microphoneEnabled ? "Microphone live" : "Listening");
      }).catch(function () {
        voipStatus.textContent = "Incoming audio is ready; tap the microphone once to allow playback.";
      });
      return;
    }
    video.srcObject = stream;
    video.style.display = "block";
    trackAttachedAt = Date.now();
    videoTrackState = event.track.readyState || "live";
    reportDiagnostic("video_track_attached", `muted=${Boolean(event.track.muted)}`);
    // A receiver can get a negotiated track before any RTP arrives. Start the
    // packet deadline here; waiting for HTMLMediaElement.playing would leave a
    // zero-packet source open forever because that event can never fire.
    if (!statsTimer) {
      statsTimer = window.setInterval(function () {
        inspectFrameProgress().catch(function () {});
      }, 1500);
    }
    inspectFrameProgress().catch(function () {});
    event.track.addEventListener("mute", function () {
      reportDiagnostic("video_track_muted", "");
    });
    event.track.addEventListener("unmute", function () {
      reportDiagnostic("video_track_unmuted", "");
    });
    event.track.addEventListener("ended", function () {
      videoTrackState = event.track.readyState || "ended";
      reportDiagnostic("video_track_ended", "");
      if (!pageLeaving) {
        reportEnded("Video source stopped; the last frame was cleared.").catch(function () {});
      }
    });
    video.play().then(function () {
      reportDiagnostic("video_play_resolved", "");
    }).catch(function (error) {
      reportDiagnostic("video_play_rejected", error?.message || String(error || ""));
      show("Video arrived. Select Play if browser autoplay is disabled.", "playing");
    });
  });
  video.addEventListener("loadedmetadata", function () {
    reportDiagnostic("video_loadedmetadata", `${video.videoWidth}x${video.videoHeight}`);
  });
  video.addEventListener("playing", function () {
    reportDiagnostic("video_playing", `${video.videoWidth}x${video.videoHeight}`);
    show("Video track attached; waiting for the first video frame…", "connecting");
  });
  video.addEventListener("waiting", function () {
    reportDiagnostic("video_waiting", "");
  });
  video.addEventListener("stalled", function () {
    reportDiagnostic("video_stalled", "");
  });
  video.addEventListener("error", function () {
    const mediaError = video.error;
    reportDiagnostic(
      "video_element_error",
      mediaError ? `code=${mediaError.code} message=${mediaError.message || ""}` : "unknown",
    );
  });
  peer.addEventListener("iceconnectionstatechange", function () {
    reportDiagnostic("ice_connection_state", peer.iceConnectionState || "");
  });
  peer.addEventListener("icegatheringstatechange", function () {
    reportDiagnostic("ice_gathering_state", peer.iceGatheringState || "");
  });
  peer.addEventListener("signalingstatechange", function () {
    reportDiagnostic("signaling_state", peer.signalingState || "");
  });
  peer.addEventListener("connectionstatechange", function () {
    reportDiagnostic("peer_connection_state", peer.connectionState || "");
    if (!pageLeaving && !endedReported &&
        (peer.connectionState === "failed" || peer.connectionState === "closed")) {
      reportEnded(`Video connection ${peer.connectionState}; the last frame was cleared.`).catch(function () {});
    }
  });

  async function pollAnswer() {
    const deadline = Date.now() + 20000;
    while (!answerApplied && Date.now() < deadline) {
      const response = await fetch(`${base}/status`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      const current = await response.json().catch(function () { return null; });
      if (!response.ok || !current) {
        const detail = current && typeof current.detail === "string"
          ? current.detail
          : "Media signaling status is unavailable.";
        throw new Error(detail);
      }
      const terminalMessage = terminalStatusMessage(current);
      if (terminalMessage) {
        await endFromServer(terminalMessage);
        return;
      }
      if (current.answerSdp) {
        await peer.setRemoteDescription({ type: "answer", sdp: current.answerSdp });
        answerApplied = true;
        reportDiagnostic("media_answer_applied", "");
        show("Media path negotiated; waiting for the first video frame…", "connecting");
        if (!serverStateTimer) {
          serverStateTimer = window.setInterval(function () {
            inspectServerState().catch(function () {});
          }, 1000);
        }
        return;
      }
      await new Promise(function (resolve) { window.setTimeout(resolve, 300); });
    }
    throw new Error("The tablet did not answer the media offer within 20 seconds.");
  }

  async function start() {
    if (!requestId || !designator || !formToken) throw new Error("Media request is incomplete.");
    reportDiagnostic("media_page_started", "");
    show("Creating the authorized video receiver…", "starting");
    peer.addTransceiver("video", { direction: "recvonly" });
    audioTransceiver = peer.addTransceiver("audio", { direction: "sendrecv" });
    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    const gatheringStartedAt = performance.now();
    await waitForRelayCandidate(4000);
    const relayCandidateMs = Math.max(
      0,
      Math.round(performance.now() - gatheringStartedAt),
    );
    if (!peer.localDescription?.sdp.includes(" typ relay ")) {
      reportDiagnostic("media_relay_candidate_missing", `waitedMs=${relayCandidateMs}`);
      throw new Error("A routed TURN candidate was not available for video.");
    }
    reportDiagnostic("media_relay_candidate_ready", `waitedMs=${relayCandidateMs}`);
    const response = await fetch(`${base}/offer`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        sdp: peer.localDescription.sdp,
        form_token: formToken,
        relay_candidate_ms: relayCandidateMs,
      }),
    });
    if (!response.ok) throw new Error("The tracker rejected the media offer.");
    const result = await response.json();
    reportDiagnostic("media_offer_recorded", `delivered=${Boolean(result.delivered)}`);
    show(
      result.delivered
        ? "Media offer delivered to the tablet…"
        : "Media offer recorded; waiting for the tablet to reconnect…",
      "waiting"
    );
    await pollAnswer();
    voipToggle.style.display = "block";
    renderMicrophoneState(false);
  }

  voipToggle.addEventListener("click", function () {
    setMicrophoneEnabled(!microphoneEnabled).catch(function () {});
  });

  window.addEventListener("pagehide", function () {
    pageLeaving = true;
    window.clearTimeout(terminalReloadTimer);
    window.clearInterval(statsTimer);
    window.clearInterval(serverStateTimer);
    reportMetrics(true, true).catch(function () {});
    if (localAudioStream) {
      localAudioStream.getTracks().forEach(function (track) { track.stop(); });
    }
    peer.close();
  });
  start().catch(function (error) {
    reportDiagnostic("media_start_failed", error?.message || String(error || ""));
    show(error.message || "The video connection failed.", "error");
    peer.close();
  });
})();
