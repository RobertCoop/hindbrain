"""Deterministic bootstrap survey (mem scout): mines an existing project for
memory-candidate evidence. Read-only; emits structured findings, never saves.
The judgment half (filter/verify/save) belongs to the agent — see the mem-init
skill."""
import json
import os
import re
import subprocess

MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 5000
MAX_PER_FILE = 5
MAX_PER_SOURCE = 80

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
             "build", "vendor", "target", ".tox", ".nox", ".mypy_cache",
             ".pytest_cache", ".next", ".cache", "coverage", "htmlcov"}
TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java",
            ".c", ".h", ".cpp", ".hpp", ".cs", ".sh", ".bash", ".zsh", ".pl",
            ".php", ".swift", ".kt", ".scala", ".sql", ".tf", ".yaml", ".yml",
            ".toml", ".ini", ".cfg", ".md", ".txt", ".mk", ".cmake", ".ex",
            ".exs", ".erl", ".lua", ".r", ".jl", ".vue", ".svelte"}
TEXT_NAMES = {"Makefile", "makefile", "GNUmakefile", "Dockerfile", "Justfile",
              "justfile", "Rakefile", "Procfile"}

CONFESSION = re.compile(
    r"(?i)\b(hack|workaround|xxx|fixme|footgun|gotcha|careful|do not|don'?t|"
    r"must not|must be|be sure to|beware|caution|important:|note:|warning:|"
    r"only works|will (?:break|fail)|breaks? (?:if|when|on)|fails? (?:if|when|on))\b")
COMMENT_LINE = re.compile(r"^\s*(#|//|/\*|\*|--|<!--|;|%|\"\"\"|''')")

SUSPICIOUS_COMMIT = re.compile(
    r"(?i)\b(revert|workaround|hotfix|again|actually|really fix|properly|"
    r"for real|finally)\b")


def _walk_files(root):
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            ext = os.path.splitext(name)[1].lower()
            if ext not in TEXT_EXT and name not in TEXT_NAMES:
                continue
            p = os.path.join(dirpath, name)
            try:
                if os.path.getsize(p) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            n += 1
            if n > MAX_FILES:
                return
            yield p


def _read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def confession_comments(root):
    out = []
    for path in _walk_files(root):
        rel = os.path.relpath(path, root)
        per_file = 0
        for i, line in enumerate(_read_lines(path), 1):
            s = line.strip()
            if len(s) < 12 or len(s) > 400:
                continue
            is_comment = bool(COMMENT_LINE.match(s))
            if not is_comment and not rel.lower().endswith((".md", ".txt")):
                continue
            if CONFESSION.search(s):
                out.append({"file": rel, "line": i, "text": s[:300]})
                per_file += 1
                if per_file >= MAX_PER_FILE:
                    break
        if len(out) >= MAX_PER_SOURCE:
            break
    return out[:MAX_PER_SOURCE]


def _task_runners_one(root):
    out = []
    for name in ("Makefile", "makefile", "GNUmakefile", "Justfile", "justfile"):
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            continue
        target = None
        for line in _read_lines(p)[:800]:
            m = re.match(r"^([A-Za-z0-9_.-]+):(?!=)", line)
            if m and not line.startswith("\t"):
                target = m.group(1)
            elif target and line.startswith(("\t", "    ")):
                recipe = line.strip()
                # recipes carrying env vars or unusual flags are gotcha-shaped
                if re.search(r"\b[A-Z][A-Z0-9_]{2,}=|--\w[\w-]{3,}", recipe):
                    out.append({"file": name, "target": target,
                                "recipe": recipe[:300]})
    pkg = os.path.join(root, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg, encoding="utf-8") as f:
                scripts = (json.load(f).get("scripts") or {})
            for k, v in sorted(scripts.items()):
                if isinstance(v, str) and re.search(
                        r"\b[A-Z][A-Z0-9_]{2,}=|--\w[\w-]{3,}|&&", v):
                    out.append({"file": "package.json", "target": k,
                                "recipe": v[:300]})
        except (ValueError, OSError):
            pass
    for name in ("tox.ini", "noxfile.py", "pytest.ini", "setup.cfg",
                 "pyproject.toml"):
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            continue
        for i, line in enumerate(_read_lines(p)[:400], 1):
            if re.search(r"(?i)^\s*(addopts|setenv|passenv|env(?:_list)?|"
                         r"pythonpath|testpaths|markers)\s*[:=]", line):
                out.append({"file": name, "target": f"line {i}",
                            "recipe": line.strip()[:300]})
    return out[:MAX_PER_SOURCE]


def _ci_truth_one(root):
    out = []
    for wf_dir in (os.path.join(root, ".github", "workflows"),
                   os.path.join(root, ".gitlab-ci.d")):
        if not os.path.isdir(wf_dir):
            continue
        for name in sorted(os.listdir(wf_dir))[:20]:
            if not name.endswith((".yml", ".yaml")):
                continue
            rel = os.path.relpath(os.path.join(wf_dir, name), root)
            in_env = False
            for i, line in enumerate(_read_lines(os.path.join(wf_dir, name)), 1):
                s = line.rstrip()
                if re.match(r"^\s*env\s*:", s):
                    in_env = True
                    continue
                if in_env:
                    m = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*)\s*:", s)
                    if m:
                        out.append({"file": rel, "line": i,
                                    "kind": "env", "text": s.strip()[:200]})
                        continue
                    in_env = False
                m = re.match(r"^\s*(?:-\s+)?run\s*:\s*(.+)", s)
                if m and re.search(r"\b[A-Z][A-Z0-9_]{2,}=|--\w[\w-]{3,}",
                                   m.group(1)):
                    out.append({"file": rel, "line": i, "kind": "run",
                                "text": m.group(1).strip()[:300]})
    ci = os.path.join(root, ".gitlab-ci.yml")
    if os.path.isfile(ci):
        for i, line in enumerate(_read_lines(ci)[:400], 1):
            if re.search(r"\b[A-Z][A-Z0-9_]{2,}=", line):
                out.append({"file": ".gitlab-ci.yml", "line": i, "kind": "run",
                            "text": line.strip()[:300]})
    return out[:MAX_PER_SOURCE]


