#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E1 GATE 1 结算与裁决 (DEB-20260815 §11.2 + §14.4.1 预注册口径, 冻结)
====================================================================
假设: LLM 选股层为负贡献, 绕开即改善。
两臂 (同一信号池 e1_shadow_signals.jsonl, 同一影子结算口径, 仅"选股"不同):
  臂P = 每交易日信号强度 top-3 (无 LLM 层; tie 按 symbol 升序取确定性序, 并列率披露)
  臂D = 当日实际成交匹配的指令子集 (与 trade_records(trade_mode=simulate)
        同 (trade_date, symbol, direction) 命中) — DA/UE 现役选股的对照臂

结算 (§14.4.1 预注册, 防"成交不了的单恰好方向最好"选择偏差):
  入场 = 信号次一交易日开盘价 (采集器已落盘 next_open_price)
  出场 = 当日收盘 (next_open_date 的日 bar close, replay_bars 缓存)
  费用 = app.execution.fee_model.estimate_round_trip (生产实盘费率)
       + SHORT 借券费: 随信号落盘 borrow_fee_annual_pct=5% (P1-D 保守下限);
       持仓=当日开当日平 → 按生产 accrue_borrow_fee 惯例 int(hold_days)=0 → $0
       (披露: 真实券商日内 SHORT 可能收最低借券费, 本口径对 SHORT 臂偏宽)
  $口径 = 臂D 笔均名义 (窗内实际成交 entry_price×quantity 均值) / 入场价 = 股数,
          两臂同名义, 费用非线性 ($1.99 最低佣金) 按该股数真实结算

裁决判据 (§11.2 冻结):
  成功 = 臂P 净均笔 > $0 且 (臂P−臂D) 净均笔差 ≥ $10 且 两臂 n≥30 且 双周同号
  失败 = 差 < $10/笔 或 双周异号;  臂 n<30 → "样本不足" (不排名不救活不延期, §14.4.3)
  双周 = W1 8/17-8/21, W2 8/24-8/28 (按信号 trade_date 分周, 差值符号逐周检验)

输出: data/e1_gate1_verdict.json + stdout 裁决表。PYTEST 守卫: 测试下无 --out 写临时文件。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

REPO = Path("/opt/ftseek/FTSeek-US")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from app.execution.fee_model import estimate_round_trip, accrue_borrow_fee  # noqa: E402

SHADOW_JSONL = REPO / "data" / "e1_shadow_signals.jsonl"
TRADES_DB = REPO / "data" / "ftseek_us.db"
OUT_JSON = REPO / "data" / "e1_gate1_verdict.json"

# 预注册窗口 (§11.3 时间轴: W1-W2 攒样 8/17-8/28, GATE 1 = 8/28 盘后) — 冻结
WINDOW = ("2026-08-17", "2026-08-28")
WEEK_SPLIT = {"W1": ("2026-08-17", "2026-08-21"), "W2": ("2026-08-24", "2026-08-28")}
MIN_N = 30
DIFF_GATE = 10.0  # $/笔


def load_shadow(path):
    recs = []
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            logger.warning("e1_shadow 坏行跳过: %s", line[:120])
            continue
        if r.get("next_open_price") and r.get("next_open_date"):
            recs.append(r)
    return recs


def load_close_map(recs, cache_dir=None):
    """next_open_date 的日 bar close。replay_bars 缓存只读; 缺失记 missing (不现拉,
    结算是离线确定性操作 — 缓存由采集器维护)。"""
    cache_dir = Path(cache_dir) if cache_dir else REPO / "data" / "replay_bars"
    bars_cache: dict[str, dict] = {}
    close = {}
    missing = []
    for r in recs:
        sym, d = r["symbol"], r["next_open_date"]
        if sym not in bars_cache:
            days = {}
            for p in cache_dir.glob(f"daily_{sym.replace('.', '_')}_*.json"):
                try:
                    for b in json.loads(p.read_text()):
                        days[b[0]] = b
                except Exception:
                    logger.warning("replay_bars 缓存坏文件跳过: %s", p)
                    continue
            bars_cache[sym] = days
        b = bars_cache[sym].get(d)
        if b:
            close[(sym, d)] = b[4]  # close
        else:
            missing.append((sym, d))
    return close, missing


def load_actual_trades(db_path, lo, hi):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        """SELECT trade_date, symbol, direction, entry_price*quantity
           FROM trade_records
           WHERE trade_mode='simulate' AND trade_date BETWEEN ? AND ?""",
        (lo, hi)).fetchall()
    conn.close()
    keys = {(d, s, di) for d, s, di, _ in rows}
    notionals = [n for *_, n in rows if n]
    return keys, notionals


def settle_one(r, close_px, notional):
    """单笔影子结算 → $ 净额。qty = 臂D笔均名义/入场价。"""
    entry = r["next_open_price"]
    qty = notional / entry
    sign = 1.0 if r["direction"] == "LONG" else -1.0
    gross = qty * (close_px - entry) * sign
    fees = estimate_round_trip(qty, entry, close_px, r["direction"],
                               trade_date=r["next_open_date"])["total"]
    borrow = accrue_borrow_fee(qty, entry, r.get("borrow_fee_annual_pct", 0.0),
                               hold_days=0)  # 当日开平, 生产惯例=0 (见 docstring 披露)
    return round(gross - fees - borrow, 4)


