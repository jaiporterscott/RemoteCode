"use strict";
const $ = s => document.querySelector(s);
const el = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; };
const rawUrl = p => "/api/files/raw?path=" + encodeURIComponent(p);
const IMG_RE = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif|tiff?)$/i;
const MODEL_RE = /\.(glb|gltf)$/i;
const LANG = { js: "javascript", mjs: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript", py: "python", rb: "ruby", go: "go", rs: "rust", java: "java", c: "c", h: "c", cpp: "cpp", cc: "cpp", hpp: "cpp", cs: "csharp", php: "php", sh: "bash", bash: "bash", zsh: "bash", json: "json", yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", xml: "xml", html: "xml", css: "css", scss: "scss", md: "markdown", sql: "sql", lua: "lua", swift: "swift", kt: "kotlin", dockerfile: "dockerfile", cshtml: "xml", razor: "xml" };
function human(n) { if (n == null) return ""; const u = ["B", "KB", "MB", "GB"]; let i = 0; n = +n; while (n >= 1024 && i < 3) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + u[i]; }

let cur = null, curReadonly = false, curChat = true;
let editing = false;                 // pause rail refresh during inline rename
let sessMap = new Map();
let sse = null, mode = "chat";
let term = null, fit = null, ws = null;
let seen = new Set();

async function j(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.text().catch(() => "")) || r.status);
  return r.status === 204 ? null : r.json();
}

/* ---------- rail ---------- */
async function refresh() {
  if (editing) return;                 // don't yank the rail out from under an inline edit
  let list = [];
  try { list = await j("/api/sessions"); } catch (e) { return; }
  sessMap = new Map(list.map(s => [s.id, s]));
  const ul = $("#sessionList"); ul.innerHTML = "";
  for (const s of list) {
    const li = el("li"); li.dataset.id = s.id;
    if (s.id === cur) li.classList.add("active");
    const st = s.status || "idle";
    li.append(el("span", "dot " + st));
    const m = el("div", "sess-meta");
    m.append(el("div", "sess-name", s.name));
    const tag = s.hub ? "" : (s.kind === "readonly" ? " · read-only" : " · ext");
    m.append(el("div", "sess-sub", `${s.agent} · ${st}${tag}${s.changed ? " · " + s.changed + " files" : ""}`));
    li.append(m);
    li.onclick = () => { selectSession(s.id); closeRail(); };
    li.oncontextmenu = e => { e.preventDefault(); showCtx(e.clientX, e.clientY, s); };
    let lp = null;
    li.addEventListener("touchstart", e => { const t = e.touches[0]; lp = setTimeout(() => { navigator.vibrate && navigator.vibrate(15); showCtx(t.clientX, t.clientY, s); }, 500); }, { passive: true });
    const clr = () => clearTimeout(lp);
    li.addEventListener("touchend", clr); li.addEventListener("touchmove", clr); li.addEventListener("touchcancel", clr);
    ul.append(li);
  }
  $("#railFoot").textContent = list.length ? `${list.length} session${list.length > 1 ? "s" : ""}` : "no sessions — tap ＋";
  const c = sessMap.get(cur);
  if (c) setStatus(c.status, c.waitingFor);
}
function setStatus(st, waitingFor) {
  const e = $("#curStatus");
  e.className = "status " + (st || "");
  e.textContent = st ? (st === "waiting" && waitingFor ? `waiting · ${waitingFor}` : st) : "";
}

/* ---------- select ---------- */
function selectSession(id) {
  if (sse) { sse.close(); sse = null; }
  teardownTerm();
  hideSelCard();
  clearAttachments();
  cur = id;
  const s = sessMap.get(id);
  curReadonly = s ? s.kind === "readonly" : false;
  curChat = s ? s.chat : true;
  $("#curName").textContent = s ? s.name : id;
  $("#curAgent").textContent = s ? s.agent : "";
  applyCaps();
  document.querySelectorAll("#sessionList li").forEach(li => li.classList.toggle("active", li.dataset.id === id));
  if (curChat) { showChat(); startStream(); loadFiles(); refreshClaudeBar(); }
  else { setFiles([]); showTerm(); }   // non-chat agents: terminal is the view
}
function applyCaps() {
  $("#killBtn").classList.toggle("hidden", curReadonly);
  $("#renameBtn").classList.toggle("hidden", curReadonly);
  $("#filesBtn").classList.toggle("hidden", !curChat);
  $("#modeBtn").classList.toggle("hidden", !curChat || curReadonly);
  $("#claudeBar").classList.toggle("hidden", !curChat || curReadonly);
  $("#chatKeys").classList.toggle("hidden", !curChat || curReadonly);
  const noInput = curReadonly || !curChat;
  $("#promptInput").disabled = noInput;
  $("#sendBtn").disabled = noInput;
  $("#attachBtn").disabled = noInput;
  $("#attachBtn").classList.toggle("hidden", !curChat);
  $("#promptInput").placeholder = curReadonly
    ? "Read-only — this session isn't in tmux, so it can't be driven from here."
    : "Type a prompt…  (Enter to send, Shift+Enter for newline)";
}

