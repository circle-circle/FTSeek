r"""day_score 战绩来源标注契约 (2026-09-05, DEB-20260905 §13.16).

用户裁: 「webdashboard 是我用来手动操作实盘, 自管理的, 和 SIM 模拟账号数据不要混乱了」。

病灶: `app/plan/day_score.py` 的 `_get_recent_win_rate` / `_get_weekly_pnl` **硬编码**
`data/ftseek_us.db` (连 TRADE_ENV 都不看) ⇒ "近5笔胜率"/"本周已盈" 全来自 **795 笔 sim 成交**;
而看板 (`routes/battle_plan.py:1372`) 传入的 `available_cash` / `position_count` 来自
**REAL 账户** ⇒ 混算出的 `score=85 STRONG` / `daily_risk_budget=$12,975` /
"可适度放大仓位追趋势" 是**用实盘现金 × sim 战绩算出的实盘仓位建议**。

本批只做**如实披露** (用户裁的选项 b): 文案加来源前缀 + `DayScore.stats_source` 机器可读披露。
**数字、评分、level、风险预算一律不变** —— 改数据源属产品语义变更 (§13.16 待裁 ①/②), 未动。
且该函数**同时被 sim 主管线消费** (`us_pipeline/battle_plan_manager.py:291`) ⇒ 标注必须是
**调用侧选择加入**的, 默认不传 = sim 主管线文案逐字不变 (由本文件两条契约钉住)。
"""
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.plan import day_score as ds  # noqa: E402

VIX_LOW = 14.3          # < vix_low(15) ⇒ +10, 触发"低位"文案 (与生产实测同一档)
WEEKLY_PNL = 3000.0     # >= weekly_target_default(2000) ⇒ −10, 触发"本周已盈"文案
WIN_RATE = 1.0          # > 0.7 ⇒ +5, 触发"近5笔胜率"文案
CASH = 43250.33         # 任意实盘现金, 用于验证风险预算不受标注影响


@pytest.fixture
def frozen(monkeypatch):
    """冻结全部外部取数, 让 calculate_day_score 变成纯函数 (否则测试依赖行情/日历/账本)"""
    monkeypatch.setattr(ds, "_load_config", lambda: dict(ds._DEFAULT_CONFIG))
    monkeypatch.setattr(ds, "_get_vix", lambda: VIX_LOW)
    monkeypatch.setattr(ds, "_get_spy_trend", lambda: {"range_5d": 0.01, "trend": "UP"})
    monkeypatch.setattr(ds, "_get_economic_events", lambda: [])
    monkeypatch.setattr(ds, "_get_weekly_pnl", lambda: WEEKLY_PNL)
    monkeypatch.setattr(ds, "_get_recent_win_rate", lambda lookback_days=7, limit=5: WIN_RATE)
    return ds


def _scored(label=None):
    return ds.calculate_day_score(
        available_cash=CASH, position_count=2, stats_ledger_label=label
    )


# ─────────────────────────────────────────────────────────────────────────
# A. 默认 (sim 主管线口径): 文案必须逐字不变
# ─────────────────────────────────────────────────────────────────────────

def test_default_reasons_are_byte_identical_to_legacy(frozen):
    """不传 label ⇒ 文案与修复前逐字相同 (sim 主管线 `battle_plan_manager.py` 走这条路)"""
    reasons = _scored().reasons
    assert f"近5笔胜率 {WIN_RATE*100:.0f}%，状态良好" in reasons
    assert (f"本周已盈 ${WEEKLY_PNL:.0f} >= 目标 $2000，建议收手保护利润") in reasons
    assert f"VIX={VIX_LOW:.1f} 低位，市场稳定有利趋势" in reasons
    assert not any(r.startswith("模拟盘") for r in reasons), "默认口径不应带标注前缀"


def test_sim_pipeline_call_site_does_not_pass_label():
    """源码契约: sim 主管线的调用点**不得**传 stats_ledger_label (否则 sim 侧文案被改)"""
    src = (PROJECT_ROOT / "app/us_pipeline/battle_plan_manager.py").read_text(encoding="utf-8")
    call = src.split("day = calculate_day_score(", 1)[1].split(")", 1)[0]
    assert "stats_ledger_label" not in call, \
        "sim 主管线开始传标注了 ⇒ 其 reasons 文案会变, 与本批'sim 侧逐字不变'的承诺冲突"


