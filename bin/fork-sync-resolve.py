#!/usr/bin/env python3
"""Resolve the upstream-merge conflicts that .github/fork-ci-policy.yml calls mechanical.

The fork's CI overrides are generated, not hand-maintained, so under `.github/` upstream
always wins the raw merge and bin/fork-ci-normalize.py re-applies the policy on top.
Files the fork owns outright win as-is. Everything else is a genuine conflict between an
upstream change and a fork change, needs judgement, and is left staged as-is for a human.

Exits 1 when conflicts remain, so the sync can open a draft PR instead of merging.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / ".github" / "fork-ci-policy.yml"

# git status codes where one side deleted the file, so "take that side" means `git rm`.
DELETED_BY_US = "DU"
DELETED_BY_THEM = "UD"
DELETED_BY_BOTH = "DD"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True).stdout


def conflicted_paths() -> list[tuple[str, str]]:
    entries = []
    for line in git("status", "--porcelain=v1", "-z").split("\0"):
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:]
        if code in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
            entries.append((code, path))
    return entries


def matches(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/") and path.startswith(pattern):
            return True
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def take(side: str, code: str, path: str) -> None:
    gone = (side == "ours" and code in {DELETED_BY_US, DELETED_BY_BOTH}) or (
        side == "theirs" and code in {DELETED_BY_THEM, DELETED_BY_BOTH}
    )
    if gone:
        git("rm", "-f", "--quiet", "--", path)
        return
    git("checkout", f"--{side}", "--", path)
    git("add", "--", path)


def main() -> int:
    policy = yaml.safe_load(POLICY_PATH.read_text())
    resolution = policy.get("conflict_resolution") or {}
    ours = resolution.get("ours") or []
    theirs = resolution.get("theirs") or []

    unresolved: list[str] = []
    for code, path in conflicted_paths():
        if matches(path, ours):
            take("ours", code, path)
            print(f"kept fork version: {path}")
        elif matches(path, theirs):
            take("theirs", code, path)
            print(f"took upstream, will renormalize: {path}")
        else:
            unresolved.append(path)

    if unresolved:
        print("\nNeeds manual resolution:", file=sys.stderr)
        for path in unresolved:
            print(f"  {path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
