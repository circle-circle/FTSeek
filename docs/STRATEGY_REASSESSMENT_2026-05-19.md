# 策略建议重新评估 — Karpathy 自我修正 (2026-05-19)

> **诚实承认**: 之前的 7 项建议中 **P2 和 P5 是统计陷阱** — 基于被 outlier 主导的 38/17 笔样本下了大改主战场的结论.
> **核心修正**: 必须先修 shadow A/B 基础设施, 收集足够样本 + 排除 outlier, 再做主战场迁移.

---

## 1. 致命统计陷阱揭示

### 1.1 6min-1h "alpha 区" 真相

我之前说: "6min-1h 38 笔 +$15,331, avg +$403, 是 alpha 区". 

**实际数据**:
| Top 3 笔 | 占比 |
|---|---:|
| US.ORCL SHORT 4/24 EXTERNAL_CLOSE | **+$17,883** (117%!) |
| US.LCID LONG 5/7 TRAILING_STOP | +$739 |
| US.SNDK SHORT 5/15 TRAILING_STOP | +$196 |
| **其他 35 笔合计** | **-$3,487** |

**剔除 ORCL 这 1 笔**:
- 38 笔 → 37 笔
- 总 PnL: $15,331 → **-$2,552**
- avg: +$403 → **-$69**

**真相**: 6min-1h 区间不是 alpha 区, **被 1 笔 EXTERNAL_CLOSE (人工/紧急触发, 非策略信号)主导**. 系统化策略在该区间实际亏损.

### 1.2 4-24h "长线区" 真相

| Top 4 笔 (4/27-30 集中) | 累计 |
|---|---:|
| US.BWXT LONG 4/30 TRAILING_STOP | +$3,165 |
| US.MSFT LONG 4/27 PRE_CLOSE_EXIT | +$3,142 |
| US.SPY LONG 4/27 PRE_CLOSE_EXIT | +$2,092 |
| US.NTES SHORT 4/27 PRE_CLOSE_EXIT | +$1,855 |
| **小计 4 笔** | **+$10,254** |
| **其他 13 笔合计** | **-$3,257** |

**洞察**: 4-24h 总 +$6,997 全部由 4 月底特定行情的 4 笔贡献, 5 月 13 笔实际亏损. **不是稳定 alpha, 是事件驱动**.

### 1.3 main SHORT "唯一 alpha" 真相

我之前说: "main SHORT 3 笔 +$1,415 胜率 66.7%, 应该加权 1.3x".

**实际**:
| date | symbol | pnl | hrs |
|---|---|---:|---:|
| 5/15 | US.ARM | +$1,219 | 1.2 |
| 5/15 | US.SNDK | +$196 | 0.4 |
| 5/18 | US.ARM | **$0** | 0.2 (反向开仓未成交) |

**真盈利样本: 只有 2 笔**. 而且 5/15 的 ARM SHORT +$1,219 占 86% (再次单笔主导).

**结论**: n=2 完全无法支持"加权 1.3x"决策. 这是赌博而非数据驱动.

---

## 2. 真正可靠 vs 不可靠的建议

| 建议 | 样本 | outlier 主导? | 可靠性 | 应该立即做? |
|---|---:|:---:|:---:|:---:|
| **P1 砍 <6min scalping 流量** | 461 | 否 (普遍亏损) | ✅ 充分 | ✅ 是 |
| ~~P2 主线 SHORT 加权 1.3x~~ | 3 (实 2) | 🔴 1 笔 86% | 🔴 不足 | ❌ 不! |
| P3 TP/SL 改 5:1 | 数学 | n/a | 🟡 需 shadow 验证 | ❌ 先 shadow 测 |
| P4 信号反向 (strength 80+ 减仓) | 298 | 否 (普遍微亏) | ✅ 充分 | ✅ 是 |
| ~~P5 主战场迁移 6min-1h~~ | 38 | 🔴 ORCL 117% | 🔴 不足 | ❌ 不! |
| P6 低价股黑名单 | LCID 21+TME 15 | 否 | ✅ 充分 | ✅ 已做 |
| P7 Chandelier 早激活 | 推断 | n/a | 🟡 需 shadow 测 | ❌ 先 shadow 测 |

**真正立即可做的**: 仅 P1 + P4 (实际数据已支持).
**需要 shadow 验证的**: P3 + P7.
**完全不能做的**: P2 + P5 (统计陷阱, 等数据).

---

## 3. Shadow 系统现状 — 必须先修

### 当前 (5/19 数据)
```
strategy        n_total  n_closed  pnl
baseline        13       10        -$247    ← 真实数据 (镜像 real)
no_chandelier   13       0         $0       ← 永远 closed=0 (黑洞!)
quick_exit      13       0         $0       ← 永远 closed=0 (黑洞!)
tighter_stop    13       0         $0       ← 永远 closed=0 (黑洞!)
```

