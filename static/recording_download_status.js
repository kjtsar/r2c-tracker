(function () {
  "use strict";
  const items = Array.from(document.querySelectorAll(".recording-download-status"));
  if (!items.length) return;
  let stopped = false;
  async function poll() {
    if (stopped || document.hidden) return;
    for (const item of items) {
      try {
        const response = await fetch(item.dataset.statusUrl, {
          cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" },
        });
        if (!response.ok) continue;
        const payload = await response.json();
        if (payload.state === "ready") {
          stopped = true;
          // A recording download is one user gesture with an asynchronous
          // tablet-approval/upload phase. Complete that gesture by navigating
          // to the authorized attachment as soon as the upload is ready.
          window.location.assign(item.dataset.downloadUrl);
          return;
        }
        if (["declined", "failed"].includes(payload.state)) {
          stopped = true;
          window.location.reload();
          return;
        }
      } catch (_error) {
        // The next bounded poll repairs transient network failures.
      }
    }
    window.setTimeout(poll, 2000);
  }
  window.addEventListener("pagehide", function () { stopped = true; });
  window.setTimeout(poll, 1000);
})();
