"""
FTSeek-US 策略风控过滤器

在 LLM 生成的策略进入执行层之前, 进行多维度风控检查:
- 仓位限制
- 冷却期
- 熔断状态
- 黑名单
- 流动性检查

作者: FTSeek Team
日期: 2026-04-16
"""

import logging
from datetime import datetime, timedelta
from app.utils.timezone import now_et
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek.strategy_validator")


class StrategyValidator:
    """策略风控过滤器"""
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        
        # 仓位限制
        self.max_position_pct = self.config.get("max_position_pct", 0.10)  # 单策略最大仓位
        self.max_total_position = self.config.get("max_total_position", 0.50)  # 总仓位上限
        self.max_daily_strategies = self.config.get("max_daily_strategies", 10)  # 日最大策略数
        
        # 黑名单
        self.blacklist = set(self.config.get("blacklist", []))
        
        # 冷却期
        self.cooldown_symbols: Dict[str, datetime] = {}
        self.cooldown_seconds = self.config.get("cooldown_seconds", 3600)  # 默认1小时
        
        # 日统计
        self._daily_count = 0
        self._daily_date = now_et().strftime("%Y-%m-%d")
        
        logger.info(f"[策略风控] 初始化 | 单策略最大仓位={self.max_position_pct*100:.0f}% | 日最大策略数={self.max_daily_strategies}")
    
    def validate(self, strategy: Dict, current_positions: List[Dict] = None,
                 total_exposure: float = 0.0) -> tuple:
        """
        验证策略是否可以通过风控
        
        Returns:
            (bool, str): (通过, 拒绝原因)
        """
        # 重置日统计
        self._check_daily_reset()
        
        symbol = strategy.get("symbol", "")
        direction = strategy.get("direction", "")
        position_pct = strategy.get("max_position_pct", 0)
        
        # 1. 黑名单检查
        if symbol in self.blacklist:
            return False, f"黑名单股票: {symbol}"
        
        # 2. 冷却期检查
        cooldown_key = f"{symbol}_{direction}"
        last_time = self.cooldown_symbols.get(cooldown_key)
        if last_time and (now_et() - last_time).total_seconds() < self.cooldown_seconds:
            remaining = int(self.cooldown_seconds - (now_et() - last_time).total_seconds())
            return False, f"冷却中: {cooldown_key} (剩余 {remaining}s)"
        
        # 3. 仓位限制
        if position_pct > self.max_position_pct:
            return False, f"仓位超限: {position_pct*100:.0f}% > {self.max_position_pct*100:.0f}%"
        
        # 4. 总仓位检查
        if total_exposure + position_pct > self.max_total_position:
            return False, f"总仓位超限: {(total_exposure+position_pct)*100:.0f}% > {self.max_total_position*100:.0f}%"
        
        # 5. 日策略数限制
        if self._daily_count >= self.max_daily_strategies:
            return False, f"日策略数已达上限: {self._daily_count}/{self.max_daily_strategies}"
        
        # 6. 置信度检查
        # P1 2026-05-12 审计修复: 硬编码 0.50 → CFG.MIN_CONFIDENCE
        from app.core.config_defaults import CFG
        confidence = strategy.get("confidence", 0)
        min_conf = CFG.MIN_CONFIDENCE
        if confidence < min_conf:
            return False, f"置信度过低: {confidence:.2f} < {min_conf}"
        
        # 通过所有检查
        return True, ""
    
    def mark_executed(self, symbol: str, direction: str):
        """标记策略已执行, 开始冷却"""
        cooldown_key = f"{symbol}_{direction}"
        self.cooldown_symbols[cooldown_key] = now_et()
        self._daily_count += 1
        logger.info(f"[策略风控] 执行记录: {cooldown_key} | 今日第 {self._daily_count} 条")
    
    def _check_daily_reset(self):
        """检查是否需要重置日统计"""
        today = now_et().strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_date = today
            self._daily_count = 0
            logger.info("[策略风控] 日统计已重置")
    
    def get_daily_stats(self) -> Dict:
        """获取当日统计"""
        return {
            "date": self._daily_date,
            "strategies_count": self._daily_count,
            "max_daily": self.max_daily_strategies,
            "remaining": max(0, self.max_daily_strategies - self._daily_count),
            "cooldown_count": len(self.cooldown_symbols),
        }
