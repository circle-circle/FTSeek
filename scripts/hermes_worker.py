#!/usr/bin/env python3.11
"""
FTSeek-US Hermes Agent Worker
==============================
Python 3.11 独立进程，通过 stdin/stdout JSON 协议提供 Hermes Agent 能力。

协议:
  请求 (stdin, 每行一个 JSON):
    {"id": "req-1", "method": "chat", "params": {"prompt": "...", "system": "..."}}
  响应 (stdout, 每行一个 JSON):
    {"id": "req-1", "status": "ok", "result": {...}}
    {"id": "req-1", "status": "error", "error": "..."}

支持方法:
  - ping          健康检查
  - chat          基础对话 (无工具)
  - chat_with_tools  带工具调用
  - delegate      批量并行 delegate_task
  - debate        多角色辩论 + 终审
  - shutdown      优雅退出

用法:
  # 交互模式 (持久进程, 等待 stdin 输入)
  python3.11 scripts/hermes_worker.py

  # 单次调用模式
  python3.11 scripts/hermes_worker.py --once '{"method":"chat","params":{"prompt":"hello"}}'

  # 自检模式
  python3.11 scripts/hermes_worker.py --test
"""
import os
import sys
import json
import time
import signal
import logging
import argparse
import threading
from pathlib import Path
from typing import Dict, Any, Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 设置 Hermes 路径 ──
HERMES_HOME = Path(os.path.expanduser("~/.hermes"))
HERMES_AGENT_DIR = HERMES_HOME / "hermes-agent"
sys.path.insert(0, str(HERMES_AGENT_DIR))

# 加载配置
import yaml
with open(HERMES_HOME / "config.yaml") as f:
    _cfg = yaml.safe_load(f)

_model_cfg = _cfg["model"]
_model_name = _model_cfg.get("model") or _model_cfg.get("default")
os.environ["OPENAI_API_KEY"] = _model_cfg["api_key"]
os.environ["OPENAI_BASE_URL"] = _model_cfg["base_url"]

from hermes_cli.env_loader import load_hermes_dotenv
load_hermes_dotenv(hermes_home=HERMES_HOME)

# ── 日志全部输出到 stderr，保持 stdout 纯净给 JSON 协议 ──
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [hermes-worker] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("hermes_worker")

# ── 延迟导入 AIAgent (重载耗时但只需一次) ──
from run_agent import AIAgent


