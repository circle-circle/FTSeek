"""
P&L Attribution Closed-Loop — 盈亏归因闭环

目的：将每笔交易的盈亏数据自动接入复盘系统，实现：
1. 交易级别的归因分析（为什么盈/亏）
2. 策略级别的表现追踪（哪个策略有效）
3. 自动参数调优建议（基于历史表现）
4. 次日盘前自动应用教训

架构：
  交易平仓 → PnLAttributionEngine.analyze() → 归因报告 → 
  存入 attribution_db → 盘前 Agent 读取 → 生成次日策略调整
"""

import logging
import sqlite3
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from pathlib import Path

from app.core.trading_env import get_trading_env

logger = logging.getLogger("ftseek.attribution.pnl_loop")


@dataclass
class AttributionReport:
    """归因分析报告"""
    trace_id: str
    code: str
    action: str  # BUY/SELL
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_rate: float
    hold_minutes: float
    exit_reason: str
    strategy: str
    
    # 归因维度
    signal_quality: str = "unknown"  # excellent/good/poor/unknown
    timing_quality: str = "unknown"  # early/optimal/late/unknown
    market_condition: str = "unknown"  # trending/ranging/volatile/unknown
    risk_management: str = "effective"  # effective/ineffective/unknown
    
    # 分析结论
    success_factors: List[str] = field(default_factory=list)
    failure_factors: List[str] = field(default_factory=list)
    lessons_learned: List[str] = field(default_factory=list)
    
    # 次日建议
    next_day_actions: List[str] = field(default_factory=list)
    param_adjustments: Dict[str, float] = field(default_factory=dict)
    
    timestamp: str = ""
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


