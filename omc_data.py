"""
Read-only bridge to oh-my-claudecode (OMC) state on disk.

Sources (all written by OMC itself, never by us):
  <root>/state/<mode>-state.json               per-mode loop state (root-level)
  <root>/state/sessions/<sessionId>/*.json     the same, scoped to one session
  <root>/plans|handoffs|research/*.md          artifacts a mode produced
  <root>/notepad.md, <root>/project-memory.json
  ~/.claude/plugins/cache/*/oh-my-claudecode/<ver>/skills|commands   invocable catalogue

<root> is the ".omc" dir resolved the same way OMC's own getOmcRoot() does:
OMC_STATE_DIR > .omc-workspace marker > git toplevel > cwd.

Everything here is best-effort: a missing dir, a truncated write, or a state file
written by a future OMC version must degrade to an empty/partial answer, never raise.
"""
import os, json, glob, time, subprocess

import claude_data as cd

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
USER_SKILLS_DIR = os.path.join(CLAUDE, "skills")
# version is globbed, not pinned — the plugin cache updates itself under us
PLUGIN_GLOB = os.path.join(CLAUDE, "plugins", "cache", "*", "oh-my-claudecode", "*")

# skills the OMC docs call "tier-0" workflows — the UI gives these top billing
TIER0 = {"autopilot", "ultrawork", "ralph", "team", "ralplan"}

CANCEL_COMMAND = "/oh-my-claudecode:cancel"

# OMC's HUD treats state files older than this as abandoned; we use it only to
# decide whether a mode file that omits `active` is still worth showing.
MAX_STATE_AGE = 2 * 60 * 60

_SKILL_CACHE = {}          # cwd -> (expires_at, [skill dicts])
_SKILL_TTL = 60.0
_GIT_CACHE = {}            # dir -> (expires_at, toplevel or None)
_GIT_TTL = 60.0


# ---- defensive coercion -------------------------------------------------
# The MCP state_write tool transports every value as a STRING, so the same field
# is "3" in one file and 3 in the next. Nothing below may raise on either.

def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on", "active")
    return False


def _str(v):
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def _load_json(path):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8", "replace"))
    except Exception:
        return None


# ---- state root resolution ---------------------------------------------

def _git_toplevel(cwd):
    if not cwd or not os.path.isdir(cwd):
        return None
    hit = _GIT_CACHE.get(cwd)
    if hit and hit[0] > time.time():
        return hit[1]
    top = None
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            top = r.stdout.strip() or None
    except Exception:
        top = None
    _GIT_CACHE[cwd] = (time.time() + _GIT_TTL, top)
    return top


def _workspace_root(cwd):
    """Nearest ancestor holding a `.omc-workspace` marker. Stops at $HOME so a
    stray marker in home can't collapse every repo into one state root."""
    if not cwd:
        return None
    cur = os.path.abspath(cwd)
    home = os.path.abspath(HOME)
    while True:
        if cur == home:
            return None
        if os.path.exists(os.path.join(cur, ".omc-workspace")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _centralized_roots(state_dir, anchor):
    """Candidates under OMC_STATE_DIR. OMC names them "<dirname>-<sha256[:16]>"
    where the hash is over the git REMOTE url (not the path), so we can't
    reproduce it here — match on the dirname prefix instead and take the newest."""
    base = os.path.basename(os.path.abspath(anchor).rstrip("/"))
    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in base)
    cands = [d for d in glob.glob(os.path.join(state_dir, safe + "-*")) if os.path.isdir(d)]
    cands.sort(key=lambda d: os.path.getmtime(d) if os.path.exists(d) else 0, reverse=True)
    if os.path.isdir(os.path.join(state_dir, "state")):
        cands.append(state_dir)          # OMC_STATE_DIR used flat, no project subdir
    return cands


def state_root(cwd):
    """Absolute path of the .omc root for `cwd`, or None when none exists.

    Ordered exactly like OMC's getOmcRoot(); we return the first candidate that
    is actually on disk, so a repo whose state lives one level up still resolves."""
    cwd = cwd or os.getcwd()
    cands = []
    state_dir = os.environ.get("OMC_STATE_DIR")
    if state_dir:
        cands += _centralized_roots(state_dir, _git_toplevel(cwd) or cwd)
    ws = _workspace_root(cwd)
    if ws:
        cands.append(os.path.join(ws, ".omc"))
    top = _git_toplevel(cwd)
    if top:
        cands.append(os.path.join(top, ".omc"))
    cands.append(os.path.join(os.path.abspath(cwd), ".omc"))
    for c in cands:
        if os.path.isdir(c):
            return c
    return None


# ---- skill discovery ----------------------------------------------------

