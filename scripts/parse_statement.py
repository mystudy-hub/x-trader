#!/usr/bin/env python
"""[脚本工具] 解析期货公司结算单并与本地账本逐项比对 (S5-08, FR-LED-08, A22).

用法：
    python scripts/parse_statement.py STATEMENT --journal data_storage/live/trading.db --account ACC
    python scripts/parse_statement.py STATEMENT --ledger-json local.json   # 不读交易库，比对给定本地口径

差异超过约定误差时退出码 2；加 `--hold REASON` 会以操作员名义向命令表写入 REDUCE_ONLY 命令
(ADR-X1 人工入口)，由执行服务受理后进入只减仓状态，直到人工确认或更正事件入账后 RESUME。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.execution import CommandKind, ExecutionCommand  # noqa: E402
from qh_trader.data.statement import (  # noqa: E402
    LocalDayFigures,
    StatementFormatError,
    load_statement,
    reconcile_statement,
    render_report,
    statements_to_json,
)
from qh_trader.infrastructure.command_queue import SQLiteCommandClient  # noqa: E402
from scripts.live_assembly import AssemblyError, local_day_figures, open_model_read_only  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="解析结算单并与本地账本比对")
    parser.add_argument("statement", help="结算单文件 (.txt 监控中心文本 或 .json 规范化)")
    parser.add_argument("--journal", default=None, help="本地交易库 (只读重建账本)")
    parser.add_argument("--account", default=None, help="账户标识 (默认取结算单客户号)")
    parser.add_argument("--catalog", default="config/contract_catalog_s4_2024v1.json", help="合约目录")
    parser.add_argument("--ledger-json", default=None, help="本地口径 JSON (不读交易库时使用)")
    parser.add_argument("--tolerance", default="0.01", help="金额误差 (记账单位)")
    parser.add_argument("--margin-tolerance", default=None, help="保证金占用误差 (默认同 --tolerance)")
    parser.add_argument("--out", default=None, help="报告输出路径 (Markdown)")
    parser.add_argument("--dump-normalized", default=None, help="把解析结果写成规范化 JSON")
    parser.add_argument("--hold", default=None, metavar="REASON", help="差异阻塞时向命令表写入 REDUCE_ONLY")
    parser.add_argument("--producer-id", default="parse_statement", help="命令生产者标识")
    return parser


def figures_from_json(path: Path) -> LocalDayFigures:
    from qh_trader.core.constants import Exchange, PositionSide
    from qh_trader.core.objects import InstrumentId

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    positions = {
        (InstrumentId(Exchange(row["exchange"]), str(row["symbol"])), PositionSide(row["side"])): int(row["quantity"])
        for row in data.get("positions", ())
    }

    def optional(key: str) -> Decimal | None:
        return None if data.get(key) is None else Decimal(str(data[key]))

    def required(key: str) -> Decimal:
        # 本地口径缺项不能当作 0：那会把“缺数据”伪装成一条金额差异
        value = optional(key)
        if value is None:
            raise StatementFormatError(f"local figures lack {key}")
        return value

    return LocalDayFigures(
        balance_end=required("balance_end"),
        close_pnl=required("close_pnl"),
        mtm_pnl=required("mtm_pnl"),
        commission=required("commission"),
        margin=optional("margin"),
        positions=positions,
        cash_flow=optional("cash_flow") or Decimal(0),
    )


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        statement = load_statement(ROOT / args.statement if not Path(args.statement).is_absolute() else args.statement)
    except (StatementFormatError, OSError) as exc:
        print(f"结算单无法解析: {exc}", file=sys.stderr)
        return 3
    if args.dump_normalized:
        Path(args.dump_normalized).write_text(statements_to_json([statement]), encoding="utf-8")
    account = args.account or statement.account_id
    if account != statement.account_id:
        print(f"账户不匹配: 结算单 {statement.account_id}，参数 {account}", file=sys.stderr)
        return 3

    journal = None
    try:
        if args.ledger_json:
            local = figures_from_json(Path(args.ledger_json))
        elif args.journal:
            journal, model = open_model_read_only(ROOT / args.journal, account, ROOT / args.catalog)
            funds = model.funds_state()
            local = local_day_figures(model.ledger, statement.trading_day, margin_used=funds.margin_used)
        else:
            print("需要 --journal 或 --ledger-json 之一", file=sys.stderr)
            return 2
        tolerance = Decimal(args.tolerance)
        margin_tolerance = Decimal(args.margin_tolerance) if args.margin_tolerance else None
        result = reconcile_statement(statement, local, tolerance=tolerance, margin_tolerance=margin_tolerance)
        report = render_report(result)
        print(report)
        if args.out:
            out = Path(args.out) if Path(args.out).is_absolute() else ROOT / args.out
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(report, encoding="utf-8")
        if result.consistent:
            return 0
        if args.hold and not args.journal:
            print("--hold 需要 --journal 指向交易库；未写入 REDUCE_ONLY 命令", file=sys.stderr)
        if args.hold and args.journal:
            control = journal.load_control_record() if journal is not None else None
            if control is None:
                print("无控制记录，无法写入 REDUCE_ONLY 命令", file=sys.stderr)
            else:
                with SQLiteCommandClient(ROOT / args.journal, account_id=account) as client:
                    stamp = datetime.now(timezone.utc)
                    command = ExecutionCommand(
                        command_id=f"statement-hold-{statement.trading_day.isoformat()}-{stamp.strftime('%H%M%S')}",
                        account_id=account,
                        producer_id=args.producer_id,
                        control=control.epoch,
                        kind=CommandKind.REDUCE_ONLY,
                        submitted_at=stamp,
                        payload={"reason": f"statement mismatch {statement.trading_day}: {args.hold}"},
                    )
                    client.submit(command)
                    print(f"已写入 REDUCE_ONLY 命令 {command.command_id}", file=sys.stderr)
        return 2
    except AssemblyError as exc:
        print(f"本地账本重建失败: {exc}", file=sys.stderr)
        return 3
    except StatementFormatError as exc:
        print(f"本地口径无法使用: {exc}", file=sys.stderr)
        return 3
    finally:
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    sys.exit(main())
