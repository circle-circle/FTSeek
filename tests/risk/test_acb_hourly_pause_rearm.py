"""AbsoluteCircuitBreaker 小时熔断"到期重新武装"回归测试 (2026-09-05 审计 G-03).

背景: 本类自称"最后防线, 不受任何 feature flag 控制"。但小时熔断实为**每日一次**:
  - record_trade_result 的触发条件含 `not self._hourly_paused_until`;
  - 该字段此前仅在 _check_new_day / manual_reset 清零;
  - can_open_new_position 只做 `now < paused_until` 判断, 到期后**从不回写清零**。
→ 首次触发后当日永久解除武装, 且暂停期满零告警 (防线静默失效, 与 G-13 同族)。
另一入口: _restore_from_db 启动重建亦会预置该字段 → 一次重启即消耗当日唯一配额。

修复 = _check_new_hour 内按"到期"清零 + WARNING + audit 事件 hourly_pause_expired。
语义边界: 按到期而非跨小时清零 — 10:15 触发的 1h 暂停须完整持续到 11:15,
若在 11:00 跨小时即清零会把暂停砍短 15min (放松方向, 不可接受)。

覆盖点:
  1. 首次触发 → 停新仓 (基线行为不变)
  2. 暂停期内跨小时 → 仍停 (不被 _check_new_hour 提前放松)
  3. **到期后再违例 → 再次触发停新仓 (本修复的核心回归锁)**
  4. 到期清零留 WARNING + audit 事件 (防"静默失效"复发)
  5. 日亏维度不受本修复影响 (仍独立触发)

回滚: 删本文件 + 移除 absolute_circuit_breaker._check_new_hour 内到期清零块即可。
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
    """可控 ET 时钟 (保持 aware, 与 now_et 同口径)."""

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
    # 隔离小时维度: 日亏硬限调到不可能触发, 避免两次 -1000 违例顺带触发日亏暂停
    breaker.DAILY_MAX_LOSS = -1e9
    return breaker, clock, audits


def _breach_hourly(breaker, pnl=-1000.0):
    breaker.record_trade_result("playbook", pnl, "US.TEST")


class TestHourlyPauseRearm:
    def test_first_breach_blocks_new_position(self, acb):
        breaker, _clock, _audits = acb
        assert breaker.can_open_new_position("playbook")[0] is True
        _breach_hourly(breaker)
        allowed, reason = breaker.can_open_new_position("playbook")
        assert allowed is False
        assert "小时亏损触发熔断" in reason

    def test_hour_rollover_does_not_shorten_pause(self, acb):
        """10:15 触发的暂停须撑到 11:15 — 跨小时 (11:00) 不得提前放松."""
        breaker, clock, _audits = acb
        _breach_hourly(breaker)
        clock.advance(minutes=45)  # → 11:00, 已进入新小时窗
        assert breaker.can_open_new_position("playbook")[0] is False, (
            "跨小时即清零会把 1h 暂停砍短 (放松方向), 必须按到期清零"
        )

    def test_second_breach_after_expiry_rearms(self, acb):
        """核心回归锁: 到期后再次违例必须能再次触发 (修复前当日永不再触发)."""
        breaker, clock, _audits = acb
        _breach_hourly(breaker)
        assert breaker.can_open_new_position("playbook")[0] is False

        clock.advance(minutes=61)  # → 11:16, 暂停已到期
        allowed, _reason = breaker.can_open_new_position("playbook")
        assert allowed is True, "暂停到期后应放行新仓"
        assert breaker._hourly_paused_until is None, "到期必须回写清零 (否则触发条件永假)"

        _breach_hourly(breaker)  # 第二次违例
        allowed2, reason2 = breaker.can_open_new_position("playbook")
        assert allowed2 is False, (
            "到期后第二次小时违例必须再次熔断 — 修复前此处为 True (防线已静默解除)"
        )
        assert "小时亏损触发熔断" in reason2

    def test_third_breach_also_rearms(self, acb):
        """连锁: 到期→违例 可重复多轮, 非"每日两次"."""
        breaker, clock, _audits = acb
        for _round in range(3):
            _breach_hourly(breaker)
            assert breaker.can_open_new_position("playbook")[0] is False
            clock.advance(minutes=61)
            assert breaker.can_open_new_position("playbook")[0] is True

    def test_expiry_is_observable(self, acb):
        """到期清零必须留 WARNING + audit 事件 — 防"防线静默失效"复发 (G-13 同族)."""
        breaker, clock, audits = acb
        _breach_hourly(breaker)
        clock.advance(minutes=61)
        breaker.can_open_new_position("playbook")
        events = [a for a in audits if a.get("event") == "hourly_pause_expired"]
        assert len(events) == 1, f"到期应写一条 hourly_pause_expired, 实得 {[a.get('event') for a in audits]}"
        assert events[0]["threshold"] == breaker.HOURLY_MAX_LOSS
        assert "expired_until" in events[0]

    def test_no_expiry_event_while_still_paused(self, acb):
        breaker, clock, audits = acb
        _breach_hourly(breaker)
        clock.advance(minutes=10)  # 仍在暂停期内
        breaker.can_open_new_position("playbook")
        assert not [a for a in audits if a.get("event") == "hourly_pause_expired"]

    def test_daily_dimension_unaffected(self, acb):
        """本修复只动小时维度: 日亏暂停仍独立生效且不被小时到期清掉."""
        breaker, clock, _audits = acb
        breaker.DAILY_MAX_LOSS = -500.0
        breaker.record_trade_result("playbook", -600.0, "US.TEST")
        assert breaker.can_open_new_position("playbook")[0] is False
        clock.advance(minutes=120)  # 小时暂停(若有)早已到期
        allowed, reason = breaker.can_open_new_position("playbook")
        assert allowed is False, "日亏暂停须持续到下一交易日或人工重置"
        assert "日亏损触发熔断" in reason
