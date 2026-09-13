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

运行配置的 `broker.profile` 指向登记的 `profile_name`。S0 检查允许明确的未关闭缺口；这只说明登记完整，不能使未知能力成为实盘默认值。实际报撤、查询和账户资金语义继续在 S5 联调时核验。
