#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BE 宽限窗影子重放 (2026-10-20, 10-19 复查后新增; 预注册在本文件头部).

问题: 现行 SL_BREAKEVEN 在 MFE 刚过 0.4% 时武装, 0.4% 最小回撤对高 ATR 个股
是噪声 (10-19 ARM/COIN 捕获率仅 10%/6%, 出场后继续下跌 1.85%/3.21% 到收盘).
假设: 武装后给 N 分钟宽限, 期间只认初始 SL; 宽限结束再启用净保本线, 可能保留
趋势延续.

预注册 (读数前写死, 不在看到结果后改 N/阈值):
  - 人群: trade_date >= 2026-08-18 (be0.4 era), exit_reason='SL_BREAKEVEN',
    post_exit_path 有 RTH exit_min, 且有 1m bar 覆盖.
  - 主变体: N=10 分钟; N=5/15 只作描述性披露, 不参与裁决 (防多窗拣选).
  - 主判据 (N=10): n>=30 ∧ 配对差均值>0 ∧ (单侧符号检验 p<0.05 ∨ 日聚类 t>2)
    ∧ 剔 top3 正差后均值>0. 任一不过 => FAIL; n<30 => KEEP_SAMPLING.
  - 配对差 = 宽限变体净额 - 真实净额 (同一 fees_est; 价格用 1m bar close).
  - 近似边界: bar close 代替轮询报价; 忽略 TIME_EXIT/trail lock/隔夜续持;
    宽限后 BE 出场按净保本线成交 (live 仍有 marketable pad 滑点). 结论只作
    方向读数/立项依据, 不自动改线上 be 参数.

用法: PYTHONPATH=. venv/bin/python scripts/be_grace_replay.py
落盘: data/shadow/be_grace_replay_latest.json (只读 DB; 不写交易表).
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.breakeven_shadow_settle import (  # noqa: E402
    BE_ARM_PCT_04,
    DB,
    DEFAULT_CUSHION_PCT,
    agg,
    load_bars,
    profit_pct,
    to_aware,
)

OUT = ROOT / "data" / "shadow" / "be_grace_replay_latest.json"
ET = ZoneInfo("America/New_York")
ERA_START = "2026-08-18"
GRACE_MINUTES = (5, 10, 15)
PRIMARY_N = 10
MIN_N = 30
BE_ARM_PCT = BE_ARM_PCT_04


def _et_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(ET).replace(tzinfo=None)
    return value


def _bar_dt(raw) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def simulate_grace(bars, arm_dt, grace_minutes, direction, entry, qty, fees,
                   initial_sl, be_line):
    """返回 (cf_net, exit_px, exit_dt).

    bars: [(naive_et_dt, close)] 升序; 宽限内只认 initial_sl, 宽限后认 be_line.
    未触发则按最后一根 bar 收盘 (EOD) 出场.
    """
    sign = 1.0 if str(direction).upper() in ("LONG", "BUY") else -1.0
    grace_end = arm_dt + timedelta(minutes=grace_minutes)
    exit_px = None
    exit_dt = None
    for dt, px in bars:
        if dt <= arm_dt:
            continue
        if dt <= grace_end:
            if (sign > 0 and px <= initial_sl) or (sign < 0 and px >= initial_sl):
                exit_px, exit_dt = px, dt
                break
        else:
            if (sign > 0 and px <= be_line) or (sign < 0 and px >= be_line):
                exit_px, exit_dt = be_line, dt
                break
    if exit_px is None:
        exit_px, exit_dt = bars[-1][1], bars[-1][0]
    cf_net = (exit_px - entry) * qty * sign - fees
    return round(cf_net, 2), round(exit_px, 4), exit_dt


def _day_t(pairs):
    by_day = defaultdict(list)
    for day, delta in pairs:
        by_day[day].append(delta)
    daily = [sum(values) / len(values) for values in by_day.values() if values]
    if len(daily) < 2:
        return None
    return round(statistics.mean(daily) / (statistics.stdev(daily) / math.sqrt(len(daily))), 4)


def _variant(pairs, label):
    deltas = [delta for _, delta in pairs]
    out = agg(deltas, label)
    out["day_clustered_t"] = _day_t(pairs)
    if len(deltas) > 3:
        without_top3 = sorted(deltas, reverse=True)[3:]
        out["mean_excl_top3"] = round(sum(without_top3) / len(without_top3), 3)
    else:
        out["mean_excl_top3"] = None
    return out


def _criteria(primary):
    n = primary["n"]
    mean = primary.get("mean_pp") or 0.0
    p = (primary.get("sign_test") or {}).get("p_one_sided")
    t = primary.get("day_clustered_t")
    ex3 = primary.get("mean_excl_top3")
    if n < MIN_N:
        verdict = "KEEP_SAMPLING"
    elif mean > 0 and ((p is not None and p < 0.05) or (t is not None and t > 2)) \
            and ex3 is not None and ex3 > 0:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    return {
        "min_n": MIN_N, "primary_n": PRIMARY_N,
        "n_ok": n >= MIN_N, "mean_positive": mean > 0,
        "sign_p_lt_005": p is not None and p < 0.05,
        "day_t_gt_2": t is not None and t > 2,
        "mean_excl_top3_positive": ex3 is not None and ex3 > 0,
        "verdict": verdict,
    }


