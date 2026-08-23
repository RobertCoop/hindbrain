#!/usr/bin/env python3
"""Latency harness (spec 13.4).

Standalone:  python3 tests/perf/latency_harness.py   (exit 1 on budget breach)
Pytest:      pytest -m perf tests/perf/latency_harness.py

Seeds 5,000 synthetic memories + 50k access rows into a tmp HINDBRAIN_DATA,
then times prompt_gate, pretool_gate (edit + bash branches) and failure_gate
200x each via subprocess — interpreter spawn included, that is the honest
number. Fails if any branch has p95 >= 100 ms or p99 >= 250 ms.
"""
import json
import math
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from lib import db as hdb
from lib import ids

SCRIPTS = os.path.join(ROOT, "scripts")
N_MEMORIES = 5000
N_ACCESS = 50000
N_RUNS = 200
N_WARMUP = 2
P95_BUDGET_MS = 100.0
P99_BUDGET_MS = 250.0

# dev-ish vocabulary; deliberately excludes P0/correction trigger words
WORDS = ("pytest fixture sqlite migration schema index query timeout retry cache "
         "docker compose container deploy pipeline artifact build lint format "
         "typecheck async await coroutine thread lock mutex race deadlock "
         "config toml yaml json parse serialize encode decode utf8 unicode "
         "api endpoint route handler middleware auth token session cookie "
         "database transaction rollback commit vacuum analyze pragma journal "
         "module package import namespace pathlib subprocess environ shell "
         "branch merge rebase conflict staging upstream tag release version "
         "logging metric tracing profiler latency throughput percentile budget "
         "numpy pandas dataframe column series groupby aggregate filter sort "
         "frontend backend template render component state props hook effect "
         "kubernetes ingress service replica volume secret namespace cluster "
         "terraform provider resource variable output plan state backend "
         "regression coverage assertion mock patch stub spy snapshot golden").split()

KINDS = ["gotcha", "decision", "preference", "fact", "procedure", "env"]
AUTHORITIES = ["full"] * 15 + ["standard"] * 35 + ["pending"] * 40 + ["quarantined"] * 10
COMMAND_HEADS = ["pytest", "npm.run", "npm.install", "cargo.build", "make",
                 "python3", "node", "git.push", "docker.build", "pip"]
PATH_GLOBS = ["src/*.py", "tests/*.py", "src/api/*.py", "*.toml", "scripts/*",
              "lib/**/*.py", "src/models/*.py", "docs/*.md"]


def _sentence(rng, lo, hi):
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(lo, hi)))


def _body(rng):
    # lognormal length distribution clipped to the CLI's 20..2000 char range
    target = int(min(2000, max(20, rng.lognormvariate(4.8, 0.7))))
    parts = []
    while sum(len(p) + 1 for p in parts) < target:
        parts.append(_sentence(rng, 4, 12) + ".")
    return " ".join(parts)[:2000]


def seed(data_dir, project):
    rng = random.Random(42)
    conn = sqlite3.connect(os.path.join(data_dir, "hindbrain.db"))
    conn.executescript(hdb.DDL)
    now = int(time.time())

    mems = []
    for i in range(N_MEMORIES):
        scope_roll = rng.random()
        if scope_roll < 0.40:
            scope_type, scope_value = "project", ""
        elif scope_roll < 0.65:
            scope_type, scope_value = "path", rng.choice(PATH_GLOBS)
        elif scope_roll < 0.85:
            scope_type, scope_value = "command", rng.choice(COMMAND_HEADS)
        elif scope_roll < 0.90:
            scope_type, scope_value = "tool", rng.choice(["Bash", "Edit", "Write"])
        else:
            scope_type, scope_value = "global", ""
        body = _body(rng)
        status = "active" if rng.random() < 0.90 else rng.choice(
            ["superseded", "expired"])
        hazard = 1 if (scope_type == "command"
                       and scope_value in ("git.push", "docker.build")
                       and rng.random() < 0.05) else 0
        mems.append((
            ids.ulid(), body[:80], body, rng.choice(KINDS), scope_type,
            scope_value, project if rng.random() < 0.6 else "",
            " ".join(rng.sample(WORDS, 3)), "agent", rng.choice(AUTHORITIES),
            status, hazard, "deny", 0, None, None, None, None,
            rng.choice([1.0, 1.0, 2.0, 3.0]), rng.randint(0, 3), "seed", None,
            now - rng.randint(0, 180 * 86400), None, 0,
        ))
    conn.executemany(
        "INSERT INTO memory (id,title,body,kind,scope_type,scope_value,project,"
        "tags,channel,authority,status,hazard,hazard_mode,pinned,supersedes,"
        "valid_from,invalidated_at,ttl_days,prior,corroborations,source_session,"
        "source_event,created_at,last_access_at,access_count) "
        "VALUES (" + ",".join("?" * 25) + ")", mems)

    events = ["synthetic"] * 10 + ["injected"] * 30 + ["reminded"] * 40 + \
             ["fetched"] * 10 + ["cited"] * 8 + ["denied"] * 2
    acc = []
    for _ in range(N_ACCESS):
        m = mems[int(N_MEMORIES * rng.random() ** 3)]  # zipf-ish skew
        ev = rng.choice(events)
        w = 3.0 if ev in ("fetched", "denied", "synthetic") else 1.0
        acc.append((m[0], f"perfsess-{rng.randint(0, 30)}", "main",
                    now - rng.randint(0, 90 * 86400), ev, w, None))
    conn.executemany(
        "INSERT INTO access_log (memory_id,session_id,agent_id,ts,event,weight,query) "
        "VALUES (?,?,?,?,?,?,?)", acc)
    conn.commit()
    # merge the trigger-built FTS segments into steady-state shape
    conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('optimize')")
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()


