"""B2 (DEB-20261128-hk-collector-constituent-stale): HK 采集器成分级时点/状态守卫契约.

单变量 = 成分报价当日有效性 (镜像指数 stale 判据: update_time 日期==今日 ∧ sec_status==NORMAL):
  - 指数新鲜≠成分新鲜: 停牌/前日报价成分不得计入当日篮子均值/breadth/n_available/turnover;
  - 非新鲜成分当日 chg 标缺失 (None, 不归零), 原始 last/prev_close/update_time/sec_status
    保留 (tick_rows 真值源可复算), 条件字段 stale_reason 仅非新鲜时出现 (全新鲜旧文档逐字节不变);
  - stale_reason 枚举 (单位/量纲声明, 军规 2026-09-02): enum ∈
    {quote_not_today, non_normal_status, quote_not_today+non_normal_status};
    n_available/n_missing = count (新鲜有限成员数 / 缺失成员数);
  - 指数级 is_stale_tick 闸不变 (仍只核指数); 指数分支不受 B2 影响 (股票分支限定);
  - ADR 港股腿 / day-doc 序列自然排除 stale 派生的 None;
  - 真实数据回归: 2026-11-27 周五快照全新鲜 → derive_day_doc 幂等重算 == 存储 doc 逐字一致.

病灶合成证据 (pre-fix, /tmp/rfc218_b2_pre_evidence.log):
  指数当日新鲜 → is_stale_tick=False 整 tick 放行; 半导体一成分前一日报价 +10% 仍进篮子
  eq=11.6667 n_available=3 breadth up=3 turnover 含旧值; SUSPENDED 当日成分同样被计入.
"""
import json
from pathlib import Path

import pytest

import scripts.hk_market_context_collector as mod

TS = "2026-11-30T10:07:00+08:00"
TODAY = "2026-11-30"
REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SNAPSHOT = REPO_ROOT / "data/shadow/hk_us_linkage/_verify/2026-11-27.json"
STALE_ENUM = {"quote_not_today", "non_normal_status", "quote_not_today+non_normal_status"}


def _row(code, last, ut="2026-11-30 10:07:00", st="NORMAL", prev=100.0, turnover=1e6):
    return {"code": code, "last_price": last, "prev_close_price": prev,
            "high_price": last, "low_price": prev * 0.99, "turnover": turnover,
            "update_time": ut, "sec_status": st}


def _semis_rows(stale_00981=None):
    """00981 可注入 stale 形态; 01347=+20%, 00522=+5% 恒新鲜。"""
    r = {"HK.800000": _row("HK.800000", 102.0),
         "HK.01347": _row("HK.01347", 120.0),
         "HK.00522": _row("HK.00522", 105.0)}
    if stale_00981 is None:
        r["HK.00981"] = _row("HK.00981", 110.0)
    else:
        r["HK.00981"] = stale_00981
    return r


