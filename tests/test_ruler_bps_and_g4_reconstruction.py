"""修尺子批钉子 (2026-09-06, ADR-038 续; 用户裁「需要」→ 三项尺子改动)。

病灶 (本轮实测三处, 全部是"数字看着正常但仪器在说谎"):
  ① **美元口径跨 sizing 断点不可比**: 2026-08-06 三提交 (8b81f4dc 单笔上限 4%→6% /
     8e281cd3 BUY 仓位因子 0.5→1.0 / e889b6b5 删重复 max_position_pct 键) 使名义额中位
     $3,307→$5,737 (8/7 一步跳), 而每笔佣金几乎不变 ($4.14→$4.21) → "净 $/笔从 −$2.03
     变 +$8.79" 里混着"注下大了 2.5 倍"。治法: bps of notional 与美元口径**并列**输出。
  ② **G4 回撤腿永久免检**: 旧码读 capital_state 逐行算峰谷, 但该表是单行状态表
     (UNIQUE INDEX idx_capital_state_trade_env, app/risk/capital_safety.py:741 + 建表即
     DELETE 非最大 id 行 :733) → 窗口内恒 1 行 → max_drawdown_pct 恒 0.0 且 ok=True。
     治法: 从 trade_records 出场日记账重建权益曲线, 锚定 capital_state.current_equity
     (与 app/risk/absolute_circuit_breaker.py::_restore_from_db 同源, 不造平行层)。
  ③ **同口径时序缺失**: trigger_gate_history.jsonl 10 行的 since 随 --days 30 滚动
     (6/19,7/1,7/8,7/15,7/22,7/29,8/5,8/7…), 于是"缺口 4.955→0.703 收窄"其实是窗口起点
     在动; render 里写的"持续 ≥2 周"判据零留痕。治法: 冻结 ANCHOR_SINCE 锚点块 +
     generated_at (带时区) + 连续观测计数。

本文件钉住的语义:
  Ⓐ bps 算式的**量纲契约** (毛 − 费 = 净 恒等; 分母是名义额, 不是 quantity);
  Ⓑ SQL 侧 (okr_status) 与 Python 侧 (exit_scorecard) 两把尺子**必须同值** —
     钉死本轮实犯的 `/` `*` 左结合优先级事故 (裸写 pnl/entry_price*quantity 被解析成
     (pnl/entry_price)*quantity, bps 炸到 +1.5e6);
  Ⓒ 取样面一致 (未结算净额的行不得计入 n, 否则 n 与均值的真实样本数不符);
  Ⓓ **不可测 ≠ 0**: 无样本/无锚点/日聚类<3/零方差 一律 None, 不得静默变成 PASS;
  Ⓔ 按日聚类是**等权日** (成交多的日子不得主导统计量, 宪章第9律);
  Ⓕ 回撤单位是**正百分点**且终点与权威权益对齐 (capital_state 入库是带符号分数,
     量纲不同, 只作交叉校验不得混用);
  Ⓖ 锚点是判据的一部分: 滚动窗口的 history 行不得混进"持续≥2周"计数。

回滚: 删本文件 + revert 对应提交即可, 零生产代码依赖 (三个脚本均为只读报表)。
"""
import importlib.util
import math
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


esc = _load("exit_scorecard_under_test", "scripts/exit_scorecard.py")
tgr = _load("trigger_gate_report_under_test", "scripts/trigger_gate_report.py")

_OKR_SRC = (PROJECT_ROOT / "scripts" / "okr_status.py").read_text(encoding="utf-8")

# 合成样本: quantity 故意 ≠ 1 —— 优先级事故在 quantity=1 时**完全隐形**, 只有不等 1 才暴露。
#   (trade_date, entry_price, quantity, realized_pnl, fees_est, pnl_net)
_ROWS = [
    ("2026-08-10", 100.0, 10, 10.0, 1.0, 9.0),      # notional 1000 → 毛 +100bps 费 10bps 净 +90bps
    ("2026-08-11", 50.0, 100, -25.0, 5.0, -30.0),   # notional 5000 → 毛 −50bps  费 10bps 净 −60bps
    ("2026-08-12", 200.0, 0, 999.0, 1.0, 998.0),    # notional 0    → 必须被 guard 剔除
    ("2026-08-13", 100.0, 10, None, 1.0, None),     # 未结算        → 必须被 guard 剔除
]


