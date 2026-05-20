#!/usr/bin/env python3
"""
配置默认值唯一数据源 (2026-04-24 新增)

规则:
1. 所有 config.get(key, default) 中的 default 必须来自此文件
2. 修改任何默认值: 只改 unified_config.yaml + 此文件
3. 禁止在代码中硬编码配置数值
4. config_audit.py 会自动验证此文件与 unified_config.yaml 的一致性

用法:
    from app.core.config_defaults import CFG
    
    self.min_confidence = self.config.get('min_confidence', CFG.MIN_CONFIDENCE)
    self.max_pending = self.config.get('max_pending', CFG.DECISION_BUFFER_MAX_PENDING)
"""


class ConfigDefaults:
    """
    所有配置默认值的唯一代码数据源。
    与 unified_config.yaml 中的值必须保持一致。
    """
    
    # ============================================================
    # 风控 - 置信度
    # ============================================================
    MIN_CONFIDENCE = 0.45                    # risk_limits.min_confidence  # 2026-05-19: 提高信号门槛 (原0.35)
    
    # ============================================================
    # 决策缓冲层
    # ============================================================
    DECISION_BUFFER_SIGNAL_COOLDOWN = 120     # strategy.decision_buffer.signal_cooldown
    DECISION_BUFFER_MAX_PENDING = 500         # strategy.decision_buffer.max_pending
    DECISION_BUFFER_MIN_CONFIDENCE = 0.45     # strategy.decision_buffer.min_confidence  # 2026-05-19: 提高信号门槛 (原0.35)
    DECISION_BUFFER_MAX_POSITIONS = None      # strategy.decision_buffer.max_positions (null=动态)
    
    # ============================================================
    # 置信度调整器
    # ============================================================
    CONFIDENCE_ADJUSTER_MIN_CONFIDENCE = 0.45  # strategy.confidence_adjuster.min_confidence  # 2026-05-19: 提高信号门槛 (原0.35)
    CONFIDENCE_ADJUSTER_MAX_SINGLE_PENALTY = 0.15
    CONFIDENCE_ADJUSTER_MAX_TOTAL_PENALTY = 0.40
    CONFIDENCE_ADJUSTER_MAX_SINGLE_BOOST = 0.10
    CONFIDENCE_ADJUSTER_MAX_TOTAL_BOOST = 0.20
    
    # ============================================================
    # 风控 - 仓位 & 亏损限制
    # ============================================================
    MAX_SINGLE_POSITION = 0.08             # risk_limits.max_single_position
    MAX_TOTAL_POSITION = 0.75              # risk_limits.max_total_position
    MAX_DAILY_TRADES = 5                   # risk_limits.max_daily_trades
    MAX_DAILY_LOSS = 0.03                  # risk_limits.max_daily_loss
    MAX_WEEKLY_LOSS = 0.08                 # risk_limits.max_weekly_loss
    MAX_MONTHLY_LOSS = 0.15                # risk_limits.max_monthly_loss
    MAX_DRAWDOWN = 0.12                    # risk_limits.max_drawdown
    CONSECUTIVE_LOSS_LIMIT = 3             # risk_limits.consecutive_loss_limit
    STRATEGY_MIN_WINRATE = 0.45            # risk_limits.strategy_min_winrate
    
    # ============================================================
    # 止损 / 止盈
    # ============================================================
    DEFAULT_STOP_LOSS_PCT = 0.03           # stop_loss.default_percent (对齐 unified_config.yaml)
    DEFAULT_TAKE_PROFIT_PCT = 0.10         # take_profit.default_percent (对齐 unified_config.yaml)
    TRAILING_STOP_PCT = 0.02               # stop_loss.trailing_percent
    TRAILING_TP_PCT = 0.02                 # take_profit.trailing_percent
    MAX_HOLDING_HOURS = 48                 # stop_loss.max_holding_hours
    ATR_MULTIPLIER = 2.5                   # stop_loss.atr_multiplier
    BREAKEVEN_TRIGGER = 0.03               # take_profit.breakeven.trigger_profit
    BREAKEVEN_LEVEL = 0.01                 # take_profit.breakeven.stop_level
    
    # ============================================================
    # 共识引擎
    # ============================================================
    CONSENSUS_WINDOW = 90                  # strategy.consensus.window_seconds (对齐 unified_config.yaml)
    CONSENSUS_THRESHOLD = 1.5              # strategy.consensus.threshold  # 2026-05-19: 提高共识阈值 (原1.0)
    CONSENSUS_ADX_TREND = 25               # strategy.consensus.adx_trend
    CONSENSUS_ADX_RANGE = 20               # strategy.consensus.adx_range
    
    # ============================================================
    # 信号冷却
    # ============================================================
    SIGNAL_COOLDOWN = 120                  # strategy.signal_cooldown.signal_cooldown
    
    # ============================================================
    # 趋势过滤
    # ============================================================
    TREND_FILTER_MIN_ADX = 20              # trend_filter.min_adx_for_trend
    TREND_FILTER_USE_MULTI_TIMEFRAME = True
    TREND_FILTER_PREVENT_BUY_IN_DOWNTREND = True
    TREND_FILTER_PREVENT_SHORT_IN_UPTREND = True
    
    # ============================================================
    # 入场价格检查
    # ============================================================
    ENTRY_PRICE_HIGH_BUY_THRESHOLD = 0.005  # 0.5%
    ENTRY_PRICE_CHASE_WARNING_THRESHOLD = 0.03  # 3%
    ENTRY_PRICE_CHASE_BLOCK_THRESHOLD = 0.05  # 5%
    
    # ============================================================
    # LLM 交易
    # ============================================================
    LLM_TRADING_MIN_CONFIDENCE = 0.60
    LLM_TRADING_MANUAL_CONFIRM_MIN_CONFIDENCE = 0.75
    LLM_TRADING_AUTO_EXECUTE_MIN_CONFIDENCE = 0.60
    
    # ============================================================
    # 动态阈值
    # ============================================================
    DYNAMIC_THRESHOLD_CURRENT = 0.55
    
    # ============================================================
    # 熔断器
    # ============================================================
    CIRCUIT_BREAKER_REJECT_LIMIT = 8
    CIRCUIT_BREAKER_PAUSE_DURATION = 300    # 秒
    CIRCUIT_BREAKER_DRAWDOWN_LIMIT = 0.15
    
    # ============================================================
    # 硬编码熔断守卫 (HardStopGuard)
    # ============================================================
    HARD_STOP_DRAWDOWN_LIMIT = 0.03         # 日内回撤 3%
    HARD_STOP_CONSECUTIVE_LOSSES = 3        # 连续亏损次数


# 便捷访问
CFG = ConfigDefaults()
