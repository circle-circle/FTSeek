#!/usr/bin/env python3.11
"""日K缓存增量刷新 (2026-08-17, 数据卫生根因修复).

背景: data/replay_bars_daily/*.json (110票×5年日K) 停更于 2026-07-24 —
  A2 plan-vs-actual 报告用它算 prev_close/gap, 7/24 后 165 行 gap 被三周
  漂移污染 (如 CRM "gap 16%" / GOOG "gap 11%" 实为缓存陈旧伪缺口, big 桶
  120 行中 118 行是脏数据)。market_profile_builder 的"近20日"指标同受累。

设计:
  - 增量尾部刷新: 每票从缓存末日(含, 防末bar盘中写入截断)重拉 KL_DAY, 按日期
    merge 回写。~1 call/票, 配额安全 (对比全量重拉 ~8 page/票)。
  - QFQ 与 historical_robustness_check.fetch_daily 同口径 (尾行新鲜即可满足
    prev_close; 全序列QFQ重基线需全量重拉, 留独立任务)。
  - 原子写 (tmp+rename), 幂等 (末日期>=最新交易日则跳过)。
  - 只读交易系统数据, 不触碰执行路径。

用法: python3 scripts/refresh_daily_bars.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DAILY_DIR = REPO / "data" / "replay_bars_daily"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = sorted(DAILY_DIR.glob("US_*.json"))
    if args.limit:
        files = files[:args.limit]
    print(f"扫描 {len(files)} 个日K缓存文件")

    from futu import KLType, RET_OK, AuType, OpenQuoteContext
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    ok = skip = fail = 0
    try:
        for fp in files:
            sym = fp.stem.replace("_", ".")
            try:
                rows = json.loads(fp.read_text())
            except Exception:
                fail += 1
                continue
            if not rows:
                fail += 1
                continue
            last = rows[-1][0]
            start = last  # 含末日重拉, 防截断
            if args.dry_run:
                print(f"  {sym}: last={last} → 将增量拉取 {start}~today")
                continue
            new_rows: list[list] = []
            page_key = None
            for _ in range(3):  # 增量最多3页足够
                ret, data, page_key = ctx.request_history_kline(
                    sym, ktype=KLType.K_DAY, start=start,
                    end=datetime.now().strftime("%Y-%m-%d"),
                    max_count=1000, page_req_key=page_key, autype=AuType.QFQ)
                if ret != RET_OK or data is None:
                    break
                for _, r in data.iterrows():
                    new_rows.append([str(r.get("time_key", ""))[:10],
                                     float(r["open"]), float(r["high"]),
                                     float(r["low"]), float(r["close"])])
                if page_key is None:
                    break
                time.sleep(1.2)
            time.sleep(1.2)  # kline 配额保护 (同 fetch_daily)
            if not new_rows:
                fail += 1
                print(f"  {sym}: 拉取失败 (ret={ret})")
                continue
            # merge: 按日期覆盖同名旧行
            merged = {r[0]: r for r in rows}
            added = sum(1 for r in new_rows if r[0] not in merged)
            merged.update({r[0]: r for r in new_rows})
            out = sorted(merged.values(), key=lambda b: b[0])
            if added == 0 and out[-1][0] == last:
                skip += 1
                continue
            tmp = fp.with_suffix(".tmp")
            tmp.write_text(json.dumps(out))
            tmp.rename(fp)
            ok += 1
            if added:
                print(f"  {sym}: {last} → {out[-1][0]} (+{added}行)")
    finally:
        ctx.close()
    print(f"完成: 刷新 {ok} | 已最新跳过 {skip} | 失败 {fail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
