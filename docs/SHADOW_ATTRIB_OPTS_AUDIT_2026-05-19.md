# 影子账号 + 归因 + 上周优化点核查 (2026-05-19)

> **范围**: ShadowMirror / trade_attribution / 5/14 - 5/17 共 9 系列优化的实际启用状态
> **核心结论**: 6/13 已启用且工作; 2/13 设计黑洞 (永远不会触发); 5/13 部署但 0 触发 (因 5/18 无主线 entry)

---

## 1. 影子账号 (ShadowMirror) 现状

### 1.1 5/18 影子镜像数据
```
strategy        n_total  n_closed  pnl     胜率
baseline        13       10        -$247   0%
no_chandelier   13       0         $0      —
quick_exit      13       0         $0      —
tighter_stop    13       0         $0      —
```

**症状**: 4 variant × 13 笔 = 52 镜像入场; 但仅 baseline 完成平仓, 其他 3 variant 全 `closed=0 pnl=0`.

### 1.2 根因 — 设计黑洞

`app/evolution/shadow_mirror.py:163-174`:
```python
# 非 baseline 变体: EOD 由 batch 任务回放, 此处只标记 real 已平
else:
    detail["real_exit_price"] = exit_price
    # ...只更新 signal_detail JSON, 不平仓
```

注释承诺 "EOD batch 回放", 但**全代码库 0 个 EOD batch 任务**:
- 无 `scripts/shadow_replay.py`
- 无 `scheduler_triggers.py` 中的 EOD 钩子
- 仅有 `scripts/shadow_compare.py` (CLI 查看, 不写)

→ **3 个非 baseline variant 永远停留 closed=0**, 失去 A/B 对比价值.

### 1.3 daily_review 看到的"假对比"
```json
"shadow_comparison": {
  "real": {"n": 5, "total_pnl": -46.34, "win_rate": 0.0},
  "shadow_by_variant": [{"variant": "baseline", "n": 3, "total_pnl": -28.15}]
}
```
SQL 是 `WHERE closed=1` → 永远只看到 baseline. **过去 5 天的影子对比统计全部失效**.

### 1.4 ShadowMirror.mirror_entry 在 5/18 0 触发

| 触发点 | 5/18 期望 | 5/18 实际 |
|---|:---:|:---:|
| 主线 entry (`order_executor.py:1597`) | 0 (无信号通过) | 0 ✅ 一致 |
| Scalping entry (`scalping/engine.py:2161`) | 13 | 待查 (52 条镜像 = 13 × 4 variant, 看起来 OK) |

→ Scalping 路径触发了 mirror_entry. 主线没触发, 因为 13 个 SHORT 信号全部 PRE_TRADE_CHECK / SHORT_STRENGTH_WEAK 拒掉.

---

## 2. trade_attribution 归因记录

### 2.1 5/18 覆盖率
```
trade_records 5/18:        15 笔
trade_attribution 5/18-19: 3 笔   (20% 覆盖率)
```

### 2.2 写入 3 笔的来源
全部来自 **scalping** owner_strategy:
- US.ARM scalping × 2
- US.TME scalping × 1

scalping 的归因走 `order_executor.py:1497` 的 `INSERT OR REPLACE INTO trade_attribution` (新路径).
主线/voting_llm 走 PostTradeHook → `_insert_trade_attribution` (line 2054).

### 2.3 缺口
- 主线 voting_llm: 0 attribution (因为无新 entry)
- 12 笔 scalping 中 9 笔无 attribution — 入场路径条件分支未覆盖
- 历史 39 条 attribution 全部 entry_time 为 NULL (脏数据)

---

## 3. 上周 (5/14 - 5/17) 优化点启用状态总览

### 3.1 已启用且工作正常 (6 项 ✅)

| 优化项 | 文档 | 5/18 证据 |
|---|---|---|
| **O1: Sandbox universe 严格过滤** | OPTIMIZATION_2026-05-16 | 日志: `[O1·SANDBOX] polling_controller 已严格过滤: 注册 12 只 (过滤 21 只非 universe)` |
| **Phase A: Coordinator 多策略协调** | T1_T6_DELIVERY | 日志: `║ Coordinator Phase A — 多策略协调层启动` + `capital={'main': 0.0, 'scalping': 0.0, 'llm_plan': 0.0}` |
| **Y1: Market Regime Detector** | AGENT_REVIEW_AND_Y_FIXES | 日志: `regime=TREND_UP/TREND_DOWN/RANGE` (不再 UNKNOWN) |
| **Y5: PnL Outlier Monitor** | AGENT_REVIEW_AND_Y_FIXES | 日志: `[PnLOutlierMonitor] 30d 统计刷新: n=255 mean=$57 std=$1258` |
| **O3: time_stop 禁用 / atr_stop 接管** | OPTIMIZATION_2026-05-16 | yaml 验证 `time_stop.enabled=False, atr_stop.enabled=True`; TME -$153 由 ATR 止损触发 |
| **M1: PositionSource + ProtectionState** | M1_POSITION_INTEGRATION | 日志: `[退出监控·M1·跳过] US.ARM source=legacy protection=protected` |

