# FTSeek-US 止损止盈策略审计 — 治理框架对齐报告

> 日期: 2026-05-21
> 审计范围: 美股止损止盈全链路 (config → ExitMonitor → ScalpingEngine → 实际交易数据)
> 治理框架: SYSTEM_OBJECTIVE.md v1.0
> 焦点对齐: F1 (买卖规范与止损止盈策略)

---

## 1. 治理 5 问

| # | 问题 | 回答 |
|---|------|------|
| 1 | 服务哪个焦点? | F1: 买卖规范与止损止盈策略 |
| 2 | 改什么指标? | 胜率 (31.3%→50%) + 日均收益 ($398→$500) |
| 3 | 怎么量化验证? | SQL: exit_reason 分布变化 + 日PnL追踪 (样本≥30) |
| 4 | 最坏情况? | 参数调优过宽→单笔亏损扩大; 过紧→胜率进一步下降 |
| 5 | 怎么回滚? | git revert (YAML 参数改动) / git checkout (代码改动) |

---

## 2. 当前数据基线 (268 笔, 20 天)

```
整体:   268 笔 | 胜率 31.3% | 总PnL +$7,970 | 日均 +$398
LONG:   118 笔 | 胜率 22.9% | 总PnL +$1,933 | 均盈 +$16.38
SHORT:  148 笔 | 胜率 38.5% | 总PnL +$12,021| 均盈 +$81.22
OPTION:   2 笔 | 胜率  0.0% | 总PnL -$5,984 | 已下线
```

### 2.1 按退出原因分布 (核心发现)

| exit_reason | 笔数 | 胜率 | 总PnL | 诊断 |
|-------------|------|------|-------|------|
| TRAILING_STOP | 76 | 52.6% | +$5,515 | ✅ 唯一盈利引擎 |
| tp_hit | 5 | 100% | +$747 | ⚠️ 几乎不触发 (1.9%) |
| PRE_CLOSE_EXIT | 22 | 27.3% | +$2,578 | ✅ 盘末退出有效 |
| sl_hit | 115 | 16.5% | -$2,023 | 🔴 最大亏损源 (42.9%交易) |
| STRUCTURE_EXIT | 33 | 30.3% | -$3,106 | 🔴 做空技术破位频繁触发 |
| FLASH_EXIT | 4 | 0.0% | -$1,263 | 🔴 闪崩退出全亏 |
| time_stop | 7 | 28.6% | -$376 | 🟡 时间止损低效 |
| BREAKEVEN_STOP | 1 | 0.0% | -$6,015 | 🔴 单笔灾难 |
| QUICK_EXIT (期权) | 2 | 0.0% | -$5,984 | 已下线 |

### 2.2 pnl_pct 区间分布 (揭示核心矛盾)

| 区间 | 笔数 | 说明 |
|------|------|------|
| +0~2% | 84 笔 (100%盈) | 盈利集中在极小区间 |
| -0~2% | 184 笔 (100%亏) | 亏损也集中在极小区间 |

**关键发现**: 系统盈利和亏损都在 0~2% 的极窄区间内。这说明:
- 止损线太紧 (default -3%), 频繁在噪声中被扫出
- 止盈线太远 (default +10%), 几乎永远触不到
- R:R 严重倒挂: 实际均盈 ~1% vs 止损 -3% → R:R = 1:3 (反向)

---

## 3. 止损止盈逻辑架构审计

### 3.1 双引擎并行 (代码事实)

```
ExitMonitor (position_exit_monitor.py)  ← 主线持仓
  规则优先级: Flash → DayTrade → ScaleOut → TrailingStop → TimeStop → PreClose
  60s 扫描间隔, 120s 冷却

ScalpingEngine (scalping/engine.py)     ← 短线持仓
  FixedExit (tp=1.8%, sl=1.2%) + TimeExit (5min) + AdaptiveExit + ATR Exit
  独立 monitor_loop, 与 ExitMonitor 互斥
```

### 3.2 各策略实际参数 (从代码 + YAML 提取)

| 策略/场景 | 止损 | 止盈 | R:R | 问题 |
|-----------|------|------|-----|------|
| dynamic_exit default | -3% | +10% | 1:3.3 | 止盈太远, 止损太紧 |
| dynamic_exit strong_trend_up | -8% | +25% | 1:3.1 | 合理但样本极少 |
| dynamic_exit range_bound | -3.5% | +5% | 1:1.4 | 最佳 R:R |
| dynamic_exit strong_trend_down | -3% | +4.5% | 1:1.5 | 合理 |
| Scalping FixedExit | -1.2% | +1.8% | 1:1.5 | 过紧, 噪声频繁扫单 |
| Scalping base (P0重写后) | -2.0% | +2.5% | 1:1.25 | 改善后合理 |
| Short Stock (正股做空) | -8% | +10% | 1:1.25 | 合理 |
| Short Option (期权) | -25% | +15% | 1:0.6 | 倒挂, 已下线 |

