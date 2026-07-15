#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 事件推理驱动的选股宇宙扩张 (LLM Universe Scan)
==================================================
把 LLM 用在"发现哪些标的"这一步: 读今日新闻事件 + 市场佐证 → LLM 关联因果推理 →
浮现受影响标的(含宇宙外, 受益+受损方) → 代码出数值(entry/SL/TP) → 写 playbook_instructions →
与 DA 共用同一自动执行/触发/退出轨道 在模拟盘执行 (2026-07: 从 fast_lane 迁移, 统一执行层)。

设计 (见 plan: LLM 事件推理驱动的选股宇宙扩张):
  - 信息源 = 新闻事件为主 + 市场佐证(板块轮动/异动)
  - LLM 出判断(方向/因果链/thesis/置信度), 代码出数值(Track A)
  - 独立 strategy_id=universe_expansion, 与 playbook_sim 物理隔离
  - feature flag universe_expansion.entry_enabled 默认关 → 只 scan/dry-run 不下单

子命令:
  scan    组装 clue brief → LLM 推理 → 落影子日志 (含 causal_chain/thesis); 不写作战计划
  inject  读最近 scan → 代码算 entry/SL/TP → 写 playbook_instructions (走 DA 统一执行轨道, 仅 entry_enabled=true)
  score   对已 scan 未评分标的取现价 → 前瞻收益 + 命中率
  report  查看近 N 天 scan/score 效果 (归因源=LLM_EVENT_SCAN)

运行:
  PYTHONPATH=. ./venv/bin/python scripts/llm_universe_scan.py scan [--clue "手动线索文本"]
  PYTHONPATH=. ./venv/bin/python scripts/llm_universe_scan.py inject [--dry-run]
  PYTHONPATH=. ./venv/bin/python scripts/llm_universe_scan.py score
  PYTHONPATH=. ./venv/bin/python scripts/llm_universe_scan.py report --days 30
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
_SCRIPTS = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_SCAN_PATH = _ROOT / "data" / "shadow" / "llm_universe_scan.jsonl"
_BATTLE_PLAN_PATH = _ROOT / "data" / "daily_battle_plan_us.json"
_MECH_CAPTURE_PATH = _ROOT / "data" / "shadow" / "universe_expansion_shadow.jsonl"

# 归因分类 (与 config/signal_stage 对齐)
DISCOVERY_SOURCE = "LLM_EVENT_SCAN"       # → battle_plan target.discovery_source
SOURCE_TYPE = "UNIVERSE_EXPANSION"        # → trade_records.source_type (语义标签)
STRATEGY_ID = "universe_expansion"        # → trade_records.strategy_id (物理隔离)
INFO_SOURCE = "LLM_EVENT_REASONING"

HIT_PCT = 1.0  # 评分: 前瞻收益(按方向) ≥ 此值 → 命中
PROMPT_VERSION = "v2.0"  # scan-eval 的 V2 prompt 版本 (未来迭代 P1/P2 改此号, 供归因)

SYSTEM_PROMPT = (
    "你是资深美股宏观-产业链关联分析师。基于给定的今日新闻事件、板块轮动与异动线索, "
    "做关联因果推理, 找出直接与间接受影响的美股标的 —— 既要受益方(做多), 也要受损方(做空), "
    "尤其挖掘不在常规观察池里的标的(泛化)。\n"
    "对每个标的给出字段: symbol(美股代码, 不带交易所前缀), direction(LONG/SHORT/SKIP), "
    "causal_chain(事件→传导→该标的的因果链, 一句话), thesis(交易论点, ≤60字), "
    "confidence(0~1 小数), is_universe_external(bool, 是否常规池外), "
    "winner_loser(WINNER 或 LOSER), corroborated_by_market(bool, 市场异动是否已印证)。\n"
    "只输出可在美股实际交易的真实 symbol; 不确定的标的 direction 填 SKIP。"
    "不要输出任何价格数字(entry/止损/止盈由下游代码计算)。\n"
    "严格只输出 JSON, 格式: {\"surfaced\": [ {字段...}, ... ]}。"
)

# P0 (意图对齐 + 强制引证自评): 与 V1 同源, 仅用于 scan-eval A/B; 不覆盖 V1。
SYSTEM_PROMPT_V2 = (
    "你是资深事件驱动型美股分析师。职责: 仅当存在【明确、可核验的因果传导路径】时才浮现标的; "
    "没有强事件支撑时, 宁可少报或不报, 绝不为凑数做板块联想。\n"
    "要求:\n"
    "1. evidence_ref(强制): 每个标的必须引用给定材料中的【具体证据原文】—— 某条新闻标题 / "
    "某个市场异动数据 / 某条人工线索。若无法从给定材料引出具体证据(仅凭行业常识或板块联想), evidence_ref 留空。\n"
    "2. self_tier(强制自评): HARD_EVENT=因果链第一环锚定 evidence_ref 里的具体事件(封锁/战争/财报/监管/事故); "
    "MARKET_CORROBORATED=无具体事件但 evidence_ref 是真实市场异动/板块轮动数据; "
    "THESIS_ONLY=无 evidence_ref, 仅逻辑或板块联想。\n"
    "3. 自律约束: evidence_ref 为空者, self_tier 必须=THESIS_ONLY 且 confidence≤0.60。\n"
    "4. 宁缺毋滥: 0~2 个高置信标的优于 5 个勉强凑数; 今日无强事件则返回 {\"surfaced\": []}。\n"
    "5. 不输出任何价格数字(entry/止损/止盈由下游计算); 只输出可真实交易的美股 symbol(不带前缀); 不确定填 direction=SKIP。\n"
    "每个标的字段: symbol, direction(LONG/SHORT/SKIP), causal_chain(事件→传导→标的, 一句话), "
    "evidence_ref(引用的具体证据原文, 无则空), self_tier(HARD_EVENT/MARKET_CORROBORATED/THESIS_ONLY), "
    "thesis(≤60字), confidence(0~1 小数), is_universe_external(bool), winner_loser(WINNER/LOSER), "
    "corroborated_by_market(bool)。\n"
    "严格只输出 JSON, 格式: {\"surfaced\": [ {字段...}, ... ]}。"
)


