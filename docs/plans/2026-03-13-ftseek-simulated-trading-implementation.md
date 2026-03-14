# FTSeek 模拟交易推送优化 - 实施计划

**日期**: 2026-03-13  
**状态**: 待执行  
**预计时间**: 60-90 分钟  
**任务数**: 8

---

## 📝 任务列表

### Task 1: 数据库迁移 - 扩展持仓表
**时间**: 5 分钟  
**文件**: `data/ftseek.db`

**步骤**:
1. 检查 paper_positions 表是否存在
2. 执行 ALTER TABLE 添加新字段
3. 创建索引
4. 验证表结构

**SQL 脚本**:
```sql
-- 如果表不存在则创建
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_time DATETIME NOT NULL,
    current_price REAL,
    market_value REAL,
    unrealized_pnl REAL,
    unrealized_pnl_rate REAL,
    status TEXT DEFAULT 'OPEN',
    exit_price REAL,
    exit_time DATETIME,
    realized_pnl REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 添加索引
CREATE INDEX IF NOT EXISTS idx_positions_code ON paper_positions(code);
CREATE INDEX IF NOT EXISTS idx_positions_status ON paper_positions(status);

-- 扩展 paper_trades 表
ALTER TABLE paper_trades ADD COLUMN order_id TEXT;
ALTER TABLE paper_trades ADD COLUMN position_id INTEGER;
ALTER TABLE paper_trades ADD COLUMN commission REAL DEFAULT 0;
ALTER TABLE paper_trades ADD COLUMN trade_type TEXT DEFAULT 'OPEN';
```

**验收**:
```bash
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db ".schema paper_positions"
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db ".schema paper_trades"
```

---

### Task 2: 创建持仓管理器 (PositionManager)
**时间**: 15 分钟  
**文件**: `app/trader/position_manager.py` (新建)

**步骤**:
1. 创建 PositionManager 类
2. 实现 get_position() 方法
3. 实现 get_all_positions() 方法
4. 实现 update_price() 方法
5. 实现 close_position() 方法
6. 实现 calculate_pnl() 方法
7. 创建单元测试

**核心代码结构**:
```python
class PositionManager:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or '/opt/ftseek/FTSeek/data/ftseek.db'
    
    def get_position(self, code: str) -> Optional[Position]:
        """获取某股票的当前持仓"""
    
    def get_all_positions(self) -> List[Position]:
        """获取所有持仓"""
    
    def update_price(self, code: str, price: float):
        """更新当前价和浮动盈亏"""
    
    def close_position(self, code: str, exit_price: float, exit_time: str) -> float:
        """平仓并返回已实现盈亏"""
    
    def calculate_pnl(self, entry_price: float, exit_price: float,
                      quantity: int, action: str) -> float:
        """计算盈亏"""
```

**验收**:
```bash
python3 -m pytest /opt/ftseek/FTSeek/tests/test_position_manager.py -v
```

---

### Task 3: 扩展交易执行器 (TradeExecutor)
**时间**: 15 分钟  
**文件**: `app/trader/executor.py` (修改)

**步骤**:
1. 修改 execute() 方法签名，添加 trade_type 参数
2. 开仓逻辑：创建 paper_positions 记录
3. 平仓逻辑：更新 paper_positions 状态 + 计算盈亏
4. 返回 TradeResult (包含 position_id)
5. 添加单元测试

**修改点**:
```python
def execute(self, signal: Signal, trade_type: str = 'OPEN') -> TradeResult:
    if trade_type == 'OPEN':
        # 创建持仓记录
        position_id = self._create_position(signal)
    else:
        # 平仓
        pnl = self._close_position(signal)
        position_id = None
    
    return TradeResult(
        success=True,
        order_id=order_id,
        position_id=position_id,
        trade_type=trade_type,
        pnl=pnl if trade_type == 'CLOSE' else None
    )
```

**验收**:
```bash
python3 -m pytest /opt/ftseek/FTSeek/tests/test_trade_executor.py -v
```

---

### Task 4: 修改数据订阅器 (DataSubscriber)
**时间**: 15 分钟  
**文件**: `app/data_subscriber.py` (修改)

**步骤**:
1. 导入 PositionManager
2. 修改 _push_consensus_signal() 方法
3. 添加持仓检查逻辑
4. 判断开仓/平仓
5. 调用新的推送方法

**修改逻辑**:
```python
from app.trader.position_manager import PositionManager

position_manager = PositionManager()

def _push_consensus_signal(self, code: str, result: ConsensusResult):
    # 检查持仓
    position = position_manager.get_position(code)
    
    # 判断交易类型
    if position and result.action == opposite(position.action):
        trade_type = "CLOSE"
    else:
        trade_type = "OPEN"
    
    # 执行交易
    trade_result = trade_executor.execute(consensus_signal, trade_type)
    
    # 推送
    if trade_result.success:
        notifier.send_detailed_signal(...)
```

