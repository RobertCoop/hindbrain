"""Consolidator KPI passes (spec §10 pass 7/8, §14): deny rate per 100 Bash
calls, rolling p95 gate latency, and the graduation coverage rule — plus the
M5 pass fixtures: idempotence, expiry, dedup, promotion, GC."""
import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEM = os.path.join(ROOT, "bin", "mem")

from consolidator import consolidate
from tests.test_gates import seed_candidate, seed_memory

DAY = 86400


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    return str(p)


def seed_mem(conn, *, title, body, project="", scope_type="project"):
    from lib import ids
    mid = ids.ulid()
    conn.execute(
        "INSERT INTO memory(id, title, body, kind, scope_type, project, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (mid, title, body, "fact", scope_type, project, int(time.time())))
    conn.commit()
    return mid


def log_access(conn, mid, session, event, ts=None):
    conn.execute(
        "INSERT INTO access_log(memory_id, session_id, agent_id, ts, event, weight) "
        "VALUES (?,?,?,?,?,1.0)", (mid, session, "main", ts or int(time.time()), event))
    conn.commit()


def journal_bash(conn, session, event, n):
    from lib import db
    for _ in range(n):
        db.journal(conn, session, "main", event, {"tool": "Bash", "cmd_head": "git"})


def test_deny_rate_per_100_bash(tmp_data, conn, proj):
    from lib import paths
    cfg = paths.load_config()
    now = int(time.time())
    mid = seed_mem(conn, title="hazard note title here",
                   body="a hazard note body long enough to matter", project=proj)
    journal_bash(conn, "s1", "tool_ok", 38)
    journal_bash(conn, "s1", "tool_fail", 2)
    # non-Bash journal rows must not count toward the denominator
    from lib import db as libdb
    libdb.journal(conn, "s1", "main", "tool_ok", {"file": "x.py"})
    for _ in range(2):
        log_access(conn, mid, "s1", "denied")

    rollup = consolidate.metrics_rollup(conn, cfg, now)
    assert rollup["bash_calls"] == 40
    assert rollup["deny_rate"] == pytest.approx(5.0)  # 2 denies / 40 calls * 100
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["deny_rate_per_100_bash"] == "5.00"

    # alert lands in the promotions report when rate > 1
    n = consolidate.promotions_report(cfg, [], rollup)
    assert n >= 1
    reports = os.path.join(tmp_data, "reports")
    path = [os.path.join(reports, f) for f in os.listdir(reports)
            if f.startswith("promotions-")][0]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert "Deny rate = 5.00 per 100 Bash calls" in text

    # zero Bash calls -> rate 0.0, no alert
    conn.execute("DELETE FROM journal")
    conn.commit()
    rollup = consolidate.metrics_rollup(conn, cfg, now)
    assert rollup["bash_calls"] == 0 and rollup["deny_rate"] == 0.0


def test_p95_gate_latency_from_metrics(tmp_data, conn, proj):
    from lib import paths
    cfg = paths.load_config()
    logs = os.path.join(tmp_data, "logs")
    os.makedirs(logs, mode=0o700, exist_ok=True)
    with open(os.path.join(logs, "metrics.jsonl"), "w", encoding="utf-8") as f:
        for i in range(1, 101):
            f.write(json.dumps({"hook": "prompt_gate", "dur_ms": i}) + "\n")
        f.write(json.dumps({"hook": "observer", "dur_ms": 9999}) + "\n")
        f.write(json.dumps({"hook": "prompt_gate"}) + "\n")  # no dur_ms
        f.write("not json\n")

    rollup = consolidate.metrics_rollup(conn, cfg, int(time.time()))
    assert rollup["p95_gate_ms"] == pytest.approx(95.0)
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["p95_gate_ms"] == "95"

    # documented (with method) in the report whenever one is written
    rollup["deny_rate"] = 2.0  # force an alert so the report exists
    consolidate.promotions_report(cfg, [], rollup)
    reports = os.path.join(tmp_data, "reports")
    path = [os.path.join(reports, f) for f in os.listdir(reports)
            if f.startswith("promotions-")][0]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert "p95 = 95 ms" in text and "metrics.jsonl" in text


def test_p95_absent_without_metrics_file(tmp_data, conn, proj):
    from lib import paths
    cfg = paths.load_config()
    rollup = consolidate.metrics_rollup(conn, cfg, int(time.time()))
    assert rollup["p95_gate_ms"] is None
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert "p95_gate_ms" not in meta


def test_mem_stats_shows_kpis(tmp_data, conn, proj):
    for k, v in (("deny_rate_per_100_bash", "5.00"), ("p95_gate_ms", "42")):
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (k, v))
    conn.commit()
    env = os.environ.copy()
    env["HINDBRAIN_DATA"] = tmp_data
    env.pop("HINDBRAIN_DB", None)
    env.pop("HINDBRAIN_DISABLE", None)
    r = subprocess.run([sys.executable, MEM, "stats"], capture_output=True,
                       text=True, cwd=proj, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "deny rate (30d): 5.00 per 100 Bash calls" in r.stdout
    assert "p95 gate latency: 42 ms" in r.stdout


def test_graduation_two_sessions_suffice(tmp_data, conn, proj):
    from lib import paths
    cfg = paths.load_config()
    now = int(time.time())
    mid = seed_mem(conn, title="graduation candidate note",
                   body="a note used in every session of this project", project=proj)
    log_access(conn, mid, "g1", "injected", ts=now - 100)
    log_access(conn, mid, "g2", "fetched", ts=now - 50)
    grad = consolidate.graduation_proposals(conn, cfg, now)
    assert len(grad["proposals"]) == 1
    assert mid in grad["proposals"][0]
    assert "2 of the last 2 sessions" in grad["proposals"][0]


def test_graduation_skips_single_session_and_low_coverage(tmp_data, conn, proj):
    from lib import paths
    cfg = paths.load_config()
    now = int(time.time())
    # degenerate single-session case: no coverage claim possible
    solo = seed_mem(conn, title="single session note",
                    body="seen once in one session only", project=proj)
    log_access(conn, solo, "g1", "injected", ts=now - 100)
    grad = consolidate.graduation_proposals(conn, cfg, now)
    assert grad["proposals"] == []

    # reminds register session presence but not coverage: 1 of 3 sessions
    # injected/fetched -> below 50%, still no proposal
    log_access(conn, solo, "g2", "reminded", ts=now - 40)
    log_access(conn, solo, "g3", "reminded", ts=now - 30)
    grad = consolidate.graduation_proposals(conn, cfg, now)
    assert all(solo not in p for p in grad["proposals"])


# ---- M5 pass fixtures (spec §10 passes 1/2/3/6/9): run twice, expected
# states, idempotence, and the never-writes-CLAUDE.md invariant ----

def _seed_m5_fixture(conn, proj, now):
    ids = {}
    # pass 2 expiry: env kind past its [decay] ttl (45d)
    ids["env_old"] = seed_memory(
        conn, "old environment note about the proxy endpoint address",
        kind="env", created_at=now - 60 * DAY)
    # pass 2 refresh-on-use: gotcha past its row ttl but recently used
    ids["gotcha"] = seed_memory(
        conn, "gotcha with an explicit ttl kept alive by recent use",
        kind="gotcha", ttl_days=30, created_at=now - 60 * DAY,
        last_access_at=now - 2 * DAY)
    # pass 3 dedup: near-identical actives in the same (project, kind);
    # keeper = higher authority, tags merged, corroborations summed
    ids["keep"] = seed_memory(
        conn, "the staging database lives on host quartz and resets nightly",
        kind="fact", project=proj, authority="standard", tags="one,two",
        corroborations=1, created_at=now - 10 * DAY)
    ids["drop"] = seed_memory(
        conn, "the staging database lives on host quartz and resets nightly",
        kind="fact", project=proj, authority="pending", tags="two,three",
        corroborations=2, created_at=now - 5 * DAY)
    # pass 6 promotion: pending with corroborations >= 2
    ids["pend"] = seed_memory(
        conn, "pending decision corroborated twice about the cache layer",
        kind="decision", authority="pending", corroborations=2)
    # pass 6 promotion: quarantined with 2 distinct-session corroborations
    ids["quar"] = seed_memory(
        conn, "quarantined external note about the queue broker retries",
        kind="fact", authority="quarantined")
    for sess in ("q1", "q2"):
        conn.execute(
            "INSERT INTO access_log(memory_id, session_id, agent_id, ts, "
            "event, weight) VALUES (?,?,'main',?,'cited',1.0)",
            (ids["quar"], sess, now - DAY))
    # pass 1 GC: open beyond session+carry, and a stale carried row
    ids["c_open"] = seed_candidate(conn, "s-m5-old", ts=now - 8 * DAY)
    ids["c_carried"] = seed_candidate(conn, "s-m5-carried", ts=now - 2 * DAY,
                                      status="carried")
    conn.commit()
    return ids


def test_consolidator_pass_effects(tmp_data, conn, proj, tmp_path):
    now = int(time.time())
    ids = _seed_m5_fixture(conn, proj, now)
    consolidate.run()

    mem = {m["id"]: dict(m) for m in conn.execute("SELECT * FROM memory")}
    # expiry: env past its kind ttl -> expired with invalidated_at set
    assert mem[ids["env_old"]]["status"] == "expired"
    assert mem[ids["env_old"]]["invalidated_at"] is not None
    # gotcha refresh-on-use: recent last_access resets the ttl clock
    assert mem[ids["gotcha"]]["status"] == "active"
    # dedup: keeper survives with merged tags and summed corroborations
    keep, drop = mem[ids["keep"]], mem[ids["drop"]]
    assert keep["status"] == "active"
    assert drop["status"] == "superseded" and drop["invalidated_at"] is not None
    assert set(keep["tags"].split(",")) == {"one", "two", "three"}
    assert keep["corroborations"] == 3
    assert keep["supersedes"] == ids["drop"]
    # promotion sweep
    assert mem[ids["pend"]]["authority"] == "standard"
    assert mem[ids["quar"]]["authority"] == "standard"
    # GC: both stale candidates expired
    for cid in (ids["c_open"], ids["c_carried"]):
        assert conn.execute("SELECT status FROM candidate WHERE id=?",
                            (cid,)).fetchone()[0] == "expired"
    # meta.last_consolidation set
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert int(float(meta["last_consolidation"])) >= now
    # spec §16: never writes CLAUDE.md (or any auto memory file) anywhere
    for root, _dirs, files in os.walk(str(tmp_path)):
        assert "CLAUDE.md" not in files, root


def _snapshot(conn):
    mem = [tuple(r) for r in conn.execute("SELECT * FROM memory ORDER BY id")]
    cand = [tuple(r) for r in
            conn.execute("SELECT * FROM candidate ORDER BY id")]
    meta = {r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM meta")}
    meta.pop("last_consolidation", None)  # timestamp; refreshed every run
    return mem, cand, meta


def test_consolidator_idempotent_on_double_run(tmp_data, conn, proj):
    now = int(time.time())
    _seed_m5_fixture(conn, proj, now)
    consolidate.run()
    before = _snapshot(conn)
    summary2 = consolidate.run()
    assert _snapshot(conn) == before
    assert summary2["gc"]["candidates_expired"] == 0
    assert summary2["expiry"] == {"expired": 0}
    assert summary2["dedup"] == {"superseded": 0}
    assert summary2["promotion"] == {"promoted": 0}
