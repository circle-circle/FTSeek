"""
PaperTradingEngine 单元测试 - 模拟交易引擎

测试覆盖:
- 买入时价格上浮 0.5%
- 卖出时价格下浮 0.5%
- 持仓盈亏计算准确
- 交易记录完整保存
- 支持多股票独立持仓
- 支持多次买入 (平均成本)
- 支持部分卖出

测试场景:
1. 首次买入 100 股 @ ¥100 → 成交价¥100.5
2. 再次买入 100 股 @ ¥102 → 平均成本¥101.5
3. 卖出 50 股 @ ¥105 → 成交价¥104.475, 盈利计算
4. 卖出全部持仓 → 清空持仓
5. 卖出超过持仓 → 拒绝
6. 查询持仓 → 返回准确数据
7. 多股票独立持仓 → 互不干扰
"""
import pytest
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.v4.trader.paper_trader import PaperTradingEngine, PaperPosition, PaperTrade
from app.v4.models.trading_models import TradingDatabase, get_trading_db


@pytest.fixture
def test_db_path(tmp_path):
    """创建临时数据库路径"""
    db_path = tmp_path / "test_trading.db"
    yield str(db_path)
    # 清理
    if db_path.exists():
        db_path.unlink()
    # 清理 WAL 文件
    for ext in ['-wal', '-shm']:
        wal_file = Path(str(db_path) + ext)
        if wal_file.exists():
            wal_file.unlink()


@pytest.fixture
def engine(test_db_path):
    """创建测试用的交易引擎"""
    eng = PaperTradingEngine(db_path=test_db_path, slippage=0.005)
    yield eng
    # 清理
    eng.db_session.close()


