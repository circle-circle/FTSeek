"""采集闸契约测试 (2026-09-21): "无效就停"必须**显式**、且不能从判据状态反推。

背景: 四路富途影子采集原先只有总闸 + 三条零散子开关, capital_flow 连独立开关都没有。
复核时发现两个方向都踩过坑:
  · iv_hv / 异动榜: 判据已出结论却继续花 IO;
  · capital_flow: "判据 FAIL" 被误当成 "数据没用" ⇒ 差点停掉**有生产消费者**的料
    (喂 app/playbook/intraday_reentry.py 盘中资金背离门, 停采 = 门 fail-open 退化)。
本测试把三条语义钉死。
"""
import importlib.util
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "fcs_collect_under_test", REPO / "scripts" / "futu_channel_shadow_collect.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Cfg:
    def __init__(self, vals):
        self.vals = vals

    def get_dotted(self, key, default=None):
        return self.vals.get(key, default)


def _patch_cfg(monkeypatch, vals):
    import app.core.config_container as cc
    monkeypatch.setattr(cc, "get_config", lambda: _Cfg(vals))


def test_gate_blocks_when_collection_retired(monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_registry_entry",
                        lambda cid: types.SimpleNamespace(collection_retired=True))
    assert m._collector_gate("futu_channel_iv_hv",
                             "futu_channels.iv_collect_enabled") is False


def test_gate_blocks_when_switch_off(monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_registry_entry",
                        lambda cid: types.SimpleNamespace(collection_retired=False))
    _patch_cfg(monkeypatch, {"futu_channels.iv_collect_enabled": False})
    assert m._collector_gate("futu_channel_iv_hv",
                             "futu_channels.iv_collect_enabled") is False


def test_gate_allows_when_default_on_and_not_retired(monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_registry_entry",
                        lambda cid: types.SimpleNamespace(collection_retired=False))
    _patch_cfg(monkeypatch, {})
    assert m._collector_gate("futu_channel_ofi_baseline",
                             "futu_channels.ofi_collect_enabled") is True


def test_terminal_status_alone_must_not_stop_collection(monkeypatch):
    """⚠️ 反推禁令: 登记状态 KILLED (= 研究判据退役) **不等于**采集该停。

    实锤: capital_flow 的原料在喂生产门; 若按状态自动停采 → 静默断料。
    """
    m = _load()
    monkeypatch.setattr(
        m, "_registry_entry",
        lambda cid: types.SimpleNamespace(collection_retired=False, status="KILLED"))
    _patch_cfg(monkeypatch, {})
    assert m._collector_gate("futu_channel_capital_flow",
                             "futu_channels.capital_flow_collect_enabled") is True


def test_run_collect_applies_gate_per_channel(monkeypatch):
    """端到端: run_collect 只调用过闸的采集函数 (停采的通道一次都不碰 OpenD)。"""
    m = _load()
    called = []
    monkeypatch.setattr(m, "_enabled", lambda: True)
    monkeypatch.setattr(m, "_load_state", lambda: {})
    monkeypatch.setattr(m, "_save_state", lambda st: None)
    monkeypatch.setattr(m, "_interval_sec", lambda: 300)
    monkeypatch.setattr(m, "_active_symbols", lambda sch: ["US.AAPL"])
    monkeypatch.setattr(m, "_connect_ctx", lambda: types.SimpleNamespace(close=lambda: None))
    # 本测试只验证按通道开关过闸; 登记集合已随退役批变化 (top_movers/iv_hv 停采),
    # 由上方三条注册表语义测试单独覆盖, 故此处桩掉登记闸。
    def _gate_by_switch(cid, key, default=True):
        from app.core.config_container import get_config
        return bool(get_config().get_dotted(key, default))
    monkeypatch.setattr(m, "_collector_gate", _gate_by_switch)
    monkeypatch.setattr(m, "_collect_capital_flow", lambda ctx, s: called.append("flow") or 1)
    monkeypatch.setattr(m, "_collect_ofi_baseline", lambda ctx, s, st, phase: called.append("ofi") or 1)
    monkeypatch.setattr(m, "_collect_iv_hv", lambda ctx, s: called.append("iv") or 1)
    monkeypatch.setattr(m, "_collect_top_movers", lambda ctx: called.append("movers") or 1)
    # 只留 capital_flow 开, 其余三路停采
    _patch_cfg(monkeypatch, {
        "futu_channels.capital_flow_collect_enabled": True,
        "futu_channels.ofi_collect_enabled": False,
        "futu_channels.iv_collect_enabled": False,
        "futu_channels.movers_shadow_enabled": False,
    })
    m.run_collect(phase="TRADING")
    assert called == ["flow"], f"只应调 capital_flow, 实际 {called}"

    # 全开时四路都调 (确认闸没把该采的也拦了)
    called.clear()
    _patch_cfg(monkeypatch, {
        "futu_channels.capital_flow_collect_enabled": True,
        "futu_channels.ofi_collect_enabled": True,
        "futu_channels.iv_collect_enabled": True,
        "futu_channels.movers_shadow_enabled": True,
    })
    m.run_collect(phase="TRADING")
    assert called == ["flow", "ofi", "movers"], f"TRADING 段应调 flow/ofi/movers, 实际 {called}"
    called.clear()
    m.run_collect(phase="AFTER_HOURS")
    assert called == ["flow", "ofi", "iv"], f"AFTER_HOURS 段应调 flow/ofi/iv, 实际 {called}"

