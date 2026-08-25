"""Stop-lint (design S3): mechanical preference checks over
last_assistant_message at the Stop hook."""
import json
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from lib import db, ids  # noqa: E402
from tests.test_gates import hook_env, hook_output, run_hook  # noqa: E402


def seed_pref(conn, body, project="", authority="standard"):
    mid = ids.ulid()
    conn.execute(
        "INSERT INTO memory(id, title, body, kind, scope_type, project, "
        "authority, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (mid, body[:80], body, "preference", "project", project, authority,
         int(time.time())))
    conn.commit()
    return mid


def stop_evt(session, cwd, msg):
    return {"session_id": session, "cwd": str(cwd),
            "hook_event_name": "Stop", "last_assistant_message": msg}


def test_lint_fires_on_quoted_phrase(tmp_data, conn, tmp_path):
    mid = seed_pref(conn, 'in outbound drafts, never open with '
                          '"I hope this finds you well" — too stiff')
    msg = ("Here is the draft:\n\nI hope this finds you WELL! I wanted to "
           "reach out about the meeting.")
    proc = run_hook("stop_gate.py", stop_evt("s-lint", tmp_path, msg),
                    hook_env(tmp_data))
    out = hook_output(proc, "Stop")
    assert out is not None
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "conflicts with saved preference" in text
    assert mid in text and "I hope this finds you well" in text
    # surfacing feeds activation
    ev = [r[0] for r in conn.execute(
        "SELECT event FROM access_log WHERE memory_id=?", (mid,))]
    assert "injected" in ev

    # once per session per note — the identical follow-up turn stays silent
    proc = run_hook("stop_gate.py", stop_evt("s-lint", tmp_path, msg),
                    hook_env(tmp_data))
    assert proc.returncode == 0 and proc.stdout.decode().strip() == ""


def test_lint_silent_without_violation_or_quote(tmp_data, conn, tmp_path):
    # no quoted phrase -> not mechanically checkable -> never lints
    seed_pref(conn, "keep a humble, tentative tone in outbound drafts")
    # quoted phrase not present in the message -> silent
    seed_pref(conn, 'avoid the word "utilize" in docs')
    msg = "I hope this helps! We use the standard flow here."
    proc = run_hook("stop_gate.py", stop_evt("s-clean", tmp_path, msg),
                    hook_env(tmp_data))
    assert proc.returncode == 0 and proc.stdout.decode().strip() == ""


def test_lint_excludes_quarantined_and_respects_flag(tmp_data, conn, tmp_path):
    seed_pref(conn, 'never say "synergy" anywhere', authority="quarantined")
    msg = "This creates great synergy across the teams."
    proc = run_hook("stop_gate.py", stop_evt("s-q", tmp_path, msg),
                    hook_env(tmp_data))
    assert proc.stdout.decode().strip() == ""

    seed_pref(conn, 'never say "synergy" anywhere', authority="standard")
    with open(os.path.join(str(tmp_data), "config.toml"), "w") as f:
        f.write("[lint]\nenabled = false\n")
    proc = run_hook("stop_gate.py", stop_evt("s-q2", tmp_path, msg),
                    hook_env(tmp_data))
    assert proc.stdout.decode().strip() == ""


def test_lint_combines_with_nudge(tmp_data, conn, tmp_path):
    from tests.test_cli import seed_candidate
    mid = seed_pref(conn, 'avoid "circle back" in summaries')
    seed_candidate(conn, "s-both", priority="P0", signal="remember_request",
                   payload={"text": "remember the deploy freeze",
                            "project": str(tmp_path)})
    msg = "Let's circle back on this next week."
    proc = run_hook("stop_gate.py", stop_evt("s-both", tmp_path, msg),
                    hook_env(tmp_data))
    out = hook_output(proc, "Stop")
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "conflicts with saved preference" in text  # lint part
    assert "unsaved observation" in text              # nudge part
    assert mid in text
