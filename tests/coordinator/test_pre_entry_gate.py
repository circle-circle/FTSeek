"""Pre-Entry Signal Gate 回归测试 (2026-05-19)

核心安全要点:
1. 默认 disabled → check_gate 立即返回 go=True
2. LLM 失败 → 降级 go=True (不阻塞主线)
3. 历史 < min 笔数 → 放行 (不拒新机会)
4. 历史失败模式 (LCID LONG 14 笔 0 胜率) → LLM 应 go=False
5. 缓存 5min 内复用同 symbol+direction 决策
"""
import time
from unittest.mock import patch, MagicMock
import pytest


def test_gate_disabled_returns_go_true():
    """默认 disabled, check_gate 立即返回 go=True 不调 LLM."""
    from app.llm.pre_entry_gate import check_gate, reset_cache
    reset_cache()
    decision = check_gate(
        "US.LCID", "LONG",
        signal={"strength": 65, "confidence": 0.7},
        market_ctx={"regime": "TREND_DOWN"}
    )
    assert decision.go is True, "默认 disabled 必须放行"
    assert decision.source == "fallback"


def test_gate_history_insufficient_passes():
    """历史 < 5 笔 (新 symbol) → 放行避免错过机会."""
    from app.llm.pre_entry_gate import check_gate, reset_cache
    from app.llm.features import feature_flags
    reset_cache()

    # 模拟 enabled
    with patch("app.llm.pre_entry_gate.is_enabled", lambda f: f == "pre_entry_gate"), \
         patch("app.llm.pre_entry_gate.get_feature_config", lambda f: {"cache_ttl_sec": 300, "min_history_trades": 5, "timeout_sec": 5}):
        with patch("app.llm.pre_entry_gate._query_history") as mock_hist:
            mock_hist.return_value = {
                "trades": 2, "wins": 0, "win_rate": 0,  # < min_history=5
                "total_pnl": -10, "avg_pnl": -5,
                "sample_exit_reasons": [], "sample_holding_hours": [],
            }
            decision = check_gate(
                "US.NEW_SYMBOL", "LONG",
                signal={"strength": 65, "confidence": 0.7},
                market_ctx={"regime": "RANGE"}
            )
    assert decision.go is True, "历史不足必须放行 (避免错过新机会)"
    assert "insufficient_history" in decision.reason
    assert decision.source == "no_history"


def test_gate_llm_failure_falls_back_to_go():
    """LLM 调用失败 → 降级 go=True (核心安全保证)."""
    from app.llm.pre_entry_gate import check_gate, reset_cache
    from app.llm.features import feature_flags
    reset_cache()

    with patch("app.llm.pre_entry_gate.is_enabled", lambda f: f == "pre_entry_gate"), \
         patch("app.llm.pre_entry_gate.get_feature_config", lambda f: {"cache_ttl_sec": 300, "min_history_trades": 5, "timeout_sec": 5}):
        with patch("app.llm.pre_entry_gate._query_history") as mock_hist:
            mock_hist.return_value = {
                "trades": 14, "wins": 0, "win_rate": 0,
                "total_pnl": -694, "avg_pnl": -49,
                "sample_exit_reasons": ["sl_hit"] * 5,
                "sample_holding_hours": [0.05] * 5,
            }
            # Patch model_call to return failure
            with patch("app.llm.model_call.call_llm") as mock_llm:
                from app.llm.model_call import CallResult
                mock_llm.return_value = CallResult(success=False, error="timeout")
                decision = check_gate(
                    "US.LCID", "LONG",
                    signal={"strength": 65, "confidence": 0.7},
                    market_ctx={}
                )
    # 关键安全保证: LLM 失败 → 不阻塞主线
    assert decision.go is True, "LLM 失败必须降级放行"
    assert decision.source == "fallback"
    assert "llm_failed" in decision.reason


