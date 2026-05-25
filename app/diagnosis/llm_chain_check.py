#!/usr/bin/env python3
"""港股 LLM 信号链路盘前诊断脚本

用法: python app/diagnosis/llm_chain_check.py

检查项:
1. battle_plan.json 是否包含 signal_impact 字段
2. dynamic_active_pool 是否存储 signal_impact metadata
3. executor.py 是否包含 LLM 门禁逻辑
4. consensus_enhancer.py 是否包含 llm_factor 加权
5. SYSTEM_PROMPT 是否定义 signal_impact 行为

输出: 诊断报告 + 准备状态
"""

import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAN_PATH = os.path.join(PROJECT_ROOT, "data", "daily_battle_plan.json")
POOL_PATH = os.path.join(PROJECT_ROOT, "data", "daily_active_pool.json")


def check_file_exists(path, name):
    if os.path.exists(path):
        print(f"  ✅ {name}: {path}")
        return True
    else:
        print(f"  ⚠️  {name}: 文件不存在 {path}")
        return False


def check_json_field(path, field, name):
    try:
        with open(path) as f:
            data = json.load(f)
        targets = data.get("targets", [])
        if not targets:
            print(f"  ⚠️  {name}: targets 为空")
            return False
        has_field = any(field in t for t in targets)
        if has_field:
            count = sum(1 for t in targets if field in t)
            print(f"  ✅ {name}: {count}/{len(targets)} 个标的包含 {field}")
            return True
        else:
            print(f"  ❌ {name}: targets 中未找到 {field} 字段")
            return False
    except Exception as e:
        print(f"  ❌ {name}: 解析失败 - {e}")
        return False


def check_code_contains(filepath, keywords, name):
    try:
        with open(filepath) as f:
            content = f.read()
        results = []
        for kw in keywords:
            found = kw in content
            results.append(found)
            status = "✅" if found else "❌"
            print(f"  {status} {name}: {'包含' if found else '缺失'} '{kw}'")
        return all(results)
    except Exception as e:
        print(f"  ❌ {name}: 文件读取失败 - {e}")
        return False


