"""RFC-218 §25: HK 行业/动态趋势影子采集器契约 (record-only, 零判定消费).

钉子:
  - 涨跌幅口径 (percent, 未知/非法 → None 不归零, bool 拒);
  - 行业等权聚合 / breadth 平盘带 / dispersion / 缺失处理;
  - 时段闸 (早盘/午盘/两收盘锚/盘前/午休/周末 → None);
  - 假日/停市 stale 闸 (update_time 日期 != 今日 / sec_status != NORMAL / 指数全缺);
  - 翻红/翻绿事件 (符号穿越 0);
  - day doc 从 tick_rows 幂等重算 (无增量漂移) + 量纲声明;
  - record_tick: dry_run 零写盘 / 窗外跳过 / stale 跳过 / 正常写 / 同分去重 / cap /
    force 落 _verify 不污染主目录 / OpenD 失败 fail-safe 恒不抛;
  - 并发 flock 不丢 tick + 原子写无残留 tmp;
  - ADR 映射与 hkex_source 同源 parity + OTC 标记;
  - 注册表条目 (计数轴=交易日数, USER_ADJUDICATION, config_key=None);
  - 静态零交易/零推送耦合 + 模块导入零副作用。
"""
import importlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import scripts.hk_market_context_collector as mod

CST = ZoneInfo("Asia/Shanghai")
MON_1007 = datetime(2026, 11, 30, 10, 7, tzinfo=CST)   # 周一早盘
TODAY = "2026-11-30"


def _df(chg_map=None, today=TODAY, ut="10:07:00", status="NORMAL", drop=()):
    """合成快照 DataFrame: prev_close=100, last 按 chg_map(百分比) 推。"""
    chg_map = chg_map or {}
    rows = []
    for code in mod._all_codes():
        if code in drop:
            continue
        c = chg_map.get(code, 1.0)
        prev = 100.0
        last = round(prev * (1 + c / 100.0), 4)
        rows.append({
            "code": code, "name": code, "last_price": last, "prev_close_price": prev,
            "open_price": prev, "high_price": round(last * 1.01, 4),
            "low_price": round(prev * 0.99, 4), "volume": 1000.0, "turnover": 1e6,
            "update_time": f"{today} {ut}", "sec_status": status,
        })
    return pd.DataFrame(rows)


class _FakeCtx:
    def __init__(self, df, ret=0):
        self._df, self._ret = df, ret
        self.closed = False

    def get_market_snapshot(self, codes):
        if self._ret != 0:
            return self._ret, "snapshot error"
        return 0, self._df[self._df["code"].isin(codes)]

    def close(self):
        self.closed = True


def _factory(df, ret=0):
    return lambda: _FakeCtx(df, ret)


@pytest.fixture(autouse=True)
def _out_dir(tmp_path, monkeypatch):
    d = tmp_path / "hk_us_linkage"
    monkeypatch.setattr(mod, "OUT_DIR", d)
    return d


# ── 涨跌幅口径 ──────────────────────────────────────────────
def test_chg_pct_basic():
    assert mod.chg_pct(103.0, 100.0) == 3.0
    assert mod.chg_pct(97.0, 100.0) == -3.0


def test_chg_pct_unknown_not_zero():
    assert mod.chg_pct(None, 100.0) is None
    assert mod.chg_pct(100.0, None) is None
    assert mod.chg_pct(100.0, 0.0) is None
    assert mod.chg_pct(100.0, -1.0) is None
    assert mod.chg_pct(0.0, 100.0) is None


def test_chg_pct_bool_rejected():
    assert mod.chg_pct(True, 100.0) is None
    assert mod.chg_pct(100.0, False) is None


# ── 行业聚合 ──────────────────────────────────────────────
def test_aggregate_equal_weight_and_breadth():
    rows = {"A": {"chg_pct": 2.0, "turnover": 10.0},
            "B": {"chg_pct": -1.0, "turnover": 20.0},
            "C": {"chg_pct": 0.01, "turnover": 5.0}}   # 0.01 < 0.05 平盘带 → flat
    agg = mod.aggregate_sector(rows)
    assert agg["chg_pct_eq"] == round((2.0 - 1.0 + 0.01) / 3, 4)
    assert agg["breadth"] == {"up": 1, "flat": 1, "down": 1}
    assert agg["turnover_hkd"] == 35.0
    assert agg["n_available"] == 3 and agg["n_missing"] == 0


