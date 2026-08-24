"""Scenario replays (spec §15 M2/M4): observer event-stream fixtures, the
correction detector, the remind -> fetch -> inject escalation loop, the S7
compact reset, and the full S1 fail -> fix -> nudge -> save -> next-session
surfacing loop. Hooks and the CLI run via subprocess with fixture stdin."""
import json
import os
import re
import shlex
import subprocess
import sys
import time

import pytest

from tests.test_gates import hook_env, hook_output, run_hook, seed_memory

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEM = os.path.join(REPO, "bin", "mem")


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    return str(p)


# ---- helpers ----

FILLER_WORDS = ("quartermaster", "ledger", "harbor", "archive", "lantern",
                "granite", "meadow", "copper", "willow", "ember")


def seed_fillers(conn, n=200):
    # healthy-corpus seed: with ~200 unrelated notes, FTS5 IDF is meaningful
    # and bm25 for a real match is strongly negative (not the small-corpus
    # collapse the rel floor papers over)
    from lib import ids
    now = int(time.time())
    rows = []
    for i in range(n):
        w = FILLER_WORDS[i % len(FILLER_WORDS)]
        body = f"{w} shelf entry alpha{i} catalogued beside the {w} annex bay"
        rows.append((ids.ulid(), body[:80], body, "fact", "project", "", now))
    conn.executemany(
        "INSERT INTO memory(id, title, body, kind, scope_type, project, "
        "created_at) VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()


def write_taus(tmp_data, tau_hi, tau_lo):
    with open(os.path.join(str(tmp_data), "config.toml"), "w") as f:
        f.write(f"[thresholds]\ntau_hi = {tau_hi}\ntau_lo = {tau_lo}\n")


def run_mem(args, tmp_data, session, cwd):
    env = hook_env(tmp_data, HINDBRAIN_SESSION=session)
    return subprocess.run([sys.executable, MEM] + args, capture_output=True,
                          text=True, env=env, cwd=cwd, timeout=60)


def read_state(tmp_data, session, agent="main"):
    with open(os.path.join(str(tmp_data), "sessions",
                           f"{session}__{agent}.json")) as f:
        return json.load(f)


def bash_fail_evt(session, proj, cmd="pytest -x",
                  error="ModuleNotFoundError: No module named 'app'"):
    return {"session_id": session, "cwd": proj, "tool_name": "Bash",
            "tool_input": {"command": cmd}, "error": error,
            "hook_event_name": "PostToolUseFailure"}


def bash_ok_evt(session, proj, cmd="PYTHONPATH=src pytest -x"):
    return {"session_id": session, "cwd": proj, "tool_name": "Bash",
            "tool_input": {"command": cmd}, "tool_response": "5 passed",
            "hook_event_name": "PostToolUse"}


# ---- M2 event-stream replay: observer (spec §8.7) ----

def test_observer_fail_fix_yields_exactly_one_learned_fix(tmp_data, conn, proj):
    s = "s-m2-fix"
    env = hook_env(tmp_data)
    # fail -> matching success; the success replayed once more must not
    # duplicate the candidate (one learned_fix per head per session)
    for evt in (bash_fail_evt(s, proj), bash_ok_evt(s, proj),
                bash_ok_evt(s, proj)):
        proc = run_hook("observer.py", evt, env)
        assert proc.returncode == 0
        assert proc.stdout.decode().strip() == ""  # async: never emits output
    rows = conn.execute(
        "SELECT * FROM candidate WHERE session_id=? AND signal='learned_fix'",
        (s,)).fetchall()
    assert len(rows) == 1
    c = dict(rows[0])
    assert c["status"] == "open"
    assert c["priority"] == "P2"
    payload = json.loads(c["payload"])
    assert payload["observer"] is True
    assert payload["fail_cmd"] == "pytest -x"
    assert payload["ok_cmd"] == "PYTHONPATH=src pytest -x"
    assert c["draft_cmd"].startswith("mem save")
    assert "--from-candidate" in c["draft_cmd"]


@pytest.mark.parametrize("error", [
    "Command was aborted: exit code 130",
    "HTTP 429 Too Many Requests: rate limit exceeded",
])
def test_observer_trivial_failures_produce_nothing(tmp_data, conn, proj, error):
    s = "s-m2-trivial"
    env = hook_env(tmp_data)
    for evt in (bash_fail_evt(s, proj, error=error), bash_ok_evt(s, proj)):
        proc = run_hook("observer.py", evt, env)
        assert proc.returncode == 0
    # trivial failures are dropped at journal-write time (§8.7 ignore list),
    # so no tool_fail row exists and the later success pairs with nothing
    n = conn.execute("SELECT COUNT(*) FROM journal WHERE session_id=? AND "
                     "event='tool_fail'", (s,)).fetchone()[0]
    assert n == 0
    n = conn.execute("SELECT COUNT(*) FROM candidate WHERE session_id=?",
                     (s,)).fetchone()[0]
    assert n == 0


def test_observer_webfetch_writes_taint_row(tmp_data, conn, proj):
    s = "s-m2-taint"
    evt = {"session_id": s, "cwd": proj, "tool_name": "WebFetch",
           "tool_input": {"url": "https://example.com/docs"},
           "hook_event_name": "PostToolUse"}
    proc = run_hook("observer.py", evt, hook_env(tmp_data))
    assert proc.returncode == 0
    n = conn.execute("SELECT COUNT(*) FROM journal WHERE session_id=? AND "
                     "event='taint'", (s,)).fetchone()[0]
    assert n == 1


# ---- M2 event-stream replay: correction detector (spec §8.4) ----

def test_correction_detector_p2_then_p1_on_repeat(tmp_data, conn, proj):
    env = hook_env(tmp_data)
    evt1 = {"session_id": "s-corr-1", "cwd": proj,
            "prompt": "no, that's wrong — use the socket path instead",
            "hook_event_name": "UserPromptSubmit"}
    proc = run_hook("prompt_gate.py", evt1, env)
    hook_output(proc, "UserPromptSubmit")
    row = conn.execute(
        "SELECT priority, signal FROM candidate WHERE session_id='s-corr-1'"
    ).fetchone()
    assert row is not None
    assert row["signal"] == "correction" and row["priority"] == "P2"
    # run 1 journaled a 'correction' row for this project (within 30 days);
    # a similar correction later is a repeat -> P1
    n = conn.execute("SELECT COUNT(*) FROM journal WHERE event='correction'"
                     ).fetchone()[0]
    assert n == 1
    evt2 = {"session_id": "s-corr-2", "cwd": proj,
            "prompt": "no, that's wrong — the socket path is what should be "
                      "used instead",
            "hook_event_name": "UserPromptSubmit"}
    proc = run_hook("prompt_gate.py", evt2, env)
    hook_output(proc, "UserPromptSubmit")
    row = conn.execute(
        "SELECT priority, signal FROM candidate WHERE session_id='s-corr-2'"
    ).fetchone()
    assert row is not None
    assert row["signal"] == "correction" and row["priority"] == "P1"


# ---- M4 escalation loop: remind -> mem get (fetch_weight) -> inject ----

def test_escalation_remind_fetch_then_inject(tmp_data, conn, proj):
    from lib import db, paths, querybuild, scoring
    write_taus(tmp_data, 0.5, 0.25)  # steady-state taus (spec §4.3)
    seed_fillers(conn, 200)
    body = ("pytest here needs PYTHONPATH=src exported; the sandbox imports "
            "fail otherwise")
    mid = seed_memory(conn, body, authority="standard")
    s = "s-esc-loop"
    prompt = "why does pytest fail with pythonpath sandbox imports here"
    evt = {"session_id": s, "cwd": proj, "prompt": prompt,
           "hook_event_name": "UserPromptSubmit"}
    env = hook_env(tmp_data)

    cfg = paths.load_config()
    q = querybuild.fts_query(prompt)
    ctx = scoring.Ctx(session=s, project=proj)

    def _score():
        now = int(time.time())
        hit = next(h for h in db.search(conn, q, proj) if h["id"] == mid)
        act = scoring.activation(
            db.activation_events(conn, mid, cfg["scoring"]["act_window"]),
            now, cfg)
        return scoring.score(hit, ctx, cfg, act)

    # 1. never-accessed memory scores in the remind band, and the first gate
    #    pass reminds
    s1 = _score()
    assert cfg["thresholds"]["tau_lo"] <= s1 < cfg["thresholds"]["tau_hi"]
    proc = run_hook("prompt_gate.py", evt, env)
    out = hook_output(proc, "UserPromptSubmit")
    text = out["hookSpecificOutput"]["additionalContext"]
    assert mid in text and "not loaded" in text
    assert "Reference notes retrieved" not in text

    # 2. the agent fetches the reminded note: logs 'fetched' at fetch_weight
    r = run_mem(["get", mid], tmp_data, s, proj)
    assert r.returncode == 0, r.stderr
    row = conn.execute(
        "SELECT weight FROM access_log WHERE memory_id=? AND event='fetched'",
        (mid,)).fetchone()
    assert row is not None and row["weight"] == pytest.approx(3.0)

    # 3. escalation: the fetch strictly raises the score past tau_hi
    s2 = _score()
    assert s2 > s1
    assert s2 >= cfg["thresholds"]["tau_hi"]

    # 4. the next gate pass injects (remind dedup does not block inject)
    proc = run_hook("prompt_gate.py", evt, env)
    out = hook_output(proc, "UserPromptSubmit")
    text = out["hookSpecificOutput"]["additionalContext"]
    assert mid in text and "Reference notes retrieved" in text
    events = [r["event"] for r in conn.execute(
        "SELECT event FROM access_log WHERE memory_id=? ORDER BY ts", (mid,))]
    assert "injected" in events


# ---- M4 / S7: compact reset re-arms injection and reminding ----

def _seed_active_decoys(conn, n=5):
    # decoys with high activation keep the target out of the SessionStart
    # profile top-5, so the profile's own injected-marking cannot mask the
    # compact-reset assertion on the target id
    from lib import ids
    now = int(time.time())
    out = []
    for i in range(n):
        mid = ids.ulid()
        body = f"granite corridor briefing note delta{i} for the archive wing"
        conn.execute(
            "INSERT INTO memory(id, title, body, kind, scope_type, project, "
            "created_at, access_count) VALUES (?,?,?,?,'project','',?,50)",
            (mid, body[:80], body, "fact", now))
        for _ in range(3):
            conn.execute(
                "INSERT INTO access_log(memory_id, session_id, agent_id, ts, "
                "event, weight) VALUES (?,'seed','main',?,'synthetic',5.0)",
                (mid, now))
        out.append(mid)
    conn.commit()
    return out


def test_s7_compact_reset_allows_re_remind(tmp_data, conn, proj):
    write_taus(tmp_data, 9.9, 0.01)  # remind-only, low remind bar
    body = ("pytest here needs PYTHONPATH=src exported; the sandbox imports "
            "fail otherwise")
    mid = seed_memory(conn, body)
    decoys = _seed_active_decoys(conn)
    s = "s-compact"
    pevt = {"session_id": s, "cwd": proj,
            "prompt": "why does pytest fail with pythonpath sandbox imports here",
            "hook_event_name": "UserPromptSubmit"}
    env = hook_env(tmp_data)

    # 1. remind the memory; the id lands in the session's reminded ledger
    proc = run_hook("prompt_gate.py", pevt, env)
    out = hook_output(proc, "UserPromptSubmit")
    assert mid in out["hookSpecificOutput"]["additionalContext"]
    assert mid in read_state(tmp_data, s)["reminded"]

    # 2. compact: ledgers cleared, archived to a compact_reset journal event
    cevt = {"session_id": s, "source": "compact", "cwd": proj,
            "hook_event_name": "SessionStart"}
    proc = run_hook("session_start.py", cevt, env)
    hook_output(proc, "SessionStart")
    st = read_state(tmp_data, s)
    assert st["reminded"] == []
    assert mid not in st["injected"]  # only the post-reset profile decoys
    assert set(st["injected"]) <= set(decoys)
    row = conn.execute(
        "SELECT data FROM journal WHERE session_id=? AND event='compact_reset'",
        (s,)).fetchone()
    assert row is not None and mid in json.loads(row["data"])["reminded"]

    # 3. the memory can remind again after the reset
    proc = run_hook("prompt_gate.py", pevt, env)
    out = hook_output(proc, "UserPromptSubmit")
    assert out is not None
    assert mid in out["hookSpecificOutput"]["additionalContext"]


# ---- M4 / S1: fail -> fix -> nudge -> save -> next-session surfacing ----

def test_s1_replay_fail_fix_nudge_save_and_surface(tmp_data, conn, proj):
    s1, s2 = "s-s1-a", "s-s1-b"
    env = hook_env(tmp_data)
    seed_fillers(conn, 200)

    # 1. fail -> fix under the observer produces the learned_fix candidate
    for evt in (bash_fail_evt(s1, proj), bash_ok_evt(s1, proj)):
        assert run_hook("observer.py", evt, env).returncode == 0
    cand = conn.execute(
        "SELECT * FROM candidate WHERE session_id=? AND signal='learned_fix'",
        (s1,)).fetchone()
    assert cand is not None
    draft = cand["draft_cmd"]
    assert draft.startswith("mem save") and "--from-candidate" in draft

    # 2. Stop gate nudges with the pre-drafted save
    proc = run_hook("stop_gate.py",
                    {"session_id": s1, "cwd": proj, "hook_event_name": "Stop"},
                    env)
    out = hook_output(proc, "Stop")
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "unsaved observation" in text and draft in text

    # 3. the draft executes cleanly from inside the project; the
    #    observer-witnessed channel lands authority=standard (§5.1)
    r = run_mem(shlex.split(draft)[1:], tmp_data, s1, proj)
    assert r.returncode == 0, r.stderr
    assert "authority=standard" in r.stdout
    mid = re.search(r"saved \[(\w+)\]", r.stdout).group(1)
    m = dict(conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone())
    assert m["channel"] == "observer_witnessed"
    assert m["authority"] == "standard"
    assert m["kind"] == "gotcha"
    assert m["scope_type"] == "command" and m["scope_value"] == "pytest"
    assert conn.execute("SELECT status FROM candidate WHERE id=?",
                        (cand["id"],)).fetchone()[0] == "saved"

    # 4. new session, matching task prompt: reminds under the shipped
    #    remind-only default taus (AM-8: tau_hi = 9.9)
    pevt = {"session_id": s2, "cwd": proj,
            "prompt": "the pytest suite fails with pythonpath src import trouble",
            "hook_event_name": "UserPromptSubmit"}
    proc = run_hook("prompt_gate.py", pevt, env)
    out = hook_output(proc, "UserPromptSubmit")
    text = out["hookSpecificOutput"]["additionalContext"]
    assert mid in text and "not loaded" in text
    assert "Reference notes retrieved" not in text

    # 5. Bash PreToolUse on the scoped command surfaces it command-adjacently
    #    (standard authority IS allowed command-adjacent, unlike pending)
    write_taus(tmp_data, 0.05, 0.01)
    bevt = {"session_id": s2, "cwd": proj, "tool_name": "Bash",
            "tool_input": {"command": "pytest -x"},
            "hook_event_name": "PreToolUse"}
    proc = run_hook("pretool_gate.py", bevt, env)
    out = hook_output(proc, "PreToolUse")
    hso = out["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert mid in hso["additionalContext"]
    assert "Reference notes retrieved" in hso["additionalContext"]
    events = [r["event"] for r in conn.execute(
        "SELECT event FROM access_log WHERE memory_id=?", (mid,))]
    assert "injected" in events


def test_learned_fix_rejects_unrelated_same_head(tmp_data, conn, tmp_path):
    # live FP (a): compound command's incidental exit-1 paired with an
    # unrelated later command sharing only the head
    fail_evt = {"session_id": "s-fp", "cwd": str(tmp_path), "tool_name": "Bash",
                "hook_event_name": "PostToolUseFailure",
                "tool_input": {"command":
                               'for d in a b; do ls "$d"; done && [ -d .git ]'},
                "tool_response": ""}
    run_hook("observer.py", fail_evt, hook_env(tmp_data))
    ok_evt = {"session_id": "s-fp", "cwd": str(tmp_path), "tool_name": "Bash",
              "hook_event_name": "PostToolUse",
              "tool_input": {"command": "ls -la"}, "tool_response": "total 8"}
    run_hook("observer.py", ok_evt, hook_env(tmp_data))
    n = conn.execute("SELECT COUNT(*) FROM candidate WHERE session_id='s-fp' "
                     "AND signal='learned_fix'").fetchone()[0]
    assert n == 0  # bare exit with no error text is unattributable; and
    # 'ls -la' shares only the head with the compound anyway


def test_learned_fix_rejects_error_shaped_success(tmp_data, conn, tmp_path):
    # live FP (b): the "working" command's own output still reads as a failure
    fail_evt = {"session_id": "s-fp2", "cwd": str(tmp_path), "tool_name": "Bash",
                "hook_event_name": "PostToolUseFailure",
                "tool_input": {"command": "pytest -x"},
                "tool_response": "ImportError: No module named app"}
    run_hook("observer.py", fail_evt, hook_env(tmp_data))
    ok_evt = {"session_id": "s-fp2", "cwd": str(tmp_path), "tool_name": "Bash",
              "hook_event_name": "PostToolUse",
              "tool_input": {"command": "PYTHONPATH=src pytest -x"},
              "tool_response": "3 failed, 1 error in 0.4s"}
    run_hook("observer.py", ok_evt, hook_env(tmp_data))
    n = conn.execute("SELECT COUNT(*) FROM candidate WHERE session_id='s-fp2' "
                     "AND signal='learned_fix'").fetchone()[0]
    assert n == 0

    # the genuine fix still pairs
    ok2 = dict(ok_evt, tool_response="4 passed in 0.4s")
    run_hook("observer.py", ok2, hook_env(tmp_data))
    n = conn.execute("SELECT COUNT(*) FROM candidate WHERE session_id='s-fp2' "
                     "AND signal='learned_fix'").fetchone()[0]
    assert n == 1


def test_synthetic_turns_excluded_from_witness(tmp_data, conn, tmp_path):
    # harness wrappers journaled as user prompts must not crowd the witness
    # window or count as the user's words
    base = {"session_id": "s-syn", "cwd": str(tmp_path),
            "hook_event_name": "UserPromptSubmit"}
    run_hook("prompt_gate.py",
             dict(base, prompt="remember that deploys here always need VAULT_ADDR exported"),
             hook_env(tmp_data))
    for i in range(6):
        run_hook("prompt_gate.py",
                 dict(base, prompt=f"<task-notification>background task {i} done"
                                   "</task-notification>"),
                 hook_env(tmp_data))
    import sys as _sys
    _sys.path.insert(0, REPO)
    from lib import witness, paths
    paths._reset_cache_for_tests()
    import os as _os
    _os.environ["HINDBRAIN_DATA"] = str(tmp_data)
    cfg = paths.load_config()
    assert witness.user_witnessed(
        "deploys here always need VAULT_ADDR exported first", "s-syn", conn,
        cfg, n_prompts=5)
    rows = conn.execute("SELECT data FROM journal WHERE session_id='s-syn' "
                        "AND event='user_prompt'").fetchall()
    import json as _json
    flags = [bool(_json.loads(r[0]).get("synthetic")) for r in rows]
    assert flags.count(True) == 6 and flags.count(False) == 1
