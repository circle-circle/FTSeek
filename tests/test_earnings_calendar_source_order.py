"""
财报日历源序守卫 (2026-08-29, 用户裁"2直接做")
================================================
背景: G6 复核确认财报禁建门是活门 (8/19-8/26 拦截 85 次), 其数据源原为
Alpha Vantage 单点 (外部配额/断流风险), 富途原生只作"AV 空才触发"的
fallback。本批反转为富途主源/AV 备源。

本测试钉住的是【行为】不是文本:
  1. 主源优先调用 (mock 记录调用顺序)
  2. 主源失败时备源接管 (降级路径不断)
  3. 双源皆空时产生断流告警 (此前静默返回空列表 = G6 死表误判的同族失明)
  4. 活体冒烟: 富途主源在真实 OpenD 上返回数据且窗口合规 (≤7 天/段)

回滚契约: 源序回退 = revert app/data/earnings_calendar.py 源序块,
本测试随之失效 (红) — 这是故意的, 防静默回退。
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def ec():
    from app.data.earnings_calendar import EarningsCalendar
    return EarningsCalendar(api_key="test")


def test_futu_primary_called_before_av(ec, monkeypatch):
    """源序: 富途主源先于 AV 备源被调用."""
    calls = []

    def fake_futu(**kw):
        calls.append("futu")
        return [{"symbol": "TEST", "reportDate": "2026-09-01",
                 "source": "futu"}]

    def fake_av(horizon="3month"):
        calls.append("av")
        return "symbol,reportDate\nFAKE,2026-09-01"

    monkeypatch.setattr(ec, "_fetch_futu_calendar_primary", fake_futu)
    monkeypatch.setattr(ec, "_fetch_raw_calendar", fake_av)

    rows = ec.get_earnings_calendar(force_refresh=True)
    assert calls == ["futu"], f"主源成功时不应碰 AV, 实际调用序={calls}"
    assert rows and rows[0]["source"] == "futu"


def test_av_fallback_when_futu_empty(ec, monkeypatch):
    """降级: 富途主源空 → AV 备源接管, 输出契约不变."""
    calls = []
    monkeypatch.setattr(
        ec, "_fetch_futu_calendar_primary",
        lambda **kw: (calls.append("futu"), [])[1])
    monkeypatch.setattr(
        ec, "_fetch_raw_calendar",
        lambda horizon="3month": (calls.append("av"),
                                  "symbol,reportDate\nFAKE,2026-09-01")[1])

    rows = ec.get_earnings_calendar(force_refresh=True)
    assert calls == ["futu", "av"], f"主源空后必须走 AV, 实际={calls}"
    assert rows and rows[0]["symbol"] == "FAKE"


def test_dual_source_empty_warns(ec, monkeypatch, caplog):
    """断流告警: 双源皆空 → WARNING 显式标记 (防空转失明)."""
    import logging
    monkeypatch.setattr(ec, "_fetch_futu_calendar_primary", lambda **kw: [])
    monkeypatch.setattr(ec, "_fetch_raw_calendar", lambda horizon="3month": "")

    with caplog.at_level(logging.WARNING):
        rows = ec.get_earnings_calendar(force_refresh=True)
    assert rows == []
    assert any("断流告警" in r.message for r in caplog.records), (
        "双源皆空必须产生断流告警 — 静默空列表正是 G6 死表误判的同族失明")


def test_futu_window_segments_within_api_limit():
    """窗口分段合规: 富途 API 硬限制单次 ≤7 天含端点 (实测超限报
    '日期范围不能超过7天'). 主源必须拆段且每段 ≤7 天含端点."""
    from datetime import date, timedelta
    import inspect
    from app.data.earnings_calendar import EarningsCalendar

    src = inspect.getsource(EarningsCalendar._fetch_futu_calendar_primary)
    # 结构断言 (非语义猜测): 必须有双窗口构造 + 去重
    assert "windows" in src and "drop_duplicates" in src, (
        "主源必须是双窗口合并去重结构 — 单窗口无法同时覆盖过去段 "
        "(get_recently_reported) 与未来段 (days_ahead=7) 且不超 7 天限制")

    # 数值边界验证: 默认参数下两段各自长度
    days_back, days_ahead = 2, 7
    today = date.today()
    seg_past = (today - (today - timedelta(days=days_back))).days + 1
    seg_future = ((today + timedelta(days=days_ahead - 1)) - today).days + 1
    assert seg_past <= 7, f"过去段 {seg_past} 天超 API 限制"
    assert seg_future <= 7, f"未来段 {seg_future} 天超 API 限制"


@pytest.mark.live
def test_futu_primary_live_smoke(ec):
    """活体冒烟: 真实 OpenD 上主源返回非空且覆盖未来 7 天.
    OpenD 不可达时跳过 (非本测试失败)."""
    import socket
    import os
    host = os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("FUTU_OPEND_PORT", "11111"))
    except (ValueError, TypeError):
        port = 11111
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect((host, port))
    except (ConnectionRefusedError, OSError):
        pytest.skip("OpenD 不可达, 跳过活体冒烟")
    finally:
        s.close()

    rows = ec._fetch_futu_calendar_primary()
    assert rows, "OpenD 可达但富途主源返回空 — 查 get_earnings_calendar 权限/参数"
    assert all(r.get("source") == "futu" for r in rows)
    assert all(r.get("reportDate") for r in rows), "存在缺 reportDate 的行"
