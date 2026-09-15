#!/usr/bin/env python3
"""B门(接飞刀硬门)拦截质量周报 (2026-09-15, 用户裁「都做」批①, record-only).

问题: B门 (RFC-068, premarket_invalidation_hard_gate) 8/29 转正后只有一次性反事实
验证 (拦4省$305), 无持续追踪。9/14 实锤单日误杀 $333 (BE/SKHY/NOK 全天飞刀区被拦,
收盘 shadow_pnl 全正) — 门的保护价值与误杀成本必须持续记账, 攒够样本才有资格
裁决调门, 否则单日噪声就会驱动改参数。

数据源:
  - data/shadow/exec_gate_audit*.jsonl action=skipped_knife_gate (轮询级, 必须按
    (date, inst_id) 去重 — 9/14 实测 83 事件 = 3 指令, 28x 放大)
  - playbook_instructions JOIN: fwd_return_t1_open (裁决基准, 开盘价可成交口径,
    T+1 回填所以最近 1-2 天恒缺) / shadow_pnl (诊断列, anchor 纸面口径禁止裁决引用)

口径纪律 (继承 gate_block_counterfactual):
  - 裁决基准 = fwd_return_t1_open 方向调整后 (BUY=+f1o, 语义已是方向化收益则直用)
  - 费用: mean 与 mean−fee_drag 两列 (默认 0.0018)
  - n < min_n (默认 10) 打 small_sample 旗标, 数值照算仅供观察
  - 已执行 (executed=1, 回升复评后成交) 的指令不算"拦截", 单列 recovered

只读, 零落库变更。输出: 控制台 + data/knife_gate_weekly_latest.json
推送: opt-in (--push), 手动重跑默认静默。
用法: ./venv/bin/python scripts/knife_gate_weekly_report.py [--push] [--days 7]
回滚: 删本文件 + 删 crontab 行 (CRON_MARKER: KNIFE-GATE-WEEKLY) + 删台账行。
"""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "knife_gate_weekly_latest.json"
AUDIT_GLOB = str(ROOT / "data" / "shadow" / "exec_gate_audit*.jsonl")
FEE_DRAG = 0.0018


def load_blocked(since: str) -> dict:
    """jsonl 去重: (date, inst_id) → 首条事件 (chg_pct 取最深一轮, 保守口径)。"""
    blocked = {}
    for f in glob.glob(AUDIT_GLOB):
        for line in open(f, encoding="utf-8"):
            if "skipped_knife_gate" not in line:
                continue
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if r.get("action") != "skipped_knife_gate":
                continue
            d = str(r.get("date") or "")[:10]
            if d < since:
                continue
            iid = r.get("inst_id")
            if not iid:
                continue
            key = (d, iid)
            chg = r.get("chg_pct")
            prev = blocked.get(key)
            if prev is None:
                blocked[key] = {"date": d, "inst_id": iid, "symbol": r.get("symbol"),
                                "rounds": 1, "deepest_chg": chg,
                                "confidence": r.get("confidence"),
                                "source": r.get("source")}
            else:
                prev["rounds"] += 1
                if chg is not None and (prev["deepest_chg"] is None
                                        or chg < prev["deepest_chg"]):
                    prev["deepest_chg"] = chg
    return blocked


def enrich(blocked: dict) -> list:
    """JOIN playbook_instructions 拿前瞻收益 + shadow_pnl (诊断列)。"""
    if not blocked:
        return []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    ids = sorted({v["inst_id"] for v in blocked.values()})
    q = ",".join(str(i) for i in ids)
    rows = {r["id"]: r for r in con.execute(
        f"SELECT id, symbol, date, direction, executed, entry_price, position_size,"
        f" fwd_return_t1_open, shadow_pnl FROM playbook_instructions WHERE id IN ({q})")}
    con.close()
    out = []
    for v in blocked.values():
        r = rows.get(v["inst_id"])
        if r is None:
            continue
        rec = dict(v)
        rec["direction"] = r["direction"]
        rec["executed"] = bool(r["executed"])
        rec["entry_price"] = r["entry_price"]
        rec["position_size"] = r["position_size"]
        rec["fwd_t1_open"] = r["fwd_return_t1_open"]   # 裁决基准 (None=未回填)
        rec["shadow_pnl"] = r["shadow_pnl"]            # 诊断列, 禁止裁决引用
        out.append(rec)
    return out


