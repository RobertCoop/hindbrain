"""Associative memory: related_to links, tau-tiered activation spread."""
import json
import os
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEM = os.path.join(REPO, "bin", "mem")
sys.path.insert(0, REPO)

from lib import db, ids, paths, scoring  # noqa: E402


def seed(conn, body, title=None, project="/p", authority="full", **kw):
    mid = ids.ulid()
    row = {"id": mid, "title": (title or body)[:80], "body": body,
           "kind": "gotcha", "scope_type": "project", "scope_value": "",
           "project": project, "authority": authority,
           "created_at": int(time.time())}
    row.update(kw)
    cols = ",".join(row)
    conn.execute(f"INSERT INTO memory({cols}) VALUES "
                 f"({','.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    return mid


def fill_corpus(conn, n=200):
    now = int(time.time())
    for i in range(n):
        conn.execute(
            "INSERT INTO memory(id, title, body, kind, scope_type, project, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (ids.ulid(), f"filler {i}", f"note about topic{i} and misc stuff",
             "fact", "project", "/p", now))
    conn.commit()


def gate_for(conn, cfg, query, st=None, taus=(0.5, 0.25), adjacent=False):
    hits = db.search(conn, query, "/p")
    ctx = scoring.Ctx(session="s", project="/p", command_adjacent=adjacent)
    st = st or {"injected": [], "reminded": [], "fetched": [], "denied": {},
                "struggle": {"active": False}}
    return scoring.gate(hits, st, ctx, cfg, conn, taus, now=int(time.time()))


def test_link_crud_bidirectional(conn):
    a = seed(conn, "anchor note about the flux capacitor calibration")
    b = seed(conn, "partner note about the plutonium loading procedure")
    assert db.upsert_link(conn, a, b, 0.7) == "created"
    assert db.upsert_link(conn, b, a, 0.9) == "updated"  # same pair, reversed
    assert db.links_for(conn, a) == [(b, 0.9, "cli")]
    assert db.links_for(conn, b) == [(a, 0.9, "cli")]
    # consolidator never clobbers cli
    assert db.upsert_link(conn, a, b, 0.3, source="consolidator") == "kept"
    assert db.links_for(conn, a)[0][1] == 0.9
    assert db.delete_link(conn, b, a) is True
    assert db.links_for(conn, a) == []


def test_spread_tiers_by_strength(tmp_data, conn):
    cfg = paths.load_config()
    fill_corpus(conn)
    anchor = seed(conn, "pytest here needs PYTHONPATH=src; imports fail without it")
    strong = seed(conn, "tox wraps pytest in this repo with the same pythonpath fix")
    mid_ = seed(conn, "coverage config quirk lives in setup.cfg not pyproject")
    weak = seed(conn, "an unrelated deployment ordering note for the worker")
    db.upsert_link(conn, anchor, strong, 0.95)
    db.upsert_link(conn, anchor, mid_, 0.45)
    db.upsert_link(conn, anchor, weak, 0.10)
    db.log_access(conn, anchor, "s0", "main", "synthetic", weight=3.0)

    inject, remind = gate_for(conn, cfg, '"pytest" OR "pythonpath" OR "imports"')
    inj_ids = {h["id"] for h in inject}
    rem_ids = {h["id"] for h in remind}
    assert anchor in inj_ids                      # direct hit
    assert strong in inj_ids                      # 0.95 x anchor >= tau_hi
    assert mid_ in rem_ids                        # mid strength -> remind
    assert weak not in inj_ids | rem_ids          # below tau_lo
    via = [h for h in remind if h["id"] == mid_][0]
    assert via["_related_via"] == anchor


def test_spread_respects_authority_and_dedup(tmp_data, conn):
    cfg = paths.load_config()
    fill_corpus(conn)
    anchor = seed(conn, "pytest here needs PYTHONPATH=src; imports fail without it")
    pend = seed(conn, "an unproven partner note about the test harness",
                authority="pending")
    db.upsert_link(conn, anchor, pend, 0.95)
    db.log_access(conn, anchor, "s0", "main", "synthetic", weight=3.0)

    # command-adjacent: pending partner must NOT spread in
    inject, remind = gate_for(conn, cfg, '"pytest" OR "pythonpath" OR "imports"',
                              adjacent=True)
    assert pend not in {h["id"] for h in inject + remind}

    # dedup: an already-injected partner never re-surfaces
    st = {"injected": [pend], "reminded": [], "fetched": [], "denied": {},
          "struggle": {"active": False}}
    inject, remind = gate_for(conn, cfg, '"pytest" OR "pythonpath" OR "imports"',
                              st=st)
    assert pend not in {h["id"] for h in inject + remind}

    # a remind-band spread is deduped against the reminded ledger (a
    # reminded id may still ESCALATE to inject — that's the S1 arc — but
    # never re-reminds)
    db.upsert_link(conn, anchor, pend, 0.4)
    st = {"injected": [], "reminded": [pend], "fetched": [], "denied": {},
          "struggle": {"active": False}}
    inject, remind = gate_for(conn, cfg, '"pytest" OR "pythonpath" OR "imports"',
                              st=st)
    assert pend not in {h["id"] for h in remind}


