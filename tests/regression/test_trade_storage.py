"""交易存储回归测试 - 验证数据完整性"""
import pytest
import sys
import tempfile
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestTradeMode:
    """测试 trade_mode 字段"""

    def test_trade_mode_auto_fill(self):
        """trade_mode 应根据 AUTO_TRADE_ACCOUNT 环境变量自动填充 (2026-07-03 收敛为模块级函数)"""
        import os
        from unittest.mock import patch
        from app.storage.trade_store import _get_trade_mode

        assert _get_trade_mode() in ('simulate', 'live')
        with patch.dict(os.environ, {"AUTO_TRADE_ACCOUNT": "sim"}):
            assert _get_trade_mode() == 'simulate'
        with patch.dict(os.environ, {"AUTO_TRADE_ACCOUNT": "real"}):
            assert _get_trade_mode() == 'live'


class TestExitReasonRegistry:
    """测试exit_reason注册表"""

    def test_all_standard_reasons_valid(self):
        """所有标准原因应通过验证"""
        from app.risk.exit_reason_registry import validate_exit_reason, EXIT_REASON_REGISTRY
        for reason in EXIT_REASON_REGISTRY:
            assert validate_exit_reason(reason), f"{reason} should be valid"

    def test_suffix_format_valid(self):
        """带@后缀的格式应通过验证"""
        from app.risk.exit_reason_registry import validate_exit_reason
        assert validate_exit_reason("DAY_TRADE_TP@trail_suppressed")

    def test_unknown_reason_invalid(self):
        """未知原因不通过验证"""
        from app.risk.exit_reason_registry import validate_exit_reason
        assert not validate_exit_reason("TOTALLY_UNKNOWN_REASON")

    def test_normalize_legacy_reasons(self):
        """历史原因应能被映射"""
        from app.risk.exit_reason_registry import normalize_exit_reason
        assert normalize_exit_reason('tp1') == 'TP1_PRICE_ACTION'
        assert normalize_exit_reason('stop_loss') == 'SL_ATR_STOP'