def summarize(recs: list) -> dict:
    n = len(recs)
    if not n:
        return {"n": 0}
    blocked_recs = [r for r in recs if not r["executed"]]   # executed=回升复评后成交
    f1o = [r["fwd_t1_open"] for r in blocked_recs if r["fwd_t1_open"] is not None]
    spnl = [r["shadow_pnl"] for r in blocked_recs if r["shadow_pnl"] is not None]
    misskill = sum(1 for x in f1o if x > 0)
    mean_f1o = sum(f1o) / len(f1o) if f1o else None
    return {
        "n_blocked_inst": len(blocked_recs),
        "n_recovered_executed": n - len(blocked_recs),
        "n_poll_events": sum(r["rounds"] for r in recs),
        "n_f1o_settled": len(f1o),
        "n_f1o_pending": len(blocked_recs) - len(f1o),
        "mean_f1o": round(mean_f1o, 6) if mean_f1o is not None else None,
        "mean_f1o_net": (round(mean_f1o - FEE_DRAG, 6)
                         if mean_f1o is not None else None),
        "misskill_n": misskill,
        "misskill_rate": round(misskill / len(f1o), 3) if f1o else None,
        "sum_shadow_pnl_diag": round(sum(spnl), 2) if spnl else None,
        "small_sample_flag": len(f1o) < 10,
        "per_inst": [{k: r[k] for k in
                      ("date", "symbol", "direction", "rounds", "deepest_chg",
                       "fwd_t1_open", "shadow_pnl", "executed")}
                     for r in sorted(recs, key=lambda x: (x["date"], x["inst_id"]))],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    since = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    blocked = load_blocked(since)
    recs = enrich(blocked)
    s = summarize(recs)
    week = datetime.now().strftime("%Y-W%W")
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "window_since": since, "week": week, **s}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"═══ B门(接飞刀硬门)拦截质量周报 {since} ~ (week {week}) ═══")
    if not s.get("n_blocked_inst") and not s.get("n_recovered_executed"):
        print("窗口内无拦截 → 门未触发 (飞刀日才会拦, 空窗正常)")
        return 0
    print(f"拦截指令: {s['n_blocked_inst']} 个 (轮询事件 {s['n_poll_events']} 次, 已去重)")
    print(f"回升复评后成交: {s['n_recovered_executed']} 个 (不算拦截)")
    if s["n_f1o_settled"]:
        print(f"裁决基准 fwd_t1_open: n={s['n_f1o_settled']} 均值={s['mean_f1o']:+.4%} "
              f"扣费={s['mean_f1o_net']:+.4%} 误杀率={s['misskill_rate']:.0%}"
              f"{'  ⚠️small_sample' if s['small_sample_flag'] else ''}")
        print(f"  解读: 均值<0 = 门在避坑 (拦对了); >0 = 门在杀机会 (误杀)")
    else:
        print("裁决基准 fwd_t1_open: 全部未回填 (T+1 回填滞后, 最近1-2天恒缺)")
    print(f"诊断列 Σshadow_pnl(anchor纸面口径, 禁止裁决引用): "
          f"{s['sum_shadow_pnl_diag']}")
    print(f"产物: {OUT}")

    if args.push:
        try:
            from app.push import get_push_hub, PushMessage, Level, Category
            lines = [f"拦截 {s['n_blocked_inst']} 指令 / {s['n_poll_events']} 轮询事件"
                     f" / 复评成交 {s['n_recovered_executed']}"]
            if s["n_f1o_settled"]:
                lines.append(f"fwd_t1_open n={s['n_f1o_settled']} "
                             f"均值{s['mean_f1o']:+.3%} 误杀率{s['misskill_rate']:.0%}"
                             f"{' (small_sample)' if s['small_sample_flag'] else ''}")
            else:
                lines.append("fwd_t1_open 未回填 (T+1 滞后)")
            lines.append(f"诊断 Σshadow={s['sum_shadow_pnl_diag']}")
            ok = get_push_hub().push(PushMessage(
                title=f"🔪 B门拦截质量周报 ({since}~)",
                body="\n".join(lines),
                level=Level.NORMAL,
                category=Category.REVIEW_WEEKLY,
                dedup_key=f"knife_gate_weekly:{week}",
            ), sync=True)
            print(f"[knife-gate-weekly] 推送{'成功' if ok else '失败'}")
        except Exception as ex:
            print(f"[knife-gate-weekly] 推送失败 (显性不静默): {ex}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
