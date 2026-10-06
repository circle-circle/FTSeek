"""B1 通道心跳 ping 抑制窗契约钉 (2026-10-06 推送治理批).

病灶: `HealthMonitor` 每小时对每个可用通道发一条「FTSeek-US 通道心跳 / 系统正常」
  (Level.INFO)。实测 41 天窗口 710 条 = 17.3 条/天, 占钉钉侧推送的 36%。
  它的存在理由是「防 webhook 静默失效」— 而失效兜底链实测**级别不匹配**:
    · error_alert 只认字面量 "] ERROR:" / "] CRITICAL:" (scripts/error_alert.py LEVEL_MARKERS)
    · 通道发送失败记的是 logger.warning (app/push/channels.py:61/91, notifier/dingtalk.py:451/507)
    · _degrade() 熔断降级只存在于 HealthMonitor
  ⇒ 直接关掉 push.health_check 会造成真盲区 (本批已撤回该建议)。
修法: 保留探测, 但「距上次业务消息成功外发 < 抑制窗」时跳过 ping —
  业务推送成功本身就是该通道 webhook 可用的直接证据, 此时 ping 纯属冗余噪音。

本文件钉三条不可回退的契约:
  ① 抑制只在有业务成功证据时发生 (无证据 ⇒ ping 照发, 盲区不放大);
  ② **ping 自身不得记为证据** (否则第一条 ping 成功后永远被抑制 = 探测彻底失效,
     自我抑制环);
  ③ 熔断半开恢复路径不被抑制窗吞掉 (恢复后若通道其实是死的, 必须能再发现)。
"""
import time

import pytest

from app.push.health import HealthMonitor
from app.push.message import Category, Level, PushMessage


class _RecordingChannel:
    """记录 send 的通道桩 (conftest 已封死真实网络出口, 这里再显式不外发)."""

    name = "stub_dingtalk"
    priority = 1

    def __init__(self, ok: bool = True):
        self.sent = []
        self._ok = ok
        self._available = True

    def is_available(self) -> bool:
        return self._available

    def send(self, msg) -> bool:
        self.sent.append(msg)
        return self._ok


@pytest.fixture
def ch():
    return _RecordingChannel(ok=True)


def _ping(hm: HealthMonitor):
    """跑一轮 _ping_all (不启动线程, 直接调用被测单元)."""
    hm._ping_all()


# ══════════ ① 抑制只在有业务成功证据时发生 ══════════
def test_no_evidence_means_ping_still_sent(ch):
    """无业务成功证据 ⇒ ping 必须照发 (抑制不得放大失效盲区)."""
    hm = HealthMonitor([ch], suppress_window_sec=14400)
    _ping(hm)
    assert len(ch.sent) == 1, "无证据时被抑制 = 探测盲区放大"
    assert ch.sent[0].category == Category.HEALTH


def test_business_success_within_window_suppresses_ping(ch):
    """窗口内有业务成功 ⇒ 跳过 ping, 且计入可观测计数."""
    hm = HealthMonitor([ch], suppress_window_sec=14400)
    hm.note_success(ch.name)
    _ping(hm)
    assert ch.sent == [], "业务推送已证明 webhook 活着, ping 应被抑制"
    st = hm.get_status()
    assert st["suppress_window_sec"] == 14400
    row = [c for c in st["channels"] if c["name"] == ch.name][0]
    assert row["ping_suppressed"] == 1, "抑制必须可观测 (部署后按此计数核实收益)"
    assert row["last_business_ok"] > 0


def test_business_success_outside_window_does_not_suppress(ch):
    """证据超过窗口 ⇒ ping 照发 (盲区上界 = 抑制窗, 不叠加)."""
    hm = HealthMonitor([ch], suppress_window_sec=60)
    hm.note_success(ch.name)
    hm._last_business_ok[ch.name] = time.time() - 120  # 2min 前 > 60s 窗
    _ping(hm)
    assert len(ch.sent) == 1, "过期证据仍被当活性 = 盲区超过声明的窗口"


def test_zero_window_disables_suppression(ch):
    """suppress_window_sec=0 ⇒ 回到原行为 (每小时必发), 提供无风险回退."""
    hm = HealthMonitor([ch], suppress_window_sec=0)
    hm.note_success(ch.name)
    _ping(hm)
    assert len(ch.sent) == 1, "=0 必须完全关闭抑制 (回滚开关)"
    row = [c for c in hm.get_status()["channels"] if c["name"] == ch.name][0]
    assert row["ping_suppressed"] == 0


# ══════════ ② ping 自身不得记为证据 (自我抑制环) ══════════
def test_ping_success_does_not_self_suppress(ch):
    """连续多轮 ping: 若 ping 成功被记成证据, 第2轮起永远静默 = 探测彻底失效."""
    hm = HealthMonitor([ch], suppress_window_sec=14400)
    for _ in range(3):
        _ping(hm)
    assert len(ch.sent) == 3, (
        "ping 自身成功被当成活性证据 ⇒ 自我抑制环, 只有第一条 ping 后探测永久停摆"
    )
    assert hm._last_business_ok == {}, "ping 路径不得写业务证据表"