def test_dashboard_call_site_passes_label():
    """源码契约: 看板 (实盘手动台) **必须**传标注, 且必须把 stats_source 透出给接口"""
    src = (PROJECT_ROOT / "app/web_dashboard/routes/battle_plan.py").read_text(encoding="utf-8")
    call = src.split("day = calculate_day_score(", 1)[1].split("\n            )", 1)[0]
    assert 'stats_ledger_label="模拟盘"' in call, "看板未标注战绩来源 ⇒ sim 战绩会被读成实盘战绩"
    assert '"stats_source": day.stats_source' in src, \
        "接口未透出机器可读的来源披露"


# ─────────────────────────────────────────────────────────────────────────
# B. 标注只动文案, 不动任何数字
# ─────────────────────────────────────────────────────────────────────────

def test_label_prefixes_only_ledger_derived_reasons(frozen):
    reasons = _scored("模拟盘").reasons
    assert f"模拟盘近5笔胜率 {WIN_RATE*100:.0f}%，状态良好" in reasons
    assert f"模拟盘本周已盈 ${WEEKLY_PNL:.0f} >= 目标 $2000，建议收手保护利润" in reasons
    # 非账本来源的文案 (VIX = 真实市场数据) **不得**被标注 —— 全都加前缀等于没加
    assert f"VIX={VIX_LOW:.1f} 低位，市场稳定有利趋势" in reasons
    assert not any(r.startswith("模拟盘") and "VIX" in r for r in reasons)


def test_label_changes_no_number_and_no_verdict(frozen):
    """标注必须是**纯披露**: score/level/预算/建议/回避窗全部逐字不变.

    弱测试陷阱: 只断 score 相同不够 —— level 决定预算档位、suggestions 才是用户真读的
    那句"可适度放大仓位追趋势", 任一处被连带改动都属越权 (本批授权只是"去掉误导")。
    """
    plain, tagged = _scored(), _scored("模拟盘")
    assert plain.score == tagged.score
    assert plain.level == tagged.level
    assert plain.daily_risk_budget == tagged.daily_risk_budget == round(CASH * 0.30, 2)
    assert plain.risk_budget_pct == tagged.risk_budget_pct
    assert plain.suggestions == tagged.suggestions
    assert plain.avoid_windows == tagged.avoid_windows
    assert len(plain.reasons) == len(tagged.reasons)


def test_weekly_pnl_near_target_branch_also_labelled(frozen, monkeypatch):
    """第二条周盈亏分支 (接近周目标) 同样要标注 — 只改一条会留下半真半假的文案"""
    monkeypatch.setattr(ds, "_get_weekly_pnl", lambda: 1700.0)   # >= 2000*0.8, < 2000
    reasons = _scored("模拟盘").reasons
    assert "模拟盘本周已盈 $1700，接近周目标" in reasons


# ─────────────────────────────────────────────────────────────────────────
# C. 来源必须机器可读, 且两个 helper 真的读同一个常量
# ─────────────────────────────────────────────────────────────────────────

def test_stats_source_always_discloses_ledger(frozen):
    for label in (None, "模拟盘"):
        src = _scored(label).stats_source
        assert "ftseek_us.db" in src and "模拟盘" in src, \
            "stats_source 必须说清战绩来自哪个账本 (消费方据此如实披露)"


def test_both_helpers_read_the_shared_constant(tmp_path, monkeypatch):
    """把常量指到临时库 ⇒ 两个 helper 的读数必须跟着变.

    这钉住"常量是唯一来源": 若有人把硬编码路径写回任一 helper, 本例立刻红。
    """
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE trade_records (trade_date TEXT, exit_time TEXT, "
                 "exit_price REAL, realized_pnl REAL)")
    today = datetime.now()
    for i in range(6):                       # 6 笔全胜、均在近 7 天与本周内
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO trade_records VALUES (?, ?, 100.0, 100.0)",
            (day, f"{day}T16:00:00-04:00"),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(ds, "STATS_LEDGER_PATH", str(db))

    assert ds._get_recent_win_rate(lookback_days=7, limit=5) == 1.0
    assert ds._get_weekly_pnl() == pytest.approx(
        100.0 * sum(1 for i in range(6)
                    if (today - timedelta(days=i)).date()
                    >= (today - timedelta(days=today.weekday())).date()),
        abs=0.01,
    )


def test_constant_points_at_sim_ledger_not_mode_routed():
    """常量必须仍是 sim 库 (本批**不改数据源**, 只披露); 若有人改成 get_main_db() 路由,
    就会连带改动 sim 主管线行为 ⇒ 必须先过 §13.16 的裁决, 故在此钉住现状。"""
    assert re.search(r"data\"?,?\s*\"?ftseek_us\.db", ds.STATS_LEDGER_PATH) or \
        ds.STATS_LEDGER_PATH.endswith("data/ftseek_us.db"), ds.STATS_LEDGER_PATH
    assert "live" not in ds.STATS_LEDGER_PATH