def test_aggregate_missing_not_zero():
    agg = mod.aggregate_sector({"A": {"chg_pct": None}, "B": {"chg_pct": 4.0}})
    assert agg["chg_pct_eq"] == 4.0
    assert agg["n_missing"] == 1 and agg["missing"] == ["A"]


def test_aggregate_all_missing_none():
    agg = mod.aggregate_sector({"A": {"chg_pct": None}})
    assert agg["chg_pct_eq"] is None and agg["n_available"] == 0
    assert agg["dispersion_pp"] is None


def test_aggregate_dispersion():
    agg = mod.aggregate_sector({"A": {"chg_pct": 5.0}, "B": {"chg_pct": -3.0}})
    assert agg["dispersion_pp"] == 8.0


# ── 时段闸 ──────────────────────────────────────────────
@pytest.mark.parametrize("hhmm,expect", [
    ((9, 30), "morning"), ((11, 52), "morning"),
    ((12, 2), "morning_close"), ((13, 7), "afternoon"),
    ((15, 52), "afternoon"), ((16, 2), "close"),
    ((9, 7), None), ((9, 22), None), ((12, 30), None),
    ((16, 30), None), ((8, 0), None),
])
def test_session_for(hhmm, expect):
    dt = datetime(2026, 11, 30, hhmm[0], hhmm[1], tzinfo=CST)
    assert mod.session_for(dt) == expect


def test_session_weekend_none():
    sat = datetime(2026, 11, 28, 10, 7, tzinfo=CST)
    assert mod.session_for(sat) is None


# ── stale 闸 ──────────────────────────────────────────────
def test_stale_yesterday_update():
    tick = mod.build_tick_row("t", "morning", {r["code"]: r for r in _df(today="2026-11-29").to_dict("records")})
    assert mod.is_stale_tick(tick, TODAY) is True


def test_stale_suspended_index():
    tick = mod.build_tick_row("t", "morning", {r["code"]: r for r in _df(status="SUSPENDED").to_dict("records")})
    assert mod.is_stale_tick(tick, TODAY) is True


def test_stale_no_indices():
    assert mod.is_stale_tick({"indices": {}}, TODAY) is True


def test_not_stale_fresh():
    tick = mod.build_tick_row("t", "morning", {r["code"]: r for r in _df().to_dict("records")})
    assert mod.is_stale_tick(tick, TODAY) is False


# ── 翻红/翻绿事件 ──────────────────────────────────────────
def test_events_sign_cross_up():
    series = [{"ts": "t1", "chg_pct": -1.0}, {"ts": "t2", "chg_pct": 0.5}]
    ev = mod.derive_events("sector:x", series)
    assert len(ev) == 1 and ev[0]["kind"] == "sign_cross_up"
    assert ev[0]["from_pct"] == -1.0 and ev[0]["to_pct"] == 0.5


def test_events_sign_cross_down():
    series = [{"ts": "t1", "chg_pct": 0.5}, {"ts": "t2", "chg_pct": -0.5}]
    ev = mod.derive_events("index:y", series)
    assert ev[0]["kind"] == "sign_cross_down"


def test_events_monotonic_none():
    series = [{"ts": "t1", "chg_pct": 1.0}, {"ts": "t2", "chg_pct": 2.0}]
    assert mod.derive_events("sector:x", series) == []


# ── day doc 幂等 + 量纲 ──────────────────────────────────────
def _tick_rows():
    r1 = {r["code"]: r for r in _df({"HK.800700": -1.0}, ut="10:07:00").to_dict("records")}
    r2 = {r["code"]: r for r in _df({"HK.800700": 2.0}, ut="10:22:00").to_dict("records")}
    return [mod.build_tick_row(f"{TODAY}T10:07:00+08:00", "morning", r1),
            mod.build_tick_row(f"{TODAY}T10:22:00+08:00", "morning", r2)]


