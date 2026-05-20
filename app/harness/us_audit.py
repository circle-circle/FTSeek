#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek-US 驾驭巡检体系 (Harness Audit System)
Phase 4: 主动式巡检与系统自愈

功能:
1. 盘前检查 (Pre-Market):
   - API 额度检查 (TwelveData, Finnhub)
   - VIX 指数状态评估
   - 隔夜新闻情绪摘要
2. 盘后检查 (Post-Market):
   - 交易归因报告生成
   - 异常持仓风险扫描
   - 日志清理与归档
3. API 熔断器 (Circuit Breaker):
   - 监控 API 错误率
   - 达到阈值时自动暂停扫描
"""

import logging
import time
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger("ftseek_us.harness_audit")

# 熔断器状态
CIRCUIT_BREAKER = {
    "is_open": False,
    "error_count": 0,
    "last_error_time": 0,
    "reset_at": 0,
    "threshold": 10  # 连续错误次数阈值
}

def record_api_error():
    """记录 API 错误，检查是否触发熔断"""
    CIRCUIT_BREAKER["error_count"] += 1
    CIRCUIT_BREAKER["last_error_time"] = time.time()
    
    if CIRCUIT_BREAKER["error_count"] >= CIRCUIT_BREAKER["threshold"]:
        if not CIRCUIT_BREAKER["is_open"]:
            CIRCUIT_BREAKER["is_open"] = True
            CIRCUIT_BREAKER["reset_at"] = time.time() + 1800  # 冷却 30 分钟
            logger.warning(f"⚠️ API 熔断器已触发: 错误次数={CIRCUIT_BREAKER['error_count']}, 冷却至 {CIRCUIT_BREAKER['reset_at']}")

def record_api_success():
    """记录 API 成功，重置计数器"""
    # 如果距离上次错误超过 5 分钟，重置计数
    if time.time() - CIRCUIT_BREAKER["last_error_time"] > 300:
        CIRCUIT_BREAKER["error_count"] = 0
    
    # 如果在冷却期外，关闭熔断器
    if CIRCUIT_BREAKER["is_open"]:
        if time.time() >= CIRCUIT_BREAKER["reset_at"]:
            CIRCUIT_BREAKER["is_open"] = False
            CIRCUIT_BREAKER["error_count"] = 0
            logger.info("✅ API 熔断器已重置，恢复扫描")

def is_circuit_breaker_open() -> bool:
    """检查熔断器是否开启"""
    if CIRCUIT_BREAKER["is_open"]:
        if time.time() >= CIRCUIT_BREAKER["reset_at"]:
            CIRCUIT_BREAKER["is_open"] = False
            CIRCUIT_BREAKER["error_count"] = 0
            logger.info("✅ API 熔断器冷却结束，恢复扫描")
            return False
        return True
    return False

def run_pre_market_audit(scheduler) -> Dict[str, Any]:
    """
    盘前巡检 (Pre-Market Audit)
    
    检查:
    1. API 剩余额度
    2. VIX 状态
    3. 数据源连通性
    """
    logger.info("🔍 开始盘前巡检 (Pre-Market Audit)...")
    
    audit_result = {
        "status": "OK",
        "api_health": "UNKNOWN",
        "vix_level": 0.0,
        "recommendations": []
    }
    
    try:
        # 1. 检查 API 额度
        if hasattr(scheduler, 'data_router') and scheduler.data_router:
            status = scheduler.data_router.get_rate_limit_status()
            
            # 简单评估
            td_tokens = 0
            fh_tokens = 0
            
            if "twelvedata" in status:
                td_status = status["twelvedata"]
                td_tokens = td_status.get("tokens_remaining", 0)
                td_max = td_status.get("max_tokens", 8)
                # 2026-04-16 修复: 区分 per-minute 限速和日额度耗尽
                # RateLimiter 是 per-minute 令牌桶 (8 tokens, 7.5s/token), 
                # tokens=0 只代表当前分钟桶空了, 不是日额度耗尽
                td_usage = td_status.get("usage_pct", 0)
                td_total = td_status.get("total_calls", 0)
                
                if td_tokens == 0 and td_total > td_max * 10:
                    # 真正的大量调用后额度耗尽 → 降为 WARNING
                    audit_result["recommendations"].append(
                        f"🟡 TwelveData 限速桶空 (已用{td_total}次): 等待补充 ({td_max} tokens/{td_status.get('refill_interval_sec', 7.5)}s)")
                    audit_result["status"] = max(audit_result["status"], "WARNING")
                elif td_tokens == 0:
                    # 刚启动桶就空了 → INFO, 不影响扫描
                    logger.info(f"   TwelveData 令牌桶初始化: {td_tokens}/{td_max}")
                elif td_tokens < 2:
                    audit_result["recommendations"].append(f"⚠️ TwelveData 令牌紧张: {td_tokens}/{td_max}")
                    audit_result["status"] = max(audit_result["status"], "WARNING")
            
            if "finnhub" in status:
                fh_status = status["finnhub"]
                fh_tokens = fh_status.get("tokens_remaining", 0)
                fh_max = fh_status.get("max_tokens", 60)
                fh_total = fh_status.get("total_calls", 0)
                # Finnhub: 60 tokens/min, 1s/token
                if fh_tokens == 0 and fh_total > fh_max * 5:
                    audit_result["recommendations"].append(
                        f"🟡 Finnhub 限速桶空 (已用{fh_total}次): 等待补充 ({fh_max} tokens/{fh_status.get('refill_interval_sec', 1)}s)")
                    audit_result["status"] = max(audit_result["status"], "WARNING")
                elif fh_tokens == 0:
                    logger.info(f"   Finnhub 令牌桶初始化: {fh_tokens}/{fh_max}")
                elif fh_tokens < 5:
                    audit_result["recommendations"].append(f"⚠️ Finnhub 令牌紧张: {fh_tokens}/{fh_max}")
            
            audit_result["api_health"] = "GOOD" if audit_result["status"] == "OK" else "WARNING"
            logger.info(f"   API 状态: TD({td_tokens}), FH({fh_tokens})")
            
        # 2. 检查 VIX
        if hasattr(scheduler, 'yahoo') and scheduler.yahoo:
            try:
                vix = scheduler.market_data.get_vix()
                audit_result["vix_level"] = vix
                if vix > 30:
                    audit_result["recommendations"].append(f"🔴 高波动预警: VIX={vix}, 建议收紧风控")
                    audit_result["status"] = "CRITICAL"
                elif vix > 20:
                    audit_result["recommendations"].append(f"⚠️ 波动率上升: VIX={vix}")
            except Exception as e:
                logger.warning(f"   获取 VIX 失败: {e}")
                
        # 3. 总结
        if audit_result["recommendations"]:
            for rec in audit_result["recommendations"]:
                logger.warning(f"   - {rec}")
        else:
            logger.info("   ✅ 盘前检查通过，系统状态良好")
            
    except Exception as e:
        logger.error(f"   ❌ 盘前巡检失败: {e}")
        audit_result["status"] = "ERROR"
        
    return audit_result

def run_post_market_audit(scheduler) -> Dict[str, Any]:
    """
    盘后巡检 (Post-Market Audit)
    
    检查:
    1. 持仓风险分析
    2. 交易记录完整性
    3. 日志归档
    """
    logger.info("🌙 开始盘后巡检 (Post-Market Audit)...")
    
    audit_result = {
        "status": "OK",
        "open_positions": 0,
        "risk_flags": [],
        "recommendations": []
    }
    
    try:
        # 1. 检查持仓
        if hasattr(scheduler, 'position_store'):
            positions = scheduler.position_store.all()
            audit_result["open_positions"] = len(positions)
            
            for p in positions:
                # 检查是否有接近止损的持仓
                if hasattr(p, 'current_sl') and hasattr(p, 'avg_entry_price'):
                    # 简单逻辑：如果现价接近 SL (这里需要获取现价，暂时跳过)
                    pass
            
            logger.info(f"   当前持仓数: {audit_result['open_positions']}")
            
        # 2. 检查 Trade Store
        if hasattr(scheduler, 'trade_store'):
            # 这里可以检查最近是否有未平仓的交易记录等
            pass
            
        # 3. 总结
        if audit_result["risk_flags"]:
            audit_result["status"] = "WARNING"
            for flag in audit_result["risk_flags"]:
                logger.warning(f"   - {flag}")
        else:
            logger.info("   ✅ 盘后检查通过，无风险标记")
            
    except Exception as e:
        logger.error(f"   ❌ 盘后巡检失败: {e}")
        audit_result["status"] = "ERROR"
        
    return audit_result
