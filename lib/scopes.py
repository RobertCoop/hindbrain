"""Scope matching (spec 7.7): 'exact' | 'project' | None."""
import fnmatch
import os.path
import re

from lib import signatures


def _exact(mem, ctx):
    st = mem.get("scope_type") or ""
    sv = mem.get("scope_value") or ""
    if st == "command":
        cmd = getattr(ctx, "command", "")
        if not cmd or not sv:
            return False
        heads = {signatures.head_str(t) for t in signatures.subcommands(cmd)}
        return any(e.strip() in heads for e in sv.split("|") if e.strip())
    if st == "path":
        fp = getattr(ctx, "file_path", "")
        if not fp or not sv:
            return False
        candidates = [fp]
        project = getattr(ctx, "project", "")
        if project:
            try:
                candidates.append(os.path.relpath(fp, project))
            except ValueError:
                pass
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
