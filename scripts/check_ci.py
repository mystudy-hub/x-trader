"""Run architecture, unit, smoke, documentation and Python/JavaScript checks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_CHECKS = (
    ("Architecture tests", ("-m", "pytest", "tests/architecture", "-q")),
    ("Unit tests", ("-m", "pytest", "tests/unit", "-q")),
    ("Smoke check", ("scripts/smoke.py",)),
    ("Documentation validation", ("scripts/check_docs.py", "--check")),
    (
        "Python syntax and names",
        ("-m", "ruff", "check", "--select", "E9,F63,F7,F82,F401,F841", "qh_trader", "scripts", "tests"),
    ),
)
CHECKS = tuple((name, (sys.executable, *arguments)) for name, arguments in PYTHON_CHECKS) + (
    ("Chart drawing tests", ("node", "--test", "tests/unit/test_chart_drawings.cjs")),
    ("Web app syntax", ("node", "--check", "web/app.js")),
)


def main() -> int:
    failures = []
    for name, command in CHECKS:
        print(f"\n=== {name} ===", flush=True)
        try:
            result = subprocess.run(list(command), cwd=ROOT, check=False)
        except OSError as exc:
            print(f"Could not start {name}: {exc}", file=sys.stderr, flush=True)
            failures.append((name, 127))
            continue
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
