#!/usr/bin/env python3
"""
E1: IC 归因隔离 — 按决策链路分段重算因子/字段级 IC
====================================================
背景 (2026-08-07 grilling 修正): P0"因子淘汰"的前提是 IC 口径干净。
现状 factor_ic_latest.json / ic_analysis_latest.json 的 field 级 IC
(signal_strength/confidence/consensus) 是 signal_snapshots 全量混算,
其中近30天混有 voting_llm 85条 + technical_fallback 34条 旧链路尾巴,
且 factors_json 存在 list[str] 字符串化形态 (256/282 list) 被旧提取器
当垃圾因子名 (strategy_type:: 232次 等)。

本脚本 (纯分析, 零行为变化, 不覆盖任何生产 JSON):
  1. 按 decision_id 前缀分组: DA- (新链路) vs other (旧链路/其他)
  2. 对每组计算:
     a. field 级 IC: strength / confidence / consensus_level × fwd_ret_t1
     b. 因子级 IC: factors_json 拍平 (修复字符串化形态) × fwd_ret_t1
  3. 输出三列对比 (全量 / DA- / other) + 保存 data/ic_attribution_latest.json

用法:
    python3 scripts/ic_attribution_by_link.py [--days 90]
"""
import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DB_SIGNALS = PROJECT_ROOT / "data" / "signal_snapshots.db"
OUTPUT = PROJECT_ROOT / "data" / "ic_attribution_latest.json"


# ── 修复版因子提取: 兼容 4 种历史形态 ──────────────────────────────
def extract_factors(raw):
    """归一化 factors_json → 干净因子名列表 (去重, 丢弃垃圾):
    - dict{claims,conditions,...}  : 结构化 deep_analysis 输出 (当前主流)
    - list[dict]                    : 结构化 claim 列表
    - list[str]                     : 旧版 27-condition
    - list[str] 字符串化            : 'claims:[...]' / 'strategy_type:...' 整串 → 丢弃
    """
    out = []

    def _add(v):
        if isinstance(v, str):
            s = v.strip()
            # 垃圾特征: 含冒号键值对 / 过长 / 嵌引号逃逸
            if (":" in s or "[" in s or "]" in s or "'" in s or '"' in s
                    or len(s) > 60 or s.startswith("claims")):
                return
            out.append(s)
        elif isinstance(v, dict):
            ct = v.get("claim_type") or v.get("source_type")
            if ct and isinstance(ct, str):
                out.append(ct)

    if isinstance(raw, dict):
        if "conditions" in raw or "claims" in raw:
            for c in (raw.get("conditions") or []):
                _add(c)
            claims = raw.get("claims")
            if isinstance(claims, list):
                for cl in claims:
                    _add(cl)
            elif isinstance(claims, str):
                # 字符串化 claims: 尝试二次解析, 失败则丢弃
                try:
                    for cl in json.loads(claims):
                        _add(cl)
                except Exception:
                    pass
        else:
            for k, v in raw.items():
                if v and isinstance(k, str):
                    _add(k)
    elif isinstance(raw, list):
        for f in raw:
            _add(f)
    return list(dict.fromkeys(out))


