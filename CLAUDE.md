# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

QH-Trader is a Windows-first, single-account tool for Chinese futures research, backtesting and staged automated trading. Backtest and live trading share one domain kernel (orders, positions, ledger, rules, risk) so their semantics cannot drift. Work is organized into stages S0–S7 with task IDs such as `S5-04`, defined in `docs/06_开发计划.md`; the current status is in the "当前进度" section of `README.md`. Docs, code comments and docstrings are written in Chinese; commit messages are in English.

## Commands

The system `python` lacks the project dependencies. Use the uv-managed venv, either `uv run --no-sync python …` or `.venv/Scripts/python.exe …`.

- Setup: `uv sync --locked --group dev`, then `uv run --no-sync python scripts/install_hooks.py`. The CTP binding (`openctp-ctp==6.7.11.0`) is an optional extra: add `--extra ctp`. Without it the CTP gateway fails explicitly; it never falls back to the simulated gateway.
- Full gate (same as GitHub CI and the pre-commit hook, takes about 2 minutes): `uv run --no-sync python scripts/check_ci.py`. It runs the architecture tests, unit tests, `scripts/smoke.py`, `scripts/check_docs.py --check` and a narrow ruff pass (`E9,F63,F7,F82,F401,F841`).
- Single test: `uv run --no-sync python -m pytest tests/unit/test_statement.py::test_trade_without_price_fails_instead_of_reading_zero -q`
- Layering only: `uv run --no-sync python -m pytest tests/architecture -q`
- Lint and format (line length 120): run `ruff check` and `ruff format` on the files you touch. Full-repo `ruff check` and `mypy` are not part of the gate and already fail (for example in `scripts/check_docs.py`, and `types-PyYAML` is not installed).
- Docs: after editing `docs/requirements.json`, run `python scripts/check_docs.py --write` to regenerate the derived tables, then `--check`.

`git commit` runs `.githooks/pre-commit`, which calls `scripts/pre_commit.py`. It exports the **staged** snapshot to a temporary directory and runs `check_ci.py` there, so every commit must pass on its own. Give commits a long timeout.

## Architecture

`tests/architecture/test_boundaries.py` enforces the layering with AST analysis. It follows transitive import chains, including re-exports through package `__init__` files:

| Layer | May import |
| :--- | :--- |
| `core` | `core` |
| `domain`, `strategy` | `core` and itself |
| `data`, `gateway`, `infrastructure`, `monitor` (adapters) | `core` and itself |
| `engine` | `core`, `domain`, `engine` |
| `research`, `analysis` | not in the matrix: anything except `scripts` |
| `scripts` | anything |

No module may import `scripts`, and the whole import graph must be acyclic. As a result, `engine` never imports concrete adapters. They are built and injected as the protocols defined in `qh_trader/core/ports.py`: by `research/backtest_assembly.py` for backtests and by `scripts/` for everything else. Code that needs both a domain object and an adapter lives in `scripts/`; for example, `scripts/live_assembly.py` extracts ledger figures for statement reconciliation. Shared value types live in `core/objects.py`, `core/event.py` and `core/execution.py`.

Two runtime paths use the same kernel in `qh_trader/domain/`: order, position, ledger, risk, rules, rollover, recovery and lifecycle management.

