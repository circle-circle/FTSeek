#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VWAP 斜率证伪门周检 (2026-08-23 预注册, 门加载当日立判据 — 见数前)
====================================================================
门: falsification_gate_vwap_slope (DEB-20260823-phase1, P1-1, 生产拦截,
    2026-08-23 12:55 随服务重启加载)。拦截时落两处:
      - data/trigger_miss_telemetry.jsonl (reason='falsification_vwap_slope')  ← 本脚本主源
      - data/shadow_portfolio_entries.jsonl (blocked_by=...)  ← 影子组合 P&L 台账,
        按 (inst_id,date) 跨全部原因去重、每指令每日仅一行, **不作单门归因源**
        (会漏计, 见 load_blocked 注释)

问题 (预注册): 被拦的单是否真的更烂? 守宪章第 9 律 — 按日聚类 + MDE,
    n<30 只报方向不下结论 (与 reentry_gate_effect_tracker 同纪律)。

口径 (预注册, 见数前定):
  - 真被拦组: trigger_miss_telemetry 中 reason='falsification_vwap_slope' 的
    (inst_id, date) 去重集合 (逐轮 append, 无跨原因去重 = 完整总体)。
    ⚠️ 拦截是逐轮的 (该轮 triggered=False, 指令未作废) — 同指令下一轮
    可能重新触发并成交。查 playbook_instructions.executed 剔除"延迟成交"
    (计入独立桶, 不污染被拦组)。
  - 被拦组结局 (反事实): 拦截时刻价入场, 当日收盘出场 (与
    reentry_gate_effect_tracker 反事实口径一致), 扣 13bps 往返费用;
    另报 T+1 收盘。方向: BUY=exit-entry, SHORT=entry-exit。
  - 放行组: 同日全部 PLAYBOOK_SIM 已平仓成交 (trade_records, 真实退出
    路径的真实净额)。**不对称披露**: 放行组经真实退出链, 被拦组是收盘
    反事实 — 两组口径不同, 差异含退出链贡献, 不能全归因于门。
  - 统计: 报 pooled 值 + 按日聚类值 (同日信号相关, 宪章第 9 律);
    MDE = 2.8 × sd_daymean × sqrt(2/n_days) (双侧 α=0.05, 功效 0.8 近似)。

预注册判据 (n_真被拦 ≥ 30 时才启用, 此前一律"继续攒样"):
  - 按日聚类 t ≤ −2.0 (被拦组净收益显著低于放行组) → "门有效, 保持"
  - 按日聚类 t ≥ +2.0 (被拦组反而更好)             → "门在拦赢家, 建议关"
  - 否则                                          → "继续攒样"
  任何结论必须同时满足 |效应| > MDE, 否则降级为"不可分辨"。

挂载: crontab 周六 09:05 CST (ET 周五 21:05, 全周数据已落)。
只读: 零交易路径改动。输出:
  data/falsification_gate_review_history.jsonl (每笔被拦候选一行+结局)
  data/falsification_gate_review_report.json   (周检汇总)
回滚: 删本脚本 + 上述两个 data 文件 + cron 行。
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TELEMETRY = BASE / "data" / "trigger_miss_telemetry.jsonl"
DB = BASE / "data" / "ftseek_us.db"
BARS_DIR = BASE / "data" / "replay_bars"
BARS_1M_DIR = BASE / "data" / "replay_bars_1m"
OUT_HISTORY = BASE / "data" / "falsification_gate_review_history.jsonl"
OUT_REPORT = BASE / "data" / "falsification_gate_review_report.json"
FEE_RT = 0.0013  # 13bps 往返 (与 reentry_gate_effect_tracker 同源)
GATE = "falsification_vwap_slope"
MIN_N = 30


