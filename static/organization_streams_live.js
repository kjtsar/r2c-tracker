(function () {
  "use strict";

  const state = document.getElementById("streams-live-status");
  if (!state) return;

  const designator = state.dataset.designator || "";
  const deviceId = state.dataset.deviceId || "";
  const streamFilter = state.dataset.streamFilter || "";
  // Preflight and media controllers own their request lifecycle. Reloading an
  // active controller because the tablet's advertised stream set changes can
  // destroy an otherwise healthy WebRTC session (for example, during a brief
  // decoder/UI transition on the tablet).
  const requestControllerActive = Boolean(
    document.getElementById("video-preflight") ||
    document.getElementById("video-media")
  );
  const renderedMembershipRevision = state.dataset.membershipRevision || "";
  let renderedInProgressSessionIds = [];
  try {
    renderedInProgressSessionIds = JSON.parse(
      state.dataset.inProgressSessionIds || "[]"
    ).map(String).sort();
  } catch (_error) {
    renderedInProgressSessionIds = [];
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const query = new URLSearchParams();
  if (deviceId) query.set("device", deviceId);
  if (streamFilter) query.set("stream", streamFilter);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  const eventUrl = `${protocol}//${window.location.host}/${encodeURIComponent(designator)}/streams/events${suffix}`;
  const statusUrl = `${state.dataset.statusUrl || ""}${suffix}`;
  let retryDelayMs = 1000;
  let timer = null;
  let stopped = false;
  let socket = null;
  let refreshPromise = null;
  let refreshQueued = false;
  let windowFocused = document.hasFocus();

  function pageHasFocus() {
    return !document.hidden && windowFocused;
  }

  function suspend() {
    window.clearTimeout(timer);
    timer = null;
    if (socket) {
      const activeSocket = socket;
      socket = null;
      activeSocket.close();
    }
  }

  function stopForNavigation() {
    stopped = true;
    window.clearTimeout(timer);
    if (socket) socket.close();
  }

  document.addEventListener("submit", stopForNavigation, true);

  function reloadForMembershipChange() {
    if (stopped) return;
    stopped = true;
    suspend();
    window.location.reload();
  }

  function previewImage(sessionId) {
    return Array.from(document.querySelectorAll(".stream-preview-image"))
      .find((image) => image.dataset.streamSessionId === sessionId);
  }

  function updatePreview(item) {
    const image = previewImage(item.sessionId);
    if (!image || !item.thumbnailUrl) return;
    if (image.dataset.thumbnailRevision === item.thumbnailRevision) return;

    image.classList.add("is-refreshing");
    const replacement = new Image();
    replacement.onload = function () {
      image.src = item.thumbnailUrl;
      image.dataset.thumbnailRevision = item.thumbnailRevision;
      image.hidden = false;
      const previewCell = image.closest(".stream-preview-cell");
      const pending = previewCell &&
        previewCell.querySelector(".stream-preview-pending");
      const label = previewCell &&
        previewCell.querySelector(".stream-preview-label");
      if (pending) pending.hidden = true;
      if (label) label.hidden = false;
      image.classList.remove("is-refreshing");
    };
    replacement.onerror = function () {
      image.classList.remove("is-refreshing");
    };
    replacement.src = item.thumbnailUrl;
  }

  async function fetchAndReconcile() {
    if (stopped || !statusUrl) return;
    const response = await fetch(statusUrl, {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`Stream status ${response.status}`);
    const status = await response.json();
    if (!requestControllerActive &&
        status.membershipRevision !== renderedMembershipRevision) {
      reloadForMembershipChange();
      return;
    }
    const currentInProgressSessionIds = (status.inProgressSessionIds || [])
      .map(String)
      .sort();
    if (!requestControllerActive &&
        JSON.stringify(currentInProgressSessionIds) !==
        JSON.stringify(renderedInProgressSessionIds)) {
      reloadForMembershipChange();
      return;
    }
    (status.streams || []).forEach(updatePreview);
  }

  function reconcile() {
    if (refreshPromise) {
      refreshQueued = true;
      return refreshPromise;
    }
    refreshPromise = fetchAndReconcile()
      .catch(function () {
        // The socket's reconnect/backoff will provide the next reconciliation.
      })
      .finally(function () {
        refreshPromise = null;
        if (refreshQueued && !stopped) {
          refreshQueued = false;
          reconcile();
        }
      });
    return refreshPromise;
  }

  function scheduleReconnect(delayMs) {
    if (stopped || !pageHasFocus()) return;
    window.clearTimeout(timer);
    timer = window.setTimeout(connect, delayMs);
  }

  function connect() {
    if (stopped || !pageHasFocus()) return;
    const connectedSocket = new WebSocket(eventUrl);
    socket = connectedSocket;
    connectedSocket.onopen = function () {
      retryDelayMs = 1000;
    };
    connectedSocket.onmessage = function (event) {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (_error) {
        return;
      }
      if (message.type === "ready" || message.type === "streams_changed") {
        window.dispatchEvent(new CustomEvent("r2c:streams-changed", {
          detail: message,
        }));
        reconcile();
      }
    };
    connectedSocket.onclose = function () {
      if (socket !== connectedSocket) return;
      socket = null;
      if (!stopped && pageHasFocus()) {
        scheduleReconnect(retryDelayMs);
        retryDelayMs = Math.min(retryDelayMs * 2, 30000);
      }
    };
    connectedSocket.onerror = function () {
      connectedSocket.close();
    };
  }

  function syncPageActivity() {
    if (!pageHasFocus()) {
      suspend();
      return;
    }
    if (!stopped && !socket) scheduleReconnect(0);
  }

  function handleFocus() {
    windowFocused = true;
    syncPageActivity();
  }

  function handleBlur() {
    windowFocused = false;
    syncPageActivity();
  }

  document.addEventListener("visibilitychange", syncPageActivity);
  window.addEventListener("focus", handleFocus);
  window.addEventListener("blur", handleBlur);
  window.addEventListener("pageshow", syncPageActivity);
  connect();
})();
