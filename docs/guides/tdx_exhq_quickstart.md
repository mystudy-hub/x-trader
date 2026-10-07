# 通达信扩展行情接入

通达信只作为研究补充行情源。成交额原生缺失；日线 `settlement_proxy` 是服务端自算均价，不是交易所官方结算价。接入不需要账户或运行通达信客户端，协议实现仅使用标准库。

## 本轮交付范围（2026-10-07）

本轮交付 P0–P2：八类只读协议查询、实际合约下载及原帧归档、按会话聚合、显式研究发布、质量报告和 S0 探测证据挂接。支持范围与已取得数据分别如下：

| 层次 | 覆盖 | 限制 |
| :--- | :--- | :--- |
| 正式下载入口 | CZCE / DCE / SHFE / CFFEX / GFEX 已解析实际合约；1d、1m、5m、15m、30m、1h | 多分钟周期由 1m 聚合并要求版本化日历；市场映射不等于全市场数据已核验；INE 默认拒绝 |
| 实际合约现场证据 | RB2701 的 1d / 1m / 原生 30m 各采样 64 根；另归档 2026-09-30 单根日线 | 64 根采样均标记截断；RB2410 日线返回空；未发布真实 30m 规范研究数据 |
| 主连探测 | RBL8 同样取得三个周期各 64 根 | 只作原始观察，不能作为实际合约执行价 |
| 既有加权日线归档 | 85 个 L9 序列，221,506 根；总体日期包络 2000-01-04 至 2026-09-30 | 各序列起点不同；覆盖仅指本次节点可枚举历史，非交易所完整历史或逐合约历史 |

加权归档位于 `data_storage/raw/tdx_weighted/20261006_daily/`，`summary.json` / `summary.csv` 登记逐序列覆盖，`all_weighted_1d.csv.gz` 为合并文件。既有落盘核验记录包含 449 个原始分页、85 个 CSV 哈希与总记录数一致。3 根 OHLC 异常原样保留并标记 `INVALID`，不能作为有效 Bar 使用；全部记录保留 `TURNOVER_UNAVAILABLE`。本轮只登记这一既有本地归档，不将原始数据或 `runs/` 内的一次性采集脚本提交到 Git，也未提供 L9 批量下载的正式 CLI。三个含连字符的月均价加权代码仅由既有采集脚本处理，正式客户端代码校验仍不接纳连字符。

目录实际可枚举 106,836 条，服务端声明 106,855 条，差额 19 条保留为来源限制。85 个国内加权的目录核对不代表整个服务端目录计数一致。目录中的市场 30 同时含 INE 加权，这不改变正式实际合约入口对 INE 的默认限制。

**策略使用边界**：`scripts/validate_strategy.py` 和实时策略预热仍要求 `QualityFlag.OK`，会拒绝带 `TURNOVER_UNAVAILABLE` 的数据。本轮不放宽这些门禁；研究发布成功不等于 EMA A/B 验证或 SimNow 策略启动就绪。历史聚合丢弃短尾桶、实时聚合保留短尾桶，二者也不能未经核对就当作同一 Bar 序列。可信 30m 数据、时间证据、当前日历及预先固定的样本切分仍需另行完成。

授权、分钟标签和量仓口径、节点稳定性、官方结算核验、长期逐合约数据及 P3 持续 CTP 互检均未在本轮完成；`GAP-TDX-01` 与既有 S0/S5 出口保持未关闭。

## 显式假设下的 EMA 研究回测（2026-10-07 增量）

用户确认先忽略成交额、获取 30m 数据并进行回测后，新增 `scripts/run_tdx_ema_research.py`。
此入口复用 EMA A/B 和事件驱动内核，必须显式传入 `--research`；原有 `validate_strategy.py` 的数据检查和实盘预热继续拒绝缺成交额及带时间假设的数据。

先用原始下载入口取得同一实际合约的 `1d,1m` 归档，再运行：

```powershell
.\.venv\Scripts\python.exe scripts/run_tdx_ema_research.py --research --symbol SHFE.rb2701 --minute-raw <分钟原始归档.json> --daily-raw <日线原始归档.json> --catalog <对应合约目录.json>
```

