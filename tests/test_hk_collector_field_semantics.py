"""B3 (DEB-20261128-hk-collector-field-semantics): 观测字段语义诚实化契约.

单变量 = rollup 字段语义命名 (schema v1→v2, 值不变仅改名 + 指数真实盘中高低分列):
  - first_obs_chg_pct = 首个采样观测 chg (非交易所开盘; open_price 未接入, 不得伪装);
  - obs_high/obs_low_chg_pct = 采样观测 chg 极值 (非真实盘中高低);
  - 指数 day_high/day_low_chg_pct = 交易所报盘中高低 chg, 与采样极值分列;
    缺失→None, **不回退采样值冒充** (v1 混合语义废止);
  - 篮子无 day_high/day_low (篮子均值的真实盘中高低不可由成分高低推导);
  - sign_cross_up/down = 相邻采样观测相对前收穿零, 非趋势反转 (kind 名未动, 语义声明);
  - tick_rows 真值源 schema 未动 (tick 级指数 high/low_chg_pct 保留);
  - 旧值保留: v2→v1 显式投影后与旧文档逐字一致 (真实快照回归见 B1/B2 契约文件).

病灶合成证据 (pre-fix, /tmp/rfc218_b3_pre_evidence.log):
  (a) open_chg_pct=首观测+1.0, 真实开盘+3.0 被丢弃; (b) 同键 high_chg_pct 指数=交易所+4.0
  vs 篮子=采样均值+2.0 双语义; (c) 交易所 hi 缺失→采样 max 回退冒充 high_chg_pct.
"""
import json

import scripts.hk_market_context_collector as mod
from tests.hk_day_doc_v1_compat import project_day_doc_to_v1

TS1 = "2026-11-30T09:37:00+08:00"
TS2 = "2026-11-30T09:52:00+08:00"
TS3 = "2026-11-30T10:07:00+08:00"
TODAY = "2026-11-30"


def _row(code, last, hi=None, lo=None, prev=100.0):
    return {"code": code, "last_price": last, "prev_close_price": prev,
            "high_price": hi if hi is not None else last,
            "low_price": lo if lo is not None else prev * 0.99,
            "turnover": 1e6, "update_time": "2026-11-30 09:37:00", "sec_status": "NORMAL"}


def _idx_rows(last, hi=None, lo=None):
    return {"HK.800000": _row("HK.800000", last, hi=hi, lo=lo)}


def _semis_rows(chg):
    """三成分同 chg → 篮子等权均值 == chg."""
    last = 100.0 * (1 + chg / 100.0)
    return {"HK.800000": _row("HK.800000", 102.0),
            **{c: _row(c, last) for c in ("HK.00981", "HK.01347", "HK.00522")}}


class TestRollupSemantics:
    def test_first_obs_is_first_sample_not_open(self):
        t1 = mod.build_tick_row(TS1, "morning", _idx_rows(101.0))
        t2 = mod.build_tick_row(TS2, "morning", _idx_rows(102.0))
        roll = mod.derive_day_doc([t1, t2], TODAY)["indices"]["HK.800000"]
        assert roll["first_obs_chg_pct"] == 1.0      # 首观测, 非开盘
        assert roll["latest_chg_pct"] == 2.0
        assert "open_chg_pct" not in roll            # 旧误导名废止

    def test_sector_obs_extremes_are_sampled_mean(self):
        ticks = [mod.build_tick_row(ts, "morning", _semis_rows(c))
                 for ts, c in ((TS1, 1.0), (TS2, 5.0), (TS3, 2.0))]
        roll = mod.derive_day_doc(ticks, TODAY)["sectors"]["semis"]
        assert roll["obs_high_chg_pct"] == 5.0       # 采样均值极值
        assert roll["obs_low_chg_pct"] == 1.0
        assert roll["first_obs_chg_pct"] == 1.0
        assert "day_high_chg_pct" not in roll        # 篮子无真实盘中高低 (不可推导)
        assert "day_low_chg_pct" not in roll

    def test_index_day_high_low_separate_from_obs(self):
        # 交易所 hi=104 (+4%) 为采样点间尖峰; 采样 chg 1.0→2.0 → 两列分列且值不同
        t1 = mod.build_tick_row(TS1, "morning", _idx_rows(101.0, hi=104.0, lo=100.5))
        t2 = mod.build_tick_row(TS2, "morning", _idx_rows(102.0, hi=104.0, lo=100.5))
        roll = mod.derive_day_doc([t1, t2], TODAY)["indices"]["HK.800000"]
        assert roll["day_high_chg_pct"] == 4.0       # 交易所真实盘中高
        assert roll["day_low_chg_pct"] == 0.5
        assert roll["obs_high_chg_pct"] == 2.0       # 采样极值另列
        assert roll["obs_low_chg_pct"] == 1.0
        assert "high_chg_pct" not in roll            # 旧混合键废止 (rollup 级)
        assert "low_chg_pct" not in roll

    def test_missing_exchange_high_none_not_sampled_fallback(self):
        # v1 病灶 (c): 交易所 hi 缺失时采样 max 回退冒充 → v2 必须 None, 采样值留 obs 列
        t1 = mod.build_tick_row(TS1, "morning", _idx_rows(101.0, hi=float("inf"), lo=100.5))
        roll = mod.derive_day_doc([t1], TODAY)["indices"]["HK.800000"]
        assert roll["day_high_chg_pct"] is None      # 未知不归零不回退
        assert roll["obs_high_chg_pct"] == 1.0
        assert roll["day_low_chg_pct"] == 0.5        # lo 有限正常

    def test_day_high_max_across_ticks(self):
        t1 = mod.build_tick_row(TS1, "morning", _idx_rows(101.0, hi=103.0))
        t2 = mod.build_tick_row(TS2, "morning", _idx_rows(102.0, hi=105.0))
        roll = mod.derive_day_doc([t1, t2], TODAY)["indices"]["HK.800000"]
        assert roll["day_high_chg_pct"] == 5.0       # 交易所高随日单调, 取跨 tick max

    def test_rollup_all_missing_four_none(self):
        r = mod._rollup([{"ts": "t1", "chg_pct": None}])
        assert r == {"first_obs_chg_pct": None, "obs_high_chg_pct": None,
                     "obs_low_chg_pct": None, "latest_chg_pct": None}

    def test_latest_is_last_non_none(self):
        series = [{"ts": "t1", "chg_pct": 1.0}, {"ts": "t2", "chg_pct": 3.0},
                  {"ts": "t3", "chg_pct": None}]
        assert mod._rollup(series)["latest_chg_pct"] == 3.0


