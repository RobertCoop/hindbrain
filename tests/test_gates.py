"""Spec 13.3 + F1: hook I/O via subprocess. Exit 0 always; JSON output (when
present) schema-valid with correct hookEventName and <=9500-char payloads;
flock concurrency; deny -> ask state machine."""
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
HOOK_SCRIPTS = ["session_start.py", "prompt_gate.py", "pretool_gate.py",
                "observer.py", "failure_gate.py", "stop_gate.py",
                "precompact_salvage.py", "session_end.py"]
ALL_SCRIPTS = sorted(f for f in os.listdir(SCRIPTS) if f.endswith(".py"))

MAX_OUT = 9500


# ---- F1: hooks.json must parse (FIRST assertion of this suite) ----

def test_00_hooks_json_parses():
    with open(os.path.join(REPO, "hooks", "hooks.json")) as f:
        spec = json.load(f)
    hooks = spec["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PreToolUse",
                          "PostToolUse", "PostToolUseFailure", "Stop",
                          "PreCompact", "SessionEnd"}
    for groups in hooks.values():
        for group in groups:
            for h in group["hooks"]:
                assert h["type"] == "command"
                script = h["args"][0].replace("${CLAUDE_PLUGIN_ROOT}", REPO)
                assert os.path.exists(script), script


# ---- harness ----

def hook_env(tmp_data, **extra):
    env = dict(os.environ)
    env.pop("HINDBRAIN_DISABLE", None)
    env.pop("HINDBRAIN_DB", None)
    env["HINDBRAIN_DATA"] = str(tmp_data)
    env.update(extra)
    return env


def run_hook(script, evt, env, stdin=None, timeout=60):
    data = stdin if stdin is not None else json.dumps(evt).encode()
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, script)],
                          input=data, capture_output=True, env=env,
                          timeout=timeout)


def hook_output(proc, event_name=None):
    # exit 0 always; parse+validate JSON output when present
    assert proc.returncode == 0, proc.stderr.decode()
    out = proc.stdout.decode().strip()
    if not out:
        return None
    obj = json.loads(out)
    if "hookSpecificOutput" in obj:
        hso = obj["hookSpecificOutput"]
        if event_name is not None:
            assert hso["hookEventName"] == event_name
        for k in ("additionalContext", "permissionDecisionReason"):
            if k in hso:
                assert isinstance(hso[k], str) and len(hso[k]) <= MAX_OUT
    else:
        assert set(obj) <= {"decision", "reason"}
        if "reason" in obj:
            assert len(obj["reason"]) <= MAX_OUT
    return obj


