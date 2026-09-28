"""S0-02 原生库装载：绑定期望文件名、摘要校验、预装载顺序与风味核验 (GAP-S0-01).

这些用例只用临时目录里的假库：真实 TTS 原生库的装载与前置连通性属于联调证据
(``runs/s5/ctp_runtime_evidence_*.json``)，不在单元测试里断言。
"""

from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

import pytest

from qh_trader.gateway import ctp_native_libs
from qh_trader.gateway.ctp_native_libs import (
    NATIVE_LIB_BASENAMES,
    NativeLibError,
    NativeLibSpec,
    ensure_native_libs,
    expected_library_names,
    native_lib_suffix,
    preload_native_libs,
    stage_native_libs,
    verify_api_marker,
    verify_staged_flavor,
)
from scripts import ctp_setup

SUFFIX = native_lib_suffix()
TRADER_LIB = f"{NATIVE_LIB_BASENAMES[0]}{SUFFIX}"
MARKET_LIB = f"{NATIVE_LIB_BASENAMES[1]}{SUFFIX}"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_binding_libs(root: Path, *, trader_name: str | None = None, market_name: str | None = None) -> Path:
    """复刻 wheel 的 ``openctp_ctp.libs``：库名带 delvewheel 摘要后缀."""
    directory = root / "openctp_ctp.libs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (trader_name or f"thosttraderapi_se-deadbeef{SUFFIX}")).write_bytes(b"binding trader")
    (directory / (market_name or f"thostmduserapi_se-feedface{SUFFIX}")).write_bytes(b"binding market")
    return directory


def write_source_libs(root: Path, *, trader: bytes = b"tts trader", market: bytes = b"tts market") -> tuple[Path, dict]:
    directory = root / "vendor"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / TRADER_LIB).write_bytes(trader)
    (directory / MARKET_LIB).write_bytes(market)
    return directory, {TRADER_LIB: _sha256(trader), MARKET_LIB: _sha256(market)}


def make_spec(root: Path, source: Path, digests: dict[str, str], **overrides) -> NativeLibSpec:
    values = {
        "flavor": "openctp-tts",
        "api_marker": "openctp-tts",
        "source_dir": str(source),
        "staging_dir": str(root / "runs" / "ctp_native" / "openctp-tts"),
        "digests": tuple(sorted(digests.items())),
    }
    values.update(overrides)
    return NativeLibSpec(**values)


def test_loader_names_are_discovered_from_the_binding_layout(tmp_path: Path):
    directory = write_binding_libs(tmp_path)
    names = expected_library_names(directory, suffix=SUFFIX)
    assert names == {
        NATIVE_LIB_BASENAMES[0]: f"thosttraderapi_se-deadbeef{SUFFIX}",
        NATIVE_LIB_BASENAMES[1]: f"thostmduserapi_se-feedface{SUFFIX}",
    }


def test_missing_or_ambiguous_binding_libraries_are_refused(tmp_path: Path):
    with pytest.raises(NativeLibError, match="does not exist"):
        expected_library_names(tmp_path / "absent", suffix=SUFFIX)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(NativeLibError, match="exactly one"):
        expected_library_names(empty, suffix=SUFFIX)
    duplicate = write_binding_libs(tmp_path / "two")
    (duplicate / f"thosttraderapi_se-duplicate{SUFFIX}").write_bytes(b"second binding trader")
    with pytest.raises(NativeLibError, match="exactly one"):
        expected_library_names(duplicate, suffix=SUFFIX)


def test_staging_copies_the_registered_libraries_under_the_loader_names(tmp_path: Path):
    directory = write_binding_libs(tmp_path)
    source, digests = write_source_libs(tmp_path)
    staged = stage_native_libs(make_spec(tmp_path, source, digests), libs_dir=directory, suffix=SUFFIX)

    assert staged.flavor == "openctp-tts" and staged.loaded is False
    by_basename = {item.basename: item for item in staged.files}
    trader = by_basename[NATIVE_LIB_BASENAMES[0]]
    assert trader.expected_name == f"thosttraderapi_se-deadbeef{SUFFIX}"
    assert trader.sha256 == digests[TRADER_LIB]
    assert Path(trader.path).read_bytes() == b"tts trader"
    # 只改暂存目录：绑定自带的库保持不变
    assert (directory / trader.expected_name).read_bytes() == b"binding trader"


