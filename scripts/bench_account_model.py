"""[Scripts 层] 实盘账户模型负载基准：多交易日委托 / 回报 / 行情经执行服务落盘的耗时与库体积 (S5-04, 06 R11).

在临时目录里装配真实的 SQLite Journal、执行服务存储、命令客户端、实盘账户模型与执行服务，网关为只记录调用、
返回"远端结果未知"的桩，按交易日循环：开仓 → 回报 → 成交 → 全成回报 → 平今 → ...，穿插行情快照，日终写结算价
并推进交易日。输出每个交易日的命令、回报、行情与日终 (结算价 + 推进交易日，含检查点写入) 处理耗时
(中位数 / 最大值)、检查点之后的事实条数与库文件大小。

这是本地负载证据：不连接柜台、不代表柜台联调或实时性能，也不替代 S5-12 连续仿真。

用法:
    uv run --no-sync python scripts/bench_account_model.py --days 10 --round-trips 20 --ticks 200
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import (  # noqa: E402
    EventKind,
    Exchange,
    MarketPhase,
    Offset,
    OrderStatus,
    OrderType,
    QualityFlag,
    SendState,
    Side,
)
from qh_trader.core.event import CanonicalEvent, JournalTransaction  # noqa: E402
from qh_trader.core.execution import CommandKind, ExecutionCommand  # noqa: E402
from qh_trader.core.objects import (  # noqa: E402
    AccountFunds,
    ControlEpoch,
    ControlRecord,
    InstrumentId,
    LocalSendResult,
    OrderIdentity,
    OrderIntent,
    OrderUpdate,
    QueryBatch,
    QueryResult,
    RecordMeta,
    Settlement,
    Tick,
    Trade,
    TradeKey,
)
from qh_trader.domain.recovery import RecoveryCoordinator  # noqa: E402
from qh_trader.engine.base_engine import InstrumentEconomics  # noqa: E402
from qh_trader.engine.execution_service import ExecutionService  # noqa: E402
from qh_trader.engine.live_account_model import ADVANCE_TRADING_DAY, AccountOpening, LiveAccountModel  # noqa: E402
from qh_trader.infrastructure.command_queue import SQLiteCommandClient, SQLiteExecutionStore  # noqa: E402
from qh_trader.infrastructure.journal import SQLiteJournal  # noqa: E402

ACCOUNT = "bench-account"
CONTROL = ControlEpoch("bench-controller", 1)
RB = InstrumentId(Exchange.SHFE, "rb2610")
NOW = datetime(2026, 9, 28, 1, tzinfo=timezone.utc)
FIRST_DAY = date(2026, 9, 28)
ECONOMICS = {RB: InstrumentEconomics(Decimal("10"), Decimal("1"), Decimal("0"), Decimal("0.1"), "benchmark")}


class UnknownResultGateway:
    """只记录调用；每次都返回"远端结果未知"，与真实柜台发送后等待回报的路径一致."""

    def __init__(self) -> None:
        self.calls = 0

    def submit(self, order, control):
        self.calls += 1
        return LocalSendResult(SendState.SENT_UNKNOWN, 0, "benchmark: remote result unknown")

    def cancel(self, identity, control):
        self.calls += 1
        return LocalSendResult(SendState.SENT_UNKNOWN, 0, "benchmark: remote result unknown")

    def capabilities(self):
        raise NotImplementedError


def _event(event_id: str, kind: EventKind, payload) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        kind=kind,
        event_time=NOW,
        available_at=NOW,
        sequence=0,
        source_id="benchmark",
        payload=payload,
    )


def _meta(day: date, seq: int) -> RecordMeta:
    return RecordMeta(
        event_time=NOW,
        available_at=NOW,
        ingested_at=NOW,
        trading_day=day,
        source_id="benchmark",
        source_version="1",
        ingest_seq=seq,
        quality_flags=QualityFlag.OK,
    )


class Bench:
    def __init__(self, path: Path) -> None:
        self.stack = ExitStack()
        self.journal = self.stack.enter_context(SQLiteJournal(path, account_id=ACCOUNT))
        self.journal.migrate()
        seed = CanonicalEvent(
            event_id="benchmark-control",
            kind=EventKind.CONTROL,
            event_time=NOW,
            available_at=NOW,
            sequence=1,
            source_id="benchmark",
            payload={"benchmark": "seed"},
        )
        self.journal.append(
            JournalTransaction(
                transaction_id="benchmark-control",
                events=(seed,),
                cursor_before=0,
                cursor_after=1,
                control_record=ControlRecord(CONTROL, NOW, 1),
            )
        )
        self.store = self.stack.enter_context(SQLiteExecutionStore(self.journal))
        self.store.migrate()
        self.client = self.stack.enter_context(SQLiteCommandClient(path, account_id=ACCOUNT))
        self.model = LiveAccountModel(ACCOUNT, AccountOpening(Decimal("1000000"), FIRST_DAY), ECONOMICS)
        placeholder = self.model.replica()
        self.recovery = RecoveryCoordinator(placeholder.orders, placeholder.positions)
        self.service = ExecutionService(
            store=self.store, model=self.model, gateway=UnknownResultGateway(), recovery=self.recovery
        )
        self.path = path

    def close(self) -> None:
        self.stack.close()

    def _query(self, kind: str, records=()) -> QueryResult:
        return QueryResult(
            batch=QueryBatch(kind, ACCOUNT, FIRST_DAY, NOW),
            records=records,
            available_at=NOW,
            source_id="benchmark",
            source_version="1",
            complete=True,
        )

    def make_ready(self) -> None:
        replica = self.model.replica()
        self.recovery.order_manager = replica.orders
        self.recovery.position_manager = replica.positions
        self.recovery.start_recovery(expected_trading_day=FIRST_DAY)
        self.recovery.begin_reconciliation()
        self.recovery.merge_order_query(self._query("orders"))
        self.recovery.merge_trade_query(self._query("trades"))
        self.recovery.reconcile_positions(self._query("positions"))
        balance = self.model.ledger.balance
        funds = AccountFunds(balance, balance, Decimal("0"), balance)
        self.recovery.reconcile_funds(self._query("funds", (funds,)), balance)
        self.service.enable_after_reconciliation()

    def command(self, identifier: str, payload, kind: CommandKind = CommandKind.SUBMIT) -> float:
        self.client.submit(
            ExecutionCommand(
                command_id=identifier,
                account_id=ACCOUNT,
                producer_id="benchmark",
                control=CONTROL,
                kind=kind,
                submitted_at=NOW,
                payload=payload,
            )
        )
        started = time.perf_counter()
        self.service.process_next_command()
        return time.perf_counter() - started

    def fact(self, event: CanonicalEvent) -> float:
        started = time.perf_counter()
        self.service.enqueue(event)
        self.service.run_once()
        return time.perf_counter() - started


def _intent(client_order_id: str, side: Side, offset: Offset, price: int) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        account_id=ACCOUNT,
        strategy_id="benchmark",
        instrument=RB,
        side=side,
        offset=offset,
        quantity=1,
        order_type=OrderType.LIMIT,
        created_at=NOW,
        limit_price_ticks=price,
    )


def _report(client_order_id: str, side: Side, offset: Offset, status: OrderStatus, filled: int) -> CanonicalEvent:
    update = OrderUpdate(
        identity=OrderIdentity(
            account_id=ACCOUNT,
            exchange=Exchange.SHFE,
            client_order_id=client_order_id,
            exchange_order_id="EX-" + client_order_id,
        ),
        instrument=RB,
        side=side,
        offset=offset,
        status=status,
        quantity=1,
        filled_quantity=filled,
        event_time=NOW,
        available_at=NOW,
    )
    return _event(f"report:{client_order_id}:{status.value}", EventKind.ORDER_REPORT, update)


def _trade(client_order_id: str, day: date, side: Side, offset: Offset, price: int) -> CanonicalEvent:
    trade_id = "T-" + client_order_id
    trade = Trade(
        account_id=ACCOUNT,
        instrument=RB,
        trading_day=day,
        trade_id=trade_id,
        side=side,
        offset=offset,
        quantity=1,
        price=Decimal(price),
        event_time=NOW,
        available_at=NOW,
        deduplication_key=TradeKey(ACCOUNT, Exchange.SHFE, day, trade_id),
        order_identity=OrderIdentity(account_id=ACCOUNT, exchange=Exchange.SHFE, client_order_id=client_order_id),
    )
    return _event("trade:" + trade_id, EventKind.TRADE_REPORT, trade)


def _tick(day: date, seq: int, price: int) -> CanonicalEvent:
    tick = Tick(
        instrument=RB,
        meta=_meta(day, seq),
        last_price=Decimal(price),
        bid_price=None,
        ask_price=None,
        bid_volume=None,
        ask_volume=None,
        cumulative_volume=seq,
        cumulative_turnover=Decimal(seq),
        open_interest=1,
        pre_settlement_price=None,
        upper_limit_price=None,
        lower_limit_price=None,
        phase=MarketPhase.CONTINUOUS,
    )
    return _event(f"tick:{day}:{seq}", EventKind.MARKET_DATA, tick)


def _settlement(day: date, price: int) -> CanonicalEvent:
    payload = Settlement(
        instrument=RB,
        meta=_meta(day, 1),
        settlement_price=Decimal(price),
        pre_settlement_price=None,
        published_at=NOW,
        is_final=True,
    )
    return _event(f"settle:{day}", EventKind.SETTLEMENT, payload)


def _advance(day: date, new_day: date) -> CanonicalEvent:
    payload = {"action": ADVANCE_TRADING_DAY, "trading_day": day, "new_trading_day": new_day, "version": "v1"}
    return _event(f"advance:{day}:{new_day}", EventKind.CONTROL, payload)


def _ms(values: list[float]) -> dict[str, float]:
    if not values:
        return {"median_ms": 0.0, "max_ms": 0.0}
    return {"median_ms": round(statistics.median(values) * 1000, 2), "max_ms": round(max(values) * 1000, 2)}


def run(days: int, round_trips: int, ticks: int, directory: Path) -> list[dict[str, object]]:
    bench = Bench(directory / "trading.db")
    rows: list[dict[str, object]] = []
    try:
        bench.make_ready()
        day, counter, tick_seq = FIRST_DAY, 0, 0
        for index in range(days):
            commands: list[float] = []
            facts: list[float] = []
            market: list[float] = []
            per_trip = max(1, ticks // max(1, round_trips))
            for _ in range(round_trips):
                for side, offset in ((Side.BUY, Offset.OPEN), (Side.SELL, Offset.CLOSE_TODAY)):
                    counter += 1
                    identifier = f"o{counter}"
                    commands.append(bench.command("c" + identifier, _intent(identifier, side, offset, 3000)))
                    facts.append(bench.fact(_report(identifier, side, offset, OrderStatus.ACCEPTED, 0)))
                    facts.append(bench.fact(_trade(identifier, day, side, offset, 3000)))
                    facts.append(bench.fact(_report(identifier, side, offset, OrderStatus.FILLED, 1)))
                for _ in range(per_trip):
                    tick_seq += 1
                    market.append(bench.fact(_tick(day, tick_seq, 3000 + tick_seq % 7)))
            window = bench.model.fact_count
            next_day = day + timedelta(days=1)
            day_end = [bench.fact(_settlement(day, 3000)), bench.fact(_advance(day, next_day))]
            day = next_day
            bench.journal.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            rows.append(
                {
                    "day": index + 1,
                    "commands": _ms(commands),
                    "facts": _ms(facts),
                    "market": _ms(market),
                    "day_end": _ms(day_end),
                    "facts_before_settlement": window,
                    "facts_after_settlement": bench.model.fact_count,
                    "checkpoint_through": bench.model.checkpoint_through,
                    "database_mb": round((directory / "trading.db").stat().st_size / 1e6, 2),
                }
            )
            row = rows[-1]
            timings = "  ".join(
                f"{label} {row[key]['median_ms']:7.2f}/{row[key]['max_ms']:7.2f} ms"  # type: ignore[index]
                for label, key in (
                    ("command", "commands"),
                    ("fact", "facts"),
                    ("tick", "market"),
                    ("day-end", "day_end"),
                )
            )
            print(
                f"day {index + 1:3d}  {timings}  window {window:4d} -> {bench.model.fact_count:3d}"
                f"  db {row['database_mb']:7.2f} MB",
                flush=True,
            )
    finally:
        bench.close()
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--days", type=int, default=10, help="模拟交易日数")
    parser.add_argument("--round-trips", type=int, default=20, help="每个交易日的开平仓往返次数 (每次 2 笔委托)")
    parser.add_argument("--ticks", type=int, default=200, help="每个交易日的行情快照数")
    parser.add_argument("--json", default=None, help="可选：把逐日结果写入该 JSON 文件")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="qh-bench-") as directory:
        rows = run(args.days, args.round_trips, args.ticks, Path(directory))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "evidence_type": "local benchmark; not broker or real-time evidence",
            "parameters": {"days": args.days, "round_trips": args.round_trips, "ticks": args.ticks},
            "days": rows,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