- **Backtest and research.** `research/backtest_assembly.py` wires the contract catalog, calendar, data snapshot, `gateway/simulated_gateway.py` and `engine/backtest_engine.py` (built on `engine/base_engine.py`), then writes a run manifest. The backtest uses an in-memory journal (ADR-07). `research/vector_backtest.py` is the fast parameter-scan channel; it shares the event engine's data mapping, execution timing and cost assumptions (ADR-10).
- **Live trading (S5, in progress).**
  - `engine/execution_service.py` is the single trading exit (ADR-02/03).
  - Strategies, the watchdog and operator scripts only INSERT commands into the SQLite command table (`infrastructure/command_queue.py`). That table lives in the same `trading.db` as the journal (`infrastructure/journal.py`, WAL + FULL).
  - The service processes each command on one thread: stage on a private copy, atomically commit the command status, journal events and projections, publish the new state, then send.
  - Every command carries a `ControlEpoch`. Takeover runs isolate → bump epoch → reconcile → enable. The epoch is checked again at the gateway call (`gateway/epoch_fence.py`).
  - Broker callbacks only enqueue: the trade queue is unbounded, the market-data queue is bounded.
  - `engine/live_account_model.py` keeps ordered account facts in journal state and replays them into the kernel. Every staging replays the full fact list; see risk R11 in 06.
  - `scripts/run_execution_service.py` and `scripts/live_assembly.py` assemble the service. `--mode paper` uses the simulated gateway and `gateway/paper_query.py`. `--mode live` wires the CTP gateway and runs connect → isolate → bump epoch → reconcile → enable.
  - CTP adapters:
    - `gateway/ctp_gateway.py` handles the handshake, re-checks the epoch before `ReqOrderInsert`/`ReqOrderAction`, and persists the `(FrontID, SessionID, OrderRef)` triple.
    - `gateway/feedback_normalizer.py` turns order and trade reports into `OrderUpdate`/`Trade`.
    - `gateway/ctp_query.py` runs the account and contract queries.
    - `gateway/ctp_market.py` turns MdApi snapshots into `Tick`.
  - Counter capabilities that have not been verified are refused rather than guessed: today/yesterday offset mapping and market orders. A cancel must carry the original session triple.
  - `scripts/ctp_setup.py` builds `CtpSettings` from `config/broker_profiles/*.yaml` (SimNow; openctp TTS is registered as a candidate). `scripts/ctp_probe.py` produces redacted login, query, order, market-data and catalog-diff evidence under `runs/s0/`.
  - Secrets come only from the environment (`QH_CTP_PASSWORD`, `QH_CTP_AUTH_CODE`). They are never written to config, logs or evidence files.

These modules are one-line placeholders for work that has not started: `gateway/terminal_info.py`, `engine/live_engine.py`, `engine/shadow_engine.py`, `monitor/watchdog.py`, `monitor/control.py`, `data/recorder.py`, `analysis/execution_quality.py`.

Design references:
- `docs/04_系统架构设计.md`: §3 layering, §4 ports, §6 execution sequence, §12 ADRs
- `docs/05_核心业务规则设计.md`: business rules
- `docs/reviews/S5_进程模型_IPC_回调汇入_时钟同步设计评审.md`: the ADR-X1/X2/X3 constraints on process model, IPC, callback ingress and clock sync

## Conventions

- Money and P&L use `Decimal`. Order prices are integer ticks (`limit_price_ticks`) (ADR-09).
- Fail explicitly instead of guessing:
  - A missing rule, price or contract economics raises (`MissingRuleError`, `StatementFormatError`, …).
  - Missing data is never read as 0.
  - Unknown send results stay `SENT_UNKNOWN` and keep their reservations; they are never retried automatically.
- A module docstring starts with `[<Layer> 层]` and cites the task and requirement or acceptance IDs, for example `(S5-08, FR-LED-08, A22)`.
- Trading days follow the exchange calendar: a night session belongs to the next trading day. Never derive a trading day from a UTC or local date.
- `config/settings.yaml`, `runs/` and `data_storage/` are git-ignored runtime locations. `config/settings.yaml.example` is only a template; the execution service refuses to start with it.

## Documentation system (validated in CI)

- `docs/00–10` form the current baseline. `docs/requirements.json` is the single source of requirement IDs, stages and acceptance links. Content between `<!-- BEGIN GENERATED: … -->` markers is generated; do not edit it by hand.
- `docs/sources/` is frozen: it is SHA-256 checked and marked `-text` in `.gitattributes`. Do not modify it.
- When sources disagree, `docs/10_文档变更记录.md` (confirmed changes) wins, then the baseline docs, then the original plan in `docs/sources/`. Items marked 待确认 or 【评审补充】 cannot change scope or stage exits.
- `check_docs.py` treats any table row whose first cell exactly matches `A\d{2}` or `A\d{2}-\d{2}` as a formal acceptance case and requires exactly 36 of them in `docs/07_测试与验收方案.md`. In evidence tables, label rows like `A22 结算单比对` or `A23、F15`, never a bare `A22`.
- Finishing a task adds four records:
  - a dated implementation record with an anchor in 06, under the stage table
  - a local-evidence table in 07 mapped to existing acceptance IDs (no new IDs; the frozen pass conditions stay unchanged)
  - a dated entry in 10
  - an update to the README "当前进度" paragraph
- State the type of evidence honestly. Local, paper and replay evidence never replaces broker integration testing (柜台联调) or real-time evidence, and a stage exit is not claimed without it.
- `tests/fixtures/*.json` are specifications with `execution_status: not_executed` and independently derived expectations. Do not mark them passed or fill in expectations from implementation output.

## Commits

The branch is `main`. The summary follows `type(scope): summary`, with type one of feat/fix/test/docs/refactor/chore/ci and a scope such as `s5`. Make one explainable change per commit, and use the body for reasons and verification results.
