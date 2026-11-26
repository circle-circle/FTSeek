#!/usr/bin/env python3
"""DA 入场质量趋势 KPI（record-only，零交易行为变更）——「保留 DA + LLM 改进」的度量尺。

用户 2026-11-14 战略：盘前聚焦（当前 edge）+ 保留 DA（LLM 能力孵化器）+ 流程成熟高效。
本工具把「DA 是否随 LLM/流程改进而变好」变成**可测量**：DA 的失血全在「入场即错」死单
（MFE<0.3%），活单（MFE≥0.3%）本身正期望。故 **死单率 = DA 改进的核心 KPI**。

只读承诺：sqlite mode=ro，不写库/不下单/不改生产；默认仅控制台，--json 才写产物。
口径：A9 干净；死单=mfe_pct<0.3%（A11 唯一口径，百分比）；bps=pnl_net/(entry_price×qty)×1e4。
盈亏平衡死单率 p* = L/(L+D)，L=活单笔均净bps、D=|死单笔均净bps|；实际死单率<p* ⇒ DA 正期望。
样本分层（2026-11-14 修尺子）：收养/补录记账行（sync_recovery_backfill 等）的 MFE/peak/
  regime 为收养时近似值（peak 重置=entry、regime 硬编码 RANGE），与信号入场不同量纲，
  混入会虚高死单率。故 headline 死单率只算**真实信号**，补录行单列（artifact）不抹账。
  详见 scripts/utils/da_sample.py。实证：11 月表观 22.8% → 真实信号 15.8%（与 10 月持平）。
空转指标（2026-11-19 D4，RFC-145 §4 B3 / RFC-215 §4 D4）：「同指令 ≥20 轮空转 且当日
  executed=0」= DA 产出会被自己 chase 帽锁死的计划 ⇒ 生成侧噪音成本。数据全部来自**既有**
  产物（pullback_limit_shadow.jsonl 的 chase_cap_violation 行 + playbook_instructions.executed），
  不新增机制、不新增采集。锚读数（RFC-145 §1, 窗 ≤2026-10-17）: 2167 轮 / 89 (指令,日) 对 /
  中位 10 / 最大 71 / ≥20 轮 40 对。join 键 = (decision_id, date)（决策 id 的日期段是
  生成夜 ET，与交易日用 shadow 行自身 date 字段，勿用 id 反推）。
用法：PYTHONPATH=. venv/bin/python scripts/da_entry_quality_trend.py --start 2026-07-01
"""
import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from scripts.utils.da_sample import split_signal_artifact

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
SHADOW_JSONL = ROOT / "data" / "shadow" / "pullback_limit_shadow.jsonl"
DEAD_MFE_PCT = 0.3
IDLE_MIN_ROUNDS = 20  # RFC-145 §4 B3 预注册阈值（≥20 轮）；改阈值 = 重新落档


def _connect():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load(conn, start):
    return [dict(r) for r in conn.execute(
        """
        SELECT trade_date, entry_price, quantity, pnl_net, realized_pnl, fees_est, mfe_pct, entry_reason
          FROM trade_records
         WHERE source_type='PLAYBOOK_SIM' AND owner_strategy='playbook_sim'
           AND trade_date>=? AND exit_price>0
           AND COALESCE(pnl_net, realized_pnl) IS NOT NULL
           AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''
        """, (start,)).fetchall()]


def _net(row):
    if row["pnl_net"] is not None:
        return row["pnl_net"]
    # realized_pnl 是毛盈亏；回退必须扣费，不能把毛额当净额（项目净 PnL 约定）。
    return (row["realized_pnl"] or 0.0) - (row.get("fees_est") or 0.0)


def _bps(row):
    denom = (row["entry_price"] or 0.0) * (row["quantity"] or 0)
    return _net(row) / denom * 1e4 if denom else 0.0


def _is_dead(row):
    return (row["mfe_pct"] or 0.0) < DEAD_MFE_PCT


def breakeven_dead_rate(dead_avg_bps, live_avg_bps):
    """p* = L/(L+D)。live_avg<=0 ⇒ 无正期望可谈，返回 None。"""
    if live_avg_bps is None or live_avg_bps <= 0:
        return None
    loss = abs(dead_avg_bps) if dead_avg_bps is not None else 0.0
    return live_avg_bps / (live_avg_bps + loss) if (live_avg_bps + loss) > 0 else None


