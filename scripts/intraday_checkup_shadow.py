#!/usr/bin/env python3
"""开盘 15 分钟「体检单」影子 (DEB-20261106 §41 / 用户裁「要」)。

背景：§41 实测——开盘后 15 分钟的方向归一指标对最终盈亏 AUC 0.618（VWAP 关系）/0.607（区间位置），
日中心化后仍 0.605/0.564 ⇒ 个股级真信息；但**用它挑入场价提不到钱**（延后进 = -0.327%/笔 vs 无条件 -0.187%）。
所以本影子测的是**持仓决策**而非入场定价：15 分钟时决定 hold / cut。

四个臂（出场以外完全相同，成本已建立）：
  hold          基线：不动，用实际 pnl_net
  rule_pos40    固定规则：区间位置 < 0.40 → 15 分钟市价平
  rule_vwap     固定规则：价格在 VWAP 不利侧 → 15 分钟市价平
  llm           LLM 体检：模型读同一份快照，输出 {"verdict": "hold|cut"}，cut 则平

判定（预注册，读数前写死）：
  触发 ≥30 笔 ∧ 日聚类 |t| ≥ 2 ∧ 7-8 月与 9-11 月同号 ∧ 单日集中度 < 50% ⇒ 才提交执行链提案。
  任一不满足 = 不成立，登记后退役。

性质：只读 DB + bars；LLM 臂只发 API，不写 playbook、不下单。
用法:
  venv/bin/python scripts/intraday_checkup_shadow.py                     # 只跑固定规则（免费）
  venv/bin/python scripts/intraday_checkup_shadow.py --llm qwen3.7-max   # 加 LLM 臂
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
BARS_DIRS = (ROOT / "data" / "replay_bars", ROOT / "data" / "replay_bars_1m")
OUT = ROOT / "data" / "shadow" / "intraday_checkup_shadow.jsonl"
URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
T = 15  # 开盘后第 15 分钟

SYSTEM = """你是一名美股日内交易风控员。给你一笔已成交头寸在开盘后第15分钟的快照,
你要判断: 继续持有 (hold) 还是立刻平仓 (cut)。
判断依据只有: 价格相对VWAP的位置、价格在早盘区间中的位置、相对开盘的涨跌、当日振幅。
不要预测, 只判断当下证据是否还支持这个方向。只输出 JSON: {"verdict":"hold|cut","reason":"不超过20字"}"""


def bars_of(symbol: str, day: str) -> Optional[List[list]]:
    sym = symbol.replace("US.", "")
    for d in BARS_DIRS:
        for name in (f"US_{sym}_{day}.json", f"{sym}_{day}.json"):
            p = d / name
            if p.exists():
                return json.loads(p.read_text())
    return None


def load_trades() -> List[dict]:
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        """SELECT symbol, trade_date, direction, entry_price, quantity, pnl_net, mfe_pct,
                  fees_est, realized_pnl
           FROM trade_records
           WHERE trade_date >= '2026-07-01' AND realized_pnl IS NOT NULL
             AND decision_id LIKE 'DA-%'""")]
    conn.close()
    return rows


def snapshot(t: dict, bars: List[list]) -> Optional[dict]:
    if len(bars) < T + 5:
        return None
    sign = 1 if t["direction"] == "LONG" else -1
    seg = bars[:T + 1]
    px, o = seg[-1][4], bars[0][1]
    hi, lo = max(x[2] for x in seg), min(x[3] for x in seg)
    vwap = sum((x[2] + x[3] + x[4]) / 3 for x in seg) / len(seg)
    entry = t["entry_price"]
    return {
        "sign": sign, "px": px, "open": o, "hi": hi, "lo": lo, "vwap": vwap,
        "pos": (px - lo) / (hi - lo) if hi > lo else 0.5,
        "ret_bps": sign * (px / o - 1) * 1e4,
        "vwap_bps": sign * (px / vwap - 1) * 1e4,
        "rng_pct": (hi - lo) / o * 100,
        "float_pct": sign * (px / entry - 1) * 100,
        "cut_pnl": sign * (px - entry) * (t["quantity"] or 0),
    }


def llm_verdict(model: str, t: dict, s: dict, timeout: int = 120) -> Optional[str]:
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")
    user = (
        f"标的 {t['symbol']} 方向 {t['direction']} 入场价 {t['entry_price']:.2f} "
        f"股数 {t['quantity']}\n"
        f"开盘 {s['open']:.2f} | 早盘高 {s['hi']:.2f} 低 {s['lo']:.2f} | 现价 {s['px']:.2f}\n"
        f"VWAP {s['vwap']:.2f} | 现价相对VWAP {s['vwap_bps']:+.0f}bp | 相对开盘 {s['ret_bps']:+.0f}bp\n"
        f"早盘振幅 {s['rng_pct']:.2f}% | 区间位置 {s['pos']:.2f} | 当前浮动 {s['float_pct']:+.2f}%\n"
        f'只回答 JSON: {{"verdict":"hold|cut","reason":"..."}}'
    )
    body = {"model": model, "messages": [{"role": "system", "content": SYSTEM},
                                         {"role": "user", "content": user}],
            "temperature": 0.0, "max_tokens": 2000,
            "response_format": {"type": "json_object"}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        txt = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        obj = json.loads(txt)
        v = str(obj.get("verdict") or "").lower()
        return v if v in ("hold", "cut") else None
    except Exception:
        return None


def arm_stats(recs: List[dict], key: str) -> dict:
    """key 指向每笔的 arm 决策字段: 'hold' | 'pos40' | 'vwap' | 'llm'"""
    deltas, by_day = [], defaultdict(float)
    trig = 0
    for r in recs:
        fire = {"hold": False, "pos40": r["pos"] < 0.40, "vwap": r["vwap_bps"] < 0,
                "llm": r.get("llm") == "cut"}[key]
        d = (r["cut_pnl"] - r["net"]) if fire else 0.0
        if fire:
            trig += 1
        deltas.append(d)
        by_day[r["day"]] += d
    days = list(by_day.values())
    m, sd = (statistics.mean(days), statistics.pstdev(days)) if days else (0, 0)
    t = m / (sd / len(days) ** 0.5) if sd and len(days) > 2 else None
    tot = sum(deltas)
    era = {
        "7-8": sum(v for d, v in by_day.items() if d < "2026-09-01"),
        "9-11": sum(v for d, v in by_day.items() if d >= "2026-09-01"),
    }
    return {"arm": key, "n": len(recs), "triggered": trig, "delta_usd": round(tot, 2),
            "per_trade": round(tot / len(recs), 2) if recs else 0,
            "t_day": round(t, 2) if t is not None else None,
            "max_day_share_pct": round(100 * abs(max(days, key=abs)) / abs(tot), 0) if tot and days else None,
            "era_7_8": round(era["7-8"], 2), "era_9_11": round(era["9-11"], 2),
            "era_same_sign": (era["7-8"] > 0) == (era["9-11"] > 0),
            "gates": {
                "n>=30": trig >= 30,
                "|t|>=2": bool(t is not None and abs(t) >= 2),
                "era_same_sign": (era["7-8"] > 0) == (era["9-11"] > 0),
                "single_day<50%": bool(tot and abs(max(days, key=abs)) / abs(tot) < 0.5),
            }}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default=None, help="模型名，如 qwen3.7-max；留空则只跑固定规则")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    trades = load_trades()
    recs = []
    for t in trades:
        b = bars_of(t["symbol"], t["trade_date"])
        if not b:
            continue
        s = snapshot(t, b)
        if not s:
            continue
        recs.append({"day": t["trade_date"], "symbol": t["symbol"], "direction": t["direction"],
                     "entry": t["entry_price"], "qty": t["quantity"], "net": t["pnl_net"] or 0,
                     **s})
    print("样本 %d 笔（有 1min bar）" % len(recs))

    if a.llm:
        t0 = time.time()
        def _one(r):
            return r, llm_verdict(a.llm, {"symbol": r["symbol"], "direction": r["direction"],
                                          "entry_price": r["entry"], "quantity": r["qty"]}, r)
        with ThreadPoolExecutor(max_workers=4) as ex:
            for i, (r, v) in enumerate(ex.map(_one, recs), 1):
                r["llm"] = v
                if i % 50 == 0:
                    print("  LLM %d/%d (%.0fs)" % (i, len(recs), time.time() - t0))
        miss = sum(1 for r in recs if not r.get("llm"))
        print("LLM 返回有效 %d/%d（无效 %d）" % (len(recs) - miss, len(recs), miss))

    arms = ["pos40", "vwap"] + (["llm"] if a.llm else [])
    results = [arm_stats(recs, k) for k in arms]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as fh:
        for r in recs:
            fh.write(json.dumps({k: r[k] for k in ("day", "symbol", "direction", "pos", "vwap_bps",
                                                   "ret_bps", "rng_pct", "net", "cut_pnl")
                                 if k in r} | {"llm": r.get("llm")}, ensure_ascii=False) + "\n")

    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print("\n%-10s %5s %7s %10s %8s %7s %10s %10s %6s" % (
        "臂", "n", "触发", "Δ合计$", "每笔$", "日t", "7-8月$", "9-11月$", "单日%"))
    for r in results:
        print("%-10s %5d %7d %10.2f %8.2f %7s %10.0f %10.0f %6s" % (
            r["arm"], r["n"], r["triggered"], r["delta_usd"], r["per_trade"],
            r["t_day"] if r["t_day"] is not None else "n/a",
            r["era_7_8"], r["era_9_11"], r["max_day_share_pct"]))
    print("\n判据: 触发≥30 ∧ |日t|≥2 ∧ 双时代同号 ∧ 单日<50%")
    for r in results:
        g = r["gates"]
        ok = all(g.values())
        print("  %-8s %s %s" % (r["arm"], "✅ 过门" if ok else "❌ 未过",
                                {k: ("✔" if v else "✘") for k, v in g.items()}))
    print("\n落盘: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())