#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件驱动标的生成器 (P2' 池子事件化, Component A; 2026-07-21 标的量治理辩论落地).

背景: DA 每日分析宇宙 = 扫描器5只动量股 + cron 硬编码15只静态熟股, 无事件日子里
LLM 对静态大盘股"无话可说" → gate BLOCK 归因实测 59% 为 "LLM无方向(SKIP)".
本脚本每日生成"事件驱动标的清单" (top 10, 带 reason 标签), 供:
  ① DA cron (--symbols = 核心5熟股 + 事件top10, jobs.json prompt 引用);
  ② UE clue brief [今日事件驱动标的] 段 (llm_universe_scan._gather_event_symbols_file).

三个信号源全部复用既有零/低成本组件 (与 ADR-017/018 同源):
  - 硬事件:   CatalystClassifier.classify(use_llm=False) 近24h新闻 (零 LLM 成本)
  - 财报:     EarningsCalendar 已发布(1d动量) + 临近(3d时点) (1次调用+内置缓存)
  - 内部人:   data/reports/insider_daily_{ET date}.json 集群买入 (无网络)

输出: data/event_symbols_{ET date}.json (原子写; 重跑幂等覆盖).
quiet day (无事件) → event_symbols=[], symbols_arg 回退核心熟股, 行为退化为现状.
只读信号源, 不改任何库/配置; 可安全随时手动运行.

用法:
  PYTHONPATH=. ./venv/bin/python scripts/gen_event_symbols.py [--date 2026-07-21] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 核心熟股 (保留单股记忆积累; 取自原静态15只中近期有成交/高频分析者).
# 事件名单为空时的 fallback, 行为=现状. 调整此列表=改本常量即可.
CORE_FAMILIAR = ["US.MSFT", "US.AAPL", "US.NVDA", "US.ORCL", "US.LLY"]

_SEV_SCORE = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1}
_TOP_N = 10


def _now_et() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=4)


def _today_et() -> str:
    return _now_et().strftime("%Y-%m-%d")


def _is_trading_day_et(date_str: str) -> bool:
    """[P3 2026-09-08 遗留清单] 假日守卫 — 与 llm_universe_scan 同款 fail-open.

    gen_event_symbols 产物 (event_symbols_{ET}.json) 仅被 DA(--symbols)/UE(clue) 消费,
    二者自身已按交易日守卫跳过 (bace63a6 同批修 9 处). 非交易日生成 = 无人消费的白跑.
    日历不可用 → 放行 (宁可假日白跑一轮, 不可让守卫在依赖故障日误拦交易日).
    """
    try:
        from datetime import date as _date
        from app.utils.holiday_calendar import get_holiday_calendar
        _y, _m, _d = (int(x) for x in date_str.split("-"))
        return bool(get_holiday_calendar().is_trading_day("US", _date(_y, _m, _d)))
    except Exception as e:
        print(f"  [warn] 交易日判定失败 (放行): {e}", file=sys.stderr)
        return True


def _add(bag: Dict[str, Dict[str, Any]], symbol: str, score: int, reason: str) -> None:
    sym = str(symbol).strip().upper()
    if not sym or sym in ("(板块传导)", ""):
        return
    if not sym.startswith("US."):
        sym = f"US.{sym}"
    # 过滤指数/ETF 占位 (DA 只分析个股)
    base = sym[3:]
    if not base.isalpha() or len(base) > 6:
        return
    e = bag.setdefault(sym, {"symbol": sym, "score": 0, "reasons": []})
    e["score"] += score
    if reason not in e["reasons"]:
        e["reasons"].append(reason)


def gather_hard_events(news_hours: int = 24) -> Dict[str, Dict[str, Any]]:
    """硬事件 (CatalystClassifier 关键词分类, 零成本) → 按 severity 计分."""
    bag: Dict[str, Dict[str, Any]] = {}
    try:
        from llm_universe_scan import _gather_news  # 复用同一新闻源 (RSSHub)
        from app.catalyst.event_classifier import CatalystClassifier
        from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency
    except Exception as e:
        print(f"  [warn] 硬事件组件不可用 (降级): {e}", file=sys.stderr)
        return bag
    news = _gather_news(news_hours)
    if not news:
        return bag
    items = []
    for n in news:
        rel = n.get("symbols") or "[]"
        if isinstance(rel, str):
            try:
                rel = json.loads(rel)
            except Exception:
                rel = []
        if not isinstance(rel, list):
            rel = []
        try:
            items.append(UnifiedNewsItemV2(
                id=str(n.get("id", "")), title=str(n.get("title", "")),
                content=str(n.get("summary", "")), source=str(n.get("source", "rsshub")),
                source_name=str(n.get("source_name") or n.get("source", "rsshub")),
                timestamp=0.0, related_stocks=[str(s) for s in rel],
                urgency=NewsUrgency.NORMAL,
            ))
        except Exception:
            continue
    try:
        events = CatalystClassifier().classify(items, use_llm=False)
    except Exception as e:
        print(f"  [warn] 催化分类失败 (降级): {e}", file=sys.stderr)
        return bag
    for ev in events:
        sev = getattr(ev, "severity", "MEDIUM")
        sc = _SEV_SCORE.get(sev, 1)
        head = (getattr(ev, "headline", "") or "").strip()[:60]
        for tk in (getattr(ev, "tickers", None) or []):
            _add(bag, str(tk), sc, f"HARD_EVENT[{sev}/{getattr(ev, 'event_type', '?')}] {head}")
    return bag


