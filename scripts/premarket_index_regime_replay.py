#!/usr/bin/env python3
"""盘前指数 regime 只读回放 (DEB-20260924 Phase 0; 零副作用, 不下单/不写指令/不改任何开关)。

目的: 回答「盘前指数普跌日, DA 的低吸 BUY 是否系统性更差」——用**历史回放**而非前向攒样
      (用户既有偏好: 历史可重放时直接回测裁决; 攒样只留给输入不存在于历史的新组件)。

特征 (每交易日一格, 冻结时点 ET 08:00 = DA 实跑时刻; 另附 09:25 近开盘变体作敏感性):
- qqq_pre_chg_0800 / soxx_pre_chg_0800 / spy_pre_chg_0800
  = 当日 08:00 ET 5m bar close vs **上一交易日 15:55** close (%)
数据源: 富途历史 5m K 线 (session=ALL, 含盘前段), 只读。

样本与结局 (双列口径, DEB 要求):
- 指令层 (母体): playbook_instructions direction='BUY' 窗口内全部, 结局= fwd_return_intraday /
  fwd_return_t1_adj (**毛**, 分数 → 打印为 %)
- 成交层: 按 decision_id 关联 ftseek_us.db.trade_records → realized_pnl(**毛**$) / pnl_net(**净**$)
  费用线由 fees_est 实测给出 (净=毛−费)

预注册判据 (DEB §四 P1, 见数前写死, 本脚本只**报读数不判决**):
  只有当最普跌桶相对其余桶 净均差 ≤ 0 ∧ 双窗同向 ∧ 该桶 n ≥ 30 才进入下一次裁决;
  否则记录「无判别力」并关闭该轴。本脚本不修改任何交易行为。

用法:
  cd /opt/ftseek/FTSeek-US && PYTHONPATH=. ./venv/bin/python3.11 scripts/premarket_index_regime_replay.py \
      --since 2026-08-18 --until 2026-09-24 [--out data/regime_replay_<date>.json]

回滚: 删本文件 (+ 删 --out 产物)。无 cron / 无消费者 / 不改任何 config。
"""
import argparse
import collections
import datetime as _dt
import json
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "ftseek_us.db")
PY = os.path.join(ROOT, "venv", "bin", "python3.11")
KLINE = "/root/.hermes/skills/futuapi/scripts/quote/get_kline.py"
INDICES = ("US.QQQ", "US.SOXX", "US.SPY")
# 预注册档位 (见数前定义; 主判据用连续分桶, 这三档只作机制诊断)
PREREG = {
    "QQQ<=-0.5": lambda q, s: q <= -0.5,
    "QQQ<=-0.8 & SOXX<=-1.5": lambda q, s: q <= -0.8 and s <= -1.5,
    "QQQ<=-0.5 & SOXX<=-1.5": lambda q, s: q <= -0.5 and s <= -1.5,
}
W2_START = "2026-09-09"  # 双窗切点 (见数前定; W1 < 切点 ≤ W2)


def _kline(sym, start, end):
    cmd = [PY, KLINE, sym, "--ktype", "5m", "--start", start, "--end", end,
           "--session", "ALL", "--num", "1000", "--json"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    f, l = r.stdout.find("{"), r.stdout.rfind("}")
    if f < 0 or l <= f:
        return []
    try:
        return json.loads(r.stdout[f:l + 1]).get("data") or []
    except json.JSONDecodeError:
        return []


def build_features(since, until):
    """→ {date: {field: value}}, 附完整性哨兵.

    ⚠️ 2026-09-24 首跑修正: `--session ALL` 的日列表含**无 RTH 的日期**(周末/隔夜),
    原先用「日历上前一天」取 15:55 昨收 ⇒ **每个周一都配不到特征**(实测丢 59/327 = 18%,
    丢失日恰为 8/18、8/24、8/31、9/08、9/14、9/21)。改为只在**有 15:55 收盘的交易日**之间链式取昨收。
    """
    feats = collections.defaultdict(dict)
    sentinel = {}
    for sym in INDICES:
        bars = _kline(sym, since, until)
        by_date = collections.defaultdict(dict)
        for b in bars:
            t = str(b.get("time") or "")
            if len(t) >= 16:
                by_date[t[:10]][t[11:16]] = b.get("close")
        trading_prev = [d for d in sorted(by_date) if by_date[d].get("15:55") is not None]
        sentinel[sym] = {"bars": len(bars), "dates_all": len(by_date), "trading_dates": len(trading_prev),
                         "first": trading_prev[0] if trading_prev else None,
                         "last": trading_prev[-1] if trading_prev else None}
        key = sym.split(".")[1].lower()
        # 候选日 = **有 08:00 盘前 bar 的日** (含当日尚未收盘者: 它自己的 15:55 还不在, 但特征仍可算);
        # 昨收 = 该日之前**最近一个有 15:55 收盘的交易日** (跨周末/假日正确)。
        for d in sorted(by_date):
            if by_date[d].get("08:00") is None:
                continue
            prev = [t for t in trading_prev if t < d]
            if not prev:
                continue
            pc = by_date[prev[-1]]["15:55"]
            for hhmm, tag in (("08:00", "0800"), ("09:25", "0925")):
                px = by_date[d].get(hhmm)
                if pc and px:
                    feats[d][f"{key}_pre_chg_{tag}"] = round((px - pc) / pc * 100, 3)
    return feats, sentinel


def load_instructions(since, until):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        """select date, symbol, decision_id, fwd_return_intraday, fwd_return_t1_adj,
                  outcome, pnl_pct, confidence, model_used
           from playbook_instructions
           where direction='BUY' and date>=? and date<=?""", (since, until))]
    tr = {r["decision_id"]: dict(r) for r in c.execute(
        """select decision_id, realized_pnl, pnl_net, fees_est, exit_reason
           from trade_records where decision_id is not null and decision_id!=''""")}
    for r in rows:
        t = tr.get(r.get("decision_id") or "", {})
        r["realized_pnl"] = t.get("realized_pnl")
        r["pnl_net"] = t.get("pnl_net")
        r["fees_est"] = t.get("fees_est")
    c.close()
    return rows


