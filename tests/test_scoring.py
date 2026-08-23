"""Spec 13.1: activation math, the A3 zero-kill regression, floor behavior,
struggle tau adjustment, and gate invariants (dedup, budgets, quarantine,
command_adjacent exclusions)."""
import copy
import math
import time

import pytest

from lib import db as dbmod
from lib import paths, querybuild, scoring

NOW = int(time.time())


def _cfg():
    return copy.deepcopy(paths.load_config())


def _mem(mid, **kw):
    h = {
        "id": mid,
        "title": kw.get("body", "note")[:80],
        "body": "a body long enough to be plausible for this note",
        "kind": "fact",
        "scope_type": "project",
        "scope_value": "",
        "project": "/proj",
        "tags": "",
        "channel": "agent",
        "authority": "standard",
        "status": "active",
        "hazard": 0,
        "bm25": -20.0,
        "created_at": NOW,
    }
    h.update(kw)
    return h


def _insert(conn, h, synthetic_weight=None):
    conn.execute(
        "INSERT INTO memory(id,title,body,kind,scope_type,scope_value,project,"
        "tags,channel,authority,status,hazard,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (h["id"], h["title"], h["body"], h["kind"], h["scope_type"],
         h["scope_value"], h["project"], h["tags"], h["channel"],
         h["authority"], h["status"], h["hazard"], h["created_at"]))
    if synthetic_weight is not None:
        conn.execute(
            "INSERT INTO access_log(memory_id,session_id,agent_id,ts,event,weight) "
            "VALUES (?,?,?,?,?,?)",
            (h["id"], "s", "main", NOW, "synthetic", synthetic_weight))
    conn.commit()


def _ctx(**kw):
    kw.setdefault("session", "s")
    kw.setdefault("project", "/proj")
    return scoring.Ctx(**kw)


def _state():
    return {"injected": [], "reminded": [], "fetched": [], "denied": {}}


# ---- activation math (spec 6.2) ----

def test_activation_no_events_is_zero(tmp_data):
    cfg = _cfg()
    assert scoring.activation([], NOW, cfg) == 0.0


def test_activation_single_event_matches_formula(tmp_data):
    cfg = _cfg()
    d = cfg["scoring"]["act_decay_d"]
    w = 3.0
    a = math.log(1.0 + w * 0.05 ** (-d))  # delta-days = 0
    expected = a / (a + 2.0)
    assert scoring.activation([(NOW, w)], NOW, cfg) == pytest.approx(expected)


def test_activation_decays_with_age(tmp_data):
    cfg = _cfg()
    fresh = scoring.activation([(NOW, 1.0)], NOW, cfg)
    old = scoring.activation([(NOW - 30 * 86400, 1.0)], NOW, cfg)
    assert 0.0 < old < fresh < 1.0


def test_activation_monotonic_in_events(tmp_data):
    cfg = _cfg()
    one = scoring.activation([(NOW, 1.0)], NOW, cfg)
    many = scoring.activation([(NOW - i * 3600, 1.0) for i in range(10)], NOW, cfg)
    assert many > one


def test_activation_future_ts_clamped(tmp_data):
    cfg = _cfg()
    # a clock-skewed future event must not blow up (negative delta clamped to 0)
    v = scoring.activation([(NOW + 9999, 1.0)], NOW, cfg)
    assert 0.0 < v < 1.0


# ---- floor behavior (spec 6.4 / finding A3) ----

def test_score_floor_zero_activation(tmp_data):
    cfg = _cfg()
    sc = cfg["scoring"]
    h = _mem("m1", project="/other")  # no scope/project boost
    got = scoring.score(h, _ctx(), cfg, act=0.0)
    r = 20.0
    rel = max(r / (r + sc["rel_k"]), sc["rel_match_floor"])
    assert got == pytest.approx(rel * sc["act_floor"] * sc["auth_standard"])


def test_score_never_zero_killed_by_activation(tmp_data):
    cfg = _cfg()
    h = _mem("m1", authority="full")
    assert scoring.score(h, _ctx(), cfg, act=0.0) > 0.0


def test_a3_regression_new_memory_clears_tau_hi(tmp_data):
    # THE A3 REGRESSION (pure math): brand-new memory, one synthetic prior
    # access, strong rel, authority full -> score >= 0.50 with no boosts.
    cfg = _cfg()
    act = scoring.activation([(NOW, cfg["scoring"]["prior_p0"])], NOW, cfg)
    h = _mem("m1", authority="full", project="/other", bm25=-100.0)
    assert scoring.score(h, _ctx(), cfg, act) >= 0.50


