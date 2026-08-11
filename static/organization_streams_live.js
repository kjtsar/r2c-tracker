(function () {
  "use strict";

  const state = document.getElementById("streams-live-status");
  if (!state) return;

  const designator = state.dataset.designator || "";
  const renderedActive = state.dataset.active === "true";
  const renderedRevision = state.dataset.revision || "";
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const eventUrl = `${protocol}//${window.location.host}/${encodeURIComponent(designator)}/streams/events`;
  let retryDelayMs = 1000;
  let timer = null;
  let stopped = false;
  let socket = null;
  let pendingReload = false;
  let pendingReloadTimer = null;
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
    window.clearTimeout(pendingReloadTimer);
    if (socket) socket.close();
  }

  // A lifecycle event can arrive while a request/cancel form is navigating.
  // Let that navigation (and its preflight query parameter) win instead of
  // reloading the old URL from the WebSocket callback.
  document.addEventListener("submit", stopForNavigation, true);

  function preflightIsBusy() {
    const preflight = document.getElementById("video-preflight");
    if (!preflight) return false;
    return !["complete", "error"].includes(preflight.dataset.state || "");
  }

  function preflightOwnsLifecycle() {
    // The preflight controller already polls the request and performs the one
    // intentional navigation after the pilot/VO decision.  Reloading this
    // page for intermediate request notifications causes the stable session
    // snapshot (and the rest of the page) to flash repeatedly.
    return Boolean(document.getElementById("video-preflight"));
  }

  function mediaIsBusy() {
    const media = document.getElementById("video-media");
    if (!media) return false;
    return !["error", "ended"].includes(media.dataset.state || "");
  }

  function reloadWhenIdle() {
    if (stopped) return;
    if (preflightIsBusy() || mediaIsBusy()) {
      pendingReload = true;
      window.clearTimeout(pendingReloadTimer);
      pendingReloadTimer = window.setTimeout(reloadWhenIdle, 1000);
      return;
    }
    pendingReload = false;
    stopped = true;
    window.location.reload();
  }

  function readyStateDiffers(message) {
    if (message.revision && renderedRevision) {
      return message.revision !== renderedRevision;
    }
    return typeof message.active === "boolean" && message.active !== renderedActive;
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
      if (message.type === "ready" && readyStateDiffers(message)) {
        if (preflightOwnsLifecycle()) return;
        reloadWhenIdle();
      } else if (message.type === "streams_changed") {
        if (preflightOwnsLifecycle()) return;
        // Do not tear down an in-flight preflight or media connection. Keep
        // the refresh pending and apply it as soon as that operation reaches
        // a terminal UI state.
        reloadWhenIdle();
      }
    };
    connectedSocket.onclose = function () {
      // A delayed close from a socket suspended during blur must not clear a
      // newer socket opened after focus returned.
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
    if (pendingReload) {
      reloadWhenIdle();
      return;
    }
    if (!stopped && !socket) scheduleReconnect(0);
  }

  function handleFocus() {
    // Some browsers dispatch focus before document.hasFocus() changes. Keep
    // explicit window state so returning to the page always reconnects.
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
