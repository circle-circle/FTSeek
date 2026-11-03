"""组合在险总帽 (DEB-20261031-budget-design) 契约测试。

钉三件事:
  ① 在险 = Σ|入场−有效止损|×股数, 止损缺失按 0 计 (不臆造);
  ② 超帽才拦, 未超帽不拦 (边界: 恰好等于帽 = 放行);
  ③ 任何读取异常一律 fail-open (返回不拦), 但**不得**把异常当成"帽通过"而不留痕。
"""
import sqlite3

import pytest

from app.playbook import portfolio_heat as ph


def _mkdb(tmp_path, rows):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE open_positions (symbol TEXT, size REAL, avg_entry_price REAL,"
        " current_sl REAL, initial_sl REAL, stop_loss_price REAL)"
    )
    conn.executemany("INSERT INTO open_positions VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def test_open_risk_sums_to_stop(tmp_path):
    db = _mkdb(tmp_path, [
        ("US.A", 10, 100.0, 95.0, None, None),      # 5×10 = 50
        ("US.B", 4, 200.0, None, 190.0, None),      # 10×4 = 40
        ("US.C", 2, 50.0, None, None, 45.0),        # 5×2  = 10
    ])
    total, n = ph.open_risk_usd(db, use_cache=False)
    assert (total, n) == (100.0, 3)


def test_missing_stop_counts_zero_not_guess(tmp_path):
    """缺止损的持仓不得被当成 0 风险之外的任何值 —— 合计只累加可算的。"""
    db = _mkdb(tmp_path, [
        ("US.A", 10, 100.0, 95.0, None, None),      # 50
        ("US.B", 10, 100.0, None, None, None),      # 缺止损 ⇒ 不计
    ])
    total, n = ph.open_risk_usd(db, use_cache=False)
    assert total == 50.0 and n == 2


def test_cap_boundary_is_inclusive(tmp_path):
    db = _mkdb(tmp_path, [("US.A", 10, 100.0, 95.0, None, None)])   # 在险 50
    cfg = {"max_open_risk_pct": 1.0}                                 # 权益 100 ⇒ 帽 1.0? 不, 1% ⇒ 1.0
    # 权益 5000 ⇒ 帽 50 ⇒ 50+0 ≤ 50 放行; 50+1 > 50 拦
    assert ph.heat_reason(cfg, 0.0, 5000.0, db_path=db) is None
    reason = ph.heat_reason(cfg, 1.0, 5000.0, db_path=db)
    assert reason and reason.startswith("PORTFOLIO_HEAT(")


def test_zero_cap_never_blocks(tmp_path):
    db = _mkdb(tmp_path, [("US.A", 10, 100.0, 95.0, None, None)])
    assert ph.heat_reason({"max_open_risk_pct": 0.0}, 1e6, 100.0, db_path=db) is None
    assert ph.heat_reason({}, 1e6, 100.0, db_path=db) is None


def test_missing_table_fails_open(tmp_path):
    """库/表不存在 ⇒ 在险按 0 计, 不抛异常打断下单链。

    边界说明 (别指望 fail-open 当免死金牌): 读不到库时 `cur=0`, 于是**本笔单独超帽仍会被拦** ——
    那是单笔层判据, 不是组合层。要验的是"不抛异常 + 小单不被误拦"。
    """
    db = tmp_path / "nope.db"
    total, n = ph.open_risk_usd(db, use_cache=False)
    assert (total, n) == (0.0, 0)
    # 权益 1000, 帽 2% = $20; 本笔 $10 ⇒ 10 ≤ 20 放行
    assert ph.heat_reason({"max_open_risk_pct": 2.0}, 10.0, 1000.0, db_path=db) is None
    # 本笔 $50 单独就超帽 ⇒ 仍拦 (单笔语义, 与组合层读取失败无关)
    assert ph.heat_reason({"max_open_risk_pct": 2.0}, 50.0, 1000.0, db_path=db).startswith("PORTFOLIO_HEAT")


def test_admission_default_is_off():
    """组合在险总帽的**当前裁决值**。

    2026-11-03: 原断言 `== 0.0` (关) —— 但 2026-10-31 P3 (DEB-20261031-budget-design §8,
    用户裁「P3上」) 已把 max_open_risk_pct 由 0 抬到 2.5 再到 **4.0** ⇒ 该断言自 10-31 起
    常驻红 (守卫失效)。现按当前裁决值断言, 并留引用。
    """
    from app.playbook.admission import get_admission_config
    assert get_admission_config().get("max_open_risk_pct") == 4.0