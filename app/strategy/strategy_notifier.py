"""
FTSeek-US LLM 策略推送通知

将 LLM 生成的策略推送到钉钉/飞书。
包含策略详情、推理链、风险提示。

作者: FTSeek Team
日期: 2026-04-16
"""

import json
import logging
import hashlib
import hmac
import base64
import time
import urllib.parse
from datetime import datetime
from typing import List, Dict, Optional

import requests

logger = logging.getLogger("ftseek.strategy_notifier")


class StrategyNotifier:
    """策略推送通知器"""
    
    def __init__(self, webhook_url: str = None, secret: str = None):
        import os
        self.webhook_url = webhook_url or os.getenv("DINGTALK_WEBHOOK_URL_US", "")
        self.secret = secret or os.getenv("DINGTALK_SECRET_US", "")
        self.enabled = bool(self.webhook_url)
    
    def notify_strategy(self, strategy: Dict) -> bool:
        """推送单条策略到钉钉"""
        if not self.enabled:
            logger.warning("[策略通知] Webhook 未配置, 跳过推送")
            return False
        
        direction_emoji = {"LONG": "📈", "SHORT": "📉"}
        emoji = direction_emoji.get(strategy.get("direction", ""), "📊")
        
        direction_cn = {
            "LONG": "买入正股",
            "SHORT": "看跌(正股)",
        }.get(strategy.get("direction", ""), strategy.get("direction", ""))
        
        confidence_pct = int(strategy.get("confidence", 0) * 100)
        
        trigger_cn = {
            "immediate": "立即执行",
            "on_pullback": "回调入场",
            "on_breakout": "突破入场",
        }.get(strategy.get("trigger_condition", ""), "")
        
        # 推理链展示
        impact_chain = strategy.get("impact_chain", [])
        chain_text = ""
        if impact_chain:
            chain_text = "\n**传导链**: " + " → ".join(impact_chain[:4])
        
        # 止损止盈
        tp = strategy.get("take_profit_pct", 0) * 100
        sl = strategy.get("stop_loss_pct", 0) * 100
        hours = strategy.get("time_horizon_hours", 72)
        position = strategy.get("max_position_pct", 0) * 100
        
        msg = f"""### {emoji} LLM 策略信号 | {strategy.get("symbol", "")}

**方向**: {direction_cn} | **置信度**: {confidence_pct}%
**入场**: {trigger_cn} | **仓位**: {position:.0f}%
**止盈**: +{tp:.0f}% | **止损**: -{sl:.0f}% | **持有**: {hours}h

**推理**: {strategy.get("reasoning", "")}
{chain_text}

**关联事件**: {strategy.get("related_event", "")}

---
⚠️ 风险提示: 本策略由 LLM 自动生成, 仅供参考"""

        return self._send_markdown(
            title=f"LLM策略 {strategy.get('symbol', '')} {direction_cn}",
            text=msg,
        )
    
    def notify_batch(self, strategies: List[Dict]) -> int:
        """批量推送策略"""
        sent = 0
        for s in strategies:
            if self.notify_strategy(s):
                sent += 1
        return sent
    
    def notify_backtest_result(self, result: Dict) -> bool:
        """推送回测结果"""
        if not self.enabled:
            return False
        
        win_rate = result.get("win_rate", 0) * 100
        total = result.get("total_strategies", 0)
        
        msg = f"""### 📊 LLM 策略回测报告

**回测样本**: {total} 条策略
**胜率**: {win_rate:.1f}%
**平均盈亏**: {result.get("avg_pnl", 0)*100:.1f}%
**盈亏比**: {result.get("profit_factor", 0):.2f}
**最大回撤**: {result.get("max_drawdown", 0)*100:.1f}%

---
📝 注: 当前处于数据收集期, 待积累足够新闻后执行完整回测"""
        
        return self._send_markdown(title="LLM 策略回测报告", text=msg)
    
    def _send_markdown(self, title: str, text: str) -> bool:
        """发送钉钉 Markdown 消息"""
        try:
            url = self.webhook_url
            if self.secret:
                timestamp = str(int(time.time() * 1000))
                string_to_sign = f"{timestamp}\n{self.secret}"
                hmac_code = hmac.new(
                    self.secret.encode('utf-8'),
                    string_to_sign.encode('utf-8'),
                    digestmod='sha256'
                ).digest()
                sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
                url = f"{url}&timestamp={timestamp}&sign={sign}"
            
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": text,
                }
            }
            
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            if data.get("errcode", -1) == 0:
                logger.info(f"[策略通知] 推送成功: {title}")
                return True
            else:
                logger.error(f"[策略通知] 推送失败: {data}")
                return False
                
        except Exception as e:
            logger.error(f"[策略通知] 推送异常: {e}")
            return False
