#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Reject mutable external GitHub Actions references in workflow files."""
from __future__ import annotations

import re
import sys
from pathlib import Path

USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)(?:\s+#\s*(.*))?\s*$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def pin_errors(text: str, path: str = "workflow.yml") -> list[str]:
    errors = []
    for number, line in enumerate(text.splitlines(), 1):
        match = USES.match(line)
        if not match:
            continue
        action, comment = match.groups()
        if action.startswith("./"):
            continue
        if "@" not in action:
            errors.append(f"{path}:{number}: external action has no immutable ref")
            continue
        ref = action.rsplit("@", 1)[1]
        if not SHA.fullmatch(ref):
            errors.append(f"{path}:{number}: external action ref must be a full 40-character SHA")
        if not comment or not re.match(r"v\S+", comment):
            errors.append(f"{path}:{number}: immutable action pin needs a readable v-tag comment")
    return errors


def tree_errors(root: Path) -> list[str]:
    workflows = sorted((root / ".github/workflows").glob("*.yml"))
    return [error for path in workflows for error in pin_errors(path.read_text(encoding="utf-8"), str(path.relative_to(root)))]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = tree_errors(root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Checked all external action refs in {len(list((root / '.github/workflows').glob('*.yml')))} workflow files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
