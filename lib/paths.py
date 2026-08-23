import json
import os

# tomllib/shutil are imported lazily: hot-path hooks normally hit the JSON
# snapshot below and skip both (~5 ms of import+parse per invocation)
_cache = {"data_dir": None, "config": None}


def plugin_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    if _cache["data_dir"]:
        return _cache["data_dir"]
    d = (os.environ.get("HINDBRAIN_DATA")
         or os.environ.get("CLAUDE_PLUGIN_DATA")
         or os.path.expanduser("~/.claude/hindbrain"))
    d = os.path.abspath(d)
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
