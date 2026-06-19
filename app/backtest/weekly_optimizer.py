"""
WeeklyParameterOptimizer — 周频参数优化流程
================================================

任务: Task #16 建立回测驱动参数优化流程 (2026-05-27)

设计目标
--------
1. **数据源**: 直接从 DB (trade_records.db / decisions.db) 读取近期实盘 + 模拟盘成交,
   按 trade_date 分页过滤, 避免一次性加载所有历史。
2. **训练/验证切分**: 近 30 天作为训练集 (IS), 近 7 天作为验证集 (OOS)。
3. **优化目标**: Sharpe Ratio + 最大回撤 (MaxDD) 约束。
4. **优化空间**:
       min_signal_strength      ∈ [30, 80]
       stop_loss_pct            ∈ [-0.40, -0.15]
       take_profit_pct          ∈ [0.03, 0.10]
       trailing_stop_trigger    ∈ [0.02, 0.05]
5. **输出**: JSON + Markdown 双格式, 落到 data/optimization_reports/。
6. **过拟合检测**: 若 OOS Sharpe 比 IS Sharpe 衰减 >30%, 标记 overfit_warning。

执行入口
--------
- 命令行:  python -m app.backtest.weekly_optimizer
- 函数式:  run_weekly_optimization(notifier=...)

注意
----
- 不直接修改实盘配置, 仅产出建议报告供人工审核。
- 复用 walk_forward._compute_stats 的统计方法。
- 当样本不足时 (训练集 < 20 笔), 直接 fail-safe 返回, 不生成误导性建议。
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import sqlite3
import statistics
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from itertools import product
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 路径与默认参数
# ─────────────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_REPORT_DIR = _DATA_DIR / "optimization_reports"
_REPORT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TRADE_DB_CANDIDATES = [
    _DATA_DIR / "trade_records.db",
    _DATA_DIR / "ftseek_us.db",
]

# 当前实盘默认值 (与 unified_config.yaml 对齐, 仅用于对比展示)
LIVE_CURRENT_PARAMS: Dict[str, float] = {
    "min_signal_strength": 50.0,
    "stop_loss_pct": -0.045,
    "take_profit_pct": 0.025,
    "trailing_stop_trigger": 0.01,
}

# 优化网格 (粗采样, 兼顾速度与覆盖)
PARAM_GRID: Dict[str, List[float]] = {
    "min_signal_strength":   [30, 40, 50, 60, 70, 80],
    "stop_loss_pct":         [-0.40, -0.30, -0.25, -0.20, -0.15],
    "take_profit_pct":       [0.03, 0.05, 0.07, 0.10],
    "trailing_stop_trigger": [0.02, 0.03, 0.04, 0.05],
}

PARAM_BOUNDS: Dict[str, Tuple[float, float]] = {
    "min_signal_strength":   (30.0, 80.0),
    "stop_loss_pct":         (-0.40, -0.15),
    "take_profit_pct":       (0.03, 0.10),
    "trailing_stop_trigger": (0.02, 0.05),
}

MIN_TRAIN_SAMPLES = 20
MIN_OOS_SAMPLES = 5
OVERFIT_DEGRADATION_THRESHOLD = 0.30  # OOS Sharpe < (1-0.3) * IS Sharpe 即过拟合
MAX_DD_HARD_LIMIT = 0.30              # 候选解 MaxDD 超过此值直接淘汰


# ─────────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeRow:
    """归一化的成交行 (跨 schema 兼容)"""
    trade_date: str
    symbol: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    realized_pnl: float
    signal_strength: float
    max_drawdown_during_trade: float = 0.0   # 持仓期最大回撤 (绝对值, 可为 0)
    holding_hours: float = 0.0


@dataclass
class ParamCandidate:
    """单组参数候选"""
    params: Dict[str, float]
    is_stats: Dict[str, float] = field(default_factory=dict)
    oos_stats: Dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    rejected: bool = False
    reject_reason: str = ""


@dataclass
class OptimizationReport:
    """优化总报告"""
    generated_at: str
    train_window: Tuple[str, str]
    valid_window: Tuple[str, str]
    train_size: int
    valid_size: int
    best_params: Dict[str, float]
    best_score: float
    best_is_stats: Dict[str, float]
    best_oos_stats: Dict[str, float]
    live_params: Dict[str, float]
    overfit_warning: bool
    degradation_ratio: float
    sensitivity: Dict[str, List[Dict[str, float]]]
    bootstrap_ci: Dict[str, Dict[str, float]]
    safe_zones: Dict[str, Tuple[float, float]]
    top_candidates: List[Dict[str, Any]]
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # tuple → list (JSON 友好)
        d["train_window"] = list(self.train_window)
        d["valid_window"] = list(self.valid_window)
        d["safe_zones"] = {k: list(v) for k, v in self.safe_zones.items()}
        return d


# ─────────────────────────────────────────────────────────────────────────────
# DB 读取层
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_trade_db() -> Optional[Path]:
    for p in DEFAULT_TRADE_DB_CANDIDATES:
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cur.fetchone() is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> List[str]:
    try:
        cur = conn.execute(f"PRAGMA table_info({name})")
        return [r[1] for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def load_trade_rows(
    db_path: Optional[Path] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page_size: int = 2000,
) -> List[TradeRow]:
    """
    从 trade_records.db 中读取已平仓成交, 兼容 trade_recorder / unified_trade_store 两套 schema。

    返回归一化 TradeRow 列表, 以日期升序排列。
    """
    db_path = db_path or _resolve_trade_db()
    if db_path is None:
        logger.warning("[WeeklyOptimizer] 未找到可用 trade_records DB")
        return []

    rows: List[TradeRow] = []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if not _has_table(conn, "trade_records"):
            logger.warning(f"[WeeklyOptimizer] {db_path} 中无 trade_records 表")
            return []

        cols = set(_table_columns(conn, "trade_records"))

        # 选择性字段, 避免缺列报错
        select_cols = []
        def pick(name: str, default: str = "NULL") -> str:
            if name in cols:
                select_cols.append(name)
                return name
            select_cols.append(f"{default} AS {name}")
            return f"{default} AS {name}"

        # 触发收集 (会写入 select_cols)
        for c in (
            "trade_date", "symbol", "entry_price", "exit_price",
            "pnl_pct", "realized_pnl", "signal_strength",
            "max_drawdown_during_trade", "holding_hours",
        ):
            pick(c)

        where = ["exit_price > 0"]
        params: List[Any] = []
        if start_date:
            where.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            where.append("trade_date <= ?")
            params.append(end_date)
        where_clause = " AND ".join(where)

        offset = 0
        while True:
            sql = (
                f"SELECT {', '.join(select_cols)} FROM trade_records "
                f"WHERE {where_clause} ORDER BY trade_date ASC "
                f"LIMIT ? OFFSET ?"
            )
            cur = conn.execute(sql, (*params, page_size, offset))
            batch = cur.fetchall()
            if not batch:
                break
            for r in batch:
                try:
                    rows.append(_row_to_trade(dict(r)))
                except Exception as e:
                    logger.debug(f"[WeeklyOptimizer] 跳过异常行: {e}")
            if len(batch) < page_size:
                break
            offset += page_size
    finally:
        conn.close()

    logger.info(
        f"[WeeklyOptimizer] DB={db_path.name} 读取 {len(rows)} 笔已平仓成交 "
        f"(start={start_date}, end={end_date})"
    )
    return rows


def _row_to_trade(d: Dict[str, Any]) -> TradeRow:
    entry = float(d.get("entry_price") or 0)
    exit_ = float(d.get("exit_price") or 0)
    pnl_pct = d.get("pnl_pct")
    if pnl_pct is None:
        pnl_pct = (exit_ - entry) / entry if entry > 0 else 0.0
    pnl_pct = float(pnl_pct)
    # 兼容百分比形式 (5.2 表示 5.2%)
    if abs(pnl_pct) > 1.5:  # 几乎不可能 > 150% 的小数比, 视为百分数
        pnl_pct = pnl_pct / 100.0

    mdd = d.get("max_drawdown_during_trade") or 0.0
    mdd = abs(float(mdd))
    if mdd > 1.5:
        mdd = mdd / 100.0

    return TradeRow(
        trade_date=str(d.get("trade_date") or "")[:10],
        symbol=str(d.get("symbol") or ""),
        entry_price=entry,
        exit_price=exit_,
        pnl_pct=pnl_pct,
        realized_pnl=float(d.get("realized_pnl") or 0.0),
        signal_strength=float(d.get("signal_strength") or 0.0),
        max_drawdown_during_trade=mdd,
        holding_hours=float(d.get("holding_hours") or 0.0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 回测 Replay 层
# ─────────────────────────────────────────────────────────────────────────────

def _replay_trade(row: TradeRow, params: Dict[str, float]) -> Optional[float]:
    """
    用给定参数对单笔历史成交做"假设性"重放, 返回该笔在新参数下的 pnl_pct。
    返回 None 表示该笔被参数拒绝 (e.g. signal_strength 不达标)。

    Replay 规则 (由实盘字段近似估算, 仅供建议):
      1. signal_strength < min_signal_strength → 拒绝
      2. 历史持仓最大回撤 ≥ |stop_loss_pct| → 在 stop_loss_pct 处截断 (止损先到)
      3. 否则, 若实际 pnl_pct ≥ take_profit_pct → 在 take_profit_pct 处止盈
      4. 否则, 若实际 pnl_pct ≥ trailing_stop_trigger → 锁定 max(0.5*pnl_pct, trailing_stop_trigger)
      5. 否则原样使用 realized pnl_pct
    """
    if row.signal_strength < params["min_signal_strength"]:
        return None

    sl = params["stop_loss_pct"]    # 负数, e.g. -0.20
    tp = params["take_profit_pct"]  # 正数, e.g. 0.05
    trig = params["trailing_stop_trigger"]

    # 1) 止损先触发
    if row.max_drawdown_during_trade >= abs(sl):
        return sl

    actual = row.pnl_pct
    # 2) 止盈先触发
    if actual >= tp:
        return tp
    # 3) 追踪止盈触发 — 用 trail 锁定一半利润
    if actual >= trig:
        return max(actual * 0.5, trig)
    # 4) 维持原 pnl
    return actual


def replay_pnl_series(rows: List[TradeRow], params: Dict[str, float]) -> List[float]:
    """对一组历史 trade 进行参数重放, 返回非拒绝笔的 pnl_pct 序列"""
    out: List[float] = []
    for r in rows:
        v = _replay_trade(r, params)
        if v is not None:
            out.append(v)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 统计与评分
# ─────────────────────────────────────────────────────────────────────────────

def _stats(pnls: List[float]) -> Dict[str, float]:
    """计算 sharpe / win_rate / total_return / max_drawdown"""
    n = len(pnls)
    if n == 0:
        return {
            "n_trades": 0, "win_rate": 0.0, "sharpe": 0.0,
            "total_return": 0.0, "max_drawdown": 0.0, "avg_pnl": 0.0,
        }
    wins = sum(1 for p in pnls if p > 0)
    total_return = sum(pnls)
    avg = total_return / n
    if n > 1:
        try:
            std = statistics.stdev(pnls)
        except statistics.StatisticsError:
            std = 0.0
    else:
        std = 0.0
    sharpe = (avg / std) * math.sqrt(252) if std > 0 else 0.0

    # 净值曲线最大回撤
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for p in pnls:
        equity *= (1.0 + p)
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    return {
        "n_trades": n,
        "win_rate": round(wins / n, 4),
        "sharpe": round(sharpe, 4),
        "total_return": round(total_return, 4),
        "avg_pnl": round(avg, 4),
        "max_drawdown": round(max_dd, 4),
    }


def _score(stats: Dict[str, float]) -> float:
    """
    综合得分 = sharpe - 2 * max_drawdown_penalty - sample_penalty
    MaxDD > MAX_DD_HARD_LIMIT 时打回; 样本不足时降权。
    """
    if stats["n_trades"] < MIN_TRAIN_SAMPLES // 2:
        return -999.0
    dd = stats["max_drawdown"]
    if dd > MAX_DD_HARD_LIMIT:
        return -999.0
    return stats["sharpe"] - 2.0 * dd


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap 置信区间
# ─────────────────────────────────────────────────────────────────────────────

def _bootstrap_metric(
    pnls: List[float],
    metric: str = "sharpe",
    n_boot: int = 200,
    seed: int = 42,
) -> Dict[str, float]:
    """对 sharpe / win_rate / total_return 做 bootstrap, 返回 95% CI"""
    if not pnls:
        return {"low": 0.0, "high": 0.0, "median": 0.0}
    rng = random.Random(seed)
    n = len(pnls)
    samples: List[float] = []
    for _ in range(n_boot):
        boot = [pnls[rng.randrange(n)] for _ in range(n)]
        s = _stats(boot)
        samples.append(s.get(metric, 0.0))
    samples.sort()
    lo = samples[int(0.025 * n_boot)]
    hi = samples[int(0.975 * n_boot) - 1]
    med = samples[n_boot // 2]
    return {"low": round(lo, 4), "high": round(hi, 4), "median": round(med, 4)}


# ─────────────────────────────────────────────────────────────────────────────
# 优化器主类
# ─────────────────────────────────────────────────────────────────────────────

class WeeklyParameterOptimizer:
    """
    周频参数优化器

    Usage:
        opt = WeeklyParameterOptimizer()
        report = opt.run()
    """

    def __init__(
        self,
        live_params: Optional[Dict[str, float]] = None,
        param_grid: Optional[Dict[str, List[float]]] = None,
        train_days: int = 30,
        valid_days: int = 7,
        max_candidates: int = 200,
    ):
        self.live_params = dict(live_params or LIVE_CURRENT_PARAMS)
        self.param_grid = dict(param_grid or PARAM_GRID)
        self.train_days = train_days
        self.valid_days = valid_days
        self.max_candidates = max_candidates

    # ──────── 入口 ────────
    def run(self) -> OptimizationReport:
        end_dt = datetime.utcnow().date()
        valid_start = end_dt - timedelta(days=self.valid_days)
        train_start = valid_start - timedelta(days=self.train_days)

        train_rows = load_trade_rows(
            start_date=train_start.isoformat(),
            end_date=valid_start.isoformat(),
        )
        valid_rows = load_trade_rows(
            start_date=valid_start.isoformat(),
            end_date=end_dt.isoformat(),
        )
        notes: List[str] = []

        if len(train_rows) < MIN_TRAIN_SAMPLES:
            notes.append(
                f"训练集样本不足 ({len(train_rows)} < {MIN_TRAIN_SAMPLES}), "
                "回退到使用最近 60 天全部数据进行训练。"
            )
            train_rows = load_trade_rows(
                start_date=(end_dt - timedelta(days=60)).isoformat(),
                end_date=end_dt.isoformat(),
            )

        # ── 网格搜索 ──
        candidates = self._grid_search(train_rows, valid_rows, notes)
        if not candidates:
            notes.append("所有候选参数均被淘汰, 返回当前实盘参数作为占位。")
            best = ParamCandidate(params=dict(self.live_params))
            best.is_stats = _stats(replay_pnl_series(train_rows, self.live_params))
            best.oos_stats = _stats(replay_pnl_series(valid_rows, self.live_params))
            best.score = _score(best.is_stats)
            candidates = [best]

        candidates.sort(key=lambda c: c.score, reverse=True)
        best = candidates[0]

        # ── 过拟合检测 ──
        is_sharpe = best.is_stats.get("sharpe", 0.0)
        oos_sharpe = best.oos_stats.get("sharpe", 0.0)
        if is_sharpe > 0:
            degradation = max(0.0, (is_sharpe - oos_sharpe) / is_sharpe)
        else:
            degradation = 0.0
        overfit = degradation > OVERFIT_DEGRADATION_THRESHOLD
        if overfit:
            notes.append(
                f"⚠️ 过拟合警告: OOS 相比 IS 衰减 {degradation*100:.1f}% "
                f"(IS sharpe={is_sharpe:.2f}, OOS sharpe={oos_sharpe:.2f})。"
                "请勿直接采用。"
            )

        # ── 敏感性分析 ──
        sens = self._sensitivity_analysis(best.params, train_rows)
        # ── 安全区间 ──
        safe_zones = self._safe_zones(sens, baseline_score=best.score)
        # ── Bootstrap CI ──
        oos_pnls = replay_pnl_series(valid_rows, best.params)
        is_pnls = replay_pnl_series(train_rows, best.params)
        ci = {
            "is_sharpe": _bootstrap_metric(is_pnls, "sharpe"),
            "oos_sharpe": _bootstrap_metric(oos_pnls, "sharpe"),
            "is_total_return": _bootstrap_metric(is_pnls, "total_return"),
            "oos_total_return": _bootstrap_metric(oos_pnls, "total_return"),
        }

        report = OptimizationReport(
            generated_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            train_window=(train_start.isoformat(), valid_start.isoformat()),
            valid_window=(valid_start.isoformat(), end_dt.isoformat()),
            train_size=len(train_rows),
            valid_size=len(valid_rows),
            best_params=best.params,
            best_score=round(best.score, 4),
            best_is_stats=best.is_stats,
            best_oos_stats=best.oos_stats,
            live_params=self.live_params,
            overfit_warning=overfit,
            degradation_ratio=round(degradation, 4),
            sensitivity=sens,
            bootstrap_ci=ci,
            safe_zones=safe_zones,
            top_candidates=[
                {"params": c.params, "score": round(c.score, 4),
                 "is_sharpe": c.is_stats.get("sharpe"),
                 "oos_sharpe": c.oos_stats.get("sharpe"),
                 "is_max_dd": c.is_stats.get("max_drawdown")}
                for c in candidates[:5]
            ],
            notes=notes,
        )
        return report

    # ──────── 网格搜索 ────────
    def _grid_search(
        self,
        train_rows: List[TradeRow],
        valid_rows: List[TradeRow],
        notes: List[str],
    ) -> List[ParamCandidate]:
        keys = list(self.param_grid.keys())
        spaces = [self.param_grid[k] for k in keys]
        total = 1
        for s in spaces:
            total *= len(s)
        notes.append(f"网格搜索空间: {total} 个候选 (上限 {self.max_candidates})")
        if total > self.max_candidates:
            # 简单子采样: 取每维等距采样使笛卡尔积 ≤ max
            spaces = [_subsample(s, max_n=4) for s in spaces]

        results: List[ParamCandidate] = []
        for values in product(*spaces):
            params = dict(zip(keys, values))
            is_pnls = replay_pnl_series(train_rows, params)
            if len(is_pnls) < max(3, MIN_TRAIN_SAMPLES // 4):
                continue
            is_stats = _stats(is_pnls)
            score = _score(is_stats)
            if score <= -100:  # 被淘汰
                continue
            oos_pnls = replay_pnl_series(valid_rows, params)
            oos_stats = _stats(oos_pnls) if oos_pnls else _stats([])
            results.append(ParamCandidate(
                params=params, is_stats=is_stats,
                oos_stats=oos_stats, score=score,
            ))
        return results

    # ──────── 单变量敏感性 (±10% 网格 + 邻近网格点) ────────
    def _sensitivity_analysis(
        self,
        best_params: Dict[str, float],
        rows: List[TradeRow],
    ) -> Dict[str, List[Dict[str, float]]]:
        sens: Dict[str, List[Dict[str, float]]] = {}
        for k, v in best_params.items():
            lo, hi = PARAM_BOUNDS[k]
            # 5 点扫描: -10%, -5%, 0%, +5%, +10% 相对值 (clamp 到 bounds)
            steps = [-0.10, -0.05, 0.0, 0.05, 0.10]
            sweep_points: List[Dict[str, float]] = []
            for s in steps:
                if v >= 0:
                    new_v = v * (1.0 + s)
                else:
                    new_v = v * (1.0 - s)  # 负数 +10% 表示更深, -10% 表示更浅
                new_v = max(lo, min(hi, new_v))
                trial = dict(best_params)
                trial[k] = new_v
                pnls = replay_pnl_series(rows, trial)
                stats = _stats(pnls)
                sweep_points.append({
                    "delta_pct": round(s * 100, 2),
                    "value": round(new_v, 4),
                    "sharpe": stats["sharpe"],
                    "win_rate": stats["win_rate"],
                    "max_drawdown": stats["max_drawdown"],
                    "n_trades": stats["n_trades"],
                })
            sens[k] = sweep_points
        return sens

    # ──────── 安全区间识别 ────────
    def _safe_zones(
        self,
        sens: Dict[str, List[Dict[str, float]]],
        baseline_score: float,
    ) -> Dict[str, Tuple[float, float]]:
        """
        定义安全区间: 在该区间内, sharpe 不低于基线的 80%。
        """
        zones: Dict[str, Tuple[float, float]] = {}
        for k, sweep in sens.items():
            valid = [pt for pt in sweep
                     if pt["sharpe"] >= 0.8 * abs(baseline_score)
                     and pt["max_drawdown"] <= MAX_DD_HARD_LIMIT]
            if not valid:
                # 退化到全部扫描点的最小/最大
                values = [pt["value"] for pt in sweep]
            else:
                values = [pt["value"] for pt in valid]
            zones[k] = (round(min(values), 4), round(max(values), 4))
        return zones


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _subsample(seq: List[float], max_n: int = 4) -> List[float]:
    if len(seq) <= max_n:
        return seq
    step = len(seq) / max_n
    return [seq[int(i * step)] for i in range(max_n)]


# ─────────────────────────────────────────────────────────────────────────────
# 报告输出
# ─────────────────────────────────────────────────────────────────────────────

def build_pairwise_heatmap(
    rows: List[TradeRow],
    base_params: Dict[str, float],
    pair: Tuple[str, str],
    grid: Optional[Dict[str, List[float]]] = None,
    metric: str = "sharpe",
) -> Dict[str, Any]:
    """
    生成 2D 参数热力图 (param_a × param_b -> metric)。

    返回结构:
        {
          "x_param": str, "y_param": str, "metric": str,
          "x_values": [...], "y_values": [...],
          "matrix": [[v00, v01, ...], ...]  # 行=y, 列=x
        }
    """
    grid = grid or PARAM_GRID
    a, b = pair
    if a not in grid or b not in grid:
        raise ValueError(f"未知参数: {pair}")
    xs = list(grid[a])
    ys = list(grid[b])
    matrix: List[List[float]] = []
    for y in ys:
        row_metrics: List[float] = []
        for x in xs:
            trial = dict(base_params)
            trial[a] = x
            trial[b] = y
            pnls = replay_pnl_series(rows, trial)
            stats = _stats(pnls)
            row_metrics.append(stats.get(metric, 0.0))
        matrix.append(row_metrics)
    return {
        "x_param": a,
        "y_param": b,
        "metric": metric,
        "x_values": xs,
        "y_values": ys,
        "matrix": matrix,
    }


def export_heatmap_bundle(
    report: OptimizationReport,
    rows: List[TradeRow],
    out_dir: Optional[Path] = None,
) -> Path:
    """
    导出全参对热力图 bundle (4 个参数 → C(4,2)=6 对)。
    """
    out_dir = out_dir or _REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = list(PARAM_GRID.keys())
    pairs: List[Tuple[str, str]] = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            pairs.append((keys[i], keys[j]))

    heatmaps = []
    for p in pairs:
        try:
            heatmaps.append(
                build_pairwise_heatmap(rows, report.best_params, p, metric="sharpe")
            )
        except Exception as e:
            logger.debug(f"heatmap {p} 失败: {e}")

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"heatmap_{ts}.json"
    payload = {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "base_params": report.best_params,
        "safe_zones": {k: list(v) for k, v in report.safe_zones.items()},
        "heatmaps": heatmaps,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "latest_heatmap.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"[WeeklyOptimizer] heatmap bundle: {out_path}")
    return out_path


def render_markdown(report: OptimizationReport) -> str:
    lines: List[str] = []
    lines.append(f"# Weekly Parameter Optimization Report")
    lines.append(f"_Generated: {report.generated_at}_")
    lines.append("")
    lines.append(f"## 数据窗口")
    lines.append(f"- 训练集 (IS): `{report.train_window[0]}` → `{report.train_window[1]}`, "
                 f"样本数 = {report.train_size}")
    lines.append(f"- 验证集 (OOS): `{report.valid_window[0]}` → `{report.valid_window[1]}`, "
                 f"样本数 = {report.valid_size}")
    lines.append("")

    lines.append("## 最优参数 vs 当前实盘")
    lines.append("| 参数 | 当前实盘 | 建议值 | Δ |")
    lines.append("|------|---------|-------|---|")
    for k in report.best_params:
        live_v = report.live_params.get(k, "—")
        new_v = report.best_params[k]
        try:
            delta = f"{(new_v - float(live_v)):+.4f}"
        except Exception:
            delta = "—"
        lines.append(f"| `{k}` | {live_v} | **{new_v}** | {delta} |")
    lines.append("")

    lines.append("## 训练 / 验证集统计")
    lines.append("| 指标 | IS (训练) | OOS (验证) |")
    lines.append("|------|----------|-----------|")
    for key in ("n_trades", "win_rate", "sharpe", "total_return", "max_drawdown"):
        lines.append(
            f"| {key} | {report.best_is_stats.get(key)} | {report.best_oos_stats.get(key)} |"
        )
    lines.append("")

    flag = "⚠️ 过拟合警告" if report.overfit_warning else "✅ 通过"
    lines.append(f"**过拟合检测**: {flag} (退化 {report.degradation_ratio*100:.1f}%)")
    lines.append("")

    lines.append("## Bootstrap 95% 置信区间")
    for k, ci in report.bootstrap_ci.items():
        lines.append(f"- `{k}`: median={ci['median']}, 95%CI=[{ci['low']}, {ci['high']}]")
    lines.append("")

    lines.append("## 敏感性分析 (单变量 ±10%)")
    for k, sweep in report.sensitivity.items():
        lines.append(f"### `{k}`")
        lines.append("| Δ% | value | sharpe | win_rate | max_dd | n |")
        lines.append("|----|-------|--------|----------|--------|---|")
        for pt in sweep:
            lines.append(
                f"| {pt['delta_pct']:+.0f}% | {pt['value']} | {pt['sharpe']} | "
                f"{pt['win_rate']} | {pt['max_drawdown']} | {pt['n_trades']} |"
            )
        lines.append("")

    lines.append("## 安全区间 (sharpe ≥ 80% baseline)")
    for k, (lo, hi) in report.safe_zones.items():
        lines.append(f"- `{k}`: [{lo}, {hi}]")
    lines.append("")

    if report.top_candidates:
        lines.append("## Top-5 候选参数组合")
        lines.append("| Rank | params | score | IS sharpe | OOS sharpe | IS MaxDD |")
        lines.append("|------|--------|-------|-----------|-----------|----------|")
        for i, c in enumerate(report.top_candidates, 1):
            lines.append(
                f"| {i} | `{json.dumps(c['params'], ensure_ascii=False)}` | "
                f"{c['score']} | {c['is_sharpe']} | {c['oos_sharpe']} | {c['is_max_dd']} |"
            )
        lines.append("")

    if report.notes:
        lines.append("## 备注")
        for n in report.notes:
            lines.append(f"- {n}")
        lines.append("")

    lines.append("---")
    lines.append("_本报告仅作为参数调整建议, 不会自动落地实盘配置, 需人工审核后通过 ConfigWriter 应用。_")
    return "\n".join(lines)


def write_report(report: OptimizationReport, out_dir: Optional[Path] = None) -> Tuple[Path, Path]:
    out_dir = out_dir or _REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"weekly_optimization_{ts}.json"
    md_path = out_dir / f"weekly_optimization_{ts}.md"

    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")

    # 同步写一份 latest, 方便外部消费
    (out_dir / "latest.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "latest.md").write_text(render_markdown(report), encoding="utf-8")

    logger.info(f"[WeeklyOptimizer] 报告已写出: {json_path}")
    return json_path, md_path


# ─────────────────────────────────────────────────────────────────────────────
# 顶层入口 (cron / 手动调用)
# ─────────────────────────────────────────────────────────────────────────────

def run_weekly_optimization(
    notifier: Any = None,
    live_params: Optional[Dict[str, float]] = None,
) -> OptimizationReport:
    """
    周频参数优化入口

    Args:
        notifier: 可选, 任意带 send_text(content) 方法的对象 (e.g. FeishuNotifier)
        live_params: 可选, 当前实盘参数, 缺省则使用 LIVE_CURRENT_PARAMS

    Returns:
        OptimizationReport
    """
    t0 = time.time()
    opt = WeeklyParameterOptimizer(live_params=live_params)
    try:
        report = opt.run()
    except Exception as e:
        logger.error(f"[WeeklyOptimizer] 运行失败: {e}", exc_info=True)
        if notifier and hasattr(notifier, "send_text"):
            try:
                notifier.send_text(f"[WeeklyOptimizer] 运行失败: {e}")
            except Exception:
                pass
        raise

    json_path, md_path = write_report(report)

    # 同步导出 heatmap bundle (供前端 / 复盘使用)
    try:
        end_dt = datetime.utcnow().date()
        all_rows = load_trade_rows(
            start_date=(end_dt - timedelta(days=opt.train_days + opt.valid_days)).isoformat(),
            end_date=end_dt.isoformat(),
        )
        if all_rows:
            export_heatmap_bundle(report, all_rows)
    except Exception as e:
        logger.warning(f"[WeeklyOptimizer] heatmap 导出失败 (不影响主流程): {e}")

    elapsed = time.time() - t0

    summary = _format_summary(report, elapsed, json_path)
    logger.info(summary)
    if notifier and hasattr(notifier, "send_text"):
        try:
            notifier.send_text(summary)
        except Exception as e:
            logger.warning(f"[WeeklyOptimizer] 通知发送失败: {e}")

    return report


def _format_summary(report: OptimizationReport, elapsed: float, json_path: Path) -> str:
    flag = "⚠️ 过拟合" if report.overfit_warning else "✅"
    lines = [
        f"📊 [WeeklyOptimizer] 周频参数优化完成 ({elapsed:.1f}s)",
        f"窗口: IS {report.train_window[0]}→{report.train_window[1]} "
        f"({report.train_size}笔) | OOS {report.valid_window[0]}→{report.valid_window[1]} "
        f"({report.valid_size}笔)",
        f"最优: score={report.best_score} | IS sharpe={report.best_is_stats.get('sharpe')} "
        f"OOS sharpe={report.best_oos_stats.get('sharpe')} | {flag}",
        "建议参数:",
    ]
    for k, v in report.best_params.items():
        live_v = report.live_params.get(k, "?")
        lines.append(f"  • {k}: {live_v} → {v}")
    lines.append(f"完整报告: {json_path}")
    return "\n".join(lines)


def _cli() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    notifier = None
    if os.getenv("WEEKLY_OPT_NOTIFY", "0") == "1":
        try:
            from app.notifier.feishu import get_notifier
            notifier = get_notifier()
        except Exception as e:
            logger.warning(f"加载 notifier 失败: {e}")

    report = run_weekly_optimization(notifier=notifier)
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
