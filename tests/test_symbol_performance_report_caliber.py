"""scripts/symbol_performance_report.py 口径回归测试 (2026-08-28, Task #16).

三处缺陷 (与本轮全仓"入场日记账"清扫同症, 这是最后一个报告消费者):
  ① 窗口按 trade_date(=入场日) 归集 + 用服务器 CST 时钟算 cutoff;
  ② 无 exit_price>0 → 在仓行计笔数, 稀释胜率并把不足样本的组抬过 MIN_TRADES 门槛;
  ③ trade_mode != 'live' 与来源白名单自相矛盾 (放行 PLAYBOOK_LIVE 又按 mode 剔除);
  ④ main() 无 argparse → Hermes「周日综合复盘」Step 4 传的 --weeks 1 被静默忽略,
     且原"昨天是不是交易日"门在周日 10:00 CST 语境恒 False → 整段静默 exit(0)。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "symbol_performance_report", PROJECT_ROOT / "scripts" / "symbol_performance_report.py"
)
spr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spr)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""CREATE TABLE trade_records (
        id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, direction TEXT,
        entry_time TEXT, exit_time TEXT, exit_price REAL,
        realized_pnl REAL, pnl_net REAL, source_type TEXT, trade_mode TEXT,
        holding_hours REAL)""")
    conn.commit()
    conn.close()
    return p


def _ins(db, symbol, *, exit_time, trade_date, entry_time=None, exit_price=100.0,
         realized=5.0, net=None, direction="LONG", source="PLAYBOOK_SIM",
         mode="simulate", hold=3.0):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, direction, entry_time, exit_time, "
        "exit_price, realized_pnl, pnl_net, source_type, trade_mode, holding_hours) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, trade_date, direction, entry_time, exit_time, exit_price,
         realized, net, source, mode, hold))
    conn.commit()
    conn.close()


@pytest.fixture
def fixed_today(monkeypatch):
    """把 ET 今天钉在 2026-08-27 (周四), 窗口 [--days] 可预测."""
    monkeypatch.setattr(spr, "_today_et", lambda: date(2026, 8, 27))
    return date(2026, 8, 27)


# ── ② 在仓行不得参与裁决 ────────────────────────────────────────────
def test_open_rows_do_not_lift_group_over_min_trades(db, fixed_today):
    """2 笔了结 + 1 笔在仓: 旧口径 trades=3 → 进裁决且胜率被 NULL 稀释; 新口径 n=2 → 样本不足."""
    for d in ("2026-08-25", "2026-08-26"):
        _ins(db, "US.AMD", exit_time=f"{d}T10:00:00-04:00", trade_date=d, net=10.0)
    _ins(db, "US.AMD", exit_time=None, trade_date="2026-08-27",
         entry_time="2026-08-27T09:40:00-04:00", exit_price=None, realized=None)
    results, dropped = spr.analyze_symbols(days=7, db_path=str(db))
    assert [r["symbol"] for r in results] == []
    assert [(r["symbol"], r["trades"]) for r in dropped] == [("US.AMD", 2)]
    # 旧口径留痕: 不加 exit_price>0 时该组会被当成 3 笔达标样本
    conn = sqlite3.connect(str(db))
    old_n = conn.execute(
        "SELECT COUNT(*) FROM trade_records WHERE trade_date>=? AND symbol='US.AMD'",
        ("2026-08-21",)).fetchone()[0]
    conn.close()
    assert old_n == 3


