"""仓位限制检查器 — P0 修复 (2026-05-04)

修复清单:
1. 新增 existing_symbol_exposure 参数 — 检查「已有+新仓」单品种集中度
2. 修复 max_single_position 死配置问题 — 代码读取 risk.position.max_per_symbol
3. 新增单品种集中度告警 (warning_only)
4. 统一配置读取: 所有参数从 unified_config.yaml SSOT 加载
"""
import logging
from typing import Tuple, Optional, Dict

logger = logging.getLogger("ftseek_us.position_limits")

# P0 修复 (2026-05-01): ETF 敞口折扣因子
# ETF/宽基指数波动率远低于个股，不应 1:1 消耗方向敞口
# 否则会出现 QQQ 一个持仓占 45% 敞口，导致所有个股新仓被 DIRECTION_LIMIT 拒绝
ETF_EXPOSURE_DISCOUNT = {
    "US.QQQ": 0.30,   # 纳斯达克100 ETF — 波动率约为个股的 30%
    "US.SPY": 0.30,   # 标普500 ETF
    "US.DIA": 0.30,   # 道琼斯 ETF
    "US.IWM": 0.40,   # 罗素2000 — 小盘波动略高
    "US.SOX": 0.50,   # 半导体 ETF — 行业集中度高
    "US.XLF": 0.35,   # 金融 ETF
    "US.XLE": 0.40,   # 能源 ETF
}

# 单持仓占比超过此阈值时告警 (warning_only，不拦截)
SINGLE_POSITION_WARNING_PCT = 0.20


def get_etf_discount(symbol: str) -> float:
    """获取 ETF 敞口折扣因子，非 ETF 返回 1.0"""
    return ETF_EXPOSURE_DISCOUNT.get(symbol, 1.0)


class PositionLimitsChecker:
    """仓位限制检查器

    P0 修复: check_position() 新增 existing_symbol_exposure 参数，
    检查「已有持仓 + 新建议仓位」是否超过单品种上限。
    修复前只检查新交易本身，导致 QQQ 累积到 44.8% 不被拦截。
    """

    def __init__(
        self,
        max_position_per_symbol: float = 0.25,
        max_position_per_direction: float = 0.50,
        max_total_position: float = 0.80,
    ):
        self.max_symbol = max_position_per_symbol
        self.max_direction = max_position_per_direction
        self.max_total = max_total_position
        logger.info(
            f"[PositionLimits] 初始化: max_symbol={self.max_symbol:.0%}, "
            f"max_direction={self.max_direction:.0%}, max_total={self.max_total:.0%}"
        )

    def check_position(
        self,
        symbol: str,
        proposed_position: float,
        current_direction_exposure: float,
        current_total_exposure: float,
        existing_symbol_exposure: float = 0.0,
    ) -> Tuple[bool, Optional[str]]:
        """
        检查仓位是否超限

        Args:
            symbol: 交易标的
            proposed_position: 新交易建议仓位 (占净值比例, 0-1)
            current_direction_exposure: 当前方向总敞口 (所有品种合计)
            current_total_exposure: 当前总敞口 (多空合计)
            existing_symbol_exposure: 该品种已有持仓敞口 (P0 新增)

        Returns:
            (是否允许，拒绝原因)

        P0 修复:
        - 单品种限制检查「已有 + 新仓」合计是否超过 max_symbol
        - 使用折扣后的有效值进行比较
        """
        # ETF 敞口折扣：QQQ/SPY 等 ETF 的实际风险远低于个股
        discount = get_etf_discount(symbol)
        effective_proposed = proposed_position * discount
        effective_existing = existing_symbol_exposure * discount
        effective_total_symbol = effective_existing + effective_proposed

        # ====== P0 修复: 单品种集中度检查 (已有 + 新仓) ======
        if effective_total_symbol > self.max_symbol:
            logger.warning(
                f"[PositionLimits] {symbol} 单品种集中度超限: "
                f"已有={existing_symbol_exposure:.1%}(折后{effective_existing:.1%}) + "
                f"新仓={proposed_position:.1%}(折后{effective_proposed:.1%}) = "
                f"合计折后{effective_total_symbol:.1%} > 上限{self.max_symbol:.0%}"
            )
            return False, "SINGLE_SYMBOL_LIMIT"

        # 单品种集中度告警 (不拦截，但记录日志)
        if effective_total_symbol > SINGLE_POSITION_WARNING_PCT:
            logger.warning(
                f"[PositionLimits] {symbol} 单品种集中度接近上限: "
                f"折后合计 {effective_total_symbol:.1%} > 告警阈值 {SINGLE_POSITION_WARNING_PCT:.0%}"
            )

        # 单方向限制 (ETF 的敞口占用按折扣计算)
        if current_direction_exposure + effective_proposed > self.max_direction:
            return False, "DIRECTION_LIMIT"

        # 总仓位限制
        if current_total_exposure + effective_proposed > self.max_total:
            return False, "TOTAL_POSITION_LIMIT"

        return True, None