def _git(root, *args):
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True,
                           text=True, timeout=15)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _nested_repos(root):
    # the workspace itself, or (multi-repo workspace) its immediate child repos
    if os.path.isdir(os.path.join(root, ".git")):
        return [("", root)]
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    for n in names:
        if n in SKIP_DIRS or n.startswith("."):
            continue
        p = os.path.join(root, n)
        if os.path.isdir(os.path.join(p, ".git")):
            out.append((n, p))
            if len(out) >= 10:
                break
    return out


def git_history(root):
    suspicious, counts = [], {}
    for label, path in _nested_repos(root):
        log = _git(path, "log", "--oneline", "-200")
        if not log:
            continue
        pre = f"{label}: " if label else ""
        suspicious.extend(pre + l[:200] for l in log.splitlines()
                          if SUSPICIOUS_COMMIT.search(l))
        names = _git(path, "log", "--name-only", "--pretty=format:", "-200")
        fpre = f"{label}/" if label else ""
        for l in names.splitlines():
            l = l.strip()
            if l:
                counts[fpre + l] = counts.get(fpre + l, 0) + 1
    churn = sorted(counts.items(), key=lambda kv: -kv[1])[:15]
    return {"suspicious_commits": suspicious[:30],
            "churn_files": [{"file": f, "changes": c} for f, c in churn
                            if c >= 3]}


def _env_shape_one(root):
    out = []
    for name in (".env.example", ".env.sample", ".env.template"):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            keys = [l.split("=", 1)[0].strip() for l in _read_lines(p)
                    if "=" in l and not l.lstrip().startswith("#")]
            if keys:
                out.append({"file": name, "kind": "env_keys",
                            "text": ", ".join(keys[:40])[:400]})
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml",
                 "compose.yaml"):
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            continue
        for i, line in enumerate(_read_lines(p)[:400], 1):
            if re.search(r'^\s*-?\s*"?\d{2,5}:\d{2,5}"?\s*$|^\s{2}[a-z][\w-]*:\s*$',
                         line):
                out.append({"file": name, "line": i, "kind": "compose",
                            "text": line.strip()[:200]})
    for name in (".python-version", ".nvmrc", ".ruby-version",
                 "rust-toolchain", "rust-toolchain.toml", ".tool-versions"):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            v = " ".join(_read_lines(p)[:3]).strip()[:100]
            if v:
                out.append({"file": name, "kind": "version_pin", "text": v})
    dockerfile = os.path.join(root, "Dockerfile")
    if os.path.isfile(dockerfile):
        for line in _read_lines(dockerfile)[:100]:
            if line.strip().upper().startswith("FROM "):
                out.append({"file": "Dockerfile", "kind": "base_image",
                            "text": line.strip()[:200]})
    return out[:MAX_PER_SOURCE]


def _scan_roots(root):
    # the workspace root itself, plus (multi-repo workspace) its child repos —
    # each repo carries its own Makefile/CI/env shape that a root-only scan
    # would miss entirely
    roots = [("", root)]
    if not os.path.isdir(os.path.join(root, ".git")):
        roots.extend(_nested_repos(root))
    return roots


def _aggregate(root, scan_one):
    out = []
    for label, path in _scan_roots(root):
        for item in scan_one(path):
            if label:
                item["file"] = f"{label}/{item['file']}"
            out.append(item)
            if len(out) >= MAX_PER_SOURCE:
                return out
    return out


def task_runners(root):
    return _aggregate(root, _task_runners_one)


def ci_truth(root):
    return _aggregate(root, _ci_truth_one)


def env_shape(root):
    return _aggregate(root, _env_shape_one)


def run_scout(root):
    return {
        "project": root,
        "confession_comments": confession_comments(root),
        "task_runners": task_runners(root),
        "ci": ci_truth(root),
        "git": git_history(root),
        "env": env_shape(root),
    }


def format_text(report):
    # compact summary, <= 30 lines (full detail via --json)
    lines = [f"scout: {report['project']}"]

    def section(title, items, fmt, top):
        if not items:
            return
        lines.append(f"{title} ({len(items)}):")
        for it in items[:top]:
            lines.append("  " + fmt(it))
        if len(items) > top:
            lines.append(f"  … {len(items) - top} more (--json)")

    section("confession comments", report["confession_comments"],
            lambda c: f"{c['file']}:{c['line']}  {c['text'][:90]}", 6)
    section("task-runner/env-var recipes", report["task_runners"],
            lambda t: f"{t['file']} [{t['target']}]  {t['recipe'][:80]}", 5)
    section("CI truth", report["ci"],
            lambda c: f"{c['file']}:{c.get('line', '?')}  {c['text'][:85]}", 4)
    g = report["git"]
    section("suspicious commits", g["suspicious_commits"],
            lambda s: s[:100], 4)
    if g["churn_files"]:
        top = ", ".join(f"{c['file']}({c['changes']})"
                        for c in g["churn_files"][:5])
        lines.append(f"churn files: {top}")
    section("environment shape", report["env"],
            lambda e: f"{e['file']}  {e['text'][:85]}", 4)
    if len(lines) == 1:
        lines.append("nothing notable found")
    lines.append("full detail: mem scout --json")
    return lines[:30]
