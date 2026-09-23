#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
选股宇宙扩张 · 影子版 (Universe Expansion Shadow)
================================================
纯只读、零 live 行为改动. 只记录"如果按板块轮动扩池, 会浮现哪些宇宙外的标的",
以及这些标的事后真实走势, 用于验证 P0(板块轮动→自动扩池) 的泛化/获利能力.

背景 (2026-07-14):
  盘前扫描器已能检测板块轮动 (strongest/weakest sectors), 但选股宇宙固定 (~43 只,
  多为中概+大科技). 当资金轮入 Energy 时, 系统"看得见、打不着"(宇宙里无能源票).
  本影子闭合该缺口: 轮入板块 → 拉板块成分股 → 筛出宇宙外的异动名 → 记录 → 事后评分.
  验证有数据后, 再决定是否让它真正驱动 daily_active_pool 扩池.

三个子命令:
  capture  盘中跑一次: 算板块涨跌 → 轮入板块的宇宙外异动成分 → 落库 (按天幂等)
  score    次日跑: 对已 capture 未评分的标的取现价 → 算前瞻收益 + 命中率 → 落库
  report   查看: 汇总近 N 天 capture/score → 命中率/平均前瞻收益/每日明细

落库: data/shadow/universe_expansion_shadow.jsonl (capture 与 score 记录混写, 用 type 区分)

运行:
  PYTHONPATH=. ./venv/bin/python scripts/universe_expansion_shadow.py capture
  PYTHONPATH=. ./venv/bin/python scripts/universe_expansion_shadow.py score
  PYTHONPATH=. ./venv/bin/python scripts/universe_expansion_shadow.py report --days 30
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SHADOW_PATH = _ROOT / "data" / "shadow" / "universe_expansion_shadow.jsonl"
_POOL_PATH = _ROOT / "data" / "daily_active_pool.json"

# ── 阈值 (影子期保守; 有数据后再调) ─────────────────────────────
STRONG_SECTOR_PCT = 1.0    # 板块 ETF 涨/跌 ≥ 此值 → 视为轮入/轮出
MOVER_PCT = 2.0            # 成分股同向异动 ≥ 此值 → 浮现为候选
HIT_PCT = 1.0             # 评分时: 前瞻收益 (按方向) ≥ 此值 → 记为命中
BACKFILL_HORIZON_DAYS = 3  # 2026-09-23: horizon > 此值 → 记为回填/长窗批 (见 cmd_score 注释)


def _horizon_days(scored_on, capture_date):
    """(scored_on − capture_date).days; 解析失败 → None (2026-09-23: horizon 批次标记用)。"""
    try:
        return (datetime.strptime(str(scored_on)[:10], "%Y-%m-%d")
                - datetime.strptime(str(capture_date)[:10], "%Y-%m-%d")).days
    except Exception:
        return None


def _batch_of(horizon_days):
    """horizon > BACKFILL_HORIZON_DAYS 或不可解析 → "backfill" (长窗/回填批), 否则 "daily"。"""
    return "backfill" if (horizon_days is None or horizon_days > BACKFILL_HORIZON_DAYS) else "daily"
MAX_SURFACED_PER_SECTOR = 6  # 每板块最多浮现数 (控制噪音)

# ── 归因分类 (2026-07-14) ─────────────────────────────────────
# 让本信息源产生的候选/交易全程可追溯, 与其它来源(playbook_sim/scalping...)隔离,
# 便于事后按来源切片算盈亏; 取值风格对齐 trade_records.source_type / strategy_id.
SOURCE_TYPE = "UNIVERSE_EXPANSION"              # → trade_records.source_type
STRATEGY_ID_PAPER = "universe_expansion_sim"    # 纸面影子; 真实模拟盘阶段用 universe_expansion
INFO_SOURCE = "SECTOR_ROTATION_UNIVERSE_EXPANSION"

