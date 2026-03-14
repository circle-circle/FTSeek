# FTSeek 模拟交易推送优化设计文档

**日期**: 2026-03-13  
**作者**: 阿呆 (OpenClaw)  
**状态**: 已批准  
**版本**: 1.0

---

## 🎯 目标

实现模拟交易闭环推送，确保：
1. **开仓推送**: 模拟下单成功后推送详细交易信息
2. **持仓跟踪**: 实时记录持仓和浮动盈亏
3. **平仓推送**: 策略反向信号触发平仓后推送盈亏结果
4. **完整闭环**: 每笔交易有开仓、有平仓、有盈亏统计

---

## 📊 当前架构

```
策略引擎 → 共识评估 → 信号生成 → 钉钉推送
                                    ↓
                              (无实际交易)
```

## 🏗️ 目标架构

```
策略引擎 → 共识评估 → 信号生成
                        ↓
              ┌────────┴────────┐
              ↓                 ↓
        检查持仓状态      执行模拟交易
              ↓                 ↓
        反向信号？          下单成功？
              ↓                 ↓
        执行平仓          记录 + 推送开仓
              ↓                 ↓
        推送盈亏结果        更新持仓
```

---

## 🔧 核心改动

### 1. 数据模型扩展

#### paper_positions (持仓表) - 新增/修改
```sql
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,           -- 股票代码
    action TEXT NOT NULL,         -- BUY/SELL (多/空)
    quantity INTEGER NOT NULL,    -- 持仓数量
    entry_price REAL NOT NULL,    -- 开仓均价
    entry_time DATETIME NOT NULL, -- 开仓时间
    current_price REAL,           -- 当前价 (实时更新)
    market_value REAL,            -- 市值 = quantity * current_price
    unrealized_pnl REAL,          -- 浮动盈亏
    unrealized_pnl_rate REAL,     -- 浮动盈亏率
    status TEXT DEFAULT 'OPEN',   -- OPEN/CLOSED
    exit_price REAL,              -- 平仓价
    exit_time DATETIME,           -- 平仓时间
    realized_pnl REAL,            -- 已实现盈亏
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### paper_trades (交易记录表) - 扩展现有
```sql
ALTER TABLE paper_trades ADD COLUMN order_id TEXT;      -- 订单 ID
ALTER TABLE paper_trades ADD COLUMN position_id INTEGER; -- 关联持仓 ID
ALTER TABLE paper_trades ADD COLUMN commission REAL DEFAULT 0; -- 手续费
```

---

### 2. 核心模块

#### A. 交易执行器 (TradeExecutor) - 扩展
**文件**: `app/trader/executor.py`

**新增方法**:
```python
def execute(self, signal: Signal) -> TradeResult:
    """
    执行交易信号
    返回：TradeResult {
        success: bool,
        order_id: str,
        position_id: int,  # 新增
        message: str,
        trade_type: str    # 新增：OPEN/CLOSE
    }
    """
```

**修改点**:
- 执行开仓时，创建 paper_positions 记录
- 执行平仓时，更新 paper_positions 状态 + 计算盈亏
- 返回 position_id 供推送使用

---

#### B. 持仓管理器 (PositionManager) - 新增
**文件**: `app/trader/position_manager.py`

**核心功能**:
```python
class PositionManager:
    def get_position(self, code: str) -> Optional[Position]:
        """获取某股票的当前持仓"""
    
    def get_all_positions(self) -> List[Position]:
        """获取所有持仓"""
    
    def update_price(self, code: str, price: float):
        """更新某股票的当前价和浮动盈亏"""
    
    def close_position(self, code: str, exit_price: float, exit_time: str) -> float:
        """平仓并返回已实现盈亏"""
    
    def calculate_pnl(self, entry_price: float, exit_price: float, 
                      quantity: int, action: str) -> float:
        """计算盈亏"""
```

---

#### C. 数据订阅器 (DataSubscriber) - 修改
**文件**: `app/data_subscriber.py`

**修改逻辑**:
```python
def _push_consensus_signal(self, code: str, result: ConsensusResult):
    # 1. 检查是否已有持仓
    position = position_manager.get_position(code)
    
    # 2. 判断交易类型
    if position and result.action == opposite(position.action):
        # 反向信号 → 平仓
        trade_type = "CLOSE"
    else:
        # 同向或无持仓 → 开仓
        trade_type = "OPEN"
    
    # 3. 执行交易
    trade_result = trade_executor.execute(consensus_signal, trade_type)
    
    # 4. 推送 (带详细信息)
    if trade_result.success:
        notifier.send_detailed_signal(
            signal=consensus_signal,
            trade_result=trade_result,
            position=position,
            result=result
        )
```

---

#### D. 钉钉推送器 (DingTalkNotifier) - 扩展
**文件**: `app/notifier/dingtalk.py`

**新增方法**:
```python
def send_detailed_signal(self, signal: Signal, trade_result: TradeResult, 
                         position: Position, consensus_result: ConsensusResult):
    """
    发送详细版交易推送
    包含：订单信息、策略信号、风控检查
    """
