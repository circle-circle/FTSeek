#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信号追踪模块 (Signal Tracker)

功能:
1. 记录每个信号的完整生命周期
2. 存储置信度、分数、策略、执行状态、盈亏
3. 按日存储到 JSON 文件
4. 支持查询和统计

数据结构:
- 信号生成时：记录置信度、分数、策略
- 信号执行时：记录执行状态、成交价
- 信号平仓后：填充盈亏、持有时间
"""

import json
import logging
import os
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict, field
from collections import defaultdict

logger = logging.getLogger("ftseek.signal_tracker")
logger.propagate = False  # 防止日志重复

# 信号记录存储目录
SIGNAL_RECORDS_DIR = Path('/opt/ftseek/FTSeek/data/signal_records')
SIGNAL_RECORDS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class SignalRecord:
    """信号记录数据类"""
    
    # 基础信息
    signal_id: str
    code: str
    timestamp: str  # ISO 格式
    strategy: str
    action: str  # BUY/SELL
    
    # 信号质量
    confidence: float  # 置信度 (0-1)
    consensus_score: float  # 共识分数
    strategies: List[str]  # 参与策略列表
    
    # 价格信息
    signal_price: float
    quantity: int
    
    # 执行结果 (执行后填充)
    executed: bool = False
    execution_price: Optional[float] = None
    execution_time: Optional[str] = None
    reject_reason: Optional[str] = None
    
    # 平仓结果 (平仓后填充)
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    holding_period_minutes: Optional[int] = None
    
    # 评估
    profitable: Optional[bool] = None
    confidence_correct: Optional[bool] = None  # 高置信度是否对应盈利
    
    # 市场状态
    market_regime: Optional[str] = None
    adx: Optional[float] = None
    
    # 多时间框架对齐 (P1 新增)
    mtf_alignment: Optional[str] = None  # 多时间框架对齐状态：BULLISH/BEARISH/NEUTRAL
    
    # 情绪评分 (P1 新增)
    sentiment_score: Optional[float] = None  # 情绪评分 (-1 到 1)
    
    # 信号质量评分 (P1 新增)
    quality_score: Optional[float] = None  # 信号质量评分 (0-1)
    
    # 持仓期间极值 (P1 新增)
    max_drawdown: Optional[float] = None  # 持仓期间最大回撤 (%)
    max_profit: Optional[float] = None  # 持仓期间最大盈利 (%)
    
    # 元数据
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # Trace ID (2026-04-08 新增：用于追踪完整链路)
    trace_id: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'SignalRecord':
        """从字典创建"""
        return cls(**data)


class SignalTracker:
    """信号追踪器"""
    
    def __init__(self):
        self.records: Dict[str, SignalRecord] = {}  # signal_id -> record
        self.daily_records: Dict[str, List[str]] = defaultdict(list)  # date -> [signal_ids]
        self._load_today_records()
        logger.info(f"信号追踪器已初始化 | 今日已记录 {len(self.records)} 个信号")
    
    def _load_today_records(self):
        """加载今日信号记录"""
        today = datetime.now().strftime('%Y-%m-%d')
        records_file = self._get_today_file()
        
        if records_file.exists():
            try:
                with open(records_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for record_data in data:
                        record = SignalRecord.from_dict(record_data)
                        self.records[record.signal_id] = record
                        self.daily_records[today].append(record.signal_id)
                logger.info(f"加载今日信号记录：{len(self.records)} 条")
            except Exception as e:
                logger.error(f"加载记录失败：{e}")
    
    def _get_today_file(self) -> Path:
        """获取今日记录文件路径"""
        today = datetime.now().strftime('%Y-%m-%d')
        return SIGNAL_RECORDS_DIR / f'signals_{today}.json'
    
    def record_signal(self, 
                     code: str,
                     strategy: str,
                     action: str,
                     price: float,
                     quantity: int,
                     confidence: float,
                     consensus_score: float,
                     strategies: List[str],
                     market_regime: str = None,
                     adx: float = None,
                     mtf_alignment: str = None,
                     sentiment_score: float = None,
                     quality_score: float = None,
                     trace_id: str = None) -> str:
        """
        记录新信号
        
        Args:
            code: 股票代码
            strategy: 策略名称
            action: BUY/SELL
            price: 信号价格
            quantity: 数量
            confidence: 置信度
            consensus_score: 共识分数
            strategies: 参与策略列表
            market_regime: 市场状态
            adx: ADX 值
            mtf_alignment: 多时间框架对齐状态 (BULLISH/BEARISH/NEUTRAL)
            sentiment_score: 情绪评分 (-1 到 1)
            quality_score: 信号质量评分 (0-1)
            trace_id: 追踪 ID (2026-04-08 新增)
        
        Returns:
            str: signal_id
        """
        signal_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        
        record = SignalRecord(
            signal_id=signal_id,
            code=code,
            timestamp=timestamp,
            strategy=strategy,
            action=action,
            confidence=confidence,
            consensus_score=consensus_score,
            strategies=strategies,
            signal_price=price,
            quantity=quantity,
            market_regime=market_regime,
            adx=adx,
            mtf_alignment=mtf_alignment,
            sentiment_score=sentiment_score,
            quality_score=quality_score,
            trace_id=trace_id,
        )
        
        self.records[signal_id] = record
        today = datetime.now().strftime('%Y-%m-%d')
        self.daily_records[today].append(signal_id)
        
        # 异步保存到文件
        self._save_today_records()
        
        logger.debug(f"[{code}] 信号已记录：{signal_id[:8]}... | 置信度={confidence:.2f} | 分数={consensus_score:.2f}")
        
        return signal_id
    
    def record_execution(self, 
                        signal_id: str,
                        executed: bool,
                        execution_price: float = None,
                        execution_time: str = None,
                        reject_reason: str = None):
        """
        记录信号执行结果
        
        Args:
            signal_id: 信号 ID
            executed: 是否执行成功
            execution_price: 成交价
            execution_time: 执行时间
            reject_reason: 拒绝原因 (如未执行)
        """
        if signal_id not in self.records:
            logger.warning(f"信号未找到：{signal_id}")
            return
        
        record = self.records[signal_id]
        record.executed = executed
        record.execution_price = execution_price
        record.execution_time = execution_time or datetime.now().isoformat()
        record.reject_reason = reject_reason
        record.updated_at = datetime.now().isoformat()
        
        self._save_today_records()
        
        if executed:
            logger.debug(f"[{record.code}] 信号执行成功：{execution_price}")
        else:
            logger.debug(f"[{record.code}] 信号执行失败：{reject_reason}")
    
    def record_exit(self,
                   signal_id: str,
                   exit_price: float,
                   exit_time: str = None,
                   pnl: float = None,
                   pnl_pct: float = None,
                   max_drawdown: float = None,
                   max_profit: float = None):
        """
        记录平仓结果
        
        Args:
            signal_id: 信号 ID
            exit_price: 平仓价
            exit_time: 平仓时间
            pnl: 盈亏金额
            pnl_pct: 盈亏比例
            max_drawdown: 持仓期间最大回撤 (%)
            max_profit: 持仓期间最大盈利 (%)
        """
        if signal_id not in self.records:
            logger.warning(f"信号未找到：{signal_id}")
            return
        
        record = self.records[signal_id]
        
        if not record.executed:
            logger.warning(f"信号未执行，无法记录平仓：{signal_id}")
            return
        
        record.exit_price = exit_price
        record.exit_time = exit_time or datetime.now().isoformat()
        record.pnl = pnl
        record.pnl_pct = pnl_pct
        
        # 新增：持仓期间极值
        record.max_drawdown = max_drawdown
        record.max_profit = max_profit
        
        # 计算持有时间
        if record.execution_time:
            exec_time = datetime.fromisoformat(record.execution_time)
            exit_dt = datetime.fromisoformat(record.exit_time)
            record.holding_period_minutes = int((exit_dt - exec_time).total_seconds() / 60)
        
        # 评估
        record.profitable = pnl > 0 if pnl is not None else None
        record.confidence_correct = None
        if record.profitable is not None and record.confidence >= 0.6:
            record.confidence_correct = record.profitable  # 高置信度是否对应盈利
        
        record.updated_at = datetime.now().isoformat()
        
        self._save_today_records()
        
        logger.debug(f"[{record.code}] 平仓记录：盈亏={pnl:.2f} | 持有={record.holding_period_minutes}分钟")
    
    def _save_today_records(self):
        """保存今日记录到文件"""
        today = datetime.now().strftime('%Y-%m-%d')
        records_file = self._get_today_file()
        
        try:
            today_records = [
                self.records[sid].to_dict() 
                for sid in self.daily_records[today]
                if sid in self.records
            ]
            
            # 临时文件 + 重命名，确保原子性
            temp_file = records_file.with_suffix('.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(today_records, f, indent=2, ensure_ascii=False)
            
            temp_file.rename(records_file)
            
        except Exception as e:
            logger.error(f"保存记录失败：{e}")
    
    def record_exit_by_code(self,
                            code: str,
                            exit_price: float,
                            pnl: float,
                            exit_reason: str = None,
                            exit_time: str = None):
        """
        通过股票代码回填平仓结果到最近未平仓的已执行信号记录。
        用于止损/止盈触发路径（此时没有原始 signal_id）。

        Args:
            code:       股票代码
            exit_price: 平仓价
            pnl:        盈亏金额
            exit_reason: 平仓原因标签（如 'stop_loss' / 'take_profit' / 'trailing_stop'）
            exit_time:  平仓时间（ISO格式，缺省取当前时间）
        """
        today_signals = self.get_today_signals()
        # 找该 code 最近一条已执行但尚未记录平仓的开仓信号
        candidates = [
            r for r in today_signals
            if r.code == code
            and r.executed
            and r.exit_price is None
            and r.action == 'BUY'  # 只回填多头平仓（空头可按需扩展）
        ]
        if not candidates:
            # 也尝试 SELL 方向（空头）
            candidates = [
                r for r in today_signals
                if r.code == code
                and r.executed
                and r.exit_price is None
            ]
        if not candidates:
            logger.debug(f"[signal_tracker] record_exit_by_code: {code} 无未平仓信号记录，跳过回填")
            return

        # 取最近一条（按 execution_time 倒序，或取最后一条）
        candidates.sort(key=lambda r: r.execution_time or r.created_at, reverse=True)
        record = candidates[0]

        record.exit_price = exit_price
        record.exit_time = exit_time or datetime.now().isoformat()
        record.pnl = pnl
        record.pnl_pct = None  # 可由外部传入，此处留 None

        if record.execution_time:
            try:
                exec_time = datetime.fromisoformat(record.execution_time)
                exit_dt = datetime.fromisoformat(record.exit_time)
                record.holding_period_minutes = int((exit_dt - exec_time).total_seconds() / 60)
            except Exception:
                pass

        record.profitable = pnl > 0 if pnl is not None else None
        if record.profitable is not None and record.confidence >= 0.6:
            record.confidence_correct = record.profitable

        if exit_reason:
            # 将平仓原因追加到 reject_reason 字段（复用字段存储平仓标签）
            record.reject_reason = exit_reason

        record.updated_at = datetime.now().isoformat()
        self._save_today_records()
        logger.debug(
            f"[signal_tracker] {code} 平仓回填成功: "
            f"exit_price={exit_price} pnl={pnl:.2f} reason={exit_reason}"
        )

    def get_today_signals(self) -> List[SignalRecord]:
        """获取今日所有信号"""
        today = datetime.now().strftime('%Y-%m-%d')
        return [
            self.records[sid] 
            for sid in self.daily_records.get(today, [])
            if sid in self.records
        ]
    
    def get_signals_by_confidence(self, 
                                  min_conf: float = 0.0, 
                                  max_conf: float = 1.0) -> List[SignalRecord]:
        """
        按置信度筛选信号
        
        Args:
            min_conf: 最低置信度
            max_conf: 最高置信度
        
        Returns:
            list: 信号记录列表
        """
        today_signals = self.get_today_signals()
        return [
            r for r in today_signals 
            if min_conf <= r.confidence <= max_conf
        ]
    
    def get_statistics(self) -> Dict:
        """
        获取今日统计
        
        Returns:
            dict: 统计数据
        """
        today_signals = self.get_today_signals()
        
        if not today_signals:
            return {}
        
        # 基础统计
        total = len(today_signals)
        executed = sum(1 for r in today_signals if r.executed)
        rejected = total - executed
        
        # 盈亏统计
        profitable_signals = [r for r in today_signals if r.pnl is not None]
        winning = sum(1 for r in profitable_signals if r.pnl > 0)
        losing = sum(1 for r in profitable_signals if r.pnl <= 0)
        
        total_pnl = sum(r.pnl for r in profitable_signals) if profitable_signals else 0
        win_rate = winning / len(profitable_signals) if profitable_signals else 0
        
        # 置信度统计
        avg_confidence = sum(r.confidence for r in today_signals) / total if total else 0
        
        # 按置信度区间分组
        confidence_buckets = {
            '0.40-0.50': [],
            '0.50-0.60': [],
            '0.60-0.70': [],
            '0.70-0.80': [],
            '0.80-0.90': [],
            '0.90-1.00': [],
        }
        
        for r in today_signals:
            if 0.40 <= r.confidence < 0.50:
                confidence_buckets['0.40-0.50'].append(r)
            elif 0.50 <= r.confidence < 0.60:
                confidence_buckets['0.50-0.60'].append(r)
            elif 0.60 <= r.confidence < 0.70:
                confidence_buckets['0.60-0.70'].append(r)
            elif 0.70 <= r.confidence < 0.80:
                confidence_buckets['0.70-0.80'].append(r)
            elif 0.80 <= r.confidence < 0.90:
                confidence_buckets['0.80-0.90'].append(r)
            elif r.confidence >= 0.90:
                confidence_buckets['0.90-1.00'].append(r)
        
        bucket_stats = {}
        for bucket, signals in confidence_buckets.items():
            if signals:
                bucket_executed = sum(1 for r in signals if r.executed)
                bucket_pnl = [r.pnl for r in signals if r.pnl is not None]
                bucket_winning = sum(1 for pnl in bucket_pnl if pnl > 0)
                bucket_win_rate = bucket_winning / len(bucket_pnl) if bucket_pnl else 0
                
                bucket_stats[bucket] = {
                    'count': len(signals),
                    'executed': bucket_executed,
                    'win_rate': round(bucket_win_rate, 4),
                    'avg_pnl': round(sum(bucket_pnl) / len(bucket_pnl), 2) if bucket_pnl else 0,
                }
        
        return {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'total_signals': total,
            'executed': executed,
            'rejected': rejected,
            'execution_rate': round(executed / total, 4) if total else 0,
            'winning': winning,
            'losing': losing,
            'win_rate': round(win_rate, 4),
            'total_pnl': round(total_pnl, 2),
            'avg_confidence': round(avg_confidence, 4),
            'confidence_buckets': bucket_stats,
        }
    
    def print_statistics(self):
        """打印统计信息"""
        stats = self.get_statistics()
        
        if not stats:
            print("今日暂无信号记录")
            return
        
        print("\n" + "=" * 80)
        print(f"📊 FTSeek 信号质量统计 - {stats['date']}")
        print("=" * 80)
        print(f"总信号数：{stats['total_signals']}")
        print(f"执行数：{stats['executed']} ({stats['execution_rate']*100:.1f}%)")
        print(f"拒绝数：{stats['rejected']}")
        print(f"胜率：{stats['win_rate']*100:.1f}%")
        print(f"总盈亏：HK${stats['total_pnl']:.2f}")
        print(f"平均置信度：{stats['avg_confidence']:.3f}")
        print("=" * 80)
        
        if stats['confidence_buckets']:
            print("\n🎯 置信度区间分析:")
            print("-" * 80)
            for bucket, bucket_stats in stats['confidence_buckets'].items():
                print(f"  {bucket}: {bucket_stats['count']}笔，执行{bucket_stats['executed']}笔，"
                      f"胜率{bucket_stats['win_rate']*100:.1f}%, 平均盈亏 HK${bucket_stats['avg_pnl']:.2f}")
        
        print("\n" + "=" * 80)
        print()


# 全局单例
signal_tracker = SignalTracker()
