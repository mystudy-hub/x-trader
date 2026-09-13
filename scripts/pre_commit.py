"""Run the project checks against a snapshot of Git's index before committing."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *arguments: str, env=None) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, env=env, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout


def check_staged_snapshot(root: Path) -> int:
    root = root.resolve()
    local_git_variables = git(root, "rev-parse", "--local-env-vars").splitlines()
    clean_env = {key: value for key, value in os.environ.items() if key not in local_git_variables}
    with TemporaryDirectory(prefix="qh-trader-staged-") as temporary:
        snapshot = Path(temporary).resolve()
        git(root, "checkout-index", "--all", f"--prefix={snapshot.as_posix()}/")
        # 使用快照中的 .gitignore 检查被强制暂存的运行文件和凭证路径。
        git(snapshot, "init", "--quiet", env=clean_env)
        ignored = git(snapshot, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", env=clean_env)
        ignored_paths = [path for path in ignored.split("\0") if path]
        if ignored_paths:
            print("FAIL: staged files match ignore rules:", file=sys.stderr)
            for path in ignored_paths:
                print(f"  {path}", file=sys.stderr)
            return 1
        check_script = snapshot / "scripts/check_ci.py"
        if not check_script.is_file():
            print("FAIL: stage scripts/check_ci.py before committing.", file=sys.stderr)
            return 1
        # Git 在 hook 中导出的仓库变量不得污染子测试创建的独立仓库。
        clean_env["PYTHONPATH"] = str(snapshot)
        print("Checking staged snapshot (working files are preserved)...", flush=True)
        result = subprocess.run(
            [sys.executable, str(check_script)], cwd=snapshot, env=clean_env, check=False,
        )
        return result.returncode


def main() -> int:
    try:
        return check_staged_snapshot(ROOT)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"FAIL: could not validate staged snapshot: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
