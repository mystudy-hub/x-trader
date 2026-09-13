"""离线环境采集的文件级 SQLite 与归档哈希验证。"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module():
    spec = importlib.util.spec_from_file_location("environment_checks", ROOT / "scripts/init_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_probe_checks_a_file_and_enforces_foreign_keys(tmp_path):
    report = load_module().check_sqlite(tmp_path)
    assert report["passed"] is True
    assert report["journal_mode"] == "wal"
    assert report["synchronous"] == 2
    assert report["foreign_keys"] == 1
    assert report["foreign_key_enforced"] is True
    assert report["durability_fault_tests"] == "not_executed"


def test_sdk_archive_and_native_members_have_hashes(tmp_path):
    module = load_module()
    directory = tmp_path / "docs/simnow"
    directory.mkdir(parents=True)
    path = directory / "6.7.13_fixture.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("api.dll", b"synthetic dll bytes; never executed")
        archive.writestr("notes.txt", b"not a native member")
    report = module.inspect_sdk_archives(tmp_path)
    assert report[0]["sha256"] == module.get_file_hash(path)
    assert report[0]["version_hint_from_filename"] == "6.7.13"
    assert [entry["name"] for entry in report[0]["members"]] == ["api.dll"]
    assert report[0]["status"] == "INVENTORIED"
