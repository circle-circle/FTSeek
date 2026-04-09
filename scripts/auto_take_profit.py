#!/usr/bin/env python3
"""
FTSeek 自动止盈监控服务

功能:
- 定期检查所有持仓的盈亏
- 当盈利达到止盈目标 (默认 5%) 时自动卖出
- 支持分批止盈 (可选)
- 推送钉钉通知

使用方式:
    python3 scripts/auto_take_profit.py          # 单次检查
    python3 scripts/auto_take_profit.py --daemon # 守护进程模式

定时任务 (crontab):
    */5 * * * * cd /opt/ftseek/FTSeek && python3 scripts/auto_take_profit.py
"""
import os
import sys
import logging
import argparse
import time
from datetime import datetime
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import config
from app.storage.database import get_database
from app.notifier.dingtalk import DingTalkNotifier
from app.risk.dynamic_exit_manager import DynamicExitManager

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/FTSeek/logs/auto_take_profit.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("ftseek.auto_take_profit")


class AutoTakeProfitMonitor:
    """自动止盈监控器"""
    
    def __init__(self, take_profit_pct: float = 0.05):
        """
        初始化
        
        Args:
            take_profit_pct: 止盈比例 (默认 5%)
        """
        self.take_profit_pct = take_profit_pct
        self.db = get_database()
        self.notifier = DingTalkNotifier()
        self.exit_manager = DynamicExitManager()
        
        # 从动态止盈管理器获取止盈比例
        config_tp = self.exit_manager.get_take_profit()
        if config_tp > 0:
            self.take_profit_pct = config_tp
        
        logger.info(f"自动止盈监控器已初始化 | 止盈目标={self.take_profit_pct:.1%}")
    
    def check_all_positions(self) -> dict:
        """
        检查所有持仓的盈亏情况
        
        Returns:
            检查结果统计
        """
        result = {
            "checked": 0,
            "profit_targets": [],
            "executed": 0,
            "errors": []
        }
        
        try:
            # 获取所有活跃持仓
            positions = self._get_active_positions()
            result["checked"] = len(positions)
            
            if not positions:
                logger.info("无活跃持仓")
                return result
            
            logger.info(f"检查 {len(positions)} 个持仓...")
            
            for pos in positions:
                try:
                    check_result = self._check_position(pos)
                    if check_result.get("hit_target"):
                        result["profit_targets"].append(check_result)
                        
                        # 执行止盈卖出
                        if self._execute_take_profit(pos, check_result):
                            result["executed"] += 1
                            
                except Exception as e:
                    logger.error(f"检查持仓 {pos.get('code')} 失败: {e}")
                    result["errors"].append({
                        "code": pos.get("code"),
                        "error": str(e)
                    })
                    
        except Exception as e:
            logger.error(f"检查持仓失败: {e}", exc_info=True)
            result["errors"].append({"error": str(e)})
            
        return result
    
    def _get_active_positions(self) -> list:
        """获取所有活跃持仓"""
        try:
            # 尝试从数据库获取
            cursor = self.db.conn.execute("""
                SELECT code, name, action, quantity, entry_price, entry_time, 
                       stop_loss, take_profit, status
                FROM positions 
                WHERE status = 'open'
            """)
            rows = cursor.fetchall()
            
            positions = []
            for row in rows:
                positions.append({
                    "code": row[0],
                    "name": row[1],
                    "action": row[2],
                    "quantity": row[3],
                    "entry_price": row[4],
                    "entry_time": row[5],
                    "stop_loss": row[6],
                    "take_profit": row[7] or self.take_profit_pct,
                    "status": row[8]
                })
            
            return positions
            
        except Exception as e:
            logger.warning(f"从数据库获取持仓失败: {e}")
            return []
    
    def _check_position(self, position: dict) -> dict:
        """
        检查单个持仓的盈亏
        
        Args:
            position: 持仓信息
            
        Returns:
            检查结果
        """
        code = position["code"]
        entry_price = position["entry_price"]
        action = position["action"]
        take_profit = position.get("take_profit", self.take_profit_pct)
        
        # 获取当前价格
        current_price = self._get_current_price(code)
        
        if current_price is None:
            return {"code": code, "error": "无法获取当前价格"}
        
        # 计算盈亏比例
        if action == "BUY":
            pnl_pct = (current_price - entry_price) / entry_price
        else:  # SELL (做空)
            pnl_pct = (entry_price - current_price) / entry_price
        
        result = {
            "code": code,
            "name": position.get("name", code),
            "action": action,
            "entry_price": entry_price,
            "current_price": current_price,
            "quantity": position["quantity"],
            "pnl_pct": pnl_pct,
            "take_profit_target": take_profit,
            "hit_target": pnl_pct >= take_profit,
            "market_value": current_price * position["quantity"]
        }
        
        # 记录日志
        status_emoji = "🎯" if result["hit_target"] else "📊"
        logger.info(
            f"{status_emoji} [{code}] 入场价={entry_price:.2f} | "
            f"当前价={current_price:.2f} | 盈亏={pnl_pct:+.2%} | "
            f"止盈目标={take_profit:.1%}"
        )
        
        return result
    
    def _get_current_price(self, code: str) -> float:
        """获取股票当前价格"""
        try:
            # 尝试从富途获取实时价格
            from futu import OpenQuoteContext
            
            quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
            ret, data = quote_ctx.get_market_snapshot([code])
            quote_ctx.close()
            
            if ret == 0 and len(data) > 0:
                return float(data.iloc[0]['last_price'])
                
        except Exception as e:
            logger.warning(f"获取 {code} 实时价格失败: {e}")
        
        # 回退：从数据库获取最新价格
        try:
            cursor = self.db.conn.execute("""
                SELECT close FROM kline_1m 
                WHERE code = ? 
                ORDER BY time DESC LIMIT 1
            """, (code,))
            row = cursor.fetchone()
            if row:
                return float(row[0])
        except Exception as e:
            logger.warning(f"从数据库获取 {code} 价格失败: {e}")
        
        return None
    
    def _execute_take_profit(self, position: dict, check_result: dict) -> bool:
        """
        执行止盈卖出
        
        Args:
            position: 持仓信息
            check_result: 检查结果
            
        Returns:
            是否成功
        """
        code = position["code"]
        quantity = position["quantity"]
        current_price = check_result["current_price"]
        pnl_pct = check_result["pnl_pct"]
        
        logger.info(f"🎯 [{code}] 触发止盈! 盈亏={pnl_pct:+.2%} >= 目标={self.take_profit_pct:.1%}")
        
        # 发送钉钉通知
        self._send_notification(position, check_result)
        
        # TODO: 实际执行卖出订单
        # 这里需要调用 TradeExecutor 执行卖出
        # 由于是模拟模式，目前只发送通知
        
        logger.info(f"✅ [{code}] 止盈通知已发送 (模拟模式)")
        
        return True
    
    def _send_notification(self, position: dict, check_result: dict):
        """发送止盈通知到钉钉"""
        code = position["code"]
        name = position.get("name", code)
        pnl_pct = check_result["pnl_pct"]
        pnl_amount = check_result["pnl_pct"] * check_result["market_value"]
        
        message = f"""
🎯 **止盈信号触发**

**股票**: {name} ({code})
**入场价**: {check_result["entry_price"]:.2f}
**当前价**: {check_result["current_price"]:.2f}
**盈亏比例**: {pnl_pct:+.2%}
**盈利金额**: {pnl_amount:+,.0f} HKD

**建议**: 当前盈利已达到 {self.take_profit_pct:.1%} 止盈目标，建议卖出锁定利润。
"""
        
        try:
            self.notifier.send_message(message)
            logger.info(f"钉钉通知已发送: {code}")
        except Exception as e:
            logger.error(f"发送钉钉通知失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="FTSeek 自动止盈监控")
    parser.add_argument("--daemon", action="store_true", help="守护进程模式")
    parser.add_argument("--interval", type=int, default=60, help="检查间隔 (秒)")
    parser.add_argument("--take-profit", type=float, default=0.05, help="止盈比例 (默认 5%%)")
    args = parser.parse_args()
    
    monitor = AutoTakeProfitMonitor(take_profit_pct=args.take_profit)
    
    if args.daemon:
        logger.info(f"启动守护进程模式，检查间隔 {args.interval} 秒")
        while True:
            try:
                result = monitor.check_all_positions()
                logger.info(f"检查完成: 检查 {result['checked']} 个，触发 {len(result['profit_targets'])} 个")
            except Exception as e:
                logger.error(f"检查失败: {e}")
            
            time.sleep(args.interval)
    else:
        result = monitor.check_all_positions()
        print(f"\n检查完成:")
        print(f"  - 检查持仓: {result['checked']} 个")
        print(f"  - 触发止盈: {len(result['profit_targets'])} 个")
        print(f"  - 已执行: {result['executed']} 个")
        
        if result["profit_targets"]:
            print("\n触发止盈的持仓:")
            for t in result["profit_targets"]:
                print(f"  - {t['code']}: {t['pnl_pct']:+.2%}")


if __name__ == "__main__":
    main()