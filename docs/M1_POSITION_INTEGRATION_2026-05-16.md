# M1 + M1.1 多源持仓集成 (2026-05-16)

> **角色**: 量化仓位生命周期与多源交易集成架构师
> **铁律**:
> 1. 不修改现有信号生成与核心交易逻辑
> 2. 所有外部头寸必须带 source 标签, 默认 PROTECTED 隔离
>
> **回归**: 181/181 全绿 (135 旧 + 18 PushHub + 28 M1 新)
> **代码量**: 835 行 (5 模块 + 1 CLI)

---

## 1. 现状问题 (重构前)

| 问题 | 实证 |
|----|----|
| 用户手动开仓后系统**自动加止损** (隐式接管) | `position_sync.py:467-477` 新发现持仓直接 `INSERT INTO open_positions` 带 ATR 止损 |
| Source 字段散落字符串 | `'MANUAL' / 'futu_sync' / 'main' / 'voting_llm' / 'broker_recovery' / 'simulation'` 6+ 命名 |
| 无 takeover/release 显式开关 | 用户无法暂停/接管单标的 |
| 风险敞口未审计跨 source | `pre_trade_check` 计算 sector 限额时基于 `_current_positions`, 但未区分 source |
| 启动期无强制对账 | 启动后 30s 才发现 broker_only 持仓, 这 30s 内可能错配仓 |

---

## 2. 新模块设计

```
app/positions/                        (5 文件, 673 行)
├── __init__.py                       (33 行) 公开 API
├── source.py                        (135 行) PositionSource + ProtectionState enum
├── protection.py                    (180 行) ProtectionController takeover/release/freeze
├── reconciler.py                    (204 行) StartupReconciler 启动对账
└── risk_aggregator.py               (121 行) RiskAggregator 跨 source 敞口

scripts/position_takeover.py         (162 行) CLI 工具
```

### 核心枚举

**PositionSource** (7 值):
- `AUTO_MAIN` / `AUTO_SCALPING` / `AUTO_FAST_LANE` / `AUTO_LLM_PLAN` — 策略产生
- `MANUAL` — 用户在 broker app 手动开仓
- `LEGACY` — 系统启动前已存在的历史仓
- `UNKNOWN` — 兜底

**ProtectionState** (3 值):
- `PROTECTED` (默认 for MANUAL/LEGACY) — 策略禁止 close/add/move_sl
- `MANAGED` (默认 for AUTO_*) — 策略可全权管理
- `FROZEN` — 任意状态显式冻结 (例: 财报前)

**默认行为**:
```python
ProtectionState.default_for(PositionSource.MANUAL)     # → PROTECTED
ProtectionState.default_for(PositionSource.LEGACY)     # → PROTECTED
ProtectionState.default_for(PositionSource.AUTO_MAIN)  # → MANAGED
```

---

## 3. 启动强制对账 (`StartupReconciler`)

**触发**: `main.py` 启动期一次性, 在 `start_us_position_sync` 之后:
```python
from app.positions import run_startup_reconcile, get_protection_controller
get_protection_controller(position_store=scheduler.position_store)
result = run_startup_reconcile(scheduler.position_store, broker)
```

**算法**:
1. `_fetch_broker_positions()` — 拉真实持仓 (复用 USPositionSync.fetch_futu_positions)
2. `_fetch_local_positions()` — 拉内存 PositionStore.all()
3. **三路分类** (集合差):
   - `broker_only` (broker 有 + 本地无) → `_add_legacy_position` 自动补录, 标 `LEGACY + PROTECTED`. **不擅自加止损** (initial_sl=0).
   - `local_only` (本地有 + broker 无) → `_mark_ghost` 标 `is_ghost=true` (30s position_sync 接力清理)
   - `matched` (两侧都有) → 检查 source 标签, UNKNOWN → 补 `LEGACY` 标
4. 启动 banner 打印对账摘要

**关键设计**: `broker_only_added` 不加止损, 因为用户可能有自己的退出计划. 想让策略接管必须显式 `takeover`.

---

## 4. Takeover/Release/Freeze 控制 (`ProtectionController`)

```python
ctrl = get_protection_controller()
ctrl.takeover("US.NVDA", "用户决定接管", actor="user")  # PROTECTED → MANAGED
ctrl.release("US.NVDA", "策略表现差, 用户接手", actor="user")  # MANAGED → PROTECTED
ctrl.freeze("US.NVDA", "财报前 24h 冻结", actor="cron")  # → FROZEN
ctrl.unfreeze("US.NVDA", target_state=ProtectionState.PROTECTED)
```

