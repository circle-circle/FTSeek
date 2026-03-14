#!/usr/bin/env python3
"""
FTSeek 实时行情监控服务

功能:
- 通过 OpenD 实时订阅港股行情
- 实时运行策略生成买卖信号
- 推送到钉钉群 (带人工确认按钮)
- 人工确认后执行交易 (先期模拟模式)

使用方式:
    python3 scripts/realtime_monitor.py

系统服务:
    systemctl start ftseek-realtime
"""
import sys
import os
import logging
import time
import threading
from datetime import datetime
from typing import List, Dict, Optional

# 必须在导入 futu 之前设置日志
os.environ["FUTU_LOG_LEVEL"] = "ERROR"
os.environ["FUTU_LOG_TO_FILE"] = "0"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app.storage.database import Database
from app.notifier.dingtalk import DingTalkNotifier
from app.strategy.base import Signal, SignalAction
from app.signal_store import signal_store

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger("ftseek.realtime")

# 关注的港股列表
WATCHLIST = [
    'HK.00700',  # 腾讯控股
    'HK.09988',  # 阿里巴巴
    'HK.09992',  # 泡泡玛特
    'HK.02390',  # 智谱 AI
    'HK.03690',  # 美团
]

# 策略配置
STRATEGIES_CONFIG = {
    'ma_cross': {
        'enabled': True,
        'fast_period': 5,
        'slow_period': 20,
    },
    'rsi_rebound': {
        'enabled': True,
        'rsi_period': 14,
        'oversold_threshold': 30,
        'overbought_threshold': 70,
    },
    'macd': {
        'enabled': True,
        'fast_period': 12,
        'slow_period': 26,
        'signal_period': 9,
    },
}

# 推送冷却时间 (秒)
SIGNAL_COOLDOWN = 300  # 5 分钟内不重复推送同一股票

# 交易模式
TRADING_MODE = os.environ.get('FTSEEK_TRADING_MODE', 'paper')  # 'paper' | 'real'


