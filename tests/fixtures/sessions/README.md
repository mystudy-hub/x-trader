# A25 时段权限夹具 (S3-10)

每个子目录对应一个 A25 子用例，至少包含：

- `calendar.json`：`TradingCalendar.from_file` 可直接加载的版本化 Sessions（合成规则，非交易所公告）；
- `expected.json`：固定输入与独立预期（撤单到达时刻、原单状态、预占、成交入账次数）；
- 测试：`tests/unit/test_s3_session_fixtures.py` 通过 `CalendarSessionGate` + `SimulatedGateway` + `BacktestEngine` 逐项比对。

| 目录 | 子用例 | 状态 | 说明 |
| :--- | :--- | :--- | :--- |
| `A25-04/` | A25-04 撤单边界；A25-01 小节休市 | 已由单元测试执行（合成规则） | 左闭右开 08:59:00；被拒撤单不终结原单、不释放预占；随后竞价成交只入账一次 |
| （未建） | A25-02 夜盘残留单 | 待核验 | 需要夜盘→日盘跨时段的残留单夹具与 CZCE 只撤模板 |
| （未建） | A25-03 竞价制度历史版本 | 待核验 | 需要 2023-05-26 生效边界的原公告归档与新旧两版 Sessions |
| （未建） | A25-05 节假日公告 | 待核验 | 需要节假日公告归档 |
| （未建） | A25-06 中金所国债 | 未启用 | 本期不启用中金所 |
| （未建） | A25-07 竞价数据缺失降级 | 部分 | `AuctionFillPolicy.REJECT` 已由 `test_simulated_gateway.py::test_a25_07_*` 覆盖；ExecutionReference 降级路径未接入 |

“已由单元测试执行”只证明规则边界逻辑，不证明历史制度或当前柜台行为；正式出口仍以 09 与 `config/a25_applicability.yaml` 为准。