# SPDR 板块 ETF → risk.sector_limits.DEFAULT_SECTOR_MAPPING 的板块键
ETF_TO_SECTOR = {
    "XLK": "Technology",
    "XLF": "Financial",
    "XLE": "Energy",
    "XLV": "Healthcare",
    "XLY": "Consumer",
    "XLP": "Consumer",
    "XLI": "Industrial",
    "XLB": "Materials",
    "XLRE": "RealEstate",
    "XLU": "Utilities",
    "XLC": "Communication",
}
ETF_NAME = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Healthcare",
    "XLY": "Consumer Discretionary", "XLP": "Consumer Staples", "XLI": "Industrials",
    "XLB": "Materials", "XLRE": "Real Estate", "XLU": "Utilities",
    "XLC": "Communication Services",
}


# ── 通用小工具 ────────────────────────────────────────────────
def _now_et() -> datetime:
    """ET 钟 (DST 安全)。

    2026-09-20 时钟台账变种1清剿: 原硬编码 UTC-4 是"永久EDT假钟"——冬令时
    (EST=UTC-5) 全程快 1 小时, ET 23:00-24:00 窗内日期戳落到次日 (旧注释
    "精度足够"不成立, 日期错位即产物归日错)。委托 app.utils.timezone.now_et
    (pytz→zoneinfo→固定偏移链); 独立运行兜底也必须是 ET 钟, 不回退硬编码偏移。
    """
    try:
        from app.utils.timezone import now_et
        return now_et()
    except Exception:  # pragma: no cover - app 不可导入时的 ET 兜底
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))


def _today_et_str() -> str:
    return _now_et().strftime("%Y-%m-%d")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _append_jsonl(record: Dict[str, Any]) -> None:
    _SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_SHADOW_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_universe() -> set:
    """当前选股宇宙 (daily_active_pool 的 code, 去 US. 前缀, 大写)."""
    syms = set()
    try:
        d = json.load(open(_POOL_PATH, encoding="utf-8"))
        for e in d.get("entries", []):
            code = e.get("code") if isinstance(e, dict) else e
            if code:
                syms.add(str(code).replace("US.", "").upper())
    except Exception:
        pass
    return syms


def _build_router():
    from app.data.market_data_router import MarketDataRouter
    return MarketDataRouter(
        twelvedata_api_key=os.getenv("TWELVEDATA_API_KEY"),
        finnhub_api_key=os.getenv("FINNHUB_API_KEY"),
        alphavantage_api_key=os.getenv("ALPHAVANTAGE_API_KEY"),
    )


def _quote_change_pct(q: Dict[str, Any]) -> Optional[float]:
    if not q:
        return None
    price = q.get("price") or q.get("last_price")
    # provider 键名不一: finnhub=previous_close, 其余可能 prev_close_price/prev_close
    prev = q.get("previous_close") or q.get("prev_close_price") or q.get("prev_close")
    if price and prev and prev > 0:
        return round((price - prev) / prev * 100, 2)
    # 兜底: change_percent (finnhub 为小数 0.03=3%)
    cp = q.get("change_percent")
    if cp is not None:
        return round(cp * 100, 2) if abs(cp) < 1 else round(cp, 2)
    return None


def _quote_price(q: Dict[str, Any]) -> Optional[float]:
    if not q:
        return None
    return q.get("price") or q.get("last_price")


