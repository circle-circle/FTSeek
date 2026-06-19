# FTSeek-US Trading Playbook System — Specification

**Version**: 2.0  
**Date**: 2026-06-18  
**Status**: Draft → Implementation  
**Changes from v1.0**: 基于代码库审计的优化 — IC 数据修正、Provider 复用、批量 LLM、Market Regime Gate、双层 Pattern 分析（贝叶斯）、固定条件枚举、持仓上下文注入、ExitMonitor 集成、Shadow 验证阶段

---

## 1. 系统定位

**一句话**: 每日交易决策系统 — 深度分析选股 → 出具明确交易指令 → 执行追踪 → 公式迭代

**核心理念**: 人做决策，系统做研究、纪律和记录

### 1.1 不是什么

| 不是 | 为什么 |
|------|--------|
| 全自动交易系统 | Signal Quality Scorer IC 为负（live IC=-0.047），自动交易=亏钱 |
| 条件等待系统 | 不是 IF price=118 THEN buy，是今天该做什么 |
| 结构化研报系统 | 不要"建议谨慎"，要 BUY/SELL/SHORT + 具体数字 |
| LLM 预测系统 | LLM 不做方向预测，只做数据整合和模式识别 |

### 1.2 是什么

```
盘前: 市场环境判断 → watchlist 筛选 Top 标的 → 批量深度分析 → 出具交易指令
盘中: 用户通过 Dashboard 手动执行 → ExitMonitor 智能管理止损止盈
盘后: 系统自动追踪每条指令结果 → 对比执行 vs 计划
周末: 双层因子分析 (单因子+组合) → 淘汰负收益条件 → 强化正收益条件
```

### 1.3 做减法：砍掉的模块

| 模块 | 砍的原因 |
|------|---------|
| Signal Quality Scorer (5维度) | IC 为负（live -0.047, sim -0.046），信号越强亏越多 |
| LLM 多 Agent 投票 | 投票喂给无效 scorer |
| 多策略并行 (5→0) | 4/5 IC≤0 |
| Battle Plan 8 阶段深度研究 | 4 LLM 并行+辩论，84s/次，命中率 0% |
| 盘前作战辩论 (双Agent) | 上游信号无效 |
| trend_first 策略 | IC=-0.24 |
| 自动交易 Pipeline | IC 门禁已拦截，保留基础设施但暂停自动下单 |

**保留观察**: LONG 方向信号 IC(T1)=+0.185, 胜率 60.8%，未来可考虑仅对 LONG 方向恢复部分自动化。manual_sync 策略 8 笔 87.5% 胜率，验证了"人做决策"的有效性。

### 1.4 保留的模块

| 模块 | 用途 | 实现位置 |
|------|------|---------|
| 市场数据管道 (LV3 + Futu Push) | 基础设施 | `app/data/` |
| Finnhub Provider | 基本面/新闻/K线 | `app/data/finnhub_provider.py` |
| SEC EDGAR Form4 Provider | 内部人交易 + cluster buy 检测 | `app/data/sec_form4_provider.py` |
| Earnings Calendar | 财报日期 + 行业 ATR 乘数 | `app/data/earnings_calendar.py` |
| Fundamental Provider | 基本面数据 | `app/data/providers/FundamentalProvider` |
| 持仓管理 + 对账 (position_sync) | 安全底线 | `app/positions/` |
| IC 门禁 | 守护资金（防止自动交易意外开启） | `app/decision/` |
| ExitMonitor (7+ 退出规则) | 智能止损/止盈/退出 | `app/risk/position_exit_monitor.py` |
| Dashboard 手动交易 | 人工执行通道 | `app/web_dashboard/` |
| ManualOrderService | 下单执行 | `app/web_dashboard/manual_order_service.py` |
| trade_records | 统一交易记录 | `app/storage/` |
| 飞书/钉钉通知 | 消息推送 | `app/notifier/` + `app/push/` |
| WatchlistManager | watchlist 管理 | `app/utils/watchlist_manager.py` |

---

## 2. 系统架构

### 2.1 四层架构

```
┌──────────────────────────────────────────────────────────┐
│  Layer 0: 市场环境门禁 (盘前 08:00 ET)                     │
│                                                          │
│  SPY 趋势 + VIX 水平 + 市场宽度 → RISK_ON/CAUTIOUS/OFF   │
│                                                          │
│  输出: 市场regime + 今日操作基调 + Top N 数量调整          │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│  Layer 1: 决策支持 (盘前 08:00-09:30 ET)                  │
│                                                          │
│  watchlist → 数据收集(复用现有Provider)                   │
│           → 规则筛选 → 批量LLM分析 → 交易指令             │
│                                                          │
│  输出: 1-5 条明确的 BUY/SELL/SHORT 指令                   │
│  格式: 标的+方向+入场价+止损+止盈+仓位+理由               │
│  约束: 1次批量LLM调用, ≤90s 总延迟                        │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│  Layer 2: 纪律执行 (盘中 09:30-16:00 ET)                  │
│                                                          │
│  Dashboard 展示指令 → 确认面板 → 用户手动下单             │
│           → ExitMonitor 智能管理止损止盈                  │
│                                                          │
│  能力: ExitMonitor 7+退出规则 + Broker STOP 最后防线       │
│  防护: 日内亏损熔断(%) + 连续亏损熔断 + PDT + 仓位上限    │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│  Layer 3: 迭代复盘 (盘后 16:30 ET + 每周五)               │
│                                                          │
│  每日: 指令追踪 (执行了没? 赚了吗? MFE/MAE?)             │
│  每周: 双层因子分析 (单因子贡献 + 条件对分析)             │
│  每月: 公式库更新 (淘汰/新增/调整) + watchlist 调整        │
└──────────────────────────────────────────────────────────┘
```

### 2.2 数据流

```
                    ┌─────────────────┐
                    │ Layer 0: 环境门禁 │
                    │ SPY + VIX + 宽度 │
                    └───────┬─────────┘
                            ▼ regime
                    ┌─────────────┐
                    │  watchlist   │ (unified_config.yaml)
                    │  ~28 只股票  │ WatchlistManager
                    └──────┬──────┘
                           ▼
              ┌────────────────────────┐
              │  Layer 1: 数据收集     │  ← 复用现有 Provider
              │                        │
              │  FundamentalProvider ──→ 基本面    │
              │  Futu K线 ────────────→ 技术面     │
              │  SECForm4Provider ────→ 内部人     │
              │  FinnhubProvider ─────→ 新闻/情绪  │
              │  EarningsCalendar ────→ 财报日历   │
              └────────────┬───────────┘
                           ▼
              ┌────────────────────────┐
              │  PlaybookScreener      │  ← 可配置权重
              │  规则筛选 Top N        │     + regime 调整
              │  + 行业集中度检查      │     + 持仓去重
              └────────────┬───────────┘
                           ▼
              ┌────────────────────────┐
              │  批量 LLM 分析 (1次)   │
              │                        │
              │  输入: Top N 标的       │
              │       + 6 维度数据/标的 │
              │       + 当前持仓上下文  │
              │       + 已知 pattern    │
              │  输出: 1-5 条交易指令   │
              │  约束: 固定条件枚举     │
              └────────────┬───────────┘
                           ▼
              ┌────────────────────────┐
              │  Dashboard 展示        │
              │                        │
              │  今日指令列表           │
              │  确认面板 + 风险提示    │
              │  [执行] [跳过+原因]     │
              └────────────┬───────────┘
                           ▼
              ┌────────────────────────┐
              │  用户确认执行          │
              │  (ManualOrderService)  │
              │                        │
              │  Futu 下单 (开仓)      │
              │  Broker STOP (最后防线)│
              │  ExitMonitor (主管理)  │
              └────────────┬───────────┘
                           ▼
              ┌────────────────────────┐
              │  Layer 3: 结果追踪     │
              │                        │
              │  playbook_instructions │
              │  playbook_factor_stats │
              │  playbook_patterns     │
              │  MFE/MAE 追踪          │
              │  用户反馈收集          │
              └────────────────────────┘
```

---

## 3. Layer 0: 市场环境门禁

### 3.1 Market Regime Gate

在选股之前，先判断整体市场环境。这不是 LLM 预测，是简单的规则判断。

