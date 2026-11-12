// Small page behaviours. No libraries; every page works without this file,
// it only adds the live previews and a few comforts.
(function () {
  "use strict";

  // ------------------------------------------------------------ live preview
  // Mirrors joltbot/core/templating.py: only {simple_names} are replaced, unknown
  // ones are left as typed, and {guild} is an old name for {server}. Keep the two
  // in sync if you change one.
  const ALIASES = { guild: "server" };
  const SAMPLE_USER = "@NewMember";
  const SAMPLE_CHANNEL = "#ticket-newmember";

  function sampleValues(serverName) {
    return {
      user: SAMPLE_USER,
      user_name: "NewMember",
      server: serverName || "Your Server",
      channel: SAMPLE_CHANNEL,
      reason: "Example reason typed by staff",
      role: "Example Role",
      role_line: "Role: **Example Role**\n",
    };
  }

  function render(template, values) {
    return template.replace(/\{([a-z_]+)\}/g, function (match, name) {
      name = ALIASES[name] || name;
      return Object.prototype.hasOwnProperty.call(values, name) ? values[name] : match;
    });
  }

  // Escape first, so nothing typed into the box can become real HTML on the page.
  function escapeHtml(text) {
    return text.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Just enough Discord markdown for a preview: **bold**, *italic*, `code`,
  // and the sample user and channel shown as Discord shows mentions.
  function markdown(html) {
    return html
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\*(.+?)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .split(SAMPLE_USER)
      .join('<span class="dc-mention">' + SAMPLE_USER + "</span>")
      .split(SAMPLE_CHANNEL)
      .join('<span class="dc-mention">' + SAMPLE_CHANNEL + "</span>");
  }

  // A field with data-preview="id" (or several ids, space separated) shows its
  // text in those elements as you type. data-preview-plain skips the markdown,
  // for places where Discord shows text as typed (buttons, form titles).
  function setUpPreview() {
    const form = document.querySelector("form[data-server-name]");
    const values = sampleValues(form ? form.dataset.serverName : "");
    document.querySelectorAll("[data-preview]").forEach(function (field) {
      const targets = field.dataset.preview
        .split(/\s+/)
        .map(function (id) {
          return document.getElementById(id);
        })
        .filter(Boolean);
      if (!targets.length) return;
      const plain = field.hasAttribute("data-preview-plain");
      const update = function () {
        const html = escapeHtml(render(field.value, values));
        targets.forEach(function (target) {
          target.innerHTML = plain ? html : markdown(html);
        });
      };
      field.addEventListener("input", update);
      update();
    });
  }

  // Placeholder buttons: <button data-insert="{user}" data-insert-into="field id">
  // puts the placeholder where the cursor is.
  function setUpInsertButtons() {
    document.querySelectorAll("[data-insert]").forEach(function (button) {
      const field = document.getElementById(button.dataset.insertInto);
      if (!field) return;
      button.addEventListener("click", function () {
        const start = field.selectionStart == null ? field.value.length : field.selectionStart;
        const end = field.selectionEnd == null ? field.value.length : field.selectionEnd;
        field.setRangeText(button.dataset.insert, start, end, "end");
        field.focus();
        field.dispatchEvent(new Event("input", { bubbles: true }));
      });
    });
  }

  // ---------------------------------------------------------------- save bar
  // A form with data-savebar shows "Unsaved changes" once something is edited,
  // and "Saving…" on its button while the page reloads.
  function setUpSaveBars() {
    document.querySelectorAll("form[data-savebar]").forEach(function (form) {
      const bar = form.querySelector(".jb-savebar");
      const note = bar && bar.querySelector("[data-savebar-note]");
      const button = form.querySelector("[data-save-button]");

      function markDirty() {
        if (!bar || bar.dataset.dirty === "true") return;
        bar.dataset.dirty = "true";
        if (note) note.textContent = "You have unsaved changes.";
      }

      form.addEventListener("input", markDirty);
      form.addEventListener("change", markDirty);
      form.addEventListener("submit", function () {
        if (!button) return;
        button.dataset.label = button.textContent;
        button.textContent = "Saving…";
        button.disabled = true;
      });
      // Coming back with the browser's Back button: make the button usable again.
      window.addEventListener("pageshow", function () {
        if (button && button.disabled) {
          button.disabled = false;
          button.textContent = button.dataset.label || "Save changes";
        }
      });
    });
  }

  // Shows the chosen colour's code next to a colour picker (data-hex-target),
  // and on the sample message beside it (data-color-target).
  function setUpColorInputs() {
    document.querySelectorAll("input[type=color][data-hex-target]").forEach(function (input) {
      const target = document.getElementById(input.dataset.hexTarget);
      const sample = document.getElementById(input.dataset.colorTarget || "");
      if (!target) return;
      const update = function () {
        target.textContent = input.value.toUpperCase();
        if (sample) sample.style.setProperty("--dc-bar", input.value);
      };
      input.addEventListener("input", update);
      update();
    });
  }

  // ------------------------------------------------------------------- menus
  // Dropdowns are <details data-menu>: they open without scripts. This closes
  // them on Escape and on a click anywhere else.
  function setUpMenus() {
    const menus = Array.prototype.slice.call(document.querySelectorAll("details[data-menu]"));
    if (!menus.length) return;

    document.addEventListener("click", function (event) {
      menus.forEach(function (menu) {
        if (menu.open && !menu.contains(event.target)) menu.open = false;
      });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      menus.forEach(function (menu) {
        if (!menu.open) return;
        menu.open = false;
        const summary = menu.querySelector("summary");
        if (summary) summary.focus();
      });
    });
  }

  // ------------------------------------------------------ scrolling strips
  // Scrolls a link into the middle of the sideways-scrolling strip it sits in.
  // Only the strip moves, never the page.
  function centreInStrip(link) {
    const strip = link.parentElement;
    if (!strip || strip.scrollWidth <= strip.clientWidth) return;
    const offset = link.getBoundingClientRect().left - strip.getBoundingClientRect().left + strip.scrollLeft;
    strip.scrollLeft = offset - strip.clientWidth / 2 + link.clientWidth / 2;
  }

  // On phones the dashboard navigation is such a strip: show the current page's link.
  function setUpNavStrip() {
    const current = document.querySelector('nav.jb-scroll-x [aria-current="page"]');
    if (current) centreInStrip(current);
  }

  // -------------------------------------------------------- "on this page"
  // Links with data-spy are highlighted while their section is on screen.
  function setUpSectionSpy() {
    const links = Array.prototype.slice.call(document.querySelectorAll("a[data-spy]"));
    if (!links.length || !("IntersectionObserver" in window)) return;
    const byId = {};
    links.forEach(function (link) {
      byId[link.getAttribute("href").slice(1)] = link;
    });

    const observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          links.forEach(function (link) {
            link.removeAttribute("aria-current");
          });
          const link = byId[entry.target.id];
          link.setAttribute("aria-current", "true");
          centreInStrip(link);
        });
      },
      { rootMargin: "-25% 0px -65% 0px" }
    );
    Object.keys(byId).forEach(function (id) {
      const section = document.getElementById(id);
      if (section) observer.observe(section);
    });
  }

  // ------------------------------------------------------------ scroll reveal
  // Elements with .jb-reveal fade in as they scroll into view. Whatever is
  // already on screen is shown at once, so nothing flickers on load.
  function setUpReveal() {
    const items = Array.prototype.slice.call(document.querySelectorAll(".jb-reveal"));
    if (!items.length || !("IntersectionObserver" in window)) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    items.forEach(function (item) {
      if (item.getBoundingClientRect().top < window.innerHeight) item.classList.add("is-visible");
    });
    document.documentElement.classList.add("jb-js");

    const observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -8% 0px" }
    );
    items.forEach(function (item) {
      if (!item.classList.contains("is-visible")) observer.observe(item);
    });
  }

  function setUp() {
    setUpReveal();
    setUpPreview();
    setUpInsertButtons();
    setUpSaveBars();
    setUpColorInputs();
    setUpMenus();
    setUpNavStrip();
    setUpSectionSpy();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setUp);
  } else {
    setUp();
  }
})();
