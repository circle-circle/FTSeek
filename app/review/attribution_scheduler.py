"""
attribution_scheduler.py - LLM 逐笔归因调度器

⚠️ T5 钩子 (TZ 审计 2026-08-30, 复活前必修): 本模块零消费方 (死码)。调度用服务器
    CST date.today() (get_unattributed_trades / run_daily_attribution) 对比 ET 平仓日
    — ET 午后平仓系统性漏扫。复活时须改 app/utils/timezone 的 ET 口径并重验扫描窗口。

Phase 2 (2026-05-31): 完整交易链路归因

职责:
- 每日收盘后扫描当日新平仓且未归因的交易
- 逐笔调用 LLM 归因引擎 (LLMTAttributionEngine)
- 结果写入 trade_attribution 表 (Phase 2 扩展字段)
- 内置预算控制 (daily_budget_usd)、单次配额 (max_trades_per_run)

设计原则:
- 框架先行, attribution_engine 缺失时使用 placeholder 跳过
- 幂等: 已归因的 trade_id 不会重复处理
- 失败不阻塞: 单笔异常不影响整体流程
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AttributionScheduler:
    """归因调度器

    Args:
        trade_store: TradeStore 实例 (持有 ftseek_us.db 连接)
        attribution_engine: LLMTAttributionEngine 实例 (可选, 缺失时只走 placeholder)
        daily_budget_usd:   单日预算上限 (美元), 超出后跳过剩余交易
        max_trades_per_run: 单次运行最多处理多少笔
    """

    def __init__(
        self,
        trade_store,
        attribution_engine: Any = None,
        daily_budget_usd: float = 2.0,
        max_trades_per_run: int = 20,
    ):
        self.trade_store = trade_store
        self.attribution_engine = attribution_engine
        self.daily_budget_usd = daily_budget_usd
        self.max_trades_per_run = max_trades_per_run
        self._cost_today: float = 0.0
        # 单笔归因预估成本 (美元), 实际成本若 attribution_engine 返回则覆盖
        self._estimated_cost_per_call: float = 0.05

    # ──────────────────────────────────────────────────────────
    # DB helpers
    # ──────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.trade_store.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ──────────────────────────────────────────────────────────
    # 1. 取数: 当日新平仓且未归因的交易
    # ──────────────────────────────────────────────────────────

    def get_unattributed_trades(
        self, target_date: Optional[date] = None
    ) -> List[Dict[str, Any]]:
        """获取指定日期未归因的已平仓交易

        判定条件:
          - exit_time 落在 target_date 当天
          - exit_price > 0
          - 不存在 trade_attribution.trade_id 等于 trade_records.id 且 key_decision_factor 已写入
        """
        if target_date is None:
            target_date = date.today()
        date_str = target_date.strftime("%Y-%m-%d")

        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT t.id, t.trade_date, t.symbol, t.direction,
                       t.entry_time, t.exit_time,
                       t.entry_price, t.exit_price, t.quantity,
                       t.realized_pnl, t.pnl_pct,
                       t.exit_reason, t.holding_hours,
                       t.signal_strength, t.signal_confidence,
                       t.voting_result, t.model_scores, t.consensus_level,
                       t.strategy_id, t.trace_id, t.market_regime,
                       t.entry_reason, t.signal_details
                FROM trade_records t
                LEFT JOIN trade_attribution a
                       ON CAST(t.id AS TEXT) = a.trade_id
                      AND a.key_decision_factor IS NOT NULL
                WHERE t.exit_price > 0
                  AND t.exit_time IS NOT NULL
                  AND substr(t.exit_time, 1, 10) = ?
                  AND a.trade_id IS NULL
                ORDER BY t.exit_time ASC
                LIMIT ?
            """, (date_str, self.max_trades_per_run)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ──────────────────────────────────────────────────────────
    # 2. 主流程: 每日归因
    # ──────────────────────────────────────────────────────────

    def run_daily_attribution(
        self, target_date: Optional[date] = None
    ) -> Dict[str, Any]:
        """执行每日归因任务

        Returns:
            {"total": N, "attributed": M, "skipped": K, "cost_usd": X, "errors": E}
        """
        self._cost_today = 0.0
        target_date = target_date or date.today()

        trades = self.get_unattributed_trades(target_date)
        total = len(trades)
        attributed, skipped, errors = 0, 0, 0

        logger.info(
            f"[AttributionScheduler] {target_date} 待归因 {total} 笔 "
            f"(budget=${self.daily_budget_usd:.2f}, max={self.max_trades_per_run})"
        )

        for trade in trades:
            if not self._check_budget():
                logger.warning(
                    f"[AttributionScheduler] 预算用尽 ${self._cost_today:.3f}, "
                    f"跳过剩余 {total - attributed - skipped - errors} 笔"
                )
                skipped += total - attributed - skipped - errors
                break
            try:
                result = self._attribute_single_trade(trade)
                if result is None:
                    skipped += 1
                    continue
                self._save_attribution(str(trade["id"]), result)
                attributed += 1
                self._cost_today += float(result.get("cost_usd", self._estimated_cost_per_call))
            except Exception as e:
                errors += 1
                logger.exception(
                    f"[AttributionScheduler] 归因失败 trade_id={trade.get('id')} "
                    f"symbol={trade.get('symbol')}: {e}"
                )

        summary = {
            "total":      total,
            "attributed": attributed,
            "skipped":    skipped,
            "errors":     errors,
            "cost_usd":   round(self._cost_today, 4),
            "date":       target_date.isoformat(),
        }
        logger.info(f"[AttributionScheduler] 完成: {summary}")
        return summary

    # ──────────────────────────────────────────────────────────
    # 3. 单笔归因
    # ──────────────────────────────────────────────────────────

    def _attribute_single_trade(self, trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """对单笔交易执行归因分析

        步骤:
          1. 构建归因上下文 (交易信息 + 市场上下文 + 投票信息)
          2. 调用 LLM 归因引擎 (缺失时返回 None)
          3. 解析为标准化的字段 dict
        """
        context = self._build_context(trade)

        if self.attribution_engine is None:
            logger.debug(
                f"[AttributionScheduler] attribution_engine 未配置, "
                f"跳过 trade_id={trade.get('id')}"
            )
            return None

        # placeholder: 兼容多种 engine 接口签名
        engine = self.attribution_engine
        try:
            if hasattr(engine, "attribute_trade"):
                raw = engine.attribute_trade(trade, context)
            elif callable(engine):
                raw = engine(trade, context)
            else:
                logger.warning("[AttributionScheduler] attribution_engine 接口未知")
                return None
        except Exception as e:
            logger.exception(f"[AttributionScheduler] LLM 调用失败: {e}")
            return None

        return self._parse_engine_result(raw, trade)

    def _build_context(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """构建归因上下文: 交易 + 市场快照 + 投票"""
        ctx: Dict[str, Any] = {
            "trade_summary": {
                "id":            trade.get("id"),
                "symbol":        trade.get("symbol"),
                "direction":     trade.get("direction"),
                "entry_price":   trade.get("entry_price"),
                "exit_price":    trade.get("exit_price"),
                "quantity":      trade.get("quantity"),
                "realized_pnl":  trade.get("realized_pnl"),
                "pnl_pct":       trade.get("pnl_pct"),
                "exit_reason":   trade.get("exit_reason"),
                "holding_hours": trade.get("holding_hours"),
                "trace_id":      trade.get("trace_id"),
            }
        }
        # 投票信息
        if trade.get("voting_result"):
            try:
                ctx["voting_result"] = json.loads(trade["voting_result"])
            except (TypeError, ValueError):
                ctx["voting_result"] = trade.get("voting_result")
        if trade.get("model_scores"):
            try:
                ctx["model_scores"] = json.loads(trade["model_scores"])
            except (TypeError, ValueError):
                ctx["model_scores"] = trade.get("model_scores")

        # 市场上下文快照已退役 (2026-09-13): entry_context_snapshot 死表删除,
        # 活通道 = trade_records.factor_detail.ts_ctx (F2, DEB-20260804)

        # 入场理由
        if trade.get("entry_reason"):
            try:
                ctx["entry_reason"] = json.loads(trade["entry_reason"])
            except (TypeError, ValueError):
                ctx["entry_reason"] = trade.get("entry_reason")

        return ctx

    def _parse_engine_result(
        self, raw: Any, trade: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """把 engine 返回值标准化为 trade_attribution 字段 dict"""
        if raw is None:
            return None

        # 兼容 dataclass / dict
        if hasattr(raw, "__dict__") and not isinstance(raw, dict):
            try:
                from dataclasses import asdict, is_dataclass
                data = asdict(raw) if is_dataclass(raw) else dict(raw.__dict__)
            except Exception:
                data = dict(raw.__dict__)
        elif isinstance(raw, dict):
            data = raw
        else:
            logger.warning(f"[AttributionScheduler] 未知 result 类型: {type(raw)}")
            return None

        result = {
            "trace_id":                     data.get("trace_id") or trade.get("trace_id"),
            "key_decision_factor":          data.get("key_decision_factor", ""),
            "should_have_done_differently": self._serialize_advice(data.get("should_have_done_differently"),
                                                                    data.get("specific_suggestion")),
            "specific_suggestion":          data.get("specific_suggestion", ""),
            "is_systemic_issue":            int(bool(data.get("is_systemic_issue", False))),
            "systemic_pattern":             data.get("systemic_pattern", ""),
            "root_cause_category":          data.get("root_cause_category")
                                              or self._infer_root_cause(trade, data),
            "confidence":                   float(data.get("confidence", 0.0) or 0.0),
            "llm_model":                    data.get("llm_model", ""),
            "cost_usd":                     float(data.get("cost_usd", self._estimated_cost_per_call)
                                                  or self._estimated_cost_per_call),
        }
        return result

    @staticmethod
    def _serialize_advice(differently: Any, suggestion: Any) -> str:
        """把 should_have_done_differently 序列化为 TEXT (DB 层是 TEXT)"""
        if isinstance(differently, bool):
            payload = {"flag": differently, "suggestion": suggestion or ""}
            return json.dumps(payload, ensure_ascii=False)
        if isinstance(differently, (dict, list)):
            return json.dumps(differently, ensure_ascii=False)
        return str(differently or "")

    @staticmethod
    def _infer_root_cause(trade: Dict[str, Any], data: Dict[str, Any]) -> str:
        """根据 exit_reason 给出兜底的 root_cause_category"""
        try:
            from app.risk.exit_reason_registry import get_exit_category, normalize_exit_reason
        except Exception:
            return ""

        reason = normalize_exit_reason(trade.get("exit_reason") or "")
        cat = get_exit_category(reason)
        # 把 ExitCategory 映射到 root_cause_category 四大类
        mapping = {
            "price_action": "strategy",
            "time_based":   "strategy",
            "risk_control": "risk",
            "manual":       "execution",
        }
        return mapping.get(cat or "", "")

    # ──────────────────────────────────────────────────────────
    # 4. 落库
    # ──────────────────────────────────────────────────────────

    def _save_attribution(self, trade_id: str, result: Dict[str, Any]) -> None:
        """保存归因结果到 trade_attribution (UPSERT by trade_id)"""
        now = datetime.now().isoformat()
        conn = self._conn()
        try:
            existing = conn.execute(
                "SELECT 1 FROM trade_attribution WHERE trade_id = ? LIMIT 1",
                (trade_id,)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE trade_attribution
                    SET trace_id = COALESCE(?, trace_id),
                        key_decision_factor = ?,
                        should_have_done_differently = ?,
                        specific_suggestion = ?,
                        is_systemic_issue = ?,
                        systemic_pattern = ?,
                        root_cause_category = ?,
                        confidence = ?,
                        llm_model = ?,
                        updated_at = ?
                    WHERE trade_id = ?
                """, (
                    result.get("trace_id"),
                    result.get("key_decision_factor"),
                    result.get("should_have_done_differently"),
                    result.get("specific_suggestion"),
                    result.get("is_systemic_issue", 0),
                    result.get("systemic_pattern"),
                    result.get("root_cause_category"),
                    result.get("confidence"),
                    result.get("llm_model"),
                    now,
                    trade_id,
                ))
            else:
                # 2026-07-09: 从 trade_records 查 decision_id 并写入 (P1: 归因链路贯穿)
                _dec_id = None
                try:
                    _tr_row = conn.execute(
                        "SELECT decision_id FROM trade_records WHERE id = ?", (trade_id,)
                    ).fetchone()
                    if _tr_row:
                        _dec_id = _tr_row[0]
                except Exception:
                    pass

                conn.execute("""
                    INSERT INTO trade_attribution (
                        trade_id, trace_id, key_decision_factor,
                        should_have_done_differently, specific_suggestion,
                        is_systemic_issue, systemic_pattern, root_cause_category,
                        confidence, llm_model, created_at, updated_at, decision_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    result.get("trace_id"),
                    result.get("key_decision_factor"),
                    result.get("should_have_done_differently"),
                    result.get("specific_suggestion"),
                    result.get("is_systemic_issue", 0),
                    result.get("systemic_pattern"),
                    result.get("root_cause_category"),
                    result.get("confidence"),
                    result.get("llm_model"),
                    now, now, _dec_id,
                ))
            conn.commit()
            logger.info(
                f"[AttributionScheduler] 保存归因 trade_id={trade_id} "
                f"factor='{(result.get('key_decision_factor') or '')[:40]}' "
                f"systemic={result.get('is_systemic_issue', 0)}"
            )
        finally:
            conn.close()

    # ──────────────────────────────────────────────────────────
    # 5. 预算控制
    # ──────────────────────────────────────────────────────────

    def _check_budget(self) -> bool:
        """检查是否超预算"""
        return self._cost_today < self.daily_budget_usd

    def reset_daily_cost(self) -> None:
        """重置当日累计成本 (一般在每日任务开始时调用)"""
        self._cost_today = 0.0
