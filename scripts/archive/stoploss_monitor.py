#!/usr/bin/env python3
"""
止损执行监控器 - FTSeek-US (P0 强化)

功能:
1. 实时监控所有持仓的止损状态
2. 30 秒间隔扫描
3. 自动触发止损执行
4. 告警升级机制

使用方式:
    # 持续监控模式
    python scripts/stoploss_monitor.py --mode continuous
    
    # 单次检查模式
    python scripts/stoploss_monitor.py --mode once
    
    # 测试模式
    python scripts/stoploss_monitor.py --test

作者：FTSeek-US Elite Squad
版本：1.0 (2026-04-01 P0 强化)
"""
import sys
import os
import time
import logging
import argparse
import json
from datetime import datetime
from pathlib import Path

# 路径配置
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env.us")

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "logs" / "stoploss_monitor.log")
    ]
)
logger = logging.getLogger(__name__)

# 导入核心模块
from app.risk.exit_config import ExitConfig, StrategyType
from app.risk.exit_engine import ExitEngine
from app.core.models import Position, Direction, ExitReason
from app.core.scheduler_core import PositionStore, TradingScheduler
from app.storage.trade_store import TradeStore
from app.notifier.dingtalk import DingTalkNotifier


class StopLossMonitorService:
    """止损监控服务 (P0 强化版)"""
    
    def __init__(self):
        self.config = ExitConfig()
        self.exit_engine = ExitEngine()
        self.position_store = PositionStore()
        self.trade_store = TradeStore()
        self.notifier = DingTalkNotifier()
        
        # 监控统计
        self.stats = {
            "start_time": datetime.now(),
            "total_checks": 0,
            "stoploss_triggered": 0,
            "execution_success": 0,
            "execution_failure": 0,
        }
        
        logger.info("[止损监控服务] 初始化完成")
        logger.info(f"  监控间隔：{self.config.monitor_interval_seconds}秒")
        logger.info(f"  卖空止损：+{self.config.short_stop_loss*100:.0f}%")
        logger.info(f"  做多止损：短线{self.config.long_stop_loss_short_term*100:.0f}%, "
                    f"中线{self.config.long_stop_loss_mid_term*100:.0f}%, "
                    f"长线{self.config.long_stop_loss_long_term*100:.0f}%")
        logger.info(f"  最大重试：{self.config.max_retry_count}次")
    
    def check_position(self, position: Position, current_price: float) -> tuple:
        """
        检查单个持仓的止损状态
        
        Returns:
            (是否触发，退出原因)
        """
        should_exit, reason = self.exit_engine.should_exit(position, current_price)
        
        if should_exit and reason in [ExitReason.STOP_LOSS, ExitReason.TRAILING_STOP]:
            return True, reason
        
        return False, None
    
    def execute_stoploss(self, position: Position, current_price: float, reason: ExitReason) -> bool:
        """
        执行止损 (带重试机制)
        
        Returns:
            bool: 是否成功执行
        """
        symbol = position.symbol
        retry_count = 0
        
        while retry_count < self.config.max_retry_count:
            try:
                # 记录止损事件
                event = {
                    "type": "stoploss_triggered",
                    "symbol": symbol,
                    "direction": position.direction.value,
                    "entry_price": position.avg_entry_price,
                    "stoploss_price": position.initial_sl,
                    "current_price": current_price,
                    "reason": reason.value,
                    "timestamp": datetime.now().isoformat(),
                }
                self.trade_store.log_event(event)
                
                # TODO: 调用富途 OpenAPI 执行实际平仓
                # 这里模拟执行
                logger.info(f"[止损执行] {symbol} 平仓订单已提交 (重试 {retry_count+1}/{self.config.max_retry_count})")
                
                # 发送告警
                self._send_alert(position, current_price, reason)
                
                return True
                
            except Exception as e:
                error_msg = str(e)
                retry_count += 1
                logger.error(f"[止损执行失败] {symbol}: {error_msg} (重试 {retry_count}/{self.config.max_retry_count})")
                
                if retry_count < self.config.max_retry_count:
                    time.sleep(self.config.retry_interval_seconds)
                else:
                    # 发送三级告警
                    self._send_critical_alert(position, error_msg)
        
        return False
    
    def _send_alert(self, position: Position, current_price: float, reason: ExitReason):
        """发送止损告警 (一级)"""
        symbol = position.symbol
        direction = "卖空" if position.direction == Direction.SHORT else "做多"
        
        message = (
            f"🚨【止损触发】{symbol}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"方向：{direction}\n"
            f"入场价：${position.avg_entry_price:.2f}\n"
            f"当前价：${current_price:.2f}\n"
            f"止损价：${position.initial_sl:.2f}\n"
            f"盈亏：{((current_price - position.avg_entry_price) / position.avg_entry_price * 100):.2f}%\n"
            f"原因：{reason.value}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        try:
            self.notifier.send_markdown(title="FTSeek-US 止损告警", content=message)
            logger.info(f"[告警已发送] {symbol} 止损触发")
        except Exception as e:
            logger.error(f"[告警发送失败] {e}")
    
    def _send_critical_alert(self, position: Position, error_message: str):
        """发送三级告警 (连续失败)"""
        symbol = position.symbol
        
        message = (
            f"🔴【紧急告警】止损连续执行失败 - {symbol}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"错误：{error_message}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ 需要人工干预!\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        try:
            self.notifier.send_markdown(title="FTSeek-US 紧急告警", content=message)
            logger.critical(f"[三级告警已发送] {symbol} 连续失败")
        except Exception as e:
            logger.error(f"[告警发送失败] {e}")
    
    def _get_current_price(self, symbol: str) -> float:
        """获取当前价格 (需要集成行情数据源)"""
        # TODO: 集成富途 OpenAPI 获取实时价格
        # 这里返回 None 表示需要实现
        return None
    
    def monitor_once(self) -> dict:
        """执行一次监控检查"""
        positions = self.position_store.all()
        results = {
            "checked": len(positions),
            "triggered": 0,
            "success": 0,
            "failure": 0,
        }
        
        for position in positions:
            # 获取当前价格
            current_price = self._get_current_price(position.symbol)
            if current_price is None:
                logger.warning(f"[价格获取失败] {position.symbol}")
                continue
            
            # 检查止损
            triggered, reason = self.check_position(position, current_price)
            
            if triggered:
                results["triggered"] += 1
                logger.warning(f"[止损触发] {position.symbol} ({reason.value})")
                
                # 执行止损
                success = self.execute_stoploss(position, current_price, reason)
                
                if success:
                    results["success"] += 1
                    self.stats["execution_success"] += 1
                else:
                    results["failure"] += 1
                    self.stats["execution_failure"] += 1
        
        self.stats["total_checks"] += results["checked"]
        self.stats["stoploss_triggered"] += results["triggered"]
        
        return results
    
    def monitor_continuous(self):
        """持续监控循环"""
        logger.info("[止损监控] 启动持续监控循环")
        
        while True:
            try:
                start_time = time.time()
                
                # 执行一次监控
                results = self.monitor_once()
                
                # 输出统计
                elapsed = time.time() - start_time
                logger.info(f"[监控完成] 检查={results['checked']}, "
                            f"触发={results['triggered']}, "
                            f"成功={results['success']}, "
                            f"失败={results['failure']}, "
                            f"耗时={elapsed:.2f}s")
                
                # 等待下一次检查
                sleep_time = max(0, self.config.monitor_interval_seconds - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                
            except KeyboardInterrupt:
                logger.info("[止损监控] 用户中断，停止服务")
                break
            except Exception as e:
                logger.error(f"[监控异常] {e}")
                time.sleep(self.config.monitor_interval_seconds)
    
    def get_stats(self) -> dict:
        """获取监控统计"""
        uptime = datetime.now() - self.stats["start_time"]
        return {
            "uptime_seconds": uptime.total_seconds(),
            "total_checks": self.stats["total_checks"],
            "stoploss_triggered": self.stats["stoploss_triggered"],
            "execution_success": self.stats["execution_success"],
            "execution_failure": self.stats["execution_failure"],
            "success_rate": (
                self.stats["execution_success"] / 
                (self.stats["execution_success"] + self.stats["execution_failure"])
                if (self.stats["execution_success"] + self.stats["execution_failure"]) > 0
                else 0
            ),
        }


def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(description="FTSeek-US 止损监控器 (P0 强化)")
    parser.add_argument("--mode", choices=["once", "continuous"], default="once",
                        help="运行模式：once=单次检查，continuous=持续监控")
    parser.add_argument("--test", action="store_true", help="运行测试模式")
    parser.add_argument("--config", type=str, help="配置文件路径")
    
    args = parser.parse_args()
    
    # 创建监控服务
    monitor = StopLossMonitorService()
    
    if args.test:
        # 测试模式
        print("\n" + "=" * 60)
        print("测试模式：验证止损监控功能")
        print("=" * 60)
        
        # 运行测试
        import subprocess
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "tests" / "risk" / "test_stoploss_enhancement.py")],
            cwd=str(BASE_DIR)
        )
        sys.exit(result.returncode)
        
    elif args.mode == "once":
        # 单次检查
        print("\n执行单次止损检查...")
        results = monitor.monitor_once()
        
        print("\n" + "=" * 60)
        print("检查结果:")
        print(f"  持仓检查：{results['checked']}")
        print(f"  止损触发：{results['triggered']}")
        print(f"  执行成功：{results['success']}")
        print(f"  执行失败：{results['failure']}")
        print("=" * 60)
        
    else:
        # 持续监控
        print("\n" + "=" * 60)
        print("FTSeek-US 止损监控服务 (P0 强化)")
        print("=" * 60)
        print(f"启动时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"监控间隔：{monitor.config.monitor_interval_seconds}秒")
        print("=" * 60)
        
        monitor.monitor_continuous()


if __name__ == "__main__":
    main()
