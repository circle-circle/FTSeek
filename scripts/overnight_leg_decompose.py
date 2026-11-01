#!/usr/bin/env python3
"""跨日单的两腿分解 + 隔夜方案证伪器 (DEB-20261031-overnight-exposure).

把跨日持仓拆成两段:
  日内腿 = 入场 → 当日收盘 (Futu 日线 close 实算, 不估算)
  隔夜腿 = 当日收盘 → 实际出场价
然后检验四个候选方案:
  S1 完全禁隔夜 / S2 隔夜暴露减半 / S3 只留浮盈仓过夜 / S4 (由 exit_param 系列覆盖, 此处不重复)

预注册判据 (读数前写死):
  G1 方案净额 > 现状净额
  G2 逐笔一致性 ≥ 55% (改善笔 / (改善+变差))
  G3 改善幅度 > 单边 $150 (4 个月口径下不被单笔噪声驱动)
  G1∧G2∧G3 ⇒ CANDIDATE (须另立 DEB 才可上线); 否则 ⇒ FALSIFIED

只读: 不写库, 不改生产。需要 Futu OpenD 在线 (127.0.0.1:11111)。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "shadow" / "overnight_leg_latest.json"
SINCE = "2026-07-01"

# 预注册门 (写死, 读数后不许改)
MIN_CONSISTENCY = 0.55
MIN_ABS_IMPROVE = 150.0


def load_trades():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT entry_time, exit_time, symbol, direction, entry_price, exit_price,
               quantity, pnl_net, exit_reason, metadata_json
        FROM trade_records
        WHERE COALESCE(exit_price,0)>0 AND trade_date>=?
          AND COALESCE(json_extract(metadata_json,'$.gate_mode'),'')!='research_momentum'
          AND entry_time IS NOT NULL AND exit_time IS NOT NULL
    """, (SINCE,)).fetchall()
    same, overnight = [], []
    for r in rows:
        tgt = overnight if str(r["entry_time"])[:10] != str(r["exit_time"])[:10] else same
        tgt.append(r)
    return same, overnight


def fetch_closes(symbols):
    from futu import AuType, KLType, OpenQuoteContext

    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    out, fail = {}, []
    try:
        for sym in sorted(symbols):
            try:
                res = ctx.request_history_kline(sym, start=SINCE, end="2026-11-01",
                                                ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=200)
                ret, df = res[0], res[1]
                if ret == 0 and hasattr(df, "columns"):
                    out[sym] = {str(k)[:10]: float(v) for k, v in zip(df["time_key"], df["close"])}
                else:
                    fail.append((sym, ret))
            except Exception as exc:  # noqa: BLE001 - 逐个隔离, 不让单只票阻断全量
                fail.append((sym, f"{type(exc).__name__}:{exc}"))
            time.sleep(1.2)      # OpenD 30 次/30 秒限流
    finally:
        ctx.close()
    return out, fail


def decompose(overnight, closes):
    out = []
    for r in overnight:
        d = str(r["entry_time"])[:10]
        cl = closes.get(r["symbol"], {}).get(d)
        if not cl:
            out.append({"sym": r["symbol"], "d": d, "ok": False, "actual": r["pnl_net"] or 0})
            continue
        ep, qty = float(r["entry_price"]), float(r["quantity"])
        nom = ep * qty
        fee = 3.98 + nom * 0.000042
        long_ = str(r["direction"]).upper().startswith(("BUY", "LONG"))
        pct = (cl - ep) / ep if long_ else (ep - cl) / ep
        intraday = pct * nom - fee              # 当日收盘平可实现
        actual = r["pnl_net"] or 0
        out.append({"sym": r["symbol"], "d": d, "ok": True, "why": r["exit_reason"],
                    "nom": nom, "actual": actual, "intraday": intraday,
                    "overnight": actual - intraday, "close_pct": pct * 100})
    return out


def evaluate(rows, intra, over):
    """四个方案的逐笔结果与判据。"""
    n = len(rows)
    base = sum(x["actual"] for x in rows)
    # S1 完全禁隔夜: 全部按当日收盘平
    s1 = [x["intraday"] for x in rows]
    # S2 隔夜暴露减半: 日内腿 + 0.5×隔夜腿, 另付半仓平仓佣金
    s2 = [x["intraday"] + 0.5 * x["overnight"] - 1.99 for x in rows]
    # S3 只让浮盈仓过夜
    s3 = [x["actual"] if x["intraday"] > 0 else x["intraday"] for x in rows]

    def pack(name, vals):
        net = sum(vals)
        imp = sum(1 for x, v in zip(rows, vals) if v > x["actual"] + 0.01)
        wor = sum(1 for x, v in zip(rows, vals) if v < x["actual"] - 0.01)
        cons = imp / max(imp + wor, 1)
        g1 = net > base
        g2 = cons >= MIN_CONSISTENCY
        g3 = (net - base) > MIN_ABS_IMPROVE
        state = "CANDIDATE" if (g1 and g2 and g3) else "FALSIFIED"
        return {"scheme": name, "net": net, "delta": net - base, "improved": imp,
                "worsened": wor, "consistency": cons,
                "checks": {"G1_net_up": g1, "G2_consistency": g2, "G3_material": g3},
                "state": state}

    return {
        "n_overnight": n,
        "baseline_net": base,
        "intraday_leg": sum(intra),
        "overnight_leg": sum(over),
        "schemes": [pack("S1_ban_overnight", s1),
                    pack("S2_half_overnight", s2),
                    pack("S3_profit_only_overnight", s3)],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    same, ov = load_trades()
    print(f"当日平 {len(same)} 笔 净 ${sum(r['pnl_net'] or 0 for r in same):+,.0f}")
    print(f"跨日持 {len(ov)} 笔 净 ${sum(r['pnl_net'] or 0 for r in ov):+,.0f} "
          f"({len({r['symbol'] for r in ov})} 个标的)")

    closes, fail = fetch_closes({r["symbol"] for r in ov})
    if fail:
        print(f"[warn] {len(fail)} 只拉取失败 (首个: {fail[0]})", file=sys.stderr)
    rows = [x for x in decompose(ov, closes) if x["ok"]]
    if len(rows) < len(ov):
        print(f"[warn] 只重算 {len(rows)}/{len(ov)} 笔, 其余无日线")

    res = evaluate(rows, [x["intraday"] for x in rows], [x["overnight"] for x in rows])
    res["same_day_net"] = sum(r["pnl_net"] or 0 for r in same)
    res["system_net"] = res["same_day_net"] + res["baseline_net"]

    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({**res, "rows": rows}, ensure_ascii=False, indent=2))
        print(f"产物: {OUT.relative_to(REPO)}")
    except OSError as exc:
        print(f"[warn] 写产物失败: {exc}", file=sys.stderr)

    print(f"\n跨日单两腿: 日内腿 ${res['intraday_leg']:+,.0f} | 隔夜腿 ${res['overnight_leg']:+,.0f} "
          f"| 实际 ${res['baseline_net']:+,.0f}")
    print(f"全系统: 当日平 ${res['same_day_net']:+,.0f} + 跨日 ${res['baseline_net']:+,.0f} "
          f"= ${res['system_net']:+,.0f}")
    print(f"\n{'方案':26s} {'净额':>9s} {'Δ':>8s} {'改善':>5s} {'变差':>5s} {'一致性':>7s}  判定")
    for s in res["schemes"]:
        print(f"{s['scheme']:26s} {s['net']:9,.0f} {s['delta']:+8,.0f} {s['improved']:5d} "
              f"{s['worsened']:5d} {s['consistency']*100:6.1f}%  {s['state']}")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())