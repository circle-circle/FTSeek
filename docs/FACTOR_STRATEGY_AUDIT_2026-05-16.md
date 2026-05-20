# 因子-策略-信号-仓位 关联审计 (2026-05-16)

> **触发**: 用户 review 5/15 16.67% 胜率, 要求审计因子重复 / trace 链 / 权重叠加
> **基准数据**: `daily_review_2026-05-15.json`, `ftseek_us.db`, `decisions.db`, `ftseek_us.log`

---

## 1. 因子-策略映射表

### 1.1 因子总清单 (代码定位)

| 因子 ID | 名称 | 定义位置 | 计算方法 |
|--------|-----|---------|---------|
| **F1** | trend_confirm | `decision/signal_scorer.py:81` + `intraday_scorer.py:407` | EMA20/MA50 + 价格关系 |
| **F2** | momentum | `intraday_scorer.py:486` | 3m/5m 涨跌率 + EMA 斜率 |
| **F3** | volume_confirm / volume | `signal_scorer.py:85` + `intraday_scorer.py:577` | 当日量 vs 20d 均量 |
| **F4** | orderflow | `intraday_scorer.py:658` [LV3 专有] | 买卖盘失衡 (实时挂单) |
| **F5** | volatility_fit | `signal_scorer.py:89` | ATR / VIX 调节器 |
| **F6** | session | `intraday_scorer.py:204` (`_session_multiplier`) | 时段乘子 (调节器) |
| **F7** | lifecycle | `intraday_scorer.py:819` | 二阶 EMA 平滑 + 加速/收敛 |
| **F8** | market_align | `signal_scorer.py:93` | SPY 同向 + 板块同向 |
| **F9** | history_performance | `signal_scorer.py:97` | 标的 60 日胜率/夏普 |
| **F10** | news_sentiment | `signal_scorer.py:101` | L1 LLM 情绪分 |
| **F11** | regime_fit | `signal_scorer.py:105` | TRENDING/RANGING 适配 |
| **F12** | risk_reward | `signal_scorer.py:109` | TP/SL 比 |
| **S1** | RSI(14) extreme | `scalping/signals.py:66` | <20=LONG, >80=SHORT (反向) |
| **S2** | VWAP deviation | `scalping/signals.py:80` | ±1.8% (反向) |
| **S3** | Bollinger band | `scalping/signals.py:98` | 突破 2.5σ (反向) |
| **S4** | k5m_trend | `scalping/signals.py:detect` 调用 | 5min 趋势对齐过滤 |
| **S5** | order_book imbalance | `scalping/signals.py:detect` 调用 | 订单流 (与 F4 同源数据) |
| **N1** | major_event_upcoming | `signal_scorer.py:49` (负因子) | 财报/FOMC 临近 |
| **N2** | low_liquidity | `signal_scorer.py:50` (负因子) | spread > 阈值 |
| **N3** | high_spread | `signal_scorer.py:51` (负因子) | 同 N2, 程度区分 |
| **N4** | signal_conflict | `signal_scorer.py:52` (负因子) | 模型分歧 |
| **N5** | net_edge_below_floor | `signal_scorer.py:53` (负因子) | 扣 cost 后净边际 < 门槛 |
| **P1** | battle_plan entry_zone | `strategy/pure/fast_lane.py:48` | 价格 ∈ entry_zone |

### 1.2 策略 × 因子 矩阵

