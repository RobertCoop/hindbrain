import copy
import json
import os
import re
import time

try:
    import fcntl
except ImportError:
    fcntl = None

from lib import paths

DEFAULT_STATE = {"turn": 0, "injected": [], "reminded": [], "fetched": [],
                 "denied": {}, "last_nudge_turn": -10, "nudges": 0,
                 "struggle": {"fails": 0, "churn": {}, "streak": 0, "active": False,
                              "events": []},
                 "tainted_turns": [], "ended": False}

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def state_path(session: str, agent: str = "main") -> str:
    name = f"{_SAFE.sub('_', session or 'unknown')}__{_SAFE.sub('_', agent or 'main')}.json"
    return os.path.join(paths.data_dir(), "sessions", name)


def _fill_defaults(st: dict) -> dict:
    for k, v in DEFAULT_STATE.items():
        if k not in st:
            st[k] = copy.deepcopy(v)
    if isinstance(st.get("struggle"), dict):
        for k, v in DEFAULT_STATE["struggle"].items():
            st["struggle"].setdefault(k, copy.deepcopy(v))
    else:
        st["struggle"] = copy.deepcopy(DEFAULT_STATE["struggle"])
    return st


def load(session: str, agent: str = "main") -> dict:
    try:
        with open(state_path(session, agent), "r", encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            raise ValueError("state not a dict")
        return _fill_defaults(st)
    except (OSError, ValueError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULT_STATE)


def save(session: str, agent: str, st: dict) -> None:
    # pid-unique temp + os.replace keeps atomicity without importing tempfile
    path = state_path(session, agent)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _acquire(lock_path):
    # <=50ms poll then fail-open (§12.2)
    if fcntl is None:
        return None
    try:
        fh = open(lock_path, "a+")
    except OSError:
        return None
    deadline = time.monotonic() + 0.05
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.monotonic() >= deadline:
                fh.close()
                return None
            time.sleep(0.005)


def update(session: str, agent: str, fn) -> dict:
    lock = _acquire(state_path(session, agent) + ".lock")
    try:
        st = load(session, agent)
        result = fn(st)
        if isinstance(result, dict):
            st = result
        save(session, agent, st)
        return st
    finally:
        if lock is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock.close()


def reset(session: str, agent: str = "main") -> dict:
    st = copy.deepcopy(DEFAULT_STATE)
    save(session, agent, st)
    return st
