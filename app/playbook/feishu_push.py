"""
Playbook Feishu Push — 盘前指令推送 + 盘后追踪推送

推送时机:
  1. 08:30 ET 盘前: 今日交易指令 + 市场环境
  2. 16:30 ET 盘后: 今日追踪结果

Usage:
  from app.playbook.feishu_push import PlaybookPusher

  pusher = PlaybookPusher()
  pusher.push_morning_instructions("2026-06-18")
  pusher.push_evening_tracking("2026-06-18")
"""
import logging
from typing import Dict, List, Optional
from datetime import date

from app.playbook.db import PlaybookDB
from app.playbook.shadow_tracker import is_trading_day

logger = logging.getLogger(__name__)


class PlaybookPusher:
    """Playbook 飞书推送"""

    def __init__(self, playbook_db: PlaybookDB = None):
        self.db = playbook_db or PlaybookDB()

    def push_morning_instructions(self, trade_date: str = None, sync: bool = False) -> bool:
        """
        盘前推送: 今日交易指令 + 市场环境

        格式 (spec 7.2):
        📋 今日交易指令 (2026-06-18)
        环境: 🟢 RISK_ON (VIX 18.5, SPY $542 MA20上方)
        ━━━━━━━━━━━━━━━━━━━━━━━
        #1 US.NTES BUY LONG ✅ 72%
           入场 $119.50 → 止损 $114.80 → 止盈 $128.00
           50股 | R:R 1.8:1 | 风险 $235
           条件: insider_cluster_buy + pe_below_industry
        跳过: META(财报3天内)
        """
        if trade_date is None:
            trade_date = date.today().isoformat()

        if not is_trading_day(date.fromisoformat(trade_date)):
            logger.info(f"{trade_date} not a trading day, skip morning push")
            return False

        instructions = self.db.get_instructions_by_date(trade_date)
        if not instructions:
            logger.info(f"No instructions for {trade_date}, skip morning push")
            return False

        # 获取 regime: 优先实时评估 (regime_log 由已休眠的 in-app 生成任务写, DA 链不写 → 易陈旧;
        # assess() 已在调度环每轮运行, 取当前真值), 失败再回退到 (已过滤标记的) regime_log 缓存。
        regime_info = {}
        try:
            from app.playbook.market_regime import MarketRegimeGate
            _ctx = MarketRegimeGate().assess()
            regime_info = {
                "regime": _ctx.regime.value,
                "vix": _ctx.vix,
                "spy_price": _ctx.spy_price,
            }
        except Exception as _re:
            logger.debug(f"[playbook] 实时 regime 评估失败, 回退 regime_log 缓存: {_re}")
            regime_log = self.db.get_regime_log(days=1)
            regime_info = regime_log[0] if regime_log else {}

        # 构建消息
        message = self._format_morning_message(trade_date, instructions, regime_info)

        return self._send(message, dedup_key=f"playbook:morning:{trade_date}", sync=sync)

    def push_evening_tracking(self, trade_date: str = None) -> bool:
        """
        盘后推送: 今日追踪结果

        格式 (spec 7.2):
        📊 今日指令追踪 (2026-06-18)
        ━━━━━━━━━━━━━━━━━━━━━━━
        #1 NTES BUY LONG
           ✅ 执行 $120.10 | 持仓中 | 浮盈 +$30
           MFE +$135 | MAE -$45
        今日总结: 执行 2/3, 浮盈 +$110
        """
        if trade_date is None:
            trade_date = date.today().isoformat()

        if not is_trading_day(date.fromisoformat(trade_date)):
            return False

        instructions = self.db.get_instructions_by_date(trade_date)
        if not instructions:
            return False

        message = self._format_evening_message(trade_date, instructions)
        return self._send(message, dedup_key=f"playbook:evening:{trade_date}")

    # ========== 格式化 ==========

    def _format_morning_message(
        self, trade_date: str, instructions: List[Dict], regime_info: Dict
    ) -> str:
        """格式化盘前推送消息"""
        # 环境描述
        regime = str(regime_info.get("regime", "未知")).upper()  # 库里存小写 risk_on, 统一大写以匹配 emoji 表
        # or 0: .get(k,0) 仅挡键缺失, 挡不住键存在但值为 NULL/None (会令 {x:.1f} 崩溃)
        vix = regime_info.get("vix", 0) or 0
        spy = regime_info.get("spy_price", 0) or 0
        regime_emoji = {
            "RISK_ON": "🟢", "CAUTIOUS": "🟡",
            "RISK_OFF": "🔴", "NO_TRADE": "⛔",
        }.get(regime, "❓")

        lines = [
            f"📋 今日交易指令 ({trade_date})",
            f"环境: {regime_emoji} {regime}" + (f" (VIX {vix:.1f}, SPY ${spy:.0f})" if vix else ""),
            "━" * 20,
            "",
        ]

        # 活跃指令 (非 SKIP)
        active = [i for i in instructions if i.get("direction") != "SKIP"]
        skipped = [i for i in instructions if i.get("direction") == "SKIP"]

        for idx, inst in enumerate(active, 1):
            sym = inst.get("symbol", "?")
            direction = inst.get("direction", "?")
            confidence = inst.get("confidence") or 0  # 2026-07-13: NULL 值(键存在但为None)会显示 'None%', 与兄弟字段统一 or 0
            entry = inst.get("entry_price", 0) or 0
            sl = inst.get("stop_loss", 0) or 0
            tp = inst.get("take_profit", 0) or 0
            pos = inst.get("position_size", 0) or 0
            rr = inst.get("risk_reward", 0) or 0
            risk = inst.get("risk_amount", 0) or 0
            conditions = self._parse_conditions(inst.get("conditions_json"))
            horizon = inst.get("horizon") or "short"  # 2026-07-14: or guard for NULL horizon
            horizon_emoji = {"short": "⚡", "long": "🌙"}.get(horizon, "❓")

            conf_icon = "✅" if (confidence or 0) >= 70 else "⚠️" if (confidence or 0) >= 50 else "❌"
            dir_text = "BUY LONG" if direction == "BUY" else "SELL SHORT"

            lines.append(f"#{idx} {sym} {dir_text} {conf_icon} {confidence}% [{horizon_emoji} {horizon}]")
            if entry:
                lines.append(f"   入场 ${entry:.2f} → 止损 ${sl:.2f} → 止盈 ${tp:.2f}")
                lines.append(f"   {pos}股 | R:R {rr:.1f}:1 | 风险 ${risk:.0f}")
            if conditions:
                lines.append(f"   条件: {' + '.join(conditions[:4])}")
            lines.append("")

        # 跳过的
        if skipped:
            skip_parts = [f"{i.get('symbol', '?')}({i.get('skip_reason', 'N/A')})" for i in skipped]
            lines.append(f"跳过: {', '.join(skip_parts)}")

        return "\n".join(lines)

    def _format_evening_message(self, trade_date: str, instructions: List[Dict]) -> str:
        """格式化盘后追踪消息"""
        lines = [
            f"📊 今日指令追踪 ({trade_date})",
            "━" * 20,
            "",
        ]

        total_pnl = 0
        executed_count = 0

        for idx, inst in enumerate(instructions, 1):
            if inst.get("direction") == "SKIP":
                continue

            sym = inst.get("symbol", "?")
            direction = inst.get("direction", "?")
            executed = inst.get("executed", 0)
            outcome = inst.get("outcome", "")
            pnl = inst.get("pnl", 0)
            mfe = inst.get("max_favorable_excursion", 0)
            mae = inst.get("max_adverse_excursion", 0)
            horizon = inst.get("horizon") or "short"  # 2026-07-14: or guard for NULL horizon
            horizon_emoji = {"short": "⚡", "long": "🌙"}.get(horizon, "❓")

            dir_text = "BUY LONG" if direction == "BUY" else "SELL SHORT"
            lines.append(f"#{idx} {sym} {dir_text} [{horizon_emoji} {horizon}]")

            if executed:
                executed_count += 1
                total_pnl += pnl or 0
                exec_price = inst.get("execution_price", 0) or 0  # 2026-07-14: or guard for NULL
                status = "持仓中" if outcome == "holding" else outcome
                pnl_text = f"{'+' if pnl >= 0 else ''}${pnl:.0f}" if pnl else "-"
                lines.append(f"   ✅ 执行 ${exec_price:.2f} | {status} | {pnl_text}")
                if mfe or mae:
                    lines.append(f"   MFE +${mfe:.0f} | MAE ${mae:.0f}")
            else:
                shadow = inst.get("shadow_pnl") or 0
                lines.append(f"   ❌ 未执行 | 理论盈亏: {'+' if shadow >= 0 else ''}${shadow:.0f}")
            lines.append("")

        lines.append(f"今日总结: 执行 {executed_count}/{len(instructions)}, "
                      f"盈亏 {'+' if total_pnl >= 0 else ''}${total_pnl:.0f}")

        return "\n".join(lines)

    # ========== 内部方法 ==========

    def _send(self, message: str, dedup_key: str = None, sync: bool = False) -> bool:
        """发送飞书消息"""
        try:
            # 优先使用 PushHub
            from app.push import get_push_hub, PushMessage, Level, Category
            msg = PushMessage(
                title="📋 Playbook 交易指令",
                body=message,
                level=Level.NORMAL,
                category=Category.PLAN_PREMARKET,
                dedup_key=dedup_key or f"playbook:{date.today().isoformat()}",
            )
            ok = get_push_hub().push(msg, sync=sync)
            logger.info(f"Playbook push via PushHub: sent={ok} sync={sync}")
            return ok
        except ImportError:
            pass

        try:
            # 回退到 FeishuNotifier
            from app.notifier.feishu import send_text
            result = send_text(message)
            logger.info(f"Playbook push sent via FeishuNotifier: {result}")
            return result
        except ImportError:
            logger.warning("No push channel available for playbook")
            return False

    @staticmethod
    def _parse_conditions(val) -> List[str]:
        """解析 conditions_json"""
        if not val:
            return []
        if isinstance(val, list):
            return val
        try:
            import json
            return json.loads(val)
        except (ValueError, TypeError):
            return []
