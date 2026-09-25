"""CTP 绑定的等价假件：在没有柜台和原生库的环境里驱动网关的握手、回报与查询.

它模拟 ``openctp-ctp`` 的结构体、TraderApi 与回调 SPI 的最低限度表面：
结构体可以是任意属性对象，SPI 基类方法可被覆盖，Req* 立即或以脚本化的方式回调。
假件只用于测试；它证明的是我们的映射与门禁逻辑，不构成柜台行为的证据。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any


class FakeField:
    """SWIG 结构体等价物：可写任意属性，带 ``thisown``."""

    def __init__(self, __type__: str = "", **values: Any) -> None:
        self.__type__ = __type__
        self.thisown = True
        for name, value in values.items():
            setattr(self, name, value)

    def as_mapping(self) -> dict[str, Any]:
        return {name: value for name, value in self.__dict__.items() if not name.startswith("_")}


class FakeTraderSpiBase:
    """SPI 基类：只保留网关会覆盖的方法名，默认实现记录调用以便断言覆盖生效."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def OnFrontConnected(self) -> None:  # noqa: N802 - CTP 命名
        self.calls.append("OnFrontConnected")

    def OnFrontDisconnected(self, nReason: int) -> None:  # noqa: N802, N803
        self.calls.append("OnFrontDisconnected")

    def OnHeartBeatWarning(self, nTimeLapse: int) -> None:  # noqa: N802, N803
        self.calls.append("OnHeartBeatWarning")

    def OnRtnOrder(self, order: object) -> None:  # noqa: N802
        self.calls.append("OnRtnOrder")

    def OnRtnTrade(self, trade: object) -> None:  # noqa: N802
        self.calls.append("OnRtnTrade")

    def OnRspQryTradingAccount(self, field: object, info: object, request_id: int, is_last: bool) -> None:  # noqa: N802
        self.calls.append("OnRspQryTradingAccount")

    def OnRspQryInvestorPosition(self, field: object, info: object, request_id: int, is_last: bool) -> None:  # noqa: N802
        self.calls.append("OnRspQryInvestorPosition")

    def OnRspQryOrder(self, field: object, info: object, request_id: int, is_last: bool) -> None:  # noqa: N802
        self.calls.append("OnRspQryOrder")

    def OnRspQryTrade(self, field: object, info: object, request_id: int, is_last: bool) -> None:  # noqa: N802
        self.calls.append("OnRspQryTrade")

    def OnRspQryInstrument(self, field: object, info: object, request_id: int, is_last: bool) -> None:  # noqa: N802
        self.calls.append("OnRspQryInstrument")

    def OnRspQryDepthMarketData(self, field: object, info: object, request_id: int, is_last: bool) -> None:  # noqa: N802
        self.calls.append("OnRspQryDepthMarketData")


