"""
Hermes Agent 处理器 — 持久化后台线程，真正调用 LLM + 工具。

从 hermes_queue.db 轮询任务，通过 tool-calling 循环执行分析，
结果写入 response 文件。支持 3 种任务类型:
  - battle_plan: 盘前作战计划生成
  - midday_adjust: 午间计划调整
  - eod_review: 盘后复盘归因

同时保留文件扫描模式，向后兼容 daily_review.py 的文件协议。
"""

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Any

from .hermes_queue import HermesQueue
from .hermes_tools import (
    ToolDependencies,
    get_openai_tools,
    execute_tool,
)

logger = logging.getLogger("ftseek.llm.agent_processor")


# ═══════════════════════════════════════════════════════════════════
#  System Prompts (按任务类型)
# ═══════════════════════════════════════════════════════════════════

BATTLE_PLAN_SYSTEM = """你是 FTSeek 的**盘前策略师 (Pre-Market Strategist)**，运行在 Hermes Agent 中。

核心目标: **稳定赚钱，控制风险**。

你有以下工具可用:
- query_database: 查询昨日交易、持仓、信号数据
- get_market_data: 获取实时行情快照
- get_or_update_memory: 读取历史教训和市场记忆
- send_notification: 推送重要通知
- get_config: 读取风控参数

【工作流程】
1. 先用 get_or_update_memory 读取历史教训 (避免重复犯错)
2. 用 query_database 获取昨日交易记录和当前持仓
3. 如需了解市场状态，用 get_market_data 查看关键指数和标的
4. 综合所有信息，生成今日作战计划

【输出格式】最终必须输出严格 JSON:
{
  "date": "YYYY-MM-DD",
  "market_regime": "RISK_ON / RISK_OFF / NEUTRAL",
  "overall_bias": "BUY / SELL / NEUTRAL",
  "targets": [
    {
      "symbol": "HK.00700",
      "direction": "LONG / SHORT",
      "entry_condition": "具体入场条件",
      "stop_loss_pct": -2.0,
      "take_profit_pct": 3.0,
      "confidence": 0.0-1.0,
      "rationale": "推荐理由 (结合记忆和新闻)"
    }
  ],
  "risk_warnings": ["风险提示"],
  "avoid_symbols": ["应避免的标的及原因"],
  "max_positions": 3,
  "key_question": "今日最需要关注的问题",
  "summary": "盘前分析总结 (中文，不超过150字)"
}

分析要基于实际数据，不要编造。不确定时保守为上。"""

MIDDAY_ADJUST_SYSTEM = """你是 FTSeek 的**午间分析师 (Midday Analyst)**，运行在 Hermes Agent 中。

核心任务: 基于上午交易结果和市场变化，调整下午作战计划。

你有以下工具可用:
- query_database: 查询上午交易、当前持仓、信号
- get_market_data: 获取午间行情快照 (A股收盘影响)
- get_or_update_memory: 读取历史教训
- send_notification: 推送异常提醒
- get_config: 读取风控参数

【工作流程】
1. 用 query_database 获取上午交易记录 (9:30-12:00)
2. 用 get_market_data 查看 A股上午收盘和港股大盘
3. 分析上午盈亏归因: 哪些标的超预期？哪些异常？
4. 调整下午计划: 增/删/改标的，调整仓位

【输出格式】最终必须输出严格 JSON:
{
  "date": "YYYY-MM-DD",
  "morning_summary": {
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "total_pnl": 0.0,
    "win_rate": 0.0
  },
  "anomaly_flags": ["上午异常信号"],
  "plan_adjustments": [
    {
      "symbol": "HK.00700",
      "action": "elevate / demote / add / remove",
      "reason": "调整原因"
    }
  ],
  "risk_adjustments": {
    "max_positions": 3,
    "reduce_exposure": false,
    "reason": "调整原因"
  },
  "afternoon_bias": "BUY / SELL / NEUTRAL",
  "key_watch": ["下午重点关注标的"],
  "summary": "午间分析总结 (中文，不超过150字)"
}

诚实分析，不确定时建议观望。"""

