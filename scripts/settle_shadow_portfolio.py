#!/usr/bin/env python3
"""影子组合结算器 (Qoder, 2026-08-09; 用户指示 sim 放量①).

对 data/shadow_portfolio_entries.jsonl 里"被门拦下但本会触发"的指令做盘后结算:
按生产退出栈的轮询级近似走完当日, 回答"每道门拦掉的到底是钱还是坑"。

退出栈 (与生产对齐, 2026-08-09 部署态):
  初始 SL: dist = max(β × 指令SL%, 1.5%)  (β=sl_tighten_beta, 2026-08-15起=1.0 不打折, DEB§35)
  保本:    mfe ≥ 0.5% 激活, 线 = 成本 ± 0.4% 费垫
  锁利:    激活后峰值回撤 0.8%
  死信号:  持仓 ≥60 bar(≈60min) 且峰值浮盈 <0.2% → 离场
  TP:      指令 take_profit 触及
  EOD:     以上皆未触发 → 收盘最后一根 bar 收盘平
轮询语义: 每 bar 取 (o+c)/2 与 c 两个采样点 (对齐 §二十 模拟器), 不用 bar 内瞬时极值判触发。
记账: 直接名义额法 (gross% × 名义 − fee_model 往返费), 不用比例缩放。

用法:
  venv/bin/python scripts/settle_shadow_portfolio.py            # 结算全部未结算行
  venv/bin/python scripts/settle_shadow_portfolio.py --date 2026-08-09
  venv/bin/python scripts/settle_shadow_portfolio.py --fetch    # 缺 bars 时连 OpenD 拉取
输出: data/shadow_portfolio_outcomes.jsonl + 按 blocked_by 分组的打印摘要。
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENTRIES = os.path.join(_ROOT, "data", "shadow_portfolio_entries.jsonl")
_OUTCOMES = os.path.join(_ROOT, "data", "shadow_portfolio_outcomes.jsonl")
_BAR_DIR = os.path.join(_ROOT, "data", "replay_bars")

BE_PCT, CUSHION_PCT, TRAIL_PCT = 0.5, 0.4, 0.8
DS_MINUTES, DS_MFE = 60, 0.2
# 2026-08-15 (DEB§35): 生产 sl_tighten_beta 0.5→1.0 (用户裁"按标的分桶不打折"),
#   影子同步; SL_FLOOR 保留语义 (β=1.0 时 max() 仅对 <1.5% 的极窄SL兜底).
SL_BETA, SL_FLOOR = 1.0, 1.5


def load_bars(symbol, date):
    p = os.path.join(_BAR_DIR, f"{symbol.replace('.', '_')}_{date}.json")
    if not os.path.exists(p):
        return None
    return json.load(open(p))


def fetch_bars(symbol, date):
    """连 OpenD 拉 1-min 日 K (gate_a_mfe_replay 同款), 落缓存。"""
    try:
        from futu import KLType, RET_OK, AuType, OpenQuoteContext, SysConfig
        import logging
        # futu SDK 的 FTConsoleLog 默认打 stdout (非 stderr): 连接/断开 (reason=CallClose
        # 是 ctx.close() 的正常收尾) 会混进结算输出并被 cron 投递 (2026-09-01 实锤),
        # 压到 WARNING 级别。
        logging.getLogger("FTConsoleLog").setLevel(logging.WARNING)
        SysConfig.enable_proto_encrypt(False)
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        data = None
        for attempt in (1, 2):
            ret, data, _ = ctx.request_history_kline(
                symbol, ktype=KLType.K_1M, start=date, end=date, max_count=1000,
                autype=AuType.NONE)
            if ret == RET_OK and data is not None and len(data) > 0:
                break
            time.sleep(1.2)
        ctx.close()
        if data is None or len(data) == 0:
            return None
        bars = [[str(r.get("time_key", ""))[:19], float(r["open"]), float(r["high"]),
                 float(r["low"]), float(r["close"])] for _, r in data.iterrows()]
        bars.sort(key=lambda b: b[0])
        p = os.path.join(_BAR_DIR, f"{symbol.replace('.', '_')}_{date}.json")
        json.dump(bars, open(p, "w"))
        return bars
    except Exception as e:
        print(f"[warn] fetch {symbol} {date} 失败: {e}", file=sys.stderr)
        return None


def settle(direction, entry_price, bars, sl_dist_pct, tp_price, entry_hm):
    """轮询级退出模拟. 返回 (exit_price, reason, mfe_pct) 或 (None, 'NO_BARS', mfe)."""
    is_long = direction == "LONG"
    base_sl = entry_price * (1 - sl_dist_pct / 100) if is_long else entry_price * (1 + sl_dist_pct / 100)
    evo = 0.0
    peak = entry_price
    held = 0
    started = False
    for ts, o, h, l, c in bars:
        if not started:
            if ts[11:16] >= entry_hm:
                started = True
            else:
                continue
        peak = max(peak, h) if is_long else min(peak, l)
        mfe = ((peak - entry_price) / entry_price * 100 if is_long
               else (entry_price - peak) / entry_price * 100)
        if mfe >= BE_PCT:
            _be = entry_price * (1 + CUSHION_PCT / 100) if is_long else entry_price * (1 - CUSHION_PCT / 100)
            evo = max(evo, _be) if is_long else (min(evo, _be) if evo > 0 else _be)
            _tr = peak * (1 - TRAIL_PCT / 100) if is_long else peak * (1 + TRAIL_PCT / 100)
            evo = max(evo, _tr) if is_long else min(evo, _tr)
        eff = (max(base_sl, evo) if is_long else (min(base_sl, evo) if evo > 0 else base_sl))
        for px in ((o + c) / 2, c):
            if is_long and px <= eff:
                return px, ("SL_TIGHT" if eff == base_sl else "BE_TRAIL"), mfe
            if not is_long and px >= eff:
                return px, ("SL_TIGHT" if eff == base_sl else "BE_TRAIL"), mfe
            if tp_price and ((is_long and px >= tp_price) or (not is_long and px <= tp_price)):
                return px, "TP", mfe
        held += 1
        if held >= DS_MINUTES and mfe < DS_MFE:
            return c, "DEAD_SIGNAL", mfe
    return (bars[-1][4], "EOD", mfe) if started else (None, "NO_BARS", 0.0)


def _load_delayed_inst_ids():
    """executed=1 的指令 id = 拦截后延迟成交 (下一轮重新触发, 真实路径已执行)。
    其影子反事实与真实成交双计, 展示层须从门统计剔除 —— 与
    falsification_gate_weekly_review 的 delayed_executed 独立桶同口径。
    DB 不可读时返回空集 (纯展示层, 退化为不剔除, 不影响结算与写盘)。"""
    try:
        cc = sqlite3.connect(os.path.join(_ROOT, "data", "ftseek_us.db"), timeout=5)
        ids = {r[0] for r in cc.execute(
            "SELECT id FROM playbook_instructions WHERE executed=1")}
        cc.close()
        return ids
    except Exception as e:
        print(f"[warn] 延迟成交剔除降级 (DB 不可读): {e}", file=sys.stderr)
        return set()


def summarize(new_rows, all_rows, delayed_ids, skipped):
    """拼装投递输出 (纯函数, 回归测试 tests/test_shadow_settle_honesty.py)。

    诚实化护栏 (2026-09-15, 用户批准落地; 触发事故 = 09-14 日报):
      1. 延迟成交剔除: 当日 5 笔 vwap_slope 中 DELL/AAOI 实际已走真实路径成交,
         仍被计入"被拦反事实" → 双计。
      2. 单笔主导标记: 当日门合计被 |最大单笔|>80% 主导时打 ⚠ 并点名 —
         实测 n≥3 的门×日 4/5 被单笔主导 (09-14: NOK -259 占 125%), 日符号≠门画像。
      3. 累计 n<30 仅方向 (宪章第 9 律), 判读行不再引导从单日笔均下"松/紧"结论;
         门裁决唯一权威 = 周六预注册周检 (按日聚类 t/MDE, n_真被拦≥30)。
      4. 归因披露: blocked_by 是 (inst_id,date) 先到先占 (_shadow_portfolio_emit),
         实测捕获率 ~87%, 非单门完整总体 (完整总体在 trigger_miss_telemetry)。
    """
    eff_new = [r for r in new_rows if r.get("inst_id") not in delayed_ids]
    n_delayed = len(new_rows) - len(eff_new)
    head = f"新结算 {len(new_rows)} 笔 | 跳过 (缺bars/重复) {skipped}"
    if n_delayed:
        head += f" | 剔除延迟成交 {n_delayed} 笔 (真实路径已执行, 不计反事实)"
    lines = [head]

    by_gate = defaultdict(list)
    for r in eff_new:
        by_gate[r["blocked_by"]].append(r)
    cum_gate = defaultdict(list)
    for r in all_rows:
        if r.get("inst_id") in delayed_ids:
            continue
        cum_gate[r.get("blocked_by")].append(float(r.get("net", 0) or 0))

    flags = []
    if by_gate:
        # pipe 表格左对齐 (行首 4+ 空格会被飞书旧版渲染器当代码块截断, 2026-09-03 实弹)
        lines.append("")
        lines.append("| 门 | 日n | 日净合计 | 笔均 | 胜率 | 累计n | 累计净额 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for g in sorted(by_gate, key=lambda g: sum(r["net"] for r in by_gate[g])):
            rows_g = by_gate[g]
            v = [r["net"] for r in rows_g]
            s = sum(v)
            w = sum(1 for x in v if x > 0)
            flag = ""
            if len(v) >= 2 and s != 0:
                top = max(rows_g, key=lambda r: abs(r["net"]))
                if abs(top["net"]) > 0.8 * abs(s):
                    flag = " ⚠"
                    flags.append(
                        f"⚠ {g}: 当日净额 {abs(top['net'])/abs(s)*100:.0f}% 来自单笔 "
                        f"{top['symbol']} ({top['net']:+.2f}) — 单笔噪声, 非门画像")
            cum = cum_gate.get(g, [])
            cum_txt = f"{sum(cum):+.0f}" if cum else "-"
            lines.append(f"| {g}{flag} | {len(v)} | {s:+.2f} | {s/len(v):+.2f} "
                         f"| {w/len(v)*100:.0f}% | {len(cum)} | {cum_txt} |")
        lines.extend(flags)

    lines.append("")
    lines.append("判读护栏: 当日读数仅监控快照 (n 小, 易被单笔主导, 见 ⚠); 累计 n<30 只报方向。")
    lines.append("门的松/紧裁决以周六预注册周检为准 (按日聚类 t/MDE 判据, n_真被拦≥30 才启用)。")
    lines.append("归因注意: blocked_by 先到先占 (实测捕获 ~87%), 非单门完整总体; 延迟成交已剔除。")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--fetch", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(_ENTRIES):
        print("无影子入场台账")
        return 0
    entries = [json.loads(l) for l in open(_ENTRIES, encoding="utf-8")]
    if args.date:
        entries = [e for e in entries if e.get("date") == args.date]

    done = set()
    all_rows = []
    if os.path.exists(_OUTCOMES):
        for l in open(_OUTCOMES, encoding="utf-8"):
            try:
                r = json.loads(l)
                done.add((r.get("inst_id"), r.get("date")))
                all_rows.append(r)
            except Exception:
                pass

    sys.path.insert(0, _ROOT)
    from app.playbook.admission import estimate_round_trip_fee

    new_rows, skipped = [], 0
    for e in entries:
        key = (e.get("inst_id"), e.get("date"))
        if key in done:
            continue
        bars = load_bars(e["symbol"], e["date"])
        if bars is None and args.fetch:
            bars = fetch_bars(e["symbol"], e["date"])
            time.sleep(1.2)
        if not bars:
            skipped += 1
            continue
        entry = float(e["shadow_entry_price"])
        anchor = float(e.get("anchor") or entry)
        inst_sl = float(e.get("inst_stop_loss") or 0)
        sl_pct = abs(anchor - inst_sl) / anchor * 100 if inst_sl > 0 and anchor > 0 else 3.0
        sl_dist = max(SL_BETA * sl_pct, SL_FLOOR)          # 生产 β+下限
        tp = float(e.get("inst_take_profit") or 0) or None
        qty = int(e.get("position_size") or 0)
        if qty <= 0:
            qty = max(1, int(5500 / entry))
        ex, reason, mfe = settle(e["direction"], entry, bars, sl_dist, tp, e["ts"][11:16])
        if ex is None:
            skipped += 1
            continue
        is_long = e["direction"] == "LONG"
        gross_pct = (ex - entry) / entry * 100 if is_long else (entry - ex) / entry * 100
        fee = estimate_round_trip_fee(qty, entry, e["direction"], e["date"])
        net = gross_pct / 100 * entry * qty - fee
        new_rows.append({
            **{k: e.get(k) for k in ("ts", "date", "inst_id", "decision_id", "symbol",
                                     "direction", "blocked_by", "entry_path", "confidence")},
            "shadow_entry_price": entry, "qty": qty, "exit_price": round(ex, 4),
            "exit_reason": reason, "mfe_pct": round(mfe, 3),
            "gross_pct": round(gross_pct, 3), "fee": round(fee, 2), "net": round(net, 2),
        })

    with open(_OUTCOMES, "a", encoding="utf-8") as f:
        for r in new_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    delayed_ids = _load_delayed_inst_ids()
    for line in summarize(new_rows, all_rows + new_rows, delayed_ids, skipped):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
