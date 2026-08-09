#!/usr/bin/env python3
"""Reject migration operations that can make an immediate rollback unsafe."""

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE),
    re.compile(r"\bRENAME\s+(?:TABLE|COLUMN)\b", re.IGNORECASE),
)


def main() -> int:
    failures = []
    for relative_path in ("main.py", "control_plane.py"):
        source = (ROOT / relative_path).read_text()
        for pattern in FORBIDDEN:
            match = pattern.search(source)
            if match:
                line = source.count("\n", 0, match.start()) + 1
                failures.append(f"{relative_path}:{line}: {match.group(0)}")
    if failures:
        print("Rollback-incompatible database migration detected:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(
            "Use an expand/migrate/contract release sequence and a reviewed maintenance plan.",
            file=sys.stderr,
        )
        return 1
    print("Database migrations are rollback-compatible (no drop or rename operations).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
