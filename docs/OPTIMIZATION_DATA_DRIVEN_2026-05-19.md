# 数据驱动优化建议 — 提升胜率与收益 (2026-05-19)

> **数据基础**: 5/12-5/18 共 6 个交易日, 117 笔实际成交, $1751 累计净亏
> **核心矛盾**: 信号系统反向 — LONG 73 笔 -$3196 (2.7% 胜率) vs SHORT 44 笔 +$1444 (15.9% 胜率)
> **优化路径**: 5 维度 14 项数据驱动建议, 预期 7-30 天胜率 7%→25%, PnL 转正

---

## 1. 整体业绩快照 (5/12-5/18)

| 日期 | 笔数 | 胜率 | PnL | 最大单笔 |
|---|---:|---:|---:|---|
| 5/12 | 26 | 11.5% | **-$1421** | 灾难: 1 笔 -$743 FLASH_EXIT |
| 5/13 | 31 | 6.5% | -$341 | 异常: 大笔 +$229 / -$395 |
| 5/14 | 31 | 6.5% | -$616 | 全场 sl_hit |
| **5/15** | **14** | **14.3%** | **+$891** | **唯一盈利日**: ARM SHORT main +$1219 |
| 5/18 | 15 | 0% | -$265 | TME LONG -$153 |

**结论**: 6 天 5 输 1 赢, 仅 5/15 因主线 SHORT 中长线策略盈利.

---

## 2. 维度分析与建议

### 维度 A: 链路 (Architecture/Flow)

#### A.1 🔴 信号过滤反向: SHORT 被拒, LONG 全过 (124 笔被拒)

**数据证据**:
| 日 | by_direction_strength | rejected SHORT | 实际 LONG | 实际 SHORT |
|---|---|---:|---:|---:|
| 5/14 | 全是 SHORT(s=14/15/19) | 76 | 多笔 LONG | 极少 SHORT |
| 5/15 | 全是 SHORT(s=11/13/14/19) | 35 | — | ARM SHORT +$1219 ⭐ |
| 5/18 | 全是 SHORT(s=14/15/19) | 13 | LCID/TME LONG 全亏 | 0 SHORT 通过 |

**根因** (`order_executor.py:792`):
```python
short_min_strength = 20  # 绝对强度底限
if signal.strength < short_min_strength:
    return False  # 拒绝
```
SHORT 信号语义是反转, 原始 `strength` 越低代表 SHORT 越强 (effective=100-strength). 但门禁仍以 raw strength<20 拒绝 → **拒掉了 effective=85+ 的强 SHORT 信号**.

**建议 A.1**: 删除 `short_min_strength` 绝对底限 (line 792-803), 仅保留 effective_strength 门 (line 804 `< 65`).
- 风险: 极弱信号可能进, 但已有 effective_strength 兜底
- **预期影响**: 124 笔被拒 SHORT 中估计 30+ 笔可放行, 按 SHORT 历史 15.9% 胜率 + ARM 单笔 +$1219 体量, 累积可补 +$2000-3000

#### A.2 🔴 regime 落库断链 (102/116 笔 = 88% 写为 UNKNOWN)

**数据证据**:
```
market_regime  n    pnl
UNKNOWN        102  -$1505
unknown        13   -$247
RANGE          1    $0
TREND_UP/DOWN  0
```
但 `market_data_router` 日志输出 `regime=TREND_UP/RANGE` 等 — Y1 RegimeDetector 工作了, 但**没写到 trade_records.market_regime**.

**根因**: `order_executor` 写 trade_records 时 `market_regime` 字段未从 router context 传入.

**建议 A.2**: 在 `_close_position` 写 trade_record 前从 `router.get_full_market_context(symbol)['regime']` 取值; 同时回填历史脏数据 (trade_records JOIN router 历史日志).
- **预期影响**: regime-aware A/B 测试成为可能 (现在 88% 数据 UNKNOWN, 复盘失效)

#### A.3 ⚠️ scalping 主导但未连 PostTradeHook 链路

**数据证据**: 117 笔中 scalping ~110 笔, main 仅 6 笔. PostTradeHook 设计来 trigger thesis_inferer / shadow / push, 但只在 main 路径调用. **scalping 路径绕过 PostTradeHook → 110 笔无 LLM 归因**.

**建议 A.3**: scalping `_execute_trade` 后调 `post_trade_hook.on_position_opened(...)` 完成 attribution + thesis (现在仅 record_position_owner 给 coordinator). 已修 P0-3 (5/19) 让 attribution 路径走通, 但 thesis_inferer/shadow_mirror 的 hook 串联未连.

---

### 维度 B: 策略 (Entry/Exit Signals)

