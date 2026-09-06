# 统一退出策略设计：基于股票分层+波动率的针对性止损止盈

> **⚠️ 时代勘误 (2026-09-06, PROPOSAL-20260906B)**：本文是 MAIN_PIPELINE 时代 (2026-06) 的设计文档。
> §统一规则 第1条"所有持仓走 ExitMonitor"对 **playbook 仓 (DA/UE/reentry, PLAYBOOK_SIM/LIVE) 从未成立**——
> playbook 仓走自管链 `_check_playbook_exit_rules` (app/playbook/pipeline.py)，ExitMonitor 对其结构性排除
> (position_exit_monitor.py:942-945)；exit_policy.yaml 同样不管辖 playbook 仓。
> 收敛终态见 docs/proposals/PROPOSAL-20260906B-exit-arch-convergence.md。本文保留作历史定位，勿据第1条推断现状。

## 设计目标

一套统一的退出策略，基于每只股票的实时行情和波动率特征，自动选择针对性的SL/TP参数。不再区分"哪套退出系统"，所有持仓走同一条退出路径。

## 设计依据

### 数据

| 指标 | LONG | SHORT |
|------|------|-------|
| PF | 0.73(负期望) | 2.00(正期望) |
| 胜率 | 27.3% | 43.0% |
| avg_win | $377 | $596 |
| avg_loss | -$195 | -$226 |
| EOD avg pnl% | 0.82% | 2.92% |
| 方向准确率 | 38.9% | 94.1% |

### 盈利/亏损退出

| 退出原因 | 笔数 | 总PnL | 判定 |
|---------|------|-------|------|
| DAY_TRADE_TP | 13 | +$13,101 | ✅ 盈利源(TP有效时) |
| PRE_CLOSE_EXIT | 33 | +$7,421 | ✅ 盈利源(EOD捕获) |
| EOD_FORCE_CLOSE | 7 | +$1,423 | ✅ 盈利源 |
| sl_hit | 123 | -$2,023 | ⚠️ 小亏(avg-$16/笔) |
| TRAILING_STOP | 85 | -$3,802 | ❌ 亏损源(avg-$45/笔) |
| STRUCTURE_EXIT | 33 | -$3,106 | ❌ 已禁用 |
| SL_ATR_STOP | 8 | -$2,847 | ❌ 亏损源(avg-$356/笔) |
| BREAKEVEN_STOP | 2 | -$6,125 | ❌ 巨亏已禁用 |

### 当前持仓波动率分层

| 持仓 | 方向 | ATR% | 桶 | 当前PnL |
|------|------|------|----|---------|
| BIDU | LONG | 3.70% | high | +0.80% |
| IWM | SHORT | 1.99% | medium | -0.63% |
| MSFT | SHORT | 3.47% | medium | +0.08% |
| SNDK | LONG | 9.88% | high | +0.59% |
| TSLA | SHORT | 4.41% | high | -7.97% |
| UNH | SHORT | 2.35% | medium | +0.17% |
| ARM | SHORT | 11.03% | high | -7.16% |

## 统一设计

### 架构：3层退出，1条路径

```
持仓 → ExitMonitor(scan_positions, 唯一退出引擎)
  │
  ├─ 第1层: SL止损 (atr_stop)
  │   ├─ 计算式: entry ± multiplier × ATR
  │   ├─ 硬上限: max_loss_pct (绝对亏损上限)
  │   ├─ SL契约: current_sl不得逾越initial_sl
  │   └─ multiplier/max_loss_pct 按波动率桶动态选择
  │
  ├─ 第2层: 盘末退出 (pre_close)
  │   ├─ 亏损仓: loss_flat=true → 盘末全平
  │   ├─ 盈利≥close_profit_atr → 落袋全平
  │   ├─ 微利0~half_exit_atr → 减半仓
  │   └─ close_profit_atr/half_exit_atr 按波动率桶动态选择
  │
  └─ 第3层: 开盘冷却 (grace period)
      ├─ 持仓<30min → 只允许hard_stop
      └─ 开盘噪声保护
```

