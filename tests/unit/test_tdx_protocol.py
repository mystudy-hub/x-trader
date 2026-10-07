"""[测试] 扩展行情帧、缺失字段与损坏响应拒绝（S1-12，A03、A04）。

除明确标注的 2026-10-06 真实录制回放外，其余报文为按公开布局人工构造的独立样本。
"""

import hashlib
import json
import struct
import zlib
from datetime import datetime, time
from decimal import Decimal

import pytest
from fake_tdx_server import FakeTdxServer, ReplayStep

from qh_trader.data.tdx_exhq import DataFormatError, TdxExHqClient, load_tdx_servers

HANDSHAKE = bytes.fromhex(
    "0101486500015200520054241f32c6e5d53dfb411f32c6e5d53dfb41"
    "1f32c6e5d53dfb411f32c6e5d53dfb411f32c6e5d53dfb411f32c6e5"
    "d53dfb411f32c6e5d53dfb411f32c6e5d53dfb41cce16dffd5ba3fb8cbc57a054f7748ea"
)
INSTRUMENT = b"\x1eRB2701\0\0\0"

# 2026-10-06 从公开节点 116.205.143.214:7727 录制。完整本地证据：
# runs/s0/tdx_protocol_corrected_handshake_20261006.json；无账户和私有客户端文件。
# 该样本独立于人工组包测试，保留真实 16 字节信封与压缩握手响应。
RECORDED_HANDSHAKE_RESPONSE = bytes.fromhex(
    "b1cb7400110148650000542446002b01789c63642003bc62e762531263100d71898877094693ec3f54eac010cac00884"
    "bb7f6edc77fdd4c1f3e4d84143c0c8c0606861ac676460a1676aaa676864429c36007d050ea9"
)
RECORDED_BAR_REQUEST = bytes.fromhex("0101086a010116001600ff231e52423237303100000004000100000000000100")
RECORDED_BAR_RESPONSE = bytes.fromhex(
    "b1cb74000101086a0100ff23340034001e52423237303100000004000100000000000100422835010070424500b04345"
    "0040424500804245ca4b1800d4bf090000c04245"
)
RECORDED_EMPTY_REQUEST = bytes.fromhex("0101086a010116001600ff231e52423234313000000004000100000000004000")
RECORDED_EMPTY_RESPONSE = bytes.fromhex(
    "b1cb74000101086a0100ff23340034001e5242323431300000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000"
)


def test_recorded_public_server_handshake_and_daily_bar_replay():
    """真实响应帧离线回放；预期量仓与方案给出的独立日线样本一致。"""
    assert hashlib.sha256(RECORDED_HANDSHAKE_RESPONSE).hexdigest() == (
        "3437257a2e60ffe664f2a6444b7a27173d5acf9df8a6e668cdadec8d24b8c72a"
    )
    assert hashlib.sha256(RECORDED_BAR_RESPONSE).hexdigest() == (
        "666d58fd07b92315a17faf29b6fc64bafe6f0a8706d2e1cdf5320ee1652404c1"
    )
    steps = [
        ReplayStep(HANDSHAKE, RECORDED_HANDSHAKE_RESPONSE),
        ReplayStep(RECORDED_BAR_REQUEST, RECORDED_BAR_RESPONSE),
    ]
    with FakeTdxServer(steps, fragment_size=1) as server, TdxExHqClient([server.endpoint], retries=0) as client:
        row = client.get_instrument_bars(4, 30, "RB2701", 0, 1)[0]
    assert row["datetime"] == datetime(2026, 9, 30)
    assert row["open_interest"] == 1592266
    assert row["volume"] == 638932
    assert row["close"] == Decimal("3112.0")
    assert row["price"] == Decimal("3116.0")
    assert row["turnover"] is row["settlement_price"] is None


def test_recorded_expired_contract_zero_record_placeholder_is_recognized():
    """RB2410 实录空响应恰含一个全零占位，不把损坏数据泛化为空页。"""
    assert hashlib.sha256(RECORDED_EMPTY_RESPONSE).hexdigest() == (
        "3f0d3e98f157f89351529d2878703b20e356de2b185592b23127ddde4d8bf935"
    )
    steps = [
        ReplayStep(HANDSHAKE, RECORDED_HANDSHAKE_RESPONSE),
        ReplayStep(RECORDED_EMPTY_REQUEST, RECORDED_EMPTY_RESPONSE),
    ]
    with FakeTdxServer(steps) as server, TdxExHqClient([server.endpoint], retries=0) as client:
        assert client.get_instrument_bars(4, 30, "RB2410", 0, 64) == []


def test_zero_count_with_nonzero_placeholder_is_rejected(monkeypatch):
    payload = INSTRUMENT + b"\0" * 41 + b"\x01"
    client = TdxExHqClient()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: payload)
    with pytest.raises(DataFormatError):
        client.get_instrument_bars(4, 30, "RB2701", 0, 1)