def _frontmatter(path, max_bytes=8192):
    """(fields, body) from a `---`-delimited YAML-ish header. Hand-rolled on
    purpose: only `key: value` scalars matter here and we refuse a dependency."""
    try:
        with open(path, "rb") as f:
            text = f.read(max_bytes).decode("utf-8", "replace")
    except Exception:
        return {}, ""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields, end = {}, len(lines)
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end = i
            break
        if ":" not in line or line.startswith((" ", "\t", "-", "#")):
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        fields[k.strip()] = v
    return fields, "\n".join(lines[end + 1:])


def _first_heading(body):
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
        if line:
            return line[:120]
    return None


def _entry(name, command, path, source):
    fm, body = _frontmatter(path)
    desc = fm.get("description") or _first_heading(body) or ""
    return {"name": fm.get("name") or name,
            "command": command,
            "description": desc,
            "source": source,
            "tier0": name in TIER0,
            # honest signal rather than a guess: either the author declared an
            # argument hint or the file interpolates $ARGUMENTS
            "takesArgs": bool(fm.get("argument-hint")) or "$ARGUMENTS" in body,
            "path": path}


def skills(cwd=None):
    """Invocable OMC skills/commands, project > user > plugin, cached ~60s."""
    key = os.path.abspath(cwd or os.getcwd())
    hit = _SKILL_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    found = {}

    def add(name, command, path, source):
        if name and name not in found:
            found[name] = _entry(name, command, path, source)

    root = state_root(cwd)
    if root:
        for p in sorted(glob.glob(os.path.join(root, "skills", "*", "SKILL.md"))):
            n = os.path.basename(os.path.dirname(p))
            add(n, "/" + n, p, "project")
    for p in sorted(glob.glob(os.path.join(USER_SKILLS_DIR, "*", "SKILL.md"))):
        n = os.path.basename(os.path.dirname(p))
        add(n, "/" + n, p, "user")
    for plugin in sorted(glob.glob(PLUGIN_GLOB)):
        for p in sorted(glob.glob(os.path.join(plugin, "skills", "*", "SKILL.md"))):
            n = os.path.basename(os.path.dirname(p))
            add(n, "/oh-my-claudecode:" + n, p, "plugin")
        # commands/ carries a few entries that have no SKILL.md of their own
        for p in sorted(glob.glob(os.path.join(plugin, "commands", "*.md"))):
            n = os.path.splitext(os.path.basename(p))[0]
            add(n, "/oh-my-claudecode:" + n, p, "plugin")

    out = sorted(found.values(), key=lambda s: (not s["tier0"], s["name"]))
    _SKILL_CACHE[key] = (time.time() + _SKILL_TTL, out)
    return out


def is_known_command(command, cwd=None) -> bool:
    """Whitelist check for anything we're about to type into a live agent."""
    if not command:
        return False
    if command == CANCEL_COMMAND:
        return True
    return command in {s["command"] for s in skills(cwd)}


# ---- live mode state ----------------------------------------------------

def _mode_from(path, data):
    """One `modes[]` entry, or None when the file isn't an active mode."""
    if not isinstance(data, dict):
        return None
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    name = _str(meta.get("mode")) or os.path.basename(path)[:-len("-state.json")]
    if "active" in data:
        active = _bool(data.get("active"))
    else:
        # Several writers (team) never set `active` at all — infer it from a
        # phase plus freshness, using OMC's own 2h staleness cutoff.
        phase = data.get("current_phase") or data.get("phase")
        try:
            fresh = (time.time() - os.path.getmtime(path)) < MAX_STATE_AGE
        except OSError:
            fresh = False
        active = bool(phase) and fresh
    if not active:
        return None
    detail = (_str(data.get("team_name")) or _str(data.get("current_story_id"))
              or _str(data.get("workflow")) or _str(data.get("stage_history")))
    return {"mode": name,
            "active": True,
            "phase": _str(data.get("current_phase")) or _str(data.get("phase")),
            "iteration": _num(data.get("iteration")),
            "maxIterations": _num(data.get("max_iterations")),
            "task": (_str(data.get("task_description")) or _str(data.get("task"))
                     or _str(data.get("goal"))),
            "detail": detail,
            "startedAt": (_str(data.get("started_at")) or _str(data.get("startedAt"))
                          or _str(meta.get("startedAt"))),
            "updatedAt": _str(meta.get("updatedAt")),
            "session": _str(meta.get("sessionId"))}


def modes(root, session_id=None):
    if not root:
        return []
    out = {}
    files = sorted(glob.glob(os.path.join(root, "state", "*-state.json")))
    if session_id:
        # session-scoped state wins over the root-level file for the same mode
        files += sorted(glob.glob(os.path.join(root, "state", "sessions",
                                               session_id, "*-state.json")))
    for p in files:
        m = _mode_from(p, _load_json(p))
        if m:
            out[m["mode"]] = m
    return sorted(out.values(), key=lambda m: m["mode"])


# ---- todos + agents (from the Claude transcript) ------------------------

