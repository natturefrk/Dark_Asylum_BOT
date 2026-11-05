// Portal behaviour: folder tabs, member autocomplete, points simulator, idle sign-out warning.
// Kept in one external file because the Content-Security-Policy forbids inline scripts.
(function () {
  "use strict";
  const body = document.body;
  const csrf = body.dataset.csrf;

  // --- Folder-style tabs: show one panel, hide the rest -----------------------------------
  document.querySelectorAll("[data-tabs]").forEach(function (bar) {
    bar.addEventListener("click", function (ev) {
      const btn = ev.target.closest("[data-tab]");
      if (!btn) return;
      bar.querySelectorAll(".tab").forEach(function (t) { t.classList.toggle("on", t === btn); });
      document.querySelectorAll(".tabpanel").forEach(function (p) {
        p.hidden = p.id !== "tab-" + btn.dataset.tab;
      });
    });
  });

  // --- Member autocomplete: fills a <datalist> from the search API ------------------------
  document.querySelectorAll("[data-search]").forEach(function (input) {
    const list = document.getElementById(input.getAttribute("list") || "");
    input.addEventListener("input", function () {
      if (input.value.length < 1 || !list) return;
      fetch("/api/members/search?q=" + encodeURIComponent(input.value), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (rows) {
          list.replaceChildren();
          rows.forEach(function (m) {
            const opt = document.createElement("option");
            opt.value = m.id;
            opt.label = (m.rsn || "unlinked") + " (" + m.status + ")";
            list.appendChild(opt);
          });
        });
    });
  });

  // --- Preset award dropdown copies its amount into the amount box -------------------------
  const preset = document.getElementById("preset-pick");
  if (preset) {
    preset.addEventListener("change", function () {
      document.getElementById("award-amount").value = preset.value;
    });
  }

  // --- Reward simulator: monthly points -> GP, using the table values ----------------------
  const sim = document.getElementById("tab-sim");
  if (sim) {
    const val = function (id) { return Number(document.getElementById(id).value) || 0; };
    const run = function () {
      // visit 50, cap bonus +100, event 150, competition 100 (values from the clan point table)
      const pts = val("sim-visits") * 50 + val("sim-caps") * 100 + val("sim-events") * 150 + val("sim-comps") * 100;
      const gp = pts * Number(sim.dataset.rate);
      document.getElementById("sim-points").textContent = pts.toLocaleString();
      document.getElementById("sim-gp").textContent = gp.toLocaleString();
      document.getElementById("sim-months").textContent = gp ? (150000000 / gp).toFixed(1) : "never";
    };
    sim.addEventListener("input", run);
    run();
  }

  // --- Idle sign-out: warn when 2 minutes remain, sign out at zero -------------------------
  if (!body.dataset.authed) return;
  const modal = document.getElementById("idle-modal");
  const counter = document.getElementById("idle-count");
  const warnAt = Number(body.dataset.warn);
  let lastPing = Date.now();

  function post(url) {
    return fetch(url, { method: "POST", headers: { "X-CSRF-Token": csrf }, credentials: "same-origin" });
  }
  // User activity keeps the server session alive (throttled to once a minute).
  ["click", "keydown"].forEach(function (name) {
    document.addEventListener(name, function () {
      if (modal.hidden && Date.now() - lastPing > 60000) { lastPing = Date.now(); post("/session/keepalive"); }
    });
  });
  document.getElementById("idle-stay").addEventListener("click", function () {
    post("/session/keepalive").then(function () { modal.hidden = true; lastPing = Date.now(); });
  });
  // Poll the server (which owns the real timer) without extending the session.
  setInterval(function () {
    fetch("/session/status", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (s.remaining <= 0) { window.location.href = "/login"; return; }
        modal.hidden = s.remaining > warnAt;
        counter.textContent = s.remaining;
      })
      .catch(function () {});
  }, 5000);
})();
