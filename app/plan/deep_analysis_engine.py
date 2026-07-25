"""深度分析引擎 — 为作战计划中每个 target 生成专业多空辩论分析

核心能力（对标 MU_research-deep.md 输出质量）：
1. 组装技术面 / 因子验证 / 宏观 / 基本面 / 持仓 五类输入
2. 调用 LLM (feature="deep_target_analysis") 生成结构化 JSON
3. 可选基本面增强 (feature="web_search_fundamental")，失败降级为 N/A
4. 并行批量处理多个 targets (asyncio.gather)

输出字段（详见 deep_target_analysis.py USER_PROMPT_TEMPLATE）：
    bull_case[], bear_case[], verdict, causal_chain, historical_reference,
    fundamental_snapshot, risk_tiers, execution_plan
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.llm.model_call import call_llm
from app.llm.prompts.deep_target_analysis import (
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# 文本格式化工具：把结构化数据格式化为 prompt 友好的中文文本
# ════════════════════════════════════════════════════════════════════
def format_technicals_text(technicals: Dict) -> str:
    """将技术面数据字典格式化为 Prompt 输入文本。

    输入约定（缺字段安全降级）：
        - close_10_ema, close_50_sma, close_200_sma : 均线
        - rsi, macd, macd_signal                    : 动量
        - boll_mid, boll_upper, boll_lower          : 布林
        - atr, atr_pct                              : 波动
        - vwma, volume_ratio                        : 量价
        - swing_high, swing_low, week52_high, week52_low : 关键价位
        - ytd_change, trend_alignment               : 趋势/年内涨幅
    Args:
        technicals: 任意子集字典；缺失字段以 "N/A" 显示，不抛异常
    Returns:
        多行中文文本块，可直接拼入 prompt
    """
    if not technicals:
        return "(技术面数据缺失)"

    def _g(k, default="N/A"):
        v = technicals.get(k)
        return default if v is None else v

    lines = [
        f"- 趋势均线: 10EMA={_g('close_10_ema')} / 50SMA={_g('close_50_sma')} / 200SMA={_g('close_200_sma')}",
        f"- 趋势排列: {_g('trend_alignment', '未给出')} | YTD: {_g('ytd_change')}",
        f"- 动量: RSI={_g('rsi')} | MACD={_g('macd')} (signal={_g('macd_signal')})",
        f"- 布林带: 中={_g('boll_mid')} / 上={_g('boll_upper')} / 下={_g('boll_lower')}",
        f"- 波动: ATR={_g('atr')} (≈{_g('atr_pct')}/日) | VWMA={_g('vwma')} | 量比={_g('volume_ratio')}",
        f"- 关键价位: 52w高={_g('week52_high')} / 52w低={_g('week52_low')} | "
        f"swing_high={_g('swing_high')} / swing_low={_g('swing_low')}",
    ]
    return "\n".join(lines)


def format_macro_text(macro: Dict) -> str:
    """将宏观环境字典格式化为 Prompt 输入文本。

    输入约定（缺字段安全降级）：
        - vix, vix_change         : 恐慌指数
        - spy_trend, spy_change   : 大盘趋势
        - market_regime           : Risk-On / Risk-Off / Neutral
        - dxy, ust10y             : 美元 / 10Y 国债
        - upcoming_events         : 列表，未来重大宏观事件
    """
    if not macro:
        return "(宏观数据缺失)"

    def _g(k, default="N/A"):
        v = macro.get(k)
        return default if v is None else v

    lines = [
        f"- 市场状态: {_g('market_regime')}",
        f"- VIX: {_g('vix')} (变化: {_g('vix_change')})",
        f"- SPY 趋势: {_g('spy_trend')} (日内: {_g('spy_change')})",
        f"- DXY: {_g('dxy')} | 10Y 国债: {_g('ust10y')}",
    ]
    events = macro.get("upcoming_events") or []
    if events:
        ev_text = "; ".join(str(e) for e in events[:5])
        lines.append(f"- 未来事件: {ev_text}")
    return "\n".join(lines)


def _format_factor_validation(target: Dict) -> str:
    """从 target 中抽取因子验证结果（如有）格式化。"""
    fv = target.get("factor_validation") or target.get("factors") or {}
    if not fv:
        return "(无因子验证记录)"
    if isinstance(fv, str):
        return fv
    if isinstance(fv, dict):
        parts = [f"- {k}: {v}" for k, v in fv.items()]
        return "\n".join(parts) if parts else "(无因子验证记录)"
    if isinstance(fv, list):
        return "\n".join(f"- {item}" for item in fv)
    return str(fv)


def _safe_json_loads(raw: Any) -> Optional[Dict]:
    """容错地解析 LLM 返回为 JSON dict。

    处理：
    - 已是 dict 直接返回
    - 字符串首尾 ```json ``` markdown 包裹
    - 文本中混入 JSON 块（取首个 {...}）
    失败返回 None。
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None

    text = raw.strip()
    # 去 markdown 包裹
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    # 直接解析
    try:
        return json.loads(text)
    except Exception:
        pass

    # 提取首个 {...} 块（贪婪匹配最长）
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception as e:
            logger.debug(f"[deep_analysis] JSON 二次解析失败: {e}")
    return None


