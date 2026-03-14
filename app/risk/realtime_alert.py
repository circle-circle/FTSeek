"""
实时风险预警模块

实时监控风险指标，提前预警
"""
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("ftseek.risk.realtime_alert")


@dataclass
class RiskAlert:
    """风险预警"""
    alert_id: str
    alert_type: str
    level: str  # warning/critical
    message: str
    timestamp: str
    current_value: float
    threshold: float
    suggested_action: str


class RealtimeRiskMonitor:
    """实时风险监控器"""
    
    def __init__(self):
        # 预警配置
        self.alert_config = {
            'consecutive_loss': {
                'warning': 3,
                'critical': 5,
                'action': 'reduce_position'
            },
            'daily_loss': {
                'warning': 0.03,
                'critical': 0.05,
                'action': 'stop_trading'
            },
            'drawdown': {
                'warning': 0.10,
                'critical': 0.15,
                'action': 'liquidate'
            },
            'ai_accuracy': {
                'warning': 0.45,
                'critical': 0.35,
                'action': 'disable_ai'
            }
        }
        
        # 当前状态
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.daily_pnl_percent = 0.0
        self.peak_capital = 1000000.0
        self.current_capital = 1000000.0
        self.ai_predictions = []
        self.alerts: List[RiskAlert] = []
    
    def update_trade_result(self, pnl: float, pnl_percent: float):
        """更新交易结果"""
        import uuid
        
        # 更新连续亏损
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        
        # 更新日盈亏
        self.daily_pnl += pnl
        self.daily_pnl_percent += pnl_percent
        
        # 更新资金
        self.current_capital += pnl
        
        # 检查预警
        self._check_alerts()
    
    def update_ai_prediction_result(self, is_correct: bool):
        """更新 AI 预测结果"""
        self.ai_predictions.append(is_correct)
        
        # 只保留最近 20 次预测
        if len(self.ai_predictions) > 20:
            self.ai_predictions.pop(0)
        
        # 检查 AI 准确率预警
        self._check_ai_accuracy()
    
    def _check_alerts(self):
        """检查各项风险指标"""
        import uuid
        
        # 1. 连续亏损检查
        config = self.alert_config['consecutive_loss']
        if self.consecutive_losses >= config['critical']:
            self._create_alert(
                alert_type='consecutive_loss',
                level='critical',
                message=f'连续亏损 {self.consecutive_losses} 笔，达到临界值',
                current_value=self.consecutive_losses,
                threshold=config['critical'],
                action=config['action']
            )
        elif self.consecutive_losses >= config['warning']:
            self._create_alert(
                alert_type='consecutive_loss',
                level='warning',
                message=f'连续亏损 {self.consecutive_losses} 笔',
                current_value=self.consecutive_losses,
                threshold=config['warning'],
                action=config['action']
            )
        
        # 2. 日盈亏检查
        config = self.alert_config['daily_loss']
        if self.daily_pnl_percent <= -config['critical']:
            self._create_alert(
                alert_type='daily_loss',
                level='critical',
                message=f'日亏损 {abs(self.daily_pnl_percent):.2%}，达到临界值',
                current_value=abs(self.daily_pnl_percent),
                threshold=config['critical'],
                action=config['action']
            )
        elif self.daily_pnl_percent <= -config['warning']:
            self._create_alert(
                alert_type='daily_loss',
                level='warning',
                message=f'日亏损 {abs(self.daily_pnl_percent):.2%}',
                current_value=abs(self.daily_pnl_percent),
                threshold=config['warning'],
                action=config['action']
            )
        
        # 3. 回撤检查
        if self.current_capital < self.peak_capital:
            drawdown = (self.peak_capital - self.current_capital) / self.peak_capital
            config = self.alert_config['drawdown']
            
            if drawdown >= config['critical']:
                self._create_alert(
                    alert_type='drawdown',
                    level='critical',
                    message=f'回撤 {drawdown:.2%}，达到临界值',
                    current_value=drawdown,
                    threshold=config['critical'],
                    action=config['action']
                )
            elif drawdown >= config['warning']:
                self._create_alert(
                    alert_type='drawdown',
                    level='warning',
                    message=f'回撤 {drawdown:.2%}',
                    current_value=drawdown,
                    threshold=config['warning'],
                    action=config['action']
                )
        else:
            self.peak_capital = self.current_capital
    
    def _check_ai_accuracy(self):
        """检查 AI 准确率"""
        if len(self.ai_predictions) < 10:
            return  # 样本不足
        
        accuracy = sum(self.ai_predictions) / len(self.ai_predictions)
        import uuid
        
        config = self.alert_config['ai_accuracy']
        if accuracy <= config['critical']:
            self._create_alert(
                alert_type='ai_accuracy',
                level='critical',
                message=f'AI 准确率 {accuracy:.1%}，低于临界值',
                current_value=accuracy,
                threshold=config['critical'],
                action=config['action']
            )
        elif accuracy <= config['warning']:
            self._create_alert(
                alert_type='ai_accuracy',
                level='warning',
                message=f'AI 准确率 {accuracy:.1%}，偏低',
                current_value=accuracy,
                threshold=config['warning'],
                action=config['action']
            )
    
    def _create_alert(self,
                     alert_type: str,
                     level: str,
                     message: str,
                     current_value: float,
                     threshold: float,
                     action: str):
        """创建预警"""
        import uuid
        
        # 检查是否已有相同类型的预警
        existing_alerts = [a for a in self.alerts if a.alert_type == alert_type and a.level == level]
        if existing_alerts:
            return  # 避免重复预警
        
        alert = RiskAlert(
            alert_id=str(uuid.uuid4())[:8],
            alert_type=alert_type,
            level=level,
            message=message,
            timestamp=datetime.now().isoformat(),
            current_value=current_value,
            threshold=threshold,
            suggested_action=action
        )
        
        self.alerts.append(alert)
        
        if level == 'critical':
            logger.critical(f"🚨 风险预警 [{alert_type}]: {message}")
        else:
            logger.warning(f"⚠️ 风险预警 [{alert_type}]: {message}")
    
    def get_current_status(self) -> Dict:
        """获取当前风险状态"""
        ai_accuracy = None
        if len(self.ai_predictions) >= 10:
            ai_accuracy = sum(self.ai_predictions) / len(self.ai_predictions)
        
        drawdown = (self.peak_capital - self.current_capital) / self.peak_capital
        
        return {
            'consecutive_losses': self.consecutive_losses,
            'daily_pnl': self.daily_pnl,
            'daily_pnl_percent': self.daily_pnl_percent,
            'current_capital': self.current_capital,
            'peak_capital': self.peak_capital,
            'drawdown': drawdown,
            'ai_accuracy': ai_accuracy,
            'active_alerts': len([a for a in self.alerts if a.level == 'critical'])
        }
    
    def get_alerts(self, level: str = None) -> List[RiskAlert]:
        """获取预警列表"""
        if level:
            return [a for a in self.alerts if a.level == level]
        return self.alerts
    
    def reset_daily_stats(self):
        """重置日统计"""
        self.daily_pnl = 0.0
        self.daily_pnl_percent = 0.0
        self.alerts = []
        logger.info("日统计已重置")
    
    def execute_action(self, action: str) -> Dict:
        """执行预警建议操作"""
        actions = {
            'reduce_position': {
                'message': '建议降低仓位至 5%',
                'recommended_position': 0.05
            },
            'stop_trading': {
                'message': '建议停止今日交易',
                'recommended_position': 0.0
            },
            'liquidate': {
                'message': '建议清仓',
                'recommended_position': 0.0
            },
            'disable_ai': {
                'message': '建议暂停使用 AI 预测',
                'recommended_ai_weight': 0.0
            }
        }
        
        return actions.get(action, {'message': '未知操作', 'recommended_position': None})


# 全局单例
_monitor = None

def get_realtime_risk_monitor() -> RealtimeRiskMonitor:
    """获取实时风险监控器单例"""
    global _monitor
    if _monitor is None:
        _monitor = RealtimeRiskMonitor()
    return _monitor