class TestConstituentStaleGuard:
    def test_prev_day_quote_excluded_from_basket(self):
        rows = _semis_rows(_row("HK.00981", 110.0, ut="2026-11-29 16:00:00"))
        tick = mod.build_tick_row(TS, "morning", rows)
        stock = tick["stocks"]["HK.00981"]
        assert stock["chg_pct"] is None
        assert stock["stale_reason"] == "quote_not_today"
        # 原始真值源保留 (可复算)
        assert stock["last"] == 110.0 and stock["prev_close"] == 100.0
        assert stock["update_time"] == "2026-11-29 16:00:00"
        semis = tick["sectors"]["semis"]
        assert semis["n_available"] == 2
        assert semis["missing"] == ["HK.00981"]
        assert semis["chg_pct_eq"] == 12.5            # (20+5)/2, 旧 +10% 不参与
        assert semis["breadth"] == {"up": 2, "flat": 0, "down": 0}
        assert semis["turnover_hkd"] == 2e6           # stale 成员成交额亦排除

    def test_suspended_today_excluded(self):
        rows = _semis_rows(_row("HK.00981", 110.0, st="SUSPENDED"))
        tick = mod.build_tick_row(TS, "morning", rows)
        assert tick["stocks"]["HK.00981"]["chg_pct"] is None
        assert tick["stocks"]["HK.00981"]["stale_reason"] == "non_normal_status"
        assert tick["sectors"]["semis"]["n_available"] == 2

    def test_both_reasons_combined(self):
        rows = _semis_rows(_row("HK.00981", 110.0, ut="2026-11-29 16:00:00", st="SUSPENDED"))
        tick = mod.build_tick_row(TS, "morning", rows)
        assert tick["stocks"]["HK.00981"]["stale_reason"] == "quote_not_today+non_normal_status"

    def test_missing_update_time_is_quote_not_today(self):
        rows = _semis_rows(_row("HK.00981", 110.0, ut=""))
        tick = mod.build_tick_row(TS, "morning", rows)
        assert tick["stocks"]["HK.00981"]["stale_reason"] == "quote_not_today"
        assert tick["stocks"]["HK.00981"]["chg_pct"] is None

    def test_fresh_stock_has_no_stale_reason_key(self):
        tick = mod.build_tick_row(TS, "morning", _semis_rows())
        stock = tick["stocks"]["HK.00981"]
        assert stock["chg_pct"] == 10.0
        assert "stale_reason" not in stock            # 条件字段: 全新鲜无此键
        assert tick["sectors"]["semis"]["n_available"] == 3

    def test_index_fresh_does_not_rescue_stale_constituent(self):
        # 核心审计点: 指数当日新鲜 → 整 tick 不被跳, 但 stale 成分仍被篮子排除
        rows = _semis_rows(_row("HK.00981", 110.0, ut="2026-11-29 16:00:00"))
        tick = mod.build_tick_row(TS, "morning", rows)
        assert mod.is_stale_tick(tick, TODAY) is False
        assert tick["sectors"]["semis"]["n_available"] == 2

    def test_all_constituents_stale_basket_empty_not_zero(self):
        rows = {"HK.800000": _row("HK.800000", 102.0),
                "HK.00981": _row("HK.00981", 110.0, st="SUSPENDED"),
                "HK.01347": _row("HK.01347", 120.0, ut="2026-11-29 16:00:00"),
                "HK.00522": _row("HK.00522", 105.0, st="HALTED")}
        tick = mod.build_tick_row(TS, "morning", rows)
        semis = tick["sectors"]["semis"]
        assert semis["chg_pct_eq"] is None
        assert semis["n_available"] == 0 and semis["n_missing"] == 3
        assert semis["breadth"] == {"up": 0, "flat": 0, "down": 0}
        assert semis["turnover_hkd"] is None

    def test_tick_level_stale_gate_still_index_only(self):
        # 成分 stale 不使整 tick stale (指数级闸不变); 指数 stale 才跳
        rows = _semis_rows(_row("HK.00981", 110.0, st="SUSPENDED"))
        tick = mod.build_tick_row(TS, "morning", rows)
        assert mod.is_stale_tick(tick, TODAY) is False
        rows_bad_idx = dict(rows)
        rows_bad_idx["HK.800000"] = _row("HK.800000", 102.0, ut="2026-11-29 16:00:00")
        assert mod.is_stale_tick(mod.build_tick_row(TS, "morning", rows_bad_idx), TODAY) is True

    def test_index_branch_unaffected_by_b2(self):
        # B2 限定股票分支: 指数 rec 仍按原逻辑算 chg (其新鲜度由 tick 级闸负责)
        rows = {"HK.800000": _row("HK.800000", 102.0, ut="2026-11-29 16:00:00")}
        tick = mod.build_tick_row(TS, "morning", rows)
        idx = tick["indices"]["HK.800000"]
        assert idx["chg_pct"] == 2.0
        assert "stale_reason" not in idx

    def test_stale_reason_enum_and_count_units(self):
        rows = _semis_rows(_row("HK.00981", 110.0, ut="2026-11-29 16:00:00", st="SUSPENDED"))
        tick = mod.build_tick_row(TS, "morning", rows)
        for rec in tick["stocks"].values():
            if "stale_reason" in rec:
                assert rec["stale_reason"] in STALE_ENUM
        semis = tick["sectors"]["semis"]
        assert isinstance(semis["n_available"], int) and isinstance(semis["n_missing"], int)
        assert semis["n_available"] + semis["n_missing"] == 3  # 篮子成员总数守恒


class TestDayDocConstituentStale:
    def test_adr_leg_chg_none_when_stale(self):
        rows = {"HK.800000": _row("HK.800000", 102.0),
                "HK.00700": _row("HK.00700", 110.0, ut="2026-11-29 16:00:00")}
        tick = mod.build_tick_row(TS, "morning", rows)
        doc = mod.derive_day_doc([tick], TODAY)
        assert doc["adr_hk_legs"]["00700"]["chg_pct"] is None

    def test_series_excludes_stale_derived_none_no_infinity(self):
        t1 = mod.build_tick_row(TS, "morning", _semis_rows())
        t2 = mod.build_tick_row("2026-11-30T10:22:00+08:00", "morning",
                                _semis_rows(_row("HK.00981", 110.0, st="SUSPENDED")))
        # t2 另两成分也置 stale → 整篮子 None
        rows2 = {"HK.800000": _row("HK.800000", 102.0),
                 "HK.00981": _row("HK.00981", 110.0, st="SUSPENDED"),
                 "HK.01347": _row("HK.01347", 120.0, st="SUSPENDED"),
                 "HK.00522": _row("HK.00522", 105.0, st="SUSPENDED")}
        t2 = mod.build_tick_row("2026-11-30T10:22:00+08:00", "morning", rows2)
        doc = mod.derive_day_doc([t1, t2], TODAY)
        assert [p["chg_pct"] for p in doc["sectors"]["semis"]["series"]] == [11.6667]
        assert doc["sectors"]["semis"]["latest"]["chg_pct_eq"] is None
        s = json.dumps(doc)
        assert "Infinity" not in s and "NaN" not in s

    @pytest.mark.skipif(not VERIFY_SNAPSHOT.exists(),
                        reason="真实快照不存在 (data/ gitignored, 新克隆无)")
    def test_friday_real_snapshot_idempotent_regression(self):
        stored = json.loads(VERIFY_SNAPSHOT.read_text(encoding="utf-8"))
        recomputed = mod.derive_day_doc(stored["tick_rows"], stored["date"])
        # 周五全新鲜 → B2 条件字段不出现, 幂等重算逐字一致
        assert json.loads(json.dumps(recomputed)) == stored
