"""C 批 (DEB-20261128-us-adr-leg): 美股 ADR 对应腿影子采集契约.

单变量 = 美股对应腿新采集通道 (record-only, 先同公司可交易 ADR 4 只):
  - ET 时区采样窗 (premarket 04:00-09:30 / regular_open 09:30-10:15), DST 自动适应;
  - OTC 绝不入请求 (探针实证 TCEHY 混入致整批 ret=-1);
  - 成分级时点/状态守卫 (B2 范式) + B1 有限值守卫 (复用 HK 采集器原语);
  - B3 诚实 rollup 命名 (first_obs/obs_high/obs_low/latest);
  - date=ET 交易日 (配对合同); ADS 比例凭据双源 (公开条款 + 真实平价交叉验证);
  - flock/原子写/去重/cap/fail-safe/force→_verify/dry 零写盘 (HK 范式)。

探针凭据: /tmp/rfc218_c_us_probe.log (OTC 毒化整批) + /tmp/rfc218_c_us_probe2.log
(4 只交易所上市 ret_code=0, 2026-11-27 半日盘后实时)。
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import scripts.us_adr_context_collector as mod

CST = ZoneInfo("Asia/Shanghai")
ET = ZoneInfo("America/New_York")
TODAY_ET = "2026-11-30"


def _cst(s):
    return datetime.fromisoformat(s).replace(tzinfo=CST)


MON_1707 = _cst("2026-11-30T17:07:00")   # 04:07 ET premarket
MON_2237 = _cst("2026-11-30T22:37:00")   # 09:37 ET regular_open
MON_2245 = _cst("2026-11-30T22:45:00")   # 09:45 ET 入场参考锚
MON_2322 = _cst("2026-11-30T23:22:00")   # 10:22 ET 窗外
MON_1607 = _cst("2026-11-30T16:07:00")   # 03:07 ET 盘前未始
SAT_1707 = _cst("2026-11-28T17:07:00")   # 周六


def _row(code, last, ut="2026-11-30 09:37:00.123", st="NORMAL", prev=100.0):
    return {"code": code, "last_price": last, "prev_close_price": prev,
            "update_time": ut, "sec_status": st}


def _rows(chg=2.0, ut="2026-11-30 09:37:00.123", st="NORMAL"):
    last = 100.0 * (1 + chg / 100.0)
    return {c: _row(c, last, ut=ut, st=st) for c in mod.ADR_META}


class _FakeCtx:
    def __init__(self, rows, ret=0):
        self.df = pd.DataFrame(list(rows.values()))
        self.ret = ret
        self.requested = None
        self.closed = False

    def get_market_snapshot(self, codes):
        self.requested = list(codes)
        return self.ret, self.df

    def close(self):
        self.closed = True


def _factory(rows, ret=0):
    holder = {}

    def f():
        holder["ctx"] = _FakeCtx(rows, ret)
        return holder["ctx"]
    f.holder = holder
    return f


class TestSessionGateET:
    def test_premarket_boundaries(self):
        assert mod.session_for(datetime(2026, 11, 30, 4, 0, tzinfo=ET)) == "premarket"
        assert mod.session_for(datetime(2026, 11, 30, 3, 59, tzinfo=ET)) is None
        assert mod.session_for(datetime(2026, 11, 30, 9, 29, tzinfo=ET)) == "premarket"

    def test_regular_open_boundaries(self):
        assert mod.session_for(datetime(2026, 11, 30, 9, 30, tzinfo=ET)) == "regular_open"
        assert mod.session_for(datetime(2026, 11, 30, 9, 45, tzinfo=ET)) == "regular_open"
        assert mod.session_for(datetime(2026, 11, 30, 10, 14, tzinfo=ET)) == "regular_open"
        assert mod.session_for(datetime(2026, 11, 30, 10, 15, tzinfo=ET)) is None

    def test_weekend_none(self):
        assert mod.session_for(datetime(2026, 11, 28, 9, 45, tzinfo=ET)) is None  # 周六

    def test_dst_robust_same_et_wallclock(self):
        # 夏令时 2026-07-06 16:07 CST = 04:07 EDT; 冬令时 2026-12-07 17:07 CST = 04:07 EST
        summer = _cst("2026-07-06T16:07:00").astimezone(ET)
        winter = _cst("2026-12-07T17:07:00").astimezone(ET)
        assert (summer.hour, summer.minute) == (4, 7)
        assert (winter.hour, winter.minute) == (4, 7)
        assert mod.session_for(summer) == mod.session_for(winter) == "premarket"

    def test_cst_wide_window_rejected_outside_et(self):
        assert mod.session_for(MON_1607.astimezone(ET)) is None   # 03:07 ET
        assert mod.session_for(MON_2322.astimezone(ET)) is None   # 10:22 ET
        assert mod.session_for(MON_2245.astimezone(ET)) == "regular_open"


class TestTickGuards:
    def test_fresh_no_stale_reason(self):
        t = mod.build_tick_row("ts", "ts_et", "premarket", _rows(), TODAY_ET)
        for r in t["tickers"].values():
            assert r["chg_pct"] == 2.0 and "stale_reason" not in r

    def test_prev_day_quote_stale(self):
        rows = _rows(ut="2026-11-29 16:00:00.000")
        t = mod.build_tick_row("ts", "ts_et", "premarket", rows, TODAY_ET)
        r = t["tickers"]["US.BABA"]
        assert r["chg_pct"] is None and r["stale_reason"] == "quote_not_today"
        assert r["last_usd"] == 102.0 and r["prev_close_usd"] == 100.0  # 原始值保留

    def test_suspended_and_combined_reasons(self):
        rows = _rows(st="SUSPENDED")
        t = mod.build_tick_row("ts", "ts_et", "premarket", rows, TODAY_ET)
        assert t["tickers"]["US.JD"]["stale_reason"] == "non_normal_status"
        rows2 = _rows(ut="2026-11-29 16:00:00", st="HALTED")
        t2 = mod.build_tick_row("ts", "ts_et", "premarket", rows2, TODAY_ET)
        assert t2["tickers"]["US.BIDU"]["stale_reason"] == "quote_not_today+non_normal_status"

    def test_missing_from_snapshot(self):
        rows = _rows()
        del rows["US.NTES"]
        t = mod.build_tick_row("ts", "ts_et", "premarket", rows, TODAY_ET)
        assert t["tickers"]["US.NTES"]["stale_reason"] == "missing_from_snapshot"
        assert t["tickers"]["US.NTES"]["chg_pct"] is None

    def test_finite_guard_inherited(self):
        rows = _rows()
        rows["US.BABA"]["prev_close_price"] = float("inf")   # B1: inf prev → 伪 -100% 被拦
        rows["US.JD"]["last_price"] = float("nan")
        t = mod.build_tick_row("ts", "ts_et", "premarket", rows, TODAY_ET)
        assert t["tickers"]["US.BABA"]["chg_pct"] is None
        assert t["tickers"]["US.JD"]["chg_pct"] is None
        assert t["tickers"]["US.BIDU"]["chg_pct"] == 2.0

    def test_is_stale_tick_all_vs_partial(self):
        all_old = mod.build_tick_row("ts", "e", "premarket", _rows(ut="2026-11-29 16:00:00"), TODAY_ET)
        assert mod.is_stale_tick(all_old) is True
        partial = _rows(ut="2026-11-29 16:00:00")
        partial["US.NTES"] = _row("US.NTES", 102.0)
        t = mod.build_tick_row("ts", "e", "premarket", partial, TODAY_ET)
        assert mod.is_stale_tick(t) is False
        assert mod.is_stale_tick({"tickers": {}}) is True


class TestDayDoc:
    def _ticks(self):
        out = []
        for ts, chg in (("17:07", 1.0), ("17:37", 3.0), ("22:37", 2.0)):
            now = _cst(f"2026-11-30T{ts}:00")
            out.append(mod.build_tick_row(now.isoformat(), now.astimezone(ET).isoformat(),
                                          "premarket", _rows(chg), TODAY_ET))
        return out

    def test_rollup_honest_naming(self):
        doc = mod.derive_day_doc(self._ticks(), TODAY_ET)
        r = doc["tickers"]["US.BABA"]
        assert r["first_obs_chg_pct"] == 1.0
        assert r["obs_high_chg_pct"] == 3.0
        assert r["obs_low_chg_pct"] == 1.0
        assert r["latest_chg_pct"] == 2.0
        assert "open_chg_pct" not in json.dumps(doc)

    def test_meta_ratios_fx_otc(self):
        doc = mod.derive_day_doc(self._ticks(), TODAY_ET)
        assert {c: m["shares_per_ads"] for c, m in doc["adr_meta"].items()} == {
            "US.BABA": 8, "US.JD": 2, "US.BIDU": 8, "US.NTES": 5}
        assert doc["fx_hkd_per_usd_declared"] == 7.8
        assert set(doc["otc_unavailable"]) == {"US.TCEHY", "US.MPNGY", "US.XIAOMY"}
        assert not (set(doc["tickers"]) & set(doc["otc_unavailable"]))  # OTC 不采
        assert doc["market"] == "US_ADR" and doc["schema_version"] == 1
        assert doc["date"] == TODAY_ET

    def test_events_sign_cross(self):
        ticks = []
        for ts, chg in (("17:07", -1.0), ("17:37", 0.5)):
            now = _cst(f"2026-11-30T{ts}:00")
            ticks.append(mod.build_tick_row(now.isoformat(), now.astimezone(ET).isoformat(),
                                            "premarket", _rows(chg), TODAY_ET))
        doc = mod.derive_day_doc(ticks, TODAY_ET)
        assert any(e["kind"] == "sign_cross_up" and e["target"] == "adr:US.BABA"
                   for e in doc["events"])

    def test_idempotent(self):
        ticks = self._ticks()
        assert mod.derive_day_doc(ticks, TODAY_ET) == mod.derive_day_doc(ticks, TODAY_ET)

    def test_units_declared(self):
        doc = mod.derive_day_doc(self._ticks(), TODAY_ET)
        assert doc["units"]["last_usd"] == "USD_absolute"
        assert doc["units"]["chg_pct"] == "percent"
        assert doc["units"]["shares_per_ads"] == "count"


class TestRecordTick:
    def test_dry_run_zero_write(self, tmp_path):
        out = tmp_path / "us_leg"   # 尚不存在的子目录 (HK 范式; conftest autouse 会写 tmp_path 根)
        res = mod.record_tick(now=MON_2237, dry_run=True, ctx_factory=_factory(_rows()),
                              out_dir=out)
        assert res["status"] == "dry_run"
        assert not out.exists()

    def test_written_and_otc_never_requested(self, tmp_path):
        f = _factory(_rows())
        res = mod.record_tick(now=MON_2237, ctx_factory=f, out_dir=tmp_path)
        assert res["status"] == "written"
        assert f.holder["ctx"].requested == list(mod.ADR_META)   # 只 4 只, 无 OTC
        assert f.holder["ctx"].closed is True
        doc = json.loads((tmp_path / "2026-11-30.json").read_text())
        assert doc["market"] == "US_ADR" and doc["ticks"] == 1

    def test_date_is_et_not_cst(self, tmp_path):
        # 冬令 CST 17:07 = ET 04:07 同日; 构造 CST 周六 00:30 = ET 周五 11:30 (窗外 skip)
        res = mod.record_tick(now=MON_1707, ctx_factory=_factory(_rows()), out_dir=tmp_path)
        assert res["date"] == "2026-11-30" and res["status"] == "written"
        sat_late = _cst("2026-11-28T00:30:00")   # ET 周五 11:30 → 窗外
        res2 = mod.record_tick(now=sat_late, ctx_factory=_factory(_rows()), out_dir=tmp_path)
        assert res2["status"] == "skipped_session"

    def test_dedup_same_minute(self, tmp_path):
        mod.record_tick(now=MON_2237, ctx_factory=_factory(_rows()), out_dir=tmp_path)
        res = mod.record_tick(now=MON_2237, ctx_factory=_factory(_rows()), out_dir=tmp_path)
        assert res["status"] == "skipped_dedup"

    def test_cap(self, tmp_path):
        doc = {"tick_rows": [{"ts": f"2026-11-30T{i:02d}:00:00+08:00"} for i in range(40)]}
        (tmp_path / "2026-11-30.json").write_text(json.dumps(doc))
        res = mod.record_tick(now=MON_2237, ctx_factory=_factory(_rows()), out_dir=tmp_path)
        assert res["status"] == "skipped_cap"

    def test_stale_skip_all_old(self, tmp_path):
        out = tmp_path / "us_leg"
        res = mod.record_tick(now=MON_2237, ctx_factory=_factory(_rows(ut="2026-11-29 16:00:00")),
                              out_dir=out)
        assert res["status"] == "skipped_stale"
        assert not out.exists()

    def test_force_verify_subdir(self, tmp_path):
        res = mod.record_tick(now=SAT_1707, force=True, ctx_factory=_factory(_rows()),
                              out_dir=tmp_path)
        assert res["status"] == "written"
        # force 绕周末闸; date=ET 日期 (CST 周六 17:07 = ET 周六 04:07); 只落 _verify/
        assert (tmp_path / "_verify" / "2026-11-28.json").exists()
        assert not (tmp_path / "2026-11-28.json").exists()

    def test_weekend_skip(self, tmp_path):
        res = mod.record_tick(now=SAT_1707, ctx_factory=_factory(_rows()), out_dir=tmp_path)
        assert res["status"] == "skipped_session"

    def test_failsafe_error_exit0(self, tmp_path):
        def boom():
            raise RuntimeError("OpenD down")
        res = mod.record_tick(now=MON_2237, ctx_factory=boom, out_dir=tmp_path)
        assert res["status"] == "error"

    def test_main_exit0_on_error(self, monkeypatch, capsys):
        def boom():
            raise RuntimeError("OpenD down")
        monkeypatch.setattr(mod, "_default_ctx_factory", boom)
        monkeypatch.setattr(mod, "_now_cst", lambda: MON_2237)
        assert mod.main([]) == 0
        assert "error" in capsys.readouterr().out

    def test_ret_nonzero_failsafe(self, tmp_path):
        out = tmp_path / "us_leg"
        res = mod.record_tick(now=MON_2237, ctx_factory=_factory(_rows(), ret=-1),
                              out_dir=out)
        assert res["status"] == "error"
        assert not out.exists()