def test_an_unregistered_digest_refuses_to_load(tmp_path: Path):
    directory = write_binding_libs(tmp_path)
    source, digests = write_source_libs(tmp_path)
    digests.pop(MARKET_LIB)
    with pytest.raises(NativeLibError, match="no registered sha256"):
        stage_native_libs(make_spec(tmp_path, source, digests), libs_dir=directory, suffix=SUFFIX)


def test_a_digest_mismatch_refuses_to_load(tmp_path: Path):
    directory = write_binding_libs(tmp_path)
    source, digests = write_source_libs(tmp_path)
    digests[TRADER_LIB] = "0" * 64
    with pytest.raises(NativeLibError, match="does not match the registered"):
        stage_native_libs(make_spec(tmp_path, source, digests), libs_dir=directory, suffix=SUFFIX)
    assert not (tmp_path / "runs" / "ctp_native" / "openctp-tts" / f"thosttraderapi_se-deadbeef{SUFFIX}").exists()


def test_a_missing_source_directory_or_file_refuses_to_load(tmp_path: Path):
    directory = write_binding_libs(tmp_path)
    _, digests = write_source_libs(tmp_path / "unused")
    spec = make_spec(tmp_path, tmp_path / "absent", digests)
    with pytest.raises(NativeLibError, match="source directory does not exist"):
        stage_native_libs(spec, libs_dir=directory, suffix=SUFFIX)

    source = tmp_path / "partial"
    source.mkdir()
    (source / TRADER_LIB).write_bytes(b"tts trader")
    with pytest.raises(NativeLibError, match="is missing"):
        stage_native_libs(make_spec(tmp_path, source, digests), libs_dir=directory, suffix=SUFFIX)


def test_preloading_happens_once_and_records_the_loaded_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    directory = write_binding_libs(tmp_path)
    source, digests = write_source_libs(tmp_path)
    monkeypatch.delitem(sys.modules, "openctp_ctp", raising=False)
    loaded: list[str] = []
    spec = make_spec(tmp_path, source, digests)

    first = ensure_native_libs(spec, libs_dir=directory, suffix=SUFFIX, loader=loaded.append)
    second = ensure_native_libs(spec, libs_dir=directory, suffix=SUFFIX, loader=loaded.append)

    assert first.loaded is True and second is first
    assert [Path(path).name for path in loaded] == [
        f"thosttraderapi_se-deadbeef{SUFFIX}",
        f"thostmduserapi_se-feedface{SUFFIX}",
    ]
    assert set(first.preloaded_hashes()) == {Path(path).name for path in loaded}
    verify_staged_flavor(first, "openctp-tts v6.7.11")


def test_a_changed_registration_is_not_reused_from_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """同一暂存目录但源库 / 摘要不同时必须重新装载，不能拿旧记录充当已装载."""
    directory = write_binding_libs(tmp_path)
    source, digests = write_source_libs(tmp_path)
    monkeypatch.delitem(sys.modules, "openctp_ctp", raising=False)
    monkeypatch.setattr(ctp_native_libs, "_PRELOADED", {})
    loaded: list[str] = []
    ensure_native_libs(make_spec(tmp_path, source, digests), libs_dir=directory, suffix=SUFFIX, loader=loaded.append)

    (source / TRADER_LIB).write_bytes(b"different tts trader")
    changed = dict(digests, **{TRADER_LIB: _sha256(b"different tts trader")})
    ensure_native_libs(make_spec(tmp_path, source, changed), libs_dir=directory, suffix=SUFFIX, loader=loaded.append)
    assert len(loaded) == 4


def test_a_second_binding_reuses_the_libraries_already_loaded_in_this_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """交易与行情两个绑定在同一进程里共用一套原生库：第二个绑定不应报导入顺序错误。"""
    directory = write_binding_libs(tmp_path)
    source, digests = write_source_libs(tmp_path)
    spec = make_spec(tmp_path, source, digests)
    monkeypatch.delitem(sys.modules, "openctp_ctp", raising=False)
    monkeypatch.setattr(ctp_native_libs, "_PRELOADED", {})
    loaded: list[str] = []
    first = ensure_native_libs(spec, libs_dir=directory, suffix=SUFFIX, loader=loaded.append)
    monkeypatch.setitem(sys.modules, "openctp_ctp", types.ModuleType("openctp_ctp"))
    second = ensure_native_libs(spec, libs_dir=directory, suffix=SUFFIX, loader=loaded.append)
    assert second is first and len(loaded) == 2


