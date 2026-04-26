"""
主动利润管理器 (ActiveProfitManager)

职责:
1. 综合 资金状态 + 持仓画像 + 波动率 (ATR) 计算动态离场参数。
2. 实现模式切换的防抖 (Hysteresis) 机制 (预留)。
3. 提供标准化的规则输出给 TradeExitManager 执行。
"""

import os
import time
import yaml
import logging
from typing import Dict, Any, Optional

from app.risk.futu_margin_monitor import MarginStatus

logger = logging.getLogger("ftseek.profit.active_manager")

class ActiveProfitManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ActiveProfitManager, cls).__new__(cls)
            cls._instance._initialized = False
            cls._instance.config = {}
            cls._instance.margin_monitor = None
            cls._instance.mdr = None
            cls._instance.position_modes: Dict[str, str] = {} # For hysteresis
        return cls._instance

    def initialize(self, config_path: str, margin_monitor, market_data_router):
        if self._initialized:
            return
        self.config = self._load_config(config_path)
        self.margin_monitor = margin_monitor
        self.mdr = market_data_router
        self._initialized = True
        logger.info("🧠 主动利润管理引擎 (ActiveProfitManager) 已启动")

    def _load_config(self, path: str) -> dict:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"❌ 加载利润矩阵配置失败: {e}")
            return {}

    def get_exit_rules(self, position: Dict) -> Dict[str, Any]:
        """
        获取当前持仓的动态离场规则
        
        Returns:
            Dict: {
                "action": "HOLD" | "RISK_REDUCE" | "EMERGENCY_EXIT",
                "mode": "PROFIT_RUN" | "CAUTIOUS_RUN" | ...,
                "stop_loss_price": 123.45,
                "scale_out_targets": [1.0, 2.0], # R levels
                "force_breakeven": False,
                "reduce_pct": 0.5
            }
        """
        if not self._initialized or not self.config:
            return {"action": "HOLD", "mode": "ERROR"}

        symbol = position.get('symbol')
        entry_price = position.get('entry_price', 0)
        profile = position.get('profile', 'EVENT') # Default conservative

        # 1. 获取资金状态
        margin_status = MarginStatus.SAFE # Default fallback
        try:
            margin_status, _ = self.margin_monitor.get_margin_status()
        except Exception:
            logger.warning("⚠️ 无法获取资金状态，默认 SAFE")
            
        status_key = margin_status.value

        # 2. 获取市场波动 (ATR)
        # 假设 MDR 提供了 get_atr(symbol, period) 方法
        atr = 0.0
        try:
            if self.mdr:
                atr = self.mdr.get_atr(symbol, period=14) 
        except Exception as e:
            logger.debug(f"ATR 获取失败 {symbol}: {e}")
            
        # Fallback: 2% of entry price
        if not atr or atr <= 0:
            atr = entry_price * 0.02 if entry_price > 0 else 0.0
            
        # 3. 路由配置
        status_config = self.config.get(status_key, {})
        # Try specific profile, then DEFAULT
        profile_config = status_config.get(profile, status_config.get('DEFAULT', {}))
        
        # 4. 安全强制覆盖 (Hardcoded Safety Overrides)
        # 无论配置如何，资金恶化必须执行特定逻辑
        if margin_status == MarginStatus.WARNING:
            profile_config['force_breakeven'] = True
        elif margin_status == MarginStatus.MARGIN_CALL:
            profile_config['action'] = 'RISK_REDUCE'
            profile_config['reduce_pct'] = profile_config.get('reduce_pct', 0.5)
        elif margin_status == MarginStatus.LIQUIDATION:
            profile_config['action'] = 'EMERGENCY_EXIT'
            profile_config['reduce_pct'] = 1.0

        # 5. 计算具体数值
        rules = {
            "action": profile_config.get('action', 'HOLD'),
            "mode": self._decide_mode(profile_config),
            "trail_atr_mult": profile_config.get('trail_atr_mult', 2.0),
            "atr": atr,
            "scale_out_targets": profile_config.get('scale_out_r', []),
            "force_breakeven": profile_config.get('force_breakeven', False),
            "max_hold_mins": profile_config.get('max_hold_mins'),
            "reduce_pct": profile_config.get('reduce_pct', 0),
            "ignore_technicals": profile_config.get('ignore_technicals', False)
        }
        
        # 6. 计算目标止损价 (Dynamic Stop Loss)
        # 基础公式: Stop = HighWaterMark - (ATR * Multiplier)
        hwm = position.get('high_water_mark', entry_price)
        dynamic_stop = hwm - (atr * rules['trail_atr_mult'])
        
        # 保本保护逻辑：如果强制保本，止损不得低于 Entry + Fees
        if rules['force_breakeven']:
            # 假设 0.5% 费用+滑点缓冲
            breakeven_price = entry_price * 1.005 
            dynamic_stop = max(dynamic_stop, breakeven_price)
            
        rules['stop_loss_price'] = dynamic_stop
        
        return rules

    def _decide_mode(self, config: Dict) -> str:
        """
        辅助判断当前离场模式 (用于日志和监控)
        """
        if config.get('action') == 'EMERGENCY_EXIT':
            return "EMERGENCY_EXIT"
        if config.get('action') == 'RISK_REDUCE':
            return "RISK_REDUCE"
        if config.get('force_breakeven'):
            return "BREAKEVEN_LOCK"
        if config.get('trail_atr_mult', 0) > 3.0:
            return "PROFIT_RUN"
        return "CAUTIOUS_RUN"
