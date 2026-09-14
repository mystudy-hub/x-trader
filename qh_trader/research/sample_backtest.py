"""Small research demonstration with explicit fills; not the S2 trading ledger or an S3 acceptance engine."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from qh_trader.core.clock import VirtualClock, utc_timestamp
from qh_trader.core.constants import MissingRuleError, PriceType
from qh_trader.core.objects import InstrumentId, require_decimal, require_int
from qh_trader.core.ports import MarketDataPort


@dataclass
class ResearchPositionBook:
    initial_capital: Decimal
    multiplier: Decimal
    fee_per_lot: Decimal
    quantity: int = field(init=False, default=0)
    cost: Decimal = field(init=False, default=Decimal(0))
    realized_pnl: Decimal = field(init=False, default=Decimal(0))
    fees: Decimal = field(init=False, default=Decimal(0))

    def __post_init__(self) -> None:
        for name in ("initial_capital", "multiplier", "fee_per_lot"):
            require_decimal(getattr(self, name), name, Decimal(0))
        if self.initial_capital <= 0 or self.multiplier <= 0:
            raise ValueError("initial capital and multiplier must be positive")

    def fill_target(self, target: int, price: Decimal) -> dict[str, Any] | None:
        require_int(target, "target quantity", None)
        require_decimal(price, "fill price")
        delta = target - self.quantity
        if delta == 0:
            return None
        old = self.quantity
        realized = Decimal(0)
        if old == 0:
            self.cost = price
        elif old * delta > 0:
            self.cost = (abs(old) * self.cost + abs(delta) * price) / abs(target)
        else:
            closed = min(abs(old), abs(delta))
            realized = closed * (price - self.cost) * (1 if old > 0 else -1) * self.multiplier
            self.realized_pnl += realized
            if target == 0:
                self.cost = Decimal(0)
            elif old * target < 0:
                self.cost = price
        fee = abs(delta) * self.fee_per_lot
        self.fees += fee
        self.quantity = target
        return {"quantity": delta, "price": price, "fee": fee, "realized_pnl": realized, "position_after": target}

    def equity(self, price: Decimal) -> Decimal:
        require_decimal(price, "mark price")
        return (
            self.initial_capital + self.realized_pnl - self.fees + self.quantity * (price - self.cost) * self.multiplier
        )


def simulate_trend(
    market: MarketDataPort,
    instrument: InstrumentId,
    interval: str,
    observations: Sequence[datetime],
    openings: Sequence[tuple[datetime, str | None, PriceType]],
    *,
    multiplier: Decimal,
    fast_window: int = 5,
    slow_window: int = 20,
    initial_capital: Decimal = Decimal("1000000"),
    fee_per_lot: Decimal = Decimal("5"),
    order_size: int = 10,
) -> dict[str, Any]:
    for name, value in (("fast_window", fast_window), ("slow_window", slow_window), ("order_size", order_size)):
        require_int(value, name, 1)
    if fast_window >= slow_window:
        raise ValueError("fast_window must be smaller than slow_window")
    points: dict[datetime, list[tuple[str | None, PriceType]]] = {}
    for at, session, kind in openings:
        points.setdefault(utc_timestamp(at), []).append((session, kind))
    timeline = sorted(set(map(utc_timestamp, observations)) | set(points))
    if not timeline:
        raise ValueError("no canonical market observations are available")
    clock: VirtualClock[object] = VirtualClock(timeline[0])
    book = ResearchPositionBook(initial_capital, multiplier, fee_per_lot)
    pending: tuple[datetime, int] | None = None
    last_bar_key = None
    mark: Decimal | None = None
    peak = initial_capital
    max_drawdown = Decimal(0)
    equity = initial_capital
    fills = []
    for at in timeline:
        clock.advance_to(at)
        # Explicit conservative tie order: opening observations occur before same-time signals.
        for session_id, price_type in points.get(at, ()):
            if pending is None or pending[0] >= clock.now() or pending[1] == book.quantity:
                continue
            if session_id is None:
                raise MissingRuleError("execution point has no verified session identity")
            reference = market.execution_reference(instrument, session_id, at, price_type, clock.now())
            if reference is None:
                raise MissingRuleError("the selected next-bar opening price is unavailable at the execution clock")
            if (reference.instrument, reference.session_id, reference.reference_time, reference.price_type) != (
                instrument,
                session_id,
                at,
                price_type,
            ) or reference.meta.available_at > clock.now():
                raise ValueError("execution port returned a mismatched or future observation")
            fill = book.fill_target(pending[1], reference.price)
            mark = reference.price
            if fill is not None:
                fills.append({**fill, "at": at.isoformat(), "signal_at": pending[0].isoformat()})
            pending = None
        visible = tuple(market.bars(instrument, interval, until=clock.now()))
        if any(bar.meta.available_at > clock.now() or bar.instrument != instrument for bar in visible):
            raise ValueError("market port returned future or unrelated bars")
        if visible:
            visible = tuple(sorted(visible, key=lambda bar: bar.bar_end))
            latest = visible[-1]
            key = latest.bar_end, latest.meta.source_id, latest.meta.source_version, latest.meta.ingest_seq
            if key != last_bar_key:
                last_bar_key = key
                mark = latest.close
                if len(visible) >= slow_window:
                    fast = sum((bar.close for bar in visible[-fast_window:]), Decimal(0)) / fast_window
                    slow = sum((bar.close for bar in visible[-slow_window:]), Decimal(0)) / slow_window
                    target = order_size if fast > slow else -order_size if fast < slow else book.quantity
                    pending = (clock.now(), target) if target != book.quantity else None
        if mark is not None:
            equity = book.equity(mark)
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    if last_bar_key is None:
        raise ValueError("the market port returned no visible bars; backtest validation did not run")
    return {
        "validation_scope": "research_prototype",
        "execution_policy": "NEXT_BAR_OPEN",
        "same_time_order": "open_before_signal",
        "initial_capital": initial_capital,
        "final_equity": equity,
        "total_return_pct": (equity / initial_capital - 1) * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "trades_count": len(fills),
        "fills": fills,
        "realized_pnl": book.realized_pnl,
        "fees": book.fees,
        "ending_position": book.quantity,
        "assumptions": {
            "fee_per_lot": str(fee_per_lot),
            "slippage": "0",
            "order_size": order_size,
            "fill_model": "full fill at the first known opening strictly after the signal",
            "margin_limits_and_daily_settlement": "not modeled; not a trading-engine acceptance result",
        },
    }
