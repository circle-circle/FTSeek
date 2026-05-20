"""定时任务调度器 - 管理所有定时任务"""
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Any, Optional, Callable
from pathlib import Path

logger = logging.getLogger("ftseek_us.scheduled.scheduler")

class TaskScheduler:
    """定时任务调度器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.last_run_times: Dict[str, datetime] = {}
    
    def add_task(self, name: str, func: Callable, interval_minutes: int, **kwargs):
        """
        添加定时任务
        
        Args:
            name: 任务名称
            func: 任务函数
            interval_minutes: 执行间隔 (分钟)
            **kwargs: 任务参数
        """
        self.tasks[name] = {
            "func": func,
            "interval": interval_minutes * 60,  # 转换为秒
            "kwargs": kwargs,
            "last_run": None,
            "next_run": datetime.now(),
        }
        logger.info(f"添加定时任务：{name} (间隔：{interval_minutes}分钟)")
    
    def start(self):
        """启动调度器"""
        if self.running:
            logger.warning("调度器已在运行中")
            return
        
        self.running = True
        self._thread = threading.Thread(target=self._run, name="TaskScheduler", daemon=True)
        self._thread.start()
        logger.info("✅ 定时任务调度器已启动")
    
    def stop(self):
        """停止调度器"""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("✅ 定时任务调度器已停止")
    
    def _run(self):
        """调度器主循环"""
        logger.info("调度器主循环启动")
        
        while self.running:
            try:
                now = datetime.now()
                
                for name, task in self.tasks.items():
                    if now >= task["next_run"]:
                        # 执行任务
                        self._execute_task(name, task)
                        
                        # 计算下次执行时间
                        task["next_run"] = now + timedelta(seconds=task["interval"])
                        task["last_run"] = now
                
                # 每秒检查一次
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"调度器循环错误：{e}", exc_info=True)
                time.sleep(5)
    
    def _execute_task(self, name: str, task: Dict[str, Any]):
        """执行任务"""
        try:
            logger.info(f"执行定时任务：{name}")
            task["func"](**task["kwargs"])
        except Exception as e:
            logger.error(f"执行任务 {name} 失败：{e}", exc_info=True)
    
    def get_task_status(self) -> Dict[str, Dict[str, Any]]:
        """获取任务状态"""
        status = {}
        now = datetime.now()
        
        for name, task in self.tasks.items():
            status[name] = {
                "interval_minutes": task["interval"] / 60,
                "last_run": task["last_run"].isoformat() if task["last_run"] else None,
                "next_run": task["next_run"].isoformat(),
                "time_until_next": (task["next_run"] - now).total_seconds(),
            }
        
        return status

# 导入 timedelta
from datetime import timedelta
