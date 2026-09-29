# QH-Trader

面向中国期货的策略研究、回测与自动交易工具。项目规划通过共享交易内核，让回测与实盘复用订单状态机、持仓、资金账本、交易规则和事前风控。

首版目标是在 Windows 上交付单账户、普通期货、日线 / 小时级 Bar 回测与 CLI 报告，并提供向量化快速研究通道；后续按阶段扩展多品种、模拟盘、影子模式和实盘能力。

## 当前进度

2026-09-29 本轮推进：交付 [S5 策略进程、查询成交恢复及备份重放](docs/06_开发计划.md#s5-local-recovery-runtime)。独立策略订阅已提交的 Bar / 回报，通过耐久 outbox 投递命令，固定控制代次并校验执行心跳；异步双均线示例等待在途订单完成，反转先平后开。查询发现的可归属成交经唯一执行服务入账，再重新查询对账。新增 `scripts/backup_state.py` 整库一致性备份、隔离恢复和 `scripts/replay_events.py` 离线事务重放；恢复副本带数据库内门禁，不能直接启动执行服务。同时修复 CTP 装配退出未释放交易 / 行情接口的资源泄漏、探针提前退出未清理连接及测试漏用真实行情绑定的问题。证据为本地单元测试、实际策略子进程与纸面执行服务演练；Tick → Bar、生命周期调度、外部成交人工归属、策略状态配套备份、完整柜台故障演练与一个完整自然月仿真仍待交付，S5 出口未通过。下方按日期保留历史增量，最新状态以本段及 06 / 07 的对应记录为准。

2026-09-28 增量：已交付 [S0-02 原生库登记与 openctp TTS 接入通道](docs/06_开发计划.md#s0-02-native-libs)。`gateway/ctp_native_libs.py` 按柜台登记装载指定版本的原生库：先发现绑定要求的 delvewheel 摘要文件名（`thosttraderapi_se-<摘要>.dll`），逐文件校验 sha256 后复制到项目内暂存目录（不改 `site-packages`），在导入绑定之前预装载进进程，并在注册前置前用 `GetApiVersion()` 复核 `api_marker`；未登记摘要、缺文件、摘要不符、导入顺序颠倒一律 `NativeLibError`，不回退到绑定自带库。`config/broker_profiles/openctp_tts.yaml` 登记 TTS 版库的归档 URL、sha256 与逐平台文件摘要；`scripts/fetch_ctp_native_libs.py` 按登记下载并逐层校验（归档 → 成员 → 落盘，产物在 git-ignored 的 `vendor/`）；`scripts/ctp_probe.py --dummy-login` 提供无凭据联调入口。实测（本机 Windows，2026-09-28）：装载 TTS 版库（`openctp-tts v6.7.11`）后 openctp TTS 交易前置握手成功（`front_connected=1`）且柜台在登录请求上回错误码 1005，行情前置 MdApi 登录成功并收到 `SHFE.rb2610` 快照（柜台交易日 2026-09-11，6~8 秒 1 笔，7x24 按最近交易日 Tick 回放）；对照实测中装载绑定自带官方库时同一交易前置陷入 4097 断开循环（`front_disconnected=10`）。拿到 openctp 公众号发放的 7x24 与仿真账号后，两个环境均完成登录、资金 / 持仓 / 报单 / 成交查询，并跑通 `SHFE.rb2610` 限价开仓（取柜台跌停价，不会成交）+ 按原会话三元组撤单的报撤单闭环（此前 SimNow 因休市日两次拒撤）；账号标识只写在本机 git-ignored 配置，口令只从 `QH_CTP_PASSWORD` 读取。同时发现 TTS 的 `ReqQryProduct` 把乘数与最小变动恒返 0，品种级比对在 TTS 不可用，须改用合约级 `ReqQryInstrument`（实测 rb2610 乘数 10 / 最小变动 1.0，与本地登记一致）。本轮又把联调暴露的问题修掉：口径比对不再把柜台返回的 0 当成登记错误（新增合约级 `ReqQryInstrument` 核验，25281 个合约 0 不一致）；接入柜台费率 / 保证金查询（真实市场合约返回空、柜台自有合约有值，因此本地品种仍无柜台口径可比）；新增柜台口径合约目录生成器，实盘装配因此对 TTS 跑通「连接 → 隔离 → 提升代次 → 对账 → 放行」（`ready=True`，并挡下了一次本地资金与柜台不一致）；新增持仓探测（穿价建仓 → 逐个开平标志平仓 → 收尾零仓），实测平今 `'3'` 可用、平仓 `'1'` 与平昨 `'4'` 被拒（1009），市价单被接受并成交，登记对应项据此标为已核验；并修掉拒单回报的转换缺陷（柜台拒单不带 `TradingDay`，改用会话交易日补齐，拒单能表达为 `REJECTED` 且错误码可见）。**以上是本地单元测试与仿真柜台实测证据**：昨仓与分配顺序、市价单在其他交易所 / 柜台、真实市场合约费率与保证金口径、部分成交与外部委托，以及期货公司柜台联调仍待交付；GAP-S0-01 与 GAP-S0-05 未关闭，S5 阶段出口不变。

2026-09-28 增量：已交付 [S5-03 终端采集与认证适配](docs/06_开发计划.md#s5-03-terminal-info) 的本地实现：`gateway/terminal_info.py`。适配器动态加载官方 `WinDataCollect.dll` / `libDataCollect.so`，防御性解析 MSVC C++ 符号与 C 导出，调用 `CTP_GetDataCollectApiVersion()` 与 `CTP_GetSystemInfo()` 提取硬件特征载荷与脱敏摘要；支持直连（`direct`，进程内预先核验）与中继模式（`relay`，在认证后、登录前显式调用 `RegisterUserSystemInfo`，以 `memmove` 精确写入 `CThostFtdcUserSystemInfoField` 避免截断）。缺库、载荷长度非法或采集失败抛出 `TerminalInfoError` 并关闭交易门禁（`ready_to_send=False`，A27 / F17）；报告与快照只记录脱敏摘要 `masked_digest`（`sha256:` 前缀）及长度，绝不落盘原始硬件指纹或二进制载荷（NFR-06）。采集库 SHA-256 哈希写入 `CtpSessionReport.dll_hashes`。**以上是本地单元测试与原生动态库实测证据**；看穿式前置白盘联调受限于柜台时段，S5-05 实盘引擎与 S5-12 月度仿真仍待交付，S5 阶段出口不变。

2026-09-27 增量：已交付 [S5-06 看门狗与人工控制](docs/06_开发计划.md#s5-06-watchdog-control) 的本地实现。`scripts/watchdog.py` 按心跳序号推进与本机单调时钟判断存活，每个事件只告警一次；策略失联时可按配置写入接管申请，前提是执行服务仍在运行并已就绪；执行服务自身失联只告警。申请本身不授予交易权，须由新实例按"隔离 → 提升代次 → 对账 → 放行"受理。`scripts/control.py` 提供状态查询、暂停、只减仓、恢复（须确认原因已消除且账户一致）、撤单、人工平仓与接管申请；只有当前控制者能发出影响交易状态的命令，所有命令都写入命令表，由唯一执行服务再次校验后执行。**以上是本地单元测试与纸面模式证据**：接管后的自动撤单 / 减仓、策略进程与外部告警通道尚未交付，A23 柜台联调未进行，S5 阶段出口不变。

2026-09-25 增量：[S5-04 日终检查点与增量提交](docs/06_开发计划.md#s5-04-r11-checkpoint) 实现了 06 R11 的缓解措施。实测发现，改动前每次提交都会重写并重新解码全部账户事实：每日 20 笔委托的负载下，第 4 个交易日单条命令已需 851 ms，库文件按平方增长。现在事实逐条存储，存储层缓存已提交快照；日终结算完成后，以经过逐项校验的内核检查点替代已结算的事实前缀，终态委托在第二个日终边界退役，结算单比对保留最近已结算交易日的口径。同一负载下单条命令降到 17 ms；每日 40 笔委托、400 笔行情连续 20 个交易日，耗时逐日持平。**以上是本地负载基准**，不连接柜台，S5-12 连续仿真中须复测；撤单闭环仍待 2026-09-28 交易时段在 SimNow 重测，S5 阶段出口不变。

2026-09-24 增量：已交付 [S5-01 CTP 网关与 S5-02 回报归一化](docs/06_开发计划.md#s5-01-ctp-gateway) 的本地实现：`gateway/ctp_gateway.py`（握手、代次复核、按柜台 `MaxOrderRef` 分配并持久化委托号、回调只入队）、`gateway/feedback_normalizer.py`（报单 / 成交 / 纠错回报归一化）、`gateway/ctp_query.py`（资金 / 持仓 / 报单 / 成交与合约查询）、`gateway/ctp_market.py`（MdApi 行情订阅与 Tick 归一化），`--mode live` 装配按"连接 → 隔离 → 提升代次 → 对账 → 放行"推进。CTP 绑定锁定为 `openctp-ctp==6.7.11.0`（`uv sync --extra ctp`），SimNow 前置候选与接入参数已登记，`scripts/ctp_probe.py` 生成脱敏登录 / 查询 / 报单证据。**未核验能力一律禁用**：今昨仓映射与市价单未核验即拒发，撤单必须带原会话三元组。候选环境已登记 openctp TTS（7x24 `trading.openctp.cn:30001`，需注册账号并换用 TTS 版原生库）。柜台口径比对（探针 `--verify-catalog`）显示本地登记的 10 个品种在**乘数与最小变动上与柜台一致**，手续费 / 保证金比例仍缺柜台口径。**已在 SimNow 7x24 环境实测跑通登录、资金 / 持仓 / 报单 / 成交查询与一笔开仓报单（柜台回报 `ACCEPTED` 并按原会话三元组归属）；撤单被柜台以错误码 25 拒绝，原因是当天为中秋节休市日，须在 2026-09-28 交易时段重测**，GAP-S0-01 / GAP-S0-05 未关闭，S5-03 终端采集、S5-05 实盘引擎、S5-06 看门狗与 S5-12 月度仿真仍待交付，S5 阶段出口不变。

2026-09-23 增量：已交付 [S5-04 实盘账户模型与运行入口、S5-08 结算单比对](docs/06_开发计划.md#s5-04-live-assembly)。`scripts/run_execution_service.py` 可在纸面模式下完成接管、对账、放行与主循环，实盘模式在 CTP 网关交付前拒绝启动；`scripts/parse_statement.py` 逐项比对结算单与本地账本，超误差时可写入只减仓命令。账户事实全量重放的性能限制已登记为 06 R11，须在 S5-12 连续仿真前解决；柜台联调与实际结算单核验仍待进行。

2026-09-22 增量：已推进 [S5-04 本地命令与执行序列](docs/06_开发计划.md#s5-04-local-execution)，提供 SQLite 命令去重、命令与 Journal 同事务提交、单实例锁、发送前代次复核及回调队列故障处理。账户模型通过暂存 / 提交 / 发布协议注入；CTP 网关、完整实盘账户装配和柜台联调仍待交付，S4 的研究证据缺口与阶段出口状态保持不变。

截至 2026-09-15，仓库维持 **0.2 文档基线，Day 0 八项工程基础要求已完成**。本轮补齐的 **S1-08 交易日志、S1-09 日志/脱敏/指标、S1-10 数据校验与缺口工具**均已实现并通过专项测试，按工作项单独提交。完整 S1 的真实数据与规则验收仍待资料齐备，见 [S1 逐项记录](docs/06_开发计划.md#s1-10-implementation)。

新浪接口的 241 条日线和 1023 条小时线原始响应已归档，但均缺少成交额 `turnover`，其 Bar 边界、开盘语义、许可和规则证据仍待核验。完整 S0/S1 出口、S2 交易内核验收与柜台联调尚未通过，原有规格夹具仍为 `not_executed`。研究演示脚本的正确记账单元测试不替代正式交易验收。

前期工作从 [09 前期准备与规则核验清单](docs/09_前期准备与规则核验清单.md) 开始，逐项登记核验结果与缺口；阶段安排和出口条件见 [06 开发计划](docs/06_开发计划.md)。已记录的修订与验证结果见 [10 文档变更记录](docs/10_文档变更记录.md)。

材料内容与保存约定见 [09 §6](docs/09_前期准备与规则核验清单.md#6-工程样本与研究数据集)。[数据覆盖清单](config/data_coverage.yaml) 已登记原始响应路径、哈希、覆盖范围及缺项；规范工程样本的 `artifacts` 仍待补齐。旧版 Parquet 文件保留在本地，新读取路径只接受已经提交的版本清单。

## 文档入口

| 想了解什么 | 从这里开始 |
| :--- | :--- |
| 文档如何组织、如何维护 | [00 文档索引](docs/00_文档索引.md) |
| 项目目标、范围与使用场景 | [01 原始需求](docs/01_原始需求.md)、[03 产品分析](docs/03_产品分析.md) |
| 开发前需要准备什么、如何安排阶段 | [09 前期准备与规则核验清单](docs/09_前期准备与规则核验清单.md)、[06 开发计划](docs/06_开发计划.md) |
| 功能约束与具体设计 | [02 需求拆解](docs/02_需求拆解.md)、[04 系统架构设计](docs/04_系统架构设计.md)、[05 核心业务规则设计](docs/05_核心业务规则设计.md) |
| 如何验收、运维与上线 | [07 测试与验收方案](docs/07_测试与验收方案.md)、[08 运维与上线方案](docs/08_运维与上线方案.md) |
| 已确认的修订及未决事项 | [10 文档变更记录](docs/10_文档变更记录.md)、[00 文档索引](docs/00_文档索引.md)中的待确认议题表 |

当前已确认约束以文档基线及明确的变更记录为准；未被已确认变更覆盖的部分继续遵守原规划。评审建议的采纳状态以基线记录为准，详见[文档职责与解释顺序](docs/00_文档索引.md#document-authority)。

## 文档目录

| 位置 | 内容 |
| :--- | :--- |
| [docs/](docs/) | 按 00–10 编号的当前流程文档，以及需求元信息清单 |
| [docs/sources/](docs/sources/) | 冻结的原始规划，以及维持原文相对链接可用的简短跳转页 |
| [docs/references/](docs/references/) | 按主题维护的业务参考资料 |
| [docs/reviews/](docs/reviews/) | 评审报告与复核记录 |

## 来源与评审资料

| 文件 | 用途 |
| :--- | :--- |
| [中国期货自动交易与回测工具构建规划](docs/sources/中国期货自动交易与回测工具构建规划.md) | 冻结的原始来源，保留内容与 SHA-256，供需求追溯 |
| [中国期货交易时间段与集合竞价机制详解](docs/references/中国期货交易时间段与集合竞价机制详解.md) | 交易时段与竞价规则的参考底稿，具体适用规则仍需按公告和柜台证据核验 |
| [第二轮深度架构评审复核意见](docs/reviews/第二轮深度架构评审复核意见.md) | 已整合进原规划的复核依据 |
| [文档拆分独立复审意见](docs/reviews/文档拆分独立复审意见.md) | 0.1 文档拆分的历史审查记录，处置结果见 10 文档变更记录 |

## 文档维护

[docs/requirements.json](docs/requirements.json) 是需求编号、状态、来源、阶段和验收关联的维护入口。修改清单后，在仓库根目录生成派生表：

```powershell
python scripts/check_docs.py --write
```

提交文档变更前执行只读校验：

```powershell
python scripts/check_docs.py --check
```

[校验脚本](scripts/check_docs.py) 仅使用 Python 标准库，检查需求追溯、派生表一致性、冻结来源及规格夹具等内容。业务正文按文档索引中的维护规则更新，标记为 `GENERATED` 的内容由脚本生成。

规格样例的输入、预期与证据状态见 [tests/fixtures/README.md](tests/fixtures/README.md)。上述命令执行文档与规格数据检查，交易功能验收按 07 测试与验收方案另行执行。

## 开发检查与 CI

使用 `uv` 在仓库根目录按 `.python-version` 和 `uv.lock` 准备环境，再运行统一检查：

```powershell
uv sync --locked --group dev
uv run --no-sync python scripts/install_hooks.py
uv run --no-sync python scripts/check_ci.py
```

[统一检查入口](scripts/check_ci.py) 依次运行架构测试、单元测试、smoke、只读文档校验，以及覆盖源码和测试的 Ruff 语法/未定义名称检查。各项检查都会执行，任一子检查失败时整体返回非零退出码。入口复用当前 Python 解释器，并固定在仓库根目录执行，便于本地与 CI 使用同一套检查。

[安装脚本](scripts/install_hooks.py) 为当前仓库启用 [.githooks/pre-commit](.githooks/pre-commit)。每次 `git commit` 都由 [暂存区检查脚本](scripts/pre_commit.py) 导出准备提交的文件并运行统一检查；未暂存的修改会保留，不能遮盖暂存内容中的失败。被强制暂存的凭证或运行文件若匹配忽略规则，也会阻止提交。新克隆的仓库须运行一次安装命令；已有自定义 hooks 会保留并提示人工整合。使用其他虚拟环境时，可通过 `QH_TRADER_PYTHON` 指定 Python 可执行文件。

执行 `uv run --no-sync python scripts/install_hooks.py --check` 可检查本地触发器是否已启用。本地提交检查满足 D0-6 的自动触发要求，GitHub 工作流继续提供托管检查。

[GitHub Actions 工作流](.github/workflows/ci.yml) 在推送、PR 更新或手动触发时，使用 Windows runner 和固定的 uv 0.8.4 安装锁定依赖，然后运行同一入口。工作流随提交推送到 GitHub 后生效，结果在仓库 Actions 页面查看。Python 版本沿用 `.python-version`，其 CTP 兼容性仍按 S0 清单核验。

S0 材料检查使用实际本地配置，单独运行：

```powershell
uv run --no-sync python scripts/init_env.py
uv run --no-sync python scripts/check_s0_exit.py --json
```

环境报告写入 `runs/s0/environment.json`，只执行离线依赖、文件级 SQLite 参数和 SDK 归档检查。出口检查区分已验证、允许登记的缺口、待完成和无效证据；尚无真实样本时返回非零是预期结果，不影响独立核心模块的单元测试。

## 数据接入与研究演示

默认只归档原始观察数据，包含响应内容、采集时间及 SHA-256；PowerShell 中的周期列表须加引号：

```powershell
uv run --no-sync python scripts/download_data.py --symbols SHFE.rb2410 --intervals "1d,1h" --raw-only
```

下载和研究入口输出 JSON 行日志及本次运行指标；可用 `--log-file runs/logs/download.jsonl` 指定文件。日志使用账户别名，保留策略、控制代次、订单标识、journal_seq、交易日和规则版本等关联字段；未适用的字段为空。凭证字段、原始终端载荷和网络标识会脱敏，接入有认证的数据源或柜台时还须将实际秘密值注册到日志配置。日志写入失败会明确返回失败，不能作为可观测系统正常运行的证据。

规范发布需要准备实际的合约目录、日历和来源时间元数据 JSON，格式见 [09 导入资料约定](docs/09_前期准备与规则核验清单.md#canonical-import-evidence)。例如准备好这些文件后运行：

```powershell
uv run --no-sync python scripts/download_data.py --symbols SHFE.rb2410 --intervals "1d" --publish --catalog config/contracts.actual.json --calendar config/calendar.actual.json --timings config/import_timings.actual.json
```

字段不完整、边界重叠、合约范围不符或缺少结算发布时间时拒绝发布。规范文件按内容哈希保存，`data_storage/manifests/` 保存不可变清单，`current.json` 是提交点；读者固定一个快照并校验哈希。旧 `manifest.json` 及无版本文件不会自动迁入此流程。

发布合格数据后，`scripts/run_sample_backtest.py --catalog <实际目录文件> --snapshot <快照哈希>` 可运行研究演示。它通过 `MarketDataPort` 和虚拟时钟，在信号时刻之后的开盘观察成交，跟踪成本、已实现盈亏和费用，并输出成本及时间假设；保证金约束、正式逐日结算和 S2/S3 交易状态机仍须按计划实现。此前错误账务脚本产生的收益率不能沿用。

## 独立数据校验与缺口报告

`scripts/validate_data.py --raw <原始归档路径>` 检查响应/归档哈希和原始字段；`--raw` 模式不代表规范数据已通过。规范检查须提供 `--catalog`、`--calendar`、`--timings`；默认精确模式还要求 `--limits`、`--rules-db` 和 `--profile` 对应的实际边界、最终结算及唯一规则。执行价格默认检查下一 Bar 开盘，其他执行时点通过 `--execution-spec` 显式提供。`--mode research` 会列出假设和未校验项，不标为可用于精确模式。

`scripts/gaps.py --calendar <日历路径> --start-day YYYY-MM-DD --end-day YYYY-MM-DD` 按实际 Sessions 扫描规范快照，排除休市和周末；无成交与断线只能由 `--evidence` 中带文件哈希的记录解释。它不生成缺失行情，也不把未知阶段当作休市。查看已登记的准备缺口可运行：

```powershell
uv run --no-sync python scripts/gaps.py --registry config/gaps.yaml
```

两项工具输出 JSON 报告，分别默认写入 `runs/validation/`、`runs/gaps/` 的内容哈希文件；失败范围另有隔离清单，源文件保持不变。任一必需检查失败或缺口未解决时退出码为 1。当前两份新浪归档的真实校验报告见 `config/data_coverage.yaml`，均因缺少成交额而未通过规范字段检查。

## 提交规范

`main` 为主分支。提交摘要采用 `type(scope): 摘要`，其中 scope 可省略；type 使用 `feat`、`fix`、`test`、`docs`、`refactor`、`chore` 或 `ci`。一次提交对应一个可解释的改动，复杂变更在提交说明中补充原因与验证结果。先暂存准备交付的文件，再由提交前检查验证这一份内容。
