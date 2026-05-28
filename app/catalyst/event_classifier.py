"""
催化事件分类器 — 从新闻流提取交易相关催化事件

设计原则 (Karpathy):
- 复用 emergency_interceptor.BLACK_SWAN_KEYWORDS (利空关键词库)
- 新增利好关键词 + 中文监管关键词
- 关键词快速匹配 (O(n), 无LLM成本) 过滤 95% 无关新闻
- 命中后 LLM 辅助: 确认事件类型 + 关联标的 + 生成摘要 (中英文统一)

复用:
- emergency_interceptor.BLACK_SWAN_KEYWORDS
- NewsPipelineV2 输出 (已去重+过滤的新闻)
- call_llm(feature="catalyst_classifier") 做 LLM 辅助分类
- catalyst.sector_mapping 做行业/板块传导
"""

import hashlib
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from app.catalyst.models import CatalystEvent
from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency

logger = logging.getLogger("ftseek_us.catalyst.classifier")


# ============================================================================
#  关键词库 — 利空 (复用 emergency_interceptor)
# ============================================================================

# 直接复用 emergency_interceptor 的关键词 (英文利空)
BEARISH_KEYWORDS_EN = [
    # 监管/法律
    "SEC investigation", "SEC charges", "subpoena", "delist", "delisting",
    "receivership", "going concern", "audit qualification", "restatement",
    # FDA
    "FDA rejection", "clinical trial failed", "not approvable", "CRL",
    # 人事
    "CEO resign", "CFO resign",
    # 业绩
    "missed earnings", "revenue miss", "guidance cut", "downgraded to sell",
    "entity list", "insider selling",
    # 新增利空关键词
    "fraud", "accounting irregularity", "short seller report",
    "regulatory action", "trading halt", "suspension",
    "sanctions", "export ban", "tariff",
]

# 中文利空关键词 (CSRC/HKEX/NewsNow 中文源)
BEARISH_KEYWORDS_ZH = [
    # 监管
    "证监会", "行政处罚", "立案调查", "监管措施", "警示函",
    "责令整改", "暂停业务", "吊销牌照", "罚款", "没收违法所得",
    "市场禁入", "通报批评", "公开谴责",
    # 业绩/财务
    "业绩预亏", "商誉减值", "财务造假", "审计意见",
    "退市", "暂停上市", "终止上市",
    # 事件
    "停牌", "黑天鹅", "暴雷", "违约",
]

# ============================================================================
#  关键词库 — 利好
# ============================================================================

BULLISH_KEYWORDS_EN = [
    # FDA
    "FDA approval", "FDA approved", "breakthrough therapy",
    # 业绩
    "earnings beat", "revenue beat", "raised guidance", "upgraded to buy",
    "price target raised",
    # 并购/合作
    "acquisition", "merger", "strategic partnership",
    # 产品
    "product launch", "record sales",
    # 分析师
    "upgraded", "overweight", "outperform", "strong buy",
]

BULLISH_KEYWORDS_ZH = [
    # 业绩
    "财报超预期", "业绩预增", "营收超预期", "上调指引",
    # 产品/合作
    "获批", "新药获批", "战略合作", "收购",
    # 分析师
    "上调评级", "买入评级", "目标价上调",
    # 回购
    "股份回购", "增持",
]

# ============================================================================
#  事件类型映射
# ============================================================================

