# 交易策略核心深度分析 (2026-05-19)

> **核心结论**: 数据驱动揭示**策略层面 5 个反直觉真相**, 当前系统的"信号过滤"和"主战场"选择**与真实 alpha 完全错配**.
> **数据基础**: 4/24-5/19 共 598 笔成交 + exit_policy + voting_config + scalping rules 全部审视.

---

## 1. 5 个反直觉数据真相

### 真相 1: 🔴 信号强度越高反而越亏 (信号过滤系统反向)

| 信号强度 | 笔数 | 总 PnL | 平均 PnL |
|---|---:|---:|---:|
| **strength=0** (legacy/无信号) | 126 | **+$11,904** | **+$94** |
| strength 30-59 | 16 | -$500 | -$31 |
| strength 60-79 | 291 | +$3,221 | +$11 |
| **strength 80+** ("最高质量") | **298** | **-$247** | **-$1** |

**冲击性洞察**:
- 系统挑出的"高质量信号" (strength 80+) 实际**接近零收益, 平均每笔 -$1**
- 真正盈利的是 strength=0 (没经过信号系统的 legacy/手动/trailing 触发的 trade)
- **当前的特征工程从根本上错了** — 高 strength 信号是反向 alpha

### 真相 2: 🔴 持仓时长 U 形曲线 — 真正赚钱的窗口被避开

| 持仓时长 | 笔数 | PnL | avg | 胜率 |
|---|---:|---:|---:|---:|
| **<6min** | **461** (77%!) | +$1,199 | +$3 | 2.8% |
| **6min-1h** | 38 | **+$15,331** | **+$403** | 31.6% |
| 1-4h | 55 | -$1,498 | -$27 | 41.8% |
| **4-24h** | 17 | **+$6,998** | **+$412** | 52.9% |
| >24h | 1 | -$6,015 | -$6,015 | 0% |

**洞察**:
- **77% 笔数集中在 <6min** (摩擦灾难区, 几乎不可能盈利)
- **真正盈利的 alpha 在 6min-1h 和 4-24h** — 但合计只有 55 笔, 占 9%
- **当前策略主战场 (scalping <6min) 是错的** — 数据告诉我们应该做中长线

### 真相 3: 🔴 main SHORT 是唯一 alpha, 但仅 3 笔

| owner_strategy × direction | 笔数 | PnL | 胜率 |
|---|---:|---:|---:|
| **main SHORT** | **3** | **+$1,415** | **66.7%** ⭐ |
| scalping SHORT | 8 | -$56 | 0% |
| scalping LONG | 5 | -$191 | 0% |
| main LONG | 3 | -$398 | 0% |

**洞察**: 
- 真正赚钱的是**主线 SHORT 中线** (5/15 ARM SHORT +$1219 / 5/8 ARM SHORT +$359 / 5/9 ARM SHORT +$189)
- 但**只占 owner_strategy 标记笔数的 16%** — 大部分资源浪费在 scalping (110+ 笔)
- 当前系统在主战场选择上**完全失衡**

### 真相 4: 🔴 voting "5 model agent" 实际只是 2 模型

`app/agents/voting/voting_config.py:85-115`:
```python
model_weights={
    ModelRole.QWEN: 0.70,       # Qwen 3.6 Plus, 主判断
    ModelRole.MINIMAX: 0.30,    # MiniMax 异议检测
}
```

**真相**: 设计是 "5 model agent voting", 实际**只有 2 个 LLM** (Qwen 主 + MiniMax 副), MiniMax 仅在分歧时介入. 加上 yaml 路由的 thesis_inferer/news_classifier 等也都用 Qwen/Deepseek, **没有真正的 5 独立 agent**.

### 真相 5: 🔴 TP/SL 数学不匹配胜率

scalping 默认: TP=1.5%, SL=0.8%, 比例 = 1.875 : 1
- 盈亏平衡需要的胜率: 1 / (1 + 1.875) = **34.8%**
- 实际胜率: 2.8% (<6min) ~ 31.6% (6min-1h)

