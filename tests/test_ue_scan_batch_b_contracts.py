"""DEB-20260816-fullchain 批次B 契约测试: P1-11 (盘前锚 fail-safe) /
P1-14 (DA 优先防覆盖) / #24 (confidence [0,1] 夹逼)。

被测对象: scripts/llm_universe_scan.py (非包模块, importlib 直载)。
红线: 只测行为契约, 不测实现细节。
"""
import importlib.util
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "llm_universe_scan_uat", _PROJECT_ROOT / "scripts" / "llm_universe_scan.py"
)
scan = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = scan
_SPEC.loader.exec_module(scan)


# ── P1-11: _effective_quote_price 盘前无 pre_price 不回落昨收 ────────────

def _pm(monkeypatch, is_premarket: bool, anchor: bool = True):
    monkeypatch.setattr(scan, "_is_us_premarket", lambda: is_premarket)
    monkeypatch.setattr(scan, "_cfg", lambda k, d=None: anchor if k == "universe_expansion.premarket_anchor" else d)


def test_p1_11_premarket_with_pre_price_uses_pre(monkeypatch):
    _pm(monkeypatch, is_premarket=True)
    assert scan._effective_quote_price({"price": 33.47, "pre_price": 32.21}) == 32.21


def test_p1_11_premarket_missing_pre_price_returns_none(monkeypatch):
    # 修复点: 盘前+锚开启+无 pre_price → None (旧行为: 回落昨收 33.47 锚错 3.8%)
    _pm(monkeypatch, is_premarket=True)
    assert scan._effective_quote_price({"price": 33.47}) is None


def test_p1_11_premarket_invalid_pre_price_returns_none(monkeypatch):
    _pm(monkeypatch, is_premarket=True)
    assert scan._effective_quote_price({"price": 33.47, "pre_price": "abc"}) is None


def test_p1_11_premarket_anchor_disabled_keeps_legacy(monkeypatch):
    _pm(monkeypatch, is_premarket=True, anchor=False)
    assert scan._effective_quote_price({"price": 33.47}) == 33.47


def test_p1_11_regular_hours_keeps_legacy(monkeypatch):
    _pm(monkeypatch, is_premarket=False)
    assert scan._effective_quote_price({"price": 33.47, "pre_price": 32.21}) == 33.47


def test_p1_11_empty_quote_returns_none(monkeypatch):
    _pm(monkeypatch, is_premarket=True)
    assert scan._effective_quote_price({}) is None


# ── #24: _normalize_surfaced confidence 夹逼 [0,1] ──────────────────────

def _surf(conf):
    return scan._normalize_surfaced(
        [{"symbol": "XOM", "direction": "LONG", "confidence": conf}]
    )


def test_24_conf_above_one_clamped():
    assert _surf(1.2)[0]["confidence"] == 1.0
    assert _surf(7)[0]["confidence"] == 1.0


def test_24_conf_negative_clamped_to_zero():
    assert _surf(-0.3)[0]["confidence"] == 0.0


def test_24_conf_invalid_string_defaults_zero():
    assert _surf("abc")[0]["confidence"] == 0.0


def test_24_conf_normal_untouched():
    assert _surf(0.85)[0]["confidence"] == 0.85


def test_24_invalid_direction_dropped():
    out = scan._normalize_surfaced([{"symbol": "XOM", "direction": "buy", "confidence": 0.8}])
    assert out == []


# ── P1-14: _is_da_priority_skip DA 前缀保护 ──────────────────────────────

def test_p1_14_da_prefix_skipped():
    assert scan._is_da_priority_skip("DA-20260814192000-1-XOM") is True


def test_p1_14_legacy_dec_prefix_skipped():
    assert scan._is_da_priority_skip("DEC-202606230400-XOM") is True


def test_p1_14_ue_self_update_allowed():
    assert scan._is_da_priority_skip("universe_expansion:2026-08-16:XOM") is False


def test_p1_14_empty_or_none_keeps_legacy_overwrite():
    assert scan._is_da_priority_skip("") is False
    assert scan._is_da_priority_skip(None) is False
