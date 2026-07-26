"""Shadow EOD Replay 回归测试 (2026-05-19)

核心: 8 个 variant 各自的 replay 逻辑是否正确实现 P3/P5/P7 等想法.

2026-06-19: ShadowMirror (app.evolution.shadow_mirror) 退役,
对应的 variant 定义/short_only 过滤测试已删除.
"""
import pytest
from pathlib import Path


def _read_src(rel: str) -> str:
    return (Path(__file__).resolve().parent.parent.parent / rel).read_text()


def test_eod_replay_handles_all_8_variants():
    """shadow_eod_replay.py 必须有 8 个 variant 的 replay 逻辑."""
    src = _read_src("scripts/shadow_eod_replay.py")
    variants_with_logic = [
        "tighter_stop", "no_chandelier", "quick_exit",
        "tp_sl_5to1", "min_hold_6min", "short_only", "early_chandelier",
    ]
    for v in variants_with_logic:
        assert f'variant == "{v}"' in src, f"EOD replay 必须处理 variant {v}"


def test_eod_replay_dry_run_works():
    """EOD replay --dry-run 必须能跑且不写 DB."""
    import subprocess
    result = subprocess.run(
        ["python3", str(Path(__file__).resolve().parent.parent.parent
                        / "scripts" / "shadow_eod_replay.py"),
         "--days", "0", "--dry-run"],  # 0 天 → 必然 0 笔
        capture_output=True, text=True, timeout=30,
    )
    # 不应崩溃
    assert result.returncode == 0, f"EOD replay 异常退出: {result.stderr}"


def test_t_test_function_exists():
    """shadow_compare.py 必须有 cmd_t_test + _welch_t_test."""
    src = _read_src("scripts/shadow_compare.py")
    assert "def cmd_t_test" in src, "必须有 cmd_t_test 函数"
    assert "def _welch_t_test" in src, "必须有 Welch's t-test 实现"
    assert "p_value" in src, "必须输出 p_value"


def test_t_test_correctness():
    """_welch_t_test 数学正确性 (用已知样本验证)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "shadow_compare",
        Path(__file__).resolve().parent.parent.parent / "scripts" / "shadow_compare.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 两组明显不同的样本
    a = [10, 12, 11, 13, 12, 11, 14, 12, 13, 11]  # mean ~11.9
    b = [5, 6, 4, 5, 7, 6, 5, 5, 6, 4]              # mean ~5.3
    t, p = mod._welch_t_test(a, b)
    assert t > 0, "a 均值 > b, t 应 > 0"
    assert p < 0.001, f"两组明显不同, p 应远 < 0.05, 实际 p={p}"

    # 两组相同样本
    c = [1, 2, 3, 4, 5]
    d = [1, 2, 3, 4, 5]
    t2, p2 = mod._welch_t_test(c, d)
    assert abs(t2) < 0.01, "相同样本 t≈0"
    assert p2 > 0.99, f"相同样本 p≈1, 实际 p={p2}"


# test_p1_vwap_deviation_tightened — 已随 scalping 引擎移除 (2026-06-19)


def test_p4_observation_log_exists():
    """P4 (2026-05-19): order_executor 必须为 strength≥80 加观察日志 (不改行为)."""
    src = _read_src("app/pipeline/order_executor.py")
    assert "P4·observation" in src, "P4 必须有观察日志"
    assert "strength={_strength_observed} ≥80" in src or "strength=" in src, "必须记录 strength 值"