/* ---------- claude model / mode controls ---------- */
function setActiveMode(m) {
  document.querySelectorAll("#modeSeg .seg-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === m));
}
function cbFlash(t, err) {
  const e = $("#cbMsg"); e.textContent = t || ""; e.className = "cb-msg" + (err ? " err" : "");
  if (t) setTimeout(() => { if (e.textContent === t) { e.textContent = ""; e.className = "cb-msg"; } }, 2600);
}
async function refreshClaudeBar() {
  if (!cur || !curChat || curReadonly) return;
  setActiveMode(null); cbFlash("");
  try {
    const d = await j(`/api/sessions/${encodeURIComponent(cur)}/claude`);
    setActiveMode(d.mode);
    if (d.model) $("#modelSel").value = d.model;   // reflect the model actually running
  } catch (e) { /* non-claude or unreachable — bar simply shows no active mode */ }
}
$("#modelSel").addEventListener("change", async e => {
  if (!cur) return;
  const m = e.target.value;
  try {
    const d = await j(`/api/sessions/${encodeURIComponent(cur)}/model`,
      { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ model: m }) });
    cbFlash("model → " + m + (d && d.savesDefault ? " · saved as default" : ""));
  } catch (err) { cbFlash("model failed: " + err.message, true); }
});
document.querySelectorAll("#modeSeg .seg-btn").forEach(b => {
  b.addEventListener("click", async () => {
    if (!cur) return;
    const target = b.dataset.mode;
    cbFlash("switching…");
    try {
      const d = await j(`/api/sessions/${encodeURIComponent(cur)}/mode`,
        { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ mode: target }) });
      setActiveMode(d.mode);
      cbFlash(d.reached ? "mode → " + d.mode
        : "couldn't reach " + target + (d.mode ? " (still " + d.mode + ")" : ""), !d.reached);
    } catch (err) { cbFlash("mode failed: " + err.message, true); }
  });
});

/* ---------- interactive selection: on-screen keys + choice card ---------- */
const TERM_SEQ = { esc:"\x1b", up:"\x1b[A", down:"\x1b[B", left:"\x1b[D", right:"\x1b[C",
                   tab:"\t", btab:"\x1b[Z", enter:"\r", ctrlc:"\x03" };
function sendTermSeq(seq) { if (ws && ws.readyState === 1) ws.send(seq); if (term) term.focus(); }
document.querySelectorAll("#termKeys [data-k]").forEach(b => {
  b.addEventListener("click", () => { const s = TERM_SEQ[b.dataset.k]; if (s != null) sendTermSeq(s); });
});
document.querySelectorAll("#chatKeys [data-k]").forEach(b => {
  b.addEventListener("click", async () => {
    if (!cur || curReadonly) return;
    try {
      await j(`/api/sessions/${encodeURIComponent(cur)}/key`,
        { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ key: b.dataset.k }) });
    } catch (e) { /* transient — the poll re-syncs */ }
    setTimeout(refreshPromptState, 250);
  });
});