```python
class MarketRegimeGate:
    """市场环境门禁 — 决定今天的操作基调"""
    
    def assess(self) -> MarketRegime:
        """
        输入:
          - SPY 趋势: MA20 vs MA50 关系
          - VIX 水平: 从 TwelveData 获取 (Finnhub 的 get_vix_estimate 只返回 20.0 stub)
          - 市场宽度: NYSE 涨跌比 (可选, Phase 2+)
          
        输出:
        """
        spy_ma20 = get_ma("US.SPY", 20)
        spy_ma50 = get_ma("US.SPY", 50)
        vix = get_vix_from_twelve_data()  # 不用 Finnhub stub
        
        # 评分
        score = 0
        if spy_ma20 > spy_ma50: score += 2     # 短期趋势向上
        else: score -= 2                         # 短期趋势向下
        if spy_price > spy_ma20: score += 1      # 价格在均线上方
        else: score -= 1
        
        if vix < 15: score += 2                  # 低波动, 利好
        elif vix < 25: score += 0                # 正常
        elif vix < 35: score -= 2                # 高波动, 谨慎
        else: score -= 4                         # 恐慌
        
        return self._classify(score)
    
    def _classify(self, score: int) -> MarketRegime:
        """
        | score | regime    | 行为                             |
        |-------|-----------|----------------------------------|
        | ≥ 3   | RISK_ON   | 正常操作, top_n=10, 正常仓位    |
        | 1~2   | CAUTIOUS  | 减仓操作, top_n=5, 仓位减半     |
        | -1~0  | RISK_OFF  | 防守为主, top_n=3, 仅 SHORT/SKIP|
        | ≤ -2  | NO_TRADE  | 全部 SKIP, 只追踪 shadow_pnl    |
        """
```

### 3.2 Regime 对后续流程的影响

| Regime | Screener top_n | 仓位倍率 | LLM Prompt 注入 |
|--------|---------------|---------|----------------|
| RISK_ON | 10 | 1.0x | "市场趋势向上，正常操作" |
| CAUTIOUS | 5 | 0.5x | "市场谨慎，缩小仓位，提高入场标准" |
| RISK_OFF | 3 | 0.3x | "市场风险偏高，防守为主，只做高确定性机会" |
| NO_TRADE | 0 | 0x | "极端环境，全部 SKIP" |

---

## 4. Layer 1: 决策支持

### 4.1 数据源规格 (复用现有 Provider)

| 维度 | Provider 类 | API Endpoint | 缓存 TTL | 状态 |
|------|------------|-------------|---------|------|
| **基本面** | `FundamentalProvider` | Finnhub `/stock/metric` + Yahoo | 24h | ✅ 已有 |
| **技术面** | Futu K线管道 | 5min + 日线 | 实时 | ✅ 已有 |
| **内部人** | `SECForm4Provider` | SEC EDGAR Form 4 + cluster buy | 1h | ✅ 已有 (含 cluster 评分) |
| **新闻** | `FinnhubProvider.get_company_news()` | `/company-news` 近 3 天 | 2h | ✅ 已有 |
| **财报** | `EarningsCalendar` | Alpha Vantage EARNINGS_CALENDAR | 24h | ✅ 已有 (含行业 ATR) |
| **情绪** | `FinnhubProvider` + 新增 | `/news-sentiment` (需新增) | 1h | ⚠️ 需扩展 |

**新增工作**: 仅 Finnhub `/news-sentiment` 端点需要在 `FinnhubProvider` 中新增一个方法。其余全部复用。

**SEC Form4 Provider 增强利用**: 现有 `detect_cluster_buys()` 已实现:
- ≥2 个独立内部人买入
- 每人 >$50K
- 排除 10b5-1 预设计划
- 综合评分 (金额×0.3 + 人数×0.4 + 时效×0.3)

这比 spec v1 简单的 buy_count/sell_count 强得多，直接使用 `InsiderClusterBuy.score`。

**每只股票 API 消耗**: ~3 Finnhub calls + Futu K线 (已有) + SEC (有缓存)
**Top 10 扫描**: ~30 Finnhub calls (free tier 60/min, 充裕)

### 4.2 数据收集模块 (编排层)

```python
class PlaybookDataCollector:
    """编排层: 组合调用现有 Provider, 不重写数据获取逻辑"""
    
    def __init__(self, 
                 fundamental_provider: FundamentalProvider,
                 finnhub_provider: FinnhubProvider,
                 sec_form4_provider: SECForm4Provider,
                 earnings_calendar: EarningsCalendar):
        self.fundamental = fundamental_provider
        self.finnhub = finnhub_provider
        self.sec = sec_form4_provider
        self.earnings = earnings_calendar
    
    def collect(self, symbol: str) -> PlaybookData:
        """收集单只股票的 6 维度数据, 并行调用"""
        return PlaybookData(
            symbol=symbol,
            fundamental=self._collect_fundamental(symbol),
            technical=self._collect_technical(symbol),
            insider=self._collect_insider(symbol),
            news=self._collect_news(symbol),
            earnings=self._collect_earnings(symbol),
            sentiment=self._collect_sentiment(symbol),
        )
    
    def collect_batch(self, symbols: List[str]) -> List[PlaybookData]:
        """批量收集, 使用 ThreadPoolExecutor 并行"""
        with ThreadPoolExecutor(max_workers=5) as pool:
            return list(pool.map(self.collect, symbols))
    
    def _collect_fundamental(self, symbol: str) -> FundamentalData:
        """
        委托 FundamentalProvider.get_data(symbol)
        
        返回:
          pe: float (TTM P/E)
          market_cap: float (百万美元)
          industry: str
          revenue_growth_yoy: float
          roe: float
          debt_to_equity: float
          52w_high: float
          52w_low: float
          source: "finnhub+yahoo"
          fetched_at: ISO timestamp
          quality: "ok" | "stale" | "partial" | "missing"
        """
    
    def _collect_technical(self, symbol: str) -> TechnicalData:
        """
        复用 Futu K线管道 (已有完整实现)
        
        返回:
          price: float (当前价)
          ma20: float
          ma50: float
          rsi_14: float
          macd_signal: str ("golden_cross" | "dead_cross" | "neutral")
          atr_14: float
          bollinger_position: str ("upper" | "middle" | "lower")
          volume_5d_avg_ratio: float (近5日成交量 / 20日均量)
          trend_5d: str ("up" | "down" | "flat")
          candles_5d: List[str] ("阳线" | "阴线")
          quality: "ok" | "missing"
        """
    
    def _collect_insider(self, symbol: str) -> InsiderData:
        """
        委托 SECForm4Provider
        
        增强: 使用 detect_cluster_buys() 的 cluster score (0-1)
        而非简单的 buy_count/sell_count
        
        返回:
          buy_count_30d: int
          sell_count_30d: int
          net_shares_30d: int
          cluster_score: float (0-1, 来自 SECForm4Provider)
          cluster_buyers: List[str] (多人买入时的买家列表)
          signal: str ("strong_buy" | "buy" | "neutral" | "sell")
          transactions: List[InsiderTransaction]
          quality: "ok" | "missing"
        """
    
    def _collect_news(self, symbol: str) -> NewsData:
        """
        委托 FinnhubProvider.get_company_news(symbol, days=3)
        
        返回:
          headlines: List[str] (最近 5 条)
          sentiment_score: float (-1.0 ~ 1.0, bag-of-words)
          source: "finnhub"
          fetched_at: ISO timestamp
          quality: "ok" | "missing"
        """
    
    def _collect_earnings(self, symbol: str) -> EarningsData:
        """
        委托 EarningsCalendar
        
        返回:
          next_date: Optional[str] (下次财报日期)
          days_until: Optional[int]
          history: List[EarningsRecord] (最近4季)
          beat_rate: float (超预期比例)
          sector_atr_multiplier: float (来自 EarningsCalendar 的行业 ATR 乘数)
          quality: "ok" | "missing"
        """
    
    def _collect_sentiment(self, symbol: str) -> SentimentData:
        """
        Finnhub /news-sentiment (新增端点) + VIX from TwelveData
        
        返回:
          news_sentiment: float (-1.0 ~ 1.0, Finnhub 原生)
          articles_count: int
          vix: float (当前 VIX, 来自 TwelveData 真实数据)
          quality: "ok" | "missing"
        """
```

### 4.3 数据质量降级策略

| 可用维度数 | 策略 |
|-----------|------|
| ≥ 4 维度 OK | 正常生成指令 |
| 3 维度 OK | 正常生成，但 confidence × 0.7 |
| 2 维度 OK | 生成指令，confidence × 0.4，标注 ⚠️ 数据不完整 |
| < 2 维度 OK | 直接 SKIP，原因: "数据不足" |

```python
def apply_quality_degradation(instruction, data_quality):
    ok_count = sum(1 for v in data_quality.values() if v == "ok")
    if ok_count >= 4:
        return instruction
    elif ok_count == 3:
        instruction.confidence = int(instruction.confidence * 0.7)
        instruction.risks.append("数据不完整(3/6维度), 置信度已下调")
    elif ok_count == 2:
        instruction.confidence = int(instruction.confidence * 0.4)
        instruction.risks.append("数据严重不完整(2/6维度), 建议观望")
    else:
        instruction.direction = "SKIP"
        instruction.skip_reason = f"数据不足({ok_count}/6维度可用)"
    return instruction
```

### 4.4 选股筛选 (Top N 筛选)

#### 4.4.1 可配置权重 (存入 config/playbook_screening.yaml)

