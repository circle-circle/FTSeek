"""
消息模板模块
负责构建各种通知消息的模板
"""
from datetime import datetime
from typing import Optional, Dict, List


class MessageTemplates:
    """消息模板类"""
    
    @staticmethod
    def format_strategies(result) -> str:
        """格式化策略信息"""
        if not hasattr(result, 'agree_strategies'):
            return ""
        
        strategies = result.agree_strategies
        if not strategies:
            return ""
        
        return " + ".join(strategies)
    
    @staticmethod
    def format_risk_checks() -> str:
        """格式化风控检查信息"""
        return "✅ 风控检查通过"
    
    @staticmethod
    def calculate_hold_time(entry_time, exit_time) -> str:
        """计算持仓时间"""
        try:
            if isinstance(entry_time, str):
                entry_time = datetime.fromisoformat(entry_time.replace('Z', '+00:00'))
            if isinstance(exit_time, str):
                exit_time = datetime.fromisoformat(exit_time.replace('Z', '+00:00'))
            
            delta = exit_time - entry_time
            total_minutes = int(delta.total_seconds() / 60)
            
            if total_minutes < 60:
                return f"{total_minutes}分钟"
            elif total_minutes < 1440:
                hours = total_minutes / 60
                return f"{hours:.1f}小时"
            else:
                days = total_minutes / 1440
                return f"{days:.1f}天"
        except Exception:
            return "N/A"
    
    @staticmethod
    def format_trade_summary(position, exit_price, pnl, pnl_rate, hold_time) -> str:
        """格式化交易摘要"""
        summary_lines = [
            f"入场价：{position.entry_price:.3f}",
            f"出场价：{exit_price:.3f}",
            f"盈亏：{pnl:.2f} ({pnl_rate:+.2%})",
            f"持仓时间：{hold_time}",
        ]
        return "\n".join(summary_lines)
    
    @staticmethod
    def format_market_status(result) -> str:
        """格式化市场状态"""
        regime = getattr(result, 'market_regime', 'unknown')
        regime_icons = {
            'strong_up': '📈',
            'weak_up': '📊',
            'neutral': '➡️',
            'weak_down': '📉',
            'strong_down': '📉📉',
            'range_bound': '↔️',
            'high_volatility': '⚡',
        }
        regime_names = {
            'strong_up': '强上升',
            'weak_up': '弱上升',
            'neutral': '中性',
            'weak_down': '弱下跌',
            'strong_down': '强下跌',
            'range_bound': '震荡',
            'high_volatility': '高波动',
        }
        icon = regime_icons.get(regime, '❓')
        name = regime_names.get(regime, regime)
        return f"{icon} 市场状态：{name}"
    
    @staticmethod
    def get_risk_alert(signal) -> str:
        """获取风控提示"""
        extra = getattr(signal, 'extra', {}) or {}
        alerts = []
        
        if extra.get('stop_loss'):
            alerts.append("⚠️ 止损触发")
        elif extra.get('take_profit'):
            alerts.append("✅ 止盈触发")
        elif extra.get('partial_take_profit'):
            alerts.append("📊 分批止盈")
        
        return "\n".join(alerts) if alerts else ""
    
    @staticmethod
    def get_close_reason(signal, extra: dict) -> str:
        """获取平仓原因"""
        if extra.get('stop_loss'):
            return "止损平仓"
        elif extra.get('take_profit'):
            return "止盈平仓"
        elif extra.get('partial_take_profit'):
            ratio = extra.get('ratio', 1.0)
            return f"分批止盈 ({ratio:.0%})"
        elif extra.get('trailing_stop'):
            return "追踪止盈"
        else:
            return "策略信号平仓"
    
    @staticmethod
    def get_close_market_analysis(signal, extra: dict, pnl_rate: float) -> str:
        """获取平仓市场分析"""
        analysis_lines = []
        
        if pnl_rate > 0.05:
            analysis_lines.append("✅ 盈利超过 5%，表现优秀")
        elif pnl_rate > 0.02:
            analysis_lines.append("✅ 盈利超过 2%，符合预期")
        elif pnl_rate > 0:
            analysis_lines.append("✅ 小幅盈利")
        elif pnl_rate > -0.03:
            analysis_lines.append("⚠️ 小幅亏损，在可控范围内")
        else:
            analysis_lines.append("⚠️ 触发止损，执行纪律")
        
        return "\n".join(analysis_lines)
    
    @staticmethod
    def get_next_action_suggestion(signal, pnl_rate: float, extra: dict) -> str:
        """获取下一步操作建议"""
        if pnl_rate > 0.05:
            return "💡 建议：保持观察，等待新信号"
        elif pnl_rate > 0:
            return "💡 建议：继续监控，关注趋势延续"
        elif pnl_rate > -0.03:
            return "💡 建议：等待反弹机会"
        else:
            return "💡 建议：冷静分析，避免情绪化交易"
    
    @staticmethod
    def format_open_position_content(signal, trade_result, position, consensus_result) -> str:
        """格式化开仓消息内容"""
        code = signal.code
        action = signal.action.value if hasattr(signal.action, 'value') else str(signal.action)
        action_icon = "🟢" if action == "BUY" else "🔴"
        
        extra = signal.extra or {}
        strategies_str = MessageTemplates.format_strategies(consensus_result) if consensus_result else signal.strategy
        
        content_lines = [
            f"{action_icon} **{code} {action}**",
            "",
            f"价格：{signal.price:.3f}",
            f"数量：{signal.quantity}股",
            f"策略：{strategies_str}",
            "",
            f"共识分数：{extra.get('score', 'N/A')}",
            f"置信度：{extra.get('confidence', 'N/A')}",
            f"市场状态：{extra.get('market_regime', 'N/A')}",
            "",
            f"订单：{trade_result.order_id}",
            "",
            MessageTemplates.format_risk_checks(),
        ]
        
        if signal.reason:
            content_lines.extend(["", "**信号原因**:", signal.reason])
        
        return "\n".join(content_lines)
    
    @staticmethod
    def format_close_position_content(signal, trade_result, position, consensus_result) -> str:
        """格式化平仓消息内容"""
        code = signal.code
        action = signal.action.value if hasattr(signal.action, 'value') else str(signal.action)
        
        extra = signal.extra or {}
        pnl = trade_result.pnl or 0
        entry_price = position.entry_price if position else 0
        pnl_rate = pnl / (entry_price * position.quantity) if position and position.quantity > 0 else 0
        
        pnl_icon = "✅" if pnl > 0 else "⚠️"
        
        hold_time = MessageTemplates.calculate_hold_time(
            getattr(position, 'entry_time', None),
            datetime.now().isoformat()
        )
        
        content_lines = [
            f"{pnl_icon} **{code} {action}**",
            "",
            MessageTemplates.format_trade_summary(position, signal.price, pnl, pnl_rate, hold_time),
            "",
            f"平仓原因：{MessageTemplates.get_close_reason(signal, extra)}",
            "",
            MessageTemplates.get_risk_alert(signal),
            MessageTemplates.get_close_market_analysis(signal, extra, pnl_rate),
            MessageTemplates.get_next_action_suggestion(signal, pnl_rate, extra),
        ]
        
        return "\n".join(content_lines)


# 辅助函数
def format_value(val, fmt=".1%", na="待接入", suffix=""):
    """格式化数值"""
    if val is None:
        return na
    try:
        return f"{val:{fmt}}{suffix}"
    except (ValueError, TypeError):
        return na


def format_icon(val, good_above=None, bad_below=None):
    """根据数值返回图标"""
    if val is None:
        return "❓"
    if good_above is not None and val >= good_above:
        return "✅"
    if bad_below is not None and val < bad_below:
        return "⚠️"
    return "➡️"
