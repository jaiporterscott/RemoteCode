"""
RemoteCode — a web control panel for AI coding agents running in tmux.

Launch/list/rename/kill sessions for any agent (Claude Code, Aider, Codex,
OpenCode, Goose, Gemini, or a plain shell), type prompts, and open a live
terminal for any of them. When the agent is Claude Code, a rich chat +
files-changed view is rendered from Claude's own transcript.

Binds 127.0.0.1:7070 by default with HTTP basic-auth built in.
"""
import os, re, time, json, glob, stat, base64, asyncio, secrets, subprocess, threading
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body, Request
from fastapi.responses import (StreamingResponse, FileResponse, HTMLResponse,
                               Response, JSONResponse)
from fastapi.staticfiles import StaticFiles

import config
import claude_data as cd
import recover
import minio_sync

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
RENAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp",
              ".ico", ".avif", ".tif", ".tiff"}

app = FastAPI(title="RemoteCode")


# ---- auth (pure-ASGI so it also covers the websocket handshake) ---------

class BasicAuthMiddleware:
    def __init__(self, app, user, password):
        self.app = app
        self.user = user
        self.password = password

    async def __call__(self, scope, receive, send):
        if not self.password or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        auth = dict(scope.get("headers") or []).get(b"authorization", b"")
        if self._ok(auth):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"www-authenticate", b'Basic realm="RemoteCode"'),
            (b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body", "body": b"Authentication required"})

    def _ok(self, auth: bytes) -> bool:
        if not auth.startswith(b"Basic "):
            return False
        try:
            u, p = base64.b64decode(auth[6:]).decode("utf-8", "replace").split(":", 1)
        except Exception:
            return False
        return (secrets.compare_digest(u, self.user)
                and secrets.compare_digest(p, self.password))


app.add_middleware(BasicAuthMiddleware, user=config.AUTH_USER,
                   password=config.get_password())


# ---- tmux + /proc helpers ----------------------------------------------

def _needs_sudo(socket) -> bool:
    return bool(socket) and not os.access(socket, os.R_OK | os.W_OK) and config.sudo_enabled()


def _tmux_argv(socket, *args) -> list:
    pre = ["sudo", "-n"] if _needs_sudo(socket) else []
    return pre + ["tmux"] + (["-S", socket] if socket else []) + list(args)


def tmux(*args, socket=None):
    return subprocess.run(_tmux_argv(socket, *args), capture_output=True,
                          text=True, timeout=10)


def _tmux_env_socket(pid: int):
    """The socket a running process is attached to, from its TMUX env var."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            for kv in f.read().split(b"\0"):
                if kv.startswith(b"TMUX="):
                    return kv[5:].split(b",")[0].decode() or None
    except Exception:
        return None
    return None


def tmux_sockets() -> list:
    """Every tmux server socket we can use. Found on the filesystem AND from the
    TMUX env of running agent processes (which reveals sockets in dirs we can't
    list, e.g. a root `sudo tmux`). Sockets we can't access directly are included
    only when REMOTECODE_SUDO is enabled (we then drive them via `sudo -n tmux`)."""
    socks = set()
    for s in glob.glob("/tmp/tmux-*/*"):
        try:
            if stat.S_ISSOCK(os.stat(s).st_mode):
                socks.add(s)
        except OSError:
            continue
    for info in cd.live_sessions().values():
        s = _tmux_env_socket(info["pid"])
        if s:
            socks.add(s)
    usable = [s for s in socks if os.access(s, os.R_OK | os.W_OK) or config.sudo_enabled()]
    return sorted(usable) or [None]      # None → tmux's own default socket


def tmux_panes() -> dict:
    """name -> (socket, pane_pid) across ALL accessible tmux servers."""
    out = {}
    for sock in tmux_sockets():
        r = tmux("list-panes", "-a", "-F", "#{session_name}|#{pane_pid}", socket=sock)
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            if "|" in line:
                n, pid = line.rsplit("|", 1)
                try:
                    out[n] = (sock, int(pid))
                except ValueError:
                    pass
    return out


def socket_of(name: str):
    return tmux_panes().get(name, (None, None))[0]


def _ppid_map() -> dict:
    m = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                data = f.read()
            rest = data[data.rfind(")") + 2:].split()  # comm may contain ')'
            m[int(pid)] = int(rest[1])
        except Exception:
            continue
    return m


def _children_map(ppm: dict) -> dict:
    ch = {}
    for pid, ppid in ppm.items():
        ch.setdefault(ppid, []).append(pid)
    return ch


def _tree(root: int, ch: dict) -> set:
    seen, stack = {root}, [root]
    while stack:
        p = stack.pop()
        for c in ch.get(p, []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def _comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except Exception:
        return ""


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:
        return ""


# ---- session discovery --------------------------------------------------

def _session_agent(name: str, comms: set):
    """Which agent this tmux session is running (by rc- name, else by process)."""
    if name.startswith(config.RC_PREFIX):
        key = name[len(config.RC_PREFIX):].split("-")[0]
        a = config.agent_by_key(key)
        if a:
            return a
    for a in config.load_agents():
        if config.agent_comm(a) in comms:
            return a
    return None


def discover_sessions() -> list:
    """Every tmux session running a known agent (or created by RemoteCode)."""
    panes = tmux_panes()
    ppm = _ppid_map()
    ch = _children_map(ppm)
    pid2sid = {info["pid"]: (sid, info) for sid, info in cd.live_sessions().items()}
    comm_set = {config.agent_comm(a) for a in config.load_agents()
                if a.get("detect", True)}
    out = []
    for name, (sock, root) in panes.items():
        tree = _tree(root, ch)
        comms = {_comm(p) for p in tree}
        agent_pids = [p for p in tree if _comm(p) in comm_set]
        is_hub = name.startswith(config.RC_PREFIX)
        if not agent_pids and not is_hub:
            continue
        if any("--remote-control" in _cmdline(p) for p in agent_pids):
            continue                                   # infra loop, not chattable
        agent = _session_agent(name, comms)
        provider = agent["provider"] if agent else None
        sid, info = None, None
        if provider == "claude":
            for p in tree:
                if p in pid2sid:
                    sid, info = pid2sid[p]
                    break
        out.append({"name": name, "socket": sock, "sid": sid, "info": info,
                    "hub": is_hub, "agent": agent["label"] if agent else "agent",
                    "agentKey": agent["key"] if agent else None,
                    "provider": provider})
    out.sort(key=lambda s: (not s["hub"], s["name"]))
    return out


def resolve_sid(name: str):
    for s in discover_sessions():
        if s["name"] == name:
            return s["sid"], s["info"]
    return None, None


def all_sessions() -> list:
    """tmux agent sessions + live Claude sessions not in tmux (read-only)."""
    tmux_list = discover_sessions()
    shown = {s["sid"] for s in tmux_list if s["sid"]}
    out = [{**s, "id": s["name"], "kind": "tmux"} for s in tmux_list]
    for sid, info in cd.live_sessions().items():
        if sid in shown:
            continue
        out.append({"id": sid, "name": info.get("name") or sid[:8], "sid": sid,
                    "socket": None, "info": info, "hub": False, "agent": "Claude Code",
                    "agentKey": "claude", "provider": "claude", "kind": "readonly"})
    order = {"tmux": 0, "readonly": 1}
    out.sort(key=lambda s: (order[s["kind"]], not s["hub"], s["name"]))
    return out


def resolve(sess: str) -> dict:
    if not SAFE_NAME.match(sess):
        raise HTTPException(404, "no such session")
    for s in all_sessions():
        if s["id"] == sess:
            return s
    raise HTTPException(404, "no such session")


def _project_label(name: str, cwd) -> str:
    projects = config.load_projects()
    rev = {v: k for k, v in projects.items()}
    if cwd and cwd in rev:
        return rev[cwd]
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or cwd
    return name


def _next_name(agentkey: str) -> str:
    existing = set(tmux_panes())
    i = 1
    while f"{config.RC_PREFIX}{agentkey}-{i}" in existing:
        i += 1
    return f"{config.RC_PREFIX}{agentkey}-{i}"


# ---- REST ---------------------------------------------------------------

@app.get("/api/agents")
def api_agents():
    return [{"key": a["key"], "label": a["label"], "installed": a["installed"],
             "chat": a["provider"] == "claude"} for a in config.load_agents()]


@app.get("/api/settings")
def api_settings():
    return {
        "agents": [{"key": a["key"], "label": a["label"], "cmd": a["cmd"],
                    "provider": a.get("provider"), "detect": a.get("detect", True),
                    "installed": a["installed"]} for a in config.load_agents()],
        "projects": [{"key": k, "path": p, "exists": os.path.isdir(p)}
                     for k, p in config.load_projects().items()],
        "sudo": config.sudo_enabled(),
        "authRequired": bool(config.get_password()),
    }


@app.post("/api/settings/agents")
def api_save_agents(body: dict = Body(...)):
    agents = body.get("agents")
    if not isinstance(agents, list) or not agents:
        raise HTTPException(400, "agents must be a non-empty list")
    for a in agents:
        if not (a.get("key") and a.get("label") and a.get("cmd")):
            raise HTTPException(400, "each agent needs key, label and cmd")
        if not re.match(r"^[a-z0-9_-]+$", a["key"]):
            raise HTTPException(400, f"invalid key '{a['key']}' (use a-z 0-9 _ -)")
    config.save_agents(agents)
    return {"ok": True}


@app.get("/api/projects")
def api_projects():
    return [{"key": k, "path": p, "exists": os.path.isdir(p)}
            for k, p in config.load_projects().items()]


@app.get("/api/sessions")
def api_sessions():
    out = []
    for s in all_sessions():
        sid, info = s["sid"], s["info"]
        cwd = info.get("cwd") if info else None
        default_status = "starting" if s["hub"] else "running"
        entry = {"id": s["id"], "name": s["name"], "kind": s["kind"],
                 "hub": s["hub"], "agent": s["agent"], "agentKey": s["agentKey"],
                 "chat": s["provider"] == "claude", "sessionId": sid,
                 "status": (info.get("status") or "idle") if info else default_status,
                 "cwd": cwd, "project": _project_label(s["name"], cwd), "changed": 0}
        if info:
            entry["waitingFor"] = info.get("waitingFor")
            items, _ = cd.read_tail(cd.transcript_path(sid), max_bytes=262144)
            entry["changed"] = len(cd.changed_files(items))
        out.append(entry)
    return out


@app.post("/api/sessions")
def api_create(body: dict = Body(...)):
    agentkey = body.get("agent", "claude")
    projkey = body.get("project")
    agent = config.agent_by_key(agentkey)
    projects = config.load_projects()
    if not agent:
        raise HTTPException(400, "unknown agent")
    if projkey not in projects:
        raise HTTPException(400, "unknown project")
    cwd = projects[projkey]
    if not os.path.isdir(cwd):
        raise HTTPException(400, f"cwd missing: {cwd}")
    name = _next_name(agentkey)
    # login shell so PATH/aliases resolve; exec so the pane pid becomes the agent
    r = tmux("new-session", "-d", "-s", name, "-c", cwd,
             "bash", "-lc", f"exec {agent['cmd']}")
    if r.returncode != 0:
        raise HTTPException(500, f"tmux: {r.stderr.strip()}")
    return {"name": name}


@app.post("/api/sessions/{sess}/rename")
def api_rename(sess: str, body: dict = Body(...)):
    s = resolve(sess)
    if s["kind"] != "tmux":
        raise HTTPException(403, "read-only session — cannot rename")
    new = (body.get("name") or "").strip()
    if not RENAME_RE.match(new):
        raise HTTPException(400, "name must be 1-60 chars: letters, digits, . _ -")
    if new != sess and new in tmux_panes():
        raise HTTPException(409, "a session with that name already exists")
    r = tmux("rename-session", "-t", sess, new, socket=s["socket"])
    if r.returncode != 0:
        raise HTTPException(500, f"tmux: {r.stderr.strip()}")
    return {"name": new}


@app.delete("/api/sessions/{sess}")
def api_kill(sess: str):
    s = resolve(sess)
    if s["kind"] != "tmux":
        raise HTTPException(403, "read-only session (not tmux) — cannot kill")
    tmux("kill-session", "-t", sess, socket=s["socket"])
    return {"ok": True}


@app.post("/api/sessions/{sess}/prompt")
def api_prompt(sess: str, body: dict = Body(...)):
    s = resolve(sess)
    if s["kind"] != "tmux":
        raise HTTPException(403, "read-only session (not tmux) — cannot send input")
    text = body.get("text") or ""
    if not text.strip():
        raise HTTPException(400, "empty")
    tmux("send-keys", "-t", sess, "-l", "--", text, socket=s["socket"])
    tmux("send-keys", "-t", sess, "Enter", socket=s["socket"])
    return {"ok": True}


# ---- Claude Code runtime controls: model + permission mode via tmux keys ------
# Model is set deterministically with `/config model=<alias>` (Claude Code >=2.1.182,
# verified on 2.1.206). Permission mode has no direct command — it cycles on
# Shift+Tab (tmux key name "BTab"). The cycle length varies per account (auto /
# bypass modes may be present), so rather than counting presses we read the
# on-screen mode badge and cycle until we reach the target, stopping if we loop
# all the way back to where we started (meaning the target isn't available here).
CLAUDE_MODELS = ["default", "sonnet", "opus", "opusplan", "haiku", "fable"]
CLAUDE_MODES = ["default", "acceptEdits", "plan", "auto"]
_MODEL_RE = re.compile(r"^(?:claude-[a-z0-9.\-]+|[a-z0-9]+)$")
# `/config model=X` sets the running session only, but can't set every model
# (Fable is a no-op there). Those go through `/model X`, which also saves X as the
# account default for new sessions — a side effect we surface back to the caller.
_MODEL_VIA_SLASH = {"fable"}
# Matched against lowercased pane text. "manual" and "plan" share the ⏸ glyph, so
# detection keys off the words, never the symbol.
_MODE_BADGES = [
    ("accept edits on", "acceptEdits"),
    ("plan mode on", "plan"),
    ("auto mode on", "auto"),
    ("manual mode on", "default"),
    ("bypass permissions on", "bypass"),
]


def _require_claude_tmux(sess: str) -> dict:
    s = resolve(sess)
    if s["kind"] != "tmux":
        raise HTTPException(403, "read-only session (not tmux) — cannot send input")
    if s.get("provider") != "claude":
        raise HTTPException(400, "model/mode controls are only available for Claude Code sessions")
    return s


def _capture(sess: str, s: dict) -> str:
    r = tmux("capture-pane", "-t", sess, "-p", socket=s["socket"])
    return r.stdout if r.returncode == 0 else ""


def _detect_mode(pane_text: str):
    low = pane_text.lower()
    for needle, name in _MODE_BADGES:
        if needle in low:
            return name
    return None


@app.get("/api/sessions/{sess}/claude")
def api_claude_state(sess: str):
    s = _require_claude_tmux(sess)
    model = None
    if s.get("sid"):
        jp = cd.transcript_path(s["sid"])
        if jp:
            model = cd.latest_model_alias(jp)
    return {"models": CLAUDE_MODELS, "modes": CLAUDE_MODES,
            "mode": _detect_mode(_capture(sess, s)), "model": model}


@app.post("/api/sessions/{sess}/model")
def api_set_model(sess: str, body: dict = Body(...)):
    s = _require_claude_tmux(sess)
    model = (body.get("model") or "").strip()
    if not _MODEL_RE.match(model):
        raise HTTPException(400, "invalid model name")
    saves_default = model in _MODEL_VIA_SLASH
    cmd = f"/model {model}" if saves_default else f"/config model={model}"
    tmux("send-keys", "-t", sess, "-l", "--", cmd, socket=s["socket"])
    tmux("send-keys", "-t", sess, "Enter", socket=s["socket"])
    return {"ok": True, "model": model, "savesDefault": saves_default}


@app.post("/api/sessions/{sess}/mode")
def api_set_mode(sess: str, body: dict = Body(...)):
    s = _require_claude_tmux(sess)
    target = (body.get("mode") or "").strip()
    if target not in CLAUDE_MODES:
        raise HTTPException(400, f"mode must be one of {CLAUDE_MODES}")
    start = _detect_mode(_capture(sess, s))
    if start is None:
        raise HTTPException(409, "couldn't read the current mode badge — bring the session to its prompt and retry")
    if start != target:
        moved = False
        for _ in range(8):
            tmux("send-keys", "-t", sess, "BTab", socket=s["socket"])
            time.sleep(0.22)
            cur = _detect_mode(_capture(sess, s))
            if cur == target:
                break
            if cur is not None and cur != start:
                moved = True
            if moved and cur == start:  # cycled a full loop without hitting target
                break
    final = _detect_mode(_capture(sess, s))
    return {"ok": True, "mode": final, "reached": final == target}


# ---- interactive selection: on-screen keys + menu detection / navigation ------
# Claude Code prompts (trust / permissions / plan approval / model picker) all render
# as numbered lists with a `❯` marking the current row. We parse that from the pane and
# drive it with arrow keys — the one primitive that works for every menu shape, whether
# or not the options are numbered on screen.
_OPT_RE = re.compile(r"^\s*(❯)?\s*(\d+)\.\s+(\S.*?)\s*$")
_KEY_ALIASES = {
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "enter": "Enter", "escape": "Escape", "esc": "Escape", "tab": "Tab",
    "btab": "BTab", "space": "Space", "pageup": "PageUp", "pagedown": "PageDown",
    "ctrlc": "C-c",
}


def _parse_menu(pane_text: str):
    """Detect a Claude Code numbered selection menu in the pane. Returns
    {"title", "options": [{"n", "label", "selected"}]} or None."""
    lines = pane_text.split("\n")
    found = []
    for i, ln in enumerate(lines):
        m = _OPT_RE.match(ln)
        if m:
            label = re.sub(r"\s{2,}.*$", "", m.group(3)).strip()  # drop trailing description column
            found.append((i, bool(m.group(1)), int(m.group(2)), label[:60]))
    # keep the longest contiguous run numbered 1,2,3,… with one row marked ❯ — this
    # rejects stray "1." lines in ordinary output that aren't an actual menu.
    best, run = None, []
    for it in found:
        if not run:
            run = [it] if it[2] == 1 else []
        elif it[2] == run[-1][2] + 1 and 0 < it[0] - run[-1][0] <= 6:
            run.append(it)
        else:
            if len(run) >= 2 and any(x[1] for x in run):
                best = run
            run = [it] if it[2] == 1 else []
    if len(run) >= 2 and any(x[1] for x in run):
        best = run
    if not best:
        return None
    first = best[0][0]
    title = ""
    for j in range(first - 1, max(-1, first - 6), -1):
        t = lines[j].strip(" │╭╮╰╯─").strip()
        if t and not _OPT_RE.match(lines[j]):
            title = t[:80]
            break
    return {"title": title,
            "options": [{"n": n, "label": lbl, "selected": sel} for (_i, sel, n, lbl) in best]}


def _require_tmux(sess: str) -> dict:
    s = resolve(sess)
    if s["kind"] != "tmux":
        raise HTTPException(403, "read-only session (not tmux) — cannot send input")
    return s


@app.get("/api/sessions/{sess}/prompt-state")
def api_prompt_state(sess: str):
    s = resolve(sess)
    if s["kind"] != "tmux":
        return {"waiting": False}
    menu = _parse_menu(_capture(sess, s))
    return {"waiting": True, **menu} if menu else {"waiting": False}


@app.post("/api/sessions/{sess}/key")
def api_key(sess: str, body: dict = Body(...)):
    s = _require_tmux(sess)
    raw = (body.get("key") or "").strip().lower()
    if len(raw) == 1 and raw.isdigit():
        tmux("send-keys", "-t", sess, "-l", "--", raw, socket=s["socket"])
        return {"ok": True}
    key = _KEY_ALIASES.get(raw)
    if not key:
        raise HTTPException(400, "unknown key")
    tmux("send-keys", "-t", sess, key, socket=s["socket"])
    return {"ok": True}


@app.post("/api/sessions/{sess}/select")
def api_select(sess: str, body: dict = Body(...)):
    s = _require_tmux(sess)
    try:
        target = int(body.get("option"))
    except (TypeError, ValueError):
        raise HTTPException(400, "option must be a number")
    for _ in range(16):
        menu = _parse_menu(_capture(sess, s))
        if not menu:
            raise HTTPException(409, "no selection menu is active")
        nums = [o["n"] for o in menu["options"]]
        if target not in nums:
            raise HTTPException(400, f"no option {target} in the current menu")
        cur = next((o["n"] for o in menu["options"] if o["selected"]), nums[0])
        if cur == target:
            break
        ci, ti = nums.index(cur), nums.index(target)
        tmux("send-keys", "-t", sess, "Down" if ti > ci else "Up", socket=s["socket"])
        time.sleep(0.14)
    tmux("send-keys", "-t", sess, "Enter", socket=s["socket"])
    return {"ok": True, "selected": target}


# ---- file upload from chat: save into the session's cwd so the agent can read it ----
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
_UP_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _uploads_dir(s: dict) -> str:
    cwd = (s.get("info") or {}).get("cwd")
    if cwd and os.path.isdir(cwd) and os.access(cwd, os.W_OK):
        return os.path.join(cwd, ".remotecode-uploads")
    return os.path.expanduser("~/.remotecode/uploads")


def _unique_path(dirpath: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    cand = os.path.join(dirpath, name)
    i = 1
    while os.path.exists(cand):
        cand = os.path.join(dirpath, f"{base}_{i}{ext}")
        i += 1
    return cand


@app.post("/api/sessions/{sess}/upload")
async def api_upload(sess: str, request: Request, name: str = ""):
    s = _require_tmux(sess)
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")
    safe = _UP_SAFE.sub("_", os.path.basename(name or "").strip()) or "upload.bin"
    dest_dir = _uploads_dir(s)
    os.makedirs(dest_dir, exist_ok=True)
    path = _unique_path(dest_dir, safe)
    with open(path, "wb") as f:
        f.write(data)
    return {"ok": True, "path": path, "name": os.path.basename(path), "size": len(data)}


@app.get("/api/sessions/{sess}/files")
def api_files(sess: str):
    s = resolve(sess)
    if not s["sid"]:
        return []
    cwd = (s.get("info") or {}).get("cwd")
    items, _ = cd.read_tail(cd.transcript_path(s["sid"]), max_bytes=8_000_000)
    files = cd.changed_files(items, cwd=cwd)
    if minio_sync.enabled():                       # mark which are backed up to MinIO
        for f in files:
            f["synced"] = minio_sync.is_synced(s["sid"], f["path"])
    return files


@app.get("/api/minio")
def api_minio_status():
    return minio_sync.status()


@app.post("/api/minio/sync")
def api_minio_sync(body: dict = Body(default={})):
    """Force an immediate MinIO sync. {"sess": name} for one session, else all."""
    if not minio_sync.enabled():
        return {"enabled": False, "uploaded": []}
    sess = (body or {}).get("sess")
    targets = [resolve(sess)] if sess else all_sessions()
    uploaded = []
    for s in targets:
        sid = s.get("sid")
        if not sid:
            continue
        cwd = (s.get("info") or {}).get("cwd")
        path = cd.transcript_path(sid)
        if not path:
            continue
        items, _ = cd.read_tail(path, max_bytes=8_000_000)
        uploaded += minio_sync.sync_files(sid, cd.changed_files(items, cwd=cwd), cwd=cwd)
    return {"enabled": True, "uploaded": uploaded, "count": len(uploaded)}


TEXT_MAX = 500_000          # above this we return a snippet + flag, never the whole file
MODEL_EXTS = {".glb", ".gltf"}
RAW_TYPES = {".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".svg": "image/svg+xml"}


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


@app.get("/api/files/preview")
def api_preview(path: str):
    if not os.path.isfile(path):
        return {"path": path, "exists": False, "kind": "missing"}
    name, size, ext = os.path.basename(path), os.path.getsize(path), _ext(path)
    base = {"path": path, "exists": True, "name": name, "size": size, "ext": ext}
    if ext in IMAGE_EXTS:
        return {**base, "kind": "image"}
    if ext in MODEL_EXTS:
        return {**base, "kind": "model"}
    try:
        with open(path, "rb") as f:
            raw = f.read(TEXT_MAX)
    except Exception as e:
        return {**base, "kind": "error", "content": f"<unreadable: {e}>"}
    if b"\x00" in raw:                                   # crude binary sniff
        return {**base, "kind": "binary"}
    return {**base, "kind": "text", "content": raw.decode("utf-8", "replace"),
            "truncated": size > TEXT_MAX}


@app.get("/api/files/raw")
def api_raw(path: str):
    """Serve a file's bytes (images, GLB models, downloads). Auth-gated."""
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    mt = RAW_TYPES.get(_ext(path))
    return FileResponse(path, media_type=mt) if mt else FileResponse(path)


@app.get("/api/sessions/{sess}/stream")
async def api_stream(sess: str):
    s0 = resolve(sess)
    if s0["provider"] != "claude":
        async def none():
            yield _sse("info", {"message": "This agent has no chat transcript — "
                                "use the terminal."})
        return StreamingResponse(none(), media_type="text/event-stream")
    readonly = s0["kind"] != "tmux"

    async def gen():
        sid, jp, offset = s0["sid"], None, 0
        cwd = (s0.get("info") or {}).get("cwd")
        last_status = None
        for _ in range(60):
            if sid is None:
                r = resolve(sess)
                sid = r["sid"]
                cwd = (r.get("info") or {}).get("cwd")
            if sid and jp is None:
                jp = cd.transcript_path(sid)
                if jp:
                    items, offset = cd.read_tail(
                        jp, max_bytes=8_000_000 if readonly else 4_000_000)
                    cd.attach_outputs(items, cwd=cwd)
                    yield _sse("init", {"sessionId": sid, "readonly": readonly,
                                        "items": items,
                                        "files": cd.changed_files(items, cwd=cwd)})
                    break
            yield ": waiting\n\n"
            await asyncio.sleep(0.5)
        if not jp:
            yield _sse("error", {"message": "Agent hasn't started its session yet. "
                                 "If it's asking to trust the folder or another "
                                 "prompt, open the terminal to answer it."})
            return
        while True:
            if readonly:
                info = cd.live_sessions().get(sid)
                if info is None:
                    yield _sse("closed", {})
                    return
            else:
                if sess not in tmux_panes():
                    yield _sse("closed", {})
                    return
                _, info = resolve_sid(sess)
            new, offset = cd.read_since(jp, offset)
            if new:
                cd.attach_outputs(new, cwd=cwd)
                yield _sse("append", {"items": new,
                                      "files": cd.changed_files(new, cwd=cwd)})
            st = (info or {}).get("status")
            if st != last_status:
                last_status = st
                yield _sse("status", {"status": st,
                                      "waitingFor": (info or {}).get("waitingFor")})
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ---- PTY websocket (terminal for ANY tmux agent) ------------------------

@app.websocket("/ws/term/{name}")
async def ws_term(ws: WebSocket, name: str):
    await ws.accept()
    panes = tmux_panes()
    if not SAFE_NAME.match(name) or name not in panes:
        await ws.close(code=4404)
        return
    sock = panes[name][0]
    argv = _tmux_argv(sock, "attach", "-t", name)
    import ptyprocess
    pty = ptyprocess.PtyProcess.spawn(
        argv, env={**os.environ, "TERM": "xterm-256color"})
    loop = asyncio.get_event_loop()
    alive = True

    def reader():
        nonlocal alive
        while alive:
            try:
                data = pty.read(65536)
            except Exception:
                break
            if not data:
                break
            asyncio.run_coroutine_threadsafe(_safe_send(ws, data), loop)
        alive = False
        asyncio.run_coroutine_threadsafe(_safe_close(ws), loop)

    threading.Thread(target=reader, daemon=True).start()
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                pty.write(msg["bytes"])
            elif msg.get("text") is not None:
                t = msg["text"]
                if t.startswith("\x00resize:"):
                    try:
                        cols, rows = (int(x) for x in t.split(":", 1)[1].split(","))
                        pty.setwinsize(rows, cols)
                    except Exception:
                        pass
                else:
                    pty.write(t.encode())
    except WebSocketDisconnect:
        pass
    finally:
        alive = False
        try:
            pty.terminate(force=True)     # detaches the tmux client; session lives on
        except Exception:
            pass


async def _safe_send(ws, data: bytes):
    try:
        await ws.send_bytes(data)
    except Exception:
        pass


async def _safe_close(ws):
    try:
        await ws.close()
    except Exception:
        pass


# ---- static -------------------------------------------------------------

# Files that make up the installable app shell. The build version is the newest
# mtime across all of them, so ANY edit bumps it — which (a) cache-busts the
# in-page asset URLs and (b) changes the service-worker bytes, triggering a SW
# update that purges the old offline cache and re-syncs. One number, no manual bump.
_SHELL_FILES = (
    "index.html", "app.js", "style.css", "sw.js", "manifest.webmanifest",
    "vendor/xterm.js", "vendor/xterm.css", "vendor/addon-fit.js",
    "vendor/highlight.min.js", "vendor/hljs-dark.css", "vendor/hljs-light.css",
    "vendor/model-viewer.min.js",
)


def build_version() -> str:
    v = 0
    for rel in _SHELL_FILES:
        try:
            v = max(v, int(os.path.getmtime(os.path.join(APP_DIR, "static", *rel.split("/")))))
        except OSError:
            pass
    return str(v)


@app.middleware("http")
async def _revalidate_assets(request: Request, call_next):
    # StaticFiles/FileResponse send ETag + Last-Modified but no Cache-Control, so
    # browsers heuristically cache the UI and skip revalidation — meaning updates to
    # index.html / app.js / style.css aren't picked up on a normal refresh. Force a
    # revalidation (still cheap: a 304 when unchanged) so the UI is always current.
    # (The service worker, when installed, is what actually serves these from a
    # local cache for speed; this header governs the plain-browser fallback.)
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/version")
def api_version():
    return {"version": build_version()}


@app.get("/sw.js")
def service_worker():
    # Served from the ROOT (not /static/) so its scope covers the whole app. The
    # build version is baked in, so when any shell file changes the SW body changes,
    # the browser sees a new worker, installs it, and the activate step drops the
    # stale cache — that's the "re-sync on version change" guarantee.
    path = os.path.join(APP_DIR, "static", "sw.js")
    with open(path, encoding="utf-8") as f:
        js = f.read().replace("__BUILD__", build_version())
    return Response(js, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache",
                             "Service-Worker-Allowed": "/"})


