"""mem doctor: KPI rollup + substrate liveness inferred from collected data."""
import json
import os
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEM = os.path.join(REPO, "bin", "mem")
sys.path.insert(0, REPO)

from lib import db, doctor, ids, paths  # noqa: E402


def write_metric(tmp_data, hook, dur_ms=50, ago=0):
    p = os.path.join(str(tmp_data), "logs", "metrics.jsonl")
    with open(p, "a") as f:
        f.write(json.dumps({"ts": int(time.time()) - ago, "hook": hook,
                            "dur_ms": dur_ms}) + "\n")


def run_doc(conn, session="s-doc", project="/p"):
    return doctor.run_doctor(conn, paths.load_config(), session, project)


def levels(report):
    return {c["text"].split(":")[0] + "|" + c["level"] for c in report["checks"]}


def test_no_metrics_is_a_failure(tmp_data, conn):
    r = run_doc(conn)
    assert r["failures"] == 1
    assert any("hooks are not running" in c["text"] for c in r["checks"])


def test_missing_hooks_named_with_consequence(tmp_data, conn):
    for h in ("session_start", "prompt_gate", "pretool_gate", "observer",
              "stop_gate", "session_end"):
        write_metric(tmp_data, h)
    r = run_doc(conn)
    texts = " | ".join(c["text"] for c in r["checks"])
    assert "hook never seen: failure_gate" in texts
    assert "failure-text retrieval" in texts
    # optional precompact_salvage never warned about
    assert "precompact_salvage" not in texts


def test_witness_and_binding_checks(tmp_data, conn, monkeypatch):
    write_metric(tmp_data, "prompt_gate")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live-sess")
    # stale binding shape: no turns for this session, recent project turns exist
    db.journal(conn, "other-sess", "main", "user_prompt",
               {"text": "hello there", "project": "/p"})
    r = run_doc(conn, session="s-doc", project="/p")
    texts = " | ".join(c["text"] for c in r["checks"])
    assert "CLAUDE_CODE_SESSION_ID" in texts
    assert "0 user turns journaled" in texts and "binding may be stale" in texts

    db.journal(conn, "s-doc", "main", "user_prompt",
               {"text": "real turn", "project": "/p"})
    r = run_doc(conn, session="s-doc", project="/p")
    assert any("1 user turn(s) journaled" in c["text"] for c in r["checks"])


def test_store_hygiene_warnings(tmp_data, conn):
    write_metric(tmp_data, "prompt_gate")
    now = int(time.time())
    hz = ids.ulid()
    conn.execute(
        "INSERT INTO memory(id, title, body, kind, scope_type, scope_value, "
        "project, authority, hazard, created_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
        (hz, "unarmed hazard", "force push protection note body here",
         "gotcha", "command", "git.push", "/p", "pending", now))
    hub = ids.ulid()
    others = []
    conn.execute(
        "INSERT INTO memory(id, title, body, kind, scope_type, project, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (hub, "hub note", "a very connected note body", "fact", "project",
         "/p", now))
    for i in range(4):
        o = ids.ulid()
        others.append(o)
        conn.execute(
            "INSERT INTO memory(id, title, body, kind, scope_type, project, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (o, f"leaf {i}", f"leaf note body number {i}", "fact", "project",
             "/p", now))
        db.upsert_link(conn, hub, o, 0.4)
    conn.execute(
        "INSERT INTO candidate(id, session_id, ts, priority, signal, payload) "
        "VALUES (?,?,?,?,?,?)",
        (ids.ulid(), "s-old", now - 2 * 86400, "P2", "learned_fix", "{}"))
    conn.commit()
    r = run_doc(conn)
    texts = " | ".join(c["text"] for c in r["checks"])
    assert "UNARMED" in texts and hz[:10] in texts
    assert "link hub" in texts
    assert "open >24h" in texts


def test_cli_doctor_text_and_json(tmp_data, conn, tmp_path):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    write_metric(tmp_data, "prompt_gate", dur_ms=120)  # over budget
    env = dict(os.environ, HINDBRAIN_DATA=str(tmp_data),
               HINDBRAIN_SESSION="s-cli")
    env.pop("HINDBRAIN_DISABLE", None)
    r = subprocess.run([sys.executable, MEM, "doctor"], capture_output=True,
                       text=True, cwd=str(proj), env=env, timeout=60)
    assert r.returncode == 0
    assert r.stdout.startswith("hindbrain doctor")
    assert "verdict:" in r.stdout
    assert "p95=120ms" in r.stdout  # latency breach surfaced
    assert len(r.stdout.strip().splitlines()) <= 30

    r = subprocess.run([sys.executable, MEM, "doctor", "--json"],
                       capture_output=True, text=True, cwd=str(proj), env=env,
                       timeout=60)
    d = json.loads(r.stdout)
    assert {"checks", "warnings", "failures"} <= set(d)
