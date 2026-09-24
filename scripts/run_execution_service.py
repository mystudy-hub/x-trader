#!/usr/bin/env python
"""[脚本工具] 装配并启动唯一执行服务 (S5-04, FR-RISK-01/07, FR-REC-04, ADR-X1/X2).

流程：读取配置 → 打开本地交易库并取得执行锁 → 重建账户模型 → 装配网关与查询 →
(可选) 受理接管申请：隔离 → 提升代次 → 对账 → 放行 → 主循环 (交易优先 + 命令轮询 + 心跳)。

纸面模式 (`--mode paper`) 使用模拟网关与回报投影查询，只证明本地机制；实盘模式在 S5-01 CTP 网关
交付前明确拒绝启动。停止用 Ctrl+C / SIGTERM；`--max-iterations` 供测试与演练。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.execution import CommandStatus, ExecutionNotReadyError  # noqa: E402
from scripts.live_assembly import (  # noqa: E402
    AssemblyError,
    PaperIsolation,
    assemble,
    load_settings,
    spec_from_settings,
    write_manifest,
)

LOGGER = logging.getLogger("run_execution_service")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="装配并启动唯一执行服务")
    parser.add_argument("--config", default="config/settings.yaml", help="运行配置 (不是 .example)")
    parser.add_argument("--mode", choices=("paper", "live"), default=None, help="覆盖 system.mode")
    parser.add_argument("--symbols", default=None, help="逗号分隔的实际合约 (如 SHFE.rb2410)，覆盖 strategy.symbols")
    parser.add_argument("--catalog", default=None, help="合约目录路径")
    parser.add_argument("--controller-id", default="execution-service", help="本实例控制者标识")
    parser.add_argument("--trading-day", default=None, help="预期交易日 YYYY-MM-DD (必填；夜盘属于下一交易日)")
    parser.add_argument("--heartbeat", default=None, help="心跳文件路径 (独立于 trading.db)")
    parser.add_argument("--poll-interval", type=float, default=0.1, help="命令轮询周期 (秒，50–200ms)")
    parser.add_argument("--take-over", default=None, help="受理指定的接管申请 command_id")
    parser.add_argument("--request-control", default=None, metavar="REASON", help="以本实例名义写入接管申请并立即受理")
    parser.add_argument("--confirm-isolated", action="store_true", help="操作员确认旧交易出口已隔离 (纸面模式)")
    parser.add_argument("--skip-reconcile", action="store_true", help="不做启动对账 (只处理接管或演练；不放行交易)")
    parser.add_argument("--max-iterations", type=int, default=None, help="运行固定轮数后退出 (测试 / 演练)")
    parser.add_argument("--out", default="runs/live", help="运行清单输出目录")
    parser.add_argument("--dry-run", action="store_true", help="只装配并写清单，不进入主循环")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    config_path = ROOT / args.config
    if not config_path.is_file():
        print(f"配置不存在: {config_path}", file=sys.stderr)
        return 2
    if config_path.name.endswith(".example"):
        print("不能用模板配置启动执行服务；复制为 config/settings.yaml 后按 S0 清单填写", file=sys.stderr)
        return 2
    try:
        spec = spec_from_settings(
            load_settings(config_path),
            config_path=config_path,
            mode=args.mode,
            symbols=[item.strip() for item in args.symbols.split(",")] if args.symbols else None,
            catalog_path=args.catalog,
            controller_id=args.controller_id,
            heartbeat_path=args.heartbeat,
            trading_day=date.fromisoformat(args.trading_day) if args.trading_day else None,
            poll_interval=args.poll_interval,
        )
        assembled = assemble(spec)
    except AssemblyError as exc:
        print(f"装配失败: {exc}", file=sys.stderr)
        return 2

    stop = threading.Event()

    def request_stop(signum, frame):  # noqa: ARG001
        LOGGER.info("stop requested (signal %s)", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)

    exit_code = 0
    try:
        command_id = args.take_over
        if args.request_control:
            command_id = assembled.request_control(args.request_control).command_id
        if command_id is not None:
            try:
                control = assembled.take_over(command_id, PaperIsolation(operator_confirmed=args.confirm_isolated))
                LOGGER.info("control acquired: %s epoch %s", control.controller_id, control.epoch)
            except ExecutionNotReadyError as exc:
                queued = assembled.client.get(command_id)
                status = None if queued is None else queued.status
                print(f"接管未成立 ({status or 'unknown'}): {exc}", file=sys.stderr)
                if status == CommandStatus.REJECTED:
                    print("旧交易出口未隔离；纸面模式需 --confirm-isolated 由操作员确认", file=sys.stderr)
                return 3
        current = assembled.store.control()
        if current is None:
            print("尚无控制记录：先用 --request-control REASON 建立控制权", file=sys.stderr)
            return 3
        if current.epoch.controller_id != spec.controller_id:
            # 本实例不能沿用其他控制者的代次；须显式申请并受理接管 (ADR-X1 / A23)
            print(
                f"当前控制者为 {current.epoch.controller_id} (代次 {current.epoch.epoch})，"
                f"本实例 {spec.controller_id} 须先 --request-control 接管",
                file=sys.stderr,
            )
            return 3
        if not args.skip_reconcile:
            assembled.reconcile_and_enable()
            LOGGER.info("reconciliation complete; trading enabled: %s", assembled.service.ready)
        manifest = assembled.manifest()
        out_dir = ROOT / args.out / f"{spec.account_id}-{current.epoch.controller_id}-{current.epoch.epoch}"
        path = write_manifest(out_dir, manifest)
        LOGGER.info("manifest written to %s", path)
        if args.dry_run:
            return 0
        iterations = 0
        while not stop.is_set():
            assembled.step()
            iterations += 1
            if args.max_iterations is not None and iterations >= args.max_iterations:
                break
        LOGGER.info("execution service stopped after %d iterations; ready=%s", iterations, assembled.service.ready)
    except ExecutionNotReadyError as exc:
        print(f"执行服务未就绪: {exc}", file=sys.stderr)
        exit_code = 4
    finally:
        assembled.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
