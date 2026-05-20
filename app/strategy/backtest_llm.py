"""
FTSeek-US LLM 策略回测验证模块

使用历史新闻 + 历史行情回测 LLM 生成的策略质量。
目的: 在自动执行前验证推理引擎的有效性。

回测流程:
1. 加载历史新闻 (RSSHub + Finnhub)
2. 对每条新闻运行 LLM 推理生成策略
3. 用历史行情验证策略盈亏
4. 计算胜率、盈亏比、夏普比率等指标

作者: FTSeek Team
日期: 2026-04-16
"""

import os
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger("ftseek.backtest_llm")


@dataclass
class BacktestResult:
    """回测结果"""
    total_strategies: int = 0
    winning: int = 0
    losing: int = 0
    breakeven: int = 0
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    total_pnl: float = 0.0
    strategies_detail: List[Dict] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "total_strategies": self.total_strategies,
            "winning": self.winning,
            "losing": self.losing,
            "breakeven": self.breakeven,
            "win_rate": round(self.win_rate, 4),
            "avg_pnl": round(self.avg_pnl, 4),
            "avg_win": round(self.avg_win, 4),
            "avg_loss": round(self.avg_loss, 4),
            "profit_factor": round(self.profit_factor, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "total_pnl": round(self.total_pnl, 4),
        }


class LLMStrategyBacktester:
    """LLM 策略回测器"""
    
    def __init__(self, data_dir: str = None):
        if data_dir:
            self.data_dir = Path(data_dir)
        else:
            from app.core.paths import get_data_dir
            self.data_dir = get_data_dir()
        self.news_db = self.data_dir / "rsshub_us" / "news_us.db"
        self.backtest_db = self.data_dir / "backtest_llm_strategies.db"
        self._init_backtest_db()
    
    def _init_backtest_db(self):
        """初始化回测数据库"""
        import sqlite3
        conn = sqlite3.connect(str(self.backtest_db))
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS backtest_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id TEXT,
                symbol TEXT,
                direction TEXT,
                entry_time TEXT,
                exit_time TEXT,
                entry_price REAL,
                exit_price REAL,
                pnl_pct REAL,
                is_win INTEGER,
                reasoning TEXT,
                run_date TEXT,
                news_ids TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bt_symbol ON backtest_results(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bt_direction ON backtest_results(direction)")
        conn.commit()
        conn.close()
    
    def run_backtest(self, days: int = 30) -> BacktestResult:
        """
        运行回测
        
        Args:
            days: 回测天数 (默认30天)
        
        Returns:
            BacktestResult 回测结果
        """
        result = BacktestResult()
        
        # 检查新闻数据库是否存在
        if not self.news_db.exists():
            logger.warning("[回测] 新闻数据库不存在, 无法回测")
            return result
        
        # 获取历史新闻
        news_items = self._load_historical_news(days)
        if not news_items:
            logger.warning(f"[回测] 过去 {days} 天无新闻数据")
            return result
        
        logger.info(f"[回测] 加载 {len(news_items)} 条历史新闻 (过去 {days} 天)")
        
        # TODO: 实际需要:
        # 1. 对每条新闻运行 LLM 推理
        # 2. 获取对应股票的历史行情 (从 TwelveData/Finnhub)
        # 3. 计算策略盈亏
        # 4. 统计指标
        
        # 当前先用已有数据做基础统计
        symbols_seen = set()
        sources_seen = {}
        
        for n in news_items:
            try:
                syms = json.loads(n.get('symbols', '[]'))
                symbols_seen.update(syms)
                src = n.get('source_name', 'unknown')
                sources_seen[src] = sources_seen.get(src, 0) + 1
            except Exception:
                pass
        
        logger.info(f"[回测] 统计: {len(symbols_seen)} 只股票, 来源: {sources_seen}")
        logger.info(f"[回测] 当前阶段: 数据收集期, 待积累足够新闻后执行完整回测")
        logger.info(f"[回测] 建议: 积累 7 天以上新闻数据后运行完整回测")
        
        # 保存统计快照
        self._save_snapshot({
            "days": days,
            "news_count": len(news_items),
            "symbol_count": len(symbols_seen),
            "sources": sources_seen,
            "timestamp": datetime.now().isoformat(),
        })
        
        return result
    
    def _load_historical_news(self, days: int) -> List[Dict]:
        """加载历史新闻"""
        import sqlite3
        conn = sqlite3.connect(str(self.news_db))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        cursor.execute("""
            SELECT * FROM us_news 
            WHERE fetched_at > ? 
            ORDER BY fetched_at DESC
        """, (cutoff,))
        
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    
    def _save_snapshot(self, snapshot: Dict):
        """保存回测统计快照"""
        snapshot_path = self.data_dir / "backtest_snapshots.json"
        snapshots = []
        
        if snapshot_path.exists():
            try:
                with open(snapshot_path, 'r') as f:
                    snapshots = json.load(f)
            except Exception:
                pass
        
        snapshots.append(snapshot)
        snapshots = snapshots[-30:]  # 保留最近30次
        
        with open(snapshot_path, 'w') as f:
            json.dump(snapshots, f, ensure_ascii=False, indent=2)
    
    def load_backtest_history(self, limit: int = 50) -> List[Dict]:
        """加载历史回测结果"""
        import sqlite3
        if not self.backtest_db.exists():
            return []
        
        conn = sqlite3.connect(str(self.backtest_db))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM backtest_results 
            ORDER BY entry_time DESC LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]


def main():
    """独立运行回测"""
    logging.basicConfig(level=logging.INFO)
    backtester = LLMStrategyBacktester()
    result = backtester.run_backtest(days=30)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