def test_hub_does_not_record_evidence_for_health_category():
    """接线层契约: hub 只对非 HEALTH 类消息记证据.

    与 ② 同源约束, 钉在 hub 侧 — 因为 HealthMonitor 无法知道调用者意图,
    排除必须在 _immediate_send 的成功分支上做 (见 hub.py 注释)。
    """
    import inspect

    from app.push import hub as hub_mod

    src = inspect.getsource(hub_mod.PushHub._immediate_send)
    assert "note_success" in src, "hub 未把业务成功回传给 HealthMonitor ⇒ 抑制永不生效"
    assert "Category.HEALTH" in src, (
        "hub 未排除 HEALTH 类 ⇒ ping 成功会自我记证据 (自我抑制环)"
    )


# ══════════ ③ 熔断半开恢复不被抑制窗吞掉 ══════════
def test_degrade_recovery_probe_not_suppressed(ch):
    """熔断冷却到期后的半开探测必须真发一条 (否则死通道永远发现不了)."""
    hm = HealthMonitor([ch], suppress_window_sec=14400)
    hm.note_success(ch.name)          # 有新鲜业务证据 ⇒ 普通轮次会被抑制
    # 模拟: 通道已熔断且冷却到期
    hm._degraded[ch.name] = time.time() - (HealthMonitor.DEGRADE_RECOVER_SEC + 10)
    _ping(hm)
    assert len(ch.sent) == 1, "半开恢复探测被抑制窗吞掉 ⇒ 恢复后若通道仍死则永不再发现"
    assert ch.name not in hm._degraded, "探测成功后应清除熔断标记"
    assert hm._fail_count[ch.name] == 0


def test_recovery_probe_flag_does_not_leak():
    """多通道: 前一个通道的半开恢复不得让后续通道的抑制失效.

    `_recovery_probe` 是循环内局部标志; 若不在每轮开头重置, 通道A 触发恢复后
    通道B/C 会继承 True ⇒ 它们的 ping 永不被抑制 (噪音悄悄回来, 且无报错)。
    """
    a = _RecordingChannel(ok=True)
    a.name = "ch_a"
    b = _RecordingChannel(ok=True)
    b.name = "ch_b"
    hm = HealthMonitor([a, b], suppress_window_sec=14400)
    hm.note_success("ch_a")
    hm.note_success("ch_b")
    # a 处于「冷却到期」⇒ 本轮 a 走半开探测 (豁免抑制), b 必须仍被抑制
    hm._degraded["ch_a"] = time.time() - (HealthMonitor.DEGRADE_RECOVER_SEC + 10)
    _ping(hm)
    assert len(a.sent) == 1, "半开恢复通道应真发验证 ping"
    assert b.sent == [], "标志泄漏 ⇒ 未恢复的通道也被放行, 抑制静默失效"


def test_dead_channel_still_degrades_when_suppressed_window_active(ch):
    """抑制窗开启时, 真失效仍要走完整降级链 (探测能力不减的核心保证)."""
    dead = _RecordingChannel(ok=False)
    hm = HealthMonitor([dead], suppress_window_sec=14400)
    for _ in range(HealthMonitor.DEGRADE_THRESHOLD):
        _ping(hm)          # 无业务证据 ⇒ 每轮都发, 每轮都失败
    assert dead.name in hm._degraded, "连续失败达阈值必须熔断 (原 DEB-20260903 #111 行为)"
    assert dead._available is False, "熔丝必须标记不可用"
    assert len(dead.sent) == HealthMonitor.DEGRADE_THRESHOLD


# ══════════ 配置面契约 ══════════
def test_config_declares_ping_suppress_window():
    """yaml 必须声明该键 (代码默认 0=关闭, 靠 yaml 打开).

    钉住是为了防「配置面删键 ⇒ 静默回退到每小时必发」这种无声回滚 —
    与 D3 反向普查 (send_retries) 同一纪律。
    """
    from pathlib import Path

    cfg = (Path(__file__).resolve().parents[1] / "config" / "unified_config.yaml").read_text(
        encoding="utf-8"
    )
    assert "ping_suppress_window_sec" in cfg, (
        "yaml 未声明 push.ping_suppress_window_sec ⇒ 抑制静默关闭 (回退到 17.3 条/天)"
    )


def test_get_push_hub_reads_suppress_window():
    """接线完整性: get_push_hub 必须把 yaml 值传进 PushHub (断头传感防线)."""
    import inspect

    from app.push import hub as hub_mod

    src = inspect.getsource(hub_mod.get_push_hub)
    assert "push.ping_suppress_window_sec" in src, "get_push_hub 未读该配置键"
    assert "ping_suppress_window_sec=ping_suppress" in src, "读到但未传入 PushHub 构造"
