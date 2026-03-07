"""
WatchlistManager 单元测试

测试观察列表管理系统的核心功能:
1. 观察列表生成
2. 优先级排序
3. 动态更新
4. 失效检测
5. 清理失效
6. 添加删除
7. Top 推荐过滤
8. 最大条目数限制
"""

import pytest
import time
from unittest.mock import Mock, patch, MagicMock
from dataclasses import dataclass
from typing import List, Dict

# 导入被测试的类
from app.v4.strategy.watchlist_manager import (
    WatchlistManager,
    WatchlistEntry,
    WatchlistReport,
)


class TestWatchlistGeneration:
    """测试观察列表生成功能"""

    def test_watchlist_generation(self):
        """测试盘前观察列表生成"""
        # Arrange
        manager = WatchlistManager()
        candidate_symbols = ["US.AAPL", "US.NVDA", "US.TSLA", "US.MSFT", "US.GOOGL"]

        # Mock PreMarketAnalyzer 和 SignalFusion
        with patch.object(manager, 'premarket') as mock_premarket, \
             patch.object(manager, 'fusion') as mock_fusion:
            
            # 配置 mock 返回值
            mock_report = Mock()
            mock_report.volume_ratio = 3.5
            mock_report.abnormal_score = 70
            mock_premarket.analyze_volume.return_value = mock_report
            
            mock_signal = Mock()
            mock_signal.final_score = 0.75
            mock_signal.premarket_score = 0.8
            mock_signal.momentum_score = 0.7
            mock_signal.pattern_score = 0.9
            mock_signal.catalyst_score = 0.6
            mock_signal.entry_price = 150.0
            mock_signal.stop_loss = 142.5
            mock_signal.target_price = 165.0
            mock_fusion.generate_signal.return_value = mock_signal

            # Act
            report = manager.generate_premarket_watchlist(candidate_symbols)

            # Assert
            assert isinstance(report, WatchlistReport)
            assert report.total_count == len(candidate_symbols)
            assert len(report.entries) > 0
            assert report.market_condition in ['bullish', 'neutral', 'bearish']
            assert isinstance(report.top_picks, list)
            
            # 验证所有条目都有正确的字段
            for entry in report.entries:
                assert isinstance(entry, WatchlistEntry)
                assert entry.symbol in candidate_symbols
                assert 0 <= entry.priority_score <= 100
                assert entry.added_at > 0


class TestPriorityRanking:
    """测试优先级排序功能"""

    def test_priority_ranking(self):
        """测试优先级评分和排序"""
        # Arrange
        manager = WatchlistManager()
        
        # 手动添加测试数据
        entries = [
            WatchlistEntry(
                symbol="US.AAPL",
                name="Apple Inc.",
                priority_score=85.0,
                premarket_score=0.8,
                momentum_score=0.7,
                pattern_score=0.9,
                catalyst_score=0.6,
                entry_price=150.0,
                stop_loss=142.5,
                target_price=165.0,
                added_at=time.time(),
                is_stale=False
            ),
            WatchlistEntry(
                symbol="US.NVDA",
                name="NVIDIA Corp.",
                priority_score=92.0,
                premarket_score=0.9,
                momentum_score=0.95,
                pattern_score=0.85,
                catalyst_score=0.8,
                entry_price=450.0,
                stop_loss=427.5,
                target_price=495.0,
                added_at=time.time(),
                is_stale=False
            ),
            WatchlistEntry(
                symbol="US.TSLA",
                name="Tesla Inc.",
                priority_score=65.0,
                premarket_score=0.5,
                momentum_score=0.6,
                pattern_score=0.7,
                catalyst_score=0.5,
                entry_price=200.0,
                stop_loss=190.0,
                target_price=220.0,
                added_at=time.time(),
                is_stale=False
            ),
        ]
        
        manager.watchlist = entries

        # Act
        # 验证优先级评分公式
        # 注意：测试中手动设置的 priority_score 可能不匹配公式计算值
        # 这里验证公式计算是否正确，而不是验证手动设置的值
        for entry in entries:
            calculated_score = (
                entry.premarket_score * 0.25 +
                entry.momentum_score * 0.30 +
                entry.pattern_score * 0.30 +
                entry.catalyst_score * 0.15
            ) * 100
            # 使用 manager 的方法重新计算验证
            recalculated = manager._calculate_priority_score(
                entry.premarket_score,
                entry.momentum_score,
                entry.pattern_score,
                entry.catalyst_score
            )
            assert abs(recalculated - calculated_score) < 0.1

        # 获取 top picks 验证排序
        top_picks = manager.get_top_picks(limit=2)

        # Assert
        assert len(top_picks) == 2
        assert top_picks[0].symbol == "US.NVDA"  # 最高分
        assert top_picks[1].symbol == "US.AAPL"  # 第二高分
        assert top_picks[0].priority_score > top_picks[1].priority_score


