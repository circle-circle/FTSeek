# DEB-20260708: Horizon Selector 趋势强度评分优化方向

## 背景

2026-07-08 实施了新的 `_fallback_score` 趋势强度评分逻辑（TS 0-6分），修复了 `atr_pct` 致命bug（绝对值当百分比用→几乎所有股票被误判高波动→long_score封顶0.5）。修复后强趋势+低波动标的正确选为long horizon。

**但**: 所有163条历史horizon决策的reasoning source是"disabled"或"migration"——新fallback规则**从未在生产环境运行过**。今晚(07-08盘前)将是首次。

## 目标

辩论horizon_selector的合理发散方向和可提升方案，确保今晚首次运行不会引入新问题，同时识别中期优化方向。

## Pre-debate 数据 (DB真实数据)

### D1: Horizon分布 (6/1-7/8)
```
short: n=163 avg_pnl=0.01% wr=17.2%
long: 0条 (从未选过)
```
→ 所有决策都是short horizon，long从未被选中过。

### D2: 按方向×退出原因
```
EOD_FORCE_CLOSE: n=14 avg_pnl=1.81% (BUY 5条 100%WR avg1.44%, SHORT 9条 77.8%WR avg2.02%)
TIME_EXIT: n=15 avg_pnl=-0.44% (多数hold 14-37h)
SL_ATR_STOP: n=8 avg_pnl=-2.0%
PRE_CLOSE_EXIT: n=5 avg_pnl=0.01%
TAKE_PROFIT: n=1 avg_pnl=0.04%
```
→ EOD是盈利主力，TIME_EXIT是亏损主因。

### D3: 按方向PnL
```
BUY:        n=89 avg_pnl=-0.14% wr=18.0% total=-$2814
SELL_SHORT: n=74 avg_pnl=0.2%  wr=16.2% total=+$1820
```

### D4: SL%分布
```
BUY:        avg_sl=2.43% min=0.43% max=8.0%
SELL_SHORT: avg_sl=3.05% min=1.4%  max=8.5%
```

### D5: 持仓时间
```
BUY:        avg=6.13h max=34h
SELL_SHORT: avg=2.49h max=22h
```

### D6: MFE/MAE (绝对值)
```
BUY:        avg_mfe=122.2  avg_mae=-89.7  (n=27)
SELL_SHORT: avg_mfe=187.7  avg_mae=-49.1  (n=14)
```
→ MFE远高于最终PnL → 利润未捕获，退出过早。

### D7: fwd_return
```
BUY:        avg_t1=-0.0013 avg_t3=+0.0048 avg_t5=+0.0051
SELL_SHORT: avg_t1=+0.0019 avg_t3=-0.0041 avg_t5=-0.0117
```
→ BUY T3/T5正回报(方向正确)，SHORT T3/T5负回报(做空后下跌=方向正确)。

### D8: fallback规则阈值
```python
# _fallback_score 关键阈值:
_high_vol = vix > 22 or atr_pct > 0.04  # 4%
if _high_vol and ts >= 3: long_score += 0.5  # 封顶
elif ts >= 4 and vix < 20: long_score += 2.0  # 强趋势低波动
elif ts >= 3: long_score += 1.5
elif ts >= 2: long_score += 0.5

# 决策: long_score > short_score * 1.1 → long
```

### D9: vol_buckets配置
```
short: low(<1.5%) med(<3.5%) high(<8%) extreme(>8%)
long:  low(<1.5%) med(<3.5%) high(>3.5%)
```
→ long horizon没有extreme桶，high桶上限=999(无上限)。

### D10: factor_exit_rules
```
default:        time_exit=4h  breakeven=1.5% max_hold=1d
volume_expansion: time_exit=0.5h max_hold=1d
macd_golden_cross: time_exit=6h max_hold=3d  trailing=true
insider_cluster_buy: time_exit=999h max_hold=5d trailing=true
```

## 讨论记录

### 第 1 轮：hermes

**立场**: 新fallback规则逻辑合理但存在5个风险点，今晚首次运行前应处理P0，中期优化P1。

**论据**:

**1. (P0风险) long horizon SL未加宽 → 趋势没发展就扫损**
- 数据D4: BUY avg_sl=2.43%，但long horizon max_hold=3天
- 数据D6: MFE=122 vs 最终PnL≈0 → 利润未捕获
- 问题: long horizon用同一套vol_buckets计算SL，但3天持仓的波动远大于日内
- 验证: `grep -n "vol_buckets" config/playbook_horizon.yaml` → short和long的sl_multiplier相同(1.2/1.5/1.5)
- **如果不加宽，TS≥4选long的标的会在3天内被2.5%SL扫损，还不如short**

