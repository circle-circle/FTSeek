"""
回撤控制模块

功能:
- 实时回撤监控
- 最大回撤限制
- 自动减仓机制
- 回撤预警
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import json

logger = logging.getLogger("ftseek.risk.drawdown_control")
logger.propagate = False  # 防止日志重复


@dataclass
class DrawdownConfig:
    """回撤配置"""
    max_daily_drawdown: float = 0.03      # 单日最大回撤 3%
    max_weekly_drawdown: float = 0.05     # 单周最大回撤 5%
    max_monthly_drawdown: float = 0.10    # 单月最大回撤 10%
    max_total_drawdown: float = 0.15      # 总最大回撤 15%
    
    # 减仓触发
    reduce_position_at: float = 0.05      # 回撤 5% 时开始减仓
    reduce_position_ratio: float = 0.5    # 减仓 50%
    
    # 停止交易触发
    stop_trading_at: float = 0.10         # 回撤 10% 时停止交易
    
    # 预警阈值
    warning_threshold: float = 0.03       # 回撤 3% 时预警


@dataclass
class DrawdownState:
    """回撤状态"""
    current_drawdown: float       # 当前回撤
    max_drawdown: float           # 历史最大回撤
    peak_value: float             # 峰值资产
    current_value: float          # 当前资产
    drawdown_start: Optional[str] # 回撤开始时间
    duration_days: int            # 回撤持续天数
    status: str                   # normal/warning/reduced/stopped


class DrawdownController:
    """回撤控制器"""
    
    def __init__(self, config: DrawdownConfig = None):
        self.config = config or DrawdownConfig()
        
        # 状态
        self.peak_value = 1000000.0  # 初始资产 100 万
        self.current_value = 1000000.0
        self.max_drawdown = 0.0
        self.drawdown_start = None
        
        # 历史记录
        self.daily_values: List[Dict] = []
        self.drawdown_events: List[Dict] = []
        
        # 交易状态
        self.trading_allowed = True
        self.position_reduced = False
        
        logger.info(f"回撤控制器已初始化：{self._get_config_summary()}")
    
    def _get_config_summary(self) -> str:
        return (f"日{self.config.max_daily_drawdown*100:.0f}%/"
                f"周{self.config.max_weekly_drawdown*100:.0f}%/"
                f"月{self.config.max_monthly_drawdown*100:.0f}%/"
                f"总{self.config.max_total_drawdown*100:.0f}%")
    
    def update_value(self, current_value: float) -> DrawdownState:
        """更新资产值并计算回撤
        
        Args:
            current_value: 当前总资产
        
        Returns:
            DrawdownState 回撤状态
        """
        self.current_value = current_value
        
        # 更新峰值
        if current_value > self.peak_value:
            self.peak_value = current_value
            self.drawdown_start = None
            self.position_reduced = False
            self.trading_allowed = True
        
        # 计算回撤
        if self.peak_value > 0:
            self.current_drawdown = (self.peak_value - current_value) / self.peak_value
        else:
            self.current_drawdown = 0.0
        
        # 更新最大回撤
        if self.current_drawdown > self.max_drawdown:
            self.max_drawdown = self.current_drawdown
            if self.drawdown_start is None:
                self.drawdown_start = datetime.now().isoformat()
        
        # 计算持续天数
        duration_days = 0
        if self.drawdown_start:
            start_date = datetime.fromisoformat(self.drawdown_start)
            duration_days = (datetime.now() - start_date).days
        
        # 确定状态
        status = self._determine_status()
        
        state = DrawdownState(
            current_drawdown=self.current_drawdown,
            max_drawdown=self.max_drawdown,
            peak_value=self.peak_value,
            current_value=self.current_value,
            drawdown_start=self.drawdown_start,
            duration_days=duration_days,
            status=status,
        )
        
        # 记录历史
        self._record_daily_value()
        
        # 检查是否需要行动
        self._check_actions(state)
        
        return state
    
    def _determine_status(self) -> str:
        """确定回撤状态"""
        if self.current_drawdown >= self.config.stop_trading_at:
            return "stopped"
        elif self.current_drawdown >= self.config.reduce_position_at:
            return "reduced"
        elif self.current_drawdown >= self.config.warning_threshold:
            return "warning"
        else:
            return "normal"
    
    def _check_actions(self, state: DrawdownState):
        """检查并执行行动"""
        # 停止交易
        if state.status == "stopped":
            if self.trading_allowed:
                self.trading_allowed = False
                logger.warning(f"🛑 停止交易：回撤 {state.current_drawdown*100:.2f}% 超过限制")
                self._record_event("trading_stopped", state)
        
        # 减仓
        elif state.status == "reduced":
            if not self.position_reduced:
                self.position_reduced = True
                logger.warning(f"⚠️ 自动减仓：回撤 {state.current_drawdown*100:.2f}%")
                self._record_event("position_reduced", state)
        
        # 预警
        elif state.status == "warning":
            logger.warning(f"⚠️ 回撤预警：{state.current_drawdown*100:.2f}%")
            self._record_event("warning", state)
    
    def _record_daily_value(self):
        """记录每日资产值"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        # 检查是否已有今日记录
        if self.daily_values and self.daily_values[-1]['date'] == today:
            # 更新最新记录
            self.daily_values[-1]['value'] = self.current_value
            self.daily_values[-1]['drawdown'] = self.current_drawdown
        else:
            # 添加新记录
            self.daily_values.append({
                'date': today,
                'value': self.current_value,
                'drawdown': self.current_drawdown,
            })
        
        # 保留最近 90 天
        if len(self.daily_values) > 90:
            self.daily_values = self.daily_values[-90:]
    
    def _record_event(self, event_type: str, state: DrawdownState):
        """记录回撤事件"""
        event = {
            'timestamp': datetime.now().isoformat(),
            'type': event_type,
            'drawdown': state.current_drawdown,
            'peak_value': self.peak_value,
            'current_value': self.current_value,
        }
        self.drawdown_events.append(event)
        
        # 保留最近 50 个事件
        if len(self.drawdown_events) > 50:
            self.drawdown_events = self.drawdown_events[-50:]
    
    def get_daily_drawdown(self) -> float:
        """计算单日回撤"""
        if len(self.daily_values) < 2:
            return 0.0
        
        today = self.daily_values[-1]['value']
        yesterday = self.daily_values[-2]['value']
        
        if yesterday > 0:
            return (yesterday - today) / yesterday
        return 0.0
    
    def get_weekly_drawdown(self) -> float:
        """计算单周回撤"""
        if len(self.daily_values) < 7:
            return 0.0
        
        current = self.daily_values[-1]['value']
        week_ago = self.daily_values[-7]['value']
        
        if week_ago > 0:
            return (week_ago - current) / week_ago
        return 0.0
    
    def get_monthly_drawdown(self) -> float:
        """计算单月回撤"""
        if len(self.daily_values) < 30:
            return 0.0
        
        current = self.daily_values[-1]['value']
        month_ago = self.daily_values[-30]['value']
        
        if month_ago > 0:
            return (month_ago - current) / month_ago
        return 0.0
    
    def check_limits(self) -> Dict:
        """检查回撤限制
        
        Returns:
            检查结果
        """
        daily_dd = self.get_daily_drawdown()
        weekly_dd = self.get_weekly_drawdown()
        monthly_dd = self.get_monthly_drawdown()
        
        violations = []
        
        if daily_dd > self.config.max_daily_drawdown:
            violations.append(f"单日回撤 {daily_dd*100:.2f}% > {self.config.max_daily_drawdown*100:.0f}%")
        
        if weekly_dd > self.config.max_weekly_drawdown:
            violations.append(f"单周回撤 {weekly_dd*100:.2f}% > {self.config.max_weekly_drawdown*100:.0f}%")
        
        if monthly_dd > self.config.max_monthly_drawdown:
            violations.append(f"单月回撤 {monthly_dd*100:.2f}% > {self.config.max_monthly_drawdown*100:.0f}%")
        
        if self.current_drawdown > self.config.max_total_drawdown:
            violations.append(f"总回撤 {self.current_drawdown*100:.2f}% > {self.config.max_total_drawdown*100:.0f}%")
        
        return {
            'allowed': len(violations) == 0,
            'violations': violations,
            'daily_drawdown': daily_dd,
            'weekly_drawdown': weekly_dd,
            'monthly_drawdown': monthly_dd,
            'total_drawdown': self.current_drawdown,
            'trading_allowed': self.trading_allowed,
        }
    
    def reset(self, new_peak: float = None):
        """重置回撤状态
        
        Args:
            new_peak: 新峰值 (默认使用当前值)
        """
        self.peak_value = new_peak or self.current_value
        self.current_drawdown = 0.0
        self.max_drawdown = 0.0
        self.drawdown_start = None
        self.trading_allowed = True
        self.position_reduced = False
        
        logger.info(f"回撤状态已重置：新峰值 {self.peak_value:,.0f}")
    
    def get_report(self) -> Dict:
        """生成回撤报告"""
        return {
            'current_value': self.current_value,
            'peak_value': self.peak_value,
            'current_drawdown': self.current_drawdown,
            'max_drawdown': self.max_drawdown,
            'daily_drawdown': self.get_daily_drawdown(),
            'weekly_drawdown': self.get_weekly_drawdown(),
            'monthly_drawdown': self.get_monthly_drawdown(),
            'status': self._determine_status(),
            'trading_allowed': self.trading_allowed,
            'duration_days': (datetime.now() - datetime.fromisoformat(self.drawdown_start)).days if self.drawdown_start else 0,
            'config': {
                'max_daily': self.config.max_daily_drawdown,
                'max_weekly': self.config.max_weekly_drawdown,
                'max_monthly': self.config.max_monthly_drawdown,
                'max_total': self.config.max_total_drawdown,
            },
        }
    
    def export_history(self, filepath: str):
        """导出历史数据"""
        data = {
            'exported_at': datetime.now().isoformat(),
            'peak_value': self.peak_value,
            'current_value': self.current_value,
            'max_drawdown': self.max_drawdown,
            'daily_values': self.daily_values,
            'events': self.drawdown_events,
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"回撤历史已导出：{filepath}")


# 全局单例
_drawdown_controller = None

def get_drawdown_controller(config: Dict = None) -> DrawdownController:
    """获取回撤控制器单例"""
    global _drawdown_controller
    if _drawdown_controller is None:
        if config:
            _drawdown_controller = DrawdownController(DrawdownConfig(**config))
        else:
            _drawdown_controller = DrawdownController()
    return _drawdown_controller
