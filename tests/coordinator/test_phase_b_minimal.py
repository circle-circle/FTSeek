"""
Phase B 最小实施单测 (PB-2/PB-3, 2026-05-15)

覆盖:
- L3 thesis_inferer 启用状态
- ExitPolicy thesis-aware 行为 (无 thesis fallback / event horizon / breakout failure / scalping 30min)
"""
import os
import sqlite3
import tempfile
import pytest


# ─── PB-1: L3 启用状态 ─────────────────────────────────────
def test_pb1_thesis_inferer_enabled_in_yaml():
    """yaml `llm_features.thesis_inferer.enabled = true`"""
    import yaml
    cfg = yaml.safe_load(open("config/unified_config.yaml"))
    assert cfg["llm_features"]["thesis_inferer"]["enabled"] is True


# ─── PB-2: order_executor 接入 thesis_inferer ────────────
def test_pb2_order_executor_calls_thesis_inferer():
    """order_executor 入场后异步调 infer_thesis_async.

    2026-05-16: thesis_inferer 调用从 order_executor 重构到 post_trade_hook
    (4 个散乱副作用合并为统一钩子). 测试相应更新: 检查任一文件含调用.
    """
    src_oe = open("app/pipeline/order_executor.py").read()
    src_pth = open("app/pipeline/post_trade_hook.py").read()
    assert "on_position_opened" in src_oe, "order_executor 应调用 on_position_opened"
    assert "infer_thesis_async" in src_pth, "post_trade_hook 应含 infer_thesis_async"


# ─── PB-3: ExitPolicy thesis-aware 行为 ───────────────────
def test_pb3_no_thesis_fallback():
    """无 entry_thesis → fallback NO_ACTION"""
    from app.strategies.exit_policies import MainPipelineExitPolicy

    class _Pos:
        symbol = "TEST"
        trace_id = ""  # 无 trace_id → 读不到 thesis
        avg_entry_price = 100
        direction = "LONG"

    p = MainPipelineExitPolicy()
    decision = p.evaluate(_Pos(), {"current_price": 105})
    assert decision.action == "NO_ACTION"
    assert "fallback" in decision.reason or "no_thesis" in decision.reason


def test_pb3_main_event_thesis_horizon_close():
    """main + event thesis + 持仓 > 48h → CLOSE"""
    from app.strategies.exit_policies import MainPipelineExitPolicy, _read_entry_thesis
    from unittest.mock import patch

    class _Pos:
        symbol = "TEST"
        trace_id = "trace-event"
        avg_entry_price = 100
        direction = "LONG"
        holding_hours = 50  # > 48h

    p = MainPipelineExitPolicy()
    with patch("app.strategies.exit_policies._read_entry_thesis", return_value="event"):
        decision = p.evaluate(_Pos(), {"current_price": 105})
    assert decision.action == "CLOSE"
    assert "EVENT_HORIZON" in decision.reason


def test_pb3_main_breakout_failed_close():
    """main + breakout thesis + 跌破 0.5% → CLOSE"""
    from app.strategies.exit_policies import MainPipelineExitPolicy
    from unittest.mock import patch

    class _Pos:
        symbol = "TEST"
        trace_id = "trace-bo"
        avg_entry_price = 100
        direction = "LONG"
        holding_hours = 1

    p = MainPipelineExitPolicy()
    with patch("app.strategies.exit_policies._read_entry_thesis", return_value="breakout"):
        decision = p.evaluate(_Pos(), {"current_price": 99.4})  # -0.6%
    assert decision.action == "CLOSE"
    assert "BREAKOUT_FAILED" in decision.reason


def test_pb3_main_reversion_quick_profit_reduce():
    """main + reversion + +1.5% → REDUCE 50%"""
    from app.strategies.exit_policies import MainPipelineExitPolicy
    from unittest.mock import patch

    class _Pos:
        symbol = "TEST"
        trace_id = "trace-rev"
        avg_entry_price = 100
        direction = "LONG"
        holding_hours = 0.5

    p = MainPipelineExitPolicy()
    with patch("app.strategies.exit_policies._read_entry_thesis", return_value="reversion"):
        decision = p.evaluate(_Pos(), {"current_price": 102})  # +2%
    assert decision.action == "REDUCE"
    assert decision.qty_pct == 0.5
    assert "REVERSION_QUICK_PROFIT" in decision.reason


def test_pb3_scalping_30min_close():
    """scalping 持仓 > 30min → CLOSE"""
    from app.strategies.exit_policies import ScalpingExitPolicy
    from unittest.mock import patch

    class _Pos:
        symbol = "TEST"
        trace_id = "trace-sc"
        avg_entry_price = 50
        direction = "LONG"
        holding_hours = 0.7  # 42 min > 30

    p = ScalpingExitPolicy()
    with patch("app.strategies.exit_policies._read_entry_thesis", return_value="momentum"):
        decision = p.evaluate(_Pos(), {"current_price": 51})
    assert decision.action == "CLOSE"
    assert "30MIN" in decision.reason


def test_pb3_fast_lane_1atr_profit_lock():
    """fast_lane + 浮盈 ≥ 1×ATR → REDUCE 50%"""
    from app.strategies.exit_policies import FastLaneExitPolicy
    from unittest.mock import patch

    class _Pos:
        symbol = "TEST"
        trace_id = "trace-fl"
        avg_entry_price = 100
        direction = "LONG"
        holding_hours = 0.3

    p = FastLaneExitPolicy()
    with patch("app.strategies.exit_policies._read_entry_thesis", return_value="momentum"):
        decision = p.evaluate(_Pos(), {"current_price": 103, "atr": 2.5})  # +3 = 1.2 ATR
    assert decision.action == "REDUCE"
    assert "1ATR_PROFIT_LOCK" in decision.reason


def test_pb3_llm_plan_target_hit_close():
    """llm_plan + 价命中 plan_target → CLOSE"""
    from app.strategies.exit_policies import LLMPlanExitPolicy
    from unittest.mock import patch

    class _Pos:
        symbol = "TEST"
        trace_id = "trace-llm"
        avg_entry_price = 100
        direction = "LONG"
        metadata = {"plan_target": 110, "plan_stop": 95}

    p = LLMPlanExitPolicy()
    with patch("app.strategies.exit_policies._read_entry_thesis", return_value="event"):
        decision = p.evaluate(_Pos(), {"current_price": 110.5})
    assert decision.action == "CLOSE"
    assert "TARGET_HIT" in decision.reason


def test_pb3_llm_plan_stop_hit_close():
    """llm_plan + 跌破 plan_stop → CLOSE"""
    from app.strategies.exit_policies import LLMPlanExitPolicy
    from unittest.mock import patch

    class _Pos:
        symbol = "TEST"
        trace_id = "trace-llm-s"
        avg_entry_price = 100
        direction = "LONG"
        metadata = {"plan_target": 110, "plan_stop": 95}

    p = LLMPlanExitPolicy()
    with patch("app.strategies.exit_policies._read_entry_thesis", return_value="event"):
        decision = p.evaluate(_Pos(), {"current_price": 94})
    assert decision.action == "CLOSE"
    assert "STOP_HIT" in decision.reason


# ─── 验证 phase_b_status CLI 可 import ─────────────────
def test_pb_status_cli_importable():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pbs", "scripts/phase_b_status.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "_check_dispatcher")
    assert hasattr(mod, "_check_thesis_inferer")
    assert hasattr(mod, "_check_arbitrator")
