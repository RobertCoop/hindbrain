"""AM-1 P0-detector fixture (spec-mandated): 30 cases, 15 fire / 15 hold,
exercising the two-clause rule — remember/for future reference/from now on/
going forward anywhere; always/never only within 4 tokens of a clause-start
second-person directive verb. Drives the real detector in prompt_gate, plus a
subprocess check that fires enqueue P0 candidates and holds do not."""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import prompt_gate  # noqa: E402


FIRE = [
    # clause one: keyword anywhere
    "Remember that we deploy only on Fridays.",
    "Please remember the staging database is shared with QA.",
    "For future reference, the API rate limit is 100 requests per minute.",
    "From now on run the linter before every commit.",
    "Going forward we tag releases with the sprint number.",
    "One more thing — remember: builds need Java 17.",
    # clause two: always|never within 4 tokens of a clause-start directive verb
    "Always use spaces for indentation in this repo.",
    "Never run migrations against prod directly.",
    "You should always prefer rebase over merge here.",
    "Please never use force push on main.",
    "Always avoid editing the generated files by hand.",
    "Never write secrets into the log output.",
    "Ask always before deleting remote branches.",
    "Don't always trust the cached lockfile; regenerate it.",
    "Use always the docker wrapper script, not raw docker.",
]

HOLD = [
    # bare always/never in ordinary technical prose (the AM-1 false-positive class)
    "This function always returns null on bad input.",
    "The build never finishes in under five minutes.",
    "Tests always pass locally but fail in CI.",
    "Why does the daemon never restart cleanly?",
    "The cache is never invalidated automatically.",
    "It always defaults to utf-8 encoding.",
    "That query never uses the covering index.",
    "Deployment is always slow on Mondays.",
    "I never understood why this worked.",
    # keyword near-misses (morphology must not fire \bremember\b etc.)
    "I remembered to update the changelog yesterday.",
    "She remembers the old API fondly.",
    "We are going forwards with the modular plan.",
    # directive verb present but always/never beyond the 4-token window
    "Use the helper module because the parser code always breaks otherwise.",
    "Avoid the tempdir cleanup since it never worked on macOS anyway.",
    # directive verb not at clause start
    "The linter tells us to never use tabs.",
]


@pytest.mark.parametrize("prompt", FIRE)
def test_p0_detector_fires(prompt):
    assert prompt_gate._p0_sentence(prompt) is not None


@pytest.mark.parametrize("prompt", HOLD)
def test_p0_detector_holds(prompt):
    assert prompt_gate._p0_sentence(prompt) is None


# ---- end to end: the real script enqueues (or doesn't) a P0 candidate ----

def _run_prompt_gate(tmp_data, prompt, session):
    env = dict(os.environ)
    env.pop("HINDBRAIN_DISABLE", None)
    env.pop("HINDBRAIN_DB", None)
    env["HINDBRAIN_DATA"] = str(tmp_data)
    evt = {"session_id": session, "cwd": str(tmp_data), "prompt": prompt,
           "hook_event_name": "UserPromptSubmit"}
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "prompt_gate.py")],
        input=json.dumps(evt).encode(), capture_output=True, env=env,
        timeout=60)


def _p0_count(conn, session):
    return conn.execute(
        "SELECT COUNT(*) FROM candidate WHERE session_id=? AND priority='P0' "
        "AND signal='remember_request'", (session,)).fetchone()[0]


def test_always_never_clause_creates_p0_candidate(tmp_data, conn):
    proc = _run_prompt_gate(tmp_data, FIRE[6], "s-det-fire")
    assert proc.returncode == 0, proc.stderr.decode()
    assert _p0_count(conn, "s-det-fire") == 1


def test_prose_always_creates_no_candidate(tmp_data, conn):
    proc = _run_prompt_gate(tmp_data, HOLD[0], "s-det-hold")
    assert proc.returncode == 0, proc.stderr.decode()
    assert _p0_count(conn, "s-det-hold") == 0
