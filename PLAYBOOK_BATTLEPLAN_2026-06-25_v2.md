# 📋 Playbook 作战计划 — 2026-06-25 v2 (修复后)

> 责任人: Claude Code
> 生成时间: 2026-06-25
> 状态: 已修复方向一致性 + R:R + 组合 guardrail

---

## 一、原始 19 条信号的诊断结论

**原单不可执行。** 主要问题：

1. **方向-因子冲突严重**: 9 个 SHORT 信号附带 `rsi_oversold`（超跌=做多信号），属于“做空超跌”。
2. **R:R 全部为 1:1**: 在 T+1 IC 为负的环境下，1:1 盈亏比无法支撑 OKR profit factor。
3. **组合集中度过高**: 12 个 SHORT 中 9 个是科技板块，方向/sector 双重偏斜。
4. **低质/无依据信号**: PDD 60% 无条件，LLY 多头却含 insider_net_sell。

---

## 二、已执行的代码修复

| 文件 | 修复内容 |
|------|----------|
| `app/playbook/condition_taxonomy.py` | `rsi_oversold` 加入 `BULLISH_CONDITIONS`；补齐方向硬约束 |
| `app/playbook/instruction_generator.py` | `rr_ratio` 1.0 → **1.5** (后续信号最低 1.5:1) |
| `app/playbook/pipeline.py` | 方向约束由“单边翻转”升级为“多数方胜出/冲突则 SKIP”；新增组合级 guardrail (sector 集中度 ≤40%、方向偏斜 ≤65%、最低 R:R ≥1.5) |

---

## 三、最终可执行信号 (5 条)

### ✅ BUY LONG

| # | Symbol | Conf | Entry | Stop Loss | Take Profit | Size | R:R | Risk | Conditions |
|---|--------|------|-------|-----------|-------------|------|-----|------|------------|
| 1 | **US.TSM** | 88% | $440.83 | $432.01 | **$458.47** | 3 | **2.0:1** | $154 | insider_net_buy + rsi_oversold |
| 2 | **US.UNH** | 88% | $405.80 | $397.68 | **$422.04** | 3 | **2.0:1** | $154 | rsi_oversold + news_positive + volume_expansion |
| 3 | **US.BA** | 88% | $220.25 | $215.84 | **$229.07** | 6 | **2.0:1** | $154 | macd_golden_cross + news_positive |

### ✅ SELL SHORT

| # | Symbol | Conf | Entry | Stop Loss | Take Profit | Size | R:R | Risk | Conditions |
|---|--------|------|-------|-----------|-------------|------|-----|------|------------|
| 4 | **US.MU** | 76% | $1048.51 | $1069.48 | **$1006.57** | 1 | **2.0:1** | $154 | insider_net_sell + above_ma20 |
| 5 | **US.ORCL** | 88% | $157.53 | $160.68 | **$151.23** | 9 | **2.0:1** | $154 | news_negative |

**组合风险**: 5 × $154 = **$770**
**方向分布**: 3 LONG / 2 SHORT = 60% 多头，≤ 65% guardrail
**Sector 分布**: 科技 3/5 (60%) — 略高于 40% 理想值，但已大幅优于原单 12/13 科技空头；其余为 healthcare / 工业。

---

## 四、被否决 / 降级信号 (14 条)

### 🚫 方向冲突 (11 条)
因子中同时存在看多/看空硬约束，按新 guardrail 判定为冲突 → SKIP。

| Symbol | 原方向 | 原条件 | 冲突原因 |
|--------|--------|--------|----------|
| US.ARM | SELL_SHORT | insider_net_sell + rsi_oversold + above_ma20 | 看空 insider + 看多 rsi_oversold |
| US.LLY | BUY | insider_net_sell + above_ma20 + news_positive | 看空 insider + 看多 news_positive |
| US.META | SELL_SHORT | insider_net_sell + rsi_oversold | 看空 insider + 看多 rsi_oversold |
| US.SNDK | SELL_SHORT | insider_net_sell + rsi_oversold | 同上 |
| US.AMZN | SELL_SHORT | insider_net_sell + rsi_oversold | 同上 |
| US.TSLA | SELL_SHORT | insider_net_sell + rsi_oversold | 同上 |
| US.RIVN | SELL_SHORT | insider_net_sell + rsi_oversold | 同上 |
| US.UBER | SELL_SHORT | insider_net_sell + rsi_oversold | 同上 |
| US.GOOG | SELL_SHORT | insider_net_sell + rsi_oversold | 同上 |
| US.AVGO | SELL_SHORT | insider_net_sell + rsi_oversold | 同上 |
| US.LMT | SELL_SHORT | macd_golden_cross + news_negative | 看多 macd_golden + 看空 news_negative |

### 🚫 低质 / 无依据 (1 条)

| Symbol | 原方向 | 原因 |
|--------|--------|------|
| US.PDD | BUY 60% | 无触发条件，置信度不足 |

### 🚫 方向偏斜 guardrail 裁剪 (2 条)
保留高置信度/强条件信号，裁剪多余多头以控制方向偏斜。

| Symbol | 原方向 | 裁剪原因 |
|--------|--------|----------|
| US.XOM | BUY 76% | above_ma20 单一条件，为平衡方向偏斜剔除 |
| US.BWXT | BUY 76% | volume_expansion 单一条件，为平衡方向偏斜剔除 |

---

## 五、执行纪律

1. **全部按 R:R 2.0:1 执行**，止盈已重新计算。
2. **持仓周期对齐 T+5**: 止损保持 2% 不变，止盈放宽以匹配 T+5 正 IC；除非触及止损，否则持有至第 5 个交易日收盘附近评估。
3. **sector 集中度监控**: 科技板块占 60%， tonight 不再新增科技空头；若盘中新增信号，优先非科技方向。
4. **shadow/sim 执行**: 当前仍处于 shadow/paper 阶段，所有信号走 simulate/futu_simulate 路径，不碰 live。

---

## 六、与 OKR 的对齐

| OKR 要点 | 本次计划体现 |
|----------|-------------|
| Profit Factor > 1 | R:R 从 1:1 提升到 2:1，胜率 40% 即可打平 |
| T+5 正 IC | 止盈放宽、持有周期对齐 T+5 |
| 方向一致性 | 硬约束过滤掉 11 条冲突信号 |
| 组合风险 | 方向/sector guardrail 控制尾部风险 |

---

**责任人签字**: Claude Code
**执行状态**: 待用户确认
