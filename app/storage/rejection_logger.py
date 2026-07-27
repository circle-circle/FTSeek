"""
拒绝日志收集器 — 记录每次信号被拒绝的原因，用于盘后归因分析

修复 2026-04-25: 原系统只打印一行 "置信度不足" 日志，
盘后无法分析哪个环节拦截最多、是否系统性问题。
"""

import sqlite3
import logging
from typing import List, Dict, Optional
from pathlib import Path

logger = logging.getLogger("ftseek_us.rejection_logger")


class RejectionLogger:
    """结构化记录每次信号被拒绝的原因"""

    def __init__(self, db_path: str = "data/rejections.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rejections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    reason_category TEXT NOT NULL,
                    strength REAL,
                    confidence REAL,
                    direction TEXT,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rej_timestamp ON rejections(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rej_symbol ON rejections(symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rej_reason ON rejections(reason_category)")
            conn.commit()
            conn.close()
            logger.debug(f"[拒绝日志] 数据库初始化: {self.db_path}")
        except Exception as e:
            logger.warning(f"[拒绝日志] 初始化失败 (不影响主流程): {e}")

    def record(self, symbol: str, reason: str, reason_category: str,
               strength: Optional[float] = None, confidence: Optional[float] = None,
               direction: Optional[str] = None, timestamp: Optional[str] = None):
        """记录一次信号拒绝

        Args:
            symbol: 股票代码
            reason: 详细拒绝原因
            reason_category: 分类（用于统计），如:
                - MARKET_STATE: 市场状态不允许
                - CONSENSUS_LOW: 模型共识不足
                - NEUTRAL: 中性信号
                - STRENGTH_WEAK: 信号强度不足
                - CONFIDENCE_LOW: 置信度不足
                - CAPITAL_SAFETY: 资本安全拦截
                - PRE_TRADE_CHECK: 预交易风控未通过
                - REGIME_FILTER: 大盘联动过滤
            strength: 信号强度 (0-100)
            confidence: 置信度 (0-1)
            direction: LONG/SHORT/NEUTRAL
            timestamp: ISO 格式时间戳
        """
        try:
            # 2026-07-27 修复 (昨日扫尾自引瑕疵): aware ISO 带 -04:00 后缀会被 SQLite date()
            #   折算成 UTC 日 (实测 date('...21:30:00-04:00')=次日) — ET 20:00 后写入与 ET 日
            #   查询参数错位。改写 naive ET (date() 对 naive 不做时区转换), 写读同口径。
            from app.utils.timezone import now_et
            ts = timestamp or now_et().replace(tzinfo=None).isoformat()

            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO rejections (symbol, reason, reason_category, strength, confidence, direction, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (symbol, reason, reason_category, strength, confidence, direction, ts)
            )
            conn.commit()
            conn.close()

            # P2 修复 (2026-05-01): 同步到盘中归因内存缓存
            try:
                from app.reports.intraday_attribution import record_rejection as _attr_record
                _attr_record(
                    symbol=symbol, reason=f"{reason_category}:{reason}",
                    strength=strength or 0, direction=direction or "LONG",
                )
            except Exception:
                pass  # 非关键路径, 不影响主流程
        except Exception as e:
            logger.debug(f"[拒绝日志] 记录失败 (不影响主流程): {e}")

    def get_today_summary(self) -> Dict:
        """获取今日拒绝统计摘要（用于盘后归因）"""
        try:
            # 2026-07-27 修复 (时区批同款): 原写入 CST naive、查询 date('now')=UTC —
            #   双口径错位, CST 00:00-08:00 窗口"今日"统计漂移。写读统一 ET。
            from app.utils.timezone import now_et
            _et_day = now_et().strftime("%Y-%m-%d")
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            
            # 今日总数
            total = conn.execute(
                "SELECT COUNT(*) as cnt FROM rejections WHERE date(timestamp) = ?", (_et_day,)
            ).fetchone()["cnt"]
            
            # 按分类统计
            by_category = {}
            rows = conn.execute(
                "SELECT reason_category, COUNT(*) as cnt FROM rejections "
                "WHERE date(timestamp) = ? "
                "GROUP BY reason_category ORDER BY cnt DESC", (_et_day,)
            ).fetchall()
            for r in rows:
                by_category[r["reason_category"]] = r["cnt"]
            
            # 按标的统计
            by_symbol = {}
            rows = conn.execute(
                "SELECT symbol, COUNT(*) as cnt FROM rejections "
                "WHERE date(timestamp) = ? "
                "GROUP BY symbol ORDER BY cnt DESC LIMIT 10", (_et_day,)
            ).fetchall()
            for r in rows:
                by_symbol[r["symbol"]] = r["cnt"]
            
            # 平均置信度和强度
            avg = conn.execute(
                "SELECT AVG(confidence) as avg_conf, AVG(strength) as avg_str FROM rejections "
                "WHERE date(timestamp) = ?", (_et_day,)
            ).fetchone()
            
            conn.close()
            
            return {
                "total": total,
                "by_category": by_category,
                "by_symbol": by_symbol,
                "avg_confidence": round(avg["avg_conf"], 3) if avg["avg_conf"] else 0,
                "avg_strength": round(avg["avg_str"], 1) if avg["avg_str"] else 0,
                "top_rejector": max(by_category, key=by_category.get) if by_category else "none"
            }
        except Exception as e:
            logger.warning(f"[拒绝日志] 获取摘要失败: {e}")
            return {"total": 0, "by_category": {}, "by_symbol": {}, "top_rejector": "error"}

    def get_today_rejections(self, limit: int = 100) -> List[Dict]:
        """获取今日详细拒绝记录"""
        try:
            from app.utils.timezone import now_et  # 2026-07-27: 同 get_today_summary, ET 口径
            _et_day = now_et().strftime("%Y-%m-%d")
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM rejections WHERE date(timestamp) = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (_et_day, limit)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[拒绝日志] 获取记录失败: {e}")
            return []


# 全局单例
_rejection_logger: Optional[RejectionLogger] = None


def get_rejection_logger() -> RejectionLogger:
    """获取全局拒绝日志实例"""
    global _rejection_logger
    if _rejection_logger is None:
        _rejection_logger = RejectionLogger()
    return _rejection_logger