# ════════════════════════════════════════════════════════════════════
# 基本面增强 — 真实 API 数据源 (2026-06-17 重构)
# ════════════════════════════════════════════════════════════════════
# 根因修复: 旧版 _fetch_fundamental 用 LLM 编基本面数据 → 每只股票
# 统一标注"基于模型知识，可能不是最新" → 模板废话。
# 新版直接调用 Finnhub REST API 获取真实数据，每个维度独立判定成功/失败。

import os as _os
import requests as _req
from datetime import datetime as _dt, timedelta as _td


# _finnhub_get 已迁移到 providers/fundamental_provider.py
# 保留此注释作为迁移记录 (2026-06-17)


async def _fetch_real_fundamental(symbol: str, company_name: str = "") -> Dict:
    """从统一数据源获取基本面数据（通过 enrichment_router）
    
    重构说明：
      - 旧版直接调用 Finnhub REST API
      - 新版通过 enrichment_router 统一入口，支持多源聚合和自动降级
    
    返回格式保持兼容旧代码。
    """
    try:
        from app.data.enrichment_router import get_fundamental
        
        # 在线程池中执行同步调用
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: get_fundamental(symbol))
        
        if result.get("status") == "ok":
            data = result["data"]
            # 保持旧的返回格式
            return {
                "financials": data.get("financials", {}),
                "analyst": data.get("analyst", {}),
                "recent_news": data.get("recent_news", []),
                "next_catalyst": data.get("next_catalyst", {}),
                "_sources": data.get("_sources", {}),
            }
    except Exception as e:
        logger.error(f"[deep_analysis] enrichment_router failed for {symbol}: {e}")
    
    # 全部失败，返回空结构
    from app.utils.timezone import now_utc
    now_iso = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "financials": {"source": "none", "fetched_at": now_iso},
        "analyst": {"source": "none", "fetched_at": now_iso},
        "recent_news": [],
        "next_catalyst": {"source": "none", "fetched_at": now_iso},
        "_sources": {"financials": "failed", "analyst": "failed", "news": "failed", "catalyst": "failed"},
    }


# ── 数字格式化工具 ──
def _fmt_num(v, suffix="", divisor=1) -> str:
    """格式化数字。suffix='B' 时 Finnhub 返回百万单位，除以1000得十亿。"""
    if v is None:
        return "N/A"
    try:
        n = float(v) / divisor
        if suffix == "B":
            # Finnhub market cap is in millions. divisor=1000 → billions.
            if abs(n) >= 1000:
                return f"${n/1000:.1f}T"
            elif abs(n) >= 1:
                return f"${n:.1f}B"
            else:
                return f"${n*1000:.0f}M"
        return f"{n:.2f}{suffix}"
    except (ValueError, TypeError):
        return "N/A"


def _fmt_pct(v) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):.1f}%"
    except (ValueError, TypeError):
        return "N/A"