入口校验原始文件名与内容哈希，按数据质量选取最近连续、每分钟完整的区间，在运行策略前固定扣除预热后的 70% / 30% 交易日切分。以合约目录的乘数和最小变动、配置中的手续费/保证金/滑点假设运行；日终按最后收盘价模拟结算。输出独立 `runs/tdx_ema_research/<时间>/dataset` 快照、30m CSV、逐日质量记录、实验配置与 A/B 报告。

本次 rb2701 原始分钟标签的夜盘日期是所属交易日：按该标签汇总的 172 日成交量与日线全部一致。研究入口按前一交易日重排夜盘，不将其放在同日白盘之后；首日缺前序交易日证据，予以排除。该解释、时段模板及历史可见时间仍标记为研究假设，不能作为正式日历核验。现有规范 Bar 类型用数值占位表示缺成交额，但原始归档/研究 CSV 保持缺失，规范记录必须保留 `TURNOVER_UNAVAILABLE`，不得将占位值视为观测值。

分钟缺失、重复、时段外数据、开收盘价/成交量/收盘持仓量不一致或越过同源日线价格范围均不纳入连续区间。分钟极值窄于日线的差异单独披露，采用分钟原值；不能用日线极值回填分钟。每小节不足 30m 的短尾不参与信号及撮合，完整保留原始缺口报告，仅豁免可精确对应会话短尾的缺口。

本次取得 54,528 根 1m，选取 2026-08-12 至 2026-09-30 的 35 个交易日，聚合 381 根完整 30m；每段预热 210 根，样本外从 2026-09-23 开始。该短样本研究结果不能替代策略稳定性验证或 S0/S5 出口。

## 原始下载

### 全品种加权指数近五年归档（2026-10-07 增量）

新增独立批量入口 `scripts/download_weighted_history.py`，刷新国内加权目录，下载指定日期内的原生日线及 30m，并保存逐页报文、哈希、单序列 CSV、合并压缩 CSV 和覆盖清单。该入口兼容目录已登记的三个大商所月均价代码，不改变实际合约入口的代码校验。

```powershell
.\.venv\Scripts\python.exe scripts/download_weighted_history.py --start 2021-10-07 --end 2026-10-07 --output data_storage/raw/tdx_weighted/20261007_5y --workers 3
```

同一输出目录可复用已完成且文件哈希一致的数据集；请求日期变化必须换目录。短页继续翻页，直到覆盖请求起点或来源返回空页；来源页数达到上限、内容冲突或复读首批不一致均明确失败。夜盘源标签与交易时序的分页交错通过全字段核对及唯一键合并处理，不把分页顺序冒充自然时间顺序。

指定品种的全部来源历史可用 `--all-history --codes FGL9` 替代 `--start`，例如：

```powershell
.\.venv\Scripts\python.exe scripts/download_weighted_history.py --all-history --codes FGL9 --end 2026-10-07 --output data_storage/raw/tdx_weighted/20261007_FGL9_full --workers 2
```

玻璃本次日线为 3,358 根，30m 为 23,612 根，日期包络 2012-12-03 至 2026-09-30。两周期均读到来源空页；**30m 中间缺少 2013-11-20 至 2019-02-15 的 1,277 个日线日期**，不得标为连续完整分钟历史。两台公开备用节点连接/握手失败。缺口和交叉核对见该输出目录 `coverage_audit.json`、`alternate_nodes.json`。

本次取得 **85 个序列 × 2 周期，170 个数据集**：87,716 根日线、1,009,060 根原生 30m，来源最新日期为 2026-09-30。两个周期各有 64 个序列的历史覆盖请求起点，21 个起点较晚；具体见输出目录的 `coverage.csv`。1,677 个原始行情分页及 170 个 CSV 哈希与计数已核对，请求区间内未检出 OHLC 区间异常。刷新目录后声明与实际枚举差额为 14 条，保留此来源限制。

附加覆盖核对见输出目录 `coverage_audit.json`：82 个序列的日线日期与 30m 来源标签日期集合一致；菜籽、胶板、线材共 182 个日线日期无 30m，其中 181 日的来源日线成交量为零，菜籽 2021-10-25 日线成交量为 2，30m 未返回。均不填造行情。原生 30m 的夜盘日期、跨休市分桶与加权规则尚未规范化，这些原始数据不自动成为 EMA 规范输入或实际合约成交价。

