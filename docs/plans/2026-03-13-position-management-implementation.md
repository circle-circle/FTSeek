# FTSeek 持仓管理策略 - 实施计划

**日期**: 2026-03-13  
**状态**: 待执行  
**预计时间**: 90-120 分钟  
**任务数**: 10

---

## 📝 任务列表

### Task 1: 数据库扩展 - 持仓管理字段
**时间**: 10 分钟  
**文件**: `data/ftseek.db`

**步骤**:
1. 备份数据库
2. 执行 ALTER TABLE 添加新字段
3. 创建索引
4. 验证表结构

**SQL 脚本**:
```sql
-- paper_positions 表扩展
ALTER TABLE paper_positions ADD COLUMN initial_quantity INTEGER;
ALTER TABLE paper_positions ADD COLUMN added_quantity INTEGER DEFAULT 0;
ALTER TABLE paper_positions ADD COLUMN avg_cost REAL;
ALTER TABLE paper_positions ADD COLUMN last_trade_time DATETIME;
ALTER TABLE paper_positions ADD COLUMN trade_count_today INTEGER DEFAULT 0;
ALTER TABLE paper_positions ADD COLUMN stop_loss_price REAL;
ALTER TABLE paper_positions ADD COLUMN take_profit_price REAL;
ALTER TABLE paper_positions ADD COLUMN position_status TEXT DEFAULT 'OPEN';

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_positions_last_trade ON paper_positions(last_trade_time);
CREATE INDEX IF NOT EXISTS idx_positions_status ON paper_positions(position_status);

-- paper_trades 表扩展
ALTER TABLE paper_trades ADD COLUMN trade_reason TEXT;
ALTER TABLE paper_trades ADD COLUMN is_stop_loss INTEGER DEFAULT 0;
ALTER TABLE paper_trades ADD COLUMN is_take_profit INTEGER DEFAULT 0;
```

**验收**:
```bash
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db ".schema paper_positions"
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db "PRAGMA table_info(paper_positions);"
```

---

### Task 2: 创建策略配置文件
**时间**: 5 分钟  
**文件**: `config/position_strategy.yaml` (新建)

**内容**:
```yaml
# 仓位管理
position:
  initial_ratio: 0.50
  max_position_ratio: 0.20
  total_max_ratio: 0.80

# 金字塔加仓
pyramid:
  enabled: true
  level1:
    price_increase: 0.05
    ratio: 0.30
    min_confidence: 0.70
  level2:
    price_increase: 0.10
    ratio: 0.20
    min_confidence: 0.70

# 出仓策略
exit:
  stop_loss: -0.05
  take_profit: 0.15
  take_profit_ratio: 0.50

# 交易间隔
cooldown:
  hours: 4
  max_trades_per_day: 3
  min_confidence: 0.60
  add_position_confidence: 0.70
```

**验收**:
```bash
python3 -c "import yaml; yaml.safe_load(open('config/position_strategy.yaml'))"
```

---

### Task 3: 扩展 PositionManager - 加仓逻辑
**时间**: 20 分钟  
**文件**: `app/trader/position_manager.py` (修改)

**新增方法**:
```python
def can_add_position(self, code: str, signal_confidence: float) -> bool:
    """检查是否可以加仓"""
    position = self.get_position(code)
    if not position:
        return False
    
    # 检查仓位上限
    if position.added_quantity >= position.initial_quantity:
        return False
    
    # 检查信号置信度
    if signal_confidence < self.config.add_position_confidence:
        return False
    
    # 检查冷却时间
    if self.is_in_cooling_period(code):
        return False
    
    # 检查今日交易次数
    if self.get_today_trade_count(code) >= self.config.max_trades_per_day:
        return False
    
    return True

def get_add_quantity(self, code: str, price_increase: float) -> int:
    """计算加仓数量 (金字塔)"""
    position = self.get_position(code)
    
    if price_increase >= 0.10:
        # 第 2 次加仓：20%
        ratio = 0.20
    elif price_increase >= 0.05:
        # 第 1 次加仓：30%
        ratio = 0.30
    else:
        return 0
    
    return int(position.initial_quantity * ratio)

def is_in_cooling_period(self, code: str, hours: int = 4) -> bool:
    """检查是否在冷却期内"""
    position = self.get_position(code)
    if not position or not position.last_trade_time:
        return False
    
    last_trade = datetime.strptime(position.last_trade_time, '%Y-%m-%d %H:%M:%S')
    cooldown_end = last_trade + timedelta(hours=hours)
    
    return datetime.now() < cooldown_end

def get_today_trade_count(self, code: str) -> int:
    """获取今日交易次数"""
    today = datetime.now().strftime('%Y-%m-%d')
    # 查询 paper_trades 表今日交易记录
```

