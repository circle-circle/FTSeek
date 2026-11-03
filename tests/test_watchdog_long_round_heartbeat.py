"""llm_strategy_loop 长轮心跳护航回归 (2026-09-03, DEB-20260903-intraday-chain-audit §四 D2)。

病灶: `_llm_strategy_loop` 原先只在 while 顶部 ping 一次, 而盘前/盘后单轮迭代实测 ~58min
(2026-09-03 20:02:28→21:01:09 CST · 2026-09-01 同窗 · 2026-08-22 04:00→04:30+ 盘后轮)
≫ 阈值 600s → 连报 stale → heal 整服重启 (09-03 21:31:38 = 开盘后 90s, 36s 盲窗)。

本文件钉住修法的四条语义边界:
  1. 非盘中阶段护航 (补心跳) / TRADING·WEEKEND 不护航 (保持紧检出);
  2. 预算耗尽即停 ping → 交回 watchdog (不做"永不检出"的无脑定时 ping = 静默死亡);
  3. 轮次跨入 TRADING 立即停 ping + 交回 watchdog; 跨界先 WARNING, 宽限
     (scheduler.watchdog_cross_open_grace_sec, 默认 120s) 内未收轮才升级 critical
     (2026-10-09 两阶段化, 用户裁「请优化调整」: 10-09 实证慢轮跨开盘 0.2s 自解,
     即时推送是纯噪音; 真卡死检出延迟 0s→grace, 「入场被阻塞必须可见」保留);
  4. 线程卫生: 每轮最多 1 个护航线程, 结算即 join, 不累积 (同日 D3 泄漏教训)。

回滚: 删本文件即可, 零生产代码依赖。
"""
import re
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from app.core.scheduler_core import TradingScheduler  # noqa: E402
from app.core.scheduler_triggers import MarketPhase  # noqa: E402

_SCHED_SRC = (PROJECT_ROOT / "app" / "core" / "scheduler_core.py").read_text(encoding="utf-8")
_CFG_SRC = (PROJECT_ROOT / "config" / "unified_config.yaml").read_text(encoding="utf-8")


class _FakeWatchdog:
    def __init__(self):
        self.pings = []

    def ping(self, name):
        self.pings.append((name, time.time()))


@pytest.fixture
def sched(monkeypatch):
    """最小可用 scheduler (绕开 __init__ 重依赖) + 假 watchdog + 假配置。"""
    obj = TradingScheduler.__new__(TradingScheduler)
    obj.running = True
    obj._WD_ROUND_INTERVAL_SEC = 0.02  # 测试加速 (生产 120s)
    fake_wd = _FakeWatchdog()
    monkeypatch.setattr("app.observability.watchdog.get_watchdog", lambda: fake_wd)
    cfg = {"scheduler.watchdog_long_round_enabled": True,
           "scheduler.watchdog_long_round_budget_sec": 2700}
    asked = []

    def _get_dotted(key, default=None):
        asked.append(key)
        return cfg.get(key, default)

    import app.core.config_container as cc
    monkeypatch.setattr(cc, "get_config", lambda: type("C", (), {"get_dotted": staticmethod(_get_dotted)})())
    # 阶段判定默认恒为盘前 (跨盘用例单独覆盖)
    import app.core.scheduler_triggers as st
    monkeypatch.setattr(st, "get_market_phase", lambda: MarketPhase.PRE_MARKET)
    obj._test_wd = fake_wd
    obj._test_cfg = cfg
    obj._test_asked = asked
    yield obj
    obj.running = False
    obj._wd_close_round_heartbeat()


def _keeper_alive():
    return [t for t in threading.enumerate() if t.name == "WdRoundHeartbeat" and t.is_alive()]


