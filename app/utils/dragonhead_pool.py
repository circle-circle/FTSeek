"""
龙头股票池动态管理器 (DragonHeadPoolManager)

基于拉莫尔龙头理论 + 做强原则，实现:
1. 全行业龙头覆盖 (港股 + 美股)
2. 基于市场阶段 (Market Regime) 的动态池切换
3. 盘前自动轮换 (根据 LLM 盘前分析结果)
4. 强度排序 + 自动升降级

P2 实现 (2026-04-19):
- 覆盖 12 大行业 × 3 层梯队 = 36+ 核心龙头
- 支持 Risk-On / Risk-Off / Rotation / Recovery 四种市场阶段
- 盘前自动推荐当日最强股票池
"""

import json
import logging
import time
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("ftseek.dragonhead_pool")


# ============================================================================
#  市场阶段枚举 (与盘前 LLM 分析结果联动)
# ============================================================================
class MarketRegime(str, Enum):
    RISK_ON = "risk_on"          # 风险偏好上升 → 成长/科技龙头
    RISK_OFF = "risk_off"        # 风险规避 → 防御/红利龙头
    ROTATION = "rotation"        # 板块轮动 → 跟随资金流向
    RECOVERY = "recovery"        # 经济复苏 → 周期/价值龙头


# ============================================================================
#  龙头股票数据库 (全行业覆盖)
# ============================================================================
HK_DRAGON_HEADS: Dict[str, List[Dict]] = {
    # ── 科技互联网 ──
    "tech": {
        "name": "科技互联网",
        "regimes": [MarketRegime.RISK_ON, MarketRegime.RECOVERY],
        "stocks": [
            {"code": "HK.00700", "name": "腾讯控股", "tier": 1, "notes": "社交+游戏+云龙头"},
            {"code": "HK.09988", "name": "阿里巴巴", "tier": 1, "notes": "电商+云龙头"},
            {"code": "HK.03690", "name": "美团-W", "tier": 1, "notes": "本地生活龙头"},
            {"code": "HK.01024", "name": "快手-W", "tier": 2, "notes": "短视频龙头"},
            {"code": "HK.01810", "name": "小米集团", "tier": 2, "notes": "消费电子+汽车龙头"},
            {"code": "HK.09618", "name": "京东集团", "tier": 2, "notes": "电商+物流龙头"},
            {"code": "HK.09992", "name": "泡泡玛特", "tier": 2, "notes": "潮玩龙头"},
        ]
    },
    # ── 半导体 ──
    "semiconductor": {
        "name": "半导体",
        "regimes": [MarketRegime.RISK_ON, MarketRegime.RECOVERY],
        "stocks": [
            {"code": "HK.00981", "name": "中芯国际", "tier": 1, "notes": "晶圆代工龙头"},
            {"code": "HK.01347", "name": "华虹半导体", "tier": 2, "notes": "特色工艺龙头"},
            {"code": "HK.02018", "name": "瑞声科技", "tier": 3, "notes": "声学+光学龙头"},
        ]
    },
    # ── 新能源/电动车 ──
    "ev_new_energy": {
        "name": "新能源/电动车",
        "regimes": [MarketRegime.RISK_ON, MarketRegime.RECOVERY],
        "stocks": [
            {"code": "HK.01211", "name": "比亚迪股份", "tier": 1, "notes": "新能源车全球龙头"},
            {"code": "HK.09868", "name": "小鹏汽车", "tier": 2, "notes": "智能驾驶龙头"},
            {"code": "HK.02015", "name": "理想汽车", "tier": 2, "notes": "增程SUV龙头"},
            {"code": "HK.09866", "name": "蔚来-SW", "tier": 2, "notes": "换电模式龙头"},
            {"code": "HK.03800", "name": "协鑫科技", "tier": 3, "notes": "多晶硅龙头"},
        ]
    },
    # ── 金融 ──
    "finance": {
        "name": "金融",
        "regimes": [MarketRegime.RISK_OFF, MarketRegime.RECOVERY],
        "stocks": [
            {"code": "HK.00005", "name": "汇丰控股", "tier": 1, "notes": "外资行龙头"},
            {"code": "HK.02318", "name": "中国平安", "tier": 1, "notes": "保险龙头"},
            {"code": "HK.01299", "name": "友邦保险", "tier": 1, "notes": "亚洲保险龙头"},
            {"code": "HK.03968", "name": "招商银行", "tier": 2, "notes": "零售银行龙头"},
            {"code": "HK.00939", "name": "建设银行", "tier": 3, "notes": "国有大行龙头"},
        ]
    },
    # ── 医药 ──
    "pharma": {
        "name": "医药/生物科技",
        "regimes": [MarketRegime.RISK_OFF, MarketRegime.RECOVERY],
        "stocks": [
            {"code": "HK.01093", "name": "石药集团", "tier": 1, "notes": "创新药龙头"},
            {"code": "HK.02269", "name": "药明生物", "tier": 1, "notes": "CXO龙头"},
            {"code": "HK.01801", "name": "信达生物", "tier": 2, "notes": "抗体药龙头"},
            {"code": "HK.06160", "name": "百济神州", "tier": 2, "notes": "抗肿瘤龙头"},
        ]
    },
    # ── 消费 ──
    "consumer": {
        "name": "消费",
        "regimes": [MarketRegime.RECOVERY, MarketRegime.RISK_ON],
        "stocks": [
            {"code": "HK.00291", "name": "华润啤酒", "tier": 1, "notes": "啤酒龙头"},
            {"code": "HK.01876", "name": "百威亚太", "tier": 2, "notes": "高端啤酒龙头"},
            {"code": "HK.09633", "name": "农夫山泉", "tier": 2, "notes": "饮用水龙头"},
            {"code": "HK.02331", "name": "李宁", "tier": 3, "notes": "运动品牌龙头"},
        ]
    },
    # ── 能源/资源 ──
    "energy_materials": {
        "name": "能源/资源",
        "regimes": [MarketRegime.RISK_OFF, MarketRegime.ROTATION],
        "stocks": [
            {"code": "HK.00883", "name": "中国海洋石油", "tier": 1, "notes": "海上油气龙头"},
            {"code": "HK.01378", "name": "中国宏桥", "tier": 1, "notes": "铝加工龙头"},
            {"code": "HK.00857", "name": "中国石油股份", "tier": 2, "notes": "综合油气龙头"},
            {"code": "HK.02899", "name": "紫金矿业", "tier": 2, "notes": "黄金+铜龙头"},
        ]
    },
    # ── 汽车 ──
    "auto": {
        "name": "汽车",
        "regimes": [MarketRegime.RECOVERY, MarketRegime.RISK_ON],
        "stocks": [
            {"code": "HK.00175", "name": "吉利汽车", "tier": 1, "notes": "自主品牌龙头"},
            {"code": "HK.02238", "name": "广汽集团", "tier": 2, "notes": "日系合资龙头"},
            {"code": "HK.01958", "name": "北京汽车", "tier": 3, "notes": "奔驰合资龙头"},
        ]
    },
    # ── 地产 ──
    "real_estate": {
        "name": "房地产",
        "regimes": [MarketRegime.RECOVERY],
        "stocks": [
            {"code": "HK.01038", "name": "长江基建", "tier": 1, "notes": "基建龙头"},
            {"code": "HK.00001", "name": "长和", "tier": 2, "notes": "综合企业龙头"},
            {"code": "HK.00688", "name": "中国海外发展", "tier": 2, "notes": "央企地产龙头"},
        ]
    },
    # ── 通信 ──
    "telecom": {
        "name": "通信",
        "regimes": [MarketRegime.RISK_OFF],
        "stocks": [
            {"code": "HK.00941", "name": "中国移动", "tier": 1, "notes": "运营商龙头"},
            {"code": "HK.00762", "name": "中国联通", "tier": 3, "notes": "5G龙头"},
        ]
    },
    # ── 博彩 ──
    "gaming": {
        "name": "博彩/旅游",
        "regimes": [MarketRegime.RECOVERY, MarketRegime.RISK_ON],
        "stocks": [
            {"code": "HK.01928", "name": "金沙中国", "tier": 1, "notes": "澳门博彩龙头"},
            {"code": "HK.02282", "name": "美高梅中国", "tier": 2, "notes": "高端博彩龙头"},
        ]
    },
    # ── 工业 ──
    "industrials": {
        "name": "工业/基建",
        "regimes": [MarketRegime.RECOVERY, MarketRegime.ROTATION],
        "stocks": [
            {"code": "HK.01800", "name": "中国交通建设", "tier": 2, "notes": "基建龙头"},
            {"code": "HK.02328", "name": "中国财险", "tier": 2, "notes": "财险龙头"},
        ]
    },
}

