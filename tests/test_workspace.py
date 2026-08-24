"""Sticky workspace binding: project identity is decided by the session's
launch dir (via the handshake) and holds for any cwd inside it."""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from lib import paths  # noqa: E402


def _mk_ws(tmp_path):
    ws = tmp_path / "ws"
    for r in ("repoA", "repoB"):
        (ws / r / ".git").mkdir(parents=True)
        (ws / r / "src").mkdir()
    return ws


def test_resolve_project_sticky(tmp_data, tmp_path):
    ws = _mk_ws(tmp_path)
    paths.write_handshake(str(ws), "sess-ws")
    # anywhere inside the workspace resolves to the workspace, not the repo
    assert paths.resolve_project(str(ws)) == str(ws)
    assert paths.resolve_project(str(ws / "repoA")) == str(ws)
    assert paths.resolve_project(str(ws / "repoA" / "src")) == str(ws)
    # outside the workspace: plain gitroot fallback
    other = tmp_path / "elsewhere" / ".git"
    other.mkdir(parents=True)
    assert paths.resolve_project(str(tmp_path / "elsewhere")) == str(
        tmp_path / "elsewhere")


def test_resolve_project_freshest_wins(tmp_data, tmp_path):
    ws = _mk_ws(tmp_path)
    repo_a = str(ws / "repoA")
    paths.write_handshake(str(ws), "sess-parent")
    time.sleep(0)  # write order below decides via ts; force distinct ts
    hs = paths.read_handshake(str(ws))
    # a NEWER session launched directly in repoA rebinds cwds under repoA
    paths.write_handshake(repo_a, "sess-repo")
    p = paths._handshake_path(repo_a)
    import json
    with open(p) as f:
        d = json.load(f)
    d["ts"] = hs["ts"] + 10
    with open(p, "w") as f:
        json.dump(d, f)
    assert paths.resolve_project(os.path.join(repo_a, "src")) == repo_a
    # but the parent workspace still owns paths outside repoA
    assert paths.resolve_project(str(ws / "repoB")) == str(ws)


def test_resolve_project_env_override(tmp_data, tmp_path, monkeypatch):
    ws = _mk_ws(tmp_path)
    paths.write_handshake(str(ws), "sess-ws")
    monkeypatch.setenv("HINDBRAIN_PROJECT", str(ws / "repoB"))
    assert paths.resolve_project(str(ws / "repoA")) == str(ws / "repoB")


def test_stale_workspace_handshake_ignored(tmp_data, tmp_path):
    ws = _mk_ws(tmp_path)
    paths.write_handshake(str(ws), "sess-old")
    p = paths._handshake_path(str(ws))
    import json
    with open(p) as f:
        d = json.load(f)
    d["ts"] = int(time.time()) - paths.WORKSPACE_FRESH_S - 60
    with open(p, "w") as f:
        json.dump(d, f)
    # stale workspace: falls back to gitroot of cwd
    assert paths.resolve_project(str(ws / "repoA" / "src")) == str(ws / "repoA")


def test_hook_gate_sticky_across_nested_repo(tmp_data, tmp_path, conn):
    # a memory bound to the workspace still surfaces when the hook event's
    # cwd has drifted into a nested repo
    import subprocess
    from tests.test_gates import hook_env, hook_output, run_hook, seed_memory

    ws = _mk_ws(tmp_path)
    paths.write_handshake(str(ws), "sess-ws")
    with open(os.path.join(str(tmp_data), "config.toml"), "w") as f:
        f.write("[thresholds]\ntau_hi = 0.05\ntau_lo = 0.01\n")
    mid = seed_memory(conn, "pytest in this workspace needs PYTHONPATH=repoA/src",
                      project=str(ws))
    evt = {"session_id": "sess-ws", "cwd": str(ws / "repoA" / "src"),
           "prompt": "why does pytest fail with import errors here",
           "hook_event_name": "UserPromptSubmit"}
    proc = run_hook("prompt_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "UserPromptSubmit")
    assert out is not None
    assert mid in out["hookSpecificOutput"]["additionalContext"]
