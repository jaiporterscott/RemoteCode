"use strict";
/* omc-status.js — live "what is OMC doing" telemetry strip.
 * Self-contained module, no framework, no build step, vendored offline (nothing external).
 * Public API: window.OMCStatus = { init, setSession, update, destroy }
 * See /home/admin/RemoteCode/.omc/handoffs/api-contract.md for the snapshot shape this renders.
 */
(function () {
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const reduceMotion = () =>
    !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  const MODE_LABEL = {
    team: "Team", ralph: "Ralph", autopilot: "Autopilot", ultrawork: "Ultrawork",
    ultraqa: "UltraQA", ultragoal: "UltraGoal", swarm: "Swarm", ultrapilot: "Ultrapilot",
    pipeline: "Pipeline",
  };
  // reuse existing style.css tokens only — no invented hexes
  const MODE_TONE = {
    team: "accent2", ralph: "accent", autopilot: "ok", ultrawork: "wait",
    ultraqa: "wait", ultragoal: "accent", swarm: "accent2", ultrapilot: "ok",
    pipeline: "accent2",
  };
  const KIND_LABEL = { notepad: "Notepad", plan: "Plans", handoff: "Handoffs", research: "Research", memory: "Memory" };
  const KIND_ORDER = ["notepad", "plan", "handoff", "research", "memory"];
  const AGENT_CAP = 8;

  function human(n) {
    if (n == null) return "";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0; n = +n;
    while (n >= 1024 && i < 3) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + u[i];
  }
  function relTime(sec) {
    if (sec == null) return "";
    let d = Math.max(0, Date.now() / 1000 - sec);
    if (d < 60) return "just now";
    d = Math.floor(d / 60);
    if (d < 60) return d + "m ago";
    d = Math.floor(d / 60);
    if (d < 24) return d + "h ago";
    d = Math.floor(d / 24);
    return d + "d ago";
  }
  function fmtElapsed(startedAt) {
    if (!startedAt) return "";
    const t = Date.parse(startedAt);
    if (Number.isNaN(t)) return "";
    let s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); s -= m * 60;
    if (h) return `${h}h${String(m).padStart(2, "0")}m`;
    if (m) return `${m}m${String(s).padStart(2, "0")}s`;
    return `${s}s`;
  }

  const state = { sessionId: null, openArtifactCb: null };
  let dom = null;
  let expanded = false;
  let lastSig = null;
  let pollTimer = null;
  let tickTimer = null;
  let lastAnnounce = "";
  let fallback = null; // lazily-built artifact fallback sheet

  function buildDom() {
    const root = el("div", "omc-status hidden");
    root.id = "omcStatus";
    root.setAttribute("role", "region");
    root.setAttribute("aria-label", "OMC status");

    const strip = el("div", "omc-strip");
    strip.tabIndex = 0;
    strip.setAttribute("role", "button");
    strip.setAttribute("aria-expanded", "false");
    strip.setAttribute("aria-controls", "omcPanel");

    const modesRow = el("div", "omc-modes");
    const todoBadge = el("span", "omc-todo-badge hidden");
    const spacer = el("div", "omc-spacer");
    const cancelBtn = el("button", "btn danger icon omc-cancel hidden", "Cancel");
    cancelBtn.type = "button";
    const caret = el("span", "omc-caret");
    caret.setAttribute("aria-hidden", "true");

    strip.append(modesRow, todoBadge, spacer, cancelBtn, caret);

    const live = el("div", "omc-live");
    live.id = "omcLive";
    live.setAttribute("aria-live", "polite");
    live.setAttribute("role", "status");

    const panel = el("div", "omc-panel hidden");
    panel.id = "omcPanel";
    const todoSection = el("div", "omc-section");
    const agentSection = el("div", "omc-section");
    const artifactSection = el("div", "omc-section");
    panel.append(todoSection, agentSection, artifactSection);

    root.append(strip, live, panel);

    strip.addEventListener("click", () => togglePanel());
    strip.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); togglePanel(); }
    });
    cancelBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!state.sessionId) return;
      if (!confirm("Cancel the active OMC mode?")) return;
      cancelBtn.disabled = true;
      try {
        const r = await fetch(`/api/sessions/${encodeURIComponent(state.sessionId)}/omc/cancel`, { method: "POST" });
        if (!r.ok) throw new Error(await r.text().catch(() => String(r.status)));
      } catch (err) { /* transient — the next SSE/poll update re-syncs the real state */ }
      finally { cancelBtn.disabled = false; }
    });
    // tap outside the strip/panel collapses it — the panel is an overlay so it never
    // resizes #chat, but it should still get out of the way on outside tap
    document.addEventListener("click", (e) => {
      if (!expanded || !dom) return;
      if (!dom.root.contains(e.target)) { expanded = false; syncPanelVisibility(); }
    }, true);

    return { root, strip, modesRow, todoBadge, cancelBtn, caret, live, panel, todoSection, agentSection, artifactSection };
  }

  function togglePanel() {
    expanded = !expanded;
    syncPanelVisibility();
  }
  function syncPanelVisibility() {
    dom.panel.classList.toggle("hidden", !expanded);
    dom.strip.setAttribute("aria-expanded", String(expanded));
    dom.strip.classList.toggle("expanded", expanded);
  }

  /* ---------- rendering ---------- */
  function modeChip(m) {
    const tone = MODE_TONE[m.mode] || "dim";
    const chip = el("span", `omc-mode-chip tone-${tone}` + (m.active ? "" : " done"));
    chip.append(el("span", "omc-mode-name", MODE_LABEL[m.mode] || m.mode));
    if (m.phase) chip.append(el("span", "omc-mode-phase", m.phase));
    if (m.iteration != null) {
      const it = m.maxIterations != null ? `${m.iteration}/${m.maxIterations}` : String(m.iteration);
      chip.append(el("span", "omc-mode-iter", it));
    }
    if (m.active && m.startedAt) {
      const e = el("span", "omc-elapsed", fmtElapsed(m.startedAt));
      e.dataset.started = m.startedAt;
      chip.append(e);
    }
    const tip = [m.task, m.detail].filter(Boolean).join(" — ");
    if (tip) chip.title = tip;
    return chip;
  }
  function renderModes(modes) {
    dom.modesRow.innerHTML = "";
    if (!modes.length) { dom.modesRow.append(el("span", "omc-idle", "OMC idle")); return; }
    modes.forEach((m) => dom.modesRow.append(modeChip(m)));
  }
  function renderTodoBadge(todos) {
    if (!todos.length) { dom.todoBadge.classList.add("hidden"); dom.todoBadge.textContent = ""; return; }
    const done = todos.filter((t) => t.status === "completed").length;
    dom.todoBadge.textContent = `${done}/${todos.length}`;
    dom.todoBadge.classList.remove("hidden");
  }
  function renderTodoSection(todos) {
    const sec = dom.todoSection; sec.innerHTML = "";
    sec.append(el("div", "omc-section-title", "Todos"));
    if (!todos.length) { sec.append(el("div", "omc-empty", "No todos yet.")); return; }
    const order = { in_progress: 0, pending: 1, completed: 2 };
    const list = el("div", "omc-todolist");
    todos.slice()
      .sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3))
      .forEach((t) => {
        const status = t.status || "pending";
        const row = el("div", `omc-todo omc-todo-${status}`);
        const mark = el("span", "omc-todo-mark");
        if (status === "in_progress") mark.append(el("span", "dot busy"));
        else mark.textContent = status === "completed" ? "✓" : "○";
        row.append(mark);
        const label = status === "in_progress" && t.activeForm ? t.activeForm : t.content;
        row.append(el("span", "omc-todo-text", label || ""));
        list.append(row);
      });
    sec.append(list);
  }
  function agentRow(a, running) {
    const row = el("div", "omc-agent" + (running ? "" : " done"));
    row.append(el("span", "dot" + (running ? " busy" : "")));
    const meta = el("div", "omc-agent-meta");
    meta.append(el("div", "omc-agent-desc", a.description || a.type || "agent"));
    if (a.type) meta.append(el("div", "omc-agent-type", a.type));
    row.append(meta);
    return row;
  }
  function renderAgentSection(agents) {
    const sec = dom.agentSection; sec.innerHTML = "";
    sec.append(el("div", "omc-section-title", "Subagents"));
    if (!agents.length) { sec.append(el("div", "omc-empty", "No subagents yet.")); return; }
    const sorted = agents.slice().sort((a, b) => (Date.parse(b.at || 0) || 0) - (Date.parse(a.at || 0) || 0));
    const running = sorted.filter((a) => a.status === "running");
    const done = sorted.filter((a) => a.status !== "running");
    const list = el("div", "omc-agentlist");
    running.forEach((a) => list.append(agentRow(a, true)));
    const doneSlots = Math.max(0, AGENT_CAP - running.length);
    done.slice(0, doneSlots).forEach((a) => list.append(agentRow(a, false)));
    sec.append(list);
    const shown = running.length + Math.min(done.length, doneSlots);
    const rest = agents.length - shown;
    if (rest > 0) sec.append(el("div", "omc-more", `+${rest} more`));
  }
  function artifactRow(a) {
    const row = el("button", "omc-artifact");
    row.type = "button";
    row.append(el("span", "omc-art-name", a.name));
    const metaText = [human(a.size), relTime(a.mtime)].filter(Boolean).join(" · ");
    row.append(el("span", "omc-art-meta", metaText));
    row.addEventListener("click", () => openArtifact(a.path, a.name));
    return row;
  }
  function renderArtifactSection(artifacts) {
    const sec = dom.artifactSection; sec.innerHTML = "";
    sec.append(el("div", "omc-section-title", "Artifacts"));
    if (!artifacts.length) { sec.append(el("div", "omc-empty", "No artifacts yet.")); return; }
    const groups = new Map();
    artifacts.forEach((a) => {
      const k = a.kind || "other";
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(a);
    });
    const kinds = [...groups.keys()].sort((a, b) => {
      const ia = KIND_ORDER.indexOf(a), ib = KIND_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    kinds.forEach((k) => {
      const group = el("div", "omc-artgroup");
      group.append(el("div", "omc-artgroup-title", KIND_LABEL[k] || k));
      groups.get(k)
        .sort((a, b) => (b.mtime || 0) - (a.mtime || 0))
        .forEach((a) => group.append(artifactRow(a)));
      sec.append(group);
    });
  }
  function announce(modes) {
    const active = modes.filter((m) => m.active);
    const text = active.map((m) => `${MODE_LABEL[m.mode] || m.mode}: ${m.phase || "running"}`).join(" · ");
    if (text && text !== lastAnnounce) { lastAnnounce = text; dom.live.textContent = text; }
    else if (!text) { lastAnnounce = ""; dom.live.textContent = ""; }
  }

  function hide() { dom.root.classList.add("hidden"); }
  function render(snapshot) {
    if (!snapshot || snapshot.available === false) { hide(); return; }
    const modes = snapshot.modes || [];
    const todos = snapshot.todos || [];
    const agents = snapshot.agents || [];
    const artifacts = snapshot.artifacts || [];
    if (!modes.length && !todos.length && !agents.length && !artifacts.length) { hide(); return; }

    dom.root.classList.remove("hidden");
    renderModes(modes);
    renderTodoBadge(todos);
    renderTodoSection(todos);
    renderAgentSection(agents);
    renderArtifactSection(artifacts);
    const anyActive = modes.some((m) => m.active);
    dom.cancelBtn.classList.toggle("hidden", !anyActive);
    dom.root.classList.toggle("omc-active", anyActive);
    announce(modes);
  }

  /* ---------- polling / ticking ---------- */
  function managePolling(snapshot) {
    const active = !!(snapshot && snapshot.available !== false && (snapshot.modes || []).some((m) => m.active));
    if (active && !pollTimer) pollTimer = setInterval(pollNow, 5000);
    else if (!active && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  async function pollNow() {
    if (!state.sessionId) return;
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(state.sessionId)}/omc`);
      if (!r.ok) return;
      applyUpdate(await r.json());
    } catch (e) { /* transient network hiccup — retried on the next tick */ }
  }
  function applyUpdate(snapshot) {
    const sig = JSON.stringify(snapshot === undefined ? null : snapshot);
    if (sig !== lastSig) { lastSig = sig; render(snapshot); }
    managePolling(snapshot);
  }
  function startTicking() {
    if (tickTimer) return;
    tickTimer = setInterval(() => {
      if (reduceMotion() || !dom || dom.root.classList.contains("hidden")) return;
      dom.root.querySelectorAll(".omc-elapsed[data-started]").forEach((e) => {
        e.textContent = fmtElapsed(e.dataset.started);
      });
    }, 1000);
  }

  /* ---------- artifact fallback sheet ---------- */
  function ensureFallback() {
    if (fallback) return fallback;
    const overlay = el("div", "omc-artoverlay hidden");
    const box = el("div", "omc-artbox");
    const head = el("div", "omc-artbox-head");
    const title = el("b", "omc-artbox-title");
    const close = el("button", "btn icon", "✕");
    close.type = "button";
    head.append(title, close);
    const body = el("pre", "omc-artbox-body");
    const code = el("code");
    body.append(code);
    box.append(head, body);
    overlay.append(box);
    document.body.append(overlay);
    close.addEventListener("click", () => overlay.classList.add("hidden"));
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.classList.add("hidden"); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !overlay.classList.contains("hidden")) overlay.classList.add("hidden");
    });
    fallback = { overlay, title, code };
    return fallback;
  }
  async function openArtifactFallback(path, name) {
    const f = ensureFallback();
    f.title.textContent = name || path;
    f.code.textContent = "loading…";
    f.overlay.classList.remove("hidden");
    try {
      const r = await fetch("/api/omc/artifact?path=" + encodeURIComponent(path));
      if (!r.ok) throw new Error((await r.text().catch(() => "")) || String(r.status));
      const d = await r.json();
      f.title.textContent = d.name || name || path;
      f.code.textContent = d.text || "";
    } catch (err) {
      f.code.textContent = "error: " + err.message;
    }
  }
  function openArtifact(path, name) {
    if (typeof state.openArtifactCb === "function") { state.openArtifactCb(path, name); return; }
    openArtifactFallback(path, name);
  }

  /* ---------- public API ---------- */
  function init(opts) {
    opts = opts || {};
    state.sessionId = opts.sessionId || null;
    state.openArtifactCb = typeof opts.openArtifact === "function" ? opts.openArtifact : null;
    if (dom) return; // already inited — treat as idempotent
    const bar = document.getElementById("claudeBar");
    if (!bar || !bar.parentNode) return; // no host to anchor to — silent no-op
    dom = buildDom();
    bar.parentNode.insertBefore(dom.root, bar);
    startTicking();
  }
  function setSession(id) {
    state.sessionId = id || null;
    lastSig = null;
    expanded = false;
    if (dom) { syncPanelVisibility(); hide(); }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  function update(snapshot) { if (dom) applyUpdate(snapshot); }
  function destroy() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
    if (dom) { dom.root.remove(); dom = null; }
    if (fallback) { fallback.overlay.remove(); fallback = null; }
    lastSig = null; lastAnnounce = ""; expanded = false;
    state.sessionId = null; state.openArtifactCb = null;
  }

  window.OMCStatus = { init, setSession, update, destroy };
})();
