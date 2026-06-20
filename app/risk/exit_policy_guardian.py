#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
退出策略守卫 (Exit Policy Guardian)

设计哲学 (Karpathy 原则):
1. 代码不能违背配置 — 启动时强制校验
2. 单一真相源 — exit_policy.yaml 是唯一参数来源
3. 手术刀式验证 — 只检查关键断言，不做全量测试
4. 降级安全 — 校验失败时用保守策略，不崩溃

职责:
1. 加载 exit_policy.yaml 为结构化配置
2. 启动时断言：position_exit_monitor 的规则必须覆盖所有 enabled 的规则
3. 运行时提供统一参数查询接口
4. 每次对账后重新校验

作者: FTSeek-US
日期: 2026-04-25
"""

import yaml
import logging
import os
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

logger = logging.getLogger("exit_policy_guardian")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_POLICY_PATH = BASE_DIR / "app" / "risk" / "exit_policy.yaml"


@dataclass
class PolicyResult:
    """策略加载结果"""
    version: str
    rules_enabled: List[str] = field(default_factory=list)
    rules_violated: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    is_valid: bool = True


class ExitPolicyGuardian:
    """
    退出策略守卫
    
    使用方式:
        guardian = ExitPolicyGuardian()
        result = guardian.validate()
        if not result.is_valid:
            logger.error(f"策略校验失败: {result.rules_violated}")
        
        # 运行时查询
        atr_mult = guardian.get_atr_multiplier("US.AAPL", "LONG", profit_atr=3.0)
    """
    
    def __init__(self, policy_path: str = None):
        self.policy_path = policy_path or str(DEFAULT_POLICY_PATH)
        self.policy: Dict[str, Any] = {}
        self._sector_map: Dict[str, str] = {}  # symbol -> sector 映射
        
    def load(self) -> Dict[str, Any]:
        """加载 YAML 策略文件"""
        try:
            with open(self.policy_path, 'r') as f:
                self.policy = yaml.safe_load(f)
            logger.info(f"✅ 退出策略已加载: {self.policy.get('version', '?')} ({self.policy_path})")
            return self.policy
        except FileNotFoundError:
            logger.error(f"❌ 策略文件不存在: {self.policy_path}")
            self.policy = self._default_policy()
            return self.policy
        except Exception as e:
            logger.error(f"❌ 策略文件解析失败: {e}")
            self.policy = self._default_policy()
            return self.policy
    
    def validate(self) -> PolicyResult:
        """
        启动时策略校验 — 验证代码是否覆盖了所有启用的规则
        
        这是防止策略退化的关键防线。
        """
        if not self.policy:
            self.load()
        
        result = PolicyResult(version=self.policy.get('version', '?'))
        rules = self.policy.get('exit_rules', {})
        
        # 检查所有 enabled 规则是否在代码中实现
        for rule_name, rule_config in rules.items():
            if not isinstance(rule_config, dict):
                continue
            
            if rule_config.get('enabled', False):
                result.rules_enabled.append(rule_name)
                
                # 验证该规则是否有对应的代码实现
                violation = self._check_rule_implemented(rule_name, rule_config)
                if violation:
                    result.rules_violated.append(violation)
                    result.is_valid = False
        
        if result.is_valid:
            logger.info(f"✅ 策略校验通过: {len(result.rules_enabled)} 条规则全部实现")
        else:
            logger.error(f"❌ 策略校验失败: {result.rules_violated}")
        
        return result
    
    def _check_rule_implemented(self, rule_name: str, config: Dict) -> Optional[str]:
        """
        检查规则是否在 position_exit_monitor 中实现
        
        返回 None 表示通过，返回字符串表示违规描述
        """
        # 规则验证矩阵 — 随着规则逐步实现，更新此矩阵
        implementation_status = {
            'flash_exit': {
                'required_code': 'flash_multiplier * atr',
                'check': lambda: self._grep_in_daemon('flash_multiplier'),
            },
            'atr_stop': {
                'required_code': 'entry_price - atr * atr_mult (ATR动态止损)',
                'check': lambda: self._grep_in_daemon('atr_stop') or self._grep_in_daemon('atr_mult'),
            },
            'chandelier_trail': {
                'required_code': 'chandelier_stop = peak - atr * trail_mult',
                'check': lambda: self._grep_in_daemon('chandelier_stop') or self._grep_in_daemon('compute_stop_lines'),
            },
            'breakeven': {
                'required_code': 'breakeven_stop = entry * (1 + buffer)',
                'check': lambda: self._grep_in_daemon('breakeven_stop') or self._grep_in_daemon('compute_stop_lines'),
            },
            'scale_out': {
                'required_code': 'scale_out / tier / close_pct',
                'check': lambda: self._grep_in_daemon('scale_out') or self._grep_in_daemon('take_profit_levels'),
            },
            'time_stop': {
                'required_code': 'held_hours >= max_holding_hours',
                'check': lambda: self._grep_in_daemon('time_stop') or self._grep_in_daemon('held_hours'),
            },
            'cost_buffer': {
                'required_code': 'cost_buffer_price = current_price * cost_bps/1e4 * k',
                'check': lambda: self._grep_in_daemon('cost_buffer_price') or self._grep_in_daemon('cost_buffer'),
            },
            'earnings_guard': {
                'required_code': 'earnings_guard.get_earnings_guard() or is_in_earnings_window()',
                'check': lambda: self._grep_in_daemon('is_in_earnings_window') or self._grep_in_daemon('flash_multiplier_override') or self._grep_in_daemon('earnings_guard') or self._grep_in_daemon('get_earnings_guard'),
            },
            'pre_close_exit': {
                'required_code': '_check_pre_close_exit() with time-weighted tiered exit',
                'check': lambda: self._grep_in_daemon('_check_pre_close_exit') or self._grep_in_daemon('preclose_result'),
            },
            'pre_close': {
                'required_code': 'PreCloseExitRule or check_adaptive_pre_close_exit',
                'check': lambda: self._grep_in_daemon('PreCloseExitRule') or self._grep_in_daemon('check_adaptive_pre_close_exit'),
            },
        }
        
        checker = implementation_status.get(rule_name)
        if not checker:
            return f"规则 '{rule_name}' 未定义校验逻辑"
        
        if not checker['check']():
            return f"规则 '{rule_name}' (enabled=true) 未在代码中实现 — 需要: {checker['required_code']}"
        
        return None
    
    def _grep_in_daemon(self, pattern: str) -> bool:
        """在 exit monitor + ATR engine 中搜索代码模式

        P0-3 修复 (2026-04-29): 同时搜索 atr_stop_engine.py（核心定价逻辑所在），
        防止因实现在 engine 中而导致校验假阳性。
        """
        import re
        search_files = [
            BASE_DIR / "app" / "risk" / "position_exit_monitor.py",
            BASE_DIR / "app" / "risk" / "atr_stop_engine.py",
            BASE_DIR / "app" / "risk" / "earnings_guard.py",
        ]
        for fpath in search_files:
            try:
                if not fpath.exists():
                    continue
                with open(fpath, 'r') as f:
                    if re.search(pattern, f.read(), re.IGNORECASE):
                        return True
            except Exception:
                continue
        return False
    
    def get_atr_multiplier(self, symbol: str, direction: str, profit_atr: float = 0) -> float:
        """
        获取 ATR 止损倍数 — 综合行业、方向、利润深度

        非线性状态机 (2026-05-30 Task#7 扩展为5档):
        - profit_atr < 0.5:           initial      (2.0x)
        - 0.5 <= profit_atr < 1.0:    profit_05atr (1.8x)  ← 新增
        - 1.0 <= profit_atr < 2.0:    profit_1atr  (1.6x)
        - 2.0 <= profit_atr < 5.0:    profit_2atr  (1.4x)  ← 新增
        - profit_atr >= 5.0:          profit_5atr  (1.2x)

        优先级: 行业覆盖 > 非线性状态机 > 做空覆盖 > 默认值
        """
        rules = self.policy.get('exit_rules', {})
        atr_config = rules.get('atr_stop', {})
        multipliers = atr_config.get('multiplier_by_profit', {})

        # 1. 根据利润深度计算非线性倍数 (核心逻辑, 5档分级)
        if profit_atr >= 5.0:
            base_mult = multipliers.get('profit_5atr', 1.2)
        elif profit_atr >= 2.0:
            base_mult = multipliers.get('profit_2atr', 1.4)
        elif profit_atr >= 1.0:
            base_mult = multipliers.get('profit_1atr', 1.6)
        elif profit_atr >= 0.5:
            base_mult = multipliers.get('profit_05atr', 1.8)
        else:
            base_mult = multipliers.get('initial', 4.0)  # 2026-06-05: 2.0→4.0 SSOT对齐

        # 2. 做空更紧 (取 行业/非线性 与 short_override 的较小值)
        if direction == 'SHORT':
            short_overrides = self.policy.get('short_overrides', {})
            base_mult = min(base_mult, short_overrides.get('atr_multiplier', 2.0))

        # 3. 行业覆盖 (仅当有明确的行业映射时)
        sector = self._sector_map.get(symbol)
        if sector:
            sector_config = self.policy.get('sector_overrides', {}).get(sector, {})
            if 'atr_multiplier' in sector_config:
                base_mult = sector_config['atr_multiplier']

        return base_mult
    
    def get_flash_multiplier(self, symbol: str = None) -> float:
        """获取 Flash Exit 阈值"""
        rules = self.policy.get('exit_rules', {})
        base = rules.get('flash_exit', {}).get('flash_multiplier_default', 0.8)
        
        if symbol:
            sector = self._sector_map.get(symbol, 'default')
            sector_config = self.policy.get('sector_overrides', {}).get(sector, {})
            if 'flash_multiplier' in sector_config:
                base = sector_config['flash_multiplier']
        
        return base
    
    def get_scale_out_tiers(self, direction: str = 'LONG') -> List[Dict]:
        """获取分层止盈配置"""
        rules = self.policy.get('exit_rules', {})
        config = rules.get('scale_out', {})
        
        if not config.get('enabled', False):
            return []
        
        tiers = config.get('tiers', [])
        
        # 做空差异化
        if direction == 'SHORT':
            short_overrides = self.policy.get('short_overrides', {})
            so = short_overrides.get('scale_out', {})
            if so:
                return [
                    {'level': 1, 'trigger_profit_pct': so.get('tier_1_pct', 10), 'close_pct': 40},
                    {'level': 2, 'trigger_profit_pct': so.get('tier_2_pct', 20), 'close_pct': 60},
                ]
        
        return tiers
    
    def should_earnings_tighten(self, symbol: str) -> bool:
        """判断是否触发财报收紧"""
        # 简化实现 — 实际应接入 earnings_calendar
        rules = self.policy.get('exit_rules', {})
        return rules.get('earnings_guard', {}).get('enabled', False)
    
    def set_sector_map(self, sector_map: Dict[str, str]):
        """设置股票 -> 行业映射"""
        self._sector_map = sector_map
    
    def _default_policy(self) -> Dict[str, Any]:
        """策略文件不存在时的降级默认值"""
        return {
            'version': 'fallback-1.0',
            'exit_rules': {
                'flash_exit': {'enabled': True, 'flash_multiplier_default': 0.8},
                'atr_stop': {'enabled': True, 'multiplier_by_profit': {'initial': 4.0}},  # 2026-06-05: 2.5→4.0 SSOT
                'breakeven': {'enabled': True, 'trigger_profit_atr': 0.4, 'buffer_pct': 0.15},
                'chandelier_trail': {'enabled': True, 'activation_profit_atr': 0.8},
                'scale_out': {'enabled': False},
                'time_stop': {'enabled': True, 'max_holding_hours': 72},
                'earnings_guard': {'enabled': False},
            },
            'short_overrides': {'atr_multiplier': 2.0, 'max_loss_pct': 10.0},
            'sector_overrides': {'default': {'atr_multiplier': 3.0}},  # 2026-06-05: 2.5→3.0 与yaml:224对齐
        }


# 全局单例
_guardian: Optional[ExitPolicyGuardian] = None


def get_guardian() -> ExitPolicyGuardian:
    """获取全局策略守卫"""
    global _guardian
    if _guardian is None:
        _guardian = ExitPolicyGuardian()
        _guardian.load()
    return _guardian


def validate_exit_policy() -> PolicyResult:
    """便捷函数：启动时校验"""
    return get_guardian().validate()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    guardian = ExitPolicyGuardian()
    guardian.load()
    result = guardian.validate()
    
    print(f"\n策略版本: {result.version}")
    print(f"启用规则: {result.rules_enabled}")
    print(f"校验结果: {'✅ 通过' if result.is_valid else '❌ 失败'}")
    if result.rules_violated:
        print(f"违规规则: {result.rules_violated}")
    
    # 测试查询
    print(f"\nATR 倍数测试:")
    print(f"  AAPL LONG (盈利 0 ATR): {guardian.get_atr_multiplier('US.AAPL', 'LONG', 0)}")
    print(f"  AAPL LONG (盈利 3 ATR): {guardian.get_atr_multiplier('US.AAPL', 'LONG', 3)}")
    print(f"  AAPL SHORT (盈利 0 ATR): {guardian.get_atr_multiplier('US.AAPL', 'SHORT', 0)}")
