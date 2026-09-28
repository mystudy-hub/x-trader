# 柜台能力登记

关联需求：FR-ORD-01、FR-ORD-02、FR-CAL-07、FR-LED-05、FR-LED-06。完整核验项见 [09 §4](../../docs/09_前期准备与规则核验清单.md)。

[template.yaml](template.yaml) 是填写模板；[simnow_v6.yaml](simnow_v6.yaml) 是当前候选环境登记，18 组能力尚未实测。文件名不代表已确认的 CTP 版本，也不证明柜台支持其中的能力。

## 填写约定

- 顶层使用 `schema_version: 1`，填写 `profile_name` 及 `scope` 中的交易所、品种和实际合约。
- `ctp_version` 核验后填写具体三段版本；`effective_from/effective_to` 使用带时区时刻。尚未核验时保留 `null`，关联有效的 `gap_ids`。
- `capabilities` 按模板保留 18 组能力；可以按交易所或子能力继续分层，每个叶子独立登记。
- 未核验叶子的 `value` 必须为 `null`，`verification_status` 使用 `未开始`、`进行中` 或 `登记缺口`；登记缺口时引用 [gaps.yaml](../gaps.yaml) 中尚未关闭的编号。
- 叶子标为 `已核验` 时填写明确的 `value`、`source`、`verified_at`、`verified_by` 和 `evidence: {path, sha256}`。证据路径相对于仓库根目录。
- 目标范围以外的叶子可标为 `未启用`，但须填写 `reason`，值仍为空。
- `evidence_level`、`test_id` 用于区分本地受理、柜台接受和交易所受理，并关联脱敏联调记录。头文件中存在枚举不能替代实测。
- `fronts`、`account`、`terminal_info`、`native_libs` 是接入参数段：`fronts` 登记交易 / 行情前置候选；`account` 只登记用户号等非秘密项（口令与 AuthCode 只从本地环境变量读取，从不入库）；`terminal_info` 登记看穿式采集模式（`none` / `direct` / `relay`）；`native_libs` 登记该环境要求的原生库（`flavor`、`api_marker`、`staging_dir`、归档 URL 与逐平台文件 sha256）。
- `native_libs` 是硬门禁而不是建议：登记了就必须装载成功（缺文件 / 未登记摘要 / 摘要不符即失败），且 `GetApiVersion()` 必须含 `api_marker`，否则拒绝连接（GAP-S0-01）。绑定按 delvewheel 摘要文件名加载原生库，换库必须走预装载，不能只改目录。下载与校验见 `scripts/fetch_ctp_native_libs.py`。
- 本机账号标识与前置放在 git-ignored 的运行配置（`config/settings.yaml` 或 `config/settings.<环境>.local.yaml`）与 `config/secrets.yaml`：登记文件里不写账户标识，口令只从 `QH_CTP_PASSWORD` 读取。探针在 `--config` 的 `broker.profile` **与所选登记一致**时才从中取 `user_id` / `investor_id`，避免把另一个环境的账号带进来。

运行配置的 `broker.profile` 指向登记的 `profile_name`。S0 检查允许明确的未关闭缺口；这只说明登记完整，不能使未知能力成为实盘默认值。实际报撤、查询和账户资金语义继续在 S5 联调时核验。
