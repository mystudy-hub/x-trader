#!/usr/bin/env python
"""[Scripts 层] SimNow 单合约策略观察与受门禁约束的仿真入口 (S5-05, FR-LIVE-04, FR-ORD-08)。

默认 observe，只录行情和策略观察结果。trade 必须在连接前通过预热、日历、费率和
平仓能力检查；策略命令只进入唯一账户执行服务的 SQLite 命令队列。
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import MarketPhase, Offset, OrderType, PositionSide, QualityFlag, Side  # noqa: E402
from qh_trader.core.execution import CommandStatus  # noqa: E402
from qh_trader.core.objects import InstrumentId, OrderIntent, OrderUpdate, Session, Tick, Trade  # noqa: E402
from qh_trader.data.calendar import TradingCalendar  # noqa: E402
from qh_trader.data.contracts import ContractResolver  # noqa: E402
from qh_trader.data.live_bars import LiveBarAggregator, LiveBarDataError  # noqa: E402
from qh_trader.engine.base_engine import CommissionSchedule, InstrumentEconomics  # noqa: E402
from qh_trader.engine.live_engine import LiveEngine, StrategyCheckpoint  # noqa: E402
from qh_trader.gateway.ctp_market import CtpMarketDataGateway, CtpMarketSettings  # noqa: E402
from qh_trader.infrastructure import journal_codec  # noqa: E402
from qh_trader.infrastructure.strategy_checkpoint import SQLiteStrategyCheckpointStore  # noqa: E402
from qh_trader.strategy.examples.ema_trend import EmaTrendStrategy  # noqa: E402
from scripts import ctp_setup, live_assembly  # noqa: E402
from scripts.ctp_probe import mask_identifier  # noqa: E402
from scripts.simnow_preflight import registered_front_pair  # noqa: E402
from scripts.validate_strategy import load_validation_config, load_warmup_bars, parameters_from_config  # noqa: E402


class RuntimeBlocked(ValueError):  # noqa: N818
    """已识别的运行资料或安全门禁缺口，连接前返回明确原因。"""


class CheckpointMappingStore:
    """将引擎游标转换成基础设施允许的 Core Mapping，不反向引入引擎依赖。"""

    def __init__(self, store: SQLiteStrategyCheckpointStore) -> None:
        self.store = store

    def load(self, stream_id: str) -> StrategyCheckpoint | None:
        value = self.store.load(stream_id)
        return None if value is None else StrategyCheckpoint(**dict(value))

    def save(self, checkpoint: StrategyCheckpoint) -> None:
        self.store.save(
            checkpoint.stream_id, {field.name: getattr(checkpoint, field.name) for field in fields(checkpoint)}
        )


class CommittedAccountView:
    """只读已发布账户对象；不调用会自动创建零持仓的 get_position 接口。"""

    def __init__(self, model, store) -> None:
        self.model, self.store = model, store

    def quantities(self, instrument: InstrumentId, side: PositionSide) -> tuple[int, int]:
        values = [
            row for row in self.model.positions.all_positions() if row.instrument == instrument and row.side == side
        ]
        return sum(row.pos_yd - row.frozen_yd for row in values), sum(row.pos_td - row.frozen_td for row in values)

    def get_position(self, instrument: InstrumentId) -> int:
        rows = [row for row in self.model.positions.all_positions() if row.instrument == instrument]
        long = sum(row.pos_yd + row.pos_td for row in rows if row.side == PositionSide.LONG)
        short = sum(row.pos_yd + row.pos_td for row in rows if row.side == PositionSide.SHORT)
        if long and short:
            raise RuntimeBlocked("two_sided_exposure_requires_reconciliation")
        return long - short

    def is_order_active(self, client_order_id: str) -> bool:
        order = self.model.orders.get_order(client_order_id)
        return order is not None and order.is_active

    def order_identity(self, client_order_id: str):
        order = self.model.orders.get_order(client_order_id)
        return None if order is None else order.identity

    def has_active_orders(self, instrument: InstrumentId) -> bool:
        return bool(self.model.orders.active_orders(instrument)) or self.model.orders.has_open_reconciliation

    def equity(self) -> Decimal:
        # 首次 opened 事实尚无 account_view 投影；从已发布模型的资金接口读取，不能假设投影键存在。
        value = self.model.funds_state().total_equity
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise RuntimeBlocked("committed_account_equity_unavailable")
        return value


class FlatObservationView:
    def __init__(self, equity: Decimal) -> None:
        self._equity = equity

    def get_position(self, instrument):
        return 0

    def quantities(self, instrument, side):
        return 0, 0

    def is_order_active(self, client_order_id):
        return False

    def order_identity(self, client_order_id):
        return None

    def has_active_orders(self, instrument):
        return False

    def equity(self):
        return self._equity


class ObservationClient:
    """观察模式没有 SQLite 命令表，更没有交易网关。"""

    def get(self, command_id):
        return None

    def submit(self, command):
        raise RuntimeError("observe mode cannot submit account commands")


class RuntimeClock:
    def __init__(self) -> None:
        self._timers = []
        self._sequence = 0

    def now(self):
        return datetime.now(timezone.utc)

    def schedule(self, at, event):
        self._sequence += 1
        heapq.heappush(self._timers, (at, self._sequence, event))

    def due(self):
        while self._timers and self._timers[0][0] <= self.now():
            yield heapq.heappop(self._timers)[2]


class QueueSink:
    """行情回调只排有界队列；交易事实使用账户服务提交后的独立观察队列。"""

    def __init__(self, capacity=8192) -> None:
        self.queue = Queue(maxsize=capacity)
        self.dropped = 0
        self.errors = 0

    def enqueue(self, event):
        try:
            self.queue.put_nowait(event)
            return True
        except Full:
            self.dropped += 1
            return False

    def enqueue_callback_error(self, source_id, error):
        self.errors += 1


def load_runtime_inputs(path: Path, *, root: Path, now: datetime):
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("profile") != "simnow_v6" or report.get("kind") != "simnow_read_only_preflight":
        raise RuntimeBlocked("registered_simnow_preflight_required")
    inputs = report.get("runtime_inputs") or {}
    selected = (report.get("selection") or {}).get("instrument")
    if not selected or inputs.get("symbol") != selected:
        raise RuntimeBlocked("preflight_has_no_consistent_actual_contract_selection")
    prefix, symbol = selected.split(".", 1)
    if prefix != "SHFE" or not symbol.startswith("rb") or not symbol[2:].isdigit() or len(symbol) != 6:
        raise RuntimeBlocked("only_actual_SHFE_rb_contract_supported")
    if report.get("counter_trading_day") != inputs.get("trading_day"):
        raise RuntimeBlocked("preflight_trading_day_mismatch")
    profile = ctp_setup.load_broker_profile("simnow_v6")
    fronts = registered_front_pair(profile, inputs.get("front_trade"), inputs.get("front_market"))
    if fronts != (report.get("front_trade"), report.get("front_market")):
        raise RuntimeBlocked("preflight_fronts_mismatch")
    generated = datetime.fromisoformat(report["generated_at"])
    if generated.tzinfo is None or generated > now:
        raise RuntimeBlocked("preflight_observation_time_invalid")
    catalog_path = (root / inputs["catalog_path"]).resolve()
    if not catalog_path.is_relative_to(root.resolve()):
        raise RuntimeBlocked("preflight_catalog_must_be_local_to_project")
    day = date.fromisoformat(inputs["trading_day"])
    contract = ContractResolver.from_file(catalog_path).get_spec(selected, as_of=day, known_at=now)
    return report, profile, contract, catalog_path, day


def economics_from_preflight(report: Mapping[str, Any], contract, *, source: str) -> InstrumentEconomics:
    """严格关联实际合约费率；按金额和按手数的开、平昨、平今费用完整传入共享内核。"""

    def unique_rate(kind):
        result = (report.get("rates") or {}).get(kind) or {}
        rows = result.get("records", ())
        if not result.get("complete") or len(rows) != 1:
            raise RuntimeBlocked(f"complete_unambiguous_counter_{kind}_required")
        row = rows[0]
        if (
            row.get("InstrumentID") != contract.instrument.symbol
            or row.get("ExchangeID") != contract.instrument.exchange.value
        ):
            raise RuntimeBlocked(f"counter_{kind}_must_exactly_match_actual_contract")
        return row

    def number(row, key):
        try:
            value = Decimal(str(row[key]))
        except (KeyError, ArithmeticError, ValueError) as exc:
            raise RuntimeBlocked("counter_economics_fields_incomplete") from exc
        if not value.is_finite() or value < 0:
            raise RuntimeBlocked("counter_economics_fields_invalid")
        return value

    fees, margin = unique_rate("commission"), unique_rate("margin")
    if str(margin.get("IsRelative")) not in ("0", "False") or str(margin.get("HedgeFlag")) != "1":
        raise RuntimeBlocked("absolute_speculative_counter_margin_required")
    margin_long = number(margin, "LongMarginRatioByMoney")
    margin_short = number(margin, "ShortMarginRatioByMoney")
    if margin_long <= 0 or margin_long != margin_short:
        raise RuntimeBlocked("equal_positive_long_short_counter_margin_required")
    if number(margin, "LongMarginRatioByVolume") or number(margin, "ShortMarginRatioByVolume"):
        raise RuntimeBlocked("per_lot_margin_unsupported_by_ratio_account_model")
    schedule = CommissionSchedule(
        multiplier=contract.multiplier,
        open_money_ratio=number(fees, "OpenRatioByMoney"),
        open_per_lot=number(fees, "OpenRatioByVolume"),
        close_yesterday_money_ratio=number(fees, "CloseRatioByMoney"),
        close_yesterday_per_lot=number(fees, "CloseRatioByVolume"),
        close_today_money_ratio=number(fees, "CloseTodayRatioByMoney"),
        close_today_per_lot=number(fees, "CloseTodayRatioByVolume"),
        source=source,
    )
    return InstrumentEconomics(
        multiplier=contract.multiplier,
        price_tick=contract.price_tick,
        commission_per_lot=Decimal(0),
        margin_ratio=margin_long,
        source=source,
        commission_schedule=schedule,
    )


def verify_configured_identity(preflight, user_id: str | None, investor_id: str | None) -> None:
    for field, value in (("user", user_id), ("investor", investor_id)):
        expected = (preflight.get(field) or {}).get("sha256_prefix")
        if not value or not expected:
            raise RuntimeBlocked("preflight_account_identity_link_required")
        if mask_identifier(value)["sha256_prefix"] != expected:
            raise RuntimeBlocked("configured_account_differs_from_preflight")


def verify_counter_identity(preflight, assembled) -> None:
    """关联柜台返回的投资者和当前 FrontID/SessionID，缺失关联不能授予执行权。"""
    settings = assembled.counter_gateway.settings
    verify_configured_identity(preflight, settings.user_id, settings.investor_id)
    try:
        investor = assembled.query.query_investor()
        sessions = assembled.query.query_user_sessions(settings.user_id)
    except Exception as exc:
        raise RuntimeBlocked("counter_identity_queries_unavailable") from exc
    if not investor or str(investor.get("BrokerID")) != settings.broker_id:
        raise RuntimeBlocked("counter_investor_identity_link_required")
    verify_configured_identity(preflight, settings.user_id, str(investor.get("InvestorID") or ""))
    status = assembled.counter_gateway.status()
    linked = [
        row
        for row in sessions
        if row.get("FrontID") == status.get("front_id") and row.get("SessionID") == status.get("session_id")
    ]
    if len(linked) != 1 or str(linked[0].get("BrokerID")) != settings.broker_id:
        raise RuntimeBlocked("counter_current_user_session_link_required")
    verify_configured_identity(preflight, str(linked[0].get("UserID") or ""), settings.investor_id)


def account_state_directory(root: Path, user_id: str) -> Path:
    if not user_id:
        raise RuntimeBlocked("account_user_id_required_for_single_writer_state")
    key = hashlib.sha256(("simnow_v6:" + user_id).encode()).hexdigest()[:24]
    return root / "runs" / "simnow_strategy_state" / key


def verified_close_mapping(profile, instrument):
    for mapping in ctp_setup.offset_mappings(profile):
        if mapping.exchange == instrument.exchange and mapping.verified and mapping.evidence_ref:
            if all(offset in mapping.flags for offset in (Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY)):
                return mapping
    return None


class LimitOrderTranslator:
    """根据新鲜对手价加显式滑点转换限价，单手约束及平仓桶在命令落库前复核。"""

    def __init__(self, *, instrument, price_tick, account, quote_provider, clock, slippage_ticks, close_mapping):
        if isinstance(slippage_ticks, bool) or not isinstance(slippage_ticks, int) or slippage_ticks < 0:
            raise RuntimeBlocked("explicit_nonnegative_slippage_ticks_required")
        self.instrument, self.price_tick, self.account = instrument, price_tick, account
        self.quote_provider, self.clock = quote_provider, clock
        self.slippage_ticks, self.close_mapping = slippage_ticks, close_mapping

    def __call__(self, intent: OrderIntent):
        if intent.instrument != self.instrument or intent.quantity != 1:
            raise RuntimeBlocked("only_one_lot_for_selected_actual_contract_is_allowed")
        quote = self.quote_provider()
        if quote is None or quote.instrument != intent.instrument:
            raise RuntimeBlocked("fresh_selected_contract_quote_required")
        if not timedelta(0) <= self.clock.now() - quote.meta.event_time <= timedelta(seconds=5):
            raise RuntimeBlocked("quote_is_stale_or_future_dated")
        if quote.upper_limit_price is None or quote.lower_limit_price is None:
            raise RuntimeBlocked("counter_price_limits_required")
        reference = quote.ask_price if intent.side == Side.BUY else quote.bid_price
        if reference is None or reference <= 0 or quote.lower_limit_price > quote.upper_limit_price:
            raise RuntimeBlocked("valid_opposite_quote_and_limits_required")
        slip = self.price_tick * self.slippage_ticks
        price = reference + slip if intent.side == Side.BUY else reference - slip
        rounding = ROUND_CEILING if intent.side == Side.BUY else ROUND_FLOOR
        ticks = int((price / self.price_tick).to_integral_value(rounding=rounding))
        if not quote.lower_limit_price <= Decimal(ticks) * self.price_tick <= quote.upper_limit_price:
            raise RuntimeBlocked("translated_limit_price_exceeds_counter_limits")
        offset = intent.offset
        if offset == Offset.OPEN:
            if self.account.get_position(self.instrument) or self.account.has_active_orders(self.instrument):
                raise RuntimeBlocked("opening_requires_flat_committed_account_and_no_active_order")
        else:
            mapping = self.close_mapping
            if mapping is None or not mapping.verified or not mapping.evidence_ref:
                raise RuntimeBlocked("verified_close_today_yesterday_mapping_required")
            side = PositionSide.LONG if intent.side == Side.SELL else PositionSide.SHORT
            yd, td = self.account.quantities(self.instrument, side)
            if offset == Offset.CLOSE:
                offset = Offset.CLOSE_YESTERDAY if yd else Offset.CLOSE_TODAY
            available = yd if offset == Offset.CLOSE_YESTERDAY else td if offset == Offset.CLOSE_TODAY else 0
            if offset not in mapping.flags or available < intent.quantity:
                raise RuntimeBlocked("close_requires_existing_unfrozen_position_in_verified_bucket")
        return (replace(intent, offset=offset, order_type=OrderType.LIMIT, limit_price_ticks=ticks),)


def _write_line(stream, value) -> None:
    stream.write(journal_codec.dumps(value) + "\n")
    stream.flush()


def _load_settings(path):
    values = live_assembly.load_settings(path)
    if (values.get("broker") or {}).get("profile") != "simnow_v6":
        raise RuntimeBlocked("settings_must_select_simnow_v6")
    return dict(values)


def pending_strategy_commands(engine, client, account) -> tuple[str, ...]:
    checkpoint = engine.checkpoint
    if checkpoint is None:
        return ()
    pending = []
    for command_id in checkpoint.pending_command_ids:
        queued = client.get(command_id)
        if queued is None:
            raise RuntimeBlocked("persisted_strategy_command_missing_from_account_queue")
        if queued.status in (CommandStatus.PENDING, CommandStatus.DISPATCHING):
            pending.append(command_id)
        elif queued.status == CommandStatus.SENT_UNKNOWN:
            payload = queued.command.payload
            if isinstance(payload, OrderIntent) and (
                account.is_order_active(payload.client_order_id)
                or account.order_identity(payload.client_order_id) is None
            ):
                pending.append(command_id)
    return tuple(pending)


def quote_timed_out(*, now: datetime, quote: Tick | None, sessions: Sequence[Session], started_at: datetime) -> bool:
    """仅在登记的连续撮合时段计时；启动及复市等待首条行情最多五秒。"""
    current = next(
        (
            row
            for row in sessions
            if row.contains(now) and row.phase == MarketPhase.CONTINUOUS and row.permissions.match
        ),
        None,
    )
    if current is None:
        return False
    last_observation = max(started_at, current.start)
    if quote is not None and quote.meta.trading_day == current.trading_day and current.contains(quote.meta.event_time):
        last_observation = max(last_observation, quote.meta.event_time)
    return now - last_observation > timedelta(seconds=5)


def run_runtime(args) -> tuple[dict[str, Any], int]:
    clock = RuntimeClock()
    now = clock.now()
    out = (ROOT / args.out).resolve()
    if not out.is_relative_to((ROOT / "runs").resolve()):
        raise RuntimeBlocked("runtime_output_must_stay_within_runs")
    out.mkdir(parents=True, exist_ok=True)
    preflight, profile, contract, catalog_path, day = load_runtime_inputs(ROOT / args.preflight, root=ROOT, now=now)
    instrument = contract.instrument
    config_path = ROOT / args.config
    config = load_validation_config(config_path)
    params = parameters_from_config(config, args.entry_mode)
    slippage_ticks = config["research"].get("slippage_ticks")
    if isinstance(slippage_ticks, bool) or not isinstance(slippage_ticks, int) or slippage_ticks < 0:
        raise RuntimeBlocked("explicit_nonnegative_slippage_ticks_required")
    config["data"]["catalog_path"] = str(catalog_path)
    report = {
        "kind": "simnow_strategy_runtime",
        "mode": args.mode,
        "entry_mode": args.entry_mode,
        "instrument": str(instrument),
        "trading_day": day.isoformat(),
        "interval": "30m",
        "generated_at": now.isoformat(),
        "status": "preparing",
        "blockers": [],
        "ticks_recorded": 0,
        "fresh_ticks": 0,
        "bars_recorded": 0,
        "commands_enqueued": 0,
        "quality_faults": [],
        "tick_file": (out / "ticks.jsonl").relative_to(ROOT).as_posix(),
        "bar_file": (out / "bars.jsonl").relative_to(ROOT).as_posix(),
    }
    calendar, sessions, aggregator = None, (), None
    if args.calendar:
        try:
            calendar = TradingCalendar.from_file(ROOT / args.calendar)
            sessions = calendar.sessions_for_day(instrument, day, known_at=now)
            aggregator = LiveBarAggregator(instrument=instrument, sessions=sessions, interval=timedelta(minutes=30))
        except Exception as exc:
            report["bar_blocker"] = f"explicit_calendar_unusable:{type(exc).__name__}"
    else:
        report["bar_blocker"] = "explicit_current_session_calendar_required; ticks_only"
    try:
        warmup = load_warmup_bars(config, root=ROOT, symbol=str(instrument), known_at=now)
    except Exception as exc:
        warmup = ()
        report["warmup_blocker"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    report["warmup_bars"] = len(warmup)
    funds = preflight.get("funds") or []
    try:
        capital = Decimal(str(funds[0]["balance"]))
        if not capital.is_finite() or capital <= 0:
            raise ValueError("invalid balance")
    except (IndexError, KeyError, ArithmeticError, ValueError):
        capital = Decimal(0)
        report["blockers"].append("counter_initial_capital_unavailable")
    economics = None
    close_mapping = verified_close_mapping(profile, instrument)
    if args.mode == "trade":
        blockers = report["blockers"]
        if not args.confirm_isolated:
            blockers.append("explicit_confirm_isolated_required")
        if preflight["runtime_inputs"]["front_market"].endswith(":40011"):
            blockers.append("simnow_7x24_replay_is_not_current_market")
        if not preflight.get("read_only_checks_passed"):
            blockers.append("preflight_read_only_checks_must_pass")
        if now - datetime.fromisoformat(preflight["generated_at"]) > timedelta(minutes=15):
            blockers.append("fresh_preflight_within_15_minutes_required")
        if not aggregator:
            blockers.append("explicit_current_session_calendar_required")
        elif not any(
            row.contains(now)
            and row.phase == MarketPhase.CONTINUOUS
            and row.permissions.submit
            and row.permissions.match
            for row in sessions
        ):
            blockers.append("currently_registered_trading_session_required")
        if len(warmup) < params.warmup_bars:
            blockers.append("trusted_current_30m_warmup_required")
        if close_mapping is None:
            blockers.append("verified_close_today_yesterday_mapping_required")
        try:
            preflight_hash = hashlib.sha256((ROOT / args.preflight).read_bytes()).hexdigest()
            economics = economics_from_preflight(preflight, contract, source=f"preflight:sha256:{preflight_hash}")
            report["economics_source"] = economics.source
        except RuntimeBlocked as exc:
            blockers.append(str(exc))
        if preflight.get("active_orders") or any(
            row.get("pos_td", 0) or row.get("pos_yd", 0) for row in preflight.get("positions", ())
        ):
            blockers.append("first_strategy_run_requires_flat_counter_and_no_active_orders")
        if blockers:
            report["status"] = "blocked_before_connection"
            return report, 2
    settings_path = ROOT / args.settings
    settings = _load_settings(settings_path)
    fronts = preflight["runtime_inputs"]
    broker = dict(settings.get("broker") or {})
    registered_account = profile.get("account") or {}
    user_id = broker.get("user_id") or registered_account.get("user_id") or broker.get("investor_id")
    investor_id = broker.get("investor_id") or registered_account.get("investor_id") or user_id
    broker.update(user_id=user_id, investor_id=investor_id)
    state_dir = out
    if args.mode == "trade":
        verify_configured_identity(preflight, user_id, investor_id)
        state_dir = account_state_directory(ROOT, user_id)
        report["account_state_directory"] = state_dir.relative_to(ROOT).as_posix()
        if (state_dir / "trading.db").exists():
            report["blockers"].append("existing_account_database_requires_explicit_recovery_workflow")
            report["status"] = "blocked_before_connection"
            return report, 2
    broker.update(
        front_trade_uri=fronts["front_trade"],
        front_market_uri=fronts["front_market"],
        flow_dir=str(state_dir / "ctp_flow"),
    )
    settings["broker"] = broker
    observed, committed = QueueSink(), Queue()
    quote_state = {"tick": None, "fault": None}
    assembled = None
    with ExitStack() as stack:
        if args.mode == "trade":
            settings["storage"] = dict(settings.get("storage") or {}) | {
                "journal_db_path": str(state_dir / "trading.db")
            }
            settings["risk"] = dict(settings.get("risk") or {}) | {"initial_capital": str(capital)}
            spec = live_assembly.spec_from_settings(
                settings,
                config_path=settings_path,
                mode="live",
                symbols=[str(instrument)],
                catalog_path=str(catalog_path),
                trading_day=day,
                controller_id=f"ema-{args.entry_mode}",
                heartbeat_path=str(state_dir / "heartbeat.json"),
                poll_interval=0.05,
            )
            assembled = live_assembly.assemble(spec, economics_override={instrument: economics})
            stack.callback(assembled.close)
            assembled.service.fact_observer = committed.put
            assembled.connect_counter()
            verify_counter_identity(preflight, assembled)
            report["counter_identity_verified"] = True
            request = assembled.request_control("single SimNow strategy runtime startup")
            assembled.take_over(request.command_id, assembled.isolation(operator_confirmed=args.confirm_isolated))
            assembled.reconcile_and_enable(expected_trading_day=day)
            market = assembled.market_gateway
            if market is None:
                raise RuntimeBlocked("assembled_simnow_market_gateway_required")
            account, client = CommittedAccountView(assembled.model, assembled.store), assembled.client

            def control():
                current = assembled.store.control()
                return None if current is None else current.epoch
        else:
            ctp_settings = ctp_setup.ctp_settings(
                profile,
                user_id=broker.get("user_id") or broker.get("investor_id"),
                investor_id=broker.get("investor_id"),
                front=fronts["front_trade"],
                flow_dir=out / "ctp_flow",
            )
            market = CtpMarketDataGateway(
                settings=CtpMarketSettings.from_settings(ctp_settings, front_market=fronts["front_market"]),
                events=observed,
                source_id="simnow-strategy-observe",
                timestamp_tolerance=timedelta(seconds=5),
            )
            stack.callback(market.close)
            account, client, control = FlatObservationView(capital), ObservationClient(), lambda: None

        def current_session(at):
            return next((row for row in sessions if row.contains(at) and row.phase == MarketPhase.CONTINUOUS), None)

        def ready():
            tick = quote_state["tick"]
            status = market.status()
            current = current_session(clock.now())
            return bool(
                args.mode == "trade"
                and quote_state["fault"] is None
                and tick is not None
                and timedelta(0) <= clock.now() - tick.meta.event_time <= timedelta(seconds=5)
                and tick.meta.trading_day == day
                and current
                and current.permissions.submit
                and current.permissions.match
                and status.get("connected")
                and status.get("logged_in")
                and not status.get("fault")
                and assembled.service.ready
                and assembled.counter_gateway.ready_to_send
            )

        translator = LimitOrderTranslator(
            instrument=instrument,
            price_tick=contract.price_tick,
            account=account,
            quote_provider=lambda: quote_state["tick"],
            clock=clock,
            slippage_ticks=slippage_ticks,
            close_mapping=close_mapping,
        )
        checkpoint_store = stack.enter_context(
            SQLiteStrategyCheckpointStore(state_dir / f"strategy_{args.entry_mode}.db")
        )
        config_version = hashlib.sha256(config_path.read_bytes() + args.entry_mode.encode()).hexdigest()
        engine = LiveEngine(
            account_id=str((settings.get("risk") or {}).get("account_id") or "simnow-observe"),
            producer_id=f"ema-{args.entry_mode}",
            strategy_id=f"ema-trend-{args.entry_mode}",
            config_version=config_version,
            instrument=instrument,
            interval="30m",
            command_client=client,
            account_view=account,
            checkpoint_store=CheckpointMappingStore(checkpoint_store),
            clock=clock,
            control_provider=control,
            ready_provider=ready,
            order_translator=translator,
            max_bar_age=timedelta(seconds=5),
            min_warmup_bars=params.warmup_bars if args.mode == "trade" else 0,
            equity_provider=account.equity,
        )
        strategy = EmaTrendStrategy(
            engine.strategy_id,
            engine,
            instrument,
            parameters=params,
            multiplier=contract.multiplier,
            price_tick=contract.price_tick,
            equity_provider=account.equity,
        )
        engine.attach_strategy(strategy)
        engine.warmup(warmup)
        engine.start()
        stack.callback(engine.stop)
        ticks_file = stack.enter_context((out / "ticks.jsonl").open("a", encoding="utf-8"))
        bars_file = stack.enter_context((out / "bars.jsonl").open("a", encoding="utf-8"))
        decisions_file = stack.enter_context((out / "strategy_events.jsonl").open("a", encoding="utf-8"))

        def pause(reason):
            if quote_state["fault"] is None:
                quote_state["fault"] = reason
                report["quality_faults"].append(reason)
                _write_line(decisions_file, {"kind": "runtime_paused", "reason": reason, "at": clock.now()})
                if aggregator is not None:
                    aggregator.mark_gap()
                if assembled is not None:
                    assembled.recovery.on_disconnected("strategy market data quality requires explicit reconciliation")

        def record_result(result):
            report["commands_enqueued"] += len(result.commands)
            _write_line(
                decisions_file,
                {
                    "at": result.bar_end,
                    "reason": result.reason,
                    "intents": result.intents,
                    "command_ids": tuple(command.command_id for command in result.commands),
                },
            )

        def accept_bar(bar):
            _write_line(bars_file, bar)
            report["bars_recorded"] += 1
            record_result(engine.on_bar(bar))

        def accept_event(event):
            payload = event.payload
            if isinstance(payload, Trade):
                engine.on_trade(payload)
            elif isinstance(payload, OrderUpdate):
                engine.on_order(payload)
            elif isinstance(payload, Tick):
                if payload.instrument != instrument:
                    pause("unexpected_market_instrument")
                    return
                _write_line(ticks_file, payload)
                report["ticks_recorded"] += 1
                observed_at = clock.now()
                market_counts = market.status().get("counts") or {}
                fresh = (
                    payload.meta.quality_flags == QualityFlag.OK
                    and payload.meta.trading_day == day
                    and timedelta(0) <= observed_at - payload.meta.event_time <= timedelta(seconds=5)
                    and payload.meta.available_at <= observed_at
                    and payload.last_price is not None
                    and not market_counts.get("timestamp_anomalies")
                    and not fronts["front_market"].endswith(":40011")
                )
                if not fresh:
                    pause("market_tick_is_not_verified_current_data")
                    return
                report["fresh_ticks"] += 1
                if aggregator is None or quote_state["fault"] is not None:
                    return
                before = aggregator.quality_gaps
                try:
                    bars = aggregator.on_tick(payload, now=clock.now())
                except LiveBarDataError:
                    pause("untrusted_market_tick")
                    return
                if aggregator.quality_gaps > before:
                    pause("bar_continuity_gap")
                    return
                session = current_session(payload.meta.event_time)
                if session is None:
                    pause("market_tick_outside_registered_session")
                    return
                quote_state["tick"] = replace(payload, phase=session.phase)
                record_result(engine.on_tick(quote_state["tick"]))
                for bar in bars:
                    accept_bar(bar)

        if assembled is None:
            market.connect()
            accepted = market.subscribe([instrument])
            if instrument.symbol not in accepted:
                raise RuntimeBlocked("selected_contract_subscription_not_confirmed")
        else:
            market_report = assembled.connect_market([instrument])
            if (
                not market_report
                or not market_report.get("available")
                or instrument.symbol not in market_report.get("subscribed", ())
            ):
                raise RuntimeBlocked("selected_contract_subscription_not_confirmed")
        report["status"] = "running"
        market_started_at = clock.now()
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            health = market.status()
            counts = health.get("counts") or {}
            if not health.get("connected") or not health.get("logged_in") or health.get("fault"):
                pause("market_connection_not_healthy")
            if any(counts.get(key, 0) for key in ("front_disconnected", "timestamp_anomalies", "conversion_failures")):
                pause("market_gateway_quality_gap")
            if observed.dropped or observed.errors:
                pause("market_observation_queue_gap")
            if assembled is not None:
                if not assembled.counter_gateway.ready_to_send:
                    pause("counter_session_not_ready")
                assembled.step(process_commands=False)
                source = committed
            else:
                source = observed.queue
            while True:
                try:
                    accept_event(source.get_nowait())
                except Empty:
                    break
            if assembled is not None and quote_timed_out(
                now=clock.now(), quote=quote_state["tick"], sessions=sessions, started_at=market_started_at
            ):
                pause("fresh_quote_timeout")
            if aggregator is not None and quote_state["fault"] is None:
                before = aggregator.quality_gaps
                for bar in aggregator.advance(clock.now()):
                    accept_bar(bar)
                if aggregator.quality_gaps > before:
                    pause("bar_continuity_timeout")
            for timer in clock.due():
                engine.on_timer(timer)
            if assembled is not None:
                if ready():
                    assembled.service.process_next_command()
                elif engine.checkpoint is not None and any(
                    (queued := client.get(command_id)) is not None
                    and queued.status in (CommandStatus.PENDING, CommandStatus.DISPATCHING)
                    for command_id in engine.checkpoint.pending_command_ids
                ):
                    pause("pending_strategy_command_requires_reconciliation")
            time.sleep(0.01)
        report["status"] = (
            "paused_on_quality_gap"
            if quote_state["fault"]
            else "completed_observation"
            if args.mode == "observe"
            else "completed_simulation_window"
        )
        report["strategy_ready"] = strategy.ready
        report["discarded_bars"] = 0 if aggregator is None else aggregator.discarded_bars
        report["final_position"] = account.get_position(instrument)
        report["active_orders_at_exit"] = account.has_active_orders(instrument)
        report["pending_strategy_commands"] = pending_strategy_commands(engine, client, account)
        if args.mode == "trade" and (
            report["final_position"] or report["active_orders_at_exit"] or report["pending_strategy_commands"]
        ):
            report["status"] = "operator_handoff_required"
            report["blockers"].append("counter_exposure_or_active_order_remains_at_runtime_deadline")
            return report, 3
        if report["fresh_ticks"] == 0:
            report["status"] = "no_market_data" if report["ticks_recorded"] == 0 else "no_verified_current_market_data"
            return report, 2
        return report, 2 if quote_state["fault"] else 0


def build_parser():
    parser = argparse.ArgumentParser(description="SimNow EMA 30m 策略观察/仿真入口")
    parser.add_argument("--preflight", required=True)
    parser.add_argument("--mode", choices=("observe", "trade"), default="observe")
    parser.add_argument("--confirm-isolated", action="store_true", help="trade必需：显式确认账户原交易连接已隔离")
    parser.add_argument("--entry-mode", choices=("A", "B"), default="A")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--calendar", help="显式覆盖当前合约与交易日的版本化 Session 日历")
    parser.add_argument("--config", default="config/strategy_validation.yaml")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--out", default="runs/live/ema_observe")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 < args.seconds <= 86400:
        raise ValueError("seconds must be positive and at most one day")
    out = (ROOT / args.out).resolve()
    if not out.is_relative_to((ROOT / "runs").resolve()):
        raise ValueError("runtime output must stay within runs")
    try:
        report, code = run_runtime(args)
    except Exception as exc:
        report, code = (
            {
                "kind": "simnow_strategy_runtime",
                "mode": args.mode,
                "status": "failed",
                "error_type": type(exc).__name__,
                "blockers": [str(exc)] if isinstance(exc, RuntimeBlocked) else [],
            },
            2 if isinstance(exc, RuntimeBlocked) else 1,
        )
    out.mkdir(parents=True, exist_ok=True)
    path = out / "runtime.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"exit_code": code, "status": report["status"], "evidence": path.relative_to(ROOT).as_posix()},
            ensure_ascii=False,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