| 因子 | 主线 IntradayScorer | 主线 SignalScorer | Scalping | FastLane | LLM Plan |
|-----|:-:|:-:|:-:|:-:|:-:|
| **F1 trend** | ✅ 0.25 | ✅ 0.20 | ⚠ S4 (k5m_trend 过滤) | — | LLM 隐式参考 |
| **F2 momentum** | ✅ 0.25 | — | ⚠ S2 VWAP 偏离 | — | LLM 隐式 |
| **F3 volume** | ✅ 0.18 | ✅ 0.15 | — | — | LLM 隐式 |
| **F4 orderflow** | ✅ 0.17 | — | ⚠ S5 (同源数据) | — | — |
| **F5 volatility_fit** | (调节器) | ✅ 0.10 | (VIX 自适应 BB 权重) | — | — |
| **F6 session** | (调节器) | — | (TimeGate 过滤) | — | — |
| **F7 lifecycle** | ✅ 0.15 | — | — | — | — |
| **F8 market_align** | — | ✅ 0.15 | — | — | LLM 隐式 |
| **F9 history_performance** | — | ✅ 0.10 | — | — | — |
| **F10 news_sentiment** | — | ✅ 0.10 | — | — | LLM 显式 |
| **F11 regime_fit** | (内嵌权重偏移) | ✅ 0.10 | — | — | LLM 显式 |
| **F12 risk_reward** | — | ✅ 0.10 | — | — | LLM 显式 |
| **S1 RSI extreme** | — | — | ✅ 1/3 投票 | (附加 note) | — |
| **S2 VWAP dev** | — | — | ✅ 1/3 投票 | — | — |
| **S3 Bollinger** | — | — | ✅ bb_weight 投票 | — | — |
| **P1 entry_zone** | — | — | — | ✅ 唯一触发器 | (LLM 自己生成) |
| **N1-N5 负因子** | — | ✅ 全套 | — (走 ScalpingCircuitBreaker) | — | — |

✅ = 显式权重计算 / ⚠ = 同向使用但形式不同 (重叠风险) / — = 不涉及 / 隐式 = LLM prompt 中描述

---

## 2. 冲突 / 重复因子清单

### 2.1 🔴 严重重叠 (同一指标被 2+ 策略计入)

| ID | 重叠因子 | 策略 A | 策略 B | 重叠形式 | 风险 |
|----|---------|------|------|---------|-----|
| **C1** | 趋势 (EMA + 价格) | IntradayScorer F1 (0.25) | SignalScorer F1 trend_confirm (0.20) | 同源数据, 不同权重, **两层串联** | 主线评分 = `IntradayScorer.score() → result → SignalScorer.score()` 时趋势被算 2 次, 实际权重 ≈ 0.25 + 0.20×(传递系数) |
| **C2** | 成交量 | IntradayScorer F3 (0.18) | SignalScorer F3 volume_confirm (0.15) | 同上, 双层 | 同 C1 |
| **C3** | 订单流 | IntradayScorer F4 (0.17) [LV3] | Scalping S5 order_book (检测内) | **同源 raw data** (Futu LV3 order_book), 但分别消费 | 主线和短线对同一时刻订单流不一致解读时, 一方持仓另一方反向 → 资金叠加 |
| **C4** | VWAP / 动量 | IntradayScorer F2 momentum (0.25) | Scalping S2 VWAP deviation | F2 用 EMA 斜率, S2 用 VWAP 偏离 — 都反映"价格脱离均值" | 同方向时双下单 (主线 LONG + scalping LONG 同标的同向) |
| **C5** | RSI | (无显式主线因子) | Scalping S1 (反向交易) | 仅 Scalping 用, 但反向逻辑 (超卖→LONG) | 与主线 F2 动量正向逻辑冲突 — 同标的同时 RSI<20 (S1 触发 LONG) + 动量负 (F2 偏 SHORT), 系统两套都出信号 |
| **C6** | 新闻情绪 | SignalScorer F10 (0.10) | LLM Plan prompt 显式参考 | 同一 L1 LLM 输出 (news_classifier) 被打分 + 二次重写 LLM 决策 | LLM Plan 已在 prompt 内消化新闻, 再被主线 F10 加权 → 同一新闻打分双计 |
| **C7** | regime | SignalScorer F11 (0.10) | IntradayScorer REGIME_WEIGHT_MODIFIERS (隐式) | regime 既改变其他因子权重, 又作为独立因子计入 | regime=EXTREME 时, F4/F5/F11 同时降权/扣分, 形成"乘性叠加" |

### 2.2 🟡 互斥但未声明 (容易踩雷)