# ═════════════════════════════════════════════════════════════════════════════
# OFI 采集器契约测试 (本批): 时间口径 / 采样 gap / 状态推进 / 质量报表 / CLI
#
# 被测契约 (scripts/futu_channel_shadow_collect.py::_collect_ofi_baseline + _ofi_timing):
#   · ingested_at_utc = **API 返回后**捕获的 datetime.now(timezone.utc), 微秒精度;
#   · request_duration_ms = monotonic_ns 换算毫秒; ts_et 由接收时刻派生, 秒精度;
#   · 源快照时间 (SDK update_time, ET) 解析为 snapshot_update_time_et/age:
#     MISSING / INVALID / FUTURE(age<-2s) / STALE(age>2*interval) / RECENT;
#   · sample_gap 只在同 symbol 且同 ET 日 TRADING→TRADING 间比较:
#     FIRST_SAMPLE / OFF_SESSION / SESSION_RESET / OK / GAP / CLOCK_ROLLBACK /
#     INVALID_PREVIOUS; 状态只在**落盘成功后**推进 (run_collect 负责 _save_state);
#   · 非有限报价过滤; 零量是真实观测, 照收不伪造。
# 全程 mock ctx + tmp_path, 不连 OpenD/不写生产 data。
# ═════════════════════════════════════════════════════════════════════════════
import json
import math
import re
import socket
import sys
import time as _time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.utils.timezone import ET as _ET

UTC = timezone.utc
# 固定基准时刻: 2026-10-15 15:00 UTC = ET 周四 11:00 (EDT)
_ING = datetime(2026, 10, 15, 15, 0, 0, tzinfo=UTC)


