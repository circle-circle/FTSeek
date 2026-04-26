#!/usr/bin/env python3
"""
FTSeek 交易后评估系统

功能:
1. 记录成交后股价阶段性变化 (1min/5min/15min/1h/EOD)
2. 评估交易决策质量 (买入时机/卖出时机/持仓时长)
3. 生成评分和优化建议
4. 支持周度/月度复盘分析

数据结构:
- trade_evaluations: 交易评估记录表
- evaluation_summaries: 周/月汇总表

使用方式:
    from app.analytics.trade_evaluation import TradeEvaluator
    
    evaluator = TradeEvaluator()
    evaluator.record_trade(trade_info)
    evaluator.evaluate_outcome(trade_id, price_changes)
    evaluator.get_weekly_summary()
"""

import json
import logging
from app.storage.repository import get_db
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from enum import Enum

logger = logging.getLogger("ftseek.analytics.trade_evaluation")
logger.propagate = False  # 防止日志重复

# 数据库路径
DB_PATH = Path("/opt/ftseek/FTSeek/data/ftseek.db")


class TradeAction(Enum):
    """交易方向"""
    BUY = "BUY"
    SELL = "SELL"


class DecisionQuality(Enum):
    """决策质量等级"""
    EXCELLENT = "excellent"    # 优秀: 盈利 > 3% 或止损避免了 > 3% 亏损
    GOOD = "good"             # 良好: 盈利 1-3% 或止损避免了 1-3% 亏损
    FAIR = "fair"             # 一般: 盈利 0-1% 或小幅亏损
    POOR = "poor"             # 较差: 亏损 1-3%
    BAD = "bad"               # 差: 亏损 > 3%


@dataclass
class PriceChange:
    """价格变化记录"""
    timestamp: str
    price: float
    change_pct: float        # 相对成交价变化
    high_since: float        # 成交后最高价
    low_since: float         # 成交后最低价
    drawdown_pct: float      # 最大回撤


@dataclass
class TradeEvaluation:
    """交易评估记录"""
    # 基本信息
    trade_id: str
    code: str
    action: str              # BUY/SELL
    entry_time: str
    entry_price: float
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    quantity: int = 0
    realized_pnl: float = 0.0
    realized_pnl_pct: float = 0.0
    
    # 成交原因
    trigger_strategy: str = ""
    trigger_reason: str = ""
    confidence: float = 0.0
    consensus_score: float = 0.0
    market_regime: str = ""
    
    # 阶段性价格变化 (买入后追踪)
    price_1min: Optional[float] = None
    price_5min: Optional[float] = None
    price_15min: Optional[float] = None
    price_1hour: Optional[float] = None
    price_eod: Optional[float] = None          # 当日收盘
    price_next_day: Optional[float] = None     # 次日收盘
    
    # 价格变化百分比
    change_1min: Optional[float] = None
    change_5min: Optional[float] = None
    change_15min: Optional[float] = None
    change_1hour: Optional[float] = None
    change_eod: Optional[float] = None
    change_next_day: Optional[float] = None
    
    # 极值追踪
    high_after_entry: Optional[float] = None   # 入场后最高价
    low_after_entry: Optional[float] = None    # 入场后最低价
    max_profit_pct: float = 0.0                # 最大盈利机会
    max_drawdown_pct: float = 0.0              # 最大回撤风险
    
    # 决策评估
    entry_quality: str = ""                    # 入场时机评估
    exit_quality: str = ""                     # 出场时机评估
    overall_quality: str = ""                  # 综合评估
    
    # 评分
    entry_score: float = 0.0                   # 入场评分 0-100
    exit_score: float = 0.0                    # 出场评分 0-100
    timing_score: float = 0.0                  # 时机评分 0-100
    overall_score: float = 0.0                 # 综合评分 0-100
    
    # 评估建议
    analysis: str = ""
    suggestions: List[str] = field(default_factory=list)
    
    # 元数据
    created_at: str = ""
    updated_at: str = ""


