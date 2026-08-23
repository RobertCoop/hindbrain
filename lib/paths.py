import os
import shutil
import tomllib

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


def load_config() -> dict:
    if _cache["config"] is not None:
        return _cache["config"]
    default_file = os.path.join(plugin_root(), "config.default.toml")
    with open(default_file, "rb") as f:
        cfg = tomllib.load(f)
    user_file = config_path()
    if not os.path.exists(user_file):
        try:
            shutil.copy(default_file, user_file)
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
