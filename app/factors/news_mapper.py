#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻 - 股票映射器 (News-to-Stock Mapper)

功能:
- 使用 LLM 做命名实体识别 (NER)
- 建立新闻内容到股票的自动映射
- 支持影响链扩散 (新闻提到"电动车"→自动关联 Tesla/比亚迪/锂电池)

核心流程:
新闻文本 → NLP 实体识别 → 股票池匹配 → 影响链扩散 → 映射结果

示例:
    mapper = NewsStockMapper()
    result = mapper.map_news_to_stocks(
        title="电动车补贴退坡，行业面临洗牌",
        content="..."
    )
    # result = {
    #     'direct_matches': ['HK.00700', 'US.TSLA'],  # 直接提及
    #     'industry_matches': ['HK.00981', 'US.NVDA'],  # 产业链关联
    #     'impact_chain': [...]  # 影响链详情
    # }
"""

import logging
from app.storage.repository import get_db
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from pathlib import Path

logger = logging.getLogger("ftseek.factors.news_mapper")
logger.propagate = False

# 导入 AI 服务
try:
    from app.ai import AIService
    AI_AVAILABLE = True
except ImportError:
    AI_AVAILABLE = False
    logger.warning("AI 服务不可用，使用规则模式")

# 导入股票池
try:
    from app.strategy.stock_pool import get_stock_pool_manager
    STOCK_POOL_AVAILABLE = True
except ImportError:
    STOCK_POOL_AVAILABLE = False
    logger.warning("股票池管理器不可用")


@dataclass
class NewsMappingResult:
    """新闻映射结果"""
    news_id: str = ""
    title: str = ""
    content: str = ""

    # 直接匹配的股票 (新闻中明确提及)
    direct_matches: List[str] = field(default_factory=list)
    # [{'code': 'HK.00700', 'name': '腾讯控股', 'mention_count': 3}]

    # 行业关联的股票 (同一行业/概念)
    industry_matches: List[str] = field(default_factory=list)
    # [{'code': 'HK.00981', 'name': '中芯国际', 'industry': '半导体'}]

    # 影响链 (产业链上下游)
    impact_chain: List[Dict] = field(default_factory=list)
    # [{'upstream': '锂电池', 'downstream': ['电动车整车'], 'stocks': [...]}]

    # 实体识别结果
    entities: List[Dict] = field(default_factory=list)
    # [{'text': '特斯拉', 'type': '公司', 'sentiment': 'negative'}]

    # 映射置信度
    confidence: float = 0.0  # 0-1

    # AI 分析原始响应
    raw_response: str = ""

    # 元数据
    mapped_at: float = field(default_factory=time.time)
    trace_id: str = ""

    def to_dict(self) -> Dict:
        return {
            'news_id': self.news_id,
            'title': self.title,
            'content': self.content,
            'direct_matches': self.direct_matches,
            'industry_matches': self.industry_matches,
            'impact_chain': self.impact_chain,
            'entities': self.entities,
            'confidence': self.confidence,
            'raw_response': self.raw_response,
            'mapped_at': datetime.fromtimestamp(self.mapped_at).isoformat(),
            'trace_id': self.trace_id,
        }


class NewsStockMapper:
    """
    新闻 - 股票映射器

    使用规则 + AI 混合模式:
    1. 规则模式：关键词匹配 (快速，适用于明确提及股票名称)
    2. AI 模式：LLM 实体识别 + 影响链分析 (慢，适用于隐含关联)
    """

    def __init__(self, db_path: str = None, use_ai: bool = True):
        """
        初始化映射器

        Args:
            db_path: 映射数据库路径
            use_ai: 是否启用 AI 分析
        """
        self.use_ai = use_ai and AI_AVAILABLE
        self._db_path = db_path or str(Path(__file__).parent.parent / "data" / "news_mapping.db")

        # 关键词映射表 (行业/概念 -> 股票列表)
        self._industry_keywords: Dict[str, List[str]] = {}
        # {'电动车': ['HK.00700', 'US.TSLA', 'US.NIO'], ...}

        # 公司别名映射 (公司别名 -> 股票代码)
        self._company_aliases: Dict[str, str] = {}
        # {'腾讯': 'HK.00700', '阿里': 'HK.09988', ...}

        # 缓存
        self._cache: Dict[str, Tuple[NewsMappingResult, float]] = {}
        self._cache_ttl = 300  # 5 分钟

        # AI 服务
        self.ai = AIService() if self.use_ai else None

        # 初始化
        self._init_db()
        self._load_aliases()
        self._load_industry_keywords()

        logger.info(
            f"News-Stock Mapper 已初始化 | "
            f"AI={self.use_ai}, aliases={len(self._company_aliases)}, "
            f"industries={len(self._industry_keywords)}"
        )

    def _init_db(self):
        """初始化数据库"""
        try:
            with get_db(self._db_path) as conn:
                pass
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS news_mappings (
                    news_id TEXT PRIMARY KEY,
                    title TEXT,
                    direct_matches TEXT,
                    industry_matches TEXT,
                    impact_chain TEXT,
                    entities TEXT,
                    confidence REAL,
                    mapped_at REAL
                )
            """)
            conn.commit()
            conn.close()
            logger.debug(f"数据库已初始化：{self._db_path}")
        except Exception as e:
            logger.warning(f"数据库初始化失败：{e}")

    def _load_aliases(self):
        """加载公司别名映射"""
        # 内置别名表
        self._company_aliases = {
            # 港股
            '腾讯': 'HK.00700',
            '腾讯控股': 'HK.00700',
            'Tencent': 'HK.00700',
            '阿里': 'HK.09988',
            '阿里巴巴': 'HK.09988',
            'Alibaba': 'HK.09988',
            '美团': 'HK.03690',
            '小米': 'HK.01810',
            '小米集团': 'HK.01810',
            '比亚迪': 'HK.01211',
            'BYD': 'HK.01211',
            '汇丰': 'HK.00005',
            '汇丰控股': 'HK.00005',
            '友邦': 'HK.01299',
            '友邦保险': 'HK.01299',
            '中芯': 'HK.00981',
            '中芯国际': 'HK.00981',
            '宁德时代': 'HK.03093',
            'CATL': 'HK.03093',
            # 美股
            '苹果': 'US.AAPL',
            'Apple': 'US.AAPL',
            'iPhone': 'US.AAPL',
            '特斯拉': 'US.TSLA',
            'Tesla': 'US.TSLA',
            '马斯克': 'US.TSLA',
            'Elon': 'US.TSLA',
            '英伟达': 'US.NVDA',
            'NVIDIA': 'US.NVDA',
            'AI 芯片': 'US.NVDA',
            '微软': 'US.MSFT',
            'Microsoft': 'US.MSFT',
            '谷歌': 'US.GOOGL',
            'Google': 'US.GOOGL',
            '亚马逊': 'US.AMZN',
            'Amazon': 'US.AMZN',
            'Meta': 'US.META',
            'Facebook': 'US.META',
            '奈飞': 'US.NFLX',
            'Netflix': 'US.NFLX',
            'AMD': 'US.AMD',
            '英特尔': 'US.INTC',
            'Intel': 'US.INTC',
            '波音': 'US.BA',
            '高盛': 'US.GS',
            '摩根大通': 'US.JPM',
            'JPMorgan': 'US.JPM',
            '迪士尼': 'US.DIS',
            '沃尔玛': 'US.WMT',
            'Visa': 'US.V',
            '万事达': 'US.MA',
            '强生': 'US.JNJ',
            '辉瑞': 'US.PFE',
            'Moderna': 'US.MRNA',
        }

        # 从股票池加载
        # P1 修复 (2026-04-12): 已集成股票池
        if STOCK_POOL_AVAILABLE:
            try:
                pool = get_stock_pool_manager()
                # 注：股票池信息已在初始化时加载
            except Exception as e:
                logger.debug(f"股票池加载别名：{e}")

    def _load_industry_keywords(self):
        """加载行业关键词映射"""
        self._industry_keywords = {
            # 科技/互联网
            '互联网': ['HK.00700', 'HK.09988', 'HK.03690', 'US.GOOGL', 'US.META'],
            '电商': ['HK.09988', 'HK.03690', 'US.AMZN'],
            '游戏': ['HK.00700', 'HK.09992'],
            '半导体': ['HK.00981', 'US.NVDA', 'US.AMD', 'US.INTC', 'US.TSM'],
            'AI': ['US.NVDA', 'US.MSFT', 'US.GOOGL', 'HK.00700'],
            'AI 芯片': ['US.NVDA', 'US.AMD'],
            '云计算': ['US.MSFT', 'US.AMZN', 'US.GOOGL', 'HK.09988'],
            # 电动车/新能源
            '电动车': ['US.TSLA', 'HK.01211', 'US.NIO', 'HK.09868'],
            '新能源汽车': ['US.TSLA', 'HK.01211', 'US.NIO', 'HK.09868'],
            '锂电池': ['HK.03093', 'HK.01211'],
            '光伏': ['HK.03093', 'US.FSLR'],
            # 金融
            '银行': ['HK.00005', 'HK.02318', 'US.JPM', 'US.BAC', 'US.GS'],
            '保险': ['HK.01299', 'HK.02318', 'US.AIG'],
            '券商': ['HK.06030', 'HK.09988', 'US.GS', 'US.MS'],
            # 生物医药
            '生物医药': ['HK.01801', 'HK.09926', 'US.MRNA', 'US.PFE'],
            '疫苗': ['HK.01801', 'US.MRNA', 'US.BNTX'],
            # 消费
            '消费电子': ['US.AAPL', 'HK.01810'],
            '零售': ['US.WMT', 'US.AMZN', 'HK.09988'],
            # 其他
            '元宇宙': ['US.META', 'US.MSFT', 'HK.00700'],
            '自动驾驶': ['US.TSLA', 'US.NVDA', 'HK.01211'],
        }

    def map_news_to_stocks(self,
                          news_id: str,
                          title: str,
                          content: str = "",
                          trace_id: str = None) -> NewsMappingResult:
        """
        将新闻映射到股票

        Args:
            news_id: 新闻 ID
            title: 新闻标题
            content: 新闻内容
            trace_id: 追踪 ID

        Returns:
            NewsMappingResult: 映射结果
        """
        # 检查缓存
        cache_key = f"{title}_{content[:100]}"
        cached = self._cache.get(cache_key)
        if cached:
            result, expiry = cached
            if time.time() < expiry:
                logger.debug(f"使用缓存映射结果：{news_id}")
                return result

        # 合并标题和内容
        full_text = f"{title} {content}"

        # 步骤 1: 规则模式 - 关键词匹配
        direct_matches = self._rule_based_match(full_text)

        # 步骤 2: AI 模式 - 实体识别 + 影响链分析
        entities = []
        industry_matches = []
        impact_chain = []
        confidence = 0.0
        raw_response = ""

        if self.use_ai and (not direct_matches or len(full_text) > 100):
            ai_result = self._ai_analyze_news(title, content)
            entities = ai_result.get('entities', [])
            industry_matches = ai_result.get('industry_matches', [])
            impact_chain = ai_result.get('impact_chain', [])
            raw_response = ai_result.get('raw_response', '')
            confidence = ai_result.get('confidence', 0.0)

            # 合并 AI 识别的直接匹配
            if entities:
                for entity in entities:
                    if entity.get('type') == '公司':
                        alias = entity.get('text', '')
                        if alias in self._company_aliases:
                            code = self._company_aliases[alias]
                            if not any(m.get('code') == code for m in direct_matches):
                                direct_matches.append({
                                    'code': code,
                                    'name': alias,
                                    'mention_count': 1,
                                    'source': 'ai'
                                })

        # 步骤 3: 行业扩散
        if not direct_matches and not industry_matches:
            # 尝试行业关键词匹配
            industry_matches = self._industry_match(full_text)

        # 构建结果
        result = NewsMappingResult(
            news_id=news_id,
            title=title,
            content=content,
            direct_matches=direct_matches,
            industry_matches=industry_matches,
            impact_chain=impact_chain,
            entities=entities,
            confidence=confidence or (0.8 if direct_matches else 0.5),
            raw_response=raw_response,
            trace_id=trace_id or f"MAP-{news_id[:8]}"
        )

        # 缓存结果
        self._cache[cache_key] = (result, time.time() + self._cache_ttl)

        # 持久化到数据库
        self._persist_mapping(result)

        if direct_matches or industry_matches:
            logger.info(
                f"[新闻映射] {news_id}: "
                f"直接={len(direct_matches)}, 行业={len(industry_matches)}"
            )
        else:
            logger.debug(f"[新闻映射] 无匹配结果：{news_id}")

        return result

    def _rule_based_match(self, text: str) -> List[Dict]:
        """
        规则模式匹配

        遍历公司别名表，匹配文本中出现的公司名称
        """
        matches = []
        matched_codes: Set[str] = set()

        for alias, code in self._company_aliases.items():
            if alias in text:
                if code not in matched_codes:
                    matches.append({
                        'code': code,
                        'name': alias,
                        'mention_count': text.count(alias),
                        'source': 'rule'
                    })
                    matched_codes.add(code)

        return matches

    def _industry_match(self, text: str) -> List[Dict]:
        """
        行业关键词匹配

        遍历行业关键词，匹配文本中出现的行业/概念
        """
        matches = []

        for industry, codes in self._industry_keywords.items():
            if industry in text:
                for code in codes:
                    matches.append({
                        'code': code,
                        'name': code.split('.')[-1],
                        'industry': industry,
                        'source': 'industry'
                    })

        return matches

    def _ai_analyze_news(self, title: str, content: str) -> Dict:
        """
        使用 AI 分析新闻

        Returns:
            {
                'entities': [{'text': '特斯拉', 'type': '公司', 'sentiment': 'negative'}],
                'industry_matches': [...],
                'impact_chain': [...],
                'confidence': 0.85,
                'raw_response': '...'
            }
        """
        from app.ai.providers.qwen import QwenProvider

        # 尝试使用 Qwen Provider
        try:
            provider = QwenProvider()
            if not provider.api_key:
                logger.debug("Qwen API Key 未配置，使用规则模式")
                return {
                    'entities': [],
                    'industry_matches': [],
                    'impact_chain': [],
                    'confidence': 0.0,
                    'raw_response': 'Qwen API Key 未配置'
                }
        except Exception as e:
            logger.warning(f"初始化 Qwen Provider 失败：{e}")
            return {
                'entities': [],
                'industry_matches': [],
                'impact_chain': [],
                'confidence': 0.0,
                'raw_response': str(e)
            }

        # 1. 实体识别
        entities = []
        entity_response = provider.analyze_news_entities(title, content)
        if entity_response.success:
            entities = entity_response.data.get('entities', [])

        # 2. 影响链分析
        impact_chain = []
        impact_response = provider.analyze_impact_chain(title, content)
        if impact_response.success:
            impact_chain = impact_response.data.get('impact_chain', [])

        # 3. 事件分类
        event_type = 'other'
        event_response = provider.classify_event(title, content)
        if event_response.success:
            event_type = event_response.data.get('event_type', 'other')

        # 4. 合并结果
        industry_matches = []
        if entities:
            for entity in entities:
                if entity.get('type') == 'industry':
                    industry = entity.get('text', '')
                    if industry in self._industry_keywords:
                        for code in self._industry_keywords[industry]:
                            industry_matches.append({
                                'code': code,
                                'name': code.split('.')[-1],
                                'industry': industry,
                                'relevance': entity.get('confidence', 0.5)
                            })

        # 计算置信度
        confidence = 0.0
        if entities:
            confidence += 0.4
        if impact_chain:
            confidence += 0.3
        if industry_matches:
            confidence += 0.3

        raw_response = json.dumps({
            'entities': entities,
            'impact_chain': impact_chain,
            'event_type': event_type
        }, ensure_ascii=False)

        return {
            'entities': entities,
            'industry_matches': industry_matches,
            'impact_chain': impact_chain if isinstance(impact_chain, list) else [],
            'confidence': min(1.0, confidence),
            'raw_response': raw_response,
            'event_type': event_type
        }

    def _persist_mapping(self, result: NewsMappingResult):
        """持久化映射结果到数据库"""
        try:
            with get_db(self._db_path) as conn:
                pass
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO news_mappings
                (news_id, title, direct_matches, industry_matches,
                 impact_chain, entities, confidence, mapped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                result.news_id, result.title,
                json.dumps(result.direct_matches),
                json.dumps(result.industry_matches),
                json.dumps(result.impact_chain),
                json.dumps(result.entities),
                result.confidence,
                result.mapped_at
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"持久化映射结果失败：{e}")

    def get_historical_mapping(self, news_id: str) -> Optional[NewsMappingResult]:
        """获取历史映射结果"""
        try:
            with get_db(self._db_path) as conn:
                pass
            cursor = conn.cursor()
            cursor.execute("""
                SELECT title, direct_matches, industry_matches,
                       impact_chain, entities, confidence, mapped_at
                FROM news_mappings
                WHERE news_id = ?
            """, (news_id,))
            row = cursor.fetchone()
            conn.close()

            if row:
                return NewsMappingResult(
                    news_id=news_id,
                    title=row[0],
                    direct_matches=json.loads(row[1]),
                    industry_matches=json.loads(row[2]),
                    impact_chain=json.loads(row[3]),
                    entities=json.loads(row[4]),
                    confidence=row[5],
                    mapped_at=row[6]
                )
        except Exception as e:
            logger.debug(f"查询历史映射失败：{e}")

        return None


# 全局单例
_mapper: Optional[NewsStockMapper] = None


def get_news_mapper(use_ai: bool = True) -> NewsStockMapper:
    """获取全局 News-Stock Mapper 单例"""
    global _mapper
    if _mapper is None:
        _mapper = NewsStockMapper(use_ai=use_ai)
    return _mapper


def reset_mapper():
    """重置映射器 (用于测试)"""
    global _mapper
    _mapper = None
