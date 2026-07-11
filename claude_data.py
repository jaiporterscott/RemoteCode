"""
Read-only helpers over Claude Code's on-disk data.

Sources (all written by Claude Code itself):
  ~/.claude/sessions/<pid>.json                     live-process registry (pid,sessionId,cwd,status)
  ~/.claude/projects/<cwd-slug>/<sessionId>.jsonl   full transcript, one JSON object per line
  ~/.claude/file-history/<sessionId>/<hash>@vN      versioned snapshots of edited files
"""
import os, json, glob, time, re

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
SESSIONS_DIR = os.path.join(CLAUDE, "sessions")
PROJECTS_DIR = os.path.join(CLAUDE, "projects")
FILE_HISTORY = os.path.join(CLAUDE, "file-history")

# tool_use name -> (verb, kind). kind "file" ones feed the Files panel.
TOOL_VERBS = {
    "Write": ("wrote", "file"),
    "Edit": ("edited", "file"),
    "MultiEdit": ("edited", "file"),
    "NotebookEdit": ("edited", "file"),
    "Read": ("read", "activity"),
    "Bash": ("ran", "activity"),
    "Grep": ("searched", "activity"),
    "Glob": ("globbed", "activity"),
    "WebFetch": ("fetched", "activity"),
    "WebSearch": ("searched web", "activity"),
    "Task": ("delegated", "activity"),
}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def live_sessions() -> dict:
    """sessionId -> {pid, sessionId, cwd, status, updatedAt, name}. Only live pids."""
    out = {}
    for fp in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            d = json.load(open(fp))
        except Exception:
            continue
        pid = d.get("pid")
        sid = d.get("sessionId")
        if not sid or not pid or not pid_alive(int(pid)):
            continue
        out[sid] = {
            "pid": int(pid),
            "sessionId": sid,
            "cwd": d.get("cwd"),
            "status": d.get("status"),
            "waitingFor": d.get("waitingFor"),
            "updatedAt": d.get("updatedAt"),
            "name": d.get("name"),
        }
    return out


def transcript_path(session_id: str):
    """Find the jsonl for a sessionId regardless of cwd-slug encoding."""
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


# ---- transcript parsing -------------------------------------------------

def _first_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _is_tool_result_only(content) -> bool:
    if not isinstance(content, list):
        return False
    types = {b.get("type") for b in content if isinstance(b, dict)}
    return bool(types) and types <= {"tool_result"}


def _tool_errors(content) -> dict:
    """tool_use_id -> is_error, from tool_result blocks in a user line."""
    out = {}
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out[b.get("tool_use_id")] = bool(b.get("is_error"))
    return out


def _chip_from_tool_use(b: dict) -> dict:
    name = b.get("name", "?")
    inp = b.get("input") or {}
    verb, kind = TOOL_VERBS.get(name, (name.lower(), "activity"))
    path = inp.get("file_path") or inp.get("notebook_path")
    detail = None
    if not path:
        if name == "Bash":
            detail = inp.get("command") or ""      # full command, untruncated
        elif name in ("Grep", "Glob"):
            detail = inp.get("pattern") or inp.get("query")
        elif name == "Task":
            detail = inp.get("description")
    return {"id": b.get("id"), "tool": name, "verb": verb, "kind": kind,
            "path": path, "detail": detail, "ok": True}


def parse_lines(lines):
    """Turn raw jsonl lines into ordered chat items. Robust to partial/blank lines."""
    items = []
    err_map = {}
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        t = d.get("type")
        msg = d.get("message")
        ts = d.get("timestamp")
        if t == "user" and isinstance(msg, dict):
            content = msg.get("content")
            err_map.update(_tool_errors(content))
            if _is_tool_result_only(content):
                continue
            text = _first_text(content)
            if text:
                items.append({"role": "user", "text": text, "chips": [], "ts": ts,
                              "uuid": d.get("uuid")})
        elif t == "assistant" and isinstance(msg, dict):
            content = msg.get("content") or []
            text = _first_text(content)
            chips = [_chip_from_tool_use(b) for b in content
                     if isinstance(b, dict) and b.get("type") == "tool_use"]
            if text or chips:
                items.append({"role": "assistant", "text": text, "chips": chips,
                              "ts": ts, "uuid": d.get("uuid")})
    # apply tool errors
    for it in items:
        for c in it["chips"]:
            if c["id"] in err_map:
                c["ok"] = not err_map[c["id"]]
    return items


def read_tail(path: str, max_bytes: int = 262144):
    """Return (items, end_offset). Reads only the last max_bytes for first paint."""
    if not path or not os.path.exists(path):
        return [], 0
    size = os.path.getsize(path)
    start = max(0, size - max_bytes)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    text = data.decode("utf-8", "replace")
    if start > 0:  # drop first partial line
        nl = text.find("\n")
        text = text[nl + 1:] if nl >= 0 else ""
    return parse_lines(text.splitlines()), size


