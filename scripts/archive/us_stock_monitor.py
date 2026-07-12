#!/usr/bin/env python3.11
"""
FTSeek-US 美股行情监控脚本

功能:
1. 通过 Yahoo Finance 获取美股价格 (15 分钟延迟)
2. 定期查询监控股票池
3. 计算止损止盈
4. 钉钉推送告警

数据源：Yahoo Finance (富途无美股行情权限时的备用方案)
"""
import sys
import os
import time
from datetime import datetime
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from dotenv import load_dotenv
load_dotenv("/opt/ftseek/FTSeek-US/.env.us")

# 导入 Yahoo Finance
try:
    import yfinance as yf
    HAS_YAHOO = True
except ImportError:
    print("❌ Yahoo Finance 未安装，请运行：pip install yfinance")
    sys.exit(1)

# ============================================================
# 配置
# ============================================================
WATCHLIST = [
    "TSLA",   # 特斯拉
    "NVDA",   # 英伟达
    "MU",     # 美光科技
    "BABA",   # 阿里巴巴
]

# 止损止盈配置 (从 .env.us 读取)
STOP_LOSS_RATIO = float(os.getenv("DEFAULT_STOP_LOSS", "0.10"))  # -10%
TAKE_PROFIT_1_RATIO = float(os.getenv("DEFAULT_TAKE_PROFIT_1", "0.15"))  # +15%
TAKE_PROFIT_2_RATIO = float(os.getenv("DEFAULT_TAKE_PROFIT_2", "0.25"))  # +25%
TAKE_PROFIT_3_RATIO = float(os.getenv("DEFAULT_TAKE_PROFIT_3", "0.40"))  # +40%

# 持仓成本 (根据实际持仓修改)
POSITIONS = {
    "AAPL": {"cost": 170.0, "qty": 100},
    "MSFT": {"cost": 400.0, "qty": 50},
    "NVDA": {"cost": 800.0, "qty": 30},
}

# 查询间隔 (秒)
QUERY_INTERVAL = 300  # 每 5 分钟查询一次 (Yahoo Finance 有延迟，不需要太频繁)

# ============================================================
# Yahoo Finance 行情获取
# ============================================================
def get_stock_prices(stock_list):
    """
    通过 Yahoo Finance 获取股票价格 (15 分钟延迟)
    
    Args:
        stock_list: 股票代码列表 ["AAPL", "MSFT", ...]
    
    Returns:
        dict: {code: {last_price, change, change_percent, ...}}
    """
    prices = {}
    
    for code in stock_list:
        try:
            ticker = yf.Ticker(code)
            info = ticker.fast_info
            
            if info and info.last_price:
                # 计算涨跌
                prev_close = info.previous_close if hasattr(info, 'previous_close') else info.last_price
                change = info.last_price - prev_close
                change_percent = (change / prev_close * 100) if prev_close > 0 else 0
                
                prices[code] = {
                    "last_price": round(info.last_price, 2),
                    "change": round(change, 2),
                    "change_percent": round(change_percent, 2),
                    "open": round(info.day_open, 2) if hasattr(info, 'day_open') else 0,
                    "high": round(info.day_high, 2) if hasattr(info, 'day_high') else 0,
                    "low": round(info.day_low, 2) if hasattr(info, 'day_low') else 0,
                    "prev_close": round(prev_close, 2),
                    "volume": info.last_volume if hasattr(info, 'last_volume') else 0,
                    "source": "yahoo",
                }
            else:
                print(f"⚠️  {code}: Yahoo 获取失败")
                prices[code] = None
                
        except Exception as e:
            print(f"⚠️  {code}: Yahoo 异常 - {str(e)[:50]}")
            prices[code] = None
    
    return prices

# ============================================================
# 止损止盈计算
# ============================================================
def calculate_stop_loss_take_profit(code, current_price, cost_price):
    """
    计算止损止盈价格
    
    Args:
        code: 股票代码
        current_price: 当前价格
        cost_price: 成本价
    
    Returns:
        dict: {stop_loss, take_profit_1, take_profit_2, take_profit_3, pnl_ratio}
    """
    stop_loss = cost_price * (1 - STOP_LOSS_RATIO)
    take_profit_1 = cost_price * (1 + TAKE_PROFIT_1_RATIO)
    take_profit_2 = cost_price * (1 + TAKE_PROFIT_2_RATIO)
    take_profit_3 = cost_price * (1 + TAKE_PROFIT_3_RATIO)
    
    pnl_ratio = (current_price - cost_price) / cost_price
    
    return {
        "stop_loss": round(stop_loss, 2),
        "take_profit_1": round(take_profit_1, 2),
        "take_profit_2": round(take_profit_2, 2),
        "take_profit_3": round(take_profit_3, 2),
        "pnl_ratio": round(pnl_ratio, 4),
        "pnl_percent": f"{pnl_ratio * 100:+.2f}%",
    }

