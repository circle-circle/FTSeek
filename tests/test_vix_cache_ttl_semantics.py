"""VIX 缓存语义钉子 (2026-11-03, 全链审计 P0-2 — DEB-20261103-full-chain-perf-risk-audit.md §10)。

守什么 (两个已证实的缺陷):

1. **60s/600s 分档从未生效** —— 原实现把 60/600 传给 `_is_cache_valid(min_age_seconds=…)`,
   而该参数是**最小年龄**(防抖)语义 ⇒ 真实上限是 provider 的 `cache_ttl=900s`。
   合成年龄实测: 30/90/300/700/880s 全部"命中缓存", 950s 才失效
   ⇒ 盘中"每 60s 刷新以捕捉 VIX 急变"从未发生, VIX 最多陈旧 15 分钟
   (VIXGuard 入场否决 / regime 闸 / position_sync 缩放因子都读这条)。
   本文件钉住: 实时槽位在**盘中 60s、盘外 600s** 过期即作废。

2. **来源键冲突** —— 实时代理 (VIXY) 与日频序列 (FRED) 曾写同一个 cache_key,
   读取端不看 source ⇒ 一次代理失败即可让 T-1 值占住实时槽位最多 900s,
   **代理恢复后仍读不到实时值**。
   本文件钉住: 两来源两槽位 + 读端校验 source + 代理恢复后能回切。

边界: 只钉 provider 的取值层语义; 消费方 (VIXGuard / market_regime /
position_sync / exit_monitor) 的读法不在本文件范围。

回滚: 还原 app/data/alphavantage_provider.py 的 VIX 取值层 (本文件随删)。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.scheduler_triggers import MarketPhase  # noqa: E402
from app.data.alphavantage_provider import (  # noqa: E402
    AlphaVantageProvider,
    _VIX_FRED_CACHE_KEY,
    _VIX_LIVE_CACHE_KEY,
)

LIVE = "finnhub_vixy"
FRED = "fred"


def _provider() -> AlphaVantageProvider:
    return AlphaVantageProvider(api_key="test-key")


def _seed(av: AlphaVantageProvider, key: str, source: str, vix: float, age_s: float) -> None:
    av.cache[key] = {"vix": vix, "source": source}
    av.cache_time[key] = datetime.now() - timedelta(seconds=age_s)


def _phase(monkeypatch, phase: MarketPhase, proxy_window: bool) -> None:
    monkeypatch.setattr("app.core.scheduler_triggers.get_market_phase", lambda: phase)
    monkeypatch.setattr("app.data.vix_source.is_vixy_proxy_window", lambda *a, **k: proxy_window)


def _stub_fetchers(monkeypatch, av, live_value, fred_value, calls=None):
    def _live(cache_key):
        if calls is not None:
            calls.append(("live", cache_key))
        if live_value is None:
            return None
        av.cache[cache_key] = {"vix": live_value, "source": LIVE}
        av.cache_time[cache_key] = datetime.now()
        return live_value

    def _fred(cache_key):
        if calls is not None:
            calls.append(("fred", cache_key))
        if fred_value is None:
            return None
        av.cache[cache_key] = {"vix": fred_value, "source": FRED}
        av.cache_time[cache_key] = datetime.now()
        return fred_value

    monkeypatch.setattr(av, "_fetch_finnhub_vixy", _live)
    monkeypatch.setattr(av, "_fetch_fred_vix", _fred)


# ── 1. 实时槽位的"上限 TTL" ────────────────────────────────────────────

def test_live_slot_expires_after_60s_in_trading(monkeypatch):
    """盘中: 61s 前的实时值必须作废重取 (原实现会一直命中到 900s)。"""
    av = _provider()
    _phase(monkeypatch, MarketPhase.TRADING, True)
    calls = []
    _stub_fetchers(monkeypatch, av, live_value=21.5, fred_value=19.0, calls=calls)
    _seed(av, _VIX_LIVE_CACHE_KEY, LIVE, 15.0, age_s=61)

    out = av.get_vix_or_none()

    assert out == 21.5, "61s 前的实时值必须作废 (盘中上限 60s)"
    assert calls and calls[0][0] == "live", "必须重新打代理, 而不是吃旧缓存"


def test_live_slot_within_60s_is_hit(monkeypatch):
    """盘中: 60s 内的实时值命中缓存, 不打 API (调用频率 ≤1 次/分钟)。"""
    av = _provider()
    _phase(monkeypatch, MarketPhase.TRADING, True)
    calls = []
    _stub_fetchers(monkeypatch, av, live_value=21.5, fred_value=19.0, calls=calls)
    _seed(av, _VIX_LIVE_CACHE_KEY, LIVE, 15.0, age_s=50)

    assert av.get_vix_or_none() == 15.0
    assert calls == [], "60s 内必须命中缓存, 不得打 API"


def test_live_slot_off_hours_ttl_is_600s(monkeypatch):
    """盘外 (非代理窗口): 日频优先; 日频不可用时实时槽位 600s 内命中、601s 作废。"""
    av = _provider()
    _phase(monkeypatch, MarketPhase.AFTER_HOURS, False)
    calls = []
    # 盘外 FRED 优先 ⇒ 这里让 FRED 不可用, 才会走到实时槽位
    _stub_fetchers(monkeypatch, av, live_value=21.5, fred_value=None, calls=calls)
    _seed(av, _VIX_LIVE_CACHE_KEY, LIVE, 15.0, age_s=500)

    assert av.get_vix_or_none() == 15.0
    assert not [c for c in calls if c[0] == "live"], "600s 内的实时值必须命中, 不得重打代理"

    av2 = _provider()
    calls2 = []
    _stub_fetchers(monkeypatch, av2, live_value=21.5, fred_value=None, calls=calls2)
    _seed(av2, _VIX_LIVE_CACHE_KEY, LIVE, 15.0, age_s=700)

    assert av2.get_vix_or_none() == 21.5, "700s 的实时值必须作废 (盘外上限 600s)"
    assert [c for c in calls2 if c[0] == "live"]


# ── 2. 来源分槽: FRED 不得占住实时槽位 ────────────────────────────────

def test_fred_value_does_not_occupy_live_slot(monkeypatch):
    """代理失败落 FRED 后, 实时槽位必须仍为空 (旧实现两者同 key ⇒ T-1 值冒充实时)。"""
    av = _provider()
    _phase(monkeypatch, MarketPhase.TRADING, True)
    _stub_fetchers(monkeypatch, av, live_value=None, fred_value=19.0)

    assert av.get_vix_or_none() == 19.0          # 代理失败 → 日频兜底
    assert _VIX_LIVE_CACHE_KEY not in av.cache or \
        av.cache.get(_VIX_LIVE_CACHE_KEY, {}).get("source") != FRED
    assert av.cache[_VIX_FRED_CACHE_KEY]["source"] == FRED


def test_live_value_takes_over_after_proxy_recovers(monkeypatch):
    """代理恢复后必须读实时值 (旧实现要等 900s, 期间一直用 T-1 值)。"""
    av = _provider()
    _phase(monkeypatch, MarketPhase.TRADING, True)
    _stub_fetchers(monkeypatch, av, live_value=None, fred_value=19.0)
    assert av.get_vix_or_none() == 19.0           # 代理失败 → T-1

    _stub_fetchers(monkeypatch, av, live_value=21.5, fred_value=19.0)
    assert av.get_vix_or_none() == 21.5           # 代理恢复 → 立即回切实时值


def test_source_mismatch_is_rejected(monkeypatch):
    """实时槽位里若躺着 source=fred 的值 (历史遗留/异写入), 不得当作实时值返回。"""
    av = _provider()
    _phase(monkeypatch, MarketPhase.TRADING, True)
    calls = []
    _stub_fetchers(monkeypatch, av, live_value=21.5, fred_value=19.0, calls=calls)
    _seed(av, _VIX_LIVE_CACHE_KEY, FRED, 19.0, age_s=5)

    assert av.get_vix_or_none() == 21.5
    assert calls and calls[0][0] == "live"


# ── 3. 代理探针失败冷却 (实时槽位变短后必须配冷却) ─────────────────────

def test_probe_cooldown_avoids_repeated_finnhub_calls(monkeypatch):
    """代理失败后 60s 内的再次调用不得再打 Finnhub (否则每次调用都打一次)。"""
    av = _provider()
    _phase(monkeypatch, MarketPhase.TRADING, True)
    hits = {"n": 0}

    def _boom(cache_key):
        hits["n"] += 1
        raise RuntimeError("network down")

    monkeypatch.setattr(av, "_fetch_finnhub_vixy", _boom)
    monkeypatch.setattr(av, "_fetch_fred_vix", lambda cache_key: 19.0)

    av.get_vix_or_none()          # 第一次: 代理真打一次并失败
    assert hits["n"] == 1
    av.get_vix_or_none()          # 冷却期内: 不得再打
    assert hits["n"] == 1, "冷却期内必须跳过代理探针"


def test_probe_cooldown_expires(monkeypatch):
    """冷却过期后允许重新探测 (代理可能已恢复)。"""
    av = _provider()
    _phase(monkeypatch, MarketPhase.TRADING, True)
    monkeypatch.setattr(av, "_vix_probe_fail_ts", 0.0)
    monkeypatch.setattr(av, "_fetch_fred_vix", lambda cache_key: 19.0)
    seen = {"n": 0}

    def _live(cache_key):
        seen["n"] += 1
        return None

    monkeypatch.setattr(av, "_fetch_finnhub_vixy", _live)
    av.get_vix_or_none()
    assert seen["n"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