class TestPaperTradingEngine:
    """PaperTradingEngine 核心功能测试"""
    
    def test_engine_initialization(self, engine):
        """测试引擎初始化"""
        assert engine.slippage == 0.005
        assert engine.db_session is not None
    
    def test_custom_slippage(self, test_db_path):
        """测试自定义滑点"""
        eng = PaperTradingEngine(db_path=test_db_path, slippage=0.01)
        assert eng.slippage == 0.01
        eng.db_session.close()
    
    # ==================== 买入测试 ====================
    
    def test_first_buy(self, engine):
        """测试场景 1: 首次买入 100 股 @ ¥100 → 成交价¥100.5"""
        timestamp = datetime(2026, 3, 12, 10, 30, 0)
        trade = engine.buy(code="HK.00700", quantity=100, market_price=100.0, timestamp=timestamp)
        
        # 验证成交价 (上浮 0.5%)
        assert trade.price == pytest.approx(100.5, rel=1e-6)
        assert trade.action == "BUY"
        assert trade.quantity == 100
        assert trade.code == "HK.00700"
        assert trade.timestamp == timestamp
        assert trade.pnl == 0.0  # 买入时不计算盈亏
        
        # 验证持仓
        position = engine.get_position("HK.00700")
        assert position is not None
        assert position.code == "HK.00700"
        assert position.quantity == 100
        assert position.avg_cost == pytest.approx(100.5, rel=1e-6)
    
    def test_second_buy_average_cost(self, engine):
        """测试场景 2: 再次买入 100 股 @ ¥102 → 平均成本¥101.5"""
        # 第一次买入
        engine.buy(code="HK.00700", quantity=100, market_price=100.0, 
                   timestamp=datetime(2026, 3, 12, 10, 30, 0))
        
        # 第二次买入
        trade2 = engine.buy(code="HK.00700", quantity=100, market_price=102.0,
                           timestamp=datetime(2026, 3, 12, 11, 0, 0))
        
        # 验证第二次成交价
        assert trade2.price == pytest.approx(102.51, rel=1e-6)  # 102 * 1.005
        
        # 验证平均成本：(100*100.5 + 100*102.51) / 200 = 101.505
        position = engine.get_position("HK.00700")
        assert position.quantity == 200
        expected_avg_cost = (100 * 100.5 + 100 * 102.51) / 200
        assert position.avg_cost == pytest.approx(expected_avg_cost, rel=1e-6)
    
    # ==================== 卖出测试 ====================
    
    def test_partial_sell(self, engine):
        """测试场景 3: 卖出 50 股 @ ¥105 → 成交价¥104.475, 盈利计算"""
        # 先买入 100 股
        engine.buy(code="HK.00700", quantity=100, market_price=100.0,
                   timestamp=datetime(2026, 3, 12, 10, 30, 0))
        
        # 卖出 50 股
        timestamp = datetime(2026, 3, 12, 14, 0, 0)
        trade = engine.sell(code="HK.00700", quantity=50, market_price=105.0, timestamp=timestamp)
        
        # 验证成交价 (下浮 0.5%)
        assert trade.price == pytest.approx(104.475, rel=1e-6)  # 105 * 0.995
        assert trade.action == "SELL"
        assert trade.quantity == 50
        
        # 验证盈亏：(104.475 - 100.5) * 50 = 198.75
        expected_pnl = (104.475 - 100.5) * 50
        assert trade.pnl == pytest.approx(expected_pnl, rel=1e-6)
        
        # 验证持仓减少
        position = engine.get_position("HK.00700")
        assert position.quantity == 50
        # 平均成本不变 (部分卖出不影响平均成本)
        assert position.avg_cost == pytest.approx(100.5, rel=1e-6)
    
    def test_sell_all_position(self, engine):
        """测试场景 4: 卖出全部持仓 → 清空持仓"""
        # 先买入
        engine.buy(code="HK.00700", quantity=100, market_price=100.0)
        
        # 卖出全部
        trade = engine.sell(code="HK.00700", quantity=100, market_price=110.0)
        
        # 验证交易
        assert trade.price == pytest.approx(109.45, rel=1e-6)  # 110 * 0.995
        assert trade.quantity == 100
        
        # 验证持仓清空
        position = engine.get_position("HK.00700")
        assert position is None
    
    def test_sell_exceeds_position(self, engine):
        """测试场景 5: 卖出超过持仓 → 拒绝"""
        # 先买入 100 股
        engine.buy(code="HK.00700", quantity=100, market_price=100.0)
        
        # 尝试卖出 150 股 (超过持仓)
        with pytest.raises(ValueError, match="卖出数量.*超过持仓"):
            engine.sell(code="HK.00700", quantity=150, market_price=105.0)
        
        # 验证持仓未变
        position = engine.get_position("HK.00700")
        assert position.quantity == 100
    
    # ==================== 持仓查询测试 ====================
    
    def test_get_position(self, engine):
        """测试场景 6: 查询持仓 → 返回准确数据"""
        # 买入
        engine.buy(code="HK.00700", quantity=100, market_price=100.0)
        
        # 查询
        position = engine.get_position("HK.00700")
        
        assert position is not None
        assert position.code == "HK.00700"
        assert position.quantity == 100
        assert position.avg_cost == pytest.approx(100.5, rel=1e-6)
    
    def test_get_nonexistent_position(self, engine):
        """查询不存在的持仓"""
        position = engine.get_position("HK.99999")
        assert position is None
    
    def test_get_all_positions(self, engine):
        """获取所有持仓"""
        # 买入多只股票
        engine.buy(code="HK.00700", quantity=100, market_price=100.0)
        engine.buy(code="HK.09988", quantity=200, market_price=50.0)
        engine.buy(code="US.AAPL", quantity=50, market_price=150.0)
        
        positions = engine.get_all_positions()
        
        assert len(positions) == 3
        codes = {p.code for p in positions}
        assert codes == {"HK.00700", "HK.09988", "US.AAPL"}
    
    # ==================== 多股票独立持仓测试 ====================
    
    def test_multiple_stocks_independent(self, engine):
        """测试场景 7: 多股票独立持仓 → 互不干扰"""
        # 买入股票 A
        engine.buy(code="HK.00700", quantity=100, market_price=100.0)
        
        # 买入股票 B
        engine.buy(code="HK.09988", quantity=200, market_price=50.0)
        
        # 验证持仓独立
        pos_a = engine.get_position("HK.00700")
        pos_b = engine.get_position("HK.09988")
        
        assert pos_a.quantity == 100
        assert pos_a.avg_cost == pytest.approx(100.5, rel=1e-6)
        
        assert pos_b.quantity == 200
        assert pos_b.avg_cost == pytest.approx(50.25, rel=1e-6)  # 50 * 1.005
        
        # 卖出股票 A 不影响 B
        engine.sell(code="HK.00700", quantity=50, market_price=105.0)
        
        pos_a_after = engine.get_position("HK.00700")
        pos_b_after = engine.get_position("HK.09988")
        
        assert pos_a_after.quantity == 50
        assert pos_b_after.quantity == 200  # 不变
    
    # ==================== 盈亏计算测试 ====================
    
    def test_update_price_and_pnl(self, engine):
        """测试更新价格和未实现盈亏"""
        # 买入
        engine.buy(code="HK.00700", quantity=100, market_price=100.0)
        
        # 更新当前价
        engine.update_price("HK.00700", 110.0)
        
        position = engine.get_position("HK.00700")
        assert position.current_price == 110.0
        
        # 计算未实现盈亏：(110 - 100.5) * 100 = 950
        pnl, return_pct = engine.calculate_pnl(position)
        assert pnl == pytest.approx(950.0, rel=1e-6)
        assert return_pct == pytest.approx(950.0 / (100.5 * 100) * 100, rel=1e-6)
    
    def test_calculate_pnl_loss(self, engine):
        """测试亏损情况"""
        # 买入
        engine.buy(code="HK.00700", quantity=100, market_price=100.0)
        
        # 价格下跌
        engine.update_price("HK.00700", 95.0)
        
        position = engine.get_position("HK.00700")
        pnl, return_pct = engine.calculate_pnl(position)
        
        # (95 - 100.5) * 100 = -550
        assert pnl == pytest.approx(-550.0, rel=1e-6)
        assert return_pct < 0
    
    # ==================== 交易记录测试 ====================
    
    def test_trade_history(self, engine):
        """测试交易记录保存"""
        # 多次交易
        engine.buy(code="HK.00700", quantity=100, market_price=100.0,
                   timestamp=datetime(2026, 3, 12, 10, 0, 0))
        engine.buy(code="HK.00700", quantity=50, market_price=102.0,
                   timestamp=datetime(2026, 3, 12, 11, 0, 0))
        engine.sell(code="HK.00700", quantity=75, market_price=105.0,
                    timestamp=datetime(2026, 3, 12, 14, 0, 0))
        
        trades = engine.get_trade_history("HK.00700")
        
        assert len(trades) == 3
        # 验证交易顺序 (最新在前)
        assert trades[0].action == "SELL"
        assert trades[1].action == "BUY"
        assert trades[2].action == "BUY"
    
    def test_all_trades_history(self, engine):
        """获取所有交易记录"""
        engine.buy(code="HK.00700", quantity=100, market_price=100.0)
        engine.buy(code="HK.09988", quantity=200, market_price=50.0)
        
        all_trades = engine.get_all_trades()
        
        assert len(all_trades) == 2
    
    # ==================== 边界情况测试 ====================
    
    def test_zero_quantity_buy(self, engine):
        """测试买入 0 股"""
        with pytest.raises(ValueError):
            engine.buy(code="HK.00700", quantity=0, market_price=100.0)
    
    def test_negative_quantity_buy(self, engine):
        """测试负数买入"""
        with pytest.raises(ValueError):
            engine.buy(code="HK.00700", quantity=-10, market_price=100.0)
    
    def test_zero_quantity_sell(self, engine):
        """测试卖出 0 股"""
        engine.buy(code="HK.00700", quantity=100, market_price=100.0)
        with pytest.raises(ValueError):
            engine.sell(code="HK.00700", quantity=0, market_price=105.0)
    
    def test_negative_price(self, engine):
        """测试负价格"""
        with pytest.raises(ValueError):
            engine.buy(code="HK.00700", quantity=100, market_price=-10.0)
    
    def test_default_timestamp(self, engine):
        """测试默认时间戳"""
        trade = engine.buy(code="HK.00700", quantity=100, market_price=100.0)
        
        assert trade.timestamp is not None
        # 时间应该在当前时间附近 (1 秒内)
        assert abs((datetime.now() - trade.timestamp).total_seconds()) < 1
    
    # ==================== 复杂场景测试 ====================
    
    def test_complex_trading_scenario(self, engine):
        """复杂交易场景：多次买入 + 多次部分卖出"""
        # 三次买入
        engine.buy(code="HK.00700", quantity=100, market_price=100.0,
                   timestamp=datetime(2026, 3, 12, 10, 0, 0))
        engine.buy(code="HK.00700", quantity=100, market_price=102.0,
                   timestamp=datetime(2026, 3, 12, 11, 0, 0))
        engine.buy(code="HK.00700", quantity=100, market_price=98.0,
                   timestamp=datetime(2026, 3, 12, 12, 0, 0))
        
        position = engine.get_position("HK.00700")
        assert position.quantity == 300
        
        # 计算平均成本
        cost1 = 100 * 1.005  # 100.5
        cost2 = 102 * 1.005  # 102.51
        cost3 = 98 * 1.005   # 98.49
        expected_avg = (cost1 * 100 + cost2 * 100 + cost3 * 100) / 300
        assert position.avg_cost == pytest.approx(expected_avg, rel=1e-6)
        
        # 第一次部分卖出
        engine.sell(code="HK.00700", quantity=150, market_price=105.0,
                    timestamp=datetime(2026, 3, 12, 14, 0, 0))
        
        position = engine.get_position("HK.00700")
        assert position.quantity == 150
        # 平均成本不变
        assert position.avg_cost == pytest.approx(expected_avg, rel=1e-6)
        
        # 第二次部分卖出
        engine.sell(code="HK.00700", quantity=100, market_price=108.0,
                    timestamp=datetime(2026, 3, 12, 15, 0, 0))
        
        position = engine.get_position("HK.00700")
        assert position.quantity == 50
        
        # 全部卖出
        engine.sell(code="HK.00700", quantity=50, market_price=110.0,
                    timestamp=datetime(2026, 3, 12, 16, 0, 0))
        
        position = engine.get_position("HK.00700")
        assert position is None
        
        # 验证交易记录
        trades = engine.get_trade_history("HK.00700")
        assert len(trades) == 6  # 3 次买入 + 3 次卖出


