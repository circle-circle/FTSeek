#!/usr/bin/env python3
"""
FTSeek-US HermesClient — Python 3.11 兼容的 Hermes Agent REST 客户端

通过 subprocess 与 hermes_worker.py (Python 3.11) 通信。
纯 Python 3.11 实现，仅依赖 subprocess + json + threading。

协议:
  → stdin:  {"id": "req-1", "method": "chat", "params": {...}}
  ← stdout: {"id": "req-1", "status": "ok", "result": {...}}

用法:
    from app.llm.hermes_client import HermesClient

    client = HermesClient()
    result = client.chat("分析 TSLA 走势", system="你是量化分析师")
    print(result["final_response"])
"""
import os
import sys
import json
import time
import uuid
import signal
import logging
import threading
import subprocess
from typing import Dict, Any, Optional, List
from pathlib import Path

logger = logging.getLogger("ftseek_us.hermes_client")

# Worker 配置
HERMES_WORKER_PYTHON = os.environ.get(
    "HERMES_WORKER_PYTHON",
    "/root/.hermes/hermes-agent/venv/bin/python3",
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WORKER_SCRIPT = str(PROJECT_ROOT / "scripts" / "hermes_worker.py")


class HermesWorkerError(Exception):
    """Worker 通信异常"""


class HermesWorkerTimeout(HermesWorkerError):
    """Worker 响应超时"""


class HermesClient:
    """
    FTSeek-US Hermes Agent 客户端

    通过 subprocess 与 Python 3.11 worker 进程通信，
    获取 Hermes Agent 全部 AI 能力。
    纯 Python 3.11 兼容，零外部依赖。
    """

    def __init__(self,
                 worker_python: str = None,
                 worker_script: str = None,
                 request_timeout: float = 120.0,
                 startup_timeout: float = 30.0):
        self.worker_python = worker_python or HERMES_WORKER_PYTHON
        self.worker_script = worker_script or WORKER_SCRIPT
        self.request_timeout = request_timeout
        self.startup_timeout = startup_timeout

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._req_counter = 0
        self._started = False

    # ── 进程生命周期 ──

    def _ensure_worker(self):
        """确保 worker 进程正在运行"""
        if self._proc is not None and self._proc.poll() is None:
            return  # 仍存活

        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return

            logger.info("启动 Hermes Worker: %s %s", self.worker_python, self.worker_script)
            try:
                self._proc = subprocess.Popen(
                    [self.worker_python, self.worker_script],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                # 等待 worker 就绪: 发送 ping 验证
                deadline = time.time() + self.startup_timeout
                last_error = None
                while time.time() < deadline:
                    if self._proc.poll() is not None:
                        stderr_output = ""
                        try:
                            stderr_output = self._proc.stderr.read()
                        except Exception:
                            pass
                        raise HermesWorkerError(
                            f"Worker 进程异常退出 (exit={self._proc.returncode}): {stderr_output[:500]}"
                        )
                    try:
                        ping_resp = self._raw_request("ping", {}, timeout=min(10, self.startup_timeout))
                        if ping_resp.get("status") == "ok":
                            logger.info("Hermes Worker 就绪 (PID=%d)", self._proc.pid)
                            return
                        last_error = ping_resp.get("error", "unknown")
                    except (HermesWorkerTimeout, Exception) as e:
                        last_error = str(e)
                        time.sleep(1)

                raise HermesWorkerError(f"Worker 启动超时: {last_error}")
            except HermesWorkerError:
                raise
            except Exception as e:
                self._cleanup_proc()
                raise HermesWorkerError(f"启动 Worker 失败: {e}")

    def _cleanup_proc(self):
        """清理 worker 进程"""
        if self._proc:
            try:
                if self._proc.poll() is None:
                    self._proc.send_signal(signal.SIGTERM)
                    try:
                        self._proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                        self._proc.wait(timeout=3)
            except Exception:
                pass
            finally:
                self._proc = None

    def _raw_request(self, method: str, params: dict,
                     timeout: float = None) -> dict:
        """发送原始 JSON 请求，返回完整响应字典"""
        if self._proc is None or self._proc.poll() is not None:
            raise HermesWorkerError("Worker 未运行")

        self._req_counter += 1
        req_id = f"req-{self._req_counter}-{uuid.uuid4().hex[:6]}"
        request = {"id": req_id, "method": method, "params": params}
        timeout = timeout or self.request_timeout

        try:
            req_line = json.dumps(request, ensure_ascii=False) + "\n"
            self._proc.stdin.write(req_line)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self._cleanup_proc()
            raise HermesWorkerError(f"stdin 写入失败 (worker 已崩溃): {e}")

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                line = self._read_line(timeout=min(remaining, 5))
                if line is None:
                    if self._proc.poll() is not None:
                        raise HermesWorkerError(
                            f"Worker 进程退出 (exit={self._proc.returncode})"
                        )
                    continue
                try:
                    resp = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("非 JSON 行: %s", line[:100])
                    continue
                if resp.get("id") == req_id:
                    return resp
            except HermesWorkerError:
                raise
            except Exception as e:
                logger.debug("_raw_request read error: %s", e)
                continue

        raise HermesWorkerTimeout(
            f"Worker 响应超时 ({timeout}s) method={method}"
        )

    def _read_line(self, timeout: float = 5) -> Optional[str]:
        """从 worker stdout 读取一行 (带超时)"""
        import select
        ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
        if ready:
            line = self._proc.stdout.readline()
            if line:
                return line.strip()
        return None

    # ── 公共 API ──

    def ping(self) -> bool:
        """检查 Worker 是否可用"""
        try:
            self._ensure_worker()
            return True
        except Exception as e:
            logger.debug("ping 失败: %s", e)
            return False

    def chat(self, prompt: str,
             system: str = "",
             max_iterations: int = 5,
             stream: bool = False,
             timeout: float = None) -> Optional[Dict[str, Any]]:
        """
        基础对话 (无工具调用)

        Args:
            prompt: 用户消息
            system: 系统提示
            max_iterations: 最大 LLM 轮次
            stream: 是否启用流式输出
            timeout: 超时秒数

        Returns:
            {"final_response": "...", "total_tokens": N, ...} 或 None
        """
        try:
            self._ensure_worker()
            resp = self._raw_request("chat", {
                "prompt": prompt,
                "system": system,
                "max_iterations": max_iterations,
                "stream": stream,
            }, timeout=timeout)
            if resp.get("status") == "ok":
                return resp["result"]
            logger.warning("chat 失败: %s", resp.get("error", "unknown"))
            return None
        except (HermesWorkerError, HermesWorkerTimeout) as e:
            logger.warning("hermes chat 异常: %s", e)
            return None
        except Exception as e:
            logger.error("hermes chat 未知异常: %s", e)
            return None

    def chat_with_tools(self, prompt: str,
                        system: str = "",
                        toolsets: List[str] = None,
                        max_iterations: int = 10,
                        timeout: float = None) -> Optional[Dict[str, Any]]:
        """
        带工具调用的对话

        Agent 可以自主使用 web_search, terminal, file 等工具完成任务。

        Args:
            prompt: 用户消息
            system: 系统提示
            toolsets: 启用的工具集 (默认 ["web", "file"])
            max_iterations: 最大轮次
            timeout: 超时秒数

        Returns:
            {"final_response": "...", "total_tokens": N, ...} 或 None
        """
        try:
            self._ensure_worker()
            resp = self._raw_request("chat_with_tools", {
                "prompt": prompt,
                "system": system,
                "toolsets": toolsets or ["web", "file"],
                "max_iterations": max_iterations,
            }, timeout=timeout)
            if resp.get("status") == "ok":
                return resp["result"]
            logger.warning("chat_with_tools 失败: %s", resp.get("error", "unknown"))
            return None
        except (HermesWorkerError, HermesWorkerTimeout) as e:
            logger.warning("hermes chat_with_tools 异常: %s", e)
            return None
        except Exception as e:
            logger.error("hermes chat_with_tools 未知异常: %s", e)
            return None

    def delegate(self, tasks: List[Dict[str, str]],
                 synthesizer_prompt: str = "",
                 max_iterations: int = 12,
                 timeout: float = None) -> Optional[Dict[str, Any]]:
        """
        批量并行任务委派 + 综合总结

        Worker 内部通过 delegate_task 并行执行多个子任务，
        然后将结果综合为一个报告。

        Args:
            tasks: 任务列表 [{"goal": "分析 NVDA", "context": "..."}, ...]
            synthesizer_prompt: 综合提示 (可选)
            max_iterations: 最大轮次
            timeout: 超时秒数

        Returns:
            {"final_response": "...", ...} 或 None
        """
        try:
            self._ensure_worker()
            resp = self._raw_request("delegate", {
                "tasks": tasks,
                "synthesizer_prompt": synthesizer_prompt,
                "max_iterations": max_iterations,
            }, timeout=timeout)
            if resp.get("status") == "ok":
                return resp["result"]
            logger.warning("delegate 失败: %s", resp.get("error", "unknown"))
            return None
        except (HermesWorkerError, HermesWorkerTimeout) as e:
            logger.warning("hermes delegate 异常: %s", e)
            return None
        except Exception as e:
            logger.error("hermes delegate 未知异常: %s", e)
            return None

    def debate(self, topic: str,
               debate_prompt: str = "",
               roles: List[Dict[str, str]] = None,
               judge_prompt: str = "",
               timeout: float = None) -> Optional[Dict[str, Any]]:
        """
        多角色辩论 + 终审法官

        并行运行多个分析师角色，各自独立判断，
        然后由法官综合各方意见给出最终结论。

        Args:
            topic: 辩论主题
            debate_prompt: 发给各分析师的具体问题
            roles: [{"name": "技术分析师", "system": "..."}]
            judge_prompt: 法官综合提示 (可选)
            timeout: 超时秒数

        Returns:
            {
                "final_response": "...",   # 法官最终结论
                "analysts": [...],         # 各分析师判断
                ...
            } 或 None
        """
        try:
            self._ensure_worker()
            params = {
                "topic": topic,
                "debate_prompt": debate_prompt or topic,
                "judge_prompt": judge_prompt,
            }
            if roles:
                params["roles"] = roles
            resp = self._raw_request("debate", params, timeout=timeout)
            if resp.get("status") == "ok":
                return resp["result"]
            logger.warning("debate 失败: %s", resp.get("error", "unknown"))
            return None
        except (HermesWorkerError, HermesWorkerTimeout) as e:
            logger.warning("hermes debate 异常: %s", e)
            return None
        except Exception as e:
            logger.error("hermes debate 未知异常: %s", e)
            return None

    # ── 便捷方法 ──

    def analyze_market(self, symbols: List[str],
                       focus: str = "技术面+情绪面") -> Optional[Dict]:
        """
        快捷美股分析 (chat_with_tools)

        Args:
            symbols: 股票代码列表 ["US.NVDA", "US.TSLA"]
            focus: 分析侧重
        """
        prompt = (
            f"分析以下美股今日走势:\n"
            + "\n".join(f"  - {s}" for s in symbols)
            + f"\n\n侧重: {focus}\n"
            "对每只股票给出: 趋势判断 (多/空/中性), 关键价位, 操作建议。用中文。"
        )
        return self.chat_with_tools(
            prompt=prompt,
            system="你是 FTSeek-US 美股量化分析师。简洁专业地输出分析结果。",
        )

    def premarket_analysis(self, context: dict) -> Optional[Dict]:
        """
        盘前综合分析

        综合新闻、宏观、持仓等因素，给出当日交易计划
        """
        prompt = f"""你是 FTSeek-US 美股盘前策略师。

【宏观环境】
{json.dumps(context.get('market_context', {}), ensure_ascii=False)}

【隔夜新闻】
{context.get('overnight_news', '无')}

【当前持仓】
{json.dumps(context.get('positions', []), ensure_ascii=False)}

【历史教训】
{context.get('memory', '无')}

请给出:
1. 市场阶段判断 (1-2句)
2. 今日重点标的及操作建议 (最多3只)
3. 风险提示
用中文，结构化输出。"""

        return self.chat_with_tools(
            prompt=prompt,
            system="你是美股量化交易策略师。基于数据做判断，避免主观臆测。",
            toolsets=["web", "file"],
            max_iterations=10,
        )

    def eod_attribution(self, trades: List[Dict],
                        signals: Dict,
                        plan: Dict = None) -> Optional[Dict]:
        """
        盘后全链路归因分析

        分析当日交易表现，提取教训，给出改进建议
        """
        prompt = f"""你是 FTSeek-US 盘后复盘师。

【今日交易】
{json.dumps(trades, ensure_ascii=False, indent=2)}

【信号漏斗】
{json.dumps(signals, ensure_ascii=False)}

【作战计划】
{json.dumps(plan, ensure_ascii=False, indent=2) if plan else '无'}

请分析:
1. 计划执行偏差
2. 盈亏归因 (按标的)
3. 系统性问题识别
4. 1-2条新教训
5. 明日改进建议
用中文，结构化输出。"""

        return self.chat(
            prompt=prompt,
            system="你是美股量化交易复盘专家。客观分析，不做主观辩护。",
            max_iterations=5,
        )

    def shutdown(self):
        """关闭 worker 连接"""
        try:
            if self._proc and self._proc.poll() is None:
                self._raw_request("shutdown", {}, timeout=5)
        except Exception:
            pass
        self._cleanup_proc()

    def __del__(self):
        self.shutdown()


# ── 模块级单例 ──

_hermes_client: Optional[HermesClient] = None


def get_hermes_client(**kwargs) -> HermesClient:
    """获取全局 HermesClient 单例"""
    global _hermes_client
    if _hermes_client is None:
        _hermes_client = HermesClient(**kwargs)
    return _hermes_client
