#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 信号门禁 (LLM Signal Impact Gate)

基于 LLM 盘前分析的 signal_impact 标签进行门禁检查。

根因:
- LLM 在盘前分析中对标的给出 signal_impact (boost/neutral/weaken/block)
- block: 硬拦截 (实盘) / 告警放行 (模拟盘)
- weaken: 要求更高阈值
- boost: 增强信号

配置:
    通过 daily_active_pool 动态池或 signal.extra 传递

行为:
- block: 实盘硬拦截，模拟盘 shadow 放行
- weaken: 记录标签
- boost: 记录标签

版本历史:
  2026-05-19: 从 executor.py 提取
"""

from typing import Tuple

from app.trader.gates.base_gate import GateInterface
from app.strategy.base import Signal


class LLMSignalImpactGate(GateInterface):
    """
    LLM 信号门禁 — 基于 LLM 盘前分析的 signal_impact 标签

    依赖 context:
        - config: 配置
        - logger: 日志记录器
        - failure_tracker: 失败追踪器
        - is_sim: 是否模拟盘
        - daily_active_pool: 动态活跃池 (可选)
    """

    def name(self) -> str:
        return "LLMSignalImpactGate"

    def is_enabled(self, context: dict) -> bool:
        return True  # 始终启用

    def check(self, signal: Signal, context: dict) -> Tuple[bool, str]:
        """
        检查 LLM signal_impact 标签

        Returns:
            (True, ""): 通过
            (False, "原因"): 拦截
        """
        config = context.get('config')
        logger = context.get('logger')
        failure_tracker = context.get('failure_tracker')
        is_sim = context.get('is_sim', True)
        daily_active_pool = context.get('daily_active_pool')

        # 获取 signal_impact
        _impact = None
        _urgency = "Medium"

        # 路径 1: 动态池 metadata
        if daily_active_pool is not None:
            try:
                # 前缀归一化查找 (兼容 "00700" vs "HK.00700")
                _entry = daily_active_pool._entries.get(signal.code)
                if not _entry and not signal.code.startswith("HK."):
                    _entry = daily_active_pool._entries.get(f"HK.{signal.code}")

                if _entry and not _entry.is_expired():
                    _impact = _entry.metadata.get("signal_impact", "neutral")
                    _urgency = _entry.metadata.get("urgency", "Medium")
            except Exception:
                pass

        # 路径 2: signal.extra (battle_plan 等绕过动态池的路径)
        if _impact is None or _impact == "neutral":
            _extra_impact = (signal.extra or {}).get("signal_impact")
            if _extra_impact:
                _impact = _extra_impact
                _urgency = (signal.extra or {}).get("urgency", "Medium")

        # 处理 signal_impact
        if _impact and _impact != "neutral":
            signal.extra = signal.extra or {}
            signal.extra["llm_signal_impact"] = _impact
            signal.extra["llm_urgency"] = _urgency

            if _impact == "block":
                if is_sim:
                    # 模拟盘：告警但放行 (数据收集)
                    msg = f"LLM 建议回避 (block)，模拟盘放行+打标签 (urgency={_urgency})"
                    if logger:
                        logger.warning(f"[LLM 门禁·影子] {signal.code} {msg}")
                    signal.extra['llm_block_override'] = True
                    return True, ""  # 告警但放行
                else:
                    # 实盘：硬拦截
                    _reason = f"LLM 信号 block: 政策/风险因素"[:100]
                    msg = f"LLM 信号 block: {_reason}"
                    if logger:
                        logger.warning(f"[LLM 门禁·硬拦截] {signal.code} {msg}")
                    if failure_tracker:
                        failure_tracker.record_failure(signal.code, 'llm_block', _reason)
                    return False, msg

            elif _impact == "weaken":
                if logger:
                    logger.info(f"[LLM 门禁·降权] {signal.code} signal_impact=weaken, 要求更高阈值")
                signal.extra['llm_weaken_applied'] = True

            elif _impact == "boost":
                if logger:
                    logger.info(f"[LLM 门禁·增强] {signal.code} signal_impact=boost (urgency={_urgency})")
                signal.extra['llm_boost_applied'] = True

        # 通过：无 block 标签
        return True, ""
