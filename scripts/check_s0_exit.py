"""Validate S0 exit evidence without connecting to a broker or placing orders."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NamedTuple

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CAPABILITIES = (
    "offset_mapping",
    "order_types",
    "time_in_force",
    "stage_permissions",
    "parking_orders",
    "query_limits",
    "cancel_counting",
    "commission_rates",
    "margin_rates",
    "delivery_limits",
    "cancel_identifiers",
    "position_query",
    "front_maintenance",
    "floating_profit_usage",
    "conditional_orders",
    "settlement_format",
    "overnight_orders",
    "allow_lock",
)
REQUIRED_RULE_TYPES = {"commission", "margin", "trading_hours"}
REQUIRED_SAMPLE_ROLES = {"bars", "settlement", "limits", "contract_info", "sessions", "rules"}
PENDING_STATUSES = {"未开始", "未核验", "待核验", "进行中"}
CHECKS = (
    ("9.1", "策略类别及执行策略声明", "strategy"),
    ("9.2", "环境与接入模式清单", "environment"),
    ("9.3", "数据覆盖与执行价格", "data_coverage"),
    ("9.4", "柜台能力登记", "broker_capabilities"),
    ("9.5", "规则来源登记", "rules"),
    ("9.6", "A25 适用清单", "a25"),
    ("9.7", "跨日盈亏手工账", "ledger"),
    ("9.8", "完整工程样本", "engineering_sample"),
    ("9.9", "研究数据采购启动", "research"),
    ("9.10", "缺口登记及关联", "gap_registration"),
    ("9.11", "首个合约账户能力登记", "account"),
)


class NotReadyError(ValueError):
    """A required deliverable or external verification is still pending."""


class InvalidEvidenceError(ValueError):
    """Evidence is malformed, inconsistent, or falsely marked verified."""


class Result(NamedTuple):
    check_id: str
    name: str
    status: str
    detail: str


def require(condition, message):
    if not condition:
        raise InvalidEvidenceError(message)


def text(value):
    return isinstance(value, str) and value.strip() not in {
        "",
        "null",
        "None",
        "TODO",
        "TBD",
        "未核验",
        "未开始",
        "待填写",
        "待确定",
        "供应商名称或接口",
    }


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    require(isinstance(value, str), "日期必须使用 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidEvidenceError("日期格式不正确") from exc


def as_time(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidEvidenceError("时间必须为带时区的 ISO 8601 时刻") from exc
    require(result.tzinfo is not None, "时刻必须明确时区")
    return result


def time_range(record):
    require(isinstance(record, dict), "缺少时间范围")
    if record.get("start_date") is None or record.get("end_date") is None:
        raise NotReadyError("样本或数据的起止日期尚未确定")
    start, end = as_date(record["start_date"]), as_date(record["end_date"])
    require(start <= end, "时间范围起止顺序错误")
    return start, end


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class S0Checker:
    def __init__(self, root=ROOT, config="config/settings.yaml", environment="runs/s0/environment.json"):
        self.root = Path(root).resolve()
        self.config_path = config
        self.environment_path = environment
        self._gap_cache = None

    def path(self, relative):
        require(isinstance(relative, (str, Path)), "文件引用必须包含路径")
        path = (self.root / relative).resolve()
        require(path.is_relative_to(self.root), "证据路径必须位于项目目录内")
        return path

    def load(self, relative):
        path = self.path(relative)
        if not path.is_file():
            raise NotReadyError(f"缺少文件：{path.relative_to(self.root).as_posix()}")
        try:
            content = path.read_text(encoding="utf-8-sig")
            data = json.loads(content) if path.suffix == ".json" else yaml.safe_load(content)
        except (yaml.YAMLError, json.JSONDecodeError, UnicodeError) as exc:
            # 配置可能含本地凭证，不回显解析异常中的原文。
            raise InvalidEvidenceError(f"文件格式无效：{path.name}") from exc
        require(isinstance(data, dict), f"{path.name} 必须是映射对象")
        return data

    def file_ref(self, reference, label):
        require(isinstance(reference, dict), f"{label} 缺少 path/sha256 引用")
        require(text(reference.get("path")), f"{label} 路径未填写")
        expected = reference.get("sha256")
        require(isinstance(expected, str) and re.fullmatch(r"[a-fA-F0-9]{64}", expected), f"{label} 哈希无效")
        path = self.path(reference["path"])
        require(path.is_file() and path.stat().st_size > 0, f"{label} 文件不存在或为空")
        require(sha256(path).lower() == expected.lower(), f"{label} 哈希不符")
        return path

    def settings(self):
        return self.load(self.config_path)

    def coverage(self):
        data = self.load("config/data_coverage.yaml")
        require(data.get("schema_version") == 1, "数据覆盖清单 schema_version 必须为 1")
        return data

    def known_requirements(self):
        return {item["id"] for item in self.load("docs/requirements.json")["requirements"]}

    def gaps(self):
        if self._gap_cache is not None:
            return self._gap_cache
        data = self.load("config/gaps.yaml")
        require(data.get("schema_version") == 1 and isinstance(data.get("gaps"), list), "缺口登记结构无效")
        known = self.known_requirements()
        result = {}
        for gap in data["gaps"]:
            require(isinstance(gap, dict) and text(gap.get("gap_id")), "缺口必须有稳定编号")
            ident = gap["gap_id"]
            require(ident not in result, f"重复缺口编号：{ident}")
            for field in ("item", "gap_type", "current_handling", "registered_by", "close_condition"):
                require(text(gap.get(field)), f"{ident} 缺少 {field}")
            require(
                gap["gap_type"] in {"无原文", "版本不唯一", "柜台未答复", "数据不可得", "待核验"},
                f"{ident} 缺口类型无效",
            )
            require(isinstance(gap.get("affected_scope"), dict) and gap["affected_scope"], f"{ident} 未说明影响范围")
            refs = gap.get("related_requirements")
            require(isinstance(refs, list) and refs and set(refs) <= known, f"{ident} 需求关联无效")
            as_date(gap.get("registered_at"))
            require(gap.get("status") in {"未关闭", "已关闭"}, f"{ident} 状态无效")
            if gap["status"] == "已关闭":
                as_date(gap.get("closed_at"))
                self.file_ref(gap.get("close_evidence"), f"{ident} 关闭证据")
            result[ident] = gap
        self._gap_cache = result
        return result

    def gap_refs(self, identifiers, label):
        require(isinstance(identifiers, list) and identifiers, f"{label} 未关联缺口")
        gaps = self.gaps()
        for ident in identifiers:
            require(ident in gaps and gaps[ident]["status"] == "未关闭", f"{label} 关联了无效或已关闭缺口：{ident}")

    def verification(self, record, label, allow_gap=False):
        require(isinstance(record, dict), f"{label} 缺少核验信息")
        status = record.get("verification_status")
        if status == "已核验":
            require(text(record.get("verified_by")), f"{label} 未记录核验人")
            as_date(record.get("verified_at"))
            self.file_ref(record.get("evidence"), f"{label} 核验证据")
            return "verified"
        if status == "登记缺口" and allow_gap:
            self.gap_refs(record.get("gap_ids"), label)
            return "gap"
        if status in PENDING_STATUSES or status == "登记缺口":
            raise NotReadyError(f"{label} 尚未完成核验")
        raise InvalidEvidenceError(f"{label} 核验状态无效")

    def strategy(self):
        from scripts.smoke import validate_config_template

        require(not str(self.config_path).endswith(".example"), "配置模板不能代替实际运行声明")
        settings = self.settings()
        validate_config_template(settings, root=self.root)
        strategy = settings["strategy"]
        sample = settings.get("data", {}).get("engineering_sample", {})
        require(text(sample.get("contract")), "未声明首个实际合约")
        return "verified", f"{strategy['category']} / {strategy['signal_frequency']} / {strategy['execution_strategy']}"

    def environment(self):
        from scripts.init_env import DEPENDENCIES

        report = self.load(self.environment_path)
        require(report.get("schema_version") == 1 and report.get("kind") == "s0_environment", "环境报告格式无效")
        as_time(report.get("generated_at"))
        for name in ("pyproject.toml", "uv.lock", ".python-version"):
            ref = report.get("inputs", {}).get(name)
            require(isinstance(ref, dict) and ref.get("path") == name, f"环境报告缺少 {name} 版本绑定")
            self.file_ref(ref, f"环境输入 {name}")
        require(report.get("python", {}).get("supported") is True, "Python 版本不符合项目声明")
        pin = self.path(".python-version").read_text(encoding="utf-8").strip()
        version = report["python"].get("version", "")
        require(version == pin or version.startswith(pin + "."), "环境报告的 Python 版本与声明不一致")
        dependencies = report.get("dependencies", {})
        require(set(dependencies) == set(DEPENDENCIES), "环境报告未覆盖全部核心依赖")
        require(
            all(item.get("status") == "OK" and text(item.get("version")) for item in dependencies.values()),
            "核心依赖记录不完整",
        )
        lock = tomllib.loads(self.path("uv.lock").read_text(encoding="utf-8"))
        locked_versions = {}
        for package in lock["package"]:
            locked_versions.setdefault(package["name"], set()).add(package.get("version"))
        require(
            all(item["version"] in locked_versions.get(name, set()) for name, item in dependencies.items()),
            "环境报告的依赖版本与锁文件不一致",
        )
        require(report.get("required_dependencies_ok") is True, "核心依赖检查未通过")
        sqlite = report.get("sqlite", {})
        require(sqlite.get("passed") is True and sqlite.get("journal_mode") == "wal", "文件级 SQLite 配置未通过")
        require(
            sqlite.get("synchronous") == 2
            and sqlite.get("foreign_keys") == 1
            and sqlite.get("busy_timeout", 0) > 0
            and sqlite.get("foreign_key_enforced") is True,
            "SQLite 连接参数或约束检查不完整",
        )
        rows = {}
        document = self.path("docs/09_前期准备与规则核验清单.md").read_text(encoding="utf-8-sig")
        for line in document.splitlines():
            cells = [cell.strip() for cell in line.split("|")[1:-1]]
            if cells and re.fullmatch(r"[23]\.\d+", cells[0]):
                rows[cells[0]] = cells
        expected = {f"{section}.{n}" for section in (2, 3) for n in range(1, 13)}
        require(set(rows) == expected, "09 的环境与接入清单条目缺失或编号发生变化")
        pending = []
        has_gap = False
        for ident, cells in rows.items():
            require(len(cells) >= 7, f"09 的 {ident} 行结构无效")
            if cells[3] == "登记缺口":
                self.gap_refs(re.findall(r"GAP-[A-Za-z0-9-]+", cells[4]), f"09 的 {ident}")
                has_gap = True
            elif cells[3] == "已核验":
                require(text(cells[4]), f"09 的 {ident} 缺少证据位置")
            else:
                pending.append(ident)
        if pending:
            raise NotReadyError("09 环境/接入清单未闭合：" + "、".join(sorted(pending)))
        artifacts = report.get("sdk_archives", [])
        candidates = report.get("ctp_candidates", {})
        if rows["2.1"][3] == "已核验" or rows["3.6"][3] == "已核验":
            proof_ref = report.get("ctp_runtime_evidence")
            if proof_ref is None:
                raise NotReadyError("离线导入检查不能证明 CTP 联调；缺少脱敏运行验证记录")
            proof = self.load(self.file_ref(proof_ref, "CTP 联调证据"))
            candidate = candidates.get(proof.get("package"), {})
            require(candidate.get("status") == "IMPORTABLE", "联调记录的封装在当前环境不可导入")
            require(
                candidate.get("module_sha256") and proof.get("module_sha256") == candidate["module_sha256"],
                "联调记录与当前封装版本不一致",
            )
            require(proof.get("schema_version") == 1 and proof.get("status") == "passed", "CTP 联调记录未通过")
            require(text(proof.get("verified_by")), "CTP 联调记录未注明核验人")
            as_date(proof.get("verified_at"))
            required = {"login", "query", "callbacks"}
            if rows["3.6"][3] == "已核验":
                required.add("terminal_auth_order")
            require(all(proof.get("checks", {}).get(key) is True for key in required), "CTP 联调检查不完整")
        if not artifacts and not any(item.get("status") == "IMPORTABLE" for item in candidates.values()):
            raise NotReadyError("CTP 候选库或 SDK 版本/哈希尚未记录")
        for artifact in artifacts:
            require(artifact.get("status") == "INVENTORIED", "SDK 归档检查未完成")
            self.file_ref(artifact, "SDK 归档")
        if not any(item.get("status") == "IMPORTABLE" for item in candidates.values()):
            self.gap_refs(["GAP-S0-01"], "CTP 选型与联调")
            require(rows["2.1"][3] == "登记缺口", "未安装的 CTP 候选不能标记兼容性已核验")
            has_gap = True
        return ("gap" if has_gap else "verified"), "环境报告与清单一致；缺口状态不代表 CTP 联调通过"

    def data_coverage(self):
        coverage = self.coverage()
        sources = coverage.get("data_sources")
        require(isinstance(sources, list), "data_sources 必须为列表")
        if not sources:
            raise NotReadyError("尚未登记实际数据供应商及数据集")
        datasets = {}
        for source in sources:
            require(text(source.get("source_id")) and text(source.get("source_name")), "数据源仍是占位信息")
            require(isinstance(source.get("datasets"), list) and source["datasets"], "数据源没有数据集")
            for dataset in source["datasets"]:
                ident = (source["source_id"], dataset.get("dataset_id"))
                require(text(ident[1]) and ident not in datasets, "数据集编号缺失或重复")
                self.verification(dataset, "数据集 " + ident[1])
                time_range(dataset.get("time_range"))
                require(
                    isinstance(dataset.get("available_fields"), list) and dataset["available_fields"], "字段清单为空"
                )
                for field in ("update_delay", "historical_revision"):
                    require(dataset.get(field) is not None, f"数据集缺少 {field}")
                license_info = dataset.get("license", {})
                require(text(license_info.get("scope")) and license_info.get("local_storage") is True, "数据许可未确认")
                datasets[ident] = dataset
        strategy = self.settings()["strategy"]
        selected = [
            item
            for item in coverage.get("execution_price_coverage", [])
            if item.get("strategy") == strategy["execution_strategy"]
        ]
        require(len(selected) == 1, "所选执行策略缺少唯一的价格覆盖记录")
        selected = selected[0]
        self.verification(selected, "执行价格覆盖")
        ref = selected.get("data_source", {})
        dataset = datasets.get((ref.get("source_id"), ref.get("dataset_id")))
        require(dataset is not None, "执行价格指向不存在的数据集")
        require("open" in dataset["available_fields"], "执行价格数据未声明 open 字段")
        require(
            text(dataset.get("open_semantic")) and dataset.get("auction_inclusion") is not None, "开盘/竞价口径未核验"
        )
        require(selected.get("data_granularity") == strategy["data_granularity"], "执行价格粒度与运行声明不符")
        require(dataset.get("period") == selected["data_granularity"], "数据集周期与执行价格记录不符")
        return "verified", "实际数据源、许可、时间范围及所选执行价格已有核验证据"

    def profile(self):
        requested = self.settings().get("broker", {}).get("profile")
        paths = sorted(
            path for path in (self.root / "config/broker_profiles").glob("*.yaml") if path.name != "template.yaml"
        )
        profiles = [self.load(path) for path in paths]
        if requested:
            profiles = [profile for profile in profiles if profile.get("profile_name") == requested]
        if not profiles:
            raise NotReadyError("未登记目标柜台能力表")
        require(len(profiles) == 1, "多个候选柜台存在，须在运行配置中明确 profile")
        profile = profiles[0]
        require(profile.get("schema_version") == 1 and text(profile.get("profile_name")), "柜台能力表结构无效")
        contract = self.settings()["data"]["engineering_sample"]["contract"]
        require(contract in profile.get("scope", {}).get("contracts", []), "柜台能力表未覆盖首个实际合约")
        if not text(profile.get("ctp_version")) or profile.get("effective_from") is None:
            self.gap_refs(profile.get("gap_ids"), "柜台版本与适用区间")
        else:
            require(re.fullmatch(r"\d+\.\d+\.\d+", profile["ctp_version"]), "CTP 版本不能使用范围占位符")
            as_time(profile["effective_from"])
        return profile

    def capability_states(self, names):
        profile = self.profile()
        capabilities = profile.get("capabilities", {})
        require(set(CAPABILITIES) <= set(capabilities), "柜台能力表缺少 18 项中的必要能力")

        def leaves(node):
            require(isinstance(node, dict) and node, "能力项为空")
            if "value" in node:
                yield node
            else:
                for child in node.values():
                    yield from leaves(child)

        states = []
        for name in names:
            for leaf in leaves(capabilities[name]):
                if leaf.get("verification_status") == "未启用":
                    require(text(leaf.get("reason")), "未启用能力须说明范围和原因")
                    require(leaf.get("value") is None, "未启用能力不能携带已确认值")
                    continue
                state = self.verification(leaf, name, allow_gap=True)
                if state == "verified":
                    value = leaf.get("value")
                    require(
                        value is not None
                        and value != {}
                        and value != []
                        and (not isinstance(value, str) or text(value)),
                        f"{name} 没有真实核验值",
                    )
                    require(text(leaf.get("source")), f"{name} 未记录依据")
                else:
                    require(leaf.get("value") is None, f"{name} 未核验值必须为空")
                states.append(state)
        require(states, "目标能力未作核验或缺口登记")
        if not text(profile.get("ctp_version")) or profile.get("effective_from") is None:
            states.append("gap")
        return "gap" if "gap" in states else "verified"

    def broker_capabilities(self):
        state = self.capability_states(CAPABILITIES)
        return state, "18 项能力按实际核验或未关闭缺口登记，未核验值不作为柜台默认"

    def registered_rules(self):
        sample = self.coverage()["engineering_sample"]
        time_range(sample.get("time_range"))
        contract = self.settings()["data"]["engineering_sample"]["contract"]
        exchange, symbol = contract.split(".", 1)
        product = re.match(r"[A-Za-z]+", symbol).group()
        covered = set()
        sources = {}
        paths = sorted((self.root / "config/rule_sources/exchanges").rglob("*.yaml"))
        for path in paths:
            record = self.load(path)
            if record.get("is_example") is True or "example" in path.stem or path.name == "template.yaml":
                continue
            require(record.get("schema_version") == 1, "规则登记结构无效")
            scope = record.get("applies_to", {})
            if exchange not in scope.get("exchanges", []) or product not in scope.get("products", []):
                continue
            if scope.get("contracts") and contract not in scope["contracts"]:
                continue
            self.verification(record, "规则 " + str(record.get("rule_id")))
            require(text(record.get("rule_id")) and text(record.get("source_url")), "规则缺少编号或原始来源")
            self.file_ref(record.get("source_document"), "原公告")
            require(record.get("effective_basis") in {"timestamp", "trading_day"}, "规则未声明生效基准")
            start = as_time(record.get("effective_from"))
            end = as_time(record["effective_to"]) if record.get("effective_to") else None
            require(end is None or start < end, "规则生效区间无效")
            published, known = as_time(record.get("published_at")), as_time(record.get("known_at"))
            require(published <= known, "规则可获知时刻不能早于发布时刻")
            if record["effective_basis"] == "trading_day":
                as_date(record.get("effective_trading_day"))
            covered.add(record.get("rule_type"))
            sources[path.relative_to(self.root).as_posix()] = {
                "sha256": sha256(path),
                "rule_type": record.get("rule_type"),
            }
        missing = REQUIRED_RULE_TYPES - covered
        if missing:
            raise NotReadyError("所选样本区间尚缺已核验来源：" + "、".join(sorted(missing)))
        return sources

    def rules(self):
        self.registered_rules()
        return "verified", "目标规则原文和版本已登记；样本适用性由 9.8 的绑定验收记录核对"

    def a25(self):
        data = self.load("config/a25_applicability.yaml")
        require(data.get("schema_version") == 1 and isinstance(data.get("cases"), list), "A25 清单结构无效")
        expected = {
            item["id"] for item in self.load("docs/requirements.json")["acceptances"] if item["id"].startswith("A25-")
        }
        identifiers = [item.get("id") for item in data["cases"]]
        require(
            len(identifiers) == len(set(identifiers)) and set(identifiers) == expected, "A25 编号缺失、重复或被重定义"
        )
        contract = self.settings()["data"]["engineering_sample"]["contract"]
        require(data.get("target_contract") == contract, "A25 清单与首个实际合约不一致")
        has_gap = False
        for case in data["cases"]:
            require(text(case.get("reason")), f"{case['id']} 缺少适用性依据")
            status = case.get("status")
            if status == "未启用":
                require(case.get("scope", {}).get("exchanges"), "未启用场景须明确范围")
            elif status == "待核验":
                self.gap_refs(case.get("gap_ids"), case["id"])
                has_gap = True
            elif status == "适用":
                time_range(case.get("scope", {}).get("time_range"))
                require(case["scope"].get("exchanges") and case["scope"].get("products"), "适用场景缺少范围")
                self.file_ref(case.get("evidence"), case["id"] + " 适用性依据")
            else:
                raise InvalidEvidenceError("A25 适用状态无效")
        return ("gap" if has_gap else "verified"), "沿用原 A25-01—07 编号，适用/未启用/待核验分别登记"

    def ledger(self):
        data = self.load("tests/fixtures/ledger_examples.json")
        require(
            data.get("schema_version") == 1 and data.get("execution_status") == "not_executed",
            "手工账规格结构或状态无效",
        )
        require(data.get("oracle", {}).get("method") == "independent_specification", "手工账缺少独立预期")
        baseline = self.load("docs/requirements.json")["baseline"]
        source = self.file_ref({"path": baseline["source_file"], "sha256": baseline["source_sha256"]}, "冻结规划")
        anchors = set(re.findall(r'<a id="([^"]+)">', source.read_text(encoding="utf-8-sig")))
        require(isinstance(data.get("source_refs"), list) and data["source_refs"], "手工账缺少来源关联")
        for reference in data["source_refs"]:
            filename, separator, anchor = reference.partition("#")
            require(filename == baseline["source_file"] and separator and anchor in anchors, "手工账来源锚点无效")
        cases = data.get("cases", [])
        lookup = {case.get("id"): case for case in cases}
        require(
            len(lookup) == len(cases) and {"cross_day_pnl", "mixed_commission"} <= set(lookup), "基础手工账案例缺失"
        )
        pnl, fee_case = lookup["cross_day_pnl"], lookup["mixed_commission"]
        require(
            {"A06", "A22"} <= set(pnl.get("acceptance_ids", [])) and "A07" in fee_case.get("acceptance_ids", []),
            "手工账验收关联缺失",
        )

        def number(value):
            require(isinstance(value, (str, int)) and not isinstance(value, bool), "规格数值必须使用十进制字符串或整数")
            result = Decimal(value)
            require(result.is_finite(), "规格数值不能为 NaN 或无穷")
            return result

        inputs, expected = pnl["inputs"], pnl["expected"]
        size = number(inputs["lots"]) * number(inputs["multiplier"])
        day_one = (number(inputs["settlement_price"]) - number(inputs["open_price"])) * size
        day_two = (number(inputs["close_next_day"]) - number(inputs["settlement_price"])) * size
        trade = (number(inputs["close_next_day"]) - number(inputs["open_price"])) * size
        require(number(inputs["fee"]) == 0, "基础跨日样例须保持无手续费口径")
        checks = {
            "day_one_settlement": day_one,
            "day_two_mtm_close_pnl": day_two,
            "trade_close_pnl": trade,
            "equity_change": day_one + day_two,
            "final_cash": number(inputs["initial_cash"]) + trade,
        }
        for key, value in checks.items():
            require(number(expected[key]) == value, f"跨日手工账算式不符：{key}")
        require(
            expected["final_position"] == 0 and expected["double_count_trade_pnl"] is False, "跨日结果不满足一次入账"
        )
        inputs, expected = fee_case["inputs"], fee_case["expected"]
        per_lot = number(inputs["lots"]) * number(inputs["fee_per_lot"])
        ad_valorem = (
            number(inputs["lots"]) * number(inputs["price"]) * number(inputs["multiplier"]) * number(inputs["fee_rate"])
        )
        for key, value in {
            "per_lot_component": per_lot,
            "ad_valorem_component": ad_valorem,
            "total_fee": per_lot + ad_valorem,
        }.items():
            require(number(expected[key]) == value, f"手续费手工账算式不符：{key}")
        return "verified", "独立复算跨日 +100/-20/+80 及混合手续费 5 元；规格保持未执行状态"

    def engineering_sample(self):
        sample = self.coverage().get("engineering_sample", {})
        contract = self.settings()["data"]["engineering_sample"]["contract"]
        require(sample.get("contract") == contract, "工程样本与运行声明的合约不一致")
        sample_range = time_range(sample.get("time_range"))
        self.verification(sample, "工程样本")
        artifacts = sample.get("artifacts", {})
        require(isinstance(artifacts, dict) and REQUIRED_SAMPLE_ROLES <= set(artifacts), "工程样本文件类型不完整")
        for role in REQUIRED_SAMPLE_ROLES:
            self.file_ref(artifacts[role], "工程样本 " + role)
        report = self.load(self.file_ref(sample["evidence"], "样本验收报告"))
        require(report.get("schema_version") == 1 and report.get("contract") == contract, "样本验收报告合约不一致")
        require(time_range(report.get("time_range")) == sample_range, "样本验收报告区间不一致")
        required_checks = ("fields_complete", "execution_price_covered", "time_range_covered", "rules_covered")
        require(all(report.get("checks", {}).get(key) is True for key in required_checks), "工程样本验收存在未通过项")
        for role in REQUIRED_SAMPLE_ROLES:
            require(
                report.get("artifact_hashes", {}).get(role) == artifacts[role]["sha256"], "样本验收报告与数据版本不一致"
            )
        registered = self.registered_rules()
        used = report.get("rule_source_hashes")
        require(isinstance(used, dict) and used, "样本验收报告未绑定所用规则来源版本")
        types = set()
        for relative, digest in used.items():
            require(relative in registered and registered[relative]["sha256"] == digest, "样本规则来源版本不一致")
            types.add(registered[relative]["rule_type"])
        require(REQUIRED_RULE_TYPES <= types, "样本验收未覆盖所需规则类型")
        return "verified", f"{contract} 的完整小样本已到位，文件版本与验收记录一致"

    def research(self):
        research = self.coverage().get("research_dataset")
        require(isinstance(research, dict), "研究数据采购记录缺失")
        requirements = research.get("requirements", {})
        require(
            requirements.get("min_years", 0) >= 8 and requirements.get("min_products", 0) >= 20,
            "采购范围未满足原研究要求",
        )
        require(text(requirements.get("cycles")), "研究采购未声明跨周期要求")
        if research.get("status") not in {"采购中", "已到位"} or not research.get("procurement_started"):
            raise NotReadyError("研究数据采购尚未启动；完整数据须在 M4 评价前到位")
        as_date(research["procurement_started"])
        require(text(research.get("data_source")), "未确定研究数据来源")
        self.file_ref(research.get("procurement_evidence"), "采购启动证据")
        return "verified", "采购范围和启动证据已记录；M4 是多品种研究交付里程碑"

    def gap_registration(self):
        gaps = self.gaps()
        for relative in ("config/data_coverage.yaml", "config/a25_applicability.yaml"):
            data = self.load(relative)

            def walk(node, label, inherited=()):
                if isinstance(node, dict):
                    refs = node.get("gap_ids", list(inherited))
                    state = node.get("verification_status", node.get("status"))
                    if state in PENDING_STATUSES or state in {"登记缺口", "待核验", "未启动"}:
                        self.gap_refs(refs, label)
                    for key, value in node.items():
                        if not key.endswith("_template"):
                            walk(value, label, refs)
                elif isinstance(node, list):
                    for value in node:
                        walk(value, label, inherited)

            walk(data, relative)
        return "verified", f"{len(gaps)} 条缺口的范围、责任、关闭条件和需求关联有效"

    def account(self):
        names = ("floating_profit_usage", "margin_rates", "commission_rates", "stage_permissions", "front_maintenance")
        state = self.capability_states(names)
        return state, "首个合约的资金、时段、竞价与维护能力分别核验或关联未关闭缺口"

    def run(self):
        results = []
        for ident, name, method in CHECKS:
            try:
                status, detail = getattr(self, method)()
            except NotReadyError as exc:
                status, detail = "pending", str(exc)
            except (
                InvalidEvidenceError,
                KeyError,
                TypeError,
                ValueError,
                AttributeError,
                OSError,
                InvalidOperation,
            ) as exc:
                status = "invalid"
                detail = (
                    str(exc) if isinstance(exc, InvalidEvidenceError) else f"证据结构不符合要求（{type(exc).__name__}）"
                )
            results.append(Result(ident, name, status, detail))
        return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/settings.yaml", help="Actual local configuration, not the example")
    parser.add_argument("--environment-report", default="runs/s0/environment.json")
    parser.add_argument("--json", action="store_true", help="Print machine-readable results")
    args = parser.parse_args(argv)
    results = S0Checker(config=args.config, environment=args.environment_report).run()
    ready = all(result.status in {"verified", "gap"} for result in results)
    if args.json:
        print(
            json.dumps(
                {"stage": "S0", "ready": ready, "results": [item._asdict() for item in results]},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        labels = {"verified": "已验证", "gap": "按计划登记缺口", "pending": "待完成", "invalid": "记录无效"}
        for result in results:
            print(f"[{labels[result.status]}] {result.check_id} {result.name}: {result.detail}")
        print("S0 出口条件满足。" if ready else "S0 尚未满足出口条件；已登记缺口不替代工程样本与采购启动。")
    return 0 if ready else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
