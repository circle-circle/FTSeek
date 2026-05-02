#!/usr/bin/env python3
"""
港股专属数据源集成 (HK Market Data Feeds)

功能:
1. 恒指期货贴水/溢价 (HSI Futures Basis) -- 现货端最强 lead 指标
2. 南向资金流向 (Southbound Flow) -- 恒指第一解释变量
3. HKEX 卖空比率 (Short Selling Ratio) -- 空头压力指标

数据源:
- Futu OpenD API (期货行情)
- 外部 API (南向资金 -- 待配置)

使用:
    from app.factors.hk_market_data import get_hk_market_data
    data = get_hk_market_data()
    print(data.hsi_basis_pct)      # 恒指期货贴水
    print(data.southbound_net)     # 南向资金净流入
    print(data.short_ratio)        # 卖空比率
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Dict
from datetime import datetime

logger = logging.getLogger("ftseek.factors.hk_market_data")


@dataclass
class HKMarketData:
    """港股专属市场数据"""
    timestamp: datetime = field(default_factory=datetime.now)

    # 恒指期货
    hsi_spot: float = 0.0          # 恒指现货
    hsi_futures: float = 0.0       # 恒指期货 (主力合约)
    hsi_basis_pct: float = 0.0     # 贴水/溢价 = (期货-现货)/现货
    hstech_futures: float = 0.0    # 恒生科技期货
    hstech_basis_pct: float = 0.0

    # 南向资金 (亿元)
    southbound_net: float = 0.0    # 净流入 (正=流入, 负=流出)
    southbound_flow_valid: bool = False

    # 卖空比率 (HKEX 日数据)
    short_ratio: float = 0.0       # 全市场卖空成交占比
    short_ratio_valid: bool = False

    # 综合判断
    def market_pressure(self) -> str:
        """
        综合判断市场压力方向
        Returns: "bullish" / "bearish" / "neutral"
        """
        score = 0.0

        # 恒指期货贴水 → 看空
        if self.hsi_basis_pct < -0.5:
            score -= 2
        elif self.hsi_basis_pct < -0.2:
            score -= 1
        elif self.hsi_basis_pct > 0.3:
            score += 1

        # 南向资金
        if self.southbound_flow_valid:
            if self.southbound_net < -80:
                score -= 2
            elif self.southbound_net < -30:
                score -= 1
            elif self.southbound_net > 80:
                score += 2
            elif self.southbound_net > 30:
                score += 1

        # 卖空比率
        if self.short_ratio_valid:
            if self.short_ratio > 20:
                score -= 2
            elif self.short_ratio > 15:
                score -= 1
            elif self.short_ratio < 8:
                score += 1

        if score >= 2:
            return "bullish"
        elif score <= -2:
            return "bearish"
        return "neutral"

    def to_risk_params(self) -> Dict:
        """输出给风控系统的参数"""
        return {
            "hsi_basis_pct": self.hsi_basis_pct,
            "southbound_net": self.southbound_net,
            "short_ratio": self.short_ratio,
            "market_pressure": self.market_pressure(),
        }


# ── 全局缓存 ────────────────────────────────────────────────────────
_hk_data_cache: Optional[HKMarketData] = None
_hk_cache_time: float = 0
_HK_CACHE_TTL = 300  # 5 分钟刷新


def get_hk_market_data(force_refresh: bool = False) -> HKMarketData:
    """
    获取港股专属市场数据 (带缓存)

    Args:
        force_refresh: 强制刷新

    Returns:
        HKMarketData 实例
    """
    global _hk_data_cache, _hk_cache_time

    now = time.time()
    if _hk_data_cache and not force_refresh and (now - _hk_cache_time) < _HK_CACHE_TTL:
        return _hk_data_cache

    fetcher = HKDataFetcher()
    _hk_data_cache = fetcher.fetch_all()
    _hk_cache_time = now

    return _hk_data_cache


class HKDataFetcher:
    """港股数据获取器"""

    def fetch_all(self) -> HKMarketData:
        """获取所有港股专属数据"""
        data = HKMarketData(timestamp=datetime.now())

        # 1. 恒指期货贴水
        data = self._fetch_hsi_futures(data)

        # 2. 南向资金
        data = self._fetch_southbound(data)

        # 3. 卖空比率
        data = self._fetch_short_ratio(data)

        logger.info(
            f"[HKData] 恒指贴水={data.hsi_basis_pct:+.2%} | "
            f"南向={data.southbound_net:+.0f}亿 | "
            f"卖空比={data.short_ratio:.1f}% | "
            f"压力={data.market_pressure()}"
        )

        return data

    def _fetch_hsi_futures(self, data: HKMarketData) -> HKMarketData:
        """
        获取恒指期货贴水/溢价

        Futu API:
          - 恒指现货: HK.800000
          - 恒指期货: HSI main contract
        """
        try:
            from futu import OpenQuoteContext, SecurityReferenceType

            host = __import__('os').environ.get("OPEND_HOST", "127.0.0.1")
            port = int(__import__('os').environ.get("OPEND_PORT", "11111"))

            quote_ctx = OpenQuoteContext(host=host, port=port)

            # 恒指现货
            ret, spot_data = quote_ctx.get_market_snapshot(["HK.800000"])
            if ret == 0 and spot_data is not None and not spot_data.empty:
                data.hsi_spot = float(spot_data["last_price"].iloc[0])
                logger.debug(f"[HSI] 现货 = {data.hsi_spot:.0f}")

            # 恒指期货主力合约 (通过参考合约获取)
            ret, ref_data = quote_ctx.get_future_contract_list(
                "HK.800000", SecurityReferenceType.FUTURES_MAIN
            )
            if ret == 0 and ref_data is not None and not ref_data.empty:
                future_code = ref_data["code"].iloc[0]
                ret, fut_data = quote_ctx.get_market_snapshot([future_code])
                if ret == 0 and fut_data is not None and not fut_data.empty:
                    data.hsi_futures = float(fut_data["last_price"].iloc[0])

                if data.hsi_spot > 0 and data.hsi_futures > 0:
                    data.hsi_basis_pct = (data.hsi_futures - data.hsi_spot) / data.hsi_spot

            # 恒生科技期货
            ret, hstech_ref = quote_ctx.get_future_contract_list(
                "HK.HSTECH", SecurityReferenceType.FUTURES_MAIN
            )
            if ret == 0 and hstech_ref is not None and not hstech_ref.empty:
                hstech_code = hstech_ref["code"].iloc[0]
                ret, ht_data = quote_ctx.get_market_snapshot([hstech_code])
                if ret == 0 and ht_data is not None and not ht_data.empty:
                    data.hstech_futures = float(ht_data["last_price"].iloc[0])

                    # 恒生科技现货
                    ret, ht_spot = quote_ctx.get_market_snapshot(["HK.HSTECH"])
                    if ret == 0 and ht_spot is not None and not ht_spot.empty:
                        ht_spot_price = float(ht_spot["last_price"].iloc[0])
                        if ht_spot_price > 0:
                            data.hstech_basis_pct = (data.hstech_futures - ht_spot_price) / ht_spot_price

            quote_ctx.close()

        except Exception as e:
            logger.warning(f"[HSI期货] 获取失败: {e}")

        return data

    def _fetch_southbound(self, data: HKMarketData) -> HKMarketData:
        """
        获取南向资金流向

        方案 A: Futu API 资金流向数据 (get_capital_flow)
        方案 B: 外部 API (东方财富/同花顺)
        方案 C: 手动输入 (配置文件)

        优先使用方案 A，失败后降级到 B/C
        """
        # 方案 A: Futu API
        try:
            from futu import OpenQuoteContext

            host = __import__('os').environ.get("OPEND_HOST", "127.0.0.1")
            port = int(__import__('os').environ.get("OPEND_PORT", "11111"))

            quote_ctx = OpenQuoteContext(host=host, port=port)

            # Futu 资金流向 API
            ret, flow_data = quote_ctx.get_capital_flow(["HK.800000"])
            if ret == 0 and flow_data is not None and not flow_data.empty:
                # 净流入 (亿元)
                net_in = float(flow_data.get("net_inflow", 0).iloc[0])
                data.southbound_net = net_in
                data.southbound_flow_valid = True

            quote_ctx.close()

        except Exception:
            logger.debug("[南向资金] Futu API 不可用，尝试降级")

        # 方案 B: 外部 API (待配置)
        if not data.southbound_flow_valid:
            data = self._fetch_southbound_external(data)

        return data

    def _fetch_southbound_external(self, data: HKMarketData) -> HKMarketData:
        """从外部 API 获取南向资金 (预留接口)"""
        import urllib.request
        import json as _json

        # 预留: 东方财富 API
        # 实际部署时配置 API_KEY
        api_url = __import__('os').environ.get("SOUTHBOUND_API_URL", "")

        if not api_url:
            logger.debug("[南向资金] 未配置外部 API，数据不可用")
            return data

        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": "FTSeek/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = _json.loads(resp.read())
                # 根据实际 API 格式解析
                data.southbound_net = result.get("net_inflow", 0)
                data.southbound_flow_valid = True
        except Exception as e:
            logger.warning(f"[南向资金] 外部 API 失败: {e}")

        return data

    def _fetch_short_ratio(self, data: HKMarketData) -> HKMarketData:
        """
        获取 HKEX 卖空比率

        HKEX 每日公布卖空成交数据 (T+1 延迟)
        方案 A: Futu API (get_short_selling_ratio 如果有)
        方案 B: 爬取 HKEX 官网
        """
        # 目前 Futu API 不直接提供全市场卖空比率
        # 预留接口，实际部署时从 HKEX 官网或第三方数据源获取

        try:
            # 可以从配置中获取最新数据
            from app import config
            sr = getattr(config, "HK_SHORT_SELLING_RATIO", None)
            if sr is not None:
                data.short_ratio = float(sr)
                data.short_ratio_valid = True
                logger.debug(f"[卖空比] 从配置读取: {data.short_ratio:.1f}%")
        except Exception:
            pass

        return data


# ── 与 MarketProfile 集成 ───────────────────────────────────────────
def update_market_profile_with_hk_data(profile=None):
    """
    将港股专属数据注入 MarketProfile 动态权重计算

    用法:
        profile = get_market_profile("HK")
        updated = update_market_profile_with_hk_data(profile)
        # updated.signal_fusion 已根据南向资金等动态调整
    """
    if profile is None or profile.type != "HK":
        return profile

    hk_data = get_hk_market_data()

    # 使用已有的 get_dynamic_weights 方法
    new_weights = profile.get_dynamic_weights(
        vix=20.0,  # 港股用 VIX 作为参考
        southbound_net=hk_data.southbound_net,
    )

    # 根据恒指期货贴水额外调整
    if hk_data.hsi_basis_pct < -0.5:
        # 深度贴水: 降低技术面权重，增加政策面权重
        new_weights.technical_weight = max(0.1, new_weights.technical_weight - 0.15)
        new_weights.policy_weight = min(0.6, new_weights.policy_weight + 0.15)
        logger.info(
            f"[HKData→Profile] 恒指深度贴水 ({hk_data.hsi_basis_pct:+.2%}) → "
            f"技术权重↓ 政策权重↑"
        )

    return new_weights
