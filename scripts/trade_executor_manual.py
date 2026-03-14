#!/usr/bin/env python3
"""
FTSeek 交易执行器 - 人工确认模式

功能:
- 接收钉钉确认回调
- 执行买入/卖出操作
- 支持模拟和实盘模式
- 记录执行结果

使用方式:
    python3 scripts/trade_executor_manual.py --signal_id xxx --action BUY
"""
import sys
import os
import logging
import argparse
from datetime import datetime
from typing import Optional, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app.storage.database import Database
from app.notifier.dingtalk import DingTalkNotifier
from app.performance.tracker import PerformanceTracker

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger("ftseek.executor")

# 交易模式
TRADING_MODE = os.environ.get('FTSEEK_TRADING_MODE', 'paper')  # 'paper' | 'real'


class ManualTradeExecutor:
    """人工确认模式交易执行器"""
    
    def __init__(self):
        self.db = Database()
        self.notifier = DingTalkNotifier(
            webhook_url=config.DINGTALK_WEBHOOK_URL,
            secret=config.DINGTALK_SECRET,
        )
        self.tracker = PerformanceTracker()
        self._trade_ctx = None
    
    def _get_trade_ctx(self):
        """获取交易上下文"""
        if self._trade_ctx is None:
            try:
                from futu import OpenSecTradeContext, TrdEnv
                
                # 根据模式选择交易环境
                trd_env = TrdEnv.REAL if TRADING_MODE == 'real' else TrdEnv.SIMULATE
                
                self._trade_ctx = OpenSecTradeContext(
                    host=config.FUTU_HOST,
                    port=config.FUTU_PORT,
                    trd_env=trd_env,
                )
                logger.info(f"交易上下文已创建 (模式={TRADING_MODE})")
            except Exception as e:
                logger.error(f"创建交易上下文失败：{e}")
                raise
        return self._trade_ctx
    
    def get_signal(self, signal_id: str) -> Optional[Dict]:
        """从数据库获取信号"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, code, name, action, price, quantity, strategy, reason, status
                FROM signals
                WHERE id = ?
            """, (signal_id,))
            
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    
    def update_signal_status(self, signal_id: str, status: str, executed_at: str = None):
        """更新信号状态"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if executed_at:
                cursor.execute("""
                    UPDATE signals
                    SET status = ?, executed_at = ?
                    WHERE id = ?
                """, (status, executed_at, signal_id))
            else:
                cursor.execute("""
                    UPDATE signals
                    SET status = ?
                    WHERE id = ?
                """, (status, signal_id))
    
    def execute_buy(self, signal: Dict) -> Dict:
        """
        执行买入
        
        Returns:
            执行结果 {success: bool, order_id: str, message: str}
        """
        code = signal['code']
        quantity = signal.get('quantity', 100)
        price = signal['price']
        
        logger.info(f"执行买入：{code} x {quantity} @ {price}")
        
        if TRADING_MODE == 'paper':
            # 模拟模式
            return {
                'success': True,
                'order_id': f"PAPER_{signal['id']}_{int(datetime.now().timestamp())}",
                'message': f"模拟买入 {code} x {quantity} @ {price:.2f} HKD",
                'mode': 'paper',
            }
        
        # 实盘模式
        try:
            from futu import TrdSide, OrderType, TrdEnv
            
            trade_ctx = self._get_trade_ctx()
            
            # 解锁交易账户
            trade_ctx.unlock_trade(password=config.FUTU_PASSWORD)
            
            # 下单
            ret, data, page_req_key = trade_ctx.place_order(
                price=price,
                qty=quantity,
                code=code,
                trd_side=TrdSide.BUY,
                order_type=OrderType.NORMAL,
            )
            
            if ret == 0:
                order_id = data['order_id'][0]
                logger.info(f"买入订单已提交：{order_id}")
                return {
                    'success': True,
                    'order_id': str(order_id),
                    'message': f"实盘买入 {code} x {quantity} @ {price:.2f} HKD",
                    'mode': 'real',
                }
            else:
                logger.error(f"买入下单失败：{data}")
                return {
                    'success': False,
                    'order_id': None,
                    'message': f"下单失败：{data}",
                    'mode': 'real',
                }
                
        except Exception as e:
            logger.error(f"买入执行异常：{e}")
            return {
                'success': False,
                'order_id': None,
                'message': f"执行异常：{e}",
                'mode': 'real',
            }
    
    def execute_sell(self, signal: Dict) -> Dict:
        """
        执行卖出
        
        Returns:
            执行结果 {success: bool, order_id: str, message: str}
        """
        code = signal['code']
        quantity = signal.get('quantity', 100)
        price = signal['price']
        
        logger.info(f"执行卖出：{code} x {quantity} @ {price}")
        
        if TRADING_MODE == 'paper':
            # 模拟模式
            return {
                'success': True,
                'order_id': f"PAPER_{signal['id']}_{int(datetime.now().timestamp())}",
                'message': f"模拟卖出 {code} x {quantity} @ {price:.2f} HKD",
                'mode': 'paper',
            }
        
        # 实盘模式
        try:
            from futu import TrdSide, OrderType
            
            trade_ctx = self._get_trade_ctx()
            trade_ctx.unlock_trade(password=config.FUTU_PASSWORD)
            
            ret, data, page_req_key = trade_ctx.place_order(
                price=price,
                qty=quantity,
                code=code,
                trd_side=TrdSide.SELL,
                order_type=OrderType.NORMAL,
            )
            
            if ret == 0:
                order_id = data['order_id'][0]
                logger.info(f"卖出订单已提交：{order_id}")
                return {
                    'success': True,
                    'order_id': str(order_id),
                    'message': f"实盘卖出 {code} x {quantity} @ {price:.2f} HKD",
                    'mode': 'real',
                }
            else:
                logger.error(f"卖出下单失败：{data}")
                return {
                    'success': False,
                    'order_id': None,
                    'message': f"下单失败：{data}",
                    'mode': 'real',
                }
                
        except Exception as e:
            logger.error(f"卖出执行异常：{e}")
            return {
                'success': False,
                'order_id': None,
                'message': f"执行异常：{e}",
                'mode': 'real',
            }
    
    def _record_performance(self, signal: Dict):
        """
        记录交易绩效
        
        在卖出执行后，自动配对买入记录并更新 trade_pairs 表
        
        Args:
            signal: 交易信号 (status 应该已经是 'executed')
        """
        try:
            # 只对卖出操作进行绩效记录 (支持大小写)
            if signal['action'].upper() != 'SELL':
                return
            
            # 使用 tracker 配对交易
            pairs = self.tracker.match_trades(code=signal['code'])
            
            if not pairs:
                logger.info(f"没有找到匹配的交易对：{signal['code']}")
                return
            
            # 找到包含当前卖出信号的配对
            current_sell_pairs = [p for p in pairs if p['sell_signal_id'] == signal['id']]
            
            if not current_sell_pairs:
                logger.info(f"当前卖出信号未匹配到买入：{signal['id']}")
                return
            
            # 插入到 trade_pairs 表
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                
                for pair in current_sell_pairs:
                    # 生成配对 ID
                    import hashlib
                    pair_id = hashlib.md5(
                        f"{pair['buy_signal_id']}_{pair['sell_signal_id']}_{pair['quantity']}".encode()
                    ).hexdigest()[:16]
                    
                    # 检查是否已存在
                    cursor.execute("SELECT id FROM trade_pairs WHERE id = ?", (pair_id,))
                    if cursor.fetchone():
                        continue
                    
                    # 插入配对记录
                    cursor.execute("""
                        INSERT INTO trade_pairs 
                        (id, buy_signal_id, sell_signal_id, code, buy_price, sell_price,
                         quantity, return_rate, profit, open_time, close_time, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        pair_id,
                        pair['buy_signal_id'],
                        pair['sell_signal_id'],
                        pair['code'],
                        pair['buy_price'],
                        pair['sell_price'],
                        pair['quantity'],
                        pair['return_rate'],
                        pair['profit'],
                        pair['open_time'],
                        pair['close_time'],
                        pair['status'],
                    ))
            
            logger.info(f"绩效记录成功：{len(current_sell_pairs)} 个交易对")
            
        except Exception as e:
            logger.error(f"记录绩效失败：{e}")
    
    def _get_trade_profit_info(self, signal: Dict) -> Optional[Dict]:
        """
        获取交易收益信息
        
        Args:
            signal: 交易信号
            
        Returns:
            收益信息 dict，包含 return_rate, profit 等
        """
        try:
            # 只对卖出操作 (支持大小写)
            if signal['action'].upper() != 'SELL':
                return None
            
            # 获取匹配的配对
            pairs = self.tracker.match_trades(code=signal['code'])
            current_sell_pairs = [p for p in pairs if p['sell_signal_id'] == signal['id']]
            
            if not current_sell_pairs:
                return None
            
            # 汇总收益信息
            total_profit = sum(p['profit'] for p in current_sell_pairs)
            total_cost = sum(p['cost'] for p in current_sell_pairs)
            
            # 计算加权平均收益率
            if total_cost > 0:
                avg_return = total_profit / total_cost * 100
            else:
                avg_return = 0.0
            
            return {
                'total_profit': total_profit,
                'total_cost': total_cost,
                'avg_return': avg_return,
                'trade_count': len(current_sell_pairs),
            }
            
        except Exception as e:
            logger.error(f"获取收益信息失败：{e}")
            return None
    
    def notify_result(self, signal: Dict, result: Dict, profit_info: Dict = None):
        """推送执行结果到钉钉"""
        try:
            icon = "✅" if result['success'] else "❌"
            mode_text = "实盘" if result['mode'] == 'real' else "模拟"
            
            lines = [
                f"### {icon} 交易执行结果\n\n",
                f"**{signal['name']} ({signal['code']})**\n\n",
                f"---\n\n",
                f"**方向**: {'买入' if signal['action'] == 'BUY' else '卖出'}\n\n",
                f"**状态**: {'成功' if result['success'] else '失败'}\n\n",
                f"**模式**: {mode_text}\n\n",
            ]
            
            if result['success']:
                lines.extend([
                    f"**订单 ID**: `{result['order_id']}`\n\n",
                    f"**价格**: {signal['price']:.2f} HKD\n\n",
                    f"**数量**: {signal.get('quantity', 100)} 股\n\n",
                ])
                
                # 添加收益信息 (如果是卖出)
                if profit_info:
                    profit_icon = "💰" if profit_info['total_profit'] > 0 else "📉"
                    lines.extend([
                        f"---\n\n",
                        f"**{profit_icon} 收益信息**\n\n",
                        f"**实现收益**: {profit_info['total_profit']:.2f} HKD\n\n",
                        f"**收益率**: {profit_info['avg_return']:.2f}%\n\n",
                        f"**交易对数**: {profit_info['trade_count']}\n\n",
                    ])
            else:
                lines.extend([
                    f"**原因**: {result['message']}\n\n",
                ])
            
            lines.extend([
                f"---\n\n",
                f"🕒 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n",
            ])
            
            markdown_text = "".join(lines)
            
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": f"{icon} 交易执行结果 - {signal['name']}",
                    "text": markdown_text,
                },
                "at": {"isAtAll": False},
            }
            
            self.notifier._do_post(payload)
            logger.info("执行结果推送成功")
            
        except Exception as e:
            logger.error(f"推送执行结果失败：{e}")
    
    def execute(self, signal_id: str, action: str) -> bool:
        """
        执行交易
        
        Args:
            signal_id: 信号 ID
            action: BUY | SELL
            
        Returns:
            是否成功
        """
        logger.info(f"收到执行请求：{signal_id} {action}")
        
        # 获取信号
        signal = self.get_signal(signal_id)
        if not signal:
            logger.error(f"信号不存在：{signal_id}")
            return False
        
        # 检查状态
        if signal['status'] != 'pending_confirmation':
            logger.warning(f"信号状态不正确：{signal['status']}")
            return False
        
        # 执行交易
        if action == 'BUY':
            result = self.execute_buy(signal)
        elif action == 'SELL':
            result = self.execute_sell(signal)
        else:
            logger.error(f"未知操作：{action}")
            return False
        
        # 更新状态 (先更新状态，再记录绩效)
        executed_at = datetime.now().isoformat()
        profit_info = None  # 初始化
        
        if result['success']:
            self.update_signal_status(signal_id, 'executed', executed_at)
            
            # 记录绩效 (仅当交易成功且为卖出时)
            if action == 'SELL':
                try:
                    # 更新 signal 对象中的 executed_at 和 status
                    signal['executed_at'] = executed_at
                    signal['status'] = 'executed'
                    
                    # 记录绩效到数据库
                    self._record_performance(signal)
                    
                    # 获取收益信息用于推送
                    profit_info = self._get_trade_profit_info(signal)
                except Exception as e:
                    # 绩效记录失败不影响交易执行
                    logger.error(f"绩效记录失败，但不影响交易执行：{e}")
        else:
            self.update_signal_status(signal_id, 'failed', executed_at)
        
        # 推送结果 (包含收益信息)
        self.notify_result(signal, result, profit_info)
        
        return result['success']


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='FTSeek 人工确认交易执行器')
    parser.add_argument('--signal-id', required=True, help='信号 ID')
    parser.add_argument('--action', required=True, choices=['BUY', 'SELL'], help='操作类型')
    parser.add_argument('--mode', choices=['paper', 'real'], default=None, help='交易模式')
    args = parser.parse_args()
    
    if args.mode:
        global TRADING_MODE
        TRADING_MODE = args.mode
    
    logger.info("=" * 60)
    logger.info("🎯 FTSeek 交易执行器 (人工确认模式)")
    logger.info("=" * 60)
    logger.info(f"信号 ID: {args.signal_id}")
    logger.info(f"操作：{args.action}")
    logger.info(f"模式：{TRADING_MODE}")
    logger.info("=" * 60)
    
    executor = ManualTradeExecutor()
    success = executor.execute(args.signal_id, args.action)
    
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
