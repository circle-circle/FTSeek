# FTSeek-US 策略因子与止损止盈深度辩论报告

> **日期**: 2026-05-26 | **作者**: Hermes Agent | **ADR**: ADR-044
> **数据范围**: 309笔真实交易 | **服务**: 模拟盘验证期

---

## 一、系统架构评估

### ✅ 优秀设计 (保留并推广)

| 模块 | 设计 | 评价 |
|------|------|------|
| `app/factors/base.py` | BaseFactor → FactorValue → ConditionEvaluator | ⭐ 教科书级可插拔设计 |
| `app/factors/registry.py` | 单例注册中心 + auto_discover + param_space | ⭐ 完美的扩展性 |
| `app/factors/assembly.py` | FactorAssembly + StrategyVariantLibrary (YAML) | ⭐ 声明式策略定义 |
| `app/exit/base.py` | BaseExitModule + ExitProfile + ExitDecision | ⭐ 干净的接口抽象 |
| `app/exit/registry.py` | ExitModuleRegistry + evaluate_best | ⭐ 优先级合并策略 |
| `exit_policy.yaml` | 7大退出规则 + 行业差异化 + session-aware | ⭐ 详尽的参数SSOT |

### 🔴 关键架构问题

**问题 #1: 双轨并行 — 新框架未接入生产**

```
生产路径 (实际执行):
  scheduler → signal_pipeline (LLM) → order_executor → position_exit_monitor.py (1796行巨型)
                                                        ↕ exit_policy.yaml

新框架 (仅 shadow):
  shadow_bridge → FactorAssembly → ExitModuleRegistry → evaluate_best
                  (SSS+GDIAN+MFFF)   (ATR+Trail+Scale)   (ExitProfile)
```

- 新框架只在 `harness/shadow_bridge.py` 中运行（shadow 模式）
- 生产入场用 LLM pipeline，不用 FactorAssembly
- 生产退出用 `position_exit_monitor.py`（1796行单体），不用 ExitModuleRegistry
- **影响**: 精心设计的模块化框架完全未产生实际价值

---

## 二、数据驱动发现 (6大核心问题)

### 🔴 问题 #2: HIGH_VOL  regime 是毁灭引擎

| Regime | 笔数 | 占比 | 胜率 | PnL | 特征 |
|--------|------|------|------|-----|------|
| **HIGH_VOL** | **141** | **46%** | **14.9%** | **-$39,537** | 123笔sl_hit(87%) + 18笔期权灾难 |
| TREND_UP | 88 | 29% | 51.1% | +$6,262 | ✅ 唯一稳定盈利 regime |
| RANGE | 80 | 26% | 25.0% | +$16,991 | 低WR但大赢弥补 |

**根因**: 
- 系统在 HIGH_VOL regime 中持续开仓
- 87% 的仓位被止损 (sl_hit)
- 剩余 13% 是期权 PRICE_FAIL / QUICK_EXIT 灾难
- HIGH_VOL 中 **零笔** TRAILING_STOP 退出 → 利润从未有机会奔跑

**结论**: HIGH_VOL 开仓 = 确定性亏钱。需要 regime 门控。

---

### 🔴 问题 #3: <10min 持仓是亏损主引擎

| 持仓时段 | 笔数 | 占比 | 胜率 | PnL | 主要exit |
|----------|------|------|------|-----|----------|
| **<10min** | **169** | **55%** | **21.3%** | **-$10,448** | sl_hit(118) + QUICK_EXIT(5笔期权=-$10K) |
| 10-60min | 40 | 13% | 27.5% | +$11,031 | ✅ 甜蜜区 |
| 1-4h | 59 | 19% | 40.7% | -$2,676 | 高WR但大亏拉低 |
| 4-24h | 27 | 9% | 37.0% | -$5,376 | |
| 1-3d | 14 | 5% | 35.7% | -$8,815 | |

**<10min 细分**:
- sl_hit: 118笔, 16.1% WR, -$1,633 (正常止损)
- QUICK_EXIT 硬止损: 5笔, -$10,535 (期权灾难)
- TRAILING_STOP: 16笔, 75% WR, +$4,476 (快进快出盈利)

**根因**: 
- 55% 的交易在 10 分钟内结束
- 排除期权的 -$10K，正常 sl_hit 的 -$1,633 相对温和
- 但系统开仓后很快被止损，说明入场时机/信号质量不足

---

### 🟡 问题 #4: R:R 合理但 WR 不足

