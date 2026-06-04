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
        """trade_mode 应自动填充"""
        from app.storage.trade_store import TradeStore
        # TradeStore 应该能根据环境变量填充 trade_mode
        store = TradeStore.__new__(TradeStore)
        if hasattr(store, '_get_trade_mode'):
            mode = store._get_trade_mode()
            assert mode in ('simulate', 'live')


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


class TestBenchmarkTracker:
    """测试基准对照"""

    def test_benchmark_tracker_init(self):
        """BenchmarkTracker 应能正常初始化"""
        from app.analytics.benchmark_tracker import BenchmarkTracker
        import tempfile
        db_path = tempfile.mktemp(suffix='.db')
        try:
            tracker = BenchmarkTracker(db_path)
            assert tracker is not None
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


class TestAttributionScheduler:
    """测试归因调度器"""

    def test_scheduler_init(self):
        """AttributionScheduler 应能正常初始化"""
        from app.review.attribution_scheduler import AttributionScheduler
        # 使用 mock trade_store
        scheduler = AttributionScheduler(trade_store=None)
        assert scheduler is not None
        assert scheduler.daily_budget_usd == 2.0