@app.get("/")
def index():
    # Serve index.html with app.js / style.css URLs cache-busted by the build version,
    # so a fresh load always pulls the current build even if an old copy was cached.
    path = os.path.join(APP_DIR, "static", "index.html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    v = build_version()
    html = html.replace("/static/app.js", f"/static/app.js?v={v}")
    html = html.replace("/static/style.css", f"/static/style.css?v={v}")
    return HTMLResponse(html)


# ---- session recovery ---------------------------------------------------

@app.post("/api/recover")
def api_recover(body: dict = Body(default={})):
    """Restore recent Claude conversations into tmux sessions (see recover.py).
    Pass {"dry_run": true} to preview without launching anything."""
    dry = bool((body or {}).get("dry_run"))
    hours = (body or {}).get("hours")
    return {"recovered": recover.recover(dry_run=dry, lookback_hours=hours)}


@app.on_event("startup")
async def _startup_recover():
    # After a reboot the tmux server is empty; bring back the conversations that
    # were live before shutdown. Runs once, off-thread, and never blocks startup.
    if not recover.enabled():
        return

    async def _go():
        # small delay so tmux-claude.service has created the default socket first
        await asyncio.sleep(3)
        try:
            res = await asyncio.to_thread(recover.recover)
            for r in res:
                print(f"[recover] {r['action']:>8}  {r['name']}  ({r['session_id'][:8]})"
                      + (f"  {r['label']}" if r.get("label") else ""), flush=True)
            done = [r for r in res if r["action"] == "restored"]
            if done:
                print(f"[recover] restored {len(done)} session(s) into tmux", flush=True)
        except Exception as e:
            print(f"[recover] failed: {e}", flush=True)

    asyncio.create_task(_go())


def _minio_sweep_once(seen_mtimes: dict):
    """Sync files for every session whose transcript changed since the last sweep.
    Runs in a worker thread (called via asyncio.to_thread) — no async here."""
    sid2cwd = {sid: info.get("cwd") for sid, info in cd.live_sessions().items()}
    total = 0
    for jf in glob.glob(os.path.join(cd.PROJECTS_DIR, "*", "*.jsonl")):
        try:
            mt = os.path.getmtime(jf)
        except OSError:
            continue
        if seen_mtimes.get(jf) == mt:        # unchanged since we last looked
            continue
        seen_mtimes[jf] = mt
        sid = os.path.splitext(os.path.basename(jf))[0]
        cwd = sid2cwd.get(sid)
        try:
            items, _ = cd.read_tail(jf, max_bytes=8_000_000)
            up = minio_sync.sync_files(sid, cd.changed_files(items, cwd=cwd), cwd=cwd)
            total += len(up)
        except Exception as e:
            print(f"[minio] sweep {sid[:8]}: {e}", flush=True)
    return total


@app.on_event("startup")
async def _startup_minio():
    # Continuously mirror files created/edited by any session into MinIO, so work
    # survives a disk loss or a wiped tree. Only sessions with fresh transcript
    # activity are re-read, so this stays cheap.
    if not minio_sync.enabled():
        st = minio_sync.status()
        why = "SDK missing" if not st["have_sdk"] else "no credentials configured"
        print(f"[minio] persistence off ({why})", flush=True)
        return
    try:
        interval = max(5, int(os.environ.get("REMOTECODE_MINIO_INTERVAL", "25")))
    except ValueError:
        interval = 25
    print(f"[minio] persistence on -> bucket '{minio_sync.bucket()}', every {interval}s",
          flush=True)

    async def _loop():
        seen = {}
        # first pass primes mtimes but still uploads current state
        while True:
            try:
                n = await asyncio.to_thread(_minio_sweep_once, seen)
                if n:
                    print(f"[minio] synced {n} file(s)", flush=True)
            except Exception as e:
                print(f"[minio] sweep failed: {e}", flush=True)
            await asyncio.sleep(interval)

    asyncio.create_task(_loop())


app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")),
          name="static")
