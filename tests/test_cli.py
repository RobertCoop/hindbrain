"""M1 CLI acceptance: full round-trip via subprocess of bin/mem on a fresh data
dir, secrets refusal corpus (20 true / 20 false), resolution-pass corroboration,
body length limits, unique-prefix id lookup."""
import json
import os
import re
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEM = os.path.join(ROOT, "bin", "mem")


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    return str(p)


def mem_cmd(args, data, session, cwd):
    env = os.environ.copy()
    env["HINDBRAIN_DATA"] = data
    env["HINDBRAIN_SESSION"] = session
    env.pop("HINDBRAIN_DB", None)
    env.pop("HINDBRAIN_DISABLE", None)
    return subprocess.run([sys.executable, MEM] + list(args), capture_output=True,
                          text=True, cwd=cwd, env=env, timeout=60)


def saved_id(out):
    m = re.search(r"saved \[([0-9A-Za-z]+)\]", out)
    assert m, f"no saved id in output: {out!r}"
    return m.group(1)


def db_row(conn, mid):
    r = conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
    return dict(r) if r else None


def mem_count(conn):
    return conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]


def seed_candidate(conn, session, *, priority="P2", signal="learned_fix",
                   payload=None, draft=None, near=None, ts=None):
    from lib import ids
    cid = ids.ulid()
    conn.execute(
        "INSERT INTO candidate(id, session_id, agent_id, ts, priority, signal, "
        "payload, draft_cmd, near_match) VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, session, "main", ts or int(time.time()), priority, signal,
         json.dumps(payload or {}), draft, near))
    conn.commit()
    return cid


BODY_A = "pytest here needs PYTHONPATH=src exported; bare pytest fails on imports"
BODY_B = "docker compose here requires the buildkit flag exported in the environment"
BODY_NEW = "pytest now goes through the tox wrapper here; call tox -e py rather than bare pytest"


def test_full_round_trip(tmp_data, conn, proj):
    s = "sess-roundtrip"

    # save
    r = mem_cmd(["save", "--kind", "gotcha", "--scope", "command:pytest", BODY_A],
                tmp_data, s, proj)
    assert r.returncode == 0, r.stderr
    assert "authority=pending" in r.stdout
    mid = saved_id(r.stdout)
    row = db_row(conn, mid)
    assert row["kind"] == "gotcha"
    assert row["scope_type"] == "command" and row["scope_value"] == "pytest"
    assert row["project"] == proj and row["channel"] == "agent"
    assert row["status"] == "active"
    # synthetic prior access row exists
    events = [e[0] for e in conn.execute(
        "SELECT event FROM access_log WHERE memory_id=? ORDER BY ts", (mid,))]
    assert "synthetic" in events

    # get (full record + fetched escalation)
    r = mem_cmd(["get", mid], tmp_data, s, proj)
    assert r.returncode == 0
    assert BODY_A in r.stdout and mid in r.stdout
    events = [e[0] for e in conn.execute(
        "SELECT event FROM access_log WHERE memory_id=?", (mid,))]
    assert "fetched" in events

    # search
    r = mem_cmd(["search", "pytest PYTHONPATH"], tmp_data, s, proj)
    assert r.returncode == 0 and mid in r.stdout

    # list
    r = mem_cmd(["list"], tmp_data, s, proj)
    assert r.returncode == 0 and mid in r.stdout
    r = mem_cmd(["list", "--kind", "fact"], tmp_data, s, proj)
    assert mid not in r.stdout

    # supersede: new record, old superseded
    r = mem_cmd(["supersede", mid, BODY_NEW], tmp_data, s, proj)
    assert r.returncode == 0, r.stderr
    new_id = saved_id(r.stdout)
    assert f"supersedes [{mid}]" in r.stdout
    old = db_row(conn, mid)
    assert old["status"] == "superseded" and old["invalidated_at"]
    new = db_row(conn, new_id)
    assert new["supersedes"] == mid
    assert new["kind"] == "gotcha" and new["scope_value"] == "pytest"  # inherited
    r = mem_cmd(["list"], tmp_data, s, proj)
    assert new_id in r.stdout and mid not in r.stdout

    # audit shows lineage both directions
    r = mem_cmd(["audit", new_id], tmp_data, s, proj)
    assert r.returncode == 0
    assert f"supersedes: [{mid}]" in r.stdout
    r = mem_cmd(["audit", mid], tmp_data, s, proj)
    assert f"superseded by: [{new_id}]" in r.stdout
    assert "access log" in r.stdout

    # refute a second memory
    r = mem_cmd(["save", "--kind", "env", "--scope", "project", BODY_B],
                tmp_data, s, proj)
    mid2 = saved_id(r.stdout)
    r = mem_cmd(["refute", mid2, "turned out wrong"], tmp_data, s, proj)
    assert r.returncode == 0 and "refuted" in r.stdout
    assert db_row(conn, mid2)["status"] == "refuted"
    r = mem_cmd(["list"], tmp_data, s, proj)
    assert mid2 not in r.stdout

    # pin / unpin
    r = mem_cmd(["pin", new_id], tmp_data, s, proj)
    assert r.returncode == 0 and db_row(conn, new_id)["pinned"] == 1
    r = mem_cmd(["list", "--pinned"], tmp_data, s, proj)
    assert new_id in r.stdout
    r = mem_cmd(["unpin", new_id], tmp_data, s, proj)
    assert r.returncode == 0 and db_row(conn, new_id)["pinned"] == 0
    r = mem_cmd(["list", "--pinned"], tmp_data, s, proj)
    assert new_id not in r.stdout

    # stats
    r = mem_cmd(["stats"], tmp_data, s, proj)
    assert r.returncode == 0
    assert "memories: 3" in r.stdout
    assert "by kind:" in r.stdout and "gotcha" in r.stdout
    assert "by status:" in r.stdout


