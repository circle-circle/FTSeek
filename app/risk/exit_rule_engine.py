#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
退出规则引擎 (Exit Rule Engine) — Phase 1

模块化、可插拔的止损止盈框架，港美共用同一引擎。
Phase 1 目标：封装 3 个核心规则，双跑模式验证。
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger("ftseek.exit_rule_engine")

# ============================================================
#  数据模型
# ============================================================

@dataclass
class ExitContext:
    """退出决策上下文（所有规则共享，避免重复计算）"""
    code: str
    entry_price: float
    current_price: float
    peak_price: float
    atr_value: float
    entry_time: Any  # datetime 或 ISO 字符串
    direction: str   # 'BUY' or 'SELL'
    is_same_day: bool
    current_volume: float = 0.0
    avg_volume: float = 0.0
    exit_state: str = ""
    pnl_pct: float = 0.0
    
    def __post_init__(self):
        if self.entry_price > 0 and self.pnl_pct == 0.0:
            if self.direction == 'BUY':
                self.pnl_pct = (self.current_price - self.entry_price) / self.entry_price
            else:
                self.pnl_pct = (self.entry_price - self.current_price) / self.entry_price


@dataclass
class ExitDecision:
    should_exit: bool = False
    reason: str = ""
    priority: int = 99
    close_ratio: float = 1.0
    is_stop_loss: bool = False
    new_state: str = ""
    new_stop_loss: float = 0.0
    rule_name: str = ""  # Phase 1 新增：归因追踪


# ============================================================
#  规则基类
# ============================================================

class ExitRule(ABC):
    """退出规则基类"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """规则名称（唯一标识，对应 YAML key）"""
    
    @property
    @abstractmethod
    def priority(self) -> int:
        """优先级（越小越紧急）"""
    
    @abstractmethod
    def enabled(self, config: dict) -> bool:
        """是否启用"""
    
    @abstractmethod
    def evaluate(self, ctx: ExitContext, config: dict) -> Optional[ExitDecision]:
        """评估是否触发退出"""


# ============================================================
#  规则引擎
# ============================================================

class ExitRuleEngine:
    """退出规则引擎（港美共用）"""
    
    def __init__(self):
        self.rules: List[ExitRule] = []
        self.config: Dict[str, Any] = {}
        self._stats = {"evaluations": 0, "triggers": 0, "by_rule": {}}
    
    def register(self, rule: ExitRule):
        self.rules.append(rule)
        self.rules.sort(key=lambda r: r.priority)
        logger.info(f"[ExitEngine] 注册规则: {rule.name} (P{rule.priority})")
    
    def configure(self, config: dict):
        """从 YAML 加载规则配置"""
        self.config = config
        for rule in self.rules:
            en = rule.enabled(config)
            logger.info(f"[ExitEngine] {rule.name}: {'启用' if en else '禁用'}")
    
    def evaluate(self, ctx: ExitContext) -> ExitDecision:
        self._stats["evaluations"] += 1
        
        for rule in self.rules:
            if not rule.enabled(self.config):
                continue
            
            decision = rule.evaluate(ctx, self.config)
            if decision and decision.should_exit:
                decision.rule_name = rule.name
                self._stats["triggers"] += 1
                self._stats["by_rule"][rule.name] = self._stats["by_rule"].get(rule.name, 0) + 1
                logger.debug(
                    f"[ExitEngine] {ctx.code} 触发: {rule.name} (P{rule.priority}) | "
                    f"原因: {decision.reason}"
                )
                return decision
        
        return ExitDecision(should_exit=False)
    
    def get_stats(self) -> dict:
        return dict(self._stats)


# ============================================================
#  全局单例
# ============================================================

_exit_rule_engine: Optional[ExitRuleEngine] = None

def get_exit_rule_engine() -> ExitRuleEngine:
    global _exit_rule_engine
    if _exit_rule_engine is None:
        _exit_rule_engine = ExitRuleEngine()
    return _exit_rule_engine