### 3.3 退出优先级链 (ExitMonitor)

```
1. Flash Exit (60s 内 ATR 突变) — 紧急退出
2. Day Trade Exit (短线主动管理)
3. Scale Out (分批止盈, 5%/10% 各50%)
4. Trailing Stop (ATR Chandelier + 保本) ← 核心盈利引擎
5. Time Stop (10 天超时, 盈利豁免)
6. Pre-Close Exit (盘末强制平仓)
```

**问题**: 优先级链中 **没有硬止损 (sl_hit)** 的独立规则。
sl_hit 产生于 `_check_trailing_stop` 中的 ATR 止损线触穿 — 但 ATR 动态止损在入场初期等同于固定止损 (peak=entry_price, trail 未激活)。

### 3.4 sl_hit 根因分析

```
115 笔 sl_hit, 胜率 16.5%, 总亏损 -$2,023

LONG: 53笔 胜率7.5% 均亏-$0.16% (几乎每笔都亏)
SHORT: 62笔 胜率24.2% 均亏-$0.11%

根因: ATR 止损在入场初期 = entry_price × (1 - 3%) = 固定 -3% 止损
      美股日内波动 2-3% 是常态, -3% 在噪声范围内
      → 价格在 -3% 附近反复震荡时被扫出, 随后反弹
```

---

## 4. 核心目标可行性判定

### 4.1 目标: 胜率 ≥ 50%

```
当前: 84 胜 / 184 负 = 31.3%
需减少 100 笔亏损才能达到 50%

sl_hit 占亏损 115 笔 (62.5%), 但:
  - 即使 sl_hit 胜率从 16.5% → 30%, 整体 WR 也仅到 37.1%
  - 需要将 sl_hit 从 115 笔减少到 ~50 笔 (减半), 同时保持其他不变
```

**结论**: 🔴 当前逻辑 **无法实现** 50% 胜率。
瓶颈不在止损止盈参数, 而在 **入场质量** — 大量低质量入场在噪声中被止损扫出。

### 4.2 目标: 日均收益 ≥ $500

```
当前: 日均 $398 (20天均值)
差距: $102/天

盈利引擎 (TRAILING_STOP + tp_hit): 日均贡献 $522 ← 已超过 $500
但被 sl_hit (-$101/天) + STRUCTURE_EXIT (-$155/天) 拖累

模型: 消除灾难退出后, 日均可达 $1,154
但这是理想化 — 实际需要渐进验证
```

**结论**: 🟡 日均 $500 **有条件可行**, 但需要:
1. 消除 BREAKEVEN_STOP / QUICK_EXIT 类灾难 (一次性事件)
2. 将 STRUCTURE_EXIT 亏损减半 (-$3,106 → -$1,500)
3. 将 sl_hit 频率降低 30% (115 → 80 笔)

### 4.3 综合判定

| 指标 | 当前 | 目标 | 判定 | 路径 |
|------|------|------|------|------|
| 胜率 | 31.3% | ≥50% | 🔴 不可行 | 需先提升入场质量, 再调止损参数 |
| 日均 | $398 | ≥$500 | 🟡 有条件可行 | 消除灾难退出 + 收紧 STRUCTURE_EXIT |

---

## 5. 止损止盈逻辑的 P0/P1/P2 缺陷

### P0: sl_hit 止损过紧 (115 笔, -$2,023)

**根因**: dynamic_exit default 止损 -3%, 在美股日内噪声中频繁触发。
**代码位置**: `position_exit_monitor.py:1358` → `compute_stop_lines()`
**影响**: 占总交易 42.9%, 是亏损的最大单一来源。
**建议**: 🟡 (YAML 参数调优) 将 default 止损从 -3% 放宽到 -4%~5%, 给噪声留空间。
**验证**: 观察 30 笔新交易后 sl_hit 频率是否下降。

### P1: STRUCTURE_EXIT 做空频繁触发 (33 笔, -$3,106)

**根因**: 做空技术破位检查 (`_check_trailing_stop:1374`) 在 resistance_20d 有效时,
         current_price > resistance + 0.3×ATR 即触发, 门槛过低。
**代码位置**: `position_exit_monitor.py:1374-1420`
**影响**: 33 笔做空被技术破位强制平仓, 均亏 -$94。
**建议**: 🟡 提高 breach_atr_mult 从 0.3 → 1.0, 或增加 min_loss_pct 从 0.5% → 2%。
**验证**: 观察 STRUCTURE_EXIT 触发频率是否降低 50%。

