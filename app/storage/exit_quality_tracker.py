"""
退出质量追踪器 (Exit Quality Tracker) — P2 2026-05-01

幻方原则: 每个退出决策需要在事后验证是否正确。
如果 FLASH_EXIT 后价格即刻反转 → 假阳性 (退出错了, 被噪音震出)
如果继续同向 → 退出正确

追踪机制:
  1. 记录每次退出: symbol, reason, time, price, session, pnl_pct
  2. 5 分钟后回看: 价格朝退出方向继续走? 还是反转?
  3. 按 (session, exit_reason) 聚合准确率
  4. 输入 SharedKernel.calibrate_scalers_from_data() → 自动调整 session_vol_ratio

数据流:
  ExitMonitor._execute_exit() → record_exit()
  Cron/定时任务 → evaluate_pending() (5min 后回看价格)
  SharedKernel.calibrate_scalers_from_data() ← get_session_quality_report()
"""
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek.exit_quality_tracker")

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "exit_quality.db")

# 评估延迟: 15 分钟给价格充分时间消化退出冲击
# 5 分钟太短 — 无法区分"正常波动"和"趋势延续"
EVALUATION_DELAY_SECONDS = 900


class ExitQualityTracker:
    """追踪退出质量，提供 session 级准确率统计"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS exit_quality (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    exit_reason TEXT NOT NULL,
                    exit_time TEXT NOT NULL,
                    exit_price REAL NOT NULL,
                    session TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    pnl_pct REAL,
                    -- 事后评估字段 (evaluate_pending 填充)
                    evaluated INTEGER DEFAULT 0,
                    post_exit_price REAL,
                    post_exit_time TEXT,
                    quality TEXT,       -- GOOD / BAD / NEUTRAL
                    adverse_move_pct REAL  -- 退出后逆向幅度 (%)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eq_session ON exit_quality(session)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eq_reason ON exit_quality(exit_reason)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eq_evaluated ON exit_quality(evaluated)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eq_time ON exit_quality(exit_time)")
            conn.commit()
            conn.close()
            logger.debug("[退出质量] 数据库初始化: %s", self.db_path)
        except Exception as e:
            logger.warning("[退出质量] 初始化失败 (不影响主流程): %s", e)

    # ── 记录退出 ──

    def record_exit(self, symbol: str, exit_reason: str, exit_price: float,
                    session: str, direction: str, pnl_pct: float = 0.0,
                    exit_time: str = None):
        """记录一次退出事件 (由 ExitMonitor._execute_exit() 调用)

        Args:
            symbol: 标的代码
            exit_reason: FLASH_EXIT / ATR_STOP / CHANDELIER / DAYTRADE_STOP / PRE_CLOSE_EXIT 等
            exit_price: 退出价格
            session: canonical session name (从 SharedKernel.get_current_session() 获取)
            direction: LONG / SHORT
            pnl_pct: 盈亏百分比
            exit_time: ISO 时间戳
        """
        try:
            ts = exit_time or datetime.now().isoformat()
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO exit_quality (symbol, exit_reason, exit_time, exit_price, "
                "session, direction, pnl_pct) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (symbol, exit_reason, ts, exit_price, session, direction, pnl_pct)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug("[退出质量] 记录失败 (不影响主流程): %s", e)

    # ── 事后评估 ──

    def evaluate_pending(self, get_price_fn) -> int:
        """评估所有未评估的退出记录 (5 分钟后回看价格)

        由定时任务调用 (每 5 分钟一次)。

        Args:
            get_price_fn: (symbol) -> float  获取当前价格

        Returns:
            已评估的记录数
        """
        cutoff = (datetime.now() - timedelta(seconds=EVALUATION_DELAY_SECONDS)).isoformat()

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            rows = conn.execute(
                "SELECT * FROM exit_quality WHERE evaluated = 0 AND exit_time <= ?",
                (cutoff,)
            ).fetchall()
            conn.close()

            evaluated = 0
            updates = []  # batch collect → single connection commit

            for row in rows:
                try:
                    current_price = get_price_fn(row["symbol"])
                    if current_price is None or current_price <= 0:
                        continue

                    exit_price = row["exit_price"]
                    direction = row["direction"]
                    post_exit_time = datetime.now().isoformat()

                    # 计算退出后的价格变化
                    if direction == "LONG":
                        adverse_move_pct = (current_price - exit_price) / exit_price * 100
                    else:
                        adverse_move_pct = (exit_price - current_price) / exit_price * 100

                    # 质量判定 (15min 窗口, 阈值略宽于 5min 窗口)
                    if adverse_move_pct > 1.0:
                        quality = "BAD"
                    elif adverse_move_pct < -0.5:
                        quality = "GOOD"
                    else:
                        quality = "NEUTRAL"

                    updates.append((
                        current_price, post_exit_time, quality,
                        round(adverse_move_pct, 3), row["id"]
                    ))
                    evaluated += 1

                except Exception as e:
                    logger.debug("[退出质量] 单条评估失败 %s: %s", row.get("symbol", "?"), e)

            # 批量写入 — 单连接
            if updates:
                try:
                    conn2 = sqlite3.connect(self.db_path)
                    conn2.executemany(
                        "UPDATE exit_quality SET evaluated=1, post_exit_price=?, "
                        "post_exit_time=?, quality=?, adverse_move_pct=? WHERE id=?",
                        updates
                    )
                    conn2.commit()
                    conn2.close()
                except Exception as e:
                    logger.warning("[退出质量] 批量写入失败: %s", e)

            if evaluated > 0:
                logger.info("[退出质量] 评估完成: %d 条记录", evaluated)
            return evaluated

        except Exception as e:
            logger.warning("[退出质量] 批量评估失败: %s", e)
            return 0

    # ── 聚合查询 ──

    def get_session_quality_report(self, days: int = 30) -> Dict:
        """按 session 聚合退出准确率 → 输入 SharedKernel.calibrate_scalers_from_data()

        Returns:
            {
                "opening_drive": {"total": 50, "good": 20, "bad": 20, "neutral": 10, "accuracy": 0.50},
                "midday":       {"total": 30, "good": 20, "bad": 3,  "neutral": 7,  "accuracy": 0.87},
                ...
            }
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            rows = conn.execute(
                "SELECT session, quality, COUNT(*) as cnt FROM exit_quality "
                "WHERE evaluated=1 AND exit_time >= ? AND quality IS NOT NULL "
                "GROUP BY session, quality",
                (cutoff,)
            ).fetchall()
            conn.close()

            report: Dict[str, Dict] = {}
            for row in rows:
                session = row["session"]
                quality = row["quality"]
                cnt = row["cnt"]
                if session not in report:
                    report[session] = {"total": 0, "good": 0, "bad": 0, "neutral": 0}
                report[session][quality.lower()] = cnt
                report[session]["total"] += cnt

            for s, stats in report.items():
                stats["accuracy"] = round(
                    stats["good"] / max(stats["total"], 1), 3
                )

            return report
        except Exception as e:
            logger.warning("[退出质量] 聚合报告失败: %s", e)
            return {}

    def get_reason_quality_report(self, days: int = 30) -> Dict:
        """按 exit_reason 聚合退出准确率"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            rows = conn.execute(
                "SELECT exit_reason, quality, COUNT(*) as cnt FROM exit_quality "
                "WHERE evaluated=1 AND exit_time >= ? AND quality IS NOT NULL "
                "GROUP BY exit_reason, quality",
                (cutoff,)
            ).fetchall()
            conn.close()

            report: Dict[str, Dict] = {}
            for row in rows:
                reason = row["exit_reason"]
                quality = row["quality"]
                cnt = row["cnt"]
                if reason not in report:
                    report[reason] = {"total": 0, "good": 0, "bad": 0, "neutral": 0}
                report[reason][quality.lower()] = cnt
                report[reason]["total"] += cnt

            for r, stats in report.items():
                stats["accuracy"] = round(
                    stats["good"] / max(stats["total"], 1), 3
                )

            return report
        except Exception as e:
            logger.warning("[退出质量] 原因报告失败: %s", e)
            return {}

    # ── 校准建议 ──

    def suggest_session_vol_adjustments(self, days: int = 30) -> Dict[str, float]:
        """基于退出准确率建议 session_vol_ratio 调整

        核心逻辑:
          - 该 session 的 BAD 率偏高 → 止损太紧, 经常被噪音震出 → 调高 vol_ratio (放宽止损)
          - 该 session 的 GOOD 率偏高 → 止损合适或偏宽 → 可微调
          - 调整幅度: ±0.05 per 10% accuracy deviation from 60% target

        Returns:
            {"opening_drive": +0.10, "midday": -0.05, ...}
            可直接加到 DEFAULT_SESSION_VOL_RATIO
        """
        report = self.get_session_quality_report(days)
        suggestions: Dict[str, float] = {}

        TARGET_ACCURACY = 0.60  # 60% 准确率目标 (不是越高越好, 过宽止损会降低盈亏比)

        for session, stats in report.items():
            if stats["total"] < 10:  # 样本太少, 不调整
                continue
            accuracy = stats["accuracy"]
            # 偏差映射: accuracy 低 → 止损太紧 → 调高 ratio
            deviation = TARGET_ACCURACY - accuracy
            adjustment = round(deviation * 0.5, 2)  # 系数 0.5: 缓慢跟随
            if abs(adjustment) >= 0.02:  # 2% 以上才建议调整
                suggestions[session] = adjustment

        return suggestions

    def apply_calibration(self, days: int = 30):
        """应用校准建议到 DEFAULT_SESSION_VOL_RATIO

        注: ENTRY_SL_SESSION_BASELINE 已废弃 (P1-3 2026-05-01)
        入场止损现直接使用 session_vol_ratio (P1-2 修复)，不再需要独立 baseline
        """
        suggestions = self.suggest_session_vol_adjustments(days)
        from app.core.shared_kernel import DEFAULT_SESSION_VOL_RATIO, _calibration_lock

        with _calibration_lock:
            for session, adj in suggestions.items():
                if session in DEFAULT_SESSION_VOL_RATIO:
                    old = DEFAULT_SESSION_VOL_RATIO[session]
                    new = round(max(0.3, min(2.5, old + adj)), 2)
                    DEFAULT_SESSION_VOL_RATIO[session] = new
                    logger.info(
                        "[退出质量·校准] %s: vol_ratio %.2f → %.2f (调整 %+.2f, accuracy=%.0f%%)",
                        session, old, new, adj,
                        self.get_session_quality_report(days).get(session, {}).get("accuracy", 0) * 100
                    )


# 全局单例
_tracker: Optional[ExitQualityTracker] = None


def get_exit_quality_tracker() -> ExitQualityTracker:
    global _tracker
    if _tracker is None:
        _tracker = ExitQualityTracker()
    return _tracker