def test_cli_link_commands(tmp_data, conn, tmp_path):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    env = dict(os.environ, HINDBRAIN_DATA=str(tmp_data),
               HINDBRAIN_SESSION="s-link")
    env.pop("HINDBRAIN_DB", None)
    env.pop("HINDBRAIN_DISABLE", None)

    def cli(*args):
        return subprocess.run([sys.executable, MEM, *args],
                              capture_output=True, text=True, cwd=str(proj),
                              env=env, timeout=60)

    a = seed(conn, "anchor note about alembic migration ordering",
             project=str(proj))
    b = seed(conn, "partner note about the sqlite wal checkpoint quirk",
             project=str(proj))
    r = cli("link", a[:20], b, "--strength", "0.8")  # prefix long enough to
    assert r.returncode == 0 and "created link" in r.stdout  # beat shared ms
    r = cli("links", a)
    assert b in r.stdout and "0.80" in r.stdout
    r = cli("get", a)
    assert "related:" in r.stdout and b in r.stdout
    r = cli("audit", a)
    assert "related_to:" in r.stdout
    r = cli("link", a, a)
    assert r.returncode == 1  # self-link refused
    r = cli("link", a, b, "--strength", "7")  # clamped
    assert "strength=1.00" in r.stdout
    r = cli("unlink", a, b)
    assert r.returncode == 0 and "unlinked" in r.stdout
    r = cli("unlink", a, b)
    assert r.returncode == 1


def test_consolidator_coactivation(tmp_data, conn):
    sys.path.insert(0, REPO)
    from consolidator import consolidate
    a = seed(conn, "co-firing note alpha about the deploy pipeline")
    b = seed(conn, "co-firing note beta about the vault address")
    c = seed(conn, "loner note that fires alone in one session")
    for sid in ("s1", "s2", "s3"):
        db.log_access(conn, a, sid, "main", "injected")
        db.log_access(conn, b, sid, "main", "reminded")
    db.log_access(conn, c, "s9", "main", "injected")
    # a cli link that the pass must not clobber
    d = seed(conn, "explicitly linked note gamma for the same pipeline")
    db.upsert_link(conn, a, d, 0.9, source="cli")

    out = consolidate.coactivation(conn, paths.load_config(), int(time.time()))
    assert out["links_upserted"] >= 1
    links = dict((o, s) for o, s, _ in db.links_for(conn, a))
    assert b in links and links[b] == pytest.approx(0.3)  # 3 sessions
    assert links[d] == 0.9                                # cli kept
    assert db.links_for(conn, c) == []

    # links to non-active memories are dropped
    conn.execute("UPDATE memory SET status='refuted' WHERE id=?", (b,))
    conn.commit()
    out = consolidate.coactivation(conn, paths.load_config(), int(time.time()))
    assert b not in {o for o, _, _ in db.links_for(conn, a)}


def test_link_strength_dynamics(tmp_data, conn):
    from consolidator import consolidate
    cfg = paths.load_config()
    now = int(time.time())
    a = seed(conn, "note alpha about the vault deploy sequence")
    b = seed(conn, "note beta about the vault token refresh quirk")

    # reinforcement: co-firing with FETCHES beats plain co-firing
    for sid in ("d1", "d2", "d3"):
        db.log_access(conn, a, sid, "main", "injected")
        db.log_access(conn, b, sid, "main", "fetched")
    consolidate.coactivation(conn, cfg, now)
    s1 = db.links_for(conn, a)[0][1]
    assert s1 == pytest.approx(0.2 + 0.1 * 1 + 0.05 * 3)  # 0.45

    # decay: with the evidence aged out of the window, strength shrinks 30%
    conn.execute("UPDATE access_log SET ts = ts - 40*86400")
    conn.commit()
    consolidate.coactivation(conn, cfg, now)
    s2 = db.links_for(conn, a)[0][1]
    assert s2 == pytest.approx(s1 * 0.7)

    # repeated dormancy drops the link entirely
    for _ in range(4):
        consolidate.coactivation(conn, cfg, now)
    assert db.links_for(conn, a) == []

    # cli links are pinned: never decayed
    db.upsert_link(conn, a, b, 0.8, source="cli")
    consolidate.coactivation(conn, cfg, now)
    assert db.links_for(conn, a) == [(b, 0.8, "cli")]
