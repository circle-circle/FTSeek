#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟新闻生成器 - 基于历史股价波动反推新闻

功能:
1. 分析历史大涨/大跌日期
2. 生成对应"假设新闻" (大涨→利好，大跌→利空)
3. 模拟新闻延迟 (早于交易时间 5-30 分钟)
4. 多源模拟 (每条新闻 2-4 个来源)
5. 情感评分与价格波动方向一致

输出格式:
{
    "generated_at": "2026-04-08T08:00:00",
    "stocks": {
        "00700.HK": [
            {
                "id": "mock_00700_20260103_001",
                "title": "腾讯控股发布超预期财报，净利润同比增长 25%",
                "content": "...",
                "date": "2026-01-03",
                "publish_time": "2026-01-03T08:45:00",
                "stocks": ["00700.HK"],
                "sentiment": "positive",
                "sentiment_score": 0.75,
                "confidence": 0.85,
                "importance": "major",
                "sources": ["jinshi", "sina", "cls"],
                "price_change": 0.062,
                "keywords": ["财报", "业绩", "盈利"]
            }
        ]
    }
}

验收标准:
- 模拟新闻覆盖率≥80% (有价格波动的日期都有新闻)
- 情感评分与价格方向一致
- 新闻延迟 5-30 分钟
- 每条新闻 2-4 个来源
"""

import csv
import json
import logging
import os
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('mock_news_generator')


# ==================== 新闻模板库 ====================

# 利好新闻模板 (涨幅>5%)
POSITIVE_NEWS_TEMPLATES = [
    {
        "title": "{stock_name}发布超预期财报，净利润同比增长{growth}%",
        "content": "{stock_name}今日发布最新财报，显示公司净利润同比增长{growth}%，超出市场预期。分析师认为，公司核心业务增长强劲，未来发展前景良好。",
        "keywords": ["财报", "业绩", "盈利", "增长"],
        "importance": "major"
    },
    {
        "title": "{stock_name}签署重大战略合作协议，预计年增收{revenue}亿元",
        "content": "{stock_name}宣布与行业领先企业签署战略合作协议，预计将为公司带来年增收{revenue}亿元。市场对此反应积极，股价大幅上涨。",
        "keywords": ["合同", "订单", "合作", "战略"],
        "importance": "major"
    },
    {
        "title": "{stock_name}新产品发布，市场反响热烈",
        "content": "{stock_name}今日发布全新产品系列，市场反响热烈，预售量远超预期。分析师上调目标价，看好公司长期发展。",
        "keywords": ["产品", "创新", "技术", "突破"],
        "importance": "normal"
    },
    {
        "title": "{stock_name}获多家机构上调评级，目标价看高{target}%",
        "content": "多家知名投行今日上调{stock_name}评级，认为公司估值合理，未来上涨空间达{target}%。资金持续流入，股价表现强劲。",
        "keywords": ["评级", "投行", "目标价"],
        "importance": "normal"
    },
    {
        "title": "{stock_name}回购股份彰显信心，耗资{buyback}亿港元",
        "content": "{stock_name}宣布回购公司股份，耗资{buyback}亿港元。管理层表示，回购显示对公司未来发展的坚定信心。",
        "keywords": ["回购", "股份", "信心"],
        "importance": "normal"
    },
    {
        "title": "{stock_name}市场份额持续提升，行业地位稳固",
        "content": "最新数据显示，{stock_name}市场份额持续提升，行业龙头地位进一步稳固。分析师认为公司竞争优势明显，长期价值凸显。",
        "keywords": ["市场", "份额", "行业", "龙头"],
        "importance": "normal"
    }
]

# 利空新闻模板 (跌幅>5%)
NEGATIVE_NEWS_TEMPLATES = [
    {
        "title": "{stock_name}财报不及预期，净利润同比下滑{decline}%",
        "content": "{stock_name}发布最新财报，显示公司净利润同比下滑{decline}%，低于市场预期。分析师担忧公司核心业务增长放缓，股价承压。",
        "keywords": ["财报", "业绩", "亏损", "下滑"],
        "importance": "major"
    },
    {
        "title": "{stock_name}遭监管调查，股价大幅下跌",
        "content": "据报道，{stock_name}正接受监管部门调查，涉及业务合规性问题。市场担忧可能面临处罚，股价应声下跌。",
        "keywords": ["监管", "处罚", "调查", "合规"],
        "importance": "major"
    },
    {
        "title": "{stock_name}核心高管辞职，市场担忧战略稳定性",
        "content": "{stock_name}今日公告，公司核心高管因个人原因辞职。市场担忧公司战略稳定性受影响，投资者情绪谨慎。",
        "keywords": ["高管", "辞职", "管理层"],
        "importance": "normal"
    },
    {
        "title": "{stock_name}重要客户流失，营收恐受影响",
        "content": "据悉，{stock_name}失去重要大客户订单，预计将对下季度营收产生负面影响。分析师下调盈利预测。",
        "keywords": ["客户", "订单", "营收"],
        "importance": "normal"
    },
    {
        "title": "{stock_name}遭机构下调评级，目标价看低{target}%",
        "content": "多家投行今日下调{stock_name}评级，认为公司估值偏高，未来可能下跌{target}%。资金流出明显，股价承压。",
        "keywords": ["评级", "投行", "目标价"],
        "importance": "normal"
    },
    {
        "title": "{stock_name}行业竞争加剧，毛利率持续下滑",
        "content": "最新财报显示，{stock_name}毛利率持续下滑，主要受行业竞争加剧影响。分析师担忧公司盈利能力进一步恶化。",
        "keywords": ["竞争", "毛利率", "盈利"],
        "importance": "normal"
    }
]

# 中性新闻模板 (波动<2%)
NEUTRAL_NEWS_TEMPLATES = [
    {
        "title": "{stock_name}日常经营公告，无重大事项",
        "content": "{stock_name}发布日常经营公告，公司运营正常，无重大事项披露。股价波动较小，成交量平稳。",
        "keywords": ["公告", "日常", "经营"],
        "importance": "minor"
    },
    {
        "title": "{stock_name}股东例会召开，讨论常规议题",
        "content": "{stock_name}今日召开股东例会，讨论公司常规经营议题。无重大决策公布，市场反应平淡。",
        "keywords": ["股东", "例会", "常规"],
        "importance": "minor"
    }
]

# 股票名称映射
STOCK_NAMES = {
    "00700.HK": "腾讯控股",
    "09988.HK": "阿里巴巴",
    "03690.HK": "美团",
    "00941.HK": "中国移动",
    "01810.HK": "小米集团",
    "00883.HK": "中海油",
    "00001.HK": "长江实业",
    "00005.HK": "汇丰控股",
    "00388.HK": "港交所",
    "00175.HK": "吉利汽车",
    "00981.HK": "中芯国际",
    "01024.HK": "快手",
    "00027.HK": "银河娱乐",
    "00857.HK": "中国石油",
    "00386.HK": "中国石化",
    "02318.HK": "中国平安",
    "09866.HK": "蔚来",
    "02020.HK": "安踏体育",
    "01211.HK": "比亚迪股份"
}

# 新闻源列表
NEWS_SOURCES = ["jinshi", "sina", "tencent", "cls"]


@dataclass
class MockNewsItem:
    """模拟新闻项"""
    id: str
    title: str
    content: str
    date: str
    publish_time: str
    stocks: List[str]
    sentiment: str  # positive/negative/neutral
    sentiment_score: float  # -1.0 ~ 1.0
    confidence: float  # 0 ~ 1
    importance: str  # major/normal/minor
    sources: List[str]
    price_change: float
    keywords: List[str]
    
    def to_dict(self) -> dict:
        return asdict(self)


class MockNewsGenerator:
    """
    模拟新闻生成器
    
    功能:
    1. 加载历史股价数据
    2. 分析每日涨跌幅
    3. 生成对应模拟新闻
    4. 输出 JSON 格式数据
    """
    
    def __init__(
        self,
        data_dir: str = "/opt/ftseek/FTSeek/data/historical_data",
        output_dir: str = "/opt/ftseek/FTSeek/data"
    ):
        """
        初始化生成器
        
        Args:
            data_dir: 历史数据目录
            output_dir: 输出目录
        """
        self.data_dir = Path(data_dir)
        self.output_dir = Path(output_dir)
        
        # 阈值配置
        self.positive_threshold = 0.03   # 涨幅>3% → 利好
        self.negative_threshold = -0.03  # 跌幅>3% → 利空
        self.neutral_threshold = 0.01    # 波动>1% → 可能生成中性新闻
        
        # 新闻延迟配置 (分钟)
        self.min_delay = 5
        self.max_delay = 30
        
        # 来源数量配置
        self.min_sources = 2
        self.max_sources = 4
        
        logger.info("MockNewsGenerator initialized")
    
    def load_price_data(self, stock_code: str) -> List[dict]:
        """
        加载股票历史价格数据
        
        Args:
            stock_code: 股票代码 (如：00700.HK)
            
        Returns:
            List[dict]: 价格数据列表
        """
        # 转换股票代码格式 (00700.HK → HK_00700_DAY.csv)
        code_part = stock_code.split('.')[0].zfill(5)
        filename = f"HK_{code_part}_DAY.csv"
        filepath = self.data_dir / filename
        
        if not filepath.exists():
            logger.warning(f"Price data not found: {filepath}")
            return []
        
        data = []
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    data.append({
                        'date': row['time_key'].split()[0],
                        'open': float(row['open']),
                        'close': float(row['close']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'volume': int(row['volume']),
                        'turnover': float(row['turnover'])
                    })
                except (ValueError, KeyError) as e:
                    logger.warning(f"Failed to parse row: {e}")
        
        logger.info(f"Loaded {len(data)} days of data for {stock_code}")
        return data
    
    def calculate_price_change(self, data: List[dict]) -> List[Tuple[str, float]]:
        """
        计算每日价格变化
        
        Args:
            data: 价格数据
            
        Returns:
            List[Tuple[str, float]]: (日期，涨跌幅) 列表
        """
        changes = []
        
        for i in range(1, len(data)):
            prev_close = data[i-1]['close']
            curr_close = data[i]['close']
            change = (curr_close - prev_close) / prev_close
            date = data[i]['date']
            changes.append((date, change))
        
        return changes
    
    def generate_news_for_day(
        self,
        stock_code: str,
        date: str,
        price_change: float
    ) -> Optional[MockNewsItem]:
        """
        为指定日期生成模拟新闻
        
        Args:
            stock_code: 股票代码
            date: 日期
            price_change: 价格变化
            
        Returns:
            Optional[MockNewsItem]: 新闻项 (如无新闻则返回 None)
        """
        stock_name = STOCK_NAMES.get(stock_code, stock_code)
        
        # 确定新闻类型
        if price_change > self.positive_threshold:
            # 利好新闻
            template = random.choice(POSITIVE_NEWS_TEMPLATES)
            sentiment = "positive"
            sentiment_score = min(0.5 + price_change, 0.95)  # 0.5~0.95
            importance = template["importance"]
            
            # 填充模板变量
            growth = int(price_change * 100 * 2)  # 模拟增长率
            revenue = random.randint(10, 100)
            target = random.randint(15, 30)
            buyback = random.randint(5, 50)
            
            title = template["title"].format(
                stock_name=stock_name,
                growth=growth,
                revenue=revenue,
                target=target,
                buyback=buyback
            )
            content = template["content"].format(
                stock_name=stock_name,
                growth=growth,
                revenue=revenue,
                target=target,
                buyback=buyback
            )
            
        elif price_change < self.negative_threshold:
            # 利空新闻
            template = random.choice(NEGATIVE_NEWS_TEMPLATES)
            sentiment = "negative"
            sentiment_score = max(-0.5 + price_change, -0.95)  # -0.95~-0.5
            importance = template["importance"]
            
            # 填充模板变量
            decline = int(abs(price_change) * 100 * 2)  # 模拟下滑率
            target = random.randint(15, 30)
            
            title = template["title"].format(
                stock_name=stock_name,
                decline=decline,
                target=target
            )
            content = template["content"].format(
                stock_name=stock_name,
                decline=decline,
                target=target
            )
            
        elif abs(price_change) > self.neutral_threshold:
            # 小幅波动新闻 (80% 概率生成)
            if random.random() < 0.8:
                template = random.choice(NEUTRAL_NEWS_TEMPLATES)
                sentiment = "neutral"
                sentiment_score = random.uniform(-0.1, 0.1)
                importance = template["importance"]
                
                title = template["title"].format(stock_name=stock_name)
                content = template["content"].format(stock_name=stock_name)
            else:
                return None
        else:
            # 无显著波动，不生成新闻
            return None
        
        # 生成新闻发布时间 (早于交易时间 5-30 分钟)
        # 假设交易时间 09:30 开盘
        trade_start = datetime.strptime(f"{date} 09:30:00", "%Y-%m-%d %H:%M:%S")
        delay_minutes = random.randint(self.min_delay, self.max_delay)
        publish_time = trade_start - timedelta(minutes=delay_minutes)
        
        # 生成新闻 ID
        news_id = f"mock_{stock_code.replace('.', '')}_{date.replace('-', '')}_{random.randint(100, 999)}"
        
        # 选择新闻源 (2-4 个)
        num_sources = random.randint(self.min_sources, self.max_sources)
        sources = random.sample(NEWS_SOURCES, num_sources)
        
        # 计算置信度 (与价格变化幅度正相关)
        confidence = min(0.7 + abs(price_change) * 2, 0.95)
        
        news_item = MockNewsItem(
            id=news_id,
            title=title,
            content=content,
            date=date,
            publish_time=publish_time.strftime("%Y-%m-%dT%H:%M:%S"),
            stocks=[stock_code],
            sentiment=sentiment,
            sentiment_score=round(sentiment_score, 2),
            confidence=round(confidence, 2),
            importance=importance,
            sources=sources,
            price_change=round(price_change, 4),
            keywords=template["keywords"]
        )
        
        return news_item
    
    def generate_for_stock(
        self,
        stock_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> List[MockNewsItem]:
        """
        为指定股票生成模拟新闻
        
        Args:
            stock_code: 股票代码
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            
        Returns:
            List[MockNewsItem]: 新闻列表
        """
        # 加载价格数据
        price_data = self.load_price_data(stock_code)
        
        if not price_data:
            return []
        
        # 计算价格变化
        price_changes = self.calculate_price_change(price_data)
        
        # 日期过滤
        if start_date:
            price_changes = [(d, c) for d, c in price_changes if d >= start_date]
        if end_date:
            price_changes = [(d, c) for d, c in price_changes if d <= end_date]
        
        # 生成新闻
        news_items = []
        for date, change in price_changes:
            news = self.generate_news_for_day(stock_code, date, change)
            if news:
                news_items.append(news)
        
        # 计算覆盖率
        volatile_days = sum(
            1 for _, c in price_changes 
            if abs(c) > self.neutral_threshold
        )
        coverage = len(news_items) / volatile_days if volatile_days > 0 else 0
        
        logger.info(
            f"Generated {len(news_items)} news for {stock_code}, "
            f"coverage: {coverage:.1%} ({volatile_days} volatile days)"
        )
        
        return news_items
    
    def generate_for_stocks(
        self,
        stock_codes: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Dict[str, List[MockNewsItem]]:
        """
        为多只股票生成模拟新闻
        
        Args:
            stock_codes: 股票代码列表
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            Dict[str, List[MockNewsItem]]: 股票 -> 新闻列表
        """
        all_news = {}
        
        for stock_code in stock_codes:
            news = self.generate_for_stock(stock_code, start_date, end_date)
            if news:
                all_news[stock_code] = news
        
        return all_news
    
    def save_to_json(
        self,
        news_data: Dict[str, List[MockNewsItem]],
        output_filename: str = "mock_news_data.json"
    ) -> str:
        """
        保存新闻数据到 JSON 文件
        
        Args:
            news_data: 新闻数据
            output_filename: 输出文件名
            
        Returns:
            str: 输出文件路径
        """
        output_path = self.output_dir / output_filename
        
        # 转换为字典格式
        output_data = {
            "generated_at": datetime.now().isoformat(),
            "stocks": {
                stock: [news.to_dict() for news in news_list]
                for stock, news_list in news_data.items()
            },
            "summary": {
                "total_stocks": len(news_data),
                "total_news": sum(len(n) for n in news_data.values()),
                "by_importance": {
                    "major": sum(
                        sum(1 for n in news if n.importance == "major")
                        for news in news_data.values()
                    ),
                    "normal": sum(
                        sum(1 for n in news if n.importance == "normal")
                        for news in news_data.values()
                    ),
                    "minor": sum(
                        sum(1 for n in news if n.importance == "minor")
                        for news in news_data.values()
                    )
                },
                "by_sentiment": {
                    "positive": sum(
                        sum(1 for n in news if n.sentiment == "positive")
                        for news in news_data.values()
                    ),
                    "negative": sum(
                        sum(1 for n in news if n.sentiment == "negative")
                        for news in news_data.values()
                    ),
                    "neutral": sum(
                        sum(1 for n in news if n.sentiment == "neutral")
                        for news in news_data.values()
                    )
                }
            }
        }
        
        # 写入文件
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Saved mock news data to: {output_path}")
        return str(output_path)


def main():
    """主函数"""
    logger.info("Starting mock news generation...")
    
    # 配置 - 使用有价格数据的股票
    stocks = [
        "00700.HK", "09988.HK", "03690.HK",
        "00941.HK", "01810.HK", "00883.HK",
        "00001.HK", "00005.HK", "00388.HK",
        "00175.HK", "00981.HK", "01024.HK",
        "00027.HK", "00857.HK", "00386.HK"
    ]
    
    # 生成最近 3 个月的新闻
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    
    logger.info(f"Generating news from {start_date} to {end_date}")
    
    # 创建生成器
    generator = MockNewsGenerator()
    
    # 生成新闻
    news_data = generator.generate_for_stocks(stocks, start_date, end_date)
    
    # 保存到 JSON
    output_path = generator.save_to_json(news_data)
    
    # 打印摘要
    print("\n" + "="*60)
    print("模拟新闻生成完成")
    print("="*60)
    print(f"股票数量：{len(news_data)}")
    print(f"新闻总数：{sum(len(n) for n in news_data.values())}")
    print(f"时间范围：{start_date} 至 {end_date}")
    print(f"输出文件：{output_path}")
    
    # 按股票打印统计
    print("\n按股票统计:")
    for stock, news in news_data.items():
        positive = sum(1 for n in news if n.sentiment == "positive")
        negative = sum(1 for n in news if n.sentiment == "negative")
        neutral = sum(1 for n in news if n.sentiment == "neutral")
        major = sum(1 for n in news if n.importance == "major")
        print(f"  {stock}: {len(news)}条 (利好:{positive}, 利空:{negative}, 中性:{neutral}, 重大:{major})")
    
    print("\n✅ 模拟新闻生成器完成!")


if __name__ == "__main__":
    main()
