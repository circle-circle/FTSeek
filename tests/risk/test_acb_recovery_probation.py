"""AbsoluteCircuitBreaker 解除与观察机制 (2026-09-05, 用户裁"按照建议执行").

被测语义 (三件, 方向都是"既提高可用性又不放松风控"):
  ① 连亏 5 笔 → **有界冷静期** 30min (旧语义: 无到期时刻, 只由盈利平仓/次日/人工解除)
     → 到期不是全放行, 而是进入 **观察期**: 接下来 3 笔已了结交易按 0.5× 名义;
     观察期内出现亏损 → 回到冷静期 (下台阶); 3 笔无亏损 → 恢复全仓。
  ② 时亏 -$1000 → 固定 1h 之外新增**提前解除**: 冷静 ≥15min 且"触发小时口径"
     (触发瞬间累计 + 暂停期新增净额) 回升过 -$500 → 提前放行并重新武装。
  ③ 日亏 -$2000 **刻意不加恢复语义** — 锁到次日/人工。48 个交易日实测 0 次触发
     (最差日 -$793.60 = 线的 40%), 它是最后防线; 能被盘中反弹解除的最后防线不是最后防线。

立起的失效模式 (实证, 非理论): 现行制度 48 个交易日中连亏暂停触发 **11 天**
(单日最长 14 连亏), 其中 **4 天当日再无盈利平仓 → 一路锁到收盘**, 半天成交样本丢失。
旧语义下"解除"只有一条隐式路径 (任一盈利平仓), 既无冷静期也无观察期。

回滚: 删本文件 + 把 CONSEC_PAUSE_SEC 置 None (退回旧单向锁存) / git revert 本批。
"""
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import app.risk.absolute_circuit_breaker as acb_mod
from app.risk.absolute_circuit_breaker import AbsoluteCircuitBreaker
from app.utils.timezone import now_et


class _FakeClock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, **kwargs):
        self.t = self.t + timedelta(**kwargs)


@pytest.fixture
def acb(tmp_path, monkeypatch):
    """独立空台账库 + 单例复位 + 审计桩 + 固定 10:15 ET 起始时钟."""
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, exit_time TEXT, "
        "exit_price REAL, pnl_net REAL, realized_pnl REAL, source_type TEXT)"
    )
    conn.commit()
    conn.close()

    import app.core.paths as paths_mod
    monkeypatch.setattr(paths_mod, "get_trade_records_db", lambda: db)
    monkeypatch.setattr(AbsoluteCircuitBreaker, "_instance", None)

    audits = []
    monkeypatch.setattr(acb_mod, "_write_audit", lambda ev: audits.append(ev))

    clock = _FakeClock(now_et().replace(hour=10, minute=15, second=0, microsecond=0))
    monkeypatch.setattr(acb_mod, "now_et", clock)

    breaker = AbsoluteCircuitBreaker()
    # 默认隔离日亏维度 (本文件测的是连亏/时亏的解除语义)
    breaker.DAILY_MAX_LOSS = -1e9
    return breaker, clock, audits


def _events(audits, name):
    return [a for a in audits if a.get("event") == name]


# ── ① 连亏: 有界冷静期 + 观察期 (分级复权) ─────────────────────────────────

