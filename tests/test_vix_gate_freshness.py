"""VIX 入场闸新鲜度钉子 (2026-11-03, 全链审计 P0-2 续 — DEB §10.6)。

背景: 入场否决闸 (VIXGuard) 读的不是 AV provider, 而是
`app/analysis/market_state.py::get_vix_cached()`。该处原本是**固定 30 分钟**缓存 ——
2026-09-14 为"让盘中 VIX 冲高在门上可见"加的 VIXY 实时代理腿, 被这个缓存挡住:
活腿每 30 分钟才被问一次 ⇒ 盘中冲高最长 30 分钟在闸门不可见 ⇒ 那次修复被缓存半废。

本文件钉住:
1. 盘中 TTL = 60s (61s 前的值必须作废重取; 50s 前的值命中缓存不打 API);
2. 盘外 TTL 仍是 30min (收盘后无冲高可捕捉, 且避免无谓的 FRED CSV 下载);
3. `get_vix_cached()` 的 fail-close 语义不变 (取不到 ⇒ None ⇒ 上层拒新仓);
4. 阶段判据收敛到 `app/data/vix_source.py::is_trading_phase` (生产者不得自行比较
   MarketPhase.TRADING —— 见 tests/test_vix_proxy_window_single_source.py 的 AST 契约)。

回滚: 还原 market_state 的 `_vix_ttl()` (恢复 `_cache_ttl` 30min) + 删 vix_source
      的 `is_trading_phase` (本文件随删)。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.analysis.market_state import MarketStateChecker  # noqa: E402
from app.core.scheduler_triggers import MarketPhase  # noqa: E402
from app.data.vix_source import is_trading_phase  # noqa: E402


def _phase(monkeypatch, phase: MarketPhase) -> None:
    monkeypatch.setattr("app.core.scheduler_triggers.get_market_phase", lambda: phase)


def _checker(monkeypatch, phase: MarketPhase, cached_vix=None, age_s=0.0, fetch=99.0):
    """构造 checker 并预置一份指定年龄的缓存; 返回 (checker, 抓取计数 dict)。"""
    _phase(monkeypatch, phase)
    c = MarketStateChecker()
    calls = {"n": 0}

    def _fetch_vix_data():
        calls["n"] += 1
        return fetch

    c._fetch_vix_data = _fetch_vix_data
    if cached_vix is not None:
        c._vix_cache = (cached_vix, datetime.now() - timedelta(seconds=age_s))
    return c, calls


# ── 1. 阶段判据单一真源 ────────────────────────────────────────────────

def test_is_trading_phase(monkeypatch):
    assert is_trading_phase(MarketPhase.TRADING) is True
    assert is_trading_phase(MarketPhase.PRE_MARKET) is False
    assert is_trading_phase(MarketPhase.AFTER_HOURS) is False
    assert is_trading_phase(MarketPhase.OVERNIGHT) is False


def test_producer_does_not_compare_trading_locally():
    """AST 契约: market_state 体内不得出现 == MarketPhase.TRADING 之类的阶段比较。"""
    import ast
    src = (PROJECT_ROOT / "app/analysis/market_state.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
            isinstance(c, ast.Attribute) and c.attr in ("TRADING", "PRE_MARKET")
            for c in node.comparators
        ):
            pytest.fail(f"market_state.py:{node.lineno} 自行比较阶段 —— 必须走 is_trading_phase")


# ── 2. 盘中 TTL = 60s ──────────────────────────────────────────────────

def test_intraday_cache_expires_after_60s(monkeypatch):
    """盘中: 61s 前的 VIX 必须作废重取 (旧实现要等 30min)。"""
    c, calls = _checker(monkeypatch, MarketPhase.TRADING, cached_vix=15.0, age_s=61)
    assert c.get_vix_cached() == 99.0
    assert calls["n"] == 1, "61s 前的值必须重取"


def test_intraday_cache_hit_within_60s(monkeypatch):
    """盘中: 60s 内命中缓存, 不打 API (取数频率 ≤1 次/分钟)。"""
    c, calls = _checker(monkeypatch, MarketPhase.TRADING, cached_vix=15.0, age_s=50)
    assert c.get_vix_cached() == 15.0
    assert calls["n"] == 0


# ── 3. 盘外 TTL 仍是 30min (行为不变) ──────────────────────────────────

def test_offhours_cache_ttl_unchanged_30min(monkeypatch):
    """盘外: 20 分钟前的值仍命中 (收盘后无需新鲜度, 也不多打 FRED)。"""
    c, calls = _checker(monkeypatch, MarketPhase.AFTER_HOURS, cached_vix=15.0, age_s=20 * 60)
    assert c.get_vix_cached() == 15.0
    assert calls["n"] == 0


def test_offhours_cache_expires_after_30min(monkeypatch):
    c, calls = _checker(monkeypatch, MarketPhase.AFTER_HOURS, cached_vix=15.0, age_s=31 * 60)
    assert c.get_vix_cached() == 99.0
    assert calls["n"] == 1


# ── 4. fail-close 语义不变 ─────────────────────────────────────────────

def test_none_propagates_for_fail_close(monkeypatch):
    """取不到 ⇒ None (上层据此拒新仓) —— 不得被缓存/兜底掩盖。"""
    c, calls = _checker(monkeypatch, MarketPhase.TRADING, cached_vix=None, fetch=None)
    assert c.get_vix_cached() is None
    assert calls["n"] == 1


# ── 5. FRED 日频独立槽位 + 实时代理失败冷却 (P0-2 续批) ────────────────

class _FakeFRED:
    """FRED CSV 的最小替身 (market_state 只看 status_code / text)。"""

    def __init__(self, value: float):
        self.status_code = 200
        self.text = f"DATE,VIXCLS\n2026-11-02,{value}\n"

    def raise_for_status(self):
        return None


def _wire(monkeypatch, *, live=None, fred=19.42, proxy_window=True):
    """接管三条腿: 实时代理 (Futu VIXY) / FRED CSV / AV。返回计数 dict。"""
    calls = {"live": 0, "fred": 0}

    def _live():
        calls["live"] += 1
        return live

    def _get(url, *a, **k):
        calls["fred"] += 1
        return _FakeFRED(fred)

    monkeypatch.setattr("app.data.vix_source.fetch_vixy_via_opend", _live)
    monkeypatch.setattr("app.data.vix_source.is_vixy_proxy_window", lambda *a, **k: proxy_window)
    monkeypatch.setattr("requests.get", _get)
    return calls


def test_fred_daily_slot_hit_within_hour(monkeypatch):
    """日频槽位独立: 代理不可用时, 第二次调用不得再下一遍 CSV (盘中活槽位 60s 就过期)。"""
    _phase(monkeypatch, MarketPhase.TRADING)
    c = MarketStateChecker()
    calls = _wire(monkeypatch, live=None, fred=19.42)

    assert c.get_vix_cached() == 19.42
    assert calls["fred"] == 1
    # 把实时代理槽位作废 (模拟 60s 过期), 日频槽位仍在 1h 内
    c._vix_cache = None
    assert c.get_vix_cached() == 19.42
    assert calls["fred"] == 1, "日频槽位命中 ⇒ 不得重复下载 CSV"


def test_fred_daily_slot_expires_after_hour(monkeypatch):
    """日频槽位 1h 后失效 ⇒ 重新下载 (FRED 每日更新, 1h 足够)。"""
    _phase(monkeypatch, MarketPhase.TRADING)
    c = MarketStateChecker()
    calls = _wire(monkeypatch, live=None, fred=19.42)
    c._vix_fred_cache = (19.42, datetime.now() - timedelta(seconds=3601))
    c._vix_cache = None

    assert c.get_vix_cached() == 19.42
    assert calls["fred"] == 1, "日频槽位超龄 ⇒ 必须重新下载"


def test_live_probe_cooldown_after_failure(monkeypatch):
    """实时代理失败后 60s 内不得再建连接 (每次实时代理都新建一条 Futu 连接)。"""
    _phase(monkeypatch, MarketPhase.TRADING)
    c = MarketStateChecker()
    calls = _wire(monkeypatch, live=None, fred=19.42)

    assert c.get_vix_cached() == 19.42
    assert calls["live"] == 1
    c._vix_cache = None                      # 活槽位作废 (模拟 60s 过期)
    assert c.get_vix_cached() == 19.42
    assert calls["live"] == 1, "冷却期内不得重复探活 (连接 churn)"


def test_live_recovery_takes_over_after_cooldown(monkeypatch):
    """代理恢复后必须回切实时值 (不再被日频值占住)。"""
    _phase(monkeypatch, MarketPhase.TRADING)
    c = MarketStateChecker()
    _wire(monkeypatch, live=None, fred=19.42)
    assert c.get_vix_cached() == 19.42       # 代理不可用 → 日频

    c._vix_live_fail_ts = 0.0                # 冷却过期
    calls = _wire(monkeypatch, live=15.67, fred=19.42)
    c._vix_cache = None
    assert c.get_vix_cached() == 15.67, "代理恢复 ⇒ 必须回切实时值"
    assert calls["live"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])