### 3.2 部署但 5/18 0 触发 (5 项 ⏸ — 等触发条件)

| 优化项 | 触发条件 | 5/18 实际 |
|---|---|---|
| **PostTradeHook (T1+O2)** | 主线 entry | stats={submitted:0, completed:0, failed:0} |
| **ShadowMirror.mirror_entry 主线** | 主线 entry | 0 (一致, 因 0 主线 entry) |
| **LLM thesis_inferer** | 主线 entry close | 0 (依赖 PostTradeHook) |
| **LLM premarket_planner** | 03:00 PT 盘前调度 | 0 触发 (待查调度) |
| **LLM regime_classifier / weekly_proposer / earnings_analyst** | 各自盘后/周末时机 | 0 触发 (本批为盘后 feature, 5/18 收盘后 daily_review 走旧 agent_bridge) |

→ 这 5 项**功能在位**, 但需要主线 entry / 特定时机才会激活. 不算 BUG.

### 3.3 设计黑洞或重大不完整 (2 项 🔴)

| 优化项 | 黑洞 | 影响 |
|---|---|---|
| **ShadowMirror EOD batch 回放** | `shadow_mirror.py:163` 注释承诺 EOD batch 回放 non-baseline variant, 但**无任何 EOD 任务实施** | 3/4 variant 永远 closed=0, A/B 对比无效 |
| **owner_strategy 完整覆盖** | 5/15+ 29 笔 trade_records, 19 笔有 owner, **10 笔仍 NULL** (35% 缺口) | T4/T5 归因维度统计偏差 |

---

## 4. 今日 P0 修复 (5/19) — 是否需要等开盘验证?

| 修复 | 验证条件 |
|---|---|
| **P0-1: avg_entry_price SSOT 保护** | 周二开盘后 sync 1 次, 看 `[US Sync] {symbol}: local_cost=$X broker_cost=$Y (broker 漂移, 不覆盖 SSOT)` 不再出现 cost overwrite |
| **P0-2: sync tombstone (60s)** | 任何平仓后 60s 内 grep `[US Sync·tombstone] 跳过恢复` (没出现是好事) |
| **P0-3: PortfolioDrawdown 双重过滤** | 主线 entry 后, grep `[组合止损·trigger] action=... drawdown=...` (有 drawdown 数值即触发可观测) |
| **P0-4: PostTradeHook wired-up** | 重启后第 1 行: grep `✅ [PostTradeHook·wired]` (出现即通过) 否则 🔴 |

---

## 5. 下一步必修 (按价值排序)

### 必修 P0 (本周内)

1. **ShadowMirror EOD batch 任务**: 在 `app/core/scheduler_triggers.py` 的 04:00 ET 时段加 `replay_shadow_variants()` 钩子, 否则 Z1 修复名存实亡
2. **owner_strategy 兜底**: 主线/scalping 入场路径强制写 owner_strategy (现有 35% NULL); 已写 trade_records 的可用 SQL 补 (基于 strategy_id heuristic)

### 推荐 P1

3. **Scalping 摩擦预算门槛** (5/18 audit 提出): 单笔预期 PnL < 2× 摩擦时拒绝下单 — 5/18 13 笔 100% 摩擦吞利
4. **CB 熔断阈值修复**: gross PnL > -0.1% 不计入 consecutive_losses (避免 -0.0% 也算 sl_hit)

### 不阻塞 P2
5. **shadow attribution 也走 PostTradeHook 统一路径** (现在 scalping 走 1497, 主线走 2054, 路径分裂)
6. **LLM features 实测**: 等周一开盘后有真实主线 entry 触发, 验证 thesis_inferer / regime_classifier 实际 latency / cost

---

## 6. 一句话回答用户

> **影子账号**: baseline variant 数据正确 (10/13 closed), 但 3 个非 baseline variant **完全失效** — 设计的 EOD batch 任务从未实施, 这是上周 5/15 Z1 改造的黑洞, 不修则 A/B 对比永远没数据.
> **归因**: 5/18 仅 3/15 笔有 attribution (scalping 走对了路径), 主线和大部分 scalping 路径未覆盖 — owner_strategy 35% 仍 NULL 是结构性缺陷.
> **上周优化点**: 6 项已启用并工作 (sandbox/Phase A/regime/outlier/time_stop/M1); 5 项部署但等触发条件 (主线 entry); 2 项设计黑洞 (shadow EOD batch / owner_strategy 补全) 必须本周修, 否则改造投入空跑.