def read_since(path: str, offset: int):
    """Return (new_items, new_offset) for bytes appended past offset."""
    if not path or not os.path.exists(path):
        return [], offset
    size = os.path.getsize(path)
    if size <= offset:
        return [], offset
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    # only parse up to the last complete line; keep offset at that boundary
    last_nl = data.rfind(b"\n")
    if last_nl < 0:
        return [], offset
    chunk = data[:last_nl + 1]
    text = chunk.decode("utf-8", "replace")
    return parse_lines(text.splitlines()), offset + len(chunk)


# artifacts a command may produce/modify that we want surfaced (GLBs, images, etc.)
ARTIFACT_EXTS = ("glb", "gltf", "obj", "fbx", "stl", "ply", "usdz",
                 "png", "jpg", "jpeg", "webp", "gif", "svg", "bmp", "tiff",
                 "mp4", "webm", "gif", "wav", "mp3", "pdf", "csv", "npy", "ckpt", "safetensors")
_PATH_RE = re.compile(r"(?<![\w-])(~?/?[\w./+\-]+\.(?:" + "|".join(ARTIFACT_EXTS) + r"))(?![\w])",
                      re.IGNORECASE)


def changed_files(items, cwd: str = None) -> list:
    """Deduped files the session touched, newest first. Includes Edit/Write targets
    AND artifact files (GLBs, images, …) named in Bash commands that exist on disk."""
    seen = {}
    for it in items:
        ts = it["ts"]
        for c in it["chips"]:
            if c["kind"] == "file" and c["path"]:                 # Edit/Write/Notebook
                seen[c["path"]] = {"path": c["path"], "verb": c["verb"],
                                   "ok": c["ok"], "ts": ts}
            elif c.get("tool") == "Bash" and c.get("detail"):     # artifacts in commands
                for m in _PATH_RE.findall(c["detail"]):
                    p = os.path.expanduser(m)
                    if not os.path.isabs(p) and cwd:
                        p = os.path.normpath(os.path.join(cwd, p))
                    if os.path.isfile(p) and p not in seen:
                        seen[p] = {"path": p, "verb": "output", "ok": True, "ts": ts}
    files = list(seen.values())
    for f in files:
        f["exists"] = os.path.exists(f["path"])
        f["name"] = os.path.basename(f["path"])
        try:
            f["size"] = os.path.getsize(f["path"]) if f["exists"] else 0
        except OSError:
            f["size"] = 0
    files.sort(key=lambda f: f["ts"] or "", reverse=True)
    return files


# artifacts we can actually SHOW inline in the chat (3D models + images), as opposed
# to merely link for download. Subset of ARTIFACT_EXTS.
_INLINE_PREVIEW_EXTS = {"glb", "gltf", "obj", "stl", "ply", "usdz",
                        "png", "jpg", "jpeg", "webp", "gif", "svg", "bmp", "tiff", "tif"}


def attach_outputs(items, cwd: str = None):
    """Attach an `outputs` list to each item: previewable artifacts (GLBs, images)
    named in that message's Bash commands that exist on disk. Lets the chat surface a
    clickable preview chip right where the command ran — a GLB from a Blender/script
    step is viewable in the conversation flow, not only from the Files panel."""
    for it in items:
        outs, seen = [], set()
        for c in it.get("chips", []):
            if c.get("tool") != "Bash" or not c.get("detail"):
                continue
            for m in _PATH_RE.findall(c["detail"]):
                ext = m.rsplit(".", 1)[-1].lower()
                if ext not in _INLINE_PREVIEW_EXTS:
                    continue
                p = os.path.expanduser(m)
                if not os.path.isabs(p) and cwd:
                    p = os.path.normpath(os.path.join(cwd, p))
                if p in seen or not os.path.isfile(p):
                    continue
                seen.add(p)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                outs.append({"path": p, "name": os.path.basename(p),
                             "ext": ext, "size": size})
        if outs:
            it["outputs"] = outs
    return items


_MODEL_ALIASES = [("fable", "fable"), ("sonnet", "sonnet"),
                  ("haiku", "haiku"), ("opus", "opus")]


def latest_model_alias(path: str):
    """Dropdown alias (opus/sonnet/haiku/fable) for the model on the most recent
    assistant turn in the transcript, or None. Reflects what's actually running —
    `default`/`opusplan` resolve to their concrete model here, which is the honest
    answer to 'what model is selected'."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 500_000))
            chunk = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    model = None
    for line in chunk.split("\n"):
        if '"model"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        m = d.get("message")
        if isinstance(m, dict) and m.get("model"):
            model = m["model"]           # keep scanning — the last one wins
    if not model:
        return None
    low = model.lower()
    for needle, alias in _MODEL_ALIASES:
        if needle in low:
            return alias
    return None


def history_versions(session_id: str, path: str) -> list:
    """Snapshot files for a given edited path, sorted by version."""
    d = os.path.join(FILE_HISTORY, session_id)
    if not os.path.isdir(d):
        return []
    # snapshots are <hash>@vN; we can't map path->hash without an index,
    # so return all snapshot files (UI shows latest content from disk primarily).
    vs = sorted(glob.glob(os.path.join(d, "*@v*")))
    return vs
