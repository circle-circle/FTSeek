# 统一决策引擎设计文档

**创建日期**: 2026-03-12  
**作者**: 阿呆 (Ah Dai)  
**状态**: 待审批  
**版本**: v1.0

---

## 1. 背景与目标

### 1.1 问题陈述

当前 FTSeek 系统存在以下问题：
1. **信号过于频繁** - 加权共振信号持续推送，缺乏频率控制
2. **缺少模拟验证** - 信号直接推送，无纸面交易验证策略有效性
3. **无动态决策** - 缺少止盈止损机制，无法自动锁定收益
4. **无收益追踪** - 无法追踪是否达到 5% 收益目标
5. **无交易汇总** - 缺少每日/每周交易报告

### 1.2 设计目标

1. **频率控制** - 每只股票最多 2 个信号/小时，同方向去重
2. **模拟先行** - 新策略先模拟运行 3 天，验证有效后切换实盘
3. **动态止盈止损** - 收益≥5% 自动止盈，亏损≥3% 自动止损
4. **收益目标追踪** - 实时追踪收益率，确保≥5% 目标
5. **交易汇总报告** - 每日/每周自动推送交易报告
6. **市场时段管理** - 港股 (09:30-16:00 HKT) 和美股 (21:30-04:00 EST) 独立运行

---

## 2. 系统架构

### 2.1 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                    信号生成层 (现有)                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │ fusion_v2   │  │ rsi_rebound │  │ ma_cross    │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                    统一决策引擎 (新增)                         │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              DecisionEngine                         │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │    │
│  │  │ RateLimiter │  │ SessionMgr  │  │ ModeRouter  │ │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘ │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                    交易执行层 (新增)                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │ PaperTrader │  │ LiveTrader  │  │ ExitManager │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                    报告推送层 (新增)                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │ DailyReport │  │ WeeklyReport│  │ PerfTracker │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 核心模块

#### 2.2.1 DecisionEngine (决策引擎)

**职责**: 统一决策入口，路由信号到模拟/实盘

**接口**:
```python
class DecisionEngine:
    def process_signal(self, signal: Signal) -> DecisionResult
    def set_mode(self, code: str, mode: TradingMode)
    def get_mode(self, code: str) -> TradingMode
```

**TradingMode 枚举**:
- `PAPER` - 纯模拟模式
- `HYBRID` - 高置信度 (>0.8) 实盘，低置信度模拟
- `LIVE` - 纯实盘模式

#### 2.2.2 SignalRateLimiter (信号限流器)

**职责**: 控制信号频率，避免过度交易

**规则**:
- 每只股票最多 2 个信号/小时
- 同方向信号去重 (连续买入信号只保留第一个)
- 冷静期：卖出后 15 分钟内不再接受买入信号
- 全局开关：可临时禁用所有信号

**实现**:
```python
class SignalRateLimiter:
    def __init__(self, max_signals_per_hour=2, cooldown_minutes=15)
    
    def should_allow(self, code: str, action: SignalAction) -> bool
    def reset(self, code: str)  # 重置限流状态
```

#### 2.2.3 MarketSessionManager (市场时段管理器)

**职责**: 管理港股/美股交易时段

**时段配置**:
- **港股**: 09:30-12:00, 13:00-16:00 HKT (周一到周五)
- **美股**: 21:30-04:00 EST (周一到周五)
- **盘前**: 开盘前 30 分钟可生成信号，不执行
- **盘后**: 收盘后 30 分钟内可平仓

**实现**:
```python
class MarketSessionManager:
    def is_trading_session(self, code: str) -> bool
    def next_session_start(self, code: str) -> datetime
    def get_session_type(self, code: str) -> SessionType  # PRE/REGULAR/POST
```

#### 2.2.4 PaperTradingEngine (模拟交易引擎)

**职责**: 纸面交易执行和持仓管理

**功能**:
- 虚拟账户 (腾讯/MU 独立)
- 模拟成交 (按实时价格，考虑滑点)
- 持仓追踪 (成本价、当前价、盈亏)
- 交易历史记录

