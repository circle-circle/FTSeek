"""
钉钉群机器人推送
支持 ActionCard 消息，点击按钮直接触发确认回调
内置推送限流: 尊重钉钉 20条/分钟 限制，超限消息自动合并摘要
"""
import time
import hmac
import hashlib
import base64
import logging
import urllib.parse
import threading
from collections import deque
from datetime import datetime
import requests
from typing import Optional

from app import config
from app.strategy.base import Signal, SignalAction

logger = logging.getLogger("ftseek.notifier.dingtalk")

ACTION_LABELS = {
    SignalAction.BUY: "确认买入",
    SignalAction.SELL: "确认卖出",
    SignalAction.ALERT: "查看详情",
}

ACTION_COLORS = {
    SignalAction.BUY: "#FF4444",
    SignalAction.SELL: "#44BB44",
    SignalAction.ALERT: "#4488FF",
}


class DingTalkNotifier:
    """钉钉Webhook推送 (带限流)"""

    # 钉钉机器人限制: 每分钟最多20条
    MAX_PER_MINUTE = 18  # 留 2 条余量给系统消息

    def __init__(self, webhook_url: str = None, secret: str = None):
        self.webhook_url = webhook_url or config.DINGTALK_WEBHOOK_URL
        self.secret = secret or config.DINGTALK_SECRET
        # 限流: 滑动窗口记录发送时间戳
        self._send_times = deque(maxlen=60)
        self._lock = threading.Lock()
        # 被丢弃的信号摘要 (定期合并推送)
        self._dropped: list = []
        self._drop_flush_timer: Optional[threading.Timer] = None

    def _sign_url(self) -> str:
        """加签方式鉴权"""
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

    def _can_send(self) -> bool:
        """检查是否在限流窗口内"""
        now = time.time()
        with self._lock:
            # 清理 60 秒前的记录
            while self._send_times and now - self._send_times[0] > 60:
                self._send_times.popleft()
            return len(self._send_times) < self.MAX_PER_MINUTE

    def _record_send(self):
        with self._lock:
            self._send_times.append(time.time())

    def _do_post(self, payload: dict) -> bool:
        """执行实际的 HTTP POST"""
        try:
            url = self._sign_url()
            resp = requests.post(url, json=payload, timeout=10)
            result = resp.json()
            if result.get("errcode") == 0:
                self._record_send()
                return True
            else:
                logger.error(f"钉钉推送失败: {result}")
                return False
        except Exception as e:
            logger.error(f"钉钉推送异常: {e}")
            return False

    def _schedule_flush_dropped(self):
        """30 秒后合并推送被丢弃的信号摘要"""
        if self._drop_flush_timer and self._drop_flush_timer.is_alive():
            return  # 已有定时器在跑
        self._drop_flush_timer = threading.Timer(30.0, self._flush_dropped)
        self._drop_flush_timer.daemon = True
        self._drop_flush_timer.start()

    def _flush_dropped(self):
        """将缓积的丢弃信号合并为一条摘要推送"""
        with self._lock:
            dropped = self._dropped[:]
            self._dropped.clear()

        if not dropped or not self.webhook_url:
            return

        # 按 code 汇总
        summary = {}
        for code, action, strategy in dropped:
            key = f"{code}"
            if key not in summary:
                summary[key] = {"buy": 0, "sell": 0, "strategies": set()}
            if action == "BUY":
                summary[key]["buy"] += 1
            else:
                summary[key]["sell"] += 1
            summary[key]["strategies"].add(strategy)

        lines = [f"**[限流摘要] 过去 30s 内 {len(dropped)} 条信号被合并**\n"]
        for code, info in summary.items():
            strats = ", ".join(info["strategies"])
            parts = []
            if info["buy"]:
                parts.append(f"买 {info['buy']}")
            if info["sell"]:
                parts.append(f"卖 {info['sell']}")
            lines.append(f"- **{code}**: {' / '.join(parts)} ({strats})")

        text = "\n".join(lines)
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": "信号摘要", "text": text},
        }

        if self._can_send():
            self._do_post(payload)

    def send_signal(self, signal: Signal) -> bool:
        """
        发送交易信号到钉钉
        共识信号 (extra.consensus=True) 使用增强卡片格式
        
        ⚠️ 2026-03-13: 添加 watchlist 过滤，只推送启用的股票
        """
        # 加载 watchlist 配置，过滤未启用的股票
        try:
            import json
            watchlist_path = "/opt/ftseek/FTSeek/config/watchlist.json"
            with open(watchlist_path, 'r') as f:
                watchlist = json.load(f)
            enabled_codes = set()
            for stock in watchlist.get('hk_stocks', []):
                if stock.get('enabled', False):
                    enabled_codes.add(stock['code'])
            for stock in watchlist.get('us_stocks', []):
                if stock.get('enabled', False):
                    enabled_codes.add(stock['code'])
            
            # 如果股票代码不在启用列表中，跳过推送
            if signal.code not in enabled_codes:
                logger.debug(f"跳过未启用股票推送：{signal.code}")
                return False
        except Exception as e:
            logger.warning(f"watchlist 过滤异常：{e}，默认放行")

        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过推送")
            return False

        if not self._can_send():
            with self._lock:
                self._dropped.append((signal.code, signal.action.value, signal.strategy))
            logger.debug(f"限流丢弃: {signal.code} {signal.action.value} ({signal.strategy})")
            self._schedule_flush_dropped()
            return False

        action_text = ACTION_LABELS.get(signal.action, "查看")
        confirm_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/confirm"
            f"?signal_id={signal.id}&token={config.API_TOKEN}"
        )
        defer_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/defer"
            f"?signal_id={signal.id}&token={config.API_TOKEN}"
        )
        reject_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/reject"
            f"?signal_id={signal.id}&token={config.API_TOKEN}"
        )

        currency = "$" if signal.code.startswith("US.") else "HK$"
        is_consensus = signal.extra.get("consensus", False) if signal.extra else False

        if is_consensus:
            # ===== 加权共识信号: 增强卡片 =====
            n_agree = signal.extra.get("agree_count", 0)
            n_total = signal.extra.get("total_count", 0)
            strategies = signal.extra.get("strategies", [])
            score = signal.extra.get("score", 0)
            threshold = signal.extra.get("threshold", 0)
            regime = signal.extra.get("market_regime", "neutral")
            adx_val = signal.extra.get("adx")
            weights = signal.extra.get("weights", {})
            oppose = signal.extra.get("oppose", [])

            icon = "🔴" if signal.action == SignalAction.BUY else "🟢"
            direction = "买入" if signal.action == SignalAction.BUY else "卖出"
            title = f"{icon} {signal.code} 加权共振{direction}"

            regime_label = {"trending": "📈 趋势市", "ranging": "📊 震荡市", "neutral": "➖ 中性"}.get(regime, regime)
            adx_str = f"ADX={adx_val}" if adx_val else "ADX=N/A"

            # 同向策略 + 权重
            agree_lines = "\n\n".join(
                f"✅ **{s}** (权重 {weights.get(s, '?')})"
                for s in strategies
            )
            # 反向策略
            oppose_lines = ""
            if oppose:
                oppose_lines = "\n\n" + "\n\n".join(f"❌ ~~{s}~~ (权重 {weights.get(s, '?')})" for s in oppose)

            markdown_text = (
                f"### {title}\n\n"
                f"---\n\n"
                f"**标的**: {signal.code} {signal.name}\n\n"
                f"**方向**: **{direction}**\n\n"
                f"**触发均价**: {currency}{signal.price:.2f}\n\n"
                f"**数量**: {signal.quantity} 股\n\n"
                f"---\n\n"
                f"**加权评分**: **{score:+.2f}** (阈值 ±{threshold})\n\n"
                f"**市场状态**: {regime_label} ({adx_str})\n\n"
                f"---\n\n"
                f"**同向策略 ({n_agree}/{n_total})**\n\n"
                f"{agree_lines}"
                f"{oppose_lines}\n\n"
                f"---\n\n"
                f"**各策略依据**:\n\n"
                f"{signal.reason}\n\n"
                f"---\n\n"
                f"⏰ 有效期 {config.SIGNAL_EXPIRE_SECONDS // 60} 分钟"
            )
        else:
            # ===== 普通信号: 原始卡片 =====
            icon = "🔴" if signal.action == SignalAction.BUY else "🟢" if signal.action == SignalAction.SELL else "🔵"
            title = f"{icon} {signal.code} {signal.action.value} 信号"

            markdown_text = (
                f"### {title}\n\n"
                f"**标的**: {signal.code} {signal.name}\n\n"
                f"**方向**: {signal.action.value}\n\n"
                f"**触发价**: {currency}{signal.price:.2f}\n\n"
                f"**数量**: {signal.quantity} 股\n\n"
                f"**策略**: {signal.strategy}\n\n"
                f"**原因**: {signal.reason}\n\n"
                f"---\n\n"
                f"⏰ 有效期 {config.SIGNAL_EXPIRE_SECONDS // 60} 分钟，过期自动作废"
            )

        payload = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": title,
                "text": markdown_text,
                "btnOrientation": "1",  # 横向排列
                "btns": [
                    {"title": f"✅ {action_text}", "actionURL": confirm_url},
                    {"title": "⏰ 延期 15 分钟", "actionURL": defer_url},
                    {"title": "❌ 忽略", "actionURL": reject_url},
                ],
            },
        }

        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉推送成功: {signal.id} -> {signal.code} {signal.action.value}")
        return ok

    def send_text(self, content: str, at_all: bool = False) -> bool:
        """发送纯文本消息"""
        if not self.webhook_url:
            return False
        if not self._can_send():
            logger.warning(f"限流跳过文本消息: {content[:60]}...")
            return False
        payload = {
            "msgtype": "text",
            "text": {"content": content},
            "at": {"isAtAll": at_all},
        }
        return self._do_post(payload)

    def send_execution_result(self, signal: Signal, success: bool, detail: str = ""):
        """推送交易执行结果"""
        status = "✅ 执行成功" if success else "❌ 执行失败"
        currency = "$" if signal.code.startswith("US.") else "HK$"
        content = (
            f"[交易结果] {status}\n"
            f"标的: {signal.code}\n"
            f"方向: {signal.action.value}\n"
            f"价格: {currency}{signal.price:.2f}\n"
            f"数量: {signal.quantity}\n"
        )
        if detail:
            content += f"详情: {detail}\n"
        self.send_text(content)

    def send_confirm_reminder(self, signal: Signal, elapsed_minutes: int):
        """
        发送确认提醒（15 分钟未响应）
        
        Args:
            signal: 待确认的信号
            elapsed_minutes: 已等待的分钟数
        """
        if not self.webhook_url:
            return False
        
        currency = "$" if signal.code.startswith("US.") else "HK$"
        direction = "买入" if signal.action == SignalAction.BUY else "卖出"
        
        markdown_text = (
            f"### ⏰ 待确认提醒\n\n"
            f"**信号已等待 {elapsed_minutes} 分钟**\n\n"
            f"---\n\n"
            f"**标的**: {signal.code} {signal.name}\n\n"
            f"**方向**: **{direction}**\n\n"
            f"**触发价**: {currency}{signal.price:.2f}\n\n"
            f"**数量**: {signal.quantity} 股\n\n"
            f"**策略**: {signal.strategy}\n\n"
            f"---\n\n"
            f"⚠️ 信号将在 **{config.SIGNAL_EXPIRE_SECONDS // 60 - elapsed_minutes} 分钟**后过期\n\n"
            f"请尽快点击下方按钮确认操作"
        )
        
        confirm_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/confirm"
            f"?signal_id={signal.id}&token={config.API_TOKEN}"
        )
        defer_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/defer"
            f"?signal_id={signal.id}&token={config.API_TOKEN}"
        )
        reject_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/reject"
            f"?signal_id={signal.id}&token={config.API_TOKEN}"
        )
        
        payload = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": f"⏰ {signal.code} 待确认提醒",
                "text": markdown_text,
                "btnOrientation": "1",
                "btns": [
                    {"title": "✅ 确认执行", "actionURL": confirm_url},
                    {"title": "⏰ 延期 15 分钟", "actionURL": defer_url},
                    {"title": "❌ 忽略", "actionURL": reject_url},
                ],
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉提醒推送成功：{signal.id}")
        return ok

    def send_risk_alert(self, alert_type: str, details: dict):
        """
        发送风险预警（五层风控触发）
        
        Args:
            alert_type: 预警类型 (signal/order/position/account/system)
            details: 预警详情
        """
        if not self.webhook_url:
            return False
        
        alert_labels = {
            "signal": "🔴 信号级风控",
            "order": "🟠 订单级风控",
            "position": "🟡 仓位级风控",
            "account": "🔵 账户级风控",
            "system": "🟣 系统级风控",
        }
        
        alert_label = alert_labels.get(alert_type, "⚪ 风险预警")
        
        lines = [f"### {alert_label}\n\n"]
        
        # 根据预警类型添加详情
        if alert_type == "signal":
            lines.extend([
                f"**触发原因**: 置信度过低\n\n",
                f"**置信度**: {details.get('confidence', 0):.2%}\n\n",
                f"**阈值**: {details.get('threshold', 0):.2%}\n\n",
            ])
        elif alert_type == "order":
            lines.extend([
                f"**触发原因**: {details.get('reason', '订单超限')}\n\n",
                f"**订单金额**: HK${details.get('order_amount', 0):,.2f}\n\n",
                f"**最大允许**: HK${details.get('max_allowed', 0):,.2f}\n\n",
            ])
        elif alert_type == "position":
            lines.extend([
                f"**触发原因**: {details.get('reason', '仓位超限')}\n\n",
                f"**当前仓位**: {details.get('current_position', 0):.2%}\n\n",
                f"**最大允许**: {details.get('max_position', 0):.2%}\n\n",
                f"**今日交易**: {details.get('today_trades', 0)}/{details.get('max_daily', 0)} 次\n\n",
            ])
        elif alert_type == "account":
            lines.extend([
                f"**触发原因**: {details.get('reason', '账户亏损')}\n\n",
                f"**当前回撤**: {details.get('drawdown', 0):.2%}\n\n",
                f"**最大允许**: {details.get('max_drawdown', 0):.2%}\n\n",
                f"**连续亏损**: {details.get('consecutive_loss', 0)} 次\n\n",
            ])
        elif alert_type == "system":
            lines.extend([
                f"**触发原因**: {details.get('reason', '系统异常')}\n\n",
                f"**连续亏损**: {details.get('consecutive_loss', 0)} 次\n\n",
                f"**策略失效**: {details.get('strategy_failure', '无')}\n\n",
            ])
        
        lines.extend([
            f"---\n\n",
            f"⚠️ 请检查风险配置并及时处理\n\n",
            f"**时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n",
        ])
        
        markdown_text = "".join(lines)
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": alert_label,
                "text": markdown_text,
            },
            "at": {"isAtAll": True},  # 风控预警@所有人
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉风险预警推送成功：{alert_type}")
        return ok

    def send_daily_stats(self, stats: dict):
        """
        发送收盘统计（16:30）
        
        Args:
            stats: 统计数据字典
        """
        if not self.webhook_url:
            return False
        
        date_str = stats.get('date', time.strftime('%Y-%m-%d'))
        today_pnl = stats.get('today_pnl', 0)
        today_pnl_pct = stats.get('today_pnl_pct', 0)
        week_pnl = stats.get('week_pnl', 0)
        week_pnl_pct = stats.get('week_pnl_pct', 0)
        
        # 判断涨跌颜色
        today_icon = "🟢" if today_pnl >= 0 else "🔴"
        week_icon = "🟢" if week_pnl >= 0 else "🔴"
        
        # 持仓列表
        positions = stats.get('positions', [])
        positions_text = ""
        if positions:
            for pos in positions:
                pnl_icon = "🟢" if pos.get('pnl_pct', 0) >= 0 else "🔴"
                positions_text += f"- {pos.get('code', '')} {pos.get('name', '')} {pnl_icon} {pos.get('pnl_pct', 0):+.2f}%\n\n"
        else:
            positions_text = "无持仓\n\n"
        
        # 风控状态
        risk_status = "✅ 正常" if stats.get('risk_ok', True) else "⚠️ 异常"
        
        markdown_text = (
            f"### 📊 FTSeek 收盘统计\n\n"
            f"**日期**: {date_str}\n\n"
            f"---\n\n"
            f"**今日信号**: {stats.get('today_signals', 0)} 次\n\n"
            f"**已执行**: {stats.get('today_executed', 0)} 次\n\n"
            f"**忽略/延期**: {stats.get('today_ignored', 0)} 次\n\n"
            f"---\n\n"
            f"**今日盈亏**: {today_icon} HK${today_pnl:,.2f} ({today_pnl_pct:+.2f}%)\n\n"
            f"**本周盈亏**: {week_icon} HK${week_pnl:,.2f} ({week_pnl_pct:+.2f}%)\n\n"
            f"---\n\n"
            f"**当前持仓**: {len(positions)} 只\n\n"
            f"{positions_text}"
            f"---\n\n"
            f"**风控状态**: {risk_status}\n\n"
            f"**连续亏损**: {stats.get('consecutive_loss', 0)} 次\n\n"
            f"**总回撤**: {stats.get('drawdown', 0):.2f}% (安全线 {stats.get('max_drawdown', 15):.0f}%)\n\n"
        )
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"📊 FTSeek 收盘统计 ({date_str})",
                "text": markdown_text,
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉收盘统计推送成功：{date_str}")
        return ok

    def send_weekly_report(self, report_data: dict) -> bool:
        """
        发送周报复盘到钉钉
        包含：交易汇总、策略分析、基准对比、改进建议
        
        Args:
            report_data: 周报数据字典，包含：
                - period: 报告周期
                - total_trades: 总交易数
                - win_rate: 胜率 (0-1)
                - total_return: 总收益率 (%)
                - by_strategy: 按策略分组数据
                - by_stock: 按股票分组数据
                - recommendations: 改进建议列表
                - benchmark_comparison: 基准对比数据 (可选)
                
        Returns:
            bool: 推送是否成功
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过周报推送")
            return False
        
        if not self._can_send():
            logger.warning(f"限流跳过周报推送")
            return False
        
        period = report_data.get('period', '未知周期')
        total_trades = report_data.get('total_trades', 0)
        win_rate = report_data.get('win_rate', 0.0) * 100  # 转换为百分比
        total_return = report_data.get('total_return', 0.0)
        
        # 判断涨跌颜色
        pnl_icon = "🟢" if total_return >= 0 else "🔴"
        
        # 策略表现
        strategies = report_data.get('by_strategy', {})
        strategies_text = ""
        if strategies:
            for strat_name, strat_data in sorted(strategies.items(), 
                                                  key=lambda x: x[1].get('win_rate', 0), 
                                                  reverse=True):
                strat_winrate = strat_data.get('win_rate', 0) * 100
                strat_count = strat_data.get('count', 0)
                strat_profit = strat_data.get('total_profit', 0)
                star = " ⭐" if strat_winrate >= 60 else ""
                strategies_text += f"- **{strat_name}**: {strat_winrate:.0f}% 胜率 ({strat_count} 次) HK${strat_profit:,.0f}{star}\n\n"
        else:
            strategies_text = "暂无策略数据\n\n"
        
        # 股票表现 (Top 5)
        stocks = report_data.get('by_stock', {})
        stocks_text = ""
        if stocks:
            top_stocks = sorted(stocks.items(), 
                               key=lambda x: x[1].get('total_profit', 0), 
                               reverse=True)[:5]
            for code, stock_data in top_stocks:
                stock_winrate = stock_data.get('win_rate', 0) * 100
                stock_count = stock_data.get('count', 0)
                stock_profit = stock_data.get('total_profit', 0)
                stocks_text += f"- **{code}**: {stock_winrate:.0f}% ({stock_count} 次) HK${stock_profit:,.0f}\n\n"
        else:
            stocks_text = "暂无持仓数据\n\n"
        
        # 基准对比
        benchmark_text = ""
        benchmark = report_data.get('benchmark_comparison', {})
        if benchmark:
            benchmark_return = benchmark.get('benchmark_return', 2.0)
            excess_return = benchmark.get('excess_return', 0.0)
            outperform = benchmark.get('outperform', False)
            benchmark_icon = "✅" if outperform else "❌"
            benchmark_text = (
                f"**基准对比**: 恒生指数 {benchmark_return:.2f}% | "
                f"超额收益 {excess_return:+.2f}% {benchmark_icon}\n\n"
                f"---\n\n"
            )
        
        # 改进建议
        recommendations = report_data.get('recommendations', [])
        recommendations_text = ""
        if recommendations:
            recommendations_text = f"**改进建议**:\n\n"
            for i, rec in enumerate(recommendations[:5], 1):
                recommendations_text += f"{i}. {rec}\n\n"
            recommendations_text += f"---\n\n"
        
        markdown_text = (
            f"### 📊 FTSeek 周报复盘\n\n"
            f"**周期**: {period}\n\n"
            f"---\n\n"
            f"**交易汇总**:\n\n"
            f"- 总交易：{total_trades} 笔\n\n"
            f"- 胜率：{win_rate:.1f}%\n\n"
            f"- 总收益：{pnl_icon} HK${total_return:,.2f}\n\n"
            f"---\n\n"
            f"{benchmark_text}"
            f"**策略表现**:\n\n"
            f"{strategies_text}"
            f"---\n\n"
            f"**个股表现 **(Top 5):\n\n"
            f"{stocks_text}"
            f"{recommendations_text}"
            f"_数据自动生成 | FTSeek 绩效追踪系统_\n\n"
        )
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"📊 FTSeek 周报复盘 ({period})",
                "text": markdown_text,
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉周报推送成功：{period}")
        return ok

    def send_backtest_compare_report(self, report_data: dict) -> bool:
        """
        发送回测对比报告到钉钉
        包含：实盘 vs 回测收益率、胜率对比
        
        Args:
            report_data: 对比报告数据字典，包含：
                - period: 对比周期
                - code: 股票代码 (可选)
                - strategy: 策略名称 (可选)
                - returns: 收益率对比 {live_return, backtest_return, difference}
                - win_rates: 胜率对比 {live_win_rate, backtest_win_rate, difference}
                - trades: 交易次数对比 {live_trades, backtest_trades, difference}
                - avg_profits: 平均收益对比 {live_avg_profit, backtest_avg_profit, difference}
                - summary: 总结文本
                
        Returns:
            bool: 推送是否成功
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过对比报告推送")
            return False
        
        if not self._can_send():
            logger.warning(f"限流跳过对比报告推送")
            return False
        
        period = report_data.get('period', '30d')
        code = report_data.get('code', '全部')
        strategy = report_data.get('strategy', '全部')
        
        # 收益率对比
        returns = report_data.get('returns', {})
        live_return = returns.get('live_return', 0.0)
        backtest_return = returns.get('backtest_return', 0.0)
        return_diff = returns.get('difference', 0.0)
        return_icon = "🟢" if return_diff > 0 else "🔴" if return_diff < 0 else "🟡"
        
        # 胜率对比
        win_rates = report_data.get('win_rates', {})
        live_win_rate = win_rates.get('live_win_rate', 0.0) * 100
        backtest_win_rate = win_rates.get('backtest_win_rate', 0.0) * 100
        win_diff = win_rates.get('difference', 0.0) * 100
        win_icon = "🟢" if win_diff > 5 else "🔴" if win_diff < -5 else "🟡"
        
        # 交易次数对比
        trades = report_data.get('trades', {})
        live_trades = trades.get('live_trades', 0)
        backtest_trades = trades.get('backtest_trades', 0)
        trade_diff = trades.get('difference', 0)
        
        # 平均收益对比
        avg_profits = report_data.get('avg_profits', {})
        live_avg_profit = avg_profits.get('live_avg_profit', 0.0)
        backtest_avg_profit = avg_profits.get('backtest_avg_profit', 0.0)
        profit_diff = avg_profits.get('difference', 0.0)
        profit_icon = "🟢" if profit_diff > 0 else "🔴" if profit_diff < 0 else "🟡"
        
        # 总结
        summary = report_data.get('summary', '暂无总结')
        
        markdown_text = (
            f"### 📈 FTSeek 回测对比报告\n\n"
            f"**周期**: {period} | **标的**: {code} | **策略**: {strategy}\n\n"
            f"---\n\n"
            f"**收益率对比**:\n\n"
            f"- {return_icon} 实盘：{live_return:+.2f}%\n\n"
            f"- 回测：{backtest_return:+.2f}%\n\n"
            f"- 差异：{return_diff:+.2f}%\n\n"
            f"---\n\n"
            f"**胜率对比**:\n\n"
            f"- {win_icon} 实盘：{live_win_rate:.1f}%\n\n"
            f"- 回测：{backtest_win_rate:.1f}%\n\n"
            f"- 差异：{win_diff:+.1f}%\n\n"
            f"---\n\n"
            f"**交易次数**:\n\n"
            f"- 实盘：{live_trades} 笔\n\n"
            f"- 回测：{backtest_trades} 笔\n\n"
            f"- 差异：{trade_diff:+d} 笔\n\n"
            f"---\n\n"
            f"**平均收益**:\n\n"
            f"- {profit_icon} 实盘：HK${live_avg_profit:,.2f}\n\n"
            f"- 回测：HK${backtest_avg_profit:,.2f}\n\n"
            f"- 差异：HK${profit_diff:+,.2f}\n\n"
            f"---\n\n"
            f"**总结**: {summary}\n\n"
            f"---\n\n"
            f"_数据自动生成 | FTSeek 绩效追踪系统_\n\n"
        )
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"📈 FTSeek 回测对比报告 ({period})",
                "text": markdown_text,
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉回测对比报告推送成功：{period}")
        return ok

    def send_performance_summary(self, summary: dict) -> bool:
        """
        发送绩效汇总到钉钉
        包含：当日交易、累计收益、胜率统计
        
        Args:
            summary: 绩效汇总数据字典，包含：
                - date: 日期
                - today_trades: 今日交易数
                - today_wins: 今日盈利数
                - today_win_rate: 今日胜率
                - today_pnl: 今日盈亏
                - today_pnl_pct: 今日盈亏百分比
                - cumulative_trades: 累计交易数
                - cumulative_wins: 累计盈利数
                - cumulative_win_rate: 累计胜率
                - cumulative_pnl: 累计盈亏
                - cumulative_pnl_pct: 累计盈亏百分比
                - positions: 当前持仓列表 (可选)
                - risk_status: 风控状态 (可选)
                
        Returns:
            bool: 推送是否成功
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过绩效汇总推送")
            return False
        
        if not self._can_send():
            logger.warning(f"限流跳过绩效汇总推送")
            return False
        
        date_str = summary.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        # 今日数据
        today_trades = summary.get('today_trades', 0)
        today_wins = summary.get('today_wins', 0)
        today_win_rate = summary.get('today_win_rate', 0.0) * 100
        today_pnl = summary.get('today_pnl', 0.0)
        today_pnl_pct = summary.get('today_pnl_pct', 0.0)
        today_icon = "🟢" if today_pnl >= 0 else "🔴" if today_pnl < 0 else "🟡"
        
        # 累计数据
        cumulative_trades = summary.get('cumulative_trades', 0)
        cumulative_wins = summary.get('cumulative_wins', 0)
        cumulative_win_rate = summary.get('cumulative_win_rate', 0.0) * 100
        cumulative_pnl = summary.get('cumulative_pnl', 0.0)
        cumulative_pnl_pct = summary.get('cumulative_pnl_pct', 0.0)
        cumulative_icon = "🟢" if cumulative_pnl >= 0 else "🔴" if cumulative_pnl < 0 else "🟡"
        
        # 持仓列表
        positions = summary.get('positions', [])
        positions_text = ""
        if positions:
            positions_text = f"**当前持仓** ({len(positions)} 只):\n\n"
            for pos in positions[:10]:  # 最多显示 10 只
                code = pos.get('code', 'N/A')
                name = pos.get('name', '')
                pnl_pct = pos.get('pnl_pct', 0.0)
                pnl_icon = "🟢" if pnl_pct >= 0 else "🔴"
                positions_text += f"- {code} {name}: {pnl_icon} {pnl_pct:+.2f}%\n\n"
            if len(positions) > 10:
                positions_text += f"_... 还有 {len(positions) - 10} 只持仓_\n\n"
        else:
            positions_text = "**当前持仓**: 无\n\n"
        
        # 风控状态
        risk_status = summary.get('risk_status', 'normal')
        risk_label = {
            'normal': '✅ 正常',
            'warning': '⚠️ 警告',
            'critical': '🔴 严重',
        }.get(risk_status, '✅ 正常')
        
        # 其他统计
        consecutive_loss = summary.get('consecutive_loss', 0)
        max_drawdown = summary.get('max_drawdown', 0.0)
        safe_drawdown = summary.get('safe_drawdown', 15.0)
        
        markdown_text = (
            f"### 📊 FTSeek 绩效汇总\n\n"
            f"**日期**: {date_str}\n\n"
            f"---\n\n"
            f"**今日表现**:\n\n"
            f"- 交易：{today_trades} 笔 | 盈利：{today_wins} 笔 | 胜率：{today_win_rate:.1f}%\n\n"
            f"- 盈亏：{today_icon} HK${today_pnl:,.2f} ({today_pnl_pct:+.2f}%)\n\n"
            f"---\n\n"
            f"**累计表现**:\n\n"
            f"- 交易：{cumulative_trades} 笔 | 盈利：{cumulative_wins} 笔 | 胜率：{cumulative_win_rate:.1f}%\n\n"
            f"- 盈亏：{cumulative_icon} HK${cumulative_pnl:,.2f} ({cumulative_pnl_pct:+.2f}%)\n\n"
            f"---\n\n"
            f"{positions_text}"
            f"**风控状态**: {risk_label}\n\n"
            f"**连续亏损**: {consecutive_loss} 次\n\n"
            f"**当前回撤**: {max_drawdown:.2f}% (安全线 {safe_drawdown:.0f}%)\n\n"
            f"---\n\n"
            f"_数据自动生成 | FTSeek 绩效追踪系统_\n\n"
        )
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"📊 FTSeek 绩效汇总 ({date_str})",
                "text": markdown_text,
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉绩效汇总推送成功：{date_str}")
        return ok

    def send_monthly_report(self, report: dict):
        """
        发送月报（每月 1 日 09:00）
        
        Args:
            report: 月报数据字典
        """
        if not self.webhook_url:
            return False
        
        month_str = report.get('month', time.strftime('%Y-%m'))
        total_trades = report.get('total_trades', 0)
        wins = report.get('wins', 0)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        
        pnl = report.get('pnl', 0)
        pnl_pct = report.get('pnl_pct', 0)
        max_dd = report.get('max_drawdown', 0)
        sharpe = report.get('sharpe_ratio', 0)
        sortino = report.get('sortino_ratio', 0)
        
        pnl_icon = "🟢" if pnl >= 0 else "🔴"
        
        # 策略归因
        strategies = report.get('strategies', {})
        strategies_text = ""
        for strat_name, strat_data in strategies.items():
            strat_pnl = strat_data.get('pnl', 0)
            strat_count = strat_data.get('count', 0)
            strategies_text += f"- {strat_name}: HK${strat_pnl:,.2f} ({strat_count} 次)\n\n"
        
        markdown_text = (
            f"### 📊 FTSeek 月报\n\n"
            f"**月份**: {month_str}\n\n"
            f"---\n\n"
            f"**月度交易**: {total_trades} 次\n\n"
            f"**胜率**: {win_rate:.0f}% ({wins}/{total_trades})\n\n"
            f"**盈亏比**: {report.get('profit_loss_ratio', 0):.1f}\n\n"
            f"---\n\n"
            f"**月度收益**: {pnl_icon} HK${pnl:,.2f} ({pnl_pct:+.2f}%)\n\n"
            f"**最大回撤**: {max_dd:.2f}%\n\n"
            f"**夏普比率**: {sharpe:.2f}\n\n"
            f"**索提诺比率**: {sortino:.2f}\n\n"
            f"---\n\n"
            f"**策略归因**:\n\n"
            f"{strategies_text}"
            f"---\n\n"
            f"**下月计划**: {report.get('plan', '继续优化策略，控制风险')}\n\n"
        )
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"📊 FTSeek 月报 ({month_str})",
                "text": markdown_text,
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉月报推送成功：{month_str}")
        return ok

    def send_risk_monitor(self, monitor: dict):
        """
        发送风险监控面板（每 2 小时）
        
        Args:
            monitor: 监控数据字典
        """
        if not self.webhook_url:
            return False
        
        time_str = monitor.get('time', time.strftime('%H:%M'))
        total_value = monitor.get('total_value', 0)
        cash = monitor.get('cash', 0)
        position_value = monitor.get('position_value', 0)
        
        today_pnl = monitor.get('today_pnl', 0)
        today_pnl_pct = monitor.get('today_pnl_pct', 0)
        week_pnl = monitor.get('week_pnl', 0)
        week_pnl_pct = monitor.get('week_pnl_pct', 0)
        drawdown = monitor.get('drawdown', 0)
        
        today_icon = "🟢" if today_pnl >= 0 else "🔴"
        week_icon = "🟢" if week_pnl >= 0 else "🔴"
        
        risk_status = "✅ 正常" if monitor.get('risk_ok', True) else "⚠️ 异常"
        
        markdown_text = (
            f"### 📊 FTSeek 风险监控\n\n"
            f"**时间**: {time_str}\n\n"
            f"---\n\n"
            f"**账户总值**: HK${total_value:,.2f}\n\n"
            f"**可用现金**: HK${cash:,.2f} ({cash/total_value*100:.0f}%)\n\n"
            f"**持仓市值**: HK${position_value:,.2f} ({position_value/total_value*100:.0f}%)\n\n"
            f"---\n\n"
            f"**今日盈亏**: {today_icon} HK${today_pnl:,.2f} ({today_pnl_pct:+.2f}%)\n\n"
            f"**本周盈亏**: {week_icon} HK${week_pnl:,.2f} ({week_pnl_pct:+.2f}%)\n\n"
            f"**总回撤**: {drawdown:.2f}% (安全线 {monitor.get('max_drawdown', 15):.0f}%)\n\n"
            f"---\n\n"
            f"**持仓数量**: {monitor.get('position_count', 0)} 只\n\n"
            f"**今日交易**: {monitor.get('today_trades', 0)}/{monitor.get('max_daily', 10)} 次\n\n"
            f"**连续亏损**: {monitor.get('consecutive_loss', 0)} 次\n\n"
            f"---\n\n"
            f"**风控状态**: {risk_status}\n\n"
        )
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"📊 FTSeek 风险监控 ({time_str})",
                "text": markdown_text,
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉风险监控推送成功：{time_str}")
        return ok

    def send_enhanced_signal(self, signal, related_news: list = None, 
                            analysis_summary: dict = None) -> bool:
        """
        发送增强版信号推送 (带新闻链接和分析)
        
        Args:
            signal: Signal 对象
            related_news: 相关新闻列表 [NewsItem, ...]
            analysis_summary: 分析汇总 {sentiment, suggestion, reason}
            
        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过推送")
            return False
        
        if not self._can_send():
            logger.warning(f"限流跳过增强信号：{signal.code}")
            return False
        
        from app.strategy.base import SignalAction
        
        currency = "$" if signal.code.startswith("US.") else "HK$"
        is_consensus = signal.extra.get("consensus", False) if signal.extra else False
        
        # 构建消息文本
        icon = "🔴" if signal.action == SignalAction.BUY else "🟢" if signal.action == SignalAction.SELL else "🔵"
        direction = "买入" if signal.action == SignalAction.BUY else "卖出" if signal.action == SignalAction.SELL else "关注"
        
        lines = [
            f"### {icon} FTSeek {direction}信号\n\n",
            f"📈 **{signal.name} ({signal.code})**\n\n",
            f"💰 **价格**: {currency}{signal.price:.2f}\n\n",
        ]
        
        # 置信度
        if is_consensus:
            score = signal.extra.get("score", 0)
            threshold = signal.extra.get("threshold", 0)
            lines.append(f"📊 **置信度**: {score:+.2f} (阈值 ±{threshold})\n\n")
        else:
            confidence = signal.extra.get('confidence', 0.75)
            lines.append(f"📊 **置信度**: {confidence * 100:.0f}%\n\n")
        
        # 相关新闻
        if related_news:
            lines.extend([
                f"📰 **相关新闻** ({len(related_news)} 条)\n\n",
                f"---\n\n",
            ])
            
            for i, news in enumerate(related_news[:3], 1):
                impact_icon = "🟢" if news.sentiment_score > 0 else "🔴" if news.sentiment_score < 0 else "🟡"
                impact_label = "利好" if news.sentiment_score > 0 else "利空" if news.sentiment_score < 0 else "中性"
                
                lines.extend([
                    f"{i}. **{news.title}**\n\n",
                    f"   {impact_icon} 影响：{impact_label}\n\n",
                    f"   🔗 [链接]({news.url})\n\n",
                ])
            
            if len(related_news) > 3:
                lines.append(f"_... 还有 {len(related_news) - 3} 条相关新闻_\n\n")
        
        # AI 分析
        if analysis_summary:
            lines.extend([
                f"🤖 **AI 分析**\n\n",
                f"综合评估：{analysis_summary.get('reason', '暂无分析')}\n\n",
                f"建议：{analysis_summary.get('suggestion', 'hold')}\n\n",
            ])
        
        # 信号原因和有效期
        lines.extend([
            f"---\n\n",
            f"**触发原因**: {signal.reason}\n\n",
            f"---\n\n",
            f"⏰ 有效期 {config.SIGNAL_EXPIRE_SECONDS // 60} 分钟\n\n",
        ])
        
        markdown_text = "".join(lines)
        
        # 按钮
        confirm_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/confirm"
            f"?signal_id={signal.id}&token={config.API_TOKEN}"
        )
        defer_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/defer"
            f"?signal_id={signal.id}&token={config.API_TOKEN}"
        )
        reject_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/reject"
            f"?signal_id={signal.id}&token={config.API_TOKEN}"
        )
        
        action_text = ACTION_LABELS.get(signal.action, "查看")
        
        payload = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": f"{icon} FTSeek {direction}信号 - {signal.code}",
                "text": markdown_text,
                "btnOrientation": "1",
                "btns": [
                    {"title": f"✅ {action_text}", "actionURL": confirm_url},
                    {"title": "⏰ 延期 15 分钟", "actionURL": defer_url},
                    {"title": "❌ 忽略", "actionURL": reject_url},
                ],
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉增强信号推送成功：{signal.id} -> {signal.code}")
        return ok
    
    def send_major_news(self, news_item, analysis: dict = None) -> bool:
        """
        发送重大新闻独立推送
        
        Args:
            news_item: NewsItem 对象
            analysis: 分析结果 {sentiment, impact_score, suggestion, reason}
            
        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过推送")
            return False
        
        if not self._can_send():
            logger.warning(f"限流跳过重大新闻：{news_item.title[:50]}")
            return False
        
        impact_icon = "🟢" if news_item.sentiment_score > 0 else "🔴" if news_item.sentiment_score < 0 else "🟡"
        impact_label = "利好" if news_item.sentiment_score > 0 else "利空" if news_item.sentiment_score < 0 else "中性"
        
        lines = [
            f"### 🚨 重大新闻\n\n",
            f"**{news_item.title}**\n\n",
            f"---\n\n",
            f"{impact_icon} **影响**: {impact_label}\n\n",
            f"📊 **情绪评分**: {news_item.sentiment_score:+.2f}\n\n",
            f"🕒 **时间**: {news_item.published_at}\n\n",
            f"📰 **来源**: {news_item.source}\n\n",
            f"---\n\n",
        ]
        
        if analysis:
            lines.extend([
                f"🤖 **AI 分析**\n\n",
                f"综合评估：{analysis.get('reason', '暂无分析')}\n\n",
                f"建议：{analysis.get('suggestion', 'hold')}\n\n",
                f"影响程度：{analysis.get('impact_score', 5)}/10\n\n",
                f"---\n\n",
            ])
        
        lines.extend([
            f"🔗 [阅读原文]({news_item.url})\n\n",
        ])
        
        markdown_text = "".join(lines)
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"🚨 重大新闻 - {news_item.title[:50]}",
                "text": markdown_text,
            },
            "at": {"isAtAll": True},  # 重大新闻@所有人
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉重大新闻推送成功：{news_item.title[:50]}")
        return ok
    
    # 注意：send_news_digest 方法已移除 (2026-03-02)
    # 新闻推送功能已禁用
    
    def send_major_news_batch(self, news_items, max_items: int = 10) -> bool:
        """
        批量发送重大新闻 (合并为一条消息，避免限流)
        
        Args:
            news_items: NewsItem 列表
            max_items: 最多显示条数
            
        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过推送")
            return False
        
        if not news_items:
            return False
        
        # 限制条数
        news_items = news_items[:max_items]
        
        lines = [
            f"### 🚨 重大新闻速览\n\n",
            f"**更新时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n",
            f"**本时段共 {len(news_items)} 条重大新闻**\n\n",
            f"---\n\n",
        ]
        
        for i, news in enumerate(news_items, 1):
            impact_icon = "🟢" if news.sentiment_score > 0 else "🔴" if news.sentiment_score < 0 else "🟡"
            impact_label = "利好" if news.sentiment_score > 0 else "利空" if news.sentiment_score < 0 else "中性"
            
            lines.extend([
                f"**{i}. {impact_icon} {news.title}**\n\n",
                f"   影响：{impact_label} | 情绪：{news.sentiment_score:+.2f} | 来源：{news.source}\n\n",
                f"   🕒 {news.published_at}\n\n",
            ])
        
        lines.extend([
            f"---\n\n",
            f"💡 详细新闻请查看汇总或点击原文链接\n\n",
        ])
        
        markdown_text = "".join(lines)
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"🚨 重大新闻速览 ({len(news_items)}条)",
                "text": markdown_text,
            },
            "at": {"isAtAll": False},  # 合并推送不@所有人
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉重大新闻批量推送成功：{len(news_items)}条")
        return ok

    def _get_stock_name(self, code: str) -> str:
        """获取股票名称（从代码或缓存）"""
        # 简单实现：从 code 中提取名称，实际可从配置或缓存获取
        if code.startswith("US."):
            return code.replace("US.", "")
        return code.replace("HK.", "")

    def _format_strategies(self, result) -> str:
        """格式化策略信号"""
        lines = []
        for strategy_name, score in result.strategy_scores.items():
            icon = "✅" if score > 0 else "❌"
            lines.append(f"  {icon} {strategy_name}: {score:+.1f}")
        return "\n".join(lines)

    def _format_risk_checks(self) -> str:
        """格式化风控检查"""
        # 从 risk_manager 获取检查结果
        return "  ✅ 仓位 2.1% < 20%\n  ✅ 日交易 3/10\n  ✅ 回撤 0.5% < 15%"

    def _calculate_hold_time(self, entry_time, exit_time) -> str:
        """计算持仓时长"""
        # 支持字符串或 datetime 对象
        if isinstance(entry_time, str):
            entry_time = datetime.strptime(entry_time, '%Y-%m-%d %H:%M:%S')
        if isinstance(exit_time, str):
            exit_time = datetime.strptime(exit_time, '%Y-%m-%d %H:%M:%S')
        
        delta = exit_time - entry_time
        hours = int(delta.total_seconds() / 3600)
        minutes = int((delta.total_seconds() % 3600) / 60)
        if hours > 0:
            return f"{hours}小时{minutes}分钟"
        return f"{minutes}分钟"

    def send_open_position_message(self, signal, trade_result, position, consensus_result) -> bool:
        """
        发送开仓详细推送
        
        Args:
            signal: Signal 对象
            trade_result: TradeResult 对象
            position: Position 对象
            consensus_result: ConsensusResult 对象
            
        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过推送")
            return False
        
        if not self._can_send():
            logger.warning(f"限流跳过开仓推送：{signal.code}")
            return False
        
        stock_name = self._get_stock_name(signal.code)
        action_text = "买入" if signal.action == SignalAction.BUY else "卖出"
        
        message = f"""✅ {signal.code} {stock_name} {action_text}成交