let selSig = "";
function hideSelCard() { selSig = ""; const c = $("#selCard"); c.classList.add("hidden"); c.innerHTML = ""; }
async function refreshPromptState() {
  if (!cur || !curChat || curReadonly || mode !== "chat") { hideSelCard(); return; }
  let d;
  try { d = await j(`/api/sessions/${encodeURIComponent(cur)}/prompt-state`); }
  catch (e) { hideSelCard(); return; }
  if (!d || !d.waiting || !d.options || !d.options.length) { hideSelCard(); return; }
  const sig = (d.title || "") + "‖" + d.options.map(o => o.n + ":" + o.label + (o.selected ? "*" : "")).join(",");
  if (sig === selSig) return;                       // unchanged — don't rebuild (keeps taps responsive)
  selSig = sig;
  const c = $("#selCard"); c.innerHTML = "";
  c.append(el("div", "sel-hint", "Waiting for your choice"));
  if (d.title) c.append(el("div", "sel-title", d.title));
  const wrap = el("div", "sel-opts");
  d.options.forEach(o => {
    const btn = el("button", "sel-opt" + (o.selected ? " cur" : ""));
    btn.type = "button";
    btn.append(el("span", "sel-n", o.n));
    btn.append(el("span", "sel-l", o.label));
    btn.onclick = async () => {
      wrap.querySelectorAll("button").forEach(x => x.disabled = true);
      try {
        await j(`/api/sessions/${encodeURIComponent(cur)}/select`,
          { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ option: o.n }) });
      } catch (e) { wrap.querySelectorAll("button").forEach(x => x.disabled = false); }
      selSig = "";                                  // force rebuild on next poll
      setTimeout(refreshPromptState, 450);
    };
    wrap.append(btn);
  });
  c.append(wrap);
  c.classList.remove("hidden");
}
setInterval(refreshPromptState, 2500);

/* ---------- stream / chat ---------- */
function startStream() {
  seen = new Set();
  const chat = $("#chat"); chat.innerHTML = ""; chat.append(el("div", "empty", "connecting…"));
  sse = new EventSource(`/api/sessions/${encodeURIComponent(cur)}/stream`);
  sse.addEventListener("init", e => {
    const d = JSON.parse(e.data);
    if (typeof d.readonly === "boolean") { curReadonly = d.readonly; applyCaps(); }
    chat.innerHTML = ""; seen = new Set();   // full resync (also fires on SSE reconnect) — clear dedup so nothing is skipped as "seen"
    if (!d.items.length) chat.append(el("div", "empty", d.readonly ? "No messages in the recent transcript." : "No messages yet. Say hello 👋"));
    d.items.forEach(addItem); setFiles(d.files); scrollChat(true);
  });
  sse.addEventListener("append", e => {
    const d = JSON.parse(e.data);
    const emptyEl = chat.querySelector(".empty"); if (emptyEl) emptyEl.remove();
    const near = nearBottom(); d.items.forEach(addItem);
    if (d.files && d.files.length) mergeFiles(d.files);
    scrollChat(near);
  });
  sse.addEventListener("status", e => { const d = JSON.parse(e.data); setStatus(d.status, d.waitingFor); });
  sse.addEventListener("info", e => { chat.innerHTML = ""; chat.append(el("div", "empty", JSON.parse(e.data).message)); });
  sse.addEventListener("closed", () => setStatus("idle"));
}
function addItem(it) {
  if (it.uuid && seen.has(it.uuid)) return;
  if (it.uuid) seen.add(it.uuid);
  const m = el("div", "msg " + it.role);
  m.append(el("div", "role", it.role));
  if (it.text) { const b = el("div"); b.textContent = it.text; m.append(b); }
  if (it.chips && it.chips.length) {
    const wrap = el("div", "chips");
    for (const c of it.chips) wrap.append(chipEl(c));
    m.append(wrap);
  }
  if (it.outputs && it.outputs.length) {                // artifacts a command produced
    const wrap = el("div", "chips outs");
    for (const o of it.outputs) wrap.append(outChipEl(o));
    m.append(wrap);
  }
  $("#chat").append(m);
}
const MODEL_EXT_RE = /^(glb|gltf|obj|stl|ply|usdz)$/i;
function outChipEl(o) {
  const wrap = el("div");
  const isModel = MODEL_EXT_RE.test(o.ext || "");
  const isImg = IMG_RE.test(o.name || "");
  const e = el("div", "chip file out");
  e.append(el("span", "v", isModel ? "🧊 3D" : (isImg ? "🖼 view" : "open")));
  e.append(el("span", "p", o.name));
  if (o.size) e.append(el("span", "sz", human(o.size)));
  e.onclick = () => preview(o.path);
  wrap.append(e);
  if (isImg) {                                          // thumbnail for image outputs
    const img = el("img", "chip-thumb"); img.src = rawUrl(o.path); img.loading = "lazy";
    img.onclick = () => preview(o.path); wrap.append(img);
  }
  return wrap;
}
function chipEl(c) {
  const wrap = el("div");
  const e = el("div", "chip " + (c.path ? "file " : "") + (c.ok ? "" : "err"));
  e.append(el("span", "v", c.verb));
  if (c.path) {
    e.append(el("span", "p", c.path.split("/").pop()));
    e.onclick = () => preview(c.path);            // any file (edited OR read) is clickable
  } else if (c.detail && c.tool !== "Bash") {
    e.append(el("span", "p", c.detail));
  }
  if (!c.ok) e.append(el("span", null, "⚠"));
  wrap.append(e);
  if (c.tool === "Bash" && c.detail) {                 // full command — collapsed by default, click to expand
    const box = el("div", "cmdbox collapsed");
    const head = el("button", "cmd-toggle"); head.type = "button";
    head.append(el("span", "caret"));                  // CSS-drawn triangle (font-independent)
    head.append(el("span", "cmd-prev", c.detail.split("\n")[0]));
    const pre = el("pre", "cmd"); pre.textContent = c.detail;
    head.onclick = () => box.classList.toggle("collapsed");
    box.append(head); box.append(pre); wrap.append(box);
  }
  if (c.path && IMG_RE.test(c.path)) {                 // images auto-preview
    const img = el("img", "chip-thumb"); img.src = rawUrl(c.path); img.loading = "lazy";
    img.onclick = () => preview(c.path); wrap.append(img);
  }
  return wrap;
}
function scrollChat(f) { if (f) { const c = $("#chat"); c.scrollTop = c.scrollHeight; } }
function nearBottom() { const c = $("#chat"); return c.scrollHeight - c.scrollTop - c.clientHeight < 120; }

