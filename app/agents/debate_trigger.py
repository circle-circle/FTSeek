"""
多 Agent 辩论触发器 (DebateTrigger)

盘前自动触发多Agent辩论, 将辩论结果注入作战计划生成。
支持:
  - 盘前定时触发 (通过 PreMarketAnalyst 集成)
  - 关键字手动触发 (钉钉消息/API)
  - 重大事件自动触发 (波动率跳升/突发新闻)
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional, Dict, Callable

logger = logging.getLogger("ftseek.debate_trigger")

DEBATE_KEYWORDS = ["辩论", "debate", "分析市场", "怎么看", "多agent", "multi-agent"]


class DebateTrigger:
    """辩论触发器 — 管理辩论触发条件和频率"""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_debate_time: Optional[datetime] = None
        self._cooldown = 300  # 5分钟冷却
        self._daily_count = 0
        self._daily_limit = 10
        self._last_reset_date: Optional[str] = None
        self._on_debate_callback: Optional[Callable] = None
        logger.info("DebateTrigger 已初始化")

    def set_callback(self, callback: Callable):
        """设置辩论回调 (接收 DebateSynthesis)"""
        self._on_debate_callback = callback

    def check_and_debate(self, message: str, context: Optional[Dict] = None) -> bool:
        """检测关键字并触发辩论"""
        if not any(kw.lower() in message.lower() for kw in DEBATE_KEYWORDS):
            return False
        logger.info(f"检测到辩论关键字，触发多 Agent 辩论")
        return self.trigger_debate(f"关键字触发：{message[:50]}", context)

    def trigger_debate(self, topic: str, context: Optional[Dict] = None) -> bool:
        """触发辩论 (带冷却和频率控制)"""
        with self._lock:
            now = datetime.now()

            # 重置日计数器
            today = now.strftime("%Y-%m-%d")
            if self._last_reset_date != today:
                self._daily_count = 0
                self._last_reset_date = today

            # 冷却检查
            if self._last_debate_time:
                elapsed = (now - self._last_debate_time).total_seconds()
                if elapsed < self._cooldown:
                    logger.debug(f"[辩论] 冷却中 ({elapsed:.0f}s < {self._cooldown}s)")
                    return False

            # 频率限制
            if self._daily_count >= self._daily_limit:
                logger.warning(f"[辩论] 已达日上限 ({self._daily_limit})")
                return False

            self._last_debate_time = now
            self._daily_count += 1

        # 异步执行 (不阻塞调用方)
        thread = threading.Thread(
            target=self._execute_debate,
            args=(topic, context or {}),
            daemon=True,
        )
        thread.start()
        return True

    def _execute_debate(self, topic: str, context: Dict):
        """执行辩论 (在线程中运行 async)"""
        logger.info(f"[辩论] 执行中: {topic}")
        try:
            from app.agents.debate.debate_orchestrator import run_debate_sync

            synthesis = run_debate_sync(
                event_packs=context.get("event_packs"),
                market_context=context.get("market_context", ""),
                feedback_context=context.get("feedback_context", ""),
                price_data=context.get("price_data", ""),
                llm_config=context.get("llm_config"),
            )

            logger.info(
                f"[辩论] 完成: 共识={synthesis.consensus_direction} "
                f"conviction={synthesis.conviction:.2f} "
                f"quality={synthesis.debate_quality_score:.2f}"
            )

            # 回调通知
            if self._on_debate_callback:
                try:
                    self._on_debate_callback(synthesis)
                except Exception as e:
                    logger.error(f"[辩论] 回调失败: {e}")

        except Exception as e:
            logger.error(f"[辩论] 执行失败: {e}", exc_info=True)

    def get_status(self) -> Dict:
        return {
            "last_debate": self._last_debate_time.isoformat() if self._last_debate_time else None,
            "daily_count": self._daily_count,
            "daily_limit": self._daily_limit,
            "cooldown_sec": self._cooldown,
        }


# 模块级单例
_debate_trigger: Optional[DebateTrigger] = None


def get_debate_trigger() -> DebateTrigger:
    global _debate_trigger
    if _debate_trigger is None:
        _debate_trigger = DebateTrigger()
    return _debate_trigger