**验收**:
```bash
python3 -m pytest tests/test_position_manager.py::test_can_add_position -v
python3 -m pytest tests/test_position_manager.py::test_get_add_quantity -v
python3 -m pytest tests/test_position_manager.py::test_is_in_cooling_period -v
```

---

### Task 4: 扩展 PositionManager - 止损止盈
**时间**: 15 分钟  
**文件**: `app/trader/position_manager.py` (修改)

**新增方法**:
```python
def check_stop_loss(self, code: str, current_price: float) -> bool:
    """检查是否触发止损"""
    position = self.get_position(code)
    if not position:
        return False
    
    pnl_rate = (current_price - position.avg_cost) / position.avg_cost
    return pnl_rate <= self.config.stop_loss

def check_take_profit(self, code: str, current_price: float) -> bool:
    """检查是否触发止盈"""
    position = self.get_position(code)
    if not position:
        return False
    
    pnl_rate = (current_price - position.avg_cost) / position.avg_cost
    return pnl_rate >= self.config.take_profit

def calculate_stop_loss_price(self, avg_cost: float) -> float:
    """计算止损价格"""
    return avg_cost * (1 + self.config.stop_loss)

def calculate_take_profit_price(self, avg_cost: float) -> float:
    """计算止盈价格"""
    return avg_cost * (1 + self.config.take_profit)
```

**验收**:
```bash
python3 -m pytest tests/test_position_manager.py::test_check_stop_loss -v
python3 -m pytest tests/test_position_manager.py::test_check_take_profit -v
```

---

### Task 5: 扩展 TradeExecutor - 部分平仓
**时间**: 15 分钟  
**文件**: `app/trader/executor.py` (修改)

**新增方法**:
```python
def execute_partial_close(self, signal: Signal, close_ratio: float = 0.5) -> TradeResult:
    """
    执行部分平仓
    :param close_ratio: 平仓比例 (0.5 = 50%)
    """
    position = self.position_manager.get_position(signal.code)
    if not position:
        return TradeResult(success=False, message="无持仓")
    
    # 计算平仓数量
    close_quantity = int(position.quantity * close_ratio)
    if close_quantity < 1:
        return TradeResult(success=False, message="平仓数量不足 1 股")
    
    # 执行平仓
    # ... (类似 execute_close，但使用 close_quantity)
    
    return TradeResult(
        success=True,
        order_id=order_id,
        position_id=position.id,
        trade_type='PARTIAL_CLOSE',
        pnl=pnl,
        close_ratio=close_ratio,
        remaining_quantity=position.quantity - close_quantity
    )
```

**验收**:
```bash
python3 -m pytest tests/test_trade_executor.py::test_execute_partial_close -v
```

---

### Task 6: 修改数据订阅器 - 加仓逻辑
**时间**: 15 分钟  
**文件**: `app/data_subscriber.py` (修改)