def _load_eval():
    spec = importlib.util.spec_from_file_location(
        "fcs_eval_under_test", REPO / "scripts" / "futu_channel_shadow_eval.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fixed_clock(monkeypatch, m, when_utc):
    """把采集模块的 datetime.now 钉在固定时刻 (strptime/fromisoformat 走继承, 不受影响)."""
    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when_utc
    monkeypatch.setattr(m, "datetime", _FixedDatetime)


def _snap_row(code="US.AAPL", bid=100.0, ask=100.1, bid_vol=10.0, ask_vol=10.0,
              last=100.05, update_time=None):
    return {"code": code, "bid_price": bid, "ask_price": ask,
            "bid_vol": bid_vol, "ask_vol": ask_vol,
            "last_price": last, "update_time": update_time}


def _snap_df(rows):
    return pd.DataFrame(rows)


class _Ctx:
    """市场快照桩: 只记录调用, 可注入延迟/失败/任意 DataFrame, 永不触网。"""

    def __init__(self, df=None, ret=0, delay_sec=0.0):
        self.df, self.ret, self.delay_sec = df, ret, delay_sec
        self.calls = []

    def get_market_snapshot(self, symbols):
        self.calls.append(list(symbols))
        if self.delay_sec:
            _time.sleep(self.delay_sec)
        return self.ret, self.df


def _patch_ofi_paths(monkeypatch, m, tmp_path):
    out = tmp_path / "ofi.jsonl"
    state = tmp_path / "state.json"
    monkeypatch.setattr(m, "_OFI_OUT", out)
    monkeypatch.setattr(m, "_STATE", state)
    return out, state


def _read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _ofi_timing(m, update_time, ingested_at, previous=None, phase="TRADING", interval=300):
    return m._ofi_timing({"update_time": update_time}, ingested_at,
                         previous if previous is not None else {}, phase, interval)


def _prev(ingested_at, phase="TRADING"):
    return {"ingested_at_utc": ingested_at.isoformat(), "phase": phase}


# ── 源快照时间 (SDK ET update_time) ──────────────────────────────────────────

def test_ofi_source_recent_and_age_in_seconds():
    m = _load()
    r = _ofi_timing(m, "2026-10-15 10:59:55", _ING)   # ET 比接收时刻早 5s
    assert r["snapshot_time_status"] == "RECENT"
    assert r["snapshot_update_time_et"] == "2026-10-15 10:59:55"
    assert 4.0 < r["snapshot_age_sec"] < 6.0           # 秒量纲
    assert r["timing_threshold_sec"] == 600            # 2*interval


def test_ofi_source_missing():
    m = _load()
    for missing in (None, ""):
        r = _ofi_timing(m, missing, _ING)
        assert r["snapshot_time_status"] == "MISSING"
        assert r["snapshot_age_sec"] is None
        assert r["snapshot_update_time_et"] in (None, "")


def test_ofi_source_invalid():
    m = _load()
    for bad in ("2026-10-15 10:59:60", "not-a-time", "2026-10-15T10:59:55", 12345):
        r = _ofi_timing(m, bad, _ING)
        assert r["snapshot_time_status"] == "INVALID", bad
        assert r["snapshot_age_sec"] is None


def test_ofi_source_future_boundary_2s():
    m = _load()
    # age=-2 恰在阈值内 (FUTURE 是严格小于 -2)
    assert _ofi_timing(m, "2026-10-15 11:00:02", _ING)["snapshot_time_status"] == "RECENT"
    r = _ofi_timing(m, "2026-10-15 11:00:03", _ING)    # age=-3
    assert r["snapshot_time_status"] == "FUTURE"
    assert r["snapshot_age_sec"] == -3.0


def test_ofi_source_stale_boundary_2x_interval():
    m = _load()
    # age=600 (2*interval) 恰在阈值内, 601 → STALE
    assert _ofi_timing(m, "2026-10-15 10:50:00", _ING)["snapshot_time_status"] == "RECENT"
    r = _ofi_timing(m, "2026-10-15 10:49:59", _ING)
    assert r["snapshot_time_status"] == "STALE"
    assert 600.0 < r["snapshot_age_sec"] < 602.0


# ── 采样 gap (同 symbol 同 ET 日 TRADING→TRADING) ────────────────────────────

def test_ofi_gap_first_sample():
    m = _load()
    r = _ofi_timing(m, "2026-10-15 10:59:55", _ING)
    assert r["sample_gap_status"] == "FIRST_SAMPLE"
    assert r["sample_gap_sec"] is None


def test_ofi_gap_ok_and_gap_boundary():
    m = _load()
    for delta, status, gap in ((300, "OK", 300.0), (600, "OK", 600.0), (601, "GAP", 601.0)):
        r = _ofi_timing(m, "2026-10-15 10:59:55", _ING,
                        _prev(_ING - timedelta(seconds=delta)))
        assert r["sample_gap_status"] == status, delta
        assert r["sample_gap_sec"] == gap


def test_ofi_gap_off_session_not_judged_by_rth():
    m = _load()
    # 盘后: 即便同日间隔 2h (RTH 口径必 GAP), 也只报 OFF_SESSION 且不算 gap 秒数
    r = _ofi_timing(m, "2026-10-15 10:59:55", _ING,
                    _prev(_ING - timedelta(seconds=7200)), phase="AFTER_HOURS")
    assert r["sample_gap_status"] == "OFF_SESSION"
    assert r["sample_gap_sec"] is None
    # 前一次也是 AFTER_HOURS 同日 → 同样 OFF_SESSION (phase 分支先行)
    r2 = _ofi_timing(m, "2026-10-15 10:59:55", _ING,
                     _prev(_ING - timedelta(seconds=300), phase="AFTER_HOURS"),
                     phase="AFTER_HOURS")
    assert r2["sample_gap_status"] == "OFF_SESSION"


def test_ofi_gap_overnight_and_weekend_session_reset():
    m = _load()
    # 隔夜跨日: 前一 ET 日 → SESSION_RESET 而非 GAP
    r = _ofi_timing(m, "2026-10-15 10:59:55", _ING, _prev(_ING - timedelta(hours=24)))
    assert r["sample_gap_status"] == "SESSION_RESET"
    assert r["sample_gap_sec"] is None
    # 周五收盘 → 周一开盘 (跨 63h) 不按断档误报
    friday = datetime(2026, 10, 9, 19, 0, 0, tzinfo=UTC)    # ET 周五 15:00
    monday = datetime(2026, 10, 12, 13, 31, 0, tzinfo=UTC)  # ET 周一 09:31
    r2 = _ofi_timing(m, "2026-10-12 09:30:55", monday, _prev(friday))
    assert r2["sample_gap_status"] == "SESSION_RESET"
    assert r2["sample_gap_sec"] is None


def test_ofi_gap_session_reset_when_previous_phase_not_trading():
    m = _load()
    # 同 ET 日但前一次非 TRADING → SESSION_RESET (只做 TRADING 对 TRADING 比较)
    r = _ofi_timing(m, "2026-10-15 10:59:55", _ING,
                    _prev(_ING - timedelta(seconds=300), phase="AFTER_HOURS"))
    assert r["sample_gap_status"] == "SESSION_RESET"
    assert r["sample_gap_sec"] is None


def test_ofi_gap_clock_rollback():
    m = _load()
    r = _ofi_timing(m, "2026-10-15 10:59:55", _ING, _prev(_ING + timedelta(seconds=60)))
    assert r["sample_gap_status"] == "CLOCK_ROLLBACK"
    assert r["sample_gap_sec"] == -60.0


def test_ofi_gap_invalid_previous_states():
    m = _load()
    bad_prevs = [
        {"phase": "TRADING"},                                       # 缺 ingested_at_utc
        {"ingested_at_utc": "2026-10-15T14:55:00", "phase": "TRADING"},  # naive 无时区
        {"ingested_at_utc": "not-a-time", "phase": "TRADING"},       # 不可解析
    ]
    for p in bad_prevs:
        r = _ofi_timing(m, "2026-10-15 10:59:55", _ING, p)
        assert r["sample_gap_status"] == "INVALID_PREVIOUS", p
        assert r["sample_gap_sec"] is None


# ── 全链路 _collect_ofi_baseline ─────────────────────────────────────────────

def test_collect_ofi_captures_now_after_slow_return(tmp_path, monkeypatch):
    m = _load()
    _patch_cfg(monkeypatch, {})
    out, _ = _patch_ofi_paths(monkeypatch, m, tmp_path)
    ctx = _Ctx(df=_snap_df([_snap_row()]), delay_sec=0.05)   # 模拟慢请求
    t_before = datetime.now(UTC)
    assert m._collect_ofi_baseline(ctx, ["US.AAPL"], {}, "TRADING") == 1
    t_after = datetime.now(UTC)
    (row,) = _read_rows(out)
    ing = datetime.fromisoformat(row["ingested_at_utc"])
    # now 在 API 返回**之后**才捕获: 至少晚于进入前 50ms 的模拟慢请求
    assert ing >= t_before + timedelta(seconds=0.03)
    assert ing <= t_after
    assert 30.0 <= row["request_duration_ms"] < 5_000.0      # 毫秒量纲且覆盖请求时长
    assert row["ts_et"] == ing.astimezone(_ET).strftime("%Y-%m-%d %H:%M:%S")


def test_collect_ofi_same_batch_shares_one_timestamp(tmp_path, monkeypatch):
    m = _load()
    _patch_cfg(monkeypatch, {})
    out, _ = _patch_ofi_paths(monkeypatch, m, tmp_path)
    df = _snap_df([_snap_row("US.AAPL", update_time="2026-10-15 10:59:55"),
                   _snap_row("US.MSFT", update_time="2026-10-15 10:59:55")])
    assert m._collect_ofi_baseline(_Ctx(df=df), ["US.AAPL", "US.MSFT"], {}, "TRADING") == 2
    r_a, r_m = _read_rows(out)
    assert r_a["ingested_at_utc"] == r_m["ingested_at_utc"]   # 同批同接收时刻
    assert r_a["ts_et"] == r_m["ts_et"]
    assert r_a["request_duration_ms"] == r_m["request_duration_ms"]


@pytest.mark.parametrize("fixed_utc,expected_ts_et,expected_update", [
    (datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC),
     "2026-08-15 08:00:00", "2026-08-15 07:59:55"),   # 夏令时 EDT = UTC-4
    (datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
     "2026-01-15 07:00:00", "2026-01-15 06:59:55"),   # 冬令时 EST = UTC-5
])
def test_collect_ofi_ts_et_dst_and_precision(tmp_path, monkeypatch,
                                             fixed_utc, expected_ts_et, expected_update):
    m = _load()
    _patch_cfg(monkeypatch, {})
    out, _ = _patch_ofi_paths(monkeypatch, m, tmp_path)
    _fixed_clock(monkeypatch, m, fixed_utc)
    ctx = _Ctx(df=_snap_df([_snap_row(update_time=expected_update)]))
    assert m._collect_ofi_baseline(ctx, ["US.AAPL"], {}, "TRADING") == 1
    (row,) = _read_rows(out)
    assert row["ts_et"] == expected_ts_et
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["ts_et"])  # 秒精度, 无毫秒
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00",
                        row["ingested_at_utc"])                                 # UTC + 微秒
    # age=5s: 若 DST 换算错 1h, 这里会变 STALE 而非 RECENT
    assert row["snapshot_time_status"] == "RECENT"
    assert 4.0 < row["snapshot_age_sec"] < 6.0


