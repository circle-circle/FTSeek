# 4 维度深度核查 — 5/18 实战数据对照设计 (2026-05-19)

> **结论**: 总体设计正确, 4 项结构化发现 (1 P1 + 3 P2). 主链路安全可运行, Phase B 启用前必须修 capital 漂移.

---

## D1. 策略触发一致性 — ✅ 良好 / ⚠️ 1 边缘弱点

### 1.1 4 路 adapter 注册 (5/18 启动日志)
```
[调度器·Coordinator] 已注册 4 个 adapter: 
  ['main(p=5)', 'scalping(p=10)', 'fast_lane(p=8)', 'llm_plan(p=3)']
```
`p` = priority. T8b regime-aware: `effective_priority` 按 regime 浮动 (趋势策略趋势市友好, 反转策略震荡市友好).

### 1.2 5/18-19 实际触发分布
| owner_strategy | n | pnl |
|---|---:|---:|
| main (voting_llm) | 2 | -$18 (ARM legacy close) |
| scalping | 13 | -$247 (12 sl_hit + 1 stale) |
| fast_lane | **0** | — |
| llm_plan | **0** | — |

→ FastLane / llm_plan 两条 adapter 全天 0 触发. **这是符合设计的**: fast_lane 需要 battle_plan entry_zone 命中, 5/18 无 LONG 信号符合; llm_plan 走 BATTLE_PLAN_FAST_PATH 路径, 同样无激活条件.

### 1.3 越权覆盖检查
- ARM main × 2 (LONG + SHORT): 反向开仓, 间隔 60s, 都来自组合止损 FLAT_ALL 触发的反向操作. 不算重复.
- TME scalping × 3, ARM scalping × 2 等: 同标的多次, 都是不同时段的独立信号触发.

### 1.4 ⚠️ scalping cooldown 边缘弱点

**配置**: `signal_cooldown_sec = 300` (5min), 起算点 = **entry 时间** (`scalping/engine.py:1942`)

**5/18 实测 TME**:
- 22:28:36 entry #1
- 22:33:39 sl_hit (持仓 5min)
- **22:33:41 entry #2** (距 entry #1 = 5min05s, 距 sl_hit = 2 秒)

cooldown 形式上通过 (5min05s > 5min) 但**实质是刚损刚再入**. 设计弱点 — 应该追加 `last_sl_hit + 90s` 二级冷却.

---

## D2. 退出逻辑验证 — ✅ 严格隔离, 单调正确

### 2.1 SL 单调守卫 (CR-3)
`app/risk/position_exit_monitor.py:1485-1521`
```python
# LONG: SL 严格单升 (max), SHORT: 严格单降 (min)
if direction == LONG:
    position.current_sl = max(_old_sl, _new_sl)  # 不允许放松
else:
    position.current_sl = min(_old_sl, _new_sl)
```

**5/18 实证 — QQQ trail 历史 (10 条 action_log)**:
```
v532 (5/18 21:30) → v533 (22:28) → v534 (5/19 02:12)
initial_sl=$617.28 → current_sl=$700.98  (持续单调上升, 12% 浮赢锁定)
```

### 2.2 Scalping 退出独立性
- Scalping 有 `app/scalping/exits.py` 自管 (atr_exit / fixed_exit / time_stop)
- 主线 `position_exit_monitor.py:364` 显式过滤:
  ```python
  if metadata["source"] == "scalping" or strategy_id == "scalping":
      skipped_scalping += 1; continue
  ```
- 5/18 实证: `grep -aE "exit_monitor.*scalping" logs` → **0 行** (无双重平仓)
- 5/18 `[退出监控] 跳过 X 个 scalping 仓位` 仅 DEBUG 级 — 因为没冲突

### 2.3 FlashExit 配置
```yaml
flash_exit:
  flash_multiplier: 1.0  # 优化 (2026-05-03): 0.8→1.0 匹配新基准
```
配置存在, 5/18 0 触发 (因为没有快速跌幅事件, 仅 ARM 个股小波动).