def load_chase_cap_rounds(start, path=SHADOW_JSONL):
    """读既有 pullback_limit_shadow.jsonl（只读），按 (decision_id, date) 数 chase_cap_violation 轮。

    返回 Counter；文件缺失返回 None（不伪造零——「没数据」≠「零空转」）。
    不新增采集：该 jsonl 由既有 PULLBACK_LIMIT 影子写方维护，本报表只读。
    """
    if not Path(path).exists():
        return None
    pairs = Counter()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue  # 坏行跳过，不计数（同坏行不进分母的既有纪律）
            if r.get("type") != "chase_cap_violation":
                continue
            if str(r.get("date") or "") < start:
                continue
            dec = r.get("decision_id")
            if not dec:
                continue
            pairs[(dec, str(r.get("date")))] += 1
    return pairs


def load_executed_map(conn, decision_ids):
    """playbook_instructions.executed 按 (decision_id, date) → bool；只读 mode=ro。

    decision_id 无对应指令行 ⇒ 键缺席（调用方归 unknown，不误判为未执行）。
    """
    ids = sorted(set(decision_ids))
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = conn.execute(
            "SELECT decision_id, date, executed FROM playbook_instructions"
            " WHERE decision_id IN (%s)" % ",".join("?" * len(chunk)), chunk)
        for dec, d, ex in q:
            out[(dec, str(d))] = bool(ex)
    return out