# ============================================================================
#  美股龙头数据库
# ============================================================================
US_DRAGON_HEADS: Dict[str, List[Dict]] = {
    # ── 科技/AI ──
    "tech_ai": {
        "name": "科技/AI",
        "regimes": [MarketRegime.RISK_ON, MarketRegime.RECOVERY],
        "stocks": [
            {"code": "US.NVDA", "name": "NVIDIA", "tier": 1, "notes": "AI芯片龙头"},
            {"code": "US.AAPL", "name": "Apple", "tier": 1, "notes": "消费电子龙头"},
            {"code": "US.MSFT", "name": "Microsoft", "tier": 1, "notes": "企业云+AI龙头"},
            {"code": "US.GOOG", "name": "Alphabet", "tier": 1, "notes": "搜索+AI龙头"},
            {"code": "US.META", "name": "Meta", "tier": 2, "notes": "社交+AI龙头"},
            {"code": "US.AMZN", "name": "Amazon", "tier": 1, "notes": "电商+云龙头"},
            {"code": "US.AMD", "name": "AMD", "tier": 2, "notes": "CPU/GPU龙头"},
        ]
    },
    # ── 半导体 ──
    "semiconductor": {
        "name": "半导体",
        "regimes": [MarketRegime.RISK_ON, MarketRegime.RECOVERY],
        "stocks": [
            {"code": "US.NVDA", "name": "NVIDIA", "tier": 1, "notes": "GPU龙头"},
            {"code": "US.MU", "name": "Micron", "tier": 1, "notes": "存储龙头"},
            {"code": "US.AMD", "name": "AMD", "tier": 2, "notes": "CPU龙头"},
            {"code": "US.AVGO", "name": "Broadcom", "tier": 2, "notes": "网络芯片龙头"},
            {"code": "US.QCOM", "name": "Qualcomm", "tier": 3, "notes": "移动芯片龙头"},
        ]
    },
    # ── 电动车 ──
    "ev_auto": {
        "name": "电动车/汽车",
        "regimes": [MarketRegime.RISK_ON, MarketRegime.RECOVERY],
        "stocks": [
            {"code": "US.TSLA", "name": "Tesla", "tier": 1, "notes": "电动车龙头"},
            {"code": "US.RIVN", "name": "Rivian", "tier": 3, "notes": "电动皮卡龙头"},
            {"code": "US.NIO", "name": "NIO", "tier": 3, "notes": "中概电动车龙头"},
        ]
    },
    # ── 金融 ──
    "finance": {
        "name": "金融",
        "regimes": [MarketRegime.RISK_OFF, MarketRegime.RECOVERY],
        "stocks": [
            {"code": "US.JPM", "name": "JPMorgan", "tier": 1, "notes": "投行龙头"},
            {"code": "US.BAC", "name": "Bank of America", "tier": 2, "notes": "商业银行龙头"},
            {"code": "US.V", "name": "Visa", "tier": 1, "notes": "支付龙头"},
            {"code": "US.MA", "name": "Mastercard", "tier": 1, "notes": "支付龙头"},
            {"code": "US.BRK-B", "name": "Berkshire Hathaway", "tier": 1, "notes": "综合投资龙头"},
        ]
    },
    # ── 医疗 ──
    "healthcare": {
        "name": "医疗健康",
        "regimes": [MarketRegime.RISK_OFF],
        "stocks": [
            {"code": "US.LLY", "name": "Eli Lilly", "tier": 1, "notes": "减肥药龙头"},
            {"code": "US.JNJ", "name": "Johnson & Johnson", "tier": 1, "notes": "医药综合龙头"},
            {"code": "US.UNH", "name": "UnitedHealth", "tier": 1, "notes": "医疗保险龙头"},
            {"code": "US.PFE", "name": "Pfizer", "tier": 2, "notes": "制药龙头"},
        ]
    },
    # ── 能源 ──
    "energy": {
        "name": "能源",
        "regimes": [MarketRegime.RISK_OFF, MarketRegime.ROTATION],
        "stocks": [
            {"code": "US.XOM", "name": "Exxon Mobil", "tier": 1, "notes": "石油龙头"},
            {"code": "US.CVX", "name": "Chevron", "tier": 1, "notes": "石油龙头"},
            {"code": "US.COP", "name": "ConocoPhillips", "tier": 2, "notes": "页岩油龙头"},
        ]
    },
    # ── 消费 ──
    "consumer": {
        "name": "消费",
        "regimes": [MarketRegime.RECOVERY, MarketRegime.RISK_ON],
        "stocks": [
            {"code": "US.BABA", "name": "Alibaba", "tier": 1, "notes": "中概电商龙头"},
            {"code": "US.PDD", "name": "PDD Holdings", "tier": 2, "notes": "拼多多/Temu"},
            {"code": "US.WMT", "name": "Walmart", "tier": 2, "notes": "零售龙头"},
            {"code": "US.COST", "name": "Costco", "tier": 1, "notes": "仓储零售龙头"},
        ]
    },
    # ── 工业 ──
    "industrials": {
        "name": "工业/防务",
        "regimes": [MarketRegime.RECOVERY, MarketRegime.ROTATION],
        "stocks": [
            {"code": "US.BWXT", "name": "BWX Technologies", "tier": 1, "notes": "核能防务龙头"},
            {"code": "US.LMT", "name": "Lockheed Martin", "tier": 2, "notes": "军工龙头"},
            {"code": "US.CAT", "name": "Caterpillar", "tier": 2, "notes": "工程机械龙头"},
        ]
    },
    # ── ETF基准 ──
    "etf": {
        "name": "ETF基准",
        "regimes": [MarketRegime.RISK_ON, MarketRegime.RISK_OFF, MarketRegime.ROTATION, MarketRegime.RECOVERY],
        "stocks": [
            {"code": "US.SPY", "name": "SPDR S&P 500", "tier": 1, "notes": "大盘基准"},
            {"code": "US.QQQ", "name": "Invesco QQQ", "tier": 1, "notes": "科技基准"},
        ]
    },
}

