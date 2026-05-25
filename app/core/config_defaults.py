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
    CONSENSUS_THRESHOLD = 1.0              # strategy.consensus.threshold  # 2026-05-22: 恢复原值 (05-19提至1.5导致零触发)
    CONSENSUS_ADX_TREND = 25               # strategy.consensus.adx_trend
    CONSENSUS_ADX_RANGE = 20               # strategy.consensus.adx_range
    # 市场状态增益/衰减系数 (P1 SSOT 2026-05-22)
    CONSENSUS_TREND_BOOST = 1.5            # strategy.consensus.trend_boost
    CONSENSUS_TREND_DECAY = 0.6            # strategy.consensus.trend_decay
    CONSENSUS_RANGE_BOOST = 1.5            # strategy.consensus.range_boost
    CONSENSUS_RANGE_DECAY = 0.6            # strategy.consensus.range_decay
    CONSENSUS_MTF_TREND_BOOST = 1.3        # strategy.consensus.mtf_trend_boost
    CONSENSUS_MTF_RANGE_DECAY = 0.7        # strategy.consensus.mtf_range_decay
    # 分歧度与防守
    CONSENSUS_DIVERGENCE_THRESHOLD = 0.7   # strategy.consensus.divergence_threshold  # 2026-05-22: 放宽 (原0.5)
    CONSENSUS_DIVERGENCE_SCORE_DECAY = 0.3 # strategy.consensus.divergence_score_decay  # 2026-05-22: 减轻 (原0.5)
    CONSENSUS_DIVERGENCE_GRAY_ZONE = True  # strategy.consensus.divergence_trigger_gray
    CONSENSUS_GRAY_ZONE_PENALTY = -0.3     # strategy.consensus.gray_zone_penalty  # 2026-05-22: 对齐 YAML (代码原值-0.10)
    CONSENSUS_CHAOS_THRESHOLD = 0.7        # strategy.consensus.chaos_threshold
    # 场景失效检测
    CONSENSUS_SCENARIO_FAIL_LIMIT = 3      # strategy.consensus.scenario_consecutive_fail_limit
    CONSENSUS_SCENARIO_FAIL_PENALTY = -0.1 # strategy.consensus.scenario_fail_penalty
    # 突发新闻
    CONSENSUS_BREAKING_NEWS_MULTIPLIER = 2.0  # strategy.consensus.breaking_news_multiplier
    CONSENSUS_BREAKING_NEWS_LOOKBACK_HOURS = 2  # strategy.consensus.breaking_news_lookback_hours
    # 开关
    CONSENSUS_HYBRID_WEIGHT_ENABLED = True # strategy.consensus.hybrid_weight_enabled
    
    # ============================================================
    # 信号冷却
    # ============================================================
    SIGNAL_COOLDOWN = 120                  # strategy.signal_cooldown.signal_cooldown
    
    # ============================================================
    # 趋势过滤
    # ============================================================
    TREND_FILTER_MIN_ADX = 18              # trend_filter.adx_range_gate (对齐 unified_config.yaml)
    TREND_FILTER_USE_MULTI_TIMEFRAME = True
    TREND_FILTER_PREVENT_BUY_IN_DOWNTREND = True
    TREND_FILTER_PREVENT_SHORT_IN_UPTREND = True
    
    # ============================================================
    # 入场价格检查
    # ============================================================
    ENTRY_PRICE_HIGH_BUY_THRESHOLD = 0.01  # entry_price_checker.high_buy_warn (对齐 unified_config.yaml)
    ENTRY_PRICE_CHASE_WARNING_THRESHOLD = 0.03  # 3%
    ENTRY_PRICE_CHASE_BLOCK_THRESHOLD = 0.03  # entry_price_checker.panic_threshold (对齐 unified_config.yaml)
    
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
