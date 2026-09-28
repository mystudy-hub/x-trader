#!/usr/bin/env python
"""[脚本工具] 下载并校验柜台登记要求的一套原生 CTP 库 (S0-02, S5-01, GAP-S0-01).

背景：``openctp_ctp`` 绑定随 wheel 自带官方 CTP 原生库；连 openctp 模拟环境（TTS 系统）必须换用
TTS 版兼容库（官方库的症状是 `OnFrontDisconnected 4097` 或柜台回『不合法的登录』）。本脚本只做
"把登记的那一套库下载下来并逐层校验"，不做替换：装载由 ``gateway/ctp_native_libs.py`` 在导入绑定前
预装载完成，产物只落在 ``vendor/``（git-ignored）。

校验链条（任一层不符即失败，不继续）：

1. 归档 sha256 必须等于登记值；
2. 归档内平台目录下的每个文件 sha256 必须等于登记值；
3. 写盘后再算一次，确认落盘内容与登记一致。

用法::

    uv run --no-sync python scripts/fetch_ctp_native_libs.py --profile openctp_tts
    uv run --no-sync python scripts/fetch_ctp_native_libs.py --profile openctp_tts --check
"""

from __future__ import annotations

import argparse
import hashlib
import socket
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.gateway.ctp_native_libs import NATIVE_LIB_BASENAMES  # noqa: E402
from scripts import ctp_setup  # noqa: E402

VENDOR_DIR = ROOT / "vendor" / "ctp"
DOWNLOAD_TIMEOUT_S = 120.0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "qh-trader-fetch-native-libs/1"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:  # noqa
        payload = response.read()
    target.write_bytes(payload)


def _platform_members(archive: zipfile.ZipFile, platform_key: str, suffix: str) -> dict[str, str]:
    """归档内属于该平台的库成员 ``{落盘文件名: 成员名}``；缺项即失败。"""
    members: dict[str, str] = {}
    for name in archive.namelist():
        parts = Path(name).parts
        if len(parts) < 2 or parts[-2] != platform_key or not name.endswith(suffix):
            continue
        basename = Path(name).stem.split("-")[0]
        if basename in NATIVE_LIB_BASENAMES:
            members[f"{basename}{suffix}"] = name
    missing = [basename for basename in NATIVE_LIB_BASENAMES if f"{basename}{suffix}" not in members]
    if missing:
        raise ctp_setup.BrokerProfileError(f"archive has no {platform_key} members for {missing}")
    return members


def fetch(profile_name: str, *, platform_name: str | None, check_only: bool) -> int:
    profile = ctp_setup.load_broker_profile(profile_name)
    spec = ctp_setup.native_libs_spec(profile, platform_name=platform_name)
    if spec is None:
        print(f"登记 {profile_name} 未登记 native_libs：该环境使用绑定自带库，无需下载", file=sys.stderr)
        return 2
    platform_key = ctp_setup.native_lib_platform(platform_name)
    suffix = ".dll" if platform_key == "win64" else ".so"
    source_dir = Path(spec.source_dir)
    registered = dict(spec.digests)
    print(f"环境 {profile_name} / 原生库 {spec.flavor} / 平台 {platform_key} / 目标目录 {source_dir}")

    if check_only:
        failures = 0
        for basename in NATIVE_LIB_BASENAMES:
            filename = f"{basename}{suffix}"
            path = source_dir / filename
            expected = registered.get(filename)
            if not path.is_file():
                print(f"[缺失] {filename}")
                failures += 1
                continue
            actual = _sha256_file(path)
            if expected is None:
                print(f"[未登记摘要] {filename}")
                failures += 1
            elif actual != expected:
                print(f"[摘要不符] {filename} 本地 {actual} 登记 {expected}")
                failures += 1
            else:
                print(f"[已校验] {filename} {actual}")
        return 1 if failures else 0

    if spec.archive_url is None or spec.archive_sha256 is None:
        raise ctp_setup.BrokerProfileError("native_libs.archive must register both url and sha256 to download it")
    archive_path = VENDOR_DIR / spec.flavor / Path(spec.archive_url).name
    if not archive_path.is_file() or _sha256_file(archive_path) != spec.archive_sha256.lower():
        print(f"正在下载 {spec.archive_url} ...")
        try:
            _download(spec.archive_url, archive_path)
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            print(f"下载失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    actual_archive = _sha256_file(archive_path)
    if actual_archive != spec.archive_sha256.lower():
        archive_path.unlink(missing_ok=True)
        print(f"归档摘要不符：{actual_archive} != 登记 {spec.archive_sha256}", file=sys.stderr)
        return 1
    print(f"[归档已校验] {archive_path.name} {actual_archive}")

    with zipfile.ZipFile(archive_path) as archive:
        members = _platform_members(archive, platform_key, suffix)
        source_dir.mkdir(parents=True, exist_ok=True)
        for filename, member in sorted(members.items()):
            expected = registered.get(filename)
            if expected is None:
                raise ctp_setup.BrokerProfileError(f"native library {filename} has no registered sha256")
            payload = archive.read(member)
            digest = hashlib.sha256(payload).hexdigest()
            if digest != expected:
                raise ctp_setup.BrokerProfileError(
                    f"archive member {member} sha256 {digest} does not match the registered {expected}"
                )
            target = source_dir / filename
            target.write_bytes(payload)
            if _sha256_file(target) != expected:
                raise ctp_setup.BrokerProfileError(
                    f"written native library {target} does not match the registered sha256"
                )
            print(f"[已落盘] {target} {expected}")
    print("原生库就绪：连接时由 gateway/ctp_native_libs.py 在导入绑定前预装载")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="下载并校验柜台登记要求的原生 CTP 库 (S0-02)")
    parser.add_argument("--profile", default="openctp_tts", help="柜台登记名，如 openctp_tts")
    parser.add_argument("--platform", default=None, help="覆盖平台条目（win64 / lin64），默认取当前平台")
    parser.add_argument("--check", action="store_true", help="只校验本地已有的原生库，不下载")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return fetch(args.profile, platform_name=args.platform, check_only=args.check)
    except ctp_setup.BrokerProfileError as exc:
        print(f"登记不成立：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