def test_collect_ofi_zero_qty_kept_nonfinite_dropped(tmp_path, monkeypatch):
    m = _load()
    _patch_cfg(monkeypatch, {})
    out, _ = _patch_ofi_paths(monkeypatch, m, tmp_path)
    df = _snap_df([
        _snap_row("US.ZEROQ", bid_vol=0.0),           # 零量 = 真实观测
        _snap_row("US.NORMAL"),
        _snap_row("US.NANPX", bid=float("nan")),      # 非有限价 → 整行丢弃
        _snap_row("US.INFPX", ask=float("inf")),
        _snap_row("US.ZEROPX", bid=0.0),              # 非正价 → 丢弃
        _snap_row("US.NANLAST", last=float("nan")),   # 任一字段非有限 → 整行丢弃
    ])
    syms = ["US.ZEROQ", "US.NORMAL", "US.NANPX", "US.INFPX", "US.ZEROPX", "US.NANLAST"]
    n = m._collect_ofi_baseline(_Ctx(df=df), syms, {}, "TRADING")
    rows = _read_rows(out)
    assert n == 2
    assert {r["symbol"] for r in rows} == {"US.ZEROQ", "US.NORMAL"}
    zq = next(r for r in rows if r["symbol"] == "US.ZEROQ")
    assert zq["bid1_qty"] == 0.0                       # 零量照收, 不伪造
    for r in rows:                                     # 落盘行全有限
        assert all(math.isfinite(r[k]) for k in
                   ("bid1", "ask1", "bid1_qty", "ask1_qty", "last"))


