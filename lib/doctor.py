"""mem doctor: one-shot health diagnosis. Rolls up KPIs and — the V2 lesson,
generalized — infers substrate liveness from the data the system already
collects, so drift shows up as a diagnosis instead of a monthly checklist."""
import json
import os
import time

from lib import paths, scoring

DAY = 86400

# every hook script logs one metrics line per invocation (§8.2 skeleton); a
# hook absent from metrics.jsonl has not fired on this substrate
EXPECTED_HOOKS = {
    "session_start": "SessionStart not firing: no env contract, no handshake, "
                     "no profile — check plugin install",
    "prompt_gate": "UserPromptSubmit not firing: no witness journal, no "
                   "prompt-tier retrieval, mem confirm cannot work",
    "pretool_gate": "PreToolUse not firing: no scoped injection, no deny tier",
    "observer": "PostToolUse not firing: no fail->fix capture, no taint "
                "tracking, no struggle detection",
    "stop_gate": "Stop not firing: candidates never nudged",
    "failure_gate": "PostToolUseFailure not firing: no failure-text retrieval "
                    "(may be unsupported on this build)",
    "session_end": "SessionEnd not firing: consolidator only kicks at "
                   "session start",
}
OPTIONAL_HOOKS = {"precompact_salvage"}  # fires only when compaction happens


def _read_metrics(window_s=7 * DAY, cap=8000):
    path = os.path.join(paths.data_dir(), "logs", "metrics.jsonl")
    cutoff = int(time.time()) - window_s
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-cap:]
    except OSError:
        return out
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("ts", 0) >= cutoff:
            out.append(d)
    return out


def _p95(values):
    if not values:
        return None
    vs = sorted(values)
    return vs[min(len(vs) - 1, int(0.95 * len(vs)))]


def _errors_recent(window_s=7 * DAY):
    path = os.path.join(paths.data_dir(), "logs", "errors.log")
    cutoff = time.time() - window_s
    n = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.startswith("["):
                    continue
                try:
                    stamp = line[1:line.index("]")]
                    t = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
                except (ValueError, IndexError):
                    continue
                if t >= cutoff:
                    n += 1
    except OSError:
        pass
    return n


def _meta(conn, key):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _session_binding():
    if os.environ.get("HINDBRAIN_SESSION"):
        return "HINDBRAIN_SESSION env (explicit contract)", True
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "CLAUDE_CODE_SESSION_ID (harness-set, race-free)", True
    hs = paths.handshake_session(paths.resolve_project(os.getcwd()))
    if hs:
        return "workspace handshake (last-writer-wins: fragile under " \
               "concurrent sessions)", False
    return "unbound (no env, no fresh handshake)", False