```yaml
# config/playbook_screening.yaml
# 权重可被 Layer 3 的 Pattern 系统每周迭代调整

screening_factors:
  # --- 正信号 ---
  insider_cluster_buy:
    weight: 4          # SECForm4Provider cluster score > 0.5
    description: "内部人集群买入"
  insider_net_buy:
    weight: 2
    description: "内部人净买入(非集群)"
  rsi_oversold:
    weight: 2
    condition: "rsi_14 < 40"
    description: "RSI 超卖回调"
  above_ma20:
    weight: 2
    condition: "price > ma20"
    description: "价格在 20 日均线上方"
  macd_golden_cross:
    weight: 1
    description: "MACD 金叉"
  news_positive:
    weight: 1
    condition: "sentiment_score > 0"
    description: "近 3 天新闻情绪正面"
  earnings_beat_streak:
    weight: 1
    condition: "beat_rate > 0.5"
    description: "财报超预期率 > 50%"
  volume_expansion:
    weight: 1
    condition: "volume_5d_avg_ratio > 1.2"
    description: "成交量放大"
  pe_below_industry:
    weight: 1
    description: "PE 低于行业平均"
  
  # --- 负信号 (扣分) ---
  rsi_overbought:
    weight: -2
    condition: "rsi_14 > 70"
  insider_net_sell:
    weight: -2
  news_negative:
    weight: -1
    condition: "sentiment_score < -0.3"
  earnings_imminent:
    weight: -3
    condition: "days_until_earnings < 3"
    description: "财报 < 3 天, 波动太大"

# 权重调整记录 (Layer 3 每周更新)
weight_adjustments:
  - date: "2026-06-20"
    factor: "insider_cluster_buy"
    delta: +1
    reason: "周度分析: 该因子胜率 75%, profit_factor 1.8"
```

#### 4.4.2 筛选器实现

```python
class PlaybookScreener:
    """从 watchlist 筛选 Top N 候选"""
    
    def __init__(self, config_path="config/playbook_screening.yaml"):
        self.config = load_yaml(config_path)
    
    def screen(self, watchlist: List[str], regime: MarketRegime, 
               positions: List[OpenPosition], top_n: int = None) -> List[ScreenResult]:
        """
        筛选逻辑:
        
        0. 根据 regime 调整 top_n (RISK_ON=10, CAUTIOUS=5, RISK_OFF=3, NO_TRADE=0)
        1. 排除已有持仓 (避免重复)
        2. 排除 PDT 限制内的标的
        3. 排除 global_blacklist 中的标的
        4. 排除 self_managed_symbols (AMD, BABA, NVDA, AAPL 等已有独立管理)
        5. 排除财报 < 3 天的标的
        6. 行业集中度检查: 同行业已有 ≥2 持仓 → 降权 -3
        7. 按可配置权重打分排序
        8. 取 Top N
        """
        top_n = top_n or regime.top_n
        
        candidates = []
        for symbol in watchlist:
            if self._should_exclude(symbol, positions):
                continue
            
            data = quick_data_fetch(symbol)  # 轻量数据, 只取筛选所需字段
            score = self._calculate_score(data, positions)
            candidates.append(ScreenResult(symbol=symbol, score=score, factors=data))
        
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:top_n]
    
    def _should_exclude(self, symbol, positions):
        held = {p.symbol for p in positions}
        blacklist = get_global_blacklist()
        self_managed = get_self_managed_symbols()
        return symbol in held or symbol in blacklist or symbol in self_managed
    
    def _calculate_score(self, data, positions) -> int:
        score = 0
        for factor_name, factor_cfg in self.config['screening_factors'].items():
            if evaluate_condition(factor_cfg, data):
                score += factor_cfg['weight']
        
        # 行业集中度惩罚
        sector = get_sector(data.symbol)
        same_sector_positions = [p for p in positions if get_sector(p.symbol) == sector]
        if len(same_sector_positions) >= 2:
            score -= 3
        
        return score
```

### 4.5 LLM 交易指令生成 (批量调用)

**一次 LLM 调用，输入 Top N 标的的 6 维度数据 + 持仓上下文 + 已知 pattern，输出 1-5 条交易指令。**

#### 4.5.1 固定条件枚举表

LLM 生成的 condition tag 必须从以下固定枚举中选择，确保 Pattern 系统的一致性:

```python
CONDITION_TAXONOMY = {
    # 趋势类
    "above_ma20": "价格在 MA20 上方",
    "above_ma50": "价格在 MA50 下方",
    "below_ma20": "价格在 MA20 下方",
    "below_ma50": "价格在 MA50 下方",
    "ma_golden_cross": "MA20 上穿 MA50",
    "ma_dead_cross": "MA20 下穿 MA50",
    
    # 动量类
    "rsi_oversold": "RSI(14) < 40",
    "rsi_overbought": "RSI(14) > 70",
    "rsi_neutral_low": "RSI(14) 40-50",
    "macd_golden_cross": "MACD 金叉",
    "macd_dead_cross": "MACD 死叉",
    "bollinger_lower": "价格触及布林带下轨",
    "bollinger_upper": "价格触及布林带上轨",
    "bollinger_squeeze": "布林带收窄(突破前兆)",
    
    # 价值类
    "pe_below_industry": "PE 低于行业平均",
    "pe_above_industry": "PE 高于行业平均",
    "revenue_growth_high": "收入增长 > 20% YoY",
    "roe_high": "ROE > 15%",
    "near_52w_low": "当前价接近 52 周低点(10% 以内)",
    "near_52w_high": "当前价接近 52 周高点(10% 以内)",
    
    # 内部人
    "insider_cluster_buy": "多内部人集群买入(cluster_score > 0.5)",
    "insider_net_buy": "内部人净买入(非集群)",
    "insider_net_sell": "内部人净卖出",
    
    # 情绪/新闻
    "news_positive": "近 3 天新闻情绪正面",
    "news_negative": "近 3 天新闻情绪负面",
    "sentiment_bullish": "Finnhub 情绪分数看多",
    "sentiment_bearish": "Finnhub 情绪分数看空",
    
    # 量价类
    "volume_expansion": "成交量 > 1.2x 均量",
    "volume_contraction": "成交量 < 0.8x 均量",
    
    # 事件类
    "earnings_beat_streak": "连续 2+ 季超预期",
    "post_earnings_momentum": "财报后动量信号",
    "earnings_approaching": "财报 < 14 天",
}
```

#### 4.5.2 Prompt 设计

```
你是 FTSeek 交易分析系统。基于以下数据，从候选标的中选出最佳交易机会，生成 1-5 条交易指令。

## 市场环境
Regime: {regime} ({regime_description})
VIX: {vix}
SPY: ${spy_price} (MA20=${spy_ma20}, MA50=${spy_ma50})

## 当前持仓 ({position_count} 个, 总仓位 {total_exposure}%)
{positions_table}
  例:
  | 标的 | 方向 | 股数 | 入场价 | 现价 | 浮盈% | 行业 |
  | AMD  | BUY  | 50   | $155.2 | $158 | +1.8% | 半导体 |
  | LLY  | BUY  | 30   | $780.5 | $772 | -1.1% | 医疗 |

## 行业暴露
{sector_exposure}
  例: 半导体: 1 持仓 (8%), 医疗: 1 持仓 (6%), 剩余可用资金: $42,000

## 总资金: ${total_equity}

## 历史验证的正收益条件 (如有):
{active_patterns}
  例:
  - insider_cluster_buy + pe_below_industry: 胜率 72%, 平均 +$180 (12笔)
  - rsi_oversold + above_ma20: 胜率 65%, 平均 +$95 (8笔)

## 历史验证的负收益条件 (避免):
{retired_patterns}
  例:
  - macd_golden_cross + volume_expansion: 胜率 28%, 平均 -$140 (11笔)

---
{对每个候选标的, 重复以下结构}

### 标的 #{i}: {symbol} | 当前价: ${price}

基本面 (来源: Finnhub+Yahoo, {fetched_at})
PE: {pe}x (行业: {industry_pe}x) | 市值: ${market_cap}M | 行业: {industry}
收入增长: {revenue_growth}% | ROE: {roe}% | 52周: ${52w_high}/${52w_low}

技术面 (来源: Futu K线, 实时)
MA20: ${ma20} | MA50: ${ma50} | RSI(14): {rsi}
MACD: {macd_signal} | ATR(14): ${atr} | 布林: {bollinger}
近5日: {candles_5d} | 量比: {volume_ratio}x

内部人 (来源: SEC EDGAR, 30天)
买入: {buy_count}笔 | 卖出: {sell_count}笔 | 净买入: {net_shares}股
集群评分: {cluster_score} | 信号: {insider_signal}

新闻 (来源: Finnhub, 近3天)
{headlines}
情绪: {sentiment_score}

财报
下次: {next_earnings} ({days_until}天后) | 超预期率: {beat_rate}% | 行业ATR乘数: {sector_atr}

数据质量: {data_quality_summary}
---

## 规则
1. 从 {n} 个候选中选出 1-5 条最佳指令, 输出 BUY / SELL_SHORT / SKIP
2. 不选的标的输出 SKIP + 原因, 不需要勉强凑数
3. 如果环境 regime = NO_TRADE, 全部 SKIP
4. 入场价必须是具体数字 (基于当前价和技术支撑/阻力位)
5. 止损基于 ATR: 使用行业 ATR 乘数 (来自 EarningsCalendar), 正常 1.5-2x, 财报期用 sector_atr_multiplier
6. 止盈必须使 R:R ≥ 1.5:1
7. 仓位基于 ATR 风险: 风险金额 = 总资金 × {risk_pct}%, 股数 = 风险金额 / (入场价 - 止损价)
   仓位倍率根据 regime 调整: RISK_ON=1.0x, CAUTIOUS=0.5x, RISK_OFF=0.3x
8. 同行业最多 2 个持仓 (含已有持仓)
9. 每个判断必须引用具体数据 (不说"趋势向好"，说"20MA上方, RSI=42, MACD金叉")
10. 置信度 = 数据一致性评分 (几个维度同向) × 数据质量系数
11. conditions 必须从以下固定标签中选择:
{condition_taxonomy_list}

## 输出格式 (JSON Array):
[
  {
    "symbol": "US.NTES",
    "direction": "BUY" | "SELL_SHORT" | "SKIP",
    "skip_reason": "",
    "entry_price": 0.0,
    "stop_loss": 0.0,
    "take_profit": 0.0,
    "position_size": 0,
    "risk_amount": 0.0,
    "risk_reward": 0.0,
    "confidence": 0,
    "thesis": "",
    "conditions": ["insider_cluster_buy", "pe_below_industry"],
    "data_quality": {"fundamental": "ok", "technical": "ok", "insider": "ok", "news": "ok", "earnings": "ok", "sentiment": "ok"},
    "risks": [],
    "sector": "Communication Services"
  }
]
```

