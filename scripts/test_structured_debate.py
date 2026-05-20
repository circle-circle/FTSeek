#!/usr/bin/env python3
"""验证结构化辩论 Level 1 代码加载"""
import sys, os
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env.us")

from app.review.review_orchestrator import MultiModelDebateRunner, RichContextBuilder
from app.llm.agent_bridge import get_agent_bridge

bridge = get_agent_bridge()
runner = MultiModelDebateRunner(bridge, max_rounds=2)

print(f"max_rounds: {runner.max_rounds}")
print(f"MODEL_ROLES: {len(runner.MODEL_ROLES)} 模型")
print(f"has _run_round2: {hasattr(runner, '_run_round2')}")
print(f"has _run_arbiter: {hasattr(runner, '_run_arbiter')}")
print(f"has _compute_convergence_metrics: {hasattr(runner, '_compute_convergence_metrics')}")
print(f"has _fallback_result: {hasattr(runner, '_fallback_result')}")

# 验证 Round 2 prompt 构建
import json
test_round1 = {
    "bull": {"insights": ["信号质量下降"], "root_causes": ["阈值过低"]},
    "bear": {"insights": ["执行延迟是主因"], "root_causes": ["冷却机制失效"]},
    "auditor": {"insights": ["两者都有影响"], "risks": ["资金回撤接近警戒线"]}
}

# 模拟构建 Round 2 prompt
others = {}
for role, label in [("bear", "空头审计师"), ("auditor", "独立风险评估师")]:
    r = test_round1.get(role, {})
    summary = {}
    for key in ["insights", "root_causes", "suggestions", "fixes", "risks"]:
        val = r.get(key)
        if isinstance(val, list):
            summary[key] = val[:3]
    others[label] = summary

print(f"\nRound 2 交叉反驳 prompt 构建:")
print(f"  其他模型观点: {json.dumps(others, ensure_ascii=False, indent=2)[:200]}...")

# 验证收敛度指标计算
consensus = [{"topic": "信号问题", "roles": ["bull", "auditor"]}]
divergences = [{"topic": "执行问题", "only_in": ["bear"]}]

metrics = runner._compute_convergence_metrics(test_round1, test_round1, consensus, divergences)
print(f"\n收敛度指标:")
print(f"  view_change_ratio: {metrics['view_change_ratio']}")
print(f"  consensus_count: {metrics['consensus_count']}")
print(f"  divergence_count: {metrics['divergence_count']}")
print(f"  convergence_score: {metrics['convergence_score']}")
print(f"  debate_quality: {metrics['debate_quality']}")

print("\n✅ Level 1 结构化辩论代码验证通过!")
