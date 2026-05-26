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
            # 2026-05-17 修复: LLM 不再决定是否交易，而是提供上下文
            # 即使 direction=NO_TRADE，只要 signal_impact 有意义，也应传递 focus_symbols
            llm_direction = getattr(a, "direction", "NO_TRADE")
            signal_impact = getattr(a, "signal_impact", "neutral")
            
            # 只处理有交易价值或信号影响的评估
            if llm_direction == "NO_TRADE" and signal_impact not in ("boost", "block"):
                continue

            for sym in getattr(a, "focus_symbols", []):
                # 辩论过滤: 在回避名单中的标的 → 跳过
                if sym in avoid_codes:
                    continue

                # LLM signal_impact 注入
                if signal_impact == "block":
                    # LLM 建议回避 → 加入 restricted
                    if sym not in restricted:
                        restricted.append(sym)
                        logger.info(f"[BattlePlan] LLM 标记回避: {sym} ({getattr(a, 'second_order_insight', '')[:50]})")
                    continue

                # 辩论排序: 有辩论共识的标的使用辩论信心调整 urgency
                urgency = "High" if getattr(a, "confidence", 0) > 0.5 else "Medium"
                trigger = getattr(a, "recommendation", "")[:50]
                if signal_impact == "boost":
                    trigger = f"[LLM+signal] {getattr(a, 'second_order_insight', '')[:40]}"
                elif signal_impact == "weaken":
                    urgency = "Low"
                    trigger = f"[LLM⚠️weaken] {getattr(a, 'second_order_insight', '')[:40]}"

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
                    "direction": llm_direction if llm_direction != "NO_TRADE" else "ANY",
                    "status": "VALID",
                    "urgency": urgency,
                    "key_trigger": trigger,
                    "invalidation_reason": getattr(a, "invalidating_factors", [""])[0] if getattr(a, "invalidating_factors", []) else "",
                    "debate_net_confidence": debate_symbols.get(sym, {}).get('net_confidence', 0),
                    "signal_impact": signal_impact,  # 2026-05-17 新增
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

        # P0 修复 (2026-05-26): restricted-targets 交叉去重
        # 根因: restricted 从 avoid_codes/signal_impact=="block" 生成,
        #        targets 从 focus_symbols 生成, 两者独立导致 60% 标的被自我封锁
        # 审计: docs/hk_premarket_intraday_audit_20260526.md
        restricted_clean = set()
        for r in restricted:
            sym = r.split('（')[0].strip() if isinstance(r, str) and '（' in r else (r.strip() if isinstance(r, str) else str(r))
            restricted_clean.add(sym)
        _before_dedup = len(unique_targets)
        unique_targets = [t for t in unique_targets if t.get('symbol') not in restricted_clean]
        if _before_dedup != len(unique_targets):
            logger.warning(
                f"[BattlePlan] P0 去重: 移除 {(_before_dedup - len(unique_targets))} 只 "
                f"同时出现在 targets 和 restricted 中的标的"
            )

        # P0 修复 (2026-05-26): 为 targets 填充交易参数 (止损/止盈/仓位)
        # 根因: 原实现仅传递 symbol+direction, 缺乏 entry_zone/stop_loss/take_profit
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            _sl_pct = abs(_dem.get_stop_loss())
            _tp_pct = abs(_dem.get_take_profit())
        except Exception:
            _sl_pct, _tp_pct = 0.025, 0.08

        # P2 修复 (2026-05-26): 为缺少 entry_price 的标的尝试获取市场参考价
        _price_cache = {}
        for t in unique_targets:
            if not t.get('max_position_pct'):
                t['max_position_pct'] = 0.08
            # 尝试获取 entry_price (优先已有值, 其次 market_snapshot)
            ep = None
            if t.get('entry_price'):
                try:
                    ep = float(t['entry_price'])
                except (ValueError, TypeError):
                    pass
            if ep is None:
                sym = t.get('symbol', '')
                if sym and sym not in _price_cache:
                    try:
                        from app.execution.battle_plan_executor import BattlePlanExecutor
                        _prices = BattlePlanExecutor._get_yesterday_close_prices(None, [sym])
                        _price_cache[sym] = _prices.get(sym)
                    except Exception:
                        _price_cache[sym] = None
                ep = _price_cache.get(sym)
            if ep and not t.get('stop_loss'):
                try:
                    direction = t.get('direction', 'ANY').upper()
                    if direction in ('SHORT', 'SELL'):
                        t['stop_loss'] = round(ep * (1 + _sl_pct), 3)
                        t['take_profit'] = round(ep * (1 - _tp_pct), 3)
                    else:
                        t['stop_loss'] = round(ep * (1 - _sl_pct), 3)
                        t['take_profit'] = round(ep * (1 + _tp_pct), 3)
                    t['entry_price'] = ep  # 回填供后续引用
                except (ValueError, TypeError):
                    pass
            # 即使无法获取价格，也记录 SL/TP 百分比供执行器参考
            if not t.get('stop_loss'):
                t['stop_loss_pct'] = round(_sl_pct, 4)
                t['take_profit_pct'] = round(_tp_pct, 4)

        # 辩论驱动的 market_regime 设置
        if debate_direction == "BUY" and debate_conviction > 0.6:
            max_risk = "LOW"
        elif debate_direction in ("SELL", "CAUTIOUS_BEARISH"):
            max_risk = "HIGH"

        # P2 优化 (2026-05-17): LLM signal_impact 分布统计
        signal_dist = {"boost": 0, "neutral": 0, "weaken": 0, "block": 0}
        for t in unique_targets:
            impact = t.get("signal_impact", "neutral")
            if impact in signal_dist:
                signal_dist[impact] += 1

        # P2 优化 (2026-05-17): 交易日历状态
        calendar_status = self._get_calendar_status()

        # P2 优化 (2026-05-17): 关键假设 (从 assessments 提取 invalidating_factors)
        key_assumptions = self._extract_key_assumptions(assessments)

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
                "quality": debate_conviction if self._debate_synthesis else None,
                "risk_consensus": getattr(self._debate_synthesis, 'risk_consensus', 'N/A') if self._debate_synthesis else 'N/A',
            },
            # 新增区块
            "signal_distribution": signal_dist,
            "calendar_status": calendar_status,
            "key_assumptions": key_assumptions,
            "system_health": self._get_system_health(),
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
                    "invalidation_reason": p.get("invalidation", ""),
                    "signal_impact": "neutral",  # 旧格式无 signal_impact，默认 neutral
                })
            
            plan = {
                "date": date.today().isoformat(),
                "targets": targets,
                "restricted": raw_json.get("restricted", []),
                "overall_bias": raw_json.get("market_regime", "NEUTRAL"),
                "market_regime": raw_json.get("market_regime", "NEUTRAL"),
                "risk_level": raw_json.get("risk_level", "MEDIUM"),
                "stop_trading": raw_json.get("stop_trading", False),
                "debate_consensus": {
                    "direction": "NEUTRAL",
                    "conviction": 0.0,
                    "ranked_count": 0,
                    "avoid_count": 0,
                    "quality": None,
                    "risk_consensus": "N/A",
                },
                # P2 优化 (2026-05-17): 新增区块
                "signal_distribution": {"boost": 0, "neutral": len(targets), "weaken": 0, "block": 0},
                "calendar_status": self._get_calendar_status(),
                "key_assumptions": [],
                "system_health": self._get_system_health(),
                "generated_at": time.time()
            }
            return self._save_plan(plan)
        except Exception as e:
            logger.error(f"解析 LLM JSON 失败: {e}")
            return self._save_fallback_plan(str(e))

    def merge_debate_targets(self, existing_targets: List[Dict], existing_restricted: List[str] = None,
                             assessments: List[Any] = None) -> Dict:
        """从辩论 consensus_ranking 提取不在 assessments 中的标的，合并到现有计划

        解决根因: generate_from_assessments() 只遍历 assessments.focus_symbols,
        辩论新增的标的如果不在 assessments 中会丢失。

        Args:
            existing_targets: 已由 generate_from_assessments() 生成的 targets
            existing_restricted: 已有的 restricted 名单
            assessments: LLM PreMarketAssessment 列表，用于交叉引用 signal_impact

        Returns:
            {added_count: int, new_targets: List[Dict], restricted: List[str]}
        """
        if not self._debate_synthesis:
            logger.info("[BattlePlan] 无辩论合成数据，跳过标的合并")
            return {"added_count": 0, "new_targets": [], "restricted": existing_restricted or []}

        ds = self._debate_synthesis
        avoid_codes = set(getattr(ds, 'avoid_codes', []))
        ranking = getattr(ds, 'consensus_ranking', [])

        if not ranking:
            logger.info("[BattlePlan] 辩论无 consensus_ranking，跳过标的合并")
            return {"added_count": 0, "new_targets": [], "restricted": existing_restricted or []}

        # 构建 LLM signal_impact 查找表 (从 assessments 中提取)
        llm_impact_map = {}
        if assessments:
            for a in assessments:
                syms = getattr(a, "focus_symbols", [])
                impact = getattr(a, "signal_impact", "neutral")
                for s in syms:
                    llm_impact_map[s] = impact

        # 已存在的标的集合 (不重复添加)
        existing_symbols = {t.get('symbol', '') for t in existing_targets}
        restricted_set = set(existing_restricted or [])

        new_targets = []
        for item in ranking:
            sym = item.get('code', '')
            if not sym:
                continue
            # 跳过: 已在 assessments 中、在 avoid 名单中、或在 restricted 中
            if sym in existing_symbols or sym in avoid_codes or sym in restricted_set:
                continue

            net_conf = item.get('net_confidence', 0)

            # P2 修复 (2026-05-26): 辩论信心度门控 — 强烈看空标的不进白名单
            # 根因: 辩论 net_confidence < -0.3 意味着多模型共识不推荐交易
            # 将此类标的加入 restricted 而非 targets，直接提升胜率
            if net_conf < -0.3:
                restricted_set.add(sym)
                logger.info(
                    f"[BattlePlan] 辩论标的 {sym} 信心度过低 "
                    f"(net_conf={net_conf:+.2f} < -0.3) → 加入 restricted"
                )
                continue

            urgency = "High" if net_conf > 0.3 else "Medium" if net_conf > 0 else "Low"

            # P0 修复 (2026-05-17): 从 LLM assessments 交叉引用 signal_impact
            # 辩论标的如果也有 LLM 分析，应继承其 signal_impact 而非默认 neutral
            signal_impact = llm_impact_map.get(sym, "neutral")
            if signal_impact == "block":
                # LLM 建议回避 → 加入 restricted 而非 target
                restricted_set.add(sym)
                logger.info(f"[BattlePlan] 辩论标的 {sym} LLM标记回避 → 加入 restricted")
                continue

            if signal_impact == "boost":
                urgency = "High"  # LLM boost 提升 urgency
            elif signal_impact == "weaken":
                urgency = "Low"   # LLM weaken 降低 urgency

            new_targets.append({
                "symbol": sym,
                "direction": "ANY",  # 辩论不指定方向，由盘中信号决定
                "status": "VALID",
                "urgency": urgency,
                "key_trigger": f"[辩论+] {item.get('reason_bull', '')[:40]}",
                "invalidation_reason": "",
                "debate_net_confidence": net_conf,
                "signal_impact": signal_impact,  # 交叉引用 LLM signal_impact
                "max_position_pct": 0.08,  # P2 修复 (2026-05-26): 辩论标的默认仓位
            })

        # 按辩论净信心排序
        new_targets.sort(key=lambda t: t.get('debate_net_confidence', 0), reverse=True)

        logger.info(f"[BattlePlan] 辩论标的合并: {len(new_targets)} 只新增, "
                    f"{len(restricted_set) - len(existing_restricted or [])} 只加入 restricted")
        return {"added_count": len(new_targets), "new_targets": new_targets,
                "restricted": list(restricted_set)}

    # ── P2 新增辅助方法 (2026-05-17) ────────────────────────────

    def _get_calendar_status(self) -> Dict:
        """获取今日交易日历状态"""
        try:
            from datetime import date as _date
            from app.utils.holiday_calendar import get_holiday_calendar
            cal = get_holiday_calendar()
            today = _date.today()
            is_half = cal.is_hk_half_day(today)
            # 获取半日市下午收盘时间 (如果有)
            half_day_close = "12:00" if is_half else None
            return {
                "is_trading_day": cal.is_trading_day("HK", today),
                "is_half_day": is_half,
                "half_day_close": half_day_close,
            }
        except Exception as e:
            return {"is_trading_day": True, "is_half_day": False, "half_day_close": None, "error": str(e)}

    def _extract_key_assumptions(self, assessments: List[Any]) -> List[str]:
        """从 LLM assessments 提取关键假设 (invalidating_factors)"""
        assumptions = []
        seen = set()
        for a in assessments:
            factors = getattr(a, "invalidating_factors", []) or []
            for f in factors:
                if f and f not in seen and len(f) > 5:
                    seen.add(f)
                    assumptions.append(f)
        # 最多取 5 个
        return assumptions[:5]

    def _get_system_health(self) -> Dict:
        """获取当前系统健康状态"""
        health = {
            "scheduler_alive": None,
            "circuit_breaker_active": True,
            "calendar_valid": True,
        }
        try:
            from app.pipeline.premarket_scheduler import PreMarketScheduler
            # 检查 scheduler 心跳
            scheduler = getattr(self, '_scheduler', None)
            if scheduler:
                status = scheduler.get_status()
                health["scheduler_alive"] = status.get("alive", False)
            else:
                health["scheduler_alive"] = None  # 未初始化
        except Exception:
            health["scheduler_alive"] = None

        try:
            from app.risk.circuit_breaker import circuit_breaker
            health["circuit_breaker_active"] = circuit_breaker.can_trade()
        except Exception:
            health["circuit_breaker_active"] = None

        try:
            from app.utils.holiday_calendar import get_holiday_calendar
            cal = get_holiday_calendar()
            health["calendar_valid"] = not getattr(cal, '_calendar_expired', False)
        except Exception:
            health["calendar_valid"] = None

        return health

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
                    "max_position_pct": 0.03,
                    "signal_impact": "neutral",  # 兜底标的无 LLM signal_impact
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
        """发送钉钉通知 (HK) — 条件分段渲染，跟随 plan 结构自动调整"""
        try:
            from app.risk.dingtalk_alert import init_dingtalk_alert
            from app import config

            sender = init_dingtalk_alert(
                webhook_url=config.DINGTALK_WEBHOOK_URL,
                secret=config.DINGTALK_SECRET
            )

            lines = []
            targets = plan.get("targets", [])
            plan_date = plan.get("date", "")

            # ═══ 标题 ═══
            lines.append(f"📊 **港股盘前作战计划**")
            lines.append(f"📅 {plan_date}")
            lines.append("")
            lines.append("---")
            lines.append("")

            # ═══ Section 1: 市场环境 (条件渲染) ═══
            env_lines = self._render_section_market_env(plan)
            if env_lines:
                lines.extend(env_lines)
                lines.append("")

            # ═══ Section 2: 辩论摘要 (条件渲染) ═══
            debate_lines = self._render_section_debate(plan)
            if debate_lines:
                lines.extend(debate_lines)
                lines.append("")

            # ═══ Section 3: LLM 信号分布 (如有) ═══
            sig_lines = self._render_section_signals(plan)
            if sig_lines:
                lines.extend(sig_lines)
                lines.append("")

            # ═══ Section 4: 日历状态 (如有) ═══
            cal_lines = self._render_section_calendar(plan)
            if cal_lines:
                lines.extend(cal_lines)
                lines.append("")

            # ═══ Section 5: 系统健康 (如有) ═══
            health_lines = self._render_section_health(plan)
            if health_lines:
                lines.extend(health_lines)
                lines.append("")

            # ═══ Section 6: 关键假设 (如有) ═══
            assumption_lines = self._render_section_assumptions(plan)
            if assumption_lines:
                lines.extend(assumption_lines)
                lines.append("")

            # ═══ Section 7: 作战目标 ═══
            if targets:
                lines.extend(self._render_section_targets(targets))
                lines.append("")

            # ═══ Section 8: 限制名单 (如有) ═══
            restricted = plan.get("restricted", [])
            if restricted:
                lines.extend(self._render_section_restricted(restricted))
                lines.append("")

            # ═══ Section 9: 风险提醒 (条件触发) ═══
            risk_lines = self._render_section_risk(plan)
            if risk_lines:
                lines.extend(risk_lines)
                lines.append("")

            lines.append("---")
            lines.append("*FTSeek 盘前作战计划 · 条件分段推送*")

            sender.send_text("\n".join(lines), alert_level="INFO")
            logger.info(f"[作战计划] 钉钉通知已推送 ({len(targets)}只标的)")
        except Exception as e:
            logger.error(f"发送盘前计划通知失败: {e}")

    # ═══════════════════════════════════════════════
    #  条件分段 Section 渲染器
    #  新增字段需在对应 _render_section_* 中添加渲染逻辑
    # ═══════════════════════════════════════════════

    def _render_section_market_env(self, plan: Dict) -> List[str]:
        """市场环境 — 自动聚合所有顶层简单字段"""
        lines = ["## 🌍 市场环境", ""]

        # 核心字段 (始终显示)
        core = [
            ("market_regime", "市场状态"),
            ("overall_bias", "整体倾向"),
            ("risk_level", "风险等级"),
        ]
        values = []
        for key, label in core:
            val = plan.get(key)
            if val is not None:
                values.append(f"**{val}**")
            else:
                values.append("—")
        lines.append(f"- 市场状态: {values[0]} | 整体倾向: {values[1]} | 风险等级: {values[2]}")

        # 条件字段 (有则显示)
        stop = plan.get("stop_trading")
        if stop is not None:
            lines.append(f"- 停止交易: **{'🔴 是' if stop else '🟢 否'}**")

        reason = plan.get("reason")
        if reason:
            lines.append(f"- 生成方式: {reason[:80]}")

        return lines

    def _render_section_debate(self, plan: Dict) -> List[str]:
        """辩论摘要 — 自动渲染 debate_consensus 下的所有字段"""
        dc = plan.get("debate_consensus")
        if not dc:
            return []

        lines = ["## 🧠 多Agent辩论摘要", ""]

        # 核心信息 (始终显示)
        direction = dc.get("direction", "N/A")
        conviction = dc.get("conviction", 0)
        lines.append(f"- 共识方向: **{direction}** (conviction={conviction:.2f})")

        # 扩展字段 (有则显示)
        quality = dc.get("quality")
        if quality is not None:
            lines.append(f"- 辩论质量: **{quality:.2f}**")

        risk_consensus = dc.get("risk_consensus")
        if risk_consensus and risk_consensus != "N/A":
            lines.append(f"- 风险共识: **{risk_consensus}**")

        ranked = dc.get("ranked_count")
        if ranked is not None:
            lines.append(f"- 排名标的: {ranked} 只")

        avoid = dc.get("avoid_count")
        if avoid is not None and avoid > 0:
            lines.append(f"- 回避标的: {avoid} 只")

        return lines

    def _render_section_signals(self, plan: Dict) -> List[str]:
        """LLM 信号分布 — 自动渲染 signal_distribution"""
        sig = plan.get("signal_distribution")
        if not sig:
            return []

        lines = ["## 🤖 LLM 信号分布", ""]
        emoji_map = {"block": "🔴", "weaken": "🟡", "neutral": "⚪", "boost": "🟢"}
        parts = []
        for impact, count in sig.items():
            emoji = emoji_map.get(impact, "❓")
            parts.append(f"{emoji} {impact}: {count}")
        lines.append(f"- {' | '.join(parts)}")
        return lines

    def _render_section_calendar(self, plan: Dict) -> List[str]:
        """交易日历 — 自动渲染 calendar_status"""
        cal = plan.get("calendar_status")
        if not cal:
            return []

        lines = ["## 📅 交易日历", ""]
        is_trading = cal.get("is_trading_day")
        is_half = cal.get("is_half_day")
        half_close = cal.get("half_day_close")

        if is_trading is not None:
            if not is_trading:
                lines.append("- 状态: **🔴 非交易日**")
            elif is_half:
                lines.append(f"- 状态: **🟡 半日市** (下午 {half_close or '12:00'} 休市)")
            else:
                lines.append("- 状态: **🟢 正常交易日**")
        return lines

    def _render_section_health(self, plan: Dict) -> List[str]:
        """系统健康 — 自动渲染 system_health"""
        health = plan.get("system_health")
        if not health:
            return []

        lines = ["## 💚 系统健康", ""]
        items = []

        sched = health.get("scheduler_alive")
        if sched is not None:
            items.append(f"调度 {'✅' if sched else '❌'}")

        cb = health.get("circuit_breaker_active")
        if cb is not None:
            items.append(f"熔断 {'✅' if cb else '❌'}")

        cal_v = health.get("calendar_valid")
        if cal_v is not None:
            items.append(f"日历 {'✅' if cal_v else '❌'}")

        if items:
            lines.append(f"- {' | '.join(items)}")
        return lines

    def _render_section_assumptions(self, plan: Dict) -> List[str]:
        """关键假设 — 自动渲染 key_assumptions"""
        assumptions = plan.get("key_assumptions", [])
        if not assumptions:
            return []

        lines = ["## ⚠️ 关键假设", ""]
        for i, a in enumerate(assumptions[:5], 1):
            lines.append(f"{i}. {a[:80]}")
        return lines

    def _render_section_targets(self, targets: List[Dict]) -> List[str]:
        """作战目标 — 条件分段列 (根据 target 中实际存在的字段)"""
        if not targets:
            return []

        lines = [
            f"## 🎯 作战目标 ({len(targets)} 只)",
            "",
        ]

        # 动态检测列: 根据 target 实际字段调整表格列
        has_signal_impact = any("signal_impact" in t for t in targets)

        if has_signal_impact:
            lines.append("| 标的 | 方向 | 紧迫度 | signal_impact | 触发条件 |")
            lines.append("|------|------|--------|---------------|----------|")
        else:
            lines.append("| 标的 | 方向 | 紧迫度 | 触发条件 |")
            lines.append("|------|------|--------|----------|")

        for t in targets:
            sym = t.get("symbol", "?").replace("HK.", "")
            direction = t.get("direction", "ANY")
            urgency = t.get("urgency", "—")
            trigger = (t.get("key_trigger", "") or "")[:40]
            urgency_emoji = "🔴" if urgency == "High" else "🟡" if urgency == "Medium" else "⚪"

            if has_signal_impact:
                impact = t.get("signal_impact", "—")
                impact_emoji = {"boost": "🟢", "weaken": "🟡", "block": "🔴", "neutral": "⚪"}.get(impact, "—")
                lines.append(f"| {sym} | {direction} | {urgency_emoji}{urgency} | {impact_emoji}{impact} | {trigger} |")
            else:
                lines.append(f"| {sym} | {direction} | {urgency_emoji}{urgency} | {trigger} |")

        return lines

    def _render_section_restricted(self, restricted: List) -> List[str]:
        """限制名单"""
        if not restricted:
            return []
        lines = [f"## 🚫 限制名单 ({len(restricted)} 只)"]
        for r in restricted:
            if isinstance(r, dict):
                lines.append(f"- {r.get('symbol', '?')}: {r.get('reason', '')}")
            else:
                lines.append(f"- {r}")
        return lines

    def _render_section_risk(self, plan: Dict) -> List[str]:
        """风险提醒 — 条件触发"""
        risk = plan.get("risk_level", "")
        stop = plan.get("stop_trading", False)
        if risk != "HIGH" and not stop:
            return []

        lines = ["## ⚠️ 风险提醒"]
        if stop:
            lines.append("- 🔴 **已触发全面停止交易**，请检查市场状况")
        if risk == "HIGH":
            lines.append("- 🔴 高风险环境，建议缩减仓位至1-2%")
            lines.append("- 严格止损，禁止追高")
        return lines
