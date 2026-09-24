# QH-Trader

面向中国期货的策略研究、回测与自动交易工具。项目规划通过共享交易内核，让回测与实盘复用订单状态机、持仓、资金账本、交易规则和事前风控。

首版目标是在 Windows 上交付单账户、普通期货、日线 / 小时级 Bar 回测与 CLI 报告，并提供向量化快速研究通道；后续按阶段扩展多品种、模拟盘、影子模式和实盘能力。

## 当前进度

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
