"""
批量复盘处理器模块

负责批量获取交易数据、智能推断、6 大维度分析和报告生成。
支持日报、周报、月报的批量处理。

Author: strategy-gen (Harness Multi-Agent System)
Version: 1.0
Date: 2026-03-28
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid

logger = logging.getLogger(__name__)


class ReportType(Enum):
    """报告类型枚举"""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass
class InferredTrade:
    """智能推断后的交易数据"""
    trade_id: str
    stock_code: str
    market: str
    side: str
    price: float
    quantity: int
    exec_time: datetime
    pnl: float
    pnl_percent: float
    decision_reason: str = "未明确"
    emotion_state: str = "neutral"
    confidence_score: float = 0.5
    timing_score: float = 0.5
    strategy_type: str = "unknown"
    original_data: Optional[Dict[str, Any]] = None


@dataclass
class DimensionAnalysis:
    """维度分析结果"""
    score: float
    details: Dict[str, Any]
    level: str = field(init=False)
    
    def __post_init__(self):
        if self.score >= 90:
            self.level = "S"
        elif self.score >= 80:
            self.level = "A"
        elif self.score >= 70:
            self.level = "B"
        elif self.score >= 60:
            self.level = "C"
        else:
            self.level = "D"


@dataclass
class AnalysisResult:
    """6 大维度分析结果"""
    decision_quality: DimensionAnalysis
    timing_quality: DimensionAnalysis
    position_management: DimensionAnalysis
    risk_execution: DimensionAnalysis
    psychology: DimensionAnalysis
    strategy_effectiveness: DimensionAnalysis
    overall_score: float = field(init=False)
    
    def __post_init__(self):
        scores = [
            self.decision_quality.score,
            self.timing_quality.score,
            self.position_management.score,
            self.risk_execution.score,
            self.psychology.score,
            self.strategy_effectiveness.score,
        ]
        self.overall_score = sum(scores) / len(scores)


@dataclass
class ErrorPattern:
    """错误模式"""
    pattern_type: str
    severity: str
    description: str
    suggestion: str
    affected_trades: List[str] = field(default_factory=list)


@dataclass
class DailyReport:
    """每日复盘报告"""
    report_id: str
    report_type: ReportType
    report_date: str
    generated_time: datetime
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl_percent: float = 0.0
    six_dimensions: Optional[AnalysisResult] = None
    error_patterns: List[ErrorPattern] = field(default_factory=list)
    improvement_suggestions: List[str] = field(default_factory=list)
    trades: List[InferredTrade] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'report_id': self.report_id,
            'report_type': self.report_type.value,
            'report_date': self.report_date,
            'generated_time': self.generated_time.isoformat(),
            'summary': {
                'total_trades': self.total_trades,
                'winning_trades': self.winning_trades,
                'losing_trades': self.losing_trades,
                'win_rate': self.win_rate,
                'total_pnl': self.total_pnl,
                'avg_pnl_percent': self.avg_pnl_percent,
            },
            'six_dimensions': self._dimensions_to_dict(),
            'error_patterns': [p.__dict__ for p in self.error_patterns],
            'improvement_suggestions': self.improvement_suggestions,
        }
    
    def _dimensions_to_dict(self) -> Optional[Dict[str, Any]]:
        if not self.six_dimensions:
            return None
        return {
            'decision_quality': {
                'score': self.six_dimensions.decision_quality.score,
                'level': self.six_dimensions.decision_quality.level,
                'details': self.six_dimensions.decision_quality.details,
            },
            'timing_quality': {
                'score': self.six_dimensions.timing_quality.score,
                'level': self.six_dimensions.timing_quality.level,
                'details': self.six_dimensions.timing_quality.details,
            },
            'position_management': {
                'score': self.six_dimensions.position_management.score,
                'level': self.six_dimensions.position_management.level,
                'details': self.six_dimensions.position_management.details,
            },
            'risk_execution': {
                'score': self.six_dimensions.risk_execution.score,
                'level': self.six_dimensions.risk_execution.level,
                'details': self.six_dimensions.risk_execution.details,
            },
            'psychology': {
                'score': self.six_dimensions.psychology.score,
                'level': self.six_dimensions.psychology.level,
                'details': self.six_dimensions.psychology.details,
            },
            'strategy_effectiveness': {
                'score': self.six_dimensions.strategy_effectiveness.score,
                'level': self.six_dimensions.strategy_effectiveness.level,
                'details': self.six_dimensions.strategy_effectiveness.details,
            },
            'overall_score': self.six_dimensions.overall_score,
        }


class BatchDataFetcher:
    """批量数据获取器"""
    
    def __init__(self, futu_api_client: Any = None):
        self.futu_api = futu_api_client
        self.logger = logging.getLogger(f"{__name__}.BatchDataFetcher")
        self.timezone = ZoneInfo('Asia/Shanghai')
    
    def fetch_daily_trades(self, date: str, market: str = 'HK') -> List[Dict[str, Any]]:
        self.logger.info(f"Fetching daily trades for {date}, market={market}")
        
        try:
            start_time = datetime.strptime(date, '%Y-%m-%d').replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=self.timezone
            )
            end_time = start_time.replace(hour=23, minute=59, second=59)
            
            if self.futu_api:
                trades = self.futu_api.fetch_trade_list(market, start_time, end_time)
            else:
                trades = self._mock_fetch_daily_trades(date, market)
            
            self.logger.info(f"Successfully fetched {len(trades)} trades")
            return trades
        except Exception as e:
            self.logger.error(f"Failed to fetch daily trades: {e}")
            raise
    
    def fetch_trades_by_period(self, start_date: str, end_date: str, market: str = 'HK') -> List[Dict[str, Any]]:
        self.logger.info(f"Fetching trades from {start_date} to {end_date}")
        
        try:
            start_time = datetime.strptime(start_date, '%Y-%m-%d').replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=self.timezone
            )
            end_time = datetime.strptime(end_date, '%Y-%m-%d').replace(
                hour=23, minute=59, second=59, microsecond=0, tzinfo=self.timezone
            )
            
            if self.futu_api:
                trades = self.futu_api.fetch_trade_list(market, start_time, end_time)
            else:
                trades = self._mock_fetch_period_trades(start_date, end_date, market)
            
            self.logger.info(f"Successfully fetched {len(trades)} trades")
            return trades
        except Exception as e:
            self.logger.error(f"Failed to fetch period trades: {e}")
            raise
    
    def fetch_positions(self, market: str = 'HK') -> List[Dict[str, Any]]:
        return []
    
    def fetch_performance_data(self, date: str, market: str = 'HK') -> Dict[str, Any]:
        return self._mock_performance_data(date, market)
    
    def _mock_fetch_daily_trades(self, date: str, market: str) -> List[Dict[str, Any]]:
        import random
        trade_count = random.randint(5, 20)
        trades = []
        
        for i in range(trade_count):
            trade = {
                'trade_id': f"MOCK_{market}_{uuid.uuid4().hex[:8]}",
                'stock_code': f"{'0' if market == 'HK' else ''}{random.randint(1, 99999)}",
                'market': market,
                'side': random.choice(['BUY', 'SELL']),
                'price': random.uniform(10, 1000),
                'quantity': random.randint(100, 10000),
                'exec_time': datetime.strptime(date, '%Y-%m-%d').replace(
                    hour=random.randint(9, 16), minute=random.randint(0, 59), tzinfo=self.timezone
                ),
                'pnl': random.uniform(-1000, 5000),
                'pnl_percent': random.uniform(-0.05, 0.1),
                'commission': random.uniform(1, 100),
                'stamp_duty': random.uniform(0, 50) if market == 'HK' else 0,
                'signal_strength': random.uniform(0.3, 0.9),
                'position_ratio': random.uniform(0.05, 0.3),
                'stop_loss_triggered': random.random() < 0.1,
                'holding_days': random.randint(0, 10),
            }
            trades.append(trade)
        
        return trades
    
    def _mock_fetch_period_trades(self, start_date: str, end_date: str, market: str) -> List[Dict[str, Any]]:
        all_trades = []
        current = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        
        while current <= end:
            date_str = current.strftime('%Y-%m-%d')
            daily_trades = self._mock_fetch_daily_trades(date_str, market)
            all_trades.extend(daily_trades)
            current += timedelta(days=1)
        
        return all_trades
    
    def _mock_performance_data(self, date: str, market: str) -> Dict[str, Any]:
        import random
        return {
            'date': date,
            'market': market,
            'total_pnl': random.uniform(-5000, 20000),
            'total_pnl_percent': random.uniform(-0.05, 0.15),
            'win_rate': random.uniform(0.4, 0.7),
        }


class BatchRuleEngine:
    """批量规则引擎"""
    
    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.BatchRuleEngine")
    
    def apply_rules(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        result = trade.copy()
        result['confidence_score'] = self._infer_confidence(trade)
        result['strategy_type'] = self._infer_strategy_type(trade)
        result['timing_score'] = self._infer_timing_score(trade)
        result['decision_reason'] = self._infer_decision_reason(trade)
        result['emotion_state'] = self._infer_emotion_state(trade)
        return result
    
    def _infer_confidence(self, trade: Dict[str, Any]) -> float:
        signal_strength = trade.get('signal_strength', 0.5)
        if signal_strength >= 0.8:
            return min(0.95, signal_strength)
        elif signal_strength >= 0.6:
            return 0.7 + (signal_strength - 0.6) * 0.5
        elif signal_strength >= 0.4:
            return 0.5 + (signal_strength - 0.4) * 0.5
        else:
            return 0.3 + signal_strength * 0.5
    
    def _infer_strategy_type(self, trade: Dict[str, Any]) -> str:
        holding_days = trade.get('holding_days', 0)
        if holding_days <= 1:
            return 'day_trade'
        elif holding_days <= 5:
            return 'swing_trade'
        else:
            return 'position_trade'
    
    def _infer_timing_score(self, trade: Dict[str, Any]) -> float:
        buy_price = trade.get('price', 0)
        day_low = trade.get('day_low', buy_price * 0.95)
        day_high = trade.get('day_high', buy_price * 1.05)
        
        if day_high > day_low:
            position = (buy_price - day_low) / (day_high - day_low)
            return max(0.0, min(1.0, 1.0 - position))
        else:
            return 0.5
    
    def _infer_decision_reason(self, trade: Dict[str, Any]) -> str:
        signal_strength = trade.get('signal_strength', 0.5)
        pnl_percent = trade.get('pnl_percent', 0)
        reasons = []
        
        if signal_strength >= 0.7:
            reasons.append("强信号触发")
        elif signal_strength >= 0.5:
            reasons.append("常规信号")
        else:
            reasons.append("弱信号")
        
        if pnl_percent > 0.05:
            reasons.append("盈利良好")
        elif pnl_percent < -0.03:
            reasons.append("止损执行")
        
        return ", ".join(reasons) if reasons else "未明确"
    
    def _infer_emotion_state(self, trade: Dict[str, Any]) -> str:
        pnl_percent = trade.get('pnl_percent', 0)
        timing_score = trade.get('timing_score', 0.5)
        stop_loss_triggered = trade.get('stop_loss_triggered', False)
        
        if stop_loss_triggered:
            return 'disciplined'
        elif pnl_percent > 0.05 and timing_score > 0.7:
            return 'confident'
        elif pnl_percent < -0.03 and timing_score < 0.3:
            return 'panic'
        elif pnl_percent > 0.03 and timing_score < 0.3:
            return 'fomo'
        elif abs(pnl_percent) < 0.01:
            return 'neutral'
        else:
            return 'calm'


class BatchInferenceEngine:
    """批量智能推断引擎"""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None, max_workers: int = 4):
        self.config = config or {}
        self.max_workers = max_workers
        self.logger = logging.getLogger(f"{__name__}.BatchInferenceEngine")
        self.rule_engine = BatchRuleEngine()
    
    def batch_infer(self, trades: List[Dict[str, Any]]) -> List[InferredTrade]:
        self.logger.info(f"Starting batch inference for {len(trades)} trades")
        
        if not trades:
            return []
        
        results = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_trade = {
                executor.submit(self._infer_single_trade, trade): trade
                for trade in trades
            }
            
            for future in as_completed(future_to_trade):
                trade = future_to_trade[future]
                try:
                    inferred = future.result()
                    results.append(inferred)
                except Exception as e:
                    self.logger.warning(f"Inference failed for trade {trade.get('trade_id')}: {e}")
                    results.append(self._get_default_inference(trade))
        
        self.logger.info(f"Batch inference completed: {len(results)} trades")
        return results
    
    def _infer_single_trade(self, trade: Dict[str, Any]) -> InferredTrade:
        inferred_data = self.rule_engine.apply_rules(trade)
        
        return InferredTrade(
            trade_id=trade.get('trade_id', ''),
            stock_code=trade.get('stock_code', ''),
            market=trade.get('market', ''),
            side=trade.get('side', ''),
            price=float(trade.get('price', 0)),
            quantity=int(trade.get('quantity', 0)),
            exec_time=trade.get('exec_time', datetime.now(ZoneInfo('Asia/Shanghai'))),
            pnl=float(trade.get('pnl', 0)),
            pnl_percent=float(trade.get('pnl_percent', 0)),
            decision_reason=inferred_data.get('decision_reason', '未明确'),
            emotion_state=inferred_data.get('emotion_state', 'neutral'),
            confidence_score=inferred_data.get('confidence_score', 0.5),
            timing_score=inferred_data.get('timing_score', 0.5),
            strategy_type=inferred_data.get('strategy_type', 'unknown'),
            original_data=trade,
        )
    
    def _get_default_inference(self, trade: Dict[str, Any]) -> InferredTrade:
        return InferredTrade(
            trade_id=trade.get('trade_id', ''),
            stock_code=trade.get('stock_code', ''),
            market=trade.get('market', ''),
            side=trade.get('side', ''),
            price=float(trade.get('price', 0)),
            quantity=int(trade.get('quantity', 0)),
            exec_time=trade.get('exec_time', datetime.now(ZoneInfo('Asia/Shanghai'))),
            pnl=float(trade.get('pnl', 0)),
            pnl_percent=float(trade.get('pnl_percent', 0)),
            decision_reason='未明确',
            emotion_state='neutral',
            confidence_score=0.5,
            timing_score=0.5,
            strategy_type='unknown',
            original_data=trade,
        )


class ErrorPatternDetector:
    """错误模式检测器"""
    
    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.ErrorPatternDetector")
    
    def detect_patterns(self, trades: List[Dict[str, Any]]) -> List[ErrorPattern]:
        patterns = []
        patterns.extend(self._detect_continuous_losses(trades))
        patterns.extend(self._detect_chase_kill(trades))
        patterns.extend(self._detect_early_profit_taking(trades))
        patterns.extend(self._detect_overtrading(trades))
        patterns.extend(self._detect_heavy_loss(trades))
        return patterns
    
    def _detect_continuous_losses(self, trades: List[Dict[str, Any]]) -> List[ErrorPattern]:
        patterns = []
        consecutive_losses = 0
        max_consecutive = 0
        
        sorted_trades = sorted(trades, key=lambda x: str(x.get('exec_time', '')))
        
        for trade in sorted_trades:
            if trade.get('pnl', 0) < 0:
                consecutive_losses += 1
                max_consecutive = max(max_consecutive, consecutive_losses)
            else:
                consecutive_losses = 0
        
        if max_consecutive >= 3:
            patterns.append(ErrorPattern(
                pattern_type='continuous_losses',
                severity='high' if max_consecutive >= 5 else 'medium',
                description=f'出现连续{max_consecutive}次亏损',
                suggestion='建议暂停交易，复盘调整策略',
            ))
        
        return patterns
    
    def _detect_chase_kill(self, trades: List[Dict[str, Any]]) -> List[ErrorPattern]:
        patterns = []
        chase_count = sum(1 for t in trades if t.get('timing_score', 0.5) < 0.3)
        
        if chase_count >= len(trades) * 0.3:
            patterns.append(ErrorPattern(
                pattern_type='chase_kill',
                severity='high',
                description=f'{chase_count}笔交易存在追涨杀跌行为',
                suggestion='避免情绪化交易，严格执行买入纪律',
            ))
        
        return patterns
    
    def _detect_early_profit_taking(self, trades: List[Dict[str, Any]]) -> List[ErrorPattern]:
        patterns = []
        early_count = sum(1 for t in trades if t.get('pnl', 0) > 0 and t.get('holding_days', 0) < 1)
        
        if early_count >= len(trades) * 0.4:
            patterns.append(ErrorPattern(
                pattern_type='early_profit_taking',
                severity='medium',
                description=f'{early_count}笔交易过早止盈',
                suggestion='适当延长持仓时间，让利润奔跑',
            ))
        
        return patterns
    
    def _detect_overtrading(self, trades: List[Dict[str, Any]]) -> List[ErrorPattern]:
        patterns = []
        
        if len(trades) > 20:
            patterns.append(ErrorPattern(
                pattern_type='overtrading',
                severity='high',
                description=f'单日交易{len(trades)}笔，存在过度交易',
                suggestion='减少交易频率，提高交易质量',
            ))
        
        return patterns
    
    def _detect_heavy_loss(self, trades: List[Dict[str, Any]]) -> List[ErrorPattern]:
        patterns = []
        
        for trade in trades:
            pnl_percent = trade.get('pnl_percent', 0)
            position_ratio = trade.get('position_ratio', 0)
            
            if pnl_percent < -0.05 and position_ratio > 0.3:
                patterns.append(ErrorPattern(
                    pattern_type='heavy_loss',
                    severity='high',
                    description=f'重仓 ({position_ratio*100:.0f}%) 亏损 {pnl_percent*100:.1f}%',
                    suggestion='控制单笔仓位，避免重仓亏损',
                ))
        
        return patterns


class BatchAnalyzer:
    """批量分析器"""
    
    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.BatchAnalyzer")
        self.error_detector = ErrorPatternDetector()
    
    def analyze_six_dimensions(self, trades: List[InferredTrade]) -> AnalysisResult:
        self.logger.info(f"Analyzing {len(trades)} trades across 6 dimensions")
        
        if not trades:
            return self._get_empty_analysis()
        
        trade_dicts = [self._trade_to_dict(t) for t in trades]
        
        result = AnalysisResult(
            decision_quality=self._analyze_decision_quality(trade_dicts),
            timing_quality=self._analyze_timing_quality(trade_dicts),
            position_management=self._analyze_position_management(trade_dicts),
            risk_execution=self._analyze_risk_execution(trade_dicts),
            psychology=self._analyze_psychology(trade_dicts),
            strategy_effectiveness=self._analyze_strategy_effectiveness(trade_dicts),
        )
        
        self.logger.info(f"Analysis completed. Overall score: {result.overall_score:.2f}")
        return result
    
    def _trade_to_dict(self, trade: InferredTrade) -> Dict[str, Any]:
        return {
            'trade_id': trade.trade_id,
            'pnl': trade.pnl,
            'pnl_percent': trade.pnl_percent,
            'confidence_score': trade.confidence_score,
            'timing_score': trade.timing_score,
            'emotion_state': trade.emotion_state,
            'strategy_type': trade.strategy_type,
            'position_ratio': trade.original_data.get('position_ratio', 0.1) if trade.original_data else 0.1,
            'stop_loss_triggered': trade.original_data.get('stop_loss_triggered', False) if trade.original_data else False,
            'holding_days': trade.original_data.get('holding_days', 0) if trade.original_data else 0,
        }
    
    def _get_empty_analysis(self) -> AnalysisResult:
        empty_dim = DimensionAnalysis(score=0.0, details={})
        return AnalysisResult(
            decision_quality=empty_dim,
            timing_quality=empty_dim,
            position_management=empty_dim,
            risk_execution=empty_dim,
            psychology=empty_dim,
            strategy_effectiveness=empty_dim,
        )
    
    def _analyze_decision_quality(self, trades: List[Dict[str, Any]]) -> DimensionAnalysis:
        if not trades:
            return DimensionAnalysis(score=0.0, details={})
        
        avg_confidence = sum(t.get('confidence_score', 0) for t in trades) / len(trades)
        high_confidence_trades = [t for t in trades if t.get('confidence_score', 0) >= 0.7]
        high_confidence_ratio = len(high_confidence_trades) / len(trades)
        winning_trades = [t for t in trades if t.get('pnl', 0) > 0]
        win_rate = len(winning_trades) / len(trades)
        
        score = (avg_confidence * 0.4 + high_confidence_ratio * 0.3 + win_rate * 0.3) * 100
        
        return DimensionAnalysis(
            score=round(score, 2),
            details={
                'avg_confidence': round(avg_confidence, 3),
                'high_confidence_ratio': round(high_confidence_ratio, 3),
                'win_rate': round(win_rate, 3),
            }
        )
    
    def _analyze_timing_quality(self, trades: List[Dict[str, Any]]) -> DimensionAnalysis:
        if not trades:
            return DimensionAnalysis(score=0.0, details={})
        
        avg_timing = sum(t.get('timing_score', 0) for t in trades) / len(trades)
        good_timing_trades = [t for t in trades if t.get('timing_score', 0) >= 0.7]
        good_timing_ratio = len(good_timing_trades) / len(trades)
        
        score = (avg_timing * 0.6 + good_timing_ratio * 0.4) * 100
        
        return DimensionAnalysis(
            score=round(score, 2),
            details={
                'avg_timing': round(avg_timing, 3),
                'good_timing_ratio': round(good_timing_ratio, 3),
            }
        )
    
    def _analyze_position_management(self, trades: List[Dict[str, Any]]) -> DimensionAnalysis:
        if not trades:
            return DimensionAnalysis(score=0.0, details={})
        
        positions = [t.get('position_ratio', 0) for t in trades]
        avg_position = sum(positions) / len(positions)
        max_position = max(positions) if positions else 0
        
        position_score = 100 if max_position <= 0.3 else max(0, 100 - (max_position - 0.3) * 200)
        
        return DimensionAnalysis(
            score=round(position_score, 2),
            details={
                'avg_position': round(avg_position, 3),
                'max_position': round(max_position, 3),
            }
        )
    
    def _analyze_risk_execution(self, trades: List[Dict[str, Any]]) -> DimensionAnalysis:
        if not trades:
            return DimensionAnalysis(score=0.0, details={})
        
        stop_loss_trades = [t for t in trades if t.get('stop_loss_triggered', False)]
        stop_loss_ratio = len(stop_loss_trades) / len(trades) if trades else 0
        
        large_loss_trades = [t for t in trades if t.get('pnl_percent', 0) < -0.05]
        large_loss_ratio = len(large_loss_trades) / len(trades) if trades else 0
        
        risk_score = 100 * (1 - large_loss_ratio) - stop_loss_ratio * 20
        
        return DimensionAnalysis(
            score=round(max(0, risk_score), 2),
            details={
                'stop_loss_ratio': round(stop_loss_ratio, 3),
                'large_loss_ratio': round(large_loss_ratio, 3),
            }
        )
    
    def _analyze_psychology(self, trades: List[Dict[str, Any]]) -> DimensionAnalysis:
        if not trades:
            return DimensionAnalysis(score=0.0, details={})
        
        emotion_counts = {}
        for t in trades:
            emotion = t.get('emotion_state', 'neutral')
            emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1
        
        rational_emotions = ['calm', 'confident', 'neutral', 'disciplined']
        rational_trades = sum(emotion_counts.get(e, 0) for e in rational_emotions)
        rational_ratio = rational_trades / len(trades)
        
        impulse_emotions = ['fomo', 'panic', 'revenge']
        impulse_trades = sum(emotion_counts.get(e, 0) for e in impulse_emotions)
        impulse_ratio = impulse_trades / len(trades)
        
        score = (rational_ratio * 0.7 + (1 - impulse_ratio) * 0.3) * 100
        
        return DimensionAnalysis(
            score=round(score, 2),
            details={
                'emotion_distribution': emotion_counts,
                'rational_ratio': round(rational_ratio, 3),
                'impulse_ratio': round(impulse_ratio, 3),
            }
        )
    
    def _analyze_strategy_effectiveness(self, trades: List[Dict[str, Any]]) -> DimensionAnalysis:
        if not trades:
            return DimensionAnalysis(score=0.0, details={})
        
        strategy_groups = {}
        for t in trades:
            strategy = t.get('strategy_type', 'unknown')
            if strategy not in strategy_groups:
                strategy_groups[strategy] = []
            strategy_groups[strategy].append(t)
        
        strategy_performance = {}
        for strategy, group in strategy_groups.items():
            winning = [t for t in group if t.get('pnl', 0) > 0]
            win_rate = len(winning) / len(group)
            total_pnl = sum(t.get('pnl', 0) for t in group)
            avg_pnl = total_pnl / len(group)
            
            strategy_performance[strategy] = {
                'trade_count': len(group),
                'win_rate': round(win_rate, 3),
                'total_pnl': round(total_pnl, 2),
                'avg_pnl': round(avg_pnl, 2),
            }
        
        avg_win_rate = sum(p['win_rate'] for p in strategy_performance.values()) / len(strategy_performance)
        score = avg_win_rate * 100
        
        return DimensionAnalysis(
            score=round(score, 2),
            details={
                'strategy_performance': strategy_performance,
                'avg_win_rate': round(avg_win_rate, 3),
            }
        )
    
    def detect_error_patterns(self, trades: List[InferredTrade]) -> List[ErrorPattern]:
        trade_dicts = [self._trade_to_dict(t) for t in trades]
        return self.error_detector.detect_patterns(trade_dicts)


class BatchReportGenerator:
    """批量报告生成器"""
    
    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.BatchReportGenerator")
        self.analyzer = BatchAnalyzer()
    
    def generate_daily_report(self, date: str, trades: List[InferredTrade], analysis: Optional[AnalysisResult] = None) -> DailyReport:
        self.logger.info(f"Generating daily report for {date}")
        
        if not analysis:
            analysis = self.analyzer.analyze_six_dimensions(trades)
        
        total_trades = len(trades)
        winning_trades = len([t for t in trades if t.pnl > 0])
        losing_trades = len([t for t in trades if t.pnl < 0])
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
        total_pnl = sum(t.pnl for t in trades)
        avg_pnl_percent = sum(t.pnl_percent for t in trades) / total_trades if total_trades > 0 else 0.0
        
        error_patterns = self.analyzer.detect_error_patterns(trades)
        suggestions = self._generate_suggestions(analysis, error_patterns)
        
        report = DailyReport(
            report_id=f"DAILY_{date}_{uuid.uuid4().hex[:8]}",
            report_type=ReportType.DAILY,
            report_date=date,
            generated_time=datetime.now(ZoneInfo('Asia/Shanghai')),
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=round(win_rate, 3),
            total_pnl=round(total_pnl, 2),
            avg_pnl_percent=round(avg_pnl_percent, 4),
            six_dimensions=analysis,
            error_patterns=error_patterns,
            improvement_suggestions=suggestions,
            trades=trades,
        )
        
        self.logger.info(f"Daily report generated: {report.report_id}")
        return report
    
    def generate_weekly_report(self, week_start: str, week_end: str, trades: List[InferredTrade]) -> Dict[str, Any]:
        self.logger.info(f"Generating weekly report from {week_start} to {week_end}")
        
        daily_groups = self._group_by_date(trades)
        weekly_stats = self._calculate_weekly_stats(trades)
        trend_analysis = self._analyze_trend(daily_groups)
        analysis = self.analyzer.analyze_six_dimensions(trades)
        error_patterns = self.analyzer.detect_error_patterns(trades)
        suggestions = self._generate_weekly_suggestions(weekly_stats, trend_analysis, error_patterns)
        
        return {
            'report_id': f"WEEKLY_{week_start}_{uuid.uuid4().hex[:8]}",
            'report_type': ReportType.WEEKLY.value,
            'week_start': week_start,
            'week_end': week_end,
            'generated_time': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
            'summary': weekly_stats,
            'trend_analysis': trend_analysis,
            'daily_breakdown': daily_groups,
            'six_dimensions': {
                'decision_quality': analysis.decision_quality.__dict__,
                'timing_quality': analysis.timing_quality.__dict__,
                'position_management': analysis.position_management.__dict__,
                'risk_execution': analysis.risk_execution.__dict__,
                'psychology': analysis.psychology.__dict__,
                'strategy_effectiveness': analysis.strategy_effectiveness.__dict__,
                'overall_score': analysis.overall_score,
            },
            'error_patterns': [p.__dict__ for p in error_patterns],
            'improvement_suggestions': suggestions,
        }
    
    def generate_monthly_report(self, month: str, trades: List[InferredTrade]) -> Dict[str, Any]:
        self.logger.info(f"Generating monthly report for {month}")
        
        weekly_groups = self._group_by_week(trades)
        monthly_stats = self._calculate_monthly_stats(trades)
        strategy_eval = self._evaluate_strategies(trades)
        analysis = self.analyzer.analyze_six_dimensions(trades)
        error_patterns = self.analyzer.detect_error_patterns(trades)
        suggestions = self._generate_monthly_suggestions(monthly_stats, strategy_eval, error_patterns)
        
        return {
            'report_id': f"MONTHLY_{month}_{uuid.uuid4().hex[:8]}",
            'report_type': ReportType.MONTHLY.value,
            'month': month,
            'generated_time': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
            'summary': monthly_stats,
            'strategy_evaluation': strategy_eval,
            'weekly_breakdown': weekly_groups,
            'six_dimensions': {
                'decision_quality': analysis.decision_quality.__dict__,
                'timing_quality': analysis.timing_quality.__dict__,
                'position_management': analysis.position_management.__dict__,
                'risk_execution': analysis.risk_execution.__dict__,
                'psychology': analysis.psychology.__dict__,
                'strategy_effectiveness': analysis.strategy_effectiveness.__dict__,
                'overall_score': analysis.overall_score,
            },
            'error_patterns': [p.__dict__ for p in error_patterns],
            'improvement_suggestions': suggestions,
        }
    
    def _generate_suggestions(self, analysis: AnalysisResult, error_patterns: List[ErrorPattern]) -> List[str]:
        suggestions = []
        
        dimensions = [
            ('决策质量', analysis.decision_quality),
            ('时机质量', analysis.timing_quality),
            ('仓位管理', analysis.position_management),
            ('风控执行', analysis.risk_execution),
            ('心理状态', analysis.psychology),
            ('策略有效性', analysis.strategy_effectiveness),
        ]
        
        for name, dim in dimensions:
            if dim.score < 60:
                suggestions.append(f'{name}评分较低 ({dim.score}分，等级{dim.level})，需要重点关注')
        
        for pattern in error_patterns:
            suggestions.append(pattern.suggestion)
        
        return suggestions
    
    def _generate_weekly_suggestions(self, weekly_stats: Dict[str, Any], trend_analysis: Dict[str, Any], error_patterns: List[ErrorPattern]) -> List[str]:
        suggestions = []
        
        if trend_analysis.get('win_rate_trend', 0) < 0:
            suggestions.append('胜率呈下降趋势，建议复盘近期交易策略')
        if trend_analysis.get('pnl_trend', 0) < 0:
            suggestions.append('盈亏呈下降趋势，注意风险控制')
        
        for pattern in error_patterns:
            suggestions.append(f"[周度] {pattern.suggestion}")
        
        return suggestions
    
    def _generate_monthly_suggestions(self, monthly_stats: Dict[str, Any], strategy_eval: Dict[str, Any], error_patterns: List[ErrorPattern]) -> List[str]:
        suggestions = []
        
        for strategy, perf in strategy_eval.items():
            if perf.get('win_rate', 0) < 0.5:
                suggestions.append(f'策略"{strategy}"胜率较低 ({perf.get("win_rate", 0)*100:.1f}%)，建议优化或停用')
        
        for pattern in error_patterns:
            suggestions.append(f"[月度] {pattern.suggestion}")
        
        return suggestions
    
    def _group_by_date(self, trades: List[InferredTrade]) -> Dict[str, List[Dict[str, Any]]]:
        groups = {}
        for trade in trades:
            date_str = trade.exec_time.strftime('%Y-%m-%d')
            if date_str not in groups:
                groups[date_str] = []
            groups[date_str].append({
                'trade_id': trade.trade_id,
                'pnl': trade.pnl,
                'pnl_percent': trade.pnl_percent,
                'win': trade.pnl > 0,
            })
        return groups
    
    def _group_by_week(self, trades: List[InferredTrade]) -> Dict[str, List[Dict[str, Any]]]:
        groups = {}
        for trade in trades:
            iso_cal = trade.exec_time.isocalendar()
            week_str = f"{iso_cal[0]}-W{iso_cal[1]:02d}"
            if week_str not in groups:
                groups[week_str] = []
            groups[week_str].append({
                'trade_id': trade.trade_id,
                'pnl': trade.pnl,
                'pnl_percent': trade.pnl_percent,
                'win': trade.pnl > 0,
            })
        return groups
    
    def _calculate_weekly_stats(self, trades: List[InferredTrade]) -> Dict[str, Any]:
        if not trades:
            return self._empty_stats()
        
        total_trades = len(trades)
        winning_trades = len([t for t in trades if t.pnl > 0])
        total_pnl = sum(t.pnl for t in trades)
        avg_pnl_percent = sum(t.pnl_percent for t in trades) / total_trades
        
        return {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': total_trades - winning_trades,
            'win_rate': round(winning_trades / total_trades, 3),
            'total_pnl': round(total_pnl, 2),
            'avg_pnl_percent': round(avg_pnl_percent, 4),
        }
    
    def _calculate_monthly_stats(self, trades: List[InferredTrade]) -> Dict[str, Any]:
        return self._calculate_weekly_stats(trades)
    
    def _analyze_trend(self, daily_groups: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
        if len(daily_groups) < 2:
            return {'win_rate_trend': 0, 'pnl_trend': 0}
        
        sorted_dates = sorted(daily_groups.keys())
        win_rates = []
        pnls = []
        
        for date in sorted_dates:
            trades = daily_groups[date]
            win_rate = sum(1 for t in trades if t.get('win', False)) / len(trades) if trades else 0
            pnl = sum(t.get('pnl', 0) for t in trades)
            win_rates.append(win_rate)
            pnls.append(pnl)
        
        mid = len(win_rates) // 2
        win_rate_trend = sum(win_rates[mid:]) / (len(win_rates) - mid) - sum(win_rates[:mid]) / mid if mid > 0 else 0
        pnl_trend = sum(pnls[mid:]) / (len(pnls) - mid) - sum(pnls[:mid]) / mid if mid > 0 else 0
        
        return {
            'win_rate_trend': round(win_rate_trend, 4),
            'pnl_trend': round(pnl_trend, 2),
        }
    
    def _evaluate_strategies(self, trades: List[InferredTrade]) -> Dict[str, Dict[str, Any]]:
        strategy_groups = {}
        for trade in trades:
            strategy = trade.strategy_type
            if strategy not in strategy_groups:
                strategy_groups[strategy] = []
            strategy_groups[strategy].append(trade)
        
        evaluation = {}
        for strategy, group in strategy_groups.items():
            winning = [t for t in group if t.pnl > 0]
            win_rate = len(winning) / len(group)
            total_pnl = sum(t.pnl for t in group)
            avg_pnl = total_pnl / len(group)
            
            evaluation[strategy] = {
                'trade_count': len(group),
                'win_rate': round(win_rate, 3),
                'total_pnl': round(total_pnl, 2),
                'avg_pnl': round(avg_pnl, 2),
            }
        
        return evaluation
    
    def _empty_stats(self) -> Dict[str, Any]:
        return {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'win_rate': 0.0,
            'total_pnl': 0.0,
            'avg_pnl_percent': 0.0,
        }


class BatchReviewProcessor:
    """
    批量复盘处理器
    
    整合批量数据获取、智能推断、分析和报告生成的完整流程。
    
    使用示例:
        processor = BatchReviewProcessor()
        report = processor.process_daily_trades('2026-03-28')
    """
    
    def __init__(
        self,
        data_fetcher: Optional[BatchDataFetcher] = None,
        inference_engine: Optional[BatchInferenceEngine] = None,
        analyzer: Optional[BatchAnalyzer] = None,
        report_generator: Optional[BatchReportGenerator] = None,
        max_workers: int = 4
    ):
        self.data_fetcher = data_fetcher or BatchDataFetcher()
        self.inference_engine = inference_engine or BatchInferenceEngine(max_workers=max_workers)
        self.analyzer = analyzer or BatchAnalyzer()
        self.report_generator = report_generator or BatchReportGenerator()
        self.max_workers = max_workers
        self.logger = logging.getLogger(f"{__name__}.BatchReviewProcessor")
        self.timezone = ZoneInfo('Asia/Shanghai')
    
    def process_daily_trades(self, date: str, market: str = 'HK') -> DailyReport:
        self.logger.info(f"Processing daily trades for {date}, market={market}")
        
        try:
            self.logger.info("Step 1: Fetching daily trades")
            trade_data = self.data_fetcher.fetch_daily_trades(date, market)
            
            self.logger.info("Step 2: Running batch inference")
            inferred_trades = self.inference_engine.batch_infer(trade_data)
            
            self.logger.info("Step 3: Analyzing 6 dimensions")
            analysis = self.analyzer.analyze_six_dimensions(inferred_trades)
            
            self.logger.info("Step 4: Generating daily report")
            report = self.report_generator.generate_daily_report(date, inferred_trades, analysis)
            
            self.logger.info(
                f"Daily review completed: {report.total_trades} trades, "
                f"win_rate={report.win_rate:.1%}, overall_score={analysis.overall_score:.2f}"
            )
            
            return report
            
        except Exception as e:
            self.logger.error(f"Failed to process daily trades: {e}")
            raise
    
    def process_weekly_trades(self, week_start: str, week_end: str, market: str = 'HK') -> Dict[str, Any]:
        self.logger.info(f"Processing weekly trades from {week_start} to {week_end}")
        
        trade_data = self.data_fetcher.fetch_trades_by_period(week_start, week_end, market)
        inferred_trades = self.inference_engine.batch_infer(trade_data)
        report = self.report_generator.generate_weekly_report(week_start, week_end, inferred_trades)
        
        self.logger.info(f"Weekly review completed: {report['summary']['total_trades']} trades")
        return report
    
    def process_monthly_trades(self, month: str, market: str = 'HK') -> Dict[str, Any]:
        self.logger.info(f"Processing monthly trades for {month}")
        
        start_date = f"{month}-01"
        if month.endswith('-12'):
            end_date = f"{int(month[:4]) + 1}-01-01"
        else:
            next_month = int(month[5:7]) + 1
            end_date = f"{month[:4]}-{next_month:02d}-01"
        
        trade_data = self.data_fetcher.fetch_trades_by_period(start_date, end_date, market)
        inferred_trades = self.inference_engine.batch_infer(trade_data)
        report = self.report_generator.generate_monthly_report(month, inferred_trades)
        
        self.logger.info(f"Monthly review completed: {report['summary']['total_trades']} trades")
        return report
    
    def process_all_markets_daily(self, date: str) -> Dict[str, DailyReport]:
        results = {}
        for market in ['HK', 'US']:
            try:
                report = self.process_daily_trades(date, market)
                results[market] = report
            except Exception as e:
                self.logger.error(f"Failed to process {market} trades: {e}")
                results[market] = None
        return results


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    processor = BatchReviewProcessor()
    
    today = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
    
    try:
        report = processor.process_daily_trades(today, market='HK')
        print(f"\n✓ Daily report generated:")
        print(f"  Report ID: {report.report_id}")
        print(f"  Total trades: {report.total_trades}")
        print(f"  Win rate: {report.win_rate:.1%}")
        print(f"  Total PnL: {report.total_pnl:.2f}")
        print(f"  Overall score: {report.six_dimensions.overall_score:.2f}")
    except Exception as e:
        print(f"✗ Failed to generate daily report: {e}")
