#!/usr/bin/env python3.8
"""
FTSeek-US 美股行情监控脚本 (双数据源轮询)

数据源:
- TwelveData (免费版：800 次/天，8 次/分钟)
- Finnhub (免费版：60 次/分钟)

功能:
1. 双数据源智能轮询，避免触发限流
2. 定期查询监控股票池
3. 计算止损止盈
4. 补仓逻辑 (分批建仓)
5. 钉钉推送告警

请求频率策略:
- 监控股票：8 只
- 查询间隔：60 秒
- 每小时请求：8 只 × 60 次 = 480 次/小时
- TwelveData 限制：800 次/天 ≈ 33 次/小时
- Finnhub 限制：60 次/分钟 = 3600 次/小时
- 策略：优先使用 Finnhub，TwelveData 作为备用 (每小时最多 30 次)
"""
import sys
import os
import time
import requests
from datetime import datetime
from collections import deque
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from dotenv import load_dotenv
load_dotenv("/opt/ftseek/FTSeek-US/.env.us")

# ============================================================
# 配置
# ============================================================
# API Keys
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# 监控股票池
WATCHLIST = [
    "TSLA",   # 特斯拉
    "NVDA",   # 英伟达
    "MU",     # 美光科技
    "BABA",   # 阿里巴巴
]

# 止损止盈配置
STOP_LOSS_RATIO = float(os.getenv("DEFAULT_STOP_LOSS", "0.10"))  # -10%
TAKE_PROFIT_1_RATIO = float(os.getenv("DEFAULT_TAKE_PROFIT_1", "0.15"))  # +15%
TAKE_PROFIT_2_RATIO = float(os.getenv("DEFAULT_TAKE_PROFIT_2", "0.25"))  # +25%
TAKE_PROFIT_3_RATIO = float(os.getenv("DEFAULT_TAKE_PROFIT_3", "0.40"))  # +40%

# 补仓配置
ADD_POSITION_RATIO = float(os.getenv("ADD_POSITION_RATIO", "0.05"))  # 下跌 5% 补仓
MAX_ADD_POSITION_TIMES = int(os.getenv("MAX_ADD_POSITION_TIMES", "3"))  # 最多补仓 3 次

# 持仓管理
POSITIONS = {
    "AAPL": {"cost": 170.0, "qty": 100, "add_times": 0, "last_add_price": 170.0},
    "MSFT": {"cost": 400.0, "qty": 50, "add_times": 0, "last_add_price": 400.0},
    "NVDA": {"cost": 800.0, "qty": 30, "add_times": 0, "last_add_price": 800.0},
}

# 查询间隔 (秒)
QUERY_INTERVAL = 60  # 每分钟查询一次

# 限流控制
class RateLimiter:
    """请求频率限制器"""
    def __init__(self, max_calls, period_seconds):
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self.calls = deque()
    
    def add_call(self):
        """添加一次调用"""
        now = time.time()
        # 清理过期调用
        while self.calls and self.calls[0] <= now - self.period_seconds:
            self.calls.popleft()
        
        # 检查是否超限
        if len(self.calls) >= self.max_calls:
            return False
        
        self.calls.append(now)
        return True
    
    def get_remaining(self):
        """获取剩余调用次数"""
        now = time.time()
        while self.calls and self.calls[0] <= now - self.period_seconds:
            self.calls.popleft()
        return self.max_calls - len(self.calls)
    
    def get_wait_time(self):
        """获取需要等待的时间"""
        if not self.calls:
            return 0
        now = time.time()
        oldest = self.calls[0]
        return max(0, self.period_seconds - (now - oldest))

# 初始化限流器
# TwelveData: 8 次/分钟，800 次/天
twelvedata_limiter = RateLimiter(max_calls=8, period_seconds=60)
# Finnhub: 60 次/分钟
finnhub_limiter = RateLimiter(max_calls=60, period_seconds=60)

# 数据源优先级 (优先使用 Finnhub)
PREFERRED_SOURCE = "finnhub"

# ============================================================
# TwelveData API
# ============================================================
def get_price_twelvedata(symbol):
    """
    通过 TwelveData 获取实时价格
    
    API: https://api.twelvedata.com/price?symbol={symbol}&apikey={apikey}
    限制：免费版 800 次/天，8 次/分钟
    """
    if not TWELVEDATA_API_KEY:
        return None
    
    try:
        url = f"https://api.twelvedata.com/price"
        params = {
            "symbol": symbol,
            "apikey": TWELVEDATA_API_KEY,
            "outputsize": "1",
        }
        
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if "price" in data:
                return float(data["price"])
        
        return None
    except Exception as e:
        print(f"⚠️  TwelveData 异常：{str(e)[:50]}")
        return None

