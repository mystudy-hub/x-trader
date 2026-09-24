"""[脚本工具] 柜台登记与 CTP 接入参数的装配输入 (S0-02, S5-01, FR-LIVE-01/04).

只做"把登记文件翻译成端口要求的对象"：登记里没有的、或核验状态未成立的项一律翻译成
"未知能力"，绝不填默认值（GAP-S0-05：未核验能力禁用而非猜测）。

秘密只从环境变量读取（``QH_CTP_PASSWORD``、``QH_CTP_AUTH_CODE``），不写入配置文件、日志或证据文件。
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from qh_trader.core.constants import Exchange, Offset
from qh_trader.core.objects import Capability, CapabilityProfile
from qh_trader.gateway.ctp_gateway import CtpOffsetMapping, CtpSettings

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "config/broker_profiles"
PASSWORD_ENV = "QH_CTP_PASSWORD"
AUTH_CODE_ENV = "QH_CTP_AUTH_CODE"
VERIFIED_STATUSES = frozenset({"已核验", "verified"})
UNVERIFIED_EVIDENCE_LEVELS = frozenset({"", "未核验", "假设", "assumption"})


class BrokerProfileError(RuntimeError):
    """登记文件缺失、结构不符或缺少必要项."""


def load_broker_profile(profile: str | None, *, profile_dir: Path | None = None) -> Mapping[str, Any]:
    """按 ``system`` / ``broker.profile`` 名称读取 ``config/broker_profiles`` 下的登记."""
    if not profile:
        raise BrokerProfileError("broker.profile is required for a counter connection")
    if not isinstance(profile, str) or "/" in profile or "\\" in profile or profile.endswith(".yaml"):
        raise BrokerProfileError("broker.profile must be a bare profile name such as simnow_v6")
    path = (profile_dir or PROFILE_DIR) / f"{profile}.yaml"
    if not path.is_file():
        raise BrokerProfileError(f"broker profile not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, Mapping):
        raise BrokerProfileError(f"broker profile must be a YAML mapping: {path}")
    if data.get("profile_name") != profile:
        raise BrokerProfileError(f"broker profile {path.name} declares profile_name {data.get('profile_name')!r}")
    return data


def profile_path(profile: str | None, *, profile_dir: Path | None = None) -> Path:
    load_broker_profile(profile, profile_dir=profile_dir)
    return (profile_dir or PROFILE_DIR) / f"{profile}.yaml"


def _capability(entry: object) -> Capability:
    """把登记项翻译成能力：只有明确的已核验值才带证据成立，其余都是"未知"."""
    if not isinstance(entry, Mapping):
        return Capability(None, False)
    value = entry.get("value")
    status = str(entry.get("verification_status", ""))
    level = str(entry.get("evidence_level") or "")
    evidence = entry.get("evidence")
    verified = (
        value is not None and status in VERIFIED_STATUSES and level not in UNVERIFIED_EVIDENCE_LEVELS and bool(evidence)
    )
    if not verified:
        return Capability(None, False)
    normalized: bool | int | str
    if isinstance(value, bool):
        normalized = value
    elif isinstance(value, int):
        normalized = value
    else:
        normalized = str(value)
    return Capability(normalized, True, str(entry.get("test_id") or evidence))


def capability_profile(profile: Mapping[str, Any]) -> CapabilityProfile:
    """把登记的各项能力翻译成 ``CapabilityProfile``；未登记项视为未知，不给默认值."""
    capabilities = profile.get("capabilities") or {}
    if not isinstance(capabilities, Mapping):
        raise BrokerProfileError("broker profile capabilities must be a mapping")
    values: dict[str, Capability] = {}
    order_types = capabilities.get("order_types") or {}
    if isinstance(order_types, Mapping):
        for name in ("market_order", "limit_order"):
            values[f"order_types.{name}"] = _capability(order_types.get(name))
    for name in ("query_rate_limit", "cancel_permission", "realtime_report"):
        values[f"counter.{name}"] = _capability(capabilities.get(name))
    return CapabilityProfile(
        profile_id=str(profile.get("profile_name")),
        ctp_version=None if profile.get("ctp_version") is None else str(profile["ctp_version"]),
        values=values,
    )


def offset_mappings(profile: Mapping[str, Any]) -> tuple[CtpOffsetMapping, ...]:
    """读取逐交易所的开平标志登记；未核验的登记保留为禁用状态 (S5-01 发送前检查)."""
    capabilities = profile.get("capabilities") or {}
    registered = capabilities.get("offset_mapping") if isinstance(capabilities, Mapping) else None
    if not isinstance(registered, Mapping):
        return ()
    mappings: list[CtpOffsetMapping] = []
    for exchange_name, entry in registered.items():
        try:
            exchange = Exchange(str(exchange_name))
        except ValueError:
            continue  # 登记里的交易所不在本项目启用的枚举内
        if not isinstance(entry, Mapping):
            continue
        raw = entry.get("value")
        flags: dict[Offset, str] = {}
        if isinstance(raw, Mapping):
            for offset_name, flag in raw.items():
                try:
                    flags[Offset(str(offset_name))] = str(flag)
                except ValueError as exc:
                    raise BrokerProfileError(f"unknown offset {offset_name!r} in the {exchange_name} mapping") from exc
        status = str(entry.get("verification_status", ""))
        verified = status in VERIFIED_STATUSES and bool(entry.get("evidence")) and bool(flags)
        evidence = entry.get("test_id") or entry.get("evidence")
        mappings.append(
            CtpOffsetMapping(
                exchange=exchange,
                flags=flags,
                verified=verified,
                evidence_ref=None if not verified else str(evidence),
            )
        )
    return tuple(mappings)


def front_addresses(profile: Mapping[str, Any]) -> Mapping[str, str]:
    """前置候选地址；登记状态为待核验时调用方须自行记录证据 (R4)."""
    fronts = profile.get("fronts") or {}
    if not isinstance(fronts, Mapping):
        raise BrokerProfileError("broker profile fronts must be a mapping")
    trade = fronts.get("trade")
    if not isinstance(trade, str) or not trade:
        raise BrokerProfileError("broker profile has no trade front candidate; register one before connecting")
    return {"trade": trade, "market": str(fronts.get("market") or "")}


def secrets_from_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if environment is None else environment
    secrets = {}
    password = env.get(PASSWORD_ENV)
    if not password:
        raise BrokerProfileError(
            f"{PASSWORD_ENV} is not set; the trading password must come from the local "
            "environment, never the repository"
        )
    secrets["password"] = password
    auth_code = env.get(AUTH_CODE_ENV)
    if auth_code:
        secrets["auth_code"] = auth_code
    return secrets


def ctp_settings(
    profile: Mapping[str, Any],
    *,
    user_id: str | None = None,
    investor_id: str | None = None,
    front: str | None = None,
    flow_dir: str | Path = "runs/live/ctp_flow",
    environment: Mapping[str, str] | None = None,
    connect_timeout_s: float = 20.0,
    login_timeout_s: float = 20.0,
    query_timeout_s: float = 15.0,
    query_interval_ms: int | None = None,
) -> CtpSettings:
    """登记 + 环境秘密 → ``CtpSettings``；缺项即失败，不用占位值连接柜台."""
    fronts = front_addresses(profile)
    broker_id = profile.get("broker_id")
    if not broker_id:
        raise BrokerProfileError("broker profile has no broker_id")
    account = profile.get("account") or {}
    if not isinstance(account, Mapping):
        raise BrokerProfileError("broker profile account section must be a mapping")
    resolved_user = user_id or account.get("user_id")
    resolved_investor = investor_id or account.get("investor_id") or resolved_user
    if not resolved_user:
        raise BrokerProfileError("a login user id is required (broker profile account.user_id or --user)")
    secrets = secrets_from_environment(environment)
    app_id = account.get("app_id")
    auth_code = secrets.get("auth_code") or account.get("auth_code")
    if auth_code and not app_id:
        raise BrokerProfileError("an AuthCode is only usable together with the AppID registered for it")
    return CtpSettings(
        front_trade=front or fronts["trade"],
        broker_id=str(broker_id),
        investor_id=str(resolved_investor),
        user_id=str(resolved_user),
        password=secrets["password"],
        app_id=None if not app_id else str(app_id),
        auth_code=None if not auth_code else str(auth_code),
        product_info=str(account.get("product_info", "qh_trader")),
        flow_dir=str(flow_dir),
        ctp_version=None if profile.get("ctp_version") is None else str(profile["ctp_version"]),
        connect_timeout_s=connect_timeout_s,
        login_timeout_s=login_timeout_s,
        query_timeout_s=query_timeout_s,
    )


def profile_summary(profile: Mapping[str, Any]) -> Mapping[str, object]:
    """脱敏摘要：写运行清单与证据，不含任何凭证."""
    fronts = profile.get("fronts") or {}
    account = profile.get("account") or {}
    return {
        "profile_name": profile.get("profile_name"),
        "broker_id": profile.get("broker_id"),
        "ctp_version": profile.get("ctp_version"),
        "registered_at": profile.get("registered_at"),
        "fronts": dict(fronts) if isinstance(fronts, Mapping) else {},
        "terminal_authentication": bool(account.get("app_id")) if isinstance(account, Mapping) else False,
        "capability_states": capability_states(profile),
    }


def capability_states(profile: Mapping[str, Any]) -> Mapping[str, str]:
    """逐项登记状态，供清单与证据引用；不改写登记本身."""
    capabilities = profile.get("capabilities") or {}
    states: dict[str, str] = {}
    if not isinstance(capabilities, Mapping):
        return states
    for name, entry in capabilities.items():
        if isinstance(entry, Mapping) and "verification_status" in entry:
            states[str(name)] = str(entry["verification_status"])
        elif isinstance(entry, Mapping):
            for sub_name, sub_entry in entry.items():
                if isinstance(sub_entry, Mapping) and "verification_status" in sub_entry:
                    states[f"{name}.{sub_name}"] = str(sub_entry["verification_status"])
                else:
                    states[f"{name}.{sub_name}"] = "未登记"
    return states


def register_front_candidates(profile: Mapping[str, Any], *, now: datetime | None = None) -> Sequence[str]:
    """把前置候选的登记时间与来源拼成审计行（不改登记文件）."""
    fronts = profile.get("fronts") or {}
    if not isinstance(fronts, Mapping):
        return ()
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    return tuple(
        f"{name}={value} registered_at={stamp} source={fronts.get('source') or 'unregistered'}"
        for name, value in sorted(fronts.items())
        if name not in {"source", "registered_at"} and isinstance(value, str) and value
    )