def test_day_doc_idempotent_recompute():
    rows = _tick_rows()
    a = mod.derive_day_doc(rows, TODAY)
    b = mod.derive_day_doc(rows, TODAY)
    assert a == b
    assert a["ticks"] == 2
    assert a["indices"]["HK.800700"]["first_obs_chg_pct"] == -1.0   # B3: 首观测语义命名
    assert a["indices"]["HK.800700"]["latest_chg_pct"] == 2.0
    assert any(e["kind"] == "sign_cross_up" for e in a["events"])


def test_day_doc_units_declared():
    doc = mod.derive_day_doc(_tick_rows(), TODAY)
    assert doc["units"]["chg_pct"] == "percent"
    assert doc["units"]["turnover_hkd"] == "HKD_absolute"
    assert doc["units"]["dispersion_pp"] == "percent_points"
    assert doc["schema_version"] == mod.SCHEMA_VERSION


# ── record_tick 编排 ──────────────────────────────────────────
def test_record_dry_run_zero_write(_out_dir):
    res = mod.record_tick(now=MON_1007, dry_run=True, ctx_factory=_factory(_df()), out_dir=_out_dir)
    assert res["status"] == "dry_run"
    assert not _out_dir.exists()


def test_record_out_of_session_skip(_out_dir):
    pre = datetime(2026, 11, 30, 9, 7, tzinfo=CST)
    res = mod.record_tick(now=pre, ctx_factory=_factory(_df()), out_dir=_out_dir)
    assert res["status"] == "skipped_session"
    assert not _out_dir.exists()


def test_record_stale_holiday_skip(_out_dir):
    res = mod.record_tick(now=MON_1007, ctx_factory=_factory(_df(today="2026-11-27")), out_dir=_out_dir)
    assert res["status"] == "skipped_stale"
    assert not _out_dir.exists()


def test_record_written_schema(_out_dir):
    res = mod.record_tick(now=MON_1007, ctx_factory=_factory(_df()), out_dir=_out_dir)
    assert res["status"] == "written" and res["ticks"] == 1
    doc = json.loads((_out_dir / f"{TODAY}.json").read_text())
    assert doc["ticks"] == 1 and doc["trading_day"] is True
    assert "HK.800700" in doc["indices"] and "tech_internet" in doc["sectors"]
    assert isinstance(doc["tick_rows"], list) and len(doc["tick_rows"]) == 1


def test_record_dedup_same_minute(_out_dir):
    mod.record_tick(now=MON_1007, ctx_factory=_factory(_df()), out_dir=_out_dir)
    again = datetime(2026, 11, 30, 10, 7, 40, tzinfo=CST)
    res = mod.record_tick(now=again, ctx_factory=_factory(_df()), out_dir=_out_dir)
    assert res["status"] == "skipped_dedup"
    assert json.loads((_out_dir / f"{TODAY}.json").read_text())["ticks"] == 1


def test_record_cap(_out_dir, monkeypatch):
    monkeypatch.setattr(mod, "MAX_TICK_ROWS", 2)
    for mm in (7, 22, 37):
        mod.record_tick(now=datetime(2026, 11, 30, 10, mm, tzinfo=CST),
                        ctx_factory=_factory(_df()), out_dir=_out_dir)
    doc = json.loads((_out_dir / f"{TODAY}.json").read_text())
    assert doc["ticks"] == 2


def test_record_force_verify_subdir(_out_dir):
    sat = datetime(2026, 11, 28, 22, 0, tzinfo=CST)   # 周末窗外
    res = mod.record_tick(now=sat, force=True, ctx_factory=_factory(_df(today="2026-11-28")), out_dir=_out_dir)
    assert res["status"] == "written"
    assert (_out_dir / "_verify" / "2026-11-28.json").exists()
    assert not (_out_dir / "2026-11-28.json").exists()   # 主目录零污染


def test_record_opend_failure_failsafe(_out_dir):
    def _boom():
        raise ConnectionError("OpenD down")
    res = mod.record_tick(now=MON_1007, ctx_factory=_boom, out_dir=_out_dir)
    assert res["status"] == "error"
    assert not _out_dir.exists()