### 黑洞根因 (上次 audit 已识别)
`shadow_mirror.py:163` 注释: "EOD 由 batch 任务回放" — 但**全代码库 0 个 EOD 任务**. 3/4 variant 永远停留 closed=0.

→ **当前 shadow A/B 对比根本不可用**. 用户提议"用 shadow 对比再调整"完全正确, 但前提是先修这个基础设施.

---

## 4. 重新设计 — 渐进式实施路径

### Stage 0: 修 shadow 基础设施 (本周必修)

**任务 1**: 实施 ShadowMirror EOD batch
- 文件: `scripts/shadow_eod_replay.py` (新)
- cron: 04:30 ET = 16:30 北京 (盘后)
- 逻辑: 扫所有 closed=0 的 non-baseline shadow, 按各 variant 规则用真实价格回放
  ```python
  for variant in ["no_chandelier", "quick_exit", "tighter_stop"]:
      for trade in unclosed_shadows[variant]:
          # 拉真实价格序列 (entry → exit_signal_time)
          prices = fetch_intraday_prices(trade.symbol, trade.entry_time, ...)
          # 按 variant 规则计算 exit
          exit_price = apply_variant_logic(variant, trade, prices)
          # 写入 shadow_trades closed=1
          db.update_shadow_close(trade_id, exit_price, ...)
  ```

**任务 2**: 加多模式并行 shadow
当前实盘 1 套 + shadow 4 variant. 应扩展:
```python
SHADOW_VARIANTS = [
    "baseline",                    # 现有, 1:1 镜像
    "no_chandelier",               # 现有, 待 EOD
    "quick_exit",                  # 现有, 待 EOD
    "tighter_stop",                # 现有, 待 EOD
    "tp_sl_5to1",                  # NEW: TP=2.5% SL=0.5% (P3 测试)
    "min_hold_6min",               # NEW: <6min 强制持仓 (P1 验证)
    "short_only",                  # NEW: 拒所有 LONG, 只跑 SHORT
    "early_chandelier_0_5atr",     # NEW: 0.5 ATR 激活 (P7 测试)
]
```

**任务 3**: 加 shadow_compare CLI 增强
```bash
python scripts/shadow_compare.py --days 14 --variant-diff --t-test
# 输出:
# variant            n_closed  pnl       avg     wr     vs_baseline_p
# baseline           42        -$247     -$5.9   8%     -
# no_chandelier      42        +$120     +$2.9   12%    p=0.08 (边缘)
# quick_exit         42        -$580     -$13.8  6%     p=0.42 (无显著)
# tighter_stop       42        +$340     +$8.1   15%    p=0.04 (显著)  ⭐
# tp_sl_5to1         42        +$520     +$12.4  18%    p=0.02 (显著)  ⭐
```

### Stage 1: 立即可做 — 高置信度小改动 (今晚后 1 周)

只做 P1 + P4 (数据充分):

**P1 砍 <6min** (n=461 充分):
```python
# scalping/exits.py 已实施 breakeven_after=6min ✅
# 进一步: scalping/engine.py 在 _execute_trade 加 vwap_deviation_pct 收紧
```

**P4 信号反向部分实施** (n=298 充分):
```python
# voting_aggregator.py 加诊断
if signal.strength >= 80:
    # 仅记录, 不改行为 (等 shadow tighter_stop 验证)
    record_observation("high_strength_observation", signal)
```

**不做** P2 / P5 (等数据).

### Stage 2: 数据收集 (2-4 周)

**等待信号**: 关键样本量达到统计显著水平
- main SHORT: 当前 2 笔 → 等 ≥ 15 笔 (3 周)
- 6min-1h 区间 (排除 EXTERNAL_CLOSE): 当前 ~3 笔 → 等 ≥ 30 笔
- 4-24h 区间 (排除 4/27-30 outlier): 当前 ~13 笔 → 等 ≥ 30 笔

**每周一统计** (LLM Failure Miner cron):
```sql
-- 每周一查 main SHORT 是否累计 ≥ 15 笔
SELECT COUNT(*) FROM trade_records
WHERE owner_strategy='main' AND direction='SHORT' 
  AND realized_pnl IS NOT NULL
  AND trade_date >= date('now', '-30 days');
```

### Stage 3: 渐进 A/B (4-6 周后)

**前置条件**: shadow 数据 + 实盘数据**双重确认**:
1. shadow 中 variant X 显著优于 baseline (t-test p < 0.05)
2. 实盘对应模式累计 ≥ 30 笔同向 PnL