def test_gate_llm_rejection_when_history_bad():
    """LLM 看历史 14 笔 0 胜率 → 应返回 go=False."""
    from app.llm.pre_entry_gate import check_gate, reset_cache
    from app.llm.features import feature_flags
    from app.llm.model_call import CallResult
    reset_cache()

    with patch("app.llm.pre_entry_gate.is_enabled", lambda f: f == "pre_entry_gate"), \
         patch("app.llm.pre_entry_gate.get_feature_config", lambda f: {"cache_ttl_sec": 300, "min_history_trades": 5, "timeout_sec": 5}):
        with patch("app.llm.pre_entry_gate._query_history") as mock_hist:
            mock_hist.return_value = {
                "trades": 14, "wins": 0, "win_rate": 0.0,
                "total_pnl": -694, "avg_pnl": -49,
                "sample_exit_reasons": ["sl_hit"] * 5,
                "sample_holding_hours": [0.05] * 5,
            }
            with patch("app.llm.model_call.call_llm") as mock_llm:
                mock_llm.return_value = CallResult(
                    success=True,
                    response={
                        "go": False,
                        "reason": "14 trades 0 wins, proven failure mode",
                        "confidence": 0.9,
                    },
                    model_used="deepseek-pro",
                    latency_ms=850,
                )
                decision = check_gate(
                    "US.LCID", "LONG",
                    signal={"strength": 65, "confidence": 0.7},
                    market_ctx={"regime": "RANGE"}
                )
    assert decision.go is False, "历史 14 笔 0 胜率应被 LLM 拦截"
    assert "0 wins" in decision.reason or "failure mode" in decision.reason
    assert decision.confidence >= 0.8
    assert decision.source == "llm"


def test_gate_cache_reuse():
    """同 symbol+direction 5min 内复用缓存, 不重复调 LLM."""
    from app.llm.pre_entry_gate import check_gate, reset_cache
    from app.llm.features import feature_flags
    from app.llm.model_call import CallResult
    reset_cache()

    call_count = {"n": 0}
    def mock_llm_call(*args, **kwargs):
        call_count["n"] += 1
        return CallResult(
            success=True,
            response={"go": True, "reason": "ok", "confidence": 0.7},
            model_used="deepseek-pro", latency_ms=500,
        )

    with patch("app.llm.pre_entry_gate.is_enabled", lambda f: f == "pre_entry_gate"), \
         patch("app.llm.pre_entry_gate.get_feature_config", lambda f: {"cache_ttl_sec": 300, "min_history_trades": 5, "timeout_sec": 5}):
        with patch("app.llm.pre_entry_gate._query_history") as mock_hist:
            mock_hist.return_value = {
                "trades": 10, "wins": 5, "win_rate": 0.5,
                "total_pnl": 100, "avg_pnl": 10,
                "sample_exit_reasons": [], "sample_holding_hours": [],
            }
            with patch("app.llm.model_call.call_llm", side_effect=mock_llm_call):
                # 第 1 次 → 调 LLM
                d1 = check_gate("US.NVDA", "LONG", {"strength": 60, "confidence": 0.6}, {})
                # 第 2 次 → 缓存命中
                d2 = check_gate("US.NVDA", "LONG", {"strength": 60, "confidence": 0.6}, {})

    assert call_count["n"] == 1, f"缓存失效, LLM 被调 {call_count['n']} 次 (期望 1)"
    assert d1.go is True
    assert d2.go is True
    assert d2.source == "cache"