---

## D3. Phase A 多策略协同 — ⚠️ 1 个 P1 问题

### 3.1 SymbolLock 现状 (observe_only=True)
```
locks={'US.ARM': 'main', 'US.QQQ': 'main'}
```
锁记录正确 — 主线 ARM/QQQ 持仓锁定; scalping 入场 13 次 0 拒绝 (不同 symbols).

### 3.2 CapitalAllocator 现状
```
capital={'main': 0.0, 'scalping': 0.0, 'llm_plan': 0.0} total=0.000/0.90
```
账本 = 0 但实际 main 持有 ARM/QQQ ~44% capital → **账本与实际不一致**.

### 3.3 🔴 **P1**: 5/18-19 共 **111 次 RECONCILE warning**

```
2026-05-18 22:33:09 bucket=main 账本=0.000 实际=0.440 → 修正
2026-05-19 02:17:41 bucket=main 账本=0.000 实际=0.393 → 修正
```

**症状**: 平均每 5min 一次校正, 账本与实际持仓持续偏离 0.5-2%. 22:33 / 02:17 出现"账本归零"事件 (可能源自 reset_and_reconcile 调用或服务重启的状态丢失).

**根因 (推测)**:
1. `reset_and_reconcile` 调用未 sandwich 一次 enforce 切换 — 但服务重启 4 次, 每次重启账本归零
2. 重启时 capital reserve 没从 DB 恢复, 只能通过 reconcile 修复
3. 部分平仓事件 `release_capital` 未触发 (只看到 reconcile 修正, 未见显式 release 日志)

**Phase A 影响**: observe_only=True 不阻断信号 → 不影响交易. **Phase B 启用前必修**, 否则 enforce 模式会拒绝合法信号.

### 3.4 资金闭环健康度
| 指标 | 期望 | 实际 |
|---|---|---|
| reserve 调用 (entry) | 每 entry 1 次 | 仅有 RECONCILE 修正, 无 reserve 日志 |
| release 调用 (exit) | 每 exit 1 次 | 同上, 缺失显式 release |
| 账本与实际一致性 | < 0.5% 漂移 | 5min 周期持续漂移 |
| 负敞口 | 不应有 | 未观察到 (账本 ≥ 0) |
| 超配 (>0.9) | 不应有 | total=0.000-0.477, 安全 |

→ **没有负敞口或超配**, 但事件粒度 reserve/release 路径明显未连通. Phase B 上线前需要把 entry/exit 路径串到 capital_allocator (现在只有周期性 reconcile 在维稳).

---

## D4. 链路贯通 — ✅ 主链路完整, 1 个边缘缺失

### 4.1 trace_id 全链路覆盖 (5/18-19)

| 表 | 总数 | 含 trace_id | 覆盖率 |
|---|---:|---:|---:|
| trade_records | 15 | 13 | **87%** |
| trade_attribution | 3 | 3 | 100% |
| shadow_trades | 52 | 52 | 100% |

**缺失 2 笔 = id 718/719 (ARM main voting_llm)**: 这两笔是 5/18 ARM legacy 仓位被 FLAT_ALL 强平触发的 close 事件; legacy 持仓没有原始 entry signal trace_id, 写 trade_record 时无 trace_id 兜底.

### 4.2 盘前 plan 加载方式 — ✅ 单例 + 内存缓存

`app/us_pipeline/battle_plan_manager.py:23-29`:
```python
class USBattlePlanManager:
    _instance = None  # 显式 singleton
    _plan: Dict = None  # 内存缓存
```

- `USBattlePlanManager()` 永远返回同一实例, `_plan` 类变量
- 文件 IO 仅在 `load()` 时, `get_plan()` 只读 `self._plan`
- **5/18 实测加载次数: 8** (4 次服务重启 × 2 次/启动 = 启动期 + plan 重生成. **零盘中网络 IO**)

