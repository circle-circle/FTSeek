#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动态阈值调整模块

功能:
1. 根据信号质量分析结果动态调整置信度阈值
2. 每周评估一次
3. 设置调整幅度限制 (±0.05/周)
4. 保存调整历史
"""

import json
import logging
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("ftseek.dynamic_threshold")
logger.propagate = False  # 防止日志重复

# 配置文件
THRESHOLD_CONFIG_PATH = '/opt/ftseek/FTSeek/config/dynamic_threshold.json'
THRESHOLD_HISTORY_PATH = '/opt/ftseek/FTSeek/data/threshold_history.json'


class DynamicThresholdManager:
    """动态阈值管理器"""
    
    def __init__(self):
        self.current_threshold = 0.45  # 2026-05-19: 提高信号门槛 (原0.35, 与 min_confidence 对齐)
        self.last_adjustment = None
        self.adjustment_history: List[Dict] = []
        self._load_config()
        self._load_history()
    
    def _load_config(self):
        """加载配置"""
        try:
            if os.path.exists(THRESHOLD_CONFIG_PATH):
                with open(THRESHOLD_CONFIG_PATH, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.current_threshold = config.get('current_threshold', 0.55)  # 2026-04-16 对齐 unified_config.yaml
                    self.last_adjustment = config.get('last_adjustment')
                    logger.info(f"加载动态阈值配置：{self.current_threshold}")
        except Exception as e:
            logger.error(f"加载配置失败：{e}")
    
    def _load_history(self):
        """加载调整历史"""
        try:
            if os.path.exists(THRESHOLD_HISTORY_PATH):
                with open(THRESHOLD_HISTORY_PATH, 'r', encoding='utf-8') as f:
                    self.adjustment_history = json.load(f)
                    logger.info(f"加载调整历史：{len(self.adjustment_history)} 条")
        except Exception as e:
            logger.debug(f"加载历史失败：{e}")
    
    def _save_config(self):
        """保存配置"""
        try:
            config = {
                'current_threshold': self.current_threshold,
                'last_adjustment': self.last_adjustment,
                'updated_at': datetime.now().isoformat(),
            }
            
            Path(THRESHOLD_CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
            
            with open(THRESHOLD_CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            
            logger.info(f"配置已保存：{self.current_threshold}")
        except Exception as e:
            logger.error(f"保存配置失败：{e}")
    
    def _save_history(self):
        """保存历史"""
        try:
            Path(THRESHOLD_HISTORY_PATH).parent.mkdir(parents=True, exist_ok=True)
            
            with open(THRESHOLD_HISTORY_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.adjustment_history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存历史失败：{e}")
    
    def should_adjust(self) -> bool:
        """
        判断是否应该调整阈值
        
        Returns:
            bool: 是否应该调整
        """
        if not self.last_adjustment:
            return True  # 从未调整过，可以调整
        
        last_dt = datetime.fromisoformat(self.last_adjustment)
        now = datetime.now()
        
        # 至少间隔 7 天
        days_since_last = (now - last_dt).days
        return days_since_last >= 7
    
    def calculate_adjustment(self, 
                            confidence_stats: Dict) -> Tuple[float, str]:
        """
        计算阈值调整
        
        Args:
            confidence_stats: 置信度区间统计
        
        Returns:
            tuple: (新阈值，调整原因)
        """
        current = self.current_threshold
        adjustment = 0.0
        reason = ""
        
        # 找出最佳置信度区间
        best_bucket = None
        best_score = 0
        
        for bucket, stats in confidence_stats.items():
            if stats.get('count', 0) < 3:
                continue
            
            # 综合评分：胜率 * 0.6 + 盈亏比归一化 * 0.4
            win_rate = stats.get('win_rate', 0)
            profit_factor = stats.get('profit_factor', 1.0)
            if profit_factor == float('inf'):
                profit_factor = 3.0  # 上限
            
            score = win_rate * 0.6 + min(profit_factor, 3.0) / 3.0 * 0.4
            
            if score > best_score:
                best_score = score
                best_bucket = bucket
        
        if not best_bucket:
            return current, "数据不足，不调整"
        
        # 解析最佳区间
        bucket_min = float(best_bucket.split('-')[0])
        bucket_max = float(best_bucket.split('-')[1])
        
        # 当前阈值是否在最佳区间内
        if bucket_min <= current < bucket_max:
            reason = f"当前阈值 {current:.2f} 在最佳区间 {best_bucket} 内"
            return current, reason
        
        # 计算调整量
        target = bucket_min  # 调整到最佳区间的下限
        
        # 限制调整幅度 (±0.05/周)
        max_adjustment = 0.05
        if target - current > max_adjustment:
            adjustment = max_adjustment
            reason = f"向上调整 (最大幅度 {max_adjustment})"
        elif target - current < -max_adjustment:
            adjustment = -max_adjustment
            reason = f"向下调整 (最大幅度 {max_adjustment})"
        else:
            adjustment = target - current
            reason = f"调整至最佳区间 {best_bucket}"
        
        new_threshold = round(current + adjustment, 2)
        
        # 确保在合理范围内
        new_threshold = max(0.40, min(0.80, new_threshold))
        
        return new_threshold, reason
    
    def adjust_threshold(self, confidence_stats: Dict) -> Dict:
        """
        执行阈值调整
        
        Args:
            confidence_stats: 置信度区间统计
        
        Returns:
            dict: 调整结果
        """
        if not self.should_adjust():
            days_since = (datetime.now() - datetime.fromisoformat(self.last_adjustment)).days if self.last_adjustment else 999
            return {
                'adjusted': False,
                'reason': f"距离上次调整仅{days_since}天，需间隔 7 天",
                'current_threshold': self.current_threshold,
            }
        
        old_threshold = self.current_threshold
        new_threshold, reason = self.calculate_adjustment(confidence_stats)
        
        if new_threshold == old_threshold:
            return {
                'adjusted': False,
                'reason': reason,
                'current_threshold': old_threshold,
            }
        
        # 执行调整
        self.current_threshold = new_threshold
        self.last_adjustment = datetime.now().isoformat()
        
        # 记录历史
        adjustment_record = {
            'timestamp': self.last_adjustment,
            'old_threshold': old_threshold,
            'new_threshold': new_threshold,
            'change': round(new_threshold - old_threshold, 2),
            'reason': reason,
        }
        self.adjustment_history.append(adjustment_record)
        
        # 保存
        self._save_config()
        self._save_history()
        
        logger.info(f"阈值已调整：{old_threshold:.2f} → {new_threshold:.2f} | {reason}")
        
        return {
            'adjusted': True,
            'old_threshold': old_threshold,
            'new_threshold': new_threshold,
            'change': round(new_threshold - old_threshold, 2),
            'reason': reason,
        }
    
    def get_current_threshold(self) -> float:
        """获取当前阈值"""
        return self.current_threshold
    
    def get_adjustment_history(self, limit: int = 10) -> List[Dict]:
        """获取调整历史"""
        return self.adjustment_history[-limit:]
    
    def print_status(self):
        """打印状态"""
        print("\n" + "=" * 80)
        print("🎯 动态阈值状态")
        print("=" * 80)
        print(f"当前阈值：{self.current_threshold:.2f}")
        print(f"最后调整：{self.last_adjustment or '从未调整'}")
        
        if self.adjustment_history:
            print(f"\n调整历史 (最近{len(self.adjustment_history)}次):")
            print("-" * 80)
            for record in self.adjustment_history[-5:]:
                print(f"  {record['timestamp'][:10]}: "
                      f"{record['old_threshold']:.2f} → {record['new_threshold']:.2f} "
                      f"({record['change']:+.2f}) - {record['reason']}")
        
        print("\n" + "=" * 80)
        print()


# 全局单例
threshold_manager = DynamicThresholdManager()
