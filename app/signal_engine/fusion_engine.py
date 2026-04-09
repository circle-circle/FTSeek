"""
信号融合引擎 v2.1 - 模块化设计

特性:
- 完全模块化设计 (指标/情绪/融合/输出独立)
- 信号方向概率显示 (BUY/SELL 概率)
- 可配置权重和阈值
- 支持热插拔模块

信号输出格式:
{
    "direction": "BUY" | "SELL",
    "buy_probability": 0.75,      # 买入概率 (0-1)
    "sell_probability": 0.25,     # 卖出概率 (0-1)
    "confidence": 0.80,           # 置信度
    "signal_level": "strong",     # 信号等级
    "technical_score": 0.65,      # 技术分 (-1 到 1)
    "sentiment_score": 0.40       # 情绪分 (-1 到 1)
}
"""
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import json

from app.strategy.base import Signal, SignalAction, BaseStrategy
# 新闻模块已移除 (2026-03-02)
# from app.data.news_fetcher import NewsItem, get_sync_news_fetcher
from app.storage.database import get_database

logger = logging.getLogger("ftseek.signal.fusion_engine")
logger.propagate = False  # 防止日志重复


# ==================== 数据模型 ====================

@dataclass
class TechnicalScore:
    """技术指标评分"""
    rsi_score: float = 0.0          # RSI 评分 (-1 到 1)
    macd_score: float = 0.0         # MACD 评分
    ma_score: float = 0.0           # 均线评分
    volume_score: float = 0.0       # 成交量评分
    bollinger_score: float = 0.0    # 布林带评分
    composite_score: float = 0.0    # 综合评分 (-1 到 1)
    indicators: Dict = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            'rsi': self.rsi_score,
            'macd': self.macd_score,
            'ma': self.ma_score,
            'volume': self.volume_score,
            'bollinger': self.bollinger_score,
            'composite': self.composite_score,
            'indicators': self.indicators
        }


@dataclass
class SentimentScore:
    """情绪评分"""
    score: float = 0.0              # 情绪得分 (-1 到 1)
    confidence: float = 0.0         # 置信度 (0 到 1)
    news_count: int = 0             # 新闻数量
    keywords: List[str] = field(default_factory=list)
    recent_news: List[Dict] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            'score': self.score,
            'confidence': self.confidence,
            'news_count': self.news_count,
            'keywords': self.keywords[:5],
            'recent_news': self.recent_news[:3]
        }


@dataclass
class FusionResult:
    """融合结果 - 模块化输出"""
    code: str
    name: str
    price: float
    quantity: int
    strategy: str
    
    # 方向概率
    direction: str = "NEUTRAL"      # BUY | SELL | NEUTRAL
    buy_probability: float = 0.5    # 买入概率 (0-1)
    sell_probability: float = 0.5   # 卖出概率 (0-1)
    
    # 评分
    technical_score: float = 0.0    # 技术分 (-1 到 1)
    sentiment_score: float = 0.0    # 情绪分 (-1 到 1)
    final_score: float = 0.0        # 最终分 (-1 到 1)
    confidence: float = 0.0         # 置信度 (0-1)
    
    # 信号等级
    signal_level: str = "weak"      # strong | recommended | neutral | weak
    
    # 详情
    reason: str = ""
    technical_details: Dict = field(default_factory=dict)
    sentiment_details: Dict = field(default_factory=dict)
    extra: Dict = field(default_factory=dict)
    
    def to_signal(self, expire_seconds: int = 900) -> Signal:
        """转换为 Signal 对象"""
        # 根据方向概率确定动作
        if self.direction == "BUY":
            action = SignalAction.BUY
        elif self.direction == "SELL":
            action = SignalAction.SELL
        else:
            action = SignalAction.ALERT
        
        signal = Signal(
            code=self.code,
            name=self.name,
            action=action,
            price=self.price,
            quantity=self.quantity,
            strategy=self.strategy,
            reason=self._generate_reason(),
            extra={
                'direction': self.direction,
                'buy_probability': self.buy_probability,
                'sell_probability': self.sell_probability,
                'technical_score': self.technical_score,
                'sentiment_score': self.sentiment_score,
                'final_score': self.final_score,
                'confidence': self.confidence,
                'signal_level': self.signal_level,
                'technical_details': self.technical_details,
                'sentiment_details': self.sentiment_details,
                **self.extra
            }
        )
        signal.expire_at = datetime.now().timestamp() + expire_seconds
        return signal
    
    def _generate_reason(self) -> str:
        """生成原因描述"""
        parts = [
            f"方向:{self.direction}",
            f"置信度:{self.confidence:.0%}",
            f"技术分:{self.technical_score:+.2f}",
            f"情绪分:{self.sentiment_score:+.2f}"
        ]
        
        if self.technical_details:
            if abs(self.technical_details.get('rsi', 50) - 50) > 20:
                parts.append(f"RSI:{self.technical_details.get('rsi', 50):.1f}")
            if self.technical_details.get('macd_signal', ''):
                parts.append(f"MACD:{self.technical_details.get('macd_signal', '')}")
        
        return " | ".join(parts)
    
    def to_dict(self) -> Dict:
        """转换为字典 (用于 API 返回)"""
        return {
            'code': self.code,
            'name': self.name,
            'price': self.price,
            'direction': self.direction,
            'buy_probability': f"{self.buy_probability:.1%}",
            'sell_probability': f"{self.sell_probability:.1%}",
            'confidence': f"{self.confidence:.1%}",
            'signal_level': self.signal_level,
            'technical_score': f"{self.technical_score:.2f}",
            'sentiment_score': f"{self.sentiment_score:.2f}",
            'reason': self.reason,
            'strategy': self.strategy
        }