class TestSchemaAndTruthSource:
    def test_schema_version_2_no_legacy_rollup_key(self):
        t1 = mod.build_tick_row(TS1, "morning", _semis_rows(1.0))
        doc = mod.derive_day_doc([t1], TODAY)
        assert doc["schema_version"] == 2
        assert '"open_chg_pct"' not in json.dumps(doc)

    def test_units_declare_new_semantics(self):
        u = mod.UNITS
        for k in ("first_obs_chg_pct", "obs_high_chg_pct", "obs_low_chg_pct",
                  "day_high_chg_pct", "day_low_chg_pct", "latest_chg_pct"):
            assert u[k] == "percent"
        assert u["high_chg_pct"] == "percent"        # tick 级真值源保留声明
        assert u["low_chg_pct"] == "percent"

    def test_tick_truth_source_schema_unchanged(self):
        t1 = mod.build_tick_row(TS1, "morning", _idx_rows(101.0, hi=104.0))
        assert set(t1["indices"]["HK.800000"].keys()) == {
            "last", "prev_close", "chg_pct", "update_time", "sec_status",
            "high_chg_pct", "low_chg_pct", "turnover"}
        assert t1["indices"]["HK.800000"]["high_chg_pct"] == 4.0  # tick 级仍是交易所值
        t2 = mod.build_tick_row(TS1, "morning", _semis_rows(1.0))
        assert "first_obs_chg_pct" not in json.dumps(t2)          # tick 级无 rollup 键

    def test_idempotent_recompute_v2(self):
        ticks = [mod.build_tick_row(TS1, "morning", _semis_rows(1.0)),
                 mod.build_tick_row(TS2, "morning", _semis_rows(2.0))]
        assert mod.derive_day_doc(ticks, TODAY) == mod.derive_day_doc(ticks, TODAY)

    def test_v1_projection_roundtrip_synthetic(self):
        t1 = mod.build_tick_row(TS1, "morning", _idx_rows(101.0, hi=104.0, lo=100.5))
        doc = mod.derive_day_doc([t1], TODAY)
        v1 = project_day_doc_to_v1(doc)
        idx = v1["indices"]["HK.800000"]
        assert set(idx.keys()) == {"open_chg_pct", "high_chg_pct", "low_chg_pct",
                                   "latest_chg_pct", "name", "latest_turnover_hkd", "series"}
        assert idx["high_chg_pct"] == doc["indices"]["HK.800000"]["day_high_chg_pct"]
        assert v1["schema_version"] == 1

    def test_no_open_price_fabrication(self):
        # open_price 未接入 (后续批): 不得出现伪装真实开盘的字段
        t1 = mod.build_tick_row(TS1, "morning", _idx_rows(101.0))
        s = json.dumps(mod.derive_day_doc([t1], TODAY))
        assert "day_open_chg_pct" not in s and "open_price" not in s


class TestEventSemantics:
    def test_zero_cross_across_none_gap_not_trend_reversal(self):
        series = [{"ts": "t1", "chg_pct": -1.0}, {"ts": "t2", "chg_pct": None},
                  {"ts": "t3", "chg_pct": 0.5}]
        ev = mod.derive_events("index:x", series)
        assert len(ev) == 1 and ev[0]["kind"] == "sign_cross_up"
        assert ev[0]["from_pct"] == -1.0 and ev[0]["to_pct"] == 0.5  # prev=最近非 None

    def test_monotonic_no_cross_event(self):
        series = [{"ts": "t1", "chg_pct": 1.0}, {"ts": "t2", "chg_pct": 2.0}]
        assert mod.derive_events("sector:x", series) == []   # 同向加速 ≠ 穿零事件