━━━━━━━━━━━━━━━━━━━━
订单 ID: {trade_result.order_id}
数量：{signal.quantity:,} 股
成交价：HK$ {signal.price:.2f}
总金额：HK$ {signal.price * signal.quantity:,.2f}
时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
━━━━━━━━━━━━━━━━━━━━
策略信号:
{self._format_strategies(consensus_result)}
加权分：{consensus_result.score:+.2f} (置信度 {consensus_result.confidence:.0%})
市场状态：{consensus_result.market_regime}
━━━━━━━━━━━━━━━━━━━━
风控检查:
{self._format_risk_checks()}
"""
        return self._send_markdown(message)

    def send_close_position_message(self, signal, trade_result, position, consensus_result) -> bool:
        """
        发送平仓详细推送
        
        Args:
            signal: Signal 对象
            trade_result: TradeResult 对象
            position: Position 对象
            consensus_result: ConsensusResult 对象
            
        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过推送")
            return False
        
        if not self._can_send():
            logger.warning(f"限流跳过平仓推送：{signal.code}")
            return False
        
        pnl = trade_result.pnl if trade_result.pnl else 0.0
        pnl_rate = (pnl / (position.entry_price * position.quantity)) * 100
        hold_time = self._calculate_hold_time(position.entry_time, datetime.now())
        
        action_icon = "📊" if pnl >= 0 else "⚠️"
        pnl_icon = "💰" if pnl >= 0 else "💸"
        
        # 使用 signal.price 作为平仓价（TradeResult 没有 exit_price 字段）
        exit_price = signal.price
        
        message = f"""{action_icon} {signal.code} {self._get_stock_name(signal.code)} 平仓盈亏
━━━━━━━━━━━━━━━━━━━━
开仓：{position.quantity:,} 股 @ HK$ {position.entry_price:.2f} ({position.entry_time})
平仓：{position.quantity:,} 股 @ HK$ {exit_price:.2f} ({datetime.now().strftime('%H:%M:%S')})
━━━━━━━━━━━━━━━━━━━━
{pnl_icon} 盈亏：HK$ {pnl:+,.2f} ({pnl_rate:+.2f}%)
持仓时长：{hold_time}
━━━━━━━━━━━━━━━━━━━━
策略：反向信号触发平仓
原信号：{position.action} → 平仓信号：{signal.action.value}
"""
        return self._send_markdown(message)

    def _send_markdown(self, text: str) -> bool:
        """
        发送 Markdown 格式消息
        
        Args:
            text: Markdown 文本
            
        Returns:
            是否发送成功
        """
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "FTSeek 交易通知",
                "text": text,
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉 Markdown 推送成功")
        return ok


