"""
Session recovery for RemoteCode.

A tmux server dies on shutdown, so any Claude Code conversations that were
running when the machine went down vanish as *processes*.  But Claude persists
every conversation to  ~/.claude/projects/<slug>/<sessionId>.jsonl , so the
*content* survives.  On startup we find every recently-active conversation
rooted in a configured project and relaunch each into its own tmux session with
`claude --resume <sessionId>`, so the panel comes back already holding your last
threads — each sitting idle at its prompt (no tokens are spent until you type).

Panes are named `rc-<agent>-<session name>`, reusing the name the conversation
already had (Claude writes it into the transcript, so it outlives the crash).
That matches the `rc-<agentkey>-…` form the web UI parses while keeping each
thread recognisable instead of a bare sequence number.

Only conversations whose working directory is one of RemoteCode's configured
projects are restored, so scratch/subagent transcripts are ignored.

Controlled by environment:
  REMOTECODE_RECOVER        1/0    enable auto-recovery on startup   (default 1)
  REMOTECODE_RECOVER_HOURS  float  lookback window in hours          (default 48)
                                   — only sessions active within this window are
                                   restored, so ancient threads stay dormant.
  REMOTECODE_RECOVER_MAX    int    safety cap on sessions restored   (default 8)
                                   — this is the real bound now that sessions
                                   are no longer collapsed one-per-directory.
"""
import os, glob, json, re, subprocess, time

import config
import claude_data as cd

PROJECTS_DIR = cd.PROJECTS_DIR
RC_PREFIX = config.RC_PREFIX


def enabled() -> bool:
    return os.environ.get("REMOTECODE_RECOVER", "1").lower() not in ("", "0", "false", "no")


def _hours() -> float:
    try:
        return float(os.environ.get("REMOTECODE_RECOVER_HOURS", "48"))
    except ValueError:
        return 48.0


def _cap() -> int:
    try:
        return int(os.environ.get("REMOTECODE_RECOVER_MAX", "8"))
    except ValueError:
        return 8


def _scan(path: str, max_lines: int = 80):
    """Pull (cwd, first_user_text) out of a transcript by reading only its head."""
    cwd = None
    label = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if cwd is None and d.get("cwd"):
                    cwd = d["cwd"]
                if not label and d.get("type") == "user":
                    label = cd._first_text((d.get("message") or {}).get("content"))
                if cwd and label:
                    break
    except OSError:
        return None, ""
    return cwd, (label or "").strip().replace("\n", " ")[:80]


# Claude records the conversation's name in the transcript itself, as
#   {"type":"ai-title","aiTitle":...}  /  {"type":"agent-name","agentName":...}
# so it survives the crash that killed the pane.  A session can be renamed, so
# the LAST such record wins.  We substring-test before parsing to stay cheap on
# multi-megabyte transcripts.
_NAME_KEYS = (("ai-title", "aiTitle"), ("agent-name", "agentName"))


def _title(path: str) -> str:
    """The session's own name, from the newest title record in its transcript."""
    found = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"ai-title"' not in line and '"agent-name"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                for typ, key in _NAME_KEYS:
                    if d.get("type") == typ and isinstance(d.get(key), str):
                        v = d[key].strip()
                        if v:
                            found = v
    except OSError:
        return ""
    return found


def _slug(text: str, maxlen: int = 40) -> str:
    """Squeeze a session name into something tmux and the UI both accept."""
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", (text or "").strip())
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return s[:maxlen].strip("-.")


_NAME_OK = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")


def _pane_name(agentkey: str, cand: dict, taken: set, stored=None) -> str:
    """The name this conversation should come back under, in preference order:

      1. the pane name it last had  — including one you set yourself, which is
         the whole point: tmux forgets those on crash, config remembers them;
      2. `rc-<agent>-<conversation name>`, the UI's own convention;
      3. `rc-<agent>-<short session id>` when it was never named at all.

    `taken` is mutated so a batch of restores cannot collide with each other or
    with panes that are already up.
    """
    name = ((stored or {}).get(cand["session_id"]) or "").strip()
    if not _NAME_OK.match(name):
        base = _slug(cand.get("title") or "") or cand["session_id"][:8]
        name = f"{RC_PREFIX}{agentkey}-{base}"
    name = name[:60].strip("-.")
    if name not in taken:
        taken.add(name)
        return name
    for i in range(2, 100):
        alt = f"{name[: 60 - len(str(i)) - 1].strip('-.')}-{i}"
        if alt not in taken:
            taken.add(alt)
            return alt
    taken.add(name)
    return name


