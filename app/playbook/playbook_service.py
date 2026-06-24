"""
Playbook Service — 编排执行、反馈收集、查询

核心职责:
  1. 执行 Playbook 指令 (通过 ManualOrderService 下单)
  2. 注册 ManualHoldRegistry (防止 auto-pipeline 干扰)
  3. 收集用户反馈 (跳过原因、参数修改)
  4. 更新 playbook_instructions DB 执行状态
  5. 提供 Dashboard 查询接口

Usage:
  from app.playbook.playbook_service import get_playbook_service

  svc = get_playbook_service()
  instructions = svc.get_today_instructions()
  result = svc.execute_instruction(instruction_id)
  svc.skip_instruction(instruction_id, reason="风险太大")
"""
import logging
import json
from typing import Dict, List, Optional, Any
from datetime import datetime, date

from app.playbook.db import PlaybookDB

logger = logging.getLogger(__name__)

# Singleton
_service_instance = None


def _extract_atr_from_instruction(inst: Dict[str, Any]) -> float:
    """从 playbook 指令提取 ATR 数值, 写入 trade_records.metadata_json.atr (诊断用).

    2026-06-24 修复: playbook_instructions 表无 atr_source 顶层列 (schema 见 db.py),
    atr_source 嵌在 raw_instruction_json blob 里 (instruction_generator 产出). 此前
    inst.get("atr_source","") 永远返回 "" → "=" not in "" → metadata.atr 恒为 0,
    导致事后复盘"SL 距离是否合理"无法诊断. 注意: 退出逻辑本身不读此值 (退出用实时
    market_data.atr), 故此前 bug 仅影响可观测性, 不影响执行.

    atr_source 格式 (来自 instruction_generator):
      "technical.atr_14=8.35 (quality=good)"
      "daily_kline.atr_14=8.35 (quality=good)"
      "FALLBACK: price×2%=8.35 (ATR missing)"
    """
    # 1. 顶层 atr_source (前向兼容: 若未来加列可直接命中)
    src = inst.get("atr_source") or ""
    if "=" not in src:
        # 2. 嵌在 raw_instruction_json 里 — 解析出来
        raw = inst.get("raw_instruction_json")
        if raw:
            try:
                src = (json.loads(raw) if isinstance(raw, str) else raw).get("atr_source") or ""
            except (ValueError, TypeError):
                src = ""
    if not src or "=" not in src:
        return 0.0
    try:
        # 取第一个 "=" 后、空格前的数值
        return float(src.split("=", 1)[1].split(" ")[0])
    except (ValueError, IndexError):
        return 0.0



def get_playbook_service():
    """获取 PlaybookService 单例"""
    global _service_instance
    if _service_instance is None:
        _service_instance = PlaybookService()
    return _service_instance


