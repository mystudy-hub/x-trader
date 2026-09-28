"""[Gateway 层] 原生 CTP 动态库的选择、暂存、预装载与核验 (S0-02, S5-01, FR-LIVE-01/04, GAP-S0-01).

``openctp-ctp`` 的 Python 绑定（``_thosttraderapi.pyd`` / ``_thostmduserapi.pyd``）按**固定文件名**
加载原生库：delvewheel 把库重命名为 ``thosttraderapi_se-<摘要>.dll`` 放在 ``openctp_ctp.libs``，
绑定按该名字取库。因此"换一套原生库"不是换一个搜索目录：必须先把目标库按绑定期望的文件名放到
一处，并在**导入绑定之前**预装载进进程（Windows 的 LoadLibrary 会按基名复用已装载模块）。

openctp 模拟环境（TTS 系统）与 SimNow 等 CTP 原厂柜台需要**不同的原生库**：连 TTS 前置必须用 TTS 版
CTPAPI 兼容库，用官方库的症状是 `OnFrontDisconnected(4097)` 或柜台回『不合法的登录』。本模块把这件事
变成可核验的工程步骤：

1. 发现绑定期望的文件名（``thosttraderapi_se-<摘要>.dll`` / ``.so``）；
2. 按柜台登记校验源库摘要，并复制到**项目内私有暂存目录**（不改 ``site-packages``，可重复执行）；
3. 在导入绑定前预装载暂存库；
4. 用登记的 ``api_marker`` 复核 ``GetApiVersion()`` —— 例如 TTS 库自报 ``openctp-tts v6.7.11``。

未登记摘要、缺文件、摘要不符、导入顺序颠倒或标记不符一律抛 :class:`NativeLibError`，绝不静默
回退到绑定自带库或猜测柜台行为（GAP-S0-01）。
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: 绑定需要的原生库基名；实际文件名由 delvewheel 加摘要后缀，必须在 wheel 的 ``.libs`` 目录里发现。
NATIVE_LIB_BASENAMES: tuple[str, ...] = ("thosttraderapi_se", "thostmduserapi_se")

PACKAGE_NAME = "openctp_ctp"
LIBS_DIR_NAME = "openctp_ctp.libs"


class NativeLibError(RuntimeError):
    """原生库登记、摘要、装载顺序或版本标记不成立；不做静默回退。"""


def native_lib_suffix(platform_name: str | None = None) -> str:
    """按平台给出原生库扩展名；未知平台明确失败，不猜。"""
    name = platform_name or sys.platform
    if name == "win32":
        return ".dll"
    if name.startswith("linux"):
        return ".so"
    raise NativeLibError(f"unsupported platform for CTP native libraries: {name!r}")


def binding_libs_dir() -> Path:
    """绑定 wheel 自带的原生库目录 (``openctp_ctp.libs``)."""
    spec = importlib.util.find_spec(PACKAGE_NAME)
    if spec is None or spec.origin is None:
        raise NativeLibError(f"{PACKAGE_NAME} is not installed; cannot locate its native library directory")
    return Path(spec.origin).resolve().parent.parent / LIBS_DIR_NAME


def expected_library_names(libs_dir: Path | None = None, *, suffix: str | None = None) -> Mapping[str, str]:
    """``{基名: 绑定按此名字加载的文件名}``；每个基名必须恰好匹配一个文件。"""
    directory = Path(libs_dir) if libs_dir is not None else binding_libs_dir()
    if not directory.is_dir():
        raise NativeLibError(f"the CTP binding's native library directory does not exist: {directory}")
    extension = suffix or native_lib_suffix()
    found: dict[str, str] = {}
    for basename in NATIVE_LIB_BASENAMES:
        matches = sorted(path.name for path in directory.glob(f"{basename}-*{extension}"))
        if len(matches) != 1:
            raise NativeLibError(
                f"expected exactly one {basename}-*{extension} in {directory}, found {matches or 'none'}"
            )
        found[basename] = matches[0]
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class NativeLibSpec:
    """柜台登记里的原生库选择：用哪一套库、自报什么版本、逐文件摘要是什么."""

    flavor: str
    api_marker: str
    source_dir: str
    staging_dir: str
    digests: tuple[tuple[str, str], ...] = ()
    archive_url: str | None = None
    archive_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in ("flavor", "api_marker", "source_dir", "staging_dir"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"native library {name} is required when a flavour is registered")
        seen: set[str] = set()
        for filename, digest in self.digests:
            if filename in seen:
                raise ValueError(f"native library digest registered twice: {filename}")
            seen.add(filename)
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
                raise ValueError(f"native library digest for {filename} must be a hex sha256")

    def digest_for(self, filename: str) -> str | None:
        for name, digest in self.digests:
            if name == filename:
                return digest.lower()
        return None

    def digest(self, filename: str) -> str:
        """取登记摘要；未登记即失败——未核验的原生库不得装载."""
        digest = self.digest_for(filename)
        if digest is None:
            raise NativeLibError(
                f"native library {filename} has no registered sha256 in the {self.flavor} registration; "
                "register the digest before loading it"
            )
        return digest

    def as_mapping(self) -> dict[str, object]:
        """脱敏登记摘要：只含库标识与摘要，不含凭证."""
        return {
            "flavor": self.flavor,
            "api_marker": self.api_marker,
            "source_dir": self.source_dir,
            "staging_dir": self.staging_dir,
            "archive_url": self.archive_url,
            "archive_sha256": self.archive_sha256,
            "digests": dict(self.digests),
        }


@dataclass(frozen=True, slots=True)
class StagedNativeLib:
    """一个已暂存并预装载的原生库."""

    basename: str
    expected_name: str
    path: str
    bytes: int
    sha256: str

    def as_mapping(self) -> dict[str, object]:
        return {
            "basename": self.basename,
            "loader_name": self.expected_name,
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class StagedNativeLibs:
    """一次成功装载的完整记录，用于会话报告与证据文件."""

    flavor: str
    api_marker: str
    source_dir: str
    staging_dir: str
    files: tuple[StagedNativeLib, ...]
    loaded: bool = False

    def as_mapping(self) -> dict[str, object]:
        return {
            "flavor": self.flavor,
            "api_marker": self.api_marker,
            "source_dir": self.source_dir,
            "staging_dir": self.staging_dir,
            "loaded": self.loaded,
            "files": [item.as_mapping() for item in self.files],
        }

    def preloaded_hashes(self) -> Mapping[str, str]:
        """``{loader 可见文件名: sha256}``；与 wheel 自带库同名，调用方须加前缀区分."""
        return {item.expected_name: item.sha256 for item in self.files}


def stage_native_libs(
    spec: NativeLibSpec,
    *,
    libs_dir: Path | None = None,
    source_dir: Path | None = None,
    suffix: str | None = None,
) -> StagedNativeLibs:
    """校验源库摘要并按绑定期望的名字复制到暂存目录；不修改 ``site-packages``."""
    extension = suffix or native_lib_suffix()
    names = expected_library_names(libs_dir, suffix=extension)
    source = Path(source_dir) if source_dir is not None else Path(spec.source_dir)
    if not source.is_dir():
        raise NativeLibError(
            f"native library source directory does not exist: {source}; fetch the {spec.flavor} package first"
        )
    staging = Path(spec.staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    files: list[StagedNativeLib] = []
    for basename in NATIVE_LIB_BASENAMES:
        source_path = source / f"{basename}{extension}"
        if not source_path.is_file():
            raise NativeLibError(f"registered native library is missing: {source_path}")
        digest = spec.digest(source_path.name)
        actual = _sha256(source_path)
        if actual != digest:
            raise NativeLibError(f"native library {source_path} sha256 {actual} does not match the registered {digest}")
        target = staging / names[basename]
        if not target.is_file() or _sha256(target) != digest:
            shutil.copyfile(source_path, target)
            if _sha256(target) != digest:
                raise NativeLibError(f"staged native library {target} does not match the registered sha256")
        files.append(
            StagedNativeLib(
                basename=basename,
                expected_name=names[basename],
                path=str(target),
                bytes=target.stat().st_size,
                sha256=digest,
            )
        )
    return StagedNativeLibs(
        flavor=spec.flavor,
        api_marker=spec.api_marker,
        source_dir=str(source),
        staging_dir=str(staging),
        files=tuple(files),
    )


def _default_loader(path: str) -> object:
    if sys.platform == "win32":
        return ctypes.WinDLL(path)
    return ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)


_PRELOADED: dict[tuple[object, ...], StagedNativeLibs] = {}


def _selection_key(staged: StagedNativeLibs) -> tuple[object, ...]:
    """按“暂存目录 + 实际装载的文件摘要”识别一次装载，避免换了源库却复用旧记录."""
    return (staged.staging_dir, tuple((item.expected_name, item.sha256) for item in staged.files))


def preload_native_libs(
    staged: StagedNativeLibs,
    *,
    loader: Callable[[str], object] | None = None,
) -> StagedNativeLibs:
    """在导入绑定之前把暂存库预装载进进程；同一套库在一个进程内只装载一次.

    已经从缓存拿到的选择可以直接复用：交易与行情两个绑定在同一进程里各持一个实例，
    但原生库只有一套（第二个绑定看到的是已装载模块）。
    """
    key = _selection_key(staged)
    cached = _PRELOADED.get(key)
    if cached is not None:
        return cached
    if PACKAGE_NAME in sys.modules:
        raise NativeLibError(
            f"{PACKAGE_NAME} is already imported; {staged.flavor} native libraries must be preloaded before "
            "the first binding import, otherwise the process keeps the binding's own libraries"
        )
    load = loader or _default_loader
    for item in staged.files:
        load(item.path)
    loaded = StagedNativeLibs(
        flavor=staged.flavor,
        api_marker=staged.api_marker,
        source_dir=staged.source_dir,
        staging_dir=staged.staging_dir,
        files=staged.files,
        loaded=True,
    )
    _PRELOADED[key] = loaded
    return loaded


def ensure_native_libs(
    spec: NativeLibSpec,
    *,
    libs_dir: Path | None = None,
    source_dir: Path | None = None,
    suffix: str | None = None,
    loader: Callable[[str], object] | None = None,
) -> StagedNativeLibs:
    """登记 → 暂存（摘要一致则不重写）→ 预装载；一个进程内同一套库只装载一次."""
    staged = stage_native_libs(spec, libs_dir=libs_dir, source_dir=source_dir, suffix=suffix)
    return preload_native_libs(staged, loader=loader)


def verify_api_marker(*, flavor: str, api_marker: str, api_version: str) -> None:
    """复核实际加载的原生库自报版本；不符即拒绝连接，不猜测柜台行为 (GAP-S0-01)."""
    if api_marker.lower() in (api_version or "").lower():
        return
    raise NativeLibError(
        f"native library mismatch: the registration expects {flavor} libraries reporting {api_marker!r} in "
        f"GetApiVersion(), but the binding reports {api_version!r}. Connecting to this counter with the wrong "
        "native library shows up as OnFrontDisconnected 4097 or a counter-side login rejection"
    )


def verify_staged_flavor(staged: StagedNativeLibs, api_version: str) -> None:
    """按暂存记录复核版本标记（会话报告与探针的核验入口）."""
    verify_api_marker(flavor=staged.flavor, api_marker=staged.api_marker, api_version=api_version)