# ── 通用工具 ──────────────────────────────────────────────────
def _now_et() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=4)


def _today() -> str:
    return _now_et().strftime("%Y-%m-%d")


def _is_us_trading_day_today() -> bool:
    """美股交易日判断 (复用 holiday_calendar 权威能力: Futu缓存+硬编码降级+周末兑底).

    cron `30 20 * * 1-5` 仅排周末, 不排美股节假日 → 靠本守卫避免假日白跑 LLM/写陈旧指令.
    日历不可用时 fail-open 按交易日 (与 app/playbook _is_trading_day_today 一致, 宁可跑不可漏).
    """
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        return get_holiday_calendar().is_trading_day("US", _now_et().date())
    except Exception:
        return True


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _cfg(dotted: str, default=None):
    try:
        from app.core.config_container import get_config
        v = get_config().get_dotted(dotted, default)
        return v if v is not None else default
    except Exception:
        return default


def _build_router():
    from app.data.market_data_router import MarketDataRouter
    return MarketDataRouter(
        twelvedata_api_key=os.getenv("TWELVEDATA_API_KEY"),
        finnhub_api_key=os.getenv("FINNHUB_API_KEY"),
        alphavantage_api_key=os.getenv("ALPHAVANTAGE_API_KEY"),
    )


def _batch_quotes(router, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    full = [f"US.{s}" if not str(s).startswith("US.") else s for s in symbols]
    try:
        raw = router.get_batch_quotes(full) or {}
    except Exception as e:
        print(f"  [warn] get_batch_quotes 异常: {e}")
        return {}
    return {str(k).replace("US.", "").upper(): v for k, v in raw.items()}


def _quote_price(q: Dict[str, Any]) -> Optional[float]:
    if not q:
        return None
    return q.get("price") or q.get("last_price")


# ── clue brief 组装 (信息获取) ────────────────────────────────
def _gather_news(hours: int) -> List[Dict[str, Any]]:
    """近 N 小时新闻 (RSSHub, llm_pipeline 同源); 失败返回 []."""
    try:
        from app.data.rsshub_us_news import RSSHubUSNewsFetcher
        items = RSSHubUSNewsFetcher().get_recent_news(hours=hours, limit=30) or []
        return items
    except Exception as e:
        print(f"  [warn] 新闻拉取失败 (降级): {e}")
        return []


def _gather_market_corroboration() -> Dict[str, Any]:
    """市场佐证: 复用机械影子最近一次 capture 的板块轮动/异动 (若有)."""
    recs = _read_jsonl(_MECH_CAPTURE_PATH)
    caps = [r for r in recs if r.get("type") == "capture"]
    if not caps:
        return {}
    last = caps[-1]
    return {
        "date": last.get("date"),
        "strong_sectors": [f"{s['etf']}({s['name']}) {s['change_pct']:+}%"
                           for s in last.get("strong_sectors", [])],
        "weak_sectors": [f"{s['etf']}({s['name']}) {s['change_pct']:+}%"
                         for s in last.get("weak_sectors", [])],
        "movers": [f"{r['symbol']} {r['direction']} {r['change_pct']:+}%"
                   for r in last.get("surfaced", [])],
    }


def _gather_hard_events(news: List[Dict[str, Any]]) -> List[str]:
    """[ADR-017] 复用 CatalystClassifier(纯关键词, use_llm=False) 把已拉取新闻分类为
    结构化硬事件 → clue brief 的 [硬事件] 段, 供 V2 自评 HARD_EVENT。失败降级返回 []。"""
    if not news:
        return []
    try:
        from app.catalyst.event_classifier import CatalystClassifier
        from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency
    except Exception as e:
        print(f"  [warn] 催化分类器不可用 (降级, 无硬事件段): {e}")
        return []
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
                id=str(n.get("id", "")),
                title=str(n.get("title", "")),
                content=str(n.get("summary", "")),
                source=str(n.get("source", "rsshub")),
                source_name=str(n.get("source_name") or n.get("source", "rsshub")),
                timestamp=0.0,
                related_stocks=[str(s) for s in rel],
                urgency=NewsUrgency.NORMAL,
            ))
        except Exception:
            continue
    try:
        events = CatalystClassifier().classify(items, use_llm=False)
    except Exception as e:
        print(f"  [warn] 催化分类失败 (降级): {e}")
        return []
    _rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
    events.sort(key=lambda ev: _rank.get(getattr(ev, "severity", "MEDIUM"), 3))
    out = []
    for ev in events[:15]:
        tks = ", ".join(str(t).replace("US.", "") for t in (ev.tickers or []))
        head = (ev.headline or "").strip()[:70]
        out.append(f"[{ev.severity}/{ev.event_type}] {head} → 关联: {tks or '(板块传导)'}")
    return out


