// Generic "..." dropdown menus (see .menu / .menu-trigger / .menu-panel in app.css).
// Listeners live on document so they survive htmx swapping the menu markup itself.
(function () {
  function closeAll(except) {
    document.querySelectorAll(".menu.open").forEach((m) => {
      if (m === except) return;
      m.classList.remove("open");
      m.querySelector(".menu-panel")?.setAttribute("hidden", "");
      m.querySelector(".menu-trigger")?.setAttribute("aria-expanded", "false");
    });
  }

  document.addEventListener("click", (e) => {
    const trigger = e.target.closest(".menu-trigger");
    if (trigger) {
      const menu = trigger.closest(".menu");
      const wasOpen = menu.classList.contains("open");
      closeAll();
      if (!wasOpen) {
        menu.classList.add("open");
        menu.querySelector(".menu-panel")?.removeAttribute("hidden");
        trigger.setAttribute("aria-expanded", "true");
      }
      e.stopPropagation();
      return;
    }
    if (e.target.closest(".menu-panel")) {
      // let the item's own action (hx-post, href, onclick) run, then close
      closeAll();
      return;
    }
    closeAll();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeAll();
  });
})();
