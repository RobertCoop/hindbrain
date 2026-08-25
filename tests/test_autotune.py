"""Earn-the-inject-tier tuner: meta.auto_tau_hi as learned state, config as
operator intent, gates taking min() at read time."""
import json
import os
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEM = os.path.join(REPO, "bin", "mem")
sys.path.insert(0, REPO)

from consolidator import consolidate  # noqa: E402
from lib import db, ids, paths, scoring  # noqa: E402


def seed_accesses(conn, reminded, fetched_after, days_ago=1):
    ts = int(time.time()) - days_ago * 86400
    for i in range(reminded):
        mid = ids.ulid()
        conn.execute(
            "INSERT INTO memory(id, title, body, kind, scope_type, project, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (mid, f"note {i}", f"body of remembered note number {i} here",
             "fact", "project", "/p", ts))
        conn.execute(
            "INSERT INTO access_log(memory_id, session_id, agent_id, ts, event, "
            "weight) VALUES (?,?,?,?,'reminded',1.0)",
            (mid, f"s{i % 7}", "main", ts + i))
        if i < fetched_after:
            conn.execute(
                "INSERT INTO access_log(memory_id, session_id, agent_id, ts, "
                "event, weight) VALUES (?,?,?,?,'fetched',3.0)",
                (mid, f"s{i % 7}", "main", ts + i + 10))
    conn.commit()


def rollup_and_tune(conn, cfg=None, now=None):
    cfg = cfg or paths.load_config()
    now = now or int(time.time())
    r = consolidate.metrics_rollup(conn, cfg, now)
    return consolidate.autotune(conn, cfg, now, r["acceptance"], r["reminded"]), r


def auto_tau(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='auto_tau_hi'").fetchone()
    return float(row[0]) if row else None


def test_effective_taus_min_and_flag(tmp_data, conn):
    cfg = paths.load_config()
    st = {"struggle": {"active": False}}
    assert scoring.effective_taus(cfg, st, conn) == (9.9, 0.25)
    conn.execute("INSERT INTO meta(key, value) VALUES ('auto_tau_hi', '0.50')")
    conn.commit()
    assert scoring.effective_taus(cfg, st, conn) == (0.5, 0.25)
    # operator hand-set value wins in the enabling direction
    cfg2 = json.loads(json.dumps(cfg))
    cfg2["thresholds"]["tau_hi"] = 0.4
    assert scoring.effective_taus(cfg2, st, conn)[0] == 0.4
    # auto_inject=false pins config outright
    cfg3 = json.loads(json.dumps(cfg))
    cfg3["thresholds"]["auto_inject"] = False
    assert scoring.effective_taus(cfg3, st, conn)[0] == 9.9
    # struggle factor applies after
    st2 = {"struggle": {"active": True}}
    th, tl = scoring.effective_taus(cfg, st2, conn)
    assert th == pytest.approx(0.5 * 0.75) and tl == pytest.approx(0.25 * 0.75)


def test_tuner_enables_with_sample_and_holds_without(tmp_data, conn):
    # under-sample: rate alone never flips
    seed_accesses(conn, reminded=10, fetched_after=5)
    out, _ = rollup_and_tune(conn)
    assert out["state"] == "watching" and "held" in out
    assert auto_tau(conn) is None

    # sample + acceptance -> enabled once
    seed_accesses(conn, reminded=15, fetched_after=5)
    out, r = rollup_and_tune(conn)
    assert r["reminded"] >= 20 and r["acceptance"] >= 0.15
    assert out.get("transition") == "inject_enabled"
    assert auto_tau(conn) == 0.5
    rows = conn.execute("SELECT data FROM journal WHERE event='autotune'").fetchall()
    assert any("inject_enabled" in r0[0] for r0 in rows)


def test_tuner_cooldown_and_disable(tmp_data, conn):
    seed_accesses(conn, reminded=25, fetched_after=10)
    now = int(time.time())
    out, _ = rollup_and_tune(conn, now=now)
    assert out.get("transition") == "inject_enabled"

    # collapse immediately: cooldown holds the tier
    conn.execute("DELETE FROM access_log WHERE event='fetched'")
    conn.commit()
    out, _ = rollup_and_tune(conn, now=now + 3600)
    assert out.get("transition") is None and auto_tau(conn) == 0.5

    # after the cooldown, sustained collapse hands the tier back
    out, r = rollup_and_tune(conn, now=now + 8 * 86400)
    if r["reminded"] >= 20:  # window still holds the reminded rows
        assert out.get("transition") == "inject_disabled"
        assert auto_tau(conn) is None


def test_gate_injects_under_tuned_tau(tmp_data, conn, tmp_path):
    # end-to-end: default config (tau_hi 9.9) + tuner-enabled meta -> a strong
    # hit INJECTS through the real prompt_gate subprocess
    from tests.test_gates import hook_env, hook_output, run_hook, seed_memory
    for i in range(200):
        conn.execute(
            "INSERT INTO memory(id, title, body, kind, scope_type, project, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (ids.ulid(), f"filler {i}", f"note about topic{i} and misc",
             "fact", "project", str(tmp_path), int(time.time())))
    mid = seed_memory(conn, "pytest here needs PYTHONPATH=src or imports fail",
                      project=str(tmp_path))
    db.log_access(conn, mid, "s0", "main", "synthetic", weight=3.0)
    evt = {"session_id": "s-tuned", "cwd": str(tmp_path),
           "prompt": "why does pytest fail with pythonpath import errors here",
           "hook_event_name": "UserPromptSubmit"}

    # remind-only before the tuner speaks
    proc = run_hook("prompt_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "UserPromptSubmit")
    assert out is not None
    assert "not loaded" in out["hookSpecificOutput"]["additionalContext"]

    conn.execute("INSERT INTO meta(key, value) VALUES ('auto_tau_hi', '0.50')")
    conn.commit()
    evt["session_id"] = "s-tuned-2"  # fresh dedup ledger
    proc = run_hook("prompt_gate.py", evt, hook_env(tmp_data))
    out = hook_output(proc, "UserPromptSubmit")
    assert out is not None
    assert "Reference notes retrieved" in out["hookSpecificOutput"]["additionalContext"]


def test_stats_shows_tier_posture(tmp_data, conn, tmp_path):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    env = dict(os.environ, HINDBRAIN_DATA=str(tmp_data),
               HINDBRAIN_SESSION="s-stats")
    env.pop("HINDBRAIN_DISABLE", None)
    r = subprocess.run([sys.executable, MEM, "stats"], capture_output=True,
                       text=True, cwd=str(proj), env=env, timeout=60)
    assert "remind-only; tuner watching" in r.stdout
    conn.execute("INSERT INTO meta(key, value) VALUES ('auto_tau_hi', '0.50')")
    conn.commit()
    r = subprocess.run([sys.executable, MEM, "stats"], capture_output=True,
                       text=True, cwd=str(proj), env=env, timeout=60)
    assert "ENABLED by tuner" in r.stdout and "0.5" in r.stdout