#### 4.5.3 JSON 校验和重试

```python
INSTRUCTION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["symbol", "direction", "conditions", "data_quality"],
        "properties": {
            "symbol": {"type": "string", "pattern": "^US\\.[A-Z]+$"},
            "direction": {"type": "string", "enum": ["BUY", "SELL_SHORT", "SKIP"]},
            "skip_reason": {"type": "string"},
            "entry_price": {"type": "number", "minimum": 0},
            "stop_loss": {"type": "number", "minimum": 0},
            "take_profit": {"type": "number", "minimum": 0},
            "position_size": {"type": "integer", "minimum": 0},
            "risk_amount": {"type": "number", "minimum": 0},
            "risk_reward": {"type": "number", "minimum": 0},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
            "thesis": {"type": "string"},
            "conditions": {
                "type": "array",
                "items": {"type": "string", "enum": list(CONDITION_TAXONOMY.keys())}
            },
            "data_quality": {"type": "object"},
            "risks": {"type": "array", "items": {"type": "string"}},
            "sector": {"type": "string"}
        }
    }
}

def generate_instructions(symbols, data_batch, context, max_retries=2):
    """批量生成, 带 JSON Schema 校验和重试"""
    import jsonschema
    
    for attempt in range(max_retries + 1):
        raw = call_llm(feature="playbook_generator", prompt=build_prompt(...))
        try:
            instructions = json.loads(raw)
            jsonschema.validate(instructions, INSTRUCTION_SCHEMA)
            
            # 业务规则校验
            for inst in instructions:
                if inst["direction"] != "SKIP":
                    assert inst["risk_reward"] >= 1.5, f"R:R {inst['risk_reward']} < 1.5"
                    assert inst["stop_loss"] > 0, "stop_loss 必须 > 0"
            
            # 数据质量降级
            for inst in instructions:
                apply_quality_degradation(inst, inst["data_quality"])
            
            return instructions
            
        except (json.JSONDecodeError, jsonschema.ValidationError, AssertionError) as e:
            if attempt < max_retries:
                logger.warning(f"LLM 输出校验失败 (attempt {attempt+1}): {e}, 重试中...")
            else:
                logger.error(f"LLM 输出校验最终失败: {e}")
                return [make_skip(s, f"LLM 输出异常: {e}") for s in symbols]
```

#### 4.5.4 输出示例

```json
[
  {
    "symbol": "US.NTES",
    "direction": "BUY",
    "entry_price": 119.50,
    "stop_loss": 114.80,
    "take_profit": 128.00,
    "position_size": 50,
    "risk_amount": 235.00,
    "risk_reward": 1.81,
    "confidence": 72,
    "thesis": "基本面低估(PE 22x<行业28x) + 内部人集群买入(CTO+VP, cluster=0.78) + 技术面回调至布林中轨(RSI=42)",
    "conditions": ["insider_cluster_buy", "pe_below_industry", "rsi_neutral_low", "bollinger_lower"],
    "data_quality": {"fundamental": "ok", "technical": "ok", "insider": "ok", "news": "ok", "earnings": "ok", "sentiment": "ok"},
    "risks": ["中概政策风险(低概率高影响)", "距财报58天(安全)"],
    "sector": "Communication Services"
  },
  {
    "symbol": "US.META",
    "direction": "SKIP",
    "skip_reason": "财报3天后, 波动风险过大",
    "conditions": ["earnings_approaching"],
    "data_quality": {"fundamental": "ok", "technical": "ok", "insider": "ok", "news": "ok", "earnings": "ok", "sentiment": "ok"},
    "sector": "Technology"
  }
]
```

### 4.6 Dashboard 展示

Battle Plan 页面改造为 **今日 Playbook** 视图：

```
┌─────────────────────────────────────────────────────────┐
│  📋 今日交易指令 (2026-06-18)     环境: RISK_ON 🟢      │
│                                           [刷新] [导出] │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  #1 US.NTES  BUY LONG  ✅ 置信度 72%                    │
│  入场 $119.50 | 止损 $114.80 | 止盈 $128.00 | 50股      │
│  R:R = 1.8:1 | 风险 $235 (0.3%)                         │
│  条件: 内部人集群买入 + PE低估 + RSI中性偏低             │
│  Pattern: insider_cluster_buy+pe_below → 历史胜率72%     │
│  [▶ 执行买入]  [👎 跳过]  [📊 详情]                    │
│                                                         │
│  #2 US.ARM   BUY LONG  ⚠️ 置信度 55%                    │
│  入场 $415.00 | 止损 $395.00 | 止盈 $445.00 | 8股       │
│  R:R = 1.5:1 | 风险 $160 (0.2%)                         │
│  条件: MACD金叉 + 放量 + 新闻正面                        │
│  ⚠️ 注意: 已有半导体持仓 1 个                           │
│  [▶ 执行买入]  [👎 跳过]  [📊 详情]                    │
│                                                         │
│  ─── 已跳过 ───                                         │
│  US.META SKIP: 财报3天内，波动太大                       │
│  US.ORCL SKIP: 内部人数据缺失 (2/6维度)                  │
│                                                         │
│  ─── 执行确认弹窗 (点击 [▶ 执行] 后) ───                │
│  ┌──────────────────────────────────────┐               │
│  │ 确认执行 US.NTES BUY LONG            │               │
│  │ 入场 $119.50 (现价 $120.10, +0.5%)   │               │
│  │ 止损 $114.80 (-3.9%) | 止盈 $128 (+7.1%)            │
│  │ 50 股 = $5,975 (占总资金 7.5%)       │               │
│  │ ⚠️ 已有中概持仓: BABA                │               │
│  │ [确认执行]  [取消]                    │               │
│  └──────────────────────────────────────┘               │
│                                                         │
│  ─── 跳过原因 (点击 [👎 跳过] 后) ───                  │
│  ┌──────────────────────────────────────┐               │
│  │ 跳过原因: [下拉选择/自由填写]        │               │
│  │ ○ 风险太大                           │               │
│  │ ○ 不了解该行业                        │               │
│  │ ○ 已有类似持仓                        │               │
│  │ ○ 资金不足                            │               │
│  │ ○ 其他: [_______]                    │               │
│  └──────────────────────────────────────┘               │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**用户反馈的价值**:
- 用户跳过的指令最终亏损 → 用户直觉正确，该 pattern 需要审视
- 用户跳过的指令最终盈利 → 系统发现了用户的盲点，可以在周报中提醒
- 这些反馈存入 `playbook_user_feedback` 表，作为定性数据积累

---

## 5. Layer 2: 纪律执行

### 5.1 现有能力 (保留)

| 能力 | 实现 | 状态 |
|------|------|------|
| 开仓后挂保护性 STOP | `place_protective_stop()` in executor.py | ✅ 已实现 |
| 手动平仓 | `close_position()` in executor.py | ✅ 已实现 |
| PDT 保护 | `pdt_trades` 表 + 检查 | ✅ 已实现 |
| 平仓金额上限 | `MAX_CLOSE_VALUE_USD` ($200K) | ✅ 已实现 |
| ManualHoldRegistry | 防止自动管线误平手动持仓 | ✅ 已实现 |

### 5.2 ExitMonitor 集成 (核心改动)

**原则**: ExitMonitor (2316行, 7+ 退出规则) 作为止损止盈的**主管理器**, Broker STOP 单仅作为**最后防线**。

```
用户点击 [▶ 执行] → 确认面板 → 确认
    ↓