def idle_churn_metrics(pairs, executed_map, min_rounds=IDLE_MIN_ROUNDS):
    """RFC-145 §4 B3 指标：同指令 ≥min_rounds 轮空转 且当日 executed=0（纯函数，可测）。

    pairs: Counter{(decision_id, date): rounds}；executed_map: {(decision_id, date): bool}。
    unknown = ≥min_rounds 但指令行缺席（找不到就如实单列，不当未执行）。
    """
    values = sorted(pairs.values())
    ge20 = [k for k in pairs if pairs[k] >= min_rounds]
    idle = [k for k in ge20 if executed_map.get(k) is False]
    unknown = [k for k in ge20 if k not in executed_map]
    by_month = defaultdict(list)
    for k in idle:
        by_month[k[1][:7]].append(pairs[k])
    monthly = [{"month": m, "idle_pairs": len(v), "idle_rounds": sum(v)}
               for m, v in sorted(by_month.items())]
    return {
        "min_rounds": min_rounds,
        "rounds_total": sum(values),
        "pairs_total": len(values),
        "median_rounds": (values[len(values) // 2] if values else None),
        "max_rounds": (values[-1] if values else None),
        "pairs_ge_min": len(ge20),
        "idle_pairs": len(idle),
        "idle_rounds": sum(pairs[k] for k in idle),
        "executed_pairs_ge_min": sum(1 for k in ge20 if executed_map.get(k) is True),
        "unknown_pairs_ge_min": len(unknown),
        "monthly": monthly,
    }


def analyze(rows):
    signal, artifact = split_signal_artifact(rows)
    dead = [r for r in signal if _is_dead(r)]
    live = [r for r in signal if not _is_dead(r)]
    dead_avg = sum(_bps(r) for r in dead) / len(dead) if dead else None
    live_avg = sum(_bps(r) for r in live) / len(live) if live else None
    pstar = breakeven_dead_rate(dead_avg, live_avg)
    dead_rate = len(dead) / len(signal) if signal else None
    by_month = defaultdict(list)
    for row in signal:
        by_month[row["trade_date"][:7]].append(row)
    trend = []
    for month in sorted(by_month):
        subset = by_month[month]
        d = sum(1 for r in subset if _is_dead(r))
        trend.append({
            "month": month, "n": len(subset), "dead_n": d,
            "dead_rate_pct": round(100 * d / len(subset), 1),
            "net_bps": round(sum(_net(r) for r in subset) / sum((r["entry_price"] or 0) * (r["quantity"] or 0) for r in subset) * 1e4, 2)
            if sum((r["entry_price"] or 0) * (r["quantity"] or 0) for r in subset) else None,
        })
    art_dead = [r for r in artifact if _is_dead(r)]
    art_net = sum(_net(r) for r in artifact)
    art_den = sum((r["entry_price"] or 0) * (r["quantity"] or 0) for r in artifact)
    dead_all = len(dead) + len(art_dead)
    return {
        "n": len(signal), "dead_n": len(dead), "live_n": len(live),
        "dead_rate_pct": round(100 * dead_rate, 1) if dead_rate is not None else None,
        "dead_avg_bps": round(dead_avg, 2) if dead_avg is not None else None,
        "live_avg_bps": round(live_avg, 2) if live_avg is not None else None,
        "breakeven_dead_rate_pct": round(100 * pstar, 1) if pstar is not None else None,
        "below_breakeven": bool(pstar is not None and dead_rate is not None and dead_rate < pstar),
        "monthly": trend,
        "n_all": len(rows), "dead_n_all": dead_all,
        "dead_rate_all_pct": round(100 * dead_all / len(rows), 1) if rows else None,
        "artifact": {
            "n": len(artifact), "dead_n": len(art_dead),
            "dead_rate_pct": round(100 * len(art_dead) / len(artifact), 1) if artifact else None,
            "net_usd": round(art_net, 2),
            "net_bps": round(art_net / art_den * 1e4, 2) if art_den else None,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="DA 入场质量趋势 KPI（死单率 + 空转指标）")
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()
    conn = _connect()
    report = analyze(_load(conn, args.start))
    print("== DA entry-quality KPI (真实信号) | start=%s | n=%d ==" % (args.start, report["n"]))
    print("  dead(MFE<0.3%%)=%d (%.1f%%) avg=%.2fbps | live=%d avg=%.2fbps" % (
        report["dead_n"], report["dead_rate_pct"] or 0, report["dead_avg_bps"] or 0,
        report["live_n"], report["live_avg_bps"] or 0))
    print("  breakeven dead-rate=%.1f%% | actual=%.1f%% | below_breakeven(DA正期望)=%s" % (
        report["breakeven_dead_rate_pct"] or 0, report["dead_rate_pct"] or 0, report["below_breakeven"]))
    art = report["artifact"]
    print("  [分层] 收养/补录记账行(不计入死单率): n=%d dead=%d net=$%.2f | 全样本死单率=%.1f%%(n=%d)" % (
        art["n"], art["dead_n"], art["net_usd"], report["dead_rate_all_pct"] or 0, report["n_all"]))
    print("  monthly dead-rate trend (LLM/流程改进 KPI):")
    for m in report["monthly"]:
        print("    %s n=%3d dead=%4.1f%% netbps=%7.2f" % (m["month"], m["n"], m["dead_rate_pct"], m["net_bps"] or 0))
    # ── 空转指标 (RFC-145 §4 B3 / RFC-215 §4 D4): 同指令 ≥20 轮空转 且当日 executed=0 ──
    pairs = load_chase_cap_rounds(args.start)
    if pairs is None:
        report["idle_churn"] = None
        print("  [idle-churn] pullback_limit_shadow.jsonl 缺失 ⇒ 无数据 (不伪造零)")
    else:
        executed_map = load_executed_map(conn, [k[0] for k in pairs])
        idle = idle_churn_metrics(pairs, executed_map)
        report["idle_churn"] = idle
        print("  [idle-churn] start=%s | chase_cap_violation rounds=%d pairs=%d (median %s, max %s)" % (
            args.start, idle["rounds_total"], idle["pairs_total"],
            idle["median_rounds"], idle["max_rounds"]))
        print("    pairs >= %d rounds: %d | idle(>= %d ∧ executed=0): %d (rounds %d)"
              " | executed: %d | unknown(无指令行): %d" % (
                  idle["min_rounds"], idle["pairs_ge_min"], idle["min_rounds"],
                  idle["idle_pairs"], idle["idle_rounds"],
                  idle["executed_pairs_ge_min"], idle["unknown_pairs_ge_min"]))
        if idle["monthly"]:
            print("    monthly idle trend (DA 生成侧噪音成本):")
            for m in idle["monthly"]:
                print("      %s idle_pairs=%d idle_rounds=%d" % (m["month"], m["idle_pairs"], m["idle_rounds"]))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("wrote", args.json)


if __name__ == "__main__":
    main()