class TestRoundHeartbeat:
    def test_premarket_round_gets_heartbeat(self, sched):
        """盘前轮: 开护航 → 轮内持续补 ping (原实现整轮零 ping)。"""
        sched._wd_open_round_heartbeat(MarketPhase.PRE_MARKET)
        time.sleep(0.12)
        sched._wd_close_round_heartbeat()
        names = {n for n, _ in sched._test_wd.pings}
        assert names == {"llm_strategy_loop"}
        assert len(sched._test_wd.pings) >= 2

    def test_trading_phase_not_escorted(self, sched):
        """盘中轮不护航 — 入场执行在同一循环内, 卡死要紧检出 + heal。"""
        sched._wd_open_round_heartbeat(MarketPhase.TRADING)
        assert getattr(sched, "_wd_round_keeper", None) is None
        assert not _keeper_alive()

    def test_weekend_not_escorted(self, sched):
        """周末不护航 — watchdog._check_all 对 WEEKEND 本就跳过检查。"""
        sched._wd_open_round_heartbeat(MarketPhase.WEEKEND)
        assert getattr(sched, "_wd_round_keeper", None) is None

    def test_budget_exhaustion_returns_authority_to_watchdog(self, sched):
        """预算耗尽 → 停止补 ping (真卡死仍能被 watchdog 检出, 非静默死亡)。"""
        sched._test_cfg["scheduler.watchdog_long_round_budget_sec"] = 0.05
        sched._wd_open_round_heartbeat(MarketPhase.PRE_MARKET)
        time.sleep(0.1)
        frozen = len(sched._test_wd.pings)
        time.sleep(0.15)
        assert len(sched._test_wd.pings) == frozen  # 不再补 ping
        _th, _stop, _t0, _phase, state = sched._wd_round_keeper
        assert state["stopped_reason"] and "预算耗尽" in state["stopped_reason"]
        sched._wd_close_round_heartbeat()

    def test_crossing_into_trading_stops_and_alerts(self, sched, monkeypatch):
        """轮次跨入 TRADING → 停 ping; 超宽限未收轮 → critical 升级 (真卡死必须可见)。

        grace=0 ⇒ 跨界后的下一次循环即升级 (两阶段语义下钉升级路径本身)。"""
        sched._test_cfg["scheduler.watchdog_cross_open_grace_sec"] = 0
        sent = []
        import app.utils.critical_alerts as ca
        monkeypatch.setattr(ca, "send_critical",
                            lambda msg, async_mode=False: sent.append(msg))
        import app.core.scheduler_triggers as st
        phase = {"now": MarketPhase.PRE_MARKET}
        monkeypatch.setattr(st, "get_market_phase", lambda: phase["now"])
        sched._wd_open_round_heartbeat(MarketPhase.PRE_MARKET)
        time.sleep(0.06)
        phase["now"] = MarketPhase.TRADING
        time.sleep(0.12)
        frozen = len(sched._test_wd.pings)
        time.sleep(0.1)
        assert len(sched._test_wd.pings) == frozen
        _th, _stop, _t0, _phase_name, state = sched._wd_round_keeper
        assert state["stopped_reason"] and "TRADING" in state["stopped_reason"]
        assert sent and "入场执行被阻塞" in sent[0] and "真卡死" in sent[0]
        sched._wd_close_round_heartbeat()

    def test_crossing_within_grace_no_push(self, sched, monkeypatch):
        """两阶段降噪 (2026-10-09): 跨界后宽限内收轮 → 零推送, 仅 WARNING+慢轮次遥测。

        钉死 10-09 生产实证形态: 121s 慢轮跨开盘 0.2s 自解, 🚨 不得发出。"""
        sched._test_cfg["scheduler.watchdog_cross_open_grace_sec"] = 9999
        sent = []
        import app.utils.critical_alerts as ca
        monkeypatch.setattr(ca, "send_critical",
                            lambda msg, async_mode=False: sent.append(msg))
        import app.core.scheduler_triggers as st
        phase = {"now": MarketPhase.PRE_MARKET}
        monkeypatch.setattr(st, "get_market_phase", lambda: phase["now"])
        sched._wd_open_round_heartbeat(MarketPhase.PRE_MARKET)
        time.sleep(0.06)
        phase["now"] = MarketPhase.TRADING
        time.sleep(0.12)
        frozen = len(sched._test_wd.pings)
        time.sleep(0.1)
        assert len(sched._test_wd.pings) == frozen, "跨界后不得再补 ping"
        assert sent == [], "宽限内收轮不得推送 critical (10-09 噪音实证)"
        _th, _stop, _t0, _phase_name, state = sched._wd_round_keeper
        assert state.get("crossed_at"), "跨界时刻必须落态 (供宽限判定)"
        assert "TRADING" in (state.get("stopped_reason") or "")
        sched._wd_close_round_heartbeat()

    def test_disabled_by_config_restores_old_behavior(self, sched):
        """回滚阀: watchdog_long_round_enabled=false → 完全不护航 (恢复原行为)。"""
        sched._test_cfg["scheduler.watchdog_long_round_enabled"] = False
        sched._wd_open_round_heartbeat(MarketPhase.PRE_MARKET)
        assert getattr(sched, "_wd_round_keeper", None) is None
        assert not sched._test_wd.pings

    def test_no_thread_accumulation_across_rounds(self, sched):
        """线程卫生: 连续开 5 轮, 任一时刻最多 1 个护航线程; 结算后归零。"""
        for _ in range(5):
            sched._wd_open_round_heartbeat(MarketPhase.PRE_MARKET)
            time.sleep(0.03)
            assert len(_keeper_alive()) == 1
        sched._wd_close_round_heartbeat()
        time.sleep(0.05)
        assert not _keeper_alive()

    def test_close_is_idempotent(self, sched):
        """重复结算不抛 (轮顶每圈都调)。"""
        sched._wd_open_round_heartbeat(MarketPhase.PRE_MARKET)
        sched._wd_close_round_heartbeat()
        sched._wd_close_round_heartbeat()
        assert not _keeper_alive()

    def test_stops_when_scheduler_stopping(self, sched):
        """停机 (running=False) 时护航线程自终止 — 关闭期不留心跳假象。"""
        sched._wd_open_round_heartbeat(MarketPhase.PRE_MARKET)
        time.sleep(0.05)
        sched.running = False
        time.sleep(0.12)
        frozen = len(sched._test_wd.pings)
        time.sleep(0.1)
        assert len(sched._test_wd.pings) == frozen
        sched._wd_close_round_heartbeat()


