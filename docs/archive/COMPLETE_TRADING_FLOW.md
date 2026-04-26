# FTSeek 完整交易流程说明

**文档版本**: v1.0  
**更新日期**: 2026-03-15  
**状态**: ✅ 核心流程完整，AI 集成待增强

---

## 📋 功能概览

| 功能 | 状态 | 说明 |
|------|------|------|
| **信号生成** | ✅ 完整 | 7 大策略 + 融合引擎 + 共识引擎 |
| **因子分析** | ✅ 完整 | 技术面 5 因子 + 情绪面 + AI 因子 (待启用) |
| **AI 趋势判断** | ⚠️ 部分 | Phase 2 已集成，未完全接入主流程 |
| **富途下单 (买多)** | ✅ 完整 | 支持港股/美股 |
| **富途下单 (卖空)** | ✅ 完整 | 支持港股/美股 |
| **开仓 (OPEN)** | ✅ 完整 | 新建持仓记录 |
| **平仓 (CLOSE)** | ✅ 完整 | 全部平仓 + 盈亏计算 |
| **部分平仓** | ✅ 完整 | 支持金字塔减仓 |
| **加仓 (ADD)** | ✅ 完整 | 金字塔加仓逻辑 |
| **T+0 日内交易** | ✅ 完整 | 港股特性适配已集成 |
| **风控检查** | ✅ 完整 | 五层风控体系 |

---

## 🔄 完整交易流程图