# ============================================================
# 告警检查
# ============================================================
def check_alerts(code, current_price, cost_price):
    """
    检查是否需要告警 (止损/止盈)
    
    Returns:
        list: 告警消息列表
    """
    alerts = []
    
    levels = calculate_stop_loss_take_profit(code, current_price, cost_price)
    
    # 止损告警
    if current_price <= levels["stop_loss"]:
        alerts.append({
            "type": "STOP_LOSS",
            "level": "CRITICAL",
            "message": f"⚠️ {code} 触发止损！当前价 ${current_price:.2f}, 止损价 ${levels['stop_loss']}"
        })
    
    # 止盈告警
    if current_price >= levels["take_profit_3"]:
        alerts.append({
            "type": "TAKE_PROFIT_3",
            "level": "INFO",
            "message": f"✅ {code} 触发第三止盈！当前价 ${current_price:.2f}, 目标价 ${levels['take_profit_3']}"
        })
    elif current_price >= levels["take_profit_2"]:
        alerts.append({
            "type": "TAKE_PROFIT_2",
            "level": "INFO",
            "message": f"✅ {code} 触发第二止盈！当前价 ${current_price:.2f}, 目标价 ${levels['take_profit_2']}"
        })
    elif current_price >= levels["take_profit_1"]:
        alerts.append({
            "type": "TAKE_PROFIT_1",
            "level": "INFO",
            "message": f"✅ {code} 触发第一止盈！当前价 ${current_price:.2f}, 目标价 ${levels['take_profit_1']}"
        })
    
    return alerts

# ============================================================
# 钉钉推送
# ============================================================
def send_dingtalk_alert(title, content, level="INFO"):
    """
    发送钉钉告警
    
    Args:
        title: 标题
        content: 内容
        level: 告警级别 (INFO/WARN/CRITICAL)
    """
    from app.observability.alert_manager import AlertManager
    
    webhook = os.getenv("DINGTALK_WEBHOOK_URL_US")
    secret = os.getenv("DINGTALK_SECRET_US")
    
    if not webhook or not secret:
        print(f"❌ 钉钉配置缺失")
        return False
    
    manager = AlertManager(
        provider="dingtalk",
        dingtalk_webhook=webhook,
        dingtalk_secret=secret,
    )
    
    # 根据级别设置颜色
    colors = {
        "INFO": "#00ff00",
        "WARN": "#ffa500",
        "CRITICAL": "#ff0000",
    }
    
    result = manager.send_info(
        title=title,
        content=f"**级别**: <font color=\"{colors.get(level, '#000000')}\">{level}</font>\n\n{content}"
    )
    
    if result:
        print(f"✅ 钉钉推送成功")
    else:
        print(f"❌ 钉钉推送失败")
    
    return result

# ============================================================
# 主循环
# ============================================================
def main():
    """主监控循环"""
    print("=" * 80)
    print("  FTSeek-US 美股行情监控")
    print(f"  数据源：Yahoo Finance (15 分钟延迟)")
    print(f"  监控股票：{len(WATCHLIST)} 只")
    print(f"  查询间隔：{QUERY_INTERVAL} 秒")
    print(f"  止损：-{STOP_LOSS_RATIO*100:.0f}%, 止盈：+{TAKE_PROFIT_1_RATIO*100:.0f}%/+{TAKE_PROFIT_2_RATIO*100:.0f}%/+{TAKE_PROFIT_3_RATIO*100:.0f}%")
    print("=" * 80)
    print()
    
    # 发送启动通知
    send_dingtalk_alert(
        title="🚀 FTSeek-US 监控启动",
        content=f"数据源：Yahoo Finance\n监控股票：{len(WATCHLIST)}只\n查询间隔：{QUERY_INTERVAL}秒\n止损：-{STOP_LOSS_RATIO*100:.0f}%, 止盈：+{TAKE_PROFIT_1_RATIO*100:.0f}%/+{TAKE_PROFIT_2_RATIO*100:.0f}%/+{TAKE_PROFIT_3_RATIO*100:.0f}%"
    )
    
    while True:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 检查是否在交易时间 (美东时间 9:30-16:00 = 北京时间 22:30-05:00)
        beijing_hour = datetime.now().hour
        is_trading_time = (beijing_hour >= 22 or beijing_hour < 5)
        
        if not is_trading_time:
            print(f"[{current_time}] ⏰ 非交易时间，跳过")
            time.sleep(QUERY_INTERVAL)
            continue
        
        # 获取行情
        print(f"[{current_time}] 📡 使用 Yahoo Finance 获取行情...")
        prices = get_stock_prices(WATCHLIST)
        
        # 处理每只股票
        for code in WATCHLIST:
            if code not in prices or prices[code] is None:
                continue
            
            price_data = prices[code]
            current_price = price_data["last_price"]
            
            # 检查是否有持仓
            if code in POSITIONS:
                position = POSITIONS[code]
                cost_price = position["cost"]
                qty = position["qty"]
                
                # 计算止损止盈
                levels = calculate_stop_loss_take_profit(code, current_price, cost_price)
                
                # 检查告警
                alerts = check_alerts(code, current_price, cost_price)
                
                # 发送告警
                for alert in alerts:
                    send_dingtalk_alert(
                        title=f"{alert['type']} - {code}",
                        content=alert["message"],
                        level=alert["level"]
                    )
                
                # 打印持仓信息
                print(f"  💼 {code}: ${current_price:.2f} | 盈亏：{levels['pnl_percent']} | 止损：${levels['stop_loss']} | 止盈：${levels['take_profit_1']}/{levels['take_profit_2']}/{levels['take_profit_3']}")
            else:
                # 无持仓，只打印价格
                print(f"  📊 {code}: ${current_price:.2f} ({price_data['change_percent']:+.2f}%)")
        
        print()
        
        # 等待下次查询
        time.sleep(QUERY_INTERVAL)

# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 监控已停止")
        
        # 发送停止通知
        send_dingtalk_alert(
            title="⏹️ FTSeek-US 监控停止",
            content="监控脚本已手动停止"
        )