def main():
    print("=" * 65)
    print("港股 LLM 信号链路盘前诊断")
    print("=" * 65)

    all_pass = True

    # 1. battle_plan.json
    print("\n📄 1. 作战计划文件 (daily_battle_plan.json)")
    if check_file_exists(PLAN_PATH, "计划文件"):
        all_pass &= check_json_field(PLAN_PATH, "signal_impact", "signal_impact 字段")
        all_pass &= check_json_field(PLAN_PATH, "urgency", "urgency 字段")
    else:
        print("  ℹ️  盘前尚未生成计划，正常现象")

    # 2. dynamic_active_pool.json
    print("\n📄 2. 动态池文件 (daily_active_pool.json)")
    if check_file_exists(POOL_PATH, "池文件"):
        try:
            with open(POOL_PATH) as f:
                pool = json.load(f)
            entries = pool.get("entries", [])
            if not entries:
                print("  ⚠️  动态池为空")
            else:
                si_count = sum(1 for e in entries if "signal_impact" in e.get("metadata", {}))
                urg_count = sum(1 for e in entries if "urgency" in e.get("metadata", {}))
                print(f"  ✅ 动态池: {len(entries)} 只标的")
                print(f"  ✅ signal_impact: {si_count}/{len(entries)} 包含")
                print(f"  ✅ urgency: {urg_count}/{len(entries)} 包含")
                if si_count == 0:
                    print("  ℹ️  当前池中 signal_impact 为空 — 可能是修复前的旧数据")
                    print("  ℹ️  周一盘前 pipeline 执行后，新注入标的将自动携带 signal_impact")
                    # 不标记为失败，这是预期的历史数据状态
        except Exception as e:
            print(f"  ❌ 池文件解析失败: {e}")
            all_pass = False
    else:
        print("  ℹ️  动态池尚未初始化")

    # 3. executor.py LLM 门禁
    print("\n📄 3. 执行器门禁 (executor.py)")
    executor_path = os.path.join(PROJECT_ROOT, "app", "trader", "executor.py")
    all_pass &= check_code_contains(executor_path, [
        "llm_signal_impact",
        '_impact == "block"',  # 局部变量名带下划线前缀
        "llm_weaken_applied",
        "llm_boost_applied",
    ], "executor.py")

    # 4. consensus_enhancer.py 加权
    print("\n📄 4. 信号加权 (consensus_enhancer.py)")
    enhancer_path = os.path.join(PROJECT_ROOT, "app", "strategy", "consensus_enhancer.py")
    all_pass &= check_code_contains(enhancer_path, [
        "_get_llm_signal_impact_factor",
        '"boost": 1.15',
        "final_score *= llm_factor",
    ], "consensus_enhancer.py")

    # 5. premarket_analyst.py SYSTEM_PROMPT
    print("\n📄 5. LLM Prompt (premarket_analyst.py)")
    analyst_path = os.path.join(PROJECT_ROOT, "app", "pipeline", "premarket_analyst.py")
    all_pass &= check_code_contains(analyst_path, [
        '"signal_impact"',
        'signal_impact=str(validated_data',
        "get_cur_kline",
    ], "premarket_analyst.py")

    # 6. LLM 质量追踪器
    print("\n📄 6. LLM 质量追踪 (llm_quality_tracker.py)")
    tracker_path = os.path.join(PROJECT_ROOT, "app", "consensus", "llm_quality_tracker.py")
    all_pass &= check_code_contains(tracker_path, [
        "record_signal",
        "record_outcome",
        "find_open_signal_by_code",
        "get_calibration_suggestions",
    ], "llm_quality_tracker.py")

    # 7. 执行器 · 质量追踪集成 (executor.py)
    print("\n📄 7. 执行器 · 质量追踪集成 (executor.py)")
    all_pass &= check_code_contains(executor_path, [
        "LLM 信号质量追踪",
        "llm_quality_tracker",
        "record_signal",
        "record_outcome",
    ], "executor.py")

    # 8. HSI 门禁 shadow + 缓存
    print("\n📄 8. HSI 门禁 · Shadow 模式 + 缓存 (executor.py)")
    all_pass &= check_code_contains(executor_path, [
        "_hsi_cache",
        "hsi_gap_min_pct",
        "hsi_regime_warning",
    ], "executor.py")

    # 9. 门禁叠加分析脚本
    print("\n📄 9. 门禁叠加分析 (gate_stack_analysis.py)")
    gate_analysis_path = os.path.join(PROJECT_ROOT, "app", "diagnostics", "gate_stack_analysis.py")
    all_pass &= check_file_exists(gate_analysis_path, "gate_stack_analysis.py")

    # 10. 共识引擎 · LLM 分数回写
    print("\n📄 10. 共识引擎 · LLM 分数回写 (consensus_engine.py)")
    engine_path = os.path.join(PROJECT_ROOT, "app", "strategy", "consensus_engine.py")
    all_pass &= check_code_contains(engine_path, [
        "llm_signal_impact_factor",
        "result.score = enhanced.final_score",
        "LLM乘数",
    ], "consensus_engine.py")

    # 11. 数据订阅器 · LLM metadata 透传 (data_subscriber.py)
    print("\n📄 11. 数据订阅器 · LLM metadata 透传 (data_subscriber.py)")
    sub_path = os.path.join(PROJECT_ROOT, "app", "data_subscriber.py")
    all_pass &= check_code_contains(sub_path, [
        "llm_signal_impact_factor",
        "final_score",
        "quality_boost",
    ], "data_subscriber.py")

    # 12. 午间刷新器 · LLM signal_impact 继承 (midday_plan_refresher.py)
    print("\n📄 12. 午间刷新器 · LLM signal_impact 继承 (midday_plan_refresher.py)")
    midday_path = os.path.join(PROJECT_ROOT, "app", "pipeline", "midday_plan_refresher.py")
    all_pass &= check_code_contains(midday_path, [
        "_lookup_premarket_signal_impact",
        "impact_map",
    ], "midday_plan_refresher.py")

    # 13. 辩论编排器 · LLM signal_impact 感知 (debate_orchestrator.py)
    print("\n📄 13. 辩论编排器 · LLM signal_impact 感知 (debate_orchestrator.py)")
    debate_path = os.path.join(PROJECT_ROOT, "app", "agents", "debate", "debate_orchestrator.py")
    all_pass &= check_code_contains(debate_path, [
        "signal_impact",
        "LLM影响",
    ], "debate_orchestrator.py")

    # 14. 代码规范化 · HK 前缀兼容 (executor.py)
    print("\n📄 14. 代码规范化 · HK 前缀兼容查找 (executor.py)")
    all_pass &= check_code_contains(executor_path, [
        "if not _entry and not signal.code.startswith(\"HK.\"):",
        "f\"HK.{signal.code}\"",
    ], "executor.py (prefix norm)")

    # 15. 拦截器 · 盘前上下文注入 (agent_bridge.py)
    print("\n📄 15. 拦截器 · 盘前 signal_impact 上下文 (agent_bridge.py)")
    bridge_path = os.path.join(PROJECT_ROOT, "app", "llm", "agent_bridge.py")
    all_pass &= check_code_contains(bridge_path, [
        "premarket_signal_impact",
    ], "agent_bridge.py")

    # 16. 交易日历 · 半日市支持 (holiday_calendar.py + market_hours.py + executor.py)
    print("\n📄 16. 交易日历 · 半日市支持 (HK half-day awareness)")
    cal_path = os.path.join(PROJECT_ROOT, "app", "utils", "holiday_calendar.py")
    all_pass &= check_code_contains(cal_path, [
        "HK_HALF_DAYS_2026",
        "is_hk_half_day",
    ], "holiday_calendar.py")

    mkt_path = os.path.join(PROJECT_ROOT, "app", "shared", "market_hours.py")
    all_pass &= check_code_contains(mkt_path, [
        "is_hk_half_day",
        "半日市",
    ], "market_hours.py (half-day phase)")

    all_pass &= check_code_contains(executor_path, [
        "half_day_closed",
        "半日市下午",
    ], "executor.py (half-day gate)")

    # Summary
    print(f"\n{'=' * 65}")
    if all_pass:
        print("✅ 诊断通过 — LLM 信号链路已就绪，可以接收周一开盘数据")
    else:
        print("⚠️  诊断未完全通过，请检查上述 ❌ 标记项")
    print(f"{'=' * 65}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