def _gather_earnings_events() -> List[str]:
    """[ADR-018] 复用 EarningsCalendar (Alpha Vantage, 仅 1 次日历调用+内置缓存) 注入
    财报硬事件: 近期已发布(EARNINGS) + 临近(EARNINGS_UPCOMING 时点风险)。
    不用逐股 history 接口(5/min 限流不稳)。失败降级返回 []。"""
    try:
        from app.data.earnings_calendar import EarningsCalendar
    except Exception as e:
        print(f"  [warn] 财报日历不可用 (降级): {e}")
        return []
    out: List[str] = []
    try:
        ec = EarningsCalendar()
        for e in ec.get_recently_reported(days_back=2)[:8]:
            out.append(f"[HIGH/EARNINGS] {e.get('ticker','')} 财报已发布 {e.get('report_date','')}"
                       f"({e.get('time_of_day','')}) → 关注财报后动量")
        for u in ec.get_watchlist_earnings(days_ahead=3)[:8]:
            out.append(f"[MEDIUM/EARNINGS_UPCOMING] {u.get('ticker','')} 财报临近 {u.get('report_date','')}"
                       f"({u.get('days_until','?')}d) → 时点风险")
    except Exception as e:
        print(f"  [warn] 财报事件拉取失败 (降级): {e}")
    return out


def _gather_insider_clusters() -> List[str]:
    """[ADR-018] 读今日已持久化的内部人集群报告 (insider_daily_{date}.json, 无网络).
    集群买入=看多硬事件。无今日文件/无信号 → []。"""
    try:
        path = _ROOT / "data" / "reports" / f"insider_daily_{_today()}.json"
        if not path.exists():
            return []
        d = json.loads(path.read_text())
        by_sym = (d.get("stats") or {}).get("by_symbol") or {}
        out: List[str] = []
        for sym, cnt in by_sym.items():
            t = str(sym).replace("US.", "")
            out.append(f"[HIGH/INSIDER_CLUSTER] {t} 内部人集群买入信号 x{cnt} → 看多")
        return out
    except Exception as e:
        print(f"  [warn] 内部人集群读取失败 (降级): {e}")
        return []


def _build_clue_brief(manual_clue: str = "") -> str:
    hours = int(_cfg("universe_expansion.news_lookback_hours", 24))
    parts = [f"日期: {_today()} (ET)"]

    if manual_clue:
        parts.append(f"\n[人工线索]\n{manual_clue.strip()}")

    news = _gather_news(hours)

    # [ADR-017/018] 结构化硬事件段 (config 驱动, 传导优先级最高, 供 V2 自评 HARD_EVENT)
    hard: List[str] = []
    if bool(_cfg("universe_expansion.clue_hard_events", False)):
        hard += _gather_hard_events(news)          # 新闻分类 (关键词, 零成本)
    if bool(_cfg("universe_expansion.clue_earnings_events", False)):
        hard += _gather_earnings_events()          # 财报日历 (Alpha Vantage)
    if bool(_cfg("universe_expansion.clue_insider_clusters", False)):
        hard += _gather_insider_clusters()         # 内部人集群 (已持久化文件)
    if hard:
        parts.append("\n[硬事件 (已分类, 传导优先级最高)]")
        parts.extend(hard)

    mkt = _gather_market_corroboration()
    if mkt:
        parts.append("\n[市场佐证 (板块轮动/异动)]")
        if mkt.get("strong_sectors"):
            parts.append("轮入板块: " + ", ".join(mkt["strong_sectors"]))
        if mkt.get("weak_sectors"):
            parts.append("轮出板块: " + ", ".join(mkt["weak_sectors"]))
        if mkt.get("movers"):
            parts.append("宇宙外异动: " + ", ".join(mkt["movers"][:12]))

    if news:
        parts.append(f"\n[近{hours}h 新闻头条 (共{len(news)}条, 取前20)]")
        for n in news[:20]:
            title = (n.get("title") or "").strip()
            syms = n.get("symbols") or ""
            if title:
                parts.append(f"- {title}" + (f"  [{syms}]" if syms else ""))
    else:
        parts.append("\n[新闻] 无近期新闻 (依赖市场佐证 + 人工线索推理)")

    return "\n".join(parts)


