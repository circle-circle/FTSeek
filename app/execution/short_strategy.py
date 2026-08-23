"""
做空策略统一抽象层 (Short Strategy Router) - 2026-04-15 新增

架构原则:
1. 信号层不关心执行方式 (正股做空 or 期权)
2. 执行层通过配置决定具体实现
3. 模拟盘强制使用期权 (Put)
4. 实盘支持正股做空 (预留接口)
5. 期权策略: 快进快出 (持仓 < 72h)

使用方式:
    from app.execution.short_strategy import ShortStrategyRouter
    router = ShortStrategyRouter(config)
    strategy = router.select_strategy(symbol, signal)
    strategy.execute(symbol, signal)
"""

import logging
import time
from enum import Enum
from typing import Dict, Any, Optional, Tuple, Protocol
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================
#  枚举与协议
# ============================================================
class ShortMethod(str, Enum):
    """做空实现方式"""
    STOCK_SHORT = "stock_short"       # 直接做空正股 (实盘专用)


class ShortExecutionStrategy(Protocol):
    """做空执行策略接口"""

    @property
    def method(self) -> ShortMethod: ...

    def can_execute(self, symbol: str, signal: Any, context: Dict) -> Tuple[bool, str]: ...

    def execute(self, symbol: str, signal: Any, context: Dict) -> Dict[str, Any]: ...

    def close(self, position: Dict) -> Dict[str, Any]: ...

    def check_exit(self, position: Dict, current_price: float) -> Tuple[bool, str]: ...


# ============================================================
#  配置加载器
# ============================================================
def load_short_config() -> Dict[str, Any]:
    """
    加载做空策略配置 — 单一真相源 (SSOT)

    所有配置来自 ConfigContainer → unified_config.yaml
    无硬编码默认值，无独立 YAML 文件加载。
    """
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        options = cfg.get_section("options", {})
        short_cfg = options.get("short_strategy", {})

        if short_cfg:
            logger.debug("[做空策略] 配置已从 ConfigContainer SSOT 加载")
            return short_cfg

    except Exception as e:
        logger.warning(f"[做空策略] ConfigContainer 加载失败: {e}")

    # 最终降级 (仅当 ConfigContainer 完全不可用时)
    return {
        "default_method": "stock_short",
        "stock_short": {
            "enabled": True,
            "stop_loss_pct": -0.08,
            "take_profit_pct": 0.10,
            "max_shares": 100,
            "borrow_fee_max_pct": 0.05,
            "require_borrow_availability": True,
            "borrow_cost_gate": {"enabled": True, "max_annual_rate": 0.05, "elevated_conviction_threshold": 85},
            "squeeze_detector": {"enabled": True, "max_short_float_pct": 20.0, "action": "block"},
            "position_sizing": {"short_to_long_ratio": 0.60, "max_position_pct": 0.10},
            "time_decay": {"max_holding_days": 5, "no_catalyst_exit_days": 3},
            "technical_breach": {"enabled": True, "resistance_lookback_days": 20, "breach_threshold_pct": 1.0},
            "pair_preference": {"enabled": True, "prefer_sector_pairs": True, "naked_short_penalty": 0.10},
        },
    }