def candidates(projects: dict, lookback_hours=None, now=None, exclude=None):
    """Every resumable Claude session under a configured project directory.

    A crashed tmux server takes *all* of its panes down, and several of them are
    usually rooted in the same directory, so this deliberately does NOT collapse
    to one session per directory — that would silently drop the rest.  The
    lookback window and `_cap()` are what bound the result.

    `projects` is {key: dir}.  Returns a list of dicts, newest-first:
        {project, cwd, session_id, mtime, label, title}
    Sessions in `exclude` (a set of session ids, e.g. ones already live) are
    skipped so we never fight an already-running conversation for its transcript.
    """
    lookback_hours = _hours() if lookback_hours is None else lookback_hours
    now = time.time() if now is None else now
    cutoff = now - lookback_hours * 3600
    exclude = exclude or set()

    dir2key = {}
    for key, path in projects.items():
        try:
            dir2key[os.path.realpath(path)] = key
        except OSError:
            continue

    out = []
    for jf in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        sid = os.path.splitext(os.path.basename(jf))[0]
        if sid in exclude:
            continue
        try:
            st = os.stat(jf)
        except OSError:
            continue
        if st.st_size == 0 or st.st_mtime < cutoff:
            continue
        cwd, label = _scan(jf)
        if not cwd:
            continue
        rp = os.path.realpath(cwd)
        if rp not in dir2key:                      # only configured projects
            continue
        out.append({"project": dir2key[rp], "cwd": rp, "session_id": sid,
                    "mtime": st.st_mtime, "label": label, "title": _title(jf)})
    out.sort(key=lambda c: c["mtime"], reverse=True)
    return out


def _panes_by_session() -> dict:
    """sessionId -> tmux session name, for panes already resuming that session.

    Idempotency has to key on the session id rather than the pane name: names
    are now unique-per-conversation, so a name-based check would never match an
    already-restored pane and every run would launch another copy of it.
    """
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F",
                            "#{session_name}\t#{pane_start_command}"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return {}
    if r.returncode != 0:
        return {}
    out = {}
    for ln in r.stdout.splitlines():
        name, _, cmdline = ln.partition("\t")
        m = re.search(r"--resume\s+([0-9a-fA-F-]{8,})", cmdline or "")
        if m and name.strip():
            out.setdefault(m.group(1), name.strip())
    return out


def _existing_tmux_sessions() -> set:
    """Session names on tmux's default socket (where we create recovery panes)."""
    try:
        r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return set()
    if r.returncode != 0:
        return set()
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


def recover(dry_run: bool = False, lookback_hours=None, projects=None):
    """Restore recent conversations into tmux sessions. Idempotent.

    Returns one record per candidate describing what happened:
        {name, project, session_id, label, action}
    action ∈ {restored, planned, exists, skipped-live, error}
    """
    projects = config.load_projects() if projects is None else projects
    agent = config.agent_by_key("claude") or {"cmd": "claude"}
    cmd = agent["cmd"]

    live = set(cd.live_sessions().keys())
    cands = candidates(projects, lookback_hours=lookback_hours, exclude=live)[:_cap()]
    existing = _existing_tmux_sessions()
    already = _panes_by_session()
    stored = config.load_pane_names()
    taken = set(existing)

    results = []
    for c in cands:
        sid = c["session_id"]
        rec = {"name": None, "project": c["project"], "session_id": sid,
               "label": c["label"], "title": c.get("title", ""), "action": None}
        if sid in already:                       # this conversation is already up
            rec["name"] = already[sid]
            rec["action"] = "exists"
            results.append(rec)
            continue
        name = _pane_name(agent.get("key", "claude"), c, taken, stored)
        rec["name"] = name
        if dry_run:
            rec["action"] = "planned"
            results.append(rec)
            continue
        # login shell so PATH/aliases resolve; exec so the pane pid becomes claude
        launch = f"exec {cmd} --resume {c['session_id']}"
        try:
            r = subprocess.run(
                ["tmux", "new-session", "-d", "-s", name, "-c", c["cwd"],
                 "bash", "-lc", launch],
                capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                existing.add(name)
                already[sid] = name
                rec["action"] = "restored"
            else:
                rec["action"] = "error"
                rec["error"] = r.stderr.strip()
        except Exception as e:
            rec["action"] = "error"
            rec["error"] = str(e)
        results.append(rec)
    return results
