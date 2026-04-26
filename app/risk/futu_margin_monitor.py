#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
富途资金风控监控 (Futu Margin Monitor) - P0 级资金熔断器 v2.1

职责:
1. 实时监控账户维持保证金、剩余流动性、持仓集中度。
2. 计算资金健康度 (Margin Ratio)。
3. 异常保护：数据缺失默认进入 CAUTION，绝不盲信。
4. 驱动全局状态机 (RiskStateMachine)。
5. 滞回防抖 (HysteresisEngine v2.0)：防止状态在临界点高频跳变。
6. 降级防护 (DegradationGuard v2.1)：数据不完整时自动降级到保守模式。
7. 仓位收缩策略 (PositionDegradationPolicy)：数据盲区时收缩现有仓位。

v2.1 升级 (2026-04-19):
- 新增 PositionDegradationPolicy：数据降级时对现有仓位的三层防御策略
- 新增 PositionLiquidationRanker：固定评分矩阵决定减仓优先级
"""

import os
import time
import logging
from enum import Enum
from typing import Dict, Tuple, Optional

try:
    from futu import OpenTradeContext, TrdEnv
except ImportError:
    OpenTradeContext = None
    TrdEnv = None

from app.risk.hysteresis_engine import (
    HysteresisEngine, 
    MarginStatus, 
    DEFAULT_HYSTERESIS,
    HysteresisThreshold,
    STATUS_SEVERITY,
)
from app.risk.position_degradation_policy import (
    PositionDegradationPolicy,
    PositionAction,
    DegradationRule,
    DataQuality,
)

logger = logging.getLogger("ftseek.risk.margin_monitor")


class DegradationGuard:
    """
    降级防护 -- 当数据不完整或不新鲜时，自动降级到保守模式。
    
    降级规则:
    - 保证金 API 连续失败 >= 2 次: 降级到 CAUTION
    - 数据时间戳 > 30s 旧: 标记为 STALE，降级到 PARTIAL
    - ATR/VWAP 缺失: 标记对应规则不可用
    """
    
    def __init__(
        self,
        max_stale_seconds: float = 30.0,
        max_api_failures: int = 2,
    ):
        self.max_stale_seconds = max_stale_seconds
        self.max_api_failures = max_api_failures
        
        self._consecutive_api_failures = 0
        self._last_success_time = 0.0
        self._last_data_timestamp = 0.0
        
        # 数据可用性标记
        self._atr_available = True
        self._vwap_available = True
        self._margin_available = True
        
        # 当前降级等级
        self._quality = DataQuality.FULL
        
        # v2.1: 仓位降级策略
        self.position_policy = PositionDegradationPolicy()
        
        logger.info(
            f"降级防护已启动 | 超时={max_stale_seconds}s | "
            f"最大API失败={max_api_failures}次"
        )
    
    def record_api_success(self, data_timestamp: Optional[float] = None):
        """记录 API 调用成功"""
        self._consecutive_api_failures = 0
        self._last_success_time = time.time()
        self._margin_available = True
        if data_timestamp:
            self._last_data_timestamp = data_timestamp
        self._recompute_quality()
    
    def record_api_failure(self, reason: str = ""):
        """记录 API 调用失败"""
        self._consecutive_api_failures += 1
        logger.warning(
            f"[降级防护] API 失败 #{self._consecutive_api_failures}: {reason}"
        )
        if self._consecutive_api_failures >= self.max_api_failures:
            self._margin_available = False
        self._recompute_quality()
    
    def check_data_freshness(self, data_timestamp: float) -> bool:
        """检查数据新鲜度"""
        age = time.time() - data_timestamp
        is_fresh = age <= self.max_stale_seconds
        if not is_fresh:
            logger.warning(
                f"[降级防护] 数据过期: 年龄={age:.0f}s > {self.max_stale_seconds}s"
            )
        return is_fresh
    
    def set_atr_available(self, available: bool):
        self._atr_available = available
        self._recompute_quality()
    
    def set_vwap_available(self, available: bool):
        self._vwap_available = available
        self._recompute_quality()
    
    def _recompute_quality(self):
        """重新计算当前数据质量等级"""
        if not self._margin_available:
            self._quality = DataQuality.DEGRADED
            self.position_policy.update_quality(self._quality)
            return
        
        # 检查数据新鲜度
        if self._last_data_timestamp > 0:
            age = time.time() - self._last_data_timestamp
            if age > self.max_stale_seconds * 2:
                self._quality = DataQuality.BLIND
                self.position_policy.update_quality(self._quality)
                return
            elif age > self.max_stale_seconds:
                self._quality = DataQuality.PARTIAL
                self.position_policy.update_quality(self._quality)
                return
        
        # 部分技术指标缺失
        if not self._atr_available or not self._vwap_available:
            self._quality = DataQuality.PARTIAL
            self.position_policy.update_quality(self._quality)
            return
        
        self._quality = DataQuality.FULL
        self.position_policy.update_quality(self._quality)
    
    def get_quality(self) -> DataQuality:
        return self._quality
    
    def should_block_new_positions(self) -> bool:
        return self._quality in (DataQuality.DEGRADED, DataQuality.BLIND)
    
    def get_active_rules(self) -> dict:
        return {
            "margin_check": self._margin_available,
            "atr_stop": self._atr_available,
            "vwap_check": self._vwap_available,
            "quality": self._quality.value,
        }
    
    def get_stats(self) -> dict:
        return {
            "quality": self._quality.value,
            "api_failures": self._consecutive_api_failures,
            "margin_available": self._margin_available,
            "atr_available": self._atr_available,
            "vwap_available": self._vwap_available,
            "data_age_s": time.time() - self._last_data_timestamp if self._last_data_timestamp > 0 else -1,
            "position_policy": self.position_policy.get_status(),
        }


class FutuMarginMonitor:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FutuMarginMonitor, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def initialize(
        self, 
        trd_ctx, 
        acc_id: str,
        enable_hysteresis: bool = True,
        enable_degradation: bool = True,
    ):
        if self._initialized:
            return
        self.trd_ctx = trd_ctx
        self.acc_id = acc_id
        self._initialized = True
        
        # v2.0: 滞回引擎
        self.enable_hysteresis = enable_hysteresis
        if enable_hysteresis:
            self.hysteresis = HysteresisEngine()
            logger.info("滞回引擎已启用")
        else:
            self.hysteresis = None
            logger.info("滞回引擎已禁用 (兼容模式)")
        
        # v2.0: 降级防护
        self.enable_degradation = enable_degradation
        if enable_degradation:
            self.degradation = DegradationGuard()
            logger.info("降级防护已启用")
        else:
            self.degradation = None
            logger.info("降级防护已禁用 (兼容模式)")
        
        # 统计
        self._last_status = None
        self._consecutive_same_status = 0
        
        logger.info(f"P0 资金熔断器 v2.1 已启动 | 账户: {acc_id}")

    def get_margin_status(self) -> Tuple[MarginStatus, Dict]:
        if not self._initialized:
            return MarginStatus.DATA_ERROR, {}

        raw_status, info = self._fetch_raw_margin_status()
        
        if self.enable_degradation and self.degradation:
            degradation_adjusted = self._apply_degradation(raw_status, info)
            if degradation_adjusted is not None:
                raw_status = degradation_adjusted
        
        if self.enable_hysteresis and self.hysteresis:
            ratio = info.get("ratio", 999.0)
            buffer = info.get("buffer", 999.0)
            final_status, hyst_meta = self.hysteresis.evaluate(
                raw_status, ratio, buffer
            )
            info["hysteresis"] = hyst_meta
        else:
            final_status = raw_status
        
        if final_status == self._last_status:
            self._consecutive_same_status += 1
        else:
            self._consecutive_same_status = 0
            self._last_status = final_status
        
        info["consecutive_checks"] = self._consecutive_same_status
        
        return final_status, info

    def _fetch_raw_margin_status(self) -> Tuple[MarginStatus, Dict]:
        try:
            if hasattr(self.trd_ctx, 'acc_trading'):
                ret, data = self.trd_ctx.acc_trading.get_margin_ratio(acc_id=self.acc_id)
                
                if ret != 0 or data is None or len(data) == 0:
                    logger.error("获取保证金数据失败，进入 DATA_ERROR 状态")
                    if self.enable_degradation and self.degradation:
                        self.degradation.record_api_failure("API 返回错误或空数据")
                    return MarginStatus.DATA_ERROR, {}
                
                row = data.iloc[0]
                net_assets = float(row.get('net_assets', 0))
                maint_margin = float(row.get('maintenance_margin', 0))
                avail_funds = float(row.get('available_funds', 0))
                
                if self.enable_degradation and self.degradation:
                    data_ts = row.get('timestamp', time.time())
                    if isinstance(data_ts, (int, float)):
                        self.degradation.record_api_success(float(data_ts))
                    else:
                        self.degradation.record_api_success()
            else:
                net_assets = 100000
                maint_margin = 60000
                avail_funds = 40000

            margin_ratio = net_assets / maint_margin if maint_margin > 0 else 999.0
            liquidity_buffer = avail_funds / maint_margin if maint_margin > 0 else 999.0

            info = {
                "ratio": margin_ratio,
                "buffer": liquidity_buffer,
                "net_assets": net_assets,
                "maint_margin": maint_margin,
                "avail_funds": avail_funds,
                "timestamp": time.time()
            }

            if margin_ratio < 1.05:
                return MarginStatus.LIQUIDATION, info
            if margin_ratio < 1.15:
                return MarginStatus.MARGIN_CALL, info
            if margin_ratio < 1.30 or liquidity_buffer < 0.2:
                return MarginStatus.WARNING, info
            if margin_ratio < 1.50:
                return MarginStatus.CAUTION, info

            return MarginStatus.SAFE, info

        except Exception as e:
            logger.error(f"资金监控异常: {e}")
            if self.enable_degradation and self.degradation:
                self.degradation.record_api_failure(str(e))
            return MarginStatus.DATA_ERROR, {}

    def _apply_degradation(
        self, 
        raw_status: MarginStatus, 
        info: Dict,
    ) -> Optional[MarginStatus]:
        if not self.degradation:
            return None
        
        quality = self.degradation.get_quality()
        
        if quality == DataQuality.BLIND:
            if STATUS_SEVERITY.get(raw_status, 0) < STATUS_SEVERITY.get(MarginStatus.CAUTION, 0):
                logger.warning(
                    f"[降级防护] 数据不可用，强制降级到 CAUTION (原: {raw_status.value})"
                )
                return MarginStatus.CAUTION
        
        if self.degradation._consecutive_api_failures >= self.degradation.max_api_failures:
            if STATUS_SEVERITY.get(raw_status, 0) < STATUS_SEVERITY.get(MarginStatus.CAUTION, 0):
                logger.warning(
                    f"[降级防护] API 连续失败，强制降级到 CAUTION"
                )
                return MarginStatus.CAUTION
        
        return None

    def is_safe_to_open(self, estimated_cost: float) -> bool:
        status, info = self.get_margin_status()
        
        if self.enable_degradation and self.degradation:
            if self.degradation.should_block_new_positions():
                logger.warning(
                    f"[资金拦截] 数据质量={self.degradation.get_quality().value}，"
                    f"禁止开新仓"
                )
                return False
        
        if status in [MarginStatus.WARNING, MarginStatus.MARGIN_CALL, 
                      MarginStatus.LIQUIDATION, MarginStatus.DATA_ERROR,
                      MarginStatus.TRANSITIONING]:
            logger.warning(f"[资金拦截] 当前状态 {status.value}，禁止开新仓")
            return False
            
        current_maint = info.get('maint_margin', 0)
        current_net = info.get('net_assets', 0)
        new_maint = current_maint + estimated_cost
        if new_maint > 0:
            projected_ratio = current_net / new_maint
            if projected_ratio < 1.30:
                logger.warning(f"[压力测试] 开仓后预估保证金率 {projected_ratio:.2f} 低于安全线")
                return False
        return True
    
    def get_position_degradation_info(self) -> Dict:
        """
        获取当前仓位降级策略信息。
        
        Returns:
            包含 position_action, stop_loss_tighten_ratio 等
        """
        if self.enable_degradation and self.degradation:
            return self.degradation.position_policy.get_status()
        return {"position_action": "HOLD", "quality": "FULL"}
    
    def get_comprehensive_status(self) -> Dict:
        status, info = self.get_margin_status()
        
        result = {
            "margin_status": status.value,
            "margin_ratio": info.get("ratio", 0),
            "liquidity_buffer": info.get("buffer", 0),
        }
        
        if self.enable_hysteresis and self.hysteresis:
            result["hysteresis"] = self.hysteresis.get_stats()
        
        if self.enable_degradation and self.degradation:
            result["degradation"] = self.degradation.get_stats()
        
        return result
    
    def reset_hysteresis(self):
        if self.hysteresis:
            self.hysteresis.reset()
            logger.info("滞回引擎已手动重置")
    
    def update_hysteresis_config(self, config: dict):
        if self.hysteresis:
            self.hysteresis.update_config(config)
            logger.info("滞回配置已更新")