def build_arms(recs, trade_keys, notional, close_map):
    """返回 (armP, armD) 各自结算行列表 + tie 披露。"""
    by_day = defaultdict(list)
    for r in recs:
        by_day[r["trade_date"]].append(r)
    arm_p, tie_days = [], 0
    for day, rs in sorted(by_day.items()):
        ranked = sorted(rs, key=lambda x: (-x["strength"], x["symbol"], x["direction"]))
        arm_p.extend(ranked[:3])
        if len(ranked) > 3 and ranked[3]["strength"] == ranked[2]["strength"]:
            tie_days += 1  # 第3/4名并列 → top-3 选择含任意性, 披露
    arm_d = [r for r in recs
             if (r["trade_date"], r["symbol"], r["direction"]) in trade_keys]

    def settle(rows):
        out = []
        for r in rows:
            c = close_map.get((r["symbol"], r["next_open_date"]))
            if c is None:
                continue
            out.append({"symbol": r["symbol"], "trade_date": r["trade_date"],
                        "direction": r["direction"], "strength": r["strength"],
                        "net_usd": settle_one(r, c, notional),
                        "week": "W1" if r["trade_date"] <= WEEK_SPLIT["W1"][1] else "W2"})
        return out

    return settle(arm_p), settle(arm_d), tie_days


def _mean(xs):
    return round(sum(xs) / len(xs), 2) if xs else None


def adjudicate(arm_p, arm_d):
    """§11.2 冻结判据。"""
    n_p, n_d = len(arm_p), len(arm_d)
    res = {"n_p": n_p, "n_d": n_d,
           "p_mean": _mean([x["net_usd"] for x in arm_p]),
           "d_mean": _mean([x["net_usd"] for x in arm_d])}
    if n_p < MIN_N or n_d < MIN_N:
        res["verdict"] = "INSUFFICIENT_SAMPLE"
        res["reason"] = f"n<30 (P={n_p}, D={n_d}) → 不排名不救活不延期 (§14.4.3)"
        return res
    res["diff"] = round(res["p_mean"] - res["d_mean"], 2)
    wk = {}
    for w in ("W1", "W2"):
        pw = [x["net_usd"] for x in arm_p if x["week"] == w]
        dw = [x["net_usd"] for x in arm_d if x["week"] == w]
        wk[w] = {"n_p": len(pw), "n_d": len(dw),
                 "diff": round(_mean(pw) - _mean(dw), 2) if pw and dw else None}
    res["weekly"] = wk
    same_sign = (wk["W1"]["diff"] is not None and wk["W2"]["diff"] is not None
                 and (wk["W1"]["diff"] > 0) == (wk["W2"]["diff"] > 0))
    res["weekly_same_sign"] = same_sign
    ok = (res["p_mean"] > 0 and res["diff"] >= DIFF_GATE and same_sign
          and wk["W1"]["diff"] > 0)
    res["verdict"] = "E1_SUCCESS" if ok else "E1_FAIL"
    res["reason"] = (
        f"P均笔${res['p_mean']} vs D均笔${res['d_mean']}, 差${res['diff']} "
        f"(门≥${DIFF_GATE}); 双周差 W1={wk['W1']['diff']} W2={wk['W2']['diff']} "
        f"({'同号' if same_sign else '异号'})")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="E1 GATE 1 结算裁决 (§11.2/§14.4.1 冻结口径)")
    ap.add_argument("--shadow", default=str(SHADOW_JSONL))
    ap.add_argument("--db", default=str(TRADES_DB))
    ap.add_argument("--out", default=str(OUT_JSON))
    ap.add_argument("--window", default=f"{WINDOW[0]}:{WINDOW[1]}",
                    help="预注册窗; 扩展窗须显式传并标 non_preregistered")
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args(argv)

    lo, hi = args.window.split(":")
    pre_reg = (lo, hi) == WINDOW

    recs = [r for r in load_shadow(args.shadow) if lo <= r["trade_date"] <= hi]
    close_map, missing = load_close_map(recs, args.cache_dir)
    trade_keys, notionals = load_actual_trades(args.db, lo, hi)
    notional = round(sum(notionals) / len(notionals), 2)
    arm_p, arm_d, tie_days = build_arms(recs, trade_keys, notional, close_map)
    res = adjudicate(arm_p, arm_d)
    res.update({
        "window": [lo, hi], "pre_registered_window": pre_reg,
        "arm_d_avg_notional": notional,
        "signals_filled": len(recs), "bar_missing": len(missing),
        "tie_days_top3": tie_days, "days": len({r["trade_date"] for r in recs}),
        "fee_model": "app.execution.fee_model.estimate_round_trip (生产费率)",
        "borrow_fee": "SHORT 5%/年 × 0 天 (当日开平, 生产 accrue 惯例 int(hold)=0)",
    })

    out = Path(args.out)
    if os.environ.get("PYTEST_CURRENT_TEST") and args.out == str(OUT_JSON):
        out = Path(tempfile.mktemp(suffix=".json"))  # PYTEST 守卫: 不写生产 JSON
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    tag = "预注册窗" if pre_reg else "⚠️ 非预注册扩展窗 (仅参考)"
    print(f"[E1 GATE 1] {tag} {lo}..{hi} | 臂D笔均名义 ${notional}")
    print(f"臂P(top-3强度) n={res['n_p']} 净均笔=${res['p_mean']} | "
          f"臂D(实际成交) n={res['n_d']} 净均笔=${res['d_mean']}")
    if res["verdict"] != "INSUFFICIENT_SAMPLE":
        print(f"差=${res['diff']}/笔 (门≥$10) | 双周: "
          + " ".join(f"{w} Δ=${res['weekly'][w]['diff']}" for w in ("W1", "W2"))
          + f" ({'同号' if res['weekly_same_sign'] else '异号'})")
    print(f"裁决: {res['verdict']} — {res['reason']}")
    print(f"(披露: top-3 并列任意性日 {tie_days}/{res['days']}; bar 缺失 {len(missing)}; "
          f"SHORT 借券费按 0 天计) → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
