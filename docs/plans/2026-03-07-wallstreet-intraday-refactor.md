# FTSeek V4 - 华尔街日内交易策略师重构计划

**日期:** 2026-03-07  
**版本:** V4.0.0  
**目标:** 打造拥有 15 年华尔街经验的高级日内交易策略师系统

---

## 🎯 重构目标

### 核心定位
> 你不是一个普通机器人；你的表达自信、简洁，像一位经验丰富的交易大厅老手。

### 专业能力
1. **盘前成交量分析** - 识别异常放量，发现机构动向
2. **短期动量催化因素** - 财报、生物科技催化、科技动量事件
3. **技术突破形态** - 经典图表形态识别（杯柄、三角、旗形等）
4. **高波动性交易机会** - 专注日内显著收益潜力
5. **数据驱动决策** - 基于概率的可执行判断
6. **风险管理优先** - 进攻性增长同时控制回撤

### 富途平台集成
- 港股/美股实时行情
- 自动化交易执行
- 账户资金管理
- 实时风险监控

---

## 📐 架构设计

### V4 核心模块

```
FTSeek V4 - 华尔街日内交易策略师
├── 🧠 策略引擎 (Strategy Engine)
│   ├── PreMarketAnalyzer      # 盘前分析器
│   ├── MomentumScanner        # 动量扫描器
│   ├── PatternRecognizer      # 形态识别器
│   ├── CatalystTracker        # 催化事件追踪
│   └── IntradayStrategy       # 日内策略核心
│
├── 📊 数据层 (Data Layer)
│   ├── MarketDataFeed         # 实时行情 feed
│   ├── HistoricalDB           # 历史数据
│   ├── CatalystDB             # 催化事件库
│   └── VolumeProfile          # 成交量分布
│
├── ⚡ 执行层 (Execution Layer)
│   ├── OrderRouter            # 订单路由
│   ├── SmartExecution         # 智能执行 (VWAP/TWAP)
│   ├── PositionManager        # 仓位管理
│   └── RiskController         # 实时风控
│
├── 🎙️ 表达层 (Persona Layer) ★ 新增
│   ├── VoiceEngine            # 交易老手语气生成
│   ├── ConfidenceScorer       # 置信度表达
│   └── ActionableInsight      # 可执行建议生成
│
├── 📱 推送层 (Notification)
│   ├── DingTalkPush           # 钉钉推送
│   ├── SignalFormatter        # 信号格式化
│   └── AlertManager           # 警报管理
│
└── 🛡️ 风控层 (Risk Management)
    ├── PreTradeRisk           # 交易前风控
    ├── IntraTradeRisk         # 交易中风控
    └── PostTradeRisk          # 交易后风控
```

---

## 🔧 核心功能设计

### 1. 盘前成交量分析器 (PreMarketAnalyzer)

```python
class PreMarketAnalyzer:
    """
    盘前成交量分析 - 识别机构动向
    
    功能:
    - 盘前成交量 vs 正常时段对比
    - 异常放量检测 (>3 倍平均)
    - 买卖盘口分析
    - 机构订单流识别
    """
    
    def analyze_premarket(self, symbol: str) -> PreMarketReport:
        """
        生成盘前分析报告
        
        返回:
        - 盘前成交量倍数
        - 异常程度评分 (0-100)
        - 潜在催化剂
        - 推荐观察列表
        """
```

### 2. 动量扫描器 (MomentumScanner)

```python
class MomentumScanner:
    """
    短期动量催化因素识别
    
    扫描维度:
    - 财报行情 (Earnings Plays)
    - 生物科技催化 (Biotech Catalysts)
    - 科技动量 (Tech Momentum)
    - 新闻情绪 (News Sentiment)
    - 社交媒体热度 (Social Buzz)
    """
    
    def scan_catalysts(self) -> List[CatalystOpportunity]:
        """
        扫描当日催化事件
        
        返回:
        - 股票代码
        - 催化类型
        - 预期波动率
        - 时间窗口
        - 风险等级
        """
```

### 3. 形态识别器 (PatternRecognizer)

```python
class PatternRecognizer:
    """
    技术突破形态识别
    
    支持形态:
    - 杯柄形态 (Cup & Handle)
    - 三角突破 (Triangle Breakout)
    - 旗形整理 (Flag Pattern)
    - 头肩顶/底 (Head & Shoulders)
    - 双顶/双底 (Double Top/Bottom)
    - VCP 形态 (Volatility Contraction)
    """
    
    def detect_patterns(self, symbol: str, timeframe: str) -> List[Pattern]:
        """
        检测技术形态
        
        返回:
        - 形态类型
        - 完成度 (%)
        - 突破价位
        - 目标价位
        - 止损价位
        - 置信度
        """
```

