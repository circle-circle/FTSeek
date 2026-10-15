#!/usr/bin/env python3
"""[一次性结算器] ue_short_boost_shadow (UE×SHORT ×1.25 加码假设) 冻结样本终局结算 (2026-10-15).

背景: 写方 (UE 直写 inject 链 llm_universe_scan) 已随 2026-10-06 cron 摘除 (UE fresh-event
改造) 而退役, 产物 data/ue_short_boost_shadow.jsonl 冻结于 2026-09-28, n=54/60 —
DEB-20260805 §4.3 主门 n=60 在现路径下数学不可达。2026-10-15 用户裁「冻结样本结算后归档」:
按指令级 T+1 方向调整收益口径 (§4.3 主张2 预注册) 对 54 笔冻结样本结算, 结果标
「样本未达门」落档, 注册表转 KILLED。

口径 (逐字对齐预注册):
- 指令级 T+1: ret_T1 = (entry_price − close(T+1)) / entry_price (SELL_SHORT 方向调整,
  价格下跌为正); T+1 = 指令日 (ET date) 后的下一个有日K收盘的交易日。
- 日聚类 (宪章第 9 律): 按 ET 日聚合簇均值后 Welch/单样本 t; pooled 不单独采信。
- MDE80 = 2.802 × se(日簇均值); 声称效应 = 日簇均值。
- 停止规则可判部分: 滚动 T+1 ≤ −1pp @ n≥15 (尾部窗); 借券失败率/vol 档漂移不在
  jsonl 字段内 ⇒ 标「不可判」不伪造。
- ×1.25 只放大名义不改方向性百分比 ⇒ 百分比口径下 boost 与否同读数, 结算回答的是
  「这批候选的 T+1 期望是否为正」(为负则加码只会多亏)。

数据源纪律: 本地日K缓存 `data/replay_bars_daily/US_*.json` ([date, open, high, low, close],
50/50 符号覆盖实测; CRNX/NCSM 缓存止于 08-31 ⇒ 其 9 月行标 missing 不伪造);
**不走 MarketDataRouter** (futu OpenD 槽位与生产共享, 一次性历史结算不抢); yfinance 本机
无网络可达性 (实测 2026-10-15 全符号失败) ⇒ 移除该兜底, 缺数据就如实 missing。

幂等可重跑; 零交易行为; 产物 data/ue_short_boost_final_settlement.json。
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SRC = REPO / "data" / "ue_short_boost_shadow.jsonl"
OUT = REPO / "data" / "ue_short_boost_final_settlement.json"
MIN_N_GATE = 60          # DEB-20260805 §4.3 主门 (冻结 54 ⇒ 永不可达, 只作背景)
MDE_Z = 2.802


def _load_rows():
    return [json.loads(l) for l in SRC.read_text(encoding="utf-8").splitlines() if l.strip()]


def _closes(symbol: str) -> dict:
    """{date: close} — 本地日K缓存 data/replay_bars_daily/US_*.json ([date,o,h,l,c] 升序)。
    无缓存/覆盖不足 ⇒ 返回空 dict, 调用方落 missing (宁可缺数不伪造)。"""
    p = REPO / "data" / "replay_bars_daily" / (symbol.replace(".", "_") + ".json")
    if not p.exists():
        return {}
    return {b[0]: b[4] for b in json.loads(p.read_text(encoding="utf-8"))}


def _t1_close(closes: dict, date: str):
    later = [d for d in closes if d > date]
    return (later[0], closes[later[0]]) if later else (None, None)


def main() -> int:
    rows = _load_rows()
    by_symbol = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r)

    evaluated, missing = [], []
    for sym, rs in sorted(by_symbol.items()):
        closes = _closes(sym)
        for r in rs:
            d, entry = r["date"], r.get("entry_price")
            t1d, t1c = _t1_close(closes, r["date"])
            if not entry or t1c is None:
                missing.append({"symbol": sym, "date": d,
                                "reason": "no_entry" if not entry else "no_t1_close"})
                continue
            evaluated.append({"date": d, "symbol": sym, "entry": entry,
                              "t1_date": t1d, "t1_close": t1c,
                              "ret_t1_pct": round((entry - t1c) / entry * 100, 4)})

    n = len(evaluated)
    rets = [e["ret_t1_pct"] for e in evaluated]
    # 日聚类
    by_day = defaultdict(list)
    for e in evaluated:
        by_day[e["date"]].append(e["ret_t1_pct"])
    day_means = [sum(v) / len(v) for v in by_day.values()]
    nd = len(day_means)
    t_stat = se = mde80 = None
    if nd >= 2:
        m = sum(day_means) / nd
        var = sum((x - m) ** 2 for x in day_means) / (nd - 1)
        se = math.sqrt(var / nd)
        t_stat = (m / se) if se > 0 else None
        mde80 = MDE_Z * se if se > 0 else None
    # 停止规则可判部分: 滚动尾部 T+1 (按日期序最后 15 笔)
    tail15 = [e["ret_t1_pct"] for e in
              sorted(evaluated, key=lambda e: (e["date"], e["symbol"]))[-15:]]

    verdict = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "prereg": "DEB-20260805 §4.3 主张2 (指令级 T+1, 主门 n=60 单侧 α=0.025 δ=2pp)",
        "sample": {"n_rows": len(rows), "n_evaluated": n, "n_missing": len(missing),
                   "missing": missing, "gate_n": MIN_N_GATE,
                   "gate_status": "样本未达门 (冻结 54/60, 写方路径 2026-10-06 已退役 ⇒ 永不可达)"},
        "t1_direction_adjusted_pct": {
            "mean": round(sum(rets) / n, 4) if n else None,
            "median": round(statistics.median(rets), 4) if n else None,
            "win_rate": round(sum(1 for x in rets if x > 0) / n, 4) if n else None,
            "day_clusters": nd,
            "day_clustered_mean": round(sum(day_means) / nd, 4) if nd else None,
            "t_vs_0": round(t_stat, 3) if t_stat is not None else None,
            "mde80": round(mde80, 4) if mde80 is not None else None,
        },
        "stop_rules_evaluable": {
            "rolling_tail15_t1_mean_pct": round(sum(tail15) / len(tail15), 4) if tail15 else None,
            "borrow_fail_and_vol_drift": "不可判 (jsonl 无借券/波动率字段, 不伪造)",
        },
        "disposition": "用户裁「冻结样本结算后归档」 ⇒ 注册表 KILLED; 读数为「样本未达门」"
                       "观测值, 不构成对假设的确认或证伪 (n<60 主门)",
    }
    OUT.write_text(json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"═══ ue_short_boost 冻结样本终局结算 ({verdict['generated_at']}) ═══")
    print(f"样本: {len(rows)} 行冻结 → 可结算 {n} / 缺数据 {len(missing)} | 主门 n=60 永不可达")
    s = verdict["t1_direction_adjusted_pct"]
    print(f"T+1 方向调整: mean={s['mean']}% median={s['median']}% 胜率={s['win_rate']} "
          f"日簇={s['day_clusters']} t={s['t_vs_0']} MDE80={s['mde80']}")
    print(f"尾部15笔均值: {verdict['stop_rules_evaluable']['rolling_tail15_t1_mean_pct']}%")
    print(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