**数据结构**:
```python
@dataclass
class PaperPosition:
    code: str
    quantity: int
    avg_cost: float
    current_price: float
    entry_time: datetime
    unrealized_pnl: float
    unrealized_return: float

@dataclass
class PaperTrade:
    code: str
    action: str  # BUY/SELL
    quantity: int
    price: float
    timestamp: datetime
    pnl: float  # 卖出时填充
```

#### 2.2.5 AutoExitManager (自动退出管理器)

**职责**: 动态止盈止损决策

**退出策略**:
1. **目标止盈**: 收益率≥5% → 自动卖出
2. **止损**: 亏损≥3% → 自动卖出 (可配置)
3. **时间退出**: 持仓超 24 小时 → 自动平仓
4. **Trailing Stop**: 从最高点回撤≥2% → 卖出 (可选)

**实现**:
```python
class AutoExitManager:
    def check_exit(self, position: Position) -> Optional[ExitDecision]
    
@dataclass
class ExitDecision:
    should_exit: bool
    reason: str  # TARGET_PROFIT/STOP_LOSS/TIMEOUT/TRAILING
    expected_pnl: float
    expected_return: float
```

#### 2.2.6 StrategyGraduator (策略晋级器)

**职责**: 模拟→实盘晋级管理

**晋级规则**:
- **晋级条件**: 连续 3 天收益率>5% → 从 PAPER 切换到 HYBRID/LIVE
- **降级条件**: 连续 2 天亏损 → 从 LIVE 降级到 PAPER
- **状态持久化**: SQLite 存储晋级历史

**实现**:
```python
class StrategyGraduator:
    def evaluate_performance(self, code: str, daily_return: float)
    def get_recommended_mode(self, code: str) -> TradingMode
```

#### 2.2.7 ReportGenerator (报告生成器)

**职责**: 生成每日/每周交易报告

**日报内容**:
- 当日交易明细 (买入/卖出)
- 当日收益 (绝对值 + 百分比)
- 当前持仓状态
- 累计收益 (模拟期从启动日开始)

**周报内容**:
- 周度交易汇总
- 周度收益率
- 胜率 (盈利交易数/总交易数)
- 最大回撤
- 夏普比率 (可选)

**推送时间**:
- **日报**: 每个交易日收盘后 30 分钟
  - 港股: 16:30 HKT
  - 美股: 次日 04:30 EST
- **周报**: 周五/周日收盘后

---

## 3. 数据流

### 3.1 信号处理流程

```
1. 策略生成信号 (fusion_v2.py)
        ↓
2. DecisionEngine.process_signal(signal)
        ↓
3. SignalRateLimiter.should_allow(code, action)
   ├─ 拒绝 → 记录日志，丢弃信号
   └─ 允许 → 继续
        ↓
4. MarketSessionManager.is_trading_session(code)
   ├─ 非交易时段 → 加入等待队列
   └─ 交易时段 → 继续
        ↓
5. DecisionEngine.get_mode(code)
   ├─ PAPER → PaperTradingEngine.execute()
   ├─ HYBRID → (confidence>0.8 ? Live : Paper)
   └─ LIVE → LiveTrader.execute()
        ↓
6. AutoExitManager.check_exit(position)
   ├─ 触发退出 → 执行卖出
   └─ 未触发 → 继续持仓
        ↓
7. ReportGenerator.record_trade(trade)
```

### 3.2 定时任务

```yaml
cron_jobs:
  - name: daily_report_hk
    schedule: "30 16 * * 1-5"  # 港股收盘后 30 分钟
    timezone: Asia/Shanghai
    action: generate_daily_report(0700.HK)
    
  - name: daily_report_us
    schedule: "30 4 * * 1-5"  # 美股收盘后 30 分钟
    timezone: America/New_York
    action: generate_daily_report(MU)
    
  - name: weekly_report
    schedule: "0 20 * * 5"  # 周五晚 8 点
    timezone: Asia/Shanghai
    action: generate_weekly_report()
    
  - name: performance_check
    schedule: "0 */1 * * *"  # 每小时检查
    action: check_exit_conditions()
```

