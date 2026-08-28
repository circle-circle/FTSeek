#!/usr/bin/env python3
"""E1 前瞻影子臂一次性重建 (DEB-20260815 §40.5; 承用户 2026-08-28 裁决 B).

背景: 采集器阶段2 CST/ET 日期错位 (§四十) → 2026-08-17..08-27 共 9 个 ET 交易日
  的 9 次运行全部空转 (jsonl 0 字节)。用户裁决 B = 从 signal_snapshots 补回本该
  采到的记录 — 是"修好坏掉的计数器", 不是"延期补样" (§40.3/§40.4)。

语义 (不新增口径, 全部复用 scripts/e1_shadow_collector.py):
  - 查询过滤/去重键/记录格式/借券费参数与采集器阶段2逐字一致, 仅把
    "WHERE trade_date='{当日}'" 换成 "WHERE trade_date BETWEEN {窗口}";
  - 首行优先 (ORDER BY rowid = 采集器单日查询的自然扫描序), 同键后行跳过;
  - next_open 回填 = 采集器阶段1同款循环;
  - 只记价不结算 — 结算与判读仍归 GATE 1 裁决方 (§16.4 原意)。

前视纯净性 (§40.4 "未逐行 diff" 口子的实测补强, 2026-08-28):
  窗口内 191 行 (去重 184 键) 的落库时刻全部早于采集时点 (各自交易日 16:45 ET):
  - DA 行 137 条, created_at/created_at_snap 均 tz-aware ET 07-08 时 (盘前扫描);
  - UE 行 54 条, 双戳均 naive CST 21:10 (= 09:10 ET 盘前, 与 crontab
    "10 21 * * 1-5" llm_universe_scan 精确吻合 — naive 是 CST 墙钟, 非晚写);
  - 方向/强度/交易日/策略列经 UPSERT 写路径核查为写入后不可变 (§40.4)。

幂等: 与采集器共用 (symbol, trade_date, direction) 键空间, 已存在即跳过;
  08-28 起由修复版采集器自然接管 (08-28 不在本窗口, 今晚 04:45 CST 正常采集)。
回滚: data/e1_shadow_signals.jsonl 重建前为 0 字节 — 清空该文件即回到原状。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import sqlite3

from scripts import e1_shadow_collector as e1c
from app.utils.timezone import now_et

WINDOW_START = "2026-08-17"
WINDOW_END = "2026-08-27"


def rebuild(start: str = WINDOW_START, end: str = WINDOW_END) -> dict:
    recs = e1c.load_records()
    before = len(recs)

    con = sqlite3.connect(f"file:{e1c.SNAP_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sigs = con.execute(f"""
        SELECT symbol, trade_date, direction, strength, strategy_id
        FROM signal_snapshots
        WHERE trade_date BETWEEN '{start}' AND '{end}'
          AND strategy_id IN ('deep_analysis','universe_expansion')
          AND direction IN ('LONG','SHORT')
        ORDER BY rowid
    """).fetchall()
    con.close()

    added = 0
    no_bar = 0
    seen = set()
    for s in sigs:
        k = (s["symbol"], s["trade_date"], s["direction"])
        if k in seen:
            continue
        seen.add(k)
        if k in recs:
            continue
        days, day_list = e1c.get_daily(s["symbol"], s["trade_date"])
        sig_open = days[s["trade_date"]][1] if days and s["trade_date"] in days else None
        if sig_open is None:
            no_bar += 1
        recs[k] = {
            "symbol": s["symbol"], "trade_date": s["trade_date"],
            "direction": s["direction"], "strength": s["strength"],
            "strategy_id": s["strategy_id"], "signal_day_open": sig_open,
            "next_open_price": None, "next_open_date": None,
            "borrow_fee_annual_pct": e1c.BORROW_FEE_ANNUAL_PCT if s["direction"] == "SHORT" else 0.0,
            "collected_at": now_et().isoformat(timespec="seconds"),
        }
        added += 1

    filled = 0
    for r in recs.values():
        if r.get("next_open_price") is not None:
            continue
        days, day_list = e1c.get_daily(r["symbol"], r["trade_date"])
        if not days or r["trade_date"] not in day_list:
            continue
        idx = day_list.index(r["trade_date"])
        if idx + 1 < len(day_list):
            next_day = day_list[idx + 1]
            r["next_open_price"] = days[next_day][1]
            r["next_open_date"] = next_day
            filled += 1

    e1c.save_records(recs)
    return {"before": before, "added": added, "filled": filled,
            "total": len(recs), "no_bar": no_bar}


if __name__ == "__main__":
    stats = rebuild()
    print(f"[E1重建] 窗口 {WINDOW_START}..{WINDOW_END}: 原有{stats['before']}条, "
          f"新增{stats['added']}条, 累计{stats['total']}条, "
          f"回填次日开盘{stats['filled']}条, 缺日线{stats['no_bar']}条")
