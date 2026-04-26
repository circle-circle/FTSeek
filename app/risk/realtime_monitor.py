"""
实时风险监控模块 (Real-time Risk Monitor)

功能：
- 实时仓位监控
- 止损预警
- 风险指标实时计算
- 异常报警
- 风险仪表盘
"""
import logging
import time
import threading
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
from collections import deque
import json

logger = logging.getLogger("ftseek.risk.realtime_monitor")
logger.propagate = False  # 防止日志重复


@dataclass
class RiskAlert:
    """风险警报"""
    level: str  # "INFO", "WARNING", "CRITICAL"
    type: str   # 警报类型
    message: str
    code: Optional[str] = None
    value: Optional[float] = None
    threshold: Optional[float] = None
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "type": self.type,
            "message": self.message,
            "code": self.code,
            "value": self.value,
            "threshold": self.threshold,
            "timestamp": datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S"),
        }


@dataclass
class PositionRisk:
    """持仓风险指标"""
    code: str
    quantity: int
    entry_price: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_percent: float
    stop_loss_price: float
    stop_loss_distance: float  # 距离止损的百分比
    risk_level: str  # "LOW", "MEDIUM", "HIGH"
    days_held: int


class RealtimeRiskMonitor:
    """
    实时风险监控器
    
    监控内容：
    1. 仓位风险
    2. 止损预警
    3. 回撤监控
    4. 集中度风险
    5. 波动率风险
    """

    def __init__(
        self,
        risk_manager,
        alert_callbacks: List[Callable] = None,
        check_interval: int = 5,  # 检查间隔 (秒)
    ):
        """
        初始化实时监控器
        
        Args:
            risk_manager: 风险管理器实例
            alert_callbacks: 警报回调函数列表
            check_interval: 检查间隔
        """
        self.risk_manager = risk_manager
        self.alert_callbacks = alert_callbacks or []
        self.check_interval = check_interval
        
        # 监控状态
        self._running = False
        self._monitor_thread = None
        self._alerts: deque = deque(maxlen=100)  # 最近 100 条警报
        self._position_risks: Dict[str, PositionRisk] = {}
        
        # 预警阈值
        self.stop_loss_warning_threshold = 0.02  # 距离止损 2% 时预警
        self.large_loss_threshold = -0.05  # 亏损 5% 预警
        self.concentration_threshold = 0.30  # 集中度 30% 预警
        self.drawdown_warning_threshold = 0.10  # 回撤 10% 预警
        
        logger.info(
            f"实时风险监控器已初始化 | "
            f"检查间隔={check_interval}s | "
            f"止损预警阈值={self.stop_loss_warning_threshold:.1%}"
        )

    def start(self):
        """启动监控线程"""
        if self._running:
            logger.warning("监控器已在运行")
            return
        
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="RiskMonitor"
        )
        self._monitor_thread.start()
        logger.info("实时风险监控已启动")

    def stop(self):
        """停止监控"""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        logger.info("实时风险监控已停止")

    def _monitor_loop(self):
        """监控主循环"""
        while self._running:
            try:
                self._check_all_risks()
                time.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"监控循环错误：{e}", exc_info=True)

    def _check_all_risks(self):
        """检查所有风险"""
        # 1. 检查持仓风险
        self._check_position_risks()
        
        # 2. 检查总体风险
        self._check_portfolio_risks()
        
        # 3. 检查市场风险
        self._check_market_risks()

    def _check_position_risks(self):
        """检查持仓风险"""
        positions = self.risk_manager.account.get("positions", {})
        for code, position in positions.items():
            current_price = position["current_price"]
            entry_price = position["entry_price"]
            stop_loss = position["stop_loss_price"]
            direction = position["direction"]
            
            # 计算盈亏
            if direction == "BUY":
                pnl_percent = (current_price - entry_price) / entry_price
                stop_distance = (current_price - stop_loss) / current_price
            else:
                pnl_percent = (entry_price - current_price) / entry_price
                stop_distance = (stop_loss - current_price) / current_price
            
            # 判断风险等级
            risk_level = "LOW"
            if stop_distance <= self.stop_loss_warning_threshold:
                risk_level = "HIGH"
                self._send_alert(
                    level="CRITICAL",
                    type="STOP_LOSS_WARNING",
                    message=f"{code} 接近止损！",
                    code=code,
                    value=stop_distance,
                    threshold=self.stop_loss_warning_threshold,
                )
            elif stop_distance <= self.stop_loss_warning_threshold * 2:
                risk_level = "MEDIUM"
                self._send_alert(
                    level="WARNING",
                    type="STOP_LOSS_APPROACHING",
                    message=f"{code} 距离止损较近 ({stop_distance:.1%})",
                    code=code,
                    value=stop_distance,
                    threshold=self.stop_loss_warning_threshold * 2,
                )
            
            # 大额亏损预警
            if pnl_percent < self.large_loss_threshold:
                self._send_alert(
                    level="WARNING",
                    type="LARGE_LOSS",
                    message=f"{code} 亏损 {pnl_percent:.1%}",
                    code=code,
                    value=pnl_percent,
                    threshold=self.large_loss_threshold,
                )
            
            # 时间止损检查 (Phase 2 新增)
            # 如果持仓超过 45 分钟且利润微薄 (< 0.5%)，触发时间止损
            # 注意：realtime_monitor 是分钟级/秒级循环，这里需要 access to entry_time
            entry_time_str = position.get('entry_time')
            if entry_time_str:
                try:
                    from datetime import datetime
                    entry_time = datetime.fromisoformat(entry_time_str) if isinstance(entry_time_str, str) else entry_time
                    minutes_held = (datetime.now() - entry_time).total_seconds() / 60
                    
                    # 45 分钟时间止损逻辑
                    if minutes_held > 45 and pnl_percent < 0.005:
                        self._send_alert(
                            level="CRITICAL",
                            type="TIME_STOP_TRIGGERED",
                            message=f"{code} 时间止损！持仓 {int(minutes_held)} 分钟，利润 {pnl_percent:.2%}",
                            code=code,
                            value=minutes_held,
                            threshold=45,
                        )
                        # 这里应该触发平仓逻辑，通常需要回调 risk_manager
                        # 暂时仅记录告警，具体平仓由外部循环处理或在此处调用 risk_manager
                except Exception as e:
                    logger.error(f"[实时监控] {code} 风险评估异常: {e}")
            
            # 更新持仓风险
            market_value = position["quantity"] * current_price
            self._position_risks[code] = PositionRisk(
                code=code,
                quantity=position["quantity"],
                entry_price=entry_price,
                current_price=current_price,
                market_value=market_value,
                unrealized_pnl=pnl_percent * market_value,
                unrealized_pnl_percent=pnl_percent,
                stop_loss_price=stop_loss,
                stop_loss_distance=stop_distance,
                risk_level=risk_level,
                days_held=self._calculate_days_held(position.get('entry_time')),
            )

    def _calculate_days_held(self, entry_time_str: str) -> int:
        """计算持仓天数"""
        try:
            from datetime import datetime
            entry_time = datetime.fromisoformat(entry_time_str) if isinstance(entry_time_str, str) else entry_time
            return (datetime.now() - entry_time).days
        except Exception:
            return 1

    def _check_portfolio_risks(self):
        """检查组合风险"""
        metrics = self.risk_manager.get_risk_metrics()
        
        # 1. 回撤预警
        if metrics.get("current_drawdown", 0) >= self.drawdown_warning_threshold:
            self._send_alert(
                level="CRITICAL",
                type="DRAWDOWN_WARNING",
                message=f"当前回撤 {metrics['current_drawdown']:.1%} 超过预警线",
                value=metrics['current_drawdown'],
                threshold=self.drawdown_warning_threshold,
            )
        
        # 2. 集中度风险
        if self._position_risks:
            total_value = sum(p.market_value for p in self._position_risks.values())
            if total_value > 0:
                max_position = max(p.market_value for p in self._position_risks.values())
                concentration = max_position / total_value
                
                if concentration >= self.concentration_threshold:
                    self._send_alert(
                        level="WARNING",
                        type="HIGH_CONCENTRATION",
                        message=f"持仓集中度过高 ({concentration:.1%})",
                        value=concentration,
                        threshold=self.concentration_threshold,
                    )
        
        # 3. 总仓位预警
        if metrics.get("total_exposure", 0) >= 0.90:
            self._send_alert(
                level="WARNING",
                type="HIGH_EXPOSURE",
                message=f"总仓位过高 ({metrics.get('total_exposure', 0):.1%})",
                value=metrics.get('total_exposure', 0),
                threshold=0.90,
            )

    def _check_market_risks(self):
        """检查市场风险"""
        # P1 修复 (2026-04-12): 集成市场状态识别
        try:
            from app.strategy.market_regime import MarketRegimeDetector
            regime_detector = MarketRegimeDetector()
            regime = regime_detector.detect()

            if regime == 'high_volatility':
                self._send_alert(
                    level="WARNING",
                    type="HIGH_VOLATILITY",
                    message="市场状态：高波动，建议降低仓位",
                    value=regime,
                    threshold=0,
                )
        except Exception as e:
            logger.debug(f"市场状态检查失败：{e}")

    def _send_alert(
        self,
        level: str,
        type: str,
        message: str,
        code: Optional[str] = None,
        value: Optional[float] = None,
        threshold: Optional[float] = None,
    ):
        """发送警报"""
        alert = RiskAlert(
            level=level,
            type=type,
            message=message,
            code=code,
            value=value,
            threshold=threshold,
        )
        
        self._alerts.append(alert)
        
        # 调用回调函数（包括钉钉通知）
        for callback in self.alert_callbacks:
            try:
                callback(alert)
            except Exception as e:
                logger.error(f"警报回调错误：{e}")
        
        # 记录日志
        if level == "CRITICAL":
            logger.critical(f"🚨 {message}")
        elif level == "WARNING":
            logger.warning(f"⚠️  {message}")
        else:
            logger.info(f"ℹ️  {message}")

    def get_position_risks(self) -> Dict[str, dict]:
        """获取持仓风险"""
        return {
            code: {
                "code": risk.code,
                "market_value": risk.market_value,
                "unrealized_pnl": risk.unrealized_pnl,
                "unrealized_pnl_percent": risk.unrealized_pnl_percent,
                "stop_loss_distance": risk.stop_loss_distance,
                "risk_level": risk.risk_level,
            }
            for code, risk in self._position_risks.items()
        }

    def get_alerts(self, limit: int = 20) -> List[dict]:
        """获取最近警报"""
        alerts = list(self._alerts)[-limit:]
        return [alert.to_dict() for alert in alerts]

    def get_risk_dashboard(self) -> dict:
        """获取风险仪表盘数据"""
        metrics = self.risk_manager.get_risk_metrics()
        
        # 风险评分 (0-100, 越高风险越大)
        risk_score = 0
        
        # 回撤风险 (0-40 分)
        risk_score += min(40, metrics.get("current_drawdown", 0) * 400)
        
        # 仓位风险 (0-20 分)
        risk_score += min(20, metrics.get("total_exposure", 0) * 20)
        
        # 集中度风险 (0-20 分)
        if self._position_risks:
            total_value = sum(p.market_value for p in self._position_risks.values())
            if total_value > 0:
                max_position = max(p.market_value for p in self._position_risks.values())
                concentration = max_position / total_value
                risk_score += min(20, concentration * 20)
        
        # 持仓风险 (0-20 分)
        high_risk_positions = sum(
            1 for p in self._position_risks.values() if p.risk_level == "HIGH"
        )
        risk_score += min(20, high_risk_positions * 10)
        
        # 风险等级
        if risk_score >= 80:
            risk_level = "CRITICAL"
        elif risk_score >= 60:
            risk_level = "HIGH"
        elif risk_score >= 40:
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"
        
        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "risk_score": round(risk_score, 1),
            "risk_level": risk_level,
            "metrics": metrics,
            "positions": self.get_position_risks(),
            "recent_alerts": self.get_alerts(5),
            "summary": {
                "total_positions": len(self._position_risks),
                "high_risk_positions": sum(
                    1 for p in self._position_risks.values() if p.risk_level == "HIGH"
                ),
                "total_alerts_today": len([
                    a for a in self._alerts
                    if time.time() - a.timestamp < 86400
                ]),
            },
        }

    def export_report(self) -> str:
        """导出风险报告"""
        dashboard = self.get_risk_dashboard()
        
        lines = [
            "=" * 80,
            "  FTSeek 实时风险监控报告",
            "=" * 80,
            f"时间：{dashboard['timestamp']}",
            f"风险评分：{dashboard['risk_score']} / 100 ({dashboard['risk_level']})",
            "",
            "核心指标:",
            f"  总资金：¥{dashboard['metrics'].get('total_capital', 0):,.0f}",
            f"  总仓位：{dashboard['metrics'].get('total_exposure', '0%')}",
            f"  当前回撤：{dashboard['metrics'].get('current_drawdown', '0%')}",
            f"  持仓数：{dashboard['summary']['total_positions']}",
            f"  高风险持仓：{dashboard['summary']['high_risk_positions']}",
            "",
        ]
        
        # 持仓详情
        if dashboard['positions']:
            lines.append("持仓风险:")
            lines.append("-" * 80)
            for code, risk in dashboard['positions'].items():
                lines.append(
                    f"  {code}: "
                    f"盈亏={risk['unrealized_pnl_percent']:.1%} | "
                    f"止损距离={risk['stop_loss_distance']:.1%} | "
                    f"风险={risk['risk_level']}"
                )
            lines.append("")
        
        # 最近警报
        if dashboard['recent_alerts']:
            lines.append("最近警报:")
            lines.append("-" * 80)
            for alert in dashboard['recent_alerts']:
                lines.append(
                    f"  [{alert['level']}] {alert['timestamp']} - {alert['message']}"
                )
            lines.append("")
        
        lines.append("=" * 80)
        
        return "\n".join(lines)


# 全局单例
realtime_monitor: Optional[RealtimeRiskMonitor] = None


def init_realtime_monitor(risk_manager, alert_callbacks=None):
    """初始化全局监控器"""
    global realtime_monitor
    realtime_monitor = RealtimeRiskMonitor(risk_manager, alert_callbacks)
    return realtime_monitor
