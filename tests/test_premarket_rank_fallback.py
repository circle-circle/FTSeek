"""盘前榜发现链冷缓存回退守卫 (2026-08-29 复查加固)。

背景: factor_engine Source 1b 此前直读 premarket_enrichment._cache — 主服务盘中
重启后进程内缓存清空, 该源当日静默零贡献 (仅 debug 日志)。修复: 新增
get_premarket_rank_entries() 封装"缓存→当日持久化文件"两级口径, Source 1b 改走它。
"""
import json

import pytest

import app.data.premarket_enrichment as pe
from app.utils.timezone import now_et


@pytest.fixture
def cold_cache(monkeypatch):
    monkeypatch.setattr(pe, "_cache", {})


def _today_file(tmp_path, payload):
    d = now_et().strftime("%Y-%m-%d")
    path = tmp_path / f"premarket_rank_candidates_{d}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                    encoding="utf-8")
    return path


def test_warm_cache_preferred(monkeypatch, tmp_path):
    """缓存有数据时直接用缓存, 不读文件。"""
    monkeypatch.setattr(pe, "_cache", {
        "futu_premarket_rank": {"gainers": [{"symbol": "AAA"}], "losers": []}})
    monkeypatch.setattr(pe, "_RANK_CANDIDATES_DIR", tmp_path)
    _today_file(tmp_path, {"gainers": {"candidates": [{"symbol": "FILE"}]},
                           "losers": {"candidates": []}})
    out = pe.get_premarket_rank_entries()
    assert out == {"gainers": [{"symbol": "AAA"}], "losers": []}


def test_cold_cache_falls_back_to_today_file(cold_cache, monkeypatch, tmp_path):
    """冷缓存 + 当日文件存在 → 回退读文件 (nested candidates 口径)。"""
    monkeypatch.setattr(pe, "_RANK_CANDIDATES_DIR", tmp_path)
    _today_file(tmp_path, {"gainers": {"candidates": [{"symbol": "BBB"}, {"symbol": "CCC"}]},
                           "losers": {"candidates": [{"symbol": "DDD"}]}})
    out = pe.get_premarket_rank_entries()
    assert out == {"gainers": [{"symbol": "BBB"}, {"symbol": "CCC"}],
                   "losers": [{"symbol": "DDD"}]}


def test_cold_cache_no_file_returns_empty(cold_cache, monkeypatch, tmp_path):
    """冷缓存 + 无当日文件 → 空, 不抛异常 (宁缺毋滥)。"""
    monkeypatch.setattr(pe, "_RANK_CANDIDATES_DIR", tmp_path)
    assert pe.get_premarket_rank_entries() == {"gainers": [], "losers": []}


def test_cold_cache_corrupt_file_returns_empty(cold_cache, monkeypatch, tmp_path):
    """当日文件损坏 → 空, 不抛异常。"""
    monkeypatch.setattr(pe, "_RANK_CANDIDATES_DIR", tmp_path)
    _today_file(tmp_path, "{not valid json")
    assert pe.get_premarket_rank_entries() == {"gainers": [], "losers": []}


def test_factor_engine_source1b_uses_helper(monkeypatch):
    """Source 1b 集成: 经 get_premarket_rank_entries 进发现池 (不再直读 _cache)。"""
    monkeypatch.setattr(pe, "_cache", {})  # 掐掉 Source 1 (premarket_movers)
    monkeypatch.setattr(pe, "get_premarket_rank_entries",
                        lambda: {"gainers": [{"symbol": "US.XYZ"}], "losers": []})
    monkeypatch.setattr(pe, "_get_finnhub_client", lambda: None)  # 掐掉 Source 2/3
    from app.plan.factor_engine import _discover_finnhub_movers
    assert "US.XYZ" in _discover_finnhub_movers()