def test_queue_drop_and_from_candidate(tmp_data, conn, proj):
    s = "sess-queue"
    now = int(time.time())
    c1 = seed_candidate(
        conn, s, priority="P2", signal="learned_fix", ts=now - 10,
        payload={"fail_cmd": "pytest", "ok_cmd": "PYTHONPATH=src pytest",
                 "observer": True},
        draft='mem save --kind gotcha --scope command:pytest "..."')
    c2 = seed_candidate(
        conn, s, priority="P0", signal="remember_request", ts=now - 5,
        payload={"text": "user prefers ruff over flake8 for linting"})

    r = mem_cmd(["queue"], tmp_data, s, proj)
    assert r.returncode == 0
    assert "(1)" in r.stdout and "(2)" in r.stdout
    assert "ruff over flake8" in r.stdout

    r = mem_cmd(["drop", "2"], tmp_data, s, proj)
    assert r.returncode == 0
    assert conn.execute("SELECT status FROM candidate WHERE id=?",
                        (c2,)).fetchone()[0] == "dropped"
    r = mem_cmd(["queue"], tmp_data, s, proj)
    assert "(2)" not in r.stdout

    # save from the observer-witnessed candidate -> standard authority
    body = "pytest requires PYTHONPATH=src exported in this repo; bare runs fail on imports"
    r = mem_cmd(["save", "--from-candidate", "1", "--kind", "gotcha",
                 "--scope", "command:pytest", body], tmp_data, s, proj)
    assert r.returncode == 0, r.stderr
    assert "authority=standard" in r.stdout
    assert conn.execute("SELECT status FROM candidate WHERE id=?",
                        (c1,)).fetchone()[0] == "saved"
    r = mem_cmd(["queue"], tmp_data, s, proj)
    assert "queue empty" in r.stdout


def test_corroborate_promotes_pending_to_standard(tmp_data, conn, proj):
    s = "sess-corr"
    r = mem_cmd(["save", "--kind", "decision", "--scope", "project",
                 "we chose sqlite over postgres here for the zero-dependency install"],
                tmp_data, s, proj)
    mid = saved_id(r.stdout)
    assert db_row(conn, mid)["authority"] == "pending"

    r = mem_cmd(["corroborate", mid], tmp_data, s, proj)
    assert r.returncode == 0 and "count=1" in r.stdout
    assert db_row(conn, mid)["authority"] == "pending"

    # same-session retry is idempotent: no ratchet toward standard
    r = mem_cmd(["corroborate", mid], tmp_data, s, proj)
    assert r.returncode == 0
    assert "count unchanged" in r.stdout
    row = db_row(conn, mid)
    assert row["corroborations"] == 1 and row["authority"] == "pending"

    # a distinct session's corroboration promotes
    r = mem_cmd(["corroborate", mid], tmp_data, "sess-corr-2", proj)
    assert r.returncode == 0
    assert "promoted pending -> standard" in r.stdout
    row = db_row(conn, mid)
    assert row["corroborations"] == 2 and row["authority"] == "standard"


