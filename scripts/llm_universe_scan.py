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
PROMPT_VERSION = "v2.2"  # scan 落库归因戳; v2.2=可证伪字段+证据清单+负反馈注入 (2026-07-25 用户批, DEB-20260725-da-ue-pe-agent-optimization P1/P2/P3/P4)

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
# v2.1 (2026-07-23 用户批准, DEB-20260723-da-ue-prompt-optimization): 开放 THESIS_ONLY 通道 —
#   V2 "绝不为凑数做板块联想"致 THESIS_ONLY n=0 (不可证伪) + 模拟期样本不足 (日仅 2-3 浮现).
#   改双通道: 主通道纪律不变, 板块联想/逻辑推演可经 THESIS_ONLY 通道浮现 (标签+夹逼照常),
#   质量分层交给 score tier 切片用数据判定, 不在浮现端压制. 加法式变更, 回滚=git revert.
SYSTEM_PROMPT_V2 = (
    "你是资深事件驱动型美股分析师。职责: 浮现受事件/市场/逻辑驱动的美股标的, 双通道运行: "
    "主通道=存在【明确、可核验的因果传导路径】的标的(HARD_EVENT/MARKET_CORROBORATED); "
    "THESIS_ONLY 通道=有交易价值的板块联想/逻辑推演类标的(模拟验证期开放, 供下游分层判分)。\n"
    "要求:\n"
    "1. evidence_ref(主通道强制): 主通道标的必须引用给定材料中的【具体证据原文】—— 某条新闻标题 / "
    "某个市场异动数据 / 某条人工线索。若无法从给定材料引出具体证据(仅凭行业常识或板块联想), evidence_ref 留空。\n"
    "2. self_tier(强制自评): HARD_EVENT=因果链第一环锚定 evidence_ref 里的具体事件(封锁/战争/财报/监管/事故); "
    "MARKET_CORROBORATED=无具体事件但 evidence_ref 是真实市场异动/板块轮动数据; "
    "THESIS_ONLY=无 evidence_ref, 仅逻辑或板块联想。\n"
    "3. 自律约束: evidence_ref 为空者, self_tier 必须=THESIS_ONLY 且 confidence≤0.60; "
    "THESIS_ONLY 标的的 thesis 必须以'板块联想:'或'逻辑推演:'开头, 明示推演性质。\n"
    "4. 充分浮现: 凡具备【可核验因果路径 + 真实可执行机会】的标的都要浮现(多空对等), 不设人为数量上限, "
    "也不为控量而漏报——质量由 self_tier/evidence_ref 标签与模拟交易结果在下游度量, 而非在浮现端压制。"
    "证据强弱如实反映在 confidence 与 self_tier(HARD_EVENT>MARKET_CORROBORATED>THESIS_ONLY)。\n"
    "5. 不输出任何价格数字(entry/止损/止盈由下游计算); 只输出可真实交易的美股 symbol(不带前缀); 不确定填 direction=SKIP。\n"
    # v2.2 (2026-07-25, P1 可证伪字段 + P2 证据清单): 预测与叙事分离
    "6. 可证伪性(强制): 每个非 SKIP 标的额外输出 — invalidation(一句话失效条件, ≤40字), "
    "horizon_hours(论点预期兑现时效, 整数小时, 日内动量 4-8), expected_move_pct(预期方向幅度%, 小数)。\n"
    "7. 证据清单(强制, 如实勾选不得美化): evidence_checklist 四项布尔 — "
    "catalyst_specific(存在具体催化事件非泛情绪), catalyst_within_24h(催化发生在24h内), "
    "price_volume_confirmed(给定材料中有量价异动印证), crowding_low(非已被充分炒作2日以上的拥挤交易)。\n"
    # v2.2 (2026-07-25, P5 SHORT 高门槛): LLM 多头叙事先验强, 提高 SHORT 输出成本=天然质量门
    #   (实证: 本源 SHORT 命中 25%, DA 分歧SHORT 0%)。不禁 SHORT, 只要求想清三件事。
    "8. SHORT 高门槛(强制): direction=SHORT 的标的必须额外输出 short_check — "
    "borrow_feasible(bool, 大盘股/流动性好可借券), downside_catalyst_expiry_hours(int, 下跌催化剂时效), "
    "squeeze_risk(1-5, 逼空风险自评)。无具体下跌催化剂(仅'估值高'/'涨多了')的 SHORT 不得浮现。\n"
    "每个标的字段: symbol, direction(LONG/SHORT/SKIP), causal_chain(事件→传导→标的, 一句话), "
    "evidence_ref(引用的具体证据原文, 无则空), self_tier(HARD_EVENT/MARKET_CORROBORATED/THESIS_ONLY), "
    "thesis(≤60字), confidence(0~1 小数), is_universe_external(bool), winner_loser(WINNER/LOSER), "
    "corroborated_by_market(bool), invalidation(str), horizon_hours(int), expected_move_pct(float), "
    "evidence_checklist({catalyst_specific,catalyst_within_24h,price_volume_confirmed,crowding_low} 均bool), "
    "short_check(仅SHORT: {borrow_feasible,downside_catalyst_expiry_hours,squeeze_risk})。\n"
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
    """近 N 小时新闻 (RSSHub, llm_pipeline 同源); 失败返回 [].

    C1 (2026-07-27, 用户裁"提升标的/指令质量优先"): 并入 finnhub 市场新闻 (已付费闲置
    能力, 60/min 令牌桶+2h 缓存在位) — 同构 dict (source='finnhub_market') 进同一条链:
    brief 新闻段 + S0 渠道归因 + S1 切片全自动覆盖, 判分"持证上岗"。
    门控: universe_expansion.clue_finnhub_news (默认开); 回滚: 设 false。
    """
    items: List[Dict[str, Any]] = []
    try:
        from app.data.rsshub_us_news import RSSHubUSNewsFetcher
        items = RSSHubUSNewsFetcher().get_recent_news(hours=hours, limit=30) or []
    except Exception as e:
        print(f"  [warn] 新闻拉取失败 (降级): {e}")
    if bool(_cfg("universe_expansion.clue_finnhub_news", True)):
        try:
            from datetime import timezone as _tz
            from email.utils import format_datetime as _fmt_dt
            from app.data.finnhub_provider import FinnhubProvider
            from app.data.rsshub_us_news import RSSHubUSNewsFetcher as _RF
            _matcher = _RF.__new__(_RF)  # 只用 P4a 匹配器, 不碰 db
            fh = FinnhubProvider()
            cutoff_ts = datetime.now(_tz.utc).timestamp() - hours * 3600
            for a in (fh.get_market_news(limit=20) or []):
                ts = float(a.get("datetime") or 0)
                if ts and ts < cutoff_ts:
                    continue
                title = str(a.get("headline") or "").strip()
                if not title:
                    continue
                syms = set(f"US.{s}" for s in (a.get("relatedSymbols") or []) if s)
                syms |= _matcher._match_symbols_v2(f"{title} {a.get('summary') or ''}")
                items.append({
                    "id": f"fh_{int(ts)}_{abs(hash(title)) % 10**8}",
                    "title": title,
                    "summary": str(a.get("summary") or "")[:300],
                    "source": "finnhub_market",
                    "source_name": f"Finnhub({a.get('source') or 'market'})",
                    "symbols": json.dumps(sorted(syms)),
                    "published_at": _fmt_dt(datetime.fromtimestamp(ts, _tz.utc)) if ts else "",
                })
        except Exception as e:
            print(f"  [warn] finnhub 新闻并入失败 (降级): {e}")
    # 合并后按发布时间降序 (brief 展示只取前 20, 不排序则 finnhub 条目永远被截断)
    def _pub_ts(n):
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(str(n.get("published_at") or "")).timestamp()
        except Exception:
            return 0.0
    items.sort(key=_pub_ts, reverse=True)
    return items


def _gather_market_corroboration() -> Dict[str, Any]:
    """市场佐证: 复用机械影子最近一次 capture 的板块轮动/异动 (若有)."""
    recs = _read_jsonl(_MECH_CAPTURE_PATH)
    caps = [r for r in recs if r.get("type") == "capture"]
    if not caps:
        return {}
    last = caps[-1]
    # 防腐守卫 (2026-07-22): capture writer 曾停更, LLM 每日啃陈旧轮动数据浮现同批标的.
    #   若最新 capture 早于 4 个自然日, 视为失效 → 返回 {}, 让 LLM 落回实时新闻+硬事件推理.
    _cap_date = str(last.get("date", ""))
    try:
        _age_days = (datetime.strptime(_today(), "%Y-%m-%d")
                     - datetime.strptime(_cap_date, "%Y-%m-%d")).days
    except Exception:
        _age_days = 999
    if _age_days > 4:
        print(f"  [warn] 市场佐证 capture 已陈旧 (date={_cap_date or '?'}, {_age_days}d) → 跳过, "
              f"LLM 落回新闻+硬事件推理")
        return {}
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


def _gather_event_symbols_file() -> List[str]:
    """[P2' 2026-07-21] 读 gen_event_symbols.py 的当日产出 (data/event_symbols_{ET}.json).

    给 V2 提供"预消化的事件驱动标的+原因"作为佐证原料 — MARKET_CORROBORATED 需要
    可引用证据, 事件清单正是证据源; 不动 prompt 克制标准. 文件不存在/为空 → []。
    同一天 DA --symbols 与本段共用同一文件, 双源原料同源。"""
    try:
        path = _ROOT / "data" / f"event_symbols_{_today()}.json"
        if not path.exists():
            return []
        d = json.loads(path.read_text())
        out: List[str] = []
        for e in (d.get("event_symbols") or [])[:10]:
            t = str(e.get("symbol", "")).replace("US.", "")
            reasons = "; ".join(str(r)[:60] for r in (e.get("reasons") or [])[:2])
            out.append(f"- {t} (score={e.get('score', 0)}): {reasons}")
        return out
    except Exception as e:
        print(f"  [warn] 事件标的文件读取失败 (降级): {e}")
        return []


def _gather_peer_surge() -> List[str]:
    """[P3 2026-07-22] 盘前峰群异动段 — 读 premarket_peer_shadow.jsonl 当日记录 (record-only 影子),
    把板块级盘前异动作为 clue 供 V2 引用 (evidence_ref 可用 peer_surge).
    定位: 输入端扩容, 非放行信号 — "峰群直接跟随"已被 7 天回测否决 (-8.48%, 唯一 CONFIRM 日 -1.32%),
    此段仅把高波动板块票送入分析池, gate 照常把关. 文件缺失/当日无记录 → 跳过 (降级无碍).
    回滚: 配置 universe_expansion.clue_peer_surge: false."""
    try:
        path = Path("data/shadow/premarket_peer_shadow.jsonl")
        if not path.exists():
            return []
        today = _today()
        rec: Optional[dict] = None
        for line in path.read_text().strip().splitlines():
            try:
                d = json.loads(line)
                if d.get("date") == today and d.get("peers_snapshot"):
                    rec = d  # 当日最后一条 (refresh-join 复写保留快照)
            except Exception:
                continue
        if not rec:
            return []
        snap = rec.get("peers_snapshot") or {}

        def _fmt(members: List[str]) -> str:
            cells = []
            for sym in members:
                v = snap.get(sym)
                if isinstance(v, dict) and v.get("pre_change") is not None:
                    cells.append(f"{sym}{float(v['pre_change']):+.1f}%(量比{float(v.get('volume_ratio', 0)):.2f})")
            return " ".join(cells)

        # 2026-07-22: 多板块组 (groups + groups_members 自描述成分); 旧记录无 groups → 回落顶层存储组 (向后兼容)
        groups = rec.get("groups") or {}
        if groups:
            members_map = rec.get("groups_members") or {}
            out = []
            for name, gd in groups.items():
                line = _fmt([str(s).upper() for s in (members_map.get(name) or [])])
                if line:
                    out.append(f"{name}峰群 verdict={(gd or {}).get('verdict', '?')}: {line}")
            return out
        cells: List[str] = []
        for sym, v in snap.items():
            if isinstance(v, dict) and v.get("pre_change") is not None:
                cells.append(f"{sym}{float(v['pre_change']):+.1f}%(量比{float(v.get('volume_ratio', 0)):.2f})")
        if not cells:
            return []
        return [f"存储峰群 verdict={rec.get('group_verdict', '?')}: " + " ".join(cells)]
    except Exception as e:
        print(f"  [warn] 峰群影子读取失败 (降级): {e}")
        return []


def _latest_insider_report_age() -> Optional[int]:
    """最近一份 insider_daily 报告距今天数 (0=今日); 无任何报告返回 None."""
    try:
        files = sorted((_ROOT / "data" / "reports").glob("insider_daily_*.json"))
        if not files:
            return None
        d = files[-1].stem.replace("insider_daily_", "")
        return (datetime.strptime(_today(), "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days
    except Exception:
        return None


def _build_feedback_table(days: int = 7, max_rows: int = 10) -> List[str]:
    """v2.2 (P3): 本源近 N 日浮现→fwd 结果小表 (供 prompt 负反馈注入)。

    口径: playbook_instructions 中 source=universe_expansion 的方向指令,
    fwd = fwd_return_intraday 优先 (t1 兜底), 按方向调整 (SHORT 取负)。失败返回 []。
    """
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{_ROOT / 'data' / 'ftseek_us.db'}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date, symbol, direction, fwd_return_intraday, fwd_return_t1, raw_instruction_json "
            "FROM playbook_instructions WHERE date >= date(?, ?) "
            "AND direction IN ('BUY','SELL_SHORT') AND raw_instruction_json LIKE '%universe_expansion%' "
            "ORDER BY date DESC, symbol", (_today(), f"-{days} day")).fetchall()
        conn.close()
        out = []
        for r in rows[:max_rows]:
            f = r["fwd_return_intraday"] if r["fwd_return_intraday"] is not None else r["fwd_return_t1"]
            if f is None:
                res = "待回填"
            else:
                adj = float(f) * (1 if r["direction"] == "BUY" else -1) * 100
                res = f"{adj:+.1f}%" + (" ✓" if adj > 0 else " ✗")
            _dir = "LONG" if r["direction"] == "BUY" else "SHORT"
            out.append(f"- {r['date']} {r['symbol'].replace('US.', '')} {_dir}: {res}")
        return out
    except Exception as e:
        print(f"  [warn] 负反馈表构建失败 (降级无注入): {e}")
        return []


def _build_clue_brief(manual_clue: str = "") -> str:
    hours = int(_cfg("universe_expansion.news_lookback_hours", 24))
    parts = [f"日期: {_today()} (ET)"]
    health: List[str] = []  # [新鲜度 2026-07-22] 各输入源健康摘要 (日志可观测, 区分"无事件"与"链路断")

    if manual_clue:
        parts.append(f"\n[人工线索]\n{manual_clue.strip()}")

    news = _gather_news(hours)

    # [ADR-017/018] 结构化硬事件段 (config 驱动, 传导优先级最高, 供 V2 自评 HARD_EVENT)
    hard: List[str] = []
    if bool(_cfg("universe_expansion.clue_hard_events", False)):
        _he = _gather_hard_events(news)            # 新闻分类 (关键词, 零成本)
        hard += _he
        health.append(f"硬事件:{len(_he)}")
    if bool(_cfg("universe_expansion.clue_earnings_events", False)):
        _ee = _gather_earnings_events()            # 财报日历 (Alpha Vantage)
        hard += _ee
        health.append(f"财报:{len(_ee)}")
    if bool(_cfg("universe_expansion.clue_insider_clusters", False)):
        _ic = _gather_insider_clusters()           # 内部人集群 (已持久化文件)
        hard += _ic
        _age = _latest_insider_report_age()
        health.append(f"内部人:{len(_ic)}" + ("(报告缺失)" if _age is None
                      else (f"(断更{_age}d)" if _age > 0 else "")))
    # S3 (2026-07-27, DEB-20260727): EDGAR 8-K 直连 — 分钟级官方一手源, CIK→ticker 精确,
    #   item code 带方向先验标注 (2.02业绩/4.02重述强利空等)。config 门控, 失败降级空。
    if bool(_cfg("universe_expansion.clue_8k_events", True)):
        try:
            from app.data.sec_8k_provider import fetch_recent_8k
            _8k = fetch_recent_8k(lookback_hours=int(_cfg(
                "universe_expansion.news_lookback_hours", 24)))
            _8k_lines = [f"8-K {r['ticker']}: {'; '.join(r['item_labels'])} "
                         f"(filed {r['updated'][:16]})" for r in _8k[:15]]
            hard += _8k_lines
            health.append(f"8-K:{len(_8k_lines)}")
        except Exception as _8k_e:
            print(f"  [warn] 8-K 直连失败 (降级): {_8k_e}")
            health.append("8-K:err")
    # C2 (2026-07-27, 用户裁"提升标的/指令质量优先"): SC 13D/G 举牌 — 常先于新闻的
    #   强 HARD_EVENT (13D=主动举牌看多先验, 13G=被动持仓弱信号)。门控+降级同 8-K。
    if bool(_cfg("universe_expansion.clue_13dg_events", True)):
        try:
            from app.data.sec_8k_provider import fetch_recent_13dg
            _dg = fetch_recent_13dg(lookback_hours=96)
            _dg_lines = [f"举牌 {r['ticker']}: {r['form']}"
                         f"{' (主动举牌, 看多先验强)' if r['form'].startswith('SC 13D') else ' (被动持仓)'} "
                         f"(filed {r['updated'][:16]})" for r in _dg[:10]]
            hard += _dg_lines
            health.append(f"13DG:{len(_dg_lines)}")
        except Exception as _dg_e:
            print(f"  [warn] 13D/G 直连失败 (降级): {_dg_e}")
            health.append("13DG:err")
    if hard:
        parts.append("\n[硬事件 (已分类, 传导优先级最高)]")
        parts.extend(hard)

    # [P2' 2026-07-21] 事件驱动标的清单 (gen_event_symbols 当日产出, 与 DA --symbols 同源).
    #   给 V2 可引用的佐证原料; 文件缺失=当日未生成 → 跳过, 行为与之前一致. 回滚: 配置设 false.
    if bool(_cfg("universe_expansion.clue_event_symbols", True)):
        es = _gather_event_symbols_file()
        health.append(f"事件标的:{len(es)}")
        if es:
            parts.append("\n[今日事件驱动标的 (事件清单, 可作 evidence_ref 引用)]")
            parts.extend(es)

    # [P3 2026-07-22] 盘前板块峰群 (影子测量转 clue 输入; 仅热度参考, 跟随策略已回测否决).
    if bool(_cfg("universe_expansion.clue_peer_surge", False)):
        ps = _gather_peer_surge()
        health.append(f"峰群:{len(ps)}组")
        if ps:
            parts.append("\n[盘前板块峰群 (影子实测, 热度参考非跟随信号)]")
            parts.extend(ps)

    mkt = _gather_market_corroboration()
    health.append(f"佐证:{mkt.get('date') or '无/陈旧'}")
    if mkt:
        parts.append("\n[市场佐证 (板块轮动/异动)]")
        if mkt.get("strong_sectors"):
            parts.append("轮入板块: " + ", ".join(mkt["strong_sectors"]))
        if mkt.get("weak_sectors"):
            parts.append("轮出板块: " + ", ".join(mkt["weak_sectors"]))
        if mkt.get("movers"):
            parts.append("宇宙外异动: " + ", ".join(mkt["movers"][:12]))

    if news:
        # [2026-07-22] symbols 字段为 JSON 字符串, 空列表 "[]" 真值恒真曾拼出 "[[]]" 噪声 → 解析后非空才展示
        _n_sym = 0
        parts.append(f"\n[近{hours}h 新闻头条 (共{len(news)}条, 取前20; 时效越新传导价值越高)]")
        for n in news[:20]:
            title = (n.get("title") or "").strip()
            syms = n.get("symbols") or ""
            if isinstance(syms, str):
                try:
                    syms = json.loads(syms)
                except Exception:
                    syms = [syms] if syms else []
            syms = [str(s).replace("US.", "") for s in syms if s]
            if syms:
                _n_sym += 1
            # v2.2 (2026-07-25, P4 信息饮食): 每条带时效小时数 — LLM 无时钟,
            #   不标时效则 23h 前旧闻与 1h 前突发同权重 (重复推荐的助燃剂之一)
            _age_tag = ""
            try:
                _pub = str(n.get("published_at") or "")
                if _pub:
                    # RSSHub 返 RFC2822 ('Fri, 24 Jul 2026 16:21:16 -0400'); ISO 兜底
                    try:
                        from email.utils import parsedate_to_datetime
                        _pub_dt = parsedate_to_datetime(_pub)
                    except Exception:
                        _pub_dt = datetime.fromisoformat(_pub.replace("Z", "+00:00"))
                    _age_h = (datetime.now(_pub_dt.tzinfo) - _pub_dt).total_seconds() / 3600
                    if 0 <= _age_h < 200:
                        _age_tag = f" ({_age_h:.0f}h前)"
            except Exception:
                pass
            if title:
                parts.append(f"- {title}{_age_tag}" + (f"  [{', '.join(syms)}]" if syms else ""))
        health.append(f"新闻:{len(news)}条(带标的{_n_sym})")
    else:
        parts.append("\n[新闻] 无近期新闻 (依赖市场佐证 + 人工线索推理)")
        health.append("新闻:0⚠️")

    # v2.2 (2026-07-25, P3 负反馈注入): 本源近7日浮现→结果小表 — 模型每天看同样新闻
    #   自然得同样结论, 它不知道昨天推错了 (ORCL SHORT×5 根因)。比代码硬冷却聪明:
    #   保留"事件持续发酵"的合理重复, 砍"无新信息惯性重复"。回滚: 配置 clue_feedback=false。
    if bool(_cfg("universe_expansion.clue_feedback", True)):
        fb = _build_feedback_table()
        health.append(f"负反馈:{len(fb)}条")
        if fb:
            parts.append("\n[本源近7日浮现回顾 (方向调整后前瞻收益, 负=推错)]")
            parts.extend(fb)
            parts.append("规则: 对上表中连续推错(连续负收益)的 同标的+同方向, 除非今日材料中出现"
                         "【新增量证据】(新事件/新数据), 否则不得再次浮现; 若确有新证据, evidence_ref 必须引用新证据。")

    print(f"  [输入源健康] " + " | ".join(health))
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
            # v2.2 (2026-07-25, P1 可证伪字段): 纯落库供判分, 不进任何执行逻辑
            item["invalidation"] = str(it.get("invalidation", ""))[:120]
            try:
                item["horizon_hours"] = int(it.get("horizon_hours", 0) or 0)
            except (TypeError, ValueError):
                item["horizon_hours"] = 0
            try:
                item["expected_move_pct"] = round(float(it.get("expected_move_pct", 0) or 0), 2)
            except (TypeError, ValueError):
                item["expected_move_pct"] = 0.0
            # v2.2 (P2 rubric化): 校准从模型移到代码 — 模型只枚举证据, 分数代码算。
            #   rubric_score = 四项 checklist 命中数 (0-4), 与 conf 并列落库供相关性对比 (预注册 Q2)。
            _cl = it.get("evidence_checklist") or {}
            if not isinstance(_cl, dict):
                _cl = {}
            _keys = ("catalyst_specific", "catalyst_within_24h", "price_volume_confirmed", "crowding_low")
            item["evidence_checklist"] = {k: bool(_cl.get(k, False)) for k in _keys}
            item["rubric_score"] = sum(1 for k in _keys if _cl.get(k))
            # v2.2 (P5 SHORT 高门槛): short_check 解析落库 (纯归因, 不拦不改流程)
            if direction == "SHORT":
                _sc = it.get("short_check") or {}
                if not isinstance(_sc, dict):
                    _sc = {}
                try:
                    _sqz = int(_sc.get("squeeze_risk", 0) or 0)
                except (TypeError, ValueError):
                    _sqz = 0
                try:
                    _dce = int(_sc.get("downside_catalyst_expiry_hours", 0) or 0)
                except (TypeError, ValueError):
                    _dce = 0
                item["short_check"] = {
                    "borrow_feasible": bool(_sc.get("borrow_feasible", False)),
                    "downside_catalyst_expiry_hours": _dce,
                    "squeeze_risk": _sqz,
                    "provided": bool(_sc),   # 模型是否交作业 (未交=旧习惯残留, 切片可判)
                }
        out.append(item)
    return out


def _annotate_news_channel(surfaced: List[Dict[str, Any]], news: List[Dict[str, Any]]) -> None:
    """S0 (2026-07-27): 机械归因浮现标的 → 首报新闻渠道 (record-only, 不影响筛选/排序).

    匹配口径: 新闻 symbols 标注含该标的 (P4a 匹配器产出), 或裸 ticker 词界出现在标题
    (大小写敏感, 与 P4a 同规则)。多条命中取 published 最早者 = 首报。
    落 s["info_channel"] (如 us_bloomberg / none_matched) + s["first_news_id"]。
    """
    import re as _re
    from email.utils import parsedate_to_datetime as _p2d
    if not surfaced:
        return
    prepared = []
    for n in news or []:
        syms = n.get("symbols") or "[]"
        if isinstance(syms, str):
            try:
                syms = json.loads(syms)
            except Exception:
                syms = []
        try:
            pub = _p2d(str(n.get("published_at") or ""))
        except Exception:
            pub = None
        prepared.append((set(str(x).replace("US.", "") for x in (syms or [])),
                         str(n.get("title") or ""), str(n.get("source") or "unknown"),
                         str(n.get("id") or ""), pub))
    # 歧义 ticker (F/T/V/MA...) 标题兜底误归因风险 (独立字母命中任意标题) — 与 P4a 同规:
    #   只信 symbols 标注 (P4a 产出已带歧义守卫), 不做标题裸词匹配。
    try:
        from app.data.rsshub_us_news import RSSHubUSNewsFetcher as _NF
        _ambiguous = _NF.AMBIGUOUS_TICKERS
    except Exception:
        _ambiguous = frozenset()
    for s in surfaced:
        sym = str(s.get("symbol", "")).replace("US.", "").upper()
        if not sym:
            continue
        hits = []
        pat = None if sym in _ambiguous else _re.compile(r"\b" + _re.escape(sym) + r"\b")
        for syms, title, src, nid, pub in prepared:
            if sym in syms or (pat is not None and pat.search(title)):
                hits.append((pub, src, nid))
        if hits:
            # 最早 published 优先; None 沉底 (统一 timestamp 化, 防 None/naive-aware 比较炸)
            hits.sort(key=lambda h: (h[0] is None, h[0].timestamp() if h[0] else 0))
            s["info_channel"] = hits[0][1]
            s["first_news_id"] = hits[0][2]
        else:
            s["info_channel"] = "none_matched"
            s["first_news_id"] = None


def _annotate_emergence_streak(surfaced: List[Dict[str, Any]]) -> None:
    """[shadow 2026-07-22] 浮现连续天数埋点 (record-only, 不影响任何筛选/排序).

    背景: 7/9-7/21 实证重开≥3次标的 +$202 vs 一次性标的 -$331 (含幸存者偏差),
    且 UE 存在同 thesis 惯性续期 (ORCL 连续 6 日空, 第 6 天 -$100 回吐)。
    本埋点给每个浮现标的标注 emergence_streak_days, 两周后判分
    "首日浮现 vs 续期第 N 天"绩效差。

    口径: 以 scan jsonl 历史中的 distinct scan 日期序列为日历 (scan 仅交易日跑,
    天然跳过周末/节假日), 同 symbol+direction 在紧邻的前序 scan 日持续出现则续期。
    首日浮现 streak=1; 中断后重新浮现从 1 重计。今日多次 scan 不重复累加。
    """
    try:
        history = [r for r in _read_jsonl(_SCAN_PATH)
                   if r.get("type") == "scan" and r.get("date") and r.get("date") < _today()]
        # 每个历史 scan 日 → 当日浮现过的 (symbol, direction) 集合 (同日多次 scan 合并)
        by_date: Dict[str, set] = {}
        for r in history:
            keys = by_date.setdefault(r["date"], set())
            for s in r.get("surfaced", []):
                if s.get("direction") in ("LONG", "SHORT"):
                    keys.add((s.get("symbol"), s.get("direction")))
        prior_dates = sorted(by_date.keys(), reverse=True)  # 紧邻在前
        for s in surfaced:
            streak = 1
            if s.get("direction") in ("LONG", "SHORT"):
                key = (s["symbol"], s["direction"])
                for d in prior_dates:
                    if key in by_date[d]:
                        streak += 1
                    else:
                        break
            s["emergence_streak_days"] = streak
    except Exception as e:  # 埋点失败不阻断 scan 主流程
        print(f"  [warn] emergence_streak 埋点失败 (继续): {e}")
        for s in surfaced:
            s.setdefault("emergence_streak_days", None)


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
        max_tokens=4000,
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

    # [shadow] 浮现连续天数标注 (必须在 _append_jsonl 前: 只读历史日, 不含今日)
    _annotate_emergence_streak(surfaced)

    # S0 (2026-07-27, DEB-20260727 §六.3): 真实新闻渠道归因 — origin.info_source 此前
    #   恒为常量 LLM_EVENT_REASONING (管线标签), 渠道判分零区分度 ("常量退化"第三例)。
    #   机械归因: 标的出现在哪条新闻 (symbols 标注或标题词界) → 最早发布者=首报渠道。
    #   无匹配 → none_matched (市场佐证/纯推理浮现)。S1 切片两周后判 Q1。
    _annotate_news_channel(surfaced, _gather_news(48))

    # v2.2 (2026-07-25, A2 self-critique shadow, 用户批"不改流程可执行"): 同模型二次调用
    #   逐条攻击因果链最弱环, verdict(KEEP/DOWNGRADE/DROP) 只落库不拦截 — 注入集合零变化。
    #   判分: 攒样本后切 DROP vs KEEP 的 fwd 对比, 显著则再裁是否实拦。
    #   回滚: universe_expansion.self_critique=false。显式缓存已开 → 二次调用前缀命中成本低。
    if bool(_cfg("universe_expansion.self_critique", True)) and tradeable:
        try:
            _cr_prompt = ("以下是你刚浮现的标的列表(JSON)。现在切换为独立审查官角色: 逐条攻击其因果链最薄弱的一环 "
                          "(传导断点/已price-in/拥挤/证据不足), 如实输出。严格 JSON: "
                          "{\"critiques\": [{\"symbol\": str, \"weakest_link\": 一句话, "
                          "\"verdict\": \"KEEP/DOWNGRADE/DROP\"}]}\n\n"
                          + json.dumps([{k: s.get(k) for k in ("symbol", "direction", "causal_chain", "thesis", "self_tier")}
                                        for s in tradeable], ensure_ascii=False))
            _cr = call_llm(feature="universe_expansion_reasoning", prompt=_cr_prompt,
                           system=_system, json_mode=True, max_tokens=1500)
            if _cr and _cr.success:
                _crp = _cr.response if isinstance(_cr.response, dict) else json.loads(_cr.response)
                _cmap = {str(c.get("symbol", "")).replace("US.", "").upper(): c
                         for c in (_crp.get("critiques") or []) if isinstance(c, dict)}
                _n_drop = 0
                for s in surfaced:
                    c = _cmap.get(s["symbol"])
                    if c:
                        _v = str(c.get("verdict", "")).upper()
                        s["critique"] = {
                            "weakest_link": str(c.get("weakest_link", ""))[:120],
                            "verdict": _v if _v in ("KEEP", "DOWNGRADE", "DROP") else "KEEP",
                        }
                        if _v in ("DOWNGRADE", "DROP"):
                            _n_drop += 1
                print(f"  [self-critique shadow] 覆盖 {len(_cmap)}/{len(tradeable)}, "
                      f"DOWNGRADE/DROP {_n_drop} (只落库不拦截)")
        except Exception as _cr_e:
            print(f"  [warn] self-critique 失败 (降级无批注): {_cr_e}")

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
    # [2026-07-22] tier 分布计数: 解除数量压制后监控 THESIS_ONLY 是否洪泛 (配合输入源健康行)
    _tc = {"HARD_EVENT": 0, "MARKET_CORROBORATED": 0, "THESIS_ONLY": 0}
    for s in surfaced:
        _t = str(s.get("self_tier", "")).upper()
        if _t in _tc:
            _tc[_t] += 1
    print(f"  [tier分布] HARD {_tc['HARD_EVENT']} / CORR {_tc['MARKET_CORROBORATED']} / "
          f"THESIS {_tc['THESIS_ONLY']}" + (" ⚠️THESIS占多数" if _tc['THESIS_ONLY'] > len(surfaced) / 2 else ""))
    for s in surfaced:
        tag = "🌍外" if s["is_universe_external"] else "  "
        _stk = s.get("emergence_streak_days")
        _stk_tag = f" 续期D{_stk}" if _stk and _stk > 1 else ""
        print(f"  {s['direction']:5} {s['symbol']:6} conf={s['confidence']:.2f} {tag} "
              f"{s['winner_loser']:6} | {s['causal_chain'][:70]}{_stk_tag}")
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
        # 2026-07-16 P1 修复: universe_expansion confidence 上限 50.
        #   根因: LLM 直接输出 confidence=0.85-0.92, 高于 DA 2模型一致的 0.70.
        #   但 universe_expansion 的分析深度 (1句thesis, 无conditions, 无invalidation)
        #   远不如 DA (200字thesis + 3 conditions + invalidation).
        #   高 confidence → auto_execute 排序优先 → 抢占 DA 信号的预算分配.
        #   修复: 上限 50 (低于 DA 一致70, 高于 DA 分歧35), 反映"有方向但无深度分析".
        #   [2026-07-25 勘误留记] 曾试 cap→30 降 UE 排序优先级, 后发现排序主键是
        #   pipeline.py::_src_priority (ADR-020 用户指定 UE 最优先), conf 仅同源内次键 —
        #   改 cap 对跨源排序无效且污染 scorecard conf 分桶, 已回滚。资源错配议题见 handoff。
        _raw_conf = int(round(float(s["confidence"]) * 100))  # schema INTEGER 0-100
        _UE_CONF_CAP = 50
        _capped_conf = min(_raw_conf, _UE_CONF_CAP)
        instructions.append({
            "date": today,
            "symbol": f"US.{s['symbol']}",
            "direction": pb_direction,
            "entry_price": round(float(px), 4),  # 触发基准 (_check_intraday_trigger 用)
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": nums["stop_loss"],
            "take_profit": nums["take_profit"],
            "confidence": _capped_conf,
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
                    # S0 (2026-07-27): 真实首报渠道 + 新闻 id (机械归因, 见 _annotate_news_channel)
                    "info_channel": s.get("info_channel") or None,
                    "first_news_id": s.get("first_news_id") or None,
                    "causal_chain": s.get("causal_chain", ""),
                    "winner_loser": s.get("winner_loser", ""),
                    "corroborated_by_market": s.get("corroborated_by_market"),
                    # [shadow] 浮现连续天数透传 → 交易级判分可直接 join (首日=1)
                    "emergence_streak_days": s.get("emergence_streak_days"),
                    # [2026-07-22] 解除数量压制后按证据层级分层判分的前提: tier/证据透传落库
                    "self_tier": s.get("self_tier") or None,
                    "evidence_ref": (s.get("evidence_ref") or "")[:300] or None,
                    # [2026-07-25 信息源复盘] cap 前原始 conf 落库 — 否则 07-16 cap=50 把 LLM
                    #   置信分层全抹掉 (库内 UE conf 恒=50, 归因无信息量)。排序仍用 capped。
                    "raw_confidence": _raw_conf,
                    # [v2.2 2026-07-25, P1/P2] 可证伪字段 + rubric 落库 (纯归因, 不进执行)
                    "invalidation_ue": s.get("invalidation") or None,
                    "horizon_hours": s.get("horizon_hours") or None,
                    "expected_move_pct": s.get("expected_move_pct") or None,
                    "rubric_score": s.get("rubric_score"),
                    "evidence_checklist": s.get("evidence_checklist") or None,
                    # [v2.2 2026-07-25, P5/A2] SHORT 三问 + self-critique shadow 落库 (纯归因)
                    "short_check": s.get("short_check") or None,
                    "critique": s.get("critique") or None,
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
                            # [P0.1-b 2026-07-23] 分层标签随行落库: tier 排序验证 + streak 判分的原料
                            "self_tier": s.get("self_tier"),
                            "emergence_streak_days": s.get("emergence_streak_days"),
                            "evidence_ref": (s.get("evidence_ref") or "")[:80] or None,
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

    # [P0.1-b 2026-07-23] 分层切片: self_tier 排序验证 (P1.3 判据) + streak 判分
    _rows = [x for r in scores for x in r.get("results", [])]

    def _slice_print(title: str, key_fn, order=None):
        buckets: Dict[str, List[float]] = {}
        hits_m: Dict[str, int] = {}
        for x in _rows:
            k = key_fn(x)
            if k is None:
                continue
            buckets.setdefault(k, []).append(x["fwd_pct"])
            hits_m[k] = hits_m.get(k, 0) + (1 if x.get("hit") else 0)
        if not buckets:
            print(f"{title}: 无带标签评分数据 (score 需含分层字段)")
            return
        print(title)
        keys = order or sorted(buckets)
        for k in keys:
            if k not in buckets:
                continue
            v = buckets[k]
            print(f"    {k:24s} n={len(v):3d} 命中率={round(hits_m[k]/len(v),3):.3f}"
                  f" 平均前瞻={sum(v)/len(v):+.2f}%")

    _slice_print("按 self_tier 切片 (预期 HARD_EVENT > MARKET_CORROBORATED > THESIS_ONLY):",
                 lambda x: x.get("self_tier"),
                 order=["HARD_EVENT", "MARKET_CORROBORATED", "THESIS_ONLY"])
    _slice_print("按浮现连续天数切片 (07-22 埋点判分):",
                 lambda x: ("首日" if (x.get("emergence_streak_days") or 0) <= 1
                            else ("2日续期" if x["emergence_streak_days"] == 2 else "≥3日续期"))
                 if x.get("emergence_streak_days") is not None else None,
                 order=["首日", "2日续期", "≥3日续期"])
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


def cmd_push(dry: bool = False, date: Optional[str] = None) -> int:
    """读今日最新一轮 scan → 钉钉推一条整体汇总 (镜像 da_bridge_dingtalk_push 模式)。

    新鲜度守卫: 只推今日 ET 的 scan 记录; 无今日记录 → 跳过不推 (绝不推陈旧)。
    失败安全: 钉钉异常吞掉, 不影响主链。entry_enabled 状态一并汇报 (record-only / 已注入)。
    """
    target = date or _today()
    recs = [r for r in _read_jsonl(_SCAN_PATH)
            if r.get("type") == "scan" and r.get("date") == target]
    if not recs:
        print(f"[push] {target} 无 scan 记录 (今日未扫描/非交易日), 跳过不推")
        return 0
    rec = recs[-1]  # 今日最后一轮
    surfaced = rec.get("surfaced", []) or []
    tradeable = [s for s in surfaced if s.get("direction") in ("LONG", "SHORT")]
    entry_enabled = bool(_cfg("universe_expansion.entry_enabled", False))

    n_long = sum(1 for s in tradeable if s.get("direction") == "LONG")
    n_short = sum(1 for s in tradeable if s.get("direction") == "SHORT")
    n_ext = sum(1 for s in tradeable if s.get("is_universe_external"))
    n_skip = sum(1 for s in surfaced if s.get("direction") == "SKIP")
    tier_cnt: Dict[str, int] = {}
    for s in tradeable:
        t = s.get("self_tier") or "-"
        tier_cnt[t] = tier_cnt.get(t, 0) + 1

    mode = "已注入DB→执行轨道" if entry_enabled else "record-only(未下单)"
    lines = [
        f"🌌 Universe选股扫描完成 ({target})",
        f"模型={rec.get('model_used','?')} prompt={rec.get('prompt_version','?')} · {mode}",
        f"可交易: {len(tradeable)}只 (多{n_long}/空{n_short}) · 池外{n_ext} · SKIP{n_skip}",
    ]
    if tier_cnt:
        lines.append("自评tier: " + " ".join(f"{k}:{v}" for k, v in tier_cnt.items()))
    lines.append("")
    for s in sorted(tradeable, key=lambda x: x.get("confidence", 0) or 0, reverse=True):
        sym = s.get("symbol", "")
        d = s.get("direction", "")
        conf = s.get("confidence", 0) or 0
        wl = s.get("winner_loser", "")
        ext = "🌍外 " if s.get("is_universe_external") else ""
        tier = s.get("self_tier", "")
        tier_str = f" [{tier}]" if tier else ""
        px = s.get("scan_price")
        px_str = f" 参考${px:.2f}" if px else ""
        chain = (s.get("causal_chain", "") or "")[:60]
        thesis = (s.get("thesis", "") or "")[:50]
        lines.append(f"{d} {sym} conf={conf:.2f} {ext}{wl}{tier_str}{px_str}")
        if chain:
            lines.append(f"  链: {chain}")
        if thesis:
            lines.append(f"  论: {thesis}")
    lines.append("")
    lines.append("数值(entry/SL/TP)由 inject 代码计算; 实际执行价=re-price 实时bid/ask")
    msg = "\n".join(lines)
    print(msg)
    if dry:
        print("\n[dry-run] 未推送")
        return 0
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        DingTalkNotifier().send_text(msg)
        print("\n[ok] 钉钉已推送")
    except Exception as e:
        print(f"\n[warn] 钉钉推送失败(不影响链路): {e}")
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
    p_push = sub.add_parser("push", help="读今日最新一轮 scan → 钉钉整体汇总 (新鲜度守卫)")
    p_push.add_argument("--dry", action="store_true", help="只打印不推送")
    p_push.add_argument("--date", help="scan 日期 YYYY-MM-DD (默认今日 ET)")
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
        if args.cmd == "push":
            return cmd_push(dry=args.dry, date=args.date)
    except Exception as e:
        import traceback
        print(f"⚠️ 执行异常 ({args.cmd}): {e}")
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    _ret = main()
    # 2026-07-16: 强制干净退出。futu OpenQuoteContext 的非 daemon 看门狗线程会吊住
    # 进程 (曾观测 scan 写完记录后挂起 10min+), 导致 cron `scan && inject && push`
    # 卡在第一段不推进。所有实质工作 (jsonl 落盘 / DB 提交 / 钉钉发送) 均在 main()
    # return 前同步完成, 残留仅为 futu 线程 → flush 后直接终止, 不等待残留线程。
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(_ret if isinstance(_ret, int) else 0)
