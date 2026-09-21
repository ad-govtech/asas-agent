#!/usr/bin/env python3
"""Mechanical checks on the specification. Run it; do not read for these.

Every check here exists because reading missed the thing it catches, at least
once, in this repository. The first version of these checks was three shell
one-liners, and each of the three could pass for the wrong reason: `sort -u`
compares membership so a duplicated row is invisible, the contiguity command
printed ids without asserting anything about them, and the evidence check
grepped for `def <name>`, which matched a commented-out definition.

    python docs/specs/asas-agent/check_spec.py

Exits non-zero, naming every failure, and prints what it checked when clean.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from pathlib import Path

SPECS = Path(__file__).resolve().parent
ROOT = SPECS.parents[2]
TESTS = ROOT / "tests"

ID = re.compile(r"\bR-([A-Z]+)-(\d+)\b")
TEST_NAME = re.compile(r"\btest_[a-z0-9_]+\b")
ROW = re.compile(r"^\|\s*(R-[A-Z]+-\d+)\b")

# Named in prose as deliberately absent; see the findings they belong to.
DELETED = {"test_two_sub_agents_cannot_share_a_tool_name"}


def ids(text: str) -> list[str]:
    return [m.group(0) for m in ID.finditer(text)]


def collected_test_names() -> set[str]:
    """What pytest would collect — read from the AST, so a comment cannot qualify."""
    names: set[str] = set()
    for path in TESTS.rglob("test_*.py"):
        names.add(path.stem)  # a file may be cited as evidence too
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
                names.add(node.name)
    return names


def main() -> int:
    spec = (SPECS / "spec.md").read_text(encoding="utf-8")
    verification = (SPECS / "verification.md").read_text(encoding="utf-8")
    failures: list[str] = []

    # 1. Declared and reported, each exactly once. Counted, not compared as a set:
    #    a requirement with two status rows is a real way to report on the wrong one.
    declared = Counter(ids(spec))
    # A status row opens with its id; ids cited elsewhere in verification.md — in the
    # invariants table, in a finding — are references, not rows, and are not counted.
    reported = Counter(ROW.match(line).group(1) for line in verification.splitlines() if ROW.match(line))
    for rid, n in sorted(reported.items()):
        if n != 1:
            failures.append(f"{rid} has {n} status rows in verification.md, expected exactly one")
    for rid in sorted(set(declared) - set(reported)):
        failures.append(f"{rid} is declared in spec.md and has no status row")
    for rid in sorted(set(reported) - set(declared)):
        failures.append(f"{rid} has a status row and is declared nowhere in spec.md")

    # 2. Contiguous from 1 within each prefix, no gaps, no repeats.
    by_prefix: dict[str, list[int]] = {}
    for rid in set(declared):
        prefix, number = ID.match(rid).groups()
        by_prefix.setdefault(prefix, []).append(int(number))
    for prefix, numbers in sorted(by_prefix.items()):
        expected = list(range(1, len(numbers) + 1))
        if sorted(numbers) != expected:
            failures.append(f"R-{prefix}-* is {sorted(numbers)}, expected {expected}")

    # 3. Every test named as evidence is one pytest would collect.
    known = collected_test_names()
    for name in sorted(set(TEST_NAME.findall(verification))):
        if name not in known and name not in DELETED:
            failures.append(f"{name} is named as evidence and is not a collected test")

    if failures:
        print(f"{len(failures)} failure(s):", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1

    total = len(set(declared))
    prefixes = ", ".join(f"R-{p}-1..{len(n)}" for p, n in sorted(by_prefix.items()))
    print(f"{total} requirements, one status row each, contiguous: {prefixes}")
    print(f"{len(set(TEST_NAME.findall(verification)) - DELETED)} test names cited, all collected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
