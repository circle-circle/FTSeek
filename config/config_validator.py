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
logger.propagate = False


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
        """加载所有核心配置文件"""
        core_configs = [
            'unified_config.yaml',
            'risk_config.yaml',
            'dynamic_exit_params.yaml',
            'position_strategy.yaml',
            'strategy_params.json',
        ]
        
        for config_file in core_configs:
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
        
        # 3. 输出结果
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
        """校验风控配置一致性"""
        unified = self._configs.get('unified_config.yaml', {})
        risk = self._configs.get('risk_config.yaml', {})
        
        # 检查止盈止损合理性
        risk_limits = unified.get('risk_limits', {})
        min_confidence = risk_limits.get('min_confidence', 0)
        
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


def validate_configs() -> ValidationResult:
    """便捷函数：执行配置校验"""
    validator = get_config_validator()
    return validator.validate_all()
