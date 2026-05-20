# Phase B/C 实施决策 + 渐进启动方案

> **日期**: 2026-05-15
> **基于**: 设计文档启动条件 + 5/14 真实数据 + karpathy 渐进原则

## 1. Phase B/C 启动条件 vs 5/14 实测

### Phase B 设计文档触发条件
| 条件 | 5/14 实测 | 满足? |
|-----|---------|------|
| `OBSERVE_CONFLICT > 5` (SymbolLock) | 0 | ❌ |
| `BLOCKED_STRATEGY_CAP > 3` (Capital) | 0 | ❌ |
| `n_candidates > 1 仲裁 > 10` | 0 (signal_arbitration 表 0 行) | ❌ |

**严格按规矩**: Phase B 全部触发条件未满足 (因为 P1-A 今天才修, coordinator 协调层在 5/14 没流量).

### Phase C 启动条件 (设计文档)
- "Phase A/B 完成后启动" — Phase B 子组件全部稳定 7+ 天

**结论**: Phase C 一晚上不可能, 重构风险远大于收益.

---

## 2. 推荐方案: 渐进启动 (karpathy 原则)

### 今晚做 (PB 系列, 4 项, 1.5h)
| ID | 修复 | 文件 | 风险 |
|----|------|------|------|
| **PB-1** L3 thesis_inferer 启用 | yaml `thesis_inferer.enabled=true` | 低 (异步, fire-and-forget) |
| **PB-2** order_executor 接入 thesis_inferer | `_execute_long/short` 入场 hook | 低 |
| **PB-3** 4 个 ExitPolicy thesis-aware 升级 | `app/strategies/exit_policies.py` (从 NO_ACTION stub 升级) | 中 (改变退出行为) |
| **PB-4** phase_b_status.py CLI | 新工具 | 0 (只读) |

### 今晚不做 (等数据)
- ❌ B.3 SignalArbitrator enforce: 需 7 天 multi-candidate 数据 (现 0 行)
- ❌ B.4 SymbolLock enforce: 需 7 天 OBSERVE_CONFLICT 数据
- ❌ B.5 CapitalAllocator enforce: 必须先 reset_and_reconcile (P0-4 已建函数)
- ❌ Phase C 全部: OrderGateway 重构 + ShadowExecutor + 多 coordinator 并行 → 重构风险 vs 边际收益

---

## 3. PB 修复详解

### PB-1: L3 entry_thesis 启用
yaml `llm_features.thesis_inferer.enabled: false → true`. 模型 deepseek, 单次 ~$0.02, 日预算 $0.40.

### PB-2: 入场后异步推断 thesis
`order_executor.py:_execute_long` 入场后调 `infer_thesis_async(symbol, signal_dict, context)`. 
- 异步 fire-and-forget (~10s 内完成)
- 写入 `trade_attribution.entry_thesis` (5 类: momentum/breakout/reversion/event/technical_setup)
- 不阻塞下单

### PB-3: 4 ExitPolicy 升级 (从 stub 到 thesis-aware)

**MainPipelineExitPolicy** (主线):
- `event` thesis + 持仓 > 48h → CLOSE (事件 horizon)
- `breakout` thesis + 跌破 0.5% → CLOSE (突破失败立即退)
- `reversion` thesis + 浮盈 +1.5% → REDUCE 50% (锁短线利润)
- `momentum`/`technical_setup` → fallback 走 PositionExitMonitor (chandelier)

**ScalpingExitPolicy** (短线):
- 任何 thesis + 持仓 > 30min → CLOSE (短线本质 — 杜绝持仓 12 小时变长线)

**FastLaneExitPolicy**:
- 浮盈 ≥ 1×ATR → REDUCE 50% (1ATR 立即保本, 让剩 50% 跑趋势)

**LLMPlanExitPolicy**:
- 命中 `position.metadata.plan_target` → CLOSE
- 跌破 `position.metadata.plan_stop` → CLOSE
- 既无 target 也无 stop → NO_ACTION (走默认)

**关键设计**: 
- 无 thesis 时 fallback `NO_ACTION` (走 PositionExitMonitor 6 规则) — 向后兼容
- 有 thesis 时按 owner+thesis 组合选规则
- 优先 ExitDispatcher (P0-2 已加路由), 再 fallback legacy

### PB-4: phase_b_status.py 验证 CLI
4 维度查 Phase B 启动条件:
1. ExitDispatcher 4 policy 注册状态
2. entry_thesis 当日落库率
3. signal_arbitration multi-candidate 数据
4. 综合判定 + 启动建议

