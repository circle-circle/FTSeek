"""
数据加载器模块
2026-03-27 修复: 添加 generate_sample_data 函数
"""

def load_market_data():
    """加载市场数据"""
    return {"status": "data loader placeholder"}

def generate_sample_data(code: str, start_price: float = 100.0, days: int = 30):
    """
    生成模拟数据 (用于 API 演示和测试)
    
    Args:
        code: 股票代码
        start_price: 起始价格
        days: 天数
        
    Returns:
        dict: 包含 OHLCV 数据的字典
    """
    import random
    from datetime import datetime, timedelta
    
    data = []
    price = start_price
    
    for i in range(days):
        date = datetime.now() - timedelta(days=days-i)
        
        # 模拟价格波动
        change_pct = random.uniform(-0.03, 0.03)
        open_price = price
        close_price = price * (1 + change_pct)
        high_price = max(open_price, close_price) * (1 + random.uniform(0, 0.02))
        low_price = min(open_price, close_price) * (1 - random.uniform(0, 0.02))
        volume = random.randint(100000, 1000000)
        
        data.append({
            "date": date.strftime("%Y-%m-%d"),
            "open": round(open_price, 2),
            "high": round(high_price, 2),
            "low": round(low_price, 2),
            "close": round(close_price, 2),
            "volume": volume,
        })
        
        price = close_price
    
    return {
        "code": code,
        "data": data,
        "count": len(data),
    }