**修改内容**:
```python
def _determine_trade_type(self, position: Optional[Position], 
                          action: SignalAction, confidence: float) -> str:
    """判断交易类型 (开仓/加仓/平仓)"""
    if position is None:
        return 'OPEN'
    
    if position.action == 'BUY' and action == SignalAction.SELL:
        return 'CLOSE'
    if position.action == 'SELL' and action == SignalAction.BUY:
        return 'CLOSE'
    
    # 同向信号，检查加仓
    if self.position_manager.can_add_position(position.code, confidence):
        return 'ADD'
    
    return 'IGNORE'  # 同向但不满足加仓条件

def _execute_add_position(self, signal: Signal, position: Position) -> TradeResult:
    """执行加仓"""
    # 计算当前价格涨幅
    current_price = get_current_price(signal.code)
    price_increase = (current_price - position.avg_cost) / position.avg_cost
    
    # 计算加仓数量 (金字塔)
    add_quantity = self.position_manager.get_add_quantity(signal.code, price_increase)
    
    if add_quantity < 1:
        return TradeResult(success=False, message="加仓数量不足")
    
    # 执行加仓
    signal.quantity = add_quantity
    return self.trade_executor.execute(signal, trade_type='ADD')
```

**验收**:
- 代码审查通过
- 单元测试通过

---

### Task 7: 创建监控任务 - 止损止盈检查
**时间**: 15 分钟  
**文件**: `app/trader/position_monitor.py` (新建)

**内容**:
```python
"""
持仓监控任务
每 5 分钟检查一次所有持仓的止损止盈状态
"""
import logging
import threading
import time
from datetime import datetime
from typing import List

from app.trader.position_manager import PositionManager
from app.trader.executor import TradeExecutor
from app.strategy.base import Signal, SignalAction

logger = logging.getLogger("ftseek.trader.monitor")

class PositionMonitor:
    def __init__(self):
        self.position_manager = PositionManager()
        self.trade_executor = TradeExecutor()
        self._running = False
        self._thread = None
    
    def start(self, interval_minutes: int = 5):
        """启动监控任务"""
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            args=(interval_minutes,),
            daemon=True
        )
        self._thread.start()
        logger.info(f"持仓监控已启动，间隔={interval_minutes}分钟")
    
    def stop(self):
        """停止监控任务"""
        self._running = False
        if self._thread:
            self._thread.join()
        logger.info("持仓监控已停止")
    
    def _monitor_loop(self, interval_minutes: int):
        """监控循环"""
        while self._running:
            try:
                self._check_all_positions()
            except Exception as e:
                logger.error(f"监控异常：{e}")
            
            time.sleep(interval_minutes * 60)
    
    def _check_all_positions(self):
        """检查所有持仓"""
        positions = self.position_manager.get_all_positions()
        
        for position in positions:
            current_price = self._get_current_price(position.code)
            
            # 检查止损
            if self.position_manager.check_stop_loss(position.code, current_price):
                logger.warning(f"触发止损：{position.code} 盈亏={(current_price-position.avg_cost)/position.avg_cost:.2%}")
                self._execute_stop_loss(position, current_price)
            
            # 检查止盈
            elif self.position_manager.check_take_profit(position.code, current_price):
                logger.info(f"触发止盈：{position.code} 盈亏={(current_price-position.avg_cost)/position.avg_cost:.2%}")
                self._execute_take_profit(position, current_price)
    
    def _get_current_price(self, code: str) -> float:
        """获取当前价格"""
        # 从富途 API 获取
        pass
    
    def _execute_stop_loss(self, position, current_price: float):
        """执行止损平仓"""
        signal = Signal(
            code=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            quantity=position.quantity,
            price=current_price,
            confidence=1.0  # 止损强制平仓
        )
        
        result = self.trade_executor.execute(signal, trade_type='CLOSE')
        
        if result.success:
            logger.info(f"止损平仓成功：{position.code} 亏损={result.pnl:.2f}")
        else:
            logger.error(f"止损平仓失败：{position.code} {result.message}")
    
    def _execute_take_profit(self, position, current_price: float):
        """执行止盈平仓 (50%)"""
        result = self.trade_executor.execute_partial_close(
            Signal(
                code=position.code,
                action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
                quantity=position.quantity,
                price=current_price
            ),
            close_ratio=0.5
        )
        
        if result.success:
            logger.info(f"止盈平仓成功：{position.code} 盈利={result.pnl:.2f}")
        else:
            logger.error(f"止盈平仓失败：{position.code} {result.message}")

# 单例
_monitor_instance = None

def get_position_monitor() -> PositionMonitor:
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = PositionMonitor()
    return _monitor_instance
```

