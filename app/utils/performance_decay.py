#!/usr/bin/env python3
"""
信号性能衰减器 (Performance Decay)

功能：
根据最近 N 天的信号胜率，动态调整当前信号的置信度。
防止在模型“连败”期间继续重仓亏损，或在“连胜”期间错失机会。
"""

import sqlite3
import logging
from typing import Optional

logger = logging.getLogger("ftseek.utils.performance_decay")

def get_decay_factor(db_path: str, strategy_name: str = None, lookback_days: int = 5, min_samples: int = 15) -> float:
    """
    计算衰减/增强因子
    返回 0.5 ~ 1.2 之间的浮点数
    
    逻辑：
    - 样本不足 -> 1.0 (不调整)
    - 胜率 < 40% -> 0.5 (严重降级，半仓或观望)
    - 胜率 40%-60% -> 0.8 (轻微降级)
    - 胜率 > 70% -> 1.2 (增强，允许加仓)
    """
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 查询最近 N 天的已平仓信号
        # 假设表名为 positions (HK) 或 trade_records (US)
        # 这里适配 HK 逻辑，后续可复用
        query = """
            SELECT count(*) as total,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins
            FROM positions 
            WHERE status = 'CLOSED' 
            AND exit_time > datetime('now', ?)
        """
        
        if strategy_name:
            query += " AND strategy = ?"
            params = (f'-{lookback_days} days', strategy_name)
        else:
            params = (f'-{lookback_days} days',)
            
        cursor.execute(query, params)
        row = cursor.fetchone()
        
        if not row:
            return 1.0
            
        total = row[0]
        wins = row[1] or 0
        
        if total < min_samples:
            return 1.0 # 样本太少，不调整
            
        win_rate = wins / total
        
        # 映射到因子
        if win_rate < 0.40:
            factor = 0.5
            logger.warning(f"📉 策略 {strategy_name or 'ALL'} 近期胜率 {win_rate:.1%} -> 降级因子 {factor}")
        elif win_rate < 0.55:
            factor = 0.8
            logger.info(f"⚖️ 策略 {strategy_name or 'ALL'} 近期胜率 {win_rate:.1%} -> 调整因子 {factor}")
        elif win_rate > 0.70:
            factor = 1.1
            logger.info(f"🚀 策略 {strategy_name or 'ALL'} 近期胜率 {win_rate:.1%} -> 增强因子 {factor}")
        else:
            factor = 1.0
            
        conn.close()
        return factor
        
    except Exception as e:
        logger.error(f"计算衰减因子失败: {e}")
        return 1.0
