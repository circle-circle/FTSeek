#!/usr/bin/env python3
"""
Daily IC Analysis — 按 LLM 特征分组统计信号质量
===============================================

每日 08:00 ET (盘前) 运行，输出:
1. 总体 IC (by trade_mode, strategy, direction)
2. 因子级 IC (f1_trend ~ f5_lifecycle)
3. LLM 特征分组 IC:
   - debate_consensus: Hermes+Bull 同向的信号
   - debate_conflict: 方向冲突的信号
   - high_confidence: confidence > 0.7
   - enrichment_cited: rationale 中引用了数据的信号
   - causal_chain: rationale 含因果链的信号
4. Regime 分组 IC
5. 趋势报告 (最近 7 天 IC 变化)

输出: data/ic_analysis_latest.json + 飞书/钉钉通知

用法:
    python3 scripts/daily_ic_analysis.py [--notify] [--days 30]
"""

import json
import logging
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

logger = logging.getLogger("daily_ic_analysis")

PROJECT_ROOT = Path(__file__).parent.parent
DB_SIGNALS = PROJECT_ROOT / "data" / "signal_snapshots.db"
OUTPUT_PATH = PROJECT_ROOT / "data" / "ic_analysis_latest.json"


def _resolve_main_db() -> Path:
    """交易库必须按 TRADE_ENV 解析 — 硬编码 sim 文件名会在切实盘那一刻起继续给
    旧臂打 IC 分 (与 2026-08-28 排查出的 ftseek_us_live.db 死库同族, 只是方向相反)。
    解析器不可用时按 TRADE_ENV 判, 绝不无条件锁死任一文件名。"""
    for mod, fn in (("app.core.db_utils", "get_main_db"),
                    ("app.core.paths", "get_trade_records_db")):
        try:
            import importlib
            return Path(str(getattr(importlib.import_module(mod), fn)()))
        except Exception:
            continue
    live = os.getenv("TRADE_ENV", "1").strip() == "0"
    return PROJECT_ROOT / "data" / ("ftseek_us_live.db" if live else "ftseek_us.db")


DB_TRADES = _resolve_main_db()

# ═══════════════════════════════════════════════════════════════
# IC Calculation
# ═══════════════════════════════════════════════════════════════

def spearman_ic(strengths, returns):
    """Spearman rank IC between signal values and forward returns"""
    pairs = [(s, r) for s, r in zip(strengths, returns) if s is not None and r is not None]
    if len(pairs) < 5:
        return None, None, len(pairs)
    s_vals, r_vals = zip(*pairs)
    s_ranks = np.argsort(np.argsort(s_vals)).astype(float)
    r_ranks = np.argsort(np.argsort(r_vals)).astype(float)
    n = len(s_ranks)
    d2 = np.sum((s_ranks - r_ranks) ** 2)
    ic = 1 - (6 * d2) / (n * (n**2 - 1)) if n > 1 else 0
    wins = sum(1 for s, r in pairs
               if (s > np.median(s_vals) and r > 0) or
                  (s <= np.median(s_vals) and r <= 0))
    wr = wins / len(pairs)
    return round(float(ic), 4), round(float(wr), 4), len(pairs)


# ═══════════════════════════════════════════════════════════════
# Data Loading
# ═══════════════════════════════════════════════════════════════

