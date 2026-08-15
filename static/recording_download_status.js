(function () {
  "use strict";
  const items = Array.from(document.querySelectorAll(".recording-download-status"));
  if (!items.length) return;
  let stopped = false;
  let lastProgressState = "";

  function showProgress(state, message) {
    if (state === lastProgressState) return;
    lastProgressState = state;
    const flash = Array.from(document.querySelectorAll("[data-flash-message]"))
      .find((item) => item.textContent.includes("Recording transfer"));
    if (!flash) return;
    flash.textContent = message;
    flash.className = "alert alert-success";
  }

  function startDownload(url) {
    const link = document.createElement("a");
    link.href = url;
    link.download = "";
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    link.remove();
    showProgress(
      "download_started",
      "Recording download started. You can continue using this page."
    );
  }

  async function poll() {
    if (stopped || document.hidden) return;
    for (const item of items) {
      try {
        const response = await fetch(item.dataset.statusUrl, {
          cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" },
        });
        if (!response.ok) continue;
        const payload = await response.json();
        if (payload.state === "approved") {
          showProgress(
            payload.state,
            payload.statusMessage ||
              "Recording transfer approved; waiting for the tablet to upload it."
          );
        } else if (payload.state === "uploading") {
          showProgress(
            payload.state,
            payload.statusMessage || "Recording transfer is in progress."
          );
        }
        if (payload.state === "ready") {
          stopped = true;
          // Preserve the tablet-specific streams page while the browser handles
          // the attachment as an independent download.
          startDownload(item.dataset.downloadUrl);
          return;
        }
        if (["declined", "failed", "expired"].includes(payload.state)) {
          stopped = true;
          window.location.reload();
          return;
        }
      } catch (_error) {
        // The next bounded poll repairs transient network failures.
      }
    }
  }
  window.addEventListener("r2c:streams-changed", poll);
  window.addEventListener("pagehide", function () { stopped = true; });
  // Reconcile once after navigation or reconnect. Subsequent checks are driven
  // by the existing organization streams WebSocket rather than periodic polls.
  window.setTimeout(poll, 0);
})();
