"""
交易决策引擎数据模型单元测试

测试覆盖:
- trading_modes 表：交易模式管理
- paper_positions 表：模拟持仓管理
- paper_trades 表：交易记录管理
- performance_stats 表：绩效统计管理
- graduation_history 表：晋级历史管理
- 数据库迁移脚本幂等性
"""
import pytest
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import sqlite3

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.v4.models.trading_models import (
    TradingMode,
    TradingModeModel,
    PaperPosition,
    PaperTrade,
    PerformanceStats,
    GraduationHistory,
    TradingDatabase,
    init_trading_db,
    get_trading_db
)


@pytest.fixture
def temp_db():
    """创建临时数据库用于测试"""
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    
    # 初始化数据库
    db = TradingDatabase(db_path)
    
    yield db, db_path
    
    # 清理临时文件
    try:
        os.unlink(db_path)
    except:
        pass


class TestTradingModeModel:
    """交易模式模型测试"""
    
    def test_create_trading_mode(self, temp_db):
        """测试创建交易模式"""
        db, _ = temp_db
        
        mode = db.create_trading_mode("0700.HK", TradingMode.PAPER)
        
        assert mode is not None
        assert mode.code == "0700.HK"
        assert mode.mode == TradingMode.PAPER
        assert mode.created_at is not None
        assert mode.updated_at is not None
    
    def test_update_trading_mode(self, temp_db):
        """测试更新交易模式"""
        db, _ = temp_db
        
        # 创建
        db.create_trading_mode("0700.HK", TradingMode.PAPER)
        
        # 更新
        updated = db.update_trading_mode("0700.HK", TradingMode.HYBRID)
        
        assert updated.mode == TradingMode.HYBRID
        assert updated.updated_at is not None
    
    def test_get_trading_mode(self, temp_db):
        """测试获取交易模式"""
        db, _ = temp_db
        
        db.create_trading_mode("0700.HK", TradingMode.PAPER)
        
        mode = db.get_trading_mode("0700.HK")
        
        assert mode is not None
        assert mode.code == "0700.HK"
        assert mode.mode == TradingMode.PAPER
    
    def test_get_nonexistent_mode(self, temp_db):
        """测试获取不存在的交易模式"""
        db, _ = temp_db
        
        mode = db.get_trading_mode("NONEXISTENT")
        
        assert mode is None
    
    def test_list_all_modes(self, temp_db):
        """测试列出所有交易模式"""
        db, _ = temp_db
        
        db.create_trading_mode("0700.HK", TradingMode.PAPER)
        db.create_trading_mode("MU", TradingMode.HYBRID)
        db.create_trading_mode("AAPL", TradingMode.LIVE)
        
        modes = db.list_all_modes()
        
        assert len(modes) == 3
        codes = [m.code for m in modes]
        assert "0700.HK" in codes
        assert "MU" in codes
        assert "AAPL" in codes
    
    def test_delete_trading_mode(self, temp_db):
        """测试删除交易模式"""
        db, _ = temp_db
        
        db.create_trading_mode("0700.HK", TradingMode.PAPER)
        deleted = db.delete_trading_mode("0700.HK")
        
        assert deleted is True
        
        # 验证已删除
        mode = db.get_trading_mode("0700.HK")
        assert mode is None


