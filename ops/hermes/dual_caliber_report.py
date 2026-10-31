#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dc双口径对比推送 (2026-08-27, no_agent cron 用)。

数据源: dc_calibration_samples.jsonl (盘后cron 05:00 Step1.6 更新, 本脚本 05:40 读)。
日模式(默认): 新增"收盘口径×策略退出"双口径配对逐笔 + 累计对比; 非交易日→静默;
             无新配对→一行简报。
周模式(--weekly): 最近7天窗口逐笔全量 + 周聚合 + 变体反事实排行 + 环比上周。

空stdout = 不推送 (no_agent 静默语义)。
状态文件 dual_caliber_state.json 记已推配对/历史聚合 (幂等, 漏跑自愈补推)。

2026-09-23: 截留率改报 Σ/Σ 为主 + 中位 (原 mean(strat/close) 被小分母笔拉飞,
  同一子集 均值0.47 / 中位0.11 / Σ/Σ0.19); 上游 collector 同步改口径。
  历史 state 里 2026-09-23 及以前的 capture 是均值比口径, 不与新值比较。
2026-10-31 (周报两项可读性修复, 零交易行为变更):
  ① 变体反事实表补「实盘同子集」臂: 原表只印变体自身 gross%, 读者会读成
     「变体更赚 +1.31%」; 实测补臂后 12 个变体 Δ=变体−实盘 全在 ±0.08pp、
     |t|<1 ⇒ 退出参数收紧方向无杠杆 (是负面结果, 不是可用杠杆)。
     实盘毛% = strategy_net_pct(净) + fees_est/notional×100, 需读生产库 (只读,
     失败则降级为旧表并标注, 不阻断推送)。
  ② 周窗口改互斥: 原 cutoff=max_d−7d 且按 > cutoff 过滤, 上一期 week_end 那天
     会被两周同时计入 (实测 10-22 的 8 笔双计, 稀释 Δ上周)。现起点取 state 里
     上一期 week_end (互斥); 缺失/超 14 天回退 7 日历日。标题印实际交易日数,
     待回填拆「窗内 / 窗后顺延」。