# 关键词 → (event_type, severity, default_direction)
KEYWORD_EVENT_MAP = {
    # 监管类 → REGULATORY
    "SEC investigation": ("REGULATORY", "CRITICAL", "SHORT"),
    "SEC charges": ("REGULATORY", "CRITICAL", "SHORT"),
    "证监会": ("REGULATORY", "CRITICAL", "SHORT"),
    "行政处罚": ("REGULATORY", "CRITICAL", "SHORT"),
    "立案调查": ("REGULATORY", "CRITICAL", "SHORT"),
    "监管措施": ("REGULATORY", "HIGH", "SHORT"),
    "sanctions": ("REGULATORY", "CRITICAL", "SHORT"),
    "entity list": ("REGULATORY", "HIGH", "SHORT"),
    "export ban": ("REGULATORY", "HIGH", "SHORT"),

    # 退市/停牌
    "delist": ("DELISTING", "CRITICAL", "SHORT"),
    "delisting": ("DELISTING", "CRITICAL", "SHORT"),
    "退市": ("DELISTING", "CRITICAL", "SHORT"),
    "暂停上市": ("DELISTING", "CRITICAL", "SHORT"),
    "trading halt": ("BREAKING", "HIGH", None),
    "停牌": ("BREAKING", "HIGH", None),

    # FDA
    "FDA approval": ("FDA_APPROVAL", "HIGH", "LONG"),
    "FDA approved": ("FDA_APPROVAL", "HIGH", "LONG"),
    "新药获批": ("FDA_APPROVAL", "HIGH", "LONG"),
    "获批": ("FDA_APPROVAL", "HIGH", "LONG"),
    "FDA rejection": ("FDA_REJECTION", "CRITICAL", "SHORT"),
    "clinical trial failed": ("FDA_REJECTION", "CRITICAL", "SHORT"),

    # 财报
    "earnings beat": ("EARNINGS_BEAT", "HIGH", "LONG"),
    "revenue beat": ("EARNINGS_BEAT", "HIGH", "LONG"),
    "财报超预期": ("EARNINGS_BEAT", "HIGH", "LONG"),
    "业绩预增": ("EARNINGS_BEAT", "HIGH", "LONG"),
    "missed earnings": ("EARNINGS_MISS", "HIGH", "SHORT"),
    "revenue miss": ("EARNINGS_MISS", "HIGH", "SHORT"),
    "业绩预亏": ("EARNINGS_MISS", "HIGH", "SHORT"),
    "guidance cut": ("EARNINGS_MISS", "CRITICAL", "SHORT"),

    # 评级
    "upgraded to buy": ("UPGRADE", "MEDIUM", "LONG"),
    "price target raised": ("UPGRADE", "MEDIUM", "LONG"),
    "上调评级": ("UPGRADE", "MEDIUM", "LONG"),
    "downgraded to sell": ("DOWNGRADE", "MEDIUM", "SHORT"),

    # 宏观
    "tariff": ("MACRO", "HIGH", None),
    "制裁": ("MACRO", "HIGH", None),

    # 突发
    "fraud": ("BREAKING", "CRITICAL", "SHORT"),
    "财务造假": ("BREAKING", "CRITICAL", "SHORT"),
    "暴雷": ("BREAKING", "CRITICAL", "SHORT"),
    "short seller report": ("BREAKING", "HIGH", "SHORT"),
    "CEO resign": ("BREAKING", "HIGH", "SHORT"),
    "CFO resign": ("BREAKING", "HIGH", "SHORT"),
}


