#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置校验器 (Config Validator) - FTSeek v5.0

功能：
1. 验证配置文件的一致性和合理性
2. 检测配置冲突（如：止盈<止损）
3. 提供配置合并和加载接口
4. 支持配置热重载监听

使用示例：
    validator = ConfigValidator()
    validator.validate_all()
    
    # 获取合并后的配置
    config = validator.get_merged_config()
"""

import os
import yaml
import json
import logging
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.config_validator")
# 2026-04-15: propagate=True，让校验日志输出到主日志文件
logger.propagate = True


# ============================================================
#  校验结果数据类
# ============================================================
@dataclass
class ValidationIssue:
    """配置校验问题"""
    severity: str  # 'error', 'warning', 'info'
    category: str  # 'conflict', 'range', 'missing', 'deprecated'
    message: str
    file: str = ""
    field: str = ""
    suggestion: str = ""


@dataclass
class ValidationResult:
    """校验结果汇总"""
    is_valid: bool = True
    errors: List[ValidationIssue] = field(default_factory=list)
    warnings: List[ValidationIssue] = field(default_factory=list)
    infos: List[ValidationIssue] = field(default_factory=list)
    
    def add_error(self, **kwargs):
        issue = ValidationIssue(severity='error', **kwargs)
        self.errors.append(issue)
        self.is_valid = False
    
    def add_warning(self, **kwargs):
        self.warnings.append(ValidationIssue(severity='warning', **kwargs))
    
    def add_info(self, **kwargs):
        self.infos.append(ValidationIssue(severity='info', **kwargs))
    
    def summary(self) -> str:
        return (
            f"校验结果: {'✅ 通过' if self.is_valid else '❌ 失败'} | "
            f"错误={len(self.errors)} | 警告={len(self.warnings)} | 提示={len(self.infos)}"
        )


# ============================================================
#  配置校验器主类
# ============================================================
class ConfigValidator:
    """配置校验器"""
    
    def __init__(self, config_dir: str = "/opt/ftseek/FTSeek/config"):
        self.config_dir = Path(config_dir)
        self._configs: Dict[str, Dict] = {}
        self._file_hashes: Dict[str, str] = {}
        
    def load_config(self, filename: str) -> Dict:
        """加载单个配置文件"""
        filepath = self.config_dir / filename
        
        if not filepath.exists():
            logger.warning(f"配置文件不存在: {filepath}")
            return {}
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                if filename.endswith('.yaml') or filename.endswith('.yml'):
                    config = yaml.safe_load(f) or {}
                elif filename.endswith('.json'):
                    config = json.load(f)
                else:
                    logger.warning(f"不支持的配置格式: {filename}")
                    return {}
            
            self._configs[filename] = config
            self._file_hashes[filename] = self._calculate_hash(filepath)
            
            logger.debug(f"已加载配置: {filename}")
            return config
            
        except Exception as e:
            logger.error(f"加载配置失败 {filename}: {e}")
            return {}
    
    def load_all_configs(self):
        """加载所有核心配置文件 (2026-04-26: unified_config.yaml 为 SSOT)"""
        core_configs = [
            'unified_config.yaml',      # SSOT (Single Source of Truth) v4.0
            'stock_pool.yaml',          # 股票池 (独立管理)
        ]
        
        for config_file in core_configs:
            config_path = self.config_dir / config_file
            if not config_path.exists():
                logger.debug(f"配置文件 {config_file} 不存在，已跳过")
                continue
            self.load_config(config_file)
        
        logger.info(f"已加载 {len(self._configs)} 个配置文件")
    
    def validate_all(self) -> ValidationResult:
        """执行全部校验"""
        result = ValidationResult()
        
        # 1. 加载配置
        self.load_all_configs()
        
        # 2. 执行各项校验
        self._validate_risk_consistency(result)
        self._validate_exit_params(result)
        self._validate_position_limits(result)
        self._validate_strategy_params(result)
        self._validate_deprecated_configs(result)
        
        # 3. 校验 watchlist 和订阅槽位 (2026-04-13 新增)
        self._validate_watchlist(result)
        self._validate_subscription_slots(result)

        # 4. 参数一致性校验 (2026-04-15 新增)
        self._validate_parameter_consistency(result)

        # 5. 输出结果
        if result.is_valid:
            logger.info(f"✅ {result.summary()}")
        else:
            logger.error(f"❌ {result.summary()}")
            for error in result.errors:
                logger.error(f"  - [{error.category}] {error.message}")

        for warning in result.warnings:
            logger.warning(f"  ⚠️ [{warning.category}] {warning.message}")

        return result
    
    def _calculate_hash(self, filepath: Path) -> str:
        """计算文件哈希（用于检测配置变更）"""
        with open(filepath, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    
    def has_config_changed(self, filename: str) -> bool:
        """检查配置文件是否发生变化"""
        filepath = self.config_dir / filename
        
        if not filepath.exists():
            return False
        
        current_hash = self._calculate_hash(filepath)
        old_hash = self._file_hashes.get(filename)
        
        return current_hash != old_hash
    
    # ----------------------------------------------------------
    #  校验规则
    # ----------------------------------------------------------
    
    def _validate_risk_consistency(self, result: ValidationResult):
        """校验风控配置一致性 (2026-04-26: unified_config.yaml 为 SSOT)"""
        # SSOT: unified_config.yaml
        unified = self._configs.get('unified_config.yaml', {})
        
        # 从 unified_config.yaml 读取 risk_limits
        risk_limits = unified.get('risk_limits', {})
        min_confidence = risk_limits.get('min_confidence', 0.30)
        
        if min_confidence < 0.3 or min_confidence > 0.8:
            result.add_warning(
                category='range',
                message=f"min_confidence={min_confidence} 超出推荐范围 [0.3, 0.8]",
                field='risk_limits.min_confidence',
                suggestion='建议设置为 0.45-0.60'
            )
        
        # 检查仓位限制
        max_single = risk_limits.get('max_single_position', 0)
        max_total = risk_limits.get('max_total_position', 0)
        
        if max_single > max_total:
            result.add_error(
                category='conflict',
                message=f"单标的仓位({max_single}) > 总仓位({max_total})",
                field='risk_limits.max_single_position',
                suggestion='单标的仓位应小于总仓位'
            )
        
        if max_single > 0.30:
            result.add_warning(
                category='range',
                message=f"单标的仓位{max_single:.0%}过高，建议≤30%",
                field='risk_limits.max_single_position'
            )
    
    def _validate_exit_params(self, result: ValidationResult):
        """校验退出参数合理性"""
        exit_config = self._configs.get('dynamic_exit_params.yaml', {})
        
        for regime, params in exit_config.items():
            if not isinstance(params, dict):
                continue
            
            stop_loss = abs(params.get('stop_loss', 0))
            take_profit = abs(params.get('take_profit', 0))
            
            # 止盈应大于止损（正期望值）
            if take_profit < stop_loss:
                result.add_warning(
                    category='conflict',
                    message=f"{regime}: 止盈({take_profit:.1%}) < 止损({stop_loss:.1%})，盈亏比<1",
                    file='dynamic_exit_params.yaml',
                    field=f'{regime}.take_profit',
                    suggestion='建议止盈至少为止损的1.2倍以上'
                )
            
            # 止损范围检查
            if stop_loss < 0.01 or stop_loss > 0.10:
                result.add_warning(
                    category='range',
                    message=f"{regime}: 止损{stop_loss:.1%}超出合理范围[1%, 10%]",
                    field=f'{regime}.stop_loss'
                )
            
            # ATR倍数检查
            atr_config = params.get('atr_stop', {})
            atr_mult = atr_config.get('multiplier', 0)
            if atr_mult > 0 and (atr_mult < 1.0 or atr_mult > 5.0):
                result.add_warning(
                    category='range',
                    message=f"{regime}: ATR倍数{atr_mult}超出推荐范围[1.0, 5.0]",
                    field=f'{regime}.atr_stop.multiplier'
                )
    
    def _validate_position_limits(self, result: ValidationResult):
        """校验仓位策略"""
        position_config = self._configs.get('position_strategy.yaml', {})
        
        if not position_config:
            return
        
        # 检查各策略仓位分配
        strategies = position_config.get('strategies', {})
        total_weight = sum(s.get('weight', 0) for s in strategies.values())
        
        if total_weight > 0 and abs(total_weight - 1.0) > 0.01:
            result.add_warning(
                category='conflict',
                message=f"策略权重总和={total_weight:.2f} ≠ 1.0",
                file='position_strategy.yaml',
                field='strategies',
                suggestion='策略权重总和应为1.0'
            )
    
    def _validate_strategy_params(self, result: ValidationResult):
        """校验策略参数"""
        strategy_params = self._configs.get('strategy_params.json', {})
        
        if not strategy_params:
            return
        
        # 检查RSI参数
        rsi_params = strategy_params.get('rsi', {})
        rsi_period = rsi_params.get('period', 14)
        
        if rsi_period < 5 or rsi_period > 30:
            result.add_warning(
                category='range',
                message=f"RSI周期={rsi_period}超出推荐范围[5, 30]",
                file='strategy_params.json',
                field='rsi.period'
            )
    
    def _validate_deprecated_configs(self, result: ValidationResult):
        """检查废弃配置文件"""
        deprecated_files = [
            'risk_config.yaml',
            'strategy_params.json',
            'dynamic_exit_params.yaml',
            'position_strategy.yaml',
            'app_config.yaml',
            'system_config.yaml',
        ]

        for filename in deprecated_files:
            filepath = self.config_dir / filename
            if filepath.exists():
                result.add_info(
                    category='deprecated',
                    message=f"发现备份文件: {filename}，建议清理",
                    file=filename,
                    suggestion='移至 config/deprecated/ 目录'
                )

    def _validate_watchlist(self, result: ValidationResult):
        """校验 watchlist 配置 (2026-04-26: unified_config.yaml 为 SSOT)"""
        config = self._configs.get('unified_config.yaml', {})
        watchlist_hk = config.get('trading', {}).get('watch_list_hk', [])

        if not isinstance(watchlist_hk, list):
            result.add_error(
                category='missing',
                message="trading.watch_list_hk 不是有效的列表",
                file='config.yaml',
                field='trading.watch_list_hk',
                suggestion='应为股票代码列表，如 ["HK.00700", "HK.09988"]'
            )
            return

        if len(watchlist_hk) == 0:
            result.add_warning(
                category='range',
                message="watch_list_hk 为空列表，将使用最小 fallback (3只核心股)",
                field='trading.watch_list_hk'
            )
            return

        # 校验股票代码格式
        valid_hk = 0
        valid_us = 0
        invalid = []
        for code in watchlist_hk:
            if code.startswith("HK."):
                valid_hk += 1
            elif code.startswith("US."):
                valid_us += 1
            else:
                invalid.append(code)

        if invalid:
            result.add_warning(
                category='range',
                message=f"发现 {len(invalid)} 个格式不正确的股票代码: {invalid[:5]}",
                field='trading.watch_list_hk',
                suggestion='港股以 HK. 开头，美股以 US. 开头'
            )

        total = len(watchlist_hk)
        result.add_info(
            category='info',
            message=f"watchlist: {total} 只 (港股={valid_hk}, 美股={valid_us})",
            field='trading.watch_list_hk'
        )

        if total > 60:
            result.add_warning(
                category='range',
                message=f"watchlist 共 {total} 只，超过 60 只可能影响策略执行效率",
                field='trading.watch_list_hk',
                suggestion='建议 ≤ 60 只 (模拟期可扩展至 50+), 如需更多请使用分批轮询'
            )

    def _validate_parameter_consistency(self, result: ValidationResult):
        """
        参数一致性校验 (2026-04-26: unified_config.yaml 为 SSOT)

        核心原则：unified_config.yaml 是唯一权威数据源 (2026-04-26)
        1. 检测 YAML 内部重复定义冲突
        2. 检测 YAML 与代码硬编码默认值冲突
        3. 检测参数值合理性
        """
        config = self._configs.get('unified_config.yaml', {})
        if not config:
            return

        def get_nested(d, key, default=None):
            keys = key.split('.')
            current = d
            for k in keys:
                if isinstance(current, dict):
                    current = current.get(k, default)
                else:
                    return default
            return current

        def values_conflict(a, b, tolerance=0.001):
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return abs(a - b) > tolerance
            return a != b

        # ── 规则定义：(名称, yaml_key, 代码默认值, 代码路径, yaml内部别名) ──
        rules = [
            ("止损百分比", "stop_loss.default_percent", 0.03, "app/risk/stop_loss.py", {}),
            ("止盈百分比", "take_profit.default_percent", 0.10, "app/risk/dynamic_exit_manager.py", {}),
            ("最大开仓数", "trading.max_open_positions", 8, "app/core/config_container.py", {}),  # 2026-05-19: 对齐 YAML (原 20)
            ("单股最大仓位", "risk_limits.max_single_position", 0.08, "app/risk/risk_manager.py",
             {"trading.max_single_position": "trading 部分重复定义"}),
            ("总仓位上限", "risk_limits.max_total_position", 0.75, "app/risk/risk_manager.py",
             {"trading.max_total_position": "trading 部分重复定义"}),
            ("最大日交易次数", "risk_limits.max_daily_trades", 5, "app/risk/risk_manager.py",
             {"trading.max_daily_trades": "trading 部分重复定义"}),
            ("最大日亏损", "risk_limits.max_daily_loss", 0.03, "app/risk/risk_manager.py",
             {"trading.max_daily_loss": "trading 部分重复定义"}),
            ("最大周亏损", "risk_limits.max_weekly_loss", 0.08, "app/risk/risk_manager.py",
             {"trading.max_weekly_loss": "trading 部分重复定义"}),
            ("最大回撤", "risk_limits.max_drawdown", 0.12, "app/risk/risk_manager.py", {}),
            ("月度最大亏损", "risk_limits.max_monthly_loss", 0.15, "app/factors/position_sizing.py", {}),
            ("连续亏损限制", "risk_limits.consecutive_loss_limit", 3, "app/risk/risk_manager.py", {}),
            ("策略最低胜率", "risk_limits.strategy_min_winrate", 0.45, "app/risk/risk_manager.py", {}),
            ("最大单笔亏损", "risk_limits.max_single_loss", 0.05, "app/risk/risk_manager.py", {}),
            ("最小置信度", "risk_limits.min_confidence", 0.45, "app/risk/risk_manager.py", {}),  # 2026-05-19: 对齐 YAML (原 0.35)
            ("共识窗口", "strategy.consensus.window_seconds", 90, "app/core/config_container.py", {}),
            ("共识阈值", "strategy.consensus.threshold", 1.0, "app/core/config_container.py", {}),  # 2026-05-22: 1.5→1.0, 对齐 config_container.py + YAML
            ("决策缓冲冷却", "strategy.decision_buffer.signal_cooldown", 120, "app/decision_buffer.py", {}),
            ("决策缓冲队列", "strategy.decision_buffer.max_pending", 500, "app/decision_buffer.py", {}),
            ("信号冷却", "signal_cooldown.default_seconds", 120, "app/core/config_container.py", {}),
            # 2026-04-16 新增：跨模块默认值校验
            ("信号输出置信度", "risk_limits.min_confidence", 0.45, "app/signal_engine/fusion_engine.py", {}),  # 2026-05-19: 对齐 YAML (原 0.35)
            ("动态阈值默认", "risk_limits.min_confidence", 0.45, "app/risk/dynamic_threshold.py", {}),  # 2026-05-19: 对齐 YAML (原 0.35)
            ("止损计算器默认", "stop_loss.default_percent", 0.03, "app/risk/stop_loss.py", {}),
            ("体制参数加载器-单股仓位", "risk_limits.max_single_position", 0.08, "app/risk/regime_params_loader.py", {}),
            ("体制参数加载器-总仓位", "risk_limits.max_total_position", 0.75, "app/risk/regime_params_loader.py", {}),
            ("体制参数加载器-日交易次数", "risk_limits.max_daily_trades", 5, "app/risk/regime_params_loader.py", {}),
            ("体制参数加载器 - 置信度", "risk_limits.min_confidence", 0.45, "app/risk/regime_params_loader.py", {}),  # 2026-05-19: 对齐 YAML
            ("跨市场调度 - 最大持仓", "trading.max_open_positions", 8, "app/strategy/cross_market_scheduler.py", {}),  # 2026-05-19: 对齐 YAML
            ("跨市场调度-日交易次数", "risk_limits.max_daily_trades", 5, "app/strategy/cross_market_scheduler.py", {}),
            ("热重载-单股仓位", "risk_limits.max_single_position", 0.08, "app/core/config_hot_reload.py", {}),
            ("热重载-总仓位", "risk_limits.max_total_position", 0.75, "app/core/config_hot_reload.py", {}),
            ("热重载-日亏损", "risk_limits.max_daily_loss", 0.03, "app/core/config_hot_reload.py", {}),
            ("热重载-最大回撤", "risk_limits.max_drawdown", 0.12, "app/core/config_hot_reload.py", {}),
            ("热重载-置信度", "risk_limits.min_confidence", 0.45, "app/core/config_hot_reload.py", {}),
            ("ConsensusConfig-止损", "stop_loss.default_percent", 0.03, "app/core/config_container.py", {}),
            ("ConsensusConfig-止盈", "take_profit.default_percent", 0.10, "app/core/config_container.py", {}),
            ("AI权重", "strategy.ai.weight", 0.15, "app/ai/config.py", {}),
            ("AI日预算", "ai.daily_budget", 50.0, "app/ai/config.py", {}),
            ("AI月预算", "ai.monthly_budget", 1500.0, "app/ai/config.py", {}),
        ]

        for name, yaml_key, code_default, code_path, aliases in rules:
            yaml_val = get_nested(config, yaml_key)

            # 1. 检查 YAML 内部重复定义
            for alias_key, alias_desc in aliases.items():
                alias_val = get_nested(config, alias_key)
                if alias_val is not None and yaml_val is not None:
                    if values_conflict(yaml_val, alias_val):
                        result.add_error(
                            category='conflict',
                            message=f"YAML 内部冲突 [{name}]: {yaml_key}={yaml_val} vs {alias_key}={alias_val} ({alias_desc})",
                            file='config.yaml',
                            field=alias_key,
                            suggestion=f"删除 {alias_key}，统一使用 {yaml_key}"
                        )

            # 2. 检查 YAML vs 代码硬编码
            if yaml_val is not None and code_default is not None:
                if values_conflict(yaml_val, code_default):
                    ratio = abs(yaml_val - code_default) / max(abs(code_default), 0.001)
                    if ratio > 0.5:
                        severity_func = result.add_error
                        sev_msg = "严重不一致 (差异>{:.0%})".format(ratio)
                    elif ratio > 0.1:
                        severity_func = result.add_warning
                        sev_msg = "不一致 (差异>{:.0%})".format(ratio)
                    else:
                        severity_func = result.add_info
                        sev_msg = "轻微差异 (差异{:.0%})".format(ratio)

                    # 生成具体的修复命令
                    fix_cmd = f"修改 {code_path} 中的默认值 {code_default} → {yaml_val}"
                    if 'min_confidence' in name.lower():
                        fix_cmd += f"\n同时检查 config_validator.py 中的对应规则"
                    elif 'max_pending' in name.lower():
                        fix_cmd += f"\n同时更新 config_validator.py 中的规则默认值"
                    
                    severity_func(
                        category='conflict',
                        message=f"参数不一致 [{name}]: config.yaml={yaml_val} vs {code_path} 默认值={code_default} ({sev_msg})",
                        file='config.yaml',
                        field=yaml_key,
                        suggestion=f"{fix_cmd}\n运行: python3.8 scripts/config_audit_sentinel.py 验证修复"
                    )

            # 3. 参数合理性检查
            if yaml_val is not None:
                reasonableness = [
                    ("止损>5%", "stop_loss.default_percent", lambda v: abs(v) <= 0.05, "止损超过5%过大"),
                    ("止盈<5%", "take_profit.default_percent", lambda v: v >= 0.05, "止盈低于5%过小"),
                    ("单股仓位>30%", "risk_limits.max_single_position", lambda v: v <= 0.30, "单股仓位超过30%过于集中"),
                    ("日亏损>5%", "risk_limits.max_daily_loss", lambda v: v <= 0.05, "日亏损超过5%过大"),
                    ("总回撤>20%", "risk_limits.max_drawdown", lambda v: v <= 0.20, "总回撤超过20%过大"),
                ]
                for rn, rk, check_fn, suggestion in reasonableness:
                    if yaml_key == rk and not check_fn(yaml_val):
                        result.add_warning(
                            category='range',
                            message=f"参数不合理 [{rn}]: {yaml_key}={yaml_val}",
                            file='config.yaml',
                            field=yaml_key,
                            suggestion=suggestion
                        )

        # 总结
        error_count = len(result.errors)
        warn_count = len(result.warnings)
        if error_count == 0 and warn_count == 0:
            result.add_info(
                category='info',
                message="✅ 参数一致性校验通过：所有参数与 config.yaml 一致",
                field='parameter_consistency'
            )

    def _validate_subscription_slots(self, result: ValidationResult):
        """校验订阅槽位兼容性 (2026-04-26: unified_config.yaml 为 SSOT)"""
        config = self._configs.get('unified_config.yaml', {})
        watchlist = config.get('trading', {}).get('watch_list_hk', [])
        subscribe_str = os.getenv('SUBSCRIBE_TYPES', 'QUOTE,K_1M,K_5M')

        # 解析订阅类型
        if subscribe_str.startswith('['):
            try:
                sub_types = json.loads(subscribe_str)
            except json.JSONDecodeError:
                sub_types = [s.strip() for s in subscribe_str.strip('[]').split(',')]
        else:
            sub_types = [s.strip() for s in subscribe_str.split(',')]

        stock_count = len(watchlist) if isinstance(watchlist, list) else 0
        sub_count = len(sub_types)
        total_slots = stock_count * sub_count

        if stock_count > 0:
            result.add_info(
                category='info',
                message=f"订阅槽位: {stock_count} 只 × {sub_count} 种 = {total_slots}/1000",
                field='SUBSCRIBE_TYPES'
            )

            if total_slots > 1000:
                result.add_error(
                    category='conflict',
                    message=f"订阅槽位 {total_slots} 超过富途限制 (1000)! "
                            f"{stock_count} 只 × {sub_types} 种",
                    field='SUBSCRIBE_TYPES',
                    suggestion=f'减少 SUBSCRIBE_TYPES 至 {1000 // max(stock_count, 1)} 种'
                )
            elif total_slots > 950:
                result.add_warning(
                    category='range',
                    message=f"订阅槽位 {total_slots}/1000 已接近上限，余量不足",
                    field='SUBSCRIBE_TYPES',
                    suggestion='建议 ≤ 900 以保留 100 个安全余量'
                )
    
    def get_merged_config(self) -> Dict:
        """获取合并后的配置（unified_config优先）"""
        if not self._configs:
            self.load_all_configs()
        
        # 以unified_config为基础
        merged = self._configs.get('unified_config.yaml', {}).copy()
        
        # 合并其他配置（unified_config未定义的字段）
        for filename, config in self._configs.items():
            if filename == 'unified_config.yaml':
                continue
            
            for key, value in config.items():
                if key not in merged:
                    merged[key] = value
        
        return merged
    
    def reload_if_changed(self) -> bool:
        """检查并重载已变更的配置"""
        changed_files = []
        
        for filename in list(self._file_hashes.keys()):
            if self.has_config_changed(filename):
                self.load_config(filename)
                changed_files.append(filename)
                logger.info(f"配置已重载: {filename}")
        
        return len(changed_files) > 0


# ============================================================
#  全局单例
# ============================================================
_config_validator: Optional[ConfigValidator] = None
_validator_lock = None  # 简化版，不使用锁


def get_config_validator() -> ConfigValidator:
    """获取配置校验器单例"""
    global _config_validator
    
    if _config_validator is None:
        _config_validator = ConfigValidator()
    
    return _config_validator


def validate_configs(strict_mode: bool = True) -> ValidationResult:
    """
    便捷函数：执行配置校验
    
    Args:
        strict_mode: True=有错误时阻止启动 (抛异常), False=仅警告
    
    Returns:
        ValidationResult
    
    Raises:
        RuntimeError: strict_mode=True 且有错误时
    """
    validator = get_config_validator()
    result = validator.validate_all()
    
    if strict_mode and not result.is_valid:
        error_summary = "\n".join(
            f"  - {err.category}: {err.message}" for err in result.errors[:5]
        )
        raise RuntimeError(
            f"配置校验失败，阻止启动:\n{error_summary}\n"
            f"请修复配置不一致问题后重启服务。"
        )
    
    return result