def _batch(router, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """批量取行情, 入参自动加 US. 前缀, 返回按裸 symbol 归一."""
    if not symbols:
        return {}
    full = [f"US.{s}" if not s.startswith("US.") else s for s in symbols]
    try:
        raw = router.get_batch_quotes(full) or {}
    except Exception as e:
        print(f"  ⚠️ get_batch_quotes 异常: {e}")
        return {}
    out = {}
    for k, v in raw.items():
        out[str(k).replace("US.", "").upper()] = v
    return out


# ── capture: 记录当日会浮现哪些宇宙外标的 ──────────────────────
def cmd_capture(force: bool = False) -> int:
    date = _today_et_str()
    existing = _read_jsonl(_SHADOW_PATH)
    if not force and any(r.get("type") == "capture" and r.get("date") == date for r in existing):
        print(f"[capture] {date} 已存在 capture 记录, 跳过 (--force 覆写)")
        return 0

    try:
        from app.risk.sector_limits import DEFAULT_SECTOR_MAPPING
    except Exception as e:
        print(f"[capture] 无法导入 DEFAULT_SECTOR_MAPPING: {e}")
        return 1

    router = _build_router()
    universe = _load_universe()

    # 1) 算 11 个板块 ETF 涨跌
    etfs = list(ETF_TO_SECTOR.keys())
    etf_q = _batch(router, etfs)
    sectors = []
    for etf in etfs:
        cp = _quote_change_pct(etf_q.get(etf, {}))
        if cp is None:
            continue
        sectors.append({"etf": etf, "name": ETF_NAME.get(etf, etf), "change_pct": cp})
    sectors.sort(key=lambda x: x["change_pct"], reverse=True)

    strong = [s for s in sectors if s["change_pct"] >= STRONG_SECTOR_PCT]
    weak = [s for s in sectors if s["change_pct"] <= -STRONG_SECTOR_PCT]

    # 2) 轮入板块 → 宇宙外成分股 → 同向异动者浮现 (LONG)
    #    轮出板块 → 宇宙外成分股 → 同向下跌者浮现 (SHORT/规避)
    surfaced: List[Dict[str, Any]] = []

    def _scan(sector_list, direction):
        for s in sector_list:
            members = DEFAULT_SECTOR_MAPPING.get(ETF_TO_SECTOR.get(s["etf"], ""), [])
            cand = [m.upper() for m in members if m.upper() not in universe]
            if not cand:
                continue
            mq = _batch(router, cand)
            rows = []
            for sym in cand:
                cp = _quote_change_pct(mq.get(sym, {}))
                px = _quote_price(mq.get(sym, {}))
                if cp is None or px is None:
                    continue
                aligned = cp if direction == "LONG" else -cp
                if aligned >= MOVER_PCT:
                    rows.append({
                        "symbol": sym, "sector": s["name"], "etf": s["etf"],
                        "direction": direction, "change_pct": cp,
                        "captured_price": round(px, 4),
                        # 归因分类: 这一候选/(未来)交易的来源标签, 全程可追溯
                        "source_type": SOURCE_TYPE,
                        "strategy_id": STRATEGY_ID_PAPER,
                        "trade_mode": "paper_shadow",
                        "origin": {
                            "info_source": INFO_SOURCE,
                            "rotation_etf": s["etf"],
                            "rotation_sector": s["name"],
                            "sector_change_pct": s["change_pct"],
                            "mover_change_pct": cp,
                            "direction_basis": "sector_rotate_in" if direction == "LONG"
                                               else "sector_rotate_out",
                        },
                        # 退出仍由现有退出策略托管, 本源不自定义 SL/TP
                        "exit_management": "existing_exit_engine",
                    })
            rows.sort(key=lambda r: (r["change_pct"] if direction == "LONG" else -r["change_pct"]),
                      reverse=True)
            surfaced.extend(rows[:MAX_SURFACED_PER_SECTOR])

    _scan(strong, "LONG")
    _scan(weak, "SHORT")

    rec = {
        "type": "capture",
        "date": date,
        "captured_at": _now_et().isoformat(),
        "source_type": SOURCE_TYPE,
        "strategy_id": STRATEGY_ID_PAPER,
        "universe_size": len(universe),
        "strong_sectors": strong,
        "weak_sectors": weak,
        "surfaced": surfaced,
        "note": "record-only shadow; 不驱动任何 live 行为; 归因源=" + INFO_SOURCE,
    }
    # 幂等: --force 时先剔除同日旧 capture
    if force:
        kept = [r for r in existing if not (r.get("type") == "capture" and r.get("date") == date)]
        _SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SHADOW_PATH, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _append_jsonl(rec)

    ss = ", ".join(f"{s['etf']}+{s['change_pct']}%" for s in strong) or "无"
    print(f"[capture] {date} 宇宙={len(universe)} 轮入板块=[{ss}] 浮现宇宙外标的={len(surfaced)}")
    for r in surfaced[:12]:
        print(f"    {r['direction']:5} {r['symbol']:6} {r['sector']:22} {r['change_pct']:+.2f}%")
    return 0


# ── score: 对已 capture 未评分标的算前瞻收益 ───────────────────
def cmd_score() -> int:
    records = _read_jsonl(_SHADOW_PATH)
    captures = [r for r in records if r.get("type") == "capture"]
    scored_dates = {r.get("capture_date") for r in records if r.get("type") == "score"}
    # 2026-08-26: 同日评分守卫 — capture 当日 scored_price≈captured_price, fwd≈0 无前瞻
    #   意义且污染命中率 (8/26 首跑实锤 3 标的全 0)。只评至少隔一个自然日的 capture。
    _today_str = _today_et_str()
    todo = [c for c in captures if c.get("date") not in scored_dates and c.get("surfaced")
            and str(c.get("date", "")) < _today_str]
    if not todo:
        print("[score] 无待评分的 capture (需先有 capture 且未评分过)")
        return 0

    router = _build_router()
    today = _today_et_str()
    for c in todo:
        cdate = c.get("date")
        syms = [r["symbol"] for r in c["surfaced"]]
        q = _batch(router, syms)
        results = []
        hits = 0
        fwd_list = []
        for r in c["surfaced"]:
            cur = _quote_price(q.get(r["symbol"], {}))
            cap = r.get("captured_price")
            if not cur or not cap or cap <= 0:
                continue
            raw = (cur - cap) / cap * 100
            fwd = round(raw if r["direction"] == "LONG" else -raw, 2)  # 按方向的前瞻收益
            hit = fwd >= HIT_PCT
            hits += 1 if hit else 0
            fwd_list.append(fwd)
            results.append({
                "symbol": r["symbol"], "direction": r["direction"],
                "sector": r.get("sector"), "captured_price": cap,
                "scored_price": round(cur, 4), "fwd_pct": fwd, "hit": hit,
                # 携带归因标签, 使评分记录也可按来源切片
                "source_type": r.get("source_type", SOURCE_TYPE),
                "strategy_id": r.get("strategy_id", STRATEGY_ID_PAPER),
                "origin": r.get("origin"),
            })
        n = len(results)
        # 2026-09-23 (用户裁「按建议执行」后续): horizon 批次标记。8/26 首启 score 时
        #   一次性回填了 26 个 capture 日 (horizon 1..43 天, 207 只), 与日常批 (1-3 天)
        #   混在同一文件里 → 下游按 scored_on 聚合会把长窗走势当 1 日读数 (实测把
        #   LONG/SHORT 的方向结论整个翻转)。落字段使下游可切片, 不改任何既有字段。
        _h = _horizon_days(today, cdate)
        summary = {
            "n": n,
            "hit_rate": round(hits / n, 3) if n else None,
            "avg_fwd_pct": round(sum(fwd_list) / len(fwd_list), 2) if fwd_list else None,
            "best": max(fwd_list) if fwd_list else None,
            "worst": min(fwd_list) if fwd_list else None,
            "horizon_days": _h,
            "batch": _batch_of(_h),
        }
        _append_jsonl({
            "type": "score", "capture_date": cdate, "scored_at": _now_et().isoformat(),
            "scored_on": today, "results": results, "summary": summary,
        })
        print(f"[score] capture={cdate} horizon={_h}d batch={summary['batch']} "
              f"评分 {n} 标的 命中率={summary['hit_rate']} 平均前瞻={summary['avg_fwd_pct']}%")
    return 0


# ── report: 查看效果 ──────────────────────────────────────────
def cmd_report(days: int = 30) -> int:
    records = _read_jsonl(_SHADOW_PATH)
    if not records:
        print(f"影子文件为空: {_SHADOW_PATH}")
        return 0
    cutoff = (_now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    captures = [r for r in records if r.get("type") == "capture" and r.get("date", "") >= cutoff]
    scores = [r for r in records if r.get("type") == "score" and r.get("capture_date", "") >= cutoff]

    total_surfaced = sum(len(c.get("surfaced", [])) for c in captures)
    uniq = {r["symbol"] for c in captures for r in c.get("surfaced", [])}
    all_fwd = [x["fwd_pct"] for s in scores for x in s.get("results", [])]
    all_hits = [x["hit"] for s in scores for x in s.get("results", [])]

    print("=" * 64)
    print(f"📡 选股宇宙扩张 · 影子效果 (近 {days} 天)   {_SHADOW_PATH.name}")
    print(f"归因分类: source_type={SOURCE_TYPE} | strategy_id={STRATEGY_ID_PAPER} | 退出=现有策略托管")
    print("=" * 64)
    print(f"capture 次数     : {len(captures)}")
    print(f"浮现宇宙外标的   : {total_surfaced} 人次 / {len(uniq)} 只去重")
    if all_fwd:
        hr = round(sum(all_hits) / len(all_hits), 3)
        print(f"已评分标的       : {len(all_fwd)}")
        print(f"命中率(前瞻≥{HIT_PCT}%) : {hr}  ({sum(all_hits)}/{len(all_hits)})")
        print(f"平均前瞻收益     : {round(sum(all_fwd)/len(all_fwd),2)}%   "
              f"(best {max(all_fwd):+.2f}% / worst {min(all_fwd):+.2f}%)")
    else:
        print("已评分标的       : 0  (还没跑过 score, 或 capture 未满一个前瞻周期)")
    print("-" * 64)

    score_by_date = {s["capture_date"]: s for s in scores}
    for c in sorted(captures, key=lambda x: x.get("date", "")):
        d = c.get("date")
        strong = ",".join(s["etf"] for s in c.get("strong_sectors", [])) or "无轮入"
        surf = c.get("surfaced", [])
        line = f"{d}  轮入[{strong}]  浮现{len(surf)}只"
        sc = score_by_date.get(d)
        if sc and sc.get("summary", {}).get("n"):
            sm = sc["summary"]
            # 2026-09-23: 标批次, 防把回填批的长窗读数当日常读数读。历史行无字段 ⇒ 由
            #   (scored_on − capture_date) 现算, 避免老回填行被默认成 daily 骗人。
            _hd = sm.get("horizon_days")
            if _hd is None:
                _hd = _horizon_days(sc.get("scored_on"), sc.get("capture_date"))
            _batch = sm.get("batch") or _batch_of(_hd)
            _tag = str(_batch) + (f",{_hd}d" if _hd is not None else "")
            line += f"  → 命中率{sm['hit_rate']} 平均{sm['avg_fwd_pct']:+}% [{_tag}]"
            if _batch == "backfill":
                line += " ⚠回填批(混合horizon, 勿与日常批混算)"
        print(line)
        for r in surf[:8]:
            tag = ""
            if sc:
                m = next((x for x in sc.get("results", []) if x["symbol"] == r["symbol"]), None)
                if m:
                    tag = f"  → 前瞻{m['fwd_pct']:+.2f}% {'✅' if m['hit'] else '❌'}"
            print(f"      {r['direction']:5} {r['symbol']:6} {r.get('sector',''):20} "
                  f"{r['change_pct']:+.2f}%{tag}")
    print("=" * 64)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="选股宇宙扩张影子")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_cap = sub.add_parser("capture", help="记录当日会浮现的宇宙外标的")
    p_cap.add_argument("--force", action="store_true", help="覆写当日已有 capture")
    sub.add_parser("score", help="对已 capture 未评分标的算前瞻收益")
    p_rep = sub.add_parser("report", help="查看近 N 天效果")
    p_rep.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    try:
        if args.cmd == "capture":
            return cmd_capture(force=args.force)
        if args.cmd == "score":
            return cmd_score()
        if args.cmd == "report":
            return cmd_report(days=args.days)
    except Exception as e:
        import traceback
        print(f"⚠️ 执行异常 ({args.cmd}): {e}")
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
