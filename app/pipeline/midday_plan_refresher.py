"""
午间作战计划刷新器 — 12:00 HKT 触发，纯规则驱动，无 LLM 依赖。

流程:
  1. 收集午间增量信息 (A股收盘 / 南向资金 / 快讯 / 量价异动)
  2. 规则筛选候选标的 (量比>3x / 南向集中买入 / 持仓异动)
  3. 合并到现有计划 → 写入 daily_battle_plan.json
  4. 热重载 BattlePlanManager
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.pipeline.midday_refresher")

PLAN_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "daily_battle_plan.json"

# 午间新增门槛 (比盘前更严格，防止午间追高)
MIDDAY_MIN_VOLUME_RATIO = 3.0       # 量比 > 3x
MIDDAY_MIN_PRICE_CHANGE = 0.02      # 涨幅 > 2% (防噪音)
MIDDAY_MAX_POSITION_PCT = 0.03      # 午间新增仓位上限 3%
MIDDAY_MAX_NEW_TARGETS = 5          # 午间最多新增 5 只


class MiddayPlanRefresher:

    def __init__(self, subscriber=None, quote_ctx=None):
        self._subscriber = subscriber
        self._quote_ctx = quote_ctx

    def run(self, trade_date: Optional[str] = None) -> Dict:
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        logger.info("[午间刷新] === 作战计划午间刷新开始 ===")
        result = {"status": "skipped", "added": 0, "removed": 0, "errors": []}

        plan = self._load_plan()
        if not plan:
            result["errors"].append("无法加载现有计划")
            return result

        existing_symbols = {t.get("symbol") for t in plan.get("targets", [])}

        # Step 1: 收集午间信息
        candidates = []
        try:
            candidates.extend(self._scan_volume_leaders(existing_symbols))
        except Exception as e:
            result["errors"].append(f"量比扫描: {e}")

        try:
            candidates.extend(self._scan_southbound_focus())
        except Exception as e:
            result["errors"].append(f"南向资金: {e}")

        try:
            candidates.extend(self._scan_position_drift())
        except Exception as e:
            result["errors"].append(f"持仓异动: {e}")

        # Step 2: 午间多Agent辩论 (LLM — 非阻塞, 失败降级)
        debate_synthesis = None
        try:
            ashare_data = self._log_ashare_context()
            debate_ctx = self._build_debate_context(
                ashare_data=ashare_data,
                volume_leaders=candidates,
            )
            debate_synthesis = self._run_midday_debate(debate_ctx)
        except Exception as e:
            logger.warning(f"[午间刷新] 辩论阶段异常 (降级): {e}")

        # Step 3: 辩论结果过滤候选 (仅在有辩论结果时)
        # P0 修复 (2026-05-17): 预加载盘前 LLM signal_impact，午间候选继承历史判断
        impact_map = self._lookup_premarket_signal_impact(plan)
        if impact_map:
            logger.info(f"[午间刷新] 盘前 LLM signal_impact 映射: {len(impact_map)} 只 (非neutral)")
        if debate_synthesis and debate_synthesis.merged_symbols:
            debate_symbols = {s.get("symbol", "") for s in debate_synthesis.merged_symbols}
            # 将辩论推荐中不在候选列表里的标的也加入
            for sym in debate_symbols:
                if sym.startswith("HK.") and sym not in {c["symbol"] for c in candidates}:
                    direction = "LONG"
                    for s in debate_synthesis.merged_symbols:
                        if s.get("symbol") == sym:
                            direction = s.get("direction", "LONG").upper()
                            break
                    candidates.append({
                        "symbol": sym,
                        "direction": direction,
                        "status": "VALID",
                        "urgency": "Medium",
                        "key_trigger": f"午间辩论推荐 (共识={debate_synthesis.consensus_direction})",
                        "max_position_pct": MIDDAY_MAX_POSITION_PCT,
                        "source": "midday_debate",
                        "signal_impact": impact_map.get(sym, "neutral"),  # P0: 继承盘前 LLM 判断
                    })
            logger.info(f"[午间刷新] 辩论注入 {len(debate_symbols)} 只标的")

        # Step 4: 去重 + 过滤已在计划中的
        new_targets = []
        seen = set(existing_symbols)
        for c in candidates:
            sym = c["symbol"]
            if sym in seen or not sym.startswith("HK."):
                continue
            seen.add(sym)
            # P0 修复 (2026-05-17): 午间候选继承盘前 LLM signal_impact
            _sym_key = sym
            if not _sym_key.startswith("HK."):
                _sym_key = f"HK.{_sym_key}"
            if "signal_impact" not in c or c["signal_impact"] == "neutral":
                # 尝试匹配 HK. 前缀
                c["signal_impact"] = impact_map.get(_sym_key, impact_map.get(sym, "neutral"))
            new_targets.append(c)
            if len(new_targets) >= MIDDAY_MAX_NEW_TARGETS:
                break

        if not new_targets:
            logger.info("[午间刷新] 无符合条件的午间新增标的")
            result["status"] = "no_candidates"
            ashare_data = self._log_ashare_context()
            try:
                self._send_midday_status(plan, ashare_data, result, debate_synthesis)
            except Exception:
                pass
            return result

        # Step 3: 合并到计划
        for t in new_targets:
            plan["targets"].append(t)
        plan["plan_version"] = plan.get("plan_version", 1) + 1
        plan["midday_refreshed_at"] = time.time()
        plan["midday_added"] = [t["symbol"] for t in new_targets]

        self._save_plan(plan)
        logger.info(
            f"[午间刷新] 新增 {len(new_targets)} 只标的: "
            f"{[t['symbol'] for t in new_targets]}"
        )

        # Step 4: 热重载
        try:
            from app.pipeline.battle_plan import BattlePlanManager
            BattlePlanManager().initialize(str(PLAN_PATH), enabled=True)
            logger.info("[午间刷新] BattlePlanManager 已热重载")
        except Exception as e:
            result["errors"].append(f"热重载失败: {e}")

        # Step 5: 同步到动态池
        try:
            from app.utils.daily_active_pool import get_daily_active_pool
            pool = get_daily_active_pool(market_prefix="HK.")
            for t in new_targets:
                pool.add_plan_target(
                    code=t["symbol"],
                    direction=t.get("direction", "ANY"),
                    logic=t.get("key_trigger", ""),
                    max_position_pct=t.get("max_position_pct", MIDDAY_MAX_POSITION_PCT),
                    signal_impact=t.get("signal_impact", "neutral"),
                    urgency=t.get("urgency", "Medium"),
                )
            logger.info(f"[午间刷新] 动态池已同步 {len(new_targets)} 只")
        except Exception as e:
            result["errors"].append(f"动态池同步: {e}")

        result["status"] = "refreshed"
        result["added"] = len(new_targets)
        ashare_data = self._log_ashare_context()

        # Step 6: 推送通知 (含辩论摘要)
        try:
            self._send_midday_update(plan, new_targets, ashare_data, result, debate_synthesis)
        except Exception as e:
            result["errors"].append(f"通知推送: {e}")

        logger.info(f"[午间刷新] === 完成: 新增 {len(new_targets)} 只 ===")

        # 领域事件 — 午间计划刷新 (含辩论合成)
        from app.core.domain_events import PlanMiddayRefreshedPayload, get_event_bus, DomainEvent, EventType
        payload = PlanMiddayRefreshedPayload(
            date=trade_date,
            added_count=result["added"],
            total_count=len(plan.get("targets", [])),
            new_targets=[t["symbol"] for t in new_targets],
            ashare_context=ashare_data,
        )
        # 将辩论合成注入 ashare_context 的扩展字段
        if debate_synthesis:
            debate_info = {
                "debate_consensus": debate_synthesis.consensus_direction,
                "debate_conviction": debate_synthesis.conviction,
                "debate_quality": debate_synthesis.debate_quality_score,
                "debate_risk": debate_synthesis.risk_consensus,
                "debate_summary": debate_synthesis.summary[:300] if debate_synthesis.summary else "",
            }
            payload.ashare_context.append(debate_info)

        get_event_bus().emit(DomainEvent(
            type=EventType.PLAN_MIDDAY_REFRESHED,
            data=payload,
        ))

        # Step 7: 保存午间更新到 feedback relay (连接 EOD 复盘)
        try:
            from app.review.hk_feedback_relay import save_midday_update
            save_midday_update(
                new_targets=new_targets,
                market_context={
                    "debate_consensus": debate_synthesis.consensus_direction if debate_synthesis else None,
                    "debate_conviction": debate_synthesis.conviction if debate_synthesis else None,
                },
                a_share_close=ashare_data,
            )
            logger.info(f"[午间刷新] Step 7: 午间更新已保存到 feedback relay")
        except Exception as _relay_err:
            logger.warning(f"[午间刷新] Step 7 反馈保存失败 (非阻塞): {_relay_err}")

        logger.info("[午间刷新] 领域事件已发射 (PLAN_MIDDAY_REFRESHED)")

        return result

    # ─── 数据收集 ───────────────────────────────────────────

    def _scan_volume_leaders(self, existing: set) -> List[Dict]:
        """扫描量比 > 3x 的 watchlist 标的"""
        results = []
        try:
            from app import config
            watchlist = getattr(config, 'WATCH_LIST_HK', [])
        except Exception:
            return results

        if not self._quote_ctx:
            return results

        for code in watchlist:
            if code in existing:
                continue
            try:
                ret, data = self._quote_ctx.get_market_snapshot([code])
                if ret != 0 or data is None or data.empty:
                    continue
                row = data.iloc[0]
                vol_ratio = float(row.get("volume_ratio", 0) or 0)
                change_pct = float(row.get("change_rate", 0) or 0) / 100.0

                if vol_ratio >= MIDDAY_MIN_VOLUME_RATIO and abs(change_pct) >= MIDDAY_MIN_PRICE_CHANGE:
                    direction = "LONG" if change_pct > 0 else "SHORT"
                    results.append({
                        "symbol": code,
                        "direction": direction,
                        "status": "VALID",
                        "urgency": "High",
                        "key_trigger": f"午间量比{vol_ratio:.1f}x 涨跌{change_pct:+.1%}",
                        "max_position_pct": MIDDAY_MAX_POSITION_PCT,
                        "source": "midday_volume",
                    })
                    logger.info(f"[午间量比] {code} vol_ratio={vol_ratio:.1f}x change={change_pct:+.1%}")
            except Exception:
                continue

        return results

    def _scan_southbound_focus(self) -> List[Dict]:
        """扫描南向资金集中买入标的 (从快讯或 DB 获取)"""
        results = []
        today = datetime.now().strftime("%Y-%m-%d")
        db_path = os.environ.get("FTSEEK_DB_PATH", "data/ftseek.db")

        try:
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                # 从 trade_evaluations 或 news 表查找南向资金相关记录
                cursor = conn.execute(
                    "SELECT DISTINCT code FROM trade_evaluations "
                    "WHERE evaluated_at >= ? AND evaluated_at < ? "
                    "AND (notes LIKE '%南向%' OR notes LIKE '%港股通%' OR notes LIKE '%southbound%')",
                    (f"{today} 09:30:00", f"{today} 12:00:00"),
                )
                rows = cursor.fetchall()

            for r in rows:
                code = r["code"] if isinstance(r, dict) else r[0]
                if code and code.startswith("HK."):
                    results.append({
                        "symbol": code,
                        "direction": "LONG",
                        "status": "VALID",
                        "urgency": "Medium",
                        "key_trigger": "午间: 南向资金关注",
                        "max_position_pct": MIDDAY_MAX_POSITION_PCT,
                        "source": "midday_southbound",
                    })
        except Exception:
            pass

        return results

    def _scan_position_drift(self) -> List[Dict]:
        """扫描持仓异动: 浮亏超 -5% → 加入重点关注"""
        results = []
        if not self._subscriber:
            return results

        try:
            positions = getattr(self._subscriber.position_manager, 'get_all_positions', None)
            if not positions:
                return results

            for pos in positions():
                if not pos.code.startswith("HK."):
                    continue
                pnl_pct = getattr(pos, 'unrealized_pnl_pct', 0)
                if pnl_pct < -0.05:
                    results.append({
                        "symbol": pos.code,
                        "direction": "LONG" if getattr(pos, 'action', 'BUY') == 'BUY' else "SHORT",
                        "status": "CAUTION",
                        "urgency": "High",
                        "key_trigger": f"午间: 持仓浮亏{pnl_pct:.1%}, 重点关注",
                        "max_position_pct": MIDDAY_MAX_POSITION_PCT,
                        "source": "midday_position_drift",
                    })
        except Exception:
            pass

        return results

    def _log_ashare_context(self) -> List[Dict]:
        """记录 A 股上午收盘数据, 返回结构化数据供通知使用"""
        results = []
        if not self._quote_ctx:
            return results
        ashare_indices = ["SH.000001", "SH.000300", "SZ.399006"]
        try:
            ret, data = self._quote_ctx.get_market_snapshot(ashare_indices)
            if ret != 0 or data is None or data.empty:
                return results
            name_map = {"SH.000001": "上证", "SH.000300": "沪深300", "SZ.399006": "创业板"}
            for _, row in data.iterrows():
                code = row.get("code", "")
                change = float(row.get("change_rate", 0) or 0) / 100.0
                last = float(row.get("last_price", 0))
                name = name_map.get(code, code)
                logger.info(f"[午间A股] {name}: {change:+.2%}")
                results.append({"name": name, "code": code, "price": last, "change_pct": change})
        except Exception:
            pass
        return results

    # ─── 午间辩论 ───────────────────────────────────────────

    def _gather_morning_price_data(self) -> str:
        """收集上午量价数据 — 为辩论Agent提供技术视角"""
        lines = []
        if not self._quote_ctx:
            return ""

        try:
            from app import config
            watchlist = getattr(config, 'WATCH_LIST_HK', [])[:15]
        except Exception:
            watchlist = []

        if not watchlist:
            return ""

        try:
            ret, data = self._quote_ctx.get_market_snapshot(watchlist)
            if ret != 0 or data is None or data.empty:
                return ""
            for _, row in data.iterrows():
                code = row.get("code", "")
                last = float(row.get("last_price", 0))
                change = float(row.get("change_rate", 0) or 0) / 100.0
                vol_ratio = float(row.get("volume_ratio", 0) or 0)
                turnover = float(row.get("turnover", 0) or 0)
                amplitude = float(row.get("amplitude", 0) or 0) / 100.0 if row.get("amplitude") else 0
                lines.append(
                    f"{code}: {last:.2f} ({change:+.2%}) "
                    f"量比={vol_ratio:.1f}x 换手={turnover:.2%} 振幅={amplitude:.2%}"
                )
        except Exception as e:
            logger.warning(f"[午间辩论] 量价数据收集失败: {e}")

        return "\n".join(lines[:30])

    def _gather_morning_news(self) -> str:
        """收集上午重要新闻摘要"""
        today = datetime.now().strftime("%Y-%m-%d")
        db_path = os.environ.get("FTSEEK_DB_PATH", "data/ftseek.db")
        lines = []

        try:
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT DISTINCT title, summary FROM news_cache "
                    "WHERE fetched_at >= ? AND fetched_at < ? "
                    "ORDER BY fetched_at DESC LIMIT 10",
                    (f"{today} 08:00:00", f"{today} 12:00:00"),
                )
                rows = cursor.fetchall()
            for r in rows:
                title = r["title"] if isinstance(r, dict) else r[0]
                summary = (r["summary"] if isinstance(r, dict) else r[1]) or ""
                lines.append(f"- {title}" + (f": {summary[:80]}" if summary else ""))
        except Exception:
            pass

        # Fallback: 从新闻缓存文件收集
        if not lines:
            try:
                import glob
                cache_dir = Path(__file__).resolve().parent.parent.parent / "data" / "news_cache"
                for f in sorted(glob.glob(str(cache_dir / "*.json"))):
                    try:
                        with open(f) as fh:
                            item = json.load(fh)
                            title = item.get("title", "")
                            if title:
                                lines.append(f"- {title[:120]}")
                    except Exception:
                        pass
            except Exception:
                pass

        return "\n".join(lines[:10]) if lines else "上午无重要新闻"

    def _gather_morning_trade_summary(self) -> str:
        """收集上午交易摘要 — 作为反馈上下文"""
        today = datetime.now().strftime("%Y-%m-%d")
        db_path = os.environ.get("FTSEEK_DB_PATH", "data/ftseek.db")

        try:
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT code, action, entry_time, exit_time, pnl, pnl_percent, exit_reason "
                    "FROM trades WHERE code LIKE 'HK.%' "
                    "AND entry_time >= ? AND entry_time < ? "
                    "ORDER BY entry_time ASC",
                    (f"{today} 09:30:00", f"{today} 12:00:00"),
                )
                rows = cursor.fetchall()

            if not rows:
                return "上午无交易"

            trades = [dict(r) for r in rows]
            wins = sum(1 for t in trades if (t.get("pnl", 0) or 0) > 0)
            losses = sum(1 for t in trades if (t.get("pnl", 0) or 0) < 0)
            total_pnl = sum(t.get("pnl", 0) or 0 for t in trades)

            lines = [
                f"上午交易: {len(trades)}笔 | 盈利{wins} | 亏损{losses} | 净盈亏{total_pnl:+.2f}",
            ]
            for t in trades:
                pnl = t.get("pnl", 0) or 0
                pnl_pct = t.get("pnl_percent", 0) or 0
                reason = t.get("exit_reason", "") or ""
                lines.append(
                    f"  {t['code']} {t.get('action','?')} PnL={pnl:+.2f} ({pnl_pct:+.2%})"
                    + (f" [{reason}]" if reason else "")
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[午间辩论] 交易摘要收集失败: {e}")
            return "上午交易数据不可用"

    def _build_debate_context(self, ashare_data: List[Dict], volume_leaders: List[Dict]) -> Dict:
        """构建午间辩论所需的完整上下文"""
        # 市场环境: A股上午收盘 + 南向资金 + 大盘
        market_parts = []

        if ashare_data:
            market_parts.append("【A股上午收盘】")
            for a in ashare_data:
                market_parts.append(f"  {a['name']}: {a['price']:.2f} ({a['change_pct']:+.2%})")

        # 港股大盘快照
        if self._quote_ctx:
            try:
                hk_indices = ["HK.800000", "HK.800700"]
                ret, data = self._quote_ctx.get_market_snapshot(hk_indices)
                if ret == 0 and data is not None and not data.empty:
                    name_map = {"HK.800000": "恒指", "HK.800700": "国指"}
                    market_parts.append("【港股大盘上午】")
                    for _, row in data.iterrows():
                        code = row.get("code", "")
                        change = float(row.get("change_rate", 0) or 0) / 100.0
                        last = float(row.get("last_price", 0))
                        name = name_map.get(code, code)
                        market_parts.append(f"  {name}: {last:.0f} ({change:+.2%})")
            except Exception:
                pass

        market_context = "\n".join(market_parts)

        # 量价数据: watchlist 上午表现
        price_data = self._gather_morning_price_data()

        # 反馈上下文: 上午交易总结 + 昨日教训 + 量比异动
        feedback_parts = [self._gather_morning_trade_summary()]

        if volume_leaders:
            feedback_parts.append("【午间量比异动】")
            for v in volume_leaders[:5]:
                feedback_parts.append(f"  {v['symbol']}: {v['key_trigger']}")

        # 尝试注入昨日教训
        try:
            from app.review.hk_memory_injector import get_hk_memory_injector
            injector = get_hk_memory_injector()
            if injector:
                recent = injector.inject({"source": "midday_debate", "limit": 5})
                if recent:
                    feedback_parts.append(f"【历史教训】\n{recent[:1500]}")
        except Exception:
            pass

        feedback_context = "\n".join(feedback_parts)

        return {
            "market_context": market_context,
            "price_data": price_data,
            "feedback_context": feedback_context,
        }

    def _run_midday_debate(self, context: Dict) -> Optional[Any]:
        """执行午间多Agent辩论 (同步, 非阻塞 — 失败时降级为规则)

        双轨: 优先走 Hermes Agent 队列 → 失败回退内部 run_debate_sync
        """
        # ── 双轨: 尝试 Hermes Agent ──
        try:
            cfg = getattr(self, '_hermes_config', None)
            if not cfg:
                from app.core.config_container import get_config_center
                cc = get_config_center()
                cfg = cc.get('hermes_agent', {}) if cc else {}
                self._hermes_config = cfg

            if cfg.get('midday_enabled', False):
                from app.llm.agent_bridge import get_ftseek_agent_bridge
                bridge = get_ftseek_agent_bridge("hk")
                h_payload = {
                    "market_context": context.get("market_context", ""),
                    "feedback_context": context.get("feedback_context", ""),
                    "price_data": context.get("price_data", ""),
                }
                h_rid = bridge.enqueue_task("midday_adjust", h_payload)
                if h_rid:
                    logger.info("[午间辩论] Hermes 任务已入队: %s", h_rid)
                    h_result = bridge.wait_for_queue_result(h_rid, timeout=120)
                    if h_result:
                        logger.info("[午间辩论] Hermes 完成: bias=%s", h_result.get('afternoon_bias', '?'))
                        return _hermes_midday_to_synthesis(h_result)
        except Exception as _he:
            logger.warning("[午间辩论] Hermes 路径异常 (降级): %s", _he)

        # 原有辩论 (回退)
        try:
            from app.agents.debate.debate_orchestrator import run_debate_sync

            logger.info("[午间辩论] 启动多Agent辩论...")
            synthesis = run_debate_sync(
                assessments=[],
                market_context=context.get("market_context", ""),
                feedback_context=context.get("feedback_context", ""),
                price_data=context.get("price_data", ""),
            )
            logger.info(
                f"[午间辩论] 完成: 共识={synthesis.consensus_direction} "
                f"conviction={synthesis.conviction:.2f} "
                f"quality={synthesis.debate_quality_score:.2f}"
            )
            return synthesis
        except Exception as e:
            logger.warning(f"[午间辩论] 辩论执行失败, 降级为规则驱动: {e}")
            return None

    # ─── 计划读写 ───────────────────────────────────────────

    def _load_plan(self) -> Optional[Dict]:
        if not PLAN_PATH.exists():
            logger.warning("[午间刷新] 作战计划文件不存在")
            return None
        try:
            with open(PLAN_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[午间刷新] 读取计划失败: {e}")
            return None

    def _lookup_premarket_signal_impact(self, plan: Dict) -> Dict[str, str]:
        """从盘前计划中提取各标的的 LLM signal_impact 映射

        午间新增标的如果已被盘前 LLM 分析过，应继承其 signal_impact，
        而非默认 neutral。这确保 LLM 信号链在午间刷新中不中断。
        """
        impact_map = {}
        for t in plan.get("targets", []):
            sym = t.get("symbol", "")
            si = t.get("signal_impact", "neutral")
            if sym and si != "neutral":
                impact_map[sym] = si
        # 同时从动态池中查找
        try:
            from app.utils.daily_active_pool import get_daily_active_pool
            pool = get_daily_active_pool(market_prefix="HK.")
            for code, entry in pool._entries.items():
                if not entry.is_expired():
                    si = entry.metadata.get("signal_impact", "neutral")
                    if si != "neutral" and code not in impact_map:
                        impact_map[code] = si
        except Exception:
            pass
        return impact_map

    def _save_plan(self, plan: Dict):
        try:
            with open(PLAN_PATH, "w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[午间刷新] 保存计划失败: {e}")

    # ─── 通知推送 ───────────────────────────────────────────

    def _send_midday_update(self, plan: Dict, new_targets: List[Dict],
                            ashare_data: List[Dict], result: Dict,
                            debate_synthesis=None):
        """推送午间刷新结果 (含辩论摘要)"""
        try:
            from app.risk.dingtalk_alert import init_dingtalk_alert
            from app import config
            sender = init_dingtalk_alert(
                webhook_url=config.DINGTALK_WEBHOOK_URL,
                secret=config.DINGTALK_SECRET,
            )

            lines = [
                f"🔄 **午间作战计划刷新**",
                f"📅 {plan.get('date', '')} 12:00 HKT",
                f"",
                f"---",
                f"",
            ]

            # A股上午收盘
            if ashare_data:
                lines.append("## 📈 A股上午收盘")
                for a in ashare_data:
                    c = a["change_pct"]
                    emoji = "🔴" if c < 0 else "🟢"
                    lines.append(f"- {emoji} {a['name']}: {a['price']:.2f} ({c:+.2%})")
                lines.append("")

            # 新增标的
            lines.extend([
                f"## ✅ 新增标的 ({len(new_targets)} 只)",
                "",
                "| 标的 | 方向 | 触发条件 | 来源 |",
                "|------|------|----------|------|",
            ])
            for t in new_targets:
                sym = t.get("symbol", "?").replace("HK.", "")
                direction = t.get("direction", "ANY")
                trigger = (t.get("key_trigger", "") or "")[:50]
                source = t.get("source", "?").replace("midday_", "")
                lines.append(f"| {sym} | {direction} | {trigger} | {source} |")
            lines.append("")

            # 🧠 辩论摘要
            if debate_synthesis:
                ds = debate_synthesis
                dir_emoji = "🟢" if ds.consensus_direction == "BUY" else "🔴" if ds.consensus_direction == "SELL" else "⚪"
                risk_emoji = "🟢" if ds.risk_consensus == "LOW" else "🟡" if ds.risk_consensus == "MEDIUM" else "🔴"

                lines.extend([
                    "## 🧠 午间多Agent辩论",
                    "",
                    f"- 共识方向: {dir_emoji} **{ds.consensus_direction}** (conviction={ds.conviction:.2f})",
                    f"- 风险共识: {risk_emoji} **{ds.risk_consensus}**",
                    f"- 辩论质量: **{ds.debate_quality_score:.2f}**",
                ])

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
                    lines.extend(["", f"> {ds.summary[:200]}"])

                lines.append("")

            # 更新后统计
            total = len(plan.get("targets", []))
            lines.extend([
                f"## 📊 更新后计划",
                f"- 目标总数: **{total}** 只 (新增{len(new_targets)}只)",
                f"- 计划版本: v{plan.get('plan_version', '?')}",
                f"- 风险等级: {plan.get('risk_level', 'N/A')}",
                f"",
                "---",
                "*FTSeek 午间刷新 · 自动推送*",
            ])

            sender.send_text("\n".join(lines), alert_level="INFO")
            logger.info(f"[午间刷新] 钉钉通知已推送 ({len(new_targets)}新增)")
        except Exception as e:
            logger.error(f"[午间刷新] 通知推送失败: {e}")

    def _send_midday_status(self, plan: Dict, ashare_data: List[Dict], result: Dict,
                            debate_synthesis=None):
        """午间无更新时的状态通报 (含辩论摘要)"""
        if not ashare_data:
            return

        try:
            from app.risk.dingtalk_alert import init_dingtalk_alert
            from app import config
            sender = init_dingtalk_alert(
                webhook_url=config.DINGTALK_WEBHOOK_URL,
                secret=config.DINGTALK_SECRET,
            )

            lines = [
                f"ℹ️ **午间市场快报**",
                f"📅 {plan.get('date', '')} 12:00 HKT",
                f"",
                f"作战计划无需更新 (无符合条件的午间新增标的)",
                f"",
            ]

            if ashare_data:
                lines.append("## 📈 A股上午收盘")
                for a in ashare_data:
                    c = a["change_pct"]
                    emoji = "🔴" if c < 0 else "🟢"
                    lines.append(f"- {emoji} {a['name']}: {a['price']:.2f} ({c:+.2%})")
                lines.append("")

            # 🧠 辩论摘要 (即使无新增标的也推送)
            if debate_synthesis:
                ds = debate_synthesis
                dir_emoji = "🟢" if ds.consensus_direction == "BUY" else "🔴" if ds.consensus_direction == "SELL" else "⚪"
                lines.extend([
                    "## 🧠 午间多Agent辩论",
                    "",
                    f"- 共识方向: {dir_emoji} **{ds.consensus_direction}** (conviction={ds.conviction:.2f})",
                    f"- 风险共识: **{ds.risk_consensus}**",
                    f"- 辩论质量: **{ds.debate_quality_score:.2f}**",
                ])
                if ds.disagreement_flags:
                    for flag in ds.disagreement_flags[:2]:
                        lines.append(f"- 分歧: {flag}")
                if ds.summary:
                    lines.extend(["", f"> {ds.summary[:200]}"])
                lines.append("")

            lines.extend([
                f"---",
                f"*FTSeek 午间快报 · 自动推送*",
            ])

            sender.send_text("\n".join(lines), alert_level="INFO")
            logger.info("[午间刷新] 午间快报已推送 (无更新)")
        except Exception as e:
            logger.error(f"[午间刷新] 快报推送失败: {e}")


def _hermes_midday_to_synthesis(hermes_result: Dict) -> Any:
    """将 Hermes Agent 的 midday_adjust 结果适配为 DebateSynthesis 兼容对象"""
    from collections import namedtuple
    Synthesis = namedtuple("HermesMiddaySynthesis", [
        "consensus_direction", "conviction", "debate_quality_score",
        "risk_consensus", "agent_opinions", "disagreement_flags",
        "merged_symbols", "summary",
    ])

    bias = hermes_result.get("afternoon_bias", "NEUTRAL")
    adjustments = hermes_result.get("plan_adjustments", [])
    risks = hermes_result.get("risk_adjustments", {})

    merged = [
        {
            "symbol": a.get("symbol", ""),
            "direction": "LONG",
            "support": a.get("reason", ""),
            "concern": "",
        }
        for a in adjustments[:5]
    ]

    return Synthesis(
        consensus_direction=bias,
        conviction=0.6,
        debate_quality_score=0.7,
        risk_consensus="HIGH" if risks.get("reduce_exposure") else "MEDIUM",
        agent_opinions=[{"agent": "HermesMidday", "direction": bias, "confidence": 0.6}],
        disagreement_flags=hermes_result.get("anomaly_flags", [])[:3],
        merged_symbols=merged,
        summary=hermes_result.get("summary", ""),
    )
