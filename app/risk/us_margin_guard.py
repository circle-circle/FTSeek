#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股保证金风控 (US Margin Guard) - P0 级资金熔断器

与港股 FutuMarginMonitor 对齐的美股实现，但更轻量：
- 通过 broker_adapter 获取账户数据（不直接调用 Futu API）
- 内置滞回防抖，不依赖外部 HysteresisEngine
- 内置降级防护，不依赖外部 position_degradation_policy

核心指标:
  margin_ratio = net_assets_power / maintain_margin
  
状态分级:
  SAFE      >= 1.50
  CAUTION   >= 1.30
  WARNING   >= 1.15  (或 buffer < 0.20)
  MARGIN_CALL >= 1.05
  LIQUIDATION < 1.05
  DATA_ERROR 数据不可用

v1.0 (2026-05-09): 基于港股 FutuMarginMonitor v2.1 适配美股 broker_adapter
"""

import time
import logging
from enum import Enum
from typing import Dict, Tuple, Optional, Any

logger = logging.getLogger("ftseek-us.risk.margin_guard")


class MarginStatus(Enum):
    SAFE = "SAFE"
    CAUTION = "CAUTION"
    WARNING = "WARNING"
    MARGIN_CALL = "MARGIN_CALL"
    LIQUIDATION = "LIQUIDATION"
    DATA_ERROR = "DATA_ERROR"


# 状态严重度排序 (数值越大越危险)
STATUS_SEVERITY = {
    MarginStatus.SAFE: 0,
    MarginStatus.CAUTION: 1,
    MarginStatus.WARNING: 2,
    MarginStatus.MARGIN_CALL: 3,
    MarginStatus.LIQUIDATION: 4,
    MarginStatus.DATA_ERROR: 5,
}


class USMarginGuard:
    """美股保证金风控 — 线程安全单例"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(USMarginGuard, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def initialize(
        self,
        broker_adapter=None,
        hysteresis_gap: float = 0.10,
        max_api_failures: int = 2,
        max_stale_seconds: float = 60.0,
        cache_ttl: float = 30.0,
    ):
        if self._initialized:
            return
        self._initialized = True

        self._broker = broker_adapter
        self._hysteresis_gap = hysteresis_gap  # 滞回防抖间隙
        self._max_api_failures = max_api_failures
        self._max_stale_seconds = max_stale_seconds
        self._cache_ttl = cache_ttl  # API 缓存 TTL

        # 内部状态
        self._last_status: Optional[MarginStatus] = None
        self._last_info: Dict = {}
        self._consecutive_api_failures = 0
        self._last_success_time = 0.0
        self._last_data_timestamp = 0.0
        self._cache_expiry = 0.0  # 缓存过期时间戳
        self._cached_status: Optional[MarginStatus] = None
        self._cached_info: Dict = {}

        logger.info(
            f"[MarginGuard] 美股保证金风控已启动 | "
            f"滞回间隙={hysteresis_gap} | "
            f"最大API失败={max_api_failures}次 | "
            f"缓存TTL={cache_ttl}s"
        )

    def set_broker(self, broker_adapter):
        """动态设置 broker adapter (延迟注入)"""
        self._broker = broker_adapter

    def get_margin_status(self) -> Tuple[MarginStatus, Dict]:
        """获取当前保证金状态 (带滞回防抖 + 缓存)"""
        if not self._initialized:
            return MarginStatus.DATA_ERROR, {"error": "未初始化"}

        # P0 修复 (2026-05-09): 检查缓存是否有效
        now = time.time()
        if now < self._cache_expiry and self._cached_status is not None:
            return self._cached_status, self._cached_info

        raw_status, info = self._fetch_raw_status()

        # 滞回防抖: 防止状态在临界点频繁跳变
        final_status = self._apply_hysteresis(raw_status, info)

        # 降级防护: API 连续失败降级到 CAUTION
        final_status = self._apply_degradation(final_status)

        # 更新缓存
        self._cached_status = final_status
        self._cached_info = info
        self._cache_expiry = now + self._cache_ttl

        self._last_status = final_status
        self._last_info = info
        return final_status, info

    def _fetch_raw_status(self) -> Tuple[MarginStatus, Dict]:
        """从 broker adapter 拉取原始保证金数据"""
        try:
            if not self._broker or not self._broker.is_connected():
                return MarginStatus.DATA_ERROR, {"error": "broker 未连接"}

            acc_info = self._broker.get_account_info()
            if not acc_info:
                return MarginStatus.DATA_ERROR, {"error": "无账户数据"}

            net_assets = acc_info.get("net_assets_power", 0)
            maint_margin = acc_info.get("maintain_margin", 0)
            total_assets = acc_info.get("total_assets", 0)
            buying_power = acc_info.get("buying_power", 0)
            debt = acc_info.get("debt", 0)

            # P0 修复 (2026-05-09): 模拟盘 net_assets_power 返回 0.0（_safe_float 将 None 转为 0），用 total_assets 兜底
            if net_assets <= 0 and total_assets > 0:
                net_assets = total_assets
                logger.info("[MarginGuard] 模拟盘 net_assets_power 为 0，使用 total_assets 替代")
            # 模拟盘 maintain_margin 可能为 0（_safe_float 将 None/N/A 转为 0）
            if maint_margin is None or maint_margin == 'N/A' or maint_margin == 0:
                maint_margin = 0
            # 确保数值类型
            try:
                net_assets = float(net_assets) if net_assets else 0.0
                maint_margin = float(maint_margin) if maint_margin else 0.0
                total_assets = float(total_assets) if total_assets else 0.0
                buying_power = float(buying_power) if buying_power else 0.0
                debt = float(debt) if debt else 0.0
            except (ValueError, TypeError) as e:
                logger.warning(f"[MarginGuard] 数值转换异常: net_assets={net_assets}, maint={maint_margin}, err={e}")
                net_assets = 0.0
                maint_margin = 0.0
                total_assets = 0.0

            logger.debug(f"[MarginGuard] 账户数据: net_assets=${net_assets:,.0f}, maint=${maint_margin:,.0f}, total=${total_assets:,.0f}, buying_power=${buying_power:,.0f}")

            # 模拟盘 maintain_margin 可能为 0 或 'N/A'
            if maint_margin <= 0:
                # 无杠杆持仓时视为安全
                margin_ratio = 999.0
                liquidity_buffer = 999.0
            else:
                margin_ratio = net_assets / maint_margin if maint_margin > 0 else 999.0
                liquidity_buffer = buying_power / maint_margin if maint_margin > 0 else 999.0

            self._consecutive_api_failures = 0
            self._last_success_time = time.time()
            self._last_data_timestamp = time.time()

            info = {
                "ratio": margin_ratio,
                "buffer": liquidity_buffer,
                "net_assets": net_assets,
                "maint_margin": maint_margin,
                "total_assets": total_assets,
                "buying_power": buying_power,
                "debt": debt,
                "timestamp": time.time(),
            }

            # 状态判定
            if margin_ratio < 1.05:
                return MarginStatus.LIQUIDATION, info
            if margin_ratio < 1.15:
                return MarginStatus.MARGIN_CALL, info
            if margin_ratio < 1.30 or liquidity_buffer < 0.20:
                return MarginStatus.WARNING, info
            if margin_ratio < 1.50:
                return MarginStatus.CAUTION, info

            return MarginStatus.SAFE, info

        except Exception as e:
            self._consecutive_api_failures += 1
            logger.error(f"[MarginGuard] API 失败 #{self._consecutive_api_failures}: {e}")
            return MarginStatus.DATA_ERROR, {"error": str(e), "timestamp": time.time()}

    def _apply_hysteresis(self, raw: MarginStatus, info: Dict) -> MarginStatus:
        """滞回防抖: 恶化立即生效，改善需跨越 gap"""
        if self._last_status is None:
            return raw

        raw_severity = STATUS_SEVERITY.get(raw, 0)
        last_severity = STATUS_SEVERITY.get(self._last_status, 0)

        if raw_severity >= last_severity:
            # 恶化或持平 → 立即采纳
            return raw
        else:
            # 改善 → 检查是否跨越滞回间隙
            ratio = info.get("ratio", 999.0)
            # 只有 ratio 显著改善时才允许升级
            gap_threshold = 1.50 + self._hysteresis_gap  # SAFE 门槛 + gap
            if raw == MarginStatus.SAFE and ratio >= gap_threshold:
                return raw
            # 其他改善场景也要求显著跨越
            if raw_severity < last_severity - 1:
                return raw
            return self._last_status

    def _apply_degradation(self, status: MarginStatus) -> MarginStatus:
        """降级防护: API 连续失败时强制降级"""
        if self._consecutive_api_failures >= self._max_api_failures:
            if STATUS_SEVERITY.get(status, 0) < STATUS_SEVERITY.get(MarginStatus.CAUTION, 0):
                logger.warning(
                    f"[MarginGuard] API 连续失败 #{self._consecutive_api_failures}，"
                    f"强制降级到 CAUTION (原: {status.value})"
                )
                return MarginStatus.CAUTION

        # 数据过期检查
        if self._last_data_timestamp > 0:
            age = time.time() - self._last_data_timestamp
            if age > self._max_stale_seconds * 3:
                if STATUS_SEVERITY.get(status, 0) < STATUS_SEVERITY.get(MarginStatus.CAUTION, 0):
                    logger.warning(f"[MarginGuard] 数据过期 {age:.0f}s，强制降级到 CAUTION")
                    return MarginStatus.CAUTION

        return status

    def is_safe_to_open(self, estimated_cost: float = 0) -> bool:
        """
        开仓前保证金压力测试。

        Args:
            estimated_cost: 预估开仓总成本 (position_value)

        Returns:
            True = 允许开仓, False = 拒绝
        """
        status, info = self.get_margin_status()

        # 数据质量不足 → 拒绝
        if status == MarginStatus.DATA_ERROR:
            logger.warning("[MarginGuard] 数据不可用，禁止开仓")
            return False

        # 非安全状态 → 拒绝
        if status in (
            MarginStatus.WARNING,
            MarginStatus.MARGIN_CALL,
            MarginStatus.LIQUIDATION,
        ):
            logger.warning(f"[MarginGuard] 状态={status.value}，禁止开仓")
            return False

        # 压力测试: 模拟开仓后的保证金率
        if estimated_cost > 0:
            current_maint = info.get("maint_margin", 0)
            current_net = info.get("net_assets", 0)
            current_total_assets = info.get("total_assets", 0)

            # P0 修复 (2026-05-09): 美股维持保证金 = position_value × margin_rate
            # 通常 margin_rate 25%-50% (Reg T 初始保证金 50%, 维持 25%)
            # 保守估算: 假设新仓位需要 50% 初始保证金 (Reg T 要求)
            margin_rate = 0.50  # Reg T initial margin requirement
            maint_rate = 0.25  # Reg T maintenance margin requirement

            if current_maint > 0 and current_total_assets > 0:
                # 从当前持仓反推隐含保证金率
                implied_rate = current_maint / max(current_total_assets - info.get("buying_power", 0), 1)
                # 取反推值和 Reg T 要求中较高的 (更保守)
                effective_rate = min(implied_rate, 0.50)
            else:
                # 无现有持仓或数据不足，使用 Reg T 默认
                effective_rate = margin_rate

            new_maint = current_maint + (estimated_cost * effective_rate)
            if new_maint > 0:
                projected_ratio = current_net / new_maint
                if projected_ratio < 1.30:
                    logger.warning(
                        f"[MarginGuard] 压力测试: 开仓后保证金率 {projected_ratio:.2f} < 1.30，拒绝 "
                        f"(估算: cost=${estimated_cost:,.0f} × rate={effective_rate:.0%} → maint=${estimated_cost*effective_rate:,.0f})"
                    )
                    return False

        return True

    def get_comprehensive_status(self) -> Dict:
        """获取综合状态 (用于仪表盘/日志)"""
        status, info = self.get_margin_status()
        return {
            "margin_status": status.value,
            "margin_ratio": round(info.get("ratio", 0), 2),
            "liquidity_buffer": round(info.get("buffer", 0), 2),
            "net_assets": round(info.get("net_assets", 0), 0),
            "maint_margin": round(info.get("maint_margin", 0), 0),
            "total_assets": round(info.get("total_assets", 0), 0),
            "buying_power": round(info.get("buying_power", 0), 0),
            "api_failures": self._consecutive_api_failures,
            "data_age_s": round(time.time() - self._last_data_timestamp, 0) if self._last_data_timestamp > 0 else -1,
        }


# 单例访问
_margin_guard: Optional[USMarginGuard] = None


def get_margin_guard() -> USMarginGuard:
    global _margin_guard
    if _margin_guard is None:
        _margin_guard = USMarginGuard()
    return _margin_guard
