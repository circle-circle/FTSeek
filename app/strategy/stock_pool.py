#!/usr/bin/env python3
"""
股票池分层管理系统

功能:
1. 股票分层 (Tier 1/2/3)
2. 板块归类 (科技/金融/新能源...)
3. 跨市场映射 (港股↔美股 ADR)
4. 动态权重分配

配置:
- config/stock_pool.yaml: 股票池配置
- config/hk_us_mapping.json: 港股 - 美股映射
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

import yaml

logger = logging.getLogger("ftseek.strategy.stock_pool")
logger.propagate = False


class Tier(str, Enum):
    """股票分层枚举"""
    TIER1 = "tier1"   # 核心股票
    TIER2 = "tier2"   # 观察股票
    TIER3 = "tier3"   # 机会股票


class Sector(str, Enum):
    """板块枚举"""
    TECHNOLOGY = "technology"      # 科技
    FINANCE = "finance"            # 金融
    ENERGY = "energy"              # 能源
    EV_AUTO = "ev_auto"            # 新能源汽车
    TELECOM = "telecom"            # 电信
    CONSUMER = "consumer"          # 消费
    HEALTHCARE = "healthcare"      # 医疗
    REAL_ESTATE = "real_estate"    # 房地产
    GAMING = "gaming"              # 博彩
    MATERIALS = "materials"        # 建材
    STEEL = "steel"                # 钢铁
    ENVIRONMENTAL = "environmental" # 环保


@dataclass
class StockInfo:
    """股票信息"""
    code: str
    name: str
    tier: Tier
    sector: Sector
    market: str  # HK/US
    market_cap: float = 0.0  # 市值
    daily_volume: float = 0.0  # 日成交额
    weight: float = 0.0  # 权重
    catalyst: str = ""  # 催化剂
    last_updated: str = ""

    def to_dict(self) -> Dict:
        return {
            'code': self.code,
            'name': self.name,
            'tier': self.tier.value,
            'sector': self.sector.value,
            'market': self.market,
            'market_cap': self.market_cap,
            'daily_volume': self.daily_volume,
            'weight': self.weight,
            'catalyst': self.catalyst,
            'last_updated': self.last_updated,
        }


@dataclass
class CrossMarketPair:
    """跨市场配对 (港股 - 美股 ADR)"""
    hk_code: str
    hk_name: str
    us_code: str
    us_name: str
    correlation: float
    sector: str
    enabled: bool = True

    def to_dict(self) -> Dict:
        return {
            'hk_code': self.hk_code,
            'hk_name': self.hk_name,
            'us_code': self.us_code,
            'us_name': self.us_name,
            'correlation': self.correlation,
            'sector': self.sector,
            'enabled': self.enabled,
        }


class StockPoolManager:
    """
    股票池分层管理器

    管理股票分层、板块归类、跨市场映射

    Example:
        >>> manager = StockPoolManager()
        >>> tier1_stocks = manager.get_tier_stocks(Tier.TIER1)
        >>> tech_stocks = manager.get_sector_stocks(Sector.TECHNOLOGY)
        >>> pair = manager.get_cross_market_pair("HK.00700")
    """

    DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "stock_pool.yaml"
    DEFAULT_MAPPING_PATH = Path(__file__).parent.parent.parent / "config" / "hk_us_mapping.json"

    def __init__(self, config_path: str = None, mapping_path: str = None):
        """
        初始化股票池管理器

        Args:
            config_path: 配置文件路径，默认 config/stock_pool.yaml
            mapping_path: 映射文件路径，默认 config/hk_us_mapping.json
        """
        self.config_path = Path(config_path) if config_path else self.DEFAULT_CONFIG_PATH
        self.mapping_path = Path(mapping_path) if mapping_path else self.DEFAULT_MAPPING_PATH

        # 配置
        self.config = {}

        # 股票分层缓存
        self._tier_stocks: Dict[Tier, List[str]] = {
            Tier.TIER1: [],
            Tier.TIER2: [],
            Tier.TIER3: [],
        }

        # 板块成分缓存
        self._sector_stocks: Dict[Sector, List[str]] = {}

        # 股票信息缓存
        self._stock_info: Dict[str, StockInfo] = {}

        # 跨市场映射
        self._hk_us_mapping: Dict[str, CrossMarketPair] = {}
        self._us_hk_mapping: Dict[str, CrossMarketPair] = {}

        # 加载配置
        self._load_config()
        self._load_mapping()

        logger.info(f"股票池管理器已初始化：{len(self._stock_info)} 只股票")

    def _load_config(self):
        """加载股票池配置"""
        if not self.config_path.exists():
            logger.warning(f"配置文件不存在：{self.config_path}, 使用默认配置")
            self._create_default_config()
            return

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)

            # 解析分层配置
            tiers_config = self.config.get('tiers', {})
            for tier_name, tier_data in tiers_config.items():
                tier = Tier(tier_name)
                stocks = tier_data.get('stocks', [])
                self._tier_stocks[tier] = stocks

                # 创建股票信息
                weight = tier_data.get('weight', 0.0)
                for code in stocks:
                    self._stock_info[code] = StockInfo(
                        code=code,
                        name=code,  # 名称待填充
                        tier=tier,
                        sector=Sector.TECHNOLOGY,  # 默认
                        market='HK' if code.startswith('HK.') else 'US',
                        weight=weight,
                        last_updated=datetime.now().isoformat()
                    )

            # 解析板块配置
            sectors_config = self.config.get('sectors', {})
            for sector_name, sector_data in sectors_config.items():
                sector = Sector(sector_name)
                stocks = sector_data.get('stocks', [])
                self._sector_stocks[sector] = stocks

                # 更新股票板块信息
                for code in stocks:
                    if code in self._stock_info:
                        self._stock_info[code].sector = sector

            logger.info(f"加载股票池配置：Tier1={len(self._tier_stocks[Tier.TIER1])}, "
                       f"Tier2={len(self._tier_stocks[Tier.TIER2])}, "
                       f"Tier3={len(self._tier_stocks[Tier.TIER3])}")

        except Exception as e:
            logger.error(f"加载配置文件失败：{e}")
            self._create_default_config()

    def _create_default_config(self):
        """创建默认配置"""
        self.config = {
            'version': '2.0',
            'tiers': {
                'tier1': {
                    'name': '核心股票',
                    'weight': 0.50,
                    'stocks': [
                        'HK.00700',  # 腾讯
                        'HK.03690',  # 美团
                        'HK.09988',  # 阿里
                    ]
                },
                'tier2': {
                    'name': '观察股票',
                    'weight': 0.30,
                    'stocks': [
                        'HK.09618',  # 京东
                        'HK.01810',  # 小米
                    ]
                },
                'tier3': {
                    'name': '机会股票',
                    'weight': 0.20,
                    'stocks': []
                }
            },
            'sectors': {
                'technology': {
                    'name': '科技',
                    'stocks': ['HK.00700', 'HK.03690', 'HK.09988']
                },
                'finance': {
                    'name': '金融',
                    'stocks': ['HK.00005', 'HK.02318', 'HK.03968']
                }
            }
        }

        # 保存默认配置
        with open(self.config_path, 'w', encoding='utf-8') as f:
            yaml.dump(self.config, f, allow_unicode=True, default_flow_style=False)

        logger.info(f"已创建默认配置文件：{self.config_path}")

    def _load_mapping(self):
        """加载跨市场映射"""
        if not self.mapping_path.exists():
            logger.warning(f"映射文件不存在：{self.mapping_path}")
            return

        try:
            with open(self.mapping_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            mapping_list = data.get('mapping', [])
            for item in mapping_list:
                pair = CrossMarketPair(
                    hk_code=item['hk_stock'],
                    hk_name=item['hk_name'],
                    us_code=item['us_stock'],
                    us_name=item['us_name'],
                    correlation=item.get('correlation', 0.0),
                    sector=item.get('sector', 'unknown'),
                    enabled=item.get('enabled', False)
                )

                self._hk_us_mapping[pair.hk_code] = pair
                self._us_hk_mapping[pair.us_code] = pair

            logger.info(f"加载跨市场映射：{len(self._hk_us_mapping)} 对")

        except Exception as e:
            logger.error(f"加载映射文件失败：{e}")

    # ========== 分层查询 ==========

    def get_tier_stocks(self, tier: Tier) -> List[str]:
        """
        获取指定分层的股票列表

        Args:
            tier: 分层枚举

        Returns:
            股票代码列表
        """
        return self._tier_stocks.get(tier, [])

    def get_tier1_stocks(self) -> List[str]:
        """获取核心股票 (Tier 1)"""
        return self._tier_stocks[Tier.TIER1]

    def get_tier2_stocks(self) -> List[str]:
        """获取观察股票 (Tier 2)"""
        return self._tier_stocks[Tier.TIER2]

    def get_tier3_stocks(self) -> List[str]:
        """获取机会股票 (Tier 3)"""
        return self._tier_stocks[Tier.TIER3]

    def get_stock_tier(self, code: str) -> Optional[Tier]:
        """
        获取股票所属分层

        Args:
            code: 股票代码

        Returns:
            分层枚举，不存在返回 None
        """
        for tier, stocks in self._tier_stocks.items():
            if code in stocks:
                return tier
        return None

    # ========== 板块查询 ==========

    def get_sector_stocks(self, sector: Sector) -> List[str]:
        """
        获取指定板块的股票列表

        Args:
            sector: 板块枚举

        Returns:
            股票代码列表
        """
        return self._sector_stocks.get(sector, [])

    def get_stock_sector(self, code: str) -> Optional[Sector]:
        """
        获取股票所属板块

        Args:
            code: 股票代码

        Returns:
            板块枚举，不存在返回 None
        """
        if code in self._stock_info:
            return self._stock_info[code].sector
        return None

    def get_all_sectors(self) -> List[Sector]:
        """获取所有板块"""
        return list(self._sector_stocks.keys())

    # ========== 跨市场查询 ==========

    def get_hk_us_pair(self, hk_code: str) -> Optional[CrossMarketPair]:
        """
        获取港股对应的美股 ADR

        Args:
            hk_code: 港股代码

        Returns:
            跨市场配对，不存在返回 None
        """
        return self._hk_us_mapping.get(hk_code)

    def get_us_hk_pair(self, us_code: str) -> Optional[CrossMarketPair]:
        """
        获取美股对应的港股

        Args:
            us_code: 美股代码

        Returns:
            跨市场配对，不存在返回 None
        """
        return self._us_hk_mapping.get(us_code)

    def get_enabled_correlation_pairs(self) -> List[CrossMarketPair]:
        """
        获取启用的跨市场配对 (相关性>=0.6)

        Returns:
            配对列表
        """
        return [pair for pair in self._hk_us_mapping.values() if pair.enabled]

    # ========== 股票信息管理 ==========

    def get_stock_info(self, code: str) -> Optional[StockInfo]:
        """
        获取股票详细信息

        Args:
            code: 股票代码

        Returns:
            股票信息，不存在返回 None
        """
        return self._stock_info.get(code)

    def update_stock_info(self, code: str, **kwargs):
        """
        更新股票信息

        Args:
            code: 股票代码
            **kwargs: 更新字段
        """
        if code not in self._stock_info:
            # 创建新条目
            self._stock_info[code] = StockInfo(
                code=code,
                name=code,
                tier=Tier.TIER3,
                sector=Sector.TECHNOLOGY,
                market='HK' if code.startswith('HK.') else 'US'
            )

        info = self._stock_info[code]
        for key, value in kwargs.items():
            if hasattr(info, key):
                setattr(info, key, value)

        info.last_updated = datetime.now().isoformat()

    def set_stock_catalyst(self, code: str, catalyst: str):
        """
        设置股票催化剂

        Args:
            code: 股票代码
            catalyst: 催化剂描述
        """
        self.update_stock_info(code, catalyst=catalyst)

    def update_stock_metrics(self, code: str, market_cap: float = None,
                            daily_volume: float = None):
        """
        更新股票指标

        Args:
            code: 股票代码
            market_cap: 市值
            daily_volume: 日成交额
        """
        updates = {}
        if market_cap is not None:
            updates['market_cap'] = market_cap
        if daily_volume is not None:
            updates['daily_volume'] = daily_volume

        if updates:
            self.update_stock_info(code, **updates)

    # ========== 动态分层调整 ==========

    def promote_to_tier1(self, code: str):
        """
        将股票提升至 Tier 1

        Args:
            code: 股票代码
        """
        self._demote_stock(code)
        self._tier_stocks[Tier.TIER1].append(code)
        self.update_stock_info(code, tier=Tier.TIER1)
        logger.info(f"{code} 已提升至 Tier 1")

    def demote_to_tier2(self, code: str):
        """
        将股票降级至 Tier 2

        Args:
            code: 股票代码
        """
        self._demote_stock(code)
        self._tier_stocks[Tier.TIER2].append(code)
        self.update_stock_info(code, tier=Tier.TIER2)
        logger.info(f"{code} 已降级至 Tier 2")

    def add_to_tier3(self, code: str, catalyst: str = ""):
        """
        添加股票至 Tier 3 (机会股票)

        Args:
            code: 股票代码
            catalyst: 催化剂
        """
        if code not in self._tier_stocks[Tier.TIER3]:
            self._tier_stocks[Tier.TIER3].append(code)
            self.update_stock_info(code, tier=Tier.TIER3, catalyst=catalyst)
            logger.info(f"{code} 已添加至 Tier 3")

    def _demote_stock(self, code: str):
        """从当前分层移除"""
        for tier in Tier:
            if code in self._tier_stocks[tier]:
                self._tier_stocks[tier].remove(code)

    # ========== 权重分配 ==========

    def get_target_weights(self) -> Dict[str, float]:
        """
        获取目标权重分配

        Returns:
            {code: weight} 字典
        """
        weights = {}

        for tier, stocks in self._tier_stocks.items():
            if not stocks:
                continue

            tier_config = self.config.get('tiers', {}).get(tier.value, {})
            tier_weight = tier_config.get('weight', 0.0)

            # 平均分配
            per_stock_weight = tier_weight / len(stocks) if stocks else 0

            for code in stocks:
                weights[code] = per_stock_weight

        return weights

    def get_position_limit(self, code: str, total_capital: float) -> float:
        """
        计算股票仓位上限

        Args:
            code: 股票代码
            total_capital: 总资金

        Returns:
            仓位上限金额
        """
        tier = self.get_stock_tier(code)
        if not tier:
            return 0

        tier_config = self.config.get('tiers', {}).get(tier.value, {})
        tier_weight = tier_config.get('weight', 0.0)
        stocks_in_tier = len(self._tier_stocks[tier])

        if stocks_in_tier == 0:
            return 0

        # 单只股票上限 = 分层权重 / 分层股票数
        per_stock_limit = (tier_weight / stocks_in_tier) * total_capital

        return per_stock_limit

    # ========== 统计与报告 ==========

    def get_summary(self) -> Dict:
        """获取股票池摘要"""
        return {
            'total_stocks': len(self._stock_info),
            'tier1_count': len(self._tier_stocks[Tier.TIER1]),
            'tier2_count': len(self._tier_stocks[Tier.TIER2]),
            'tier3_count': len(self._tier_stocks[Tier.TIER3]),
            'sectors': list(self._sector_stocks.keys()),
            'correlation_pairs': len(self.get_enabled_correlation_pairs()),
        }

    def print_summary(self):
        """打印股票池摘要"""
        summary = self.get_summary()

        print("\n" + "=" * 60)
        print("📊 股票池摘要")
        print("=" * 60)
        print(f"总股票数：{summary['total_stocks']}")
        print(f"Tier 1 (核心): {summary['tier1_count']}")
        print(f"Tier 2 (观察): {summary['tier2_count']}")
        print(f"Tier 3 (机会): {summary['tier3_count']}")
        print(f"板块数：{len(summary['sectors'])}")
        print(f"跨市场配对：{summary['correlation_pairs']} 对")
        print("=" * 60)

        # 各分层股票列表
        for tier in Tier:
            stocks = self.get_tier_stocks(tier)
            if stocks:
                print(f"\n{tier.value.upper()}: {', '.join(stocks[:10])}{'...' if len(stocks) > 10 else ''}")

        print()


# 全局单例
_pool_manager: Optional[StockPoolManager] = None


def get_stock_pool_manager(config_path: str = None,
                          mapping_path: str = None) -> StockPoolManager:
    """获取股票池管理器单例"""
    global _pool_manager
    if _pool_manager is None:
        _pool_manager = StockPoolManager(config_path, mapping_path)
    return _pool_manager


def init_stock_pool_manager(config_path: str = None,
                           mapping_path: str = None) -> StockPoolManager:
    """初始化股票池管理器"""
    global _pool_manager
    _pool_manager = StockPoolManager(config_path, mapping_path)
    return _pool_manager


if __name__ == "__main__":
    # 测试
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    manager = get_stock_pool_manager()
    manager.print_summary()

    print("\n跨市场配对示例:")
    pair = manager.get_hk_us_pair("HK.00700")
    if pair:
        print(f"  {pair.hk_name} ({pair.hk_code}) ↔ {pair.us_name} ({pair.us_code})")
        print(f"  相关性：{pair.correlation:.2f}, 启用：{pair.enabled}")