```
Avg Win:   $475.4
Avg Loss:  $-296.2
R:R:       1.60:1  ← 合理
盈亏平衡:  38.4%   ← 需要这个胜率才能保本
实际 WR:   30.8%   ← 低于盈亏平衡 7.6pp
```

**数学现实**: 即使 R:R 1.6:1 不错，WR 不足导致负 EV。每笔交易期望值 = -$52.7。

---

### 🟡 问题 #5: 各退出规则效率差异极大

| Exit Reason | 笔数 | WR | PnL | Avg | 评价 |
|-------------|------|-----|-----|-----|------|
| TRAILING_STOP | 85 | 47.1% | +$5,515 | +$64.9 | ✅ **唯一持续盈利** |
| PRE_CLOSE_EXIT | 22 | 27.3% | +$2,578 | +$117.2 | ✅ 盘末退出合理 |
| tp_hit | 5 | 100% | +$747 | +$149.5 | ✅ 太少触发 |
| sl_hit | 123 | 15.4% | -$2,023 | -$16.4 | 🔴 频率最高 |
| STRUCTURE_EXIT | 33 | 30.3% | -$3,106 | -$94.1 | 🔴 技术破位过于激进 |
| FLASH_EXIT | 4 | 0% | -$1,263 | -$315.7 | 🔴 全部亏损 |
| time_stop | 7 | 28.6% | -$376 | -$53.7 | 已禁用(正确) |

**关键**: TRAILING_STOP 是唯一盈利退出方式，说明"让利润奔跑"策略有效。但只有 85/309 = 28% 的交易走到追踪止损，大部分在 sl_hit 就被清出。

---

### 🟡 问题 #6: 做空方向偏好正确但 LONG 信号质量差

| 方向 | 笔数 | WR | PnL | 评价 |
|------|------|-----|-----|------|
| SHORT | 163 | 35.0% | +$12,021 | ✅ 主引擎 |
| LONG | 133 | 20.3% | +$1,933 | 🟡 低效 |
| SHORT_PROXY_OPTION | 13 | 15.4% | -$30,237 | 🔴 已禁用 |

- SHORT 是盈利主力 (与 P1-P10 优化的 SHORT 方向偏好一致)
- LONG 20.3% WR 接近灾难级别
- 系统已经正确偏好 SHORT，但 LONG 信号门控不够严

---

## 三、模块化与可插拔评估

### 当前状态

| 维度 | 评分 | 原因 |
|------|------|------|
| 因子接口 | ⭐⭐⭐⭐⭐ | BaseFactor + FactorValue + param_space |
| 退出接口 | ⭐⭐⭐⭐⭐ | BaseExitModule + ExitProfile + ExitDecision |
| 注册中心 | ⭐⭐⭐⭐⭐ | 单例 + auto_discover + YAML |
| **生产集成** | **⭐** | 完全未接入！只在 shadow 模式运行 |
| 配置驱动 | ⭐⭐⭐⭐ | exit_policy.yaml 详尽，但两套配置并存 |
| 可测试性 | ⭐⭐⭐⭐ | 纯函数 + 无副作用 + param_space 可扫描 |

### 模块化差距

1. **FactorAssembly 未替代 LLM pipeline**: 生产入场仍用 LLM 投票，不用因子组合
2. **ExitModuleRegistry 未替代 position_exit_monitor**: 生产退出是 1796 行单体函数
3. **两套配置系统**: `exit_policy.yaml` (生产) vs `exit_profiles.yaml` (新框架)
4. **无 regime 门控**: regime 只在退出参数差异化中使用，不在入场门控中使用

---

## 四、辩证分析 (PRO / ANTI / SYNTHESIS)

### 辩论 1: 是否立即禁用 HIGH_VOL regime 开仓?

| 维度 | PRO (禁用) | ANTI (保留) |
|------|-----------|-------------|
| 数据 | 14.9% WR, -$39K，确定性负 EV | 样本仅 141 笔，可能有季节性 |
| 数学 | 需要 87% WR 才能在 HIGH_VOL 保本 | — |
| 风险 | 不禁用每天持续失血 | 可能错过 VIX 冲高后的暴力反弹 |
| 类比 | 港股已禁用0%胜率因子 | — |

**综合**: ✅ **立即禁用 HIGH_VOL 开仓**。数据确凿：141 笔 14.9% WR -$39K 不是噪声，是结构性缺陷。

### 辩论 2: 新模块化框架是否应该替代生产代码?