| ID | 策略 A | 策略 B | 互斥关系 | 当前处理 |
|----|------|------|---------|---------|
| **X1** | Scalping S1 (RSI 反向) | 主线 F1+F2 (趋势/动量 顺向) | **逻辑反向**: scalping 在超卖时做多 (赌反弹), 主线在动量为正时做多 (赌延续) — 同标的同价位, 一方对了另一方就错 | C2 修复 (2026-05-13) 让 PositionExitMonitor 跳过 scalping; 但**入场层未互斥** — 5/15 同标的 (CRM/ARM/SNDK) 既走 SCALPING 又走 MAIN_PIPELINE |
| **X2** | FastLane P1 (entry_zone) | 主线 SignalScorer 全因子 | FastLane 设计就是跳过投票直接执行, 主线却仍可能在同标的产生独立信号 | `signal_pipeline.py:861` 走 fast-path 后 `return`; 但 fast-path 失败 fallback 到主线 → 同一价格 2 次评估 |
| **X3** | scalping budget 释放 | main capital_pct 占用 | 现有 bucket_map={fast_lane→main}, 但 scalping 独立 25% 桶 — scalping 平仓不通知 main 桶, main 桶占用与 scalping 实际持仓**重复计算总仓位** | Phase A observe_only=true, 真正 reset_and_reconcile 未运行, **总敞口可能被双计** |
| **X4** | symbol_lock vs 双引擎并存 | SymbolLockManager 同标 1 owner | C3 修复 (signal_pipeline.py:370) 让 scalping 持仓时主线仍能评估 — **绕过了 lock** | `lock acquire` 在 observe_only 下不阻断 → 5/15 ARM 既有 692/693 (SCALPING) 又有 706 (MAIN_PIPELINE) 同标的 |

### 2.3 ⚠ 权重叠加导致仓位放大 (实际证据)

5/15 同标的 (ARM) 多笔证据:
```
trade_id=692  US.ARM SHORT  SCALPING       (scalping budget)
trade_id=693  US.ARM SHORT  SCALPING       (scalping budget)
trade_id=706  US.ARM SHORT  MAIN_PIPELINE  (main capital)  ← +$1219 winner
```
- 692/693 在 22:30-22:34 (ET 10:30-10:34) 进出
- 706 在 21:30 平仓 (ET 09:30 = 美东开盘时入场, 持仓 1.2h)
- 三笔都是同方向 SHORT, 但分别占用 scalping 桶 + main 桶
- 没有 `signal_arbitration` 记录 (该表 0 行) → **多策略竞争完全未发生仲裁**

→ 同方向同标的同时持仓 = 单一 thesis 的资金敞口被 2x

---

## 3. trace_id 关联可追溯性审计

### 3.1 设计意图 (理想链路)

```
factor_detail (IntradayScorer._factor_detail)
   ↓
SignalScore (trace_id=US-YYYYMMDD-XXXXXX) [signal_pipeline.py:1248]
   ↓ signal.metadata["factor_detail"]
_submit_order(signal, ...)
   ↓ position.trace_id = signal.trace_id
trade_attribution INSERT (trace_id, signal_strength, signal_confidence)
   ↓
_update_trade_attribution (exit_time, pnl, factor_*)
   ↓
trade_records.trace_id = position.trace_id
```

### 3.2 实测链路断裂点 (5/15 数据)

| 节点 | 期望 | 实测 | 断裂方式 |
|-----|------|------|---------|
| **factor_detail → SignalScore.metadata** | 完整 5 因子值 | ✅ 主线写入 (`_build_signal_from_vote`) | OK |
| **SignalScore.trace_id 设置** | `US-20260515-ABC123` | ✅ `signal_pipeline.py:1248` 生成 | OK |
| **position.trace_id = signal.trace_id** | 传播 | ⚠ 仅 `_execute_long/_execute_stock_short` 显式赋值 | scalping path 完全无 trace_id |
| **trade_attribution INSERT** | 26 行 (一笔一行) | ❌ 仅 4 行 (主线 4 笔) | 22 笔 SCALPING 无 attribution |
| **trade_records.trace_id** | 26 行均填充 | ❌ 4/26 NULL, 22/26 空字符串 | scalping 写 trade_records 时 trace_id="" |
| **trade_attribution.factor_trend/momentum/...** | 5 列填充 | ❌ 100% NULL | `_insert_trade_attribution` 不写 factor_* |
| **trade_attribution.entry_thesis** | PB-1 启用后填充 | ❌ 100% NULL | infer_thesis_async 异步未回写 (或失败 silent) |
| **trade_attribution.owner_strategy** | "main"/"scalping" | ❌ 100% NULL | INSERT 时未传 owner_strategy |
| **signal_arbitration** | 多 candidate 写入 | ❌ 0 行 (历史累计) | observe_only=true 下 arbitrate() 未被调用 |