# ---- secrets refusal corpus (spec 7.8 / 13.1): 20 true, 20 false ----

TRUE_SECRETS = [
    ("pem_rsa", "deploy key material -----BEGIN RSA PRIVATE KEY----- MIIEpAIBAAKCAQEA kept in ci"),
    ("pem_ec", "backup of -----BEGIN EC PRIVATE KEY----- MHcCAQEEIB stored in the vault"),
    ("pem_openssh", "found -----BEGIN OPENSSH PRIVATE KEY----- b3BlbnNzaC1rZXk in the home dir"),
    ("akia_1", "s3 sync uses AKIAIOSFODNN7EXAMPLE as the access key id"),
    ("akia_2", "old creds AKIA0123456789ABCDEF still active in prod"),
    ("ghp_1", "github pat ghp_1234567890abcdefghij1234567890ABCDEF works for repo scope"),
    ("ghp_2", "ci uses ghp_ABCDEFGHIJabcdefghij0123456789KLMNOP for releases"),
    ("xoxb", "slack bot token xoxb-2407-9876-abcdefghijklmnop posts the alerts"),
    ("xoxp", "user token xoxp-1111-2222-3333-abcdef0123 grants admin access"),
    ("sk_1", "openai key sk-abc123def456ghi789jkl012mno powers the summarizer"),
    ("sk_2", "billing worker uses sk-ZYXWVUTSRQPONMLKJIHGFEDCBA99 in staging"),
    ("jwt_1", "session cookie is eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.sig here"),
    ("jwt_2", "auth header bearer eyJraWQiOiJhYmNkZWYxMjM0NTYiLCJhbGciOiJSUzI1NiJ9.payload.sig2"),
    ("password_real", "the staging db password=hunter2secret99 rotates monthly"),
    ("passwd_real", "root login passwd: Sup3rSecretPw11 on the old box"),
    ("token_real", "webhook uses token=9f8e7d6c5b4a3210fedc for verification"),
    ("secret_real", "stripe secret=whsec4f9d8e7c6b5a guards the endpoint"),
    ("api_key_real", "grafana api_key=zk39dj29dk29dj29aa is set in the env"),
    ("entropy_1", "artifact signer uses kQ7mXz2Rv9Lt4Wc8Jd1Pn6Ba as its seed value"),
    ("entropy_2", "the vault unseal string Gh5Tz1Kq8Xw3Vb7Nm2Rc9Ld4Pj6S must never leak"),
]

FALSE_POSITIVES = [
    ("placeholder_angle", "password=<your-password> goes in the local env file"),
    ("placeholder_var", "compose reads password=$DB_PASSWORD from the environment"),
    ("placeholder_changeme", "password=changeme ships as the default and must be rotated"),
    ("placeholder_example", "docs show token=example123 as a placeholder value"),
    ("placeholder_xxx", "replace secret=xxx before deploying the webhook config"),
    ("placeholder_brace", "the template uses api_key={API_KEY} substitution at render time"),
    ("placeholder_none", "password: none by default when auth is disabled in dev"),
    ("short_sk", "sk-test appears in the docs as a sample prefix only"),
    ("bare_ghp", "personal tokens use the ghp underscore prefix on github"),
    ("bare_akia", "aws access key ids start with the AKIA prefix string"),
    ("bare_xoxb", "slack bot tokens use the xoxb dash prefix convention"),
    ("bare_jwt", "jwts begin with eyJ and contain two dot separators"),
    ("prose_pythonpath", "pytest wants PYTHONPATH=src before collecting the tests"),
    ("prose_git", "prefer git push with force-with-lease on shared branches"),
    ("prose_env", "the deploy script reads DATABASE_URL from the environment at boot"),
    ("prose_short_token", "short fixture token abc123 appears throughout the test data"),
    ("prose_rotation", "password rotation happens every ninety days per company policy"),
    ("prose_scanning", "secret scanning runs on every push to the main branch"),
    ("prose_header", "the api-key header name for this service is X-Api-Key"),
    ("prose_numbers", "connection timeout=30 and retries=5 in the example config"),
]