/* ---------- files ---------- */
let filesMap = new Map();
function setFiles(files) { filesMap = new Map((files || []).map(f => [f.path, f])); renderFiles(); }
function mergeFiles(files) { for (const f of files) filesMap.set(f.path, f); renderFiles(); }
async function loadFiles() { try { setFiles(await j(`/api/sessions/${encodeURIComponent(cur)}/files`)); } catch (e) {} }
function renderFiles() {
  const arr = [...filesMap.values()].sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
  $("#filesCount").textContent = arr.length;
  const badge = $("#filesCountBadge"); if (badge) badge.textContent = arr.length;
  const ul = $("#filesList"); ul.innerHTML = "";
  if (!arr.length) { ul.append(el("li", null, "nothing yet")); return; }
  for (const f of arr) {
    const li = el("li");
    li.dataset.path = f.path;
    if (f.path === activePreviewPath) li.classList.add("active");
    if (f.exists && IMG_RE.test(f.name)) { const img = el("img", "thumb"); img.src = rawUrl(f.path); img.loading = "lazy"; li.append(img); }
    else { li.append(el("div", "ficon", MODEL_RE.test(f.name) ? "🧊" : "📄")); }
    const meta = el("div", "sess-meta");
    meta.append(el("div", "fn", f.name));
    meta.append(el("div", "fp", `${f.verb} · ${human(f.size)}${f.exists ? "" : " · deleted"} · ${f.path}`));
    li.append(meta);
    li.onclick = () => preview(f.path);
    ul.append(li);
  }
}