ManualOrderService.submit_order()
    ├── Futu 下市价/限价单 (开仓)
    ├── Futu 下 STOP 单 (硬止损, 仅实盘, 作为最后防线)
    │   └── 止损价 = instruction.stop_loss (绝对底线, 不可放宽)
    ├── 写入 trade_records (source_type=MANUAL_PLAYBOOK)
    ├── 写入 playbook_instructions (executed=1, execution_price, execution_time)
    └── 注册到 ExitMonitor (使用 playbook exit profile)

ExitMonitor 盘中管理 (每 60s 扫描)
    ├── ATR Trailing Stop: 初始 4.0x ATR → Chandelier 2.5x
    ├── Scale Out (分批止盈):
    │   ├── 浮盈 5% → 平 1/3
    │   ├── 浮盈 10% → 平 1/3
    │   └── 剩余 → 移动止损管理
    ├── Breakeven Stop: 浮盈 > 1.0x ATR → 止损上移至成本价
    ├── Flash Exit: 60s 急跌 > 1.0x ATR → 立即平仓
    ├── Time Stop: 持仓 > 72h → 评估是否平仓
    ├── Pre-Close Exit: 15:45 ET 评估尾盘退出
    └── Portfolio Drawdown: 组合回撤 > 2% → 减仓/全平

盘后
    └── playbook_instructions 更新: outcome, pnl, MFE, MAE
```

**为什么用 ExitMonitor 而非纯 Broker 单**:
- Broker STOP 单是固定价格，无法做 trailing stop
- Broker 无法做分批止盈 (scale-out)
- ExitMonitor 已有 regime-aware ATR 乘数
- ExitMonitor 已有 cooldown、grace period 等防抖机制

### 5.3 新增能力 (Phase 2 实施)

| 能力 | 描述 | 优先级 |
|------|------|--------|
| **仓位硬上限** | 单票 ≤ 10% 总资金, 总仓位 ≤ 80% | P0 |
| **分批止盈** | 由 ExitMonitor Scale Out 规则管理 (5%/10%/18%) | P1 |
| **移动止损** | 由 ExitMonitor Trailing Stop 管理 (ATR-based) | P1 |
| **日内亏损熔断** | 当日亏损 > 总资金 1% → Dashboard 开仓按钮灰掉 + 飞书告警 | P1 |
| **连续亏损熔断** | 连续 3 笔亏损 → 仓位减半; 连续 5 笔 → 停止开仓 1 天 | P1 |
| **价格预警** | 接近止损 (-1%) / 接近止盈 (-1%) → 飞书推送 | P2 |

### 5.4 风控参数

```python
# config/playbook_risk.yaml
risk_limits:
  max_position_pct: 0.10          # 单票 ≤ 10% 总资金
  max_total_exposure_pct: 0.80    # 总仓位 ≤ 80%
  max_same_sector: 2              # 同行业最多 2 持仓
  risk_per_trade_pct: 0.01        # 单笔风险 = 1% 总资金
  daily_loss_limit_pct: 0.01      # 日内亏损上限 = 1% 总资金 (非固定 $500)
  consecutive_loss_reduce: 3      # 连续 N 笔亏损 → 仓位减半
  consecutive_loss_stop: 5        # 连续 N 笔亏损 → 停止开仓 1 天
  min_rr_ratio: 1.5               # 最低 R:R
  max_daily_orders: 10            # 每日最多 10 笔 (ManualOrderService 已有 20 上限)
```

---

## 6. Layer 3: 迭代复盘

### 6.1 数据表设计

```sql
-- 交易指令表 (Layer 1 产出)
CREATE TABLE playbook_instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,                    -- 交易日 (YYYY-MM-DD, ET)
    symbol TEXT NOT NULL,                  -- US.NTES
    direction TEXT NOT NULL,               -- BUY / SELL_SHORT / SKIP
    skip_reason TEXT,                      -- SKIP 时的原因
    
    -- 执行参数
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    position_size INTEGER,                 -- 股数
    risk_amount REAL,                      -- 风险金额
    risk_reward REAL,                      -- R:R
    confidence INTEGER,                    -- 0-100
    
    -- 选股条件 (固定枚举, 用于 pattern 分析)
    conditions_json TEXT,                  -- ["insider_cluster_buy", "pe_below_industry", ...]
    thesis TEXT,                           -- 1句话交易逻辑
    sector TEXT,                           -- 行业 (用于集中度分析)
    risks_json TEXT,                       -- ["中概政策风险", ...]
    
    -- 市场环境 (生成时快照)
    market_regime TEXT,                    -- RISK_ON / CAUTIOUS / RISK_OFF / NO_TRADE
    vix_at_generation REAL,
    spy_trend_at_generation TEXT,          -- above_ma20 / below_ma20
    
    -- 数据质量
    data_quality_json TEXT,               -- {"fundamental": "ok", ...}
    ok_dimensions INTEGER,                 -- 可用维度数 (用于降级分析)
    
    -- Pattern 匹配
    matched_patterns_json TEXT,            -- 匹配了哪些已知 pattern
    pattern_verdict TEXT,                  -- follow / avoid / neutral
    
    -- 追踪 (Layer 3 填充)
    triggered INTEGER DEFAULT 0,           -- 价格是否进入入场区间
    executed INTEGER DEFAULT 0,            -- 用户是否执行了
    execution_price REAL,                  -- 实际执行价
    execution_time TEXT,                   -- 执行时间
    exit_price REAL,                       -- 平仓价
    exit_time TEXT,                        -- 平仓时间
    exit_reason TEXT,                      -- stop_loss / take_profit / trailing / scale_out / manual / time_stop
    holding_period_hours REAL,             -- 持仓时长
    pnl REAL DEFAULT 0,                    -- 盈亏
    pnl_pct REAL DEFAULT 0,               -- 盈亏百分比
    
    -- MFE/MAE (最大有利/不利偏移, 评估止损止盈质量)
    max_favorable_excursion REAL,          -- 持仓期间最大浮盈
    max_adverse_excursion REAL,            -- 持仓期间最大浮亏
    mfe_analysis TEXT,                     -- "止盈设低了(MFE=$8.5 vs TP=$5.2)" / "止损设对了"
    
    outcome TEXT,                          -- win / loss / holding / skipped / missed
    shadow_pnl REAL,                       -- 未执行的假设盈亏
    
    -- 复盘
    lesson TEXT,                           -- LLM 生成的复盘教训
    
    -- LLM 原始输出
    raw_instruction_json TEXT,
    
    created_at TEXT NOT NULL,
    updated_at TEXT,
    
    UNIQUE(date, symbol)
);

-- 因子统计表 (双层分析的核心)
CREATE TABLE playbook_factor_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_key TEXT NOT NULL,              -- "insider_cluster_buy" 或 "insider_cluster_buy|pe_below_industry"
    analysis_level TEXT NOT NULL,          -- "single" / "pair" / "combo"
    co_factors_json TEXT,                  -- null for single, ["pe_below_industry"] for pair
    
    -- 统计
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_pnl REAL DEFAULT 0,
    avg_pnl_pct REAL DEFAULT 0,
    total_pnl REAL DEFAULT 0,
    profit_factor REAL DEFAULT 0,
    max_win REAL DEFAULT 0,
    max_loss REAL DEFAULT 0,
    
    -- 贝叶斯估计 (小样本更可靠)
    bayesian_win_rate REAL,                -- 后验均值 (先验=50%, beta分布)
    bayesian_lower REAL,                   -- 95% 可信区间下界
    bayesian_upper REAL,                   -- 95% 可信区间上界
    effective_sample_size REAL,            -- 贝叶斯有效样本量
    
    -- 判定
    verdict TEXT DEFAULT 'observing',      -- positive / negative / observing
    last_updated TEXT,
    
    UNIQUE(factor_key, analysis_level)
);

-- 精确组合表 (仅高频组合)
CREATE TABLE playbook_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_key TEXT NOT NULL UNIQUE,      -- 条件组合的 hash
    conditions_json TEXT NOT NULL,         -- ["insider_cluster_buy", "pe_below_industry"]
    
    -- 统计
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_pnl REAL DEFAULT 0,
    avg_pnl_pct REAL DEFAULT 0,
    max_win REAL DEFAULT 0,
    max_loss REAL DEFAULT 0,
    profit_factor REAL DEFAULT 0,
    
    -- 贝叶斯估计
    bayesian_win_rate REAL,
    bayesian_lower REAL,
    bayesian_upper REAL,
    
    -- 状态
    status TEXT DEFAULT 'observing',       -- observing / active / retired
    first_seen TEXT,
    last_seen TEXT,
    last_updated TEXT,
    
    -- 周度评估
    weekly_notes TEXT                      -- LLM 生成的周度评价
);

-- 用户反馈表
CREATE TABLE playbook_user_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instruction_id INTEGER NOT NULL REFERENCES playbook_instructions(id),
    action TEXT NOT NULL,                  -- executed / skipped / modified
    skip_reason TEXT,                      -- 用户跳过原因 (自由文本或预设选项)
    modified_field TEXT,                   -- 用户修改了哪个参数 (如 stop_loss)
    original_value TEXT,                   -- 原始值
    modified_value TEXT,                   -- 修改后的值
    created_at TEXT NOT NULL
);

