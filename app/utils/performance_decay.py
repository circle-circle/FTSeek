#!/usr/bin/env python3
"""
信号性能衰减器 (Performance Decay) - FTSeek-US Version
"""

import sqlite3
import logging

logger = logging.getLogger("ftseek_us.utils.performance_decay")

def get_decay_factor(db_path: str, strategy_name: str = None, lookback_days: int = 5, min_samples: int = 3) -> float:
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 美股使用 trade_records 表 (或者 open_positions if tracking pnl there, but trade_records is better)
        # Let's use trade_records
        query = """
            SELECT count(*) as total,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins
            FROM trade_records 
            WHERE exit_time IS NOT NULL
            AND exit_time > datetime('now', ?)
        """
        
        if strategy_name:
            query += " AND strategy_id = ?"
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
            return 1.0
            
        win_rate = wins / total
        
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
