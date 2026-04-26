"""
增强交易记录器 (支持深度复盘 + 全链路追踪)

记录完整交易上下文，支持后续复盘和迭代改进
支持决策/时机/持仓/风控/心理 5 大维度复盘字段
支持 TradeChain 全链路追踪 (信号→决策→执行→结果)
"""
import os
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
import uuid

logger = logging.getLogger("ftseek.storage.trade_recorder")
logger.propagate = False


def _make_json_serializable(obj: Any) -> Any:
    """
    递归转换对象为 JSON 可序列化格式
    
    处理 mappingproxy 和其他不可序列化类型
    """
    if isinstance(obj, dict):
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_make_json_serializable(item) for item in obj]
    elif type(obj).__name__ == 'mappingproxy':
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    elif hasattr(obj, '__dict__'):
        return _make_json_serializable(obj.__dict__)
    elif isinstance(obj, (datetime,)):
        return obj.isoformat()
    else:
        return obj


# 交易记录存储目录
TRADE_RECORDS_DIR = Path('/opt/ftseek/FTSeek/data/trade_records')
TRADE_RECORDS_DIR.mkdir(parents=True, exist_ok=True)

# TradeChain 存储目录
TRADE_CHAIN_DIR = Path('/opt/ftseek/FTSeek/data/trade_chains')
TRADE_CHAIN_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class TradeRecord:
    """增强版交易记录 (支持深度复盘)"""
    
    # 基础信息
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    code: str = ''
    timestamp: str = ''
    strategy: str = ''  # fusion_v2/fusion_v3
    account_type: str = 'real'  # real/simulation
    
    # 信号信息
    signal_confidence: float = 0.0
    ai_score: float = 0.0
    factor_scores: Dict = field(default_factory=dict)
    
    # 执行信息
    action: str = ''  # BUY/SELL
    entry_price: float = 0.0
    quantity: int = 0
    position_size: float = 0.0  # 仓位占比
    
    # 风控信息
    stop_loss: float = 0.0
    take_profit: float = 0.0
    risk_reward_ratio: float = 0.0
    
    # 结果信息 (平仓后填充)
    exit_price: Optional[float] = None
    exit_timestamp: Optional[str] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    holding_period: Optional[int] = None
    
    # 市场环境
    market_regime: str = ''
    volatility: float = 0.0
    
    # ========== 决策复盘字段 (6 个) ==========
    decision_reason: str = ''  # technical/fundamental/sentiment/news/confluence/impulse
    decision_confidence: int = 5  # 1-10
    strategy_type: str = ''  # confluence_v2/fusion_v3/etc
    decision_time_spent: int = 0  # 决策耗时 (秒)
    decision_quality_score: int = 5  # 1-10
    decision_notes: str = ''
    
    # ========== 时机复盘字段 (5 个) ==========
    entry_timing_score: int = 5  # 1-10
    exit_timing_score: int = 5  # 1-10
    entry_vs_signal_delay: int = 0  # 入场相对信号延迟 (秒)
    market_phase: str = ''  # 上涨/下跌/震荡/转折
    timing_notes: str = ''
    
    # ========== 持仓复盘字段 (4 个) ==========
    position_size_actual: float = 0.0
    position_size_planned: float = 0.0
    position_adjustments: int = 0  # 调仓次数
    position_notes: str = ''
    
    # ========== 风控复盘字段 (5 个) ==========
    stop_loss_planned: float = 0.0
    stop_loss_executed: bool = True
    stop_loss_adjusted: bool = False
    max_drawdown_during_trade: float = 0.0
    risk_reward_actual: float = 0.0
    risk_notes: str = ''
    
    # ========== 心理复盘字段 (5 个) ==========
    emotion_state: str = 'calm'  # calm/anxious/excited/fearful/greedy/frustrated
    emotion_intensity: int = 3  # 1-10
    confidence_entry: int = 5  # 入场时信心 1-10
    confidence_exit: int = 5  # 出场时信心 1-10
    psychological_notes: str = ''
    
    # 复盘信息
    lessons_learned: str = ''
    is_good_trade: bool = True
    review_notes: str = ''
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'TradeRecord':
        """从字典创建"""
        return cls(**data)


