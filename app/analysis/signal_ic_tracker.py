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
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
import math


def _rank_values(values: List[float]) -> List[float]:
    """Pure-Python rank (average rank for ties, 1-based)."""
    indexed = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman_fallback(x: List[float], y: List[float]) -> tuple:
    """Pure-Python Spearman without scipy (no p-value).
    Uses Pearson correlation on ranks (tie-corrected, matches scipy)."""
    if not x or len(x) != len(y) or len(x) < 2:
        return None, None
    xr = _rank_values(x)
    yr = _rank_values(y)
    n = len(xr)
    mx = sum(xr) / n
    my = sum(yr) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xr, yr))
    vx = sum((a - mx) ** 2 for a in xr)
    vy = sum((b - my) ** 2 for b in yr)
    if vx == 0 or vy == 0:
        return None, None
    rho = cov / math.sqrt(vx * vy)
    return float(rho), None

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


# 2026-06-30: Strategy Registry 替换硬编码 mapping
# 从 app.core.strategy_registry 读取 disabled 策略列表，不再手动维护 cfg_to_sid 映射

def _get_disabled_strategy_ids() -> List[str]:
    """从 strategy_registry 获取所有 disabled 策略的 strategy_id。
    
    自动适配：新增/禁用策略只需改 unified_config.yaml 的 strategy_registry 段。
    """
    try:
        from app.core.strategy_registry import get_disabled_strategy_ids as _get
        return _get()
    except Exception as e:
        logger.warning(f"[SignalIC] strategy_registry 不可用, 返回空列表: {e}")
        return []


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
            # 2026-06-30 Phase 1: 因子级IC基础设施 — 记录信号生成时命中的因子
            try:
                conn.execute("ALTER TABLE signal_snapshots ADD COLUMN factors_json TEXT")
                logger.info("[SignalIC] 已添加 factors_json 列")
            except Exception:
                pass  # 列已存在
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_factors ON signal_snapshots(factors_json)")
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
    factors: Optional[list] = None,  # 2026-06-30 Phase 1: 命中的因子列表 ["insider_cluster_buy", "above_ma20", ...]
    market_context: Optional[dict] = None,  # P1 2026-07-11: 决策时数值特征快照 (RSI/MACD/ATR/因子详情) — 供 per-feature edge 测量
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
        factors: 信号生成时命中的因子列表 (用于因子级IC计算)
    """
    try:
        _ensure_table()
        
        # P0 (2026-06-29): 防再生 — strategy_id 为空时标记 "unknown"
        if not strategy_id:
            strategy_id = "unknown"
        
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
            _factors_json = json.dumps(factors) if factors else None
            # P1 (2026-07-11): 持久化决策时特征向量 (default=str 兜底不可序列化对象)
            _mc_json = json.dumps(market_context, default=str) if market_context else None
            # UPSERT: 若 decision_recorder 先写入(无factors), 后续 pipeline 带 factors 时更新
            conn.execute("""
                INSERT INTO signal_snapshots
                (decision_id, symbol, direction, strength, confidence,
                 consensus_level, strategy_id, created_at, trade_date,
                 created_at_snap, trade_mode, weight_version, factors_json,
                 market_context_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    factors_json = COALESCE(excluded.factors_json, signal_snapshots.factors_json),
                    market_context_json = COALESCE(excluded.market_context_json, signal_snapshots.market_context_json)
            """, (
                decision_id, symbol, direction, strength, confidence,
                consensus_level, strategy_id, created_at, trade_date,
                now_snap, _trade_mode, _weight_version, _factors_json,
                _mc_json,
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
            'ic_mean': float,      # pooled IC 均值 (仅诊断参考, 显著性夸大)
            'day_clustered': dict, # 头条采信口径 (宪章第9律): 按日聚类
                                   #   {n_days, day_ic_mean, t, mde, distinguishable}
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
        spearmanr = None
    
    conn = _conn()
    try:
        # 获取已回填的信号
        # 2026-06-16 修复: days_back 参数原来被声明但从未注入 WHERE 子句
        # 2026-06-30 修复: 排除已disabled策略的信号，防止死策略污染IC
        from datetime import timedelta as _td
        # 读侧日期串改按 ET 取 (2026-09-05 M-6/B1 第四阶): 下方 WHERE 过滤 signal_snapshots.trade_date,
        #   该列 DDL 自注 "(YYYY-MM-DD, ET)" (:144), 写方是 record_signal_snapshot 的 get_trade_date()
        #   (:247, = now_et() 的日历日)。原按宿主 CST 取"今天"再减 days_back ⇒ 在 ET 12:00-23:59
        #   (北京次日 00:00-11:59) cutoff 推后 1 天, IC 样本窗**静默少一天**。该列另有
        #   `ORDER BY trade_date DESC` (:320/:506) 与字符串比较, 跨尺即错序; 但本处只算读侧字符串、
        #   **不写库** ⇒ 零混存风险 (不同于 DEB §11.5 的账本列, 无需归一方案)。
        from app.utils.timezone import now_et as _now_et_ic
        _cutoff = (_now_et_ic() - _td(days=days_back)).strftime("%Y-%m-%d")
        # Task #4: 支持按 weight_version 过滤
        _wv_clause = ""
        if weight_version:
            _wv_clause = f" AND COALESCE(weight_version, 'v1') = '{weight_version}'"
        
        # 2026-06-30: 从config读取disabled策略，排除其历史信号
        _disabled_strats = _get_disabled_strategy_ids()
        _excl_clause = ""
        if _disabled_strats:
            _placeholders = ",".join(f"'{s}'" for s in _disabled_strats)
            _excl_clause = f" AND strategy_id NOT IN ({_placeholders})"
        
        rows = conn.execute(f"""
            SELECT strength, {forward_col}, strategy_id, direction, trade_date, symbol
            FROM signal_snapshots
            WHERE {forward_col} IS NOT NULL
              AND strength IS NOT NULL
              AND trade_date >= '{_cutoff}'
              {_wv_clause}
              {_excl_clause}
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
        # 2026-07-19 修复 sign double-adjustment: fwd_ret 回填时已按方向调整
        #   (_calc_fwd: SHORT 用 t0/close_tn-1), 此处不可再对 SHORT strength 取负,
        #   否则双重调整使 SHORT 信号 IC 系统性反号. LONG/SHORT 均用 raw strength.
        signed_strengths = []
        fwd_rets_list = []
        for r in rows:
            s = r["strength"]
            d = r["direction"]
            fr = r[forward_col]
            if d == "NEUTRAL":
                continue
            signed_strengths.append(float(s))
            fwd_rets_list.append(float(fr))
        
        if spearmanr is not None:
            ic, pval = spearmanr(signed_strengths, fwd_rets_list)
        else:
            ic, pval = _spearman_fallback(signed_strengths, fwd_rets_list)

        # 2026-08-09 (独立样本修正): 同一天同一标的的多个信号共享同一 fwd_ret
        #   (T+1 是同一个收盘价) → 信号数≠独立样本数, n 虚高. 按 (trade_date, symbol,
        #   direction) 聚合: strength 取组内均值, fwd_ret 取组内唯一值, 再算秩相关.
        #   同时报告 n_independent (独立 日期×标的×方向 对数) 供周报对照信号数.
        indep_map: dict = {}
        for r in rows:
            if r["direction"] == "NEUTRAL":
                continue
            _k = (r["trade_date"], r["symbol"], r["direction"])
            indep_map.setdefault(_k, []).append((float(r["strength"]), float(r[forward_col])))
        ic_independent = None
        n_independent = len(indep_map)
        if n_independent >= 5:
            i_s = [sum(x[0] for x in v) / len(v) for v in indep_map.values()]
            i_f = [v[0][1] for v in indep_map.values()]  # fwd_ret 组内同值, 取首
            if spearmanr is not None:
                _ic_ind, _ = spearmanr(i_s, i_f)
            else:
                _ic_ind, _ = _spearman_fallback(i_s, i_f)
            if _ic_ind is not None and _ic_ind == _ic_ind:
                ic_independent = round(float(_ic_ind), 4)
        
        # 按日计算 IC (用于 IC positive rate)
        from collections import defaultdict
        daily_ic = {}
        by_date = defaultdict(list)
        for r in rows:
            by_date[r["trade_date"]].append(r)
        
        for date, day_rows in by_date.items():
            if len(day_rows) < 3:
                continue
            # 2026-07-19 修复 sign double-adjustment: fwd_ret 已方向调整, strength 用 raw
            d_signed = []
            d_rets = []
            for r in day_rows:
                if r["direction"] == "NEUTRAL":
                    continue
                d_signed.append(float(r["strength"]))
                d_rets.append(float(r[forward_col]))
            if len(d_signed) < 3:
                continue
            try:
                if spearmanr is not None:
                    d_ic, _ = spearmanr(d_signed, d_rets)
                else:
                    d_ic, _ = _spearman_fallback(d_signed, d_rets)
                daily_ic[date] = d_ic
            except Exception:
                pass
        
        ic_positive_rate = (
            sum(1 for v in daily_ic.values() if v is not None and v > 0) / len(daily_ic)
            if daily_ic else 0.0
        )

        # 审计 (2026-08-24, 全仓过时/冲突清理): 日聚类成对输出 (宪章第 9 律,
        # 与 playbook ic_tracker._day_clustered_ic 同口径). 头条采信口径=日聚类:
        # 同日聚集信号共享相关结构, pooled Spearman 显著性系统性夸大
        # (DEB-20260823 实锤: pooled 夸大 ~4 倍). pooled ic_mean 仅诊断参考.
        day_clustered = None
        _day_vals = [v for v in daily_ic.values() if v is not None and v == v]
        if len(_day_vals) >= 3:
            import math as _math
            _n = len(_day_vals)
            _mean = sum(_day_vals) / _n
            _sd = _math.sqrt(sum((v - _mean) ** 2 for v in _day_vals) / (_n - 1))
            _t = (_mean / (_sd / _math.sqrt(_n))) if _sd > 0 else 0.0
            _mde = 2.8 * _sd / _math.sqrt(_n)
            day_clustered = {
                "n_days": _n,
                "day_ic_mean": round(_mean, 4),
                "t": round(_t, 3),
                "mde": round(_mde, 4),
                "distinguishable": abs(_mean) > _mde and abs(_t) >= 2.0,
            }
        
        # 按策略分组 IC
        by_strategy = {}
        strat_groups = defaultdict(list)
        for r in rows:
            sid = r["strategy_id"] or "unknown"
            strat_groups[sid].append(r)
        for sid, group in strat_groups.items():
            if len(group) < 5:
                continue
            # 2026-07-19 修复 sign double-adjustment: fwd_ret 已方向调整, strength 用 raw
            g_signed = []
            g_rets = []
            for r in group:
                if r["direction"] == "NEUTRAL":
                    continue
                g_signed.append(float(r["strength"]))
                g_rets.append(float(r[forward_col]))
            if len(g_signed) < 5:
                continue
            try:
                if spearmanr is not None:
                    g_ic, g_pval = spearmanr(g_signed, g_rets)
                else:
                    g_ic, g_pval = _spearman_fallback(g_signed, g_rets)
                import math
                by_strategy[sid] = {
                    "ic": round(g_ic, 4) if g_ic is not None and not math.isnan(g_ic) else 0.0,
                    "pval": round(g_pval, 4) if g_pval is not None and not math.isnan(g_pval) else 1.0,
                    "n": len(g_signed),
                }
            except Exception:
                pass
        
        # 按方向分组 IC
        by_direction = {}
        dir_groups = defaultdict(list)
        for r in rows:
            dir_groups[r["direction"]].append(r)
        for d, group in dir_groups.items():
            if d == "NEUTRAL":
                # 2026-08-09: NEUTRAL = "不交易" 信号, fwd_ret 是市场自然漂移,
                #   strength 与它无因果 → IC 无意义, 排除出方向IC (与全局/按策略口径一致).
                continue
            if len(group) < 5:
                continue
            s = [r["strength"] for r in group]
            f = [r[forward_col] for r in group]
            try:
                if spearmanr is not None:
                    g_ic, _ = spearmanr(s, f)
                else:
                    g_ic, _ = _spearman_fallback(s, f)
                import math
                by_direction[d] = {"ic": round(g_ic, 4) if g_ic is not None and not math.isnan(g_ic) else 0.0, "n": len(group)}
            except Exception:
                pass
        
        return {
            "status": "ok",
            "ic_mean": round(float(ic), 4) if ic is not None and ic == ic else None,
            "ic_p_value": round(float(pval), 4) if pval is not None and pval == pval else None,
            "ic_independent": ic_independent,   # 2026-08-09: 按(日期×标的×方向)聚合IC
            "n_independent": n_independent,     # 独立 (日期×标的×方向) 对数
            # 2026-08-24: 日聚类成对输出 (宪章第 9 律) — 头条采信口径,
            # ic_mean(pooled) 仅诊断参考. 详见 day_clustered 块.
            "day_clustered": day_clustered,
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


# ═══════════════════════════════════════════════════════════════════
#  App 级 backfill 入口 (P2 2026-06-29: 从 scripts/ 提到 app/analysis/ 层)
# ═══════════════════════════════════════════════════════════════════

def run_signal_ic_backfill(dry_run: bool = False) -> Dict[str, int]:
    """App 级 Signal IC 前瞻收益回填入口.

    供 run_playbook_track 统一调度, 替代 scripts/backfill_forward_returns.py 的 cron 独立触发.
    逻辑与 scripts 版本一致: 拉 pending → 按symbol分组 → 获取日K → 回填 T1/T5/T20.

    Args:
        dry_run: 试运行 (不写DB)

    Returns:
        {symbol: count_filled}
    """
    from datetime import datetime as _dt, timedelta as _td
    import logging as _logging
    _bl = _logging.getLogger("ftseek_us.signal_ic_backfill")

    # 读侧日期串改按 ET 取 (2026-09-05 M-6/B1 第四阶): today_str 喂给 get_pending_snapshots 的
    #   `trade_date < ?` (:319), 而 signal_snapshots.trade_date 的写方就是 get_trade_date() ⇒ 这里
    #   直接调**同一个函数**, 同尺由构造保证, 不靠人工记着两边一致。原按宿主 CST 取"今天", 在 ET
    #   12:00-23:59 (北京次日) 会把 today_str 推成 ET 的明天 ⇒ 当天 ET 的信号被提前当成"已有足够
    #   交易日可回填"而进入回填队列 (forward return 尚不可得 → 灌进未成熟样本)。
    #   注: 本函数下方 :833 的 `_dt.strptime` (±1 天容差匹配) 仍在用 ⇒ `_dt` 导入保留不动。
    from app.utils.timezone import get_trade_date
    today_str = get_trade_date()
    pending = get_pending_snapshots(trade_date_before=today_str, limit=5000)

    if not pending:
        _bl.info("[SignalIC-Backfill] 无待回填快照")
        return {}

    _bl.info(f"[SignalIC-Backfill] 待回填: {len(pending)} 条")

    # 按 symbol 分组
    by_symbol: Dict[str, List[Dict]] = {}
    for snap in pending:
        sym = snap["symbol"]
        by_symbol.setdefault(sym, []).append(snap)

    _bl.info(f"[SignalIC-Backfill] 涉及 {len(by_symbol)} 只股票")

    # 获取 router (复用 scripts 的 _get_data_router 逻辑)
    try:
        # 2026-07-11: 复用进程级共享单例, 避免每次 backfill 新建 Futu 连接+watchdog
        from app.data.market_data_router import get_market_data_router
        router = get_market_data_router()
    except Exception as e:
        _bl.error(f"[SignalIC-Backfill] Router 初始化失败: {e}")
        return {}

    stats: Dict[str, int] = {}

    for symbol, snaps in by_symbol.items():
        # 获取日K (复用 router 的 _daily_bars_cache)
        bars = router.get_daily_bars(symbol, count=50)
        if not bars:
            _bl.warning(f"[SignalIC-Backfill] {symbol}: 无法获取日K, 跳过")
            continue

        # 构建 date→close 映射
        date_to_close: Dict[str, float] = {}
        sorted_dates = []
        for bar in bars:
            t = str(bar.get("time", ""))[:10]
            c = bar.get("close")
            if t and c:
                try:
                    date_to_close[t] = float(c)
                    sorted_dates.append(t)
                except (TypeError, ValueError):
                    continue
        sorted_dates.sort()
        date_to_idx = {d: i for i, d in enumerate(sorted_dates)}

        filled = 0
        for snap in snaps:
            trade_date = snap["trade_date"]
            # 允许 ±1 天容差 (时区差异)
            snap_idx = None
            trade_date_actual = None
            for offset in [0, 1, -1]:
                check_date = (_dt.strptime(trade_date, "%Y-%m-%d") + _td(days=offset)).strftime("%Y-%m-%d")
                if check_date in date_to_idx:
                    snap_idx = date_to_idx[check_date]
                    trade_date_actual = check_date
                    break

            if snap_idx is None or trade_date_actual is None:
                continue

            close_t0 = date_to_close.get(trade_date_actual)
            close_t1 = date_to_close.get(sorted_dates[snap_idx + 1]) if snap_idx + 1 < len(sorted_dates) else None
            close_t5 = date_to_close.get(sorted_dates[snap_idx + 5]) if snap_idx + 5 < len(sorted_dates) else None
            close_t20 = date_to_close.get(sorted_dates[snap_idx + 20]) if snap_idx + 20 < len(sorted_dates) else None

            if not dry_run:
                update_forward_returns(
                    decision_id=snap["decision_id"],
                    close_t0=close_t0,
                    close_t1=close_t1,
                    close_t5=close_t5,
                    close_t20=close_t20,
                )
            filled += 1

        stats[symbol] = filled
        _bl.info(f"[SignalIC-Backfill] {symbol}: {filled}/{len(snaps)} 条")

    total = sum(stats.values())
    _bl.info(f"[SignalIC-Backfill] 完成: {total} 条回填, {len(stats)} 只股票")
    return stats
