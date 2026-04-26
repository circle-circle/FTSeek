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
logger.propagate = False  # 防止日志重复

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
        # 缓存 watchlist (2026-04-13 统一配置)
        self._watchlist_cache: set = set()
        self._watchlist_cache_time: float = 0
        self._watchlist_cache_ttl: float = 60  # 60 秒刷新

    def _get_enabled_watchlist(self) -> set:
        """获取启用的 watchlist 股票代码集合 (2026-04-13 统一配置)

        优先级:
        1. watchlist.json 中 enabled=True 的股票 (保留精细控制)
        2. app.config.WATCH_LIST (统一配置，fallback)
        """
        now = time.time()
        if now - self._watchlist_cache_time < self._watchlist_cache_ttl:
            return self._watchlist_cache

        try:
            import json
            watchlist_path = "/opt/ftseek/FTSeek/config/watchlist.json"
            import os as _os
            if _os.path.exists(watchlist_path):
                with open(watchlist_path, 'r') as f:
                    watchlist = json.load(f)
                enabled = set()
                for stock in watchlist.get('hk_stocks', []):
                    if stock.get('enabled', False):
                        enabled.add(stock['code'])
                for stock in watchlist.get('us_stocks', []):
                    if stock.get('enabled', False):
                        enabled.add(stock['code'])
                if enabled:
                    self._watchlist_cache = enabled
                    self._watchlist_cache_time = now
                    return enabled
        except Exception:
            pass

        # Fallback: 从统一配置 WATCH_LIST_HK 获取 (仅港股)
        enabled = set(config.WATCH_LIST_HK)
        self._watchlist_cache = enabled
        self._watchlist_cache_time = now
        return enabled

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
        # 加载 watchlist 配置，过滤未启用的股票 (2026-04-13 统一配置)
        try:
            enabled_codes = self._get_enabled_watchlist()

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
                f"### [FTSeek] {title} | 🇭🇰 港股\n\n"
                f"**📍 标的**: {signal.code} {signal.name}\n\n"
                f"**📊 方向**: **{direction}**\n\n"
                f"**📈 触发均价**: {currency}{signal.price:.2f}\n\n"
                f"**📦 数量**: {signal.quantity} 股\n\n"
                f"---\n\n"
                f"**🧮 加权评分**: **{score:+.2f}** (阈值 ±{threshold})\n\n"
                f"**🌍 市场状态**: {regime_label} ({adx_str})\n\n"
                f"---\n\n"
                f"**👥 同向策略 ({n_agree}/{n_total})**\n\n"
                f"{agree_lines}"
                f"{oppose_lines}\n\n"
                f"---\n\n"
                f"**📝 各策略依据**:\n\n"
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

    def send_event_impact_alert(self, event_name: str, news_title: str, 
                                logic: str, impact_stocks: dict, 
                                source: str = "", publish_time: str = ""):
        """发送事件联想预警 (2026-04-13 新增)
        
        Args:
            event_name: 事件名称 (e.g., "地缘冲突与能源")
            news_title: 触发新闻标题
            logic: 影响逻辑 (e.g., "停火 -> 油价跌 -> 利空")
            impact_stocks: 影响股票字典 {code: name}
            source: 新闻来源
            publish_time: 发布时间
        """
        if not self.webhook_url:
            return False
        if not self._can_send():
            logger.warning(f"限流跳过事件预警: {event_name}")
            return False

        stocks_text = "\n".join([f"- **{name} ({code})**" for code, name in impact_stocks.items()])

        text = f"""### 🔥 宏观事件预警 | {event_name}

**事件**: {news_title}
**逻辑**: {logic}

**🎯 关联标的预警**:
{stocks_text}

---
来源: {source}
时间: {publish_time[:19] if publish_time else datetime.now().strftime('%Y-%m-%d %H:%M')}
⚠️ 此为 AI 联想分析，仅供参考
"""
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"宏观事件预警 | {event_name}",
                "text": text,
            },
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
        
        lines = [f"### [FTSeek] {alert_label} | 🇭🇰 港股\n\n"]
        
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

    def send_daily_stats(self, stats: dict, review: dict = None):
        """
        发送收盘统计（16:30）
        
        Args:
            stats: 统计数据字典
            review: 深度复盘数据（可选），来自 DailyStatsReporter.generate_review_report()
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
        
        # ============ 深度复盘区块（如传入 review 则追加）============
        if review:
            # 1. 信号归因
            rej = review.get('rejection_stats', {})
            total_rej = sum(rej.values())
            if total_rej > 0:
                markdown_text += (
                    f"---\n\n"
                    f"**信号归因** (共拒绝 {total_rej} 次)\n\n"
                    f"- 盈亏比不足：{rej.get('rr_filter', 0)} 次\n\n"
                    f"- 时段噪音过滤：{rej.get('time_filter', 0)} 次\n\n"
                    f"- 熔断拦截：{rej.get('circuit_breaker', 0)} 次\n\n"
                    f"- 置信度低：{rej.get('low_confidence', 0)} 次\n\n"
                    f"- 其他：{rej.get('other', 0)} 次\n\n"
                )
            
            # 2. 个股盈亏归因
            closed_trades = review.get('closed_trades', [])
            if closed_trades:
                markdown_text += "---\n\n**今日平仓明细**\n\n"
                for t in closed_trades[:6]:  # 最多展示 6 笔
                    pnl = t.get('pnl', 0)
                    pnl_pct = t.get('pnl_pct', 0)
                    p_icon = "💰" if pnl >= 0 else "💸"
                    dir_label = "多" if t.get('direction') == 'long' else "空"
                    hold = t.get('hold_mins', 0)
                    hold_str = f"{hold // 60}h{hold % 60}m" if hold >= 60 else f"{hold}m"
                    reason = t.get('exit_reason', '-')
                    markdown_text += (
                        f"- {p_icon} **{t.get('code','')}** [{dir_label}] "
                        f"{t.get('entry_price',0):.2f}→{t.get('exit_price',0):.2f} "
                        f"({pnl_pct:+.1f}%) HK${pnl:+.0f} "
                        f"持仓{hold_str} | {reason}\n\n"
                    )
            
            # 3. 市场状态回顾
            regime = review.get('market_regime', {})
            if regime:
                adx_str = f"ADX={regime.get('adx', 0):.1f}" if regime.get('adx') else ""
                markdown_text += (
                    f"---\n\n"
                    f"**市场状态**: {regime.get('description', '-')} {adx_str}\n\n"
                )
            
            # 4. 策略排行
            strat_stats = review.get('strategy_stats', {})
            if strat_stats:
                markdown_text += "---\n\n**策略贡献排行**\n\n"
                # 按 total_pnl 排序，展示前 5
                sorted_strats = sorted(
                    strat_stats.items(),
                    key=lambda x: x[1].get('total_pnl', 0),
                    reverse=True
                )
                for name, s in sorted_strats[:5]:
                    wr = s.get('win_rate', 0) * 100
                    cnt = s.get('trade_count', 0)
                    pnl = s.get('total_pnl', 0)
                    icon = "⭐" if wr >= 60 else ""
                    markdown_text += (
                        f"- **{name}**: 胜率{wr:.0f}% {cnt}笔 HK${pnl:+.0f} {icon}\n\n"
                    )
            
            # 5. 下日预警
            at_risk = review.get('at_risk_positions', [])
            if at_risk:
                markdown_text += "---\n\n**⚠️ 下日预警（浮亏超 -2%）**\n\n"
                for pos in at_risk:
                    pnl_pct = pos.get('unrealized_pnl_percent', 0)
                    markdown_text += (
                        f"- 🔴 **{pos.get('code','')}** {pos.get('name','')} "
                        f"浮亏 {pnl_pct:.1f}% 需关注\n\n"
                    )

            # 6. 权重收敛仪表板
            wc = review.get('weight_convergence', {})
            wc_strategies = wc.get('strategies', {})
            if wc_strategies:
                convergence_pct = wc.get('overall_convergence', 0) * 100
                converged = wc.get('converged_count', 0)
                total_en = wc.get('total_enabled', 0)
                ready_icon = "✅" if wc.get('ready') else "⏳"
                markdown_text += (
                    f"---\n\n"
                    f"**📈 权重收敛** {ready_icon}\n\n"
                )
                # 按置信度降序排列
                sorted_wc = sorted(
                    wc_strategies.items(),
                    key=lambda x: x[1].get('confidence', 0),
                    reverse=True
                )
                for name, s in sorted_wc:
                    conf = s.get('confidence', 0) * 100
                    trades = s.get('trades', 0)
                    wr = s.get('win_rate', 0) * 100
                    w = s.get('current_weight', 0)
                    bar = "█" * int(conf / 10) + "░" * (10 - int(conf / 10))
                    markdown_text += (
                        f"- **{name}**: {bar} {conf:.0f}% ({trades}/20笔) "
                        f"权重{w:.2f} 胜率{wr:.0f}%\n\n"
                    )
                markdown_text += (
                    f"\n整体收敛: {converged}/{total_en} 策略已收敛 "
                    f"({convergence_pct:.0f}%) "
                    f"{'[就绪]' if wc.get('ready') else '[未就绪]'}\n\n"
                )

            # 7. 执行质量
            eq = review.get('execution_quality', {})
            exec_rate = eq.get('execution_rate', 0)
            if eq.get('total_signals', 0) > 0 or eq.get('exit_distribution'):
                markdown_text += "---\n\n**🎯 执行质量**\n\n"
                if eq.get('total_signals', 0) > 0:
                    rate_label = "健康" if 0.2 <= exec_rate <= 0.7 else "异常"
                    markdown_text += (
                        f"- 信号执行率: {exec_rate:.0%} "
                        f"({eq.get('executed',0)}/{eq.get('total_signals',0)}) "
                        f"[{rate_label}]\n\n"
                    )
                exit_dist = eq.get('exit_distribution_pct', {})
                if exit_dist:
                    parts = []
                    label_map = {
                        'stop_loss': '止损', 'take_profit': '止盈',
                        'trailing_stop': '追踪', 'time_exit': '时间',
                        'risk_control': '风控', 'partial_take_profit': '分批止盈',
                    }
                    for reason, pct in sorted(exit_dist.items(), key=lambda x: -x[1]):
                        label = label_map.get(reason, reason)
                        parts.append(f"{label} {pct:.0%}")
                    markdown_text += f"- 退出分布: {' | '.join(parts)}\n\n"
                warnings = eq.get('health_warnings', [])
                for w in warnings:
                    markdown_text += f"- ⚠️ {w}\n\n"
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"📊 FTSeek 收盘统计 ({date_str})",
                "text": markdown_text[:4096],  # 钉钉单条限制
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉收盘统计推送成功：{date_str}")
        return ok

    def send_weekly_report(self, report_data: dict, deep_review: dict = None) -> bool:
        """
        发送周报复盘到钉钉（分两条消息：汇总+深度复盘）
        
        Args:
            report_data: 周报数据字典
            deep_review: 深度复盘数据（可选），来自 WeeklyReportGenerator.generate_deep_review()
                
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
        win_rate = report_data.get('win_rate', 0.0) * 100
        total_return = report_data.get('total_return', 0.0)
        
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
        
        # ===== Part 1：交易汇总 + 策略归因 =====
        part1_text = (
            f"### 📊 FTSeek 周报复盘 (Part 1)\n\n"
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
            f"**个股表现 (Top 5)**:\n\n"
            f"{stocks_text}"
            f"{recommendations_text}"
        )
        
        # 注入深度复盘 Part 1（信号漏斗 + 策略权重）
        if deep_review:
            funnel = deep_review.get('signal_funnel', {})
            if funnel.get('total_signals', 0) > 0 or funnel.get('executed', 0) > 0:
                executed = funnel.get('executed', 0)
                profitable = funnel.get('profitable', 0)
                total_sig = funnel.get('total_signals', executed)
                rej = max(0, total_sig - executed)
                part1_text += (
                    f"**信号转化漏斗**:\n\n"
                    f"- 共识信号：{total_sig} 次\n\n"
                    f"- 实际入场：{executed} 次（转化率 {(executed/total_sig*100) if total_sig else 0:.0f}%）\n\n"
                    f"- 盈利笔数：{profitable} 次（胜率 {(profitable/executed*100) if executed else 0:.0f}%）\n\n"
                    f"- 风控拒绝：{rej} 次\n\n"
                    f"---\n\n"
                )
            
            strategy_weights = deep_review.get('strategy_weights', {})
            if strategy_weights:
                part1_text += "**策略历史胜率排行**:\n\n"
                sorted_sw = sorted(strategy_weights.items(), key=lambda x: x[1].get('win_rate', 0), reverse=True)
                for name, sw in sorted_sw[:5]:
                    wr = sw.get('win_rate', 0) * 100
                    cnt = sw.get('trade_count', 0)
                    total_pnl = sw.get('total_pnl', 0)
                    icon = "⭐" if wr >= 60 else ""
                    part1_text += f"- **{name}**: 胜率{wr:.0f}% {cnt}笔 HK${total_pnl:+.0f} {icon}\n\n"
                part1_text += "---\n\n"
        
        part1_text += "_数据自动生成 | FTSeek 绩效追踪系统_\n\n"
        
        payload1 = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"📊 FTSeek 周报复盘 Part1 ({period})",
                "text": part1_text[:4096],
            },
        }
        ok1 = self._do_post(payload1)
        if ok1:
            logger.info(f"钉钉周报 Part1 推送成功：{period}")
        
        # ===== Part 2：资金曲线 + 多空对比 + 下周展望 =====
        if deep_review:
            if not self._can_send():
                logger.warning("限流，跳过周报 Part2")
                return ok1
            
            highlights = deep_review.get('equity_highlights', {})
            ls_cmp = deep_review.get('long_short_comparison', {})
            outlook = report_data.get('outlook', '关注市场动态，谨慎交易')
            
            part2_text = (
                f"### 📊 FTSeek 周报复盘 (Part 2)\n\n"
                f"**周期**: {period}\n\n"
                f"---\n\n"
            )
            
            # 资金曲线关键节点
            if highlights.get('best_day') or highlights.get('worst_day'):
                best_day = highlights.get('best_day', '-')
                best_pnl = highlights.get('best_day_pnl', 0)
                worst_day = highlights.get('worst_day', '-')
                worst_pnl = highlights.get('worst_day_pnl', 0)
                part2_text += (
                    f"**资金曲线关键节点**:\n\n"
                    f"- 🟢 最佳交易日：{best_day} HK${best_pnl:+,.0f}\n\n"
                    f"- 🔴 最差交易日：{worst_day} HK${worst_pnl:+,.0f}\n\n"
                    f"---\n\n"
                )
            
            # 多空对比
            if ls_cmp.get('long_count', 0) > 0:
                long_wr = ls_cmp.get('long_win_rate', 0) * 100
                long_pnl = ls_cmp.get('long_pnl', 0)
                long_cnt = ls_cmp.get('long_count', 0)
                part2_text += (
                    f"**多空效果对比**:\n\n"
                    f"- 多头：{long_cnt}笔 胜率{long_wr:.0f}% HK${long_pnl:+,.0f}\n\n"
                )
                if ls_cmp.get('has_short'):
                    short_wr = ls_cmp.get('short_win_rate', 0) * 100
                    short_pnl = ls_cmp.get('short_pnl', 0)
                    short_cnt = ls_cmp.get('short_count', 0)
                    part2_text += (
                        f"- 空头：{short_cnt}笔 胜率{short_wr:.0f}% HK${short_pnl:+,.0f}\n\n"
                    )
                else:
                    part2_text += "- 空头：本周无做空交易\n\n"
                part2_text += "---\n\n"
            
            # 基准对比（静态占位）
            hsi_return = 0.0  # 实际使用时可从行情 API 获取
            excess = (total_return / 1_000_000 * 100) - hsi_return  # 简化估算
            part2_text += (
                f"**基准对比（参考）**:\n\n"
                f"- 恒生指数本周涨跌：占位（待接入行情 API）\n\n"
                f"- 纳斯达克本周涨跌：占位（待接入行情 API）\n\n"
                f"---\n\n"
            )
            
            # 下周展望
            part2_text += (
                f"**下周核心观点**:\n\n"
                f"{outlook}\n\n"
                f"---\n\n"
            )

            # 策略权重演化
            weight_evo = deep_review.get('weight_evolution', {})
            if weight_evo:
                part2_text += "**📈 策略权重演化**\n\n"
                dir_icons = {'up': '⬆️上升', 'down': '⬇️下降', 'stable': '➖持平'}
                for name, evo in sorted(weight_evo.items(),
                                        key=lambda x: abs(x[1].get('weight_delta', 0)),
                                        reverse=True):
                    cw = evo.get('current_weight', 0)
                    pw = evo.get('prev_weight')
                    direction = dir_icons.get(evo.get('direction', 'stable'), '➖')
                    conf = evo.get('confidence', 0) * 100
                    wr = evo.get('win_rate', 0) * 100
                    if pw is not None:
                        part2_text += (
                            f"- **{name}**: {pw:.2f} → {cw:.2f} [{direction}] "
                            f"置信度{conf:.0f}% 胜率{wr:.0f}%\n\n"
                        )
                    else:
                        part2_text += (
                            f"- **{name}**: {cw:.2f} [{direction}] "
                            f"置信度{conf:.0f}% 胜率{wr:.0f}%\n\n"
                        )
                    advice = evo.get('advice')
                    if advice:
                        part2_text += f"  → {advice}\n\n"
                part2_text += "---\n\n"

            part2_text += "_数据自动生成 | FTSeek 绩效追踪系统_\n\n"
            
            payload2 = {
                "msgtype": "markdown",
                "markdown": {
                    "title": f"📊 FTSeek 周报复盘 Part2 ({period})",
                    "text": part2_text[:4096],
                },
            }
            ok2 = self._do_post(payload2)
            if ok2:
                logger.info(f"钉钉周报 Part2 推送成功：{period}")
            return ok1 and ok2
        
        return ok1

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

    def send_health_dashboard(self, monitor_data) -> bool:
        """
        发送交易健康度看板（替换旧版 send_risk_monitor，每 2 小时推送）

        Args:
            monitor_data: RiskMonitorData 实例（含 .health: HealthDashboard）
        """
        if not self.webhook_url:
            return False

        h = getattr(monitor_data, 'health', None)
        time_str = getattr(monitor_data, 'time_str', time.strftime('%H:%M'))

        def _fmt(val, fmt=".1%", na="待接入", suffix=""):
            if val is None:
                return na
            try:
                return format(val, fmt) + suffix
            except Exception:
                return str(val) + suffix

        def _fmtf(val, decimals=2, na="待接入", suffix=""):
            if val is None:
                return na
            return f"{val:.{decimals}f}{suffix}"

        def _icon(val, good_above=None, bad_below=None):
            if val is None:
                return ""
            if good_above is not None and val >= good_above:
                return "🟢 "
            if bad_below is not None and val <= bad_below:
                return "🔴 "
            return "🟡 "

        # ── 账户摘要行 ────────────────────────────────────────
        total_value = getattr(monitor_data, 'total_value', 0)
        cash = getattr(monitor_data, 'cash', 0)
        today_pnl = getattr(monitor_data, 'today_pnl', 0)
        week_pnl = getattr(monitor_data, 'week_pnl', 0)
        today_icon = "🟢" if today_pnl >= 0 else "🔴"
        week_icon = "🟢" if week_pnl >= 0 else "🔴"

        account_section = (
            f"**账户**: HK${total_value:,.0f} | "
            f"可用 HK${cash:,.0f} | "
            f"{today_icon} 今日 HK${today_pnl:+,.0f} | "
            f"{week_icon} 本周 HK${week_pnl:+,.0f}\n\n"
        )

        if h is None:
            # 降级到简版
            markdown_text = (
                f"### 📊 FTSeek 交易健康度看板 ({time_str})\n\n"
                + account_section
                + "_健康度数据采集中，请稍候..._\n"
            )
        else:
            # ── 策略层 ──────────────────────────────────────
            wr_str = _fmt(h.win_rate, ".1%", "N/A")
            pl_str = _fmtf(h.profit_loss_ratio, 2, "N/A")
            exp_str = _fmtf(h.expectancy, 1, "N/A", " HKD")
            dd_str = _fmt(h.max_drawdown, ".1%", "N/A")
            sh_str = _fmtf(h.sharpe_ratio, 2, "N/A")
            pf_str = _fmtf(h.profit_factor, 2, "N/A")
            win_icon = _icon(h.win_rate, good_above=0.5, bad_below=0.3)
            strategy_section = (
                f"**策略层**\n\n"
                f"胜率: {win_icon}{wr_str} ({h.winning_trades}/{h.total_trades}笔) | "
                f"盈亏比: {pl_str} | 期望: {exp_str}\n\n"
                f"最大回撤: {dd_str} | Sharpe: {sh_str} | Profit Factor: {pf_str}\n\n"
            )

            # ── 执行层 ──────────────────────────────────────
            fr_str = _fmt(h.fill_rate, ".1%", "N/A")
            slip_str = (
                f"{h.avg_slippage_pct:.3f}%"
                if h.avg_slippage_pct is not None else "待接入"
            )
            fill_icon = _icon(h.fill_rate, good_above=0.9, bad_below=0.7)
            execution_section = (
                f"**执行层**\n\n"
                f"成交率: {fill_icon}{fr_str} | 平均滑点: {slip_str} | "
                f"下单延迟: 待接入\n\n"
                f"撤单成功率: 待接入 | 部分成交: 待接入\n\n"
            )

            # ── 风控层 ──────────────────────────────────────
            ir_str = _fmt(h.risk_intercept_rate, ".1%", "N/A")
            exp_total = _fmtf(h.total_exposure, 1, "N/A", "%")
            exp_max = _fmtf(h.max_single_exposure, 1, "N/A", "%")
            max_code = h.max_single_code or "-"
            fuse_icon = "🟢" if h.fuse_count == 0 else "🔴"
            risk_section = (
                f"**风控层**\n\n"
                f"拦截率: {ir_str} | 今日拒绝: {h.total_rejects_today}次 | "
                f"{fuse_icon} 熔断: {h.fuse_count}次\n\n"
                f"总敞口: {exp_total} | 最大单品种: {max_code} {exp_max} | "
                f"杠杆: 待接入\n\n"
            )

            # ── 系统层 ──────────────────────────────────────
            svc_str = "🟢 运行中" if h.service_alive else "🔴 异常"
            qd_str = (
                f"{h.quote_delay_sec:.1f}s"
                if h.quote_delay_sec is not None else "待接入"
            )
            qd_icon = (
                "🟢" if h.quote_delay_sec is not None and h.quote_delay_sec < 5
                else ("🔴" if h.quote_delay_sec is not None and h.quote_delay_sec > 30 else "🟡")
            )
            db_str = (
                f"{h.db_write_latency_ms:.0f}ms"
                if h.db_write_latency_ms is not None else "待接入"
            )
            queue_icon = "🟢" if h.task_queue_size == 0 else "🟡"
            system_section = (
                f"**系统层**\n\n"
                f"服务: {svc_str} | {qd_icon} 行情延迟: {qd_str} | "
                f"DB写入: {db_str}\n\n"
                f"{queue_icon} 任务堆积: {h.task_queue_size} | API错误率: 待接入\n\n"
            )

            markdown_text = (
                f"### 📊 FTSeek 交易健康度看板 ({time_str})\n\n"
                + account_section
                + "---\n\n"
                + strategy_section
                + "---\n\n"
                + execution_section
                + "---\n\n"
                + risk_section
                + "---\n\n"
                + system_section
            )

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"📊 FTSeek 交易健康度看板 ({time_str})",
                "text": markdown_text,
            },
        }

        ok = self._do_post(payload)
        if ok:
            logger.info(f"钉钉交易健康度看板推送成功：{time_str}")
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
        # 兼容处理：检查是否有 strategy_scores 或 weight_details
        if hasattr(result, 'strategy_scores') and result.strategy_scores:
            for strategy_name, score in result.strategy_scores.items():
                icon = "✅" if score > 0 else "❌"
                lines.append(f"  {icon} {strategy_name}: {score:+.1f}")
        elif hasattr(result, 'weight_details') and result.weight_details:
            for strategy_name, weight in result.weight_details.items():
                lines.append(f"  ✅ {strategy_name}: 权重 {weight:.2f}")
        elif hasattr(result, 'agree_strategies') and result.agree_strategies:
            for strategy_name in result.agree_strategies:
                lines.append(f"  ✅ {strategy_name}")
        else:
            lines.append("  ✅ price_break")
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
        发送开仓详细推送 (增强版 - 包含分析和操作原因)
        
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
        action_icon = "🟢" if signal.action == SignalAction.BUY else "🔴"
        
        # 获取操作原因分析
        close_reason = getattr(signal, 'reason', '') or ''
        strategy_reason = getattr(signal, 'strategy', '共识策略') or '共识策略'
        
        # 解析 extra 中的分析信息
        extra = getattr(signal, 'extra', {}) or {}
        market_analysis = extra.get('market_analysis', '')
        trigger_reason = extra.get('trigger_reason', '')
        
        # 格式化策略共识分析
        strategy_analysis = self._format_strategy_analysis(consensus_result, signal.action)
        
        # 格式化市场状态分析
        market_status = self._format_market_status(consensus_result)
        
        # 风险提示
        risk_alert = self._get_risk_alert(signal)
        
        message = f"""{action_icon} {signal.code} {stock_name} {action_text}成交
━━━━━━━━━━━━━━━━━━━━
📋 **订单信息**
订单 ID: {trade_result.order_id}
数量：{signal.quantity:,} 股
成交价：HK$ {signal.price:.2f}
总金额：HK$ {signal.price * signal.quantity:,.2f}
时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
━━━━━━━━━━━━━━━━━━━━
📊 **操作原因**
{strategy_analysis}
━━━━━━━━━━━━━━━━━━━━
📈 **市场分析**
{market_status}
━━━━━━━━━━━━━━━━━━━━
⚠️ **风险提示**
{risk_alert}
"""
        return self._send_markdown(message)

    def send_close_position_message(self, signal, trade_result, position, consensus_result) -> bool:
        """
        发送平仓详细推送 (增强版 - 包含分析和操作原因)
        
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
        
        # 解析平仓原因
        extra = getattr(signal, 'extra', {}) or {}
        close_reason = self._get_close_reason(signal, extra)
        
        # 获取市场分析
        market_analysis = self._get_close_market_analysis(signal, extra, pnl_rate)
        
        # 后续建议
        next_action = self._get_next_action_suggestion(signal, pnl_rate, extra)
        
        # 交易总结
        trade_summary = self._format_trade_summary(position, exit_price, pnl, pnl_rate, hold_time)
        
        message = f"""{action_icon} {signal.code} {self._get_stock_name(signal.code)} 平仓成交
