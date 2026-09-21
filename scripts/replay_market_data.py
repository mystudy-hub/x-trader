#!/usr/bin/env python
"""[S3-09 / FR-VAL-06 / A28] Bar 全链路行情回放.

- 复用 BacktestEngine 与共享领域内核，强制绑定 SimulatedGateway (不加载实盘凭证、不连接真实端口)；
- 调度模式：asap (尽快) / step (单步，每根 Bar 后等待回车) / realtime (原速) / speed=N (加速)。
  模式只影响墙钟等待，不改变事件时间、可见时间与同时间事件顺序；
- `--compare-speeds` 以多种倍速重跑同一固定输入并比对规范哈希；
- `--expected` 指向独立预期 JSON (手工样例)，回放结果与之比对而不是只比较两次运行。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.analysis.performance import calculate_performance  # noqa: E402
from qh_trader.research.backtest_assembly import BacktestSpec, assemble, build_manifest, run_assembled  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from run_backtest import add_common_arguments, configure_console, spec_from_args  # noqa: E402


class ReplayPacer:
    """按模式在两根 Bar 之间等待墙钟；业务时间完全由虚拟时钟决定."""

    def __init__(self, mode: str, speed: float = 1.0) -> None:
        self.mode = mode
        self.speed = speed
        self._last_bar_end: datetime | None = None
        self.bars_seen = 0

    def __call__(self, bar, snapshot) -> None:
        self.bars_seen += 1
        if self.mode == "step":
            print(
                f"[{snapshot.timestamp.isoformat()}] equity={snapshot.total_equity:,.2f} pos={snapshot.long_position}-{snapshot.short_position} (Enter 继续)"
            )
            sys.stdin.readline()
        elif self.mode in {"realtime", "speed"} and self._last_bar_end is not None:
            gap = (bar.bar_end - self._last_bar_end).total_seconds() / max(self.speed, 1e-9)
            if gap > 0:
                time.sleep(min(gap, 0.05))  # 墙钟等待只作演示，业务时间不受影响
        self._last_bar_end = bar.bar_end


def replay_once(spec: BacktestSpec, mode: str, speed: float) -> dict:
    assembled = assemble(spec, root=ROOT)
    pacer = ReplayPacer(mode, speed)
    result = run_assembled(assembled, on_bar_processed=pacer)
    metrics = calculate_performance(result, annual_trading_days=spec.annual_trading_days, rf_rate=spec.risk_free_rate)
    manifest = build_manifest(assembled, result, metrics, root=ROOT, extra={"replay_mode": mode, "replay_speed": speed})
    return {
        "mode": mode,
        "speed": speed,
        "bars": pacer.bars_seen,
        "final_equity": str(result.final_equity),
        "total_trades": result.total_trades,
        "total_commission": str(result.total_commission),
        "timer_events": sum(1 for e in result.events if e.kind.value == "TIMER"),
        "hashes": manifest["outputs"]["canonical_hashes"],
        "manifest": manifest,
    }


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="Replay market data end-to-end through the simulated gateway")
    add_common_arguments(parser)
    parser.add_argument("--mode", choices=["asap", "step", "realtime", "speed"], default="asap")
    parser.add_argument("--speed", type=float, default=60.0, help="Acceleration factor for --mode speed")
    parser.add_argument(
        "--compare-speeds", nargs="*", type=float, default=None, help="Rerun at these speeds and compare hashes"
    )
    parser.add_argument(
        "--expected",
        default=None,
        help="Independent expected JSON: {final_equity, total_trades, total_commission, hashes?}",
    )
    parser.add_argument("--output", default=None, help="Write replay manifest JSON here")
    args = parser.parse_args()
    spec = spec_from_args(args)

    print(f"开始全链路行情回放: {spec.symbol} {spec.interval} 模式={args.mode} (SimulatedGateway 强制绑定, 离线验证)")
    first = replay_once(spec, args.mode, args.speed)
    print(
        f"回放完成: 期末权益={float(first['final_equity']):,.2f} 元, 成交={first['total_trades']} 笔, 手续费={float(first['total_commission']):,.2f} 元, 定时器事件={first['timer_events']}"
    )
    print(f"规范哈希: {json.dumps({k: v[:12] for k, v in first['hashes'].items()})}")

    ok = True
    if args.compare_speeds:
        for speed in args.compare_speeds:
            other = replay_once(spec, "speed", speed)
            same = other["hashes"] == first["hashes"]
            ok &= same
            print(f"倍速 {speed:g}: 哈希一致={same}")
    if args.expected:
        path = Path(args.expected)
        expected = json.loads((path if path.is_absolute() else ROOT / path).read_text(encoding="utf-8"))
        actual_snapshot = first["manifest"]["inputs"]["data"]["dataset_snapshot"].get("snapshot_id")
        pinned = expected.get("dataset_snapshot_id")
        if pinned and pinned != actual_snapshot:
            print(
                f"独立预期比对: 不可比 (预期固定于数据快照 {pinned[:12]}，当前发布为 {str(actual_snapshot)[:12]}；"
                "请以 --snapshot 指定预期对应的快照，或按新快照重新生成并核对预期文件)"
            )
            expected = None
        for key in ("final_equity", "total_trades", "total_commission") if expected is not None else ():
            if key in expected and str(expected[key]) != str(first[key]):
                ok = False
                print(f"独立预期不一致: {key} 预期 {expected[key]} 实际 {first[key]}")
        for key, digest in ((expected or {}).get("hashes") or {}).items():
            if first["hashes"].get(key) != digest:
                ok = False
                print(f"独立预期哈希不一致: {key}")
        if expected is not None:
            print(f"独立预期比对: {'通过' if ok else '失败'}")
    if args.output:
        out = Path(args.output)
        out = out if out.is_absolute() else ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(first["manifest"], indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"回放清单已写入: {out}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