def load_signals(days=90, since_date=None):
    """Load signal snapshots with forward returns.
    since_date (如 '2026-07-01') 给定时用**固定起点** (冻结队列, 逐月只增不重基线,
    2026-10-16 DEB-20261016 L0) — 滚动 days 窗的月度读数会随窗口漂移, 不可比。"""
    conn = sqlite3.connect(str(DB_SIGNALS))
    conn.row_factory = sqlite3.Row
    try:
        from app.utils.timezone import now_et as _now_et
        cutoff = since_date or (_now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = since_date or (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT * FROM signal_snapshots
        WHERE fwd_ret_t1 IS NOT NULL
          AND trade_date >= ?
    """, (cutoff,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_trades(days=90):
    """Load trade records with PnL"""
    conn = sqlite3.connect(str(DB_TRADES))
    conn.row_factory = sqlite3.Row
    try:
        from app.utils.timezone import now_et as _now_et
        cutoff = (_now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT * FROM trade_records
        WHERE pnl_pct IS NOT NULL
          AND created_at >= ?
          AND trade_mode != 'live'
    """, (cutoff,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_debate_logs(days=30):
    """Load debate engine logs"""
    debates_dir = PROJECT_ROOT / "data" / "debates"
    if not debates_dir.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    logs = []
    for f in debates_dir.glob("unified_*.json"):
        try:
            data = json.loads(f.read_text())
            ts = datetime.fromisoformat(data.get("timestamp", ""))
            if ts >= cutoff:
                logs.append(data)
        except Exception:
            pass
    # Also load old format debate files
    for f in debates_dir.glob("debate_*.json"):
        try:
            data = json.loads(f.read_text())
            ts_str = data.get("timestamp", "")
            if ts_str:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00").split("+")[0])
                if ts.replace(tzinfo=None) >= cutoff:
                    logs.append(data)
        except Exception:
            pass
    return logs


# ═══════════════════════════════════════════════════════════════
# Analysis Dimensions
# ═══════════════════════════════════════════════════════════════

def analyze_by_group(signals, key_fn, label):
    """Group signals and compute IC for each group"""
    groups = defaultdict(list)
    for s in signals:
        k = key_fn(s)
        if k is not None:
            groups[k].append(s)

    results = {}
    for k, g in sorted(groups.items(), key=lambda x: str(x[0])):
        strengths = [s["strength"] for s in g]
        rets_t1 = [s["fwd_ret_t1"] for s in g if s.get("fwd_ret_t1") is not None]
        ic_t1, wr_t1, n_t1 = spearman_ic(strengths[:len(rets_t1)], rets_t1) if rets_t1 else (None, None, 0)

        rets_t5 = [s["fwd_ret_t5"] for s in g if s.get("fwd_ret_t5") is not None]
        ic_t5, wr_t5, n_t5 = spearman_ic(strengths[:len(rets_t5)], rets_t5) if rets_t5 else (None, None, 0)

        results[str(k)] = {
            "n": len(g),
            "ic_t1": ic_t1, "wr_t1": wr_t1,
            "ic_t5": ic_t5, "wr_t5": wr_t5,
            "avg_ret_t1": round(float(np.mean(rets_t1)), 4) if rets_t1 else None,
        }
    return results


def analyze_factors(trades):
    """Factor-level IC from trade records (E1 修复 2026-08-07)

    原实现直接读 trade_records.signal_strength — 该字段是 confidence 的镜像值
    (见 playbook_service.py:1332 注释: DA 分歧=35/一致=70, "不应当质量分使用"),
    区分度被压平 → 交易层 IC 失真 (E1 实证: 131笔中120笔=35)。

    E1 修复: 通过 decision_id JOIN signal_snapshots.strength (真实信号强度,
    132/132 命中), 缺失时 fallback 到镜像字段。并加链路分段 (DA- vs other),
    避免旧链路 (voting_llm etc.) 污染 DA 的真实 IC。
    """
    import sqlite3 as _sqlite3
    from pathlib import Path as _Path

    # 加载 signal_snapshots decision_id → strength 映射 (真实信号强度)
    _snap_map = {}
    try:
        _snap_db = _Path(__file__).parent.parent / "data" / "signal_snapshots.db"
        if _snap_db.exists():
            with _sqlite3.connect(str(_snap_db)) as _conn:
                for _r in _conn.execute(
                    "SELECT decision_id, strength FROM signal_snapshots "
                    "WHERE strength IS NOT NULL"
                ):
                    _snap_map[_r[0]] = _r[1]
    except Exception as _e:
        logger.warning(f"[E1] 加载 signal_snapshots strength 失败, 回退镜像字段: {_e}")

    factors = ["signal_strength", "signal_confidence", "consensus_level",
               "f1_trend", "f2_momentum", "f3_volume", "f4_orderflow", "f5_lifecycle"]
    results = {}
    for f in factors:
        # 按链路分组: DA- (新链路) vs other (旧链路/其他)
        groups = {"all": [], "da": [], "other": []}
        for t in trades:
            if t.get(f) is None:
                continue
            # signal_strength 用真实 snapshot 值 (E1), 其余字段保持原值
            val = t[f]
            if f == "signal_strength":
                _did = t.get("decision_id") or ""
                val = _snap_map.get(_did, t[f])
            groups["all"].append((val, t["pnl_pct"]))
            grp = "da" if (t.get("decision_id") or "").startswith("DA-") else "other"
            groups[grp].append((val, t["pnl_pct"]))
        row = {}
        for g, pairs in groups.items():
            ic, wr, n = spearman_ic([p[0] for p in pairs], [p[1] for p in pairs])
            row[g] = {"ic": ic, "wr": wr, "n": n}
        results[f] = row
    return results


# 策略组活性阈值 (天): 组的最后一条信号早于此天数 ⇒ 判为已停产, 不作"当前信号质量"引用
#   2026-09-18 (用户令"按建议动手执行"): 此前停产管线标签 (BATTLE_PLAN_FAST_PATH 最后
#   信号 06-25 / unknown 06-29 / technical_fallback 08-21) 与活跃主链同列 llm_feature_ic,
#   被 key_findings 打 ✅ 后再由盘前报告转述给决策者 — 而当日活跃主链 deep_analysis 反而
#   不在头条里。停产组改存 report["retired_strategy_groups"] (仅存档, 不进头条)。
ACTIVE_STRATEGY_DAYS = 14


def _is_retired_strategy(last_seen: str) -> bool:
    """策略组最后信号是否早于 ACTIVE_STRATEGY_DAYS 天 (日期解析失败=不判停产, 宁留勿误删)"""
    try:
        d = datetime.strptime(str(last_seen or "")[:10], "%Y-%m-%d").date()
    except Exception:
        return False
    try:
        from app.utils.timezone import now_et as _now_et
        base = _now_et().date()
    except Exception:
        base = datetime.now().date()
    return (base - d).days > ACTIVE_STRATEGY_DAYS


def analyze_llm_features(signals, trades):
    """
    LLM 特征分组 IC:
    - debate_consensus: Hermes+Bear 同向
    - debate_conflict: 方向冲突
    - high_confidence: confidence > 0.7
    - long_only: LONG 方向信号
    - short_only: SHORT 方向信号
    """
    results = {}

    # By direction
    for d in ["LONG", "SHORT"]:
        group = [s for s in signals if s.get("direction") == d]
        if group:
            strengths = [s["strength"] for s in group]
            rets = [s["fwd_ret_t1"] for s in group if s.get("fwd_ret_t1") is not None]
            ic, wr, n = spearman_ic(strengths[:len(rets)], rets) if rets else (None, None, 0)
            results[f"direction_{d}"] = {
                "n": len(group), "ic_t1": ic, "wr_t1": wr,
                "avg_ret": round(float(np.mean(rets)), 4) if rets else None,
            }

    # By confidence
    for lo, hi, label in [(0, 0.4, "low_conf"), (0.4, 0.7, "mid_conf"), (0.7, 1.01, "high_conf")]:
        group = [s for s in signals if lo <= (s.get("confidence") or 0) < hi]
        if group:
            strengths = [s["strength"] for s in group]
            rets = [s["fwd_ret_t1"] for s in group if s.get("fwd_ret_t1") is not None]
            ic, wr, n = spearman_ic(strengths[:len(rets)], rets) if rets else (None, None, 0)
            results[f"confidence_{label}"] = {
                "n": len(group), "ic_t1": ic, "wr_t1": wr,
                "avg_ret": round(float(np.mean(rets)), 4) if rets else None,
            }

    # By strategy (2026-09-18 活性分流: 活跃组进头条, 停产组只存档 — 见 ACTIVE_STRATEGY_DAYS)
    strats = set(s.get("strategy_id") or "unknown" for s in signals)
    retired = {}
    for st in strats:
        group = [s for s in signals if (s.get("strategy_id") or "unknown") == st]
        if len(group) >= 5:
            strengths = [s["strength"] for s in group]
            rets = [s["fwd_ret_t1"] for s in group if s.get("fwd_ret_t1") is not None]
            ic, wr, n = spearman_ic(strengths[:len(rets)], rets) if rets else (None, None, 0)
            last_seen = max(str(s.get("trade_date") or s.get("created_at") or "")[:10] for s in group)
            entry = {
                "n": len(group), "ic_t1": ic, "wr_t1": wr,
                "avg_ret": round(float(np.mean(rets)), 4) if rets else None,
                "last_seen": last_seen,
            }
            if _is_retired_strategy(last_seen):
                retired[f"strategy_{st}"] = entry
            else:
                results[f"strategy_{st}"] = entry

    # Trade-level: by strategy PnL
    trade_by_strat = defaultdict(list)
    for t in trades:
        s = t.get("strategy_id") or t.get("source_type") or "unknown"
        trade_by_strat[s].append(t)

    for st, tg in trade_by_strat.items():
        pnls = [t["pnl_pct"] for t in tg if t.get("pnl_pct") is not None]
        if pnls:
            wr = sum(1 for p in pnls if p > 0) / len(pnls)
            results[f"trade_strategy_{st}"] = {
                "n": len(pnls),
                "wr": round(wr, 4),
                "avg_pnl": round(float(np.mean(pnls)), 4),
                "total_pnl": round(float(sum(pnls)), 4),
            }

    return results, retired


def analyze_monthly_trend(signals, min_n=30):
    """按月维度 IC 趋势 (2026-09-01 用户裁: 历史早期链路混杂信号稀释窗口IC,
    需按月观察改善趋势)。口径与全局一致: Spearman(strength, fwd_ret_t1)。
    每月样本 < min_n 不出 IC 只记 n (防小样本噪声当结论, 与 OKR ≥30 门槛同标尺)。"""
    from collections import defaultdict as _dd
    months = _dd(list)
    for s in signals:
        td = s.get("trade_date") or ""
        if len(td) >= 7:
            months[td[:7]].append(s)
    trend = []
    for m in sorted(months):
        g = months[m]
        row = {"month": m, "n": len(g),
               "ic_t1": None, "wr_t1": None,
               "ic_long": None, "ic_short": None}
        if len(g) >= min_n:
            ic, wr, _ = spearman_ic([x["strength"] for x in g],
                                    [x["fwd_ret_t1"] for x in g])
            row["ic_t1"], row["wr_t1"] = ic, wr
            for d, key in (("LONG", "ic_long"), ("SHORT", "ic_short")):
                sub = [x for x in g if x.get("direction") == d]
                if len(sub) >= min_n:
                    ic_d, _, _ = spearman_ic([x["strength"] for x in sub],
                                             [x["fwd_ret_t1"] for x in sub])
                    row[key] = ic_d
        trend.append(row)
    return trend


def analyze_monthly_trend_frozen(signals, min_n=30):
    """冻结队列月度 IC (2026-10-16 DEB-20261016 L0) — 与 analyze_monthly_trend 的区别:
    输入是固定起点 (A10 现役时代 2026-07-01) 的冻结队列, 逐月只增不重基线 ⇒ 月度序列可比
    (滚动 90d 窗的月读数会漂移, 审计曾因此误读趋势线)。每月附:
      - ic_t1_day_mean / ci95_lo / ci95_hi / t / n_days: **日聚类**口径 (先逐日算 IC 再对
        日序列取均值±CI, 宪章第 9 律; pooled IC 会夸大显著性 ~4×, 只作对照);
      - by_chain: 活跃链路 (deep_analysis/universe_expansion) 各自月度 IC (已停产链不进表)。
    """
    from collections import defaultdict as _dd
    ACTIVE_CHAINS = ("deep_analysis", "universe_expansion")
    months = _dd(list)
    for s in signals:
        td = s.get("trade_date") or ""
        if len(td) >= 7:
            months[td[:7]].append(s)
    trend = []
    for m in sorted(months):
        g = months[m]
        row = {"month": m, "n": len(g), "ic_t1": None, "wr_t1": None}
        if len(g) >= min_n:
            ic, wr, _ = spearman_ic([x["strength"] for x in g],
                                    [x["fwd_ret_t1"] for x in g])
            row["ic_t1"], row["wr_t1"] = ic, wr
            # 日聚类: 逐日 IC → 日序列均值/CI (日 n<5 不入簇)
            by_day = _dd(list)
            for x in g:
                by_day[x["trade_date"]].append(x)
            day_ics = []
            for d in sorted(by_day):
                dg = by_day[d]
                if len(dg) >= 5:
                    dic, _, _ = spearman_ic([x["strength"] for x in dg],
                                            [x["fwd_ret_t1"] for x in dg])
                    if dic is not None:
                        day_ics.append(dic)
            row["n_days"] = len(day_ics)
            if len(day_ics) >= 2:
                import statistics as _st
                mu = sum(day_ics) / len(day_ics)
                se = _st.stdev(day_ics) / math.sqrt(len(day_ics)) if len(day_ics) > 1 else None
                row["ic_t1_day_mean"] = round(mu, 4)
                row["ci95_lo"] = round(mu - 1.96 * se, 4) if se else None
                row["ci95_hi"] = round(mu + 1.96 * se, 4) if se else None
                row["t"] = round(mu / se, 3) if se and se > 0 else None
            for chain in ACTIVE_CHAINS:
                sub = [x for x in g if x.get("strategy_id") == chain]
                if len(sub) >= min_n:
                    cic, _, _ = spearman_ic([x["strength"] for x in sub],
                                            [x["fwd_ret_t1"] for x in sub])
                    row.setdefault("by_chain", {})[chain] = \
                        {"n": len(sub), "ic_t1": cic}
        trend.append(row)
    return trend



def analyze_regime(trades):
    """PnL by market regime"""
    by_regime = defaultdict(list)
    for t in trades:
        r = t.get("market_regime") or "UNKNOWN"
        by_regime[r].append(t)

    results = {}
    for r, g in by_regime.items():
        pnls = [t["pnl_pct"] for t in g if t.get("pnl_pct") is not None]
        if pnls:
            wr = sum(1 for p in pnls if p > 0) / len(pnls)
            results[r] = {
                "n": len(pnls),
                "wr": round(wr, 4),
                "avg_pnl": round(float(np.mean(pnls)), 4),
                "total_pnl": round(float(sum(pnls)), 4),
            }
    return results


# ═══════════════════════════════════════════════════════════════
# Report Generation
# ═══════════════════════════════════════════════════════════════

def generate_report(days=90):
    """Run full IC analysis and generate report"""
    print(f"{'='*60}")
    print(f"  FTSeek-US Daily IC Analysis")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Window: {days} days")
    print(f"{'='*60}")

    signals = load_signals(days)
    trades = load_trades(days)
    debates = load_debate_logs(min(days, 30))

    print(f"\nData: {len(signals)} signals, {len(trades)} trades, {len(debates)} debate logs")

    report = {
        "generated_at": datetime.now().isoformat(),
        "window_days": days,
        "data_counts": {
            "signals": len(signals),
            "trades": len(trades),
            "debate_logs": len(debates),
        },
    }

    # 1. Overall IC by trade_mode
    print(f"\n{'─'*60}")
    print("1. IC by Trade Mode")
    print(f"{'─'*60}")
    mode_ic = analyze_by_group(signals, lambda s: s.get("trade_mode"), "trade_mode")
    report["ic_by_mode"] = mode_ic
    for k, v in mode_ic.items():
        print(f"  {k}: N={v['n']} IC_t1={v['ic_t1']} WR={v['wr_t1']} AvgRet={v['avg_ret_t1']}")

    # 2. IC by direction
    print(f"\n{'─'*60}")
    print("2. IC by Direction")
    print(f"{'─'*60}")
    dir_ic = analyze_by_group(signals, lambda s: s.get("direction"), "direction")
    report["ic_by_direction"] = dir_ic
    for k, v in dir_ic.items():
        print(f"  {k}: N={v['n']} IC_t1={v['ic_t1']} WR={v['wr_t1']} AvgRet={v['avg_ret_t1']}")

    # 3. Factor-level IC
    print(f"\n{'─'*60}")
    print("3. Factor-Level IC (trades, E1 链路分段: all/DA-/other)")
    print(f"{'─'*60}")
    factor_ic = analyze_factors(trades)
    report["factor_ic"] = factor_ic
    for k, v in factor_ic.items():
        _a, _d, _o = v.get("all", {}), v.get("da", {}), v.get("other", {})
        _fmt = lambda d: f"IC={d.get('ic')} N={d.get('n')} WR={d.get('wr')}" if d.get("n") else "N/A"
        print(f"  {k}\n    all  : {_fmt(_a)}\n    DA-  : {_fmt(_d)}\n    other: {_fmt(_o)}")

    # 4. LLM feature groups
    print(f"\n{'─'*60}")
    print("4. LLM Feature Group IC")
    print(f"{'─'*60}")
    llm_ic, retired_strats = analyze_llm_features(signals, trades)
    report["llm_feature_ic"] = llm_ic
    report["retired_strategy_groups"] = retired_strats
    for k, v in llm_ic.items():
        ic = v.get("ic_t1") or v.get("ic") or "—"
        wr = v.get("wr_t1") or v.get("wr") or "—"
        extra = f"AvgRet={v.get('avg_ret')}" if "avg_ret" in v else f"TotalPnL={v.get('total_pnl', '—')}"
        print(f"  {k}: N={v['n']} IC={ic} WR={wr} {extra}")
    if retired_strats:
        # 停产组单列: 供人/报告区分"当前信号质量"与"历史池残渣", 禁止计入头条结论
        print("  ── 已停产策略组 (不计入当前信号质量, 仅存档) ──")
        for k, v in retired_strats.items():
            print(f"  {k}: N={v['n']} IC={v.get('ic_t1')} 最后信号={v.get('last_seen')} [已停产]")

    # 5. Regime PnL
    print(f"\n{'─'*60}")
    print("5. PnL by Market Regime")
    print(f"{'─'*60}")
    regime_pnl = analyze_regime(trades)
    report["regime_pnl"] = regime_pnl
    for k, v in regime_pnl.items():
        print(f"  {k}: N={v['n']} WR={v['wr']:.1%} AvgPnL={v['avg_pnl']:.2%} Total={v['total_pnl']:.2%}")

    # 5b. Monthly IC trend (历史早期链路混杂, 按月看改善趋势 — 2026-09-01 用户裁)
    monthly = analyze_monthly_trend(signals)
    report["monthly_ic_trend"] = monthly
    # 5c. 冻结队列月度 IC (2026-10-16 DEB-20261016 L0): 固定起点 A10 现役时代,
    #     逐月只增不重基线 + 日聚类 CI + 活跃链路分列 — 判决面用这个, 5b 滚动窗仅诊断。
    frozen_signals = load_signals(since_date="2026-07-01")
    monthly_frozen = analyze_monthly_trend_frozen(frozen_signals)
    report["monthly_ic_trend_frozen"] = monthly_frozen
    print(f"\n{'─'*60}")
    print("5c. Monthly IC Trend · 冻结队列 (起点 2026-07-01, 日聚类 CI; 判决面)")
    print(f"{'─'*60}")
    for r in monthly_frozen:
        if r["ic_t1"] is None:
            print(f"  {r['month']}: n={r['n']} (样本不足30)")
        else:
            ci = (f"CI[{r['ci95_lo']:+.3f},{r['ci95_hi']:+.3f}] t={r['t']}"
                  if r.get("ci95_lo") is not None else "CI=na(日簇<2)")
            chains = " ".join(f"{k}={v['ic_t1']:+.3f}(n{v['n']})"
                              for k, v in (r.get("by_chain") or {}).items())
            print(f"  {r['month']}: pooled={r['ic_t1']:+.3f} 日聚类={r.get('ic_t1_day_mean')} "
                  f"{ci} n={r['n']} {chains}")

    print(f"\n{'─'*60}")
    print("5b. Monthly IC Trend (n<30 只记样本数)")
    print(f"{'─'*60}")
    for r in monthly:
        if r["ic_t1"] is None:
            print(f"  {r['month']}: n={r['n']} (样本不足30)")
        else:
            extra = []
            if r["ic_long"] is not None:
                extra.append(f"LONG={r['ic_long']:+.3f}")
            if r["ic_short"] is not None:
                extra.append(f"SHORT={r['ic_short']:+.3f}")
            print(f"  {r['month']}: IC_t1={r['ic_t1']:+.3f} WR={r['wr_t1']:.2f} "
                  f"n={r['n']}{' ' + '/'.join(extra) if extra else ''}")

    # 6. Key findings
    findings = []
    for k, v in llm_ic.items():
        ic = v.get("ic_t1") or v.get("ic")
        if ic is not None and abs(ic) > 0.1:
            direction = "positive ✅" if ic > 0 else "negative ❌"
            findings.append(f"{k}: IC={ic} ({direction})")
    for k, v in regime_pnl.items():
        if v["total_pnl"] > 10:
            findings.append(f"Regime {k}: profitable (+{v['total_pnl']:.1f}%)")
        elif v["total_pnl"] < -10:
            findings.append(f"Regime {k}: LOSING ({v['total_pnl']:.1f}%)")

    # Monthly trend 发现: 最近完整月 IC 明显优于窗口均值 = 早期历史在稀释 (用户 2026-09-01 关注点)
    _valid_months = [r for r in monthly if r["ic_t1"] is not None and r["n"] >= 100]
    if _valid_months:
        _last_m = _valid_months[-1]
        _win_ic = mode_ic.get("simulate", {}).get("ic_t1")
        if (_win_ic is not None and _last_m["ic_t1"] is not None
                and _last_m["ic_t1"] - _win_ic > 0.02):
            findings.append(
                f"📈 最近完整月 {_last_m['month']} IC={_last_m['ic_t1']:+.3f} "
                f"优于窗口均值 {_win_ic:+.3f} — 早期链路历史在稀释窗口IC, 看 5b 月度趋势")

    report["key_findings"] = findings
    print(f"\n{'─'*60}")
    print("6. Key Findings")
    print(f"{'─'*60}")
    for f in findings:
        print(f"  • {f}")

    # 7. Recommendations
    recs = []
    # LONG vs SHORT
    long_ic = llm_ic.get("direction_LONG", {}).get("ic_t1")
    short_ic = llm_ic.get("direction_SHORT", {}).get("ic_t1")
    if long_ic and long_ic > 0.1:
        recs.append("✅ LONG 信号有预测力 (IC={}), 应优先做多".format(long_ic))
    if short_ic and short_ic < -0.05:
        recs.append("❌ SHORT 信号 IC={}, 做空逻辑需要重构".format(short_ic))

    # HIGH_VOL
    hv = regime_pnl.get("HIGH_VOL", {})
    if hv and hv.get("total_pnl", 0) < -10:
        recs.append("🚨 HIGH_VOL regime 总亏 {}%, 必须在此 regime 下停止交易".format(hv["total_pnl"]))

    # voting_llm
    vl = llm_ic.get("trade_strategy_voting_llm", {})
    if vl and vl.get("total_pnl", 0) < -10:
        recs.append("🚨 voting_llm 策略总亏 {}%, LLM 投票逻辑需要停止或重构".format(vl["total_pnl"]))

    # Confidence calibration
    hc = llm_ic.get("confidence_high_conf", {})
    lc = llm_ic.get("confidence_low_conf", {})
    if hc.get("ic_t1", 0) > lc.get("ic_t1", 0):
        recs.append("✅ 高置信度信号 IC 高于低置信度, 信心度校准有效")

    report["recommendations"] = recs
    print(f"\n{'─'*60}")
    print("7. Recommendations")
    print(f"{'─'*60}")
    for r in recs:
        print(f"  • {r}")

    # Save
    with open(str(OUTPUT_PATH), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n📁 Report saved: {OUTPUT_PATH}")

    return report


def format_notification(report):
    """Format report as concise notification"""
    findings = report.get("key_findings", [])
    recs = report.get("recommendations", [])

    lines = [
        f"📊 FTSeek-US 每日 IC 分析报告",
        f"📅 {report['generated_at'][:10]} | 窗口: {report['window_days']}天",
        f"📈 信号: {report['data_counts']['signals']} | 交易: {report['data_counts']['trades']}",
        "",
        "🔑 关键发现:",
    ]
    for f in findings[:5]:
        lines.append(f"  • {f}")

    if recs:
        lines.append("")
        lines.append("📋 行动建议:")
        for r in recs[:5]:
            lines.append(f"  • {r}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90, help="Analysis window in days")
    parser.add_argument("--notify", action="store_true", help="Send notification")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    report = generate_report(args.days)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.notify:
        msg = format_notification(report)
        print("\n" + "=" * 60)
        print(msg)
        # Try to send via Hermes notification system
        try:
            from app.notification.notifier import send_notification
            send_notification(msg, level="info")
        except Exception:
            pass