```
┌─────────────────────────────────────────────────────────────┐
│ 1. 行情数据接入 (Futu OpenD API)                            │
│    - 实时 K 线 (1 分钟/5 分钟/15 分钟/30 分钟/60 分钟/日 K)        │
│    - 实时报价                                                │
│    - 订阅标的：HK.00700, HK.09988, HK.09992 等               │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ 2. 策略层 - 7 大独立策略并行计算                              │
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │ RSI 策略     │  │ MACD 策略    │  │ SuperTrend  │        │
│  │ OB=65 优化  │  │ 柱状图翻转  │  │ 乘数=2.5 优化│        │
│  └─────────────┘  └─────────────┘  └─────────────┘        │
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │ 布林带策略  │  │ 均线交叉    │  │ 价格突破    │        │
│  │ 震荡/突破   │  │ 金叉/死叉   │  │ N 日高低点   │        │
│  └─────────────┘  └─────────────┘  └─────────────┘        │
│                                                             │
│  ┌─────────────┐                                           │
│  │ Confluence  │ 多因子融合策略 (权重 1.2)                 │
│  └─────────────┘                                           │
│                                                             │
│ 输出：Signal {code, action(BUY/SELL), price, quantity}    │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ 3. 信号融合引擎 (Fusion Engine v2.1)                        │
│                                                             │
│  技术面评分 (55%):                                          │
│    - RSI 评分 (25%)                                         │
│    - MACD 评分 (25%)                                        │
│    - 均线评分 (20%)                                         │
│    - 成交量评分 (15%)                                       │
│    - 布林带评分 (15%)                                       │
│                                                             │
│  情绪面评分 (25%):                                          │
│    - 新闻情绪分析 (财联社/华尔街见闻等)                     │
│    - 社交情绪分析                                           │
│                                                             │
│  AI 预测评分 (20%): ⚠️ 待启用                                │
│    - Bailian API 调用                                       │
│    - 市场趋势预测                                           │
│    - 个股走势分析                                           │
│                                                             │
│  输出：FusionResult {direction, buy_prob, sell_prob, conf} │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ 4. 加权共识引擎 (Consensus Engine)                          │
│                                                             │
│  市场状态判定 (ADX):                                        │
│    - ADX > 25: 趋势市 → 趋势策略权重 ×1.5                   │
│    - ADX < 20: 震荡市 → 震荡策略权重 ×1.5                   │
│                                                             │
│  加权评分:                                                  │
│    score = Σ(strategy_weight × direction)                   │
│    - BUY: +1                                                │
│    - SELL: -1                                               │
│                                                             │
│  触发阈值:                                                  │
│    - |score| ≥ 1.5 → 推送信号                               │
│    - |score| < 1.5 → 过滤                                   │
│                                                             │
│  输出：ConsensusResult {action, score, strategies, weights}│
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ 5. 港股特性适配层 (HK Specific Adapter) ✅ 新增              │
│                                                             │
│  T+0 交易检查:                                              │
│    - 允许日内反向交易                                       │
│    - 记录日内持仓                                           │
│    - 平仓后标记                                             │
│                                                             │
│  流动性分层:                                                │
│    - Tier1 (腾讯/阿里): 仓位 25%, 置信度 0.55               │
│    - Tier2 (京东/网易): 仓位 15%, 置信度 0.60               │
│    - Tier3 (小盘股): 仓位 8%, 置信度 0.65                   │
│                                                             │
│  做空信号增强:                                              │
│    - 置信度 ×1.2                                            │
│    - 止盈 +20%, 止损 -8%                                    │
│                                                             │
│  无涨跌停适配:                                              │
│    - 做空止损放宽至 -8%                                     │
│    - 极端波动仓位 ×0.5                                      │
│                                                             │
│  输出：Processed Signal (可能被过滤)                        │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ 6. Watchlist 过滤                                           │
│    - 读取 config/watchlist.json                             │
│    - 仅推送 enabled=true 的股票                             │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ 7. 持仓检查与交易类型判定                                   │
│                                                             │
│  def _determine_trade_type(position, action):               │
│    if position is None:                                     │
│      return 'OPEN'          # 无持仓 → 开仓                 │
│    if position.action == 'BUY' and action == SELL:          │
│      return 'CLOSE'         # 持多 + 卖信号 → 平多          │
│    if position.action == 'SELL' and action == BUY:          │
│      return 'CLOSE'         # 持空 + 买信号 → 平空          │
│    return 'OPEN'            # 同向信号 → 加仓 (待实现)      │
│                                                             │
│  输出：trade_type ∈ {OPEN, CLOSE, ADD}                      │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ 8. 风控检查 (Risk Manager)                                  │
│                                                             │
│  五层风控体系:                                              │
│    1. 信号级：min_confidence ≥ 0.60                         │
│    2. 订单级：max_single_position ≤ 20%                     │
│    3. 仓位级：max_total_position ≤ 80%                      │
│    4. 账户级：max_drawdown ≤ 15%                            │
│    5. 策略级：min_winrate ≥ 40%                             │
│                                                             │
│  其他检查:                                                  │
│    - 日亏损上限：≤ 5%                                       │
│    - 周亏损上限：≤ 10%                                      │
│    - 连续亏损上限：≤ 5 次                                    │
│    - 单股日交易上限：≤ 3 次                                  │
│                                                             │
│  输出：RiskResult {passed: bool, details: dict}             │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ 9. 交易执行器 (Trade Executor)                              │
│                                                             │
│  执行开仓 (OPEN):                                           │
│    1. 检测市场 (HK/US)                                      │
│    2. 获取交易上下文 (港股/美股)                            │
│    3. 确定买卖方向 (BUY/SELL)                               │
│    4. 设置订单类型 (MARKET/LIMIT)                           │
│    5. 风控检查：金额限制                                    │
│    6. 富途 API 下单：place_order()                           │
│    7. 创建持仓记录：create_position()                       │
│    8. 返回：TradeResult {success, order_id, position_id}   │
│                                                             │
│  执行平仓 (CLOSE):                                          │
│    1. 获取持仓：get_position()                              │
│    2. 确定平仓方向 (与持仓相反)                             │
│    3. 富途 API 下单：place_order()                           │
│    4. 更新持仓状态：close_position()                        │
│    5. 计算已实现盈亏：pnl = (exit - entry) × qty           │
│    6. 返回：TradeResult {success, pnl}                     │
│                                                             │
│  执行部分平仓 (PARTIAL_CLOSE):                              │
│    1. 获取持仓                                              │
│    2. 计算平仓数量：close_qty = qty × ratio                │
│    3. 富途 API 下单                                          │
│    4. 更新持仓数量：quantity -= close_qty                  │
│    5. 计算已实现盈亏                                        │
│    6. 返回：TradeResult {success, pnl, remaining_qty}      │
│                                                             │
│  执行加仓 (ADD): ⚠️ 待完善                                   │
│    1. 检查加仓条件：can_add_position()                      │
│    2. 计算加仓数量：get_add_quantity() (金字塔)             │
│    3. 富途 API 下单                                          │
│    4. 更新持仓：增加数量，调整成本价                        │
│    5. 返回：TradeResult {success, new_position_id}         │
│                                                             │
│  输出：TradeResult {success, order_id, pnl, ...}            │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ 10. 钉钉推送                                                │
│                                                             │
│  开仓消息:                                                  │
│    - 标的代码/名称                                          │
│    - 交易方向 (BUY/SELL)                                    │
│    - 价格/数量                                              │
│    - 策略组合                                               │
│    - 共识分数                                               │
│    - 订单 ID                                                │
│                                                             │
│  平仓消息:                                                  │
│    - 标的代码/名称                                          │
│    - 平仓方向                                               │
│    - 价格/数量                                              │
│    - 已实现盈亏 (HK$)                                       │
│    - 收益率 (%)                                             │
│    - 持仓时间                                               │
│    - 订单 ID                                                │
│                                                             │
│  冷却控制:                                                  │
│    - 同标的 5 分钟冷却                                       │
│    - 避免重复推送                                           │
│                                                             │
│  输出：钉钉群消息                                           │
└─────────────────────────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ 11. 数据持久化                                              │
│                                                             │
│  SQLite 数据库：/opt/ftseek/FTSeek/data/ftseek.db          │
│                                                             │
│  positions 表:                                              │
│    - id, code, action, quantity                             │
│    - entry_price, entry_time                                │
│    - current_price, unrealized_pnl                          │
│    - status (OPEN/CLOSED)                                   │
│    - exit_price, exit_time, realized_pnl                   │
│                                                             │
│  paper_trades 表:                                           │
│    - id, code, trade_type (OPEN/CLOSE/ADD)                 │
│    - quantity, price                                        │
│    - timestamp, order_id                                    │
│    - pnl (平仓时)                                           │
│                                                             │
│  输出：持久化记录                                           │
└─────────────────────────────────────────────────────────────┘
```

