#!/usr/bin/env python3
"""判据②可复算脚本 (RFC-097 复现链入库, DEB-20260923 §8/§14.1 的复现层缺口补齐)。

用途: 在 Finnhub 财报日历窗口上复算**判据层**两个读数, 供 10 月财报季裁决时独立复核:
  ① 旧判据口径的负龄误拦占比 (calendar quarter/year → 归一化日历季度末, 按 asof 时点)
  ② 新判据口径的放行数 (epsActual 非空 ∧ epsEstimate 有效非零 ∧ 相位 ∈ {bmo, amc})

为什么只有判据层入库: 逐 tick 回放器 (入场/出场复刻) 依赖 futu 延长时段 K 线缓存, 体量与
限频成本高, 留在 scratch; 判据层是"口径正确性"的判据本身, 必须可被后续 Agent 独立复算,
否则本档数字对后来者是"采信链"而非"复算链" (DEB-20260923 §14.1)。

只读: 只用 Finnhub 公开日历端点; 不下单、不写生产目录 (--out 显式给路径才落盘)。
用法:
  venv/bin/python scripts/verify_earnings_criterion.py
  venv/bin/python scripts/verify_earnings_criterion.py --from 2026-08-28 --to 2026-09-22 --asof 2026-09-22
"""
from __future__ import print_function

import argparse
import json
import os
import sys
from datetime import datetime

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QEND = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}


def _api_key():
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(ROOT, ".env.us"), override=True)
            key = os.environ.get("FINNHUB_API_KEY")
        except Exception as exc:
            print("[verify] load_dotenv 失败: %s" % exc, file=sys.stderr)
    if not key:
        raise SystemExit("[verify] FINNHUB_API_KEY 缺失 (只核查存在性, 不打印值)")
    return key


def _qend(quarter, year):
    try:
        return datetime.strptime("%s-%s" % (int(year), QEND[int(quarter)]), "%Y-%m-%d").date()
    except (KeyError, TypeError, ValueError):
        return None


def _phase(row):
    hour = str(row.get("hour") or "").lower()
    return hour if hour in ("bmo", "amc") else None


def fetch_calendar(frm, to, key):
    resp = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                        params={"from": frm, "to": to, "token": key}, timeout=30)
    resp.raise_for_status()
    return (resp.json() or {}).get("earningsCalendar") or []


def _beat(row):
    """新判据②: 当日 actual vs est 打赢预期 (est 缺失/为 0/相位未标定 ⇒ 不计入放行)。"""
    est = row.get("epsEstimate")
    act = row.get("epsActual")
    if act is None or not _phase(row):
        return False
    if isinstance(est, bool) or not isinstance(est, (int, float)) or float(est) == 0:
        return False
    return (float(act) - float(est)) / abs(float(est)) * 100.0 > 0


def replay(rows, asof):
    disclosed = [r for r in rows if r.get("epsActual") is not None]
    dated = [(r, _qend(r.get("quarter"), r.get("year"))) for r in disclosed]
    dated = [(r, q) for r, q in dated if q is not None]
    ages = sorted((asof - q).days for _r, q in dated)
    negative = [a for a in ages if a < -7]
    eligible = [r for r in disclosed
                if isinstance(r.get("epsEstimate"), (int, float)) and r["epsEstimate"] != 0
                and _phase(r)]
    new_pass = [r for r in eligible if _beat(r)]
    return {
        "window": {"from": None, "to": None},
        "asof": str(asof),
        "calendar_rows": len(rows),
        "disclosed_rows": len(disclosed),
        "legacy_negative_age": {
            "n": len(ages), "blocked": len(negative),
            "share_pct": round(100.0 * len(negative) / len(ages), 1) if ages else None,
            "age_min": ages[0] if ages else None,
            "age_median": ages[len(ages) // 2] if ages else None,
            "age_max": ages[-1] if ages else None,
        },
        "new_criterion_eligible": len(eligible),
        "new_criterion_pass": len(new_pass),
        "new_criterion_symbols": sorted({str(r.get("symbol")) for r in new_pass}),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default="2026-08-28")
    ap.add_argument("--to", dest="to", default="2026-09-22")
    ap.add_argument("--asof", dest="asof", default=None, help="年龄快照日 (默认 = --to; 龄随日漂移, 必填口径)")
    ap.add_argument("--out", dest="out", default=None, help="可选: 结果 JSON 落盘路径")
    args = ap.parse_args()
    asof = datetime.strptime(args.asof or args.to, "%Y-%m-%d").date()
    rows = fetch_calendar(args.frm, args.to, _api_key())
    res = replay(rows, asof)
    res["window"] = {"from": args.frm, "to": args.to}
    print(json.dumps(res, ensure_ascii=False, indent=2)[:1600])
    if args.out:
        with open(args.out, "w") as handle:
            json.dump(res, handle, ensure_ascii=False, indent=2)
        print("[verify] 已落盘: %s" % args.out)


if __name__ == "__main__":
    main()