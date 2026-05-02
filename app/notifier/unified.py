"""
统一通知适配器 — 支持 DingTalk / Feishu 双通道

目的：
1. 修复飞书平仓通知缺股票代码的问题
2. 统一通知格式，消除双平台重复代码
3. 通过 TradingEnvironment 自动添加环境标识（[SIM]/[REAL]）
"""

import logging
import json
import hashlib
import hmac
import base64
import time
import urllib.parse
from typing import Optional, Dict, Any, List
from datetime import datetime

from app.core.trading_env import get_trading_env
from app.notifier.dingtalk import DingTalkNotifier

logger = logging.getLogger("ftseek.notifier.unified")


class UnifiedNotifier:
    """
    统一通知适配器
    
    同时支持钉钉和飞书推送，自动根据环境添加前缀。
    
    使用方式:
        notifier = UnifiedNotifier(dingtalk_url="...", feishu_url="...")
        notifier.send_trade_exit(
            code="US.MSFT",
            entry_price=672.46,
            exit_price=679.30,
            quantity=287,
            pnl=-1963.65,
            pnl_rate=-0.0102,
            hold_minutes=5,
            reason="PRE_CLOSE_EXIT",
        )
    """
    
    def __init__(self, dingtalk_url: str = None, dingtalk_secret: str = None,
                 feishu_url: str = None, feishu_secret: str = None):
        self.dingtalk_url = dingtalk_url
        self.dingtalk_secret = dingtalk_secret
        self.feishu_url = feishu_url
        self.feishu_secret = feishu_secret
        self._dingtalk = DingTalkNotifier(webhook_url=dingtalk_url, secret=dingtalk_secret)
    
    def send_trade_exit(self, code: str, entry_price: float, exit_price: float,
                        quantity: int, pnl: float, pnl_rate: float,
                        hold_minutes: float, reason: str,
                        signal_strength: float = None, confidence: float = None,
                        **kwargs) -> Dict[str, bool]:
        """
        发送平仓通知
        
        Args:
            code: 股票代码 (必填)
            entry_price: 开仓价
            exit_price: 平仓价
            quantity: 数量
            pnl: 盈亏金额
            pnl_rate: 盈亏比例
            hold_minutes: 持仓时长(分钟)
            reason: 平仓原因
            signal_strength: 信号强度 (可选)
            confidence: 置信度 (可选)
        
        Returns:
            {"dingtalk": True/False, "feishu": True/False}
        """
        results = {"dingtalk": False, "feishu": False}
        
        # 构建通知数据
        trade_data = {
            "code": code,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "quantity": quantity,
            "pnl": pnl,
            "pnl_rate": pnl_rate,
            "hold_minutes": hold_minutes,
            "reason": reason,
            "signal_strength": signal_strength,
            "confidence": confidence,
            **kwargs,
        }
        
        # 发送钉钉
        if self.dingtalk_url:
            results["dingtalk"] = self._send_dingtalk_exit(trade_data)
        
        # 发送飞书
        if self.feishu_url:
            results["feishu"] = self._send_feishu_exit(trade_data)
        
        return results

    # ===== 交易通知 (双通道) =====

    def send_text(self, content: str, at_all: bool = False) -> Dict[str, bool]:
        """发送纯文本到双通道"""
        results = {"dingtalk": False, "feishu": False}
        env_prefix = get_trading_env().get_notification_prefix()
        full_content = f"{env_prefix} {content}" if env_prefix else content

        if self._dingtalk.webhook_url:
            results["dingtalk"] = self._dingtalk.send_text(full_content, at_all=at_all)

        if self.feishu_url:
            try:
                results["feishu"] = self._post_feishu({
                    "msg_type": "text",
                    "content": {"text": full_content},
                })
            except Exception as e:
                logger.error(f"飞书文本推送失败: {e}")

        return results

    def send_open_position_message(self, signal, trade_result, position, consensus_result) -> Dict[str, bool]:
        """发送开仓通知到双通道"""
        results = {"dingtalk": False, "feishu": False}

        if self._dingtalk.webhook_url:
            results["dingtalk"] = self._dingtalk.send_open_position_message(
                signal, trade_result, position, consensus_result
            )

        if self.feishu_url:
            try:
                results["feishu"] = self._send_feishu_open(signal, trade_result, position, consensus_result)
            except Exception as e:
                logger.error(f"飞书开仓推送失败: {e}")

        return results

    def send_close_position_message(self, signal, trade_result, position, consensus_result) -> Dict[str, bool]:
        """发送平仓通知到双通道"""
        results = {"dingtalk": False, "feishu": False}

        if self._dingtalk.webhook_url:
            results["dingtalk"] = self._dingtalk.send_close_position_message(
                signal, trade_result, position, consensus_result
            )

        if self.feishu_url:
            try:
                results["feishu"] = self._send_feishu_close(signal, trade_result, position, consensus_result)
            except Exception as e:
                logger.error(f"飞书平仓推送失败: {e}")

        return results

    # ===== 飞书开仓卡片 =====

    def _send_feishu_open(self, signal, trade_result, position, consensus_result) -> bool:
        """构建并发送飞书开仓卡片"""
        env_prefix = get_trading_env().get_notification_prefix()
        action_text = "买入" if str(getattr(signal, 'action', '')).endswith('BUY') else "卖出"
        action_color = "green" if action_text == "买入" else "red"
        code = getattr(signal, 'code', '')
        quantity = getattr(signal, 'quantity', 0)
        price = getattr(signal, 'price', 0.0)
        order_id = getattr(trade_result, 'order_id', 'N/A')
        currency = "$" if code.startswith("US.") else "HK$"

        score = getattr(consensus_result, 'score', None) if consensus_result else None
        confidence = getattr(consensus_result, 'confidence', None) if consensus_result else None

        score_line = f"• 共识得分: {score:+.2f}\n" if score is not None else ""
        conf_line = f"• 置信度: {confidence:.2f}\n" if confidence is not None else ""

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": action_color,
                "title": {
                    "tag": "plain_text",
                    "content": f"{env_prefix} {code} {action_text}成交"
                }
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**订单 ID**: {order_id}\n"
                            f"• 数量: {quantity:,} 股\n"
                            f"• 成交价: {currency}{price:.2f}\n"
                            f"• 总金额: {currency}{price * quantity:,.2f}\n"
                            f"{score_line}"
                            f"{conf_line}"
                        )
                    }
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | FTSeek 交易系统"
                        }
                    ]
                }
            ]
        }

        return self._post_feishu({"msg_type": "interactive", "card": card})

    # ===== 飞书平仓卡片 =====

    def _send_feishu_close(self, signal, trade_result, position, consensus_result) -> bool:
        """构建并发送飞书平仓卡片"""
        env_prefix = get_trading_env().get_notification_prefix()
        code = getattr(signal, 'code', '')
        quantity = getattr(position, 'quantity', 0) if position else 0
        entry_price = getattr(position, 'entry_price', 0.0) if position else 0.0
        exit_price = getattr(signal, 'price', 0.0)
        pnl = getattr(trade_result, 'pnl', 0.0) or 0.0
        pnl_rate = (pnl / (entry_price * quantity)) if entry_price > 0 and quantity > 0 else 0.0
        currency = "$" if code.startswith("US.") else "HK$"

        pnl_color = "green" if pnl >= 0 else "red"
        pnl_icon = "💰" if pnl >= 0 else "🔴"

        reason = ""
        extra = getattr(signal, 'extra', {}) or {}
        if extra.get('stop_loss'):
            reason = "止损触发"
        elif extra.get('take_profit'):
            reason = "止盈触发"
        elif extra.get('partial_take_profit'):
            reason = f"分批止盈 ({extra.get('ratio', 0):.0%})"
        else:
            reason = getattr(signal, 'reason', '') or getattr(signal, 'strategy', '共识平仓')

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": pnl_color,
                "title": {
                    "tag": "plain_text",
                    "content": f"{pnl_icon} {env_prefix} {code} 平仓成交"
                }
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"• 开仓: {quantity:,} 股 @ {currency}{entry_price:.2f}\n"
                            f"• 平仓: {quantity:,} 股 @ {currency}{exit_price:.2f}\n"
                            f"• 盈亏: {currency}{pnl:+.2f} ({pnl_rate:+.2%})\n"
                            f"• 原因: {reason}"
                        )
                    }
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | FTSeek 交易系统"
                        }
                    ]
                }
            ]
        }

        return self._post_feishu({"msg_type": "interactive", "card": card})
    
    def _send_dingtalk_exit(self, data: Dict) -> bool:
        """发送钉钉平仓通知"""
        env_prefix = get_trading_env().get_notification_prefix()
        
        pnl_icon = "💰" if data["pnl"] >= 0 else "🔴"
        action_icon = "📊" if data["pnl"] >= 0 else "⚠️"
        
        hold_hours = data["hold_minutes"] / 60
        if hold_hours < 1:
            hold_str = f"{data['hold_minutes']:.0f} 分钟"
        else:
            hold_str = f"{hold_hours:.1f} 小时"
        
        currency = "$" if data["code"].startswith("US.") else "HK$"
        
        # 生成动态建议
        suggestion = self._generate_suggestion(data)
        
        text = (
            f"{action_icon} {env_prefix} FTSeek-US 平仓成交\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 **交易详情**\n"
            f"• **标的**: {data['code']}\n"
            f"• 开仓: {data['quantity']} 股 @ {currency}{data['entry_price']:.2f}\n"
            f"• 平仓: {data['quantity']} 股 @ {currency}{data['exit_price']:.2f}\n"
            f"• 盈亏: {currency}{data['pnl']:+.2f} ({data['pnl_rate']:+.2%})\n"
            f"• 持仓: {hold_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **平仓原因**\n"
            f"🛡️ {data['reason']}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 **市场分析**\n"
            f"{self._get_market_analysis(data)}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 **后续建议**\n"
            f"{suggestion}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        
        return self._post_dingtalk({
            "msgtype": "markdown",
            "markdown": {
                "title": f"{data['code']} 平仓通知",
                "text": text,
            }
        })
    
    def _send_feishu_exit(self, data: Dict) -> bool:
        """发送飞书平仓通知（修复：包含股票代码）"""
        env_prefix = get_trading_env().get_notification_prefix()
        
        pnl_icon = "💰" if data["pnl"] >= 0 else "🔴"
        
        hold_hours = data["hold_minutes"] / 60
        if hold_hours < 1:
            hold_str = f"{data['hold_minutes']:.0f}分钟"
        else:
            hold_str = f"{hold_hours:.1f}小时"
        
        currency = "$" if data["code"].startswith("US.") else "HK$"
        
        suggestion = self._generate_suggestion(data)
        
        # 飞书卡片格式
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "green" if data["pnl"] >= 0 else "red",
                "title": {
                    "tag": "plain_text",
                    "content": f"{pnl_icon} {env_prefix} FTSeek-US {data['code']} 平仓成交"
                }
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**📋 交易详情**\n"
                            f"• **标的**: {data['code']}\n"
                            f"• 开仓: {data['quantity']} 股 @ {currency}{data['entry_price']:.2f}\n"
                            f"• 平仓: {data['quantity']} 股 @ {currency}{data['exit_price']:.2f}\n"
                            f"• 盈亏: {currency}{data['pnl']:+.2f} ({data['pnl_rate']:+.2%})\n"
                            f"• 持仓: {hold_str}"
                        )
                    }
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**📊 平仓原因**: 🛡️ {data['reason']}"
                    }
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**📈 市场分析**\n{self._get_market_analysis(data)}"
                    }
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**💡 后续建议**\n{suggestion}"
                    }
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')} | FTSeek 交易系统"
                        }
                    ]
                }
            ]
        }
        
        return self._post_feishu({
            "msg_type": "interactive",
            "card": card,
        })
    
    # ===== 动态归因建议 =====
    
    def _generate_suggestion(self, data: Dict) -> str:
        """
        基于 6 维度生成动态建议
        
        维度: pnl_rate, reason, signal_strength, confidence, hold_minutes, max_drawdown
        """
        pnl_rate = data["pnl_rate"]
        reason = data.get("reason", "")
        signal_strength = data.get("signal_strength")
        confidence = data.get("confidence")
        hold_minutes = data.get("hold_minutes", 0)
        code = data.get("code", "")
        
        suggestions = []
        
        # 1. 盈亏分析
        if pnl_rate >= 0.03:
            suggestions.append(f"✅ 盈利 {pnl_rate:+.2%}，策略执行良好")
        elif pnl_rate >= 0:
            suggestions.append(f"✅ 保本微利 {pnl_rate:+.2%}，风控有效")
        elif pnl_rate >= -0.02:
            suggestions.append(f"⚠️ 小幅亏损 {pnl_rate:+.2%}，在正常波动范围内")
        else:
            suggestions.append(f"🔴 亏损 {pnl_rate:+.2%}，需复盘入场信号质量")
        
        # 2. 平仓原因分析
        if reason == "STOP_LOSS":
            suggestions.append(f"📌 止损触发，建议检查: 入场时机是否过晚")
        elif reason == "PRE_CLOSE_EXIT":
            suggestions.append(f"📌 收盘平仓，{code} 后续可继续观察")
        elif reason == "TIME_STOP":
            suggestions.append(f"📌 超时平仓，建议评估该标的活跃度")
        elif reason == "TAKE_PROFIT":
            suggestions.append(f"📌 止盈触发，趋势判断正确")
        elif reason == "TRAILING_STOP":
            suggestions.append(f"📌 追踪止盈，利润锁定成功")
        elif reason == "RISK_CUT":
            suggestions.append(f"📌 风控减仓，资金保护优先")
        
        # 3. 信号质量分析
        if signal_strength is not None:
            if signal_strength >= 0.7:
                suggestions.append(f"📊 原信号强度高 ({signal_strength:.2f})，本次可能为随机波动")
            elif signal_strength <= 0.3:
                suggestions.append(f"📊 原信号强度偏低 ({signal_strength:.2f})，建议提高入场阈值")
        
        # 4. 持仓时间分析
        if hold_minutes < 10 and pnl_rate < 0:
            suggestions.append(f"⚠️ 持仓仅 {hold_minutes:.0f} 分钟即亏损，可能入场时机不佳")
        elif hold_minutes > 120 and pnl_rate > 0:
            suggestions.append(f"✅ 耐心持仓 {hold_minutes/60:.1f} 小时获盈利")
        
        return "\n".join(suggestions)
    
    def _get_market_analysis(self, data: Dict) -> str:
        """获取市场层面分析"""
        pnl_rate = data["pnl_rate"]
        
        if pnl_rate >= 0.03:
            return "✅ 交易盈利，策略执行正常"
        elif pnl_rate >= 0:
            return "✅ 交易保本，风险控制有效"
        elif pnl_rate >= -0.02:
            return "⚠️ 小幅亏损，在正常波动范围内"
        else:
            return "❌ 交易亏损，建议复盘分析原因"
    
    # ===== HTTP 推送 =====
    
    def _post_dingtalk(self, payload: dict) -> bool:
        """发送钉钉消息"""
        try:
            import requests
            url = self.dingtalk_url
            if self.dingtalk_secret:
                timestamp = str(round(time.time() * 1000))
                string_to_sign = f"{timestamp}\n{self.dingtalk_secret}"
                hmac_code = hmac.new(
                    self.dingtalk_secret.encode("utf-8"),
                    string_to_sign.encode("utf-8"),
                    digestmod=hashlib.sha256,
                ).digest()
                sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
                url = f"{url}&timestamp={timestamp}&sign={sign}"
            
            resp = requests.post(url, json=payload, timeout=10)
            result = resp.json()
            return result.get("errcode") == 0
        except Exception as e:
            logger.error(f"钉钉推送失败: {e}")
            return False
    
    def _post_feishu(self, payload: dict) -> bool:
        """发送飞书消息"""
        try:
            import requests
            url = self.feishu_url
            if self.feishu_secret:
                timestamp = str(int(time.time()))
                sign = self._feishu_sign(timestamp)
                url = f"{url}&timestamp={timestamp}&sign={sign}"
            
            resp = requests.post(url, json=payload, timeout=10)
            result = resp.json()
            return result.get("code") == 0 or result.get("StatusCode") == 0
        except Exception as e:
            logger.error(f"飞书推送失败: {e}")
            return False
    
    def _feishu_sign(self, timestamp: str) -> str:
        """生成飞书签名"""
        if not self.feishu_secret:
            return ""
        string_to_sign = f"{timestamp}\n{self.feishu_secret}"
        hmac_code = hashlib.sha256(string_to_sign.encode('utf-8')).digest()
        return base64.b64encode(hmac_code).decode('utf-8')


# ===== 全局便捷访问 =====

_unified_notifier: Optional[UnifiedNotifier] = None


def get_unified_notifier() -> UnifiedNotifier:
    """获取统一通知器实例"""
    global _unified_notifier
    if _unified_notifier is None:
        from app import config
        _unified_notifier = UnifiedNotifier(
            dingtalk_url=getattr(config, 'DINGTALK_WEBHOOK_URL', None),
            dingtalk_secret=getattr(config, 'DINGTALK_SECRET', None),
            feishu_url=getattr(config, 'FEISHU_WEBHOOK_URL', None),
            feishu_secret=getattr(config, 'FEISHU_SECRET', None),
        )
    return _unified_notifier
