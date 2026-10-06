import sys

import pandas as pd
import pytest

import scripts.rehang_live_stop as tool


class FakeTradeContext:
    def __init__(self, rows):
        self.rows = rows

    def order_list_query(self, **kwargs):
        return 0, pd.DataFrame(self.rows)


def _order_row(order_id, **overrides):
    from futu import OrderType, TrdSide

    row = {
        "order_id": order_id,
        "code": "US.PBR",
        "order_type": str(OrderType.STOP),
        "trd_side": str(TrdSide.SELL),
        "qty": 1.0,
        "order_status": "WAITING_SUBMIT",
        "remark": "pb_stop_US.PBR_real",
    }
    row.update(overrides)
    return row


def test_broker_stop_lookup_binds_only_exact_playbook_stop():
    ctx = FakeTradeContext([
        _order_row("MANUAL", remark="manual_stop"),
        _order_row("WRONG-QTY", qty=2.0),
        _order_row("CANCELLED", order_status="CANCELLED_ALL"),
        _order_row("OWN-STOP"),
    ])

    assert tool._broker_open_stop_ids(ctx, "US.PBR", "LONG", 1.0) == ["OWN-STOP"]


def test_dry_run_alias_runs_default_check(monkeypatch, capsys):
    import dotenv

    monkeypatch.setattr(sys, "argv", ["rehang_live_stop.py", "--dry-run"])
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(tool, "_find_missing_stop_positions", lambda ledger: [])

    assert tool.main() == 0
    output = capsys.readouterr().out
    assert '"apply": false' in output
    assert "无缺失 STOP 的 LIVE 持仓" in output


def test_dry_run_and_apply_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["rehang_live_stop.py", "--dry-run", "--apply"]
    )

    with pytest.raises(SystemExit) as exc_info:
        tool.main()

    assert exc_info.value.code == 2
