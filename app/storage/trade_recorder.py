"""
增强交易记录器 (美股版 - 支持深度复盘)

⚠️ 评审系统专用 — 独立于 TradeStore (ftseek_us.db).
   此模块写入 trade_records.db, 包含 TradeStore 没有的复盘字段:
   decision_confidence, emotion_state, psychological_notes, lessons_learned.
   review/* 模块依赖此数据源. 不要将交易写入逻辑合并到此处.

记录完整交易上下文，支持决策/时机/持仓/风控/心理 5 大维度复盘字段
适配美股多 Agent 架构
"""
import sqlite3
import logging
from datetime import datetime, timedelta
from app.utils.timezone import now_et, get_trade_date
from typing import Dict, List, Optional, Any
from pathlib import Path
import uuid

logger = logging.getLogger("ftseek_us.storage.trade_recorder")
logger.propagate = False


class TradeRecorder:
    """美股交易记录器 (支持深度复盘)"""
    
    def __init__(self, db_path: str = None):
        """
        初始化交易记录器
        
        Args:
            db_path: 数据库路径
        """
        if db_path:
            self.db_path = Path(db_path)
        else:
            from app.core.paths import get_trade_records_legacy_db
            self.db_path = get_trade_records_legacy_db()
        
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info(f"交易记录器已初始化：{self.db_path}")
    
    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_db(self):
        """初始化数据库表"""
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_records (
                    -- 基础信息
                    id TEXT PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,  -- LONG/SHORT
                    entry_time TEXT,
                    exit_time TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    quantity INTEGER,
                    realized_pnl REAL,
                    pnl_pct REAL,
                    exit_reason TEXT,
                    holding_hours REAL,
                    signal_strength INTEGER,
                    signal_confidence REAL,
                    futu_order_id TEXT,
                    
                    -- 账户类型
                    account_type TEXT DEFAULT 'simulate',
                    
                    -- 多 Agent 协作字段
                    analyst_score REAL DEFAULT 0,
                    strategist_recommendation TEXT,
                    risk_officer_approval BOOLEAN DEFAULT FALSE,
                    executor_notes TEXT,
                    
                    -- 决策复盘字段 (6 个)
                    decision_reason TEXT,
                    decision_confidence INTEGER,
                    strategy_type TEXT,
                    decision_time_spent INTEGER,
                    decision_quality_score INTEGER,
                    decision_notes TEXT,
                    
                    -- 时机复盘字段 (5 个)
                    entry_timing_score INTEGER,
                    exit_timing_score INTEGER,
                    entry_vs_signal_delay INTEGER,
                    market_phase TEXT,
                    timing_notes TEXT,
                    
                    -- 持仓复盘字段 (4 个)
                    position_size_actual REAL,
                    position_size_planned REAL,
                    position_adjustments INTEGER,
                    position_notes TEXT,
                    
                    -- 风控复盘字段 (5 个)
                    stop_loss_planned REAL,
                    stop_loss_executed BOOLEAN DEFAULT TRUE,
                    stop_loss_adjusted BOOLEAN DEFAULT FALSE,
                    max_drawdown_during_trade REAL,
                    risk_reward_actual REAL,
                    risk_notes TEXT,
                    
                    -- 心理复盘字段 (5 个)
                    emotion_state TEXT DEFAULT 'calm',
                    emotion_intensity INTEGER DEFAULT 3,
                    confidence_entry INTEGER DEFAULT 5,
                    confidence_exit INTEGER DEFAULT 5,
                    psychological_notes TEXT,
                    
                    -- 复盘信息
                    lessons_learned TEXT,
                    is_good_trade BOOLEAN DEFAULT TRUE,
                    review_notes TEXT,
                    
                    created_at TEXT NOT NULL,
                    updated_at TEXT
                )
            """)
            
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_date ON trade_records(trade_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON trade_records(symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_account_type ON trade_records(account_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_emotion_state ON trade_records(emotion_state)")
            
            conn.commit()
            logger.info("交易记录表初始化完成")
        finally:
            conn.close()
    
    def record(self,
               stock_code: str,
               side: str,
               quantity: int,
               price: float,
               decision_reason: str = 'technical',
               decision_confidence: int = 8,
               strategy_type: str = '',
               emotion_state: str = 'calm',
               **kwargs) -> str:
        """
        记录交易 (方式 1: 交易时自动记录)
        
        使用示例:
            trade_recorder.record(
                stock_code="AAPL",
                side="buy",
                quantity=100,
                price=175.5,
                decision_reason="technical",
                decision_confidence=8,
                strategy_type="confluence_v2",
                emotion_state="calm"
            )
        
        Args:
            stock_code: 股票代码
            side: buy/sell
            quantity: 数量
            price: 价格
            decision_reason: 决策原因
            decision_confidence: 决策信心 1-10
            strategy_type: 策略类型
            emotion_state: 心理状态 (默认 calm)
            **kwargs: 其他参数
        
        Returns:
            trade_id: 交易 ID
        """
        trade_id = kwargs.get('trade_id', str(uuid.uuid4())[:8])
        trade_date = get_trade_date()
        entry_time = now_et().isoformat()
        direction = 'LONG' if side.lower() in ['buy', 'long'] else 'SHORT'
        
        # 计算风险收益比
        stop_loss = kwargs.get('stop_loss', 0)
        take_profit = kwargs.get('take_profit', 0)
        if stop_loss > 0 and take_profit > 0:
            risk = abs(price - stop_loss)
            reward = abs(take_profit - price)
            risk_reward_ratio = reward / risk if risk > 0 else 0
        else:
            risk_reward_ratio = 0
        
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO trade_records (
                    id, trade_date, symbol, direction, entry_time,
                    entry_price, quantity, signal_confidence,
                    account_type, decision_reason, decision_confidence,
                    strategy_type, emotion_state,
                    stop_loss_planned, position_size_actual, position_size_planned,
                    confidence_entry, created_at, config_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v2')
            """, (
                trade_id,
                trade_date,
                stock_code,
                direction,
                entry_time,
                price,
                quantity,
                kwargs.get('signal_confidence', decision_confidence / 10.0),
                kwargs.get('account_type', 'simulate'),
                decision_reason,
                decision_confidence,
                strategy_type,
                emotion_state,
                stop_loss,
                kwargs.get('position_size', 0.1),
                kwargs.get('position_size', 0.1),
                decision_confidence,
                now_et().isoformat()
            ))
            conn.commit()
            
            logger.info(f"记录交易：{stock_code} {direction} @ {price}")
            return trade_id
        finally:
            conn.close()
    
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
                      review_notes: str = None,
                      **kwargs) -> bool:
        """
        交易后补充复盘 (方式 2: Dashboard 界面)
        
        使用示例:
            trade_recorder.update_review(
                trade_id="abc123",
                entry_timing_score=7,
                exit_timing_score=5,
                stop_loss_executed=True,
                review_notes="买早了，应等待回调"
            )
        
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
            bool: 是否更新成功
        """
        updates = []
        values = []
        
        if entry_timing_score is not None:
            updates.append("entry_timing_score = ?")
            values.append(entry_timing_score)
        if exit_timing_score is not None:
            updates.append("exit_timing_score = ?")
            values.append(exit_timing_score)
        if stop_loss_executed is not None:
            updates.append("stop_loss_executed = ?")
            values.append(stop_loss_executed)
        if position_adjustments is not None:
            updates.append("position_adjustments = ?")
            values.append(position_adjustments)
        if max_drawdown_during_trade is not None:
            updates.append("max_drawdown_during_trade = ?")
            values.append(max_drawdown_during_trade)
        if emotion_intensity is not None:
            updates.append("emotion_intensity = ?")
            values.append(emotion_intensity)
        if psychological_notes is not None:
            updates.append("psychological_notes = ?")
            values.append(psychological_notes)
        if timing_notes is not None:
            updates.append("timing_notes = ?")
            values.append(timing_notes)
        if position_notes is not None:
            updates.append("position_notes = ?")
            values.append(position_notes)
        if risk_notes is not None:
            updates.append("risk_notes = ?")
            values.append(risk_notes)
        if decision_notes is not None:
            updates.append("decision_notes = ?")
            values.append(decision_notes)
        if review_notes is not None:
            updates.append("review_notes = ?")
            values.append(review_notes)
        
        # 处理 kwargs 中的其他字段
        for key, value in kwargs.items():
            if hasattr(self, f'_validate_{key}'):
                updates.append(f"{key} = ?")
                values.append(value)
        
        if not updates:
            logger.warning(f"没有要更新的字段：{trade_id}")
            return False
        
        updates.append("updated_at = ?")
        values.append(now_et().isoformat())
        values.append(trade_id)
        
        conn = self._get_conn()
        try:
            cursor = conn.execute(f"""
                UPDATE trade_records 
                SET {', '.join(updates)}
                WHERE id = ?
            """, values)
            conn.commit()
            
            success = cursor.rowcount > 0
            if success:
                logger.info(f"更新交易复盘：{trade_id}")
            else:
                logger.warning(f"未找到交易记录：{trade_id}")
            
            return success
        finally:
            conn.close()
    
    def record_close(self,
                     trade_id: str,
                     exit_price: float,
                     exit_reason: str,
                     lessons_learned: str = '',
                     exit_timing_score: int = 5,
                     confidence_exit: int = 5,
                     emotion_state: str = 'calm',
                     stop_loss_executed: bool = True) -> bool:
        """
        记录平仓
        
        Args:
            trade_id: 交易 ID
            exit_price: 出场价
            exit_reason: 出场原因
            lessons_learned: 复盘总结
            exit_timing_score: 出场时机评分
            confidence_exit: 出场时信心
            emotion_state: 出场时心理状态
            stop_loss_executed: 止损是否执行
        
        Returns:
            bool: 是否更新成功
        """
        conn = self._get_conn()
        try:
            # 获取当前记录
            cursor = conn.execute("SELECT * FROM trade_records WHERE id = ?", (trade_id,))
            row = cursor.fetchone()
            
            if not row:
                logger.error(f"未找到交易记录：{trade_id}")
                return False
            
            # 计算盈亏
            entry_price = row['entry_price']
            quantity = row['quantity']
            direction = row['direction']
            
            if direction == 'LONG':
                pnl = (exit_price - entry_price) * quantity
                pnl_pct = (exit_price - entry_price) / entry_price
            else:
                pnl = (entry_price - exit_price) * quantity
                pnl_pct = (entry_price - exit_price) / entry_price
            
            # 计算持有时间
            entry_time = datetime.fromisoformat(row['entry_time'])
            holding_hours = (now_et() - entry_time).total_seconds() / 3600
            
            # 计算实际风险收益比
            stop_loss_planned = row['stop_loss_planned']
            if stop_loss_planned > 0:
                risk = abs(entry_price - stop_loss_planned)
                risk_reward_actual = abs(pnl) / risk if risk > 0 else 0
            else:
                risk_reward_actual = 0
            
            # 更新记录
            conn.execute("""
                UPDATE trade_records 
                SET exit_time = ?, exit_price = ?, exit_reason = ?,
                    realized_pnl = ?, pnl_pct = ?, holding_hours = ?,
                    lessons_learned = ?, exit_timing_score = ?,
                    confidence_exit = ?, emotion_state = ?,
                    risk_reward_actual = ?, stop_loss_executed = ?,
                    updated_at = ?
                WHERE id = ?
            """, (
                now_et().isoformat(),
                exit_price,
                exit_reason,
                pnl,
                pnl_pct,
                holding_hours,
                lessons_learned,
                exit_timing_score,
                confidence_exit,
                emotion_state,
                risk_reward_actual,
                stop_loss_executed,
                now_et().isoformat(),
                trade_id
            ))
            conn.commit()
            
            logger.info(f"记录平仓：{row['symbol']} {exit_reason} 盈亏：{pnl:.2f}")
            return True
        finally:
            conn.close()
    
    def get_record(self, trade_id: str) -> Optional[Dict]:
        """获取交易记录"""
        conn = self._get_conn()
        try:
            cursor = conn.execute("SELECT * FROM trade_records WHERE id = ?", (trade_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    
    def get_records_by_date(self, date_str: str) -> List[Dict]:
        """获取指定日期的交易记录"""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT * FROM trade_records WHERE trade_date = ? ORDER BY entry_time DESC",
                (date_str,)
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
    
    def get_records_by_account(self, account_type: str) -> List[Dict]:
        """获取指定账户类型的交易记录"""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT * FROM trade_records WHERE account_type = ? ORDER BY trade_date DESC",
                (account_type,)
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
    
    def get_records_with_pnl(self) -> List[Dict]:
        """获取已平仓的交易记录"""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT * FROM trade_records WHERE exit_price > 0 ORDER BY exit_time DESC"
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
    
    def get_review_statistics(self, start_date: str = None, end_date: str = None) -> Dict:
        """获取复盘统计数据"""
        conn = self._get_conn()
        try:
            query = "SELECT * FROM trade_records WHERE exit_price > 0"
            params = []
            
            if start_date:
                query += " AND trade_date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND trade_date <= ?"
                params.append(end_date)
            
            cursor = conn.execute(query, params)
            records = [dict(row) for row in cursor.fetchall()]
            
            if not records:
                return {'total_trades': 0}
            
            total_trades = len(records)
            winning_trades = len([r for r in records if r.get('realized_pnl', 0) > 0])
            losing_trades = len([r for r in records if r.get('realized_pnl', 0) < 0])
            
            total_pnl = sum(r.get('realized_pnl', 0) for r in records)
            win_rate = winning_trades / total_trades if total_trades > 0 else 0
            
            # 情绪分布
            emotion_counts = {}
            for r in records:
                emotion = r.get('emotion_state', 'unknown')
                emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1
            
            # 决策原因分布
            decision_counts = {}
            for r in records:
                reason = r.get('decision_reason', 'unknown') or 'unknown'
                decision_counts[reason] = decision_counts.get(reason, 0) + 1
            
            # 处理 None 值
            confidence_values = [r.get('decision_confidence') for r in records if r.get('decision_confidence') is not None]
            entry_timing_values = [r.get('entry_timing_score') for r in records if r.get('entry_timing_score') is not None]
            exit_timing_values = [r.get('exit_timing_score') for r in records if r.get('exit_timing_score') is not None]
            stop_loss_count = len([r for r in records if r.get('stop_loss_executed') is True])
            impulse_count = len([r for r in records if r.get('decision_reason') == 'impulse'])
            
            return {
                'total_trades': total_trades,
                'winning_trades': winning_trades,
                'losing_trades': losing_trades,
                'total_pnl': total_pnl,
                'win_rate': round(win_rate, 4),
                'avg_pnl': total_pnl / total_trades if total_trades > 0 else 0,
                'emotion_distribution': emotion_counts,
                'decision_reason_distribution': decision_counts,
                'avg_decision_confidence': sum(confidence_values) / len(confidence_values) if confidence_values else 5,
                'avg_entry_timing_score': sum(entry_timing_values) / len(entry_timing_values) if entry_timing_values else 5,
                'avg_exit_timing_score': sum(exit_timing_values) / len(exit_timing_values) if exit_timing_values else 5,
                'stop_loss_execution_rate': stop_loss_count / total_trades if total_trades > 0 else 0,
                'impulse_trade_ratio': impulse_count / total_trades if total_trades > 0 else 0
            }
        
        finally:
            conn.close()


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
                 **kwargs) -> str:
    """便捷函数：记录交易"""
    recorder = get_trade_recorder()
    return recorder.record(
        stock_code=stock_code,
        side=side,
        quantity=quantity,
        price=price,
        decision_reason=decision_reason,
        decision_confidence=decision_confidence,
        strategy_type=strategy_type,
        emotion_state=emotion_state,
        **kwargs
    )


def update_trade_review(trade_id: str, **kwargs) -> bool:
    """便捷函数：交易后补充复盘"""
    recorder = get_trade_recorder()
    return recorder.update_review(trade_id, **kwargs)