"""
import json
import pathlib
import sqlite3
import statistics
import subprocess
import sys
from datetime import datetime, timedelta

SAMPLES = pathlib.Path(
    "/root/.hermes/skills/deep-stock-analysis/scripts/dc_calibration_samples.jsonl")
HERE = pathlib.Path(__file__).resolve().parent
STATE = HERE / "dual_caliber_state.json"
FTSEEK = "/opt/ftseek/FTSeek-US"
TRADE_DB = FTSEEK + "/data/ftseek_us.db"


def trading_day() -> bool:
    """复用 FTSeek 交易日检查; 检查失败时宁推勿漏。"""
    try:
        r = subprocess.run(
            [FTSEEK + "/venv/bin/python3.11",
             FTSEEK + "/scripts/trading_day_check.py", "--market", "US"],
            capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return True


def load_samples():
    rows = []
    try:
        for line in SAMPLES.open(encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return rows


def complete_pairs(rows):
    """双口径完整配对: strategy_net_pct(策略净落袋%) + fwd_ret_t1_entry(收盘口径) 均在。"""
    out = []
    for s in rows:
        if s.get("strategy_net_pct") is None or s.get("fwd_ret_t1_entry") is None:
            continue
        p = dict(s)
        p["close_pct"] = p["fwd_ret_t1_entry"] * 100.0
        p["strat_pct"] = p["strategy_net_pct"]
        p["agree"] = (p["close_pct"] > 0) == (p["strat_pct"] > 0)
        out.append(p)
    out.sort(key=lambda p: (p["trade_date"], p["symbol"]))
    return out


def agg(pairs):
    if not pairs:
        return None
    n = len(pairs)
    # 2026-09-23 口径修正: 截留率原为 mean(v_i/d_i) — 小分母笔拉飞 (实测同一子集
    #   均值 0.47 / 中位 0.11 / Σ/Σ 0.19)。改报 Σ/Σ 为主 + 中位, 两者并存。
    pos = [p for p in pairs if p["close_pct"] > 0.1]
    capture = capture_med = None
    if pos:
        capture = round(sum(p["strat_pct"] for p in pos) / sum(p["close_pct"] for p in pos), 2)
        capture_med = round(statistics.median(p["strat_pct"] / p["close_pct"] for p in pos), 2)
    return {
        "n": n,
        "close_mean": round(sum(p["close_pct"] for p in pairs) / n, 3),
        "strat_mean": round(sum(p["strat_pct"] for p in pairs) / n, 3),
        "close_wr": round(sum(1 for p in pairs if p["close_pct"] > 0) / n, 3),
        "strat_wr": round(sum(1 for p in pairs if p["strat_pct"] > 0) / n, 3),
        "agree_pct": round(sum(1 for p in pairs if p["agree"]) / n * 100, 1),
        "capture": capture,          # Σ/Σ 口径 (2026-09-23 起; 此前状态文件里存的是均值比)
        "capture_med": capture_med,
        "capture_n": len(pos),
    }


def fmt_agg(a, label=""):
    if not a:
        return f"{label}无数据"
    cap = ""
    if a.get("capture") is not None:
        cap = f" | 截留率Σ/Σ {a['capture']}"
        if a.get("capture_med") is not None:
            cap += f"(中位{a['capture_med']})"
    return (f"{label}n={a['n']} | 收盘口径 {a['close_mean']:+.2f}%/WR{a['close_wr']*100:.0f}%"
            f" vs 策略退出 {a['strat_mean']:+.2f}%/WR{a['strat_wr']*100:.0f}%"
            f" | 方向一致 {a['agree_pct']:.0f}%{cap}")


def trade_row(p):
    mark = "✅" if p["agree"] else "❌"
    return (f"| {p['trade_date'][5:]} | {p['symbol'].replace('US.', '')} | {p['direction']} "
            f"| {p.get('exit_reason') or '-'} | {p['strat_pct']:+.2f}% "
            f"| {p['close_pct']:+.2f}% | {mark} |")


# A5 (2026-10-06 推送治理批): 末列原写「方向」, 语义是**两口径是否同号**
#   (L67: agree = (close_pct>0) == (strat_pct>0)), 不是「本笔方向判对」。
#   实测歧义: SNPS 收盘口径 −1.34% 标 ✅ 与 AMD 收盘 −0.43% 标 ❌ 并列 ⇒
#   读者会把 ✅ 读成「这笔看对方向了」, 而它只表示「策略退出与收盘口径同为负」。
#   改列名不动判定逻辑 (逻辑本身正确), 纯文案消歧。
#   第3列「方向」= direction (LONG/SHORT) 保持原义, 与本列不同物。
TABLE_HEAD = ("|日期|标的|方向|退出|策略净|收盘口径|同号|\n"
              "|---|---|---|---|---|---|---|")


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"seen": {}, "daily": {}, "weekly": []}


def save_state(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False))


def _live_gross_map(pairs):
    """实盘同子集「毛%」映射: decision_id → (净+费)/名义×100。

    变体 profit_pct 是 gross%, 必须与实盘 gross 同口径才可比。净额已在样本里
    (strategy_net_pct), 但费率不在样本字段内 ⇒ 只读生产库取 fees_est/notional。
    报表路径: 任何异常都降级为 {} (调用方退回旧表并标注), 绝不阻断推送。
    """
    ids = sorted({p.get("decision_id") for p in pairs
                  if p.get("decision_id") and p.get("strategy_net_pct") is not None})
    if not ids:
        return {}
    out = {}
    try:
        con = sqlite3.connect("file:" + TRADE_DB + "?mode=ro", uri=True, timeout=10)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ("SELECT decision_id, "
                 "SUM(COALESCE(fees_est,0)), SUM(entry_price*quantity) "
                 "FROM trade_records WHERE decision_id IN (%s) "
                 "GROUP BY decision_id" % ",".join("?" * len(chunk)))
            for did, fee, nom in con.execute(q, chunk):
                if nom and nom > 0:
                    out[did] = (fee or 0.0) / nom * 100.0
        con.close()
    except Exception:
        return {}
    # (fee/notional%) 拼上样本里的净% = 毛%
    for p in pairs:
        fp = out.get(p.get("decision_id"))
        if fp is not None and p.get("strategy_net_pct") is not None:
            p["live_gross_pct"] = p["strategy_net_pct"] + fp
    return out


def variant_table(pairs, week, top=6):
    """变体反事实: 变体自身 mean 必须并列实盘同子集 mean 才有判读价值。

    2026-10-31: 原实现只印变体利润均值并按它排序 ⇒ 「+1.31%」会被读成
    「变体比实盘多赚 1.31pp」。补实盘臂后实测 12 变体 Δ 全在 ±0.08pp (|t|<1)：
    真正该读的是 Δ 与 t，不是变体绝对值。
    """
    stat = {}
    for p in pairs:
        for vn, v in (p.get("shadow_variants") or {}).items():
            if isinstance(v, dict) and v.get("profit_pct") is not None:
                stat.setdefault(vn, []).append(p)
    if not stat:
        return ""
    _live_gross_map(pairs)  # 补实盘毛% 到样本上; 失败返回 {}, 下方自动降级
    week_n = {}
    for p in week:
        for vn, v in (p.get("shadow_variants") or {}).items():
            if isinstance(v, dict) and v.get("profit_pct") is not None:
                week_n[vn] = week_n.get(vn, 0) + 1

    rows, degraded = [], False
    for vn, ps in stat.items():
        both = [p for p in ps if p.get("live_gross_pct") is not None]
        if not both:
            degraded = True
            rows.append((vn, len(ps), sum(p["shadow_variants"][vn]["profit_pct"] for p in ps) / len(ps),
                         None, None, None, week_n.get(vn, 0)))
            continue
        diffs = [p["shadow_variants"][vn]["profit_pct"] - p["live_gross_pct"] for p in both]
        t = None
        if len(diffs) > 1:
            sd = statistics.stdev(diffs)
            if sd > 0:
                t = (sum(diffs) / len(diffs)) / (sd / len(diffs) ** 0.5)
        rows.append((vn, len(both),
                     sum(p["shadow_variants"][vn]["profit_pct"] for p in both) / len(both),
                     sum(p["live_gross_pct"] for p in both) / len(both),
                     sum(diffs) / len(diffs), t, week_n.get(vn, 0)))

    # 排序: 有 Δ 的按 Δ 降序 (最值得看的在最上), 缺 Δ 的沉底
    rows.sort(key=lambda r: (r[4] is None, -(r[4] or 0), -r[1]))
    L = ["**变体反事实**(累计触发子集; 变体恒比实盘更紧; gross%):",
         "|变体|累计n|变体均值|实盘同子集|Δpp|t|本周n|",
         "|---|---|---|---|---|---|---|"]
    for vn, n, vm, lm, dm, t, wn in rows[:top]:
        if dm is None:
            L.append(f"| {vn} | {n} | {vm:+.2f}% | — | — | — | {wn} |")
        else:
            ts = f"{t:+.2f}" if t is not None else "—"
            L.append(f"| {vn} | {n} | {vm:+.2f}% | {lm:+.2f}% | {dm:+.2f} | {ts} | {wn} |")
    if degraded:
        L.append("Δ 列不可用(生产库读取失败), 本表只列变体自身收益 —— **不可**据此判变体优劣。")
    else:
        L.append("Δ = 变体毛% − 实盘毛%(净+费)。|t|<2 即与实盘不可分辨 —— 变体均值高 "
                 "不代表它更好, 只看 Δ/t。")
    return "\n".join(L)


def daily():
    if not trading_day():
        return ""
    rows = load_samples()
    pairs = complete_pairs(rows)
    a = agg(pairs)
    if a is None:
        return ""
    pending = sum(1 for s in rows if s.get("strategy_net_pct") is not None
                  and s.get("fwd_ret_t1_entry") is None)
    st = load_state()
    seen = st.get("seen", {})
    new = [p for p in pairs if seen.get(p["key"]) != round(p["strat_pct"], 4)]
    today = datetime.now().strftime("%Y-%m-%d")
    prev_dates = [d for d in st.get("daily", {}) if d < today]
    prev = st.get("daily", {}).get(max(prev_dates)) if prev_dates else None
    st["seen"] = {p["key"]: round(p["strat_pct"], 4) for p in pairs}
    st.setdefault("daily", {})[today] = a
    st["daily"] = {k: v for k, v in sorted(st["daily"].items())[-14:]}
    save_state(st)

    L = [f"📊 **dc双口径日对比** ({today})",
         fmt_agg(a, "累计 "),
         f"待T+1回填(已成交未齐): {pending}"]
    if prev:
        L.append(f"Δ昨日: 配对{a['n']-prev['n']:+d}"
                 f" | 收盘口径 {a['close_mean']-prev['close_mean']:+.2f}pp"
                 f" | 策略退出 {a['strat_mean']-prev['strat_mean']:+.2f}pp"
                 f" | 方向一致 {a['agree_pct']-prev['agree_pct']:+.0f}pp")
    if new:
        show = new[-20:]
        more = f"，仅示最近{len(show)}" if len(new) > len(show) else ""
        L.append(f"\n**新增/更新记录 ({len(new)}{more})**:")
        L.append(TABLE_HEAD)
        L.extend(trade_row(p) for p in show)
    else:
        L.append("今日无新增双口径配对。")
    return "\n".join(L)


def _week_start(prev, max_d):
    """周窗口起点 (不含): 优先接上一期 week_end ⇒ 两期互斥, 不重不漏。

    原实现起点 = max_d−7 天且按 > 起点过滤 ⇒ 上一期 week_end 那天 (实为上一期
    窗口末日) 会被两周同时计入 (实测 2026-10-22 的 8 笔双计, 稀释 Δ上周)。
    缺失上期 / 上期早于 14 天 (漏跑多周) 时回退 7 日历日, 避免窗口无限拉长。
    同期重跑 (days=0, 上期窗末就是最新配对日) 也走「接上期」⇒ 窗口为空 ⇒ 静默不推,
    这是有意的: 半周内重复触发 cron/手工重跑不应再推一遍同一批数据。
    """
    if prev and prev.get("week_end"):
        try:
            d0 = datetime.strptime(prev["week_end"], "%Y-%m-%d")
            if 0 <= (datetime.strptime(max_d, "%Y-%m-%d") - d0).days <= 14:
                return prev["week_end"], "接上期"
        except Exception:
            pass
    return ((datetime.strptime(max_d, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d"),
            "回退7日历日")


def weekly():
    rows = load_samples()
    pairs = complete_pairs(rows)
    if not pairs:
        return ""
    max_d = max(p["trade_date"] for p in pairs)
    st = load_state()
    hist = st.get("weekly", [])
    prev = hist[-1] if hist else None
    start, why = _week_start(prev, max_d)
    week = [p for p in pairs if p["trade_date"] > start]
    if not week:
        return ""  # 无新配对 → 静默 (no_agent 语义), 不推进 state
    wa = agg(week)
    sessions = sorted({p["trade_date"] for p in week})
    pend_in = sum(1 for s in rows if s.get("strategy_net_pct") is not None
                  and s.get("fwd_ret_t1_entry") is None
                  and start < s.get("trade_date", "") <= max_d)
    pend_after = sorted({s.get("trade_date", "") for s in rows
                         if s.get("strategy_net_pct") is not None
                         and s.get("fwd_ret_t1_entry") is None
                         and s.get("trade_date", "") > max_d})
    hist.append({"week_end": max_d, **(wa or {})})
    st["weekly"] = hist[-12:]
    save_state(st)

    L = [f"📈 **dc双口径周对比** (窗口 {start}<日期≤{max_d} · {len(sessions)} 个交易日 · {why})",
         fmt_agg(wa, "本周 "),
         fmt_agg(agg(pairs), "累计 "),
         f"待T+1回填: 窗内 {pend_in} 笔 | 窗后顺延下期 {len(pend_after)} 笔"
         + (f" ({'/'.join(pend_after[:4])})" if pend_after else "")]
    if prev and prev.get("n"):
        L.append(f"Δ上期: 配对{wa['n']-prev['n']:+d}"
                 f" | 收盘口径 {wa['close_mean']-prev['close_mean']:+.2f}pp"
                 f" | 策略退出 {wa['strat_mean']-prev['strat_mean']:+.2f}pp"
                 f" | 方向一致 {wa['agree_pct']-prev['agree_pct']:+.0f}pp"
                 f" (上期窗末 {prev.get('week_end')}, 两期互斥)")
    L.append(f"\n**本周逐笔记录 ({len(week)})**:")
    L.append(TABLE_HEAD)
    L.extend(trade_row(p) for p in week)
    vt = variant_table(pairs, week)
    if vt:
        L.append("\n" + vt)
    return "\n".join(L)


def main(argv):
    out = weekly() if "--weekly" in argv else daily()
    if out:
        print(out)


if __name__ == "__main__":
    main(sys.argv[1:])
