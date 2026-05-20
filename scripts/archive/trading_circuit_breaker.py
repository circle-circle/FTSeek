#!/usr/bin/env python3
"""
交易频率熔断器 - FTSeek-US

功能:
- 跟踪单日交易次数
- 达到上限后自动禁止新开仓
- 支持手动重置和查询

配置:
- 单日最大交易数：5 笔 (USER.md 规定)
- 单只股票日交易上限：2 笔
- 连续亏损熔断：2 笔后暂停 24 小时

作者：FTSeek-US Elite Squad
版本：1.0 (2026-03-28 P0 改进)
"""

import json
import os
from datetime import datetime, timedelta

# 配置文件路径
STATE_FILE = '/opt/ftseek/FTSeek-US/state/trading_limits.json'

# 默认限制
DEFAULT_LIMITS = {
    'daily_max_trades': 5,          # 单日最大交易数
    'per_stock_daily_max': 2,       # 单只股票日交易上限
    'consecutive_loss_limit': 2,    # 连续亏损熔断
    'cooldown_hours': 24            # 熔断后冷却时间 (小时)
}

class TradingCircuitBreaker:
    def __init__(self):
        self.state = self.load_state()
        self.check_and_reset_day()
    
    def load_state(self):
        """加载状态文件"""
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        else:
            # 初始化状态
            return {
                'last_reset_date': datetime.now().strftime('%Y-%m-%d'),
                'daily_trade_count': 0,
                'stock_trade_counts': {},  # {symbol: count}
                'consecutive_losses': 0,
                'last_loss_time': None,
                'circuit_breaker_active': False,
                'circuit_breaker_until': None
            }
    
    def save_state(self):
        """保存状态文件"""
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w') as f:
            json.dump(self.state, f, indent=2)
    
    def check_and_reset_day(self):
        """检查是否需要重置日计数"""
        today = datetime.now().strftime('%Y-%m-%d')
        if self.state['last_reset_date'] != today:
            # 新的一天，重置计数
            self.state['last_reset_date'] = today
            self.state['daily_trade_count'] = 0
            self.state['stock_trade_counts'] = {}
            self.save_state()
            print(f"[INFO] 新交易日开始 ({today}), 已重置交易计数")
    
    def can_trade(self, symbol=None):
        """
        检查是否允许交易
        
        Args:
            symbol: 股票代码 (可选)
        
        Returns:
            (bool, str): (是否允许，原因)
        """
        # 检查熔断状态
        if self.state['circuit_breaker_active']:
            until = datetime.fromisoformat(self.state['circuit_breaker_until'])
            if datetime.now() < until:
                return False, f"连续亏损熔断中，剩余 {until - datetime.now()}"
            else:
                # 熔断期已过，自动解除
                self.state['circuit_breaker_active'] = False
                self.state['circuit_breaker_until'] = None
                self.state['consecutive_losses'] = 0
                self.save_state()
                print("[INFO] 熔断已解除")
        
        # 检查单日交易上限
        if self.state['daily_trade_count'] >= DEFAULT_LIMITS['daily_max_trades']:
            return False, f"已达单日交易上限 ({DEFAULT_LIMITS['daily_max_trades']}笔)"
        
        # 检查单只股票交易上限
        if symbol:
            stock_count = self.state['stock_trade_counts'].get(symbol, 0)
            if stock_count >= DEFAULT_LIMITS['per_stock_daily_max']:
                return False, f"{symbol} 已达单日交易上限 ({DEFAULT_LIMITS['per_stock_daily_max']}笔)"
        
        return True, "允许交易"
    
    def record_trade(self, symbol, is_loss=False):
        """
        记录交易
        
        Args:
            symbol: 股票代码
            is_loss: 是否为亏损交易
        """
        # 增加日计数
        self.state['daily_trade_count'] += 1
        
        # 增加股票计数
        if symbol not in self.state['stock_trade_counts']:
            self.state['stock_trade_counts'][symbol] = 0
        self.state['stock_trade_counts'][symbol] += 1
        
        # 记录亏损
        if is_loss:
            self.state['consecutive_losses'] += 1
            self.state['last_loss_time'] = datetime.now().isoformat()
            
            # 检查是否需要触发熔断
            if self.state['consecutive_losses'] >= DEFAULT_LIMITS['consecutive_loss_limit']:
                self.state['circuit_breaker_active'] = True
                self.state['circuit_breaker_until'] = (
                    datetime.now() + timedelta(hours=DEFAULT_LIMITS['cooldown_hours'])
                ).isoformat()
                print(f"[WARNING] 触发连续亏损熔断！暂停交易 {DEFAULT_LIMITS['cooldown_hours']} 小时")
        else:
            # 盈利交易，重置连续亏损计数
            self.state['consecutive_losses'] = 0
        
        self.save_state()
        print(f"[INFO] 交易已记录：{symbol}, 当日累计：{self.state['daily_trade_count']}/{DEFAULT_LIMITS['daily_max_trades']}")
    
    def get_status(self):
        """获取当前状态"""
        return {
            'date': self.state['last_reset_date'],
            'daily_trades': f"{self.state['daily_trade_count']}/{DEFAULT_LIMITS['daily_max_trades']}",
            'consecutive_losses': self.state['consecutive_losses'],
            'circuit_breaker': 'ACTIVE' if self.state['circuit_breaker_active'] else 'INACTIVE',
            'stock_counts': self.state['stock_trade_counts']
        }

def main():
    import sys
    
    breaker = TradingCircuitBreaker()
    
    if len(sys.argv) < 2:
        # 查询状态
        status = breaker.get_status()
        print(f"\n{'='*50}")
        print("交易频率熔断器状态 - FTSeek-US")
        print(f"{'='*50}")
        print(f"日期：{status['date']}")
        print(f"当日交易：{status['daily_trades']}")
        print(f"连续亏损：{status['consecutive_losses']}")
        print(f"熔断状态：{status['circuit_breaker']}")
        if status['stock_counts']:
            print(f"股票交易计数：{status['stock_counts']}")
        print(f"{'='*50}\n")
    
    elif sys.argv[1] == 'check':
        # 检查是否允许交易
        symbol = sys.argv[2] if len(sys.argv) > 2 else None
        can_trade, reason = breaker.can_trade(symbol)
        if can_trade:
            print(f"✅ {reason}")
            sys.exit(0)
        else:
            print(f"❌ {reason}")
            sys.exit(1)
    
    elif sys.argv[1] == 'record':
        # 记录交易
        if len(sys.argv) < 3:
            print("用法：record <symbol> [loss]")
            sys.exit(1)
        symbol = sys.argv[2]
        is_loss = len(sys.argv) > 3 and sys.argv[3] == 'loss'
        breaker.record_trade(symbol, is_loss)
    
    elif sys.argv[1] == 'reset':
        # 重置状态 (手动)
        breaker.state['daily_trade_count'] = 0
        breaker.state['stock_trade_counts'] = {}
        breaker.state['consecutive_losses'] = 0
        breaker.state['circuit_breaker_active'] = False
        breaker.save_state()
        print("✅ 状态已重置")
    
    else:
        print("用法:")
        print("  python trading_circuit_breaker.py          # 查询状态")
        print("  python trading_circuit_breaker.py check    # 检查是否允许交易")
        print("  python trading_circuit_breaker.py check TSLA")
        print("  python trading_circuit_breaker.py record TSLA  # 记录交易")
        print("  python trading_circuit_breaker.py record TSLA loss  # 记录亏损交易")
        print("  python trading_circuit_breaker.py reset    # 重置状态")

if __name__ == '__main__':
    main()