### 核心设计：波动率桶参数表

每只股票按ATR%(ATR/价格)自动分层，每层有独立的SL multiplier、max_loss、pre_close参数：

| 桶 | ATR%范围 | SL multiplier | max_loss% | pre_close close_profit_atr | half_exit_atr | max_hold |
|----|---------|---------------|-----------|---------------------------|---------------|---------|
| **low** | <1.5% | 1.5× | 1.5% | 1.5 ATR | 0.3 ATR | 8h |
| **medium** | 1.5%~3.5% | 2.0× | 2.0% | 2.0 ATR | 0.5 ATR | 4h |
| **high** | 3.5%~8% | 2.5× | 3.0% | 2.5 ATR | 0.8 ATR | 2h |
| **extreme** | >8% | 3.0× | 4.0% | 3.0 ATR | 1.0 ATR | 1h |

**设计逻辑**：
- 低波动股(<1.5%)：SL收紧(1.5×ATR)，给趋势发展空间(8h)，pre_close阈值低(1.5ATR) → 小波动也能落袋
- 中波动股(1.5-3.5%)：标准参数(2.0×ATR)，4h持仓，2ATR落袋 → 平衡
- 高波动股(3.5-8%)：SL放宽(2.5×ATR)，短持仓(2h)，2.5ATR落袋 → 防噪声扫损
- 极端波动股(>8%)：SL最宽(3.0×ATR)，1h持仓，3ATR落袋 → ARM/SNDK类，给足空间但快速退出

### VIX叠加（market_state_overlay）

VIX作为市场级叠加，在个股SL基础上加宽：

| VIX | 仓位乘数 | SL加宽 | TP倍率 |
|-----|---------|--------|--------|
| <20 | 1.0× | +0.0% | 1.0× |
| 20~25 | 0.8× | +0.3% | 0.9× |
| 25~30 | 0.5× | +0.8% | 0.7× |
| >30 | 0.0×(不开仓) | +1.5% | 0.5× |

### TP策略

**rr_ratio=999（禁用固定TP）**。理由：
- TP=3%从未触发（历史0笔）
- SHORT EOD avg=2.92% > TP=3% → TP会截断SHORT盈利
- 盈利落袋由pre_close的close_profit_atr负责（按波动率桶2-3ATR）

### 开盘冷却期

持仓<30分钟时：
- 只允许hard_stop（SL_HIT/HARD_STOP/ATR_STOP）
- 其他退出规则被grace period压制
- 开盘前15分钟SL额外放宽到4%（防开盘噪声扫损）

### 统一规则

1. ~~所有持仓走ExitMonitor，不走_check_playbook_exit_rules~~ **(已失效，见文首勘误： playbook 仓恰好相反——只走 _check_playbook_exit_rules，不走 ExitMonitor)**
2. **initial_sl由vol_buckets在开仓时设定**，position_sync不得覆盖
3. **current_sl由ExitMonitor的atr_stop管理**，受SL契约约束（不逾越initial_sl）
4. **pre_close参数按波动率桶动态选择**，不再用全局固定值
5. **VIX叠加在个股SL基础上加宽**，不影响SL multiplier本身

## 配置实现

### exit_policy.yaml — 统一退出引擎

