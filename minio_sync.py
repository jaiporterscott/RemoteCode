"""
MinIO persistence for RemoteCode.

Every file a Claude session creates or edits lives only on local disk. This module
mirrors those files into a MinIO (S3) bucket so they survive a disk failure or a
wiped working tree, and keeps an append-only version history of each one.

RemoteCode already knows which files a session touched (claude_data.changed_files);
a background sweeper in app.py feeds them here and we upload the ones whose content
changed since we last saw them.

Object layout in the bucket:
  current/<session_id>/<relpath>                 latest copy (overwritten)
  history/<session_id>/<relpath>/<sha8>          immutable per-version copy
where <relpath> is the path relative to the session's cwd (or abs/<path> if outside).

Credentials are NEVER hardcoded or discovered. They come, in order, from:
  1. env: REMOTECODE_MINIO_ENDPOINT / _ACCESS_KEY / _SECRET_KEY / _SECURE / _BUCKET
  2. file: $XDG_CONFIG_HOME/remotecode/minio.json
           {"endpoint","access_key","secret_key","secure","bucket"}
If no credentials resolve, the feature is simply disabled (no-op) and the rest of
RemoteCode runs normally.

Toggle / tuning env:
  REMOTECODE_MINIO           1/0     master enable            (default 1 if creds present)
  REMOTECODE_MINIO_BUCKET    name    bucket                   (default "remotecode")
  REMOTECODE_MINIO_MAX_MB    int     skip files bigger than   (default 100)
"""
import os, io, json, hashlib, threading, time

import config

try:
    from minio import Minio
    _HAVE_MINIO = True
except Exception:
    _HAVE_MINIO = False

_CONFIG_FILE = os.path.join(config.CONFIG_DIR, "minio.json")
_STATE_FILE = os.path.join(config.CONFIG_DIR, "minio_state.json")

_lock = threading.Lock()
_client = None
_client_key = None            # creds tuple the cached client was built with
_state = None                 # {"<sid>|<abspath>": "<sha256>"}
_stats = {"uploaded": 0, "bytes": 0, "last_sync": None, "last_error": None}


# ---- config -------------------------------------------------------------

def _bool(v, default=False):
    if v is None:
        return default
    return str(v).lower() not in ("", "0", "false", "no", "off")


def _creds():
    """Return (endpoint, access_key, secret_key, secure, bucket) or None."""
    ep = os.environ.get("REMOTECODE_MINIO_ENDPOINT")
    ak = os.environ.get("REMOTECODE_MINIO_ACCESS_KEY")
    sk = os.environ.get("REMOTECODE_MINIO_SECRET_KEY")
    secure = _bool(os.environ.get("REMOTECODE_MINIO_SECURE"), False)
    bucket = os.environ.get("REMOTECODE_MINIO_BUCKET")
    if not (ak and sk):
        try:
            with open(_CONFIG_FILE) as f:
                d = json.load(f)
            ep = ep or d.get("endpoint")
            ak = ak or d.get("access_key")
            sk = sk or d.get("secret_key")
            if "secure" in d and os.environ.get("REMOTECODE_MINIO_SECURE") is None:
                secure = bool(d.get("secure"))
            bucket = bucket or d.get("bucket")
        except (OSError, ValueError):
            pass
    if not (ak and sk):
        return None
    ep = (ep or "127.0.0.1:9000").split("://", 1)[-1]
    bucket = bucket or "remotecode"
    return ep, ak, sk, secure, bucket


def enabled() -> bool:
    return _HAVE_MINIO and _bool(os.environ.get("REMOTECODE_MINIO"), True) and _creds() is not None


def _max_bytes() -> int:
    try:
        return int(os.environ.get("REMOTECODE_MINIO_MAX_MB", "100")) * 1024 * 1024
    except ValueError:
        return 100 * 1024 * 1024


# ---- client -------------------------------------------------------------

def client():
    """A connected Minio client with the bucket ensured, or None if unusable."""
    global _client, _client_key
    c = _creds()
    if not c or not _HAVE_MINIO:
        return None
    ep, ak, sk, secure, bucket = c
    key = (ep, ak, sk, secure, bucket)
    if _client is not None and _client_key == key:
        return _client
    cli = Minio(ep, access_key=ak, secret_key=sk, secure=secure)
    if not cli.bucket_exists(bucket):
        cli.make_bucket(bucket)
    _client, _client_key = cli, key
    return cli


def bucket() -> str:
    c = _creds()
    return c[4] if c else "remotecode"


def status() -> dict:
    c = _creds()
    return {
        "enabled": enabled(),
        "have_sdk": _HAVE_MINIO,
        "endpoint": c[0] if c else None,
        "bucket": c[4] if c else None,
        "secure": c[3] if c else None,
        "configured": c is not None,
        **_stats,
    }


# ---- version state ------------------------------------------------------

def _load_state():
    global _state
    if _state is None:
        try:
            with open(_STATE_FILE) as f:
                _state = json.load(f)
        except (OSError, ValueError):
            _state = {}
    return _state


def _save_state():
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    tmp = _STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_state, f)
    os.replace(tmp, _STATE_FILE)


def is_synced(session_id: str, path: str) -> bool:
    st = _load_state()
    return f"{session_id}|{os.path.abspath(path)}" in st


# ---- keys ---------------------------------------------------------------

def _relkey(path: str, cwd) -> str:
    ap = os.path.abspath(path)
    if cwd:
        try:
            rp = os.path.relpath(ap, os.path.abspath(cwd))
            if not rp.startswith(".."):
                return rp.replace(os.sep, "/")
        except ValueError:
            pass
    return "abs/" + ap.lstrip("/").replace(os.sep, "/")


def _sha256(path: str):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- sync ---------------------------------------------------------------

def sync_files(session_id: str, files, cwd=None) -> list:
    """Upload files (list of dicts from claude_data.changed_files) whose content
    changed since last sync. Returns the records actually uploaded. Never raises —
    errors are captured in status()['last_error']."""
    if not enabled():
        return []
    try:
        cli = client()
    except Exception as e:
        _stats["last_error"] = f"connect: {e}"
        return []
    if cli is None:
        return []

    bkt = bucket()
    cap = _max_bytes()
    uploaded = []
    with _lock:
        st = _load_state()
        dirty = False
        for f in files:
            path = f.get("path")
            if not path or not f.get("exists"):
                continue
            if not os.path.isfile(path):          # skip dirs / vanished
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > cap:
                continue
            key_state = f"{session_id}|{os.path.abspath(path)}"
            try:
                digest = _sha256(path)
            except OSError:
                continue
            if st.get(key_state) == digest:       # unchanged since last upload
                continue

            rel = _relkey(path, cwd)
            meta = {"session": session_id, "verb": str(f.get("verb") or ""),
                    "src": os.path.abspath(path)}
            try:
                for objkey in (f"current/{session_id}/{rel}",
                               f"history/{session_id}/{rel}/{digest[:8]}"):
                    with open(path, "rb") as fh:
                        cli.put_object(bkt, objkey, fh, size,
                                       content_type="application/octet-stream",
                                       metadata=meta)
            except Exception as e:
                _stats["last_error"] = f"put {rel}: {e}"
                continue

            st[key_state] = digest
            dirty = True
            _stats["uploaded"] += 1
            _stats["bytes"] += size
            uploaded.append({"path": path, "key": f"current/{session_id}/{rel}",
                             "size": size, "sha": digest[:8]})
        if dirty:
            _save_state()
        _stats["last_sync"] = int(time.time())
        if uploaded:
            _stats["last_error"] = None
    return uploaded