class PlaybookService:
    """Playbook 业务服务层"""

    def __init__(self, playbook_db: PlaybookDB = None):
        self.db = playbook_db or PlaybookDB()
        self.db.ensure_tables()

    # ========== 查询接口 ==========

    def get_today_instructions(self, trade_date: str = None) -> List[Dict]:
        """获取今日交易指令"""
        if trade_date is None:
            trade_date = self._et_today()
        instructions = self.db.get_instructions_by_date(trade_date)
        # 附加用户反馈
        for inst in instructions:
            feedbacks = self.db.get_user_feedback(inst["id"])
            inst["user_feedback"] = feedbacks
        return instructions

    def get_instruction_detail(self, instruction_id: int) -> Optional[Dict]:
        """获取指令详情 (含反馈)"""
        # 通过 recent instructions 查找
        instructions = self.db.get_recent_instructions(limit=200)
        for inst in instructions:
            if inst["id"] == instruction_id:
                inst["user_feedback"] = self.db.get_user_feedback(inst["id"])
                return inst
        return None

    def get_playbook_summary(self, days: int = 30) -> Dict[str, Any]:
        """获取 Playbook 汇总统计"""
        stats = self.db.get_summary_stats(days)
        regime_log = self.db.get_regime_log(days=5)
        today_instructions = self.get_today_instructions()

        # 按方向分类
        buy_count = sum(1 for i in today_instructions if i.get("direction") == "BUY")
        short_count = sum(1 for i in today_instructions if i.get("direction") == "SELL_SHORT")
        skip_count = sum(1 for i in today_instructions if i.get("direction") == "SKIP")
        executed_count = sum(1 for i in today_instructions if i.get("executed"))

        return {
            **stats,
            "today_date": self._et_today(),
            "today_instructions_count": len(today_instructions),
            "today_buy_count": buy_count,
            "today_short_count": short_count,
            "today_skip_count": skip_count,
            "today_executed_count": executed_count,
            "recent_regime": regime_log[0] if regime_log else None,
            "active_patterns": self._get_active_patterns_summary(),
        }

    def get_history(self, days: int = 30) -> List[Dict]:
        """获取历史指令"""
        return self.db.get_recent_instructions(limit=days * 10)

    def get_factor_stats(self) -> Dict[str, List[Dict]]:
        """获取因子统计"""
        return {
            "single": self.db.get_factor_stats(level="single"),
            "pair": self.db.get_factor_stats(level="pair"),
        }

    def get_patterns(self, status: str = None) -> List[Dict]:
        """获取 patterns"""
        return self.db.get_patterns(status=status)

    # ========== 执行接口 ==========

    def execute_instruction(
        self,
        instruction_id: int,
        acknowledge_risk: bool = False,
        custom_params: Dict = None,
    ) -> Dict[str, Any]:
        """
        执行一条 Playbook 指令

        流程:
        1. 查找指令
        2. 校验 (方向、价格合理性)
        3. 通过 ManualOrderService 下单
        4. 注册 ManualHoldRegistry
        5. 更新 DB (executed=1, execution_price, execution_time)

        Args:
            instruction_id: 指令 ID
            acknowledge_risk: 用户确认风险
            custom_params: 用户自定义参数 (如修改止损)

        Returns:
            {"success": bool, "message": str, "order_result": dict}
        """
        # 1. 查找指令
        inst = self._find_instruction_by_id(instruction_id)
        if not inst:
            return {"success": False, "message": "指令不存在"}

        if inst.get("executed"):
            return {"success": False, "message": "指令已执行"}

        if inst.get("direction") == "SKIP":
            return {"success": False, "message": "SKIP 指令不可执行"}

        # 读取下单模式: 默认 simulate (shadow 安全); live 需在 unified_config.yaml playbook.execute_mode=live
        try:
            from app.core.config_container import get_config
            _exec_mode = get_config().get_dotted("playbook.execute_mode", "simulate")
        except Exception:
            _exec_mode = "simulate"

        # ── P0 (2026-06-22): simulate 模式创建虚拟仓位 (不调 broker) ──
        # 原设计: simulate+live_broker 拒绝下单. 但 shadow_only 阶段需要采集数据,
        # 不应依赖 broker 环境参照. 改为: simulate 模式直接创建虚拟 trade_record,
        # 不调 ManualOrderService/executor, 与 ORB shadow 模式对齐.
        #
        # 安全保证: simulate 路径零 broker 调用, 不可能下到真实账户.
        # live 模式仍走 ManualOrderService → executor → Futu API.
        symbol = inst["symbol"]
        direction = inst["direction"]
        entry_price = custom_params.get("entry_price") if custom_params else inst.get("entry_price", 0)
        stop_loss = custom_params.get("stop_loss") if custom_params else inst.get("stop_loss", 0)
        take_profit = custom_params.get("take_profit") if custom_params else inst.get("take_profit", 0)
        position_size = custom_params.get("position_size") if custom_params else inst.get("position_size", 0)
        if not position_size or position_size <= 0:
            position_size = 1

        # 方向映射
        mos_direction = "LONG" if direction == "BUY" else "SHORT"

        # 关联投票决策 ID (用于 IC 追踪)
        # 优先用 playbook_instructions 表中已存储的 decision_id (由 VotingCoordinator 写入)
        # fallback: 查 signal_snapshots (兼容旧数据)
        _matched_decision_id = inst.get("decision_id", "") or ""
        if not _matched_decision_id:
            try:
                from app.analysis.signal_ic_tracker import _get_db_path as _get_snap_path
                import sqlite3 as _sqlite3
                _snap_conn = _sqlite3.connect(str(_get_snap_path()))
                _snap_conn.row_factory = _sqlite3.Row
                _trade_date = inst.get("date", "")
                _row = _snap_conn.execute(
                    "SELECT decision_id FROM signal_snapshots WHERE symbol = ? AND date(trade_date) = ? ORDER BY strength DESC LIMIT 1",
                    (symbol, _trade_date),
                ).fetchone()
                if _row:
                    _matched_decision_id = _row["decision_id"]
                    logger.info(f"[playbook] matched decision_id={_matched_decision_id} for {symbol} (from signal_snapshots fallback)")
                _snap_conn.close()
            except Exception as _e:
                logger.debug(f"[playbook] decision_id lookup failed: {_e}")

        if _exec_mode == "simulate":
            # ── simulate 路径: 创建虚拟仓位, 写 trade_records, 不调 broker ──
            import uuid as _uuid
            _shadow_order_id = f"shadow_playbook_{_uuid.uuid4().hex[:8]}"
            _now_iso = datetime.now().isoformat()

            # 写 trade_records (与 ORB shadow 模式对齐)
            try:
                from app.storage.trade_store import TradeStore
                from app.core.paths import get_trade_records_db
                import json as _json
                _ts = TradeStore(str(get_trade_records_db()))
                _trace_id = f"playbook-{symbol}-{_now_iso[:10]}"
                _ts.save_trade({
                    "trace_id": _trace_id,
                    "trade_date": _now_iso[:10],
                    "symbol": symbol,
                    "direction": mos_direction,
                    "entry_time": _now_iso,
                    "exit_time": "",
                    "entry_price": float(entry_price) if entry_price else 0.0,
                    "exit_price": 0,
                    "quantity": int(position_size),
                    "realized_pnl": 0,
                    "pnl_pct": 0,
                    "exit_reason": "",
                    "holding_hours": 0,
                    "signal_strength": int(inst.get("confidence", 0) * 100) if inst.get("confidence") else 0,
                    "signal_confidence": float(inst.get("confidence", 0)),
                    "futu_order_id": _shadow_order_id,
                    "source_type": "PLAYBOOK_SHADOW",
                    "entry_reason": "playbook_shadow",
                    "owner_strategy": "playbook_shadow",
                    "market_regime": inst.get("market_regime", ""),
                    "mode": "simulate",
                    "trade_mode": "simulate",
                    "signal_id": _trace_id,
                    "decision_id": _matched_decision_id,
                    "metadata_json": _json.dumps({
                        "playbook_instruction_id": instruction_id,
                        "stop_loss": float(stop_loss) if stop_loss else 0,
                        "take_profit": float(take_profit) if take_profit else 0,
                        "atr": _extract_atr_from_instruction(inst),
                        "thesis": inst.get("thesis", "")[:200],
                        "peak_price": float(entry_price) if entry_price else 0,  # 初始化peak = entry
                    }),
                    "signal_details": _json.dumps({
                        "source": "playbook_shadow",
                        "entry_price": float(entry_price) if entry_price else 0,
                        "stop_loss": float(stop_loss) if stop_loss else 0,
                        "take_profit": float(take_profit) if take_profit else 0,
                    }),
                })
                logger.info(f"[playbook] shadow trade_record 已写入: {symbol} {mos_direction} @ {entry_price}")
            except Exception as _tre:
                logger.warning(f"[playbook] shadow trade_record 写入失败 (不影响执行): {_tre}")

            # 2026-06-24 清理: shadow_trades 表写入已退役.
            # 原用途 (shadow A/B 对比 + IC 追踪) 已被 playbook_instructions.fwd_return_t1/t3/t5
            # + ic_tracker.by_source_type 取代. shadow_trades 只被旧 evolution/harness 读,
            # playbook OKR 链不读 → 写它是 vestigial conflation, 停写以保持逻辑干净.

            # 注册 ManualHoldRegistry (防止 auto-pipeline 干扰)
            try:
                from app.coordinator.manual_hold_registry import manual_hold_registry
                manual_hold_registry.register(
                    symbol=symbol,
                    direction=mos_direction,
                    quantity=int(position_size),
                    reason=f"Playbook shadow: {inst.get('thesis', '')[:60]}",
                )
            except Exception as e:
                logger.warning(f"ManualHoldRegistry register failed: {e}")

            # 更新 DB
            self.db.update_instruction_tracking(instruction_id, {
                "executed": 1,
                "execution_price": float(entry_price) if entry_price else 0,
                "execution_time": _now_iso,
                "outcome": "holding",
                "decision_id": _matched_decision_id,
            })
            self.db.save_user_feedback({
                "instruction_id": instruction_id,
                "action": "executed",
            })

            logger.info(f"Playbook shadow executed: {symbol} {direction} @ ${entry_price} (no broker)")
            return {
                "success": True,
                "message": f"shadow 执行 {symbol} {direction} (虚拟仓位, 不调 broker)",
                "execution_price": float(entry_price) if entry_price else 0,
                "mode": "simulate",
            }

        # ── broker 路径 (futu_simulate / live 统一): target_env 由 AUTO_TRADE_ACCOUNT 决定 ──
        # 2026-06-24 修复: 之前 futu_simulate/live 各写死 target_env, 与 switch_mode (写 AUTO_TRADE_ACCOUNT)
        # 不通 → 系统页切实盘时 playbook 仍在 sim. 现统一: target_env = AUTO_TRADE_ACCOUNT (sim|real).
        # 这样 switch_mode (AUTO_TRADE_ACCOUNT) 真正控制 playbook + 主 pipeline 两个自动路径.
        # execute_mode=simulate (虚拟离线) 仍走上面的虚拟分支; futu_simulate/live 都走此 broker 路径.
        import os as _os
        _auto_account = _os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()

        if _auto_account == "real":
            # 实盘: graduation gate 前置, 未毕业 fail-close 降级 sim
            try:
                from app.playbook.pipeline import _check_graduation_gate
                _gate = _check_graduation_gate()
            except Exception as _ge:
                logger.error(f"[playbook] graduation gate 异常 (fail-close → 降级 sim): {_ge}")
                return self._execute_via_dual_broker(
                    inst, instruction_id, symbol, direction, entry_price, stop_loss,
                    take_profit, position_size, mos_direction, _matched_decision_id,
                    target_env="sim",
                )
            if not _gate.get("graduated"):
                logger.warning(f"[playbook] AUTO_TRADE_ACCOUNT=real 但未毕业, 降级 sim: {_gate.get('reason')}")
                return self._execute_via_dual_broker(
                    inst, instruction_id, symbol, direction, entry_price, stop_loss,
                    take_profit, position_size, mos_direction, _matched_decision_id,
                    target_env="sim",
                )
            return self._execute_via_dual_broker(
                inst, instruction_id, symbol, direction, entry_price, stop_loss,
                take_profit, position_size, mos_direction, _matched_decision_id,
                target_env="real",
            )

        # AUTO_TRADE_ACCOUNT=sim → 模拟盘真实下单 (futu_simulate, OKR 验证)
        return self._execute_via_dual_broker(
            inst, instruction_id, symbol, direction, entry_price, stop_loss,
            take_profit, position_size, mos_direction, _matched_decision_id,
            target_env="sim",
        )

    def _execute_via_dual_broker(
        self, inst, instruction_id, symbol, direction, entry_price, stop_loss,
        take_profit, position_size, mos_direction, matched_decision_id, target_env,
    ):
        """sim/live 共用执行路径 (Phase 2 无缝统一, 2026-06-24).

        无缝命门: futu_simulate (target_env=sim) 与 live (target_env=real) 走完全相同的代码,
        只差 target_env. 策略逻辑/仓位/记录全一致, sim→live 零行为分叉.

        - target_env=sim  → source_type=PLAYBOOK_SIM,  mode=futu_simulate, trade_mode=simulate
        - target_env=real → source_type=PLAYBOOK_LIVE, mode=live,          trade_mode=live
        """
        import uuid as _uuid
        import json as _json
        from app.core.models import Order, Direction, OrderType
        from app.execution.dual_broker import get_dual_broker

        _is_live = (target_env == "real")

        # LIVE_TRIAL 试运行 (Phase 2): target_env=real 且 live_trial 开启时仓位×0.5,
        # 对冲 sim→live 不可见的滑点 gap. 验证 sim PnL≈live PnL (10-20笔) 后用户关 live_trial 放全量.
        if _is_live:
            try:
                from app.core.config_container import get_config as _get_cfg
                _live_trial = _get_cfg().get_dotted("playbook.live_trial", True)
            except Exception:
                _live_trial = True
            if _live_trial and position_size and position_size > 1:
                position_size = max(1, int(position_size * 0.5))
                logger.info(f"[playbook] LIVE_TRIAL 试运行: {symbol} 仓位×0.5 → {position_size}")
        _source_type = "PLAYBOOK_LIVE" if _is_live else "PLAYBOOK_SIM"
        _mode_label = "live" if _is_live else "futu_simulate"
        _trade_mode = "live" if _is_live else "simulate"
        _owner = "playbook_live" if _is_live else "playbook_sim"

        # Phase 3 (2026-06-24) 源头拦截: 下单前查 ManualHoldRegistry.
        # 若该 symbol 被手动持有 (实盘), playbook 跳过 — 避免同 symbol 在 open_positions
        # (symbol PK) 上 manual(real, owner=manual) 与 sim(owner=auto) 互相覆盖导致仓位孤儿化.
        # 这是 flat-switch 下最安全的同账户隔离方式, 无需深改 open_positions 主键.
        try:
            from app.coordinator.manual_hold_registry import manual_hold_registry
            if manual_hold_registry.is_manual_held(symbol):
                logger.info(f"[playbook] {symbol} 被手动持有, 跳过 {_mode_label} 执行 (避免同 symbol 冲突)")
                return {
                    "success": False,
                    "message": f"{symbol} 被手动持有, 跳过 (避免与实盘手动仓位冲突)",
                    "skipped_manual_held": True,
                }
        except Exception as _mhe:
            logger.warning(f"[playbook] ManualHoldRegistry 检查异常 (放行): {_mhe}")

        # Phase 3 (2026-06-24, decision "sim 也守 PDT"): 下单前查 PDT 权限.
        # sim/live 统一查 (is_pdt_account: equity≥25k 豁免, <25k 守 ≤3/5日).
        # 确保 sim 验证的是 PDT 合规策略, 避免 sim 达标 live(<25k) 跑不了的假达标.
        try:
            from app.risk.pdt_guard import get_pdt_guard
            _pdt = get_pdt_guard()
            _allowed, _pdt_reason, _pdt_level = _pdt.check_trade_permission()
            if not _allowed:
                logger.warning(f"[playbook] {symbol} {_mode_label} 被 PDT 拦截: {_pdt_reason}")
                return {
                    "success": False,
                    "message": f"{symbol} 被 PDT 拦截: {_pdt_reason}",
                    "skipped_pdt": True,
                }
        except Exception as _pe:
            logger.warning(f"[playbook] PDT 检查异常 (放行): {_pe}")

        _order = Order(
            symbol=symbol,
            direction=Direction.LONG if direction == "BUY" else Direction.SHORT,
            order_type=OrderType.MARKET,
            quantity=int(position_size) if position_size else 1,
            price=0.0,  # 市价单
        )
        _order.metadata = {"playbook_instruction_id": instruction_id, "source": f"playbook_{_mode_label}"}

        # 下单 (sim 或 real, 同一方法)
        try:
            _dual = get_dual_broker()
            _result = _dual.submit_order(_order, target_env=target_env)
        except Exception as _e:
            logger.error(f"[playbook] {_mode_label} 下单异常 {symbol}: {_e}")
            return {"success": False, "message": f"{_mode_label} 下单异常: {_e}"}

        if not _result.get("success"):
            logger.error(f"[playbook] {_mode_label} 下单失败 {symbol}: {_result.get('error')}")
            return {"success": False, "message": f"{_mode_label} 下单失败: {_result.get('error')}"}

        _futu_id = _result.get("futu_order_id", "")
        _fill = _result.get("filled_price", 0) or float(entry_price) or 0
        _now_iso = datetime.now().isoformat()
        _order_id = f"{_mode_label}_{_uuid.uuid4().hex[:8]}"

        # 写 trade_records (真实成交价, source_type 按 target_env 区分)
        try:
            from app.storage.trade_store import TradeStore
            from app.core.paths import get_trade_records_db
            _ts = TradeStore(str(get_trade_records_db()))
            _trace_id = f"playbook-{_mode_label}-{symbol}-{_now_iso[:10]}"
            _ts.save_trade({
                "trace_id": _trace_id,
                "trade_date": _now_iso[:10],
                "symbol": symbol,
                "direction": mos_direction,
                "entry_time": _now_iso,
                "exit_time": "",
                "entry_price": float(_fill),
                "exit_price": 0,
                "quantity": int(position_size),
                "realized_pnl": 0,
                "pnl_pct": 0,
                "exit_reason": "",
                "holding_hours": 0,
                "signal_strength": int(inst.get("confidence", 0) * 100) if inst.get("confidence") else 0,
                "signal_confidence": float(inst.get("confidence", 0)),
                "futu_order_id": _futu_id or _order_id,
                "source_type": _source_type,
                "entry_reason": f"playbook_{_mode_label}",
                "owner_strategy": _owner,
                "market_regime": inst.get("market_regime", ""),
                "mode": _mode_label,
                "trade_mode": _trade_mode,
                "signal_id": _trace_id,
                "decision_id": matched_decision_id,
                "metadata_json": _json.dumps({
                    "playbook_instruction_id": instruction_id,
                    "stop_loss": float(stop_loss) if stop_loss else 0,
                    "take_profit": float(take_profit) if take_profit else 0,
                    "atr": _extract_atr_from_instruction(inst),
                    "thesis": inst.get("thesis", "")[:200],
                    "peak_price": float(_fill),
                }),
                "signal_details": _json.dumps({
                    "source": f"playbook_{_mode_label}",
                    "entry_price": float(_fill),
                    "stop_loss": float(stop_loss) if stop_loss else 0,
                    "take_profit": float(take_profit) if take_profit else 0,
                    "futu_order_id": _futu_id,
                }),
            })
            logger.info(f"[playbook] {_mode_label} trade_record 已写入: {symbol} @ {_fill} futu_id={_futu_id}")
        except Exception as _tre:
            logger.warning(f"[playbook] {_mode_label} trade_record 写入失败: {_tre}")

        # 注册 ManualHoldRegistry (防 auto pipeline 干扰)
        try:
            from app.coordinator.manual_hold_registry import manual_hold_registry
            manual_hold_registry.register(
                symbol=symbol, direction=mos_direction, quantity=int(position_size),
                reason=f"Playbook {_mode_label}: {inst.get('thesis', '')[:60]}",
            )
        except Exception as _e:
            logger.warning(f"ManualHoldRegistry register failed: {_e}")

        # 更新 DB — execution_price 用真实成交价
        self.db.update_instruction_tracking(instruction_id, {
            "executed": 1,
            "execution_price": float(_fill),
            "execution_time": _now_iso,
            "outcome": "holding",
            "decision_id": matched_decision_id,
        })
        self.db.save_user_feedback({"instruction_id": instruction_id, "action": "executed"})

        _env_desc = "实盘真实成交" if _is_live else "模拟盘真实成交"
        logger.info(f"Playbook {_mode_label} executed: {symbol} {direction} @ ${_fill} ({_env_desc}, futu_id={_futu_id})")

        # live 兜底: 挂 broker STOP 单 (服务断连时仍止损; sim 跳过由规则引擎兜底)
        if _is_live and stop_loss and float(stop_loss) > 0:
            try:
                _dual.place_protective_stop(
                    symbol, int(position_size) if position_size else 1,
                    mos_direction, float(stop_loss), target_env="real",
                )
            except Exception as _se:
                logger.warning(f"[playbook] live STOP 兜底失败 {symbol}: {_se}")

        return {
            "success": True,
            "message": f"{_mode_label} 执行 {symbol} {direction} @ ${_fill} ({_env_desc})",
            "execution_price": float(_fill),
            "mode": _mode_label,
            "futu_order_id": _futu_id,
        }

    # ========== 跳过/反馈接口 ==========

    def skip_instruction(
        self,
        instruction_id: int,
        reason: str = "",
        custom_reason: str = "",
    ) -> Dict[str, Any]:
        """
        跳过一条指令并记录原因

        Args:
            instruction_id: 指令 ID
            reason: 预设原因 (risk_too_high / unfamiliar_sector / similar_position / insufficient_capital)
            custom_reason: 自定义原因文本

        Returns:
            {"success": bool, "message": str}
        """
        inst = self._find_instruction_by_id(instruction_id)
        if not inst:
            return {"success": False, "message": "指令不存在"}

        skip_reason = custom_reason or reason or "用户主动跳过"

        # 更新 DB
        self.db.update_instruction_tracking(instruction_id, {
            "outcome": "skipped",
        })

        # 保存反馈
        self.db.save_user_feedback({
            "instruction_id": instruction_id,
            "action": "skipped",
            "skip_reason": skip_reason,
        })

        logger.info(f"Playbook skipped: {inst['symbol']} reason={skip_reason}")
        return {"success": True, "message": f"已跳过 {inst['symbol']}"}

    def modify_instruction(
        self,
        instruction_id: int,
        modified_field: str,
        modified_value: str,
    ) -> Dict[str, Any]:
        """
        记录用户修改了指令的某个参数

        Args:
            instruction_id: 指令 ID
            modified_field: 修改的字段 (stop_loss / take_profit / position_size)
            modified_value: 修改后的值

        Returns:
            {"success": bool, "message": str}
        """
        inst = self._find_instruction_by_id(instruction_id)
        if not inst:
            return {"success": False, "message": "指令不存在"}

        original_value = str(inst.get(modified_field, ""))

        self.db.save_user_feedback({
            "instruction_id": instruction_id,
            "action": "modified",
            "modified_field": modified_field,
            "original_value": original_value,
            "modified_value": modified_value,
        })

        logger.info(f"Playbook modified: {inst['symbol']} {modified_field}={modified_value}")
        return {"success": True, "message": f"已修改 {modified_field}"}

    # ========== 内部方法 ==========

    def _find_instruction_by_id(self, instruction_id: int) -> Optional[Dict]:
        """在最近的指令中查找"""
        instructions = self.db.get_recent_instructions(limit=200)
        for inst in instructions:
            if inst["id"] == instruction_id:
                return inst
        return None

    def _get_active_patterns_summary(self) -> List[Dict]:
        """获取活跃 patterns 摘要"""
        try:
            patterns = self.db.get_patterns(status="active")
            return [{
                "pattern_key": p["pattern_key"],
                "conditions": p.get("conditions_json", []),
                "win_rate": p.get("win_rate", 0),
                "total_trades": p.get("total_trades", 0),
            } for p in patterns[:5]]
        except Exception:
            return []

    @staticmethod
    def _et_today() -> str:
        """获取 ET 时区的今日日期"""
        try:
            from app.web_dashboard.data_service import et_today
            return et_today()
        except ImportError:
            return date.today().isoformat()