def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return (sum(xs) / len(xs)) if xs else None


def _median(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _worst5(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    if not xs:
        return None
    k = max(1, round(len(xs) * 0.05))
    return sum(xs[:k]) / k


def day_cluster_boot(rows_a, rows_b, key="fwd_return_intraday", B=2000, seed=20260924):
    """日聚类 bootstrap (两独立样本, 重抽单位=交易日; DEB-20260805 §1 族① + MDE 纪律).

    ⚠️ 同一天的所有指令共享同一市场环境 ⇒ 指令级独立同分布**不成立**, 必须在**日**层面重抽。
    返回 (diff, ci_lo, ci_hi, p_two_sided, n_days_a, n_days_b); 全为**日聚类均值差**口径。
    """
    import random
    rnd = random.Random(seed)

    def by_day(rows):
        d = collections.defaultdict(list)
        for r in rows:
            v = r.get(key)
            if isinstance(v, (int, float)):
                d[r["date"]].append(v)
        return {k: sum(v) / len(v) for k, v in d.items()}

    a, b = by_day(rows_a), by_day(rows_b)
    if len(a) < 2 or len(b) < 2:
        return None
    da, db = list(a.values()), list(b.values())
    ma, mb = sum(da) / len(da), sum(db) / len(db)
    diffs = []
    for _ in range(B):
        sa = [da[rnd.randrange(len(da))] for _ in range(len(da))]
        sb = [db[rnd.randrange(len(db))] for _ in range(len(db))]
        diffs.append(sum(sa) / len(sa) - sum(sb) / len(sb))
    diffs.sort()
    lo, hi = diffs[int(B * 0.025)], diffs[int(B * 0.975)]
    obs = ma - mb
    p = 2 * min(sum(1 for d in diffs if d <= 0) / B, sum(1 for d in diffs if d >= 0) / B)
    return {"diff_day_mean": round(100 * obs, 3), "ci95_pct": [round(100 * lo, 3), round(100 * hi, 3)],
            "p_two_sided": round(p, 4), "n_days_a": len(a), "n_days_b": len(b),
            "includes_zero": lo <= 0 <= hi}


def spearman(idx, vals):
    """桶序号 vs 桶均值的秩相关 (单调性检查, 非显著性检验)."""
    pairs = [(i, v) for i, v in zip(idx, vals) if isinstance(v, (int, float))]
    if len(pairs) < 3:
        return None
    def rank(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        rk = [0.0] * len(xs)
        for pos, i in enumerate(order):
            rk[i] = pos + 1
        return rk
    a = rank([p[0] for p in pairs]); b = rank([p[1] for p in pairs])
    n = len(pairs)
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return round(num / den, 3) if den else None


def summarize(label, rows):
    g = lambda k: [r[k] for r in rows]
    n_exec = sum(1 for r in rows if r.get("pnl_net") is not None)
    w1 = [r for r in rows if r["date"] < W2_START]
    w2 = [r for r in rows if r["date"] >= W2_START]
    d = {
        "bucket": label, "n": len(rows), "days": len({r["date"] for r in rows}),
        "fwd_intraday_mean_pct": None, "fwd_t1_mean_pct": None,
        "fwd_intraday_median_pct": None, "fwd_intraday_worst5_pct": None,
        "n_exec": n_exec, "realized_pnl_mean": _mean(g("realized_pnl")),
        "pnl_net_mean": _mean(g("pnl_net")), "fees_mean": _mean(g("fees_est")),
        "w1_n": len(w1), "w1_fwd_intraday_mean_pct": _mean([r["fwd_return_intraday"] for r in w1]),
        "w2_n": len(w2), "w2_fwd_intraday_mean_pct": _mean([r["fwd_return_intraday"] for r in w2]),
        "outcomes": dict(collections.Counter(r["outcome"] for r in rows)),
    }
    fi = [r["fwd_return_intraday"] for r in rows]
    if any(isinstance(x, (int, float)) for x in fi):
        d["fwd_intraday_mean_pct"] = round(100 * _mean(fi), 3)
        d["fwd_intraday_median_pct"] = round(100 * _median(fi), 3)
        d["fwd_intraday_worst5_pct"] = round(100 * _worst5(fi), 3)
    t1 = _mean([r["fwd_return_t1_adj"] for r in rows])
    d["fwd_t1_mean_pct"] = round(100 * t1, 3) if t1 is not None else None
    for k in ("realized_pnl_mean", "pnl_net_mean", "fees_mean"):
        if d[k] is not None:
            d[k] = round(d[k], 2)
    for k in ("w1_fwd_intraday_mean_pct", "w2_fwd_intraday_mean_pct"):
        if d[k] is not None:
            d[k] = round(100 * d[k], 3)
    same_dir = None
    if d["w1_fwd_intraday_mean_pct"] is not None and d["w2_fwd_intraday_mean_pct"] is not None:
        same_dir = (d["w1_fwd_intraday_mean_pct"] < 0) == (d["w2_fwd_intraday_mean_pct"] < 0)
    d["two_windows_same_direction"] = same_dir
    return d


def fmt(rows, keys, hdr=None):
    print(" | ".join(hdr or keys))
    for r in rows:
        print(" | ".join(str(r.get(k)) for k in keys))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-18")
    ap.add_argument("--until", default=_dt.datetime.utcnow().strftime("%Y-%m-%d"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    # 特征取数窗口前推 10 个日历日: 窗口首日的"上一交易日 15:55"须在取数范围内,
    #   否则首日必然缺特征 (实测丢 17 条 = 8/18 全部)。指令/结局仍按 --since 过滤。
    _fs = (_dt.date.fromisoformat(a.since) - _dt.timedelta(days=10)).isoformat()
    feats, sentinel = build_features(_fs, a.until)
    rows = load_instructions(a.since, a.until)
    matched = [r for r in rows if r["date"] in feats]
    dropped = collections.Counter(r["date"] for r in rows if r["date"] not in feats)
    print(f"[母体] BUY 指令 {len(rows)} 条 / {len({r['date'] for r in rows})} 日; "
          f"特征命中 {len(matched)} 条; 未配到特征 {len(rows) - len(matched)} 条 {dict(dropped)}")
    print(f"[完整性哨兵] {json.dumps(sentinel, ensure_ascii=False)}")
    if not matched:
        print("!! 特征为空 — 先查 K 线取数, 不要下结论")
        return 1

    # ── 连续分桶 (主判据): qqq_pre_chg_0800 五分位 ──
    qs = sorted(r["date"] for r in matched)
    uniq_days = sorted(set(qs))
    f_q = {d: feats[d].get("qqq_pre_chg_0800") for d in uniq_days}
    dayvals = sorted(v for v in f_q.values() if v is not None)
    cuts = [dayvals[int(len(dayvals) * p)] for p in (0.2, 0.4, 0.6, 0.8)] if len(dayvals) >= 5 else []
    def qbucket(v):
        if v is None:
            return None
        b = 0
        for c in cuts:
            if v > c:
                b += 1
        return b
    buckets = collections.defaultdict(list)
    for r in matched:
        b = qbucket(feats[r["date"]].get("qqq_pre_chg_0800"))
        if b is not None:
            buckets[b].append(r)
    quint = [summarize(f"Q{b+1}(QQQ盘前)", buckets[b]) for b in sorted(buckets)]
    print("\n== 主判据: QQQ 08:00 盘前涨跌 五分位 ==")
    fmt(quint, ["bucket", "n", "days", "fwd_intraday_mean_pct", "fwd_t1_mean_pct",
                "fwd_intraday_median_pct", "fwd_intraday_worst5_pct", "n_exec",
                "realized_pnl_mean", "pnl_net_mean", "w1_fwd_intraday_mean_pct",
                "w2_fwd_intraday_mean_pct", "two_windows_same_direction"])
    rho = spearman([i for i in range(len(quint))],
                   [q["fwd_intraday_mean_pct"] for q in quint])
    print(f"单调性 (桶序号 vs 均值秩相关) rho={rho}  [|rho|>=0.5 才算形状成立]")

    # ── 主对比: 最普跌桶(Q1) vs 其余四桶, 日聚类 + bootstrap CI (决策级读数) ──
    q1 = buckets[min(buckets)]
    rest_q = [r for b in sorted(buckets) if b != min(buckets) for r in buckets[b]]
    boot_fwd = day_cluster_boot(q1, rest_q, "fwd_return_intraday")
    boot_t1 = day_cluster_boot(q1, rest_q, "fwd_return_t1_adj")
    boot_net = day_cluster_boot(q1, rest_q, "pnl_net")
    net_q1 = [r["pnl_net"] for r in q1 if r.get("pnl_net") is not None]
    net_r = [r["pnl_net"] for r in rest_q if r.get("pnl_net") is not None]
    print("\n== 主对比: Q1(最普跌五分位) vs 其余四桶 ==")
    print(f"  毛(日内) 日聚类均值差 = {boot_fwd['diff_day_mean']}pp "
          f"CI95={boot_fwd['ci95_pct']} p={boot_fwd['p_two_sided']} "
          f"(日数 {boot_fwd['n_days_a']} vs {boot_fwd['n_days_b']}, 含0={boot_fwd['includes_zero']})")
    print(f"  毛(T+1调) 日聚类均值差 = {boot_t1['diff_day_mean']}pp CI95={boot_t1['ci95_pct']} "
          f"p={boot_t1['p_two_sided']} (含0={boot_t1['includes_zero']})")
    print(f"  净($/成交笔, 指令级) Q1={round(_mean(net_q1),2) if net_q1 else None} "
          f"vs 其余={round(_mean(net_r),2) if net_r else None} (n={len(net_q1)} vs {len(net_r)})")
    if boot_net:
        print(f"  净($) 日聚类均值差 = {boot_net['diff_day_mean']} CI95={boot_net['ci95_pct']} "
              f"p={boot_net['p_two_sided']} (日数 {boot_net['n_days_a']} vs {boot_net['n_days_b']}, "
              f"含0={boot_net['includes_zero']})")
    print(f"  预注册三条件自检: 净均差<=0 → {(bool(net_q1) and bool(net_r) and _mean(net_q1) <= _mean(net_r))}; "
          f"双窗同向 → {quint[0].get('two_windows_same_direction')}; 桶内 n>=30 → {len(q1) >= 30}")

    # ── 预注册档位 (机制诊断) ──
    print("\n== 机制诊断: 预注册档位 (命中 vs 其余) ==")
    raw = {}
    for name, fn in PREREG.items():
        hit = [r for r in matched
               if feats[r["date"]].get("qqq_pre_chg_0800") is not None
               and fn(feats[r["date"]]["qqq_pre_chg_0800"], feats[r["date"]].get("soxx_pre_chg_0800", 0))]
        rest = [r for r in matched if r not in hit]
        raw[name] = {"n_days": len({r["date"] for r in hit}), "hit": summarize(name, hit),
                     "rest": summarize("其余", rest)}
        print(f"-- {name}: 命中日 {raw[name]['n_days']} / 指令 {len(hit)}")
        fmt([raw[name]["hit"], raw[name]["rest"]],
            ["bucket", "n", "fwd_intraday_mean_pct", "fwd_t1_mean_pct", "n_exec",
             "realized_pnl_mean", "pnl_net_mean", "w1_fwd_intraday_mean_pct",
             "w2_fwd_intraday_mean_pct", "two_windows_same_direction"])

    out = {"schema": "regime_replay/1", "since": a.since, "until": a.until,
           "window": f"{a.since}..{a.until}",
           "universe": "playbook_instructions direction=BUY (母体) + trade_records 按 decision_id 关联 (成交层)",
           "truncation_checked": {"kline_sentinel": sentinel, "unmatched_instructions": dict(dropped)},
           "n_mother": len(rows), "n_matched": len(matched), "days": uniq_days,
           "feature_primary": "qqq_pre_chg_0800 (当日 08:00 ET vs 上一交易日 15:55, %)",
           "quintiles": quint, "monotonic_rho": rho,
           "primary_contrast_Q1_vs_rest": {"fwd_intraday": boot_fwd, "fwd_t1_adj": boot_t1,
                                           "pnl_net_mean_q1": round(_mean(net_q1), 2) if net_q1 else None,
                                           "pnl_net_mean_rest": round(_mean(net_r), 2) if net_r else None,
                                           "n_exec_q1": len(net_q1), "n_exec_rest": len(net_r)},
           "prereg": raw,
           "notes": ["毛=fwd_return_* / realized_pnl; 净=pnl_net; 费用由 fees_est 实测",
                     "本脚本只报读数不判决; 判据见 DEB-20260924 §四 P1 (预注册)"]}
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"\n[产物] {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())