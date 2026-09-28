"""[脚本工具] 柜台登记与 CTP 接入参数的装配输入 (S0-02, S5-01, FR-LIVE-01/04).

只做"把登记文件翻译成端口要求的对象"：登记里没有的、或核验状态未成立的项一律翻译成
"未知能力"，绝不填默认值（GAP-S0-05：未核验能力禁用而非猜测）。

秘密只从环境变量读取（``QH_CTP_PASSWORD``、``QH_CTP_AUTH_CODE``），不写入配置文件、日志或证据文件。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from qh_trader.core.constants import Exchange, Offset
from qh_trader.core.objects import Capability, CapabilityProfile
from qh_trader.gateway.ctp_gateway import CtpOffsetMapping, CtpSettings
from qh_trader.gateway.ctp_native_libs import NativeLibSpec

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


#: 登记里允许的原生库平台条目；扩展名由 gateway.ctp_native_libs 按平台决定。
NATIVE_LIB_PLATFORMS: tuple[str, ...] = ("win64", "lin64")


def native_lib_platform(platform_name: str | None = None) -> str:
    """把运行平台翻译成登记里的条目名；未登记的平台明确失败，不猜 (GAP-S0-01)."""
    key = platform_name or sys.platform
    if key in NATIVE_LIB_PLATFORMS:
        return key  # 已是登记里的条目名（脚本 --platform 直接给出）
    if key == "win32":
        return "win64"
    if key.startswith("linux"):
        return "lin64"
    raise BrokerProfileError(f"no registered CTP native library for platform {key!r}")


def native_libs_spec(
    profile: Mapping[str, Any],
    *,
    platform_name: str | None = None,
    root: Path | None = None,
) -> NativeLibSpec | None:
    """柜台登记 → 原生库选择；未登记返回 ``None``（用绑定自带库）.

    登记了就必须逐文件登记摘要：未登记摘要的库不得装载（GAP-S0-01）。
    """
    block = profile.get("native_libs")
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise BrokerProfileError("broker profile native_libs must be a mapping")
    platform_key = native_lib_platform(platform_name)
    platforms = block.get("platforms") or {}
    if not isinstance(platforms, Mapping):
        raise BrokerProfileError("native_libs.platforms must be a mapping")
    entry = platforms.get(platform_key)
    if not isinstance(entry, Mapping):
        raise BrokerProfileError(f"native_libs has no entry for {platform_key}; register source_dir and digests")
    source_dir = entry.get("source_dir")
    if not isinstance(source_dir, str) or not source_dir:
        raise BrokerProfileError(f"native_libs.platforms.{platform_key}.source_dir is required")
    digests = entry.get("digests")
    if not isinstance(digests, Mapping) or not digests:
        raise BrokerProfileError(f"native_libs.platforms.{platform_key}.digests must list every file's sha256")
    archive = block.get("archive") or {}
    if not isinstance(archive, Mapping):
        raise BrokerProfileError("native_libs.archive must be a mapping")
    base = Path(root) if root is not None else ROOT
    return NativeLibSpec(
        flavor=str(block.get("flavor") or ""),
        api_marker=str(block.get("api_marker") or ""),
        source_dir=str((base / source_dir).resolve()),
        staging_dir=str((base / str(block.get("staging_dir") or "")).resolve()),
        digests=tuple(sorted((str(name), str(value)) for name, value in digests.items())),
        archive_url=None if not archive.get("url") else str(archive["url"]),
        archive_sha256=None if not archive.get("sha256") else str(archive["sha256"]),
    )


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
    terminal_mode: str | None = None,
    collector_lib_path: str | Path | None = None,
    terminal_public_ip: str | None = None,
    terminal_ip_port: int | None = None,
    native_libs: NativeLibSpec | None = None,
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
    term_info = profile.get("terminal_info") or {}
    resolved_terminal_mode = (
        terminal_mode or (term_info.get("mode") if isinstance(term_info, Mapping) else None) or "none"
    )
    resolved_lib_path = (
        str(collector_lib_path)
        if collector_lib_path
        else (term_info.get("collector_lib") if isinstance(term_info, Mapping) else None)
    )
    resolved_native_libs = native_libs if native_libs is not None else native_libs_spec(profile)
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
        terminal_mode=str(resolved_terminal_mode),
        collector_lib_path=None if not resolved_lib_path else str(resolved_lib_path),
        terminal_public_ip=terminal_public_ip
        or (str(term_info.get("public_ip")) if isinstance(term_info, Mapping) and term_info.get("public_ip") else None),
        terminal_ip_port=terminal_ip_port
        or (int(term_info.get("ip_port")) if isinstance(term_info, Mapping) and term_info.get("ip_port") else None),
        native_libs=resolved_native_libs,
    )


def profile_summary(profile: Mapping[str, Any]) -> Mapping[str, object]:
    """脱敏摘要：写运行清单与证据，不含任何凭证."""
    fronts = profile.get("fronts") or {}
    account = profile.get("account") or {}
    term_info = profile.get("terminal_info") or {}
    mode = term_info.get("mode", "none") if isinstance(term_info, Mapping) else "none"
    native_libs = profile.get("native_libs") or {}
    return {
        "profile_name": profile.get("profile_name"),
        "broker_id": profile.get("broker_id"),
        "ctp_version": profile.get("ctp_version"),
        "registered_at": profile.get("registered_at"),
        "fronts": dict(fronts) if isinstance(fronts, Mapping) else {},
        "terminal_authentication": bool(account.get("app_id")) if isinstance(account, Mapping) else False,
        "terminal_info_mode": str(mode),
        "native_libs_flavor": native_libs.get("flavor") if isinstance(native_libs, Mapping) else None,
        "native_libs_api_marker": native_libs.get("api_marker") if isinstance(native_libs, Mapping) else None,
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
        if isinstance(value, str) and value.startswith("tcp://")
    )


def product_specs() -> Mapping[str, object]:
    """本地登记的品种口径（研究假设，来源字段逐项标注）."""
    from qh_trader.data.product_registry import registered_products

    return {spec.product.upper(): spec for spec in registered_products()}


def _counter_number(record: Mapping[str, object], name: str) -> Decimal | None:
    """读柜台给出的数值字段；缺失、空串或非数值都按"柜台未给出"处理，不读成 0."""
    value = record.get(name)
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _counter_value_state(counter: Decimal | None, registered: Decimal) -> tuple[bool, str]:
    """柜台口径 vs 本地登记：``(是否一致, 状态)``；柜台未给出（缺失或 0）不算"不一致"."""
    if counter is None:
        return False, "柜台未给出该口径"
    if counter == 0:
        # 实测：openctp TTS 的 ReqQryProduct 把乘数与最小变动恒返 0（品种级口径不可用），
        # SimNow 休市日也会返回空值；把 0 当成"不一致"会把柜台的空口径误报成登记错误。
        return False, "柜台未给出该口径"
    return counter == registered, "一致" if counter == registered else "不一致"


def compare_products(counter_products: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    """柜台品种清单 vs 本地品种登记：只比对柜台直接给出的乘数与最小变动 (FR-RULE-05).

    三种状态必须分开："一致""柜台未给出该口径"（缺失或 0，例如 openctp TTS 的品种级返回）与
    "不一致"（柜台给出了与登记不同的非零值）。只有第三种算差异，其余两种单独登记；手续费与
    保证金比例由 :func:`compare_rates` 用柜台费率 / 保证金查询比对。
    """
    registry = product_specs()
    by_id = {}
    for record in counter_products:
        product_id = record.get("ProductID")
        exchange = record.get("ExchangeID")
        if isinstance(product_id, str) and isinstance(exchange, str):
            by_id[(product_id.upper(), exchange)] = record
    compared: list[Mapping[str, object]] = []
    missing: list[str] = []
    for name, spec in sorted(registry.items()):
        record = by_id.get((name, spec.exchange.value))
        if record is None:
            missing.append(f"{spec.exchange.value}.{name}")
            continue
        counter_multiplier = _counter_number(record, "VolumeMultiple")
        counter_tick = _counter_number(record, "PriceTick")
        multiplier_matches, multiplier_state = _counter_value_state(counter_multiplier, spec.multiplier)
        tick_matches, tick_state = _counter_value_state(counter_tick, spec.price_tick)
        states = {multiplier_state, tick_state}
        value_state = "一致" if states == {"一致"} else ("不一致" if "不一致" in states else "柜台未给出该口径")
        compared.append(
            {
                "product": f"{spec.exchange.value}.{name}",
                "registry_multiplier": str(spec.multiplier),
                "counter_multiplier": None if counter_multiplier is None else str(counter_multiplier),
                "registry_price_tick": str(spec.price_tick),
                "counter_price_tick": None if counter_tick is None else str(counter_tick),
                "counter_name": record.get("ProductName"),
                "value_state": value_state,
                "matches": bool(multiplier_matches and tick_matches),
                "registry_verification_status": spec.verification_status,
                "registry_source": spec.source,
            }
        )
    mismatches = [item for item in compared if item["value_state"] == "不一致"]
    counter_absent = [item for item in compared if item["value_state"] == "柜台未给出该口径"]
    return {
        "counter_products": len(counter_products),
        "registered_products": len(registry),
        "compared": compared,
        "missing_at_counter": missing,
        "mismatches": mismatches,
        "counter_absent": counter_absent,
        "commission_and_margin": (
            "手续费与保证金比例由 compare_contract_rates 用柜台费率 / 保证金查询比对"
            "（品种级查询不给这两个口径）；脚本入口是 scripts/ctp_probe.py --verify-rates"
        ),
        "result": (
            "柜台品种级口径与本地登记不一致"
            if mismatches
            else (
                "柜台品种级查询未给出乘数 / 最小变动：改用合约级 ReqQryInstrument 核验（见 instrument_comparison）"
                if counter_absent
                else "柜台品种级口径与本地登记一致"
            )
        ),
    }


def compare_contract_rates(observations: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    """柜台费率 / 保证金率 vs 本地品种登记的研究假设 (FR-RULE-05).

    每条 observation 形如 ``{"instrument": "SHFE.rb2610", "commission": {...}|None, "margin": {...}|None}``。
    判定口径（柜台未给出与不一致严格分开）：

    - 手续费：柜台 ``OpenRatioByVolume``（元/手）与登记的每手费用比对；柜台只给按金额
      （``OpenRatioByMoney``）时标注"柜台按金额收费"，不当成不一致也不算核验通过；
    - 保证金：投机仓 ``LongMarginRatioByMoney`` / ``ShortMarginRatioByMoney`` 与登记比例比对，
      两者都给出且不同（同一账户多空不一致）时单独记 ``margin_direction_conflict``；
    - 缺失或 0 一律记"柜台未给出该口径"。
    """
    registry = product_specs()
    compared: list[Mapping[str, object]] = []
    for observation in observations:
        instrument = str(observation.get("instrument") or "")
        product = str(observation.get("product") or "").upper()
        spec = registry.get(product)
        commission = observation.get("commission")
        margin = observation.get("margin")
        commission = commission if isinstance(commission, Mapping) else None
        margin = margin if isinstance(margin, Mapping) else None
        per_lot = None if commission is None else _counter_number(commission, "OpenRatioByVolume")
        by_money = None if commission is None else _counter_number(commission, "OpenRatioByMoney")
        long_ratio = None if margin is None else _counter_number(margin, "LongMarginRatioByMoney")
        short_ratio = None if margin is None else _counter_number(margin, "ShortMarginRatioByMoney")
        if spec is None:
            compared.append({"instrument": instrument, "product": product, "value_state": "无本地登记"})
            continue
        commission_matches, commission_state = (
            _counter_value_state(per_lot, spec.commission_per_lot)
            if per_lot is not None and per_lot != 0
            else (False, "按金额收费（与每手登记不可比）" if by_money else "柜台未给出该口径")
        )
        margin_states = {
            _counter_value_state(long_ratio, spec.margin_ratio)[1],
            _counter_value_state(short_ratio, spec.margin_ratio)[1],
        }
        margin_state = (
            "一致" if margin_states == {"一致"} else ("不一致" if "不一致" in margin_states else "柜台未给出该口径")
        )
        value_state = _aggregate_states(commission_state, margin_state)
        compared.append(
            {
                "instrument": instrument,
                "product": f"{spec.exchange.value}.{spec.product}",
                "registry_commission_per_lot": str(spec.commission_per_lot),
                "counter_open_ratio_by_volume": None if per_lot is None else str(per_lot),
                "counter_open_ratio_by_money": None if by_money is None else str(by_money),
                "counter_close_today_ratio_by_volume": (
                    None if commission is None else _string_or_none(commission.get("CloseTodayRatioByVolume"))
                ),
                "commission_state": commission_state,
                "commission_matches": bool(commission_matches),
                "registry_margin_ratio": str(spec.margin_ratio),
                "counter_long_margin_ratio": None if long_ratio is None else str(long_ratio),
                "counter_short_margin_ratio": None if short_ratio is None else str(short_ratio),
                "margin_state": margin_state,
                "margin_direction_conflict": bool(
                    long_ratio is not None
                    and short_ratio is not None
                    and long_ratio != 0
                    and short_ratio != 0
                    and long_ratio != short_ratio
                ),
                "value_state": value_state,
                "matches": value_state == "一致",
                "registry_verification_status": spec.verification_status,
                "registry_source": spec.source,
            }
        )
    mismatches = [item for item in compared if item.get("value_state") == "不一致"]
    counter_absent = [item for item in compared if item.get("value_state") == "柜台未给出该口径"]
    state_counts: dict[str, int] = {}
    for item in compared:
        key = str(item.get("value_state"))
        state_counts[key] = state_counts.get(key, 0) + 1
    return {
        "observed_contracts": len(compared),
        "compared": compared,
        "mismatches": mismatches,
        "counter_absent": counter_absent,
        "state_counts": state_counts,
        "result": (
            f"柜台费率 / 保证金率与本地登记不一致：{len(mismatches)} 个合约（其余见 state_counts）"
            if mismatches
            else (
                "柜台未给出费率 / 保证金率口径（休市日或该柜台对重放合约无费率配置）"
                if counter_absent
                else "柜台费率 / 保证金率与本地登记一致"
            )
        ),
    }


def _aggregate_states(commission_state: str, margin_state: str) -> str:
    """把手续费与保证金两项状态汇总成条目状态；"口径不同"不得降级成"未给出"."""
    states = {commission_state, margin_state}
    if "不一致" in states:
        return "不一致"
    if "按金额收费（与每手登记不可比）" in states:
        return "口径不同"
    if states == {"一致"}:
        return "一致"
    return "柜台未给出该口径"


def _string_or_none(value: object) -> str | None:
    return None if value is None or value == "" else str(value)


def compare_instruments(
    counter_instruments: Sequence[Mapping[str, object]],
    *,
    sample_limit: int = 3,
) -> Mapping[str, object]:
    """合约级口径核验：用 ``ReqQryInstrument`` 的合约清单按品种核对乘数与最小变动 (FR-RULE-05).

    品种级查询在部分柜台（实测 openctp TTS）恒返回 0，因此真正的核验落在合约级：逐个可交易合约比对
    ``VolumeMultiple`` / ``PriceTick``。柜台没有 ``IsTrading='1'`` 的合约时退回该品种的全部合约并标注，
    不把空清单当成一致，也不把 0 当成"不一致"。
    """
    registry = product_specs()
    by_product: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for record in counter_instruments:
        product_id = record.get("ProductID")
        exchange = record.get("ExchangeID")
        if isinstance(product_id, str) and isinstance(exchange, str):
            by_product.setdefault((product_id.upper(), exchange), []).append(record)
    compared: list[Mapping[str, object]] = []
    missing: list[str] = []
    for name, spec in sorted(registry.items()):
        records = by_product.get((name, spec.exchange.value), [])
        trading = [item for item in records if str(item.get("IsTrading", "")) == "1"]
        filtered = bool(trading)
        selected = trading or records
        if not selected:
            missing.append(f"{spec.exchange.value}.{name}")
            continue
        states = {
            _counter_value_state(_counter_number(item, "VolumeMultiple"), spec.multiplier)[1] for item in selected
        }
        states |= {_counter_value_state(_counter_number(item, "PriceTick"), spec.price_tick)[1] for item in selected}
        value_state = "一致" if states == {"一致"} else ("不一致" if "不一致" in states else "柜台未给出该口径")
        disagreeing = [
            str(item.get("InstrumentID"))
            for item in selected
            if not (
                _counter_number(item, "VolumeMultiple") == spec.multiplier
                and _counter_number(item, "PriceTick") == spec.price_tick
            )
        ]
        compared.append(
            {
                "product": f"{spec.exchange.value}.{name}",
                "registry_multiplier": str(spec.multiplier),
                "registry_price_tick": str(spec.price_tick),
                "counter_contracts": len(selected),
                "is_trading_filter_applied": filtered,
                "counter_contracts_disagreeing": disagreeing[:10],
                "samples": sorted(str(item.get("InstrumentID")) for item in selected)[:sample_limit],
                "value_state": value_state,
                "matches": value_state == "一致",
                "registry_verification_status": spec.verification_status,
                "registry_source": spec.source,
            }
        )
    mismatches = [item for item in compared if item["value_state"] == "不一致"]
    counter_absent = [item for item in compared if item["value_state"] == "柜台未给出该口径"]
    return {
        "counter_instruments": len(counter_instruments),
        "registered_products": len(registry),
        "compared": compared,
        "missing_at_counter": missing,
        "mismatches": mismatches,
        "counter_absent": counter_absent,
        "result": (
            "柜台合约级口径与本地登记不一致"
            if mismatches
            else ("柜台合约级查询未给出乘数 / 最小变动" if counter_absent else "柜台合约级口径与本地登记一致")
        ),
    }
