"""Shared utility helpers for repository scanning."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

_FC_SCAN_DIRS_DEFAULT = "src,app,lib,internal,pkg,cmd"
_NESTED_SKIP_DIRS = {
    "__tests__",
    "tests",
    "test",
    "testdata",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "__pycache__",
    "specs",
}


def _get_scan_roots(repo_root: Path) -> list[Path]:
    """Resolve allowed scan roots from FC_SCAN_DIRS under repo_root."""
    scan_dirs_env = os.getenv("FC_SCAN_DIRS", _FC_SCAN_DIRS_DEFAULT)
    scan_dirs = {d.strip() for d in scan_dirs_env.split(",") if d.strip()}
    return [repo_root / d for d in scan_dirs if (repo_root / d).is_dir()]


def _is_in_scan_roots(rel_path: str, scan_roots: list[Path], repo_root: Path) -> bool:
    """Return True when rel_path is inside one of configured scan roots.

    If no roots exist in the repository, allow all paths (backward-compatible).
    """
    if not scan_roots:
        return True
    full_path = (repo_root / rel_path).resolve()
    for root in scan_roots:
        try:
            full_path.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _has_nested_skip_dir(rel_path: str) -> bool:
    """Return True if any path segment is in the nested skip set."""
    return any(part in _NESTED_SKIP_DIRS for part in PurePosixPath(rel_path).parts)
