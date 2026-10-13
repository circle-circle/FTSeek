# FTSeek-US v4 Context — 当前时代(real)统计 + 策略代码

# PRODUCTION TRADE STATISTICS

NOTE: data_quality='real' AND trade_date>='2026-08-01' = current era (2026-08 onwards, broker-verified; Jun-Jul was validation phase, excluded). 'clean' rows are legacy Apr-May 2026 under old configs and are EXCLUDED from the main tables below.

## Current era totals (Aug 2026 onwards): n=446, net_pnl=-586.0 USD, avg MFE=0.91%, avg realized=0.08%, avg giveback=0.83 pct-points


## By exit_reason — CURRENT ERA (reason | n | pnl_usd | avg_pct | win% | avgMFE% | avgMAE%)
```
SL_INITIAL | 27 | -4383.0 | -3.05 | 0.0 | 0.04 | 3.14
OVERNIGHT_GAP_STOP | 9 | -2812.0 | -5.45 | 0.0 | 0.18 | 5.47
MFE_STALL_EXIT | 14 | -606.0 | -1.37 | 0.0 | 0.16 | 1.77
tighten | 3 | -452.0 | -2.15 | 0.0 | 0.02 | 2.61
breakeven_lock | 2 | -297.0 | 1.61 | 50.0 | 1.61 | 0.0
DEAD_SIGNAL_EXIT | 43 | -291.0 | -0.04 | 46.5 | 0.13 | 1.45
PATIENT_EXIT_STOP | 3 | -287.0 | -1.61 | 0.0 | 0.0 | 1.62
PATIENT_EXIT_EOD | 4 | -198.0 | -0.81 | 0.0 | 0.09 | 1.13
TIME_EXIT | 3 | -44.0 | -0.19 | 0.0 | 0.43 | 0.53
PATIENT_EXIT_FILL | 1 | -5.0 | 0.0 | 0.0 | 0.0 | 0.62
lock_in | 1 | 108.0 | 2.08 | 100.0 | 2.08 | 3.97
TAKE_PROFIT | 1 | 444.0 | 7.67 | 100.0 | 7.67 | 0.0
PRE_CLOSE_EXIT | 16 | 955.0 | 1.46 | 68.8 | 1.76 | 0.47
SL_BREAKEVEN | 196 | 974.0 | 0.2 | 75.0 | 0.63 | 0.58
SL_TRAIL_LOCK | 123 | 6307.0 | 1.03 | 93.5 | 1.86 | 0.5
```


## Monthly trend — CURRENT ERA (month | n | pnl_usd | win%)
```
2026-08 | 207 | -242.0 | 54.6
2026-09 | 181 | -509.0 | 76.8
2026-10 | 58 | 165.0 | 75.9
```


## By strategy — CURRENT ERA (strategy | n | pnl | win%)
```
extended_hours_entry | 8 | -522.0 | 50.0
playbook_sim | 438 | -64.0 | 66.7
```


## Legacy era reference ONLY (Apr-May, old configs, do NOT treat as current): 265 trades, net -7429; sl_hit 43% share; TRAILING_STOP was the profit engine then.


## 10 largest losing trades — CURRENT ERA (date | symbol | dir | reason | pnl | pct | mfe | mae | hold_h)
```
2026-09-23 | US.ORCL | LONG | OVERNIGHT_GAP_STOP | -400.0 | -6.71 | 0.2442 | 6.7135 | 6.3
2026-08-28 | US.GAP | LONG | OVERNIGHT_GAP_STOP | -387.0 | -6.42 | 0.0 | 6.4245 | 6.3
2026-10-05 | US.QCOM | LONG | OVERNIGHT_GAP_STOP | -343.0 | -5.74 | 0.4744 | 5.7415 | 19.4
2026-09-21 | US.RIVN | LONG | OVERNIGHT_GAP_STOP | -320.0 | -5.19 | 0.0 | 5.1873 | 13.8
2026-09-17 | US.CTNT | LONG | breakeven_lock | -314.0 | 2.78 | 2.7778 | 0.0 | 0.1
2026-08-18 | US.MU | LONG | SL_INITIAL | -309.0 | -5.23 | 0.1688 | 5.2273 | 6.7
2026-08-28 | US.AFRM | LONG | SL_INITIAL | -308.0 | -5.17 | 0.0 | 5.1685 | 1.9
2026-08-26 | US.UBER | LONG | SL_INITIAL | -307.0 | -5.26 | 0.0 | 5.2591 | 6.2
2026-10-09 | US.SNDK | LONG | OVERNIGHT_GAP_STOP | -293.0 | -5.93 | 0.291 | 5.9299 | 6.3
2026-09-25 | US.SNDK | LONG | OVERNIGHT_GAP_STOP | -277.0 | -5.04 | 0.1056 | 5.044 | 6.4
```


# KEY CONFIG FILES (full)


