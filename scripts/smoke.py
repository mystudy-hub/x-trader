"""QH-Trader 自动化冒烟测试 (Smoke Test).

在阶段 3 完整撮合链路就绪前作为基础冒烟占位脚本 (规划 D0-6, S3-11).
验证基础运行环境、配置骨架、模块可导入性及架构基础条件.
"""

from __future__ import annotations

import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

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


def validate_config_template(config, root=None):
    """检查模板声明及受版本控制的默认路径，不表示账户能力已核验。"""
    root = ROOT if root is None else Path(root).resolve()
    if not isinstance(config, dict):
        raise ValueError("配置必须为 YAML 映射")
    if config["system"]["mode"] not in {"backtest", "vector_scan", "paper", "shadow", "live", "replay"}:
        raise ValueError("未知运行模式")
    if config["accounting"]["mode"] not in {"research", "exact"}:
        raise ValueError("未知核算模式")
    strategy = config["strategy"]
    if strategy["category"] not in {"low_frequency_cta", "minute_intraday", "tick"}:
        raise ValueError("策略能力类别须按 FR-SCOPE-02/03 声明")
    if strategy["signal_frequency"] not in {"bar_daily", "bar_hourly", "bar_minute", "tick"}:
        raise ValueError("未知信号频率")
    if strategy["data_granularity"] not in {"1d", "1h", "1m", "tick"}:
        raise ValueError("未知价格数据粒度")
    if strategy["execution_strategy"] not in {
        "NEXT_SESSION_OPEN",
        "NEXT_DAY_SESSION_OPEN",
        "NEXT_DAY_FIXED_TIME",
        "NEXT_BAR_OPEN",
    }:
        raise ValueError("执行策略须符合 FR-EXEC-02")
    if strategy["execution_strategy"] == "NEXT_DAY_FIXED_TIME":
        offset = strategy.get("fixed_time_before_close_minutes")
        if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not 0 < offset < float("inf"):
            raise ValueError("固定执行时刻必须声明正的收盘前分钟偏移")
    if strategy["missed_execution"] not in {"defer", "cancel"}:
        raise ValueError("错过执行时点须明确顺延或取消")
    capital = Decimal(config["risk"]["initial_capital"])
    if not capital.is_finite() or capital <= 0:
        raise ValueError("示例初始资金必须是正的有限金额")
    data_root = (root / config["data"]["storage_dir"]).resolve()
    if not data_root.is_relative_to(root / "data_storage"):
        raise ValueError("模板运行数据必须位于已忽略的 data_storage/ 内")
    databases = [(root / config["storage"][key]).resolve() for key in ("journal_db_path", "rules_db_path")]
    if databases[0] == databases[1] or any(not path.is_relative_to(data_root) for path in databases):
        raise ValueError("交易库与规则库必须分开并位于运行数据目录内")


def check_config_template():
    """解析配置模板并验证基本声明、执行策略和存储路径。"""
    cfg = ROOT / "config" / "settings.yaml.example"
    if not cfg.is_file() or cfg.stat().st_size == 0:
        print(f"FAIL: 缺失配置模板: {cfg}", file=sys.stderr)
        return False
    try:
        validate_config_template(yaml.safe_load(cfg.read_text(encoding="utf-8")))
    except (yaml.YAMLError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
        print(f"FAIL: 配置模板无效: {exc}", file=sys.stderr)
        return False
    print("PASS: 配置模板声明、执行策略及存储路径检查通过")
    return True


def check_package_import():
    """检查 qh_trader 包基础导入."""
    try:
        import qh_trader
        import qh_trader.analysis
        import qh_trader.core
        import qh_trader.data
        import qh_trader.domain
        import qh_trader.engine
        import qh_trader.gateway
        import qh_trader.infrastructure
        import qh_trader.research
        import qh_trader.strategy

        print(f"PASS: {qh_trader.__name__} 及其主要分层模块导入成功")
        return True
    except Exception as exc:
        print(f"FAIL: 核心模块导入失败: {exc}", file=sys.stderr)
        return False


def check_backtest_smoke():
    """检查 S3 简化 Bar 撮合与回测链路可运行."""
    try:
        from datetime import date, datetime, timedelta, timezone
        from decimal import Decimal

        from qh_trader.core.constants import Exchange
        from qh_trader.core.objects import Bar, InstrumentId, RecordMeta
        from qh_trader.engine.backtest_engine import BacktestEngine
        from qh_trader.engine.base_engine import InstrumentEconomics
        from qh_trader.gateway.simulated_gateway import SimulatedGateway
        from qh_trader.strategy.base import StrategyBase

        inst = InstrumentId(Exchange.SHFE, "rb2410")
        start = datetime(2024, 9, 10, 1, 0, tzinfo=timezone.utc)
        meta = RecordMeta(
            event_time=start + timedelta(hours=1),
            available_at=start + timedelta(hours=1),
            ingested_at=start + timedelta(hours=1),
            trading_day=date(2024, 9, 10),
            source_id="smoke",
            source_version="v1",
            ingest_seq=1,
        )
        bar = Bar(
            instrument=inst,
            meta=meta,
            bar_start=start,
            bar_end=start + timedelta(hours=1),
            open_time=start,
            interval="1h",
            open=Decimal("3000"),
            high=Decimal("3050"),
            low=Decimal("2980"),
            close=Decimal("3020"),
            volume=100,
            turnover=Decimal("100000"),
            open_interest=50000,
            includes_auction=False,
        )
        gw = SimulatedGateway("smoke-acc", date(2024, 9, 10))
        economics = InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("5.0"), Decimal("0.1"), "smoke")
        engine = BacktestEngine(account_id="smoke-acc", gateway=gw, start_time=start, default_economics=economics)

        class DummyStrat(StrategyBase):
            def on_bar(self, b):
                pass

        engine.add_strategy(DummyStrat("dummy", engine))
        res = engine.run([bar])
        if res.final_equity > 0:
            print("PASS: S3 事件驱动 Bar 撮合与回测引擎冒烟通过")
            return True
        return False
    except Exception as exc:
        print(f"FAIL: 回测引擎冒烟失败: {exc}", file=sys.stderr)
        return False


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    print("=== QH-Trader 冒烟测试 (S3 阶段) ===")
    ok = (
        check_python_environment()
        and check_config_template()
        and check_package_import()
        and check_backtest_smoke()
    )
    if ok:
        print("=== 冒烟测试全部通过: 工程基础骨架与回测引擎就绪 ===")
        sys.exit(0)
    else:
        print("=== 冒烟测试失败 ===", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