# ==================== 模块化组件 ====================

class TechnicalIndicatorModule:
    """
    技术指标模块 - 独立可替换
    
    负责计算 5 大技术指标并综合评分
    """
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        
        # 指标权重 (可配置)
        self.weights = self.config.get('weights', {
            'rsi': 0.25,
            'macd': 0.25,
            'ma': 0.20,
            'volume': 0.15,
            'bollinger': 0.15
        })
        
        # 指标参数
        self.rsi_period = self.config.get('rsi_period', 14)
        self.rsi_overbought = self.config.get('rsi_overbought', 70)
        self.rsi_oversold = self.config.get('rsi_oversold', 30)
        
        logger.info(f"技术指标模块初始化：权重={self.weights}")
    
    def calculate(self, code: str, indicators: Dict) -> TechnicalScore:
        """
        计算技术指标评分
        
        Args:
            code: 股票代码
            indicators: 指标字典
            
        Returns:
            TechnicalScore
        """
        ts = TechnicalScore()
        ts.indicators = indicators
        
        current_price = indicators.get('current_price', 0)
        
        # 1. RSI 评分
        ts.rsi_score = self._calc_rsi_score(indicators.get('rsi', 50))
        
        # 2. MACD 评分
        ts.macd_score = self._calc_macd_score(
            indicators.get('macd', 0),
            indicators.get('macd_signal', 0),
            indicators.get('macd_histogram', 0)
        )
        
        # 3. 均线评分
        ts.ma_score = self._calc_ma_score(
            current_price,
            indicators.get('ma_fast', 0),
            indicators.get('ma_slow', 0)
        )
        
        # 4. 成交量评分
        ts.volume_score = self._calc_volume_score(
            indicators.get('volume', 0),
            indicators.get('volume_avg', 0)
        )
        
        # 5. 布林带评分
        ts.bollinger_score = self._calc_bb_score(
            current_price,
            indicators.get('bollinger_upper', 0),
            indicators.get('bollinger_lower', 0)
        )
        
        # 综合评分 (加权平均)
        ts.composite_score = (
            ts.rsi_score * self.weights['rsi'] +
            ts.macd_score * self.weights['macd'] +
            ts.ma_score * self.weights['ma'] +
            ts.volume_score * self.weights['volume'] +
            ts.bollinger_score * self.weights['bollinger']
        )
        
        # 限制在 -1 到 1 之间
        ts.composite_score = max(-1.0, min(1.0, ts.composite_score))
        
        logger.debug(f"{code} 技术评分：{ts.composite_score:.3f} "
                    f"(RSI:{ts.rsi_score:.2f}, MACD:{ts.macd_score:.2f})")
        
        return ts
    
    def _calc_rsi_score(self, rsi: float) -> float:
        """RSI 评分 (30 以下超买 +1, 70 以上超卖 -1)"""
        if rsi < self.rsi_oversold:
            return 1.0 * (1 - rsi / self.rsi_oversold)
        elif rsi > self.rsi_overbought:
            return -1.0 * ((rsi - self.rsi_overbought) / (100 - self.rsi_overbought))
        else:
            # 中间区域线性插值
            return (50 - rsi) / 50
    
    def _calc_macd_score(self, macd: float, signal: float, histogram: float) -> float:
        """MACD 评分 (金叉 +1, 死叉 -1)"""
        if macd > signal and histogram > 0:
            return 0.5 + 0.5 * min(1.0, histogram / max(abs(macd), 0.01))
        elif macd < signal and histogram < 0:
            return -0.5 - 0.5 * min(1.0, abs(histogram) / max(abs(macd), 0.01))
        else:
            return histogram / max(abs(macd), 0.01) * 0.5
    
    def _calc_ma_score(self, price: float, ma_fast: float, ma_slow: float) -> float:
        """均线评分"""
        if ma_fast > 0 and ma_slow > 0:
            if price > ma_fast > ma_slow:
                return 1.0  # 多头排列
            elif price < ma_fast < ma_slow:
                return -1.0  # 空头排列
            elif ma_fast > ma_slow:
                return 0.5
            else:
                return -0.5
        return 0.0
    
    def _calc_volume_score(self, volume: float, volume_avg: float) -> float:
        """成交量评分"""
        if volume_avg > 0:
            vol_ratio = volume / volume_avg
            if vol_ratio > 2.0:
                return 1.0
            elif vol_ratio > 1.5:
                return 0.5
            elif vol_ratio < 0.5:
                return -0.5
        return 0.0
    
    def _calc_bb_score(self, price: float, upper: float, lower: float) -> float:
        """布林带评分"""
        if upper > 0 and lower > 0:
            position = (price - lower) / (upper - lower)
            if position < 0.1:
                return 1.0  # 接近下轨
            elif position > 0.9:
                return -1.0  # 接近上轨
            elif position < 0.3:
                return 0.5
            elif position > 0.7:
                return -0.5
        return 0.0