def test_collect_ofi_switch_off_makes_no_request(tmp_path, monkeypatch):
    m = _load()
    _patch_cfg(monkeypatch, {"futu_channels.ofi_collect_enabled": False})
    out, _ = _patch_ofi_paths(monkeypatch, m, tmp_path)
    ctx = _Ctx(df=_snap_df([_snap_row()]))
    assert m._collect_ofi_baseline(ctx, ["US.AAPL"], {}, "TRADING") == 0
    assert ctx.calls == []                              # 开关关 = 一次都不碰 API
    assert not out.exists()


def test_collect_ofi_request_failure_keeps_state(tmp_path, monkeypatch):
    m = _load()
    _patch_cfg(monkeypatch, {})
    out, _ = _patch_ofi_paths(monkeypatch, m, tmp_path)
    state = {"last_collect_ts": 123}
    ctx = _Ctx(df=_snap_df([_snap_row()]), ret=-1)     # API 失败
    assert m._collect_ofi_baseline(ctx, ["US.AAPL"], state, "TRADING") == 0
    assert state == {"last_collect_ts": 123}            # 失败不推进
    assert not out.exists()
    # 失败不制造 previous: 后续成功批仍按 FIRST_SAMPLE 起算
    ok = _Ctx(df=_snap_df([_snap_row()]))
    assert m._collect_ofi_baseline(ok, ["US.AAPL"], state, "TRADING") == 1
    assert _read_rows(out)[0]["sample_gap_status"] == "FIRST_SAMPLE"


