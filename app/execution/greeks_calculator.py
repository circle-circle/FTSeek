"""
期权 Greeks 计算模块

提供实时 Greeks 计算和更新功能：
- Delta: 标的价格变动对期权价格的影响
- Gamma: Delta 对标的价格变动的敏感度
- Theta: 时间衰减对期权价格的影响
- Vega: 波动率变动对期权价格的影响
- Rho: 利率变动对期权价格的影响 (可选)

支持两种模式：
1. 富途实时数据模式 (通过 OpenD API 获取)
2. Black-Scholes 理论计算模式 (离线估算)
"""
import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


@dataclass
class GreeksData:
    """Greeks 数据"""
    delta: float = 0.0      # Delta
    gamma: float = 0.0      # Gamma
    theta: float = 0.0      # Theta (每日)
    vega: float = 0.0       # Vega (1% 波动率变动)
    rho: float = 0.0        # Rho (1% 利率变动)
    implied_volatility: float = 0.0  # 隐含波动率
    timestamp: str = ""     # 时间戳
    
    def to_dict(self) -> Dict[str, float]:
        """转换为字典"""
        return {
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
            "rho": self.rho,
            "implied_volatility": self.implied_volatility,
        }


class GreeksCalculator:
    """
    期权 Greeks 计算器
    
    支持：
    - 富途 OpenD 实时获取
    - Black-Scholes 理论计算
    """
    
    def __init__(self, quote_ctx=None, risk_free_rate: float = 0.05):
        """
        Args:
            quote_ctx: 富途 OpenQuoteContext，None 则使用理论计算
            risk_free_rate: 无风险利率 (默认 5%)
        """
        self.quote_ctx = quote_ctx
        self.risk_free_rate = risk_free_rate
    
    # =================================================================
    # 公开接口
    # =================================================================
    
    def get_greeks(
        self,
        option_code: str,
        underlying_price: float,
        strike: float,
        expiry_date: str,
        option_type: str = "P",  # "P"=Put, "C"=Call
        option_price: Optional[float] = None,
    ) -> GreeksData:
        """
        获取期权 Greeks
        
        优先使用富途实时数据，无连接时使用 Black-Scholes 理论计算
        
        Args:
            option_code: 期权代码
            underlying_price: 标的价格
            strike: 行权价
            expiry_date: 到期日 (YYYY-MM-DD)
            option_type: 期权类型 (P=Put, C=Call)
            option_price: 期权价格 (用于计算 IV，可选)
        
        Returns:
            GreeksData 对象
        """
        # 尝试从富途获取实时 Greeks
        if self.quote_ctx:
            real_time_greeks = self._get_from_futu(option_code)
            if real_time_greeks:
                return real_time_greeks
        
        # 使用 Black-Scholes 理论计算
        return self._calculate_bs_greeks(
            underlying_price, strike, expiry_date, option_type, option_price
        )
    
    def update_greeks(self, greeks: GreeksData, underlying_price: float) -> GreeksData:
        """
        根据标的价格变动更新 Greeks
        
        用于实时跟踪 Greeks 变化
        
        Args:
            greeks: 当前 Greeks
            underlying_price: 新的标的价格
        
        Returns:
            更新后的 Greeks
        """
        # 使用 Gamma 估算 Delta 变化
        # ΔDelta ≈ Gamma × ΔS
        # 这里简化处理，实际需要重新计算
        greeks.timestamp = datetime.now().isoformat()
        return greeks
    
    # =================================================================
    # 富途实时数据获取
    # =================================================================
    
    def _get_from_futu(self, option_code: str) -> Optional[GreeksData]:
        """从富途 OpenD 获取实时 Greeks"""
        try:
            from futu import RET_OK
            
            ret, data = self.quote_ctx.get_market_snapshot([option_code])
            
            if ret != RET_OK or data is None or data.empty:
                return None
            
            row = data.iloc[0]
            
            greeks = GreeksData(
                delta=float(row.get("option_delta", 0) or 0),
                gamma=float(row.get("option_gamma", 0) or 0),
                theta=float(row.get("option_theta", 0) or 0),
                vega=float(row.get("option_vega", 0) or 0),
                rho=float(row.get("option_rho", 0) or 0),
                implied_volatility=float(row.get("option_implied_volatility", 0) or 0),
                timestamp=datetime.now().isoformat(),
            )
            
            logger.debug(f"[Greeks] {option_code} 实时数据：Δ={greeks.delta:.4f}")
            return greeks
            
        except ImportError:
            logger.debug("[Greeks] futu 未安装，使用理论计算")
            return None
        except Exception as e:
            logger.warning(f"[Greeks] 获取实时数据失败 {option_code}: {e}")
            return None
    
    # =================================================================
    # Black-Scholes 理论计算
    # =================================================================
    
    def _calculate_bs_greeks(
        self,
        S: float,  # 标的价格
        K: float,  # 行权价
        expiry_date: str,
        option_type: str,
        option_price: Optional[float] = None,
        iv: Optional[float] = None,
    ) -> GreeksData:
        """
        使用 Black-Scholes 模型计算 Greeks
        
        公式参考：
        - Delta: N(d1) for Call, N(d1)-1 for Put
        - Gamma: N'(d1) / (S * σ * sqrt(T))
        - Theta: 时间衰减
        - Vega: S * N'(d1) * sqrt(T)
        
        Args:
            S: 标的价格
            K: 行权价
            expiry_date: 到期日
            option_type: P=Put, C=Call
            option_price: 期权价格 (用于反推 IV)
            iv: 隐含波动率 (如已知)
        
        Returns:
            GreeksData
        """
        try:
            # 计算到期时间 T (年)
            expiry = datetime.strptime(expiry_date, "%Y-%m-%d").date()
            days_to_expiry = (expiry - date.today()).days
            T = max(1/365, days_to_expiry / 365.0)  # 至少 1 天
            
            # 确定隐含波动率
            if iv is None:
                if option_price:
                    # 反推 IV
                    iv = self._implied_volatility(S, K, T, option_price, option_type)
                else:
                    # 默认 IV (美股科技股典型值 45%)
                    iv = 0.45
            
            # 计算 d1, d2
            sqrt_T = math.sqrt(T)
            d1 = (math.log(S / K) + (self.risk_free_rate + 0.5 * iv ** 2) * T) / (iv * sqrt_T)
            d2 = d1 - iv * sqrt_T
            
            # 标准正态分布函数
            N_d1 = self._norm_cdf(d1)
            N_d2 = self._norm_cdf(d2)
            N_prime_d1 = self._norm_pdf(d1)
            
            is_call = option_type.upper() == "C"
            
            # 计算 Greeks
            if is_call:
                delta = N_d1
            else:
                delta = N_d1 - 1
            
            gamma = N_prime_d1 / (S * iv * sqrt_T)
            
            # Theta (每日)
            term1 = -(S * N_prime_d1 * iv) / (2 * sqrt_T)
            if is_call:
                term2 = self.risk_free_rate * K * math.exp(-self.risk_free_rate * T) * N_d2
                theta = (term1 - term2) / 365.0
            else:
                term2 = -self.risk_free_rate * K * math.exp(-self.risk_free_rate * T) * self._norm_cdf(-d2)
                theta = (term1 + term2) / 365.0
            
            # Vega (1% 波动率变动)
            vega = S * N_prime_d1 * sqrt_T / 100.0
            
            # Rho (1% 利率变动)
            if is_call:
                rho = K * T * math.exp(-self.risk_free_rate * T) * N_d2 / 100.0
            else:
                rho = -K * T * math.exp(-self.risk_free_rate * T) * self._norm_cdf(-d2) / 100.0
            
            greeks = GreeksData(
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
                rho=rho,
                implied_volatility=iv,
                timestamp=datetime.now().isoformat(),
            )
            
            logger.debug(
                f"[Greeks·BS] {option_type} S={S} K={K} T={days_to_expiry}d "
                f"Δ={delta:.4f} Γ={gamma:.6f} Θ={theta:.4f} V={vega:.4f}"
            )
            
            return greeks
            
        except Exception as e:
            logger.error(f"[Greeks] BS 计算异常：{e}", exc_info=True)
            return GreeksData(timestamp=datetime.now().isoformat())
    
    def _implied_volatility(
        self,
        S: float,
        K: float,
        T: float,
        option_price: float,
        option_type: str,
        max_iter: int = 100,
        tol: float = 1e-5,
    ) -> float:
        """
        使用 Newton-Raphson 方法反推隐含波动率
        """
        iv = 0.5  # 初始猜测
        
        for _ in range(max_iter):
            price = self._bs_price(S, K, T, iv, option_type)
            vega = self._bs_vega(S, K, T, iv)
            
            if vega < 1e-10:
                break
            
            diff = option_price - price
            if abs(diff) < tol:
                break
            
            iv = iv + diff / vega
            iv = max(0.01, min(iv, 5.0))  # 限制 IV 范围
        
        return iv
    
    def _bs_price(
        self,
        S: float,
        K: float,
        T: float,
        iv: float,
        option_type: str,
    ) -> float:
        """Black-Scholes 期权价格"""
        sqrt_T = math.sqrt(T)
        d1 = (math.log(S / K) + (self.risk_free_rate + 0.5 * iv ** 2) * T) / (iv * sqrt_T)
        d2 = d1 - iv * sqrt_T
        
        if option_type.upper() == "C":
            price = S * self._norm_cdf(d1) - K * math.exp(-self.risk_free_rate * T) * self._norm_cdf(d2)
        else:
            price = K * math.exp(-self.risk_free_rate * T) * self._norm_cdf(-d2) - S * self._norm_cdf(-d1)
        
        return max(0, price)
    
    def _bs_vega(self, S: float, K: float, T: float, iv: float) -> float:
        """Black-Scholes Vega"""
        sqrt_T = math.sqrt(T)
        d1 = (math.log(S / K) + (self.risk_free_rate + 0.5 * iv ** 2) * T) / (iv * sqrt_T)
        return S * self._norm_pdf(d1) * sqrt_T
    
    @staticmethod
    def _norm_cdf(x: float) -> float:
        """标准正态分布累积分布函数"""
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))
    
    @staticmethod
    def _norm_pdf(x: float) -> float:
        """标准正态分布概率密度函数"""
        return math.exp(-0.5 * x ** 2) / math.sqrt(2 * math.pi)