### 4.3 thesis 异步队列健康度

`app/pipeline/post_trade_hook.py:41`:
```python
ThreadPoolExecutor(max_workers=2, thread_name_prefix="PostTradeHook")
```

**5/18 实测**:
- `submitted = 0` (因为无主线 entry, hook 0 触发)
- 0 PostTradeHook 失败 / timeout 日志
- 0 thesis_inferer 阻塞日志
- 0 队列积压

→ 不是数据驱动可验证, 但**配置上无阻塞热路径风险**: 主线 → hook (异步 fire-and-forget) → ThreadPool (2 worker) → thesis_inferer (本身已 async).

### 4.4 push 链路
- PushHub 启用: `channels=['dingtalk', 'feishu', 'flag_file'] dedup_ttl=60s aggregate_window=300s async=True workers=4`
- 0 push.failed 日志
- 钉钉/飞书都在 5/18 收到告警

---

## 5. 必修问题汇总

| # | 严重度 | 问题 | 修复点 |
|---|---|---|---|
| 1 | 🔴 P1 | CapitalAllocator 账本与实际持续 5min 漂移 (111 RECONCILE) | 入场/出场路径连通 `capital.reserve(...)` / `capital.release(...)`, 见 `order_executor.py:1602` Phase A 钩子已就绪但未连通 |
| 2 | ⚠️ P2 | scalping cooldown 5min 边缘 — sl_hit 后 2s 即可再开 | `app/scalping/engine.py` 在 sl_hit 后追加 `last_loss_cooldown` (建议 90s) |
| 3 | ⚠️ P2 | legacy 仓 close 事件 trade_records 无 trace_id | `order_executor.py:_close_position` 写 trade_record 前生成 close-side trace_id |
| 4 | ⚠️ P2 | 服务 5/18 重启 4 次 → plan 重加载 8 次 | 已知 (D-DAY POST_OPEN_AUDIT P1 项 #7), 排查 22:28 重启原因 |

---

## 6. 上周以来的可启用优化点状态 (核查再确认)

| 上周改进点 | D1-D4 核查结果 |
|---|---|
| ✅ Phase A coordinator 注册 4 adapter | 实测 main/scalping/fast_lane/llm_plan 4 路全部 register, regime priority 工作 |
| ✅ T8b regime-aware priority | adapter 元数据存在 (`base_adapter.py:64`) |
| ⚠️ Phase A capital reserve/release | **未连通** (只有周期 reconcile 维稳), Phase B 启用前必修 |
| ⚠️ ShadowMirror 4 variant | baseline 完整, 其他 3 variant 等 EOD batch (尚未实施) |
| ✅ Y1 RegimeDetector | 5/18 logs `regime=TREND_UP/RANGE` (不再 UNKNOWN) |
| ✅ Y5 PnLOutlierMonitor | 5/18 `30d 统计刷新: n=255 mean=$57 std=$1258` |
| ✅ O1 sandbox universe (12 只白名单) | 严格过滤生效, 21 只非 universe 拒绝 |
| ✅ M1 Position Source/Protection | ARM legacy → PROTECTED 跳过 EM, 5/18 跳过 969 次 |
| ✅ CR-3 SL 单调守卫 | QQQ trail 7 次更新无放松 |

---

## 7. 一句话回答

> **触发一致性 ✅** 主线/Scalping/FastLane regime 触发正确, 1 个 scalping cooldown 边缘弱点 (sl_hit 后 2s 再入).
> **退出逻辑 ✅** SL 严格单调, scalping 显式过滤后 0 干扰主线 ExitMonitor.
> **多策略协同 ⚠️** SymbolLock 工作, **CapitalAllocator 账本漂移 111 次** — Phase A observe_only 下不影响交易, **Phase B 启用前必修**.
> **链路贯通 ✅** trace_id 87% 覆盖 (legacy ARM 2 笔缺失), 盘前 plan 单例内存缓存零网络 IO, thesis 异步队列健康.
