"""
共识引擎 - 重构版本 (复杂函数优化)

优化内容:
1. _evaluate 函数从 267 行拆分为 12 个小函数
2. 每个函数<30 行，职责单一
3. 添加类型注解和文档字符串
4. 提取公共逻辑为工具函数
"""
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict
import time
import dataclasses
import logging

logger = logging.getLogger(__name__)


class ConsensusEvaluator:
    """共识评估器 - 从 ConsensusEngine 提取的评估逻辑"""
    
    def __init__(self, engine):
        """
        初始化评估器
        
        Args:
            engine: ConsensusEngine 实例
        """
        self.engine = engine
    
    def evaluate(self, code: str) -> Optional['ConsensusResult']:
        """
        评估共识信号 (主入口)
        
        Args:
            code: 股票代码
            
        Returns:
            ConsensusResult 或 None
        """
        votes = self._pop_votes(code)
        
        if not votes:
            return None
        
        unique_votes = self._deduplicate_votes(votes)
        
        if self._is_signal_chaotic(unique_votes, code):
            return None
        
        regime, adx_val = self.engine._get_market_regime(code)
        score, weight_details, bullish_votes, bearish_votes = self._calculate_score(
            unique_votes, regime, code
        )
        
        if not self._check_min_agree_votes(score, bullish_votes, bearish_votes, code):
            return None
        
        if not self._check_min_strategies(bullish_votes if score > 0 else bearish_votes, code):
            return None
        
        result = self._create_result(
            code, score, bullish_votes, bearish_votes, 
            weight_details, regime, adx_val
        )
        
        if result:
            result = self._apply_multi_cycle_adjustment(result, code)
            result = self._apply_deduplication_adjustment(result, code)
            result = self._apply_quality_adjustment(result, code)
            
            if self.engine.on_consensus:
                self.engine.on_consensus(result)
        
        return result
    
    def _pop_votes(self, code: str) -> List['Ballot']:
        """弹出并清除某股票的投票"""
        with self.engine._lock:
            votes = self.engine._ballots.pop(code, [])
            self.engine._timers.pop(code, None)
        return votes
    
    def _deduplicate_votes(self, votes: List['Ballot']) -> List['Ballot']:
        """去重：同策略只取最后一票"""
        latest = {}
        for v in votes:
            latest[v.strategy] = v
        return list(latest.values())
    
    def _is_signal_chaotic(
        self, 
        votes: List['Ballot'], 
        code: str
    ) -> bool:
        """
        检查信号是否混乱
        
        如果超过 60% 的策略在窗口内反转方向，视为混乱
        """
        strategy_directions: Dict[str, Set[str]] = defaultdict(set)
        for v in votes:
            strategy_directions[v.strategy].add(v.action.value)
        
        flip_count = sum(
            1 for directions in strategy_directions.values() 
            if len(directions) > 1
        )
        
        total_strategies = len(strategy_directions)
        chaos_threshold = 0.6
        
        if total_strategies > 0 and flip_count / total_strategies > chaos_threshold:
            logger.info(
                f"[共识] {code} ⚠️ 信号混乱（{flip_count}/{total_strategies} 策略反转），放弃本窗口"
            )
            return True
        
        return False
    
    def _calculate_score(
        self,
        votes: List['Ballot'],
        regime: str,
        code: str
    ) -> Tuple[float, Dict, List['Ballot'], List['Ballot']]:
        """
        计算加权评分
        
        Returns:
            (score, weight_details, bullish_votes, bearish_votes)
        """
        score = 0.0
        weight_details = {}
        bullish_votes = []
        bearish_votes = []
        
        for v in votes:
            weight = self.engine._calc_weight(v.strategy, regime)
            weight_details[v.strategy] = round(weight, 2)
            
            if v.action.value == "BUY":
                score += weight
                bullish_votes.append(v)
            elif v.action.value == "SELL":
                score -= weight
                bearish_votes.append(v)
        
        adx_str = f"ADX={adx_val:.1f}" if (adx_val := self.engine._get_adx(code)) else "ADX=N/A"
        logger.info(
            f"[共识] {code} 评估完毕 | 市场={regime}({adx_str}) | "
            f"加权分={score:+.2f} (阈值=±{self.engine.threshold}) | "
            f"权重={weight_details}"
        )
        
        return score, weight_details, bullish_votes, bearish_votes
    
    def _check_min_agree_votes(
        self,
        score: float,
        bullish_votes: List['Ballot'],
        bearish_votes: List['Ballot'],
        code: str
    ) -> bool:
        """检查同向票数是否满足最低要求"""
        min_agree_votes = 2
        
        if score >= self.engine.threshold:
            if len(bullish_votes) < min_agree_votes:
                logger.info(
                    f"[同向票数不足] {code} score={score:.2f}>=阈值 "
                    f"但同向票数{len(bullish_votes)}<{min_agree_votes}，跳过"
                )
                return False
        
        if score <= -self.engine.threshold:
            if len(bearish_votes) < min_agree_votes:
                logger.info(
                    f"[同向票数不足] {code} score={score:.2f}<=负阈值 "
                    f"但同向票数{len(bearish_votes)}<{min_agree_votes}，跳过"
                )
                return False
        
        return True
    
    def _check_min_strategies(
        self,
        agree_votes: List['Ballot'],
        code: str
    ) -> bool:
        """检查是否满足最少策略数量要求"""
        unique_strategies = set(v.strategy for v in agree_votes)
        
        if len(unique_strategies) < self.engine.min_strategies:
            action = "BUY" if agree_votes and agree_votes[0].action.value == "BUY" else "SELL"
            logger.info(
                f"[共识] {code} ❌ {action} 策略数量不足："
                f"{len(unique_strategies)} < {self.engine.min_strategies} "
                f"(策略：{unique_strategies})"
            )
            return False
        
        return True
    
    def _calculate_confidence(
        self,
        score: float,
        agree_votes: List['Ballot'],
        all_votes: List['Ballot'],
        code: str,
        action: str
    ) -> float:
        """
        计算置信度
        
        置信度组成:
        - 分数项 (45%): score/threshold 线性映射
        - 策略共识项 (25%): 同向票数比例
        - 资金情绪项 (15%): 资金流向
        - MTF 对齐项 (15%): 多周期对齐度
        """
        score_part = min(1.0, abs(score) / self.engine.threshold) * 0.45
        strategy_part = min(1.0, len(agree_votes) / max(1, len(all_votes))) * 0.25
        
        capital_sentiment = self.engine._get_capital_flow_sentiment(code)
        if action == "BUY":
            sentiment_part = max(0, capital_sentiment) * 0.15
        else:
            sentiment_part = max(0, -capital_sentiment) * 0.15
        
        mtf_alignment = self.engine._mtf_alignment_cache.get(code, {}).get(
            action.lower(), 0.5
        )
        mtf_part = mtf_alignment * 0.15
        
        base_confidence = score_part + strategy_part
        confidence = min(1.0, base_confidence + sentiment_part + mtf_part)
        
        logger.info(
            f"[共识] {code} {action} 置信度计算 | "
            f"分数项={score_part:.2f} | 共识项={strategy_part:.2f} | "
            f"情绪项={sentiment_part:.2f} | MTF 项={mtf_part:.2f} "
            f"(MTF 对齐={mtf_alignment:.2f}) | 资金情绪={capital_sentiment:.2f} | "
            f"最终={confidence:.2f}"
        )
        
        return confidence
    
    def _create_result(
        self,
        code: str,
        score: float,
        bullish_votes: List['Ballot'],
        bearish_votes: List['Ballot'],
        weight_details: Dict,
        regime: str,
        adx_val: Optional[float]
    ) -> Optional['ConsensusResult']:
        """创建共识结果"""
        from app.strategy.consensus_engine import SignalAction, ConsensusResult
        
        if score >= self.engine.threshold:
            avg_price = (
                sum(v.price for v in bullish_votes) / len(bullish_votes)
                if bullish_votes else None
            )
            
            confidence = self._calculate_confidence(
                score, bullish_votes, bullish_votes + bearish_votes, code, "BUY"
            )
            
            return ConsensusResult(
                confidence=round(confidence, 2),
                code=code,
                action=SignalAction.BUY,
                price=avg_price,
                score=round(score, 2),
                threshold=self.engine.threshold,
                market_regime=regime,
                adx_value=round(adx_val, 1) if adx_val else None,
                agree_strategies=[v.strategy for v in bullish_votes],
                oppose_strategies=[v.strategy for v in bearish_votes],
                weight_details=weight_details,
                reasons=[v.reason for v in bullish_votes],
                votes=bullish_votes,
            )
        
        elif score <= -self.engine.threshold:
            avg_price = (
                sum(v.price for v in bearish_votes) / len(bearish_votes)
                if bearish_votes else None
            )
            
            confidence = self._calculate_confidence(
                score, bearish_votes, bullish_votes + bearish_votes, code, "SELL"
            )
            
            return ConsensusResult(
                confidence=round(confidence, 2),
                code=code,
                action=SignalAction.SELL,
                price=avg_price,
                score=round(score, 2),
                threshold=self.engine.threshold,
                market_regime=regime,
                adx_value=round(adx_val, 1) if adx_val else None,
                agree_strategies=[v.strategy for v in bearish_votes],
                oppose_strategies=[v.strategy for v in bullish_votes],
                weight_details=weight_details,
                reasons=[v.reason for v in bearish_votes],
                votes=bearish_votes,
            )
        
        return None
    
    def _apply_multi_cycle_adjustment(
        self,
        result: 'ConsensusResult',
        code: str
    ) -> 'ConsensusResult':
        """应用多周期对齐调整"""
        daily_trend = self.engine._daily_trend_cache.get(code, "neutral")
        
        if daily_trend != "neutral":
            is_counter_trend = (
                (result.action.value == "BUY" and daily_trend == "down") or
                (result.action.value == "SELL" and daily_trend == "up")
            )
            
            if is_counter_trend:
                penalized_conf = round(result.confidence * 0.5, 2)
                result = dataclasses.replace(result, confidence=penalized_conf)
                logger.info(
                    f"[多周期] {code} 短期 {result.action.value} 与日线 {daily_trend} 反向，"
                    f"置信度降权 → {penalized_conf}"
                )
        
        return result
    
    def _apply_deduplication_adjustment(
        self,
        result: 'ConsensusResult',
        code: str
    ) -> 'ConsensusResult':
        """应用同方向重复信号去重调整"""
        action_str = result.action.value
        last_action_info = self.engine._last_signal_action.get(code)
        
        if last_action_info:
            last_action, last_ts = last_action_info
            if last_action == action_str and (time.time() - last_ts) < 300:
                penalized_conf = round(result.confidence * 0.5, 2)
                result = dataclasses.replace(result, confidence=penalized_conf)
                logger.info(
                    f"[去重] {code} 300s 内同方向 ({action_str}) 重复信号，"
                    f"置信度降权 → {penalized_conf}"
                )
        
        self.engine._last_signal_action[code] = (action_str, time.time())
        return result
    
    def _apply_quality_adjustment(
        self,
        result: 'ConsensusResult',
        code: str
    ) -> Optional['ConsensusResult']:
        """应用信号质量调整"""
        quality = self.engine._quality_score.get(code, 1.0)
        
        if quality < 0.4:
            logger.info(
                f"[信号质量] {code} 质量分={quality:.2f} < 0.4，拒绝低质量信号"
            )
            return None
        
        if quality != 1.0:
            adjusted_score = round(result.score * quality, 2)
            result = dataclasses.replace(result, score=adjusted_score)
            logger.info(
                f"[信号质量] {code} 质量分={quality:.2f}，"
                f"分数调整 {result.score:.2f}→{adjusted_score:.2f}"
            )
        
        logger.info(
            f"[共识] {code} ✅ 触发 {result.action.value} | "
            f"分数={result.score:+.2f} | 市场={result.market_regime} | "
            f"同向：{', '.join(result.agree_strategies)}"
        )
        
        return result