**洞察**: 即使在最佳 6min-1h 区间 (31.6% 胜率), **仍低于盈亏平衡线 34.8%**. 当前 TP/SL 比例对低胜率信号根本不可能盈利.

---

## 2. 现有策略的设计 vs 实际

### 入场策略 (scalping)
**配置** (`scalping_engine.py`):
- vwap_deviation_pct: 0.012 (偏离 vwap 1.2%)
- rsi_oversold: 25, overbought: 75
- bollinger 边界
- momentum_detector
- 默认 TP=1.5% / SL=0.8%

**问题**:
- 这些是教科书级 mean-reversion 信号 — 在震荡市可能有效, 趋势市完全无效
- 5/12-5/18 市场是震荡下行, 但系统主要做 LONG (scalping 默认偏多)
- **vwap 偏离 1.2% 在低价股 (LCID $5.75) 是日内常态噪音**, 不是信号

### 退出策略 (exit_policy.yaml v2.4) — **设计良好**
- ✅ Flash Exit (60s 内剧烈波动, multiplier 1.0)
- ✅ ATR 动态止损 (3 段: initial 2.0x → 1atr 1.8x → 5atr 1.2x)
- ✅ Chandelier trail (regime-aware: normal 2.5x / trending 3.5x / volatile 2.0x)
- ✅ Breakeven (盈利后保本)
- ✅ Pre-close exit (盘末时间加权)
- ❌ Time stop 已禁用 (5/16 O3 修复)

退出逻辑确实做得不错 — 真正赚钱的 main SHORT TRAILING_STOP 是这套机制的功劳.

### LONG vs SHORT 设计差异

**SHORT 门禁** (order_executor.py:787-) — **过严**:
- ~~short_min_strength<20~~ (5/19 已删, A.1 修复)
- effective<65 仍保留
- 三重确认: RSI + MACD + 均线
- regime gate (check_regime_for_short)

**LONG 门禁** — **过松**:
- 几乎没有专属过滤器
- scalping LONG 直接走 vwap/rsi 信号

**结果**: SHORT 信号反复被拒 (5/12-18 124 笔被拒), LONG 大量入场全军覆没.

### 止损止盈分层 (`exit_policy.yaml`)

| 层级 | 触发 | 行为 |
|---|---|---|
| Flash | 60s 内 ≥ 1.0 ATR 反向 | 立即平 |
| ATR Stop | 价格 ≤ entry - 2.0 ATR (initial) | 止损 |
| ATR Stop (盈利) | 收紧到 1.8/1.2 ATR | 锁利 |
| Chandelier | 盈利 ≥ 1.2 ATR 后激活 | trail 高点 - 2.5 ATR |
| Breakeven | 触发后 SL = entry | 保本 |

**评估**: **退出策略本身不需要大改**. 真正赚钱的 main SHORT TRAILING_STOP 用的就是这套.

---

## 3. 策略层面的 7 项调整建议 (按 ROI 排序)

### 🎯 Priority 1: 砍 <6min 持仓 (减亏 + 减摩擦)

**数据**: <6min 461 笔 (77%) avg=+$3 (基本不赚), 摩擦+滑点吞利

**调整**:
```yaml
scalping:
  min_hold_minutes: 6                    # 强制最小持仓 6min
  max_signals_per_day: 30                # 当前 ~80 笔/天 → 砍 60%
  vwap_deviation_pct: 0.020              # 1.2% → 2.0% (更严)
  rsi_oversold: 20                       # 25 → 20 (更极端)
  rsi_overbought: 80                     # 75 → 80
```

**预期**: 5/12-18 461 笔 → ~150 笔, 减摩擦 $1500/月

### 🎯 Priority 2: 主线 SHORT 加权 + 放开

**数据**: main SHORT 3 笔 +$1,415 (胜率 66.7%) — 唯一 alpha 路径

**调整**:
1. **加大权重**: voting 主线对 SHORT 信号 confidence × 1.3
2. **放开门禁**: 已删 short_min_strength<20, 还可放宽 effective<65 → effective<55
3. **加目标 SHORT 标的**: 当前 main SHORT 集中 ARM, 应扩展到 SPY/QQQ/SNDK 等机构标的

