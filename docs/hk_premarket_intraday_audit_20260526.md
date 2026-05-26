# 港股盘前计划 × 盘中联动 × 午休更新 — 深度审计报告

> **日期**: 2026-05-26 | **作者**: Hermes Agent
> **范围**: 盘前计划生成 → 盘中交易门禁 → 午休计划刷新 全链路

---

## 一、当前架构链路图

```
08:20  PreMarketDebateScheduler → premarket_debate.json (多模型辩论)
  ↓
08:30  PreMarketScheduler → PreMarketPipeline → assessments[]
  ↓       ↓
  ↓     BattlePlanGenerator.generate_from_assessments()
  ↓       ↓ + debate_synthesis (08:20+08:30融合)
  ↓       ↓ + AuctionManager (竞价验证)
  ↓       ↓
  ↓     daily_battle_plan.json ← SSOT
  ↓
09:00  BattlePlanExecutionService.execute_auction_orders()
  ↓       → 竞价限价单 (09:00-09:12)
  ↓
09:30  盘中交易开始
  ↓
  信号 → consensus_engine → risk_pipeline → TradeExecutor.execute()
                                              ↓
                                         Phase 2: BattlePlanManager.check_open_trade()
                                              ↓
                                         ① restricted 检查 → 拦截
                                         ② targets 检查 → 放行/拦截
                                         ③ direction 检查 → 方向匹配
                                         ④ 高置信度例外通道
  ↓
12:00  MiddayPlanRefresher.run() (cron 12:15 HKT)
  ↓     → _scan_volume_leaders() (量比>3x)
  ↓     → _scan_southbound_focus() (南向资金)
  ↓     → _scan_position_drift() (持仓浮亏>5%)
  ↓     → _run_midday_debate() (LLM辩论)
  ↓     → 合并到 daily_battle_plan.json
  ↓     → BattlePlanManager 热重载
  ↓     → MiddayRecommendationCheck (risk_pipeline)
  ↓
13:00  下午交易继续 (使用更新后的计划)
```

---

## 二、发现的问题 (按严重度排序)

### 🔴 P0: Targets vs Restricted 重叠 — 3只标的自我封锁

**现象**:
```
Targets:    {HK.00700, HK.09988, HK.01347, HK.02269, HK.01211}
Restricted: {HK.01211（比亚迪股份）, HK.02269（药明生物）, HK.01347（华虹半导体）}
重叠:       {HK.01347, HK.02269, HK.01211} ← 3/5 标的!
```

**根因**: `BattlePlanGenerator.generate_from_assessments()` 中:
- `restricted` 从 `avoid_codes` + `signal_impact=="block"` 生成
- `targets` 从 assessments 的 `focus_symbols` 生成
- **两者独立生成，无交叉去重**

**影响**: `check_open_trade()` 先检查 restricted (line 208)，3只标的被永久拦截，即使它们在 targets 中。

**修复方案**:
```python
# battle_plan_generator.py — 生成 restricted 后，从 targets 中移除
# 或在 check_open_trade 中 targets 优先于 restricted
```

---

### 🔴 P0: 作战计划缺乏交易参数 — 白名单而非作战计划

**现象**: 所有5只标的配置均为空:
```
HK.00700: dir=ANY status=VALID urgency=High max_pos=? entry=? sl=? tp=?
HK.09988: dir=ANY status=VALID urgency=Low  max_pos=? entry=? sl=? tp=?
...
```

**缺失字段**:
- `entry_zone`: 无入场价格区间 → 无法指导竞价定价
- `stop_loss`: 无止损价 → 依赖全局 -2.5% 止损
- `take_profit`: 无止盈价 → 依赖全局 +8% 止盈
- `max_position_pct`: 无仓位上限 → 使用默认 8%
- `direction`: 全为 ANY → 无方向指导

**根因**: `BattlePlanGenerator` 从 LLM assessments 提取 focus_symbols，但 assessments 不包含具体价格参数。竞价验证 (`AuctionManager`) 只更新 status，不填充参数。

**影响**: 作战计划退化为简单的"允许/禁止"名单，丧失了精细化交易指导能力。

---

### 🟡 P1: 午间刷新从未执行

**现象**: `midday_refreshed_at: never`, `midday_added: []`

**根因**: 
- 午间刷新已迁移到 Hermes cron (12:15 HKT)，但 `midday_plan_refresher.py` 中的 `run()` 方法需要 `subscriber` 和 `quote_ctx` 参数
- cron job 中可能缺少这些依赖注入
- `MiddayAnalyst` 是另一个独立的午间分析模块，与 `MiddayPlanRefresher` 功能重叠

**影响**: 下午交易仍使用盘前计划，无法捕捉午间量价异动和A股收盘信息。

---

### 🟡 P1: 两套午间分析模块并存

| 模块 | 触发方式 | 功能 |
|------|----------|------|
| `MiddayPlanRefresher` | cron 12:15 | 量比扫描 + 南向资金 + 持仓异动 + LLM辩论 → 更新计划 |
| `MiddayAnalyst` | cron 12:15 | 上午交易数据 + 市场快照 → LLM分析 → midday_update.json |