class TestSlippageCalculation:
    """滑点计算专项测试"""
    
    def test_buy_slippage_0_5_percent(self, engine):
        """买入滑点 0.5%"""
        trade = engine.buy(code="HK.00700", quantity=100, market_price=100.0)
        assert trade.price == pytest.approx(100.5, rel=1e-6)
    
    def test_sell_slippage_0_5_percent(self, engine):
        """卖出滑点 0.5%"""
        engine.buy(code="HK.00700", quantity=100, market_price=100.0)
        trade = engine.sell(code="HK.00700", quantity=100, market_price=100.0)
        assert trade.price == pytest.approx(99.5, rel=1e-6)
    
    def test_slippage_rounding(self, engine):
        """滑点四舍五入精度"""
        trade = engine.buy(code="HK.00700", quantity=100, market_price=100.123)
        expected = 100.123 * 1.005
        assert trade.price == pytest.approx(expected, rel=1e-6)
    
    def test_zero_slippage(self, test_db_path):
        """零滑点配置"""
        eng = PaperTradingEngine(db_path=test_db_path, slippage=0.0)
        trade = eng.buy(code="HK.00700", quantity=100, market_price=100.0)
        assert trade.price == 100.0
        eng.db_session.close()


class TestPositionDataClass:
    """PaperPosition 数据类测试"""
    
    def test_position_unrealized_pnl(self):
        """测试未实现盈亏计算"""
        position = PaperPosition(
            code="HK.00700",
            quantity=100,
            avg_cost=100.5,
            current_price=110.0,
            entry_time=datetime.now()
        )
        
        # 未实现盈亏：(110 - 100.5) * 100 = 950
        assert position.unrealized_pnl == pytest.approx(950.0, rel=1e-6)
        # 收益率：950 / (100.5 * 100) * 100 ≈ 9.45%
        assert position.unrealized_return == pytest.approx(9.4527, rel=1e-3)
    
    def test_position_loss(self):
        """测试亏损情况"""
        position = PaperPosition(
            code="HK.00700",
            quantity=100,
            avg_cost=100.5,
            current_price=95.0,
            entry_time=datetime.now()
        )
        
        assert position.unrealized_pnl == pytest.approx(-550.0, rel=1e-6)
        assert position.unrealized_return < 0


class TestTradeDataClass:
    """PaperTrade 数据类测试"""
    
    def test_trade_buy_pnl_zero(self):
        """买入交易盈亏为 0"""
        trade = PaperTrade(
            code="HK.00700",
            action="BUY",
            quantity=100,
            price=100.5,
            timestamp=datetime.now()
        )
        assert trade.pnl == 0.0
        assert trade.return_pct == 0.0
    
    def test_trade_sell_with_pnl(self):
        """卖出交易有盈亏"""
        trade = PaperTrade(
            code="HK.00700",
            action="SELL",
            quantity=100,
            price=105.0,
            timestamp=datetime.now(),
            pnl=450.0,
            return_pct=4.48
        )
        assert trade.pnl == 450.0
        assert trade.return_pct == 4.48


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
