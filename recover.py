"""
Session recovery for RemoteCode.

A tmux server dies on shutdown, so any Claude Code conversations that were
running when the machine went down vanish as *processes*.  But Claude persists
every conversation to  ~/.claude/projects/<slug>/<sessionId>.jsonl , so the
*content* survives.  On startup we find the most-recently-active conversation
per project directory and relaunch it into a tmux session with
`claude --resume <sessionId>`, so the panel comes back already holding your last
threads — each sitting idle at its prompt (no tokens are spent until you type).

Only conversations whose working directory is one of RemoteCode's configured
projects are restored, so scratch/subagent transcripts are ignored.

Controlled by environment:
  REMOTECODE_RECOVER        1/0    enable auto-recovery on startup   (default 1)
  REMOTECODE_RECOVER_HOURS  float  lookback window in hours          (default 48)
                                   — only sessions active within this window are
                                   restored, so ancient threads stay dormant.
  REMOTECODE_RECOVER_MAX    int    safety cap on sessions restored   (default 8)
"""
import os, glob, json, subprocess, time

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


def candidates(projects: dict, lookback_hours=None, now=None, exclude=None):
    """The most-recent resumable Claude session per configured project directory.

    `projects` is {key: dir}.  Returns a list of dicts, newest-first:
        {project, cwd, session_id, mtime, label}
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

    best = {}   # cwd(realpath) -> (mtime, session_id, jsonl_path)
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
        cwd, _label = _scan(jf)
        if not cwd:
            continue
        rp = os.path.realpath(cwd)
        if rp not in dir2key:                      # only configured projects
            continue
        cur = best.get(rp)
        if cur is None or st.st_mtime > cur[0]:
            best[rp] = (st.st_mtime, sid, jf)

    out = []
    for rp, (mt, sid, jf) in best.items():
        _cwd, label = _scan(jf)
        out.append({"project": dir2key[rp], "cwd": rp, "session_id": sid,
                    "mtime": mt, "label": label})
    out.sort(key=lambda c: c["mtime"], reverse=True)
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

    results = []
    for c in cands:
        name = f"{RC_PREFIX}{c['project']}"
        rec = {"name": name, "project": c["project"], "session_id": c["session_id"],
               "label": c["label"], "action": None}
        if name in existing:
            rec["action"] = "exists"
            results.append(rec)
            continue
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
                rec["action"] = "restored"
            else:
                rec["action"] = "error"
                rec["error"] = r.stderr.strip()
        except Exception as e:
            rec["action"] = "error"
            rec["error"] = str(e)
        results.append(rec)
    return results