### 3.3 实际证据: ARM 5/15 最大盈利 trade (id=706)

```sql
SELECT id, symbol, trace_id, futu_order_id, source_type, signal_strength
FROM trade_records WHERE id=706;
706 | US.ARM | "" (空) | "" (空) | MAIN_PIPELINE | 0
```

**事实**: 单日最大盈利 +$1219 的交易, trace_id/futu_order_id/signal_strength **全部空/0** — 无法追溯入场因子, 无法判断是策略对了还是运气. 复盘只能盲猜.

---

## 4. 一条可验证的关联日志示例 (现有 vs 理想)

### 4.1 现有 (5/15 ARM 705 TME 入场)
```
[scalping.engine] 🚀 ACTIVE | US.ARM SHORT @ $217.90 | TP: $216.81 | SL: $218.44 | 数量: 61 | 信号: vwap/bollinger
[scalping.engine] ✅ [信号结果] US.ARM Scalping 已写入 signal_outcomes | pnl=-0.27% | regime=high_volatility | exit=ATR止损
[storage.trade_store] [TradeStore] 保存交易记录 id=706 US.ARM SHORT pnl=$1219.00 strength=0   ← trace_id 缺失
```

**问题**: 缺 trace_id, 不知道哪个信号实例触发, 缺 factor 详细评分, 缺 thesis, 缺 owner_strategy.

### 4.2 理想 (建议格式)
```
[SignalPipeline] US.ARM SHORT trace=US-20260515-A1B2C3 strength=68 conf=0.74 strategy=scalping owner=scalping
    └─ factors: trend=-25 momentum=-18 volume=+12 orderflow=-8 lifecycle=0  (consensus=0.71)
    └─ rationale: VWAP偏离+2.1%, RSI=82超买, BB 上轨突破
[OrderExecutor] US.ARM SHORT trace=US-20260515-A1B2C3 → futu_order=7715955 qty=61 @ $217.90
[ShadowMirror] trace=US-20260515-A1B2C3 → 4 variant (baseline/tighter_stop/no_chandelier/quick_exit)
[Coordinator] arbitrate symbol=US.ARM candidates=[scalping(p=10,c=0.74), main(p=5,c=0.65)] → scalping wins
[CapitalAllocator] reserve scalping +0.05 (after: scalping=0.20, main=0.45, total=0.65/0.90)
[SymbolLock] acquire US.ARM owner=scalping (release on close)
...
[OrderExecutor] US.ARM SHORT trace=US-20260515-A1B2C3 → exit @ $210.80 reason=TRAILING_STOP pnl=+$1219
[ShadowMirror·exit] trace=US-20260515-A1B2C3 close 4 variants (baseline 1:1, others EOD batch)
[Attribution] trace=US-20260515-A1B2C3 thesis=momentum factor_post_review=trend_confirmed=true regime=TRENDING_DOWN
```

每一条都通过 `trace=US-20260515-A1B2C3` 串起来, 复盘时:
```sql
SELECT t.symbol, t.pnl, a.factor_trend, a.factor_momentum, a.entry_thesis, a.regime
FROM trade_records t JOIN trade_attribution a ON t.trace_id = a.trace_id
WHERE t.trace_id = 'US-20260515-A1B2C3';
```
能直接拉出"哪个因子 + 哪个策略 + 哪个 regime → 哪个结果"的全链路.

---

## 5. 6 条具体建议 (按优先级)

### 🔴 立即 (本周内, 修复 trace 链)

**T1. Scalping 入场必须生成并写入 trace_id**
- 位置: `app/scalping/engine.py:1956` (`signal_record = UnifiedTradeRecord(...)`)
- 修复: 入场时生成 `trace_id = f"US-{date}-{uuid[:6]}"`, 注入 `UnifiedTradeRecord(trace_id=...)`
- 影响: 22/26 笔不再 trace_id="" — 复盘可关联

