"""影子结算链复挂回归测试 (2026-08-28, 死调度 + 静默空转双缺陷).

背景 (本轮"复查是否还有未完成的任务"揪出):
  ① 每日影子组合结算 (settle_shadow_portfolio.py) 与每周 ADF 转正监测原挂在
     agent-session 定时任务上, 该机制绑定创建会话进程 — 进程一死, 任务在注册表里照列
     "已排定" 却永不触发。创建至今: 30 条影子入场 / 12 个交易日全部可结算, 而
     data/shadow_portfolio_outcomes.jsonl 从未存在过一秒。现已迁至 Hermes no_agent
     (ops/hermes/shadow_settle_daily.sh + adf_weekly_check.sh)。
  ② 下游 scripts/ic_weekly_report.py 读不到该文件时 `return []` 静默跳过 →
     周报"影子组合"段落每周安静消失, 无人得知采集器已死 (与本周五连修的
     "日志正常但逻辑失效" 同族)。现改为缺失/陈旧必出声。
  ③ _tmp_adf_check.py 日聚合仍按 trade_date (入场日) 分桶 + pnl_net NULL 当 0 —
     本周全仓已统一为出场日 + COALESCE 净额, 它是漏网的一处; 且它现在是**自投递**
     周检器 (无 agent 代读判据), 数错即直接误导"是否实盘化"裁决。

回滚: 删本文件即可, 生产代码零依赖。
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load(mod_name, rel_path):
    spec = importlib.util.spec_from_file_location(mod_name, PROJECT_ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


iwr = _load("ic_weekly_report", "scripts/ic_weekly_report.py")
adf = _load("tmp_adf_check", "scripts/_tmp_adf_check.py")

_OUTCOMES = "shadow_portfolio_outcomes.jsonl"


# ── ① 周报影子结算段: 缺失/陈旧必出声 ──────────────────────────────────── #
def _write_outcomes(tmp, rows):
    p = tmp / "data"
    p.mkdir(parents=True, exist_ok=True)
    (p / _OUTCOMES).write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def test_missing_file_is_loud_not_silent(tmp_path, monkeypatch):
    """文件不存在 = 采集链从未跑过 → 必须给出可定位的缺口说明 (旧行为: 静默 [])."""
    monkeypatch.setattr(iwr, "BASE", tmp_path)
    rows, notes = iwr._read_shadow_portfolio_summary()
    assert rows == []
    assert notes and "不存在" in notes[0]


def test_empty_file_is_loud(tmp_path, monkeypatch):
    monkeypatch.setattr(iwr, "BASE", tmp_path)
    _write_outcomes(tmp_path, [])
    rows, notes = iwr._read_shadow_portfolio_summary()
    assert rows == [] and any("为空" in n for n in notes)


def test_stale_collector_warns_while_still_rendering(tmp_path, monkeypatch):
    """文件在但末次结算落后 >5 天 = 采集器中途死亡 (比"文件不存在"更隐蔽的一种).

    旧数据仍要出表 (聚合本身有效), 但必须同时报停摆。
    """
    monkeypatch.setattr(iwr, "BASE", tmp_path)
    monkeypatch.setattr(iwr, "TODAY", "2026-08-28")
    _write_outcomes(tmp_path, [{"blocked_by": "vwap_gate", "net": -5.0, "date": "2026-08-01"}])
    rows, notes = iwr._read_shadow_portfolio_summary()
    assert len(rows) == 1 and rows[0]["gate"] == "vwap_gate"
    assert notes and "停摆" in notes[0] and "落后当前交易日 27 天" in notes[0]


def test_fresh_data_renders_without_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(iwr, "BASE", tmp_path)
    monkeypatch.setattr(iwr, "TODAY", "2026-08-28")
    _write_outcomes(tmp_path, [
        {"blocked_by": "band", "net": 30.0, "date": "2026-08-27"},
        {"blocked_by": "band", "net": -10.0, "date": "2026-08-28"},
    ])
    rows, notes = iwr._read_shadow_portfolio_summary()
    assert notes == []
    assert rows[0]["gate"] == "band" and rows[0]["n"] == 2 and rows[0]["net"] == 20.0
    assert abs(rows[0]["win_rate"] - 50.0) < 1e-6


# ── ② ADF: 出场日归集 + 净额回退 (与全仓口径同尺) ───────────────────────── #
@pytest.fixture
def trade_db(tmp_path):
    """最小 trade_records: 隔夜仓 8/26 开 8/27 出场亏 300, 日内一笔 8/27 赚 100,
    一笔未平仓 (exit_price=0) 必须被排除, 一笔 pnl_net NULL 走毛额回退。"""
    db = tmp_path / "ftseek_us.db"
    c = sqlite3.connect(str(db))
    c.execute("""CREATE TABLE trade_records (
        source_type TEXT, trade_date TEXT, entry_time TEXT,
        exit_time TEXT, exit_price REAL, pnl_net REAL, realized_pnl REAL)""")
    c.executemany("INSERT INTO trade_records VALUES (?,?,?,?,?,?,?)", [
        ("PLAYBOOK", "2026-08-26", "2026-08-26 15:50:00", "2026-08-27 09:35:00", 10.0, -300.0, -280.0),
        ("PLAYBOOK", "2026-08-27", "2026-08-27 10:00:00", "2026-08-27 14:00:00", 10.0, 100.0, 110.0),
        ("PLAYBOOK", "2026-08-27", "2026-08-27 11:00:00", "", 0.0, None, None),      # 未平仓
        ("PLAYBOOK", "2026-08-20", "2026-08-20 10:00:00", "2026-08-21 10:00:00", 10.0, None, 55.0),
        ("MANUAL", "2026-08-27", "2026-08-27 10:00:00", "2026-08-27 15:00:00", 10.0, 999.0, 999.0),
    ])
    c.commit()
    c.close()
    return str(db)


def test_daily_buckets_by_exit_not_entry_date(trade_db, monkeypatch):
    """隔夜跳空亏损属出场日 (8/27), 不得留在入场日 (8/26) — 入场日分桶会让 8/27 少记 300."""
    monkeypatch.setattr(adf, "DB", trade_db)
    dates, vals = adf.daily_pnl("net", start="2026-08-01")
    assert "2026-08-26" not in dates, "入场日不应成桶 (旧口径缺陷)"
    assert dates == ["2026-08-21", "2026-08-27"]
    assert dict(zip(dates, vals))["2026-08-27"] == pytest.approx(-200.0)   # -300 + 100


def test_null_pnl_net_falls_back_to_gross_not_zero(trade_db, monkeypatch):
    """pnl_net NULL 的旧行按毛额回退 (55), 不得被 `or 0` 静默稀释成 0."""
    monkeypatch.setattr(adf, "DB", trade_db)
    dates, vals = adf.daily_pnl("net", start="2026-08-01")
    assert dict(zip(dates, vals))["2026-08-21"] == pytest.approx(55.0)


def test_gross_series_excludes_fallback(trade_db, monkeypatch):
    """毛额序列用 realized_pnl 原值: 8/21 = 55, 8/27 = -280 + 110 = -170."""
    monkeypatch.setattr(adf, "DB", trade_db)
    dates, vals = adf.daily_pnl("gross", start="2026-08-01")
    assert dict(zip(dates, vals))["2026-08-27"] == pytest.approx(-170.0)


def test_open_positions_and_non_playbook_excluded(trade_db, monkeypatch):
    """exit_price>0 只算已了结 (未平仓行不入分母); source_type 限 PLAYBOOK."""
    monkeypatch.setattr(adf, "DB", trade_db)
    dates, vals = adf.daily_pnl("net", start="2026-08-01")
    assert sum(vals) == pytest.approx(-145.0)          # -300 + 100 + 55, 不含 MANUAL 999
    per = adf.per_trade_net(start="2026-08-01")
    assert len(per) == 3 and 999.0 not in list(per)


def test_per_trade_series_ordered_by_exit_time(trade_db, monkeypatch):
    """逐笔序列按实现时点排序: 8/21 那笔 (晚入场早出场? 本例) 须在 8/27 -300 之前."""
    monkeypatch.setattr(adf, "DB", trade_db)
    per = list(adf.per_trade_net(start="2026-08-01"))
    assert per == [55.0, -300.0, 100.0]


# ── ③ 四判据结论: 无 agent 代读也必须给出 go/no-go ──────────────────────── #
def test_verdict_reports_not_qualified_when_sample_short(capsys):
    """OOS 不足 15 日 → 明确"未达标", 且不得出现"四项全满足"."""
    adf.graduation_verdict(
        ["2026-08-10", "2026-08-11"], [10.0, 20.0],
        ["2026-08-10", "2026-08-11"], [30.0, 40.0])
    out = capsys.readouterr().out
    assert "未达标" in out and "四项全满足" not in out
    assert "[❌] OOS ≥15 日" in out


def test_verdict_handles_empty_window(capsys):
    adf.graduation_verdict([], [], [], [])
    out = capsys.readouterr().out
    assert "无法判定" in out and "四项全满足" not in out


def test_main_end_to_end_on_fixture(capsys, trade_db, monkeypatch):
    """整链跑通 (含 report/per-trade/verdict), 且不抛异常 — 自投递脚本最怕静默崩."""
    monkeypatch.setattr(adf, "DB", trade_db)
    adf.main()
    out = capsys.readouterr().out
    assert "日净盈亏" in out and "实盘化门槛四判据" in out and "未达标" in out


def test_main_survives_empty_sample(tmp_path, capsys, monkeypatch):
    """零已了结交易时不得抛异常: report() 原直接索引 dates[0] + mean_t 除零,
    逐笔 ADF 各阶在小样本下 X'X 奇异 (LinAlgError) — 未护的异常 = 整条周检消息消失。"""
    db = tmp_path / "empty.db"
    c = sqlite3.connect(str(db))
    c.execute("""CREATE TABLE trade_records (
        source_type TEXT, trade_date TEXT, entry_time TEXT,
        exit_time TEXT, exit_price REAL, pnl_net REAL, realized_pnl REAL)""")
    c.commit()
    c.close()
    monkeypatch.setattr(adf, "DB", str(db))
    adf.main()                      # 不 raise
    out = capsys.readouterr().out
    assert "无统计量可报" in out and "无法判定" in out
