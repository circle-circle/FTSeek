#!/usr/bin/env python3
"""UE tier 闸影子结算评估器 (2026-09-11, 用户裁「做」— 方案3: 裁决报告加方向分层读数).

消费 `data/shadow/ue_tier_gate_shadow.jsonl` (PROPOSAL-20260905 U3/P4, 写者
llm_universe_scan._log_tier_gate_shadow, 每扫描日 1 行, rows[]=当日候选逐条判定)。
此前只有写者无读者 — 15 交易日裁决点 (≈9/29) 前本器即"既有裁决报告"载体。

预注册判据 (逐字引 unified_config.yaml universe_expansion.tier_gate 注释, 2026-09-05):
  攒样≥15交易日 且 被影子化桶 fwd 回填 n≥30;
  毕业 = 被拦桶方向性前瞻均值≤0 且 放行桶均笔>0 → 用户裁决;
  撤闸认错 = 被拦桶前瞻 >+0.3% 且 n≥30。

方向分层 (2026-09-11 本批新增, record-only 读数并列不改判据):
  UE LONG 全史 n=41 WR46% 净−$780 vs SHORT n=28 WR68% +$208 (30天复核同源) —
  裁决日若 tier 门过线, LONG/SHORT 分层证据须已就位; 不过线则双证据一起否。
  分层只并列呈报, 不另设门 (防多重比较挪门柱)。

口径:
  - join playbook_instructions (date, 'US.'+symbol) 取 direction/fwd_return_t1/executed;
    fwd_return_t1 为方向调整后收益 (ic_tracker sign=±1, 可直接跨方向聚合)。
  - 被拦桶 = would_block=true 行; 放行桶 = would_block=false 行。
  - 影子零成交 (enforce=false) → 全部行都是"放行后实际发生", 被拦桶的 fwd 是反事实口径。

🔵 通道级经济性备证 (2026-09-26 用户裁「按建议执行」= 建议1: 登记备证不立门, record-only):
  **只印不判** — 新增 `channel_economics` 段, `gating=False`, **不参与 verdict 计算**。
  为什么加: 本轮实测 UE 通道 86 笔 (decision_id LIKE 'universe_expansion%') 显示
    毛利 −$40.38 / 费用 $524.88 / 净 −$565.27 ⇒ **毛利率 −1.02bps < 费率 13.25bps**,
    即「费用归零仍不赚」= 没有毛利来源, 与用户 8/22 证伪「执行纪律可保底」同型
    (那次费=毛利 42 倍, 本次更彻底: 毛利≈0)。前瞻 t 检验 LONG t=+0.109 / SHORT t=+0.874
    ⇒ **两方向皆零 alpha**, 故「禁 UE LONG / 不对称限流」在数据上无依据
    (且用户 8/2 明令「市场双向, 反对不对称限流」, 本判据亦禁「方向分层另设门」)。
  为什么**不立门**: ① UE 整体净亏 t=−0.827 **不显著** (n=86, 均 −6.57, sd 73.72);
    ② 9 月净仅 −$56.41 (7月 −104 / 8月 −404) ⇒ 金额小且无趋势;
    ③ SHORT 腿实盘毛利率 **+30.57bps** 是正 edge, 停通道会连带砍掉赚钱的腿。
  ⇒ 结论: 病灶不是「选错方向」而是「无毛利来源」, 而钱是退出机制赚的
    (LONG 与 SHORT 前瞻皆零 alpha, 实盘毛利却一负一正 ⇒ 差异在退出侧),
    退出参数已被 DEB-20260819 终裁「近似最优, 执行层不再改」⇒ 该杠杆合规上锁死。
  ⚠️ 审计局限 (判读时必读, 勿当强证据): `fees_est` 是**估算列** (伴 `fees_source` 标记来源),
    未逐笔对账; tier 维度对实盘成交覆盖率仅 **21/86=24.4%** (影子 9/08 起, 成交回溯 7/14);
    前瞻 vs 实盘相关 r=+0.403 (t=+4.01, n=85) ⇒ 尺子有效但不强 (符号一致率 63%)。

挂载: crontab 52 5 * * 6 (周六, fwd 回填 05:30 之后; ue_tier_shadow_eval 05:50 同窗)。
产物: data/shadow/ue_tier_gate_eval_latest.json (atomic write)。
用法: venv/bin/python scripts/ue_tier_gate_shadow_eval.py [--min-days 15] [--min-n 30]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
SHADOW = ROOT / "data" / "shadow" / "ue_tier_gate_shadow.jsonl"
OUT = ROOT / "data" / "shadow" / "ue_tier_gate_eval_latest.json"

MIN_DAYS = 15   # 预注册: 攒样交易日下限
MIN_N = 30      # 预注册: 被影子化桶 fwd 回填样本下限
RETIRE_RET = 0.003  # 预注册: 撤闸认错线 (被拦桶 fwd > +0.3% 且 n≥30)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _load_days() -> list:
    if not SHADOW.exists():
        return []
    days = []
    for ln in SHADOW.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            days.append(json.loads(ln))
        except Exception as e:  # 坏行留痕不静默
            print(f"[ue_tier_gate_eval] 影子档坏行跳过: {e} | {ln[:120]!r}", file=sys.stderr)
    return days


def _bucket_stats(rows: list) -> dict:
    fwd = [r["fwd"] for r in rows if r.get("fwd") is not None]
    n = len(fwd)
    mean = (sum(fwd) / n) if n else None
    return {"n_rows": len(rows), "n_fwd": n,
            "fwd_mean_pct": round(mean * 100, 3) if mean is not None else None}


def _channel_economics(conn) -> dict:
    """🔵 UE 通道级经济性备证 — **只印不判**, 不参与 verdict (2026-09-26 用户裁建议1).

    为什么是通道级而不是方向级: 前瞻 t 检验两方向皆零 alpha (LONG t=+0.109 / SHORT
    t=+0.874) ⇒ 「禁 UE LONG / 不对称限流」无数据依据, 且撞用户 8/2「反对不对称限流」
    与本判据「方向分层只并列呈报不另设门」禁令。方向读数只作并列披露。
    口径 (memory 纪律):
      - 归属用 `decision_id LIKE 'universe_expansion%'` — **不是** strategy_id
        (后者是 playbook_sim / extended_hours_entry 落库标签, 查它得 0 笔 = 假前提);
      - 量纲: pnl_pct / fwd_return_t1 均为**小数比率** (实测 |x|<1 占 99.9%/100%),
        转百分数一律 ×100, 两者必须同法处理 (混用会造出 100× 假落差);
      - symbol join 必归一化 US. 前缀。
    局限: fees_est 是估算列 (伴 fees_source), 未逐笔对账。
    """
    out: dict = {"gating": False, "adjudication_role": "record-only 备证, 不设门不改判据"}

    def _t(vals):
        n = len(vals)
        if n < 3:
            return None
        m = sum(vals) / n
        var = sum((x - m) ** 2 for x in vals) / (n - 1)
        sd = var ** 0.5
        return round(m / (sd / n ** 0.5), 3) if sd else None

    # ── 通道整体: 毛利 / 费用 / 净 + bps 口径 ──
    r = conn.execute("""
        SELECT COUNT(*) n, SUM(COALESCE(realized_pnl,0)) gross, SUM(COALESCE(fees_est,0)) fee,
               SUM(COALESCE(pnl_net,0)) net, AVG(COALESCE(pnl_pct,0)) avg_pct,
               AVG(entry_price*quantity) notional
        FROM trade_records WHERE decision_id LIKE 'universe_expansion%'
    """).fetchone()
    n = r["n"] or 0
    if n:
        notional = r["notional"] or 0
        gross_bps = (r["gross"] / n / notional * 10000) if notional else None
        fee_bps = (r["fee"] / n / notional * 10000) if notional else None
        pnl = [x[0] for x in conn.execute(
            "SELECT pnl_net FROM trade_records WHERE decision_id LIKE 'universe_expansion%' "
            "AND pnl_net IS NOT NULL")]
        out["all"] = {
            "n": n,
            "gross": round(r["gross"] or 0, 2), "fee": round(r["fee"] or 0, 2),
            "net": round(r["net"] or 0, 2),
            "gross_bps": round(gross_bps, 2) if gross_bps is not None else None,
            "fee_bps": round(fee_bps, 2) if fee_bps is not None else None,
            "net_t": _t(pnl),
            "net_zero_fee": round(r["gross"] or 0, 2),
        }
        out["verdict_note"] = (
            "毛利率 < 费率 ⇒ 结构性无毛利来源 (费用归零仍不赚), 非「选错方向」; "
            "net_t 不显著 (|t|<2) ⇒ 86 笔不足以证明系统性亏钱 ⇒ 不立门, 只登记备证。")

    # ── 方向并列 (只披露, 禁据此设门) ──
    dirs = {}
    for d in ("LONG", "SHORT"):
        x = conn.execute("""
            SELECT COUNT(*) n, SUM(COALESCE(realized_pnl,0)) gross, SUM(COALESCE(fees_est,0)) fee,
                   SUM(COALESCE(pnl_net,0)) net
            FROM trade_records WHERE decision_id LIKE 'universe_expansion%' AND direction=?
        """, (d,)).fetchone()
        if not (x["n"] or 0):
            continue
        notional = r["notional"] or 0
        dirs[d] = {
            "n": x["n"], "gross": round(x["gross"] or 0, 2), "fee": round(x["fee"] or 0, 2),
            "net": round(x["net"] or 0, 2),
            "gross_bps": round((x["gross"] or 0) / x["n"] / notional * 10000, 2) if notional else None,
        }
    if dirs:
        out["by_direction_disclose_only"] = dirs

    # ── 前瞻 alpha t 检验 (证「两方向皆零 alpha」) ──
    fwd = {}
    for d in ("LONG", "SHORT"):
        v = [x[0] * 100 for x in conn.execute("""
            SELECT i.fwd_return_t1 FROM trade_records t
            JOIN playbook_instructions i ON i.decision_id = t.decision_id
            WHERE t.decision_id LIKE 'universe_expansion%' AND t.direction=?
              AND i.fwd_return_t1 IS NOT NULL
        """, (d,))]
        if len(v) >= 3:
            fwd[d] = {"n": len(v), "mean_pct": round(sum(v) / len(v), 3), "t": _t(v)}
    if fwd:
        out["fwd_alpha_t"] = fwd
        out["fwd_note"] = ("两方向 |t|<2 ⇒ 皆零 alpha ⇒ 方向不是病灶; 实盘毛利一负一正 "
                           "而前瞻皆零 ⇒ 差异在退出侧 (DEB-20260819 已终裁不再改)。")

    # ── tier 覆盖率 (为何 tier×盈亏 不能裁) ──
    out["tier_coverage_note"] = (
        "tier 影子对实盘成交覆盖率仅 ~24% (影子 9/08 起, 成交回溯 7/14) ⇒ "
        "任何 tier×实盘盈亏 结论都是小样本, 不得据此立判据。")
    return out



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-days", type=int, default=MIN_DAYS)
    ap.add_argument("--min-n", type=int, default=MIN_N)
    args = ap.parse_args()

    days = _load_days()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    joined = []
    for day in days:
        for c in day.get("rows", []):
            sym = c.get("symbol") or ""
            full = sym if sym.startswith("US.") else f"US.{sym}"
            inst = conn.execute(
                "SELECT direction, fwd_return_t1, COALESCE(executed,0) AS executed "
                "FROM playbook_instructions WHERE date=? AND symbol=? "
                "AND decision_id LIKE 'universe_expansion%' LIMIT 1",
                (day.get("date"), full)).fetchone()
            joined.append({
                "date": day.get("date"), "symbol": full,
                "direction": (c.get("direction") or "").upper(),
                "tier": c.get("tier"), "would_block": bool(c.get("would_block")),
                "rule": c.get("rule"),
                "fwd": (inst["fwd_return_t1"] if inst else None),
                "executed": bool(inst["executed"]) if inst else False,
                "inst_dir": (inst["direction"] if inst else None),
            })
    # 🔵 通道级经济性备证 (只印不判) — 必须在 conn.close() 前取 (2026-09-26 用户裁建议1)
    try:
        chan_econ = _channel_economics(conn)
    except Exception as e:  # 备证失败不得拖垮门结算主链
        chan_econ = {"gating": False, "error": f"channel_economics 计算失败 (不影响 verdict): {e}"}
        print(f"[ue_tier_gate_eval] ⚠️ 通道经济性备证失败 (verdict 不受影响): {e}", file=sys.stderr)
    conn.close()

    n_days = len({d.get("date") for d in days})
    blocked = [r for r in joined if r["would_block"]]
    passed = [r for r in joined if not r["would_block"]]

    def _slice(rows, direction=None):
        sel = rows if direction is None else [r for r in rows if r["direction"] == direction]
        return _bucket_stats(sel)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_days": n_days,
        "n_candidates": len(joined),
        "prereg": (f"攒样≥{args.min_days}交易日 ∧ 被影子化桶 fwd n≥{args.min_n}; "
                   f"毕业=被拦桶 fwd 均值≤0 ∧ 放行桶均笔>0 → 用户裁决; "
                   f"撤闸认错=被拦桶 fwd>+{RETIRE_RET*100:.1f}% ∧ n≥{args.min_n}"),
        "buckets": {
            "ALL": {"blocked": _slice(blocked), "passed": _slice(passed)},
            "LONG": {"blocked": _slice(blocked, "LONG"), "passed": _slice(passed, "LONG")},
            "SHORT": {"blocked": _slice(blocked, "SHORT"), "passed": _slice(passed, "SHORT")},
        },
        "verdict": "KEEP_SAMPLING",
        # 🔵 只印不判 (gating=False): 通道级经济性备证, 不参与上面 verdict 的任何计算分支
        "channel_economics": chan_econ,
    }

    b_all, p_all = report["buckets"]["ALL"]["blocked"], report["buckets"]["ALL"]["passed"]
    days_ok = n_days >= args.min_days
    n_ok = b_all["n_fwd"] >= args.min_n
    if days_ok and n_ok:
        bm, pm = b_all["fwd_mean_pct"], p_all["fwd_mean_pct"]
        if bm is not None and bm > RETIRE_RET * 100:
            report["verdict"] = "RETIRE_GATE (撤闸认错: 被拦桶前瞻>+0.3%)"
        elif bm is not None and bm <= 0 and (pm or 0) > 0:
            report["verdict"] = "READY_TO_ADJUDICATE (毕业判据过线, 提请用户裁决; 方向分层并列见 buckets)"
        else:
            report["verdict"] = "KEEP_SAMPLING (判据未过线)"
    _atomic_write(OUT, json.dumps(report, ensure_ascii=False, indent=1))

    print(f"[ue_tier_gate_eval] days={n_days}/{args.min_days} candidates={len(joined)} "
          f"blocked={len(blocked)} → {OUT.name}")
    for k in ("ALL", "LONG", "SHORT"):
        bk, pk = report["buckets"][k]["blocked"], report["buckets"][k]["passed"]
        print(f"  {k:5s} 被拦: n_fwd={bk['n_fwd']:3d} fwd均值={bk['fwd_mean_pct']}% | "
              f"放行: n_fwd={pk['n_fwd']:3d} fwd均值={pk['fwd_mean_pct']}%")
    print(f"  verdict: {report['verdict']}")
    # 🔵 [只印不判] 通道级经济性备证 (不参与 verdict; 前缀防把披露读成裁决)
    ce = chan_econ
    if "all" in ce:
        a = ce["all"]
        print(f"  [只印不判] UE 通道经济性: n={a['n']} 毛利={a['gross']:+.2f} 费用={a['fee']:.2f} "
              f"净={a['net']:+.2f} | 毛利率={a['gross_bps']:+.2f}bps 费率={a['fee_bps']:.2f}bps "
              f"⇒ {'结构性无毛利来源' if (a['gross_bps'] or 0) < (a['fee_bps'] or 0) else '毛利可覆盖费用'}"
              f" | 净t={a['net_t']} ({'不显著⇒不立门' if a['net_t'] is not None and abs(a['net_t']) < 2 else '显著'})"
              f" | 费用归零则净={a['net_zero_fee']:+.2f}")
        for d, v in (ce.get("by_direction_disclose_only") or {}).items():
            print(f"  [只印不判]   {d:5s} n={v['n']:3d} 毛利率={v['gross_bps']:+7.2f}bps "
                  f"净={v['net']:+9.2f}  (方向并列披露, 禁据此设门)")
        for d, v in (ce.get("fwd_alpha_t") or {}).items():
            print(f"  [只印不判]   前瞻 {d:5s} n={v['n']:3d} 均={v['mean_pct']:+.3f}% "
                  f"t={v['t']:+.3f} ({'零alpha' if v['t'] is not None and abs(v['t']) < 2 else '有信号'})")
    elif "error" in ce:
        print(f"  [只印不判] 通道经济性备证不可用: {ce['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