## app/risk/exit_policy.yaml
```
# ============================================================
# FTSeek-US 退出策略 — 主线生产 SSOT (Single Source of Truth)
# ============================================================
# 适用范围: ExitMonitor + atr_stop_engine + trailing_stop.py
# 读取者: app/risk/position_exit_monitor.py, app/pipeline/position_monitor.py
#         (exit_rule_dispatcher.py 已于 2026-08-12 随架构体检删除)
#
# ⚠️ 不管辖 (2026-09-06 勘明, PROPOSAL-20260906B P0):
#   PLAYBOOK_SIM / PLAYBOOK_LIVE 仓 (DA/UE/reentry 三源落库仓) 不读本文件——
#   它们走 playbook 自管链 _check_playbook_exit_rules (本体 app/playbook/exit_engine.py,
#   2026-09-06 P2a 自 pipeline.py 逐字迁出, 旧名在 pipeline.py 留转发),
#   退出参数来自武装指令 meta (如 reentry 的 ATR 相对化 SL/TP) 与
#   config/unified_config.yaml 的 playbook 节。本文件改名前, 请勿据此推断 playbook 仓行为。
#
# ⚠️ 不要与以下 yaml 混淆:
#   - config/exit_profiles.yaml — harness/shadow_bridge 影子系统专用 (回测/辩论)
#   - config/unified_config.yaml::dynamic_exit — regime-aware 兜底参数, 优先级低于本表
#
# ── 极简退出 (2026-06-21): 4条规则, 0分批, 0状态机 ──
# [1] ATR止损: entry ± 2.0×ATR, max 2.0% (atr_stop)
# [2] 保本线: 盈利≥1.5% → SL移至成本 (breakeven, 不平仓)
# [3] 时间退出: 持仓>1h 且 盈利<0.3% → 保本退出 (time_exit)
# [4] 盘末退出: 收盘前8min, 盈利全平, 亏损保留+隔夜-3%止损 (pre_close)
#
# 数据依据 (2026-06-21 完整回测):
#   - TRAILING_STOP 84笔 -$3,773 → 禁用, 被保本ratchet替代
#   - STRUCTURE_EXIT 33笔 -$3,106 → 禁用, 误触发多
#   - scale_out 仅4笔触发 → 禁用, 形同虚设
#   - DAY_TRADE_TP 13笔 100%WR +$13K → 保留1.5%阈值作为保本触发
#   - 4-24h持仓 70.7%WR +$21K → 时间退出给趋势发展空间
#   - 1-4h持仓 47.5%WR -$9.5K → 1h保本退出解决死亡区间
#   - 新方案模拟: +$34,194 vs 当前+$23,034 (+48.4%)
#
# 设计原则:
# 1. 截断亏损 (ATR 2.0× + 2%硬上限)
# 2. 让利润奔跑 (盈利≥1.5%只移SL不平仓, 跑到盘末)
# 3. 解决死亡区间 (1h无进展→保本退出)
# 4. 极简 (4条规则, 无分批状态机)
# ============================================================

version: "3.0"
last_updated: "2026-06-21"
design_rationale: "极简4规则: ATR 2.0×止损 + 1.5%保本ratchet + 2h时间退出(放宽) + 盘末退出. 禁用trailing/scale_out/structure_exit"

# ── DEAD RULE: progressive_trailing ──
# P1 (2026-06-14): 禁用 — current_sl 写入17次读取0次, 死代码
progressive_trailing:
  enabled: false
  effective_atr_floor_ratio: 0.35
  atr_floor_coef: 0.85
  atr_floor_price_pct: 0.0
  session_blend_window_minutes: 90.0

# ── RFC-057 (2026-06-01): 全局入场冷却 (grace period) ──
# 入场后 min_hold_minutes 内仅允许 atr_stop/SL_HIT (硬止损)
# 2026-07-07 说明: 本配置仅用于 ExitMonitor 管理的主线/ORB 仓位.
#   PLAYBOOK_SIM/LIVE 仓位由 app/playbook/pipeline.py 独立管理, 其开盘冷却期
#   为前 15 分钟且 SL 放宽到 4% (参见 pipeline.py _check_playbook_exit_rules).
global_hold_protection:
  enabled: true
  min_hold_minutes_after_entry: 45
  max_grace_loss_pct: 0.03
  allowed_during_grace:
    - circuit_breaker
    - earnings_guard
    - pre_close
    - atr_stop
  log_each_block: true

# ── 退出监控调度参数 ──
exit_monitor:
  scan_interval_seconds: 60
  cooldown_seconds: 120
  min_hold_minutes: 45

# ── 退出规则互斥 ──
exit_rules_exclusivity:
  pre_close: [trailing_stop, time_exit]
  time_exit: [trailing_stop]

# ── 全局风控 ──
global:
  # ⚠️ 本文件按**具名键**被消费 (exit_policy_guardian 逐键 .get(), 无泛型遍历) → "写了不代表生效"。
  #   2026-08-28 普查 (仓内 *.py + 仓外 /root/.hermes/scripts) 逐键核对 4 键:
  #     max_consecutive_losses → app/risk/absolute_circuit_breaker.py + symbol_daily_loss_guard.py ✅
  #     max_position_pct       → app/pipeline/llm_pipeline.py + position_monitor.py ✅
  #     max_daily_drawdown_pct ← ❌ 零消费方。日亏损防线实际在 absolute_circuit_breaker 按
  #         净值百分比档位 (减仓/停新仓两档) 执行, 与本键无关, 本键改了也无人读。
  #     max_total_positions    ← ❌ 零消费方。并发/日开仓帽实际在 unified_config
  #         playbook.execution_parameters.max_daily_positions / max_new_positions_per_cycle。
  #   两个 ❌ 是"装饰性风险旋钮": 读这份 SSOT 的人会以为此处守着 3% 日回撤帽 + 8 只并发帽,
  #   而真实防线在别处且数值不同 — 比缺失更危险 (与 ACB"强制全平"虚假声明同族)。
  #   接线还是删除属风险口径变更, 留人工裁决; 今日只标注。回滚: 删本段注释。
  max_daily_drawdown_pct: 3.0
  max_consecutive_losses: 3
  max_total_positions: 8
  max_position_pct: 15

# ── 退出规则定义 (唯一真相源) ──
exit_rules:

  # ── REMOVED RULE: flash_exit (2026-06-20) ──
  # 6笔全亏 0%WR 纯噪音, 代码+接线已删.

  # 规则 1: ATR 固定止损 (风险控制)
  # 2026-06-30 DEB: 按波动率桶动态选择 atr_mult 和 max_loss_pct
  #   桶判定: ATR% = ATR / entry_price, 按 vol_bucket_multipliers 选桶
  #   滞回区间: ±0.2% 防止边界抖动 (Claude Code建议)
  #   SL = min(atr_stop(盘中ATR×桶mult), hard_stop(桶max_loss%))
  #   SL契约: 再与 initial_sl 取更紧的 (floor保护)
  atr_stop:
    enabled: true
    method: "entry_price - atr * multiplier"
    multiplier_by_profit:
      initial: 2.0                   # 默认medium桶(盘中动态覆盖)
    max_loss_pct: 2.0                # 默认medium桶(盘中动态覆盖)
    atr_floor_pct: 70
    # 波动率桶覆盖 — 按ATR%动态选择 atr_mult, max_loss_pct, take_profit_pct
    # 2026-07-05: 分桶×分方向 SL/TP (374笔真实数据推导)
    #   亏损中位数→SL, 盈利中位数→TP, 按桶×方向差异化
    #   direction_override: 7/24后 guardian代码消费; 当前yaml预配置
    vol_bucket_multipliers:
      low:
        atr_pct_max: 0.015           # ATR% < 1.5%
        atr_multiplier: 1.5          # 低波动: SL收紧
        max_loss_pct: 1.5            # 全局fallback(方向无override时用)
        take_profit_pct: 0.4         # 盘中TP: 盈利0.4%部分止盈
        direction_override:          # 分方向SL/TP (374笔数据: low桶亏损中位数0.24%/0.31%)
          LONG:  {max_loss_pct: 0.3, take_profit_pct: 0.4}   # 129笔LONG: 亏损中位0.24%×1.25=0.3%
          SHORT: {max_loss_pct: 0.4, take_profit_pct: 0.4}   # 157笔SHORT: 亏损中位0.31%×1.3=0.4%
      medium:
        atr_pct_max: 0.035           # 1.5% ≤ ATR% < 3.5%
        atr_multiplier: 2.0          # 标准
        max_loss_pct: 2.0
        take_profit_pct: 3.0         # 盘中TP: 盈利3%部分止盈
        direction_override:          # 16笔LONG+25笔SHORT
          LONG:  {max_loss_pct: 2.7, take_profit_pct: 3.0}   # 亏损中位2.25%×1.2=2.7%
          SHORT: {max_loss_pct: 2.7, take_profit_pct: 2.8}   # 亏损中位2.26%×1.2=2.7%, 盈利中位1.88%×1.5=2.8%
      high:
        atr_pct_max: 0.08            # 3.5% ≤ ATR% < 8%
        atr_multiplier: 2.5          # 高波动: SL放宽防噪声
        max_loss_pct: 5.0            # 从3.0→5.0 (数据: 亏损中位4.27%/7.47%)
        take_profit_pct: 9.0         # 盘中TP: 盈利9%部分止盈
        direction_override:          # 12笔LONG+16笔SHORT (样本偏少, 置信度中低)
          LONG:  {max_loss_pct: 5.1, take_profit_pct: 9.0}   # 亏损中位4.27%×1.2=5.1%, 盈利中位6.17%×1.5=9.3%
          SHORT: {max_loss_pct: 9.0, take_profit_pct: 8.9}   # 亏损中位7.47%×1.2=9.0%, 盈利中位5.93%×1.5=8.9%
      extreme:
        atr_pct_max: 999.0           # ATR% ≥ 8%
        atr_multiplier: 3.0          # 极端波动: SL最宽
        max_loss_pct: 4.0            # 样本不足, 维持现状
        # extreme无direction_override: 样本<3笔, 数据不足以推导
    # 滞回区间: 桶切换时需超过边界±0.2%, 防止频繁切换
    hysteresis_pct: 0.002

  # 规则 2: 保本线 (利润保护, 不平仓)
  # 2026-06-21: 新设计 — 盈利≥1.5%时SL移至成本
  # 2026-06-25: 阶梯式利润锁定 (stepped ratchet)
  # 2026-06-28 (ADR-008): 按辩论结论禁用 — 2% SL 下 ratchet 意义有限, 由 pre_close 替代利润保护
  # 2026-07-07 说明: 本文件规则仅适用于 ExitMonitor 管理的主线/ORB 仓位.
  #   PLAYBOOK_SIM/LIVE 仓位由 app/playbook/pipeline.py 的 factor_exit_rules 独立管理,
  #   其中 breakeven 默认启用 (breakeven_at_pct=0.4, 2026-08-17 be_0.4 落地), 二者 SSOT 分离.
  breakeven:
    enabled: false
    trigger_profit_pct: 1.5          # 保本触发: SL移至entry
    lock_1pct_trigger: 3.0           # 锁1%利润: SL移至entry×1.01
    lock_3pct_trigger: 5.0           # 锁3%利润: SL移至entry×1.03
    buffer_pct: 0.0                  # 保本档: SL = entry × (1 + 0%) = entry (纯保本)

  # ── DEAD RULE: chandelier_trail ──
  # 92笔 46.7%WR -$34 接近零EV, 已禁用
  chandelier_trail:
    enabled: false

  # ── DEAD RULE: scale_out (2026-06-21) ──
  # 4笔触发形同虚设, 分批状态机增加复杂度无收益
  # 数据: 50%锁利反而少赚 (ORCL $2,258→$1,328)
  scale_out:
    enabled: false
    tier_mode: pct
    tiers: []

  # ── DEAD RULE: velocity_exit (2026-06-20) ──
  # 样本不足且与 trailing_stop 冲突, 代码+接线已删.

  # 规则 3: 时间退出 (解决1-4h死亡区间)
  # 2026-06-21: 新规则; 2026-06-24: 放宽 (1h/0.3%→2h/-0.5%)
  # 2026-06-28 (ADR-008): 按辩论结论禁用 — 亏损仓应交给硬止损, 不需要等2h
  # 2026-07-07 说明: 本文件规则仅适用于 ExitMonitor 管理的主线/ORB 仓位.
  #   因 7/3 起 ExitMonitor 不再管理 PLAYBOOK_SIM/LIVE 仓位, pipeline 于 7/7 重新启用
  #   TIME_EXIT (由 factor_exit_rules.time_exit_hours 驱动), 二者 SSOT 分离.
  time_exit:
    enabled: false
    holding_hours: 2.0              # 2026-06-24: 1h→2h, 让趋势发展
    max_profit_pct: -0.5            # 2026-06-24: 0.3%→-0.5%, 只砍真亏仓位
    action: "breakeven_exit"        # 退出方式: 尽量保本 (市价单, 可能微亏/微盈)

  # 规则 4: 盘末退出 (Pre-Close Exit)
  # 2026-06-30 DEB: 改为"仅盈利仓"模式 — 盈利落袋+亏损由SL管理
  # 2026-07-05: loss_flat改false — 374笔数据: 亏损仓盘末退均亏$500 vs 硬止损均亏$27, 差18倍
  # close_profit_atr=2.0: 浮盈≥2ATR全平(落袋)
  # loss_flat=false: 亏损仓不等盘末, 由atr_stop硬止损管理(数据支撑: 均亏$27 vs $500)
  # half_exit_below_atr=0.5: 微利0~0.5ATR减半仓
  pre_close_exit:
    enabled: true
    window_minutes: 8
    min_hold_minutes_before_pre_close: 30
    time_weighted: false
    loss_flat: false               # true→false: 亏损仓不等到盘末, 由硬止损管理
    overnight_loss_stop_pct: 0.03
    close_profit_atr: 2.0
    half_exit_below_atr: 0.5
    hold_threshold_atr: 99
    quarter_exit_below_atr: 99

  # 自适应分级退出 (盘末细化)
  # 2026-06-30 DEB: 同步启用, 仅盈利仓模式
  pre_close:
    enabled: true
    mode: adaptive
    profit_exit:
      min_profit_atr: 2.0
      close_ratio: 1.0
    breakeven_exit:
      tolerance_atr: 0.3
      close_ratio: 0.50
    loss_exit:
      min_loss_atr: 99
      close_ratio: 0.0
    force_close_remaining: false
    window_minutes:
      trending: 10
      range_bound: 5
      high_vol: 15
      default: 10

  # ── DEAD RULE: time_stop (2026-06-20) ──
  # 30天 sharpe=-0.40, 被 time_exit 替代

  # 规则 5: 成本缓冲 (已禁用)
  cost_buffer:
    enabled: false

  # 规则 6: 财报保护
  earnings_guard:
    enabled: true
    days_before: 2
    days_after: 1
    action: "reduce_position_50pct"
    flash_multiplier_override: 0.5

# ── 行业差异化配置 (简化: 统一2.0×ATR) ──
# 原因: 行业差异由ATR自身已反映(高波股ATR大→止损距离自动放大)
sector_overrides:
  default:
    atr_multiplier: 2.0
  tech:
    atr_multiplier: 2.0
  biotech:
    atr_multiplier: 2.0
    max_position_pct: 5
  utility:
    atr_multiplier: 2.0

# ── 做空差异化 ──
# 2026-06-21: 统一2.0×ATR + 2.0%上限, 不再区分做空
# 原因: 模拟显示统一2.0%上限SHORT 0恶化 (vs 2.5%少亏$1,972)
short_overrides:
  atr_multiplier: 2.0              # 4.0→2.0, 与atr_stop.initial一致
  max_loss_pct: 2.0               # 10%→2.0%, 统一硬上限

  # ── DEAD: technical_breach (2026-06-21) ──
  # STRUCTURE_EXIT 33笔 -$3,106, 30%WR, 误触发多
  # 被 ATR 2.0× 止损自然替代
  technical_breach:
    enabled: false

  # ── DEAD: scale_out 做空差异化 ──
  scale_out:
    tier_1_pct: 10.0
    tier_2_pct: 20.0

# ── 时段自适应 ──
# 保留: ATR 止损距离 × session_vol_ratio (开盘波动大→自动放宽)
session_adaptation:
  enabled: true
  source: "shared_kernel"
  apply_to:
    - atr_stop
  path_dependency:
    enabled: true
    rule: "低置信度 session 入场 (opening_drive) → 止损收紧 0.85x"

# ── DEAD RULE: day_trade_mode ──
day_trade_mode:
  enabled: false

```


