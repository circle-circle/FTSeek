"""
持仓跌幅告警模块 - FTSeek-US

功能:
- 监控持仓股票跌幅
- 跌幅>5% 预警，>8% 紧急告警
- 集成钉钉推送
- 与 realtime_monitor.py 协同工作
"""
import logging
import time
from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import dataclass
import requests

logger = logging.getLogger("ftseek.risk.position_drop_alert")
logger.propagate = False  # 防止日志重复

# 跌幅预警阈值
DROP_WARNING_THRESHOLD = -0.05   # -5% 预警
DROP_CRITICAL_THRESHOLD = -0.08  # -8% 紧急告警


@dataclass
class PositionDropAlert:
    """持仓跌幅警报"""
    code: str
    name: str
    current_price: float
    drop_percent: float
    threshold: float
    level: str  # "WARNING" or "CRITICAL"
    timestamp: float
    recommendation: str


class PositionDropMonitor:
    """
    持仓跌幅监控器
    
    监控持仓股票跌幅，及时预警
    """

    def __init__(
        self,
        futu_api=None,
        dingtalk_webhook: str = None,
        check_interval: int = 60,  # 检查间隔 (秒)
    ):
        """
        初始化持仓跌幅监控器
        
        Args:
            futu_api: 富途 API 实例
            dingtalk_webhook: 钉钉 Webhook URL
            check_interval: 检查间隔
        """
        self.futu_api = futu_api
        self.dingtalk_webhook = dingtalk_webhook
        self.check_interval = check_interval
        
        # 警报历史 (避免重复推送)
        self._alert_history: Dict[str, float] = {}  # {code: last_alert_time}
        self._alert_cooldown = 1800  # 同一股票 30 分钟内不重复推送
        
        logger.info(
            f"持仓跌幅监控器已初始化 | "
            f"预警阈值={DROP_WARNING_THRESHOLD:.0%} | "
            f"紧急阈值={DROP_CRITICAL_THRESHOLD:.0%}"
        )

    def check_positions(self, positions: List[Dict]) -> List[PositionDropAlert]:
        """
        检查持仓跌幅
        
        Args:
            positions: 持仓列表，每项包含 {code, name, current_price, cost_price}
        
        Returns:
            警报列表
        """
        alerts = []
        
        for pos in positions:
            code = pos.get("code", "")
            name = pos.get("name", "")
            current_price = pos.get("current_price", 0)
            cost_price = pos.get("cost_price", 0)
            
            if not current_price or not cost_price:
                continue
            
            # 计算跌幅 (相对于成本价)
            drop_percent = (current_price - cost_price) / cost_price - 1
            
            # 检查是否触及预警阈值
            alert = None
            if drop_percent <= DROP_CRITICAL_THRESHOLD:
                alert = PositionDropAlert(
                    code=code,
                    name=name,
                    current_price=current_price,
                    drop_percent=drop_percent,
                    threshold=DROP_CRITICAL_THRESHOLD,
                    level="CRITICAL",
                    timestamp=time.time(),
                    recommendation="建议立即评估是否止损，警惕继续下跌风险",
                )
            elif drop_percent <= DROP_WARNING_THRESHOLD:
                alert = PositionDropAlert(
                    code=code,
                    name=name,
                    current_price=current_price,
                    drop_percent=drop_percent,
                    threshold=DROP_WARNING_THRESHOLD,
                    level="WARNING",
                    timestamp=time.time(),
                    recommendation="建议关注走势，准备止损方案",
                )
            
            if alert:
                # 检查冷却时间
                last_alert = self._alert_history.get(code, 0)
                if time.time() - last_alert > self._alert_cooldown:
                    alerts.append(alert)
                    self._alert_history[code] = time.time()
                    logger.warning(
                        f"持仓跌幅警报 | {code} {name} | "
                        f"跌幅={drop_percent:.1%} | 级别={alert.level}"
                    )
        
        return alerts

    def send_alert(self, alert: PositionDropAlert):
        """
        发送跌幅警报到钉钉
        
        Args:
            alert: 跌幅警报对象
        """
        if not self.dingtalk_webhook:
            logger.warning("未配置钉钉 Webhook，跳过推送")
            return
        
        # 构建 Markdown 消息
        if alert.level == "CRITICAL":
            title = "🚨 持仓紧急警报"
            emoji = "🔴"
        else:
            title = "⚠️ 持仓预警"
            emoji = "🟡"
        
        markdown = f"""## {title}

{emoji} **股票**: {alert.name} ({alert.code})
📉 **当前价**: ${alert.current_price:.2f}
📊 **跌幅**: {alert.drop_percent:.1%}
⚡ **阈值**: {alert.threshold:.0%}

**建议**: {alert.recommendation}

---
*FTSeek-US 风险监控系统*
*{datetime.fromtimestamp(alert.timestamp).strftime('%Y-%m-%d %H:%M:%S')}*
"""
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": markdown,
            },
        }
        
        try:
            response = requests.post(
                self.dingtalk_webhook,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            response.raise_for_status()
            logger.info(f"持仓跌幅警报已推送 | {alert.code} | 级别={alert.level}")
        except Exception as e:
            logger.error(f"推送持仓跌幅警报失败：{e}")


def integrate_with_risk_monitor():
    """
    集成到 risk_monitor.py 的示例代码
    
    在 risk_monitor.py 中添加以下代码:
    
    ```python
    from app.risk.position_drop_alert import PositionDropMonitor
    
    # 初始化
    self.position_drop_monitor = PositionDropMonitor(
        futu_api=self.futu_api,
        dingtalk_webhook=config.DINGTALK_WEBHOOK_URL,
    )
    
    # 在 _check_all_risks() 中调用
    def _check_all_risks(self):
        # ... 现有代码 ...
        
        # 添加持仓跌幅检查
        positions = self._get_positions()  # 获取持仓列表
        alerts = self.position_drop_monitor.check_positions(positions)
        for alert in alerts:
            self.position_drop_monitor.send_alert(alert)
    ```
    """
    pass


if __name__ == "__main__":
    # 测试
    logging.basicConfig(level=logging.INFO)
    
    # 模拟持仓数据
    test_positions = [
        {"code": "HK.00981", "name": "中芯国际", "current_price": 51.0, "cost_price": 54.0},
        {"code": "HK.03690", "name": "美团-W", "current_price": 78.0, "cost_price": 85.0},
    ]
    
    monitor = PositionDropMonitor(dingtalk_webhook="")
    alerts = monitor.check_positions(test_positions)
    
    for alert in alerts:
        print(f"警报：{alert.code} 跌幅={alert.drop_percent:.1%} 级别={alert.level}")