class HermesWorker:
    """Hermes Agent Worker — JSON 协议处理器"""

    def __init__(self):
        self._agent_cache: Dict[str, AIAgent] = {}  # 按 toolsets 缓存 agent 实例
        self._lock = threading.Lock()

    def _get_agent(self, toolsets: Optional[List[str]] = None,
                   max_iterations: int = 8) -> AIAgent:
        """获取或创建 AIAgent 实例 (按 toolsets 缓存)"""
        ts_key = ",".join(sorted(toolsets or []))
        cache_key = f"{ts_key}:{max_iterations}"
        if cache_key not in self._agent_cache:
            self._agent_cache[cache_key] = AIAgent(
                base_url=_model_cfg["base_url"],
                api_key=_model_cfg["api_key"],
                model=_model_name,
                provider=_model_cfg.get("provider", "custom"),
                max_iterations=max_iterations,
                quiet_mode=True,
                enabled_toolsets=toolsets or [],
                verbose_logging=False,
            )
            logger.info("创建 AIAgent 实例: toolsets=%s max_iter=%d", ts_key, max_iterations)
        return self._agent_cache[cache_key]

    def handle(self, request: dict) -> dict:
        """处理单个请求，返回响应字典"""
        req_id = request.get("id", "unknown")
        method = request.get("method", "ping")
        params = request.get("params", {})

        try:
            if method == "ping":
                return {"id": req_id, "status": "ok", "result": {
                    "model": _model_name,
                    "base_url": _model_cfg["base_url"],
                    "timestamp": time.time(),
                }}

            elif method == "chat":
                return self._handle_chat(req_id, params)

            elif method == "chat_with_tools":
                return self._handle_chat_with_tools(req_id, params)

            elif method == "delegate":
                return self._handle_delegate(req_id, params)

            elif method == "debate":
                return self._handle_debate(req_id, params)

            elif method == "shutdown":
                return {"id": req_id, "status": "ok", "result": {"message": "shutting down"}}

            else:
                return {"id": req_id, "status": "error",
                        "error": f"unknown method: {method}"}

        except Exception as e:
            logger.error("处理请求失败 [%s] %s: %s", req_id, method, e)
            return {"id": req_id, "status": "error", "error": str(e)}

    # ── 方法实现 ──

    def _handle_chat(self, req_id: str, params: dict) -> dict:
        prompt = params.get("prompt", "")
        system = params.get("system", "")
        max_iterations = params.get("max_iterations", 5)
        stream = params.get("stream", False)

        agent = self._get_agent(toolsets=[], max_iterations=max_iterations)

        stream_chunks = []
        def stream_cb(text: str):
            stream_chunks.append(text)

        result = agent.run_conversation(
            user_message=prompt,
            system_message=system or None,
            stream_callback=stream_cb if stream else None,
        )

        return {"id": req_id, "status": "ok", "result": {
            "final_response": result.get("final_response", ""),
            "total_tokens": result.get("total_tokens"),
            "prompt_tokens": result.get("prompt_tokens"),
            "completion_tokens": result.get("completion_tokens"),
            "api_calls": result.get("api_calls"),
            "completed": result.get("completed", False),
            "stream_chunks": stream_chunks if stream else None,
        }}

    def _handle_chat_with_tools(self, req_id: str, params: dict) -> dict:
        prompt = params.get("prompt", "")
        system = params.get("system", "")
        toolsets = params.get("toolsets", ["web", "terminal", "file"])
        max_iterations = params.get("max_iterations", 10)

        agent = self._get_agent(toolsets=toolsets, max_iterations=max_iterations)

        result = agent.run_conversation(
            user_message=prompt,
            system_message=system or (
                "你是 FTSeek-US 美股量化交易分析系统。使用可用工具完成任务，输出简洁专业。"
            ),
        )

        return {"id": req_id, "status": "ok", "result": {
            "final_response": result.get("final_response", ""),
            "total_tokens": result.get("total_tokens"),
            "api_calls": result.get("api_calls"),
            "completed": result.get("completed", False),
        }}

    def _handle_delegate(self, req_id: str, params: dict) -> dict:
        """批量 delegate_task + 综合总结"""
        tasks = params.get("tasks", [])
        synthesizer_prompt = params.get("synthesizer_prompt", "")
        max_iterations = params.get("max_iterations", 12)

        if not tasks:
            return {"id": req_id, "status": "error", "error": "tasks is required"}

        # 构建 delegation agent
        agent = self._get_agent(
            toolsets=["delegation", "web", "file"],
            max_iterations=max_iterations,
        )

        # 构建 delegate_task 调用提示
        tasks_json = json.dumps(tasks, ensure_ascii=False, indent=2)
        delegate_prompt = (
            f"使用 delegate_task 工具并行执行以下 {len(tasks)} 个任务。\n\n"
            f"任务列表:\n{tasks_json}\n\n"
        )
        if synthesizer_prompt:
            delegate_prompt += (
                f"所有子任务完成后，请综合结果:\n{synthesizer_prompt}\n"
            )
        else:
            delegate_prompt += (
                "所有子任务完成后，请按以下格式输出综合报告:\n"
                "1. 各任务关键发现 (每任务1-2句)\n"
                "2. 共识与分歧\n"
                "3. 最终建议\n"
            )

        result = agent.run_conversation(
            user_message=delegate_prompt,
            system_message=(
                "你是 FTSeek-US 任务调度器。使用 delegate_task 并行分发任务，"
                "tasks 参数传入任务数组。每个子任务用中文输出。"
            ),
        )

        return {"id": req_id, "status": "ok", "result": {
            "final_response": result.get("final_response", ""),
            "total_tokens": result.get("total_tokens"),
            "api_calls": result.get("api_calls"),
            "completed": result.get("completed", False),
        }}

    def _handle_debate(self, req_id: str, params: dict) -> dict:
        """多角色辩论 + 终审法官"""
        topic = params.get("topic", "")
        roles = params.get("roles", [
            {"name": "技术分析师", "system": "你是纯粹的技术分析专家，看K线、指标、量价关系。倾向用技术面证据做判断。"},
            {"name": "宏观分析师", "system": "你是宏观经济学家，关注利率、通胀、地缘政治、资金流向。倾向用宏观面证据做判断。"},
            {"name": "情绪分析师", "system": "你是市场情绪/行为金融专家，关注期权链、做空比率、新闻情绪、散户热度。倾向用情绪面证据做判断。"},
        ])
        debate_prompt = params.get("debate_prompt", topic)
        judge_prompt = params.get("judge_prompt", "")

        if not debate_prompt:
            return {"id": req_id, "status": "error", "error": "topic or debate_prompt is required"}
        if not roles:
            return {"id": req_id, "status": "error", "error": "at least one role is required"}

        # Phase 1: 并行运行各角色分析师
        def run_role(role_cfg: dict) -> dict:
            agent = AIAgent(
                base_url=_model_cfg["base_url"],
                api_key=_model_cfg["api_key"],
                model=_model_name,
                max_iterations=2,
                quiet_mode=True,
                enabled_toolsets=[],
                verbose_logging=False,
            )
            result = agent.run_conversation(
                user_message=debate_prompt,
                system_message=role_cfg["system"],
            )
            return {
                "role": role_cfg["name"],
                "verdict": result.get("final_response", ""),
                "tokens": result.get("total_tokens", 0),
            }

        analysts = []
        absent_roles = []
        with ThreadPoolExecutor(max_workers=min(len(roles), 4)) as executor:
            futures = {executor.submit(run_role, r): r for r in roles}
            for future in as_completed(futures):
                # 2026-07-25 (failure DEB B③): per-role 兜底 — 单角色异常不再整场全灭
                try:
                    _a = future.result()
                except Exception as _re:
                    _a = {"role": futures[future].get("name", "?"), "verdict": "", "tokens": 0,
                          "error": str(_re)[:200]}
                analysts.append(_a)

        # 2026-07-25 (failure DEB B③): 空 verdict 质量门 — 空意见不入法官 prompt
        #   (此前静默混入, 法官在残缺输入上综合)。语义=标注缺席继续 (对齐 dual_agent_debate
        #   hermes_only/claude_only 降级模式); 缺席角色计数入响应供 DA 侧告警。
        _present = [a for a in analysts if (a.get("verdict") or "").strip()]
        absent_roles = [a.get("role", "?") for a in analysts if not (a.get("verdict") or "").strip()]
        if not _present:
            return {"id": req_id, "status": "error",
                    "error": f"all_roles_absent ({len(analysts)} roles, 全部空 verdict)"}

        # Phase 2: 法官综合 (仅非空意见; 缺席角色显式标注)
        opinions = "\n\n".join(
            f"【{a['role']}】: {a['verdict']}" for a in _present
        )
        if absent_roles:
            opinions += f"\n\n(注: 角色 {', '.join(absent_roles)} 本场缺席/无输出, 未纳入意见)"
        judge_default = (
            f"以下是 {len(_present)} 位分析师对同一问题的独立判断:\n\n{opinions}\n\n"
            "请综合以上各方意见，给出最终判断，说明共识程度和分歧点。"
        )

        judge_agent = AIAgent(
            base_url=_model_cfg["base_url"],
            api_key=_model_cfg["api_key"],
            model=_model_name,
            max_iterations=2,
            quiet_mode=True,
            enabled_toolsets=[],
            verbose_logging=False,
        )
        final = judge_agent.run_conversation(
            user_message=judge_prompt or judge_default,
            system_message="你是首席投资官，综合各方专家意见做出最终决策。用中文回答。",
        )

        return {"id": req_id, "status": "ok", "result": {
            "final_response": final.get("final_response", ""),
            "analysts": [
                {"role": a["role"], "verdict": a["verdict"][:300], "tokens": a["tokens"]}
                for a in analysts
            ],
            "total_tokens": final.get("total_tokens", 0),
            "completed": final.get("completed", False),
            # 2026-07-25 (failure DEB B③): 缺席角色透出, 供 DA 侧告警 (阈值首周数据校准)
            "absent_roles": absent_roles,
        }}

    def shutdown(self):
        """清理资源"""
        self._agent_cache.clear()
        logger.info("Worker 已关闭")


