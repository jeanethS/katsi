"""Shared include/exclude glob filtering for ingest entry points."""

from __future__ import annotations

import fnmatch
from pathlib import Path


def matches_any(path_str: str, patterns: list[str]) -> bool:
    """Match a path against globs, trying the full path and then the basename."""
    p = path_str.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(p, pat):
            return True
        base = p.rsplit("/", 1)[-1]
        if fnmatch.fnmatch(base, pat):
            return True
    return False


def walk_files(root: Path, include: list[str], exclude: list[str]) -> list[Path]:
    """Return files under root matching include globs and NOT matching exclude globs."""
    out: list[Path] = []
    if not root.exists():
        return out
    if root.is_file():
        rp = str(root)
        if matches_any(rp, include) and not matches_any(rp, exclude):
            out.append(root)
        return out
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        rp = str(p)
        if matches_any(rp, include) and not matches_any(rp, exclude):
            out.append(p)
    return out