# ============================================================
#  正股做空策略 (实盘预留)
# ============================================================
class StockShortStrategy:
    """正股做空策略 — 幻方原则增强版

    幻方 (High-Flyer) 核心原则:
    1. 借券成本门控: >5% 年化利率拒绝
    2. 轧空检测: 空头 >20% 拒绝
    3. 市况适配: 牛市提升门槛
    4. 非对称仓位: 做空 = 做多的 60%
    5. 时间衰减: 无催化 3 天退出
    6. 技术破位: 快速平仓
    7. 配对偏好: 行业配对优于裸做空
    """

    @property
    def method(self) -> ShortMethod:
        return ShortMethod.STOCK_SHORT

    def __init__(self, config: Dict[str, Any]):
        sc = config.get("stock_short", {})
        self.enabled = sc.get("enabled", True)
        self.stop_loss_pct = abs(float(sc.get("stop_loss_pct", -0.08)))
        self.take_profit_pct = float(sc.get("take_profit_pct", 0.10))
        self.max_shares = int(sc.get("max_shares", 100))

        # 幻方 Principle 1: 借券成本门控
        self.borrow_cost_gate = sc.get("borrow_cost_gate", {})
        # 幻方 Principle 2: 轧空检测
        self.squeeze_detector = sc.get("squeeze_detector", {})
        # 幻方 Principle 3: 非对称仓位
        self.position_sizing = sc.get("position_sizing", {})
        # 幻方 Principle 4: 时间衰减
        self.time_decay = sc.get("time_decay", {})
        # 幻方 Principle 5: 技术破位
        self.technical_breach = sc.get("technical_breach", {})
        # 幻方 Principle 6: 配对偏好
        self.pair_preference = sc.get("pair_preference", {})

    def can_execute(self, symbol: str, signal: Any, context: Dict) -> Tuple[bool, str]:
        """检查是否可以执行正股做空 (全部门控)"""
        # 模拟盘不允许
        try:
            from app.core.config_container import get_config
            if get_config().is_simulated():
                return False, "模拟盘不支持正股做空"
        except Exception:
            return False, "无法确认交易模式"

        if not self.enabled:
            return False, "正股做空未启用 (stock_short.enabled=false)"

        # (借券成本/轧空两门已删 — DEB-20260823 用户裁"不值得": 数据源恒空=死门)

        # 幻方 Principle 3: 市况适配
        try:
            from app.risk.short_specific_checks import check_regime_for_short
            regime = getattr(signal, "market_regime", "") if signal else ""
            strength = getattr(signal, "strength", 50) if signal else 50
            effective = (100 - strength) if strength < 50 else strength
            ok, reason = check_regime_for_short(regime, effective, self.borrow_cost_gate)
            if not ok:
                return False, reason
        except ImportError:
            pass

        return True, "正股做空可用"

    def execute(self, symbol: str, signal: Any, context: Dict) -> Dict[str, Any]:
        """执行正股做空 — 非对称仓位 + 退出策略集成"""
        price = float(context.get("price", 0))
        if price <= 0:
            return {"success": False, "reason": "价格为 0"}

        # 幻方 Principle 4: 非对称仓位 (做空 = 做多 * ratio)
        base_qty = int(context.get("order_qty", self.max_shares))
        short_ratio = float(self.position_sizing.get("short_to_long_ratio", 0.60))
        quantity = max(1, int(base_qty * short_ratio))
        quantity = min(quantity, self.max_shares)

        # 从 exit_policy.yaml 读取做空差异化参数
        sl_mult, max_loss = self._read_exit_policy_overrides()

        # 止损 = 价格上涨触发 (做空亏钱方向)
        stop_loss = price * (1 + self.stop_loss_pct)
        # 根据 ATR 调整 (如果有)
        atr = float(context.get("atr", 0))
        if atr > 0:
            atr_stop = price + atr * sl_mult
            hard_stop = price * (1 + max_loss)
            stop_loss = min(atr_stop, hard_stop)  # 取更紧的

        take_profit = price * (1 - self.take_profit_pct)

        # 幻方 Principle 5: 技术破位阻力位 (20 日高点)
        resistance = float(context.get("resistance_20d", price * 1.05))
        breach_threshold = resistance * (1 + float(self.technical_breach.get("breach_threshold_pct", 0.01)))

        # 幻方 Principle 6: 配对偏好判断
        is_naked = True
        try:
            sector = context.get("sector", "")
            pair_etf = self.pair_preference.get("sector_etf_map", {}).get(sector)
            if pair_etf and self.pair_preference.get("prefer_sector_pairs"):
                is_naked = False
        except Exception:
            pass

        order_info = {
            "success": True,
            "method": "stock_short",
            "symbol": symbol,
            "quantity": quantity,
            "price": price,
            "stop_loss": round(stop_loss, 2),
            "take_profit": round(take_profit, 2),
            "resistance_20d": round(resistance, 2),
            "breach_cover_price": round(breach_threshold, 2),
            "max_holding_days": int(self.time_decay.get("max_holding_days", 5)),
            "no_catalyst_exit_days": int(self.time_decay.get("no_catalyst_exit_days", 3)),
            "is_naked_short": is_naked,
            "entry_time": datetime.now().isoformat(),
            "direction": "SHORT",
        }

        logger.info(
            f"[正股做空] {symbol} SELL qty={quantity} price=${price:.2f} "
            f"SL=${order_info['stop_loss']:.2f} TP=${order_info['take_profit']:.2f} "
            f"resistance=${resistance:.2f} naked={is_naked}"
        )

        return order_info

    def _read_exit_policy_overrides(self) -> Tuple[float, float]:
        """从 exit_policy.yaml 读取做空差异化参数"""
        try:
            from app.risk.exit_policy_guardian import ExitPolicyGuardian
            policy = ExitPolicyGuardian().load()
            overrides = policy.get("short_overrides", {})
            sl_mult = float(overrides.get("atr_multiplier", 2.0))
            max_loss = float(overrides.get("max_loss_pct", 10.0)) / 100.0
            return sl_mult, max_loss
        except Exception:
            return 2.0, 0.10

    def close(self, position: Dict) -> Dict[str, Any]:
        """平仓正股做空 (BUY to Cover)"""
        symbol = position.get("symbol", "")
        qty = position.get("size", position.get("quantity", 0))

        logger.info(f"[正股平仓] {symbol} BUY to Cover qty={qty}")

        try:
            from app.core.config_container import get_config
            adapter = get_config().get("_broker_adapter") if hasattr(get_config(), 'get') else None
        except Exception:
            adapter = None

        return {
            "success": True,
            "symbol": symbol,
            "quantity": qty,
            "action": "BUY_TO_COVER",
            "close_time": datetime.now().isoformat(),
        }

    def check_exit(self, position: Dict, current_price: float) -> Tuple[bool, str]:
        """检查正股做空退出 — 幻方全维度检测"""
        entry_price = float(position.get("entry_price", position.get("avg_entry_price", 0)))
        if entry_price <= 0 or current_price <= 0:
            return False, ""

        pnl_pct = (entry_price - current_price) / entry_price  # 做空盈利 = 价格跌了赚

        # 1. 止盈
        if pnl_pct >= self.take_profit_pct:
            return True, f"止盈达到 ({pnl_pct:.1%} >= {self.take_profit_pct:.1%})"

        # 2. 止损
        if pnl_pct <= -self.stop_loss_pct:
            return True, f"止损触发 ({pnl_pct:.1%} <= -{self.stop_loss_pct:.1%})"

        # 3. 幻方 Principle 5: 技术破位快速平仓
        if self.technical_breach.get("enabled", True):
            resistance = float(position.get("resistance_20d", 0))
            if resistance > 0:
                breach_price = resistance * (1 + float(self.technical_breach.get("breach_threshold_pct", 0.01)))
                if current_price >= breach_price:
                    return True, f"技术破位平仓 (${current_price:.2f} >= breach=${breach_price:.2f})"

        # 4. 幻方 Principle 4: 时间衰减 (无催化退出)
        max_days = int(self.time_decay.get("max_holding_days", 5))
        no_catalyst_days = int(self.time_decay.get("no_catalyst_exit_days", 3))
        entry_time_str = position.get("entry_time", position.get("entry_time_iso", ""))

        if entry_time_str:
            try:
                from app.utils.timezone import now_et as _now_et, to_et as _to_et
                entry_time = _to_et(datetime.fromisoformat(entry_time_str))
                days_held = (_now_et() - entry_time).total_seconds() / 86400

                if days_held >= max_days:
                    return True, f"持仓超时 ({days_held:.1f}d >= {max_days}d)"

                # 无催化退出 (如果 position 有 catalyst 标记)
                catalysts = position.get("catalysts", [])
                if days_held >= no_catalyst_days and not catalysts:
                    return True, f"无催化超时 ({days_held:.1f}d, 无新利空催化)"
            except (ValueError, TypeError):
                pass

        return False, ""


