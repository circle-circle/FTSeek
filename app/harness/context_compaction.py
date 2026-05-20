"""
Context Compaction Module — 辩论框架的「内存管理」

功能:
1. Token 计数与预警 (估算 LLM 上下文长度)
2. 消息压缩 (保留关键信息，丢弃中间冗余)
3. 历史总结保存 (压缩后的摘要持久化)

借鉴自 Dexter 的 context compaction 模式，适配 FTSeek 辩论框架。

使用方式:
    from app.harness.context_compaction import ContextCompactor

    compactor = ContextCompactor(max_tokens=8000)
    compacted = compactor.compact(messages, keep_last_n=3)
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("harness.context_compaction")


# ============================================================
#  Token 估算 (简化版，无需额外依赖)
# ============================================================

def estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数量

    规则 (近似):
    - 英文: ~1 token per 4 chars
    - 中文: ~1 token per 1.5 chars
    - JSON/代码: ~1 token per 3 chars

    误差约 ±15%，足够用于阈值判断。
    """
    if not text:
        return 0

    # 简单启发式: 中文密集文本 vs 英文/代码
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars

    # 中文 ~1.5 chars/token, 英文 ~4 chars/token
    tokens = (chinese_chars / 1.5) + (other_chars / 4.0)
    return int(tokens)


