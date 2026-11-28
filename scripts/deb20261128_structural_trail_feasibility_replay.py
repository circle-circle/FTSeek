#!/usr/bin/env python3
"""DEB-20261128-livermore §13 补遗：结构锚 trailing 只读可行性回放（方案 A 方向性 sanity）.

定位（务必先读）:
  本脚本是 **record-only 历史可行性回放**, 不是注册影子、不是 KR4 实验、不是预注册 efficacy
  证明。它回答用户「能否用已有数据验证方案 A」: 在已有 1min K 线缓存上, 把现行 adaptive trail
  出场替换为 RFC-213 结构锚 (跌破最后确认的 15m higher-low 才走), 配对比较净结果。
  §7A 的 **前瞻配对影子** (≥30 TREND 独立仓 ∧ ≥10 ET 日, 预注册主终点配对 Δ净 bps) 仍是
  独立的用户裁决步骤, 本脚本不替代它, 也不据此上线/下线任何生产规则。

只读承诺: sqlite `mode=ro`; 只读 `data/replay_bars` 共享 1min 缓存 (绝不碰 OpenD/实时行情);
  不写库 / 不下单 / 不改任何文件 / 不 import app.* (避免 db_utils 全局 monkey-patch; 与
  scripts/deb20261128_livermore_firstprinciples_probe.py 同款约定)。输出仅 stdout。

参数事先固定 (RFC-213 §4.6/§5 + DEB-20261128 §7A), **不调参**:
  RESAMPLE_MIN=15; SWING_CONFIRM_BARS=2 (fractal 左右各2bar, 右侧确认⇒无未来函数);
  ATR_PERIOD=14; ATR_BUFFER_MULT=0.2; TREND=连续 HH∧HL (LONG) / LH∧LL (SHORT);
  破位以 15m **收盘价**穿越结构锚判定; 未形成结构⇒ABSTAIN 沿用现行出场。
  ⚠️ 以上 bar 数/0.2ATR/收盘破位均为 **影子初始假设** (RFC 明文 bar 数未钉死), 非已证生产参数;
  生效率 (arming rate) 对 swing 定义敏感, 见 §13.5 局限。

口径 (与 docs/ADJUDICATED-CRITERIA.md 一致):
  A10 ERA 谓词 = app/core/db_utils.ERA_CLEAN_PERFORMANCE_WHERE 逐字副本 (硬编码避免 app 依赖);
  A9 首跑测试批排除; 人群 source_type='PLAYBOOK_SIM' ∧ exit_price>0 ∧ 净额可测; DA=decision_id LIKE 'DA-%'。
  单位: pnl_pct/net_pnl_pct=分数, mfe_pct=百分数; capture%=pnl_pct×100/mfe_pct×100。
  Δbps 分母 = 11 月 DA 全人群入场名义额 (与 −30.66bps 头条同分母, 可比)。

局限: SIM-only; fees 用该笔实际 fees_est 代理 (非重新估算); 历史回放非前瞻; armed n=7<30
  按宪章不构成结论 (非证伪); 无市场冲击假设。复现:
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./venv/bin/python \
      scripts/deb20261128_structural_trail_feasibility_replay.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")

# --- 参数事先固定 (不调参) ---
RESAMPLE_MIN = 15
SWING_CONFIRM_BARS = 2          # fractal 左右各 2bar; 右侧确认 ⇒ 无未来函数
ATR_PERIOD = 14
ATR_BUFFER_MULT = 0.2           # DEB §7A / RFC §4.6
MIN_BARS = 20                   # 重采样后最少 15m bar 数, 否则 skip
# 11 月窗口内的美国休市日 (Thanksgiving); 最小假设, 与可行性探针一致。
HOLIDAYS = {"2026-11-26"}

# A10 SSOT 逐字副本 (app/core/db_utils.ERA_CLEAN_PERFORMANCE_WHERE)。
ERA = (
    "AND trade_date >= '2026-07-01' "
    "AND COALESCE(data_quality,'') IN ('clean','real') "
    "AND COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL')"
)
A9 = "AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _et(ts):
    s = str(ts or "")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=ET) if dt.tzinfo is None else dt.astimezone(ET)


def fetch(conn, where):
    q = (
        "SELECT * FROM trade_records WHERE source_type='PLAYBOOK_SIM' "
        "AND exit_price>0 AND COALESCE(pnl_net,realized_pnl) IS NOT NULL "
        f"{ERA} {A9} {where}"
    )
    return [dict(r) for r in conn.execute(q)]


def required_days(e, x):
    if not e or not x:
        return []
    d, end, out = e.date(), x.date(), []
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in HOLIDAYS:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def coverage(bar_dir, sym, e, x):
    req = required_days(e, x)
    if not req:
        return "no_days", 0, 0
    stem = sym.replace(".", "_")
    have = sum(1 for dt in req if (bar_dir / f"{stem}_{dt}.json").exists())
    state = "full" if have == len(req) else ("partial" if have else "none")
    return state, have, len(req)


def load_bars(bar_dir, sym, e, x):
    stem = sym.replace(".", "_")
    out, ok = [], True
    for dt in required_days(e, x):
        p = bar_dir / f"{stem}_{dt}.json"
        if not p.exists():
            ok = False
            continue
        for row in json.loads(p.read_text(encoding="utf-8")):
            if len(row) < 5:
                continue
            try:
                t = datetime.strptime(str(row[0])[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
            except ValueError:
                continue
            out.append({"t": t, "o": float(row[1]), "h": float(row[2]),
                        "l": float(row[3]), "c": float(row[4])})
    out = [b for b in out if b["t"] > e.replace(second=0, microsecond=0)]
    out.sort(key=lambda b: b["t"])
    return out, ok


def resample(bars, minutes=RESAMPLE_MIN):
    buck = {}
    for b in bars:
        k = b["t"].replace(minute=(b["t"].minute // minutes) * minutes, second=0, microsecond=0)
        if k not in buck:
            buck[k] = {"t": k, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
        else:
            g = buck[k]
            g["h"] = max(g["h"], b["h"])
            g["l"] = min(g["l"], b["l"])
            g["c"] = b["c"]
    return [buck[k] for k in sorted(buck)]


def confirmed_swings(bars, k=SWING_CONFIRM_BARS):
    """无未来函数: pivot 在 i-k, 仅当右侧 k 根已走完 (当前 i) 才确认, confirmed_at=bars[i].t."""
    lo, hi = [], []
    for i in range(2 * k, len(bars)):
        p = i - k
        L, H = bars[p]["l"], bars[p]["h"]
        nb = [j for j in range(p - k, p + k + 1) if j != p]
        if all(L < bars[j]["l"] for j in nb):
            lo.append((bars[i]["t"], L))
        if all(H > bars[j]["h"] for j in nb):
            hi.append((bars[i]["t"], H))
    return lo, hi


def atr(bars, i, n=ATR_PERIOD):
    trs = []
    for j in range(max(1, i - n + 1), i + 1):
        pc = bars[j - 1]["c"]
        trs.append(max(bars[j]["h"] - bars[j]["l"],
                       abs(bars[j]["h"] - pc), abs(bars[j]["l"] - pc)))
    return st.mean(trs) if trs else None


def simulate(bars15, direction):
    """结构锚 trailing: TREND(HH∧HL / LH∧LL) 才 arm; 收盘破位出场; 否则 ABSTAIN."""
    lo, hi = confirmed_swings(bars15)
    armed, stop = False, None
    for i, b in enumerate(bars15):
        t = b["t"]
        clo = [v for (ct, v) in lo if ct <= t]
        chi = [v for (ct, v) in hi if ct <= t]
        a = atr(bars15, i)
        if direction == "LONG":
            if len(clo) >= 2 and len(chi) >= 2 and clo[-1] > clo[-2] and chi[-1] > chi[-2] and a is not None:
                ns = clo[-1] - ATR_BUFFER_MULT * a
                stop = ns if stop is None else max(stop, ns)
                armed = True
            if armed and stop is not None and b["c"] < stop:
                return {"exit_t": t, "exit_px": stop, "armed": True}
        else:
            if len(chi) >= 2 and len(clo) >= 2 and chi[-1] < chi[-2] and clo[-1] < clo[-2] and a is not None:
                ns = chi[-1] + ATR_BUFFER_MULT * a
                stop = ns if stop is None else min(stop, ns)
                armed = True
            if armed and stop is not None and b["c"] > stop:
                return {"exit_t": t, "exit_px": stop, "armed": True}
    return {"exit_t": None, "exit_px": None, "armed": armed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(ROOT / "data" / "ftseek_us.db"))
    ap.add_argument("--bars-dir", default=str(ROOT / "data" / "replay_bars"))
    ap.add_argument("--since", default="2026-11-01")
    args = ap.parse_args()
    bar_dir = Path(args.bars_dir)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    da_nov = fetch(conn, f"AND trade_date>='{args.since}' AND decision_id LIKE 'DA-%'")
    trail = [r for r in da_nov if r["exit_reason"] == "SL_TRAIL_LOCK"]
    nov_notional = sum((_f(r["entry_price"]) or 0) * (_f(r["quantity"]) or 0) for r in da_nov)

    # --- §13.2 数据可行性覆盖 ---
    allc, trailc = Counter(), Counter()
    for r in da_nov:
        allc[coverage(bar_dir, r["symbol"], _et(r["entry_time"]), _et(r["exit_time"]))[0]] += 1
    for r in trail:
        trailc[coverage(bar_dir, r["symbol"], _et(r["entry_time"]), _et(r["exit_time"]))[0]] += 1
    print("=== §13.2 数据可行性 (1min 缓存覆盖, 只读) ===")
    print(f"Nov DA 全人群 n={len(da_nov)} 覆盖={dict(allc)}")
    print(f"SL_TRAIL_LOCK n={len(trail)} 覆盖={dict(trailc)}")

    # --- §13.4 结构锚配对回放 ---
    armed_rows, abstain, skip = [], [], 0
    for r in trail:
        e, x = _et(r["entry_time"]), _et(r["exit_time"])
        bars, ok = load_bars(bar_dir, r["symbol"], e, x)
        if not ok or len(bars) < MIN_BARS:
            skip += 1
            continue
        b15 = resample(bars)
        sim = simulate(b15, r["direction"])
        ep, qty, fee = _f(r["entry_price"]), _f(r["quantity"]), _f(r["fees_est"]) or 0.0
        sign = 1.0 if r["direction"] == "LONG" else -1.0
        actual_net = _f(r["pnl_net"])
        if sim["exit_px"] is not None:
            struct_net = sign * (sim["exit_px"] - ep) * qty - fee
            armed_rows.append({
                "sym": r["symbol"], "date": str(r["trade_date"]), "dir": r["direction"],
                "delta": struct_net - actual_net, "actual": actual_net, "struct": struct_net,
                "mfe": _f(r["mfe_pct"]),
                "struct_pct": sign * (sim["exit_px"] - ep) / ep * 100,
                "earlier": sim["exit_t"] < x if x else None,
            })
        else:
            abstain.append(r["symbol"])

    cov = len(armed_rows) + len(abstain)
    print("\n=== §13.4 结构锚 trailing 配对回放 (Nov DA SL_TRAIL_LOCK; 历史 sanity 非 efficacy) ===")
    print(f"trail={len(trail)} 可回放={cov} skip={skip} | TREND生效(armed)={len(armed_rows)} ABSTAIN={len(abstain)}")
    if armed_rows:
        ds = [r["delta"] for r in armed_rows]
        caps = [r["struct_pct"] / r["mfe"] * 100 for r in armed_rows if r["mfe"] and r["mfe"] > 0.05]
        print(f"armed n={len(armed_rows)}: Δ净$ sum={sum(ds):.1f} mean={st.mean(ds):.2f} med={st.median(ds):.2f} "
              f"Δbps={10000 * sum(ds) / nov_notional:.2f} (分母 Nov DA 名义 ${nov_notional:.0f})")
        print(f"  更早(截断回吐)={sum(1 for r in armed_rows if r['earlier'])} "
              f"更晚(让利润奔跑)={sum(1 for r in armed_rows if not r['earlier'])} | "
              f"Δ>0={sum(1 for d in ds if d > 0)} Δ<0={sum(1 for d in ds if d < 0)}")
        print(f"  实际净$ sum={sum(r['actual'] for r in armed_rows):.1f} "
              f"结构净$ sum={sum(r['struct'] for r in armed_rows):.1f}")
        if caps:
            print(f"  结构 capture_med={st.median(caps):.0f}% (现行 trail capture_med=56%, 见 §3.3)")
        print("  逐笔 (Δ$ 升序):")
        for r in sorted(armed_rows, key=lambda z: z["delta"]):
            print(f"    {r['sym']:9} {r['date']} {r['dir']:5} Δ={r['delta']:+7.1f} "
                  f"actual={r['actual']:+7.1f} struct={r['struct']:+7.1f} mfe={r['mfe']:.2f}% "
                  f"{'早' if r['earlier'] else '晚'}")
    print(f"ABSTAIN n={len(abstain)} (未形成 HH/HL ⇒ 沿用现行, Δ=0): {abstain}")
    print("\n⚠️ §13.5 边界: 可行性/方向性 sanity, 非 efficacy 证明; armed n<30 不结论(非证伪); "
          "历史非前瞻; arming 率对 swing 定义敏感(RFC bar 数未钉死); 参数事先固定未调; "
          "SIM 无市场冲击; fees 用实际估算代理。上线仍须前瞻配对影子+预注册+用户裁(§7A)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
