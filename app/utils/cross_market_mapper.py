"""
跨市场映射模块 (Cross-Market Mapper)

功能:
1. 加载美股 → 港股映射关系
2. 根据美股代码查找受影响的港股
3. 根据港股代码查找关联的美股
4. 提供盘前分析用的美股行情上下文
5. 支持跨市场套利配对查询

更新时间: 2026-04-13
"""

import logging
import yaml
import os
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("ftseek.cross_market")

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "cross_market_mapping.yaml"
)


@dataclass
class USImpactInfo:
    """美股影响信息"""
    us_code: str
    hk_codes: List[str]
    impact_type: str  # "direct" | "sector" | "sentiment"
    description: str
    correlation: float


@dataclass
class ArbPair:
    """跨市场套利配对"""
    us_code: str
    hk_code: str
    ratio_threshold: float
    description: str


@dataclass
class USMarketData:
    """美股行情数据 (用于注入 LLM prompt)"""
    code: str
    name: str
    last_price: float = 0.0
    prev_close: float = 0.0
    change_pct: float = 0.0
    volume: str = ""
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    rsi_14: Optional[float] = None


class CrossMarketMapper:
    """跨市场映射管理器"""

    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = config_path
        self._us_watch_list: List[str] = []
        self._direct_mapping: Dict[str, USImpactInfo] = {}
        self._index_impact: Dict[str, Dict] = {}
        self._arb_pairs: List[ArbPair] = []
        self._us_subscription: Dict = {}

        # 反向映射: hk_code -> [us_codes]
        self._reverse_mapping: Dict[str, List[str]] = {}

        self._load_config()
        self._build_reverse_mapping()

    def _load_config(self):
        """加载跨市场映射配置"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            # 美股监控名单
            self._us_watch_list = config.get('us_watch_list', [])

            # 直接映射
            for us_code, info in config.get('direct_mapping', {}).items():
                self._direct_mapping[us_code] = USImpactInfo(
                    us_code=us_code,
                    hk_codes=info.get('hk_codes', []),
                    impact_type=info.get('impact_type', 'sector'),
                    description=info.get('description', ''),
                    correlation=info.get('correlation', 0.5),
                )

            # 指数影响
            self._index_impact = config.get('index_impact', {})

            # 套利配对
            for pair in config.get('arb_pairs', []):
                self._arb_pairs.append(ArbPair(
                    us_code=pair['us_code'],
                    hk_code=pair['hk_code'],
                    ratio_threshold=pair.get('ratio_threshold', 0.02),
                    description=pair.get('description', ''),
                ))

            # 订阅配置
            self._us_subscription = config.get('us_subscription', {})

            logger.info(
                f"[CrossMarketMapper] 配置已加载: "
                f"{len(self._us_watch_list)} 只美股, "
                f"{len(self._direct_mapping)} 条直接映射, "
                f"{len(self._arb_pairs)} 对套利配对"
            )

        except Exception as e:
            logger.error(f"[CrossMarketMapper] 配置加载失败: {e}")

    def _build_reverse_mapping(self):
        """构建反向映射: hk_code -> [us_codes]"""
        for us_code, info in self._direct_mapping.items():
            for hk_code in info.hk_codes:
                if hk_code not in self._reverse_mapping:
                    self._reverse_mapping[hk_code] = []
                self._reverse_mapping[hk_code].append(us_code)

        logger.info(
            f"[CrossMarketMapper] 反向映射已构建: "
            f"{len(self._reverse_mapping)} 只港股有关联美股"
        )

    # ============================================================
    #  查询接口
    # ============================================================

    def get_hk_codes_by_us(self, us_code: str) -> List[str]:
        """根据美股代码获取受影响的港股列表"""
        info = self._direct_mapping.get(us_code)
        if info:
            return info.hk_codes.copy()

        # 检查指数影响
        index_info = self._index_impact.get(us_code)
        if index_info:
            impact = index_info.get('impact', [])
            if isinstance(impact, list):
                return impact.copy()
            elif impact == "all":
                # 返回所有有映射的港股
                all_hk = set()
                for info in self._direct_mapping.values():
                    all_hk.update(info.hk_codes)
                return list(all_hk)

        return []

    def get_us_codes_by_hk(self, hk_code: str) -> List[str]:
        """根据港股代码获取关联的美股列表"""
        return self._reverse_mapping.get(hk_code, []).copy()

    def get_impact_info(self, us_code: str) -> Optional[USImpactInfo]:
        """获取美股影响信息"""
        return self._direct_mapping.get(us_code)

    def get_arb_pairs(self) -> List[ArbPair]:
        """获取所有套利配对"""
        return self._arb_pairs.copy()

    def get_arb_pair_by_hk(self, hk_code: str) -> Optional[ArbPair]:
        """根据港股代码查找套利配对"""
        for pair in self._arb_pairs:
            if pair.hk_code == hk_code:
                return pair
        return None

    def get_arb_pair_by_us(self, us_code: str) -> Optional[ArbPair]:
        """根据美股代码查找套利配对"""
        for pair in self._arb_pairs:
            if pair.us_code == us_code:
                return pair
        return None

    def get_us_watch_list(self) -> List[str]:
        """获取美股监控名单"""
        return self._us_watch_list.copy()

    def get_core_us_codes(self) -> List[str]:
        """获取核心必订阅美股代码"""
        return self._us_subscription.get('core_codes', [])

    def get_us_subscribe_types(self) -> List[str]:
        """获取美股订阅类型"""
        return self._us_subscription.get('subscribe_types', ['QUOTE'])

    # ============================================================
    #  盘前分析辅助
    # ============================================================

    def get_us_market_context(self, us_data_map: Dict[str, dict]) -> str:
        """
        构建美股市场上下文 (用于注入 LLM prompt)

        Args:
            us_data_map: {us_code: {last_price, change_pct, ...}}

        Returns:
            格式化的美股市场上下文字符串
        """
        lines = ["## 美股市场上下文"]

        # 大盘指数
        for idx_code in ["US.SPY", "US.QQQ", "US.DIA", "US.UVXY"]:
            if idx_code in us_data_map:
                d = us_data_map[idx_code]
                name = {"US.SPY": "S&P 500", "US.QQQ": "纳斯达克 100",
                        "US.DIA": "道琼斯", "US.UVXY": "VIX 短期期货 ETF"}.get(idx_code, idx_code)
                lines.append(
                    f"- **{name}**: {d.get('last_price', 'N/A')} "
                    f"({d.get('change_pct', 0):+.2f}%)"
                )

        lines.append("")
        lines.append("### 重点个股")

        # 直接映射的美股
        for us_code, info in self._direct_mapping.items():
            if us_code in us_data_map:
                d = us_data_map[us_code]
                hk_list = ", ".join(info.hk_codes[:3])
                lines.append(
                    f"- **{us_code}**: {d.get('last_price', 'N/A')} "
                    f"({d.get('change_pct', 0):+.2f}%) "
                    f"→ 影响 [{hk_list}] ({info.impact_type})"
                )

        return "\n".join(lines)

    def get_affected_hk_for_news(self, mentioned_us_codes: List[str]) -> Dict[str, List[str]]:
        """
        根据新闻中提到的美股，找出受影响的港股

        Args:
            mentioned_us_codes: 新闻中提到的美股代码列表

        Returns:
            {us_code: [affected_hk_codes]}
        """
        result = {}
        for us_code in mentioned_us_codes:
            hk_codes = self.get_hk_codes_by_us(us_code)
            if hk_codes:
                result[us_code] = hk_codes
        return result

    def is_cross_market_code(self, code: str) -> bool:
        """判断代码是否在跨市场映射中"""
        return code in self._direct_mapping or code in self._reverse_mapping


# ============================================================
#  全局单例
# ============================================================
_mapper: Optional[CrossMarketMapper] = None


def get_cross_market_mapper(config_path: str = None) -> CrossMarketMapper:
    """获取跨市场映射器单例"""
    global _mapper
    if _mapper is None:
        if config_path:
            _mapper = CrossMarketMapper(config_path)
        else:
            _mapper = CrossMarketMapper()
    return _mapper


def reset_mapper():
    """重置单例 (用于测试)"""
    global _mapper
    _mapper = None