def test_record_snapshot_ret_error(_out_dir):
    res = mod.record_tick(now=MON_1007, ctx_factory=_factory(_df(), ret=-1), out_dir=_out_dir)
    assert res["status"] == "error"
    assert not _out_dir.exists()


def test_record_partial_basket_missing(_out_dir):
    res = mod.record_tick(now=MON_1007, ctx_factory=_factory(_df(drop=("HK.00981",))), out_dir=_out_dir)
    assert res["status"] == "written"
    doc = json.loads((_out_dir / f"{TODAY}.json").read_text())
    assert doc["sectors"]["semis"]["latest"]["n_missing"] == 1


def test_record_ctx_closed(_out_dir):
    ctx = _FakeCtx(_df())
    mod.record_tick(now=MON_1007, ctx_factory=lambda: ctx, out_dir=_out_dir)
    assert ctx.closed is True


# ── 并发原子性 ──────────────────────────────────────────────
def test_concurrent_writers_no_lost_tick(_out_dir):
    minutes = list(range(0, 20))
    def _one(mm):
        now = datetime(2026, 11, 30, 10, mm, tzinfo=CST)
        return mod.record_tick(now=now, ctx_factory=_factory(_df()), out_dir=_out_dir)
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_one, minutes))
    assert all(r["status"] == "written" for r in results)
    doc = json.loads((_out_dir / f"{TODAY}.json").read_text())
    assert doc["ticks"] == 20


def test_atomic_no_tmp_leftover(_out_dir):
    mod.record_tick(now=MON_1007, ctx_factory=_factory(_df()), out_dir=_out_dir)
    assert list(_out_dir.glob("*.tmp")) == []


# ── ADR parity + OTC ──────────────────────────────────────────
def test_adr_mapping_parity_hkex_source():
    from app.catalyst.sources.hkex_source import HK_STOCK_MAPPING
    assert mod.HK_ADR_MAPPING == HK_STOCK_MAPPING


def test_adr_otc_status():
    doc = mod.derive_day_doc(_tick_rows(), TODAY)
    assert doc["adr_hk_legs"]["00700"]["us_quote_status"] == "OTC_UNAVAILABLE"
    assert doc["adr_hk_legs"]["09988"]["us_quote_status"] == "EXCHANGE_LISTED_DEFERRED"


# ── 注册表条目 ──────────────────────────────────────────────
def test_registry_entry_contract():
    import app.playbook.shadow_registry as registry
    item = next(s for s in registry.REGISTRY if s.id == "hk_us_linkage_context_shadow")
    assert item.data_path == "data/shadow/hk_us_linkage"
    assert item.config_key is None
    assert item.min_n == 60
    assert "USER_ADJUDICATION" in item.no_adjudication_reason


def test_registry_counts_days_not_ticks(tmp_path):
    import app.playbook.shadow_registry as registry
    item = next(s for s in registry.REGISTRY if s.id == "hk_us_linkage_context_shadow")
    d = tmp_path / item.data_path
    d.mkdir(parents=True, exist_ok=True)
    for day in ("2026-11-24", "2026-11-25"):
        (d / f"{day}.json").write_text(json.dumps(mod.derive_day_doc(_tick_rows(), day)))
    verify = d / "_verify"
    verify.mkdir()
    (verify / "2026-11-27.json").write_text(json.dumps(mod.derive_day_doc(_tick_rows(), "2026-11-27")))
    assert registry._count_samples(replace(item), tmp_path) == 2


# ── 静态零耦合 + 导入零副作用 ──────────────────────────────────
def test_no_trading_or_push_coupling():
    src = Path(mod.__file__).read_text(encoding="utf-8")
    for token in ("place_order", "submit_order", "DualBrokerAdapter", "push_text", "notifier", "webhook"):
        assert token not in src, token


def test_import_side_effect_free(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    importlib.reload(mod)
    assert not (tmp_path / "data" / "shadow" / "hk_us_linkage").exists()


def test_main_exit_zero_on_error(monkeypatch, capsys):
    monkeypatch.setattr(mod, "record_tick", lambda **kw: {"status": "error"})
    assert mod.main([]) == 0