-- 市场环境日志 (Layer 0 产出)
CREATE TABLE playbook_regime_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,             -- 交易日
    regime TEXT NOT NULL,                  -- RISK_ON / CAUTIOUS / RISK_OFF / NO_TRADE
    vix REAL,
    spy_price REAL,
    spy_ma20 REAL,
    spy_ma50 REAL,
    score INTEGER,                         -- regime 评分
    notes TEXT,
    created_at TEXT NOT NULL
);
```

### 6.2 每日盘后追踪 (cron 16:30 ET)

```python
def daily_playbook_tracking():
    """每日盘后更新 playbook_instructions 的结果"""
    
    # 假日检查
    if not is_trading_day(get_trade_date()):
        return
    
    today = get_trade_date()
    instructions = get_today_instructions(today)
    
    for inst in instructions:
        if inst.direction == "SKIP":
            inst.shadow_pnl = calc_shadow_pnl(inst.symbol, inst.entry_price, inst.direction)
            inst.outcome = "skipped"
            continue
        
        # 1. 检查是否触发 (价格进入入场区间 ±2%)
        day_low, day_high = get_day_range(inst.symbol)
        entry_low = inst.entry_price * 0.98
        entry_high = inst.entry_price * 1.02
        inst.triggered = 1 if (entry_low <= day_high and day_low <= entry_high) else 0
        
        # 2. 检查是否执行了 (匹配 trade_records, source_type=MANUAL_PLAYBOOK)
        trade = find_matching_trade(inst.symbol, inst.direction, today)
        if trade:
            inst.executed = 1
            inst.execution_price = trade.entry_price
            inst.execution_time = trade.entry_time
        
        # 3. 计算 MFE/MAE (无论是否执行, 都计算 shadow 值)
        if inst.executed:
            inst.max_favorable_excursion = calc_mfe(inst)
            inst.max_adverse_excursion = calc_mae(inst)
        
        # 4. 检查是否已平仓
        if inst.executed:
            exit_trade = find_exit_trade(inst.symbol, today)
            if exit_trade:
                inst.exit_price = exit_trade.exit_price
                inst.exit_time = exit_trade.exit_time
                inst.exit_reason = exit_trade.exit_reason
                inst.pnl = exit_trade.realized_pnl
                inst.pnl_pct = exit_trade.pnl_pct
                inst.holding_period_hours = calc_holding_hours(inst)
                inst.outcome = "win" if inst.pnl > 0 else "loss"
                
                # MFE/MAE 分析
                inst.mfe_analysis = analyze_mfe_mae(inst)
            else:
                # 持仓中: 计算浮盈 + 更新 MFE/MAE
                current_price = get_current_price(inst.symbol)
                if current_price and inst.execution_price:
                    if inst.direction == "BUY":
                        inst.pnl = (current_price - inst.execution_price) * inst.position_size
                    else:
                        inst.pnl = (inst.execution_price - current_price) * inst.position_size
                    inst.outcome = "holding"
                    inst.max_favorable_excursion = max(inst.max_favorable_excursion or 0, inst.pnl)
                    inst.max_adverse_excursion = min(inst.max_adverse_excursion or 0, inst.pnl)
                    
        elif inst.triggered:
            # 触发了但没执行 → 计算 missed PnL
            inst.shadow_pnl = calc_shadow_pnl(inst.symbol, inst.entry_price, inst.direction)
            inst.outcome = "missed"
        
        update_instruction(inst)
    
    # 更新双层因子统计
    update_factor_stats()
    update_pattern_stats()  # 仅高频精确组合
    
    # 更新连续亏损计数
    update_consecutive_loss_counter()
    
    # 推送飞书
    send_daily_tracking_report(today)


def analyze_mfe_mae(inst) -> str:
    """分析止损止盈设置是否合理"""
    notes = []
    if inst.max_favorable_excursion and inst.take_profit:
        tp_profit = abs(inst.take_profit - inst.entry_price) * inst.position_size
        if inst.max_favorable_excursion > tp_profit * 1.5:
            notes.append(f"止盈设低了(MFE=${inst.max_favorable_excursion:.0f} vs TP收益=${tp_profit:.0f})")
    
    if inst.max_adverse_excursion and inst.stop_loss:
        sl_loss = abs(inst.entry_price - inst.stop_loss) * inst.position_size
        if abs(inst.max_adverse_excursion) > sl_loss and inst.pnl > 0:
            notes.append(f"止损设对了但运气好(MAE=${inst.max_adverse_excursion:.0f} > SL=${sl_loss:.0f})")
    
    return "; ".join(notes) if notes else "止损止盈设置合理"


def is_trading_day(date) -> bool:
    """检查是否为交易日 (排除周末和美国公共假日)"""
    if date.weekday() >= 5:
        return False
    
    US_MARKET_HOLIDAYS_2026 = {
        "2026-01-01",  # New Year
        "2026-01-19",  # MLK Day
        "2026-02-16",  # Presidents Day
        "2026-04-03",  # Good Friday
        "2026-05-25",  # Memorial Day
        "2026-06-19",  # Juneteenth
        "2026-07-03",  # Independence Day (observed)
        "2026-09-07",  # Labor Day
        "2026-11-26",  # Thanksgiving
        "2026-12-25",  # Christmas
    }
    return date.isoformat() not in US_MARKET_HOLIDAYS_2026
```

### 6.3 双层因子分析 (核心改进)

替代 v1 的精确组合匹配，使用双层分析使系统在小样本下就能工作。

```python
def update_factor_stats():
    """双层因子分析: 单因子贡献 + 条件对分析"""
    
    results = get_playbook_results_with_outcome(days_back=90)
    
    # === Layer A: 单因子贡献分析 ===
    factor_trades = defaultdict(list)
    for r in results:
        for condition in r.conditions:
            factor_trades[condition].append(r)
    
    for factor, trades in factor_trades.items():
        stats = calculate_stats(trades)
        bayesian = bayesian_binomial(stats.wins, stats.total)
        
        upsert_factor_stats(FactorStat(
            factor_key=factor,
            analysis_level="single",
            total_trades=stats.total,
            wins=stats.wins,
            win_rate=stats.win_rate,
            avg_pnl=stats.avg_pnl,
            profit_factor=stats.profit_factor,
            bayesian_win_rate=bayesian.mean,
            bayesian_lower=bayesian.lower_95,
            bayesian_upper=bayesian.upper_95,
            verdict=classify_factor(bayesian, stats),
        ))
    
    # === Layer B: 条件对分析 ===
    pair_trades = defaultdict(list)
    for r in results:
        conditions = sorted(r.conditions)
        for i in range(len(conditions)):
            for j in range(i + 1, len(conditions)):
                pair_key = f"{conditions[i]}|{conditions[j]}"
                pair_trades[pair_key].append(r)
    
    for pair_key, trades in pair_trades.items():
        if len(trades) < 3:
            continue  # 样本太少
        stats = calculate_stats(trades)
        bayesian = bayesian_binomial(stats.wins, stats.total)
        
        upsert_factor_stats(FactorStat(
            factor_key=pair_key,
            analysis_level="pair",
            co_factors_json=pair_key.split("|"),
            total_trades=stats.total,
            wins=stats.wins,
            win_rate=stats.win_rate,
            avg_pnl=stats.avg_pnl,
            profit_factor=stats.profit_factor,
            bayesian_win_rate=bayesian.mean,
            bayesian_lower=bayesian.lower_95,
            bayesian_upper=bayesian.upper_95,
            verdict=classify_factor(bayesian, stats),
        ))
    
    # === Layer C: 精确组合 (仅 ≥5 次的组合) ===
    combo_trades = defaultdict(list)
    for r in results:
        combo_key = hash(sorted(r.conditions))
        combo_trades[combo_key].append(r)
    
    for combo_key, trades in combo_trades.items():
        if len(trades) < 5:
            continue
        # 写入 playbook_patterns 表 (保持 v1 逻辑)
        ...


def bayesian_binomial(wins, total, prior_alpha=1, prior_beta=1):
    """
    贝叶斯二项分布估计 (Beta-Binomial)
    
    先验: Beta(1,1) = 均匀分布 (无信息先验)
    后验: Beta(1+wins, 1+losses)
    
    小样本时: 20笔16胜 → 后验均值 = (1+16)/(2+20) = 77%
              而非 naive 80%, 且 CI = [55%, 92%] (宽区间反映不确定性)
    
    大样本时: 100笔72胜 → 后验均值 = (1+72)/(2+100) = 71.6%
              CI = [62%, 80%] (窄区间, 高确定性)
    """
    alpha = prior_alpha + wins
    beta = prior_beta + (total - wins)
    
    mean = alpha / (alpha + beta)
    # 95% 可信区间 (使用 scipy.stats.beta.ppf 或近似)
    from scipy.stats import beta as beta_dist
    lower = beta_dist.ppf(0.025, alpha, beta)
    upper = beta_dist.ppf(0.975, alpha, beta)
    
    return BayesianEstimate(mean=mean, lower_95=lower, upper_95=upper)