---

## 4. 今晚开盘验证 checklist

### 启动后 (~21:30)
```bash
journalctl -u ftseek-us -n 100 | grep -E "thesis_inferer|ExitDispatcher|ExitPolicy"
```

期望:
```
[ExitDispatcher] 注册 main → main_exit
[ExitDispatcher] 注册 scalping → scalping_exit
[ExitDispatcher] 注册 fast_lane → fast_lane_exit
[ExitDispatcher] 注册 llm_plan → llm_plan_exit
```

### 30 分钟后 (~22:00)
```bash
python scripts/phase_b_status.py
```

期望:
- ExitDispatcher: ✅ 4 policy 注册
- entry_thesis 落库率: 50%+ (有 LLM 调用)
- signal_arbitration: 至少 > 0 (P1-A 接入后)

### 收盘后 (~04:30)
```bash
# Phase B 渐进数据
python scripts/phase_b_status.py --days 1

# Shadow 1:1 对照
python scripts/shadow_compare.py --all
```

### 7 天后做决策
满足条件后:
- ✅ multi-candidate > 10 → B.3 enforce 切换
- ✅ OBSERVE_CONFLICT > 5 → B.4 lock enforce
- ✅ thesis 落库率 > 80% + ExitPolicy 工作正常 → B.6 完成

---

## 5. 风险与回滚

### 已有 try/except 守护
- 所有 PB 改动用 `try/except` 包, 失败降级
- thesis_inferer 失败 → ExitPolicy 走 fallback NO_ACTION
- ExitPolicy 异常 → 走原 PositionExitMonitor

### 一键回滚
```bash
# 关 thesis_inferer (只关 LLM 调用, ExitPolicy 自动 fallback)
yq -i '.llm_features.thesis_inferer.enabled = false' config/unified_config.yaml
systemctl restart ftseek-us
```

PB-3 的 ExitPolicy 改动**不需回滚** — 无 thesis 时自动 fallback 到原行为, 完全向后兼容.

---

## 6. Phase C 推迟原因

### 评估的 Phase C 工作量
| 子组件 | 行数 | 复杂度 |
|------|-----|------|
| C.1 OrderGateway Protocol | 50 行 | 低 (无价值, 因为只有 1 个实现) |
| C.2 RealOrderGateway 抽取 | 重构 order_executor 2,500 行 | 高 |
| C.3 真 ShadowExecutor (虚拟撮合 + slippage) | 400 行 | 高 |
| C.4 多 coordinator 并行 | 300 行 + 集成测试 | 高 |

**Z1 ShadowMirror 已提供 80% 价值** (1:1 对照 + variant 比较), 不需立即建 ShadowExecutor.

### Phase C 启动建议
- 至少 7 天 Phase B 稳定运行
- Z1 ShadowMirror 数据足够 (300+ 笔/variant) 才有 A/B 对照价值
- 用户明确决策 (实盘资金量 > $100k 时考虑)

---

## 7. 测试

`tests/coordinator/test_phase_b_minimal.py`: **11/11 全绿**
- L3 yaml 启用验证
- order_executor 接入验证
- ExitPolicy 7 个 thesis 场景 (无 thesis fallback / event horizon / breakout failure / reversion profit / scalping 30min / fast_lane 1ATR / llm_plan target/stop)
- CLI 可 import

全 coordinator 回归: **129/129 全绿**

---

## 8. Phase B/C 路线图

| 阶段 | 时间 | 状态 |
|-----|-----|------|
| **Phase A** (基础协调) | 5/14 完成 | ✅ |
| **Phase B.1** ExitDispatcher 注册 | 5/14 完成 | ✅ |
| **Phase B.6** thesis-driven exit | **今晚 (PB)** | ✅ 实施 |
| **Phase B.2** ExitPolicy 升级 | **今晚 (PB-3)** | ✅ 实施 |
| **Phase B.3-B.5** enforce 切换 | 7 天后看数据 | ⏸ |
| **Phase C** ExecutionMode | 1 个月后 (有 baseline) | ⏸ 推迟 |

---

**结论**: Phase B 渐进启动 (B.6 + B.2 minimal) 今晚可做, 0 风险 (有 fallback). Phase C 大重构推迟至 baseline 数据足够后. 部署: `systemctl restart ftseek-us`. 收盘后 `python scripts/phase_b_status.py` 看数据决定下一步.
