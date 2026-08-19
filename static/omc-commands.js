"use strict";
/*
 * OMC command palette — quick-action chips, "/" autocomplete, and a full skills
 * browser sheet, all self-injected directly above #promptForm.
 *
 * Public API: window.OMCCommands = { init({sessionId, sendPrompt, onRun}), setSession(id), destroy() }
 * Graceful no-op whenever GET /api/omc/skills is unavailable or absent.
 */
(function () {
  const FORM_SEL = "#promptForm";
  const INPUT_SEL = "#promptInput";
  const SKILLS_URL = "/api/omc/skills";
  const TIER0_ORDER = ["autopilot", "ultrawork", "ralph", "team", "ralplan"];
  const DESTRUCTIVE_RE = /cancel|reset|clear|stop|delete|kill|abort/i;
  const SOURCE_LABEL = { plugin: "Plugin", user: "User", project: "Project" };
  const SOURCE_ORDER = ["plugin", "user", "project"];
  const POP_LIMIT = 8;

  const $ = s => document.querySelector(s);
  const el = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; };

  let state = null;   // per-init instance data; null when disabled/destroyed
  let gen = 0;         // guards against stale async work after destroy()/re-init()

  /* ---------- lifecycle ---------- */
  function init(opts) {
    if (state) destroy();
    const myGen = ++gen;
    state = {
      sessionId: (opts && opts.sessionId) || null,
      sendPrompt: (opts && typeof opts.sendPrompt === "function") ? opts.sendPrompt : null,
      onRun: (opts && typeof opts.onRun === "function") ? opts.onRun : null,
      enabled: false,
      skills: [],
      container: null,
      chipBar: null,
      stagedBar: null,
      errorBar: null,
      popover: null,
      sheet: null,
      sheetSearch: null,
      sheetBody: null,
      staged: null,          // the skill currently staged in the composer, or null
      popoverOpen: false,
      popMatches: [],        // [{...skill}] currently rendered, may include a synthetic "__more" entry
      popSel: -1,
      sheetOpen: false,
      sheetLastFocus: null,
    };
    const form = $(FORM_SEL);
    if (!form) return;       // no-op silently — host anchor absent

    fetch(SKILLS_URL).then(r => r.ok ? r.json() : { available: false })
      .catch(() => ({ available: false }))
      .then(d => {
        if (gen !== myGen || !state) return;             // superseded or destroyed meanwhile
        if (!d || !d.available || !Array.isArray(d.skills)) return;  // graceful no-op
        state.skills = d.skills;
        buildUI(form);
        state.enabled = true;
      });
  }

  function setSession(id) {
    if (!state) return;
    state.sessionId = id || null;
    unstage();
    closePopover();
    closeSheet();
  }

  function destroy() {
    clearTimeout(errorTimer);
    document.removeEventListener("keydown", onDocKeydown, true);
    const input = $(INPUT_SEL);
    if (input) {
      input.removeEventListener("input", onInput);
      input.removeEventListener("blur", onInputBlur);
      ["aria-expanded", "aria-controls", "aria-autocomplete", "aria-activedescendant", "role"].forEach(a => input.removeAttribute(a));
    }
    if (state && state.container && state.container.parentNode) {
      state.container.parentNode.removeChild(state.container);
    }
    state = null;
  }

  /* ---------- build ---------- */
  function buildUI(form) {
    const container = el("div", "omc-commands");
    container.setAttribute("data-omc-commands", "");

    const chipBar = el("div", "omc-chipbar");
    chipBar.setAttribute("role", "toolbar");
    chipBar.setAttribute("aria-label", "OMC quick actions");
    container.append(chipBar);

    const stagedBar = el("div", "omc-staged hidden");
    container.append(stagedBar);

    const errorBar = el("div", "omc-error hidden");
    errorBar.setAttribute("role", "alert");
    container.append(errorBar);

    const popover = el("div", "omc-pop hidden");
    popover.id = "omcPopover";
    popover.setAttribute("role", "listbox");
    popover.setAttribute("aria-label", "Matching OMC skills");
    popover.addEventListener("mousedown", e => e.preventDefault());  // keep focus in the textarea
    container.append(popover);

    const sheet = buildSheet();
    container.append(sheet);

    form.parentNode.insertBefore(container, form);

    state.container = container;
    state.chipBar = chipBar;
    state.stagedBar = stagedBar;
    state.errorBar = errorBar;
    state.popover = popover;
    state.sheet = sheet;
    state.sheetSearch = sheet.querySelector("#omcSheetSearch");
    state.sheetBody = sheet.querySelector("#omcSheetBody");

    renderChips();

    const input = $(INPUT_SEL);
    if (input) {
      input.setAttribute("role", "combobox");
      input.setAttribute("aria-autocomplete", "list");
      input.setAttribute("aria-expanded", "false");
      input.setAttribute("aria-controls", "omcPopover");
      input.addEventListener("input", onInput);
      input.addEventListener("blur", onInputBlur);
    }
    document.addEventListener("keydown", onDocKeydown, true);
  }

  function buildSheet() {
    const sheet = el("div", "modal omc-sheet hidden");
    sheet.id = "omcSheet";
    sheet.setAttribute("role", "dialog");
    sheet.setAttribute("aria-modal", "true");
    sheet.setAttribute("aria-labelledby", "omcSheetTitle");

    const box = el("div", "modal-box omc-sheet-box");
    const head = el("div", "modal-head");
    head.append(el("b", null, "OMC skills"));
    head.lastChild.id = "omcSheetTitle";
    const actions = el("span", "head-actions");
    const closeBtn = el("button", "btn icon", "✕");
    closeBtn.type = "button"; closeBtn.id = "omcSheetClose"; closeBtn.setAttribute("aria-label", "Close");
    closeBtn.addEventListener("click", closeSheet);
    actions.append(closeBtn);
    head.append(actions);
    box.append(head);

    const searchWrap = el("div", "omc-sheet-search");
    const search = document.createElement("input");
    search.type = "text"; search.id = "omcSheetSearch"; search.placeholder = "Search skills…";
    search.autocomplete = "off"; search.setAttribute("aria-label", "Search skills");
    search.addEventListener("input", () => renderSheetList(search.value.trim().toLowerCase()));
    searchWrap.append(search);
    box.append(searchWrap);

    const body = el("div", "omc-sheet-body");
    body.id = "omcSheetBody";
    box.append(body);

    sheet.append(box);
    sheet.addEventListener("mousedown", e => { if (e.target === sheet) closeSheet(); });
    return sheet;
  }

  /* ---------- chip bar ---------- */
  function renderChips() {
    const bar = state.chipBar;
    bar.innerHTML = "";
    const byName = new Map(state.skills.map(s => [s.name, s]));
    TIER0_ORDER.forEach(name => {
      const s = byName.get(name);
      if (s) bar.append(chipEl(s));
    });
    const more = el("button", "omc-chip omc-chip-more", "⋯ All skills");
    more.type = "button";
    more.addEventListener("click", () => openSheet(""));
    bar.append(more);
  }

  function chipEl(skill) {
    const b = el("button", "omc-chip" + (skill.tier0 ? " tier0" : ""));
    b.type = "button";
    b.append(el("span", "omc-chip-dot"));
    b.append(el("span", "omc-chip-label", skill.name));
    b.title = skill.description || skill.command;
    b.addEventListener("click", () => activateSkill(skill));
    return b;
  }

  /* ---------- activate (shared by chip bar + sheet rows) ---------- */
  function activateSkill(skill, opts) {
    opts = opts || {};
    if (opts.closeSheet) closeSheet();
    if (skill.takesArgs) { stage(skill); return; }
    if (DESTRUCTIVE_RE.test(skill.command) || DESTRUCTIVE_RE.test(skill.name)) {
      if (!confirm(`Run ${skill.name}? This can end or change the current session's state.`)) return;
    }
    doRun(skill, "");
  }

  /* ---------- staged command pill ---------- */
  function stage(skill) {
    state.staged = skill;
    renderStaged();
    closePopover();
    const input = $(INPUT_SEL);
    if (input) input.focus();
  }
  function unstage() {
    if (!state) return;
    state.staged = null;
    renderStaged();
  }
  function renderStaged() {
    const bar = state.stagedBar;
    bar.innerHTML = "";
    if (!state.staged) { bar.classList.add("hidden"); return; }
    bar.classList.remove("hidden");
    const pill = el("span", "omc-pill");
    pill.append(el("span", "omc-pill-name", state.staged.name));
    const x = el("button", "omc-pill-x", "✕");
    x.type = "button"; x.setAttribute("aria-label", "Cancel staged command");
    x.addEventListener("click", unstage);
    pill.append(x);
    bar.append(pill);
    bar.append(el("span", "omc-staged-hint", "type your task, then"));
    const run = el("button", "omc-run-btn", "▶ Run");
    run.type = "button";
    run.addEventListener("click", runStaged);
    bar.append(run);
  }
  async function runStaged() {
    const skill = state.staged;
    if (!skill) return;
    const ta = $(INPUT_SEL);
    const args = ta ? ta.value.trim() : "";
    const ok = await doRun(skill, args);
    if (ok) {
      if (ta) { ta.value = ""; ta.style.height = "auto"; ta.dispatchEvent(new Event("input", { bubbles: true })); }
      unstage();
    }
  }

  /* ---------- run (POST /omc/run — the only path that launches a skill; no silent fallback) ---------- */
  async function doRun(skill, args) {
    if (!state.sessionId) {
      const msg = "no active session";
      state.onRun && state.onRun({ command: skill.command, args, ok: false, error: msg });
      showError(`${skill.name} — ${msg}`);
      return false;
    }
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(state.sessionId)}/omc/run`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ command: skill.command, args }),
      });
      if (!r.ok) throw new Error((await r.text().catch(() => "")) || String(r.status));
      const d = await r.json().catch(() => ({}));
      state.onRun && state.onRun({ command: skill.command, args, ok: true, sent: d && d.sent });
      return true;
    } catch (e) {
      // Report failure honestly and surface it in the UI — do NOT paper over it by
      // re-sending through a different path (that would make a 400/403 from the
      // validated endpoint just quietly retry via another route, and would report
      // ok:true for a call that failed).
      state.onRun && state.onRun({ command: skill.command, args, ok: false, error: e.message });
      showError(`${skill.name} failed to run — ${e.message}`);
      return false;
    }
  }

  /* ---------- inline error banner (near the composer, auto-dismisses) ---------- */
  let errorTimer = null;
  function showError(text) {
    if (!state || !state.errorBar) return;
    clearTimeout(errorTimer);
    state.errorBar.textContent = "⚠ " + text;
    state.errorBar.classList.remove("hidden");
    errorTimer = setTimeout(() => { if (state && state.errorBar) state.errorBar.classList.add("hidden"); }, 4500);
  }

  /* ---------- "/" autocomplete ---------- */
  function onInput(e) {
    if (!state || !state.enabled) return;
    const ta = e.target;
    const val = ta.value;
    if (val[0] !== "/") { closePopover(); return; }
    const pos = ta.selectionStart;
    const ws = val.search(/\s/);
    const tokenEnd = ws === -1 ? val.length : ws;
    if (pos > tokenEnd) { closePopover(); return; }   // caret has moved past the command token
    openPopoverWithQuery(val.slice(1, tokenEnd).toLowerCase());
  }
  function onInputBlur() { closePopover(); }

  function matchSkills(query) {
    const all = state.skills;
    if (!query) return all.slice().sort(bySkillOrder);
    const q = query;
    return all.filter(s => s.name.toLowerCase().includes(q) || s.command.toLowerCase().includes(q))
      .sort((a, b) => {
        const aStarts = a.name.toLowerCase().startsWith(q) ? 0 : 1;
        const bStarts = b.name.toLowerCase().startsWith(q) ? 0 : 1;
        return aStarts !== bStarts ? aStarts - bStarts : bySkillOrder(a, b);
      });
  }
  function bySkillOrder(a, b) {
    if (!!a.tier0 !== !!b.tier0) return a.tier0 ? -1 : 1;
    return a.name.localeCompare(b.name);
  }

  function openPopoverWithQuery(query) {
    const all = matchSkills(query);
    const shown = all.slice(0, POP_LIMIT);
    const moreCount = all.length - shown.length;
    state.popMatches = shown.slice();
    if (moreCount > 0) state.popMatches.push({ __more: true, __query: query, __count: moreCount });
    state.popSel = state.popMatches.length ? 0 : -1;
    state.popoverOpen = true;
    renderPopover();
    const input = $(INPUT_SEL);
    if (input) input.setAttribute("aria-expanded", "true");
  }
  function closePopover() {
    if (!state) return;
    if (!state.popoverOpen) return;
    state.popoverOpen = false;
    state.popMatches = [];
    state.popSel = -1;
    if (state.popover) { state.popover.classList.add("hidden"); state.popover.innerHTML = ""; }
    const input = $(INPUT_SEL);
    if (input) { input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); }
  }
  function renderPopover() {
    const pop = state.popover;
    pop.innerHTML = "";
    if (!state.popMatches.length) {
      pop.append(el("div", "omc-pop-empty", "No matching skills"));
      pop.classList.remove("hidden");
      return;
    }
    state.popMatches.forEach((m, i) => {
      const sel = i === state.popSel;
      if (m.__more) {
        const row = el("div", "omc-pop-item omc-pop-more" + (sel ? " sel" : ""));
        row.id = "omcPopOpt" + i;
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", String(sel));
        row.textContent = `⋯ ${m.__count} more — see all skills`;
        row.addEventListener("click", () => { openSheet(m.__query); closePopover(); });
        pop.append(row);
        return;
      }
      const row = el("div", "omc-pop-item" + (sel ? " sel" : ""));
      row.id = "omcPopOpt" + i;
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(sel));
      const top = el("div", "omc-pop-row");
      top.append(el("span", "omc-pop-name", m.name));
      if (m.tier0) top.append(el("span", "omc-pop-badge", "tier 0"));
      row.append(top);
      if (m.description) row.append(el("div", "omc-pop-desc", m.description));
      row.addEventListener("click", () => pickMatch(i));
      pop.append(row);
    });
    pop.classList.remove("hidden");
    const input = $(INPUT_SEL);
    if (input && state.popSel >= 0) input.setAttribute("aria-activedescendant", "omcPopOpt" + state.popSel);
  }
  function movePopSel(dir) {
    if (!state.popMatches.length) return;
    state.popSel = (state.popSel + dir + state.popMatches.length) % state.popMatches.length;
    renderPopover();
  }
  function pickMatch(i) {
    const m = state.popMatches[i];
    if (!m) return;
    if (m.__more) { openSheet(m.__query); closePopover(); return; }
    insertCommand(m);
    closePopover();
  }
  function pickPopSel() { if (state.popSel >= 0) pickMatch(state.popSel); }

  function insertCommand(skill) {
    const ta = $(INPUT_SEL);
    if (!ta) return;
    const val = ta.value;
    const ws = val.search(/\s/);
    const after = ws === -1 ? "" : val.slice(ws).replace(/^\s+/, " ");
    ta.value = skill.command + " " + after;
    const caret = skill.command.length + 1;
    ta.setSelectionRange(caret, caret);
    ta.focus();
    ta.dispatchEvent(new Event("input", { bubbles: true }));   // triggers app.js's auto-grow
  }

  /* ---------- keyboard routing (capture phase — see report for why) ---------- */
  function onDocKeydown(e) {
    if (!state || !state.enabled) return;
    if (state.sheetOpen) {
      if (e.key === "Escape") { closeSheet(); return; }
      if (e.key === "Tab") { trapSheetTab(e); }
      return;
    }
    const input = $(INPUT_SEL);
    if (!input) return;
    if (state.popoverOpen && e.target === input) {
      if (e.key === "ArrowDown") { e.preventDefault(); e.stopPropagation(); movePopSel(1); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); e.stopPropagation(); movePopSel(-1); return; }
      if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); pickPopSel(); return; }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closePopover(); return; }
      if (e.key === "Tab") { closePopover(); return; }
      return;
    }
    if (state.staged && e.target === input && e.key === "Enter" && !e.shiftKey) {
      e.preventDefault(); e.stopPropagation();
      runStaged();
    }
  }

  /* ---------- skills browser sheet ---------- */
  function openSheet(presetQuery) {
    if (!state.sheet) return;
    state.sheetOpen = true;
    state.sheetLastFocus = document.activeElement;
    state.sheet.classList.remove("hidden");
    state.sheetSearch.value = presetQuery || "";
    renderSheetList((presetQuery || "").toLowerCase());
    state.sheetSearch.focus();
  }
  function closeSheet() {
    if (!state || !state.sheetOpen) return;
    state.sheetOpen = false;
    state.sheet.classList.add("hidden");
    state.sheetBody.innerHTML = "";
    if (state.sheetLastFocus && typeof state.sheetLastFocus.focus === "function") {
      try { state.sheetLastFocus.focus(); } catch (e) { /* target may be gone */ }
    }
    state.sheetLastFocus = null;
  }
  function renderSheetList(query) {
    const body = state.sheetBody;
    body.innerHTML = "";
    const q = (query || "").toLowerCase();
    const filtered = state.skills.filter(s =>
      !q || s.name.toLowerCase().includes(q) || (s.description || "").toLowerCase().includes(q) || s.command.toLowerCase().includes(q));
    if (!filtered.length) { body.append(el("div", "omc-sheet-empty", "No skills found")); return; }
    const groups = new Map();
    filtered.forEach(s => {
      const key = SOURCE_ORDER.includes(s.source) ? s.source : "other";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(s);
    });
    const order = SOURCE_ORDER.concat(["other"]);
    order.forEach(key => {
      const list = groups.get(key);
      if (!list || !list.length) return;
      list.sort(bySkillOrder);
      body.append(el("h4", "omc-sheet-group", SOURCE_LABEL[key] || "Other"));
      list.forEach(s => body.append(sheetRow(s)));
    });
  }
  function sheetRow(skill) {
    const row = el("button", "omc-skill-row");
    row.type = "button";
    const top = el("div", "omc-skill-top");
    top.append(el("span", "omc-skill-name", skill.name));
    if (skill.tier0) top.append(el("span", "omc-pop-badge", "tier 0"));
    row.append(top);
    if (skill.description) row.append(el("div", "omc-skill-desc", skill.description));
    row.addEventListener("click", () => activateSkill(skill, { closeSheet: true }));
    return row;
  }
  function trapSheetTab(e) {
    const box = state.sheet;
    const focusables = box.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),[tabindex]:not([tabindex="-1"])');
    if (!focusables.length) return;
    const first = focusables[0], last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  window.OMCCommands = { init, setSession, destroy };
})();
