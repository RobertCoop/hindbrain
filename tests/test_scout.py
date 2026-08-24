"""mem scout: deterministic bootstrap survey (read-only)."""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEM = os.path.join(REPO, "bin", "mem")
sys.path.insert(0, REPO)

from lib import scout  # noqa: E402


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "fixture-proj"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "auth.py").write_text(
        "import os\n"
        "# HACK: token refresh must not run before the clock sync, see #142\n"
        "def refresh():\n"
        "    pass\n"
        "# plain comment, nothing notable here at all\n")
    (root / "Makefile").write_text(
        "test:\n"
        "\tPYTHONPATH=src pytest -x --no-header\n"
        "lint:\n"
        "\truff check .\n")
    (root / "package.json").write_text(json.dumps(
        {"scripts": {"build": "NODE_OPTIONS=--max-old-space-size=4096 webpack",
                     "start": "node server.js"}}))
    (root / ".env.example").write_text(
        "DATABASE_URL=\nVAULT_ADDR=\n# comment\nSECRET_KEY=\n")
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "name: ci\n"
        "env:\n"
        "  PYTHONPATH: src\n"
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - run: PYTEST_TIMEOUT=60 pytest -q\n")
    (root / ".python-version").write_text("3.11.9\n")

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                       check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t",
                            "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t"})

    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "initial import")
    for i in range(3):
        (root / "src" / "auth.py").write_text(
            f"# HACK: token refresh must not run before clock sync (rev {i})\n")
        git("add", "-A")
        git("commit", "-qm", f"fix auth token refresh again ({i})")
    return str(root)


def test_run_scout_finds_planted_evidence(proj):
    r = scout.run_scout(proj)
    cc = [c for c in r["confession_comments"] if "clock sync" in c["text"]]
    assert cc and cc[0]["file"].startswith("src")
    recipes = {t["recipe"] for t in r["task_runners"]}
    assert any("PYTHONPATH=src" in x for x in recipes)
    assert any("NODE_OPTIONS" in x for x in recipes)
    assert any("ruff" not in x for x in recipes)  # plain recipes not swept in
    ci = {c["text"] for c in r["ci"]}
    assert any("PYTHONPATH" in x for x in ci)
    assert any("PYTEST_TIMEOUT" in x for x in ci)
    assert any("again" in s for s in r["git"]["suspicious_commits"])
    assert any(c["file"] == "src/auth.py" and c["changes"] >= 3
               for c in r["git"]["churn_files"])
    env = {(e["file"], e["kind"]) for e in r["env"]}
    assert (".env.example", "env_keys") in env
    assert (".python-version", "version_pin") in env
    keys = [e["text"] for e in r["env"] if e["kind"] == "env_keys"][0]
    assert "DATABASE_URL" in keys and "=" not in keys  # names only, no values


def test_scout_empty_dir_clean(tmp_path):
    r = scout.run_scout(str(tmp_path))
    assert r["confession_comments"] == [] and r["task_runners"] == []
    assert r["git"] == {"suspicious_commits": [], "churn_files": []}
    lines = scout.format_text(r)
    assert len(lines) <= 30


def test_scout_skips_vendored_dirs(tmp_path):
    nm = tmp_path / "node_modules" / "dep"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("// HACK: do not ever ship this workaround\n")
    r = scout.run_scout(str(tmp_path))
    assert r["confession_comments"] == []


def test_cli_scout_text_and_json(proj, tmp_data):
    env = dict(os.environ, HINDBRAIN_DATA=str(tmp_data))
    env.pop("HINDBRAIN_DB", None)
    env.pop("HINDBRAIN_DISABLE", None)
    r = subprocess.run([sys.executable, MEM, "scout"], capture_output=True,
                       text=True, cwd=proj, env=env, timeout=60)
    assert r.returncode == 0
    assert len(r.stdout.strip().splitlines()) <= 30
    assert "confession comments" in r.stdout

    r = subprocess.run([sys.executable, MEM, "scout", "--json"],
                       capture_output=True, text=True, cwd=proj, env=env,
                       timeout=60)
    assert r.returncode == 0
    d = json.loads(r.stdout)
    assert set(d) == {"project", "confession_comments", "task_runners",
                      "ci", "git", "env"}

    r = subprocess.run([sys.executable, MEM, "scout", "--path", "/nonexistent"],
                       capture_output=True, text=True, cwd=proj, env=env,
                       timeout=60)
    assert r.returncode == 1
