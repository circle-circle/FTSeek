"""
策略性能追踪器 — Per-Regime Performance Matrix

为每个策略建立按市场状态分组的绩效矩阵:
- 自动从 trade_records.db 读取已平仓交易
- 按 MarketRegime 分组统计胜率、平均收益、最大回撤
- 每24小时输出策略绩效摘要
- 支持盘前计划中的策略权重动态调整
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class RegimeStats:
    """单个市场状态下的策略表现"""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    last_updated: str = ""

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    @property
    def avg_return_pct(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_pnl_pct / self.total_trades

    @property
    def profit_factor(self) -> float:
        gross_win = self.winning_trades * abs(self.avg_win_pct) if self.avg_win_pct > 0 else 0
        gross_loss = self.losing_trades * abs(self.avg_loss_pct) if self.avg_loss_pct < 0 else 1
        if gross_loss == 0:
            return 999.0 if gross_win > 0 else 0.0
        return gross_win / gross_loss

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 3),
            "avg_return_pct": round(self.avg_return_pct, 4),
            "avg_win_pct": round(self.avg_win_pct, 4),
            "avg_loss_pct": round(self.avg_loss_pct, 4),
            "profit_factor": round(self.profit_factor, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
        }


class PerformanceTracker:
    """
    策略性能追踪器

    单例模式。线程安全。
    """

    _instance: Optional["PerformanceTracker"] = None
    REGIMES = ["TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL", "LOW_VOL"]

    def __new__(cls) -> "PerformanceTracker":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._lock = threading.RLock()
        self._perf: Dict[str, Dict[str, RegimeStats]] = defaultdict(
            lambda: {r: RegimeStats() for r in self.REGIMES}
        )
        self._db_path: Optional[str] = None
        self._persist_path: Optional[str] = None
        self._last_sync: Optional[datetime] = None
        self._initialized = True
        self._auto_discover_paths()

    def _auto_discover_paths(self) -> None:
        """自动发现数据库和持久化路径"""
        base = Path(__file__).resolve().parent.parent.parent
        # Prefer ftseek_us.db (current US trading DB) over legacy trade_records.db
        us_db = base / "data" / "ftseek_us.db"
        legacy_db = base / "data" / "trade_records.db"
        if us_db.exists():
            self._db_path = str(us_db)
        elif legacy_db.exists():
            self._db_path = str(legacy_db)
        self._persist_path = str(base / "data" / "strategy_perf.json")

    # ----- 数据同步 -----
    def sync_from_db(self, db_path: Optional[str] = None) -> int:
        """
        从交易记录数据库同步绩效数据

        Returns:
            int: 新同步的交易数量
        """
        path = db_path or self._db_path
        if not path or not os.path.exists(path):
            logger.debug("[PerfTracker] DB 不可用，跳过同步")
            return 0

        new_count = 0
        try:
            with self._lock:
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT strategy_id, direction, pnl_pct AS realized_pnl_pct,
                           market_regime, entry_time
                    FROM trade_records
                    WHERE pnl_pct IS NOT NULL
                      AND market_regime IS NOT NULL
                    ORDER BY entry_time DESC
                    LIMIT 5000
                """)
                rows = cursor.fetchall()
                conn.close()

                # 重建统计（全量刷新）
                new_perf: Dict[str, Dict[str, RegimeStats]] = defaultdict(
                    lambda: {r: RegimeStats() for r in self.REGIMES}
                )
                for row in rows:
                    sid = row["strategy_id"] or "unknown"
                    regime = row["market_regime"] or "RANGE"
                    if regime not in self.REGIMES:
                        regime = "RANGE"
                    pnl = float(row["realized_pnl_pct"] or 0)

                    stats = new_perf[sid][regime]
                    stats.total_trades += 1
                    stats.total_pnl_pct += pnl
                    if pnl > 0:
                        stats.winning_trades += 1
                        stats.avg_win_pct = (
                            (stats.avg_win_pct * (stats.winning_trades - 1) + pnl)
                            / stats.winning_trades
                        )
                    elif pnl < 0:
                        stats.losing_trades += 1
                        stats.avg_loss_pct = (
                            (stats.avg_loss_pct * (stats.losing_trades - 1) + pnl)
                            / stats.losing_trades
                        )
                    stats.last_updated = datetime.now().isoformat()
                    new_count += 1

                self._perf = new_perf
                self._last_sync = datetime.now()
        except Exception as e:
            logger.warning("[PerfTracker] DB 同步失败: %s", e)

        return new_count

    # ----- 查询 -----
    def get_strategy_stats(
        self, strategy_id: str, regime: Optional[str] = None
    ) -> Dict[str, Any]:
        """获取策略绩效"""
        with self._lock:
            if strategy_id not in self._perf:
                return {"strategy_id": strategy_id, "regimes": {}, "summary": {}}

            data = self._perf[strategy_id]
            regimes_out = {}
            total_trades = 0
            total_wins = 0
            total_pnl = 0.0

            for r in (self.REGIMES if regime is None else [regime]):
                stats = data.get(r, RegimeStats())
                regimes_out[r] = stats.to_dict()
                total_trades += stats.total_trades
                total_wins += stats.winning_trades
                total_pnl += stats.total_pnl_pct

            summary = {
                "total_trades": total_trades,
                "win_rate": round(total_wins / max(total_trades, 1), 3),
                "avg_return_pct": round(total_pnl / max(total_trades, 1), 4),
            }

            return {
                "strategy_id": strategy_id,
                "regimes": regimes_out,
                "summary": summary,
            }

    def get_regime_weight(
        self, strategy_id: str, regime: str
    ) -> float:
        """
        获取策略在某市场状态下的建议权重

        基于历史表现：胜率高 → 权重高，亏损 → 权重低
        """
        with self._lock:
            stats = self._perf.get(strategy_id, {}).get(regime)
            if stats is None or stats.total_trades < 5:
                return 1.0  # 样本不足，保持原权重

            wr = stats.win_rate
            ar = stats.avg_return_pct

            # 胜率加权 + 平均收益率加权
            factor = max(0.3, min(1.5, wr * 1.0 + ar * 5.0))
            return round(factor, 3)

    def get_all_performance(self) -> Dict[str, Any]:
        """获取所有策略的完整绩效矩阵"""
        with self._lock:
            result = {}
            for sid in sorted(self._perf.keys()):
                result[sid] = self.get_strategy_stats(sid)
            return result

    def get_top_strategies(
        self, regime: str, top_n: int = 3
    ) -> List[Tuple[str, float]]:
        """获取某市场状态下表现最好的N个策略"""
        scored = []
        with self._lock:
            for sid, data in self._perf.items():
                stats = data.get(regime)
                if stats is None or stats.total_trades < 5:
                    continue
                score = stats.win_rate * 0.6 + stats.avg_return_pct * 10.0
                scored.append((sid, round(score, 3)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]

    # ----- 持久化 -----
    def persist(self) -> None:
        """持久化绩效数据到 JSON"""
        if not self._persist_path:
            return
        try:
            with self._lock:
                data = {}
                for sid, regimes in self._perf.items():
                    data[sid] = {}
                    for r, stats in regimes.items():
                        if stats.total_trades > 0:
                            data[sid][r] = stats.to_dict()
                p = Path(self._persist_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({
                    "updated": datetime.now().isoformat(),
                    "strategies": data,
                }, indent=2, ensure_ascii=False))
            logger.info("[PerfTracker] 绩效已持久化: %s", self._persist_path)
        except Exception as e:
            logger.warning("[PerfTracker] 持久化失败: %s", e)

    def load(self) -> bool:
        """从 JSON 恢复绩效数据"""
        if not self._persist_path or not os.path.exists(self._persist_path):
            return False
        try:
            blob = json.loads(Path(self._persist_path).read_text())
            strategies = blob.get("strategies", {})
            loaded = 0
            with self._lock:
                for sid, regimes in strategies.items():
                    for r, rdict in regimes.items():
                        if r in self.REGIMES:
                            stats = self._perf[sid][r]
                            stats.total_trades = rdict.get("total_trades", 0)
                            stats.winning_trades = int(
                                stats.total_trades * rdict.get("win_rate", 0)
                            )
                            stats.total_pnl_pct = rdict.get("avg_return_pct", 0) * stats.total_trades
                            stats.avg_win_pct = rdict.get("avg_win_pct", 0)
                            stats.avg_loss_pct = rdict.get("avg_loss_pct", 0)
                            stats.max_drawdown_pct = rdict.get("max_drawdown_pct", 0)
                            loaded += 1
            logger.info("[PerfTracker] 已从文件恢复 %d 条绩效记录", loaded)
            self._last_sync = datetime.now()
            return True
        except Exception as e:
            logger.warning("[PerfTracker] 恢复失败: %s", e)
            return False

    # ----- 每日摘要 -----
    def generate_daily_summary(self) -> str:
        """生成当日策略绩效摘要（供钉钉推送）"""
        self.sync_from_db()
        lines = ["[策略绩效摘要]", ""]

        # 按策略族分组
        try:
            from .registry import get_strategy_registry
            reg = get_strategy_registry()
            groups = reg.list_groups()
        except Exception:
            groups = {}

        for group, sids in groups.items():
            lines.append(f"--- {group.upper()} ---")
            for sid in sids:
                stats = self.get_strategy_stats(sid)
                summary = stats.get("summary", {})
                if summary.get("total_trades", 0) == 0:
                    continue
                lines.append(
                    f"  {sid}: 交易{summary['total_trades']}笔 | "
                    f"胜率{summary['win_rate']:.0%} | "
                    f"均收{summary['avg_return_pct']:.2%}"
                )
                # 各市场状态明细
                for r, rs in stats.get("regimes", {}).items():
                    if rs["total_trades"] > 0:
                        lines.append(
                            f"    {r}: {rs['total_trades']}笔 "
                            f"WR={rs['win_rate']:.0%} AR={rs['avg_return_pct']:.2%}"
                        )

        if len(lines) <= 2:
            lines.append("  (暂无足够交易数据)")

        return "\n".join(lines)


# ============================================================
#  便捷函数
# ============================================================
def get_performance_tracker() -> PerformanceTracker:
    """获取全局性能追踪器"""
    tracker = PerformanceTracker()
    # 首次获取时尝试恢复数据
    if tracker._last_sync is None:
        tracker.load()
        tracker.sync_from_db()
    return tracker