# 保留旧接口名兼容 (fundamental_analyst.py 也调用此函数)
async def _fetch_fundamental(symbol: str, company_name: str = "") -> Dict:
    """兼容入口 — 内部调用 _fetch_real_fundamental，返回格式兼容旧代码。"""
    real = await _fetch_real_fundamental(symbol, company_name)

    # 转为旧格式 (financials dict / analyst dict / recent_news list / next_catalyst dict)
    fin = real.get("financials", {})
    ana = real.get("analyst", {})
    news = real.get("recent_news", [])
    cat = real.get("next_catalyst", {})

    return {
        "financials": {
            "revenue": fin.get("market_cap", "N/A"),  # 旧字段复用
            "eps": fin.get("pe", "N/A"),
            "revenue_yoy": fin.get("revenue_yoy", "N/A"),
            "margin": fin.get("gross_margin", "N/A"),
        } if fin.get("source") != "none" else "N/A",
        "analyst": {
            "target_low": "N/A",  # Finnhub free 不提供目标价
            "target_high": "N/A",
            "target_median": "N/A",
            "buy_pct": ana.get("buy_pct", "N/A"),
            "hold_pct": ana.get("hold_pct", "N/A"),
            "sell_pct": ana.get("sell_pct", "N/A"),
        } if ana.get("source") != "none" else "N/A",
        "recent_news": [n.get("headline", "") for n in news] if news else [],
        "next_catalyst": {
            "event": cat.get("event", "N/A"),
            "date": cat.get("date", "N/A"),
        } if cat.get("source") != "none" else "N/A",
        # 传递真实数据到上层
        "_real_data": real,
    }


def _format_fundamental_text(fund: Dict) -> str:
    """把基本面 dict 格式化为可读中文文本（喂给主 prompt）。

    2026-06-17 重构: 使用 _real_data 中的真实 API 数据 + 时效性标注。
    """
    if not fund:
        return "(基本面数据缺失)"

    # 优先使用 _real_data (新版真实 API 数据)
    real = fund.get("_real_data")
    if real:
        return _format_real_fundamental_text(real)

    # 兼容旧格式 (fundamental_analyst.py 等)
    try:
        fin = fund.get("financials") or {}
        ana = fund.get("analyst") or {}
        news = fund.get("recent_news") or []
        cat = fund.get("next_catalyst") or {}

        fin_str = (
            f"营收={fin.get('revenue', 'N/A')}, EPS={fin.get('eps', 'N/A')}, "
            f"YoY={fin.get('revenue_yoy', 'N/A')}, 毛利={fin.get('margin', 'N/A')}"
            if isinstance(fin, dict) else str(fin)
        )
        ana_str = (
            f"买/持/卖={ana.get('buy_pct', 'N/A')}/{ana.get('hold_pct', 'N/A')}/{ana.get('sell_pct', 'N/A')}"
            if isinstance(ana, dict) else str(ana)
        )
        news_str = "; ".join(str(n) for n in news[:3]) if news else "无"
        cat_str = (
            f"{cat.get('event', 'N/A')} ({cat.get('date', 'N/A')})"
            if isinstance(cat, dict) else str(cat)
        )
        return (
            f"- 财务: {fin_str}\n"
            f"- 分析师: {ana_str}\n"
            f"- 近期新闻: {news_str}\n"
            f"- 下一催化: {cat_str}"
        )
    except Exception as e:
        logger.debug(f"[deep_analysis] 基本面格式化失败: {e}")
        return str(fund)[:500]


