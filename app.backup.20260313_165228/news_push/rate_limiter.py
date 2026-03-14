#!/usr/bin/env python3
"""
推送频率控制器

功能:
- 每小时最多 N 条
- 每日最多 N 条
- 冷却时间控制
- 消息合并 (限流时摘要推送)
"""
import time
import json
import logging
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Tuple
from pathlib import Path
from dataclasses import dataclass, asdict

from app.news_push.config import RATE_LIMIT, PUSH_TIME_WINDOW, DATA_DIR

logger = logging.getLogger("ftseek.news_push.rate_limiter")


@dataclass
class PushRecord:
    """推送记录"""
    news_id: str
    title: str
    source: str
    pushed_at: float
    score: float


class RateLimiter:
    """
    推送频率控制器
    """
    
    def __init__(self, state_file: Path = None):
        self.state_file = state_file or (DATA_DIR / "news_push_state.json")
        self.state_file.parent.mkdir(exist_ok=True)
        
        # 限流配置
        self.max_per_hour = RATE_LIMIT['max_per_hour']
        self.max_per_day = RATE_LIMIT['max_per_day']
        self.cooldown_seconds = RATE_LIMIT['cooldown_seconds']
        self.merge_window = RATE_LIMIT['merge_window_seconds']
        
        # 时间窗口
        self.start_hour = PUSH_TIME_WINDOW['start_hour']
        self.end_hour = PUSH_TIME_WINDOW['end_hour']
        self.weekend_push = PUSH_TIME_WINDOW['weekend_push']
        
        # 滑动窗口记录
        self._hourly_pushes: deque = deque(maxlen=60)  # 最近 60 分钟
        self._daily_pushes: deque = deque(maxlen=24)   # 最近 24 小时
        self._last_push_time: float = 0
        self._merge_buffer: List[PushRecord] = []
        
        # 线程锁
        self._lock = threading.Lock()
        
        # 加载状态
        self._load_state()
        
        # 清理过期记录
        self._cleanup()
        
        logger.info(
            f"✅ 频率控制器已初始化 | "
            f"每小时≤{self.max_per_hour}条 | 每日≤{self.max_per_day}条 | "
            f"冷却{self.cooldown_seconds}秒"
        )
    
    def _load_state(self):
        """加载持久化状态"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                
                # 恢复小时记录
                hourly = state.get('hourly', [])
                now = time.time()
                for record in hourly:
                    if now - record.get('time', 0) < 3600:
                        self._hourly_pushes.append(record)
                
                # 恢复日记录
                daily = state.get('daily', [])
                for record in daily:
                    if now - record.get('time', 0) < 86400:
                        self._daily_pushes.append(record)
                
                # 恢复最后推送时间
                self._last_push_time = state.get('last_push_time', 0)
                
                logger.debug(f"已加载状态：小时={len(self._hourly_pushes)}, 日={len(self._daily_pushes)}")
                
            except Exception as e:
                logger.warning(f"加载状态失败：{e}")
    
    def _save_state(self):
        """保存持久化状态"""
        try:
            now = time.time()
            state = {
                'hourly': [
                    {'news_id': r.get('news_id'), 'time': r.get('time')}
                    for r in self._hourly_pushes
                    if now - r.get('time', 0) < 3600
                ],
                'daily': [
                    {'news_id': r.get('news_id'), 'time': r.get('time')}
                    for r in self._daily_pushes
                    if now - r.get('time', 0) < 86400
                ],
                'last_push_time': self._last_push_time,
                'updated_at': now
            }
            
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            logger.warning(f"保存状态失败：{e}")
    
    def _cleanup(self):
        """清理过期记录"""
        now = time.time()
        
        with self._lock:
            # 清理小时记录
            while self._hourly_pushes and now - self._hourly_pushes[0].get('time', 0) > 3600:
                self._hourly_pushes.popleft()
            
            # 清理日记录
            while self._daily_pushes and now - self._daily_pushes[0].get('time', 0) > 86400:
                self._daily_pushes.popleft()
        
        self._save_state()
    
    def _is_within_time_window(self) -> bool:
        """检查是否在推送时间窗口内"""
        now = datetime.now()
        current_hour = now.hour
        
        # 检查小时窗口
        if current_hour < self.start_hour or current_hour >= self.end_hour:
            return False
        
        # 检查周末
        if not self.weekend_push and now.weekday() >= 5:
            return False
        
        return True
    
    def _is_cooled_down(self) -> Tuple[bool, float]:
        """
        检查冷却时间
        
        Returns:
            (是否冷却完成，还需等待秒数)
        """
        now = time.time()
        elapsed = now - self._last_push_time
        
        if elapsed < self.cooldown_seconds:
            remaining = self.cooldown_seconds - elapsed
            return False, remaining
        
        return True, 0
    
    def _check_hourly_limit(self) -> Tuple[bool, int]:
        """
        检查小时限流
        
        Returns:
            (是否未超限，剩余可推送数)
        """
        now = time.time()
        count = sum(1 for r in self._hourly_pushes if now - r.get('time', 0) < 3600)
        
        remaining = self.max_per_hour - count
        return remaining > 0, max(0, remaining)
    
    def _check_daily_limit(self) -> Tuple[bool, int]:
        """
        检查日限流
        
        Returns:
            (是否未超限，剩余可推送数)
        """
        now = time.time()
        count = sum(1 for r in self._daily_pushes if now - r.get('time', 0) < 86400)
        
        remaining = self.max_per_day - count
        return remaining > 0, max(0, remaining)
    
    def can_push(self, news_id: str) -> Tuple[bool, str]:
        """
        检查是否可以推送
        
        Args:
            news_id: 新闻 ID
            
        Returns:
            (是否可以推送，原因)
        """
        now = time.time()
        
        # 1. 检查时间窗口
        if not self._is_within_time_window():
            return False, f"不在推送时间窗口 ({self.start_hour}:00-{self.end_hour}:00)"
        
        # 2. 检查冷却时间
        cooled, remaining = self._is_cooled_down()
        if not cooled:
            return False, f"冷却中 (还需{remaining:.0f}秒)"
        
        # 3. 检查小时限流
        hourly_ok, hourly_remaining = self._check_hourly_limit()
        if not hourly_ok:
            return False, f"小时限流 (已推送{self.max_per_hour}条)"
        
        # 4. 检查日限流
        daily_ok, daily_remaining = self._check_daily_limit()
        if not daily_ok:
            return False, f"日限流 (已推送{self.max_per_day}条)"
        
        # 5. 检查重复推送
        with self._lock:
            for record in list(self._hourly_pushes) + list(self._daily_pushes):
                if record.get('news_id') == news_id:
                    return False, "重复新闻"
        
        return True, f"可推送 (小时剩余{hourly_remaining}, 日剩余{daily_remaining})"
    
    def record_push(self, news_id: str, title: str, source: str, score: float):
        """
        记录推送
        
        Args:
            news_id: 新闻 ID
            title: 标题
            source: 来源
            score: 评分
        """
        now = time.time()
        record = {
            'news_id': news_id,
            'title': title,
            'source': source,
            'time': now,
            'score': score
        }
        
        with self._lock:
            # 添加到小时记录
            self._hourly_pushes.append(record)
            
            # 添加到日记录
            self._daily_pushes.append(record)
            
            # 更新最后推送时间
            self._last_push_time = now
        
        self._save_state()
        
        logger.info(
            f"📊 推送记录：{title[:50]}... | "
            f"小时={len(self._hourly_pushes)}/{self.max_per_hour} | "
            f"日={len(self._daily_pushes)}/{self.max_per_day}"
        )
    
    def add_to_merge_buffer(self, news_id: str, title: str, source: str, score: float):
        """
        添加到合并缓冲区 (限流时)
        
        Args:
            news_id: 新闻 ID
            title: 标题
            source: 来源
            score: 评分
        """
        record = PushRecord(
            news_id=news_id,
            title=title,
            source=source,
            pushed_at=time.time(),
            score=score
        )
        
        with self._lock:
            self._merge_buffer.append(record)
            
            # 清理过期缓冲
            now = time.time()
            self._merge_buffer = [
                r for r in self._merge_buffer
                if now - r.pushed_at < self.merge_window
            ]
        
        logger.debug(f"加入合并缓冲：{title[:30]}... (缓冲={len(self._merge_buffer)}条)")
    
    def get_merge_summary(self) -> List[PushRecord]:
        """
        获取合并摘要
        
        Returns:
            缓冲区中的新闻列表
        """
        with self._lock:
            # 按分数排序
            sorted_buffer = sorted(self._merge_buffer, key=lambda x: x.score, reverse=True)
            
            # 清空缓冲区
            self._merge_buffer = []
            
            return sorted_buffer
    
    def get_stats(self) -> Dict:
        """
        获取统计信息
        
        Returns:
            统计字典
        """
        now = time.time()
        
        with self._lock:
            hourly_count = sum(1 for r in self._hourly_pushes if now - r.get('time', 0) < 3600)
            daily_count = sum(1 for r in self._daily_pushes if now - r.get('time', 0) < 86400)
            
            cooled, remaining = self._is_cooled_down()
            
            return {
                'hourly_count': hourly_count,
                'hourly_limit': self.max_per_hour,
                'hourly_remaining': self.max_per_hour - hourly_count,
                'daily_count': daily_count,
                'daily_limit': self.max_per_day,
                'daily_remaining': self.max_per_day - daily_count,
                'cooled_down': cooled,
                'cooldown_remaining': remaining,
                'merge_buffer_size': len(self._merge_buffer),
                'last_push_at': datetime.fromtimestamp(self._last_push_time).isoformat() if self._last_push_time > 0 else None
            }
    
    def reset(self):
        """重置状态 (用于测试)"""
        with self._lock:
            self._hourly_pushes.clear()
            self._daily_pushes.clear()
            self._last_push_time = 0
            self._merge_buffer = []
        
        self._save_state()
        logger.info("频率控制器已重置")


# 全局单例
_limiter: Optional[RateLimiter] = None


def get_rate_limiter(state_file: Path = None) -> RateLimiter:
    """获取频率控制器单例"""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(state_file)
    return _limiter


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    limiter = get_rate_limiter()
    
    # 测试
    print("\n📊 当前状态:")
    stats = limiter.get_stats()
    for key, value in stats.items():
        print(f"   {key}: {value}")
    
    # 测试限流
    can_push, reason = limiter.can_push("test_news_1")
    print(f"\n📋 推送测试：{reason}")
    
    if can_push:
        limiter.record_push("test_news_1", "测试新闻 1", "test", 0.8)
        print("✅ 推送记录成功")
    
    print("\n📊 更新后状态:")
    stats = limiter.get_stats()
    for key, value in stats.items():
        print(f"   {key}: {value}")