# =================================================================
# Greeks 实时监控器
# =================================================================

@dataclass
class GreeksMonitorConfig:
    """Greeks 监控配置"""
    update_interval_seconds: int = 30  # 更新间隔
    delta_target_min: float = 0.4      # Delta 目标下限 (绝对值)
    delta_target_max: float = 0.6      # Delta 目标上限 (绝对值)
    gamma_threshold: float = 0.05      # Gamma 阈值
    theta_decay_alert: float = 0.10    # Theta 衰减告警阈值
    vega_exposure_limit: float = 0.20  # Vega 敞口限制


class GreeksMonitor:
    """
    Greeks 实时监控器
    
    功能：
    - 定期更新 Greeks 值
    - Delta 目标范围监控 (0.4-0.6)
    - Gamma/Theta/Vega 异常告警
    """
    
    def __init__(self, calculator: GreeksCalculator, config: GreeksMonitorConfig = None):
        self.calculator = calculator
        self.config = config or GreeksMonitorConfig()
        self.current_greeks: Dict[str, GreeksData] = {}
        self.alerts: List[Dict[str, Any]] = []
    
    def update(
        self,
        option_code: str,
        underlying_price: float,
        strike: float,
        expiry_date: str,
        option_type: str,
        option_price: Optional[float] = None,
    ) -> GreeksData:
        """更新单个期权的 Greeks"""
        greeks = self.calculator.get_greeks(
            option_code, underlying_price, strike, expiry_date, option_type, option_price
        )
        self.current_greeks[option_code] = greeks
        
        # 检查 Delta 目标范围
        self._check_delta_range(option_code, greeks)
        
        # 检查 Gamma 异常
        self._check_gamma_threshold(option_code, greeks)
        
        # 检查 Theta 衰减
        self._check_theta_decay(option_code, greeks)
        
        # 检查 Vega 敞口
        self._check_vega_exposure(option_code, greeks)
        
        return greeks
    
    def _check_delta_range(self, option_code: str, greeks: GreeksData):
        """检查 Delta 是否在目标范围 (0.4-0.6)"""
        delta_abs = abs(greeks.delta)
        
        if delta_abs < self.config.delta_target_min or delta_abs > self.config.delta_target_max:
            alert = {
                "type": "DELTA_OUT_OF_RANGE",
                "option_code": option_code,
                "delta": greeks.delta,
                "target_range": [self.config.delta_target_min, self.config.delta_target_max],
                "timestamp": greeks.timestamp,
            }
            self.alerts.append(alert)
            logger.warning(f"[Greeks·监控] {option_code} Delta={greeks.delta:.4f} 超出目标范围")
    
    def _check_gamma_threshold(self, option_code: str, greeks: GreeksData):
        """检查 Gamma 是否超过阈值"""
        if abs(greeks.gamma) > self.config.gamma_threshold:
            alert = {
                "type": "GAMMA_HIGH",
                "option_code": option_code,
                "gamma": greeks.gamma,
                "threshold": self.config.gamma_threshold,
                "timestamp": greeks.timestamp,
            }
            self.alerts.append(alert)
            logger.warning(f"[Greeks·监控] {option_code} Gamma={greeks.gamma:.6f} 超过阈值")
    
    def _check_theta_decay(self, option_code: str, greeks: GreeksData):
        """检查 Theta 时间衰减"""
        if greeks.theta < -self.config.theta_decay_alert:
            alert = {
                "type": "THETA_DECAY_HIGH",
                "option_code": option_code,
                "theta": greeks.theta,
                "threshold": self.config.theta_decay_alert,
                "timestamp": greeks.timestamp,
            }
            self.alerts.append(alert)
            logger.warning(f"[Greeks·监控] {option_code} Theta={greeks.theta:.4f} 时间衰减过快")
    
    def _check_vega_exposure(self, option_code: str, greeks: GreeksData):
        """检查 Vega 波动率敞口"""
        if abs(greeks.vega) > self.config.vega_exposure_limit:
            alert = {
                "type": "VEGA_EXPOSURE_HIGH",
                "option_code": option_code,
                "vega": greeks.vega,
                "limit": self.config.vega_exposure_limit,
                "timestamp": greeks.timestamp,
            }
            self.alerts.append(alert)
            logger.warning(f"[Greeks·监控] {option_code} Vega={greeks.vega:.4f} 波动率敞口过大")
    
    def get_alerts(self) -> List[Dict[str, Any]]:
        """获取所有告警"""
        return self.alerts
    
    def clear_alerts(self):
        """清除告警"""
        self.alerts = []


