"""
期权风险监控配置

定义期权交易的风险参数和阈值：
- 保证金充足性检查
- 期权价值止损 (-50% 权利金)
- 到期日监控 (DTE≥7 天)
- Greeks 风险监控
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any


@dataclass
class OptionRiskConfig:
    """期权风险配置
    
    P0 修复 (2026-05-01): max_position_cost/max_total_options_cost 已废弃，
    使用 max_position_pct 和 max_total_options_pct 替代，自动适配不同资金量。
    绝对值字段仅保留向下兼容。
    """
    
    # ========== 仓位限制 (百分比优先) ==========
    max_position_cost: float = 5000.0      # [已废弃] 向下兼容，使用 max_position_pct
    max_total_options_cost: float = 20000.0  # [已废弃] 向下兼容，使用 max_total_options_pct
    max_position_pct: float = 0.10         # 单张期权占账户比例上限 (10%)
    max_total_options_pct: float = 0.20    # P0 新增: 期权总成本占账户比例上限 (20%)
    
    # ========== 止损规则 ==========
    stop_loss_premium_pct: float = 0.50    # 期权价值止损 (-50% 权利金)
    stop_loss_type: str = "premium"        # 止损类型：premium=权利金，price=标的价格
    
    # ========== 到期日监控 ==========
    min_dte: int = 7                       # 最小剩余到期天数
    max_dte: int = 30                      # 最大剩余到期天数
    dte_alert_threshold: int = 5           # DTE 告警阈值 (剩余≤5 天告警)
    
    # ========== Greeks 限制 ==========
    delta_target_min: float = 0.4          # Delta 目标下限 (绝对值)
    delta_target_max: float = 0.6          # Delta 目标上限 (绝对值)
    gamma_threshold: float = 0.05          # Gamma 阈值 (超过则告警)
    theta_decay_limit: float = 0.50        # Theta 每日衰减上限 (美元)
    vega_exposure_limit: float = 0.30      # Vega 敞口限制
    
    # ========== 保证金检查 ==========
    margin_check_enabled: bool = True      # 启用保证金检查
    margin_buffer_pct: float = 0.20        # 保证金缓冲比例 (20%)
    
    # ========== 流动性检查 ==========
    min_volume: int = 100                  # 最小成交量
    min_open_interest: int = 500           # 最小未平仓合约数
    max_bid_ask_spread_pct: float = 0.05   # 最大买卖价差 (5%)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "max_position_cost": self.max_position_cost,
            "max_total_options_cost": self.max_total_options_cost,
            "max_position_pct": self.max_position_pct,
            "stop_loss_premium_pct": self.stop_loss_premium_pct,
            "stop_loss_type": self.stop_loss_type,
            "min_dte": self.min_dte,
            "max_dte": self.max_dte,
            "dte_alert_threshold": self.dte_alert_threshold,
            "delta_target_min": self.delta_target_min,
            "delta_target_max": self.delta_target_max,
            "gamma_threshold": self.gamma_threshold,
            "theta_decay_limit": self.theta_decay_limit,
            "vega_exposure_limit": self.vega_exposure_limit,
            "margin_check_enabled": self.margin_check_enabled,
            "margin_buffer_pct": self.margin_buffer_pct,
            "min_volume": self.min_volume,
            "min_open_interest": self.min_open_interest,
            "max_bid_ask_spread_pct": self.max_bid_ask_spread_pct,
        }


# 默认配置
DEFAULT_OPTION_RISK_CONFIG = OptionRiskConfig()


class OptionRiskMonitor:
    """
    期权风险监控器
    
    功能：
    - 保证金充足性检查
    - 期权价值止损监控
    - 到期日监控
    - Greeks 风险监控
    """
    
    def __init__(self, config: OptionRiskConfig = None):
        self.config = config or DEFAULT_OPTION_RISK_CONFIG
        self.alerts: List[Dict[str, Any]] = []
    
    def check_margin_sufficiency(
        self,
        option_cost: float,
        account_balance: float,
    ) -> Dict[str, Any]:
        """
        保证金充足性检查
        
        期权买方最大损失 = 权利金，无需额外保证金
        但需验证账户余额充足性
        
        Args:
            option_cost: 期权成本 (权利金)
            account_balance: 账户余额
        
        Returns:
            检查结果字典
        """
        required_balance = option_cost * (1 + self.config.margin_buffer_pct)
        is_sufficient = account_balance >= required_balance
        
        result = {
            "check": "margin_sufficiency",
            "passed": is_sufficient,
            "option_cost": option_cost,
            "required_balance": required_balance,
            "account_balance": account_balance,
            "buffer_pct": self.config.margin_buffer_pct,
        }
        
        if not is_sufficient:
            self.alerts.append({
                "type": "MARGIN_INSUFFICIENT",
                "level": "CRITICAL",
                "message": f"账户余额不足：需要${required_balance:.2f}, 现有${account_balance:.2f}",
                "data": result,
            })
        
        return result
    
    def check_stop_loss(
        self,
        entry_premium: float,
        current_premium: float,
    ) -> Dict[str, Any]:
        """
        期权价值止损检查 (-50% 权利金)
        
        Args:
            entry_premium: 开仓权利金
            current_premium: 当前权利金
        
        Returns:
            检查结果字典
        """
        loss_pct = (entry_premium - current_premium) / entry_premium
        stop_loss_threshold = self.config.stop_loss_premium_pct
        
        should_stop = loss_pct >= stop_loss_threshold
        
        result = {
            "check": "stop_loss",
            "triggered": should_stop,
            "entry_premium": entry_premium,
            "current_premium": current_premium,
            "loss_pct": loss_pct,
            "stop_loss_threshold": stop_loss_threshold,
        }
        
        if should_stop:
            self.alerts.append({
                "type": "STOP_LOSS_TRIGGERED",
                "level": "CRITICAL",
                "message": f"期权价值止损触发：亏损{loss_pct:.1%} (阈值{stop_loss_threshold:.0%})",
                "data": result,
            })
        
        return result
    
    def check_dte(
        self,
        expiry_date: str,
        current_date: str = None,
    ) -> Dict[str, Any]:
        """
        到期日监控 (DTE≥7 天)
        
        Args:
            expiry_date: 到期日 (YYYY-MM-DD)
            current_date: 当前日期 (YYYY-MM-DD), 默认今天
        
        Returns:
            检查结果字典
        """
        from datetime import datetime, date
        
        if current_date is None:
            current = date.today()
        else:
            current = datetime.strptime(current_date, "%Y-%m-%d").date()
        
        expiry = datetime.strptime(expiry_date, "%Y-%m-%d").date()
        dte = (expiry - current).days
        
        is_valid = dte >= self.config.min_dte
        is_alert = dte <= self.config.dte_alert_threshold
        
        result = {
            "check": "dte",
            "passed": is_valid,
            "dte": dte,
            "min_required": self.config.min_dte,
            "alert_threshold": self.config.dte_alert_threshold,
            "is_alert": is_alert,
        }
        
        if not is_valid:
            self.alerts.append({
                "type": "DTE_TOO_LOW",
                "level": "WARNING",
                "message": f"到期日过近：剩余{dte}天 (要求≥{self.config.min_dte}天)",
                "data": result,
            })
        elif is_alert:
            self.alerts.append({
                "type": "DTE_ALERT",
                "level": "INFO",
                "message": f"到期日提醒：剩余{dte}天",
                "data": result,
            })
        
        return result
    
    def check_greeks_risk(
        self,
        delta: float,
        gamma: float,
        theta: float,
        vega: float,
    ) -> Dict[str, Any]:
        """
        Greeks 风险监控
        
        Args:
            delta: Delta 值
            gamma: Gamma 值
            theta: Theta 值
            vega: Vega 值
        
        Returns:
            检查结果字典
        """
        delta_abs = abs(delta)
        delta_in_range = (
            self.config.delta_target_min <= delta_abs <= self.config.delta_target_max
        )
        
        gamma_ok = abs(gamma) <= self.config.gamma_threshold
        theta_ok = abs(theta) <= self.config.theta_decay_limit
        vega_ok = abs(vega) <= self.config.vega_exposure_limit
        
        all_ok = delta_in_range and gamma_ok and theta_ok and vega_ok
        
        result = {
            "check": "greeks_risk",
            "passed": all_ok,
            "delta": {
                "value": delta,
                "in_range": delta_in_range,
                "target_range": [self.config.delta_target_min, self.config.delta_target_max],
            },
            "gamma": {
                "value": gamma,
                "ok": gamma_ok,
                "threshold": self.config.gamma_threshold,
            },
            "theta": {
                "value": theta,
                "ok": theta_ok,
                "limit": self.config.theta_decay_limit,
            },
            "vega": {
                "value": vega,
                "ok": vega_ok,
                "limit": self.config.vega_exposure_limit,
            },
        }
        
        if not all_ok:
            issues = []
            if not delta_in_range:
                issues.append(f"Delta={delta:.4f} 超出目标范围")
            if not gamma_ok:
                issues.append(f"Gamma={gamma:.6f} 超过阈值")
            if not theta_ok:
                issues.append(f"Theta={theta:.4f} 衰减过快")
            if not vega_ok:
                issues.append(f"Vega={vega:.4f} 敞口过大")
            
            self.alerts.append({
                "type": "GREEKS_RISK",
                "level": "WARNING",
                "message": "; ".join(issues),
                "data": result,
            })
        
        return result
    
    def check_liquidity(
        self,
        volume: int,
        open_interest: int,
        bid: float,
        ask: float,
    ) -> Dict[str, Any]:
        """
        流动性检查
        
        Args:
            volume: 成交量
            open_interest: 未平仓合约数
            bid: 买一价
            ask: 卖一价
        
        Returns:
            检查结果字典
        """
        volume_ok = volume >= self.config.min_volume
        oi_ok = open_interest >= self.config.min_open_interest
        
        spread_pct = (ask - bid) / ((bid + ask) / 2) if bid > 0 and ask > 0 else 1.0
        spread_ok = spread_pct <= self.config.max_bid_ask_spread_pct
        
        all_ok = volume_ok and oi_ok and spread_ok
        
        result = {
            "check": "liquidity",
            "passed": all_ok,
            "volume": {
                "value": volume,
                "ok": volume_ok,
                "min_required": self.config.min_volume,
            },
            "open_interest": {
                "value": open_interest,
                "ok": oi_ok,
                "min_required": self.config.min_open_interest,
            },
            "bid_ask_spread": {
                "value": spread_pct,
                "ok": spread_ok,
                "max_allowed": self.config.max_bid_ask_spread_pct,
            },
        }
        
        if not all_ok:
            issues = []
            if not volume_ok:
                issues.append(f"成交量={volume} 不足")
            if not oi_ok:
                issues.append(f"未平仓={open_interest} 不足")
            if not spread_ok:
                issues.append(f"买卖价差={spread_pct:.1%} 过大")
            
            self.alerts.append({
                "type": "LOW_LIQUIDITY",
                "level": "WARNING",
                "message": "; ".join(issues),
                "data": result,
            })
        
        return result
    
    def get_alerts(self) -> List[Dict[str, Any]]:
        """获取所有告警"""
        return self.alerts
    
    def clear_alerts(self):
        """清除告警"""
        self.alerts = []


# =================================================================
# 使用示例
# =================================================================

if __name__ == "__main__":
    # 创建监控器
    monitor = OptionRiskMonitor()
    
    # 示例 1: 保证金检查
    print("示例 1: 保证金充足性检查")
    result = monitor.check_margin_sufficiency(
        option_cost=1500.0,
        account_balance=50000.0,
    )
    print(f"  结果：{'通过' if result['passed'] else '失败'}")
    print(f"  期权成本：${result['option_cost']:.2f}")
    print(f"  所需余额：${result['required_balance']:.2f}")
    print()
    
    # 示例 2: 止损检查
    print("示例 2: 期权价值止损检查")
    result = monitor.check_stop_loss(
        entry_premium=15.0,
        current_premium=7.0,
    )
    print(f"  结果：{'触发止损' if result['triggered'] else '未触发'}")
    print(f"  亏损比例：{result['loss_pct']:.1%}")
    print()
    
    # 示例 3: DTE 检查
    print("示例 3: 到期日监控")
    from datetime import date, timedelta
    expiry = (date.today() + timedelta(days=16)).strftime("%Y-%m-%d")
    result = monitor.check_dte(expiry_date=expiry)
    print(f"  结果：{'通过' if result['passed'] else '失败'}")
    print(f"  剩余天数：{result['dte']}天")
    print()
    
    # 示例 4: Greeks 检查
    print("示例 4: Greeks 风险监控")
    result = monitor.check_greeks_risk(
        delta=-0.5,
        gamma=0.01,
        theta=-0.3,
        vega=0.25,
    )
    print(f"  结果：{'通过' if result['passed'] else '失败'}")
    print(f"  Delta: {result['delta']['value']:.4f} (在范围内：{result['delta']['in_range']})")
    print()
    
    # 显示告警
    alerts = monitor.get_alerts()
    if alerts:
        print(f"\n告警 ({len(alerts)}条):")
        for alert in alerts:
            print(f"  [{alert['level']}] {alert['type']}: {alert['message']}")
