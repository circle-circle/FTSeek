#!/usr/bin/env python3
"""
LLM Output Validator

验证 LLM 输出的合规性。
核心逻辑：
1. 检查必要字段。
2. 强制执行业务规则（如置信度低于 0.6 必须为 NO_TRADE）。
"""

import logging
from typing import Dict, Any

logger = logging.getLogger("ftseek.utils.llm_validator")

def validate_strategy(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    验证并修正 LLM 的策略输出。
    如果不符合风控要求，强制修正为 NO_TRADE。
    """
    if not result:
        return result
        
    try:
        # 1. 提取关键字段 (兼容不同 Key)
        direction = result.get("direction", "NO_TRADE")
        confidence = float(result.get("confidence", 0.0))
        tradeability = result.get("tradeability", "no_trade")
        
        # 2. 强制规则 1: 置信度门槛
        if confidence < 0.60:
            logger.warning(f"📉 置信度 {confidence:.2f} 低于 0.60 -> 强制 NO_TRADE")
            result["direction"] = "NO_TRADE"
            result["tradeability"] = "no_trade"
            return result

        # 3. 强制规则 2: Tradeability 检查
        if tradeability == "no_trade":
            logger.warning(f"🛑 Tradeability 为 no_trade -> 强制 NO_TRADE")
            result["direction"] = "NO_TRADE"
            return result

        # 4. 强制规则 3: 定价状态 (如果已充分定价，建议观望)
        # 这是一个建议规则，不强制，但打日志
        pricing = result.get("pricing_status", "unknown")
        if pricing == "fully_priced_in":
            logger.warning(f"⚠️ 事件已充分定价，风险较高")

        return result

    except Exception as e:
        logger.error(f"LLM 校验异常: {e}")
        # 出错时保守处理
        result["direction"] = "NO_TRADE"
        result["error"] = str(e)
        return result
