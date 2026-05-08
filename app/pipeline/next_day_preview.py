"""
次日预判生成器 (NextDayPreview)

盘后二阶推导: 基于今日交易结果、教训和市场状态，预判次日需要关注的风险和机会。

输入: {今日教训, 作战计划执行, 市场状态快照, 板块轮动, 异常提醒}
输出: NextDayPreview (regime_shift, sector_rotation, watchlist_changes, risks, overnight_watch)

目的: 让系统"向前看"，而非仅仅复盘过去。这是二阶推导的核心体现。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.pipeline.next_day_preview")


@dataclass
class NextDayPreview:
    """次日预判结果"""
    regime_shift_flags: List[str] = field(default_factory=list)
    sector_rotation: List[Dict] = field(default_factory=list)
    watchlist_changes: List[Dict] = field(default_factory=list)
    emergent_risks: List[str] = field(default_factory=list)
    overnight_watch: List[str] = field(default_factory=list)
    market_bias_next_day: str = "NEUTRAL"
    key_question: str = ""           # 明日最需要回答的问题
    raw_response: str = ""

    def to_dict(self) -> Dict:
        return {
            "regime_shift_flags": self.regime_shift_flags,
            "sector_rotation": self.sector_rotation,
            "watchlist_changes": self.watchlist_changes,
            "emergent_risks": self.emergent_risks,
            "overnight_watch": self.overnight_watch,
            "market_bias_next_day": self.market_bias_next_day,
            "key_question": self.key_question,
        }

    def to_injection_text(self) -> str:
        """生成用于注入到盘前计划的文本"""
        parts = ["【昨日盘后预判 (次日关注)】"]

        if self.market_bias_next_day != "NEUTRAL":
            parts.append(f"明日倾向: {self.market_bias_next_day}")

        if self.regime_shift_flags:
            parts.append("市场状态变化:")
            for flag in self.regime_shift_flags:
                parts.append(f"  - {flag}")

        if self.sector_rotation:
            parts.append("板块轮动:")
            for sr in self.sector_rotation:
                frm = sr.get("from", "?")
                to = sr.get("to", "?")
                strength = sr.get("strength", "")
                parts.append(f"  - {frm} → {to} ({strength})")

        if self.watchlist_changes:
            parts.append("Watchlist 调整:")
            for wc in self.watchlist_changes:
                symbol = wc.get("symbol", "?")
                action = wc.get("action", "?")
                reason = wc.get("reason", "")
                parts.append(f"  - [{action}] {symbol}: {reason}")

        if self.emergent_risks:
            parts.append("新风险:")
            for risk in self.emergent_risks:
                parts.append(f"  - {risk}")

        if self.overnight_watch:
            parts.append(f"隔夜关注: {', '.join(self.overnight_watch)}")

        if self.key_question:
            parts.append(f"核心问题: {self.key_question}")

        return "\n".join(parts)


NEXT_DAY_PROMPT = """你是 FTSeek 的**次日预判分析师 (Next-Day Preview Analyst)**。

基于今日交易结果、市场状态和教训，预判明日需要关注的风险和机会。

【你的分析框架】
1. 市场状态是否在变化？(震荡→趋势 / 防御→进攻 / 缩量→放量)
2. 哪些板块在轮动？资金从哪里流出，流向哪里？
3. Watchlist 需要调整吗？哪些标的应该提升关注度？哪些应该降级？
4. 今日未覆盖的风险有哪些？盘前计划可能遗漏了什么？
5. 隔夜需要关注什么？(美股/夜盘期货/A股盘前)

【输入数据】
{today_data}

【输出要求】
必须输出严格 JSON:
{{
  "regime_shift_flags": ["状态变化1", "状态变化2"],
  "sector_rotation": [
    {{"from": "流出板块", "to": "流入板块", "strength": "STRONG|MODERATE|WEAK"}}
  ],
  "watchlist_changes": [
    {{"symbol": "HK.00001", "action": "elevate|demote|add|remove", "reason": "原因"}}
  ],
  "emergent_risks": ["盘前计划未覆盖的风险"],
  "overnight_watch": ["需要关注的隔夜标的/指数"],
  "market_bias_next_day": "BUY|SELL|NEUTRAL",
  "key_question": "明日最需要回答的一个核心问题"
}}