## app/risk/stop_loss_config.py
```
"""
止损配置 Single Source of Truth (SoT) — P1 2026-05-20

设计原则:
  1. 所有止损参数从 unified_config.yaml 读取，不硬编码
  2. 提供统一的 get_stop_loss_pct() 接口，按策略/市况返回
  3. 保持向后兼容：旧代码可继续用 config['stop_loss'] 等字段
  4. 启动时验证：所有策略的止损参数必须存在且合理

止损参数层级 (优先级从高到低):
  1. LLM 策略自定义 (signal.metadata['custom_stop_loss_pct'])
  2. 市况匹配 (dynamic_exit.strong_trend_up/range_bound/...)
  3. 策略默认 (strategies.defaults.stop_loss_pct)
  4. 系统兜底 (dynamic_exit.default.stop_loss)
"""

from __future__ import annotations

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
#  默认值 (YAML 缺失时的兜底)
# ═══════════════════════════════════════════════════════════════════

_DEFAULT_STOP_LOSS = {
    "default": -0.03,        # 3% 止损
    "strong_trend_up": -0.08,
    "strong_trend_down": -0.03,
    "range_bound": -0.035,
    "high_volatility": -0.07,
    "intraday": -0.03,
}

_DEFAULT_TAKE_PROFIT = {
    "default": 0.10,
    "strong_trend_up": 0.25,
    "strong_trend_down": 0.045,
    "range_bound": 0.05,
    "high_volatility": 0.09,
    "intraday": 0.05,
}


class StopLossConfig:
    """止损配置管理器 — 全局唯一 SoT"""

    _instance: Optional[StopLossConfig] = None
    _config: Dict[str, Any] = {}

    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self._validate()

    @classmethod
    def initialize(cls, config: Dict[str, Any]) -> "StopLossConfig":
        """初始化 (服务启动时调用一次)"""
        cls._instance = cls(config)
        logger.info("[StopLossSoT] ✅ 初始化完成")
        return cls._instance

    @classmethod
    def get_instance(cls) -> "StopLossConfig":
        """获取单例 (未初始化时用默认值)"""
        if cls._instance is None:
            logger.warning("[StopLossSoT] ⚠️ 未初始化，使用默认值")
            cls._instance = cls({})
        return cls._instance

    def _validate(self):
        """启动时验证止损参数"""
        dynamic_exit = self._config.get("dynamic_exit", {})
        if not dynamic_exit:
            logger.warning("[StopLossSoT] dynamic_exit 配置缺失，使用默认值")
            return

        default_sl = dynamic_exit.get("default", {}).get("stop_loss")
        if default_sl is None:
            logger.error("[StopLossSoT] ❌ dynamic_exit.default.stop_loss 缺失!")
        elif default_sl > 0:
            logger.error(f"[StopLossSoT] ❌ stop_loss 应为负数: {default_sl}")

        # 验证所有 regime 配置
        for regime in ["strong_trend_up", "strong_trend_down", "range_bound", "high_volatility"]:
            regime_config = dynamic_exit.get(regime, {})
            sl = regime_config.get("stop_loss")
            if sl is None:
                logger.warning(f"[StopLossSoT] {regime}.stop_loss 缺失，使用默认")
            elif sl > 0:
                logger.warning(f"[StopLossSoT] {regime}.stop_loss 应为负数: {sl}")

    def get_stop_loss_pct(
        self,
        strategy_id: str = "voting_llm",
        market_regime: str = "default",
        custom_sl: Optional[float] = None,
    ) -> float:
        """
        获取止损百分比 (负数表示亏损)

        优先级: custom_sl > regime > strategy_default > system_default

        Args:
            strategy_id: 策略 ID (如 "voting_llm")
            market_regime: 市场状态 (如 "strong_trend_up", "range_bound")
            custom_sl: LLM 策略自定义止损 (最高优先级)

        Returns:
            止损百分比 (负数, 如 -0.05 表示 5% 止损)
        """
        # 1. LLM 自定义 (最高优先级)
        if custom_sl is not None:
            return -abs(custom_sl)  # 确保为负数

        # 2. 市况匹配
        dynamic_exit = self._config.get("dynamic_exit", {})
        regime_config = dynamic_exit.get(market_regime, {})
        if regime_config and "stop_loss" in regime_config:
            sl = regime_config["stop_loss"]
            if isinstance(sl, (int, float)) and sl < 0:
                return sl
            elif isinstance(sl, (int, float)) and sl > 0:
                return -sl  # 兼容正数写法

        # 3. 策略默认
        strategies = self._config.get("strategies", {})
        strategy_config = strategies.get(strategy_id, {})
        if strategy_config and "stop_loss_pct" in strategy_config:
            sl = strategy_config["stop_loss_pct"]
            if isinstance(sl, (int, float)):
                return sl if sl < 0 else -sl

        # 4. 系统兜底
        default_sl = dynamic_exit.get("default", {}).get("stop_loss", _DEFAULT_STOP_LOSS["default"])
        if isinstance(default_sl, (int, float)):
            return default_sl if default_sl < 0 else -default_sl

        return _DEFAULT_STOP_LOSS["default"]

    def get_take_profit_pct(
        self,
        strategy_id: str = "voting_llm",
        market_regime: str = "default",
        custom_tp: Optional[float] = None,
    ) -> float:
        """获取止盈百分比 (正数)"""
        if custom_tp is not None:
            return abs(custom_tp)

        dynamic_exit = self._config.get("dynamic_exit", {})
        regime_config = dynamic_exit.get(market_regime, {})
        if regime_config and "take_profit" in regime_config:
            tp = regime_config["take_profit"]
            if isinstance(tp, (int, float)):
                return abs(tp)

        strategies = self._config.get("strategies", {})
        strategy_config = strategies.get(strategy_id, {})
        if strategy_config and "take_profit_pct" in strategy_config:
            tp = strategy_config["take_profit_pct"]
            if isinstance(tp, (int, float)):
                return abs(tp)

        return _DEFAULT_TAKE_PROFIT.get(market_regime, _DEFAULT_TAKE_PROFIT["default"])

    def get_all_regimes(self) -> Dict[str, Dict[str, float]]:
        """获取所有市况的止损止盈配置 (用于启动验证)"""
        result = {}
        dynamic_exit = self._config.get("dynamic_exit", {})
        for regime in ["default", "strong_trend_up", "strong_trend_down", "range_bound", "high_volatility"]:
            regime_config = dynamic_exit.get(regime, {})
            sl = regime_config.get("stop_loss", _DEFAULT_STOP_LOSS.get(regime, _DEFAULT_STOP_LOSS["default"]))
            tp = regime_config.get("take_profit", _DEFAULT_TAKE_PROFIT.get(regime, _DEFAULT_TAKE_PROFIT["default"]))
            result[regime] = {
                "stop_loss": sl if isinstance(sl, (int, float)) else _DEFAULT_STOP_LOSS["default"],
                "take_profit": tp if isinstance(tp, (int, float)) else _DEFAULT_TAKE_PROFIT["default"],
            }
        return result


# ═══════════════════════════════════════════════════════════════════
#  便捷函数 (全局访问)
# ═══════════════════════════════════════════════════════════════════

def get_stop_loss_pct(strategy_id: str = "voting_llm", market_regime: str = "default", custom_sl: Optional[float] = None) -> float:
    """获取止损百分比 (全局便捷函数)"""
    return StopLossConfig.get_instance().get_stop_loss_pct(strategy_id, market_regime, custom_sl)


def get_take_profit_pct(strategy_id: str = "voting_llm", market_regime: str = "default", custom_tp: Optional[float] = None) -> float:
    """获取止盈百分比 (全局便捷函数)"""
    return StopLossConfig.get_instance().get_take_profit_pct(strategy_id, market_regime, custom_tp)

```


