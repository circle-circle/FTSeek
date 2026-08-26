"""U11 开盘+10min 确认门单测 (2026-08-26, DEB-ue-audit r10).

谓词 _price_in_verdict 为纯函数, 直接单测全分支。
阈值 0.02 校准依据见 config/unified_config.yaml confirm_gate 注释。
"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "_lus_u11", _ROOT / "scripts" / "llm_universe_scan.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["_lus_u11"] = _mod
_spec.loader.exec_module(_mod)

V = _mod._price_in_verdict
TH = 0.02


# ── 基本裁决 ──────────────────────────────────────────────
def test_long_price_in():
    """LONG 同向涨超阈值 → PRICE_IN (信息已反映)."""
    v, s = V("LONG", 100.0, 103.0, TH)
    assert v == "PRICE_IN"
    assert abs(s - 0.03) < 1e-9


def test_long_confirm_small_move():
    """LONG 同向小幅涨 (<阈值) → CONFIRM (适度反映=确认)."""
    v, s = V("LONG", 100.0, 101.0, TH)
    assert v == "CONFIRM"
    assert abs(s - 0.01) < 1e-9


def test_long_confirm_zero_move():
    """LONG 平盘 → CONFIRM (未反映=假设仍在)."""
    v, s = V("LONG", 100.0, 100.0, TH)
    assert v == "CONFIRM"
    assert s == 0.0


def test_long_reversed():
    """LONG 反向跌超阈值 → REVERSED (市场不认同)."""
    v, s = V("LONG", 100.0, 97.0, TH)
    assert v == "REVERSED"
    assert abs(s + 0.03) < 1e-9


def test_short_price_in():
    """SHORT: 盘前跌超阈值=同向已反映 → PRICE_IN (signed 方向化)."""
    v, s = V("SHORT", 100.0, 97.0, TH)
    assert v == "PRICE_IN"
    assert abs(s - 0.03) < 1e-9


def test_short_reversed():
    """SHORT: 盘前涨超阈值=市场反向 → REVERSED."""
    v, s = V("SHORT", 100.0, 103.0, TH)
    assert v == "REVERSED"
    assert abs(s + 0.03) < 1e-9


def test_short_confirm():
    """SHORT: 微涨/平盘 → CONFIRM."""
    v, _ = V("SHORT", 100.0, 101.0, TH)
    assert v == "CONFIRM"


# ── 边界: 恰在阈值上 ──────────────────────────────────────
def test_boundary_exactly_threshold_is_price_in():
    """signed == threshold → PRICE_IN (≥ 判定)."""
    v, _ = V("LONG", 100.0, 102.0, TH)
    assert v == "PRICE_IN"


def test_boundary_neg_exactly_threshold_is_reversed():
    """signed == -threshold → REVERSED (≤ 判定)."""
    v, _ = V("LONG", 100.0, 98.0, TH)
    assert v == "REVERSED"


def test_just_inside_threshold_is_confirm():
    """±threshold 内侧一点点 → CONFIRM."""
    v1, _ = V("LONG", 100.0, 101.99, TH)
    v2, _ = V("LONG", 100.0, 98.01, TH)
    assert v1 == "CONFIRM"
    assert v2 == "CONFIRM"


# ── 缺价防护 ─────────────────────────────────────────────
def test_no_quote_missing_prev():
    v, s = V("LONG", None, 100.0, TH)
    assert v == "NO_QUOTE" and s is None


def test_no_quote_missing_price():
    v, s = V("LONG", 100.0, None, TH)
    assert v == "NO_QUOTE" and s is None


def test_no_quote_zero_prev():
    v, s = V("LONG", 0.0, 100.0, TH)
    assert v == "NO_QUOTE"


def test_no_quote_negative():
    v, _ = V("SHORT", -1.0, 100.0, TH)
    assert v == "NO_QUOTE"


# ── 阈值参数行为 ──────────────────────────────────────────
def test_high_threshold_never_price_in():
    """撤闸语义: threshold=99 → 永不判 PRICE_IN/REVERSED."""
    v, _ = V("LONG", 100.0, 300.0, 99.0)
    assert v == "CONFIRM"


def test_zero_threshold_any_move_splits():
    """threshold=0: 任何同向动量都判 PRICE_IN."""
    v, _ = V("LONG", 100.0, 100.001, 0.0)
    assert v == "PRICE_IN"


# ── cmd_confirm 当日幂等守卫 (2026-08-26 复查加固: 双季 cron 防重复落样) ──
def test_confirm_dedup_same_day_skips(monkeypatch, tmp_path, capsys):
    """当日已有 confirm 记录 → 幂等跳过, 不再触达配置/行情链路 (--force 同样不绕过)."""
    import datetime as _dtmod
    import json as _jsonmod
    cpath = tmp_path / "ue_confirm_gate.jsonl"
    rec = {"type": "confirm", "date": "2026-08-26", "verdicts": [{"symbol": "X"}]}
    cpath.write_text(_jsonmod.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(_mod, "_CONFIRM_PATH", cpath)
    monkeypatch.setattr(_mod, "_is_us_trading_day_today", lambda: True)
    monkeypatch.setattr(_mod, "_now_et",
                        lambda: _dtmod.datetime(2026, 8, 26, 9, 40,
                                                tzinfo=_dtmod.timezone.utc))
    # 哨兵: 若守卫失效继续往下走 (读配置), 立即炸出 — 证明早退
    def _boom(*a, **k):
        raise AssertionError("幂等守卫未生效: 已越过 dedup 进入裁决链路")
    monkeypatch.setattr(_mod, "_cfg", _boom)

    for force in (False, True):
        rc = _mod.cmd_confirm(force=force)
        assert rc == 0
        assert "幂等跳过" in capsys.readouterr().out
        assert len(cpath.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_confirm_no_prior_record_proceeds_past_dedup(monkeypatch, tmp_path):
    """无当日记录 → 越过 dedup 正常进入后续链路 (以 _cfg 被调用为信号)."""
    import datetime as _dtmod
    monkeypatch.setattr(_mod, "_CONFIRM_PATH", tmp_path / "absent.jsonl")
    monkeypatch.setattr(_mod, "_is_us_trading_day_today", lambda: True)
    monkeypatch.setattr(_mod, "_now_et",
                        lambda: _dtmod.datetime(2026, 8, 26, 9, 40,
                                                tzinfo=_dtmod.timezone.utc))
    calls = []
    monkeypatch.setattr(_mod, "_cfg", lambda *a, **k: calls.append(a) or 0.02)

    class _Stop(Exception):
        pass
    monkeypatch.setattr(_mod, "_read_jsonl",
                        lambda p: (_raise(_Stop) if p == _mod._SCAN_PATH else []))
    try:
        _mod.cmd_confirm(force=True)
    except _Stop:
        pass
    assert calls, "dedup 误拦了无记录日: 未进入裁决链路"


def _raise(exc):
    raise exc