class TestPaperPosition:
    """模拟持仓模型测试"""
    
    def test_create_position(self, temp_db):
        """测试创建持仓"""
        db, _ = temp_db
        
        position = db.create_paper_position(
            code="0700.HK",
            quantity=100,
            avg_cost=380.5,
            entry_time=datetime.now()
        )
        
        assert position is not None
        assert position.code == "0700.HK"
        assert position.quantity == 100
        assert position.avg_cost == 380.5
    
    def test_update_position(self, temp_db):
        """测试更新持仓"""
        db, _ = temp_db
        
        position = db.create_paper_position(
            code="0700.HK",
            quantity=100,
            avg_cost=380.5,
            entry_time=datetime.now()
        )
        
        # 加仓
        updated = db.update_paper_position(
            position.id,
            quantity=150,
            avg_cost=382.0
        )
        
        assert updated.quantity == 150
        assert updated.avg_cost == 382.0
    
    def test_get_position_by_code(self, temp_db):
        """测试按代码获取持仓"""
        db, _ = temp_db
        
        db.create_paper_position(
            code="0700.HK",
            quantity=100,
            avg_cost=380.5,
            entry_time=datetime.now()
        )
        
        position = db.get_position_by_code("0700.HK")
        
        assert position is not None
        assert position.code == "0700.HK"
    
    def test_get_all_positions(self, temp_db):
        """测试获取所有持仓"""
        db, _ = temp_db
        
        db.create_paper_position("0700.HK", 100, 380.5, datetime.now())
        db.create_paper_position("MU", 50, 85.3, datetime.now())
        
        positions = db.get_all_positions()
        
        assert len(positions) == 2
    
    def test_delete_position(self, temp_db):
        """测试删除持仓"""
        db, _ = temp_db
        
        position = db.create_paper_position(
            code="0700.HK",
            quantity=100,
            avg_cost=380.5,
            entry_time=datetime.now()
        )
        
        deleted = db.delete_paper_position(position.id)
        assert deleted is True
        
        # 验证已删除
        pos = db.get_position_by_code("0700.HK")
        assert pos is None


class TestPaperTrade:
    """交易记录模型测试"""
    
    def test_create_buy_trade(self, temp_db):
        """测试创建买入交易记录"""
        db, _ = temp_db
        
        trade = db.create_paper_trade(
            code="0700.HK",
            action="BUY",
            quantity=100,
            price=380.5,
            timestamp=datetime.now()
        )
        
        assert trade is not None
        assert trade.code == "0700.HK"
        assert trade.action == "BUY"
        assert trade.quantity == 100
        assert trade.price == 380.5
    
    def test_create_sell_trade_with_pnl(self, temp_db):
        """测试创建卖出交易记录（含盈亏）"""
        db, _ = temp_db
        
        trade = db.create_paper_trade(
            code="0700.HK",
            action="SELL",
            quantity=100,
            price=390.0,
            timestamp=datetime.now(),
            pnl=950.0
        )
        
        assert trade.pnl == 950.0
    
    def test_get_trades_by_code(self, temp_db):
        """测试按代码获取交易记录"""
        db, _ = temp_db
        
        db.create_paper_trade("0700.HK", "BUY", 100, 380.5, datetime.now())
        db.create_paper_trade("0700.HK", "SELL", 100, 390.0, datetime.now(), pnl=950.0)
        
        trades = db.get_trades_by_code("0700.HK")
        
        assert len(trades) == 2
    
    def test_get_trades_by_action(self, temp_db):
        """测试按操作类型获取交易记录"""
        db, _ = temp_db
        
        db.create_paper_trade("0700.HK", "BUY", 100, 380.5, datetime.now())
        db.create_paper_trade("MU", "BUY", 50, 85.3, datetime.now())
        db.create_paper_trade("0700.HK", "SELL", 100, 390.0, datetime.now(), pnl=950.0)
        
        buy_trades = db.get_trades_by_action("BUY")
        sell_trades = db.get_trades_by_action("SELL")
        
        assert len(buy_trades) == 2
        assert len(sell_trades) == 1
    
    def test_get_recent_trades(self, temp_db):
        """测试获取最近交易记录"""
        db, _ = temp_db
        
        for i in range(10):
            db.create_paper_trade("0700.HK", "BUY", 100, 380.5, datetime.now())
        
        recent = db.get_recent_trades(limit=5)
        
        assert len(recent) == 5


