"""测试环境兼容：Windows 拒绝遍历 pytest 临时目录符号链接时，收尾清理不应让整次测试失败.

部分 Windows 11 版本 (实测 10.0.26220) 把用户创建的符号链接视为"不受信任的装入点"，解析时报
``WinError 448``。pytest 每个 ``tmp_path`` 都会在临时根目录留下 ``*current`` 链接，并在会话结束 / 退出时
解析它们以清理失效链接；该错误会让全部通过的测试以退出码 1 结束，进而挡住 ``check_ci.py`` 与提交钩子。

这里只放过 448 这一种错误 (跳过对应链接，不删除任何东西)，其他错误照常抛出；不影响任何测试结果。
"""

from __future__ import annotations

from pathlib import Path

import _pytest.pathlib
import _pytest.tmpdir

ERROR_UNTRUSTED_MOUNT_POINT = 448
_original_cleanup = _pytest.pathlib.cleanup_dead_symlinks


def _cleanup_dead_symlinks(root: Path) -> None:
    for left_dir in root.iterdir():
        if not left_dir.is_symlink():
            continue
        try:
            dead = not left_dir.resolve().exists()
        except OSError as exc:
            if getattr(exc, "winerror", None) == ERROR_UNTRUSTED_MOUNT_POINT:
                continue
            raise
        if dead:
            left_dir.unlink()


if _original_cleanup is not None:
    # tmpdir 按名字导入了该函数，pathlib 在退出时的编号目录清理里按模块属性调用；两处都要替换
    _pytest.pathlib.cleanup_dead_symlinks = _cleanup_dead_symlinks
    _pytest.tmpdir.cleanup_dead_symlinks = _cleanup_dead_symlinks
