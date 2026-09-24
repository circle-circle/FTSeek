#!/usr/bin/env python3
"""跨源同指标一致性抽样 (只读工具)。

用途: 把「同一指标跨源是否给出不同数值」从结构性观察变成可量化结论; 覆盖
      昨收 / PE(TTM) / 分析师共识 / 覆盖家数 / 目标价 / 当日涨跌 六类指标,
      源为 futu(主) · finnhub · TwelveData · AlphaVantage。

典型用法:
  # 全量采集 (需 OpenD 在线; 免费额度有限, 默认 112 只标的)
  PYTHONPATH=. venv/bin/python scripts/multisource_consistency_sample.py --run
  # 只看已有缓存 (不发起任何外部请求)
  PYTHONPATH=. venv/bin/python scripts/multisource_consistency_sample.py --from-cache

产物: data/multisource_consistency_latest.json  (+ 缓存 data/multisource_cache/ms_*.json)

只读保证: 不写交易库/不下单/不改配置; 外部调用均为行情/基本面查询;
          AlphaVantage 免费档 25 次/日, 默认只花 `--av-limit`(默认 10)。
冲突阈值在 CHANNELS 表内预注册, 事后不得修改。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import statistics as st
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "multisource_cache"
OUT = ROOT / "data" / "multisource_consistency_latest.json"

# (key, 单位, 相对?, 阈值, K线/子集说明) —— 预注册, 勿改
TOL = {"bps": 5.0, "pe_rel": 10.0, "pp": 0.10, "target_rel": 5.0}

_log = logging.getLogger(__name__)


def env_key(name: str) -> str:
    for f in (ROOT / ".env", ROOT / ".env.us", ROOT / ".env.local"):
        if not f.exists():
            continue
        for line in f.read_text(errors="ignore").splitlines():
            if line.strip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip().upper() == name.upper():
                m = re.match(r"^[A-Za-z0-9_\-]+", v.strip())
                if m:
                    return m.group(0)
    return ""


def fnum(x):
    try:
        v = float(x)
        return None if v != v else v
    except Exception:
        return None


def http_json(url: str, tries: int = 3):
    for a in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                d = json.loads(r.read().decode())
            if isinstance(d, dict) and (d.get("error") or d.get("Note") or d.get("Information")):
                raise RuntimeError(str(d.get("error") or d.get("Note") or d.get("Information"))[:100])
            return d
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"__err__": f"HTTP {e.code} 无权限"}
            if a == tries - 1:
                return {"__err__": f"HTTP {e.code}"}
        except Exception as e:
            if a == tries - 1:
                return {"__err__": f"{type(e).__name__}: {e}"}
        time.sleep(2.0 + a)
    return {"__err__": "unreachable"}


def save(name, obj):
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / f"ms_{name}.json").write_text(json.dumps(obj, ensure_ascii=False, default=str))
    print(f"  [cache] ms_{name}.json ({len(obj)})", flush=True)


def load(name):
    p = CACHE / f"ms_{name}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def universe(limit: int) -> list[str]:
    con = sqlite3.connect(ROOT / "data/ftseek_us.db")
    con.row_factory = sqlite3.Row
    syms = [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM trade_records WHERE trade_date>='2026-08-01' AND symbol LIKE 'US.%' ORDER BY symbol")]
    try:
        syms += [r[0] for r in con.execute("SELECT DISTINCT symbol FROM open_positions WHERE symbol LIKE 'US.%'")]
    except Exception:
        _log.warning("open positions query failed", exc_info=True)
    syms = sorted({s for s in syms if re.match(r"^US\.[A-Z.\-]{1,8}$", s)})
    return syms[:limit] if limit else syms


def collect(syms, td_limit, av_limit):
    FK, TD, AV = env_key("FINNHUB_API_KEY"), env_key("TWELVEDATA_API_KEY"), env_key("ALPHAVANTAGE_API_KEY")
    snaps, cons, kprev = load("futu_snap"), load("futu_cons"), load("futu_kprev")
    if not (snaps and cons and kprev):
        from futu import OpenQuoteContext, RET_OK
        qt = OpenQuoteContext(host="127.0.0.1", port=11111)
        if not snaps:
            ret, snap = qt.get_market_snapshot(syms)
            if ret == RET_OK and not snap.empty:
                keep = ["prev_close_price", "last_price", "pe_ratio", "pe_ttm_ratio", "pb_ratio"]
                for _, row in snap.iterrows():
                    snaps[str(row["code"])] = {
                        k: (None if str(row[k]) in ("nan", "N/A", "") else float(row[k])) for k in keep}
            save("futu_snap", snaps)
        if not cons:
            for i, s in enumerate(syms):
                try:
                    r, c = qt.get_research_analyst_consensus(s)
                    cons[s] = c if r == RET_OK else {"__err__": f"ret={r}"}
                except Exception as e:
                    cons[s] = {"__err__": f"{type(e).__name__}: {e}"}
                time.sleep(0.6)
            save("futu_cons", cons)
        if not kprev:
            for s in syms[:20]:
                try:
                    r, kl, _ = qt.request_history_kline(s, start="2026-09-05", end="2026-09-19",
                                                        ktype="K_DAY", max_count=12)
                    if r == RET_OK and len(kl) >= 2:
                        kprev[s] = float(kl.iloc[-2]["close"])
                except Exception:
                    _log.warning("kline request failed", exc_info=True)
                time.sleep(0.2)
            save("futu_kprev", kprev)
        qt.close()
    fh = load("finnhub")
    for i, s in enumerate(syms):
        if s in fh:
            continue
        bare = s.split(".")[-1].replace(".", "-")
        fh[s] = {"quote": http_json(f"https://finnhub.io/api/v1/quote?symbol={bare}&token={FK}"),
                 "recommendation": http_json(f"https://finnhub.io/api/v1/stock/recommendation?symbol={bare}&token={FK}"),
                 "metric": http_json(f"https://finnhub.io/api/v1/stock/metric?symbol={bare}&metric=all&token={FK}")}
        time.sleep(1.05)
        if (i + 1) % 25 == 0:
            save("finnhub", fh)
    save("finnhub", fh)
    td = load("twelvedata")
    for s in syms[:td_limit]:
        if s in td:
            continue
        bare = s.split(".")[-1].replace(".", "-")
        td[s] = {"quote": http_json(f"https://api.twelvedata.com/quote?symbol={bare}&apikey={TD}")}
        time.sleep(7.6)
        td[s]["price_target"] = http_json(f"https://api.twelvedata.com/price_target?symbol={bare}&apikey={TD}")
        time.sleep(7.6)
        save("twelvedata", td)
    save("twelvedata", td)
    av = load("alphavantage")
    for s in syms[:av_limit]:
        if s in av:
            continue
        bare = s.split(".")[-1].replace(".", "-")
        av[s] = {"OVERVIEW": http_json(f"https://www.alphavantage.co/query?function=OVERVIEW&symbol={bare}&apikey={AV}")}
        time.sleep(13.0)
        save("alphavantage", av)
    save("alphavantage", av)


def analyze():
    snaps, cons, kprev = load("futu_snap"), load("futu_cons"), load("futu_kprev")
    fh, td, av = load("finnhub"), load("twelvedata"), load("alphavantage")
    res = defaultdict(lambda: {"n": 0, "conflict": 0, "deltas": [], "ex": [], "missing": 0, "unit": "bps"})

    def add(key, a, b, thr, rel=False, label="", unit=None):
        g = res[key]
        g["unit"] = unit or ("%" if rel else "bps")
        if a is None or b is None or b == 0:
            g["missing"] += 1
            return
        if rel and abs(b) < 0.05 and abs(a) < 0.05:
            return  # 双方均近零 → 相对差不具意义, 计为不可比
        d = (a - b) / abs(b) * (100.0 if rel else 10000.0)
        g["n"] += 1
        g["deltas"].append(d)
        if abs(d) > thr:
            g["conflict"] += 1
            if len(g["ex"]) < 8:
                g["ex"].append(f"{label}: {a:g} vs {b:g} (Δ{d:+.1f}{g['unit']})")

    stale = []
    for s, sn in snaps.items():
        sn = sn or {}
        c = cons.get(s) or {}
        q = (fh.get(s) or {}).get("quote") or {}
        mt = (fh.get(s) or {}).get("metric") or {}
        reco = (fh.get(s) or {}).get("recommendation") or []
        qtd = (td.get(s) or {}).get("quote") or {}
        tpt = (td.get(s) or {}).get("price_target") or {}
        ov = (av.get(s) or {}).get("OVERVIEW") or {}
        add("I1 昨收 futu vs finnhub", fnum(sn.get("prev_close_price")), fnum(q.get("pc")), TOL["bps"], label=s)
        add("I1b 昨收 futu vs TwelveData", fnum(sn.get("prev_close_price")), fnum(qtd.get("previous_close")),
            TOL["bps"], label=s)
        add("I2 昨收 futu快照 vs futu日K", fnum(sn.get("prev_close_price")), fnum(kprev.get(s)), TOL["bps"], label=s)
        if isinstance(mt, dict) and "metric" in mt:
            add("I3 PE(TTM) futu vs finnhub", fnum(sn.get("pe_ttm_ratio")),
                fnum(mt["metric"].get("peTTM")), TOL["pe_rel"], rel=True, label=s)
        add("I3b PE(TTM) futu vs AlphaVantage", fnum(sn.get("pe_ttm_ratio")), fnum(ov.get("PERatio")),
            TOL["pe_rel"], rel=True, label=s)
        last, pc = fnum(sn.get("last_price")), fnum(sn.get("prev_close_price"))
        fu_chg = (last - pc) / pc * 100 if (last and pc and pc != 0) else None
        add("I7 当日涨跌% futu vs finnhub", fu_chg, fnum(q.get("dp")), TOL["pp"], rel=True, label=s)
        add("I7b 当日涨跌% futu vs TwelveData", fu_chg, fnum(qtd.get("percent_change")), TOL["pp"], rel=True, label=s)
        if isinstance(c, dict) and "buy" in c and isinstance(reco, list) and reco:
            d0 = reco[0]
            tot = sum(fnum(d0.get(k)) or 0 for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
            fbpct = ((fnum(d0.get("strongBuy")) or 0) + (fnum(d0.get("buy")) or 0)) / tot * 100 if tot else None
            fb = fnum(c.get("buy"))
            if fb is not None and fbpct is not None:
                g = res["I4 共识方向 futu.buy% vs finnhub"]
                g["unit"] = "pp"
                g["n"] += 1
                g["deltas"].append(fb - fbpct)
                lab = lambda x: "bull" if x >= 60 else ("bear" if x <= 40 else "neutral")
                if lab(fb) != lab(fbpct):
                    g["conflict"] += 1
                    if len(g["ex"]) < 8:
                        g["ex"].append(f"{s}: futu {fb:.0f}%({lab(fb)}) vs finnhub {fbpct:.0f}%({lab(fbpct)})")
            t1, t2 = fnum(c.get("total")), tot or None
            if t1 and t2:
                g = res["I5 覆盖家数 比值 futu/finnhub"]
                g["unit"] = "x"
                g["n"] += 1
                g["deltas"].append(t1 / t2)
            tm = (tpt.get("price_target") or {}).get("median") if isinstance(tpt, dict) else None
            add("I6 目标价 futu vs TwelveData", fnum(c.get("average")), fnum(tm), TOL["target_rel"], rel=True, label=s)
            add("I6b 目标价 futu vs AlphaVantage", fnum(c.get("average")),
                fnum(ov.get("AnalystTargetPrice")), TOL["target_rel"], rel=True, label=s)
            stale.append((c.get("update_time_str"), d0.get("period")))

    print(f"\n样本: futu快照 {len(snaps)} / futu共识 {sum(1 for v in cons.values() if isinstance(v, dict) and 'buy' in v)}"
          f" / finnhub {len(fh)} / TwelveData {len(td)} / AlphaVantage {len(av)}\n")
    print(f"{'指标':<44}{'可比n':>6}{'冲突':>6}{'冲突率':>8}{'中位Δ':>11}{'p90|Δ|':>10}{'缺失':>6}")
    print("-" * 92)
    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "indicators": {}}
    for k in sorted(res):
        g = res[k]
        u = g["unit"]
        if not g["n"]:
            print(f"{k:<44}{0:>6}{'-':>6}{'-':>8}{'-':>11}{'-':>10}{g['missing']:>6}")
            continue
        ds = g["deltas"]
        med = st.median(ds)
        p90 = sorted(abs(x) for x in ds)[min(len(ds) - 1, int(len(ds) * 0.9))]
        rate = "-" if u == "x" else f"{g['conflict'] / g['n']:.0%}"
        print(f"{k:<44}{g['n']:>6}{g['conflict']:>6}{rate:>8}{med:>10.2f}{u}{p90:>9.1f}{u}{g['missing']:>6}")
        out["indicators"][k] = {"n": g["n"], "conflict": g["conflict"], "rate": g["conflict"] / g["n"],
                                "median_delta": med, "p90_abs": p90, "unit": u,
                                "missing": g["missing"], "examples": g["ex"]}
        for e in g["ex"][:4]:
            print(f"      · {e}")
    out["staleness"] = Counter([str(x) for x in stale]).most_common(5)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n时效组合 (富途 update vs finnhub period): {out['staleness']}")
    print(f"结果 → {OUT}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="发起采集 (需 OpenD 在线)")
    ap.add_argument("--from-cache", action="store_true", help="只用已有缓存分析, 不发外部请求")
    ap.add_argument("--limit", type=int, default=112)
    ap.add_argument("--td-limit", type=int, default=15)
    ap.add_argument("--av-limit", type=int, default=10)
    a = ap.parse_args()
    if a.run:
        collect(universe(a.limit), a.td_limit, a.av_limit)
    if a.run or a.from_cache:
        analyze()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()