"""
滑点控制

监控和控制订单执行滑点
"""


class SlippageController:
    """滑点控制器"""
    
    def __init__(self, max_slippage_ratio: float = 0.005):
        self.max_slippage = max_slippage_ratio
    
    def is_acceptable(
        self,
        expected_price: float,
        actual_price: float,
        side: str,  # "BUY" or "SELL"
    ) -> bool:
        """检查滑点是否可接受"""
        if side == "BUY":
            slippage = (actual_price - expected_price) / expected_price
        else:  # SELL
            slippage = (expected_price - actual_price) / expected_price
        
        return slippage <= self.max_slippage
    
    def calculate_slippage(
        self,
        expected_price: float,
        actual_price: float,
        side: str,
    ) -> float:
        """计算滑点比例"""
        if side == "BUY":
            return (actual_price - expected_price) / expected_price
        else:
            return (expected_price - actual_price) / expected_price
