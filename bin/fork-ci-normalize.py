#!/usr/bin/env python3
"""Apply this fork's CI policy to .github workflows and composite actions.

Upstream targets runner fleets this fork does not have, and every sync brings more
references to them. Keeping the overrides generated rather than hand-maintained means an
upstream merge can resolve .github/ conflicts in upstream's favour and regenerate the
fork's changes on top. See .github/fork-ci-policy.yml.

    bin/fork-ci-normalize.py            rewrite in place
    bin/fork-ci-normalize.py --check    exit 1 if a rewrite is pending
    bin/fork-ci-normalize.py --invariants-only
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / ".github" / "fork-ci-policy.yml"

# A workflow's top-level keys sit at column 0, so an `on:` block runs until the next
# unindented key. Its triggers sit one indent level in.
ON_KEY = re.compile(r"^(['\"]?on['\"]?):\s*(.*)$")
TOP_LEVEL_KEY = re.compile(r"^\S")
TRIGGER_KEY = re.compile(r"^(\s+)([A-Za-z_][\w-]*):")


def load_policy() -> dict[str, Any]:
    return yaml.safe_load(POLICY_PATH.read_text())


def target_files() -> list[Path]:
    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
    actions = sorted((REPO_ROOT / ".github" / "actions").glob("*/action.y*ml"))
    return workflows + actions


def rewrite_runners(text: str, rules: list[dict[str, str]]) -> str:
    for rule in rules:
        text = re.sub(rule["match"], rule["replace"], text)
    return text


def rewrite_actions(text: str, rules: list[dict[str, Any]]) -> str:
    for rule in rules:
        pattern = re.compile(rf"(uses:\s*){re.escape(rule['from'])}@\S+(?:\s+#.*)?$", re.MULTILINE)
        if not pattern.search(text):
            continue
        text = pattern.sub(lambda m: m.group(1) + rule["to"], text)
        drop = rule.get("drop_inputs") or []
        if drop:
            text = drop_step_inputs(text, rule["to"].split("@", 1)[0], drop)
    return text


def drop_step_inputs(text: str, action: str, inputs: list[str]) -> str:
    """Remove `with:` entries that only the replaced action understood.

    Scoped to the `with:` block of the rewritten step so an input name that is valid
    elsewhere (`push:`, `load:`) is never touched in another step.
    """
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        out.append(line)
        index += 1
        if f"uses: {action}@" not in line:
            continue
        step_indent = len(line) - len(line.lstrip())
        # Walk the rest of the step looking for its `with:` block.
        while index < len(lines):
            current = lines[index]
            indent = len(current) - len(current.lstrip())
            if current.strip() and indent < step_indent:
                break
            if re.match(rf"^\s{{{step_indent}}}with:\s*$", current):
                out.append(current)
                index += 1
                with_indent = None
                while index < len(lines):
                    entry = lines[index]
                    if not entry.strip():
                        out.append(entry)
                        index += 1
                        continue
                    entry_indent = len(entry) - len(entry.lstrip())
                    if entry_indent <= step_indent:
                        break
                    if with_indent is None:
                        with_indent = entry_indent
                    key = re.match(rf"^\s{{{with_indent}}}([\w-]+):", entry)
                    if key and key.group(1) in inputs:
                        index += 1
                        # Swallow the input's continuation lines (block scalars, lists).
                        while index < len(lines):
                            follow = lines[index]
                            if follow.strip() and (len(follow) - len(follow.lstrip())) <= with_indent:
                                break
                            index += 1
                        continue
                    out.append(entry)
                    index += 1
                break
            out.append(current)
            index += 1
    return "\n".join(out)


def find_on_block(lines: list[str]) -> tuple[int, int] | None:
    for start, line in enumerate(lines):
        if not ON_KEY.match(line):
            continue
        end = start + 1
        while end < len(lines) and not TOP_LEVEL_KEY.match(lines[end]):
            end += 1
        return start, end
    return None


def restrict_triggers(text: str, keep: list[str]) -> str:
    lines = text.split("\n")
    span = find_on_block(lines)
    if span is None:
        return text
    start, end = span
    header, inline = ON_KEY.match(lines[start]).groups()
    if inline.strip() and not inline.strip().startswith("#"):
        # `on: push` / `on: [push, pull_request]` — a shorthand with nothing to keep.
        return "\n".join(lines[:start] + [f"{header}:", "    workflow_dispatch:"] + lines[end:])

    body = lines[start + 1 : end]
    kept: list[str] = []
    pending: list[str] = []
    index = 0
    while index < len(body):
        line = body[index]
        if not line.strip() or line.lstrip().startswith("#"):
            pending.append(line)
            index += 1
            continue
        match = TRIGGER_KEY.match(line)
        if not match:
            kept.extend(pending)
            pending = []
            kept.append(line)
            index += 1
            continue
        indent, name = match.groups()
        block = [line]
        index += 1
        while index < len(body):
            nxt = body[index]
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= len(indent):
                break
            block.append(nxt)
            index += 1
        if name in keep:
            kept.extend(pending)
            kept.extend(block)
        # Comments introducing a dropped trigger go with it.
        pending = []

    if not any(line.strip() for line in kept):
        kept = ["    workflow_dispatch:"]
    while kept and not kept[-1].strip():
        kept.pop()
    # Keep the blank line that separated `on:` from the next top-level key.
    if end < len(lines) and not lines[end - 1].strip():
        kept.append("")
    return "\n".join(lines[:start] + [f"{header}:"] + kept + lines[end:])


def normalize(path: Path, policy: dict[str, Any]) -> str:
    text = path.read_text()
    text = rewrite_runners(text, policy.get("runner_rewrites") or [])
    text = rewrite_actions(text, policy.get("action_rewrites") or [])
    is_workflow = path.parent.name == "workflows"
    if is_workflow and path.name not in set(policy.get("enabled_workflows") or []):
        text = restrict_triggers(text, policy.get("keep_triggers") or ["workflow_dispatch"])

    # A textual rewrite that lands on an unexpected upstream shape must fail here rather
    # than ship a workflow GitHub silently refuses to load.
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: rewrite produced a non-mapping document")
    if is_workflow and not parsed.get(True) and not parsed.get("on"):
        raise ValueError(f"{path}: rewrite left the workflow with no triggers")
    return text


def check_invariants(policy: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for rule in policy.get("fork_invariants") or []:
        path = REPO_ROOT / rule["path"]
        why = rule.get("why", "fork change")
        if not path.exists():
            failures.append(f"{rule['path']} is missing ({why})")
            continue
        needle = rule.get("contains")
        if needle and needle not in path.read_text():
            failures.append(f"{rule['path']} no longer contains {needle!r} ({why})")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if a rewrite is pending")
    parser.add_argument("--invariants-only", action="store_true", help="only verify fork invariants")
    args = parser.parse_args()

    policy = load_policy()
    changed: list[str] = []

    if not args.invariants_only:
        for path in target_files():
            original = path.read_text()
            updated = normalize(path, policy)
            if updated == original:
                continue
            changed.append(str(path.relative_to(REPO_ROOT)))
            if not args.check:
                path.write_text(updated)

    failures = check_invariants(policy)

    for name in changed:
        print(f"{'pending' if args.check else 'normalized'}: {name}")
    for failure in failures:
        print(f"fork invariant lost: {failure}", file=sys.stderr)

    if failures:
        return 1
    if args.check and changed:
        print(f"\n{len(changed)} file(s) drift from .github/fork-ci-policy.yml. Run bin/fork-ci-normalize.py.", file=sys.stderr)
        return 1
    if not changed and not args.invariants_only:
        print("CI policy already applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