class TestConsecutivePauseAndProbation:
    def _trip(self, breaker, n=5, pnl=-10.0):
        for i in range(n):
            breaker.record_trade_result("playbook", pnl, symbol=f"S{i}")

    def test_trip_blocks_with_bounded_cooldown(self, acb):
        breaker, clock, audits = acb
        self._trip(breaker)
        allowed, reason = breaker.can_open_new_position("playbook")
        assert allowed is False and "连亏5笔" in reason
        assert "冷静期剩余" in reason, "拒绝理由必须给出剩余秒数 (可观测的解除路径)"
        assert breaker._strategy_paused_until.get("playbook") is not None, (
            "连亏暂停必须带到期时刻 (旧语义无到期 = 锁到盈利平仓或次日)")
        assert _events(audits, "consecutive_loss_triggered")[0]["paused_until"], (
            "触发审计必须记到期时刻")

    def test_still_blocked_within_cooldown(self, acb):
        breaker, clock, _ = acb
        self._trip(breaker)
        clock.advance(seconds=breaker.CONSEC_PAUSE_SEC - 60)
        assert breaker.can_open_new_position("playbook")[0] is False

    def test_cooldown_expiry_enters_probation_not_full_release(self, acb):
        """到期 ≠ 全放行: 必须先走半仓观察期 (这是"观察机制"的本体)."""
        breaker, clock, audits = acb
        self._trip(breaker)
        clock.advance(seconds=breaker.CONSEC_PAUSE_SEC + 1)
        allowed, reason = breaker.can_open_new_position("playbook")
        assert allowed is True, f"冷静期到期应放行, 实得 {reason}"
        assert breaker.get_position_scale("playbook") == pytest.approx(0.5)
        assert breaker._strategy_probation["playbook"] == breaker.CONSEC_PROBATION_TRADES
        assert _events(audits, "consecutive_pause_expired_probation"), (
            "转入观察期必须留审计痕 (否则'为什么这笔是半仓'无从追溯)")

    def test_probation_passes_after_n_wins(self, acb):
        breaker, clock, audits = acb
        self._trip(breaker)
        clock.advance(seconds=breaker.CONSEC_PAUSE_SEC + 1)
        breaker.can_open_new_position("playbook")   # 触发到期扫描 → 进观察期
        assert breaker.get_position_scale("playbook") == pytest.approx(0.5)
        for i in range(breaker.CONSEC_PROBATION_TRADES - 1):
            breaker.record_trade_result("playbook", 25.0, symbol=f"W{i}")
            assert breaker.get_position_scale("playbook") == pytest.approx(0.5), (
                "观察期未走完必须维持半仓")
        breaker.record_trade_result("playbook", 25.0, symbol="W_last")
        assert breaker.get_position_scale("playbook") == pytest.approx(1.0)
        assert not breaker._strategy_probation
        assert _events(audits, "probation_passed")

    def test_probation_loss_returns_to_cooldown(self, acb):
        """观察期内亏损 → 下台阶回冷静期, 不是继续半仓也不是全放行."""
        breaker, clock, audits = acb
        self._trip(breaker)
        clock.advance(seconds=breaker.CONSEC_PAUSE_SEC + 1)
        breaker.can_open_new_position("playbook")
        breaker.record_trade_result("playbook", 25.0, symbol="W0")   # 观察期 1/3 通过
        breaker.record_trade_result("playbook", -30.0, symbol="L0")   # 观察期失败
        assert _events(audits, "probation_failed")
        assert not breaker._strategy_probation, "观察期失败必须清空观察计数"
        assert breaker.can_open_new_position("playbook")[0] is False, (
            "观察期失败必须回到冷静期 (重新停新开)")

    def test_win_during_cooldown_releases_early(self, acb):
        """原有的隐式恢复路径保留: 冷静期内任一盈利平仓即解除 (亏损好转就放行)."""
        breaker, clock, audits = acb
        self._trip(breaker)
        assert breaker.can_open_new_position("playbook")[0] is False
        breaker.record_trade_result("playbook", 40.0, symbol="RESCUE")
        allowed, reason = breaker.can_open_new_position("playbook")
        assert allowed is True, f"盈利平仓应解除冷静期, 实得 {reason}"
        assert breaker.get_position_scale("playbook") == pytest.approx(1.0), (
            "由盈利平仓解除 = 已自证恢复, 不再叠加观察期半仓")
        assert _events(audits, "consecutive_pause_released_by_win")

    def test_none_pause_sec_restores_legacy_latch(self, acb):
        """回滚开关: CONSEC_PAUSE_SEC=None → 旧单向锁存 (到期也不放行)."""
        breaker, clock, _ = acb
        breaker.CONSEC_PAUSE_SEC = None
        self._trip(breaker)
        clock.advance(hours=6)
        assert breaker.can_open_new_position("playbook")[0] is False
        assert breaker.get_position_scale("playbook") == pytest.approx(1.0)

    def test_daily_loss_stays_latched_despite_recovery(self, acb):
        """③ 日亏 -$2000 刻意**不加**恢复语义: 赚回来也不解除 (锁到次日/人工)."""
        breaker, clock, _ = acb
        breaker.DAILY_MAX_LOSS = -2000.0
        breaker.HOURLY_MAX_LOSS = -1e9          # 隔离时亏维度
        breaker.record_trade_result("playbook", -2100.0, symbol="BIG")
        assert breaker.can_open_new_position("playbook")[0] is False
        for i in range(10):                      # 随后大幅盈利, 日累计转正
            breaker.record_trade_result("playbook", 500.0, symbol=f"W{i}")
        assert breaker._daily_pnl > 0
        allowed, reason = breaker.can_open_new_position("playbook")
        assert allowed is False, "日亏闸门一旦触发, 当日不因反弹解除 (最后防线语义)"
        assert "日亏损触发熔断" in reason
        breaker.manual_reset("test")
        assert breaker.can_open_new_position("playbook")[0] is True

    def test_manual_reset_clears_probation(self, acb):
        breaker, clock, _ = acb
        self._trip(breaker)
        clock.advance(seconds=breaker.CONSEC_PAUSE_SEC + 1)
        breaker.can_open_new_position("playbook")
        assert breaker.get_position_scale("playbook") == pytest.approx(0.5)
        breaker.manual_reset("运维放行")
        assert breaker.get_position_scale("playbook") == pytest.approx(1.0)
        assert not breaker._strategy_paused_until

    def test_status_exposes_recovery_fields(self, acb):
        breaker, clock, _ = acb
        self._trip(breaker)
        st = breaker.get_status()
        for key in ("strategy_paused_until", "strategy_probation", "probation_size_mult",
                    "hourly_pause_started", "hourly_recovery_pnl"):
            assert key in st, f"get_status 必须暴露 {key} (可观测性)"
        assert st["thresholds"]["consec_pause_sec"] == breaker.CONSEC_PAUSE_SEC
        assert st["thresholds"]["hourly_early_release_pnl"] == breaker.HOURLY_EARLY_RELEASE_PNL