class FakeTraderApi:
    """柜台交易接口：握手按脚本应答，报单 / 撤单 / 查询记录到 ``calls``."""

    def __init__(self, binding: FakeCtpBinding) -> None:
        self.binding = binding
        self.spi: object | None = None
        self.calls: list[tuple[str, object]] = []
        self.released = False
        self.insert_fields: list[FakeField] = []
        self.action_fields: list[FakeField] = []
        self.query_fields: list[tuple[str, FakeField]] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期
    def RegisterSpi(self, spi: object) -> None:  # noqa: N802
        self.spi = spi
        self.calls.append(("RegisterSpi", spi))

    def SubscribePrivateTopic(self, mode: int) -> None:  # noqa: N802
        self.calls.append(("SubscribePrivateTopic", mode))

    def SubscribePublicTopic(self, mode: int) -> None:  # noqa: N802
        self.calls.append(("SubscribePublicTopic", mode))

    def RegisterFront(self, front: str) -> None:  # noqa: N802
        self.calls.append(("RegisterFront", front))

    def Init(self) -> None:  # noqa: N802
        self.calls.append(("Init", None))
        if not self.binding.silent_front:
            self.front_connected()

    def GetApiVersion(self) -> str:  # noqa: N802
        return self.binding.api_version_value

    def Release(self) -> None:  # noqa: N802
        self.released = True
        self.calls.append(("Release", None))

    # ------------------------------------------------------------------ 请求
    def ReqAuthenticate(self, field: FakeField, request_id: int) -> int:  # noqa: N802
        self.calls.append(("ReqAuthenticate", field))
        code = self.binding.auth_code
        if code != 0:
            self.spi.OnRspAuthenticate(field, self.rsp_info(code), request_id, True)  # type: ignore[attr-defined]
            return 0
        if self.binding.auth_network_error:
            return -1
        self.spi.OnRspAuthenticate(field, self.rsp_info(0), request_id, True)  # type: ignore[attr-defined]
        return 0

    def ReqUserLogin(self, field: FakeField, request_id: int) -> int:  # noqa: N802
        self.calls.append(("ReqUserLogin", field))
        code = self.binding.login_code
        if code != 0:
            self.spi.OnRspUserLogin(field, self.rsp_info(code), request_id, True)  # type: ignore[attr-defined]
            return 0
        if self.binding.login_silent:
            return 0
        self.spi.OnRspUserLogin(self.login_field(), self.rsp_info(0), request_id, True)  # type: ignore[attr-defined]
        return 0

    def ReqSettlementInfoConfirm(self, field: FakeField, request_id: int) -> int:  # noqa: N802
        self.calls.append(("ReqSettlementInfoConfirm", field))
        if self.binding.settlement_confirm_silent:
            return 0
        self.spi.OnRspSettlementInfoConfirm(  # type: ignore[attr-defined]
            field, self.rsp_info(0), request_id, True
        )
        return 0

    def ReqOrderInsert(self, field: FakeField, request_id: int) -> int:  # noqa: N802
        self.calls.append(("ReqOrderInsert", field))
        self.insert_fields.append(field)
        code = self.binding.insert_code
        if code == 0 and not self.binding.silent_reports:
            self.order_report(field, "3")
        return code

    def ReqOrderAction(self, field: FakeField, request_id: int) -> int:  # noqa: N802
        self.calls.append(("ReqOrderAction", field))
        self.action_fields.append(field)
        code = self.binding.action_code
        if code == 0 and not self.binding.silent_reports:
            self.order_report(field, "5", cancelled=True)
        return code

    def ReqQryTradingAccount(self, field: FakeField, request_id: int) -> int:  # noqa: N802
        return self._query("account", field, request_id)

    def ReqQryInvestorPosition(self, field: FakeField, request_id: int) -> int:  # noqa: N802
        return self._query("position", field, request_id)

    def ReqQryOrder(self, field: FakeField, request_id: int) -> int:  # noqa: N802
        return self._query("order", field, request_id)

    def ReqQryTrade(self, field: FakeField, request_id: int) -> int:  # noqa: N802
        return self._query("trade", field, request_id)

    def ReqQryInstrument(self, field: FakeField, request_id: int) -> int:  # noqa: N802
        return self._query("instrument", field, request_id)

    def ReqQryDepthMarketData(self, field: FakeField, request_id: int) -> int:  # noqa: N802
        return self._query("depth", field, request_id)

    def _query(self, kind: str, field: FakeField, request_id: int) -> int:
        self.calls.append((f"ReqQry:{kind}", field))
        self.query_fields.append((kind, field))
        local_code = self.binding.query_return_codes.get(kind, 0)
        if local_code != 0:
            return local_code
        if self.binding.query_silent.get(kind):
            return 0
        code = self.binding.query_codes.get(kind, 0)
        handler = getattr(self.spi, QUERY_CALLBACKS[kind])  # type: ignore[attr-defined]
        records = () if code else self.binding.query_records.get(kind, ())
        if not records:
            handler(None, self.rsp_info(code), request_id, True)
            return 0
        for index, record in enumerate(records):
            handler(record, self.rsp_info(code), request_id, index == len(records) - 1)
        return 0

    # ------------------------------------------------------------------ 回调注入
    def rsp_info(self, code: int, message: str | None = None) -> FakeField:
        return FakeField(
            "CThostFtdcRspInfoField",
            ErrorID=code,
            ErrorMsg=message if message is not None else ("" if code == 0 else f"fake error {code}"),
        )

    def login_field(self) -> FakeField:
        return FakeField(
            "CThostFtdcRspUserLoginField",
            BrokerID=self.binding.broker_id,
            UserID=self.binding.user_id,
            FrontID=self.binding.front_id,
            SessionID=self.binding.session_id,
            MaxOrderRef=self.binding.max_order_ref,
            TradingDay=self.binding.trading_day,
        )

    def order_report(self, field: FakeField, status: str, *, cancelled: bool = False) -> None:
        return self.push_order(
            order_ref=str(getattr(field, "OrderRef", "")),
            order_sys_id=str(getattr(field, "OrderSysID", "") or self.binding.order_sys_id),
            status=status,
            traded=self.binding.traded_volume if status in ("0", "1") else 0,
            cancelled=cancelled,
        )

    def push_order(
        self,
        *,
        order_ref: str,
        status: str,
        order_sys_id: str | None = None,
        traded: int = 0,
        cancelled: bool = False,
        submit_status: str = "3",
        instrument: str | None = None,
        exchange: str | None = None,
        order_time: tuple[str, str] | None = None,
    ) -> None:
        day, clock = order_time or (self.binding.insert_date, self.binding.insert_time)
        order = FakeField(
            "CThostFtdcOrderField",
            BrokerID=self.binding.broker_id,
            InvestorID=self.binding.investor_id,
            InstrumentID=instrument or self.binding.instrument_id,
            ExchangeID=exchange or self.binding.exchange_id,
            OrderRef=order_ref,
            OrderSysID=order_sys_id or self.binding.order_sys_id,
            OrderLocalID="local-1",
            FrontID=None if cancelled else self.binding.front_id,
            SessionID=None if cancelled else self.binding.session_id,
            Direction="0",
            CombOffsetFlag="0",
            OrderStatus=status,
            OrderSubmitStatus=submit_status,
            VolumeTotalOriginal=1,
            VolumeTraded=traded,
            InsertDate=day,
            InsertTime=clock,
            UpdateTime=clock,
            TradingDay=self.binding.trading_day,
        )
        self.spi.OnRtnOrder(order)  # type: ignore[attr-defined]

    def push_trade(
        self,
        *,
        order_ref: str,
        trade_id: str = "trade-1",
        volume: int = 1,
        price: float = 3000.0,
        offset_flag: str = "0",
    ) -> None:
        trade = FakeField(
            "CThostFtdcTradeField",
            BrokerID=self.binding.broker_id,
            InvestorID=self.binding.investor_id,
            InstrumentID=self.binding.instrument_id,
            ExchangeID=self.binding.exchange_id,
            OrderRef=order_ref,
            OrderSysID=self.binding.order_sys_id,
            TradeID=trade_id,
            Direction="0",
            OffsetFlag=offset_flag,
            HedgeFlag="1",
            TradeType="0",
            Price=price,
            Volume=volume,
            TradeDate=self.binding.insert_date,
            TradeTime=self.binding.insert_time,
            TradingDay=self.binding.trading_day,
        )
        self.spi.OnRtnTrade(trade)  # type: ignore[attr-defined]

    def push_insert_error(self, field: FakeField, code: int = 31) -> None:
        self.spi.OnErrRtnOrderInsert(field, self.rsp_info(code))  # type: ignore[attr-defined]

    def push_error(self, code: int, request_id: int = 0) -> None:
        self.spi.OnRspError(self.rsp_info(code), request_id, True)  # type: ignore[attr-defined]

    def front_connected(self) -> None:
        self.spi.OnFrontConnected()  # type: ignore[attr-defined]

    def front_disconnected(self, reason: int = 0x2001) -> None:
        self.spi.OnFrontDisconnected(reason)  # type: ignore[attr-defined]

    def heartbeat_warning(self, lapse: int = 90) -> None:
        self.spi.OnHeartBeatWarning(lapse)  # type: ignore[attr-defined]


