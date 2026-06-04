"""
期权快速获利了结引擎 (Option Quick Exit Engine) - 2026-04-15 新增

核心思路: 期权波动大，需要更积极的退出策略
- 盈利 8% 开始追踪止盈
- 从最高点回撤 5% 立即平仓 (锁定利润)
- 持仓 72h 无条件平仓 (避免时间价值损耗)
- 亏损 15% 硬止损 (2026-06-02 收紧: 原25%配置+滑点导致实际-40%~-63%亏损)

使用方式:
    from app.exit.option_quick_exit import OptionQuickExitEngine
    engine = OptionQuickExitEngine(config)
    should_exit, reason = engine.check_exit(position, current_price, pnl_pct)
"""

import logging
import sqlite3
import json
from typing import Dict, Tuple, Any, Optional
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 数据库路径 (与 trail_state_store 共用)
DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db")


class OptionQuickExitEngine:
    """期权快速获利了结引擎"""

    def __init__(self, config: Dict[str, Any] = None):
        config = config or {}
        self.enabled = config.get("enabled", True)
        self.trigger_profit = config.get("trigger_profit", 0.08)      # 8% 触发追踪
        self.trailing_stop = config.get("trailing_stop", 0.05)        # 5% 回撤平仓
        self.max_holding_hours = config.get("max_holding_hours", 72)  # 72h 强制平仓
        self.profit_target = config.get("profit_target_pct", 0.15)    # 15% 目标止盈
        self.stop_loss = config.get("stop_loss_pct", 0.15)            # 2026-06-02: 25% → 15% (实测5笔灾难亏损 -$10,519, 滑点~15%, 15%配置→实际~30%)
        self.time_decay_warning = config.get("time_decay_warning", 0.10)  # 10% 时间价值警告
        self.min_holding_hours_for_decay = config.get("min_holding_hours_for_decay", 24)  # P3: 至少持仓 24h 才考虑时间损耗

        # 跟踪最高盈利 (SQLite 持久化，重启后可恢复)
        self._peak_profits: Dict[str, float] = {}  # symbol -> peak_pnl_pct
        self._db_path = config.get("db_path", DEFAULT_DB_PATH)
        self._init_db()
        self._load_peaks_from_db()

    def check_exit(self, position: Dict[str, Any], current_price: float,
                   current_pnl_pct: float) -> Tuple[bool, str]:
        """
        检查是否应该退出期权持仓

        退出条件 (满足任一即退出，按优先级排序):
        1. 达到目标盈利 15% (直接获利了结)
        2. 移动止盈: 从最高点回撤 5% (锁定利润)
        3. 持仓超过 72 小时 (避免时间价值损耗)
        4. 时间价值损耗 > 10% 警告
        5. 止损触发 -15% (硬止损, 2026-06-02 收紧)
        """
        if not self.enabled:
            return False, ""

        symbol = position.get("symbol", "")
        entry_price = position.get("entry_price", 0)
        entry_time_str = position.get("entry_time", "")

        if entry_price <= 0:
            return False, "entry_price 无效"

        # 计算持仓时间
        holding_hours = self._calc_holding_hours(entry_time_str)

        # 更新最高盈利
        if current_pnl_pct > self._peak_profits.get(symbol, -999):
            self._peak_profits[symbol] = current_pnl_pct
            self._save_peak_to_db(symbol, current_pnl_pct)
        peak = self._peak_profits.get(symbol, 0)

        # === 优先级 1: 目标盈利达到 ===
        if current_pnl_pct >= self.profit_target:
            self._clear_peak(symbol)
            return True, f"🎯 目标盈利达到 ({current_pnl_pct:.1%} >= {self.profit_target:.1%})"

        # === 优先级 2: 移动止盈 ===
        if peak >= self.trigger_profit:
            drawdown = peak - current_pnl_pct
            if drawdown >= self.trailing_stop:
                self._clear_peak(symbol)
                return True, (
                    f"📉 移动止盈触发 (最高 {peak:.1%} → 当前 {current_pnl_pct:.1%}, "
                    f"回撤 {drawdown:.1%} >= {self.trailing_stop:.1%})"
                )

        # === 优先级 3: 持仓超时 ===
        if holding_hours >= self.max_holding_hours:
            self._clear_peak(symbol)
            return True, f"⏰ 持仓超时 ({holding_hours:.1f}h >= {self.max_holding_hours}h)"

        # === 优先级 4: 时间价值损耗 ===
        if self._check_time_decay(position, holding_hours):
            self._clear_peak(symbol)
            return True, f"⏳ 时间价值损耗过大"

        # === 优先级 5: 硬止损 ===
        if current_pnl_pct <= -self.stop_loss:
            self._clear_peak(symbol)
            return True, f"🛑 硬止损触发 ({current_pnl_pct:.1%} <= -{self.stop_loss:.1%})"

        # 未触发退出，输出追踪日志
        if peak >= self.trigger_profit:
            remaining = peak - current_pnl_pct
            if remaining > self.trailing_stop * 0.5:  # 超过一半回撤时警告
                logger.warning(
                    f"[期权追踪] {symbol} 最高 {peak:.1%} → 当前 {current_pnl_pct:.1%} | "
                    f"回撤 {remaining:.1%} (触发线 {self.trailing_stop:.1%})"
                )

        return False, ""

    def get_peak_profit(self, symbol: str) -> float:
        """获取当前最高盈利"""
        return self._peak_profits.get(symbol, 0)

    def reset_peak(self, symbol: str):
        """重置最高盈利跟踪"""
        self._clear_peak(symbol)

    def _clear_peak(self, symbol: str):
        """清除峰值跟踪"""
        self._peak_profits.pop(symbol, None)
        self._delete_peak_from_db(symbol)

    def _calc_holding_hours(self, entry_time_str: str) -> float:
        """计算持仓小时数"""
        if not entry_time_str:
            return 0
        try:
            entry_time = datetime.fromisoformat(entry_time_str)
            return (datetime.now() - entry_time).total_seconds() / 3600
        except (ValueError, TypeError):
            return 0

    def _check_time_decay(self, position: Dict[str, Any], holding_hours: float) -> bool:
        """
        检查时间价值损耗

        P3 优化：至少持仓 24h 才考虑时间损耗（避免过早平仓）
        简单估算：持仓时间 / 总到期时间 > 警告阈值
        """
        # P3: 持仓不足 24h 不考虑时间损耗
        if holding_hours < self.min_holding_hours_for_decay:
            return False

        expiry_str = position.get("expiry", "")
        if not expiry_str:
            return False

        try:
            entry_time_str = position.get("entry_time", "")
            if not entry_time_str:
                return False

            entry_time = datetime.fromisoformat(entry_time_str)
            # 尝试解析到期日 (支持多种格式)
            for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
                try:
                    expiry = datetime.strptime(expiry_str, fmt)
                    break
                except ValueError:
                    continue
            else:
                return False

            total_hours = (expiry - entry_time).total_seconds() / 3600
            if total_hours <= 0:
                return True  # 已过期

            decay_pct = holding_hours / total_hours
            return decay_pct > self.time_decay_warning

        except Exception as e:
            logger.debug(f"[时间价值] 计算失败：{e}")
            return False

    # ================================================================
    #  SQLite 持久化 (P0-3 修复: 重启后恢复 peak_profits)
    # ================================================================
    def _init_db(self):
        """创建 option_peak_profits 表 (如果不存在)"""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS option_peak_profits (
                        symbol TEXT PRIMARY KEY,
                        peak_pnl_pct REAL,
                        last_updated TEXT
                    )
                """)
                conn.commit()
        except Exception as e:
            logger.error(f"[OptionQuickExit] DB 初始化失败: {e}")

    def _load_peaks_from_db(self):
        """启动时从 SQLite 恢复 peak_profits"""
        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.execute("SELECT symbol, peak_pnl_pct FROM option_peak_profits")
                for symbol, peak in cursor.fetchall():
                    self._peak_profits[symbol] = peak
            if self._peak_profits:
                logger.info(f"[OptionQuickExit] 从 DB 恢复 {len(self._peak_profits)} 个期权峰值跟踪")
        except Exception as e:
            logger.warning(f"[OptionQuickExit] DB 加载失败: {e}")

    def _save_peak_to_db(self, symbol: str, peak_pnl_pct: float):
        """保存单个 peak 到 SQLite"""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO option_peak_profits (symbol, peak_pnl_pct, last_updated) VALUES (?, ?, ?)",
                    (symbol, peak_pnl_pct, datetime.now().isoformat())
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"[OptionQuickExit] 保存 peak 失败 {symbol}: {e}")

    def _delete_peak_from_db(self, symbol: str):
        """从 SQLite 删除 peak (平仓后清理)"""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("DELETE FROM option_peak_profits WHERE symbol = ?", (symbol,))
                conn.commit()
        except Exception as e:
            logger.debug(f"[OptionQuickExit] 删除 peak 失败 {symbol}: {e}")

    def get_status_report(self) -> Dict[str, Any]:
        """获取引擎状态报告"""
        return {
            "enabled": self.enabled,
            "active_tracking": len(self._peak_profits),
            "peak_profits": dict(self._peak_profits),
            "config": {
                "profit_target": f"{self.profit_target:.0%}",
                "trigger_profit": f"{self.trigger_profit:.0%}",
                "trailing_stop": f"{self.trailing_stop:.0%}",
                "max_holding_hours": self.max_holding_hours,
                "stop_loss": f"{self.stop_loss:.0%}",
            },
        }


# ============================================================
#  全局单例
# ============================================================
_quick_exit_engine = None


def get_quick_exit_engine(config: Dict[str, Any] = None) -> OptionQuickExitEngine:
    """获取快速退出引擎单例"""
    global _quick_exit_engine
    if _quick_exit_engine is None:
        _quick_exit_engine = OptionQuickExitEngine(config)
    return _quick_exit_engine
