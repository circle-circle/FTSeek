"""
港股归因执行器 (HKAttributionRunner)

职责:
1. 包装现有 AttributionEngine (复用 902 行代码)
2. 新增 T+0 日内归因
3. 新增 A 股联动归因
4. 新增 碎股执行归因
5. 输出结构化 lessons 列表

设计原则:
- 不修改 AttributionEngine，仅包装
- 港股特异性归因独立模块 (零耦合)
- lessons 带 expiry_date + validation_status
"""

import logging
import sqlite3
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from app.evidence.attribution_engine import AttributionEngine
from app.evidence.trade_evidence_chain import (
    TradeEvidenceChain,
    EvidenceLayer,
    DirectionTag,
)
from app.attribution.llm_attribution import run_plan_gap_sync

logger = logging.getLogger("ftseek.review.hk_attribution_runner")

LESSONS_DB = os.environ.get(
    "FTSEEK_HK_LESSONS_DB", "data/hk_lessons.db"
)


class HKAttributionRunner:
    """港股归因执行器 — 复用 AttributionEngine + 港股扩展"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or LESSONS_DB
        self.engine = AttributionEngine(db=None, enable_llm_attribution=True)

        # 港股特异性归因器
        self.intraday_attribution = IntradayAttribution()
        self.ashare_attribution = AShareLinkageAttribution()
        self.lotsize_attribution = LotSizeExecutionAttribution()

        # LLM 归因结果缓存
        self._llm_attributions: Dict[str, Dict] = {}
        self._plan_gap_analysis: Optional[Dict] = None

        # 初始化 lessons DB
        self._init_lessons_db()

    def run(self, collected_data: Dict) -> List[Dict]:
        """执行完整归因链

        Args:
            collected_data: HKEODCollector.collect() 的输出
        """
        trades = collected_data.get("trades", [])
        if not trades:
            logger.info("[HKAttributionRunner] 今日无交易，跳过归因")
            return []

        battle_plan = collected_data.get("battle_plan") or {}
        ashare_context = collected_data.get("ashare_context") or {}
        trade_date = collected_data.get("trade_date", "")

        lessons = []

        for trade in trades:
            try:
                # 1. 构建 TradeEvidenceChain
                chain = self._build_chain(trade, battle_plan)

                # 2. 基础归因 (复用 AttributionEngine)
                chain = self.engine.attribute_trade(chain)

                # 3. T+0 归因
                if self.intraday_attribution.is_intraday(trade):
                    intraday_lesson = self.intraday_attribution.analyze(trade, chain)
                    if intraday_lesson:
                        lessons.append(intraday_lesson)

                # 4. A 股联动归因
                linkage_lesson = self.ashare_attribution.analyze(
                    trade, chain, ashare_context
                )
                if linkage_lesson:
                    lessons.append(linkage_lesson)

                # 5. 碎股执行归因
                lotsize_lesson = self.lotsize_attribution.analyze(trade, chain)
                if lotsize_lesson:
                    lessons.append(lotsize_lesson)

                # 6. 主归因 lesson
                lesson = self._format_lesson(chain, trade, trade_date)
                lessons.append(lesson)

            except Exception as e:
                logger.error(
                    f"[HKAttributionRunner] 单笔归因失败 {trade.get('code', '?')}: {e}",
                    exc_info=True,
                )

        # 去重 + 排序 (高亏损优先)
        lessons = self._deduplicate_and_rank(lessons)

        # 7. LLM 归因 (每笔亏损交易并行方向+执行归因)
        try:
            lessons = self._run_llm_attribution(trades, battle_plan, lessons, trade_date)
        except Exception as e:
            logger.warning(f"[HKAttributionRunner] LLM归因失败 (非阻塞): {e}")

        # 8. 计划 vs 执行差距分析
        try:
            positions = collected_data.get("positions", [])
            gap = run_plan_gap_sync(battle_plan, trades, positions)
            self._plan_gap_analysis = gap
        except Exception as e:
            logger.warning(f"[HKAttributionRunner] 计划差距分析失败 (非阻塞): {e}")

        # 归因辩论门禁 (P0) — 防止垃圾数据注入下次决策
        from app.review.attribution_debate import get_attribution_debate
        lessons = get_attribution_debate().validate(lessons, collected_data)

        # 写入 lessons DB
        self._save_lessons(lessons)

        logger.info(
            f"[HKAttributionRunner] 归因完成: {len(trades)} 笔交易 → "
            f"{len(lessons)} 条教训 (已验证)"
        )
        return lessons

    # ──────────────────────────────────────────────────────────────
    #  内部方法
    # ──────────────────────────────────────────────────────────────

    def _build_chain(self, trade: Dict, battle_plan: Dict) -> TradeEvidenceChain:
        """从交易记录构建 TradeEvidenceChain"""
        code = trade.get("code", "")
        trace_id = trade.get("trace_id", f"hk_manual_{code}_{datetime.now().strftime('%H%M%S')}")
        pnl_abs = float(trade.get("pnl", 0))
        pnl_pct = float(trade.get("pnl_percent", 0))
        exit_reason = trade.get("exit_reason", trade.get("reason", ""))
        holding = int(trade.get("holding_minutes", 0))

        chain = TradeEvidenceChain(
            trace_id=trace_id,
            symbol=code,
            pnl=pnl_abs,
            pnl_percent=pnl_pct,
            exit_reason=exit_reason,
            holding_period_minutes=holding,
        )

        # 填充 evidence 层
        chain.evidence.symbol = code
        chain.evidence.trace_id = trace_id

        # 从 battle plan 获取入场时的预期方向
        plan_targets = battle_plan.get("targets", [])
        for t in plan_targets:
            if t.get("symbol") == code:
                chain.evidence.primary_strategy = "battle_plan"
                chain.evidence.llm_confidence = 0.7  # battle plan 默认置信度
                break

        return chain

    def _format_lesson(
        self, chain: TradeEvidenceChain, trade: Dict, trade_date: str
    ) -> Dict:
        """格式化 lesson 输出"""
        pnl_pct = chain.pnl_percent
        direction_correct = chain.attribution.direction_correct

        # 确定主要原因
        cause = self._determine_primary_cause(chain)

        # 提取 lesson 文本
        lesson_text = self._extract_lesson_text(chain, trade)

        # 计算过期时间 (亏损越大，教训有效期越长)
        if pnl_pct < -3:
            expiry_days = 30
        elif pnl_pct < -1:
            expiry_days = 14
        else:
            expiry_days = 7

        return {
            "id": f"hk_lesson_{chain.trace_id}",
            "trade_date": trade_date,
            "symbol": trade.get("code", ""),
            "direction": trade.get("direction", "UNKNOWN"),
            "pnl_percent": round(pnl_pct, 2),
            "cause": cause,
            "lesson": lesson_text,
            "confidence": chain.evidence.llm_confidence,
            "overall_score": chain.attribution.overall_score,
            "expiry_date": (
                datetime.now() + timedelta(days=expiry_days)
            ).strftime("%Y-%m-%d"),
            "validation_status": "pending",
            "market_context": {
                "is_intraday": self.intraday_attribution.is_intraday(trade),
                "direction_correct": direction_correct,
                "exit_reason": chain.exit_reason,
                "holding_minutes": chain.holding_period_minutes,
                "signal_quality": chain.attribution.signal_quality,
                "execution_quality": chain.attribution.execution_quality,
                "risk_quality": chain.attribution.risk_quality,
                "failure_factors": chain.attribution.failure_factors,
                "success_factors": chain.attribution.success_factors,
            },
        }

    def _determine_primary_cause(self, chain: TradeEvidenceChain) -> str:
        """确定交易的主要原因/问题"""
        pnl = chain.pnl_percent

        # 止损触发
        if chain.exit_reason and "stop" in chain.exit_reason.lower():
            return "stop_loss_triggered"

        # 方向错误
        if pnl < -1 and not chain.attribution.direction_correct:
            return "direction_wrong"

        # 执行问题
        if chain.attribution.execution_quality == "poor":
            return "execution_poor"

        # 风控问题
        if chain.attribution.risk_quality == "poor":
            return "risk_control_failed"

        # 时机问题
        if chain.attribution.timing_quality == "poor":
            return "timing_poor"

        # 信号问题
        if chain.attribution.signal_quality == "low":
            return "signal_weak"

        if pnl > 0:
            return "successful_trade"

        return "unknown"

    def _extract_lesson_text(self, chain: TradeEvidenceChain, trade: Dict) -> str:
        """提取可操作的教训文本"""
        parts = []
        symbol = chain.symbol
        pnl = chain.pnl_percent

        # 方向相关
        if pnl < -1 and not chain.attribution.direction_correct:
            parts.append(f"{symbol} 方向判断错误 (预期方向 vs 实际走势背离)")

        # 止损相关
        if "stop_loss" in chain.exit_reason.lower():
            parts.append("止损触发执行正确，但入场时机或方向判断有误")

        # 执行相关
        if chain.attribution.execution_quality == "poor":
            parts.append("执行质量差 (滑点过高/部分成交)")

        # 信号相关
        if chain.attribution.signal_quality == "low":
            parts.append("信号源不足，建议增加确认条件")

        # T+0 相关
        if chain.holding_period_minutes < 30 and pnl < 0:
            parts.append("持仓时间过短 (<30min)，可能是追高/恐慌卖出")

        if not parts:
            if pnl > 0:
                parts.append(f"{symbol} 盈利交易，方向/时机/执行均合理")
            else:
                parts.append(f"{symbol} 小幅亏损，需观察后续表现")

        return "；".join(parts)

    def _deduplicate_and_rank(self, lessons: List[Dict]) -> List[Dict]:
        """去重 + 排序 (高亏损优先，同 symbol 取最近)"""
        seen = {}
        for lesson in lessons:
            key = (lesson["symbol"], lesson["cause"])
            if key not in seen or lesson["pnl_percent"] < seen[key]["pnl_percent"]:
                seen[key] = lesson

        ranked = sorted(seen.values(), key=lambda x: x["pnl_percent"])
        return ranked

    def _run_llm_attribution(
        self, trades: List[Dict], battle_plan: Dict,
        lessons: List[Dict], trade_date: str,
    ) -> List[Dict]:
        """对亏损交易执行 LLM 归因 (方向+执行并行)"""
        from app.attribution.llm_attribution import run_attribution_sync

        loss_trades = [
            t for t in trades
            if (t.get("pnl", t.get("realized_pnl", 0)) or 0) < 0
        ]
        if not loss_trades:
            return lessons

        logger.info(f"[HKAttributionRunner] LLM归因: {len(loss_trades)}笔亏损交易")
        for trade in loss_trades:
            code = trade.get("code", "")
            try:
                result = run_attribution_sync(trade, battle_plan)
                self._llm_attributions[code] = result

                # 将 LLM 归因注入到对应 lesson
                dir_attr = result.get("direction")
                exec_attr = result.get("execution")
                if not dir_attr or not exec_attr:
                    continue

                for lesson in lessons:
                    if lesson.get("symbol") == code and lesson.get("cause") == "unknown":
                        # 用 LLM 归因覆盖 unknown
                        if dir_attr.cause_category not in ("reasonable_loss", "unknown"):
                            lesson["cause"] = dir_attr.cause_category
                            lesson["lesson"] = dir_attr.lesson_text
                            lesson["llm_attribution"] = {
                                "direction": {
                                    "cause_category": dir_attr.cause_category,
                                    "root_cause": dir_attr.root_cause,
                                    "lesson": dir_attr.lesson_text,
                                    "improvement": dir_attr.improvement_action,
                                    "confidence": dir_attr.confidence,
                                    "key_finding": dir_attr.key_finding,
                                },
                                "execution": {
                                    "cause_category": exec_attr.cause_category,
                                    "root_cause": exec_attr.root_cause,
                                    "lesson": exec_attr.lesson_text,
                                    "improvement": exec_attr.improvement_action,
                                    "confidence": exec_attr.confidence,
                                    "key_finding": exec_attr.key_finding,
                                },
                            }
                        break

                logger.debug(
                    f"[HKAttributionRunner] {code} LLM归因: "
                    f"dir={dir_attr.cause_category} exec={exec_attr.cause_category}"
                )
            except Exception as e:
                logger.warning(f"[HKAttributionRunner] {code} LLM归因失败: {e}")

        return lessons

    def get_plan_gap_analysis(self) -> Optional[Dict]:
        """获取计划差距分析结果"""
        return self._plan_gap_analysis

    def _init_lessons_db(self):
        """初始化 lessons SQLite (仅首次)"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lessons (
                    id TEXT PRIMARY KEY,
                    trade_date TEXT,
                    symbol TEXT,
                    direction TEXT,
                    pnl_percent REAL,
                    cause TEXT,
                    lesson TEXT,
                    confidence REAL,
                    overall_score INTEGER,
                    expiry_date TEXT,
                    validation_status TEXT DEFAULT 'pending',
                    market_context TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lessons_symbol ON lessons(symbol)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lessons_expiry ON lessons(expiry_date)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lessons_validation ON lessons(validation_status)"
            )
            conn.commit()
            conn.close()
            logger.debug(f"[HKAttributionRunner] Lessons DB 初始化完成: {self.db_path}")
        except Exception as e:
            logger.error(f"[HKAttributionRunner] Lessons DB 初始化失败: {e}")

    def _save_lessons(self, lessons: List[Dict]):
        """写入 lessons DB"""
        try:
            conn = sqlite3.connect(self.db_path)
            for lesson in lessons:
                context_json = __import__("json").dumps(
                    lesson.get("market_context", {})
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO lessons
                    (id, trade_date, symbol, direction, pnl_percent, cause, lesson,
                     confidence, overall_score, expiry_date, market_context)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lesson["id"],
                        lesson["trade_date"],
                        lesson["symbol"],
                        lesson["direction"],
                        lesson["pnl_percent"],
                        lesson["cause"],
                        lesson["lesson"],
                        lesson.get("confidence", 0),
                        lesson.get("overall_score", 0),
                        lesson["expiry_date"],
                        context_json,
                    ),
                )
            conn.commit()
            conn.close()
            logger.info(f"[HKAttributionRunner] {len(lessons)} 条教训已写入 DB")
        except Exception as e:
            logger.error(f"[HKAttributionRunner] 写入 Lessons DB 失败: {e}")