def test_a3_regression_end_to_end(conn):
    # Same regression through the real store: FTS search -> gate at
    # tau_hi=0.50 must inject the fresh, never-accessed-by-user memory.
    fillers = ["alpha bravo charlie delta", "echo foxtrot golf hotel",
               "india juliet kilo lima", "mike november oscar papa",
               "quebec romeo sierra tango", "uniform victor whiskey xray",
               "yankee zulu apple banana", "cherry damson elder fig",
               "grape honeydew iris jasmine", "kale lemon mango nectar",
               "olive peach quince radish", "sage thyme umber violet"]
    for i, words in enumerate(fillers):
        _insert(conn, _mem(f"filler{i:02d}", body=f"unrelated note about {words}"))
    body = "pytest here needs PYTHONPATH=src; bare pytest fails on conftest imports"
    target = _mem("target01", body=body, kind="gotcha", authority="full",
                  scope_type="command", scope_value="pytest")
    _insert(conn, target, synthetic_weight=3.0)

    q = querybuild.fts_query("pytest fails with conftest import errors PYTHONPATH")
    hits = dbmod.search(conn, q, "/proj")
    assert any(h["id"] == "target01" for h in hits)
    cfg = _cfg()
    inject, remind = scoring.gate(hits, _state(), _ctx(command="pytest -x"),
                                  cfg, conn, (0.50, 0.25), now=NOW)
    assert any(h["id"] == "target01" for h in inject)


def test_scope_and_project_boosts(tmp_data):
    cfg = _cfg()
    sc = cfg["scoring"]
    base = _mem("m1", project="/other")
    proj = _mem("m2", project="/proj")
    exact = _mem("m3", project="/proj", scope_type="command", scope_value="pytest")
    ctx = _ctx(command="pytest -x")
    s_base = scoring.score(base, ctx, cfg, 0.5)
    s_proj = scoring.score(proj, ctx, cfg, 0.5)
    s_exact = scoring.score(exact, ctx, cfg, 0.5)
    assert s_proj == pytest.approx(s_base * sc["boost_project"])
    assert s_exact == pytest.approx(s_base * sc["boost_scope_exact"])


# ---- struggle tau adjustment (spec 8.3) ----

def test_struggle_adjusted_inactive(tmp_data):
    cfg = _cfg()
    st = {"struggle": {"active": False}}
    assert scoring.struggle_adjusted(cfg, st) == (
        cfg["thresholds"]["tau_hi"], cfg["thresholds"]["tau_lo"])


def test_struggle_adjusted_active(tmp_data):
    cfg = _cfg()
    f = cfg["thresholds"]["struggle_factor"]
    st = {"struggle": {"active": True}}
    th, tl = scoring.struggle_adjusted(cfg, st)
    assert th == pytest.approx(cfg["thresholds"]["tau_hi"] * f)
    assert tl == pytest.approx(cfg["thresholds"]["tau_lo"] * f)


def test_struggle_adjusted_missing_key(tmp_data):
    cfg = _cfg()
    th, tl = scoring.struggle_adjusted(cfg, {})
    assert (th, tl) == (cfg["thresholds"]["tau_hi"], cfg["thresholds"]["tau_lo"])


# ---- gate: dedup, budgets, quarantine, command_adjacent ----

def test_gate_dedup_injected(conn):
    cfg = _cfg()
    hits = [_mem("m1", authority="full")]
    st = _state()
    st["injected"] = ["m1"]
    inject, remind = scoring.gate(hits, st, _ctx(), cfg, conn, (0.1, 0.05), now=NOW)
    assert inject == []
    # already-injected id must not fall back into remind either? spec keeps
    # remind dedup separate: score above tau_hi and deduped -> silent.
    assert all(h["id"] != "m1" for h in remind)


def test_gate_dedup_reminded(conn):
    cfg = _cfg()
    hits = [_mem("m1")]
    st = _state()
    st["reminded"] = ["m1"]
    inject, remind = scoring.gate(hits, st, _ctx(), cfg, conn, (9.9, 0.05), now=NOW)
    assert remind == [] and inject == []


def test_gate_duplicate_ids_collapse(conn):
    cfg = _cfg()
    hits = [_mem("m1"), _mem("m1")]  # scoped + fts union repeats
    inject, remind = scoring.gate(hits, _state(), _ctx(), cfg, conn, (0.1, 0.05), now=NOW)
    assert [h["id"] for h in inject] == ["m1"]


