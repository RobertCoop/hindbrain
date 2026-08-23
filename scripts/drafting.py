"""Draft generation for candidate nudges (spec 8.9).

Shared by prompt_gate.py and observer.py; import as `import drafting`
(the scripts/ dir is on sys.path when any hook script runs).
"""
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import signatures

SYNONYMS = {
    "test": ["tests", "pytest"],
    "tests": ["test", "pytest"],
    "pytest": ["test", "tests"],
    "db": ["database", "sql"],
    "database": ["db", "sql"],
    "sql": ["db", "database"],
    "migration": ["alembic", "schema"],
    "docker": ["container", "image"],
    "git": ["vcs", "branch"],
    "build": ["compile", "make"],
    "lint": ["linter", "format"],
    "deploy": ["release", "ship"],
    "env": ["environment", "config"],
    "npm": ["node", "package"],
}

_PREF = re.compile(
    r"(?i)\b(prefer|please|always|never|don'?t|do not|avoid|use|want|like|instead)\b")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]{2,}")


def _tags(seed_words):
    tags = []
    for w in seed_words:
        w = (w or "").lower()
        if w and w not in tags:
            tags.append(w)
        for s in SYNONYMS.get(w, []):
            if s not in tags:
                tags.append(s)
    return tags[:8]


def _cmd(kind, scope, tags, body):
    parts = ["mem", "save", "--kind", kind, "--scope", shlex.quote(scope)]
    if tags:
        parts += ["--tags", shlex.quote(",".join(tags))]
    parts.append(shlex.quote(body))
    return " ".join(parts)


def draft_gotcha(fail, ok):
    fail = " ".join(str(fail or "").split())[:300]
    ok = " ".join(str(ok or "").split())[:300]
    subs = signatures.subcommands(ok or fail)
    head = signatures.head_str(subs[0]) if subs else "bash"
    fail_toks = set(fail.split())
    delta = " ".join(t for t in ok.split() if t not in fail_toks)[:120]
    delta = delta or "a different invocation"
    body = f"{head} here needs {delta}; `{fail}` fails, `{ok}` works."
    scope = f"command:{head}"
    tags = _tags(head.split("."))
    return {"kind": "gotcha", "scope": scope, "body": body,
            "cmd": _cmd("gotcha", scope, tags, body)}


def draft_remember(text):
    body = " ".join(str(text or "").split())[:500]
    kind = "preference" if _PREF.search(body) else "fact"
    words = [w.lower() for w in _WORD.findall(body)]
    tags = _tags([w for w in words if w in SYNONYMS][:4])
    return {"kind": kind, "scope": "project", "body": body,
            "cmd": _cmd(kind, "project", tags, body)}
