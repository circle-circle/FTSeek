#!/usr/bin/env python3
"""DA 横截面选择回放 (DEB-20261106 §43/§44, 用户裁「要」)。

问题：DA 逐只独立分析，永远回答不了"今天这 N 只里该做哪 3 只"。
§43 实测：同日按 |盘前跳空| 最小取 3 只，日内配对差 +$10.67/笔（t=2.73，安慰剂 99 分位）。
但那是我人工事后排序。本脚本问的是：**把候选池横向摊给模型看，它自己选的 3 只，值不值？**

做法（纯回放，零交易影响）：
1. 读 data/da_checkpoint/<date>/*.raw.json 的 collected.snapshot（盘前价格、跳空、量比、振幅……），
   加上当日 playbook_instructions 的真实方向；
2. 把当日全部标的拼成一张"候选池对比表"，一次性喂给模型（每日 1 次调用，不是每只 1 次）；
3. 要求它只回答 {"pick":[...3 个...], "why":"..."}；
4. 用 trade_records 的真实净额算：模型选的 3 只 vs 当日全体 vs 机械规则（|gap| 最小 3 只）。

判据（预注册）：
  日内配对差 > 0 ∧ 日聚类 t ≥ 2 ∧ 安慰剂分位 ≥ 95% ⇒ 模型选择有效
  只在这 23 个交易日（epoch 9–11），无法做时代一致性检验 —— 这一点必须写进结论。

用法:
  venv/bin/python scripts/da_crosssection_replay.py --dry            # 只打印候选池表，不调模型
  venv/bin/python scripts/da_crosssection_replay.py --model qwen3.7-max
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
CKPT = ROOT / "data" / "da_checkpoint"
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "shadow" / "da_crosssection_replay.jsonl"
URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

SYSTEM = """你是美股日内交易的组合层交易员。系统今天对一批标的做了逐只分析，每只都有独立结论。
你的任务不是重新分析个股，而是**横向比较**：如果今天资金和风控只允许你执行 3 只，你选哪 3 只。
判断只看这张表里的事实：盘前跳空、量比、振幅、换手、方向是否与跳空一致。
注意：跳空越大说明消息可能已被市场消化；你要判断的是"哪 3 只今天最可能靠 SL/BE/trail 走出利润"。
只输出 JSON: {"pick":["US.XXX","US.YYY","US.ZZZ"],"why":"不超过40字"}"""


def load_pool(day: str) -> List[dict]:
    rows = []
    for p in sorted(CKPT.glob(f"{day}/*.raw.json")):
        try:
            j = json.loads(p.read_text())
        except Exception:
            continue
        s = (j.get("collected") or {}).get("snapshot") or {}
        if not isinstance(s, dict) or not s:
            continue
        rows.append({
            "symbol": j.get("symbol"),
            "gap": s.get("pre_change_rate"),
            "overnight": s.get("overnight_change_rate"),
            "price": s.get("last_price"),
            "prev": s.get("prev_close"),
            "vr": s.get("volume_ratio"),
            "amp": s.get("amplitude"),
            "turn": s.get("turnover_rate"),
        })
    return rows


def load_directions() -> Dict[tuple, str]:
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    d = {}
    for r in conn.execute("""SELECT symbol, date, direction, decision_id FROM playbook_instructions
                             WHERE row_kind='instruction' AND date >= '2026-10-01'"""):
        d[(r["date"], r["symbol"])] = r["direction"]
    conn.close()
    return d


def load_outcomes() -> Dict[tuple, float]:
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    o = {}
    for r in conn.execute("""SELECT symbol, trade_date, pnl_net FROM trade_records
                             WHERE realized_pnl IS NOT NULL AND decision_id LIKE 'DA-%'"""):
        o[(r["trade_date"], r["symbol"])] = r["pnl_net"] or 0.0
    conn.close()
    return o


def fmt(v, nd=2, suf=""):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{nd}f}{suf}"
    except Exception:
        return "—"


def build_table(day: str, pool: List[dict], dirs: Dict[tuple, str]) -> str:
    lines = ["标的 | 盘前跳空% | 隔夜% | 量比 | 振幅% | 换手% | 现价 | DA方向"]
    for r in sorted(pool, key=lambda x: -abs(x["gap"] or 0)):
        dd = dirs.get((day, r["symbol"]), "无卡")
        dd = {"BUY": "做多", "SELL_SHORT": "做空", "SKIP": "跳过"}.get(dd, dd)
        lines.append(" | ".join([
            str(r["symbol"]), fmt(r["gap"]), fmt(r["overnight"]), fmt(r["vr"]),
            fmt(r["amp"]), fmt(r["turn"]), fmt(r["price"]), dd]))
    return "\n".join(lines)


def ask(model: str, table: str, n_pick: int = 3, timeout: int = 300) -> Optional[List[str]]:
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")
    user = (f"今日候选池（共 {len(table.splitlines()) - 1} 只）:\n{table}\n\n"
            f'注意: 只能从"DA方向"为做多或做空的标的里选（无卡=不出卡，不可选）。\n'
            f'如果只能执行 {n_pick} 只，选哪 {n_pick} 只？只回答 JSON。')
    body = {"model": model, "messages": [{"role": "system", "content": SYSTEM},
                                         {"role": "user", "content": user}],
            "temperature": 0.0, "max_tokens": 4000,
            "response_format": {"type": "json_object"}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        txt = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        obj = json.loads(txt)
        pick = obj.get("pick") or []
        return [str(x).strip() for x in pick][:n_pick]
    except Exception as e:
        print("  ⚠️ %s" % e)
        return None


def stats(diffs: List[float]) -> dict:
    if len(diffs) < 3:
        return {"mean": 0, "t": None}
    m, sd = statistics.mean(diffs), statistics.pstdev(diffs)
    return {"mean": m, "t": m / (sd / len(diffs) ** 0.5) if sd else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--pick", type=int, default=3)
    a = ap.parse_args()

    dirs = load_directions()
    outcomes = load_outcomes()
    days = sorted(p.name for p in CKPT.iterdir() if p.is_dir())

    records = []
    for day in days:
        pool = load_pool(day)
        if len(pool) < 4:
            continue
        table = build_table(day, pool, dirs)
        # 当日已成交的方向单（真实结局可查）
        traded = {r["symbol"]: outcomes[(day, r["symbol"])] for r in pool
                  if (day, r["symbol"]) in outcomes}
        if len(traded) < 4:
            continue
        if a.dry:
            print("=" * 20, day, f"（可评分 {len(traded)} 笔）")
            print(table)
            continue
        assert a.model
        pick = ask(a.model, table, a.pick)
        mech = [s for s in sorted(traded, key=lambda s: abs(next(x["gap"] for x in pool
                                                                if x["symbol"] == s) or 0))[:a.pick]]
        rec = {"day": day, "pool_n": len(pool), "traded_n": len(traded),
               "all_syms": sorted(traded), "pick": pick, "mech": mech,
               "net_pick": round(sum(traded[s] for s in (pick or []) if s in traded), 2),
               "net_mech": round(sum(traded[s] for s in mech if s in traded), 2),
               "net_all": round(sum(traded.values()), 2)}
        records.append(rec)
        print("%s 池%2d 成交%2d | 模型选 %s → $%+.2f | 机械|gap| %s → $%+.2f | 全体 $%+.2f" % (
            day, len(pool), len(traded), ",".join(s.replace("US.", "") for s in (pick or [])),
            rec["net_pick"], ",".join(s.replace("US.", "") for s in mech), rec["net_mech"], rec["net_all"]))

    if a.dry or not records:
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    def arm(key: str, lab: str):
        vals, diffs = [], []
        for r in records:
            day = r["day"]
            picks = [outcomes[(day, s)] for s in r[key] if (day, s) in outcomes]
            allv = [outcomes[(day, s)] for s in r["all_syms"] if (day, s) in outcomes]
            if not picks or len(allv) < 4:
                continue
            vals += picks
            diffs.append(statistics.mean(picks) - statistics.mean(allv))
        st = stats(diffs)
        print("%-16s 选中 %3d 笔 笔均 %+6.2f | 日内配对差 %+6.2f t=%s" % (
            lab, len(vals), statistics.mean(vals) if vals else 0, st["mean"],
            "%.2f" % st["t"] if st["t"] is not None else "n/a"))
        return st

    print("\n" + "=" * 60)
    arm("pick", "模型选3")
    arm("mech", "机械|gap|最小3")
    allv = [outcomes[(r["day"], s)] for r in records for s in r["all_syms"] if (r["day"], s) in outcomes]
    print("%-16s 全体 %3d 笔 笔均 %+6.2f" % ("基线", len(allv), statistics.mean(allv) if allv else 0))
    print("\n落盘: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())