class TradeEvaluator:
    """交易评估器"""
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_PATH)
        self._init_db()
    
    def _init_db(self):
        """初始化数据库表"""
        with get_db(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT UNIQUE NOT NULL,
                    code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_time TEXT,
                    exit_price REAL,
                    quantity INTEGER DEFAULT 0,
                    realized_pnl REAL DEFAULT 0,
                    realized_pnl_pct REAL DEFAULT 0,
                    
                    -- 触发信息
                    trigger_strategy TEXT,
                    trigger_reason TEXT,
                    confidence REAL DEFAULT 0,
                    consensus_score REAL DEFAULT 0,
                    market_regime TEXT,
                    
                    -- 阶段性价格
                    price_1min REAL,
                    price_5min REAL,
                    price_15min REAL,
                    price_1hour REAL,
                    price_eod REAL,
                    price_next_day REAL,
                    
                    -- 价格变化百分比
                    change_1min REAL,
                    change_5min REAL,
                    change_15min REAL,
                    change_1hour REAL,
                    change_eod REAL,
                    change_next_day REAL,
                    
                    -- 极值追踪
                    high_after_entry REAL,
                    low_after_entry REAL,
                    max_profit_pct REAL DEFAULT 0,
                    max_drawdown_pct REAL DEFAULT 0,
                    
                    -- 决策评估
                    entry_quality TEXT,
                    exit_quality TEXT,
                    overall_quality TEXT,
                    
                    -- 评分
                    entry_score REAL DEFAULT 0,
                    exit_score REAL DEFAULT 0,
                    timing_score REAL DEFAULT 0,
                    overall_score REAL DEFAULT 0,
                    
                    -- 分析建议
                    analysis TEXT,
                    suggestions TEXT,
                    
                    -- 元数据
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            
            # 创建索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_eval_code 
                ON trade_evaluations(code)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_eval_entry_time 
                ON trade_evaluations(entry_time)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_eval_quality 
                ON trade_evaluations(overall_quality)
            """)
            
            conn.commit()
            logger.info("交易评估表初始化完成")
    
    def record_trade(self, trade_info: Dict) -> str:
        """
        记录交易信息
        
        Args:
            trade_info: 交易信息字典
            
        Returns:
            trade_id
        """
        trade_id = trade_info.get('trade_id') or f"T{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        try:
            with get_db(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO trade_evaluations (
                        trade_id, code, action, entry_time, entry_price,
                        quantity, trigger_strategy, trigger_reason,
                        confidence, consensus_score, market_regime,
                        high_after_entry, low_after_entry,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    trade_info.get('code', ''),
                    trade_info.get('action', ''),
                    trade_info.get('entry_time', datetime.now().isoformat()),
                    trade_info.get('entry_price', 0),
                    trade_info.get('quantity', 0),
                    trade_info.get('trigger_strategy', ''),
                    trade_info.get('trigger_reason', ''),
                    trade_info.get('confidence', 0),
                    trade_info.get('consensus_score', 0),
                    trade_info.get('market_regime', ''),
                    trade_info.get('entry_price', 0),  # 初始最高/低价
                    trade_info.get('entry_price', 0),
                    datetime.now().isoformat(),
                    datetime.now().isoformat()
                ))
                conn.commit()
            
            logger.info(f"记录交易: {trade_id} {trade_info.get('code')} {trade_info.get('action')}")
            return trade_id
            
        except Exception as e:
            logger.error(f"记录交易失败: {e}")
            return ""
    
    def update_price_tracking(self, trade_id: str, current_price: float, timestamp: str = None):
        """
        更新价格追踪数据
        
        Args:
            trade_id: 交易ID
            current_price: 当前价格
            timestamp: 时间戳
        """
        try:
            with get_db(self.db_path) as conn:
                # 获取原记录
                cursor = conn.execute(
                    "SELECT entry_price, entry_time, high_after_entry, low_after_entry FROM trade_evaluations WHERE trade_id = ?",
                    (trade_id,)
                )
                row = cursor.fetchone()
                if not row:
                    return
                
                entry_price, entry_time, high, low = row
                entry_price = float(entry_price)
                high = float(high) if high else entry_price
                low = float(low) if low else entry_price
                
                # 更新最高/低价
                new_high = max(high, current_price)
                new_low = min(low, current_price)
                
                # 计算最大盈利和最大回撤
                max_profit_pct = (new_high - entry_price) / entry_price * 100
                max_drawdown_pct = (entry_price - new_low) / entry_price * 100
                
                # 解析入场时间
                entry_dt = datetime.fromisoformat(entry_time) if isinstance(entry_time, str) else entry_time
                now = datetime.now()
                elapsed = (now - entry_dt).total_seconds()
                
                # 更新阶段性价格
                updates = {
                    'high_after_entry': new_high,
                    'low_after_entry': new_low,
                    'max_profit_pct': max_profit_pct,
                    'max_drawdown_pct': max_drawdown_pct,
                    'updated_at': now.isoformat()
                }
                
                change_pct = (current_price - entry_price) / entry_price * 100
                
                # 根据时间间隔更新对应字段
                if elapsed >= 60 and elapsed < 300:  # 1分钟
                    updates['price_1min'] = current_price
                    updates['change_1min'] = change_pct
                elif elapsed >= 300 and elapsed < 900:  # 5分钟
                    updates['price_5min'] = current_price
                    updates['change_5min'] = change_pct
                elif elapsed >= 900 and elapsed < 3600:  # 15分钟
                    updates['price_15min'] = current_price
                    updates['change_15min'] = change_pct
                elif elapsed >= 3600:  # 1小时
                    updates['price_1hour'] = current_price
                    updates['change_1hour'] = change_pct
                
                # 构建更新SQL
                set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
                values = list(updates.values()) + [trade_id]
                
                conn.execute(
                    f"UPDATE trade_evaluations SET {set_clause} WHERE trade_id = ?",
                    values
                )
                conn.commit()
                
        except Exception as e:
            logger.error(f"更新价格追踪失败: {e}")
    
    def record_exit(self, trade_id: str, exit_price: float, exit_time: str = None, realized_pnl: float = 0):
        """
        记录平仓信息并评估
        
        Args:
            trade_id: 交易ID
            exit_price: 平仓价格
            exit_time: 平仓时间
            realized_pnl: 已实现盈亏
        """
        try:
            with get_db(self.db_path) as conn:
                # 获取原记录
                cursor = conn.execute(
                    "SELECT entry_price, quantity FROM trade_evaluations WHERE trade_id = ?",
                    (trade_id,)
                )
                row = cursor.fetchone()
                if not row:
                    return
                
                entry_price, quantity = row
                entry_price = float(entry_price)
                realized_pnl_pct = (exit_price - entry_price) / entry_price * 100
                
                # 更新平仓信息
                conn.execute("""
                    UPDATE trade_evaluations SET
                        exit_time = ?,
                        exit_price = ?,
                        realized_pnl = ?,
                        realized_pnl_pct = ?,
                        updated_at = ?
                    WHERE trade_id = ?
                """, (
                    exit_time or datetime.now().isoformat(),
                    exit_price,
                    realized_pnl,
                    realized_pnl_pct,
                    datetime.now().isoformat(),
                    trade_id
                ))
                conn.commit()
                
                # 执行评估
                self._evaluate_trade(trade_id)
                
        except Exception as e:
            logger.error(f"记录平仓失败: {e}")
    
    def _evaluate_trade(self, trade_id: str):
        """
        评估交易决策质量
        
        Args:
            trade_id: 交易ID
        """
        try:
            with get_db(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT * FROM trade_evaluations WHERE trade_id = ?",
                    (trade_id,)
                )
                row = cursor.fetchone()
                if not row:
                    return
                
                # 解析字段
                columns = [desc[0] for desc in cursor.description]
                data = dict(zip(columns, row))
                
                entry_price = float(data['entry_price'])
                exit_price = float(data['exit_price']) if data['exit_price'] else entry_price
                realized_pnl_pct = float(data['realized_pnl_pct']) if data['realized_pnl_pct'] else 0
                max_profit_pct = float(data['max_profit_pct']) if data['max_profit_pct'] else 0
                max_drawdown_pct = float(data['max_drawdown_pct']) if data['max_drawdown_pct'] else 0
                action = data['action']
                
                # 计算评分
                entry_score, exit_score, timing_score, overall_score = self._calculate_scores(
                    action, realized_pnl_pct, max_profit_pct, max_drawdown_pct
                )
                
                # 确定质量等级
                entry_quality = self._get_quality_level(entry_score)
                exit_quality = self._get_quality_level(exit_score)
                overall_quality = self._get_quality_level(overall_score)
                
                # 生成分析和建议
                analysis, suggestions = self._generate_analysis(
                    action, realized_pnl_pct, max_profit_pct, max_drawdown_pct,
                    entry_price, exit_price, entry_score, exit_score
                )
                
                # 更新评估结果
                conn.execute("""
                    UPDATE trade_evaluations SET
                        entry_score = ?,
                        exit_score = ?,
                        timing_score = ?,
                        overall_score = ?,
                        entry_quality = ?,
                        exit_quality = ?,
                        overall_quality = ?,
                        analysis = ?,
                        suggestions = ?,
                        updated_at = ?
                    WHERE trade_id = ?
                """, (
                    entry_score,
                    exit_score,
                    timing_score,
                    overall_score,
                    entry_quality,
                    exit_quality,
                    overall_quality,
                    analysis,
                    json.dumps(suggestions, ensure_ascii=False),
                    datetime.now().isoformat(),
                    trade_id
                ))
                conn.commit()
                
                logger.info(f"交易评估完成: {trade_id} 得分={overall_score:.0f} 等级={overall_quality}")
                
        except Exception as e:
            logger.error(f"评估交易失败: {e}", exc_info=True)
    
    def _calculate_scores(self, action: str, realized_pnl_pct: float, 
                          max_profit_pct: float, max_drawdown_pct: float) -> tuple:
        """
        计算各项评分
        
        Returns:
            (entry_score, exit_score, timing_score, overall_score)
        """
        # 入场评分: 基于最大盈利机会
        # 如果入场后有很大盈利空间，说明入场时机不错
        if action == 'BUY':
            entry_score = min(100, max_profit_pct * 20)  # 5%盈利机会 = 100分
        else:
            entry_score = min(100, max_drawdown_pct * 20)  # 5%下跌空间 = 100分
        
        # 出场评分: 基于实际盈亏与最大盈利的比较
        if max_profit_pct > 0:
            profit_capture = max(0, realized_pnl_pct) / max_profit_pct
            exit_score = profit_capture * 100
        else:
            exit_score = 50 if realized_pnl_pct >= 0 else 30
        
        # 止损情况调整
        if realized_pnl_pct < -2:
            exit_score = max(exit_score, 60)  # 及时止损给基础分
        elif realized_pnl_pct < 0:
            exit_score = max(exit_score, 40)
        
        # 时机评分: 综合入场和出场
        timing_score = (entry_score + exit_score) / 2
        
        # 综合评分
        overall_score = timing_score * 0.6 + (50 + realized_pnl_pct * 10) * 0.4
        overall_score = max(0, min(100, overall_score))
        
        return entry_score, exit_score, timing_score, overall_score
    
    def _get_quality_level(self, score: float) -> str:
        """根据分数获取质量等级"""
        if score >= 80:
            return DecisionQuality.EXCELLENT.value
        elif score >= 60:
            return DecisionQuality.GOOD.value
        elif score >= 40:
            return DecisionQuality.FAIR.value
        elif score >= 20:
            return DecisionQuality.POOR.value
        else:
            return DecisionQuality.BAD.value
    
    def _generate_analysis(self, action: str, realized_pnl_pct: float,
                           max_profit_pct: float, max_drawdown_pct: float,
                           entry_price: float, exit_price: float,
                           entry_score: float, exit_score: float) -> tuple:
        """
        生成分析报告和建议
        
        Returns:
            (analysis, suggestions)
        """
        suggestions = []
        analysis_lines = []
        
        # 盈亏分析
        if realized_pnl_pct >= 3:
            analysis_lines.append(f"✅ 交易成功，盈利 {realized_pnl_pct:.2f}%")
        elif realized_pnl_pct >= 1:
            analysis_lines.append(f"✅ 交易盈利 {realized_pnl_pct:.2f}%")
        elif realized_pnl_pct >= 0:
            analysis_lines.append(f"📊 交易保本，盈利 {realized_pnl_pct:.2f}%")
        elif realized_pnl_pct >= -2:
            analysis_lines.append(f"⚠️ 小幅亏损 {realized_pnl_pct:.2f}%")
        else:
            analysis_lines.append(f"🔴 止损触发，亏损 {realized_pnl_pct:.2f}%")
        
        # 入场分析
        if entry_score >= 70:
            analysis_lines.append(f"📈 入场时机优秀 (得分 {entry_score:.0f})")
            suggestions.append("✅ 入场时机选择合理，可继续沿用当前策略")
        elif entry_score >= 50:
            analysis_lines.append(f"📊 入场时机一般 (得分 {entry_score:.0f})")
            suggestions.append("💡 建议等待更强的信号确认后再入场")
        else:
            analysis_lines.append(f"⚠️ 入场时机欠佳 (得分 {entry_score:.0f})")
            suggestions.append("⚠️ 需要优化入场条件，避免追高/抄底")
        
        # 出场分析
        if exit_score >= 70:
            analysis_lines.append(f"💰 出场时机优秀 (得分 {exit_score:.0f})")
        elif exit_score >= 50:
            analysis_lines.append(f"📊 出场时机一般 (得分 {exit_score:.0f})")
            if max_profit_pct > realized_pnl_pct + 1:
                suggestions.append(f"💡 曾有 {max_profit_pct:.2f}% 盈利机会，可考虑提前止盈")
        else:
            analysis_lines.append(f"⚠️ 出场时机欠佳 (得分 {exit_score:.0f})")
            if max_profit_pct > 2:
                suggestions.append(f"⚠️ 错失了 {max_profit_pct:.2f}% 的盈利机会，建议优化止盈策略")
        
        # 风险控制分析
        if max_drawdown_pct > 3:
            analysis_lines.append(f"🛡️ 最大回撤 {max_drawdown_pct:.2f}%，风控生效")
        
        # 后续建议
        if realized_pnl_pct < -2:
            suggestions.append("📌 建议复盘市场环境，检查是否适合当前策略")
        elif realized_pnl_pct >= 2:
            suggestions.append("📌 当前策略表现良好，可继续执行")
        
        analysis = "\n".join(analysis_lines)
        return analysis, suggestions
    
    def get_trade_evaluation(self, trade_id: str) -> Optional[Dict]:
        """获取单笔交易评估"""
        try:
            with get_db(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT * FROM trade_evaluations WHERE trade_id = ?",
                    (trade_id,)
                )
                row = cursor.fetchone()
                if row:
                    columns = [desc[0] for desc in cursor.description]
                    return dict(zip(columns, row))
                return None
        except Exception as e:
            logger.error(f"获取交易评估失败: {e}")
            return None
    
    def get_recent_evaluations(self, days: int = 7, code: str = None) -> List[Dict]:
        """
        获取近期交易评估
        
        Args:
            days: 天数
            code: 股票代码 (可选)
            
        Returns:
            评估记录列表
        """
        try:
            with get_db(self.db_path) as conn:
                since = (datetime.now() - timedelta(days=days)).isoformat()
                
                if code:
                    cursor = conn.execute("""
                        SELECT * FROM trade_evaluations 
                        WHERE entry_time >= ? AND code = ?
                        ORDER BY entry_time DESC
                    """, (since, code))
                else:
                    cursor = conn.execute("""
                        SELECT * FROM trade_evaluations 
                        WHERE entry_time >= ?
                        ORDER BY entry_time DESC
                    """, (since,))
                
                columns = [desc[0] for desc in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
                
        except Exception as e:
            logger.error(f"获取近期评估失败: {e}")
            return []
    
    def get_weekly_summary(self, week_start: str = None) -> Dict:
        """
        获取周度评估汇总
        
        Args:
            week_start: 周开始日期 (格式: YYYY-MM-DD)
            
        Returns:
            周度汇总数据
        """
        try:
            # 默认本周
            if not week_start:
                today = datetime.now()
                week_start = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
            
            week_end = (datetime.fromisoformat(week_start) + timedelta(days=7)).isoformat()
            
            with get_db(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total_trades,
                        SUM(CASE WHEN realized_pnl >= 0 THEN 1 ELSE 0 END) as winning_trades,
                        SUM(realized_pnl) as total_pnl,
                        AVG(realized_pnl_pct) as avg_pnl_pct,
                        AVG(entry_score) as avg_entry_score,
                        AVG(exit_score) as avg_exit_score,
                        AVG(overall_score) as avg_overall_score,
                        MAX(realized_pnl) as best_trade,
                        MIN(realized_pnl) as worst_trade
                    FROM trade_evaluations
                    WHERE entry_time >= ? AND entry_time < ?
                """, (week_start, week_end))
                
                row = cursor.fetchone()
                
                # 按股票统计
                cursor2 = conn.execute("""
                    SELECT code, COUNT(*) as trades, SUM(realized_pnl) as pnl
                    FROM trade_evaluations
                    WHERE entry_time >= ? AND entry_time < ?
                    GROUP BY code
                    ORDER BY pnl DESC
                """, (week_start, week_end))
                
                by_code = [{"code": r[0], "trades": r[1], "pnl": r[2]} for r in cursor2.fetchall()]
                
                # 按质量分布
                cursor3 = conn.execute("""
                    SELECT overall_quality, COUNT(*) as count
                    FROM trade_evaluations
                    WHERE entry_time >= ? AND entry_time < ?
                    GROUP BY overall_quality
                """, (week_start, week_end))
                
                quality_dist = {r[0]: r[1] for r in cursor3.fetchall()}
                
                return {
                    "period": f"{week_start} - {week_end[:10]}",
                    "total_trades": row[0] or 0,
                    "winning_trades": row[1] or 0,
                    "win_rate": (row[1] or 0) / max(row[0] or 1, 1) * 100,
                    "total_pnl": row[2] or 0,
                    "avg_pnl_pct": row[3] or 0,
                    "avg_entry_score": row[4] or 0,
                    "avg_exit_score": row[5] or 0,
                    "avg_overall_score": row[6] or 0,
                    "best_trade": row[7] or 0,
                    "worst_trade": row[8] or 0,
                    "by_code": by_code,
                    "quality_distribution": quality_dist
                }
                
        except Exception as e:
            logger.error(f"获取周度汇总失败: {e}")
            return {}
    
    def get_monthly_summary(self, month: str = None) -> Dict:
        """
        获取月度评估汇总
        
        Args:
            month: 月份 (格式: YYYY-MM)
            
        Returns:
            月度汇总数据
        """
        try:
            if not month:
                month = datetime.now().strftime('%Y-%m')
            
            month_start = f"{month}-01"
            month_end = f"{month}-32"  # SQLite会自动处理
            
            with get_db(self.db_path) as conn:
                # 基础统计
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total_trades,
                        SUM(CASE WHEN realized_pnl >= 0 THEN 1 ELSE 0 END) as winning_trades,
                        SUM(realized_pnl) as total_pnl,
                        AVG(realized_pnl_pct) as avg_pnl_pct,
                        AVG(overall_score) as avg_score,
                        MAX(realized_pnl) as best_trade,
                        MIN(realized_pnl) as worst_trade
                    FROM trade_evaluations
                    WHERE entry_time >= ? AND entry_time < ?
                """, (month_start, month_end))
                
                row = cursor.fetchone()
                
                # 按周统计
                cursor2 = conn.execute("""
                    SELECT 
                        date(entry_time, 'weekday 0', '-6 days') as week_start,
                        COUNT(*) as trades,
                        SUM(realized_pnl) as pnl
                    FROM trade_evaluations
                    WHERE entry_time >= ? AND entry_time < ?
                    GROUP BY week_start
                    ORDER BY week_start
                """, (month_start, month_end))
                
                weekly_trend = [{"week": r[0], "trades": r[1], "pnl": r[2]} for r in cursor2.fetchall()]
                
                # 按策略统计
                cursor3 = conn.execute("""
                    SELECT trigger_strategy, COUNT(*) as trades, SUM(realized_pnl) as pnl,
                           AVG(overall_score) as avg_score
                    FROM trade_evaluations
                    WHERE entry_time >= ? AND entry_time < ?
                    GROUP BY trigger_strategy
                    ORDER BY pnl DESC
                """, (month_start, month_end))
                
                by_strategy = [{
                    "strategy": r[0], 
                    "trades": r[1], 
                    "pnl": r[2],
                    "avg_score": r[3]
                } for r in cursor3.fetchall()]
                
                return {
                    "period": month,
                    "total_trades": row[0] or 0,
                    "winning_trades": row[1] or 0,
                    "win_rate": (row[1] or 0) / max(row[0] or 1, 1) * 100,
                    "total_pnl": row[2] or 0,
                    "avg_pnl_pct": row[3] or 0,
                    "avg_score": row[4] or 0,
                    "best_trade": row[5] or 0,
                    "worst_trade": row[6] or 0,
                    "weekly_trend": weekly_trend,
                    "by_strategy": by_strategy
                }
                
        except Exception as e:
            logger.error(f"获取月度汇总失败: {e}")
            return {}
    
    def get_optimization_suggestions(self, days: int = 30) -> List[Dict]:
        """
        基于历史数据生成优化建议
        
        Args:
            days: 分析天数
            
        Returns:
            优化建议列表
        """
        suggestions = []
        
        try:
            evaluations = self.get_recent_evaluations(days=days)
            if not evaluations:
                return suggestions
            
            # 统计入场得分低的模式
            low_entry_scores = [e for e in evaluations if e.get('entry_score', 0) < 50]
            if len(low_entry_scores) > len(evaluations) * 0.3:
                suggestions.append({
                    "type": "entry_timing",
                    "priority": "high",
                    "message": "入场时机得分偏低，建议等待更强的信号确认",
                    "detail": f"近{days}天 {len(low_entry_scores)} 笔交易入场得分低于50分"
                })
            
            # 统计出场得分低的模式
            low_exit_scores = [e for e in evaluations if e.get('exit_score', 0) < 50]
            if len(low_exit_scores) > len(evaluations) * 0.3:
                suggestions.append({
                    "type": "exit_timing",
                    "priority": "high",
                    "message": "出场时机得分偏低，可能错失盈利机会",
                    "detail": f"近{days}天 {len(low_exit_scores)} 笔交易出场得分低于50分"
                })
            
            # 分析最大盈利机会捕获率
            total_max_profit = sum(e.get('max_profit_pct', 0) for e in evaluations)
            total_realized = sum(e.get('realized_pnl_pct', 0) for e in evaluations)
            if total_max_profit > 0:
                capture_rate = max(0, total_realized) / total_max_profit
                if capture_rate < 0.5:
                    suggestions.append({
                        "type": "profit_capture",
                        "priority": "medium",
                        "message": f"盈利捕获率仅 {capture_rate:.0%}，建议优化止盈策略",
                        "detail": f"总盈利机会 {total_max_profit:.2f}%，实际捕获 {total_realized:.2f}%"
                    })
            
            # 分析止损效果
            stopped_trades = [e for e in evaluations if e.get('realized_pnl_pct', 0) < -2]
            if stopped_trades:
                avg_stop_loss = sum(e.get('realized_pnl_pct', 0) for e in stopped_trades) / len(stopped_trades)
                suggestions.append({
                    "type": "stop_loss",
                    "priority": "low",
                    "message": f"止损平均亏损 {avg_stop_loss:.2f}%，风控机制正常",
                    "detail": f"{len(stopped_trades)} 笔交易触发止损"
                })
            
        except Exception as e:
            logger.error(f"生成优化建议失败: {e}")
        
        return suggestions


# 单例
_evaluator = None

def get_evaluator() -> TradeEvaluator:
    """获取交易评估器单例"""
    global _evaluator
    if _evaluator is None:
        _evaluator = TradeEvaluator()
    return _evaluator