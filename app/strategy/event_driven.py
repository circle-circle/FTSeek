#!/usr/bin/env python3
"""
事件驱动策略层 (Rule-Based)

基于突发利好/利空消息，使用关键词规则和规则引擎分析，生成高优先级交易信号。

核心功能:
1. 事件检测 (关键词匹配 + 异常检测)
2. 规则引擎分析 ( sentiment/impact/action 基于关键词权重)
3. 信号生成与推送
4. 时效性管理

流程:
新闻源 → 事件检测器 → 规则分析 → 信号生成 → 优先推送

注：本模块为纯规则实现，不使用 LLM。LLM 分析由 llm_news_strategy.py 承担。
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import hashlib

from app.strategy.base import BaseStrategy, Signal, SignalAction
from app.strategy.stock_pool import get_stock_pool_manager, Tier

logger = logging.getLogger("ftseek.strategy.event_driven")
logger.propagate = False


class EventType(str, Enum):
    """事件类型枚举"""
    EARNINGS = "earnings"           # 财报
    GUIDANCE = "guidance"           # 业绩指引
    MA = "ma"                       # 并购重组 (M&A)
    PRODUCT = "product"             # 新产品/技术
    REGULATION = "regulation"       # 监管政策
    EXECUTIVE = "executive"         # 高管变动
    LAWSUIT = "lawsuit"             # 诉讼/调查
    ANALYST = "analyst"             # 分析师评级
    MARKET_MOVE = "market_move"     # 市场异动
    MACRO = "macro"                 # 宏观经济
    GEOPOLITICAL = "geopolitical"   # 地缘政治


class EventImpact(str, Enum):
    """事件影响程度"""
    CRITICAL = "critical"   # 极高影响 (重大政策/突发灾难/退市风险)
    HIGH = "high"           # 高影响
    MEDIUM = "medium"       # 中影响
    LOW = "low"             # 低影响


@dataclass
class Event:
    """事件数据类"""
    id: str
    type: EventType
    title: str
    content: str
    source: str
    published_at: datetime
    related_stocks: List[str] = field(default_factory=list)

    # 分析结果
    sentiment: str = "neutral"  # positive/negative/neutral
    sentiment_score: float = 0.0  # -1 到 1
    impact_level: EventImpact = EventImpact.LOW
    impact_scope: str = "stock"  # stock/industry/market
    action: str = "hold"  # buy/sell/hold
    time_horizon: str = "intraday"  # intraday/1-3days/1week+
    confidence: float = 0.0  # 0-100%

    # 目标股票及操作
    target_stocks: List[Dict] = field(default_factory=list)
    # [{"code": "HK.00700", "direction": "long", "priority": 1}]

    raw_response: str = ""
    analyzed_at: Optional[datetime] = None

    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'type': self.type.value,
            'title': self.title,
            'content': self.content,
            'source': self.source,
            'published_at': self.published_at.isoformat() if self.published_at else '',
            'related_stocks': self.related_stocks,
            'sentiment': self.sentiment,
            'sentiment_score': self.sentiment_score,
            'impact_level': self.impact_level.value,
            'impact_scope': self.impact_scope,
            'action': self.action,
            'time_horizon': self.time_horizon,
            'confidence': self.confidence,
            'target_stocks': self.target_stocks,
            'raw_response': self.raw_response,
            'analyzed_at': self.analyzed_at.isoformat() if self.analyzed_at else '',
        }


@dataclass
class SignalPriority:
    """信号优先级"""
    HIGH = 1    # 高优先级 (立即执行)
    MEDIUM = 2  # 中优先级 (观察确认)
    LOW = 3     # 低优先级 (加入观察池)


class EventDetector:
    """
    事件检测器

    从新闻流中检测突发事件
    """

    # 关键词映射
    KEYWORDS = {
        EventType.EARNINGS: ['财报', '业绩', '盈利', '亏损', '营收', '净利润', 'earnings', 'revenue'],
        EventType.GUIDANCE: ['指引', '预期', '展望', 'guidance', 'outlook', 'forecast'],
        EventType.MA: ['并购', '收购', '重组', '合并', 'acquisition', 'merger'],
        EventType.PRODUCT: ['新产品', '发布', '技术突破', '专利', 'launch', 'breakthrough'],
        EventType.REGULATION: ['监管', '政策', '调查', '处罚', 'regulation', 'investigation'],
        EventType.EXECUTIVE: ['高管', 'CEO', 'CFO', '离职', '辞职', 'executive', 'resign'],
        EventType.LAWSUIT: ['诉讼', '起诉', '赔偿', 'lawsuit', 'settlement'],
        EventType.ANALYST: ['评级', '目标价', '增持', '减持', 'upgrade', 'downgrade', 'target'],
        EventType.MACRO: ['GDP', '通胀', '利率', '美联储', '央行', 'inflation', 'Fed'],
        EventType.GEOPOLITICAL: ['贸易战', '关税', '制裁', '地缘', 'trade war', 'sanction'],
    }

    def __init__(self):
        self._event_cache: Dict[str, Event] = {}
        self._processed_ids: set = set()

    def detect(self, news_list: List[Dict]) -> List[Event]:
        """
        从新闻列表检测事件

        Args:
            news_list: 新闻列表 [{'id', 'title', 'content', 'source', 'published_at'}]

        Returns:
            检测到的事件列表
        """
        events = []

        for news in news_list:
            news_id = news.get('id', '')

            # 避免重复处理
            if news_id in self._processed_ids:
                continue

            # 关键词匹配
            matched_type = self._match_keywords(news)
            if matched_type:
                event = Event(
                    id=news_id,
                    type=matched_type,
                    title=news.get('title', ''),
                    content=news.get('content', ''),
                    source=news.get('source', ''),
                    published_at=self._parse_datetime(news.get('published_at')),
                    related_stocks=news.get('related_stocks', [])
                )
                events.append(event)

            self._processed_ids.add(news_id)

        logger.info(f"检测到 {len(events)} 个事件")
        return events

    def _match_keywords(self, news: Dict) -> Optional[EventType]:
        """
        关键词匹配事件类型

        Args:
            news: 新闻数据

        Returns:
            匹配的事件类型，无匹配返回 None
        """
        text = f"{news.get('title', '')} {news.get('content', '')}".lower()

        for event_type, keywords in self.KEYWORDS.items():
            for keyword in keywords:
                if keyword.lower() in text:
                    return event_type

        return None

    def _parse_datetime(self, dt_str: str) -> datetime:
        """解析时间字符串"""
        if not dt_str:
            return datetime.now()

        try:
            return datetime.fromisoformat(dt_str)
        except (ValueError, TypeError):
            return datetime.now()


class QwenEventAnalyzer:
    """
    使用 Qwen 大模型分析事件
    """

    ANALYSIS_PROMPT = """你是一名专业交易员，分析以下突发事件对股市的影响。

