"""Install the versioned pre-commit check in this repository."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def configured_hooks(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], cwd=root,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip() if result.returncode == 0 else None


def hooks_are_installed(root: Path) -> bool:
    configured = configured_hooks(root)
    return configured is not None and (root / Path(configured).expanduser()).resolve() == root / ".githooks"


def install_hooks(root: Path):
    root = root.resolve()
    hook = root / ".githooks/pre-commit"
    if not hook.is_file() or not (root / "scripts/pre_commit.py").is_file():
        raise RuntimeError("Missing versioned hook or staged-check script")
    if b"\r" in hook.read_bytes():
        raise RuntimeError("The shell hook must use LF line endings")
    current = configured_hooks(root)
    if current is not None and not hooks_are_installed(root):
        raise RuntimeError(f"Existing core.hooksPath is preserved: {current}")
    if current is None:
        hook_dir = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"], cwd=root, check=True,
            capture_output=True, text=True, encoding="utf-8",
        ).stdout.strip()
        existing = [
            path.name for path in (root / hook_dir).glob("*")
            if path.is_file() and not path.name.endswith(".sample")
        ]
        if existing:
            raise RuntimeError("Existing Git hooks are preserved: " + ", ".join(existing))
    if os.name != "nt":
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    subprocess.run(["git", "config", "--local", "core.hooksPath", ".githooks"], cwd=root, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check activation without changing Git configuration")
    args = parser.parse_args()
    try:
        if args.check:
            if not hooks_are_installed(ROOT):
                raise RuntimeError("Run python scripts/install_hooks.py to enable the local commit check")
        else:
            install_hooks(ROOT)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("PASS: local pre-commit check is enabled (.githooks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
