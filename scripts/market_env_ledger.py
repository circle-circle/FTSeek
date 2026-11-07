#!/usr/bin/env python3
"""市场环境账本 (源头真值, 只读): 一行一交易日, 供 brief / 归因 / 研究共用。

背景 (RFC-190 / DEB-20261106 §二十三):
执行门与 DA brief 读的是 `VIXY×0.85` 代理, 而它 (a) 盘前只等于上一交易日收盘
(23/23 实测), (b) 换算系数逐月从 0.65 漂到 1.35。同一天两个"VIX"可差 10 点并横跨阈值 28。
本脚本把**官方真值**落到一个可回放的账本里, 并把"代理 vs 真值"的差**并排保留**——
近似值不删, 但要永远可见。

数据源 (全部免费, 2026-11-07 实测可达):
  - CBOE 官方日线: VIX / VIX9D / VIX3M  (1990 起; 期限结构 = VIX9D/VIX3M-1)
  - Yahoo 日线: SPY / QQQ (算日收益与 20 日已实现波动 rv20)
  - 本地 `data/da_checkpoint/<date>/US.*.raw.json` 的 market_context.vix (代理侧留证)

性质: **只读 + 只写 data/ 产物**。不接交易、不改阈值、无 cron(由人/既有 job 调用)。
回滚: 删本脚本与 `data/market_env_daily.jsonl`。

用法:
  venv/bin/python scripts/market_env_ledger.py build            # 重建账本
  venv/bin/python scripts/market_env_ledger.py build --since 2026-07-01
  venv/bin/python scripts/market_env_ledger.py report           # 环境 vs 执行 归因
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
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

CACHE_DIR = ROOT / "data" / "cache" / "market_env"
LEDGER = ROOT / "data" / "market_env_daily.jsonl"
UA = {"User-Agent": "Mozilla/5.0 (FTSeek market_env_ledger)"}
PROXY_FACTOR = 0.85
CBOE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/%s_History.csv"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=2y&interval=1d"


# ── 取数 (缓存优先, 失败留痕) ───────────────────────────────────────────────

def _http(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (固定白名单域名)
        return resp.read().decode("utf-8", "replace")


def _cached(name: str, loader) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    try:
        body = loader()
        path.write_text(body)
        return body
    except Exception as exc:  # 换源/断网时必须可见, 不得静默
        if path.exists():
            print("[market_env] %s 取数失败, 用本地缓存: %s" % (name, exc), file=sys.stderr)
            return path.read_text()
        raise


def fetch_cboe(name: str) -> Dict[str, float]:
    body = _cached(name + ".csv", lambda: _http(CBOE % name, timeout=40))
    out: Dict[str, float] = {}
    for row in list(csv.reader(io.StringIO(body)))[1:]:
        if len(row) < 5:
            continue
        try:
            day = datetime.strptime(row[0], "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        try:
            out[day] = float(row[4])
        except ValueError:
            continue
    return out


def fetch_daily_close(symbol: str) -> Dict[str, float]:
    body = _cached(symbol + "_daily.json", lambda: _http(YAHOO % symbol.replace("^", "%5E"), timeout=30))
    result = json.loads(body)["chart"]["result"][0]
    closes = result["indicators"]["quote"][0]["close"]
    out: Dict[str, float] = {}
    for ts, close in zip(result["timestamp"], closes):
        if close is None:
            continue
        out[datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")] = float(close)
    return out


# ── 纯函数 (可测) ──────────────────────────────────────────────────────────

def pct_rank(value: float, window: Sequence[float]) -> Optional[float]:
    """value 在 window 中的百分位 (0-100, 含自身)。window 为空返回 None。"""
    if not window:
        return None
    below = sum(1 for x in window if x <= value)
    return round(100.0 * below / len(window), 1)


def realized_vol(closes: Sequence[float], window: int = 20) -> Optional[float]:
    """年化已实现波动 (%, 日对数收益标准差 × √252)。样本不足返回 None。"""
    series = [c for c in closes if c and c > 0]
    if len(series) < window + 1:
        return None
    rets = [math.log(series[i] / series[i - 1]) for i in range(len(series) - window, len(series))]
    return round(statistics.pstdev(rets) * math.sqrt(252) * 100, 2)


def pct_change(prev: Optional[float], cur: Optional[float]) -> Optional[float]:
    if not prev or not cur:
        return None
    return round((cur / prev - 1) * 100, 3)


def bucketize(rows: Iterable[dict], key: str, edges: Sequence[Tuple[float, float]],
              value_key: str = "net") -> List[dict]:
    """按 [lo, hi) 分桶汇总 (n / net)。缺失值的行整体跳过并在结果里计数。"""
    out = []
    skipped = 0
    rows = list(rows)
    for lo, hi in edges:
        picked = [r for r in rows if r.get(key) is not None and lo <= r[key] < hi]
        out.append({
            "bucket": "[%g,%g)" % (lo, hi),
            "n": len(picked),
            "net": round(sum(float(r.get(value_key) or 0) for r in picked), 2),
        })
    skipped = sum(1 for r in rows if r.get(key) is None)
    if skipped:
        out.append({"bucket": "(missing)", "n": skipped, "net": 0.0})
    return out


def proxy_gap(proxy: Optional[float], spot_prev_close: Optional[float]) -> Optional[float]:
    """代理值相对'真值(前一日现货收盘)'的偏离 %。>0 = 代理低估。"""
    if proxy is None or not spot_prev_close:
        return None
    return round((proxy / spot_prev_close - 1) * 100, 2)


# ── build ─────────────────────────────────────────────────────────────────

def read_proxy_values() -> Dict[str, dict]:
    """从 DA checkpoint 读当日盘前代理值 (按日聚合全部 raw, 取众数)。"""
    out: Dict[str, dict] = {}
    for day_dir in sorted((ROOT / "data" / "da_checkpoint").glob("*")):
        if not day_dir.is_dir():
            continue
        prices: List[float] = []
        for path in day_dir.glob("US.*.raw.json"):
            try:
                payload = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            vix = ((payload.get("collected") or {}).get("market_context") or {}).get("vix") or {}
            if vix.get("vixy_price"):
                prices.append(round(float(vix["vixy_price"]), 2))
        if not prices:
            continue
        common = statistics.mode(prices)
        out[day_dir.name] = {"vixy_price": common, "vix_proxy": round(common * PROXY_FACTOR, 2)}
    return out


def build(since: str, quiet: bool = False) -> int:
    vix = fetch_cboe("VIX")
    vix9 = fetch_cboe("VIX9D")
    vix3 = fetch_cboe("VIX3M")
    spy = fetch_daily_close("SPY")
    qqq = fetch_daily_close("QQQ")
    proxy = read_proxy_values()

    days = sorted(d for d in vix if d >= since)
    lines: List[dict] = []
    for day in days:
        hist = [d for d in days if d <= day]
        tail252 = [vix[d] for d in hist[-252:]]
        spy_hist = [spy[d] for d in sorted(spy) if d <= day][-21:]
        prev_day_list = [d for d in days if d < day]
        prev_day = prev_day_list[-1] if prev_day_list else None
        slope = None
        if vix9.get(day) and vix3.get(day):
            slope = round(vix9[day] / vix3[day] - 1, 4)
        rec = {
            "date": day,
            "vix_close": round(vix[day], 2),
            "vix9d": round(vix9[day], 2) if day in vix9 else None,
            "vix3m": round(vix3[day], 2) if day in vix3 else None,
            "term_slope": slope,
            "vix_pct252": pct_rank(vix[day], tail252),
            "rv20_spy": realized_vol(spy_hist, 20),
            "spy_ret_pct": pct_change(spy.get(prev_day) if prev_day else None, spy.get(day)),
            "qqq_ret_pct": pct_change(qqq.get(prev_day) if prev_day else None, qqq.get(day)),
            "vix_proxy_premarket": proxy.get(day, {}).get("vix_proxy"),
            "vixy_price": proxy.get(day, {}).get("vixy_price"),
            "proxy_gap_pct": proxy_gap(proxy.get(day, {}).get("vix_proxy"),
                                       vix.get(prev_day) if prev_day else None),
        }
        lines.append(rec)

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for rec in lines:
            handle.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, LEDGER)
    if not quiet:
        print("[market_env] 写入 %s: %d 行 (%s .. %s)" % (LEDGER, len(lines), lines[0]["date"], lines[-1]["date"]))
        gaps = [r["proxy_gap_pct"] for r in lines if r["proxy_gap_pct"] is not None]
        if gaps:
            print("[market_env] 代理 vs 真值(前收) 偏离: 中位 %+.1f%% / 最差 %+.1f%% / 样本 %d 天"
                  % (statistics.median(gaps), max(gaps, key=abs), len(gaps)))
    return 0


def summary_line(since: str = "2026-07-01", max_age_hours: float = 30.0) -> str:
    """盘前健康检查用的一行真值摘要 (账本过期则尝试重建; 重建失败降级为提示, 不外抛)。"""
    try:
        stale = (not LEDGER.exists()
                 or (datetime.now(timezone.utc).timestamp() - LEDGER.stat().st_mtime) > max_age_hours * 3600)
        if stale:
            build(since, quiet=True)
        rows = [json.loads(line) for line in LEDGER.read_text().splitlines() if line]
        if not rows:
            return "🟡 环境账本为空"
        last = rows[-1]
        slope = last.get("term_slope")
        slope_txt = ("倒挂 %+.2f" % slope) if (slope is not None and slope > 0) else (
            "正常 %+.2f" % slope if slope is not None else "n/a")
        gap = last.get("proxy_gap_pct")
        return ("✅ 环境真值 | %s VIX %.2f (pct%s) | 期限 %s | rv20 %s | 代理差 %s"
                % (last["date"], last["vix_close"], last.get("vix_pct252"), slope_txt,
                   last.get("rv20_spy"), ("%+.0f%%" % gap) if gap is not None else "n/a"))
    except Exception as exc:  # 取数失败必须可见, 但不阻断其它检查
        return "🟡 环境账本不可用: %s" % exc


# ── report: 环境 vs 执行 ──────────────────────────────────────────────────

def load_ledger() -> Dict[str, dict]:
    if not LEDGER.exists():
        raise SystemExit("账本不存在, 先跑 build")
    return {r["date"]: r for r in (json.loads(line) for line in LEDGER.read_text().splitlines() if line)}


def load_trades(since: str) -> List[dict]:
    conn = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data" / "ftseek_us.db"), uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        """SELECT trade_date, symbol, direction, entry_session, exit_reason,
                  realized_pnl, pnl_net, fees_est, mfe_pct, market_regime
           FROM trade_records WHERE trade_date >= ?""", (since,))]
    conn.close()
    return rows


def report(since: str) -> int:
    ledger = load_ledger()
    trades = load_trades(since)
    closed = [t for t in trades if t["realized_pnl"] is not None]
    gross = sum(float(t["realized_pnl"]) for t in closed)
    fees = sum(float(t["fees_est"] or 0) for t in closed)
    net = sum(float(t["pnl_net"] or 0) for t in closed)

    print("== 1. 输入真值体检 (代理 vs 官方) ==")
    gaps = [(d, r["proxy_gap_pct"]) for d, r in ledger.items() if r.get("proxy_gap_pct") is not None]
    if gaps:
        vals = [g for _, g in gaps]
        bad = [(d, g) for d, g in gaps if abs(g) >= 20]
        print("  样本 %d 天 | 中位 %+.1f%% | 平均 %+.1f%% | |差|>=20%% 的 %d 天"
              % (len(vals), statistics.median(vals), statistics.mean(vals), len(bad)))
        print("  最严重的 5 天: %s" % ", ".join("%s %+.0f%%" % (d, g) for d, g in sorted(gaps, key=lambda x: -abs(x[1]))[:5]))

    print("\n== 2. 总量 (A10 起, 已平仓 n=%d) ==" % len(closed))
    print("  毛 %+.2f | 费 %.2f (占毛 %.1f%%) | 净 %+.2f"
          % (gross, fees, (fees / gross * 100 if gross else float('nan')), net))
    print("  未平仓 %d 笔" % (len(trades) - len(closed)))

    print("\n== 3. 执行面: 按 exit_reason (净额升序) ==")
    agg: Dict[str, List[float]] = defaultdict(lambda: [0, 0.0, 0.0])
    for t in closed:
        key = t["exit_reason"] or "(none)"
        agg[key][0] += 1
        agg[key][1] += float(t["realized_pnl"])
        agg[key][2] += float(t["pnl_net"] or 0)
    for key, (n, g, nt) in sorted(agg.items(), key=lambda kv: kv[1][2])[:10]:
        print("  %-24s n=%-3d 毛 %+9.2f 净 %+9.2f" % (key, n, g, nt))

    print("\n== 4. 环境面: 按当日 QQQ 涨跌分桶 ==")
    daily: Dict[str, dict] = {}
    for t in trades:
        d = daily.setdefault(t["trade_date"], {"net": 0.0, "n": 0})
        d["net"] += float(t["pnl_net"] or 0)
        d["n"] += 1
    rows = []
    for day, agg_day in daily.items():
        led = ledger.get(day) or {}
        rows.append({"date": day, "net": agg_day["net"], "n": agg_day["n"],
                     "qqq_ret_pct": led.get("qqq_ret_pct"),
                     "vix_close": led.get("vix_close"),
                     "vix_pct252": led.get("vix_pct252"),
                     "term_slope": led.get("term_slope"),
                     "rv20_spy": led.get("rv20_spy")})
    for b in bucketize(rows, "qqq_ret_pct", [(-100, -2), (-2, -1), (-1, 0), (0, 1), (1, 100)]):
        print("  QQQ %-12s 日数=%-3d 净 %+9.2f" % (b["bucket"], b["n"], b["net"]))

    print("\n== 5. 环境面: 按真值 VIX 分位 (252 日) ==")
    for b in bucketize(rows, "vix_pct252", [(0, 20), (20, 40), (40, 60), (60, 80), (80, 101)]):
        print("  pct %-12s 日数=%-3d 净 %+9.2f" % (b["bucket"], b["n"], b["net"]))

    print("\n== 6. 环境面: 按期限结构 (VIX9D/VIX3M-1) ==")
    for b in bucketize(rows, "term_slope", [(-1, -0.1), (-0.1, 0), (0, 0.1), (0.1, 1)]):
        print("  slope %-12s 日数=%-3d 净 %+9.2f" % (b["bucket"], b["n"], b["net"]))

    print("\n== 7. 系统自打标签 regime 是否可分 ==")
    reg: Dict[str, List[float]] = defaultdict(lambda: [0, 0.0])
    for t in closed:
        key = t.get("market_regime") or "(null)"
        reg[key][0] += 1
        reg[key][1] += float(t["pnl_net"] or 0)
    for key, (n, nt) in sorted(reg.items(), key=lambda kv: kv[1][1]):
        print("  regime=%-12s n=%-3d 净 %+9.2f" % (key, n, nt))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="市场环境账本 (只读)")
    ap.add_argument("cmd", choices=["build", "report"], nargs="?", default="build")
    ap.add_argument("--since", default="2026-07-01")
    args = ap.parse_args()
    return build(args.since) if args.cmd == "build" else report(args.since)


if __name__ == "__main__":
    raise SystemExit(main())