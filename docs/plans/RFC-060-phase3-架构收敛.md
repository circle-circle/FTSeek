# RFC-060 Phase 3: 架构收敛

> **阶段目标**: 消除「组件丰富、集成不足」。统一并行风控、收敛启动面条、建立真 SSOT、复活死监控。
> **核心命题**: 本阶段**不直接提升盈利**，但消除技术债让前两阶段的成果可维护、让后续 Alpha 迭代不被陈旧架构拖累。
> **预计周期**: 2-3 周
> **总纲**: 见 `RFC-060-TRADING-OPTIMIZATION-PROGRAM.md`
> **深度**: 📋 提纲（待进入实施时按 phase1 模板细化）
> **硬前提**: Gate 2→3 通过

---

## 阶段总览

| ID | Item | 收敛对象 | 关键问题 | 投入 | 状态 |
|----|------|---------|---------|------|------|
| P3.1 | 风控三套路径合一 | 仓位/敞口检查 | 三套并行阈值不一致 | 高 | ⬜ |
| P3.2 | main.py 启动管线声明化 | 启动序列 | 540 行面条 + 20+ 静默降级 | 中 | ⬜ |
| P3.3 | ConfigContainer 读写锁 | 配置热重载 | reload 非原子，读到半初始化 | 中 | ⬜ |
| P3.4 | 消除配置四源 | SSOT | YAML/env/CFG/类常量并存 | 中 | ⬜ |
| P3.5 | TradingMonitor 复活 | 实时监控 | record_trade 零调用 + 硬编码本金 | 低 | ⬜ |

> **建议顺序**: P3.5（低、立即收益）→ P3.3（P3.4/P2.5 前置）→ P3.2 → P3.4 → P3.1（最大、放最后）

---

## P3.1 风控三套路径合一 🔴

### 目标
当前三套仓位/敞口检查并行存在，阈值来源不同（一套 ETF 折扣、一套无折扣、一套 TradingMode 缩放），导致**误拦截或误放行**。统一为单一主路径。

### 关键问题
| 系统 | 文件 | 问题 |
|------|------|------|
| 旧链路 | `app/risk/engine.py:139-296` `pre_trade_check()` | 含 ETF 折扣的仓位检查，含很多重叠逻辑 |
| Pipeline | `app/risk/checks/position_limit_check.py:29` | 简化版仓位检查 |
| 下单前闸门 | `app/risk/pre_trade_gate.py:246` | 按真实股数 + 硬编码 `DEFAULT_POSITION_PCT=0.20`（实际仓位远小于此 → 检查形同虚设）|

### 方案方向
- 以 `USRiskPipeline.run()` 为唯一入口
- 把 `pre_trade_gate` 的真实股数检查合并为 Pipeline 的第 10 个 check（接收已计算的股数）
- 废弃 `RiskEngine.pre_trade_check()` 的仓位检查（保留作日志对比，或整体退役）
- 统一 ETF 折扣 / TradingMode 缩放到一个 shared utility

### 预期收益
- 消除阈值不一致导致的误拦截/误放行
- 减少 30%+ 风控代码维护成本
- 让 `CapitalAllocator`（当前 `observe_only=True`）有机会从观察→强制

### 依赖
- P2 完成（通电后的系统行为更可预测，再做路径合并才安全）

### 待细化项
- [ ] 三套阈值的具体差异表（需详查 ETF 折扣/TradingMode 系数）
- [ ] 废弃旧路径的灰度策略（双写对比 N 天后切流）
- [ ] CapitalAllocator 切 enforce 的灰度

---

## P3.2 main.py 启动管线声明化 🟡

### 目标
`main.py:_run_new_scheduler()` 是 **540 行单体函数 + 20+ 个 try/except「降级运行」**，关键安全组件（配置治理/持仓同步/对账）也在静默降级。改为声明式阶段管线，critical 阶段失败即退出。

### 关键问题
- `main.py:432-433`：配置治理降级（三层防御可静默消失）
- `main.py:543-544`：持仓同步器降级（幽灵仓）
- `main.py:562`：启动对账降级（持仓不一致）
- `main.py:641`：TradingMonitor 降级（无实时监控）
- 步骤间隐式依赖（position_sync → reconcile → manual_hold → run_forever 顺序耦合）

### 方案方向
```python
phases = [
    Phase("preflight",  [opend_check, trade_env_check, schema_migration], critical=True),
    Phase("core",       [di_container, scheduler_build],                  critical=True),
    Phase("config_gov", [validator, drift_detector, hot_reloader],         critical=True),
    Phase("position",   [startup_reconcile, position_sync, manual_hold],   critical=True),
    Phase("observe",    [heartbeat, trading_monitor, attribution],        critical=False),
    Phase("aux",        [emergency_interceptor, news_v2, orb_engine],      critical=False),
]
```
- critical=True 失败 → 立即退出（不静默降级）
- critical=False 失败 → 记入 `IssueRegistry`，心跳中暴露

### 预期收益
- 故障可见（不再静默吞噬）
- 启动顺序依赖显式化