/* ---------- preview (any type) ---------- */
let pv = null, pvPath = null, pvRendered = false, activePreviewPath = null;
const isHtml = n => /\.(html?)$/i.test(n || "");
function applyActiveFile() {
  document.querySelectorAll("#filesList li").forEach(li =>
    li.classList.toggle("active", li.dataset.path === activePreviewPath));
}
function preview(path) {
  pvPath = path; pvRendered = false; activePreviewPath = path;
  $("#previewName").textContent = path;
  $("#previewOpen").href = rawUrl(path);
  $("#previewRender").classList.add("hidden");
  $("#previewBody").innerHTML = "loading…";
  $("#previewModal").classList.remove("hidden");   // open the dedicated preview lightbox
  applyActiveFile();
  j("/api/files/preview?path=" + encodeURIComponent(path)).then(d => { pv = d; renderPreview(); })
    .catch(e => { $("#previewBody").innerHTML = ""; $("#previewBody").append(el("div", "binary-card", "error: " + e.message)); });
}
function closePreview() {
  $("#previewModal").classList.add("hidden");
  $("#previewModal .modal-box").classList.remove("expanded");
  $("#previewBody").innerHTML = "";                 // stop any playing model-viewer / iframe
  activePreviewPath = null; applyActiveFile();
}
function renderPreview() {
  const d = pv, body = $("#previewBody"); body.innerHTML = "";
  $("#previewName").textContent = (d.name || pvPath) + (d.size != null ? "  ·  " + human(d.size) : "");
  const htmlish = d.kind === "text" && isHtml(d.name);
  $("#previewRender").classList.toggle("hidden", !htmlish);          // raw/preview for HTML & source
  $("#previewRender").textContent = pvRendered ? "Source" : "Rendered";
  if (!d.exists) { body.append(el("div", "binary-card", "file no longer exists")); return; }
  if (htmlish && pvRendered) {
    const f = el("iframe", "preview-frame"); f.src = rawUrl(pvPath); f.setAttribute("sandbox", ""); body.append(f); return;
  }
  if (d.kind === "image") {
    const img = el("img", "preview-img"); img.src = rawUrl(pvPath); body.append(img);
  } else if (d.kind === "model") {
    const mv = document.createElement("model-viewer");
    mv.className = "preview-model"; mv.setAttribute("src", rawUrl(pvPath));
    mv.setAttribute("camera-controls", ""); mv.setAttribute("auto-rotate", "");
    mv.setAttribute("shadow-intensity", "1"); mv.setAttribute("exposure", "1");
    body.append(mv);
  } else if (d.kind === "text") {
    if (d.truncated) body.append(el("div", "preview-note", `Large file — showing the first ${human((d.content || "").length)} of ${human(d.size)}.`));
    const pre = el("pre"), code = el("code");
    code.textContent = d.content || "";
    const lang = LANG[(d.ext || "").replace(".", "")] || (/^\.?dockerfile$/i.test(d.name) ? "dockerfile" : null);
    if (lang && window.hljs && hljs.getLanguage(lang)) code.className = "language-" + lang;
    pre.append(code); body.append(pre);
    try { window.hljs && hljs.highlightElement(code); } catch (e) {}
  } else {
    const card = el("div", "binary-card");
    card.append(el("div", "big", "📦"));
    card.append(el("div", null, `${d.kind === "binary" ? "Binary file" : (d.content || "Not previewable")} · ${human(d.size)}`));
    const a = el("a", "btn primary", "Open / download"); a.href = rawUrl(pvPath); a.target = "_blank";
    const w = el("div"); w.style.marginTop = "12px"; w.append(a); card.append(w);
    body.append(card);
  }
}
$("#previewRender").onclick = () => { pvRendered = !pvRendered; renderPreview(); };
$("#previewExpand").onclick = () => $("#previewModal .modal-box").classList.toggle("expanded");
$("#previewClose").onclick = closePreview;
// click the dimmed backdrop (outside the box) to dismiss
$("#previewModal").addEventListener("click", e => { if (e.target === $("#previewModal")) closePreview(); });
// Esc closes the preview (only when it's open)
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !$("#previewModal").classList.contains("hidden")) closePreview();
});

