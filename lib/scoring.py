"""Scoring and tier gate (spec 6). Activation is ACT-R base-level with a floor
so unaccessed memories can still surface (finding A3)."""
import math
import time
from dataclasses import dataclass

from lib import db, scopes


@dataclass
class Ctx:
    session: str = ""
    agent: str = "main"
    cwd: str = ""
    project: str = ""
    command_adjacent: bool = False
    file_path: str = ""
    command: str = ""
    tool_name: str = ""


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
    m = scopes.match(hit, ctx)
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

    scored, seen = [], set()
    for h in hits:
        hid = h.get("id")
        if not hid or hid in seen:  # scoped+fts unions can repeat an id
            continue
        seen.add(hid)
        try:
            events = db.activation_events(conn, hid, window)
        except Exception:
            events = []
        h["_score"] = score(h, ctx, cfg, activation(events, now, cfg))
        scored.append(h)

    inject, remind = [], []
    for h in sorted(scored, key=lambda x: x["_score"], reverse=True):
        if h.get("status") != "active":
            continue
        auth = h.get("authority")
        if ctx.command_adjacent and auth in ("pending", "quarantined"):
            continue
        s = h["_score"]
        if auth == "quarantined":
            if s >= tl and h["id"] not in dedup_r and len(remind) < bud["remind_max_items"]:
                h["_unverified"] = True
                remind.append(h)
            continue
        if s >= th and h["id"] not in dedup_i and len(inject) < bud["inject_max_items"]:
            inject.append(h)
        elif tl <= s < th and h["id"] not in dedup_r and len(remind) < bud["remind_max_items"]:
            remind.append(h)
    return _trim_to_char_budgets(inject, bud), remind