def test_gate_inject_item_budget(conn):
    cfg = _cfg()
    hits = [_mem(f"m{i}", authority="full") for i in range(6)]
    inject, remind = scoring.gate(hits, _state(), _ctx(), cfg, conn, (0.1, 0.05), now=NOW)
    assert len(inject) == cfg["budgets"]["inject_max_items"]


def test_gate_remind_item_budget(conn):
    cfg = _cfg()
    hits = [_mem(f"m{i}") for i in range(8)]
    inject, remind = scoring.gate(hits, _state(), _ctx(), cfg, conn, (9.9, 0.05), now=NOW)
    assert len(remind) == cfg["budgets"]["remind_max_items"]


def test_gate_char_budgets(conn):
    cfg = _cfg()
    each = cfg["budgets"]["inject_max_chars_each"]
    cfg["budgets"]["inject_max_chars_total"] = 2 * each
    hits = [_mem(f"m{i}", body="x" * (each + 500), bm25=-20.0 - i) for i in range(3)]
    inject, _ = scoring.gate(hits, _state(), _ctx(), cfg, conn, (0.1, 0.05), now=NOW)
    assert all(len(h["body"]) <= each for h in inject)
    assert sum(len(h["body"]) for h in inject) <= cfg["budgets"]["inject_max_chars_total"]
    assert len(inject) == 2  # lowest-scored dropped to fit total budget
    kept = {h["id"] for h in inject}
    assert kept == {"m1", "m2"}  # m0 had the weakest bm25


def test_gate_quarantine_remind_only(conn):
    cfg = _cfg()
    # perfect relevance, but quarantined -> remind tier only, flagged
    hits = [_mem("q1", authority="quarantined", bm25=-1000.0)]
    inject, remind = scoring.gate(hits, _state(), _ctx(), cfg, conn, (0.1, 0.05), now=NOW)
    assert inject == []
    assert len(remind) == 1
    assert remind[0]["id"] == "q1"
    assert remind[0].get("_unverified") is True


def test_gate_quarantine_dedup_and_budget(conn):
    cfg = _cfg()
    st = _state()
    st["reminded"] = ["q1"]
    hits = [_mem("q1", authority="quarantined", bm25=-1000.0)]
    inject, remind = scoring.gate(hits, st, _ctx(), cfg, conn, (0.1, 0.05), now=NOW)
    assert inject == [] and remind == []


def test_gate_command_adjacent_excludes_pending_and_quarantined(conn):
    cfg = _cfg()
    hits = [_mem("p1", authority="pending", bm25=-1000.0),
            _mem("q1", authority="quarantined", bm25=-1000.0),
            _mem("s1", authority="standard", bm25=-1000.0)]
    ctx = _ctx(command_adjacent=True, command="git push")
    inject, remind = scoring.gate(hits, _state(), ctx, cfg, conn, (0.1, 0.05), now=NOW)
    surfaced = {h["id"] for h in inject} | {h["id"] for h in remind}
    assert "p1" not in surfaced and "q1" not in surfaced
    assert "s1" in surfaced


def test_gate_non_command_adjacent_allows_pending_inject(conn):
    cfg = _cfg()
    hits = [_mem("p1", authority="pending", bm25=-1000.0)]
    inject, remind = scoring.gate(hits, _state(), _ctx(), cfg, conn, (0.1, 0.05), now=NOW)
    assert [h["id"] for h in inject] == ["p1"]


def test_gate_skips_non_active(conn):
    cfg = _cfg()
    hits = [_mem("m1", status="superseded", bm25=-1000.0),
            _mem("m2", status="refuted", bm25=-1000.0),
            _mem("m3", status="expired", bm25=-1000.0)]
    inject, remind = scoring.gate(hits, _state(), _ctx(), cfg, conn, (0.1, 0.05), now=NOW)
    assert inject == [] and remind == []


def test_gate_scores_attached(conn):
    cfg = _cfg()
    hits = [_mem("m1", bm25=-1000.0)]
    inject, remind = scoring.gate(hits, _state(), _ctx(), cfg, conn, (0.1, 0.05), now=NOW)
    assert isinstance(inject[0]["_score"], float) and inject[0]["_score"] > 0


def test_gate_mid_band_reminds(conn):
    cfg = _cfg()
    hits = [_mem("m1", bm25=-1000.0)]
    inject, remind = scoring.gate(hits, _state(), _ctx(), cfg, conn, (9.9, 0.05), now=NOW)
    assert inject == [] and [h["id"] for h in remind] == ["m1"]