class TestWiringAndConfigContract:
    def test_loop_top_settles_previous_round_then_pings(self):
        """源码契约: 轮顶必须"结算上一轮 → ping → 开本轮护航", 否则修法被静默摘掉。"""
        loop = _SCHED_SRC.split("def _llm_strategy_loop", 1)[1]
        loop = loop.split("\n    def ", 1)[0]
        i_close = loop.index("self._wd_close_round_heartbeat()")
        i_ping = loop.index("self._wd_ping()")
        i_open = loop.index("self._wd_open_round_heartbeat(phase)")
        assert i_close < i_ping < i_open

    def test_loop_exit_settles_last_round(self):
        """源码契约: 循环退出后仍结算一次 (不留守护线程)。"""
        tail = _SCHED_SRC.split("def _llm_strategy_loop", 1)[1].split("\n    def ", 1)[0]
        assert tail.rstrip().endswith("self._wd_close_round_heartbeat()")

    def test_threshold_registration_is_config_driven(self):
        """源码契约: llm_strategy_loop 阈值走配置键, 不再硬编码 600。"""
        assert re.search(
            r'wd\.register\("llm_strategy_loop",\s*stale_threshold_sec=_llm_loop_stale_sec\)',
            _SCHED_SRC)
        assert 'cfg.get_dotted("scheduler.watchdog_llm_loop_stale_sec", 600)' in _SCHED_SRC

    def test_config_keys_present_with_documented_defaults(self):
        """三个键必须落在 YAML (SSOT), 且默认值与本文件语义一致。"""
        import yaml
        cfg = yaml.safe_load(_CFG_SRC)["scheduler"]
        assert cfg["watchdog_llm_loop_stale_sec"] == 600  # 默认不放宽检出
        assert cfg["watchdog_long_round_enabled"] is True
        assert cfg["watchdog_long_round_budget_sec"] == 2700
        assert cfg["watchdog_cross_open_grace_sec"] == 120  # 跨界告警宽限 (2026-10-09)
        for key in ("watchdog_llm_loop_stale_sec", "watchdog_long_round_enabled",
                    "watchdog_long_round_budget_sec", "watchdog_cross_open_grace_sec"):
            assert key in _SCHED_SRC or key in _CFG_SRC

    def test_runtime_reads_the_same_keys(self, sched):
        """运行期读的键名与 YAML 一致 (防拼写漂移 → 配置改了但代码读不到)。"""
        sched._wd_open_round_heartbeat(MarketPhase.PRE_MARKET)
        sched._wd_close_round_heartbeat()
        assert "scheduler.watchdog_long_round_enabled" in sched._test_asked
        assert "scheduler.watchdog_long_round_budget_sec" in sched._test_asked