# ── ② 时亏: 最短冷静期 + 提前解除 ──────────────────────────────────────────

class TestHourlyEarlyRelease:
    def _trip_hourly(self, breaker):
        breaker.HOURLY_MAX_LOSS = -1000.0
        breaker.record_trade_result("playbook", -1000.0, symbol="H0")
        assert breaker.can_open_new_position("playbook")[0] is False

    def test_no_early_release_before_min_pause(self, acb):
        """回补再快也不得短于最短冷静期 (15min) — 否则闸门等于没有."""
        breaker, clock, _ = acb
        self._trip_hourly(breaker)
        clock.advance(seconds=breaker.HOURLY_MIN_PAUSE_SEC - 60)
        breaker.record_trade_result("playbook", 800.0, symbol="R0")   # 小时口径回到 -200
        assert breaker.can_open_new_position("playbook")[0] is False

    def test_early_release_after_recovery(self, acb):
        breaker, clock, audits = acb
        self._trip_hourly(breaker)
        clock.advance(seconds=breaker.HOURLY_MIN_PAUSE_SEC + 1)
        breaker.record_trade_result("playbook", 800.0, symbol="R0")   # -1000+800 = -200 > -500
        allowed, reason = breaker.can_open_new_position("playbook")
        assert allowed is True, f"亏损已回补且过最短冷静期应提前解除, 实得 {reason}"
        assert _events(audits, "hourly_pause_early_release")

    def test_no_early_release_if_not_recovered_enough(self, acb):
        """回补不足 (仍 ≤ -$500) → 必须等满 1 小时."""
        breaker, clock, audits = acb
        self._trip_hourly(breaker)
        clock.advance(seconds=breaker.HOURLY_MIN_PAUSE_SEC + 1)
        breaker.record_trade_result("playbook", 300.0, symbol="R0")   # -1000+300 = -700 ≤ -500
        assert breaker.can_open_new_position("playbook")[0] is False
        assert not _events(audits, "hourly_pause_early_release")
        clock.advance(minutes=61)                                     # 到期照常解除 (G-03 语义)
        assert breaker.can_open_new_position("playbook")[0] is True

    def test_clock_hour_roll_does_not_fake_recovery(self, acb):
        """口径独立性: 跨自然小时把 _hourly_pnl 归零 **不等于** 亏损已回补。
        若用 _hourly_pnl 判提前解除, 10:50 触发的暂停会在 11:05 被"归零"提前放行 =
        把暂停砍短 (放松方向, 与 G-03 的语义边界同源)。"""
        breaker, clock, _ = acb
        clock.t = clock.t.replace(minute=50, second=0, microsecond=0)
        self._trip_hourly(breaker)
        clock.advance(minutes=20)          # 11:10 — 已跨小时, _hourly_pnl 被归零
        allowed, _ = breaker.can_open_new_position("playbook")   # 本次调用触发跨小时归零
        assert breaker._hourly_pnl == 0.0, "前置条件: 跨小时后计数器确已归零"
        assert allowed is False, "跨小时归零不得被当成'已回补'而提前解除"
        assert breaker._hourly_pnl_at_pause == pytest.approx(-1000.0), (
            "提前解除口径必须锚在触发瞬间的小时累计, 不随自然小时归零漂移")
        clock.advance(minutes=45)          # 触发后满 1h → 正常到期解除
        assert breaker.can_open_new_position("playbook")[0] is True

    def test_early_release_rearms_breaker(self, acb):
        """提前解除后必须重新武装: 同一小时再违例仍能再次触发."""
        breaker, clock, _ = acb
        self._trip_hourly(breaker)
        clock.advance(seconds=breaker.HOURLY_MIN_PAUSE_SEC + 1)
        breaker.record_trade_result("playbook", 800.0, symbol="R0")
        assert breaker.can_open_new_position("playbook")[0] is True
        breaker.record_trade_result("playbook", -1000.0, symbol="H1")
        assert breaker.can_open_new_position("playbook")[0] is False
