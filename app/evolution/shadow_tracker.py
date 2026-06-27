#!/usr/bin/env python3
"""
FTSeek-US 影子策略追踪器 (Shadow Tracker) - 工业级实现

⚠️ DEPRECATED (2026-06-21): 此模块已停用
原因: scheduler_triggers.py Phase 6 已 REMOVED (2026-06-19), proposals.db 0行数据
      Playbook shadow 模式(app/playbook/shadow_tracker.py)替代了此模块
保留原因: improvement_pipeline.py 仍引用 PromotionEngine (每周LLM假设写入proposals.db)
状态: 不删除(避免import断裂), 但 run_daily_eval 永远不会被调用

原始职责：
1. 管理待测试的提议（Proposals）。
2. 分层测试：Level 1 (信号), Level 2 (退出), Level 3 (全栈)。
3. 引入虚拟账户、滑点模型和严格的统计学决策引擎。
4. 自动执行策略切换，并建立回滚影子。

2026-05-13: 初始实现
2026-05-14: 完善 Level 2 退出影子逻辑与回滚机制
"""
import json
import logging
from app.storage.db_pool import get_pool
import sqlite3  # kept for schema init with custom paths
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from pathlib import Path

from app.storage.daily_log_store import DailyLogStore
from app.evolution.promotion_engine import PromotionEngine

logger = logging.getLogger("ftseek_us.evolution.shadow")


# ============================================================
#  核心组件：滑点模型 (Trap 1)
# ============================================================
class SlippageModel:
    """
    模拟交易成本，防止影子策略产生虚假的高收益。
    Shadow Fill Price = Market Price + Slippage + Fees
    """
    def __init__(self, atr_multiplier: float = 0.05, commission_bps: float = 1.0):
        self.atr_mult = atr_multiplier
        self.commission_bps = commission_bps

    def get_fill_price(self, price: float, atr: float, side: str) -> float:
        if atr <= 0:
            return price
        slippage = atr * self.atr_mult
        fee = price * (self.commission_bps / 10000.0)
        cost = slippage + fee
        if side == 'BUY':
            return price + cost
        else:
            return price - cost


# ============================================================
#  核心组件：虚拟账户 (Trap 2)
# ============================================================
class VirtualAccount:
    """影子策略的独立账户，模拟真实资金约束。"""
    def __init__(self, initial_capital: float = 100000.0, max_positions: int = 5):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, Dict] = {}
        self.max_positions = max_positions

    def can_open(self, symbol: str, price: float, size: int, max_pct: float = 0.15) -> bool:
        if symbol in self.positions:
            return False
        if len(self.positions) >= self.max_positions:
            return False
        cost = price * size
        if cost > self.cash:
            return False
        if cost > self.initial_capital * max_pct:
            return False
        return True

    def open_position(self, symbol: str, price: float, size: float, timestamp: str):
        cost = price * size
        self.cash -= cost
        self.positions[symbol] = {'size': size, 'entry_price': price, 'entry_time': timestamp}

    def close_position(self, symbol: str, price: float, timestamp: str) -> float:
        if symbol not in self.positions: return 0.0
        pos = self.positions.pop(symbol)
        pnl = (price - pos['entry_price']) * pos['size']
        self.cash += price * pos['size']
        return pnl