def seed_memory(conn, body, **kw):
    from lib import ids
    row = {"id": ids.ulid(), "title": body[:80], "body": body, "kind": "gotcha",
           "scope_type": "project", "scope_value": "", "project": "",
           "tags": "", "channel": "user_witnessed", "authority": "full",
           "status": "active", "hazard": 0, "hazard_mode": "deny", "pinned": 0,
           "prior": 1.0, "corroborations": 0, "created_at": int(time.time())}
    row.update(kw)
    cols = ",".join(row)
    conn.execute(f"INSERT INTO memory({cols}) VALUES "
                 f"({','.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    return row["id"]


def seed_candidate(conn, session, **kw):
    from lib import ids
    row = {"id": ids.ulid(), "session_id": session, "agent_id": None,
           "ts": int(time.time()), "priority": "P0",
           "signal": "remember_request",
           "payload": json.dumps({"text": "deploys happen on Fridays"}),
           "draft_cmd": "mem save --kind fact --scope project "
                        "'deploys happen on Fridays'",
           "near_match": None, "status": "open"}
    row.update(kw)
    cols = ",".join(row)
    conn.execute(f"INSERT INTO candidate({cols}) VALUES "
                 f"({','.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    return row["id"]


def low_tau_config(tmp_data):
    # data dir exists once the conn fixture has run
    with open(os.path.join(str(tmp_data), "config.toml"), "w") as f:
        f.write("[thresholds]\ntau_hi = 0.05\ntau_lo = 0.01\n")


# ---- one run per event shape ----

def test_session_start_startup(tmp_data, conn, tmp_path):
    seed_memory(conn, "pytest here needs PYTHONPATH=src; bare pytest fails")
    evt = {"session_id": "s-ss", "source": "startup", "cwd": str(tmp_path),
           "hook_event_name": "SessionStart"}
    proc = run_hook("session_start.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "SessionStart")
    assert out is not None
    assert "additionalContext" in out["hookSpecificOutput"]


def _proj_with_git(tmp_path):
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    return str(p)


def test_carryover_skips_live_owner_session(tmp_data, conn, tmp_path):
    # carryover must not claim an open candidate whose owning session is
    # still live (its own Stop gate owns it)
    from lib import state as statemod
    proj = _proj_with_git(tmp_path)
    cand = seed_candidate(
        conn, "s-owner-live", ts=int(time.time()) - 3600,
        payload=json.dumps({"text": "deploys happen on Fridays",
                            "project": proj}))
    statemod.reset("s-owner-live", "main")  # state file with ended=false
    evt = {"session_id": "s-carry1", "source": "startup", "cwd": proj,
           "hook_event_name": "SessionStart"}
    proc = run_hook("session_start.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "SessionStart")
    assert conn.execute("SELECT status FROM candidate WHERE id=?",
                        (cand,)).fetchone()[0] == "open"
    if out is not None:
        assert "Carried-over" not in out["hookSpecificOutput"]["additionalContext"]


def test_carryover_claims_ended_owner_session(tmp_data, conn, tmp_path):
    from lib import state as statemod
    proj = _proj_with_git(tmp_path)
    cand = seed_candidate(
        conn, "s-owner-done", ts=int(time.time()) - 3600,
        payload=json.dumps({"text": "deploys happen on Fridays",
                            "project": proj}))
    st = statemod.reset("s-owner-done", "main")
    st["ended"] = True
    statemod.save("s-owner-done", "main", st)
    evt = {"session_id": "s-carry2", "source": "startup", "cwd": proj,
           "hook_event_name": "SessionStart"}
    proc = run_hook("session_start.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "SessionStart")
    assert conn.execute("SELECT status FROM candidate WHERE id=?",
                        (cand,)).fetchone()[0] == "carried"
    assert out is not None
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "Carried-over" in text and "deploys happen on Fridays" in text


def test_prompt_gate_capability_on_remember(tmp_data, conn, tmp_path):
    evt = {"session_id": "s-p0", "cwd": str(tmp_path),
           "prompt": "Remember that we deploy only on Fridays",
           "hook_event_name": "UserPromptSubmit"}
    proc = run_hook("prompt_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "UserPromptSubmit")
    assert out is not None
    assert "mem save" in out["hookSpecificOutput"]["additionalContext"]
    # P0 candidate enqueued
    n = conn.execute("SELECT COUNT(*) FROM candidate WHERE session_id='s-p0' "
                     "AND priority='P0'").fetchone()[0]
    assert n == 1


def test_prompt_gate_remind_tier(tmp_data, conn, tmp_path):
    low_tau_config(tmp_data)
    mid = seed_memory(conn, "pytest here needs PYTHONPATH=src or imports fail")
    evt = {"session_id": "s-rem", "cwd": str(tmp_path),
           "prompt": "why does pytest fail with import errors here",
           "hook_event_name": "UserPromptSubmit"}
    proc = run_hook("prompt_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "UserPromptSubmit")
    assert out is not None
    assert mid in out["hookSpecificOutput"]["additionalContext"]


def test_pretool_gate_edit_shape(tmp_data, conn, tmp_path):
    evt = {"session_id": "s-edit", "cwd": str(tmp_path), "tool_name": "Edit",
           "tool_input": {"file_path": str(tmp_path / "src" / "x.py"),
                          "new_string": "import os\n"},
           "hook_event_name": "PreToolUse"}
    proc = run_hook("pretool_gate.py", evt, hook_env(tmp_data))
    hook_output(proc, "PreToolUse")


def test_pretool_gate_bash_benign(tmp_data, conn, tmp_path):
    evt = {"session_id": "s-bash", "cwd": str(tmp_path), "tool_name": "Bash",
           "tool_input": {"command": "ls -la"},
           "hook_event_name": "PreToolUse"}
    proc = run_hook("pretool_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "PreToolUse")
    if out is not None:  # never a decision on the benign path
        assert "permissionDecision" not in out["hookSpecificOutput"]


def test_pretool_gate_subagent_own_state(tmp_data, conn, tmp_path):
    low_tau_config(tmp_data)
    mid = seed_memory(conn, "git status is slow here; use --untracked-files=no",
                      scope_type="command", scope_value="git.status")
    evt = {"session_id": "s-sub", "agent_id": "agent-7", "cwd": str(tmp_path),
           "tool_name": "Bash", "tool_input": {"command": "git status"},
           "hook_event_name": "PreToolUse"}
    proc = run_hook("pretool_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "PreToolUse")
    assert out is not None
    assert mid in out["hookSpecificOutput"]["additionalContext"]
    # A6: dedup ledger is per (session, agent), not shared with main
    with open(os.path.join(str(tmp_data), "sessions",
                           "s-sub__agent-7.json")) as f:
        st = json.load(f)
    assert mid in st["injected"] + st["reminded"]
    assert not os.path.exists(
        os.path.join(str(tmp_data), "sessions", "s-sub__main.json"))


def test_edit_branch_excludes_quarantined(tmp_data, conn, tmp_path):
    # §5 capability table: quarantined memories never appear in pretool output
    low_tau_config(tmp_data)
    mid = seed_memory(conn, "editing settings.py here breaks the import cycle",
                      authority="quarantined", scope_type="path",
                      scope_value="*.py")
    evt = {"session_id": "s-qedit", "cwd": str(tmp_path), "tool_name": "Edit",
           "tool_input": {"file_path": str(tmp_path / "settings.py"),
                          "new_string": "editing settings breaks the import cycle"},
           "hook_event_name": "PreToolUse"}
    proc = run_hook("pretool_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "PreToolUse")
    if out is not None:
        assert mid not in out["hookSpecificOutput"].get("additionalContext", "")


def test_observer_bash_ok(tmp_data, conn, tmp_path):
    evt = {"session_id": "s-obs", "cwd": str(tmp_path), "tool_name": "Bash",
           "tool_input": {"command": "make test"}, "tool_response": "ok",
           "hook_event_name": "PostToolUse"}
    proc = run_hook("observer.py", evt, hook_env(tmp_data))
    assert proc.returncode == 0
    assert proc.stdout.decode().strip() == ""  # async: never emits output


def test_observer_webfetch_taint(tmp_data, conn, tmp_path):
    evt = {"session_id": "s-obs", "cwd": str(tmp_path), "tool_name": "WebFetch",
           "tool_input": {"url": "https://example.com"},
           "hook_event_name": "PostToolUse"}
    proc = run_hook("observer.py", evt, hook_env(tmp_data))
    assert proc.returncode == 0
    assert proc.stdout.decode().strip() == ""
    n = conn.execute("SELECT COUNT(*) FROM journal WHERE session_id='s-obs' "
                     "AND event='taint'").fetchone()[0]
    assert n == 1


def test_observer_failure_subagent(tmp_data, conn, tmp_path):
    evt = {"session_id": "s-obs", "agent_id": "agent-2", "cwd": str(tmp_path),
           "tool_name": "Bash", "tool_input": {"command": "pytest -x"},
           "error": "ModuleNotFoundError: No module named 'app'",
           "hook_event_name": "PostToolUseFailure"}
    proc = run_hook("observer.py", evt, hook_env(tmp_data))
    assert proc.returncode == 0
    assert proc.stdout.decode().strip() == ""
    row = conn.execute("SELECT agent_id, data FROM journal WHERE "
                       "session_id='s-obs' AND event='tool_fail'").fetchone()
    assert row is not None and row["agent_id"] == "agent-2"
    assert json.loads(row["data"])["sig"].startswith("Bash:pytest:")


def test_failure_gate_shape_and_output(tmp_data, conn, tmp_path):
    low_tau_config(tmp_data)
    mid = seed_memory(
        conn, "pytest ModuleNotFoundError here means PYTHONPATH=src is missing")
    evt = {"session_id": "s-fg", "cwd": str(tmp_path), "tool_name": "Bash",
           "tool_input": {"command": "pytest"},
           "error": "ModuleNotFoundError: No module named 'app'",
           "hook_event_name": "PostToolUseFailure"}
    proc = run_hook("failure_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "PostToolUseFailure")
    assert out is not None
    assert mid in out["hookSpecificOutput"]["additionalContext"]


def test_failure_gate_subagent(tmp_data, conn, tmp_path):
    evt = {"session_id": "s-fg", "agent_id": "agent-3", "cwd": str(tmp_path),
           "tool_name": "Edit", "tool_input": {"file_path": "/x.py"},
           "error": "old_string not found in file",
           "hook_event_name": "PostToolUseFailure"}
    proc = run_hook("failure_gate.py", evt, hook_env(tmp_data))
    hook_output(proc, "PostToolUseFailure")


def test_stop_gate_nudge(tmp_data, conn, tmp_path):
    cand = seed_candidate(conn, "s-stop")
    evt = {"session_id": "s-stop", "cwd": str(tmp_path),
           "hook_event_name": "Stop"}
    proc = run_hook("stop_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "Stop")
    assert out is not None
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "mem save" in text and "unsaved observation" in text
    row = conn.execute("SELECT data FROM journal WHERE session_id='s-stop' "
                       "AND event='nudge'").fetchone()
    assert cand in json.loads(row["data"])["shown"]


def test_stop_gate_surfaces_subagent_candidate(tmp_data, conn, tmp_path):
    # §12.1: subagent-authored candidates surface at the main-thread Stop
    cand = seed_candidate(conn, "s-substop", agent_id="agent-9")
    evt = {"session_id": "s-substop", "cwd": str(tmp_path),
           "hook_event_name": "Stop"}
    proc = run_hook("stop_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "Stop")
    assert out is not None
    assert "mem save" in out["hookSpecificOutput"]["additionalContext"]
    row = conn.execute("SELECT data FROM journal WHERE session_id='s-substop' "
                       "AND event='nudge'").fetchone()
    assert cand in json.loads(row["data"])["shown"]


def test_stop_gate_loop_guard(tmp_data, conn, tmp_path):
    seed_candidate(conn, "s-stop2")
    evt = {"session_id": "s-stop2", "cwd": str(tmp_path),
           "stop_hook_active": True, "hook_event_name": "Stop"}
    proc = run_hook("stop_gate.py", evt, hook_env(tmp_data))
    assert proc.returncode == 0
    assert proc.stdout.decode().strip() == ""


def test_precompact_salvage(tmp_data, conn, tmp_path):
    evt = {"session_id": "s-pc", "trigger": "auto", "cwd": str(tmp_path),
           "hook_event_name": "PreCompact"}
    proc = run_hook("precompact_salvage.py", evt, hook_env(tmp_data))
    assert proc.returncode == 0
    assert proc.stdout.decode().strip() == ""
    n = conn.execute("SELECT COUNT(*) FROM journal WHERE session_id='s-pc' "
                     "AND event='precompact'").fetchone()[0]
    assert n == 1


def test_session_end(tmp_data, conn, tmp_path):
    evt = {"session_id": "s-end", "cwd": str(tmp_path),
           "hook_event_name": "SessionEnd"}
    proc = run_hook("session_end.py", evt, hook_env(tmp_data))
    assert proc.returncode == 0
    with open(os.path.join(str(tmp_data), "sessions", "s-end__main.json")) as f:
        assert json.load(f)["ended"] is True


# ---- malformed stdin: every scripts/*.py exits 0 silently ----

@pytest.mark.parametrize("script", ALL_SCRIPTS)
def test_malformed_stdin(script, tmp_data):
    proc = run_hook(script, None, hook_env(tmp_data), stdin=b'{"not json{{{')
    assert proc.returncode == 0
    assert proc.stdout.decode().strip() == ""


@pytest.mark.parametrize("script", HOOK_SCRIPTS)
def test_empty_stdin(script, tmp_data):
    proc = run_hook(script, None, hook_env(tmp_data), stdin=b"")
    assert proc.returncode == 0
    assert proc.stdout.decode().strip() == ""


# ---- missing DB dir / uncreatable data dir ----

GENERIC_EVT = {"session_id": "s-x", "source": "startup", "prompt": "hello pytest",
               "tool_name": "Bash", "tool_input": {"command": "ls"},
               "error": "boom", "trigger": "auto"}


@pytest.mark.parametrize("script", HOOK_SCRIPTS)
def test_db_path_in_missing_dir(script, tmp_data, tmp_path):
    env = hook_env(tmp_data,
                   HINDBRAIN_DB=str(tmp_path / "no" / "such" / "dir" / "m.db"))
    evt = dict(GENERIC_EVT, cwd=str(tmp_path))
    proc = run_hook(script, evt, env)
    assert proc.returncode == 0, proc.stderr.decode()


@pytest.mark.parametrize("script", HOOK_SCRIPTS)
def test_data_dir_uncreatable(script, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("plain file")
    env = dict(os.environ)
    env.pop("HINDBRAIN_DISABLE", None)
    env.pop("HINDBRAIN_DB", None)
    env["HINDBRAIN_DATA"] = str(blocker / "sub")  # makedirs must fail
    evt = dict(GENERIC_EVT, cwd=str(tmp_path))
    proc = run_hook(script, evt, env)
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode().strip() == ""


# ---- locked DB: every hook script exits 0 (§13.3) ----

@pytest.mark.parametrize("script", HOOK_SCRIPTS)
def test_locked_db(script, tmp_data, conn, tmp_path):
    locker = sqlite3.connect(os.path.join(str(tmp_data), "hindbrain.db"))
    locker.execute("BEGIN IMMEDIATE")
    try:
        evt = dict(GENERIC_EVT, session_id="s-lock", cwd=str(tmp_path),
                   prompt="does the gate survive a locked database")
        proc = run_hook(script, evt, hook_env(tmp_data))
        assert proc.returncode == 0, proc.stderr.decode()
    finally:
        locker.rollback()
        locker.close()


# ---- 12.2 flock path: concurrent prompt_gate invocations ----

def test_concurrent_prompt_gates(tmp_data, conn, tmp_path):
    env = hook_env(tmp_data)
    evt = {"session_id": "s-conc", "cwd": str(tmp_path),
           "prompt": "concurrent invocation of the same gate",
           "hook_event_name": "UserPromptSubmit"}
    data = json.dumps(evt).encode()
    procs = [subprocess.Popen(
        [sys.executable, os.path.join(SCRIPTS, "prompt_gate.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env) for _ in range(2)]
    for p in procs:
        p.communicate(data, timeout=60)
    assert all(p.returncode == 0 for p in procs)
    with open(os.path.join(str(tmp_data), "sessions", "s-conc__main.json")) as f:
        st = json.load(f)
    assert st["turn"] >= 1  # lock held -> 2; fail-open may lose one, never all


# ---- deny path: hazard memory blocks, identical retry escalates to ask ----

def test_deny_then_ask_on_identical_retry(tmp_data, conn, tmp_path):
    body = ("Force-pushing to main wiped a teammate's work here; "
            "use --force-with-lease instead")
    mid = seed_memory(conn, body, scope_type="command", scope_value="git.push",
                      hazard=1, authority="full")
    evt = {"session_id": "s-deny", "cwd": str(tmp_path), "tool_name": "Bash",
           "tool_input": {"command": "git push --force origin main"},
           "hook_event_name": "PreToolUse"}
    env = hook_env(tmp_data)

    proc = run_hook("pretool_gate.py", evt, env)
    out = hook_output(proc, "PreToolUse")
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert body in hso["permissionDecisionReason"]  # memory body inline
    assert mid in hso["permissionDecisionReason"]

    # denied state persisted for the (session, agent) context
    with open(os.path.join(str(tmp_data), "sessions", "s-deny__main.json")) as f:
        assert "Bash:git.push" in json.load(f)["denied"][mid]

    proc2 = run_hook("pretool_gate.py", evt, env)
    out2 = hook_output(proc2, "PreToolUse")
    hso2 = out2["hookSpecificOutput"]
    assert hso2["permissionDecision"] == "ask"
    assert mid in hso2["permissionDecisionReason"]

    events = [r["event"] for r in conn.execute(
        "SELECT event FROM access_log WHERE memory_id=?", (mid,))]
    assert events.count("denied") == 2


def test_escalation_deny_then_ask_on_rerun(tmp_data, conn, tmp_path):
    # reminded-but-unfetched escalation (§8.5): a non-hazard full-authority
    # memory denies once, and the persisted state turns an identical rerun
    # into permissionDecision "ask"
    from lib import state as statemod
    body = ("kubectl delete on this cluster cascades into the shared "
            "namespace; double-check the context first")
    mid = seed_memory(conn, body, scope_type="command",
                      scope_value="kubectl.delete", hazard=0, authority="full")
    st = statemod.load("s-esc", "main")
    st["reminded"].append(mid)  # reminded earlier this session, never fetched
    statemod.save("s-esc", "main", st)
    evt = {"session_id": "s-esc", "cwd": str(tmp_path), "tool_name": "Bash",
           "tool_input": {"command": "kubectl delete pod web-1"},
           "hook_event_name": "PreToolUse"}
    env = hook_env(tmp_data)

    proc = run_hook("pretool_gate.py", evt, env)
    out = hook_output(proc, "PreToolUse")
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert mid in hso["permissionDecisionReason"]

    with open(os.path.join(str(tmp_data), "sessions", "s-esc__main.json")) as f:
        assert "Bash:kubectl.delete" in json.load(f)["denied"][mid]

    proc2 = run_hook("pretool_gate.py", evt, env)
    out2 = hook_output(proc2, "PreToolUse")
    hso2 = out2["hookSpecificOutput"]
    assert hso2["permissionDecision"] == "ask"
    assert mid in hso2["permissionDecisionReason"]


def test_deny_needs_full_authority(tmp_data, conn, tmp_path):
    # RT-5 adjunct: standard-authority hazard never denies
    seed_memory(conn, "half-trusted hazard note about git push",
                scope_type="command", scope_value="git.push",
                hazard=1, authority="standard")
    evt = {"session_id": "s-nod", "cwd": str(tmp_path), "tool_name": "Bash",
           "tool_input": {"command": "git push --force origin main"},
           "hook_event_name": "PreToolUse"}
    proc = run_hook("pretool_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "PreToolUse")
    if out is not None:
        assert "permissionDecision" not in out["hookSpecificOutput"]


def test_bash_file_read_triggers_path_scope(tmp_data, conn, tmp_path):
    # sed/cat-style reads: positional file args make path scopes fire in the
    # Bash branch; pending notes stay excluded there (§5 capability table)
    low_tau_config(tmp_data)
    (tmp_path / ".hindbrain").write_text("")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "db.py").write_text("engine = create_engine(...)\n")
    ok = seed_memory(conn, "db.py schema changes must go through alembic here",
                     scope_type="path", scope_value="src/**",
                     project=str(tmp_path), authority="standard")
    pend = seed_memory(conn, "unproven note about the same src files",
                       scope_type="path", scope_value="src/**",
                       project=str(tmp_path), authority="pending")
    evt = {"session_id": "s-sed", "cwd": str(tmp_path), "tool_name": "Bash",
           "tool_input": {"command": "sed -n '1,50p' src/db.py"},
           "hook_event_name": "PreToolUse"}
    proc = run_hook("pretool_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "PreToolUse")
    assert out is not None
    text = out["hookSpecificOutput"]["additionalContext"]
    assert ok in text
    assert pend not in text
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_read_tool_gate_and_flag(tmp_data, conn, tmp_path):
    # the Read tool goes through the edit branch: pending notes MAY remind
    low_tau_config(tmp_data)
    (tmp_path / "src").mkdir()
    fp = tmp_path / "src" / "db.py"
    fp.write_text("engine = create_engine(...)\n")
    pend = seed_memory(conn, "db.py schema changes must go through alembic",
                       scope_type="path", scope_value="src/**",
                       project=str(tmp_path), authority="pending")
    evt = {"session_id": "s-read", "cwd": str(tmp_path), "tool_name": "Read",
           "tool_input": {"file_path": str(fp)},
           "hook_event_name": "PreToolUse"}
    proc = run_hook("pretool_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "PreToolUse")
    assert out is not None
    assert pend in out["hookSpecificOutput"]["additionalContext"]

    # read_gate = false silences the Read branch (fresh session id: no dedup)
    with open(os.path.join(str(tmp_data), "config.toml"), "w") as f:
        f.write("[thresholds]\ntau_hi = 0.05\ntau_lo = 0.01\n"
                "[general]\nread_gate = false\n")
    evt["session_id"] = "s-read2"
    proc = run_hook("pretool_gate.py", evt, hook_env(tmp_data))
    assert proc.returncode == 0
    assert proc.stdout.decode().strip() == ""