# ============================================================
# Finnhub API
# ============================================================
def get_price_finnhub(symbol):
    """
    通过 Finnhub 获取实时价格
    
    API: https://finnhub.io/api/v1/quote?symbol={symbol}&token={token}
    限制：免费版 60 次/分钟
    """
    if not FINNHUB_API_KEY:
        return None
    
    try:
        url = "https://finnhub.io/api/v1/quote"
        params = {
            "symbol": symbol,
            "token": FINNHUB_API_KEY,
        }
        
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data and "c" in data and data["c"] > 0:
                return float(data["c"])  # c = current price
        
        return None
    except Exception as e:
        print(f"⚠️  Finnhub 异常：{str(e)[:50]}")
        return None

# ============================================================
# 智能获取价格 (双数据源轮询)
# ============================================================
def get_price_smart(symbol):
    """
    智能获取价格
    
    策略:
    1. 优先使用首选数据源
    2. 如果首选限流，使用备用数据源
    3. 记录使用次数，避免超限
    """
    global PREFERRED_SOURCE
    
    # 尝试首选数据源
    if PREFERRED_SOURCE == "finnhub":
        if finnhub_limiter.add_call():
            price = get_price_finnhub(symbol)
            if price:
                return price
    
    # 首选失败或限流，尝试备用数据源
    if twelvedata_limiter.add_call():
        price = get_price_twelvedata(symbol)
        if price:
            # 切换首选为 TwelveData
            PREFERRED_SOURCE = "twelvedata"
            return price
    
    # 都失败了，切换回 Finnhub
    PREFERRED_SOURCE = "finnhub"
    return None

# ============================================================
# 止损止盈计算
# ============================================================
def calculate_levels(code, current_price, cost_price):
    """计算止损止盈价格"""
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
    """检查是否需要补仓"""
    last_add_price = position["last_add_price"]
    add_times = position["add_times"]
    
    add_trigger_price = last_add_price * (1 - ADD_POSITION_RATIO)
    
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
    """检查是否需要告警"""
    alerts = []
    levels = calculate_levels(code, current_price, cost_price)
    
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
    warning_price = levels["stop_loss"] * 1.02
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
    print("  FTSeek-US 美股行情监控 (双数据源轮询)")
    print(f"  数据源：TwelveData (800 次/天) + Finnhub (60 次/分钟)")
    print(f"  监控股票：{len(WATCHLIST)} 只")
    print(f"  查询间隔：{QUERY_INTERVAL} 秒")
    print(f"  止损：-{STOP_LOSS_RATIO*100:.0f}%, 止盈：+{TAKE_PROFIT_1_RATIO*100:.0f}%/+{TAKE_PROFIT_2_RATIO*100:.0f}%/+{TAKE_PROFIT_3_RATIO*100:.0f}%")
    print(f"  补仓：下跌{ADD_POSITION_RATIO*100:.0f}%补仓，最多{MAX_ADD_POSITION_TIMES}次")
    print("=" * 80)
    print()
    
    # 检查 API Key 配置
    if not TWELVEDATA_API_KEY:
        print("⚠️  TwelveData API Key 未配置")
    if not FINNHUB_API_KEY:
        print("⚠️  Finnhub API Key 未配置")
    
    if not TWELVEDATA_API_KEY and not FINNHUB_API_KEY:
        print("❌ 至少需要配置一个数据源的 API Key")
        return
    
    # 发送启动通知
    send_dingtalk_alert(
        title="🚀 FTSeek-US 监控启动",
        content=f"数据源：TwelveData+Finnhub 轮询\n监控股票：{len(WATCHLIST)}只\n查询间隔：{QUERY_INTERVAL}秒\nTwelveData 剩余：{twelvedata_limiter.max_calls}次/分钟\nFinnhub 剩余：{finnhub_limiter.max_calls}次/分钟"
    )
    
    query_count = 0
    twelvedata_usage = 0
    finnhub_usage = 0
    
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
        print(f"[{current_time}] 📡 获取行情... (TwelveData 剩余：{twelvedata_limiter.get_remaining()}次，Finnhub 剩余：{finnhub_limiter.get_remaining()}次)")
        
        for code in WATCHLIST:
            # 智能获取价格
            current_price = get_price_smart(code)
            
            if not current_price:
                print(f"  ❌ {code}: 获取失败")
                continue
            
            query_count += 1
            
            # 检查是否有持仓
            if code in POSITIONS:
                position = POSITIONS[code]
                cost_price = position["cost"]
                
                # 计算止损止盈
                levels = calculate_levels(code, current_price, cost_price)
                
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
                print(f"  💼 {code}: ${current_price:.2f} | 盈亏：{levels['pnl_percent']}")
            else:
                # 无持仓，只打印价格
                print(f"  📊 {code}: ${current_price:.2f}")
        
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
        
        send_dingtalk_alert(
            title="⏹️ FTSeek-US 监控停止",
            content="监控脚本已手动停止"
        )