```python
# voting_aggregator.py
SHORT_BIAS_MULT = 1.3   # 5/12-18 SHORT alpha 是 LONG 反向, 给 SHORT 加权
if signal.direction == "SHORT":
    signal.confidence *= SHORT_BIAS_MULT
```

**预期**: 主线 SHORT 笔数 3/月 → 10-15/月, 同等胜率累计 +$5,000/月

### 🎯 Priority 3: TP/SL 比例匹配实际胜率

**当前**: 1.5% / 0.8% = 1.875:1 (盈亏平衡需 34.8% 胜率, 实际 ~30%)

**调整**: 拉宽 TP, 收紧 SL
```yaml
scalping:
  take_profit_pct: 0.025    # 1.5% → 2.5%
  stop_loss_pct: 0.005      # 0.8% → 0.5%
  ratio: 5.0                # 5:1 (盈亏平衡仅需 16.7% 胜率)
```

**风险**: SL 太紧可能噪音平仓 → 实测后微调

**预期**: 同样 30% 胜率下从 -$0.18% net → +$0.4% net (净改善 0.5%)

### 🎯 Priority 4: 信号过滤反向 — 重做特征工程

**数据**: strength 80+ 反而亏 — 当前特征是反向 alpha

**调整**:
1. **A/B 测试**: 把 strength 评分**反转** — strength 80+ 的拒入场, strength 30-60 的优先
2. **重新校准**: 基于 100+ 笔回测找出真正与 PnL 正相关的特征
3. **临时方案**: 在 voting_aggregator 加 inversion gate
   ```python
   # 临时反向门 — 数据驱动验证后决定保留与否
   if signal.strength >= 80 and not _has_recent_alpha_pattern(symbol):
       reduce_position_size_pct = 0.5  # 高 strength 减半仓位 (而非加倍)
   ```

**预期**: 1 周后看 strength × PnL 是否仍反向, 决定保留还是回滚

### 🎯 Priority 5: 主战场迁移 6min-1h 和 4-24h

**当前**: scalping <6min 主战场 (461/598 = 77% 笔)
**应该**: 中线 6min-1h (38 笔 +$15,331) + 长线 4-24h (17 笔 +$6,998)

**调整**:
```yaml
strategies:
  scalping:
    enabled: true
    weight: 0.20             # 从 ~70% 流量降到 20%
    min_hold_minutes: 6
  
  swing_short:                # 新增 — 复用 main SHORT 模式
    enabled: true
    weight: 0.50              # 主战场
    direction_bias: SHORT
    target_holding_hours: [0.1, 1.5]
    
  multi_day:                  # 长线 (复用 4-24h 模式)
    enabled: true
    weight: 0.30
    target_holding_hours: [4, 24]
```

**预期**: 资源从 461 笔 <6min → 30 笔 <6min + 80 笔 6min-1h + 30 笔 4-24h, 整体 PnL 提升 5-10x

### 🎯 Priority 6: 低价股黑名单常态化

**数据**: LCID($5.75) 21 笔 4.8% 胜率 -$656; TME($9) 15 笔 0% 胜率 -$442

**调整** (B.2 已实施): scalping 拒 < $20
**额外**: yaml 配置永久黑名单
```yaml
scalping:
  symbol_blacklist:
    - "US.LCID"
    - "US.TME"
    - "US.RIVN"
    - "US.UBER"   # 5/12-18 7 笔 -$111
```

### 🎯 Priority 7: 退出策略保留, 微调 Chandelier

**评估**: 退出策略 (exit_policy.yaml) 已经非常成熟 — 真正赚钱的 main SHORT TRAILING_STOP 就是这套.

**唯一微调**:
- 加 `early_profit_lock`: 盈利 ≥ 0.5% 后立即激活 chandelier (现在 1.2 ATR ≈ 1-2%, 太晚)
- 让短期波动也能锁利 (5/15 ARM SHORT +$1219 持仓 1.2h, 但很多机会被 chandelier 太晚激活错过)

