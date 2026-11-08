// Live preview for message fields on the dashboard.
//
// Mirrors joltbot/core/templating.py: only {simple_names} are replaced, unknown
// ones are left as typed, and {guild} is an old name for {server}. Keep the two
// in sync if you change one.
(function () {
  "use strict";

  const ALIASES = { guild: "server" };

  function sampleValues(serverName) {
    return {
      user: "@NewMember",
      user_name: "NewMember",
      server: serverName || "Your Server",
      channel: "#ticket-newmember",
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

  // Just enough Discord markdown for a preview: **bold**, *italic*, `code`.
  function markdown(html) {
    return html
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\*(.+?)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, '<code class="rounded bg-black/30 px-1">$1</code>');
  }

  function setUp() {
    const form = document.querySelector("form[data-server-name]");
    const values = sampleValues(form ? form.dataset.serverName : "");
    document.querySelectorAll("[data-preview]").forEach(function (field) {
      const target = document.getElementById(field.dataset.preview);
      if (!target) return;
      const update = function () {
        target.innerHTML = markdown(escapeHtml(render(field.value, values)));
      };
      field.addEventListener("input", update);
      update();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setUp);
  } else {
    setUp();
  }
})();
