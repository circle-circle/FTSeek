"""BE 出场分类契约测试 (2026-08-21).

Qoder §56.3 判序 bug 修复的回归守护: 旧序 GAP 先判致 BWXT drift=-15.44bp 但
净+8.82 被标瀑布 (盈利单配瀑布标签自相矛盾). 修复后 OK 先判, 与分析口径
(47笔 17/6/24 净额优先) 对齐.
"""
from app.playbook.pipeline import classify_be_exit


class TestClassifyBeExit:
    def test_net_positive_is_ok_even_with_waterfall_drift(self):
        # BWXT 实弹场景: drift=-15.44bp 但净+8.82 → 必须标 OK (盈利即名实相符)
        assert classify_be_exit(8.82, -15.44) == "OK"

    def test_net_positive_no_telemetry_is_ok(self):
        assert classify_be_exit(4.0, None) == "OK"

    def test_waterfall_with_loss_is_gap(self):
        # 净亏 + 坠穿≥5bp → GAP (砍的是坠落本身)
        assert classify_be_exit(-10.0, -8.0) == "GAP"
        assert classify_be_exit(-0.5, -5.0) == "GAP"  # 边界含 -5.0

    def test_slip_residue(self):
        # 净亏但坠穿<5bp → SLIP (门口成交滑点/费用残差)
        assert classify_be_exit(-1.2, -3.0) == "SLIP"
        assert classify_be_exit(-0.8, None) == "SLIP"

    def test_zero_net_falls_through(self):
        # 净=0 不算 OK (>0 才名实相符), 无坠穿 → SLIP
        assert classify_be_exit(0.0, -2.0) == "SLIP"

    def test_fail_open_no_fees_no_telemetry(self):
        # 净额与 drift 都缺失 → SLIP (fail-open 不抛异常)
        assert classify_be_exit(None, None) == "SLIP"
        assert classify_be_exit(None, -9.9) == "GAP"  # 有 drift 仍可判瀑布
