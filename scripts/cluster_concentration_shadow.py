#!/usr/bin/env python3
"""簇浓度影子打标 (2026-08-27, 组合风控P2, record-only, 零执行路径改动).

背景 (8/27 组合风控讨论): 日开仓帽16/并发帽12全是"笔数"维度, 无结构维度 —
实测 8/24 组合 HOOD/COIN/RIVN(高beta) + AVGO/AMD/CRM/NVDA(半导体/软件) +
BABA/NTES(中概) = 12并发仓实为3-4个相关赌注; 8/20 AU/WPM/AEM(金矿)同日三仓.
簇内同跌 = 一个赌注打3-6次. 缺观测: 不知道簇浓度分布与"若有簇帽会拦掉什么".

本脚本盘后全量重放 (幂等, 确定性):
  1. 手工簇映射 (semis/software_cloud/high_beta/china_adr/gold_miners/
     pharma/financial/consumer/industrial_energy/other)
  2. 持仓占位区间 [entry_time, exit_time), 未平仓=至今; 跨日占位自然计入次日
  3. 每笔开仓时刻的同簇并发数 (含自身, 与排序无关的良定义)
  4. 每日行: 簇开仓数/最大并发/标的; 每笔记 concurrent → 任意 cap 阈值可离线重切
  5. blocked = concurrent > CAP(6) 的开仓, 记其真实 pnl_net → 回答"簇帽会帮还是会害"

判据 (预注册, DEB-20260805格式): n≥30 交易日且 blocked 样本≥30 笔,
blocked_pnl 净差>0 (拦掉的合计是亏的) + 符号检验 p<0.05 才议真簇帽.
注意反向偏差: 簇爆发日恰是动量日, 帽可能拦掉盈利簇 — 以数据裁决, 不预设方向.

幂等: 全量重放覆写 jsonl (纯函数, 重跑字节稳定; 未平仓行 pnl=pending, 次日自动补).
挂载: AFTER_HOURS (scheduler_core), 配置键 playbook.cluster_concentration_shadow_enabled.
回滚: 配置键 false + 重启.
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "cluster_concentration_shadow.jsonl"
SUMMARY = ROOT / "data" / "cluster_concentration_shadow_summary.json"
MIN_DATE = "2026-08-01"   # 16帽时代供给结构 (8/17起), 略前推含齐射实证期
CLUSTER_CAP = 6           # 观测参考帽 (任意 cap 可由 jsonl 的 concurrent 字段离线重切)
ET = ZoneInfo("America/New_York")

CLUSTERS = {
    "semis": "AVGO AMD NVDA MU TSM QCOM MRVL LRCX SNDK STX WDC SMTC LITE ALAB UCTT",
    "software_cloud": "MSFT META AMZN GOOG AAPL CRM ORCL CSCO PLTR DUOL WEAV DELL",
    "high_beta": "HOOD COIN RIVN TSLA NU BULL SKHY",
    "china_adr": "BABA NTES JD",
    "gold_miners": "AEM WPM AU NEM",
    "pharma": "LLY CI BBIO",
    "financial": "GS IBKR",
    "consumer": "UBER CMG SPOT",
    "industrial_energy": "CAT FCX GLW FIX TRGP EOG HON BWXT",
}
SYM2CLUSTER = {}
for _name, _syms in CLUSTERS.items():
    for _s in _syms.split():
        SYM2CLUSTER[f"US.{_s}"] = _name


def cluster_of(symbol: str) -> str:
    return SYM2CLUSTER.get(symbol, "other")


def _parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt


def run_check(scheduler=None) -> int:
    """调度器钩子 (AFTER_HOURS)."""
    import logging
    _logger = logging.getLogger(__name__)
    try:
        from app.core.config_container import ConfigContainer
        if not bool(ConfigContainer.get_instance().get_dotted(
                "playbook.cluster_concentration_shadow_enabled", True)):
            return 0
    except Exception:
        pass
    try:
        n = main()
        if n:
            _logger.info("[簇浓度影子] 已重放 %d 交易日", n)
        return n
    except Exception as e:
        _logger.warning("[簇浓度影子] 异常 (已隔离): %s", e)
        return 0


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT symbol, direction, entry_time, exit_time, entry_price, quantity, "
        "pnl_net, source_type FROM trade_records "
        "WHERE symbol LIKE 'US.%' AND substr(entry_time,1,10) >= ? ORDER BY entry_time",
        (MIN_DATE,),
    ).fetchall()
    con.close()

    # 占位区间 (aware): 未平仓 → 占用至今 (跨日自然计入)
    _now = datetime.now(ET)
    intervals = []
    skipped = 0
    for r in rows:
        t0 = _parse_ts(r["entry_time"])
        if t0 is None:
            skipped += 1
            continue
        t1 = _parse_ts(r["exit_time"]) if r["exit_time"] else _now
        if t1 is None or t1 <= t0:
            t1 = _now
        intervals.append({
            "symbol": r["symbol"], "cluster": cluster_of(r["symbol"]),
            "direction": r["direction"], "t0": t0, "t1": t1,
            "pnl_net": r["pnl_net"], "source": r["source_type"],
        })

    by_day = defaultdict(list)
    for it in intervals:
        by_day[it["t0"].strftime("%Y-%m-%d")].append(it)

    out_rows = []
    for day in sorted(by_day):
        opens = by_day[day]
        cluster_stats = defaultdict(lambda: {"n": 0, "max_conc": 0, "symbols": set()})
        open_events = []
        for it in opens:
            # 开仓时刻的同簇并发 (含自身): 与排序无关
            conc = sum(1 for o in intervals
                       if o["cluster"] == it["cluster"] and o["t0"] <= it["t0"] < o["t1"])
            cs = cluster_stats[it["cluster"]]
            cs["n"] += 1
            cs["max_conc"] = max(cs["max_conc"], conc)
            cs["symbols"].add(it["symbol"])
            open_events.append({
                "symbol": it["symbol"], "cluster": it["cluster"],
                "time": it["t0"].isoformat(timespec="seconds"),
                "direction": it["direction"], "concurrent": conc,
                "pnl_net": (round(float(it["pnl_net"]), 2)
                            if it["pnl_net"] is not None else None),
                "source": it["source"],
            })
        # 全量 opens 落盘: 任意 cap 阈值可离线重切 (concurrent 已逐笔记录)
        blocked = [o for o in open_events if o["concurrent"] > CLUSTER_CAP]
        blocked_closed = [b["pnl_net"] for b in blocked if b["pnl_net"] is not None]
        out_rows.append({
            "date": day,
            "n_opens": len(opens),
            "cap_ref": CLUSTER_CAP,
            "clusters": {
                c: {"n_opens": v["n"], "max_concurrent": v["max_conc"],
                    "symbols": sorted(v["symbols"])}
                for c, v in sorted(cluster_stats.items(),
                                   key=lambda kv: -kv[1]["max_conc"])
            },
            "max_cluster_concurrent": max(v["max_conc"] for v in cluster_stats.values()),
            "blocked_count": len(blocked),
            "blocked_pnl_sum": (round(sum(blocked_closed), 2)
                                if blocked_closed else 0.0),
            "opens": open_events,
        })

    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out_rows))

    # 汇总
    if out_rows:
        all_open_events = [o for r in out_rows for o in r["opens"]]
        closed_pnl = [o["pnl_net"] for o in all_open_events if o["pnl_net"] is not None]
        # cap 敏感性: 各档会拦多少笔/拦掉合计 pnl (负=帽会帮, 正=帽会害)
        cap_sens = {}
        for cap in (3, 4, 5, 6):
            blk = [o["pnl_net"] for o in all_open_events if o["concurrent"] > cap
                   and o["pnl_net"] is not None]
            cap_sens[str(cap)] = {
                "blocked": sum(1 for o in all_open_events if o["concurrent"] > cap),
                "blocked_closed": len(blk),
                "blocked_pnl_sum": round(sum(blk), 2) if blk else 0.0,
            }
        by_cluster = defaultdict(lambda: {"days": 0, "max_conc": 0, "opens": 0})
        for r in out_rows:
            for c, v in r["clusters"].items():
                by_cluster[c]["days"] += 1
                by_cluster[c]["max_conc"] = max(by_cluster[c]["max_conc"], v["max_concurrent"])
                by_cluster[c]["opens"] += v["n_opens"]
        summary = {
            "updated_at": datetime.now(ET).isoformat(timespec="seconds"),
            "n_days": len(out_rows),
            "n_opens_total": sum(r["n_opens"] for r in out_rows),
            "n_opens_closed": len(closed_pnl),
            "opens_pnl_sum": round(sum(closed_pnl), 2) if closed_pnl else 0.0,
            "skipped_rows": skipped,
            "cap_ref": CLUSTER_CAP,
            "criteria": "n≥30日 且 blocked≥30笔 且 blocked_pnl净差>0 + 符号p<0.05 才议真簇帽",
            "cap_sensitivity": cap_sens,
            "max_cluster_concurrent_ever": max(r["max_cluster_concurrent"] for r in out_rows),
            "by_cluster": {c: dict(v) for c, v in
                           sorted(by_cluster.items(), key=lambda kv: -kv[1]["max_conc"])},
            "top_concentration_days": [
                {"date": r["date"], "max_conc": r["max_cluster_concurrent"],
                 "n_opens": r["n_opens"]}
                for r in sorted(out_rows, key=lambda r: -r["max_cluster_concurrent"])[:5]
            ],
        }
        SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=1))

        print(f"=== 簇浓度影子 (n={len(out_rows)}日, cap_ref={CLUSTER_CAP}) ===")
        print(f"最大单簇并发: {summary['max_cluster_concurrent_ever']}")
        print("cap敏感性 (拦笔数/Σ被拦pnl): " + " | ".join(
            f"cap{c}={v['blocked']}笔/{v['blocked_pnl_sum']:+.0f}$"
            for c, v in cap_sens.items()))
        print("簇排行 (最大并发): " + ", ".join(
            f"{c}={v['max_conc']}" for c, v in
            sorted(by_cluster.items(), key=lambda kv: -kv[1]["max_conc"])))
    print(f"落盘: {OUT.name} / {SUMMARY.name} (跳过不可解析行 {skipped})")
    return len(out_rows)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
