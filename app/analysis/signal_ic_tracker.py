"""
Signal IC Tracker — 信号预测力追踪系统 (Phase 1)

设计原则:
  1. 不动交易热路径 — 记录函数非阻塞, 失败不抛异常
  2. 利用现有 decisions.db — 镜像信号元数据到 signal_snapshots 表
  3. 前瞻收益盘后回填 — 由 scripts/backfill_forward_returns.py 批处理

核心指标:
  Signal IC = Spearman rank correlation(signal_strength, forward_return)
  - IC > 0.05: 信号有预测力
  - IC ≈ 0:    信号是噪音
  - IC < 0:    信号反向

数据流:
  signal_pipeline → decision_recorder.record_decision()
    → record_signal_snapshot()     # 实时: 记录信号快照
  cron 17:00 ET → backfill_forward_returns.py
    → backfill_batch()             # 盘后: 回填 T+1/T+5/T+20 前瞻收益
  post_market_audit → get_ic_summary()
    → 盘后报告中展示 IC 摘要
"""
from __future__ import annotations

import sqlite3
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger("ftseek_us.signal_ic_tracker")

# DB 路径: 与 decisions.db 同目录, 独立文件
_DB_PATH: Optional[Path] = None


def _get_db_path() -> Path:
    """获取 signal_snapshots.db 路径 (lazy init)"""
    global _DB_PATH
    if _DB_PATH is None:
        try:
            from app.core.paths import get_data_dir
            _DB_PATH = get_data_dir() / "signal_snapshots.db"
        except Exception:
            _DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "signal_snapshots.db"
    return _DB_PATH


def _conn() -> sqlite3.Connection:
    """创建 DB 连接"""
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ═══════════════════════════════════════════════════════════════════
#  表初始化
# ═══════════════════════════════════════════════════════════════════

_INITIALIZED = False
_INIT_LOCK = threading.Lock()