#### B.1 🔴 持仓时长 U 形: <6min 与 >8h 双灾难

**数据证据**:
| 持仓 | 笔数 | 胜率 | PnL |
|---|---:|---:|---:|
| <6min | 16 | **0%** | **-$973** |
| 6-30min | 2 | 50% | +$196 |
| **30min-2h** | **5** | **60%** | **+$742** ⭐ |
| 2-8h | 5 | 20% | -$357 |
| >8h | 89 | 4.5% | -$1359 |

**核心洞察**: **30min-2h 是真正的 alpha 区间** (5 笔 60% 胜率, +$742). <6min 是 scalping 摩擦灾难, >8h 是过夜风险.

**建议 B.1**:
- **scalping 强制最小持仓 6 min** (避免 <6min 全损区), 现 2-5min 提前止损路径直接禁掉
- **主线 trailing 强制最大持仓 4h** (避免 >8h 减少胜率), 现 time_stop 已禁用 (O3) 改为软上限
- **预期影响**: 减少 <6min 16 笔损失 → 即可减亏 $900+

#### B.2 🔴 scalping 低价股摩擦吞利 (LCID 21 笔 4.8% 胜率)

**数据证据**: 5/18 LCID $5.75, 摩擦 $17.99, gross_pnl 0.00%, **net -0.09% 全部由摩擦造成**. TME $9.06 摩擦 $11.58, 摩擦/价值 = 0.058%.

**LCID 5/12-18 业绩**: 21 笔, **4.8% 胜率, -$656 累计**.

**建议 B.2**: 
- **scalping 排除单价 < $20 的标的** (LCID/TME/RIVN), 加入 watchlist 黑名单
- 或加 "expected_pnl > 2× friction" 门禁 (Karpathy: 利润必须显著大于成本)
- **预期影响**: 直接砍掉 LCID/TME 30+ 笔损失, 按平均 -$30/笔 估算可减亏 $900+

#### B.3 ⚠️ FLASH_EXIT 1 笔 -$743 (单笔致命)

**数据证据**: 5/12 QCOM LONG FLASH_EXIT -$743, 0.0 hours (秒杀级别).

**建议 B.3**: FLASH_EXIT 单笔限额 — 持仓 < 1min 触发 FLASH_EXIT 时, 限制最大损失 < 0.5% × equity (不能让 1 笔 wipe 整周收益).

---

### 维度 C: 逻辑 (Rules/Gates)

#### C.1 🔴 LONG 全军覆没 (73 笔 2.7% 胜率)

**数据证据**:
- LONG 73 笔, 仅 2 胜, **PnL -$3196**
- 大输标的: QCOM/LCID/TME/BA/UBER/CRM/ORCL/NFLX (全是 LONG 主导)
- 期间市场是震荡下行 (vix 17-18, 5/12-18 SPY 微跌)

**核心洞察**: 系统在弱势市场被强迫做多 (scalping 默认 LONG 偏向). 配合 A.1 SHORT 被拒, 等于"在熊市做多 + 拒绝做空".

**建议 C.1**:
- **scalping 加 regime gate**: regime=TREND_DOWN 时禁止 LONG (仅 SHORT 或观望)
- **scalping 加 SPY momentum gate**: 5min SPY 动量 < 0 时禁 LONG
- **预期影响**: 砍掉 70+ 笔不该开的 LONG, 按平均 -$45/笔估算可减亏 $3000+

#### C.2 🔴 熔断 sl_hit 阈值过松 (-0.0% 也算 sl_hit)

**数据证据**: 5/18 LCID gross 0.00% (摩擦 -0.09%), 计为 sl_hit; 3 次 sl_hit → 60min CB 熔断. 5/18 触发 3 次熔断 (22:34/00:22/02:20).

**建议 C.2**: `app/scalping/circuit_breaker.py` — gross_pnl > -0.1% 不计 consecutive_loss (噪声波动不算亏).
- **预期影响**: 减少 60min × 3 = 180min 失去交易窗口, 增加 30min-2h alpha 区间命中机会

#### C.3 ⚠️ Symbol 级日内损失上限缺失

**数据证据** (daily_review.symbol_loss_guard_triggers):
- TME 5/18: 2 笔连续亏 -$174, 触发 block (好)
- LCID 5/13: 已亏 -$394 但无 block 记录 (loss_guard 阈值过松?)

**建议 C.3**: SymbolLossGuard 阈值 5/18 已是 `consecutive_losses>=2`, 但 LCID 5/13 多笔分散亏不命中"连续". 改为 "**单标的当日累计净亏 > $200 直接 block**".

---

### 维度 D: 归因 (Attribution / Reflection)

