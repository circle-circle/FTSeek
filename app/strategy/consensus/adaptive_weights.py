"""
自适应权重管理器 — 基于历史表现自动调整策略权重

从 consensus_engine.py 拆出 AdaptiveWeightManager 类。

职责：
1. 记录交易结果
2. 基于胜率/盈亏自动调整权重
3. 市场状态/场景权重调整
4. UCB (Upper Confidence Bound) 探索奖励
5. 权重持久化 (SQLite)
"""

import logging
import time
import threading
import sqlite3
from typing import Optional, Dict, List, Any
from datetime import datetime
from pathlib import Path

from app.core.config_loader import cfg

logger = logging.getLogger("ftseek.consensus.adaptive_weights")

_DB_PATH = Path(__file__).parent.parent.parent.parent / "data" / "consensus_weights.db"


class AdaptiveWeightManager:
    """
    自适应权重管理器
    
    基于历史交易表现自动调整策略权重。
    算法：基础权重 + 表现调整 + UCB 探索奖励
    """
    
    def __init__(self, engine_ref=None, config: Dict = None):
        """
        Args:
            engine_ref: 对 ConsensusEngine 的引用
            config: 自适应配置
        """
        self._engine_ref = engine_ref
        self._config = config or {}
        self._strategy_stats: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        
        # 初始化数据库
        self._init_db()
        
        # 加载历史权重
        self._load_from_db()
    
    def _init_db(self):
        """初始化权重数据库"""
        try:
            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(_DB_PATH)) as conn:
                c = conn.cursor()
                c.execute('''
                    CREATE TABLE IF NOT EXISTS strategy_weights (
                        strategy TEXT PRIMARY KEY,
                        base_weight REAL,
                        adjusted_weight REAL,
                        total_trades INTEGER,
                        win_rate REAL,
                        avg_pnl REAL,
                        updated_at TEXT
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS trade_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        strategy TEXT,
                        code TEXT,
                        action TEXT,
                        pnl REAL,
                        holding_hours REAL,
                        is_win INTEGER,
                        timestamp TEXT
                    )
                ''')
                conn.commit()
        except Exception as e:
            logger.error(f"自适应权重 DB 初始化失败: {e}")
    
    def record_trade(self, strategy: str, code: str, action: str,
                     pnl: float, holding_hours: float = 0):
        """
        记录交易结果
        
        Args:
            strategy: 策略名称
            code: 股票代码
            action: 交易方向
            pnl: 盈亏
            holding_hours: 持仓时长
        """
        is_win = pnl > 0
        
        with self._lock:
            # 更新统计
            if strategy not in self._strategy_stats:
                self._strategy_stats[strategy] = {
                    "total_trades": 0,
                    "win_count": 0,
                    "total_pnl": 0.0,
                    "holding_hours": 0.0,
                }
            
            stats = self._strategy_stats[strategy]
            stats["total_trades"] += 1
            if is_win:
                stats["win_count"] += 1
            stats["total_pnl"] += pnl
            stats["holding_hours"] += holding_hours
        
        # 持久化
        try:
            with sqlite3.connect(str(_DB_PATH)) as conn:
                c = conn.cursor()
                c.execute('''
                    INSERT INTO trade_records (strategy, code, action, pnl, holding_hours, is_win, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (strategy, code, action, pnl, holding_hours, int(is_win), datetime.now().isoformat()))
                conn.commit()
        except Exception as e:
            logger.error(f"记录交易结果失败: {e}")
    
    def update_weights_if_ready(self, min_trades: int = 20):
        """
        检查并更新权重
        
        当策略有足够的交易记录时，自动调整权重。
        """
        with self._lock:
            updated = {}
            
            for strategy, stats in self._strategy_stats.items():
                total = stats["total_trades"]
                if total < min_trades:
                    continue
                
                win_rate = stats["win_count"] / total
                avg_pnl = stats["total_pnl"] / total
                
                # 计算新权重
                base_weight = cfg.get(f"strategy.weights.{strategy}", 0.5)
                
                # 表现调整
                performance_delta = (win_rate - 0.5) * 0.5  # 胜率 > 50% 奖励
                ucb_bonus = self._calculate_ucb_bonus(strategy, total)
                
                # 市场状态调整
                regime_adjust = self._calculate_regime_adjustment(strategy)
                
                # 场景调整
                scenario_delta = self._calculate_scenario_delta(strategy)
                
                # 最终权重
                new_weight = base_weight + performance_delta + ucb_bonus + regime_adjust + scenario_delta
                new_weight = max(0.0, min(3.0, new_weight))  # 限制范围 [0, 3]
                
                if abs(new_weight - base_weight) > 0.05:
                    updated[strategy] = {
                        "old_weight": base_weight,
                        "new_weight": new_weight,
                        "win_rate": win_rate,
                        "total_trades": total,
                    }
                    
                    # 保存到 DB
                    self._save_weight(strategy, new_weight, total, win_rate, avg_pnl)
            
            if updated:
                logger.info(f"自适应权重已更新: {updated}")
                # 通知引擎重新评估
                if self._engine_ref and hasattr(self._engine_ref, 'update_params'):
                    self._engine_ref.update_params({"weights_updated": True})
    
    def _calculate_ucb_bonus(self, strategy: str, total_trades: int) -> float:
        """UCB (Upper Confidence Bound) 探索奖励"""
        total_all = sum(s["total_trades"] for s in self._strategy_stats.values())
        if total_all == 0 or total_trades == 0:
            return 0.0
        
        import math
        exploration = math.sqrt(2 * math.log(total_all) / total_trades)
        return exploration * 0.1  # 缩放因子
    
    def _calculate_regime_adjustment(self, strategy: str) -> float:
        """市场状态权重调整"""
        regime_params = cfg.get(f"dynamic_exit_params.default", {})
        # 简化实现，可根据实际需求扩展
        return 0.0
    
    def _calculate_scenario_delta(self, strategy: str) -> float:
        """场景权重调整"""
        # 简化实现，可根据实际需求扩展
        return 0.0
    
    def _save_weight(self, strategy: str, weight: float,
                     total_trades: int, win_rate: float, avg_pnl: float):
        """保存权重到数据库"""
        try:
            with sqlite3.connect(str(_DB_PATH)) as conn:
                c = conn.cursor()
                c.execute('''
                    INSERT OR REPLACE INTO strategy_weights 
                    (strategy, base_weight, adjusted_weight, total_trades, win_rate, avg_pnl, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (strategy, cfg.get(f"strategy.weights.{strategy}", 0.5),
                      weight, total_trades, win_rate, avg_pnl, datetime.now().isoformat()))
                conn.commit()
        except Exception as e:
            logger.error(f"保存权重失败: {e}")
    
    def _load_from_db(self):
        """从数据库加载历史权重"""
        try:
            with sqlite3.connect(str(_DB_PATH)) as conn:
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT * FROM strategy_weights")
                for row in c.fetchall():
                    self._strategy_stats[row["strategy"]] = {
                        "total_trades": row["total_trades"],
                        "win_count": int(row["win_rate"] * row["total_trades"]),
                        "total_pnl": row["avg_pnl"] * row["total_trades"],
                    }
            logger.info(f"自适应权重已加载 ({len(self._strategy_stats)} 个策略)")
        except Exception as e:
            logger.error(f"加载权重失败: {e}")
    
    def get_strategy_stats(self) -> Dict[str, Dict]:
        """获取策略统计"""
        with self._lock:
            result = {}
            for strategy, stats in self._strategy_stats.items():
                total = stats["total_trades"]
                result[strategy] = {
                    "total_trades": total,
                    "win_rate": stats["win_count"] / total if total > 0 else 0.0,
                    "avg_pnl": stats["total_pnl"] / total if total > 0 else 0.0,
                    "total_pnl": stats["total_pnl"],
                }
            return result