class RealtimeMonitor:
    """实时行情监控器"""
    
    def __init__(self):
        self.db = Database()
        self.notifier = DingTalkNotifier(
            webhook_url=config.DINGTALK_WEBHOOK_URL,
            secret=config.DINGTALK_SECRET,
        )
        self._quote_ctx = None
        self._running = False
        self._cooldown: Dict[str, float] = {}
        self._cooldown_lock = threading.Lock()
        
        # 初始化策略
        self.strategies = {}
        self._init_strategies()
    
    def _init_strategies(self):
        """初始化策略实例"""
        try:
            if STRATEGIES_CONFIG.get('ma_cross', {}).get('enabled'):
                from app.strategy.ma_cross import MACrossStrategy
                self.strategies['ma_cross'] = MACrossStrategy(
                    STRATEGIES_CONFIG['ma_cross']
                )
                logger.info("✓ 均线交叉策略已加载")
            
            if STRATEGIES_CONFIG.get('rsi_rebound', {}).get('enabled'):
                from app.strategy.rsi_strategy import RSIStrategy
                self.strategies['rsi_rebound'] = RSIStrategy(
                    STRATEGIES_CONFIG['rsi_rebound']
                )
                logger.info("✓ RSI 反弹策略已加载")
            
            if STRATEGIES_CONFIG.get('macd', {}).get('enabled'):
                from app.strategy.macd_strategy import MACDStrategy
                self.strategies['macd'] = MACDStrategy(
                    STRATEGIES_CONFIG['macd']
                )
                logger.info("✓ MACD 策略已加载")
            
            logger.info(f"共加载 {len(self.strategies)} 个策略")
            
        except Exception as e:
            logger.error(f"初始化策略失败：{e}")
            raise
    
    def _get_quote_ctx(self):
        """获取行情上下文"""
        if self._quote_ctx is None:
            try:
                from futu import OpenQuoteContext
                self._quote_ctx = OpenQuoteContext(
                    host=config.OPEND_HOST,
                    port=config.OPEND_PORT
                )
                logger.info(f"行情上下文已创建 ({config.OPEND_HOST}:{config.OPEND_PORT})")
            except Exception as e:
                logger.error(f"创建行情上下文失败：{e}")
                raise
        return self._quote_ctx
    
    def _is_cooled_down(self, code: str) -> bool:
        """检查是否在冷却期"""
        now = time.time()
        with self._cooldown_lock:
            last_time = self._cooldown.get(code, 0)
            if now - last_time < SIGNAL_COOLDOWN:
                return False
            self._cooldown[code] = now
            return True
    
    def _run_strategies(self, code: str, kline: Dict) -> List[Signal]:
        """运行所有策略"""
        signals = []
        
        for name, strategy in self.strategies.items():
            try:
                signal = strategy.on_kline(code, kline, '1M')
                if signal:
                    signals.append(signal)
                    logger.info(f"[{code}] {name} 生成信号：{signal.action.value}")
            except Exception as e:
                logger.error(f"策略 {name} 运行失败：{e}")
        
        return signals
    
    def _save_signal(self, signal: Signal):
        """保存信号到数据库"""
        import hashlib
        
        signal_id = hashlib.md5(
            f"{signal.code}_{signal.action.value}_{signal.created_at}".encode()
        ).hexdigest()[:16]
        
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO signals 
                (id, code, name, action, price, quantity, strategy, reason, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_id,
                signal.code,
                signal.name,
                signal.action.value,
                signal.price,
                signal.quantity,
                signal.strategy,
                signal.reason,
                signal.created_at,
                'pending_confirmation',  # 待确认状态
            ))
    
    def _send_signal_for_confirmation(self, signal: Signal):
        """发送信号到钉钉请求人工确认 (ActionCard 带按钮)"""
        try:
            from app.strategy.base import SignalAction
            
            # 确定图标和按钮文字
            if signal.action == SignalAction.BUY:
                icon = "🟢"
                action_label = "确认买入"
                action_color = "#FF4444"
                action_cmd = "BUY"
            else:
                icon = "🔴"
                action_label = "确认卖出"
                action_color = "#44BB44"
                action_cmd = "SELL"
            
            # 构建 ActionCard 消息 (带点击按钮)
            # 注意：钉钉 ActionCard 的 btns 需要 HTTP/HTTPS 链接
            confirm_url = f"http://39.97.58.4:8899/confirm?signal_id={signal.id}&action={action_cmd}&mode={TRADING_MODE}"
            
            payload = {
                "msgtype": "actionCard",
                "actionCard": {
                    "title": f"{icon} 交易信号确认 - {signal.name}",
                    "text": f"""## {icon} 交易信号确认

**{signal.name} ({signal.code})**

---

**方向**: {action_label}
**策略**: {signal.strategy}
**价格**: {signal.price:.2f} HKD
**数量**: {signal.quantity} 股
**原因**: {signal.reason}

---

🕒 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
📊 模式：{'实盘' if TRADING_MODE == 'real' else '模拟'}

---

*请点击下方按钮确认交易*
""",
                    "btnOrientation": "0",  # 0: 竖直排列
                    "btns": [
                        {
                            "title": f"✅ {action_label}",
                            "actionURL": confirm_url
                        },
                        {
                            "title": "❌ 忽略",
                            "actionURL": f"http://39.97.58.4:8899/api/ignore?signal_id={signal.id}"
                        }
                    ]
                },
                "at": {"isAtAll": False},
            }
            
            # 发送消息
            ok = self.notifier._do_post(payload)
            if ok:
                logger.info(f"钉钉确认推送成功：{signal.code} {signal.action.value}")
                logger.info(f"确认链接：{confirm_url}")
            else:
                logger.error(f"钉钉推送失败：{signal.code}")
            
        except Exception as e:
            logger.error(f"发送确认消息失败：{e}")
    
    def _on_kline(self, code: str, kline: Dict):
        """K 线数据回调"""
        if not self._is_cooled_down(code):
            return
        
        # 运行策略
        signals = self._run_strategies(code, kline)
        
        # 处理信号
        for signal in signals:
            # 保存信号
            self._save_signal(signal)
            
            # 推送钉钉请求确认
            self._send_signal_for_confirmation(signal)
    
    def subscribe(self):
        """订阅行情"""
        from futu import SubType
        
        quote_ctx = self._get_quote_ctx()
        
        # 订阅 1 分钟 K 线
        logger.info(f"开始订阅 {len(WATCHLIST)} 只股票的 1 分钟 K 线")
        
        ret, data = quote_ctx.subscribe(
            code_list=WATCHLIST,
            subtype_list=[SubType.K_1M],
            subscribe_push=True,
        )
        
        if ret == 0:
            logger.info(f"✓ 成功订阅 {len(WATCHLIST)} 只股票")
        else:
            logger.error(f"✗ 订阅失败：{data}")
        
        # 设置回调
        from futu import StockQuoteHandlerBase
        
        class KlineHandler(StockQuoteHandlerBase):
            def __init__(self, monitor):
                self.monitor = monitor
            
            def on_recv_rsp(self, rsp_pb):
                from futu import StockQuoteHandlerBase
                ret_code, content = StockQuoteHandlerBase.on_recv_rsp(self, rsp_pb)
                if ret_code != 0:
                    return
                
                # 解析 K 线数据
                for code, klines in content.items():
                    for kline in klines:
                        self.monitor._on_kline(code, kline)
        
        quote_ctx.set_handler(KlineHandler(self))
        
        logger.info("行情订阅已启动，等待数据...")
        
        # 保持运行
        self._running = True
        while self._running:
            time.sleep(1)
    
    def start(self):
        """启动监控"""
        logger.info("=" * 60)
        logger.info("🎯 FTSeek 实时行情监控启动")
        logger.info("=" * 60)
        logger.info(f"关注股票：{len(WATCHLIST)} 只")
        logger.info(f"策略数量：{len(self.strategies)} 个")
        logger.info(f"交易模式：{'实盘' if TRADING_MODE == 'real' else '模拟'}")
        logger.info(f"推送冷却：{SIGNAL_COOLDOWN}秒")
        logger.info("=" * 60)
        
        try:
            self.subscribe()
        except KeyboardInterrupt:
            logger.info("收到停止信号，正在退出...")
        except Exception as e:
            logger.error(f"监控异常：{e}")
        finally:
            self.stop()
    
    def stop(self):
        """停止监控"""
        self._running = False
        if self._quote_ctx:
            self._quote_ctx.close()
        logger.info("实时行情监控已停止")


def main():
    """主函数"""
    monitor = RealtimeMonitor()
    monitor.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
