"""数据源/路由 单例回归测试 (2026-11-03 性能批).

守什么 (都是实测出来过的浪费):
1. `get_alpha_vantage_provider()` 必须返回同一实例 —— 原实现每次 new 都新建
   requests.Session + **新的日配额计数器** (配额门形同虚设) + 丢失缓存;
   一个交易时段实测 137 次 "AlphaVantage 初始化完成"。
2. 四个旁路构造点必须走 `get_market_data_router()` —— 每次 new MarketDataRouter()
   = 一条 Futu LV3 连接 + 一个 30s 守护线程 (实测一个时段 13 次 "正在连接 OpenD")。
3. 源码契约: 这些文件里不得再出现裸构造 (防被改回去)。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.data import alphavantage_provider as av_mod  # noqa: E402


def test_av_provider_is_process_singleton():
    a = av_mod.get_alpha_vantage_provider()
    b = av_mod.get_alpha_vantage_provider()
    assert a is b
    assert av_mod._shared_av_provider is a


def test_av_singleton_creates_only_once(monkeypatch):
    created = []

    class _Fake:
        def __init__(self):
            created.append(1)

    monkeypatch.setattr(av_mod, "_shared_av_provider", None)
    monkeypatch.setattr(av_mod, "AlphaVantageProvider", _Fake)
    for _ in range(5):
        av_mod.get_alpha_vantage_provider()
    assert len(created) == 1


def _src(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


def test_no_bare_av_construction_in_hot_paths():
    for rel in ("app/risk/position_exit_monitor.py", "app/core/position_sync.py"):
        src = _src(rel)
        # 去掉 import 行与注释后不得再有裸构造
        code = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "AlphaVantageProvider()" not in code, f"{rel} 仍存在裸构造 (每次调用新建 provider)"


def test_router_bypass_sites_use_shared_getter():
    targets = (
        "app/recommendations/accuracy_tracker.py",
        "app/recommendations/price_calibrator.py",
        "app/playbook/data_collector.py",
        "app/us_pipeline/open_checker.py",
    )
    for rel in targets:
        src = _src(rel)
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        assert not re.search(r"=\s*MarketDataRouter\(", code), f"{rel} 仍裸构造 MarketDataRouter"
        assert "get_market_data_router(" in code, f"{rel} 未使用共享 getter"