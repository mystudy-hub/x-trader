"""[Gateway 层] 终端采集与看穿式监管上报 (S5-03, FR-LIVE-02, FR-LIVE-03, A27).

按 CTP 官方看穿式监管规范（FR-LIVE-02），提供 Windows / Linux 下匹配的终端信息采集
组件（WinDataCollect.dll / libDataCollect.so）加载、系统特征采集与脱敏摘要校验。

硬约束：
- ReqAuthenticate 用于客户端认证，不能等同于终端信息上报（FR-LIVE-02）。
- 直连与中继模式按官方规范与期货公司联调确定：直连模式核验采集库并载入进程，中继模式构造
  CThostFtdcUserSystemInfoField 载荷并调用 RegisterUserSystemInfo（A27）。
- 采集载荷按 API 定义的字节及长度传递，绝不手工拼接 MAC/IP 或做普通字符串截断（FR-LIVE-02）。
- 缺库、版本不匹配、采集失败必须阻止进入可交易状态（A27, F17）。
- 日志与快照绝不落原始硬件指纹或二进制载荷，只记录脱敏摘要与哈希前缀（NFR-06）。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import platform
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class TerminalAccessMode(str, Enum):
    """看穿式终端接入模式 (A27)."""

    NONE = "none"  # 未启用看穿式采集（离线或纯回测模拟）
    DIRECT = "direct"  # 直连模式：个人/独立程序化接入，由底层 CTP 运行时加载采集库并上报
    RELAY = "relay"  # 中继模式：在认证后、登录前显式调用 RegisterUserSystemInfo


class TerminalInfoError(RuntimeError):
    """终端信息采集组件缺失、加载失败、版本不符或采集异常 (A27, F17)."""


@dataclass(frozen=True, slots=True)
class TerminalCollectorReport:
    """采集组件与特征采集脱敏证据 (NFR-06, A27).

    绝不记录原始采集二进制载荷或明文硬件序列号，只暴露哈希前缀与长度。
    """

    collector_dll: str
    dll_hash: str
    api_version: str
    system_info_len: int
    status_code: int
    is_complete: bool
    collected_at: str
    masked_digest: str
    access_mode: str
    notes: tuple[str, ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {
            "collector_dll": self.collector_dll,
            "dll_hash": self.dll_hash,
            "api_version": self.api_version,
            "system_info_len": self.system_info_len,
            "status_code": self.status_code,
            "is_complete": self.is_complete,
            "collected_at": self.collected_at,
            "masked_digest": self.masked_digest,
            "access_mode": self.access_mode,
            "notes": list(self.notes),
        }

    def __repr__(self) -> str:
        return (
            f"TerminalCollectorReport(collector_dll={self.collector_dll!r}, "
            f"api_version={self.api_version!r}, len={self.system_info_len}, "
            f"digest={self.masked_digest!r}, mode={self.access_mode!r})"
        )


@dataclass(frozen=True, slots=True)
class TerminalInfoPayload:
    """采集到的内存载荷（仅在内部传递，防泄漏）."""

    raw_bytes: bytes
    length: int
    report: TerminalCollectorReport

    def __repr__(self) -> str:
        return f"<TerminalInfoPayload len={self.length} digest={self.report.masked_digest}>"


# 符号解析候选（处理 MSVC C++ 名称修饰 与 C 命名导出）
_VERSION_SYMBOLS = (
    "?CTP_GetDataCollectApiVersion@@YAPEBDXZ",  # MSVC 64-bit C++ mangled
    "CTP_GetDataCollectApiVersion",  # C / extern "C"
    "_CTP_GetDataCollectApiVersion@0",  # 32-bit stdcall
)

_SYSTEM_INFO_SYMBOLS = (
    "?CTP_GetSystemInfo@@YAHPEADAEAH@Z",  # MSVC 64-bit C++ mangled
    "CTP_GetSystemInfo",  # C / extern "C"
    "_CTP_GetSystemInfo@8",  # 32-bit stdcall
)


class TerminalInfoCollector:
    """看穿式监管采集库适配器 (FR-LIVE-02, A27)."""

    def __init__(
        self,
        *,
        lib_path: str | Path | None = None,
        access_mode: TerminalAccessMode = TerminalAccessMode.DIRECT,
        public_ip: str | None = None,
        ip_port: int | None = None,
        search_paths: Sequence[str | Path] | None = None,
    ) -> None:
        self.lib_path = Path(lib_path).resolve() if lib_path else None
        self.access_mode = (
            access_mode if isinstance(access_mode, TerminalAccessMode) else TerminalAccessMode(str(access_mode))
        )
        self.public_ip = public_ip
        self.ip_port = ip_port
        self._search_paths = tuple(Path(p).resolve() for p in (search_paths or ()))

    def find_library(self) -> Path | None:
        """寻找匹配操作系统的官方采集动态库."""
        if self.lib_path is not None:
            return self.lib_path if self.lib_path.is_file() else None

        env_path = os.environ.get("QH_CTP_DATA_COLLECT_LIB")
        if env_path and Path(env_path).is_file():
            return Path(env_path).resolve()

        dll_name = "WinDataCollect.dll" if platform.system() == "Windows" else "libDataCollect.so"
        candidates: list[Path] = list(self._search_paths)

        # 常用项目位置探测
        root = Path(__file__).resolve().parents[2]
        candidates.extend(
            [
                root / "libs" / dll_name,
                root / "config" / dll_name,
                root / "docs/sinnow/6.7.13_apidemo/demo" / dll_name,
                Path.cwd() / dll_name,
            ]
        )

        # Python site-packages 内部探测
        try:
            import openctp_ctp

            pkg_dir = Path(openctp_ctp.__file__).parent
            candidates.append(pkg_dir / dll_name)
            candidates.append(pkg_dir.parent / "openctp_ctp.libs" / dll_name)
        except ImportError:
            pass

        for p in candidates:
            if p.is_file():
                return p.resolve()
        return None

    def collect(self) -> TerminalInfoPayload:
        """执行硬件特征采集，生成脱敏摘要与内部载荷."""
        path = self.find_library()
        if path is None:
            raise TerminalInfoError(
                "看穿式监管采集库未找到 (A27: 缺库阻止进入交易状态)。"
                "请配置 QH_CTP_DATA_COLLECT_LIB 或将 WinDataCollect.dll 放置于工程搜索路径。"
            )

        try:
            content = path.read_bytes()
            dll_hash = hashlib.sha256(content).hexdigest()
        except Exception as exc:
            raise TerminalInfoError(f"读取采集库文件失败: {path} ({exc})") from exc

        try:
            cdll = ctypes.cdll.LoadLibrary(str(path))
        except Exception as exc:
            raise TerminalInfoError(f"动态加载采集库失败: {path} ({exc})") from exc

        # 1. 读取采集库版本
        get_ver_fn = None
        for sym in _VERSION_SYMBOLS:
            if hasattr(cdll, sym):
                get_ver_fn = getattr(cdll, sym)
                break
        if get_ver_fn is None:
            raise TerminalInfoError(f"采集库未导出 CTP_GetDataCollectApiVersion 符号: {path}")

        get_ver_fn.restype = ctypes.c_char_p
        try:
            raw_ver = get_ver_fn()
            api_ver = raw_ver.decode("gbk", errors="ignore") if raw_ver else "unknown"
        except Exception as exc:
            raise TerminalInfoError(f"调用 CTP_GetDataCollectApiVersion 失败: {exc}") from exc

        # 2. 采集系统信息
        get_info_fn = None
        for sym in _SYSTEM_INFO_SYMBOLS:
            if hasattr(cdll, sym):
                get_info_fn = getattr(cdll, sym)
                break
        if get_info_fn is None:
            raise TerminalInfoError(f"采集库未导出 CTP_GetSystemInfo 符号: {path}")

        get_info_fn.restype = ctypes.c_int
        buf = ctypes.create_string_buffer(512)
        n_len = ctypes.c_int(512)
        try:
            status_code = get_info_fn(buf, ctypes.byref(n_len))
        except Exception as exc:
            raise TerminalInfoError(f"调用 CTP_GetSystemInfo 异常: {exc}") from exc

        collected_len = n_len.value
        # CTP 官方字段容量限制为 344 字节 (ClientSystemInfo)
        if collected_len <= 0 or collected_len > 344:
            raise TerminalInfoError(f"采集载荷长度非法 (长度: {collected_len}, 要求 1~344 字节)")

        raw_bytes = buf.raw[:collected_len]
        payload_hash = hashlib.sha256(raw_bytes).hexdigest()
        masked_digest = f"sha256:{payload_hash[:16]}"
        is_complete = status_code == 0

        notes: list[str] = []
        if status_code != 0:
            notes.append(f"collector_partial_bitmask:0x{status_code:x}")

        report = TerminalCollectorReport(
            collector_dll=str(path),
            dll_hash=dll_hash,
            api_version=api_ver,
            system_info_len=collected_len,
            status_code=status_code,
            is_complete=is_complete,
            collected_at=datetime.now(timezone.utc).isoformat(),
            masked_digest=masked_digest,
            access_mode=self.access_mode.value,
            notes=tuple(notes),
        )
        return TerminalInfoPayload(raw_bytes=raw_bytes, length=collected_len, report=report)


def apply_relay_user_system_info(
    field: Any,
    payload: TerminalInfoPayload,
    *,
    broker_id: str,
    user_id: str,
    app_id: str,
    public_ip: str | None = None,
    ip_port: int | None = None,
) -> None:
    """将采集载荷精确注入 CThostFtdcUserSystemInfoField (中继模式 A27)."""
    field.BrokerID = broker_id
    field.UserID = user_id
    field.ClientAppID = app_id
    field.ClientSystemInfoLen = payload.length

    # 优先使用 memmove 进行字节级直接内存写入，避免 string 编码截断 null 字节
    written = False
    ptr = getattr(field, "this", None)
    if ptr is not None:
        try:
            # 在 MSVC x64 CThostFtdcUserSystemInfoField 结构中：
            # BrokerID(11) + UserID(16) + pad(1) + ClientSystemInfoLen(4) -> offset 32
            addr = int(ptr)
            ctypes.memmove(addr + 32, payload.raw_bytes, payload.length)
            written = True
        except Exception:
            written = False

    if not written:
        # 回退安全方案：通过 latin1 保持 1-to-1 字节映射
        field.ClientSystemInfo = payload.raw_bytes.decode("latin1")

    if public_ip:
        field.ClientPublicIP = public_ip
    if ip_port:
        field.ClientIPPort = int(ip_port)
    field.ClientLoginTime = datetime.now(timezone.utc).strftime("%H:%M:%S")