def gather_earnings() -> Dict[str, Dict[str, Any]]:
    """财报: 已发布(动量, +2) / 临近≤3d(时点, +1).

    2026-07-21 实测: 共享 ALPHAVANTAGE key 限流(免费档 25 次/日, 多组件共用)时
    API 返回 200+JSON note → CSV 解析 0 行 → 静默空. demo key 对 EARNINGS_CALENDAR
    端点可用, 主路径全空时以只读 fallback 重试一次; 真空周两次皆空也无害."""
    bag: Dict[str, Dict[str, Any]] = {}
    try:
        from app.data.earnings_calendar import EarningsCalendar
        ec = EarningsCalendar()
        recent = ec.get_recently_reported(days_back=1)[:10]
        upcoming = ec.get_watchlist_earnings(days_ahead=3)[:10]
        if not recent and not upcoming:
            print("  [warn] 财报日历主路径为空(疑限流), demo key 重试", file=sys.stderr)
            ec2 = EarningsCalendar(api_key="demo")
            recent = ec2.get_recently_reported(days_back=1)[:10]
            upcoming = ec2.get_watchlist_earnings(days_ahead=3)[:10]
        for e in recent:
            _add(bag, e.get("ticker", ""), 2,
                 f"EARNINGS_REPORTED {e.get('report_date', '')} → 财报后动量")
        for u in upcoming:
            _add(bag, u.get("ticker", ""), 1,
                 f"EARNINGS_UPCOMING {u.get('report_date', '')} ({u.get('days_until', '?')}d) → 时点风险")
    except Exception as e:
        print(f"  [warn] 财报日历不可用 (降级): {e}", file=sys.stderr)
    return bag


def gather_insider(date: str) -> Dict[str, Dict[str, Any]]:
    """内部人集群买入 (无网络, +2)."""
    bag: Dict[str, Dict[str, Any]] = {}
    try:
        path = _ROOT / "data" / "reports" / f"insider_daily_{date}.json"
        if not path.exists():
            return bag
        d = json.loads(path.read_text())
        for sym, cnt in ((d.get("stats") or {}).get("by_symbol") or {}).items():
            _add(bag, str(sym), 2, f"INSIDER_CLUSTER x{cnt} → 看多")
    except Exception as e:
        print(f"  [warn] 内部人集群读取失败 (降级): {e}", file=sys.stderr)
    return bag


def merge_bags(*bags: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for bag in bags:
        for sym, e in bag.items():
            m = merged.setdefault(sym, {"symbol": sym, "score": 0, "reasons": []})
            m["score"] += e["score"]
            for r in e["reasons"]:
                if r not in m["reasons"]:
                    m["reasons"].append(r)
    out = sorted(merged.values(), key=lambda x: -x["score"])
    return out[:_TOP_N]


def main() -> int:
    ap = argparse.ArgumentParser(description="事件驱动标的生成器 (DA --symbols / UE clue 共用)")
    ap.add_argument("--date", default=_today_et(), help="ET 交易日, 默认今天")
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON 而非人类摘要")
    args = ap.parse_args()
    date = args.date

    # [P3 2026-09-08] 假日守卫: 非交易日 → 跳过生成 (产物无消费方, 白跑).
    #   与 9 个已修守卫同批语义 (bace63a6): 周末由 cron 表达式排除, 假日由本守卫兜底.
    if not _is_trading_day_et(date):
        print(f"[gen_event_symbols] {date} 非美股交易日, 跳过 (产物仅 DA/UE 交易日消费)")
        return 0

    events = merge_bags(gather_hard_events(), gather_earnings(), gather_insider(date))
    quiet = not events
    symbols = list(CORE_FAMILIAR) + [e["symbol"] for e in events if e["symbol"] not in CORE_FAMILIAR]
    payload = {
        "date": date,
        "generated_at": _now_et().isoformat(),
        "core": CORE_FAMILIAR,
        "event_symbols": events,
        "quiet_day_fallback": quiet,
        "symbols_arg": ",".join(symbols),
    }
    out_path = _ROOT / "data" / f"event_symbols_{date}.json"
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out_path)  # 原子写, 消费端不会读到半截文件

    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"[gen_event_symbols] {date} → {out_path.name}")
        if quiet:
            print("  quiet day: 无事件标的, 回退核心熟股 (行为=现状)")
        for e in events:
            print(f"  {e['symbol']} score={e['score']}")
            for r in e["reasons"]:
                print(f"    • {r}")
        print(f"  --symbols 参数: {payload['symbols_arg']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