---

## 4. 配置设计

### 4.1 配置文件位置

`/opt/ftseek/FTSeek/config/trading_decision.yaml`

### 4.2 配置示例

```yaml
# 交易决策引擎配置

# 决策模式 (per stock)
modes:
  "0700.HK": "PAPER"  # 腾讯：模拟模式
  "MU": "PAPER"       # MU: 模拟模式

# 限流配置
rate_limit:
  max_signals_per_hour: 2
  cooldown_minutes: 15
  global_enabled: true

# 退出策略
exit:
  target_profit: 0.05      # 5% 止盈
  stop_loss: 0.03          # 3% 止损
  timeout_hours: 24        # 24 小时超时
  trailing_stop:
    enabled: false
    drawdown: 0.02         # 2% 回撤

# 晋级规则
graduation:
  promotion_days: 3        # 连续 3 天盈利晋级
  promotion_threshold: 0.05
  demotion_days: 2         # 连续 2 天亏损降级

# 市场时段
sessions:
  hk:
    timezone: Asia/Shanghai
    morning: "09:30-12:00"
    afternoon: "13:00-16:00"
  us:
    timezone: America/New_York
    regular: "21:30-04:00"

# 报告推送
reports:
  daily:
    enabled: true
    time_hk: "16:30"
    time_us: "04:30"
  weekly:
    enabled: true
    day: "friday"
    time: "20:00"
  dingtalk_webhook: "${DINGTALK_WEBHOOK}"
```

---

## 5. 数据库设计

### 5.1 表结构

```sql
-- 交易模式表
CREATE TABLE trading_modes (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE,
    mode TEXT,  -- PAPER/HYBRID/LIVE
    updated_at DATETIME
);

-- 模拟持仓表
CREATE TABLE paper_positions (
    id INTEGER PRIMARY KEY,
    code TEXT,
    quantity INTEGER,
    avg_cost REAL,
    entry_time DATETIME,
    last_update DATETIME
);

-- 交易记录表
CREATE TABLE paper_trades (
    id INTEGER PRIMARY KEY,
    code TEXT,
    action TEXT,  -- BUY/SELL
    quantity INTEGER,
    price REAL,
    timestamp DATETIME,
    pnl REAL,
    return_pct REAL
);

-- 绩效统计表
CREATE TABLE performance_stats (
    id INTEGER PRIMARY KEY,
    code TEXT,
    date DATE,
    daily_return REAL,
    cumulative_return REAL,
    trade_count INTEGER,
    win_count INTEGER,
    created_at DATETIME
);

-- 晋级历史表
CREATE TABLE graduation_history (
    id INTEGER PRIMARY KEY,
    code TEXT,
    from_mode TEXT,
    to_mode TEXT,
    reason TEXT,
    created_at DATETIME
);
```

---

## 6. API 设计

### 6.1 内部 API

```python
# 决策引擎
POST /api/v2/decision/process
{
    "code": "0700.HK",
    "action": "BUY",
    "price": 380.5,
    "quantity": 100,
    "confidence": 0.75,
    "strategy": "fusion_v2"
}

# 查询持仓
GET /api/v2/positions/{code}

# 查询绩效
GET /api/v2/performance/{code}?start=2026-03-01&end=2026-03-12

# 切换模式
POST /api/v2/mode/{code}
{
    "mode": "LIVE"
}
```

### 6.2 钉钉推送格式

