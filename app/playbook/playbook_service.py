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
from typing import Dict, List, Optional, Any
from datetime import datetime, date

from app.playbook.db import PlaybookDB

logger = logging.getLogger(__name__)

# Singleton
_service_instance = None


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

        # ── P0 (2026-06-19): paper/live 代码级隔离 gate ──
        # 审计发现: ManualOrderService 无条件 place_order, mode=simulate 仅是元数据,
        # 会被 web_dashboard/order/executor.py 覆写, broker 路由只看 TRADE_ENV.
        # 因此 simulate 模式下若 broker 是 live(TRADE_ENV=0), 必须拒绝 — 否则
        # paper 单会下到真实账户. 只允许 execute_mode=live 且 TRADE_ENV=0 一致组合.
        import os as _os
        _broker_live = str(_os.getenv("TRADE_ENV", "1")).strip() == "0"
        if _exec_mode != "live" and _broker_live:
            logger.error(
                f"[playbook] 🚫 paper/live 不一致: execute_mode={_exec_mode} 但 TRADE_ENV=0(live broker) "
                f"— 拒绝下单以防 paper 单下到真实账户. 若要 live 执行需显式设 playbook.execute_mode=live"
            )
            return {
                "success": False,
                "message": (
                    f"paper/live 隔离拒绝: execute_mode={_exec_mode} 与 live broker 不一致. "
                    f"若确认要在真实账户执行, 请在 unified_config.yaml 显式设 playbook.execute_mode=live"
                ),
            }

        # 2. 构建 ManualOrder
        try:
            from app.web_dashboard.manual_order_service import (
                get_manual_order_service, ManualOrder,
            )
        except ImportError:
            return {"success": False, "message": "ManualOrderService 不可用"}

        symbol = inst["symbol"]
        direction = inst["direction"]
        entry_price = custom_params.get("entry_price") if custom_params else inst.get("entry_price", 0)
        stop_loss = custom_params.get("stop_loss") if custom_params else inst.get("stop_loss", 0)
        take_profit = custom_params.get("take_profit") if custom_params else inst.get("take_profit", 0)
        position_size = custom_params.get("position_size") if custom_params else inst.get("position_size", 0)

        # 方向映射
        mos_direction = "LONG" if direction == "BUY" else "SHORT"
        mos_action = "BUY" if direction == "BUY" else "SELL"

        order = ManualOrder(
            symbol=symbol,
            direction=mos_direction,
            action=mos_action,
            quantity=int(position_size) if position_size else 1,
            order_type="MARKET",
            price=None,
            reason=f"Playbook: {inst.get('thesis', '')[:80]}",
            mode=_exec_mode,  # shadow 默认 simulate; 上线改 live 需在 yaml 显式配置
            stop_loss=float(stop_loss) if stop_loss else 0.0,
            take_profit=float(take_profit) if take_profit else 0.0,
            acknowledge_price_warning=acknowledge_risk,
        )

        # 3. 下单
        try:
            svc = get_manual_order_service()
            order_result = svc.submit_order(
                order,
                idempotency_key=f"playbook_{inst['date']}_{symbol}"
            )
        except Exception as e:
            logger.error(f"Playbook order failed for {symbol}: {e}")
            return {"success": False, "message": f"下单失败: {e}"}

        if not order_result.get("success"):
            return {
                "success": False,
                "message": order_result.get("error", "下单失败"),
                "order_result": order_result,
            }

        # 4. 注册 ManualHoldRegistry
        try:
            from app.coordinator.manual_hold_registry import manual_hold_registry
            manual_hold_registry.register(
                symbol=symbol,
                direction=mos_direction,
                quantity=int(position_size) if position_size else 1,
                reason=f"Playbook: {inst.get('thesis', '')[:60]}",
            )
        except Exception as e:
            logger.warning(f"ManualHoldRegistry register failed: {e}")

        # 5. 更新 DB
        execution_price = order_result.get("filled_price", entry_price)
        now = datetime.now().isoformat()
        self.db.update_instruction_tracking(instruction_id, {
            "executed": 1,
            "execution_price": execution_price,
            "execution_time": now,
            "outcome": "holding",
        })

        # 记录执行反馈
        self.db.save_user_feedback({
            "instruction_id": instruction_id,
            "action": "executed",
        })

        logger.info(f"Playbook executed: {symbol} {direction} @ ${execution_price}")
        return {
            "success": True,
            "message": f"成功执行 {symbol} {direction}",
            "order_result": order_result,
            "execution_price": execution_price,
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
