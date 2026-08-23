import json
import os
import time

from lib import paths


def _log_path(name):
    return os.path.join(paths.data_dir(), "logs", name)


def err(exc: BaseException, hook_name: str) -> None:
    try:
        import traceback  # lazy: only the error path pays for it
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        tb = "".join(traceback.format_exception(exc))
        with open(_log_path("errors.log"), "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {hook_name}\n{tb}\n")
    except Exception:
        pass


def metric(d: dict) -> None:
    try:
        line = json.dumps({"ts": int(time.time()), **d})
        with open(_log_path("metrics.jsonl"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