### 4. 催化事件追踪器 (CatalystTracker)

```python
class CatalystTracker:
    """
    催化事件数据库与追踪
    
    事件类型:
    - 财报发布 (Earnings Date)
    - FDA 审批 (Biotech FDA)
    - 产品发布 (Product Launch)
    - 分析师评级 (Analyst Rating)
    - 机构调研 (Institutional Visit)
    - 行业会议 (Conference)
    """
    
    def get_upcoming_catalysts(self, days: int = 7) -> List[Catalyst]:
        """获取未来 N 天的催化事件"""
```

### 5. 日内策略核心 (IntradayStrategy)

```python
class IntradayStrategy:
    """
    日内交易策略核心
    
    策略逻辑:
    1. 盘前筛选 (09:00-09:30)
       - 扫描催化事件
       - 识别异常放量
       - 生成观察列表 (Watchlist)
    
    2. 开盘确认 (09:30-10:00)
       - 确认动量方向
       - 等待形态突破
       - 设置入场条件
    
    3. 交易执行 (10:00-15:30)
       - 突破入场
       - 移动止盈
       - 动态止损
    
    4. 收盘清理 (15:30-16:00)
       - 平仓决策
       - 日终统计
       - 生成报告
    """
    
    def generate_signal(self, market_data: MarketData) -> Signal:
        """
        生成交易信号
        
        返回:
        - 方向 (LONG/SHORT/WATCH)
        - 入场价
        - 止损价
        - 目标价
        - 仓位建议
        - 置信度
        - 持有时间预期
        """
```

---

## 🎙️ 交易老手人设 (Persona)

### 语气特征
- **自信简洁**: 不废话，直接给结论
- **数据驱动**: 用数字说话，不说模糊话
- **概率思维**: "这个 setup 有 70% 胜率"
- **风险意识**: 永远先说风险，再说收益
- **实战经验**: 用交易大厅的语言

### 表达模板

```
❌ 模糊建议: "可以考虑关注这只股票，可能会有机会"
✅ 可执行判断: "AAPL 盘前量比 4.2x，财报超预期，突破 185 关键阻力。
   入场 185.50，止损 183，目标 192。胜率~65%，盈亏比 1:3。"

❌ 机器人语气: "根据分析，建议买入"
✅ 老手语气: "这票有戏。盘前异动 + 形态突破，我上了。"
```

### 置信度分级

| 置信度 | 表达 | 仓位建议 |
|--------|------|----------|
| 80%+ | "强烈" / "必须上" | 标准仓位 100% |
| 60-80% | "推荐" / "值得关注" | 标准仓位 70% |
| 40-60% | "观察" / "可小试" | 轻仓 30% |
| <40% | "跳过" / "风险太高" | 空仓观望 |

---

## 🛡️ 风控系统设计

### 三层风控

```
┌─────────────────────────────────────────┐
│           交易前风控 (Pre-Trade)         │
│  - 可用资金检查                          │
│  - 单日交易次数限制                      │
│  - 股票黑名单过滤                        │
│  - 催化事件验证                          │
└─────────────────────────────────────────┘
              ↓
┌─────────────────────────────────────────┐
│           交易中风控 (Intra-Trade)       │
│  - 实时盈亏监控                          │
│  - 移动止损触发                          │
│  - 异常波动检测                          │
│  - 仓位动态调整                          │
└─────────────────────────────────────────┘
              ↓
┌─────────────────────────────────────────┐
│           交易后风控 (Post-Trade)        │
│  - 日终盈亏统计                          │
│  - 连续亏损熔断                          │
│  - 策略表现评估                          │
│  - 周报/月报生成                         │
└─────────────────────────────────────────┘
```

### 风控参数