class CatalystClassifier:
    """催化事件分类器
    
    从新闻管线输出 (List[UnifiedNewsItemV2]) 中提取催化事件。
    
    流程:
    1. 关键词快速匹配 (O(n), 无LLM成本)
    2. 命中项: 确定 event_type + severity + 关联标的
    3. 可选 LLM 辅助: 中文翻译 + 模糊关键词确认
    """
    
    def __init__(self, sector_mapping: Optional[Dict[str, List[str]]] = None):
        """
        Args:
            sector_mapping: 板块传导映射, 如 {"chinese_broker": ["FUTU", "TIGR"]}
        """
        self.sector_mapping = sector_mapping or {}
        # 预编译正则 (大小写不敏感)
        self._bearish_en = [(kw, re.compile(r'\b' + re.escape(kw.lower()) + r'\b'))
                            for kw in BEARISH_KEYWORDS_EN]
        self._bearish_zh = [(kw, re.compile(re.escape(kw)))
                            for kw in BEARISH_KEYWORDS_ZH]
        self._bullish_en = [(kw, re.compile(r'\b' + re.escape(kw.lower()) + r'\b'))
                            for kw in BULLISH_KEYWORDS_EN]
        self._bullish_zh = [(kw, re.compile(re.escape(kw)))
                            for kw in BULLISH_KEYWORDS_ZH]
        
        # 构建事件映射正则
        self._event_map = []
        for kw, (etype, sev, direction) in KEYWORD_EVENT_MAP.items():
            if any('\u4e00' <= c <= '\u9fff' for c in kw):  # 中文关键词
                pattern = re.compile(re.escape(kw))
            else:
                pattern = re.compile(r'\b' + re.escape(kw.lower()) + r'\b')
            self._event_map.append((kw, pattern, etype, sev, direction))
        
        # 去重缓存 (event_id → timestamp, 24h 内不重复生成)
        self._seen_events: Dict[str, float] = {}
    
    def classify(self, news_items: List[UnifiedNewsItemV2],
                 use_llm: bool = True) -> List[CatalystEvent]:
        """从新闻列表提取催化事件
        
        Args:
            news_items: NewsPipelineV2 输出的新闻列表
            use_llm: 是否用 LLM 辅助确认 (模糊关键词 + 中文翻译)
        
        Returns:
            去重后的 CatalystEvent 列表
        """
        events = []
        candidates = []
        
        for item in news_items:
            text = f"{item.title} {item.content}".lower()
            
            # Step 1: 关键词匹配
            matched_keyword, event_info = self._match_keywords(text, item)
            if not matched_keyword:
                continue
            
            event_type, severity, default_direction = event_info
            
            # Step 2: 确定受影响标的
            tickers = self._extract_tickers(item, text)
            if not tickers:
                # 无明确标的 → 尝试板块传导
                tickers = self._sector_propagation(text, matched_keyword)
            
            if not tickers:
                logger.debug("[Catalyst] %s: 无关联标的, 跳过", matched_keyword)
                continue
            
            # Step 3: 去重
            event_id = self._make_event_id(item, matched_keyword)
            if event_id in self._seen_events:
                age = time.time() - self._seen_events[event_id]
                if age < 86400:  # 24h 内去重
                    continue
            self._seen_events[event_id] = time.time()
            
            # Step 4: 判断语言
            source_lang = "zh" if any('\u4e00' <= c <= '\u9fff' for c in item.title) else "en"
            
            # 收集候选
            candidates.append({
                "event_id": event_id,
                "event_type": event_type,
                "severity": severity,
                "tickers": tickers,
                "headline": item.title,
                "summary": item.content[:500],
                "source": item.source,
                "source_language": source_lang,
                "published_at": item.timestamp,
                "default_direction": default_direction,
                "raw_item": item,
            })
        
        # Step 5: LLM 辅助 (仅对命中项, 控制成本)
        if use_llm and candidates:
            candidates = self._llm_enrich(candidates)
        
        # Step 6: 构造 CatalystEvent
        for c in candidates:
            event = CatalystEvent(
                event_id=c["event_id"],
                event_type=c["event_type"],
                severity=c["severity"],
                tickers=c["tickers"],
                headline=c["headline"],
                summary=c.get("llm_summary", c["summary"]),
                source=c["source"],
                source_language=c["source_language"],
                published_at=c["published_at"],
            )
            events.append(event)
            logger.info(
                "[Catalyst] 检测到: %s %s %s → %s",
                event.severity, event.event_type,
                event.headline[:40], ", ".join(event.tickers)
            )
        
        # 清理过期的去重缓存
        now = time.time()
        expired = [eid for eid, ts in self._seen_events.items() if now - ts > 86400]
        for eid in expired:
            del self._seen_events[eid]
        
        return events
    
    def _match_keywords(self, text: str,
                        item: UnifiedNewsItemV2) -> Tuple[Optional[str], Optional[tuple]]:
        """关键词匹配 — 返回 (matched_keyword, (event_type, severity, direction))"""
        # 优先检查事件映射表 (精确分类)
        for kw, pattern, etype, sev, direction in self._event_map:
            if pattern.search(text):
                return kw, (etype, sev, direction)
        
        # 回退: 通用利空/利好关键词 (分类为 BREAKING)
        for kw, pattern in self._bearish_en:
            if pattern.search(text):
                return kw, ("BREAKING", "MEDIUM", "SHORT")
        for kw, pattern in self._bearish_zh:
            if pattern.search(text):
                return kw, ("BREAKING", "MEDIUM", "SHORT")
        for kw, pattern in self._bullish_en:
            if pattern.search(text):
                return kw, ("BREAKING", "MEDIUM", "LONG")
        for kw, pattern in self._bullish_zh:
            if pattern.search(text):
                return kw, ("BREAKING", "MEDIUM", "LONG")
        
        # 如果 urgency 已经是 IMMEDIATE, 也作为候选
        if item.urgency == NewsUrgency.IMMEDIATE:
            return "[IMMEDIATE urgency]", ("BREAKING", "HIGH", None)
        
        return None, None
    
    def _extract_tickers(self, item: UnifiedNewsItemV2, text: str) -> List[str]:
        """从新闻条目提取关联的 US ticker"""
        tickers = set()
        
        # 1. 新闻已有的 related_stocks
        if item.related_stocks:
            tickers.update(item.related_stocks)
        
        # 2. 从文本匹配公司名 → ticker
        company_ticker_map = {
            "富途": "US.FUTU", "futu": "US.FUTU", "moomoo": "US.FUTU",
            "老虎": "US.TIGR", "tiger brok": "US.TIGR",
            "长桥": "US.LB", "longbridge": "US.LB",
            "特斯拉": "US.TSLA", "tesla": "US.TSLA",
            "英伟达": "US.NVDA", "nvidia": "US.NVDA",
            "苹果": "US.AAPL", "apple": "US.AAPL",
            "微软": "US.MSFT", "microsoft": "US.MSFT",
            "谷歌": "US.GOOGL", "alphabet": "US.GOOGL",
            "亚马逊": "US.AMZN", "amazon": "US.AMZN",
            "meta": "US.META", "facebook": "US.META",
            "amd": "US.AMD",
            "高通": "US.QCOM", "qualcomm": "US.QCOM",
            "博通": "US.AVGO", "broadcom": "US.AVGO",
            "美光": "US.MU", "micron": "US.MU",
            "arm": "US.ARM",
            "蔚来": "US.NIO",
            "小鹏": "US.XPEV", "xpeng": "US.XPEV",
            "理想": "US.LI", "li auto": "US.LI",
            "阿里": "US.BABA", "alibaba": "US.BABA",
            "拼多多": "US.PDD", "pinduoduo": "US.PDD",
            "京东": "US.JD",
            "百度": "US.BIDU", "baidu": "US.BIDU",
            "网易": "US.NTES", "netease": "US.NTES",
        }
        for name, ticker in company_ticker_map.items():
            if name.lower() in text:
                tickers.add(ticker)
        
        return list(tickers)
    
    def _sector_propagation(self, text: str, keyword: str) -> List[str]:
        """板块传导 — "中概股监管" → 所有中概券商"""
        tickers = set()
        
        # 检查是否匹配某个板块关键词
        sector_triggers = {
            "中概": "chinese_adr",
            "中概股": "chinese_adr",
            "券商": "chinese_broker",
            "互联网券商": "chinese_broker",
            "芯片": "ai_chip",
            "半导体": "ai_chip",
            "新能源": "ev",
            "电动车": "ev",
            "科技股": "big_tech",
            "chinese adr": "chinese_adr",
            "semiconductor": "ai_chip",
        }
        
        for trigger, sector_key in sector_triggers.items():
            if trigger in text.lower():
                sector_tickers = self.sector_mapping.get(sector_key, [])
                tickers.update(f"US.{t}" if not t.startswith("US.") else t
                               for t in sector_tickers)
        
        return list(tickers)
    
    def _make_event_id(self, item: UnifiedNewsItemV2, keyword: str) -> str:
        """生成事件唯一 ID (用于去重)"""
        raw = f"{item.source}:{keyword}:{item.title[:50]}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]
    
    def _llm_enrich(self, candidates: List[dict]) -> List[dict]:
        """LLM 辅助: 中文翻译 + 模糊关键词确认 + 摘要生成
        
        仅对 MEDIUM severity 或中文新闻调用 LLM (控制成本)
        """
        needs_llm = [
            c for c in candidates
            if c["severity"] == "MEDIUM" or c["source_language"] == "zh"
        ]
        
        if not needs_llm:
            return candidates
        
        for c in needs_llm:
            try:
                from app.llm.model_call import call_llm
                
                prompt = (
                    f"分析以下新闻对美股的影响:\n"
                    f"标题: {c['headline']}\n"
                    f"内容: {c['summary'][:300]}\n"
                    f"匹配关键词: {c.get('_matched_keyword', 'N/A')}\n\n"
                    f"返回JSON:\n"
                    f'{{"confirmed": true/false, '
                    f'"event_type": "REGULATORY/EARNINGS_BEAT/...", '
                    f'"severity": "CRITICAL/HIGH/MEDIUM", '
                    f'"direction": "LONG/SHORT/NEUTRAL", '
                    f'"summary_en": "英文摘要(50字内)", '
                    f'"affected_tickers": ["US.XXX"]}}'
                )
                
                result = call_llm(
                    feature="catalyst_classifier",
                    prompt=prompt,
                    json_mode=True,
                    timeout=20,
                )
                
                if result.success and result.response:
                    llm_data = result.response
                    if not llm_data.get("confirmed", True):
                        logger.info("[Catalyst] LLM 否决: %s", c["headline"][:40])
                        candidates.remove(c)
                        continue
                    
                    # LLM 可能修正分类
                    if llm_data.get("event_type"):
                        c["event_type"] = llm_data["event_type"]
                    if llm_data.get("severity"):
                        c["severity"] = llm_data["severity"]
                    if llm_data.get("summary_en"):
                        c["llm_summary"] = llm_data["summary_en"]
                    if llm_data.get("affected_tickers"):
                        # 合并 LLM 识别的标的
                        existing = set(c["tickers"])
                        existing.update(llm_data["affected_tickers"])
                        c["tickers"] = list(existing)
                        
            except Exception as e:
                logger.debug("[Catalyst] LLM 辅助失败 (保留原始分类): %s", e)
        
        return candidates
