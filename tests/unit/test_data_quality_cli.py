"""Quality command exit states, immutable reports and explicit missing-input failures."""

import hashlib
import json

import pytest

from scripts.data_reports import load_gap_evidence
from scripts.gaps import main as gaps_main
from scripts.validate_data import main as validate_main


def test_raw_validation_writes_quality_and_quarantine_records_without_changing_input(tmp_path, source_records, capsys):
    data = {
        "schema_version": 1,
        "status": "raw_observation",
        "instrument": "SHFE.rb2410",
        "interval": "1d",
        "source_id": "test",
        "records": [dict(source_records[0], turnover=None)],
        "captures": [],
    }
    original = json.dumps(data).encode("utf-8")
    path = tmp_path / (hashlib.sha256(original).hexdigest() + ".json")
    path.write_bytes(original)
    output = tmp_path / "reports"
    assert validate_main(["--raw", str(path), "--output-dir", str(output)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["passed"] and report["scope"] == "raw_observation"
    assert len(list(output.glob("report-*.json"))) == len(list(output.glob("quarantine-*.json"))) == 1
    assert path.read_bytes() == original
    assert validate_main(["--raw", str(path), "--output-dir", str(output)]) == 1
    capsys.readouterr()
    assert len(list(output.glob("report-*.json"))) == 1


def test_missing_canonical_snapshot_is_not_reported_as_no_gaps(tmp_path, capsys):
    assert (
        gaps_main(
            [
                "--storage-dir",
                str(tmp_path / "empty"),
                "--calendar",
                str(tmp_path / "calendar.json"),
                "--start-day",
                "2024-09-09",
                "--end-day",
                "2024-09-13",
                "--output-dir",
                str(tmp_path / "reports"),
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert not report["passed"] and report["issues"][0]["code"] == "input_error"
    assert not (tmp_path / "empty").exists()


def test_missing_calendar_cannot_fall_back_to_weekdays():
    with pytest.raises(SystemExit) as error:
        gaps_main(["--start-day", "2024-09-09", "--end-day", "2024-09-13"])
    assert error.value.code == 2


def test_gap_cause_proof_must_match_bytes(tmp_path):
    proof = tmp_path / "proof.txt"
    proof.write_text("no trades observed", encoding="utf-8")
    document = {
        "schema_version": 1,
        "windows": [
            {
                "exchange": "SHFE",
                "symbol": "rb2410",
                "trading_day": "2024-09-09",
                "start": "2024-09-09T09:00:00+08:00",
                "end": "2024-09-09T10:00:00+08:00",
                "kind": "no_trades",
                "source_id": "fixture",
                "proof": {"path": "proof.txt", "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()},
            }
        ],
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_gap_evidence(path, root=tmp_path)[0].kind == "no_trades"
    proof.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        load_gap_evidence(path, root=tmp_path)


def test_registered_open_gaps_remain_unresolved(tmp_path, capsys):
    path = tmp_path / "gaps.yaml"
    path.write_text(
        "schema_version: 1\ngaps:\n- gap_id: GAP-X\n  status: 未关闭\n  item: sample data\n", encoding="utf-8"
    )
    assert gaps_main(["--registry", str(path), "--output-dir", str(tmp_path / "reports")]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["scope"] == "preparation_gap_registry"
    assert report["unresolved"][0]["gap_id"] == "GAP-X"


def test_closed_gap_without_evidence_cannot_report_success(tmp_path, capsys):
    path = tmp_path / "gaps.yaml"
    path.write_text(
        "schema_version: 1\ngaps:\n- gap_id: GAP-X\n  status: 已关闭\n  closed_at: '2024-09-09'\n", encoding="utf-8"
    )
    assert gaps_main(["--registry", str(path), "--output-dir", str(tmp_path / "reports")]) == 1
    assert not json.loads(capsys.readouterr().out)["passed"]
