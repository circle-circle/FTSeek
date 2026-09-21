"""统一推送渠道 + 阶段内容 契约测试 (2026-09-18 用户裁「统一推送渠道 + 保持各阶段真实内容」)。

覆盖:
  1. 交易类 (成交/平仓) 双发全部通道; 非交易类仍是"主通道成功即停"
  2. broadcast_trade_events=false 时交易类也退回单通道 (开关真的生效)
  3. 阶段行渲染 (盘前/盘中/盘后 + 别名) 与 **不臆造** (无 session → 不出现阶段行)
  4. 延长时段 (盘前/盘后) 专属字段不丢: 财报超预期 / 阶段涨跌 / EXT 量 / 止损来源 /
     SIM 无券商 STOP 兜底提示
  5. push_text_safe 异常永不上抛; HubTextAdapter 保留旧调用形状

病灶背景 (实测 2026-09-18 US.WBD 18:35 ET 盘后建仓): 成交推送走飞书直连、平仓走
PushHub→钉钉, 同一笔交易落在两个平台; 且文案硬编码"盘前事件开仓", 把盘后说成盘前。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.push import (  # noqa: E402
    PushHub, PushMessage, Level, Category,
    trade_entry, trade_exit, push_text_safe, HubTextAdapter,
)
from app.push.channels import Channel  # noqa: E402

# tests/conftest.py 有 autouse fixture 统一封死"测试→生产通知出口" (把 PushHub.push 换掉)。
# 路由类用例必须断言真实 push() 行为 ⇒ 在**模块导入时**抓取未被替换的实现
# (fixture 在用例期才执行, 导入期这里拿到的还是真身), 用例内用 monkeypatch 装回。
_REAL_PUSH = PushHub.push


@pytest.fixture(autouse=True)
def _restore_real_push(monkeypatch):
    """恢复真实 PushHub.push (仅本模块; conftest 的封口在别的用例仍生效)。"""
    monkeypatch.setattr(PushHub, "push", _REAL_PUSH, raising=True)


class _StubChannel(Channel):
    """记录收到的消息, 永远发送成功。"""

    def __init__(self, name, priority, ok=True):
        self.name = name
        self.priority = priority
        self._ok = ok
        self.sent = []

    def is_available(self) -> bool:
        return True

    def send(self, msg: PushMessage) -> bool:
        self.sent.append(msg)
        return self._ok


def _hub(p0=True, p1=True, broadcast=True, retries=0):
    c1 = _StubChannel("primary", 1, ok=p0)
    c2 = _StubChannel("backup", 2, ok=p1)
    return PushHub(channels=[c1, c2], async_mode=False, enable_health_check=False,
                   send_retries=retries, broadcast_trade_events=broadcast), c1, c2


# ── 1/2. 路由 ────────────────────────────────────────────────────────────────

def test_trade_entry_broadcasts_to_all_channels():
    hub, c1, c2 = _hub()
    hub.push(trade_entry("US.WBD", "BUY", 167, 29.70, session="afterhours"), sync=True)
    assert len(c1.sent) == 1, "主通道未收到成交推送"
    assert len(c2.sent) == 1, "备用通道未收到成交推送 (交易类双发失效)"


def test_non_trade_still_primary_only():
    hub, c1, c2 = _hub()
    hub.push(PushMessage(title="日报", body="x", level=Level.NORMAL,
                         category=Category.REVIEW_DAILY), sync=True)
    assert len(c1.sent) == 1
    assert len(c2.sent) == 0, "非交易类不应双发 (会噪音翻倍)"


def test_trade_broadcast_can_be_switched_off():
    hub, c1, c2 = _hub(broadcast=False)
    hub.push(trade_entry("US.WBD", "BUY", 10, 10.0), sync=True)
    assert len(c1.sent) == 1
    assert len(c2.sent) == 0, "broadcast_trade_events=false 未生效"


def test_broadcast_does_not_depend_on_primary_success():
    """主通道失败时也应广播到备用通道 (而不是仅降级到备用)。"""
    hub, c1, c2 = _hub(p0=False)
    hub.push(trade_exit("US.WBD", "BUY", 56.78, "SL_TRAIL_LOCK"), sync=True)
    assert len(c1.sent) >= 1, "主通道未被尝试"
    assert len(c2.sent) == 1, "主通道失败时备用通道未收到 (广播失效)"


# ── 3. 阶段行 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("session,expect", [
    ("premarket", "阶段: 盘前"),
    ("afterhours", "阶段: 盘后"),
    ("after-hours", "阶段: 盘后"),   # 别名归一
    ("intraday", "阶段: 盘中"),
    ("regular", "阶段: 盘中"),
])
def test_stage_line_rendered_for_each_phase(session, expect):
    msg = trade_entry("US.WBD", "BUY", 167, 29.70, session=session)
    assert expect in msg.render_text(), f"{session} 未渲染出 {expect}"
    assert expect.replace("阶段: ", "阶段: ") in msg.render_markdown()


def test_exit_push_also_carries_stage():
    msg = trade_exit("US.WBD", "BUY", 56.78, "SL_TRAIL_LOCK", session="afterhours")
    md = msg.render_markdown()
    assert "阶段: 盘后" in md and "口径: " in md


def test_no_session_means_no_stage_line():
    """无阶段信息时不臆造 (绝不默认成"盘中")。"""
    msg = trade_entry("US.WBD", "BUY", 1, 1.0)
    assert "阶段" not in msg.render_text()


# ── 4. 延长时段专属内容不丢 ──────────────────────────────────────────────────

def test_eth_specific_fields_survive_rich_card():
    msg = trade_entry(
        "US.WBD", "BUY", 167, 29.70, strategy="extended_hours_entry",
        sl=28.21, source_type="playbook", session="afterhours",
        extra={
            "earnings_surprise_pct": 142.343, "ext_change_pct": 7.194,
            "ext_volume": 5383960, "stop_source": "规则引擎 initial_sl (-5%)",
            "sim_no_broker_stop": True, "planned_entry": 29.55,
            "entry_dev_pct": 0.5, "extra_lines": ["🆔 延长时段持仓: EXT-4553c8388794"],
        },
    )
    for text in (msg.render_text(), msg.render_markdown()):
        assert "财报超预期: +142.3%" in text
        assert "盘后涨跌: +7.19%" in text      # 阶段标签随 session 走 (不是硬编码盘前)
        assert "5,383,960" in text
        assert "规则引擎 initial_sl (-5%)" in text
        assert "monitor_playbook_sim_exits" in text
        assert "EXT-4553c8388794" in text
        assert "$28.21" in text                 # 止损线


def test_premarket_field_label_follows_session():
    msg = trade_entry("US.X", "BUY", 1, 10.0, session="premarket",
                      extra={"ext_change_pct": 2.0})
    assert "盘前涨跌: +2.00%" in msg.render_text()


# ── 5. SSOT 出口 ─────────────────────────────────────────────────────────────

def test_push_text_safe_never_raises(monkeypatch):
    import app.push.hub as hubmod

    def _boom(*_a, **_kw):
        raise RuntimeError("hub 炸了")

    monkeypatch.setattr(hubmod, "get_push_hub", _boom)
    assert push_text_safe("x") is False, "通知异常必须吞掉 (不得影响交易主流程)"


def test_hub_text_adapter_keeps_legacy_shapes():
    msgs = []

    class _FakeHub:
        # sync 参数镜像 2026-09-21 的真实签名 (RFC-085·续2): 默认必须为 False —
        #   14 处旧调用点 (非硬退出场景) 的异步行为不得被改动。
        def push_text(self, text, level=None, category=None, title=None, sync=False):
            msgs.append((text, title, level, category, sync))
            return True

    import app.push.hub as hubmod
    orig = hubmod.get_push_hub
    hubmod.get_push_hub = lambda: _FakeHub()
    try:
        ad = HubTextAdapter(level=Level.HIGH, category=Category.RISK_ALERT)
        assert ad.send_text("hello") is True
        assert msgs[-1][0] == "hello" and msgs[-1][2] == Level.HIGH
        assert ad.send_markdown("标题", "正文") is True
        assert msgs[-1][1] == "标题"
        assert ad.notify_system_alert(title="T", message="M", level="ERROR") is True
        assert msgs[-1][2] == Level.CRITICAL, "旧 level 字符串 ERROR 未映射到 CRITICAL"
        assert msgs[-1][4] is False, "适配器不传 sync ⇒ 必须保持异步默认"
    finally:
        hubmod.get_push_hub = orig


def test_push_text_safe_sync_passthrough(monkeypatch):
    """sync=True 必须真传到 hub.push_text。

    末尾 `os._exit()` 的脚本 (llm_universe_scan push / live_reconcile_check) 靠它同步
    送达 —— 异步 fire-and-forget 会被硬退出在 worker 投递前杀掉 (10 次丢 2-3 次)。
    """
    seen = {}

    class _FakeHub:
        def push_text(self, text, level=None, category=None, title=None, sync=False):
            seen.update(text=text, level=level, category=category, title=title, sync=sync)
            return True

    import app.push.hub as hubmod
    monkeypatch.setattr(hubmod, "get_push_hub", lambda: _FakeHub())
    assert push_text_safe("x", level=Level.HIGH, category=Category.SYSTEM,
                          title="T", sync=True) is True
    assert seen["sync"] is True and seen["level"] is Level.HIGH and seen["title"] == "T"
    assert push_text_safe("y") is True
    assert seen["sync"] is False, "不传 sync 时必须保持异步默认"


def test_hub_text_adapter_delegates_unknown_methods():
    """注入点收口的安全前提: 只收口三个文本出口, 其余方法必须原样委派。

    `container.notifier()` 被全仓当 DingTalkNotifier 用 (~30 处), 缺方法 = AttributeError
    打断主流程 ⇒ 透明代理是硬要求, 不是便利。
    """
    class _Real:
        enabled = True

        def send_order_notification(self, *a, **k):
            return "delegated"

        def some_property(self):
            return 7

    a = HubTextAdapter(category=Category.SYSTEM, inner=_Real())
    assert a.enabled is True                       # 属性委派
    assert a.send_order_notification() == "delegated"   # 方法委派
    assert a.some_property() == 7
    # 缺 inner 时按普通缺属性处理 (不静默返回 None)
    bare = HubTextAdapter()
    with pytest.raises(AttributeError):
        bare.no_such_attr


def test_di_container_notifier_is_hub_adapter():
    """DIContainer.notifier() 必须返回 Hub 适配器 (单点回收 ~30 处 self.notifier.send_text)。"""
    from app.core.di_container import DIContainer
    c = DIContainer()
    n = c.notifier()
    assert isinstance(n, HubTextAdapter)
    assert n is c.notifier(), "身份必须稳定 (scheduler.notifier 会被多处持有并比较)"
    assert "DingTalkNotifier" in type(n._inner).__name__


def test_adapter_send_text_routes_through_hub(monkeypatch):
    """适配器的 send_text 必须真的经 PushHub 出口 (而非直连旧通知器)。"""
    calls = []
    from app.push import hub as _hub
    monkeypatch.setattr(_hub, "push_text_safe",
                        lambda text, **kw: calls.append((text, kw)) or True)
    a = HubTextAdapter(category=Category.SYSTEM)
    assert a.send_text("review loop msg") is True
    assert calls and calls[0][0] == "review loop msg"


def test_phase_cn_ssot():
    from app.core.trade_identity import phase_cn
    assert phase_cn("premarket") == "盘前"
    assert phase_cn("regular") == "盘中"
    assert phase_cn("afterhours") == "盘后"
    assert phase_cn("intraday") == "盘中"
    assert phase_cn("") == ""
    assert phase_cn("nonsense") == "", "无法识别必须返回空, 不臆造"


def test_phase_from_time_et_windows():
    """按 ET 时钟判相位 (与延长时段链 SESSION_WINDOWS 同源): 盘外时段不臆造。"""
    import datetime as dt
    from zoneinfo import ZoneInfo
    from app.core.trade_identity import phase_from_time
    et = ZoneInfo("America/New_York")
    cases = [
        ("2026-09-18 05:30", "premarket"),
        ("2026-09-18 09:29", "premarket"),
        ("2026-09-18 09:30", "regular"),
        ("2026-09-18 15:59", "regular"),
        ("2026-09-18 16:00", "afterhours"),
        ("2026-09-18 19:29", "afterhours"),
        ("2026-09-18 19:59", "afterhours"),
        ("2026-09-18 20:00", ""),      # 盘外: 不臆造
        ("2026-09-18 02:00", ""),
    ]
    for s, want in cases:
        got = phase_from_time(dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=et))
        assert got == want, f"{s} ET → {got!r}, 期望 {want!r}"
    # WBD 实测时刻 (18:35 ET 盘后建仓) 必须判成盘后
    assert phase_from_time(dt.datetime(2026, 9, 18, 18, 35, tzinfo=et)) == "afterhours"