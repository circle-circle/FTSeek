"""出场观测面同源 + trail_state 跨仓污染 + ATR 失败可见化 契约 (2026-11-20).

三个病灶均来自 US.MU 实盘核查 (DEB-20261120):

R2 告警线与执行线不同源:
  `_check_approaching_stop` 自行推算 `entry − effective_atr × atr_mult`, 忽略
  hard_stop(max_loss_pct 帽)/成本缓冲/initial_sl 地板。实测 MU: 告警显示 669.47
  (距 3.2%, WARNING), 而执行线是 690.587 (距 0.71%, 应为 CRITICAL) ⇒ 接近程度被
  低估约 4.5×。且当 ATR 走 `|entry-initial_sl|/2` 代理时该公式循环恒等于 initial_sl。

R3 ATR 取数失败静默:
  MU 单日 1408 次 `[ATR底限]` 告警却查不到任何失败原因 (DEBUG 或裸 return 0.0);
  进程外同路径实调 ret=0 rows=277 ATR14=64.17 完好 ⇒ 失败在进程内被吞掉。

R4 trail_state 跨仓污染:
  store 按 symbol 单键, MU 2026-06-12 的残留 (lowest_price=977.0 /
  entry_session_ratio=1.5 / entry_session='opening_drive' /
  scale_out_original_size=10.0) 被恢复进 2026-11-18 入场的仓; 完整性检查只重置了
  lowest_price 一个字段, entry_session* 因"键已存在"而永久沿用六月值。

安全: 本文件把 PushHub 与 WebSocket 全部打桩, 绝不外发真实告警。
"""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from app.risk import position_exit_monitor as pem
from app.risk.exit_rules.trailing_stop import check_trailing_stop
from app.risk.position_exit_monitor import ExitMonitor
from app.risk.trail_state_store import is_stale_for_position

_POLICY_PATH = "app/risk/exit_policy.yaml"


