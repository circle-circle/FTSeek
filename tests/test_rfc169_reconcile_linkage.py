"""RFC-169 回归: 慢成交 (fill=0 在途) 的链路与三态记账。

两个病灶都在 2026-10-26 开盘窗实证 (US.CAT):
1) position_sync 的 order-回执 reconcile 补录 trade_record 时**不带链路**
   (`metadata.playbook_instruction_id` / `decision_id` / `entry_session` 全缺) ⇒
   exit_monitor_flow 取 inst_id 为空 ⇒ close_flow 跳过 instruction outcome 结算 ⇒
   已平仓单 (净 +11.89) 永久停在 outcome='holding'。
2) auto_execute 把"已受理未确认"记成"跳过", 且原因只进返回 dict 不进日志 ⇒
   `自动执行完成: 0 成功` 与当场真实成交矛盾, pending 与真失败不可分。

本文件只钉这两处契约, 不测交易决策语义 (本批零交易行为变更)。
"""
import sqlite3
from pathlib import Path

import pytest

import app.core.position_sync as ps
from app.storage.trade_store import TradeStore as RealTradeStore
from tests.playbook.test_exit_compensation import compensation_case  # noqa: F401 (fixture)

_REPO = Path(__file__).resolve().parents[1]
_AUTO_EXECUTE = _REPO / "app" / "playbook" / "pipeline_parts" / "auto_execute_flow.py"


@pytest.fixture(autouse=True)
def sealed_backfill(compensation_case, monkeypatch):  # noqa: F811
    import sys
    monkeypatch.setattr(sys.modules['app.storage.trade_store'], 'TradeStore', RealTradeStore)


def _receipt(order_id: str = "RFC169-1"):
    return {'confirmed': True, 'order_id': order_id, 'dealt_qty': 8, 'price': 686.105}


def _backfill(tmp_path, monkeypatch, **linkage):
    db_path = tmp_path / "ftseek_test.db"
    from app.storage.trade_store import TradeStore
    TradeStore(str(db_path))
    monkeypatch.setattr(ps, "_get_db_path", lambda: str(db_path))
    inst = object.__new__(ps.USPositionSync)  # 该方法不触碰实例属性
    ok = ps.USPositionSync._backfill_playbook_trade_record(
        inst, symbol="US.CAT", direction="SHORT", qty=8,
        entry=686.105, sl=722.95, tp=628.73,
        trace_id="playbook-reconcile-4725-1", broker_env="sim",
        futu_order_id="RFC169-1", order_evidence=_receipt(),
        broker_position={'qty': 8, 'direction': 'SHORT'},
        **linkage,
    )
    assert ok is True, "补录应成功 (失败会吞异常仅告警)"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute(
            "SELECT decision_id, entry_session, metadata_json FROM trade_records"
        ).fetchone())
    finally:
        conn.close()


def test_reconcile_backfill_keeps_instruction_linkage(tmp_path, monkeypatch):
    """补录必须带 playbook_instruction_id / decision_id / entry_session。"""
    import json
    row = _backfill(tmp_path, monkeypatch,
                    instruction_id=4725, decision_id="DA-20261026202638-US.CAT-b1dd")
    meta = json.loads(row["metadata_json"])
    assert meta["playbook_instruction_id"] == 4725, (
        "缺该键 ⇒ 平仓时 inst_id 为空 ⇒ instruction outcome 永不结算 (CAT 实锤)")
    assert row["decision_id"] == "DA-20261026202638-US.CAT-b1dd"
    # entry_session 与正常执行链同源 (取不到落 "", fail-closed 不抛)
    assert row["entry_session"] in ("", "premarket", "regular", "afterhours")
    # 原有 SL/TP 口径不许被本次改动带偏
    assert meta["stop_loss"] == 722.95 and meta["take_profit"] == 628.73


def test_reconcile_backfill_without_linkage_is_unchanged(tmp_path, monkeypatch):
    """legacy 调用 (P3 restore, 无 instruction) 不写该键 — 不伪造链路。"""
    import json
    row = _backfill(tmp_path, monkeypatch)
    meta = json.loads(row["metadata_json"])
    assert "playbook_instruction_id" not in meta
    assert row["decision_id"] == ""


def test_auto_execute_three_state_accounting_source():
    """受理未确认必须单列, 且失败原因必须进日志 (源码契约, 防回退成静默跳过)。"""
    src = _AUTO_EXECUTE.read_text(encoding="utf-8")
    assert "submitted_pending_count" in src
    assert "submitted_unconfirmed" in src
    assert "get_instruction_by_id" in src, "必须读 execution_state 才能区分在途与真失败"
    assert '("SUBMITTED", "FILLED_UNBOOKED", "UNKNOWN")' in src
    # 汇总行必须能表达三态, 旧的二态字面量不得复活
    assert "待确认" in src
    assert "自动执行完成: {executed_count} 成功, {skipped_count} 跳过" not in src
    # 真失败必须留痕 (此前只 append 到返回 dict)
    assert "自动执行未成功" in src