# ============================================================
#  启动校验 (Karpathy: 代码不能违反配置)
# ============================================================
import dataclasses

@dataclasses.dataclass
class ShortConfigResult:
    """做空配置校验结果"""
    is_valid: bool
    errors: list = dataclasses.field(default_factory=list)
    warnings: list = dataclasses.field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.is_valid:
            return f"通过 ({len(self.warnings)} 个警告)" if self.warnings else "全部通过"
        return f"{len(self.errors)} 个错误, {len(self.warnings)} 个警告"


def validate_short_config() -> ShortConfigResult:
    """启动时校验做空配置完整性 (所有阈值来自 ConfigContainer SSOT)"""
    errors = []
    warnings = []

    try:
        from app.core.config_container import get_config
        cfg = get_config()
        sc = cfg.get_dotted("options.short_strategy.stock_short", {})
    except Exception as e:
        errors.append(f"无法加载配置: {e}")
        return ShortConfigResult(is_valid=False, errors=errors, warnings=warnings)

    # 1. stock_short.enabled 必须为 bool
    if not isinstance(sc.get("enabled"), bool):
        errors.append("stock_short.enabled 必须是 boolean")

    # 2. 借券成本门控
    bcg = sc.get("borrow_cost_gate", {})
    if bcg.get("enabled", True):
        max_rate = float(bcg.get("max_annual_rate", 0.05))
        if not (0.01 <= max_rate <= 0.20):
            errors.append(f"borrow_cost_gate.max_annual_rate={max_rate} 超出范围 [0.01, 0.20]")
        threshold = float(bcg.get("elevated_conviction_threshold", 85))
        if not (50 <= threshold <= 100):
            errors.append(f"borrow_cost_gate.elevated_conviction_threshold={threshold} 超出范围 [50, 100]")

    # 3. 轧空检测
    sd = sc.get("squeeze_detector", {})
    if sd.get("enabled", True):
        max_float = float(sd.get("max_short_float_pct", 20.0))
        if not (5.0 <= max_float <= 50.0):
            errors.append(f"squeeze_detector.max_short_float_pct={max_float} 超出范围 [5, 50]")

    # 4. 非对称仓位
    ps = sc.get("position_sizing", {})
    ratio = float(ps.get("short_to_long_ratio", 0.60))
    if not (0.30 <= ratio <= 1.0):
        errors.append(f"position_sizing.short_to_long_ratio={ratio} 超出范围 [0.3, 1.0]")
    max_pp = float(ps.get("max_position_pct", 0.10))
    if not (0.03 <= max_pp <= 0.25):
        errors.append(f"position_sizing.max_position_pct={max_pp} 超出范围 [0.03, 0.25]")

    # 5. 时间衰减
    td = sc.get("time_decay", {})
    max_days = int(td.get("max_holding_days", 5))
    if not (1 <= max_days <= 30):
        errors.append(f"time_decay.max_holding_days={max_days} 超出范围 [1, 30]")
    no_cat = int(td.get("no_catalyst_exit_days", 3))
    if no_cat > max_days:
        errors.append(f"time_decay.no_catalyst_exit_days={no_cat} > max_holding_days={max_days}")

    # 6. 技术破位
    tb = sc.get("technical_breach", {})
    if tb.get("enabled", True):
        breach_pct = float(tb.get("breach_threshold_pct", 1.0))
        if not (0.1 <= breach_pct <= 5.0):
            errors.append(f"technical_breach.breach_threshold_pct={breach_pct} 超出范围 [0.1, 5.0]")

    # 7. 配对偏好 sector_etf_map 检查
    pp = sc.get("pair_preference", {})
    if pp.get("enabled", True):
        etf_map = pp.get("sector_etf_map", {})
        for sector, etf in etf_map.items():
            if not etf.startswith("US."):
                warnings.append(f"pair_preference.sector_etf_map.{sector}={etf} 不是 US 前缀")

    # 8. exit_policy.yaml short_overrides 一致性
    try:
        from app.risk.exit_policy_guardian import ExitPolicyGuardian
        policy = ExitPolicyGuardian().load()
        overrides = policy.get("short_overrides", {})
        if not overrides:
            warnings.append("exit_policy.yaml 缺少 short_overrides 节")
        else:
            required_keys = ["atr_multiplier", "max_loss_pct", "scale_out"]
            for k in required_keys:
                if k not in overrides:
                    warnings.append(f"exit_policy.yaml short_overrides 缺少 '{k}'")
    except Exception as e:
        warnings.append(f"exit_policy 一致性校验跳过: {e}")

    is_valid = len(errors) == 0
    return ShortConfigResult(is_valid=is_valid, errors=errors, warnings=warnings)