**验收**:
```bash
python3 -m pytest tests/test_position_monitor.py -v
```

---

### Task 8: 集成到主程序
**时间**: 10 分钟  
**文件**: `app/main.py` (修改)

**修改内容**:
```python
from app.trader.position_monitor import get_position_monitor

# 启动时初始化
position_monitor = get_position_monitor()
position_monitor.start(interval_minutes=5)

# 停止时清理
def cleanup():
    position_monitor.stop()
    # ... 其他清理
```

**验收**:
- 服务启动正常
- 监控任务正常运行
- 日志输出正常

---

### Task 9: 钉钉推送扩展 - 止损止盈通知
**时间**: 10 分钟  
**文件**: `app/notifier/dingtalk.py` (修改)

**新增方法**:
```python
def send_stop_loss_message(self, position, pnl: float, pnl_rate: float):
    """发送止损平仓通知"""
    message = f"""⚠️ {position.code} {position.code_name} 止损平仓
━━━━━━━━━━━━━━━━━━━━
开仓：{position.initial_quantity} 股 @ HK$ {position.avg_cost:.2f}
平仓：{position.quantity} 股 @ HK$ {position.exit_price:.2f}
━━━━━━━━━━━━━━━━━━━━
💸 盈亏：HK$ {pnl:+,.2f} ({pnl_rate:+.2f}%)
触发条件：亏损 ≥ 5%
━━━━━━━━━━━━━━━━━━━━
风控提示：请检查策略信号
"""
    self._send_markdown(message)

def send_take_profit_message(self, position, pnl: float, pnl_rate: float, 
                             close_ratio: float, remaining: int):
    """发送止盈平仓通知"""
    message = f"""📊 {position.code} {position.code_name} 止盈部分平仓
━━━━━━━━━━━━━━━━━━━━
开仓：{position.initial_quantity} 股 @ HK$ {position.avg_cost:.2f}
平仓：{int(position.quantity * close_ratio)} 股 @ HK$ {position.exit_price:.2f}
━━━━━━━━━━━━━━━━━━━━
💰 盈亏：HK$ {pnl:+,.2f} ({pnl_rate:+.2f}%)
触发条件：盈利 ≥ 15%
━━━━━━━━━━━━━━━━━━━━
剩余持仓：{remaining} 股
等待反向信号平仓
"""
    self._send_markdown(message)
```

**验收**:
```bash
python3 -m pytest tests/test_dingtalk_notifier.py::test_send_stop_loss_message -v
python3 -m pytest tests/test_dingtalk_notifier.py::test_send_take_profit_message -v
```

---

### Task 10: 部署与验证
**时间**: 10 分钟  
**文件**: 生产环境

**步骤**:
1. 备份数据库和代码
2. 执行数据库迁移
3. 部署代码
4. 重启服务
5. 验证监控任务运行
6. 等待交易信号验证

**验收清单**:
- [ ] 数据库扩展完成
- [ ] 配置文件生效
- [ ] 加仓逻辑正确
- [ ] 止损止盈检查正常
- [ ] 部分平仓功能正常
- [ ] 监控任务运行正常
- [ ] 钉钉推送格式正确

---

## 📊 执行顺序

```
Task 1 (数据库) → Task 2 (配置) → Task 3 (加仓) → Task 4 (止损止盈)
                                              ↓
Task 10 (部署) ← Task 9 (推送) ← Task 8 (集成) ← Task 7 (监控) ← Task 6 (订阅器) ← Task 5 (部分平仓)
```

---

## ⚠️ 注意事项

1. **数据库备份**: 必须备份
2. **服务重启**: 部署后重启
3. **监控验证**: 确认监控任务运行
4. **日志监控**: 部署后密切监控日志

---

## 🎯 完成标准

- [ ] 所有 10 个任务完成
- [ ] 所有测试通过
- [ ] 生产环境验证通过
- [ ] 止损止盈监控正常
- [ ] 金字塔加仓正确

---

*实施计划结束*