def test_collect_ofi_write_failure_does_not_advance_state(tmp_path, monkeypatch):
    m = _load()
    _patch_cfg(monkeypatch, {})
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")                    # 落盘路径父目录损坏
    monkeypatch.setattr(m, "_OFI_OUT", blocker / "ofi.jsonl")
    state = {}
    with pytest.raises(OSError):
        m._collect_ofi_baseline(_Ctx(df=_snap_df([_snap_row()])), ["US.AAPL"], state, "TRADING")
    assert "ofi_last_ingested" not in state             # 落盘失败 ⇒ 状态不推进
    assert not (blocker / "ofi.jsonl").exists()


def test_collect_ofi_gap_isolated_per_symbol(tmp_path, monkeypatch):
    m = _load()
    _patch_cfg(monkeypatch, {})
    out, _ = _patch_ofi_paths(monkeypatch, m, tmp_path)
    now = datetime(2026, 10, 15, 15, 0, 0, tzinfo=UTC)
    _fixed_clock(monkeypatch, m, now)
    state = {"ofi_last_ingested": {"US.AAPL": {
        "ingested_at_utc": (now - timedelta(seconds=300)).isoformat(),
        "date_et": "2026-10-15", "phase": "TRADING"}}}
    df = _snap_df([_snap_row("US.AAPL", update_time="2026-10-15 10:59:55"),
                   _snap_row("US.MSFT", update_time="2026-10-15 10:59:55")])
    m._collect_ofi_baseline(_Ctx(df=df), ["US.AAPL", "US.MSFT"], state, "TRADING")
    rows = {r["symbol"]: r for r in _read_rows(out)}
    assert rows["US.AAPL"]["sample_gap_status"] == "OK"
    assert rows["US.AAPL"]["sample_gap_sec"] == 300.0
    assert rows["US.MSFT"]["sample_gap_status"] == "FIRST_SAMPLE"   # 不同 symbol 不互串


def test_collect_ofi_state_keeps_only_same_et_date(tmp_path, monkeypatch):
    m = _load()
    _patch_cfg(monkeypatch, {})
    out, _ = _patch_ofi_paths(monkeypatch, m, tmp_path)
    now = datetime(2026, 10, 15, 15, 0, 0, tzinfo=UTC)   # ET 2026-10-15 周四
    _fixed_clock(monkeypatch, m, now)
    state = {"ofi_last_ingested": {
        "US.OLD": {"ingested_at_utc": (now - timedelta(days=1)).isoformat(),
                   "date_et": "2026-10-14", "phase": "TRADING"},
        "US.AAPL": {"ingested_at_utc": (now - timedelta(seconds=300)).isoformat(),
                    "date_et": "2026-10-15", "phase": "TRADING"}}}
    df = _snap_df([_snap_row("US.AAPL"), _snap_row("US.NEW")])
    m._collect_ofi_baseline(_Ctx(df=df), ["US.AAPL", "US.NEW"], state, "TRADING")
    st = state["ofi_last_ingested"]
    assert set(st) == {"US.AAPL", "US.NEW"}              # 跨日旧条目被裁, 不把隔夜当断档
    assert st["US.NEW"]["date_et"] == "2026-10-15"
    assert st["US.AAPL"]["ingested_at_utc"] == now.isoformat(timespec="microseconds")
    rows = {r["symbol"]: r for r in _read_rows(out)}
    assert rows["US.NEW"]["sample_gap_status"] == "FIRST_SAMPLE"


def test_collect_ofi_state_save_load_and_gap_across_restart(tmp_path, monkeypatch):
    t1 = datetime(2026, 10, 15, 15, 0, 0, tzinfo=UTC)
    t2 = t1 + timedelta(seconds=3600)
    m1 = _load()
    _patch_cfg(monkeypatch, {})
    out, state_path = _patch_ofi_paths(monkeypatch, m1, tmp_path)
    _fixed_clock(monkeypatch, m1, t1)
    st1 = {}
    ctx1 = _Ctx(df=_snap_df([_snap_row(update_time="2026-10-15 10:59:55")]))
    assert m1._collect_ofi_baseline(ctx1, ["US.AAPL"], st1, "TRADING") == 1
    m1._save_state(st1)   # run_collect 负责落盘 (此处直接调用同一函数)

    m2 = _load()          # 重新加载模块 = 进程重启后
    monkeypatch.setattr(m2, "_OFI_OUT", out)
    monkeypatch.setattr(m2, "_STATE", state_path)
    _fixed_clock(monkeypatch, m2, t2)
    st2 = m2._load_state()
    assert st2["ofi_last_ingested"]["US.AAPL"]["ingested_at_utc"] \
        == t1.isoformat(timespec="microseconds")
    ctx2 = _Ctx(df=_snap_df([_snap_row(update_time="2026-10-15 11:59:55")]))
    assert m2._collect_ofi_baseline(ctx2, ["US.AAPL"], st2, "TRADING") == 1
    rows = _read_rows(out)
    assert len(rows) == 2
    assert rows[1]["sample_gap_status"] == "GAP"         # 断档跨重启仍可测
    assert rows[1]["sample_gap_sec"] == 3600.0


