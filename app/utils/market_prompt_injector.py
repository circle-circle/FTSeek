# -*- coding: utf-8 -*-
"""市场规则 Prompt 注入器 (FTSeek 适配版)

根据股票代码自动识别市场 (HK/US)，并注入该市场的特有交易规则到 LLM Prompt 中。
防止 LLM 产生跨市场幻觉（如建议港股做 T+0 止损，或美股做空整手限制）。
"""
from __future__ import annotations
import re
from typing import Optional, Dict

# 市场识别正则
_HK_PATTERN = re.compile(r'^(HK\.)?(\d{5})(\.HK)?$', re.IGNORECASE)
_US_PATTERN = re.compile(r'^(US\.)?[A-Z]{1,5}(\.[A-Z]{1,2})?$', re.IGNORECASE)  # 添加 IGNORECASE

# FTSeek 定制化市场规则 (基于用户哲学: LLM是刹车，规则做决策)
MARKET_RULES: Dict[str, str] = {
    "hk": """
【港股交易规则约束 (LLM 必须遵守)】
1. 交易机制: T+0 交易，无涨跌幅限制，支持当日回转交易。
2. 交易单位: 必须按"整手"买卖，不可拆分（如腾讯 1 手=100 股）。碎股无法通过常规订单交易。
3. 交易时间: 
   - 开市前时段: 09:00-09:30 HKT
   - 早市: 09:30-12:00 HKT
   - 午市: 12:00-13:00 HKT (休市)
   - 下午市: 13:00-16:00 HKT
   - 半日市: 部分节假日仅上午交易，12:00 后休市。
4. 风险提示: 港股受南向资金、美元汇率及内地政策影响大。警惕"老千股"及流动性枯竭风险。
5. 止损建议: 由于无涨跌停限制，波动可能极大，建议设置较宽止损或使用条件单。
6. 费用感知: 港股交易费用较高 (~140bps)，小额交易易被费用侵蚀，建议交易金额 > HKD 8,000。
""",
    "us": """
【美股交易规则约束 (LLM 必须遵守)】
1. 交易机制: T+0 交易 (账户需满足 $25,000 净值要求，否则受限 PDT 规则)。
2. 交易单位: 最小交易单位为 1 股，支持碎股交易 (Fractional Shares)。
3. 交易时间 (ET):
   - 盘前: 04:00-09:30
   - 正股: 09:30-16:00
   - 盘后: 16:00-20:00
4. 风险提示: 美股受美联储政策、SEC 监管及财报季影响大。注意期权交割日 (OpEx) 波动。
5. 做空规则: 美股融券做空相对容易，但需注意借券成本和强制平仓风险 (Short Squeeze)。
6. 熔断机制: 大盘跌幅 7%/13%/20% 触发全市场熔断，个股无涨跌幅限制。
"""
}

def detect_market(code: str) -> str:
    """识别市场类型
    
    Args:
        code: 股票代码 (如 HK.00700, US.AAPL)
        
    Returns:
        "hk", "us", 或 "unknown"
    """
    if not code:
        return "unknown"
    code = code.strip().upper()
    if _HK_PATTERN.match(code):
        return "hk"
    if _US_PATTERN.match(code):
        return "us"
    return "unknown"

def inject_market_context(code: str, prompt: str) -> str:
    """注入市场规则到 Prompt
    
    Args:
        code: 股票代码 (单个或多个，逗号分隔)
        prompt: 原始 Prompt 文本
        
    Returns:
        注入规则后的 Prompt
    """
    # 支持多个标的 (如 "HK.00700,US.AAPL")
    codes = [c.strip() for c in code.split(',') if c.strip()]
    markets_seen = set()
    
    for c in codes:
        m = detect_market(c)
        if m != "unknown":
            markets_seen.add(m)
    
    injected_rules = []
    for m in sorted(markets_seen):  # hk 在前, us 在后
        if m in MARKET_RULES:
            injected_rules.append(MARKET_RULES[m])
    
    if injected_rules:
        rules_header = "\n".join(injected_rules)
        return f"{rules_header}\n\n{prompt}"
    return prompt
