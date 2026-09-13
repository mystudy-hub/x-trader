"""QH-Trader 自动化冒烟测试 (Smoke Test).

在阶段 3 完整撮合链路就绪前作为基础冒烟占位脚本 (规划 D0-6, S3-11).
验证基础运行环境、配置骨架、模块可导入性及架构基础条件.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))



def check_python_environment():
    """检查 Python 版本 (>=3.12)."""
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 12):
        print(f"FAIL: Python 版本过低: {major}.{minor}, 必须 >= 3.12", file=sys.stderr)
        return False
    print(f"PASS: Python 运行环境就绪 ({major}.{minor}.{sys.version_info[2]})")
    return True


def check_config_template():
    """检查配置文件模板是否存在且非空."""
    cfg = ROOT / "config" / "settings.yaml.example"
    if not cfg.is_file() or cfg.stat().st_size == 0:
        print(f"FAIL: 缺失配置模板: {cfg}", file=sys.stderr)
        return False
    print("PASS: 配置模板 config/settings.yaml.example 检查通过")
    return True


def check_package_import():
    """检查 qh_trader 包基础导入."""
    try:
        import qh_trader
        import qh_trader.core
        import qh_trader.domain
        import qh_trader.gateway
        import qh_trader.data
        import qh_trader.infrastructure
        import qh_trader.engine
        print(f"PASS: 核心包 qh_trader 及其主要分层模块导入成功 (v{qh_trader.__doc__[:20]}...)")
        return True
    except Exception as exc:
        print(f"FAIL: 核心模块导入失败: {exc}", file=sys.stderr)
        return False


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    print("=== QH-Trader 冒烟测试 (Day 0 / S0 阶段占位) ===")
    ok = (
        check_python_environment()
        and check_config_template()
        and check_package_import()
    )
    if ok:
        print("=== 冒烟测试全部通过: 工程基础骨架就绪 ===")
        sys.exit(0)
    else:
        print("=== 冒烟测试失败 ===", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