def run_doctor(conn, cfg, session, project):
    now = int(time.time())
    checks = []  # (level, text): level in OK|WARN|FAIL

    def add(level, text):
        checks.append({"level": level, "text": text})

    # ---- substrate liveness, inferred from metrics ----
    metrics = _read_metrics()
    by_hook = {}
    for m in metrics:
        h = m.get("hook")
        if h:
            by_hook.setdefault(h, []).append(m)
    fired = set(by_hook) & set(EXPECTED_HOOKS)
    missing = set(EXPECTED_HOOKS) - set(by_hook)
    if metrics:
        add("OK" if not missing else "WARN",
            f"hooks firing (7d): {' '.join(sorted(fired)) or 'none'}")
        for h in sorted(missing):
            add("WARN", f"hook never seen: {h} — {EXPECTED_HOOKS[h]}")
    else:
        add("FAIL", "no hook metrics in 7d — hooks are not running at all "
                    "(plugin not installed/enabled, or HINDBRAIN_DISABLE set)")

    # ---- session identity & witness capability ----
    src, safe = _session_binding()
    add("OK" if safe else "WARN", f"session binding: {src}")
    try:
        st_turns = conn.execute(
            "SELECT COUNT(*) FROM journal WHERE session_id=? AND "
            "event='user_prompt'", (session,)).fetchone()[0]
    except Exception:
        st_turns = 0
    try:
        rows = conn.execute(
            "SELECT data FROM journal WHERE event='user_prompt' AND ts>=?",
            (now - 900,)).fetchall()
        recent_other = 0
        for r in rows:
            try:
                d = json.loads(r[0])
            except (ValueError, TypeError):
                continue
            if isinstance(d, dict) and d.get("project") == project:
                recent_other += 1
    except Exception:
        recent_other = 0
    if st_turns == 0 and session != "unbound":
        add("WARN", f"witness: 0 user turns journaled for session {session}"
                    + (f" ({recent_other} recent turns exist project-wide — "
                       f"binding may be stale)" if recent_other else
                       " — mem confirm cannot witness yet"))
    else:
        add("OK", f"witness: {st_turns} user turn(s) journaled for this session")
    try:
        sub = conn.execute(
            "SELECT COUNT(*) FROM journal WHERE agent_id IS NOT NULL AND "
            "agent_id != 'main'").fetchone()[0]
        add("OK", f"subagent hooks (V11): {'observed' if sub else 'never observed yet'}")
    except Exception:
        pass

    # ---- KPIs & tuner ----
    th, _tl = scoring.effective_taus(cfg, {}, conn)
    acc = _meta(conn, "acceptance_rate")
    auto = _meta(conn, "auto_tau_hi")
    if not cfg["thresholds"].get("auto_inject", True):
        add("OK", f"inject tier: tau_hi={th} (tuner disabled by config)")
    elif auto:
        add("OK", f"inject tier: ENABLED by tuner (effective tau_hi={th})")
    elif th >= 1.0:
        add("OK", f"inject tier: remind-only; tuner watching "
                  f"(acceptance {acc or 'n/a'})")
    else:
        add("OK", f"inject tier: tau_hi={th} (operator-set)")
    last_cons = _meta(conn, "last_consolidation")
    if last_cons:
        age_h = (now - int(float(last_cons))) / 3600
        add("OK" if age_h < 48 else "WARN",
            f"last consolidation: {age_h:.0f}h ago"
            + ("" if age_h < 48 else " — the kick may be broken; run "
               "python3 consolidator/consolidate.py"))
    else:
        add("WARN", "consolidator has never run — no KPIs, no tuning, no GC")
    deny_rate = _meta(conn, "deny_rate_per_100_bash")
    if deny_rate is not None and float(deny_rate) > 1.0:
        add("WARN", f"deny rate {deny_rate}/100 Bash calls (>1): hazard notes "
                    f"look miscalibrated")
    gini = _meta(conn, "gini_access")
    if gini is not None and float(gini) > 0.85:
        add("WARN", f"access concentration Gini={gini} (>0.85): a few notes "
                    f"dominate surfacing while the rest rot")
    for hook in ("prompt_gate", "pretool_gate", "failure_gate"):
        p95 = _p95([m.get("dur_ms") for m in by_hook.get(hook, [])
                    if isinstance(m.get("dur_ms"), (int, float))])
        if p95 is not None and p95 >= 100:
            add("WARN", f"{hook} p95={p95:.0f}ms (budget 100)")
    nerr = _errors_recent()
    add("OK" if nerr == 0 else "WARN",
        f"errors.log: {nerr} entr{'y' if nerr == 1 else 'ies'} in 7d"
        + ("" if nerr == 0 else " — see <data>/logs/errors.log"))

    # ---- store hygiene ----
    try:
        unarmed = [r[0] for r in conn.execute(
            "SELECT id FROM memory WHERE hazard=1 AND status='active' "
            "AND authority != 'full'").fetchall()]
        if unarmed:
            ids_ = " ".join(i[:10] + "…" for i in unarmed[:3])
            add("WARN", f"{len(unarmed)} hazard note(s) UNARMED (below full "
                        f"authority — deny tier inert): {ids_} — ask the user "
                        f"to `mem confirm` them")
        hubs = [r[0] for r in conn.execute(
            "SELECT from_id FROM (SELECT from_id FROM memory_link UNION ALL "
            "SELECT to_id FROM memory_link) GROUP BY from_id "
            "HAVING COUNT(*) > 3").fetchall()]
        if hubs:
            add("WARN", f"{len(hubs)} link hub(s) (>3 links) dilute the "
                        f"association spread — prune with mem unlink")
        stale_c = conn.execute(
            "SELECT COUNT(*) FROM candidate WHERE status='open' AND ts < ?",
            (now - DAY,)).fetchone()[0]
        if stale_c:
            add("WARN", f"{stale_c} candidate(s) open >24h — review with "
                        f"mem queue (save or drop)")
        pend = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE status='active' AND "
            "authority='pending'").fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE status='active'").fetchone()[0]
        if total:
            add("OK", f"store: {total} active ({pend} pending"
                      f"{'' if pend <= total * 0.8 else ' — corroboration is not happening'})")
    except Exception:
        pass

    warns = sum(1 for c in checks if c["level"] == "WARN")
    fails = sum(1 for c in checks if c["level"] == "FAIL")
    return {"ts": now, "session": session, "project": project,
            "checks": checks, "warnings": warns, "failures": fails}


def format_text(report):
    lines = [f"hindbrain doctor — session {report['session']}"]
    # failures and warnings first; OK lines fill remaining budget
    ordered = ([c for c in report["checks"] if c["level"] == "FAIL"]
               + [c for c in report["checks"] if c["level"] == "WARN"]
               + [c for c in report["checks"] if c["level"] == "OK"])
    for c in ordered[:26]:
        lines.append(f"{c['level']:<4} {c['text']}")
    v = []
    if report["failures"]:
        v.append(f"{report['failures']} failure(s)")
    if report["warnings"]:
        v.append(f"{report['warnings']} warning(s)")
    lines.append("verdict: " + (", ".join(v) if v else "healthy"))
    return lines[:30]
