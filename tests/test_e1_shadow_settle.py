#!/usr/bin/env python3
"""E1 GATE 1 结算器契约测试 (2026-09-05, #24)。

钉住的不变量:
  1. 预注册冻结: 窗口 8/17-8/28 / MIN_N=30 / DIFF_GATE=$10 / 双周切分
  2. 臂P top-3 选择: 强度降序, tie 确定性 (symbol 升序), 并列日披露
  3. 臂D = 与实际成交 (trade_date,symbol,direction) 匹配的子集
  4. 结算方向感知: LONG 涨为盈, SHORT 跌为盈; 费用真实扣减 (最低佣金非线性)
  5. 裁决三路径: E1_SUCCESS / E1_FAIL(差<$10 或双周异号) / INSUFFICIENT_SAMPLE(n<30)
  6. PYTEST 守卫: main() 默认 out 在测试下不写生产 JSON
  7. bar 缺失: 跳过并计数, 不静默当 0
"""
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

spec = importlib.util.spec_from_file_location(
    "e1_shadow_settle", REPO / "scripts" / "e1_shadow_settle.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def _sig(sym, day, strength, direction="LONG", nopen=100.0, nday=None):
    return {"symbol": sym, "trade_date": day, "direction": direction,
            "strength": strength, "strategy_id": "deep_analysis",
            "next_open_price": nopen, "next_open_date": nday or day,
            "borrow_fee_annual_pct": 0.05 if direction == "SHORT" else 0.0}


def _mk_env(tmp_path, recs, bars, trades):
    """bars: {symbol: [[date,o,h,l,c],...]}; trades: (date,sym,dir,entry,qty)"""
    sj = tmp_path / "shadow.jsonl"
    sj.write_text("\n".join(json.dumps(r) for r in recs))
    cdir = tmp_path / "bars"
    cdir.mkdir()
    for sym, bs in bars.items():
        (cdir / f"daily_{sym.replace('.', '_')}_2026-08-01_2026-09-30.json"
         ).write_text(json.dumps(bs))
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE trade_records (
        id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, direction TEXT,
        entry_price REAL, quantity INTEGER, trade_mode TEXT DEFAULT 'simulate')""")
    conn.executemany(
        "INSERT INTO trade_records (trade_date,symbol,direction,entry_price,quantity,trade_mode)"
        " VALUES (?,?,?,?,?,'simulate')", trades)
    conn.commit()
    conn.close()
    return sj, cdir, db


# ── 1. 冻结常量 ──────────────────────────────────────────
def test_frozen_constants():
    assert m.WINDOW == ("2026-08-17", "2026-08-28")
    assert m.MIN_N == 30 and m.DIFF_GATE == 10.0
    assert m.WEEK_SPLIT["W1"] == ("2026-08-17", "2026-08-21")
    assert m.WEEK_SPLIT["W2"] == ("2026-08-24", "2026-08-28")


# ── 2. 臂P top-3 与 tie 确定性 ───────────────────────────
def test_arm_p_top3_deterministic_tiebreak(tmp_path):
    day = "2026-08-17"
    recs = [_sig("US.A", day, 70), _sig("US.B", day, 70),
            _sig("US.C", day, 70), _sig("US.D", day, 70),
            _sig("US.E", day, 80)]  # E 最强; A-D 同分 tie
    close = {("US." + s, day): 110.0 for s in "ABCDE"}
    p, d, tie_days = m.build_arms(recs, set(), 1000.0, close)
    assert [x["symbol"] for x in p] == ["US.E", "US.A", "US.B"]  # 强度降序+字母序
    assert tie_days == 1  # 第3/4名并列被披露


# ── 3. 臂D 匹配子集 ──────────────────────────────────────
def test_arm_d_matches_actual_trades(tmp_path):
    day = "2026-08-17"
    recs = [_sig("US.A", day, 90), _sig("US.B", day, 80),
            _sig("US.C", day, 70, "SHORT")]
    keys = {("2026-08-17", "US.A", "LONG"), ("2026-08-17", "US.C", "SHORT")}
    close = {("US.A", day): 110.0, ("US.B", day): 110.0, ("US.C", day): 90.0}
    p, d, _ = m.build_arms(recs, keys, 1000.0, close)
    assert {x["symbol"] for x in d} == {"US.A", "US.C"}  # US.B 未实际成交 → 不在臂D


# ── 4. 方向感知结算 + 费用扣减 ────────────────────────────
def test_settle_direction_and_fees():
    r_long = _sig("US.A", "2026-08-17", 90, "LONG", 100.0)
    r_short = _sig("US.A", "2026-08-17", 90, "SHORT", 100.0)
    # 涨 10%: LONG 盈 / SHORT 亏 (含费, 净额必小于毛额)
    nl = m.settle_one(r_long, 110.0, 1000.0)
    ns = m.settle_one(r_short, 110.0, 1000.0)
    assert 90 < nl < 100     # 毛利 $100, 费用>0
    assert -100 > ns         # SHORT 亏损超 $100 (费叠加)
    # 跌 10% 镜像
    assert m.settle_one(r_short, 90.0, 1000.0) > 90
    # 小名义下最低佣金 $1.99×2 主导 → 净额非线性
    tiny = m.settle_one(r_long, 110.0, 100.0)
    assert tiny < 10.0 - 3.9  # 毛利$10 - 双边最低佣金≈$3.98


# ── 5. 裁决三路径 ─────────────────────────────────────────
def _row(week, usd):
    return {"symbol": "US.A", "trade_date": "2026-08-17", "direction": "LONG",
            "strength": 90, "net_usd": usd, "week": week}


def test_adjudicate_paths():
    # SUCCESS: P>0, 差≥10, 双周同号为正
    p = [_row("W1", 30.0)] * 15 + [_row("W2", 30.0)] * 15
    d = [_row("W1", 10.0)] * 15 + [_row("W2", 10.0)] * 15
    r = m.adjudicate(p, d)
    assert r["verdict"] == "E1_SUCCESS" and r["diff"] == 20.0
    # FAIL: 差 <$10
    d2 = [_row("W1", 25.0)] * 15 + [_row("W2", 25.0)] * 15
    assert m.adjudicate(p, d2)["verdict"] == "E1_FAIL"
    # FAIL: 双周异号 (W2 差为负)
    d3 = [_row("W1", 0.0)] * 15 + [_row("W2", 99.0)] * 15
    r3 = m.adjudicate(p, d3)
    assert r3["verdict"] == "E1_FAIL" and r3["weekly_same_sign"] is False
    # INSUFFICIENT: n<30 不排名
    r4 = m.adjudicate(p[:20], d)
    assert r4["verdict"] == "INSUFFICIENT_SAMPLE" and "diff" not in r4
    # FAIL: P 均笔 <= 0 即使赢 D (两臂皆亏 = 假设"绕开即改善"不成立)
    p5 = [_row("W1", -5.0)] * 15 + [_row("W2", -5.0)] * 15
    assert m.adjudicate(p5, d)["verdict"] == "E1_FAIL"


# ── 6. PYTEST 守卫: 默认 out 不写生产 JSON ────────────────
def test_pytest_guard_redirects_default_out(tmp_path, monkeypatch):
    day = "2026-08-17"
    recs = [_sig("US.A", day, 90)]
    sj, cdir, db = _mk_env(tmp_path, recs,
                           {"US.A": [[day, 100, 101, 99, 110]]},
                           [("2026-08-17", "US.A", "LONG", 100.0, 10)])
    fake_prod_out = tmp_path / "prod_verdict.json"
    monkeypatch.setattr(m, "OUT_JSON", fake_prod_out)
    assert os.environ.get("PYTEST_CURRENT_TEST")
    rc = m.main(["--shadow", str(sj), "--db", str(db), "--cache-dir", str(cdir)])
    assert rc == 0
    assert not fake_prod_out.exists()  # 生产 JSON 未被写


# ── 7. 显式 --out 直通 + bar 缺失计数 ─────────────────────
def test_explicit_out_and_missing_bar(tmp_path):
    day = "2026-08-17"
    recs = [_sig("US.A", day, 90), _sig("US.B", day, 80)]  # B 无 bar
    sj, cdir, db = _mk_env(tmp_path, recs,
                           {"US.A": [[day, 100, 101, 99, 110]]},
                           [("2026-08-17", "US.A", "LONG", 100.0, 10)] * 30)
    out = tmp_path / "o.json"
    rc = m.main(["--shadow", str(sj), "--db", str(db),
                 "--cache-dir", str(cdir), "--out", str(out)])
    assert rc == 0
    res = json.loads(out.read_text())
    assert res["bar_missing"] == 1
    assert res["arm_d_avg_notional"] == 1000.0
    assert res["pre_registered_window"] is True
    # 扩展窗标记
    out2 = tmp_path / "o2.json"
    m.main(["--shadow", str(sj), "--db", str(db), "--cache-dir", str(cdir),
            "--out", str(out2), "--window", "2026-08-17:2026-09-30"])
    assert json.loads(out2.read_text())["pre_registered_window"] is False
