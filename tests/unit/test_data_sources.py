"""Public-source parsing, strict identities and transport failures; no network in unit tests."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from qh_trader.core.constants import Exchange
from qh_trader.core.objects import InstrumentId
from qh_trader.data.sources import (
    AkShareDataSource,
    SinaFuturesDataSource,
    create_data_source,
    normalize_instrument_to_symbol,
)


def test_normalize_instrument_to_symbol_without_year_guessing():
    assert normalize_instrument_to_symbol(InstrumentId(Exchange.SHFE, "rb2410")) == ("rb2410", "SHFE")
    assert normalize_instrument_to_symbol("CZCE.TA1405") == ("TA1405", "CZCE")
    with pytest.raises(ValueError, match="historical catalog"):
        normalize_instrument_to_symbol("CZCE.TA405")
    with pytest.raises(ValueError):
        normalize_instrument_to_symbol("rb0")


def test_create_data_source():
    assert isinstance(create_data_source("sina"), SinaFuturesDataSource)
    assert isinstance(create_data_source("akshare"), AkShareDataSource)
    with pytest.raises(ValueError, match="unknown"):
        create_data_source("unknown")


def test_sina_parse_daily_bars_preserves_missing_fields():
    source = SinaFuturesDataSource()
    raw = '([{"d":"2024-09-09","o":"100","h":"102","l":"99","c":"101","v":"10","p":"100","s":"100.5"}])'
    with patch.object(source, "_http_get", return_value=raw):
        row = source.fetch_daily_bars("SHFE.rb2410")[0]
    assert row["open"] == Decimal(100)
    assert row["volume"] == 10
    assert row["settlement_price"] == Decimal("100.5")
    assert row["turnover"] is None
    with patch.object(source, "_http_get", return_value=raw.replace('"v":"10"', '"v":"1.9"')):
        with pytest.raises(ValueError, match="int64"):
            source.fetch_daily_bars("SHFE.rb2410")


def test_sina_minute_end_date_is_inclusive_and_interval_cannot_fall_back():
    source = SinaFuturesDataSource()
    raw = '[{"d":"2024-09-09 14:15:00","o":"100","h":"100","l":"100","c":"100","v":"10","p":"100"}]'
    with patch.object(source, "_http_get", return_value=raw):
        assert len(source.fetch_minute_bars("SHFE.rb2410", end_time="2024-09-09")) == 1
        assert len(source.fetch_minute_bars("SHFE.rb2410", end_time="2024-09-08")) == 0
        assert len(source.fetch_minute_bars("SHFE.rb2410", end_time=datetime(2024, 9, 9, 7, tzinfo=timezone.utc))) == 1
        with pytest.raises(ValueError, match="unsupported"):
            source.fetch_minute_bars("SHFE.rb2410", period="2h")


def test_realtime_parsing_rejects_malformed_prices_without_name_error():
    source = SinaFuturesDataSource()
    raw = 'var hq_str_nf_RB2410="name,150000,100,102,99,100,100,101,101,100,99,10,20,100,10,SHFE,rb,2024-09-09";'
    with patch.object(source, "_http_get", return_value=raw):
        result = source.fetch_latest_tick("SHFE.rb2410")
    assert result["last_price"] == Decimal(101)
    assert result["receive_time"].tzinfo is timezone.utc
    with patch.object(source, "_http_get", return_value=raw.replace(",100,102,", ",bad,102,")):
        assert source.fetch_latest_tick("SHFE.rb2410") is None


def test_transport_retries_https_without_disabling_certificate_checks():
    source = SinaFuturesDataSource(timeout=1)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"[]"
    with patch(
        "qh_trader.data.sources.urllib.request.urlopen", side_effect=[URLError("temporary"), response]
    ) as request:
        with patch("qh_trader.data.sources.time.sleep"):
            assert source._http_get("https://example.invalid/data") == "[]"
    assert request.call_count == 2
    assert all(call.args[0].full_url.startswith("https://") for call in request.call_args_list)
    assert all("context" not in call.kwargs for call in request.call_args_list)
    assert source.captures[0]["body"] == "[]"
    with pytest.raises(ValueError, match="HTTPS"):
        source._http_get("http://example.invalid/data")


def test_client_http_error_is_not_retried_as_plaintext():
    source = SinaFuturesDataSource()
    failure = HTTPError("https://example.invalid", 404, "missing", {}, None)
    with patch("qh_trader.data.sources.urllib.request.urlopen", side_effect=failure) as request:
        with pytest.raises(HTTPError):
            source._http_get("https://example.invalid")
    assert request.call_count == 1