# ── ① 出场日归集 + ET 日界 ──────────────────────────────────────────
def test_window_buckets_by_exit_day(db, fixed_today):
    _ins(db, "US.IN", exit_time="2026-08-26T10:00:00-04:00", trade_date="2026-08-10")   # 窗口内出场
    _ins(db, "US.OUT", exit_time="2026-08-15T10:00:00-04:00", trade_date="2026-08-26")  # 窗口内入场/外出场
    results, dropped = spr.analyze_symbols(days=7, db_path=str(db))
    seen = {r["symbol"] for r in results} | {r["symbol"] for r in dropped}
    assert seen == {"US.IN"}
    # 旧口径 (trade_date) 结论完全相反
    conn = sqlite3.connect(str(db))
    old_seen = {r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM trade_records WHERE trade_date>=?", ("2026-08-21",))}
    conn.close()
    assert old_seen == {"US.OUT"}


# ── 净额口径 + 白名单 + ③ 不再按 trade_mode 剔除 ────────────────────
def test_net_caliber_used_for_verdict(db, fixed_today):
    for i in range(5):
        _ins(db, "US.GROSS", exit_time=f"2026-08-2{i+1}T10:00:00-04:00",
             trade_date=f"2026-08-2{i+1}", realized=3.0, net=-90.0)
    results, _ = spr.analyze_symbols(days=7, db_path=str(db))
    assert results[0]["total_pnl"] == pytest.approx(-450.0)
    assert results[0]["wr_pct"] == pytest.approx(0.0)     # 毛额口径下会是 100%
    assert results[0]["verdict"] == "DISABLE"


def test_live_mode_rows_now_included_and_other_sources_excluded(db, fixed_today):
    for k in range(3):
        _ins(db, "US.LIVE", exit_time=f"2026-08-2{k+1}T10:00:00-04:00",
             trade_date=f"2026-08-2{k+1}", net=20.0, source="PLAYBOOK_LIVE", mode="live")
    for k in range(3):
        _ins(db, "US.OLD", exit_time=f"2026-08-2{k+1}T11:00:00-04:00",
             trade_date=f"2026-08-2{k+1}", net=-99.0, source="MAIN_PIPELINE")
    results, dropped = spr.analyze_symbols(days=7, db_path=str(db))
    by_sym = {r["symbol"]: r for r in results + dropped}
    assert "US.LIVE" in by_sym and by_sym["US.LIVE"]["trades"] == 3   # 旧: 谓词剔除 → 完全不见
    assert "US.OLD" not in by_sym                                    # 冻结来源仍排除


def test_all_null_pnl_group_does_not_crash(db, fixed_today):
    for i in range(4):
        _ins(db, "US.NULLPNL", exit_time=f"2026-08-2{i+1}T10:00:00-04:00",
             trade_date=f"2026-08-2{i+1}", realized=None, net=None)
    results, dropped = spr.analyze_symbols(days=7, db_path=str(db))
    assert results[0]["total_pnl"] == 0 and results[0]["avg_pnl"] == 0


# ── ④ 参数与调度门 ──────────────────────────────────────────────────
def test_resolve_days_prefers_days_then_weeks_then_default():
    ns = lambda **kw: type("A", (), kw)()
    assert spr.resolve_days(ns(days=None, weeks=None)) == 14
    assert spr.resolve_days(ns(days=None, weeks=1)) == 7
    assert spr.resolve_days(ns(days=3, weeks=2)) == 3
    assert spr.resolve_days(ns(days=0, weeks=None)) == 1


def test_weekly_guard_no_longer_killed_by_sunday(monkeypatch):
    """周日 10:00 CST 跑周报: 旧 exit_if_not_trading_day(days_back=1) 看"昨天=周六"恒退出."""
    from app.utils.holiday_calendar import get_holiday_calendar
    monkeypatch.setattr(spr, "_today_et", lambda: date(2026, 8, 23))   # ET 周日
    cal = get_holiday_calendar()
    assert cal.is_trading_day("US", date(2026, 8, 22)) is False        # 旧门检查的就是这天
    assert spr.window_has_trading_day(7) is True                      # 窗口含上周五 → 出报告
    assert spr.window_has_trading_day(1) is False                     # 真无数据仍跳过


def test_guard_fails_open_on_calendar_error(monkeypatch):
    def _boom():
        raise RuntimeError("calendar down")
    monkeypatch.setattr(spr, "_today_et", _boom)
    assert spr.window_has_trading_day(7) is True


# ── CLI 与报告文本 ──────────────────────────────────────────────────
def test_main_cli_end_to_end(db, fixed_today, tmp_path, capsys):
    for i in range(4):
        _ins(db, "US.CLI", exit_time=f"2026-08-2{i+1}T10:00:00-04:00",
             trade_date=f"2026-08-2{i+1}", net=-200.0)
    _ins(db, "US.TINY", exit_time="2026-08-26T10:00:00-04:00", trade_date="2026-08-26", net=1.0)
    _ins(db, "US.OPEN", exit_time=None, trade_date="2026-08-26",
         entry_time="2026-08-26T10:00:00-04:00", exit_price=None, realized=None)
    out = tmp_path / "sym.json"
    rc = spr.main(["--days", "7", "--db", str(db), "--no-push"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "(7天主线交易 (2026-08-21 ~ 2026-08-27, 出场日ET))" in text
    assert "样本不足未裁决 (<3笔): 1 组 / 1笔" in text
    assert "US.OPEN" not in text             # 在仓行既不裁决也不报数
    assert "US.TINY" not in text             # 组级明细只报数, 逐条不列 (口径与旧版一致)
    assert not out.exists()                   # --no-push 不写 JSON

    rc2 = spr.main(["--weeks", "1", "--db", str(db), "--out", str(out), "--no-push"])
    assert rc2 == 0
    assert not out.exists()                   # --no-push 同时跳过 JSON 与推送
    assert "7天主线交易" in capsys.readouterr().out


def test_main_writes_json_with_caliber(db, fixed_today, tmp_path, capsys, monkeypatch):
    import app.notifier.feishu as feishu
    sent = []
    monkeypatch.setattr(feishu, "send_text", lambda content: sent.append(content) or True)
    out = tmp_path / "sym.json"
    _ins(db, "US.JSON", exit_time="2026-08-26T10:00:00-04:00", trade_date="2026-08-26", net=9.0)
    assert spr.main(["--days", "7", "--db", str(db), "--out", str(out)]) == 0
    assert len(sent) == 1, "推送桩应被调用一次 (真 send_text 不得在测试里发出去)"
    data = json.loads(out.read_text())
    assert data["period_days"] == 7
    assert data["window"] == {"start": "2026-08-21", "end": "2026-08-27",
                              "caliber": "exit_et_day"}
    assert data["below_min_trades"][0]["trades"] == 1
    capsys.readouterr()


def test_main_missing_db_returns_1(tmp_path, fixed_today):
    assert spr.main(["--db", str(tmp_path / "nope.db"), "--no-push"]) == 1