class TradeRecorder:
    """交易记录器"""
    
    def __init__(self):
        self.records: List[TradeRecord] = []
        self._load_today_records()
    
    def _load_today_records(self):
        """加载今日交易记录"""
        today = datetime.now().strftime('%Y-%m-%d')
        records_file = TRADE_RECORDS_DIR / f'{today}.json'
        
        if records_file.exists():
            try:
                with open(records_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.records = [TradeRecord.from_dict(r) for r in data]
                    logger.info(f"加载今日交易记录：{len(self.records)} 条")
            except Exception as e:
                logger.error(f"加载记录失败：{e}")
    
    def record_open(self, 
                    code: str,
                    strategy: str,
                    action: str,
                    entry_price: float,
                    quantity: int,
                    signal_confidence: float,
                    ai_score: float = 0.0,
                    factor_scores: Dict = None,
                    stop_loss: float = 0.0,
                    take_profit: float = 0.0,
                    position_size: float = 0.1,
                    market_regime: str = '',
                    volatility: float = 0.0,
                    # 复盘字段 (可选)
                    decision_reason: str = 'technical',
                    decision_confidence: int = 5,
                    strategy_type: str = '',
                    emotion_state: str = 'calm',
                    account_type: str = 'real') -> TradeRecord:
        """
        记录开仓 (支持复盘字段)
        
        Args:
            code: 股票代码
            strategy: 策略名称
            action: BUY/SELL
            entry_price: 入场价
            quantity: 数量
            signal_confidence: 信号置信度
            ai_score: AI 分数
            factor_scores: 各因子分数
            stop_loss: 止损价
            take_profit: 止盈价
            position_size: 仓位占比
            market_regime: 市场环境
            volatility: 波动率
            decision_reason: 决策原因 (technical/fundamental/sentiment/news/confluence/impulse)
            decision_confidence: 决策信心 1-10
            strategy_type: 策略类型
            emotion_state: 心理状态 (默认 calm)
            account_type: 账户类型 (real/simulation)
        
        Returns:
            TradeRecord: 交易记录
        """
        # 计算风险收益比
        if stop_loss > 0 and take_profit > 0:
            risk = abs(entry_price - stop_loss)
            reward = abs(take_profit - entry_price)
            risk_reward_ratio = reward / risk if risk > 0 else 0
        else:
            risk_reward_ratio = 0
        
        record = TradeRecord(
            code=code,
            timestamp=datetime.now().isoformat(),
            strategy=strategy,
            action=action,
            entry_price=entry_price,
            quantity=quantity,
            signal_confidence=signal_confidence,
            ai_score=ai_score,
            factor_scores=factor_scores or {},
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward_ratio=risk_reward_ratio,
            position_size=position_size,
            market_regime=market_regime,
            volatility=volatility,
            # 复盘字段
            decision_reason=decision_reason,
            decision_confidence=decision_confidence,
            strategy_type=strategy_type,
            emotion_state=emotion_state,
            account_type=account_type,
            stop_loss_planned=stop_loss,
            position_size_actual=position_size,
            position_size_planned=position_size,
            confidence_entry=decision_confidence
        )
        
        self.records.append(record)
        self._save_records()
        
        logger.info(f"记录开仓：{code} {action} @ {entry_price}")
        return record
    
    def record_close(self,
                     trade_id: str,
                     exit_price: float,
                     exit_reason: str,
                     lessons_learned: str = '',
                     exit_timing_score: int = 5,
                     confidence_exit: int = 5,
                     emotion_state: str = 'calm',
                     stop_loss_executed: bool = True) -> Optional[TradeRecord]:
        """
        记录平仓 (支持复盘字段)
        
        Args:
            trade_id: 交易 ID
            exit_price: 出场价
            exit_reason: 出场原因
            lessons_learned: 复盘总结
            exit_timing_score: 出场时机评分 1-10
            confidence_exit: 出场时信心 1-10
            emotion_state: 出场时心理状态
            stop_loss_executed: 止损是否执行
        
        Returns:
            TradeRecord: 更新后的交易记录
        """
        record = self.get_record(trade_id)
        
        if not record:
            logger.error(f"未找到交易记录：{trade_id}")
            return None
        
        # 更新平仓信息
        record.exit_price = exit_price
        record.exit_timestamp = datetime.now().isoformat()
        record.exit_reason = exit_reason
        
        # 计算盈亏
        if record.action == 'BUY':
            pnl = (exit_price - record.entry_price) * record.quantity
            pnl_percent = (exit_price - record.entry_price) / record.entry_price
        else:
            pnl = (record.entry_price - exit_price) * record.quantity
            pnl_percent = (record.entry_price - exit_price) / record.entry_price
        
        record.pnl = pnl
        record.pnl_percent = pnl_percent
        
        # 计算持有天数
        entry_date = datetime.strptime(record.timestamp[:19], '%Y-%m-%dT%H:%M:%S')
        exit_date = datetime.now()
        record.holding_period = (exit_date - entry_date).days
        
        # 复盘信息
        record.lessons_learned = lessons_learned
        record.exit_timing_score = exit_timing_score
        record.confidence_exit = confidence_exit
        record.emotion_state = emotion_state
        record.stop_loss_executed = stop_loss_executed
        record.is_good_trade = self._evaluate_trade(record)
        
        # 计算实际风险收益比
        if record.stop_loss_planned > 0:
            risk = abs(record.entry_price - record.stop_loss_planned)
            reward = abs(record.pnl)
            record.risk_reward_actual = reward / risk if risk > 0 else 0
        
        self._save_records()
        
        logger.info(f"记录平仓：{record.code} {exit_reason} 盈亏：{pnl:.2f}")
        return record
    
    def update_review(self,
                      trade_id: str,
                      entry_timing_score: int = None,
                      exit_timing_score: int = None,
                      stop_loss_executed: bool = None,
                      position_adjustments: int = None,
                      max_drawdown_during_trade: float = None,
                      emotion_intensity: int = None,
                      psychological_notes: str = None,
                      timing_notes: str = None,
                      position_notes: str = None,
                      risk_notes: str = None,
                      decision_notes: str = None,
                      review_notes: str = None) -> Optional[TradeRecord]:
        """
        交易后补充复盘 (通过 Dashboard 界面)
        
        Args:
            trade_id: 交易 ID
            entry_timing_score: 入场时机评分 1-10
            exit_timing_score: 出场时机评分 1-10
            stop_loss_executed: 是否执行止损
            position_adjustments: 调仓次数
            max_drawdown_during_trade: 持仓期间最大回撤
            emotion_intensity: 情绪强度 1-10
            psychological_notes: 心理备注
            timing_notes: 时机备注
            position_notes: 持仓备注
            risk_notes: 风控备注
            decision_notes: 决策备注
            review_notes: 复盘备注
        
        Returns:
            TradeRecord: 更新后的交易记录
        """
        record = self.get_record(trade_id)
        
        if not record:
            logger.error(f"未找到交易记录：{trade_id}")
            return None
        
        # 更新复盘字段
        if entry_timing_score is not None:
            record.entry_timing_score = entry_timing_score
        if exit_timing_score is not None:
            record.exit_timing_score = exit_timing_score
        if stop_loss_executed is not None:
            record.stop_loss_executed = stop_loss_executed
        if position_adjustments is not None:
            record.position_adjustments = position_adjustments
        if max_drawdown_during_trade is not None:
            record.max_drawdown_during_trade = max_drawdown_during_trade
        if emotion_intensity is not None:
            record.emotion_intensity = emotion_intensity
        if psychological_notes is not None:
            record.psychological_notes = psychological_notes
        if timing_notes is not None:
            record.timing_notes = timing_notes
        if position_notes is not None:
            record.position_notes = position_notes
        if risk_notes is not None:
            record.risk_notes = risk_notes
        if decision_notes is not None:
            record.decision_notes = decision_notes
        if review_notes is not None:
            record.review_notes = review_notes
        
        self._save_records()
        logger.info(f"更新交易复盘：{trade_id}")
        return record
    
    def _evaluate_trade(self, record: TradeRecord) -> bool:
        """评估交易质量"""
        # 符合策略的交易
        if record.exit_reason == '止盈':
            return True
        elif record.exit_reason == '止损':
            # 止损是风控的一部分，也算好交易
            return True
        elif record.exit_reason == '信号反转':
            # 按信号操作，算好交易
            return True
        else:
            # 其他原因需要人工评估
            return False
    
    def _save_records(self):
        """保存交易记录"""
        today = datetime.now().strftime('%Y-%m-%d')
        records_file = TRADE_RECORDS_DIR / f'{today}.json'
        
        data = [r.to_dict() for r in self.records]
        
        with open(records_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def get_today_summary(self) -> Dict:
        """获取今日交易汇总"""
        today_records = [r for r in self.records if r.timestamp.startswith(datetime.now().strftime('%Y-%m-%d'))]
        
        if not today_records:
            return {'trades': 0, 'pnl': 0, 'win_rate': 0}
        
        closed_trades = [r for r in today_records if r.exit_price is not None]
        winning_trades = [r for r in closed_trades if r.pnl and r.pnl > 0]
        
        total_pnl = sum(r.pnl for r in closed_trades if r.pnl)
        win_rate = len(winning_trades) / len(closed_trades) if closed_trades else 0
        
        return {
            'total_trades': len(today_records),
            'closed_trades': len(closed_trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(closed_trades) - len(winning_trades),
            'total_pnl': total_pnl,
            'win_rate': round(win_rate, 4),
            'avg_pnl': total_pnl / len(closed_trades) if closed_trades else 0
        }
    
    def get_all_records(self) -> List[TradeRecord]:
        """获取所有交易记录"""
        return self.records
    
    def get_record(self, trade_id: str) -> Optional[TradeRecord]:
        """根据 ID 获取交易记录"""
        for r in self.records:
            if r.trade_id == trade_id:
                return r
        return None
    
    def get_records_by_date(self, date_str: str) -> List[TradeRecord]:
        """获取指定日期的交易记录"""
        return [r for r in self.records if r.timestamp.startswith(date_str)]
    
    def get_records_by_account(self, account_type: str) -> List[TradeRecord]:
        """获取指定账户类型的交易记录"""
        return [r for r in self.records if r.account_type == account_type]
    
    def get_records_with_pnl(self) -> List[TradeRecord]:
        """获取已平仓的交易记录"""
        return [r for r in self.records if r.exit_price is not None]
    
    def get_review_statistics(self, start_date: str = None, end_date: str = None) -> Dict:
        """
        获取复盘统计数据
        
        Args:
            start_date: 开始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD
        
        Returns:
            统计数据字典
        """
        records = self.get_records_with_pnl()
        
        # 日期过滤
        if start_date:
            records = [r for r in records if r.timestamp >= start_date]
        if end_date:
            records = [r for r in records if r.timestamp <= end_date]
        
        if not records:
            return {'total_trades': 0}
        
        total_trades = len(records)
        winning_trades = len([r for r in records if r.pnl and r.pnl > 0])
        losing_trades = len([r for r in records if r.pnl and r.pnl < 0])
        
        total_pnl = sum(r.pnl for r in records if r.pnl)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0
        
        # 情绪分布
        emotion_counts = {}
        for r in records:
            emotion = r.emotion_state
            emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1
        
        # 决策原因分布
        decision_counts = {}
        for r in records:
            reason = r.decision_reason or 'unknown'
            decision_counts[reason] = decision_counts.get(reason, 0) + 1
        
        return {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'total_pnl': total_pnl,
            'win_rate': round(win_rate, 4),
            'avg_pnl': total_pnl / total_trades if total_trades > 0 else 0,
            'emotion_distribution': emotion_counts,
            'decision_reason_distribution': decision_counts,
            'avg_decision_confidence': sum(r.decision_confidence for r in records) / total_trades,
            'avg_entry_timing_score': sum(r.entry_timing_score for r in records) / total_trades,
            'avg_exit_timing_score': sum(r.exit_timing_score for r in records) / total_trades,
            'stop_loss_execution_rate': len([r for r in records if r.stop_loss_executed]) / total_trades,
            'impulse_trade_ratio': len([r for r in records if r.decision_reason == 'impulse']) / total_trades
        }


# 全局单例
_recorder = None

def get_trade_recorder() -> TradeRecorder:
    """获取交易记录器单例"""
    global _recorder
    if _recorder is None:
        _recorder = TradeRecorder()
    return _recorder


def record_trade(stock_code: str,
                 side: str,
                 quantity: int,
                 price: float,
                 decision_reason: str = 'technical',
                 decision_confidence: int = 8,
                 strategy_type: str = '',
                 emotion_state: str = 'calm',
                 **kwargs) -> TradeRecord:
    """
    便捷函数：记录交易 (方式 1: 交易时自动记录)
    
    使用示例:
        trade_recorder.record(
            stock_code="00700.HK",
            side="buy",
            quantity=100,
            price=380.5,
            decision_reason="technical",
            decision_confidence=8,
            strategy_type="confluence_v2",
            emotion_state="calm"
        )
    """
    recorder = get_trade_recorder()
    action = 'BUY' if side.lower() in ['buy', 'long'] else 'SELL'
    
    return recorder.record_open(
        code=stock_code,
        strategy=kwargs.get('strategy', 'manual'),
        action=action,
        entry_price=price,
        quantity=quantity,
        signal_confidence=kwargs.get('signal_confidence', decision_confidence / 10.0),
        decision_reason=decision_reason,
        decision_confidence=decision_confidence,
        strategy_type=strategy_type,
        emotion_state=emotion_state,
        account_type=kwargs.get('account_type', 'real'),
        stop_loss=kwargs.get('stop_loss', 0),
        take_profit=kwargs.get('take_profit', 0),
        position_size=kwargs.get('position_size', 0.1)
    )


def update_trade_review(trade_id: str, **kwargs) -> Optional[TradeRecord]:
    """
    便捷函数：交易后补充复盘 (方式 2: Dashboard 界面)

    使用示例:
        trade_recorder.update_review(
            trade_id=123,
            entry_timing_score=7,
            exit_timing_score=5,
            stop_loss_executed=True,
            review_notes="买早了，应等待回调"
        )
    """
    recorder = get_trade_recorder()
    return recorder.update_review(trade_id, **kwargs)


# ==================== TradeChain 全链路记录器 ====================

from app.analytics.trade_chain import TradeChain, SignalLayer, DecisionLayer, ExecutionLayer, ResultLayer


class TradeChainRecorder:
    """
    交易全链路记录器

    记录从信号产生到交易结束的完整链路:
    1. 信号层 - 信号分数、置信度、共识权重、风控检查
    2. 决策层 - 决策原因、信心、仓位计划与调整
    3. 执行层 - 订单时间、预期价、实际成交价、滑点、延迟
    4. 结果层 - 持仓周期、最大浮盈/浮亏、出场原因、净收益
    """

    def __init__(self):
        self.chains: Dict[str, TradeChain] = {}
        self._load_all_chains()

    def _load_all_chains(self):
        """加载所有交易链路"""
        if not TRADE_CHAIN_DIR.exists():
            return

        for file in TRADE_CHAIN_DIR.glob('*.json'):
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for item in data:
                        chain = TradeChain.from_dict(item)
                        self.chains[chain.trade_id] = chain
            except Exception as e:
                logger.debug(f"跳过 TradeChain 文件 {file}: {e}")

        logger.info(f"加载 TradeChain: {len(self.chains)} 条")

    def start_chain(self,
                    trade_id: str,
                    code: str,
                    signal_data: Dict[str, Any],
                    decision_data: Dict[str, Any],
                    action: str = 'BUY',
                    quantity: int = 0) -> TradeChain:
        """
        启动交易链路 (信号产生 + 决策完成时调用)

        Args:
            trade_id: 交易 ID
            code: 股票代码
            signal_data: 信号层数据
                - signal_id, signal_score, signal_confidence
                - consensus_passed, consensus_weights
                - risk_check_passed, risk_reason
                - market_regime, volatility_atr, ai_sentiment_score
            decision_data: 决策层数据
                - decision_reason, decision_confidence
                - position_planned, position_adjusted
                - position_adjust_reason, weight_config_snapshot
                - account_state
            action: BUY/SELL
            quantity: 数量

        Returns:
            TradeChain: 交易链路
        """
        chain = TradeChain()
        chain.trade_id = trade_id
        chain.code = code
        chain.action = action
        chain.quantity = quantity
        chain.created_at = datetime.now().isoformat()
        chain.updated_at = chain.created_at

        # 信号层
        chain.signal = SignalLayer(
            signal_id=signal_data.get('signal_id', ''),
            signal_time=datetime.now().isoformat(),
            signal_score=signal_data.get('signal_score', 0.0),
            signal_confidence=signal_data.get('signal_confidence', 0.0),
            consensus_passed=signal_data.get('consensus_passed', False),
            consensus_weights=signal_data.get('consensus_weights', {}),
            risk_check_passed=signal_data.get('risk_check_passed', False),
            risk_reason=signal_data.get('risk_reason', ''),
            market_regime=signal_data.get('market_regime', ''),
            volatility_atr=signal_data.get('volatility_atr', 0.0),
            ai_sentiment_score=signal_data.get('ai_sentiment_score', 0.0),
        )

        # 决策层
        chain.decision = DecisionLayer(
            decision_reason=decision_data.get('decision_reason', 'technical'),
            decision_confidence=decision_data.get('decision_confidence', 5),
            position_planned=decision_data.get('position_planned', 0.0),
            position_adjusted=decision_data.get('position_adjusted', 0.0),
            position_adjust_reason=decision_data.get('position_adjust_reason', ''),
            weight_config_snapshot=decision_data.get('weight_config_snapshot', {}),
            account_state=decision_data.get('account_state', {}),
        )

        self.chains[trade_id] = chain
        self._save_chain(chain)

        logger.info(f"启动 TradeChain: {trade_id} - {code} - {action}")
        return chain

    def record_execution(self,
                         trade_id: str,
                         execution_data: Dict[str, Any]) -> Optional[TradeChain]:
        """
        记录执行层数据 (订单成交时调用)

        Args:
            trade_id: 交易 ID
            execution_data: 执行层数据
                - expected_price, actual_fill_price
                - slippage_bps, execution_latency_ms
                - fill_reason (market/limit/partial)
                - commission, stamp_duty, transaction_fee, total_cost

        Returns:
            TradeChain: 更新后的交易链路
        """
        chain = self.chains.get(trade_id)
        if not chain:
            logger.error(f"未找到 TradeChain: {trade_id}")
            return None

        chain.execution = ExecutionLayer(
            order_time=datetime.now().isoformat(),
            expected_price=execution_data.get('expected_price', 0.0),
            actual_fill_price=execution_data.get('actual_fill_price', 0.0),
            slippage_bps=execution_data.get('slippage_bps', 0.0),
            execution_latency_ms=execution_data.get('execution_latency_ms', 0),
            fill_reason=execution_data.get('fill_reason', 'market'),
            commission=execution_data.get('commission', 0.0),
            stamp_duty=execution_data.get('stamp_duty', 0.0),
            transaction_fee=execution_data.get('transaction_fee', 0.0),
            total_cost=execution_data.get('total_cost', 0.0),
        )

        chain.updated_at = datetime.now().isoformat()
        self._save_chain(chain)

        logger.info(f"记录执行：{trade_id} - 成交价={execution_data.get('actual_fill_price', 0.0):.2f}")
        return chain

    def complete_chain(self,
                       trade_id: str,
                       result_data: Dict[str, Any],
                       code: str = None) -> Optional[TradeChain]:
        """
        完成交易链路 (平仓时调用)

        Args:
            trade_id: 交易 ID (如果为 None，则通过 code 查找未关闭的链路)
            code: 股票代码 (当 trade_id 为 None 时使用)
            result_data: 结果层数据
                - exit_price, exit_reason
                - pnl, pnl_percent
                - max_profit_during, max_loss_during
                - holding_period_minutes

        Returns:
            TradeChain: 更新后的交易链路
        """
        # 如果 trade_id 为 None，尝试通过 code 查找
        if not trade_id and code:
            trade_id = self._find_open_chain_by_code(code)
            if not trade_id:
                logger.warning(f"未找到 code={code} 的开放 TradeChain，创建新链路")
                return None

        chain = self.chains.get(trade_id)
        if not chain:
            logger.error(f"未找到 TradeChain: {trade_id}")
            return None

        # 计算持仓分钟数
        holding_minutes = result_data.get('holding_period_minutes', 0)
        if holding_minutes == 0 and chain.execution.order_time:
            try:
                entry_time = datetime.fromisoformat(chain.execution.order_time)
                exit_time = datetime.now()
                holding_minutes = int((exit_time - entry_time).total_seconds() / 60)
            except Exception:
                holding_minutes = 0

        # 计算总成本
        total_cost = chain.execution.total_cost

        # 创建结果层
        chain.result = ResultLayer(
            exit_time=datetime.now().isoformat(),
            exit_price=result_data.get('exit_price', 0.0),
            exit_reason=result_data.get('exit_reason', 'manual'),
            holding_period_minutes=holding_minutes,
            max_profit_during=result_data.get('max_profit_during', 0.0),
            max_loss_during=result_data.get('max_loss_during', 0.0),
            pnl=result_data.get('pnl', 0.0),
            pnl_after_fee=result_data.get('pnl', 0.0) - total_cost,
            pnl_percent=result_data.get('pnl_percent', 0.0),
            risk_reward_actual=abs(result_data.get('pnl', 0.0)) / total_cost if total_cost > 0 else 0,
            max_drawdown_during=result_data.get('max_loss_during', 0.0),
        )

        chain.is_closed = True
        chain.is_profitable = result_data.get('pnl', 0.0) > 0
        chain.updated_at = datetime.now().isoformat()

        self._save_chain(chain)

        pnl_status = "盈利" if chain.is_profitable else "亏损"
        logger.info(f"完成 TradeChain: {trade_id} - {pnl_status} {result_data.get('pnl', 0.0):.2f}")
        return chain

    def _save_chain(self, chain: TradeChain, max_retries: int = 3):
        """保存单个交易链路（带重试机制）- 同时保存到文件和数据库"""
        import tempfile
        import shutil

        # 保存到文件 (向后兼容)
        for attempt in range(max_retries):
            try:
                date_str = chain.created_at[:10]  # YYYY-MM-DD
                file_path = TRADE_CHAIN_DIR / f'{date_str}.json'

                # 加载当日数据（带损坏恢复）
                chains_data = []
                if file_path.exists():
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            chains_data = json.load(f)
                    except json.JSONDecodeError:
                        # 文件损坏，备份并重置
                        backup_path = file_path.with_suffix('.json.corrupt')
                        shutil.copy2(file_path, backup_path)
                        logger.warning(f"TradeChain 文件损坏，已备份到 {backup_path}")
                        chains_data = []

                # 更新或添加
                found = False
                for i, item in enumerate(chains_data):
                    if item.get('trade_id') == chain.trade_id:
                        chains_data[i] = chain.to_dict()
                        found = True
                        break

                if not found:
                    chains_data.append(chain.to_dict())

                # 原子写入：先写临时文件，再重命名
                tmp_fd, tmp_path = tempfile.mkstemp(dir=TRADE_CHAIN_DIR, suffix='.tmp')
                try:
                    # 转换不可序列化对象 (mappingproxy 等)
                    chains_data_clean = _make_json_serializable(chains_data)
                    with os.fdopen(tmp_fd, 'w', encoding='utf-8') as tmp_f:
                        json.dump(chains_data_clean, tmp_f, indent=2, ensure_ascii=False)
                    shutil.move(tmp_path, str(file_path))
                except Exception:
                    # 清理临时文件
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    raise

                break  # 成功保存到文件，退出重试循环

            except json.JSONDecodeError as e:
                logger.warning(f"保存 TradeChain JSON 解析失败 (尝试 {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    import time
                    time.sleep(0.1 * (attempt + 1))
                else:
                    logger.error(f"保存 TradeChain 失败（重试{max_retries}次后）: {e}")
            except Exception as e:
                logger.error(f"保存 TradeChain 失败：{e}")
                if attempt < max_retries - 1:
                    import time
                    time.sleep(0.1 * (attempt + 1))
                else:
                    logger.error(f"保存 TradeChain 失败（重试{max_retries}次后）: {e}")

        # 同时保存到数据库 (UTR 统一模型)
        self._save_chain_to_db(chain)


    def _save_chain_to_db(self, chain: TradeChain) -> bool:
        """
        保存 TradeChain 到数据库 (UTR 统一模型)

        Args:
            chain: TradeChain 对象

        Returns:
            是否成功
        """
        try:
            from app.storage.database import get_database
            import json

            db = get_database()

            # 转换为 UTR 格式的字典
            utr_data = {
                'trade_id': chain.trade_id,
                'code': chain.code,
                'action': 'BUY',  # 需要从外部传入，这里默认
                'quantity': 100,  # 需要从外部传入
                'account_type': 'real',
                'strategy': chain.decision.decision_reason if chain.decision else '',
                'status': 'closed' if chain.is_closed else 'open',
                'is_closed': chain.is_closed,
                'is_profitable': chain.is_profitable,
                'days_ago': chain.days_ago,
                'created_at': chain.created_at,
                'updated_at': chain.updated_at,
                # 四层数据转换为 JSON 字符串 (先清理不可序列化对象)
                'signal_layer': json.dumps(_make_json_serializable(chain.signal.to_dict() if chain.signal else {}), ensure_ascii=False),
                'decision_layer': json.dumps(_make_json_serializable(chain.decision.to_dict() if chain.decision else {}), ensure_ascii=False),
                'execution_layer': json.dumps(_make_json_serializable(chain.execution.to_dict() if chain.execution else {}), ensure_ascii=False),
                'result_layer': json.dumps(_make_json_serializable(chain.result.to_dict() if chain.result else None), ensure_ascii=False) if chain.result else None,
            }

            return db.save_trade_chain(utr_data)

        except Exception as e:
            logger.error(f"保存到数据库失败：{e}")
            return False

    def get_chain(self, trade_id: str) -> Optional[TradeChain]:
        """根据 ID 获取交易链路"""
        return self.chains.get(trade_id)

    def _find_open_chain_by_code(self, code: str) -> Optional[str]:
        """根据代码查找未关闭的 TradeChain ID"""
        for trade_id, chain in self.chains.items():
            if chain.code == code and not chain.is_closed:
                return trade_id
        return None

    def get_all_chains(self, start_date: str = None, end_date: str = None) -> List[TradeChain]:
        """获取所有交易链路 (支持日期过滤)"""
        chains = list(self.chains.values())

        if start_date:
            chains = [c for c in chains if c.created_at >= start_date]
        if end_date:
            chains = [c for c in chains if c.created_at <= end_date]

        return chains

    def get_statistics(self, start_date: str = None, end_date: str = None) -> Dict:
        """
        获取交易链路统计

        Returns:
            统计字典：
            - total_trades: 总交易数
            - closed_trades: 已平仓数
            - win_rate: 胜率
            - total_pnl: 总盈亏
            - avg_pnl: 平均盈亏
            - avg_slippage_bps: 平均滑点
            - avg_latency_ms: 平均延迟
        """
        chains = self.get_all_chains(start_date, end_date)
        closed_chains = [c for c in chains if c.is_closed]

        if not closed_chains:
            return {'total_trades': len(chains), 'closed_trades': 0, 'win_rate': 0}

        winning_chains = [c for c in closed_chains if c.is_profitable]
        total_pnl = sum(c.result.pnl for c in closed_chains if c.result)

        # 计算平均滑点和延迟
        avg_slippage = sum(c.execution.slippage_bps for c in closed_chains if c.execution) / len(closed_chains)
        avg_latency = sum(c.execution.execution_latency_ms for c in closed_chains if c.execution) / len(closed_chains)

        return {
            'total_trades': len(chains),
            'closed_trades': len(closed_chains),
            'winning_trades': len(winning_chains),
            'losing_trades': len(closed_chains) - len(winning_chains),
            'win_rate': round(len(winning_chains) / len(closed_chains), 4),
            'total_pnl': round(total_pnl, 2),
            'avg_pnl': round(total_pnl / len(closed_chains), 2),
            'avg_slippage_bps': round(avg_slippage, 2),
            'avg_latency_ms': round(avg_latency, 0),
        }


# 全局单例
_chain_recorder = None

def get_trade_chain_recorder() -> TradeChainRecorder:
    """获取 TradeChain 记录器单例"""
    global _chain_recorder
    if _chain_recorder is None:
        _chain_recorder = TradeChainRecorder()
    return _chain_recorder