在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts/download_data.py --source tdx --symbols SHFE.rb2701 --intervals "1d,1m" --start-date 2026-09-28 --end-date 2026-09-30
```

默认只写 `data_storage/raw/`，保留原始响应、来源标记和缺失字段，不发布规范数据。实际合约沿用项目写法，发送到协议时转成大写；郑商所须先解析到四位年月，不能直接传入有歧义的三位短码。INE 市场编号未核验，默认拒绝。节点配置为 `config/tdx_exhq_servers.yaml`，使用 JSON 兼容 YAML 格式；命令行可通过 `--tdx-servers` 指定另一个节点文件。

分页到空页或覆盖请求起点才算完成；短页继续按实际返回条数推进。到达 `--tdx-max-pages` 上限时明确失败，应收窄日期范围或调高页数，不能把截断结果当完整历史。响应合约与请求不符时拒绝解析，不能把其他合约数据归档到当前合约。

## 30 分钟研究发布

30m、5m、15m、1h 均抓取原生 1m 后自聚合，要求提供实际合约的显式日历：

```powershell
.\.venv\Scripts\python.exe scripts/download_data.py --source tdx --symbols SHFE.rb2701 --intervals 30m --start-date 2026-09-28 --end-date 2026-09-30 --calendar runs/my_calendar.json
```

`runs/my_calendar.json` 是需准备的真实日历路径示例。按每个连续竞价时段起点分桶，跨小休不拼接；缺任意一分钟、未满周期、会话末尾短桶均丢弃。完整桶窗口与现有实盘会话锚点相同，但实盘聚合器保留短尾桶。提供日历时，日期筛选按交易日所属会话覆盖夜盘；无日历的原始 1m 下载只能按自然时间筛选，不声明交易日归属。

规范研究发布必须提供三项导入证据并显式启用研究模式：

```powershell
.\.venv\Scripts\python.exe scripts/download_data.py --source tdx --symbols SHFE.rb2701 --intervals 30m --start-date 2026-09-28 --end-date 2026-09-30 --research --publish --catalog runs/my_catalog.json --calendar runs/my_calendar.json --timings runs/my_timings.json
```

上述目录和时间证据路径也是示例，须替换为已准备文件。时间证据格式沿用 `load_import_metadata`：`schema_version: 1`、`bar_timings`（日线以交易日为键，分钟线以 UTC 结束时刻为键）。不得使用未来才可见的数据，聚合起止、交易日和会话与外部时间证据不一致即拒绝。发布保留 `TURNOVER_UNAVAILABLE`，回测报告显示品质警告；精确模式 `require_turnover=True` 仍拒绝缺成交额。

主连 `RBL8` 等仅供协议或探测观察。缺少历史逐合约量仓与切换价差时不能生成可信复权主连，更不能伪装为真实合约成交或移仓回测。

## 只读探测与环境登记

```powershell
.\.venv\Scripts\python.exe scripts/tdx_probe.py --max-pages 2 --output runs/s0/tdx_runtime_evidence.json
.\.venv\Scripts\python.exe scripts/init_env.py --tdx-evidence runs/s0/tdx_runtime_evidence.json
```

探测报告记录节点握手与延迟、市场/目录、历史深度、缺字段、过期合约和主连跳空样本。深度遇页数上限标为截断；默认不宣称完整深度或三方对拍通过。对拍需另给 `--calendar`、`--comparison-day`、`--comparison-symbol` 与匹配的 `--calendar-symbol`，以日历推导夜盘归属；必要时增加 `--max-pages`。`--price-tick` 必须来自已知合约规则。

证据只能写入项目 `runs/s0/`。`init_env` 挂接保留研究性质，不关闭 `GAP-S0-02`、`GAP-S0-03`、`GAP-TDX-01`。2026-10-06 已修正方案握手包重复 16 字节的笔误，并实测取得 RB2701 日线；自动化测试使用离线响应及匿名帧回放，公网采样独立进行。P3 持续 CTP 行情互检进程尚未启用。