**日报模板**:
```
### 📊 交易日报 - 2026-03-12

**腾讯 (0700.HK)**
├─ 模式：模拟
├─ 当日交易：2 笔 (1 买 1 卖)
├─ 当日收益：+¥1,250 (+3.2%)
├─ 累计收益：+¥5,680 (+5.8%) ✅
└─ 当前持仓：100 股 @ ¥385.2

**MU (美股)**
├─ 模式：模拟
├─ 当日交易：1 笔 (1 买)
├─ 当日收益：+$0 (持仓中)
├─ 累计收益：+$0 (新建仓)
└─ 当前持仓：50 股 @ $85.3

**汇总**
├─ 总收益：¥5,680 + $0
└─ 目标进度：58% (目标 5%)
```

**周报模板**:
```
### 📈 交易周报 - 2026-W11

**腾讯 (0700.HK)**
├─ 周收益率：+8.5%
├─ 交易次数：5 笔
├─ 胜率：80% (4/5)
├─ 最大回撤：-2.1%
└─ 夏普比率：1.85

**MU (美股)**
├─ 周收益率：+3.2%
├─ 交易次数：3 笔
├─ 胜率：67% (2/3)
├─ 最大回撤：-1.5%
└─ 夏普比率：1.42

**建议**
├─ 腾讯：保持 PAPER 模式 (连续 3 天>5%)
└─ MU: 保持 PAPER 模式 (观察中)
```

---

## 7. 测试策略

### 7.1 单元测试

- `test_decision_engine.py` - 决策路由逻辑
- `test_rate_limiter.py` - 限流规则
- `test_session_manager.py` - 时段判断
- `test_auto_exit.py` - 止盈止损触发
- `test_graduator.py` - 晋级降级逻辑

### 7.2 集成测试

- `test_paper_trading_flow.py` - 完整模拟交易流程
- `test_report_generation.py` - 报告生成和推送

### 7.3 回测验证

- 使用历史数据回测 3 个月
- 验证 5% 收益目标可行性
- 优化参数 (止盈/止损阈值)

---

## 8. 部署计划

### 8.1 依赖项

- 现有：Futu OpenD, DingTalk webhook, SQLite
- 新增：`pytz` (时区管理), `croniter` (cron 解析)

### 8.2 迁移步骤

1. 创建新模块 (不改动现有代码)
2. 并行运行 (现有信号流 + 新决策引擎)
3. 切换流量 (配置路由到新引擎)
4. 观察 1 周，验证稳定性
5. 清理旧代码 (可选)

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 模拟与实盘滑点差异 | 中 | 模拟时添加 0.5% 滑点 |
| 时段判断错误 | 高 | 单元测试覆盖所有时段边界 |
| 报告推送失败 | 低 | 失败重试 + 日志记录 |
| 数据库锁死 | 中 | 使用 WAL 模式，添加超时 |

---

## 10. 验收标准

- [ ] 信号频率≤2 个/小时/股票
- [ ] 模拟交易成交延迟<1 秒
- [ ] 止盈止损触发准确率 100%
- [ ] 日报/周报准时推送 (误差<5 分钟)
- [ ] 连续 3 天收益>5% 自动晋级
- [ ] 港股/美股独立运行，互不干扰

---

## 11. 已决策事项 (2026-03-12 审批通过)

| 事项 | 决策 |
|------|------|
| 滑点设置 | ✅ 添加 0.5% 滑点 (模拟买入价上浮 0.5%，卖出价下浮 0.5%) |
| 止损阈值 | ✅ 3% 止损合理，保持不变 |
| 周报时间 | ✅ 周日晚 20:00 推送 |
| 实盘切换 | ✅ **必须人工确认**，禁用自动晋级 (保留通知功能) |

### 设计调整

**StrategyGraduator 修改**:
- 移除自动切换模式功能
- 保留绩效评估和通知功能
- 当连续 3 天收益>5% 时，发送钉钉通知建议切换到实盘
- 用户通过 API 或钉钉指令手动切换模式

**配置调整**:
```yaml
graduation:
  auto_switch: false  # 禁用自动切换
  notify_on_promotion: true  # 达到晋级条件时通知
  notify_channel: dingtalk
```

---

**下一步**: 审批通过，进入 Phase 2 (编写实施计划)
