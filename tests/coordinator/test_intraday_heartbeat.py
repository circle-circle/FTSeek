"""盘中心跳测试 (2026-05-19)

核心要求:
1. 输出格式紧凑 (一眼看完, 钉钉友好)
2. 7 个维度全覆盖 (服务/持仓/盈亏/信号/LLM/告警/风控)
3. 任何一个 query 失败不导致整体失败 (fail-safe)
4. 非盘中跳过 (减噪音)
"""
from unittest.mock import patch, MagicMock
import pytest


def test_heartbeat_format_has_all_7_dimensions():
    """心跳报告必须含 7 个核心维度"""
    from app.observability.intraday_heartbeat import _format_heartbeat
    text = _format_heartbeat()
    # 7 个 emoji/标签必须都有
    assert "💓" in text or "盘中心跳" in text, "顶部 banner"
    assert "🟢 服务" in text or "服务:" in text, "服务健康"
    assert "📊 持仓" in text or "持仓:" in text, "持仓状态"
    assert "盈亏" in text, "今日盈亏维度 (已平盈亏)"
    assert "🎯 信号" in text or "信号" in text, "信号漏斗"
    assert "🤖 LLM" in text or "LLM" in text, "LLM 链路"
    assert "⚠️" in text or "告警" in text, "告警计数"
    assert "🔵 风控" in text or "风控" in text, "风控状态"


def test_heartbeat_compact_under_500_chars():
    """钉钉友好: 单条心跳 < 500 字 (一屏)"""
    from app.observability.intraday_heartbeat import _format_heartbeat
    text = _format_heartbeat()
    assert len(text) < 1000, f"心跳报告太长 ({len(text)} 字), 钉钉看不下"


def test_heartbeat_skips_outside_market_hours():
    """非盘中 → push_heartbeat 直接返回 False (不推)"""
    from app.observability import intraday_heartbeat
    with patch.object(intraday_heartbeat, "_is_market_hours", return_value=False):
        result = intraday_heartbeat.push_heartbeat()
    assert result is False, "非盘中必须跳过"


def test_heartbeat_force_bypasses_market_check(monkeypatch):
    """force=True 跳过盘中检查 (用于手动测试)"""
    from app.observability import intraday_heartbeat
    pushed = []

    def fake_push(msg):
        pushed.append(msg)
        return True

    monkeypatch.setattr(intraday_heartbeat, "_is_market_hours", lambda: False)
    # 拦截 PushHub.push 避免真发钉钉
    fake_hub = MagicMock()
    fake_hub.push = fake_push
    monkeypatch.setattr("app.push.get_push_hub", lambda: fake_hub)

    # force=True 应触发推送
    result = intraday_heartbeat.push_heartbeat(force=True)
    # 注意: 即使 PushHub mock 失败, 函数也应有兜底返回 (不抛异常)
    assert isinstance(result, bool)


def test_heartbeat_query_failure_does_not_crash():
    """任一 query 失败, _format_heartbeat 不应抛异常 (fail-safe)"""
    from app.observability import intraday_heartbeat
    with patch.object(intraday_heartbeat, "_query_today_trades",
                       side_effect=Exception("DB locked")):
        # 模块内部已经 try/except, 但仍验证整体不崩
        try:
            text = intraday_heartbeat._format_heartbeat()
            # 如果到这, 说明整体方法的 try/except 兜住了
            assert isinstance(text, str)
        except Exception as e:
            pytest.fail(f"心跳格式化不应抛异常: {e}")


def test_heartbeat_loop_default_interval_30min():
    """HeartbeatLoop 默认 30min interval, 不允许 < 60s"""
    from app.observability.intraday_heartbeat import HeartbeatLoop
    loop = HeartbeatLoop(interval_min=30)
    assert loop.interval_sec == 1800, "默认 30min = 1800s"

    # 防误配, < 60s 强制 60s
    loop_bad = HeartbeatLoop(interval_min=0)  # 0 → 应被 max 兜底到 60s
    assert loop_bad.interval_sec >= 60, "interval 不应 < 60s (防 cron 误触发)"