# ============================================================
#  主控制器：Shadow Tracker
# ============================================================
class ShadowTracker:
    def __init__(self, proposals_db: str = None):
        self.proposals_db = Path(proposals_db) if proposals_db else (
            Path(__file__).resolve().parent.parent.parent / "data" / "proposals.db"
        )
        self.log_store = DailyLogStore()
        self.promotion_engine = PromotionEngine()
        self.slippage_model = SlippageModel()
        self._ensure_shadow_schema()

    def _ensure_shadow_schema(self):
        try:
            # Defensive: delete 0-byte DB files (corrupted from interrupted migration)
            if self.proposals_db.exists() and self.proposals_db.stat().st_size == 0:
                logger.warning(f"[Shadow] 检测到 0 字节 DB 文件，删除并重建: {self.proposals_db}")
                self.proposals_db.unlink()
            conn = get_pool().connection("proposals")
            # 先确保 proposals 表存在（improvement_pipeline.py 也会创建，但 ShadowTracker 可能先初始化）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    proposer TEXT,
                    target_param TEXT,
                    current_value TEXT,
                    proposed_value TEXT,
                    rationale TEXT,
                    evidence TEXT,
                    expected_improvement TEXT,
                    failure_hypothesis TEXT,
                    confidence REAL,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT,
                    approved_at TEXT,
                    approved_by TEXT,
                    rejected_reason TEXT,
                    backtest_result TEXT,
                    test_start_date TEXT,
                    test_days_count INTEGER DEFAULT 0,
                    shadow_pnl_total REAL DEFAULT 0.0,
                    shadow_win_count REAL DEFAULT 0.0,
                    shadow_total_count REAL DEFAULT 0.0,
                    test_mode TEXT DEFAULT 'level3',
                    rollback_until TEXT
                )
            """)
            conn.commit()
            cursor = conn.execute("PRAGMA table_info(proposals)")
            columns = [row[1] for row in cursor.fetchall()]
            
            # 检查提议表字段
            needed_prop = ["test_start_date", "test_days_count", "shadow_pnl_total",
                      "test_mode", "rollback_until", "strategy_type"]
            for col in needed_prop:
                if col not in columns:
                    if col == "test_mode":
                        conn.execute("ALTER TABLE proposals ADD COLUMN test_mode TEXT DEFAULT 'level3'")
                    elif col == "rollback_until":
                        conn.execute("ALTER TABLE proposals ADD COLUMN rollback_until TEXT")
                    elif col == "strategy_type":
                        conn.execute("ALTER TABLE proposals ADD COLUMN strategy_type TEXT DEFAULT 'main'")
                    else:
                        if "date" in col:
                            conn.execute(f"ALTER TABLE proposals ADD COLUMN {col} TEXT")
                        else:
                            conn.execute(f"ALTER TABLE proposals ADD COLUMN {col} REAL DEFAULT 0.0")
            
            # 创建每日结果表 (用于 T-test)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shadow_daily_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT,
                    strategy_type TEXT DEFAULT 'main',
                    date_str TEXT,
                    real_pnl REAL,
                    shadow_pnl REAL,
                    diff_pnl REAL,
                    regime TEXT DEFAULT 'UNKNOWN'
                )
            """)
            # 检查 shadow_daily_results 表字段
            cursor = conn.execute("PRAGMA table_info(shadow_daily_results)")
            daily_cols = [row[1] for row in cursor.fetchall()]
            if "strategy_type" not in daily_cols:
                conn.execute("ALTER TABLE shadow_daily_results ADD COLUMN strategy_type TEXT DEFAULT 'main'")
                logger.info("[Shadow] 已迁移 shadow_daily_results.strategy_type 列")
            if "regime" not in daily_cols:
                conn.execute("ALTER TABLE shadow_daily_results ADD COLUMN regime TEXT DEFAULT 'UNKNOWN'")
                logger.info("[Shadow] 已迁移 shadow_daily_results.regime 列")

            conn.commit()
        except Exception as e:
            logger.error(f"[Shadow] Schema 迁移失败: {e}")

    def start_shadow_test(self, proposal_id: str):
        try:
            test_mode = self._determine_test_mode(proposal_id)
            self._update_proposal_status(proposal_id, "testing")
            conn = get_pool().connection("proposals")
            conn.execute("UPDATE proposals SET test_start_date = ?, test_mode = ? WHERE proposal_id = ?", 
                        (datetime.now().strftime('%Y-%m-%d'), test_mode, proposal_id))
            conn.commit()
            logger.info(f"[Shadow] 开始测试提议: {proposal_id} (模式: {test_mode})")
        except Exception as e:
            logger.error(f"[Shadow] 启动测试失败: {e}")

    def _determine_test_mode(self, proposal_id: str) -> str:
        try:
            conn = get_pool().connection("proposals")
            row = conn.execute("SELECT target_param FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
            if row:
                param = row[0].lower()
                if 'min_confidence' in param or 'min_score' in param:
                    return 'level1'
                if any(k in param for k in ['take_profit', 'stop_loss', 'trailing', 'atr_mult', 'max_loss']):
                    return 'level2'
            return 'level3'
        except (sqlite3.Error, IndexError):
            return 'level3'

    def run_daily_eval(self, strategy_type: str, symbol: str, date_str: str) -> List[Dict]:
        logs = self.log_store.get_day_data(symbol, date_str)
        if not logs:
            return []

        real_stats = self._get_real_pnl_stats(symbol, date_str, strategy_type)
        market_regime = self._get_market_regime(date_str)
        testing_proposals = self._get_active_testing_proposals(strategy_type=strategy_type, limit=PromotionEngine.MAX_ACTIVE_SHADOWS)
        results = []

        for prop in testing_proposals:
            try:
                new_params = json.loads(prop['proposed_value']) if prop.get('proposed_value') else {}
                current_params = self._load_current_params(strategy_type)
                
                mode = prop.get('test_mode', 'level3')
                if mode == 'level2':
                    shadow_pnl, shadow_wr, daily_diff = self._simulate_level2(logs, current_params, new_params)
                elif mode == 'level1':
                    shadow_pnl, shadow_wr, daily_diff = self._simulate_level1(logs, current_params, new_params)
                else:
                    shadow_pnl, shadow_wr, daily_diff = self._simulate_level3(logs, current_params, new_params)
                
                # 记录每日结果（含策略类型）
                self._record_daily_result(prop['proposal_id'], strategy_type, date_str, real_stats['pnl'], shadow_pnl, daily_diff, market_regime)
                self._update_shadow_stats(prop['proposal_id'], shadow_pnl, shadow_wr, daily_diff)

                decision = self._decide_fate(prop['proposal_id'], strategy_type)
                
                if decision == 'promoted':
                    self._execute_promotion(prop['proposal_id'])
                    results.append({"proposal_id": prop['proposal_id'], "strategy": strategy_type, "decision": "promoted"})
                elif decision == 'rejected':
                    self._update_proposal_status(prop['proposal_id'], "rejected")
                    results.append({"proposal_id": prop['proposal_id'], "strategy": strategy_type, "decision": "rejected"})
                else:
                    results.append({"proposal_id": prop['proposal_id'], "strategy": strategy_type, "decision": "continue"})

            except Exception as e:
                logger.error(f"[Shadow] 评估 {prop['proposal_id']} 失败: {e}")

        return results

    # --- 模拟引擎 ---

    def _simulate_level1(self, logs, current_params, new_params) -> Tuple[float, float, float]:
        """
        Level 1: 信号影子 — 比较触发率 + 后续收益方向

        不模拟成交，只记录: 如果用了新阈值，会多触发多少信号，
        以及这些增量信号在后续时间窗口中的表现。
        """
        min_score_new = new_params.get("min_score", current_params.get("min_score", 50))
        min_score_main = current_params.get("min_score", 50)

        shadow_signals = 0
        main_signals = 0
        shadow_follow_up_pnl = 0.0
        main_follow_up_pnl = 0.0

        for tick in logs:
            score = tick.get('score', 0)
            follow_up = tick.get('follow_up_pnl', 0)

            if score >= min_score_new:
                shadow_signals += 1
                shadow_follow_up_pnl += follow_up
            if score >= min_score_main:
                main_signals += 1
                main_follow_up_pnl += follow_up

        incremental = shadow_signals - main_signals
        avg_shadow_pnl = shadow_follow_up_pnl / max(shadow_signals, 1)
        avg_main_pnl = main_follow_up_pnl / max(main_signals, 1)

        if incremental > main_signals * 0.5 and avg_shadow_pnl < avg_main_pnl:
            quality_score = -0.01
        elif incremental > 0 and avg_shadow_pnl > avg_main_pnl:
            quality_score = 0.01
        else:
            quality_score = 0.0

        wr = 1.0 if quality_score > 0 else 0.0
        return quality_score, wr, quality_score

    def _simulate_level2(self, logs, current_params, new_params) -> Tuple[float, float, float]:
        """
        Level 2: 退出影子 — 在主线持仓上模拟新退出参数

        数据依赖: logs 需包含 close, atr, is_entry, is_exit 字段。
        如果 is_entry/is_exit 不存在, 默认 False (不会触发模拟)。
        建议数据源: 从 trade_records 的 entry_time/exit_time 与 market_ticks join。
        """
        trades = []
        in_trade = False
        entry_price_with_slippage = 0.0

        tp1_new = new_params.get("tp1", current_params.get("tp1", 0.04))
        sl_new = new_params.get("sl", current_params.get("sl", 0.03))

        for tick in logs:
            close = tick.get('close', 0)
            if close <= 0:
                continue
            atr = tick.get('atr', close * 0.01)
            is_entry = tick.get('is_entry', False)
            is_exit = tick.get('is_exit', False)
            
            if is_entry and not in_trade:
                in_trade = True
                entry_price_with_slippage = self.slippage_model.get_fill_price(close, atr, 'BUY')
            
            if in_trade:
                pnl_pct = (close - entry_price_with_slippage) / entry_price_with_slippage
                if pnl_pct <= -sl_new or pnl_pct >= tp1_new:
                    shadow_exit = self.slippage_model.get_fill_price(close, atr, 'SELL')
                    pnl = (shadow_exit - entry_price_with_slippage) / entry_price_with_slippage
                    trades.append(pnl)
                    in_trade = False
                elif is_exit:
                    shadow_exit = self.slippage_model.get_fill_price(close, atr, 'SELL')
                    pnl = (shadow_exit - entry_price_with_slippage) / entry_price_with_slippage
                    trades.append(pnl)
                    in_trade = False
                    
        if trades:
            return sum(trades), sum(1 for t in trades if t > 0) / len(trades), sum(trades)
        return 0.0, 0.0, 0.0

    def _simulate_level3(self, logs, current_params, new_params) -> Tuple[float, float, float]:
        """
        Level 3: 全栈影子 — 完整虚拟账户 + 资金约束 + 滑点

        与 shadow.md 的 ShadowAccount 对齐:
        - 初始资金 = 100000
        - 单仓上限 = 初始资金 × 15%
        - 最大并发 = 3
        - 所有成交价都扣滑点 + 手续费
        """
        account = VirtualAccount(initial_capital=100000, max_positions=3)
        trades = []

        tp1 = new_params.get("tp1", current_params.get("tp1", 0.04))
        sl = new_params.get("sl", current_params.get("sl", 0.03))
        min_score = new_params.get("min_score", current_params.get("min_score", 50))
        position_pct = new_params.get("position_pct", 0.10)  # 默认单仓 10%

        for tick in logs:
            close = tick.get('close', 0)
            if close <= 0:
                continue
            atr = tick.get('atr', close * 0.01)
            score = tick.get('score', 0)
            symbol = tick.get('symbol', 'UNKNOWN')

            if score >= min_score:
                size = max(1, int(account.initial_capital * position_pct / max(close, 0.01)))
                if account.can_open(symbol, close, size):
                    fill_price = self.slippage_model.get_fill_price(close, atr, 'BUY')
                    account.open_position(symbol, fill_price, size, tick['timestamp'])

            for sym, pos in list(account.positions.items()):
                pnl_pct = (close - pos['entry_price']) / pos['entry_price']
                if pnl_pct <= -sl or pnl_pct >= tp1:
                    fill_exit = self.slippage_model.get_fill_price(close, atr, 'SELL')
                    pnl = (fill_exit - pos['entry_price']) * pos['size']
                    trades.append(pnl / account.initial_capital)
                    account.close_position(sym, fill_exit, tick['timestamp'])

        if trades:
            return sum(trades), sum(1 for t in trades if t > 0) / len(trades), sum(trades)
        return 0.0, 0.0, 0.0

    # --- 决策与执行 ---

    def _decide_fate(self, proposal_id: str, strategy_type: str = "main") -> str:
        daily_diffs = self._get_daily_diffs(proposal_id)
        regime_labels = self._get_regime_labels(proposal_id)
        active_count = self._count_active_proposals(strategy_type)
        should_promote, reason = self.promotion_engine.should_promote(
            daily_diffs, proposal_id, active_count, regime_labels=regime_labels
        )

        logger.info(f"[Shadow] 决策 {proposal_id}: {reason}")

        if should_promote:
            return 'promoted'

        # 淘汰逻辑: 测试超过 10 天仍持续恶化 → 放弃
        if len(daily_diffs) >= 10 and sum(daily_diffs) < -0.03:
            return 'rejected'

        # 测试超过 30 天上限 → 强制淘汰
        conn = get_pool().connection("proposals")
        row = conn.execute(
            "SELECT test_days_count FROM proposals WHERE proposal_id = ?",
            (proposal_id,)
        ).fetchone()
        if row and row[0] and row[0] >= 30:
            return 'rejected'

        return 'continue'

    def _get_regime_labels(self, proposal_id: str) -> Optional[List[str]]:
        """从 shadow_daily_results 表读取 regime 标签"""
        try:
            conn = get_pool().connection("proposals")
            rows = conn.execute(
                "SELECT regime FROM shadow_daily_results WHERE proposal_id = ? ORDER BY date_str",
                (proposal_id,)
            ).fetchall()
            labels = [r[0] for r in rows if r[0]]
            return labels if labels else None
        except Exception:
            return None

    def _execute_promotion(self, proposal_id: str):
        try:
            conn = get_pool().connection("proposals")
            row = conn.execute("SELECT target_param, proposed_value, current_value FROM proposals WHERE proposal_id = ?", 
                              (proposal_id,)).fetchone()
            
            if not row: return
            target_param, proposed_value, current_value = row
            
            logger.warning(f"🚀 [自动优化] 策略 {proposal_id} 胜出! 应用参数: {target_param} -> {proposed_value}")
            
            # 创建回滚影子
            self._create_rollback_shadow(target_param, current_value, proposal_id)
            
            # 实际写入 ConfigWriter (模拟)
            self._update_proposal_status(proposal_id, "promoted")
            
        except Exception as e:
            logger.error(f"[Shadow] 执行切换失败: {e}")

    def _create_rollback_shadow(self, target_param: str, old_value: Any, original_id: str):
        try:
            rollback_id = f"rb_{original_id}"
            rollback_until = (datetime.now() + timedelta(days=10)).strftime('%Y-%m-%d')

            # 继承原提议的 strategy_type
            conn = get_pool().connection("proposals")
            row = conn.execute(
                "SELECT strategy_type FROM proposals WHERE proposal_id = ?",
                (original_id,)
            ).fetchone()
            inherited_strategy = row[0] if row else 'main'

            conn.execute("""
                INSERT OR REPLACE INTO proposals
                (proposal_id, proposer, target_param, current_value, proposed_value,
                 rationale, status, rollback_until, test_start_date, test_mode, strategy_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rollback_id, "rollback", target_param, old_value, "original",
                f"Rollback shadow for {original_id}", "testing", rollback_until,
                datetime.now().strftime('%Y-%m-%d'), "level3", inherited_strategy
            ))
            conn.commit()
            logger.info(f"[Shadow] 创建回滚影子: {rollback_id} (观察至 {rollback_until})")
        except Exception as e:
            logger.error(f"[Shadow] 创建回滚影子失败: {e}")

    def _get_market_regime(self, date_str: str) -> str:
        """推断当日的市场风格 (TREND / RANGE / CHOP / UNKNOWN)"""
        try:
            db_path = Path(__file__).resolve().parent.parent.parent / "data" / "shadow_logs.db"
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute(
                "SELECT close, vix FROM market_ticks WHERE timestamp LIKE ? ORDER BY id",
                (f"{date_str}%",)
            ).fetchall()
            conn.close()

            if not rows or len(rows) < 10:
                return "UNKNOWN"

            closes = [r[0] for r in rows if r[0] and r[0] > 0]
            if len(closes) < 2:
                return "UNKNOWN"

            # 日内涨跌幅
            day_open, day_close = closes[0], closes[-1]
            day_range_pct = abs(day_close - day_open) / day_open if day_open > 0 else 0

            # 路径效率: 净涨跌幅 / 累计绝对波动
            total_movement = sum(abs(closes[i] - closes[i-1]) for i in range(1, len(closes)))
            net_change = abs(day_close - day_open)
            path_efficiency = net_change / total_movement if total_movement > 0 else 0

            if path_efficiency > 0.55:
                return "TREND"
            elif path_efficiency < 0.35:
                return "CHOP"
            else:
                return "RANGE"
        except Exception as e:
            logger.debug(f"[Shadow] Regime 推断失败: {e}")
        return "UNKNOWN"

    # --- Helper Methods ---

    def _record_daily_result(self, pid, strategy_type, date_str, real_pnl, shadow_pnl, diff_pnl, regime="UNKNOWN"):
        try:
            conn = get_pool().connection("proposals")
            conn.execute("""
                INSERT INTO shadow_daily_results (proposal_id, strategy_type, date_str, real_pnl, shadow_pnl, diff_pnl, regime)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (pid, strategy_type, date_str, real_pnl, shadow_pnl, diff_pnl, regime))
            conn.commit()
        except Exception as e:
            logger.error(f"[Shadow] 记录每日结果失败: {e}")

    def _get_daily_diffs(self, pid) -> List[float]:
        try:
            conn = get_pool().connection("proposals")
            rows = conn.execute("SELECT diff_pnl FROM shadow_daily_results WHERE proposal_id = ? ORDER BY date_str", (pid,)).fetchall()
            return [r[0] for r in rows]
        except sqlite3.Error:
            return []

    def _get_active_testing_proposals(self, strategy_type: str = None, limit: int = 3) -> List[Dict]:
        try:
            conn = get_pool().connection("proposals")
            conn.row_factory = sqlite3.Row
            if strategy_type:
                rows = conn.execute(
                    "SELECT * FROM proposals WHERE status='testing' AND strategy_type=? ORDER BY test_start_date DESC LIMIT ?", 
                    (strategy_type, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM proposals WHERE status='testing' ORDER BY test_start_date DESC LIMIT ?", 
                    (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            return []

    def _get_real_pnl_stats(self, symbol, date_str, strategy_type: str = "main") -> Dict:
        """
        从 trade_records 表查询指定策略线的真实 PnL
        """
        try:
            from app.core.db_utils import get_main_db
            db_path = get_main_db()
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                """SELECT COALESCE(SUM(realized_pnl), 0) as total_pnl,
                          COUNT(*) as trade_count
                   FROM trade_records
                   WHERE trade_date = ? AND symbol = ?""",
                (date_str, symbol)
            ).fetchone()
            conn.close()
            if row:
                return {"pnl": float(row[0]), "trade_count": int(row[1])}
        except Exception as e:
            logger.debug(f"[Shadow] 真实 PnL 查询失败: {e}")
        return {"pnl": 0.0, "trade_count": 0}

    def _update_shadow_stats(self, pid, pnl, wr, daily_diff):
        try:
            conn = get_pool().connection("proposals")
            conn.execute("""
                UPDATE proposals SET 
                    shadow_pnl_total = COALESCE(shadow_pnl_total, 0) + ?,
                    shadow_win_count = COALESCE(shadow_win_count, 0) + ?,
                    shadow_total_count = COALESCE(shadow_total_count, 0) + 1,
                    test_days_count = test_days_count + 1
                WHERE proposal_id = ?
            """, (pnl, 1 if wr > 0 else 0, pid))
            conn.commit()
        except Exception as e:
            logger.error(f"[Shadow] 更新统计失败: {e}")

    def _count_active_proposals(self, strategy_type: str = None) -> int:
        try:
            conn = get_pool().connection("proposals")
            if strategy_type:
                count = conn.execute("SELECT COUNT(*) FROM proposals WHERE status='testing' AND strategy_type=?", (strategy_type,)).fetchone()[0]
            else:
                count = conn.execute("SELECT COUNT(*) FROM proposals WHERE status='testing'").fetchone()[0]
            return count
        except sqlite3.Error:
            return 0

    def _update_proposal_status(self, pid, status):
        try:
            conn = get_pool().connection("proposals")
            conn.execute("UPDATE proposals SET status = ? WHERE proposal_id = ?", (status, pid))
            conn.commit()
        except Exception as e:
            logger.error(f"[Shadow] 更新状态失败: {e}")

    def _load_current_params(self, strategy_type: str = "main") -> Dict:
        """从 unified_config.yaml 动态加载当前策略的基准参数"""
        params = {}
        try:
            from app.core.config_container import get_config
            cfg = get_config()

            risk_cfg = cfg.get_section("risk")
            params["min_score"] = risk_cfg.get("signal_quality", {}).get("min_score", 50)
            exit_cfg = cfg.get_section("dynamic_exit")
            default_exit = exit_cfg.get("default", {})
            params["tp1"] = abs(float(default_exit.get("take_profit", 0.10)))
            params["sl"] = abs(float(default_exit.get("stop_loss", -0.03)))
        except Exception as e:
            logger.debug(f"[Shadow] 配置读取失败，使用硬编码回退: {e}")

        # 硬编码回退（与 unified_config.yaml 保持一致）
        if not params:
            params = {"min_score": 50, "tp1": 0.10, "sl": 0.03}

        return params
