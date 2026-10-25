"""scripts/weekly_trade_summary.py 回归测试 (2026-08-28, Task #15).

背景: Hermes job「FTSeek-US 周日综合复盘」Step 5 的 SQL 写在 prompt 里, 长期
`SELECT ... SUM(pnl) ... WHERE date(entry_time)` — trade_records 无 `pnl` 列,
每次运行必然 OperationalError (本文件 test_legacy_prompt_sql_would_fail 留痕)。
抽为仓内脚本后口径统一到全仓尺子: 出场日 substr(exit_time,1,10) + 净额
COALESCE(pnl_net, realized_pnl) + 已了结 exit_price>0 + 现役来源白名单。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "weekly_trade_summary", PROJECT_ROOT / "scripts" / "weekly_trade_summary.py"
)
wts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wts)


_SCHEMA = """
CREATE TABLE trade_records (
    id INTEGER PRIMARY KEY,
    symbol TEXT,
    trade_date TEXT,
    direction TEXT,
    exit_reason TEXT,
    entry_time TEXT,
    exit_time TEXT,
    exit_price REAL,
    realized_pnl REAL,
    pnl_net REAL,
    source_type TEXT,
    metadata_json TEXT,
    data_quality TEXT
)
"""


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(p))
    conn.execute(_SCHEMA)
    conn.commit()
    conn.close()
    return p


def _row(db, exit_time, *, entry_time=None, trade_date=None, exit_price=100.0,
         realized=10.0, net=None, direction="LONG", exit_reason="SL_TRAIL_LOCK",
         source="PLAYBOOK_SIM", metadata_json=None):
    if trade_date is None:
        trade_date = (entry_time or exit_time or "1970-01-01")[:10]
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, direction, exit_reason, "
        "entry_time, exit_time, exit_price, realized_pnl, pnl_net, source_type, "
        "metadata_json) "
        "VALUES ('US.X', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (trade_date, direction, exit_reason,
         entry_time, exit_time, exit_price, realized, net, source, metadata_json),
    )
    conn.commit()
    conn.close()


def _conn(db):
    return sqlite3.connect(str(db))


# ── 归集日: 出场日记账, 不是入场日 ─────────────────────────────────────
def test_overnight_exit_counts_in_exit_week(db):
    """8/26 入场 / 8/27 出场跳空亏损 — 归 8/27 窗口 (旧 date(entry_time) 会算进上期)."""
    _row(db, "2026-08-27T09:35:12-04:00", entry_time="2026-08-26T15:30:00-04:00", net=-307.13)
    cur = wts.summarize_window(_conn(db), "2026-08-27", "2026-08-27")
    assert cur["total_n"] == 1
    assert cur["total_pnl"] == pytest.approx(-307.13)
    # 入场日窗口里不该有它
    prev = wts.summarize_window(_conn(db), "2026-08-26", "2026-08-26")
    assert prev["total_n"] == 0


def test_exit_time_with_tz_suffix_still_bucketed_by_wall_clock_day(db):
    """substr 取前 10 位 = ET 墙钟日; SQLite date() 会把 -04:00 归一到 UTC 而错日."""
    _row(db, "2026-08-27T21:00:00-04:00", net=5.0)          # UTC 已是 08-28
    _row(db, "2026-08-27T09:00:00-04:00", net=5.0)
    cur = wts.summarize_window(_conn(db), "2026-08-27", "2026-08-27")
    assert cur["total_n"] == 2


# ── 已了结门: 未出场行不入胜率分母 ─────────────────────────────────────
def test_open_rows_excluded(db):
    _row(db, None, exit_price=None, realized=None, net=None)      # 在仓
    _row(db, "2026-08-27T10:00:00-04:00", exit_price=0.0, net=3.0)  # exit_price=0 脏行
    _row(db, "2026-08-27T11:00:00-04:00", net=8.0)                  # 正常了结
    cur = wts.summarize_window(_conn(db), "2026-08-27", "2026-08-27")
    assert cur["total_n"] == 1
    assert cur["total_wins"] == 1
    assert cur["total_pnl"] == pytest.approx(8.0)


# ── 净额优先, 缺失回退毛额 ────────────────────────────────────────────
def test_net_preferred_and_gross_fallback(db):
    _row(db, "2026-08-27T10:00:00-04:00", realized=7.06, net=-2.31)  # 毛盈净亏
    _row(db, "2026-08-27T11:00:00-04:00", realized=2.99, net=None)   # 旧行无 pnl_net
    cur = wts.summarize_window(_conn(db), "2026-08-27", "2026-08-27")
    assert cur["total_pnl"] == pytest.approx(0.68)
    # 净额口径下第一笔是亏单 → 胜率 1/2 而非毛额口径的 2/2
    assert cur["total_wins"] == 1
    assert cur["win_rate_pct"] == pytest.approx(50.0)


# ── 来源白名单 + 口径外不静默丢弃 ─────────────────────────────────────
def test_source_whitelist_and_outside_visible(db):
    _row(db, "2026-08-27T10:00:00-04:00", net=100.0, source="PLAYBOOK_SIM")
    _row(db, "2026-08-27T10:05:00-04:00", net=50.0, source="PLAYBOOK_LIVE")
    _row(db, "2026-08-27T10:10:00-04:00", net=9.0, source="MAIN_PIPELINE")
    _row(db, "2026-08-27T10:15:00-04:00", exit_price=101.0, realized=None, net=None,
         source="POSITION_SYNC")
    cur = wts.summarize_window(_conn(db), "2026-08-27", "2026-08-27")
    assert cur["total_n"] == 2
    assert cur["total_pnl"] == pytest.approx(150.0)
    assert cur["outside_n"] == 2          # 口径外 2 笔仍在报告里可见
    by_src = {o["source_type"]: o for o in cur["outside"]}
    assert by_src["MAIN_PIPELINE"]["total_pnl"] == pytest.approx(9.0)
    assert by_src["POSITION_SYNC"]["null_pnl"] == 1


def test_identity_summary_reuses_existing_fields(db):
    _row(db, "2026-08-27T10:00:00-04:00", net=10.0, source="PLAYBOOK_SIM")
    _row(db, "2026-08-27T11:00:00-04:00", net=-2.0, source="PLAYBOOK_SIM")
    cur = wts.summarize_window(_conn(db), "2026-08-27", "2026-08-27")
    assert cur["identity"] == [{
        "trade_env": "sim", "source_type": "PLAYBOOK_SIM", "trade_phase": "unknown",
        "n": 2, "wins": 1, "pnl": 8.0,
    }]


# ── 交叉表与头条必然自洽 ─────────────────────────────────────────────
def test_crosstab_sums_to_headline(db):
    for i, (d, r, n) in enumerate([
        ("LONG", "SL_TRAIL_LOCK", 3), ("SHORT", "TAKE_PROFIT", 2),
        ("LONG", "SL_INITIAL", 1), ("SHORT", "PRE_CLOSE_EXIT", 4),
    ]):
        for k in range(n):
            _row(db, f"2026-08-27T1{i}:00:00-04:00", direction=d, exit_reason=r,
                 net=(i + 1) * 10.0 + k)
    cur = wts.summarize_window(_conn(db), "2026-08-27", "2026-08-27")
    assert len(cur["rows"]) == 4
    assert sum(r["n"] for r in cur["rows"]) == cur["total_n"] == 10
    assert sum(r["wins"] for r in cur["rows"]) == cur["total_wins"]
    assert round(sum(r["total_pnl"] for r in cur["rows"]), 2) == cur["total_pnl"]


def test_null_direction_and_reason_bucketed_not_dropped(db):
    _row(db, "2026-08-27T10:00:00-04:00", direction=None, exit_reason=None, net=1.0)
    cur = wts.summarize_window(_conn(db), "2026-08-27", "2026-08-27")
    assert cur["rows"][0]["direction"] == "N/A"
    assert cur["rows"][0]["exit_reason"] == "N/A"
    assert cur["total_n"] == 1


# ── 窗口数学 ─────────────────────────────────────────────────────────
def test_windows_are_adjacent_and_equal_length():
    (start, end), (prev_start, prev_end) = wts.windows(7, date(2026, 8, 27))
    assert (start, end) == ("2026-08-21", "2026-08-27")
    assert (prev_start, prev_end) == ("2026-08-14", "2026-08-20")
    # 上期紧邻本期, 无重叠无空档
    assert date.fromisoformat(prev_end) + wts.timedelta(days=1) == date.fromisoformat(start)
    assert wts.windows(14, date(2026, 8, 27))[0] == ("2026-08-14", "2026-08-27")


def test_default_end_date_uses_et(monkeypatch):
    import app.utils.timezone as tz

    class _FakeDT:
        def date(self):
            return date(2026, 8, 27)

    monkeypatch.setattr(tz, "now_et", lambda: _FakeDT())
    assert wts.default_end_date() == date(2026, 8, 27)


def test_default_end_date_falls_back_when_tz_broken(monkeypatch):
    """时区库异常不得让报告脚本崩 (回落本地日, 报告仍出)."""
    import app.utils.timezone as tz

    def _boom():
        raise RuntimeError("no tz data")

    monkeypatch.setattr(tz, "now_et", _boom)
    assert isinstance(wts.default_end_date(), date)


# ── 渲染与 CLI ───────────────────────────────────────────────────────
def test_render_empty_window_says_so(db, capsys):
    assert wts.main(["--db", str(db), "--end-date", "2026-08-27"]) == 0
    out = capsys.readouterr().out
    assert "无已了结交易" in out
    assert "环比上期" in out


def test_report_declares_sim_caliber_not_live(db, capsys):
    """RFC-078 批C 钉桩: 本报告必须**自称模拟口径**, 不得让读者以为是实盘.

    病灶: 此前标题只写"本周交易汇总", 推到飞书无法分辨模拟/实盘; 而全套周报
    (本脚本 + symbol_performance_report + exit_scorecard 的 `trade_mode != 'live'`)
    **都是模拟口径** ⇒ 用户的真钱交易在任何周报里都不是一等公民。
    证伪: 删掉 render() 里的口径三行 ⇒ 本条转红。
    """
    _row(db, "2026-08-27T10:00:00-04:00", net=42.5)
    assert wts.main(["--db", str(db), "--end-date", "2026-08-27"]) == 0
    out = capsys.readouterr().out
    assert "模拟研究口径" in out and "非实盘" in out
    assert "live_weekly_review.py" in out, "必须指向实盘周报, 否则两份报告仍会互相冒充"


def test_cli_end_to_end(db, capsys):
    _row(db, "2026-08-27T10:00:00-04:00", net=42.5)
    _row(db, "2026-08-20T10:00:00-04:00", net=-7.5)
    assert wts.main(["--db", str(db), "--end-date", "2026-08-27"]) == 0
    out = capsys.readouterr().out
    assert "1笔 | 胜 1 (100.0%) | 净$42.50" in out
    assert "净$-7.50" in out                       # 上期
    assert "Δ盈亏$+50.00" in out
    assert "SL_TRAIL_LOCK" in out                  # 交叉表


def test_cli_bad_end_date_and_missing_db(db, capsys):
    assert wts.main(["--db", str(db), "--end-date", "20260827"]) == 2
    assert wts.main(["--db", str(db.parent / "nope.db"), "--end-date", "2026-08-27"]) == 1
    assert "DB not found" in capsys.readouterr().out


# ── 旧行为对照留痕: 原 prompt SQL 在真实 schema 上必然失败 ────────────
def test_legacy_prompt_sql_would_fail(db):
    """Step 5 原文 SUM(pnl)/date(entry_time) 无版本控制所以从没红过 — 只在周日报错."""
    _row(db, "2026-08-27T10:00:00-04:00", entry_time="2026-08-27T10:00:00-04:00", net=1.0)
    legacy = """
        SELECT direction, exit_reason, COUNT(*) as cnt,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(pnl), 2) as total_pnl
        FROM trade_records WHERE date(entry_time) >= ?
        GROUP BY direction, exit_reason
    """
    with pytest.raises(sqlite3.OperationalError, match="no such column: pnl"):
        _conn(db).execute(legacy, ("2026-08-20",)).fetchall()


def test_script_is_referenced_by_nothing_but_cron():
    """脚本口径唯一调用方=Hermes cron prompt; 若将来有代码 import 它, 白名单需两处同步."""
    src = (PROJECT_ROOT / "scripts" / "weekly_trade_summary.py").read_text(encoding="utf-8")
    assert "PLAYBOOK_LIVE" in src and "substr(exit_time, 1, 10)" in src


# ── A9 首跑测试批排除口径 (判据 2026-09-20 用户裁「同意」; 2026-09-25 接线) ──
# 死门实证: 判据落档后全库 238 个 trade_records 查询排除方命中 0 个 ⇒ 报表一直含污染。
# 影响: 9 月净利 −$476(含) vs +$173(不含), 月度结论符号翻转。
_A9_MARK = '{"exclusion_reason": "first_run_test_2026-09-17"}'


def test_a9_predicate_is_ssot_wired():
    """周报必须从 app.core.db_utils 取 SSOT 谓词, 不得各自 inline 一份 (漂移源)."""
    src = (PROJECT_ROOT / "scripts" / "weekly_trade_summary.py").read_text(encoding="utf-8")
    assert "from app.core.db_utils import a9_clean_where" in src
    assert "a9=a9_clean_where(conn)" in src          # 两个消费点 (_query / identity) 都走探测
    from app.core.db_utils import A9_CLEAN_PERFORMANCE_WHERE as SSOT
    assert "exclusion_reason" in SSOT and "'first_run_test" not in SSOT
    # 谓词必须是纯 SQL 片段 (可直接拼进 WHERE), 不含 Python 占位符
    assert "{" not in SSOT


def test_a9_first_run_batch_excluded_from_performance(db):
    """9/17 首跑测试批 (标 exclusion_reason) 不计入周报表现读数."""
    _row(db, "2026-09-19T10:00:00-04:00", net=100.0)                 # 正常赢单
    _row(db, "2026-09-19T11:00:00-04:00", net=-314.27, realized=136.99,
         metadata_json=_A9_MARK)                                       # CTNT 首跑污染
    cur = wts.summarize_window(_conn(db), "2026-09-15", "2026-09-21")
    assert cur["total_n"] == 1                 # 只算正常那笔
    assert cur["total_pnl"] == pytest.approx(100.0)
    assert cur["total_wins"] == 1
    assert cur["win_rate_pct"] == pytest.approx(100.0)


def test_a9_exclusion_applies_to_identity_and_outside(db):
    """identity (逐笔明细) 与 outside (口径外来源) 两条查询同样排除首跑批."""
    _row(db, "2026-09-19T10:00:00-04:00", net=5.0)
    _row(db, "2026-09-19T11:00:00-04:00", net=-200.0, source="POSITION_SYNC",
         metadata_json=_A9_MARK)
    cur = wts.summarize_window(_conn(db), "2026-09-15", "2026-09-21")
    assert len(cur["identity"]) == 1           # identity 是逐笔行列表, 污染行不进
    assert cur["outside_n"] == 0               # 污染行不进 outside 计数
    assert cur["total_pnl"] == pytest.approx(5.0)


def test_a9_missing_or_empty_metadata_not_excluded(db):
    """metadata_json 为 NULL / 空串 / 无该键 的正常行必须保留 (宁漏排除不可误杀)."""
    _row(db, "2026-09-19T10:00:00-04:00", net=10.0, metadata_json=None)
    _row(db, "2026-09-19T11:00:00-04:00", net=20.0, metadata_json="")
    _row(db, "2026-09-19T12:00:00-04:00", net=30.0, metadata_json='{"cohort": "A"}')
    cur = wts.summarize_window(_conn(db), "2026-09-15", "2026-09-21")
    assert cur["total_n"] == 3
    assert cur["total_pnl"] == pytest.approx(60.0)


def test_post_market_audit_performance_views_wired_but_fee_view_exempt():
    """盘后审计: 表现类视图必须排除; 费用标定视图 A9 明令不得剔除 (CTNT 9.15% 样本)."""
    src = (PROJECT_ROOT / "scripts" / "post_market_audit.py").read_text(encoding="utf-8")
    # SSOT = a9_clean_where(conn) 函数 (schema 无 metadata_json 列时返回空串, fail-open);
    # 禁止各脚本 inline 谓词副本 (漂移源)。
    assert "from app.core.db_utils import a9_clean_where" in src
    assert src.count("{_A9_WHERE}") >= 9      # 表现类查询已接线 (strategy/signal/exit/regime/trend/exec/risk)
    # 每个用到谓词的函数必须先做 schema 探测 (局部赋值在使用前)
    assert src.count("_A9_WHERE = a9_clean_where(db)") >= 7
    # fee_net 函数体内不得出现排除谓词 (费用标定例外)
    i = src.find("def attribution_fee_net(")
    j = src.find("\ndef ", i + 1)
    fee_body = src[i:j]
    assert "{_A9_WHERE}" not in fee_body
    assert "A9 例外视图" in fee_body           # 例外必须留痕, 防止后人误当遗漏补上


def test_a9_where_is_schema_tolerant():
    """无 metadata_json 列的载体 (旧库/异构 fixture) 不得让报表崩 — 谓词退化为空串."""
    import sqlite3 as _sq
    from app.core.db_utils import a9_clean_where, A9_CLEAN_PERFORMANCE_WHERE
    old = _sq.connect(":memory:")
    old.execute("CREATE TABLE trade_records (id INTEGER, realized_pnl REAL)")
    assert a9_clean_where(old) == ""
    new = _sq.connect(":memory:")
    new.execute("CREATE TABLE trade_records (id INTEGER, metadata_json TEXT)")
    assert a9_clean_where(new) == A9_CLEAN_PERFORMANCE_WHERE
    # 空串路径: 拼接后 SQL 仍可执行 (不产生语法错误)
    rows = old.execute(
        "SELECT COUNT(*) FROM trade_records WHERE realized_pnl IS NOT NULL " + a9_clean_where(old)
    ).fetchone()
    assert rows == (0,)


# ── 阶段双口径 + 口径互斥提示 (2026-10-25 周日复盘实测: 同页两个 W42 数字打架) ──
def test_render_has_both_stage_arms_and_caliber_guard(db, monkeypatch):
    """入场口径与出场口径必须同屏并列, 且印口径互斥提示。

    病灶: 报告同时印 trade_records 出场日 $582.11(40笔) 与指令表入口日 $234.38(27笔),
    且阶段只按入场分桶 —— 读者无法判断哪个是真、也看不见"盘前兑现"的那部分利润。
    """
    _row(db, "2026-08-26T06:12:46-04:00", entry_time="2026-08-25T16:45:00-04:00", net=236.81)
    monkeypatch.setattr(wts, "DB_PATH", str(db))
    cur = wts.summarize_window(_conn(db), "2026-08-21", "2026-08-27")
    txt = wts.render(cur, None, 7)
    assert "按入场时段" in txt
    assert "按出场时刻" in txt
    assert "口径互斥" in txt


def test_both_arms_totals_are_identical(db, monkeypatch):
    """两臂合计必须相等 (只换维度不改账) —— 防分桶实现把某类行漏掉。"""
    from app.review.stage_stats import rows_by_exit_stage, rows_from_db, stage_breakdown
    _row(db, "2026-08-26T06:12:46-04:00", entry_time="2026-08-25T16:45:00-04:00", net=236.81)
    _row(db, "2026-08-27T15:52:40-04:00", entry_time="2026-08-27T09:46:00-04:00", net=78.96)
    rows = [r for r in rows_from_db(str(db), "2026-08-21", "2026-08-27")
            if str(r.get("source_type") or "") in wts.TRADE_SOURCES]
    a = stage_breakdown(rows)
    b = stage_breakdown(rows_by_exit_stage(rows))
    assert sum(x["n"] for x in a.values()) == sum(x["n"] for x in b.values()) == 2
    assert round(sum(x["net"] for x in a.values()), 2) == \
           round(sum(x["net"] for x in b.values()), 2)
