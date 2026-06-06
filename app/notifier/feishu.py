#!/usr/bin/env python3
"""
飞书 Webhook 推送模块

用于 FTSeek-US 美股复盘推送 + 实时成交通知
"""
import logging
import requests
import hmac
import hashlib
import base64
import time
import os
from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量
_env_path = Path(__file__).resolve().parent.parent.parent / ".env.us"
if _env_path.exists():
    load_dotenv(_env_path)

logger = logging.getLogger("ftseek_us.feishu")
logger.propagate = False


class FeishuNotifier:
    """飞书 Webhook 推送器"""
    
    def __init__(self, webhook_url: str = None, secret: str = None):
        """
        初始化飞书推送器
        
        Args:
            webhook_url: 飞书 Webhook URL
            secret: 加签密钥
        """
        # 从环境变量读取
        self.webhook_url = webhook_url or os.getenv("FEISHU_WEBHOOK_URL_US")
        self.secret = secret or os.getenv("FEISHU_APP_SECRET")
        self.chat_id = os.getenv("FEISHU_CHAT_ID")
        self._send_times = []  # 限流记录
        
        logger.info(f"飞书推送器已初始化")
        logger.info(f"   Webhook: {'已配置' if self.webhook_url else '未配置'}")
        logger.info(f"   Chat ID: {'已配置' if self.chat_id else '未配置'}")
        
    def _send(self, payload: dict) -> bool:
        """执行发送"""
        if not self.webhook_url:
            logger.error("飞书 Webhook URL 未配置")
            return False
        
        try:
            # 发送
            headers = {"Content-Type": "application/json"}
            resp = requests.post(self.webhook_url, json=payload, headers=headers, timeout=10)
            result = resp.json()
            
            # 飞书返回格式：{"code":0,"msg":"success",...}
            if result.get("code") == 0 or result.get("StatusCode") == 0 or result.get("msg") == "success":
                logger.info(f"飞书推送成功")
                self._send_times.append(time.time())
                return True
            else:
                logger.error(f"飞书推送失败：{result}")
                return False
        except Exception as e:
            logger.error(f"飞书推送异常：{e}")
            import traceback
            traceback.print_exc()
            return False
    
    def send_text(self, content: str) -> bool:
        """
        发送文本消息

        Args:
            content: 消息内容

        Returns:
            是否成功
        """
        # P0 修复 2026-05-21: 飞书 Webhook 要求包含关键字 [FTSeek-US]
        if not content.startswith("[FTSeek-US]"):
            content = f"[FTSeek-US] {content}"

        payload = {
            "msg_type": "text",
            "content": {
                "text": content
            }
        }

        return self._send(payload)
    
    def send_post(self, title: str, content: List[List[Dict]]) -> bool:
        """
        发送富文本消息
        
        Args:
            title: 标题
            content: 内容 [[{"tag": "text", "text": "内容"}]]
        
        Returns:
            是否成功
        """
        payload = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": content
                    }
                }
            }
        }
        
        return self._send(payload)
    
    def send_daily_review(self, stats: Dict, review: Dict = None) -> bool:
        """
        发送每日复盘
        
        Args:
            stats: 统计数据
            review: 复盘详情
        
        Returns:
            是否成功
        """
        date = stats.get('date', datetime.now().strftime("%Y-%m-%d"))
        total_trades = stats.get('total_trades', 0)
        win_rate = stats.get('win_rate', 0) * 100
        total_pnl = stats.get('total_pnl', 0)
        
        pnl_icon = "🟢" if total_pnl > 0 else "🔴" if total_pnl < 0 else "⚪"
        
        content = f"""📊 FTSeek-US 每日复盘 ({date})

{pnl_icon} 交易统计
• 总交易：{total_trades} 笔
• 胜率：{win_rate:.1f}%
• 总盈亏：${total_pnl:.2f}

📈 策略表现
• confluence_v2: {stats.get('confluence_trades', 0)} 笔，胜率 {stats.get('confluence_win_rate', 0)*100:.1f}%
• supertrend: {stats.get('supertrend_trades', 0)} 笔，胜率 {stats.get('supertrend_win_rate', 0)*100:.1f}%
• sentiment_ai: {stats.get('sentiment_trades', 0)} 笔，胜率 {stats.get('sentiment_win_rate', 0)*100:.1f}%

📝 改进建议
{review.get('improvements', '暂无') if review else '暂无'}
"""
        
        return self.send_text(content)
    
    def send_weekly_review(self, report: Dict) -> bool:
        """
        发送每周复盘
        
        Args:
            report: 周报数据
        
        Returns:
            是否成功
        """
        week = report.get('week', '')
        total_trades = report.get('total_trades', 0)
        win_rate = report.get('win_rate', 0) * 100
        total_pnl = report.get('total_pnl', 0)
        sharpe = report.get('sharpe_ratio', 0)
        
        content = f"""📊 FTSeek-US 每周复盘 ({week})

📈 周度统计
• 总交易：{total_trades} 笔
• 胜率：{win_rate:.1f}%
• 总盈亏：${total_pnl:.2f}
• 夏普比率：{sharpe:.2f}

🎯 策略对比
• confluence_v2: {report.get('confluence_trades', 0)} 笔，胜率 {report.get('confluence_win_rate', 0)*100:.1f}%
• supertrend: {report.get('supertrend_trades', 0)} 笔，胜率 {report.get('supertrend_win_rate', 0)*100:.1f}%
• sentiment_ai: {report.get('sentiment_trades', 0)} 笔，胜率 {report.get('sentiment_win_rate', 0)*100:.1f}%

📝 下周计划
{report.get('next_week_plan', '暂无')}
"""
        
        return self.send_text(content)
    
    # ============================================================
    # 成交通知 (新增)
    # ============================================================
    
    def notify_full_decision(self, final_decision) -> bool:
        """
        推送完整AI决策过程
        
        Args:
            final_decision: FinalDecision 对象
        
        Returns:
            是否成功
        """
        try:
            symbol = final_decision.symbol
            action = final_decision.action
            approved = final_decision.approved
            
            emoji = "✅" if approved else "🚫"
            action_cn = {"BUY": "买入", "SELL": "卖出", "HOLD": "观望"}.get(action, action)
            
            content_lines = [f"{emoji} FTSeek-US AI决策报告"]
            content_lines.append(f"━━━━━━━━━━━━━━━━━━━━")
            content_lines.append(f"📋 决策概览")
            content_lines.append(f"• 标的: {symbol}")
            content_lines.append(f"• 决策: {action_cn} {'(已通过)' if approved else '(风控否决)'}")
            
            # 分析师投票
            if final_decision.voting_result:
                vr = final_decision.voting_result
                content_lines.append(f"")
                content_lines.append(f"📊 分析师团队投票")
                content_lines.append(f"• 综合评分: {vr.get('final_strength', 0)}/100")
                content_lines.append(f"• 方向: {vr.get('direction', 'NEUTRAL')}")
                content_lines.append(f"• 一致性: {vr.get('consensus_level', 0):.0%}")
                
                content_lines.append(f"")
                content_lines.append(f"各模型观点:")
                for ms in vr.get('model_scores', [])[:4]:
                    model_id = ms.get('model_id', '?')
                    strength = ms.get('strength', 0)
                    reasoning = ms.get('reasoning', '')
                    content_lines.append(f"• {model_id}: {strength}分 - {reasoning}")
            
            # 研究员辩论
            if final_decision.debate_summary:
                ds = final_decision.debate_summary
                content_lines.append(f"")
                content_lines.append(f"🗣️ 研究员辩论")
                content_lines.append(f"• 多头: {ds.bull_case.core_thesis}")
                if ds.bull_case.supporting_points:
                    content_lines.append(f"  - {ds.bull_case.supporting_points[0]}")
                content_lines.append(f"• 空头: {ds.bear_case.core_thesis}")
                if ds.bear_case.supporting_points:
                    content_lines.append(f"  - {ds.bear_case.supporting_points[0]}")
                content_lines.append(f"• 分歧度: {ds.divergence_score:.0%}")
                content_lines.append(f"• 建议: {ds.recommendation}")
            
            # 交易员决策
            if final_decision.trade_decision:
                td = final_decision.trade_decision
                content_lines.append(f"")
                content_lines.append(f"🎯 交易员决策")
                content_lines.append(f"• 操作: {td.action}")
                content_lines.append(f"• 强度: {td.strength}/100")
                content_lines.append(f"• 信心: {td.confidence:.0%}")
                content_lines.append(f"• 仓位: {td.position_size:.0%}")
                if td.stop_loss > 0:
                    content_lines.append(f"• 止损: ${td.stop_loss:.2f}")
                if td.take_profit:
                    tp_str = " / ".join([f"${p:.2f}" for p in td.take_profit[:2]])
                    content_lines.append(f"• 止盈: {tp_str}")
                if td.reasoning:
                    content_lines.append(f"• 理由: {td.reasoning[:100]}")
            
            # 风控审查
            if final_decision.veto_result:
                vr = final_decision.veto_result
                content_lines.append(f"")
                content_lines.append(f"🛡️ 风控审查")
                content_lines.append(f"• 风险评分: {vr.risk_score:.2f}")
                if vr.vetoed and vr.reasons:
                    content_lines.append(f"• 否决原因:")
                    for reason in vr.reasons[:3]:
                        content_lines.append(f"  - {reason}")
            
            # 风险提示
            if final_decision.trade_decision and final_decision.trade_decision.risk_warnings:
                content_lines.append(f"")
                content_lines.append(f"⚠️ 风险提示:")
                for warning in final_decision.trade_decision.risk_warnings[:3]:
                    content_lines.append(f"• {warning}")
            
            content_lines.append(f"━━━━━━━━━━━━━━━━━━━━")
            content_lines.append(f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
            
            content = "\n".join(content_lines)
            return self.send_text(content)
            
        except Exception as e:
            logger.error(f"[飞书] notify_full_decision 异常: {e}")
            return False
    
    def notify_order_filled(
        self,
        symbol: str,
        direction: str,
        filled_price: float,
        quantity: int,
        stop_loss: float = 0,
        take_profit: List[float] = None,
        signal_strength: int = 0,
        signal_confidence: float = 0,
        voting_result: Dict = None,
        strategy: str = "",
        source_type: str = "MAIN_PIPELINE",  # P0 修复: 来源标注
    ) -> bool:
        """
        发送成交通知
        
        Args:
            symbol: 股票代码
            direction: 方向 (BUY/SELL)
            filled_price: 成交价格
            quantity: 成交数量
            stop_loss: 止损价
            take_profit: 止盈目标列表
            signal_strength: 信号强度 (0-100)
            signal_confidence: 信号置信度 (0-1)
            voting_result: 投票结果 (含各模型评分)
            strategy: 策略名称
        
        Returns:
            是否成功
        """
        dir_cn = "买入" if direction == "BUY" or direction == "LONG" else "卖出"
        dir_emoji = "📈" if direction == "BUY" or direction == "LONG" else "📉"
        
        # 来源标注
        source_cn = {
            "MAIN_PIPELINE": "主线策略 (LLM投票)",
            "SCALPING": "Scalping (剥头皮)",
            "MANUAL": "人工交易",
            "POSITION_SYNC": "🔄 历史仓位同步 (启动时发现)",
        }.get(source_type, source_type)
        
        # 止损止盈信息
        sl_info = f"${stop_loss:.2f}" if stop_loss > 0 else "N/A"
        tp_info = " / ".join([f"${p:.2f}" for p in (take_profit or [])[:3]]) or "N/A"
        
        # 决策关联信息
        decision_info = ""
        if signal_strength > 0:
            strength_bar = self._strength_bar(signal_strength)
            decision_info = f"""
📊 决策依据
• 信号强度: {signal_strength}/100 {strength_bar}
• 置信度: {signal_confidence:.0%}
• 策略: {strategy or 'voting_llm'}"""
        
        # 投票详情
        voting_info = ""
        if voting_result:
            consensus = voting_result.get('consensus_level', 0)
            voting_info = f"""
🗳️ 投票详情
• 最终评分: {voting_result.get('final_strength', 50)}
• 一致性: {consensus:.0%}"""
            
            # 各模型评分
            model_scores = voting_result.get('model_scores', [])
            if model_scores:
                voting_info += "\n• 模型评分:"
                for ms in model_scores[:4]:
                    voting_info += f"\n  - {ms.get('model_id', '?')}: {ms.get('strength', 0)}"
        
        content = f"""{dir_emoji} FTSeek-US 成交通知
━━━━━━━━━━━━━━━━━━━━
📋 交易详情
• 标的: {symbol}
• 来源: {source_cn}
• 方向: {dir_cn}
• 成交价: ${filled_price:.2f}
• 数量: {quantity} 股
• 止损价: {sl_info}
• 止盈目标: {tp_info}
{decision_info}
{voting_info}
━━━━━━━━━━━━━━━━━━━━
⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        
        return self.send_text(content)
    
    def notify_exit(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        quantity: int,
        pnl: float,
        pnl_pct: float,
        exit_reason: str,
        holding_hours: float = 0,
        signal_strength: int = 0,
        signal_confidence: float = 0,
        max_drawdown: float = 0,
        source_type: str = "MAIN_PIPELINE",  # P0 修复: 来源标注
    ) -> bool:
        """
        发送平仓通知
        
        Args:
            symbol: 股票代码
            direction: 方向
            entry_price: 入场价
            exit_price: 出场价
            quantity: 数量
            pnl: 盈亏金额
            pnl_pct: 盈亏比例
            exit_reason: 平仓原因
            holding_hours: 持仓时长 (小时)
            signal_strength: 原信号强度
            signal_confidence: 原信号置信度
            max_drawdown: 持仓期间最大回撤
        
        Returns:
            是否成功
        """
        pnl_sign = "+" if pnl >= 0 else ""
        pnl_emoji = "💰" if pnl >= 0 else "🔴"
        
        dir_cn = "多头" if direction == "LONG" else "空头"
        
        # 来源标注
        source_cn = {
            "MAIN_PIPELINE": "主线策略 (LLM投票)",
            "SCALPING": "Scalping (剥头皮)",
            "MANUAL": "人工交易",
            "POSITION_SYNC": "🔄 历史仓位同步 (启动时发现)",
        }.get(source_type, source_type)
        
        # 平仓原因中文
        reason_cn = {
            "STOP_LOSS": "触发止损",
            "TAKE_PROFIT": "触发止盈",
            "TRAILING_STOP": "移动止损",
            "TIME_STOP": "时间止损",
            "MANUAL": "手动平仓",
            "RISK_CUT": "风控强平",
        }.get(exit_reason, exit_reason)
        
        # 决策回顾
        decision_review = ""
        if signal_strength > 0:
            strength_bar = self._strength_bar(signal_strength)
            decision_review = f"""
📊 决策回顾
• 原信号强度: {signal_strength}/100 {strength_bar}
• 原置信度: {signal_confidence:.0%}"""
        
        # 风险分析
        risk_analysis = ""
        if max_drawdown != 0:
            risk_analysis = f"""
⚠️ 风险分析
• 持仓期间最大回撤: {max_drawdown:.2%}"""
        
        # 后续建议 (动态归因分析)
        suggestion = self._generate_suggestion(
            pnl, pnl_pct, exit_reason,
            signal_strength=signal_strength,
            signal_confidence=signal_confidence,
            holding_hours=holding_hours,
            max_drawdown=max_drawdown,
        )
        
        content = f"""{pnl_emoji} FTSeek-US 平仓成交
━━━━━━━━━━━━━━━━━━━━
📋 交易详情
• 标的: {symbol}
• 来源: {source_cn}
• 开仓: {quantity} 股 @ ${entry_price:.2f}
• 平仓: {quantity} 股 @ ${exit_price:.2f}
• 盈亏: ${pnl_sign}{pnl:.2f} ({pnl_sign}{pnl_pct:.2%})
• 持仓: {holding_hours * 60:.0f}分钟
━━━━━━━━━━━━━━━━━━━━
📊 平仓原因
🛡️ {reason_cn}
{self._get_reason_detail(exit_reason, pnl_pct)}
━━━━━━━━━━━━━━━━━━━━
📈 市场分析
{'✅ 交易盈利' if pnl >= 0 else '❌ 交易亏损'}，{'盈利' if pnl >= 0 else '亏损'} {abs(pnl_pct):.2%}
{decision_review}
{risk_analysis}
━━━━━━━━━━━━━━━━━━━━
💡 后续建议
{suggestion}
━━━━━━━━━━━━━━━━━━━━
⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        
        return self.send_text(content)
    
    @staticmethod
    def _strength_bar(strength: int) -> str:
        """生成强度进度条"""
        filled = strength // 10
        bar = "█" * filled + "░" * (10 - filled)
        return f"`{bar}`"
    
    @staticmethod
    def _get_reason_detail(exit_reason: str, pnl_pct: float) -> str:
        """获取平仓原因详情"""
        details = {
            "STOP_LOSS": f"亏损达到 {abs(pnl_pct):.2%}，触发止损线\n控制风险，避免更大损失",
            "TAKE_PROFIT": f"盈利达到 {pnl_pct:.2%}，触发止盈线\n锁定收益，落袋为安",
            "TRAILING_STOP": "移动止损触发\n保护已有利润",
            "TIME_STOP": "持仓时间过长，触发时间止损\n避免资金占用",
            "MANUAL": "手动执行平仓操作",
            "RISK_CUT": "风控系统强制平仓\n保护账户资金安全",
            "PRE_CLOSE_EXIT": "收盘前强制平仓\n避免隔夜风险，落袋为安/止损出局",
        }
        return details.get(exit_reason, "")
    
    @staticmethod
    def _generate_suggestion(
        pnl: float, pnl_pct: float, exit_reason: str,
        signal_strength: int = 0, signal_confidence: float = 0,
        holding_hours: float = 0, max_drawdown: float = 0,
    ) -> str:
        """基于交易数据动态生成后续建议 (归因分析)"""
        lines = []
        
        # ===== 盈利场景 =====
        if pnl > 0:
            lines.append("✅ 本次交易盈利，策略执行正常")
            
            # 信号质量归因
            if signal_strength >= 70 and signal_confidence >= 0.7:
                lines.append("📌 高信号+高信心命中，策略校准良好，可继续保持")
            elif signal_strength < 50 and pnl_pct > 0.02:
                lines.append("📌 低信号但盈利，可能存在信号低估，建议关注类似形态")
            
            # 持仓时长归因
            if holding_hours > 0 and holding_hours < 0.1:  # < 6分钟
                lines.append("⚡ 持仓极短即盈利，关注是否止盈过于激进")
            
            # 回撤归因
            if max_drawdown > 0.03:
                lines.append(f"⚠️ 持仓期间曾回撤 {max_drawdown:.1%}，入场时机可优化")
            
            if not lines[1:]:
                lines.append("📌 可继续关注该标的的交易机会")
        
        # ===== 保本场景 =====
        elif pnl == 0:
            lines.append("✅ 本次交易保本，策略执行正常")
            lines.append("📌 关注手续费侵蚀，避免频繁保本交易")
        
        # ===== 亏损场景 =====
        else:
            abs_pct = abs(pnl_pct)
            
            if exit_reason == "STOP_LOSS":
                lines.append("🛡️ 止损触发是正常风控行为，纪律执行到位")
                if signal_strength >= 70:
                    lines.append(f"⚠️ 高信号({signal_strength}/100)但仍止损，建议复盘信号质量")
                if abs_pct > 0.03:
                    lines.append("📌 亏损幅度较大，建议检查入场价位是否追高")
                else:
                    lines.append("📌 亏损在可控范围内，继续观察")
            
            elif exit_reason in ("TIME_STOP", "PRE_CLOSE_EXIT"):
                lines.append(f"🕐 {exit_reason} 触发平仓")
                if holding_hours > 0 and holding_hours < 0.5:
                    lines.append("⚠️ 持仓时间极短，入场时机可能不佳，建议等待更明确信号")
                elif abs_pct < 0.01:
                    lines.append("📌 亏损微小，时间/收盘止损正常执行")
                else:
                    lines.append(f"📌 亏损 {abs_pct:.2%}，建议评估该策略的时效性假设")
            
            elif exit_reason == "TRAILING_STOP":
                lines.append("📉 移动止损触发，保护了部分利润")
                if signal_strength >= 70:
                    lines.append("📌 高信号入场但回落，关注趋势持续性判断")
            
            elif exit_reason == "RISK_CUT":
                lines.append("🚨 风控强平，说明市场出现异常波动")
                lines.append("⚠️ 建议检查是否有大盘系统性风险")
            
            elif exit_reason == "MANUAL":
                lines.append("👤 手动平仓")
                lines.append("📌 记录手动决策原因，用于后续策略优化")
            
            else:
                # 通用亏损建议 (fallback)
                if signal_confidence >= 0.7:
                    lines.append(f"⚠️ 高信心({signal_confidence:.0%})但亏损，模型可能过度自信")
                if abs_pct > 0.03:
                    lines.append("📌 亏损幅度较大，建议降低该策略的仓位权重")
                else:
                    lines.append("📌 检查信号质量和入场时机")
        
        return "\n".join(lines)


# 全局实例
_notifier: Optional[FeishuNotifier] = None


def get_notifier() -> FeishuNotifier:
    """获取推送器实例"""
    global _notifier
    if _notifier is None:
        _notifier = FeishuNotifier()
    return _notifier


def send_daily_review(stats: Dict, review: Dict = None) -> bool:
    """发送每日复盘"""
    return get_notifier().send_daily_review(stats, review)


def send_weekly_review(report: Dict) -> bool:
    """发送每周复盘"""
    return get_notifier().send_weekly_review(report)


def send_text(content: str) -> bool:
    """发送文本消息"""
    return get_notifier().send_text(content)


def notify_order_filled(**kwargs) -> bool:
    """发送成交通知"""
    return get_notifier().notify_order_filled(**kwargs)


def notify_exit(**kwargs) -> bool:
    """发送平仓通知"""
    return get_notifier().notify_exit(**kwargs)
