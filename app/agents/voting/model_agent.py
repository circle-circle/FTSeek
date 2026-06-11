# app/agents/voting/model_agent.py
"""
多模型投票 Agent

支持 DeepSeek / 阿里百炼（Qwen/GLM5/Minimax）真实 LLM API 调用
fallback 到规则计算保证可用性
"""
import os
import json
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Optional
import uuid

import requests
from dotenv import load_dotenv
from pathlib import Path

# 加载环境变量
_env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env.us"
if _env_path.exists():
    load_dotenv(_env_path)

logger = logging.getLogger(__name__)


@dataclass
class ModelScore:
    """模型评分"""
    model_id: str
    strength: int         # 0-100
    confidence: float     # 0.0-1.0
    direction: str = ""   # 必须显式设置，不设默认值（消除 LONG 偏见, 2026-06-11）
    reasoning: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    score_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "score_id": self.score_id,
            "model_id": self.model_id,
            "strength": self.strength,
            "confidence": self.confidence,
            "direction": self.direction,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp,
        }


class ModelAgent:
    """单模型评分 Agent（支持真实 LLM API 调用）"""

    # API 请求超时（秒）- 2026-03-27 更新：统一为 90 秒，与 coordinator 保持一致
    API_TIMEOUT = 90  # 与 VotingCoordinator.timeout_seconds 保持一致

    def __init__(self, role: str, model_name: str, system_prompt: str = ""):
        self.role = role
        self.model_name = model_name
        self.system_prompt = system_prompt or self._default_prompt()

        # API 配置（从环境变量读取）
        self.deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "")
        self.deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

        self.bailian_api_key = os.getenv("BAILIAN_API_KEY", "")
        # P1-3 修复 (2026-04-17): 如果 env var 被 systemd 截断, 从 config 模块读取
        if not self.bailian_api_key or self.bailian_api_key.startswith("***") or len(self.bailian_api_key) < 10:
            try:
                import config as _us_config
                key_from_config = getattr(_us_config, "BAILIAN_API_KEY", "")
                if key_from_config and not key_from_config.startswith("***"):
                    self.bailian_api_key = key_from_config
            except Exception:
                pass
        self.bailian_base_url = os.getenv(
            "BAILIAN_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )

    def _default_prompt(self) -> str:
        """默认提示词"""
        return """你是一个专业的美股量化交易分析师。请根据提供的市场数据，给出 0-100 的综合评分。

评分标准:
- 0-30: 强烈看空（强烈建议卖出/做空）
- 31-45: 看空（偏空）
- 46-55: 中性（观望）
- 56-70: 看多（偏多）
- 71-100: 强烈看多（强烈建议买入）

【重要规则】
1. 不要给出 50 分除非所有指标都绝对中性（RSI 48-52, MACD≈0, 无新闻, 趋势 NEUTRAL）。
2. 即使指标微弱偏向，也应给出 46-54 的倾向评分，而非直接 50。
3. 新闻情绪是重要参考：正面新闻 >5 条时应上调评分，负面新闻 >5 条时应下调评分。
4. 请给出明确的方向偏好，避免"中立观望"作为默认输出。

【重要】返回纯 JSON，不要使用 ```json 代码块包裹，不要有任何额外文本。
返回示例：{"strength": 65, "confidence": 0.72, "direction": "LONG", "reasoning": "均线多头排列"}

请严格按照 JSON 格式返回（不要包含任何额外文本）:
{"strength": <0-100 整数>, "confidence": <0.0-1.0 小数>, "direction": "<LONG 或 SHORT>", "reasoning": "<50 字以内的分析理由>"}"""

    def generate_score(self, market_data: Dict[str, Any]) -> ModelScore:
        """
        生成评分（优先调用 LLM API，失败时 fallback 到规则）
        """
        try:
            prompt = self._build_prompt(market_data)
            response_text = self._call_llm(prompt)
            if response_text:
                score = self._parse_response(response_text)
                if score:
                    logger.info(
                        f"[{self.role}] LLM 评分 {market_data.get('symbol','?')[:10]} "
                        f"strength={score.strength} dir={score.direction}"
                    )
                    return score
        except Exception as e:
            logger.warning(f"[{self.role}] LLM 调用失败，使用规则 fallback: {e}")

        # fallback 到规则计算
        return self._rule_based_score(market_data)

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    def _call_llm(self, prompt: str) -> Optional[str]:
        """根据模型名称路由到对应 API — 2026-05-21 迁移到 UnifiedLLMService

        旧: 手动 deepseek/bailian 路由 + requests 直连
        新: YAML llm.features.model_agent 路由 (primary: deepseek-pro, fallback: qwen-plus)
        """
        from app.llm.unified_service import UnifiedLLMService
        return UnifiedLLMService.call_text(
            "model_agent", prompt,
            system=self.system_prompt,
            temperature=0.3,
            max_tokens=200,
            json_mode=True,
        )

    def _call_deepseek(self, prompt: str) -> Optional[str]:
        """调用 DeepSeek API — 2026-05-21 废弃，统一走 _call_llm"""
        return self._call_llm(prompt)

    def _call_bailian(self, prompt: str) -> Optional[str]:
        """调用阿里百炼 API — 2026-05-21 废弃，统一走 _call_llm"""
        return self._call_llm(prompt)

    # ------------------------------------------------------------------
    # 提示词构建
    # ------------------------------------------------------------------

    def _build_prompt(self, data: Dict[str, Any]) -> str:
        """构建结构化提示词 — 注入 LV3 Tick 数据 + 市场差异化指令"""
        symbol = data.get("symbol") or data.get("ticker") or "UNKNOWN"
        price = data.get("price", 0)
        trend = data.get("trend_direction", data.get("trend", "NEUTRAL"))
        volume_ratio = data.get("volume_ratio", 1.0)
        momentum = data.get("momentum", 0)
        news_sentiment = data.get("news_sentiment", 0)
        market_regime = data.get("market_regime", "RANGE")
        vix = data.get("vix", 20)
        spx_chg = data.get("spx_change_percent", 0)
        atr = data.get("atr", 0)
        ma20 = data.get("ma20", 0)
        ma50 = data.get("ma50", 0)
        rsi = data.get("rsi", 0)
        macd = data.get("macd", 0)

        # P1 修复: LV3 Tick 数据
        lv3_bid = data.get("lv3_bid", 0)
        lv3_ask = data.get("lv3_ask", 0)
        lv3_bid_vol = data.get("lv3_bid_vol", 0)
        lv3_ask_vol = data.get("lv3_ask_vol", 0)
        order_flow = data.get("order_flow_imbalance", 0)
        spread = lv3_ask - lv3_bid if (lv3_ask > 0 and lv3_bid > 0) else 0

        lv3_section = ""
        if lv3_bid_vol > 0 or lv3_ask_vol > 0:
            flow_desc = "买方主导" if order_flow > 0.2 else ("卖方主导" if order_flow < -0.2 else "买卖均衡")
            lv3_section = f"""
【LV3 实时盘口】
- 买一价: ${lv3_bid:.2f}  卖一价: ${lv3_ask:.2f}  价差: ${spread:.2f}
- 买一量: {lv3_bid_vol:.0f}  卖一量: {lv3_ask_vol:.0f}
- 订单流不平衡: {order_flow:+.2f} ({flow_desc})
"""

        # P1-3 修复: 做空信号提示
        short_section = ""
        if data.get("short_signal_triggered"):
            reasons = "; ".join(data.get("short_reasons", []))
            short_section = f"""
⚠️【做空信号触发】{reasons}
技术面出现极端超买/背离信号, 请重点评估做空 (SHORT) 可行性。
"""

        # P1 优化: 检查新闻数据源健康度
        news_warning = ""
        try:
            import os
            from pathlib import Path
            from datetime import datetime
            
            cache_dir = Path(__file__).parent.parent.parent.parent / "data" / "rsshub_multi_source"
            if cache_dir.exists():
                json_files = list(cache_dir.glob("multi_source_*.json"))
                if json_files:
                    latest = max(json_files, key=os.path.getmtime)
                    age_seconds = time.time() - os.path.getmtime(latest)
                    age_hours = age_seconds / 3600
                    
                    current_hour = datetime.now().hour
                    if age_hours > 6.0 and 9 <= current_hour <= 16:
                        news_warning = f"""
⚠️【数据源警告】新闻数据缓存已 {age_hours:.1f} 小时未更新 (最后更新: {datetime.fromtimestamp(os.path.getmtime(latest)).strftime('%H:%M')})。
新闻情绪指标可能严重滞后或完全失效。请忽略 sentiment 分数，主要基于技术面 (MACD/RSI)、盘口数据 (LV3) 和趋势进行判断。
"""
        except Exception as e:
            pass

        # P1-3: 注入市场差异化指令 (MarketProfile)
        market_context = ""
        try:
            from app.market_profile import get_market_profile
            profile = get_market_profile()
            if profile.llm_prompt_suffix:
                market_context = f"\n\n{profile.llm_prompt_suffix}\n"
        except Exception:
            pass  # 静默失败，不影响主流程

        prompt = f"""请对以下美股进行分析并评分：

【股票信息】
- 代码：{symbol}
- 当前价格：${price:.2f}
- MA20: ${ma20:.2f}  MA50: ${ma50:.2f}
- 趋势方向：{trend}
- 成交量比率：{volume_ratio:.2f}x（相对 20 日均量）
- 动量：{momentum:.2%}（价格偏离 MA20）
- RSI: {rsi:.1f}  MACD: {macd:.2f}
- ATR 波动率：{atr:.2f}{lv3_section}{short_section}
【市场环境】
- 大盘状态：{market_regime}
- VIX 恐慌指数：{vix:.1f}
- 标普 500 日涨跌：{spx_chg:.2%}
- 新闻情绪分：{news_sentiment:.2f}（-1.0 极度悲观 ~ +1.0 极度乐观）
{news_warning}
请基于你的专业角色（{self.role}），综合以上数据给出评分。{market_context}
返回 JSON: {{"strength": <整数 0-100>, "confidence": <小数 0.0-1.0>, "direction": "<LONG 或 SHORT>", "reasoning": "<简要理由>"}}"""

        return prompt

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    def _parse_response(self, response: str) -> Optional[ModelScore]:
        """解析 LLM 返回的 JSON"""
        try:
            # 移除可能的 markdown 代码块标记
            response = response.strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.startswith("```"):
                response = response[3:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()
            
            # 尝试直接解析
            data = json.loads(response)
        except json.JSONDecodeError:
            # 尝试从文本中提取 JSON
            match = re.search(r'\{[^{}]*"strength"[^{}]*\}', response, re.DOTALL)
            if not match:
                logger.warning(f"[{self.role}] 无法解析 LLM 响应：{response[:200]}")
                return None
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return None

        strength = int(data.get("strength", 50))
        strength = max(0, min(100, strength))

        confidence = float(data.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        direction = str(data.get("direction", "")).upper().strip()
        if direction not in ("LONG", "SHORT"):
            # LLM 未提供有效方向：从 strength 推断但降低置信度 (消除 LONG 偏见, 2026-06-11)
            logger.warning(f"[ModelAgent] {self.role}: LLM 未提供有效方向, strength={strength} 推断")
            if strength >= 60:
                direction = "LONG"
                confidence *= 0.7  # 降置信度
            elif strength <= 40:
                direction = "SHORT"
                confidence *= 0.7
            else:
                # 中性区间：标记为不确定，降置信度到接近过滤阈值
                direction = "LONG" if strength > 50 else "SHORT"
                confidence = min(confidence, 0.15)  # 会被下游 min_confidence 过滤

        reasoning = str(data.get("reasoning", ""))[:200]

        return ModelScore(
            model_id=self.role,
            strength=strength,
            confidence=confidence,
            direction=direction,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # 规则 fallback
    # ------------------------------------------------------------------

    def _rule_based_score(self, data: Dict[str, Any]) -> ModelScore:
        """基于规则的评分（LLM 不可用时使用）"""
        base = 50

        # 成交量因子（对称化：消除 LONG 偏见, 2026-06-11）
        volume_ratio = data.get("volume_ratio", 1.0)
        if volume_ratio > 2.0:
            base += 15
        elif volume_ratio > 1.5:
            base += 8
        elif volume_ratio < 0.5:
            base -= 15
        elif volume_ratio < 0.7:
            base -= 8

        # 动量因子
        momentum = data.get("momentum", 0)
        base += min(20, max(-20, int(momentum * 800)))

        # 趋势因子
        trend = data.get("trend_direction", data.get("trend", "NEUTRAL"))
        if trend == "UP":
            base += 10
        elif trend == "DOWN":
            base -= 10

        # 新闻情绪
        sentiment = data.get("news_sentiment", 0)
        base += int(sentiment * 10)

        # VIX 高时降低置信度
        vix = data.get("vix", 20)
        vix_penalty = max(0, (vix - 25) / 10)  # VIX>25 每增加 10 降 10 分

        strength = max(0, min(100, base - int(vix_penalty * 10)))

        # 置信度：数据越完整越高
        # 必备：price, volume_ratio, momentum, trend；可选：atr, news_sentiment
        has_core = all(k in data for k in ("price", "volume_ratio", "momentum"))
        has_extra = all(k in data for k in ("atr", "news_sentiment", "trend"))
        if has_core and has_extra:
            confidence = 0.40  # 数据完整 (从 0.55 降至 0.40)
        elif has_core:
            confidence = 0.30  # 核心数据齐 (从 0.45 降至 0.30)
        else:
            confidence = 0.20  # 数据不足 (从 0.35 降至 0.20)

        # 从动量推导方向（动量是方向性最强的因子，比 strength 阈值更准确, 2026-06-11）
        momentum = data.get("momentum", 0)
        if momentum > 0.02:
            direction = "LONG"
        elif momentum < -0.02:
            direction = "SHORT"
        else:
            direction = "LONG" if strength > 55 else "SHORT"  # 弱信号轻微偏好（趋势市场）

        return ModelScore(
            model_id=self.role,
            strength=strength,
            confidence=min(0.5, confidence),  # 上限从 0.8 降至 0.5
            direction=direction,
            reasoning=f"规则评分 (fallback): 动量={momentum:.2%} 成交量比={volume_ratio:.1f}x 趋势={trend}",
        )
