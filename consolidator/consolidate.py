#!/usr/bin/env python3
"""Offline consolidation worker (spec §10). Detached singleton; nine v1 passes,
all stdlib heuristics, no LLM calls. Runs standalone: python3 consolidator/consolidate.py
Note: parents spawn this with HINDBRAIN_DISABLE=1 (a guard for headless child
calls) — the consolidator itself must ignore that flag."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime
import json
import time

try:
    import fcntl
except ImportError:
    fcntl = None

from lib import db, log, paths, signatures, witness

NAME = "consolidate"
DAY = 86400
AUTH_RANK = {"full": 3, "standard": 2, "pending": 1, "quarantined": 0}
NEG_CUES = {"not", "never", "don", "dont", "avoid", "instead", "without", "stop"}


def _acquire_lock():
    # Singleton: exit if another consolidator holds the lock. fcntl-less
    # platforms degrade to no singleton guard (v1 targets Linux/macOS).
    path = os.path.join(paths.data_dir(), "consolidator.lock")
    fh = open(path, "a+")
    if fcntl is None:
        return fh
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def _reports_dir():
    d = os.path.join(paths.data_dir(), "reports")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _tokens(mem):
    return witness.content_words((mem.get("title") or "") + " " + (mem.get("body") or ""))


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---- pass 1: GC ----

def gc(conn, cfg, now):
    removed_files = 0
    sessions_dir = os.path.join(paths.data_dir(), "sessions")
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        p = os.path.join(sessions_dir, name)
        try:
            if now - os.path.getmtime(p) < 7 * DAY:
                continue
            with open(p, "r", encoding="utf-8") as f:
                st = json.load(f)
            if not (isinstance(st, dict) and st.get("ended")):
                continue
            os.unlink(p)
            removed_files += 1
            try:
                os.unlink(p + ".lock")
            except OSError:
                pass
        except (OSError, ValueError):
            continue
    # A candidate gets its own session plus one carry; after that it expires.
    c1 = conn.execute(
        "UPDATE candidate SET status='expired' WHERE status='carried' AND ts < ?",
        (now - DAY,)).rowcount
    c2 = conn.execute(
        "UPDATE candidate SET status='expired' WHERE status='open' AND ts < ?",
        (now - 7 * DAY,)).rowcount
    j1 = conn.execute(
        "DELETE FROM journal WHERE event != 'user_prompt' AND ts < ?",
        (now - 30 * DAY,)).rowcount
    j2 = conn.execute(
        "DELETE FROM journal WHERE event = 'user_prompt' AND ts < ?",
        (now - 90 * DAY,)).rowcount
    conn.commit()
    return {"session_files": removed_files, "candidates_expired": c1 + c2,
            "journal_rows": j1 + j2}


# ---- pass 2: expiry ----

def expire(conn, cfg, now):
    decay = cfg.get("decay", {})
    n = 0
    rows = conn.execute(
        "SELECT id, kind, valid_from, created_at, last_access_at, ttl_days, pinned "
        "FROM memory WHERE status='active'").fetchall()
    for r in rows:
        m = dict(r)
        if m.get("pinned"):
            continue
        ttl = m.get("ttl_days")
        if ttl is None:
            ttl = decay.get(m.get("kind"), 0)
        if not ttl or ttl <= 0:
            continue
        base = m.get("valid_from") or m.get("created_at") or now
        if m.get("kind") == "gotcha" and m.get("last_access_at"):
            base = max(base, m["last_access_at"])  # refresh-on-use
        if now - base > ttl * DAY:
            conn.execute(
                "UPDATE memory SET status='expired', invalidated_at=? WHERE id=?",
                (now, m["id"]))
            n += 1
    conn.commit()
    return {"expired": n}


# ---- pass 3: dedup ----

def dedup(conn, cfg, now):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM memory WHERE status='active' "
        "ORDER BY project, kind, created_at").fetchall()]
    groups = {}
    for m in rows:
        groups.setdefault((m["project"], m["kind"]), []).append(m)
    merged = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        toks = {m["id"]: _tokens(m) for m in group}
        gone = set()
        for i in range(len(group)):
            a = group[i]
            if a["id"] in gone:
                continue
            for j in range(i + 1, len(group)):
                b = group[j]
                if b["id"] in gone:
                    continue
                if _jaccard(toks[a["id"]], toks[b["id"]]) < 0.85:
                    continue
                keep, drop = a, b
                if (AUTH_RANK.get(b["authority"], 0), -b["created_at"]) > \
                   (AUTH_RANK.get(a["authority"], 0), -a["created_at"]):
                    keep, drop = b, a
                tags = [t for t in
                        (keep.get("tags", "") + "," + drop.get("tags", "")).split(",")
                        if t.strip()]
                tags = ",".join(dict.fromkeys(t.strip() for t in tags))
                conn.execute(
                    "UPDATE memory SET tags=?, corroborations=?, supersedes=? WHERE id=?",
                    (tags,
                     (keep.get("corroborations") or 0) + (drop.get("corroborations") or 0),
                     keep.get("supersedes") or drop["id"], keep["id"]))
                conn.execute(
                    "UPDATE memory SET status='superseded', invalidated_at=? WHERE id=?",
                    (now, drop["id"]))
                keep["tags"], keep["corroborations"] = tags, \
                    (keep.get("corroborations") or 0) + (drop.get("corroborations") or 0)
                gone.add(drop["id"])
                merged += 1
                if keep is b:
                    break  # a superseded; stop pairing it
    conn.commit()
    return {"superseded": merged}


# ---- passes 4+5: contradiction report + reconsolidation check ----

def _flagged_conflicts(conn):
    lines = []
    try:
        rows = conn.execute(
            "SELECT ts, session_id, data FROM journal "
            "WHERE event LIKE 'contradiction%' ORDER BY ts").fetchall()
    except Exception:
        rows = []
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (ValueError, TypeError):
            d = {"raw": str(r["data"])[:200]}
        lines.append(f"- {_d(r['ts'])} session {r['session_id']}: "
                     f"{json.dumps(d)[:300]}")
    return lines


def _d(ts):
    try:
        return datetime.date.fromtimestamp(int(ts)).isoformat()
    except (ValueError, OSError, OverflowError):
        return "?"


def _polarity(words):
    return bool(NEG_CUES & words)


def reconsolidation_flags(conn, now):
    # 15.1-J: an injected command-scoped note followed in-session by a
    # learned_fix on the same head with opposite polarity cues.
    lines = []
    injected = conn.execute(
        "SELECT memory_id, session_id, ts FROM access_log "
        "WHERE event='injected' AND ts > ?", (now - 30 * DAY,)).fetchall()
    for acc in injected:
        mem = db.get_memory(conn, acc["memory_id"])
        if not mem or mem.get("scope_type") != "command":
            continue
        heads = {h.strip() for h in (mem.get("scope_value") or "").split("|") if h.strip()}
        if not heads:
            continue
        cands = conn.execute(
            "SELECT id, payload FROM candidate WHERE session_id=? "
            "AND signal='learned_fix' AND ts > ?",
            (acc["session_id"], acc["ts"])).fetchall()
        for c in cands:
            try:
                payload = json.loads(c["payload"])
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            subs = signatures.subcommands(str(payload.get("fail_cmd") or ""))
            if not subs or signatures.head_str(subs[0]) not in heads:
                continue
            fix_words = witness.content_words(
                str(payload.get("delta") or "") + " " + str(payload.get("ok_cmd") or ""))
            if _polarity(fix_words) == _polarity(_tokens(mem)):
                continue
            lines.append(
                f"- [{mem['id']}] \"{mem.get('title', '')}\" was injected in session "
                f"{acc['session_id']} but a later fix on the same head "
                f"({signatures.head_str(subs[0])}) suggests the opposite "
                f"(candidate {c['id']}). Review for supersession.")
    return lines


def contradiction_report(conn, cfg, now):
    refuted = conn.execute(
        "SELECT id, title, invalidated_at FROM memory WHERE status='refuted'").fetchall()
    sections = []
    if refuted:
        sections.append("## Refuted memories awaiting review\n" + "\n".join(
            f"- [{r['id']}] \"{r['title']}\" (refuted {_d(r['invalidated_at'])})"
            for r in refuted))
    conflicts = _flagged_conflicts(conn)
    if conflicts:
        sections.append("## Save-pipeline conflict flags\n" + "\n".join(conflicts))
    recon = reconsolidation_flags(conn, now)
    if recon:
        sections.append("## Reconsolidation flags\n" + "\n".join(recon))
    n = len(refuted) + len(conflicts) + len(recon)
    if sections:
        path = os.path.join(_reports_dir(),
                            f"contradictions-{datetime.date.today().isoformat()}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# hindbrain contradiction report — "
                    f"{datetime.date.today().isoformat()}\n\n"
                    + "\n\n".join(sections) + "\n")
    return {"flags": n}


# ---- pass 6: promotion sweep ----

def promote(conn, cfg, now):
    p1 = conn.execute(
        "UPDATE memory SET authority='standard' WHERE status='active' "
        "AND authority='pending' AND corroborations >= 2").rowcount
    p2 = 0
    rows = conn.execute(
        "SELECT id FROM memory WHERE status='active' AND authority='quarantined'").fetchall()
    for r in rows:
        n = conn.execute(
            "SELECT COUNT(DISTINCT session_id) AS n FROM access_log "
            "WHERE memory_id=? AND event='cited'", (r["id"],)).fetchone()["n"]
        if n >= 2:
            conn.execute("UPDATE memory SET authority='standard' WHERE id=?", (r["id"],))
            p2 += 1
    conn.commit()
    return {"promoted": p1 + p2}


# ---- pass 7: CLAUDE.md graduation proposals ----

def graduation_proposals(conn, cfg, now):
    # Session presence per project from access_log coverage; NEVER writes CLAUDE.md.
    rows = conn.execute(
        "SELECT m.project AS project, a.session_id AS sid, MAX(a.ts) AS last "
        "FROM access_log a JOIN memory m ON m.id = a.memory_id "
        "WHERE a.session_id IS NOT NULL AND a.event != 'synthetic' "
        "GROUP BY m.project, a.session_id").fetchall()
    sessions_by_project = {}
    for r in rows:
        sessions_by_project.setdefault(r["project"], []).append((r["last"], r["sid"]))
    proposals = []
    for project, sess in sessions_by_project.items():
        recent = [sid for _, sid in sorted(sess, reverse=True)[:10]]
        # Spec rule: coverage >= 50% over the last min(10, available) sessions;
        # guard only the degenerate single-session case.
        if len(recent) < 2:
            continue
        marks = ",".join("?" for _ in recent)
        mems = conn.execute(
            f"SELECT m.id, m.title, COUNT(DISTINCT a.session_id) AS cov "
            f"FROM memory m JOIN access_log a ON a.memory_id = m.id "
            f"WHERE m.status='active' AND m.project=? "
            f"AND a.event IN ('injected','fetched') AND a.session_id IN ({marks}) "
            f"GROUP BY m.id", (project, *recent)).fetchall()
        for m in mems:
            if m["cov"] * 2 >= len(recent):
                proposals.append(
                    f"- [{m['id']}] \"{m['title']}\" — injected/fetched in "
                    f"{m['cov']} of the last {len(recent)} sessions of "
                    f"{project or '(global)'}; consider adding it to that "
                    f"project's CLAUDE.md and pinning or retiring the note.")
    return {"proposals": proposals}


# ---- pass 8: metrics rollup ----

def _gini(values):
    xs = sorted(v for v in values if v is not None)
    n = len(xs)
    total = sum(xs)
    if n == 0 or total == 0:
        return 0.0
    cum = sum((2 * (i + 1) - n - 1) * x for i, x in enumerate(xs))
    return cum / (n * total)


def _p95_gate_ms():
    # Rolling p95 gate latency over the dur_ms field of the last ~2000
    # logs/metrics.jsonl lines, overall across the *_gate hooks (the choice
    # is documented in the promotions report). None when no samples exist.
    path = os.path.join(paths.data_dir(), "logs", "metrics.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-2000:]
    except OSError:
        return None
    durs = []
    for line in lines:
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if (isinstance(d, dict) and str(d.get("hook") or "").endswith("_gate")
                and isinstance(d.get("dur_ms"), (int, float))):
            durs.append(float(d["dur_ms"]))
    if not durs:
        return None
    durs.sort()
    return durs[min(len(durs) - 1, round(0.95 * (len(durs) - 1)))]


def metrics_rollup(conn, cfg, now):
    cut = now - 30 * DAY
    reminded = conn.execute(
        "SELECT COUNT(*) AS n FROM access_log WHERE event='reminded' AND ts > ?",
        (cut,)).fetchone()["n"]
    fetched_after = conn.execute(
        "SELECT COUNT(*) AS n FROM access_log f WHERE f.event='fetched' AND f.ts > ? "
        "AND EXISTS (SELECT 1 FROM access_log r WHERE r.memory_id=f.memory_id "
        "AND r.session_id=f.session_id AND r.event='reminded' AND r.ts <= f.ts)",
        (cut,)).fetchone()["n"]
    acceptance = (fetched_after / reminded) if reminded else 0.0
    dispositions = {}
    for r in conn.execute(
            "SELECT signal, status, COUNT(*) AS n FROM candidate "
            "GROUP BY signal, status").fetchall():
        dispositions.setdefault(r["signal"], {})[r["status"]] = r["n"]
    denies = conn.execute(
        "SELECT COUNT(*) AS n FROM access_log WHERE event='denied' AND ts > ?",
        (cut,)).fetchone()["n"]
    bash_calls = 0
    for r in conn.execute(
            "SELECT data FROM journal WHERE event IN ('tool_ok','tool_fail') "
            "AND ts > ?", (cut,)).fetchall():
        try:
            d = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        if isinstance(d, dict) and d.get("tool") == "Bash":
            bash_calls += 1
    deny_rate = (100.0 * denies / bash_calls) if bash_calls else 0.0
    p95 = _p95_gate_ms()
    store = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM memory GROUP BY status").fetchall()}
    gini = _gini([r["access_count"] for r in conn.execute(
        "SELECT access_count FROM memory WHERE status='active'").fetchall()])
    kv = [("acceptance_rate", f"{acceptance:.4f}"),
          ("nudge_dispositions", json.dumps(dispositions)),
          ("deny_count_30d", str(denies)),
          ("deny_rate_per_100_bash", f"{deny_rate:.2f}"),
          ("store_size", json.dumps(store)),
          ("gini_access", f"{gini:.4f}")]
    if p95 is not None:
        kv.append(("p95_gate_ms", f"{p95:.0f}"))
    for key, val in kv:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, val))
    conn.commit()
    return {"acceptance": acceptance, "gini": gini, "denies": denies,
            "bash_calls": bash_calls, "deny_rate": deny_rate,
            "p95_gate_ms": p95, "store": store, "dispositions": dispositions}


def promotions_report(cfg, proposals, rollup):
    alerts = []
    if rollup["gini"] > 0.85:
        alerts.append(f"- Gini(access_count) = {rollup['gini']:.2f} > 0.85 — "
                      f"retrieval is concentrated on very few notes; review "
                      f"stale or over-broad memories.")
    if rollup.get("deny_rate", 0.0) > 1.0:
        alerts.append(
            f"- Deny rate = {rollup['deny_rate']:.2f} per 100 Bash calls over "
            f"30d ({rollup['denies']} denies / {rollup.get('bash_calls', 0)} "
            f"Bash calls; > 1) — hazard notes look miscalibrated (spec §14).")
    tau_hi = cfg.get("thresholds", {}).get("tau_hi", 0.5)
    if rollup["acceptance"] > 0.15 and tau_hi > 1.0:
        alerts.append(
            f"- Remind-tier acceptance is {rollup['acceptance']:.2f} (> 0.15) and "
            f"tau_hi is still {tau_hi} (remind-only). Set tau_hi = 0.50 in "
            f"config.toml to enable the inject tier (AM-8).")
    if not proposals and not alerts:
        return 0
    today = datetime.date.today().isoformat()
    path = os.path.join(_reports_dir(), f"promotions-{today}.md")
    parts = [f"# hindbrain promotion report — {today}"]
    if proposals:
        parts.append("## CLAUDE.md graduation proposals "
                     "(hindbrain never edits CLAUDE.md — apply by hand)\n"
                     + "\n".join(proposals))
    if alerts:
        parts.append("## Alerts\n" + "\n".join(alerts))
    if rollup.get("p95_gate_ms") is not None:
        parts.append(f"## Gate latency\np95 = {rollup['p95_gate_ms']:.0f} ms — "
                     f"overall across the *_gate hooks, from the dur_ms field "
                     f"of the last ~2000 logs/metrics.jsonl lines.")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(parts) + "\n")
    return len(proposals) + len(alerts)


def run():
    cfg = paths.load_config()
    conn = db.connect()
    db.ensure_schema(conn)
    now = int(time.time())
    summary = {}
    passes = [("gc", gc), ("expiry", expire), ("dedup", dedup),
              ("contradictions", contradiction_report), ("promotion", promote)]
    for name, fn in passes:
        try:
            summary[name] = fn(conn, cfg, now)
        except Exception as e:
            log.err(e, f"{NAME}.{name}")
    try:
        grad = graduation_proposals(conn, cfg, now)
    except Exception as e:
        log.err(e, f"{NAME}.graduation")
        grad = {"proposals": []}
    try:
        rollup = metrics_rollup(conn, cfg, now)
        summary["report_lines"] = promotions_report(cfg, grad["proposals"], rollup)
        summary["acceptance"] = round(rollup["acceptance"], 4)
        summary["gini"] = round(rollup["gini"], 4)
    except Exception as e:
        log.err(e, f"{NAME}.metrics")
    summary["proposals"] = len(grad["proposals"])
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES "
                 "('last_consolidation', ?)", (str(now),))
    conn.commit()
    conn.close()
    return summary


if __name__ == "__main__":
    t0 = time.monotonic()
    lock = None
    try:
        lock = _acquire_lock()
        if lock is None:
            sys.exit(0)  # another consolidator is running
        summary = run()
        log.metric({"hook": NAME,
                    "dur_ms": int(1000 * (time.monotonic() - t0)),
                    **{k: v for k, v in summary.items()
                       if isinstance(v, (int, float, str))}})
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        try:
            log.err(e, NAME)
        finally:
            sys.exit(0)
    finally:
        if lock is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock.close()
