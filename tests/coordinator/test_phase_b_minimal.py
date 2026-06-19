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
# 2026-06-19: app/strategies/exit_policies.py 已随旧退出体系移除,
# 退出逻辑现由 app/risk/exit_rules/ + ExitMonitor 承载.
# 原 test_pb3_* (MainPipelineExitPolicy/ScalpingExitPolicy/FastLaneExitPolicy/LLMPlanExitPolicy) 全部失效.


# ─── 验证 phase_b_status CLI 可 import ─────────────────
def test_pb_status_cli_importable():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pbs", "scripts/phase_b_status.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "_check_dispatcher")
    assert hasattr(mod, "_check_thesis_inferer")
    assert hasattr(mod, "_check_arbitrator")
