(function () {
  "use strict";

  const picker = document.getElementById("organization-picker");
  const opener = document.getElementById("open-organization-picker");
  if (!picker || !opener) return;

  const showPicker = function () {
    if (picker.open) return;
    if (typeof picker.showModal === "function") picker.showModal();
    else picker.setAttribute("open", "");
  };

  opener.addEventListener("click", showPicker);
  showPicker();
})();
