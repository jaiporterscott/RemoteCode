"use strict";
const $ = s => document.querySelector(s);
const el = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; };
const rawUrl = p => "/api/files/raw?path=" + encodeURIComponent(p);
const IMG_RE = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif|tiff?)$/i;
const MODEL_RE = /\.(glb|gltf)$/i;
const LANG = { js: "javascript", mjs: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript", py: "python", rb: "ruby", go: "go", rs: "rust", java: "java", c: "c", h: "c", cpp: "cpp", cc: "cpp", hpp: "cpp", cs: "csharp", php: "php", sh: "bash", bash: "bash", zsh: "bash", json: "json", yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", xml: "xml", html: "xml", css: "css", scss: "scss", md: "markdown", sql: "sql", lua: "lua", swift: "swift", kt: "kotlin", dockerfile: "dockerfile", cshtml: "xml", razor: "xml" };
function human(n) { if (n == null) return ""; const u = ["B", "KB", "MB", "GB"]; let i = 0; n = +n; while (n >= 1024 && i < 3) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + u[i]; }

let cur = null, curReadonly = false, curChat = true;
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
  cur = id;
  const s = sessMap.get(id);
  curReadonly = s ? s.kind === "readonly" : false;
  curChat = s ? s.chat : true;
  $("#curName").textContent = s ? s.name : id;
  $("#curAgent").textContent = s ? s.agent : "";
  applyCaps();
  document.querySelectorAll("#sessionList li").forEach(li => li.classList.toggle("active", li.dataset.id === id));
  if (curChat) { showChat(); startStream(); loadFiles(); }
  else { setFiles([]); showTerm(); }   // non-chat agents: terminal is the view
}
function applyCaps() {
  $("#killBtn").classList.toggle("hidden", curReadonly);
  $("#renameBtn").classList.toggle("hidden", curReadonly);
  $("#filesBtn").classList.toggle("hidden", !curChat);
  $("#modeBtn").classList.toggle("hidden", !curChat || curReadonly);
  const noInput = curReadonly || !curChat;
  $("#promptInput").disabled = noInput;
  $("#sendBtn").disabled = noInput;
  $("#promptInput").placeholder = curReadonly
    ? "Read-only — this session isn't in tmux, so it can't be driven from here."
    : "Type a prompt…  (Enter to send, Shift+Enter for newline)";
}

/* ---------- stream / chat ---------- */
function startStream() {
  seen = new Set();
  const chat = $("#chat"); chat.innerHTML = ""; chat.append(el("div", "empty", "connecting…"));
  sse = new EventSource(`/api/sessions/${encodeURIComponent(cur)}/stream`);
  sse.addEventListener("init", e => {
    const d = JSON.parse(e.data);
    if (typeof d.readonly === "boolean") { curReadonly = d.readonly; applyCaps(); }
    chat.innerHTML = "";
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
  $("#chat").append(m);
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
  if (c.tool === "Bash" && c.detail) {                 // full command, untruncated
    const pre = el("pre", "cmd"); pre.textContent = c.detail; wrap.append(pre);
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
  const ul = $("#filesList"); ul.innerHTML = "";
  if (!arr.length) { ul.append(el("li", null, "nothing yet")); return; }
  for (const f of arr) {
    const li = el("li");
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
let pv = null, pvPath = null, pvRendered = false;
const isHtml = n => /\.(html?)$/i.test(n || "");
function preview(path) {
  pvPath = path; pvRendered = false;
  $("#previewName").textContent = path;
  $("#previewOpen").href = rawUrl(path);
  $("#previewBox").classList.remove("expanded");
  $("#previewRender").classList.add("hidden");
  $("#previewBody").innerHTML = "loading…";
  $("#preview").classList.remove("hidden");
  j("/api/files/preview?path=" + encodeURIComponent(path)).then(d => { pv = d; renderPreview(); })
    .catch(e => { $("#previewBody").innerHTML = ""; $("#previewBody").append(el("div", "binary-card", "error: " + e.message)); });
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
$("#previewExpand").onclick = () => $("#previewBox").classList.toggle("expanded");
$("#previewClose").onclick = () => $("#preview").classList.add("hidden");

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
  if (curReadonly || !curChat) return;
  const ta = $("#promptInput"); const text = ta.value;
  if (!text.trim() || !cur) return;
  ta.value = ""; ta.style.height = "auto";
  const emptyEl = $("#chat").querySelector(".empty"); if (emptyEl) emptyEl.remove();
  addItem({ role: "user", text, chips: [], uuid: "opt-" + Date.now() }); scrollChat(true);
  try { await j(`/api/sessions/${encodeURIComponent(cur)}/prompt`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text }) }); }
  catch (err) { addItem({ role: "assistant", text: "⚠ send failed: " + err.message, chips: [] }); }
});
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

/* ---------- rename / kill ---------- */
$("#renameBtn").onclick = async () => {
  if (!cur || curReadonly) return;
  const s = sessMap.get(cur);
  const name = prompt("Rename session:", s ? s.name : cur);
  if (!name || name === cur) return;
  try {
    const r = await j(`/api/sessions/${encodeURIComponent(cur)}/rename`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name }) });
    await refresh(); selectSession(r.name);
  } catch (e) { alert("Rename failed: " + e.message); }
};
$("#killBtn").onclick = async () => {
  if (!cur || curReadonly || !confirm(`Kill ${cur}? This ends the session.`)) return;
  await j(`/api/sessions/${encodeURIComponent(cur)}`, { method: "DELETE" });
  if (sse) sse.close(); teardownTerm();
  cur = null; $("#curName").textContent = "—"; $("#curAgent").textContent = ""; setStatus("");
  $("#chat").innerHTML = ""; $("#chat").append(el("div", "empty", "Session ended. Pick or create one."));
  setFiles([]); refresh();
};

/* ---------- terminal ---------- */
$("#modeBtn").onclick = () => { mode === "chat" ? showTerm() : showChat(); };
function showChat() { mode = "chat"; $("#chatWrap").classList.remove("hidden"); $("#termWrap").classList.add("hidden"); $("#modeBtn").textContent = "⌨ Terminal"; teardownTerm(); }
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
