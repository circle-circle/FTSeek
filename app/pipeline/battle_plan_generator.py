"""
盘前作战计划生成器 (Battle Plan Generator)

职责:
1. 接收 LLM 的分析结果 (PreMarketAssessment) 或 聚合的宏观分析。
2. 提取关键的 market_regime, stop_trading, 以及具体的 plans (targets/restricted)。
3. 生成 standard JSON 格式写入 `daily_battle_plan.json`。
4. 通过钉钉 (DingTalk) 推送盘前作战计划摘要。
"""
import json
import logging
import time
from datetime import date
from typing import Dict, List, Any, Optional
from pathlib import Path

logger = logging.getLogger("ftseek.pipeline.battle_plan_generator")

class BattlePlanGenerator:
    """盘前作战计划生成器"""
    
    def __init__(self, output_path: str = None):
        self.output_path = output_path or str(Path(__file__).resolve().parent.parent.parent / "data" / "daily_battle_plan.json")
        self._debate_synthesis = None  # 辩论合成结果

    def set_debate_synthesis(self, synthesis):
        """注入辩论合成结果, 用于覆盖 overall_bias / risk_level"""
        self._debate_synthesis = synthesis

    def load_premarket_debate(self, path: str = None):
        """从 premarket_debate.json 文件加载多模型辩论结果 (P2 2026-05-06)

        Args:
            path: 文件路径, 默认从调度器输出路径读取
        """
        if path is None:
            path = str(Path(__file__).resolve().parent.parent.parent / "data" / "premarket_debate.json")

        try:
            if not Path(path).exists():
                logger.debug(f"[BattlePlanGenerator] 辩论文件不存在: {path}")
                return

            with open(path, 'r', encoding='utf-8') as f:
                debate_data = json.load(f)

            arbiter = debate_data.get('arbiter_conclusion', {})
            if not arbiter:
                logger.debug(f"[BattlePlanGenerator] 辩论文件无 arbiter_conclusion: {path}")
                return

            # 将 JSON 格式适配为内部 synthesis 格式
            self._debate_synthesis = type('DebateSynthesis', (), {
                'consensus_direction': arbiter.get('action_recommendation', 'NEUTRAL'),
                'conviction': arbiter.get('auditor_score', 50) / 100.0,
                'debate_quality_score': arbiter.get('auditor_score', 50) / 100.0,
                'market_regime': arbiter.get('risk_level', 'MEDIUM'),
                'risk_level': arbiter.get('risk_level', 'MEDIUM'),
                'risk_consensus': arbiter.get('risk_level', 'MEDIUM'),
                'stop_trading': arbiter.get('action_recommendation', '') == 'CAUTIOUS_BEARISH',
                'suggested_position_pct': arbiter.get('suggested_position_pct', 0.3),
                'avoid_codes': arbiter.get('avoid_codes', []),
                'consensus_ranking': arbiter.get('consensus_ranking', []),
                'bull_summary': arbiter.get('bull_summary', ''),
                'bear_summary': arbiter.get('bear_summary', ''),
                'disagreement_flags': [],
                'agent_opinions': [],
                'merged_symbols': [],
                'summary': '',
            })()

            logger.info(
                f"[BattlePlanGenerator] 已加载盘前辩论: "
                f"mode={debate_data.get('mode')}, "
                f"bias={self._debate_synthesis.consensus_direction}, "
                f"conviction={self._debate_synthesis.conviction:.2f}"
            )

        except Exception as e:
            logger.warning(f"[BattlePlanGenerator] 加载辩论文件失败: {e}")
        
    def generate_from_assessments(self, assessments: List[Any], auction_data: Dict = None) -> str:
        if not assessments:
            logger.warning("没有分析结果，生成保守计划")
            return self._save_fallback_plan("No analysis results")

        # LLM 调用全部失败 → 直接用智能降级，不产出方向全为 WATCH 的垃圾计划
        all_llm_failed = all(
            "LLM 调用失败" in str(getattr(a, "recommendation", ""))
            for a in assessments
        )
        if all_llm_failed:
            logger.warning("所有 LLM 盘前分析均失败，降级为 Watchlist 全覆盖白名单")
            return self._save_fallback_plan("LLM all failed")

        # ── 辩论结果注入 (P0 2026-05-09): 辩论驱动选标 ──
        debate_symbols = {}       # symbol → {net_confidence, support, concern}
        avoid_codes = set()
        debate_direction = "NEUTRAL"
        debate_conviction = 0.0

        if self._debate_synthesis:
            ds = self._debate_synthesis
            debate_direction = getattr(ds, 'consensus_direction', 'NEUTRAL')
            debate_conviction = getattr(ds, 'conviction', 0.0)
            avoid_codes = set(getattr(ds, 'avoid_codes', []))

            # 收集辩论共识排名
            for item in getattr(ds, 'consensus_ranking', []):
                sym = item.get('code', '')
                if sym:
                    debate_symbols[sym] = {
                        'net_confidence': item.get('net_confidence', 0),
                        'support': item.get('reason_bull', ''),
                        'concern': item.get('reason_bear', ''),
                    }

            logger.info(
                f"[BattlePlan] 辩论注入: direction={debate_direction}, "
                f"conviction={debate_conviction:.2f}, "
                f"ranked={len(debate_symbols)}, avoid={len(avoid_codes)}"
            )

        targets = []
        restricted = list(avoid_codes)
        max_risk = "LOW"
        overall_bias = debate_direction if debate_direction != "NEUTRAL" else "NEUTRAL"
        stop_trading = False

        # 如果辩论共识为 CAUTIOUS_BEARISH 且 conviction 高 → 停止交易
        if debate_direction in ("CAUTIOUS_BEARISH", "SELL") and debate_conviction > 0.7:
            stop_trading = True
            logger.warning("[BattlePlan] 辩论强烈看空，触发停止交易")

        for a in assessments:
            if getattr(a, "direction", "NO_TRADE") != "NO_TRADE":
                for sym in getattr(a, "focus_symbols", []):
                    # 辩论过滤: 在回避名单中的标的 → 降级为 restricted
                    if sym in avoid_codes:
                        continue

                    # 辩论排序: 有辩论共识的标的使用辩论信心调整 urgency
                    urgency = "High" if getattr(a, "confidence", 0) > 0.7 else "Medium"
                    trigger = getattr(a, "recommendation", "")[:50]

                    if sym in debate_symbols:
                        db = debate_symbols[sym]
                        # 辩论净信心 > 0 → 提升 urgency
                        if db['net_confidence'] > 0.3:
                            urgency = "High"
                        elif db['net_confidence'] < -0.3:
                            urgency = "Low"  # 辩论看空但 Pipeline 看多 → 降级
                        if db['support']:
                            trigger = f"[辩论+] {db['support'][:40]}"
                        if db['concern']:
                            trigger = f"{trigger} (⚠️{db['concern'][:30]})"

                    targets.append({
                        "symbol": sym,
                        "direction": getattr(a, "direction", "ANY").upper(),
                        "status": "VALID",
                        "urgency": urgency,
                        "key_trigger": trigger,
                        "invalidation_reason": getattr(a, "invalidating_factors", [""])[0] if getattr(a, "invalidating_factors", []) else "",
                        "debate_net_confidence": debate_symbols.get(sym, {}).get('net_confidence', 0),
                    })

        # 辩论优先排序: 净信心高的标的排在前面
        if debate_symbols:
            targets.sort(key=lambda t: t.get('debate_net_confidence', 0), reverse=True)

        # ── Phase 2 (2026-05-09): 竞价验证门禁 ──
        # 如果有竞价数据，过滤跳空过大的标的
        if auction_data:
            valid_targets = []
            filtered_out = []
            for t in targets:
                sym = t['symbol']
                auction_info = auction_data.get(sym, {})
                gap_pct = abs(auction_info.get('gap_pct', 0))
                if gap_pct > 0.03:  # 跳空超过 3% → 过滤
                    filtered_out.append(sym)
                    logger.info(f"[BattlePlan] 竞价过滤: {sym} (跳空 {gap_pct:.1%} > 3%)")
                    continue
                valid_targets.append(t)
            targets = valid_targets
            if filtered_out:
                restricted.extend(filtered_out)

        # 去重
        seen = set()
        unique_targets = []
        for t in targets:
            if t['symbol'] not in seen:
                seen.add(t['symbol'])
                unique_targets.append(t)

        # 辩论驱动的 market_regime 设置
        if debate_direction == "BUY" and debate_conviction > 0.6:
            max_risk = "LOW"
        elif debate_direction in ("SELL", "CAUTIOUS_BEARISH"):
            max_risk = "HIGH"

        plan = {
            "date": date.today().isoformat(),
            "targets": unique_targets,
            "restricted": restricted,
            "overall_bias": overall_bias,
            "market_regime": max_risk,
            "risk_level": "HIGH" if stop_trading else max_risk,
            "stop_trading": stop_trading,
            "debate_consensus": {
                "direction": debate_direction,
                "conviction": debate_conviction,
                "ranked_count": len(debate_symbols),
                "avoid_count": len(avoid_codes),
            },
            "generated_at": time.time()
        }

        return self._save_plan(plan)

    def generate_from_llm_json(self, raw_json: Dict) -> str:
        """直接从 LLM 的结构化 JSON 生成"""
        try:
            plans = raw_json.get("plans", [])
            targets = []
            for p in plans:
                targets.append({
                    "symbol": p.get("symbol"),
                    "direction": p.get("bias", "ANY").upper(),
                    "status": "VALID",
                    "urgency": "High" if p.get("conviction", 0) > 0.7 else "Medium",
                    "key_trigger": p.get("key_trigger", ""),
                    "invalidation_reason": p.get("invalidation", "")
                })
            
            plan = {
                "date": date.today().isoformat(),
                "targets": targets,
                "restricted": raw_json.get("restricted", []),
                "overall_bias": raw_json.get("market_regime", "NEUTRAL"),
                "market_regime": raw_json.get("market_regime", "NEUTRAL"),
                "risk_level": raw_json.get("risk_level", "MEDIUM"),
                "stop_trading": raw_json.get("stop_trading", False),
                "generated_at": time.time()
            }
            return self._save_plan(plan)
        except Exception as e:
            logger.error(f"解析 LLM JSON 失败: {e}")
            return self._save_fallback_plan(str(e))

    def _save_plan(self, plan: Dict) -> str:
        """保存计划并发送通知"""
        Path(self.output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_path, 'w') as f:
            json.dump(plan, f, indent=2)
        
        logger.info(f"✅ 作战计划已生成: {self.output_path} ({len(plan['targets'])} 个目标)")
        self._send_notification(plan)
        return self.output_path

    def _save_fallback_plan(self, reason: str) -> str:
        """兜底计划: 基于 WATCH_LIST_HK + 持仓生成可操作的白名单
        方向标记 ANY（不限制多空），仓位限制 3%（不确定性高时降仓）。
        """
        targets = []
        try:
            from app import config
            watchlist = getattr(config, 'WATCH_LIST_HK', [])
            for sym in watchlist:
                targets.append({
                    "symbol": sym,
                    "direction": "ANY",
                    "status": "VALID",
                    "urgency": "Low",
                    "key_trigger": f"Fallback白名单 (LLM不可用: {reason[:40]})",
                    "max_position_pct": 0.03
                })
        except Exception:
            pass

        plan = {
            "date": date.today().isoformat(),
            "targets": targets,
            "restricted": [],
            "overall_bias": "NEUTRAL",
            "market_regime": "UNKNOWN",
            "risk_level": "HIGH",
            "stop_trading": False,
            "reason": f"Fallback: {reason}",
            "generated_at": time.time()
        }
        return self._save_plan(plan)

    def _send_notification(self, plan: Dict):
        """发送钉钉通知 (HK) — 完整作战计划推送"""
        try:
            from app.risk.dingtalk_alert import init_dingtalk_alert
            from app import config

            sender = init_dingtalk_alert(
                webhook_url=config.DINGTALK_WEBHOOK_URL,
                secret=config.DINGTALK_SECRET
            )

            targets = plan.get("targets", [])
            restricted = plan.get("restricted", [])
            regime = plan.get("market_regime", "N/A")
            risk = plan.get("risk_level", "N/A")
            bias = plan.get("overall_bias", "N/A")
            stop = plan.get("stop_trading", False)
            reason = plan.get("reason", "")
            plan_date = plan.get("date", "")

            lines = [
                f"📊 **港股盘前作战计划**",
                f"📅 {plan_date}",
                f"",
                f"---",
                f"",
                f"## 🌍 市场环境",
                f"",
                f"- 市场状态: **{regime}**",
                f"- 整体倾向: **{bias}**",
                f"- 风险等级: **{risk}**",
                f"- 停止交易: **{'🔴 是' if stop else '🟢 否'}**",
            ]
            if reason:
                lines.append(f"- 生成方式: {reason[:80]}")
            lines.append("")

            # 作战目标表格
            if targets:
                lines.extend([
                    f"## 🎯 作战目标 ({len(targets)} 只)",
                    f"",
                    f"| 标的 | 方向 | 紧迫度 | 触发条件 | 仓位上限 |",
                    f"|------|------|--------|----------|----------|",
                ])
                for t in targets:
                    sym = t.get("symbol", "?").replace("HK.", "")
                    direction = t.get("direction", "ANY")
                    urgency = t.get("urgency", "—")
                    trigger = (t.get("key_trigger", "") or "")[:40]
                    max_pct = t.get("max_position_pct", 0.03)
                    urgency_emoji = "🔴" if urgency == "High" else "🟡" if urgency == "Medium" else "⚪"
                    lines.append(
                        f"| {sym} | {direction} | {urgency_emoji}{urgency} | {trigger} | {max_pct*100:.0f}% |"
                    )
                lines.append("")

            # 限制名单
            if restricted:
                lines.append(f"## 🚫 限制名单 ({len(restricted)} 只)")
                for r in restricted:
                    if isinstance(r, dict):
                        lines.append(f"- {r.get('symbol','?'): HK}{r.get('reason','')}")
                    else:
                        lines.append(f"- {r}")
                lines.append("")

            # 风险提醒
            if risk == "HIGH" or stop:
                lines.append("## ⚠️ 风险提醒")
                if stop:
                    lines.append("- 🔴 **已触发全面停止交易**，请检查市场状况")
                if risk == "HIGH":
                    lines.append("- 🔴 高风险环境，建议缩减仓位至1-2%")
                    lines.append("- 严格止损，禁止追高")
                lines.append("")

            if reason and "LLM" in reason:
                lines.append("> ⚠️ 本计划由降级逻辑生成 (LLM不可用)，方向均为 ANY，需交易员自行判断")
                lines.append("")

            # 辩论合成 (如有)
            ds = self._debate_synthesis
            if ds:
                lines.append("## 🧠 多Agent辩论摘要")
                lines.append("")
                lines.append(f"- 共识方向: **{ds.consensus_direction}** (conviction={ds.conviction:.2f})")
                lines.append(f"- 辩论质量: **{ds.debate_quality_score:.2f}**")
                lines.append(f"- 风险共识: **{ds.risk_consensus}**")
                if ds.disagreement_flags:
                    lines.append(f"- 核心分歧:")
                    for flag in ds.disagreement_flags[:3]:
                        lines.append(f"  - {flag}")
                if ds.merged_symbols:
                    lines.append(f"- 推荐标的:")
                    for s in ds.merged_symbols[:5]:
                        sym = s.get("symbol", "").replace("HK.", "")
                        sup = s.get("support", "")
                        conc = s.get("concern", "")
                        lines.append(f"  - {sym}: {sup}" + (f" (⚠️{conc})" if conc else ""))
                if ds.summary:
                    lines.append(f"")
                    lines.append(f"> {ds.summary[:200]}")
                lines.append("")

            lines.append(f"---")
            lines.append(f"*FTSeek 盘前作战计划 · 自动生成*")

            sender.send_text("\n".join(lines), alert_level="INFO")
            logger.info(f"[作战计划] 钉钉通知已推送 ({len(targets)}只标的)")
        except Exception as e:
            logger.error(f"发送盘前计划通知失败: {e}")
