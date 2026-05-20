"""
美股价格监控器 (增强版 - 集成富途 OpenAPI)

功能:
- 定期通过富途 API 查询美股股价
- 同步富途实际持仓到本地
- 检查持仓的止损止盈条件
- 触发条件时自动平仓 + 钉钉推送
- 支持中长线投资 (低频检查，降低 API 调用)

更新日志:
- 2026-03-26: 集成富途 OpenAPI，支持自动下单
"""
import os
import time
import logging
import threading
import subprocess
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import json
import yaml

logger = logging.getLogger("ftseek.us_monitor")

# 导入富途 SDK
try:
    from futu import OpenQuoteContext, RET_OK
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    logger.warning("富途 SDK 未安装，美股监控将使用模拟模式")

# 导入本地模块
from app.futu_integration import get_futu_integration, FutuIntegration

# 导入钉钉通知
try:
    from app.notifier.dingtalk import DingTalkNotifier
except ImportError:
    DingTalkNotifier = None


@dataclass
class MonitorConfig:
    """监控配置"""
    # 检查频率 (秒)
    check_interval: int = 300          # 默认 5 分钟检查一次
    night_interval: int = 900          # 夜间 15 分钟检查一次 (美东时段外)
    
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
    
    # 富途配置
    futu_host: str = "127.0.0.1"
    futu_port: int = 11111
    futu_trd_env: str = "SIMULATE"
    futu_acc_id: Optional[int] = None
    
    # 自动交易
    auto_trade_enabled: bool = True    # 启用自动下单


class Position:
    """持仓类 (增强版)"""
    
    def __init__(self, code: str, qty: float, entry_price: float,
                 current_price: float = 0, stop_loss: float = None,
                 take_profit: float = None, entry_time: str = None):
        self.code = code
        self.qty = qty
        self.entry_price = entry_price
        self.current_price = current_price
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.entry_time = entry_time or datetime.now().isoformat()
        self.unrealized_pl = 0
        self.pl_ratio = 0
        self.peak_price = current_price  # 用于追踪止盈
        self.updated_at = datetime.now().isoformat()
    
    def update_price(self, price: float):
        """更新当前价格并计算盈亏"""
        self.current_price = price
        self.unrealized_pl = (price - self.entry_price) * self.qty
        self.pl_ratio = (price - self.entry_price) / self.entry_price if self.entry_price > 0 else 0
        
        # 更新最高价 (用于追踪止盈)
        if price > self.peak_price:
            self.peak_price = price
        
        self.updated_at = datetime.now().isoformat()
    
    def check_stop_loss(self) -> bool:
        """检查是否触发止损"""
        if self.stop_loss is None:
            return False
        return self.pl_ratio <= self.stop_loss
    
    def check_take_profit(self) -> bool:
        """检查是否触发止盈"""
        if self.take_profit is None:
            return False
        return self.pl_ratio >= self.take_profit
    
    def check_trailing_stop(self, trigger: float = 0.10, percent: float = 0.05) -> bool:
        """检查追踪止盈"""
        if self.peak_price <= 0:
            return False
        
        # 计算从最高点回撤的比例
        peak_pl_ratio = (self.peak_price - self.entry_price) / self.entry_price
        
        # 如果曾经盈利超过 trigger，且当前回撤超过 percent
        if peak_pl_ratio >= trigger:
            current_peak_ratio = (self.peak_price - self.current_price) / self.peak_price
            if current_peak_ratio >= percent:
                return True
        
        return False
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'code': self.code,
            'qty': self.qty,
            'entry_price': self.entry_price,
            'current_price': self.current_price,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'entry_time': self.entry_time,
            'unrealized_pl': self.unrealized_pl,
            'pl_ratio': self.pl_ratio,
            'peak_price': self.peak_price,
            'updated_at': self.updated_at
        }


