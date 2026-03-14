"""
持仓管理器 - 加仓逻辑单元测试
测试金字塔加仓功能
"""

import pytest
import os
import tempfile
import sqlite3
from datetime import datetime, timedelta

from app.trader.position_manager import PositionManager, Position


class TestPositionManagerAddPosition:
    """PositionManager 加仓功能测试类"""
    
    @pytest.fixture
    def temp_db(self):
        """创建临时数据库用于测试"""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        
        # 创建 paper_trades 表
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                timestamp DATETIME NOT NULL,
                pnl REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                order_id TEXT,
                position_id INTEGER,
                commission REAL DEFAULT 0,
                trade_type TEXT DEFAULT 'OPEN',
                trade_reason TEXT,
                is_stop_loss INTEGER DEFAULT 0,
                is_take_profit INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()
        
        yield path
        if os.path.exists(path):
            os.remove(path)
    
    @pytest.fixture
    def position_manager(self, temp_db):
        """创建 PositionManager 实例"""
        return PositionManager(db_path=temp_db)
    
    @pytest.fixture
    def position_manager_with_config(self, temp_db, temp_config):
        """创建带配置文件的 PositionManager 实例"""
        return PositionManager(db_path=temp_db, config_path=temp_config)
    
    @pytest.fixture
    def temp_config(self):
        """创建临时配置文件"""
        import yaml
        fd, path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        
        config = {
            'position': {
                'initial_ratio': 0.50,
                'max_position_ratio': 0.20,
                'total_max_ratio': 0.80
            },
            'pyramid': {
                'enabled': True,
                'level1': {
                    'price_increase': 0.05,
                    'ratio': 0.30,
                    'min_confidence': 0.70
                },
                'level2': {
                    'price_increase': 0.10,
                    'ratio': 0.20,
                    'min_confidence': 0.70
                }
            },
            'cooldown': {
                'hours': 4,
                'max_trades_per_day': 3,
                'min_confidence': 0.60,
                'add_position_confidence': 0.70
            }
        }
        
        with open(path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f)
        
        yield path
        if os.path.exists(path):
            os.remove(path)
    
    def _setup_position_and_trades(self, pm: PositionManager, code: str, 
                                   initial_qty: int = 1000, entry_price: float = 10.0,
                                   hours_ago: float = 5):
        """辅助方法：创建持仓和交易记录"""
        # 创建初始持仓
        pm.create_position(
            code=code,
            action='BUY',
            quantity=initial_qty,
            entry_price=entry_price,
            entry_time=(datetime.now() - timedelta(hours=hours_ago)).strftime('%Y-%m-%d %H:%M:%S')
        )
        
        # 创建初始交易记录
        conn = pm._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO paper_trades (code, action, quantity, price, timestamp, trade_type)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (code, 'BUY', initial_qty, entry_price, 
                  (datetime.now() - timedelta(hours=hours_ago)).strftime('%Y-%m-%d %H:%M:%S'), 'OPEN'))
            conn.commit()
        finally:
            conn.close()
    
    def _add_trade_record(self, pm: PositionManager, code: str, quantity: int, 
                         price: float, trade_type: str = 'ADD', 
                         hours_ago: float = 0):
        """辅助方法：添加交易记录"""
        conn = pm._get_connection()
        try:
            cursor = conn.cursor()
            trade_time = (datetime.now() - timedelta(hours=hours_ago)).isoformat()
            cursor.execute('''
                INSERT INTO paper_trades (code, action, quantity, price, timestamp, trade_type)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (code, 'BUY', quantity, price, trade_time, trade_type))
            conn.commit()
        finally:
            conn.close()
    
    # ==================== can_add_position 测试 ====================
    
    def test_can_add_position_no_position(self, position_manager):
        """测试没有持仓时不能加仓"""
        result = position_manager.can_add_position('000001', 0.80)
        assert result is False
    
    def test_can_add_position_with_position_high_confidence(self, position_manager):
        """测试有持仓且置信度高时可以加仓"""
        self._setup_position_and_trades(position_manager, '000001')
        
        # 等待冷却时间过期 (5 小时前交易，冷却 4 小时)
        result = position_manager.can_add_position('000001', 0.80)
        assert result is True
    
    def test_can_add_position_low_confidence(self, position_manager):
        """测试置信度低于阈值时不能加仓"""
        self._setup_position_and_trades(position_manager, '000001')
        
        # 置信度 0.60 < 0.70 (默认加仓最小置信度)
        result = position_manager.can_add_position('000001', 0.60)
        assert result is False
    
    def test_can_add_position_in_cooling_period(self, position_manager):
        """测试在冷却期内不能加仓"""
        self._setup_position_and_trades(position_manager, '000001')
        
        # 添加 1 小时前的交易记录 (冷却期 4 小时)
        self._add_trade_record(position_manager, '000001', 100, 10.5, 'ADD', hours_ago=1)
        
        result = position_manager.can_add_position('000001', 0.80)
        assert result is False
    
    def test_can_add_position_max_trades_reached(self, position_manager):
        """测试达到日交易上限时不能加仓"""
        self._setup_position_and_trades(position_manager, '000001')
        
        # 添加 3 笔今日交易 (达到上限)
        today = datetime.now()
        for i in range(3):
            self._add_trade_record(
                position_manager, '000001', 100, 10.5, 'ADD',
                hours_ago=-i  # 今天的交易
            )
        
        result = position_manager.can_add_position('000001', 0.80)
        assert result is False
    
    def test_can_add_position_added_exceeds_initial(self, position_manager):
        """测试已加仓数量达到初始仓位时不能加仓"""
        self._setup_position_and_trades(position_manager, '000001', initial_qty=1000)
        
        # 添加加仓记录，使已加仓数量等于初始仓位
        self._add_trade_record(position_manager, '000001', 500, 10.5, 'ADD', hours_ago=5)
        self._add_trade_record(position_manager, '000001', 500, 11.0, 'ADD', hours_ago=5)
        
        result = position_manager.can_add_position('000001', 0.80)
        assert result is False
    
    # ==================== get_add_quantity 测试 ====================
    
    def test_get_add_quantity_no_position(self, position_manager):
        """测试没有持仓时加仓数量为 0"""
        result = position_manager.get_add_quantity('000001', 0.10)
        assert result == 0
    
    def test_get_add_quantity_price_increase_10_percent(self, position_manager):
        """测试价格涨幅 >= 10% 时加仓 20%"""
        self._setup_position_and_trades(position_manager, '000001', initial_qty=1000)
        
        # 涨幅 10%
        result = position_manager.get_add_quantity('000001', 0.10)
        assert result == 200  # 1000 * 0.20 = 200
    
    def test_get_add_quantity_price_increase_15_percent(self, position_manager):
        """测试价格涨幅 >= 15% 时加仓 20%"""
        self._setup_position_and_trades(position_manager, '000001', initial_qty=1000)
        
        # 涨幅 15% (仍按 20% 加仓)
        result = position_manager.get_add_quantity('000001', 0.15)
        assert result == 200  # 1000 * 0.20 = 200
    
    def test_get_add_quantity_price_increase_5_percent(self, position_manager):
        """测试价格涨幅 >= 5% 时加仓 30%"""
        self._setup_position_and_trades(position_manager, '000001', initial_qty=1000)
        
        # 涨幅 5%
        result = position_manager.get_add_quantity('000001', 0.05)
        assert result == 300  # 1000 * 0.30 = 300
    
    def test_get_add_quantity_price_increase_8_percent(self, position_manager):
        """测试价格涨幅 8% 时加仓 30%"""
        self._setup_position_and_trades(position_manager, '000001', initial_qty=1000)
        
        # 涨幅 8% (在 5%-10% 之间)
        result = position_manager.get_add_quantity('000001', 0.08)
        assert result == 300  # 1000 * 0.30 = 300
    
    def test_get_add_quantity_price_increase_below_5_percent(self, position_manager):
        """测试价格涨幅 < 5% 时不加仓"""
        self._setup_position_and_trades(position_manager, '000001', initial_qty=1000)
        
        # 涨幅 3%
        result = position_manager.get_add_quantity('000001', 0.03)
        assert result == 0
    
    def test_get_add_quantity_respects_added_limit(self, position_manager):
        """测试加仓数量不超过剩余可加仓数量"""
        self._setup_position_and_trades(position_manager, '000001', initial_qty=1000)
        
        # 已加仓 850
        self._add_trade_record(position_manager, '000001', 850, 10.5, 'ADD', hours_ago=5)
        
        # 涨幅 10%，应加仓 200，但只剩 150 可加
        result = position_manager.get_add_quantity('000001', 0.10)
        assert result == 150  # min(200, 1000-850) = 150
    
    def test_get_add_quantity_pyramid_disabled(self, temp_db, temp_config):
        """测试金字塔加仓禁用时返回 0"""
        import yaml
        
        # 修改配置禁用金字塔
        with open(temp_config, 'w', encoding='utf-8') as f:
            config = {
                'pyramid': {'enabled': False},
                'cooldown': {'hours': 4, 'max_trades_per_day': 3, 'add_position_confidence': 0.70}
            }
            yaml.dump(config, f)
        
        pm = PositionManager(db_path=temp_db, config_path=temp_config)
        self._setup_position_and_trades(pm, '000001', initial_qty=1000)
        
        result = pm.get_add_quantity('000001', 0.10)
        assert result == 0
    
    # ==================== is_in_cooling_period 测试 ====================
    
    def test_is_in_cooling_period_no_trades(self, position_manager):
        """测试没有交易记录时不在冷却期"""
        result = position_manager.is_in_cooling_period('000001')
        assert result is False
    
    def test_is_in_cooling_period_within_period(self, position_manager):
        """测试在冷却期内"""
        self._setup_position_and_trades(position_manager, '000001')
        
        # 添加 1 小时前的交易 (冷却期 4 小时)
        self._add_trade_record(position_manager, '000001', 100, 10.5, 'ADD', hours_ago=1)
        
        result = position_manager.is_in_cooling_period('000001')
        assert result is True
    
    def test_is_in_cooling_period_expired(self, position_manager):
        """测试冷却期已过"""
        self._setup_position_and_trades(position_manager, '000001')
        
        # 添加 5 小时前的交易 (冷却期 4 小时)
        self._add_trade_record(position_manager, '000001', 100, 10.5, 'ADD', hours_ago=5)
        
        result = position_manager.is_in_cooling_period('000001')
        assert result is False
    
    def test_is_in_cooling_period_custom_hours(self, position_manager):
        """测试自定义冷却时间"""
        self._setup_position_and_trades(position_manager, '000001')
        
        # 添加 3 小时前的交易
        self._add_trade_record(position_manager, '000001', 100, 10.5, 'ADD', hours_ago=3)
        
        # 自定义冷却时间 2 小时 (应已过期)
        result = position_manager.is_in_cooling_period('000001', hours=2)
        assert result is False
        
        # 自定义冷却时间 4 小时 (应仍在冷却期)
        result = position_manager.is_in_cooling_period('000001', hours=4)
        assert result is True
    
    # ==================== get_today_trade_count 测试 ====================
    
    def test_get_today_trade_count_no_trades(self, position_manager):
        """测试没有今日交易"""
        result = position_manager.get_today_trade_count('000001')
        assert result == 0
    
    def test_get_today_trade_count_with_trades(self, position_manager):
        """测试有今日交易"""
        # 不调用 _setup_position_and_trades，因为它会添加一笔交易
        # 直接添加 2 笔今日交易
        self._add_trade_record(position_manager, '000001', 100, 10.5, 'ADD', hours_ago=-1)
        self._add_trade_record(position_manager, '000001', 200, 10.8, 'ADD', hours_ago=-2)
        
        result = position_manager.get_today_trade_count('000001')
        assert result == 2
    
    def test_get_today_trade_count_yesterday_trades_not_counted(self, position_manager):
        """测试昨日交易不计入今日"""
        # 添加昨天的交易 (不创建持仓，避免 _setup_position_and_trades 添加今日交易)
        conn = position_manager._get_connection()
        try:
            cursor = conn.cursor()
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('''
                INSERT INTO paper_trades (code, action, quantity, price, timestamp, trade_type)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', ('000001', 'BUY', 100, 10.5, yesterday, 'ADD'))
            conn.commit()
        finally:
            conn.close()
        
        result = position_manager.get_today_trade_count('000001')
        assert result == 0
    
    # ==================== 集成测试 ====================
    
    def test_full_add_position_workflow(self, position_manager):
        """测试完整的加仓流程"""
        code = '000001'
        
        # 1. 创建初始持仓
        self._setup_position_and_trades(position_manager, code, initial_qty=1000, entry_price=10.0)
        
        # 2. 检查是否可以加仓 (应可以，冷却期已过)
        assert position_manager.can_add_position(code, 0.80) is True
        
        # 3. 计算加仓数量 (涨幅 6%)
        add_qty = position_manager.get_add_quantity(code, 0.06)
        assert add_qty == 300  # 1000 * 0.30 = 300
        
        # 4. 执行加仓 (模拟)
        self._add_trade_record(position_manager, code, add_qty, 10.6, 'ADD', hours_ago=0.1)
        
        # 5. 现在在冷却期内，不能立即加仓
        assert position_manager.can_add_position(code, 0.80) is False
        
        # 6. 检查加仓数量
        added_qty = position_manager._get_added_position_quantity(code)
        assert added_qty == 300
    
    def test_pyramid_levels(self, position_manager):
        """测试金字塔不同层级"""
        code = '000001'
        self._setup_position_and_trades(position_manager, code, initial_qty=1000)
        
        # 涨幅 4% - 不加仓
        assert position_manager.get_add_quantity(code, 0.04) == 0
        
        # 涨幅 5% - 加仓 30%
        assert position_manager.get_add_quantity(code, 0.05) == 300
        
        # 涨幅 9% - 加仓 30%
        assert position_manager.get_add_quantity(code, 0.09) == 300
        
        # 涨幅 10% - 加仓 20%
        assert position_manager.get_add_quantity(code, 0.10) == 200
        
        # 涨幅 20% - 加仓 20%
        assert position_manager.get_add_quantity(code, 0.20) == 200


class TestPositionManagerConfigLoading:
    """配置文件加载测试"""
    
    @pytest.fixture
    def temp_db(self):
        """创建临时数据库"""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        yield path
        if os.path.exists(path):
            os.remove(path)
    
    def test_load_config_from_file(self, temp_db):
        """测试从文件加载配置"""
        import yaml
        
        fd, config_path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        
        config = {
            'pyramid': {'enabled': False},
            'cooldown': {'hours': 8, 'max_trades_per_day': 5}
        }
        
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f)
        
        pm = PositionManager(db_path=temp_db, config_path=config_path)
        
        assert pm._config['pyramid']['enabled'] is False
        assert pm._config['cooldown']['hours'] == 8
        assert pm._config['cooldown']['max_trades_per_day'] == 5
        
        os.remove(config_path)
    
    def test_load_config_default_when_missing(self, temp_db):
        """测试配置文件不存在时使用默认配置"""
        pm = PositionManager(db_path=temp_db, config_path='/nonexistent/path/config.yaml')
        
        # 应使用默认配置
        assert pm._config['pyramid']['enabled'] is True
        assert pm._config['cooldown']['hours'] == 4
        assert pm._config['cooldown']['max_trades_per_day'] == 3
    
    def test_load_config_partial_merge(self, temp_db):
        """测试部分配置合并"""
        import yaml
        
        fd, config_path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        
        # 只提供部分配置
        config = {
            'cooldown': {'hours': 6}
        }
        
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f)
        
        pm = PositionManager(db_path=temp_db, config_path=config_path)
        
        # 提供的配置应覆盖默认值
        assert pm._config['cooldown']['hours'] == 6
        # 未提供的配置应使用默认值
        assert pm._config['cooldown']['max_trades_per_day'] == 3
        assert pm._config['pyramid']['enabled'] is True
        
        os.remove(config_path)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
