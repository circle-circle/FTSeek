"""Tests for 耐心出场 (patient exit) — DEB-20260813 §18.3 实现验收.

纯函数: patient_exit_arm_eligible / patient_exit_eval_wait (零 IO).
集成: monitor_playbook_sim_exits 状态机 — arm/等待/fill/stop/eod/expire/关闭回退,
     全 mock 隔离 (tmp DB + mock broker + frozen config), 不碰生产 DB/broker.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import app.playbook.pipeline as pipeline_mod
from app.playbook.pipeline import (
    monitor_playbook_sim_exits,
    patient_exit_arm_eligible,
    patient_exit_eval_wait,
)
from app.core.scheduler_triggers import ET as _ET


# ────────────────────────── 纯函数单测 ──────────────────────────

CFG_ON = {
    "patient_exit_enabled": True,
    "patient_exit_max_underwater_pct": 1.0,
    "patient_exit_stop_pct": 1.5,
    "patient_exit_deadline_hhmm": 1550,
}
CFG_OFF = dict(CFG_ON, patient_exit_enabled=False)


class TestPatientExitArmEligible:
    def test_dead_signal_shallow_underwater_arms(self):
        assert patient_exit_arm_eligible("DEAD_SIGNAL_EXIT", -0.7, {}, CFG_ON) is True

    def test_dead_signal_at_zero_arms(self):
        assert patient_exit_arm_eligible("DEAD_SIGNAL_EXIT", 0.0, {}, CFG_ON) is True

    def test_dead_signal_at_max_boundary_arms(self):
        assert patient_exit_arm_eligible("DEAD_SIGNAL_EXIT", -1.0, {}, CFG_ON) is True

    def test_deep_underwater_rejected(self):
        assert patient_exit_arm_eligible("DEAD_SIGNAL_EXIT", -1.5, {}, CFG_ON) is False

    def test_profit_positive_rejected(self):
        assert patient_exit_arm_eligible("DEAD_SIGNAL_EXIT", 0.3, {}, CFG_ON) is False

    def test_other_reason_rejected(self):
        assert patient_exit_arm_eligible("TIME_EXIT", -0.5, {}, CFG_ON) is False
        assert patient_exit_arm_eligible("MFE_STALL_EXIT", -0.5, {}, CFG_ON) is False

    def test_switch_off_rejected(self):
        assert patient_exit_arm_eligible("DEAD_SIGNAL_EXIT", -0.5, {}, CFG_OFF) is False

    def test_already_armed_no_rearm(self):
        meta = {"patient_exit": {"armed_at": "x"}}
        assert patient_exit_arm_eligible("DEAD_SIGNAL_EXIT", -0.5, meta, CFG_ON) is False

    def test_expired_tombstone_no_rearm(self):
        meta = {"_patient_exit_expired": True}
        assert patient_exit_arm_eligible("DEAD_SIGNAL_EXIT", -0.5, meta, CFG_ON) is False


class TestPatientExitEvalWait:
    CFG = CFG_ON

    def _meta(self, day="2026-08-13"):
        return {"patient_exit": {"armed_day": day, "armed_at": "2026-08-13T12:00:00-04:00"}}

    def test_price_back_above_entry_fills(self):
        now = _ET.localize(datetime(2026, 8, 13, 14, 0))
        mode, _ = patient_exit_eval_wait(self._meta(), "LONG", 100.0, 100.2, now, self.CFG)
        assert mode == "fill"

    def test_exactly_at_entry_fills(self):
        now = _ET.localize(datetime(2026, 8, 13, 14, 0))
        mode, _ = patient_exit_eval_wait(self._meta(), "LONG", 100.0, 100.0, now, self.CFG)
        assert mode == "fill"

    def test_deepening_loss_stops(self):
        now = _ET.localize(datetime(2026, 8, 13, 14, 0))
        mode, _ = patient_exit_eval_wait(self._meta(), "LONG", 100.0, 98.4, now, self.CFG)
        assert mode == "stop"

    def test_stop_priority_over_deadline(self):
        now = _ET.localize(datetime(2026, 8, 13, 15, 55))
        mode, _ = patient_exit_eval_wait(self._meta(), "LONG", 100.0, 98.0, now, self.CFG)
        assert mode == "stop"

    def test_deadline_eod(self):
        now = _ET.localize(datetime(2026, 8, 13, 15, 50))
        mode, _ = patient_exit_eval_wait(self._meta(), "LONG", 100.0, 99.5, now, self.CFG)
        assert mode == "eod"

    def test_before_deadline_keeps_waiting(self):
        now = _ET.localize(datetime(2026, 8, 13, 14, 30))
        mode, _ = patient_exit_eval_wait(self._meta(), "LONG", 100.0, 99.5, now, self.CFG)
        assert mode == ""

    def test_short_direction_symmetric(self):
        now = _ET.localize(datetime(2026, 8, 13, 14, 0))
        assert patient_exit_eval_wait(self._meta(), "SHORT", 100.0, 99.8, now, self.CFG)[0] == "fill"
        assert patient_exit_eval_wait(self._meta(), "SHORT", 100.0, 101.6, now, self.CFG)[0] == "stop"

    def test_cross_day_expires(self):
        now_next = _ET.localize(datetime(2026, 8, 14, 10, 0))
        mode, _ = patient_exit_eval_wait(self._meta(), "LONG", 100.0, 99.5, now_next, self.CFG)
        assert mode == "expire"

    def test_no_state_returns_empty(self):
        now = _ET.localize(datetime(2026, 8, 13, 14, 0))
        assert patient_exit_eval_wait({}, "LONG", 100.0, 99.5, now, self.CFG) == ("", "")


# ────────────────────────── 集成: monitor 状态机 ──────────────────────────

class FakePbCfg:
    """假 PlaybookConfig: .get + get_horizon_top + get_horizon_params (规则函数需要)."""

    def __init__(self, overrides=None):
        self._d = {
            "patient_exit_enabled": True,
            "patient_exit_max_underwater_pct": 1.0,
            "patient_exit_stop_pct": 1.5,
            "patient_exit_deadline_hhmm": 1550,
            "dead_signal_exit_enabled": True,
            "dead_signal_exit_minutes": 60,
            "dead_signal_exit_mfe_pct": 0.2,
            "mfe_stall_exit_enabled": False,
            "early_dead_signal_shadow_enabled": False,
            "high_atr_stall_shadow_enabled": False,
            "overnight_hold_enabled": False,
            "cooldown_sl_pct": 4.0,
            "early_tight_sl_enabled": False,
            "breakeven_fee_aware_enabled": False,
            "breakeven_fee_cushion_pct": 0.0,
            "min_tp_rr": 1.5,
        }
        if overrides:
            self._d.update(overrides)

    def get(self, k, default=None):
        return self._d.get(k, default)

    def get_horizon_top(self, k, default=None):
        # 压掉 TIME_EXIT (time_exit_hours=99) — 只测 patient exit 分支
        if k == "factor_exit_rules":
            return {"default": {"time_exit_hours": 99.0, "breakeven_at_pct": 0.5}}
        return default

    def get_horizon_params(self, horizon):
        return {}

    def get_exit_config_version(self):
        return "test-v0"


class TestMonitorPatientExitStateMachine:
    """monitor_playbook_sim_exits 循环级测试 — tmp DB + mock broker, 零生产副作用."""

    @pytest.fixture
    def env(self, monkeypatch, tmp_path):
        db_path = tmp_path / "test_ftseek.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE trade_records (
                id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT,
                entry_price REAL, entry_time TEXT, quantity REAL, source_type TEXT,
                metadata_json TEXT, exit_price REAL, exit_time TEXT, exit_reason TEXT,
                realized_pnl REAL, pnl_pct REAL, holding_hours REAL,
                close_futu_order_id TEXT, mfe_pct REAL, mae_pct REAL,
                max_drawdown REAL, pnl_net REAL, fees_est REAL, decision_id TEXT,
                trade_mode TEXT, trade_date TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE playbook_instructions (
                id INTEGER PRIMARY KEY, outcome TEXT, exit_price REAL,
                exit_reason TEXT, pnl REAL, pnl_pct REAL,
                holding_period_hours REAL, max_favorable_excursion REAL,
                max_adverse_excursion REAL
            )
        """)
        conn.execute("CREATE TABLE open_positions (symbol TEXT, size REAL)")
        conn.commit()

        # ── monkeypatch: DB 路径 / 市场相位 / config ──
        monkeypatch.setattr("app.core.paths.get_trade_records_db", lambda: db_path)
        from app.core.scheduler_triggers import MarketPhase
        monkeypatch.setattr(
            "app.core.scheduler_triggers.get_market_phase", lambda: MarketPhase.TRADING)

        cfg = FakePbCfg()
        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: cfg)

        close_calls = []

        def _fake_close(conn_, trade_id, exit_price, exit_reason, pnl, pnl_pct,
                        holding_hours, inst_id=None, symbol=None, close_futu_order_id=None):
            close_calls.append({
                "trade_id": trade_id, "exit_price": exit_price,
                "exit_reason": exit_reason, "pnl": pnl, "symbol": symbol,
            })
            conn_.execute(
                "UPDATE trade_records SET exit_price=?, exit_reason=?, exit_time=? WHERE id=?",
                (exit_price, exit_reason, datetime.now(_ET).isoformat(), trade_id))
            conn_.commit()

        monkeypatch.setattr(pipeline_mod, "_close_trade_record", _fake_close)

        # ── 关键: dual broker 必须 mock — 否则真平 Futu 仓 ──
        def _fake_get_dual_broker():
            dual = MagicMock()

            def _close(symbol, qty, direction, target_env=None,
                       limit_price=0.0, limit_timeout_s=30.0):
                return {
                    "success": True, "order_filled": True,
                    "filled_price": _last_quote.get("price", 0),
                    "price_source": "test_mock",
                    "broker_qty_before": qty, "broker_qty_after": 0,
                    "futu_order_id": "TEST-ORDER-1",
                }
            dual.close_position.side_effect = _close
            return dual

        monkeypatch.setattr("app.execution.dual_broker.get_dual_broker", _fake_get_dual_broker)
        monkeypatch.setattr(pipeline_mod, "_alert_close_failure", lambda *a, **k: None)

        # 影子 jsonl 落 tmp (chdir 隔离, 不污染仓库 data/)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)

        # ── 冻结时钟: 当日正午 ET (2026-09-02, RFC-073 第二批日期漂移钉修复) ──
        # 病灶: entry=now-120min 在 ET 凌晨/盘前跨午夜 → 判隔夜 → RFC-073 生效口径
        #   minutes_from_open<0 → DEAD_SIGNAL 时间门挡住 → 本批用例 (依赖盘中时间窗)
        #   非盘中时段全红. 冻结 pipeline 模块级 datetime 后测试与墙钟解耦.
        fixed_now = datetime.now(_ET).replace(hour=12, minute=0, second=0, microsecond=0)

        class _FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return fixed_now.replace(tzinfo=None)
                return fixed_now.astimezone(tz)

        monkeypatch.setattr(pipeline_mod, "datetime", _FrozenDateTime)

        scheduler = MagicMock()
        scheduler.data_router.get_batch_quotes.return_value = {}
        scheduler.data_router.get_bars.side_effect = RuntimeError("no bars in test")

        _last_quote = {"price": 0.0}

        return {
            "conn": conn, "db_path": db_path, "cfg": cfg,
            "close_calls": close_calls, "scheduler": scheduler,
            "last_quote": _last_quote, "fixed_now": fixed_now,
        }

    def _insert_open_position(self, env, symbol="US.TEST", direction="LONG",
                              entry_price=100.0, held_min=120, stop_loss=96.0,
                              extra_meta=None):
        now = env["fixed_now"]  # 冻结时钟, 与 pipeline 内 datetime.now 同源
        entry = (now - timedelta(minutes=held_min)).isoformat()
        meta = {"stop_loss": stop_loss, "cohort": "A", "horizon": "short",
                "peak_price": entry_price}
        if extra_meta:
            meta.update(extra_meta)
        env["conn"].execute(
            "INSERT INTO trade_records (symbol, direction, entry_price, entry_time, "
            "quantity, source_type, metadata_json, exit_price) VALUES (?,?,?,?,?,?,?,0)",
            (symbol, direction, entry_price, entry, 10, "PLAYBOOK_SIM",
             json.dumps(meta)))
        env["conn"].commit()

    def _set_quote(self, env, symbol, price):
        env["scheduler"].data_router.get_batch_quotes.return_value = {
            symbol: {"last": price}}
        env["last_quote"]["price"] = price  # mock broker 成交价 = 当前报价

    def _meta_after(self, env):
        row = env["conn"].execute(
            "SELECT metadata_json FROM trade_records ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row["metadata_json"] or "{}")

    def _armed_meta(self, minutes_ago=20, _now=None, **over):
        now = _now or datetime.now(_ET)
        d = {
            "armed_at": (now - timedelta(minutes=minutes_ago)).isoformat(),
            "armed_day": now.strftime("%Y-%m-%d"),
            "armed_held_min": 60.0, "armed_profit_pct": -0.7,
            "mfe_at_arm": 0.0, "stop_pct": 1.5, "deadline_hhmm": 1550,
            "immediate_exit_price": 99.3,
        }
        d.update(over)
        return {"patient_exit": d}

    # ── 场景 1: DEAD_SIGNAL_EXIT + 浅水下 → arm, 不平仓 ──
    def test_dead_signal_shallow_arms_and_holds(self, env):
        self._insert_open_position(env, held_min=120)
        self._set_quote(env, "US.TEST", 99.3)  # 水下 0.7%
        n = monitor_playbook_sim_exits(env["scheduler"])
        assert n == 0
        assert env["close_calls"] == []
        meta = self._meta_after(env)
        assert "patient_exit" in meta
        assert meta["patient_exit"]["armed_profit_pct"] == pytest.approx(-0.7, abs=0.02)
        assert meta["patient_exit"]["stop_pct"] == 1.5
        assert meta["patient_exit"]["immediate_exit_price"] == pytest.approx(99.3, abs=0.01)

    # ── 场景 2: armed 仓价格回入场价上方 → PATIENT_EXIT_FILL + 双口径 delta ──
    def test_armed_position_fills_at_entry_price(self, env):
        self._insert_open_position(env, held_min=80,
                                   extra_meta=self._armed_meta(_now=env["fixed_now"],
                                                               immediate_exit_price=99.3))
        self._set_quote(env, "US.TEST", 100.2)
        n = monitor_playbook_sim_exits(env["scheduler"])
        assert n == 1
        call = env["close_calls"][0]
        assert call["exit_reason"] == "PATIENT_EXIT_FILL"
        assert call["exit_price"] == pytest.approx(100.2, abs=0.001)
        meta = self._meta_after(env)
        pe = meta["patient_exit"]
        assert pe["exit_mode"] == "fill"
        # delta = (100.2 - 99.3) × 10 = +9.0
        assert pe["patient_vs_immediate_delta"] == pytest.approx(9.0, abs=0.05)

    # ── 场景 3: armed 仓水下加深至 1.5% → PATIENT_EXIT_STOP ──
    def test_armed_position_stops_on_deep_loss(self, env):
        self._insert_open_position(env, held_min=80,
                                   extra_meta=self._armed_meta(minutes_ago=10,
                                                               _now=env["fixed_now"]))
        self._set_quote(env, "US.TEST", 98.4)  # 水下 1.6%
        n = monitor_playbook_sim_exits(env["scheduler"])
        assert n == 1
        call = env["close_calls"][0]
        assert call["exit_reason"] == "PATIENT_EXIT_STOP"
        assert call["pnl"] < 0

    # ── 场景 4: deadline_hhmm=0 → 任何时刻即达兜底 → PATIENT_EXIT_EOD ──
    def test_armed_position_eod_deadline(self, env):
        env["cfg"]._d["patient_exit_deadline_hhmm"] = 0
        self._insert_open_position(
            env, held_min=260,
            extra_meta=self._armed_meta(minutes_ago=200, deadline_hhmm=0,
                                        _now=env["fixed_now"]))
        self._set_quote(env, "US.TEST", 99.5)
        n = monitor_playbook_sim_exits(env["scheduler"])
        assert n == 1
        assert env["close_calls"][0]["exit_reason"] == "PATIENT_EXIT_EOD"

    # ── 场景 5: armed 状态跨交易日 → expire 清状态 + 墓碑防重 arm ──
    def test_armed_cross_day_expires_to_normal_rules(self, env):
        yesterday = (env["fixed_now"] - timedelta(days=1)).strftime("%Y-%m-%d")
        armed = self._armed_meta(minutes_ago=2000, _now=env["fixed_now"])
        armed["patient_exit"]["armed_day"] = yesterday
        self._insert_open_position(env, held_min=300, extra_meta=armed)
        self._set_quote(env, "US.TEST", 99.5)
        monitor_playbook_sim_exits(env["scheduler"])
        meta = self._meta_after(env)
        assert "patient_exit" not in meta
        assert meta.get("_patient_exit_expired") is True
        # expire 后回正常规则链: 死单条件仍在 → DEAD_SIGNAL_EXIT 立即平 (墓碑阻止重新 arm)
        assert env["close_calls"][0]["exit_reason"] == "DEAD_SIGNAL_EXIT"

    # ── 场景 6: 开关关闭 → DEAD_SIGNAL_EXIT 立即市价平 (无等待) ──
    def test_switch_off_dead_signal_exits_immediately(self, env):
        env["cfg"]._d["patient_exit_enabled"] = False
        self._insert_open_position(env, held_min=120)
        self._set_quote(env, "US.TEST", 99.3)
        n = monitor_playbook_sim_exits(env["scheduler"])
        assert n == 1
        call = env["close_calls"][0]
        assert call["exit_reason"] == "DEAD_SIGNAL_EXIT"
        meta = self._meta_after(env)
        assert "patient_exit" not in meta

    # ── 场景 7: 深水下 (>1%) → 超出作用域, DEAD_SIGNAL_EXIT 立即平 ──
    def test_deep_underwater_not_armed(self, env):
        self._insert_open_position(env, held_min=120)
        self._set_quote(env, "US.TEST", 98.4)  # 水下 1.6% > 1.0%
        n = monitor_playbook_sim_exits(env["scheduler"])
        assert n == 1
        assert env["close_calls"][0]["exit_reason"] == "DEAD_SIGNAL_EXIT"

    # ── 场景 8: SHORT 方向对称 — armed 后价格回落 → fill ──
    def test_short_armed_fills_symmetrically(self, env):
        self._insert_open_position(
            env, direction="SHORT", entry_price=100.0, held_min=80, stop_loss=104.0,
            extra_meta=self._armed_meta(minutes_ago=10, immediate_exit_price=100.5,
                                        _now=env["fixed_now"]))
        self._set_quote(env, "US.TEST", 99.8)  # SHORT 有利 0.2%
        n = monitor_playbook_sim_exits(env["scheduler"])
        assert n == 1
        call = env["close_calls"][0]
        assert call["exit_reason"] == "PATIENT_EXIT_FILL"
        # SHORT pnl = (entry - exit) × qty = (100 - 99.8) × 10 = +2.0
        assert call["pnl"] == pytest.approx(2.0, abs=0.05)

    # ── 场景 9: armed 等待中 (未触发任何腿) → 不平仓, 状态保留 ──
    def test_armed_waiting_no_action(self, env):
        # 冻结时钟=当日正午 ET, 默认 deadline=1550 尚未到点; 2359 兜底保留作双保险.
        env["cfg"]._d["patient_exit_deadline_hhmm"] = 2359
        self._insert_open_position(env, held_min=80,
                                   extra_meta=self._armed_meta(_now=env["fixed_now"]))
        self._set_quote(env, "US.TEST", 99.6)  # 水下 0.4%: 未回本/未止损/未到点
        n = monitor_playbook_sim_exits(env["scheduler"])
        assert n == 0
        assert env["close_calls"] == []
        meta = self._meta_after(env)
        assert "patient_exit" in meta
        assert meta["patient_exit"].get("exit_mode") is None
