"""Bash command normalization and failure signatures (spec 7.6)."""
import os
import re
import shlex

MULTITOOLS = {"git", "docker", "npm", "pnpm", "yarn", "cargo", "kubectl",
              "terraform", "helm", "aws", "gcloud", "az", "make", "alembic"}

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _scan_paren(cmd, start):
    # start is just past "$("; returns (index past matching ")", inner text)
    depth, i, n, q = 1, start, len(cmd), None
    while i < n:
        c = cmd[i]
        if q:
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1, cmd[start:i]
        i += 1
    return n, cmd[start:]


def _split_segments(cmd, depth=0):
    segs, buf = [], []
    i, n, q = 0, len(cmd), None
    while i < n:
        c = cmd[i]
        if q == "'":
            buf.append(c)
            if c == "'":
                q = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(cmd[i:i + 2])
            i += 2
            continue
        # $() substitutes inside double quotes and bare text, not single quotes
        if c == "$" and i + 1 < n and cmd[i + 1] == "(":
            j, inner = _scan_paren(cmd, i + 2)
            if depth < 4:
                segs.extend(_split_segments(inner, depth + 1))
            buf.append(" ")
            i = j
            continue
        if q == '"':
            buf.append(c)
            if c == '"':
                q = None
            i += 1
            continue
        if c == '"' or c == "'":
            q = c
            buf.append(c)
            i += 1
            continue
        if c in ";\n" or c == "|" or (c == "&" and i + 1 < n and cmd[i + 1] == "&"):
            segs.append("".join(buf))
            buf = []
            i += 2 if c == "&" else 1
            continue
        buf.append(c)
        i += 1
    segs.append("".join(buf))
    return [s for s in (s.strip() for s in segs) if s]


def subcommands(bash_cmd):
    if not bash_cmd or not isinstance(bash_cmd, str):
        return []
    out = []
    for seg in _split_segments(bash_cmd):
        try:
            toks = shlex.split(seg, posix=True)
        except ValueError:
            toks = seg.split()
        while toks and _ASSIGN.match(toks[0]):
            toks.pop(0)
        if toks:
            out.append(toks)
    return out


def head(cmd_tokens):
    if not cmd_tokens:
        return ()
    first = os.path.basename(cmd_tokens[0])
    if first in MULTITOOLS:
        skip_next = False
        for t in cmd_tokens[1:]:
            if skip_next:
                skip_next = False
                continue
            if t.startswith("--"):
                continue
            if t.startswith("-"):
                # short option: next token is its argument (git -C dir, docker -H host)
                skip_next = len(t) == 2
                continue
            return (first, t)
    return (first,)


def head_str(cmd_tokens):
    return ".".join(head(cmd_tokens))


# Fixed ordered table: first match wins.
_CODE_CLASSES = [
    ("importerror", re.compile(r"\bImportError\b", re.I)),
    ("modulenotfound", re.compile(r"ModuleNotFoundError|No module named", re.I)),
    ("permissionerror", re.compile(
        r"PermissionError|Permission denied|EACCES|EPERM|operation not permitted", re.I)),
    ("exit_code", re.compile(
        r"exit code\s*\d+|exited with\s*\d+|non-zero exit|exit status\s*\d+", re.I)),
    ("timeout", re.compile(r"\btimed?\s*out\b|TimeoutError|ETIMEDOUT", re.I)),
    ("notfound", re.compile(
        r"command not found|not found|No such file or directory|ENOENT|does not exist|\b404\b", re.I)),
    ("syntax", re.compile(r"SyntaxError|syntax error|unexpected token|ParseError", re.I)),
    ("connection", re.compile(
        r"Connection refused|ConnectionError|ECONNREFUSED|ECONNRESET|"
        r"Network is unreachable|Could not resolve host", re.I)),
    ("auth", re.compile(
        r"authenticat|\bunauthorized\b|\b401\b|\b403\b|invalid credentials", re.I)),
]


def _code_class(failure_text):
    text = failure_text if isinstance(failure_text, str) else str(failure_text or "")
    for name, pat in _CODE_CLASSES:
        if pat.search(text):
            return name
    return "unknown"


def failure_signature(tool_name, tool_input, failure_text):
    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    subs = subcommands(cmd)
    hs = head_str(subs[0]) if subs else ""
    return f"{tool_name}:{hs}:{_code_class(failure_text)}"


def similar(sig_a, sig_b):
    # same tool + same head; code class may differ
    a = (sig_a or "").split(":", 2)
    b = (sig_b or "").split(":", 2)
    if len(a) < 2 or len(b) < 2:
        return False
    return a[0] == b[0] and a[1] == b[1]
