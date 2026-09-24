"""[Data 适配器] 期货公司结算单解析与本地账本比对 (S5-08, FR-LED-08, A22).

结算单是权威口径，柜台实时查询只是中间状态。本模块：

1. 解析监控中心 (CFMMC) 文本结算单常见布局 (``资金状况`` 汇总、``成交记录``、``平仓明细``、``持仓汇总``)，
   并接受同结构的 JSON 规范化输入，便于在拿到目标期货公司实际结算单前先验证比对逻辑；
2. 把结算单与本地账本口径 (``LocalDayFigures``，由入口层从账本提取) 逐项比对：
   期末结存、当日盯市平仓盈亏、持仓盯市盈亏、手续费、保证金占用、逐合约持仓手数；
3. 差异超过约定误差即为阻塞差异 (FR-LED-08 硬约束：禁止新增风险，直到人工确认或更正事件入账)。

文本布局按公开的监控中心日结算单模板实现，尚未用目标期货公司实际结算单核验；无法识别的布局明确失败，
不猜测字段含义。金额一律 ``Decimal``，按记账单位比较。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from qh_trader.core.constants import Exchange, PositionSide
from qh_trader.core.objects import InstrumentId, require_text

STATEMENT_KIND_MTM = "MTM"  # 逐日盯市
STATEMENT_KIND_TRADE = "TRADE"  # 逐笔对冲


class StatementFormatError(ValueError):
    """结算单布局或字段无法识别；不猜测."""


@dataclass(frozen=True, slots=True)
class StatementSummary:
    """资金状况；缺失字段为 None，比对时按“不可比”而非 0 处理."""

    balance_start: Decimal | None
    deposit_withdrawal: Decimal | None
    close_pnl: Decimal | None
    mtm_pnl: Decimal | None
    commission: Decimal | None
    balance_end: Decimal | None
    margin: Decimal | None
    available: Decimal | None


@dataclass(frozen=True, slots=True)
class StatementPosition:
    instrument: InstrumentId
    side: PositionSide
    quantity: int
    average_price: Decimal | None = None
    settlement_price: Decimal | None = None
    margin: Decimal | None = None


@dataclass(frozen=True, slots=True)
class StatementTrade:
    trade_id: str
    instrument: InstrumentId
    side: str
    offset: str
    price: Decimal
    quantity: int
    commission: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SettlementStatement:
    account_id: str
    trading_day: date
    kind: str
    summary: StatementSummary
    positions: tuple[StatementPosition, ...] = ()
    trades: tuple[StatementTrade, ...] = ()
    source: str = ""

    def __post_init__(self) -> None:
        require_text(self.account_id, "account_id")
        if self.kind not in (STATEMENT_KIND_MTM, STATEMENT_KIND_TRADE):
            raise StatementFormatError(f"unknown statement kind {self.kind!r}")


@dataclass(frozen=True, slots=True)
class StatementDiff:
    item: str
    statement_value: object
    local_value: object
    tolerance: Decimal
    blocking: bool
    note: str = ""


@dataclass(frozen=True, slots=True)
class StatementReconciliation:
    account_id: str
    trading_day: date
    kind: str
    diffs: tuple[StatementDiff, ...]
    compared: tuple[str, ...]
    skipped: tuple[str, ...] = ()

    @property
    def blocking(self) -> tuple[StatementDiff, ...]:
        return tuple(diff for diff in self.diffs if diff.blocking)

    @property
    def consistent(self) -> bool:
        return not self.blocking


# ---------------------------------------------------------------------- 解析：JSON 规范化输入

_EXCHANGE_ALIASES = {
    "SHFE": Exchange.SHFE,
    "上海期货交易所": Exchange.SHFE,
    "上期所": Exchange.SHFE,
    "DCE": Exchange.DCE,
    "大连商品交易所": Exchange.DCE,
    "大商所": Exchange.DCE,
    "CZCE": Exchange.CZCE,
    "郑州商品交易所": Exchange.CZCE,
    "郑商所": Exchange.CZCE,
    "CFFEX": Exchange.CFFEX,
    "中国金融期货交易所": Exchange.CFFEX,
    "中金所": Exchange.CFFEX,
    "INE": Exchange.INE,
    "上海国际能源交易中心": Exchange.INE,
    "能源中心": Exchange.INE,
}

_SIDE_ALIASES = {
    "买": PositionSide.LONG,
    "多": PositionSide.LONG,
    "LONG": PositionSide.LONG,
    "BUY": PositionSide.LONG,
    "卖": PositionSide.SHORT,
    "空": PositionSide.SHORT,
    "SHORT": PositionSide.SHORT,
    "SELL": PositionSide.SHORT,
}


def _money(value: object, name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise StatementFormatError(f"{name} is not a decimal: {value!r}") from exc
    if not number.is_finite():
        raise StatementFormatError(f"{name} is not finite")
    return number


def _required_money(value: object, name: str) -> Decimal:
    number = _money(value, name)
    if number is None:
        raise StatementFormatError(f"{name} is missing")
    return number


def _exchange(value: object) -> Exchange:
    text = str(value).strip()
    exchange = _EXCHANGE_ALIASES.get(text) or _EXCHANGE_ALIASES.get(text.upper())
    if exchange is None:
        raise StatementFormatError(f"unknown exchange {text!r}")
    return exchange


def _side(value: object) -> PositionSide:
    text = str(value).strip()
    side = _SIDE_ALIASES.get(text) or _SIDE_ALIASES.get(text.upper())
    if side is None:
        raise StatementFormatError(f"unknown position side {text!r}")
    return side


def statement_from_mapping(data: Mapping[str, object], *, source: str = "json") -> SettlementStatement:
    """JSON 规范化结算单：{account_id, trading_day, kind, summary{...}, positions[...], trades[...]}."""
    try:
        summary_data = data["summary"]
        assert isinstance(summary_data, Mapping)
        position_rows: Sequence[Mapping[str, Any]] = data.get("positions", ())  # type: ignore[assignment]
        trade_rows: Sequence[Mapping[str, Any]] = data.get("trades", ())  # type: ignore[assignment]
        summary = StatementSummary(
            balance_start=_money(summary_data.get("balance_start"), "balance_start"),
            deposit_withdrawal=_money(summary_data.get("deposit_withdrawal"), "deposit_withdrawal"),
            close_pnl=_money(summary_data.get("close_pnl"), "close_pnl"),
            mtm_pnl=_money(summary_data.get("mtm_pnl"), "mtm_pnl"),
            commission=_money(summary_data.get("commission"), "commission"),
            balance_end=_money(summary_data.get("balance_end"), "balance_end"),
            margin=_money(summary_data.get("margin"), "margin"),
            available=_money(summary_data.get("available"), "available"),
        )
        positions = tuple(
            StatementPosition(
                instrument=InstrumentId(_exchange(row["exchange"]), str(row["symbol"])),
                side=_side(row["side"]),
                quantity=int(row["quantity"]),
                average_price=_money(row.get("average_price"), "average_price"),
                settlement_price=_money(row.get("settlement_price"), "settlement_price"),
                margin=_money(row.get("margin"), "margin"),
            )
            for row in position_rows
        )
        trades = tuple(
            StatementTrade(
                trade_id=str(row["trade_id"]),
                instrument=InstrumentId(_exchange(row["exchange"]), str(row["symbol"])),
                side=str(row["side"]),
                offset=str(row["offset"]),
                price=_required_money(row["price"], "price"),
                quantity=int(row["quantity"]),
                commission=_money(row.get("commission"), "commission"),
            )
            for row in trade_rows
        )
        return SettlementStatement(
            account_id=str(data["account_id"]),
            trading_day=date.fromisoformat(str(data["trading_day"])),
            kind=str(data.get("kind", STATEMENT_KIND_MTM)).upper(),
            summary=summary,
            positions=positions,
            trades=trades,
            source=source,
        )
    except (KeyError, TypeError, AssertionError, ValueError) as exc:
        if isinstance(exc, StatementFormatError):
            raise
        raise StatementFormatError(f"invalid normalized statement: {exc}") from exc


# ---------------------------------------------------------------------- 解析：监控中心文本布局

_SUMMARY_FIELDS = {
    "期初结存": "balance_start",
    "出 入 金": "deposit_withdrawal",
    "出入金": "deposit_withdrawal",
    "平仓盈亏": "close_pnl",
    "持仓盯市盈亏": "mtm_pnl",
    "盯市盈亏": "mtm_pnl",
    "手 续 费": "commission",
    "手续费": "commission",
    "期末结存": "balance_end",
    "保证金占用": "margin",
    "可用资金": "available",
}
_SECTION_POSITIONS = re.compile(r"持仓汇总|Positions\b", re.I)
_SECTION_TRADES = re.compile(r"成交记录|Transaction Record", re.I)
_SECTION_END = re.compile(r"^\s*-{5,}\s*$")
_ACCOUNT = re.compile(r"客户号\s*Client ID[：:]?\s*(\S+)", re.I)
_DAY = re.compile(r"日期\s*Date[：:]?\s*(\d{8})", re.I)
_KIND = re.compile(r"(逐日盯市|逐笔对冲)")


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _summary_from_text(text: str) -> StatementSummary:
    values: dict[str, Decimal | None] = {}
    for label, key in _SUMMARY_FIELDS.items():
        pattern = re.compile(re.escape(label) + r"\s*[A-Za-z/ .()]*[：:]\s*(-?[\d,]+\.\d+|-?[\d,]+)")
        found = pattern.search(text)
        if found and key not in values:
            values[key] = _money(found.group(1), key)
    return StatementSummary(
        balance_start=values.get("balance_start"),
        deposit_withdrawal=values.get("deposit_withdrawal"),
        close_pnl=values.get("close_pnl"),
        mtm_pnl=values.get("mtm_pnl"),
        commission=values.get("commission"),
        balance_end=values.get("balance_end"),
        margin=values.get("margin"),
        available=values.get("available"),
    )


def _table_rows(lines: Sequence[str], start: int) -> tuple[list[str], list[list[str]]]:
    """读取一个表格段：首个 ``|`` 行为中文表头，紧随的英文表头行 (无数字单元格) 合并进表头，其余为数据行."""
    header: list[str] | None = None
    rows: list[list[str]] = []
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if _SECTION_END.match(line) and header is not None and rows:
            break
        if "|" in line:
            cells = _split_row(line)
            if header is None:
                header = cells
            elif not rows and len(cells) == len(header) and not any(re.search(r"\d", cell) for cell in cells):
                header = [f"{cn} {en}".strip() for cn, en in zip(header, cells, strict=True)]
            elif any(cells) and not all(re.fullmatch(r"-*", cell) for cell in cells):
                if cells[0].startswith(("共", "合计", "Total")):
                    break
                rows.append(cells)
        index += 1
    if header is None:
        raise StatementFormatError("table section has no header row")
    return header, rows


def _column(header: Sequence[str], *names: str) -> int:
    for position, cell in enumerate(header):
        for name in names:
            if name in cell:
                return position
    raise StatementFormatError(f"table lacks column {names[0]!r}")


def statement_from_text(text: str, *, source: str = "text") -> SettlementStatement:
    """解析监控中心文本结算单；缺少客户号、日期或资金状况即明确失败."""
    account = _ACCOUNT.search(text)
    day = _DAY.search(text)
    if account is None or day is None:
        raise StatementFormatError("statement lacks the client id or date header")
    kind_match = _KIND.search(text)
    kind = STATEMENT_KIND_TRADE if kind_match and kind_match.group(1) == "逐笔对冲" else STATEMENT_KIND_MTM
    summary = _summary_from_text(text)
    if summary.balance_end is None:
        raise StatementFormatError("statement lacks the account summary (期末结存)")
    lines = text.splitlines()
    positions: list[StatementPosition] = []
    trades: list[StatementTrade] = []
    for index, line in enumerate(lines):
        if _SECTION_POSITIONS.search(line) and "|" not in line and "持仓明细" not in line:
            header, rows = _table_rows(lines, index)
            symbol_col = _column(header, "合约", "Instrument")
            long_col = _column(header, "买持", "Long Pos")
            short_col = _column(header, "卖持", "Short Pos")
            exchange_col = next((i for i, cell in enumerate(header) if "交易所" in cell or "Exchange" in cell), None)
            for row in rows:
                symbol = row[symbol_col]
                exchange = _exchange(row[exchange_col]) if exchange_col is not None else _exchange_from_symbol(symbol)
                instrument = InstrumentId(exchange, symbol)
                for column, side in ((long_col, PositionSide.LONG), (short_col, PositionSide.SHORT)):
                    quantity = int(row[column] or 0)
                    if quantity:
                        positions.append(StatementPosition(instrument, side, quantity))
        elif _SECTION_TRADES.search(line) and "|" not in line:
            header, rows = _table_rows(lines, index)
            symbol_col = _column(header, "合约", "Instrument")
            side_col = _column(header, "买/卖", "B/S")
            offset_col = _column(header, "开平", "O/C")
            price_col = _column(header, "成交价", "Price")
            qty_col = _column(header, "手数", "Lots")
            id_col = _column(header, "成交序号", "Trans.No")
            fee_col = next((i for i, cell in enumerate(header) if "手续费" in cell or "Fee" in cell), None)
            exchange_col = next((i for i, cell in enumerate(header) if "交易所" in cell or "Exchange" in cell), None)
            for row in rows:
                symbol = row[symbol_col]
                exchange = _exchange(row[exchange_col]) if exchange_col is not None else _exchange_from_symbol(symbol)
                trades.append(
                    StatementTrade(
                        trade_id=row[id_col],
                        instrument=InstrumentId(exchange, symbol),
                        side=row[side_col],
                        offset=row[offset_col],
                        price=_required_money(row[price_col], "price"),
                        quantity=int(row[qty_col]),
                        commission=_money(row[fee_col], "commission") if fee_col is not None else None,
                    )
                )
    return SettlementStatement(
        account_id=account.group(1),
        trading_day=date(int(day.group(1)[:4]), int(day.group(1)[4:6]), int(day.group(1)[6:])),
        kind=kind,
        summary=summary,
        positions=tuple(positions),
        trades=tuple(trades),
        source=source,
    )


def _exchange_from_symbol(symbol: str) -> Exchange:
    raise StatementFormatError(f"statement row for {symbol!r} does not name its exchange; cannot infer")


def load_statement(path: Path | str) -> SettlementStatement:
    file = Path(path)
    text = file.read_text(encoding="utf-8-sig")
    if file.suffix.lower() == ".json":
        data = json.loads(text)
        if not isinstance(data, Mapping):
            raise StatementFormatError("normalized statement must be a JSON object")
        return statement_from_mapping(data, source=str(file))
    return statement_from_text(text, source=str(file))


# ---------------------------------------------------------------------- 比对


@dataclass(frozen=True, slots=True)
class LocalDayFigures:
    """本地账本在结算单交易日的口径；由入口层 ``scripts.live_assembly.local_day_figures`` 从账本提取."""

    balance_end: Decimal
    close_pnl: Decimal
    mtm_pnl: Decimal
    commission: Decimal
    margin: Decimal | None
    positions: Mapping[tuple[InstrumentId, PositionSide], int] | None
    cash_flow: Decimal


def reconcile_statement(
    statement: SettlementStatement,
    local: LocalDayFigures,
    *,
    tolerance: Decimal = Decimal("0.01"),
    margin_tolerance: Decimal | None = None,
) -> StatementReconciliation:
    """逐项比对；结算单缺失的项记为跳过，不当作一致."""
    require_decimal_nonnegative(tolerance)
    diffs: list[StatementDiff] = []
    compared: list[str] = []
    skipped: list[str] = []

    def compare(item: str, remote: Decimal | None, mine: Decimal | None, tol: Decimal, note: str = "") -> None:
        if remote is None or mine is None:
            skipped.append(item)
            return
        compared.append(item)
        if abs(remote - mine) > tol:
            diffs.append(StatementDiff(item, remote, mine, tol, True, note))

    summary = statement.summary
    compare("balance_end", summary.balance_end, local.balance_end, tolerance, "期末结存 vs 账本余额")
    if statement.kind == STATEMENT_KIND_MTM:
        compare("close_pnl", summary.close_pnl, local.close_pnl, tolerance, "盯市平仓盈亏 (mtm_close_pnl)")
        compare("mtm_pnl", summary.mtm_pnl, local.mtm_pnl, tolerance, "持仓盯市盈亏 (结算计提)")
    else:
        skipped.extend(("close_pnl", "mtm_pnl"))
    compare("commission", summary.commission, local.commission, tolerance, "手续费")
    compare(
        "margin",
        summary.margin,
        local.margin,
        margin_tolerance if margin_tolerance is not None else tolerance,
        "保证金占用 (本地按估值口径)",
    )
    if summary.deposit_withdrawal is not None:
        compare("deposit_withdrawal", summary.deposit_withdrawal, local.cash_flow, tolerance, "出入金")

    # 同一合约同一方向可能分行列示 (投机 / 套保等)，按手数累加而非覆盖
    remote_positions: dict[tuple[InstrumentId, PositionSide], int] = {}
    for row in statement.positions:
        key = (row.instrument, row.side)
        remote_positions[key] = remote_positions.get(key, 0) + row.quantity
    if local.positions is None:
        skipped.append("positions")
        remote_positions = {}
    local_positions = local.positions or {}
    for key in sorted(set(remote_positions) | set(local_positions), key=lambda k: (str(k[0]), k[1].value)):
        item = f"position:{key[0]}:{key[1].value}"
        compared.append(item)
        remote_qty = remote_positions.get(key, 0)
        local_qty = local_positions.get(key, 0)
        if remote_qty != local_qty:
            diffs.append(StatementDiff(item, remote_qty, local_qty, Decimal(0), True, "持仓手数"))
    return StatementReconciliation(
        account_id=statement.account_id,
        trading_day=statement.trading_day,
        kind=statement.kind,
        diffs=tuple(diffs),
        compared=tuple(compared),
        skipped=tuple(skipped),
    )


def require_decimal_nonnegative(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError("tolerance must be a non-negative finite Decimal")


def render_report(result: StatementReconciliation) -> str:
    lines = [
        f"# 结算单比对 {result.account_id} {result.trading_day} ({result.kind})",
        "",
        f"- 比对项: {len(result.compared)}；跳过 (结算单或本地缺项): {len(result.skipped)}；"
        f"阻塞差异: {len(result.blocking)}",
        f"- 结论: {'一致' if result.consistent else '差异超过约定误差，禁止新增风险直到人工确认或更正事件入账'}",
        "",
    ]
    if result.diffs:
        lines.append("| 项 | 结算单 | 本地 | 误差 | 说明 |")
        lines.append("| :--- | ---: | ---: | ---: | :--- |")
        for diff in result.diffs:
            lines.append(
                f"| {diff.item} | {diff.statement_value} | {diff.local_value} | {diff.tolerance} | {diff.note} |"
            )
        lines.append("")
    if result.skipped:
        lines.append("跳过: " + ", ".join(result.skipped))
    return "\n".join(lines) + "\n"


def statements_to_json(statements: Iterable[SettlementStatement]) -> str:
    """把解析结果输出为规范化 JSON (便于归档与独立核对)."""
    payload = []
    for statement in statements:
        payload.append(
            {
                "account_id": statement.account_id,
                "trading_day": statement.trading_day.isoformat(),
                "kind": statement.kind,
                "source": statement.source,
                "summary": {
                    name: (None if value is None else str(value))
                    for name, value in (
                        ("balance_start", statement.summary.balance_start),
                        ("deposit_withdrawal", statement.summary.deposit_withdrawal),
                        ("close_pnl", statement.summary.close_pnl),
                        ("mtm_pnl", statement.summary.mtm_pnl),
                        ("commission", statement.summary.commission),
                        ("balance_end", statement.summary.balance_end),
                        ("margin", statement.summary.margin),
                        ("available", statement.summary.available),
                    )
                },
                "positions": [
                    {
                        "exchange": row.instrument.exchange.value,
                        "symbol": row.instrument.symbol,
                        "side": row.side.value,
                        "quantity": row.quantity,
                    }
                    for row in statement.positions
                ],
                "trades": [
                    {
                        "trade_id": row.trade_id,
                        "exchange": row.instrument.exchange.value,
                        "symbol": row.instrument.symbol,
                        "side": row.side,
                        "offset": row.offset,
                        "price": str(row.price),
                        "quantity": row.quantity,
                        "commission": None if row.commission is None else str(row.commission),
                    }
                    for row in statement.trades
                ],
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = [
    "LocalDayFigures",
    "SettlementStatement",
    "StatementDiff",
    "StatementFormatError",
    "StatementPosition",
    "StatementReconciliation",
    "StatementSummary",
    "StatementTrade",
    "load_statement",
    "reconcile_statement",
    "render_report",
    "statement_from_mapping",
    "statement_from_text",
    "statements_to_json",
]
