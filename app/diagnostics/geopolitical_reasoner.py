"""
地缘政治推理模块 — 宏观事件 → 行业影响 → 个股信号

借鉴 Vibe-Trading 的 geopolitical_war_room swarm preset 逻辑:
  - 将宏观/地缘事件自动关联到行业影响
  - 再映射到 FTSeek 持仓/自选股
  - 生成 "联想推理" 链条

非交易路径: 仅在新闻管线有新事件时异步触发，不阻塞盘中执行

设计:
  轻量级规则推理 (不依赖 LLM) → 输出结构化传导链
  可选 LLM 增强模式 (盘前运行)
"""

import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from app.diagnostics.base import BaseDiagnostic, DiagnosticResult

logger = logging.getLogger(__name__)

# ── 地缘事件 → 行业传导矩阵 (静态知识库) ──
#
# 格式: event_keyword → {impact: {sector: direction, ...}, confidence}
# direction: "bullish" | "bearish" | "neutral"
#
# 这是基础规则层，覆盖高频确定性传导关系。
# LLM 增强模式可以处理长尾/新型事件。

GEOPOLITICAL_KNOWLEDGE_BASE = {
    # 能源冲突
    "oil_supply_disruption": {
        "keywords": ["伊朗", "中东", "霍尔木兹", "原油", "opec减产", "沙特"],
        "impact": {
            "Energy": "bullish",
            "Airlines": "bearish",
            "Consumer Cyclical": "bearish",
            "Industrials": "bearish",
        },
        "confidence": 0.85,
    },
    "iran_ceasefire": {
        "keywords": ["伊朗停火", "中东停火", "伊朗和谈"],
        "impact": {
            "Energy": "bearish",
            "Airlines": "bullish",
            "Consumer Cyclical": "bullish",
            "Transportation": "bullish",
        },
        "confidence": 0.80,
    },

    # 贸易冲突
    "tariff_escalation": {
        "keywords": ["关税", "贸易战", "制裁", "出口管制", "芯片禁令"],
        "impact": {
            "Semiconductors": "bearish",
            "Consumer Cyclical": "bearish",
            "Technology": "bearish",
        },
        "confidence": 0.75,
    },
    "trade_deescalation": {
        "keywords": ["关税豁免", "贸易协议", "关税取消", "和解"],
        "impact": {
            "Semiconductors": "bullish",
            "Consumer Cyclical": "bullish",
            "Technology": "bullish",
        },
        "confidence": 0.75,
    },

    # 美联储政策
    "fed_rate_hike": {
        "keywords": ["美联储加息", "fed加息", "升息", "加息"],
        "impact": {
            "Financial Services": "bullish",
            "Real Estate": "bearish",
            "Technology": "bearish",
            "Consumer Cyclical": "bearish",
        },
        "confidence": 0.80,
    },
    "fed_rate_cut": {
        "keywords": ["美联储降息", "fed降息", "降息"],
        "impact": {
            "Technology": "bullish",
            "Consumer Cyclical": "bullish",
            "Real Estate": "bullish",
            "Financial Services": "bearish",
        },
        "confidence": 0.80,
    },

    # 疫情/公共卫生
    "pandemic_lockdown": {
        "keywords": ["封城", " lockdown", "疫情", "感染飙升"],
        "impact": {
            "Healthcare": "bullish",
            "Technology": "bullish",
            "Consumer Cyclical": "bearish",
            "Industrials": "bearish",
            "Energy": "bearish",
        },
        "confidence": 0.70,
    },

    # 科技监管
    "tech_regulation": {
        "keywords": ["反垄断", "科技监管", "数据隐私", "ai管制", "tiktok禁令"],
        "impact": {
            "Technology": "bearish",
            "Communication Services": "bearish",
        },
        "confidence": 0.65,
    },

    # 通胀
    "inflation_spike": {
        "keywords": ["通胀飙升", "cpi超预期", "ppi大涨", "物价上涨"],
        "impact": {
            "Energy": "bullish",
            "Consumer Defensive": "bullish",
            "Consumer Cyclical": "bearish",
            "Technology": "bearish",
        },
        "confidence": 0.70,
    },
}

# ── 美股代码 → 行业映射 (从 watchlist 自动获取) ──


