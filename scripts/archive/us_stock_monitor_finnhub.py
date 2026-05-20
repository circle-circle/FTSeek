#!/usr/bin/env python3.8
"""
FTSeek-US 美股行情监控脚本 (Finnhub 数据源)

功能:
1. 通过 Finnhub API 获取美股实时价格
2. 定期查询监控股票池
3. 计算止损止盈
4. 补仓逻辑 (分批建仓)
5. 钉钉推送告警

数据源：Finnhub (免费 60 次/分钟)
"""
import sys
import os
import time
import requests
from datetime import datetime
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from dotenv import load_dotenv
load_dotenv("/opt/ftseek/FTSeek-US/.env.us")

# 导入 Finnhub
try:
    import finnhub
    HAS_FINNHUB = True
except ImportError:
    print("⚠️  Finnhub 未安装，请运行：pip install finnhub-python")
    HAS_FINNHUB = False

# ============================================================
# 配置
# ============================================================
# Finnhub API Key (需要注册获取)
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# 监控股票池
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

# 补仓配置
ADD_POSITION_RATIO = float(os.getenv("ADD_POSITION_RATIO", "0.05"))  # 下跌 5% 补仓
MAX_ADD_POSITION_TIMES = int(os.getenv("MAX_ADD_POSITION_TIMES", "3"))  # 最多补仓 3 次

# 持仓管理 (实际应从数据库读取，这里示例)
POSITIONS = {
    "AAPL": {
        "cost": 170.0,      # 成本价
        "qty": 100,         # 持仓数量
        "add_times": 0,     # 已补仓次数
        "last_add_price": 170.0,  # 上次补仓价格
    },
    "MSFT": {
        "cost": 400.0,
        "qty": 50,
        "add_times": 0,
        "last_add_price": 400.0,
    },
    "NVDA": {
        "cost": 800.0,
        "qty": 30,
        "add_times": 0,
        "last_add_price": 800.0,
    },
}

# 查询间隔 (秒)
QUERY_INTERVAL = 60  # 每分钟查询一次 (Finnhub 免费 60 次/分钟)

# ============================================================
# Finnhub 行情获取
# ============================================================
def get_finnhub_api():
    """获取 Finnhub API 客户端"""
    if not HAS_FINNHUB:
        return None
    
    if not FINNHUB_API_KEY:
        print("❌ Finnhub API Key 未配置")
        return None
    
    return finnhub.Client(api_key=FINNHUB_API_KEY)


def get_stock_prices_finnhub(stock_list):
    """
    通过 Finnhub 获取股票实时价格
    
    Args:
        stock_list: 股票代码列表 ["AAPL", "MSFT", ...]
    
    Returns:
        dict: {code: {last_price, change, change_percent, ...}}
    """
    api = get_finnhub_api()
    if not api:
        return {}
    
    prices = {}
    
    for code in stock_list:
        try:
            # 获取实时报价
            quote = api.quote(code)
            
            if quote and quote['c'] > 0:  # c = current price
                prices[code] = {
                    "last_price": quote['c'],
                    "change": quote['d'],  # d = change
                    "change_percent": quote['dp'] / 100,  # dp = change percent
                    "open": quote['o'],
                    "high": quote['h'],
                    "low": quote['l'],
                    "prev_close": quote['pc'],  # pc = previous close
                    "volume": 0,  # Finnhub quote 不包含成交量
                    "source": "finnhub",
                }
            else:
                print(f"⚠️  {code}: Finnhub 获取失败")
                prices[code] = None
                
        except Exception as e:
            print(f"⚠️  {code}: Finnhub 异常 - {str(e)[:50]}")
            prices[code] = None
    
    return prices

