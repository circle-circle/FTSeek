#!/usr/bin/env python3
"""入场去同步化影子打标 (2026-08-12, record-only, 零执行路径改动).

背景 (8/12 时段策略分析): 8/11 实锤 10:01:33-10:01:54 21秒内5笔齐射
(GOOG/GS/TSM/CRM/DUOL, 禁触窗解除瞬间同轮倾泻) → 4亏1赢.
历史对照: 10:00-10:45 桶在自然分散入场时 +$487, 同步齐射后单日 -$133.
假设: 同步齐射把独立信号变成相关性=1的篮子单, 一次反向全灭.

本脚本给全部已平仓成交打"入场同步结构"标签 (含 volley 聚类 + 时段分区,
后者兼供甜蜜区/死区后续裁决), 攒样验证 volley vs 孤立入场的结局差.
n≥30 再裁是否加单轮限流/最小间隔门.

挂载: scheduler_core AFTER_HOURS (stall影子打标之后), 异常隔离.
输出: data/entry_sync_shadow.jsonl, 按 decision_id 去重 (重启不清空).
回滚: playbook.entry_sync_shadow_enabled 置 false + 重启.
"""
import json
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("ftseek.entry_sync_shadow")

_BASE = Path(__file__).resolve().parents[1]
_OUT = _BASE / "data" / "entry_sync_shadow.jsonl"
_DB = _BASE / "data" / "ftseek_us.db"

_VOLLEY_GAP_SEC = 120   # 相邻入场 ≤120s 视为同一齐射簇


def _enabled() -> bool:
    try:
        from app.core.config_container import ConfigContainer
        return bool(ConfigContainer.get_instance().get_dotted(
            "playbook.entry_sync_shadow_enabled", True))
    except Exception:
        return True


def _seen_ids() -> set:
    """已打标 decision_id 集合 (文件持久化, 重启不丢)."""
    seen = set()
    if _OUT.exists():
        for line in _OUT.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("decision_id"):
                    seen.add(d["decision_id"])
            except Exception:
                continue
    return seen


def _entry_sec(ts: str) -> int:
    """entry_time 'HH:MM:SS' → 当日秒数."""
    try:
        h, m, s = ts[11:13], ts[14:16], ts[17:19]
        return int(h) * 3600 + int(m) * 60 + int(s)
    except Exception:
        return -1


def _zone(minute: int) -> str:
    """时段分区 (ET 分钟). 8/12 时段策略分析口径."""
    if minute < 600:
        return "open30"            # 9:30-10:00 开盘绞肉机 (8/8起禁触)
    if minute < 610:
        return "post_blackout"     # 10:00-10:10 禁触窗解除瞬间 (齐射高发)
    if minute < 615:
        return "gap_1015"          # 10:10-10:15 过渡
    if minute < 645:
        return "sweet_1015_1045"   # 10:15-10:45 趋势确立甜蜜区
    if minute < 660:
        return "late_morning"      # 10:45-11:00
    if minute < 810:
        return "midday_dead"       # 11:00-13:30 午盘漂移死区
    return "afternoon"             # 13:30+


def _cluster_volley(entries_sorted: list) -> list:
    """按相邻间隔 ≤120s 聚簇, 返回每笔的 (volley_id, volley_size, seq, gap_sec)."""
    result = {}
    cluster = []
    clusters = []
    prev_sec = None
    for r in entries_sorted:
        sec = _entry_sec(r["entry_time"])
        if prev_sec is not None and sec - prev_sec > _VOLLEY_GAP_SEC and cluster:
            clusters.append(cluster)
            cluster = []
        cluster.append(r)
        prev_sec = sec
    if cluster:
        clusters.append(cluster)
    for ci, cl in enumerate(clusters):
        for si, r in enumerate(cl):
            gap = None
            if si > 0:
                gap = _entry_sec(r["entry_time"]) - _entry_sec(cl[si - 1]["entry_time"])
            result[r["decision_id"]] = {
                "volley_id": f"{cl[0]['entry_time'][:10]}-v{ci}",
                "volley_size": len(cl),
                "seq_in_volley": si,
                "gap_from_prev_sec": gap,
            }
    return result


def run_check(scheduler=None) -> None:
    """盘后打标: 全部已平仓且未打标的成交 → 同步结构标签 → jsonl."""
    if not _enabled():
        return
    seen = _seen_ids()
    conn = sqlite3.connect(str(_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT decision_id, symbol, direction, entry_time, exit_time,
               exit_reason, realized_pnl, pnl_net, mfe_pct, mae_pct,
               holding_hours, quantity, entry_price,
               substr(entry_time, 1, 10) d
        FROM trade_records
        WHERE (entry_time LIKE '%-04:00%' OR entry_time LIKE '%-05:00%')
          AND exit_time IS NOT NULL AND exit_time != ''
          AND decision_id IS NOT NULL AND decision_id != ''
          AND decision_id NOT LIKE 'TFB-%'
        ORDER BY entry_time
    """).fetchall()
    conn.close()

    new_rows = [r for r in rows if r["decision_id"] not in seen]
    if not new_rows:
        return

    # volley 聚类必须在全量(同日全部入场)上算, 否则簇大小失真
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["d"]].append(r)
    volley_info = {}
    for _d, day_rows in by_day.items():
        day_rows.sort(key=lambda x: x["entry_time"])
        volley_info.update(_cluster_volley(day_rows))

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    n_volley = 0
    with _OUT.open("a", encoding="utf-8") as f:
        for r in new_rows:
            minute = int(r["entry_time"][11:13]) * 60 + int(r["entry_time"][14:16])
            vi = volley_info.get(r["decision_id"], {})
            vsize = vi.get("volley_size", 1)
            tag = {
                "ts": r["exit_time"],
                "trade_date": r["d"],
                "symbol": r["symbol"],
                "direction": r["direction"],
                "decision_id": r["decision_id"],
                # ── 入场同步结构 ──
                "entry_et": r["entry_time"][11:19],
                "zone": _zone(minute),
                "volley_id": vi.get("volley_id"),
                "volley_size": vsize,
                "seq_in_volley": vi.get("seq_in_volley", 0),
                "gap_from_prev_sec": vi.get("gap_from_prev_sec"),
                "in_volley": vsize >= 3,
                "post_blackout_10min": 600 <= minute < 610,
                # ── 结局 ──
                "exit_reason": r["exit_reason"],
                "realized_pnl": r["realized_pnl"],
                "pnl_net": r["pnl_net"],
                "mfe_pct": r["mfe_pct"],
                "mae_pct": r["mae_pct"],
                "holding_hours": r["holding_hours"],
            }
            f.write(json.dumps(tag, ensure_ascii=False) + "\n")
            if tag["in_volley"]:
                n_volley += 1
    logger.info(
        f"[入场同步影子] 打标 {len(new_rows)} 笔 (volley成员 {n_volley} 笔) → {_OUT.name}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    run_check()
    print(f"done → {_OUT}")
