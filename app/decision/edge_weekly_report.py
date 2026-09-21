"""
EdgeWeeklyReport — P0 配套 (2026-07-11): edge 影子闸门 & 信号质量 周度飞书汇总

每周五随 run_playbook_weekly 推送一次, 内容:
  1. 影子闸门对照 (edge_gate_shadow.jsonl, 近7天):
     信号数 / edge 判定分布 / 与实盘闸门一致率 / 分歧拆解 (edge更严 vs edge更松)
  2. 置信度桶风险归一期望 (signal_quality/latest.json 的 expectancy_by_bucket)
     + 是否单调 (置信度是否解释 edge)
  3. Top 因子组合 edge (归一 avg_pnl_pct, 最好/最差各3)

安全: 纯只读汇总 + 飞书推送; 任何异常静默 (不影响周报主流程)。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_SHADOW_LOG = _ROOT / "data" / "shadow" / "edge_gate_shadow.jsonl"
_LATEST = _ROOT / "data" / "signal_quality" / "latest.json"
_SNAP_DB = _ROOT / "data" / "signal_snapshots.db"
# 真实平仓源: ftseek_us.db::trade_records (独立 trade_records.db 为空表, 勿用)
_MAIN_DB = _ROOT / "data" / "ftseek_us.db"
# 硬拦截负因子集 (与 pipeline.py:974 _NEG_EDGE_FACTORS 同源) — 判定纯负 vs 混合
_HARD_BLOCK_NEG = {"pe_below_industry", "news_positive", "rsi_oversold"}


def _load_shadow(days: int) -> List[Dict[str, Any]]:
    """读取近 days 天的影子闸门记录."""
    if not _SHADOW_LOG.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    rows: List[Dict[str, Any]] = []
    try:
        with open(_SHADOW_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    ts = datetime.fromisoformat(r.get("ts", ""))
                    if ts >= cutoff:
                        rows.append(r)
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"[EdgeWeekly] 读影子日志失败: {e}")
    return rows


def build_edge_weekly_summary(days: int = 7) -> Dict[str, Any]:
    """汇总近 days 天的影子闸门对照 + 信号质量 edge 视角."""
    rows = _load_shadow(days)
    n = len(rows)
    edge_block = sum(1 for r in rows if r.get("edge_verdict") == "BLOCK")
    live_block = sum(1 for r in rows if r.get("live_verdict") == "BLOCK")
    agree = sum(1 for r in rows if r.get("agree_with_live"))
    # 分歧拆解
    edge_stricter = sum(  # edge 拦 / 实盘放 → edge 更严
        1 for r in rows
        if r.get("edge_verdict") == "BLOCK" and r.get("live_verdict") == "PASS"
    )
    edge_looser = sum(  # edge 放 / 实盘拦 → edge 更松
        1 for r in rows
        if r.get("edge_verdict") == "PASS" and r.get("live_verdict") == "BLOCK"
    )

    summary: Dict[str, Any] = {
        "days": days,
        "shadow_total": n,
        "edge_block": edge_block,
        "live_block": live_block,
        "agree_rate": round(agree / n, 4) if n else None,
        "edge_stricter": edge_stricter,
        "edge_looser": edge_looser,
        "expectancy_by_bucket": {},
        "expectancy_monotonic": None,
        "confidence_decoupled": None,
        "top_factor_edges": [],
        "bottom_factor_edges": [],
    }

    # 信号质量 edge 视角
    try:
        if _LATEST.exists():
            data = json.load(open(_LATEST, "r", encoding="utf-8"))
            rec = data.get("threshold_recommendations", {}) or {}
            summary["expectancy_by_bucket"] = rec.get("expectancy_by_bucket", {})
            summary["expectancy_monotonic"] = rec.get("expectancy_monotonic")
            # confidence 与 edge 脱钩 = 期望非单调
            mono = rec.get("expectancy_monotonic")
            summary["confidence_decoupled"] = (mono is False)

            combos = (data.get("factor_contribution", {}) or {}).get("top_combos", []) or []
            ranked = sorted(
                [c for c in combos if c.get("total", 0) >= 3],
                key=lambda c: c.get("avg_pnl_pct", 0.0),
                reverse=True,
            )
            summary["top_factor_edges"] = ranked[:3]
            summary["bottom_factor_edges"] = ranked[-3:][::-1] if len(ranked) > 3 else []
    except Exception as e:
        logger.debug(f"[EdgeWeekly] 读 signal_quality 失败: {e}")

    return summary


def format_edge_weekly_text(s: Dict[str, Any]) -> str:
    """格式化为飞书文本卡内容."""
    lines: List[str] = []
    d = s.get("days", 7)
    n = s.get("shadow_total", 0)

    lines.append(f"【影子闸门对照 · 近{d}天】")
    if n == 0:
        lines.append("• 暂无影子记录 (盘中周期未产生信号或系统刚重启)")
    else:
        ar = s.get("agree_rate")
        lines.append(f"• 评估信号: {n} 笔")
        lines.append(f"• edge 拦截: {s.get('edge_block')} 笔 | 实盘闸门拦截: {s.get('live_block')} 笔")
        lines.append(f"• 与实盘判定一致率: {ar:.1%}" if ar is not None else "• 一致率: N/A")
        lines.append(f"• 分歧: edge更严(实盘放/edge拦) {s.get('edge_stricter')} 笔; "
                     f"edge更松(实盘拦/edge放) {s.get('edge_looser')} 笔")

    lines.append("")
    lines.append("【置信度桶 · 风险归一期望】")
    exp = s.get("expectancy_by_bucket") or {}
    if exp:
        for k, v in exp.items():
            try:
                lines.append(f"• {k}: {float(v) * 100:+.3f}%")
            except Exception:
                lines.append(f"• {k}: {v}")
        if s.get("confidence_decoupled"):
            lines.append("⚠️ 桶期望非单调 → 置信度不解释 edge (单一阈值非合适工具)")
        elif s.get("expectancy_monotonic"):
            lines.append("✓ 桶期望单调 → 置信度近期与 edge 同向")
    else:
        lines.append("• 暂无 (signal_quality 报告未生成)")

    top = s.get("top_factor_edges") or []
    bot = s.get("bottom_factor_edges") or []
    if top or bot:
        lines.append("")
        lines.append("【因子组合 edge · 归一期望】")
        for c in top:
            lines.append(f"• ✅ {c.get('factors')}: {c.get('avg_pnl_pct', 0) * 100:+.3f}% "
                         f"(n={c.get('total')}, 胜率{c.get('win_rate', 0):.0%})")
        for c in bot:
            lines.append(f"• ❌ {c.get('factors')}: {c.get('avg_pnl_pct', 0) * 100:+.3f}% "
                         f"(n={c.get('total')}, 胜率{c.get('win_rate', 0):.0%})")

    lines.append("")
    lines.append("说明: 影子闸门不改实盘, 仅用于验证 edge 判定是否优于置信度闸门。")
    return "\n".join(lines)


def _ro_conn(path: Path):
    """只读打开 sqlite (mode=ro, 不加锁, 不影响主流程/实盘写入)."""
    import sqlite3
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def build_loop_health(days: int = 7) -> Dict[str, Any]:
    """闭环健康三不变量 + 混合含负因子 edge (Phase1 决策前置数据).

    通盘联动的可观测面: 任一环断裂 (不写记录 / edge 不更新 / 混合负因子放血) 当周暴露.
    纯只读, 每段独立 try, 异常不影响周报主流程.
      1. record   : 记录完整性 (signal_snapshots decision_id / market_context 填充率)
      2. freshness: edge 新鲜度 (signal_quality/latest.json generated_at 时龄)
      3. feedback : 反馈生效 (trade_records 本周 vs 上周 WR/PnL)
      4. mixed_neg_edge: 混合含负因子信号 edge (playbook_factor_stats pair 级, 供 Phase1)
    """
    health: Dict[str, Any] = {"record": {}, "freshness": {}, "feedback": {}, "mixed_neg_edge": [], "low_sample_factors": {}}

    # 1. 记录完整性
    try:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        conn = _ro_conn(_SNAP_DB)
        tot = conn.execute(
            "SELECT COUNT(*) FROM signal_snapshots WHERE trade_date >= ?", (cutoff,)
        ).fetchone()[0]
        did = conn.execute(
            "SELECT COUNT(*) FROM signal_snapshots WHERE trade_date >= ? "
            "AND decision_id IS NOT NULL AND decision_id <> ''", (cutoff,)
        ).fetchone()[0]
        mc = conn.execute(
            "SELECT COUNT(*) FROM signal_snapshots WHERE trade_date >= ? "
            "AND market_context_json IS NOT NULL AND market_context_json <> '' "
            "AND market_context_json <> '{}'", (cutoff,)
        ).fetchone()[0]
        conn.close()
        health["record"] = {
            "snapshots": tot,
            "decision_id_fill": round(did / tot, 3) if tot else None,
            "market_context_fill": round(mc / tot, 3) if tot else None,
        }
    except Exception as e:
        logger.debug(f"[EdgeWeekly] 记录完整性失败: {e}")

    # 1b. MFE 记录有效性 (盈利单本应有正 MFE; 大面积失效 → MFE 管道断裂, 阻塞 MFE 类退出调优)
    try:
        conn = _ro_conn(_MAIN_DB)
        wins = conn.execute("SELECT COUNT(*) FROM trade_records WHERE pnl_pct > 0.001").fetchone()[0]
        # 2026-07-18 fix: mfe_pct 存储单位为百分比 (3.72=3.72%), pnl_pct 为 ratio (0.0311=3.11%).
        #   原 mfe_pct > pnl_pct 因量纲差 100x 恒真, 修正为同单位比较.
        valid = conn.execute(
            "SELECT COUNT(*) FROM trade_records WHERE pnl_pct > 0.001 AND mfe_pct > pnl_pct * 100"
        ).fetchone()[0]
        conn.close()
        if isinstance(health.get("record"), dict):
            health["record"]["mfe_valid_rate"] = round(valid / wins, 3) if wins else None
            health["record"]["mfe_win_sample"] = wins
    except Exception as e:
        logger.debug(f"[EdgeWeekly] MFE 有效性失败: {e}")

    # 2. edge 新鲜度
    try:
        if _LATEST.exists():
            data = json.load(open(_LATEST, "r", encoding="utf-8"))
            gen = data.get("generated_at")
            age_h = None
            if gen:
                age_h = round((datetime.now() - datetime.fromisoformat(gen)).total_seconds() / 3600, 1)
            health["freshness"] = {
                "generated_at": gen,
                "age_hours": age_h,
                "stale": bool(age_h is not None and age_h > 48),
            }
    except Exception as e:
        logger.debug(f"[EdgeWeekly] edge 新鲜度失败: {e}")

    # 3. 反馈生效 (真实平仓源 = ftseek_us.db::trade_records)
    try:
        conn = _ro_conn(_MAIN_DB)

        def _wk(lo: int, hi: int) -> Dict[str, Any]:
            los = (datetime.now() - timedelta(days=hi)).strftime("%Y-%m-%d")
            his = (datetime.now() - timedelta(days=lo)).strftime("%Y-%m-%d")
            # 2026-08-20 净口径切换 (用户裁"切"): COALESCE(pnl_net, realized_pnl)
            r = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN COALESCE(pnl_net, realized_pnl) > 0 THEN 1 ELSE 0 END), "
                "COALESCE(SUM(COALESCE(pnl_net, realized_pnl)), 0) FROM trade_records "
                "WHERE trade_date > ? AND trade_date <= ?", (los, his)
            ).fetchone()
            n = r[0] or 0
            return {
                "n": n,
                "win_rate": round((r[1] or 0) / n, 3) if n else None,
                "pnl": round(r[2] or 0, 2),
            }

        health["feedback"] = {"this_week": _wk(0, days), "prev_week": _wk(days, days * 2)}
        conn.close()
    except Exception as e:
        logger.debug(f"[EdgeWeekly] 反馈生效失败: {e}")

    # 4. 混合含负因子 edge (pair 级: 含≥1硬拦截负因子 且 含≥1非负因子 → 硬拦截不触发的灰色地带)
    try:
        conn = _ro_conn(_MAIN_DB)
        rows = conn.execute(
            "SELECT factor_key, total_trades, win_rate, avg_pnl, verdict, co_factors_json "
            "FROM playbook_factor_stats WHERE analysis_level = 'pair' "
            "ORDER BY total_trades DESC"
        ).fetchall()
        conn.close()
        mixed: List[Dict[str, Any]] = []
        for fk, n, wr, ap, vd, co in rows:
            try:
                facs = set(json.loads(co)) if co else set(str(fk).split("|"))
            except Exception:
                facs = set(str(fk).split("|"))
            has_neg = bool(facs & _HARD_BLOCK_NEG)
            has_non_neg = bool(facs - _HARD_BLOCK_NEG)
            if has_neg and has_non_neg and (n or 0) >= 5:
                mixed.append({
                    "pair": fk, "n": n,
                    "win_rate": round(wr, 3) if wr is not None else None,
                    "avg_pnl": round(ap, 2) if ap is not None else None,
                    "verdict": vd,
                })
        health["mixed_neg_edge"] = mixed[:8]
    except Exception as e:
        logger.debug(f"[EdgeWeekly] 混合负因子 edge 失败: {e}")

    # 5. 因子台账低样本护栏 (2026-09-21 用户裁): n<30 的因子不得据 verdict 上调权重.
    #    insider_cluster_buy 单因子 n=1 却 verdict=observing; insider_cluster_buy|pe_below_industry
    #    n=5 却 verdict=positive —— 低样本 positive 会被误当"有效因子"调权, 此处显式暴露.
    try:
        conn = _ro_conn(_MAIN_DB)
        rows = conn.execute(
            "SELECT factor_key, analysis_level, total_trades, verdict "
            "FROM playbook_factor_stats WHERE total_trades < 30 AND verdict <> 'negative' "
            "ORDER BY total_trades DESC"
        ).fetchall()
        conn.close()
        low = [{"factor": r[0], "level": r[1], "n": r[2] or 0, "verdict": r[3]} for r in rows]
        health["low_sample_factors"] = {
            "count": len(low),
            "positives": [
                f"{x['factor']}({x['level']},n={x['n']})"
                for x in low if x["verdict"] == "positive"
            ],
        }
    except Exception as e:
        logger.debug(f"[EdgeWeekly] 低样本因子护栏失败: {e}")

    return health


def format_loop_health_text(h: Dict[str, Any]) -> str:
    """闭环健康格式化为飞书文本."""
    lines: List[str] = ["【闭环健康 · 联动不变量】"]

    # 1. 记录完整性
    rec = h.get("record") or {}
    if rec:
        did = rec.get("decision_id_fill")
        mc = rec.get("market_context_fill")
        base = f"• 记录完整性: 快照 {rec.get('snapshots', 0)} 笔"
        if did is not None and mc is not None:
            base += f" | decision_id {did:.0%} | market_context {mc:.0%}"
        lines.append(base)
        if mc is not None and mc < 0.5:
            lines.append(f"  ⚠️ market_context 填充 {mc:.0%} → 断链: 上下文未写入, edge 归因缺市场状态维度")
        mv = rec.get("mfe_valid_rate")
        if mv is not None:
            lines.append(f"• MFE 记录有效性: 盈利单 {mv:.0%} 有正MFE (n={rec.get('mfe_win_sample')})")
            if mv < 0.5:
                lines.append("  ⚠️ mfe_pct 大面积失效 → 阻塞 MFE 类退出调优(Phase2 前置需先修复)")
    else:
        lines.append("• 记录完整性: N/A")

    # 2. edge 新鲜度
    fr = h.get("freshness") or {}
    if fr.get("age_hours") is not None:
        flag = " ⚠️ 陈旧(>48h,反馈链可能停摆)" if fr.get("stale") else " ✓"
        lines.append(f"• edge 新鲜度: latest.json {fr.get('age_hours')}h 前重算{flag}")
    else:
        lines.append("• edge 新鲜度: N/A (latest.json 无 generated_at)")

    # 3. 反馈生效
    fb = h.get("feedback") or {}
    tw = fb.get("this_week") or {}
    pw = fb.get("prev_week") or {}
    if tw:
        def _fmt(w: Dict[str, Any]) -> str:
            wr = w.get("win_rate")
            if wr is None:
                return f"n={w.get('n', 0)}"
            return f"n={w.get('n', 0)} WR{wr:.0%} PnL${w.get('pnl', 0):+.0f}"
        lines.append(f"• 反馈生效: 本周 {_fmt(tw)} | 上周 {_fmt(pw)}")

    # 4. 混合含负因子 edge (Phase1 前置观测)
    mixed = h.get("mixed_neg_edge") or []
    if mixed:
        lines.append("【混合含负因子 edge · Phase1 观测】(含负因子但硬拦截不触发)")
        for m in mixed:
            wr = m.get("win_rate")
            if wr is None:
                lines.append(f"  • {m.get('pair')}: n={m.get('n')}")
                continue
            mark = "❌" if wr < 0.35 else ("✅" if wr >= 0.5 else "•")
            lines.append(f"  {mark} {m.get('pair')}: WR{wr:.0%} avg_pnl${m.get('avg_pnl', 0):+.0f} "
                         f"(n={m.get('n')}, {m.get('verdict')})")
        lines.append("  → WR 跨度大: 一刀切降仓会误伤高 WR 组合; 仅净负者是 Phase1 降仓候选")

    # 5. 因子台账低样本护栏
    lsf = h.get("low_sample_factors") or {}
    if lsf:
        cnt = lsf.get("count", 0)
        lines.append(f"• 因子台账低样本护栏: n<30 且非 negative 因子 {cnt} 个 → 禁止据此上调权重")
        pos = lsf.get("positives") or []
        if pos:
            lines.append(f"  ⚠️ 低样本 positive (n<30 却判 positive, 上调权重须等 n≥30): {', '.join(pos)}")

    return "\n".join(lines)


def build_exit_telemetry_text(days: int = 7) -> str:
    """PLAYBOOK 退出遥测按 config_version 分层的紧凑摘要 (S4, 2026-07-11).

    富途真实模拟盘平仓单; MFE捕获率/回吐/亏损单MFE≈0占比。纯只读, 异常返回 ""。
    """
    import sqlite3 as _sq
    from collections import defaultdict as _dd
    cutoff = datetime.now() - timedelta(days=days)
    try:
        conn = _sq.connect(str(_MAIN_DB))
        conn.row_factory = _sq.Row
        rows = conn.execute(
            "SELECT config_version, COALESCE(pnl_net, realized_pnl) AS pnl_net, pnl_pct, mfe_pct, entry_time "
            "FROM trade_records WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
            "AND exit_time IS NOT NULL AND exit_time != ''"
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.debug(f"[EdgeWeekly] 退出遥测查询失败: {e}")
        return ""
    agg = _dd(lambda: {"n": 0, "wins": 0, "cap_s": 0.0, "cap_n": 0, "gb_s": 0.0, "loss": 0, "lz": 0})
    for r in rows:
        try:
            if datetime.fromisoformat((r["entry_time"] or "")[:19]) < cutoff:
                continue
        except Exception:
            pass
        cv = r["config_version"] or "v1"
        a = agg[cv]
        pnl = r["pnl_net"] or 0  # 2026-08-20 净口径 (COALESCE 别名, 见上方 SELECT)
        pp = float(r["pnl_pct"] or 0) * 100.0
        mfe = float(r["mfe_pct"] or 0)
        a["n"] += 1
        if pnl > 0:
            a["wins"] += 1
            if mfe > 0:
                a["cap_s"] += max(0.0, min(pp, mfe)) / mfe
                a["cap_n"] += 1
                a["gb_s"] += max(0.0, mfe - pp)
        else:
            a["loss"] += 1
            if mfe <= 0.1:
                a["lz"] += 1
    if not agg:
        return ""
    lines = [f"🎯 PLAYBOOK 退出遥测 (近{days}天, 真实模拟盘)"]
    for cv in sorted(agg):
        a = agg[cv]
        n = a["n"] or 1
        wr = 100.0 * a["wins"] / n
        cap = 100.0 * (a["cap_s"] / a["cap_n"]) if a["cap_n"] else 0.0
        gb = (a["gb_s"] / a["cap_n"]) if a["cap_n"] else 0.0
        lz = (100.0 * a["lz"] / a["loss"]) if a["loss"] else 0.0
        lines.append(f"  [{cv}] n={a['n']} 胜率{wr:.0f}% | MFE捕获{cap:.0f}% 回吐{gb:.2f}% | 亏损单MFE≈0 {lz:.0f}%")
    return "\n".join(lines)


def push_edge_weekly_report(days: int = 7) -> bool:
    """构建并推送 edge 周度汇总到飞书. 返回是否成功."""
    try:
        webhook = os.getenv("FEISHU_WEBHOOK_URL_US")
        if not webhook:
            logger.info("[EdgeWeekly] 未配置 FEISHU_WEBHOOK_URL_US, 跳过推送")
            return False
        summary = build_edge_weekly_summary(days)
        content = format_edge_weekly_text(summary)
        try:
            content += "\n\n" + format_loop_health_text(build_loop_health(days))
        except Exception as _lh_err:
            logger.debug(f"[EdgeWeekly] 闭环健康附加失败(忽略): {_lh_err}")
        try:
            _tel = build_exit_telemetry_text(days)
            if _tel:
                content += "\n\n" + _tel
        except Exception as _te_err:
            logger.debug(f"[EdgeWeekly] 退出遥测附加失败(忽略): {_te_err}")
        from app.observability.feishu_bot import FeishuBotAlerter
        alerter = FeishuBotAlerter(webhook)
        title = f"📐 FTSeek-US Edge 影子闸门周报 {datetime.now().strftime('%m-%d')}"
        ok = alerter.send_info(title=title, content=content)
        logger.info(f"[EdgeWeekly] 飞书周报推送 {'成功' if ok else '失败'}")
        return ok
    except Exception as e:
        logger.debug(f"[EdgeWeekly] 推送异常 (已隔离): {e}")
        return False


__all__ = [
    "build_edge_weekly_summary",
    "format_edge_weekly_text",
    "build_loop_health",
    "format_loop_health_text",
    "build_exit_telemetry_text",
    "push_edge_weekly_report",
]
