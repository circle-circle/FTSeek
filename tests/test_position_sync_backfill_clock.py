"""变种4写入侧拆弹回归 (2026-09-20): P3 sync_recovery_backfill 必须写 ET 钟。

病灶: `position_sync._backfill_playbook_trade_record` 曾用
`datetime.now().astimezone()` = 服务器 CST ⇒ trade_records.entry_time 混入
+08:00 (id 1320/1511/1513/1536), 更早 naive 形态 (id 1092/1094/1095) 还伴随
trade_date 跨日错位 (违反"trade_date=入场 ET 日"不变量)。下游 (N-1 时段分桶/
pre_trade_gate 归日) 按 entry_time 前 10 位=ET 日约定, 混尺即误判。
"""
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import app.core.position_sync as ps
import pytest
from app.storage.trade_store import TradeStore as RealTradeStore
from tests.playbook.test_exit_compensation import compensation_case


@pytest.fixture(autouse=True)
def sealed_backfill(compensation_case, monkeypatch):
    import sys
    monkeypatch.setattr(sys.modules['app.storage.trade_store'], 'TradeStore', RealTradeStore)


def _receipt():
    return {'confirmed': True, 'order_id': 'CLOCK-1', 'dealt_qty': 100, 'price': 50.0}


ET = ZoneInfo("America/New_York")


def _backfill(tmp_path, monkeypatch, symbol="US.TEST"):
    db_path = tmp_path / "ftseek_test.db"
    # 生产库 trade_records 恒在; 测试先借 TradeStore._init_db 建 schema
    # (补录函数的查重 SELECT 早于 TradeStore 懒初始化)
    from app.storage.trade_store import TradeStore
    TradeStore(str(db_path))
    monkeypatch.setattr(ps, "_get_db_path", lambda: str(db_path))
    # 2026-11-03: entry_vix 取数缝钉死 — 生产走 MarketStateChecker (触网),
    #   测试环境必须 hermetic (conftest socket 边界守卫实证拦截)
    monkeypatch.setattr(ps, "_fetch_entry_vix_for_backfill", lambda: None)
    inst = object.__new__(ps.USPositionSync)  # 该方法不触碰实例属性
    ok = ps.USPositionSync._backfill_playbook_trade_record(
        inst, symbol=symbol, direction="LONG", qty=100,
        entry=50.0, sl=48.0, tp=55.0,
        trace_id=f"test-{symbol}", broker_env="sim",
        futu_order_id='CLOCK-1', order_evidence=_receipt(),
        broker_position={'qty': 100, 'direction': 'LONG'},
    )
    assert ok is True, "补录应成功 (失败会吞异常仅告警)"
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT entry_time, trade_date, entry_reason FROM trade_records"
        ).fetchone()
    finally:
        conn.close()


def test_backfill_entry_time_is_et_aware(tmp_path, monkeypatch):
    entry_time, trade_date, entry_reason = _backfill(tmp_path, monkeypatch)
    assert entry_reason == "sync_recovery_backfill"
    dt = datetime.fromisoformat(entry_time)
    assert dt.tzinfo is not None, "entry_time 必须带偏移 (禁 naive)"
    et = dt.astimezone(ET)
    # 写入即 ET 尺: 若回退服务器 CST(+08:00), utcoffset 与 ET 偏移必不等
    assert dt.utcoffset() == et.utcoffset(), (
        f"entry_time 非 ET 钟 (offset={dt.utcoffset()}): {entry_time}"
    )
    # trade_date=入场 ET 日不变量 (7 月 naive 形态曾错位 +1 天)
    assert trade_date == et.strftime("%Y-%m-%d")


def test_backfill_remains_idempotent(tmp_path, monkeypatch):
    """防重逻辑不受时钟改动影响: 二次补录不产生重复行。"""
    _backfill(tmp_path, monkeypatch, symbol="US.DUP")
    db_path = tmp_path / "ftseek_test.db"
    inst = object.__new__(ps.USPositionSync)
    again = ps.USPositionSync._backfill_playbook_trade_record(
        inst, symbol="US.DUP", direction="LONG", qty=100,
        entry=50.0, sl=48.0, tp=55.0, trace_id="dup-2", broker_env="sim",
        futu_order_id='CLOCK-1', order_evidence=_receipt(),
        broker_position={'qty': 100, 'direction': 'LONG'},
    )
    assert again is True
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM trade_records WHERE symbol='US.DUP'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_no_naive_cst_clock_left_in_backfill_source():
    """源码契约: 补录路径不得再出现服务器本地钟写 entry_time 的原写法。"""
    from pathlib import Path

    src = Path(ps.__file__).read_text(encoding="utf-8")
    assert "datetime.now().astimezone().isoformat()" not in src
    assert "now_et" in src


def test_backfill_records_entry_vix_at_adoption(tmp_path, monkeypatch):
    """钉住 2026-11-03 新语义: 补录行写入 entry_vix (收养时刻口径留痕)。

    11-02 实证: 4/14 笔 backfill 行 entry_vix=0.0 (CHKP/SPOT/SPCX/META),
    因子/IC 研究按 entry_vix 分桶被零值污染。修复后: 缝合函数有值 →
    trade_records.entry_vix 落库 + metadata.entry_vix_at_adoption 留痕;
    缝合函数回落 None → entry_vix 0.0 且不加 metadata 键 (不伪造口径)。
    """
    import json

    db_path = tmp_path / "ftseek_test.db"
    from app.storage.trade_store import TradeStore
    TradeStore(str(db_path))
    monkeypatch.setattr(ps, "_get_db_path", lambda: str(db_path))
    monkeypatch.setattr(ps, "_fetch_entry_vix_for_backfill", lambda: 15.25)
    inst = object.__new__(ps.USPositionSync)
    ok = ps.USPositionSync._backfill_playbook_trade_record(
        inst, symbol="US.VIX", direction="LONG", qty=100,
        entry=50.0, sl=48.0, tp=55.0, trace_id="vix-1", broker_env="sim",
        futu_order_id='CLOCK-1', order_evidence=_receipt(),
        broker_position={'qty': 100, 'direction': 'LONG'},
    )
    assert ok is True
    conn = sqlite3.connect(db_path)
    try:
        entry_vix, metadata_json = conn.execute(
            "SELECT entry_vix, metadata_json FROM trade_records WHERE symbol='US.VIX'"
        ).fetchone()
    finally:
        conn.close()
    assert entry_vix == 15.25
    assert json.loads(metadata_json).get("entry_vix_at_adoption") == 15.25

    # None 回落分支: 不伪造口径 (entry_vix 0.0 + 无 adoption 键)
    # (独立 futu_order_id — 同 order_id 会撞既有行查重, 返回 False)
    monkeypatch.setattr(ps, "_fetch_entry_vix_for_backfill", lambda: None)
    ok2 = ps.USPositionSync._backfill_playbook_trade_record(
        inst, symbol="US.VIXNONE", direction="LONG", qty=100,
        entry=50.0, sl=48.0, tp=55.0, trace_id="vix-2", broker_env="sim",
        futu_order_id='CLOCK-2',
        order_evidence={'confirmed': True, 'order_id': 'CLOCK-2',
                        'dealt_qty': 100, 'price': 50.0},
        broker_position={'qty': 100, 'direction': 'LONG'},
    )
    assert ok2 is True
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT entry_vix, metadata_json FROM trade_records WHERE symbol='US.VIXNONE'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 0.0
    assert "entry_vix_at_adoption" not in json.loads(row[1])
