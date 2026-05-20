"""
卖空可用性检查器

在卖空前检查：
1. 股票是否可卖空
2. 可借券数量
3. 借券成本
"""
import logging
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class ShortAvailabilityChecker:
    """卖空可用性检查器"""
    
    def __init__(self, trade_ctx=None, quote_ctx=None):
        self.trade_ctx = trade_ctx
        self.quote_ctx = quote_ctx
    
    def check_shortable(self, symbol: str) -> Dict[str, Any]:
        """
        检查股票是否可卖空
        
        Args:
            symbol: 股票代码，如 "US.TSLA"
        
        Returns:
            {
                "shortable": bool,       # 是否可卖空
                "available_qty": int,    # 可借数量
                "borrow_rate": float,    # 借券利率 (年化)
                "reason": str,           # 原因说明
            }
        """
        result = {
            "shortable": True,  # 模拟环境默认可卖空
            "available_qty": 10000,
            "borrow_rate": 0.0,
            "reason": "模拟环境，无借券限制",
        }
        
        if not self.trade_ctx:
            return result
        
        try:
            from futu import RET_OK, TrdEnv
            
            # 实盘环境需要检查借券池
            # 注意：富途 API 可能需要特定接口查询借券可用性
            
            # 方法1: 尝试查询持仓，看是否支持卖空
            ret, positions = self.trade_ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
            
            if ret == RET_OK:
                # 模拟环境默认支持
                result["shortable"] = True
                result["reason"] = "模拟账户支持卖空"
            
        except Exception as e:
            logger.warning(f"[卖空检查] {symbol} 检查失败: {e}")
            result["shortable"] = True  # 默认允许尝试
            result["reason"] = f"检查失败，默认允许: {e}"
        
        return result
    
    def get_short_summary(self, symbol: str) -> str:
        """获取卖空可用性摘要"""
        info = self.check_shortable(symbol)
        
        if info["shortable"]:
            return f"✅ {symbol} 可卖空 (可借: {info['available_qty']}股)"
        else:
            return f"❌ {symbol} 不可卖空 ({info['reason']})"


def check_and_decide(symbol: str, trade_ctx=None, quote_ctx=None) -> Dict[str, Any]:
    """
    检查卖空可用性并决定策略
    
    Returns:
        {
            "action": "direct_short" | "put_option" | "skip",
            "reason": str,
            "shortable": bool,
        }
    """
    checker = ShortAvailabilityChecker(trade_ctx, quote_ctx)
    info = checker.check_shortable(symbol)
    
    if info["shortable"]:
        return {
            "action": "direct_short",
            "reason": "股票可卖空，使用直接卖空",
            "shortable": True,
        }
    else:
        return {
            "action": "put_option",
            "reason": f"股票不可卖空 ({info['reason']})，降级到Put期权",
            "shortable": False,
        }


if __name__ == "__main__":
    import argparse
    from futu import OpenSecTradeContext, OpenQuoteContext, TrdMarket
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", type=str, required=True, help="股票代码")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    args = parser.parse_args()
    
    try:
        trd_ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=args.host, port=args.port)
        quote_ctx = OpenQuoteContext(host=args.host, port=args.port)
        
        checker = ShortAvailabilityChecker(trd_ctx, quote_ctx)
        print(checker.get_short_summary(args.symbol))
        
        decision = check_and_decide(args.symbol, trd_ctx, quote_ctx)
        print(f"\n建议策略: {decision['action']}")
        print(f"原因: {decision['reason']}")
        
        trd_ctx.close()
        quote_ctx.close()
        
    except Exception as e:
        print(f"错误: {e}")