**验收**:
- 代码编译通过
- 逻辑审查通过

---

### Task 5: 扩展钉钉推送器 (DingTalkNotifier)
**时间**: 15 分钟  
**文件**: `app/notifier/dingtalk.py` (修改)

**步骤**:
1. 创建 send_detailed_signal() 方法
2. 实现开仓推送模板
3. 实现平仓推送模板
4. 添加策略详情格式化
5. 添加风控检查格式化
6. 单元测试

**推送模板**:
```python
def _format_open_message(self, signal, trade_result, consensus) -> str:
    return f"""✅ {signal.code} {stock_name} 买入成交
━━━━━━━━━━━━━━━━━━━━
订单 ID: {trade_result.order_id}
数量：{signal.quantity} 股
成交价：HK$ {signal.price:.2f}
总金额：HK$ {signal.price * signal.quantity:,.2f}
时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
━━━━━━━━━━━━━━━━━━━━
策略信号:
  {self._format_strategies(consensus)}
加权分：{consensus.score:+.2f} (置信度 {consensus.confidence:.0%})
市场状态：{consensus.market_regime}
━━━━━━━━━━━━━━━━━━━━
风控检查:
  {self._format_risk_checks()}
"""
```

**验收**:
```bash
python3 -m pytest /opt/ftseek/FTSeek/tests/test_dingtalk_notifier.py -v
```

---

### Task 6: 风控检查集成
**时间**: 10 分钟  
**文件**: `app/risk/risk_manager.py` (修改)

**步骤**:
1. 创建 check_before_trade() 方法
2. 实现仓位限制检查
3. 实现日交易次数检查
4. 实现日亏损检查
5. 实现回撤检查
6. 实现置信度检查
7. 在交易执行前调用

**验收**:
```bash
python3 -m pytest /opt/ftseek/FTSeek/tests/test_risk_manager.py::test_check_before_trade -v
```

---

### Task 7: 集成测试
**时间**: 10 分钟  
**文件**: `tests/test_integration.py` (新建)

**步骤**:
1. 创建集成测试文件
2. 测试开仓→平仓完整流程
3. 测试反向信号触发平仓
4. 测试风控拦截
5. 测试推送内容格式

**测试用例**:
```python
def test_open_position_flow():
    """测试开仓流程"""
    
def test_close_position_on_reverse_signal():
    """测试反向信号平仓"""
    
def test_risk_check_blocks_over_limit():
    """测试风控拦截"""
    
def test_detailed_push_message_format():
    """测试推送格式"""
```

**验收**:
```bash
python3 -m pytest /opt/ftseek/FTSeek/tests/test_integration.py -v
```

---

### Task 8: 部署与验证
**时间**: 10 分钟  
**文件**: 生产环境

**步骤**:
1. 备份数据库
2. 执行数据库迁移
3. 部署代码
4. 重启服务
5. 等待交易信号
6. 验证推送内容
7. 检查数据库记录

**验证命令**:
```bash
# 备份
cp /opt/ftseek/FTSeek/data/ftseek.db /opt/ftseek/FTSeek/data/ftseek.db.backup

# 执行迁移
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db < /opt/ftseek/FTSeek/docs/migrations/001_positions.sql

# 重启服务
systemctl restart ftseek

# 查看日志
tail -f /opt/ftseek/FTSeek/logs/ftseek.log | grep -E "模拟交易 | 钉钉推送"

# 检查记录
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db "SELECT * FROM paper_positions ORDER BY id DESC LIMIT 3;"
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db "SELECT * FROM paper_trades ORDER BY id DESC LIMIT 5;"
```

**验收**:
- [ ] 服务正常启动
- [ ] 开仓推送包含详细信息
- [ ] 平仓推送包含盈亏
- [ ] 数据库记录正确
- [ ] 无异常日志

---

## 📊 执行顺序

```
Task 1 (数据库) → Task 2 (PositionManager) → Task 3 (TradeExecutor)
                                              ↓
Task 8 (部署) ← Task 7 (集成测试) ← Task 6 (风控) ← Task 5 (推送) ← Task 4 (订阅器)
```

---

## ⚠️ 注意事项

1. **数据库备份**: 执行迁移前必须备份
2. **服务停止**: 部署时先停止服务
3. **缓存清除**: 清除 Python 字节码缓存
4. **日志监控**: 部署后密切监控日志
5. **回滚方案**: 保留备份，随时可回滚

---

## 🎯 完成标准

- [ ] 所有 8 个任务完成
- [ ] 所有测试通过
- [ ] 生产环境验证通过
- [ ] 推送内容符合详细版要求
- [ ] 开仓→平仓闭环正常工作

---

*实施计划结束*
