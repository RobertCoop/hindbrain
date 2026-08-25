import json
import os

# tomllib/shutil are imported lazily: hot-path hooks normally hit the JSON
# snapshot below and skip both (~5 ms of import+parse per invocation)
_cache = {"data_dir": None, "config": None}


def plugin_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# The handshake bridges hook-only environment into env-less shells: hooks get
# CLAUDE_PLUGIN_DATA from the harness, but the agent's Bash tool may inherit
# none of it — without a bridge the CLI silently forks a parallel store at the
# home fallback as session "unbound". session_start writes the handshake at a
# FIXED home path (findable with zero env); the CLI reads it before falling
# back. Keyed by project so concurrent projects don't collide.
SESSION_FRESH_S = 12 * 3600
WORKSPACE_FRESH_S = 7 * 24 * 3600


def _handshake_dir() -> str:
    return (os.environ.get("HINDBRAIN_HANDSHAKE_DIR")
            or os.path.expanduser("~/.claude/hindbrain/handshake"))


def _handshake_path(project: str) -> str:
    import hashlib
    h = hashlib.sha256(os.path.abspath(project).encode("utf-8")).hexdigest()[:16]
    return os.path.join(_handshake_dir(), f"{h}.json")


def ancestor_handshake(cwd: str, max_age_s: int = WORKSPACE_FRESH_S) -> dict | None:
    # freshest handshake whose workspace contains cwd — this is what makes
    # project binding STICKY: a multi-repo workspace resolves to the dir the
    # session launched in, no matter which nested repo the shell has cd'd into
    import time
    d = os.path.abspath(cwd or os.getcwd())
    try:
        names = sorted(os.listdir(_handshake_dir()))[:64]
    except OSError:
        return None
    now = time.time()
    best = None
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(_handshake_dir(), name), encoding="utf-8") as f:
                hs = json.load(f)
        except (OSError, ValueError):
            continue
        if not (isinstance(hs, dict) and hs.get("data_dir") and hs.get("project")
                and isinstance(hs.get("ts"), int)):
            continue
        if now - hs["ts"] > max_age_s:
            continue
        p = os.path.abspath(hs["project"])
        if d == p or d.startswith(p + os.sep):
            if best is None or hs["ts"] > best["ts"]:
                best = hs
    return best


ANCHOR_NAME = ".hindbrain"


def anchor_root(cwd: str) -> str | None:
    # nearest ancestor (cwd included) containing the workspace anchor file.
    # The anchor is the explicit, durable form of workspace identity: presence
    # is what matters; file content is reserved and currently ignored.
    cur = os.path.abspath(cwd or os.getcwd())
    while True:
        if os.path.exists(os.path.join(cur, ANCHOR_NAME)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def resolve_project(cwd: str) -> str:
    # HINDBRAIN_PROJECT env override -> .hindbrain anchor walk -> containing
    # workspace handshake -> git root of cwd (cwd itself outside any repo)
    env = os.environ.get("HINDBRAIN_PROJECT")
    if env:
        return os.path.abspath(env)
    anchored = anchor_root(cwd)
    if anchored:
        return anchored
    hs = ancestor_handshake(cwd)
    if hs:
        return os.path.abspath(hs["project"])
    return gitroot(cwd)


def write_handshake(project: str, session_id: str) -> None:
    import time
    try:
        os.makedirs(_handshake_dir(), mode=0o700, exist_ok=True)
        p = _handshake_path(project)
        tmp = p + f".tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"data_dir": data_dir(), "db": db_path(),
                       "session_id": session_id,
                       "project": os.path.abspath(project),
                       "ts": int(time.time())}, f)
        os.replace(tmp, p)
    except (OSError, ValueError):
        pass  # best-effort; CLI degrades to env/fallback resolution


def read_handshake(project: str) -> dict | None:
    try:
        with open(_handshake_path(project), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("data_dir") else None
    except (OSError, ValueError):
        return None


def handshake_session(project: str) -> str | None:
    # session ids go stale in a way the store location doesn't: honor only
    # a fresh handshake's binding
    import time
    hs = read_handshake(project)
    if hs and isinstance(hs.get("ts"), int) and hs.get("session_id"):
        if int(time.time()) - hs["ts"] <= SESSION_FRESH_S:
            return str(hs["session_id"])
    return None


def data_dir() -> str:
    if _cache["data_dir"]:
        return _cache["data_dir"]
    d = (os.environ.get("HINDBRAIN_DATA")
         or os.environ.get("CLAUDE_PLUGIN_DATA"))
    if not d:
        hs = ancestor_handshake(os.getcwd())
        if hs:
            d = hs["data_dir"]
    d = os.path.abspath(d or os.path.expanduser("~/.claude/hindbrain"))
    os.makedirs(d, mode=0o700, exist_ok=True)
    for sub in ("sessions", "drafts", "logs"):
        os.makedirs(os.path.join(d, sub), mode=0o700, exist_ok=True)
    _cache["data_dir"] = d
    return d


def db_path() -> str:
    return os.environ.get("HINDBRAIN_DB") or os.path.join(data_dir(), "hindbrain.db")


def config_path() -> str:
    return os.path.join(data_dir(), "config.toml")


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def load_config() -> dict:
    if _cache["config"] is not None:
        return _cache["config"]
    default_file = os.path.join(plugin_root(), "config.default.toml")
    user_file = config_path()
    snap_file = os.path.join(data_dir(), "config.cache.json")
    key = [_mtime(default_file), _mtime(user_file)]
    try:
        with open(snap_file, "r", encoding="utf-8") as f:
            snap = json.load(f)
        if snap.get("key") == key and isinstance(snap.get("cfg"), dict):
            _cache["config"] = snap["cfg"]
            return snap["cfg"]
    except (OSError, ValueError):
        pass

    import shutil
    import tomllib
    with open(default_file, "rb") as f:
        cfg = tomllib.load(f)
    if not os.path.exists(user_file):
        try:
            shutil.copy(default_file, user_file)
            key[1] = _mtime(user_file)
        except OSError:
            pass
    try:
        with open(user_file, "rb") as f:
            user = tomllib.load(f)
        for table, values in user.items():
            if isinstance(values, dict) and isinstance(cfg.get(table), dict):
                cfg[table].update(values)
            else:
                cfg[table] = values
    except (OSError, tomllib.TOMLDecodeError):
        pass
    try:
        tmp = snap_file + f".tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"key": key, "cfg": cfg}, f)
        os.replace(tmp, snap_file)
    except (OSError, TypeError, ValueError):
        pass
    _cache["config"] = cfg
    return cfg


def gitroot(cwd: str) -> str:
    d = os.path.abspath(cwd or os.getcwd())
    cur = d
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return d
        cur = parent


def _reset_cache_for_tests():
    _cache["data_dir"] = None
    _cache["config"] = None
