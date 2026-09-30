"""Windows 原子心跳替换读竞争不应误判；持续不可读和损坏仍明确失败。"""

import json
from pathlib import Path

import pytest

from qh_trader.monitor.heartbeat import REPLACE_ATTEMPTS, HeartbeatFile, read_heartbeat


@pytest.mark.parametrize("failures", [2, REPLACE_ATTEMPTS])
def test_read_heartbeat_retries_transient_sharing_violations_only(tmp_path, monkeypatch, failures):
    writer = HeartbeatFile(tmp_path / "beat.json", role="execution", instance_id="fixture")
    expected = writer.beat(control_epoch=1, ready=True)
    original = Path.read_text
    calls = []

    def read(path, *args, **kwargs):
        if path == writer.path:
            calls.append(path)
            if len(calls) <= failures:
                raise PermissionError("fixture sharing violation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    if failures == REPLACE_ATTEMPTS:
        with pytest.raises(PermissionError):
            read_heartbeat(writer.path)
        assert len(calls) == REPLACE_ATTEMPTS
    else:
        assert read_heartbeat(writer.path) == expected
        assert len(calls) == failures + 1


def test_missing_and_corrupt_heartbeat_are_not_made_live(tmp_path):
    path = tmp_path / "missing.json"
    assert read_heartbeat(path) is None
    path.write_text("{incomplete", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        read_heartbeat(path)