def run_interactive():
    """交互模式: 持久进程，stdin 逐行处理"""
    worker = HermesWorker()
    logger.info("Worker 启动 (交互模式), PID=%d", os.getpid())

    # 优雅退出
    running = True

    def _handle_signal(signum, frame):
        nonlocal running
        logger.info("收到信号 %d，准备退出", signum)
        running = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    for line in sys.stdin:
        if not running:
            break
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            resp = {"id": "unknown", "status": "error", "error": f"invalid JSON: {e}"}
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        method = request.get("method", "")
        if method == "shutdown":
            resp = worker.handle(request)
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            break

        resp = worker.handle(request)
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    worker.shutdown()


def run_once(request_json: str):
    """单次调用模式"""
    worker = HermesWorker()
    request = json.loads(request_json)
    resp = worker.handle(request)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    worker.shutdown()


def run_test():
    """自检模式"""
    print("=" * 50)
    print("Hermes Worker 自检")
    print("=" * 50)
    print(f"Python:   {sys.version}")
    print(f"Hermes:   {HERMES_AGENT_DIR}")
    print(f"Model:    {_model_name}")
    print(f"Base URL: {_model_cfg['base_url']}")
    print()

    worker = HermesWorker()

    # 1. ping
    t0 = time.time()
    resp = worker.handle({"id": "test-1", "method": "ping"})
    print(f"[ping]  status={resp['status']} ({time.time()-t0:.1f}s)")
    assert resp["status"] == "ok", f"ping failed: {resp}"

    # 2. chat (简单对话)
    t0 = time.time()
    resp = worker.handle({
        "id": "test-2", "method": "chat",
        "params": {
            "prompt": "用一句话介绍美股市场。用中文回答。",
            "system": "你是金融分析师，简洁回答。",
            "max_iterations": 2,
        }
    })
    result = resp.get("result", {})
    print(f"[chat]  status={resp['status']}, tokens={result.get('total_tokens')}, "
          f"response={result.get('final_response','')[:80]}... ({time.time()-t0:.1f}s)")
    assert resp["status"] == "ok", f"chat failed: {resp}"

    # 3. debate (轻量辩论)
    t0 = time.time()
    resp = worker.handle({
        "id": "test-3", "method": "debate",
        "params": {
            "topic": "2026年5月应该做多还是做空QQQ?",
            "debate_prompt": "分析: 2026年5月应该做多还是做空QQQ? 给出判断和2条理由。50字以内，用中文。",
            "roles": [
                {"name": "乐观方", "system": "你是乐观派分析师，倾向寻找做多理由。"},
                {"name": "谨慎方", "system": "你是谨慎派分析师，倾向寻找风险。"},
            ],
        }
    })
    result = resp.get("result", {})
    print(f"[debate] status={resp['status']}, analysts={len(result.get('analysts',[]))}, "
          f"tokens={result.get('total_tokens')}, "
          f"verdict={result.get('final_response','')[:80]}... ({time.time()-t0:.1f}s)")
    assert resp["status"] == "ok", f"debate failed: {resp}"

    worker.shutdown()
    print(f"\n✅ 自检全部通过")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FTSeek-US Hermes Agent Worker")
    parser.add_argument("--once", type=str, help="单次调用，传入 JSON 请求")
    parser.add_argument("--test", action="store_true", help="自检模式")
    args = parser.parse_args()

    if args.test:
        run_test()
    elif args.once:
        run_once(args.once)
    else:
        run_interactive()
