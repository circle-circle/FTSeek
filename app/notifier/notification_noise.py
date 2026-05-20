# -*- coding: utf-8 -*-
"""通知降噪控制模块 (FTSeek 适配版)

基于 DSA 架构改进，适配 FTSeek 的多渠道通知和交易场景。
使用内存字典 + 线程锁实现轻量级去重/冷却/静默期控制。
适用于钉钉/飞书推送，防止消息轰炸。
"""
from __future__ import annotations
import hashlib
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Optional, List, Tuple

logger = logging.getLogger("ftseek.notification_noise")

# 线程安全的状态存储
_dedup_expires: Dict[str, float] = {}
_cooldown_expires: Dict[str, float] = {}
_state_lock = threading.Lock()
_last_cleanup: float = 0.0

# 默认配置
DEFAULT_DEDUP_TTL = 600       # 相同内容去重: 10 分钟
DEFAULT_COOLDOWN = 1800       # 同标的冷却: 30 分钟
DEFAULT_QUIET_HOURS: List[Tuple[int, int]] = [(23, 8)]  # 23:00-08:00 静默

def _hash_content(content: str) -> str:
    """生成内容 Hash"""
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def check_and_log(
    content: str, 
    route_type: str = "default",
    symbol: str = None,
    severity: str = "info",
    dedup_ttl: int = DEFAULT_DEDUP_TTL,
    cooldown_seconds: int = DEFAULT_COOLDOWN,
    respect_quiet_hours: bool = True
) -> bool:
    """检查是否应发送通知
    
    Args:
        content: 通知内容
        route_type: 路由类型 (plan/alert/intercept/error)
        symbol: 关联标的 (如 HK.00700)
        severity: 严重程度 (info/warning/critical)
        dedup_ttl: 去重 TTL (秒)
        cooldown_seconds: 冷却时间 (秒)
        respect_quiet_hours: 是否遵守静默期
        
    Returns:
        True: 应发送 (未命中降噪规则)
        False: 应拦截 (命中降噪规则)
    """
    now = time.time()
    content_hash = _hash_content(content)
    
    # 惰性清理: 每 60 秒清理一次过期条目 (防止内存无限增长)
    global _last_cleanup
    if now - _last_cleanup > 60:
        with _state_lock:
            expired_dedup = [k for k, v in _dedup_expires.items() if v <= now]
            expired_cooldown = [k for k, v in _cooldown_expires.items() if v <= now]
            for k in expired_dedup:
                del _dedup_expires[k]
            for k in expired_cooldown:
                del _cooldown_expires[k]
            _last_cleanup = now
    
    with _state_lock:
        # 1. Critical/Error 级别跳过所有降噪 (P0 告警必须送达)
        if severity in ("critical", "error"):
            # 仅做最短去重 (1 分钟) 防止代码 bug 导致死循环轰炸
            if content_hash in _dedup_expires and _dedup_expires[content_hash] > now:
                return False
            _dedup_expires[content_hash] = now + 60
            return True
            
        # 2. 检查静默期 (非紧急消息拦截)
        if respect_quiet_hours:
            current_hour = datetime.now().hour
            for start, end in DEFAULT_QUIET_HOURS:
                # 处理跨午夜情况 (如 23:00-08:00)
                if start > end:
                    if current_hour >= start or current_hour < end:
                        logger.debug(f"[通知降噪] 静默期拦截: {route_type}")
                        return False
                else:
                    if start <= current_hour < end:
                        logger.debug(f"[通知降噪] 静默期拦截: {route_type}")
                        return False
        
        # 3. 内容去重检查
        if dedup_ttl > 0:
            if content_hash in _dedup_expires and _dedup_expires[content_hash] > now:
                logger.debug(f"[通知降噪] 去重拦截: hash={content_hash[:6]}")
                return False
            _dedup_expires[content_hash] = now + dedup_ttl
            
        # 4. 标的冷却检查 (同标的同类型消息冷却)
        if symbol and cooldown_seconds > 0:
            key = f"cooldown:{route_type}:{symbol}"
            if key in _cooldown_expires and _cooldown_expires[key] > now:
                logger.debug(f"[通知降噪] 冷却拦截: {symbol}")
                return False
            _cooldown_expires[key] = now + cooldown_seconds
            
    return True

def reset_noise_state():
    """测试用：重置降噪状态"""
    with _state_lock:
        _dedup_expires.clear()
        _cooldown_expires.clear()