# ── 质量报表 ofi_timing_quality + CLI ────────────────────────────────────────

def _write_ofi_file(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def _timed_row(symbol, i, update="2026-10-15 10:59:55", **over):
    r = {"symbol": symbol, "ts_et": f"2026-10-15 10:{i:02d}:00",
         "bid1": 100.0, "bid1_qty": 5.0, "ask1": 100.1, "ask1_qty": 5.0,
         "ingested_at_utc": f"2026-10-15T14:{i:02d}:00.{i:06d}+00:00",
         "snapshot_update_time_et": update,
         "snapshot_age_sec": float(i),
         "request_duration_ms": 12.5 + i,
         "sample_gap_sec": float(i),
         "snapshot_time_status": "RECENT",
         "sample_gap_status": "OK",
         "timing_threshold_sec": 600,
         "collection_phase": "TRADING"}
    r.update(over)
    return r


def test_eval_quality_separates_legacy_from_timed(tmp_path, monkeypatch):
    m = _load_eval()
    ofi = tmp_path / "ofi.jsonl"
    legacy = {"symbol": "US.LEGACY", "ts_et": "2026-10-15 10:00:00",
              "bid1": 100.0, "bid1_qty": 5.0, "ask1": 100.1, "ask1_qty": 5.0}
    _write_ofi_file(ofi, [legacy, _timed_row("US.AAPL", 1), _timed_row("US.AAPL", 2)])
    monkeypatch.setattr(m, "OFI_PATH", ofi)
    q = m.ofi_timing_quality()
    assert q["rows"] == 3
    assert q["legacy_rows"] == 1
    assert q["timed_rows"] == 2
    assert q["status"] == "AVAILABLE"
    # legacy 行不得被算成 RECENT: 实现对 legacy 行直接 continue, 完全不进 status_counts
    assert q["snapshot_time_counts"] == {"RECENT": 2}
    assert "US.LEGACY" not in q["field_cardinality"]


def test_eval_quality_ten_rows_multi_symbol_no_cardinality_alarm(tmp_path, monkeypatch):
    m = _load_eval()
    ofi = tmp_path / "ofi.jsonl"
    _write_ofi_file(ofi, [_timed_row(f"US.S{i % 5}", i) for i in range(10)])
    monkeypatch.setattr(m, "OFI_PATH", ofi)
    q = m.ofi_timing_quality()
    assert q["rows"] == 10
    assert q["warnings"] == []      # 单批 10 行分散在 5 个 symbol, 每只 n=2 < 10, 不误报


def test_eval_quality_constant_source_time_warning_threshold(tmp_path, monkeypatch):
    m = _load_eval()
    ofi9 = tmp_path / "ofi9.jsonl"
    _write_ofi_file(ofi9, [_timed_row("US.AAPL", i) for i in range(9)])
    monkeypatch.setattr(m, "OFI_PATH", ofi9)
    assert m.ofi_timing_quality()["warnings"] == []      # n=9 未达阈值

    ofi10 = tmp_path / "ofi10.jsonl"
    _write_ofi_file(ofi10, [_timed_row("US.AAPL", i) for i in range(10)])
    monkeypatch.setattr(m, "OFI_PATH", ofi10)
    q = m.ofi_timing_quality()
    # 同 symbol 10 个相同源时间 → warning, 且只有该字段 (其余动态字段全相异)
    assert q["warnings"] == ["US.AAPL.snapshot_update_time_et:constant"]


def test_eval_quality_constant_enums_do_not_warn(tmp_path, monkeypatch):
    m = _load_eval()
    ofi = tmp_path / "ofi.jsonl"
    rows = [_timed_row("US.AAPL", i, update=f"2026-10-15 10:59:{i:02d}")
            for i in range(10)]
    _write_ofi_file(ofi, rows)
    monkeypatch.setattr(m, "OFI_PATH", ofi)
    q = m.ofi_timing_quality()
    assert q["warnings"] == []      # 枚举字段恒定是正常态, 不在告警之列
    card = q["field_cardinality"]["US.AAPL"]
    assert card["snapshot_time_status"] == {"n": 10, "distinct": 1, "warning": False}
    assert card["sample_gap_status"]["warning"] is False
    assert card["collection_phase"]["warning"] is False


def test_eval_quality_counts_malformed_rows(tmp_path, monkeypatch):
    m = _load_eval()
    ofi = tmp_path / "ofi.jsonl"
    ofi.write_text("not json\n"
                   '{"no_symbol": 1}\n'
                   + json.dumps(_timed_row("US.AAPL", 1)) + "\n", encoding="utf-8")
    monkeypatch.setattr(m, "OFI_PATH", ofi)
    q = m.ofi_timing_quality()
    assert q["malformed_rows"] == 2
    assert q["rows"] == 1
    assert q["timed_rows"] == 1


def _block_sockets(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("CLI 诊断路径不得创建任何 socket")
    monkeypatch.setattr(socket, "socket", _boom)


def test_cli_ofi_quality_only_readonly_no_network_no_write(tmp_path, monkeypatch, capsys):
    m = _load_eval()
    ofi = tmp_path / "ofi.jsonl"
    _write_ofi_file(ofi, [_timed_row("US.AAPL", i) for i in range(3)])
    out_path = tmp_path / "never_written.json"
    monkeypatch.setattr(m, "OFI_PATH", ofi)
    monkeypatch.setattr(m, "OUT_PATH", out_path)
    monkeypatch.setattr(m, "FLOW_PATH", tmp_path / "noflow.jsonl")
    monkeypatch.setattr(m, "DERIV_PATH", tmp_path / "noderiv.jsonl")
    monkeypatch.setattr(sys, "argv", ["futu_channel_shadow_eval.py", "--ofi-quality-only"])
    _block_sockets(monkeypatch)
    m.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == 3
    assert payload["timed_rows"] == 3
    assert not out_path.exists()    # 只读: 不写任何输出文件


def test_cli_normal_eval_includes_timing_diagnostics_and_legacy_ic(tmp_path, monkeypatch, capsys):
    m = _load_eval()
    ofi = tmp_path / "ofi.jsonl"
    _write_ofi_file(ofi, [_timed_row("US.AAPL", i) for i in range(2)])
    out_path = tmp_path / "eval_out.json"
    monkeypatch.setattr(m, "OFI_PATH", ofi)
    monkeypatch.setattr(m, "OUT_PATH", out_path)
    monkeypatch.setattr(m, "FLOW_PATH", tmp_path / "noflow.jsonl")
    monkeypatch.setattr(m, "DERIV_PATH", tmp_path / "noderiv.jsonl")
    monkeypatch.setattr(m, "BARS_DIR", tmp_path / "bars")
    monkeypatch.setattr(sys, "argv", ["futu_channel_shadow_eval.py"])
    _block_sockets(monkeypatch)
    m.main()
    assert out_path.exists()
    out = json.loads(out_path.read_text())
    assert out["ofi_timing_quality"]["rows"] == 2     # 正常 eval 增加诊断
    assert out["capital_flow"]["channel"] == "capital_flow"
    # 旧 IC 算法未改: rank-IC 对完全单调数据 = ±1
    assert m.spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])[0] == pytest.approx(1.0)
    assert m.spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1])[0] == pytest.approx(-1.0)


def test_load_ofi_reads_mixed_legacy_and_new_rows(tmp_path, monkeypatch):
    m = _load_eval()
    ofi = tmp_path / "ofi.jsonl"
    legacy = {"symbol": "US.AAPL", "ts_et": "2026-10-15 10:00:00",
              "bid1": 100.0, "bid1_qty": 5.0, "ask1": 100.1, "ask1_qty": 5.0}
    new = _timed_row("US.AAPL", 30)
    new["ts_et"] = "2026-10-15 10:05:00"
    _write_ofi_file(ofi, [legacy, new])
    monkeypatch.setattr(m, "OFI_PATH", ofi)
    data = m.load_ofi()
    snaps = data[("US.AAPL", "2026-10-15")]
    assert len(snaps) == 2                             # 旧 load_ofi 混合行通吃
    assert [s[0] for s in snaps] == ["10:00", "10:05"]
    assert snaps[0][1:] == (100.0, 5.0, 100.1, 5.0)
