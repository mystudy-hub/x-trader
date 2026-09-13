"""Port contracts expose normalized values without importing a concrete adapter."""

from datetime import datetime, timezone
from typing import get_type_hints

import pytest

from qh_trader.core.clock import VirtualClock
from qh_trader.core.constants import Exchange, Offset, OrderType, SendState, Side
from qh_trader.core.event import TimerEvent
from qh_trader.core.objects import (
    Capability,
    CapabilityProfile,
    ControlEpoch,
    InstrumentId,
    LocalSendResult,
    OrderIdentity,
    OrderIntent,
    VersionedValue,
)
from qh_trader.core.ports import (
    AccountQueryPort,
    ClockPort,
    ExecutionPort,
    JournalPort,
    MappingStorePort,
    MarketDataPort,
    RuleStorePort,
)

NOW = datetime(2024, 9, 10, 1, tzinfo=timezone.utc)


class RecordingExecution:
    def __init__(self):
        self.requests = []

    def submit(self, order: OrderIntent, epoch: ControlEpoch) -> LocalSendResult:
        self.requests.append((order, epoch))
        return LocalSendResult(SendState.SENT_UNKNOWN, 0, "test local acceptance")

    def cancel(self, ref: OrderIdentity, epoch: ControlEpoch) -> LocalSendResult:
        self.requests.append((ref, epoch))
        return LocalSendResult(SendState.SENT_UNKNOWN, 0, "test local acceptance")

    def capabilities(self) -> VersionedValue[CapabilityProfile]:
        return VersionedValue(
            value=CapabilityProfile("test", None, {"market_order": Capability(None, False)}),
            source_id="test-only",
            version="v1",
            effective_from=NOW,
            available_at=NOW,
        )


def test_execution_port_can_be_injected_without_a_gateway_base_class():
    adapter = RecordingExecution()
    port: ExecutionPort = adapter
    assert isinstance(adapter, ExecutionPort)
    epoch = ControlEpoch("test-controller", 1)
    order = OrderIntent(
        client_order_id="order-1",
        account_id="test-account",
        strategy_id="test-strategy",
        instrument=InstrumentId(Exchange.SHFE, "rb2410"),
        side=Side.BUY,
        offset=Offset.OPEN,
        quantity=1,
        order_type=OrderType.LIMIT,
        created_at=NOW,
        limit_price_ticks=3500,
    )
    assert port.submit(order, epoch).state == SendState.SENT_UNKNOWN
    assert adapter.requests == [(order, epoch)]
    assert port.capabilities().value.values["market_order"].value is None


def test_virtual_clock_implements_clock_port_with_normalized_timer_payload():
    clock = VirtualClock[TimerEvent](NOW)
    port: ClockPort = clock
    assert isinstance(clock, ClockPort)
    port.schedule(port.now(), TimerEvent("timeout", {"query_id": "q1"}))
    assert clock.next_event().payload["query_id"] == "q1"


@pytest.mark.parametrize(
    "protocol",
    [
        ExecutionPort,
        MarketDataPort,
        ClockPort,
        JournalPort,
        RuleStorePort,
        MappingStorePort,
        AccountQueryPort,
    ],
)
def test_all_port_annotations_resolve_without_adapter_imports(protocol):
    methods = [value for name, value in vars(protocol).items() if not name.startswith("_") and callable(value)]
    assert methods
    for method in methods:
        assert "return" in get_type_hints(method)