def test_secrets_all_true_refused(tmp_data, conn, proj):
    s = "sess-secrets-true"
    misses = []
    for label, body in TRUE_SECRETS:
        r = mem_cmd(["save", "--kind", "fact", "--scope", "project", body],
                    tmp_data, s, proj)
        if r.returncode != 2 or "refused" not in r.stderr:
            misses.append((label, r.returncode, r.stderr.strip()))
    assert not misses, f"true secrets not refused: {misses}"
    assert mem_count(conn) == 0  # nothing persisted


def test_secrets_all_false_accepted(tmp_data, conn, proj):
    s = "sess-secrets-false"
    misses = []
    for label, body in FALSE_POSITIVES:
        r = mem_cmd(["save", "--kind", "fact", "--scope", "project", body],
                    tmp_data, s, proj)
        if r.returncode != 0 or "saved [" not in r.stdout:
            misses.append((label, r.returncode, (r.stderr or r.stdout).strip()))
    assert not misses, f"false positives rejected: {misses}"
    assert mem_count(conn) == len(FALSE_POSITIVES)


def test_resolution_pass_corroborates_near_duplicate(tmp_data, conn, proj):
    s = "sess-resolve"
    body = "pytest in this repo needs PYTHONPATH=src exported; bare pytest fails on imports"
    near = "pytest in this repo needs PYTHONPATH=src exported because bare pytest fails on imports"
    r = mem_cmd(["save", "--kind", "gotcha", "--scope", "command:pytest", body],
                tmp_data, s, proj)
    mid = saved_id(r.stdout)
    assert mem_count(conn) == 1

    r = mem_cmd(["save", "--kind", "gotcha", "--scope", "command:pytest", near],
                tmp_data, s, proj)
    assert r.returncode == 0, r.stderr
    assert f"corroborated existing [{mid}]" in r.stdout
    assert "saved [" not in r.stdout
    assert mem_count(conn) == 1  # no insert
    assert db_row(conn, mid)["corroborations"] == 1


def test_body_length_limits(tmp_data, conn, proj):
    s = "sess-len"
    r = mem_cmd(["save", "--kind", "fact", "--scope", "project", "too short body!"],
                tmp_data, s, proj)
    assert r.returncode == 1 and "20..2000" in r.stderr

    r = mem_cmd(["save", "--kind", "fact", "--scope", "project", "k" * 2001],
                tmp_data, s, proj)
    assert r.returncode == 1 and "20..2000" in r.stderr
    assert mem_count(conn) == 0

    r = mem_cmd(["save", "--kind", "fact", "--scope", "project", "k" * 20],
                tmp_data, s, proj)
    assert r.returncode == 0, r.stderr

    r = mem_cmd(["save", "--kind", "fact", "--scope", "project", "m" * 2000],
                tmp_data, s, proj)
    assert r.returncode == 0, r.stderr
    assert mem_count(conn) == 2


def test_usage_error_exits_1_refusal_exits_2(tmp_data, conn, proj):
    # §9.1: exit 2 is reserved for refusals; usage errors are user errors
    s = "sess-exitcodes"
    r = mem_cmd(["frobnicate"], tmp_data, s, proj)
    assert r.returncode == 1
    assert "error" in r.stderr.lower()

    r = mem_cmd([], tmp_data, s, proj)
    assert r.returncode == 1

    r = mem_cmd(["save", "--kind", "fact", "--scope", "project",
                 "ci uses ghp_ABCDEFGHIJabcdefghij0123456789KLMNOP for releases"],
                tmp_data, s, proj)
    assert r.returncode == 2 and "refused" in r.stderr


