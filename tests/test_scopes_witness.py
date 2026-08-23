"""Spec 13.1: lib/scopes matching (path/command/tool scopes, global project
semantics) and lib/witness (user/observer/confirm) in both raw and hashed
(redact_journal) journal modes."""
import hashlib
import json
import time

import pytest

from lib import paths, scoring, scopes, witness


def _mem(scope_type="project", scope_value="", project="/proj"):
    return {"scope_type": scope_type, "scope_value": scope_value,
            "project": project}


def _ctx(**kw):
    kw.setdefault("project", "/proj")
    return scoring.Ctx(**kw)


# ---- path scopes: fnmatch relative-to-project AND absolute (spec 7.7) ----

def test_path_scope_relative_glob():
    m = _mem("path", "src/*.py")
    ctx = _ctx(file_path="/proj/src/x.py")
    assert scopes.match(m, ctx) == "exact"


def test_path_scope_absolute_glob():
    m = _mem("path", "/proj/src/*.py")
    ctx = _ctx(file_path="/proj/src/x.py")
    assert scopes.match(m, ctx) == "exact"


def test_path_scope_no_match_falls_to_project():
    m = _mem("path", "docs/*.md")
    ctx = _ctx(file_path="/proj/src/x.py")
    assert scopes.match(m, ctx) == "project"  # same project, not exact


def test_path_scope_no_match_other_project_is_none():
    m = _mem("path", "docs/*.md", project="/other")
    ctx = _ctx(file_path="/proj/src/x.py")
    assert scopes.match(m, ctx) is None


def test_path_scope_empty_file_path_not_exact():
    m = _mem("path", "src/*.py")
    assert scopes.match(m, _ctx()) == "project"


# ---- command scopes: heads, multitools, '|'-separated multi-values ----

def test_command_scope_single_head():
    m = _mem("command", "pytest")
    assert scopes.match(m, _ctx(command="pytest -x tests/")) == "exact"


def test_command_scope_multitool_head():
    m = _mem("command", "git.push")
    assert scopes.match(m, _ctx(command="git push origin main")) == "exact"
    assert scopes.match(m, _ctx(command="git pull origin main")) == "project"


def test_command_scope_pipe_separated_multi_value():
    m = _mem("command", "pytest|tox")
    assert scopes.match(m, _ctx(command="tox -e py")) == "exact"
    assert scopes.match(m, _ctx(command="pytest -q")) == "exact"
    assert scopes.match(m, _ctx(command="make test")) == "project"


def test_command_scope_matches_any_subcommand_segment():
    m = _mem("command", "git.push")
    assert scopes.match(m, _ctx(command="cd /tmp && git push")) == "exact"


def test_command_scope_empty_command_not_exact():
    m = _mem("command", "git.push")
    assert scopes.match(m, _ctx()) == "project"


# ---- tool scopes: regex fullmatch, invalid regex guarded ----

def test_tool_scope_regex_fullmatch():
    m = _mem("tool", "mcp__.*")
    assert scopes.match(m, _ctx(tool_name="mcp__github__search")) == "exact"


def test_tool_scope_partial_match_insufficient():
    m = _mem("tool", "Edit", project="/other")
    assert scopes.match(m, _ctx(tool_name="NotebookEdit")) is None


def test_tool_scope_invalid_regex_guarded():
    m = _mem("tool", "([", project="/other")
    assert scopes.match(m, _ctx(tool_name="Bash")) is None  # no exception


# ---- global ('') project semantics ----

def test_global_project_matches_every_project_at_project_level():
    m = _mem("global", "", project="")
    assert scopes.match(m, _ctx(project="/proj")) == "project"
    assert scopes.match(m, _ctx(project="/somewhere/else")) == "project"


def test_project_scope_same_and_other_project():
    assert scopes.match(_mem(), _ctx()) == "project"
    assert scopes.match(_mem(project="/other"), _ctx()) is None


def test_non_dict_mem_is_none():
    assert scopes.match(None, _ctx()) is None


# ---- witness harness ----

def _hash_words(text):
    return sorted(hashlib.sha256(w.encode("utf-8")).hexdigest()
                  for w in witness.content_words(text))


def _journal_prompt(conn, session, text, hashed=False, ts=None):
    data = {"hash_words": _hash_words(text)} if hashed else {"text": text}
    conn.execute(
        "INSERT INTO journal(session_id, agent_id, ts, event, data) "
        "VALUES (?,?,?,?,?)",
        (session, "main", ts if ts is not None else int(time.time()),
         "user_prompt", json.dumps(data)))
    conn.commit()


@pytest.fixture
def cfg(tmp_data):
    return paths.load_config()


BODY = "pytest here needs PYTHONPATH=src exported; bare pytest fails on imports"


# ---- user_witnessed: jaccard / subset in raw and hashed modes (spec 5.3) ----

@pytest.mark.parametrize("hashed", [False, True], ids=["raw", "hashed"])
def test_user_witnessed_jaccard_pass(conn, cfg, hashed):
    _journal_prompt(conn, "s-w", "remember that deploys happen only on fridays",
                    hashed=hashed)
    body = "deploys happen only on fridays in this repo"
    assert witness.user_witnessed(body, "s-w", conn, cfg) is True


