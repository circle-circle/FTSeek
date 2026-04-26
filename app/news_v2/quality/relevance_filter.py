"""
FTSeek 新闻相关性过滤器

判断新闻是否与 watchlist 或持仓相关。
只有相关的新闻才进入后续处理。
"""

import re
import logging
from typing import List, Dict, Set, Optional
from app.news_v2.models import UnifiedNewsItemV2

logger = logging.getLogger("ftseek.news_v2.quality")


class RelevanceFilter:
    """
    新闻相关性过滤
    
    规则:
    1. 股票代码匹配 (HK.XXXXX, US.XXXX)
    2. 股票名称匹配 (腾讯, 茅台, Apple)
    3. 宏观指标匹配 (CPI, 非农, VIX)
    4. 板块/行业匹配 (半导体, 新能源, 银行)
    """
    
    # 宏观指标关键词 (即使没有股票也放行)
    MACRO_KEYWORDS = [
        "CPI", "PPI", "非农", "失业率", "GDP", "PMI", "VIX", "恐慌指数",
        "美联储", "FOMC", "加息", "降息", "利率决议", "缩表", "扩表",
        "通胀", "通缩", "央行", "PBOC", "ECB",
        "贸易战", "关税", "制裁", "制裁",
    ]
    
    # 板块关键词
    SECTOR_KEYWORDS = {
        "科技": ["科技", "半导体", "芯片", "AI", "人工智能", "云计算", "软件", "互联网", "NVDA", "AAPL", "MSFT"],
        "能源": ["能源", "石油", "原油", "天然气", "煤炭", "新能源", "光伏", "风电", "XOM", "中海油", "中石油"],
        "金融": ["银行", "保险", "券商", "金融", "券商", "JP Morgan", "高盛", "摩根"],
        "消费": ["消费", "零售", "电商", "拼多多", "阿里", "京东", "AMZN"],
        "医药": ["医药", "制药", "疫苗", "FDA", "生物科技"],
        "房地产": ["房地产", "房企", "碧桂园", "恒大", "万科"],
        "航空": ["航空", "国航", "南航", "东航", "航司", "机票"],
        "黄金": ["黄金", "金价", "紫金", "招金", "Gold"],
    }
    
    def __init__(self, watchlist_codes: Set[str] = None, 
                 watchlist_names: Set[str] = None):
        self.watchlist_codes = watchlist_codes or set()
        self.watchlist_names = watchlist_names or set()
        self._build_patterns()
    
    def _build_patterns(self):
        """预编译正则表达式"""
        self._stock_patterns = []
        # HK.XXXXX 格式
        self._stock_patterns.append(re.compile(r"HK\.\d{5}", re.I))
        # US.XXXX 格式
        self._stock_patterns.append(re.compile(r"US\.[A-Z]{1,5}", re.I))
        # 纯数字港股代码
        self._stock_patterns.append(re.compile(r"\b\d{5}\b"))
    
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
        codes_found = []
        for pat in self._stock_patterns:
            codes_found.extend(pat.findall(text))
        codes_found = list(set(codes_found))
        
        for code in codes_found:
            normalized = code.upper()
            if normalized.startswith("HK.") or len(normalized) == 5:
                if normalized in self.watchlist_codes or any(c.startswith(normalized.rstrip("0")) for c in self.watchlist_codes):
                    score += 0.5
                    reasons.append(f"watchlist 股票: {normalized}")
                else:
                    score += 0.2
                    reasons.append(f"股票: {normalized}")
            elif normalized.startswith("US."):
                if normalized in self.watchlist_codes:
                    score += 0.5
                    reasons.append(f"watchlist 美股: {normalized}")
                else:
                    score += 0.2
                    reasons.append(f"美股: {normalized}")
        
        # 2. 股票名称匹配
        for name in self.watchlist_names:
            if name.lower() in text:
                score += 0.5
                reasons.append(f"watchlist 公司: {name}")
                break
        
        # 3. 宏观指标
        for kw in self.MACRO_KEYWORDS:
            if kw.lower() in text:
                score += 0.3
                reasons.append(f"宏观指标: {kw}")
                break
        
        # 4. 板块匹配
        matched_sectors = []
        for sector, keywords in self.SECTOR_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in text:
                    matched_sectors.append(sector)
                    score += 0.2
                    break
        
        if matched_sectors:
            reasons.append(f"板块: {', '.join(matched_sectors)}")
        
        # 归一化到 0-1
        score = min(1.0, score)
        item.raw_score = max(item.raw_score, score)
        
        return score
    
    def is_relevant(self, item: UnifiedNewsItemV2, threshold: float = 0.2) -> bool:
        """是否相关"""
        return self.check_relevance(item) >= threshold