如果信息不足以形成判断，各字段留空数组。不要编造。"""


def _build_today_summary(collected: Dict, lessons: List[Dict],
                          plan_gap: Optional[Dict] = None) -> str:
    """构建今日数据摘要 (供 LLM 分析)"""
    trades = collected.get("trades", [])
    anomalies = collected.get("anomalies", [])
    battle_plan = collected.get("battle_plan") or {}
    ashare = collected.get("ashare_context") or {}
    capital = collected.get("capital_state") or {}

    parts = [f"日期: {collected.get('trade_date', '')}", ""]

    # 交易摘要
    total = len(trades)
    if total > 0:
        wins = [t for t in trades if (t.get("pnl", t.get("realized_pnl", 0)) or 0) > 0]
        losses = [t for t in trades if (t.get("pnl", t.get("realized_pnl", 0)) or 0) < 0]
        total_pnl = sum(t.get("pnl", t.get("realized_pnl", 0)) or 0 for t in trades)

        parts.append(f"【交易摘要】")
        parts.append(f"交易笔数: {total} (赢{len(wins)}/亏{len(losses)})")
        parts.append(f"总盈亏: {total_pnl:+,.0f} HKD")
        parts.append(f"胜率: {len(wins)/total*100:.0f}%")

        # 交易标的
        symbols = [t.get("code", "").replace("HK.", "") for t in trades]
        parts.append(f"交易标的: {', '.join(symbols)}")

        # 亏损交易详情
        if losses:
            parts.append("亏损交易:")
            for t in losses[:5]:
                code = t.get("code", "").replace("HK.", "")
                pnl = t.get("pnl", t.get("realized_pnl", 0)) or 0
                phase = (t.get("time_phase", {}) or {}).get("entry_phase", "?")
                parts.append(f"  - {code}: {pnl:+.0f} (入场: {phase})")
    else:
        parts.append("【交易摘要】今日无交易")

    # 作战计划执行
    plan_targets = battle_plan.get("targets", [])
    if plan_targets:
        plan_syms = [t.get("symbol", "").replace("HK.", "") for t in plan_targets]
        traded_syms = [t.get("code", "").replace("HK.", "") for t in trades]
        hit = set(plan_syms) & set(traded_syms)
        parts.extend([
            "",
            f"【作战计划】{len(plan_targets)}个目标, 执行{len(hit)}个",
            f"计划倾向: {battle_plan.get('overall_bias', 'N/A')}",
            f"市场状态: {battle_plan.get('market_regime', 'N/A')}",
        ])

    # 计划差距
    if plan_gap:
        parts.extend([
            "",
            f"【计划差距】质量={plan_gap.get('plan_quality_assessment', 'N/A')}",
            f"根因: {plan_gap.get('gap_root_cause', 'N/A')}",
        ])

    # 异常
    if anomalies:
        parts.extend([
            "",
            f"【异常提醒】{len(anomalies)}个:",
        ])
        for a in anomalies[:5]:
            parts.append(f"  - {a.get('type', '?')}: {a.get('msg', '')[:80]}")

    # 教训
    if lessons:
        parts.extend([
            "",
            f"【今日教训】{len(lessons)}条:",
        ])
        for l in lessons[:5]:
            parts.append(f"  - [{l.get('cause', '?')}] {l.get('lesson', '')[:100]}")

    # A股
    if ashare:
        parts.extend(["", "【A股收盘】"])
        for code, data in ashare.items():
            parts.append(f"  {code}: {data.get('change_pct', 0):+.2f}%")

    # 账户
    if capital:
        parts.extend([
            "",
            f"【账户】持仓{capital.get('open_position_count', 0)}个, "
            f"未实现{capital.get('unrealized_pnl', 0):+,.0f}",
        ])

    return "\n".join(parts)


async def generate_next_day_preview(collected: Dict, lessons: List[Dict],
                                     plan_gap: Optional[Dict] = None,
                                     config: Dict = None) -> NextDayPreview:
    """生成次日预判 (1次 deep 模型调用) — P2 2026-05-06 使用 agent_bridge"""
    try:
        from app.llm.agent_bridge import get_ftseek_agent_bridge
        bridge = get_ftseek_agent_bridge("hk")
        result = bridge.analyze_next_day_preview(collected, lessons, plan_gap)

        if result and result.get("status") != "fallback":
            return NextDayPreview(
                regime_shift_flags=result.get("regime_shift_flags", []),
                sector_rotation=result.get("sector_rotation", []),
                watchlist_changes=result.get("watchlist_changes", []),
                emergent_risks=result.get("emergent_risks", []),
                overnight_watch=result.get("overnight_watch", []),
                market_bias_next_day=result.get("market_bias_next_day", "NEUTRAL"),
                key_question=result.get("key_question", ""),
                raw_response=json.dumps(result, ensure_ascii=False),
            )
    except Exception as e:
        logger.warning(f"[NextDayPreview] agent_bridge 失败，降级: {e}")

    # 降级: 空预判
    return NextDayPreview(
        regime_shift_flags=[],
        sector_rotation=[],
        watchlist_changes=[],
        emergent_risks=["LLM 不可用，无法生成预判"],
        overnight_watch=[],
        market_bias_next_day="NEUTRAL",
        key_question="",
        raw_response="{}",
    )


def generate_next_day_preview_sync(collected: Dict, lessons: List[Dict],
                                    plan_gap: Optional[Dict] = None,
                                    config: Dict = None) -> NextDayPreview:
    """同步执行次日预判 — P2 2026-05-06 直接调用 (agent_bridge 已是同步)"""
    return generate_next_day_preview(collected, lessons, plan_gap, config)
