"""
钉钉推送通知器（FTSeek-US）

复用 FTSeek-HK 推送格式，实现：
- 信号触发推送
- 成交推送
- 平仓推送（含盈亏）
- 风控拦截推送
- 日报推送
"""
import os
import json
import time
import hmac
import hashlib
import base64
import logging
import urllib.parse
from datetime import datetime
from typing import Optional, List, Dict, Any

import requests
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量
_env_path = Path(__file__).resolve().parent.parent.parent / ".env.us"
if _env_path.exists():
    load_dotenv(_env_path)

logger = logging.getLogger(__name__)



def _get_contract_multiplier(symbol: str) -> int:
    """获取合约乘数: 美股期权=100, 股票=1"""
    parts = symbol.split(".")
    if len(parts) >= 2:
        ticker_part = parts[-1]
        if len(ticker_part) >= 15:
            return 100 if ("P" in ticker_part[6:10] or "C" in ticker_part[6:10]) else 1
    return 1


class DingTalkNotifier:
    """钉钉推送通知器"""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
    ):
        self.webhook_url = webhook_url or os.getenv("DINGTALK_WEBHOOK_URL_US", "")
        self.secret = secret or os.getenv("DINGTALK_SECRET_US", "")
        self.enabled = bool(self.webhook_url)

        if not self.enabled:
            logger.warning("[钉钉] webhook_url 未配置，推送已禁用")

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def notify_full_decision(self, final_decision):
        """
        推送完整决策过程 (分析师+辩论+交易员+风控)
        
        Args:
            final_decision: FinalDecision 对象
        """
        try:
            symbol = final_decision.symbol
            action = final_decision.action
            approved = final_decision.approved
            
            emoji = "✅" if approved else "🚫"
            action_cn = {"BUY": "买入", "SELL": "卖出", "HOLD": "观望"}.get(action, action)
            
            lines = [
                f"### [FTSeek] {emoji} AI决策报告 | 🇺🇸 美股\n\n",
                f"**📍 标的**: {symbol}\n\n",
                f"**📊 最终决策**: {action_cn}  {'(已通过)' if approved else '(风控否决)'}\n\n",
                f"---\n",
            ]
            
            # 分析师团队投票
            if final_decision.voting_result:
                vr = final_decision.voting_result
                lines.append(f"**🧠 分析师团队投票**\n")
                lines.append(f"- 综合评分: {vr.get('final_strength', 0)}/100")
                lines.append(f"- 方向: {vr.get('direction', 'NEUTRAL')}")
                lines.append(f"- 一致性: {vr.get('consensus_level', 0):.0%}")
                lines.append(f"")
                lines.append(f"**各模型观点**:")
                for ms in vr.get('model_scores', [])[:4]:
                    model_id = ms.get('model_id', '?')
                    strength = ms.get('strength', 0)
                    reasoning = ms.get('reasoning', '')
                    lines.append(f"- {model_id}: {strength}分 - {reasoning}")
            
            # 研究员辩论
            if final_decision.debate_summary:
                ds = final_decision.debate_summary
                lines.append(f"")
                lines.append(f"**🗣️ 研究员辩论**")
                lines.append(f"- 多头: {ds.bull_case.core_thesis}")
                if ds.bull_case.supporting_points:
                    lines.append(f"  - {ds.bull_case.supporting_points[0]}")
                lines.append(f"- 空头: {ds.bear_case.core_thesis}")
                if ds.bear_case.supporting_points:
                    lines.append(f"  - {ds.bear_case.supporting_points[0]}")
                lines.append(f"- 分歧度: {ds.divergence_score:.0%}")
                lines.append(f"- 建议: {ds.recommendation}")
            
            # 交易员决策
            if final_decision.trade_decision:
                td = final_decision.trade_decision
                lines.append(f"")
                lines.append(f"**🎯 交易员决策**")
                lines.append(f"- 操作: {td.action}")
                lines.append(f"- 强度: {td.strength}/100")
                lines.append(f"- 信心: {td.confidence:.0%}")
                lines.append(f"- 建议仓位: {td.position_size:.0%}")
                if td.stop_loss > 0:
                    lines.append(f"- 止损: ${td.stop_loss:.2f}")
                if td.take_profit:
                    tp_str = " / ".join([f"${p:.2f}" for p in td.take_profit[:2]])
                    lines.append(f"- 止盈: {tp_str}")
                if td.reasoning:
                    lines.append(f"- 理由: {td.reasoning[:100]}")
            
            # 风控审查
            if final_decision.veto_result:
                vr = final_decision.veto_result
                lines.append(f"")
                lines.append(f"**🛡️ 风控审查**")
                lines.append(f"- 风险评分: {vr.risk_score:.2f}")
                if vr.vetoed and vr.reasons:
                    lines.append(f"- 否决原因:")
                    for reason in vr.reasons[:3]:
                        lines.append(f"  - {reason}")
            
            # 风险提示
            if final_decision.trade_decision and final_decision.trade_decision.risk_warnings:
                lines.append(f"")
                lines.append(f"**⚠️ 风险提示**:")
                for warning in final_decision.trade_decision.risk_warnings[:3]:
                    lines.append(f"- {warning}")
            
            lines.append(f"")
            lines.append(f"> 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
            
            title = f"[AI决策] {symbol} {action_cn}"
            self._send_markdown(title, "\n".join(lines))
            
        except Exception as e:
            logger.error(f"[钉钉] notify_full_decision 异常: {e}")
    
    def notify_signal(
        self,
        symbol: str,
        direction: str,
        strength: int,
        confidence: float,
        reasons: List[str],
        price: float = 0,
        consensus_level: float = 0,
    ):
        """信号触发推送"""
        emoji = "📈" if direction == "LONG" else "📉"
        dir_cn = "做多" if direction == "LONG" else "做空"
        strength_bar = self._strength_bar(strength)

        lines = [
            f"### [FTSeek] {emoji} 信号触发 | 🇺🇸 美股\n\n",
            f"**📍 标的**: {symbol}\n\n",
            f"**📊 方向**: {dir_cn}  **📈 价格**: ${price:.2f}\n\n",
            f"**🧠 信号强度**: {strength}/100  {strength_bar}\n\n",
            f"**🎯 置信度**: {confidence:.0%}  **🤝 模型一致性**: {consensus_level:.0%}\n\n",
            f"---\n",
        ]

        if reasons:
            lines.append(f"**📝 分析依据**:\n")
            for r in reasons[:3]:
                lines.append(f"- {r}")

        lines.append(f"\n> 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

        self._send_markdown(f"[信号] {symbol} {dir_cn}", "\n".join(lines))

    def notify_order_filled(self, order, position, voting_result: Dict = None):
        """成交推送 (钉钉 + 飞书) - 带来源标注"""
        try:
            from app.core.models import Direction
            dir_cn = "买入" if order.direction == Direction.LONG else "卖出"
            symbol = order.symbol

            # 止损止盈信息
            sl_info = f"${position.initial_sl:.2f}" if position.initial_sl > 0 else "N/A"
            tp_info = ""
            if position.take_profit_levels:
                tp_prices = [f"${lv['price']:.2f}" for lv in position.take_profit_levels]
                tp_info = " / ".join(tp_prices[:3])

            # 决策关联信息 (从 position.metadata 获取)
            meta = getattr(position, "metadata", {}) or {}
            signal_strength = meta.get("signal_strength", 0)
            signal_confidence = meta.get("signal_confidence", 0.0)
            strategy = meta.get("strategy_id", "voting_llm")
            source_type = meta.get("source_type", "MAIN_PIPELINE")  # 来源类型
            
            # 来源中文标注
            source_cn = {
                "MAIN_PIPELINE": "主线策略 (LLM投票)",
                "SCALPING": "Scalping (剥头皮)",
                "MANUAL": "人工交易",
            }.get(source_type, source_type)

            # 强度进度条
            strength_bar = self._strength_bar(signal_strength) if signal_strength > 0 else ""

            # 钉钉推送
            lines = [
                f"### [FTSeek-US] ✅ 成交通知 | 🇺🇸 美股\n\n",
                f"**📍 标的**: {symbol}  **📊 方向**: {dir_cn}\n\n",
                f"**📈 成交价**: ${order.avg_fill_price or order.price:.2f}\n\n",
                f"**📦 数量**: {order.quantity:.0f} 股\n\n",
                f"**📡 来源**: {source_cn}\n\n",
                f"---\n\n",
                f"**🛡️ 止损价**: {sl_info}\n\n",
                f"**🎯 止盈目标**: {tp_info or 'N/A'}\n\n",
            ]
            
            # 决策依据
            if signal_strength > 0:
                lines.append(f"**🧠 决策依据**:\n")
                lines.append(f"- 信号强度: {signal_strength}/100 {strength_bar}")
                lines.append(f"- 置信度: {signal_confidence:.0%}")
                lines.append(f"- 策略: {strategy}")
            
            # 投票详情
            if voting_result:
                lines.append(f"")
                lines.append(f"**投票详情**:")
                lines.append(f"- 最终评分: {voting_result.get('final_strength', 50)}")
                lines.append(f"- 一致性: {voting_result.get('consensus_level', 0):.0%}")
                model_scores = voting_result.get('model_scores', [])
                if model_scores:
                    for ms in model_scores[:4]:
                        lines.append(f"  - {ms.get('model_id', '?')}: {ms.get('strength', 0)}")

            lines.append(f"")
            lines.append(f"> 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

            self._send_markdown(f"[成交] {symbol} {dir_cn}", "\n".join(lines))
            
            # 飞书推送
            try:
                from app.notifier.feishu import notify_order_filled as feishu_notify
                feishu_notify(
                    symbol=symbol,
                    direction="BUY" if order.direction == Direction.LONG else "SELL",
                    filled_price=order.avg_fill_price or order.price,
                    quantity=int(order.quantity),
                    stop_loss=position.initial_sl,
                    take_profit=[lv['price'] for lv in (position.take_profit_levels or [])],
                    signal_strength=signal_strength,
                    signal_confidence=signal_confidence,
                    voting_result=voting_result,
                    strategy=strategy,
                    source_type=source_type,  # P0 修复: 传递来源标注
                )
            except Exception as e:
                logger.error(f"[飞书] 成交通知推送失败: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"[钉钉] notify_order_filled 异常: {e}", exc_info=True)

    def notify_exit(self, position, reason, pnl: float, voting_result: Dict = None):
        """平仓推送 (钉钉 + 飞书) - 带来源标注"""
        try:
            from app.core.models import ExitReason, Direction
            symbol = position.symbol
            dir_cn = "多头" if position.direction == Direction.LONG else "空头"

            pnl_sign = "+" if pnl >= 0 else ""
            pnl_emoji = "💰" if pnl >= 0 else "🔴"

            reason_cn = {
                ExitReason.STOP_LOSS: "触发止损",
                ExitReason.TAKE_PROFIT: "触发止盈",
                ExitReason.TRAILING_STOP: "移动止损",
                ExitReason.TIME_STOP: "时间止损",
                ExitReason.MANUAL: "手动平仓",
                ExitReason.RISK_CUT: "风控强平",
            }.get(reason, str(reason))

            multiplier = _get_contract_multiplier(position.symbol)
            pnl_pct = pnl / (position.avg_entry_price * position.size * multiplier) if position.avg_entry_price * position.size * multiplier > 0 else 0

            # 决策关联信息
            meta = getattr(position, "metadata", {}) or {}
            signal_strength = meta.get("signal_strength", 0)
            signal_confidence = meta.get("signal_confidence", 0.0)
            max_drawdown = meta.get("max_drawdown", 0.0)
            source_type = meta.get("source_type", "MAIN_PIPELINE")  # 来源类型
            
            # 来源中文标注
            source_cn = {
                "MAIN_PIPELINE": "主线策略 (LLM投票)",
                "SCALPING": "Scalping (剥头皮)",
                "MANUAL": "人工交易",
            }.get(source_type, source_type)

            # 钉钉推送
            lines = [
                f"### [FTSeek-US] {pnl_emoji} 平仓通知 | 🇺🇸 美股\n\n",
                f"**📍 标的**: {symbol}  **📊 方向**: {dir_cn}\n\n",
                f"**📉 平仓原因**: {reason_cn}\n\n",
                f"**📡 来源**: {source_cn}\n\n",
                f"**📈 入场价**: ${position.avg_entry_price:.2f}  **📉 出场价**: ${position.exit_price:.2f}\n\n",
                f"**⏳ 持仓时长**: {position.holding_hours:.1f}h\n\n",
                f"---\n\n",
                f"**💰 实现盈亏**: {pnl_sign}${pnl:.2f} ({pnl_sign}{pnl_pct:.2%})\n\n",
            ]
            
            # 决策回顾
            if signal_strength > 0:
                strength_bar = self._strength_bar(signal_strength)
                lines.append(f"**🧠 决策回顾**:\n")
                lines.append(f"- 原信号强度: {signal_strength}/100 {strength_bar}")
                lines.append(f"- 原置信度: {signal_confidence:.0%}")
            
            # 风险分析
            if max_drawdown != 0:
                lines.append(f"")
                lines.append(f"**风险分析**:")
                lines.append(f"- 持仓期间最大回撤: {max_drawdown:.2%}")

            lines.append(f"")
            lines.append(f"> 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

            self._send_markdown(f"[平仓] {symbol} {pnl_sign}{pnl:.2f}", "\n".join(lines))
            
            # 飞书推送
            try:
                from app.notifier.feishu import notify_exit as feishu_notify
                feishu_notify(
                    symbol=symbol,
                    direction="LONG" if position.direction == Direction.LONG else "SHORT",
                    entry_price=position.avg_entry_price,
                    exit_price=position.exit_price,
                    quantity=int(position.size),
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    exit_reason=reason.value if hasattr(reason, 'value') else str(reason),
                    holding_hours=position.holding_hours,
                    signal_strength=signal_strength,
                    signal_confidence=signal_confidence,
                    max_drawdown=max_drawdown,
                    source_type=source_type,  # P0 修复: 传递来源标注
                )
            except Exception as e:
                logger.debug(f"[飞书] 平仓通知推送失败: {e}")
                
        except Exception as e:
            logger.debug(f"[钉钉] notify_exit 异常: {e}")

    def notify_risk_blocked(self, symbol: str, reason: str):
        """风控拦截推送"""
        lines = [
            f"### [FTSeek] ⚠️ 风控拦截 | 🇺🇸 美股\n\n",
            f"**📍 标的**: {symbol}\n\n",
            f"**🚫 拦截原因**: {reason}\n\n",
            f"> 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        ]
        self._send_markdown(f"[风控] {symbol} 被拦截", "\n".join(lines))

    def notify_daily_summary(self, stats: Dict[str, Any]):
        """日报推送"""
        trades = stats.get("total_trades", 0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        total_pnl = stats.get("total_pnl", 0)
        win_rate = wins / trades if trades > 0 else 0
        holding_count = stats.get("holding_count", 0)

        pnl_sign = "+" if total_pnl >= 0 else ""
        pnl_emoji = "📊"

        lines = [
            f"## {pnl_emoji} FTSeek-US 日报",
            f"",
            f"**日期**: {datetime.now().strftime('%Y-%m-%d')}",
            f"**当前持仓**: {holding_count} 只",
            f"**今日交易**: {trades} 笔（{wins}盈/{losses}亏）",
            f"**胜率**: {win_rate:.0%}",
            f"**今日盈亏**: {pnl_sign}${total_pnl:.2f}",
        ]

        positions_info = stats.get("positions_info", [])
        if positions_info:
            lines.append(f"")
            lines.append(f"**当前持仓明细**:")
            for p in positions_info[:5]:
                pnl_str = f"{'+' if p.get('pnl_pct', 0) >= 0 else ''}{p.get('pnl_pct', 0):.2%}"
                lines.append(f"- {p['symbol']}: {pnl_str}")

        lines.append(f"")
        lines.append(f"> 生成时间: {datetime.now().strftime('%H:%M')}")

        self._send_markdown("FTSeek-US 日报", "\n".join(lines))

    def notify_system_alert(self, title: str, message: str, level: str = "WARN"):
        """系统告警推送"""
        emoji = {"INFO": "ℹ️", "WARN": "⚠️", "CRITICAL": "🚨"}.get(level, "⚠️")
        lines = [
            f"## {emoji} FTSeek-US 系统告警",
            f"",
            f"**级别**: {level}",
            f"**标题**: {title}",
            f"**详情**: {message}",
            f"",
            f"> 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        ]
        self._send_markdown(f"[{level}] {title}", "\n".join(lines))

    # ------------------------------------------------------------------
    # 内部发送
    # ------------------------------------------------------------------

    def _send_markdown(self, title: str, content: str):
        """发送 Markdown 消息"""
        if not self.enabled:
            logger.debug(f"[钉钉] 推送已禁用: {title}")
            return

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": content,
            },
        }

        try:
            url = self._build_signed_url()
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if result.get("errcode", 0) != 0:
                logger.warning(f"[钉钉] 发送失败: {result}")
            else:
                logger.info(f"[钉钉] 推送成功: {title}")
        except Exception as e:
            logger.error(f"[钉钉] 推送异常: {e}")

    def _build_signed_url(self) -> str:
        """构建带签名的 URL（安全模式）"""
        if not self.secret:
            return self.webhook_url

        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{self.webhook_url}&timestamp={timestamp}&sign={sign}"

    # ------------------------------------------------------------------
    # 兼容方法（修复日报推送报错）
    # ------------------------------------------------------------------

    def send_text(self, content: str) -> bool:
        """发送纯文本消息（兼容接口）"""
        if not self.enabled:
            logger.debug(f"[钉钉] 推送已禁用: {content[:50]}...")
            return False
        
        # 提取title (第一行或默认)
        lines = content.split("\n")
        title = "FTSeek-US 通知"
        for line in lines:
            line_stripped = line.strip()
            if line_stripped and not line_stripped.startswith('#'):
                title = line_stripped[:50]
                break
        
        # 使用纯文本消息类型 (更稳定)
        payload = {
            "msgtype": "text",
            "text": {
                "content": content,
            },
        }

        try:
            url = self._build_signed_url()
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if result.get("errcode", 0) != 0:
                logger.warning(f"[钉钉] send_text 失败: {result}")
                return False
            logger.info(f"[钉钉] send_text 成功: {title}")
            return True
        except Exception as e:
            logger.error(f"[钉钉] send_text 异常: {e}")
            return False

    def send_message(self, content: str) -> bool:
        """send_text 别名（兼容）"""
        return self.send_text(content)

    @staticmethod
    def _strength_bar(strength: int) -> str:
        """生成强度进度条"""
        filled = strength // 10
        bar = "█" * filled + "░" * (10 - filled)
        return f"`{bar}`"
