"""EOD 兜底「可成交窗口 / 收盘后新建仓」守卫契约 (2026-10-22, TSLA P0).

实锤链 (2026-10-21): 16:45:23 ET EXT 建仓 → 16:45:53 EOD 兜底下 RTH 市价腿 →
券商 CANCELLED_ALL dealt=0 → intent 卡 PENDING → 该标的进 blocked_exits ⇒
SL/TP/时间/因子规则整行跳过 (零保护) 直到人工释放。

本守卫只决定"是否把腿交给券商": 不可成交窗口 / 收盘后新建仓 ⇒ 延后 (不建意图)。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app.playbook.pipeline_parts.overnight_cleanup as oc
from app.core.scheduler_triggers import MarketPhase

ET = timezone(timedelta(hours=-4))  # 10 月 EDT, 固定偏移仅用于构造测试时刻


def _et(hour, minute, day=21, month=10, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=ET)


def _verdict(now, entry, phase, close_min=960):
    return oc.eod_close_verdict(now, entry, phase=phase, close_min=close_min)[0]


# ── ① 收盘后建仓的 EXT 仓 ⇒ 永不是退场对象 (9/17 裁决 EXT 仓可隔夜) ──────────
def test_fresh_ext_entry_after_close_defers_even_inside_window():
    # 窗口内 (16:02) 也不行: 建仓 30 秒后被 EOD 强平 = 本次 P0 的形态
    assert _verdict(_et(16, 2), "2026-10-21T16:01:10-04:00", MarketPhase.AFTER_HOURS) \
        == "defer_fresh_ext"


def test_tsla_case_1645_entry_1645_sweep_defers():
    assert _verdict(_et(16, 50), "2026-10-21T16:45:23.266750-04:00",
                    MarketPhase.AFTER_HOURS) == "defer_fresh_ext"


def test_naive_cst_entry_is_converted_before_comparison():
    # 库里混存 naive CST: 2026-10-22T04:45 CST == 2026-10-21 16:45 ET ⇒ 仍属"收盘后建仓"
    assert _verdict(_et(16, 50), "2026-10-22T04:45:23.266750",
                    MarketPhase.AFTER_HOURS) == "defer_fresh_ext"


# ── ② 可成交窗口 ────────────────────────────────────────────────────────
def test_trading_phase_submits():
    assert _verdict(_et(11, 0), "2026-10-21T10:00:00-04:00", MarketPhase.TRADING) == "submit"


def test_close_grace_window_submits():
    # 实证: trade_records EOD_FORCE_CLOSE 的券商成交 7/7 落在 16:0x
    assert _verdict(_et(16, 5), "2026-10-21T10:00:00-04:00", MarketPhase.AFTER_HOURS) == "submit"


def test_after_grace_window_defers():
    assert _verdict(_et(16, 45), "2026-10-21T10:00:00-04:00",
                    MarketPhase.AFTER_HOURS) == "defer_window"


def test_late_evening_sweep_defers():
    assert _verdict(_et(19, 50), "2026-10-21T10:00:00-04:00",
                    MarketPhase.AFTER_HOURS) == "defer_window"


def test_premarket_and_overnight_defer():
    assert _verdict(_et(9, 25), "2026-10-20T10:00:00-04:00", MarketPhase.PRE_MARKET) \
        == "defer_window"
    assert _verdict(_et(1, 0), "2026-10-20T10:00:00-04:00", MarketPhase.OVERNIGHT) \
        == "defer_window"


def test_early_close_day_uses_1300_boundary():
    # 半日市 (13:00 收盘): 13:05 仍可成交, 13:40 不可
    assert _verdict(_et(13, 5, day=27, month=11), "2026-11-27T10:00:00-05:00",
                    MarketPhase.AFTER_HOURS, close_min=780) == "submit"
    assert _verdict(_et(13, 40, day=27, month=11), "2026-11-27T10:00:00-05:00",
                    MarketPhase.AFTER_HOURS, close_min=780) == "defer_window"


# ── ③ 边界: 入场时间不可解析/为空 ⇒ 退化为纯窗口判定 (不误判为收盘后建仓) ──
def test_unparseable_entry_falls_back_to_window():
    assert _verdict(_et(11, 0), "", MarketPhase.TRADING) == "submit"
    assert _verdict(_et(11, 0), "garbage", MarketPhase.TRADING) == "submit"
    assert _verdict(_et(19, 50), "garbage", MarketPhase.AFTER_HOURS) == "defer_window"


def test_yesterday_entry_is_not_fresh():
    assert _verdict(_et(16, 45), "2026-10-20T15:00:00-04:00",
                    MarketPhase.AFTER_HOURS) == "defer_window"


# ── ④ 接线契约: 守卫必须先于交腿, 且不得早于隔夜豁免/深水闸影子打点 ──────
_SRC = Path(oc.__file__).read_text(encoding="utf-8")


def test_guard_precedes_broker_submit():
    assert _SRC.index("eod_close_verdict(") < _SRC.index("dual.close_position(")


def test_guard_keeps_exemption_telemetry_upstream():
    # 深水闸影子 (RFC-079) 打点在守卫之前 ⇒ 延后不会造成影子零样本
    assert _SRC.index("_edg_emit(\"sweeper\"") < _SRC.index("_act, _why = eod_close_verdict(")

# ── ⑤ 接线活体探针: 真调用 _force_close_orphaned_sim_positions (假 DB/假券商) ──
class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=()):
        rows = self._rows

        class _Cur:
            def fetchall(self_inner):
                return rows

        return _Cur()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _row(**over):
    row = {
        "id": 1710, "symbol": "US.TSLA", "direction": "SHORT", "entry_price": 390.05,
        "quantity": 12, "metadata_json": '{"cohort": "B"}',
        "entry_time": "2026-10-21T16:45:23.266750-04:00",
        "source_type": "PLAYBOOK_SIM", "futu_order_id": "8276448",
    }
    row.update(over)
    return row


def _call_sweeper(monkeypatch, *, now, phase, rows):
    import sqlite3
    import app.core.paths as _paths
    import app.core.scheduler_triggers as _st
    import app.execution.dual_broker as _db_mod
    import app.utils.timezone as _tz

    calls, alerts = [], []

    class _Dual:
        def close_position(self, *a, **kw):
            calls.append((a, kw))
            return {"success": True, "exit_state": "BOOKED", "filled_price": 100.0}

    class _Sched:
        class data_router:
            @staticmethod
            def get_quote(symbol):
                return {"last": 100.0}

    monkeypatch.setattr(sqlite3, "connect", lambda _p: _FakeConn(rows))
    monkeypatch.setattr(_paths, "get_trade_records_db", lambda: "/tmp/none.db")
    monkeypatch.setattr(_st, "get_market_phase", lambda: phase)
    monkeypatch.setattr(_tz, "now_et", lambda: now)
    monkeypatch.setattr(_db_mod, "get_dual_broker", lambda: _Dual())

    closed = oc._force_close_orphaned_sim_positions(
        _Sched(),
        logger=__import__("logging").getLogger("t"),
        _alert_close_failure=lambda *a, **kw: alerts.append(a),
        _book_playbook_exit_result=lambda *a, **kw: 1,
        _cohort_b_max_hold_hit=lambda _t: False,
        _mark_overnight_arm=lambda *a, **kw: None,
        _playbook_exit_decision=lambda reason, price, meta: {"reason": reason},
        _playbook_exit_owner=lambda db, r, meta: {"owner": "t"},
        _release_live_broker_stop_for_rule_exit=lambda *a, **kw: {"action": "proceed"},
        _resume_playbook_exits=lambda allow_submit: (0, {}, True),
        _today_et_str=lambda: "2026-10-21",
    )
    return closed, calls, alerts


def test_sweeper_does_not_submit_for_fresh_ext_position(monkeypatch):
    # 真实形态: 16:50 ET sweep 命中 16:45:23 ET 建仓仓 → 一条腿都不许交
    closed, calls, alerts = _call_sweeper(
        monkeypatch, now=_et(16, 50), phase=MarketPhase.AFTER_HOURS, rows=[_row()])
    assert calls == []
    assert alerts == []
    assert closed == 0


def test_sweeper_does_not_submit_outside_window(monkeypatch):
    closed, calls, alerts = _call_sweeper(
        monkeypatch, now=_et(19, 50), phase=MarketPhase.AFTER_HOURS,
        rows=[_row(entry_time="2026-10-20T10:00:00-04:00")])
    assert calls == []
    assert alerts == []
    assert closed == 0


def test_sweeper_still_submits_inside_close_window(monkeypatch):
    closed, calls, alerts = _call_sweeper(
        monkeypatch, now=_et(16, 5), phase=MarketPhase.AFTER_HOURS,
        rows=[_row(entry_time="2026-10-20T10:00:00-04:00")])
    assert len(calls) == 1
    assert calls[0][0][0] == "US.TSLA" and calls[0][0][1] == 12
    assert closed == 1 and alerts == []
