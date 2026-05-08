"""
元分析师 (MetaAnalyst)

站在系统之上，审视系统本身。每周对系统进行一次"体检"。

职责:
1. 评估辩论是否改善了盘前计划质量
2. 识别所有 Agent 共同的盲点
3. 建议策略参数调整
4. 建议新增系统规则
5. 生成系统进化路线图

这是 FTSeek "自我改进闭环"的核心 — 系统审视自身，自动提出改进建议。
"""

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.review.meta_analyst")

FTSEEK_DB = os.environ.get("FTSEEK_DB_PATH", "data/ftseek.db")
LESSONS_DB = os.environ.get("FTSEEK_HK_LESSONS_DB", "data/hk_lessons.db")


@dataclass
class MetaAnalysis:
    """元分析结果"""
    debate_impact_assessment: str = ""        # 辩论对盘前计划的影响
    shared_blind_spots: List[str] = field(default_factory=list)  # 共同盲点
    strategy_adjustments: List[Dict] = field(default_factory=list)  # 策略参数调整
    new_system_rules: List[str] = field(default_factory=list)  # 新系统规则
    evolution_recommendations: List[str] = field(default_factory=list)  # 进化建议
    weekly_score: float = 0.0                 # 本周系统综合评分
    raw_response: str = ""

    def to_dict(self) -> Dict:
        return {
            "debate_impact": self.debate_impact_assessment,
            "blind_spots": self.shared_blind_spots,
            "strategy_adjustments": self.strategy_adjustments,
            "new_rules": self.new_system_rules,
            "evolution_recommendations": self.evolution_recommendations,
            "weekly_score": self.weekly_score,
        }


META_ANALYST_PROMPT = """你是 FTSeek 的**元分析师 (Meta Analyst)**。

你的职责是站在系统之上，审视 FTSeek 这一周的表现，提出具体的改进建议。
你不对市场做判断，你对"系统本身"做判断。

【你的分析框架】
1. 辩论是否改善了盘前计划？
   - 辩论日的计划执行率 vs 非辩论日是否有差异？
   - 辩论是否帮助避免了某些亏损？
   - 辩论质量评分有无提高趋势？

2. 所有 Agent 共同的盲点是什么？
   - 有没有所有 Agent 都忽略的市场信号？
   - 有没有反复出现但从未被识别为模式的问题？

3. 有哪些策略参数需要调整？
   - 仓位上限是否合理？止损幅度是否需要调整？
   - 哪些时间阶段的表现最差？应不应该在某些时段减少交易？

4. 应该新增哪些系统规则？
   - 基于本周的教训，有什么规则可以防止未来重复犯错？

5. 系统整体健康度评分 (0-100)
   - 考虑: 胜率、盈亏比、最大回撤、计划执行率、教训质量

【输入数据】
{weekly_data}

【输出要求】
必须输出严格 JSON:
{{
  "debate_impact_assessment": "辩论对盘前计划的改善评估 (中文，不超过100字)",
  "shared_blind_spots": ["Agent 共同盲点1", "盲点2"],
  "strategy_adjustments": [
    {{"parameter": "参数名", "current_value": "当前值", "suggested_value": "建议值", "reason": "原因"}}
  ],
  "new_system_rules": ["新规则1", "新规则2"],
  "evolution_recommendations": ["本周最重要的3条进化建议"],
  "weekly_score": 0-100
}}

分析要具体、可操作。不要泛泛而谈。每个建议都应该能直接转化为代码或配置更改。"""


