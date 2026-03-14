#!/usr/bin/env python3
"""
FTSeek 交易执行器 - 富途联动增强版

功能:
- 接收钉钉确认回调
- 执行买入/卖出操作 (支持富途实盘)
- 完整记录交易信息
- 推送执行结果到钉钉
- 记录收益率

使用方式:
    python3 scripts/trade_executor.py --signal-id xxx --action BUY --mode paper
"""
import sys
import os
import logging
import argparse
import hashlib
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


class TradeExecutor:
    """交易执行器 (支持富途联动)"""
    
    def __init__(self, mode: str = 'paper'):
        """
        初始化执行器
        
        Args:
            mode: 交易模式 ('paper' | 'real')
        """
        self.mode = mode
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
                
                trd_env = TrdEnv.REAL if self.mode == 'real' else TrdEnv.SIMULATE
                
                self._trade_ctx = OpenSecTradeContext(
                    host=config.FUTU_HOST,
                    port=config.FUTU_PORT,
                    trd_env=trd_env,
                )
                logger.info(f"交易上下文已创建 (模式={self.mode})")
            except Exception as e:
                logger.error(f"创建交易上下文失败：{e}")
                raise
        return self._trade_ctx
    
    def get_signal(self, signal_id: str) -> Optional[Dict]:
        """从数据库获取信号"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, code, name, action, price, quantity, strategy, 
                       reason, status, created_at
                FROM signals
                WHERE id = ?
            """, (signal_id,))
            
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    
    def update_signal_status(self, signal_id: str, status: str, 
                            executed_at: str = None, order_id: str = None):
        """更新信号状态"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if executed_at:
                cursor.execute("""
                    UPDATE signals
                    SET status = ?, executed_at = ?, order_id = ?
                    WHERE id = ?
                """, (status, executed_at, order_id, signal_id))
            else:
                cursor.execute("""
                    UPDATE signals
                    SET status = ?, order_id = ?
                    WHERE id = ?
                """, (status, order_id, signal_id))
    
    def execute_buy(self, signal: Dict) -> Dict:
        """
        执行买入
        
        Returns:
            执行结果 {success: bool, order_id: str, message: str, 
                     fill_price: float, fill_qty: int}
        """
        code = signal['code']
        quantity = signal.get('quantity', 100)
        price = signal['price']
        
        logger.info(f"执行买入：{code} x {quantity} @ {price}")
        
        if self.mode == 'paper':
            # 模拟模式
            order_id = f"PAPER_{signal['id']}_{int(datetime.now().timestamp())}"
            return {
                'success': True,
                'order_id': order_id,
                'message': f"模拟买入 {code} x {quantity} @ {price:.2f} HKD",
                'mode': 'paper',
                'fill_price': price,
                'fill_qty': quantity,
            }
        
        # 实盘模式 - 富途下单
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
                order_id = str(data['order_id'][0])
                fill_price = data['price'][0] if 'price' in data else price
                fill_qty = data['qty'][0] if 'qty' in data else quantity
                
                logger.info(f"买入订单已提交：{order_id}")
                return {
                    'success': True,
                    'order_id': order_id,
                    'message': f"实盘买入 {code} x {fill_qty} @ {fill_price:.2f} HKD",
                    'mode': 'real',
                    'fill_price': fill_price,
                    'fill_qty': fill_qty,
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
            执行结果 {success: bool, order_id: str, message: str,
                     fill_price: float, fill_qty: int, profit: float}
        """
        code = signal['code']
        quantity = signal.get('quantity', 100)
        price = signal['price']
        
        logger.info(f"执行卖出：{code} x {quantity} @ {price}")
        
        if self.mode == 'paper':
            # 模拟模式
            order_id = f"PAPER_{signal['id']}_{int(datetime.now().timestamp())}"
            
            # 计算收益 (需要找到对应的买入记录)
            profit_info = self._calculate_profit(code, price, quantity)
            
            return {
                'success': True,
                'order_id': order_id,
                'message': f"模拟卖出 {code} x {quantity} @ {price:.2f} HKD",
                'mode': 'paper',
                'fill_price': price,
                'fill_qty': quantity,
                **profit_info,
            }
        
        # 实盘模式 - 富途下单
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
                order_id = str(data['order_id'][0])
                fill_price = data['price'][0] if 'price' in data else price
                fill_qty = data['qty'][0] if 'qty' in data else quantity
                
                # 计算收益
                profit_info = self._calculate_profit(code, fill_price, fill_qty)
                
                logger.info(f"卖出订单已提交：{order_id}")
                return {
                    'success': True,
                    'order_id': order_id,
                    'message': f"实盘卖出 {code} x {fill_qty} @ {fill_price:.2f} HKD",
                    'mode': 'real',
                    'fill_price': fill_price,
                    'fill_qty': fill_qty,
                    **profit_info,
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
    
    def _calculate_profit(self, code: str, sell_price: float, 
                         sell_qty: int) -> Dict:
        """
        计算交易收益
        
        Args:
            code: 股票代码
            sell_price: 卖出价格
            sell_qty: 卖出数量
            
        Returns:
            {profit: float, return_rate: float, buy_price: float}
        """
        try:
            # 查找最近的未平仓买入记录
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, price, quantity
                    FROM signals
                    WHERE code = ? 
                      AND action = 'buy'
                      AND status = 'executed'
                    ORDER BY executed_at DESC
                    LIMIT 1
                """, (code,))
                
                row = cursor.fetchone()
                if row:
                    buy_price = row['price']
                    buy_qty = row['quantity']
                    
                    # 计算收益
                    profit = (sell_price - buy_price) * min(sell_qty, buy_qty)
                    return_rate = (sell_price - buy_price) / buy_price * 100
                    
                    return {
                        'profit': profit,
                        'return_rate': return_rate,
                        'buy_price': buy_price,
                    }
        except Exception as e:
            logger.error(f"计算收益失败：{e}")
        
        return {
            'profit': 0.0,
            'return_rate': 0.0,
            'buy_price': 0.0,
        }
    
    def _record_trade_pair(self, signal: Dict, result: Dict):
        """
        记录交易配对到数据库
        
        Args:
            signal: 交易信号
            result: 执行结果
        """
        try:
            # 只对卖出操作记录交易对
            if signal['action'].upper() != 'SELL':
                return
            
            # 查找对应的买入记录
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, price, quantity, executed_at
                    FROM signals
                    WHERE code = ? 
                      AND action = 'buy'
                      AND status = 'executed'
                    ORDER BY executed_at DESC
                    LIMIT 1
                """, (signal['code'],))
                
                buy_row = cursor.fetchone()
                if not buy_row:
                    return
                
                buy_id = buy_row['id']
                buy_price = buy_row['price']
                buy_qty = buy_row['quantity']
                buy_time = buy_row['executed_at']
                
                # 生成交易对 ID
                pair_id = hashlib.md5(
                    f"{buy_id}_{signal['id']}".encode()
                ).hexdigest()[:16]
                
                # 计算收益
                profit = result.get('profit', 0.0)
                return_rate = result.get('return_rate', 0.0)
                
                # 插入交易对记录
                cursor.execute("""
                    INSERT OR REPLACE INTO trade_pairs 
                    (id, buy_signal_id, sell_signal_id, code, 
                     buy_price, sell_price, quantity, 
                     return_rate, profit, open_time, close_time, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pair_id, buy_id, signal['id'], signal['code'],
                    buy_price, result.get('fill_price', signal['price']),
                    min(buy_qty, signal.get('quantity', 100)),
                    return_rate, profit, buy_time, 
                    signal.get('executed_at', datetime.now().isoformat()),
                    'closed'
                ))
                
                conn.commit()
                logger.info(f"交易对已记录：{pair_id}")
                
        except Exception as e:
            logger.error(f"记录交易对失败：{e}")
    
    def notify_result(self, signal: Dict, result: Dict):
        """
        推送执行结果到钉钉
        
        Args:
            signal: 交易信号
            result: 执行结果
        """
        try:
            icon = "✅" if result['success'] else "❌"
            mode_text = "实盘" if result.get('mode') == 'real' else "模拟"
            action_text = "买入" if signal['action'].upper() == 'BUY' else "卖出"
            
            lines = [
                f"### {icon} 交易执行结果\n\n",
                f"**{signal['name']} ({signal['code']})**\n\n",
                f"---\n\n",
                f"**方向**: {action_text}\n\n",
                f"**状态**: {'成功' if result['success'] else '失败'}\n\n",
                f"**模式**: {mode_text}\n\n",
            ]
            
            if result['success']:
                lines.extend([
                    f"**订单 ID**: `{result['order_id']}`\n\n",
                    f"**成交价格**: {result.get('fill_price', signal['price']):.2f} HKD\n\n",
                    f"**成交数量**: {result.get('fill_qty', signal.get('quantity', 100))} 股\n\n",
                ])
                
                # 如果是卖出，添加收益信息
                if signal['action'].upper() == 'SELL' and 'profit' in result:
                    profit = result.get('profit', 0.0)
                    return_rate = result.get('return_rate', 0.0)
                    profit_icon = "📈" if profit > 0 else "📉" if profit < 0 else "➖"
                    
                    lines.extend([
                        f"---\n\n",
                        f"{profit_icon} **收益统计**\n\n",
                        f"**收益金额**: {profit:+.2f} HKD\n\n",
                        f"**收益率**: {return_rate:+.2f}%\n\n",
                        f"**买入价**: {result.get('buy_price', 0):.2f} HKD\n\n",
                        f"**卖出价**: {result.get('fill_price', signal['price']):.2f} HKD\n\n",
                    ])
            else:
                lines.extend([
                    f"**原因**: {result.get('message', '未知错误')}\n\n",
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
        logger.info(f"收到执行请求：{signal_id} {action} (模式={self.mode})")
        
        # 获取信号
        signal = self.get_signal(signal_id)
        if not signal:
            logger.error(f"信号不存在：{signal_id}")
            return False
        
        # 检查状态
        if signal['status'] not in ['pending_confirmation', 'pending']:
            logger.warning(f"信号状态不正确：{signal['status']}")
            return False
        
        # 执行交易
        if action.upper() == 'BUY':
            result = self.execute_buy(signal)
        elif action.upper() == 'SELL':
            result = self.execute_sell(signal)
        else:
            logger.error(f"未知操作：{action}")
            return False
        
        # 更新信号状态
        executed_at = datetime.now().isoformat()
        order_id = result.get('order_id')
        
        if result['success']:
            self.update_signal_status(signal_id, 'executed', executed_at, order_id)
            
            # 记录交易对 (卖出时)
            if action.upper() == 'SELL':
                self._record_trade_pair(signal, result)
        else:
            self.update_signal_status(signal_id, 'failed', executed_at, order_id)
        
        # 推送结果
        self.notify_result(signal, result)
        
        return result['success']


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='FTSeek 交易执行器')
    parser.add_argument('--signal-id', required=True, help='信号 ID')
    parser.add_argument('--action', required=True, choices=['BUY', 'SELL'], 
                       help='操作类型')
    parser.add_argument('--mode', choices=['paper', 'real'], default='paper', 
                       help='交易模式 (默认：paper)')
    args = parser.parse_args()
    
    logger.info("=" * 60)
    logger.info("🎯 FTSeek 交易执行器 (富途联动)")
    logger.info("=" * 60)
    logger.info(f"信号 ID: {args.signal_id}")
    logger.info(f"操作：{args.action}")
    logger.info(f"模式：{args.mode}")
    logger.info("=" * 60)
    
    executor = TradeExecutor(mode=args.mode)
    success = executor.execute(args.signal_id, args.action)
    
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