def envelope(payload: bytes, *, compressed: bool = False) -> bytes:
    encoded = zlib.compress(payload) if compressed else payload
    return struct.pack("<IIIHH", 0, 0, 0, len(encoded), len(payload)) + encoded


def bar_payload(*, date_bytes: bytes = struct.pack("<I", 20260930), open_price: float = 3111.0) -> bytes:
    return (
        INSTRUMENT + b"\0" * 8
        + struct.pack("<H", 1)
        + date_bytes
        + struct.pack("<ffffIIf", open_price, 3131.0, 3108.0, 3112.0, 0xFFFFFFFF, 638932, 3116.0)
    )


def test_all_eight_commands_replay_fragmented_and_compressed_frames():
    market = b"\x01" + "上期所".encode("gbk").ljust(32, b"\0") + b"\x1eSH" + b"\0" * 28
    directory = struct.pack("<BB3x9s17s9s24x", 1, 30, b"RB2701", "螺纹".encode("gbk"), b"RB")
    quote_values = [3110.0, 3111.0, 3131.0, 3108.0, 3112.0] + [1, 2, 100, 4, 5, 60, 40, 8, 1592266]
    quote_values += [3111.0] * 5 + [3] * 5 + [3112.0] * 5 + [4] * 5
    quote = INSTRUMENT + b"\0" * 4 + struct.pack("<5f9I5f5I5f5I", *quote_values)
    transaction = INSTRUMENT + b"\0" * 4 + struct.pack("<HHIIiH", 1, 570, 3111250, 20, -4, 10035)
    minute = INSTRUMENT + struct.pack("<HHffII", 1, 570, 3111.0, 3110.5, 20, 1592266)
    samples = [
        (bytes.fromhex("01024869000102000200f423"), struct.pack("<H", 1) + market),
        (bytes.fromhex("01034866000102000200f023"), b"\0" * 19 + struct.pack("<I", 12345) + b"\0" * 9),
        (bytes.fromhex("01044867000108000800f523050000000100"), struct.pack("<IH", 5, 1) + directory),
        (bytes.fromhex("0101080202010c000c00fa23") + INSTRUMENT, quote),
        (bytes.fromhex("0101086a010116001600ff23") + INSTRUMENT + struct.pack("<HHIH", 4, 1, 0, 1), bar_payload()),
        (bytes.fromhex("01010800030112001200fc23") + INSTRUMENT + struct.pack("<iH", 0, 1), transaction),
        (
            bytes.fromhex("010130000201160016000624")
            + struct.pack("<I", 20260930)
            + INSTRUMENT
            + struct.pack("<iH", 0, 1),
            transaction,
        ),
        (bytes.fromhex("0107080001010c000c000b24") + INSTRUMENT, minute),
    ]
    steps = [ReplayStep(HANDSHAKE, envelope(b"OK"))]
    steps += [
        ReplayStep(request, envelope(payload, compressed=index % 2 == 0))
        for index, (request, payload) in enumerate(samples)
    ]
    captures = []
    with (
        FakeTdxServer(steps) as server,
        TdxExHqClient([server.endpoint], retries=0, capture_callback=captures.append) as client,
    ):
        assert client.get_markets() == [{"market": 30, "category": 1, "name": "上期所", "short_name": "SH"}]
        assert client.get_instrument_count() == 12345
        assert client.get_instruments(5, 1)[0]["code"] == "RB2701"
        snapshot = client.get_instrument_quote(30, "RB2701")[0]
        assert snapshot["volume"] == 100
        assert snapshot["inside_volume"] + snapshot["outside_volume"] == 100
        assert snapshot["open_interest"] == 1592266
        assert snapshot["bid1"] == Decimal("3111.0")
        row = client.get_instrument_bars(4, 30, "RB2701", 0, 1)[0]
        assert row["datetime"] == datetime(2026, 9, 30)
        assert row["open_interest"] == 0xFFFFFFFF
        assert row["volume"] == 638932
        assert row["price"] == Decimal("3116.0")
        assert row["turnover"] is row["settlement_price"] is None
        for transactions in (
            client.get_transaction_data(30, "RB2701", 0, 1),
            client.get_history_transaction_data(30, "RB2701", 20260930, 0, 1),
        ):
            assert transactions[0]["price"] == Decimal("3111.25")
            assert transactions[0]["time"] == time(9, 30, 35)
            assert transactions[0]["open_interest_change"] == -4
            assert transactions[0]["direction"] == -1
            assert "date" not in transactions[0] and "trading_day" not in transactions[0]
        point = client.get_minute_time_data(30, "RB2701")[0]
        assert point["open_interest"] == 1592266
        assert point["turnover"] is None
    assert len(HANDSHAKE) == 92
    assert struct.unpack_from("<HH", HANDSHAKE, 6) == (len(HANDSHAKE) - 10,) * 2
    assert len(captures) == 9
    assert captures[4]["response_sha256"] == hashlib.sha256(bytes.fromhex(captures[4]["response_hex"])).hexdigest()


