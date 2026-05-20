#!/usr/bin/env python3
"""
Shadow Verification — 验证新管线 (P0+P1+P2) 在实盘数据下的表现

非破坏性: 纯读操作, 不提交任何订单, 不修改生产状态。
使用生产 DI 容器获取真实 data_router / risk_engine / voting_coordinator 等依赖,
喂入新 3-stage 管线, 逐阶段验证数据契约输出 + Observer 事件。

运行方式:
    cd /opt/ftseek/FTSeek-US && python scripts/shadow_verify_pipeline.py
"""

import os
import sys
import logging
import time
from datetime import datetime

PROJECT_DIR = "/opt/ftseek/FTSeek-US"
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, '.env.us'))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("shadow_verify")

# ── 测试标的 ──
TEST_SYMBOLS = ["US.AAPL", "US.NVDA", "US.TSLA", "US.MSFT", "US.META"]


def get_live_shared_state(container):
    """从生产 DI 容器获取 shared_state (读操作, 不修改任何状态)"""
    risk_engine = container.risk_engine()
    notifier = container.notifier()
    data_provider = container.data_provider()

    # VIX / SPY
    vix = 0.0
    spy_ctx = {"spy_price": 0, "spy_ma200": 0}
    try:
        vix_data = data_provider.get_vix()
        if vix_data:
            if isinstance(vix_data, (int, float)):
                vix = float(vix_data)
            elif isinstance(vix_data, dict):
                vix = float(vix_data.get("value", vix_data.get("vix", 0)) or 0)
    except Exception as e:
        logger.warning(f"VIX 获取失败: {e}")

    try:
        spy_raw = data_provider.get_full_market_context("US.SPY")
        if spy_raw:
            spy_ctx["spy_price"] = float(spy_raw.get("price", 0) or 0)
            spy_ctx["spy_ma200"] = float(spy_raw.get("ma200", 0) or 0)
    except Exception as e:
        logger.warning(f"SPY 获取失败: {e}")

    # Voting coordinator (LLM 投票)
    try:
        voting_coordinator = container.voting_coordinator()
    except Exception:
        voting_coordinator = None

    # Battle plan manager (作战计划)
    battle_plan_manager = None
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        plan_path = os.path.join(PROJECT_DIR, 'data', 'daily_battle_plan_us.json')
        if mgr.load(plan_path):
            battle_plan_manager = mgr
    except Exception as e:
        logger.warning(f"作战计划加载失败: {e}")

    # Earnings guard
    earnings_guard = None
    try:
        from app.risk.earnings_risk_guard import EarningsRiskGuard
        earnings_guard = EarningsRiskGuard()
    except Exception:
        pass

    # Lesson injector
    lesson_injector = None
    try:
        lesson_injector = container.lesson_injector()
    except Exception:
        pass

    # Position store — use USPositionSync if available, otherwise None (signal stage handles None)
    position_store = None
    try:
        from app.core.position_sync import USPositionSync
        position_store = USPositionSync()
    except Exception:
        pass

    # Execution engine (只读, 不下单)
    execution_engine = container.execution_engine()

    return {
        "data_router": data_provider,
        "vix": vix,
        "spy_context": spy_ctx,
        "risk_engine": risk_engine,
        "earnings_guard": earnings_guard,
        "voting_coordinator": voting_coordinator,
        "battle_plan_manager": battle_plan_manager,
        "position_store": position_store,
        "execution_engine": execution_engine,
        "lesson_injector": lesson_injector,
        "notifier": notifier,
    }


def setup_observer_tracker():
    """设置 Observer 追踪器 — 记录所有 EventBus 事件到内存"""
    from app.pipeline.observers import get_event_bus
    bus = get_event_bus()
    events_captured = []

    def capture(event):
        events_captured.append({
            "type": event.event_type,
            "timestamp": event.timestamp,
            "symbol": event.data.get("symbol", "?"),
            "keys": list(event.data.keys()),
        })

    bus.subscribe("*", capture)
    return events_captured