def _rows_as_dicts():
    out = []
    for d, px, qty, gross, fee, net in _ROWS:
        out.append({"date": d, "gross": gross, "net": net, "fee": fee or 0.0,
                    "notional": (px * qty) if (px is not None and qty is not None) else None})
    return out


def _okr_bps_aggregates():
    """从 okr_status.py 源码里抽出真实的 bps 聚合表达式并解析 f-string 占位符。

    不复制一份 SQL 到测试里 —— 复制就等于测不到源头的量纲事故。
    """
    net = re.search(r'^_NET = "(.+?)"$', _OKR_SRC, re.M).group(1)
    notional = re.search(r'^_NOTIONAL = "(.+?)"$', _OKR_SRC, re.M).group(1)
    guard = (re.search(r'^_BPS_GUARD = f"(.+?)"$', _OKR_SRC, re.M).group(1)
             .replace("{_NOTIONAL}", notional))
    aggs = {}
    for key in ("cnt_bps", "gross_bps", "fee_bps", "net_bps", "avg_notional"):
        m = re.search(rf"^\s*(.+?)\s+as {key},?$", _OKR_SRC, re.M)
        assert m, f"okr_status.py 里找不到 {key} 聚合 — 尺子被删了?"
        aggs[key] = (m.group(1).replace("{_BPS_GUARD}", guard)
                     .replace("{_NOTIONAL}", notional).replace("{_NET}", net))
    return aggs, notional


