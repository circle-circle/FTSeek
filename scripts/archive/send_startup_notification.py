#!/usr/bin/env python3
"""
FTSeek-US 模拟盘启动通知 - 飞书推送
"""
import requests
import json
from datetime import datetime

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/de20f742-1e2b-414c-8874-6473f48eca82"

def send_feishu_message(text):
    """发送消息到飞书"""
    payload = {
        "msg_type": "text",
        "content": {
            "text": text
        }
    }
    
    try:
        response = requests.post(
            FEISHU_WEBHOOK,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        result = response.json()
        return result.get("code") == 0 or result.get("StatusCode") == 0
    except Exception as e:
        print(f"推送失败：{e}")
        return False

if __name__ == "__main__":
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    message = f"""🇺🇸 FTSeek-US 模拟盘监控已启动

🚀 启动时间：{now}
📊 策略版本：方案 B (修复后策略)
💰 交易环境：模拟盘
📈 初始持仓：TSLA 100 股 @$395.00

📋 核心参数:
- ATR 止损：2.0×动态
- 移动止损：5% 盈利激活
- 止盈档位：+15% / +25% / +40%
- 信号频率：4 小时

🔔 监控内容:
- 持仓价格监控 (每 5 分钟)
- 小时报告推送 (交易时段整点)
- VIX 风险预警 (>25 预警，>40 紧急)
- 持仓跌幅告警 (>5% 预警，>8% 紧急)

📁 日志文件:
/opt/ftseek/FTSeek-US/logs/simulated_monitor.log

📊 预期表现 (基于回测):
- 年化收益率：~9.5%
- 最大回撤：~65%
- 胜率：~53%
- 夏普比率：~0.95

⚠️ 观察期：2 周
模拟盘胜率>50% 后考虑实盘部署

---
FTSeek-US 自动化交易系统"""

    if send_feishu_message(message):
        print("✅ 飞书推送成功")
    else:
        print("❌ 飞书推送失败")