def _load_post_exit():
    out = {}
    path = ROOT / "data" / "shadow" / "post_exit_path.jsonl"
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError) as error:
                    logger.warning("坏 post_exit_path 行跳过: %s", error, exc_info=True)
                    continue
                if row.get("status") == "ok" and row.get("exit_min"):
                    out[row.get("trade_id")] = row
    except OSError as error:
        logger.warning("读取 post_exit_path 失败 (返回空): %s", error, exc_info=True)
    return out


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    trades = con.execute(
        "SELECT id, symbol, direction, entry_price, exit_price, quantity, fees_est, "
        "pnl_net, realized_pnl, entry_time, exit_reason, metadata_json "
        "FROM trade_records WHERE exit_reason='SL_BREAKEVEN' AND trade_date >= ? "
        "ORDER BY id", (ERA_START,)
    ).fetchall()
    con.close()
    post = _load_post_exit()
    pairs = {n: [] for n in GRACE_MINUTES}
    coverage = {"be_trades": len(trades), "post_exit": 0, "bars": 0,
                "included": 0, "skipped_no_arm": 0, "skipped_no_bars": 0,
                "skipped_bad_sl": 0}
    for row in trades:
        meta = {}
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        post_row = post.get(row["id"])
        if not post_row:
            continue
        coverage["post_exit"] += 1
        entry = float(row["entry_price"] or 0)
        qty = float(row["quantity"] or 0)
        fees = float(row["fees_est"] or 0)
        if entry <= 0 or qty <= 0:
            continue
        entry_dt = to_aware(datetime.fromisoformat(row["entry_time"])) \
            if row["entry_time"] else None
        if entry_dt is None:
            continue
        day = _et_naive(entry_dt).strftime("%Y-%m-%d")
        if not post_row.get("exit_min"):
            continue
        bars_raw = load_bars(row["symbol"], day)
        if not bars_raw:
            coverage["skipped_no_bars"] += 1
            continue
        coverage["bars"] += 1
        bars = [(_bar_dt(ts), float(px)) for ts, px in bars_raw]
        bars = [(dt, px) for dt, px in bars if dt is not None]
        bars = sorted(bars, key=lambda item: item[0])
        entry_naive = _et_naive(entry_dt)
        bars = [(dt, px) for dt, px in bars if dt.date() == entry_naive.date()]
        if not bars:
            coverage["skipped_no_bars"] += 1
            continue
        sign = 1.0 if str(row["direction"]).upper() in ("LONG", "BUY") else -1.0
        arm_dt = next(
            (dt for dt, px in bars if dt >= entry_naive
             and profit_pct(row["direction"], entry, px) >= BE_ARM_PCT),
            None,
        )
        if arm_dt is None:
            coverage["skipped_no_arm"] += 1
            continue
        initial_sl = float(meta.get("stop_loss") or 0)
        if initial_sl <= 0:
            coverage["skipped_bad_sl"] += 1
            continue
        cushion = meta.get("_be_cushion_fee_aware")
        if cushion is None:
            notional = entry * qty
            cushion = fees / notional * 100 if notional > 0 else DEFAULT_CUSHION_PCT
        be_line = round(entry * (1 + sign * float(cushion) / 100.0), 4)
        actual_net = row["pnl_net"]
        if actual_net is None:
            actual_net = (row["realized_pnl"] or 0) - fees
        actual_net = float(actual_net)
        coverage["included"] += 1
        for n in GRACE_MINUTES:
            cf_net, _, _ = simulate_grace(
                bars, arm_dt, n, row["direction"], entry, qty, fees, initial_sl, be_line
            )
            pairs[n].append((day, round(cf_net - actual_net, 2)))

    variants = {
        str(n): {"all": _variant(pairs[n], f"grace={n}m")}
        for n in GRACE_MINUTES
    }
    primary = variants[str(PRIMARY_N)]["all"]
    out = {
        "generated_at": datetime.now(ET).isoformat(),
        "era_start": ERA_START,
        "be_arm_pct": BE_ARM_PCT,
        "primary_n_minutes": PRIMARY_N,
        "variants": variants,
        "criteria": _criteria(primary),
        "coverage": coverage,
        "notes": (
            "pair delta = grace variant net - actual net; same fees; bar close approximation; "
            "BE exit filled at net-breakeven line after grace; no TIME_EXIT/trail/overnight replay"
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    slim = {k: v for k, v in variants.items()}
    print(json.dumps({"coverage": coverage, "variants": slim,
                      "criteria": out["criteria"]}, ensure_ascii=False, indent=1))
    print(f"\n落盘: {OUT}")


if __name__ == "__main__":
    main()
