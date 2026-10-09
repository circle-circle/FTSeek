from types import SimpleNamespace

from app.playbook.pipeline_parts.signal_context_store import (
    _compute_key_levels,
    _load_theme_context_cached,
)


def test_compute_key_levels_sorts_and_dedupes():
    data = SimpleNamespace(
        fundamental=SimpleNamespace(high_52w=121.0, low_52w=79.0),
        technical=SimpleNamespace(ma20=100.0, ma50=100.0),
    )
    levels = _compute_key_levels(data)
    assert levels == {
        "support": [79.0, 100.0],
        "resistance": [121.0],
    }


def test_theme_context_cache_reloads_when_mtime_changes(tmp_path):
    path = tmp_path / "theme_context.json"
    path.write_text('{"symbols": {"AAA": {"net_wind": "up"}}}')
    cache = {"mtime": None, "data": None}
    first = _load_theme_context_cached(path, cache=cache)
    assert first["symbols"]["AAA"]["net_wind"] == "up"
    assert _load_theme_context_cached(path, cache=cache) is first

    path.write_text('{"symbols": {"AAA": {"net_wind": "down"}}}')
    path.stat().st_mtime_ns
    import os
    os.utime(path, ns=(1_000_000_000_000, 1_000_000_000_001))
    second = _load_theme_context_cached(path, cache=cache)
    assert second is not first
    assert second["symbols"]["AAA"]["net_wind"] == "down"