QUERY_CALLBACKS = {
    "account": "OnRspQryTradingAccount",
    "position": "OnRspQryInvestorPosition",
    "order": "OnRspQryOrder",
    "trade": "OnRspQryTrade",
    "instrument": "OnRspQryInstrument",
    "depth": "OnRspQryDepthMarketData",
    "settlement_confirm": "OnRspQrySettlementInfoConfirm",
}


def account_record(balance: str = "100000", available: str = "90000", margin: str = "10000") -> FakeField:
    return FakeField(
        "CThostFtdcTradingAccountField",
        AccountID="fake-account",
        BrokerID="9999",
        TradingDay="20260924",
        Balance=float(balance),
        Available=float(available),
        CurrMargin=float(margin),
    )


def position_record(
    *,
    instrument: str = "rb2601",
    exchange: str = "SHFE",
    direction: str = "2",
    position_date: str = "1",
    position: int = 2,
    frozen: int = 0,
    hedge: str = "1",
) -> FakeField:
    return FakeField(
        "CThostFtdcInvestorPositionField",
        BrokerID="9999",
        InvestorID="fake",
        InstrumentID=instrument,
        ExchangeID=exchange,
        PosiDirection=direction,
        HedgeFlag=hedge,
        PositionDate=position_date,
        Position=position,
        YdPosition=0 if position_date == "1" else position,
        TodayPosition=position if position_date == "1" else 0,
        LongFrozen=frozen if direction == "2" else 0,
        ShortFrozen=frozen if direction == "3" else 0,
        TradingDay="20260924",
    )


