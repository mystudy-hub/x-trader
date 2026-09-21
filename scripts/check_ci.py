"""Run architecture, unit, smoke, documentation and Python syntax/name checks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = (
    ("Architecture tests", ("-m", "pytest", "tests/architecture", "-q")),
    ("Unit tests", ("-m", "pytest", "tests/unit", "-q")),
    ("Smoke check", ("scripts/smoke.py",)),
    ("Documentation validation", ("scripts/check_docs.py", "--check")),
    (
        "Python syntax and names",
        ("-m", "ruff", "check", "--select", "E9,F63,F7,F82,F401,F841", "qh_trader", "scripts", "tests"),
    ),
)


def main() -> int:
    failures = []
    for name, arguments in CHECKS:
        print(f"\n=== {name} ===", flush=True)
        result = subprocess.run([sys.executable, *arguments], cwd=ROOT, check=False)
        if result.returncode != 0:
            failures.append((name, result.returncode))

    if failures:
        for name, returncode in failures:
            print(f"FAIL: {name} (exit {returncode})", file=sys.stderr, flush=True)
        return 1

    print("\nPASS: All CI checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
