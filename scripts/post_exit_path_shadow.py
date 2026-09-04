#!/usr/bin/env python3
"""出场后全天路径影子 (2026-09-04, MU V反核查产物; 同日 P1 口径修正) — 每笔平仓后的路径全记录。

动机 (2026-09-03 MU): LONG@937.6 → MFE+1.19% → SL_BREAKEVEN@936.89 → 出场后
  二次探底 930 → 午后 V 反 959 (+2.27% 全天错过)。「出场后走成什么样」此前无人记录。

边界 (防重复造轮子): 10m 短窗归 penetration_shadow, 禁用反事实归 exit_disable_cf,
  变体出场点归 exit_param_shadow — 本项只做全量×全天纯路径记录。

P1 口径修正 (2026-09-04 晚, 首版审计发现):
  首版直接读 data/replay_bars/<SYM>_<date>.json, 但该目录是多写入者混杂:
  fetch_bars 缓存 (全天) + 历史窗口文件 (entry-30→exit+30)。31/401 笔 ok 样本
  拿到窗口 bar → day_deep_dd/close_gap 是窗口口径污染; 19 笔 no_bars_after 里
  混着 8 笔 16:01 收盘平仓 (可算) / 4 笔真夜盘出场 / 7 笔截断误标。
  修正: ① bar 末根 <15:55 视为截断, 用 exit_replay_backtest.fetch_bars
  (refresh, 与 archetype_bars_ensure 同源同缓存) 补拉全天并写回目录 (纯增益超集);
  ② 分类语义精准化: ok / ah_exit / bar_truncated / no_bars 四类, 收盘平仓
  (exit≤16:05) 用末根 close 算 close_gap 不再误标; ③ day_* 字段只在全天 bar
  下计算; ④ 新增 next_open_gap_pct (出场→次日开盘, replay_bars_daily) —
  把路径从当日收盘延伸到次日开盘, 隔夜段也有量化 (夜盘出场适用)。

口径: 方向感知 (LONG: (px-exit)/exit; SHORT: (exit-px)/exit), 正=朝原方向走。
零交易行为变更; 幂等 (trade_id 去重); 补拉 lazy-futu + 限流 (fetch_bars 内建)。

已知边界 (2026-09-04 二次复查):
  · 时区: trade_records.exit_time 混存 (411笔带-04:00后缀 + 9笔老记录naive),
    但数值口径统一为 ET (naive 值经 reason 语义交叉验证: 16:08+GHOST_CLOSE /
    22:38+夜盘锁利只在 ET 解释下自洽), [11:16] 切片比较口径统一。
  · 半日市 (如感恩节次日 13:00 收盘): 末根<15:55 会判 bar_truncated 且每日
    重拉一次 (拉到仍是 13:00 不写回, 行为安全可观测 bar_end_min, 仅浪费一次
    futu 调用)。当前 60 天窗口无半日市样本, 未加豁免逻辑 (防御已到位)。
  · bar 时间格式全目录抽查 40/1054 统一为 'YYYY-MM-DD HH:MM:SS' (ET naive),
    与 fetch_bars 写回格式一致; exit_disable_cf 读同目录回归通过 (58笔不变)。

运行: ./venv/bin/python scripts/post_exit_path_shadow.py [--days 60] [--date YYYY-MM-DD] [--no-fetch]
输出: data/shadow/post_exit_path.jsonl (逐笔) + post_exit_path_latest.json (汇总)
cron: 05:45 ET (position_path_collector 05:15 → archetype_bars_ensure 05:35 之后)。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

DB = REPO / "data" / "ftseek_us.db"
BAR_DIR = REPO / "data" / "replay_bars"
DAILY_DIR = REPO / "data" / "replay_bars_daily"
OUT = REPO / "data" / "shadow" / "post_exit_path.jsonl"
OUT_LATEST = REPO / "data" / "shadow" / "post_exit_path_latest.json"

DEEP_DD_PCT = -2.0    # 日内深回撤阈值 (开盘→日内低, 相对开盘, %)
FULL_REC_PCT = 2.0    # 深回撤日「完整修复」阈值 (低→收盘, %)
FULL_DAY_HHMM = "15:55"   # bar 末根 >= 此值视为全天覆盖
CLOSE_EXIT_HHMM = "16:05"  # exit <= 此值视为收盘前出场 (16:01 ghost close 归 ok)

_ctx = None           # lazy futu ctx (仅补拉时创建)


def _log(msg: str):
    print(f"[post-exit-path] {msg}", file=sys.stderr)


def _load_local_bars(symbol: str, et_date: str):
    p = BAR_DIR / f"{symbol.replace('.', '_')}_{et_date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"bar 文件损坏 {p.name}: {e}")
        return None


def _fetch_full_day(symbol: str, et_date: str, allow_fetch: bool):
    """确保全天 bar: 本地截断/缺失时用 fetch_bars(refresh) 补拉并写回目录。
    返回 (bars, refreshed)。全天=末根 >= 15:55。"""
    bars = _load_local_bars(symbol, et_date)
    if bars and bars[-1][0][11:16] >= FULL_DAY_HHMM:
        return bars, False
    if not allow_fetch:
        return bars, False   # 可能是截断或 None
    global _ctx
    try:
        if _ctx is None:
            from futu import OpenQuoteContext
            import os as _os
            _ctx = OpenQuoteContext(host=_os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                                    port=int(_os.getenv("FUTU_OPEND_PORT", "11111")))
        from exit_replay_backtest import fetch_bars
        got = fetch_bars(_ctx, symbol, et_date, refresh=True)
        if got:
            rows = [[b["t"].strftime("%Y-%m-%d %H:%M:%S"), b["o"], b["h"], b["l"], b["c"]]
                    for b in got]
            if rows and rows[-1][0][11:16] >= FULL_DAY_HHMM:
                # 写回 (全天版是窗口版的超集, 下游只会更完整 — ensure 同款行为)
                (BAR_DIR / f"{symbol.replace('.', '_')}_{et_date}.json").write_text(
                    json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                return rows, True
            return rows, False   # 拉到了但非全天 (半日市/数据断) — 用但不写回污染
    except Exception as e:
        _log(f"补拉失败 {symbol} {et_date}: {e}")
    return bars, False


def _next_open(symbol: str, et_date: str):
    """次日开盘价 (replay_bars_daily)。返回 (next_date, open) 或 None。"""
    p = DAILY_DIR / f"{symbol.replace('.', '_')}.json"
    if not p.exists():
        return None
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"daily 解析失败 {p.name}: {e}")
        return None
    for i, r in enumerate(rows):
        if str(r[0]) == et_date and i + 1 < len(rows):
            return str(rows[i + 1][0]), float(rows[i + 1][1])
    return None


def main(days: int = 60, only_date: str | None = None, allow_fetch: bool = True) -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    done = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("type") == "post_exit" and r.get("trade_id"):
                    done.add(r["trade_id"])
            except Exception as e:
                _log(f"幂等集合脏行跳过: {e}")

    where = ("exit_time >= datetime('now', ?) " if not only_date else "date(exit_time) = ? ")
    params = (only_date,) if only_date else (f"-{days} day",)
    rows = con.execute(
        "SELECT id, symbol, direction, entry_time, exit_time, entry_price, exit_price, "
        "quantity, exit_reason, realized_pnl, metadata_json FROM trade_records "
        "WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
        "AND exit_price IS NOT NULL AND exit_price > 0 "
        "AND exit_reason != 'PHANTOM_RECONCILE_VOID' AND " + where +
        "ORDER BY exit_time",
        params
    ).fetchall()

    n_new = n_refetch = 0
    status_count = {}
    results = []
    with OUT.open("a", encoding="utf-8") as fh:
        for r in rows:
            if r["id"] in done:
                continue
            symbol, direction = r["symbol"], (r["direction"] or "LONG")
            entry, exit_px = float(r["entry_price"] or 0), float(r["exit_price"])
            et_iso = str(r["exit_time"])
            et_date = et_iso[:10]
            t_exit = et_iso.replace("T", " ")[11:16]
            sign = 1 if direction == "LONG" else -1

            bars, refreshed = _fetch_full_day(symbol, et_date, allow_fetch)
            if refreshed:
                n_refetch += 1
            full_day = bool(bars) and bars[-1][0][11:16] >= FULL_DAY_HHMM

            rec = {"type": "post_exit", "trade_id": r["id"], "symbol": symbol,
                   "direction": direction, "date": et_date,
                   "entry": entry, "exit_px": exit_px, "exit_min": t_exit,
                   "exit_reason": r["exit_reason"],
                   "live_pct": round((exit_px - entry) / entry * 100 * sign, 4),
                   "bars_refreshed": refreshed}

            # 次日开盘 gap (所有分类都算 — 隔夜段量化, replay_bars_daily 5年覆盖)
            no = _next_open(symbol, et_date)
            if no:
                rec["next_date"], rec["next_open_gap_pct"] = no[0], round(
                    (no[1] - exit_px) / exit_px * 100 * sign, 4)

            if not bars:
                rec["status"] = "no_bars"          # 休市/退市/拉取失败
            elif not full_day:
                # 本地窗口 bar 且补拉未成功 → 部分指标, day_* 不算 (窗口口径污染防御)
                rec["status"] = "bar_truncated"
                after = [b for b in bars if b[0][11:16] > t_exit]
                if after and t_exit < bars[-1][0][11:16]:
                    fav_px = (max(b[2] for b in after) if sign == 1 else min(b[3] for b in after))
                    rec["post_fav_partial_pct"] = round((fav_px - exit_px) / exit_px * 100 * sign, 4)
                    rec["bar_end_min"] = bars[-1][0][11:16]
            elif t_exit > CLOSE_EXIT_HHMM:
                # 夜盘/AH 出场: 当日常规段 bar 无「出场后」概念 (出场在 bar 覆盖之外)
                rec["status"] = "ah_exit"
            else:
                # ok: 全天 bar + 收盘前出场 (含 16:01 ghost close)
                rec["status"] = "ok"
                after = [b for b in bars if b[0][11:16] > t_exit]
                close = bars[-1][4]
                day_open, day_lo = bars[0][1], min(b[3] for b in bars)
                if after:
                    fav_px = (max(b[2] for b in after) if sign == 1 else min(b[3] for b in after))
                    adv_px = (min(b[3] for b in after) if sign == 1 else max(b[2] for b in after))
                    rec["n_bars_after"] = len(after)
                    rec["post_fav_pct"] = round((fav_px - exit_px) / exit_px * 100 * sign, 4)
                    rec["post_adv_pct"] = round((adv_px - exit_px) / exit_px * 100 * sign, 4)
                    fav_bar = max(after, key=lambda b: b[2] if sign == 1 else -b[3])
                    rec["fav_min"] = fav_bar[0][11:16]
                else:
                    # 收盘瞬间出场 (如 16:01 ghost): 出场后当日无剩余 — fav/adv=0
                    rec["n_bars_after"] = 0
                    rec["post_fav_pct"] = 0.0
                    rec["post_adv_pct"] = 0.0
                rec["close_gap_pct"] = round((close - exit_px) / exit_px * 100 * sign, 4)
                # V反上下文 (仅全天 bar — 窗口口径会污染)
                rec["day_deep_dd_pct"] = round((day_lo - day_open) / day_open * 100, 2)
                rec["day_recovery_pct"] = round((close - day_lo) / day_lo * 100, 2)
                rec["deep_dd_day"] = (day_lo - day_open) / day_open * 100 <= DEEP_DD_PCT
                rec["exit_dist_from_low_pct"] = round((exit_px - day_lo) / day_lo * 100, 2)
                results.append(rec)

            status_count[rec["status"]] = status_count.get(rec["status"], 0) + 1
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_new += 1
    con.close()
    if _ctx is not None:
        try:
            _ctx.close()
        except Exception as e:
            _log(f"ctx.close 异常(无副作用): {e}")

    # ── 汇总 (读全量 jsonl 含历史) ──
    all_rows = []
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            try:
                rr = json.loads(line)
                if rr.get("type") == "post_exit":
                    all_rows.append(rr)
            except Exception as e:
                _log(f"汇总读取脏行跳过: {e}")
    ok_rows = [r for r in all_rows if r.get("status") == "ok"]

    def _blk(rows_, label):
        if not rows_:
            return {"label": label, "n": 0}
        gaps = [r["close_gap_pct"] for r in rows_ if r.get("close_gap_pct") is not None]
        nog = [r["next_open_gap_pct"] for r in rows_ if r.get("next_open_gap_pct") is not None]
        return {"label": label, "n": len(rows_),
                "post_fav_med": round(statistics.median(
                    [r["post_fav_pct"] for r in rows_ if r.get("post_fav_pct") is not None]), 3),
                "close_gap_med": round(statistics.median(gaps), 3) if gaps else None,
                "close_gap_mean": round(sum(gaps) / len(gaps), 3) if gaps else None,
                "miss_gt1_pct": round(sum(1 for g in gaps if g > 1) / len(gaps), 3) if gaps else None,
                "next_open_gap_med": round(statistics.median(nog), 3) if nog else None,
                "next_open_gap_mean": round(sum(nog) / len(nog), 3) if nog else None}

    by_reason = {}
    for reason in sorted({r.get("exit_reason") for r in ok_rows}):
        by_reason[reason] = _blk([r for r in ok_rows if r["exit_reason"] == reason], reason)
    deep = [r for r in ok_rows if r.get("deep_dd_day")]
    calm = [r for r in ok_rows if not r.get("deep_dd_day")]

    sc = {}
    for r in all_rows:
        sc[r["status"]] = sc.get(r["status"], 0) + 1

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_total": len(all_rows), "n_new": n_new, "n_refetch": n_refetch,
        "status_counts": sc,
        "overall": _blk(ok_rows, "ALL"),
        "by_exit_reason": by_reason,
        "deep_dd_day_vs_calm": {
            "deep_dd_day": _blk(deep, "deep_dd_day"),
            "calm_day": _blk(calm, "calm_day"),
            "deep_dd_day_full_recovery_rate": round(
                sum(1 for r in deep if (r.get("day_recovery_pct") or 0) >= FULL_REC_PCT) / len(deep), 3
            ) if deep else None,
        },
        "notes": "ok=全天bar+收盘前出场; ah_exit=夜盘出场(隔夜段看next_open_gap); "
                 "bar_truncated=补拉未成功的窗口bar(day_*已防污染不计算); "
                 "close_gap=出场→当日收盘; next_open_gap=出场→次日开盘(隔夜量化)。"
                 "与 penetration_shadow(10m短窗)/exit_disable_cf(禁用反事实) 边界见脚本头。",
    }
    OUT_LATEST.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[post-exit-path] 新增 {n_new} 笔 (补拉全天 {n_refetch}), "
          f"累计 {len(all_rows)} 笔 | 状态分布 {sc}")
    if ok_rows:
        o = summary["overall"]
        print(f"  ok 样本: post_fav中位 {o['post_fav_med']:+.2f}% | gap中位 {o['close_gap_med']:+.2f}%"
              f"(均值{o['close_gap_mean']:+.2f}%) 错过>1% {100*(o['miss_gt1_pct'] or 0):.0f}%"
              f" | 次日开盘gap中位 {o['next_open_gap_med']:+.2f}%")
    print(f"→ {OUT_LATEST}")
    return n_new


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="出场后全天路径影子 (全量平仓单, 口径P1)")
    ap.add_argument("--days", type=int, default=60, help="回看天数 (默认60)")
    ap.add_argument("--date", type=str, default=None, help="只跑单日 (YYYY-MM-DD)")
    ap.add_argument("--no-fetch", action="store_true", help="禁止补拉 (只用本地 bar)")
    a = ap.parse_args()
    main(days=a.days, only_date=a.date, allow_fetch=not a.no_fetch)
