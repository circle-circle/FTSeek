#!/usr/bin/env python3
"""P0 路由反事实: 确认位限价 + TTL vs 实际市价入场 (只读).

对 2026-09-20→10-19 已成交的 FADE_SHORT / PULLBACK_LIMIT:
  - 用 confirmation.price_level 挂限价, TTL 内 1m bar 触碰才成交;
  - 未成交 = 跳过 (cf PnL=0);
  - 成交 = 按限价改善入场价, 出场价/数量沿用实际交易 (近似, 不重放改锚后的 SL/TP).
只读 data/replay_bars 缓存 + DB(mode=ro); 不写交易表/配置, 不触 OpenD.

变体:
  raw_confirm_300 / raw_confirm_900: 原始确认位, TTL 300/900s
  reanchor_300: 确认位与执行锚偏离 >1% 时改用执行锚 (当前 resolve_route 的重锚语义)
仅作 P0 决策证据; 转生产须另立 RFC + 用户批准.
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.utils.timezone import ET

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
BAR_DIR = REPO / "data" / "replay_bars"
OUT = REPO / "data" / "shadow" / "entry_routing_counterfactual_latest.json"
STOP_LIKE = {"SL_INITIAL", "OVERNIGHT_GAP_STOP", "SL_FIXED_PCT"}
ARCHS = {"FADE_SHORT", "PULLBACK_LIMIT"}
TTLS = (300, 900)

logger = logging.getLogger(__name__)


def _raw(inst: dict) -> dict:
    raw = inst.get("raw_instruction_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception as error:
            logger.warning("raw_instruction 坏行 inst=%s: %s", inst.get("id"), error,
                           exc_info=True)
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _level(inst: dict) -> float | None:
    conf = _raw(inst).get("confirmation")
    if not isinstance(conf, dict):
        return None
    try:
        value = float(str(conf.get("price_level", "")).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _norm_dir(direction: str) -> str:
    return "SHORT" if str(direction).upper() in ("SHORT", "SELL_SHORT", "SELL") else "LONG"


def _entry_improvement_bps(direction: str, actual: float, fill: float) -> float:
    if actual <= 0 or fill <= 0:
        return 0.0
    if direction == "SHORT":
        return round((fill - actual) / actual * 10000.0, 2)
    return round((actual - fill) / actual * 10000.0, 2)


def _load_bars(symbol: str, date: str) -> list[list] | None:
    path = BAR_DIR / f"{symbol.replace('.', '_')}_{date}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        logger.warning("bar 读取失败 %s: %s", path, error, exc_info=True)
        return None
    return [list(b[:5]) for b in raw if len(b) >= 5]


def simulate_limit(
    bars: list[list], direction: str, entry_dt: datetime, actual_entry: float,
    level: float, ttl_sec: int,
) -> dict:
    """返回 {state: marketable/filled/skipped, fill, fill_t, bars_used}."""
    if direction == "SHORT":
        marketable = level <= actual_entry
    else:
        marketable = level >= actual_entry
    if marketable:
        return {"state": "marketable", "fill": actual_entry, "fill_t": entry_dt,
                "bars_used": 0}
    deadline = entry_dt + timedelta(seconds=ttl_sec)
    start = entry_dt.replace(second=0, microsecond=0)
    used = 0
    for bar in bars:
        try:
            bar_dt = datetime.strptime(str(bar[0]), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError) as _bar_e:
            # 2026-10-28 except:pass 清剿: 坏 bar 静默跳 ⇒ 反事实成交判定样本被削。
            logger.info("[entry_routing] 坏 bar 跳过: %s (%s)", _bar_e, bar[0])
            continue
        if bar_dt <= start:
            continue
        if bar_dt > deadline:
            break
        used += 1
        high, low = float(bar[2]), float(bar[3])
        if direction == "SHORT" and high >= level:
            return {"state": "filled", "fill": level, "fill_t": bar_dt, "bars_used": used}
        if direction == "LONG" and low <= level:
            return {"state": "filled", "fill": level, "fill_t": bar_dt, "bars_used": used}
    return {"state": "skipped", "fill": None, "fill_t": None, "bars_used": used}


def _source(decision_id: str) -> str:
    value = str(decision_id or "")
    if value.startswith("universe_expansion"):
        return "UE"
    if value.startswith("EXT-"):
        return "EXT"
    if value.startswith("DA-"):
        return "DA"
    return "OTHER"


def _session_min(entry_dt: datetime) -> int:
    return entry_dt.hour * 60 + entry_dt.minute - 570


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None}
    return {"n": len(values), "mean": round(statistics.mean(values), 2),
            "median": round(statistics.median(values), 2)}


def _aggregate(rows: list[dict], variant: str) -> dict:
    eligible = [r for r in rows if r["variants"].get(variant)]
    vals = [r["variants"][variant] for r in eligible]
    filled = [v for v in vals if v["state"] in ("filled", "marketable")]
    skipped = [v for v in vals if v["state"] == "skipped"]
    actual_net = sum(r["actual_pnl"] for r in eligible)
    cf_net = sum(v["cf_pnl"] for v in vals)
    skipped_actual = sum(r["actual_pnl"] for r, v in zip(eligible, vals)
                         if v["state"] == "skipped")
    return {
        "eligible_n": len(eligible),
        "filled_n": len(filled),
        "skipped_n": len(skipped),
        "fill_rate_pct": round(100.0 * len(filled) / len(eligible), 1) if eligible else None,
        "actual_net": round(actual_net, 2),
        "cf_net": round(cf_net, 2),
        "delta_net": round(cf_net - actual_net, 2),
        "entry_improvement_bps": _stats([v["improvement_bps"] for v in filled]),
        "skipped_actual_net": round(skipped_actual, 2),
        "skipped_winners_n": sum(1 for r, v in zip(eligible, vals)
                                 if v["state"] == "skipped" and r["actual_pnl"] > 0),
        "skipped_left_tail_n": sum(1 for r, v in zip(eligible, vals)
                                   if v["state"] == "skipped" and r["left_tail"]),
        "by_archetype": {
            arch: _variant_subset([(r, v) for r, v in zip(eligible, vals)
                                   if r["archetype"] == arch])
            for arch in sorted({r["archetype"] for r in eligible})
        },
    }


def _variant_subset(pairs: list[tuple[dict, dict]]) -> dict:
    if not pairs:
        return {"n": 0}
    actual = sum(r["actual_pnl"] for r, _ in pairs)
    cf = sum(v["cf_pnl"] for _, v in pairs)
    filled = [v for _, v in pairs if v["state"] in ("filled", "marketable")]
    return {"n": len(pairs), "actual_net": round(actual, 2),
            "cf_net": round(cf, 2), "delta_net": round(cf - actual, 2),
            "fill_rate_pct": round(100.0 * len(filled) / len(pairs), 1),
            "skipped_n": sum(1 for _, v in pairs if v["state"] == "skipped")}


def compute(instructions: list[dict], trades: dict[str, dict]) -> dict:
    rows = []
    no_bars = 0
    for inst in instructions:
        raw = _raw(inst)
        arch = str(raw.get("entry_archetype") or "").upper()
        if arch not in ARCHS:
            continue
        level = _level(inst)
        if level is None:
            continue
        trade = trades.get(str(inst.get("decision_id") or ""))
        if not trade:
            continue
        try:
            entry_dt = datetime.fromisoformat(str(trade["entry_time"]))
            if entry_dt.tzinfo is not None:
                entry_dt = entry_dt.astimezone(ET).replace(tzinfo=None)
        except Exception as _edt_e:
            # 2026-10-28 except:pass 清剿: 入场时刻坏 ⇒ 该笔反事实被静默剔除。
            logger.info("[entry_routing] entry_time 解析失败跳样本: %s (%s)",
                        _edt_e, trade.get("entry_time"))
            continue
        bars = _load_bars(trade["symbol"], entry_dt.strftime("%Y-%m-%d"))
        if bars is None:
            no_bars += 1
            continue
        direction = _norm_dir(trade["direction"])
        actual = float(trade["entry_price"])
        qty = float(trade["quantity"] or 0)
        notional = actual * qty
        variants = {}
        for ttl in TTLS:
            sim = simulate_limit(bars, direction, entry_dt, actual, level, ttl)
            variants[f"raw_confirm_{ttl}"] = _cf_row(sim, direction, actual, notional,
                                                    trade["pnl_net"])
        # reanchor: 偏离 >1% 用执行锚 (=市价), 否则原始确认位; TTL 300
        re_level = actual if abs(level / actual - 1.0) > 0.01 else level
        sim = simulate_limit(bars, direction, entry_dt, actual, re_level, 300)
        variants["reanchor_300"] = _cf_row(sim, direction, actual, notional, trade["pnl_net"])
        rows.append({
            "id": inst.get("id"), "decision_id": trade["decision_id"],
            "symbol": trade["symbol"], "direction": direction, "archetype": arch,
            "actual_pnl": float(trade["pnl_net"] or 0), "left_tail": trade["exit_reason"] in STOP_LIKE,
            "entry_dt": entry_dt.isoformat(), "level": level, "actual_entry": actual,
            "session_min": _session_min(entry_dt), "source": _source(trade["decision_id"]),
            "variants": variants,
        })

    out = {
        "schema": "entry_routing_counterfactual_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": {"since": "2026-09-20", "until": "2026-10-19"},
        "coverage": {"instructions": len(instructions), "rows": len(rows),
                     "skipped_no_bars": no_bars},
        "variants": {name: _aggregate(rows, name)
                     for name in ("raw_confirm_300", "raw_confirm_900", "reanchor_300")},
        "caveats": [
            "只读 entry-price 反事实: 成交后沿用实际出场价/数量, 不重放改锚后的 SL/TP/时间退出。",
            "限价触碰即按限价成交 (保守); 市价可成交的限价按实际入场价计 (不假设更优)。",
            "未成交 = 跳过 (cf PnL=0), 其实际盈亏作为机会成本单列。",
            "bar 缓存缺失样本跳过; 不含 10-20 当日 (缓存未生成)。",
            "样本为 in-sample; 转生产须 RFC + 用户批准 + 影子/前瞻。",
        ],
    }
    return out


def _cf_row(sim: dict, direction: str, actual: float, notional: float,
            actual_pnl: float) -> dict:
    if sim["state"] == "skipped":
        return {**sim, "improvement_bps": 0.0, "cf_pnl": 0.0}
    fill = float(sim["fill"])
    bps = _entry_improvement_bps(direction, actual, fill)
    return {**sim, "improvement_bps": bps,
            "cf_pnl": round(float(actual_pnl) + bps / 10000.0 * notional, 2)}


def load_data(db_path: Path) -> tuple[list[dict], dict[str, dict]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    try:
        instructions = [dict(r) for r in con.execute(
            "SELECT id,date,symbol,direction,entry_price,executed,execution_price,"
            "triggered,raw_instruction_json,confidence,decision_id "
            "FROM playbook_instructions WHERE date BETWEEN '2026-09-20' AND '2026-10-19' "
            "AND executed=1")]
        trades: dict[str, dict] = {}
        for r in con.execute(
            "SELECT decision_id,symbol,direction,entry_time,entry_price,exit_time,"
            "exit_reason,pnl_net,quantity,mfe_pct,metadata_json "
            "FROM trade_records WHERE source_type='PLAYBOOK_SIM' "
            "AND exit_time IS NOT NULL AND exit_time<>'' "
            "AND trade_date BETWEEN '2026-09-20' AND '2026-10-19' "
            "ORDER BY entry_time"):
            row = dict(r)
            key = str(row["decision_id"] or "")
            if key and key not in trades:
                trades[key] = row
    finally:
        con.close()
    return instructions, trades


def render(report: dict) -> None:
    coverage = report["coverage"]
    print("=" * 72)
    print("P0 路由反事实: 确认位限价 + TTL vs 实际市价 (只读)")
    print(f"指令 {coverage['instructions']} / 可评估 {coverage['rows']} / 缺 bar "
          f"{coverage['skipped_no_bars']}")
    for name, block in report["variants"].items():
        print(f"-- {name} --")
        print(f"  eligible={block['eligible_n']} filled={block['filled_n']} "
              f"skipped={block['skipped_n']} fill={block['fill_rate_pct']}%")
        print(f"  actual_net={block['actual_net']} cf_net={block['cf_net']} "
              f"delta={block['delta_net']} | entry_improve={block['entry_improvement_bps']}")
        print(f"  skipped_actual_net={block['skipped_actual_net']} "
              f"skipped_winners={block['skipped_winners_n']} "
              f"skipped_left_tail={block['skipped_left_tail_n']}")
        for arch, sub in block["by_archetype"].items():
            print(f"    {arch}: n={sub.get('n')} actual={sub.get('actual_net')} "
                  f"cf={sub.get('cf_net')} delta={sub.get('delta_net')} "
                  f"fill={sub.get('fill_rate_pct')}% skipped={sub.get('skipped_n')}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    instructions, trades = load_data(Path(args.db))
    report = compute(instructions, trades)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    render(report)
    print(f"已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
