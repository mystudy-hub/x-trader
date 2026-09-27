#!/usr/bin/env python
"""[脚本工具] 人工控制入口：状态查询、暂停、只减仓、恢复、撤单、平仓与接管申请 (S5-06, FR-RISK-09, A23).

用法：
    python scripts/control.py --journal data_storage/live/trading.db --account ACC status
    python scripts/control.py --journal ... --account ACC --operator execution-service pause --reason "盘中异常"
    python scripts/control.py ... resume --reason "已核对" --cause-cleared --account-consistent
    python scripts/control.py ... cancel --client-order-id ORDER --catalog config/contract_catalog_s4_2024v1.json
    python scripts/control.py ... close SHFE.rb2410 --side SELL --offset CLOSE_TODAY --quantity 1 --price-ticks 3500
    python scripts/control.py --journal ... --account ACC takeover --applicant NEW_ID --reason "策略失联"

本入口只向命令表写入规范命令，不登录柜台、不改写交易库的其他部分；是否执行由唯一执行服务按控制者与代次
再次校验后决定 (ADR-02/03)。本地已能判定必被拒绝的命令直接退出码 2，不写入；命令表写入冲突退出码 3。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qh_trader.core.constants import Exchange, JournalConflictError, MissingRuleError, Offset, Side  # noqa: E402
from qh_trader.core.execution import CommandStatus, ExecutionCommand  # noqa: E402
from qh_trader.core.objects import InstrumentId  # noqa: E402
from qh_trader.engine.execution_service import SERVICE_STATE  # noqa: E402
from qh_trader.engine.live_account_model import VIEW_KEY  # noqa: E402
from qh_trader.infrastructure.command_queue import SQLiteCommandClient  # noqa: E402
from qh_trader.monitor import control  # noqa: E402
from scripts.live_assembly import AssemblyError, open_model_read_only  # noqa: E402

OPEN_STATUSES = (CommandStatus.PENDING, CommandStatus.DISPATCHING, CommandStatus.SENT_UNKNOWN)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="人工控制：只写规范命令，由唯一执行服务校验执行")
    parser.add_argument("--journal", required=True, help="交易库 trading.db")
    parser.add_argument("--account", required=True, help="账户标识")
    parser.add_argument("--operator", default=None, help="以当前控制者身份操作 (须与控制记录的控制者一致)")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="控制记录、服务阶段、账户摘要与未终结命令 (只读)")
    for name in ("pause", "reduce-only"):
        item = sub.add_parser(name)
        item.add_argument("--reason", required=True)
    item = sub.add_parser("resume")
    item.add_argument("--reason", required=True)
    item.add_argument("--cause-cleared", action="store_true", help="确认异常原因已消除")
    item.add_argument("--account-consistent", action="store_true", help="确认账户与柜台一致")
    item = sub.add_parser("cancel")
    item.add_argument("--client-order-id", required=True)
    item.add_argument("--catalog", default="config/contract_catalog_s4_2024v1.json", help="合约目录 (只读重建账户)")
    item = sub.add_parser("close")
    item.add_argument("instrument", help="实际合约，如 SHFE.rb2410")
    item.add_argument("--side", required=True, choices=[side.value for side in Side])
    item.add_argument("--offset", required=True, choices=sorted(offset.value for offset in control.CLOSE_OFFSETS))
    item.add_argument("--quantity", required=True, type=int)
    item.add_argument("--price-ticks", required=True, type=int, help="限价 (最小变动单位整数倍)")
    item = sub.add_parser("takeover")
    item.add_argument("--applicant", required=True, help="申请接管的新控制者标识")
    item.add_argument("--reason", required=True)
    return parser


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _instrument(raw: str) -> InstrumentId:
    exchange, separator, code = raw.partition(".")
    if not separator or not code:
        raise control.ControlRefusedError(f"instrument must be EXCHANGE.code (for example SHFE.rb2410), got {raw!r}")
    return InstrumentId(Exchange(exchange), code)


def _status(client: SQLiteCommandClient) -> dict[str, object]:
    record = client.control()
    service = client.state(SERVICE_STATE)
    view = client.state(VIEW_KEY)
    return {
        "control": None
        if record is None
        else {
            "controller_id": record.epoch.controller_id,
            "epoch": record.epoch.epoch,
            "acquired_at": record.acquired_at.isoformat(),
        },
        "phase": service.get("phase") if isinstance(service, Mapping) else None,
        "risk_state": view.get("risk_state") if isinstance(view, Mapping) else None,
        "active_orders": view.get("active_orders") if isinstance(view, Mapping) else None,
        "open_commands": [
            {"command_id": item.command.command_id, "kind": item.command.kind.value, "status": item.status.value}
            for item in client.commands(OPEN_STATUSES)
        ],
    }


def _build(args: argparse.Namespace, client: SQLiteCommandClient, at: datetime) -> ExecutionCommand:
    current = client.control()
    common = {"account_id": args.account, "current": current, "at": at}
    if args.action == "takeover":
        return control.takeover_request(applicant_id=args.applicant, reason=args.reason, **common)
    if not args.operator:
        raise control.ControlRefusedError("--operator is required for commands that change trading state")
    common["operator_id"] = args.operator
    if args.action == "pause":
        return control.pause(reason=args.reason, **common)
    if args.action == "reduce-only":
        return control.reduce_only(reason=args.reason, **common)
    if args.action == "resume":
        return control.resume(
            reason=args.reason,
            cause_cleared=args.cause_cleared,
            account_consistent=args.account_consistent,
            **common,
        )
    if args.action == "cancel":
        journal, model = open_model_read_only(_path(args.journal), args.account, _path(args.catalog))
        journal.close()
        order = model.orders.get_order(args.client_order_id)
        if order is None:
            raise control.ControlRefusedError(f"order {args.client_order_id} is not in the account journal")
        if order.is_terminal:
            raise control.ControlRefusedError(f"order {args.client_order_id} is already {order.status.value}")
        if order.cancel_pending:
            raise control.ControlRefusedError(f"a cancel is already pending for order {args.client_order_id}")
        if order.identity is None:
            raise control.ControlRefusedError(f"order {args.client_order_id} has no persisted counter identity")
        return control.cancel(identity=order.identity, **common)
    return control.close_position(
        instrument=_instrument(args.instrument),
        side=Side(args.side),
        offset=Offset(args.offset),
        quantity=args.quantity,
        limit_price_ticks=args.price_ticks,
        **common,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    journal = _path(args.journal)
    if not journal.exists():
        print(f"交易库不存在: {journal}", file=sys.stderr)
        return 2
    try:
        if args.action == "status":
            with SQLiteCommandClient(journal, account_id=args.account, read_only=True) as client:
                print(json.dumps(_status(client), ensure_ascii=False, indent=2))
            return 0
        with SQLiteCommandClient(journal, account_id=args.account) as client:
            command = _build(args, client, datetime.now(timezone.utc))
            queued = client.submit(command)
    except control.ControlRefusedError as exc:
        print(f"拒绝: {exc}", file=sys.stderr)
        return 2
    except (AssemblyError, MissingRuleError, ValueError) as exc:
        print(f"无法构造命令: {exc}", file=sys.stderr)
        return 2
    except JournalConflictError as exc:
        print(f"命令表拒绝写入: {exc}", file=sys.stderr)
        return 3
    print(f"已写入 {command.kind.value} 命令 {command.command_id} ({queued.status.value})；由执行服务校验后执行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