# ──────────────────────────────────────────────────────────────────
#  港股特异性归因器 (独立模块，零耦合)
# ──────────────────────────────────────────────────────────────────


class IntradayAttribution:
    """T+0 日内归因 — 港股特有"""

    def is_intraday(self, trade: Dict) -> bool:
        """判断是否为日内交易"""
        return trade.get("is_intraday", False)

    def analyze(self, trade: Dict, chain: TradeEvidenceChain) -> Optional[Dict]:
        """分析日内交易特征"""
        holding_min = chain.holding_period_minutes
        pnl = chain.pnl_percent

        if holding_min > 60:
            return None  # 不算日内快进快出

        cause = None
        lesson = None

        if pnl < -2 and holding_min < 15:
            cause = "intraday_panic_sell"
            lesson = (
                f"{trade['code']} 买入后 {holding_min}min 内亏损 >2% 卖出，"
                "属于恐慌性止损，建议增加入场确认条件"
            )
        elif pnl > 2 and holding_min < 30:
            cause = "intraday_quick_profit"
            lesson = (
                f"{trade['code']} 买入后 {holding_min}min 内盈利 >2%，"
                "日内快进快出策略有效，可复用该模式"
            )
        elif pnl < -1 and holding_min < 30:
            cause = "intraday_chase_high"
            lesson = (
                f"{trade['code']} 追高买入后 {holding_min}min 内亏损，"
                "港股 T+0 追高风险高，建议等待回调确认"
            )

        if not cause:
            return None

        return {
            "id": f"hk_intraday_{trade.get('code', '')}_{trade.get('entry_time', '')[:10]}",
            "trade_date": trade.get("entry_time", "")[:10],
            "symbol": trade.get("code", ""),
            "direction": trade.get("direction", "UNKNOWN"),
            "pnl_percent": round(pnl, 2),
            "cause": cause,
            "lesson": lesson,
            "confidence": 0.8,  # 规则-based，高置信度
            "overall_score": chain.attribution.overall_score,
            "expiry_date": (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d"),
            "validation_status": "pending",
            "market_context": {
                "is_intraday": True,
                "holding_minutes": holding_min,
                "attribution_type": "intraday",
            },
        }


class AShareLinkageAttribution:
    """A 股联动归因 — 港股特有"""

    # A 股 → 港股 映射 (核心联动标的)
    _MAPPING = {
        "SH.000001": ["HK.00700", "HK.00941", "HK.01810"],  # 上证 → 腾讯/中移/小米
        "SH.000300": ["HK.02202", "HK.01211", "HK.02015"],  # 沪深300 → 万科/比亚迪/理想
    }

    def analyze(
        self, trade: Dict, chain: TradeEvidenceChain, ashare_context: Dict
    ) -> Optional[Dict]:
        """分析 A 股联动影响"""
        if not ashare_context:
            return None

        hk_code = trade.get("code", "")
        pnl = chain.pnl_percent

        # 查找该港股对应的 A 股指数
        related_ashare = None
        for ashare_idx, hk_list in self._MAPPING.items():
            if hk_code in hk_list:
                related_ashare = ashare_idx
                break

        if not related_ashare or related_ashare not in ashare_context:
            return None

        ashare_data = ashare_context[related_ashare]
        ashare_change = ashare_data.get("change_pct", 0)

        # 背离检测: A 股涨但港股跌 (或反之)
        divergence = (ashare_change > 1 and pnl < -1) or (
            ashare_change < -1 and pnl > 1
        )

        if not divergence:
            return None

        cause = "ashare_divergence"
        lesson = (
            f"{hk_code} 与 {related_ashare} 走势背离 "
            f"(A股{ashare_change:+.1f}% vs 港股{pnl:+.1f}%)，"
            "联动失效可能因 AH 溢价过高或外资流向变化"
        )

        return {
            "id": f"hk_ashare_{hk_code}_{trade.get('entry_time', '')[:10]}",
            "trade_date": trade.get("entry_time", "")[:10],
            "symbol": hk_code,
            "direction": trade.get("direction", "UNKNOWN"),
            "pnl_percent": round(pnl, 2),
            "cause": cause,
            "lesson": lesson,
            "confidence": 0.7,
            "overall_score": chain.attribution.overall_score,
            "expiry_date": (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d"),
            "validation_status": "pending",
            "market_context": {
                "attribution_type": "ashare_linkage",
                "ashare_index": related_ashare,
                "ashare_change_pct": round(ashare_change, 2),
                "hk_pnl_pct": round(pnl, 2),
            },
        }


class LotSizeExecutionAttribution:
    """碎股执行归因 — 港股特有"""

    def analyze(self, trade: Dict, chain: TradeEvidenceChain) -> Optional[Dict]:
        """分析碎股对齐对执行的影响"""
        original_qty = trade.get("original_qty")
        executed_qty = trade.get("qty", trade.get("quantity"))
        pnl = chain.pnl_percent

        if original_qty is None or executed_qty is None:
            return None

        if original_qty == executed_qty:
            return None  # 无碎股调整

        trim_ratio = (original_qty - executed_qty) / max(original_qty, 1)
        if trim_ratio < 0.05:
            return None  # 调整 <5%，忽略

        cause = "lot_size_trim_impact"
        lesson = (
            f"{trade.get('code', '')} 碎股对齐: {original_qty}→{executed_qty} "
            f"(减少 {trim_ratio:.0%})，"
            "仓位不足可能导致收益不及预期，建议调整仓位计算公式"
        )

        return {
            "id": f"hk_lotsize_{trade.get('code', '')}_{trade.get('entry_time', '')[:10]}",
            "trade_date": trade.get("entry_time", "")[:10],
            "symbol": trade.get("code", ""),
            "direction": trade.get("direction", "UNKNOWN"),
            "pnl_percent": round(pnl, 2),
            "cause": cause,
            "lesson": lesson,
            "confidence": 0.9,  # 规则-based，确定性强
            "overall_score": chain.attribution.overall_score,
            "expiry_date": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"),
            "validation_status": "pending",
            "market_context": {
                "attribution_type": "lot_size_trim",
                "original_qty": original_qty,
                "executed_qty": executed_qty,
                "trim_ratio": round(trim_ratio, 3),
            },
        }
