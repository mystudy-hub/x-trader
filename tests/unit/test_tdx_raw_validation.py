"""TDX 二进制原始帧与既有文本捕获均须通过内容哈希核验。"""

import hashlib
import json

import pytest

from qh_trader.data.validation import validate_raw_archive


def binary_capture():
    request, response = b"\x01\xff\x00\x23", b"\x02\x00\xfe\x10"
    return {
        "request_hex": request.hex(),
        "response_hex": response.hex(),
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "response_sha256": hashlib.sha256(response).hexdigest(),
    }


def archive(tmp_path, capture, *, turnover=None):
    payload = {
        "schema_version": 1,
        "status": "raw_observation",
        "instrument": "SHFE.rb2701",
        "interval": "1d",
        "source_id": "tdx_exhq",
        "source_metadata": {"research_only": True, "missing_fields": ["turnover", "official_settlement_price"]},
        "records": [
            {
                "date": "2026-09-30",
                "open": "3000",
                "high": "3001",
                "low": "2999",
                "close": "3000",
                "volume": 20,
                "open_interest": 100,
                "turnover": turnover,
            }
        ],
        "captures": [capture],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path = tmp_path / f"{hashlib.sha256(body).hexdigest()}.json"
    path.write_bytes(body)
    return path


def codes(report):
    return {issue.code for issue in report.issues}


def test_binary_capture_is_validated_without_hiding_research_missing_fields(tmp_path):
    report = validate_raw_archive(archive(tmp_path, binary_capture()))
    assert codes(report) == {"INVALID_TURNOVER", "raw_scope_only"}
    assert report.summary["source_metadata"] == {
        "research_only": True,
        "missing_fields": ["turnover", "official_settlement_price"],
    }
    assert report.summary["record_count"] == 1
    assert not report.passed


@pytest.mark.parametrize("direction", ["request", "response"])
def test_tampering_either_binary_frame_is_reported(tmp_path, direction):
    capture = binary_capture()
    capture[f"{direction}_hex"] += "01"
    report = validate_raw_archive(archive(tmp_path, capture))
    assert f"{direction}_hash_mismatch" in codes(report)
    assert "raw_archive_invalid" not in codes(report)


@pytest.mark.parametrize("case", ["missing_hash", "missing_frame", "malformed_hex", "mixed_format"])
def test_incomplete_or_mixed_binary_capture_is_rejected(tmp_path, case):
    capture = binary_capture()
    if case == "missing_hash":
        del capture["request_sha256"]
    elif case == "missing_frame":
        del capture["response_hex"]
    elif case == "malformed_hex":
        capture["response_hex"] = "not hex"
    else:
        capture["body"] = "text"
        capture["sha256"] = hashlib.sha256(b"text").hexdigest()
    assert "raw_archive_invalid" in codes(validate_raw_archive(archive(tmp_path, capture)))


def test_existing_text_capture_still_uses_declared_encoding_and_hash(tmp_path):
    body = "中文行情"
    capture = {"body": body, "encoding": "gbk", "sha256": hashlib.sha256(body.encode("gbk")).hexdigest()}
    report = validate_raw_archive(archive(tmp_path, capture, turnover="60000"))
    assert report.passed and codes(report) == {"raw_scope_only"}
    capture["body"] += "篡改"
    report = validate_raw_archive(archive(tmp_path, capture, turnover="60000"))
    assert codes(report) == {"response_hash_mismatch", "raw_scope_only"}


def test_text_capture_with_missing_hash_is_not_silently_skipped(tmp_path):
    report = validate_raw_archive(archive(tmp_path, {"body": "[]"}))
    assert "raw_archive_invalid" in codes(report)