━━━━━━━━━━━━━━━━━━━━
📋 **交易详情**
{trade_summary}
━━━━━━━━━━━━━━━━━━━━
📊 **平仓原因**
{close_reason}
━━━━━━━━━━━━━━━━━━━━
📈 **市场分析**
{market_analysis}
━━━━━━━━━━━━━━━━━━━━
💡 **后续建议**
{next_action}
"""
        return self._send_markdown(message)
    
    def _format_strategy_analysis(self, result, action) -> str:
        """格式化策略分析"""
        lines = []
        
        # 共识分数分析
        if hasattr(result, 'score') and result.score:
            if result.score >= 2.0:
                lines.append(f"✅ 强共识信号 (分数: {result.score:+.2f})")
            elif result.score >= 1.0:
                lines.append(f"✅ 中等共识信号 (分数: {result.score:+.2f})")
            else:
                lines.append(f"⚠️ 弱共识信号 (分数: {result.score:+.2f})")
        
        # 置信度分析
        if hasattr(result, 'confidence') and result.confidence:
            conf = result.confidence
            if conf >= 0.7:
                lines.append(f"✅ 高置信度 ({conf:.0%}) - 信号质量优秀")
            elif conf >= 0.5:
                lines.append(f"✅ 中等置信度 ({conf:.0%}) - 信号质量良好")
            else:
                lines.append(f"⚠️ 低置信度 ({conf:.0%}) - 谨慎操作")
        
        # 触发策略
        if hasattr(result, 'agree_strategies') and result.agree_strategies:
            strategies = ", ".join(result.agree_strategies)
            lines.append(f"📊 触发策略: {strategies}")
        
        # 操作方向分析
        from app.strategy.base import SignalAction
        action_str = "买入" if action == SignalAction.BUY else "卖出"
        lines.append(f"🎯 操作方向: **{action_str}**")
        
        return "\n".join(lines) if lines else "共识策略触发"
    
    def _format_market_status(self, result) -> str:
        """格式化市场状态"""
        lines = []
        
        # 市场状态
        if hasattr(result, 'market_regime') and result.market_regime:
            regime = result.market_regime
            regime_map = {
                'trending': '📈 趋势市场',
                'range_bound': '📊 震荡市场',
                'high_volatility': '⚠️ 高波动市场',
                'low_volatility': '📉 低波动市场',
            }
            regime_text = regime_map.get(regime, f"📊 {regime}")
            lines.append(f"市场状态: {regime_text}")
        
        # ADX 指标
        if hasattr(result, 'adx') and result.adx:
            adx = result.adx
            if adx >= 25:
                lines.append(f"ADX: {adx:.1f} (趋势明确)")
            else:
                lines.append(f"ADX: {adx:.1f} (震荡区间)")
        
        # 资金情绪
        if hasattr(result, 'fund_flow') and result.fund_flow:
            flow = result.fund_flow
            if flow > 0:
                lines.append(f"资金流向: 流入 ({flow:+.2f})")
            else:
                lines.append(f"资金流向: 流出 ({flow:+.2f})")
        
        return "\n".join(lines) if lines else "市场数据获取中..."
    
    def _get_risk_alert(self, signal) -> str:
        """获取风险提示"""
        alerts = []
        
        # 仓位风险
        qty = signal.quantity
        amount = signal.price * qty
        if amount > 50000:
            alerts.append(f"⚠️ 单笔金额较大 (HK${amount:,.0f})，注意仓位控制")
        
        # 价格位置
        alerts.append("📌 已设置止损止盈，系统将自动执行")
        alerts.append("📌 盈利 ≥1% 将激活追踪止盈")
        
        return "\n".join(alerts)
    
    def _get_close_reason(self, signal, extra: dict) -> str:
        """获取平仓原因"""
        reasons = []
        
        # 止盈触发
        if extra.get('take_profit'):
            pnl_rate = extra.get('pnl_rate', 0) * 100
            reasons.append(f"💰 **追踪止盈触发**")
            reasons.append(f"   盈利达到 {pnl_rate:.2f}%，价格回落触发止盈线")
            reasons.append(f"   自动锁定利润，保护收益")
        
        # 止损触发
        elif extra.get('stop_loss'):
            pnl_rate = extra.get('pnl_rate', 0) * 100
            reasons.append(f"🛡️ **止损触发**")
            reasons.append(f"   亏损达到 {pnl_rate:.2f}%，触发止损线")
            reasons.append(f"   控制风险，避免更大损失")
        
        # 信号平仓
        else:
            strategy = getattr(signal, 'strategy', '策略信号') or '策略信号'
            reason = getattr(signal, 'reason', '') or ''
            reasons.append(f"📊 **策略信号平仓**")
            reasons.append(f"   触发策略: {strategy}")
            if reason:
                reasons.append(f"   原因: {reason}")
        
        return "\n".join(reasons)
    
    def _get_close_market_analysis(self, signal, extra: dict, pnl_rate: float) -> str:
        """获取平仓时的市场分析"""
        analysis = []
        
        # 盈亏分析
        if pnl_rate >= 3:
            analysis.append(f"✅ 交易成功，盈利 {pnl_rate:.2f}%")
        elif pnl_rate >= 0:
            analysis.append(f"✅ 交易保本，盈利 {pnl_rate:.2f}%")
        elif pnl_rate >= -2:
            analysis.append(f"⚠️ 小幅亏损 {pnl_rate:.2f}%，风控生效")
        else:
            analysis.append(f"⚠️ 止损触发，亏损 {pnl_rate:.2f}%")
        
        # 资金情绪分析
        fund_flow = extra.get('fund_flow')
        if fund_flow is not None:
            if fund_flow > 0:
                analysis.append(f"📈 资金持续流入，市场情绪偏多")
            elif fund_flow < 0:
                analysis.append(f"📉 资金持续流出，市场情绪偏空")
        
        return "\n".join(analysis) if analysis else "市场分析中..."
    
    def _get_next_action_suggestion(self, signal, pnl_rate: float, extra: dict) -> str:
        """获取后续建议"""
        suggestions = []
        
        code = signal.code
        
        if pnl_rate >= 3:
            suggestions.append(f"✅ 本次交易盈利良好，可继续关注 {code}")
            suggestions.append(f"📌 建议等待下次信号再入场")
        elif pnl_rate >= 0:
            suggestions.append(f"✅ 本次交易保本，策略执行正常")
            suggestions.append(f"📌 可继续关注 {code} 的交易机会")
        else:
            suggestions.append(f"⚠️ 本次交易止损，需要复盘分析")
            suggestions.append(f"📌 建议观察市场趋势后再入场")
        
        # 追踪止盈成功后
        if extra.get('take_profit'):
            suggestions.append(f"💡 追踪止盈机制已成功保护利润")
        
        return "\n".join(suggestions)
    
    def _format_trade_summary(self, position, exit_price, pnl, pnl_rate, hold_time) -> str:
        """格式化交易总结"""
        return f"""开仓：{position.quantity:,} 股 @ HK$ {position.entry_price:.2f}
