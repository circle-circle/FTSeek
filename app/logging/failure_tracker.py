#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
失败追踪器 (Failure Tracker)

功能:
1. 分类统计失败原因
2. 每小时/每日输出统计
3. 异常占比告警
4. 钉钉推送集成
"""

import logging
import json
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, asdict

logger = logging.getLogger("ftseek.failure_tracker")
logger.propagate = False  # 防止日志重复

# 配置文件
FAILURE_STATS_DIR = Path('/opt/ftseek/FTSeek/data/failure_stats')
FAILURE_STATS_DIR.mkdir(parents=True, exist_ok=True)

ALERT_THRESHOLDS = {
    'cooldown_skip_ratio': 0.50,     # 冷却跳过占比 >50% 告警
    'confidence_reject_ratio': 0.40, # 置信度拒绝占比 >40% 告警
    'order_limit_ratio': 0.10,       # 订单超限占比 >10% 告警
    'circuit_breaker_triggers': 3,   # 每日熔断触发>3 次告警
}


@dataclass
class FailureRecord:
    """失败记录"""
    timestamp: str
    code: str
    reason: str  # cooldown, confidence, limit, risk, other
    category: str
    message: str = ""


class FailureTracker:
    """失败追踪器"""
    
    def __init__(self):
        self.records: List[FailureRecord] = []
        self.hourly_stats: Dict[str, Dict] = defaultdict(lambda: defaultdict(int))
        self.daily_stats: Dict[str, Dict] = defaultdict(lambda: defaultdict(int))
        self._load_today_records()
        logger.info("失败追踪器已初始化")
    
    def _load_today_records(self):
        """加载今日记录"""
        today = datetime.now().strftime('%Y-%m-%d')
        records_file = self._get_today_file()
        
        if records_file.exists():
            try:
                with open(records_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.records = [FailureRecord(**r) for r in data]
                    logger.info(f"加载今日失败记录：{len(self.records)} 条")
            except Exception as e:
                logger.error(f"加载记录失败：{e}")
    
    def _get_today_file(self) -> Path:
        """获取今日记录文件路径"""
        today = datetime.now().strftime('%Y-%m-%d')
        return FAILURE_STATS_DIR / f'failures_{today}.json'
    
    def record_failure(self, code: str, reason: str, message: str = ""):
        """
        记录失败
        
        Args:
            code: 股票代码
            reason: 失败原因 (cooldown, confidence, limit, risk, other)
            message: 详细消息
        """
        record = FailureRecord(
            timestamp=datetime.now().isoformat(),
            code=code,
            reason=reason,
            category=self._categorize_reason(reason),
            message=message,
        )
        
        self.records.append(record)
        
        # 更新统计
        hour_key = datetime.now().strftime('%Y-%m-%d %H:00')
        day_key = datetime.now().strftime('%Y-%m-%d')
        
        self.hourly_stats[hour_key][reason] += 1
        self.daily_stats[day_key][reason] += 1
        
        # 保存记录
        self._save_today_records()
        
        # 检查是否触发告警
        self._check_alerts(day_key)
        
        logger.debug(f"[{code}] 失败记录：{reason} - {message[:50]}")
    
    def _categorize_reason(self, reason: str) -> str:
        """分类失败原因"""
        reason_lower = reason.lower()
        
        if 'cool' in reason_lower or 'cooldown' in reason_lower:
            return 'cooldown_skip'
        elif 'confidence' in reason_lower:
            return 'confidence_reject'
        elif 'limit' in reason_lower or 'amount' in reason_lower:
            return 'order_limit'
        elif 'risk' in reason_lower:
            return 'risk_reject'
        else:
            return 'other'
    
    def _save_today_records(self):
        """保存今日记录"""
        records_file = self._get_today_file()
        
        try:
            today_records = [asdict(r) for r in self.records]
            
            temp_file = records_file.with_suffix('.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(today_records, f, indent=2, ensure_ascii=False)
            
            temp_file.rename(records_file)
            
        except Exception as e:
            logger.error(f"保存记录失败：{e}")
    
    def _check_alerts(self, day_key: str):
        """检查是否触发告警"""
        today_stats = self.daily_stats.get(day_key, {})
        total = sum(today_stats.values())
        
        if total == 0:
            return
        
        # 检查各类型占比
        alerts = []
        
        # 冷却跳过占比
        cooldown_count = today_stats.get('cooldown_skip', 0)
        cooldown_ratio = cooldown_count / total
        if cooldown_ratio > ALERT_THRESHOLDS['cooldown_skip_ratio']:
            alerts.append(f"冷却跳过占比过高：{cooldown_ratio*100:.1f}%")
        
        # 置信度拒绝占比
        confidence_count = today_stats.get('confidence_reject', 0)
        confidence_ratio = confidence_count / total
        if confidence_ratio > ALERT_THRESHOLDS['confidence_reject_ratio']:
            alerts.append(f"置信度拒绝占比过高：{confidence_ratio*100:.1f}%")
        
        # 订单超限占比
        limit_count = today_stats.get('order_limit', 0)
        limit_ratio = limit_count / total
        if limit_ratio > ALERT_THRESHOLDS['order_limit_ratio']:
            alerts.append(f"订单超限占比过高：{limit_ratio*100:.1f}%")
        
        # 发送告警
        if alerts:
            self._send_alert(alerts, today_stats, total)
    
    def _send_alert(self, alerts: List[str], stats: Dict, total: int):
        """发送告警"""
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            notifier = DingTalkNotifier()
            
            alert_text = "\n".join(f"⚠️ {alert}" for alert in alerts)
            
            message = f"""⚠️ **FTSeek 失败统计告警**