class USStockMonitor:
    """美股价格监控器 (增强版)"""
    
    def __init__(self, config: MonitorConfig = None):
        self.config = config or MonitorConfig()
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # 组件
        self.futu: Optional[FutuIntegration] = None
        self.dingtalk = None
        
        # 持仓管理
        self.positions: Dict[str, Position] = {}
        from app.core.paths import get_us_stock_config_path, get_watchlist_path, get_trade_records_json
        self.config_file = str(get_us_stock_config_path())
        self.watchlist_file = str(get_watchlist_path())
        self.records_file = str(get_trade_records_json())
        
        # 统计
        self.check_count = 0
        self.trigger_count = 0
        self.last_check_time = None
        self.last_sync_time = None
        
        # 加载配置
        self._load_config()
    
    def _load_config(self):
        """加载配置文件"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config_data = yaml.safe_load(f)
                
                # 更新默认止损止盈
                if 'defaults' in config_data:
                    defaults = config_data['defaults']
                    self.config.default_stop_loss = defaults.get('stop_loss', self.config.default_stop_loss)
                    self.config.default_take_profit = defaults.get('take_profit', self.config.default_take_profit)
                
                logger.info(f"配置加载成功：止损={self.config.default_stop_loss:.1%}, 止盈={self.config.default_take_profit:.1%}")
        except Exception as e:
            logger.error(f"加载配置失败：{e}")
    
    def _load_stock_config(self) -> Dict[str, Dict]:
        """加载个股配置 (止损止盈参数)"""
        stock_config = {}
        
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config_data = yaml.safe_load(f)
                
                if 'individual_stocks' in config_data:
                    for code, cfg in config_data['individual_stocks'].items():
                        stock_config[code] = {
                            'stop_loss': cfg.get('stop_loss'),
                            'take_profit': cfg.get('take_profit'),
                            'enabled': cfg.get('enabled', True),
                            'name': cfg.get('name', ''),
                            'notes': cfg.get('notes', '')
                        }
        except Exception as e:
            logger.error(f"加载个股配置失败：{e}")
        
        return stock_config
    
    def initialize(self):
        """初始化组件"""
        logger.info("=" * 60)
        logger.info("FTSeek-US 美股监控系统启动 (增强版)")
        logger.info("=" * 60)
        
        # 初始化富途集成
        self.futu = get_futu_integration(
            host=self.config.futu_host,
            port=self.config.futu_port,
            trd_env=self.config.futu_trd_env,
            acc_id=self.config.futu_acc_id
        )
        
        if self.futu.connect():
            logger.info("富途 API 连接成功")
            
            # 获取账户列表
            accounts = self.futu.get_acc_list()
            if accounts:
                logger.info(f"找到 {len(accounts)} 个账户")
                for acc in accounts:
                    logger.info(f"  - 账户 {acc['acc_id']} ({acc['trd_env']})")
                
                # 自动选择第一个模拟账户
                if not self.config.futu_acc_id:
                    for acc in accounts:
                        if acc['trd_env'] == 'SIMULATE':
                            self.config.futu_acc_id = acc['acc_id']
                            self.futu.acc_id = acc['acc_id']
                            logger.info(f"自动选择模拟账户：{acc['acc_id']}")
                            break
        else:
            logger.warning("富途 API 连接失败，将使用本地模式")
        
        # 初始化备用数据源 (已禁用)
        # self.market_backup = get_market_backup()
        # backup_status = self.market_backup.health_check()
        # if backup_status['healthy']:
        #     logger.info(f"备用数据源已初始化：{list(backup_status['sources'].keys())}")
        # else:
        #     logger.warning("备用数据源未配置")
        self.market_backup = None
        logger.info("备用数据源：未启用")
        
        # 初始化钉钉通知
        if self.config.dingtalk_enabled and DingTalkNotifier:
            try:
                from config import DINGTALK_WEBHOOK, DINGTALK_SECRET
                if DINGTALK_WEBHOOK:
                    self.dingtalk = DingTalkNotifier(
                        webhook=DINGTALK_WEBHOOK,
                        secret=DINGTALK_SECRET
                    )
                    logger.info("钉钉通知已初始化")
            except Exception as e:
                logger.warning(f"钉钉通知初始化失败：{e}")
        
        # 同步持仓
        self._sync_positions()
        
        logger.info("=" * 60)
    
    def _sync_positions(self):
        """同步富途持仓到本地"""
        if not self.futu or not self.config.futu_acc_id:
            logger.warning("无法同步持仓：富途未连接或无账户 ID")
            return
        
        try:
            # 同步到本地文件
            if self.futu.sync_positions_to_local(self.records_file, self.config.futu_acc_id):
                self.last_sync_time = datetime.now()
                logger.info("持仓同步成功")
            
            # 加载到内存
            self._load_positions_from_file()
        except Exception as e:
            logger.error(f"同步持仓失败：{e}")
    
    def _load_positions_from_file(self):
        """从文件加载持仓"""
        if not os.path.exists(self.records_file):
            logger.warning(f"持仓文件不存在：{self.records_file}")
            return
        
        try:
            with open(self.records_file, 'r', encoding='utf-8') as f:
                records = json.load(f)
            
            # 加载未平仓的持仓
            stock_config = self._load_stock_config()
            
            for record in records:
                if record.get('exit_price') is None and record.get('qty', 0) > 0:
                    code = record['code']
                    
                    # 获取配置中的止损止盈
                    cfg = stock_config.get(code, {})
                    stop_loss = record.get('stop_loss') or cfg.get('stop_loss')
                    take_profit = record.get('take_profit') or cfg.get('take_profit')
                    
                    # 创建持仓对象
                    position = Position(
                        code=code,
                        qty=record['qty'],
                        entry_price=record.get('entry_price', 0),
                        current_price=record.get('current_price', 0),
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        entry_time=record.get('entry_time')
                    )
                    
                    self.positions[code] = position
                    logger.info(f"加载持仓：{code} {position.qty}股 @ ${position.entry_price}")
            
            logger.info(f"共加载 {len(self.positions)} 个持仓")
        except Exception as e:
            logger.error(f"加载持仓失败：{e}")
    
    def get_stock_price(self, code: str) -> Optional[float]:
        """获取股票价格 (带备用数据源)"""
        # 1. 优先使用富途
        if self.futu:
            price = self.futu.get_stock_price(code)
            if price and price > 0:
                return price
        
        # 2. 使用备用数据源
        if self.market_backup:
            price = self.market_backup.get_price(code)
            if price and price > 0:
                logger.info(f"[备用数据源] {code}: ${price}")
                return price
        
        logger.warning(f"无法获取股价：{code}")
        return None
    
    def check_positions(self) -> List[Tuple[str, str, float]]:
        """
        检查持仓的止损止盈条件
        
        Returns:
            [(code, reason, current_price), ...]
        """
        triggers = []
        stock_config = self._load_stock_config()
        
        for code, position in self.positions.items():
            # 获取当前价格
            price = self.get_stock_price(code)
            if not price:
                logger.warning(f"获取股价失败：{code}")
                continue
            
            # 更新持仓价格
            position.update_price(price)
            
            # 检查止损
            if position.check_stop_loss():
                triggers.append((code, "stop_loss", price))
                logger.warning(f"⚠️ {code} 触发止损！现价${price:.2f}, 盈亏{position.pl_ratio:.1%}")
                continue
            
            # 检查止盈
            if position.check_take_profit():
                triggers.append((code, "take_profit", price))
                logger.info(f"✅ {code} 触发止盈！现价${price:.2f}, 盈亏{position.pl_ratio:.1%}")
                continue
            
            # 检查追踪止盈
            if self.config.trailing_stop_enabled and position.check_trailing_stop(
                self.config.trailing_trigger,
                self.config.trailing_percent
            ):
                triggers.append((code, "trailing_stop", price))
                logger.info(f"📉 {code} 触发追踪止盈！现价${price:.2f}, 从高点回撤")
        
        return triggers
    
    def execute_exit(self, code: str, reason: str, current_price: float) -> bool:
        """
        执行平仓
        
        Args:
            code: 股票代码
            reason: 平仓原因 (stop_loss/take_profit/trailing_stop)
            current_price: 当前价格
        
        Returns:
            是否成功
        """
        if not self.config.auto_trade_enabled:
            logger.info(f"自动交易已禁用，跳过平仓：{code}")
            return False
        
        if not self.futu:
            logger.error("富途 API 未连接，无法执行平仓")
            return False
        
        position = self.positions.get(code)
        if not position:
            logger.error(f"找不到持仓：{code}")
            return False
        
        # 执行卖出
        success, message = self.futu.place_sell_order(
            code=code,
            qty=position.qty,
            price=current_price,
            reason=reason
        )
        
        if success:
            # 更新本地记录
            position.exit_price = current_price
            position.exit_time = datetime.now().isoformat()
            position.exit_reason = reason
            
            # 从内存中移除
            del self.positions[code]
            
            # 保存到文件
            self._save_positions()
            
            # 发送通知
            self._send_exit_notification(code, position, reason, message)
            
            self.trigger_count += 1
            logger.info(f"平仓执行成功：{code} {reason}")
            return True
        else:
            logger.error(f"平仓执行失败：{code} {message}")
            self._send_alert(f"❌ 平仓失败：{code}", message)
            return False
    
    def _save_positions(self):
        """保存持仓到文件"""
        try:
            records = []
            if os.path.exists(self.records_file):
                with open(self.records_file, 'r', encoding='utf-8') as f:
                    records = json.load(f)
            
            # 更新已平仓的记录
            for code, position in self.positions.items():
                for record in records:
                    if record.get('code') == code and record.get('exit_price') is None:
                        record['exit_price'] = position.exit_price
                        record['exit_time'] = position.exit_time
                        record['exit_reason'] = position.exit_reason
                        break
            
            with open(self.records_file, 'w', encoding='utf-8') as f:
                json.dump(records, f, indent=2, ensure_ascii=False)
            
            logger.info("持仓记录已保存")
        except Exception as e:
            logger.error(f"保存持仓失败：{e}")
    
    def _send_exit_notification(self, code: str, position: Position, 
                                 reason: str, order_message: str):
        """发送平仓通知"""
        if not self.dingtalk or not self.config.push_on_trigger:
            return
        
        reason_map = {
            'stop_loss': '🛑 止损',
            'take_profit': '✅ 止盈',
            'trailing_stop': '📉 追踪止盈'
        }
        
        emoji = reason_map.get(reason, '⚠️')
        pnl = position.pl_ratio * 100
        pnl_amount = position.unrealized_pl
        
        title = f"{emoji} {code} {reason_map.get(reason, '')}"
        content = (
            f"**代码**: {code}\n"
            f"**原因**: {reason_map.get(reason, reason)}\n"
            f"**入场**: ${position.entry_price:.2f}\n"
            f"**出场**: ${position.exit_price:.2f}\n"
            f"**数量**: {position.qty}股\n"
            f"**盈亏**: {pnl:+.2f}% (${pnl_amount:+.2f})\n"
            f"**订单**: {order_message}\n"
            f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        self.dingtalk.send(title, content)
        logger.info(f"钉钉通知已发送：{code}")
    
    def _send_alert(self, title: str, content: str):
        """发送告警"""
        if self.dingtalk:
            self.dingtalk.send(title, content)
    
    def start(self):
        """启动监控"""
        if self.running:
            logger.warning("FTSeek-US 已在运行中")
            return
        
        self.initialize()
        
        self.running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        
        logger.info("FTSeek-US 已启动")
        from app.core.paths import get_logs_dir
        logger.info("日志文件：" + str(get_logs_dir() / "ftseek_us.log"))
    
    def stop(self):
        """停止监控"""
        if not self.running:
            return
        
        logger.info("停止 FTSeek-US...")
        
        self.running = False
        self._stop_event.set()
        
        if self._thread:
            self._thread.join(timeout=5)
        
        if self.futu:
            self.futu.disconnect()
        
        logger.info("FTSeek-US 已停止")
        logger.info("=" * 60)
    
    def _monitor_loop(self):
        """监控主循环"""
        while self.running and not self._stop_event.is_set():
            try:
                # 检查是否是美东交易时段
                is_trading_hours = self._is_us_trading_hours()
                interval = self.config.check_interval if is_trading_hours else self.config.night_interval
                
                # 检查持仓
                self.check_count += 1
                self.last_check_time = datetime.now()
                
                logger.info(f"[检查 #{self.check_count}] 持仓检查开始...")
                
                triggers = self.check_positions()
                
                if triggers:
                    logger.warning(f"发现 {len(triggers)} 个触发条件")
                    for code, reason, price in triggers:
                        self.execute_exit(code, reason, price)
                else:
                    logger.info("无触发条件")
                
                # 每日汇总推送 (美东收盘后)
                if self.config.push_daily_summary and self._should_send_daily_summary():
                    self._send_daily_summary()
                
                # 等待下一次检查
                logger.info(f"下次检查将在 {interval} 秒后")
                self._stop_event.wait(interval)
                
            except Exception as e:
                logger.error(f"监控循环异常：{e}")
                self._stop_event.wait(60)  # 异常时等待 1 分钟
    
    def _is_us_trading_hours(self) -> bool:
        """判断是否是美东交易时段 (北京时间 21:30-次日 04:00)"""
        now = datetime.now()
        hour = now.hour
        minute = now.minute
        
        # 北京时间 21:30-24:00
        if hour >= 21 and (hour > 21 or minute >= 30):
            return True
        
        # 北京时间 00:00-04:00
        if hour >= 0 and hour < 4:
            return True
        
        return False
    
    def _should_send_daily_summary(self) -> bool:
        """判断是否应该发送每日汇总 (美东收盘后)"""
        # 简化实现：每天北京时间 05:00 发送
        now = datetime.now()
        return now.hour == 5 and now.minute < 10 and self.last_check_time
    
    def _send_daily_summary(self):
        """发送每日汇总"""
        if not self.dingtalk:
            return
        
        # 防止重复发送
        if not hasattr(self, '_last_summary_date'):
            self._last_summary_date = None
        
        today = datetime.now().date()
        if self._last_summary_date == today:
            return
        
        self._last_summary_date = today
        
        title = "📊 FTSeek-US 每日汇总"
        content = (
            f"**日期**: {today}\n"
            f"**检查次数**: {self.check_count}\n"
            f"**触发次数**: {self.trigger_count}\n"
            f"**当前持仓**: {len(self.positions)} 只\n"
            f"**最后同步**: {self.last_sync_time.strftime('%Y-%m-%d %H:%M:%S') if self.last_sync_time else 'N/A'}"
        )
        
        self.dingtalk.send(title, content)
        logger.info("每日汇总已发送")
    
    def get_status(self) -> Dict:
        """获取监控状态"""
        return {
            'running': self.running,
            'check_count': self.check_count,
            'trigger_count': self.trigger_count,
            'last_check_time': self.last_check_time.isoformat() if self.last_check_time else None,
            'last_sync_time': self.last_sync_time.isoformat() if self.last_sync_time else None,
            'positions_count': len(self.positions),
            'futu_connected': self.futu is not None,
            'auto_trade_enabled': self.config.auto_trade_enabled
        }


# 单例
_monitor = None

def get_us_stock_monitor(config: MonitorConfig = None) -> USStockMonitor:
    """获取监控器单例"""
    global _monitor
    if _monitor is None:
        _monitor = USStockMonitor(config)
    return _monitor