```yaml
exit_rules:
  atr_stop:
    enabled: true
    method: "entry_price - atr * multiplier"
    # multiplier和max_loss_pct按波动率桶动态选择(见下方vol_bucket_overrides)
    multiplier_by_profit:
      initial: 2.0  # 默认medium桶
    max_loss_pct: 2.0  # 默认medium桶
    atr_floor_pct: 70

  breakeven:
    enabled: false  # 历史2笔-$6,125, 不启用

  time_exit:
    enabled: false  # 由pre_close统一管理

  pre_close_exit:
    enabled: true
    window_minutes: 8
    min_hold_minutes_before_pre_close: 30
    time_weighted: false
    loss_flat: true  # 亏损盘末平掉
    # close_profit_atr和half_exit_below_atr按波动率桶动态选择
    close_profit_atr: 2.0  # 默认medium桶
    half_exit_below_atr: 0.5  # 默认medium桶
    hold_threshold_atr: 99
    quarter_exit_below_atr: 99

  # 波动率桶覆盖 — 个股SL/TP参数按ATR%自动分层
  vol_bucket_overrides:
    low:
      atr_pct_max: 0.015
      atr_multiplier: 1.5
      max_loss_pct: 1.5
      close_profit_atr: 1.5
      half_exit_below_atr: 0.3
    medium:
      atr_pct_max: 0.035
      atr_multiplier: 2.0
      max_loss_pct: 2.0
      close_profit_atr: 2.0
      half_exit_below_atr: 0.5
    high:
      atr_pct_max: 0.08
      atr_multiplier: 2.5
      max_loss_pct: 3.0
      close_profit_atr: 2.5
      half_exit_below_atr: 0.8
    extreme:
      atr_pct_max: 999.0
      atr_multiplier: 3.0
      max_loss_pct: 4.0
      close_profit_atr: 3.0
      half_exit_below_atr: 1.0
```

### playbook_horizon.yaml — 盘前SL生成（vol_buckets对齐）

```yaml
short_horizon:
  vol_buckets:
    low:
      atr_pct_max: 0.015
      sl_multiplier: 1.5  # 与exit_policy atr_multiplier对齐
      sl_floor_pct: 1.0
      sl_ceil_pct: 1.5
      short_widen_pct: 0.2
      max_hold_hours: 8
    medium:
      atr_pct_max: 0.035
      sl_multiplier: 2.0  # 对齐
      sl_floor_pct: 1.5
      sl_ceil_pct: 2.0
      short_widen_pct: 0.3
      max_hold_hours: 4
    high:
      atr_pct_max: 0.08
      sl_multiplier: 2.5  # 对齐
      sl_floor_pct: 2.0
      sl_ceil_pct: 3.0
      short_widen_pct: 0.5
      max_hold_hours: 2
    extreme:
      atr_pct_max: 999.0
      sl_multiplier: 3.0  # 对齐
      sl_floor_pct: 2.5
      sl_ceil_pct: 4.0
      short_widen_pct: 0.8
      max_hold_hours: 1
```

## 每个持仓的退出策略（示例）

| 持仓 | 方向 | ATR% | 桶 | SL% | SL价 | pre_close落袋 | 当前PnL | 预期退出 |
|------|------|------|----|-----|------|-------------|---------|---------|
| BIDU | LONG | 3.70% | high | 2.5×ATR=9.3%→cap 3.0% | $109.18 | 2.5ATR落袋 | +0.80% | 盈利→pre_close落袋 / 亏损→SL |
| IWM | SHORT | 1.99% | medium | 2.0×ATR=4.0%→cap 2.0% | $303.00 | 2.0ATR落袋 | -0.63% | 亏损→SL / 盘末→loss_flat平 |
| MSFT | SHORT | 3.47% | medium | 2.0×ATR=6.9%→cap 2.0% | $377.85 | 2.0ATR落袋 | +0.08% | 盈利→pre_close / 亏损→SL |
| SNDK | LONG | 9.88% | extreme | 3.0×ATR=29.6%→cap 4.0% | $2,082.44 | 3.0ATR落袋 | +0.59% | 盈利→pre_close / 亏损→SL |
| UNH | SHORT | 2.35% | medium | 2.0×ATR=4.7%→cap 2.0% | $425.40 | 2.0ATR落袋 | +0.17% | 盈利→pre_close / 亏损→SL |
