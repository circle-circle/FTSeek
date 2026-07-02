# DEB-20260630: 退出策略优化与OKR盈利目标

## 背景

FTSeek-US 30天OKR盈利目标(6/19-7/19)。当前剩余19天。今天修复了4个工程断点(K线stale/guardrail杀信号/pre_close时间退出/持仓owner)，退出系统首次按设计运行。需要评估退出策略是否能支撑OKR盈利目标，是否需要进一步优化。

## 目标

1. 评估当前退出策略(SL+vol_buckets+atr_stop, TP禁用)是否能实现盈利
2. 识别退出策略中的alpha来源和亏损源
3. 提出可落地的优化方案(做减法，不新增复杂度)

## 数据与实证

### 方向PF分解（剔除PHANTOM/ghost/stale）

| 方向 | 总笔 | 胜 | 负 | avg_win | avg_loss | PF | breakeven_WR | 实际WR |
|------|------|----|----|---------|----------|----|-------------|--------|
| LONG | 161 | 44 | 117 | $376.6 | -$195.2 | **0.73** | 34.1% | 27.3% |
| SHORT | 200 | 86 | 114 | $596.4 | -$225.5 | **2.00** | 27.4% | 43.0% |

- LONG PF=0.73 = **负期望**，WR<breakeven_WR → 长期亏损
- SHORT PF=2.00 = **正期望**，WR>breakeven_WR → 长期盈利
- SHORT alpha来源：avg_win($596) > 2×avg_loss($226) → 大赢小亏

### 退出原因PnL分解（关键项）

| 方向 | 退出原因 | 笔数 | avg_pnl | total_pnl |
|------|---------|------|---------|-----------|
| SHORT | PRE_CLOSE_EXIT | 13 | +$659 | +$8,572 |
| SHORT | DAY_TRADE_TP | 11 | +$981 | +$10,786 |
| SHORT | EOD_FORCE_CLOSE | 3 | +$340 | +$1,021 |
| SHORT | sl_hit | 65 | -$16 | -$1,028 |
| SHORT | TRAILING_STOP | 41 | -$65 | -$2,652 |
| SHORT | STRUCTURE_EXIT | 33 | -$94 | -$3,106 |
| LONG | PRE_CLOSE_EXIT | 20 | -$58 | -$1,151 |
| LONG | sl_hit | 58 | -$17 | -$995 |
| LONG | TRAILING_STOP | 44 | -$26 | -$1,150 |
| LONG | TIME_EXIT | 13 | -$96 | -$1,248 |
| LONG | SL_ATR_STOP | 6 | -$456 | -$2,737 |

- SHORT盈利来源：DAY_TRADE_TP(+$10.8K) + PRE_CLOSE_EXIT(+$8.6K)
- SHORT亏损来源：STRUCTURE_EXIT(-$3.1K) + TRAILING_STOP(-$2.7K)
- LONG全面亏损：所有退出原因均为负

### EOD pnl%按方向

| 方向 | avg_pnl% | max_pnl% | n |
|------|---------|---------|---|
| LONG | 0.82% | 5.04% | 37 |
| SHORT | 2.92% | 9.07% | 18 |

- SHORT EOD avg=2.92% → TP=3%会截断SHORT盈利
- LONG EOD avg=0.82% → TP对LONG中性

### SL触发速度

| 方向 | 退出原因 | avg_holding_hours | n |
|------|---------|-------------------|---|
| LONG | TRAILING_STOP | 3.08h | 44 |
| SHORT | TRAILING_STOP | 1.78h | 41 |
| SHORT | HARD_STOP | 0.51h | 1 |

### 方向准确率(fwd_return_t1)

| 方向 | n | avg_fwd_t1 | dir_accuracy |
|------|---|-----------|-------------|
| BUY | 36 | -0.008 | 38.9% |
| SELL_SHORT | 17 | +0.032 | 94.1% |