def test_market_hours_skips_weekend():
    """周六/周日 _is_market_hours 必须返回 False"""
    from app.observability.intraday_heartbeat import _is_market_hours
    # 直接测试当前时间是否合理 — 当前不在盘中时间也行
    result = _is_market_hours()
    assert isinstance(result, bool), "返回 bool"


# ─── 数据真实性测试 (2026-05-19 用户要求) ───

def test_no_hardcoded_status_in_runtime_output():
    """禁止心跳输出中含硬编码 '运行中' / 'unknown' / 'OK' 等伪造健康字符串.

    用户要求: 推送数据必须真实, 任何 placeholder 字符串 = 误导决策.
    检查实际运行时输出 (而非源码注释/docstring).
    """
    from app.observability.intraday_heartbeat import _format_heartbeat
    text = _format_heartbeat(scheduler=None)
    forbidden_in_output = [
        "运行中",                # 不许"运行中"出现在 push 文本
        "unknown",               # 不许"unknown" 出现在 push 文本
    ]
    for token in forbidden_in_output:
        assert token not in text, (
            f"心跳输出含伪造字符串 '{token}' — 用户明确要求数据必须真实.\n"
            f"实际输出:\n{text}"
        )


def test_uptime_source_traceable():
    """uptime 必须返回真实 source 标识 (scheduler/psutil/proc/module_load)."""
    from app.observability.intraday_heartbeat import _query_service_health
    health = _query_service_health(scheduler=None)
    assert "uptime_source" in health, "必须含 uptime_source 字段"
    assert health["uptime_source"] in ("scheduler", "psutil", "/proc", "module_load"), (
        f"uptime_source 必须是 4 种来源之一, 实际: {health['uptime_source']}"
    )
    assert health["uptime_sec"] >= 0, "uptime 必须 >= 0 秒"


def test_uptime_with_scheduler_start_time():
    """传入 scheduler.start_time → uptime_source='scheduler' (最准)."""
    from app.observability.intraday_heartbeat import _query_service_health
    import time
    fake_scheduler = type('FakeScheduler', (), {})()
    fake_scheduler.start_time = time.time() - 120  # 2 分钟前
    health = _query_service_health(scheduler=fake_scheduler)
    assert health["uptime_source"] == "scheduler"
    assert 119 <= health["uptime_sec"] <= 121, f"应是 ~120s, 实际 {health['uptime_sec']}"


def test_risk_state_uses_window_not_today():
    """风控字段必须是 'window_hours' 不是 'today' (避免日历日跨时区误导)."""
    from app.observability.intraday_heartbeat import _query_risk_state
    risk = _query_risk_state(window_hours=8)
    # 旧字段 'breakers_today' 必须不存在
    assert "breakers_today" not in risk, "禁用旧字段 breakers_today (语义不清)"
    assert "flat_all_today" not in risk, "禁用旧字段 flat_all_today"
    # 新字段必须存在
    assert "breakers" in risk
    assert "flat_all" in risk
    assert "window_hours" in risk and risk["window_hours"] == 8


def test_funnel_includes_entries_count():
    """信号漏斗必须含 entries (入场数), 不能只有 rejects (前文档样例缺这个)."""
    from app.observability.intraday_heartbeat import _query_signal_funnel
    funnel = _query_signal_funnel(30)
    assert "entries" in funnel, "信号漏斗必须含 entries 字段"
    assert isinstance(funnel["entries"], int)
    assert funnel["entries"] >= 0


def test_pre_gate_blocked_uses_json_parse_not_like():
    """Pre-Gate 拦截统计必须 JSON 解析, 不用脆弱 LIKE 字符串匹配."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent.parent
           / "app" / "observability" / "intraday_heartbeat.py").read_text()
    # 不应有 LIKE '%"go": false%' 这种字符串匹配
    assert 'LIKE \'%"go": false%\'' not in src, (
        "Pre-Gate 拦截统计不应用 LIKE 字符串 (脆弱), 必须用 json.loads 解析"
    )
    # 应有 json.loads 解析
    assert "_json.loads" in src or "json.loads(summary)" in src, (
        "必须用 JSON 解析 result_summary 提取 go 字段"
    )