def test_unique_prefix_id_lookup(tmp_data, conn, proj):
    s = "sess-prefix"
    body = "the release job signs artifacts on the runner before uploading them anywhere"
    r = mem_cmd(["save", "--kind", "procedure", "--scope", "project", body],
                tmp_data, s, proj)
    mid = saved_id(r.stdout)

    # unique prefix (>= 6 chars) resolves
    r = mem_cmd(["get", mid[:10]], tmp_data, s, proj)
    assert r.returncode == 0 and mid in r.stdout and body in r.stdout

    # prefix shorter than 6 chars is not a lookup
    r = mem_cmd(["get", mid[:5]], tmp_data, s, proj)
    assert r.returncode == 1

    # ambiguous prefix fails cleanly
    now = int(time.time())
    for tail in ("AAA", "BBB"):
        conn.execute(
            "INSERT INTO memory(id, title, body, kind, scope_type, project, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("PREFIXAMBIG000000000000" + tail, "ambig " + tail,
             "ambiguous prefix fixture body " + tail, "fact", "project", proj, now))
    conn.commit()
    r = mem_cmd(["get", "PREFIXAMBIG"], tmp_data, s, proj)
    assert r.returncode == 1
    assert "not found" in r.stderr or "ambiguous" in r.stderr
    r = mem_cmd(["get", "PREFIXAMBIG000000000000A"], tmp_data, s, proj)
    assert r.returncode == 0 and "PREFIXAMBIG000000000000AAA" in r.stdout


def test_save_prior_flag(tmp_data, conn, proj):
    # --prior sets cold-start activation strength (clamped) but never authority
    s = "sess-prior"
    r = mem_cmd(["save", "--kind", "gotcha", "--scope", "command:make",
                 "--prior", "3.0",
                 "make deploy here needs VAULT_ADDR exported first"],
                str(tmp_data), s, proj)
    assert r.returncode == 0
    mid = saved_id(r.stdout)
    row = conn.execute("SELECT prior, authority FROM memory WHERE id=?",
                       (mid,)).fetchone()
    assert row["prior"] == 3.0
    assert row["authority"] == "pending"  # strength never raises authority
    syn = conn.execute(
        "SELECT weight FROM access_log WHERE memory_id=? AND event='synthetic'",
        (mid,)).fetchone()
    assert syn["weight"] == 3.0

    # clamped to [0.5, 3.0]
    r = mem_cmd(["save", "--kind", "fact", "--scope", "project",
                 "--prior", "99",
                 "the staging cluster lives in the eu-west-1 region"],
                str(tmp_data), s, proj)
    assert r.returncode == 0
    row = conn.execute("SELECT prior FROM memory WHERE id=?",
                       (saved_id(r.stdout),)).fetchone()
    assert row["prior"] == 3.0

    # default unchanged without the flag
    r = mem_cmd(["save", "--kind", "fact", "--scope", "project",
                 "integration tests hit a live localstack container"],
                str(tmp_data), s, proj)
    assert r.returncode == 0
    row = conn.execute("SELECT prior FROM memory WHERE id=?",
                       (saved_id(r.stdout),)).fetchone()
    assert row["prior"] == 1.0


def test_confirm_cross_session_fallback(tmp_data, conn, proj):
    # hooks journaled the user's turn under a different session id (resume/
    # fork/env drift): confirm still witnesses via same-project recent turns
    import time as _t
    from lib import ids as _ids
    mid = _ids.ulid()
    conn.execute(
        "INSERT INTO memory(id, title, body, kind, scope_type, project, "
        "authority, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (mid, "deploy needs vault", "deploys here need VAULT_ADDR exported "
         "before make deploy", "gotcha", "project", proj, "pending",
         int(_t.time())))
    conn.execute(
        "INSERT INTO journal(session_id, agent_id, ts, event, data) "
        "VALUES (?,?,?,?,?)",
        ("hook-session-abc", "main", int(_t.time()),
         "user_prompt", json.dumps({"text": f"yes, confirm {mid}",
                                    "project": proj})))
    conn.commit()

    r = mem_cmd(["confirm", mid], str(tmp_data), "cli-session-xyz", proj)
    assert r.returncode == 0 and "authority=full" in r.stdout

    # a stale (>15 min) cross-session turn does NOT witness
    mid2 = _ids.ulid()
    conn.execute(
        "INSERT INTO memory(id, title, body, kind, scope_type, project, "
        "authority, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (mid2, "stale note", "another note body long enough to save here",
         "fact", "project", proj, "pending", int(_t.time())))
    conn.execute(
        "INSERT INTO journal(session_id, agent_id, ts, event, data) "
        "VALUES (?,?,?,?,?)",
        ("hook-session-abc", "main", int(_t.time()) - 3600,
         "user_prompt", json.dumps({"text": f"yes, confirm {mid2}",
                                    "project": proj})))
    conn.commit()
    r = mem_cmd(["confirm", mid2], str(tmp_data), "cli-session-other", proj)
    assert r.returncode == 0 and "downgraded to corroborate" in r.stdout
    # refusal message is diagnostic: names the searched session and the
    # fallback turns it did consider (part 1's recent turn is still in window)
    assert "searched session cli-session-other" in r.stdout
    assert "recent same-project turn" in r.stdout