**2. (P0风险) TIME_EXIT hold=14-37h 与 short horizon max_hold=1天矛盾**
- 数据D2: TIME_EXIT 15条 avg hold=14-37h，但short horizon max_hold_days=1
- 14h可能是隔夜持仓(盘前生成→次日盘中退出)，37h是2天
- 问题: short horizon的time_exit=4h(factor_exit_rules default)，但实际hold 14h+
- 说明time_exit_only_if_loss=true导致盈利仓不time_exit，持到EOD
- **新规则可能选long → max_hold=3天 → TIME_EXIT不再触发，但SL可能先触发**

**3. (P1) 趋势强度TS=4阈值缺乏回测验证**
- D8: TS≥4+VIX<20→long+2.0, TS≥3→long+1.5
- 但TS是基于5个子项(momentum+MA+MACD+volume+trend_5d)各1-2分的简单加总
- 没有回测验证TS≥4的标的实际T3/T5回报是否优于TS<4
- D7: BUY fwd_return_t3=+0.48% (正向)，说明3天持仓确实有alpha，但未按TS分层验证

**4. (P1) 高波动一票否决过于粗暴**
- D8: atr_pct>4% → long_score封顶0.5
- 但D9: vol_buckets的high桶是3.5-8%，很多正常股票(如TSLA ATR~5%)会被一票否决
- TSLA/COIN等高波动股也有趋势行情，完全禁long可能错过波段利润
- 建议: 高波动时降低long_score而非封顶0.5(如long_score*=0.5)

**5. (P2) 历史模型路径形同虚设**
- D1: 163条全是disabled/migration，历史模型从未触发
- min_samples_for_model=5，但目前IC数据不足
- fallback规则是唯一生效路径，但无A/B对比验证其优劣

**收敛建议**:
- P0: long horizon的SL需加宽(如sl_multiplier×1.5 or sl_floor+1%)
- P0: 今晚首次运行前确认long horizon标的的SL不会在开盘噪声中被扫
- P1: 运行1-2周后用实际TS分层数据回测验证TS≥4阈值
- P1: 高波动封顶改为按比例降分而非绝对封顶
- P2: 积累数据后激活历史模型路径做A/B对比

### 第 1 轮：claude-code

**立场**：认同 P0/P1 方向，但 hermes 推论有三处过度推断。

**论据**：

**1. SL 加宽需求被放大**
- `vol_buckets` 中 short/long 的 `sl_multiplier` 同为 1.2/1.5/1.5/1.2，但 long 的 `max_hold_hours` 已是 24/16/8（short 6/4/2），设计部分考虑了波动。
- 然而 long 的 `sl_ceil`/`sl_floor` 仍与 short 相同，3 天持仓用 1 天桶仍可能偏紧，建议 long 的 sl_ceil 提高 0.5%。
- "不如 short" 过度推断：long 的 max_hold_days=5 给了趋势空间。

**2. 历史 TIME_EXIT 不能推断新规则**
- 14-37h 持仓来自旧规则（time_exit=2h, breakeven=0.5）。当前已改为 4h/1.5%，不能据此推断新规则 "SL 先触发"。

**3. TS 阈值缺回测属实**
- D7 BUY T3=+0.48% 是整体平均，非 TS>=4 分层证据，不能直接支持阈值合理。

**4. 高波动封顶建议修正**
- 同意一票否决粗暴，建议改为线性降权而非封顶 0.5。

**收敛建议**：今晚加打印 sanity check；long sl_ceil +0.5%；一周后按 TS 分层回测再调阈值。

### 第 2 轮：hermes

**立场**: 接受Claude Code 3处修正，收敛到2项P0+3项P1。

**接受修正**:
1. ✅ "不如short"过度推断 → long max_hold_days=5给了趋势空间，sl_ceil+0.5%是合理微调而非全局加宽
2. ✅ TIME_EXIT历史数据不能推断新规则 → 旧time_exit=2h已改4h，旧数据不适用
3. ✅ TS分层回测正确 → D7整体均值不能证明TS≥4有效

**维持立场**:
1. long horizon SL仍需微调 → Claude Code也同意sl_ceil+0.5%，收敛
2. 高波动封顶→线性降权 → 双方一致

**新增验证**: 刚确认long horizon的vol_buckets high桶atr_pct_max=999(无上限)，extreme桶不存在。这意味着ATR 8%的COIN会进high桶(sl_multiplier=1.5, sl_ceil=8.0%)。8%SL+3天持仓=合理。但ATR 2%的AAPL会进medium桶(sl_floor=2.5%)，2.5%SL+3天持仓可能偏紧。Claude Code的sl_ceil+0.5%方案覆盖了这个gap。

**收敛结论**:
- **P0-1**: long horizon vol_buckets的sl_ceil全部+0.5%（low 2.5→3.0, medium 4.5→5.0, high 8.0→8.5）
- **P0-2**: 今晚首次运行无需其他改动，代码已修复+测试通过+服务已重启
- **P1-1**: 运行1-2周后按TS分层回测验证TS≥4阈值
- **P1-2**: 高波动封顶0.5改为线性降权(long_score *= max(0.3, 1 - atr_pct/0.08))
- **P1-3**: 积累数据后激活历史模型路径做A/B对比


