"""
Livermore 板块龙头检测引擎 (Sector Leader Detector)

核心理念:
- "如果你不能从龙头股上赚钱，那你在整个牛市都赚不到钱。"
- "永远交易当前市场最强势的板块中最强势的股票。"
- "板块轮动时提前识别新龙头。"

实现方式:
1. 基于新闻密度的板块热度分析
2. 板块内个股相对强度排名 (vs SPY)
3. 成交量放大倍数检测
4. LLM 确认板块龙头

作者: FTSeek Team
日期: 2026-04-16
"""

import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.sector_leader")


@dataclass
class SectorInfo:
    """板块信息"""
    sector_name: str
    score: float                    # 板块热度得分 (0-100)
    news_count: int                 # 近期新闻数
    trend_direction: str            # "up" / "down" / "neutral"
    relative_strength: float        # 相对 SPY 的强度
    leaders: List[Dict[str, Any]]  # 龙头股列表 [{symbol, score, reason}]
    last_update: str = ""


# 美股板块分类映射
SECTOR_MAPPING = {
    # 科技
    "Technology": ["NVDA", "AAPL", "MSFT", "GOOG", "GOOGL", "META", "AMD", "INTC", "CRM", "ORCL"],
    # 半导体
    "Semiconductors": ["NVDA", "AMD", "INTC", "TSM", "AVGO", "QCOM", "MU", "TXN", "ASML"],
    # 消费
    "Consumer": ["AMZN", "TSLA", "NKE", "MCD", "SBUX", "HD", "WMT", "COST"],
    # 金融
    "Financials": ["JPM", "BAC", "GS", "MS", "V", "MA", "BRK-B", "C"],
    # 医疗
    "Healthcare": ["JNJ", "UNH", "PFE", "MRK", "ABBV", "LLY", "BMY"],
    # 能源
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "OXY", "MPC"],
    # 中概股
    "China_ADR": ["BABA", "PDD", "JD", "BIDU", "NIO", "XPEV", "LI", "TME", "BILI"],
    # 通信
    "Communication": ["GOOG", "GOOGL", "META", "NFLX", "DIS", "T", "VZ"],
}

# 板块关键词映射 (用于新闻匹配)
SECTOR_KEYWORDS = {
    "Technology": ["AI", "人工智能", "云计算", "软件", "SaaS", "大模型", "芯片"],
    "Semiconductors": ["芯片", "半导体", "GPU", "晶圆", "台积电", "Nvidia", "AMD", "光刻"],
    "Consumer": ["消费", "零售", "电商", "特斯拉", "Amazon", "苹果", "iPhone"],
    "Financials": ["银行", "金融", "利率", "美联储", "加息", "降息", "保险"],
    "Healthcare": ["医药", "医疗", "FDA", "生物", "制药", "疫苗", "减肥药"],
    "Energy": ["原油", "油价", "石油", "能源", "天然气", "OPEC", "页岩油"],
    "China_ADR": ["中国", "中概", "阿里", "拼多多", "京东", "蔚来", "小鹏", "理想", "港股"],
    "Communication": ["流媒体", "Netflix", "迪士尼", "5G", "电信", "Meta", "TikTok"],
}