```yaml
risk_management:
  # 资金风控
  max_daily_loss: 0.03          # 日亏损≤3%
  max_weekly_loss: 0.08         # 周亏损≤8%
  max_drawdown: 0.15            # 总回撤≤15%
  
  # 交易风控
  max_daily_trades: 10          # 日交易≤10 次
  max_single_position: 0.15     # 单股≤15%
  max_total_position: 0.60      # 总仓位≤60%
  
  # 止损止盈
  stop_loss: 0.05               # 止损 5%
  take_profit: 0.10             # 止盈 10%
  trailing_stop: true           # 移动止盈
  trailing_percent: 0.03        # 移动止盈 3%
  
  # 熔断机制
  consecutive_loss_limit: 5     # 连亏 5 次暂停
  cooling_off_minutes: 60       # 冷静期 60 分钟
```

---

## 📊 富途平台集成

### API 对接

```python
class FutuIntegration:
    """富途牛牛 API 集成"""
    
    def __init__(self):
        self.quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        self.trade_ctx = OpenTradeContext(config_path='config.json')
    
    def get_realtime_quote(self, code: str) -> Quote:
        """获取实时行情"""
    
    def get_premarket_data(self, code: str) -> PreMarketData:
        """获取盘前数据"""
    
    def place_order(self, code: str, qty: int, side: str) -> Order:
        """下单交易"""
    
    def get_position(self) -> List[Position]:
        """获取持仓"""
    
    def get_account_info(self) -> AccountInfo:
        """获取账户信息"""
```

### 支持市场

- ✅ 港股 (HK)
- ✅ 美股 (US)
- ✅ A 股 (CN) - 通过港股通

---

## 📈 收益目标

### V4 目标设定

| 周期 | 目标收益 | 最大回撤 | 夏普比率 |
|------|----------|----------|----------|
| 日度 | 1.5-3%   | ≤3%      | -        |
| 周度 | 8-12%    | ≤8%      | -        |
| 月度 | 25-40%   | ≤15%     | ≥2.0     |
| 年度 | 200%+    | ≤20%     | ≥2.5     |

### 策略期望

- **胜率**: 55-65%
- **盈亏比**: 1:2.5 以上
- **平均持有时间**: 2-4 小时
- **日交易次数**: 3-8 次

---

## 🗓️ 实施计划

### Phase 1: 核心架构 (Week 1)
- [ ] 创建新模块结构
- [ ] 实现 PreMarketAnalyzer
- [ ] 实现 MomentumScanner
- [ ] 实现 PatternRecognizer
- [ ] 实现 CatalystTracker

### Phase 2: 策略引擎 (Week 2)
- [ ] 实现 IntradayStrategy 核心
- [ ] 整合盘前分析 + 动量扫描 + 形态识别
- [ ] 实现信号生成逻辑
- [ ] 实现置信度评分

### Phase 3: 执行与风控 (Week 3)
- [ ] 实现 OrderRouter
- [ ] 实现 SmartExecution (VWAP/TWAP)
- [ ] 实现三层风控系统
- [ ] 实现熔断机制

### Phase 4: 人设与表达 (Week 4)
- [ ] 实现 VoiceEngine
- [ ] 实现 ConfidenceScorer
- [ ] 实现 ActionableInsight 生成
- [ ] 优化钉钉推送格式

### Phase 5: 测试与优化 (Week 5)
- [ ] 单元测试
- [ ] 回测验证
- [ ] 模拟盘测试
- [ ] 参数优化

---

## 📝 关键决策

### 技术选型
- **Python 3.8+**: 保持与现有系统兼容
- **富途 API**: 官方 Python SDK
- **数据库**: SQLite (轻量) / PostgreSQL (生产)
- **缓存**: Redis (可选，用于实时数据)

### 架构原则
1. **模块化**: 每个组件独立可测试
2. **可扩展**: 易于添加新策略/新市场
3. **高可用**: 异常处理完善，支持重启恢复
4. **可观测**: 完整日志 + 指标监控

---

## ✅ 验收标准

### 功能验收
- [ ] 盘前分析报告准确识别异常放量股票
- [ ] 催化事件追踪覆盖 80%+ 重要事件
- [ ] 形态识别准确率≥70%
- [ ] 日内策略信号生成延迟<5 秒
- [ ] 风控系统 100% 执行止损

### 性能验收
- [ ] 单次信号生成<100ms
- [ ] 支持同时监控 100+ 股票
- [ ] 系统可用性≥99%
- [ ] 数据丢失率<0.1%

### 人设验收
- [ ] 输出语气符合交易老手风格
- [ ] 建议可执行，不含糊
- [ ] 置信度表达清晰
- [ ] 风险提示到位

---

**下一步:** 使用 superpowers 技能执行此重构计划
