#!/usr/bin/env python3
"""MAE 历史回填 (DEB-20260805-supply-reallocation 终裁③, 2026-08-06 用户裁"同意").

缺口口径 (主控 ★ SQL 复核):
  trade_records, strategy_id='playbook_sim', 已平仓, entry_time < 2026-07-16,
  mae_pct IS NULL 或 0 → 51 行 (其中亏损单真缺口 31 行; 07-16 后新平仓 0 缺口)。

回填尺子 (与 pipeline._track_bar_peak_trough 同口径):
  Futu K_1M, autype=NONE (不复权), 分钟末标签 (bar "10:33" 覆盖 10:32:00-59);
  入场 bar = 入场时刻 +1min 末标签;  LONG mae = (entry-min_low)/entry*100;
  SHORT mae = (max_high-entry)/entry*100;  floor 0, 量纲夹取 >50 判脏跳过。
  对称兜底: 亏损单 mae ≥ |pnl_pct|*100 (与 pipeline.py:4313 一致)。

时间戳解析 (库内三格式混杂):
  tz-aware → 转 ET;  naive 且落在 04:00-20:00 (ET 延展时段) → 视为 ET;
  否则视为服务器 UTC+8 → 减 12h 转 ET。歧义行打印 [tz?] 供目检。

安全: 默认 dry-run; --apply 才写库; 写前备份原值到 data/backups/mae_backfill_*.jsonl;
不可恢复行 (无 bar) 记 data/mae_backfill_unrecoverable.jsonl, 禁插值。

用法: venv/bin/python scripts/backfill_mae_from_bars.py [--apply] [--cutoff 2026-07-16]
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # [RFC-070] utils.et_time

import pytz

from utils.et_time import naive_cst_to_et  # noqa: E402  [RFC-070] 时间尺子公共层

ET = pytz.timezone("America/New_York")
BACKUP_DIR = Path("data/backups")
UNRECOVERABLE_PATH = Path("data/mae_backfill_unrecoverable.jsonl")
SANE_MAX = 50.0


def _parse_dt_et(raw: str):
    """三格式混杂时间 → ET naive datetime (与 futu time_key 对齐). 返回 (dt, ambiguous)."""
    if not raw:
        return None, False
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        return dt.astimezone(ET).replace(tzinfo=None), False
    if 4 <= dt.hour < 20:  # naive 且落在 ET 延展时段 → 视为 ET
        return dt, False
    return naive_cst_to_et(dt), True  # [RFC-070] 服务器 UTC+8 → ET, DST 正确 (EST -13h)


def _fetch_bars_1m(qctx, futu_symbol: str, start_date: str, end_date: str):
    """request_history_kline 分页拉 [start_date, end_date] (ET 日期) 的 1M bars."""
    from futu import KLType, AuType, RET_OK
    bars, page_key = [], None
    while True:
        kwargs = dict(code=futu_symbol, ktype=KLType.K_1M, autype=AuType.NONE,
                      start=start_date, end=end_date, max_count=1000)
        if page_key is not None:
            kwargs["page_req_key"] = page_key
        ret, data, page_key = qctx.request_history_kline(**kwargs)
        if ret != RET_OK:
            return None, str(data)
        if data is None or len(data) == 0:
            break
        bars.extend(data.to_dict("records"))
        if page_key is None:
            break
    return bars, None


def _recompute_mae(bars, entry_dt, exit_dt, direction: str, entry_price: float):
    """bars → mae_pct (percent, floor 0). 无覆盖 bar 返回 None."""
    entry_key = (entry_dt + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:00")
    exit_key = (exit_dt + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:00")
    highs, lows = [], []
    for b in bars:
        t = str(b.get("time_key") or "")
        if not t or t < entry_key or t > exit_key:
            continue
        h, l = float(b.get("high", 0) or 0), float(b.get("low", 0) or 0)
        if h > 0:
            highs.append(h)
        if l > 0:
            lows.append(l)
    if not highs or not lows or entry_price <= 0:
        return None, 0
    if direction.upper() == "LONG":
        mae = (entry_price - min(lows)) / entry_price * 100.0
    else:
        mae = (max(highs) - entry_price) / entry_price * 100.0
    return max(mae, 0.0), min(len(highs), len(lows))


def main() -> int:
    apply = "--apply" in sys.argv
    cutoff = "2026-07-16"
    if "--cutoff" in sys.argv:
        cutoff = sys.argv[sys.argv.index("--cutoff") + 1]

    from app.core.paths import get_trade_records_db
    conn = sqlite3.connect(get_trade_records_db())
    rows = conn.execute(
        """SELECT id, symbol, direction, entry_price, entry_time, exit_time, pnl_pct, mae_pct
           FROM trade_records
           WHERE exit_time IS NOT NULL AND strategy_id='playbook_sim' AND entry_time < ?
             AND (mae_pct IS NULL OR mae_pct=0)
           ORDER BY entry_time""", (cutoff,)).fetchall()
    print(f"[backfill] 目标行: {len(rows)} (playbook_sim, entry<{cutoff}, mae 0/NULL)")
    if not rows:
        return 0

    from futu import OpenQuoteContext
    host = os.getenv("FUTU_HOST", "127.0.0.1")
    port = int(os.getenv("FUTU_PORT", "11111"))
    qctx = OpenQuoteContext(host=host, port=port)

    updates, unrecoverable, skipped_dirty = [], [], []
    bar_cache = {}
    try:
        for rid, symbol, direction, entry_price, entry_raw, exit_raw, pnl_pct, old_mae in rows:
            entry_dt, amb1 = _parse_dt_et(entry_raw)
            exit_dt, amb2 = _parse_dt_et(exit_raw)
            tag = " [tz?]" if (amb1 or amb2) else ""
            if not entry_dt or not exit_dt or not entry_price:
                unrecoverable.append({"id": rid, "symbol": symbol, "reason": "bad_timestamp"})
                continue
            fsym = symbol if symbol.startswith("US.") else f"US.{symbol}"
            if fsym.count(".") > 1:  # 期权等无法取 1M 股票尺
                unrecoverable.append({"id": rid, "symbol": symbol, "reason": "non_equity"})
                continue
            d1, d2 = entry_dt.strftime("%Y-%m-%d"), exit_dt.strftime("%Y-%m-%d")
            cache_key = (fsym, d1, d2)
            if cache_key not in bar_cache:
                bar_cache[cache_key] = _fetch_bars_1m(qctx, fsym, d1, d2)
                time.sleep(0.3)  # 配额温和
            bars, err = bar_cache[cache_key]
            if bars is None or not bars:
                unrecoverable.append({"id": rid, "symbol": symbol, "reason": f"no_bars:{err}"})
                continue
            mae, nbars = _recompute_mae(bars, entry_dt, exit_dt, direction, float(entry_price))
            if mae is None or nbars == 0:
                unrecoverable.append({"id": rid, "symbol": symbol, "reason": "no_covering_bars"})
                continue
            # 对称兜底: 亏损单 mae ≥ |pnl_pct|*100
            if pnl_pct and pnl_pct < 0:
                mae = max(mae, round(-pnl_pct * 100, 4))
            mae = round(mae, 4)
            if mae <= 0 or mae > SANE_MAX:
                skipped_dirty.append((rid, symbol, mae))
                continue
            updates.append((rid, symbol, direction, mae, nbars, old_mae, tag))
            print(f"  {rid:>5} {symbol:<10} {direction:<6} mae 0→{mae:.4f}% ({nbars} bars){tag}")
    finally:
        qctx.close()

    print(f"\n[backfill] 可回填 {len(updates)} / 不可恢复 {len(unrecoverable)} / 判脏跳过 {len(skipped_dirty)}")
    for u in unrecoverable:
        print(f"  [unrecoverable] {u['id']} {u['symbol']} {u['reason']}")

    if not apply:
        print("[backfill] dry-run — 加 --apply 写库")
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"mae_backfill_{stamp}.jsonl"
    with backup_path.open("w", encoding="utf-8") as f:
        for rid, symbol, direction, mae, nbars, old_mae, _ in updates:
            f.write(json.dumps({"id": rid, "symbol": symbol, "old_mae_pct": old_mae,
                                "new_mae_pct": mae, "bars": nbars}, ensure_ascii=False) + "\n")
    for rid, symbol, direction, mae, nbars, old_mae, _ in updates:
        conn.execute("UPDATE trade_records SET mae_pct=? WHERE id=?", (mae, rid))
    conn.commit()
    if unrecoverable:
        with UNRECOVERABLE_PATH.open("a", encoding="utf-8") as f:
            for u in unrecoverable:
                u["backfill_run"] = stamp
                f.write(json.dumps(u, ensure_ascii=False) + "\n")
    print(f"[backfill] 已写 {len(updates)} 行, 原值备份 → {backup_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