def _tail_records(session_id, max_bytes=1_500_000):
    """Raw jsonl records from the tail of a session transcript.

    claude_data.parse_lines() throws away the bits we need here (TodoWrite
    inputs, Task subagent_type, tool_result ids), so we parse raw — but we keep
    its byte-capped tail strategy: a full re-read per poll is far too slow."""
    path = cd.transcript_path(session_id) if session_id else None
    if not path or not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        start = max(0, size - max_bytes)
        with open(path, "rb") as f:
            f.seek(start)
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    if start > 0:                     # drop the leading partial line
        nl = text.find("\n")
        text = text[nl + 1:] if nl >= 0 else ""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# Claude Code renamed the sub-agent tool: older transcripts say "Task",
# 2.1.x writes "Agent". Both carry the same subagent_type/description input.
_AGENT_TOOLS = ("Task", "Agent")
# A backgrounded agent gets its tool_result the instant it spawns, so
# "a tool_result exists" alone would report every worker as finished.
_SPAWN_MARKERS = ("spawned successfully", "is now running")


def _result_text(block) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _blocks(rec, role):
    if rec.get("type") != role:
        return []
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    c = msg.get("content")
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def todos_and_agents(session_id, limit=20):
    recs = _tail_records(session_id)
    todos, agents, done = [], [], set()
    for rec in recs:
        for b in _blocks(rec, "user"):
            if b.get("type") == "tool_result" and b.get("tool_use_id"):
                low = _result_text(b).lower()
                if not any(m in low for m in _SPAWN_MARKERS):
                    done.add(b["tool_use_id"])
        ts = rec.get("timestamp")
        for b in _blocks(rec, "assistant"):
            if b.get("type") != "tool_use":
                continue
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            if b.get("name") == "TodoWrite":
                items = inp.get("todos")
                if isinstance(items, list):       # last TodoWrite wins
                    todos = [{"content": _str(t.get("content")) or "",
                              "activeForm": _str(t.get("activeForm")),
                              "status": _str(t.get("status")) or "pending"}
                             for t in items if isinstance(t, dict)]
            elif b.get("name") in _AGENT_TOOLS:
                agents.append({"id": b.get("id"),
                               "type": _str(inp.get("subagent_type")) or "agent",
                               "description": _str(inp.get("description")) or "",
                               "name": _str(inp.get("name")),
                               "status": "running",
                               "at": ts})
    for a in agents:
        if a["id"] in done:
            a["status"] = "done"
    return todos, agents[-limit:][::-1]


# ---- artifacts ----------------------------------------------------------

_ARTIFACT_SOURCES = [("plan", "plans/*.md"), ("handoff", "handoffs/*.md"),
                     ("research", "research/*.md")]


def artifacts(root, limit=40):
    if not root:
        return []
    found = []
    for name, kind in (("notepad.md", "notepad"), ("project-memory.json", "memory")):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            found.append((kind, p))
    for kind, pat in _ARTIFACT_SOURCES:
        for p in glob.glob(os.path.join(root, pat)):
            if os.path.isfile(p):
                found.append((kind, p))
    out = []
    for kind, p in found:
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"kind": kind, "name": os.path.basename(p), "path": p,
                    "size": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda a: a["mtime"], reverse=True)
    return out[:limit]


def inside_roots(roots, path) -> bool:
    """True only if `path` really lives inside one of `roots`.

    Symlinks and `..` are defeated by realpath-ing BOTH sides before comparing —
    a prefix test on the raw string would let `/x/.omc/../../etc/passwd` through."""
    if not path:
        return False
    real = os.path.realpath(path)
    for r in roots or []:
        if not r:
            continue
        rr = os.path.realpath(r)
        if real == rr or real.startswith(rr.rstrip("/") + os.sep):
            return True
    return False


def read_artifact(roots, path, max_bytes=200_000):
    """Guarded read of one artifact. None when it's outside `roots` or gone."""
    if not inside_roots(roots, path):
        return None
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        return None
    try:
        size = os.path.getsize(real)
        with open(real, "rb") as f:
            raw = f.read(max_bytes)
    except OSError:
        return None
    return {"name": os.path.basename(real), "path": real, "size": size,
            "text": raw.decode("utf-8", "replace"), "truncated": size > max_bytes}


# ---- the one call the API layer needs -----------------------------------

def snapshot(cwd, session_id=None):
    """Everything the OMC status panel shows, for one session. Never raises."""
    try:
        root = state_root(cwd)
        todos, agents = todos_and_agents(session_id)
        return {"available": bool(root), "root": root,
                "modes": modes(root, session_id), "todos": todos,
                "agents": agents, "artifacts": artifacts(root)}
    except Exception as e:
        return {"available": False, "root": None, "modes": [], "todos": [],
                "agents": [], "artifacts": [], "error": str(e)}
