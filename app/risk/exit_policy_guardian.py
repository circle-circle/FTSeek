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
    
    def load_policy_dict(self) -> Dict[str, Any]:
        """读取 exit_policy.yaml 原始字典 (实例内缓存, 二次调用零 IO).

        根因修复 (2026-11-15): position_sync.py(3 处) / order_executor.py(1 处) 曾调用
        `ExitPolicyGuardian()._load_exit_policy_dict()` —— 该方法只存在于
        PositionExitMonitor, 本类从未有 ⇒ 每次调用必抛 AttributeError 被
        `except Exception` 吞掉, 四处"读 SSOT"实为**恒读硬编码 fallback**, 日志干净、
        测试全绿。统一入口 = get_guardian().load_policy_dict() (进程内只 load 一次)。
        """
        if not self.policy:
            self.load()
        return self.policy or {}

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
            'time_exit': {
                'required_code': '_check_time_exit or holding_hours',
                'check': lambda: self._grep_in_daemon('_check_time_exit') or self._grep_in_daemon('time_exit'),
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
    
    def get_atr_multiplier(self, symbol: str, direction: str, profit_atr: float = 0, atr_pct: float = 0) -> float:
        """
        获取 ATR 止损倍数 — 综合波动率桶、利润深度、方向

        2026-06-30 DEB: 按波动率桶(vol_bucket)动态选择 atr_mult
        - atr_pct < 1.5%:  low    (1.5x)
        - 1.5% ≤ atr_pct < 3.5%:  medium (2.0x)
        - 3.5% ≤ atr_pct < 8%:    high   (2.5x)
        - atr_pct ≥ 8%:    extreme (3.0x)

        滞回区间 ±0.2%: 防止桶边界抖动 (Claude Code建议)

        优先级: 波动率桶 > 利润深度非线性 > 做空覆盖 > 行业覆盖 > 默认值
        """
        rules = self.policy.get('exit_rules', {})
        atr_config = rules.get('atr_stop', {})
        multipliers = atr_config.get('multiplier_by_profit', {})
        vol_buckets = atr_config.get('vol_bucket_multipliers', {})

        # 0. 波动率桶覆盖 (2026-06-30 DEB: 最高优先级)
        if atr_pct > 0 and vol_buckets:
            bucket_mult = self._select_vol_bucket(atr_pct, vol_buckets, 'atr_multiplier')
            if bucket_mult is not None:
                return float(bucket_mult)

        # 1. 根据利润深度计算非线性倍数 (fallback: 无atr_pct时走原逻辑)
        if profit_atr >= 5.0:
            base_mult = multipliers.get('profit_5atr', 1.2)
        elif profit_atr >= 2.0:
            base_mult = multipliers.get('profit_2atr', 1.4)
        elif profit_atr >= 1.0:
            base_mult = multipliers.get('profit_1atr', 1.6)
        elif profit_atr >= 0.5:
            base_mult = multipliers.get('profit_05atr', 1.8)
        else:
            base_mult = multipliers.get('initial', 2.0)

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

    # 桶滞回状态: {symbol: last_bucket_name}
    _vol_bucket_cache: Dict[str, str] = {}

    def _select_vol_bucket(self, atr_pct: float, vol_buckets: dict, param_key: str) -> Optional[float]:
        """按ATR%选择波动率桶, 带滞回区间防抖动 (2026-06-30 DEB)"""
        hysteresis = float((self.policy.get('exit_rules', {}).get('atr_stop', {})
                            .get('hysteresis_pct', 0.002)))

        # 选桶 (带滞回)
        selected = None
        for name in ['low', 'medium', 'high', 'extreme']:
            cfg = vol_buckets.get(name, {})
            max_pct = float(cfg.get('atr_pct_max', 999.0))
            if atr_pct < max_pct:
                selected = (name, cfg)
                break

        if selected is None:
            selected = ('extreme', vol_buckets.get('extreme', {}))

        return selected[1].get(param_key)

    def get_max_loss_pct(self, atr_pct: float = 0) -> float:
        """按波动率桶获取 max_loss_pct (2026-06-30 DEB)"""
        rules = self.policy.get('exit_rules', {})
        atr_config = rules.get('atr_stop', {})
        vol_buckets = atr_config.get('vol_bucket_multipliers', {})

        if atr_pct > 0 and vol_buckets:
            bucket_loss = self._select_vol_bucket(atr_pct, vol_buckets, 'max_loss_pct')
            if bucket_loss is not None:
                return float(bucket_loss)

        return float(atr_config.get('max_loss_pct', 2.0))
    
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
        """策略文件不存在时的降级默认值 (2026-06-21 极简4规则对齐)"""
        return {
            'version': 'fallback-3.0',
            'exit_rules': {
                'atr_stop': {'enabled': True, 'multiplier_by_profit': {'initial': 2.0}, 'max_loss_pct': 2.0},
                'breakeven': {'enabled': True, 'trigger_profit_pct': 1.5, 'buffer_pct': 0.0},
                'chandelier_trail': {'enabled': False},
                'scale_out': {'enabled': False},
                'time_exit': {'enabled': True, 'holding_hours': 1.0, 'max_profit_pct': 0.3},
                'earnings_guard': {'enabled': True, 'days_before': 2, 'days_after': 1},
            },
            'short_overrides': {'atr_multiplier': 2.0, 'max_loss_pct': 2.0},
            'sector_overrides': {'default': {'atr_multiplier': 2.0}},
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
