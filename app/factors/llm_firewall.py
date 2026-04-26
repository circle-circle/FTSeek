"""
LLM 防火墙 (LLM Firewall)

用途:
对 LLM 生成的交易建议进行逻辑校验，防止"幻觉交易"和格式错误导致的风险。
在信号进入共识引擎或执行层之前进行拦截。

更新时间: 2026-04-14
"""

import json
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("ftseek.factors.llm_firewall")


class LLMFirewallError(Exception):
    """LLM 防火墙拦截异常"""
    pass


class LLMFirewall:
    def __init__(self, rules: Dict = None):
        """
        初始化防火墙规则
        
        Args:
            rules: 自定义规则配置
        """
        self.rules = rules or self._default_rules()
        
    def _default_rules(self) -> Dict:
        return {
            "valid_directions": ["BUY", "SELL", "HOLD", "WATCH", "NEUTRAL"],
            "min_confidence": 0.0,
            "max_confidence": 1.0,
            "required_fields": ["direction", "confidence"],
            "optional_reasoning_fields": ["reasoning", "recommendation", "trading_thesis"],  # 任一即可
            "check_consistency": True
        }

    def validate_response(self, raw_response: str) -> Dict[str, Any]:
        """
        验证 LLM 原始响应
        
        Args:
            raw_response: LLM 输出的字符串
            
        Returns:
            解析后的字典
            
        Raises:
            LLMFirewallError: 验证失败
        """
        # 1. JSON 解析检查
        try:
            # 尝试提取 JSON 块
            content = raw_response
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            
            data = json.loads(content)
        except (json.JSONDecodeError, IndexError) as e:
            raise LLMFirewallError(f"JSON 解析失败: {str(e)}")

        # 2. 字段完整性检查
        for field in self.rules["required_fields"]:
            if field not in data:
                raise LLMFirewallError(f"缺失必要字段: {field}")

        # 2b. 推理字段检查 (至少包含一个推理相关字段)
        has_reasoning = any(
            field in data 
            for field in self.rules.get("optional_reasoning_fields", ["reasoning"])
        )
        if not has_reasoning:
            raise LLMFirewallError(
                f"缺失推理字段: 需要以下任一: {self.rules.get('optional_reasoning_fields', ['reasoning'])}"
            )

        # 3. 枚举值检查
        direction = str(data.get("direction", "")).upper()
        if direction not in self.rules["valid_directions"]:
            raise LLMFirewallError(f"无效的方向: {direction}. 允许值: {self.rules['valid_directions']}")

        # 4. 数值范围检查
        try:
            confidence = float(data.get("confidence", -1))
            if not (self.rules["min_confidence"] <= confidence <= self.rules["max_confidence"]):
                raise LLMFirewallError(f"置信度越界: {confidence} (范围 {self.rules['min_confidence']}-{self.rules['max_confidence']})")
        except (ValueError, TypeError):
            raise LLMFirewallError(f"置信度格式错误: {data.get('confidence')}")

        # 5. 逻辑一致性检查 (可选)
        if self.rules["check_consistency"]:
            self._check_consistency(data)

        return data

    def _check_consistency(self, data: Dict):
        """
        内部逻辑一致性检查
        
        例如: 新闻情绪为极度负面，但建议强烈买入 -> 可能是幻觉
        """
        sentiment = data.get("news_sentiment", "")
        direction = data.get("direction", "").upper()
        confidence = float(data.get("confidence", 0))

        if not sentiment or not direction:
            return

        # 规则 1: 情绪与方向严重背离
        sentiment_lower = str(sentiment).lower()
        
        is_bearish = "negative" in sentiment_lower or "bearish" in sentiment_lower
        is_bullish = "positive" in sentiment_lower or "bullish" in sentiment_lower
        
        if is_bearish and direction in ["BUY", "STRONG_BUY"] and confidence > 0.8:
            logger.warning(
                f"[LLM Firewall] 逻辑背离警告: 情绪 ({sentiment}) 看空但建议强烈买入 "
                f"(Confidence={confidence}). 可能存在幻觉."
            )
            # 不直接拦截，而是添加警告标记，由下游决定
            data["firewall_warning"] = "sentiment_direction_mismatch"
            
        elif is_bullish and direction in ["SELL", "STRONG_SELL"] and confidence > 0.8:
            logger.warning(
                f"[LLM Firewall] 逻辑背离警告: 情绪 ({sentiment}) 看多但建议强烈卖出 "
                f"(Confidence={confidence}). 可能存在幻觉."
            )
            data["firewall_warning"] = "sentiment_direction_mismatch"
