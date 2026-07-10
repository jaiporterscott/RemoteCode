"""
RemoteCode — a web control panel for AI coding agents running in tmux.

Launch/list/rename/kill sessions for any agent (Claude Code, Aider, Codex,
OpenCode, Goose, Gemini, or a plain shell), type prompts, and open a live
terminal for any of them. When the agent is Claude Code, a rich chat +
files-changed view is rendered from Claude's own transcript.

Binds 127.0.0.1:7070 by default with HTTP basic-auth built in.
"""
import os, re, json, glob, stat, base64, asyncio, secrets, subprocess, threading
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import config
import claude_data as cd

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


@app.get("/api/sessions/{sess}/files")
def api_files(sess: str):
    s = resolve(sess)
    if not s["sid"]:
        return []
    cwd = (s.get("info") or {}).get("cwd")
    items, _ = cd.read_tail(cd.transcript_path(s["sid"]), max_bytes=8_000_000)
    return cd.changed_files(items, cwd=cwd)


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
                        jp, max_bytes=8_000_000 if readonly else 262144)
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

@app.get("/")
def index():
    return FileResponse(os.path.join(APP_DIR, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")),
          name="static")