class FakeCtpBinding:
    """绑定假件：默认脚本是一次成功的握手与空账户查询."""

    name = "fake-ctp"
    version = "6.7.11-fake"

    def __init__(self, **overrides: Any) -> None:
        self.api_version_value = "fake-api-6.7.13"
        self.broker_id = "9999"
        self.user_id = "fake-user"
        self.investor_id = "fake-investor"
        self.front_id = 12
        self.session_id = 345678
        self.max_order_ref = "0"
        self.trading_day = "20260924"
        self.insert_date = "20260924"
        self.insert_time = "09:05:00"
        self.instrument_id = "rb2601"
        self.exchange_id = "SHFE"
        self.order_sys_id = "sys-1"
        self.traded_volume = 0
        self.auth_code = 0
        self.login_code = 0
        self.insert_code = 0
        self.action_code = 0
        self.auth_network_error = False
        self.login_silent = False
        self.settlement_confirm_silent = False
        self.silent_front = False
        self.silent_reports = False
        self.query_silent: dict[str, bool] = {}
        self.query_codes: Mapping[str, int] = {}
        self.query_return_codes: Mapping[str, int] = {}
        self.query_records: dict[str, tuple[FakeField, ...]] = {"account": (account_record(),)}
        self.dll_files = {"thosttraderapi_se-fake.dll": "0" * 64}
        for name, value in overrides.items():
            if not hasattr(self, name):
                raise AttributeError(f"unknown fake binding option {name!r}")
            setattr(self, name, value)
        self.created: list[FakeTraderApi] = []

    def create_trader_api(self, flow_dir: str) -> FakeTraderApi:
        api = FakeTraderApi(self)
        self.created.append(api)
        return api

    def trader_spi_base(self) -> type:
        return FakeTraderSpiBase

    def field(self, type_name: str) -> FakeField:
        return FakeField(type_name)

    def api_version(self, api: object) -> str:
        return self.api_version_value

    def dll_hashes(self) -> Mapping[str, str]:
        return dict(self.dll_files)

    @property
    def api(self) -> FakeTraderApi:
        assert self.created, "the gateway has not created a trader api yet"
        return self.created[-1]


# --------------------------------------------------------------------------------- 行情（MdApi）假件


