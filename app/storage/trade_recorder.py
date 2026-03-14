"""
增强交易记录器

记录完整交易上下文，支持后续复盘和迭代改进
"""
import os
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict
import uuid

logger = logging.getLogger("ftseek.storage.trade_recorder")

# 交易记录存储目录
TRADE_RECORDS_DIR = Path('/opt/ftseek/FTSeek/data/trade_records')
TRADE_RECORDS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class TradeRecord:
    """增强版交易记录"""
    
    # 基础信息
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    code: str = ''
    timestamp: str = ''
    strategy: str = ''  # fusion_v2/fusion_v3
    
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
    exit_reason: Optional[str] = None  # 止盈/止损/信号反转
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    holding_period: Optional[int] = None  # 持有天数
    
    # 市场环境
    market_regime: str = ''  # 上涨/下跌/震荡
    volatility: float = 0.0
    
    # 复盘信息
    lessons_learned: str = ''
    is_good_trade: bool = True  # 是否符合策略
    
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
                    volatility: float = 0.0) -> TradeRecord:
        """
        记录开仓
        
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
            volatility=volatility
        )
        
        self.records.append(record)
        self._save_records()
        
        logger.info(f"记录开仓：{code} {action} @ {entry_price}")
        return record
    
    def record_close(self,
                     trade_id: str,
                     exit_price: float,
                     exit_reason: str,
                     lessons_learned: str = '') -> Optional[TradeRecord]:
        """
        记录平仓
        
        Args:
            trade_id: 交易 ID
            exit_price: 出场价
            exit_reason: 出场原因
            lessons_learned: 复盘总结
        
        Returns:
            TradeRecord: 更新后的交易记录
        """
        # 查找交易记录
        record = None
        for r in self.records:
            if r.trade_id == trade_id:
                record = r
                break
        
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
        else:  # SELL
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
        record.is_good_trade = self._evaluate_trade(record)
        
        self._save_records()
        
        logger.info(f"记录平仓：{record.code} {exit_reason} 盈亏：{pnl:.2f}")
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


# 全局单例
_recorder = None

def get_trade_recorder() -> TradeRecorder:
    """获取交易记录器单例"""
    global _recorder
    if _recorder is None:
        _recorder = TradeRecorder()
    return _recorder
