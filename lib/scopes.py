"""Scope matching (spec 7.7): 'exact' | 'project' | None."""
import fnmatch
import os.path
import re

from lib import signatures

# gates test one command/path against ~1000 scoped rows; memoize the
# per-context derivations (shlex split, relpath) instead of redoing them per row
_heads_cache = {}
_path_cache = {}


def _command_heads(cmd):
    heads = _heads_cache.get(cmd)
    if heads is None:
        if len(_heads_cache) > 64:
            _heads_cache.clear()
        heads = frozenset(
            signatures.head_str(t) for t in signatures.subcommands(cmd))
        _heads_cache[cmd] = heads
    return heads


def _path_candidates(fp, project):
    key = (fp, project)
    cands = _path_cache.get(key)
    if cands is None:
        if len(_path_cache) > 64:
            _path_cache.clear()
        cands = [fp]
        if project:
            try:
                cands.append(os.path.relpath(fp, project))
            except ValueError:
                pass
        _path_cache[key] = cands
    return cands


def _exact(mem, ctx):
    st = mem.get("scope_type") or ""
    sv = mem.get("scope_value") or ""
    if st == "command":
        cmd = getattr(ctx, "command", "")
        if not cmd or not sv:
            return False
        heads = _command_heads(cmd)
        return any(e.strip() in heads for e in sv.split("|") if e.strip())
    if st == "path":
        fp = getattr(ctx, "file_path", "")
        if not fp or not sv:
            return False
        candidates = _path_candidates(fp, getattr(ctx, "project", ""))
        return any(fnmatch.fnmatch(c, sv) for c in candidates)
    if st == "tool":
        tool = getattr(ctx, "tool_name", "")
        if not tool or not sv:
            return False
        try:
            return re.fullmatch(sv, tool) is not None
        except re.error:
            return False
    return False


def match(mem, ctx):
    if not isinstance(mem, dict):
        return None
    if _exact(mem, ctx):
        return "exact"
    # global project '' matches every project, at project level only
    if mem.get("project", "") in ("", getattr(ctx, "project", "")):
        return "project"
    return None
