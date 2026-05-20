"""
行业分散限制模块 - P1 改进

功能:
- 单行业持仓 ≤ 40%
- 提供行业分类配置
- 防止行业集中风险
"""
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# 默认行业分类配置
DEFAULT_SECTOR_MAPPING = {
    # 科技
    "Technology": ["AAPL", "MSFT", "GOOGL", "META", "NVDA", "AMD", "INTC", "CRM", "ORCL"],
    # 消费
    "Consumer": ["AMZN", "TSLA", "NKE", "MCD", "SBUX", "WMT", "COST", "HD"],
    # 金融
    "Financial": ["JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "SCHW"],
    # 医疗
    "Healthcare": ["JNJ", "UNH", "PFE", "MRK", "ABBV", "TMO", "ABT", "DHR"],
    # 能源
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO"],
    # 工业
    "Industrial": ["CAT", "BA", "HON", "UPS", "GE", "MMM", "LMT", "RTX"],
    # 通信
    "Communication": ["T", "VZ", "TMUS", "DIS", "NFLX", "CMCSA"],
    # 公用事业
    "Utilities": ["NEE", "DUK", "SO", "D", "AEP", "EXC"],
    # 房地产
    "RealEstate": ["AMT", "PLD", "CCI", "EQIX", "SPG", "PSA"],
    # 材料
    "Materials": ["LIN", "APD", "ECL", "SHW", "FCX", "NEM", "DOW"],
}

# 中概股行业分类
CHINA_CONCEPT_MAPPING = {
    "Technology": ["BABA", "JD", "PDD", "BIDU", "NTES"],
    "Consumer": ["BABA", "JD", "PDD", "NIO", "XPEV", "LI"],
}


@dataclass
class SectorPosition:
    """行业持仓信息"""
    symbol: str
    sector: str
    market_value: float
    weight: float  # 占账户总资产比例


@dataclass
class SectorLimitsState:
    """行业限制状态"""
    sector_exposures: Dict[str, float] = field(default_factory=dict)
    total_exposure: float = 0.0


class SectorLimitsChecker:
    """
    行业分散限制检查器
    
    P1 改进：
    - 单行业持仓 ≤ 40%
    - 提供行业分类配置
    """
    
    def __init__(
        self,
        max_sector_weight: float = 0.40,
        sector_mapping: Optional[Dict[str, List[str]]] = None,
    ):
        """
        初始化行业限制检查器
        
        Args:
            max_sector_weight: 单行业最大权重 (默认 40%)
            sector_mapping: 行业分类映射
        """
        self.max_sector_weight = max_sector_weight
        self.sector_mapping = {**DEFAULT_SECTOR_MAPPING, **CHINA_CONCEPT_MAPPING}
        
        if sector_mapping:
            self.sector_mapping.update(sector_mapping)
        
        # 构建反向映射：symbol -> sector
        self._symbol_to_sector: Dict[str, str] = {}
        for sector, symbols in self.sector_mapping.items():
            for symbol in symbols:
                self._symbol_to_sector[symbol.upper()] = sector
        
        self.state = SectorLimitsState()
        
        logger.info(
            f"行业限制检查器已初始化 | "
            f"单行业上限={max_sector_weight:.0%} | "
            f"覆盖行业数={len(self.sector_mapping)}"
        )
    
    def get_symbol_sector(self, symbol: str) -> Optional[str]:
        """
        获取股票所属行业
        
        Args:
            symbol: 股票代码
        
        Returns:
            行业名称，未知返回 None
        """
        # 处理不同格式的代码
        clean_symbol = symbol.upper().replace("US.", "").replace("HK.", "")
        return self._symbol_to_sector.get(clean_symbol)
    
    def check_sector_limit(
        self,
        symbol: str,
        new_position_value: float,
        total_portfolio_value: float,
        current_positions: List[Dict[str, Any]],
    ) -> Tuple[bool, str]:
        """
        检查行业限制
        
        Args:
            symbol: 待交易股票代码
            new_position_value: 新持仓市值
            total_portfolio_value: 账户总资产
            current_positions: 当前持仓列表 [{"symbol": "...", "market_value": ...}]
        
        Returns:
            (是否允许交易，原因)
        """
        sector = self.get_symbol_sector(symbol)
        
        if not sector:
            logger.debug(f"[行业限制] {symbol} 行业未知，跳过检查")
            return True, "行业未知"
        
        # 计算当前行业暴露
        current_sector_exposure = 0.0
        for pos in current_positions:
            pos_symbol = pos.get("symbol", "")
            pos_value = pos.get("market_value", 0)
            if self.get_symbol_sector(pos_symbol) == sector:
                current_sector_exposure += pos_value
        
        # 计算新交易后的行业权重
        new_sector_exposure = current_sector_exposure + new_position_value
        new_sector_weight = new_sector_exposure / total_portfolio_value if total_portfolio_value > 0 else 0
        
        if new_sector_weight > self.max_sector_weight:
            reason = (
                f"行业集中度过高：{sector} 行业权重 {new_sector_weight:.1%} "
                f"> 上限 {self.max_sector_weight:.0%}"
            )
            logger.warning(f"[行业限制] {symbol} 交易被拒绝：{reason}")
            return False, reason
        
        logger.debug(
            f"[行业限制] {symbol} 检查通过 | "
            f"行业={sector} 当前权重={current_sector_exposure/total_portfolio_value:.1%} "
            f"新权重={new_sector_weight:.1%}"
        )
        return True, "检查通过"
    
    def get_sector_exposure_summary(
        self,
        positions: List[Dict[str, Any]],
        total_portfolio_value: float,
    ) -> Dict[str, Any]:
        """
        获取行业暴露摘要
        
        Args:
            positions: 持仓列表
            total_portfolio_value: 账户总资产
        
        Returns:
            行业暴露摘要
        """
        sector_exposures: Dict[str, float] = {}
        
        for pos in positions:
            symbol = pos.get("symbol", "")
            market_value = pos.get("market_value", 0)
            sector = self.get_symbol_sector(symbol)
            
            if sector:
                sector_exposures[sector] = sector_exposures.get(sector, 0) + market_value
        
        # 计算权重
        sector_weights = {}
        for sector, value in sector_exposures.items():
            sector_weights[sector] = value / total_portfolio_value if total_portfolio_value > 0 else 0
        
        return {
            "total_portfolio_value": total_portfolio_value,
            "sector_exposures": sector_exposures,
            "sector_weights": sector_weights,
            "max_sector_weight": self.max_sector_weight,
            "is_concentrated": any(w > self.max_sector_weight for w in sector_weights.values()),
        }
    
    def update_state(self, positions: List[Dict[str, Any]], total_portfolio_value: float):
        """更新内部状态"""
        self.state = SectorLimitsState()
        
        for pos in positions:
            symbol = pos.get("symbol", "")
            market_value = pos.get("market_value", 0)
            sector = self.get_symbol_sector(symbol)
            
            if sector and total_portfolio_value > 0:
                weight = market_value / total_portfolio_value
                self.state.sector_exposures[sector] = (
                    self.state.sector_exposures.get(sector, 0) + weight
                )
                self.state.total_exposure += weight


if __name__ == "__main__":
    # 测试
    logging.basicConfig(level=logging.INFO)
    
    checker = SectorLimitsChecker()
    
    # 测试行业查询
    test_symbols = ["AAPL", "MSFT", "JPM", "BABA", "UNKNOWN"]
    for symbol in test_symbols:
        sector = checker.get_symbol_sector(symbol)
        print(f"{symbol} → {sector}")
    
    # 测试行业限制检查
    positions = [
        {"symbol": "AAPL", "market_value": 10000},
        {"symbol": "MSFT", "market_value": 15000},
    ]
    allowed, reason = checker.check_sector_limit(
        symbol="GOOGL",
        new_position_value=20000,
        total_portfolio_value=100000,
        current_positions=positions,
    )
    print(f"\nGOOGL 交易检查：allowed={allowed}, reason={reason}")
    
    # 获取行业暴露摘要
    summary = checker.get_sector_exposure_summary(positions, 100000)
    print(f"\n行业暴露摘要：{summary}")