- SHORT方向准确率94.1% → 做空后市场确实下跌
- BUY方向准确率38.9% → 买入后市场多数上涨(=做多错误)

### 产出率

| 日期 | 总指令 | 执行 | SKIP |
|------|--------|------|------|
| 6/30 | 37 | 8 | 29 |
| 6/29 | 38 | 15 | 23 |
| 6/24 | 25 | 25 | 0 |
| 6/23 | 25 | 25 | 0 |

### 当前退出配置

**exit_policy.yaml（ExitMonitor）**:
- atr_stop: enabled, 2.0×ATR, max 2.0%
- breakeven: disabled
- time_exit: disabled
- pre_close: disabled（今天禁用）
- progressive_trailing: disabled

**playbook_horizon.yaml（盘前生成SL/TP）**:
- vol_buckets: low/medium/high三档, SL 1.2%~5.0%, SHORT加宽0.2~0.5%
- rr_ratio=999（TP禁用）
- factor_exit_rules: 按因子差异化trailing/time_exit

### 今天修复的断点

1. K线stale → get_daily_kline加start/end/max_count
2. Guardrail杀信号 → 改为降仓50%不SKIP
3. pre_close时间退出 → 禁用，让SL/trailing接管
4. HARD_STOP不在hard_stop_reasons → 加入集合
5. position_sync覆盖initial_sl → 只在=0时才重算
6. #SIM幽灵仓污染pipeline → 过滤#后缀
7. manual持仓被排除ExitMonitor → 改owner=auto

## 讨论记录

### 第 1 轮：hermes

**立场：当前退出策略方向正确，但需要两个优化才能支撑OKR盈利目标**

**论点1: SHORT是唯一alpha来源，退出策略应保护SHORT盈利**

数据证明：SHORT PF=2.00(正期望), LONG PF=0.73(负期望)。SHORT的alpha来自大赢小亏(avg_win=$596 vs avg_loss=$226)。当前rr_ratio=999(禁用TP)的设计正确——TP=3%会截断SHORT EOD avg=2.92%的盈利。

但pre_close已禁用 → SHORT失去了最大的盈利退出路径(PRE_CLOSE_EXIT +$8.6K, DAY_TRADE_TP +$10.8K)。现在只有atr_stop(2.0×ATR) → SHORT可能在收盘前被噪声扫掉SL，而不是跑到收盘捕获EOD avg=2.92%。

**建议: 不完全禁用pre_close，改为"仅盈利仓pre_close"** — 亏损仓由SL管理，盈利仓在收盘前落袋。这保留了SHORT的EOD盈利捕获，同时让SL管理亏损。

**论点2: LONG是亏损源，不应完全放弃但需要提高门槛**

LONG PF=0.73但avg_win($377)>avg_loss($195) → 问题是胜率低(27.3%)而非赔率差。BUY方向准确率38.9%=做多多数错误。根因可能在信号层(LLM做多判断不准)而非退出层。

**建议: 不改LONG退出参数，提高LONG confidence门槛** — 用guardrail降仓替代SKIP已经在今天修复。让LONG以更低仓位运行，等IC数据达标后再决定是否禁用LONG。

**论点3: 退出规则碎片化是亏损的隐藏来源**

STRUCTURE_EXIT(33笔,-$3.1K)和TRAILING_STOP(41+44笔,-$3.8K)是主要亏损退出。STRUCTURE_EXIT已禁用(2026-06-21)，TRAILING_STOP也禁用了(progressive_trailing disabled)。但ExitMonitor的atr_stop本质上就是trailing stop → 需要确认atr_stop是否在制造同样的亏损。

**验证: atr_stop(2.0×ATR)的触发距离 vs 历史TRAILING_STOP的avg_holding(1.78h~3.08h)** — 如果atr_stop在1-3小时内触发，说明SL太紧，需要放宽到3.0×ATR或更高。