@pytest.mark.parametrize("hashed", [False, True], ids=["raw", "hashed"])
def test_user_witnessed_subset_pass_below_jaccard(conn, cfg, hashed):
    # body words are a subset of a much longer prompt: jaccard < 0.5, subset passes
    prompt = ("remember that deploys happen on fridays after the standup review "
              "meeting window closes for everyone")
    _journal_prompt(conn, "s-w", prompt, hashed=hashed)
    assert witness.user_witnessed("deploys happen fridays", "s-w", conn, cfg) is True


@pytest.mark.parametrize("hashed", [False, True], ids=["raw", "hashed"])
def test_user_witnessed_unrelated_fails(conn, cfg, hashed):
    _journal_prompt(conn, "s-w", "please refactor the parser module today",
                    hashed=hashed)
    body = "kubernetes ingress requires the sticky-session annotation here"
    assert witness.user_witnessed(body, "s-w", conn, cfg) is False


def test_user_witnessed_no_prompts(conn, cfg):
    assert witness.user_witnessed(BODY, "s-empty", conn, cfg) is False


def test_user_witnessed_respects_n_prompts_window(conn, cfg):
    _journal_prompt(conn, "s-w", "remember that deploys happen only on fridays",
                    ts=1000)
    _journal_prompt(conn, "s-w", "unrelated question about the parser", ts=1001)
    body = "deploys happen only on fridays"
    assert witness.user_witnessed(body, "s-w", conn, cfg, n_prompts=1) is False
    assert witness.user_witnessed(body, "s-w", conn, cfg, n_prompts=2) is True


# ---- observer_witnessed: payload checks (spec 5.3, RT-4 adjunct) ----

def _cand(signal="learned_fix", payload=None):
    return {"signal": signal,
            "payload": payload if isinstance(payload, str)
            else json.dumps(payload if payload is not None else {})}


def test_observer_witnessed_learned_fix():
    assert witness.observer_witnessed(_cand("learned_fix", {"observer": True}))


def test_observer_witnessed_near_miss():
    assert witness.observer_witnessed(_cand("near_miss", {"observer": True}))


def test_observer_witnessed_wrong_signal():
    assert not witness.observer_witnessed(
        _cand("remember_request", {"observer": True}))


def test_observer_witnessed_missing_flag():
    assert not witness.observer_witnessed(_cand("learned_fix", {}))


def test_observer_witnessed_string_true_rejected():
    assert not witness.observer_witnessed(
        _cand("learned_fix", {"observer": "true"}))


def test_observer_witnessed_malformed_payload():
    assert not witness.observer_witnessed(_cand("learned_fix", "{not json"))


def test_observer_witnessed_none_row():
    assert not witness.observer_witnessed(None)


# ---- confirm_witnessed: AM-5 cases in raw and hashed modes ----

MEM = {"id": "01TESTMEMORYID0000000000AA",
       "title": "pytest needs PYTHONPATH exported",
       "body": BODY}


@pytest.mark.parametrize("hashed", [False, True], ids=["raw", "hashed"])
def test_confirm_cue_plus_title(conn, cfg, hashed):
    # contract: a cue word + the memory's title words in a recent user turn
    # must confirm in BOTH raw and hashed journal modes
    _journal_prompt(conn, "s-c",
                    "confirm the pytest pythonpath exported note is correct",
                    hashed=hashed)
    assert witness.confirm_witnessed(MEM, "s-c", conn, cfg) is True


@pytest.mark.parametrize("hashed", [False, True], ids=["raw", "hashed"])
def test_confirm_cue_plus_id(conn, cfg, hashed):
    _journal_prompt(conn, "s-c",
                    f"keep {MEM['id']} exactly as saved", hashed=hashed)
    assert witness.confirm_witnessed(MEM, "s-c", conn, cfg) is True


@pytest.mark.parametrize("hashed", [False, True], ids=["raw", "hashed"])
def test_confirm_body_restatement_without_cue(conn, cfg, hashed):
    _journal_prompt(conn, "s-c",
                    "pytest fails on imports unless pythonpath src is "
                    "exported when running bare", hashed=hashed)
    assert witness.confirm_witnessed(MEM, "s-c", conn, cfg) is True


@pytest.mark.parametrize("hashed", [False, True], ids=["raw", "hashed"])
def test_confirm_title_without_cue_fails(conn, cfg, hashed):
    _journal_prompt(conn, "s-c",
                    "the pytest pythonpath exported note came up again",
                    hashed=hashed)
    assert witness.confirm_witnessed(MEM, "s-c", conn, cfg) is False


@pytest.mark.parametrize("hashed", [False, True], ids=["raw", "hashed"])
def test_confirm_cue_without_reference_fails(conn, cfg, hashed):
    _journal_prompt(conn, "s-c", "confirm whatever seems reasonable to you",
                    hashed=hashed)
    assert witness.confirm_witnessed(MEM, "s-c", conn, cfg) is False


def test_confirm_only_last_five_turns(conn, cfg):
    _journal_prompt(conn, "s-c",
                    "confirm the pytest pythonpath exported note is correct",
                    ts=1000)
    for i in range(5):
        _journal_prompt(conn, "s-c", f"unrelated question number {i} here",
                        ts=1001 + i)
    assert witness.confirm_witnessed(MEM, "s-c", conn, cfg) is False


def test_confirm_no_prompts(conn, cfg):
    assert witness.confirm_witnessed(MEM, "s-none", conn, cfg) is False
