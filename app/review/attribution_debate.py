"""
归因辩论门禁 (AttributionDebate)

在归因引擎和教训存储之间插入质量验证，防止垃圾数据注入下次决策。

Karpathy 原则:
- 1 个文件，不超过 200 行
- 零改动现有逻辑（仅在 save 前加一行调用）
- 优雅降级（验证失败不影响流程）

架构:
  归因引擎 run() → [AttributionDebate.validate()] → _save_lessons()
                         ↓
                  Stage 1: 数据完整性检查（规则，无 LLM）
                  Stage 2: 可疑归因辩论（LLM 单调用）
                  Stage 3: 收敛输出（validated/suspicious/invalidated）
"""

import json
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek.review.attribution_debate")

# ─── 硬编码阈值 ─────────────────────────────────────────────────────
# Karpathy: 不需要为 3 个数字创建 YAML 节点
PNL_SUSPICIOUS_THRESHOLD = 100.0   # PnL > 100% 视为可疑（正常交易不可能）
HOLD_TIME_SUSPICIOUS_SEC = 60      # 持仓 < 60s 视为可疑
DEBATE_TIMEOUT_SEC = 90            # 辩论总超时


class AttributionDebate:
    """归因质量辩论 — 三重验证门禁"""

    def __init__(self, llm_config: Optional[Dict] = None):
        self.llm_config = llm_config or {}

    def validate(self, raw_lessons: List[Dict], market_data: Optional[Dict] = None) -> List[Dict]:
        """执行归因验证，返回带 confidence 的 lessons

        Args:
            raw_lessons: 归因引擎输出的原始教训列表
            market_data: 可选的市场上下文（用于 LLM 辩论）

        Returns:
            验证后的教训列表（过滤掉 invalidated，保留 validated/suspicious）
        """
        if not raw_lessons:
            return []

        t0 = time.time()
        logger.info(f"[AttributionDebate] 开始验证 {len(raw_lessons)} 条归因")

        validated = []
        for lesson in raw_lessons:
            try:
                # Stage 1: 规则检查（无 LLM 成本）
                flags = self._data_quality_check(lesson)

                if flags.get("invalid"):
                    # 数据不完整，直接标记无效
                    lesson["confidence"] = 0.0
                    lesson["validation_status"] = "invalidated"
                    lesson["validation_reason"] = "; ".join(flags["reasons"])
                    continue

                if flags.get("suspicious") and (time.time() - t0 < DEBATE_TIMEOUT_SEC):
                    # Stage 2: LLM 辩论（仅对可疑且未超时的）
                    debate_result = self._run_debate(lesson)
                    lesson["confidence"] = debate_result.get("confidence", 0.3)
                    lesson["validation_status"] = debate_result.get("status", "suspicious")
                    lesson["validation_reason"] = debate_result.get("summary", "")
                elif flags.get("suspicious"):
                    # 超时降级
                    lesson["confidence"] = 0.3
                    lesson["validation_status"] = "suspicious"
                    lesson["validation_reason"] = "辩论超时，降级处理"
                else:
                    # 通过检查，提升置信度
                    lesson["confidence"] = max(lesson.get("confidence", 0.5), 0.7)
                    lesson["validation_status"] = "validated"
                    lesson["validation_reason"] = "数据完整，归因合理"

                validated.append(lesson)

            except Exception as e:
                logger.warning(f"[AttributionDebate] 单条验证失败: {e}")
                lesson["confidence"] = 0.5
                lesson["validation_status"] = "pending"
                lesson["validation_reason"] = f"验证异常: {e}"
                validated.append(lesson)

        # 过滤 confidence = 0 的无效教训
        final = [l for l in validated if l.get("confidence", 0) > 0]

        elapsed = time.time() - t0
        logger.info(
            f"[AttributionDebate] 完成: {len(raw_lessons)} → {len(final)} 条 "
            f"(过滤 {len(raw_lessons) - len(final)} 条), {elapsed:.1f}s"
        )
        return final

    # ════════════════════════════════════════════════════════════════
    #  Stage 1: 数据完整性检查
    # ════════════════════════════════════════════════════════════════

    def _data_quality_check(self, lesson: Dict) -> Dict:
        """返回 {invalid: bool, suspicious: bool, reasons: list}"""
        reasons = []
        invalid = False
        suspicious = False

        # 1. PnL 合理性检查
        pnl = abs(lesson.get("pnl_percent", 0))
        if pnl > PNL_SUSPICIOUS_THRESHOLD:
            reasons.append(f"PnL 异常: {pnl:.1f}% > {PNL_SUSPICIOUS_THRESHOLD}%")
            suspicious = True

        # 2. 持仓时间检查
        hold_time = self._calc_hold_time(lesson)
        if hold_time is not None and hold_time < HOLD_TIME_SUSPICIOUS_SEC:
            reasons.append(f"持仓过短: {hold_time}s < {HOLD_TIME_SUSPICIOUS_SEC}s")
            suspicious = True

        # 3. 根因分析质量检查
        cause = lesson.get("cause", "")
        topic = lesson.get("topic", "")
        lesson_text = lesson.get("lesson", "")
        # 如果 cause 为空或 "unknown"，且没有详细的 lesson 文本，标记 suspicious
        if not cause or cause == "unknown":
            if not lesson_text or len(lesson_text) < 20:
                reasons.append("根因为空/unknown 且无详细分析")
                suspicious = True
        # cause == topic 是现有系统的正常行为，不单独标记

        # 4. P0 教训必须有修复措施
        if lesson.get("severity") == "P0" and lesson.get("fix") == "待实施":
            reasons.append("P0 教训无修复措施")
            suspicious = True

        # 5. 缺失关键字段 → 无效
        if not lesson.get("symbol"):
            reasons.append("缺少标的代码")
            invalid = True

        return {"invalid": invalid, "suspicious": len(reasons) > 0, "reasons": reasons}

    @staticmethod
    def _calc_hold_time(lesson: Dict) -> Optional[int]:
        """计算持仓秒数（从 market_context 或 entry/exit_time）"""
        ctx = lesson.get("market_context", {})
        hold_min = ctx.get("holding_minutes")
        if hold_min is not None:
            return int(hold_min * 60)

        entry = lesson.get("entry_time")
        exit_t = lesson.get("exit_time")
        if entry and exit_t:
            try:
                fmt = "%Y-%m-%d %H:%M:%S"
                e = datetime.strptime(str(entry), fmt)
                x = datetime.strptime(str(exit_t), fmt)
                return int((x - e).total_seconds())
            except Exception:
                pass
        return None

    # ════════════════════════════════════════════════════════════════
    #  Stage 2: 归因辩论（LLM 单调用）
    # ════════════════════════════════════════════════════════════════

    def _run_debate(self, lesson: Dict) -> Dict:
        """对可疑归因触发轻量辩论（单 LLM 调用，非多 Agent）"""
        system = """你是 FTSeek 的**归因审核专家**。请对以下归因结论做三重判断:

1. 数据合理性: 交易数据是否可信？PnL/持仓时间是否异常？
2. 归因准确性: 给出的原因是否匹配实际交易行为？
3. 教训价值: 这条教训对下次决策有实际指导意义吗？

必须输出严格 JSON:
{
  "data_ok": true/false,
  "attribution_ok": true/false,
  "valuable": true/false,
  "confidence": 0.0-1.0,
  "status": "validated|suspicious|invalidated",
  "summary": "50 字以内中文总结"
}"""

        user = self._build_debate_prompt(lesson)

        try:
            from app.agents.base import BaseLLMService
            svc = BaseLLMService(
                config=self.llm_config,
                default_model="qwen3.6-flash",
                default_timeout=30,
            )
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            result = BaseLLMService.run_sync(
                svc.call_and_parse, messages, timeout=30
            )

            # 映射到验证结果
            if result.get("status") in ("validated", "suspicious", "invalidated"):
                return result
            elif result.get("data_ok") and result.get("attribution_ok"):
                return {"confidence": result.get("confidence", 0.7), "status": "validated",
                        "summary": result.get("summary", "LLM 验证通过")}
            else:
                return {"confidence": result.get("confidence", 0.3), "status": "suspicious",
                        "summary": result.get("summary", "LLM 认为归因存疑")}

        except Exception as e:
            logger.warning(f"[AttributionDebate] LLM 辩论失败，降级为规则判断: {e}")
            return {"confidence": 0.3, "status": "suspicious",
                    "summary": f"LLM 辩论失败，降级处理: {e}"}

    @staticmethod
    def _build_debate_prompt(lesson: Dict) -> str:
        """构造辩论 prompt"""
        ctx = lesson.get("market_context", {})
        return f"""审核以下归因结论:

标的: {lesson.get('symbol', '?')}
日期: {lesson.get('trade_date', '?')}
方向: {lesson.get('direction', '?')}
盈亏: {lesson.get('pnl_percent', 0):.1f}%
持仓: {ctx.get('holding_minutes', '?')} 分钟
归因原因: {lesson.get('cause', '?')}
归因详情: {lesson.get('lesson', '?')}
信号质量: {ctx.get('signal_quality', '?')}
执行质量: {ctx.get('execution_quality', '?')}
风控质量: {ctx.get('risk_quality', '?')}"""


# ════════════════════════════════════════════════════════════════
#  单例
# ════════════════════════════════════════════════════════════════
_debate_instance: Optional[AttributionDebate] = None


def get_attribution_debate(llm_config: Optional[Dict] = None) -> AttributionDebate:
    """获取归因辩论单例"""
    global _debate_instance
    if _debate_instance is None:
        _debate_instance = AttributionDebate(llm_config)
    return _debate_instance