def run_shadow_verification():
    """核心验证逻辑"""
    from app.core.di_container import get_di_container
    from app.pipeline.stages.runner import (
        PipelineRunner, ModularStage, StageConfig,
        PipelineResult, StageResult, _load_stages_config,
    )
    from app.core.pipeline_contracts import SymbolContext, SignalCandidate, OrderRequest
    from app.pipeline.stages import market_data_stage, signal_stage, risk_stage

    container = get_di_container()
    shared_state = get_live_shared_state(container)

    # 打印共享状态摘要
    print("\n" + "=" * 70)
    print("  FTSeek-US 新管线 Shadow Verification")
    print(f"  时间: {datetime.now().isoformat()}")
    print(f"  VIX: {shared_state['vix']:.1f}")
    print(f"  SPY: ${shared_state['spy_context'].get('spy_price', 0):.2f}")
    print(f"  SPY MA200: ${shared_state['spy_context'].get('spy_ma200', 0):.2f}")
    print(f"  RiskEngine: {'✓' if shared_state['risk_engine'] else '✗'}")
    print(f"  VotingCoordinator: {'✓' if shared_state['voting_coordinator'] else '✗'}")
    print(f"  BattlePlan: {'✓' if shared_state['battle_plan_manager'] else '✗'}")
    print("=" * 70)

    # 初始化 Observer 追踪
    events = setup_observer_tracker()

    # 注册真实 Observer (不推送到钉钉/飞书 — notifier 存在但验证模式下不阻塞)
    from app.pipeline.observers import setup_observers
    observers = setup_observers(
        notifier=None,  # 不推送, 避免实盘干扰
        lesson_injector=shared_state["lesson_injector"],
    )
    logger.info(f"Observer 已注册: {len(observers)} 个")

    # 构建管线
    stages_config = _load_stages_config()
    stages = [
        ModularStage("market_data", market_data_stage.process),
        ModularStage("signal", signal_stage.process),
        ModularStage("risk", risk_stage.process),
    ]
    runner = PipelineRunner(stages, stages_config=stages_config, shared_state=shared_state)

    # ── 逐标的执行 ──
    results = {}
    for symbol in TEST_SYMBOLS:
        print(f"\n{'─' * 60}")
        print(f"  ▶ {symbol}")
        print(f"{'─' * 60}")

        t0 = time.monotonic()
        result = runner.run(symbol)
        elapsed = (time.monotonic() - t0) * 1000
        results[symbol] = result

        # 逐 Stage 输出
        for sr in result.stages:
            status = "✓" if sr.success else "✗"
            output_summary = _summarize_output(sr.output)
            print(f"  [{status}] {sr.stage_name:15s} {output_summary:50s} {sr.elapsed_ms:6.0f}ms")
            if sr.error:
                print(f"       ERROR: {sr.error}")

        # 最终结果
        if result.aborted:
            print(f"  ⊘ 管线中止: {result.abort_reason}")
        elif result.final_output:
            o = result.final_output
            print(f"  ★ 最终输出: OrderRequest qty={o.quantity} sl={o.stop_loss:.2f} "
                  f"tp={o.take_profit_levels[0]['price'] if o.take_profit_levels else 'N/A':.2f} "
                  f"({elapsed:.0f}ms total)")
        else:
            print(f"  ○ 无输出 (被拦截/无信号) ({elapsed:.0f}ms total)")

    # ── Observer 事件审计 ──
    print(f"\n{'=' * 70}")
    print(f"  EventBus 事件审计 ({len(events)} events captured)")
    print(f"{'=' * 70}")
    event_types = {}
    for e in events:
        t = e["type"]
        event_types[t] = event_types.get(t, 0) + 1
    for t, count in sorted(event_types.items()):
        print(f"  {t:25s}: {count}")

    # ── 验证报告 ──
    print(f"\n{'=' * 70}")
    print(f"  验证报告")
    print(f"{'=' * 70}")

    checks = []

    # Check 1: 所有 Stage 模块可导入
    checks.append(("import", True, "所有 Stage 模块 + 契约 + Observer 导入正常"))

    # Check 2: 至少一个标的跑通了完整 3-stage
    complete = sum(1 for r in results.values() if len(r.stages) >= 3 and all(s.success for s in r.stages))
    checks.append(("3-stage-chain", complete > 0,
                   f"完整 3-stage 链路: {complete}/{len(TEST_SYMBOLS)} 标的通过"))

    # Check 3: SymbolContext 产生
    ctx_count = sum(1 for r in results.values()
                    for s in r.stages if s.success and isinstance(s.output, SymbolContext))
    checks.append(("SymbolContext", ctx_count > 0,
                   f"SymbolContext 产出: {ctx_count}/{len(TEST_SYMBOLS)}"))

    # Check 4: SignalCandidate 产生
    sig_count = sum(1 for r in results.values()
                    for s in r.stages if s.success and isinstance(s.output, SignalCandidate))
    checks.append(("SignalCandidate", True,  # 即使为 0 也不算失败 (可能无信号)
                   f"SignalCandidate 产出: {sig_count}/{len(TEST_SYMBOLS)} (0=无信号也是合法结果)"))

    # Check 5: OrderRequest 产生
    ord_count = sum(1 for r in results.values() if r.final_output is not None)
    checks.append(("OrderRequest", True,
                   f"OrderRequest 产出: {ord_count}/{len(TEST_SYMBOLS)} (可能被风控拦截)"))

    # Check 6: EventBus 事件已产生
    checks.append(("EventBus", len(events) > 0,
                   f"EventBus 产生 {len(events)} 个事件"))

    # Check 7: PipelineResult 结构完整
    pipeline_complete_events = [e for e in events if e["type"] == "pipeline_complete"]
    checks.append(("pipeline_complete", len(pipeline_complete_events) > 0,
                   f"pipeline_complete 事件: {len(pipeline_complete_events)} 个"))

    # Check 8: 配置 SSOT 加载
    checks.append(("config-SSOT", len(stages_config) == 3,
                   f"YAML pipeline.stages 加载: {len(stages_config)} stage 配置"))

    for name, passed, detail in checks:
        status = "✓" if passed else "✗"
        print(f"  [{status}] {name:20s} {detail}")

    all_pass = all(c[1] for c in checks)
    print(f"\n  {'✓ 全部验证通过' if all_pass else '✗ 存在验证失败项'}")
    return all_pass


def _summarize_output(output):
    from app.core.pipeline_contracts import SymbolContext, SignalCandidate, OrderRequest
    if output is None:
        return "(None — 被拦截)"
    if isinstance(output, SymbolContext):
        return f"SymbolContext(price=${output.price:.2f})"
    if isinstance(output, SignalCandidate):
        return f"SignalCandidate({output.direction} s={output.strength})"
    if isinstance(output, OrderRequest):
        return f"OrderRequest(qty={output.quantity})"
    return f"{type(output).__name__}"


if __name__ == "__main__":
    success = run_shadow_verification()
    sys.exit(0 if success else 1)