```yaml
chandelier_trail:
  activation_profit_atr: 0.5    # 1.2 → 0.5 (更早激活, 锁更多利润)
```

---

## 4. 决策矩阵 — 必须做 vs 推荐 vs 暂缓

| 优先级 | 调整 | 必要性 | 风险 | 实施时间 |
|---|---|:---:|:---:|---|
| P1 | 砍 <6min 持仓 | 🔴 必须 | 低 | 1 天 |
| P2 | 主线 SHORT 加权 | 🔴 必须 | 中 | 3 天 (需 A/B) |
| P3 | TP/SL 比例改 5:1 | 🟡 推荐 | 中 (噪音平仓) | 1 天 |
| P4 | 信号反向特征工程 | 🟡 推荐 | 高 (理论需验证) | 2 周 (需回测) |
| P5 | 主战场迁移 | 🔴 必须 | 高 (架构改) | 1-2 周 |
| P6 | 低价股黑名单 | ✅ 已做 | 低 | done |
| P7 | Chandelier 早激活 | 🟢 微调 | 低 | 1 天 |

---

## 5. 数据驱动的根本质疑

### 质疑 1: scalping 应该存在吗?

5/12-18 数据: scalping 13 笔 -$247 / 主线 6 笔 +$1,017. 但是过去 1 个月: scalping 478 笔 +$1,008 (avg +$2) / 主线 6 笔 +$1,017 (avg +$170).

**主线 1 笔 = scalping 85 笔的 PnL**. 

**结论**: scalping 不应该是主战场. 但完全砍掉风险也大 (老板可能要求高频交易). 折中: 砍 60% scalping 流量 + 加大主线 SHORT.

### 质疑 2: 信号系统到底有没有价值?

strength 0 的 legacy 仓位赚 $11,904 (avg +$94), strength 80+ 的"高质量"信号亏 -$247. 

**质疑**: 当前的信号系统 (5 model voting + 因子评分) **可能不如手动 + trailing stop**. 

**建议**: 1 周 A/B 测试 — 一半流量走 strength 评分入场, 一半走"无信号 + 趋势 follower" (vwap 大趋势 + chandelier).

### 质疑 3: LLM Pre-Entry Gate 应该用什么数据?

我之前设计的 Pre-Gate 用 7 天历史. 但数据显示 **strength 80+ 历史也是反向**. 单纯看 7 天可能不够.

**调整建议**: Pre-Gate prompt 加入 "市场环境匹配" — 不只是历史 win_rate, 还有 regime / SPY 趋势 / VIX 等综合判断 "现在的市场该做什么".

---

## 6. 一句话总结

> **当前策略的根本问题**:
> 1. **战场错** — 77% 流量在 <6min 摩擦灾难区, 真正盈利在 6min-1h 和 4-24h
> 2. **方向错** — main LONG 全亏, main SHORT 全赢, 但 SHORT 笔数极少
> 3. **过滤错** — strength 越高反而越亏, 信号系统是反向 alpha
> 4. **数学错** — TP/SL 1.875:1 配低胜率永远不能盈利
> 5. **agent 虚** — 设计 5 model 实际 2 个, 没有真正多视角
>
> **最关键 3 个调整 (今晚后立即做)**:
> 1. 砍 scalping <6min 流量 60% (减亏)
> 2. 主线 SHORT 加权 1.3x + 放开门禁 (放大唯一 alpha)
> 3. TP/SL 改 5:1 (匹配实际胜率)
>
> **退出策略不必大改** — exit_policy.yaml v2.4 已经是 Karpathy 级设计, 真正赚钱的 main SHORT TRAILING_STOP 就是这套机制. 关键是入场策略和主战场选择.
>
> **Karpathy 第一性**: 当数据告诉你 "高质量信号亏损" 时, 不要再优化信号过滤算法 — **先承认特征工程错了**, 重新审视什么是真正的 alpha 信号. 5/12-18 数据明确指出: regime-aware SHORT + 中长线持仓 + trailing stop = alpha. 与现有系统的 "scalping 短线 + LONG 偏向 + 高频" 完全相反.
