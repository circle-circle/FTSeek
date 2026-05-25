#!/usr/bin/env python3
"""
配置默认值 — 紧急回退层 (Emergency Fallback Layer)

⚠️ SSOT 层级:
  1. config/unified_config.yaml          ← 唯一真相源 (PRIMARY SSOT)
  2. 本文件 (config_defaults.py)          ← 仅当 YAML key 缺失时的回退值
  3. 业务代码                             ← 禁止硬编码配置数值

规则:
1. 新增配置项: 只加 YAML，不加本文件 (除非需要启动时防御)
2. 修改配置值: 只改 YAML，本文件保持同步 (config_audit.py 会校验)
3. 本文件中的值必须与 YAML 一致，标注 "最后同步: YYYY-MM-DD"
4. config_audit.py 启动时自动验证一致性

用法:
    from app.core.config_defaults import CFG

    self.min_confidence = cfg.get('risk.signal_quality.min_confidence', CFG.MIN_CONFIDENCE)
    self.max_daily = cfg.get('risk.trading.max_daily_trades', CFG.MAX_DAILY_TRADES)
"""


class ConfigDefaults:
    """所有业务配置默认值的唯一代码数据源。与 unified_config.yaml 必须保持一致。"""

    # ============================================================
    # 风控 — 信号质量 (risk.signal_quality)
    # P0 优化 (2026-05-12): 降低门槛增加交易机会，模拟盘阶段优先获取数据
    # ============================================================
    MIN_CONFIDENCE = 0.4                    # P0 优化: 0.6 → 0.4
    MIN_CONSENSUS = 0.2                     # P0 优化: 0.3 → 0.2
    MIN_SIGNAL_SCORE = 50                   # Synced to YAML risk.signal_quality.min_score (2026-05-23 06:20 CST)

    # ============================================================
    # 风控 — 交易限制 (risk.trading)
    # ============================================================
    MAX_DAILY_TRADES = 500                  # risk.trading.max_daily_trades
    MAX_ORDER_AMOUNT = 120000               # risk.trading.max_order_amount
    MIN_LIQUIDITY_RATIO = 0.5               # risk.trading.min_liquidity_ratio

    # ============================================================
    # 风控 — 仓位限制 (risk.position)
    # ============================================================
    MAX_PER_SYMBOL = 0.25                   # risk.position.max_per_symbol
    MAX_PER_DIRECTION = 0.65                # risk.position.max_per_direction
    MAX_TOTAL_POSITION = 0.8                # risk.position.max_total

    # ============================================================
    # 熔断器 (risk.circuit_breaker)
    # ============================================================
    CB_CONSECUTIVE_LOSSES = 3               # risk.circuit_breaker.strategy.consecutive_losses (synced to YAML 2026-05-21)
    CB_HOURLY_LOSS_THRESHOLD = -0.02        # risk.circuit_breaker.strategy.hourly_loss_threshold
    CB_PAUSE_DURATION = 1800                # risk.circuit_breaker.strategy.pause_duration (秒)

    # ============================================================
    # 调度器 (scheduler)
    # P0 优化 (2026-05-12): 降低门槛增加交易机会
    # ============================================================
    SCHEDULER_MIN_CONFIDENCE = 0.4          # P0 优化: 0.5 → 0.4
    SCHEDULER_MAX_POSITIONS = 5             # scheduler.max_positions
    SCHEDULER_MIN_SIGNAL_STRENGTH = 99      # scheduler.min_signal_strength (P8: raised from 50→99, strength≥99 has 32.1% WR vs 11.4% at 66)
    SCHEDULER_MIN_CONSENSUS = 0.2           # scheduler.min_consensus_level
    SCHEDULER_SCAN_INTERVAL = 10            # scheduler.scan_interval_minutes

    # ============================================================
    # 止损 / 止盈
    # ============================================================
    DEFAULT_STOP_LOSS_PCT = 0.05            # stop_loss.default_stop_loss_pct
    DEFAULT_TAKE_PROFIT_PCT = 0.1           # take_profit.default_take_profit_pct

    # ============================================================
    # 信号融合 (signal_fusion.us)
    # ============================================================
    FUSION_TECHNICAL_WEIGHT = 0.5           # signal_fusion.us.technical_weight
    FUSION_SENTIMENT_WEIGHT = 0.2           # signal_fusion.us.sentiment_weight
    FUSION_MACRO_WEIGHT = 0.3               # signal_fusion.us.macro_weight

    # ============================================================
    # 决策引擎 — 信号阈值 (decision.signal_thresholds / risk.signal_quality)
    # ============================================================
    DECISION_MIN_SCORE = 50                 # decision.signal_thresholds.min_score
    DECISION_MEDIUM_SCORE = 70              # decision.signal_thresholds.medium_score
    DECISION_HIGH_SCORE = 85                # decision.signal_thresholds.high_score

    # ============================================================
    # 决策引擎 — 仓位比例 (decision.position_ratios)
    # ============================================================
    POSITION_RATIO_LOW = 0.25
    POSITION_RATIO_MEDIUM = 0.50
    POSITION_RATIO_HIGH = 1.00

    # ============================================================
    # 决策引擎 — 信号时序 (decision.signal_timing)
    # ============================================================
    SIGNAL_COOLDOWN = 120                   # decision.signal_timing.cooldown_seconds
    SIGNAL_EXPIRE_SECONDS = 300             # decision.signal_timing.expire_seconds

    # ============================================================
    # 期权配置 (options)
    # ============================================================
    OPTIONS_STOP_LOSS_PCT = -0.3            # options.stop_loss_pct
    OPTIONS_TAKE_PROFIT_RATIO = 2.0         # options.take_profit_ratio
    OPTIONS_MAX_HOLDING_HOURS = 72          # options.max_holding_hours
    OPTIONS_MAX_COST_PER_CONTRACT = 2000    # options.max_cost_per_contract
    OPTIONS_MIN_DTE = 7                     # options.min_dte
    OPTIONS_MAX_DTE = 45                    # options.max_dte
    OPTIONS_FORCE_CLOSE_DTE = 2             # options.force_close_dte
    OPTIONS_PROFIT_TARGET_PCT = 0.5         # options.profit_target_pct

    # ============================================================
    # 执行配置 (execution)
    # ============================================================
    EXEC_MAX_SLIPPAGE = 0.005               # execution.slippage.max_ratio
    EXEC_MAX_RETRY = 3                      # execution.retry.max_attempts
    EXEC_RETRY_DELAY = 5                    # execution.retry.delay_seconds
    EXEC_ORDER_TIMEOUT = 300                # execution.order_tracking.timeout_seconds
    EXEC_IDEMPOTENCY_WINDOW = 3600          # execution.idempotency.window_seconds

    # ============================================================
    # 策略预算 (strategy_budgets)
    # ============================================================
    BUDGET_MAIN_MAX_POSITIONS = 5           # strategy_budgets.main_pipeline.max_concurrent_positions
    BUDGET_SCALPING_MAX_POSITIONS = 5       # strategy_budgets.scalping.max_concurrent_positions
    BUDGET_SYSTEM_MAX_DAILY_LOSS = 6000     # strategy_budgets.system.max_total_daily_loss


# 便捷访问
CFG = ConfigDefaults()