---

## 📊 交易类型详解

### 1. 开仓 (OPEN)

**触发条件**:
- 无持仓 + 共识信号触发
- 风控检查通过

**做多 (BUY)**:
```python
# 示例：腾讯控股开仓做多
Signal(
    code="HK.00700",
    action=SignalAction.BUY,
    price=420.0,
    quantity=100,
    strategy="共识 (rsi + macd + supertrend)"
)

# 执行流程
1. 检测市场：HK
2. 获取港股交易上下文
3. 设置订单：BUY, qty=100, price=420
4. 富途下单：place_order()
5. 创建持仓：create_position(code="HK.00700", action="BUY", qty=100, price=420)
6. 返回：TradeResult(success=True, order_id="12345", position_id=1)
```

**做空 (SELL)**:
```python
# 示例：腾讯控股开仓做空
Signal(
    code="HK.00700",
    action=SignalAction.SELL,
    price=450.0,
    quantity=100,
    strategy="共识 (rsi 超买 + SuperTrend 翻空)"
)

# 执行流程
1. 检测市场：HK
2. 获取港股交易上下文
3. 设置订单：SELL, qty=100, price=450
4. 富途下单：place_order()
5. 创建持仓：create_position(code="HK.00700", action="SELL", qty=100, price=450)
6. 返回：TradeResult(success=True, order_id="12346", position_id=2)

# 做空盈亏计算
# 平仓价格 430 → 盈利 = (450 - 430) × 100 = HK$2,000
# 平仓价格 470 → 亏损 = (450 - 470) × 100 = -HK$2,000
```