{alert_text}

📊 今日统计 (总计{total}次):
  冷却跳过：{stats.get('cooldown_skip', 0)}次
  置信度拒绝：{stats.get('confidence_reject', 0)}次
  订单超限：{stats.get('order_limit', 0)}次
  风控拒绝：{stats.get('risk_reject', 0)}次
  其他：{stats.get('other', 0)}次

🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

💡 建议:
1. 检查配置参数是否合理
2. 分析失败原因分布
3. 优化策略或调整阈值"""
            
            # notifier.send_text(message)
            logger.warning(f"失败统计告警：{', '.join(alerts)}")
            
        except Exception as e:
            logger.error(f"发送告警失败：{e}")
    
    def get_statistics(self, period: str = 'today') -> Dict:
        """
        获取统计信息
        
        Args:
            period: 'today' 或 'hour'
        
        Returns:
            dict: 统计信息
        """
        if period == 'hour':
            hour_key = datetime.now().strftime('%Y-%m-%d %H:00')
            stats = dict(self.hourly_stats.get(hour_key, {}))
        else:
            day_key = datetime.now().strftime('%Y-%m-%d')
            stats = dict(self.daily_stats.get(day_key, {}))
        
        total = sum(stats.values())
        
        # 计算占比
        stats_with_ratio = {}
        for reason, count in stats.items():
            ratio = count / total if total > 0 else 0
            stats_with_ratio[reason] = {
                'count': count,
                'ratio': round(ratio, 4),
                'ratio_pct': round(ratio * 100, 1),
            }
        
        return {
            'period': period,
            'total': total,
            'by_reason': stats_with_ratio,
        }
    
    def print_statistics(self):
        """打印统计信息"""
        today_stats = self.get_statistics('today')
        hour_stats = self.get_statistics('hour')
        
        print("\n" + "=" * 80)
        print("📊 FTSeek 失败统计")
        print("=" * 80)
        
        print(f"\n今日统计 (总计{today_stats['total']}次):")
        print("-" * 80)
        for reason, data in today_stats['by_reason'].items():
            print(f"  {reason}: {data['count']}次 ({data['ratio_pct']:.1f}%)")
        
        print(f"\n本小时统计 (总计{hour_stats['total']}次):")
        print("-" * 80)
        for reason, data in hour_stats['by_reason'].items():
            print(f"  {reason}: {data['count']}次 ({data['ratio_pct']:.1f}%)")
        
        print("\n" + "=" * 80)
        print()


# 全局单例
failure_tracker = FailureTracker()
