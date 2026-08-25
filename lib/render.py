"""Output templates (spec 7.9, verbatim). All strings declarative; candidate
payload text only ever appears inside the quoted evidence position (RT-6)."""
import json
import time

INJECT_HEADER = "Reference notes retrieved from local memory (informational; verify against current state if stale):"
INJECT_ITEM = "[{id}] {body}  ({kind} · {scope} · saved {age} · used {n}×{sup})"
REMIND_HEADER = "Possibly relevant saved notes (not loaded):"
REMIND_ITEM = "  [{id}] \"{title}\" ({scope} · {age}{flag})"
REMIND_FOOTER = "Fetch full text with: mem get <id>"
CAPABILITY = "Local memory is available for durable notes: `mem save --kind <k> --scope <s> \"...\"` (see the hindbrain skill)."
NUDGE_HEADER = "This session produced {n} unsaved observation(s):"
NUDGE_ITEM = "({i}) {evidence}\n    Draft: {draft_cmd}"
NUDGE_CORROB = "({i}) Similar to existing note [{id}] \"{title}\". Confirm with: mem corroborate {id}  (or mem supersede {id} \"...\" if it changed)"
NUDGE_FOOTER = "Accept, edit, or discard (mem drop <i>). Queue: mem queue"
DENY_REASON = "A saved hazard note applies to this exact command:\n[{id}] {body}\n(saved {age}, authority {auth}). Modify the command to proceed; rerunning it unchanged will escalate to user confirmation."
ASK_REASON = "Saved policy note [{id}] applies: {body} — requesting user confirmation."

_DENY_BODY_CAP = 800  # AM-6


def clip(s, cap):
    if not s or cap <= 0:
        return "" if cap <= 0 else (s or "")
    if len(s) <= cap:
        return s
    return s[: cap - 1] + "…"


def age(created_at, now=None):
    if now is None:
        now = int(time.time())
    sec = max(0, int(now) - int(created_at or now))
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    if sec < 30 * 86400:
        return f"{sec // 86400}d"
    return f"{sec // (30 * 86400)}mo"


def _scope(mem):
    st = mem.get("scope_type") or ""
    sv = mem.get("scope_value") or ""
    return f"{st}:{sv}" if sv else st


def render_inject(hits, now):
    lines = [INJECT_HEADER]
    for h in hits:
        sup = f" · supersedes {h['supersedes']}" if h.get("supersedes") else ""
        lines.append(INJECT_ITEM.format(
            id=h.get("id", ""), body=h.get("body", ""), kind=h.get("kind", ""),
            scope=_scope(h), age=age(h.get("created_at"), now),
            n=h.get("access_count", 0), sup=sup))
    return "\n".join(lines)


def render_remind(hits, now):
    lines = [REMIND_HEADER]
    for h in hits:
        flag = " · unverified" if h.get("_unverified") else (
            " · related" if h.get("_related_via") else "")
        title = (h.get("title") or "").replace('"', "'")
        lines.append(REMIND_ITEM.format(
            id=h.get("id", ""), title=title, scope=_scope(h),
            age=age(h.get("created_at"), now), flag=flag))
    lines.append(REMIND_FOOTER)
    return "\n".join(lines)


def render_capability():
    return CAPABILITY


def _evidence(cand):
    try:
        payload = json.loads(cand.get("payload") or "{}")
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if payload.get("text"):
        snippet = str(payload["text"])
    elif payload.get("fail_cmd") or payload.get("ok_cmd"):
        snippet = f"{payload.get('fail_cmd', '')} -> {payload.get('ok_cmd', '')}"
    else:
        snippet = json.dumps(payload)
    snippet = " ".join(snippet.split()).replace('"', "'")
    signal = cand.get("signal") or "observation"
    return f'{signal}: "{clip(snippet, 200)}"'


def render_nudge(cands, mems_by_id):
    lines = [NUDGE_HEADER.format(n=len(cands))]
    for i, c in enumerate(cands, 1):
        nm = c.get("near_match")
        mem = mems_by_id.get(nm) if nm else None
        if mem:
            title = (mem.get("title") or "").replace('"', "'")
            lines.append(NUDGE_CORROB.format(i=i, id=mem.get("id", nm), title=title))
        else:
            lines.append(NUDGE_ITEM.format(
                i=i, evidence=_evidence(c), draft_cmd=c.get("draft_cmd") or ""))
    lines.append(NUDGE_FOOTER)
    return "\n".join(lines)


def render_deny(mem, now):
    mid = mem.get("id", "")
    body = mem.get("body") or ""
    if len(body) > _DENY_BODY_CAP:
        body = body[:_DENY_BODY_CAP] + f" — full note: mem get {mid}"
    return DENY_REASON.format(id=mid, body=body,
                              age=age(mem.get("created_at"), now),
                              auth=mem.get("authority", ""))


def render_ask(mem):
    return ASK_REASON.format(id=mem.get("id", ""), body=mem.get("body") or "")