def _payloads(project):
    rng = random.Random(7)
    sessions = [f"latsess-{i}" for i in range(8)]
    prompt, edit, bash, fail = [], [], [], []
    bash_cmds = ["pytest tests/test_api.py -q", "npm run build",
                 "python3 scripts/migrate.py --dry-run", "make lint",
                 "cargo build --release && cargo test", "pip install -e ."]
    for i in range(25):
        sess = sessions[i % len(sessions)]
        base = {"session_id": sess, "cwd": project, "transcript_path": ""}
        prompt.append(json.dumps({
            **base, "hook_event_name": "UserPromptSubmit",
            "prompt": "how should the " + _sentence(rng, 10, 22) + "?"}))
        edit.append(json.dumps({
            **base, "hook_event_name": "PreToolUse", "tool_name": "Edit",
            "tool_input": {
                "file_path": os.path.join(project, "src", f"mod_{i % 6}.py"),
                "old_string": "def old():\n    pass\n",
                "new_string": ("def handler_%d():\n    # %s\n" % (i, _sentence(rng, 6, 10))
                               + "".join(f"    {w} = load_{w}()\n"
                                         for w in rng.sample(WORDS, 40)))}}))
        bash.append(json.dumps({
            **base, "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": bash_cmds[i % len(bash_cmds)]}}))
        fail.append(json.dumps({
            **base, "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
            "tool_input": {"command": bash_cmds[i % len(bash_cmds)]},
            "tool_response": (
                "Traceback (most recent call last):\n"
                f"  File \"src/mod_{i % 6}.py\", line {40 + i}, in handler\n"
                f"    {rng.choice(WORDS)} = load()\n"
                f"ModuleNotFoundError: No module named '{rng.choice(WORDS)}'\n"
                "error: command failed with exit code 1")}))
    return [("prompt_gate", "prompt_gate.py", prompt),
            ("pretool_gate/edit", "pretool_gate.py", edit),
            ("pretool_gate/bash", "pretool_gate.py", bash),
            ("failure_gate", "failure_gate.py", fail)]


def _time_gate(script, payloads, env):
    times = []
    for i in range(N_WARMUP + N_RUNS):
        payload = payloads[i % len(payloads)]
        t0 = time.perf_counter()
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, script)],
                           input=payload.encode(), capture_output=True, env=env)
        dt = (time.perf_counter() - t0) * 1000.0
        if p.returncode != 0:
            raise RuntimeError(f"{script} exited {p.returncode}: "
                               f"{p.stderr.decode(errors='replace')[:500]}")
        if i >= N_WARMUP:
            times.append(dt)
    return times


def _pct(xs, q):
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, math.ceil(q / 100.0 * len(s)) - 1))]


def run_harness():
    results = []
    with tempfile.TemporaryDirectory(prefix="hindbrain-perf-") as tmp:
        data = os.path.join(tmp, "data")
        project = os.path.join(tmp, "project")
        os.makedirs(data)
        os.makedirs(os.path.join(project, ".git"))
        seed(data, project)

        env = dict(os.environ)
        env["HINDBRAIN_DATA"] = data
        for k in ("HINDBRAIN_DB", "HINDBRAIN_DISABLE", "HINDBRAIN_SESSION"):
            env.pop(k, None)

        for name, script, payloads in _payloads(project):
            times = _time_gate(script, payloads, env)
            results.append({"name": name, "n": len(times),
                            "p50": _pct(times, 50), "p95": _pct(times, 95),
                            "p99": _pct(times, 99), "max": max(times)})
    breaches = [r["name"] for r in results
                if r["p95"] >= P95_BUDGET_MS or r["p99"] >= P99_BUDGET_MS]
    return results, breaches


def print_table(results, breaches):
    hdr = f"{'gate':<20} {'n':>4} {'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} {'max ms':>8}  verdict"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        ok = "FAIL" if r["name"] in breaches else "ok"
        print(f"{r['name']:<20} {r['n']:>4} {r['p50']:>8.1f} {r['p95']:>8.1f} "
              f"{r['p99']:>8.1f} {r['max']:>8.1f}  {ok}")
    print(f"budget: p95 < {P95_BUDGET_MS:.0f} ms, p99 < {P99_BUDGET_MS:.0f} ms "
          "(interpreter spawn included)")


def main():
    results, breaches = run_harness()
    print_table(results, breaches)
    if breaches:
        print(f"LATENCY BUDGET BREACHED: {', '.join(breaches)}", file=sys.stderr)
        return 1
    return 0


try:
    import pytest
except ImportError:
    pytest = None

if pytest is not None:
    @pytest.mark.perf
    def test_latency_budget():
        results, breaches = run_harness()
        print_table(results, breaches)
        assert not breaches, f"latency budget breached: {breaches}"


if __name__ == "__main__":
    sys.exit(main())
