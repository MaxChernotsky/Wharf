// Shared "open tool in an embedded panel" modal, used by any "Open here"
// button across the app (dashboard cards, tool detail page, ...).
(function () {
  let backdrop, panel, iframeEl, titleEl, popLink, closeBtn;

  const ICON_EXTERNAL_LINK = '<svg class="i" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14 21 3"/></svg>';
  const ICON_X = '<svg class="i" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>';

  function build() {
    backdrop = document.createElement("div");
    backdrop.className = "frame-modal-backdrop";
    backdrop.innerHTML =
      '<div class="frame-modal" role="dialog" aria-modal="true">' +
        '<div class="frame-modal-head">' +
          '<span class="frame-modal-title"></span>' +
          '<div class="frame-modal-actions">' +
            '<a class="icon-btn frame-modal-icon frame-modal-pop" target="_blank" rel="noopener" title="Open in a new tab" aria-label="Open in a new tab">' + ICON_EXTERNAL_LINK + '</a>' +
            '<button type="button" class="icon-btn frame-modal-icon frame-modal-close" title="Close" aria-label="Close">' + ICON_X + '</button>' +
          '</div>' +
        '</div>' +
        '<iframe class="frame-modal-iframe"></iframe>' +
      '</div>';
    document.body.appendChild(backdrop);
    panel = backdrop.querySelector(".frame-modal");
    iframeEl = backdrop.querySelector(".frame-modal-iframe");
    titleEl = backdrop.querySelector(".frame-modal-title");
    popLink = backdrop.querySelector(".frame-modal-pop");
    closeBtn = backdrop.querySelector(".frame-modal-close");

    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) closeFrame(); });
    closeBtn.addEventListener("click", closeFrame);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && backdrop.classList.contains("open")) closeFrame();
    });
  }

  function openFrame(url, title) {
    if (!backdrop) build();
    titleEl.textContent = title || "";
    popLink.href = url;
    iframeEl.src = url;
    backdrop.classList.add("open");
    document.body.classList.add("frame-modal-active");
  }

  function closeFrame() {
    if (!backdrop || !backdrop.classList.contains("open")) return;
    backdrop.classList.remove("open");
    document.body.classList.remove("frame-modal-active");
    iframeEl.src = "about:blank";
  }

  // Event delegation so this keeps working after htmx swaps tool cards in/out.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-frame-url]");
    if (!btn) return;
    e.preventDefault();
    openFrame(btn.getAttribute("data-frame-url"), btn.getAttribute("data-frame-title"));
  });

  window.wharfOpenFrame = openFrame;
  window.wharfCloseFrame = closeFrame;
})();