def _format_real_fundamental_text(real: Dict) -> str:
    """格式化真实 API 数据，每个维度标注数据源和时间。"""
    sources = real.get("_sources", {})
    lines = []

    # 财务
    fin = real.get("financials", {})
    if sources.get("financials") == "ok" and fin.get("source") != "none":
        lines.append(f"【财务数据 — {fin.get('source', '?')} @ {fin.get('fetched_at', '?')}】")
        parts = []
        if fin.get("market_cap") and fin["market_cap"] != "N/A":
            parts.append(f"市值={fin['market_cap']}")
        if fin.get("pe") and fin["pe"] != "N/A":
            parts.append(f"PE(TTM)={fin['pe']}")
        if fin.get("forward_pe") and fin["forward_pe"] != "N/A":
            parts.append(f"Forward PE={fin['forward_pe']}")
        if fin.get("pb") and fin["pb"] != "N/A":
            parts.append(f"PB={fin['pb']}")
        if fin.get("roe") and fin["roe"] != "N/A":
            parts.append(f"ROE={fin['roe']}")
        if fin.get("eps_ttm") and fin["eps_ttm"] != "N/A":
            parts.append(f"EPS(TTM)={fin['eps_ttm']}")
        if fin.get("revenue_yoy") and fin["revenue_yoy"] != "N/A":
            parts.append(f"营收YoY={fin['revenue_yoy']}")
        if fin.get("gross_margin") and fin["gross_margin"] != "N/A":
            parts.append(f"毛利率={fin['gross_margin']}")
        if fin.get("net_margin") and fin["net_margin"] != "N/A":
            parts.append(f"净利率={fin['net_margin']}")
        if fin.get("beta") and fin["beta"] != "N/A":
            parts.append(f"Beta={fin['beta']}")
        if fin.get("52w_high") and fin["52w_high"] != "N/A":
            parts.append(f"52周高={fin['52w_high']}")
        if fin.get("52w_low") and fin["52w_low"] != "N/A":
            parts.append(f"52周低={fin['52w_low']}")
        lines.append("  " + " | ".join(parts) if parts else "  (字段均为N/A)")
    else:
        lines.append("【财务数据 — ❌ API调用失败，无数据】")

    # 分析师
    ana = real.get("analyst", {})
    if sources.get("analyst") == "ok" and ana.get("source") != "none":
        lines.append(f"【分析师评级 — {ana.get('source', '?')} @ {ana.get('fetched_at', '?')}】")
        lines.append(
            f"  买入={ana.get('buy_pct', '?')} 持有={ana.get('hold_pct', '?')} "
            f"卖出={ana.get('sell_pct', '?')} ({ana.get('total_analysts', '?')}家) "
            f"趋势={ana.get('trend', '?')}"
        )
    else:
        lines.append("【分析师评级 — ❌ API调用失败，无数据】")

    # 新闻
    news = real.get("recent_news", [])
    if sources.get("news") == "ok" and news:
        lines.append(f"【近7天新闻 — finnhub/company-news ({len(news)}条)】")
        for n in news[:3]:
            hl = n.get("headline", "")[:100]
            src = n.get("source", "")
            lines.append(f"  • [{src}] {hl}")
    else:
        lines.append("【近7天新闻 — ❌ 无数据或API失败】")

    # 催化
    cat = real.get("next_catalyst", {})
    if sources.get("catalyst") == "ok" and cat.get("source") != "none":
        lines.append(f"【下一催化 — {cat.get('source', '?')} @ {cat.get('fetched_at', '?')}】")
        parts = [f"事件={cat.get('event', 'N/A')}"]
        if cat.get("date") and cat["date"] != "N/A":
            parts.append(f"日期={cat['date']}")
        if cat.get("eps_estimate"):
            parts.append(f"EPS预期={cat['eps_estimate']}")
        if cat.get("revenue_estimate") and cat["revenue_estimate"] != "N/A":
            parts.append(f"营收预期={cat['revenue_estimate']}")
        lines.append("  " + " | ".join(parts))
    else:
        lines.append("【下一催化 — ❌ API调用失败，无数据】")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# 主入口：单 target 深度分析
