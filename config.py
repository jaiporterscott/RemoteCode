"""
RemoteCode configuration.

Everything is overridable by environment variable so the app is portable:
  REMOTECODE_HOST       bind host           (default 127.0.0.1)
  REMOTECODE_PORT       bind port           (default 7070)
  REMOTECODE_USER       basic-auth user     (default admin)
  REMOTECODE_PASSWORD   basic-auth password (default: generated + saved to config dir;
                                             set to empty string to DISABLE auth)
  REMOTECODE_PROJECTS   comma list of dirs or key=dir pairs to offer as working dirs
                        (default: auto-discovered from ~/.claude.json, else $HOME)
Config files (optional) live in  $XDG_CONFIG_HOME/remotecode  (default ~/.config/remotecode):
  password        the generated basic-auth password
  agents.json     override the built-in agent registry
"""
import os, json, shutil, secrets

HOME = os.path.expanduser("~")
HOST = os.environ.get("REMOTECODE_HOST", "127.0.0.1")
PORT = int(os.environ.get("REMOTECODE_PORT", "7070"))
AUTH_USER = os.environ.get("REMOTECODE_USER", "admin")
RC_PREFIX = "rc-"                        # names of sessions RemoteCode creates
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "remotecode")


# ---- auth ---------------------------------------------------------------

def get_password() -> str:
    """Return the basic-auth password, or "" to disable auth (the default).

    Auth is OPTIONAL and OFF unless you opt in, either by:
      * setting REMOTECODE_PASSWORD=yourpass, or
      * putting a password in  $XDG_CONFIG_HOME/remotecode/password
    With no password configured the UI is open (fine on 127.0.0.1 / behind a
    trusted network or reverse proxy; set one before exposing it further)."""
    pw = os.environ.get("REMOTECODE_PASSWORD")
    if pw is not None:
        return pw
    fp = os.path.join(CONFIG_DIR, "password")
    if os.path.exists(fp):
        return open(fp).read().strip()
    return ""   # auth disabled by default


# ---- agents -------------------------------------------------------------
# Each agent: key, label, cmd (argv used to launch + detect), provider.
# provider "claude" unlocks the rich chat + files-changed view (reads ~/.claude);
# provider None means terminal-only (works for literally any CLI agent).

_DEFAULT_AGENTS = [
    {"key": "claude",   "label": "Claude Code", "cmd": "claude",   "provider": "claude"},
    {"key": "codex",    "label": "Codex CLI",   "cmd": "codex",    "provider": None},
    {"key": "aider",    "label": "Aider",       "cmd": "aider",    "provider": None},
    {"key": "opencode", "label": "OpenCode",    "cmd": "opencode", "provider": None},
    {"key": "goose",    "label": "Goose",       "cmd": "goose",    "provider": None},
    {"key": "gemini",   "label": "Gemini CLI",  "cmd": "gemini",   "provider": None},
    # "shell" is launchable, but detect=False so we don't list every random bash pane
    {"key": "shell",    "label": "Plain shell", "cmd": os.environ.get("SHELL", "bash"),
     "provider": None, "detect": False},
]


def load_agents() -> list:
    fp = os.path.join(CONFIG_DIR, "agents.json")
    agents = _DEFAULT_AGENTS
    if os.path.exists(fp):
        try:
            agents = json.load(open(fp))
        except Exception:
            pass
    for a in agents:
        a["installed"] = shutil.which(a["cmd"].split()[0]) is not None
    return agents


def agent_by_key(key: str):
    for a in load_agents():
        if a["key"] == key:
            return a
    return None


def agent_comm(a: dict) -> str:
    """The process name (comm) to look for when detecting this agent in a pane."""
    return os.path.basename(a["cmd"].split()[0])


# ---- working directories ("projects") -----------------------------------

def _key(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or "root"


def load_projects() -> dict:
    env = os.environ.get("REMOTECODE_PROJECTS")
    out = {}
    if env:
        for tok in env.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if "=" in tok:
                k, p = tok.split("=", 1)
                out[k.strip()] = os.path.expanduser(p.strip())
            else:
                p = os.path.expanduser(tok)
                out[_key(p)] = p
        return out
    # autodiscover from Claude's project list, else just HOME
    try:
        d = json.load(open(os.path.join(HOME, ".claude.json")))
        for p in d.get("projects", {}):
            if os.path.isdir(p):
                out[_key(p)] = p
    except Exception:
        pass
    out.setdefault("home", HOME)
    return out