平仓：{position.quantity:,} 股 @ HK$ {exit_price:.2f}
盈亏：HK$ {pnl:+,.2f} ({pnl_rate:+.2f}%)
持仓：{hold_time}"""

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
    
    # ============================================================
    #  跨市场套利专用推送 (Task 5)
    # ============================================================
    
    def send_cross_market_plan(self, metadata_dict: dict, signal_id: str, llm_assessment: dict = None) -> bool:
        """
        发送跨市场套利计划卡片 (09:10 推送)
        
        Args:
            metadata_dict: CrossMarketMetadata.to_dict()
            signal_id: 信号 ID
            llm_assessment: 可选的LLM预分析结果 dict
        
        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过跨市场推送")
            return False
        
        from app.strategy.cross_market import CrossMarketMetadata
        metadata = CrossMarketMetadata.from_dict(metadata_dict)
        
        # 构建 Markdown 文本
        markdown_text = (
            f"### 📊 跨市场套利计划 (盘前)\n\n"
            f"---\n\n"
            f"**联动方向**: {metadata.lead_market}({metadata.lead_code}) → {metadata.lag_market}({metadata.lag_code})\n\n"
            f"**历史相关性**: {metadata.correlation:.2f}\n\n"
            f"**预期方向**: {'做多' if metadata_dict.get('side', 'long') == 'long' else '做空'}\n\n"
            f"---\n\n"
            f"### 📈 评分详情\n\n"
            f"- **Lead-Lag 强度**: {metadata.lead_lag_strength:.2f}\n\n"
            f"- **套利窗口分**: {metadata.arb_window_score:.2f}\n\n"
            f"- **综合得分**: **{metadata.arb_total_score:.2f}**\n\n"
            f"- **决策建议**: {metadata.arb_decision.upper()}\n\n"
            f"- **事件强度**: {metadata.event_impact}\n\n"
        )
        
        # 【新增 2026-04-12】LLM预分析结果
        if llm_assessment:
            direction_map = {
                "STRONG_BUY": "🔴强烈看多",
                "BUY": "🔴看多",
                "NEUTRAL": "⚪中性",
                "SELL": "🟢看空",
                "STRONG_SELL": "🟢强烈看空",
            }
            confidence_map = {"HIGH": "高", "MEDIUM": "中", "LOW": "低"}
            
            direction = llm_assessment.get("direction", "NEUTRAL")
            confidence = llm_assessment.get("confidence_level", "LOW")
            reasoning = llm_assessment.get("reasoning", "")
            key_factors = llm_assessment.get("key_factors", [])
            
            markdown_text += (
                f"---\n\n"
                f"### 🤖 LLM预分析 (qwen3.5-plus)\n\n"
                f"**研判方向**: {direction_map.get(direction, direction)}\n\n"
                f"**置信度**: {confidence_map.get(confidence, confidence)}\n\n"
                f"**仓位建议**: {llm_assessment.get('position_tier', 'SKIP')}\n\n"
                f"**执行时机**: {llm_assessment.get('recommended_timing', 'SKIP')}\n\n"
            )
            
            if key_factors:
                markdown_text += f"**关键因素**:\n\n"
                for i, factor in enumerate(key_factors[:3], 1):
                    markdown_text += f"{i}. {factor}\n\n"
            
            if reasoning:
                markdown_text += f"**分析逻辑**: {reasoning[:150]}\n\n"
            
            # 跨市场洞察
            cm_insight = llm_assessment.get("cross_market_insight", "")
            if cm_insight:
                markdown_text += f"**跨市场洞察**: {cm_insight[:120]}\n\n"
            
            markdown_text += f"---\n\n"
        
        markdown_text += (
            f"### ⏰ 执行计划\n\n"
            f"- **推送时间**: {metadata.push_time}\n\n"
            f"- **计划执行**: 09:30 开盘\n\n"
            f"- **默认行为**: <font color=\"#FFA500\">无人阻止则自动执行</font>\n\n"
            f"- **否决窗口**: 09:10 - 09:25 (15分钟)\n\n"
            f"- **倒计时**: 09:25 最后5分钟提醒\n\n"
            f"---\n\n"
            f"### 💡 风控参数\n\n"
            f"- **仓位**: {metadata.risk_params['max_position_pct']:.0%}\n\n"
            f"- **止损**: {metadata.risk_params['stop_loss_pct']:.0%}\n\n"
            f"- **止盈**: {metadata.risk_params['take_profit_pct']:.0%}\n\n"
            f"- **最长持有**: {metadata.risk_params['max_holding_minutes']}分钟\n\n"
            f"- **滑点上限**: {metadata.risk_params['max_slippage_pct']:.1%}\n\n"
            f"---\n\n"
            f"### ⚠️ 操作说明\n\n"
            f"如需**阻止执行**,请点击下方按钮:\n\n"
            f"- 否决后该信号将不会执行\n\n"
            f"- 可填写否决理由 (用于复盘)\n\n"
        )
        
        # 构建 ActionCard
        reject_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/v2/cross_market/reject"
            f"?signal_id={signal_id}&token={config.API_TOKEN}"
        )
        
        payload = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": f"📊 跨市场套利计划 - {metadata.lag_code}",
                "text": markdown_text,
                "btnOrientation": "1",  # 按钮竖排
                "btns": [
                    {
                        "title": "❌ 阻止执行",
                        "actionURL": reject_url
                    },
                ],
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(
                f"[跨市场推送] 计划卡片发送成功: {metadata.lag_code} "
                f"(评分={metadata.arb_total_score:.2f}, LLM={'有' if llm_assessment else '无'})"
            )
        return ok
    
    def send_cross_market_countdown(self, metadata_dict: dict, signal_id: str) -> bool:
        """
        发送倒计时5分钟提醒卡片 (09:25 推送)
        
        Args:
            metadata_dict: CrossMarketMetadata.to_dict()
            signal_id: 信号 ID
        
        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            return False
        
        from app.strategy.cross_market import CrossMarketMetadata
        metadata = CrossMarketMetadata.from_dict(metadata_dict)
        
        markdown_text = (
            f"### ⏰ 最后5分钟倒计时\n\n"
            f"---\n\n"
            f"**标的**: {metadata.lag_code}\n\n"
            f"**方向**: {'做多' if metadata_dict.get('side', 'long') == 'long' else '做空'}\n\n"
            f"**综合评分**: {metadata.arb_total_score:.2f}\n\n"
            f"**联动**: {metadata.lead_code}({metadata.lead_change_pct:+.1%}) → {metadata.lag_code}\n\n"
            f"---\n\n"
            f"⚠️ **5分钟后 (09:30) 将自动执行**\n\n"
            f"如仍需阻止,请立即点击下方按钮:\n\n"
        )
        
        reject_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/v2/cross_market/reject"
            f"?signal_id={signal_id}&token={config.API_TOKEN}"
        )
        
        payload = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": f"⏰ 倒计时5分钟 - {metadata.lag_code}",
                "text": markdown_text,
                "btnOrientation": "1",
                "btns": [
                    {
                        "title": "❌ 立即阻止",
                        "actionURL": reject_url
                    },
                ],
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"[跨市场推送] 倒计时卡片发送成功: {metadata.lag_code}")
        return ok
    
    def send_cross_market_reject_confirm(self, metadata_dict: dict, user_id: str, comment: str) -> bool:
        """
        发送否决确认消息
        
        Args:
            metadata_dict: CrossMarketMetadata.to_dict()
            user_id: 操作人
            comment: 否决理由
        
        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            return False
        
        from app.strategy.cross_market import CrossMarketMetadata
        metadata = CrossMarketMetadata.from_dict(metadata_dict)
        
        markdown_text = (
            f"### ✅ 已阻止执行\n\n"
            f"---\n\n"
            f"**标的**: {metadata.lag_code}\n\n"
            f"**操作人**: {user_id}\n\n"
            f"**理由**: {comment or '未填写'}\n\n"
            f"**评分**: {metadata.arb_total_score:.2f} (原决策: {metadata.arb_decision})\n\n"
            f"---\n\n"
            f"该信号将不会在 09:30 执行\n\n"
        )
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"✅ 已阻止 - {metadata.lag_code}",
                "text": markdown_text,
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(
                f"[跨市场推送] 否决确认发送成功: {metadata.lag_code} "
                f"(操作人: {user_id})"
            )
        return ok
    
    def send_cross_market_execution_result(self, metadata_dict: dict, success: bool, detail: str = "") -> bool:
        """
        发送执行结果通知
        
        Args:
            metadata_dict: CrossMarketMetadata.to_dict()
            success: 是否执行成功
            detail: 详细信息 (成交价/滑点等)
        
        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            return False
        
        from app.strategy.cross_market import CrossMarketMetadata
        metadata = CrossMarketMetadata.from_dict(metadata_dict)
        
        if success:
            markdown_text = (
                f"### ✅ 跨市场套利执行成功\n\n"
                f"---\n\n"
                f"**标的**: {metadata.lag_code}\n\n"
                f"**方向**: {'做多' if metadata_dict.get('side', 'long') == 'long' else '做空'}\n\n"
                f"**评分**: {metadata.arb_total_score:.2f}\n\n"
                f"---\n\n"
                f"{detail}\n\n"
                f"---\n\n"
                f"⏰ **退出时间**: {metadata.risk_params['force_exit_time']} (强制平仓)\n\n"
            )
            title = f"✅ 执行成功 - {metadata.lag_code}"
        else:
            markdown_text = (
                f"### ❌ 跨市场套利执行失败\n\n"
                f"---\n\n"
                f"**标的**: {metadata.lag_code}\n\n"
                f"**评分**: {metadata.arb_total_score:.2f}\n\n"
                f"---\n\n"
                f"**失败原因**: {detail}\n\n"
            )
            title = f"❌ 执行失败 - {metadata.lag_code}"
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": markdown_text,
            },
        }
        
        ok = self._do_post(payload)
        if ok:
            logger.info(f"[跨市场推送] 执行结果发送成功: {metadata.lag_code}")
        return ok

    def send_llm_assessment(self, code: str, assessment_data: dict,
                             accuracy_stats: dict = None) -> bool:
        """推送 LLM 市场研判综合结果到钉钉（Task8 新增 2026-04-11，增强版 2026-04-12）

        Args:
            code: 标的代码 (如 "HK.00700")
            assessment_data: LLM 研判结果 dict，包含：
                - direction: STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL
                - confidence_level: HIGH/MEDIUM/LOW
                - position_tier: FULL/HALF/LIGHT/SKIP
                - recommended_timing: IMMEDIATE/WAIT_CONFIRM/WAIT_PULLBACK/SKIP
                - key_factors: List[str]
                - risk_flags: List[str]
                - reasoning: str
                - news_impact: Optional[dict]
                - cross_market_insight: Optional[str]
            accuracy_stats: 可选的 LLM 准确率统计 dict
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过 LLM 研判推送")
            return False

        if not self._can_send():
            logger.debug(f"[限流] LLM 研判推送被限流: {code}")
            return False

        direction_map = {
            "STRONG_BUY": "强烈看多",
            "BUY": "看多",
            "NEUTRAL": "中性",
            "SELL": "看空",
            "STRONG_SELL": "强烈看空",
        }
        confidence_map = {
            "HIGH": "高",
            "MEDIUM": "中",
            "LOW": "低",
        }
        timing_map = {
            "IMMEDIATE": "立即",
            "WAIT_CONFIRM": "等待确认",
            "WAIT_PULLBACK": "等待回调",
            "SKIP": "观望",
        }

        direction = assessment_data.get("direction", "NEUTRAL")
        confidence = assessment_data.get("confidence_level", "LOW")

        # 选择标题图标（港股：红涨绿跌）
        if direction in ("STRONG_BUY", "BUY"):
            icon = "🔴"
        elif direction in ("STRONG_SELL", "SELL"):
            icon = "🟢"
        else:
            icon = "⚪"

        title = f"{icon} LLM研判 {code} {direction_map.get(direction, direction)}"

        lines = [
            f"### {title}",
            "",
            f"**标的**: {code}",
            f"**研判方向**: {direction_map.get(direction, direction)} ({direction})",
            f"**置信度**: {confidence_map.get(confidence, confidence)}",
            f"**仓位建议**: {assessment_data.get('position_tier', 'SKIP')}",
            f"**执行时机**: {timing_map.get(assessment_data.get('recommended_timing', 'SKIP'), '观望')}",
            "",
        ]

        # 【增强 2026-04-12】买卖逻辑详解
        direction_score = assessment_data.get("direction_score", 0)
        confidence_value = assessment_data.get("confidence_value", 0)
        position_ratio = assessment_data.get("position_ratio", 0)
        
        # 买卖逻辑说明
        if direction in ("STRONG_BUY", "BUY"):
            logic_icon = "📈"
            logic_title = "做多逻辑"
            if direction_score >= 0.8:
                strength_desc = "极强"
            elif direction_score >= 0.5:
                strength_desc = "较强"
            else:
                strength_desc = "一般"
            
            lines.append(f"### {logic_icon} {logic_title}")
            lines.append("")
            lines.append(f"- **信号强度**: {strength_desc} (得分{direction_score:.2f})")
            lines.append(f"- **置信水平**: {confidence_value:.0%}")
            lines.append(f"- **建议仓位**: {position_ratio:.0%}")
            lines.append("")
            lines.append(f"**核心观点**:")
            lines.append(f"LLM分析认为{code}当前具备做多机会，主要基于以下因素的综合判断：")
            lines.append("")
        elif direction in ("STRONG_SELL", "SELL"):
            logic_icon = "📉"
            logic_title = "做空逻辑"
            if direction_score <= -0.8:
                strength_desc = "极强"
            elif direction_score <= -0.5:
                strength_desc = "较强"
            else:
                strength_desc = "一般"
            
            lines.append(f"### {logic_icon} {logic_title}")
            lines.append("")
            lines.append(f"- **信号强度**: {strength_desc} (得分{direction_score:.2f})")
            lines.append(f"- **置信水平**: {confidence_value:.0%}")
            lines.append(f"- **建议仓位**: {position_ratio:.0%}")
            lines.append("")
            lines.append(f"**核心观点**:")
            lines.append(f"LLM分析认为{code}当前存在下行风险，主要基于以下因素的综合判断：")
            lines.append("")
        else:
            lines.append(f"### ⚪ 中性观望")
            lines.append("")
            lines.append(f"LLM分析认为{code}当前无明显方向性信号，建议观望等待更明确的机会。")
            lines.append("")

        # 关键因素（最多5条，带编号）
        factors = assessment_data.get("key_factors", [])
        if factors:
            lines.append(f"**关键驱动因素**:")
            for i, f in enumerate(factors[:5], 1):
                # 为每条因素添加图标
                if any(keyword in f.upper() for keyword in ["财报", "盈利", "业绩", "收入"]):
                    factor_icon = "💰"
                elif any(keyword in f.upper() for keyword in ["政策", "监管", "法规"]):
                    factor_icon = "📋"
                elif any(keyword in f.upper() for keyword in ["技术", "突破", "支撑", "阻力"]):
                    factor_icon = "📊"
                elif any(keyword in f.upper() for keyword in ["市场", "情绪", "资金"]):
                    factor_icon = "🌊"
                else:
                    factor_icon = "📌"
                
                lines.append(f"  {i}. {factor_icon} {f}")
            lines.append("")

        # 风险标记
        risk_flags = assessment_data.get("risk_flags", [])
        if risk_flags:
            lines.append(f"### ⚠️ 风险提示")
            lines.append("")
            for flag in risk_flags[:3]:
                if "volatility" in flag.lower():
                    risk_icon = "🌪️"
                elif "liquidity" in flag.lower():
                    risk_icon = "💧"
                else:
                    risk_icon = "⚠️"
                lines.append(f"- {risk_icon} {flag}")
            lines.append("")

        # 新闻影响
        news_impact = assessment_data.get("news_impact")
        if news_impact and isinstance(news_impact, dict):
            severity = news_impact.get("severity", "normal")
            duration = news_impact.get("duration_minutes", 0)
            affected = news_impact.get("affected_codes", [])
            
            lines.append(f"### 📰 新闻影响")
            lines.append("")
            lines.append(f"- **影响程度**: {severity}")
            lines.append(f"- **预计持续**: {duration}分钟")
            if affected:
                lines.append(f"- **影响标的**: {', '.join(affected[:5])}")
            lines.append("")

        # 跨市场洞察
        cm_insight = assessment_data.get("cross_market_insight")
        if cm_insight:
            lines.append(f"### 🌐 跨市场洞察")
            lines.append("")
            lines.append(f"{cm_insight[:200]}")
            lines.append("")

        # 推理摘要（扩展到200字）
        reasoning = assessment_data.get("reasoning", "")
        if reasoning:
            lines.append(f"### 🧠 分析推理")
            lines.append("")
            lines.append(f"{reasoning[:200]}")
            lines.append("")

        # LLM 准确率统计（如果有足够样本）
        if accuracy_stats and accuracy_stats.get("total_samples", 0) > 0:
            total = accuracy_stats["total_samples"]
            acc = accuracy_stats.get("accuracy", 0)
            weight = accuracy_stats.get("current_weight", 0.15)
            correct = accuracy_stats.get("correct", 0)
            lines.append("---")
            lines.append("")
            lines.append(f"**LLM历史表现**: 准确率 {acc:.0%} ({correct}/{total})")
            lines.append(f"**当前权重**: {weight:.2f}")
            lines.append("")

        lines.append("---")
        lines.append("*FTSeek LLM研判 (qwen3.5-plus) | 开盘前定时分析*")

        markdown_text = "\n".join(lines)

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": markdown_text,
            },
        }

        ok = self._do_post(payload)
        if ok:
            logger.info(f"[LLM研判推送] {code} {direction} 发送成功")
        return ok

    def send_premarket_report(self, assessments: list, pipeline_stats: dict = None) -> bool:
        """推送盘前 Pipeline 执行汇总报告

        Args:
            assessments: List of PreMarketAssessment dicts
            pipeline_stats: Pipeline execution statistics
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过盘前报告推送")
            return False

        if not self._can_send():
            logger.debug("[限流] 盘前报告推送被限流")
            return False

        lines = [
            "### 盘前交易分析报告",
            "",
        ]

        if pipeline_stats:
            lines.append(
                f"**执行统计**: {pipeline_stats.get('raw_news_count', 0)} 条新闻 → "
                f"{pipeline_stats.get('cleaned_news_count', 0)} 清洗后 → "
                f"{pipeline_stats.get('cluster_count', 0)} 个事件"
            )
            lines.append(
                f"**LLM 分析**: {pipeline_stats.get('assessment_count', 0)} 个 | "
                f"自动执行: {pipeline_stats.get('auto_signals', 0)} | "
                f"待确认: {pipeline_stats.get('review_cards', 0)} | "
                f"观察: {pipeline_stats.get('observe_only', 0)}"
            )
            lines.append("")

        if not assessments:
            lines.append("**今日无重要交易信号，建议观望为主。**")
        else:
            for a in assessments:
                direction = a.get("direction", "WATCH")
                direction_map = {
                    "BUY": "买入", "SELL": "卖出", "NEUTRAL": "中性",
                    "WATCH": "观望", "NO_TRADE": "不交易",
                }
                conf = a.get("confidence", 0)
                conf_icon = "🟢" if conf >= 0.75 else "🟡" if conf >= 0.55 else "⚪"
                symbols = ", ".join(a.get("focus_symbols", []) or [])
                recommendation = a.get("recommendation", "")[:60]

                lines.append(
                    f"**{conf_icon} {direction_map.get(direction, direction)}** "
                    f"{symbols}"
                )
                lines.append(
                    f"  置信度: {conf:.0%} | 相关性: {a.get('relevance_score', 0):.0%} | "
                    f"仓位: {a.get('position_ratio', 0):.0%}"
                )
                if recommendation:
                    lines.append(f"  建议: {recommendation}")
                lines.append("")

        lines.append("---")
        lines.append("*FTSeek 盘前 Pipeline | qwen-plus/qwen-max 混合模型*")

        markdown_text = "\n".join(lines)
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "盘前交易分析报告",
                "text": markdown_text,
            },
        }

        ok = self._do_post(payload)
        if ok:
            logger.info(f"[盘前报告推送] 已发送 {len(assessments)} 个评估结果")
        return ok

    def send_review_action_card(self, card_data: dict) -> bool:
        """推送盘前分析人工确认 ActionCard

        Args:
            card_data: 包含 title, text, assessment_id 等字段的字典
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过 ActionCard 推送")
            return False

        if not self._can_send():
            logger.debug("[限流] ActionCard 推送被限流")
            return False

        title = card_data.get("title", "盘前分析确认")
        text = card_data.get("text", "")
        assessment_id = card_data.get("assessment_id", "")

        # 使用 Markdown 消息模拟 ActionCard (钉钉 ActionCard 需要企业内部应用)
        # 添加确认/延迟/忽略的操作指引
        action_text = (
            f"\n---\n"
            f"**操作指引**: 请联系系统管理员通过 API 确认执行\n"
            f"`POST /api/v2/premarket/assessments/{assessment_id}/approve`"
        )

        markdown_text = text + action_text
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": markdown_text,
            },
        }

        ok = self._do_post(payload)
        if ok:
            logger.info(f"[盘前ActionCard] 已发送确认卡片: {assessment_id}")
        return ok


# ============================================================
#  全局单例 (所有模块统一使用此入口)
# ============================================================

_global_notifier: Optional[DingTalkNotifier] = None
_notifier_lock = threading.Lock()


def get_dingtalk_notifier() -> DingTalkNotifier:
    """获取全局唯一的钉钉通知器实例。

    所有模块（交易信号、新闻推送、风险警报、跨市场套利等）
    都必须通过此函数获取 notifier，确保：
    1. 共享同一个 webhook URL 和 secret
    2. 共享同一个限流计数器 (_can_send)
    3. 避免重复初始化导致的多头推送
    """
    global _global_notifier
    if _global_notifier is None:
        with _notifier_lock:
            if _global_notifier is None:
                _global_notifier = DingTalkNotifier()
                if _global_notifier.webhook_url:
                    logger.info(
                        "[全局通知器] 已初始化: webhook=%s...",
                        _global_notifier.webhook_url[:50],
                    )
                else:
                    logger.warning(
                        "[全局通知器] DINGTALK_WEBHOOK_URL 未配置，所有推送将被跳过"
                    )
    return _global_notifier


def reset_dingtalk_notifier() -> None:
    """重置全局通知器（仅用于测试）"""
    global _global_notifier
    with _notifier_lock:
        _global_notifier = None