def _collect_weekly_data() -> Dict:
    """收集本周数据供元分析使用"""
    today = datetime.now()
    week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    week_end = today.strftime("%Y-%m-%d")

    data = {
        "period": f"{week_start} ~ {week_end}",
        "trading_days": 0,
        "trades": {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0},
        "best_trade": None, "worst_trade": None,
        "battle_plan": {"days_with_plan": 0, "avg_hit_rate": 0},
        "debate": {"debate_days": 0, "total_debates": 0, "avg_quality": 0},
        "lessons": {"total": 0, "patterns": 0},
        "anomalies": {"total": 0},
        "by_time_phase": {},
    }

    # 从 ftseek.db 收集交易数据
    try:
        conn = sqlite3.connect(FTSEEK_DB)
        conn.row_factory = sqlite3.Row

        # positions 表
        cursor = conn.execute(
            """
            SELECT * FROM positions
            WHERE entry_time >= ? AND entry_time <= ?
            ORDER BY entry_time
            """,
            (week_start, week_end + " 23:59:59"),
        )
        positions = [dict(r) for r in cursor.fetchall()]
        conn.close()

        if positions:
            data["trading_days"] = len(set(
                p.get("entry_time", "")[:10] for p in positions
            ))

            for p in positions:
                pnl = p.get("realized_pnl", 0) or 0
                data["trades"]["total"] += 1
                data["trades"]["total_pnl"] += pnl
                if pnl > 0:
                    data["trades"]["wins"] += 1
                elif pnl < 0:
                    data["trades"]["losses"] += 1

            data["best_trade"] = max(positions, key=lambda x: x.get("realized_pnl", 0) or 0).get("code", "")
            data["worst_trade"] = min(positions, key=lambda x: x.get("realized_pnl", 0) or 0).get("code", "")

    except Exception as e:
        logger.debug(f"[MetaAnalyst] ftseek.db 数据收集失败: {e}")

    # 从 lessons DB 收集
    try:
        conn = sqlite3.connect(LESSONS_DB)
        conn.row_factory = sqlite3.Row

        cursor = conn.execute(
            """
            SELECT COUNT(*) as c FROM lessons
            WHERE trade_date >= ? AND trade_date <= ?
            """,
            (week_start, week_end),
        )
        data["lessons"]["total"] = cursor.fetchone()["c"]

        cursor = conn.execute(
            """
            SELECT COUNT(DISTINCT pattern_group_id) as c FROM lessons
            WHERE pattern_group_id != '' AND validation_status != 'invalidated'
            """
        )
        data["lessons"]["patterns"] = cursor.fetchone()["c"]

        conn.close()
    except Exception as e:
        logger.debug(f"[MetaAnalyst] lessons DB 数据收集失败: {e}")

    # 从辩论归档收集
    try:
        from pathlib import Path
        archive_dir = Path("data/debate_archive")
        if archive_dir.exists():
            for f in sorted(archive_dir.glob("*.json")):
                f_date = f.stem
                if week_start <= f_date <= week_end:
                    debates = json.loads(f.read_text())
                    data["debate"]["debate_days"] += 1
                    data["debate"]["total_debates"] += len(debates)
                    qualities = [
                        v.get("synthesis", {}).get("debate_quality_score", 0)
                        for v in debates.values()
                    ]
                    if qualities:
                        data["debate"]["avg_quality"] += sum(qualities) / len(qualities)
    except Exception as e:
        logger.debug(f"[MetaAnalyst] 辩论归档数据收集失败: {e}")

    # 从复盘 DB 收集
    try:
        conn = sqlite3.connect(FTSEEK_DB)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT * FROM trade_evaluations
            WHERE created_at >= ? AND created_at <= ?
            """,
            (week_start, week_end + " 23:59:59"),
        )
        evaluations = [dict(r) for r in cursor.fetchall()]
        conn.close()

        if evaluations:
            data["anomalies"]["total"] = sum(
                1 for e in evaluations if e.get("is_anomaly", 0)
            )
    except Exception:
        pass

    return data


def _format_weekly_data_for_llm(data: Dict) -> str:
    """格式化为 LLM 可读的文本"""
    parts = [f"【分析周期】{data['period']}", ""]

    t = data["trades"]
    total = t["total"]
    wr = t["wins"] / max(total, 1) * 100
    parts.extend([
        f"【交易数据】",
        f"交易天数: {data['trading_days']}",
        f"总交易: {total} 笔 (赢{t['wins']}/输{t['losses']})",
        f"胜率: {wr:.0f}%",
        f"总盈亏: {t['total_pnl']:+,.0f} HKD",
        f"最佳标的: {data.get('best_trade', 'N/A')}",
        f"最差标的: {data.get('worst_trade', 'N/A')}",
        "",
    ])

    db = data["debate"]
    if db["debate_days"] > 0:
        parts.extend([
            f"【多Agent辩论】",
            f"辩论天数: {db['debate_days']}/{data['trading_days']}",
            f"辩论次数: {db['total_debates']}",
            f"平均质量: {db['avg_quality']:.2f}",
            "",
        ])

    ls = data["lessons"]
    parts.extend([
        f"【知识积累】",
        f"本周教训: {ls['total']} 条",
        f"已确认模式: {ls['patterns']} 个",
        "",
    ])

    an = data["anomalies"]
    parts.extend([
        f"【异常检测】",
        f"本周异常: {an['total']} 个",
        "",
    ])

    return "\n".join(parts)


async def run_meta_analysis(config: Dict = None) -> MetaAnalysis:
    """执行元分析 (1次 deep 模型调用)

    通常在周末运行，分析本周系统表现。
    """
    weekly = _collect_weekly_data()
    weekly_text = _format_weekly_data_for_llm(weekly)

    messages = [
        {"role": "system", "content": META_ANALYST_PROMPT},
        {"role": "user", "content": weekly_text},
    ]

    from app.agents.base import BaseLLMService
    svc = BaseLLMService(config=config, default_model="qwen3.6-plus", default_timeout=90)
    raw = await svc.call_llm(messages)
    data = svc.extract_json(raw)

    result = MetaAnalysis(
        debate_impact_assessment=data.get("debate_impact_assessment", ""),
        shared_blind_spots=data.get("shared_blind_spots", []),
        strategy_adjustments=data.get("strategy_adjustments", []),
        new_system_rules=data.get("new_system_rules", []),
        evolution_recommendations=data.get("evolution_recommendations", []),
        weekly_score=float(data.get("weekly_score", 50)),
        raw_response=raw,
    )

    # 将策略调整建议写入配置覆盖 (带 reason)
    if result.strategy_adjustments:
        _apply_strategy_adjustments(result.strategy_adjustments)

    # 将新规则注入知识库
    if result.new_system_rules:
        _inject_new_rules(result.new_system_rules)

    logger.info(
        f"[MetaAnalyst] 元分析完成: score={result.weekly_score:.0f}, "
        f"adjustments={len(result.strategy_adjustments)}, "
        f"rules={len(result.new_system_rules)}"
    )
    return result


def _apply_strategy_adjustments(adjustments: List[Dict]):
    """将策略参数调整建议写入配置"""
    try:
        from pathlib import Path
        config_path = Path("config/meta_overrides.json")

        existing = {}
        if config_path.exists():
            existing = json.loads(config_path.read_text())

        for adj in adjustments:
            param = adj.get("parameter", "")
            if param:
                existing[param] = {
                    "value": adj.get("suggested_value", ""),
                    "reason": adj.get("reason", ""),
                    "suggested_at": datetime.now().isoformat(),
                }

        config_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        logger.info(f"[MetaAnalyst] 策略调整已保存: {len(adjustments)} 项 → {config_path}")
    except Exception as e:
        logger.warning(f"[MetaAnalyst] 保存策略调整失败: {e}")


def _inject_new_rules(rules: List[str]):
    """将新规则注入 hk.json"""
    try:
        memory_file = os.path.expanduser("~/.hermes/ftseek/memory/hk.json")
        if not os.path.exists(memory_file):
            return

        with open(memory_file, "r", encoding="utf-8") as f:
            memory = json.load(f)

        existing_rules = set(memory.get("market_specific_rules", []))
        for rule in rules:
            if rule not in existing_rules:
                existing_rules.add(rule)

        memory["market_specific_rules"] = list(existing_rules)[-15:]
        memory["updated_at"] = datetime.now().isoformat()

        with open(memory_file, "w", encoding="utf-8") as f:
            json.dump(memory, f, indent=2, ensure_ascii=False)

        logger.info(f"[MetaAnalyst] 新规则已注入 hk.json: {len(rules)} 条")
    except Exception as e:
        logger.warning(f"[MetaAnalyst] 注入新规则失败: {e}")


def run_meta_analysis_sync(config: Dict = None) -> MetaAnalysis:
    """同步执行元分析"""
    from app.agents.base import BaseLLMService
    return BaseLLMService.run_sync(run_meta_analysis, config, timeout=120)


def generate_evolution_report(meta: MetaAnalysis, weekly_data: Dict) -> str:
    """生成人类可读的系统进化周报 (markdown)"""
    lines = [
        "🧬 **FTSeek 系统进化周报**",
        f"📅 {weekly_data.get('period', '')}",
        "",
        "---",
        "",
        f"## 📊 综合评分: {meta.weekly_score:.0f}/100",
        "",
    ]

    # 交易数据
    t = weekly_data.get("trades", {})
    total = t.get("total", 0)
    if total > 0:
        wr = t.get("wins", 0) / max(total, 1) * 100
        lines.extend([
            "## 💰 交易表现",
            f"- 总交易: {total} 笔 (赢{t.get('wins', 0)}/输{t.get('losses', 0)})",
            f"- 胜率: {wr:.0f}%",
            f"- 总盈亏: {t.get('total_pnl', 0):+,.0f} HKD",
            f"- 交易日: {weekly_data.get('trading_days', 0)} 天",
            "",
        ])

    # 辩论影响
    if meta.debate_impact_assessment:
        lines.extend([
            "## 🧠 辩论影响评估",
            meta.debate_impact_assessment,
            "",
        ])

    # 盲点
    if meta.shared_blind_spots:
        lines.extend([
            "## 🔍 发现的盲点",
        ])
        for bs in meta.shared_blind_spots:
            lines.append(f"- {bs}")
        lines.append("")

    # 策略调整
    if meta.strategy_adjustments:
        lines.extend([
            "## ⚙️ 建议的策略调整",
            "",
            "| 参数 | 当前值 | 建议值 | 原因 |",
            "|------|--------|--------|------|",
        ])
        for adj in meta.strategy_adjustments:
            lines.append(
                f"| {adj.get('parameter', '')} | {adj.get('current_value', '')} "
                f"| {adj.get('suggested_value', '')} | {adj.get('reason', '')} |"
            )
        lines.append("")

    # 新规则
    if meta.new_system_rules:
        lines.extend([
            "## 📜 新增系统规则",
        ])
        for i, rule in enumerate(meta.new_system_rules):
            lines.append(f"{i+1}. {rule}")
        lines.append("")

    # 进化建议
    if meta.evolution_recommendations:
        lines.extend([
            "## 🚀 进化路线图",
        ])
        for i, rec in enumerate(meta.evolution_recommendations):
            lines.append(f"{i+1}. {rec}")
        lines.append("")

    lines.extend([
        "---",
        f"*FTSeek Meta Analysis · 自动生成 · {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
    ])

    return "\n".join(lines)
