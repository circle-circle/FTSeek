"""
历史胜率反馈闭环 (Historical Win-Rate Feedback Loop)

P2-3: 从 trade_attribution 聚合历史数据，注入 LLM prompt 提升预测质量。

核心功能:
1. 聚合 trade_attribution 历史数据 (按 code/direction/strategy)
2. 计算胜率、盈亏比、信号质量等指标
3. 生成上下文注入 LLM prompt
4. 支持实时查询和缓存

设计原则:
- 数据驱动: 基于真实交易记录，非推断
- 低延迟: 结果缓存，避免重复查询
- 简洁注入: 只注入关键指标，不增加 prompt 复杂度

作者: FTSeek Team
日期: 2026-05-21
"""

import json
import logging
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.llm.feedback_loop")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
CACHE_FILE = DATA_DIR / "win_rate_cache.json"


class WinRateFeedbackLoop:
    """历史胜率反馈闭环"""

    # 缓存过期时间 (1小时)
    CACHE_TTL = 3600

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(DATA_DIR / "ftseek.db")
        self._cache: Dict[str, Dict] = {}
        self._cache_ts: float = 0

    def get_feedback_context(self, code: str, direction: str = None,
                              strategy: str = None, days: int = 30) -> str:
        """
        生成历史胜率上下文 (用于注入 prompt)

        Args:
            code: 股票代码 (如 HK.00700)
            direction: 方向 (BUY/SELL)
            strategy: 策略标签
            days: 统计天数

        Returns:
            格式化的上下文字符串
        """
        stats = self.get_historical_stats(code=code, direction=direction,
                                          strategy=strategy, days=days)

        if not stats or stats["total_trades"] == 0:
            return ""

        win_rate = stats["win_rate"]
        avg_pnl = stats["avg_pnl"]
        total_trades = stats["total_trades"]
        pnl_ratio = stats["pnl_ratio"]

        context = f"""
【历史表现参考】({code}, 最近{days}天)
- 总交易: {total_trades}笔
- 胜率: {win_rate:.1%} ({stats['wins']}胜/{stats['losses']}负)
- 平均盈亏: {avg_pnl:+.2f}
- 盈亏比: {pnl_ratio:.2f} (盈利时均:{stats['avg_win']:.2f} / 亏损时均:{stats['avg_loss']:.2f})
"""

        # 添加信号质量
        if stats.get("signal_quality_avg"):
            context += f"- 信号质量: {stats['signal_quality_avg']:.2f}/1.0\n"

        # 添加归因准确性
        if stats.get("direction_accuracy"):
            context += f"- 方向准确率: {stats['direction_accuracy']:.1%}\n"

        # 添加风险提示
        if win_rate < 0.4:
            context += f"⚠️ 历史胜率偏低({win_rate:.1%})，请谨慎评估\n"
        elif win_rate > 0.7:
            context += f"✅ 历史胜率较高({win_rate:.1%})，可适当提高信心\n"

        return context

    def get_historical_stats(self, code: str, direction: str = None,
                              strategy: str = None, days: int = 30) -> Optional[Dict]:
        """
        查询历史交易统计

        Returns:
            {
                "total_trades": 42,
                "wins": 28,
                "losses": 14,
                "win_rate": 0.667,
                "avg_pnl": 125.50,
                "avg_win": 320.00,
                "avg_loss": -180.00,
                "pnl_ratio": 1.78,
                "signal_quality_avg": 0.72,
                "direction_accuracy": 0.65,
                ...
            }
        """
        cache_key = f"{code}:{direction or 'ALL'}:{strategy or 'ALL'}:{days}"

        # 检查缓存
        if time.time() - self._cache_ts < self.CACHE_TTL and cache_key in self._cache:
            return self._cache[cache_key]

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN pnl_category = 'win' THEN 1 ELSE 0 END) as wins,
                        SUM(CASE WHEN pnl_category = 'loss' THEN 1 ELSE 0 END) as losses,
                        ROUND(AVG(pnl), 2) as avg_pnl,
                        ROUND(AVG(CASE WHEN pnl > 0 THEN pnl END), 2) as avg_win,
                        ROUND(AVG(CASE WHEN pnl < 0 THEN pnl END), 2) as avg_loss,
                        ROUND(AVG(signal_quality), 2) as signal_quality_avg,
                        ROUND(AVG(direction_correct), 2) as direction_accuracy
                    FROM trade_attribution
                    WHERE code = ?
                    AND created_at >= datetime('now', ?)
                """, (code, f"-{days} days"))

                row = cursor.fetchone()

            if not row or row[0] == 0:
                return None

            total = row[0]
            wins = row[1] or 0
            losses = row[2] or 0
            avg_pnl = row[3] or 0
            avg_win = row[4] or 0
            avg_loss = row[5] or 0
            signal_quality_avg = row[6] or 0
            direction_accuracy = row[7] or 0

            win_rate = wins / total if total > 0 else 0
            pnl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0

            stats = {
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 3),
                "avg_pnl": avg_pnl,
                "avg_win": avg_win,
                "avg_loss": avg_loss,
                "pnl_ratio": round(pnl_ratio, 2),
                "signal_quality_avg": signal_quality_avg,
                "direction_accuracy": direction_accuracy,
            }

            # 更新缓存
            self._cache[cache_key] = stats
            self._cache_ts = time.time()

            return stats

        except Exception as e:
            logger.warning(f"[FeedbackLoop] 查询失败: {e}")
            return None

    def get_market_regime_stats(self, days: int = 7) -> Dict[str, Any]:
        """
        获取市场状态统计 (整体胜率、盈亏比等)

        用于注入盘前分析 prompt
        """
        cache_key = f"market_regime:{days}"

        if time.time() - self._cache_ts < self.CACHE_TTL and cache_key in self._cache:
            return self._cache[cache_key]

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN pnl_category = 'win' THEN 1 ELSE 0 END) as wins,
                        ROUND(AVG(pnl), 2) as avg_pnl,
                        ROUND(AVG(signal_quality), 2) as signal_quality_avg,
                        ROUND(AVG(direction_correct), 2) as direction_accuracy
                    FROM trade_attribution
                    WHERE created_at >= datetime('now', ?)
                """, (f"-{days} days",))

                row = cursor.fetchone()

            if not row or row[0] == 0:
                return {"total_trades": 0}

            total = row[0]
            wins = row[1] or 0
            avg_pnl = row[2] or 0
            signal_quality_avg = row[3] or 0
            direction_accuracy = row[4] or 0

            stats = {
                "total_trades": total,
                "wins": wins,
                "win_rate": round(wins / total, 3) if total > 0 else 0,
                "avg_pnl": avg_pnl,
                "signal_quality_avg": signal_quality_avg,
                "direction_accuracy": direction_accuracy,
            }

            self._cache[cache_key] = stats
            self._cache_ts = time.time()

            return stats

        except Exception as e:
            logger.warning(f"[FeedbackLoop] 市场统计查询失败: {e}")
            return {"total_trades": 0}

    def get_strategy_stats(self, strategy: str, days: int = 30) -> Optional[Dict]:
        """获取特定策略的历史统计"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN pnl_category = 'win' THEN 1 ELSE 0 END) as wins,
                        ROUND(AVG(pnl), 2) as avg_pnl
                    FROM trade_attribution
                    WHERE strategy = ?
                    AND created_at >= datetime('now', ?)
                """, (strategy, f"-{days} days"))

                row = cursor.fetchone()

            if not row or row[0] == 0:
                return None

            total = row[0]
            wins = row[1] or 0

            return {
                "total_trades": total,
                "wins": wins,
                "win_rate": round(wins / total, 3) if total > 0 else 0,
                "avg_pnl": row[2] or 0,
            }

        except Exception as e:
            logger.warning(f"[FeedbackLoop] 策略统计查询失败: {e}")
            return None

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()
        self._cache_ts = 0
        logger.info("[FeedbackLoop] 缓存已清空")

    def get_top_codes(self, days: int = 30, limit: int = 10) -> List[Dict]:
        """获取胜率最高的股票 (用于参考)"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT
                        code,
                        COUNT(*) as total,
                        SUM(CASE WHEN pnl_category = 'win' THEN 1 ELSE 0 END) as wins,
                        ROUND(AVG(pnl), 2) as avg_pnl
                    FROM trade_attribution
                    WHERE created_at >= datetime('now', ?)
                    GROUP BY code
                    HAVING total >= 5
                    ORDER BY (wins * 1.0 / total) DESC, avg_pnl DESC
                    LIMIT ?
                """, (f"-{days} days", limit))

                rows = cursor.fetchall()

            result = []
            for row in rows:
                result.append({
                    "code": row[0],
                    "total_trades": row[1],
                    "wins": row[2],
                    "win_rate": round(row[2] / row[1], 3) if row[1] > 0 else 0,
                    "avg_pnl": row[3] or 0,
                })

            return result

        except Exception as e:
            logger.warning(f"[FeedbackLoop] Top codes 查询失败: {e}")
            return []

    def get_worst_codes(self, days: int = 30, limit: int = 10) -> List[Dict]:
        """获取胜率最低的股票 (用于风险提示)"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT
                        code,
                        COUNT(*) as total,
                        SUM(CASE WHEN pnl_category = 'win' THEN 1 ELSE 0 END) as wins,
                        ROUND(AVG(pnl), 2) as avg_pnl
                    FROM trade_attribution
                    WHERE created_at >= datetime('now', ?)
                    GROUP BY code
                    HAVING total >= 5
                    ORDER BY (wins * 1.0 / total) ASC, avg_pnl ASC
                    LIMIT ?
                """, (f"-{days} days", limit))

                rows = cursor.fetchall()

            result = []
            for row in rows:
                result.append({
                    "code": row[0],
                    "total_trades": row[1],
                    "wins": row[2],
                    "win_rate": round(row[2] / row[1], 3) if row[1] > 0 else 0,
                    "avg_pnl": row[3] or 0,
                })

            return result

        except Exception as e:
            logger.warning(f"[FeedbackLoop] Worst codes 查询失败: {e}")
            return []


# ── 全局单例 ─────────────────────────────────────────────────

_feedback_loop: Optional[WinRateFeedbackLoop] = None


def get_feedback_loop(db_path: Optional[str] = None) -> WinRateFeedbackLoop:
    """获取全局反馈闭环"""
    global _feedback_loop
    if _feedback_loop is None:
        _feedback_loop = WinRateFeedbackLoop(db_path=db_path)
    return _feedback_loop


# ── CLI 入口 ─────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    loop = get_feedback_loop()

    print("=== 市场状态 (7天) ===")
    regime = loop.get_market_regime_stats(days=7)
    print(json.dumps(regime, indent=2, ensure_ascii=False))

    print("\n=== Top 10 胜率股票 (30天) ===")
    top = loop.get_top_codes(days=30, limit=10)
    print(json.dumps(top, indent=2, ensure_ascii=False))

    print("\n=== Worst 10 胜率股票 (30天) ===")
    worst = loop.get_worst_codes(days=30, limit=10)
    print(json.dumps(worst, indent=2, ensure_ascii=False))