def test_gate_cache_separate_per_direction():
    """同 symbol 不同 direction 应分开缓存 (LONG vs SHORT 不复用)."""
    from app.llm.pre_entry_gate import check_gate, reset_cache
    from app.llm.features import feature_flags
    from app.llm.model_call import CallResult
    reset_cache()

    call_count = {"n": 0}
    def mock_llm_call(*args, **kwargs):
        call_count["n"] += 1
        return CallResult(
            success=True,
            response={"go": True, "reason": "ok", "confidence": 0.7},
            model_used="deepseek-pro", latency_ms=500,
        )

    with patch("app.llm.pre_entry_gate.is_enabled", lambda f: f == "pre_entry_gate"), \
         patch("app.llm.pre_entry_gate.get_feature_config", lambda f: {"cache_ttl_sec": 300, "min_history_trades": 5, "timeout_sec": 5}):
        with patch("app.llm.pre_entry_gate._query_history") as mock_hist:
            mock_hist.return_value = {
                "trades": 10, "wins": 5, "win_rate": 0.5,
                "total_pnl": 100, "avg_pnl": 10,
                "sample_exit_reasons": [], "sample_holding_hours": [],
            }
            with patch("app.llm.model_call.call_llm", side_effect=mock_llm_call):
                check_gate("US.AAPL", "LONG", {"strength": 60}, {})
                check_gate("US.AAPL", "SHORT", {"strength": 60}, {})

    assert call_count["n"] == 2, "LONG 和 SHORT 必须分开缓存"


def test_gate_disabled_no_history_query():
    """默认 disabled 时, 不应查 trade_records DB (避免不必要 IO)."""
    from app.llm.pre_entry_gate import check_gate, reset_cache
    from app.llm.features import feature_flags
    reset_cache()

    with patch("app.llm.pre_entry_gate._query_history") as mock_hist:
        # 默认 disabled
        with patch("app.llm.pre_entry_gate.is_enabled", lambda f: False):
            decision = check_gate("US.LCID", "LONG", {"strength": 60}, {})

    assert decision.go is True
    assert mock_hist.call_count == 0, "disabled 时不应查历史"


def test_gate_hard_timeout_protects_main_thread():
    """关键: LLM 卡住 > timeout_sec+1, Pre-Gate 硬超时返 go=True, 不阻塞主线 entry.

    场景: model_call 内部 retry + fallback 可能 22s, 主线 entry 不能等.
    硬超时机制: ThreadPoolExecutor + future.result(timeout=timeout_sec+1).
    """
    from app.llm.pre_entry_gate import check_gate, reset_cache
    import time as _time
    reset_cache()

    def slow_llm(*args, **kwargs):
        # B1 测试卫生 (2026-08-24): 10s→4s. 只需超过硬超时 (timeout_sec+1=3s) 即可
        # 证明保护生效; 原 10s 在 executor 线程里空耗并拖慢 pytest 会话退出.
        _time.sleep(4)  # 模拟 LLM 长时间卡住 (超过硬超时 3s)
        from app.llm.model_call import CallResult
        return CallResult(success=True, response={"go": False, "reason": "should not reach", "confidence": 1.0})

    with patch("app.llm.pre_entry_gate.is_enabled", lambda f: f == "pre_entry_gate"), \
         patch("app.llm.pre_entry_gate.get_feature_config",
               lambda f: {"cache_ttl_sec": 300, "min_history_trades": 5, "timeout_sec": 2}), \
         patch("app.llm.pre_entry_gate._query_history") as mock_hist:
        mock_hist.return_value = {
            "trades": 14, "wins": 0, "win_rate": 0,
            "total_pnl": -694, "avg_pnl": -49,
            "sample_exit_reasons": [], "sample_holding_hours": [],
        }
        with patch("app.llm.model_call.call_llm", side_effect=slow_llm):
            t0 = _time.time()
            decision = check_gate("US.LCID", "LONG", {"strength": 65}, {})
            elapsed = _time.time() - t0

    # 硬超时: timeout_sec=2 + 1s buffer = 3s. 实测必须 < 5s (含线程调度 buffer)
    assert elapsed < 5, f"硬超时失败 — 主线被阻塞 {elapsed:.1f}s (期望 < 3s)"
    # 必须降级 go=True (不阻塞主线)
    assert decision.go is True, "硬超时必须降级放行 (核心安全)"
    assert "hard_timeout" in decision.reason or "timeout" in decision.reason.lower()