def estimate_messages_tokens(messages: List[Dict]) -> int:
    """估算消息列表的总 token 数"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    total += estimate_tokens(item.get("text", ""))
                else:
                    total += estimate_tokens(str(item))
    return total


# ============================================================
#  数据类
# ============================================================

@dataclass
class CompactionResult:
    """压缩结果"""
    original_tokens: int
    compacted_tokens: int
    reduction_pct: float
    messages_kept: int
    messages_dropped: int
    summary: str = ""
    compacted_messages: List[Dict] = field(default_factory=list)


# ============================================================
#  Context Compactor
# ============================================================

class ContextCompactor:
    """
    上下文压缩器

    策略:
    1. 保留最近 N 轮完整对话
    2. 将更早的对话压缩为摘要
    3. 保留所有 system prompt (角色定义不可丢弃)
    4. 工具调用结果可选择性保留 (KEEP_TOOL_USES)
    """

    def __init__(
        self,
        max_tokens: int = 8000,
        keep_last_n: int = 3,
        keep_system_prompts: bool = True,
    ):
        """
        Args:
            max_tokens: 触发压缩的阈值 (超过此值开始压缩)
            keep_last_n: 保留最近 N 轮完整对话
            keep_system_prompts: 是否始终保留 system prompt
        """
        self.max_tokens = max_tokens
        self.keep_last_n = keep_last_n
        self.keep_system_prompts = keep_system_prompts

        logger.info(
            f"[ContextCompactor] 初始化: "
            f"max_tokens={max_tokens}, keep_last_n={keep_last_n}"
        )

    def should_compact(self, messages: List[Dict]) -> bool:
        """判断是否需要压缩"""
        tokens = estimate_messages_tokens(messages)
        return tokens > self.max_tokens

    def compact(
        self,
        messages: List[Dict],
        keep_last_n: Optional[int] = None,
    ) -> CompactionResult:
        """
        执行上下文压缩

        Args:
            messages: 原始消息列表
            keep_last_n: 覆盖默认的保留轮数

        Returns:
            压缩结果
        """
        if keep_last_n is not None:
            keep_last_n = keep_last_n
        else:
            keep_last_n = self.keep_last_n

        original_tokens = estimate_messages_tokens(messages)

        # 1. 分离 system prompts (始终保留)
        system_msgs = []
        non_system = []
        for msg in messages:
            if msg.get("role") == "system" and self.keep_system_prompts:
                system_msgs.append(msg)
            else:
                non_system.append(msg)

        # 2. 保留最近 N 轮 (1 轮 = user + assistant)
        total_rounds = len(non_system) // 2
        rounds_to_keep = min(keep_last_n, total_rounds)
        start_idx = max(0, len(non_system) - rounds_to_keep * 2)

        recent_msgs = non_system[start_idx:]
        dropped_msgs = non_system[:start_idx]

        # 3. 将丢弃的消息压缩为摘要
        summary = self._generate_summary(dropped_msgs)

        # 4. 构建压缩后的消息列表
        compacted = list(system_msgs)

        if summary:
            compacted.append({
                "role": "system",
                "content": f"[历史对话摘要]\n{summary}",
            })

        compacted.extend(recent_msgs)

        compacted_tokens = estimate_messages_tokens(compacted)
        reduction = (
            (original_tokens - compacted_tokens) / max(original_tokens, 1) * 100
        )

        result = CompactionResult(
            original_tokens=original_tokens,
            compacted_tokens=compacted_tokens,
            reduction_pct=round(reduction, 1),
            messages_kept=len(compacted),
            messages_dropped=len(dropped_msgs),
            summary=summary,
            compacted_messages=compacted,
        )

        logger.info(
            f"[ContextCompactor] 压缩完成: "
            f"{original_tokens} → {compacted_tokens} tokens "
            f"(减少 {reduction:.1f}%), "
            f"保留 {len(compacted)} 条, 丢弃 {len(dropped_msgs)} 条"
        )

        return result

    def _generate_summary(self, messages: List[Dict]) -> str:
        """
        将丢弃的消息压缩为文本摘要

        策略:
        1. 提取用户关键请求 (去除工具结果等冗长内容)
        2. 提取助手核心结论 (保留 decision/recommendation)
        3. 合并为紧凑摘要
        """
        if not messages:
            return ""

        parts = []
        for i, msg in enumerate(messages):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if isinstance(content, str) and len(content) > 500:
                # 长内容截断，保留前 200 和后 100 字符
                content = content[:200] + "\n... [已压缩] ...\n" + content[-100:]

            if role == "user":
                parts.append(f"[用户] {content[:300]}")
            elif role == "assistant":
                # 尝试提取关键结论
                key_points = self._extract_key_points(content)
                if key_points:
                    parts.append(f"[助手] {key_points}")
                else:
                    parts.append(f"[助手] {content[:300]}")

        return "\n".join(parts)

    def _extract_key_points(self, content: str) -> str:
        """从助手回复中提取关键结论"""
        points = []

        # 尝试查找 JSON 中的关键字段
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                for key in [
                    "summary", "conclusion", "recommendation",
                    "action", "decision", "market_sentiment",
                    "risk_level", "neutral_score",
                ]:
                    if key in data:
                        val = data[key]
                        if isinstance(val, (str, int, float)):
                            points.append(f"{key}: {val}")
                        elif isinstance(val, list):
                            points.append(f"{key}: {len(val)} 项")
        except (json.JSONDecodeError, TypeError):
            pass

        # 如果没有结构化数据，返回简短预览
        if not points:
            lines = content.split("\n")
            # 取前 3 行非空内容
            for line in lines:
                line = line.strip()
                if line and not line.startswith("#"):
                    points.append(line)
                    if len(points) >= 3:
                        break

        return " | ".join(points) if points else ""


# ============================================================
#  辩论专用压缩器 (集成到 PremarkDebateAgent)
# ============================================================

class DebateContextCompactor(ContextCompactor):
    """
    辩论框架专用压缩器

    额外功能:
    1. 保留辩论角色定义 (bull/bear/auditor system prompts)
    2. 保留候选股票池 (压缩但不清除)
    3. 保留市场关键指标
    """

    def __init__(
        self,
        max_tokens: int = 6000,  # 辩论框架 token 更敏感，阈值更低
        keep_last_n: int = 2,     # 辩论保留更少轮次
    ):
        super().__init__(
            max_tokens=max_tokens,
            keep_last_n=keep_last_n,
            keep_system_prompts=True,
        )

    def compact_debate_context(
        self,
        candidate_pool: List[Dict],
        market_context: Dict,
        debate_history: List[Dict],
    ) -> Tuple[str, CompactionResult]:
        """
        压缩辩论上下文

        Args:
            candidate_pool: 候选股票池
            market_context: 市场环境
            debate_history: 历史辩论记录

        Returns:
            (压缩后的 prompt, 压缩结果)
        """
        # 构建完整消息
        messages = []

        # System: 角色定义
        messages.append({
            "role": "system",
            "content": "FTSeek 港股盘前辩论框架 - 多模型协作分析",
        })

        # User: 候选池 + 市场环境
        context_text = f"候选池: {len(candidate_pool)} 只股票\n"
        context_text += f"市场环境: {json.dumps(market_context, ensure_ascii=False, indent=2)[:500]}"
        messages.append({"role": "user", "content": context_text})

        # 历史辩论记录
        messages.extend(debate_history)

        # 执行压缩
        result = self.compact(messages)

        # 构建压缩后的 prompt
        compacted_prompt = self._build_compacted_prompt(
            candidate_pool, market_context, result
        )

        return compacted_prompt, result

    def _build_compacted_prompt(
        self,
        candidate_pool: List[Dict],
        market_context: Dict,
        compaction_result: CompactionResult,
    ) -> str:
        """构建压缩后的辩论 prompt"""
        parts = []

        # 1. 摘要 (如果有)
        if compaction_result.summary:
            parts.append("## 历史辩论摘要")
            parts.append(compaction_result.summary)
            parts.append("")

        # 2. 候选池 (始终保留，但限制数量)
        pool_preview = candidate_pool[:10]
        parts.append(f"## 候选股票池 ({len(candidate_pool)} 只，显示前 {len(pool_preview)} 只)")
        parts.append(json.dumps(pool_preview, ensure_ascii=False, indent=2))
        parts.append("")

        # 3. 市场环境 (精简)
        parts.append("## 市场环境")
        parts.append(json.dumps(market_context, ensure_ascii=False, indent=2)[:1000])
        parts.append("")

        # 4. 压缩统计
        parts.append(
            f"## 上下文压缩统计\n"
            f"- 原始 token: {compaction_result.original_tokens}\n"
            f"- 压缩后 token: {compaction_result.compacted_tokens}\n"
            f"- 减少: {compaction_result.reduction_pct}%"
        )

        return "\n".join(parts)


# ============================================================
#  便捷函数
# ============================================================

def compact_if_needed(
    messages: List[Dict],
    max_tokens: int = 8000,
    keep_last_n: int = 3,
) -> Tuple[List[Dict], bool]:
    """
    按需压缩消息

    Args:
        messages: 原始消息
        max_tokens: 触发阈值
        keep_last_n: 保留轮数

    Returns:
        (压缩后的消息, 是否执行了压缩)
    """
    compactor = ContextCompactor(max_tokens=max_tokens, keep_last_n=keep_last_n)

    if not compactor.should_compact(messages):
        return messages, False

    result = compactor.compact(messages)
    return result.compacted_messages, True
