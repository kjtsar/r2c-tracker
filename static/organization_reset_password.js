(() => {
  const tokenField = document.getElementById("organization-reset-token");
  if (!tokenField || tokenField.value) {
    return;
  }
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  tokenField.value = fragment.get("token") || "";
  if (tokenField.value) {
    history.replaceState(null, "", window.location.pathname);
  }
})();