EOD_REVIEW_SYSTEM = """你是 FTSeek 的**盘后复盘师 (EOD Reviewer)**，运行在 Hermes Agent 中。

核心任务: 全面复盘今日交易，对比作战计划，提取可复用教训。

你有以下工具可用:
- query_database: 查询今日全量交易、持仓、信号、新闻
- get_market_data: 获取收盘行情
- get_or_update_memory: 读取历史记忆 + **写入新教训** (重要!)
- send_notification: 推送复盘摘要
- get_config: 读取风控参数

【工作流程】
1. 用 query_database 获取今日全部交易
2. 对比作战计划的执行率: 哪些计划内的做了？哪些没做？哪些是计划外的？
3. 分析每笔盈亏的根因: 方向对错、时机好坏、执行质量
4. **特别关注亏损交易**: 提取 1-2 条具体教训
5. 用 get_or_update_memory(action="append") 将教训写入记忆

【输出格式】最终必须输出严格 JSON:
{
  "date": "YYYY-MM-DD",
  "plan_execution": {
    "planned": 0,
    "executed": 0,
    "off_plan": 0,
    "execution_rate": 0.0
  },
  "trade_analysis": [
    {
      "symbol": "HK.00700",
      "action": "BUY / SELL",
      "pnl": 0.0,
      "pnl_pct": 0.0,
      "root_cause": "亏损/盈利的根因",
      "is_plan_target": true,
      "lesson": "可复用的教训 (30字内)"
    }
  ],
  "regime_shift_flags": ["市场状态变化信号"],
  "emergent_risks": ["新发现的风险"],
  "new_lessons": ["要写入记忆的教训 (每条不超过80字)"],
  "strategy_adjustments": ["策略参数调整建议"],
  "next_day_preview": {
    "bias": "BUY / SELL / NEUTRAL",
    "key_question": "明日核心问题",
    "overnight_watch": ["隔夜关注标的"]
  },
  "summary": "盘后复盘总结 (中文，不超过200字)",
  "weekly_score": 0-100
}

复盘结论要具体可操作。每个教训都应该能直接用于改进明天的交易。"""


# ═══════════════════════════════════════════════════════════════════
#  Processor
# ═══════════════════════════════════════════════════════════════════