### P1: tp_hit 几乎不触发 (5 笔, 1.9%)

**根因**: default 止盈 +10% 太高, 绝大多数交易在 +2% 内就被 trailing stop 或 time stop 清出。
**代码位置**: `unified_config.yaml:883` take_profit: 0.1
**影响**: 止盈形同虚设, 利润无法充分捕获。
**建议**: 🟡 将 default 止盈从 10% 降到 5%, 让 tp_hit 成为有效退出路径。
**验证**: 观察 tp_hit 触发率是否从 1.9% 提升到 10%+。

### P2: Scale Out 分批止盈 R:R 倒挂

**根因**: 5% 触发卖 50% + 10% 触发卖 50%, 加权均盈 ~2.9% vs 止损 -4.5%。
**代码位置**: `unified_config.yaml:884-890`
**影响**: 分批止盈拉低了整体 R:R。
**建议**: 将第一级从 5% 提到 7%, 第二级从 10% 提到 12%。

### P2: FLASH_EXIT 全亏 (4 笔, -$1,263)

**根因**: Flash Exit 阈值 1.0×ATR, 在开盘/高波动时段误触发。
**代码位置**: `position_exit_monitor.py:618`
**影响**: 4 笔全亏, 均亏 -$316。
**建议**: 增加最小亏损门槛 (如 -1%) 才触发 Flash Exit。

---

## 6. 结论与建议

### 6.1 直接回答: 当前逻辑能否实现核心目标?

**胜率 ≥ 50%**: 🔴 **不能。** 瓶颈不在止损止盈参数, 而在入场质量。
即使止损参数全部调优, 低质量入场仍会在噪声中被扫出。
需要入场侧同步改进 (提高 signal_confidence 门槛, 减少低置信度交易)。

**日均 ≥ $500**: 🟡 **有条件可行。** 盈利引擎 (TRAILING_STOP) 已日均贡献 $522。
关键是减少拖累 (STRUCTURE_EXIT, 灾难性单笔) 而非大幅提升盈利。

### 6.2 建议执行路径 (止血 > 优化)

```
Phase 0 (止血, 🟡 YAML调优, 可自主):
  1. dynamic_exit default 止损: -3% → -4.5%
  2. dynamic_exit default 止盈: 10% → 5%
  3. STRUCTURE_EXIT breach_atr_mult: 0.3 → 1.0
  预期: sl_hit 频率 -30%, STRUCTURE_EXIT 频率 -50%, 日均 +$100

Phase 1 (优化, 🟡 YAML调优, 可自主):
  4. Scale Out 第一级: 5% → 7%, 第二级: 10% → 12%
  5. FLASH_EXIT 增加最小亏损门槛 -1%
  预期: R:R 从 1:3 改善到 1:1.5

Phase 2 (入场质量, 🔴 需审批):
  6. 提高入场 signal_confidence 门槛
  7. 减少低分入场 (score < 60)
  预期: 入场质量提升 → sl_hit 自然减少 → 胜率提升
```

---

## 附录 A: 关键代码位置

| 模块 | 文件 | 行号 | 职责 |
|------|------|------|------|
| 退出调度 | coordinator/exit_dispatcher.py | 全文 | Phase A 路由中心 |
| 主线退出 | risk/position_exit_monitor.py | 255-612 | scan_positions 6规则 |
| Trailing Stop | risk/position_exit_monitor.py | 1280-1441 | ATR动态止损+Chandelier |
| Flash Exit | risk/position_exit_monitor.py | 618-692 | 紧急退出 |
| Scalping Exit | scalping/exits.py | 17-86 | FixedExit tp/sl |
| Scalping Engine | scalping/engine.py | 全文 | 独立 monitor_loop |
| Short Stock Exit | execution/short_strategy.py | 467-511 | 正股做空退出 |
| Short Option Exit | execution/short_strategy.py | 228-279 | 期权做空退出 (已下线) |
| 参数配置 | config/unified_config.yaml | 880-960 | dynamic_exit 各 regime |
| Scalping 参数 | config/unified_config.yaml | 1534-1680 | scalping phases |

## 附录 B: 盈利引擎 TRAILING_STOP 数据

```
76 笔 | 胜率 52.6% | 总PnL +$5,515 | 均盈 +$72.57
LONG:  41 笔 | 胜率 43.9% | 均盈 +$45.80
SHORT: 35 笔 | 胜率 62.9% | 均盈 +$103.92

结论: SHORT + TRAILING_STOP 是最强盈利组合
建议: 保护这个组合的退出逻辑, 不要过度收紧 SHORT 的 trailing stop 参数
```