## app/strategy/exit_policies.py
```
"""
ExitPolicy 实现 — Phase B 升级 (PB-3, 2026-05-15)

每个策略对应一个 ExitPolicy. PB-3 (Phase B.2 minimal):
- 从 NO_ACTION stub 升级到 thesis-aware
- 优先按 entry_thesis 选退出策略
- thesis 缺失 → fallback NO_ACTION (走 PositionExitMonitor 6 规则)

thesis 退出规则:
- momentum: chandelier_trail (顺势保利)
- breakout: 跌破前高/前低即退 (technical breach)
- reversion: 紧 ATR 短线 (1.5x), 5min trail
- event: 事件 horizon 到期强平 (24-48h)
- technical_setup: trail + 形态破位
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    """ExitPolicy.evaluate() 返回值"""
    action: str = "NO_ACTION"     # NO_ACTION | REDUCE | CLOSE
    qty_pct: float = 0.0
    reason: str = ""
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


def _read_entry_thesis(position: Any) -> Optional[str]:
    """从 trade_attribution 读 entry_thesis (异步 thesis_inferer 写入)"""
    try:
        import sqlite3
        from app.core.paths import get_trade_records_db
        trace_id = getattr(position, "trace_id", "") or ""
        if not trace_id:
            return None
        conn = sqlite3.connect(str(get_trade_records_db()))
        row = conn.execute(
            "SELECT entry_thesis FROM trade_attribution WHERE trace_id = ? LIMIT 1",
            (trace_id,)
        ).fetchone()
        conn.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


class _BaseExitPolicy:
    """基础 ExitPolicy — thesis-aware (PB-3 2026-05-15)"""
    name: str = "base"
    owner: str = "main"

    def evaluate(self, position: Any, ctx: Any) -> ExitDecision:
        thesis = _read_entry_thesis(position)
        if not thesis:
            # 无 thesis → fallback NO_ACTION (走 PositionExitMonitor 6 规则)
            return ExitDecision(
                action="NO_ACTION",
                reason="no_thesis_fallback_to_legacy",
                metadata={"owner_strategy": self.owner},
            )

        # thesis-driven 退出规则
        return self._evaluate_by_thesis(position, ctx, thesis)

    def _evaluate_by_thesis(self, position: Any, ctx: Any, thesis: str) -> ExitDecision:
        """子类按 owner+thesis 组合定制. 默认 NO_ACTION"""
        return ExitDecision(
            action="NO_ACTION",
            reason=f"thesis={thesis}_no_specific_rule",
            metadata={"owner_strategy": self.owner, "thesis": thesis},
        )


class MainPipelineExitPolicy(_BaseExitPolicy):
    name = "main_exit"
    owner = "main"

    def _evaluate_by_thesis(self, position: Any, ctx: Any, thesis: str) -> ExitDecision:
        """主线 thesis-driven 退出规则"""
        cur = float(ctx.get("current_price", 0) or 0)
        entry = float(getattr(position, "avg_entry_price", 0) or 0)
        if cur <= 0 or entry <= 0:
            return ExitDecision(action="NO_ACTION", reason="missing_price")

        direction = str(getattr(position, "direction", "LONG"))
        is_long = "LONG" in direction.upper()
        pnl_pct = (cur - entry) / entry if is_long else (entry - cur) / entry

        # 1. event: 事件驱动 — 持仓时长上限 48h, 到期强平
        if thesis == "event":
            holding_hours = float(getattr(position, "holding_hours", 0) or 0)
            if holding_hours > 48:
                return ExitDecision(
                    action="CLOSE", qty_pct=1.0,
                    reason="EVENT_HORIZON_48H",
                    priority=10,
                    metadata={"thesis": thesis},
                )

        # 2. reversion: 反转入场 — 浮盈 +1.5% 立即减半保利
        if thesis == "reversion":
            if pnl_pct >= 0.015:
                return ExitDecision(
                    action="REDUCE", qty_pct=0.5,
                    reason="REVERSION_QUICK_PROFIT",
                    priority=8,
                    metadata={"thesis": thesis, "pnl_pct": pnl_pct},
                )

        # 3. breakout: 跌破入场价 -0.5% 即退 (设的不是大止损)
        if thesis == "breakout":
            if pnl_pct <= -0.005:
                return ExitDecision(
                    action="CLOSE", qty_pct=1.0,
                    reason="BREAKOUT_FAILED",
                    priority=9,
                    metadata={"thesis": thesis, "pnl_pct": pnl_pct},
                )

        # momentum / technical_setup → 走 chandelier (PositionExitMonitor 默认)
        return ExitDecision(
            action="NO_ACTION",
            reason=f"main_thesis={thesis}_delegate",
            metadata={"thesis": thesis},
        )


class FastLaneExitPolicy(_BaseExitPolicy):
    name = "fast_lane_exit"
    owner = "fast_lane"

    def _evaluate_by_thesis(self, position: Any, ctx: Any, thesis: str) -> ExitDecision:
        """FastLane: 1ATR 立即保本"""
        atr = float(ctx.get("atr", 0) or 0)
        cur = float(ctx.get("current_price", 0) or 0)
        entry = float(getattr(position, "avg_entry_price", 0) or 0)
        if atr <= 0 or cur <= 0 or entry <= 0:
            return ExitDecision(action="NO_ACTION", reason="missing_data")
        is_long = "LONG" in str(getattr(position, "direction", "LONG")).upper()
        profit_atr = (cur - entry) / atr if is_long else (entry - cur) / atr
        if profit_atr >= 1.0:
            return ExitDecision(
                action="REDUCE", qty_pct=0.5,
                reason="FAST_LANE_1ATR_PROFIT_LOCK",
                priority=8,
                metadata={"thesis": thesis, "profit_atr": profit_atr},
            )
        return ExitDecision(action="NO_ACTION", reason="fast_lane_below_1atr")


class LLMPlanExitPolicy(_BaseExitPolicy):
    name = "llm_plan_exit"
    owner = "llm_plan"

    def _evaluate_by_thesis(self, position: Any, ctx: Any, thesis: str) -> ExitDecision:
        """LLM Plan: 按 BattlePlan 中的 target/stop 直接退出"""
        # 暂从 position.metadata 取 plan_target / plan_stop (Phase B 完整实现)
        meta = getattr(position, "metadata", None) or {}
        plan_target = float(meta.get("plan_target", 0) or 0)
        plan_stop = float(meta.get("plan_stop", 0) or 0)
        cur = float(ctx.get("current_price", 0) or 0)
        if cur <= 0:
            return ExitDecision(action="NO_ACTION", reason="missing_price")
        is_long = "LONG" in str(getattr(position, "direction", "LONG")).upper()

        # 命中 plan target → 全平
        if plan_target > 0:
            hit = (is_long and cur >= plan_target) or (not is_long and cur <= plan_target)
            if hit:
                return ExitDecision(
                    action="CLOSE", qty_pct=1.0,
                    reason="LLM_PLAN_TARGET_HIT",
                    priority=10,
                    metadata={"thesis": thesis, "plan_target": plan_target},
                )
        # 命中 plan stop → 全平
        if plan_stop > 0:
            hit = (is_long and cur <= plan_stop) or (not is_long and cur >= plan_stop)
            if hit:
                return ExitDecision(
                    action="CLOSE", qty_pct=1.0,
                    reason="LLM_PLAN_STOP_HIT",
                    priority=10,
                    metadata={"thesis": thesis, "plan_stop": plan_stop},
                )
        return ExitDecision(action="NO_ACTION", reason="llm_plan_neither_target_nor_stop")

```


# KEY CODE SECTIONS

# /opt/ftseek/FTSeek-US/app/playbook/exit_engine.py (total 1129 lines)

## lines 1-100
```python
"""Playbook 退出引擎 — 自管退出规则链 (P2a 纯搬家, 2026-09-06).

本模块全部代码逐字迁自 app/playbook/pipeline.py (9453 行单体), 零行为变更:
  - _check_playbook_exit_rules  (711 行, 规则[0]冷却/[0.5]早期紧SL/[1]SL/[1.5]TP/
    [2]OVERNIGHT_GAP_STOP/[3]PRE_CLOSE/[3.4]MFE_STALL/[4]TIME_EXIT/[5]MAX_HOLD)
  - 其私有 helper: _is_overnight_position / _get_factor_exit_config (+_factor_exit_warned)
    / _compute_dynamic_effective_sl / _load_horizon_config
  - 进程级日志去重集 _EARLY_TIGHT_SL_LOGGED / _LOSER_DS_EXEMPT_LOGGED
  - P7 埋点缝 _EARLY_DEAD_SHADOW_PATH (tests/conftest.py 登记已同步迁至本模块)

隔夜豁免判定不在此复制: EOD 单点走 app.playbook.overnight_decision (P1),
模块顶部以同名别名引入, 使下方迁入函数体保持逐字不变。
pipeline.py 侧保留同名转发器 (外部调用点/既有测试 import 路径不变)。

回滚: git revert 本提交即可 (pipeline 转发器与本体并存, 无双写)。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict

from app.playbook.overnight_decision import (
    horizon_max_hold_hit as _horizon_max_hold_hit,
    overnight_eod_exempt as _overnight_eod_exempt,
    w_week_loser_exempt as _loser_overnight_exempt,
)

logger = logging.getLogger(__name__)

# P7 埋点缝 (值与解析语义逐字不变; 隔离登记在 tests/conftest.py _PRODUCTION_DATA_SEAMS)
_EARLY_DEAD_SHADOW_PATH = Path("data/early_dead_signal_shadow.jsonl")

# 惰性 ET 时区访问 (与 pipeline._now_et 同模式; SSOT = app.core.scheduler_triggers.ET)
_ET = None


def _now_et():
    global _ET
    if _ET is None:
        from app.core.scheduler_triggers import ET as _et
        _ET = _et
    return _ET


def _is_overnight_position(entry_time_str: str) -> bool:
    """RFC-061 #6: 仓位是否隔夜 (entry 在之前的交易日, 非今日入场).

    用于隔夜深亏快平 (OVERNIGHT_GAP_STOP): 持仓过夜 且 亏损 ≥ 阈值 即快平。
    2026-09-24 勘误: 名称里的 "GAP" 是历史命名 — 判据里没有 gap 校验, 开盘跳空只是其中一种触发。
    用 ET 日期判断. 异常返回 False (不触发, 保守).
    """
    if not entry_time_str:
        return False
    try:
        _et = _now_et()
        _entry = datetime.fromisoformat(entry_time_str)
        if hasattr(_entry, "tzinfo") and _entry.tzinfo is not None:
            _entry_date = _entry.astimezone(_et).date()
        else:
            _entry_date = _entry.date()
        return _entry_date < datetime.now(_et).date()
    except Exception:
        return False



def note_exit_gap(meta: dict, quote, exit_reason: str, current_price: float) -> None:
    """出场日 gap 证据采集 (record-only, RFC-106 §5.2 未决项落地).

    OVERNIGHT_GAP_STOP 判据无 gap 校验 (见 _is_overnight_position 勘误) — 本函数
    **不改任何判据/阈值/平仓动作**, 只在出场决策成立时把决策时刻报价里的
    prev_close / today_open 落盘, 供复盘区分「真开盘跳空」与「开盘后下杀」。
    与入场侧 entry_gap 对称 (playbook_service 复用 _arr_ctx 同款字段)。
    fail-open: quote 缺 prev_close/open → 不写键 (历史缺省语义不变), 绝不阻断平仓。
    回滚: 删本函数 + 两个调用点。
    """
    try:
        if not isinstance(meta, dict) or not isinstance(quote, dict) or meta.get("exit_gap"):
            return
        prev_close = float(quote.get("prev_close", 0) or 0)
        today_open = float(quote.get("open", 0) or 0)
        if prev_close <= 0 or today_open <= 0:
            return
        gap_pct = (today_open - prev_close) / prev_close * 100.0
        decision_price = float(current_price or 0)
        decision_vs_open_pct = (
            (decision_price - today_open) / today_open * 100.0
            if decision_price > 0 else None)
        meta["exit_gap"] = {
            "prev_close": round(prev_close, 4),
            "today_open": round(today_open, 4),
            "gap_pct": round(gap_pct, 3),
            "decision_price": round(decision_price, 4) if decision_price > 0 else None,
            "decision_vs_open_pct": round(decision_vs_open_pct, 3)
                if decision_vs_open_pct is not None else None,
            "quote_source": str(quote.get("data_source", "") or ""),
```