# ── scan: LLM 推理浮现标的 ─────────────────────────────────────
def _normalize_surfaced(raw: Any, with_tier: bool = False) -> List[Dict[str, Any]]:
    """归一化 LLM 浮现标的。with_tier=True 时额外解析 evidence_ref/self_tier,
    并做 Track A 夹逼: evidence_ref 为空 → 强制 self_tier=THESIS_ONLY 且 confidence≤0.60
    (标签归一化, 非过滤门; V1 路径不传 with_tier, 行为不变)。"""
    if not isinstance(raw, list):
        return []
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        sym = str(it.get("symbol", "")).replace("US.", "").upper().strip()
        direction = str(it.get("direction", "SKIP")).upper().strip()
        if not sym or direction not in ("LONG", "SHORT", "SKIP"):
            continue
        try:
            conf = float(it.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            conf = 0.0
        wl = str(it.get("winner_loser", "")).upper().strip()
        item = {
            "symbol": sym,
            "direction": direction,
            "causal_chain": str(it.get("causal_chain", ""))[:200],
            "thesis": str(it.get("thesis", ""))[:120],
            "confidence": round(conf, 3),
            "is_universe_external": bool(it.get("is_universe_external", False)),
            "winner_loser": wl if wl in ("WINNER", "LOSER") else "",
            "corroborated_by_market": bool(it.get("corroborated_by_market", False)),
        }
        if with_tier:
            ev = str(it.get("evidence_ref", ""))[:200].strip()
            tier = str(it.get("self_tier", "")).upper().strip()
            if tier not in ("HARD_EVENT", "MARKET_CORROBORATED", "THESIS_ONLY"):
                tier = "THESIS_ONLY"
            raw_conf = conf
            clamped = False
            if not ev:  # Track A 夹逼: 无证据 → 强制降级 (兜底 LLM 不守自律约束)
                tier = "THESIS_ONLY"
                if conf > 0.60:
                    clamped = True
                conf = min(conf, 0.60)
            item["evidence_ref"] = ev
            item["self_tier"] = tier
            item["raw_confidence"] = round(raw_conf, 3)
            item["clamped"] = clamped
            item["confidence"] = round(conf, 3)
        out.append(item)
    return out


def cmd_scan(manual_clue: str = "") -> int:
    if not _is_us_trading_day_today():
        print(f"[scan] {_today()} 非美股交易日 (周末/节假日), 跳过扫描")
        return 0
    brief = _build_clue_brief(manual_clue)
    print("=" * 64)
    print("clue brief (喂给 LLM):")
    print(brief[:1500])
    print("=" * 64)

    try:
        from app.llm.model_call import call_llm
    except Exception as e:
        print(f"[scan] 无法导入 call_llm: {e}")
        return 1

    # [ADR-016] live scan prompt 版本 config 驱动 (v1|v2); 默认 v1 安全回退,
    # 设 universe_expansion.prompt_version=v2 → 事件驱动+强制引证+Track A 夹逼 (更克制/更稳)。
    _pv = str(_cfg("universe_expansion.prompt_version", "v1")).strip().lower()
    _use_v2 = _pv in ("v2", "v2.0")
    _system = SYSTEM_PROMPT_V2 if _use_v2 else SYSTEM_PROMPT
    print(f"[scan] prompt_version={'v2' if _use_v2 else 'v1'} (config: universe_expansion.prompt_version)")
    res = call_llm(
        "universe_expansion_reasoning",
        brief,
        system=_system,
        temperature=0.2,
        max_tokens=2500,
        schema_required={"surfaced": list},
        json_mode=True,
    )
    if not res or not res.success:
        err = getattr(res, "error", "unknown") if res else "no_result"
        print(f"[scan] LLM 调用失败 (不落库, 不下单): {err}")
        return 1

    parsed = res.response if isinstance(res.response, dict) else {}
    surfaced = _normalize_surfaced(parsed.get("surfaced"), with_tier=_use_v2)
    if not surfaced:
        print("[scan] LLM 未浮现有效标的 (schema 通过但 surfaced 空)")
        return 0

    # 为可评分, 抓 scan 时参考价
    router = _build_router()
    tradeable = [s for s in surfaced if s["direction"] in ("LONG", "SHORT")]
    q = _batch_quotes(router, [s["symbol"] for s in tradeable])
    for s in surfaced:
        px = _quote_price(q.get(s["symbol"], {}))
        s["scan_price"] = round(px, 4) if px else None

    rec = {
        "type": "scan",
        "date": _today(),
        "scanned_at": _now_et().isoformat(),
        "source_type": SOURCE_TYPE,
        "strategy_id": STRATEGY_ID,
        "discovery_source": DISCOVERY_SOURCE,
        "info_source": INFO_SOURCE,
        "model_used": getattr(res, "model_used", None),
        "prompt_version": PROMPT_VERSION if _use_v2 else "v1",
        "surfaced": surfaced,
        "note": "record-only; 不驱动 live, inject 另需 entry_enabled=true",
    }
    _append_jsonl(_SCAN_PATH, rec)

    print(f"[scan] {_today()} 模型={getattr(res,'model_used','?')} 浮现 {len(surfaced)} 标的:")
    for s in surfaced:
        tag = "🌍外" if s["is_universe_external"] else "  "
        print(f"  {s['direction']:5} {s['symbol']:6} conf={s['confidence']:.2f} {tag} "
              f"{s['winner_loser']:6} | {s['causal_chain'][:70]}")
    return 0


# ── inject: 代码出数值 → 写作战计划 (flag 闸门) ─────────────────
def _compute_numbers(entry_price: float, direction: str) -> Dict[str, Any]:
    """代码出数值 (Track A): entry_zone via entry_band, SL/TP 锚定 entry_zone 边界外.

    SL/TP 从 entry_zone 的 low/high 外扩 (而非 entry_price 中价), 保证:
      - LONG : SL 严格低于 zone low, TP 严格高于 zone high
      - SHORT: SL 严格高于 zone high, TP 严格低于 zone low
    避免 SL 落在入场区内 (KNIFE_FLOOR_PCT 与 default_stop_pct 同为 3% 时会重合).
    """
    from app.playbook.entry_band import compute_entry_band
    is_short = (direction == "SHORT")
    low, high = compute_entry_band(entry_price, is_short)
    if not (low and high):
        return {"entry_zone": [], "stop_loss": 0.0, "take_profit": 0.0}
    stop_pct = float(_cfg("universe_expansion.default_stop_pct", 3.0)) / 100.0
    tp_pct = float(_cfg("universe_expansion.default_tp_pct", 6.0)) / 100.0
    if is_short:
        stop_loss = high * (1 + stop_pct)   # SL 高于 zone high
        take_profit = low * (1 - tp_pct)    # TP 低于 zone low
    else:
        stop_loss = low * (1 - stop_pct)    # SL 低于 zone low
        take_profit = high * (1 + tp_pct)   # TP 高于 zone high
    return {
        "entry_zone": [low, high],
        "stop_loss": round(stop_loss, 4),
        "take_profit": round(take_profit, 4),
    }


def _load_battle_plan() -> Dict[str, Any]:
    try:
        return json.load(open(_BATTLE_PLAN_PATH, encoding="utf-8"))
    except Exception:
        return {}


def _existing_plan_symbols(plan: Dict[str, Any]) -> set:
    syms = set()
    for t in plan.get("targets", []) or []:
        s = str(t.get("symbol", "")).replace("US.", "").upper()
        if s:
            syms.add(s)
    for pa in plan.get("position_actions", []) or []:
        s = str(pa.get("symbol", "")).replace("US.", "").upper()
        if s:
            syms.add(s)
    return syms


def _position_symbols() -> set:
    """现有持仓标的 (从 daily_active_pool 的 POSITION 来源)."""
    try:
        from app.utils.daily_active_pool import DailyActivePool, SOURCE_POSITION
        p = DailyActivePool()
        p.load()
        return {e.code.replace("US.", "").upper()
                for e in p.get_entries() if e.source == SOURCE_POSITION}
    except Exception:
        return set()


def cmd_inject(force_dry: bool = False) -> int:
    if not _is_us_trading_day_today():
        print(f"[inject] {_today()} 非美股交易日 (周末/节假日), 跳过注入")
        return 0
    enabled = bool(_cfg("universe_expansion.entry_enabled", False))
    dry = force_dry or not enabled
    min_conf = float(_cfg("universe_expansion.min_confidence", 0.70))
    max_n = int(_cfg("universe_expansion.max_targets_per_day", 5))

    scans = [r for r in _read_jsonl(_SCAN_PATH) if r.get("type") == "scan"]
    today_scans = [r for r in scans if r.get("date") == _today()]
    if not today_scans:
        print(f"[inject] 今日({_today()})无 scan 结果, 先跑 scan")
        return 0
    surfaced = today_scans[-1].get("surfaced", [])

    today = _today()
    positions = _position_symbols()

    # 排重: 今日已在 playbook_instructions 的标的 (DA 优先, universe_expansion 让路).
    #   save_instruction 按 (date,symbol) UPSERT — 同标的会覆盖 DA, 故此处排除.
    # [ADR-019] 只对"实际占位"指令让路: 跳过 direction=SKIP 的被拒候选行.
    #   根因: get_instructions_by_date 不过滤 SKIP, 而 SKIP 是 DB 最大类 (163 vs BUY 112),
    #   若一律纳入排重 → DA/投票拒绝(SKIP)的标的会连带封杀 universe 浮现同一标的.
    existing_pb = set()
    try:
        from app.playbook.db import PlaybookDB
        for _inst in PlaybookDB().get_instructions_by_date(today):
            if str(_inst.get("direction", "")).upper() == "SKIP":
                continue
            _s = str(_inst.get("symbol", "")).replace("US.", "").upper()
            if _s:
                existing_pb.add(_s)
    except Exception as e:
        print(f"  [warn] 读取 playbook_instructions 排重失败 (继续): {e}")

    # 候选筛选: 可交易方向 + 置信度达标 + 非持仓 + 非DA已写
    cand = [s for s in surfaced
            if s["direction"] in ("LONG", "SHORT")
            and s["confidence"] >= min_conf
            and s["symbol"] not in positions
            and s["symbol"] not in existing_pb]
    cand.sort(key=lambda s: s["confidence"], reverse=True)
    cand = cand[:max_n]

    if not cand:
        print(f"[inject] 无合格候选 (min_conf={min_conf}, 已排除持仓/DA已写)")
        return 0

    router = _build_router()
    q = _batch_quotes(router, [s["symbol"] for s in cand])
    instructions = []
    for s in cand:
        px = _quote_price(q.get(s["symbol"], {})) or s.get("scan_price")
        if not px:
            print(f"  [skip] {s['symbol']} 无现价")
            continue
        nums = _compute_numbers(float(px), s["direction"])
        if not nums["entry_zone"]:
            print(f"  [skip] {s['symbol']} entry_zone 无效")
            continue
        # entry_zone 即 compute_entry_band 输出 (执行端同源, band 一致)
        entry_low, entry_high = nums["entry_zone"][0], nums["entry_zone"][1]
        # 方向映射到 playbook 词汇 (以 DA 实际写入值为准: pipeline.py L736)
        pb_direction = "BUY" if s["direction"] == "LONG" else "SELL_SHORT"
        instructions.append({
            "date": today,
            "symbol": f"US.{s['symbol']}",
            "direction": pb_direction,
            "entry_price": round(float(px), 4),  # 触发基准 (_check_intraday_trigger 用)
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": nums["stop_loss"],
            "take_profit": nums["take_profit"],
            "confidence": int(round(float(s["confidence"]) * 100)),  # schema INTEGER 0-100
            "thesis": s.get("thesis", ""),
            "sector": s.get("sector", ""),
            "horizon": "short",
            # 归因 (轻量, 复用现有字段, 无需 schema 迁移):
            "decision_id": f"{STRATEGY_ID}:{today}:{s['symbol']}",
            "raw_instruction": {
                "source": STRATEGY_ID,  # universe_expansion — 报表侧切分 DA vs 本源
                "prompt_version": PROMPT_VERSION,
                "origin": {
                    "info_source": INFO_SOURCE,
                    "causal_chain": s.get("causal_chain", ""),
                    "winner_loser": s.get("winner_loser", ""),
                    "corroborated_by_market": s.get("corroborated_by_market"),
                },
            },
        })

    print(f"[inject] entry_enabled={enabled} → {'DRY-RUN (不写库)' if dry else '写入 playbook_instructions'}")
    for it in instructions:
        print(f"  {it['direction']:10} {it['symbol']:8} entry={it['entry_price']} "
              f"zone=[{it['entry_low']},{it['entry_high']}] SL={it['stop_loss']} TP={it['take_profit']} "
              f"conf={it['confidence']} src={STRATEGY_ID}")

    if dry:
        if not enabled:
            print("  [安全阀] universe_expansion.entry_enabled=false → 未写库, 未下单。")
        return 0

    if not instructions:
        return 0

    # 写入 playbook_instructions — 与 DA 共用同一执行/触发/退出轨道.
    #   auto_execute 会对未触发指令做 _check_intraday_trigger (symbol-agnostic 取价),
    #   宇宙外标的无需订阅/轮询注册即可被执行.
    from app.playbook.db import PlaybookDB
    db = PlaybookDB()
    db.ensure_tables()
    saved = 0
    for it in instructions:
        try:
            db.save_instruction(it)
            saved += 1
        except Exception as e:
            print(f"  [warn] {it['symbol']} 写库失败: {e}")

    print(f"[inject] 已写入 {saved} 条 playbook_instructions (source={STRATEGY_ID}, 走 DA 统一执行轨道)")
    return 0


# ── score / report ───────────────────────────────────────────
def cmd_score() -> int:
    records = _read_jsonl(_SCAN_PATH)
    scans = [r for r in records if r.get("type") == "scan"]
    scored = {r.get("scan_date") for r in records if r.get("type") == "score"}
    todo = [c for c in scans if c.get("date") not in scored and c.get("surfaced")]
    if not todo:
        print("[score] 无待评分 scan")
        return 0
    router = _build_router()
    for c in todo:
        tradeable = [s for s in c["surfaced"] if s["direction"] in ("LONG", "SHORT") and s.get("scan_price")]
        q = _batch_quotes(router, [s["symbol"] for s in tradeable])
        results, fwd_list, hits = [], [], 0
        for s in tradeable:
            cur = _quote_price(q.get(s["symbol"], {}))
            base = s.get("scan_price")
            if not cur or not base or base <= 0:
                continue
            raw = (cur - base) / base * 100
            fwd = round(raw if s["direction"] == "LONG" else -raw, 2)
            hit = fwd >= HIT_PCT
            hits += 1 if hit else 0
            fwd_list.append(fwd)
            results.append({"symbol": s["symbol"], "direction": s["direction"],
                            "scan_price": base, "scored_price": round(cur, 4),
                            "fwd_pct": fwd, "hit": hit, "confidence": s["confidence"],
                            "strategy_id": STRATEGY_ID})
        n = len(results)
        summary = {"n": n, "hit_rate": round(hits / n, 3) if n else None,
                   "avg_fwd_pct": round(sum(fwd_list) / len(fwd_list), 2) if fwd_list else None}
        _append_jsonl(_SCAN_PATH, {"type": "score", "scan_date": c.get("date"),
                                   "scored_at": _now_et().isoformat(),
                                   "results": results, "summary": summary})
        print(f"[score] scan={c.get('date')} 评分{n} 命中率={summary['hit_rate']} "
              f"平均前瞻={summary['avg_fwd_pct']}%")
    return 0


def cmd_report(days: int = 30) -> int:
    records = _read_jsonl(_SCAN_PATH)
    if not records:
        print(f"影子文件为空: {_SCAN_PATH}")
        return 0
    cutoff = (_now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    scans = [r for r in records if r.get("type") == "scan" and r.get("date", "") >= cutoff]
    scores = [r for r in records if r.get("type") == "score" and r.get("scan_date", "") >= cutoff]
    total = sum(len(c.get("surfaced", [])) for c in scans)
    uniq = {s["symbol"] for c in scans for s in c.get("surfaced", [])}
    fwd = [x["fwd_pct"] for r in scores for x in r.get("results", [])]
    hit = [x["hit"] for r in scores for x in r.get("results", [])]

    print("=" * 64)
    print(f"📡 LLM 事件推理·选股宇宙扩张 效果 (近 {days} 天)   {_SCAN_PATH.name}")
    print(f"归因: source_type={SOURCE_TYPE} | strategy_id={STRATEGY_ID} | discovery_source={DISCOVERY_SOURCE}")
    print("=" * 64)
    print(f"scan 次数       : {len(scans)}")
    print(f"浮现标的         : {total} 人次 / {len(uniq)} 只去重")
    if hit:
        print(f"命中率(前瞻≥{HIT_PCT}%): {round(sum(hit)/len(hit),3)} ({sum(hit)}/{len(hit)})")
        print(f"平均前瞻收益     : {round(sum(fwd)/len(fwd),2)}%")
    else:
        print("已评分           : 0 (待 score)")
    print("-" * 64)
    sc_by_date = {s["scan_date"]: s for s in scores}
    for c in sorted(scans, key=lambda x: x.get("date", "")):
        d = c.get("date")
        surf = c.get("surfaced", [])
        line = f"{d}  模型={c.get('model_used','?')}  浮现{len(surf)}只"
        sc = sc_by_date.get(d)
        if sc and sc.get("summary", {}).get("n"):
            sm = sc["summary"]
            line += f"  → 命中率{sm['hit_rate']} 平均{sm['avg_fwd_pct']:+}%"
        print(line)
        for s in surf[:8]:
            tag = ""
            if sc:
                m = next((x for x in sc.get("results", []) if x["symbol"] == s["symbol"]), None)
                if m:
                    tag = f"  → 前瞻{m['fwd_pct']:+.2f}% {'✅' if m['hit'] else '❌'}"
            ext = "🌍" if s.get("is_universe_external") else " "
            print(f"    {s['direction']:5} {s['symbol']:6} {ext} conf={s['confidence']:.2f}"
                  f"{tag}  | {s['causal_chain'][:50]}")
    print("=" * 64)
    return 0


# ── scan-eval: V1/V2 prompt A/B 对比 (shadow, 不落主scan/不下单) ──
_PROMPT_EVAL_PATH = _ROOT / "data" / "shadow" / "prompt_eval.jsonl"


def cmd_scan_eval(manual_clue: str = "") -> int:
    """同一 clue brief 分别喂 V1(现状)/V2(P0 意图对齐+引证自评), 并排对比。
    纯 shadow: 不落主 scan、不 inject、不下单。唯一变量=SYSTEM_PROMPT。"""
    brief = _build_clue_brief(manual_clue)
    print("=" * 72)
    print(f"scan-eval clue brief ({'有人工线索' if manual_clue else '无clue,自动新闻'}):")
    print(brief[:1200])
    print("=" * 72)

    try:
        from app.llm.model_call import call_llm
    except Exception as e:
        print(f"[scan-eval] 无法导入 call_llm: {e}")
        return 1

    def _run(system: str, tag: str, with_tier: bool):
        res = call_llm("universe_expansion_reasoning", brief, system=system,
                       temperature=0.2, max_tokens=2500,
                       schema_required={"surfaced": list}, json_mode=True)
        if not res or not res.success:
            print(f"[{tag}] LLM 调用失败: {getattr(res, 'error', '?') if res else 'no_result'}")
            return [], None
        parsed = res.response if isinstance(res.response, dict) else {}
        return _normalize_surfaced(parsed.get("surfaced"), with_tier=with_tier), getattr(res, "model_used", None)

    print("[V1] 现状 prompt 推理中 ...")
    v1, m1 = _run(SYSTEM_PROMPT, "V1", False)
    print("[V2] P0 意图对齐 prompt 推理中 ...")
    v2, m2 = _run(SYSTEM_PROMPT_V2, "V2", True)

    # 抓 scan 参考价 (供远端 T+N 前瞻评分, Layer 0 补缺)
    try:
        router = _build_router()
        _all = list({s["symbol"] for s in (v1 + v2) if s["direction"] in ("LONG", "SHORT")})
        _q = _batch_quotes(router, _all)
        for s in (v1 + v2):
            _px = _quote_price(_q.get(s["symbol"], {}))
            s["scan_price"] = round(_px, 4) if _px else None
    except Exception as _pe:
        print(f"  [warn] 抓参考价失败 (不影响近端评估): {_pe}")

    v1_by = {s["symbol"]: s for s in v1}
    v2_by = {s["symbol"]: s for s in v2}
    syms = sorted(set(v1_by) | set(v2_by))

    print("\n" + "=" * 72)
    print(f"V1/V2 并排对比  (V1浮现{len(v1)} vs V2浮现{len(v2)})")
    print("-" * 72)
    for sym in syms:
        a = v1_by.get(sym)
        b = v2_by.get(sym)
        a_str = f"{a['direction']:5} conf={a['confidence']:.2f}" if a else "  —(未浮现)"
        if b:
            ev = b.get("evidence_ref", "")
            b_str = (f"{b['direction']:5} conf={b['confidence']:.2f} {b.get('self_tier','?'):<18} "
                     f"ev={('有:'+ev[:24]) if ev else '空->自降'}")
        else:
            b_str = "  —(未浮现/弃选)"
        print(f"  {sym:6}  V1[{a_str}]  ||  V2[{b_str}]")
    print("-" * 72)

    _append_jsonl(_PROMPT_EVAL_PATH, {
        "type": "prompt_eval", "date": _today(), "evaluated_at": _now_et().isoformat(),
        "prompt_version": PROMPT_VERSION,
        "clue": manual_clue, "v1": {"model": m1, "surfaced": v1},
        "v2": {"model": m2, "surfaced": v2},
    })
    print(f"[scan-eval] 已落对比文件: {_PROMPT_EVAL_PATH.name} (record-only, 未下单)")
    return 0


# ── eval-report: 近端评估 (浮现质量, 当天可得) ─────────
def cmd_eval_report(days: int = 14) -> int:
    """Layer 1 近端评估: 聚合 prompt_eval.jsonl 的浮现质量指标
    (克制比/引证率/tier分布/夹逼率/稳定性), 不依赖市场。"""
    import statistics
    from collections import defaultdict

    recs = [r for r in _read_jsonl(_PROMPT_EVAL_PATH) if r.get("type") == "prompt_eval"]
    cutoff = (_now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    recs = [r for r in recs if r.get("date", "") >= cutoff]
    if not recs:
        print(f"[eval-report] 近{days}天无 prompt_eval 数据 (先跑 scan-eval)")
        return 0

    def _trade(surf):
        return [s for s in surf if s.get("direction") in ("LONG", "SHORT")]

    restraint = []
    ev_total, ev_cited = 0, 0
    tier_cnt = {"HARD_EVENT": 0, "MARKET_CORROBORATED": 0, "THESIS_ONLY": 0}
    clamp_total, clamp_hit = 0, 0
    groups = defaultdict(list)

    for r in recs:
        v1 = _trade(r.get("v1", {}).get("surfaced", []))
        v2 = _trade(r.get("v2", {}).get("surfaced", []))
        if len(v1) > 0:
            restraint.append(len(v2) / len(v1))
        for s in v2:
            ev_total += 1
            if s.get("evidence_ref"):
                ev_cited += 1
            tier_cnt[s.get("self_tier", "THESIS_ONLY")] = tier_cnt.get(s.get("self_tier", "THESIS_ONLY"), 0) + 1
            if "clamped" in s:
                clamp_total += 1
                clamp_hit += 1 if s.get("clamped") else 0
        key = (r.get("date"), r.get("clue", ""))
        groups[key].append((frozenset(s["symbol"] for s in v1),
                            frozenset(s["symbol"] for s in v2)))

    def _avg_jaccard(sets):
        pairs = []
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                a, b = sets[i], sets[j]
                u = len(a | b)
                pairs.append((len(a & b) / u) if u else 1.0)
        return statistics.mean(pairs) if pairs else None

    v1_stab, v2_stab = [], []
    for _key, runs in groups.items():
        if len(runs) >= 2:
            a = _avg_jaccard([x[0] for x in runs])
            b = _avg_jaccard([x[1] for x in runs])
            if a is not None:
                v1_stab.append(a)
            if b is not None:
                v2_stab.append(b)

    print("=" * 66)
    print(f"📊 scan-eval 近端评估 (近{days}天, {len(recs)}次A/B)   prompt_eval.jsonl")
    print("=" * 66)
    if restraint:
        print(f"克制比 V2/V1       : {statistics.mean(restraint):.2f}  (均值; <1=V2更克制)")
    if ev_total:
        print(f"V2 引证率          : {ev_cited/ev_total:.0%}  ({ev_cited}/{ev_total})")
    print(f"V2 tier 分布       : HARD {tier_cnt['HARD_EVENT']} / MARKET {tier_cnt['MARKET_CORROBORATED']} / THESIS {tier_cnt['THESIS_ONLY']}")
    if clamp_total:
        print(f"夹逼率(无据却高置信) : {clamp_hit/clamp_total:.0%}  ({clamp_hit}/{clamp_total}; 越低=LLM越守自律)")
    if v2_stab:
        print(f"稳定性(Jaccard)    : V2={statistics.mean(v2_stab):.2f}  vs  V1={statistics.mean(v1_stab):.2f}  (越高越稳; {len(v2_stab)}组多跑)")
    else:
        print("稳定性            : (需同日同场景多次跑才有; 每周3连跑后可见)")
    print("=" * 66)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 事件推理驱动的选股宇宙扩张")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_scan = sub.add_parser("scan", help="LLM 推理浮现标的 (落影子, 不下单)")
    p_scan.add_argument("--clue", default="", help="人工事件/线索文本 (dry-run 验证推理)")
    p_inj = sub.add_parser("inject", help="代码出数值→写作战计划 (仅 entry_enabled=true)")
    p_inj.add_argument("--dry-run", action="store_true", help="强制只打印不写盘")
    sub.add_parser("score", help="对已 scan 未评分标的算前瞻收益")
    p_rep = sub.add_parser("report", help="查看近 N 天效果")
    p_rep.add_argument("--days", type=int, default=30)
    p_eval = sub.add_parser("scan-eval", help="V1/V2 prompt A/B 对比 (shadow, 不落主scan/不下单)")
    p_eval.add_argument("--clue", default="", help="人工线索 (复现强事件场景做对比)")
    p_evr = sub.add_parser("eval-report", help="近端评估: 聚合浮现质量指标 (克制比/引证率/稳定性)")
    p_evr.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    try:
        if args.cmd == "scan":
            return cmd_scan(manual_clue=args.clue)
        if args.cmd == "inject":
            return cmd_inject(force_dry=args.dry_run)
        if args.cmd == "score":
            return cmd_score()
        if args.cmd == "report":
            return cmd_report(days=args.days)
        if args.cmd == "scan-eval":
            return cmd_scan_eval(manual_clue=args.clue)
        if args.cmd == "eval-report":
            return cmd_eval_report(days=args.days)
    except Exception as e:
        import traceback
        print(f"⚠️ 执行异常 ({args.cmd}): {e}")
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
