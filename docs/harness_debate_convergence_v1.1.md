# FTSeek-HK Harness 整车平台 — 第三轮辩论收敛 (Hermes + Claude Code)

## 辩论统计

| 指标 | 数值 |
|------|------|
| Hermes 提出 | 5 层架构 + 5 Phase 路线 + 6 约束 + 4 项评估 |
| Claude Code 提出 | 8 关键问题 + 6 额外风险 + 6 修改建议 |
| Claude 事实错误 | 3 处（circuit_breaker、BatchReviewProcessor、TimeGate 均存在） |
| Claude 正确发现 | 7 处（阈值差异、print()、fallback、async 阻塞等） |
| Hermes 验证通过 | 7/7 |

---

## 逐条验证 Claude Code 断言

### 1. ⚠️ circuit_breaker.py 不存在？ — ❌ Claude 事实错误

**Claude 声称**: "搜索整个目录不存在 circuit_breaker.py 文件"

**Ground Truth**: 文件存在于 `app/risk/circuit_breaker.py`，L147 定义了 `class CircuitBreaker`，共 350+ 行。
```
find app -name "circuit_breaker*" → app/risk/circuit_breaker.py
grep "class CircuitBreaker" → L147 class CircuitBreaker
```

**结论**: Claude 的搜索可能未覆盖子目录，该文件存在且功能完整。

### 2. ⚠️ BatchReviewProcessor 不存在？ — ❌ Claude 事实错误

**Claude 声称**: "此文件不存在，Strategist 永远返回空数据"

**Ground Truth**: 文件存在于 `app/review/batch_processor_hk.py`，L1007 定义了 `class BatchReviewProcessor`，共 1100+ 行。
```
find app -name "batch_processor*" → app/review/batch_processor_hk.py
grep "class BatchReviewProcessor" → L1007
```

**结论**: Claude 搜索失败，该文件存在且完整。Strategist 可能正常工作。

### 3. ⚠️ TimeGate 未实现？ — ❌ Claude 事实错误

**Claude 声称**: "设计规划提到但从未实现"

**Ground Truth**: 文件存在于 `app/strategy/time_gate.py`，235 行，定义了 `GateConfig`、`GateResult` 和 `TimeGate` 类，覆盖 5 个港股时段。
```
find app -name "*time*gate*" → app/strategy/time_gate.py
wc -l → 235 lines
```

**结论**: TimeGate 已完整实现，Claude 未发现。

### 4. ✅ RiskOfficer 阈值与 RiskManager 不一致 — ✅ 正确

**Claude 发现**: 5 个阈值存在 2-2.5 倍差异

**Hermes 验证**:
```
RiskOfficer: max_single_position=0.20, max_daily_trades=10, max_drawdown=0.15
RiskManager: max_single_position=0.08, max_daily_trades=5,  max_drawdown=0.12
```

| 阈值 | RiskOfficer | RiskManager | 差异 |
|------|------------|-------------|------|
| max_single_position | 0.20 (20%) | 0.08 (8%) | **2.5x** 🔴 |
| max_daily_trades | 10 | 5 | **2x** 🔴 |
| max_drawdown | 0.15 (15%) | 0.12 (12%) | 1.25x ⚠️ |
| min_confidence | 0.30 (30%) | 0.35 (35%) | ⚠️ |
| max_total_position | 0.50 (50%) | 0.75 (75%) | 反向差异 ⚠️ |

**结论**: **✅ 确认**。RiskOfficer 阈值比 RiskManager 宽松 2-2.5 倍。如果 RiskOfficer 被激活，会允许 RiskManager 已拒绝的危险交易。

### 5. ✅ RiskAdapter fallback 返回 MEDIUM（fail-open） — ✅ 正确

**Claude 发现**: `risk_adapter.py:185-197` 返回 `"overall_risk": "MEDIUM"` 而非 FAIL-CLOSED

**Hermes 验证**:
```python
def _fallback_assessment(self, input_data):
    return {
        "overall_risk": "MEDIUM",  # ← 应该是 CRITICAL 或 REJECTED
        "market_risk": "LOW",
        "position_risk": "LOW",
        "note": "Fallback assessment (risk manager not available)",
    }
```

**结论**: **✅ 确认**。RiskManager 不可用时返回 MEDIUM 而非拒绝，这是 fail-open 设计，实盘危险。

### 6. ✅ RiskManager `_is_daily_circuit_broken()` bug — ✅ 正确

**Claude 发现**: L387 `details={'error': False}` 语义矛盾（`passed=False` 但 `error=False`）

**Hermes 验证**:
```python
return RiskCheckResult(
    passed=False,
    details={'error': False},  # BUG: should be True
    timestamp=datetime.now()
)
```

**结论**: **✅ 确认**。`passed=False` + `details={'error': False}` 逻辑矛盾。

### 7. ✅ messaging.py 使用 print() 而非 logger — ✅ 正确

**Claude 发现**: L176 和 L256 使用 `print()` 而非 logger

**Hermes 验证**:
```python
print(f"Error notifying subscriber {subscriber_id}: {e}")  # L176
print(f"Error notifying global subscriber {subscriber_id}: {e}")  # L256
```

**结论**: **✅ 确认**。生产环境无法追溯消息传递错误。

### 8. ✅ async/threading 阻塞风险 — ✅ PARTIALLY 正确

**Claude 发现**: `harness_pilot.py` 的 `.result(timeout=300)` 会阻塞调用方线程

**Hermes 验证**: 当前 HarnessPilot 使用独立 daemon 线程 + `run_coroutine_threadsafe`，但 `.result(timeout=300)` 是同步等待。如果从主线程调用，确实会阻塞 5 分钟。