class TestPerformanceStats:
    """绩效统计模型测试"""
    
    def test_create_performance_stats(self, temp_db):
        """测试创建绩效统计"""
        db, _ = temp_db
        
        stats = db.create_performance_stats(
            code="0700.HK",
            date=datetime.now().date(),
            daily_return=0.032,
            cumulative_return=0.058
        )
        
        assert stats is not None
        assert stats.code == "0700.HK"
        assert stats.daily_return == 0.032
        assert stats.cumulative_return == 0.058
    
    def test_get_stats_by_code_and_date(self, temp_db):
        """测试按代码和日期获取绩效统计"""
        db, _ = temp_db
        
        today = datetime.now().date()
        db.create_performance_stats("0700.HK", today, 0.032, 0.058)
        
        stats = db.get_stats_by_code_and_date("0700.HK", today)
        
        assert stats is not None
        assert stats.daily_return == 0.032
    
    def test_get_stats_history(self, temp_db):
        """测试获取绩效历史"""
        db, _ = temp_db
        
        today = datetime.now().date()
        for i in range(5):
            date = today - timedelta(days=i)
            db.create_performance_stats("0700.HK", date, 0.01 * (i+1), 0.05)
        
        history = db.get_stats_history("0700.HK", days=7)
        
        assert len(history) == 5
    
    def test_update_cumulative_return(self, temp_db):
        """测试更新累计收益"""
        db, _ = temp_db
        
        today = datetime.now().date()
        stats = db.create_performance_stats("0700.HK", today, 0.032, 0.058)
        
        updated = db.update_performance_stats(stats.id, cumulative_return=0.065)
        
        assert updated.cumulative_return == 0.065


class TestGraduationHistory:
    """晋级历史模型测试"""
    
    def test_create_graduation_record(self, temp_db):
        """测试创建晋级记录"""
        db, _ = temp_db
        
        record = db.create_graduation_history(
            code="0700.HK",
            from_mode=TradingMode.PAPER,
            to_mode=TradingMode.HYBRID,
            reason="连续 3 天收益>5%"
        )
        
        assert record is not None
        assert record.code == "0700.HK"
        assert record.from_mode == TradingMode.PAPER
        assert record.to_mode == TradingMode.HYBRID
        assert "连续 3 天收益>5%" in record.reason
    
    def test_get_graduation_history_by_code(self, temp_db):
        """测试按代码获取晋级历史"""
        db, _ = temp_db
        
        db.create_graduation_history("0700.HK", TradingMode.PAPER, TradingMode.HYBRID, "原因 1")
        db.create_graduation_history("0700.HK", TradingMode.HYBRID, TradingMode.LIVE, "原因 2")
        
        history = db.get_graduation_history_by_code("0700.HK")
        
        assert len(history) == 2
    
    def test_get_all_graduation_records(self, temp_db):
        """测试获取所有晋级记录"""
        db, _ = temp_db
        
        db.create_graduation_history("0700.HK", TradingMode.PAPER, TradingMode.HYBRID, "原因 1")
        db.create_graduation_history("MU", TradingMode.PAPER, TradingMode.LIVE, "原因 2")
        
        records = db.get_all_graduation_records()
        
        assert len(records) == 2


class TestDatabaseMigration:
    """数据库迁移脚本测试"""
    
    def test_migration_idempotency(self, temp_db):
        """测试迁移脚本幂等性（可重复执行）"""
        db, db_path = temp_db
        
        # 第一次迁移
        from app.v4.storage.db_migrate import run_migration
        result1 = run_migration(db_path)
        assert result1 is True
        
        # 第二次迁移（应该不报错）
        result2 = run_migration(db_path)
        assert result2 is True
    
    def test_all_tables_created(self, temp_db):
        """测试所有表都创建成功"""
        db, db_path = temp_db
        
        # 运行迁移
        from app.v4.storage.db_migrate import run_migration
        run_migration(db_path)
        
        # 验证表存在
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        
        expected_tables = [
            'trading_modes',
            'paper_positions',
            'paper_trades',
            'performance_stats',
            'graduation_history'
        ]
        
        for table in expected_tables:
            assert table in tables, f"表 {table} 未创建"
    
    def test_wal_mode_enabled(self, temp_db):
        """测试 WAL 模式已启用"""
        db, db_path = temp_db
        
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        conn.close()
        
        assert mode == "wal", "WAL 模式未启用"


class TestTradingModeEnum:
    """交易模式枚举测试"""
    
    def test_trading_mode_values(self):
        """测试交易模式枚举值"""
        assert TradingMode.PAPER == "PAPER"
        assert TradingMode.HYBRID == "HYBRID"
        assert TradingMode.LIVE == "LIVE"
    
    def test_trading_mode_from_string(self):
        """测试字符串转换为枚举"""
        assert TradingMode.from_string("PAPER") == TradingMode.PAPER
        assert TradingMode.from_string("HYBRID") == TradingMode.HYBRID
        assert TradingMode.from_string("LIVE") == TradingMode.LIVE
        assert TradingMode.from_string("invalid") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
