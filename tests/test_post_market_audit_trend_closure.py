"""post_market_audit 趋势表闭合回归测试 (2026-09-05)。

背景: 9/4 盘后综合复盘报告头条 "近7日 66笔 净-$321.82" 与趋势表 5 行求和
+$342.45 对不上 — 用户抓出。根因: attribution_trend 窗口 = ET now - 7 日历日,
最多含 7 个交易日 (本例 6 个: 08-28/08-31/09-01/09-02/09-03/09-04), 但
build_report 旧代码 `trend['daily'][:5]` 静默截断最老一行 (08-28, 14笔
-$664.27) → 每日表加总 ≠ 近7日头条, 读者无从核对。

修复: 渲染窗口内全部交易日 + 表头标注窗口起点; 均值行/辩论结论的"近5日"
硬编码标签改为动态"最近 N 个交易日"。本测试锁住:
  1. attribution_trend 返回窗口内全部交易日 + window_start。
  2. build_report 渲染全部交易日 (无截断), 表行求和 == 费用净额窗口净和 (闭合)。
  3. avg5['n'] 与均值标签一致, 窗口>5个交易日时不再写"近5日"。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "post_market_audit", PROJECT_ROOT / "scripts" / "post_market_audit.py"
)
pma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pma)

from app.utils.timezone import now_et  # noqa: E402

_TODAY = now_et().strftime("%Y-%m-%d")


def _d(offset_days):
    return (now_et() - timedelta(days=offset_days)).strftime("%Y-%m-%d")


@pytest.fixture
def audit_db(tmp_path):
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, strategy_id TEXT, trade_date TEXT, "
        "exit_time TEXT, realized_pnl REAL, pnl_net REAL, fees_est REAL, "
        "holding_hours REAL)"
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _closed(db, symbol, trade_date, net, gross, fees):
    db.execute(
        "INSERT INTO trade_records (symbol, strategy_id, trade_date, exit_time, "
        "realized_pnl, pnl_net, fees_est, holding_hours) VALUES (?,?,?,?,?,?,?,?)",
        (symbol, "playbook_sim", trade_date, f"{trade_date}T15:30:00-04:00",
         gross, net, fees, 1.5),
    )
    db.commit()


def _six_day_window(db):
    """复刻 9/4 事故形态: 7日历日窗口内 6 个交易日, 最老一天是大亏日。"""
    # 最老交易日 (窗口内第6天) — 9/4 报告中的 08-28 (-$664.27) 角色
    for i, net in enumerate([-300.0, -200.0, -164.27]):
        _closed(db, f"US.LOSS{i}", _d(6), net, net + 4.5, 4.5)
    for offset, net in [(5, 50.0), (4, 260.45), (3, 49.85), (2, -18.76), (1, 191.17)]:
        _closed(db, f"US.DAY{offset}", _d(offset), net, net + 4.5, 4.5)


def test_trend_returns_all_window_days_and_window_start(audit_db):
    _six_day_window(audit_db)
    trend = pma.attribution_trend(audit_db, _TODAY)
    # 6 个交易日全部返回, 无截断
    assert len(trend["daily"]) == 6
    assert trend["window_start"] == _d(7)
    dates = {d["date"] for d in trend["daily"]}
    assert _d(6) in dates  # 最老交易日不被丢
    # avg5 = 除今日外窗口内全部交易日 (今日无成交行时 = 6 个) — n 动态, 标签随之
    assert trend["avg5"]["n"] == 6


def test_report_renders_all_days_and_closes_with_window_net(audit_db):
    """核心闭合: 趋势表全部行求和 == 费用净额总览的近7日净和。"""
    _six_day_window(audit_db)
    trend = pma.attribution_trend(audit_db, _TODAY)
    fee_net = pma.attribution_fee_net(audit_db, _TODAY)

    report = pma.build_report(
        _TODAY,
        strategy=[], signal=[], exit_attr=[], regime=[], trend=trend,
        execution={"direction": [], "ghost_orphan_pct": 0.0,
                   "ghost_orphan": 0, "total_trades": 9, "failures": []},
        shadow=[], risk={"capital": None, "positions": [],
                         "scalping": None, "capital_trend": [],
                         "max_consec_loss": 1},
        plan_attr={"plan_count": 0, "executed": 0, "missed": [],
                   "unplanned": [], "confidence": 0},
        lessons=[],
        debate={"consensus": [], "disputes": [], "actions": [],
                "shadow_insight": None},
        fee_net=fee_net,
        window_start=trend["window_start"],
    )

    # 1. 全部 6 个交易日都渲染 (旧代码 [:5] 会丢最老一天)
    for offset in (6, 5, 4, 3, 2, 1):
        assert _d(offset) in report, f"{_d(offset)} 被截断未渲染"

    # 2. 闭合: 表行净和 == 近7日窗口净和 (9/4 事故中两者差 -$664.27)
    table_sum = sum(d["total_pnl"] for d in trend["daily"])
    assert table_sum == pytest.approx(fee_net["window"]["net"])

    # 3. 表头标注窗口起点, 均值行不再硬编码"近5日"
    assert f"窗口自{_d(7)}" in report
    assert "**5日均值**" not in report


def test_avg_label_dynamic_when_window_has_more_than_5_days(audit_db):
    """窗口含 7 个交易日 (无周末) 时, avg5 覆盖 6 天, 标签随之动态。"""
    for offset in range(7):  # today .. today-6 全有成交
        _closed(db=audit_db, symbol=f"US.D{offset}", trade_date=_d(offset),
                net=10.0 + offset, gross=14.5 + offset, fees=4.5)
    trend = pma.attribution_trend(audit_db, _TODAY)
    assert len(trend["daily"]) == 7
    # 除今日外 6 个交易日 → avg5 实为 6 日均值
    assert trend["avg5"]["n"] == 6
    expected_pnl = sum(10.0 + offset for offset in range(1, 7)) / 6
    assert trend["avg5"]["pnl"] == pytest.approx(expected_pnl)