**审计**: 每次变更写 `decisions.db.position_protection_log`:
```
id | symbol | from_state | to_state | reason | actor | timestamp
```

---

## 5. 集成: ExitMonitor 默认隔离

**`position_exit_monitor.py:304`** 增加 M1 过滤:
```python
for p in positions:
    # 1. scalping 自管 (C2 已存在)
    if metadata.source == "scalping": skip
    # 2. M1: PROTECTED/FROZEN 由用户管
    if not is_strategy_managed(p):
        skipped_protected += 1
        logger.info(f"[退出监控·M1·跳过] {p.symbol} ...")
        continue
    main_positions.append(p)
```

**效果**: 用户手动开 NVDA 后, 系统发现 → 标 LEGACY+PROTECTED → ExitMonitor 看到 protected → 跳过, 不会自动加 SL.

**接管路径**: 用户跑 `python scripts/position_takeover.py takeover US.NVDA "决定让策略接管"` → MANAGED → 下一轮 ExitMonitor 加 SL.

---

## 6. 跨 source 风险敞口 (`RiskAggregator`)

```python
agg = RiskAggregator(position_store)
snap = agg.snapshot(equity=1_076_604)
# RiskExposureSnapshot:
#   total_market_value=484_412
#   by_source={'auto_main': 473_034, 'legacy': 11_377}
#   by_protection={'managed': 473_034, 'protected': 11_377}
#   protected_locked_value=11_377   ← 策略不可挤占
#   strategy_available_value=1_065_227
#   long_value=484_412 short_value=0
```

**关键**: `get_strategy_budget_pct(equity, total_cap_pct)` 返回**策略可分配预算 = 总预算 - 受保护占用**. 防策略挤占用户手动仓的额度.

---

## 7. CLI 工具 (`scripts/position_takeover.py`)

```bash
# 列出全部持仓
python scripts/position_takeover.py list

# 风险敞口快照 (跨 source)
python scripts/position_takeover.py snapshot

# 接管单标的
python scripts/position_takeover.py takeover US.NVDA "决定让策略接管"

# 释放
python scripts/position_takeover.py release US.NVDA

# 冻结 (临时)
python scripts/position_takeover.py freeze US.NVDA "财报前停操作"

# 解冻
python scripts/position_takeover.py unfreeze US.NVDA
```

**实测 snapshot 输出** (5/16 运行中数据):
```
风险敞口快照 (跨 source)
总名义价值: $484,412.00
持仓数:     2
按方向:
  LONG  : $484,412.00
  SHORT : $    0.00
按 source:
  main                : $473,034.74 ( 97.7%)
  simulation          : $ 11,377.26 (  2.3%)
```

**CLI 限制**: 跨进程, 无法直接修改运行中服务的内存. 写 audit log + 重启服务生效. 未来 M2 加共享内存 IPC.

---

## 8. 测试覆盖 (28 用例)

```
test_positions_m1.py:
  PositionSource enum     × 7 (from_str 各类映射 + is_auto)
  ProtectionState 默认    × 3 (auto/manual/legacy)
  tag_position 写入        × 2
  get_position_source 优先级 × 3 (metadata/owner/strategy_id fallback)
  is_managed/is_protected × 3
  ProtectionController    × 5 (takeover/release/freeze/unknown/list)
  StartupReconciler       × 3 (broker_only/matched/local_only)
  RiskAggregator          × 3 (跨 source/long_short/budget)
```

---

## 9. 已落地清单 vs 任务要求

| 要求 | 状态 |
|----|:---:|
| 1. 启动强制对账, broker_only 自动补录 + source 标 | ✅ `StartupReconciler` |
| 2. 风险敞口跨 source 全量 | ✅ `RiskAggregator.snapshot()` |
| 3. 手动/历史仓默认保护 | ✅ `ProtectionState.default_for(MANUAL/LEGACY) = PROTECTED` |
| 3. takeover/release 开关 | ✅ `ProtectionController` + CLI |
| 3. freeze 临时冻结 | ✅ `ProtectionController.freeze` |
| 4. source_id, entry_reason, risk_group | 🟡 source 已有, entry_reason/risk_group 已有 (trade_attribution.signal_tags), 显式 risk_group 暂留作 metadata |
| 5. 外部成交触发同步事件非阻塞 | 🟡 现有 30s `position_sync` 已是非阻塞, M1 启动期一次性补强 |

---

## 10. 推迟项 (诚实清单)