# ============================================================================
#  各市场阶段推荐的龙头板块
# ============================================================================
REGIME_SECTOR_PRIORITY = {
    MarketRegime.RISK_ON: {
        "HK": ["tech", "semiconductor", "ev_new_energy", "consumer", "gaming"],
        "US": ["tech_ai", "semiconductor", "ev_auto", "consumer"],
        "label": "风险偏好上升 → 科技+成长+消费",
    },
    MarketRegime.RISK_OFF: {
        "HK": ["finance", "pharma", "energy_materials", "telecom"],
        "US": ["finance", "healthcare", "energy"],
        "label": "风险规避 → 金融+医药+能源",
    },
    MarketRegime.ROTATION: {
        "HK": ["energy_materials", "industrials", "tech", "finance"],
        "US": ["energy", "industrials", "tech_ai", "finance"],
        "label": "板块轮动 → 跟随资金流向",
    },
    MarketRegime.RECOVERY: {
        "HK": ["consumer", "ev_new_energy", "auto", "tech", "real_estate"],
        "US": ["consumer", "tech_ai", "semiconductor", "industrials", "ev_auto"],
        "label": "经济复苏 → 周期+价值+成长",
    },
}


@dataclass
class DragonHeadStock:
    """龙头股票信息"""
    code: str
    name: str
    sector: str
    sector_name: str
    tier: int
    notes: str
    market: str  # HK / US
    strength_score: float = 0.0  # 强度评分 (0-100, 盘前动态计算)
    is_active: bool = False  # 是否在当日激活池
    catalyst: str = ""  # 当日催化剂