**滚动放量**:
```
Week 1: 20% 流量切 variant X, 80% 保留现状
Week 2: 比较 20%/80% 1 周后业绩, 若 X 优 → 50%
Week 3: 50%/50% 1 周后, 若 X 优 → 80%
Week 4: 80%/20% 1 周后, 若 X 优 → 100%
任一周 X 劣 → 立即回滚
```

---

## 5. 与之前激进建议的对比

### 之前 (2026-05-19 上午, 错误的)

| 建议 | 我之前的语气 | 应有语气 |
|---|---|---|
| P2 主线 SHORT 加权 1.3x | "🔴 必须做" | "🟡 等 ≥ 15 笔再说" |
| P5 主战场迁移 6min-1h + 4-24h | "🔴 必须做" | "🟡 等 outlier 排除后再说" |
| 1 月预期 +$2-5k/周 | 直接断言 | 应该说 "shadow 显著后才能确认" |

### 现在 (重新评估后)

**真正立即做** (n 充分):
1. P1 砍 <6min (n=461)
2. P4 信号反向观察 (n=298)
3. P6 低价股黑名单 (已做)

**先修基础设施**:
1. Shadow EOD batch (3/4 variant 复活)
2. 加 4 个新 shadow variant (P3/P5/P7 全在 shadow 测)

**等数据 + shadow 验证**:
- 主线 SHORT 加权 (等 ≥ 15 笔 + shadow short_only 显著)
- 主战场迁移 (等 outlier-excluded n ≥ 30 + shadow 验证)
- TP/SL 比例 (shadow tp_sl_5to1 验证)
- Chandelier 早激活 (shadow early_chandelier 验证)

---

## 6. Karpathy 第一性的真正意思

我之前理解错了 Karpathy "show, don't tell":

**❌ 我之前**: "show me the data" → 看到 38 笔就以为充分, 直接下结论
**✅ Karpathy**: "show me the data, AND the test, AND the outliers, AND the time periods, AND the t-test"

真正的 Karpathy 风格:
1. 看 raw 数据 (我做了)
2. **检查 outlier** (我跳过了 — 这是错误)
3. **检查时间分布** (我跳过了 — 4-24h 集中在 4/27-30)
4. **统计显著性测试** (我没做)
5. **shadow A/B 对比** (用户正确指出)
6. **达到统计显著后**才下结论

---

## 7. 实施路径修正版

### 今晚部署 (已完成, 不变)
- L1/L2-B/L3 (LLM 链路)
- Pre-Entry Gate 默认 disabled
- 心跳 30min 推送
- Phase 1 5 改动 (但 Phase 1 中 B.2/C.1/C.2 都基于充分样本, 没有统计陷阱)

### 本周内 (新增)
1. **修 ShadowMirror EOD batch** (上次 audit 已识别, 必须修)
2. **加 4 个新 shadow variant** (tp_sl_5to1, min_hold_6min, short_only, early_chandelier)
3. P1 进一步 (vwap 收紧 1.2 → 2.0%)
4. **不做** P2 / P5 — 写入 deferred

### 2-4 周
- 数据收集 + LLM Failure Miner 周日 cron 输出
- 每周一统计 main SHORT / 6min-1h / 4-24h 样本累计
- 等关键指标达 ≥ 30 笔

### 4-6 周后
- t-test 比较各 shadow variant
- 显著优于 baseline 的渐进上线 (20% → 50% → 80% → 100%)

---

## 8. 一句话回答用户

> **承认错误**: 之前 P2 (主线 SHORT 加权) + P5 (主战场迁移) 基于被 outlier 主导的 3/38/17 笔样本, **是统计陷阱不是数据驱动**.
>
> **正确路径**: 用户提议的"shadow 对比"完全正确, 但前提是先修 shadow EOD batch 黑洞 (上次 audit 已识别). 路径应是:
> 1. **本周**: 修 shadow 基础设施 + 加 4 新 variant + 仅做 P1/P4 高置信度小改动
> 2. **2-4 周**: 数据收集, 等关键样本 ≥ 30 笔且 shadow t-test 显著
> 3. **4-6 周后**: 渐进 A/B (20% → 50% → 100% 滚动)
>
> **真正的 Karpathy 风格**: 不是"看数据就下结论", 是"看数据 + 看 outlier + 看时间分布 + t-test + shadow A/B 都通过才上线". 我之前过度自信了, 用户的反思救了我.
>
> **不变的事实**: P1 (砍 <6min) + P4 (信号反向) + P6 (低价股) 数据充分, 可立即做. 退出策略不必大改 (exit_policy.yaml v2.4 设计良好).