## lines 326-358 (matched: def _check_)
```python
    return bool(get_playbook_config().get("overnight_hold_enabled", False))


def _check_playbook_exit_rules(direction, entry_price, current_price, initial_sl,
                               peak_price, meta, held_minutes, minutes_to_close,
                               minutes_from_open, entry_time_str, earnings_guard=None):
    """共用退出规则判断 — 被 monitor_playbook_sim_exits 调用.

    计算盈利百分比 → peak tracking → breakeven ratchet / trailing stop (factor-specific) → 6 条退出规则.
    退出规则 (按优先级):
      [0] 开盘冷却期: 前15min SL放宽到4% (防开盘噪声秒杀)
      [0.5] 早期紧SL: 入场后30min内未浮盈达标 → SL收紧到1% (防方向全错慢性失血)
      [1] SL_INITIAL/SL_BREAKEVEN/SL_TRAIL_LOCK: effective_sl 触及 (按初始止损/保本/锁利区分)
      [1.5] TAKE_PROFIT: 触及止盈价 (RR≥1.5 才生效)
      [2] OVERNIGHT_GAP_STOP (名不副实, 2026-09-24 勘误): 持仓过夜 且 亏损 ≥
          overnight_gap_stop_pct 的快平 —— **无 gap 校验**, 开盘跳空只是其中一种触发方式
          (metadata 也不落 gap 字段, 复盘不要去查 gap%)
      [3] PRE_CLOSE_EXIT: 收盘前 8min (cohort B 盈利可跳过)
      [3.4] MFE_STALL_EXIT: 持仓≥150min 且 峰值浮盈<0.5% 且 当前水下 → 提前离场 (2026-07-24)
      [4] TIME_EXIT: 持仓 > factor_time_exit_hours 且 盈利<0 (factor-specific;
          隔夜仓按"今日开盘以来"计, 每日重置 — RFC-064)
      [5] MAX_HOLD_T5: cohort B 持仓≥5 交易日

    Args:
        direction: "LONG"/"SHORT"
        entry_price: 入场价
        current_price: 当前报价
        initial_sl: 初始 stop_loss (meta["stop_loss"])
        peak_price: 历史峰值价 (从 meta 恢复, 函数内更新后返回)
        meta: metadata dict (读取 cohort/_skip_allowed/_log_tag/_symbol/conditions)
        held_minutes: 持仓分钟数
        minutes_to_close: 距收盘剩余分钟 (ET)
        minutes_from_open: 开盘后分钟数 (ET)
```

# /opt/ftseek/FTSeek-US/app/risk/position_exit_monitor.py (total 2556 lines)

## lines 1-100
```python
"""
FTSeek-US 持仓退出监控 (Position Exit Monitor)

从 stoploss_daemon 提取的核心止损/止盈逻辑，合并到主进程内执行。
消除独立 Daemon 的双写入者竞争问题，确保：
  1. 单一写入者 (无 DB 竞争)
  2. 复用主进程 Futu 连接 (不消耗额外槽位)
  3. trail_state 实时更新 (不再全 0.0)
  4. 内存 SSOT (PositionStore → DB 单向同步)

规则 (由 exit_policy.yaml 统一配置):
  0. 防振荡: 同一持仓 60 秒内不重复触发
  1. Flash Exit: 60s 内波动 > flash_multiplier × ATR (优化: 1.0x, 60s扫描)
  1.5. Day Trade Mode: 紧止损(1.5xATR/3%) + 主动止盈(+4%/+8%/+15%) + 时间衰减 (R/R≥1.33)
  2. ATR 动态止损: entry - atr × multiplier (非线性状态机, initial=2.0x)
  2.5. Scale Out 分批止盈: 5%/10%/18% 三层
  3. Chandelier 追踪: 峰值 - atr × trail_mult (盈利≥1.2ATR 激活, regime差异化)
  4. 保本触发: 盈利≥1.0ATR 移至成本价+buffer
  3.5. Pre-Close Exit: 分层退出 + 时间加权 (亏损全平/0~0.5ATR减半/0.5~1ATR减1/4/≥1ATR过夜)
  5. Time Stop: > 72h 持仓

数学期望修正 (P0-2, 2026-04-28):
  - profit_atr 改为有向（signed）：盈利>0，亏损<0
  - 亏损时禁止激活 Chandelier / Breakeven（原 abs() 实现会误触发追踪线上移）
  - 止损定价逻辑下沉到 atr_stop_engine（纯函数，可单测/回测）
  - 接入 cost_buffer：止损线额外让出 k × total_cost_bps 价格空间

优化记录 (2026-05-03):
  - P0: Flash Exit 阈值 0.8→1.0x ATR, 扫描间隔 30→60s (历史2次Flash全部亏损)
  - P1: PRE_CLOSE_EXIT 改为分层退出 + 时间加权, 根因修复 54% 被动强平问题
  - P1: Day Trade Mode 参数全面放宽, 匹配 session-aware 实际波动
  - P1: Chandelier 激活门槛从 2.0ATR 降至 1.2ATR, regime乘数放宽 (给利润更多空间)
  - P2: 分层止盈门槛从 8%/15%/25% 降至 5%/10%/18%
  - P2: Chandelier regime乘数: normal 2.0→2.5, trending 3.0→3.5, volatile 1.5→2.0
"""
import logging
import math
import time
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from pathlib import Path

from app.core.db_utils import safe_sqlite_connect

# ATR 止损定价引擎（纯函数）
from .atr_stop_engine import compute_stop_lines, compute_profit_atr_signed, resolve_trail_multiplier

# Task #19 (2026-05-27): 退出规则模块化 — 各规则已拆分到 exit_rules/
# ExitMonitor 自身只负责编排 (规则调度 + 质量评分 + 执行平仓)
from .exit_rules import (
    ScaleOutRule,
    TrailingStopRule,
    PreCloseExitRule,
    check_scale_out as _impl_scale_out,
    check_trailing_stop as _impl_trailing_stop,
    check_pre_close_exit as _impl_pre_close_exit,
    # Task #48: 优先级注册表 (渐进式引入, 现仅作为辅助入口)
    ExitRuleRegistry,
    ExitRuleContext,
    ExitReason,
)

# 共享内核 — session-aware ATR (P2 2026-05-01)
from app.core.shared_kernel import get_shared_kernel

# C4 修复 (2026-05-13): 全局 EOD 配置 — 主线 + ORB 共享
from app.risk.eod_config import load_eod_config

# 退出质量追踪 (P2 2026-05-01)
try:
    from app.storage.exit_quality_tracker import get_exit_quality_tracker
    _QUALITY_TRACKER_AVAILABLE = True
except Exception:
    get_exit_quality_tracker = None  # type: ignore
    _QUALITY_TRACKER_AVAILABLE = False

# 退出规则质量评分 (P2 2026-05-27): 多规则触发时选最优
try:
    from app.risk.exit_quality_scorer import get_exit_quality_scorer as _get_scorer
    _EXIT_SCORER_AVAILABLE = True
except Exception:
    _get_scorer = None  # type: ignore
    _EXIT_SCORER_AVAILABLE = False

# 成本模型（可选，用于 cost_buffer）
try:
    from app.decision.cost_model import CostModel, CostModelConfig
    _COST_MODEL_AVAILABLE = True
except Exception:
    CostModel = None  # type: ignore
    CostModelConfig = None  # type: ignore
    _COST_MODEL_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger("ftseek.exit_monitor")

# 美东时区
try:
```

# /opt/ftseek/FTSeek-US/app/risk/atr_stop_engine.py (total 561 lines)

## lines 1-80
```python
"""ATR 止损定价引擎（纯函数，可单测）

职责
----
- 基于 exit_policy + cost_model 计算多候选止损线
- 修正 P0-2 数学期望错误：profit_atr 有向（盈利>0 / 亏损<0）
  亏损时禁止激活 Chandelier / Breakeven（原实现用 abs() 会把亏损也视作盈利，
  误触发追踪线上移，加速亏损）
- 接入 Regime 三档差异化 trail multiplier（normal / trending / volatile）
- 接入成本缓冲（cost buffer）：止损线额外让出 k × total_cost_bps 的价格空间，
  规避被噪声成本打穿

ATR 来源说明 (Task #4 统一ATR计算)
----------------------------------------
本模块为纯函数式止损计算器，本身不独立计算 ATR：atr 严格以参数形式传入。
调用方（position_exit_monitor / exit_rules/* / risk pipeline 等）需从统一的
ATRProvider（app.core.atr_provider）获取 ATR 后传入本模块，以保证与其他
ATR 消费方（TrendFirst / playbook 等）同根同源、数据一致。
限 fallback：调用方如拿不到 ATR，可以 entry_price * 0.02 作为保底估计。

模块严格无状态（无 I/O、无持久化），便于单元测试与回测重放。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("ftseek.atr_stop_engine")


# ============================================================
#  Regime 标准化
# ============================================================

# 外部可能传入的 regime 字符串 → 三档标准键
# 2026-06-05 修复: "range"/"RANGE" 映射到 "normal" (非 "volatile"),
# 与 trailing_stop.py 和 exit_policy_guardian 对齐.
# range-bound = 横盘震荡 = normal stops; volatile = 高波动 = wider stops.
_REGIME_MAP = {
    "normal": "normal", "range": "normal", "ranging": "normal",
    "range_bound": "normal", "RANGE": "normal", "RANGE_BOUND": "normal",
    "volatile": "volatile", "high_vol": "volatile", "chop": "volatile",
    "HIGH_VOL": "volatile", "high_volatility": "volatile",
    "trend": "trending", "trending": "trending",
    "uptrend": "trending", "downtrend": "trending",
    "bull": "trending", "bear": "trending",
    "TREND_UP": "trending", "TREND_DOWN": "trending",
}


def _normalize_regime(regime: Optional[str]) -> str:
    if not regime:
        return "normal"
    return _REGIME_MAP.get(str(regime).strip().lower(), "normal")


# ============================================================
#  结果数据结构
# ============================================================

@dataclass
class StopLinesResult:
    direction: str                  # LONG / SHORT
    entry_price: float
    current_price: float
    atr: float
    profit_atr_signed: float        # 有向：盈利>0，亏损<0
    atr_mult: float
    trail_mult: float
    hard_stop: float
    atr_stop: float
    chandelier_stop: float          # 0 表示未激活
    breakeven_stop: float           # 0 表示未激活
    scale_out_floor: float          # 2026-06-20: scale_out ratchet 下限 (0=未激活)
    cost_buffer_price: float        # 绝对价格空间
    final_stop: float
    triggered: bool
    reason_code: str                # HARD_STOP / ATR_STOP / CHANDELIER / BREAKEVEN / SCALE_OUT_FLOOR / NONE
    reason_cn: str
```

