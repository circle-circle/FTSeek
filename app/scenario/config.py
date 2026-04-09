"""
场景路由配置 (Scenario Routing Configuration)

定义场景的策略映射表

核心场景 (Phase 1):
1. TREND_STRONG - 强趋势
2. RANGE_BOUND - 震荡市
3. GAP_EVENT - 跳空事件
4. VOL_EXPANSION - 波动扩张
5. REVERSAL_SETUP - 反转预备

扩展场景 (Phase 2):
6. NEWS_DRIVEN - 新闻驱动
7. TREND_START - 趋势启动
8. TREND_EXHAUST - 趋势衰竭
9. VOL_CONTRACTION - 波动收缩
10. BREAKOUT_SETUP - 突破预备
"""
from typing import Dict, List, Any

# ============================================================
#  Phase 1: 5 个核心场景的策略映射表
# ============================================================

CORE_SCENARIO_ROUTING: Dict[str, Dict[str, Any]] = {
    # --- 1. 强趋势场景 ---
    "TREND_STRONG": {
        "primary": ["confluence_v2"],  # supertrend 已移除 (2026-04-09)，其因子已包含在 confluence_v2 中
        "secondary": ["enhanced_confluence", "macd"],
        "suppressed": ["rsi", "bollinger"],
        "risk_template": "trend_following",
        "priority_boost": {"confluence_v2": 1.3},
        "description": "ADX > 30, 明确趋势方向",
    },

    # --- 2. 震荡市场景 ---
    "RANGE_BOUND": {
        "primary": ["rsi", "bollinger"],
        "secondary": ["confluence_v2", "open_close_effect"],
        "suppressed": ["turtle"],  # supertrend 已移除 (2026-04-09)
        "risk_template": "mean_reversion",
        "priority_boost": {"rsi": 1.5, "bollinger": 1.5},
        "description": "ADX < 20, 价格区间震荡",
    },

    # --- 3. 跳空事件场景 ---
    "GAP_EVENT": {
        "primary": ["event_driven", "gap_trading"],
        "secondary": ["sentiment_ai", "confluence_v2"],
        "suppressed": ["pure_technical_indicators"],
        "risk_template": "event_driven",
        "priority_boost": {"event_driven": 2.0, "gap_trading": 2.0},
        "description": "开盘跳空 > 2%",
    },

    # --- 4. 波动扩张场景 ---
    "VOL_EXPANSION": {
        "primary": ["volatility_breakout", "confluence_v2"],
        "secondary": ["event_driven", "123_rule"],
        "suppressed": ["mean_reversion_strategies"],
        "risk_template": "breakout",
        "priority_boost": {"volatility_breakout": 1.5},
        "description": "ATR 百分位快速上升 > 15%",
    },

    # --- 5. 反转预备场景 ---
    "REVERSAL_SETUP": {
        "primary": ["123_rule", "rsi_divergence"],
        "secondary": ["confluence_v2", "sentiment_ai"],
        "suppressed": ["trend_following_strategies"],
        "risk_template": "reversal",
        "priority_boost": {"123_rule": 1.5},
        "description": "超买/超卖 + 动量衰竭",
    },

    # --- 6. 新闻驱动场景 (Phase 2 新增) ---
    "NEWS_DRIVEN": {
        "primary": ["sentiment_ai", "event_driven"],
        "secondary": ["confluence_v2", "gap_trading"],
        "suppressed": ["pure_technical_indicators", "turtle"],
        "risk_template": "news_following",
        "priority_boost": {"sentiment_ai": 2.0, "event_driven": 1.5},
        "description": "新闻情绪得分 > 阈值",
    },

    # --- 7. 趋势启动场景 (Phase 2 新增) ---
    "TREND_START": {
        "primary": ["turtle", "123_rule"],
        "secondary": ["confluence_v2", "macd"],
        "suppressed": ["rsi", "mean_reversion_strategies"],
        "risk_template": "trend_entry",
        "priority_boost": {"turtle": 1.5, "macd": 1.3},
        "description": "ADX 20-30, 趋势初期",
    },

    # --- 8. 趋势衰竭场景 (Phase 2 新增) ---
    "TREND_EXHAUST": {
        "primary": ["rsi_divergence", "123_rule"],
        "secondary": ["confluence_v2"],
        "suppressed": ["turtle"],  # supertrend 已移除 (2026-04-09)
        "risk_template": "trend_exit",
        "priority_boost": {"rsi_divergence": 1.5},
        "description": "ADX > 40, 动量背离",
    },

    # --- 9. 波动收缩场景 (Phase 2 新增) ---
    "VOL_CONTRACTION": {
        "primary": ["bollinger", "rsi"],
        "secondary": ["confluence_v2"],
        "suppressed": ["volatility_breakout"],
        "risk_template": "low_volatility",
        "priority_boost": {"bollinger": 1.3},
        "description": "ATR 百分位下降",
    },

    # --- 10. 突破预备场景 (Phase 2 新增) ---
    "BREAKOUT_SETUP": {
        "primary": ["volatility_breakout", "123_rule"],
        "secondary": ["confluence_v2"],  # supertrend 已移除 (2026-04-09)
        "suppressed": ["mean_reversion_strategies"],
        "risk_template": "breakout_prepare",
        "priority_boost": {"volatility_breakout": 1.5},
        "description": "布林带收窄 + 低波动",
    },
}

# ============================================================
#  默认权重配置
# ============================================================

DEFAULT_WEIGHT_CONFIG = {
    "primary_weight": 1.5,       # 主策略权重
    "secondary_weight": 1.0,     # 辅助策略权重
    "suppressed_weight": 0.3,    # 抑制策略权重
}

# ============================================================
#  场景参数配置
# ============================================================

SCENARIO_CLASSIFIER_CONFIG = {
    # 场景确认参数
    "confirm_window": 3,           # 场景确认 K 线数
    "cool_down_minutes": 2,        # 切换冷却时间 (分钟)
    "confidence_threshold": 0.65,  # 置信度阈值

    # 场景识别阈值
    "adx_start_min": 20.0,         # 趋势启动 ADX 下限
    "adx_start_max": 30.0,         # 趋势启动 ADX 上限
    "adx_strong_min": 30.0,        # 强趋势 ADX 下限
    "adx_exhaust_min": 40.0,       # 趋势衰竭 ADX 下限
    "gap_threshold": 0.02,         # 跳空阈值 (2%)
    "rsi_overbought": 70.0,        # 超买阈值
    "rsi_oversold": 30.0,          # 超卖阈值
    "bb_squeeze_threshold": 0.05,  # 布林带收窄阈值 (5%)
}

# ============================================================
#  导出函数
# ============================================================

def get_scenario_config(scenario_name: str) -> Dict[str, Any]:
    """
    获取指定场景的配置

    Args:
        scenario_name: 场景名称

    Returns:
        场景配置字典
    """
    return CORE_SCENARIO_ROUTING.get(
        scenario_name,
        {
            "primary": ["confluence_v2"],
            "secondary": ["confluence_v2", "rsi"],  # supertrend 已移除 (2026-04-09)
            "suppressed": [],
            "risk_template": "default",
            "priority_boost": {},
        }
    )


def get_all_scenarios() -> List[str]:
    """获取所有已配置的场景名称"""
    return list(CORE_SCENARIO_ROUTING.keys())


def is_scenario_configured(scenario_name: str) -> bool:
    """判断场景是否已配置"""
    return scenario_name in CORE_SCENARIO_ROUTING
