#!/usr/bin/env python3
"""[RFC-072 KR3 预注册 · 影子采集器] 方向×涨幅证伪门 — 只观察, 零行为变更 (C4 影子先行).

每日收盘后 (cron 06:10 CST 工作日): 对上一 ET 交易日全部 PLAYBOOK_SIM 入场, 计算
  entry_runup_pct = 前日收盘 → 入场价 的方向同向已动幅度
    LONG : (entry/prev_close - 1) × 100   (正 = 入场前已上涨)
    SHORT: (prev_close/entry - 1) × 100   (正 = 入场前已下跌)
落 data/shadow/kr3_direction_runup_gate.jsonl (幂等, 按 trade_id 去重)。
期末按 RFC-072 冻结判据裁决 (would-block 子集净期望是否显著更差)。
禁止在本窗口内把本影子转为拦截行为 — 判据预注册, 期间禁改阈值 (宪章第 6 律)。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "shadow" / "kr3_direction_runup_gate.jsonl"
RUNUP_BLOCK_PCT = 1.0  # 冻结阈值 (RFC-072): 同向已动 ≥1% 记 would_block


def _seen_ids() -> set:
    ids = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            rec = None
            try:
                rec = json.loads(line)
            except Exception:
                rec = None  # 不可解析行跳过 (幂等去重容错)
            if isinstance(rec, dict) and "trade_id" in rec:
                ids.add(rec["trade_id"])
    return ids


def _prev_close(router, symbol: str, entry_date_et: str):
    """取 entry_date_et 之前最近一根日 K 的收盘 (升序 bars, time 归一化)."""
    try:
        bars = router.get_daily_bars(symbol, count=10) or []
    except Exception as e:
        print(f"  [warn] {symbol} daily bars 获取失败: {e}")
        return None
    prev = None
    for b in bars:
        bt = str(b.get("time", ""))[:10]
        if bt and bt < entry_date_et:
            prev = b
    try:
        return float(prev["close"]) if prev else None
    except (TypeError, KeyError):
        return None


def main() -> int:
    con = sqlite3.connect(str(DB))
    rows = con.execute("""
        SELECT id, symbol, direction, entry_time, entry_price, exit_time, exit_reason, realized_pnl,
               json_extract(metadata_json,'$.mfe_pct') AS mfe
        FROM trade_records
        WHERE source_type LIKE 'PLAYBOOK%' AND entry_time >= date('now','-3 day')
    """).fetchall()
    if not rows:
        print("[kr3-shadow] 近 3 日无 PLAYBOOK 入场, 退出")
        return 0
    seen = _seen_ids()
    todo = [r for r in rows if r[0] not in seen]
    if not todo:
        print(f"[kr3-shadow] 全部 {len(rows)} 笔已采集, 退出")
        return 0
    sys.path.insert(0, str(REPO))
    from app.data.market_data_router import MarketDataRouter
    router = MarketDataRouter()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    n_ok = n_miss = 0
    with OUT.open("a", encoding="utf-8") as fh:
        for tid, symbol, direction, et, ep, xt, xr, pnl, mfe in todo:
            entry_date_et = str(et)[:10]
            pc = _prev_close(router, symbol, entry_date_et)
            if not pc or not ep:
                n_miss += 1
                continue
            ep = float(ep)
            if direction == "LONG":
                runup = (ep / pc - 1.0) * 100.0
            else:
                runup = (pc / ep - 1.0) * 100.0
            rec = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "trade_id": tid, "symbol": symbol, "direction": direction,
                "entry_time": et, "entry_price": ep, "prev_close": pc,
                "entry_runup_pct": round(runup, 3),
                "would_block": runup >= RUNUP_BLOCK_PCT,
                "exit_time": xt, "exit_reason": xr,
                "realized_pnl": pnl, "mfe_pct": mfe,
                "prereg": "RFC-072", "runup_block_pct_frozen": RUNUP_BLOCK_PCT,
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_ok += 1
    print(f"[kr3-shadow] 采集 {n_ok} 笔, prev_close 缺失 {n_miss} 笔 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