class SentimentAnalysisModule:
    """
    情绪分析模块 - 独立可替换
    
    负责抓取新闻并分析情绪
    """
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.news_fetcher = get_sync_news_fetcher(
            timeout=self.config.get('timeout', 10)
        )
        self.db = get_database()
        self.min_news_count = self.config.get('min_news_count', 1)
        self.lookback_days = self.config.get('lookback_days', 3)
        
        logger.info(f"情绪分析模块初始化")
    
    def calculate(self, code: str) -> SentimentScore:
        """
        计算情绪评分
        
        Args:
            code: 股票代码
            
        Returns:
            SentimentScore
        """
        ss = SentimentScore()
        
        try:
            # 获取新闻
            news_list = self.news_fetcher.fetch(code, sources=['sina'])
            
            if not news_list:
                ss.score = 0.0
                ss.confidence = 0.0
                ss.news_count = 0
                return ss
            
            ss.news_count = len(news_list)
            
            # 计算加权平均情绪得分
            total_score = 0.0
            total_weight = 0.0
            
            for i, news in enumerate(news_list[:20]):
                weight = 1.0 / (i + 1)  # 越新权重越高
                total_score += news.sentiment_score * weight
                total_weight += weight
                
                # 保存到数据库
                self.db.save_news(news.to_dict())
            
            if total_weight > 0:
                ss.score = total_score / total_weight
            else:
                ss.score = 0.0
            
            # 置信度计算
            ss.confidence = self._calc_confidence(news_list)
            
            # 提取关键词
            ss.keywords = self._extract_keywords(news_list)
            
            # 保存新闻摘要
            ss.recent_news = [
                {'title': n.title[:50], 'score': n.sentiment_score, 'source': n.source}
                for n in news_list[:5]
            ]
            
            logger.debug(f"{code} 情绪评分：{ss.score:.3f} (置信度:{ss.confidence:.2f})")
            
        except Exception as e:
            logger.error(f"情绪分析失败 {code}: {e}")
            ss.score = 0.0
            ss.confidence = 0.0
        
        return ss
    
    def _calc_confidence(self, news_list: List[NewsItem]) -> float:
        """计算置信度 (基于新闻数量和一致性)"""
        if len(news_list) < 2:
            return 0.3
        
        scores = [n.sentiment_score for n in news_list[:20]]
        avg = sum(scores) / len(scores)
        
        # 一致性 (标准差)
        variance = sum((s - avg) ** 2 for s in scores) / len(scores)
        consistency = 1.0 - min(1.0, variance)
        
        # 数量因子
        quantity_factor = min(1.0, len(scores) / 10.0)
        
        return (consistency + quantity_factor) / 2.0
    
    def _extract_keywords(self, news_list: List[NewsItem]) -> List[str]:
        """提取关键词"""
        all_keywords = []
        for news in news_list[:10]:
            all_keywords.extend(news.keywords)
        return list(set(all_keywords))[:10]