def spearman_ic(pairs):
    """pairs: [(strength, fwd_ret)] → (ic, n). Spearman rank correlation."""
    n = len(pairs)
    if n < 3:
        return None, n

    def _rank(values):
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

    ss = [p[0] for p in pairs]
    rs = [p[1] for p in pairs]
    xr, yr = _rank(ss), _rank(rs)
    mx, my = sum(xr) / n, sum(yr) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xr, yr))
    vx = sum((a - mx) ** 2 for a in xr)
    vy = sum((b - my) ** 2 for b in yr)
    if vx == 0 or vy == 0:
        return 0.0, n
    return num / math.sqrt(vx * vy), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_SIGNALS))
    conn.row_factory = sqlite3.Row
    try:
        from app.utils.timezone import now_et
        cutoff = (now_et() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT decision_id, direction, strength, confidence, consensus_level,
               fwd_ret_t1, alpha_t1, factors_json, strategy_id, trade_date
        FROM signal_snapshots
        WHERE fwd_ret_t1 IS NOT NULL AND strength IS NOT NULL
          AND trade_date >= ?
    """, (cutoff,)).fetchall()
    conn.close()
    print(f"窗口 {args.days} 天, 有效信号: {len(rows)}")

    # 链路分组: DA- vs other
    groups = {"da": [], "other": [], "all": []}
    for r in rows:
        if r["direction"] not in ("LONG", "SHORT"):
            continue
        rec = dict(r)
        grp = "da" if (r["decision_id"] or "").startswith("DA-") else "other"
        groups[grp].append(rec)
        groups["all"].append(rec)
    for g, v in groups.items():
        print(f"  {g}: {len(v)} (LONG/SHORT)")

    # ── field 级 IC ──
    fields = ["strength", "confidence", "consensus_level"]
    field_ic = {}
    for f in fields:
        field_ic[f] = {}
        for g, recs in groups.items():
            pairs = [(r[f], r["fwd_ret_t1"]) for r in recs if r[f] is not None]
            ic, n = spearman_ic(pairs)
            wins = sum(1 for p in pairs if p[1] > 0)
            wr = wins / n if n else 0
            avg_ret = sum(p[1] for p in pairs) / n if n else 0
            field_ic[f][g] = {
                "ic": round(ic, 4) if ic is not None else None,
                "n": n, "wr": round(wr, 4), "avg_ret": round(avg_ret, 5),
            }

    # ── 因子级 IC ──
    factor_signals = {g: defaultdict(list) for g in groups}
    for g, recs in groups.items():
        for r in recs:
            raw = r["factors_json"]
            if not raw or raw == "null":
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            for fac in extract_factors(parsed):
                alpha = r["alpha_t1"] if r["alpha_t1"] is not None else r["fwd_ret_t1"]
                factor_signals[g][fac].append((r["strength"], r["fwd_ret_t1"], alpha))

    factor_ic = {}
    all_factors = set()
    for g in groups:
        all_factors |= set(factor_signals[g].keys())
    for fac in sorted(all_factors):
        row = {}
        for g in groups:
            pairs = factor_signals[g][fac]
            ic, n = spearman_ic(pairs)
            if n < 3:
                row[g] = {"ic": None, "n": n, "wr": None, "avg_ret": None, "avg_alpha": None}
                continue
            wins = sum(1 for p in pairs if p[1] > 0)
            avg_ret = sum(p[1] for p in pairs) / n
            avg_alpha = sum(p[2] for p in pairs) / n
            row[g] = {
                "ic": round(ic, 4), "n": n, "wr": round(wins / n, 4),
                "avg_ret": round(avg_ret, 5), "avg_alpha": round(avg_alpha, 5),
            }
        factor_ic[fac] = row

    # ── 输出 ──
    def _fmt(cell):
        if cell.get("ic") is None:
            return f"{cell['n']:>4}  N/A"
        return f"{cell['n']:>4} {cell['ic']:+.4f}"

    print("\n=== field 级 IC (Spearman strength×fwd_ret_t1) ===")
    print(f"  {'Field':<18} {'全量':>14} {'DA-':>14} {'other':>14}")
    for f in fields:
        print(f"  {f:<18} {_fmt(field_ic[f]['all']):>14} {_fmt(field_ic[f]['da']):>14} {_fmt(field_ic[f]['other']):>14}")

    print("\n=== 因子级 IC (n>=10 显示) ===")
    print(f"  {'Factor':<26} {'全量':>14} {'DA-':>14} {'other':>14}")
    for fac, row in sorted(factor_ic.items(), key=lambda kv: -(kv[1]["da"].get("n") or 0)):
        if row["da"].get("n", 0) < 10 and row["other"].get("n", 0) < 10:
            continue
        print(f"  {fac:<26} {_fmt(row['all']):>14} {_fmt(row['da']):>14} {_fmt(row['other']):>14}")

    # ── 结论段: DA 链路 vs 全量 的符号/强弱变化 ──
    print("\n=== DA- 链路 vs 全量 对比结论 (n>=30) ===")
    for fac, row in sorted(factor_ic.items(), key=lambda kv: -(kv[1]["da"].get("n") or 0)):
        da, al = row["da"], row["all"]
        if (da.get("n") or 0) < 30:
            continue
        da_ic, al_ic = da.get("ic"), al.get("ic")
        if da_ic is None or al_ic is None:
            continue
        verdict = "✅正" if da_ic > 0.05 else ("❌负" if da_ic < -0.05 else "⚪零")
        flip = " <-- 符号翻转!" if (da_ic > 0.05 and al_ic < -0.05) or (da_ic < -0.05 and al_ic > 0.05) else ""
        print(f"  {fac:<26} DA ic={da_ic:+.4f} (n={da['n']}) | 全量 ic={al_ic:+.4f} (n={al['n']}) {verdict}{flip}")

    # ── 保存 ──
    out = {
        "_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_days": args.days,
            "method": "Spearman(strength, fwd_ret_t1), 按 decision_id 前缀分组 DA- vs other",
            "note": "E1 分析产物, 不覆盖 factor_ic_latest.json / ic_analysis_latest.json",
        },
        "field_ic": field_ic,
        "factor_ic": factor_ic,
    }
    with open(OUTPUT, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n已保存: {OUTPUT}")


if __name__ == "__main__":
    main()