事件类型：{event_type}
事件标题：{title}
事件内容：{content}
新闻来源：{source}
相关股票：{related_stocks}

请详细分析:
1. 情绪倾向 (正面/负面/中性) 及打分 (-1 到 1)
2. 影响范围 (个股/行业/大盘)
3. 影响程度 (critical/高/中/低)
   - critical: 重大政策变化/突发安全事件/退市风险/被做空机构狙击
   - high: 财报大幅超预期/重大并购/大额回购
   - medium: 分析师评级调整/新产品发布/管理层变动
   - low: 常规公告/市场评论/行业一般新闻
4. 建议操作 (买入/卖出/观望)
5. 时效性 (日内/1-3 天/1 周以上)
6. 置信度 (0-100%)
7. 具体目标股票及操作方向

输出格式 (严格 JSON):
{{
    "sentiment": "positive/negative/neutral",
    "sentiment_score": 0.75,
    "impact_scope": "stock/industry/market",
    "impact_level": "critical/high/medium/low",
    "action": "buy/sell/hold",
    "time_horizon": "intraday/1-3days/1week+",
    "confidence": 85,
    "target_stocks": [
        {{"code": "HK.00700", "direction": "long/short", "priority": 1}}
    ]
}}
"""

    def __init__(self, llm_client=None):
        """
        初始化分析器

        Args:
            llm_client: 大模型客户端，默认使用 Qwen
        """
        self.llm_client = llm_client
        self._cache: Dict[str, Event] = {}

    def analyze(self, event: Event) -> Event:
        """
        分析单个事件

        Args:
            event: 待分析事件

        Returns:
            分析后的事件
        """
        # 检查缓存
        cache_key = hashlib.md5(f"{event.title}{event.content}".encode()).hexdigest()[:16]
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            event.sentiment = cached.sentiment
            event.sentiment_score = cached.sentiment_score
            event.impact_level = cached.impact_level
            event.action = cached.action
            event.confidence = cached.confidence
            event.target_stocks = cached.target_stocks
            logger.debug(f"事件分析缓存命中：{event.id}")
            return event

        # 构建提示词
        prompt = self.ANALYSIS_PROMPT.format(
            event_type=event.type.value,
            title=event.title,
            content=event.content[:500],  # 限制长度
            source=event.source,
            related_stocks=', '.join(event.related_stocks[:5]) if event.related_stocks else '未指定'
        )

        # 使用规则引擎分析
        response = self._simple_analysis(prompt)

        if response:
            # 解析响应
            self._parse_response(event, response)
            event.analyzed_at = datetime.now()

            # 缓存结果
            self._cache[cache_key] = event

        return event

    def _simple_analysis(self, prompt: str) -> Optional[str]:
        """
        基于关键词权重的规则分析

        通过正面/负面关键词计数，判定事件情感、影响程度和建议操作
        """
        # 基于关键词权重的规则分析
        import json

        positive_words = ['利好', '超预期', '增长', '突破', '共赢', 'positive', 'beat']
        negative_words = ['利空', '低于预期', '下滑', '调查', '处罚', 'negative', 'miss']

        sentiment = 'neutral'
        sentiment_score = 0.0
        action = 'hold'

        text = prompt.lower()
        pos_count = sum(1 for w in positive_words if w in text)
        neg_count = sum(1 for w in negative_words if w in text)

        if pos_count > neg_count * 1.5:
            sentiment = 'positive'
            sentiment_score = min(1.0, pos_count / 10)
            action = 'buy'
        elif neg_count > pos_count * 1.5:
            sentiment = 'negative'
            sentiment_score = -min(1.0, neg_count / 10)
            action = 'sell'

        result = {
            'sentiment': sentiment,
            'sentiment_score': sentiment_score,
            'impact_scope': 'stock',
            'impact_level': 'medium',
            'action': action,
            'time_horizon': '1-3days',
            'confidence': 60,
            'target_stocks': []
        }

        return json.dumps(result, ensure_ascii=False)

    def _parse_response(self, event: Event, response: str):
        """解析 LLM 响应"""
        try:
            import json

            # 提取 JSON
            start = response.find('{')
            end = response.rfind('}') + 1
            if start >= 0 and end > start:
                json_str = response[start:end]
                data = json.loads(json_str)

                event.sentiment = data.get('sentiment', 'neutral')
                event.sentiment_score = data.get('sentiment_score', 0.0)
                event.impact_scope = data.get('impact_scope', 'stock')
                # 修复：添加 None 值保护，避免 EventImpact(None) 抛出 ValueError
                impact_level_raw = data.get('impact_level') or 'low'
                event.impact_level = EventImpact(impact_level_raw)
                event.action = data.get('action', 'hold')
                event.time_horizon = data.get('time_horizon', 'intraday')
                event.confidence = data.get('confidence', 0.0)
                event.target_stocks = data.get('target_stocks', [])
                event.raw_response = response

                logger.info(f"事件分析完成：{event.id} → {event.sentiment} ({event.sentiment_score:.2f})")

        except Exception as e:
            logger.warning(f"解析 LLM 响应失败：{e}")


class EventDrivenStrategy(BaseStrategy):
    """
    事件驱动策略

    基于突发事件生成交易信号
    """

    name = "event_driven"

    def __init__(self, config: dict = None):
        super().__init__(config or {})

        self.detector = EventDetector()
        self.analyzer = QwenEventAnalyzer()

        # 事件缓存
        self._events: Dict[str, Event] = {}

        # 信号冷却 (防止重复)
        self._signal_cooldown: Dict[str, datetime] = {}

        # 股票池
        self.stock_pool = get_stock_pool_manager()

        logger.info("事件驱动策略已初始化")

    def on_news(self, news_list: List[Dict]) -> List[Signal]:
        """
        处理新闻列表，生成交易信号

        Args:
            news_list: 新闻列表

        Returns:
            信号列表
        """
        # 1. 检测事件
        events = self.detector.detect(news_list)
        if not events:
            return []

        # 2. 分析事件
        signals = []
        for event in events:
            analyzed = self.analyzer.analyze(event)
            self._events[event.id] = analyzed

            # 3. 生成信号
            signal = self._generate_signal(analyzed)
            if signal:
                signals.append(signal)

        return signals

    def _generate_signal(self, event: Event) -> Optional[Signal]:
        """
        从事件生成交易信号

        Args:
            event: 分析后的事件

        Returns:
            交易信号，无信号返回 None
        """
        # 检查置信度阈值
        if event.confidence < 70:
            logger.debug(f"置信度不足：{event.id} ({event.confidence:.0f}%)")
            return None

        # 检查时效性
        if self._is_event_stale(event):
            logger.debug(f"事件已过时：{event.id}")
            return None

        # 确定操作方向
        if event.action == 'buy':
            action = SignalAction.BUY
        elif event.action == 'sell':
            action = SignalAction.SELL
        else:
            return None  # 观望不生成信号

        # 生成信号
        signals = []
        for target in event.target_stocks:
            code = target.get('code', '')
            direction = target.get('direction', '')
            priority = target.get('priority', 2)

            # 检查冷却
            cooldown_key = f"{code}_{event.id}"
            if cooldown_key in self._signal_cooldown:
                cooldown_end = self._signal_cooldown[cooldown_key]
                if datetime.now() < cooldown_end:
                    continue

            # 确定仓位
            tier = self.stock_pool.get_stock_tier(code)
            if tier == Tier.TIER1:
                quantity = 200  # 核心股票正常仓位
            elif tier == Tier.TIER2:
                quantity = 100  # 观察股票半仓
            else:
                quantity = 50   # 机会股票轻仓

            signal = Signal(
                code=code,
                action=action,
                price=0.0,  # 市价单
                quantity=quantity,
                reason=f"事件驱动：{event.title[:50]} | 情绪={event.sentiment} ({event.sentiment_score:.2f}) | 置信度={event.confidence:.0f}%",
                strategy=self.name,
                extra={
                    'event_id': event.id,
                    'event_type': event.type.value,
                    'impact_level': event.impact_level.value,
                    'priority': priority,
                    'time_horizon': event.time_horizon,
                }
            )

            # 设置冷却
            self._signal_cooldown[cooldown_key] = datetime.now() + timedelta(hours=1)

            signals.append(signal)

        return signals[0] if signals else None

    def _is_event_stale(self, event: Event) -> bool:
        """检查事件是否过时"""
        if not event.published_at:
            return False

        age = datetime.now() - event.published_at

        # 根据时效性判断
        if event.time_horizon == 'intraday':
            return age > timedelta(hours=6)
        elif event.time_horizon == '1-3days':
            return age > timedelta(days=1)
        else:
            return age > timedelta(days=3)

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        """K 线回调 (不支持)"""
        return None

    def get_pending_events(self) -> List[Event]:
        """获取待处理事件"""
        return [e for e in self._events.values()
                if e.action in ['buy', 'sell'] and e.confidence >= 70]

    def get_event_summary(self) -> Dict:
        """获取事件摘要"""
        return {
            'total_events': len(self._events),
            'pending_signals': len(self.get_pending_events()),
            'recent_buy': sum(1 for e in self._events.values() if e.action == 'buy'),
            'recent_sell': sum(1 for e in self._events.values() if e.action == 'sell'),
        }


# 全局单例
_strategy: Optional[EventDrivenStrategy] = None


def get_event_strategy(config: dict = None) -> EventDrivenStrategy:
    """获取事件驱动策略单例"""
    global _strategy
    if _strategy is None:
        _strategy = EventDrivenStrategy(config)
    return _strategy


def init_event_strategy(config: dict = None) -> EventDrivenStrategy:
    """初始化事件驱动策略"""
    global _strategy
    _strategy = EventDrivenStrategy(config)
    return _strategy


if __name__ == "__main__":
    # 测试
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    strategy = get_event_strategy()

    # 模拟新闻
    test_news = [
        {
            'id': 'test_001',
            'title': '腾讯控股 Q4 财报超预期 净利润增长 30%',
            'content': '腾讯控股今日发布 2025 年第四季度财报，显示净利润达到 XXX 亿元，同比增长 30%，超出市场预期。游戏业务和广告业务均表现强劲...',
            'source': 'sina',
            'published_at': datetime.now().isoformat(),
            'related_stocks': ['HK.00700']
        },
        {
            'id': 'test_002',
            'title': '美团获分析师上调评级 目标价看至 200 港元',
            'content': '高盛发布研报，上调美团目标价至 200 港元，评级从"中性"上调至"买入"。分析师认为外卖业务增速回升...',
            'source': 'hkex',
            'published_at': datetime.now().isoformat(),
            'related_stocks': ['HK.03690']
        }
    ]

    signals = strategy.on_news(test_news)

    print("\n📊 生成的信号:")
    for signal in signals:
        print(f"  {signal.code} {signal.action.value} | {signal.reason[:60]}")

    print("\n事件摘要:")
    summary = strategy.get_event_summary()
    print(f"  总事件数：{summary['total_events']}")
    print(f"  待处理信号：{summary['pending_signals']}")