## lines 102-134 (matched: def )
```python
#  纯函数 API
# ============================================================

def compute_profit_atr_signed(
    direction: str,
    entry_price: float,
    current_price: float,
    atr: float,
) -> float:
    """计算有向 ATR 盈利数：盈利>0，亏损<0，输入无效返回 0.0"""
    # P0 修复 (2026-05-01): 类型防御 — 防止 dict/str 传入导致 TypeError
    if not isinstance(current_price, (int, float)):
        return 0.0
    if not isinstance(entry_price, (int, float)):
        return 0.0
    if atr <= 0 or entry_price <= 0 or current_price <= 0:
        return 0.0
    diff = (current_price - entry_price) if direction == "LONG" else (entry_price - current_price)
    return diff / atr


def resolve_trail_multiplier(
    policy: Dict[str, Any],
    regime: Optional[str] = "normal",
    direction: str = "LONG",
) -> float:
    """解析 Chandelier trail 倍数：regime 差异化 + 做空更紧"""
    rules = (policy or {}).get("exit_rules", {})
    by_regime = rules.get("chandelier_trail", {}).get("multiplier_by_regime", {})
    key = _normalize_regime(regime)
    
    # P0 修复 (2026-05-01): 防御性检查 — by_regime[key] 可能返回 dict 而非 float
    raw_trail = by_regime.get(key, 2.0)
```

## lines 160-192 (matched: def )
```python
    return trail


def apply_atr_constraints(
    *,
    atr_raw: float,
    price: float,
    policy: Dict[str, Any],
    symbol: Optional[str] = None,
    median_atr_14d: Optional[float] = None,
    earnings_guard: Any = None,
) -> float:
    """对原始 ATR 施加 floor/cap 双重约束 (Phase 3, 2026-05-31)

    Floor (下限):
        atr ≥ price × atr_floor_price_pct/100
        防止 ATR 过小导致止损距离形同虚设

    Cap (上限 / 财报保护):
        如 symbol 临近财报期, atr ≤ median_atr_14d × atr_cap_multiplier
        防止财报跳空导致 ATR 飙升 5-10x 让止损线被锁在极远处

    Args:
        atr_raw: 原始 ATR
        price: 当前价格 (用于计算下限)
        policy: exit_policy.yaml 加载后的 dict
        symbol: 标的 (财报保护需要)
        median_atr_14d: 14 日 ATR 中位数 (用于财报上限)
        earnings_guard: EarningsGuard 实例 (None 表示不启用财报保护)

    Returns:
        约束后的 ATR
    """
```

## lines 227-259 (matched: def )
```python
    return atr


def compute_stop_lines(
    *,
    direction: str,
    entry_price: float,
    current_price: float,
    atr: float,
    peak_price: float,
    policy: Dict[str, Any],
    regime: Optional[str] = "normal",
    atr_mult: float = 2.5,
    cost_bps: float = 0.0,
    scale_out_floor: float = 0.0,
) -> StopLinesResult:
    """核心 API：计算全部候选止损线 + 最终止损线 + 触发判定

    参数
    ----
    direction     : "LONG" | "SHORT"
    entry_price   : 开仓均价
    current_price : 当前价
    atr           : 已通过 volatility floor 处理后的 ATR（底限后的有效值）
    peak_price    : LONG=持仓期间最高价，SHORT=持仓期间最低价
    policy        : exit_policy dict（exit_policy.yaml 解析结果）
    regime        : 当前市况标签（normal/trending/volatile 或其别名）
    atr_mult      : ATR 动态止损倍数（由 guardian.get_atr_multiplier 给出）
    cost_bps      : 预期双边总成本（bp），用于 cost buffer
    scale_out_floor: 2026-06-20 scale_out ratchet 下限 (绝对价). >0 时作为候选止损线,
                    利用 max(LONG)/min(SHORT) "最保护优先" 聚合收紧剩余仓位止损.
                    来源: trail_state["scale_out_stop_floor"], 由 scale_out.py 写入.

```

# /opt/ftseek/FTSeek-US/app/playbook/adaptive_trailing.py (total 89 lines)

## lines 1-89
```python
#!/usr/bin/env python3
"""SIM-only adaptive trailing drawdown helper."""
import os


SIM_SOURCE_TYPE = "PLAYBOOK_SIM"


def _number(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed


def adaptive_trailing_drawdown_pct(
    peak_profit_pct,
    config=None,
    source_type=None,
):
    """Return the adaptive drawdown, or None when unavailable.

    The formula targets a percentage of gross MFE and clamps the drawdown:
        drawdown = clamp((1 - target_capture) * MFE, min, max)

    The source gate is deliberately hard-coded to PLAYBOOK_SIM. Live must keep
    the factor-specific static drawdown until a separate explicit decision.
    """
    config = config or {}
    if not bool(config.get("adaptive_trailing_enabled", False)):
        return None
    if str(source_type or "").strip().upper() != SIM_SOURCE_TYPE:
        return None

    target_capture_pct = _number(
        config.get("adaptive_trailing_target_capture_pct"), 60.0
    )
    min_drawdown_pct = _number(
        config.get("adaptive_trailing_min_drawdown_pct"), 0.4
    )
    max_drawdown_pct = _number(
        config.get("adaptive_trailing_max_drawdown_pct"), 1.2
    )
    # 2026-09-18 第三方复查 F3 (用户令「需要」放开): clamp 的 max 在 MFE ≥ min/max 比例处即饱和,
    #   使"高 MFE 按 target_capture 给空间"在设计上失效 —— MFE 3% 与 16% 共用 1.2% 回撤
    #   (捕获率 60% vs 88%, 即高 MFE 反而被锁得更紧)。
    #   放开做成 **env**, 不改冻结窗内的 config/exit 版本哈希 (KR2 冻结期换版须先落档 + 用户裁):
    #     EXT_ADAPTIVE_TRAILING_MAX_DD=2.5 ⇒ MFE ≤6.25% 走 40% 捕获公式, 之上以 2.5% **绝对上限**封顶
    #     (MFE 10% → 捕获 75%; MFE 16% → 84%): 给空间但**有界**, 不无限让出浮盈。
    #   设极大值(如 99) = 纯 40% 捕获(无上限); 未设 = 沿用 config 的 1.2%。
    _cap_override = os.getenv("EXT_ADAPTIVE_TRAILING_MAX_DD")
    if _cap_override not in (None, ""):
        try:
            _cand = float(_cap_override)
            if _cand >= min_drawdown_pct:
                max_drawdown_pct = _cand
        except (TypeError, ValueError):
            pass
    if not (0 < target_capture_pct < 100):
        return None
    if min_drawdown_pct <= 0 or max_drawdown_pct < min_drawdown_pct:
        return None

    mfe_pct = max(0.0, _number(peak_profit_pct))
    drawdown_pct = (100.0 - target_capture_pct) / 100.0 * mfe_pct
    return round(
        min(max(drawdown_pct, min_drawdown_pct), max_drawdown_pct), 6
    )


def adaptive_trailing_snapshot(
    peak_profit_pct,
    config=None,
    source_type=None,
):
    drawdown_pct = adaptive_trailing_drawdown_pct(
        peak_profit_pct, config, source_type
    )
    if drawdown_pct is None:
        return None
    config = config or {}
    return {
        "mfe_pct": round(max(0.0, _number(peak_profit_pct)), 6),
        "drawdown_pct": drawdown_pct,
        "target_capture_pct": round(
            _number(config.get("adaptive_trailing_target_capture_pct"), 60.0), 6
        ) if config else 60.0,
    }
```

# /opt/ftseek/FTSeek-US/app/pipeline/signal_pipeline.py (total 1459 lines)