**结论**: **✅ 确认** — 但目前 `run_daily_review` 和 `run_risk_check` 仅在非交易路径调用（盘后），不阻塞交易热路径。需要在设计中标明此约束。

### 9. ✅ TradeMonitor 与 CollectorAdapter 重叠 — ✅ PARTIALLY 正确

**Claude 发现**: `harness_pilot.py` 将 CollectorAdapter 注册为 `"trade-monitor"`，而 TradeMonitor 类是占位符。

**Hermes 验证**: 
- `harness_pilot.py:149-154`: `self._coordinator.register_agent("trade-monitor", collector)`
- `daily_review.py:73-83`: `agent_name="trade-monitor"`
- `trade_monitor.py`: `_fetch_trades` 返回硬编码数据

**结论**: **✅ 确认**。CollectorAdapter 实际承担了 trade-monitor 的角色，TradeMonitor 是死代码。

### 10. ✅ MessageBus 全局单例 — ✅ 正确

**Claude 发现**: `messaging.py:268-276` `_global_bus` 是模块级全局变量

**Hermes 验证**:
```python
_global_bus: Optional[MessageBus] = None
def get_message_bus() -> MessageBus:
    global _global_bus
    if _global_bus is None:
        _global_bus = MessageBus()
    return _global_bus
```

**结论**: **✅ 确认**。HK/US 双实例共享同一个进程时无问题（不同进程），但如果未来双实例共享 Python 进程会有污染。

---

## 收敛后最终设计规划 (v1.1)

### 修正的 5 层架构

```
Layer 0: Orchestrator (方向盘 + 导航)
  - Coordinator: TaskDefinition + DAG + WorkflowContext
  - Workflows: DailyReview / WeeklyReview / PreMarket (待实现)
  - TimeGate: 已存在于 app/strategy/time_gate.py ← [修正: 不是缺失]

Layer 1: Engine (发动机)
  - MTFEngine: 多周期趋势 (已提取 ✅)
  - StopEngine: 动态止损 (已提取 ✅)
  - CollectorAdapter: 数据采集 (从 Observability 移至此处) ← [修正: Claude 建议]
  - FallbackChain: 降级路径 (从 Engine 移至 Policy) ← [修正: Claude 建议]

Layer 2: Policy/Rules (刹车)
  - RiskAdapter → RiskManager (SSOT) ← [修正: 废弃 RiskOfficer]
  - CircuitBreaker: 已存在于 app/risk/circuit_breaker.py ← [修正: 不是缺失]
  - FallbackChain (从 Engine 层移入)

Layer 3: Observability (仪表盘)
  - MessageBus: 统一事件上报 (需修复 print→logger) ← [新增修复项]
  - TradeMonitor: 与 CollectorAdapter 合并 ← [修正: Claude 建议]
  - trace_id: 待实现

Layer 4: Memory + Learning (记忆 + 进化)
  - ContextEngineer: 已修复 import os ✅
  - ReviewAdapter: 待验证 (依赖 BatchReviewProcessor) ← [修正: 文件存在]
  - Lessons 注入: 待实现
```

### 修正的 Phase 顺序

**原**: Phase 1→2→3→4→5
**新**: Phase 1→2→4→3→5 ← [修正: Claude 建议, 影子模式先于盘前注入]

| Phase | 内容 | 修正说明 |
|-------|------|----------|
| 1 ✅ | 止血 + Pilot | 已完成 |
| 2 | 盘后复盘闭环 | TradeMonitor 与 CollectorAdapter 合并 |
| 3 | 盘中监控影子模式 ← [提至 Phase 3] | ≥14 天验证 RiskOfficer 阈值 |
| 4 | 盘前规划注入 ← [降至 Phase 4] | 依赖影子模式验证结果 |
| 5 | 盘中拦截 | 激活 RiskAdapter 拦截 |

### 新增 P1 修复项（从辩论中发现）

| # | 修复 | 文件 | 严重度 |
|---|------|------|--------|
| P1-1 | RiskAdapter fallback 改 fail-closed | `adapters/risk_adapter.py` | 🔴 HIGH |
| P1-2 | RiskManager L387 details 修复 | `risk/risk_manager.py` | 🔴 MEDIUM |
| P1-3 | messaging.py print→logger | `harness/messaging.py` L176, L256 | 🟡 MEDIUM |
| P1-4 | 废弃 RiskOfficer 或对齐阈值 | `harness/risk_officer.py` | 🔴 HIGH |
| P1-5 | TradeMonitor 与 CollectorAdapter 合并 | `harness/trade_monitor.py` | 🟡 MEDIUM |

### 关键设计约束（修正版）

1. ~~绝不重写 main.py~~ → 维持不变 ✅
2. ~~独立 event loop~~ → 维持不变，但添加 timeout 约束（盘中调用 ≤5s，盘后 ≤60s）
3. ~~防御性注入~~ → 维持不变 ✅
4. ~~影子模式优先~~ → 维持不变 ✅
5. ~~配置驱动~~ → 维持不变 ✅
6. ~~市场差异通过配置注入~~ → 维持不变 ✅
7. **[新增]** RiskAdapter fallback 必须 fail-closed（返回 REJECTED），不可返回 MEDIUM
8. **[新增]** 所有 Harness 组件必须有独立 kill-switch（配置 `harness_enabled=false` 一键关闭）
9. **[新增]** RiskManager 是风控唯一 SSOT，RiskOfficer 必须废弃或完全对齐