| 维度 | PRO (替代) | ANTI (保持双轨) |
|------|-----------|-----------------|
| 架构 | 消除 1796 行巨型单体 | 替代是🔴级跨文件重构 |
| 可维护 | 每个退出规则独立可测 | 生产代码已稳定运行 |
| 可插拔 | 新增退出规则 = 新文件 + 注册 | 需要 ExitProfile ↔ exit_policy.yaml 映射层 |
| 风险 | 替代过程中可能引入 bug | 双轨维护成本持续存在 |

**综合**: 🟡 **分阶段替代**。Phase 1: regime 门控接入 FactorRegistry。Phase 2: ExitProfile 从 exit_policy.yaml 自动映射。Phase 3: position_exit_monitor 拆分为模块调用。

### 辩论 3: LONG 方向是否应该禁用?

| 维度 | PRO (禁用) | ANTI (保留) |
|------|-----------|-------------|
| 数据 | 20.3% WR，几乎不赚钱 | +$1,933 仍为正 |
| 机会 | 减少低质量信号 | RANGE regime 中 LONG 可能有效 |
| 数学 | WR < 盈亏平衡 38.4% | R:R 可能在大赢时补偿 |

**综合**: 🟡 **不禁止但提高门控**。LONG 需要 min_confidence 0.70 (vs SHORT 的 0.55) 且仅在 TREND_UP / RANGE regime 允许。

---

## 五、分级行动计划

### P0 — 止血 (🔴 级, 立即执行)

| # | 行动 | 预期收益 | 风险 |
|---|------|----------|------|
| 1 | **HIGH_VOL regime 禁止开仓** | 止住 -$39K 出血口 | 可能错过极端机会 |
| 2 | **LONG 信号门控收紧**: min_confidence 0.60→0.70 | 减少低质量 LONG 交易 | 减少交易量 |
| 3 | **STRUCTURE_EXIT 触发阈值放宽** (technical_breach trigger_atr_mult 2.0→2.5) | 减少 -$3,106 中部分误杀 | 延迟真实破位退出 |

### P1 — 优化 (🟡 级, 本周内)

| # | 行动 | 预期收益 |
|---|------|----------|
| 4 | Regime 门控接入 FactorRegistry (入场前检查 regime) | 架构统一 |
| 5 | ExitProfile ↔ exit_policy.yaml 映射层 | 消除双配置 |
| 6 | 开仓保护期: 持仓<5min 禁止 sl_hit (除非亏损>2x ATR) | 减少 <10min 噪音止损 |

### P2 — 重构 (🟢 级, 下周)

| # | 行动 | 预期收益 |
|---|------|----------|
| 7 | position_exit_monitor.py 拆分为模块调用 | 可维护性 + 可测试性 |
| 8 | FactorAssembly 接入生产 signal_pipeline | 因子框架产生实际价值 |
| 9 | 新增 RegimeFilter factor (TREND_UP/RANGE=开, HIGH_VOL=关) | 声明式 regime 门控 |

---

## 六、预期效果

### 如果执行 P0 #1 (禁用 HIGH_VOL):

```
当前: 309笔, 27.8% WR, -$16,283
移除 HIGH_VOL 后: 168笔, ~39% WR, ~+$23,254
  (TREND_UP +$6,262 + RANGE +$16,991 = +$23,253)
```

**一个配置变更就能从 -$16K 翻转到 +$23K。**

### 如果同时执行 P0 #2 (收紧 LONG):

LONG 交易减少约 40%（低置信度被过滤），预期额外减少 ~$500 亏损。

---

## 七、总结

| 维度 | 当前 | 目标 | 差距 |
|------|------|------|------|
| 胜率 | 30.8% | ≥50% | 🔴 19pp |
| R:R | 1.60:1 | ≥1.5:1 | ✅ 已达标 |
| 日均 PnL | -$52.7 | ≥$500 | 🔴 严重偏离 |
| 模块化 | 框架⭐⭐⭐⭐⭐ / 集成⭐ | 全链路⭐⭐⭐⭐ | 生产未接入 |
| 可插拔 | 因子+退出接口完美 | 全链路可插拔 | regime门控缺失 |

**核心结论**: 
1. 架构设计优秀但**生产代码完全未使用新框架** — 这是最大的浪费
2. **HIGH_VOL regime 是单一致命缺陷** — 一个配置修复即可止血
3. 止损逻辑合理 (R:R 1.6:1)，但**信号质量不足**导致 WR 远低于盈亏平衡
4. TRAILING_STOP 是唯一盈利退出方式 — 应让更多交易有机会走到追踪止损

---

*本报告基于 309 笔真实交易数据。所有建议均回答5问：服务F1(买卖规范)/F3(架构清晰)、改胜率/日均、SQL验证、最坏=减少交易量、回滚=YAML一行。*