class TestDynamicUpdate:
    """测试动态更新功能"""

    def test_dynamic_update(self):
        """测试盘中动态更新观察列表"""
        # Arrange
        manager = WatchlistManager()
        
        # 添加初始条目
        initial_entry = WatchlistEntry(
            symbol="US.AAPL",
            name="Apple Inc.",
            priority_score=80.0,
            premarket_score=0.7,
            momentum_score=0.8,
            pattern_score=0.75,
            catalyst_score=0.6,
            entry_price=150.0,
            stop_loss=142.5,
            target_price=165.0,
            added_at=time.time() - 3600,  # 1 小时前
            is_stale=False
        )
        manager.watchlist = [initial_entry]

        # Mock market data
        market_data = {
            'US.AAPL': {
                'price': 151.0,
                'volume': 1000000,
                'price_momentum': 80.0,
                'volume_momentum': 85.0,
                'catalyst_strength': 70.0,
            }
        }

        # Mock fusion.generate_signal
        mock_signal = Mock()
        mock_signal.final_score = 0.80
        mock_signal.premarket_score = 0.75
        mock_signal.momentum_score = 0.85
        mock_signal.pattern_score = 0.80
        mock_signal.catalyst_score = 0.70
        mock_signal.entry_price = 151.0
        mock_signal.stop_loss = 143.45
        mock_signal.target_price = 166.1
        
        with patch.object(manager.fusion, 'generate_signal', return_value=mock_signal), \
             patch.object(manager, '_is_market_closed', return_value=False):
            # Act
            report = manager.update_watchlist(market_data)

            # Assert
            assert isinstance(report, WatchlistReport)
            assert len(report.entries) >= 1
            # 验证条目被更新
            updated_entry = next((e for e in report.entries if e.symbol == "US.AAPL"), None)
            assert updated_entry is not None
            assert updated_entry.entry_price == 151.0  # 价格已更新


class TestStaleDetection:
    """测试失效检测功能"""

    def test_stale_detection(self):
        """测试股票失效条件检测"""
        # Arrange
        manager = WatchlistManager()
        
        # 创建测试条目
        entry = WatchlistEntry(
            symbol="US.AAPL",
            name="Apple Inc.",
            priority_score=80.0,
            premarket_score=0.7,
            momentum_score=0.8,
            pattern_score=0.75,
            catalyst_score=0.6,
            entry_price=150.0,
            stop_loss=142.5,
            target_price=165.0,
            added_at=time.time(),
            is_stale=False
        )
        manager.watchlist = [entry]

        # Act & Assert - 测试价格偏离超过 5%
        # 价格从 150 涨到 160 (偏离 6.67%)
        assert manager._is_price_stale(entry, current_price=160.0) is True
        
        # 价格从 150 跌到 140 (偏离 6.67%)
        assert manager._is_price_stale(entry, current_price=140.0) is True
        
        # 价格在 5% 范围内
        assert manager._is_price_stale(entry, current_price=155.0) is False
        assert manager._is_price_stale(entry, current_price=145.0) is False


class TestCleanupStale:
    """测试清理失效股票功能"""

    def test_cleanup_stale(self):
        """测试清理所有失效股票"""
        # Arrange
        manager = WatchlistManager()
        
        # 创建混合条目 (部分失效)
        manager.watchlist = [
            WatchlistEntry(
                symbol="US.AAPL",
                name="Apple Inc.",
                priority_score=80.0,
                premarket_score=0.7,
                momentum_score=0.8,
                pattern_score=0.75,
                catalyst_score=0.6,
                entry_price=150.0,
                stop_loss=142.5,
                target_price=165.0,
                added_at=time.time(),
                is_stale=False
            ),
            WatchlistEntry(
                symbol="US.NVDA",
                name="NVIDIA Corp.",
                priority_score=70.0,
                premarket_score=0.6,
                momentum_score=0.7,
                pattern_score=0.65,
                catalyst_score=0.5,
                entry_price=450.0,
                stop_loss=427.5,
                target_price=495.0,
                added_at=time.time(),
                is_stale=True  # 已失效
            ),
            WatchlistEntry(
                symbol="US.TSLA",
                name="Tesla Inc.",
                priority_score=60.0,
                premarket_score=0.5,
                momentum_score=0.6,
                pattern_score=0.55,
                catalyst_score=0.4,
                entry_price=200.0,
                stop_loss=190.0,
                target_price=220.0,
                added_at=time.time(),
                is_stale=True  # 已失效
            ),
        ]

        # Act
        removed_count = manager.cleanup_stale()

        # Assert
        assert removed_count == 2  # 移除了 2 个失效股票
        assert len(manager.watchlist) == 1  # 剩下 1 个有效股票
        assert manager.watchlist[0].symbol == "US.AAPL"
        assert manager.watchlist[0].is_stale is False