def test_minute_datetime_and_fractional_price_are_preserved(monkeypatch):
    client = TdxExHqClient()
    payload = bar_payload(date_bytes=struct.pack("<HH", (2026 - 2004) * 2048 + 930, 570), open_price=3111.25)
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: payload)
    row = client.get_instrument_bars(7, 30, "RB2701", 0, 1)[0]
    assert row["datetime"] == datetime(2026, 9, 30, 9, 30)
    assert row["open"] == Decimal("3111.25")


@pytest.mark.parametrize(
    "payload",
    [
        bar_payload()[:-1],
        bar_payload() + b"x",
        b"\0" * 19,
        bar_payload(open_price=float("nan")),
        bar_payload(date_bytes=struct.pack("<I", 20260230)),
    ],
)
def test_malformed_bars_fail_explicitly(monkeypatch, payload):
    client = TdxExHqClient()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: payload)
    with pytest.raises(DataFormatError):
        client.get_instrument_bars(4, 30, "RB2701", 0, 1)


@pytest.mark.parametrize("identity", [b"\x1dRB2701\0\0\0", b"\x1eRB2705\0\0\0"])
def test_bar_response_for_another_instrument_is_rejected(monkeypatch, identity):
    client = TdxExHqClient()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: identity + bar_payload()[10:])
    with pytest.raises(DataFormatError, match="instrument does not match"):
        client.get_instrument_bars(4, 30, "RB2701", 0, 1)


@pytest.mark.parametrize(
    "market,code,category,start,count",
    [
        (99, "RB2701", 4, 0, 1),
        (30, "rb2701", 4, 0, 1),
        (30, "RB2701xxx", 4, 0, 1),
        (30, "RB2701", 9, 0, 1),
        (30, "RB2701", 4, -1, 1),
        (30, "RB2701", 4, 0, 701),
    ],
)
def test_invalid_requests_fail_before_network(market, code, category, start, count):
    with pytest.raises(ValueError):
        TdxExHqClient().get_instrument_bars(category, market, code, start, count)


@pytest.mark.parametrize(
    "payload,declared",
    [
        (zlib.compress(b"a" * 100000), 1),
        (zlib.compress(b"abc") + b"trailing", 3),
        (zlib.compress(b"abc")[:-1], 3),
        (b"broken", 12),
    ],
)
def test_compressed_stream_boundaries_are_verified(payload, declared):
    response = struct.pack("<IIIHH", 0, 0, 0, len(payload), declared) + payload
    with FakeTdxServer([ReplayStep(HANDSHAKE, response)]) as server:
        with pytest.raises(DataFormatError):
            TdxExHqClient([server.endpoint], retries=0).connect()


def test_disconnect_retries_the_next_configured_node():
    request = bytes.fromhex("01034866000102000200f023")
    success = b"\0" * 19 + struct.pack("<I", 42)
    first = [ReplayStep(HANDSHAKE, envelope(b"OK")), ReplayStep(request, None)]
    second = [ReplayStep(HANDSHAKE, envelope(b"OK")), ReplayStep(request, envelope(success))]
    with FakeTdxServer(first) as bad, FakeTdxServer(second) as good:
        with TdxExHqClient([bad.endpoint, good.endpoint], retries=0) as client:
            assert client.get_instrument_count() == 42
            assert client.endpoint == good.endpoint


def test_truncated_envelope_raises_connection_error():
    with FakeTdxServer([ReplayStep(HANDSHAKE, b"\0" * 8)]) as server:
        with pytest.raises(ConnectionError):
            TdxExHqClient([server.endpoint], retries=0).connect()


def test_invalid_minute_time_and_transaction_seconds_are_not_repaired(monkeypatch):
    client = TdxExHqClient()
    invalid_bar = bar_payload(date_bytes=struct.pack("<HH", 22 * 2048 + 930, 1440))
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: invalid_bar)
    with pytest.raises(DataFormatError):
        client.get_instrument_bars(7, 30, "RB2701", 0, 1)
    invalid_tick = INSTRUMENT + b"\0" * 4 + struct.pack("<HHIIiH", 1, 570, 3111250, 20, -4, 10060)
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: invalid_tick)
    with pytest.raises(DataFormatError):
        client.get_transaction_data(30, "RB2701", 0, 1)


def test_server_config_is_standard_library_json_compatible_yaml(tmp_path):
    path = tmp_path / "servers.yaml"
    path.write_text(json.dumps({"servers": [{"host": "127.0.0.1", "port": 7727}]}), encoding="utf-8")
    assert load_tdx_servers(path) == [("127.0.0.1", 7727)]