# =================================================================
# 工具函数
# =================================================================

def calculate_position_greeks(
    positions: List[Dict[str, Any]],
    calculator: GreeksCalculator,
) -> Dict[str, float]:
    """
    计算持仓组合的 Greeks
    
    Args:
        positions: 持仓列表，每项包含：
            - option_code: 期权代码
            - quantity: 持仓数量 (正=多，负=空)
            - underlying_price: 标的价格
            - strike: 行权价
            - expiry_date: 到期日
            - option_type: P/C
        calculator: Greeks 计算器
    
    Returns:
        组合 Greeks 字典
    """
    total_greeks = {
        "delta": 0.0,
        "gamma": 0.0,
        "theta": 0.0,
        "vega": 0.0,
        "rho": 0.0,
    }
    
    for pos in positions:
        greeks = calculator.get_greeks(
            pos["option_code"],
            pos["underlying_price"],
            pos["strike"],
            pos["expiry_date"],
            pos["option_type"],
        )
        
        qty = pos.get("quantity", 1) * 100  # 1 手=100 股
        
        total_greeks["delta"] += greeks.delta * qty
        total_greeks["gamma"] += greeks.gamma * qty
        total_greeks["theta"] += greeks.theta * qty
        total_greeks["vega"] += greeks.vega * qty
        total_greeks["rho"] += greeks.rho * qty
    
    return total_greeks