class HermesAgentProcessor:
    """Hermes Agent 持久化处理器 — 真正调用 LLM + 工具"""

    def __init__(
        self,
        llm_config: Optional[Dict] = None,
        queue_path: Optional[str] = None,
        memory_dir: Optional[str] = None,
        deps: Optional[ToolDependencies] = None,
        polling_interval: float = 5.0,
        max_tool_rounds: int = 5,
        default_model: str = "qwen3.6-plus",
    ):
        self.llm_config = llm_config or {}
        self.queue = HermesQueue(
            db_path=queue_path,
            responses_dir=os.path.join(
                os.path.dirname(queue_path) if queue_path else
                os.path.expanduser("~/.hermes/ftseek"),
                "responses",
            ),
        )
        self.deps = deps or ToolDependencies(
            memory_dir=memory_dir or os.path.expanduser("~/.hermes/ftseek/memory"),
        )
        self.polling_interval = polling_interval
        self.max_tool_rounds = max_tool_rounds
        self.default_model = default_model

        self._stop_event = threading.Event()
        self._running = False

        # 请求文件目录 (文件协议向后兼容)
        self.requests_dir = os.path.expanduser("~/.hermes/ftseek/requests")
        self.responses_dir = self.queue.responses_dir
        os.makedirs(self.requests_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)

    # ─── Main Loop ─────────────────────────────────────────────────

    def run_forever(self):
        """持久化轮询主循环 (作为 daemon 线程的 target)"""
        self._running = True
        logger.info(
            "[HermesProcessor] 处理器启动: interval=%.1fs, tool_rounds=%d, model=%s",
            self.polling_interval, self.max_tool_rounds, self.default_model,
        )

        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error("[HermesProcessor] 轮询异常 (自动恢复): %s", e, exc_info=True)

            self._stop_event.wait(self.polling_interval)

        self._running = False
        logger.info("[HermesProcessor] 处理器已停止")

    def stop(self):
        """优雅停止"""
        logger.info("[HermesProcessor] 收到停止信号...")
        self._stop_event.set()

    def _tick(self):
        """单次轮询: DB 队列 + 文件扫描"""
        # 1. DB 队列优先
        pending = self.queue.poll_pending(limit=3)
        for task in pending:
            if self._stop_event.is_set():
                return
            self._process_db_task(task)

        # 2. 文件扫描 (向后兼容)
        self._scan_files()

    # ─── DB Task Processing ────────────────────────────────────────

    def _process_db_task(self, task: Dict):
        request_id = task["request_id"]
        request_type = task["request_type"]
        market = task.get("market", "hk")

        logger.info("[HermesProcessor] 处理 DB 任务: %s (%s, %s)", request_id, request_type, market)
        self.queue.mark_processing(request_id)

        try:
            payload = json.loads(task.get("payload", "{}"))
            # P0 修复: 使用新事件循环避免与主线程冲突
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(self.process_one(request_type, market, payload))
            finally:
                loop.close()
            self.queue.mark_completed(request_id, result)
            logger.info("[HermesProcessor] DB 任务完成: %s", request_id)
        except Exception as e:
            logger.error("[HermesProcessor] DB 任务失败: %s — %s", request_id, e)
            self.queue.mark_failed(request_id, str(e)[:500])

    # ─── File Scan (Backward Compat) ───────────────────────────────

    def _scan_files(self):
        """扫描 ~/.hermes/ftseek/requests/ 中的 JSON 文件"""
        if not os.path.isdir(self.requests_dir):
            return

        files = sorted([
            f for f in os.listdir(self.requests_dir)
            if f.endswith(".json")
        ])
        for filename in files[:3]:  # 每轮最多处理 3 个文件
            if self._stop_event.is_set():
                return
            filepath = os.path.join(self.requests_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    request = json.load(f)

                request_id = request.get("id", filename.replace(".json", ""))
                request_type = request.get("type", "unknown")
                market = request.get("market", "hk")

                logger.info("[HermesProcessor] 处理文件任务: %s (%s)", request_id, request_type)

                payload = request.get("context", {})
                # P0 修复: 使用新事件循环避免与主线程冲突
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    result = loop.run_until_complete(self.process_one(request_type, market, payload))
                finally:
                    loop.close()

                # 写入 response 文件
                response = {
                    "request_id": request_id,
                    "timestamp": datetime.now().isoformat(),
                    "status": "completed",
                    "result": result,
                }
                response_file = os.path.join(self.responses_dir, filename)
                with open(response_file, "w", encoding="utf-8") as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)

                # 清理 request 文件
                os.remove(filepath)
                logger.info("[HermesProcessor] 文件任务完成: %s", request_id)

            except Exception as e:
                logger.error("[HermesProcessor] 文件任务失败 %s: %s", filename, e)
                # 保留失败的文件用于调试

    # ═══════════════════════════════════════════════════════════════
    #  Core: Process One Task
    # ═══════════════════════════════════════════════════════════════

    async def process_one(
        self, request_type: str, market: str, payload: Dict
    ) -> Dict:
        """处理单个任务 — 完整 tool-calling 循环"""
        system_prompt = self._get_system_prompt(request_type)
        user_prompt = self._build_user_prompt(request_type, market, payload)

        tools = get_openai_tools()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        return await self._agent_loop(messages, tools, request_type)

    async def _agent_loop(
        self, messages: List[Dict], tools: List[Dict], request_type: str,
    ) -> Dict:
        """Tool-calling 循环: 调用 LLM → 执行工具 → 反馈结果 → 重复"""
        for round_num in range(1, self.max_tool_rounds + 1):
            logger.info("[HermesProcessor] Agent Loop round %d/%d", round_num, self.max_tool_rounds)

            response = await self._call_llm_with_tools(messages, tools)

            if response is None:
                logger.warning("[HermesProcessor] LLM 返回空，使用 fallback")
                return self._fallback_result(request_type)

            # 情况 1: LLM 返回 tool_calls → 执行工具并继续
            tool_calls = response.get("tool_calls", [])
            if tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": response.get("content"),
                    "tool_calls": tool_calls,
                })

                for tc in tool_calls:
                    tool_name = tc.get("function", {}).get("name", "")
                    try:
                        tool_args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                    except json.JSONDecodeError:
                        tool_args = {}

                    logger.info("[HermesProcessor] 调用工具: %s", tool_name)
                    tool_result = await execute_tool(tool_name, tool_args, self.deps)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", f"call_{round_num}"),
                        "content": tool_result,
                    })

                continue  # 回到 LLM 继续

            # 情况 2: LLM 返回 content → 提取 JSON 并完成
            content = response.get("content", "")
            if content:
                from app.agents.base import BaseLLMService
                result = BaseLLMService.extract_json(content)
                if result:
                    logger.info("[HermesProcessor] Agent 完成 (round %d)", round_num)
                    return result

            # 情况 3: 既无 tool_calls 也无 content → 无法继续
            logger.warning("[HermesProcessor] LLM 返回空 content (round %d)", round_num)

        # 超过最大轮次
        logger.warning("[HermesProcessor] 超过最大轮次 %d，使用 fallback", self.max_tool_rounds)
        return self._fallback_result(request_type)

    async def _call_llm_with_tools(
        self, messages: List[Dict], tools: List[Dict],
    ) -> Optional[Dict]:
        """调用 LLM，支持 native function calling。返回 {"content": str, "tool_calls": [...]} 或 None"""
        try:
            from app.core.unified_llm_factory import get_llm_client

            client = get_llm_client(self.llm_config)
            if not client:
                logger.error("[HermesProcessor] LLM client 不可用")
                return None

            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.default_model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                ),
                timeout=self.llm_config.get("timeout", 120),
            )

            msg = response.choices[0].message

            # 安全关闭客户端
            try:
                client.close()
            except Exception:
                pass

            result = {}
            if msg.content:
                result["content"] = msg.content

            if msg.tool_calls:
                result["tool_calls"] = [
                    {
                        "id": tc.id,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]

            return result if result else None

        except asyncio.TimeoutError:
            logger.warning("[HermesProcessor] LLM 调用超时")
            return None
        except Exception as e:
            error_msg = str(e)
            # 如果 native function calling 不支持，回退到模拟模式
            if "tools" in error_msg.lower() or "tool_choice" in error_msg.lower():
                logger.warning("[HermesProcessor] Native function calling 不支持，使用模拟模式")
                return await self._simulated_tool_call(messages)
            logger.error("[HermesProcessor] LLM 调用失败: %s", error_msg[:200])
            return None

    async def _simulated_tool_call(self, messages: List[Dict]) -> Optional[Dict]:
        """模拟 tool calling: 用 BaseLLMService 直调，从响应中解析工具请求"""
        try:
            from app.agents.base import BaseLLMService

            svc = BaseLLMService(
                config=self.llm_config,
                default_model=self.default_model,
                default_timeout=120,
            )
            raw = await svc.call_llm(messages)

            if not raw or raw == "{}":
                return None

            # 尝试解析为 tool_call 请求
            data = svc.extract_json(raw)
            if isinstance(data, dict) and "tool_call" in data:
                tool_name = data["tool_call"]
                tool_args = data.get("arguments", {})
                logger.info("[HermesProcessor] 模拟工具调用: %s", tool_name)
                tool_result = await execute_tool(tool_name, tool_args, self.deps)

                # 反馈工具结果并再次调用
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f"工具 {tool_name} 返回结果:\n{tool_result}\n\n请基于此结果继续分析，输出最终 JSON。",
                })
                svc2 = BaseLLMService(
                    config=self.llm_config,
                    default_model=self.default_model,
                    default_timeout=120,
                )
                raw2 = await svc2.call_llm(messages)
                return {"content": raw2} if raw2 and raw2 != "{}" else None

            # 不是工具请求，当作最终答案
            return {"content": raw}

        except Exception as e:
            logger.warning("[HermesProcessor] 模拟 tool calling 失败: %s", e)
            return None

    # ─── Prompt Builders ───────────────────────────────────────────

    def _get_system_prompt(self, request_type: str) -> str:
        prompts = {
            "generate_battle_plan": BATTLE_PLAN_SYSTEM,
            "battle_plan": BATTLE_PLAN_SYSTEM,
            "midday_adjust": MIDDAY_ADJUST_SYSTEM,
            "generate_eod_review": EOD_REVIEW_SYSTEM,
            "eod_review": EOD_REVIEW_SYSTEM,
        }
        return prompts.get(request_type, EOD_REVIEW_SYSTEM)

    def _build_user_prompt(
        self, request_type: str, market: str, payload: Dict,
    ) -> str:
        """构建用户提示 — 将业务上下文注入"""
        market_name = "港股" if market == "hk" else "美股"
        today = datetime.now().strftime("%Y-%m-%d")

        lines = [
            f"市场: {market_name} ({market.upper()})",
            f"日期: {today}",
            "",
        ]

        # 注入 payload 中的业务数据
        if isinstance(payload, dict):
            for key in ["yesterday_review", "overnight_news", "market_context",
                       "feedback_context", "price_data", "trades_context",
                       "ashare_data", "volume_leaders"]:
                val = payload.get(key, "")
                if val:
                    label = {
                        "yesterday_review": "昨日复盘",
                        "overnight_news": "隔夜新闻",
                        "market_context": "市场环境",
                        "feedback_context": "反馈/教训",
                        "price_data": "量价数据",
                        "trades_context": "交易数据",
                        "ashare_data": "A股收盘",
                        "volume_leaders": "量比异动",
                    }.get(key, key)
                    val_str = str(val)[:2000]
                    lines.extend([f"【{label}】", val_str, ""])

        # 兜底: 提示 Agent 自己查数据
        if len(lines) <= 3:
            lines.extend([
                "请使用工具自行收集所需数据:",
                f"- query_database 查询 {market_name} {today} 的交易/持仓/信号",
                "- get_market_data 获取实时行情",
                "- get_or_update_memory 读取历史教训",
            ])

        return "\n".join(lines)

    def _fallback_result(self, request_type: str) -> Dict:
        """LLM 不可用时的降级结果"""
        today = datetime.now().strftime("%Y-%m-%d")

        if request_type in ("battle_plan", "generate_battle_plan"):
            return {
                "date": today,
                "market_regime": "NEUTRAL",
                "overall_bias": "NEUTRAL",
                "targets": [],
                "risk_warnings": ["Agent LLM 不可用，建议保守交易，减少仓位"],
                "avoid_symbols": [],
                "max_positions": 2,
                "key_question": "LLM 恢复后重新评估市场",
                "summary": "Hermes Agent LLM 调用失败，使用保守降级策略。等待 LLM 恢复。",
                "_fallback": True,
            }
        elif request_type == "midday_adjust":
            return {
                "date": today,
                "morning_summary": {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0, "win_rate": 0},
                "anomaly_flags": ["Agent 不可用，无法分析上午交易"],
                "plan_adjustments": [],
                "risk_adjustments": {"max_positions": 2, "reduce_exposure": True, "reason": "LLM 不可用"},
                "afternoon_bias": "NEUTRAL",
                "key_watch": [],
                "summary": "Hermes Agent 不可用，建议下午减仓观望。",
                "_fallback": True,
            }
        else:
            return {
                "date": today,
                "plan_execution": {"planned": 0, "executed": 0, "off_plan": 0, "execution_rate": 0},
                "trade_analysis": [],
                "regime_shift_flags": [],
                "emergent_risks": ["Agent LLM 不可用，复盘不完整"],
                "new_lessons": [],
                "strategy_adjustments": [],
                "next_day_preview": {"bias": "NEUTRAL", "key_question": "", "overnight_watch": []},
                "summary": "Hermes Agent 不可用，EOD 复盘降级。请查看原始交易数据。",
                "weekly_score": 50,
                "_fallback": True,
            }


# Module-level singleton
_processor: Optional[HermesAgentProcessor] = None


def get_processor(
    llm_config: Dict = None,
    deps: ToolDependencies = None,
) -> HermesAgentProcessor:
    """获取全局处理器单例"""
    global _processor
    if _processor is None:
        _processor = HermesAgentProcessor(llm_config=llm_config, deps=deps)
    return _processor
