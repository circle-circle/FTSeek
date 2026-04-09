"""
美股价格监控器 (增强版)

功能:
- 定期通过富途 API 查询美股股价
- 检查持仓的止损止盈条件
- 触发条件时自动平仓 + 钉钉推送
- 实时计算浮动盈亏
- 距离止损/止盈价格提醒
- 异常波动告警 (如 ±5% 快速波动)
- 告警频率限制 (避免告警风暴)
- 告警确认机制

适用场景:
- 中长线投资 (支持高频监控)
- 止损止盈自动执行
- 跨时段监控 (美美股夜间交易)

版本：v2.0 (实时监控增强)
"""
import os
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from collections import defaultdict
import json

logger = logging.getLogger("ftseek.us_monitor")
logger.propagate = False  # 防止日志重复

# 导入富途 API
try:
    from futu import OpenQuoteContext, RET_OK
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    logger.warning("富途 SDK 未安装，美股监控将使用模拟模式")

# 导入项目模块
from app.storage.trade_recorder import get_trade_recorder
from app.trader.position_manager import PositionManager
from app.notifier.dingtalk import DingTalkNotifier


@dataclass
class MonitorConfig:
    """监控配置"""
    # 检查频率 (秒) - P0 优化：从 300 秒降至 30 秒
    check_interval: int = 30           # ✅ 30 秒检查一次 (原 5 分钟)
    night_interval: int = 60           # 夜间 1 分钟检查一次 (原 15 分钟)
    
    # 止损止盈参数
    default_stop_loss: float = -0.08   # 默认止损 -8%
    default_take_profit: float = 0.15  # 默认止盈 +15%
    
    # 追踪止盈
    trailing_stop_enabled: bool = True
    trailing_trigger: float = 0.10     # 盈利 10% 后启用追踪
    trailing_percent: float = 0.05     # 回撤 5% 触发
    
    # 通知
    dingtalk_enabled: bool = True
    push_on_trigger: bool = True       # 触发止损止盈时推送
    push_daily_summary: bool = True    # 每日汇总推送
    
    # 告警增强配置
    alert_rate_limit_seconds: int = 300    # 同类告警冷却时间 (5 分钟)
    volatility_alert_threshold: float = 0.05  # 异常波动阈值 (±5%)
    stop_loss_distance_alert: float = 0.02    # 距离止损 2% 时告警
    take_profit_distance_alert: float = 0.02  # 距离止盈 2% 时告警


@dataclass
class PriceHistory:
    """价格历史记录 (用于波动率计算)"""
    prices: List[float] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)
    max_size: int = 100  # 最多保留 100 个价格点
    
    def add(self, price: float, timestamp: float = None):
        """添加价格记录"""
        if timestamp is None:
            timestamp = time.time()
        
        self.prices.append(price)
        self.timestamps.append(timestamp)
        
        # 限制大小
        if len(self.prices) > self.max_size:
            self.prices.pop(0)
            self.timestamps.pop(0)
    
    def get_volatility(self, window_seconds: int = 60) -> float:
        """
        计算波动率 (指定时间窗口内的价格变化)
        
        Args:
            window_seconds: 时间窗口 (秒)
        
        Returns:
            波动率 (百分比)
        """
        if len(self.prices) < 2:
            return 0.0
        
        current_time = time.time()
        cutoff_time = current_time - window_seconds
        
        # 找到窗口内的价格
        window_prices = []
        for i, ts in enumerate(self.timestamps):
            if ts >= cutoff_time:
                window_prices.append(self.prices[i])
        
        if len(window_prices) < 2:
            return 0.0
        
        # 计算窗口内最大波动
        min_price = min(window_prices)
        max_price = max(window_prices)
        avg_price = (min_price + max_price) / 2
        
        if avg_price == 0:
            return 0.0
        
        volatility = (max_price - min_price) / avg_price
        return volatility


class AlertRateLimiter:
    """告警频率限制器"""
    
    def __init__(self, cooldown_seconds: int = 300):
        """
        初始化频率限制器
        
        Args:
            cooldown_seconds: 冷却时间 (秒)
        """
        self.cooldown_seconds = cooldown_seconds
        self._last_alert_time: Dict[str, float] = {}
        self._alert_counts: Dict[str, int] = defaultdict(int)
    
    def should_send(self, alert_key: str) -> bool:
        """
        检查是否应该发送告警
        
        Args:
            alert_key: 告警唯一标识 (如 "stop_loss_US.AAPL")
        
        Returns:
            True=可以发送，False=在冷却中
        """
        current_time = time.time()
        last_time = self._last_alert_time.get(alert_key, 0)
        
        if current_time - last_time >= self.cooldown_seconds:
            self._last_alert_time[alert_key] = current_time
            self._alert_counts[alert_key] += 1
            return True
        
        return False
    
    def get_alert_count(self, alert_key: str) -> int:
        """获取某类告警的发送次数"""
        return self._alert_counts[alert_key]


