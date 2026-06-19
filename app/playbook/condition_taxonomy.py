"""
Playbook Condition Taxonomy - 固定条件枚举表

LLM 生成的 condition tag 必须从以下固定枚举中选择，确保 Pattern 系统的一致性。
共 27 个条件标签，分为 7 个类别。
"""
from typing import Dict, List, Tuple


CONDITION_TAXONOMY: Dict[str, str] = {
    # ========== 趋势类 (6个) ==========
    "above_ma20": "价格在 MA20 上方",
    "above_ma50": "价格在 MA50 上方",
    "below_ma20": "价格在 MA20 下方",
    "below_ma50": "价格在 MA50 下方",
    "ma_golden_cross": "MA20 上穿 MA50",
    "ma_dead_cross": "MA20 下穿 MA50",
    
    # ========== 动量类 (8个) ==========
    "rsi_oversold": "RSI(14) < 40",
    "rsi_overbought": "RSI(14) > 70",
    "rsi_neutral_low": "RSI(14) 40-50",
    "macd_golden_cross": "MACD 金叉",
    "macd_dead_cross": "MACD 死叉",
    "bollinger_lower": "价格触及布林带下轨",
    "bollinger_upper": "价格触及布林带上轨",
    "bollinger_squeeze": "布林带收窄(突破前兆)",
    
    # ========== 价值类 (6个) ==========
    "pe_below_industry": "PE 低于行业平均",
    "pe_above_industry": "PE 高于行业平均",
    "revenue_growth_high": "收入增长 > 20% YoY",
    "roe_high": "ROE > 15%",
    "near_52w_low": "当前价接近 52 周低点(10% 以内)",
    "near_52w_high": "当前价接近 52 周高点(10% 以内)",
    
    # ========== 内部人 (3个) ==========
    "insider_cluster_buy": "多内部人集群买入(cluster_score > 0.5)",
    "insider_net_buy": "内部人净买入(非集群)",
    "insider_net_sell": "内部人净卖出",
    
    # ========== 情绪/新闻 (4个) ==========
    "news_positive": "近 3 天新闻情绪正面",
    "news_negative": "近 3 天新闻情绪负面",
    "sentiment_bullish": "Finnhub 情绪分数看多",
    "sentiment_bearish": "Finnhub 情绪分数看空",
    
    # ========== 量价类 (2个) ==========
    "volume_expansion": "成交量 > 1.2x 均量",
    "volume_contraction": "成交量 < 0.8x 均量",
    
    # ========== 事件类 (3个) ==========
    "earnings_beat_streak": "连续 2+ 季超预期",
    "post_earnings_momentum": "财报后动量信号",
    "earnings_approaching": "财报 < 14 天",
}


def get_condition_list() -> list:
    """获取所有条件标签列表"""
    return list(CONDITION_TAXONOMY.keys())


def get_condition_description(condition: str) -> str:
    """获取条件标签的描述"""
    return CONDITION_TAXONOMY.get(condition, "未知条件")


def validate_conditions(conditions: list) -> Tuple[bool, list]:
    """
    验证条件列表是否全部来自固定枚举
    
    Returns:
        (is_valid, invalid_conditions)
    """
    valid_conditions = set(CONDITION_TAXONOMY.keys())
    invalid = [c for c in conditions if c not in valid_conditions]
    return len(invalid) == 0, invalid


def get_taxonomy_for_prompt() -> str:
    """生成用于 LLM prompt 的条件列表文本"""
    lines = ["以下条件标签必须从中选择:"]
    
    # 按类别分组
    categories = {
        "趋势类": ["above_ma20", "above_ma50", "below_ma20", "below_ma50", "ma_golden_cross", "ma_dead_cross"],
        "动量类": ["rsi_oversold", "rsi_overbought", "rsi_neutral_low", "macd_golden_cross", "macd_dead_cross", "bollinger_lower", "bollinger_upper", "bollinger_squeeze"],
        "价值类": ["pe_below_industry", "pe_above_industry", "revenue_growth_high", "roe_high", "near_52w_low", "near_52w_high"],
        "内部人": ["insider_cluster_buy", "insider_net_buy", "insider_net_sell"],
        "情绪/新闻": ["news_positive", "news_negative", "sentiment_bullish", "sentiment_bearish"],
        "量价类": ["volume_expansion", "volume_contraction"],
        "事件类": ["earnings_beat_streak", "post_earnings_momentum", "earnings_approaching"],
    }
    
    for category, conditions in categories.items():
        lines.append(f"\n{category}:")
        for cond in conditions:
            desc = CONDITION_TAXONOMY[cond]
            lines.append(f"  - {cond}: {desc}")
    
    return "\n".join(lines)