# ════════════════════════════════════════════════════════════════════
async def generate_deep_analysis(
    target: Dict,
    technicals: Dict,
    macro: Dict,
    position_held: bool = False,
) -> Optional[Dict]:
    """为单个 target 生成深度多空辩论分析。

    Args:
        target: plan 中的目标字典，至少包含
            - symbol, direction, current_price
            - entry_zone: {low, high} 或 entry_low/entry_high
            - stop_loss, take_profit, confidence
            - 可选 factor_validation / factors / company_name
        technicals: 扩展技术面数据（见 format_technicals_text）
        macro: 宏观环境（VIX / SPY / regime / 利率 / events）
        position_held: 当前是否已持有该标的（True 影响 prompt 中持仓状态）

    Returns:
        deep_analysis dict（含 bull_case/bear_case/verdict/risk_tiers/...）；
        若 LLM 调用或 JSON 解析失败 → None。
    """
    symbol = target.get("symbol", "?")
    try:
        # ── 1. 抽取标的字段（容错） ──
        direction = target.get("direction", "N/A")
        current_price = target.get("current_price") or target.get("price") or "N/A"
        entry_zone = target.get("entry_zone") or {}
        entry_low = (
            target.get("entry_low")
            or (entry_zone.get("low") if isinstance(entry_zone, dict) else None)
            or "N/A"
        )
        entry_high = (
            target.get("entry_high")
            or (entry_zone.get("high") if isinstance(entry_zone, dict) else None)
            or "N/A"
        )
        stop_loss = target.get("stop_loss") or "N/A"
        take_profit = target.get("take_profit") or "N/A"
        confidence = target.get("confidence", "N/A")

        # ── 2. 并行获取基本面（不阻塞主调用） ──
        fund = await _fetch_fundamental(symbol, target.get("company_name", ""))
        fundamental_text = _format_fundamental_text(fund)

        # ── 3. 拼装 prompt ──
        position_status = (
            f"已持有该标的（动作偏向：管理而非新建）" if position_held else "未持仓（候选新建仓位）"
        )

        user_prompt = USER_PROMPT_TEMPLATE.format(
            symbol=symbol,
            direction=direction,
            current_price=current_price,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=confidence,
            technicals_text=format_technicals_text(technicals),
            factor_validation_text=_format_factor_validation(target),
            fundamental_text=fundamental_text,
            macro_text=format_macro_text(macro),
            position_status=position_status,
        )

        # ── 4. 调 LLM ──
        # 兼容 Python 3.8 (无 asyncio.to_thread)
        _loop = asyncio.get_event_loop()
        try:
            res = await asyncio.wait_for(
                _loop.run_in_executor(
                    None,
                    lambda: call_llm(
                        feature="deep_target_analysis",
                        prompt=user_prompt,
                        system=SYSTEM_PROMPT,
                        temperature=0.4,
                        max_tokens=3000,
                        json_mode=True,
                    ),
                ),
                timeout=120,  # 深度分析超时 120s
            )
        except asyncio.TimeoutError:
            logger.warning(f"[deep_analysis·{symbol}] LLM 调用超时 (120s)")
            return None
        if not res or not res.success:
            logger.warning(
                f"[deep_analysis·{symbol}] LLM 调用失败: "
                f"{getattr(res, 'error', 'unknown')}"
            )
            return None

        # ── 5. JSON 解析（容错） ──
        parsed = _safe_json_loads(res.response)
        if not parsed:
            logger.warning(f"[deep_analysis·{symbol}] LLM 返回 JSON 解析失败")
            return None

        # ── 6. 结构性校验 + 兜底字段 ──
        parsed.setdefault("executive_summary", "")
        parsed.setdefault("bull_case", [])
        parsed.setdefault("bear_case", [])
        parsed.setdefault("verdict", {})
        parsed.setdefault("causal_chain", "")
        parsed.setdefault("historical_reference", "")
        parsed.setdefault("fundamental_snapshot", {
            "revenue_growth": "N/A",
            "pe_forward": "N/A",
            "analyst_consensus": "N/A",
            "next_catalyst": "N/A",
        })
        parsed.setdefault("risk_tiers", {})
        parsed.setdefault("execution_plan", {})

        # ── 7. 数据质量追踪 — 基于真实 API 调用结果判定 ──
        # 2026-06-17 重构: 不再检测"基于模型知识"字符串，直接用 API 成功/失败判定
        dq = parsed.get("data_quality") or {}

        # 获取真实数据的 _sources (如果有)
        real_data = fund.get("_real_data", {}) if fund else {}
        api_sources = real_data.get("_sources", {})

        # 技术面：检查是否有实际数据（至少4个有效字段）
        tech_valid_keys = ['close_10_ema', 'close_50_sma', 'close_200_sma', 'rsi', 'macd', 'atr']
        tech_filled = sum(1 for k in tech_valid_keys if technicals and technicals.get(k) is not None)
        tech_status = "完整" if tech_filled >= 4 else ("降级" if tech_filled >= 2 else "缺失")

        # 基本面：基于 Finnhub API 实际成功/失败
        fin_ok = api_sources.get("financials") == "ok"
        ana_ok = api_sources.get("analyst") == "ok"
        if fin_ok and ana_ok:
            fund_status = "完整"
        elif fin_ok or ana_ok:
            fund_status = "降级"
        else:
            fund_status = "缺失"

        # 新闻：基于 Finnhub company-news API
        news_ok = api_sources.get("news") == "ok"
        fund_news = real_data.get("recent_news", [])
        if news_ok and len(fund_news) > 0:
            news_status = "完整"
        elif news_ok:
            news_status = "降级"  # API worked but no news found
        else:
            news_status = "缺失"

        # 宏观：检查是否有实际数据（VIX + regime + 利率）
        macro_has_vix = macro and macro.get("vix") is not None and macro.get("vix") != 20.0
        macro_has_regime = macro and macro.get("market_regime") and macro["market_regime"] != "NEUTRAL"
        # 新增: 检查 treasury yields (来自 FRED)
        macro_has_rates = macro and (macro.get("ust10y") is not None or macro.get("treasury_yields"))
        if macro_has_vix and (macro_has_regime or macro_has_rates):
            macro_status = "完整"
        elif macro_has_vix or macro_has_regime or macro_has_rates:
            macro_status = "降级"
        else:
            macro_status = "缺失"

        # 计算完整性汇总
        statuses = {
            "技术面": tech_status, "基本面": fund_status,
            "新闻": news_status, "宏观": macro_status
        }
        complete_count = sum(1 for v in statuses.values() if v == "完整")
        degraded_count = sum(1 for v in statuses.values() if v == "降级")
        missing_count = sum(1 for v in statuses.values() if v == "缺失")
        completeness = "{}项完整/{}项降级/{}项缺失".format(
            complete_count, degraded_count, missing_count
        )

        # 影响说明 — 只标注真实缺失的维度，不用模板废话
        impact_notes = []
        if fund_status == "缺失":
            impact_notes.append("Finnhub财务/分析师API均失败，无基本面数据")
        elif fund_status == "降级":
            which_failed = []
            if not fin_ok:
                which_failed.append("财务指标")
            if not ana_ok:
                which_failed.append("分析师评级")
            impact_notes.append(f"部分基本面数据缺失({'+'.join(which_failed)})")
        if news_status == "缺失":
            impact_notes.append("新闻API失败，无法获取近期催化/风险事件")
        if macro_status == "缺失":
            impact_notes.append("宏观数据缺失(VIX/利率/市场状态均无)")
        impact_text = "; ".join(impact_notes) if impact_notes else "数据完整，分析可信"

        parsed["data_quality"] = {
            "technical": tech_status,
            "fundamental": fund_status,
            "news": news_status,
            "macro": macro_status,
            "overall_completeness": completeness,
            "impact_note": impact_text,
            "data_sources": {
                "financials": api_sources.get("financials", "not_called"),
                "analyst": api_sources.get("analyst", "not_called"),
                "news": api_sources.get("news", "not_called"),
                "catalyst": api_sources.get("catalyst", "not_called"),
            },
            "fetched_at": real_data.get("financials", {}).get("fetched_at", ""),
        }

        # ── 8. 元信息 ──
        parsed["_meta"] = {
            "symbol": symbol,
            "model_used": getattr(res, "model_used", ""),
            "from_cache": getattr(res, "from_cache", False),
            "fallback_used": getattr(res, "fallback_used", False),
            "cost_usd": getattr(res, "cost_usd", 0.0),
            "position_held": position_held,
        }
        return parsed

    except Exception as e:
        logger.exception(f"[deep_analysis·{symbol}] 未预期异常: {e}")
        return None


