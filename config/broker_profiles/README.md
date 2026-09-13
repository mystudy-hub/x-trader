# 柜台能力与配置目录 (Broker Profiles)

本目录用于存放目标期货公司柜台的特性映射文件与联调能力证据（对应规划 §4.5 与需求 `FR-ORD-01`, `FR-ORD-02`, `FR-LIVE-04`）。

## 配置文件规范

每个柜台以 `<broker_id>_<profile_name>.yaml` 命名，例如 `9999_openctp_sim.yaml`。

包含字段：

- `broker_id` / `broker_name`: 柜台标识与全称
- `exchange` / `product` / `broker_profile`: 适用交易所、品种与账户配置范围
- `ctp_api_version`: CTP 头文件与动态库版本
- `effective_from` / `effective_to`: 能力适用区间，历史版本保留
- `capabilities`:
  - `supports_forquote`: 是否支持询价
  - `supports_parked_order`: 是否支持预埋单
  - `max_query_rate_per_sec`: 单连接每秒最大查询次数（流控阈值）
  - `close_order_mapping`: 平今/平昨报单语法映射（上期所平今 `THOST_FTDC_OF_CloseToday` vs 普通平仓）
- `verification`:
  - `status`: 未开始 / 进行中 / 已核验 / 登记缺口
  - `source_ref`: 期货公司说明或原始接口文档
  - `verified_at`: 联调通过日期
  - `evidence_ref`: 对应脱敏测试日志或工单编号

每项能力分别登记值、范围、来源与核验状态；未核验值保持 null 或登记缺口，不作为柜台默认能力。完整字段及核验项以 [09 柜台能力表](../../docs/09_前期准备与规则核验清单.md) 为准；本目录当前只有规范说明，实际能力表在 S0 建立。