class SignalFusionModule:
    """
    信号融合模块 - 独立可替换
    
    负责融合技术和情绪评分，计算方向概率
    """
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        
        # 权重配置
        self.technical_weight = self.config.get('technical_weight', 0.6)
        self.sentiment_weight = self.config.get('sentiment_weight', 0.4)
        
        # 信号等级阈值
        self.thresholds = self.config.get('thresholds', {
            'strong': 0.8,
            'recommended': 0.6,
            'neutral': 0.4
        })
        
        logger.info(f"融合模块初始化：技术{self.technical_weight:.0%} + 情绪{self.sentiment_weight:.0%}")
    
    def fuse(self, technical: TechnicalScore, 
             sentiment: SentimentScore) -> Tuple[float, float, str, float, float]:
        """
        融合评分并计算方向概率
        
        Returns:
            (final_score, confidence, signal_level, buy_prob, sell_prob)
        """
        # 融合评分
        final_score = (
            technical.composite_score * self.technical_weight +
            sentiment.score * self.sentiment_weight
        )
        final_score = max(-1.0, min(1.0, final_score))
        
        # 置信度计算
        tech_conf = min(1.0, abs(technical.composite_score))
        confidence = (
            tech_conf * self.technical_weight +
            sentiment.confidence * self.sentiment_weight
        )
        
        # 调整置信度
        if sentiment.news_count < 1:
            confidence *= 0.8
        
        # 信号等级
        abs_score = abs(final_score)
        if abs_score >= self.thresholds['strong']:
            signal_level = 'strong'
        elif abs_score >= self.thresholds['recommended']:
            signal_level = 'recommended'
        elif abs_score >= self.thresholds['neutral']:
            signal_level = 'neutral'
        else:
            signal_level = 'weak'
        
        # 方向概率计算 (核心改进)
        # 将 -1 到 1 的分数映射到 0 到 1 的概率
        buy_probability = (final_score + 1) / 2  # 映射到 0-1
        sell_probability = 1 - buy_probability
        
        # 根据信号等级调整概率显示
        if signal_level == 'weak':
            # 弱信号时概率接近 50%
            buy_probability = 0.5 + (buy_probability - 0.5) * 0.5
            sell_probability = 1 - buy_probability
        
        logger.debug(f"融合结果：score={final_score:.3f}, "
                    f"buy={buy_probability:.1%}, sell={sell_probability:.1%}, "
                    f"level={signal_level}")
        
        return final_score, confidence, signal_level, buy_probability, sell_probability


