"""LLM 市场研判 Prompt 模板

设计原则：
1. System prompt 控制在 ~800 tokens
2. User prompt 按场景裁剪（NEWS_ONLY/MARKET_ONLY/COMBINED）
3. LLM 输出离散分档，不要求精确数值
"""

import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# System Prompt (精简版, ~800 tokens)
# ============================================================

SYSTEM_PROMPT = """你是FTSeek量化交易系统的市场研判引擎。你的职责是综合多维输入，输出标准化的市场评估。

你不做最终交易决策——你的输出将作为调节因子输入量化引擎。

## 输出规范
严格输出JSON（不要输出其他任何内容），包含以下字段：
{
  "direction": "STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL",
  "confidence": "HIGH / MEDIUM / LOW",
  "position_tier": "FULL / HALF / LIGHT / SKIP",
  "timing": "IMMEDIATE / WAIT_CONFIRM / WAIT_PULLBACK / SKIP",
  "key_factors": ["最多3条关键决策因素"],
  "risk_flags": ["风险标记列表，无则为空数组"],
  "news_impact": {"severity": "critical/high/normal/low", "duration_minutes": 120, "affected_codes": ["HK.00700"]} 或 null,
  "cross_market_insight": "跨市场关联解读" 或 null,
  "reasoning": "50字以内推理摘要"
}

## 判断原则
1. 信息标注: 每条因素标注 [FACT]已确认 / [INFER]推断 / [ASSUME]假设
2. 冲突处理: 多源冲突时偏向保守(NEUTRAL/SKIP)
3. 时效性: 超过2小时的新闻降低影响权重
4. 不确定性: 不确定时选LOW置信度，不要臆造理由
5. 方向选择: STRONG_BUY/STRONG_SELL仅在多源强共振时使用"""


class PromptBuilder:
    """Prompt 构建器 - 按场景裁剪 User Prompt"""

    def build_system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def build_user_prompt(
        self,
        code: str,
        trigger_type: str,
        news_context=None,
        cross_market_context=None,
        technical_snapshot=None,
    ) -> str:
        """根据触发类型构建 User Prompt"""
        sections = [
            f"## 分析请求\n标的: {code}\n触发类型: {trigger_type}\n"
            f"分析时间: {time.strftime('%Y-%m-%d %H:%M HKT')}"
        ]

        if trigger_type == "NEWS_ONLY":
            sections.append(self._build_news_section(news_context))
            if technical_snapshot:
                sections.append(self._build_tech_brief(technical_snapshot))
        elif trigger_type == "MARKET_ONLY":
            sections.append(self._build_cross_market_section(cross_market_context))
            if technical_snapshot:
                sections.append(self._build_tech_section(technical_snapshot))
        elif trigger_type in ("COMBINED", "SCHEDULED"):
            if news_context:
                sections.append(self._build_news_section(news_context))
            if cross_market_context:
                sections.append(self._build_cross_market_section(cross_market_context))
            if technical_snapshot:
                sections.append(self._build_tech_section(technical_snapshot))

        sections.append("\n请根据以上信息，输出JSON格式的市场评估。")
        return "\n\n".join(sections)

    def _build_news_section(self, ctx) -> str:
        if not ctx:
            return "## 新闻输入\n无新闻数据"
        lines = [
            "## 新闻事件输入",
            f"新闻数量: {ctx.news_count}条",
            f"综合情绪: {ctx.sentiment_score:+.2f} (-1空头~+1多头)",
            f"平均时效: {ctx.avg_age_minutes:.0f}分钟",
            f"来源可信度: {ctx.source_credibility:.1f}",
        ]
        if ctx.has_breaking:
            lines.append("包含突发新闻")
        if ctx.top_news_summary:
            lines.append(f"\n核心摘要:\n{ctx.top_news_summary[:500]}")
        if ctx.headlines:
            lines.append("\n主要标题:")
            for i, h in enumerate(ctx.headlines[:5], 1):
                lines.append(f"  {i}. {h}")
        return "\n".join(lines)

    def _build_cross_market_section(self, ctx) -> str:
        if not ctx:
            return "## 跨市场输入\n无跨市场数据"
        lines = [
            "## 跨市场行情输入",
            f"SPY: {ctx.spy_change_pct:+.2f}%  QQQ: {ctx.qqq_change_pct:+.2f}%",
            f"VIX: {ctx.vix:.1f} ({ctx.vix_status})",
            f"美债10Y: {ctx.us_bond_10y:.2f}%",
            f"恒指: {ctx.hsi_change_pct:+.2f}%",
            f"南向资金: {ctx.southbound_net:+.1f}亿",
        ]
        if ctx.arb_pairs:
            lines.append("\nADR套利对:")
            for pair in ctx.arb_pairs[:5]:
                us = pair.get("us_ticker", "?")
                hk = pair.get("hk_code", "?")
                spread = pair.get("spread", 0)
                corr = pair.get("correlation", 0)
                lines.append(f"  {us}->{hk}: 价差{spread:+.2f}% 相关性{corr:.2f}")
        return "\n".join(lines)

    def _build_tech_section(self, snap) -> str:
        if not snap:
            return "## 技术面输入\n无技术数据"
        lines = [
            "## 技术指标输入",
            f"标的: {snap.code}  价格: {snap.price:.2f}",
            f"市场状态: {snap.market_regime}",
        ]
        if snap.rsi_14 is not None:
            lines.append(f"RSI(14): {snap.rsi_14:.1f}")
        if snap.macd_histogram is not None:
            lines.append(f"MACD柱: {snap.macd_histogram:+.4f}")
        if snap.adx is not None:
            lines.append(f"ADX: {snap.adx:.1f}")
        if snap.atr_14 is not None:
            lines.append(f"ATR(14): {snap.atr_14:.2f}")
        if snap.bollinger_position:
            lines.append(f"布林带位置: {snap.bollinger_position}")
        lines.append(f"成交量比: {snap.volume_ratio:.1f}x")
        return "\n".join(lines)

    def _build_tech_brief(self, snap) -> str:
        """技术面简要版（用于 NEWS_ONLY 场景）"""
        if not snap:
            return ""
        rsi_val = snap.rsi_14 if snap.rsi_14 is not None else "N/A"
        adx_val = snap.adx if snap.adx is not None else "N/A"
        return (
            f"## 技术面概况\n"
            f"{snap.code} 价格{snap.price:.2f} 市场{snap.market_regime} "
            f"RSI={rsi_val} ADX={adx_val}"
        )