```

**推送模板**:
```markdown
✅ HK.00700 腾讯控股 买入成交
━━━━━━━━━━━━━━━━━━━━
订单 ID: 12345
数量：100 股
成交价：HK$ 420.00
总金额：HK$ 42,000.00
时间：2026-03-13 15:30:00
━━━━━━━━━━━━━━━━━━━━
策略信号:
  confluence: BUY (+1.2)
  macd: BUY (+0.8)
  supertrend: NEUTRAL
加权分：+2.86 (置信度 85%)
市场状态：trending (ADX=35)
━━━━━━━━━━━━━━━━━━━━
风控检查:
  ✅ 仓位 2.1% < 20%
  ✅ 日交易 3/10
  ✅ 回撤 0.5% < 15%
```

**平仓推送模板**:
```markdown
📊 HK.00700 腾讯控股 平仓盈亏
━━━━━━━━━━━━━━━━━━━━
开仓：100 股 @ HK$ 420.00 (3 月 13 日 15:30)
平仓：100 股 @ HK$ 445.00 (3 月 13 日 15:50)
━━━━━━━━━━━━━━━━━━━━
盈亏：+HK$ 2,500.00 (+5.95%)
持仓时长：20 分钟
━━━━━━━━━━━━━━━━━━━━
策略：反向信号触发平仓
原信号：BUY → 平仓信号：SELL
```

---

### 3. 风控集成

**文件**: `app/risk/risk_manager.py`

**新增检查**:
```python
def check_before_trade(self, signal: Signal) -> RiskCheckResult:
    """交易前风控检查"""
    checks = {
        'position_limit': self.check_position_limit(signal.code),
        'daily_trades': self.check_daily_trades(),
        'daily_loss': self.check_daily_loss(),
        'drawdown': self.check_drawdown(),
        'confidence': self.check_confidence(signal.confidence)
    }
    return RiskCheckResult(passed=all(checks.values()), details=checks)
```

---

## 📈 数据流

### 开仓流程
```
1. 共识信号生成 (HK.00700 BUY)
        ↓
2. 检查无持仓 → 开仓交易
        ↓
3. TradeExecutor 下单 → 返回 order_id + position_id
        ↓
4. 保存到 paper_positions (status=OPEN)
        ↓
5. 保存到 paper_trades (type=OPEN)
        ↓
6. 风控检查通过
        ↓
7. 钉钉推送 (详细版开仓通知)
```

### 平仓流程
```
1. 共识信号生成 (HK.00700 SELL)
        ↓
2. 检查有 BUY 持仓 → 平仓交易
        ↓
3. 获取当前价 → 计算盈亏
        ↓
4. TradeExecutor 平仓 → 更新 position status=CLOSED
        ↓
5. 更新 paper_positions (exit_price, realized_pnl)
        ↓
6. 保存到 paper_trades (type=CLOSE, pnl=xxx)
        ↓
7. 钉钉推送 (详细版平仓盈亏)
```

---

## 🔍 监控指标

| 指标 | 计算方式 | 目标 |
|------|----------|------|
| 胜率 | 盈利交易数 / 总交易数 | > 55% |
| 盈亏比 | 平均盈利 / 平均亏损 | > 1.5 |
| 最大回撤 | 峰值到谷底的最大的亏损 | < 15% |
| 夏普比率 | (收益 - 无风险利率) / 标准差 | > 1.0 |
| 平均持仓时长 | 总持仓时间 / 交易次数 | < 2 天 |

---

## ⚠️ 边界情况处理

### 1. 部分平仓
- 当前版本不支持，后续迭代添加

### 2. 多次同向信号
- 已有持仓 + 同向信号 → 忽略或加仓 (当前忽略)

### 3. 价格异常
- 当前价缺失 → 使用最新成交价
- 价格波动 > 10% → 警告并跳过

### 4. 交易失败
- 下单失败 → 记录日志，不推送
- 网络错误 → 重试 3 次，失败则跳过

---

## 📝 测试策略

### 单元测试
- PositionManager.calculate_pnl()
- TradeExecutor.execute() 开仓/平仓
- DingTalkNotifier.format_message()

### 集成测试
- 完整开仓→平仓流程
- 反向信号触发平仓
- 风控拦截超限交易

### 回测验证
- 使用历史数据验证策略
- 对比推送记录与实际交易

---

## 🚀 部署计划

1. **数据库迁移**: 执行 SQL 脚本创建/修改表
2. **代码部署**: 更新 4 个核心文件
3. **服务重启**: systemctl restart ftseek
4. **验证测试**: 等待下一个交易信号
5. **监控观察**: 检查推送和交易记录

---

## 📌 验收标准

- [ ] 开仓推送包含订单 ID、数量、价格、策略详情、风控检查
- [ ] 平仓推送包含开仓/平仓价、盈亏金额、盈亏率、持仓时长
- [ ] paper_positions 表正确记录持仓状态
- [ ] paper_trades 表正确记录开仓和平仓交易
- [ ] 反向信号正确触发平仓
- [ ] 风控检查在交易前执行
- [ ] 所有推送仅针对 enabled=true 的股票

---

*设计文档结束*