class SignalOutputModule:
    """
    信号输出模块 - 独立可替换
    
    负责生成最终信号和格式化输出
    """
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.min_confidence = self.config.get('min_confidence', 0.4)
        self.min_signal_level = self.config.get('min_signal_level', 'neutral')
        
        self.level_priority = {
            'strong': 3,
            'recommended': 2,
            'neutral': 1,
            'weak': 0
        }
    
    def should_output(self, result: FusionResult) -> bool:
        """判断是否应该输出信号"""
        # 检查置信度
        if result.confidence < self.min_confidence:
            return False
        
        # 检查信号等级
        if self.level_priority.get(result.signal_level, 0) < \
           self.level_priority.get(self.min_signal_level, 1):
            return False
        
        return True
    
    def format_dingtalk(self, result: FusionResult) -> Dict:
        """格式化为钉钉消息"""
        icon = "🔴" if result.direction == "BUY" else "🟢" if result.direction == "SELL" else "🔵"
        
        # 概率条可视化
        prob_bar = self._generate_prob_bar(result.buy_probability)
        
        text = (
            f"### {icon} {result.code} {result.direction} 信号\n\n"
            f"**标的**: {result.code} {result.name}\n\n"
            f"**方向**: **{result.direction}**\n\n"
            f"**价格**: ${result.price:.2f}\n\n"
            f"**概率**:\n{prob_bar}\n\n"
            f"**置信度**: {result.confidence:.1%}\n\n"
            f"**等级**: {result.signal_level}\n\n"
            f"**技术分**: {result.technical_score:+.2f}\n\n"
            f"**情绪分**: {result.sentiment_score:+.2f}\n\n"
            f"**原因**: {result.reason}\n\n"
            f"⏰ 有效期 15 分钟"
        )
        
        return {
            'msgtype': 'markdown',
            'markdown': {
                'title': f'{result.code} {result.direction}',
                'text': text
            }
        }
    
    def _generate_prob_bar(self, buy_prob: float) -> str:
        """生成概率条"""
        buy_pct = int(buy_prob * 100)
        sell_pct = 100 - buy_pct
        
        # 简单条形图
        buy_bars = '█' * (buy_pct // 10)
        sell_bars = '█' * (sell_pct // 10)
        
        return (
            f"买入：{buy_bars} {buy_pct}%\n"
            f"卖出：{sell_bars} {sell_pct}%"
        )
    
    def to_api_response(self, result: FusionResult) -> Dict:
        """格式化为 API 响应"""
        return result.to_dict()


# ==================== 融合引擎主体 ====================

class FusionEngine:
    """
    信号融合引擎 v2.1
    
    模块化设计:
    - TechnicalIndicatorModule: 技术指标
    - SentimentAnalysisModule: 情绪分析
    - SignalFusionModule: 信号融合
    - SignalOutputModule: 信号输出
    
    所有模块可独立替换和配置
    """
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        
        # 初始化模块 (可配置)
        self.technical_module = TechnicalIndicatorModule(
            self.config.get('technical', {})
        )
        self.sentiment_module = SentimentAnalysisModule(
            self.config.get('sentiment', {})
        )
        self.fusion_module = SignalFusionModule(
            self.config.get('fusion', {})
        )
        self.output_module = SignalOutputModule(
            self.config.get('output', {})
        )
        
        logger.info("融合引擎 v2.1 初始化完成 (模块化)")
    
    def generate_signal(self, code: str, name: str, price: float,
                       indicators: Dict, strategy: str = 'fusion_v2',
                       quantity: int = 100) -> Optional[FusionResult]:
        """
        生成融合信号 - 模块化流程
        
        Args:
            code: 股票代码
            name: 股票名称
            price: 当前价格
            indicators: 技术指标字典
            strategy: 策略名称
            quantity: 建议数量
            
        Returns:
            FusionResult 或 None
        """
        # Step 1: 技术指标评分
        tech_score = self.technical_module.calculate(code, indicators)
        
        # Step 2: 情绪评分
        sent_score = self.sentiment_module.calculate(code)
        
        # Step 3: 融合评分 + 方向概率
        final_score, confidence, signal_level, buy_prob, sell_prob = \
            self.fusion_module.fuse(tech_score, sent_score)
        
        # Step 4: 确定方向
        if final_score > 0.1:
            direction = "BUY"
        elif final_score < -0.1:
            direction = "SELL"
        else:
            direction = "NEUTRAL"
        
        # Step 5: 生成结果
        result = FusionResult(
            code=code,
            name=name,
            price=price,
            quantity=quantity,
            strategy=strategy,
            direction=direction,
            buy_probability=buy_prob,
            sell_probability=sell_prob,
            technical_score=tech_score.composite_score,
            sentiment_score=sent_score.score,
            final_score=final_score,
            confidence=confidence,
            signal_level=signal_level,
            technical_details=tech_score.to_dict(),
            sentiment_details=sent_score.to_dict()
        )
        
        # Step 6: 判断是否输出
        if not self.output_module.should_output(result):
            logger.info(f"{code} 信号太弱，跳过：level={signal_level}, conf={confidence:.2f}")
            return None
        
        logger.info(f"生成信号：{code} {direction} | "
                   f"BUY:{buy_prob:.1%} SELL:{sell_prob:.1%} | "
                   f"等级:{signal_level} | 置信度:{confidence:.2f}")
        
        return result
    
    def get_module(self, name: str):
        """获取模块 (用于动态配置)"""
        modules = {
            'technical': self.technical_module,
            'sentiment': self.sentiment_module,
            'fusion': self.fusion_module,
            'output': self.output_module
        }
        return modules.get(name)
    
    def replace_module(self, name: str, new_module):
        """替换模块 (热插拔)"""
        if name == 'technical':
            self.technical_module = new_module
        elif name == 'sentiment':
            self.sentiment_module = new_module
        elif name == 'fusion':
            self.fusion_module = new_module
        elif name == 'output':
            self.output_module = new_module
        else:
            raise ValueError(f"未知模块：{name}")
        
        logger.info(f"模块已替换：{name}")


# 全局单例
_fusion_engine: Optional[FusionEngine] = None


def get_fusion_engine(config: Dict = None) -> FusionEngine:
    """获取融合引擎单例"""
    global _fusion_engine
    if _fusion_engine is None:
        _fusion_engine = FusionEngine(config)
    return _fusion_engine


def reset_fusion_engine(config: Dict = None):
    """重置融合引擎 (用于重新配置)"""
    global _fusion_engine
    _fusion_engine = FusionEngine(config)
    return _fusion_engine
