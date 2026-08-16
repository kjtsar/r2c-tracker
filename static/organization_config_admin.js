(function () {
  "use strict";

  const section = document.getElementById("organization-config");
  if (!section) return;

  document.querySelectorAll(".config-version-time").forEach(function (element) {
    const versionMs = Number(element.dataset.versionMs);
    if (Number.isFinite(versionMs) && versionMs > 0) {
      element.textContent = new Date(versionMs).toLocaleString();
    }
  });

  const state = section.dataset.configProposalState || "none";
  const storageKey = "r2c-organization-config-waiting:" + window.location.pathname;
  if (state === "awaiting_device") {
    window.sessionStorage.setItem(storageKey, "1");
    window.setTimeout(function () {
      window.location.reload();
    }, 2000);
    return;
  }

  const wasWaiting = window.sessionStorage.getItem(storageKey) === "1";
  window.sessionStorage.removeItem(storageKey);
  if (state === "ready" && wasWaiting) {
    const proposal = document.getElementById("organization-config-proposal");
    if (proposal) {
      proposal.scrollIntoView({ behavior: "smooth", block: "start" });
      proposal.focus({ preventScroll: true });
    }
  }
})();