/* ---------- settings (agents) ---------- */
let cfgAgents = [];
$("#settingsBtn").onclick = async () => {
  const d = await j("/api/settings");
  cfgAgents = d.agents.map(a => ({ ...a }));
  renderSettings(d);
  $("#settingsModal").classList.remove("hidden");
};
$("#settingsClose").onclick = () => $("#settingsModal").classList.add("hidden");
$("#settingsAdd").onclick = () => { cfgAgents.push({ key: "", label: "", cmd: "", provider: null, detect: true, installed: false }); renderSettings(); };
$("#settingsSave").onclick = async () => {
  try {
    await j("/api/settings/agents", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ agents: cfgAgents }) });
    $("#settingsModal").classList.add("hidden");
  } catch (e) { alert("Save failed: " + e.message); }
};
function renderSettings(meta) {
  const body = $("#settingsBody"); body.innerHTML = "";
  if (meta) body.append(el("div", "preview-note",
    `Auth ${meta.authRequired ? "ON" : "OFF"} · sudo ${meta.sudo ? "ON" : "OFF"}. ` +
    `Set REMOTECODE_PASSWORD to require login, REMOTECODE_SUDO=1 to control root-owned tmux. ` +
    `"installed" = found on PATH. Provider "claude" enables the rich chat view.`));
  const wrap = el("div", "cfg-wrap");
  const hdr = el("div", "cfg-row cfg-head");
  ["key", "label", "command", "provider", "detect", ""].forEach(h => hdr.append(el("div", null, h)));
  wrap.append(hdr);
  cfgAgents.forEach((a, i) => wrap.append(agentRow(a, i)));
  body.append(wrap);
}
function agentRow(a, i) {
  const row = el("div", "cfg-row");
  row.append(cfgInput(a.key, v => a.key = v, "key"));
  row.append(cfgInput(a.label, v => a.label = v, "Label"));
  row.append(cfgInput(a.cmd, v => a.cmd = v, "command"));
  const sel = document.createElement("select");
  [["", "terminal"], ["claude", "claude (chat)"]].forEach(([val, txt]) => { const o = document.createElement("option"); o.value = val; o.textContent = txt; if ((a.provider || "") === val) o.selected = true; sel.append(o); });
  sel.onchange = () => a.provider = sel.value || null; row.append(sel);
  const chk = document.createElement("input"); chk.type = "checkbox"; chk.checked = a.detect !== false;
  chk.onchange = () => a.detect = chk.checked;
  const chkWrap = el("div"); chkWrap.style.textAlign = "center"; chkWrap.append(chk); row.append(chkWrap);
  const del = el("button", "btn danger icon", "✕"); del.onclick = () => { cfgAgents.splice(i, 1); renderSettings(); };
  row.append(del);
  return row;
}
function cfgInput(val, on, ph) { const i = document.createElement("input"); i.className = "cfg-in"; i.value = val || ""; i.placeholder = ph || ""; i.oninput = () => on(i.value); return i; }

/* ---------- prompt ---------- */
$("#promptForm").addEventListener("submit", async e => {
  e.preventDefault();
  if (curReadonly || !curChat || !cur) return;
  const ta = $("#promptInput"); const text = ta.value.trim();
  const ready = pendingAttachments.filter(a => a.path);       // only fully-uploaded files
  if (!text && !ready.length) return;
  if (pendingAttachments.some(a => a.uploading)) return;      // wait for uploads to finish
  ta.value = ""; ta.style.height = "auto";
  // paths go on one line (send-keys can't do multi-line), the agent reads them by path
  const list = ready.map(a => a.path).join(" ");
  const sent = list ? (text ? `${text} — files: ${list}` : `Please look at these uploaded files: ${list}`) : text;
  clearAttachments();
  const emptyEl = $("#chat").querySelector(".empty"); if (emptyEl) emptyEl.remove();
  addItem({ role: "user", text: sent, chips: [], uuid: "opt-" + Date.now() }); scrollChat(true);
  try { await j(`/api/sessions/${encodeURIComponent(cur)}/prompt`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: sent }) }); }
  catch (err) { addItem({ role: "assistant", text: "⚠ send failed: " + err.message, chips: [] }); }
});

/* ---------- file upload (attach button · drag-drop · paste image) ---------- */
let pendingAttachments = [];   // {id, name, size, path|null, uploading, error}
let attachSeq = 0;
function renderAttachBar() {
  const bar = $("#attachBar");
  bar.classList.toggle("hidden", pendingAttachments.length === 0);
  bar.innerHTML = "";
  for (const a of pendingAttachments) {
    const chip = el("div", "attach-chip" + (a.uploading ? " uploading" : "") + (a.error ? " err" : ""));
    chip.append(el("span", "an", a.name));
    chip.append(el("span", "asz", a.error ? "failed" : (a.uploading ? "uploading…" : human(a.size))));
    const x = el("button", "ax", "✕"); x.type = "button";
    x.onclick = () => { pendingAttachments = pendingAttachments.filter(p => p.id !== a.id); renderAttachBar(); };
    chip.append(x);
    bar.append(chip);
  }
}
function clearAttachments() { pendingAttachments = []; renderAttachBar(); }
async function uploadFile(file) {
  if (!cur || curReadonly || !curChat) return;
  const a = { id: ++attachSeq, name: file.name || "pasted", size: file.size, path: null, uploading: true, error: false };
  pendingAttachments.push(a); renderAttachBar();
  try {
    const r = await fetch(`/api/sessions/${encodeURIComponent(cur)}/upload?name=${encodeURIComponent(a.name)}`,
      { method: "POST", body: file });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
    const d = await r.json();
    a.path = d.path; a.name = d.name; a.size = d.size; a.uploading = false;
  } catch (e) { a.uploading = false; a.error = true; }
  renderAttachBar();
}
function uploadFiles(files) { for (const f of files) if (f) uploadFile(f); }
$("#attachBtn").addEventListener("click", () => { if (cur && curChat && !curReadonly) $("#fileInput").click(); });
$("#fileInput").addEventListener("change", e => { uploadFiles(e.target.files); e.target.value = ""; });
$("#promptInput").addEventListener("paste", e => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); uploadFiles(files); }
});
(() => {
  const form = $("#promptForm"), chatWrap = $("#chatWrap");
  const over = e => { e.preventDefault(); form.classList.add("dragover"); };
  const leave = () => form.classList.remove("dragover");
  ["dragenter", "dragover"].forEach(ev => chatWrap.addEventListener(ev, over));
  ["dragleave", "dragend"].forEach(ev => chatWrap.addEventListener(ev, leave));
  chatWrap.addEventListener("drop", e => {
    e.preventDefault(); leave();
    if (e.dataTransfer?.files?.length) uploadFiles(e.dataTransfer.files);
  });
})();
$("#promptInput").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#promptForm").requestSubmit(); } });
$("#promptInput").addEventListener("input", e => { e.target.style.height = "auto"; e.target.style.height = Math.min(180, e.target.scrollHeight) + "px"; });