## lines 1-80
```python
"""
信号管线 — FTSeek-US 核心决策链路

从 scheduler_tasks.py 拆分而来。本模块包含完整的信号生成到决策管线，
回测适配层和实盘调度器共用此代码。

管线流程:
    scan_loop()                  # 主循环: 市场门禁 + 熔断 + 巡检
      └─ _run_signal_scan_impl() # 自适应轮询批次
           └─ _process_symbol_impl()  # 单标的完整决策链路
                ├─ 行情获取 + 动态成本注入
                ├─ 财报感知 + VIX Regime 注入
                ├─ 作战计划快车道 (跳过投票)
                ├─ 信号生成 (盘中技术面 / 盘后LLM投票)
                ├─ 大盘联动过滤 + 金字塔加仓检测
                ├─ 共识检查 + SignalScore 构建
                └─ 风控检查 → 下单委托
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from app.utils.timezone import now_et


def _get_vwap_deviation(scheduler, symbol: str, cached_context: dict = None) -> Optional[float]:
    """M9 (2026-06-03): 从已获取的 context 读取 VWAP 偏离度, 避免重复 get_full_market_context。
    返回 None 表示数据不可用 (pre_trade_check 将软放行)。
    """
    try:
        # 优先使用已缓存的 context (H1 去重)
        if cached_context and "ticker_vwap_deviation" in cached_context:
            return float(cached_context["ticker_vwap_deviation"])
        if scheduler and hasattr(scheduler, 'market_data') and scheduler.market_data:
            ctx = scheduler.market_data.get_full_market_context(symbol)
            if ctx and "ticker_vwap_deviation" in ctx:
                return float(ctx["ticker_vwap_deviation"])
    except Exception:
        pass
    return None

from app.core.models import (
    SignalScore, Direction, MarketRegime,
)
from app.storage.rejection_logger import get_rejection_logger
from app.coordinator.manual_hold_registry import manual_hold_registry

logger = logging.getLogger(__name__)

# ── Rapid Regime Change 历史缓冲区 (P0 2026-06-17) ──
# 保留最近 5 分钟的 VIX/SPY 采样, 供 check_rapid_regime_change 检测急变
_VIX_HISTORY: list = []   # [(timestamp, vix_value), ...]
_SPY_HISTORY: list = []   # [(timestamp, spy_value), ...]
_MARKET_HISTORY_LOCK = threading.Lock()
_HISTORY_MAX_SECONDS: float = 360.0  # 保留最近 6 分钟 (略大于 5 分钟窗口)
_HISTORY_MAX_ENTRIES: int = 30       # 最多 30 个采样点


# ============================================================
# 决策引擎辅助 (Task #19)
# ============================================================
def _check_execution_rules(rules: dict, symbol: str, context: dict) -> bool:
    """检查 execution_rules 是否允许执行。

    返回 True=允许, False=阻止。规则缺失或必要价格非法时 fail-close。
    """
    if not isinstance(rules, dict) or not rules:
        logger.warning(f"[ExecRules] {symbol} execution_rules 缺失或格式非法, 拒绝执行")
        return False

    try:
        prev_close = float(context.get("prev_close", 0) or 0)
        open_price = float(context.get("open_price", 0) or 0)
        current_price = float(context.get("current_price", context.get("price", 0)) or 0)
    except (TypeError, ValueError):
```

# /opt/ftseek/FTSeek-US/app/coordinator/capital_allocator.py (total 701 lines)

## lines 1-80
```python
"""
CapitalAllocator — 实时资金账本 (Phase A 2026-05-14)

强制每个 strategy 的 capital_pct 上限. 取代当前"软约束"。

观察模式 (observe_only=True): 记录所有 reserve/release (含会被阻断的影子敞口), 但永远 return True
强制模式 (observe_only=False): 超限直接拒绝
"""
from __future__ import annotations
import logging
import math
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CapitalSnapshot:
    """供 dashboard / daily_review 读"""
    timestamp: float
    by_strategy: Dict[str, float]
    total_reserved: float
    max_total: float
    headroom: float  # max_total - total_reserved
    # Task #35 (2026-06-16): auto 资本隔离 — 关键诊断字段
    legacy_protected_pct: float = 0.0   # 历史持仓占总权益比例
    manual_pct: float = 0.0             # 手动持仓占总权益比例
    auto_pool_pct: float = 0.0          # auto 可用资金池占比 (= max_total - legacy - manual, 截 [0,1])
    auto_exposure_pct: float = 0.0      # auto 当前敞口占总权益比例
    auto_headroom_pct: float = 0.0      # auto 余量占 auto_pool 比例 (≥0; auto_pool=0 时为 0)


@dataclass
class AutoCapacityReport:
    """Task #35 (2026-06-16): auto 资本容量报告 — 用于 dashboard / daily_review / 单测验证.

    所有金额字段单位均为 USD; pct 字段为比例 (0~1).
    auto_pool = total_equity - legacy_protected_value - manual_value
    auto_headroom = auto_pool - current_auto_exposure  (不会为负)
    auto_headroom_pct = auto_headroom / auto_pool      (auto_pool=0 时为 0)
    """
    timestamp: float
    total_equity: float
    legacy_protected_value: float
    manual_value: float
    auto_pool: float
    current_auto_exposure: float
    auto_headroom: float
    auto_headroom_pct: float
    legacy_count: int = 0
    manual_count: int = 0
    auto_count: int = 0
    manual_orphan_count: int = 0
    unknown_count: int = 0
    unknown_value: Optional[float] = None
    degraded: bool = False


@dataclass
class _ReserveEvent:
    timestamp: float
    strategy: str
    delta: float           # 正数=reserve, 负数=release
    decision: str          # ALLOWED / BLOCKED_STRATEGY_CAP / BLOCKED_TOTAL_CAP
    snapshot_after: float  # strategy 当前总占用


class CapitalAllocator:
    """资金账本 + 上限强制 (线程安全)"""

    def __init__(
        self,
        caps: Dict[str, float],
        max_total_pct: float = 0.90,
```

## lines 158-190 (matched: def try_reserve)
```python
            self._observe_only = observe
        logger.info(f"[CapitalAllocator] observe_only={observe}")

    def try_reserve(self, strategy: str, position_pct: float) -> bool:
        """尝试预留资金额度。

        ⚠️ 当前限制:
        - observe_only=True 模式下永远返回 True (仅记录不阻断)
        已接入 (2026-10-12): 主订单路径在下单点强制预留 — order_executor
        `_reserve_capital_for_order` 在 _execute_long/_execute_stock_short 下单前
        以真实 quantity×price/equity 计算 position_pct 调用本方法;
        释放经平仓路径 on_position_closed → release(position.position_pct)。
        Phase B 剩余项: observe_only 切为 False。

        资金记账走 bucket (B.3 2026-05-14): 多策略共享 bucket 时, 占用计入同一桶.
        例 strategy=fast_lane, bucket_map={"fast_lane":"main"} → 实际记到 main 桶, 受 main 的 cap 限制.

        Returns:
            True: 预留成功 (observe 模式总是 True, 且记录实际放行敞口)
            False: 强制模式下被拒
        """
        try:
            position_pct = self._coerce_position_pct(position_pct)
        except ValueError as error:
            logger.error(f"[CapitalAllocator] invalid reserve rejected: {error}", exc_info=True)
            return False

        with self._lock:
            bucket = self._bucket_for(strategy)
            current = self._allocations.get(bucket, 0.0)
            cap = self._caps.get(bucket, 0.0)
            # Task #35: total 同时包含 legacy_protected (与 manual 一样占用 max_total 总配额)
            total = (sum(self._allocations.values())
```

## lines 227-259 (matched: def release)
```python
            )
            return False

    def release(self, strategy: str, position_pct: float):
        """释放 strategy 的预留资金 (平仓时调用) — 走 bucket"""
        position_pct = self._coerce_position_pct(position_pct)
        with self._lock:
            bucket = self._bucket_for(strategy)
            current = self._allocations.get(bucket, 0.0)
            new_val = max(0.0, current - position_pct)
            self._allocations[bucket] = new_val
            self._by_strategy[strategy] = max(0.0, self._by_strategy.get(strategy, 0.0) - position_pct)
            self._record_event(strategy, -position_pct, "RELEASED", new_val)
            self._persist_state(bucket, strategy)  # P1-7

    def force_set(self, strategy: str, value: float):
        """从持仓快照同步 (启动恢复 / 定期 reconcile 用) — 走 bucket"""
        with self._lock:
            bucket = self._bucket_for(strategy)
            self._allocations[bucket] = max(0.0, value)
            self._record_event(strategy, 0.0, "FORCE_SET", value)
            self._persist_state(bucket, strategy)  # P1-7

    def snapshot(self) -> CapitalSnapshot:
        """当前账本快照. by_strategy 字段是 bucket 视图 (向后兼容现有调用方).
        Phase 1 (2026-06-14): total_reserved 包含 manual_reserved, headroom 扣除手动占用.
        Task #35 (2026-06-16): 同时暴露 legacy_protected / auto_pool / auto_headroom 字段."""
        with self._lock:
            by_s = dict(self._allocations)
            auto_total = sum(by_s.values())
            legacy = self._legacy_protected_reserved
            manual = self._manual_reserved
            total = auto_total + manual + legacy
```

## lines 283-315 (matched: def compute_scale_factor)
```python
        """B.6 (2026-05-14): 公开访问 — 替代私有 _observe_only"""
        return self._observe_only

    def compute_scale_factor(self, strategy: str, position_pct: float) -> float:
        """Task #1 (2026-05-31): 计算按比例缩放系数

        当调用方希望 "总敞口超限 → 按比例缩放" 而非硬拒时使用.
        返回 [0.0, 1.0] 区间系数: 1.0 = 无需缩放; 0.0 = 完全拒绝.
        同时考虑 bucket cap 和 总敞口 cap, 取较小者.
        调用方可: 将 position_pct 乘以 该系数 后再 try_reserve.
        """
        try:
            position_pct = self._coerce_position_pct(position_pct)
        except ValueError as error:
            logger.error(f"[CapitalAllocator] invalid scale request rejected: {error}", exc_info=True)
            return 0.0

        with self._lock:
            bucket = self._bucket_for(strategy)
            current = self._allocations.get(bucket, 0.0)
            cap = self._caps.get(bucket, 0.0)
            # Task #35: total 同时包含 legacy_protected
            total = (sum(self._allocations.values())
                     + self._manual_reserved
                     + self._legacy_protected_reserved)

            # bucket 剩余
            bucket_headroom = 0.0 if bucket not in self._caps or cap <= 0 else max(0.0, cap - current)
            # 总敞口剩余 (扣除手动仓位占用)
            total_headroom = max(0.0, self._max_total - total)
            allowed = min(bucket_headroom, total_headroom, position_pct)
            scale = 0.0 if position_pct == 0 else max(0.0, min(1.0, allowed / position_pct))
            return scale
```

# /opt/ftseek/FTSeek-US/app/playbook/market_regime.py (total 399 lines)