两者独立运行，产出不同文件，但都尝试注入 `BattlePlanManager`。可能导致竞态条件。

---

### 🟡 P1: 高置信度例外通道门槛过低

```python
# 当前配置 (模拟盘宽松)
override_consensus_threshold: 1.0  # 共识分数 ≥ 1.0
override_agree_count: 2            # 策略同意数 ≥ 2
override_mtf_alignment: 0.0        # MTF对齐度 ≥ 0%
override_volume_ratio: 1.1         # 量比 ≥ 1.1x
```

**问题**: MTF对齐度=0 意味着不要求多时间框架对齐，量比1.1x几乎无过滤。这导致计划外标的太容易绕过门禁。

---

### 🟢 P2: 辩论共识方向缺失

`debate_consensus` 中 `consensus_direction` 和 `risk_level` 为空，`conviction` 仅 0.5。盘前辩论质量不足以驱动计划生成。

---

## 三、优化建议

### P0 修复: Targets-Restricted 去重 (🟢 可自主)

**文件**: `app/pipeline/battle_plan_generator.py`
**改动**: 在生成最终计划时，从 targets 中移除 restricted 中的标的

```python
# 在 save 前添加:
restricted_clean = {r.split('（')[0].strip() for r in restricted}
targets = [t for t in targets if t['symbol'] not in restricted_clean]
```

**5问**:
1. F1 (数据链路) — 消除门禁逻辑冲突
2. 改什么: 减少3只被错误拦截的标的
3. 验证: SQL查询 `check_open_trade` 日志
4. 最坏: 无 (仅移除冲突)
5. 回滚: `git revert`

---

### P0 修复: check_open_trade 优先级调整 (🟢 可自主)

**文件**: `app/pipeline/battle_plan.py`
**改动**: 当标的同时在 targets 和 restricted 中时，targets 优先

```python
# 在 restricted 检查前:
target = next((t for t in plan.targets if t.get('symbol') == symbol), None)
if target and target.get('status') == 'VALID':
    # targets 中且有效 → 跳过 restricted 检查
    pass
elif symbol in plan.restricted:
    return False, f"🚫 [黑名单拦截]"
```

---

### P1 优化: 作战计划填充交易参数 (🟡 需记录)

**方案**: 在 `BattlePlanGenerator` 中接入 `DynamicExitManager` 和 `ATR` 数据:
- `entry_zone`: 从竞价数据派生 (昨日收盘 ± ATR)
- `stop_loss`: 从 `dynamic_exit_manager.get_stop_loss()` 获取
- `take_profit`: 从 `dynamic_exit_manager.get_take_profit()` 获取
- `direction`: 从辩论共识 + assessments 方向投票获取

---

### P1 优化: 统一午间分析入口 (🟡 需记录)

**方案**: 将 `MiddayAnalyst` 和 `MiddayPlanRefresher` 合并为单一入口:
```
MiddayOrchestrator.run():
  1. MiddayAnalyst 分析 → midday_update.json
  2. MiddayPlanRefresher 刷新 → daily_battle_plan.json
  3. 统一通知推送
```

---

### P2 优化: 信号→计划反馈闭环

**当前缺失**: 盘中交易结果不反馈到计划系统。
**方案**: 
- 上午止损触发的标的 → 午间自动加入 restricted
- 上午盈利标的 → 午间自动提升 urgency
- 连续亏损标的 → 触发 symbol_cooldown

---

## 四、数据验证

### 计划执行率

| 指标 | 值 | 评价 |
|------|-----|------|
| 计划标的数 | 5 | 偏少 |
| 被restricted覆盖 | 3/5 (60%) | 🔴 严重冲突 |
| 有效可交易标的 | 2 (HK.00700, HK.09988) | 严重不足 |
| 午间刷新次数 | 0 | 从未执行 |
| 高置信度例外触发 | 待查 | — |

---

## 五、总结

| 维度 | 当前评分 | 目标 | 差距 |
|------|----------|------|------|
| 计划生成 | ⭐⭐⭐ (有辩论融合) | ⭐⭐⭐⭐⭐ | 缺交易参数 |
| 计划门禁 | ⭐ (restricted/targets冲突) | ⭐⭐⭐⭐ | P0 bug |
| 盘中联动 | ⭐⭐ (有check_open_trade) | ⭐⭐⭐⭐ | 无反馈闭环 |
| 午休更新 | ⭐ (从未执行) | ⭐⭐⭐⭐ | 依赖注入缺失 |
| 午间推荐 | ⭐⭐⭐ (有risk_check) | ⭐⭐⭐⭐ | 依赖午间刷新 |

**核心结论**: 
1. **P0 targets-restricted 重叠是最致命的 bug** — 60% 的计划标的被自我封锁
2. **作战计划缺乏交易参数** — 退化为简单白名单
3. **午间刷新从未执行** — 下午交易完全依赖盘前信息
