#!/usr/bin/env python3.11
"""W7 分母分叉 · 取数面抓包探针 (2026-09-22, 只读诊断, 不落交易数据、不改任何行为).

背景: DEB-20260922 §4 已实证 ctx 侧 `change_percent` = **前一交易日涨跌幅** (3 样本差 ≤0.003pp),
但"哪个取数面给出的陈旧对"未定 — 嫌疑面 = push 通道 `futu_lv3_provider.py:951-962`
/ 快照通道 `:986-998` / 路由器抄值 `market_data_router.py:432,:723`。

本探针只做一件事: 在盘前/开盘窗口抓 1-2 只标的的**原始快照字段**
(`last_price` / `prev_close_price` / `pre_price` / `update_time`) 与日K收盘对表, 判定
"盘前快照的 (last_price, prev_close_price) 是否正好等于 (前一日收盘, 前前一日收盘)"。

- 判定命中 ⇒ 病灶在快照字段语义 (盘前 `last_price` 未切到当日), 快照通道 `:986-998` 是三选一里的主嫌;
- 判定不命中 ⇒ 快照清白, 病灶在 push 缓存/路由器抄值面。

写 `data/audit/w7_denominator_probe.jsonl` (逐次追加, 只读诊断档, 不进不变量判据)。
stdout 只出结论行 (≤6 行); 抓包失败出单行 rc 说明。**每轮只用 1 次 snapshot + 1 次日K**, 避开交易环路预算。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "audit" / "w7_denominator_probe.jsonl"
SYMBOLS = ["US.RIVN", "US.NOK"]          # 只抓 2 只: 避开 snapshot 预算
OPEN_D_HOST, OPEN_D_PORT = "127.0.0.1", 11111


def main() -> int:
    try:
        from futu import AuType, KLType, OpenQuoteContext, RET_OK
    except Exception as exc:  # noqa: BLE001
        print(f"[W7 抓包] 无法 import futu: {exc}")
        return 3

    q = OpenQuoteContext(host=OPEN_D_HOST, port=OPEN_D_PORT)
    rows, bad = [], []
    try:
        ret, snap = q.get_market_snapshot(SYMBOLS)
        if ret != RET_OK or snap is None or len(snap) == 0:
            print(f"[W7 抓包] snapshot 失败: {str(snap)[:150]}")
            return 2
        for r in snap.to_dict("records"):
            code = r.get("code")
            last = float(r.get("last_price") or 0)
            prev = float(r.get("prev_close_price") or 0)
            pre = float(r.get("pre_price") or 0)
            kret, kd, _ = q.request_history_kline(code=code, ktype=KLType.K_DAY, autype=AuType.NONE,
                                                  start="2026-08-25", end="2026-12-31", max_count=100)
            closes = [x["close"] for x in kd.to_dict("records")] if kret == RET_OK else []
            c1 = closes[-1] if closes else None            # 最近一个交易日收盘
            c2 = closes[-2] if len(closes) > 1 else None   # 再前一日收盘
            cp_snap = ((last - prev) / prev * 100) if prev > 0 else None
            cp_prior = ((c1 - c2) / c2 * 100) if (c1 and c2) else None
            hit = (prev > 0 and c1 and c2 and abs(prev - c1) < 0.01 and abs(last - c2) < 0.05)
            rows.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "code": code, "update_time": r.get("update_time"),
                "last_price": last, "prev_close_price": prev, "pre_price": pre,
                "open_price": float(r.get("open_price") or 0),
                "kline_c1": c1, "kline_c2": c2,
                "cp_from_snapshot_fields": None if cp_snap is None else round(cp_snap, 3),
                "cp_prior_session": None if cp_prior is None else round(cp_prior, 3),
                "stale_pair_hit": bool(hit),
            })
            if not hit:
                bad.append(code)
    finally:
        q.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[W7 抓包] {now} | 标的 {len(rows)} | 陈旧对命中 {sum(1 for r in rows if r['stale_pair_hit'])}")
    for r in rows:
        flag = "命中(快照字段滞后一日)" if r["stale_pair_hit"] else "未命中"
        print(f"  {r['code']} update={r['update_time']} last={r['last_price']} prev_close={r['prev_close_price']} "
              f"pre={r['pre_price']} | 快照字段算出={r['cp_from_snapshot_fields']}% vs 前一日涨跌幅={r['cp_prior_session']}% ⇒ {flag}")
    if bad:
        print(f"  未命中标的: {', '.join(bad)} ⇒ 病灶倾向 push 缓存/路由器抄值面 (快照清白)")
    return 0


if __name__ == "__main__":
    sys.exit(main())