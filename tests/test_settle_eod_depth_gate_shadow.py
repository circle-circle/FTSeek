# -*- coding: utf-8 -*-
"""settle_eod_depth_gate_shadow 身份锚定契约钉 (RFC-095, 2026-09-22).

钉住 (每条对应一个实测病灶或自引入缺陷):
  1. 身份锚定优先级 trade_ref > entry_time > (entry_price±0.5% ∧ qty)
  2. **同日同标的另一笔已平仓不得被当成结局** (病灶①活用例: US.MU 1503 曾被配成 1501)
  3. 无锚/仓未平 → 保持 unsettled, 绝不写 settled (自引入缺陷回归钉)
  4. 未持夜 (出场 ET 日 ≤ arm_date) → status=not_held 且不进 Δ 聚合 (病灶②)
  5. Δ 数学与方向 (LONG/SHORT)
  6. 触发组按 trade_id 去重取最早夜 (病灶③) + 判据阈值方向未被改动
  7. 坏行 (无 symbol/arm_date) 落 bad_row 不抛
DB 用内存 sqlite 桩 (不需要生产库), 影子文件路径不参与本测试。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from scripts import settle_eod_depth_gate_shadow as sp  # noqa: E402

SCHEMA = """
CREATE TABLE trade_records (
    id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, entry_time TEXT,
    exit_time TEXT, exit_price REAL, pnl_net REAL, realized_pnl REAL,
    exit_reason TEXT, quantity REAL, entry_price REAL
)
"""


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    for r in rows:
        conn.execute(
            "INSERT INTO trade_records (id,symbol,direction,entry_time,exit_time,exit_price,"
            "pnl_net,realized_pnl,exit_reason,quantity,entry_price) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r.get("symbol"), r.get("direction"), r.get("entry_time"),
             r.get("exit_time"), r.get("exit_price"), r.get("pnl_net"), r.get("realized_pnl"),
             r.get("exit_reason"), r.get("quantity"), r.get("entry_price")))
    conn.commit()
    return conn


def _arm(**kw):
    base = dict(shadow="eod_depth_gate", ts="2026-09-15T15:52:09.493253-04:00",
                arm_date="2026-09-15", symbol="US.AAA", direction="LONG",
                entry_price=100.0, qty=10.0, decision_price=97.0,
                depth_net_pct=-3.0, would_revoke=True)
    base.update(kw)
    return base


# ── 1/2: 锚定优先级 + 同日另一笔已平仓不得被当结局 ─────────────────────────────
class TestIdentityAnchor:
    def test_trade_ref_wins_over_other_same_day_trade(self):
        """病灶①原形: 同日两笔, 早平的那笔 (1501 形态) 不得被选; trade_ref 指向真仓。"""
        conn = _db([
            {"id": 1501, "symbol": "US.MU", "direction": "LONG", "quantity": 6,
             "entry_price": 935.095, "exit_price": 937.39, "exit_time": "2026-09-15T10:03:39-04:00",
             "pnl_net": 9.63, "exit_reason": "SL_BREAKEVEN",
             "entry_time": "2026-09-15T09:46:34-04:00"},
            {"id": 1503, "symbol": "US.MU", "direction": "LONG", "quantity": 6,
             "entry_price": 934.515, "exit_price": 931.27, "exit_time": "2026-09-16T09:31:11-04:00",
             "pnl_net": -23.61, "exit_reason": "SL_BREAKEVEN",
             "entry_time": "2026-09-15T10:48:09-04:00"},
        ])
        ok, f = sp.settle_row(_arm(symbol="US.MU", entry_price=934.515, qty=6,
                                   trade_ref=1503, entry_time="2026-09-15T10:48:09-04:00"),
                              conn)
        assert ok and f["trade_id"] == 1503 and f["match_by"] == "trade_ref"
        assert f["cf_delta"] == pytest.approx(1 * (97.0 - 931.27) * 6, abs=0.01)

    def test_price_qty_anchor_skips_trade_exited_before_arm(self):
        """存量行 (无 trade_ref/entry_time): 价量锚也不得选中'早于决策时刻出场'的仓。"""
        conn = _db([
            {"id": 1, "symbol": "US.AAA", "direction": "LONG", "quantity": 10,
             "entry_price": 100.0, "exit_price": 101.0, "exit_time": "2026-09-15T10:00:00-04:00"},
            {"id": 2, "symbol": "US.AAA", "direction": "LONG", "quantity": 10,
             "entry_price": 100.0, "exit_price": 96.0, "exit_time": "2026-09-16T09:31:00-04:00"},
        ])
        ok, f = sp.settle_row(_arm(), conn)
        assert ok and f["trade_id"] == 2 and f["match_by"] == "entry_price_qty"

    def test_entry_time_anchor(self):
        conn = _db([
            {"id": 7, "symbol": "US.AAA", "direction": "LONG", "quantity": 10,
             "entry_price": 100.0, "exit_price": 95.0, "exit_time": "2026-09-16T09:30:00-04:00",
             "entry_time": "2026-09-15T11:00:00-04:00"},
        ])
        ok, f = sp.settle_row(_arm(entry_time="2026-09-15T11:00:00-04:00"), conn)
        assert ok and f["trade_id"] == 7 and f["match_by"] == "entry_time"

    def test_direction_mismatch_not_matched(self):
        """反向同价同量不得跨方向配 (SHORT 仓不得配到 LONG 行)。"""
        conn = _db([
            {"id": 9, "symbol": "US.AAA", "direction": "SHORT", "quantity": 10,
             "entry_price": 100.0, "exit_price": 105.0, "exit_time": "2026-09-16T09:30:00-04:00"},
        ])
        ok, f = sp.settle_row(_arm(direction="LONG"), conn)
        assert ok is False and f == {}


# ── 3: 无锚不得写 settled (自引入缺陷回归钉) ──────────────────────────────────
class TestNoAnchorStaysUnsettled:
    def test_still_open_position_stays_unsettled(self):
        conn = _db([
            {"id": 1546, "symbol": "US.RIVN", "direction": "LONG", "quantity": 382,
             "entry_price": 15.615, "exit_price": None, "exit_time": None,
             "entry_time": "2026-09-21T09:37:27-04:00"},
        ])
        ok, f = sp.settle_row(_arm(symbol="US.RIVN", entry_price=15.615, qty=382,
                                   arm_date="2026-09-21", trade_ref=1546,
                                   entry_time="2026-09-21T09:37:27-04:00"), conn)
        assert ok is False and f == {}

    def test_no_matching_trade_stays_unsettled(self):
        conn = _db([])
        ok, f = sp.settle_row(_arm(), conn)
        assert ok is False and f == {}

    def test_missing_price_and_qty_stays_unsettled(self):
        conn = _db([{"id": 1, "symbol": "US.AAA", "direction": "LONG", "quantity": 1,
                     "entry_price": 10.4, "exit_price": 10.5,
                     "exit_time": "2026-09-18T09:30:00-04:00"}])
        ok, f = sp.settle_row(_arm(entry_price=0, qty=0), conn)
        assert ok is False and f == {}


# ── 4/5: 未持夜排除 + Δ 数学方向 ─────────────────────────────────────────────
class TestNotHeldAndDelta:
    def test_same_day_exit_is_not_held(self):
        """病灶②原形: 15:52:04 记豁免成立, 15:52:51 平掉 (ET 日相同) → 不算隔夜结局。"""
        conn = _db([
            {"id": 1536, "symbol": "US.NOK", "direction": "LONG", "quantity": 555,
             "entry_price": 10.615, "exit_price": 10.64,
             "exit_time": "2026-09-17T15:52:51-04:00", "pnl_net": -0.78,
             "exit_reason": "PRE_CLOSE_EXIT"},
        ])
        ok, f = sp.settle_row(_arm(symbol="US.NOK", arm_date="2026-09-17",
                                   ts="2026-09-17T15:52:04-04:00",
                                   entry_price=10.615, qty=555, decision_price=10.63), conn)
        assert ok and f["status"] == "not_held" and f["held_overnight"] is False
        assert f["cf_delta"] is None

    def test_next_day_exit_held_and_delta_long(self):
        conn = _db([
            {"id": 5, "symbol": "US.AAA", "direction": "LONG", "quantity": 10,
             "entry_price": 100.0, "exit_price": 101.0,
             "exit_time": "2026-09-16T09:31:00-04:00", "pnl_net": 8.0},
        ])
        ok, f = sp.settle_row(_arm(), conn)
        assert ok and f["status"] == "settled" and f["held_overnight"] is True
        assert f["cf_delta"] == pytest.approx((97.0 - 101.0) * 10, abs=0.01)  # 负 = 豁免比EOD平更好

    def test_delta_short_sign(self):
        conn = _db([
            {"id": 6, "symbol": "US.AAA", "direction": "SHORT", "quantity": 10,
             "entry_price": 100.0, "exit_price": 95.0,
             "exit_time": "2026-09-16T09:31:00-04:00", "pnl_net": 40.0},
        ])
        ok, f = sp.settle_row(_arm(direction="SHORT", decision_price=103.0), conn)
        assert ok and f["cf_delta"] == pytest.approx(-1 * (103.0 - 95.0) * 10, abs=0.01)

    def test_bad_price_row(self):
        conn = _db([{"id": 8, "symbol": "US.AAA", "direction": "LONG", "quantity": 10,
                     "entry_price": 100.0, "exit_price": 95.0,
                     "exit_time": "2026-09-16T09:31:00-04:00"}])
        ok, f = sp.settle_row(_arm(decision_price=0), conn)
        assert ok and f["status"] == "bad_price" and f["cf_delta"] is None

    def test_bad_row_without_symbol(self):
        ok, f = sp.settle_row({"arm_date": "", "symbol": ""}, None)
        assert ok and f["status"] == "bad_row"


def test_earnings_night_summary_is_deduped_and_not_b19_mixed():
    rows = [
        {"trade_id": 1, "arm_date": "2026-09-30", "symbol": "US.MU",
         "is_earnings_night": True, "status": "settled", "held_overnight": True,
         "cf_delta": 20.0},
        {"trade_id": 1, "arm_date": "2026-10-01", "symbol": "US.MU",
         "is_earnings_night": True, "status": "settled", "held_overnight": True,
         "cf_delta": 99.0},
        {"trade_id": 2, "arm_date": "2026-10-01", "symbol": "US.AAA",
         "is_earnings_night": True, "status": "settled", "held_overnight": True,
         "cf_delta": -12.0},
        {"trade_id": 3, "arm_date": "2026-10-01", "symbol": "US.BBB",
         "is_earnings_night": None, "status": "settled", "held_overnight": True,
         "cf_delta": 50.0},
    ]
    result = sp.earnings_night_summary(rows)
    assert result["n"] == 2
    assert result["sum_delta"] == 8.0
    assert result["median_delta"] == 4.0
    assert result["eod_better_n"] == 1
    assert result["hold_better_n"] == 1
    assert result["unknown_n"] == 1
    assert result["verdict"] == "KEEP_SAMPLING"


# ── 6: 触发组去重 + 判据方向 ─────────────────────────────────────────────────
class TestTriggerDedupeAndVerdict:
    def _row(self, tid, arm_date, delta, revoke=True, held=True, status="settled"):
        return {"trade_id": tid, "arm_date": arm_date, "cf_delta": delta, "would_revoke": revoke,
                "held_overnight": held, "status": status}

    def test_same_trade_multi_night_counted_once_earliest(self):
        rows = [self._row(1485, "2026-09-14", 78.21), self._row(1485, "2026-09-16", 50.0),
                self._row(1518, "2026-09-16", -153.0)]
        trig = sp.trigger_rows(rows)
        assert len(trig) == 2
        assert [r["arm_date"] for r in trig if r["trade_id"] == 1485] == ["2026-09-14"]

    def test_not_held_and_non_revoke_excluded(self):
        rows = [self._row(1, "2026-09-10", -100.0), self._row(2, "2026-09-10", None, held=False),
                self._row(3, "2026-09-10", 50.0, revoke=False),
                self._row(4, "2026-09-10", 50.0, status="not_held")]
        trig = sp.trigger_rows(rows)
        assert [r["trade_id"] for r in trig] == [1]
        assert sp._verdict(len(trig), sum(r["cf_delta"] for r in trig),
                           sum(1 for r in trig if r["cf_delta"] < -sp.FEE_LINE))

    def test_verdict_thresholds_unchanged(self):
        """B19 预注册阈值/direction 逐字未改 (RFC-095 只修测量口径)。"""
        assert sp._verdict(7, +500.0, 0) == "KEEP_SAMPLING"
        assert sp._verdict(8, +500.0, 0) == "GRADUATION_LINE_HIT"
        assert sp._verdict(8, -1.0, 0) == "GATE_NOT_WORTH"
        assert sp._verdict(8, +500.0, 2) == "GATE_NOT_WORTH"
        assert sp._verdict(8, +500.0, 1) == "GRADUATION_LINE_HIT"
        assert sp.MIN_N == 8 and sp.MAX_MISKILL == 1 and sp.FEE_LINE == 5.0


# ── RFC-160 (2026-10-22): MFE 轨 (当日 MFE<0.5% ∧ 水下) 契约钉 ─────────────
class TestMfeTrack:
    """MFE 轨与 B19 深度轨同尺同判据形态, 但触发谓词独立 (判别器不同)。"""

    def _row(self, tid, mfe, depth, delta, held=True, arm="2026-10-20"):
        return dict(trade_id=tid, arm_date=arm, status="settled", held_overnight=held,
                    cf_delta=delta, mfe_pct_at_arm=mfe, depth_net_pct=depth,
                    would_revoke=False)

    def test_mfe_track_predicate(self):
        """只有 mfe<0.5 ∧ depth<0 的行进组; mfe/depth 缺失行不进 (不猜)。"""
        rows = [self._row(1, 0.3, -1.0, -50.0),      # 进组
                self._row(2, 0.7, -1.0, -50.0),      # mfe 超阈
                self._row(3, 0.3, +0.5, -50.0),      # 非水下
                self._row(4, None, -1.0, -50.0),     # mfe 缺失
                self._row(5, 0.3, None, -50.0),      # depth 缺失
                self._row(6, 0.3, -1.0, -50.0, held=False)]  # 未持夜
        trig = sp.mfe_trigger_rows(rows)
        assert [r["trade_id"] for r in trig] == [1]

    def test_mfe_track_dedup_earliest_night(self):
        """同 trade_id 多夜取最早 (病灶③同型纪律)。"""
        rows = [self._row(7, 0.2, -1.0, -10.0, arm="2026-10-21"),
                self._row(7, 0.2, -1.0, -99.0, arm="2026-10-19")]
        trig = sp.mfe_trigger_rows(rows)
        assert len(trig) == 1 and trig[0]["arm_date"] == "2026-10-19"

    def test_mfe_track_verdict_form_b19(self):
        """判据形态复用 B19 (n≥8 ∧ ΣΔ>0 ∧ 误杀≤1); 与深度轨 verdict 互不影响。"""
        assert sp._verdict(8, +100.0, 1) == "GRADUATION_LINE_HIT"
        assert sp._verdict(8, +100.0, 2) == "GATE_NOT_WORTH"
        assert sp._verdict(7, +100.0, 0) == "KEEP_SAMPLING"

    def test_mfe_gate_pct_pinned(self):
        """0.5% 阈值钉死 (10-17 尸检刀尖注记: 改阈值=改判据=重新裁决)。"""
        assert sp.MFE_GATE_PCT == 0.5