# ════════════════════════════════════════════════════════════════════
# 批量并行
# ════════════════════════════════════════════════════════════════════
async def generate_all_deep_analyses(
    targets: List[Dict],
    macro: Dict,
    positions: List[str],
) -> List[Dict]:
    """并行为所有 targets 生成深度分析。

    Args:
        targets: target 字典列表（每个需包含 symbol；可附带 technicals 子字段）
        macro: 公共宏观环境字典
        positions: 当前持仓 symbol 列表，用于判定 position_held

    Returns:
        与 targets 等长的列表，每个元素：
            {symbol, deep_analysis: {...} | None, error?: str}
        失败的标的 deep_analysis=None 但不会中断整批。
    """
    if not targets:
        return []
    held = set((s or "").upper() for s in (positions or []))

    async def _one(t: Dict) -> Dict:
        sym = (t.get("symbol") or "").upper()
        tech = t.get("technicals") or t.get("indicators") or {}
        try:
            da = await generate_deep_analysis(
                target=t,
                technicals=tech,
                macro=macro or {},
                position_held=sym in held,
            )
            return {"symbol": sym, "deep_analysis": da}
        except Exception as e:
            logger.exception(f"[deep_analysis·{sym}] 批量任务异常: {e}")
            return {"symbol": sym, "deep_analysis": None, "error": str(e)}

    results = await asyncio.gather(*[_one(t) for t in targets], return_exceptions=False)
    return list(results)


__all__ = [
    "generate_deep_analysis",
    "generate_all_deep_analyses",
    "format_technicals_text",
    "format_macro_text",
]