def test_preloading_after_the_binding_import_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    directory = write_binding_libs(tmp_path)
    source, digests = write_source_libs(tmp_path)
    staged = stage_native_libs(make_spec(tmp_path, source, digests), libs_dir=directory, suffix=SUFFIX)
    monkeypatch.setitem(sys.modules, "openctp_ctp", types.ModuleType("openctp_ctp"))
    with pytest.raises(NativeLibError, match="already imported"):
        preload_native_libs(staged, loader=lambda path: None)


def test_the_api_marker_must_appear_in_get_api_version():
    verify_api_marker(flavor="openctp-tts", api_marker="openctp-tts", api_version="openctp-tts v6.7.11")
    with pytest.raises(NativeLibError) as excinfo:
        verify_api_marker(flavor="openctp-tts", api_marker="openctp-tts", api_version="v6.7.13_20260225 14:16:30.12079")
    # 症状判据必须写进错误里，否则只会被当成网络故障
    assert "4097" in str(excinfo.value)


# --------------------------------------------------------------------------------------- 登记翻译


def _profile(native_libs: object) -> dict:
    return {"profile_name": "openctp_tts", "native_libs": native_libs}


def test_registration_is_translated_into_a_native_lib_spec(tmp_path: Path):
    spec = ctp_setup.native_libs_spec(
        _profile(
            {
                "flavor": "openctp-tts",
                "api_marker": "openctp-tts",
                "staging_dir": "runs/ctp_native/openctp-tts",
                "archive": {"url": "http://example.invalid/tts.zip", "sha256": "c" * 64},
                "platforms": {
                    "win64": {
                        "source_dir": "vendor/ctp/openctp_tts_6.7.11/win64",
                        "digests": {TRADER_LIB: "d" * 64, MARKET_LIB: "e" * 64},
                    }
                },
            }
        ),
        platform_name="win32",
        root=tmp_path,
    )
    assert spec is not None
    assert spec.source_dir == str((tmp_path / "vendor/ctp/openctp_tts_6.7.11/win64").resolve())
    assert dict(spec.digests) == {TRADER_LIB: "d" * 64, MARKET_LIB: "e" * 64}
    assert spec.archive_sha256 == "c" * 64
    assert (
        ctp_setup.profile_summary(_profile({"flavor": "openctp-tts", "api_marker": "openctp-tts"}))[
            "native_libs_api_marker"
        ]
        == "openctp-tts"
    )


def test_openctp_tts_registration_pins_the_tts_native_library():
    spec = ctp_setup.native_libs_spec(ctp_setup.load_broker_profile("openctp_tts"), platform_name="win64")
    assert spec is not None and spec.flavor == "openctp-tts" and spec.api_marker == "openctp-tts"
    assert Path(spec.source_dir).name == "win64"
    assert dict(spec.digests)[TRADER_LIB] == "b09a8088786f9c919b92bdbfc40344b1435a9f345704ea964ab0bb51ec1a6939"
    assert spec.archive_url and spec.archive_url.endswith("tts_6.7.11.zip")


def test_absent_registration_keeps_the_binding_libraries():
    assert ctp_setup.native_libs_spec({"profile_name": "simnow_v6"}) is None


def test_unregistered_platform_and_incomplete_registration_fail(tmp_path: Path):
    with pytest.raises(ctp_setup.BrokerProfileError, match="platform"):
        ctp_setup.native_lib_platform("darwin")
    with pytest.raises(ctp_setup.BrokerProfileError, match="no entry for win64"):
        ctp_setup.native_libs_spec(_profile({"flavor": "x", "api_marker": "x", "platforms": {}}), platform_name="win32")
    with pytest.raises(ctp_setup.BrokerProfileError, match="digests"):
        ctp_setup.native_libs_spec(
            _profile({"flavor": "x", "api_marker": "x", "platforms": {"win64": {"source_dir": "vendor"}}}),
            platform_name="win32",
            root=tmp_path,
        )