### 依赖
- 无强依赖；P3.5 TradingMonitor 复活后，`observe` 阶段才有意义

### 待细化项
- [ ] 哪些阶段标 critical（需逐项确认安全边界）
- [ ] IssueRegistry 的持久化 + 心跳暴露格式

---

## P3.3 ConfigContainer 读写锁 + 原子 reload 🟡

### 目标
当前 `reload()` 不是原子操作（重置 `_initialized=False` 再 `__init__`），多线程读配置可能读到半初始化状态。`HotReloader` 重载时与 `DriftDetector` snapshot 可能竞争，产生「假漂移」。

### 关键问题
- `app/core/config_container.py:492-495` `reload()` 非原子
- `_config_dict`（扁平）和 `_yaml_raw`（嵌套）双字典，`get()` vs `get_dotted()` 可能返回不同值
- 无读写锁

### 方案方向
```python
def reload(self):
    new_dict, new_raw = self._load_into_temp()   # 临时对象全量加载
    with self._lock:                              # 锁内一次性 swap
        self._config_dict = new_dict
        self._yaml_raw = new_raw
```
- 加 `threading.RLock` 保护读写

### 预期收益
- 消除热重载竞态（配置撕裂）
- **是 P2.5（进化写 YAML）和 P3.4（配置合一）的前置依赖**

### 依赖
- 无

### 待细化项
- [ ] 性能验证（读锁高频，确认无瓶颈）
- [ ] `get()` / `get_dotted()` 双字典是否也要统一（见 P3.4）

---

## P3.4 消除配置四源（真 SSOT）🟡

### 目标
当前配置至少 4 个来源，虽有审计（`config_audit.py`）但仅检测不统一：
1. `unified_config.yaml`（设计上的 SSOT）
2. 环境变量（`os.getenv`，如 `STOP_LOSS_PCT`）
3. `config_defaults.CFG`（第三源，`config_audit` 正是检测它与 YAML 漂移）
4. 类级常量（如 `scheduler_core.py:155` 的 `SCAN_INTERVAL_MINUTES`）

### 方案方向
- `config_defaults.CFG` 全部迁入 `unified_config.yaml`，删除 CFG 类
- 环境变量仅作 YAML 中 `${VAR}` 占位符展开源（`_expand_env_vars` 已有）
- 类级常量仅作开发注释，运行时全走 YAML
- `config_audit.py` 增加「YAML-only」断言：关键参数不在 YAML 即 BLOCKING

### 预期收益
- 真 SSOT，消除漂移风险
- 简化 `config_audit` 的「YAML vs CFG」对照逻辑

### 依赖
- **P3.3**（读写锁就位后再做迁移更安全）

### 待细化项
- [ ] 四源逐项清单（哪些 key 在多源出现、值是否一致）
- [ ] 迁移的灰度验证（逐 key 切换 + audit 比对）

---

## P3.5 TradingMonitor 复活 🟡

### 目标
`TradingMonitor` 当前是**死代码**：`record_trade()` 从未被调用，`_check_pnl` 用硬编码 $10000 算百分比。让实时监控真正工作。

### 关键问题
| 位置 | 问题 |
|------|------|
| `app/monitoring/trading_monitor.py:297-299` | `record_trade()` 零调用 → `_metrics` 永远空 |
| `trading_monitor.py:224` | 硬编码 `$10000` 算 `daily_pnl_pct` → 告警阈值失真 |
| `trading_monitor.py` | `_check_pnl` 永远看到 daily_pnl=0 |

### 方案方向
- 在 `PostTradeHook`（`app/pipeline/post_trade_hook.py`）的 `on_position_closed` 回调调 `get_monitor().record_trade(pnl, is_winner)`
- 本金从 `capital_safety` 取真实净值（替换硬编码 10000）
- 验证 `_check_pnl` / `_check_heartbeat` 告警真实触发

### 预期收益
- 实时监控复活（PnL/PDT/熔断器）
- 是 P1.2 Kill Switch fail-close 的配套监控（连续 fail-close → 告警）

### 依赖
- 无；可与 P1.2 配套

### 待细化项
- [ ] `record_trade` 接入点（PostTradeHook 的具体回调位置）
- [ ] 与 `intraday_heartbeat` 的职责边界（避免重复告警）

---

## 阶段 3 实施状态表（迭代用）

| ID | Owner | PR/Commit | 部署日期 | 验收结果 | 状态 |
|----|-------|-----------|---------|---------|------|
| P3.1 | | | | | ⬜ |
| P3.2 | | | | | ⬜ |
| P3.3 | | | | | ⬜ |
| P3.4 | | | | | ⬜ |
| P3.5 | | | | | ⬜ |

---

## 阶段 3 完成定义（Definition of Done）
- [ ] P3.1~P3.5 全部 `✅`
- [ ] 风控只剩 1 条主路径，废弃路径代码已删除（非注释）
- [ ] `main.py` critical 阶段失败可见（退出而非静默降级）
- [ ] TradingMonitor 连续 3 日有真实告警/通过记录
- [ ] `config_audit` 无 YAML vs CFG 漂移
