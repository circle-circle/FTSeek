#!/usr/bin/env python3
"""VIX 口径审计 (只读): 代理 vs 现货, 以及"换口径后硬门会拦掉什么"。

背景 (DEB-20261106 §22.3 / §二十三):
同一周里两个"VIX"同时存在且横跨硬拒阈值 28 ——
  11-06 08:23 ET  DA brief / VIXGuard 读到 18.04  (`VIXY last_price 21.22 × 0.85`)
  11-06 23:59 ET  vix_cache / 看板读到    28.18  (FRED VIXCLS 11-05 收盘)
本脚本把"这是标定问题还是新鲜度问题"拆成三个可复跑的读数:

  A. 新鲜度: 盘前快照里的 `vixy_price` 是否等于**上一交易日收盘**
     (Futu `last_price` 在盘前停留在上一时段收盘 —— 若 21/21 全等, 则"盘前实时代理"名不副实)
  B. 标定: 同一收盘时刻 `VIX spot / (0.85 × VIXY close)` 的分布与逐月漂移
     (常数系数只有在比值稳定时才是换算; 实测按月从 0.65 漂到 1.35)
  C. 代价: 若把门的判据从"代理>28"换成"现货>28", A10 窗口内会新增拦截哪些交易日 / 那些日的净额

纪律: 只读; 不改任何阈值、不写库、不触发交易。回滚 = 删本文件。
外部依赖: FRED CSV (VIXCLS) + Yahoo chart (VIXY 日线); 均在函数内带超时与失败留痕。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import statistics
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROXY_FACTOR = 0.85          # app/data/vix_source.py::VIXY_CONTANGO_FACTOR
BLOCK_THRESHOLD = 28.0       # app/risk/vix_guard.py::VIXGuard.BLOCK_THRESHOLD
UA = {"User-Agent": "Mozilla/5.0 (FTSeek vix_caliber_audit)"}


def _get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (固定 https 白名单地址)
        return resp.read().decode("utf-8", "replace")


def fetch_vixy_daily(range_: str = "1y") -> Dict[str, float]:
    """VIXY 日收盘 (Yahoo chart API)。失败抛异常, 不兜底假值。"""
    raw = _get("https://query1.finance.yahoo.com/v8/finance/chart/VIXY?range=%s&interval=1d" % range_)
    result = json.loads(raw)["chart"]["result"][0]
    closes = result["indicators"]["quote"][0]["close"]
    out: Dict[str, float] = {}
    for ts, close in zip(result["timestamp"], closes):
        if close is None:
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        out[day] = float(close)
    return out


def fetch_vix_spot(start: str, end: str) -> Dict[str, float]:
    """VIX 现货日收盘: Yahoo ^VIX 优先, FRED VIXCLS 兜底 (两者逐日同值, 2026-11-07 实测)。

    Yahoo 先行的原因: FRED CSV 在本机 venv 出网时常超时 (实测 20s timeout), 而基座没有代理;
    两个源同日收盘值逐字一致 (11-02..11-05 = 21.15/20.83/16.61/28.18), 故可互换。
    失败时抛出, 不兜底假值。
    """
    try:
        raw = _get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=1y&interval=1d")
        result = json.loads(raw)["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        out: Dict[str, float] = {}
        for ts, close in zip(result["timestamp"], closes):
            if close is None:
                continue
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if start <= day <= end:
                out[day] = float(close)
        if out:
            return out
    except Exception as _e:  # 换源必须留痕, 不静默
        print("[vix_caliber_audit] ^VIX 取数失败, 回落 FRED: %s" % _e, file=sys.stderr)
    raw = _get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS&cosd=%s&coed=%s" % (start, end))
    out = {}
    for row in list(csv.reader(io.StringIO(raw)))[1:]:
        if len(row) >= 2 and row[1] not in ("", "."):
            out[row[0]] = float(row[1])
    return out


# ── 纯函数 (可测) ────────────────────────────────────────────────────────────

def ratio_stats(ratios: Sequence[float]) -> dict:
    if not ratios:
        return {}
    ordered = sorted(ratios)
    n = len(ordered)

    def pct(q: float) -> float:
        return round(ordered[min(n - 1, max(0, int(q * n)))], 3)

    return {
        "n": n,
        "median": round(statistics.median(ordered), 3),
        "p10": pct(0.10),
        "p90": pct(0.90),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
    }


def monthly_median_ratios(spot: Dict[str, float], vixy: Dict[str, float], factor: float) -> List[Tuple[str, int, float]]:
    buckets: Dict[str, List[float]] = defaultdict(list)
    for day in sorted(set(spot) & set(vixy)):
        buckets[day[:7]].append(spot[day] / (factor * vixy[day]))
    return [(month, len(vals), round(statistics.median(vals), 2)) for month, vals in sorted(buckets.items())]


def gate_impact(
    day_rows: Iterable[dict],
    spot: Dict[str, float],
    threshold: float,
) -> List[dict]:
    """把"日"按 grid 判据重算: 用该日**前一交易日**的现货收盘是否 > threshold。

    day_rows: [{"trade_date": "...", "n": int, "net": float}]
    返回被该判据拦下的交易日 (含当日净额), 供"换口径代价"核算。
    """
    hit: List[dict] = []
    for row in day_rows:
        day = row["trade_date"]
        prior = [k for k in sorted(spot) if k < day]
        if not prior:
            continue
        spot_prev = spot[prior[-1]]
        if spot_prev > threshold:
            hit.append({
                "trade_date": day,
                "spot_prev_close": spot_prev,
                "entries": int(row.get("n") or 0),
                "net": round(float(row.get("net") or 0.0), 2),
            })
    return hit


def premarket_freshness(checkpoint_root: Path, vixy: Dict[str, float]) -> List[dict]:
    """盘前快照的 vixy_price 与"上一交易日收盘"的对照。

    **按日聚合全部 raw 文件** (不是只取第一个): 个别标的的 raw 会缺 market_context.vix 子字段,
    单文件采样会把"该日缺字段"与"该日值不同"混为一谈 (2026-11-07 实测: 取第一个文件时
    11-02 记 None, 换个标的却有值)。
    """
    rows: List[dict] = []
    for day_dir in sorted(checkpoint_root.glob("*")):
        if not day_dir.is_dir():
            continue
        raws = sorted(day_dir.glob("US.*.raw.json"))
        if not raws:
            continue
        prices: List[float] = []
        for path in raws:
            try:
                payload = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                print(f"[vix_caliber_audit] raw 读取失败跳过 {path}: {type(exc).__name__}", file=sys.stderr)
                continue
            vix = ((payload.get("collected") or {}).get("market_context") or {}).get("vix") or {}
            price = vix.get("vixy_price")
            if price:
                prices.append(round(float(price), 2))
        day = day_dir.name
        prior = [k for k in sorted(vixy) if k < day]
        prev_close = vixy[prior[-1]] if prior else None
        distinct = sorted(set(prices))
        rows.append({
            "day": day,
            "files": len(raws),
            "files_with_value": len(prices),
            "distinct_prices": distinct,
            "prev_close": round(prev_close, 2) if prev_close is not None else None,
            "all_match_prev_close": bool(distinct and prev_close is not None
                                         and all(abs(p - round(prev_close, 2)) < 0.02 for p in distinct)),
        })
    return rows


# ── 主流程 ──────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="VIX 口径审计 (只读)")
    parser.add_argument("--since", default="2026-07-01", help="A10 代价核算起始日")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    vixy = fetch_vixy_daily()
    spot = fetch_vix_spot("2025-11-07", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    ratios = [spot[d] / (PROXY_FACTOR * vixy[d]) for d in sorted(set(spot) & set(vixy))]

    db = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data" / "ftseek_us.db"), uri=True)
    db.row_factory = sqlite3.Row
    day_rows = [
        {"trade_date": r["trade_date"], "n": r["n"], "net": r["net"]}
        for r in db.execute(
            """SELECT trade_date, COUNT(*) AS n, SUM(COALESCE(pnl_net, 0)) AS net
               FROM trade_records WHERE trade_date >= ? GROUP BY trade_date ORDER BY trade_date""",
            (args.since,),
        )
    ]
    db.close()

    payload = {
        "factor": PROXY_FACTOR,
        "threshold": BLOCK_THRESHOLD,
        "same_close_ratio": ratio_stats(ratios),
        "monthly_median_ratio": monthly_median_ratios(spot, vixy, PROXY_FACTOR),
        "premarket_freshness": premarket_freshness(ROOT / "data" / "da_checkpoint", vixy),
        "gate_impact_spot": gate_impact(day_rows, spot, BLOCK_THRESHOLD),
        "a10_days": len(day_rows),
        "a10_entries": sum(int(r["n"]) for r in day_rows),
        "a10_net": round(sum(float(r["net"]) for r in day_rows), 2),
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("VIX 口径审计 (factor=%.2f, 硬拒阈值=%.1f)" % (PROXY_FACTOR, BLOCK_THRESHOLD))
    print("\n[B] 同一收盘时刻 spot/(factor×VIXY):")
    print("   ", payload["same_close_ratio"])
    print("    逐月中位数 (漂移 = 常数系数不成立):")
    for month, n, med in payload["monthly_median_ratio"]:
        print("      %s n=%-3d median=%.2f" % (month, n, med))
    fresh = payload["premarket_freshness"]
    with_value = [r for r in fresh if r["distinct_prices"]]
    matched = [r for r in with_value if r["all_match_prev_close"]]
    no_value = [r["day"] for r in fresh if not r["distinct_prices"]]
    mismatch = [(r["day"], r["distinct_prices"], r["prev_close"])
                for r in with_value if not r["all_match_prev_close"]]
    print("\n[A] 盘前 vixy_price 是否 == 上一交易日收盘: %d/%d 天全等"
          % (len(matched), len(with_value)))
    if no_value:
        print("    缺字段的日: %s" % ", ".join(no_value))
    for day, prices, prev_close in mismatch:
        print("    ✗ %s 快照值=%s 前收=%s" % (day, prices, prev_close))
    print("\n[C] 若判据改为现货(前收)>%.0f, A10 窗口内会拦下的交易日:" % BLOCK_THRESHOLD)
    for row in payload["gate_impact_spot"]:
        print("      %s spot=%.2f entries=%d net=%+.2f"
              % (row["trade_date"], row["spot_prev_close"], row["entries"], row["net"]))
    print("    合计: A10 %d 日 / %d 笔 / 净 %+.2f"
          % (payload["a10_days"], payload["a10_entries"], payload["a10_net"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())