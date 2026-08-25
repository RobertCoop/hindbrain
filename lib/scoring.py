"""Scoring and tier gate (spec 6). Activation is ACT-R base-level with a floor
so unaccessed memories can still surface (finding A3)."""
import math
import time

from lib import db, scopes


class Ctx:
    # plain class, not @dataclass: dataclasses drags in inspect/copy (~8 ms of
    # the hot-path import budget); constructor signature is contract-identical
    def __init__(self, session="", agent="main", cwd="", project="",
                 command_adjacent=False, file_path="", command="", tool_name=""):
        self.session = session
        self.agent = agent
        self.cwd = cwd
        self.project = project
        self.command_adjacent = command_adjacent
        self.file_path = file_path
        self.command = command
        self.tool_name = tool_name


def activation(events, now, cfg):
    d = cfg["scoring"]["act_decay_d"]
    total = 0.0
    for ts, weight in events:
        ddays = max(0.0, (now - ts) / 86400.0)
        total += weight * (ddays + 0.05) ** (-d)
    a = math.log(1.0 + total)
    return a / (a + 2.0)


def _auth_weight(hit, sc):
    return {
        "full": sc["auth_full"],
        "standard": sc["auth_standard"],
        "pending": sc["auth_pending"],
        "quarantined": sc["auth_quarantined"],
    }.get(hit.get("authority"), sc["auth_pending"])


def score(hit, ctx, cfg, act):
    sc = cfg["scoring"]
    bm25 = float(hit.get("bm25") or 0.0)
    r = max(0.0, -bm25)
    rel = r / (r + sc["rel_k"])
    # callers that pre-verified an exact scope hit against a specific target
    # (e.g. one of several file args in a bash command) stamp _scope_exact;
    # ctx carries only a single file_path so match() alone can't see those
    m = "exact" if hit.get("_scope_exact") else scopes.match(hit, ctx)
    # FTS5's IDF collapses to ~1e-6 in small corpora, zero-killing every match
    # (A3 by another route); a true match or exact scope hit gets a rel floor.
    if bm25 < 0.0 or m == "exact":
        rel = max(rel, sc.get("rel_match_floor", 0.3))
    s = rel * (sc["act_floor"] + (1.0 - sc["act_floor"]) * act) * _auth_weight(hit, sc)
    if m == "exact":
        s *= sc["boost_scope_exact"]
    elif m == "project":
        s *= sc["boost_project"]
    return s


def struggle_adjusted(cfg, st):
    th = cfg["thresholds"]["tau_hi"]
    tl = cfg["thresholds"]["tau_lo"]
    if (st.get("struggle") or {}).get("active"):
        f = cfg["thresholds"]["struggle_factor"]
        th *= f
        tl *= f
    return (th, tl)


def effective_taus(cfg, st, conn):
    # config is operator intent; meta.auto_tau_hi is learned state written by
    # the consolidator's tuner. min() means a hand-set tau_hi always wins in
    # the enabling direction, and [thresholds].auto_inject=false pins the
    # config value outright. Struggle factor applies after, as always.
    th = cfg["thresholds"]["tau_hi"]
    tl = cfg["thresholds"]["tau_lo"]
    if cfg["thresholds"].get("auto_inject", True) and conn is not None:
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='auto_tau_hi'").fetchone()
            if row is not None:
                th = min(th, float(row[0]))
        except Exception:
            pass
    if (st.get("struggle") or {}).get("active"):
        f = cfg["thresholds"]["struggle_factor"]
        th *= f
        tl *= f
    return (th, tl)


def _trim_to_char_budgets(inject, bud):
    each = bud["inject_max_chars_each"]
    total = bud["inject_max_chars_total"]
    for h in inject:
        body = h.get("body") or ""
        if len(body) > each:
            h["body"] = body[:each]
    while inject and sum(len(h.get("body") or "") for h in inject) > total:
        inject.remove(min(inject, key=lambda x: x["_score"]))
    return inject


def gate(hits, st, ctx, cfg, conn, taus, now=None):
    if now is None:
        now = int(time.time())
    th, tl = taus
    bud = cfg["budgets"]
    window = cfg["scoring"]["act_window"]
    dedup_i = set(st.get("injected") or [])
    dedup_r = set(st.get("reminded") or [])

    # scoped+fts unions can repeat an id; keep one copy carrying the best
    # (most negative) bm25 so a scope-only duplicate can't shadow real relevance
    merged = {}
    for h in hits:
        hid = h.get("id")
        if not hid:
            continue
        prev = merged.get(hid)
        if prev is None:
            merged[hid] = h
        elif float(h.get("bm25") or 0.0) < float(prev.get("bm25") or 0.0):
            merged[hid] = h

    scored = []
    for hid, h in merged.items():
        try:
            events = db.activation_events(conn, hid, window)
        except Exception:
            events = []
        h["_score"] = score(h, ctx, cfg, activation(events, now, cfg))
        scored.append(h)

    inject, remind = [], []

    def place(h, s):
        # one tier decision for direct hits and association spreads alike
        if h.get("status") != "active":
            return
        auth = h.get("authority")
        if ctx.command_adjacent and auth in ("pending", "quarantined"):
            return
        if auth == "quarantined":
            if s >= tl and h["id"] not in dedup_r and len(remind) < bud["remind_max_items"]:
                h["_unverified"] = True
                remind.append(h)
            return
        if s >= th and h["id"] not in dedup_i and len(inject) < bud["inject_max_items"]:
            inject.append(h)
        elif tl <= s < th and h["id"] not in dedup_r and len(remind) < bud["remind_max_items"]:
            remind.append(h)

    for h in sorted(scored, key=lambda x: x["_score"], reverse=True):
        place(h, h["_score"])

    # ---- association spread (related_to), one hop ----
    # a surfaced anchor activates its linked memories at anchor_score x
    # strength, run through the SAME tau tiering; linked hits never spread
    # further, and all authority/dedup/budget rules apply unchanged
    injected_ids = {h["id"] for h in inject}
    spreads = {}
    for anchor in list(inject) + list(remind):
        for other_id, strength, _src in _links(conn, anchor["id"]):
            if other_id in injected_ids or strength <= 0.0:
                continue
            s = anchor["_score"] * strength
            prev = spreads.get(other_id)
            if prev is None or s > prev[0]:
                spreads[other_id] = (s, anchor["id"])
    for other_id, (s, via) in sorted(spreads.items(), key=lambda kv: -kv[1][0]):
        if s < tl:
            continue
        in_remind = next((h for h in remind if h["id"] == other_id), None)
        if in_remind is not None:
            # a strong link promotes a directly-reminded partner into inject
            # (effective score = max(direct, spread))
            if (s >= th and s > in_remind["_score"]
                    and other_id not in dedup_i
                    and not in_remind.get("_unverified")
                    and not (ctx.command_adjacent
                             and in_remind.get("authority") in ("pending", "quarantined"))
                    and len(inject) < bud["inject_max_items"]):
                remind.remove(in_remind)
                in_remind["_score"] = s
                in_remind["_related_via"] = via
                inject.append(in_remind)
            continue
        m = _get_memory(conn, other_id)
        if m is None:
            continue
        m["_score"] = s
        m["_related_via"] = via
        place(m, s)

    return _trim_to_char_budgets(inject, bud), remind


def _links(conn, mem_id):
    try:
        return db.links_for(conn, mem_id)
    except Exception:
        return []


def _get_memory(conn, mem_id):
    try:
        return db.get_memory(conn, mem_id)
    except Exception:
        return None