## lines 1-60
```python
"""
Market Regime Gate - 市场环境门禁

基于 SPY 趋势 + VIX 波动率判断市场环境，决定今天的操作基调。

Regime 判定：
- RISK_ON: 市场趋势向上，VIX 低，可以正常操作
- CAUTIOUS: 市场不确定，需要缩小仓位
- RISK_OFF: 市场风险偏高，防守为主
- NO_TRADE: 极端环境，全部 SKIP

设计原则：
- 简单的规则判断，不是 LLM 预测
- 使用现有 MarketDataRouter 和 AlphaVantageProvider
- 每个 regime 影响后续 Screener 的 top_n 和仓位倍率
"""
import logging
from typing import Dict, Any, Optional
from enum import Enum
from dataclasses import dataclass

from app.data.market_data_router import MarketDataRouter, get_market_data_router
from app.data.alphavantage_provider import AlphaVantageProvider

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    """市场环境枚举"""
    RISK_ON = "risk_on"
    CAUTIOUS = "cautious"
    RISK_OFF = "risk_off"
    NO_TRADE = "no_trade"


@dataclass
class RegimeContext:
    """市场环境上下文"""
    regime: MarketRegime
    vix: float
    spy_price: float
    spy_ma20: float
    spy_ma50: float
    spy_ma200: float
    spy_change_pct: float
    score: int  # -10 到 +10 的评分
    
    # 影响后续流程的参数
    top_n: int  # Screener 筛选数量
    position_multiplier: float  # 仓位倍率
    min_strength: int  # 最低信号强度要求
    vix_uncertain: bool = False  # RFC-060: VIX 取不到真实值时为 True, regime 判定不可信
    # [DEB-20260924 F5-B'影子] 盘前 spy_price 口径遥测 (record-only, 不影响主判定):
    #   spy_price 现为日K口径 (盘前=昨收, regime_log 4/4 逐日实证 + 9/21 距MA20 0.11% 翻转实例)。
    #   影子字段双算"若用盘前 pre_price 会怎样", 攒 5-10 个交易日后裁决是否 enforce。
    spy_price_basis: str = "kline_last_close"   # kline_last_close | pre_price(仅遥测标注, 主判定不变)
    spy_pre_price: float = 0.0                  # 盘前时段快照 pre_price (0=不可用)
    score_shadow: Optional[int] = None          # 用 pre_price 重算的 score (None=无双算/无差异)
    regime_shadow: Optional[str] = None         # score_shadow 对应的 regime 值

```

## lines 117-149 (matched: class )
```python
    return r


class MarketRegimeGate:
    """市场环境门禁"""

    # 2026-07-31 全链清理: 类级 TTL 缓存 — 调用方 (auto_execute/sizing 兜底) 每次新建实例
    #   且逐条指令各 assess 一次, 无缓存时每个周期打 AlphaVantage 付费 API 多次.
    #   regime 基于 MA20/50/200+VIX, 分钟级新鲜度足够; uncertain 降级结果短 TTL 防兜底滞留.
    _assess_cache = None          # tuple(monotonic_ts, RegimeContext)
    _ASSESS_CACHE_TTL = 120.0     # 正常结果缓存 120s
    _ASSESS_CACHE_TTL_UNCERTAIN = 30.0  # VIX uncertain 降级结果仅 30s

    def __init__(
        self,
        market_data_router: Optional[MarketDataRouter] = None,
        alphavantage_provider: Optional[AlphaVantageProvider] = None,
    ):
        self.router = market_data_router
        self.av = alphavantage_provider
        
        # 如果没有传入，尝试创建默认实例
        if not self.router:
            try:
                # 2026-07-11: 复用进程级共享单例, 避免每次实例化都新建 Futu 连接+watchdog
                self.router = get_market_data_router()
            except Exception as e:
                logger.warning(f"Failed to create MarketDataRouter: {e}")
        
        if not self.av:
            try:
                self.av = AlphaVantageProvider()
            except Exception as e:
```

# /opt/ftseek/FTSeek-US/app/risk/drawdown_control.py (total 223 lines)

## lines 1-60
```python
"""回撤控制器 (日内日亏限额)

P0 修复 (2026-04-17): 支持动态资产基准
P0-fix (2026-06-15): 日内回撤状态持久化到 SQLite, 防止重启归零绕过 3% 日限

历史注记 (DEB-20260823-system-health-check, 用户裁"不值得, 删除"):
- 原 8% 总回撤门 (max_total_drawdown / update_assets / TOTAL_DRAWDOWN 分支) 已删除:
  update_assets() 全仓零调用, 该门自诞生从未被评估过 = 死门; 总回撤防护由
  capital_safety 水位体系承担 (-20% RED 清仓 / -35% BLACKOUT 锁死, 已接线实测).
- 原 set_baseline/reset_daily_baseline/update_drawdown/get_status/
  get_baseline_history/DrawdownBaselineRecord 同批删除 (全仓零调用).
- 已知残留缺口 (未修, 见 DEB P1-M3): playbook 平仓 pnl 未喂入本控制器,
  日回撤池当前仅主线平仓在喂 (order_executor C4 点).
"""
# ── 日亏损防线登记 (2026-08-28 冲突显性化): "当日亏损"事实六套权威并存 ──
#   1. AccountCircuitBreaker  app/risk/account_circuit_breaker.py
#      净值 -1%/-2%/-3% 三档 (减半/停新仓/停新仓 — 全平从未接线)
#   2. AbsoluteCircuitBreaker app/risk/absolute_circuit_breaker.py
#      -$2000/日 + -$1000/时 + 连亏5笔 (类常量, 不受开关控制)
#   3. symbol_day_loss        app/risk/symbol_day_loss.py
#      -$500/标的/日 (出场日+净额, 主线/playbook 共用)
#   4. SymbolDailyLossGuard   app/risk/symbol_daily_loss_guard.py
#      净值0.5%/标的/日 + 连亏N笔 + 低胜率 (yaml risk.symbol_daily_loss_guard.*)
#   5. RiskCircuitBreaker     app/playbook/risk_circuit_breaker.py
#      总资金1% + 连亏3减半/连亏5停1日 (playbook 域; 周报告警组件 — "禁执行按钮+飞书"
#      从未接线, 全仓唯一调用方=周五周报 logger.warning, 仅记录不阻断; 2026-08-30 DEB-timezone 第4轮)
#   6. DrawdownController     app/risk/drawdown_control.py
#      日3% (risk.account.max_daily_drawdown, 持久化防重启归零; 仅主线平仓在喂)
# 六套独立查询/独立状态互不感知; 口径 % 与 $ 混杂, 触发先后交错, 合并/裁剪属
# 人工风险裁决 (每套有各自历史语义与恢复机制)。本登记保证读任一防线时看得见
# 全景; 已统一项: 1/3 出场日+净额 (2026-08-27/28), 其余各口径见各自头注。
from datetime import datetime
from typing import Optional
import logging
import os
import sqlite3

from app.utils.timezone import _et_trade_date, now_et

logger = logging.getLogger(__name__)

# DB 路径: data/drawdown_state.db
_DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'drawdown_state.db')


class DrawdownController:
    """日内回撤控制器 (日亏限额)

    计算逻辑：
        current_daily_dd_abs 累加每笔已实现亏损 (record_trade_pnl)
        current_daily_dd = current_daily_dd_abs / daily_start_assets
    """

    def __init__(
        self,
        max_daily_drawdown: float = 0.03,
        initial_baseline: float = 0.0,
    ):
        self.max_daily = max_daily_drawdown

```

## lines 78-110 (matched: def )
```python
    # SQLite 持久化 (P0-fix 2026-06-15)
    # ------------------------------------------------------------------

    def _ensure_table(self):
        try:
            os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
            conn = sqlite3.connect(_DB_PATH, timeout=5)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS drawdown_state (
                    trade_date TEXT PRIMARY KEY,
                    daily_start_assets REAL,
                    current_daily_dd_abs REAL,
                    block_reason TEXT,
                    baseline REAL,
                    current_assets REAL,
                    updated_at TEXT
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"[回撤持久化] 建表失败: {e}")

    def _load_state(self):
        """从 DB 恢复今日状态 (防止重启归零)"""
        try:
            conn = sqlite3.connect(_DB_PATH, timeout=5)
            today = _et_trade_date(now_et())
            row = conn.execute(
                "SELECT daily_start_assets, current_daily_dd_abs, block_reason, "
                "baseline, current_assets FROM drawdown_state WHERE trade_date=?",
                (today,),
            ).fetchone()
```

## lines 136-168 (matched: def )
```python
        except Exception as e:
            logger.debug(f"[回撤持久化] 加载失败 (使用默认值): {e}")

    def _save_state(self):
        """持久化当前状态到 DB"""
        try:
            conn = sqlite3.connect(_DB_PATH, timeout=5)
            today = _et_trade_date(now_et())
            conn.execute(
                "INSERT OR REPLACE INTO drawdown_state "
                "(trade_date, daily_start_assets, current_daily_dd_abs, block_reason, "
                "baseline, current_assets, updated_at) VALUES (?,?,?,?,?,?,?)",
                (today, self.daily_start_assets, self.current_daily_dd_abs,
                 self.block_reason or "", self.baseline, self.current_assets,
                 datetime.now().isoformat()),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"[回撤持久化] 保存失败: {e}")

    # ------------------------------------------------------------------
    # 日内回撤记账
    # ------------------------------------------------------------------

    def record_trade_pnl(self, pnl: float, source: str = "unknown"):
        """C4 fix (2026-05-31): 记录单笔交易 P&L, 累加到日内回撤

        供主线在平仓时调用, 确保所有策略的亏损都计入共享日回撤池。

        Args:
            pnl: 交易净盈亏 (正=盈利, 负=亏损), 单位: 美元
            source: 来源标识 ("main" / etc.)
```

## lines 193-223 (matched: def )
```python
    # 限制检查
    # ------------------------------------------------------------------

    def _check_limits(self):
        """检查日内回撤限制 (动态应用模式感知限额)"""
        self.block_reason = None

        effective_daily = self._get_effective_max_daily()

        if self.current_daily_dd > effective_daily:
            self.block_reason = "DAILY_DRAWDOWN"

    def _get_effective_max_daily(self) -> float:
        """获取模式感知的日亏损限额 (热加载)"""
        try:
            from app.core.trading_mode import TradingMode
            return TradingMode().get_max_daily_loss_pct()
        except Exception:
            return self.max_daily

    def is_daily_limit_breached(self) -> bool:
        """检查日内回撤是否超限"""
        return self.block_reason == "DAILY_DRAWDOWN"

    def is_trading_allowed(self) -> bool:
        """检查是否允许交易"""
        return self.block_reason is None

    def get_block_reason(self) -> Optional[str]:
        """获取阻止原因"""
        return self.block_reason
```