class TestAddRemoveSymbol:
    """测试添加和删除股票功能"""

    def test_add_remove_symbol(self):
        """测试添加和删除股票"""
        # Arrange
        manager = WatchlistManager()
        
        scores = {
            'premarket': 0.8,
            'momentum': 0.7,
            'pattern': 0.9,
            'catalyst': 0.6,
        }

        # Act - 添加股票
        add_result = manager.add_symbol("US.AAPL", scores)

        # Assert - 添加成功
        assert add_result is True
        assert len(manager.watchlist) == 1
        assert manager.watchlist[0].symbol == "US.AAPL"
        
        # 验证优先级评分计算正确
        expected_priority = (0.8 * 0.25 + 0.7 * 0.30 + 0.9 * 0.30 + 0.6 * 0.15) * 100
        assert abs(manager.watchlist[0].priority_score - expected_priority) < 0.1

        # Act - 删除股票
        remove_result = manager.remove_symbol("US.AAPL")

        # Assert - 删除成功
        assert remove_result is True
        assert len(manager.watchlist) == 0

        # Act - 删除不存在的股票
        remove_nonexistent = manager.remove_symbol("US.NONEXISTENT")

        # Assert
        assert remove_nonexistent is False


class TestTopPicksFiltering:
    """测试 Top 推荐过滤功能"""

    def test_top_picks_filtering(self):
        """测试获取 Top N 推荐"""
        # Arrange
        manager = WatchlistManager()
        
        # 创建不同优先级的条目
        manager.watchlist = [
            WatchlistEntry(
                symbol="US.AAPL", name="Apple",
                priority_score=85.0, premarket_score=0.8, momentum_score=0.7,
                pattern_score=0.9, catalyst_score=0.6,
                entry_price=150.0, stop_loss=142.5, target_price=165.0,
                added_at=time.time(), is_stale=False
            ),
            WatchlistEntry(
                symbol="US.NVDA", name="NVIDIA",
                priority_score=92.0, premarket_score=0.9, momentum_score=0.95,
                pattern_score=0.85, catalyst_score=0.8,
                entry_price=450.0, stop_loss=427.5, target_price=495.0,
                added_at=time.time(), is_stale=False
            ),
            WatchlistEntry(
                symbol="US.TSLA", name="Tesla",
                priority_score=65.0, premarket_score=0.5, momentum_score=0.6,
                pattern_score=0.7, catalyst_score=0.5,
                entry_price=200.0, stop_loss=190.0, target_price=220.0,
                added_at=time.time(), is_stale=False
            ),
            WatchlistEntry(
                symbol="US.MSFT", name="Microsoft",
                priority_score=78.0, premarket_score=0.7, momentum_score=0.75,
                pattern_score=0.8, catalyst_score=0.65,
                entry_price=380.0, stop_loss=361.0, target_price=418.0,
                added_at=time.time(), is_stale=False
            ),
            WatchlistEntry(
                symbol="US.GOOGL", name="Google",
                priority_score=88.0, premarket_score=0.85, momentum_score=0.8,
                pattern_score=0.9, catalyst_score=0.75,
                entry_price=140.0, stop_loss=133.0, target_price=154.0,
                added_at=time.time(), is_stale=False
            ),
        ]

        # Act - 获取 Top 3
        top_3 = manager.get_top_picks(limit=3)

        # Assert
        assert len(top_3) == 3
        assert top_3[0].symbol == "US.NVDA"  # 92 分
        assert top_3[1].symbol == "US.GOOGL"  # 88 分
        assert top_3[2].symbol == "US.AAPL"  # 85 分
        
        # 验证按优先级降序排列
        for i in range(len(top_3) - 1):
            assert top_3[i].priority_score >= top_3[i + 1].priority_score

        # Act - 获取 Top 5 (默认)
        top_5 = manager.get_top_picks()

        # Assert
        assert len(top_5) == 5
        # 验证包含所有股票
        symbols = [e.symbol for e in top_5]
        assert "US.NVDA" in symbols
        assert "US.GOOGL" in symbols
        assert "US.AAPL" in symbols
        assert "US.MSFT" in symbols
        assert "US.TSLA" in symbols