def _mk_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE trade_records (
        trade_date TEXT, entry_price REAL, quantity REAL,
        realized_pnl REAL, fees_est REAL, pnl_net REAL)""")
    con.executemany("INSERT INTO trade_records VALUES (?,?,?,?,?,?)", _ROWS)
    return con


# ══════════════════════ Ⓐ/Ⓒ bps 量纲契约 ══════════════════════

def test_size_normalized_identity_gross_minus_fee_equals_net():
    sn = esc.size_normalized(_rows_as_dicts(), 15.0)
    assert sn["n"] == 2, "notional=0 与未结算行必须被剔除, 只剩两笔可算"
    assert sn["gross_bps"] == 25.0     # mean(+100, −50)
    assert sn["fee_bps"] == 10.0       # mean(10, 10)
    assert sn["net_bps"] == 15.0       # mean(+90, −60)
    assert round(sn["gross_bps"] - sn["fee_bps"], 6) == sn["net_bps"], "毛 − 费 = 净 必须恒等"
    assert sn["unit"].startswith("bps of notional")


def test_size_normalized_denominator_is_notional_not_quantity():
    """量纲钉: 分母必须是名义额。若误用 quantity 或漏括号, 本例立刻炸 (qty=10/100)。"""
    sn = esc.size_normalized(_rows_as_dicts(), 15.0)
    assert abs(sn["gross_bps"]) < 1000, f"bps 数量级异常 ({sn['gross_bps']}) — 分母量纲错了"
    single = esc.size_normalized([{"date": "d", "gross": 10.0, "net": 9.0, "fee": 1.0,
                                   "notional": 1000.0}], 15.0)
    assert single["gross_bps"] == 100.0, "1000 名义额上赚 10 元 = 100bps (=1%)"


def test_size_normalized_unmeasurable_is_none_not_zero():
    """Ⓓ 不可测 ≠ 0: 空样本必须 n=0 + note, 不得输出 0.0bps 让人读成"打平"。"""
    empty = esc.size_normalized([], 15.0)
    assert empty["n"] == 0 and "gross_bps" not in empty and empty.get("note")
    bad = esc.size_normalized([{"date": "d", "gross": 5.0, "net": 5.0, "fee": 0.0,
                                "notional": 0}], 15.0)
    assert bad["n"] == 0
    unsettled = esc.size_normalized([{"date": "d", "gross": None, "net": None, "fee": 1.0,
                                      "notional": 1000.0}], 15.0)
    assert unsettled["n"] == 0, "净额未结算的行不得计入 n (取样面与 SQL guard 必须同面)"


def test_size_normalized_slip_haircut_is_roundtrip_and_flag_follows():
    sn = esc.size_normalized(_rows_as_dicts(), 15.0)
    assert sn["slip_haircut_roundtrip_bps"] == 30.0, "来回 = 2×单边 haircut (RFC-062 门1口径)"
    assert sn["net_bps_after_slip_haircut"] == -15.0
    assert sn["edge_covers_roundtrip_cost"] is False
    rich = esc.size_normalized([{"date": "d", "gross": 100.0, "net": 90.0, "fee": 10.0,
                                 "notional": 1000.0}], 15.0)
    assert rich["net_bps"] == 900.0, "1000 名义额上净赚 90 元 = 900bps (=9%)"
    assert rich["net_bps_after_slip_haircut"] == 870.0
    assert rich["edge_covers_roundtrip_cost"] is True


# ══════════════════════ Ⓑ 两把尺子必须同值 ══════════════════════

def test_okr_sql_bps_equals_python_bps():
    """钉死 `/` `*` 左结合事故: SQL 侧与 Python 侧同一批行必须给出同一个数。"""
    aggs, notional = _okr_bps_aggregates()
    assert notional.startswith("(") and notional.endswith(")"), (
        "_NOTIONAL 必须带括号 — 裸写 entry_price * quantity 会让 `pnl / entry_price * quantity` "
        "被解析成 (pnl/entry_price)*quantity, bps 放大 quantity 倍 (实测炸到 +1.5e6)")
    con = _mk_db()
    row = con.execute("SELECT " + ", ".join(f"{aggs[k]} AS {k}" for k in aggs)
                      + " FROM trade_records").fetchone()
    sn = esc.size_normalized(_rows_as_dicts(), 15.0)
    assert row["cnt_bps"] == sn["n"] == 2
    assert round(row["gross_bps"], 6) == sn["gross_bps"]
    assert round(row["fee_bps"], 6) == sn["fee_bps"]
    assert round(row["net_bps"], 6) == sn["net_bps"]


def test_okr_guard_excludes_unsettled_rows_from_every_mean():
    """Ⓒ 取样面一致: 未结算行既不进 cnt_bps 也不进任何一个均值 (否则费被稀释, 恒等式破)。"""
    aggs, _ = _okr_bps_aggregates()
    con = _mk_db()
    row = con.execute("SELECT " + ", ".join(f"{aggs[k]} AS {k}" for k in aggs)
                      + " FROM trade_records").fetchone()
    assert row["cnt_bps"] == 2
    assert round(row["gross_bps"] - row["fee_bps"], 6) == round(row["net_bps"], 6)
    assert row["avg_notional"] == 3000.0, "avg_notional 同样只算可算行 (1000 与 5000 的均值)"


# ══════════════════════ Ⓔ 按日聚类是等权日 ══════════════════════

def test_day_clustered_t_insufficient_or_degenerate_is_none():
    assert esc._day_clustered_t([1.0, 2.0]) is None, "日聚类 <3 → 不可判, 不得当 0"
    assert esc._day_clustered_t([5.0, 5.0, 5.0]) is None, "零方差 → t 无定义"
    assert esc._day_clustered_t([]) is None


def test_day_clustered_t_weights_days_equally_not_trades():
    """宪章第9律: 一天 100 笔不得压过另一天 1 笔 (先按日取均值, 再对日均值做 t)。"""
    loud = [10.0] * 100 + [-10.0]        # 日1: 均值 +10; 日2: −10; 日3: 0
    daily = [10.0, -10.0, 0.0]
    got = esc._day_clustered_t(daily)
    assert got["day_mean"] == 0.0 and got["n_days"] == 3
    assert abs(got["t"]) < 2.0 and got["distinguishable"] is False
    assert len(loud) == 101              # 逐笔塞进去会得出完全不同的 t — 故必须传日均值


def test_day_clustered_t_known_value_and_significance_gate():
    vals = [10.0, 12.0, 14.0, 16.0]      # mean 13, sd ≈ 2.582 → t = 13/(2.582/2) ≈ 10.07
    got = esc._day_clustered_t(vals)
    assert got["n_days"] == 4
    assert abs(got["t"] - 13.0 / (math.sqrt(sum((v - 13) ** 2 for v in vals) / 3) / 2)) < 0.01
    assert got["distinguishable"] is True
    assert got["mde_80pct"] == round(2.802 * math.sqrt(sum((v - 13) ** 2 for v in vals) / 3) / 2, 2)


# ══════════════════════ Ⓕ G4 回撤重建 ══════════════════════

def test_reconstruct_drawdown_known_curve():
    """锚点(窗口末权益) 1000, 窗口内 −100 再 +50 → 倒推起点 1050, 谷底 950
    → 回撤 (1050−950)/1050 = 9.52% (峰是**窗口内**峰, 不取开户至今)。"""
    events = [("t1", -100.0), ("t2", 50.0)]
    got = esc.reconstruct_max_drawdown(events, 1000.0)
    assert got["equity_end"] == 1000.0, "曲线终点必须与权威权益严格对齐"
    assert got["equity_start"] == 1050.0, "起点 = 锚点 − 窗口净盈亏合计 (1000 − (−50))"
    assert got["n_events"] == 2 and got["n_points"] == 3
    assert got["max_drawdown_pct"] == round(100.0 / 1050.0 * 100.0, 2) == 9.52
    assert got["max_drawdown_pct"] > 0, "单位是**正百分点** (5.0 = 5%), 不是带符号分数"


def test_reconstruct_drawdown_start_is_anchor_minus_window_net():
    events = [("t1", -100.0), ("t2", 50.0)]
    got = esc.reconstruct_max_drawdown(events, 1000.0)
    assert got["equity_start"] == 1000.0 - (-50.0) == 1050.0
    assert got["equity_end"] - got["equity_start"] == -50.0, "曲线净位移 = 窗口净盈亏"


def test_reconstruct_drawdown_peak_is_window_local_and_monotone_rise_is_zero():
    rising = [("t1", 10.0), ("t2", 10.0), ("t3", 10.0)]
    got = esc.reconstruct_max_drawdown(rising, 1030.0)
    assert got["max_drawdown_pct"] == 0.0, "一路上涨 → 零回撤 (不是 None, 不是负数)"
    dd = esc.reconstruct_max_drawdown([("t1", -200.0), ("t2", 100.0)], 1000.0)
    assert dd["max_drawdown_pct"] == round(200.0 / 1100.0 * 100.0, 2), "峰=窗口初 1100, 谷=900"


def test_reconstruct_drawdown_unmeasurable_is_none_not_zero():
    """Ⓓ 核心钉: 旧码在 capital_state 单行时静默返回 0.0 (=免检假绿)。现在必须 None。"""
    assert esc.reconstruct_max_drawdown([], 1000.0) is None, "无出场事件 → 不可测"
    assert esc.reconstruct_max_drawdown([("t1", -5.0)], None) is None, "无权益锚点 → 不可测"
    assert esc.reconstruct_max_drawdown([("t1", -5.0)], 0.0) is None, "锚点 ≤0 → 不可测"
    assert esc.reconstruct_max_drawdown([("t1", -5.0)], -100.0) is None


def test_reconstruct_drawdown_single_row_state_table_cannot_fake_green():
    """② 的反证: 只有一笔出场时也给出真实回撤 (2 个曲线点), 而不是 0.0%。"""
    got = esc.reconstruct_max_drawdown([("t1", -500.0)], 9500.0)
    assert got["n_points"] == 2
    assert got["max_drawdown_pct"] == round(500.0 / 10000.0 * 100.0, 2) == 5.0
    assert got["max_drawdown_pct"] >= 5.0, "5% 阈值线上, ok 腿必须判不过而非免检"


# ══════════════════════ Ⓖ 触发门同口径锚点 ══════════════════════

def test_anchor_is_single_source_of_truth_with_baseline():
    assert tgr.ANCHOR_SINCE == "2026-07-24"
    assert "BASELINE = ANCHOR_SINCE" in (PROJECT_ROOT / "scripts" / "trigger_gate_report.py").read_text(
        encoding="utf-8"), "基线警告与同口径锚点必须共用一个日期 (改锚点=改判据)"


def _hist(tmp_path, docs):
    import json
    p = tmp_path / "trigger_gate_history.jsonl"
    p.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in docs) + "\n", encoding="utf-8")
    return p


def test_streak_ignores_rolling_window_rows(tmp_path, monkeypatch):
    """Ⓖ 滚动 since 的旧行没有 fixed_anchor → 不得计入 (计入即造假时序)。"""
    monkeypatch.setattr(tgr, "OUT_HISTORY", _hist(tmp_path, [
        {"since": "2026-08-05", "adverse_selection_gap_pct": 2.285, "timestamp": "x"},
        {"since": "2026-08-07", "adverse_selection_gap_pct": 0.703, "timestamp": "y"},
    ]))
    st = tgr._fixed_anchor_streak(1.5, True)
    assert st["n_points"] == 1, "只算当前这一个锚点观测"
    assert st["consecutive_positive_robust"] == 1
    assert st["criterion_ge2_met"] is False


def test_streak_counts_consecutive_anchor_points(tmp_path, monkeypatch):
    monkeypatch.setattr(tgr, "OUT_HISTORY", _hist(tmp_path, [
        {"fixed_anchor": {"adverse_selection_gap_pct": 3.0, "robust_both_directions": True},
         "generated_at": "2026-09-06T06:24:01-04:00"},
        {"fixed_anchor": {"adverse_selection_gap_pct": 2.8, "robust_both_directions": True},
         "generated_at": "2026-09-13T06:24:01-04:00"},
    ]))
    st = tgr._fixed_anchor_streak(2.5, True)
    assert st["n_points"] == 3
    assert st["consecutive_positive_robust"] == 3
    assert st["criterion_ge2_met"] is True, "≥2 个同口径观测点 → '持续≥2周'判据可裁"


def test_streak_breaks_on_negative_or_non_robust(tmp_path, monkeypatch):
    monkeypatch.setattr(tgr, "OUT_HISTORY", _hist(tmp_path, [
        {"fixed_anchor": {"adverse_selection_gap_pct": 3.0, "robust_both_directions": True}},
        {"fixed_anchor": {"adverse_selection_gap_pct": -0.4, "robust_both_directions": True}},
        {"fixed_anchor": {"adverse_selection_gap_pct": 2.0, "robust_both_directions": True}},
    ]))
    st = tgr._fixed_anchor_streak(1.0, False)
    assert st["consecutive_positive_robust"] == 0, "缺口为正但不双向稳健 → 连续计数断开"
    assert st["criterion_ge2_met"] is False
    st2 = tgr._fixed_anchor_streak(1.0, True)
    assert st2["consecutive_positive_robust"] == 2, "从尾部往前数, 遇到负缺口即停"


def test_streak_collapses_same_day_runs(tmp_path, monkeypatch):
    """判据要的是**时间跨度**, 不是运行次数: 同日重跑不得凑够 2 个点假满足"持续≥2周"。"""
    monkeypatch.setattr(tgr, "OUT_HISTORY", _hist(tmp_path, [
        {"fixed_anchor": {"adverse_selection_gap_pct": 3.0, "robust_both_directions": True},
         "generated_at": "2026-09-06T06:24:01-04:00"},
        {"fixed_anchor": {"adverse_selection_gap_pct": 2.9, "robust_both_directions": True},
         "generated_at": "2026-09-06T17:50:00-04:00"},
    ]))
    st = tgr._fixed_anchor_streak(2.859, True, at="2026-09-06T18:10:00-04:00")
    assert st["n_points"] == 1, "同日三次运行 → 只算 1 个观测点"
    assert st["consecutive_positive_robust"] == 1
    assert st["criterion_ge2_met"] is False
    assert st["points"][-1]["gap_pct"] == 2.859, "同日保留最后一次 (最新值)"


def test_streak_requires_distinct_days_to_meet_criterion(tmp_path, monkeypatch):
    monkeypatch.setattr(tgr, "OUT_HISTORY", _hist(tmp_path, [
        {"fixed_anchor": {"adverse_selection_gap_pct": 3.0, "robust_both_directions": True},
         "generated_at": "2026-09-06T06:24:01-04:00"},
    ]))
    st = tgr._fixed_anchor_streak(2.9, True, at="2026-09-13T06:24:01-04:00")
    assert st["n_points"] == 2
    assert st["criterion_ge2_met"] is True, "两个**不同日**的锚点观测 → 判据可裁"


def test_streak_survives_missing_or_corrupt_history(tmp_path, monkeypatch):
    monkeypatch.setattr(tgr, "OUT_HISTORY", tmp_path / "nope.jsonl")
    st = tgr._fixed_anchor_streak(1.0, True)
    assert st["n_points"] == 1 and st["criterion_ge2_met"] is False
    assert st["history_rows_skipped_bad_json"] == 0 and st["history_error"] is None
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json}\n\n", encoding="utf-8")
    monkeypatch.setattr(tgr, "OUT_HISTORY", bad)
    st2 = tgr._fixed_anchor_streak(1.0, True)
    assert st2["n_points"] == 1, "坏行跳过, 不得抛异常拖垮只读报表"
    assert st2["history_rows_skipped_bad_json"] == 1, (
        "坏行必须**显性计数** — 静默 continue 会让'零观测点'与'history 坏了'不可区分 "
        "(R5 模式 / except:pass 棘轮基线只降不升)")
    assert st2["history_error"] is None
