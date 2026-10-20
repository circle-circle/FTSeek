#!/usr/bin/env python3
"""P0 只读审计: 出场后「条件持有」反事实 (RFC-154 P0).

问题: 用户体感「初盈利就跑、赢小输大」。先回答两个问题:
  ① 现有 BE/trail 出场, 出场后平均是继续有利还是不利?
  ② 如果当时用更宽的 trail 继续持有, 扣掉方向偏差后是更好还是更差?

方法 (只读, 不改任何交易行为):
  - 读 data/shadow/post_exit_path.jsonl + trade_records + playbook_instructions;
  - 从实际出场后的 1m bar 起, 按「更宽 trail」模拟继续持有到当日收盘;
  - 比较反事实出场价 vs 实际出场价 (方向归一, 正=更好, bps).
  - 变体: 固定 trail 0.8/1.2/1.6/2.0%; ATR trail 0.35/0.5/0.75/1.0×ATR%;
          条件变体 MFE≥1×ATR 才用 0.75×ATR, 否则 0.8%; hold-to-close.
  - 局限: 这不重放 BE 武装/初始止损, 只回答「实际出场后继续持有」;
          不叠加额外费用 (同一笔只多一次同向出场滑点, 近似抵消); 不含隔夜段.

输出: data/shadow/exit_conditional_audit_latest.json (只读, 无推送).
用法: PYTHONPATH=. venv/bin/python scripts/exit_conditional_audit.py
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
POST_EXIT = REPO / "data" / "shadow" / "post_exit_path.jsonl"
BAR_DIR = REPO / "data" / "replay_bars"
BAR_1M_DIR = REPO / "data" / "replay_bars_1m"
DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "shadow" / "exit_conditional_audit_latest.json"

MDE_Z = 2.802
TRAIL_FIXED = (0.8, 1.2, 1.6, 2.0)
TRAIL_ATR = (0.35, 0.5, 0.75, 1.0)
# 止损类出场不能做「出场后继续持有」反事实: 实际路径先触发了止损,
# 从止损后再模拟 trail = 止损后反转偏差 (会把均值抬到 +30bps 级别)。
# 评估宽止损必须做入场→出场全路径重放, 不在本审计范围。
STOP_LIKE = {"SL_INITIAL", "OVERNIGHT_GAP_STOP", "SL_FIXED_PCT"}

logger = logging.getLogger(__name__)


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                logger.warning("jsonl 坏行跳过: %s", error, exc_info=True)
                continue
            if row.get("type") == "post_exit" and row.get("status") == "ok":
                rows.append(row)
    return rows


def _load_bars(symbol: str, date: str) -> list[list]:
    stem = symbol.replace(".", "_")
    path = BAR_DIR / f"{stem}_{date}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [list(bar[:5]) for bar in data if len(bar) >= 5]
        except Exception as error:
            logger.warning("日 bar 读取失败 %s: %s", path, error, exc_info=True)
            return []
    path = BAR_1M_DIR / f"{stem}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        logger.warning("1m bar 读取失败 %s: %s", path, error, exc_info=True)
        return []
    out = []
    for bar in data:
        if len(bar) >= 5 and str(bar[0])[:10] == date:
            out.append(list(bar[:5]))
    return out


def _bars_after(bars: list[list], exit_min: str) -> list[list]:
    return [bar for bar in bars if str(bar[0])[11:16] > exit_min]


def _simulate_trail(
    bars: list[list], direction: str, start_price: float,
    peak_price: float | None, trail_pct: float,
) -> tuple[float, str]:
    """从实际出场后继续持有; 返回 (退出价, 原因). LONG/SHORT 对称。"""
    if not bars or start_price <= 0 or trail_pct <= 0:
        return start_price, "no_bars"
    if str(direction).upper() == "SHORT":
        peak = min(float(peak_price), start_price) if peak_price else start_price
        for bar in bars:
            high = float(bar[2])
            if high >= peak * (1.0 + trail_pct / 100.0):
                return peak * (1.0 + trail_pct / 100.0), "trail"
            peak = min(peak, float(bar[3]))
        return float(bars[-1][4]), "close"
    peak = max(float(peak_price), start_price) if peak_price else start_price
    for bar in bars:
        low = float(bar[3])
        if low <= peak * (1.0 - trail_pct / 100.0):
            return peak * (1.0 - trail_pct / 100.0), "trail"
        peak = max(peak, float(bar[2]))
    return float(bars[-1][4]), "close"


def _delta_bps(direction: str, actual: float, counterfactual: float) -> float | None:
    if actual <= 0 or counterfactual <= 0:
        return None
    if str(direction).upper() == "SHORT":
        return round((actual - counterfactual) / actual * 10000.0, 2)
    return round((counterfactual - actual) / actual * 10000.0, 2)


def _day_clustered(pairs: list[tuple[str, float]]) -> dict | None:
    by_day: dict[str, list[float]] = defaultdict(list)
    for day, value in pairs:
        by_day[day].append(value)
    daily = [statistics.mean(v) for v in by_day.values() if v]
    if len(daily) < 3:
        return None
    sd = statistics.stdev(daily)
    if sd == 0:
        return None
    mean = statistics.mean(daily)
    t_stat = mean / (sd / math.sqrt(len(daily)))
    return {
        "n_days": len(daily),
        "day_mean_bps": round(mean, 2),
        "t": round(t_stat, 2),
        "mde_80pct": round(MDE_Z * sd / math.sqrt(len(daily)), 2),
        "distinguishable": bool(abs(t_stat) >= 2.0),
    }


def _agg(pairs: list[tuple[str, float]]) -> dict:
    values = [value for _, value in pairs]
    if not values:
        return {"n": 0, "mean_bps": None, "median_bps": None,
                "pct_improved": None, "total_bps": 0.0, "day_clustered": None}
    return {
        "n": len(values),
        "mean_bps": round(statistics.mean(values), 2),
        "median_bps": round(statistics.median(values), 2),
        "pct_improved": round(100.0 * sum(1 for v in values if v > 0) / len(values), 1),
        "total_bps": round(sum(values), 2),
        "day_clustered": _day_clustered(pairs),
    }


def _mfe_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 0.3:
        return "<0.3%"
    if value < 0.6:
        return "0.3-0.6%"
    if value < 1.0:
        return "0.6-1.0%"
    return ">=1.0%"


def _atr_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 3.0:
        return "<3%"
    if value < 5.0:
        return "3-5%"
    if value < 8.0:
        return "5-8%"
    return ">=8%"


def _trade_context(db_path: Path) -> dict[int, dict]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT tr.id, tr.symbol, tr.direction, tr.entry_price, tr.exit_price, "
            "tr.metadata_json, tr.mfe_pct, pi.raw_instruction_json "
            "FROM trade_records tr LEFT JOIN playbook_instructions pi "
            "ON pi.decision_id = tr.decision_id AND tr.decision_id <> '' "
            "WHERE tr.source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')"
        ).fetchall()
    finally:
        connection.close()
    out: dict[int, dict] = {}
    for row in rows:
        metadata = {}
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception as error:
            logger.warning("metadata 坏行 trade=%s: %s", row["id"], error, exc_info=True)
        raw = {}
        try:
            raw = json.loads(row["raw_instruction_json"] or "{}")
        except Exception as error:
            logger.warning("raw_instruction 坏行 trade=%s: %s", row["id"], error, exc_info=True)
        entry = float(row["entry_price"] or 0)
        atr = metadata.get("atr")
        atr_pct = (float(atr) / entry * 100.0) if atr and entry > 0 else None
        out[int(row["id"])] = {
            "symbol": row["symbol"],
            "direction": row["direction"],
            "entry": entry,
            "exit": float(row["exit_price"] or 0),
            "mfe_pct": row["mfe_pct"],
            "atr_pct": atr_pct,
            "peak_price": metadata.get("peak_price"),
            "archetype": str(raw.get("entry_archetype") or "").upper() or "UNKNOWN",
        }
    return out


def compute(rows: list[dict], context: dict[int, dict]) -> dict:
    variants = ("hold_close", "trail_0.8", "trail_1.2", "trail_1.6", "trail_2.0",
                "atr_0.35", "atr_0.5", "atr_0.75", "atr_1.0", "cond_mfe_1atr")
    by_variant: dict[str, list[tuple[str, float]]] = defaultdict(list)
    by_reason: dict[str, dict[str, list[tuple[str, float]]]] = defaultdict(
        lambda: defaultdict(list))
    by_mfe: dict[str, dict[str, list[tuple[str, float]]]] = defaultdict(
        lambda: defaultdict(list))
    by_atr: dict[str, dict[str, list[tuple[str, float]]]] = defaultdict(
        lambda: defaultdict(list))
    premature: dict[str, list[tuple[str, float]]] = defaultdict(list)
    stop_like_reversal: dict[str, list[tuple[str, float]]] = defaultdict(list)
    coverage = {"rows": len(rows), "with_context": 0, "with_bars": 0,
                "with_atr": 0, "valid_trail_rows": 0, "stop_like_rows": 0,
                "skipped_no_bars": 0, "skipped_exit_after_close": 0}

    for row in rows:
        trade = context.get(int(row.get("trade_id") or 0))
        if not trade:
            continue
        coverage["with_context"] += 1
        date = str(row.get("date") or "")
        exit_min = str(row.get("exit_min") or "")
        if exit_min < "09:30" or exit_min > "15:59":
            coverage["skipped_exit_after_close"] += 1
            continue
        bars = _bars_after(_load_bars(trade["symbol"], date), exit_min)
        if not bars:
            coverage["skipped_no_bars"] += 1
            continue
        coverage["with_bars"] += 1
        direction = trade["direction"]
        actual = float(row.get("exit_px") or trade["exit"] or 0)
        if actual <= 0:
            continue
        start = actual
        peak = trade.get("peak_price")
        atr_pct = trade.get("atr_pct")
        if atr_pct is not None:
            coverage["with_atr"] += 1
        day = date
        deltas: dict[str, float] = {}
        close_px = float(bars[-1][4])
        deltas["hold_close"] = _delta_bps(direction, actual, close_px)
        for width in TRAIL_FIXED:
            price, _ = _simulate_trail(bars, direction, start, peak, width)
            deltas[f"trail_{width}"] = _delta_bps(direction, actual, price)
        if atr_pct:
            for mult in TRAIL_ATR:
                width = max(0.8, mult * atr_pct)
                price, _ = _simulate_trail(bars, direction, start, peak, width)
                deltas[f"atr_{mult}"] = _delta_bps(direction, actual, price)
            mfe = trade.get("mfe_pct")
            cond_width = (max(0.8, 0.75 * atr_pct)
                          if mfe is not None and mfe >= atr_pct else 0.8)
            price, _ = _simulate_trail(bars, direction, start, peak, cond_width)
            deltas["cond_mfe_1atr"] = _delta_bps(direction, actual, price)
        reason = str(row.get("exit_reason") or "UNKNOWN")
        valid_trail = reason not in STOP_LIKE
        if valid_trail:
            coverage["valid_trail_rows"] += 1
        else:
            coverage["stop_like_rows"] += 1
        for name, delta in deltas.items():
            if delta is None:
                continue
            by_reason[reason][name].append((day, delta))
            if not valid_trail:
                stop_like_reversal[name].append((day, delta))
                continue
            by_variant[name].append((day, delta))
            by_mfe[_mfe_bucket(trade.get("mfe_pct"))][name].append((day, delta))
            by_atr[_atr_bucket(atr_pct)][name].append((day, delta))
        # 「提前出场」子集: 出场后仍明显继续有利, 且不是止损类出场
        close_gap = row.get("close_gap_pct")
        if (valid_trail and isinstance(close_gap, (int, float)) and close_gap > 1.0
                and reason in
                ("SL_BREAKEVEN", "SL_TRAIL_LOCK", "TIME_EXIT", "DEAD_SIGNAL_EXIT")):
            for name, delta in deltas.items():
                if delta is not None:
                    premature[name].append((day, delta))

    return {
        "schema": "exit_conditional_audit_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coverage": coverage,
        "variants": {name: _agg(by_variant.get(name, [])) for name in variants},
        "by_exit_reason": {
            reason: {name: _agg(values) for name, values in variants_map.items()}
            for reason, variants_map in sorted(by_reason.items())
        },
        "by_mfe_bucket": {
            bucket: {name: _agg(values) for name, values in variants_map.items()}
            for bucket, variants_map in sorted(by_mfe.items())
        },
        "by_atr_bucket": {
            bucket: {name: _agg(values) for name, values in variants_map.items()}
            for bucket, variants_map in sorted(by_atr.items())
        },
        "premature_exit_subset": {
            name: _agg(values) for name, values in sorted(premature.items())
        },
        "stop_like_reversal": {
            name: _agg(values) for name, values in sorted(stop_like_reversal.items())
        },
        "caveats": [
            "反事实从实际出场后开始, 不重放 BE 武装/初始止损; 回答的是『出场后继续持有』。",
            "SL_INITIAL/OVERNIGHT_GAP_STOP/SL_FIXED_PCT 已单列为 stop_like_reversal,"
            " 不计入 trail 结论 — 从止损点之后模拟持有是止损后反转偏差, 会系统性抬高均值。",
            "不叠加额外费用; 同一笔只多一次同向出场滑点, 近似抵消。",
            "只模拟当日 bar, 不含隔夜段; 16:00 后出场样本跳过。",
            "bar 覆盖不足的标的会被跳过, 覆盖数见 coverage。",
            "正 delta = 反事实比实际更好 (方向归一, bps); 不是收益承诺。",
        ],
    }


def render(report: dict) -> None:
    coverage = report["coverage"]
    print("=" * 72)
    print("P0 出场条件审计 (RFC-154)")
    print(f"覆盖: post_exit {coverage['rows']} / 有上下文 {coverage['with_context']} / "
          f"有 bar {coverage['with_bars']} / 有 ATR {coverage['with_atr']} / "
          f"有效 trail {coverage['valid_trail_rows']} / stop-like 单列 {coverage['stop_like_rows']}")
    print("-- 有效 trail 样本 (delta vs 实际出场, 正=更好) --")
    for name, agg in report["variants"].items():
        clustered = agg.get("day_clustered") or {}
        print(f"  {name:14s} n={agg['n']:3d} mean={agg['mean_bps']}bps "
              f"median={agg['median_bps']} improved={agg['pct_improved']}% "
              f"t={clustered.get('t')} MDE80={clustered.get('mde_80pct')}")
    print("-- 提前出场子集 (exit后 close_gap>1%, 仅有效 trail 样本) --")
    for name, agg in report["premature_exit_subset"].items():
        clustered = agg.get("day_clustered") or {}
        print(f"  {name:14s} n={agg['n']:3d} mean={agg['mean_bps']}bps "
              f"median={agg['median_bps']} t={clustered.get('t')}")
    print("-- stop-like 单列 (止损后反转, 非 trail 反事实, 仅参考) --")
    for name, agg in report["stop_like_reversal"].items():
        if agg.get("n"):
            print(f"  {name:14s} n={agg['n']:3d} mean={agg['mean_bps']}bps "
                  f"median={agg['median_bps']}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--post-exit", default=str(POST_EXIT))
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rows = _load_jsonl(Path(args.post_exit))
    context = _trade_context(Path(args.db))
    report = compute(rows, context)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    render(report)
    print(f"已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