**T2. `_insert_trade_attribution` 写齐 factor_* 列**
- 位置: `app/pipeline/order_executor.py:1930`
- 修复: 入场时除 `signal_strength/confidence`, 同步从 `signal._factor_detail` 写入 `factor_trend/momentum/volume/orderflow/lifecycle`, 以及 `owner_strategy/regime/session/vix_at_entry`
- 影响: 复盘可分解每笔 PnL 到具体因子 (现在 100% NULL)

**T3. Scalping 也写 trade_attribution**
- 位置: `app/scalping/engine.py:_execute_trade` 末尾
- 修复: 加 `_insert_trade_attribution(cursor, trace_id, symbol, scalping_strength, scalping_confidence)` 并附 `signal_tags = "vwap/bollinger" / "rsi/bollinger"` 等
- 影响: SCALPING 路径首次拥有 attribution, 可与 MAIN 同维度复盘

### 🟡 一周内 (消除重叠)

**T4. 明确"决策最终 owner"**, 同标的入场强制 1 个 owner
- 位置: `app/coordinator/symbol_lock.py:acquire` (currently observe_only)
- 修复: sandbox 模式下也强制 enforce, 同标的 1 owner. 主线 + scalping 同时想下同一标的同方向 → 走 SignalArbitrator (priority × confidence) 仲裁, 写 signal_arbitration 留痕
- 影响: 消除 X1/X4 的 5/15 同标的 3 笔重复持仓问题

**T5. 评分双层去重 (C1/C2)**
- 位置: `app/pipeline/signal_pipeline.py:_build_signal_from_vote`
- 修复: 主线评分流 IntradayScorer → SignalScorer 时, 在第二层把 F1/F3 权重置 0 (因为第一层已计算). 或者干脆删除第二层 SignalScorer 的 F1/F3, 让 IntradayScorer 是 SoT
- 影响: 趋势 + 量价不再被 2x 计入

**T6. capital_pct bucket 合并对账周期化**
- 位置: `app/coordinator/capital_allocator.py:reset_and_reconcile` (已存在但 observe_only 不调)
- 修复: 每 5min 后台调用 `reset_and_reconcile(positions_by_strategy)` 校正双计漂移
- 影响: X3 总敞口准确性

### ⚪ 评估后 (结构性优化)

**T7. 因子贡献 → PnL 归因模型** (P2)
- 在 daily_review 中增加: 按因子分解当日 PnL = Σ (factor_score × weight × outcome_correlation)
- 输出: "今日 trend 因子贡献 +$120, momentum +$80, orderflow -$50, lifecycle +$20, 总 +$170, 噪声 -$60"
- 实现: 30+ 笔后用线性回归把 factor_value 回归 pnl 系数, 落入新表 `factor_attribution_daily`

---

## 6. TL;DR 给用户

**Q1 因子重复?** ✅ 重复且严重:
- 7 处明确重叠 (C1-C7), 最关键: IntradayScorer 与 SignalScorer 双层计算 trend/volume, 主线动量与 scalping VWAP/RSI 同源, F10 新闻被打分 + LLM 二次消化.
- 4 处互斥但未声明 (X1-X4), 最关键: scalping RSI 反向 vs 主线动量正向, 5/15 同标的同向 ARM 3 笔证实.

**Q2 trace 可追溯?** ❌ 链路 7 处断裂:
- 22/26 trades trace_id="" (scalping 路径完全没生成)
- 100% factor_* NULL (_insert_trade_attribution 不写)
- 100% entry_thesis NULL (异步推断未回写)
- 100% owner_strategy NULL (INSERT 漏列)
- 0 行 signal_arbitration (多策略仲裁从未发生)
- 5/15 最大盈利 +$1219 trace_id 空 → **盲飞**

**Q3 权重叠加?** ⚠ 同标的多桶占用:
- ARM 5/15 同时占用 scalping 桶 (692/693) + main 桶 (706), 总敞口 2x 名义值
- coordinator observe_only=true 下 capital_allocator 未实际去重
- bucket_map={fast_lane→main} 只覆盖 fast_lane, scalping 独立桶, 标的级冲突仍漂移

**最低成本可验证修复**: T1 (scalping 生 trace_id) + T2 (factor_* 落库) — 2-3 小时, 当晚开盘即可拉出"每笔 ARM SHORT 是哪个因子主导"的 SQL.