/* ---------- new (agent → project) ---------- */
$("#newBtn").onclick = async () => {
  const agents = await j("/api/agents");
  $("#newTitle").textContent = "New session — pick an agent";
  const body = $("#newBody"); body.innerHTML = "";
  const ul = el("ul", "agent-list");
  for (const a of agents) {
    const li = el("li", a.installed ? "" : "gone");
    const L = el("div"); L.append(el("div", "pk", a.label)); L.append(el("div", "pp", a.chat ? "rich chat + files" : "terminal"));
    li.append(L);
    li.append(el("span", "badge", a.installed ? "installed" : "not found"));
    if (a.installed) li.onclick = () => pickProject(a.key);
    ul.append(li);
  }
  body.append(ul);
  $("#newModal").classList.remove("hidden");
};
async function pickProject(agentKey) {
  const projects = await j("/api/projects");
  $("#newTitle").textContent = "New session — pick a directory";
  const body = $("#newBody"); body.innerHTML = "";
  const ul = el("ul", "project-list");
  for (const p of projects) {
    const li = el("li", p.exists ? "" : "gone");
    const L = el("div"); L.append(el("div", "pk", p.key)); L.append(el("div", "pp", p.path));
    li.append(L);
    li.onclick = async () => {
      $("#newModal").classList.add("hidden");
      const r = await j("/api/sessions", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ agent: agentKey, project: p.key }) });
      await refresh(); selectSession(r.name);
    };
    ul.append(li);
  }
  body.append(ul);
}

/* ---------- rename / kill / context menu ---------- */
$("#renameBtn").onclick = () => { if (cur && !curReadonly) startRename(cur); };
$("#killBtn").onclick = () => { if (cur && !curReadonly) doKill(cur, (sessMap.get(cur) || {}).name || cur); };

async function doKill(id, name) {
  if (!confirm(`Kill ${name}? This ends the session.`)) return;
  try { await j(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }); }
  catch (e) { alert("Kill failed: " + e.message); return; }
  if (id === cur) {
    if (sse) sse.close(); teardownTerm();
    cur = null; $("#curName").textContent = "—"; $("#curAgent").textContent = ""; setStatus("");
    $("#chat").innerHTML = ""; $("#chat").append(el("div", "empty", "Session ended. Pick or create one."));
    setFiles([]);
  }
  refresh();
}

