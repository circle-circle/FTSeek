"""direction_attribution 死仪器竞态回归 (2026-08-29).

病症: 报告 04:00 CST 随盘后复盘跑, fwd_ret_t1 回填 05:30 CST 才填"昨天"
→ 墙钟 cutoff (now_et - days) 恒指向尚未回填的交易日 → 查询恒 0 行,
44 份归档报告自 7/4 起每晚 "No data found" 而 job 状态恒 ok
("日志正常但逻辑失效"族, 且是本族唯一每晚都产出死文件的成员).

修复语义: 窗口锚定"最新已回填交易日"(最近 N 个已结算交易日),
与调用时刻解耦 — 04:00 / 05:35 / 手动任何时候跑都拿到最新可得数据,
周末缺口 (周五信号要到周二 CST 晨才回填) 也不再空转.

红绿结构: fixture 固定日期构造竞态 (已回填日之后存在未回填的更新日),
对旧墙钟口径本测试必红 (cutoff 只会随时间后移, 永远越不过已回填前沿).

回滚: git revert 修复提交 + 删本文件.
"""
import sqlite3

import pytest

import scripts.daily_direction_attribution as dda


def _make_db(tmp_path, rows):
    db = tmp_path / "signal_snapshots.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE signal_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, trade_date TEXT, direction TEXT, strategy_id TEXT,
            strength REAL, confidence REAL, fwd_ret_t1 REAL, fwd_ret_t5 REAL,
            created_at_snap TEXT)"""
    )
    conn.executemany(
        "INSERT INTO signal_snapshots (symbol, trade_date, direction, strategy_id, "
        "strength, confidence, fwd_ret_t1, fwd_ret_t5, created_at_snap) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def _race_rows():
    """已回填前沿停在 08-27; 08-28 (报告日的"昨天") 与 08-31 (下周一) 均未回填."""
    return [
        # 08-26 已结算
        ("US.AAA", "2026-08-26", "LONG", "deep_analysis", 70, 0.7, 0.010, 0.012, "2026-08-26T09:00:00-04:00"),
        ("US.BBB", "2026-08-26", "SHORT", "deep_analysis", 65, 0.7, -0.005, -0.008, "2026-08-26T09:05:00-04:00"),
        # 08-27 已结算 (最新已回填日)
        ("US.CCC", "2026-08-27", "LONG", "deep_analysis", 80, 0.7, 0.020, 0.025, "2026-08-27T09:00:00-04:00"),
        ("US.DDD", "2026-08-27", "LONG", "deep_analysis", 60, 0.7, -0.008, -0.010, "2026-08-27T09:10:00-04:00"),
        ("US.EEE", "2026-08-27", "SHORT", "deep_analysis", 75, 0.7, 0.030, 0.040, "2026-08-27T09:20:00-04:00"),
        # 08-28 未回填 (竞态日: 报告 04:00 跑, 回填 05:30 才到)
        ("US.FFF", "2026-08-28", "LONG", "deep_analysis", 70, 0.7, None, None, "2026-08-28T09:00:00-04:00"),
        # 08-31 未回填 (周末缺口: 周五信号要到周二 CST 晨的回填才结算)
        ("US.GGG", "2026-08-31", "LONG", "deep_analysis", 70, 0.7, None, None, "2026-08-31T09:00:00-04:00"),
        # NEUTRAL 不入统计 (口径)
        ("US.HHH", "2026-08-27", "NEUTRAL", "deep_analysis", 50, 0.7, 0.001, 0.002, "2026-08-27T09:30:00-04:00"),
    ]


def test_report_before_backfill_returns_last_settled_day(tmp_path, monkeypatch):
    monkeypatch.setattr(dda, "DB_PATH", _make_db(tmp_path, _race_rows()))
    data = dda.analyze_direction(days=1)
    assert "error" not in data, f"竞态复发: 回填前沿之前的数据查不到 — {data}"
    assert data["covered_dates"] == ["2026-08-27"], (
        "days=1 必须锚定最新已回填交易日 08-27, 不得被未回填的 08-28/08-31 拖空")
    assert data["total"] == 3
    assert data["by_direction"]["LONG"]["n"] == 2
    assert data["by_direction"]["SHORT"]["n"] == 1
    assert data["by_direction"]["LONG"]["win_rate"] == pytest.approx(0.5)


def test_weekend_gap_falls_back_to_thursday(tmp_path, monkeypatch):
    monkeypatch.setattr(dda, "DB_PATH", _make_db(tmp_path, _race_rows()))
    data = dda.analyze_direction(days=1)
    # 周一 04:00 报告时点: 周五(08-28)信号要等周二 CST 晨回填, 前沿停在周四(08-27)
    assert data["covered_dates"] == ["2026-08-27"]


def test_days_2_covers_two_settled_days(tmp_path, monkeypatch):
    monkeypatch.setattr(dda, "DB_PATH", _make_db(tmp_path, _race_rows()))
    data = dda.analyze_direction(days=2)
    assert data["covered_dates"] == ["2026-08-26", "2026-08-27"]
    assert data["total"] == 5


def test_no_filled_data_is_honest_empty(tmp_path, monkeypatch):
    rows = [("US.FFF", "2026-08-28", "LONG", "deep_analysis", 70, 0.7, None, None,
             "2026-08-28T09:00:00-04:00")]
    monkeypatch.setattr(dda, "DB_PATH", _make_db(tmp_path, rows))
    data = dda.analyze_direction(days=1)
    assert data.get("error") == "No data found", (
        "库中确实没有任何已回填行时必须诚实报空, 不得静默把空当正常")


def test_report_renders_covered_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(dda, "DB_PATH", _make_db(tmp_path, _race_rows()))
    text = dda.format_report(dda.analyze_direction(days=1))
    assert "2026-08-27" in text, "报告必须自述覆盖哪些已结算日 (防'数据新鲜度'误读)"


def test_zero_loser_group_reports_na_not_fake_ratio(tmp_path, monkeypatch):
    """指标自净回归 (2026-09-24): 全胜组无败笔 ⇒ 盈亏比无定义 ⇒ 必须印 N/A。

    旧写法把分母回退成 0.001, 渲染出 4360x 这类伪极值; 9/23 报告据此写
    "⚠️ SHORT 盈亏比 4360x 为极端值 (单笔贡献占 ~100%)" —— 把 2 笔全胜小样本的
    退化读数讲成了异常事件。本测试对旧口径必红。
    """
    rows = [
        ("US.AAA", "2026-08-26", "SHORT", "deep_analysis", 70, 0.7, 0.010, 0.012, "2026-08-26T09:00:00-04:00"),
        ("US.BBB", "2026-08-26", "SHORT", "deep_analysis", 70, 0.7, 0.0436, 0.0500, "2026-08-26T09:05:00-04:00"),
    ]
    monkeypatch.setattr(dda, "DB_PATH", _make_db(tmp_path, rows))
    data = dda.analyze_direction(days=1)

    s = data["by_direction"]["SHORT"]
    assert s["win_rate"] == pytest.approx(1.0)
    assert s["pnl_ratio"] is None, (
        "无败笔时盈亏比必须为 None (无定义), 不得回退假分母伪造极值")
    assert s["avg_loss"] == 0.0, "无败笔时平均亏损必须为 0.0, 不得回退 0.001"

    text = dda.format_report(data)
    assert "N/A" in text, "渲染层必须印 N/A"
    assert "4360" not in text and "436" not in text, "不得出现伪造的极值盈亏比"


def test_ratio_still_computed_when_losers_exist(tmp_path, monkeypatch):
    """对照组: 有败笔时必须照常算出比率 (防'修一个坏一个')。"""
    rows = [
        ("US.AAA", "2026-08-26", "SHORT", "deep_analysis", 70, 0.7, 0.020, None, "2026-08-26T09:00:00-04:00"),
        ("US.BBB", "2026-08-26", "SHORT", "deep_analysis", 70, 0.7, -0.010, None, "2026-08-26T09:05:00-04:00"),
    ]
    monkeypatch.setattr(dda, "DB_PATH", _make_db(tmp_path, rows))
    data = dda.analyze_direction(days=1)
    s = data["by_direction"]["SHORT"]
    assert s["pnl_ratio"] == pytest.approx(2.0), "2.0% / 1.0% = 2.00x"
    assert "N/A" not in dda.format_report(data)
