"""LLM 输出解析器 - 将 LLM 文本输出解析为 MarketAssessment"""

import json
import re
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


# 合法值集合
VALID_DIRECTIONS = {"STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL"}
VALID_CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}
VALID_POSITION_TIERS = {"FULL", "HALF", "LIGHT", "SKIP"}
VALID_TIMINGS = {"IMMEDIATE", "WAIT_CONFIRM", "WAIT_PULLBACK", "SKIP"}
VALID_SEVERITIES = {"critical", "high", "normal", "low"}


class LLMOutputParser:
    """解析 LLM 返回的 JSON 文本为 MarketAssessment"""

    def parse(self, raw_text: str, code: str = "", trigger_type: str = "COMBINED"):
        """
        解析 LLM 原始输出文本

        Args:
            raw_text: LLM 返回的原始文本（期望是 JSON）
            code: 标的代码
            trigger_type: 触发类型

        Returns:
            MarketAssessment 对象

        Raises:
            ValueError: 解析失败时
        """
        from .llm_market_analyst import MarketAssessment, NewsImpact

        # 1. 提取 JSON
        json_data = self._extract_json(raw_text)
        if json_data is None:
            raise ValueError(f"无法从LLM输出中提取JSON: {raw_text[:200]}")

        # 2. 校验并规范化字段
        direction = self._validate_enum(
            json_data.get("direction", "NEUTRAL"),
            VALID_DIRECTIONS, "NEUTRAL"
        )
        confidence = self._validate_enum(
            json_data.get("confidence", "LOW"),
            VALID_CONFIDENCE, "LOW"
        )
        position_tier = self._validate_enum(
            json_data.get("position_tier", "SKIP"),
            VALID_POSITION_TIERS, "SKIP"
        )
        timing = self._validate_enum(
            json_data.get("timing", "SKIP"),
            VALID_TIMINGS, "SKIP"
        )

        # 3. 解析列表字段
        key_factors = self._parse_string_list(json_data.get("key_factors", []))[:3]
        risk_flags = self._parse_string_list(json_data.get("risk_flags", []))

        # 4. 解析 news_impact
        news_impact = None
        raw_impact = json_data.get("news_impact")
        if raw_impact and isinstance(raw_impact, dict):
            severity = self._validate_enum(
                raw_impact.get("severity", "normal"),
                VALID_SEVERITIES, "normal"
            )
            duration = self._safe_int(
                raw_impact.get("duration_minutes", 60),
                default=60, min_val=5, max_val=1440
            )
            affected = self._parse_string_list(raw_impact.get("affected_codes", [code]))
            if not affected:
                affected = [code] if code else []
            news_impact = NewsImpact(
                severity=severity,
                duration_minutes=duration,
                affected_codes=affected,
            )

        # 5. 解析其他字段
        cross_market_insight = None
        raw_cm = json_data.get("cross_market_insight")
        if raw_cm and isinstance(raw_cm, str) and raw_cm.strip():
            cross_market_insight = raw_cm.strip()[:200]

        reasoning = str(json_data.get("reasoning", ""))[:100]

        # 6. 构建结果
        assessment = MarketAssessment(
            direction=direction,
            confidence_level=confidence,
            position_tier=position_tier,
            key_factors=key_factors,
            risk_flags=risk_flags,
            news_impact=news_impact,
            cross_market_insight=cross_market_insight,
            recommended_timing=timing,
            reasoning=reasoning,
            trigger_type=trigger_type,
        )

        return assessment

    def _extract_json(self, text: str) -> Optional[Dict]:
        """从文本中提取 JSON 对象"""
        text = text.strip()

        # 策略1: 直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 策略2: 提取 ```json ... ``` 代码块
        match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 策略3: 找第一个 { ... } 块
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        return None

    def _validate_enum(self, value: Any, valid_set: set, default: str) -> str:
        """校验枚举值，无效时返回默认值"""
        if isinstance(value, str):
            stripped = value.strip()
            # 处理可能的斜杠分隔（如 "STRONG_BUY / BUY"）
            if "/" in stripped:
                stripped = stripped.split("/")[0].strip()
            # 直接匹配（大小写敏感）
            if stripped in valid_set:
                return stripped
            # 大写匹配
            if stripped.upper() in valid_set:
                return stripped.upper()
            # 小写匹配
            if stripped.lower() in valid_set:
                return stripped.lower()
            # 模糊匹配
            for valid in valid_set:
                if valid.upper() == stripped.upper():
                    return valid
        logger.warning("无效枚举值 '%s'，使用默认值 '%s'", value, default)
        return default

    def _parse_string_list(self, value: Any) -> List[str]:
        """解析字符串列表"""
        if isinstance(value, list):
            return [str(item).strip() for item in value if item]
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        return []

    def _safe_int(
        self,
        value: Any,
        default: int = 0,
        min_val: Optional[int] = None,
        max_val: Optional[int] = None,
    ) -> int:
        """安全整数转换"""
        try:
            result = int(float(value))
        except (TypeError, ValueError):
            result = default
        if min_val is not None:
            result = max(result, min_val)
        if max_val is not None:
            result = min(result, max_val)
        return result