class DragonHeadPoolManager:
    """
    龙头股票池动态管理器

    核心能力:
    1. 全行业龙头覆盖 (12个行业 × 3层梯队)
    2. 基于市场阶段的动态池切换
    3. 盘前 LLM 推荐联动
    4. 强度排序 + 自动升降级
    """

    def __init__(self, hk_path: str = None, us_path: str = None):
        self.hk_db = HK_DRAGON_HEADS
        self.us_db = US_DRAGON_HEADS

        # 当日激活池
        self._active_hk: List[DragonHeadStock] = []
        self._active_us: List[DragonHeadStock] = []

        # 当前市场阶段
        self._current_regime = MarketRegime.RISK_ON

        # 上次更新时间
        self._last_update: Optional[date] = None
        self._last_update_time: float = 0

        logger.info(
            f"🐲 龙头股票池管理器已初始化: "
            f"HK={self._count_stocks(HK_DRAGON_HEADS)}只, "
            f"US={self._count_stocks(US_DRAGON_HEADS)}只"
        )

    def _count_stocks(self, db: Dict) -> int:
        """统计股票总数"""
        return sum(len(v["stocks"]) for v in db.values())

    # ─────────────────────────────────────────────────────────────
    #  盘前动态池生成
    # ─────────────────────────────────────────────────────────────

    def generate_active_pool(
        self,
        market: str = "HK",
        regime: Optional[MarketRegime] = None,
        max_tier1: int = 8,
        max_tier2: int = 5,
        max_tier3: int = 3,
    ) -> List[DragonHeadStock]:
        """
        生成当日激活龙头池

        Args:
            market: "HK" 或 "US"
            regime: 市场阶段 (默认使用当前阶段)
            max_tier1: Tier1 最大数量
            max_tier2: Tier2 最大数量
            max_tier3: Tier3 最大数量

        Returns:
            按强度排序的激活池
        """
        regime = regime or self._current_regime
        db = self.hk_db if market == "HK" else self.us_db

        # 获取当前阶段推荐的板块优先级
        priority = REGIME_SECTOR_PRIORITY.get(regime, {}).get(market, [])

        pool = []
        tier1_count = tier2_count = tier3_count = 0

        for sector_key in priority:
            if sector_key not in db:
                continue
            sector = db[sector_key]
            sector_name = sector["name"]

            for stock_data in sector["stocks"]:
                # 按 tier 限制数量
                tier = stock_data.get("tier", 3)
                if tier == 1 and tier1_count >= max_tier1:
                    continue
                if tier == 2 and tier2_count >= max_tier2:
                    continue
                if tier == 3 and tier3_count >= max_tier3:
                    continue

                stock = DragonHeadStock(
                    code=stock_data["code"],
                    name=stock_data["name"],
                    sector=sector_key,
                    sector_name=sector_name,
                    tier=tier,
                    notes=stock_data.get("notes", ""),
                    market=market,
                    is_active=True,
                )
                pool.append(stock)

                if tier == 1:
                    tier1_count += 1
                elif tier == 2:
                    tier2_count += 1
                else:
                    tier3_count += 1

        # 按 tier 排序 (tier 越小越靠前)
        pool.sort(key=lambda x: (x.tier, -x.strength_score))

        # 缓存
        if market == "HK":
            self._active_hk = pool
        else:
            self._active_us = pool
        self._last_update = date.today()
        self._last_update_time = time.time()

        logger.info(
            f"🐲 [{market}] 龙头池已生成: {len(pool)}只 "
            f"(T1={tier1_count} T2={tier2_count} T3={tier3_count}) | "
            f"阶段={regime.value}"
        )
        return pool

    # ─────────────────────────────────────────────────────────────
    #  市场阶段切换
    # ─────────────────────────────────────────────────────────────

    def set_regime(self, regime: MarketRegime, force: bool = False):
        """设置当前市场阶段并自动重新生成龙头池"""
        if regime != self._current_regime or force:
            if regime != self._current_regime:
                logger.info(
                    f"🔄 市场阶段切换: {self._current_regime.value} → {regime.value} "
                    f"({REGIME_SECTOR_PRIORITY.get(regime, {}).get('label', '')})"
                )
            self._current_regime = regime
            # 自动重新生成
            self.generate_active_pool("HK", regime)
            self.generate_active_pool("US", regime)

    def get_regime(self) -> MarketRegime:
        return self._current_regime

    # ─────────────────────────────────────────────────────────────
    #  强度评分更新 (盘前 LLM 分析结果注入)
    # ─────────────────────────────────────────────────────────────

    def update_strength_scores(self, scores: Dict[str, float]):
        """
        根据盘前 LLM 分析结果更新强度评分

        Args:
            scores: {code: strength_score} 如 {"HK.00700": 85.5, ...}
        """
        for pool in [self._active_hk, self._active_us]:
            for stock in pool:
                if stock.code in scores:
                    old_score = stock.strength_score
                    stock.strength_score = scores[stock.code]
                    if old_score != stock.strength_score:
                        logger.debug(
                            f"  {stock.code} {stock.name}: "
                            f"{old_score:.1f} → {stock.strength_score:.1f}"
                        )

        # 重新排序
        for pool in [self._active_hk, self._active_us]:
            pool.sort(key=lambda x: (x.tier, -x.strength_score))

    # ─────────────────────────────────────────────────────────────
    #  查询接口
    # ─────────────────────────────────────────────────────────────

    def get_active_pool(self, market: str = "HK") -> List[DragonHeadStock]:
        """获取当前激活池"""
        return self._active_hk if market == "HK" else self._active_us

    def get_active_codes(self, market: str = "HK") -> List[str]:
        """获取激活池股票代码列表"""
        pool = self.get_active_pool(market)
        return [s.code for s in pool]

    def get_top_n(self, market: str = "HK", n: int = 5) -> List[DragonHeadStock]:
        """获取强度最高的 N 只龙头"""
        pool = self.get_active_pool(market)
        sorted_pool = sorted(pool, key=lambda x: -x.strength_score)
        return sorted_pool[:n]

    def get_by_sector(self, sector: str, market: str = "HK") -> List[DragonHeadStock]:
        """获取指定板块的龙头"""
        pool = self.get_active_pool(market)
        return [s for s in pool if s.sector == sector]

    def get_sector_summary(self, market: str = "HK") -> Dict[str, List[str]]:
        """获取各板块龙头摘要"""
        pool = self.get_active_pool(market)
        result = {}
        for stock in pool:
            if stock.sector_name not in result:
                result[stock.sector_name] = []
            result[stock.sector_name].append(
                f"{stock.code} ({stock.name}) T{stock.tier}"
            )
        return result

    # ─────────────────────────────────────────────────────────────
    #  报告
    # ─────────────────────────────────────────────────────────────

    def generate_report(self, market: str = "HK") -> str:
        """生成龙头池报告"""
        pool = self.get_active_pool(market)
        regime = self._current_regime
        regime_info = REGIME_SECTOR_PRIORITY.get(regime, {})
        label = regime_info.get("label", "")

        lines = [
            f"🐲 {market} 龙头股票池报告",
            f"日期: {date.today().isoformat()}",
            f"市场阶段: {regime.value} ({label})",
            f"总数: {len(pool)}只",
            f"",
        ]

        sector_summary = self.get_sector_summary(market)
        for sector_name, stocks in sector_summary.items():
            lines.append(f"📌 {sector_name}:")
            for s in stocks:
                lines.append(f"  • {s}")
            lines.append("")

        # Top 5 最强龙头
        top5 = self.get_top_n(market, 5)
        if top5:
            lines.append("🏆 最强龙头 TOP 5:")
            for i, s in enumerate(top5, 1):
                lines.append(
                    f"  {i}. {s.code} {s.name} (强度 {s.strength_score:.1f}, T{s.tier})"
                )

        return "\n".join(lines)


# ============================================================================
#  全局单例
# ============================================================================

_instance: Optional[DragonHeadPoolManager] = None


def get_dragonhead_pool() -> DragonHeadPoolManager:
    """获取龙头股票池管理器单例"""
    global _instance
    if _instance is None:
        _instance = DragonHeadPoolManager()
    return _instance