def classify_factor(bayesian, stats) -> str:
    """
    因子判定规则 (使用贝叶斯区间而非 naive 胜率):
    
    | 条件 | 判定 |
    |------|------|
    | 贝叶斯下界 > 55% 且 profit_factor > 1.2 | positive |
    | 贝叶斯上界 < 45% 或 profit_factor < 0.8 | negative |
    | 其他 | observing |
    """
    if bayesian.lower_95 > 0.55 and stats.profit_factor > 1.2:
        return "positive"
    elif bayesian.upper_95 < 0.45 or stats.profit_factor < 0.8:
        return "negative"
    else:
        return "observing"
```

### 6.4 每周 Pattern 分析 (cron 每周五 17:00 ET)

```python
def weekly_pattern_analysis():
    """周度双层因子分析 + LLM 报告 + 权重调整"""
    
    # 1. 获取因子统计
    single_factors = get_factor_stats(level="single")
    pair_factors = get_factor_stats(level="pair")
    
    # 2. 识别正/负因子
    positive_singles = [f for f in single_factors if f.verdict == "positive"]
    negative_singles = [f for f in single_factors if f.verdict == "negative"]
    positive_pairs = [f for f in pair_factors if f.verdict == "positive"]
    negative_pairs = [f for f in pair_factors if f.verdict == "negative"]
    
    # 3. 生成因子贡献报告
    report_data = {
        "week": get_current_week(),
        "total_trades": get_week_trade_count(),
        "win_rate": get_week_win_rate(),
        "net_pnl": get_week_net_pnl(),
        "positive_singles": positive_singles,
        "negative_singles": negative_singles,
        "positive_pairs": positive_pairs,
        "negative_pairs": negative_pairs,
        "user_skips_analysis": analyze_user_skips(),  # 用户跳过的指令结果如何
    }
    
    # 4. LLM 生成周报
    weekly_report = llm_generate_weekly_report(report_data)
    
    # 5. 自动调整筛选权重 (闭环)
    adjust_screening_weights(report_data)
    
    # 6. 推送飞书
    send_weekly_report(weekly_report)


def adjust_screening_weights(report_data):
    """根据因子分析结果自动调整 PlaybookScreener 权重"""
    adjustments = []
    
    for factor in report_data["positive_singles"]:
        current_weight = get_screening_weight(factor.factor_key)
        if current_weight < 5:  # 上限 5
            adjustments.append({
                "factor": factor.factor_key,
                "delta": +1,
                "reason": f"正收益因子: 贝叶斯胜率 {factor.bayesian_win_rate:.0%}, "
                         f"CI [{factor.bayesian_lower:.0%}, {factor.bayesian_upper:.0%}]"
            })
    
    for factor in report_data["negative_singles"]:
        current_weight = get_screening_weight(factor.factor_key)
        if current_weight > -5:  # 下限 -5
            adjustments.append({
                "factor": factor.factor_key,
                "delta": -1,
                "reason": f"负收益因子: 贝叶斯胜率 {factor.bayesian_win_rate:.0%}"
            })
    
    if adjustments:
        apply_weight_adjustments(adjustments)
        # 记录调整历史
        log_weight_adjustments(adjustments)
```

### 6.5 Playbook 生成时的 Pattern 注入

```python
def generate_instructions_with_patterns(symbols, data_batch, context):
    """生成指令时注入双层 pattern 信息"""
    
    # 收集所有 positive/negative 因子
    positive_singles = get_factor_stats(level="single", verdict="positive")
    negative_singles = get_factor_stats(level="single", verdict="negative")
    positive_pairs = get_factor_stats(level="pair", verdict="positive")
    negative_pairs = get_factor_stats(level="pair", verdict="negative")
    
    # 格式化给 LLM
    active_patterns = ""
    for f in positive_singles[:5]:  # Top 5
        active_patterns += (
            f"  - [{f.analysis_level}] {f.factor_key}: "
            f"贝叶斯胜率{f.bayesian_win_rate:.0%} "
            f"(CI {f.bayesian_lower:.0%}-{f.bayesian_upper:.0%}), "
            f"平均{f.avg_pnl:+.0f}, {f.total_trades}笔\n"
        )
    for f in positive_pairs[:5]:
        active_patterns += (
            f"  - [pair] {f.factor_key}: "
            f"贝叶斯胜率{f.bayesian_win_rate:.0%}, "
            f"平均{f.avg_pnl:+.0f}, {f.total_trades}笔\n"
        )
    
    retired_patterns = ""
    for f in negative_singles[:5]:
        retired_patterns += (
            f"  - [{f.analysis_level}] {f.factor_key}: "
            f"胜率{f.bayesian_win_rate:.0%}, 平均{f.avg_pnl:+.0f}\n"
        )
    
    context["active_patterns"] = active_patterns
    context["retired_patterns"] = retired_patterns
    
    return generate_instructions(symbols, data_batch, context)
```

---

## 7. 通知系统

### 7.1 飞书推送 (复用 app/notifier/feishu.py + app/push/)

| 时机 | 内容 | 格式 |
|------|------|------|
| 08:30 ET 盘前 | 今日交易指令 + 市场环境 | 标的+方向+入场+止损+止盈+regime |
| 16:30 ET 盘后 | 今日追踪结果 | 每条指令的执行情况+盈亏+MFE/MAE |
| 盘中 (可选) | 价格预警 | 接近止损/止盈时推送 |
| 每周五 17:00 | 双层因子周报 | 单因子+条件对分析 + 权重调整 |
| 触发熔断 | 紧急告警 | 日内亏损上限 / 连续亏损 |

### 7.2 飞书消息格式

**盘前指令推送**:
```
📋 今日交易指令 (2026-06-18)
环境: 🟢 RISK_ON (VIX 18.5, SPY $542 MA20上方)
━━━━━━━━━━━━━━━━━━━━━━━

#1 US.NTES BUY LONG ✅ 72%
   入场 $119.50 → 止损 $114.80 → 止盈 $128.00
   50股 | R:R 1.8:1 | 风险 $235
   条件: 内部人集群买入(cluster=0.78) + PE低估 + RSI中性
   Pattern: insider_cluster_buy+pe_below → 贝叶斯胜率72% [55%-85%]

#2 US.ARM BUY LONG ⚠️ 55%
   入场 $415.00 → 止损 $395.00 → 止盈 $445.00
   8股 | R:R 1.5:1 | 风险 $160
   条件: MACD金叉 + 放量
   ⚠️ 已有半导体持仓1个

跳过: META(财报3天内), ORCL(数据不足2/6维度)
```

**盘后追踪推送**:
```
📊 今日指令追踪 (2026-06-18)
━━━━━━━━━━━━━━━━━━━━━━━

#1 NTES BUY LONG
   ✅ 执行 $120.10 | 日内最高 $122.80
   持仓中 | 浮盈 +$30 (+0.5%)
   MFE +$135 | MAE -$45
   止损 $114.80 未触发 (ExitMonitor trailing: $116.20)

#2 ARM BUY LONG
   ❌ 未执行 | 日内: $408→$395 (-3.2%)
   Shadow PnL: -$160
   结论: 没执行是对的 (已记录用户反馈)

#3 TSLA SHORT
   ✅ 执行 $401 | 已止盈(分批) $393
   Scale-out: 1/3@$398, 1/3@$395, 1/3@$393
   盈利 +$80 (+2.0%)

今日总结: 执行 2/3, 浮盈 +$110
连续盈利: 2 笔
```

**双层因子周报推送**:
```
📊 周度因子复盘 (第3周, 6/15-6/19)
━━━━━━━━━━━━━━━━━━━━━━━

本周交易: 12 笔 | 胜率 58% | 净盈亏 +$847
环境分布: RISK_ON 3天, CAUTIOUS 2天

🟢 正收益单因子 (强化):
   insider_cluster_buy: 8笔, 贝叶斯胜率72% [58%-84%], avg +$189
   pe_below_industry: 6笔, 贝叶斯胜率65% [45%-82%], avg +$95

🟢 正收益条件对:
   insider_cluster_buy + pe_below: 5笔, 胜率80%, avg +$210
   rsi_oversold + above_ma20: 4笔, 胜率75%, avg +$112

🔴 负收益单因子 (弱化):
   macd_golden_cross: 7笔, 贝叶斯胜率32% [15%-52%], avg -$140

🔴 负收益条件对:
   macd_golden_cross + volume_expansion: 4笔, 胜率25%, avg -$156

⚠️ 观察中 (样本不足):
   earnings_beat_streak: 2笔, 贝叶斯CI [28%-85%]

📝 权重自动调整:
   insider_cluster_buy: 4 → 5 (正收益)
   macd_golden_cross: 1 → 0 (负收益)

👤 用户跳过分析:
   本周跳过 3 条, 其中 2 条 shadow_pnl 为正
   → 建议: 可考虑更积极地执行 insider_cluster_buy 信号