# ============================================================
# 止损止盈计算
# ============================================================
def calculate_stop_loss_take_profit(code, current_price, cost_price):
    """
    计算止损止盈价格
    
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
# 补仓逻辑
# ============================================================
def check_add_position(code, current_price, position):
    """
    检查是否需要补仓
    
    补仓条件:
    1. 当前价 < 上次补仓价 * (1 - 补仓比例)
    2. 补仓次数 < 最大补仓次数
    
    Returns:
        dict: {should_add: bool, add_price: float, add_reason: str}
    """
    last_add_price = position["last_add_price"]
    add_times = position["add_times"]
    
    # 计算补仓触发价
    add_trigger_price = last_add_price * (1 - ADD_POSITION_RATIO)
    
    # 检查补仓条件
    if current_price <= add_trigger_price and add_times < MAX_ADD_POSITION_TIMES:
        return {
            "should_add": True,
            "add_price": current_price,
            "add_reason": f"下跌{ADD_POSITION_RATIO*100:.0f}%触发补仓",
            "trigger_price": add_trigger_price,
        }
    
    return {
        "should_add": False,
        "add_price": 0,
        "add_reason": "",
        "trigger_price": add_trigger_price,
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
    
    # 止损告警 (CRITICAL)
    if current_price <= levels["stop_loss"]:
        alerts.append({
            "type": "STOP_LOSS",
            "level": "CRITICAL",
            "message": f"⚠️ {code} 触发止损！当前价 ${current_price:.2f}, 止损价 ${levels['stop_loss']}"
        })
    
    # 止盈告警 (INFO)
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
    
    # 接近止损告警 (WARN)
    warning_price = levels["stop_loss"] * 1.02  # 距离止损 2%
    if current_price <= warning_price and current_price > levels["stop_loss"]:
        alerts.append({
            "type": "STOP_LOSS_WARNING",
            "level": "WARN",
            "message": f"⚠️ {code} 接近止损！当前价 ${current_price:.2f}, 止损价 ${levels['stop_loss']}"
        })
    
    return alerts

# ============================================================
# 钉钉推送
# ============================================================
def send_dingtalk_alert(title, content, level="INFO"):
    """发送钉钉告警"""
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
    print("  FTSeek-US 美股行情监控 (Finnhub)")
    print(f"  数据源：Finnhub (实时)")
    print(f"  监控股票：{len(WATCHLIST)} 只")
    print(f"  查询间隔：{QUERY_INTERVAL} 秒")
    print(f"  止损：-{STOP_LOSS_RATIO*100:.0f}%, 止盈：+{TAKE_PROFIT_1_RATIO*100:.0f}%/+{TAKE_PROFIT_2_RATIO*100:.0f}%/+{TAKE_PROFIT_3_RATIO*100:.0f}%")
    print(f"  补仓：下跌{ADD_POSITION_RATIO*100:.0f}%补仓，最多{MAX_ADD_POSITION_TIMES}次")
    print("=" * 80)
    print()
    
    # 检查 Finnhub 配置
    if not HAS_FINNHUB:
        print("❌ Finnhub 未安装，请运行：pip install finnhub-python")
        return
    
    if not FINNHUB_API_KEY:
        print("❌ Finnhub API Key 未配置，请在 .env.us 中设置 FINNHUB_API_KEY")
        return
    
    # 发送启动通知
    send_dingtalk_alert(
        title="🚀 FTSeek-US 监控启动",
        content=f"数据源：Finnhub\n监控股票：{len(WATCHLIST)}只\n查询间隔：{QUERY_INTERVAL}秒\n止损：-{STOP_LOSS_RATIO*100:.0f}%, 止盈：+{TAKE_PROFIT_1_RATIO*100:.0f}%/+{TAKE_PROFIT_2_RATIO*100:.0f}%/+{TAKE_PROFIT_3_RATIO*100:.0f}%\n补仓：下跌{ADD_POSITION_RATIO*100:.0f}%补仓，最多{MAX_ADD_POSITION_TIMES}次"
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
        print(f"[{current_time}] 📡 使用 Finnhub 获取行情...")
        prices = get_stock_prices_finnhub(WATCHLIST)
        
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
                
                # 检查补仓
                add_position = check_add_position(code, current_price, position)
                
                # 发送告警
                for alert in alerts:
                    send_dingtalk_alert(
                        title=f"{alert['type']} - {code}",
                        content=alert["message"],
                        level=alert["level"]
                    )
                
                # 补仓告警
                if add_position["should_add"]:
                    send_dingtalk_alert(
                        title=f"ADD_POSITION - {code}",
                        content=f"💰 {code} 触发补仓！{add_position['add_reason']}\n当前价：${add_position['add_price']:.2f}\n触发价：${add_position['trigger_price']:.2f}\n已补仓：{position['add_times']}/{MAX_ADD_POSITION_TIMES}次",
                        level="WARN"
                    )
                
                # 打印持仓信息
                print(f"  💼 {code}: ${current_price:.2f} | 盈亏：{levels['pnl_percent']} | 止损：${levels['stop_loss']} | 止盈：${levels['take_profit_1']}/{levels['take_profit_2']}/{levels['take_profit_3']}")
                if add_position["should_add"]:
                    print(f"      💰 触发补仓！{add_position['add_reason']} (已补{position['add_times']}/{MAX_ADD_POSITION_TIMES}次)")
            else:
                # 无持仓，只打印价格
                print(f"  📊 {code}: ${current_price:.2f} ({price_data['change_percent']*100:+.2f}%)")
        
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
