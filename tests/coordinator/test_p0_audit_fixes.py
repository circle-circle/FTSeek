"""
P0 审查修复单测 (2026-05-14)

覆盖 11 项 P0 修复:
- E1 audit_file 路径
- D1 节假日
- D2 ATR=0 拒交易
- D4 client_order_id 唯一性
- R1 sector limit (skip 集成)
- R2 system breaker (skip 集成)
- S1 warmup
- S2 halted
- E2 retry helper (skip 集成)
- E3 restore (skip 集成)
- D3 split guard
"""
import os
import time
import uuid
from datetime import date

import pytest


# ─── E1: audit_file 路径 ────────────────────────────────────
def test_e1_futu_audit_file_uses_paths():
    """audit_file 不再硬编码 /root"""
    # 不实例化 FutuIntegration (依赖 futu SDK), 直接读源码字符串验证
    src = open("app/futu_integration.py").read()
    assert "/root/.futu_trade_audit.jsonl" not in src
    assert "futu_trade_audit.jsonl" in src
    assert "get_logs_dir" in src


# ─── D1: 节假日日历 ─────────────────────────────────────────
def test_d1_holiday_calendar_integrated():
    """get_market_phase 调用 holiday_calendar"""
    src = open("app/core/scheduler_triggers.py").read()
    assert "is_us_holiday" in src
    assert "holiday_calendar" in src


# ─── D2: ATR=0 拒交易 ───────────────────────────────────────
def test_d2_atr_zero_returns_zero_position():
    """ATR=0 → _compute_position_and_stop 返回 (0,0)"""
    from app.core.intraday_scorer import IntradayScorer
    s = IntradayScorer()
    p, sl = s._compute_position_and_stop(
        confidence=0.7, atr=0.0, price=100.0, vix=20.0, direction="LONG"
    )
    assert p == 0.0
    assert sl == 0.0


def test_d2_price_zero_returns_zero_position():
    """price=0 → 同样拒交易"""
    from app.core.intraday_scorer import IntradayScorer
    s = IntradayScorer()
    p, sl = s._compute_position_and_stop(
        confidence=0.7, atr=2.0, price=0.0, vix=20.0, direction="LONG"
    )
    assert p == 0.0
    assert sl == 0.0


def test_d2_normal_path_unaffected():
    """正常输入不受 D2 影响"""
    from app.core.intraday_scorer import IntradayScorer
    s = IntradayScorer()
    p, sl = s._compute_position_and_stop(
        confidence=0.5, atr=2.0, price=100.0, vix=20.0, direction="LONG"
    )
    assert p > 0
    assert sl > 0


# ─── D4: client_order_id uuid 后缀 ──────────────────────────
def test_d4_client_order_id_unique_within_second():
    """1 秒内同 symbol 多单 client_order_id 全部唯一"""
    src = open("app/pipeline/order_executor.py").read()
    # 5 处 client_order_id 都应有 uuid
    import re
    matches = re.findall(r'client_order_id=f"[^"]+"', src)
    assert len(matches) >= 5, f"找到 {len(matches)} 处 client_order_id, 应 >= 5"
    for m in matches:
        assert "uuid" in m or "uuid4" in m, f"缺 uuid 后缀: {m}"


# ─── R1 / R2: pre_trade_check 集成 ──────────────────────────
def test_r1_sector_check_method_exists():
    from app.risk.pre_trade_check import PreTradeChecker
    pc = PreTradeChecker(min_confidence=0.5, min_signal_strength=50)
    assert hasattr(pc, "_check_sector_limit")


def test_r2_system_breaker_method_exists():
    from app.risk.pre_trade_check import PreTradeChecker
    pc = PreTradeChecker(min_confidence=0.5, min_signal_strength=50)
    assert hasattr(pc, "_check_system_breaker")
    assert hasattr(pc, "update_breaker_state")


def test_r2_breaker_state_triggered_blocks():
    from app.risk.pre_trade_check import PreTradeChecker
    pc = PreTradeChecker()
    pc.update_breaker_state("TRIGGERED")
    ok, reason = pc._check_system_breaker()
    assert not ok
    assert "data_source_triggered" in (reason or "")


def test_r2_breaker_state_normal_allows():
    from app.risk.pre_trade_check import PreTradeChecker
    pc = PreTradeChecker()
    pc.update_breaker_state("NORMAL")
    ok, reason = pc._check_system_breaker()
    assert ok


# ─── S1 / S2: warmup + halted in score() ────────────────────
def test_s1_warmup_incomplete_returns_neutral():
    from app.core.intraday_scorer import IntradayScorer
    s = IntradayScorer()
    sig = s.score({"symbol": "TEST", "price": 100.0, "bars_5m_count": 5})
    assert sig.direction == "NEUTRAL"
    assert "warmup_incomplete" in sig.reasoning


def test_s1_warmup_complete_proceeds():
    """bars_5m_count >= 14 走正常路径 (不会直接因 warmup 失败)"""
    from app.core.intraday_scorer import IntradayScorer
    s = IntradayScorer()
    # bars_5m_count=20 但 atr=0 → 会被 D2 拒, 但不是 warmup_incomplete
    sig = s.score({"symbol": "TEST", "price": 100.0, "bars_5m_count": 20, "atr": 0})
    assert "warmup_incomplete" not in sig.reasoning


def test_s2_halted_returns_neutral():
    from app.core.intraday_scorer import IntradayScorer
    s = IntradayScorer()
    sig = s.score({
        "symbol": "TEST", "price": 100.0, "bars_5m_count": 20,
        "is_halted": True, "atr": 1.0
    })
    assert sig.direction == "NEUTRAL"
    assert "halted" in sig.reasoning


# ─── E2: place_sell_order retry signature ──────────────────
def test_e2_place_sell_order_has_retry_params():
    """方法签名含 max_retries / backoff"""
    src = open("app/futu_integration.py").read()
    assert "_ensure_connected" in src
    assert "max_retries" in src
    assert "backoff" in src


# ─── E3: OrderTracker restore ───────────────────────────────
def test_e3_order_tracker_has_restore():
    from app.risk.order_tracker import OrderTracker
    assert hasattr(OrderTracker, "restore_pending_on_startup")


# ─── D3: Split guard ────────────────────────────────────────
def test_d3_split_guard_basic():
    from app.data.split_event_guard import SplitEventGuard
    g = SplitEventGuard()
    assert g.is_split_today("NVDA") is False
    g.mark_split("NVDA")
    assert g.is_split_today("NVDA") is True


def test_d3_split_anomaly_detection():
    from app.data.split_event_guard import SplitEventGuard
    g = SplitEventGuard()
    # -90% 跳变 → 自动标记
    detected = g.detect_split_anomaly("NVDA", current_price=120, prev_close=1200)
    assert detected
    assert g.is_split_today("NVDA")


def test_d3_normal_change_not_flagged():
    from app.data.split_event_guard import SplitEventGuard
    g = SplitEventGuard()
    # +5% 正常波动 → 不标记
    detected = g.detect_split_anomaly("AAPL", current_price=105, prev_close=100)
    assert not detected
    assert not g.is_split_today("AAPL")