```

---

## 8. Cron 调度

| Job | 调度 (ET → UTC) | 内容 | 交付 | 失败重试 |
|-----|-----------------|------|------|---------|
| playbook-generate | `0 8 * * 1-5` (12:00 UTC) | Layer 0 环境判断 + 数据收集 + 筛选 + 批量 LLM | 飞书 + Dashboard | 08:30, 09:00 各重试一次 |
| playbook-track | `30 16 * * 1-5` (20:30 UTC) | 结果追踪 + 双层因子更新 + MFE/MAE | 飞书 | 17:00 重试 |
| playbook-weekly | `0 17 * * 5` (21:00 UTC) | 因子分析 + 权重调整 + LLM 周报 | 飞书 | 周六 09:00 重试 |

**假日处理**: 所有 job 首先检查 `is_trading_day()`, 非交易日直接跳过。

---

## 9. 实施路线图

### Phase 0: Shadow 验证 (Week 0, 3天)

| Day | 任务 | 验收 |
|-----|------|------|
| 1 | PlaybookDataCollector 编排层 (复用现有 Provider) | 单只股票 6 维度数据正确收集 |
| 2 | MarketRegimeGate + PlaybookScreener + 批量 LLM | Top 5 标的生成指令 (shadow 模式, 不展示) |
| 3 | playbook_instructions 表 + shadow_pnl 自动计算 | 指令入库, shadow PnL 每日更新 |

**Shadow 模式运行 1 周**: Cron 正常跑, 数据正常收集, 但不推送飞书、不展示 Dashboard。验证:
- 数据管道稳定性 (Provider 调用成功率)
- LLM 输出质量 (JSON 解析成功率, condition tag 一致性)
- Shadow PnL 计算正确性

### Phase 1: Go Live (Week 1-2, 4天)

| Day | 任务 | 验收 |
|-----|------|------|
| 1 | Dashboard Playbook 视图 + 确认面板 | 指令在 Dashboard 可见, 确认弹窗正常 |
| 2 | 执行按钮 → ManualOrderService + ExitMonitor 注册 | 一键下单 + ExitMonitor 接管 |
| 3 | 用户反馈收集 + playbook_user_feedback 表 | 跳过原因正确记录 |
| 4 | 飞书推送 (盘前指令 + 盘后追踪) | 消息格式正确, 含 regime 信息 |

### Phase 2: Pattern 迭代 (Week 3-4)

| Week | 任务 | 验收 |
|------|------|------|
| 3 | playbook_factor_stats 表 + 双层因子分析 + 贝叶斯估计 | 单因子/条件对统计可见 |
| 3 | MFE/MAE 追踪 + 止损止盈质量分析 | 每日更新 MFE/MAE |
| 3 | 周报 cron + LLM 双层因子报告 | 周报自动推送, 含贝叶斯 CI |
| 4 | 权重自动调整 (筛选器闭环) | 正收益因子权重 +1, 负收益 -1 |
| 4 | 连续亏损熔断 + 日内亏损熔断(%) | 风控正常触发 |

### Phase 3: 闭环验证 (Month 2-3)

- 积累 30+ 笔交易数据
- 单因子分析开始产生 actionable 结论 (20+ 笔/因子)
- 条件对分析开始有效 (10+ 笔/对)
- 验证筛选权重调整是否提升了系统胜率
- 用户跳过分析开始产生洞察
- 公式库开始自我迭代

---

## 10. 防烂尾机制

### 10.1 每日零维护

- 所有流程 cron 自动执行 (含假日检查)
- Cron 失败自动重试 (最多 2 次)
- 用户每日操作: 看指令 → 确认 → 点执行 → 完事
- 不需要配置、调整、维护

### 10.2 正反馈循环

```
交易 → 数据积累 → 双层因子发现 → 权重调整 → 更好的指令 → 更好的交易 → 更多数据
         ↓
    用户反馈 → 定性数据积累 → 盲点发现
```

- Week 0 (Shadow): 验证数据管道, 零风险
- Week 2: 单因子分析开始有初步数据
- Week 3: 能看到贝叶斯胜率 + CI 数据
- Week 4: 第一次自动权重调整
- Month 2: 条件对分析开始有效

### 10.3 里程碑检查

| 时间 | 检查项 | 不达标怎么办 |
|------|--------|-------------|
| Week 0 Day 3 | Shadow 模式生成 1 份指令 | 检查 Provider 连通性, 简化数据源 |
| Week 0 结束 | JSON 解析成功率 > 90% | 放宽 Schema, 增加重试次数 |
| Week 1 | Dashboard 可见指令 | 先用飞书推送替代 |
| Week 2 | 积累 5+ 条数据 | 扩大 watchlist, 降低 SKIP 门槛 |
| Week 3 | 看到单因子贝叶斯胜率 | 接受小样本宽 CI, 继续积累 |
| Month 1 | 1 个 positive 单因子 | 降低贝叶斯下界阈值 (50% → 48%) |
| Month 2 | 1 个 positive 条件对 | 继续积累, 条件对比单因子需要更多数据 |

### 10.4 功能冻结规则

- 每周只加 1 个 feature
- 每个 feature 必须有明确的量化价值指标
- 不达标 → 回滚，不叠加
- 不做"以后可能有用"的功能

---

## 11. 技术约束

| 约束 | 值 | 原因 |
|------|-----|------|
| LLM 调用次数/天 | 1 次批量调用 (盘前) | 成本 + 延迟 |
| LLM 超时 | 45s (批量调用) | 10 只股票数据量较大 |
| LLM JSON 重试 | 最多 2 次 | 平衡可靠性和延迟 |
| Finnhub API calls/分钟 | ≤ 55 (free tier 60) | 留 buffer |
| 数据收集总时间 | ≤ 60s / 10 只股票 (并行) | 盘前时间窗口 |
| 指令生成总时间 | ≤ 120s (含重试) | 09:30 前必须完成 |
| Cron 重试间隔 | 30 分钟 | 给 Provider 恢复时间 |
| Dashboard 展示延迟 | ≤ 2s | 用户体验 |

---

## 12. 成功指标

| 指标 | Month 1 | Month 3 | Month 6 |
|------|---------|---------|---------|
| 累计交易数 | 20+ | 60+ | 120+ |
| 系统胜率 | ≥ 50% | ≥ 55% | ≥ 58% |
| Positive 单因子 | 1+ (贝叶斯 CI 下界>50%) | 3+ (下界>55%) | 5+ |
| Positive 条件对 | — | 1+ | 3+ |
| 筛选权重调整次数 | 0-1 | 2-4 | 持续迭代 |
| 每日维护时间 | 0 min | 0 min | 0 min |
| 月度 PnL | > 0 | 稳定正 | 可预期 |
| 最大回撤 | < 10% | < 8% | < 5% |
| LLM JSON 解析成功率 | > 85% | > 95% | > 98% |
| 用户反馈收集率 | > 50% | > 80% | > 90% |

---

## 附录 A: 与 v1.0 的关键差异

| 维度 | v1.0 | v2.0 | 改动原因 |
|------|------|------|---------|
| IC 数据 | -0.226 | -0.047 (live) | 基于最新 ic_analysis_latest.json |
| LONG 信号 | 未提及 | IC=+0.185, WR=60.8% | 未来可恢复部分自动化 |
| 数据收集 | 从零实现 6 个收集器 | 复用现有 Provider (编排层) | 减少 60%+ 开发量 |
| LLM 调用 | 每只股票 1 次 (10次) | 批量 1 次 (所有股票) | 延迟 90s→15s, 成本降 60%, 可做跨股比较 |
| 市场环境 | 无 | Layer 0 Market Regime Gate | 避免恐慌环境下推荐买入 |
| 筛选权重 | 硬编码 | YAML 可配置 + Pattern 系统反馈 | 形成真正的闭环 |
| 行业集中度 | 无 | 同行业 ≥2 持仓降权 | 控制关联风险 |
| Pattern 系统 | 精确组合匹配 | 双层分析 (单因子+条件对) + 贝叶斯 | 解决组合爆炸, 小样本可工作 |
| Condition Tag | LLM 自由生成 | 固定枚举 (27 个标签) | 确保 Pattern 系统一致性 |
| 持仓上下文 | 未注入 Prompt | 注入现有持仓+行业暴露 | 避免重复/过度集中 |
| 止损止盈 | Broker STOP + LIMIT | ExitMonitor (主) + Broker STOP (最后防线) | 复用 2316 行智能退出系统 |
| 风控参数 | 固定 $500 | 百分比 (1% 总资金) | 适配不同账户规模 |
| 连续亏损 | 无 | 3笔减仓, 5笔停 1天 | 防止情绪化交易 |
| MFE/MAE | 无 | 追踪最大有利/不利偏移 | 评估止损止盈设置质量 |
| 用户反馈 | 无 | 跳过原因收集 | 发现用户盲点, 定性数据积累 |
| Shadow 阶段 | 无 | Week 0 验证 | 上线前发现管道问题 |
| 假日处理 | 无 | is_trading_day() 检查 | 避免假日空跑 |
| Cron 重试 | 无 | 失败后 30min 重试 (最多 2 次) | 提高可靠性 |
| 数据降级 | 仅记录质量 | 质量影响 confidence 和 SKIP 决策 | 避免数据缺失时强行生成 |