#### D.1 🔴 5 因子 noise_pnl 持续 -$15,000+

**数据证据** (6 天 daily_review):
```
noise_pnl 持续 -$15,000 ~ -$17,000
overtrading +$1,100 ~ +$1,500
missed_signals +$4,500 ~ +$5,500
```

**核心洞察**:
- "**噪声交易**" (低质量小持仓) 持续吞利 — 主因 scalping LCID/TME
- "**错过信号**" 累积 +$4,500 机会成本 — 主因 SHORT 被拒
- behavioral_score=84 grade=B — **指标稳定但毫无诊断价值** (固定不变), 应该让 score 反映真实改进

**建议 D.1**:
- **定义 7d 滚动 noise_pnl 阈值** — 超过 -$3000 自动触发"暂停低质量入场"逻辑
- **missed_pnl 反向反馈**: 错过信号 7d 累计 > +$2000 → 自动放松 SHORT 门禁
- behavioral_score 加入"**今日相对昨日改善%**"维度

#### D.2 🔴 ShadowMirror 4 variant 3/4 失效 (黑洞)

**数据证据**: 5/18 baseline 10/13 closed, 其他 3 variant (no_chandelier/quick_exit/tighter_stop) 全部 closed=0. 已知设计黑洞 — EOD batch 任务从未实施.

**建议 D.2** (上次审计已提): 加 EOD 04:00 ET 批任务 `replay_shadow_variants()`. 否则 1 周积累后做 t-test 比较 4 种参数效果根本无法实施.

#### D.3 ⚠️ 5/15 唯一盈利日 anatomy

**数据证据**: 5/15 +$891 来自:
- ARM SHORT main 1.2h TRAILING_STOP +$1219 (主线策略)
- SNDK SHORT main 0.4h TRAILING_STOP +$196 (主线策略)
- 其他全是小额 0 PnL (ghost cleanup / sl_hit)

**核心洞察**: **真正盈利路径是主线 SHORT + 30min-1.5h trail**. scalping 即使盈利日也是 0 净贡献.

**建议 D.3**: 加大主线策略权重, 减少 scalping 频率. 量化:
- 现状: scalping ~110 笔, main ~6 笔
- 目标: scalping ≤ 30 笔/日 (削减 LCID/TME), main ≥ 5 笔/日 (放开 SHORT)

---

### 维度 E: LLM (Intelligent Decisions)

#### E.1 🔴 LLM 历史成功率 0% (47 笔 test + 实际 20 笔全失败)

**数据证据**:
```
feature           model            n   ok   cost
test_feature_xyz  qwen             47  47   $0.047
news_classifier   MiniMax-M2.5     15  0    $0
thesis_inferer    deepseek-v4-pro  5   0    $0
```

- thesis_inferer 5 次 latency=149ms (太快 → cache miss 立即超时? 还是 0 内容?)
- news_classifier 15 次 ok=0 (latency 202ms — 也是太快)
- D-DAY (5/17) 改造的 model_call 路径**生产 0 触发** (5/18 PostTradeHook stats=0)

**建议 E.1**:
- 立即排查 thesis_inferer / news_classifier 5/15 之前的失败原因 (响应被 schema 拒绝? API key 问题?)
- model_call.py 加 `success_rate < 50% 自动降级到 fallback model + 报警`
- 启用前在测试环境跑 10 笔模拟 (主线 entry signal 注入), 验证 PostTradeHook → thesis_inferer 链路

#### E.2 🔴 主线策略缺 LLM 决策 (PostTradeHook 0 触发)

**数据证据**: PostTradeHook stats `{submitted: 0, completed: 0, failed: 0}` × 4 次 shutdown — 整个交易日 thesis 推断 0 次.

**建议 E.2** (与 A.3 同):
- 在 scalping 路径也接入 PostTradeHook (现在只主线), 让 110 笔 scalping 也产生 thesis 推理数据
- 历史回填 5/12-5/18 100+ 笔 trade 用 batch LLM thesis (`thesis_inferer` 跑离线)
- **预期影响**: 1 周后有足够 thesis 数据做 "thesis 一致 vs 不一致" 的胜率 A/B

#### E.3 ⚠️ premarket_planner 单次/日, 无效果反馈

**数据证据**: premarket_planner 设计每日 03:00 PT 生成 battle_plan, 但 5/18 实际只生成了 1 次. 没有"昨日实战 vs plan 偏差" 反馈.

**建议 E.3**:
- premarket_planner 输入加昨日 outcome (实际 PnL / 拒绝原因 / regime 分布)
- 每日 plan 生成后自动对比"plan.targets vs 昨日实际 active_pool" 一致性

---

