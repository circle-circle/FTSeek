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
"""
import json
import pathlib
import statistics
import subprocess
import sys
from datetime import datetime, timedelta

SAMPLES = pathlib.Path(
    "/root/.hermes/skills/deep-stock-analysis/scripts/dc_calibration_samples.jsonl")
HERE = pathlib.Path(__file__).resolve().parent
STATE = HERE / "dual_caliber_state.json"
FTSEEK = "/opt/ftseek/FTSeek-US"


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


def variant_table(pairs, top=6):
    stat = {}
    for p in pairs:
        for vn, v in (p.get("shadow_variants") or {}).items():
            if isinstance(v, dict) and v.get("profit_pct") is not None:
                stat.setdefault(vn, []).append(v["profit_pct"])
    if not stat:
        return ""
    lines = []
    for vn, vs in sorted(stat.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))[:top]:
        lines.append(f"| {vn} | {len(vs)} | {sum(vs)/len(vs):+.2f}% |")
    return ("**变体反事实**(触发子集, 变体恒比实盘更紧, gross%):\n"
            "|变体|触发笔|触发均值|\n|---|---|---|\n" + "\n".join(lines))


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


def weekly():
    rows = load_samples()
    pairs = complete_pairs(rows)
    if not pairs:
        return ""
    max_d = max(p["trade_date"] for p in pairs)
    cutoff = (datetime.strptime(max_d, "%Y-%m-%d")
              - timedelta(days=7)).strftime("%Y-%m-%d")
    week = [p for p in pairs if p["trade_date"] > cutoff]
    wa = agg(week)
    pend_week = sum(1 for s in rows if s.get("strategy_net_pct") is not None
                    and s.get("fwd_ret_t1_entry") is None
                    and s.get("trade_date", "") > cutoff)
    st = load_state()
    hist = st.get("weekly", [])
    prev = hist[-1] if hist else None
    hist.append({"week_end": max_d, **(wa or {})})
    st["weekly"] = hist[-12:]
    save_state(st)

    L = [f"📈 **dc双口径周对比** (截至 {max_d} 的7天)",
         fmt_agg(wa, "本周 "),
         fmt_agg(agg(pairs), "累计 "),
         f"本周成交待T+1回填: {pend_week} (周四/五的配对下周一/三才齐)"]
    if prev and prev.get("n"):
        L.append(f"Δ上周: 配对{wa['n']-prev['n']:+d}"
                 f" | 收盘口径 {wa['close_mean']-prev['close_mean']:+.2f}pp"
                 f" | 策略退出 {wa['strat_mean']-prev['strat_mean']:+.2f}pp"
                 f" | 方向一致 {wa['agree_pct']-prev['agree_pct']:+.0f}pp")
    L.append(f"\n**本周逐笔记录 ({len(week)})**:")
    L.append(TABLE_HEAD)
    L.extend(trade_row(p) for p in week)
    vt = variant_table(week)
    if vt:
        L.append("\n" + vt)
    return "\n".join(L)


def main(argv):
    out = weekly() if "--weekly" in argv else daily()
    if out:
        print(out)


if __name__ == "__main__":
    main(sys.argv[1:])
