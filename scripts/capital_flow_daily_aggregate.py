#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capital_flow 日级聚合 (2026-08-22)
==================================
把 futu_capital_flow.jsonl 分钟级数据聚合成日级净流入信号, 攒样到 30 交易日后
触发回放验证 (四关门+安慰剂).

聚合逻辑:
  - 按 symbol+date 分组
  - 日级净流入 = 当日所有分钟级 in_flow 求和
  - 大单方面 = super_in_flow + big_in_flow (超大单+大单)
  - 输出到 data/capital_flow_daily.jsonl

攒样到 30 交易日后, 自动触发回放验证 (调用 scripts/capital_flow_replay.py).
"""

import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime

BASE = Path("/opt/ftseek/FTSeek-US")
FLOW_IN = BASE / "data" / "futu_capital_flow.jsonl"
FLOW_OUT = BASE / "data" / "capital_flow_daily.jsonl"
FLOW_LATEST = BASE / "data" / "capital_flow_daily_latest.json"

# 攒样目标: 30 个交易日
TARGET_DAYS = 30


def aggregate_daily():
    """分钟级 → 日级聚合"""
    if not FLOW_IN.exists():
        print(f"[capital_flow] 输入文件不存在: {FLOW_IN}")
        return 0

    # 按 symbol+date 分组
    daily = defaultdict(lambda: defaultdict(lambda: {
        "in_flow": 0.0, "super_in_flow": 0.0, "big_in_flow": 0.0,
        "mid_in_flow": 0.0, "sml_in_flow": 0.0, "bars": 0
    }))

    for line in open(FLOW_IN):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        sym = d.get("symbol", "")
        item_time = d.get("item_time", "")
        if not sym or not item_time:
            continue
        date = item_time[:10]
        daily[sym][date]["in_flow"] += float(d.get("in_flow") or 0)
        daily[sym][date]["super_in_flow"] += float(d.get("super_in_flow") or 0)
        daily[sym][date]["big_in_flow"] += float(d.get("big_in_flow") or 0)
        daily[sym][date]["mid_in_flow"] += float(d.get("mid_in_flow") or 0)
        daily[sym][date]["sml_in_flow"] += float(d.get("sml_in_flow") or 0)
        daily[sym][date]["bars"] += 1

    # 写出日级聚合
    records = []
    for sym, dates in daily.items():
        for date, agg in dates.items():
            records.append({
                "symbol": sym,
                "date": date,
                "in_flow": round(agg["in_flow"], 2),
                "super_in_flow": round(agg["super_in_flow"], 2),
                "big_in_flow": round(agg["big_in_flow"], 2),
                "mid_in_flow": round(agg["mid_in_flow"], 2),
                "sml_in_flow": round(agg["sml_in_flow"], 2),
                "large_in_flow": round(agg["super_in_flow"] + agg["big_in_flow"], 2),  # 大单合计
                "bars": agg["bars"],
                "aggregated_at": datetime.now().isoformat(),
            })

    # 追加写入 (去重: 同 symbol+date 覆盖)
    existing = {}
    if FLOW_OUT.exists():
        for line in open(FLOW_OUT):
            try:
                r = json.loads(line)
                existing[(r["symbol"], r["date"])] = r
            except json.JSONDecodeError:
                continue

    for r in records:
        existing[(r["symbol"], r["date"])] = r

    # 按日期排序写出
    sorted_records = sorted(existing.values(), key=lambda x: (x["date"], x["symbol"]))
    with open(FLOW_OUT, "w") as f:
        for r in sorted_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 统计
    all_dates = sorted(set(r["date"] for r in sorted_records))
    n_days = len(all_dates)
    print(f"[capital_flow] 聚合完成: {len(sorted_records)} 条日级记录, 覆盖 {n_days} 个交易日")
    print(f"  日期范围: {all_dates[0]} ~ {all_dates[-1]}")
    print(f"  攒样进度: {n_days}/{TARGET_DAYS} 天")

    # 写 latest 摘要
    latest = {
        "generated_at": datetime.now().isoformat(),
        "n_records": len(sorted_records),
        "n_days": n_days,
        "target_days": TARGET_DAYS,
        "date_range": [all_dates[0], all_dates[-1]] if all_dates else [],
        "ready_for_replay": n_days >= TARGET_DAYS,
        "symbols": sorted(set(r["symbol"] for r in sorted_records)),
    }
    FLOW_LATEST.write_text(json.dumps(latest, ensure_ascii=False, indent=2))

    # 攒样到点 → 触发回放验证
    if n_days >= TARGET_DAYS:
        print(f"[capital_flow] ✅ 攒样完成 ({n_days}天 ≥ {TARGET_DAYS}天), 触发回放验证")
        trigger_replay()
    else:
        print(f"[capital_flow] ⏳ 攒样中 ({n_days}/{TARGET_DAYS}天), 暂不回放")

    return len(sorted_records)


def trigger_replay():
    """触发回放验证 (四关门+安慰剂).

    2026-08-22b 复查修复: 原实现在此运行时生成回放脚本 (write_replay_script) —
    生成的脚本查错 db (signal_snapshots 在独立 db 不在 ftseek_us.db) 且误用
    方向调整收益 fwd_ret_t1, 到点触发必然崩/结论无效, 属"声称有验证实无验证"。
    现改为调用真实落盘的 scripts/capital_flow_replay.py (可单独运行/测试)。
    """
    import subprocess
    result = subprocess.run(
        ["python3", str(BASE / "scripts" / "capital_flow_replay.py")],
        capture_output=True, text=True, cwd=str(BASE)
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"[capital_flow] 回放脚本错误: {result.stderr}")





if __name__ == "__main__":
    aggregate_daily()