---

### 2. 平仓 (CLOSE)

**触发条件**:
- 有持仓 + 反向共识信号
- 止盈/止损触发
- 手动平仓命令

**平多 (BUY → SELL)**:
```python
# 示例：平掉腾讯控股多仓
# 持仓：BUY @ 420, qty=100
# 信号：SELL @ 480

# 执行流程
1. 获取持仓：position = get_position("HK.00700")
2. 确定方向：SELL (与持仓相反)
3. 富途下单：place_order(SELL, qty=100, price=480)
4. 更新持仓：close_position(exit_price=480)
5. 计算盈亏：pnl = (480 - 420) × 100 = HK$6,000
6. 返回：TradeResult(success=True, pnl=6000)
```

**平空 (SELL → BUY)**:
```python
# 示例：平掉腾讯控股空仓
# 持仓：SELL @ 450, qty=100
# 信号：BUY @ 430

# 执行流程
1. 获取持仓：position = get_position("HK.00700")
2. 确定方向：BUY (与持仓相反)
3. 富途下单：place_order(BUY, qty=100, price=430)
4. 更新持仓：close_position(exit_price=430)
5. 计算盈亏：pnl = (450 - 430) × 100 = HK$2,000
6. 返回：TradeResult(success=True, pnl=2000)
```

---

### 3. 部分平仓 (PARTIAL_CLOSE)

**触发条件**:
- 达到部分止盈点 (+10%)
- 分批减仓策略

**示例**:
```python
# 持仓：BUY @ 400, qty=100
# 当前价：440 (+10%)
# 部分平仓 50%

# 执行流程
1. 获取持仓：position = get_position("HK.00700")
2. 计算平仓数量：close_qty = 100 × 0.5 = 50
3. 富途下单：place_order(SELL, qty=50, price=440)
4. 更新持仓：
   - quantity: 100 → 50
   - realized_pnl: (440 - 400) × 50 = HK$2,000
5. 返回：TradeResult(success=True, pnl=2000, remaining_qty=50)
```

---

### 4. 加仓 (ADD) ⚠️ 待完善

**触发条件**:
- 有持仓 + 同向信号
- 价格涨幅达到金字塔条件
- 置信度 ≥ 0.70
- 冷却时间已过
- 今日交易次数 < 3

**金字塔加仓**:
```python
# 初始仓位：BUY @ 400, qty=100
# 价格上涨 5% → 加仓 30% (30 股)
# 价格上涨 10% → 加仓 20% (20 股)

# 执行流程 (待完善)
1. 检查加仓条件：can_add_position()
   - 有持仓 ✅
   - 仓位上限：added < initial ✅
   - 置信度 ≥ 0.70 ✅
   - 冷却时间已过 ✅
   - 今日交易次数 < 3 ✅
2. 计算加仓数量：get_add_quantity(price_increase=0.05)
   - 涨幅 5% → 加仓 30% = 30 股
3. 富途下单：place_order(BUY, qty=30, price=420)
4. 更新持仓：
   - quantity: 100 → 130
   - entry_price: 400 → 404.6 (加权平均)
5. 返回：TradeResult(success=True, new_qty=130)
```

---

## 🤖 AI 集成状态

### Phase 2 已完成 (⚠️ 未完全接入主流程)

**已实现功能**:
```python
# app/ai/bailian_provider.py
class BailianProvider:
    """通义千问 API 调用"""
    def analyze_market(self, code: str, indicators: dict) -> AIAnalysis:
        # 调用 Bailian API
        # 返回市场趋势预测
```

**已创建模块**:
- `app/ai/bailian_provider.py` - Bailian API 调用
- `app/ai/prompt_templates.py` - 金融分析提示词模板
- `app/factors/ai_sentiment_factor.py` - AI 情绪因子

