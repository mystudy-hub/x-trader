# 规则来源登记

关联需求：FR-RULE-04、FR-RULE-01、FR-CAL-01、FR-CAL-08、FR-CAL-10。核验范围见 [09 §7](../../docs/09_前期准备与规则核验清单.md)。

[template.yaml](template.yaml) 定义登记结构。当前只有模板和 [合成计费示例](examples/rb_commission_example.yaml)，尚无已核验的目标样本规则原件。示例不表示实际交易所费率，不参与 S0 验收。

## 文件安排

- `exchanges/<交易所>/`：取得原文后建立实际公告登记。当前 S0 检查在此读取样本适用的登记。
- `examples/`：明确标记 `is_example: true` 的格式示例。
- 公告原件可归档到 `docs/references/` 的主题子目录；受许可限制的材料保存在本地 `data_storage/`，登记相对路径及哈希。
- 柜台特定行为在 [broker_profiles/](../broker_profiles/README.md) 中保留说明与实测证据，实际账户规则在后续规则库接入时关联。

## 必要字段

| 字段 | 填写方式 |
| :--- | :--- |
| `schema_version`、`rule_id`、`rule_type` | 当前结构版本为 1，编号稳定，类型区分手续费、保证金、时段及其他规则 |
| `title`、`issuer`、`source_url` | 原始标题、发布机构和可追溯来源 |
| `source_document` | `path` 与原件 `sha256`；路径相对于仓库根目录 |
| `published_at`、`known_at` | 发布时间和允许获知时间，均为带时区 ISO 8601 时刻，不能以网页查看日期替代 |
| `effective_basis` | `timestamp` 或 `trading_day` |
| `effective_trading_day` | 按交易日生效时保留原始交易日，并按版本化日历解析物理时间 |
| `effective_from/effective_to` | 带时区物理时间，左闭右开；无终止时刻时后者为空 |
| `applies_to` | 明确交易所、品种及适用实际合约范围 |
| `verification_status`、`gap_ids` | 未核验项关联 [gaps.yaml](../gaps.yaml)，不编造日期或费率 |
| `verified_at`、`verified_by`、`evidence` | 核验日期、核验人和核验记录的 `path/sha256` 引用 |
| `details`、`replaces/replaced_by` | 规则内容及新旧版本关系 |

S0 至少检查目标样本的手续费、保证金和交易时段原件，并由样本核验记录绑定登记文件的哈希。其余公告按已启用范围补齐。样本日期尚未确定、只有示例或原件缺失时，检查保留为待完成。

S1 规则库将分别处理业务生效时刻和可获知时刻，并验证适用版本唯一性。当前规则不能回填历史；晚发布修订不能改写过去的策略决策。
