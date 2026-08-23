"""Secret detection (spec 7.8). scan() -> reason | None; redact() for journal mode."""
import math
import re

_ENTROPY_THRESHOLD = 4.2
_ENTROPY_MIN_LEN = 24

_PATTERNS = [
    ("private key material", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("Slack token", re.compile(r"\bxox[bap]-[A-Za-z0-9\-]*")),
    ("API secret key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-\.]*")),
]

_ASSIGN = re.compile(
    r"(?i)\b(?P<key>password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key)"
    r"(?P<sep>\s*[:=]\s*)(?P<val>\S+)")

_PLACEHOLDER = re.compile(
    r"(?i)^(\$|\{|<|\*+$|x{3,}$|\.{3}|your|example|placeholder|change[_-]?me|"
    r"dummy|redacted|«|none$|null$|true$|false$)")

_ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{%d,}" % _ENTROPY_MIN_LEN)


def _entropy(s):
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _is_placeholder(val):
    v = val.strip("\"'`,;)")
    return len(v) < 4 or _PLACEHOLDER.match(v) is not None


def scan(text):
    if not text or not isinstance(text, str):
        return None
    for reason, pat in _PATTERNS:
        if pat.search(text):
            return reason
    for m in _ASSIGN.finditer(text):
        if not _is_placeholder(m.group("val")):
            return f"credential assignment ({m.group('key').lower()}=...)"
    for m in _ENTROPY_TOKEN.finditer(text):
        if _entropy(m.group(0)) > _ENTROPY_THRESHOLD:
            return "high-entropy token (possible secret)"
    return None


def redact(text):
    if not text or not isinstance(text, str):
        return text
    out = text
    for _, pat in _PATTERNS:
        out = pat.sub("«redacted»", out)

    def _assign_sub(m):
        if _is_placeholder(m.group("val")):
            return m.group(0)
        return m.group("key") + m.group("sep") + "«redacted»"

    out = _ASSIGN.sub(_assign_sub, out)
    out = _ENTROPY_TOKEN.sub(
        lambda m: "«redacted»" if _entropy(m.group(0)) > _ENTROPY_THRESHOLD else m.group(0),
        out)
    return out
