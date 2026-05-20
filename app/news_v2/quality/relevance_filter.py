"""
FTSeek-US 新闻相关性过滤器

判断新闻是否与 watchlist 或持仓相关。
只有相关的新闻才进入后续处理。

美股特有: 宏观指标 (CPI/NFP/FOMC/VIX) 作为一等相关指标
"""

import re
import logging
from typing import List, Dict, Set, Optional
from app.news_v2.models import UnifiedNewsItemV2

logger = logging.getLogger("ftseek_us.news_v2.quality")


class RelevanceFilter:
    """
    新闻相关性过滤 (美股版)
    
    规则:
    1. 股票代码匹配 (US.XXXX)
    2. 公司名称匹配 (Tesla, NVIDIA, Apple)
    3. 宏观指标匹配 (CPI, NFP, FOMC, VIX) — 一等指标
    4. 板块/行业匹配 (Semiconductors, Energy, Financials)
    """
    
    # 美股宏观指标关键词 (一等指标，即使无股票也放行)
    MACRO_KEYWORDS = [
        "cpi", "ppi", "nonfarm", "non-farm", "unemployment", "gdp", "pmi",
        "vix", "fear index", "fomc", "federal reserve", "fed rate", "rate hike",
        "rate cut", "rate decision", "quantitative tightening", "qe", "qt",
        "inflation", "deflation", "treasury yield", "bond yield",
        "tariff", "trade war", "sanction",
        "payrolls", "jobs report", "initial claims",
        "retail sales", "consumer confidence", "housing starts",
        "powell", "jerome powell",
    ]
    
    # 美股板块关键词
    SECTOR_KEYWORDS = {
        "Technology": [
            "semiconductor", "chip", "ai ", "artificial intelligence", "cloud computing",
            "software", "internet", "cybersecurity", "saas", "nvda", "nvidia", "aapl",
            "apple", "msft", "microsoft", "goog", "google", "meta", "amd", "tsm",
            "台积电", "半导体", "芯片",
        ],
        "Energy": [
            "energy", "oil", "crude", "natural gas", "petroleum", "opec", "xom",
            "exxon", "cvx", "chevron", "cop", "conoco", "slb", "schlumberger",
            "原油", "石油", "能源", "opec",
        ],
        "Financials": [
            "bank", "insurance", "broker", "jpmorgan", "jp morgan", "goldman", "gs",
            "morgan stanley", "ms", "bofa", "bank of america", "wells fargo",
            "citi", "blackrock", "xlf", "financial",
            "银行", "金融", "高盛", "摩根",
        ],
        "Consumer Discretionary": [
            "consumer", "retail", "e-commerce", "amazon", "amzn", "tesla", "tsla",
            "walmart", "wmt", "nike", "mcdonald", "disney", "dis", "xly",
            "零售", "电商", "消费",
        ],
        "Healthcare": [
            "healthcare", "pharma", "pharmaceutical", "fda", "biotech", "biomedical",
            "vaccine", "unh", "unitedhealth", "jnj", "johnson", "pfe", "pfizer",
            "lly", "eli lilly", "xlv",
            "医药", "制药", "疫苗", "生物科技",
        ],
        "Industrials": [
            "industrial", "aerospace", "defense", "boeing", "ba", "caterpillar",
            "cat", "ge", "honeywell", "xli",
        ],
        "Materials": [
            "materials", "mining", "steel", "chemical", "xlb", "freeport", "fmc",
        ],
        "Real Estate": [
            "real estate", "reit", "property", "xlre",
            "房地产",
        ],
        "Utilities": [
            "utility", "electric", "power", "xlu",
        ],
        "Communication": [
            "telecom", "communication", "t-mobile", "tmus", "comcast", "cmcsa",
            "netflix", "nflx", "xlc",
        ],
        "China ADR": [
            "baba", "alibaba", "pdd", "pinduoduo", "jd", "jd.com", "nio", "xpev", "li",
            "bidu", "baidu", "tme", "tencent music", "bilibili", "bILI", "netease", "ntes",
            "中概股", "中概", "阿里巴巴", "拼多多", "京东", "百度", "蔚来",
        ],
    }
    
    # 地缘政治传导路径 (美股特有)
    GEO_TRANSMISSION = {
        "oil_surge": {
            "keywords": ["oil price", "crude surge", "opec cut", "middle east", "iran", "saudi"],
            "beneficiaries": ["XOM", "CVX", "COP", "SLB", "OXY"],
            "victims": ["DAL", "UAL", "LUV", "AAL"],
        },
        "china_tension": {
            "keywords": ["china tension", "taiwan", "south china sea", "china sanction"],
            "victims": ["BABA", "PDD", "JD", "NIO", "TSM", "BIDU"],
        },
        "fed_policy": {
            "keywords": ["fed rate", "fomc", "powell", "rate hike", "rate cut"],
            "affects_all": True,
        },
    }
    
    def __init__(self, watchlist_codes: Set[str] = None, 
                 watchlist_names: Set[str] = None):
        self.watchlist_codes = watchlist_codes or set()
        self.watchlist_names = watchlist_names or set()
        self._build_patterns()
    
    def _build_patterns(self):
        """预编译正则表达式"""
        self._stock_patterns = []
        # US.XXXX 格式 (1-5 字母)
        self._stock_patterns.append(re.compile(r"US\.[A-Z]{1,5}", re.I))
        # 纯美股代码 (大写 1-5 字母，前后空格或标点)
        self._stock_patterns.append(re.compile(r"\b[A-Z]{1,5}\b"))
    
    def check_relevance(self, item: UnifiedNewsItemV2) -> float:
        """
        检查新闻相关性
        
        Returns:
            相关性分数 (0-1)
            0 = 不相关, 1 = 高度相关
        """
        text = f"{item.title} {item.content[:500]}".lower()
        score = 0.0
        reasons = []
        
        # 1. 股票代码匹配
        codes_found = set()
        for pat in self._stock_patterns:
            codes_found.update(pat.findall(text))
        
        for code in codes_found:
            normalized = code.upper()
            # 如果是 US.XXXX 格式
            if normalized.startswith("US."):
                ticker = normalized[3:]
                if f"US.{ticker}" in self.watchlist_codes:
                    score += 0.5
                    reasons.append(f"watchlist stock: {normalized}")
                else:
                    score += 0.2
                    reasons.append(f"stock: {normalized}")
            # 如果是纯 ticker (2-5 字母)
            elif len(normalized) >= 2 and len(normalized) <= 5:
                if f"US.{normalized}" in self.watchlist_codes:
                    score += 0.5
                    reasons.append(f"watchlist ticker: {normalized}")
                else:
                    score += 0.15
                    reasons.append(f"ticker: {normalized}")
        
        # 2. 公司名称匹配
        for name in self.watchlist_names:
            if name.lower() in text:
                score += 0.5
                reasons.append(f"watchlist company: {name}")
                break
        
        # 3. 宏观指标 (一等指标)
        for kw in self.MACRO_KEYWORDS:
            if kw in text:
                score += 0.35
                reasons.append(f"macro indicator: {kw}")
                break
        
        # 4. 板块匹配
        matched_sectors = []
        for sector, keywords in self.SECTOR_KEYWORDS.items():
            for kw in keywords:
                if kw in text:
                    matched_sectors.append(sector)
                    score += 0.2
                    break
        
        if matched_sectors:
            reasons.append(f"sectors: {', '.join(matched_sectors)}")
        
        # 5. 地缘政治传导路径匹配
        for path_name, path_info in self.GEO_TRANSMISSION.items():
            for kw in path_info["keywords"]:
                if kw in text:
                    score += 0.15
                    reasons.append(f"geo transmission: {path_name}")
                    break
        
        # 6. 已有 related_stocks (数据源已标注)
        if item.related_stocks:
            score += 0.3
            reasons.append(f"source-tagged stocks: {len(item.related_stocks)}")
        
        # 归一化到 0-1
        score = min(1.0, score)
        item.raw_score = max(item.raw_score, score)
        
        return score
    
    def is_relevant(self, item: UnifiedNewsItemV2, threshold: float = 0.2) -> bool:
        """是否相关"""
        return self.check_relevance(item) >= threshold