class USStockMonitor:
    """美股价格监控器 (增强版)"""
    
    def __init__(self, config: MonitorConfig = None):
        self.config = config or MonitorConfig()
        self.quote_ctx = None
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # 组件
        self.recorder = get_trade_recorder()
        self.position_manager = PositionManager()
        self.notifier = DingTalkNotifier() if self.config.dingtalk_enabled else None
        
        # 状态
        self.last_check_time = 0
        self.check_count = 0
        self.trigger_count = 0
        
        # 持仓峰值缓存 (追踪止盈用)
        self.peak_prices: Dict[str, float] = {}
        
        # 价格历史记录 (波动率计算用)
        self.price_history: Dict[str, PriceHistory] = {}
        
        # 告警频率限制器
        self.alert_limiter = AlertRateLimiter(self.config.alert_rate_limit_seconds)
        
        # 告警确认状态
        self.alert_ack_status: Dict[str, bool] = {}
        
        logger.info(f"美股监控器 (增强版) 已初始化 | 检查间隔={self.config.check_interval}s | 告警冷却={self.config.alert_rate_limit_seconds}s")
    
    def _connect_quote_ctx(self) -> bool:
        """连接富途行情上下文"""
        if not FUTU_AVAILABLE:
            logger.warning("富途 SDK 不可用")
            return False
        
        try:
            from app import config as app_config
            self.quote_ctx = OpenQuoteContext(
                host=app_config.OPEND_HOST,
                port=app_config.OPEND_PORT
            )
            logger.info("富途行情上下文已连接")
            return True
        except Exception as e:
            logger.error(f"连接富途失败：{e}")
            return False
    
    def get_stock_price(self, code: str) -> Optional[float]:
        """
        获取股票最新价格
        
        Args:
            code: 股票代码 (US.AAPL, US.NVDA)
        
        Returns:
            最新价格，失败返回 None
        """
        if not self.quote_ctx:
            if not self._connect_quote_ctx():
                return None
        
        try:
            ret, data = self.quote_ctx.get_market_snapshot([code])
            
            if ret == RET_OK and data is not None and len(data) > 0:
                price = data['last_price'].iloc[0]
                logger.debug(f"{code} 最新价：{price}")
                
                # 记录价格历史
                if code not in self.price_history:
                    self.price_history[code] = PriceHistory()
                self.price_history[code].add(price)
                
                return price
            else:
                logger.warning(f"获取 {code} 行情失败：{data}")
                return None
        
        except Exception as e:
            logger.error(f"获取 {code} 行情异常：{e}")
            # 尝试重连
            self.quote_ctx = None
            return None
    
    def calculate_pnl(self, code: str, current_price: float) -> Dict:
        """
        实时计算浮动盈亏
        
        Args:
            code: 股票代码
            current_price: 当前价格
        
        Returns:
            盈亏信息字典
        """
        position = self.position_manager.get_position(code)
        if not position:
            return {}
        
        entry_price = position.entry_price
        action = position.action  # BUY=做多，SELL=做空
        
        # 计算盈亏比例和金额
        if action == 'BUY':
            pnl_rate = (current_price - entry_price) / entry_price
            pnl_amount = (current_price - entry_price) * position.quantity
        else:  # SELL (做空)
            pnl_rate = (entry_price - current_price) / entry_price
            pnl_amount = (entry_price - current_price) * position.quantity
        
        # 计算距离止损/止盈的百分比
        stop_loss = position.stop_loss or self.config.default_stop_loss
        take_profit = position.take_profit or self.config.default_take_profit
        
        # 距离止损
        if action == 'BUY':
            stop_loss_price = entry_price * (1 + stop_loss)
            distance_to_stop = (current_price - stop_loss_price) / current_price
        else:
            stop_loss_price = entry_price * (1 - stop_loss)
            distance_to_stop = (stop_loss_price - current_price) / current_price
        
        # 距离止盈
        if action == 'BUY':
            take_profit_price = entry_price * (1 + take_profit)
            distance_to_profit = (take_profit_price - current_price) / current_price
        else:
            take_profit_price = entry_price * (1 - take_profit)
            distance_to_profit = (current_price - take_profit_price) / current_price
        
        return {
            'pnl_rate': pnl_rate,
            'pnl_amount': pnl_amount,
            'entry_price': entry_price,
            'current_price': current_price,
            'stop_loss_price': stop_loss_price,
            'take_profit_price': take_profit_price,
            'distance_to_stop': distance_to_stop,
            'distance_to_profit': distance_to_profit,
        }
    
    def check_position(self, code: str, current_price: float) -> Optional[Dict]:
        """
        检查持仓的止损止盈条件
        
        Args:
            code: 股票代码
            current_price: 当前价格
        
        Returns:
            触发动作 (BUY/SELL + 原因), 无触发返回 None
        """
        position = self.position_manager.get_position(code)
        if not position:
            return None
        
        entry_price = position.entry_price
        action = position.action  # BUY=做多，SELL=做空
        
        # 计算盈亏比例
        if action == 'BUY':
            pnl_rate = (current_price - entry_price) / entry_price
        else:  # SELL (做空)
            pnl_rate = (entry_price - current_price) / entry_price
        
        # 更新峰值价格 (追踪止盈用)
        if pnl_rate > 0:
            if code not in self.peak_prices or current_price > self.peak_prices[code]:
                self.peak_prices[code] = current_price
        
        # 获取持仓的止损止盈参数
        stop_loss = position.stop_loss or self.config.default_stop_loss
        take_profit = position.take_profit or self.config.default_take_profit
        
        # 1. 检查止损
        if pnl_rate <= stop_loss:
            logger.warning(f"💔 止损触发：{code} 盈亏={pnl_rate:.2%} (止损线={stop_loss:.2%})")
            return {
                'action': 'SELL' if action == 'BUY' else 'BUY',
                'reason': 'stop_loss',
                'pnl_rate': pnl_rate,
                'entry_price': entry_price,
                'exit_price': current_price
            }
        
        # 2. 检查止盈
        if pnl_rate >= take_profit:
            logger.info(f"🎯 止盈触发：{code} 盈亏={pnl_rate:.2%} (止盈线={take_profit:.2%})")
            return {
                'action': 'SELL' if action == 'BUY' else 'BUY',
                'reason': 'take_profit',
                'pnl_rate': pnl_rate,
                'entry_price': entry_price,
                'exit_price': current_price
            }
        
        # 3. 检查追踪止盈
        if self.config.trailing_stop_enabled and pnl_rate >= self.config.trailing_trigger:
            peak_price = self.peak_prices.get(code, entry_price)
            peak_pnl = (peak_price - entry_price) / entry_price if action == 'BUY' else (entry_price - peak_price) / entry_price
            
            # 从峰值回撤
            if action == 'BUY':
                pullback = (peak_price - current_price) / peak_price
            else:
                pullback = (current_price - peak_price) / peak_price
            
            if pullback >= self.config.trailing_percent:
                logger.info(f"📉 追踪止盈触发：{code} 峰值={peak_price} 回撤={pullback:.2%}")
                return {
                    'action': 'SELL' if action == 'BUY' else 'BUY',
                    'reason': 'trailing_stop',
                    'pnl_rate': pnl_rate,
                    'peak_price': peak_price,
                    'pullback': pullback,
                    'exit_price': current_price
                }
        
        return None
    
    def check_distance_alerts(self, code: str, pnl_info: Dict):
        """
        检查距离止损/止盈告警
        
        Args:
            code: 股票代码
            pnl_info: 盈亏信息
        """
        distance_to_stop = pnl_info.get('distance_to_stop', 1.0)
        distance_to_profit = pnl_info.get('distance_to_profit', 1.0)
        pnl_rate = pnl_info.get('pnl_rate', 0)
        
        # 距离止损告警
        if 0 <= distance_to_stop <= self.config.stop_loss_distance_alert:
            alert_key = f"stop_distance_{code}"
            if self.alert_limiter.should_send(alert_key):
                self._send_distance_alert(
                    code=code,
                    alert_type='stop_loss',
                    distance=distance_to_stop,
                    pnl_rate=pnl_rate
                )
        
        # 距离止盈告警
        if 0 <= distance_to_profit <= self.config.take_profit_distance_alert:
            alert_key = f"profit_distance_{code}"
            if self.alert_limiter.should_send(alert_key):
                self._send_distance_alert(
                    code=code,
                    alert_type='take_profit',
                    distance=distance_to_profit,
                    pnl_rate=pnl_rate
                )
    
    def check_volatility_alert(self, code: str, current_price: float):
        """
        检查异常波动告警
        
        Args:
            code: 股票代码
            current_price: 当前价格
        """
        if code not in self.price_history:
            return
        
        # 计算 1 分钟波动率
        volatility = self.price_history[code].get_volatility(window_seconds=60)
        
        if volatility >= self.config.volatility_alert_threshold:
            alert_key = f"volatility_{code}"
            if self.alert_limiter.should_send(alert_key):
                self._send_volatility_alert(code, volatility, current_price)
    
    def _send_distance_alert(self, code: str, alert_type: str, distance: float, pnl_rate: float):
        """发送距离告警"""
        if not self.notifier:
            return
        
        emoji = '⚠️' if alert_type == 'stop_loss' else '🎯'
        alert_name = '接近止损' if alert_type == 'stop_loss' else '接近止盈'
        
        message = f"""{emoji} 距离提醒

📈 标的：{code}
🔔 事件：{alert_name}
📏 距离：{distance:.2%}
💰 当前盈亏：{pnl_rate:.2%}
⏰ 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

💡 请密切关注价格变化
"""
        
        try:
            self.notifier.send_text(message)
            logger.info(f"距离告警已发送：{code} {alert_type}")
        except Exception as e:
            logger.error(f"发送距离告警失败：{e}")
    
    def _send_volatility_alert(self, code: str, volatility: float, current_price: float):
        """发送异常波动告警"""
        if not self.notifier:
            return
        
        direction = "📈" if volatility > 0 else "📉"
        
        message = f"""{direction} 异常波动告警

📈 标的：{code}
📊 1 分钟波动：{volatility:.2%}
💵 当前价格：${current_price:.2f}
⏰ 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

⚠️ 请注意风险控制
"""
        
        try:
            self.notifier.send_text(message)
            logger.info(f"波动告警已发送：{code} 波动率={volatility:.2%}")
        except Exception as e:
            logger.error(f"发送波动告警失败：{e}")
    
    def execute_close(self, code: str, trigger: Dict):
        """
        执行平仓
        
        Args:
            code: 股票代码
            trigger: 触发动作信息
        """
        logger.info(f"执行平仓：{code} {trigger['action']} 原因={trigger['reason']}")
        
        # 1. 记录平仓
        try:
            records = self.recorder.get_all_records()
            target_trade = None
            for record in records:
                if record.code == code and not record.exit_price:
                    target_trade = record
                    break
            
            if target_trade:
                self.recorder.record_close(
                    trade_id=target_trade.trade_id,
                    exit_price=trigger['exit_price'],
                    exit_reason=trigger['reason'],
                    lessons_learned=f"PnL={trigger['pnl_rate']:.2%}"
                )
                logger.info(f"交易记录已更新：{target_trade.trade_id}")
        except Exception as e:
            logger.error(f"记录交易失败：{e}")
        
        # 2. 发送钉钉通知
        if self.notifier and self.config.push_on_trigger:
            self._send_notification(code, trigger)
        
        # 3. 清理缓存
        self.peak_prices.pop(code, None)
        self.price_history.pop(code, None)
        
        self.trigger_count += 1
    
    def _send_notification(self, code: str, trigger: Dict):
        """发送钉钉通知"""
        try:
            emoji = {
                'stop_loss': '💔',
                'take_profit': '🎯',
                'trailing_stop': '📉'
            }.get(trigger['reason'], '🔔')
            
            reason_text = {
                'stop_loss': '止损触发',
                'take_profit': '止盈触发',
                'trailing_stop': '追踪止盈'
            }.get(trigger['reason'], trigger['reason'])
            
            pnl_color = "✅" if trigger['pnl_rate'] > 0 else "❌"
            
            message = f"""{emoji} 美股交易提醒

📈 标的：{code}
🔔 事件：{reason_text}
💰 盈亏：{pnl_color} {trigger['pnl_rate']:.2%}
💵 入场：${trigger['entry_price']:.2f}
💵 出场：${trigger['exit_price']:.2f}
⏰ 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
            
            if trigger['reason'] == 'trailing_stop':
                message += f"""
