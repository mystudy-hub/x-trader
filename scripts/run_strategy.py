#!/usr/bin/env python
"""[脚本工具] 从交易 Journal 运行独立双均线策略进程 (S5-05, FR-RISK-01, ADR-X1, A23).

用法：python scripts/run_strategy.py --journal data_storage/live/trading.db --account ACC
    --strategy-id dma --run-id paper-001 --controller execution-service --epoch 1 --instrument SHFE.rb2610
    --execution-heartbeat runs/live/heartbeat/ACC-execution.json

状态库与心跳按账户/策略标识派生，放在交易库旁。首次启动只消费之后提交的 Bar；CTP Tick
不能直接触发均线策略。实例不登录柜台，控制者与代次必须显式传入，恢复必须沿用首次绑定配置。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import signal
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import Exchange  # noqa: E402
from qh_trader.core.execution import CommandKind, ExecutionNotReadyError  # noqa: E402
from qh_trader.core.objects import ControlEpoch, InstrumentId  # noqa: E402
from qh_trader.engine.journal_strategy_engine import LiveStrategyEngine  # noqa: E402
from qh_trader.infrastructure.command_queue import SQLiteCommandClient  # noqa: E402
from qh_trader.infrastructure.strategy_runtime import SQLiteStrategyRuntimeStore  # noqa: E402
from qh_trader.monitor.heartbeat import HeartbeatFile, read_heartbeat  # noqa: E402
from qh_trader.strategy.examples.async_trend_following import AsyncDualMovingAverageStrategy  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="独立策略进程：订阅已提交 Bar 并投递执行意图",
        epilog=(
            "策略仅消费 Journal 中已完成的 Bar；CTP Tick 不触发策略，需执行侧先提交 Bar。"
            "策略状态库须与交易库配套保存；自动配套备份尚未实现。"
        ),
    )
    parser.add_argument("--journal", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--run-id", required=True, help="首次运行标识；重启时必须保持一致")
    parser.add_argument("--controller", required=True, help="显式绑定的执行控制者")
    parser.add_argument("--epoch", required=True, type=int, help="显式绑定的控制代次")
    parser.add_argument("--execution-heartbeat", required=True, help="执行服务心跳文件；启动须观察到序号推进")
    parser.add_argument("--execution-timeout", type=float, default=5.0, help="执行服务心跳超时（秒）")
    parser.add_argument("--instrument", required=True, help="真实合约，例如 SHFE.rb2610")
    parser.add_argument("--fast-window", type=int, default=5)
    parser.add_argument("--slow-window", type=int, default=20)
    parser.add_argument("--order-size", type=int, default=1)
    parser.add_argument("--interval", type=float, default=0.2, help="Journal 轮询与心跳周期（秒）")
    parser.add_argument("--max-iterations", type=int, default=None, help="运行固定轮数后退出，供本地演练")
    return parser


def runtime_paths(journal: Path, account_id: str, strategy_id: str) -> tuple[Path, Path]:
    key = hashlib.sha256(json.dumps([account_id, strategy_id]).encode("utf-8")).hexdigest()[:24]
    return journal.with_name(f"strategy-{key}.db"), journal.with_name(f"strategy-{key}-heartbeat.json")


class ExecutionLivenessGate:
    """存活只依赖本进程看到序号推进的单调时间；启动不信任遗留 READY 文件."""

    def __init__(self, path: Path, epoch: int, timeout: float, *, controller_id: str, monotonic=time.monotonic):
        self.path, self.epoch, self.timeout = path, epoch, timeout
        self.controller_id = controller_id
        self._monotonic = monotonic
        self._marker = None
        self._progress = None

    def __call__(self) -> bool:
        # Windows 原子替换期间可能短暂不可读；仅做有界重读，持续失败仍立即阻断。
        for attempt in range(5):
            try:
                beat = read_heartbeat(self.path)
                if beat is not None:
                    break
            except OSError:
                pass
            except (ValueError, KeyError, TypeError):
                return False
            if attempt == 4:
                return False
            time.sleep(0.01)
        if (
            beat is None
            or beat.role != "execution"
            or not beat.ready
            or beat.control_epoch != self.epoch
            or beat.instance_id != self.controller_id
        ):
            return False
        marker = beat.instance_id, beat.sequence
        now = self._monotonic()
        if self._marker is not None:
            if marker[0] == self._marker[0] and marker[1] > self._marker[1]:
                self._progress = now
            elif marker[0] != self._marker[0] or marker[1] < self._marker[1]:
                self._progress = None
        self._marker = marker
        return self._progress is not None and now - self._progress <= self.timeout


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be finite and positive")
    if args.max_iterations is not None and args.max_iterations < 1:
        parser.error("--max-iterations must be positive")
    if not math.isfinite(args.execution_timeout) or args.execution_timeout <= 0:
        parser.error("--execution-timeout must be finite and positive")
    if args.fast_window < 1 or args.slow_window <= args.fast_window or args.order_size < 1:
        parser.error("strategy requires 1 <= fast-window < slow-window and a positive order-size")
    stop = threading.Event()
    prior_signals = {}
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            number = getattr(signal, name)
            prior_signals[number] = signal.signal(number, lambda signum, frame: stop.set())
    engine = None
    try:
        journal_path = Path(args.journal).resolve(strict=True)
        state_path, heartbeat_path = runtime_paths(journal_path, args.account, args.strategy_id)
        exchange, symbol = args.instrument.split(".", 1)
        instrument = InstrumentId(Exchange(exchange), symbol)
        control = ControlEpoch(args.controller, args.epoch)
        liveness = ExecutionLivenessGate(
            Path(args.execution_heartbeat), control.epoch, args.execution_timeout, controller_id=control.controller_id
        )
        deadline = time.monotonic() + args.execution_timeout
        while not liveness():
            if stop.is_set() or time.monotonic() >= deadline:
                raise ExecutionNotReadyError("execution heartbeat did not advance ready at the requested epoch")
            stop.wait(min(args.interval, 0.05))
        configuration = {
            "instrument": instrument,
            "fast_window": args.fast_window,
            "slow_window": args.slow_window,
            "order_size": args.order_size,
            "strategy_source_sha256": hashlib.sha256(
                (ROOT / "qh_trader/strategy/examples/async_trend_following.py").read_bytes()
                + (ROOT / "qh_trader/strategy/examples/trend_following.py").read_bytes()
            ).hexdigest(),
        }
        with SQLiteCommandClient(journal_path, account_id=args.account) as client:
            current = client.control()
            service = client.state("execution_service")
            if (
                current is None
                or current.epoch != control
                or not isinstance(service, Mapping)
                or service.get("phase") != "READY"
            ):
                raise ExecutionNotReadyError("execution controller/epoch is not ready")
            metadata = {
                "version": 1,
                "account_id": args.account,
                "strategy_id": args.strategy_id,
                "run_id": args.run_id,
                "control": control,
                "journal_path": str(journal_path),
                "configuration": configuration,
                "start_cursor": 0,  # 只有首次初始化时才从同一安全快照设置。
            }

            def initialize():
                head, view, pending = client.strategy_bootstrap()
                if (
                    not isinstance(view, Mapping)
                    or not isinstance(view.get("positions"), (list, tuple))
                    or type(view.get("active_orders")) is not int
                    or view["active_orders"] < 0
                ):
                    raise ExecutionNotReadyError("new strategy runtime requires a complete committed account view")
                if (view["positions"] or view["active_orders"]) or any(
                    queued.command.kind in (CommandKind.SUBMIT, CommandKind.CANCEL) for queued in pending
                ):
                    raise ExecutionNotReadyError(
                        "new strategy runtime requires a flat account without active/pending orders"
                    )
                return metadata | {"start_cursor": head}

            with SQLiteStrategyRuntimeStore(state_path, metadata=metadata, initialize=initialize) as runtime:
                heartbeat = HeartbeatFile(heartbeat_path, role="strategy:" + args.strategy_id)
                engine = LiveStrategyEngine(
                    journal=client,
                    runtime=runtime,
                    heartbeat=lambda ready: heartbeat.beat(control_epoch=control.epoch, ready=ready),
                    execution_ready=liveness,
                )
                strategy = AsyncDualMovingAverageStrategy(
                    args.strategy_id,
                    engine,
                    instrument,
                    fast_window=args.fast_window,
                    slow_window=args.slow_window,
                    order_size=args.order_size,
                )
                try:
                    engine.start(strategy)
                    iterations = 0
                    while not stop.is_set():
                        engine.run_once()
                        iterations += 1
                        if args.max_iterations is not None and iterations >= args.max_iterations:
                            break
                        stop.wait(args.interval)
                    print(
                        json.dumps(
                            {
                                "strategy_id": args.strategy_id,
                                "cursor": runtime.cursor,
                                "heartbeat": str(heartbeat_path),
                                "iterations": iterations,
                            }
                        )
                    )
                finally:
                    engine.stop()
    except Exception as exc:
        # 仅本地参数与状态错误，不加载或输出任何柜台凭据。
        print(f"策略进程停止: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        for number, handler in prior_signals.items():
            signal.signal(number, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