class PnLAttributionEngine:
    """
    盈亏归因引擎
    
    每笔交易平仓后自动调用，生成归因报告并持久化。
    
    使用方式:
        engine = PnLAttributionEngine()
        report = engine.analyze(
            code="US.MSFT",
            entry_price=672.46,
            exit_price=679.30,
            quantity=287,
            pnl=-1963.65,
            pnl_rate=-0.0102,
            hold_minutes=5,
            exit_reason="PRE_CLOSE_EXIT",
            strategy="momentum",
            signal_strength=0.65,
            market_regime="trending",
        )
        engine.save(report)
    """
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(
            Path(__file__).parent.parent.parent / "data" / "attribution.db"
        )
        self._init_db()
    
    def _init_db(self):
        """初始化归因数据库"""
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute('''
                CREATE TABLE IF NOT EXISTS attribution_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT UNIQUE,
                    code TEXT NOT NULL,
                    action TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    quantity INTEGER,
                    pnl REAL,
                    pnl_rate REAL,
                    hold_minutes REAL,
                    exit_reason TEXT,
                    strategy TEXT,
                    signal_quality TEXT,
                    timing_quality TEXT,
                    market_condition TEXT,
                    risk_management TEXT,
                    success_factors TEXT,  -- JSON array
                    failure_factors TEXT,  -- JSON array
                    lessons_learned TEXT,  -- JSON array
                    next_day_actions TEXT, -- JSON array
                    param_adjustments TEXT, -- JSON object
                    timestamp TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            ''')
            conn.commit()
    
    def analyze(self, code: str, entry_price: float, exit_price: float,
                quantity: int, pnl: float, pnl_rate: float,
                hold_minutes: float, exit_reason: str, strategy: str,
                signal_strength: Optional[float] = None,
                confidence: Optional[float] = None,
                market_regime: Optional[str] = None,
                max_drawdown: Optional[float] = None,
                **kwargs) -> AttributionReport:
        """
        执行归因分析
        
        6 维度分析：
        1. 盈亏结果 (pnl_rate)
        2. 信号质量 (signal_strength)
        3. 入场时机 (hold_minutes vs expected)
        4. 市场环境 (market_regime)
        5. 风控效果 (max_drawdown vs exit_reason)
        6. 策略匹配 (strategy + exit_reason)
        """
        trace_id = kwargs.get("trace_id", f"{code}_{datetime.now().strftime('%H%M%S')}")
        
        report = AttributionReport(
            trace_id=trace_id,
            code=code,
            action=kwargs.get("action", "BUY"),
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            pnl=pnl,
            pnl_rate=pnl_rate,
            hold_minutes=hold_minutes,
            exit_reason=exit_reason,
            strategy=strategy,
        )
        
        # === 维度 1: 信号质量分析 ===
        if signal_strength is not None:
            if signal_strength >= 0.7:
                report.signal_quality = "excellent"
            elif signal_strength >= 0.5:
                report.signal_quality = "good"
            else:
                report.signal_quality = "poor"
        
        # === 维度 2: 入场时机分析 ===
        if hold_minutes < 10 and pnl < 0:
            report.timing_quality = "late"  # 刚入场就亏，入场太晚
            report.failure_factors.append(
                f"入场时机偏晚，持仓 {hold_minutes:.0f} 分钟即触发退出"
            )
        elif hold_minutes > 120 and pnl > 0:
            report.timing_quality = "optimal"
            report.success_factors.append(
                f"入场时机优秀，持仓 {hold_minutes/60:.1f} 小时获利"
            )
        elif hold_minutes < 60 and pnl > 0:
            report.timing_quality = "early"
            report.success_factors.append("快速盈利，入场时机良好")
        
        # === 维度 3: 市场环境匹配 ===
        if market_regime:
            report.market_condition = market_regime
        
        # === 维度 4: 风控效果 ===
        if exit_reason in ("STOP_LOSS", "RISK_CUT"):
            if max_drawdown is not None and abs(max_drawdown) < 0.05:
                report.risk_management = "effective"
                report.success_factors.append("风控及时介入，损失可控")
            else:
                report.risk_management = "ineffective"
                report.failure_factors.append("风控介入较晚，亏损扩大")
        elif exit_reason in ("TAKE_PROFIT", "TRAILING_STOP"):
            report.risk_management = "effective"
            report.success_factors.append("利润保护机制生效")
        
        # === 维度 5: 盈亏归因 ===
        if pnl_rate >= 0.03:
            report.success_factors.append(f"盈利 {pnl_rate:+.2%}，策略逻辑有效")
            report.next_day_actions.append(f"继续执行 {strategy} 策略")
        elif pnl_rate >= 0:
            report.success_factors.append("保本微利，风控有效")
        elif pnl_rate >= -0.02:
            report.lessons_learned.append("小幅亏损，在可接受范围内")
        else:
            report.failure_factors.append(f"亏损 {pnl_rate:+.2%}，需要复盘")
            report.next_day_actions.append(f"重新评估 {strategy} 策略参数")
        
        # === 维度 6: 退出原因归因 ===
        exit_analysis = self._analyze_exit_reason(exit_reason, pnl_rate, hold_minutes)
        report.lessons_learned.extend(exit_analysis.get("lessons", []))
        
        # === 参数调整建议 ===
        report.param_adjustments = self._suggest_param_adjustments(
            pnl_rate, signal_strength, hold_minutes, exit_reason, strategy
        )
        
        return report
    
    def _analyze_exit_reason(self, reason: str, pnl_rate: float,
                             hold_minutes: float) -> Dict[str, Any]:
        """分析退出原因"""
        result = {"lessons": []}
        
        if reason == "PRE_CLOSE_EXIT":
            if pnl_rate < 0 and hold_minutes < 15:
                result["lessons"].append(
                    "收盘平仓亏损且持仓极短 → 可能入场时间太接近收盘，"
                    "建议调整入场时间窗口"
                )
            elif pnl_rate > 0:
                result["lessons"].append("收盘前平仓获利 → 策略节奏良好")
        
        elif reason == "STOP_LOSS":
            result["lessons"].append(
                "止损触发 → 需要检查: "
                "1. 入场信号是否有足够的量价确认 "
                "2. 止损位设置是否合理 "
                "3. 市场环境是否适合该策略"
            )
        
        elif reason == "TIME_STOP":
            result["lessons"].append(
                "超时平仓 → 该标的在该时段活跃度不足，"
                "建议调整监控时段或标的选择"
            )
        
        elif reason == "TRAILING_STOP":
            if pnl_rate > 0.02:
                result["lessons"].append(
                    "追踪止盈成功 → 趋势跟踪策略有效，可考虑扩大追踪范围"
                )
        
        return result
    
    def _suggest_param_adjustments(self, pnl_rate: float,
                                     signal_strength: Optional[float],
                                     hold_minutes: float,
                                     exit_reason: str,
                                     strategy: str) -> Dict[str, float]:
        """基于交易结果建议参数调整"""
        adjustments = {}
        
        # 连续亏损 → 降低仓位
        if pnl_rate < -0.03:
            adjustments["position_size_multiplier"] = 0.8  # 减少 20% 仓位
        
        # 快速亏损 → 提高入场阈值
        if hold_minutes < 10 and pnl_rate < 0:
            adjustments["signal_threshold"] = 0.1  # 提高阈值
        
        # 信号强度低但盈利 → 可能信号评分偏低
        if signal_strength is not None and signal_strength < 0.4 and pnl_rate > 0:
            adjustments["signal_threshold"] = -0.05  # 可降低阈值
        
        # 长时间持仓且盈利 → 可放宽超时
        if hold_minutes > 180 and pnl_rate > 0:
            adjustments["timeout_minutes"] = 60  # 增加超时时间
        
        return adjustments
    
    def save(self, report: AttributionReport) -> bool:
        """保存归因报告到数据库"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                c = conn.cursor()
                c.execute('''
                    INSERT OR REPLACE INTO attribution_reports
                    (trace_id, code, action, entry_price, exit_price, quantity,
                     pnl, pnl_rate, hold_minutes, exit_reason, strategy,
                     signal_quality, timing_quality, market_condition, risk_management,
                     success_factors, failure_factors, lessons_learned,
                     next_day_actions, param_adjustments, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    report.trace_id, report.code, report.action,
                    report.entry_price, report.exit_price, report.quantity,
                    report.pnl, report.pnl_rate, report.hold_minutes,
                    report.exit_reason, report.strategy,
                    report.signal_quality, report.timing_quality,
                    report.market_condition, report.risk_management,
                    json.dumps(report.success_factors),
                    json.dumps(report.failure_factors),
                    json.dumps(report.lessons_learned),
                    json.dumps(report.next_day_actions),
                    json.dumps(report.param_adjustments),
                    report.timestamp,
                ))
                conn.commit()
            logger.info(f"归因报告已保存: {report.code} PnL={report.pnl:+.2f}")
            return True
        except Exception as e:
            logger.error(f"保存归因报告失败: {e}")
            return False
    
    def get_recent_reports(self, code: Optional[str] = None,
                           limit: int = 20) -> List[AttributionReport]:
        """获取最近的归因报告"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
        
            if code:
                c.execute(
                    "SELECT * FROM attribution_reports WHERE code = ? ORDER BY created_at DESC LIMIT ?",
                    (code, limit)
                )
            else:
                c.execute(
                    "SELECT * FROM attribution_reports ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                )
        
            rows = c.fetchall()
        
        reports = []
        for row in rows:
            report = AttributionReport(
                trace_id=row["trace_id"],
                code=row["code"],
                action=row["action"],
                entry_price=row["entry_price"],
                exit_price=row["exit_price"],
                quantity=row["quantity"],
                pnl=row["pnl"],
                pnl_rate=row["pnl_rate"],
                hold_minutes=row["hold_minutes"],
                exit_reason=row["exit_reason"],
                strategy=row["strategy"],
                signal_quality=row["signal_quality"] or "unknown",
                timing_quality=row["timing_quality"] or "unknown",
                market_condition=row["market_condition"] or "unknown",
                risk_management=row["risk_management"] or "unknown",
                success_factors=json.loads(row["success_factors"]) if row["success_factors"] else [],
                failure_factors=json.loads(row["failure_factors"]) if row["failure_factors"] else [],
                lessons_learned=json.loads(row["lessons_learned"]) if row["lessons_learned"] else [],
                next_day_actions=json.loads(row["next_day_actions"]) if row["next_day_actions"] else [],
                param_adjustments=json.loads(row["param_adjustments"]) if row["param_adjustments"] else {},
                timestamp=row["timestamp"],
            )
            reports.append(report)
        
        return reports
    
    def get_strategy_performance(self, strategy: str, days: int = 30) -> Dict[str, Any]:
        """获取策略表现统计"""
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
        
            since = (datetime.now() - timedelta(days=days)).isoformat()
            c.execute('''
                SELECT COUNT(*) as total_trades,
                       AVG(pnl) as avg_pnl,
                       AVG(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as avg_win,
                       AVG(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) as avg_loss,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate
                FROM attribution_reports
                WHERE strategy = ? AND timestamp > ?
            ''', (strategy, since))
        
            row = c.fetchone()
        
        if row:
            return {
                "strategy": strategy,
                "total_trades": row[0],
                "avg_pnl": row[1] or 0,
                "avg_win": row[2] or 0,
                "avg_loss": row[3] or 0,
                "win_rate": row[4] or 0,
            }
        return {"strategy": strategy, "total_trades": 0}
    
    def get_lessons_for_code(self, code: str) -> List[str]:
        """获取某只股票的历史教训"""
        reports = self.get_recent_reports(code=code, limit=10)
        lessons = []
        for r in reports:
            lessons.extend(r.lessons_learned)
            lessons.extend(r.failure_factors)
        return list(set(lessons))  # 去重


# ===== 全局便捷访问 =====

_pnl_engine: Optional[PnLAttributionEngine] = None


def get_attribution_engine() -> PnLAttributionEngine:
    """获取归因引擎实例"""
    global _pnl_engine
    if _pnl_engine is None:
        _pnl_engine = PnLAttributionEngine()
    return _pnl_engine
