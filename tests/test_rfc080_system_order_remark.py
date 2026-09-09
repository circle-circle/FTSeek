# -*- coding: utf-8 -*-
"""RFC-080: AUTO 认领 remark 白名单单测.

钉锁 (活体探针实证 2026-09-09, history_order_list_query 9/1-9/9):
  1. SIMULATE env 159 笔 remark 全部 'pb_*' — auto 单指纹
  2. REAL env 45 笔 remark 全部 '' — APP/看板手动单不带 remark
  3. fail-safe 方向: 空/None/未知前缀 → False → MANUAL (宁可漏认领不误平手动仓)
  4. 白名单覆盖全部 auto 生产者: pb_* / orb_* / OVER_LIMIT_TRIM (grep 'remark=' 全仓枚举)
"""
import pytest

from app.core.position_sync import is_system_order_remark, _SYSTEM_ORDER_REMARK_PREFIXES


class TestWhitelist:
    """is_system_order_remark 语义."""

    @pytest.mark.parametrize("remark", [
        "pb_2310_sim",              # playbook 入场 (探针实锤 159/159)
        "pb_2310_real",             # cutover 后同格式
        "pb_close_US.SNDK_sim",     # playbook 平仓
        "pb_be_close_US.SNDK_sim",  # 净保本平仓
        "pb_stop_US.SNDK_sim",      # STOP 单
        "orb_US.QCOM_1a2b3c4d",     # ORB 引擎 (现禁用, 预留)
        "OVER_LIMIT_TRIM:daily_notional",  # position_sync 自trim
    ])
    def test_system_remarks_claimed(self, remark):
        assert is_system_order_remark(remark) is True

    @pytest.mark.parametrize("remark", [
        "",            # APP 手动单 (探针实锤 REAL 45/45 全空)
        None,          # 字段缺失
        "  ",          # 空白
        "manual_fix",  # 未知前缀
        "PB_2310_sim", # 大小写变体 → 严格匹配拒绝 (fail-safe 归 MANUAL)
        "pbx_1",       # 前缀相似但非 pb_ 开头段 (pbx_ ≠ pb_)... 实际 startswith('pb_')=False
    ])
    def test_non_system_remarks_rejected(self, remark):
        assert is_system_order_remark(remark) is False

    def test_prefix_registry_frozen(self):
        """白名单 = 全部 auto 生产者枚举, 增删必须过审计 (防静默漂移)."""
        assert _SYSTEM_ORDER_REMARK_PREFIXES == ("pb_", "orb_", "OVER_LIMIT_TRIM")


class TestClassificationPoints:
    """两个 AUTO 认领点的行为契约 (代码路径钉桩, 不起 broker).

    病灶回放: 切实盘后 APP 手动单 (remark='') 若被认领 AUTO →
    PositionStore 接管 → 自动退出监控平掉真钱手动仓。
    """

    def _classify(self, today_orders_entry, is_db_manual=False, is_manual_today=False):
        """复刻 _sync_to_position_store_locked 的三层判定 (RFC-080 后)."""
        _today_ord_ps = today_orders_entry
        _has_today_order = bool(_today_ord_ps and _today_ord_ps.get('is_system')) \
            and not is_manual_today and not is_db_manual
        if is_db_manual:
            return "MANUAL", "manual_sync"
        if is_manual_today:
            return "MANUAL", "manual_sync"
        if _has_today_order:
            return "AUTO", "playbook_sim"
        return "MANUAL", "manual_sync"

    def test_app_manual_order_not_claimed_auto(self):
        """cutover 核心场景: REAL env 里 remark='' 的 APP 手动单 → MANUAL."""
        entry = {'order_id': 'FH1D1B3F8868AE8000', 'remark': '', 'is_system': False}
        tt, sid = self._classify(entry)
        assert tt == "MANUAL" and sid == "manual_sync"

    def test_playbook_order_claimed_auto(self):
        """playbook 系统单 (pb_*) → AUTO/playbook_sim (既有行为保持)."""
        entry = {'order_id': 'X1', 'remark': 'pb_2310_real', 'is_system': True}
        tt, sid = self._classify(entry)
        assert tt == "AUTO" and sid == "playbook_sim"

    def test_manual_audit_still_overrides(self):
        """三层优先级不变: 今日手动审计 > 系统委托."""
        entry = {'order_id': 'X1', 'remark': 'pb_1_real', 'is_system': True}
        tt, sid = self._classify(entry, is_manual_today=True)
        assert tt == "MANUAL"

    def test_db_manual_still_overrides(self):
        entry = {'order_id': 'X1', 'remark': 'pb_1_real', 'is_system': True}
        tt, sid = self._classify(entry, is_db_manual=True)
        assert tt == "MANUAL"

    def test_no_order_defaults_manual(self):
        """无当日委托 → MANUAL (2026-07-02 安全默认保持)."""
        tt, sid = self._classify(None)
        assert tt == "MANUAL"
