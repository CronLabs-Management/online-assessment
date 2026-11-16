/*
 * ui.js: shared interface behaviour for every page.
 *  - window.oaConfirm / window.oaAlert: accessible in-app dialog (focus trap, Escape, focus restore)
 *  - data-confirm on forms and data-confirm-click on buttons/links
 *  - header user menu and phone navigation drawer
 *  - dismissible flash messages
 *  - submit buttons show a busy state and cannot be pressed twice
 * Plain script, no dependencies, loaded with defer.
 */
(function () {
  "use strict";

  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function byId(id) { return document.getElementById(id); }
  function afterTransition(el, fn, ms) {
    if (reduceMotion) { fn(); return; }
    var done = false;
    function finish() { if (!done) { done = true; el.removeEventListener("transitionend", finish); fn(); } }
    el.addEventListener("transitionend", finish);
    setTimeout(finish, ms || 300);
  }

  /* ------------------------------------------------------------------ Dialog */
  var modal = byId("oa-universal-modal");
  var ICONS = {
    danger: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v4m0 4h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>',
    warning: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v4m0 4h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>',
    info: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 16v-4m0-4h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z"/>',
    success: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 1 1-18 0 9 9 0 0 1 18 0z"/>',
    signout: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0-4-4m4 4H7m6 4v1a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3h4a3 3 0 0 1 3 3v1"/>'
  };
  var BUTTON_TONE = { danger: "ui-btn-danger", warning: "ui-btn-primary", info: "ui-btn-primary", primary: "ui-btn-primary", success: "ui-btn-primary", signout: "ui-btn-danger" };

  var active = null; // { resolve, returnFocus }

  function openDialog(opts, isAlert) {
    if (!modal) return Promise.resolve(isAlert ? true : window.confirm(opts.message || ""));
    if (active) closeDialog(false);
    var tone = opts.tone || (isAlert ? "info" : "danger");
    var iconKey = opts.icon || tone;
    byId("oa-modal-title").textContent = opts.title || (isAlert ? "Notice" : "Are you sure?");
    byId("oa-modal-desc").textContent = opts.message || "";
    var custom = byId("oa-modal-custom-content");
    if (opts.customHtml) { custom.innerHTML = opts.customHtml; custom.hidden = false; }
    else { custom.innerHTML = ""; custom.hidden = true; }
    var iconWrap = byId("oa-modal-icon-wrap");
    iconWrap.className = "ui-dialog-icon tone-" + (ICONS[iconKey] ? iconKey : "info");
    byId("oa-modal-icon").innerHTML = ICONS[iconKey] || ICONS.info;
    var cancel = byId("oa-modal-cancel");
    var confirm = byId("oa-modal-confirm");
    cancel.hidden = !!isAlert;
    cancel.textContent = opts.cancelText || "Cancel";
    confirm.className = "ui-btn " + (BUTTON_TONE[tone] || "ui-btn-primary");
    confirm.textContent = (isAlert ? opts.btnText : opts.confirmText) || (isAlert ? "OK" : "Confirm");

    return new Promise(function (resolve) {
      active = { resolve: resolve, returnFocus: document.activeElement };
      modal.hidden = false;
      document.documentElement.style.overflow = "hidden";
      // Next frame so the opening transition runs.
      requestAnimationFrame(function () {
        modal.classList.add("is-open");
        (isAlert || tone !== "danger" ? confirm : cancel).focus({ preventScroll: true });
      });
    });
  }

  function closeDialog(result) {
    if (!active) return;
    var current = active;
    active = null;
    modal.classList.remove("is-open");
    afterTransition(modal, function () {
      if (!active) { modal.hidden = true; document.documentElement.style.overflow = ""; }
    }, 260);
    if (current.returnFocus && current.returnFocus.focus) {
      try { current.returnFocus.focus({ preventScroll: true }); } catch (e) { /* element gone */ }
    }
    current.resolve(result);
  }

  window.oaConfirm = function (opts) { return openDialog(opts || {}, false); };
  window.oaAlert = function (opts) {
    if (typeof opts === "string") opts = { message: opts };
    return openDialog(opts || {}, true);
  };
  // Replace native alert() so nothing outside the design system appears.
  window.alert = function (msg) { window.oaAlert({ message: String(msg), title: "Notice", tone: "info" }); };

  if (modal) {
    byId("oa-modal-cancel").addEventListener("click", function () { closeDialog(false); });
    byId("oa-modal-confirm").addEventListener("click", function () { closeDialog(true); });
    byId("oa-modal-close").addEventListener("click", function () { closeDialog(false); });
    modal.addEventListener("mousedown", function (e) { if (e.target === modal) closeDialog(false); });
    modal.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { e.preventDefault(); closeDialog(false); return; }
      if (e.key !== "Tab") return;
      var items = Array.prototype.filter.call(modal.querySelectorAll(FOCUSABLE), function (el) { return el.offsetParent !== null; });
      if (!items.length) return;
      var first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
  }

  function confirmOptions(el, attr) {
    return {
      message: el.getAttribute(attr),
      title: el.getAttribute("data-confirm-title") || "Are you sure?",
      tone: el.getAttribute("data-confirm-tone") || "danger",
      icon: el.getAttribute("data-confirm-icon") || undefined,
      confirmText: el.getAttribute("data-confirm-btn") || "Confirm"
    };
  }

  // Forms with data-confirm ask first. The button that was pressed is passed on through
  // requestSubmit, so its name/value (for example action=approve) still reaches the server.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || !form.hasAttribute || !form.hasAttribute("data-confirm")) return;
    if (form.dataset.oaConfirmed === "true") { delete form.dataset.oaConfirmed; return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    var submitter = e.submitter || null;
    window.oaConfirm(confirmOptions(form, "data-confirm")).then(function (ok) {
      if (!ok) return;
      form.dataset.oaConfirmed = "true";
      if (form.requestSubmit) {
        try { form.requestSubmit(submitter && submitter.form === form ? submitter : undefined); return; } catch (err) { /* fall through */ }
      }
      form.submit();
    });
  }, true);

  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-confirm-click]");
    if (!btn) return;
    if (btn.dataset.oaConfirmed === "true") { delete btn.dataset.oaConfirmed; return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    window.oaConfirm(confirmOptions(btn, "data-confirm-click")).then(function (ok) {
      if (ok) { btn.dataset.oaConfirmed = "true"; btn.click(); }
    });
  }, true);

  /* ------------------------------------------------- Busy submit buttons */
  // Runs after page handlers (bubbling phase), so forms that submit through fetch and call
  // preventDefault are left alone. Disabling waits a tick so the pressed button's value is sent.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || !form || form.hasAttribute("data-no-busy")) return;
    if (form.target && form.target !== "_self") return;
    if (form.dataset.oaSubmitting === "true") { e.preventDefault(); return; }
    form.dataset.oaSubmitting = "true";
    var btn = e.submitter || form.querySelector('button[type="submit"], button:not([type])');
    setTimeout(function () {
      if (btn && !btn.hasAttribute("data-no-busy")) {
        btn.style.minWidth = btn.offsetWidth + "px";
        btn.classList.add("is-busy");
        btn.setAttribute("aria-busy", "true");
      }
    }, 0);
    // File downloads do not navigate away; release the form so it can be used again.
    setTimeout(releaseForm.bind(null, form, btn), 8000);
  });

  function releaseForm(form, btn) {
    delete form.dataset.oaSubmitting;
    if (btn) { btn.classList.remove("is-busy"); btn.removeAttribute("aria-busy"); btn.style.minWidth = ""; }
  }
  // Coming back with the browser's back button restores the page from cache with the busy state;
  // clear it so the form works again.
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("form[data-oa-submitting]").forEach(function (f) { delete f.dataset.oaSubmitting; });
    document.querySelectorAll(".is-busy").forEach(function (b) { b.classList.remove("is-busy"); b.removeAttribute("aria-busy"); b.style.minWidth = ""; });
  });

  /* ------------------------------------------------- Menus and drawer */
  function setupToggle(button, panel, opts) {
    if (!button || !panel) return null;
    function isOpen() { return panel.classList.contains("is-open"); }
    function open() {
      panel.hidden = false;
      requestAnimationFrame(function () { panel.classList.add("is-open"); });
      button.setAttribute("aria-expanded", "true");
      if (opts && opts.onOpen) opts.onOpen();
    }
    function close(restoreFocus) {
      if (!isOpen()) return;
      panel.classList.remove("is-open");
      button.setAttribute("aria-expanded", "false");
      afterTransition(panel, function () { if (!isOpen()) panel.hidden = true; }, 240);
      if (opts && opts.onClose) opts.onClose();
      if (restoreFocus) button.focus();
    }
    button.addEventListener("click", function (e) { e.stopPropagation(); isOpen() ? close() : open(); });
    document.addEventListener("click", function (e) { if (isOpen() && !panel.contains(e.target)) close(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && isOpen()) close(true); });
    return { open: open, close: close };
  }

  setupToggle(byId("user-menu-button"), byId("user-menu"));
  var iconOpen = byId("nav-icon-open"), iconClose = byId("nav-icon-close");
  var drawer = setupToggle(byId("mobile-nav-toggle"), byId("mobile-nav-drawer"), {
    onOpen: function () { if (iconOpen) iconOpen.hidden = true; if (iconClose) iconClose.hidden = false; },
    onClose: function () { if (iconOpen) iconOpen.hidden = false; if (iconClose) iconClose.hidden = true; }
  });
  if (drawer) {
    window.addEventListener("resize", function () { if (window.innerWidth >= 900) drawer.close(); }, { passive: true });
  }

  /* ------------------------------------------------- Flash messages */
  function dismiss(flash) {
    flash.classList.add("is-leaving");
    afterTransition(flash, function () { flash.remove(); }, 260);
  }
  document.addEventListener("click", function (e) {
    var close = e.target.closest && e.target.closest("[data-dismiss-flash]");
    if (close) dismiss(close.closest(".ui-flash"));
  });
  // Success notes fade out on their own; errors and warnings stay until dismissed.
  document.querySelectorAll(".ui-flash[data-autohide]").forEach(function (flash) {
    setTimeout(function () { if (document.body.contains(flash)) dismiss(flash); }, 6000);
  });

  /* ------------------------------------------------- Gliding Morph Indicators */
  function setupMorphGlider(containerSelector, itemSelector, gliderSelector, opts) {
    var containers = document.querySelectorAll(containerSelector);
    if (!containers.length) return;

    containers.forEach(function (container) {
      var glider = container.querySelector(gliderSelector);
      if (!glider) {
        glider = document.createElement("span");
        glider.className = gliderSelector.replace(/^\./, "");
        glider.setAttribute("aria-hidden", "true");
        container.prepend(glider);
      }

      var items = Array.prototype.slice.call(container.querySelectorAll(itemSelector));
      if (!items.length) return;

      var activeItem = container.querySelector(itemSelector + '[aria-current="page"]') || null;
      var currentTarget = activeItem;
      var inset = (opts && opts.inset) || 0;

      function render(target, immediate) {
        if (!target) {
          glider.style.opacity = "0";
          return;
        }
        var left = target.offsetLeft + inset;
        var width = Math.max(0, target.offsetWidth - (inset * 2));

        if (immediate || reduceMotion) {
          glider.style.transition = "none";
          glider.style.transform = "translate3d(" + left + "px, 0, 0)";
          glider.style.width = width + "px";
          glider.style.opacity = "1";
          glider.offsetHeight; // force reflow
          glider.style.transition = "";
        } else {
          glider.style.transform = "translate3d(" + left + "px, 0, 0)";
          glider.style.width = width + "px";
          glider.style.opacity = "1";
        }
      }

      // Initial placement
      render(activeItem, true);

      // Re-align on font load and window load
      if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(function () {
          if (currentTarget) render(currentTarget, true);
        });
      }
      window.addEventListener("load", function () {
        if (currentTarget) render(currentTarget, true);
      });

      // Hover and focus
      items.forEach(function (item) {
        item.addEventListener("mouseenter", function () {
          currentTarget = item;
          render(item, false);
        });
        item.addEventListener("focus", function () {
          currentTarget = item;
          render(item, false);
        });
      });

      // Mouse leave container: glide back to active page
      container.addEventListener("mouseleave", function () {
        currentTarget = activeItem;
        render(activeItem, false);
      });

      // Focus leaves container: return to active page
      container.addEventListener("focusout", function (e) {
        if (!container.contains(e.relatedTarget)) {
          currentTarget = activeItem;
          render(activeItem, false);
        }
      });

      // Resize
      window.addEventListener("resize", function () {
        if (currentTarget) render(currentTarget, true);
      }, { passive: true });
    });
  }

  // Initialize morph gliders for both top navbar and section tabs
  setupMorphGlider(".ui-nav", ".ui-nav-link", ".ui-nav-glider");
  setupMorphGlider(".ui-tabs", ".ui-tab", ".ui-tab-glider", { inset: 8 });
})();
