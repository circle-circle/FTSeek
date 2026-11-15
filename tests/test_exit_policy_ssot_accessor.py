"""exit_policy 读取入口的契约 (2026-11-15 根因批)。

病灶: position_sync.py(3 处) 与 order_executor.py(1 处) 都写成
`ExitPolicyGuardian()._load_exit_policy_dict()` —— 该方法**只存在于 PositionExitMonitor**,
ExitPolicyGuardian 从没有过 ⇒ 每次调用必抛 AttributeError, 被外层
`except Exception` 吞掉 ⇒ 四处"读 SSOT"实为恒读硬编码 fallback; 而且每标的每轮
新建 guardian。日志干净、py_compile 通过、测试全绿 (典型的"死门第三形态")。

本组用例钉三件事:
  1. 访问器存在且返回真实 policy dict (不是空 dict);
  2. 进程级单例: get_guardian() 同一实例, 二次读取零 IO;
  3. 源码契约: 那两处不得再出现 `_load_exit_policy_dict` (防回潮)。
"""
from pathlib import Path

import pytest

from app.risk import exit_policy_guardian as epg

ROOT = Path(__file__).resolve().parents[1]


def test_accessor_exists_and_returns_real_policy():
    policy = epg.ExitPolicyGuardian().load_policy_dict()
    assert isinstance(policy, dict) and policy, "应返回非空 policy dict"
    assert "exit_rules" in policy
    initial = (policy["exit_rules"]["atr_stop"]["multiplier_by_profit"]["initial"])
    assert float(initial) > 0


def test_get_guardian_is_process_singleton():
    first = epg.get_guardian()
    second = epg.get_guardian()
    assert first is second
    assert first.load_policy_dict() is first.load_policy_dict(), "二次读取必须命中实例缓存"


def test_second_load_does_no_io(monkeypatch):
    guardian = epg.get_guardian()
    guardian.load_policy_dict()  # 预热

    def _boom(*args, **kwargs):
        raise AssertionError("缓存命中路径不应再读 yaml")

    monkeypatch.setattr(guardian, "load", _boom)
    assert guardian.load_policy_dict()["exit_rules"]


@pytest.mark.parametrize("rel", [
    "app/core/position_sync.py",
    "app/pipeline/order_executor.py",
])
def test_no_call_to_nonexistent_private_loader(rel):
    """源码契约: 这些模块不得再**调用** ExitPolicyGuardian 上不存在的私有方法。

    用 AST 找 `X._load_exit_policy_dict` 属性访问 —— 别用文本 grep: 本文件/被扫文件里的
    docstring 会复述这个方法名 (解释病因), 文本匹配会把散文当违规 (同一个"散文误报"坑)。
    """
    import ast

    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    bad = [node.lineno for node in ast.walk(tree)
           if isinstance(node, ast.Attribute) and node.attr == "_load_exit_policy_dict"]
    assert not bad, (
        f"{rel}:{bad} 又调用 _load_exit_policy_dict —— ExitPolicyGuardian 没有这个方法, "
        "调用只会抛 AttributeError 被吞掉; 请用 get_guardian().load_policy_dict()")


def test_position_sync_reads_ssot_not_hardcode():
    """恢复路径合成 SL 的 ATR 倍数必须来自 SSOT (而不是又写死一个 4.0)。"""
    src = (ROOT / "app/core/position_sync.py").read_text(encoding="utf-8")
    assert "get_guardian().load_policy_dict()" in src


def test_order_executor_reads_ssot_not_pinned_constant():
    """下单股数路径也必须读 SSOT (2026-11-15 第二轮: 原钉 _ATR_STOP_MULT_LEGACY=4.0)。

    注: 这条翻转是**口径统一**, 不是尺寸变更 —— 静态不变量见
    tests/test_position_sizer_default_parity.py::test_atr_multiplier_flip_is_sizing_invariant。
    """
    src = (ROOT / "app/pipeline/order_executor.py").read_text(encoding="utf-8")
    assert "_ATR_STOP_MULT_LEGACY" not in src, "又用硬编码钉住 ATR 倍数了"
    assert "load_policy_dict()" in src
    assert "get_guardian()" in src


def test_ssot_atr_multiplier_value():
    """SSOT 现役值必须能被读到 (2.0, medium 桶默认, 盘中由 vol bucket 覆盖)。"""
    policy = epg.get_guardian().load_policy_dict()
    value = float(policy["exit_rules"]["atr_stop"]["multiplier_by_profit"]["initial"])
    assert value == 2.0