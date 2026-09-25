"""[Gateway 适配器] CTP 账户查询：资金、持仓、报单、成交与合约参数 (S5-01, FR-REC-04, A03/A04).

契约（04 §4.8 ``AccountQueryPort``）：每个结果都带请求批次与完成标志；空记录或半结算的应答不得自行
升级为一致账户快照。请求与应答按 ``RequestID`` 关联，``bIsLast`` 才表示查询流结束。

口径与缺口：

- 只映射柜台直接给出的字段（``Balance`` / ``Available`` / ``CurrMargin``）；不自行推导口径未核验的
  ``equity``，缺项留 ``None`` 而不是 0 (FR-LED-05 口径核验待联调)。
- 持仓只接受投机 (``HedgeFlag='1'``) 且方向明确的记录；净持仓 (``PosiDirection='1'``) 或套保仓位
  无法用当前 ``Position`` 表达，记录为不支持并把结果标为不完整，宁可阻塞放行也不猜。
- 报单查询复用 S5-02 归一化器；完成标志与错误码直接决定对账水位，不完整查询不能放行交易。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from qh_trader.core.constants import Exchange, OrderStatus, PositionSide
from qh_trader.core.objects import (
    AccountFunds,
    InstrumentId,
    OrderUpdate,
    Position,
    QueryBatch,
    QueryRateLimit,
    QueryResult,
    Trade,
    require_text,
)
from qh_trader.core.ports import AccountQueryPort, FeedbackNormalizerPort

LOGGER = logging.getLogger(__name__)

QUERY_TIMEOUT_CODE = -1001
QUERY_TIMEOUT_MESSAGE = "query did not complete before its deadline"
QUERY_LOCAL_REJECT_CODE = -1002
QUERY_UNSUPPORTED_CODE = -1003
QUERY_TRANSPORT_CODE = -1004

HEDGE_SPECULATION = "1"
POSI_DIRECTION_LONG = "2"
POSI_DIRECTION_SHORT = "3"
POSITION_DATE_TODAY = "1"
POSITION_DATE_HISTORY = "2"
TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED})

FIELD_BY_KIND = {
    "account": "CThostFtdcQryTradingAccountField",
    "position": "CThostFtdcQryInvestorPositionField",
    "order": "CThostFtdcQryOrderField",
    "trade": "CThostFtdcQryTradeField",
    "instrument": "CThostFtdcQryInstrumentField",
    "depth": "CThostFtdcQryDepthMarketDataField",
    "settlement_confirm": "CThostFtdcQrySettlementInfoConfirmField",
}
REQUEST_BY_KIND = {
    "account": "ReqQryTradingAccount",
    "position": "ReqQryInvestorPosition",
    "order": "ReqQryOrder",
    "trade": "ReqQryTrade",
    "instrument": "ReqQryInstrument",
    "depth": "ReqQryDepthMarketData",
    "settlement_confirm": "ReqQrySettlementInfoConfirm",
}
SOURCE_ID = "ctp"


class CtpQueryError(RuntimeError):
    """查询无法建立或应答无法解析；调用方按不完整结果处置."""


@runtime_checkable
class CtpRequestChannel(Protocol):
    """网关暴露的请求通道；查询适配器不直接触碰绑定对象类型."""

    def next_request_id(self) -> int: ...
    def new_field(self, type_name: str) -> Any: ...
    def send_request(self, name: str, field: Any | None, request_id: int) -> int: ...


@dataclass
class _Pending:
    kind: str
    batch: QueryBatch
    deadline: float
    records: list[Mapping[str, object]] = field(default_factory=list)
    error: tuple[int, str] | None = None
    done: threading.Event = field(default_factory=threading.Event)


class CtpQueryAdapter(AccountQueryPort):
    """``AccountQueryPort`` 的 CTP 实现；应答由网关回调线程经 :meth:`on_query_response` 送入."""

    def __init__(
        self,
        *,
        account_id: str,
        channel: CtpRequestChannel,
        normalizer: FeedbackNormalizerPort,
        investor_id: str,
        broker_id: str,
        trading_day: Callable[[], date | None] | None = None,
        interval_ms: int = 1000,
        timeout_s: float = 15.0,
        source_version: str | Callable[[], str] = "ctp-6.7",
        wall_time: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        require_text(account_id, "account_id")
        require_text(investor_id, "investor_id")
        require_text(broker_id, "broker_id")
        if interval_ms < 0:
            raise ValueError("query interval cannot be negative")
        self.account_id = account_id
        self.investor_id = investor_id
        self.broker_id = broker_id
        self._channel = channel
        self._normalizer = normalizer
        self._interval_ms = int(interval_ms)
        self._timeout_s = float(timeout_s)
        self._source_version = source_version if callable(source_version) else (lambda: source_version)
        self._trading_day = trading_day or (lambda: None)
        self._wall_time = wall_time or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._last_request_at: float | None = None
        self._max_evidence = 50
        self.counts = {
            "requests": 0,
            "records": 0,
            "timeouts": 0,
            "local_rejections": 0,
            "unmatched_responses": 0,
            "unsupported_records": 0,
            "conversion_failures": 0,
        }
        self.evidence: list[Mapping[str, object]] = []

    def _record_evidence(self, entry: Mapping[str, object]) -> None:
        self.evidence.append(entry)
        if len(self.evidence) > self._max_evidence:
            del self.evidence[: len(self.evidence) - self._max_evidence]

    # ------------------------------------------------------------------ 网关回调入口
    def on_query_response(
        self,
        *,
        kind: str,
        request_id: int,
        records: tuple[Mapping[str, object], ...],
        is_last: bool,
        error_code: int | None,
        error_message: str | None,
    ) -> None:
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            self.counts["unmatched_responses"] += 1
            return
        if records:
            pending.records.extend(records)
        if error_code:
            pending.error = (error_code, error_message or "")
        if error_code or is_last:
            pending.done.set()

    def on_query_error(self, *, request_id: int, error_code: int, error_message: str) -> None:
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            self.counts["unmatched_responses"] += 1
            return
        pending.error = (error_code, error_message)
        pending.done.set()

    # ------------------------------------------------------------------ 端口
    def rate_limit(self) -> QueryRateLimit:
        return QueryRateLimit(interval_ms=self._interval_ms, max_in_flight=1)

    def query_account(self, batch: QueryBatch) -> QueryResult[AccountFunds]:
        records, error = self._request("account", batch)
        funds: list[AccountFunds] = []
        skipped: list[str] = []
        for record in records:
            try:
                funds.append(self._funds(record))
            except CtpQueryError as exc:
                skipped.append(str(exc))
                self.counts["conversion_failures"] += 1
        return self._result("account", batch, tuple(funds), error, skipped)

    def query_positions(self, batch: QueryBatch) -> QueryResult[Position]:
        records, error = self._request("position", batch)
        positions: dict[tuple[InstrumentId, PositionSide], list[int]] = {}
        hedges: dict[tuple[InstrumentId, PositionSide], str] = {}
        skipped: list[str] = []
        for record in records:
            try:
                instrument, side, hedge, pos_yd, pos_td, frozen = self._position(record)
            except CtpQueryError as exc:
                skipped.append(str(exc))
                self.counts["conversion_failures"] += 1
                continue
            key = (instrument, side)
            bucket = positions.setdefault(key, [0, 0, 0, 0])
            bucket[0] += pos_yd
            bucket[1] += pos_td
            hedges[key] = hedge
            # 冻结量按今仓优先归集（平今冻结最常见），剩下放不下的部分记异常：
            # 不丢弃整条记录（否则持仓比对看不到柜台持仓），也不静默改写数量。
            remaining = frozen
            taken_td = min(remaining, pos_td)
            remaining -= taken_td
            taken_yd = min(remaining, pos_yd)
            remaining -= taken_yd
            bucket[2] += taken_yd
            bucket[3] += taken_td
            if remaining:
                skipped.append(
                    f"{instrument} {side.value}: counter froze {frozen} while reporting {pos_yd + pos_td} position"
                )
        mapped = tuple(
            Position(
                instrument=instrument,
                side=side,
                hedge_flag=hedges[(instrument, side)],
                pos_yd=bucket[0],
                pos_td=bucket[1],
                frozen_yd=bucket[2],
                frozen_td=bucket[3],
            )
            for (instrument, side), bucket in sorted(positions.items(), key=lambda item: str(item[0]))
        )
        return self._result("position", batch, mapped, error, skipped)

    def query_orders(self, batch: QueryBatch) -> QueryResult[OrderUpdate]:
        records, error = self._request("order", batch)
        updates: list[OrderUpdate] = []
        skipped: list[str] = []
        for record in records:
            try:
                event = self._normalizer.normalize_order(record, self._wall_time())
            except Exception as exc:  # 归一化失败必须让结果不完整，不能只丢一条报单
                skipped.append(f"order record could not be normalized ({type(exc).__name__})")
                self.counts["conversion_failures"] += 1
                continue
            if event is None:
                skipped.append("order record could not be represented")
                continue
            update = event.payload
            if isinstance(update, OrderUpdate) and update.status not in TERMINAL_STATUSES:
                updates.append(update)
        return self._result("order", batch, tuple(updates), error, skipped)

    def query_trades(self, batch: QueryBatch) -> QueryResult[Trade]:
        records, error = self._request("trade", batch)
        trades: dict[str, Trade] = {}
        skipped: list[str] = []
        for record in records:
            try:
                event = self._normalizer.normalize_trade(record, self._wall_time())
            except Exception as exc:
                skipped.append(f"trade record could not be normalized ({type(exc).__name__})")
                self.counts["conversion_failures"] += 1
                continue
            if event is None:
                skipped.append("trade record could not be represented")
                continue
            trade = event.payload
            if isinstance(trade, Trade):
                trades.setdefault(trade.trade_id, trade)
        return self._result("trade", batch, tuple(trades.values()), error, skipped)

    # ------------------------------------------------------------------ 合约参数（探测脚本与规则核验用）
    def query_instrument(self, symbol: str) -> Mapping[str, object] | None:
        """查询单个合约的官方参数（乘数 / 最小变动 / 到期日），供 FR-RULE-05 证据登记.

        柜台没有用过滤条件、或过滤条件不被支持时可能返回别的合约，因此回包必须与请求的合约一致。
        """
        require_text(symbol, "instrument symbol")
        batch = self.query_batch("instrument")
        records = self._request_raw("instrument", batch, {"InstrumentID": symbol})
        matching = [record for record in records if str(record.get("InstrumentID")) == symbol]
        if records and not matching:
            raise CtpQueryError("counter answered an instrument query with a different instrument")
        return None if not matching else dict(matching[0])

    def query_depth(self, symbol: str) -> Mapping[str, object] | None:
        """查询柜台最新行情快照；用于探测脚本取可核验的价格参考（不是行情订阅通道）.

        SimNow 7x24 环境实测 `ReqQryDepthMarketData` 不遵守 InstrumentID 过滤（请求 rb2610 返回
        rb2610P3300），因此只接受与请求合约一致的回包；不一致即失败，绝不用它推导委托价格。
        """
        require_text(symbol, "instrument symbol")
        batch = self.query_batch("depth")
        records = self._request_raw("depth", batch, {"InstrumentID": symbol})
        matching = [record for record in records if str(record.get("InstrumentID")) == symbol]
        if records and not matching:
            raise CtpQueryError("counter answered a depth query with another instrument; the quote must not be used")
        return None if not matching else dict(matching[0])

    # ------------------------------------------------------------------ 内部
    def query_batch(self, kind: str) -> QueryBatch:
        """显式构造查询批次；探测脚本与对账都按同一批次口径记录完成标志."""
        day = self._trading_day()
        if day is None:
            raise CtpQueryError("queries require the counter's trading day; it is not known yet")
        batch_id = "ctp-" + kind + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return QueryBatch(batch_id, self.account_id, day, self._wall_time())

    def _throttle(self) -> None:
        if self._interval_ms <= 0:
            return
        now = self._monotonic()
        if self._last_request_at is not None:
            wait = (self._interval_ms / 1000.0) - (now - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._monotonic()

    def _request(self, kind: str, batch: QueryBatch) -> tuple[list[Mapping[str, object]], tuple[int, str] | None]:
        return self._collect(kind, batch, {})

    def _request_raw(self, kind: str, batch: QueryBatch, extra: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
        records, error = self._collect(kind, batch, extra)
        if error is not None:
            raise CtpQueryError(error[1] or "query failed")
        return records

    def _collect(
        self, kind: str, batch: QueryBatch, extra: Mapping[str, object]
    ) -> tuple[list[Mapping[str, object]], tuple[int, str] | None]:
        self._throttle()
        request_id = self._channel.next_request_id()
        pending = _Pending(kind=kind, batch=batch, deadline=self._monotonic() + self._timeout_s)
        try:
            field: Any = self._channel.new_field(FIELD_BY_KIND[kind])
            field.BrokerID = self.broker_id
            field.InvestorID = self.investor_id
            for name, value in extra.items():
                setattr(field, name, value)
        except (AttributeError, TypeError, ValueError) as exc:
            return [], (QUERY_TRANSPORT_CODE, f"query field could not be prepared ({type(exc).__name__})")
        with self._lock:
            self._pending[request_id] = pending
        self.counts["requests"] += 1
        try:
            code = int(self._channel.send_request(REQUEST_BY_KIND[kind], field, request_id))
        except Exception as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            return [], (QUERY_TRANSPORT_CODE, f"query request failed locally ({type(exc).__name__})")
        finally:
            try:
                field.thisown = True
            except Exception:  # pragma: no cover - 绑定差异
                pass
        if code != 0:
            with self._lock:
                self._pending.pop(request_id, None)
            self.counts["local_rejections"] += 1
            self._record_evidence({"kind": kind, "request_id": request_id, "local_code": code})
            return [], (QUERY_LOCAL_REJECT_CODE, f"counter API rejected {REQUEST_BY_KIND[kind]} locally (code={code})")
        if not pending.done.wait(self._timeout_s):
            with self._lock:
                self._pending.pop(request_id, None)
            self.counts["timeouts"] += 1
            return [], (QUERY_TIMEOUT_CODE, QUERY_TIMEOUT_MESSAGE)
        with self._lock:
            self._pending.pop(request_id, None)
        self.counts["records"] += len(pending.records)
        return list(pending.records), pending.error

    def _result(
        self,
        kind: str,
        batch: QueryBatch,
        records: tuple,
        error: tuple[int, str] | None,
        skipped: Sequence[str],
    ) -> QueryResult:
        complete = error is None and not skipped
        error_code = None if error is None else error[0]
        if skipped:
            error_code = QUERY_UNSUPPORTED_CODE if error_code is None else error_code
            self.counts["unsupported_records"] += len(skipped)
            self._record_evidence({"kind": kind, "skipped": list(skipped)[:5]})
            LOGGER.warning("CTP %s query has %d unusable record(s); the result stays incomplete", kind, len(skipped))
        if error is not None:
            LOGGER.warning("CTP %s query failed: %s", kind, error[1] or error[0])
        return QueryResult(
            batch=batch,
            records=records,
            available_at=self._wall_time(),
            source_id=SOURCE_ID,
            source_version=self._source_version(),
            complete=complete,
            error_code=error_code,
        )

    @staticmethod
    def _funds(record: Mapping[str, object]) -> AccountFunds:
        def value(name: str) -> Decimal | None:
            raw = record.get(name)
            if raw is None or raw == "":
                return None
            return Decimal(str(raw))

        balance = value("Balance")
        if balance is None:
            raise CtpQueryError("account record carries no balance; a missing balance is never read as zero")
        return AccountFunds(
            balance=balance,
            equity=None,  # CTP 未给出可直接使用的权益口径；不自行推导 (FR-LED-05 待联调核验)
            margin=value("CurrMargin"),
            available_for_new_trades=value("Available"),
        )

    @staticmethod
    def _position(record: Mapping[str, object]) -> tuple[InstrumentId, PositionSide, str, int, int, int]:
        """按 ``PositionDate`` 归类单条持仓记录；无法表达的记录明确失败 (宁可阻塞放行也不猜).

        每条记录只取柜台给出的 ``Position`` 总量并归到今日 / 昨仓，不在本地重算今昨仓分配。
        """
        exchange = record.get("ExchangeID")
        symbol = record.get("InstrumentID")
        direction = record.get("PosiDirection")
        hedge = record.get("HedgeFlag")
        position_date = record.get("PositionDate")
        if not isinstance(exchange, str) or not isinstance(symbol, str) or not exchange or not symbol:
            raise CtpQueryError("position record is missing its instrument or exchange")
        if direction not in (POSI_DIRECTION_LONG, POSI_DIRECTION_SHORT):
            raise CtpQueryError(f"position direction {direction!r} cannot be represented as strict long/short")
        if hedge != HEDGE_SPECULATION:
            raise CtpQueryError(f"hedge flag {hedge!r} is outside the enabled speculation scope")
        try:
            instrument = InstrumentId(Exchange(exchange), symbol)
        except ValueError as exc:
            raise CtpQueryError(f"unregistered exchange {exchange!r} in a position record") from exc
        side = PositionSide.LONG if direction == POSI_DIRECTION_LONG else PositionSide.SHORT
        total = _as_int(record.get("Position"))
        frozen = _as_int(record.get("LongFrozen") if side == PositionSide.LONG else record.get("ShortFrozen"))
        if position_date == POSITION_DATE_TODAY:
            pos_yd, pos_td = 0, total
        elif position_date == POSITION_DATE_HISTORY:
            pos_yd, pos_td = total, 0
        else:
            raise CtpQueryError(f"position record has no usable PositionDate ({position_date!r})")
        return instrument, side, hedge, pos_yd, pos_td, frozen


def _as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "QUERY_LOCAL_REJECT_CODE",
    "QUERY_TIMEOUT_CODE",
    "QUERY_TRANSPORT_CODE",
    "QUERY_UNSUPPORTED_CODE",
    "CtpQueryAdapter",
    "CtpQueryError",
    "CtpRequestChannel",
]