def test_handshake_bridges_envless_shell(tmp_data, conn, proj, monkeypatch):
    # root-cause fix: hooks write a handshake at a fixed home path; an env-less
    # CLI resolves the same store and session from it instead of forking a
    # parallel store as "unbound"
    from lib import paths as _paths
    hs_dir = os.environ["HINDBRAIN_HANDSHAKE_DIR"]  # set by tmp_data fixture
    _paths.write_handshake(proj, "hook-sess-42")

    env = os.environ.copy()
    for k in ("HINDBRAIN_DATA", "HINDBRAIN_DB", "HINDBRAIN_SESSION",
              "CLAUDE_PLUGIN_DATA", "HINDBRAIN_DISABLE",
              "CLAUDE_CODE_SESSION_ID"):
        env.pop(k, None)
    env["HINDBRAIN_HANDSHAKE_DIR"] = hs_dir
    r = subprocess.run(
        [sys.executable, MEM, "save", "--kind", "gotcha",
         "--scope", "command:make",
         "make deploy here needs VAULT_ADDR exported first"],
        capture_output=True, text=True, cwd=proj, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    row = conn.execute(
        "SELECT source_session FROM memory ORDER BY created_at DESC").fetchone()
    assert row["source_session"] == "hook-sess-42"  # not "unbound"

    # sticky workspace binding: from INSIDE a nested repo, project still
    # resolves to the workspace the handshake describes, not the repo
    repo = os.path.join(proj, "repoA")
    os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
    r = subprocess.run(
        [sys.executable, MEM, "save", "--kind", "fact", "--scope", "project",
         "the staging cluster for this workspace lives in eu-west-1"],
        capture_output=True, text=True, cwd=repo, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    row = conn.execute(
        "SELECT project, source_session FROM memory "
        "ORDER BY created_at DESC, rowid DESC").fetchone()
    assert row["project"] == proj  # workspace, not proj/repoA
    assert row["source_session"] == "hook-sess-42"


def test_reinit_preview_wipe_and_backup(tmp_data, conn, proj):
    import glob
    from lib import db as _db, ids as _ids
    s = "sess-reinit"
    now = int(time.time())

    def mk(project, body):
        mid = _ids.ulid()
        conn.execute(
            "INSERT INTO memory(id, title, body, kind, scope_type, project, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (mid, body[:80], body, "fact", "project", project, now))
        conn.commit()
        return mid

    ws1 = mk(proj, "workspace note that should be erased on re-init")
    ws2 = mk(proj, "second workspace note that should also be erased")
    other = mk("/elsewhere", "other project's note that must survive")
    glob_ = mk("", "global note shared across projects that must survive")
    _db.log_access(conn, ws1, s, "main", "injected")
    _db.upsert_link(conn, ws1, ws2, 0.7)
    seed_candidate(conn, s, payload={"project": proj, "text": "x"})
    seed_candidate(conn, "s-other", payload={"project": "/elsewhere"})

    # preview deletes nothing
    r = mem_cmd(["re-init"], str(tmp_data), s, proj)
    assert r.returncode == 0
    assert "2 memories" in r.stdout and "1 candidates" in r.stdout
    assert "nothing was deleted" in r.stdout
    assert conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 4

    # --yes wipes workspace scope only, after a backup
    r = mem_cmd(["re-init", "--yes"], str(tmp_data), s, proj)
    assert r.returncode == 0 and "erased 2 memories" in r.stdout
    left = {x[0] for x in conn.execute("SELECT id FROM memory")}
    assert left == {other, glob_}
    assert conn.execute("SELECT COUNT(*) FROM access_log WHERE memory_id=?",
                        (ws1,)).fetchone()[0] == 0
    assert _db.links_for(conn, ws1) == []
    assert conn.execute(
        "SELECT COUNT(*) FROM candidate WHERE session_id='s-other'"
    ).fetchone()[0] == 1  # other project's candidate survives

    baks = sorted(glob.glob(os.path.join(str(tmp_data), "hindbrain.db.reinit-*.bak")))
    assert len(baks) >= 1
    import sqlite3 as _sq
    bconn = _sq.connect(baks[0])
    assert bconn.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 4
    bconn.close()

    # --all-projects clears the rest (backup count grows)
    r = mem_cmd(["re-init", "--yes", "--all-projects"], str(tmp_data), s, proj)
    assert r.returncode == 0
    assert conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 0
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'"
                        ).fetchone()[0] == "2"


def test_claude_code_session_id_resolution(tmp_data, conn, proj):
    # harness-set CLAUDE_CODE_SESSION_ID beats the clobber-prone handshake
    # and loses only to --session / HINDBRAIN_SESSION
    from lib import paths as _paths
    _paths.write_handshake(proj, "stale-clobbered-session")

    env = os.environ.copy()
    for k in ("HINDBRAIN_DB", "HINDBRAIN_DISABLE", "HINDBRAIN_SESSION"):
        env.pop(k, None)
    env["HINDBRAIN_DATA"] = str(tmp_data)
    env["CLAUDE_CODE_SESSION_ID"] = "live-harness-session"
    r = subprocess.run(
        [sys.executable, MEM, "save", "--kind", "fact", "--scope", "project",
         "the deploy queue drains through the eu-west-1 worker pool"],
        capture_output=True, text=True, cwd=proj, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    row = conn.execute("SELECT source_session FROM memory "
                       "ORDER BY created_at DESC").fetchone()
    assert row["source_session"] == "live-harness-session"

    # HINDBRAIN_SESSION still outranks it
    env["HINDBRAIN_SESSION"] = "explicit-contract"
    r = subprocess.run(
        [sys.executable, MEM, "save", "--kind", "fact", "--scope", "project",
         "the staging queue drains through the us-east-1 worker pool"],
        capture_output=True, text=True, cwd=proj, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    row = conn.execute("SELECT source_session FROM memory "
                       "ORDER BY created_at DESC, rowid DESC").fetchone()
    assert row["source_session"] == "explicit-contract"


def test_confirm_fallback_on_stale_session_binding(tmp_data, conn, proj):
    # the live-usage failure: session binding resolved to a WRONG but
    # nonempty session (concurrent session clobbered the handshake); the
    # user's confirming turn was journaled under the real session id.
    # confirm now falls back on NO MATCH, not only on zero rows.
    import time as _t
    from lib import ids as _ids
    mid = _ids.ulid()
    conn.execute(
        "INSERT INTO memory(id, title, body, kind, scope_type, project, "
        "authority, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (mid, "deploy needs vault", "deploys here need VAULT_ADDR exported "
         "before make deploy", "gotcha", "project", proj, "pending",
         int(_t.time())))
    # stale session has an unrelated turn on record (nonzero rows!)
    conn.execute(
        "INSERT INTO journal(session_id, agent_id, ts, event, data) "
        "VALUES (?,?,?,?,?)",
        ("stale-session", "main", int(_t.time()),
         "user_prompt", json.dumps({"text": "unrelated chatter about lunch",
                                    "project": proj})))
    # the real confirming turn lives under the live session id
    conn.execute(
        "INSERT INTO journal(session_id, agent_id, ts, event, data) "
        "VALUES (?,?,?,?,?)",
        ("live-session", "main", int(_t.time()),
         "user_prompt", json.dumps({"text": f"yes, confirm {mid}",
                                    "project": proj})))
    conn.commit()

    r = mem_cmd(["confirm", mid], str(tmp_data), "stale-session", proj)
    assert r.returncode == 0 and "authority=full" in r.stdout

    # and when nothing matches anywhere, the refusal names the likely cause
    mid2 = _ids.ulid()
    conn.execute(
        "INSERT INTO memory(id, title, body, kind, scope_type, project, "
        "authority, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (mid2, "unconfirmed note", "another note body long enough to save",
         "fact", "project", proj, "pending", int(_t.time())))
    conn.commit()
    r = mem_cmd(["confirm", mid2], str(tmp_data), "stale-session", proj)
    assert "downgraded to corroborate" in r.stdout
    assert "session binding may be stale" in r.stdout
    assert "--session $CLAUDE_CODE_SESSION_ID" in r.stdout