class FakeMdSpiBase:
    """行情 SPI 基类：只保留网关会覆盖的方法名."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def OnFrontConnected(self) -> None:  # noqa: N802
        self.calls.append("OnFrontConnected")

    def OnFrontDisconnected(self, nReason: int) -> None:  # noqa: N802, N803
        self.calls.append("OnFrontDisconnected")

    def OnRspSubMarketData(self, field: object, info: object, request_id: int, is_last: bool) -> None:  # noqa: N802
        self.calls.append("OnRspSubMarketData")

    def OnRspUnSubMarketData(self, field: object, info: object, request_id: int, is_last: bool) -> None:  # noqa: N802
        self.calls.append("OnRspUnSubMarketData")

    def OnRtnDepthMarketData(self, field: object) -> None:  # noqa: N802
        self.calls.append("OnRtnDepthMarketData")


class FakeMdApi:
    """行情接口假件：登录按脚本应答，订阅记录到 ``subscribed``."""

    def __init__(self, binding: FakeMdBinding) -> None:
        self.binding = binding
        self.spi: object | None = None
        self.calls: list[tuple[str, object]] = []
        self.subscribed: list[str] = []
        self.released = False

    def RegisterSpi(self, spi: object) -> None:  # noqa: N802
        self.spi = spi
        self.calls.append(("RegisterSpi", spi))

    def RegisterFront(self, front: str) -> None:  # noqa: N802
        self.calls.append(("RegisterFront", front))

    def Init(self) -> None:  # noqa: N802
        self.calls.append(("Init", None))
        if not self.binding.silent_front:
            self.spi.OnFrontConnected()  # type: ignore[attr-defined]

    def ReqUserLogin(self, field: object, request_id: int) -> int:  # noqa: N802
        self.calls.append(("ReqUserLogin", field))
        if self.binding.login_silent:
            return 0
        info = FakeField(
            "CThostFtdcRspInfoField",
            ErrorID=self.binding.login_code,
            ErrorMsg="" if self.binding.login_code == 0 else "fake md error",
        )
        self.spi.OnRspUserLogin(field, info, request_id, True)  # type: ignore[attr-defined]
        return 0

    def SubscribeMarketData(self, symbols: object, count: int) -> int:  # noqa: N802
        self.calls.append(("SubscribeMarketData", (list(symbols), count)))
        if self.binding.subscribe_silent:
            return 0
        payload = list(symbols) if isinstance(symbols, list) else []
        for index, raw in enumerate(payload):
            symbol = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            accepted = symbol not in self.binding.rejected_symbols
            if accepted:
                self.subscribed.append(symbol)
                if self.binding.auto_tick_on_subscribe:
                    self.push_tick(InstrumentID=symbol)
            field = FakeField("CThostFtdcSpecificInstrumentField", InstrumentID="" if not accepted else symbol)
            info = FakeField(
                "CThostFtdcRspInfoField",
                ErrorID=0 if accepted else 26,
                ErrorMsg="" if accepted else "fake subscription rejected",
            )
            self.spi.OnRspSubMarketData(field, info, index + 1, index == len(payload) - 1)  # type: ignore[attr-defined]
        return 0

    def UnSubscribeMarketData(self, symbols: object, count: int) -> int:  # noqa: N802
        self.calls.append(("UnSubscribeMarketData", (list(symbols), count)))
        return 0

    def Release(self) -> None:  # noqa: N802
        self.released = True

    def push_tick(self, **values: Any) -> None:
        defaults = {
            "InstrumentID": self.binding.symbol,
            "TradingDay": self.binding.trading_day,
            "ActionDay": self.binding.action_day,
            "UpdateTime": "10:26:08",
            "UpdateMillisec": 500,
            "LastPrice": 3053.0,
            "BidPrice1": 3052.0,
            "BidVolume1": 12,
            "AskPrice1": 3054.0,
            "AskVolume1": 30,
            "Volume": 42925.0,
            "Turnover": 1313292470.0,
            "OpenInterest": 201290.0,
            "PreSettlementPrice": 3063.0,
            "UpperLimitPrice": 3216.0,
            "LowerLimitPrice": 2909.0,
        }
        defaults.update(values)
        self.spi.OnRtnDepthMarketData(FakeField("CThostFtdcDepthMarketDataField", **defaults))  # type: ignore[attr-defined]

    def front_disconnected(self, reason: int = 0x1001) -> None:
        self.spi.OnFrontDisconnected(reason)  # type: ignore[attr-defined]


class FakeMdBinding:
    """行情绑定假件."""

    name = "fake-ctp"
    version = "6.7.11-fake"

    def __init__(self, **overrides: Any) -> None:
        self.symbol = "rb2610"
        self.trading_day = "20260923"
        self.action_day = "20260923"
        self.login_code = 0
        self.login_silent = False
        self.silent_front = False
        self.subscribe_silent = False
        self.auto_tick_on_subscribe = False
        self.rejected_symbols: tuple[str, ...] = ()
        self.created: list[FakeMdApi] = []
        for name, value in overrides.items():
            if not hasattr(self, name):
                raise AttributeError(f"unknown fake market binding option {name!r}")
            setattr(self, name, value)

    def create_md_api(self, flow_dir: str) -> FakeMdApi:
        api = FakeMdApi(self)
        self.created.append(api)
        return api

    def md_spi_base(self) -> type:
        return FakeMdSpiBase

    def login_field(self) -> FakeField:
        return FakeField("CThostFtdcReqUserLoginField")

    def snapshot_field(self, type_name: str) -> FakeField:
        return FakeField(type_name)

    @property
    def api(self) -> FakeMdApi:
        assert self.created, "the market gateway has not created an md api yet"
        return self.created[-1]