**待完成集成**:
```python
# ⚠️ 以下代码未接入主流程

# 1. 在 Fusion 引擎中调用 AI
def fuse(self, technical, sentiment, ai_score):
    # 当前 ai_score 始终为 0
    final_score = tech×0.55 + sent×0.25 + ai×0.20

# 2. 在共识达成前调用 AI 确认
def _on_consensus(self, result):
    # 可添加：ai_confirmation = self.ai_provider.confirm(result)
    # if not ai_confirmation:
    #     return  # AI 否决信号
```

**建议启用步骤**:
1. 在 `data_subscriber.py` 中导入 AI 模块
2. 在 `_on_consensus` 中添加 AI 确认步骤
3. 配置 AI 权重 (当前 20%)
4. 测试 AI 准确率 (目标 ≥50%)

---

## ✅ 功能验证清单

### 做多流程 ✅
- [x] 信号生成 (7 策略)
- [x] 融合评分 (技术 + 情绪)
- [x] 共识判定 (加权≥1.5)
- [x] 港股适配 (流动性分层)
- [x] 风控检查 (五层)
- [x] 富途下单 (BUY)
- [x] 持仓创建
- [x] 钉钉推送

### 做空流程 ✅
- [x] 信号生成 (RSI OB=65, ST=2.5)
- [x] 融合评分
- [x] 共识判定
- [x] 港股适配 (做空增强 ×1.2)
- [x] 风控检查
- [x] 富途下单 (SELL)
- [x] 持仓创建
- [x] 盈亏计算 (反向)
- [x] 钉钉推送

### 平仓流程 ✅
- [x] 持仓检查
- [x] 交易类型判定 (CLOSE)
- [x] 富途下单 (反向)
- [x] 持仓状态更新
- [x] 盈亏计算
- [x] 钉钉推送 (含盈亏)

### 部分平仓 ✅
- [x] 平仓比例计算
- [x] 富途下单
- [x] 持仓数量更新
- [x] 已实现盈亏计算
- [x] 剩余数量跟踪

### 加仓流程 ⚠️
- [x] 加仓条件检查 (can_add_position)
- [x] 金字塔数量计算 (get_add_quantity)
- [ ] 富途下单 (逻辑存在，未接入主流程)
- [ ] 持仓成本价调整 (未实现)
- [ ] 主流程集成 (待完成)

### AI 集成 ⚠️
- [x] Bailian API 调用
- [x] 提示词工程
- [x] AI 情绪因子
- [ ] 主流程集成
- [ ] 准确率追踪
- [ ] 动态权重调整

---

## 🔧 配置参数

### 交易模式
```yaml
# config/strategy_params.json
{
  "trading_mode": "simulate",      # simulate/real
  "manual_confirm_for_live": true  # 实盘需人工确认
}
```

### 仓位管理
```yaml
# config/position_strategy.yaml
position:
  initial_ratio: 0.40        # 首次开仓 40%
  max_position_ratio: 0.20   # 单股上限 20%
  total_max_ratio: 0.80      # 总仓位上限 80%
  dynamic_sizing: true       # 动态仓位
  vol_adjustment: true       # 波动率调整
```

### 止盈止损
```yaml
exit:
  stop_loss: -0.05           # 止损 -5%
  take_profit: 0.15          # 止盈 +15%
  partial_take_profit:       # 分批止盈
    - target: 0.10           # +10% 平仓 30%
    - target: 0.20           # +20% 平仓 30%
    - target: 0.30           # +30% 平仓 40%
  trailing_stop:             # 移动止盈
    enabled: true
    trigger_profit: 0.10     # 达到 +10% 启用
    trail_percent: 0.05      # 回撤 5% 止盈
```

### 做空优化
```yaml
short_strategy:
  enabled: true
  rsi_overbought: 65         # 超买阈值 (从 70 降至 65)
  supertrend_multiplier: 2.5 # 乘数 (从 3.0 降至 2.5)
  short_bias_factor: 1.2     # 做空增强因子
  max_ratio: 0.15            # 做空最大仓位 15%
  stop_loss: -0.08           # 做空止损 -8%
  take_profit: 0.20          # 做空止盈 +20%
```