| 项 | 推迟原因 | 触发条件 |
|----|----|----|
| **M2 IPC** CLI 修改运行中服务 metadata | 当前需重启生效, 改用 SQLite poll + 30s sync 轮询读 protection_log | 高频接管/释放需求 |
| **risk_group 显式字段** | 已通过 `metadata.risk_group` 实现, 但未在 Position dataclass 提升为 first-class | 风险分组归因需求成熟时 |
| **ExitDispatcher 与 protection 集成** | 当前 ExitMonitor 入口已过滤, ExitDispatcher 独立路径未加同样守门 | Phase B enforce 时一并 |
| **pre_trade_check 加 protection 守门** | 防策略对 PROTECTED 仓位加仓 | 实测加仓需求时 |
| **shadow mode 严格隔离验证** | ShadowMirror 已写 shadow_trades 表, 但未与 protection 联动 | shadow vs real 报告显示数据混淆时 |

---

## 11. 多想一步

### Q1: 为什么不直接改 Position dataclass 加 `source: PositionSource` 字段?
**A**: 改 Position 影响 50+ 文件 (反序列化/DB schema), 风险大. 走 `metadata` dict 兼容性最好, `get_position_source(p)` 容错读多个字段优先级.

### Q2: 为什么 startup reconcile 标 LEGACY 不标 MANUAL?
**A**: 启动时无法区分"用户昨晚操作"和"系统重启前已有". 全标 LEGACY 更保守, 用户决定接管时再选具体类别. 实证: 5/16 凌晨 broker 端 ARM (broker_recovery) 应该是 LEGACY.

### Q3: 为什么不把 30s position_sync 整合进来?
**A**: 30s sync 是运行时持续, reconcile 是启动期一次性. 职责分明: sync 同步常态变化, reconcile 校准启动状态. 合并会让逻辑变复杂.

### Q4: ProtectionController 单例, CLI 跨进程怎么办?
**A**: 当前限制. CLI 写 audit log + 重启服务生效. M2 改 SQLite poll 共享状态 (positions 表加 protection 列, 30s sync 读取并 apply 到内存). 但当前实测一周仅 1-2 次接管需求, 重启可接受.

### Q5: 如果用户手动平仓我系统补录的 LEGACY 仓位怎么办?
**A**: 30s position_sync 会发现 broker 端消失 → `local_only` 路径标 ghost → 下一轮清理. 此时若有 SL 监控 (PROTECTED 状态没加 SL), 不会重复触发平仓.

---

## 12. 5/16 实际验证

```bash
# 当前 broker 端实际
$ python scripts/position_takeover.py snapshot
总名义价值: $484,412
持仓数: 2
  US.QQQ  LONG  728  futu_sync  MANUAL    ← 应识别为 MANUAL
  US.ARM  LONG  54   broker_recovery SIMULATION  ← 应识别为 LEGACY
```

**重启服务后预期**:
1. `[Reconciler] 启动对账完成 broker_only=2 (标 LEGACY + PROTECTED)`  
   (因为 reconciler 启动时 PositionStore 内存空, 全部视为 broker_only)
2. ExitMonitor 跳过这 2 笔 (PROTECTED) — 用户责任
3. 用户决定接管时: `python scripts/position_takeover.py takeover US.QQQ "我让策略接管"`

---

## 13. 回滚

```bash
# M1 完全回滚 — 删模块即可, 现有 30s position_sync 仍工作
rm -rf app/positions/
# 移除 main.py 启动对账块 (5 行)
# 移除 position_exit_monitor.py M1 过滤块 (10 行)
# 注: 删除后所有 broker_only 持仓回到 ExitMonitor 接管
```

---

## 14. 整体修复时间线

| 阶段 | 主题 | 代码量 |
|-----|----|----|
| 5/15 R1-R4 | PreTrade SoT |  |
| 5/16 O1-O7 | 数据驱动修复 (sandbox/shadow/time_stop) |  |
| 5/16 T1-T6 | trace_id + factor_* + lock + reconcile |  |
| 5/16 T8b/T9b/PnL | regime priority + LLM factors |  |
| 5/16 F1-F3 | whipsaw + partial fill + cooldown SoT |  |
| 5/16 PushHub | 统一推送 + 聚合 + 防疲劳 | 794 行 |
| 5/16 R1+R2 | SymbolLock 泄漏 + PushHub 异步 |  |
| **5/16 M1** | **多源持仓集成 + 保护状态机** | **835 行** |

---