# ============================================================
#  做空策略路由器
# ============================================================
class ShortStrategyRouter:
    """
    做空策略路由器

    ADR-003: 期权已禁用，仅使用正股做空。
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or load_short_config()

        # 策略实例 — 仅正股做空
        self.strategies: Dict[ShortMethod, ShortExecutionStrategy] = {
            ShortMethod.STOCK_SHORT: StockShortStrategy(self.config),
        }

        logger.info(
            f"[做空路由器] 已初始化 | 方法=stock_short"
        )

    def select_strategy(self, symbol: str, signal: Any, context: Dict) -> ShortExecutionStrategy:
        """返回正股做空策略（期权路径已移除）"""
        return self.strategies[ShortMethod.STOCK_SHORT]

    def execute_signal(self, signal, context: Optional[Dict] = None) -> Dict[str, Any]:
        """P0 修复 (2026-04-16): 便捷方法，供 app.pipeline 直接调用

        Args:
            signal: SignalScore 或兼容对象
            context: 市场上下文 (可选)

        Returns:
            {"success": bool, "error": str, "order_id": str, ...}
        """
        try:
            symbol = getattr(signal, 'symbol', '')
            ctx = context or {}
            strategy = self.select_strategy(symbol, signal, ctx)
            return strategy.execute(symbol, signal, ctx)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_all_strategies(self) -> Dict[str, ShortExecutionStrategy]:
        """获取所有策略实例"""
        return {m.value: s for m, s in self.strategies.items()}


# ============================================================
#  全局单例
# ============================================================
_short_router: Optional[ShortStrategyRouter] = None


def get_short_strategy_router(config: Optional[Dict[str, Any]] = None) -> ShortStrategyRouter:
    """获取做空策略路由器单例"""
    global _short_router
    if _short_router is None:
        _short_router = ShortStrategyRouter(config)
    return _short_router