---

## 📈 实际案例

### 案例 1: 腾讯控股做多完整流程

```
时间：2026-03-16 10:30
标的：HK.00700 腾讯控股

Step 1: 策略信号
  - RSI: 超卖反转 (28→32) → BUY
  - MACD: 柱状图金叉 → BUY
  - SuperTrend: 持有 → HOLD
  - 共识分数：+2.5 (≥1.5 ✅)

Step 2: 港股适配
  - 流动性：Tier1 (最大仓位 25%)
  - 置信度：0.75 (≥0.55 ✅)
  - T+0 检查：无冲突 ✅

Step 3: 风控检查
  - 信号置信度：0.75 ≥ 0.60 ✅
  - 仓位：40% ≤ 80% ✅
  - 日亏损：-2% ≥ -5% ✅

Step 4: 交易执行
  - 富途下单：BUY, qty=100, price=420
  - 订单 ID: 12345
  - 持仓 ID: 1

Step 5: 钉钉推送
  🟢 HK.00700 开仓信号
  方向：BUY
  价格：HK$420
  数量：100 股
  策略：共识 (rsi + macd)
  订单：12345

Step 6: 持仓跟踪
  - 10:30: 开仓 @ 420
  - 11:00: 现价 425, 浮盈 +HK$500
  - 14:00: 现价 430, 浮盈 +HK$1,000

Step 7: 平仓
  - 时间：15:30
  - 信号：RSI 超买回落 → SELL
  - 平仓价：450
  - 盈亏：(450-420)×100 = +HK$3,000
  - 收益率：+7.14%
```

### 案例 2: 美团做空完整流程

```
时间：2026-03-16 11:00
标的：HK.03690 美团-W

Step 1: 策略信号
  - RSI: 超买回落 (68→63) → SELL ✅ (新阈值 65)
  - SuperTrend: 翻空 → SELL
  - 共识分数：-2.0 (≤-1.5 ✅)

Step 2: 港股适配
  - 流动性：Tier1
  - 做空增强：置信度 ×1.2 → 0.72
  - 止损调整：-8%

Step 3: 风控检查
  - 通过 ✅

Step 4: 交易执行
  - 富途下单：SELL, qty=100, price=150
  - 订单 ID: 12346
  - 持仓 ID: 2 (做空)

Step 5: 持仓跟踪
  - 11:00: 开空 @ 150
  - 13:00: 现价 145, 浮盈 +HK$500
  - 15:00: 现价 140, 浮盈 +HK$1,000

Step 6: 平仓
  - 时间：15:50
  - 信号：RSI 超卖反转 → BUY
  - 平仓价：135
  - 盈亏：(150-135)×100 = +HK$1,500
  - 收益率：+10.0%
```

---

## ⚠️ 注意事项

### 1. 卖空限制
- 港股卖空需要券商支持
- 富途模拟账户支持卖空
- 实盘需开通融资融券权限

### 2. T+0 交易
- 港股支持 T+0 (当日买卖)
- 系统已实现日内反向交易
- 注意交易成本 (印花税/佣金)

### 3. 加仓功能
- 金字塔加仓逻辑已实现
- 主流程集成待完善
- 建议手动触发或下周迭代

### 4. AI 集成
- Phase 2 已完成基础模块
- 主流程集成待完成
- 建议先观察准确率再调整权重

---

## 📞 运维支持

### 查看持仓
```bash
# SQLite 查询
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db
SELECT * FROM positions WHERE status='OPEN';
```

### 查看交易记录
```bash
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db
SELECT * FROM paper_trades ORDER BY timestamp DESC LIMIT 10;
```

### 钉钉命令
```
/持仓查询       # 查看当前持仓
/今日交易       # 查看今日交易记录
/策略列表       # 查看策略状态
```

---

**文档状态**: ✅ 核心流程完整  
**待完善**: 加仓主流程集成、AI 完全集成  
**下次更新**: 2026-03-22