class SectorLeaderDetector:
    """
    Livermore 板块龙头检测引擎
    
    职责:
    1. 分析新闻密度，识别当前热点板块
    2. 计算板块内个股相对强度
    3. 识别板块龙头股
    4. 检测板块轮动信号
    """
    
    def __init__(self):
        self._sector_scores: Dict[str, SectorInfo] = {}
        self._news_history: List[Dict] = []
        self._last_detection: Optional[datetime] = None
        logger.info(f"[板块龙头] 初始化 | 板块数={len(SECTOR_MAPPING)}")
    
    def analyze_sectors(self, news_items: List[Dict], 
                        price_data: Dict[str, Dict] = None) -> Dict[str, SectorInfo]:
        """
        分析当前热点板块
        
        Args:
            news_items: 新闻列表 (from RSSHub)
            price_data: 个股价格数据 {symbol: {price, change_pct, volume_ratio}}
        
        Returns:
            板块信息字典
        """
        now = datetime.now()
        self._last_detection = now
        
        # 1. 基于新闻密度的板块热度
        sector_news_count = self._count_sector_news(news_items)
        
        # 2. 计算板块得分
        for sector_name, symbols in SECTOR_MAPPING.items():
            news_count = sector_news_count.get(sector_name, 0)
            keywords = SECTOR_KEYWORDS.get(sector_name, [])
            
            # 基础得分: 新闻密度 (0-50 分)
            news_score = min(50, news_count * 5)
            
            # 价格动量得分 (0-30 分)
            momentum_score = 0
            relative_strength = 0.0
            leaders = []
            
            if price_data:
                sector_prices = {s: price_data.get(s, {}) for s in symbols if s in price_data}
                if sector_prices:
                    avg_change = sum(
                        p.get('change_pct', 0) for p in sector_prices.values()
                    ) / len(sector_prices)
                    momentum_score = min(30, max(0, (avg_change + 0.02) * 300))
                    relative_strength = avg_change
                    
                    # 识别龙头: 板块内涨幅最高的股票
                    sorted_symbols = sorted(
                        sector_prices.items(),
                        key=lambda x: x[1].get('change_pct', 0),
                        reverse=True
                    )
                    leaders = [
                        {
                            "symbol": f"US.{sym}",
                            "change_pct": data.get('change_pct', 0) * 100,
                            "volume_ratio": data.get('volume_ratio', 1.0),
                            "score": data.get('change_pct', 0) * 100 + data.get('volume_ratio', 1.0) * 10,
                        }
                        for sym, data in sorted_symbols[:3]
                    ]
            
            # LLM 确认得分 (0-20 分) - 基于关键词匹配
            llm_score = self._llm_sector_confirmation(news_items, keywords, sector_name)
            
            total_score = news_score + momentum_score + llm_score
            
            # 趋势方向
            if relative_strength > 0.01:
                trend = "up"
            elif relative_strength < -0.01:
                trend = "down"
            else:
                trend = "neutral"
            
            self._sector_scores[sector_name] = SectorInfo(
                sector_name=sector_name,
                score=round(total_score, 1),
                news_count=news_count,
                trend_direction=trend,
                relative_strength=round(relative_strength, 4),
                leaders=leaders,
                last_update=now.isoformat(),
            )
        
        return self._sector_scores
    
    def get_top_sectors(self, n: int = 3) -> List[SectorInfo]:
        """获取热度最高的 N 个板块"""
        if not self._sector_scores:
            return []
        
        sorted_sectors = sorted(
            self._sector_scores.values(),
            key=lambda x: x.score,
            reverse=True
        )
        return sorted_sectors[:n]
    
    def get_sector_leaders(self, sector_name: str = None) -> List[Dict[str, Any]]:
        """
        获取板块龙头股
        
        Args:
            sector_name: 指定板块名, None 返回所有热点板块龙头
        
        Returns:
            龙头股列表 [{sector, symbol, score, reason}]
        """
        leaders = []
        
        if sector_name and sector_name in self._sector_scores:
            sectors = [self._sector_scores[sector_name]]
        else:
            # 取热度前 3 的板块
            sectors = self.get_top_sectors(3)
        
        for sector in sectors:
            for leader in sector.leaders:
                leaders.append({
                    "sector": sector.sector_name,
                    "symbol": leader["symbol"],
                    "score": round(leader.get("score", 0), 1),
                    "change_pct": round(leader.get("change_pct", 0), 2),
                    "volume_ratio": round(leader.get("volume_ratio", 1.0), 1),
                    "sector_score": sector.score,
                })
        
        # 按综合得分排序
        leaders.sort(key=lambda x: x.get("score", 0), reverse=True)
        return leaders[:5]
    
    def get_detection_summary(self) -> Dict[str, Any]:
        """获取检测摘要 (供 LLM prompt 注入)"""
        top_sectors = self.get_top_sectors(3)
        top_leaders = self.get_sector_leaders()
        
        sector_text = ""
        for s in top_sectors:
            sector_text += f"- {s.sector_name}: 热度{s.score}/100, 趋势{s.trend_direction}, 新闻{s.news_count}条\n"
        
        leaders_text = ""
        for l in top_leaders:
            leaders_text += f"- {l['symbol']} ({l['sector']}): 涨幅{l['change_pct']}%, 量比{l['volume_ratio']}x\n"
        
        return {
            "top_sectors": sector_text if sector_text else "暂无热点板块",
            "top_leaders": leaders_text if leaders_text else "暂无龙头股",
            "detection_time": self._last_detection.isoformat() if self._last_detection else "未检测",
        }
    
    def _count_sector_news(self, news_items: List[Dict]) -> Dict[str, int]:
        """统计各板块的新闻数量"""
        counts = {name: 0 for name in SECTOR_MAPPING}
        
        for news in news_items:
            title = (news.get('title', '') + ' ' + news.get('summary', '')).lower()
            
            for sector_name, keywords in SECTOR_KEYWORDS.items():
                if any(kw.lower() in title for kw in keywords):
                    counts[sector_name] += 1
        
        return counts
    
    def _llm_sector_confirmation(self, news_items: List[Dict], 
                                  keywords: List[str], 
                                  sector_name: str) -> float:
        """
        基于新闻关键词匹配度给予 LLM 确认得分
        
        简化实现: 基于关键词匹配密度
        """
        if not news_items or not keywords:
            return 0.0
        
        match_count = 0
        total_news = len(news_items)
        
        for news in news_items:
            text = (news.get('title', '') + ' ' + news.get('summary', '')).lower()
            if any(kw.lower() in text for kw in keywords):
                match_count += 1
        
        # 匹配比例映射到 0-20 分
        match_ratio = match_count / total_news if total_news > 0 else 0
        return min(20, match_ratio * 40)


# 全局单例
_detector_instance: Optional[SectorLeaderDetector] = None

def get_sector_leader_detector() -> SectorLeaderDetector:
    """获取全局板块龙头检测引擎单例"""
    global _detector_instance
    if _detector_instance is None:
        _detector_instance = SectorLeaderDetector()
    return _detector_instance