function ensureCtx() {
  let m = $("#ctxMenu");
  if (!m) {
    m = el("div", "ctx hidden"); m.id = "ctxMenu"; document.body.append(m);
    document.addEventListener("click", hideCtx);
    document.addEventListener("scroll", hideCtx, true);
    window.addEventListener("resize", hideCtx);
    document.addEventListener("keydown", e => { if (e.key === "Escape") hideCtx(); });
  }
  return m;
}
function showCtx(x, y, s) {
  const m = ensureCtx(); m.innerHTML = "";
  const add = (label, fn, cls) => { const b = el("button", "ctx-item" + (cls ? " " + cls : ""), label); b.onmousedown = e => e.stopPropagation(); b.onclick = e => { e.stopPropagation(); hideCtx(); fn(); }; m.append(b); };
  add("✎  Rename", () => startRename(s.id));
  if (s.chat && s.kind !== "readonly") add("⌨  Open terminal", () => { selectSession(s.id); showTerm(); });
  if (s.kind !== "readonly") add("✕  Kill", () => doKill(s.id, s.name), "danger");
  if (s.kind === "readonly") add("read-only — view only", () => {}, "muted");
  m.classList.remove("hidden");
  const mw = 190, mh = m.offsetHeight || 130;
  m.style.left = Math.max(6, Math.min(x, innerWidth - mw - 8)) + "px";
  m.style.top = Math.max(6, Math.min(y, innerHeight - mh - 8)) + "px";
}
function hideCtx() { const m = $("#ctxMenu"); if (m) m.classList.add("hidden"); }

function startRename(id) {
  const s = sessMap.get(id);
  const li = document.querySelector(`#sessionList li[data-id="${(window.CSS && CSS.escape) ? CSS.escape(id) : id}"]`);
  if (!s || !li) return;
  if (s.kind === "readonly") { alert("Read-only session — it isn't in a tmux RemoteCode can reach, so it can't be renamed."); return; }
  editing = true;
  const nameEl = li.querySelector(".sess-name");
  const input = el("input", "rename-in"); input.value = s.name;
  nameEl.replaceWith(input); input.focus(); input.select();
  input.onclick = e => e.stopPropagation();
  let done = false;
  const finish = async commit => {
    if (done) return; done = true; editing = false;
    const v = input.value.trim();
    if (commit && v && v !== s.name) {
      try {
        const r = await j(`/api/sessions/${encodeURIComponent(id)}/rename`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name: v }) });
        if (cur === id) cur = r.name;
        await refresh(); if (cur === r.name) selectSession(r.name);
        return;
      } catch (e) { alert("Rename failed: " + e.message); }
    }
    refresh();
  };
  input.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); finish(true); } else if (e.key === "Escape") { e.preventDefault(); finish(false); } };
  input.onblur = () => finish(true);
}

/* ---------- terminal ---------- */
$("#modeBtn").onclick = () => { mode === "chat" ? showTerm() : showChat(); };
function showChat() { mode = "chat"; $("#chatWrap").classList.remove("hidden"); $("#termWrap").classList.add("hidden"); $("#modeBtn").textContent = "⌨ Terminal"; teardownTerm(); refreshPromptState(); }
function showTerm() {
  if (!cur) return;
  mode = "term"; $("#chatWrap").classList.add("hidden"); $("#termWrap").classList.remove("hidden"); $("#modeBtn").textContent = "💬 Chat";
  term = new Terminal({ cursorBlink: true, fontSize: 13, theme: { background: "#000000" } });
  fit = new FitAddon.FitAddon(); term.loadAddon(fit);
  term.open($("#term")); fit.fit();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/term/${encodeURIComponent(cur)}`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => sendResize();
  ws.onmessage = ev => term.write(typeof ev.data === "string" ? ev.data : new Uint8Array(ev.data));
  ws.onclose = () => term && term.write("\r\n[disconnected]\r\n");
  term.onData(d => ws && ws.readyState === 1 && ws.send(d));
  window.addEventListener("resize", onResize);
}
function onResize() { if (fit) { fit.fit(); sendResize(); } }
function sendResize() { if (ws && ws.readyState === 1 && term) ws.send(`\x00resize:${term.cols},${term.rows}`); }
function teardownTerm() {
  window.removeEventListener("resize", onResize);
  if (ws) { try { ws.close(); } catch (e) {} ws = null; }
  if (term) { try { term.dispose(); } catch (e) {} term = null; fit = null; }
  $("#term").innerHTML = "";
}

/* ---------- modals / mobile ---------- */
$("#filesBtn").onclick = () => $("#filesPanel").classList.toggle("hidden");
$("#filesClose").onclick = () => $("#filesPanel").classList.add("hidden");
$("#newClose").onclick = () => $("#newModal").classList.add("hidden");
$("#menuBtn").onclick = () => { $("#rail").classList.add("open"); $("#rail-scrim").classList.remove("hidden"); };
$("#rail-scrim").onclick = closeRail;
function closeRail() { $("#rail").classList.remove("open"); $("#rail-scrim").classList.add("hidden"); }

refresh();
setInterval(refresh, 3000);