📊 峰值：${trigger['peak_price']:.2f}
📉 回撤：{trigger['pullback']:.2%}
"""
            
            self.notifier.send_text(message)
            logger.info(f"钉钉通知已发送：{code}")
        
        except Exception as e:
            logger.error(f"发送通知失败：{e}")
    
    def check_all_positions(self):
        """检查所有持仓"""
        # 获取所有未平仓交易
        records = self.recorder.get_all_records()
        
        us_positions = [r for r in records if r.code.startswith('US.') and not r.exit_price]
        
        if not us_positions:
            logger.debug("无美股持仓")
            return
        
        logger.info(f"检查 {len(us_positions)} 个美股持仓...")
        
        for record in us_positions:
            code = record.code
            price = self.get_stock_price(code)
            
            if price is None:
                logger.warning(f"无法获取 {code} 价格，跳过")
                continue
            
            # 1. 计算实时盈亏
            pnl_info = self.calculate_pnl(code, price)
            if pnl_info:
                logger.debug(f"{code} 盈亏={pnl_info['pnl_rate']:.2%} 距离止损={pnl_info.get('distance_to_stop', 0):.2%}")
            
            # 2. 检查止损止盈触发
            trigger = self.check_position(code, price)
            
            if trigger:
                self.execute_close(code, trigger)
            else:
                # 3. 检查距离告警 (未触发时)
                if pnl_info:
                    self.check_distance_alerts(code, pnl_info)
                
                # 4. 检查异常波动
                self.check_volatility_alert(code, price)
    
    def _run_loop(self):
        """监控循环 (后台线程)"""
        logger.info("美股监控循环已启动 (30 秒间隔)")
        
        while not self._stop_event.is_set():
            try:
                # 判断是否为美东交易时段
                is_night = self._is_us_night()
                interval = self.config.night_interval if is_night else self.config.check_interval
                
                # 检查持仓
                self.check_all_positions()
                
                self.check_count += 1
                self.last_check_time = time.time()
                
                # 等待下次检查
                logger.debug(f"下次检查：{interval}秒后")
                self._stop_event.wait(interval)
            
            except Exception as e:
                logger.error(f"监控循环异常：{e}")
                time.sleep(60)  # 异常后等待 1 分钟
        
        logger.info("美股监控循环已停止")
    
    def _is_us_night(self) -> bool:
        """
        判断是否为美东夜间 (非交易时段)
        
        美东交易时间：9:30-16:00 (北京时间 21:30-次日 4:00)
        """
        now = datetime.now()
        hour = now.hour
        
        # 北京时间 21:30-次日 4:00 是美东交易时间
        if hour >= 21 or hour < 4:
            return False  # 交易时段，检查频繁
        else:
            return True   # 夜间时段，检查稀疏
    
    def start(self):
        """启动监控"""
        if self.running:
            logger.warning("美股监控已在运行中")
            return
        
        self.running = True
        self._stop_event.clear()
        
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        
        logger.info("美股监控已启动 (30 秒间隔)")
    
    def stop(self):
        """停止监控"""
        if not self.running:
            return
        
        self._stop_event.set()
        
        if self._thread:
            self._thread.join(timeout=5)
        
        self.running = False
        logger.info("美股监控已停止")
    
    def get_status(self) -> Dict:
        """获取监控状态"""
        return {
            'running': self.running,
            'check_count': self.check_count,
            'trigger_count': self.trigger_count,
            'last_check_time': datetime.fromtimestamp(self.last_check_time).isoformat() if self.last_check_time else None,
            'peak_prices': self.peak_prices,
            'config': {
                'check_interval': self.config.check_interval,
                'night_interval': self.config.night_interval,
                'stop_loss': self.config.default_stop_loss,
                'take_profit': self.config.default_take_profit,
                'alert_rate_limit': self.config.alert_rate_limit_seconds,
                'volatility_threshold': self.config.volatility_alert_threshold,
            },
            'alert_stats': {
                'total_alerts': sum(self.alert_limiter._alert_counts.values()),
                'by_type': dict(self.alert_limiter._alert_counts),
            }
        }


# 全局单例
_monitor = None

def get_us_stock_monitor(config: Dict = None) -> USStockMonitor:
    """获取美股监控器单例"""
    global _monitor
    if _monitor is None:
        if config:
            _monitor = USStockMonitor(MonitorConfig(**config))
        else:
            _monitor = USStockMonitor()
    return _monitor


if __name__ == "__main__":
    # 测试脚本
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    
    monitor = get_us_stock_monitor()
    
    # 测试获取股价
    test_codes = ['US.AAPL', 'US.NVDA', 'US.TSLA']
    for code in test_codes:
        price = monitor.get_stock_price(code)
        if price:
            print(f"{code}: ${price}")
        else:
            print(f"{code}: 获取失败")
    
    # 启动监控 (测试用)
    # monitor.start()
    # time.sleep(60)
    # monitor.stop()