@pytest.fixture
def policy_dict():
    with open(_POLICY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@pytest.fixture
def log_records():
    records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    h = _Collect(level=logging.DEBUG)
    old = pem.logger.level
    pem.logger.addHandler(h)
    pem.logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        pem.logger.removeHandler(h)
        pem.logger.setLevel(old)


@pytest.fixture
def no_external_alerts(monkeypatch):
    """把 PushHub / WebSocket 打桩 — 测试不得外发真实钉钉/飞书消息。"""
    pushed = []
    import app.push as push_mod

    class _Hub:
        def push(self, msg):
            pushed.append(msg)

    monkeypatch.setattr(push_mod, "get_push_hub", lambda: _Hub(), raising=False)
    monkeypatch.setattr(
        push_mod, "approaching_stop",
        lambda **kw: SimpleNamespace(kind="approaching_stop", **kw), raising=False,
    )
    try:
        from app.web_dashboard.routes import ws as ws_mod
        monkeypatch.setattr(ws_mod, "publish_event_sync",
                            lambda *a, **k: pushed.append(("ws", a, k)), raising=False)
    except Exception:
        pass
    return pushed


def _mk_monitor(policy_dict):
    m = ExitMonitor.__new__(ExitMonitor)
    m._cached_exit_policy = policy_dict
    m._approaching_stop_alerts = {}
    m._cost_model = None
    return m


def _mk_position(symbol="US.MU", initial_sl=669.47, size=6.0):
    return SimpleNamespace(
        symbol=symbol, initial_sl=initial_sl, size=size,
        market_regime="RANGE", trail_state={}, current_sl=initial_sl,
        owner_strategy="main", strategy_id="playbook_sim",
    )


# MU 实盘数值 (2026-11-19): entry 705.00, peak 759.57, 现价 695.49,
# ATR=0 → 代理 |705-669.47|/2 = 17.765
_MU = dict(entry=705.0, peak=759.57, cur=695.49, atr=17.765, floor=669.47)


class TestEnforcingStopSingleSource:
    def test_rule_publishes_enforcing_stop_not_the_atr_line(self, policy_dict):
        """规则写入的 enforcing_stop 必须是 hard_stop 帽绑定后的执行线, 不是 atr_stop 候选线。"""
        pos = _mk_position(initial_sl=_MU["floor"])
        ts = {"highest_price": _MU["peak"], "lowest_price": _MU["entry"]}
        guardian = SimpleNamespace(policy=policy_dict,
                                   get_atr_multiplier=lambda *a, **k: 2.0)
        profit_atr = (_MU["cur"] - _MU["entry"]) / _MU["atr"]

        out = check_trailing_stop(
            None, pos, _MU["cur"], _MU["entry"], _MU["atr"], profit_atr,
            ts, 1795119846.0, "LONG", guardian,
        )

        assert out is None, "现价 695.49 高于执行线 690.9, 不应触发"
        assert "enforcing_stop" in ts, "规则未把执行线暴露给观测面"
        # hard_stop = 705 × (1 − max_loss_pct 2%) = 690.9; cost_bps=0 (无 cost_model)
        assert ts["enforcing_stop"] == pytest.approx(690.9, abs=0.01)
        # 关键: 不是旧告警公式那条 669.47
        assert ts["enforcing_stop"] != pytest.approx(_MU["floor"], abs=0.01)

    def test_enforcing_stop_written_even_when_not_triggered(self, policy_dict):
        """未触发也要写 — 预警恰恰发生在未触发时。"""
        pos = _mk_position()
        ts = {"highest_price": _MU["peak"]}
        guardian = SimpleNamespace(policy=policy_dict,
                                   get_atr_multiplier=lambda *a, **k: 2.0)
        check_trailing_stop(None, pos, 750.0, 705.0, 17.765, 2.53, ts,
                            1795119846.0, "LONG", guardian)
        assert ts.get("enforcing_stop", 0) > 0

    def test_triggered_return_shape_unchanged(self, policy_dict):
        """加法键不得改变触发时的返回契约。"""
        pos = _mk_position(initial_sl=_MU["floor"])
        ts = {"highest_price": _MU["peak"]}
        guardian = SimpleNamespace(policy=policy_dict,
                                   get_atr_multiplier=lambda *a, **k: 2.0)
        out = check_trailing_stop(None, pos, 660.0, 705.0, 17.765, -2.53, ts,
                                  1795119846.0, "LONG", guardian)
        assert isinstance(out, dict)
        assert set(out) >= {"reason", "reason_cn", "exit_price", "pnl_pct",
                            "stop_price", "description"}
        assert out["stop_price"] == pytest.approx(ts["enforcing_stop"], abs=0.01)


class TestApproachingStopUsesEnforcingLine:
    def test_mu_real_case_alerts_on_enforcing_line_as_critical(
            self, policy_dict, log_records, no_external_alerts):
        """MU 实盘: 距执行线 0.66% ⇒ CRITICAL; 旧公式会误报 3.7% ⇒ WARNING。"""
        m = _mk_monitor(policy_dict)
        pos = _mk_position()
        ts = {"enforcing_stop": 690.9}

        m._check_approaching_stop(pos, _MU["cur"], _MU["atr"], _MU["entry"], "LONG", ts)

        warns = [r.getMessage() for r in log_records if r.levelno == logging.WARNING]
        hit = [w for w in warns if "[接近止损]" in w]
        assert hit, f"未发出预警: {warns}"
        msg = hit[0]
        assert "atr_stop=$690.90" in msg, msg
        assert "severity=CRITICAL" in msg, msg
        assert "line=enforcing" in msg, msg
        assert "669.47" not in msg, "仍在用旧的不生效线"

    def test_fallback_when_rule_produced_no_line(
            self, policy_dict, log_records, no_external_alerts):
        """规则本轮未产出线 ⇒ 回退旧公式, 但必须显式标注 line=fallback。"""
        m = _mk_monitor(policy_dict)
        pos = _mk_position()

        m._check_approaching_stop(pos, _MU["cur"], _MU["atr"], _MU["entry"], "LONG", {})

        hit = [r.getMessage() for r in log_records
               if r.levelno == logging.WARNING and "[接近止损]" in r.getMessage()]
        assert hit, "回退路径也应预警"
        assert "line=fallback" in hit[0], hit[0]
        assert "atr_stop=$669.47" in hit[0], hit[0]

    def test_no_trail_state_arg_is_backward_compatible(
            self, policy_dict, log_records, no_external_alerts):
        """不传 trail_state (旧调用签名) 不得抛异常。"""
        m = _mk_monitor(policy_dict)
        m._check_approaching_stop(_mk_position(), _MU["cur"], _MU["atr"],
                                  _MU["entry"], "LONG")
        assert any("line=fallback" in r.getMessage() for r in log_records
                   if r.levelno == logging.WARNING)

    def test_far_from_stop_stays_silent(self, policy_dict, log_records, no_external_alerts):
        m = _mk_monitor(policy_dict)
        ts = {"enforcing_stop": 600.0}   # 距 95.49 ⇒ 5.4 ATR ⇒ 不预警
        m._check_approaching_stop(_mk_position(), _MU["cur"], _MU["atr"],
                                  _MU["entry"], "LONG", ts)
        assert not [r for r in log_records
                    if r.levelno == logging.WARNING and "[接近止损]" in r.getMessage()]
        assert no_external_alerts == [], "不应外发任何告警"

    def test_invalid_enforcing_stop_falls_back(self, policy_dict, log_records,
                                              no_external_alerts):
        m = _mk_monitor(policy_dict)
        for bad in (0, -1, "690.9", None):
            m._approaching_stop_alerts.clear()
            m._check_approaching_stop(_mk_position(), _MU["cur"], _MU["atr"],
                                      _MU["entry"], "LONG", {"enforcing_stop": bad})
        hits = [r.getMessage() for r in log_records
                if r.levelno == logging.WARNING and "[接近止损]" in r.getMessage()]
        assert hits and all("line=fallback" in h for h in hits)

    def test_short_direction_uses_enforcing_line(self, policy_dict, log_records,
                                                 no_external_alerts):
        m = _mk_monitor(policy_dict)
        pos = _mk_position(symbol="US.XOM", initial_sl=168.12)
        ts = {"enforcing_stop": 163.0}
        m._check_approaching_stop(pos, 162.0, 3.0, 160.52, "SHORT", ts)
        hit = [r.getMessage() for r in log_records
               if r.levelno == logging.WARNING and "[接近止损]" in r.getMessage()]
        assert hit and "atr_stop=$163.00" in hit[0] and "line=enforcing" in hit[0]

    def test_no_real_push_escapes(self, policy_dict, log_records, no_external_alerts):
        """打桩有效性自检: 预警确实走到了推送调用, 但只落在桩上。"""
        m = _mk_monitor(policy_dict)
        m._check_approaching_stop(_mk_position(), _MU["cur"], _MU["atr"],
                                  _MU["entry"], "LONG", {"enforcing_stop": 690.9})
        assert no_external_alerts, "推送桩未被调用 ⇒ 打桩可能失效, 需检查"


class TestMuCloseCaseWithProductionCostModel:
    """锚定 2026-11-19 MU 收盘真实案例 (DEB-20261120 §5 疑点已解)。

    核查中途曾用**猜测的** cost_bps=3.0 复算得执行线 690.587, 据此以为"收盘 690.26
    低于执行线却未被平"是缺陷。用生产 CostModel 实估后更正: MU 6 股 @$690 名义仅 ~$4.1k,
    固定佣金占比高 ⇒ **cost_bps≈12.95**, 成本缓冲 1.341 ⇒ 执行线 **689.559**,
    收盘 690.26 **高于**它 ⇒ **不触发是正确行为**, 不是缺陷。
    本类用真实 CostModel 而非猜测值, 防止再次用错成本口径下结论。
    """

    @staticmethod
    def _bps(cur):
        from app.decision.cost_model import CostModel, CostModelConfig
        c = CostModel(CostModelConfig()).estimate(
            "US.MU", "SELL", 6, cur,
            market_ctx={"spread_bps": 3.0, "session": "RTH"}, asset="stock")
        return float(getattr(c, "total_bps", 0.0))

    def _lines(self, policy_dict, cur, atr, mult):
        from app.risk.atr_stop_engine import compute_stop_lines
        return compute_stop_lines(
            direction="LONG", entry_price=_MU["entry"], current_price=cur, atr=atr,
            peak_price=_MU["peak"], policy=policy_dict, regime="normal",
            atr_mult=mult, cost_bps=self._bps(cur), scale_out_floor=0.0)

    def test_close_price_does_not_trigger_and_that_is_correct(self, policy_dict):
        r = self._lines(policy_dict, 690.26, 17.765, 2.0)
        assert r.hard_stop == pytest.approx(690.9, abs=0.01)
        assert r.cost_buffer_price == pytest.approx(1.341, abs=0.01)
        assert r.final_stop == pytest.approx(689.559, abs=0.01)
        assert r.triggered is False, "收盘 690.26 高于执行线 689.559, 不应触发"
        assert 690.26 > r.final_stop

    def test_just_below_enforcing_line_triggers_hard_stop(self, policy_dict):
        r = self._lines(policy_dict, 689.50, 17.765, 2.0)
        assert r.triggered is True
        assert r.reason_code == "HARD_STOP"

    @pytest.mark.parametrize("atr,mult", [
        (17.765, 2.0), (17.765, 3.0), (64.1693, 2.0), (64.1693, 3.0),
    ])
    def test_enforcing_line_invariant_to_atr(self, policy_dict, atr, mult):
        """hard_stop(2% 帽) 绑定 ⇒ ATR=0 与否不改执行线 (更正归因的关键证据)。"""
        r = self._lines(policy_dict, 690.26, atr, mult)
        assert r.final_stop == pytest.approx(689.5588, abs=0.001)

    def test_alert_distortion_at_close_is_about_30x(self, policy_dict):
        """旧告警线 669.47 报距 3.01%, 真实执行线 689.559 距 0.10% ⇒ 低估 ~29.6×。"""
        cur = 690.26
        r = self._lines(policy_dict, cur, 17.765, 2.0)
        old_line = _MU["entry"] - 17.765 * 2.0
        assert old_line == pytest.approx(_MU["floor"], abs=0.01), "代理ATR下旧公式循环恒等 initial_sl"
        d_new = (cur - r.final_stop) / cur * 100
        d_old = (cur - old_line) / cur * 100
        assert d_new == pytest.approx(0.10, abs=0.02)
        assert d_old == pytest.approx(3.01, abs=0.02)
        assert d_old / d_new == pytest.approx(29.65, abs=0.5)


class TestStaleTrailStateGuard:
    _ET = timezone(timedelta(hours=-5))

    def test_mu_june_residue_is_rejected(self):
        """实盘病灶: 六月存储态 vs 11-18 入场的仓 ⇒ 判陈旧。"""
        entry = datetime(2026, 11, 18, 9, 56, 44, tzinfo=self._ET)
        assert is_stale_for_position("2026-06-12T23:52:51.930199", entry) is True

    def test_same_position_restart_is_kept(self):
        entry = datetime(2026, 11, 18, 9, 56, 44, tzinfo=self._ET)
        assert is_stale_for_position("2026-11-19T22:13:30", entry) is False

    def test_overnight_hold_is_kept(self):
        """隔夜持仓正是本 store 存在的目的, 不得误杀。"""
        entry = datetime(2026, 11, 18, 9, 56, 44, tzinfo=self._ET)
        assert is_stale_for_position("2026-11-19T04:30:00", entry) is False

    def test_naive_local_entry_time_is_handled(self):
        """DB 里 LLY 的 entry_time 是 naive 本地(CST) 写法。"""
        entry = datetime(2026, 11, 18, 22, 56, 43)   # naive CST
        assert is_stale_for_position("2026-06-12T23:52:51", entry) is True
        assert is_stale_for_position("2026-11-19T22:13:30", entry) is False

    def test_timezone_ambiguity_margin_prevents_false_discard(self):
        """naive 本地 vs ET-aware 最多 ~14h 口径差 ⇒ 24h 余量内不得误判陈旧。"""
        entry_naive = datetime(2026, 11, 19, 10, 0, 0)          # 当作本地 CST
        updated_et = "2026-11-19T00:30:00-05:00"                # 同日 ET
        assert is_stale_for_position(updated_et, entry_naive) is False

    def test_exactly_at_margin_boundary(self):
        entry = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
        just_inside = (entry - timedelta(hours=23)).isoformat()
        just_outside = (entry - timedelta(hours=25)).isoformat()
        assert is_stale_for_position(just_inside, entry) is False
        assert is_stale_for_position(just_outside, entry) is True

    @pytest.mark.parametrize("updated,entry", [
        (None, datetime(2026, 11, 18, tzinfo=timezone.utc)),
        ("", datetime(2026, 11, 18, tzinfo=timezone.utc)),
        ("2026-06-12T23:52:51", None),
        ("not-a-date", datetime(2026, 11, 18, tzinfo=timezone.utc)),
        ("2026-06-12T23:52:51", "not-a-date"),
    ])
    def test_undecidable_keeps_original_behavior(self, updated, entry):
        """无法判别时返回 False (不引入新的丢弃面)。"""
        assert is_stale_for_position(updated, entry) is False

    def test_custom_margin(self):
        entry = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
        updated = (entry - timedelta(hours=2)).isoformat()
        assert is_stale_for_position(updated, entry, margin_hours=1.0) is True
        assert is_stale_for_position(updated, entry, margin_hours=24.0) is False


class TestAtrFailureVisibility:
    def test_throttled_per_symbol(self, log_records):
        m = _mk_monitor({})
        m._warn_atr_failure("US.MU", "原因A")
        m._warn_atr_failure("US.MU", "原因B")
        hits = [r.getMessage() for r in log_records
                if r.levelno == logging.WARNING and "[ATR取数失败]" in r.getMessage()]
        assert len(hits) == 1, f"300s 内应只告警一次: {hits}"
        assert "原因A" in hits[0]

    def test_symbols_are_independent(self, log_records):
        m = _mk_monitor({})
        m._warn_atr_failure("US.MU", "x")
        m._warn_atr_failure("US.MSFT", "y")
        hits = [r.getMessage() for r in log_records
                if "[ATR取数失败]" in r.getMessage()]
        assert len(hits) == 2

    def test_no_quote_ctx_warns_and_returns_zero(self, log_records):
        m = _mk_monitor({})
        m._quote_ctx = None
        assert m._calc_atr_from_kline("US.MU") == 0.0
        assert any("quote_ctx 未就绪" in r.getMessage() for r in log_records
                   if r.levelno == logging.WARNING)

    def test_failed_request_warns_with_ret_and_rows(self, log_records):
        m = _mk_monitor({})

        class _Ctx:
            def request_history_kline(self, *a, **k):
                return -1, None, None

        m._quote_ctx = _Ctx()
        assert m._calc_atr_from_kline("US.MU") == 0.0
        hit = [r.getMessage() for r in log_records
               if r.levelno == logging.WARNING and "[ATR取数失败]" in r.getMessage()]
        assert hit and "ret=-1" in hit[0] and "rows=0" in hit[0], hit

    def test_too_few_rows_warns(self, log_records):
        m = _mk_monitor({})
        df = pd.DataFrame([{"high": 1.0, "low": 0.5, "close": 0.8}] * 5)

        class _Ctx:
            def request_history_kline(self, *a, **k):
                return 0, df, None

        m._quote_ctx = _Ctx()
        assert m._calc_atr_from_kline("US.MU") == 0.0
        hit = [r.getMessage() for r in log_records
               if "[ATR取数失败]" in r.getMessage()]
        assert hit and "rows=5" in hit[0], hit

    def test_exception_warns_not_debug(self, log_records):
        m = _mk_monitor({})

        class _Ctx:
            def request_history_kline(self, *a, **k):
                raise ConnectionError("OpenD gone")

        m._quote_ctx = _Ctx()
        assert m._calc_atr_from_kline("US.MU") == 0.0
        hit = [r.getMessage() for r in log_records
               if r.levelno == logging.WARNING and "[ATR取数失败]" in r.getMessage()]
        assert hit and "ConnectionError" in hit[0], hit

    def test_success_path_value_unchanged_and_silent(self, log_records):
        """可见化不得改变成功路径的 ATR 取值。"""
        m = _mk_monitor({"progressive_trailing": {"atr_floor_coef": 0.85}})
        rows = []
        for i in range(40):
            c = 100.0 + i
            rows.append({"high": c + 2.0, "low": c - 2.0, "close": c})
        df = pd.DataFrame(rows)

        class _Ctx:
            def request_history_kline(self, *a, **k):
                return 0, df, None

        m._quote_ctx = _Ctx()
        atr = m._calc_atr_from_kline("US.MU")
        assert atr == pytest.approx(4.0, abs=0.01), atr
        assert not [r for r in log_records if "[ATR取数失败]" in r.getMessage()]
