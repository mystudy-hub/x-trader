# 柜台能力与配置目录 (Broker Profiles)

本目录用于存放目标期货公司柜台的特性映射文件与联调能力证据（对应规划 §4.5 与需求 `FR-ORD-01`, `FR-ORD-02`, `FR-LIVE-04`）。

## 配置文件规范

每个柜台以 `<broker_id>_<profile_name>.yaml` 命名，例如 `9999_openctp_sim.yaml`。

包含字段：
- `broker_id` / `broker_name`: 柜台标识与全称
- `ctp_api_version`: CTP 头文件与动态库版本
- `capabilities`:
  - `supports_forquote`: 是否支持询价
  - `supports_parked_order`: 是否支持预埋单
  - `max_query_rate_per_sec`: 单连接每秒最大查询次数（流控阈值）
  - `close_order_mapping`: 平今/平昨报单语法映射（上期所平今 `THOST_FTDC_OF_CloseToday` vs 普通平仓）
- `verification`:
  - `verified_at`: 联调通过日期
  - `evidence_ref`: 对应脱敏测试日志或工单编号