## 3. 实施优先级 + 量化预期

### Phase 1 (本周内, 立即收益): 减少负贡献

| # | 建议 | 改动 | 预期 1 周影响 |
|---|---|---|---|
| **B.2** | scalping 排除 < $20 标的 (LCID/TME) | watchlist 黑名单 | **+$900** (减亏) |
| **A.1** | 删除 short_min_strength<20 门 | 删 8 行 | **+$500** (放行 SHORT) |
| **C.1** | scalping regime=TREND_DOWN 禁 LONG | 新增 1 if | **+$500** (减 LONG 灾难) |
| **C.2** | 熔断 -0.0% 不计 sl_hit | 加阈值检查 | **+$200** (恢复交易窗口) |
| **B.1** | scalping 强制 min hold 6min | 加时间检查 | **+$300** (减摩擦) |

**Phase 1 累计预期**: **+$2,400** vs 当前周均 -$300, 即从 -$300 → +$2,100 转正.

### Phase 2 (2 周内, 系统升级): 提高 alpha 捕获

| # | 建议 | 改动 | 预期 1 月影响 |
|---|---|---|---|
| **D.3** | 加大主线 SHORT 权重 (放开 + 加仓) | confidence/strength 阈值 | +$1,500 |
| **A.2** | regime 落库 + 历史回填 | 1 行 + SQL | enable A/B test |
| **D.2** | ShadowMirror EOD batch 实施 | 新 cron 任务 | enable variant 优化 |
| **A.3+E.2** | scalping 路径接 PostTradeHook | 1 调用 | thesis 全覆盖 |
| **E.1** | 排查 LLM 全失败 + 降级机制 | model_call 改造 | LLM 上线 |

### Phase 3 (1 月+, 数据沉淀): 持续优化

| # | 建议 | 何时启用 |
|---|---|---|
| **D.1** | 5 因子动态阈值 | 7 天后 (3 周数据足) |
| **B.3** | FLASH_EXIT 单笔限额 | regime 落库后 |
| **C.3** | Symbol 累计净亏 block | symbol_loss_guard 升级 |
| **E.3** | premarket_planner 反馈环 | LLM 链路稳定后 |

---

## 4. 监控指标 (本周复盘看这些)

```sql
-- 每日 6:00 自动运行, 钉钉推送
-- 1. 方向均衡性 (期望 LONG/SHORT 比 1.5:1, 现在 73:44=1.7:1 但胜率反向)
SELECT direction, COUNT(*), 
       ROUND(100.0*SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END)/COUNT(*), 1) wr
FROM trade_records WHERE trade_date = date('now', 'localtime')
GROUP BY direction;

-- 2. 持仓时长分布 (期望 30min-2h 占比 ≥ 30%)
SELECT 
  CASE WHEN holding_hours < 0.1 THEN '<6min'
       WHEN holding_hours < 2 THEN '6min-2h'
       ELSE '>2h' END bucket,
  COUNT(*), ROUND(SUM(realized_pnl),2) pnl
FROM trade_records WHERE trade_date = date('now', 'localtime')
GROUP BY bucket;

-- 3. SHORT_STRENGTH_WEAK 拒绝数 (期望 ≤ 5/天 after Phase 1 fix)
SELECT COUNT(*) FROM rejections 
WHERE date(timestamp) = date('now') AND reason_category = 'SHORT_STRENGTH_WEAK';

-- 4. scalping 低价股交易数 (期望 0 after Phase 1 fix)
SELECT COUNT(*) FROM trade_records 
WHERE trade_date = date('now') AND owner_strategy='scalping' AND entry_price < 20;

-- 5. main 主线 SHORT 笔数 (期望 ≥ 3/日)
SELECT COUNT(*) FROM trade_records 
WHERE trade_date = date('now') AND owner_strategy='main' AND direction='SHORT';
```

---

## 5. 一句话回答

> **核心矛盾**: 系统在熊市被强迫做多 — LONG 73 笔 2.7% 胜率 vs SHORT 44 笔 15.9% 胜率, 但 124 个 SHORT 信号被自家 strength<20 门拒掉, scalping LCID/TME 低价股摩擦持续吞利. 真正 alpha 路径 = **主线 SHORT 30min-2h trailing** (5/15 ARM +$1219 即此模式).
>
> **Phase 1 (本周修)**: 5 项小改动 → 预期 7 天累计 +$2,400 (从周均 -$300 转 +$2,100).
> **Phase 2 (2 周内)**: 系统升级, regime 落库 + ShadowMirror EOD + LLM 链路, 启用真正的数据驱动迭代.
> **Phase 3 (1 月+)**: 5 因子动态阈值 + 反馈环.