**结论**: M1 多源持仓集成 5 模块 + 1 CLI + 28 测试 + 文档落地. 默认 PROTECTED 隔离用户手动仓, 显式 takeover 才让策略接管. 启动期一次性对账, 不破坏现有 30s position_sync 运行时同步. 跨 source 风险敞口聚合 + 策略可用预算计算. 181/181 全绿.

下一步: `systemctl restart ftseek-us` 让 M1 生效, 然后 `python scripts/position_takeover.py snapshot` 验证.

---

## 15. M1.1 补丁: 智能 source 推断 (Adoption)

### 15.1 用户反馈触发的设计漏洞

**场景**: QQQ 自动交易仓位, 5/3 入场, 浮盈 +7.9%, ExitMonitor 已 chandelier 保利 SL 到 $700.98 (entry $649.77).
但 trade_records 0 行 (元数据丢失), trader_type='MANUAL', strategy_id='futu_sync'.

**M1 原行为 (一刀切)**: trader_type=MANUAL → 标 LEGACY+PROTECTED → ExitMonitor 跳过 → **保利的 SL 失效**.

**用户痛点**: 真正策略产生的孤儿仓被误识别为手动仓, 失去监控.

### 15.2 修复: 4 层线索综合推断

**`Reconciler._infer_source(symbol, position)` 优先级**:
1. **trade_records 历史**: 有入场记录 → 用 owner_strategy/source_type/strategy_id 反推 → MANAGED
2. **action_log_json 痕迹**: 含 `exit_monitor` 或 `update_trail` → AUTO_MAIN + MANAGED (ExitMonitor 已在管的硬证据)
3. **strategy_id 启发式**: 排除 `futu_sync/broker_recovery` 同步标记, 真策略名 → MANAGED
4. **兜底**: LEGACY + PROTECTED

### 15.3 实证 (5/16 真实数据)

```
=== QQQ 真实推断结果 ===
source: auto_main          ← 通过 action_log_json 识别
protection: managed
is_auto: True
✅ 正确识别为策略孤儿 → ExitMonitor 续命监控

=== ARM 真实推断结果 ===
source: auto_scalping      ← 通过 trade_records 识别
protection: managed
```

### 15.4 启动 banner 升级

```
[Reconciler] 启动对账完成
  Broker 持仓: 2 | 本地内存: 0
  matched         : 0
  broker_only     : 2 (智能推断)
  ↳ adopted       : 2 (策略孤儿 → MANAGED, ExitMonitor 续命)
  ↳ legacy        : 0 (LEGACY/MANUAL → PROTECTED)
  local_only      : 0
  errors          : 0
  🤖 adopted 仓位 (识别为策略孤儿): ExitMonitor 续命监控, 已有 trail/SL 不变.
```

### 15.5 CLI 新增 `adopt` 命令

```bash
# 主动声明某仓位为策略孤儿 (即使推断逻辑没识别)
python scripts/position_takeover.py adopt US.QQQ "M1.1 推断兜底失败时人工接管"
```

### 15.6 测试覆盖 (新增 6 用例)

```
test_infer_source_from_trade_records_main      # 线索 1
test_infer_source_from_action_log              # 线索 2
test_infer_source_strategy_id_heuristic        # 线索 3
test_infer_source_skip_futu_sync               # 排除同步标记
test_infer_source_legacy_fallback              # 兜底
test_qqq_real_scenario_adopted                 # QQQ 真实数据回放
```

### 15.7 多想一步

**Q: 为什么不直接默认所有 broker_only 都 MANAGED?**
A: 那就丢失了"用户手动仓默认隔离"的铁律. 真有用户在 broker app 开仓时, 系统不该擅自加 SL.

**Q: action_log_json 是 ExitMonitor 内部数据, 用作推断证据合理吗?**
A: 合理. action_log 是行为证据 (而非声明证据). 如果系统过去几天明明在管这仓位, 重启后还认为它是用户手动 — 自相矛盾.

**Q: 如果用户在 broker app 操作了一个原策略仓 (减仓), 推断会出错吗?**
A: 不会. 推断只看 source 标签, 减仓不改变 source. 仓位仍 MANAGED, 策略继续按新 size 监控.

**Q: 反向场景: 系统识别错了, 把用户手动仓识别为策略仓?**
A: 几乎不可能 — 用户手动仓不会有 trade_records 入场记录, 也不会有 action_log_json 监控痕迹. 但兜底有 CLI release 命令: `python scripts/position_takeover.py release US.X "我手动开的"`.

### 15.8 187/187 全绿 (M1 28 + M1.1 6 + 其他 153)