**行动项**:
- [P0] pre_close改为"仅盈利仓"模式(pre_close_exit.enabled=true + loss_flat=false + close_profit_atr=2.0)
- [P1] 确认atr_stop触发距离，如果<2h则考虑放宽multiplier 2.0→2.5
- [P2] LONG confidence门槛从70提高到75(降低LONG仓位)
- [P2] 不改SHORT退出参数( PF=2.00 = 正期望)

### 第 2 轮：hermes（自验证+修正）

**验证结果**:

1. **pre_close"仅盈利仓"可行** ✅
   - grep pre_close.py: close_profit_atr=2.0 → 浮盈≥2ATR全平(盈利落袋)
   - loss_flat=false → 亏损仓保留过夜, 隔夜由atr_stop管理
   - 0~0.5ATR微利 → 减半仓
   - **修正: 不完全禁用pre_close, 改为loss_flat=false+close_profit_atr=2.0**

2. **atr_stop = TRAILING_STOP（同一东西）** ✅
   - atr_stop_engine.py: 返回reason=HARD_STOP(2%上限)或ATR_STOP(2.0×ATR)
   - position_monitor.py reason_map: ATR_STOP→ExitReason.TRAILING_STOP
   - **历史TRAILING_STOP就是atr_stop, 不是另一套规则**

3. **止损执行价远超SL价格** ⚠️ 严重问题
   - ARM: SL=$350.31(5.5%), exit_price=$353.01 → 实际亏损6.31% (超SL 0.8%)
   - TSLA: SL=$397.96(3.13%), exit_price=$414.72 → 实际亏损7.47% (超SL 4.3%!)
   - 根因: ExitMonitor检测到SL触发→下单平仓→但市价单滑点巨大
   - **这比SL参数更重要: 执行层的问题导致亏损放大**

4. **IWM/UNH/SNDK变成stale_not_in_broker** ⚠️
   - position_sync只查SIMULATE → 但这3个在SIMULATE环境中不存在
   - 可能是broker端这些持仓已被平仓但DB未更新

**修正后的行动项**:
- [P0] 重新启用pre_close, 改为loss_flat=false + close_profit_atr=2.0 (盈利落袋+亏损由SL管)
- [P0] 调查TSLA止损滑点4.3%的根因(exit_price=$414.72 vs SL=$397.96)
- [P1] 不改atr_stop multiplier(2.0×ATR) — 滑点问题比SL参数更优先
- [P1] LONG confidence门槛不动 — 信号层问题不在退出层修
- [P2] 清理stale_not_in_broker的3个持仓(IWM/UNH/SNDK)

### 收敛结论（单agent fallback, Claude Code不可用）

**收敛状态**: 单agent分析, 非对抗性辩论。核心结论基于代码验证+数据验证。

**接受/修正**:
1. ✅ 接受: pre_close不完全禁用, 改为"仅盈利仓"模式(loss_flat=false + close_profit_atr=2.0)
2. ✅ 修正: atr_stop=TRAILING_STOP(同一东西), 不改multiplier — 退出层参数不是核心问题
3. ✅ 新发现: TSLA止损滑点4.2%是执行层问题, 优先于SL参数优化
4. ✅ 接受: LONG PF=0.73是信号层问题(方向准确率38.9%), 不在退出层修
5. ✅ 新发现: position_sync重启后用8% fallback覆盖initial_sl(已修复)

**最终行动项**:

- [P0] pre_close改为"仅盈利仓"模式: exit_policy.yaml pre_close_exit.enabled=true + loss_flat=false + close_profit_atr=2.0
  - 盈利仓≥2ATR → 盘末全平(落袋)
  - 亏损仓 → 保留过夜(SL管理)
  - 微利仓0~0.5ATR → 减半仓
  - 验证: grep check_pre_close_exit 确认逻辑正确

- [P0] 调查TSLA止损滑点: exit_price=$414.72 vs SL=$397.96 → 滑点$16.76(4.2%)
  - 根因: 市价单? broker延迟? 还是ExitMonitor的SL计算错误?
  - 验证: grep TSLA的平仓链路日志

