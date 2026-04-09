#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
决策日志系统 - 每笔交易权重分配详情 (可追溯)

功能:
1. 记录每笔交易的权重分配详情
2. 支持查询和追溯
3. 支持导出和审计
4. 确保决策链路可追溯

约束:
- 每笔交易必须记录权重分配
- 决策 ID 必须唯一
- 支持按时间、股票、权重范围查询
"""

import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('decision_log')


@dataclass
class WeightAllocation:
    """权重分配详情"""
    news_weight: float        # 新闻权重 (0-40%)
    hk_us_weight: float       # 港股美股跟随权重 (0-35%)
    technical_weight: float   # 技术指标权重 (25-100%)
    total_weight: float       # 总和 (必须=100%)
    
    def validate(self) -> Tuple[bool, str]:
        """验证权重总和=100%"""
        total = self.news_weight + self.hk_us_weight + self.technical_weight
        if abs(total - 1.0) > 0.001:
            return False, f"权重总和={total:.4f}≠100%"
        return True, "通过"
    
    def to_dict(self) -> dict:
        return {
            'news_weight': self.news_weight,
            'hk_us_weight': self.hk_us_weight,
            'technical_weight': self.technical_weight,
            'total_weight': self.total_weight
        }


@dataclass
class NewsFactor:
    """新闻因子详情"""
    news_count: int
    major_news_count: int
    sources: List[str]
    sentiment: str
    sentiment_score: float
    confidence: float
    importance: str
    latency_ms: int
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HkUsFactor:
    """港股美股跟随因子详情"""
    hk_stock: str
    us_stock: str
    correlation: float
    enabled: bool
    hk_price_change: float
    us_price_change: float
    weight_contribution: float
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TechnicalFactor:
    """技术指标因子详情"""
    indicators: Dict[str, float]
    signal_strength: float
    confidence: float
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DecisionRecord:
    """决策记录"""
    decision_id: str              # 唯一决策 ID
    timestamp: datetime           # 决策时间
    stock: str                    # 股票代码
    action: str                   # 买入/卖出
    weight_allocation: WeightAllocation
    news_factor: Optional[NewsFactor]
    hk_us_factor: Optional[HkUsFactor]
    technical_factor: TechnicalFactor
    final_confidence: float       # 最终置信度
    position_size: float          # 仓位比例
    stop_loss: float              # 止损价
    take_profit: float            # 止盈价
    status: str                   # pending/executed/cancelled
    execution_time: Optional[datetime]
    execution_price: Optional[float]
    notes: str
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'decision_id': self.decision_id,
            'timestamp': self.timestamp.isoformat(),
            'stock': self.stock,
            'action': self.action,
            'weight_allocation': self.weight_allocation.to_dict(),
            'news_factor': self.news_factor.to_dict() if self.news_factor else None,
            'hk_us_factor': self.hk_us_factor.to_dict() if self.hk_us_factor else None,
            'technical_factor': self.technical_factor.to_dict(),
            'final_confidence': self.final_confidence,
            'position_size': self.position_size,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'status': self.status,
            'execution_time': self.execution_time.isoformat() if self.execution_time else None,
            'execution_price': self.execution_price,
            'notes': self.notes
        }
    
    def to_json(self) -> str:
        """转换为 JSON 字符串"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class DecisionLog:
    """
    决策日志系统
    
    功能:
    1. 记录决策详情
    2. 查询和追溯
    3. 导出和审计
    """
    
    def __init__(self, db_path: Optional[str] = None):
        """
        初始化决策日志
        
        Args:
            db_path: 数据库路径 (默认：data/decision_log.db)
        """
        if db_path is None:
            db_path = "/opt/ftseek/FTSeek/data/decision_log.db"
        
        self.db_path = db_path
        
        # 确保目录存在
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # 初始化数据库
        self._init_db()
        
        logger.info(f"DecisionLog initialized: {db_path}")
    
    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 创建决策记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                stock TEXT NOT NULL,
                action TEXT NOT NULL,
                news_weight REAL NOT NULL,
                hk_us_weight REAL NOT NULL,
                technical_weight REAL NOT NULL,
                total_weight REAL NOT NULL,
                news_factor TEXT,
                hk_us_factor TEXT,
                technical_factor TEXT,
                final_confidence REAL NOT NULL,
                position_size REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                status TEXT NOT NULL,
                execution_time TEXT,
                execution_price REAL,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 创建索引
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_stock ON decisions(stock)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_timestamp ON decisions(timestamp)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_status ON decisions(status)
        ''')
        
        conn.commit()
        conn.close()
        
        logger.debug("Database tables initialized")
    
    def _connect(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def generate_decision_id(self) -> str:
        """生成唯一决策 ID"""
        return f"D{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8].upper()}"
    
    def log_decision(self, record: DecisionRecord) -> str:
        """
        记录决策
        
        Args:
            record: 决策记录
            
        Returns:
            str: 决策 ID
        """
        # 验证权重总和
        valid, msg = record.weight_allocation.validate()
        if not valid:
            raise ValueError(f"权重验证失败：{msg}")
        
        # 生成决策 ID
        if not record.decision_id:
            record.decision_id = self.generate_decision_id()
        
        conn = self._connect()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO decisions (
                    decision_id, timestamp, stock, action,
                    news_weight, hk_us_weight, technical_weight, total_weight,
                    news_factor, hk_us_factor, technical_factor,
                    final_confidence, position_size, stop_loss, take_profit,
                    status, execution_time, execution_price, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                record.decision_id,
                record.timestamp.isoformat(),
                record.stock,
                record.action,
                record.weight_allocation.news_weight,
                record.weight_allocation.hk_us_weight,
                record.weight_allocation.technical_weight,
                record.weight_allocation.total_weight,
                json.dumps(record.news_factor.to_dict()) if record.news_factor else None,
                json.dumps(record.hk_us_factor.to_dict()) if record.hk_us_factor else None,
                json.dumps(record.technical_factor.to_dict()),
                record.final_confidence,
                record.position_size,
                record.stop_loss,
                record.take_profit,
                record.status,
                record.execution_time.isoformat() if record.execution_time else None,
                record.execution_price,
                record.notes
            ))
            
            conn.commit()
            logger.info(f"Decision logged: {record.decision_id}")
            
            return record.decision_id
            
        finally:
            conn.close()
    
    def get_decision(self, decision_id: str) -> Optional[DecisionRecord]:
        """
        获取决策记录
        
        Args:
            decision_id: 决策 ID
            
        Returns:
            Optional[DecisionRecord]: 决策记录
        """
        conn = self._connect()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT * FROM decisions WHERE decision_id = ?
            ''', (decision_id,))
            
            row = cursor.fetchone()
            if not row:
                return None
            
            return self._row_to_record(row)
            
        finally:
            conn.close()
    
    def query_decisions(
        self,
        stock: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        status: Optional[str] = None,
        min_weight: Optional[float] = None,
        max_weight: Optional[float] = None,
        limit: int = 100
    ) -> List[DecisionRecord]:
        """
        查询决策记录
        
        Args:
            stock: 股票代码
            start_time: 开始时间
            end_time: 结束时间
            status: 状态
            min_weight: 最小权重
            max_weight: 最大权重
            limit: 返回数量限制
            
        Returns:
            List[DecisionRecord]: 决策记录列表
        """
        conn = self._connect()
        cursor = conn.cursor()
        
        try:
            # 构建查询条件
            conditions = []
            params = []
            
            if stock:
                conditions.append("stock = ?")
                params.append(stock)
            
            if start_time:
                conditions.append("timestamp >= ?")
                params.append(start_time.isoformat())
            
            if end_time:
                conditions.append("timestamp <= ?")
                params.append(end_time.isoformat())
            
            if status:
                conditions.append("status = ?")
                params.append(status)
            
            if min_weight is not None:
                conditions.append("total_weight >= ?")
                params.append(min_weight)
            
            if max_weight is not None:
                conditions.append("total_weight <= ?")
                params.append(max_weight)
            
            where_clause = " AND ".join(conditions) if conditions else "1=1"
            
            query = f'''
                SELECT * FROM decisions
                WHERE {where_clause}
                ORDER BY timestamp DESC
                LIMIT ?
            '''
            params.append(limit)
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            return [self._row_to_record(row) for row in rows]
            
        finally:
            conn.close()
    
    def _row_to_record(self, row: sqlite3.Row) -> DecisionRecord:
        """将数据库行转换为决策记录"""
        return DecisionRecord(
            decision_id=row['decision_id'],
            timestamp=datetime.fromisoformat(row['timestamp']),
            stock=row['stock'],
            action=row['action'],
            weight_allocation=WeightAllocation(
                news_weight=row['news_weight'],
                hk_us_weight=row['hk_us_weight'],
                technical_weight=row['technical_weight'],
                total_weight=row['total_weight']
            ),
            news_factor=NewsFactor(**json.loads(row['news_factor'])) if row['news_factor'] else None,
            hk_us_factor=HkUsFactor(**json.loads(row['hk_us_factor'])) if row['hk_us_factor'] else None,
            technical_factor=TechnicalFactor(**json.loads(row['technical_factor'])),
            final_confidence=row['final_confidence'],
            position_size=row['position_size'],
            stop_loss=row['stop_loss'],
            take_profit=row['take_profit'],
            status=row['status'],
            execution_time=datetime.fromisoformat(row['execution_time']) if row['execution_time'] else None,
            execution_price=row['execution_price'],
            notes=row['notes'] or ""
        )
    
    def update_status(self, decision_id: str, status: str, 
                      execution_time: Optional[datetime] = None,
                      execution_price: Optional[float] = None):
        """
        更新决策状态
        
        Args:
            decision_id: 决策 ID
            status: 新状态
            execution_time: 执行时间
            execution_price: 执行价格
        """
        conn = self._connect()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                UPDATE decisions
                SET status = ?, execution_time = ?, execution_price = ?
                WHERE decision_id = ?
            ''', (
                status,
                execution_time.isoformat() if execution_time else None,
                execution_price,
                decision_id
            ))
            
            conn.commit()
            logger.info(f"Decision {decision_id} status updated to {status}")
            
        finally:
            conn.close()
    
    def get_daily_decisions(self, date: datetime = None) -> List[DecisionRecord]:
        """获取指定日期的决策"""
        if date is None:
            date = datetime.now()
        
        start_time = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_time = date.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        return self.query_decisions(start_time=start_time, end_time=end_time)
    
    def get_daily_count(self, date: datetime = None) -> int:
        """获取指定日期的决策数量"""
        decisions = self.get_daily_decisions(date)
        return len(decisions)
    
    def validate_daily_limit(self, date: datetime = None, limit: int = 5) -> Tuple[bool, int]:
        """
        验证日交易限制
        
        Args:
            date: 日期
            limit: 限制数量 (默认 5)
            
        Returns:
            Tuple[bool, int]: (是否超限，当前数量)
        """
        count = self.get_daily_count(date)
        return count > limit, count
    
    def export_decisions(
        self,
        output_path: str,
        start_time: datetime = None,
        end_time: datetime = None
    ) -> str:
        """
        导出决策记录
        
        Args:
            output_path: 输出文件路径
            start_time: 开始时间
            end_time: 结束时间
            
        Returns:
            str: 输出文件路径
        """
        decisions = self.query_decisions(start_time=start_time, end_time=end_time, limit=10000)
        
        data = {
            'export_time': datetime.now().isoformat(),
            'count': len(decisions),
            'decisions': [d.to_dict() for d in decisions]
        }
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Exported {len(decisions)} decisions to {output_path}")
        return output_path
    
    def get_trace(self, decision_id: str) -> dict:
        """
        获取决策追溯链路
        
        Args:
            decision_id: 决策 ID
            
        Returns:
            dict: 追溯链路
        """
        record = self.get_decision(decision_id)
        if not record:
            return {'error': 'Decision not found'}
        
        return {
            'decision_id': record.decision_id,
            'timestamp': record.timestamp.isoformat(),
            'stock': record.stock,
            'action': record.action,
            'trace': {
                'news_factor': {
                    'weight': record.weight_allocation.news_weight,
                    'details': record.news_factor.to_dict() if record.news_factor else None
                },
                'hk_us_factor': {
                    'weight': record.weight_allocation.hk_us_weight,
                    'details': record.hk_us_factor.to_dict() if record.hk_us_factor else None
                },
                'technical_factor': {
                    'weight': record.weight_allocation.technical_weight,
                    'details': record.technical_factor.to_dict()
                }
            },
            'validation': {
                'weight_sum': record.weight_allocation.total_weight,
                'weight_sum_valid': abs(record.weight_allocation.total_weight - 1.0) < 0.001
            },
            'execution': {
                'status': record.status,
                'execution_time': record.execution_time.isoformat() if record.execution_time else None,
                'execution_price': record.execution_price
            }
        }
    
    def get_stats(self, start_time: datetime = None, end_time: datetime = None) -> dict:
        """
        获取统计信息
        
        Args:
            start_time: 开始时间
            end_time: 结束时间
            
        Returns:
            dict: 统计信息
        """
        decisions = self.query_decisions(start_time=start_time, end_time=end_time, limit=10000)
        
        if not decisions:
            return {'count': 0}
        
        # 计算统计
        total = len(decisions)
        executed = sum(1 for d in decisions if d.status == 'executed')
        cancelled = sum(1 for d in decisions if d.status == 'cancelled')
        pending = sum(1 for d in decisions if d.status == 'pending')
        
        avg_news_weight = sum(d.weight_allocation.news_weight for d in decisions) / total
        avg_hk_us_weight = sum(d.weight_allocation.hk_us_weight for d in decisions) / total
        avg_technical_weight = sum(d.weight_allocation.technical_weight for d in decisions) / total
        
        avg_confidence = sum(d.final_confidence for d in decisions) / total
        
        return {
            'count': total,
            'status_breakdown': {
                'executed': executed,
                'cancelled': cancelled,
                'pending': pending
            },
            'avg_weights': {
                'news': avg_news_weight,
                'hk_us': avg_hk_us_weight,
                'technical': avg_technical_weight
            },
            'avg_confidence': avg_confidence,
            'time_range': {
                'start': decisions[-1].timestamp.isoformat() if decisions else None,
                'end': decisions[0].timestamp.isoformat() if decisions else None
            }
        }


# 便捷函数
def create_decision_record(
    stock: str,
    action: str,
    news_weight: float,
    hk_us_weight: float,
    technical_weight: float,
    news_factor: Optional[NewsFactor] = None,
    hk_us_factor: Optional[HkUsFactor] = None,
    technical_factor: Optional[TechnicalFactor] = None,
    position_size: float = 0.05,
    stop_loss: float = 0.0,
    take_profit: float = 0.0,
    notes: str = ""
) -> DecisionRecord:
    """
    创建决策记录
    
    Args:
        stock: 股票代码
        action: 买入/卖出
        news_weight: 新闻权重
        hk_us_weight: 港股美股权重
        technical_weight: 技术权重
        news_factor: 新闻因子
        hk_us_factor: 港股美股因子
        technical_factor: 技术因子
        position_size: 仓位比例
        stop_loss: 止损价
        take_profit: 止盈价
        notes: 备注
        
    Returns:
        DecisionRecord: 决策记录
    """
    return DecisionRecord(
        decision_id="",  # 自动生成
        timestamp=datetime.now(),
        stock=stock,
        action=action,
        weight_allocation=WeightAllocation(
            news_weight=news_weight,
            hk_us_weight=hk_us_weight,
            technical_weight=technical_weight,
            total_weight=news_weight + hk_us_weight + technical_weight
        ),
        news_factor=news_factor,
        hk_us_factor=hk_us_factor,
        technical_factor=technical_factor or TechnicalFactor(
            indicators={},
            signal_strength=0.0,
            confidence=0.0
        ),
        final_confidence=0.0,
        position_size=position_size,
        stop_loss=stop_loss,
        take_profit=take_profit,
        status='pending',
        execution_time=None,
        execution_price=None,
        notes=notes
    )


# 测试代码
if __name__ == "__main__":
    # 测试
    log = DecisionLog()
    
    # 创建测试记录
    record = create_decision_record(
        stock="00700.HK",
        action="BUY",
        news_weight=0.25,
        hk_us_weight=0.25,
        technical_weight=0.50,
        news_factor=NewsFactor(
            news_count=3,
            major_news_count=1,
            sources=['sina', 'tencent', 'futu'],
            sentiment='positive',
            sentiment_score=0.6,
            confidence=0.8,
            importance='normal',
            latency_ms=150
        ),
        hk_us_factor=HkUsFactor(
            hk_stock="00700.HK",
            us_stock="TCEHY",
            correlation=0.92,
            enabled=True,
            hk_price_change=0.02,
            us_price_change=0.015,
            weight_contribution=0.25
        ),
        technical_factor=TechnicalFactor(
            indicators={'rsi': 55, 'macd': 0.02, 'bb': 0.6},
            signal_strength=0.7,
            confidence=0.85
        ),
        position_size=0.05,
        stop_loss=95.0,
        take_profit=110.0,
        notes="测试决策"
    )
    
    # 记录决策
    decision_id = log.log_decision(record)
    print(f"Decision ID: {decision_id}")
    
    # 获取追溯链路
    trace = log.get_trace(decision_id)
    print(f"\nTrace: {json.dumps(trace, ensure_ascii=False, indent=2)}")
    
    # 查询今日决策
    today_decisions = log.get_daily_decisions()
    print(f"\nToday's decisions: {len(today_decisions)}")
    
    # 获取统计
    stats = log.get_stats()
    print(f"\nStats: {json.dumps(stats, ensure_ascii=False, indent=2)}")
