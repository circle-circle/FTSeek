#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场状态动态参数加载器

功能:
1. 根据市场状态加载对应参数集
2. 支持运行时动态更新参数
3. 提供参数查询接口
"""

import logging
import yaml
import json
import os
from typing import Dict, Optional
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("ftseek.regime_params")
logger.propagate = False  # 防止日志重复

# 配置文件路径
PARAMS_CONFIG_PATH = '/opt/ftseek/FTSeek/config/market_regime_params.yaml'
RUNTIME_PARAMS_PATH = '/opt/ftseek/FTSeek/config/runtime_params.json'


class RegimeParamsLoader:
    """市场状态参数加载器"""
    
    def __init__(self, config_path: str = None):
        """
        初始化
        
        Args:
            config_path: 配置文件路径
        """
        self.config_path = config_path or PARAMS_CONFIG_PATH
        self.config = {}
        self.runtime_params = {}
        self._load_config()
        self._load_runtime_params()
    
    def _load_config(self):
        """加载配置文件"""
        try:
            if not os.path.exists(self.config_path):
                logger.warning(f"配置文件不存在：{self.config_path}")
                return
            
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)
            
            logger.info(f"市场状态参数配置已加载：{len(self.config)} 个状态")
            
        except Exception as e:
            logger.error(f"加载配置失败：{e}")
            self.config = {}
    
    def _load_runtime_params(self):
        """加载运行时参数"""
        try:
            if os.path.exists(RUNTIME_PARAMS_PATH):
                with open(RUNTIME_PARAMS_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.runtime_params = data.get('runtime_params', {})
                    logger.info(f"运行时参数已加载：{self.runtime_params.get('description', 'N/A')}")
        except Exception as e:
            logger.debug(f"加载运行时参数失败：{e}")
            self.runtime_params = {}
    
    def get_params(self, regime_type: str) -> Dict:
        """
        根据市场状态获取参数
        
        Args:
            regime_type: 市场状态类型 (如 'strong_trend_up', 'range_bound')
        
        Returns:
            dict: 参数集
        """
        # 优先使用运行时参数 (如果已设置)
        if self.runtime_params:
            logger.info(f"使用运行时参数：{self.runtime_params.get('description', 'N/A')}")
            return self.runtime_params
        
        # 否则使用配置文件中的状态参数
        params = self.config.get(regime_type, self.config.get('default', {}))
        
        # 返回标准化参数
        return {
            'signal_cooldown': params.get('signal_cooldown', 120),
            'min_confidence': params.get('min_confidence', 0.50),
            'max_single_position': params.get('max_single_position', 0.20),
            'max_total_position': params.get('max_total_position', 0.80),
            'max_daily_trades': params.get('max_daily_trades', 10),
            'description': params.get('description', f'状态：{regime_type}'),
            'regime_type': regime_type,
        }
    
    def update_runtime_params(self, params: Dict):
        """
        更新运行时参数
        
        Args:
            params: 新参数集
        """
        try:
            self.runtime_params = params
            self.runtime_params['updated_at'] = datetime.now().isoformat()
            
            # 保存到文件
            data = {
                'runtime_params': self.runtime_params,
                'updated_at': self.runtime_params['updated_at'],
            }
            
            # 确保目录存在
            Path(RUNTIME_PARAMS_PATH).parent.mkdir(parents=True, exist_ok=True)
            
            with open(RUNTIME_PARAMS_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"运行时参数已更新：{params.get('description', 'N/A')}")
            
        except Exception as e:
            logger.error(f"更新运行时参数失败：{e}")
    
    def clear_runtime_params(self):
        """清除运行时参数 (恢复配置默认)"""
        try:
            self.runtime_params = {}
            if os.path.exists(RUNTIME_PARAMS_PATH):
                os.remove(RUNTIME_PARAMS_PATH)
            logger.info("运行时参数已清除，使用配置默认值")
        except Exception as e:
            logger.error(f"清除运行时参数失败：{e}")
    
    def get_current_params(self) -> Dict:
        """获取当前生效的参数"""
        if self.runtime_params:
            return self.runtime_params
        
        # 返回默认参数
        default = self.config.get('default', {})
        return {
            'signal_cooldown': default.get('signal_cooldown', 120),
            'min_confidence': default.get('min_confidence', 0.50),
            'max_single_position': default.get('max_single_position', 0.20),
            'max_total_position': default.get('max_total_position', 0.80),
            'max_daily_trades': default.get('max_daily_trades', 10),
            'description': '默认参数',
        }
    
    def log_params(self, regime_type: str):
        """记录参数信息到日志"""
        params = self.get_params(regime_type)
        
        logger.info("=" * 60)
        logger.info(f"市场状态：{params.get('description', regime_type)}")
        logger.info("=" * 60)
        logger.info(f"  信号冷却：{params['signal_cooldown']}秒")
        logger.info(f"  置信度阈值：{params['min_confidence']}")
        logger.info(f"  单一仓位：{params['max_single_position']*100:.0f}%")
        logger.info(f"  总仓位：{params['max_total_position']*100:.0f}%")
        logger.info(f"  日交易上限：{params['max_daily_trades']}笔")
        logger.info("=" * 60)


# 全局单例
params_loader = RegimeParamsLoader()
