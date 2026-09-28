#!/usr/bin/env python
"""[脚本工具] 从柜台生成实盘用合约目录 (S5-01, FR-CON-01, FR-RULE-05).

为什么需要它：实盘装配用合约目录解析乘数与最小变动，而仓库里的版本化目录来自研究数据集的**观测区间**
（S4 目录里 `rb2610` 止于 2026-09-18，柜台给的到期日是 20261015），当前交易日很容易落在观测区间
之外，装配于是抛 `MissingRuleError`。实盘参数由柜台裁定（它在撮合与保证金上执行的正是这套值），所以
这里生成一份"柜台口径"目录供 `--catalog` 使用；**研究与回测仍用版本化目录**（带来源与观测区间）。

边界：

- 只做只读查询（`ReqQryInstrument`）：不下单、不碰交易库；输出写到 `runs/`（git-ignored）。
- 手续费与保证金**不在**本文件里：它们仍来自 `product_registry` 的研究假设，装配时按该登记注入。
- 柜台没有该合约、参数缺失或合约只剩格式差异时明确失败，不用 0、不用别的合约代替。
- 生成的目录只对"当时的柜台口径"成立：换了柜台或柜台改了参数就要重新生成。

用法::

    set QH_CTP_PASSWORD=...
    uv run --no-sync python scripts/build_live_contract_catalog.py \\
        --profile openctp_tts --config config/settings.openctp-7x24.local.yaml \\
        --symbols SHFE.rb2610 --out runs/live/ctp_live_catalog.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.event import CanonicalEvent  # noqa: E402
from qh_trader.gateway.ctp_gateway import CtpOrderRefBook, CtpTraderGateway  # noqa: E402
from qh_trader.gateway.ctp_query import CtpQueryAdapter  # noqa: E402
from qh_trader.gateway.feedback_normalizer import build_normalizer  # noqa: E402
from scripts import ctp_setup  # noqa: E402

SOURCE_ID = "ctp_counter:ReqQryInstrument"
ACCOUNT_ID = "live-catalog-builder"


class _Sink:
    """只丢弃事件：本脚本不报单、不记账，只需要一个回调出口."""

    def __init__(self) -> None:
        self.events: list[CanonicalEvent] = []

    def enqueue(self, event: CanonicalEvent) -> bool:
        self.events.append(event)
        return True

    def enqueue_callback_error(self, source_id: str, error: Exception) -> None:
        raise RuntimeError(f"counter callback failed while building the live catalog ({source_id})") from error


def _delivery(instrument_id: str, expire_date: str) -> tuple[int, int]:
    """交割年月的唯一来源是柜台的到期日（交割月内），并用合约代码的月份数字交叉核对.

    不从代码本身猜年份：3 位代码（郑商所 ``AP610``）只有一位年数字，猜出来的年份可以是十年里的任意一年。
    到期日的月份与代码最后两位不一致时明确失败——口径矛盾时不做取舍。
    """
    digits = "".join(character for character in instrument_id if character.isdigit())
    expire_year, expire_month = int(expire_date[:4]), int(expire_date[5:7])
    if digits:
        if len(digits) < 3 or int(digits[-2:]) != expire_month:
            raise ctp_setup.BrokerProfileError(
                f"the contract code {instrument_id!r} and its counter expiry {expire_date} disagree on the delivery month"
            )
    return expire_year, expire_month


def _date_from_counter(value: object, *, symbol: str, field: str) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) != 8 or not text.isdigit():
        raise ctp_setup.BrokerProfileError(f"the counter returned no usable {field} for {symbol}: {value!r}")
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def entry_from_instrument(symbol: str, record: Mapping[str, object], *, available_at: str) -> dict[str, object]:
    """柜台合约记录 → 目录条目；缺乘数 / 最小变动 / 到期日即失败."""
    instrument_id = str(record.get("InstrumentID") or "")
    exchange = str(record.get("ExchangeID") or "")
    product = str(record.get("ProductID") or "")
    if instrument_id != symbol.split(".")[-1]:
        raise ctp_setup.BrokerProfileError(
            f"the counter answered a different instrument for {symbol}: {instrument_id!r}"
        )
    if not exchange or not product:
        raise ctp_setup.BrokerProfileError(f"the counter gave no exchange or product for {symbol}")
    multiplier = str(record.get("VolumeMultiple") or "")
    price_tick = str(record.get("PriceTick") or "")
    if not multiplier or not price_tick or float(multiplier) <= 0 or float(price_tick) <= 0:
        raise ctp_setup.BrokerProfileError(f"the counter gave no usable multiplier / price tick for {symbol}")
    last_trading_day = _date_from_counter(record.get("ExpireDate"), symbol=symbol, field="ExpireDate")
    year, month = _delivery(instrument_id, last_trading_day)
    return {
        "symbol": instrument_id,
        "product": product,
        "exchange": exchange,
        "delivery_year": year,
        "delivery_month": month,
        "multiplier": multiplier,
        "price_tick": price_tick,
        "listed_on": _date_from_counter(record.get("OpenDate"), symbol=symbol, field="OpenDate"),
        "last_trading_day": last_trading_day,
        "aliases": [],
        "source_id": SOURCE_ID,
        "available_at": available_at,
        "counter_is_trading": str(record.get("IsTrading") or ""),
        "counter_inst_life_phase": str(record.get("InstLifePhase") or ""),
    }


def build(args: argparse.Namespace) -> int:
    profile = ctp_setup.load_broker_profile(args.profile)
    symbols = [item.strip() for item in str(args.symbols or "").split(",") if item.strip()]
    if not symbols:
        raise ctp_setup.BrokerProfileError("--symbols is required: list the contracts the live run needs")
    settings = ctp_setup.ctp_settings(
        profile,
        user_id=args.user_id,
        investor_id=args.investor_id,
        front=args.front,
        flow_dir=args.flow_dir,
        connect_timeout_s=args.connect_timeout,
        login_timeout_s=args.login_timeout,
        query_timeout_s=args.query_timeout,
    )
    sink = _Sink()
    ref_book = CtpOrderRefBook()
    normalizer = build_normalizer(ACCOUNT_ID, ref_book)
    gateway = CtpTraderGateway(
        settings=settings,
        account_id=ACCOUNT_ID,
        events=sink,
        normalizer=normalizer,
        price_tick=lambda instrument: 1,
        capability_profile=ctp_setup.capability_profile(profile),
        capability_version="registered:" + str(profile.get("profile_name")),
        authority=lambda: None,
        offset_mappings=ctp_setup.offset_mappings(profile),
        ref_book=ref_book,
        source_id="live-catalog",
    )
    queries = CtpQueryAdapter(
        account_id=ACCOUNT_ID,
        channel=gateway,
        normalizer=normalizer,
        investor_id=settings.investor_id,
        broker_id=settings.broker_id,
        trading_day=lambda: gateway.trading_day,
        interval_ms=args.query_interval_ms,
        timeout_s=args.query_timeout,
    )
    gateway.router.queries = queries
    session = gateway.connect()
    generated_at = datetime.now(timezone.utc)
    try:
        entries = []
        missing: list[str] = []
        for symbol in symbols:
            record = queries.query_instrument(symbol.split(".")[-1])
            if record is None:
                missing.append(symbol)
                continue
            entries.append(entry_from_instrument(symbol, record, available_at=generated_at.isoformat()))
    finally:
        gateway.close()
    if missing:
        raise ctp_setup.BrokerProfileError(f"the counter has no instrument record for: {missing}")
    catalog = {
        "schema_version": 1,
        "catalog_version": "ctp-live-" + generated_at.strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": generated_at.isoformat(),
        "source": (
            f"柜台口径：ReqQryInstrument @ {settings.front_trade}"
            "（乘数 / 最小变动 / 上市日 / 到期日由柜台给出）；手续费与保证金仍来自 product_registry 研究假设"
        ),
        "counter": {
            "profile": profile.get("profile_name"),
            "front_trade": settings.front_trade,
            "broker_id": settings.broker_id,
            "investor_id_masked": settings.investor_id[:2] + "***" if len(settings.investor_id) > 2 else "***",
            "binding": gateway.binding.name,
            "api_version": report_api_version(session),
            "trading_day": None if session.trading_day is None else session.trading_day.isoformat(),
        },
        "entries": entries,
    }
    out_path = (ROOT / args.out).resolve()
    if not out_path.is_relative_to(ROOT / "runs"):
        raise ctp_setup.BrokerProfileError("the live catalog must be written under runs/ (it carries a counter口径)")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已生成柜台口径合约目录: {out_path.relative_to(ROOT).as_posix()}（{len(entries)} 个合约）")
    for entry in entries:
        print(
            f"    {entry['exchange']}.{entry['symbol']} 乘数 {entry['multiplier']} 最小变动 {entry['price_tick']} "
            f"上市 {entry['listed_on']} 到期 {entry['last_trading_day']} 可交易={entry['counter_is_trading']}"
        )
    return 0


def report_api_version(session: object) -> str | None:
    value = getattr(session, "api_version", None)
    return None if value is None else str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从柜台生成实盘用合约目录（只读查询）")
    parser.add_argument("--profile", default="openctp_tts", help="柜台登记名")
    parser.add_argument("--user", dest="user_id", default=None, help="覆盖登录用户号")
    parser.add_argument("--investor", dest="investor_id", default=None, help="覆盖投资者号")
    parser.add_argument("--config", default="config/settings.yaml", help="运行配置（取账号标识与前置）")
    parser.add_argument("--front", default=None, help="覆盖交易前置地址")
    parser.add_argument("--symbols", default=None, help="实盘要用的合约（逗号分隔），如 SHFE.rb2610")
    parser.add_argument("--out", default="runs/live/ctp_live_catalog.json", help="输出文件（必须在 runs/ 下）")
    parser.add_argument("--flow-dir", default="runs/live/ctp_catalog_flow", help="CTP 私有流目录")
    parser.add_argument("--query-interval-ms", type=int, default=1000, help="查询流控间隔")
    parser.add_argument("--query-timeout", type=float, default=30.0, help="单次查询等待应答的秒数")
    parser.add_argument("--connect-timeout", type=float, default=20.0, help="等待前置连接的秒数")
    parser.add_argument("--login-timeout", type=float, default=20.0, help="等待登录的秒数")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    config_path = ROOT / args.config
    if config_path.is_file():
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
        broker = data.get("broker") or {}
        if args.profile is None:
            args.profile = broker.get("profile")
        if broker.get("profile") == args.profile:
            # 账号标识只从与所选登记一致的本地配置取，避免把 SimNow 账号带进 openctp 环境
            args.user_id = args.user_id or broker.get("user_id")
            args.investor_id = args.investor_id or broker.get("investor_id")
            if args.front is None and broker.get("front_trade_uri"):
                args.front = broker["front_trade_uri"]
    else:
        args.user_id = args.user_id or None
        args.investor_id = args.investor_id or None
    try:
        return build(args)
    except ctp_setup.BrokerProfileError as exc:
        print(f"登记不成立：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 柜台不可用或查询失败都要给出可读结论
        print(f"生成失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
