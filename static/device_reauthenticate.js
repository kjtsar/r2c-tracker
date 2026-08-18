(function () {
  "use strict";

  const target = document.querySelector("[data-r2c-reauth-complete]");
  if (!target) return;

  const callback = target.getAttribute("data-r2c-reauth-complete");
  if (callback) window.location.replace(callback);
}());
