"""PlanEffectiveness 分母口径契约 (2026-10-20 记录卫生).

病灶: `_load_battle_plan` 只取 `direction != 'SKIP'`，而 2 分钟门放弃的指令
(`triggered=1, direction='SKIP', skip_reason='entry_confirm_reversed'`) 被排除在分母外
⇒ 10/19 实测 7 成交 / 8 可交易 被报告成 traded_rate=100%。
本测试钉住: 放弃目标必须进入 planned、traded=False、并在报告中显式标注口径。
"""
from __future__ import annotations

import sqlite3

from app.analysis.plan_effectiveness import PlanEffectivenessTracker


def _build_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE playbook_instructions ("
        " id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT,"
        " entry_price REAL, stop_loss REAL, take_profit REAL, confidence INTEGER,"
        " triggered INTEGER, executed INTEGER, skip_reason TEXT)"
    )
    conn.execute(
        "CREATE TABLE trade_records ("
        " trade_date TEXT, symbol TEXT, direction TEXT, entry_price REAL,"
        " realized_pnl REAL, pnl REAL)"
    )
    rows = [
        ("US.UNH", "BUY", 404.0),
        ("US.BWXT", "BUY", 136.0),
        ("US.META", "BUY", 699.0),
        ("US.CRM", "BUY", 238.0),
        ("US.COIN", "SELL_SHORT", 174.0),
        ("US.RIVN", "SELL_SHORT", 12.35),
        ("US.ARM", "SELL_SHORT", 232.0),
    ]
    for i, (sym, direction, px) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO playbook_instructions (id,date,symbol,direction,entry_price,"
            " stop_loss,take_profit,confidence,triggered,executed,skip_reason)"
            " VALUES (?, '2026-10-19', ?, ?, ?, 0, 0, 50, 1, 1, '')",
            (i, sym, direction, px),
        )
        conn.execute(
            "INSERT INTO trade_records (trade_date,symbol,direction,entry_price,"
            " realized_pnl,pnl) VALUES ('2026-10-19', ?, ?, ?, 1.0, 1.0)",
            (sym, direction, px),
        )
    # 2 分钟门放弃: 方向已改 SKIP, 但 triggered=1
    conn.execute(
        "INSERT INTO playbook_instructions (id,date,symbol,direction,entry_price,"
        " stop_loss,take_profit,confidence,triggered,executed,skip_reason)"
        " VALUES (8, '2026-10-19', 'US.AVGO', 'SKIP', 336.19, 0, 0, 70, 1, 0,"
        " 'entry_confirm_reversed')"
    )
    conn.commit()
    conn.close()


def test_abandoned_entry_confirm_enters_denominator(tmp_path):
    db = tmp_path / "trades.db"
    _build_db(db)

    report = PlanEffectivenessTracker(
        db_path=str(db), output_dir=str(tmp_path / "out")
    ).evaluate_daily_plan("2026-10-19")

    assert report["planned_count"] == 8, "放弃目标必须进入 planned 分母"
    assert report["traded_planned_count"] == 7
    assert report["abandoned_count"] == 1
    assert report["abandoned_symbols"] == ["US.AVGO"]
    assert report["traded_rate"] == 0.875, "执行率必须是 7/8, 不能虚高为 100%"
    assert report["execution_rate"] == report["traded_rate"]
    assert "entry_confirm_reversed" in report["execution_rate_basis"]

    abandoned = [
        t for t in report["targets"]
        if t.get("status") == "abandoned_entry_confirm"
    ]
    assert len(abandoned) == 1
    assert abandoned[0]["symbol"] == "US.AVGO"
    assert abandoned[0]["traded"] is False
    assert abandoned[0]["skip_reason"] == "entry_confirm_reversed"


def test_no_abandoned_targets_keeps_full_rate(tmp_path):
    db = tmp_path / "trades.db"
    _build_db(db)
    # 删掉放弃行, 7 个可交易目标全部执行
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM playbook_instructions WHERE symbol='US.AVGO'")
    conn.commit()
    conn.close()

    report = PlanEffectivenessTracker(
        db_path=str(db), output_dir=str(tmp_path / "out")
    ).evaluate_daily_plan("2026-10-19")

    assert report["planned_count"] == 7
    assert report["abandoned_count"] == 0
    assert report["traded_rate"] == 1.0
