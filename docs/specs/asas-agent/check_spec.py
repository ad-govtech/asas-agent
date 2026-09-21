#!/usr/bin/env python3
"""Mechanical checks on the specification. Run it; do not read for these.

Every check here exists because reading missed the thing it catches, at least
once, in this repository. The first version of these checks was three shell
one-liners, and each of the three could pass for the wrong reason: `sort -u`
compares membership so a duplicated row is invisible, the contiguity command
printed ids without asserting anything about them, and the evidence check
grepped for `def <name>`, which matched a commented-out definition.

The first Python version still counted references rather than declarations, so a
duplicated requirement passed; that is why check 1 now matches the bold heading
and the table cell rather than every id in the file.

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
DECLARATION = re.compile(r"^\*\*(R-[A-Z]+-\d+)\*\*", re.MULTILINE)

# Named in prose as deliberately absent; see the findings they belong to.
DELETED = {"test_two_sub_agents_cannot_share_a_tool_name"}


def ids(text: str) -> list[str]:
    return [m.group(0) for m in ID.finditer(text)]


def defined_test_names() -> set[str]:
    """Test definitions at module or class level, read from the AST.

    Not `ast.walk`, which would also find a function defined inside another
    function — pytest does not collect those, and neither should a check that
    exists to catch a name that looks like a test and is not one.
    """
    names: set[str] = set()
    for path in TESTS.rglob("test_*.py"):
        names.add(path.stem)  # a file may be cited as evidence too
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bodies = [tree.body]
        bodies += [node.body for node in tree.body if isinstance(node, ast.ClassDef)]
        for body in bodies:
            for node in body:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
                    names.add(node.name)
    return names


def main() -> int:
    spec = (SPECS / "spec.md").read_text(encoding="utf-8")
    verification = (SPECS / "verification.md").read_text(encoding="utf-8")
    failures: list[str] = []

    # 1. Declared once, reported once. Counted on both sides, not compared as sets:
    #    two declarations of one id, or two status rows for it, are both real ways to
    #    end up reporting on the wrong requirement.
    #    A declaration is a bold id opening a line; a status row is an id opening a
    #    table cell. Ids anywhere else — the invariants table, a finding, a rationale —
    #    are references and are not counted on either side.
    declared = Counter(m.group(1) for m in DECLARATION.finditer(spec))
    reported = Counter(ROW.match(line).group(1) for line in verification.splitlines() if ROW.match(line))
    for rid, n in sorted(declared.items()):
        if n != 1:
            failures.append(f"{rid} is declared {n} times in spec.md, expected exactly once")
    for rid, n in sorted(reported.items()):
        if n != 1:
            failures.append(f"{rid} has {n} status rows in verification.md, expected exactly one")
    for rid in sorted(set(ids(spec)) - set(declared)):
        failures.append(f"{rid} is cited in spec.md and declared nowhere in it")
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

    # 3. Every test named as evidence is a real definition, not a comment.
    known = defined_test_names()
    for name in sorted(set(TEST_NAME.findall(verification))):
        if name not in known and name not in DELETED:
            failures.append(f"{name} is named as evidence and is not a test definition")

    if failures:
        print(f"{len(failures)} failure(s):", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1

    total = len(set(declared))
    prefixes = ", ".join(f"R-{p}-1..{len(n)}" for p, n in sorted(by_prefix.items()))
    print(f"{total} requirements, one status row each, contiguous: {prefixes}")
    print(f"{len(set(TEST_NAME.findall(verification)) - DELETED)} test names cited, all defined")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