def _ensure_table():
    """确保 signal_snapshots 表存在 (幂等, 线程安全)"""
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        conn = _conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_snapshots (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    -- 信号元数据 (从 decisions.db 镜像)
                    decision_id     TEXT NOT NULL UNIQUE,
                    symbol          TEXT NOT NULL,
                    direction       TEXT NOT NULL,          -- LONG / SHORT
                    strength        INTEGER,                -- 0-100
                    confidence      REAL,
                    consensus_level REAL,
                    strategy_id     TEXT,                   -- main / fast_lane / llm_plan
                    created_at      TEXT NOT NULL,          -- 信号生成时间 (ISO format)
                    trade_date      TEXT NOT NULL,          -- 交易日 (YYYY-MM-DD, ET)
                    
                    -- 信号时刻价格 (回填时获取)
                    price_at_signal REAL,
                    
                    -- 前瞻收益 (盘后回填)
                    close_t0        REAL,                   -- 信号当日收盘价
                    close_t1        REAL,                   -- T+1 收盘价
                    close_t5        REAL,                   -- T+5 收盘价
                    close_t20       REAL,                   -- T+20 收盘价
                    fwd_ret_t1      REAL,                   -- 前瞻收益率 T+1 (方向调整)
                    fwd_ret_t5      REAL,                   -- 前瞻收益率 T+5
                    fwd_ret_t20     REAL,                   -- 前瞻收益率 T+20
                    
                    -- 状态
                    realized        INTEGER DEFAULT 0,      -- 0=待回填, 1=T1已回填, 2=T5已回填, 3=全部回填
                    ic_batch_date   TEXT,                   -- 最近回填日期
                    created_at_snap TEXT NOT NULL,           -- 快照创建时间
                    trade_mode      TEXT DEFAULT 'simulate',  -- live / simulate (P0 fix 2026-06-16)
                    weight_version  TEXT DEFAULT 'v1'          -- Task #4: 权重版本追踪
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_symbol ON signal_snapshots(symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_date ON signal_snapshots(trade_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_realized ON signal_snapshots(realized)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_strategy ON signal_snapshots(strategy_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_decision ON signal_snapshots(decision_id)")
            # P0 fix (2026-06-16): 为已有表添加缺失的 trade_mode 列
            try:
                conn.execute("ALTER TABLE signal_snapshots ADD COLUMN trade_mode TEXT DEFAULT 'simulate'")
                logger.info("[SignalIC] 已添加 trade_mode 列")
            except Exception:
                pass  # 列已存在
            # Task #4 (2026-06-17): 为已有表添加 weight_version 列
            try:
                conn.execute("ALTER TABLE signal_snapshots ADD COLUMN weight_version TEXT DEFAULT 'v1'")
                logger.info("[SignalIC] 已添加 weight_version 列")
            except Exception:
                pass  # 列已存在
            conn.commit()
            _INITIALIZED = True
            logger.info(f"[SignalIC] 表初始化完成: {_get_db_path()}")
        except Exception as e:
            logger.error(f"[SignalIC] 表初始化失败: {e}")
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════
#  实时记录 (热路径钩子)
# ═══════════════════════════════════════════════════════════════════

def record_signal_snapshot(
    decision_id: str,
    symbol: str,
    direction: str,
    strength: int,
    confidence: Optional[float] = None,
    consensus_level: Optional[float] = None,
    strategy_id: Optional[str] = None,
    created_at: Optional[str] = None,
    trade_date: Optional[str] = None,
):
    """
    记录信号快照 — 由 decision_recorder.record_decision() 调用
    
    设计: 非阻塞, 失败静默, 不影响主流程。
    每个 decision_id 唯一 (UNIQUE 约束), 重复调用安全忽略。
    
    Args:
        decision_id: 决策 ID (DEC-xxx)
        symbol: US.TSLA
        direction: LONG / SHORT
        strength: 0-100
        confidence: 0.0-1.0
        consensus_level: 0.0-1.0
        strategy_id: 策略标识
        created_at: ISO 格式时间戳
    """
    try:
        _ensure_table()
        
        if created_at is None:
            created_at = datetime.now().isoformat()
        
        # 计算交易日 (ET) — 支持显式传入 (历史回填) 或自动推导 (实时)
        if trade_date is None:
            try:
                from app.utils.timezone import get_trade_date
                trade_date = get_trade_date()
            except Exception:
                trade_date = datetime.now().strftime("%Y-%m-%d")
        
        now_snap = datetime.now().isoformat()
        
        # 判断当前交易模式 (实盘/模拟)
        try:
            from app.core.db_utils import is_live_mode
            _trade_mode = 'live' if is_live_mode() else 'simulate'
        except Exception:
            _trade_mode = 'simulate'
        
        # Task #4: 记录当前权重版本
        try:
            from app.core.feature_flags import FeatureFlags
            _weight_version = "v2" if FeatureFlags().is_enabled("weight_v2_active") else "v1"
        except Exception:
            _weight_version = "v1"
        
        conn = _conn()
        try:
            conn.execute("""
                INSERT OR IGNORE INTO signal_snapshots
                (decision_id, symbol, direction, strength, confidence,
                 consensus_level, strategy_id, created_at, trade_date,
                 created_at_snap, trade_mode, weight_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                decision_id, symbol, direction, strength, confidence,
                consensus_level, strategy_id, created_at, trade_date,
                now_snap, _trade_mode, _weight_version,
            ))
            conn.commit()
        finally:
            conn.close()
            
    except Exception as e:
        # 非关键路径: 静默记录, 不抛异常
        logger.debug(f"[SignalIC] 记录快照失败 (不影响主流程): {e}")


# ═══════════════════════════════════════════════════════════════════
#  盘后回填
# ═══════════════════════════════════════════════════════════════════

def get_pending_snapshots(trade_date_before: str, limit: int = 500) -> List[Dict]:
    """
    获取待回填的信号快照
    
    Args:
        trade_date_before: 只返回此日期之前的信号 (确保有足够的交易日回填)
        limit: 最大返回数
    
    Returns:
        List of snapshot dicts with realized < 3
    """
    _ensure_table()
    conn = _conn()
    try:
        rows = conn.execute("""
            SELECT * FROM signal_snapshots
            WHERE realized < 3
              AND trade_date < ?
            ORDER BY trade_date DESC
            LIMIT ?
        """, (trade_date_before, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_forward_returns(
    decision_id: str,
    close_t0: Optional[float] = None,
    close_t1: Optional[float] = None,
    close_t5: Optional[float] = None,
    close_t20: Optional[float] = None,
    price_at_signal: Optional[float] = None,
):
    """
    回填前瞻收益
    
    收益率计算 (方向调整):
      LONG:  fwd_ret = (close_tN / close_t0) - 1
      SHORT: fwd_ret = (close_t0 / close_tN) - 1
    
    realized 状态机:
      0 → 1: T1 已回填
      1 → 2: T5 已回填
      2 → 3: T20 已回填 (全部完成)
    """
    _ensure_table()
    conn = _conn()
    try:
        # 读取当前状态
        row = conn.execute(
            "SELECT direction, realized, close_t0 FROM signal_snapshots WHERE decision_id = ?",
            (decision_id,)
        ).fetchone()
        if not row:
            logger.warning(f"[SignalIC] 回填失败: {decision_id} 不存在")
            return
        
        direction = row["direction"]
        current_realized = row["realized"]
        existing_t0 = row["close_t0"]
        
        # 使用传入的 close_t0 或已有的
        t0 = close_t0 or existing_t0
        
        # 计算方向调整后的前瞻收益
        def _calc_fwd(close_tn: Optional[float]) -> Optional[float]:
            if close_tn is None or t0 is None or t0 == 0:
                return None
            if direction == "SHORT":
                return (t0 / close_tn) - 1.0
            else:
                return (close_tn / t0) - 1.0
        
        fwd_t1 = _calc_fwd(close_t1)
        fwd_t5 = _calc_fwd(close_t5)
        fwd_t20 = _calc_fwd(close_t20)
        
        # 更新 realized 状态
        new_realized = current_realized
        if close_t1 is not None and new_realized < 1:
            new_realized = 1
        if close_t5 is not None and new_realized < 2:
            new_realized = 2
        if close_t20 is not None and new_realized < 3:
            new_realized = 3
        
        now = datetime.now().isoformat()
        
        # 构建 UPDATE 语句 (只更新非 None 字段)
        updates = []
        params = []
        
        if price_at_signal is not None:
            updates.append("price_at_signal = ?")
            params.append(price_at_signal)
        if close_t0 is not None:
            updates.append("close_t0 = ?")
            params.append(close_t0)
        if close_t1 is not None:
            updates.append("close_t1 = ?")
            params.append(close_t1)
        if fwd_t1 is not None:
            updates.append("fwd_ret_t1 = ?")
            params.append(fwd_t1)
        if close_t5 is not None:
            updates.append("close_t5 = ?")
            params.append(close_t5)
        if fwd_t5 is not None:
            updates.append("fwd_ret_t5 = ?")
            params.append(fwd_t5)
        if close_t20 is not None:
            updates.append("close_t20 = ?")
            params.append(close_t20)
        if fwd_t20 is not None:
            updates.append("fwd_ret_t20 = ?")
            params.append(fwd_t20)
        
        updates.append("realized = ?")
        params.append(new_realized)
        updates.append("ic_batch_date = ?")
        params.append(now)
        
        params.append(decision_id)
        
        sql = f"UPDATE signal_snapshots SET {', '.join(updates)} WHERE decision_id = ?"
        conn.execute(sql, params)
        conn.commit()
        
        logger.debug(
            f"[SignalIC] 回填 {decision_id}: "
            f"t1={fwd_t1}, t5={fwd_t5}, t20={fwd_t20}, realized={new_realized}"
        )
    except Exception as e:
        logger.error(f"[SignalIC] 回填失败 {decision_id}: {e}")
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════
#  IC 计算 (Phase 2, 此处预留接口)
# ═══════════════════════════════════════════════════════════════════

def get_ic_summary(
    min_signals: int = 20,
    forward_col: str = "fwd_ret_t1",
    days_back: int = 30,
    weight_version: Optional[str] = None,
) -> Dict[str, Any]:
    """
    获取 Signal IC 摘要 — 供盘后审计报告使用
    
    Args:
        min_signals: 最少信号数 (不足则返回 None)
        forward_col: 使用哪个前瞻收益列
        days_back: 回溯天数
    
    Returns:
        {
            'ic_mean': float,      # IC 均值
            'ic_positive_rate': float,  # IC > 0 的天数占比
            'n_signals': int,      # 总信号数
            'n_days': int,         # 覆盖天数
            'by_strategy': dict,   # 按策略分组 IC
            'by_direction': dict,  # 按方向分组 IC
            'status': 'ok' | 'insufficient_data',
        }
    """
    _ensure_table()
    
    try:
        from scipy.stats import spearmanr
    except ImportError:
        return {"status": "error", "message": "scipy not installed"}
    
    conn = _conn()
    try:
        # 获取已回填的信号
        # 2026-06-16 修复: days_back 参数原来被声明但从未注入 WHERE 子句
        from datetime import datetime as _dt, timedelta as _td
        _cutoff = (_dt.now() - _td(days=days_back)).strftime("%Y-%m-%d")
        # Task #4: 支持按 weight_version 过滤
        _wv_clause = ""
        if weight_version:
            _wv_clause = f" AND COALESCE(weight_version, 'v1') = '{weight_version}'"
        rows = conn.execute(f"""
            SELECT strength, {forward_col}, strategy_id, direction, trade_date
            FROM signal_snapshots
            WHERE {forward_col} IS NOT NULL
              AND strength IS NOT NULL
              AND trade_date >= '{_cutoff}'
              {_wv_clause}
            ORDER BY trade_date DESC
        """).fetchall()
        
        if len(rows) < min_signals:
            return {
                "status": "insufficient_data",
                "n_signals": len(rows),
                "min_required": min_signals,
            }
        
        # Task #1 (2026-06-17): 数据质量校验 — 过滤 NaN/inf/NULL
        import math
        _clean_rows = [
            r for r in rows
            if r["strength"] is not None
            and r[forward_col] is not None
            and not math.isnan(float(r["strength"]))
            and not math.isnan(float(r[forward_col]))
            and not math.isinf(float(r["strength"]))
            and not math.isinf(float(r[forward_col]))
        ]
        
        # 清洗后重新检查样本量
        if len(_clean_rows) < min_signals:
            return {
                "status": "insufficient_data",
                "n_signals": len(_clean_rows),
                "min_required": min_signals,
                "note": f"cleaned from {len(rows)} to {len(_clean_rows)} after NaN/inf filter",
            }
        
        rows = _clean_rows

        # 全局 IC
        strengths = [r["strength"] for r in rows]
        fwd_rets = [r[forward_col] for r in rows]
        ic, pval = spearmanr(strengths, fwd_rets)
        
        # 按日计算 IC (用于 IC positive rate)
        from collections import defaultdict
        daily_ic = {}
        by_date = defaultdict(list)
        for r in rows:
            by_date[r["trade_date"]].append(r)
        
        for date, day_rows in by_date.items():
            if len(day_rows) < 3:
                continue
            s = [r["strength"] for r in day_rows]
            f = [r[forward_col] for r in day_rows]
            try:
                d_ic, _ = spearmanr(s, f)
                daily_ic[date] = d_ic
            except Exception:
                pass
        
        ic_positive_rate = (
            sum(1 for v in daily_ic.values() if v > 0) / len(daily_ic)
            if daily_ic else 0.0
        )
        
        # 按策略分组 IC
        by_strategy = {}
        strat_groups = defaultdict(list)
        for r in rows:
            sid = r["strategy_id"] or "unknown"
            strat_groups[sid].append(r)
        for sid, group in strat_groups.items():
            if len(group) < 5:
                continue
            s = [r["strength"] for r in group]
            f = [r[forward_col] for r in group]
            try:
                g_ic, g_pval = spearmanr(s, f)
                import math
                by_strategy[sid] = {
                    "ic": round(g_ic, 4) if not math.isnan(g_ic) else 0.0,
                    "pval": round(g_pval, 4) if not math.isnan(g_pval) else 1.0,
                    "n": len(group),
                }
            except Exception:
                pass
        
        # 按方向分组 IC
        by_direction = {}
        dir_groups = defaultdict(list)
        for r in rows:
            dir_groups[r["direction"]].append(r)
        for d, group in dir_groups.items():
            if len(group) < 5:
                continue
            s = [r["strength"] for r in group]
            f = [r[forward_col] for r in group]
            try:
                g_ic, _ = spearmanr(s, f)
                import math
                by_direction[d] = {"ic": round(g_ic, 4) if not math.isnan(g_ic) else 0.0, "n": len(group)}
            except Exception:
                pass
        
        return {
            "status": "ok",
            "ic_mean": round(float(ic), 4) if ic == ic else None,
            "ic_p_value": round(float(pval), 4) if pval == pval else None,
            "ic_positive_rate": round(ic_positive_rate, 3),
            "n_signals": len(rows),
            "n_days": len(by_date),
            "by_strategy": by_strategy,
            "by_direction": by_direction,
            "forward_col": forward_col,
        }
    except Exception as e:
        logger.error(f"[SignalIC] IC 计算失败: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def get_stats() -> Dict[str, Any]:
    """获取快照统计信息"""
    _ensure_table()
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM signal_snapshots").fetchone()[0]
        realized = conn.execute(
            "SELECT COUNT(*) FROM signal_snapshots WHERE realized >= 1"
        ).fetchone()[0]
        fully_realized = conn.execute(
            "SELECT COUNT(*) FROM signal_snapshots WHERE realized = 3"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM signal_snapshots WHERE realized = 0"
        ).fetchone()[0]
        
        return {
            "total_snapshots": total,
            "with_t1": realized,
            "fully_realized": fully_realized,
            "pending": pending,
        }
    finally:
        conn.close()