- [P1] 不改atr_stop multiplier(2.0×ATR) — 滑点问题比SL参数更优先
- [P1] 不改rr_ratio(999) — SHORT EOD avg=2.92% > TP=3%会截断盈利
- [P2] LONG confidence门槛不动 — 信号层问题等IC数据达标后再决定
- [P2] 清理stale_not_in_broker的3个持仓(IWM/UNH/SNDK)

**OKR盈利路径**:
- SHORT PF=2.00(正期望) → 保护SHORT盈利是核心
- pre_close"仅盈利仓" → 保留SHORT的EOD盈利捕获(+$8.6K历史贡献)
- SL管理亏损仓 → 截断SHORT亏损(但需先修滑点)
- LONG以降仓运行(guardrail降仓已修复) → 等信号层IC达标

### 第 1 轮：claude-code (CRITIC, delegate_task fallback)

**论点1(部分同意, 修正loss_flat)**: pre_close.py L116-127确认close_profit_atr=2.0可锁利; L170-195确认loss_flat=false让亏损仓过夜。但L190代码注释已过时——写"隔夜保护由atr_stop(4.0×ATR)", 实际exit_policy.yaml是atr_mult=2.0×+max_loss_pct=2.0%。亏损仓过夜面临跳空直接吃2%硬止损, 等于延迟止损+放大损失。**修正: loss_flat=true**(亏损盘末平掉), 保留close_profit_atr=2.0锁利。

**论点2(反对, 范畴错误)**: atr_stop_engine对LONG/SHORT完全对称, PF=0.73 vs 2.00差距来自信号层(方向准确率38.9%), 非退出层。confidence门槛是signal pipeline参数, 不应塞进退出策略debate。

**论点3(反对, 归因错误)**: atr_stop_engine的reason_code是HARD_STOP/ATR_STOP, 无TRAILING_STOP。数据中TRAILING_STOP -$2.7K是历史已禁用规则, 与当前atr_stop无关。放宽到2.5×会扩大单笔亏损上限, 违背截断亏损原则。

**替代方案(做减法)**: 维持atr_stop 2.0×+2%上限不动; pre_close.enabled=true但loss_flat=true; confidence门槛归还信号层。

### 第 2 轮：hermes (接受CRITIC修正)

**接受CRITIC的loss_flat=true修正**。验证: pre_close.py L190注释确实写"4.0×ATR"但实际配置是2.0×+2%max → 亏损仓过夜的隔夜保护只有2%硬止损, 跳空风险大。loss_flat=true更安全: 亏损仓盘末平掉, 不持亏过夜。

**修正后的pre_close设计**:
- enabled=true
- loss_flat=true (亏损盘末平掉, CRITIC修正)
- close_profit_atr=2.0 (盈利≥2ATR落袋)
- half_exit_below_atr=0.5 (微利减半仓)
- 即"盘末全平亏损+盈利2ATR提前落袋"

**接受CRITIC论点2**: confidence门槛归还信号层, 不在退出debate中修。

**接受CRITIC论点3**: 历史TRAILING_STOP是已禁用规则的数据, 不作为放宽atr_stop的依据。维持2.0×+2%不动。

**收敛: 双方一致**

### 最终收敛（Hermes接受CRITIC修正）

**辩论结果**: 双方一致收敛

**pre_close最终设计**:
- enabled=true
- loss_flat=**true** (CRITIC修正: 亏损仓盘末平掉, 不持亏过夜)
- close_profit_atr=2.0 (盈利≥2ATR落袋)
- 即"盘末全平亏损+盈利2ATR提前落袋"

**不变的参数**:
- atr_stop: 2.0×ATR + max 2.0% (不动)
- rr_ratio: 999 (TP禁用, 不截断SHORT EOD盈利)
- confidence门槛: 归还信号层, 不在退出debate修