class TestEndToEndAgainstRealWatchdog:
    """用**真 Watchdog 代码**按 1:1200 缩放复现 2026-09-03 那次误重启的时序。

    生产: 阈值 600s · 护航间隔 120s · 单轮 58min (3480s) · heal=阈值×3。
    缩放: 阈值 0.60s · 护航间隔 0.12s · 单轮 2.90s。
    安全: 用独立 Watchdog 实例 (不动全局单例) + _heal_enabled=False + WATCHDOG_AUTO_HEAL=0
          + send_critical 打桩 → 绝不发推送、绝不 systemctl restart。
    """

    def _run_round(self, sched, monkeypatch, wd, escorted, round_sec=2.9):
        alerts = []
        # 端到端: 让 _wd_ping 打到这个真 Watchdog 实例 (fixture 的假 watchdog 在此让位)
        monkeypatch.setattr("app.observability.watchdog.get_watchdog", lambda: wd)
        import app.utils.critical_alerts as ca
        monkeypatch.setattr(ca, "send_critical",
                            lambda msg, async_mode=False: alerts.append(msg))
        monkeypatch.setenv("WATCHDOG_AUTO_HEAL", "0")
        wd._heal_enabled = False          # 双保险: 测试里绝不允许自愈重启
        wd._init_grace_sec = 0.0
        wd._alert_cooldown = 0.0
        sched._test_cfg["scheduler.watchdog_long_round_enabled"] = escorted
        sched._test_cfg["scheduler.watchdog_long_round_budget_sec"] = 5.0  # 缩放后 > 轮次时长

        if escorted:
            sched._wd_open_round_heartbeat(MarketPhase.PRE_MARKET)
        t_end = time.time() + round_sec
        while time.time() < t_end:
            wd._check_all()          # 真 watchdog 的 stale 判定逻辑
            time.sleep(0.05)
        sched._wd_close_round_heartbeat()
        return [m for m in alerts if "无心跳" in m]

    def _make_wd(self, threshold=0.60):
        from app.observability.watchdog import Watchdog
        wd = Watchdog()
        wd.register("llm_strategy_loop", stale_threshold_sec=threshold)
        return wd

    def test_escorted_round_produces_no_false_stale(self, sched, monkeypatch):
        """修法生效: 58min 慢轮次 (缩放 2.9s) 期间零 stale 告警 → 不会误 heal 重启。"""
        sched._WD_ROUND_INTERVAL_SEC = 0.12
        assert self._run_round(sched, monkeypatch, self._make_wd(), escorted=True) == []

    def test_unescorted_round_reproduces_the_incident(self, sched, monkeypatch):
        """对照组: 同一条轮次关掉护航 → 复现 09-03 的 stale 误报 (证明测试确实在压这条路径)。"""
        sched._WD_ROUND_INTERVAL_SEC = 0.12
        assert len(self._run_round(sched, monkeypatch, self._make_wd(), escorted=False)) >= 1


class TestNoSelfHealDuringTrading:
    """2026-11-03 (全链审计 P1-3): **盘中不做自愈重启**。

    背景: `llm_strategy_loop` 的心跳只由主循环**轮首**推进 ⇒ 盘中任一同步长步骤
    (定桶实测 31.4min = 1884s > 阈值 600s × 3 = 1800s) 就会被 watchdog 判成"主循环卡死"
    ⇒ 旧实现直接 `systemctl restart ftseek-us` ⇒ **盘中重启 = 内存态持仓与在途委托全丢**
    (比慢一轮更糟)。真冻结仍由**外部** freeze_sentinel (cron 每分钟) SIGKILL 处置。

    本类钉住: 盘中 ⇒ 只告警不重启; 非盘中 ⇒ 仍按原逻辑重启 (不是一刀切禁用自愈)。
    """

    def _wd(self, monkeypatch, phase):
        import app.core.scheduler_triggers as st
        from app.observability.watchdog import Watchdog
        monkeypatch.setattr(st, "get_market_phase", lambda: phase)
        wd = Watchdog()
        wd._heal_enabled = True
        wd._heal_last_attempt = 0.0
        wd._heal_max_per_day = 3
        return wd

    def test_trading_phase_does_not_restart(self, monkeypatch):
        wd = self._wd(monkeypatch, MarketPhase.TRADING)
        restarts = []
        alerts = []
        monkeypatch.setattr("subprocess.run",
                            lambda *a, **k: restarts.append(a))
        import app.utils.critical_alerts as ca
        monkeypatch.setattr(ca, "send_critical",
                            lambda msg, async_mode=False: alerts.append(msg))

        wd._self_heal("llm_strategy_loop", 1900.0, 600.0)

        time.sleep(0.2)   # 给 (不该存在的) 重启线程一点机会
        assert not restarts, f"盘中不得自动重启交易服务 (会丢内存态持仓/在途委托): {restarts}"
        assert not wd._heal_history, "盘中不入 heal 历史 (不该消耗当日自愈配额)"
        assert any("盘中未自动重启" in m for m in alerts), (
            f"盘中不重启必须留一条可见告警 (值班人要知道): {alerts}")

    def test_non_trading_phase_still_heals(self, monkeypatch):
        """反向钉住: 非盘中仍按原逻辑重启 (自愈不能被一刀切关掉)。"""
        wd = self._wd(monkeypatch, MarketPhase.AFTER_HOURS)
        restarts = []
        monkeypatch.setattr("subprocess.run", lambda *a, **k: restarts.append(a))
        import app.utils.critical_alerts as ca
        monkeypatch.setattr(ca, "send_critical",
                            lambda msg, async_mode=False: None)

        wd._self_heal("llm_strategy_loop", 1900.0, 600.0)
        time.sleep(0.3)
        assert restarts, "非盘中仍应自愈重启 (真卡死靠它兜底)"
        assert any("restart" in str(a) for a in restarts), f"应是 systemctl restart: {restarts}"