class TestMaxEntriesLimit:
    """测试最大条目数限制功能"""

    def test_max_entries_limit(self):
        """测试观察列表最大条目数限制"""
        # Arrange
        manager = WatchlistManager()
        manager.max_entries = 5  # 设置最大 5 个条目
        
        scores = {
            'premarket': 0.7,
            'momentum': 0.75,
            'pattern': 0.8,
            'catalyst': 0.65,
        }

        # Act - 尝试添加超过限制的条目
        for i in range(10):
            symbol = f"US.STOCK{i}"
            manager.add_symbol(symbol, scores)

        # Assert
        assert len(manager.watchlist) == manager.max_entries
        assert len(manager.watchlist) == 5
        
        # 验证保留的是优先级最高的 (当优先级相同时，保留先添加的)
        symbols = [e.symbol for e in manager.watchlist]
        # 因为所有股票优先级相同，排序稳定，保留最先添加的 5 个
        assert "US.STOCK0" in symbols
        assert "US.STOCK4" in symbols
        assert "US.STOCK5" not in symbols  # 后添加的被移除
        assert "US.STOCK9" not in symbols  # 最后添加的被移除


class TestPriorityScoreCalculation:
    """测试优先级评分计算公式"""

    def test_priority_score_formula(self):
        """测试优先级评分公式正确性"""
        # Arrange
        manager = WatchlistManager()
        
        # 测试用例：(premarket, momentum, pattern, catalyst) -> expected_priority
        # 计算公式：(premarket*0.25 + momentum*0.30 + pattern*0.30 + catalyst*0.15) * 100
        test_cases = [
            (0.8, 0.7, 0.9, 0.6, 77.0),  # (0.8*0.25 + 0.7*0.3 + 0.9*0.3 + 0.6*0.15) * 100 = 77.0
            (1.0, 1.0, 1.0, 1.0, 100.0),  # 满分
            (0.0, 0.0, 0.0, 0.0, 0.0),    # 零分
            (0.5, 0.5, 0.5, 0.5, 50.0),   # 中等
        ]

        # Act & Assert
        for premarket, momentum, pattern, catalyst, expected in test_cases:
            priority = manager._calculate_priority_score(
                premarket, momentum, pattern, catalyst
            )
            assert abs(priority - expected) < 0.1, \
                f"Expected {expected}, got {priority} for scores ({premarket}, {momentum}, {pattern}, {catalyst})"


class TestWatchlistReportGeneration:
    """测试观察列表报告生成"""

    def test_report_structure(self):
        """测试报告结构完整性"""
        # Arrange
        manager = WatchlistManager()
        candidate_symbols = ["US.AAPL", "US.NVDA", "US.TSLA"]

        with patch.object(manager, 'premarket') as mock_premarket, \
             patch.object(manager, 'fusion') as mock_fusion:
            
            mock_report = Mock()
            mock_report.volume_ratio = 3.0
            mock_report.abnormal_score = 60
            mock_premarket.analyze_volume.return_value = mock_report
            
            mock_signal = Mock()
            mock_signal.final_score = 0.70
            mock_signal.premarket_score = 0.7
            mock_signal.momentum_score = 0.75
            mock_signal.pattern_score = 0.8
            mock_signal.catalyst_score = 0.65
            mock_signal.entry_price = 150.0
            mock_signal.stop_loss = 142.5
            mock_signal.target_price = 165.0
            mock_fusion.generate_signal.return_value = mock_signal

            # Act
            report = manager.generate_premarket_watchlist(candidate_symbols)

            # Assert - 验证报告所有字段
            assert hasattr(report, 'entries')
            assert hasattr(report, 'total_count')
            assert hasattr(report, 'scan_time')
            assert hasattr(report, 'market_condition')
            assert hasattr(report, 'top_picks')
            
            assert isinstance(report.entries, list)
            assert isinstance(report.total_count, int)
            assert isinstance(report.scan_time, float)
            assert isinstance(report.market_condition, str)
            assert isinstance(report.top_picks, list)
            
            assert report.total_count == len(candidate_symbols)
            assert report.scan_time > 0
            assert report.market_condition in ['bullish', 'neutral', 'bearish']


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