def load_blocked():
    """全量被本门拦截的 (inst_id, date) 候选。

    [Qoder 源头修复 2026-08-23] 数据源从 shadow_portfolio_entries.jsonl 改为
    trigger_miss_telemetry.jsonl:
      · shadow 台账按 (inst_id, date) 跨【全部】拦截原因去重, 每指令每日只落一行,
        先到先占 (_shadow_portfolio_emit, pipeline.py:323) —— 这是影子组合 P&L
        台账的正确设计 (同一指令入场两次会重复计钱), 但用作【单门归因】源会系统性
        漏计: 同一指令当日早轮已因 band/vol_gate 落行, 晚轮被本门拦截就落不进去。
        同构实测 (既有 vwap_gate, 影子上线后): 捕获 13/15 = 87%, 漏 2。
      · trigger_miss_telemetry 是逐轮 append、按 reason 区分、无跨原因去重的漏斗
        遥测 (_emit_trigger_miss), 字段含 inst_id/symbol/date/direction/current_price
        —— 正是归因所需的完整总体。
    口径变更时点: 本门 n_真被拦 = 0 (门 2026-08-23 12:55 上线, 当日休市),
      即"见数前"改口径, 无窥数风险, 预注册效力不受影响。
    """
    out, seen = [], set()
    if not TELEMETRY.exists():
        return out
    for line in open(TELEMETRY, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("reason") != GATE:
            continue
        k = (d.get("inst_id"), d.get("date"))
        if k in seen:
            continue
        seen.add(k)
        out.append(d)
    return out


def load_executed_inst_ids():
    cc = sqlite3.connect(str(DB), timeout=5)
    ids = {r[0] for r in cc.execute(
        "SELECT id FROM playbook_instructions WHERE executed=1")}
    cc.close()
    return ids


def build_close_index(symbols):
    """symbol -> {date: close}. 优先 replay_bars_1m (全历史), 缺失票回落
    replay_bars 的逐日文件。"""
    idx = defaultdict(dict)
    for sym in symbols:
        fname = sym.replace(".", "_") + ".json"
        p = BARS_1M_DIR / fname
        if p.exists():
            try:
                bars = json.load(open(p, encoding="utf-8"))
                for b in bars:
                    idx[sym][str(b[0])[:10]] = float(b[4])
                continue
            except Exception:
                pass
        # 回落: 逐日文件
        for f in BARS_DIR.glob(f"{sym.replace('.', '_')}_*.json"):
            day = f.stem.rsplit("_", 1)[-1]
            try:
                bars = json.load(open(f, encoding="utf-8"))
                if bars:
                    idx[sym][day] = float(bars[-1][4])
            except Exception:
                continue
    return idx


def next_trade_date(closes, day):
    ds = sorted(d for d in closes if d > day)
    return ds[0] if ds else None


def load_passed(date_str):
    """同日放行组: 全部已平仓 PLAYBOOK_SIM 成交, 净收益率。"""
    cc = sqlite3.connect(str(DB), timeout=5)
    # [Qoder 复核 2026-08-23] 列名修正: trade_records 无 position_size (是
    #   playbook_instructions 的列), 实际数量列为 quantity — 原查询抛
    #   OperationalError。零被拦样本时本函数不被调用, 故首跑未暴露。
    rows = cc.execute(
        "SELECT symbol, direction, entry_price, quantity, "
        "COALESCE(pnl_net, realized_pnl) AS net "
        "FROM trade_records WHERE source_type='PLAYBOOK_SIM' "
        "AND exit_time IS NOT NULL AND trade_date=?", (date_str,)).fetchall()
    cc.close()
    out = []
    for sym, direction, ep, size, net in rows:
        if not ep or not size or ep <= 0 or size <= 0 or net is None:
            continue
        out.append({"symbol": sym, "direction": direction,
                    "net_ret": float(net) / (float(ep) * float(size))})
    return out


def main():
    blocked = load_blocked()
    executed_ids = load_executed_inst_ids()

    true_blocked, delayed_exec = [], []
    for d in blocked:
        if d.get("inst_id") in executed_ids:
            delayed_exec.append(d)
        else:
            true_blocked.append(d)

    symbols = {d.get("symbol") for d in true_blocked if d.get("symbol")}
    closes = build_close_index(symbols)

    history, by_day_blocked = [], defaultdict(list)
    for d in true_blocked:
        sym, day = d.get("symbol"), d.get("date")
        entry = float(d.get("current_price") or d.get("shadow_entry_price") or 0)
        is_short = str(d.get("direction") or "").upper() == "SHORT"
        day_close = closes.get(sym, {}).get(day)
        t1_day = next_trade_date(closes.get(sym, {}), day) if sym in closes else None
        t1_close = closes.get(sym, {}).get(t1_day) if t1_day else None
        row = {"ts": datetime.now().isoformat(timespec="seconds"),
               "inst_id": d.get("inst_id"), "symbol": sym, "date": day,
               "direction": d.get("direction"), "entry": entry,
               "eod_close": day_close, "t1_close": t1_close}
        if entry > 0 and day_close is not None:
            raw = (entry - day_close) / entry if is_short else (day_close - entry) / entry
            row["cf_ret_eod_net"] = round(raw - FEE_RT, 6)
            by_day_blocked[day].append(row["cf_ret_eod_net"])
        if entry > 0 and t1_close is not None:
            raw1 = (entry - t1_close) / entry if is_short else (t1_close - entry) / entry
            row["cf_ret_t1_net"] = round(raw1 - FEE_RT, 6)
        history.append(row)

    # 放行组 (同日全部已平仓成交)
    days = sorted({d.get("date") for d in true_blocked if d.get("date")})
    by_day_passed = {}
    for day in days:
        rets = [r["net_ret"] for r in load_passed(day)]
        if rets:
            by_day_passed[day] = rets

    def daymean(m):
        return (sum(m) / len(m)) if m else None

    day_diffs, pooled_b, pooled_p = [], [], []
    for day in days:
        bm, pm = by_day_blocked.get(day, []), by_day_passed.get(day, [])
        pooled_b += bm
        pooled_p += pm
        if bm and pm:
            day_diffs.append(daymean(bm) - daymean(pm))

    import math

    def tstat(vals):
        n = len(vals)
        if n < 2:
            return None
        m = sum(vals) / n
        sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
        return (m / (sd / math.sqrt(n))) if sd > 0 else None

    n_b = len(pooled_b)
    cluster_mean = daymean(day_diffs) if day_diffs else None
    cluster_t = tstat(day_diffs)
    mde = None
    if len(day_diffs) >= 2:
        m = cluster_mean
        sd = math.sqrt(sum((v - m) ** 2 for v in day_diffs) / (len(day_diffs) - 1))
        mde = round(2.8 * sd * math.sqrt(2 / len(day_diffs)), 6)

    if n_b < MIN_N:
        verdict = f"继续攒样 (n_真被拦={n_b} < {MIN_N}, 此前一切读数仅方向性)"
    elif cluster_t is None or mde is None:
        verdict = "样本足但日聚类自由度不足, 继续攒样"
    elif cluster_t <= -2.0 and abs(cluster_mean) > mde:
        verdict = "门有效, 保持 (被拦组显著更烂且效应超 MDE)"
    elif cluster_t >= 2.0 and abs(cluster_mean) > mde:
        verdict = "⚠️ 门在拦赢家, 建议关闭 (被拦组显著更好且效应超 MDE)"
    else:
        verdict = "继续攒样 (效应不显著或未超 MDE)"

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "gate": GATE, "pre_registered": "2026-08-23 (门加载当日, 见数前)",
        "source": "trigger_miss_telemetry.jsonl (reason=%s, 逐轮全量, 按 inst_id+date 去重)" % GATE,
        "n_telemetry_blocks": len(blocked),
        "n_delayed_executed": len(delayed_exec),
        "n_true_blocked": len(true_blocked),
        "n_with_counterfactual": n_b,
        "blocked_pooled_mean_net_ret": round(sum(pooled_b) / n_b, 6) if n_b else None,
        "passed_pooled_mean_net_ret": round(sum(pooled_p) / len(pooled_p), 6) if pooled_p else None,
        "blocked_daily_clustered_diff_mean": cluster_mean,
        "blocked_daily_clustered_t": round(cluster_t, 3) if cluster_t is not None else None,
        "mde_daily_clustered": mde,
        "n_days_paired": len(day_diffs),
        "asymmetry_disclosure": "放行组=真实退出链净额; 被拦组=收盘反事实-13bps。差异含退出链贡献, 不可全归因于门。",
        "verdict": verdict,
    }
    OUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    if history:
        # 幂等: 已有 (inst_id, date) 行不重写 (每周重跑全量候选)
        existing = set()
        if OUT_HISTORY.exists():
            for line in open(OUT_HISTORY, encoding="utf-8"):
                try:
                    r = json.loads(line)
                    existing.add((r.get("inst_id"), r.get("date")))
                except Exception:
                    continue
        with open(OUT_HISTORY, "a", encoding="utf-8") as f:
            for r in history:
                if (r.get("inst_id"), r.get("date")) not in existing:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
