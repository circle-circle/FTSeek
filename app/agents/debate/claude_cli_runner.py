"""
Claude Code CLI Runner — 通过 claude -p CLI 调用真正的 Agent

设计原则:
1. 通过 subprocess 调用 claude -p --output-format json，赋予完整 Agent 能力
2. 可读取代码文件、分析日志、执行审查
3. 严格降级：CLI 失败 → API fallback（ClaudeCodeRunner）
4. 与 ClaudeCodeRunner (API) 并存，低耦合

Karpathy BEFORE 检查:
- 输出格式: JSON array, 最后一个 type=result 是最终结果
- 超时: 90s (辩论单次调用), 可配置
- 模型: deepseek-v4-pro (默认), 可通过 settings.json 配置
- 工具权限: 默认仅 Read (辩论审计不需要 Write/Bash)
"""
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional, List, Any

logger = logging.getLogger("ftseek_us.debate.claude_cli")

# FTSeek-US 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class ClaudeCLIRunner:
    """Claude Code CLI Agent — 通过 claude -p 执行真正的辩论审计"""

    def __init__(
        self,
        model: str = None,
        timeout: int = 90,
        max_turns: int = 5,
        allowed_tools: List[str] = None,
        workdir: str = None,
    ):
        import uuid
        self.session_id = str(uuid.uuid4())[:8]  # P2: 短 UUID 便于日志追踪
        self.default_model = model or os.getenv("CLAUDE_DEBATE_MODEL", "deepseek-v4-pro")
        self.timeout = timeout
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools or ["Read"]
        self.workdir = workdir or str(PROJECT_ROOT)
        self._lock = threading.Lock()

        # 调用计数统计
        self.stats = {"calls": 0, "successes": 0, "failures": 0, "total_cost": 0.0}

        logger.info(
            f"[ClaudeCLI] 已初始化 | model={self.default_model} | "
            f"timeout={timeout}s | max_turns={max_turns} | "
            f"tools={self.allowed_tools} | workdir={self.workdir}"
        )

    def run_analysis(
        self,
        prompt: str,
        system_prompt: str = None,
        round_num: int = 1,
        context: dict = None,
    ) -> Optional[Dict]:
        """
        通过 claude -p CLI 执行分析

        Args:
            prompt: 辩论 prompt (可包含注入的记忆/上下文)
            system_prompt: 系统角色提示 (通过 --append-system-prompt 注入)
            round_num: 辩论轮次
            context: 额外上下文 (unused, 保持接口兼容)

        Returns:
            解析后的 JSON dict, 失败返回 None
        """
        with self._lock:
            self.stats["calls"] += 1

        try:
            # 构建完整 prompt (system + user 合并, 因为 claude -p 不支持独立 system)
            full_prompt = ""
            if system_prompt:
                full_prompt += f"**角色定义**: {system_prompt}\n\n"
            full_prompt += prompt

            # 构建 CLI 命令
            # --bare: 跳过插件/MCP/CLAUDE.md 发现
            # --no-session-persistence: 不保存会话到磁盘，加速启动
            cmd = [
                "claude", "-p", "--bare", "--no-session-persistence", full_prompt,
                "--output-format", "json",
                "--max-turns", str(self.max_turns),
                "--allowedTools", ",".join(self.allowed_tools),
            ]

            logger.info(
                f"[ClaudeCLI:{self.session_id}] Round {round_num} 调用 | "
                f"prompt_len={len(full_prompt)} | model={self.default_model}"
            )

            env = os.environ.copy()
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
                cwd=self.workdir,
            )

            if result.returncode != 0:
                stderr_snippet = result.stderr[:300] if result.stderr else "无错误输出"
                logger.warning(
                    f"[ClaudeCLI] Round {round_num} 失败 (exit={result.returncode}): "
                    f"{stderr_snippet}"
                )
                with self._lock:
                    self.stats["failures"] += 1
                return None

            # 解析 JSON array 输出
            parsed = self._parse_output(result.stdout, round_num)
            if parsed:
                with self._lock:
                    self.stats["successes"] += 1
                    if "cost" in parsed:
                        self.stats["total_cost"] += parsed["cost"]
                logger.info(
                    f"[ClaudeCLI] Round {round_num} 成功 | "
                    f"turns={parsed.get('turns', '?')} | "
                    f"cost=${parsed.get('cost', 0):.5f}"
                )
                return parsed.get("data")
            else:
                logger.warning(f"[ClaudeCLI] Round {round_num} 解析失败")
                with self._lock:
                    self.stats["failures"] += 1
                return None

        except subprocess.TimeoutExpired:
            logger.error(f"[ClaudeCLI] Round {round_num} 超时 ({self.timeout}s)")
            with self._lock:
                self.stats["failures"] += 1
            return None
        except FileNotFoundError:
            logger.error("[ClaudeCLI] claude CLI 未找到 (请确认 npm install -g @anthropic-ai/claude-code)")
            with self._lock:
                self.stats["failures"] += 1
            return None
        except Exception as e:
            logger.error(f"[ClaudeCLI] Round {round_num} 异常: {e}", exc_info=True)
            with self._lock:
                self.stats["failures"] += 1
            return None

    def _parse_output(self, stdout: str, round_num: int) -> Optional[Dict]:
        """
        解析 claude -p --output-format json 输出

        输出格式: JSON array [system, assistant, ..., result]
        result 项包含: result, total_cost_usd, num_turns
        """
        try:
            events = json.loads(stdout)
            if not isinstance(events, list):
                logger.warning(f"[ClaudeCLI] 输出不是 JSON array: {type(events)}")
                return None

            for evt in events:
                if isinstance(evt, dict) and evt.get("type") == "result":
                    result_text = evt.get("result", "")
                    cost = evt.get("total_cost_usd", 0)
                    turns = evt.get("num_turns", 0)

                    # 解析 result 文本中的 JSON
                    data = self._extract_json(result_text)

                    return {"data": data, "cost": cost, "turns": turns}

            logger.warning(f"[ClaudeCLI] 未找到 result 事件 (共 {len(events)} 个事件)")
            return None

        except json.JSONDecodeError as e:
            logger.warning(f"[ClaudeCLI] JSON 解析失败: {e}")
            logger.debug(f"[ClaudeCLI] 输出前 200 字符: {stdout[:200]}")
            return None

    def _extract_json(self, text: str) -> Optional[Dict]:
        """
        从 Claude 的文本响应中提取 JSON

        处理多种格式:
        1. 纯 JSON: {"key": "value"}
        2. Markdown 代码块: ```json {...} ```
        3. 带前缀文本: Here is the result: {...}
        """
        if not text:
            return None

        text = text.strip()

        # 清理 markdown 代码块
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.find("```", start)
            if end > start:
                text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.find("```", start)
            if end > start:
                text = text[start:end].strip()

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 JSON 对象/数组
        # 查找第一个 { 和最后一个 }
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                return json.loads(text[brace_start : brace_end + 1])
            except json.JSONDecodeError:
                pass

        # 尝试提取 JSON 数组
        bracket_start = text.find("[")
        bracket_end = text.rfind("]")
        if bracket_start >= 0 and bracket_end > bracket_start:
            try:
                return json.loads(text[bracket_start : bracket_end + 1])
            except json.JSONDecodeError:
                pass

        logger.warning(f"[ClaudeCLI] 无法从文本中提取 JSON: {text[:200]}")
        return None

    def get_stats(self) -> Dict:
        """返回调用统计"""
        with self._lock:
            return dict(self.stats)

    @property
    def is_available(self) -> bool:
        """检查 claude CLI 是否可用"""
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False


# =====================================================================
#  模块级单例
# =====================================================================

_cli_runner: Optional[ClaudeCLIRunner] = None
_cli_runner_lock = threading.Lock()


def get_cli_runner(
    model: str = None, timeout: int = 90, max_turns: int = 5
) -> ClaudeCLIRunner:
    """获取或创建 CLI Runner 单例"""
    global _cli_runner
    # P0 修复 (2026-05-03): 加锁防止并发初始化竞态
    with _cli_runner_lock:
        if _cli_runner is None:
            _cli_runner = ClaudeCLIRunner(
                model=model, timeout=timeout, max_turns=max_turns
            )
    return _cli_runner


def reset_cli_runner():
    """测试用: 重置单例"""
    global _cli_runner
    with _cli_runner_lock:
        _cli_runner = None