class GeopoliticalReasoner(BaseDiagnostic):

    @property
    def name(self) -> str:
        return "geopolitical_reasoner"

    @property
    def schedule_hint(self) -> str:
        return "on_demand"  # 新闻事件触发

    def is_applicable(self, **kwargs) -> bool:
        """需要有新闻输入"""
        return kwargs.get("news_items") is not None

    def run(self, **kwargs) -> DiagnosticResult:
        """
        执行地缘政治推理

        输入:
          - news_items: 新闻标题列表
          - watchlist: 自选股列表 (从配置获取)
          - mode: "rules" | "llm" (默认 rules)
        """
        news_items = kwargs.get("news_items", [])
        watchlist = kwargs.get("watchlist", [])
        mode = kwargs.get("mode", "rules")

        if not news_items:
            return DiagnosticResult(
                module_name=self.name,
                status="skipped",
                data={"reason": "无新闻输入"},
                summary="无新闻可分析",
            )

        # ── 1. 事件匹配 (规则引擎) ──
        matched_events = self._match_events(news_items)

        # ── 2. 行业影响推导 ──
        sector_impacts = self._derive_sector_impact(matched_events)

        # ── 3. 个股映射 ──
        stock_signals = self._map_to_stocks(sector_impacts, watchlist)

        # ── 4. 生成传导链 ──
        reasoning_chains = self._build_chains(matched_events, sector_impacts, stock_signals)

        return DiagnosticResult(
            module_name=self.name,
            status="success",
            data={
                "matched_events": matched_events,
                "sector_impacts": sector_impacts,
                "stock_signals": stock_signals,
                "reasoning_chains": reasoning_chains,
                "news_analyzed": len(news_items),
            },
            summary=self._build_summary(matched_events, stock_signals),
        )

    # ── 事件匹配 ──

    def _match_events(self, news_items: List[str]) -> List[Dict[str, Any]]:
        """将新闻标题匹配到预定义事件"""
        matched = []
        for item in news_items:
            if isinstance(item, dict):
                title = item.get("title", "") + " " + item.get("summary", "")
            else:
                title = str(item)

            title_lower = title.lower()

            for event_id, event_def in GEOPOLITICAL_KNOWLEDGE_BASE.items():
                for keyword in event_def["keywords"]:
                    if keyword.lower() in title_lower:
                        matched.append({
                            "event_id": event_id,
                            "news_title": title[:100],
                            "confidence": event_def["confidence"],
                            "matched_keyword": keyword,
                        })
                        break  # 同一事件只匹配一次

        # 去重 (同一事件可能匹配多条新闻)
        seen = set()
        unique = []
        for m in matched:
            if m["event_id"] not in seen:
                seen.add(m["event_id"])
                unique.append(m)

        return unique

    # ── 行业影响推导 ──

    def _derive_sector_impact(self, matched_events: List[Dict]) -> Dict[str, Dict[str, Any]]:
        """
        合并多个事件对同一行业的影响
        """
        sector_scores = {}  # sector → {"score": float, "events": [...]}

        for event in matched_events:
            event_def = GEOPOLITICAL_KNOWLEDGE_BASE.get(event["event_id"], {})
            impact = event_def.get("impact", {})
            confidence = event["confidence"]

            for sector, direction in impact.items():
                if sector not in sector_scores:
                    sector_scores[sector] = {"score": 0.0, "events": [], "bullish": 0, "bearish": 0}

                delta = confidence if direction == "bullish" else -confidence
                sector_scores[sector]["score"] += delta
                sector_scores[sector]["events"].append(event["event_id"])
                if direction == "bullish":
                    sector_scores[sector]["bullish"] += 1
                else:
                    sector_scores[sector]["bearish"] += 1

        # 归一化 + 评级
        result = {}
        for sector, data in sector_scores.items():
            score = data["score"]
            if score > 0.5:
                rating = "strong_bullish"
            elif score > 0.2:
                rating = "bullish"
            elif score < -0.5:
                rating = "strong_bearish"
            elif score < -0.2:
                rating = "bearish"
            else:
                rating = "neutral"

            result[sector] = {
                "rating": rating,
                "score": round(score, 3),
                "event_count": len(data["events"]),
                "events": list(set(data["events"])),
                "bullish_signals": data["bullish"],
                "bearish_signals": data["bearish"],
            }

        return result

    # ── 个股映射 ──

    def _map_to_stocks(
        self, sector_impacts: Dict[str, Dict], watchlist: List[Dict]
    ) -> List[Dict[str, Any]]:
        """
        将行业影响映射到具体个股

        输入 watchlist 格式:
          [{"code": "US.AAPL", "sector": "Technology", "name": "..."}, ...]
        """
        if not watchlist:
            # 降级: 从配置获取
            try:
                from app.core.config_container import get_config
                cfg = get_config()
                watchlist = cfg.get_dotted("watchlist.us_stocks", [])
            except Exception:
                return []

        signals = []
        for stock in watchlist:
            if isinstance(stock, dict):
                code = stock.get("code", "")
                sector = stock.get("sector", "")
                name = stock.get("name", "")
            else:
                code = str(stock)
                sector = ""
                name = ""

            impact = sector_impacts.get(sector)
            if impact and impact["rating"] != "neutral":
                signals.append({
                    "code": code,
                    "name": name,
                    "sector": sector,
                    "impact_rating": impact["rating"],
                    "impact_score": impact["score"],
                    "event_count": impact["event_count"],
                })

        return signals

    # ── 传导链构建 ──

    def _build_chains(
        self, events, sector_impacts, stock_signals
    ) -> List[Dict[str, str]]:
        """
        构建可读的因果传导链

        格式: "事件 → 行业影响 → 具体个股"
        """
        chains = []
        for signal in stock_signals:
            event_names = []
            for e in events:
                if e["event_id"] in sector_impacts.get(signal["sector"], {}).get("events", []):
                    event_names.append(e.get("matched_keyword", e["event_id"]))

            if not event_names:
                event_names = ["宏观事件"]

            chain = (
                f"{' / '.join(event_names[:3])} → "
                f"{signal['sector']} ({signal['impact_rating']}) → "
                f"{signal['code']} ({signal['name']})"
            )

            chains.append({
                "chain": chain,
                "stock_code": signal["code"],
                "impact_rating": signal["impact_rating"],
            })

        return chains

    def _build_summary(self, events, signals):
        if not events:
            return "✅ 分析完成，未检测到显著地缘事件"

        n_events = len(events)
        n_signals = len(signals)
        event_names = [e.get("matched_keyword", e["event_id"]) for e in events[:3]]

        return (
            f"🌍 检测到 {n_events} 个地缘/宏观事件 ({', '.join(event_names)}) → "
            f"影响 {n_signals} 只个股"
        )


def create_geopolitical_reasoner() -> GeopoliticalReasoner:
    return GeopoliticalReasoner()
