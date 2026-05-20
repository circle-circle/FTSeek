#!/usr/bin/env python3.8
"""
FTSeek-US Hermes Agent 端到端集成测试
测试链路: Worker → HermesClient → AgentBridge → DualAgentDebate

验证:
1. Worker 存活 (ping)
2. HermesClient.chat() 基础对话
3. AgentBridge.call_agent_with_tools() 工具调用
4. AgentBridge.call_agent_debate() 多角色辩论
5. DualAgentDebate.run_agent_debate() 完整辩论流程
"""
import os
import sys
import time
import json
import logging

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, '.env.us'))

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("ftseek_us.e2e_test")

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")

# ═══════════════════════════════════════════════════════════════
print("=" * 70)
print("FTSeek-US Hermes Agent 端到端集成测试")
print(f"Python: {sys.version}")
print("=" * 70)

# ── Test 1: HermesClient ping ──
print("\n--- Test 1: HermesClient Worker 连通性 ---")
try:
    from app.llm.hermes_client import get_hermes_client
    client = get_hermes_client()
    ok = client.ping()
    check("Worker ping", ok, f"(response in {client._last_ping_ms:.0f}ms)" if hasattr(client, '_last_ping_ms') else "")
except Exception as e:
    check("Worker ping", False, str(e))
    client = None

# ── Test 2: HermesClient.chat() 基础对话 ──
print("\n--- Test 2: HermesClient.chat() 基础对话 ---")
if client:
    try:
        t0 = time.time()
        r = client.chat(
            prompt="用一句话回答: 什么是量化交易? 用中文，20字以内。",
            system="你是金融科技助手。",
            max_iterations=1,
            timeout=120,
        )
        elapsed = time.time() - t0
        check("chat 返回非空", r is not None)
        check("chat 包含 final_response", r and "final_response" in r)
        if r:
            check("chat 包含 tokens", r.get("total_tokens", 0) > 0, f"(tokens={r.get('total_tokens')})")
            print(f"    响应: {r.get('final_response', '')[:100]}")
            print(f"    耗时: {elapsed:.1f}s, tokens: {r.get('total_tokens')}")
    except Exception as e:
        check("chat 执行", False, str(e))

# ── Test 3: AgentBridge.call_agent_with_tools() ──
print("\n--- Test 3: AgentBridge.call_agent_with_tools() ---")
try:
    from app.llm.agent_bridge import get_agent_bridge
    bridge = get_agent_bridge()
    t0 = time.time()
    r = bridge.call_agent_with_tools(
        prompt="查询当前 NVDA 股价，如果查不到就用你的知识回答当前大概价位。用中文回答，50字以内。",
        system_prompt="你是美股分析师。用中文。",
        toolsets=["web"],
        timeout=120,
    )
    elapsed = time.time() - t0
    check("tools 返回非空", r is not None)
    if r:
        check("tools 包含 response", bool(r.get("response")))
        mode = r.get("mode", "unknown")
        check("tools mode", mode in ("hermes_agent", "fallback_bailian"), f"(mode={mode})")
        print(f"    模式: {mode}, 耗时: {elapsed:.1f}s, tokens: {r.get('tokens')}")
        print(f"    响应: {r.get('response', '')[:150]}")
except Exception as e:
    check("tools 执行", False, str(e))

# ── Test 4: AgentBridge.call_agent_debate() ──
print("\n--- Test 4: AgentBridge.call_agent_debate() ---")
try:
    debate_topic = "2026年5月 NVDA 短线走势: 看多还是看空?"
    t0 = time.time()
    r = bridge.call_agent_debate(
        topic=debate_topic,
        debate_prompt="请给出明确判断 (LONG/SHORT) 和核心理由。用中文，80字以内。",
        timeout=300,
    )
    elapsed = time.time() - t0
    check("debate 返回非空", r is not None)
    if r:
        check("debate 包含 verdict", bool(r.get("verdict")))
        mode = r.get("mode", "unknown")
        check("debate mode", mode in ("hermes_debate", "fallback_single"), f"(mode={mode})")
        analysts = r.get("analysts", [])
        print(f"    模式: {mode}, 分析师: {len(analysts)} 位, 耗时: {elapsed:.1f}s")
        print(f"    结论: {r.get('verdict', '')[:200]}")
except Exception as e:
    check("debate 执行", False, str(e))

# ── Test 5: DualAgentDebate.run_agent_debate() ──
print("\n--- Test 5: DualAgentDebate.run_agent_debate() ---")
try:
    from app.llm.dual_agent_debate import DualAgentDebate
    dab = DualAgentDebate(agent_bridge=bridge)
    t0 = time.time()
    r = dab.run_agent_debate(
        topic="2026年5月美股科技板块风险与机会",
        prompt="从技术面、宏观面、情绪面分别分析。各50字以内。中文。",
    )
    elapsed = time.time() - t0
    check("DAD 返回非空", r is not None)
    if r:
        check("DAD 包含 verdict", bool(r.get("verdict")))
        mode = r.get("mode", "unknown")
        analysts = r.get("analysts", [])
        print(f"    模式: {mode}, 分析师: {len(analysts)} 位, 耗时: {elapsed:.1f}s")
        print(f"    结论: {r.get('verdict', '')[:200]}")
except Exception as e:
    check("DAD 执行", False, str(e))

# ═══════════════════════════════════════════════════════════════
print(f"\n{'=' * 70}")
print(f"测试结果: {PASS} 通过, {FAIL} 失败 (共 {PASS + FAIL} 项)")
print("=" * 70)

if FAIL > 0:
    print("⚠️  存在失败项，请检查日志")
    sys.exit(1)
else:
    print("✅ 所有测试通过")
    sys.exit(0)
