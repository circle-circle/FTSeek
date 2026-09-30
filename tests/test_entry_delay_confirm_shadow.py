#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""entry_delay_confirm_shadow 契约钉 (D2', DEB-20260909 §5.2).

钉住:
  1. price_at 口径: 首根 bar_time≥target 的 bar 开盘价; 覆盖不到 → None
  2. 延迟判定: LONG px≥ep 仍触发 / px<ep 回避; SHORT 镜像; opp_cost 方向归一符号
  3. 聚合数学: 回避率 / 回避价值=−avoided_net / 机会成本只在仍触发笔上计
  4. 毕业判据边界: n≥20 ∧ 避免率≥0.30 ∧ 机会成本严格<0.3%; None 一律不过
  5. 幂等: (trade_id, delay_min) 去重, 重跑零新增
  6. 只读纪律: DB mode=ro, 不 import 写库路径
"""
import json
import sqlite3
from pathlib import Path

from scripts import entry_delay_confirm_shadow as edc


def _bar(hhmm, o):
    return [f"2026-09-08 {hhmm}:00", o, o + 0.5, o - 0.5, o + 0.2]


class TestPriceAt:
    def test_first_bar_at_or_after_target_open(self):
        bars = [_bar("09:36", 100), _bar("09:37", 101), _bar("09:38", 102)]
        assert edc._price_at(bars, "09:37") == 101
        assert edc._price_at(bars, "09:36") == 100

    def test_gap_in_bars_skips_to_next_available(self):
        bars = [_bar("09:36", 100), _bar("09:40", 104)]
        assert edc._price_at(bars, "09:38") == 104   # 09:38 无 bar → 下一根

    def test_beyond_last_bar_returns_none(self):
        assert edc._price_at([_bar("09:36", 100)], "09:50") is None


class TestClock:
    def test_hhmm_roundtrip(self):
        assert edc._hhmm_to_min("09:31") == 571
        assert edc._min_to_hhmm(571) == "09:31"
        assert edc._min_to_hhmm(edc._hhmm_to_min("09:31") + 5) == "09:36"


class TestEvalDelay:
    def test_long_avoided_when_price_falls_back(self):
        still, opp = edc._eval_delay("LONG", 100.0, 99.0)
        assert still is False and opp == -1.0

    def test_long_still_triggers_and_costs_when_price_rises(self):
        still, opp = edc._eval_delay("LONG", 100.0, 101.0)
        assert still is True and opp == 1.0

    def test_long_equal_price_still_triggers(self):
        still, opp = edc._eval_delay("LONG", 100.0, 100.0)
        assert still is True and opp == 0.0

    def test_short_mirror(self):
        still, opp = edc._eval_delay("SHORT", 50.0, 50.5)
        assert still is False and opp == -1.0        # 反抽 1% ⇒ 回避, 回避则赚 1%
        still, opp = edc._eval_delay("SHORT", 50.0, 49.0)
        assert still is True and opp == 2.0          # 卖低 2% ⇒ 延迟更贵


class TestAggBlock:
    def test_math(self):
        recs = [
            {"status": "ok", "still_triggers": False, "pnl_net": -80.0,
             "opp_cost_pct": -1.0, "notional": 5000},
            {"status": "ok", "still_triggers": True, "pnl_net": 30.0,
             "opp_cost_pct": 1.0, "notional": 5000},
            {"status": "no_bars"},   # 非 ok 不计入
        ]
        b = edc._agg_block(recs)
        assert b["n"] == 2 and b["n_avoided"] == 1 and b["n_delayed"] == 1
        assert b["avoidance_rate"] == 0.5
        assert b["avoided_net_usd"] == -80.0 and b["avoided_value_usd"] == 80.0
        assert b["opp_cost_mean_pct"] == 1.0 and b["opp_cost_usd"] == 50.0

    def test_empty_block_nones(self):
        b = edc._agg_block([])
        assert b["n"] == 0 and b["avoidance_rate"] is None
        assert b["opp_cost_mean_pct"] is None
        assert b["net_value_usd"] is None and b["day_clustered"] is None

    def test_net_value_and_day_clustered_math(self):
        # 3 日各 2 笔: 日均值 15/20/25 (回避省 $80~100, 延迟付 $50)
        # ⇒ day_mean=20 (等权日均/笔), sd=5, t=6.93; 合计净价值 $120
        recs = []
        for day, saved in (("2026-09-08", 80.0), ("2026-09-09", 90.0),
                           ("2026-09-10", 100.0)):
            recs += [
                {"status": "ok", "still_triggers": False, "pnl_net": -saved,
                 "opp_cost_pct": -1.0, "notional": 5000, "date": day},
                {"status": "ok", "still_triggers": True, "pnl_net": 30.0,
                 "opp_cost_pct": 1.0, "notional": 5000, "date": day},
            ]
        b = edc._agg_block(recs)
        assert b["net_value_usd"] == 120.0
        assert b["day_clustered"]["n_days"] == 3
        assert b["day_clustered"]["day_mean"] == 20.0
        assert b["day_clustered"]["distinguishable"] is True
        assert b["day_clustered"]["unit"].startswith("usd_per_trade")


class TestDayClusteredCharterRule9:
    """宪章第 9 律: pooled 读数须配日聚类 t + MDE(80%); n_days<3 不可判."""

    def test_math_known_values(self):
        # 日均值 [10, 10, 10] ⇒ sd=0 → None (零方差不可判, 不当 0/PASS)
        assert edc._day_clustered_t([10, 10, 10]) is None

    def test_less_than_three_days_returns_none(self):
        assert edc._day_clustered_t([1.0, 2.0]) is None
        assert edc._day_clustered_t([]) is None

    def test_positive_shift_is_distinguishable(self):
        # 日均值 [8, 10, 12] ⇒ mean=10 sd=2 t=10/sqrt(4/3)=8.66 ≥2 ⇒ 可分辨
        dc = edc._day_clustered_t([8.0, 10.0, 12.0])
        assert dc["n_days"] == 3 and dc["day_mean"] == 10.0
        assert dc["t"] == 8.66 and dc["distinguishable"] is True
        assert dc["mde_80pct"] > 0  # MDE 与入参同量纲 (美元/笔)


class TestGraduation:
    def _blk(self, n, ar, opp):
        return {"n": n, "avoidance_rate": ar, "opp_cost_mean_pct": opp}

    def test_n_gate(self):
        assert edc._grad_pass(self._blk(19, 0.5, 0.1)) is False
        assert edc._grad_pass(self._blk(20, 0.5, 0.1)) is True

    def test_avoidance_boundary(self):
        assert edc._grad_pass(self._blk(20, 0.30, 0.1)) is True
        assert edc._grad_pass(self._blk(20, 0.29, 0.1)) is False

    def test_opp_cost_strictly_below(self):
        assert edc._grad_pass(self._blk(20, 0.5, 0.3)) is False   # 贴线 0.3 不过
        assert edc._grad_pass(self._blk(20, 0.5, 0.29)) is True

    def test_none_never_passes(self):
        assert edc._grad_pass(self._blk(20, None, 0.1)) is False
        assert edc._grad_pass(self._blk(20, 0.5, None)) is False


def _mk_db(path):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT,"
        " entry_time TEXT, entry_price REAL, exit_price REAL, quantity REAL,"
        " exit_reason TEXT, pnl_net REAL, source_type TEXT, mfe_pct REAL)")
    con.execute("INSERT INTO trade_records VALUES (1,'US.AAA','LONG','2026-09-08 09:35:00',"
                "100.0,98.0,50,'SL_INITIAL',-80.0,'PLAYBOOK_SIM',0.0)")
    con.execute("INSERT INTO trade_records VALUES (2,'US.BBB','SHORT','2026-09-08 10:00:00',"
                "50.0,49.0,100,'SL_TRAIL_LOCK',40.0,'PLAYBOOK_SIM',0.5)")
    con.commit()
    con.close()


_BARS = {
    ("US.AAA", "2026-09-08"): [_bar("09:35", 100), _bar("09:36", 100),
                               _bar("09:37", 99), _bar("09:40", 101),
                               _bar("15:59", 101)],
    ("US.BBB", "2026-09-08"): [_bar("10:00", 50), _bar("10:02", 50.5),
                               _bar("10:05", 49), _bar("15:59", 49)],
}


class TestMainIntegration:
    def _setup(self, tmp_path, monkeypatch):
        db = tmp_path / "t.db"
        _mk_db(db)
        monkeypatch.setattr(edc, "DB", db)
        monkeypatch.setattr(edc, "OUT", tmp_path / "out.jsonl")
        monkeypatch.setattr(edc, "OUT_LATEST", tmp_path / "latest.json")
        gap = tmp_path / "gap.jsonl"
        gap.write_text(json.dumps({"trade_id": 1, "source": "UE"}) + "\n", encoding="utf-8")
        monkeypatch.setattr(edc, "GAP_JSONL", gap)
        monkeypatch.setattr(edc, "_full_day",
                            lambda s, d, allow_fetch: (_BARS.get((s, d)), False))

    def test_rows_and_summary_math(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        n_new = edc.main(only_date="2026-09-08", allow_fetch=False)
        assert n_new == 4                     # 2 trades × 2 delays
        rows = [json.loads(l) for l in edc.OUT.read_text(encoding="utf-8").splitlines()]
        by_key = {(r["trade_id"], r["delay_min"]): r for r in rows}
        r12 = by_key[(1, 2)]
        assert r12["status"] == "ok" and r12["still_triggers"] is False  # 99 < 100 回避
        assert r12["source"] == "UE"
        r15 = by_key[(1, 5)]
        assert r15["still_triggers"] is True and r15["opp_cost_pct"] == 1.0
        assert by_key[(2, 2)]["still_triggers"] is False                 # 50.5 > 50 回避
        assert by_key[(2, 5)]["opp_cost_pct"] == 2.0
        assert by_key[(2, 2)]["source"] == "?"                           # gap 覆盖缺口

        latest = json.loads(edc.OUT_LATEST.read_text(encoding="utf-8"))
        d2 = latest["delays"]["2"]["ALL"]
        assert d2["n"] == 2 and d2["avoidance_rate"] == 1.0
        assert d2["avoided_value_usd"] == 40.0       # -(-80+40)
        assert d2["opp_cost_mean_pct"] is None       # 无仍触发笔
        assert latest["graduation"]["2"]["ALL_pass"] is False   # opp None → 不过
        sl2 = latest["delays"]["2"]["SL_INITIAL"]
        assert sl2["n"] == 1 and sl2["avoidance_rate"] == 1.0
        mfe2 = latest["delays"]["2"]["mfe_cohort"]["full_window"]
        assert mfe2["death_mfe_lt_0_3"]["n"] == 1
        assert mfe2["death_mfe_lt_0_3"]["avoided_value_usd"] == 80.0
        assert mfe2["alive_mfe_ge_0_3"]["n"] == 1
        assert mfe2["alive_mfe_ge_0_3"]["avoided_value_usd"] == -40.0
        assert mfe2["mfe_zero_le_0_001"]["n"] == 1
        assert latest["delays"]["2"]["mfe_cohort"]["forward"]["death_mfe_lt_0_3"]["n"] == 0
        d5 = latest["delays"]["5"]["ALL"]
        assert d5["n"] == 2 and d5["n_avoided"] == 0
        assert d5["opp_cost_mean_pct"] == 1.5 and d5["opp_cost_usd"] == 150.0
        assert "confirm_gate" in latest["disclosures"][0]   # 强制披露随汇总输出
        assert "TTL=300s" in latest["disclosures"][1]

    def test_idempotent_rerun_adds_zero(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        edc.main(only_date="2026-09-08", allow_fetch=False)
        n2 = edc.main(only_date="2026-09-08", allow_fetch=False)
        assert n2 == 0
        assert len(edc.OUT.read_text(encoding="utf-8").splitlines()) == 4


class TestReadOnlyDiscipline:
    def test_db_opened_readonly_and_no_write_imports(self):
        src = Path(edc.__file__).read_text(encoding="utf-8")
        assert "mode=ro" in src
        for banned in ("playbook.db", "trade_store", "db_pool"):
            assert banned not in src, f"影子脚本不得引入写库路径: {banned}"
