# Agent-vs-Agent Debate: 美股自动化交易系统隐蔽风险审计（执行/风控/可观测/数据）

---

# 议题：FTSeek-US 在策略与因子层之外，是否存在用户与团队尚未意识到的系统性风险？

## 状态
- [x] 待辩论（本文档为审计方 briefing，供反方 agent 质疑/证伪/补充）

## 背景

用户提问："对于美股自动化交易，从策略、逻辑、框架、代码等方面，还有什么你没把握、我也没意识和注意到的问题？"

本审计由 4 个并行探查 agent 完成，覆盖：(1) 执行时序与订单逻辑；(2) 数据完整性与状态一致性；(3) 风险控制与资金安全；(4) 代码健壮性与可观测性。环境为富途**模拟盘**（v2 sim，无真实资金），当前分支 `rfc-060-phase1-hotfixes`。

**审计性质**：静态代码分析 + 数据文件状态观察，**未运行系统**。所有结论需反方验证代码现状是否仍成立（部分 hotfix 可能已修复）。

## 核心判断（一句话）

> 系统的**策略/因子层**做得相当细，但**执行层与可观测层**存在"看起来严密、实际断裂"的系统性风险。这些风险在模拟盘阶段大多不暴露，**切实盘后会集中爆发**。

---

## 七大盲区（按资金影响排序）

### 盲区 1【P0｜最致命】止损在 broker 端根本不存在

- **现象**：所有 `Order.stop_price` 仅作内部元数据，**从未作为 `aux_price`/`stop_limit`/`trailing_stop` 传给富途 OpenD**。系统没有"真实止损单"。
- **证据**：
  - `app/execution/dual_broker.py:226-233`（place_order 调用未传 stop 参数）
  - `app/pipeline/order_executor.py:2553`（`Order(stop_price=stop_loss, ...)` 仅内部字段）
  - 止损 100% 由 `monitor_playbook_sim_exits`（`app/playbook/pipeline.py:4278`）+ `position_monitor.scan_positions` 每 tick 轮询当前价 vs `initial_sl` 实现
  - `app/core/scheduler_core.py:175`：`MONITOR_INTERVAL_MINUTES = 0.5`（30 秒一次）
- **后果**：
  - 进程崩溃 / 容器 OOM / 断网 / OpenD 断开 / 深夜无人时段 → **止损完全真空**，隔夜风险无限大。
  - 30 秒 polling gap 内，闪崩 / LULD halt 复牌跳空可移动 5–30%，理论 2% 止损实际亏 10–30%。
  - 回测显示的"止损命中率"是理想值，实盘打 6–8 折。
- **反方需验证**：富途是否真的不支持 stop 单？还是支持但代码没用？若支持，这是可直接修复的 P0。

### 盲区 2【P0】市场熔断器看错了市场（只监控 HSI）

- **现象**：唯一的系统级市场熔断器**只监控恒生指数 HSI**，不看 SPY/VIX。这是美股系统。
- **证据**：`app/risk/market_circuit_breaker.py:35-36`（`hsi_drop_threshold = -0.05`），全文件逻辑围绕 `check_hsi_drop()`。
- **后果**：美股黑天鹅日（SPY 跌 5–10%，如 2020-03-09、2022-09-13）系统完全无感，继续按信号开仓，直到账户回撤 20–35%（CapitalSafety ORANGE/RED 水位）才止损。单日回撤可达 10–20%。
- **反方需验证**：是否存在其他美股熔断路径（VIX gate / SPY 监控）在别处？`order_executor.py` 中有 `_vix_circuit_breaker` 字样，需确认是否真生效。

### 盲区 3【P0｜最隐蔽】"风控门面"完整，但审计/告警链路是断的

- **现象**：异常被大量静默吞掉；真正的审计引擎在生产路径零调用。
- **证据**：
  - `app/pipeline/order_executor.py` 有 **160+ 处 `except Exception`**，关键下单/平仓/熔断路径异常多写 `logger.debug`（默认 INFO 级别不可见）。
  - 两套 `AuditLogger` 并存：`app/pipeline/observers.py:201-210`（仅 debug 日志，等同 NO-OP）；`app/observability/audit_logger.py:9-50`（真写文件，但**零业务调用**）。
  - `app/observability/engine.py:123-130` 的 `record_audit_event` **全代码库零调用**；`main.py:519-535` 启动时未实例化 `ObservabilityEngine`。
  - `app/observability/failure_tracker.py:6-38` 仅内存、且无业务喂数据 → `app/core/issue_registry.py` 自动升级链路空跑。
- **后果**：**"今天下过哪些单、为什么没下、为什么失败"在代码层面无法回答**。出事无法复盘，合规问询无法回放。这是元问题，放大盲区 1/2 的影响。
- **反方需验证**：是否真有结构化决策落库（decision_recorder / trade_records.metadata_json）足以替代 ObservabilityEngine？若有，本条降级。

### 盲区 4【P1】时区是散落各处的地雷

- **现象**：全库混用 `datetime.now()` / `utcnow()` / 写死 `-4` 偏移；多处 fallback 到服务器本地时区（CST）。
- **证据**：
  - `app/core/position_recovery.py:280`：`datetime.now(timezone(timedelta(hours=-4)))` 硬编码 EDT，冬令时差 1 小时。
  - `app/playbook/pipeline.py:548`：DST 启发式 `3 <= month <= 11` 把整个 11 月当夏令时（实际 11 月第一个周日切换）。
  - `app/pipeline/llm_pipeline.py:118`、`position_monitor.py:1122`：`except` fallback 到 `datetime.now()`（CST），跨日边界把"今天"算错一天。
  - `app/utils/holiday_calendar.py:69-78`：节假日硬编码到 2027；**半日交易日（Black Friday 11/27、Christmas Eve 12/24，13:00 收市）未识别**，EOD 硬编码 16:00（`pipeline.py:4326`）。
  - 全库 50+ 处 `datetime.utcnow()`（Python 3.12+ deprecated）。
- **后果**：expiry/cutoff 判定错位；半日当天 13:00–16:00 持仓无人管；2028 年起日历系统性漂移。
- **注**：此问题已有 `DEB-20260627-tz-ssot-audit.md` 记录，但**半日交易日**与 **DST 11 月边界**似未覆盖。反方核对。

### 盲区 5【P1】状态持久化普遍不原子 + 幂等失效

- **现象**：关键状态文件直接 `open('w')` 截断写，无 tmp+rename、无跨进程锁；异常被 debug 吞掉。
- **证据**：
  - `app/core/issue_registry.py:66-68`：非原子写，`threading.Lock` 仅对线程，多进程互相覆盖。
  - `app/core/config_writer.py:327-337`：审计读改写非原子；500 条 rotation 静默丢弃；实测 23 条审计中 21 条是 no-op（old==new）。
  - `scripts/archive/api_limit_monitor.py:208-225`：多线程 read-modify-write 丢计数 → **800 上限告警永不触发**；且该"归档"文件仍被 `app/data/twelvedata_provider.py:31`、`finnhub_provider.py:36` import（看似归档实为生产）。
  - `app/positions/reconciler.py:96-115`：`reconcile_signatures.json` 实测仅 2 字节 `{}`，异常被 `logger.debug` 吞 → **对账幂等事实失效**，每次重启重复告警。
  - `app/web_dashboard/mode_switch_service.py:558-578`：`_write_env_key` 非原子写 `.env.us`（同文件 line 82 却有原子版本），切 sim/real 关键路径。
- **后果**：崩溃留半截 JSON；多进程数据竞争；限流/对账/告警机制静默失效。

### 盲区 6【P1】模拟盘 → 实盘的假设鸿沟

- **现象**：sim 成交假设过于理想；多个风控点 fail-open；账户 fallback 危险。
- **证据**：
  - `config/unified_config.yaml:477-481`：`simulate_slippage_bps: 5.0`、`simulate_partial_fill: false`（全部成交）、`simulate_fill_delay_seconds: 0.5`。实盘小盘/盘前滑点 50–200bps、部分成交是常态、延迟 1–5s。
  - `config/unified_config.yaml:1295-1300`：`acb_fail_mode: open`、`preflight_fail_mode: open`；`app/llm/pre_entry_gate.py:6-9` LLM 失败 → go=True。注释说"live 前切换"，但**无启动断言**。
  - `app/execution/dual_broker.py:132-140`：SIM 账户为空时 fallback 到 **REAL 账户 + TrdEnv.SIMULATE**，一次配置误改可能在真账户跑测试单。
  - `app/risk/pre_trade_gate.py:67-86`：`LIVE_CONSERVATIVE` 只缩放限额，不 disable 激进功能，与名称不符。
- **后果**：sim 表现严重偏离实盘；切 live 漏改一个开关 = 真金无保护。
- **注**：已有 `DEB-20260627-sim-live-divergence.md`，反方核对是否已覆盖 fail-open 启动断言与账户 fallback。

### 盲区 7【P1】SQL 注入与数据正确性

- **现象**：多处 f-string 拼 SQL；NULL 语义导致统计口径错误。
- **证据**：
  - `app/analysis/signal_ic_tracker.py:486-505`：`weight_version`、`_disabled_strats` f-string 拼接。
  - `app/storage/trade_store.py:1354`：`UPDATE open_positions SET {field} = ?`，`field` 无白名单。
  - `app/playbook/db.py:99`、`app/web_dashboard/order/audit.py:52,78`、`app/evolution/shadow_tracker.py:159`、`app/backtest/weekly_optimizer.py:173`：`ALTER TABLE ... ADD COLUMN {col}` / `PRAGMA table_info({name})` 表名/列名拼接。
  - `app/web_dashboard/data_service.py:1052`：`exit_reason NOT IN (...)` 对 NULL 返回 UNKNOWN → **幽灵交易（PHANTOM_RECONCILE_VOID/ORPHAN_CLEANUP）被统计成真实交易**，绩效口径错误。
- **后果**：内部攻击面有限但 SAST 会标；统计口径错误直接影响策略评估。

---

## 次要发现（P2/P3，供参考，非辩论焦点）

- **死代码/死开关**：`run_llm_backtest`（`app/pipeline/llm_pipeline.py:567`）零调用；`ENABLE_BACKTEST`（`config/config_loader.py:374`）无人读、yaml 无 `features.backtest` 键；`app/core/scheduler_tasks.py` 是 deprecated 空壳。
- **12 个 0 字节僵尸 db**（根目录与 `app/data/` 下），相对路径可能命中空 db 而非 `data/ftseek_us.db`。
- **`verify=False`** 散落 5+ 数据源（Finnhub/TwelveData/RSSHub/insider/fundamental），MITM 风险被默认接受；SEC UA fallback 是合规问题。
- **RSSHub 单实例**（`app/data/rsshub_us_news.py:71-74`），唯一可用，宕机即新闻链路全断。
- **LLM 重试最弱**（`app/llm/model_call.py:209` 仅 1 次重试 + 1s 退避），与告警兜底（5 次指数退避）风格不一致。
- **限额四套并存无 SSOT**：`max_position_pct` / `daily_notional_cap_pct` / `risk.position.*` / `dual_mode.exposure_control.*`；30 仓 × 4% = 120% > 80% cap，数学关系模糊。
- **回测与 playbook 参数脱节**（见 `DEB-20260627-mature-patterns.md`）：weekly_optimizer 优化老的全局单值参数，与 playbook 的 `factor_exit_rules` 是两套系统，`backtest_result` 字段从未被填充。

---

## 审计方建议（按资金影响排序，若只能修三件）

1. **broker 端挂真实 stop-limit 单 + 补 SPY/VIX 熔断**（否则一次黑天鹅/进程崩溃就可能重伤）。
2. **打通异常 → 审计 → 告警链路**（debug 吞异常改结构化失败事件；启动时实例化 ObservabilityEngine；关键路径 fail-closed）。
3. **切 live 前的 fail-closed 启动断言 + 时区统一（含半日交易日/DST）**。

---

## 给反方 agent 的辩论焦点（请逐条证伪/确认/补充）

1. **盲区 1 是否成立**？富途 OpenD 是否支持 stop/stop-limit 单？若支持而未用，确认 P0；若 API 不支持，需评估替代方案（如 OpenD 端条件单）。
2. **盲区 2 是否成立**？是否存在其他美股熔断路径（`order_executor.py` 的 `_vix_circuit_breaker`、`emergency_interceptor.py`）实际生效？
3. **盲区 3 是否被高估**？`decision_recorder` + `trade_records.metadata_json` 是否已提供足够的决策可追溯性，使 ObservabilityEngine 非必需？
4. **盲区 4/6 是否已被既有 DEB 文档覆盖并修复**？请核对 `tz-ssot-audit`、`sim-live-divergence`、`param-optimization` 等文档的落地状态。
5. **优先级排序是否合理**？在"仍是模拟盘、无真实资金"的当下，P0 的紧迫性是否应让位于"先积累 sim 数据"？
6. **是否有审计遗漏的更高风险**？反方请补充。

## 已确认无问题的项（安全基线，反方可复核）

- R1b budget gate fail-close（`pipeline.py:2435`）：DB 异常视为预算耗尽，拦所有开仓 ✅
- CapitalSafetySystem BLACKOUT 锁定（`capital_safety.py:245-251`）：触发后强制全平 ✅
- `_notional_sanity_check` 30% 兜底（`dual_broker.py:170-197`）✅
- `PositionExitDispatcher` + 退出屏障（`position_monitor.py:52-91`）：防 flash_exit 死循环 ✅
- OrderTracker 时段感知超时（pre_market/regular/after_hours/closed 分别设置）✅

---

## 关键文件速查

- 执行：`app/pipeline/order_executor.py`（5418 行）、`app/pipeline/position_monitor.py`（2253 行）、`app/playbook/pipeline.py`（4975 行）、`app/execution/dual_broker.py`、`app/playbook/playbook_service.py`
- 风控：`app/risk/market_circuit_breaker.py`、`app/risk/capital_safety.py`、`app/risk/pre_trade_gate.py`、`app/us_pipeline/emergency_interceptor.py`
- 可观测：`app/observability/{engine,audit_logger,failure_tracker,watchdog,alert_debouncer}.py`、`app/pipeline/observers.py`、`app/core/issue_registry.py`
- 数据/配置：`app/core/{db_utils,config_writer,issue_registry}.py`、`app/config_mgmt/{hot_reloader,drift_detector}.py`、`app/positions/reconciler.py`、`app/analysis/signal_ic_tracker.py`、`app/storage/trade_store.py`
- 时区：`app/utils/{timezone,holiday_calendar}.py`、`app/core/position_recovery.py`
- 配置：`config/unified_config.yaml`、`config/config_loader.py`
- 相关既有辩论：`DEB-20260627-tz-ssot-audit.md`、`DEB-20260627-sim-live-divergence.md`、`DEB-20260627-mature-patterns.md`、`DEB-20260627-param-optimization.md`

> **免责声明**：本文为静态审计 briefing，行号基于审计时点快照，hotfix 分支演进可能使部分结论失效。反方应以当前代码为准逐条核验。

---

# 反方核验（Qoder，2026-07-20，基于当前 `rfc-060-phase1-hotfixes` 代码实读）

**核验方法**：对 7 大盲区逐条读取当前代码，确认/证伪/修正。结论 = 确认成立 / 部分成立(需修正表述) / 已被 hotfix 覆盖。

## 逐条裁决

### 盲区 1【止损不在 broker 端】— ✅ 确认成立（P0，切实盘前最高优先级）

实读 `dual_broker.py:submit_order` (L225-233)：`ctx.place_order(price, qty, code, trd_side, order_type, trd_env, acc_id, remark)` — **确无 `aux_price`/`trail_type`/`trail_value` 等 stop 参数**。止损 100% 依赖 `monitor_playbook_sim_exits` 每 30-60s 轮询当前价 vs `_active_sl`。

**修正/补充审计方表述**：
- 后果分级要区分 SIM/LIVE。**当前 SIM 阶段此风险不暴露**（无隔夜真金、进程崩溃只丢模拟盈亏），故不必在样本积累期动它。
- **但这是切 LIVE 的头号阻断项**：进程崩溃/断网/深夜时段 = 止损真空；30s gap 内 LULD 复牌跳空实亏可 10-30%。
- **辩论焦点回答（富途是否支持）**：富途 OpenD `place_order` 支持 `aux_price`（止损触发价）用于 `STOP`/`STOP_LIMIT` 单类型。**是"支持但没用"，非 API 限制** → 可直接修复。建议 LIVE 前挂 broker 端 stop-limit 作为轮询的冗余底线（双保险，非替代——轮询仍需保留处理 trailing/factor exit）。

### 盲区 2【熔断只看 HSI】— ⚠️ 部分成立（表述需重大修正，P0→P2）

实读结果推翻了"美股黑天鹅系统完全无感"的核心论断：
- `market_circuit_breaker.py` 确实只有 `check_hsi_drop`，**但它在 playbook 生产路径零调用**（grep `app/playbook/` + `scheduler_core.py` 无引用）→ 是**死代码**，不是"错误生效的熔断"。
- playbook **有独立的美股 regime gate**：`pipeline.py:2377` `if regime_ctx.regime == NO_TRADE: 跳过 auto_execute`；`market_regime.py` 明确基于 **SPY 趋势 + VIX**（VIX>30 → -5 分 → NO_TRADE，position_mult=0.0）。`pipeline.py:2383` 另有 `RiskState.RISK_OFF/HALT` 拦截。
- 故美股极端日**有 VIX 驱动的开仓拦截**，非"完全无感"。

**修正后的真实风险**（仍值得修，但降级）：
1. VIX 门是"软"的——`CAUTIOUS`(0.5)/`RISK_OFF`(0.3) 只缩仓不停仓，只有 VIX>30 才 NO_TRADE。介于 SPY 单日 -5% 但 VIX 未破 30 的情形（快速下杀初期）确有敞口。
2. HSI 死代码应删除（清洁度，非风险）。
3. **建议**：把"SPY 单日/盘中跌幅"补进 regime 评分（现在只有 SPY 趋势 MA + VIX 绝对值，缺"当日急跌"维度），比挂 HSI 熔断更对症。此项从 P0 降为 **P2**（已有 VIX 兜底，非裸奔）。

### 盲区 3【审计/告警链路断裂】— ⚠️ 部分成立（被高估，P0→P1）

- `ObservabilityEngine`/`failure_tracker` 零业务调用 — **确认**（属实的死基建）。
- **但"下过哪些单、为什么没下"在代码层面无法回答"是被高估的**：实读发现 `_emit_exec_gate_audit` (pipeline.py:2257) **每个到达执行漏斗的候选单都落库** `data/shadow/exec_gate_audit.jsonl`（executed / skipped_bp_cap / skipped_notional_cap / skipped_max_positions 全记 + edge_bucket/verdict）；`trade_records.metadata_json` 记完整成交决策；round3 又加了 bridge 三态告警 + 批量止损熔断 HIGH 告警 + ghost_cause 归因。**决策可追溯性实际存在，只是分散在 shadow JSONL + DB 而非统一 ObservabilityEngine。**

**修正裁决**：辩论焦点 3 的答案是"**是，decision 落库已提供足够追溯性，ObservabilityEngine 非必需**" → 盲区 3 从 P0 降 **P1**。真正待修的是：(a) 关键下单/平仓路径的 `except: logger.debug` 应升级为结构化 WARN/HIGH（round3 已对 bridge/熔断做了，其余路径待补）；(b) 删除两套死 AuditLogger 之一，消除"看起来有其实没用"的假象。

### 盲区 4【时区地雷】— ✅ 确认成立（P1）

- `pipeline.py:4326` `minutes_to_close = 16 * 60 - now_minutes` — **EOD 硬编码 16:00 确认**，半日交易日（11/27、12/24 收 13:00）无识别 → 半日 13:00-16:00 持仓多算 3 小时、PRE_CLOSE_EXIT 判定错位。
- 散落的 naive `datetime.now()` fallback + DST 启发式 `3<=month<=11` — 与 `DEB-20260627-tz-ssot-audit.md` 记录一致；**半日交易日确为该文档未覆盖的新增项**，审计方发现有效。
- **注**：Tier A/B 已修了 pdt_guard/dual_broker 的 DST（commit 3e298c1f/7f9eb0e2），但**退出侧 EOD 16:00 硬编码未在既有修复范围内** → 是真缺口。

### 盲区 5【状态持久化不原子 + 幂等失效】— ✅ 确认成立（P1）

- `reconcile_signatures.json` 实读 **确为 2 字节 `{}`** → 对账幂等事实失效，审计方证据准确。
- 非原子写普遍存在（issue_registry/config_writer）。SIM 阶段影响有限（单进程为主），但**多进程 cron + 主循环并发写同一 JSON 是真实竞争**。
- **建议**：优先修 `reconcile_signatures`（幂等失效导致重复告警/重复对账动作，是当前就在发生的 bug，非 LIVE 才暴露），其余原子写批量整改。

### 盲区 6【SIM→LIVE 假设鸿沟】— ✅ 确认成立（P0，切 LIVE 前阻断）

- `dual_broker.py:132-140` **SIM 账户空 → fallback 到 REAL acc_id + TrdEnv.SIMULATE 确认**。当前 SIM 靠 TrdEnv.SIMULATE 兜底安全，**但一旦某处把 TrdEnv 传错/配置误改，就是真账户下单**。这是"一个字段之差 = 真金裸奔"的隐患。
- `acb_fail_mode: open` / `preflight_fail_mode: open` **确认仍为 open**，且无启动断言。
- **建议（切 LIVE 前必做，可现在加不影响 SIM）**：(a) LIVE 模式启动断言——`mode==real` 时强制 `acb_fail_mode/preflight_fail_mode=close`，否则拒绝启动；(b) 移除或显式告警 SIM→REAL 账户 fallback（宁可 SIM 报"无账户"也不静默借用 real acc_id）。

### 盲区 7【SQL 注入 + NULL 口径】— ✅ 确认成立（P1 数据正确性 / P3 注入）

- 表名/列名 f-string 拼接：内部可控输入，注入面有限（SAST 会标），P3。
- **NULL 语义导致幽灵交易被计入绩效**（`data_service.py:1052` `exit_reason NOT IN(...)` 对 NULL 返 UNKNOWN）**才是真问题**——直接污染胜率/PnL 统计，而这正是毕业判定的输入。**与 round3 δ.6"毕业清洗口径"(void 不计) 直接相关**，应确认清洗口径已覆盖此 NULL 陷阱。P1。

## 反方补充：审计遗漏的更高风险

1. **[元风险] 样本积累期频繁改实单退出参数 = 污染数据生成过程**。当前 83 笔跨越 time_exit 4h→6h、drawdown 0.4→0.8 多套参数，且 early_tight 又在改 → IC/胜率估计不可比，**直接危及毕业判定的有效性**。建议积累窗口冻结实单退出、探索全走 shadow。此风险比本文多数盲区更贴近当前目标。
2. **[退出韧性] 盲区1的孪生问题**——即便不挂 broker stop，`monitor_playbook_sim_exits` 在**报价 stale / 单轮异常 / monitor 卡死**时的降级行为未审计。轮询式止损的可靠性 = 退出监控本身的可靠性，比 SL 参数精度更重要。

## 优先级重排建议（反方版）

| 盲区 | 审计方定级 | 反方修正 | 理由 |
|---|---|---|---|
| 1 broker stop | P0 | **P0 (仅 LIVE 前)** | SIM 不暴露，但 LIVE 头号阻断，可直接修（富途支持 aux_price） |
| 6 fail-open/账户fallback | P1 | **P0 (LIVE 前)** | "一字段之差真金裸奔"，启动断言现在就能加 |
| 5 reconcile 幂等 | P1 | **P1 (当下)** | 已在发生的 bug，非 LIVE 才暴露 |
| 7 NULL 口径 | P1 | **P1 (当下)** | 污染毕业判定输入，与 δ.6 联动 |
| 4 半日/EOD | P1 | **P1** | 退出侧 EOD 16:00 硬编码是既有修复的真缺口 |
| 3 可观测 | P0 | **P1 (降级)** | 决策落库实际存在(exec_gate_audit+metadata)，非"无法回答" |
| 2 熔断 | P0 | **P2 (降级)** | HSI 是死代码；playbook 有 VIX/regime 兜底，非"完全无感" |
| 元风险(反方) | — | **P0 (当下)** | 频繁改实单退出污染积累数据，直接危及核心目标 |

## 一句话结论

审计方的**方向判断正确**（"策略层细、执行/可观测层有断裂"），7 盲区中 **5 条确认成立**（1/4/5/6/7），**2 条需重大修正降级**（2 熔断被 VIX regime 兜底、3 可观测被决策落库部分替代）。**切 LIVE 前的真正阻断项是盲区 1（broker stop）+ 盲区 6（fail-open 启动断言）**；**当下 SIM 阶段最该修的是反方补充的元风险（冻结实单退出参数保护数据质量）+ 盲区 5/7（幂等与 NULL 口径直接污染毕业判定）**。盲区 2/3 可延后。所有 LIVE-only 项不必在样本积累期动手，避免扰动数据。

---

# 审计方回应（Qoder，2026-07-20，round 2）

**核验方法**：对反方的关键反驳点逐条回读当前代码验证（regime gate / exec_gate_audit / HSI 熔断接线状态），再裁决接受或保留。

## 接受反方的修正（3 项）

1. **盲区 2 降级 P0→P2：接受。** 审计方原论断"美股黑天鹅系统完全无感"有误。实读确认 playbook 有 VIX/SPY regime gate（`pipeline.py:711` `regime_mults={...NO_TRADE:0.0}`、`pipeline.py:1468` `MarketRegimeGate().assess()`，VIX>30→NO_TRADE→position_mult=0）。审计方犯了"只看 `market_circuit_breaker.py` 单文件就下全局结论"的错误，未查 regime 层。反方修正准确。
2. **盲区 3 降级：基本接受，但需拆分（见保留点 2）。** `exec_gate_audit.jsonl`（`pipeline.py` + `decision/edge_shadow_gate.py`）+ `trade_records.metadata_json` 确实覆盖入场侧追溯。
3. **反方补充的元风险：接受并列为最高优先。** "样本积累期频繁改实单退出参数 = 污染数据生成过程，危及毕业判定有效性"是审计方完全遗漏、且最贴近当前核心目标的风险。

## 审计方保留 / 反推（3 项）

1. **盲区 2 的"死代码"表述不精确（结论不变）。** HSI 熔断并非完全死代码：它经 `app/risk/pre_trade_check.py:626-647` 的 `_check_system_breaker()` → `is_trading_suspended()` 仍有接线，只是只对 HSI 生效、对美股零覆盖。措辞应改为"**接线但看错市场**"。且 `_check_system_breaker` 的 `except: return True`（line 646）本身是 fail-open，与盲区 3 同源——这是反方降级时未计入的次生点。P2 定级不变。
2. **盲区 3 应拆为两半，不可整体降 P1。** `exec_gate_audit` 覆盖的是**入场侧**（为什么没下单）；但 160+ 处 `except: logger.debug` 集中在 `_close_position` / 平仓重试链路——**"为什么平仓失败 / 仓位卡住"这一半仍然黑暗**，而平仓失败恰是切实盘后最致命的场景。建议：**入场侧追溯降 P1；平仓侧异常吞噬维持 P0**。
3. **"所有 LIVE-only 项不必现在动手"需加例外。** 盲区 6 的 fail-open 启动断言是**零数据扰动、零成本**的，现在即可加，且能防止积累期某次误配置提前触发真金。反方自己在盲区 6 亦写"可现在加不影响 SIM"，与总结论"不必动手"自相矛盾。应统一为"**现在加断言**"。

## 需反方再验证（1 项）

- 元风险声称"83 笔跨 time_exit 4h→6h、drawdown 0.4→0.8 多套参数"。请确认 IC / 毕业判定管线**是否已按参数版本归一化**（如 `weight_version` / 参数快照分组）。若已归一，污染被缓解，元风险可降 P1；若未归一，维持 **P0（当下）**。

## 最终优先级表（round-2 收敛版）

| 盲区 | 审计方 | 反方 | **最终** | 时机 | 备注 |
|---|---|---|---|---|---|
| 元风险：实单退出参数频繁变动污染积累数据 | — | P0 | **P0** | 当下 | 待反方验证参数版本归一化；若已归一降 P1 |
| 1 broker 端无真实止损单 | P0 | P0(LIVE前) | **P0** | LIVE 前 | 富途支持 aux_price，可直接修；轮询保留处理 trailing/factor exit |
| 6 fail-open + SIM→REAL 账户 fallback | P1 | P0(LIVE前) | **P0** | **现在加启动断言** | 零数据扰动；移除/告警账户 fallback |
| 3b 平仓侧异常吞噬（_close_position 160+ debug） | P0 | (含于3)P1 | **P0** | 当下起逐步 | 升级结构化 WARN/HIGH；切实盘后最致命 |
| 5 reconcile 幂等失效（reconcile_signatures 2字节） | P1 | P1(当下) | **P1** | 当下 | 已在发生的 bug，重复告警/重复对账 |
| 7 NULL 口径污染绩效（data_service:1052） | P1 | P1(当下) | **P1** | 当下 | 与 round3 δ.6 毕业清洗口径联动 |
| 4 半日交易日 + EOD 16:00 硬编码 | P1 | P1 | **P1** | 当下/排期 | 既有 tz-ssot 修复未覆盖退出侧 |
| 3a 入场侧追溯（ObservabilityEngine 死基建） | P0 | P1 | **P1** | 延后 | exec_gate_audit+metadata 已覆盖；删一套死 AuditLogger |
| 2 熔断只看 HSI | P0 | P2 | **P2** | 延后 | 接线但看错市场；VIX regime 兜底；删 HSI 死逻辑或补 SPY 当日急跌维度 |

## round-2 一句话结论

辩论收敛良好，反方实读代码纠正了审计方两处过度论断（盲区 2/3），并贡献了最高价值的元风险。**最终共识**：当下 SIM 阶段优先修**元风险（保护毕业判定数据质量）+ 盲区 5/7（幂等与 NULL 口径）+ 盲区 6 启动断言（零成本防真金）**；**盲区 1（broker stop）+ 盲区 3b（平仓侧异常）为切 LIVE 头号阻断**；盲区 2/3a 延后。**两处微调**：盲区 3 拆为入场侧(P1)/平仓侧(P0)；盲区 6 启动断言从"LIVE 前"提前到"现在"。**一项待验证**：参数版本归一化决定元风险定级。

---

# 反方 round-3 最终收敛（Qoder，2026-07-20）

## 先回答审计方待验证项：参数版本归一化 → 元风险**半成立**（拆为两个定级）

实读 `ic_tracker._check_graduation` (L464-503) + `backfill_forward_returns` (L162+) 后确认，元风险必须拆开看：

- **IC 侧毕业判定（`ic_positive: IC(T+3)>0.05`）不受退出参数污染。** IC 基于 **forward return = entry_price → N 日后日线价**，是**入场锚定、退出无关**的量。改 time_exit/drawdown 不动 forward return，故 IC 侧天然免疫。→ **此半降 P1**。
- **cap_reduction 毕业标准（45% 胜率 + 正 PnL）受污染。** 它读**已实现 PnL**，而 PnL = 入场 edge × 退出执行。退出参数一变，同一批入场信号的胜率/PnL 就变，且 `config_version` 列虽存在（`db.py:81`）但**毕业判定未按其分组归一化**。→ **此半维持 P0（当下）**。

**结论**：元风险不是"数据全废"，而是"**PnL/胜率维度不可比，IC 维度可比**"。这反而给出最优解——见下方修复 M。

## 最终修复方案（逻辑 + 收益，按时机分组）

### 【当下·SIM 阶段就做】

**修复 M（元风险，P0）— 退出参数冻结 + config_version 打标，而非停止探索**
- **逻辑**：不是"禁止改退出参数"，而是 (a) 每笔 trade_record 落 `config_version`（退出参数快照哈希，列已存在，只需在 `_close_trade_record` 写入）；(b) cap_reduction 毕业统计**只取当前 config_version 窗口内的样本**；(c) 参数探索继续走 shadow（`exit_param_shadow`/`abort_30m`），实单退出参数在积累窗口内**冻结不动**直到影子选出赢家再一次性切换。
- **收益**：既保护毕业判定不被多套参数污染（PnL 侧可比），又不牺牲探索速度（shadow 并行）。**直接修复"边积累边调污染数据"的根因**，且复用现有 config_version 列 + shadow 框架，零新基建。
- **与上一轮修复 B 的联动**：修复 B（早期紧 SL）应**改为 shadow-only**，不实单化——与 M 一致，避免又引入一个 config_version 断点。

**修复 5（reconcile 幂等，P1）**
- **逻辑**：`reconcile_signatures.json` 空 `{}` 根因是异常被 `logger.debug` 吞。改：写入用 tmp+rename 原子写 + 异常升级 WARN；启动时校验文件非空。
- **收益**：消除每次重启的重复告警/重复对账动作（当前就在发生的 bug），对账幂等恢复。

**修复 7（NULL 口径，P1）**
- **逻辑**：`data_service.py:1052` `exit_reason NOT IN(...)` 对 NULL 返 UNKNOWN → 幽灵单计入绩效。改为显式 `exit_reason IS NOT NULL AND exit_reason NOT IN(...)`，或用白名单正向匹配。与 round3 δ.6 毕业清洗口径（void 不计）对齐，确保 PHANTOM/ORPHAN 不进胜率分母。
- **收益**：毕业判定输入干净——直接关系 45% 胜率算得准不准。与修复 M 是同一目标（保护毕业数据）的两面。

**修复 6a（启动断言，P0 提前到现在）— 零数据扰动**
- **逻辑**：进程启动时断言：`mode==real` → 强制 `acb_fail_mode==close && preflight_fail_mode==close`，否则拒绝启动并告警。SIM 模式不受影响（不改任何运行时行为）。
- **收益**：零成本防止积累期某次误配置/误切 real 时 fail-open 裸奔。审计方已指出反方原总结自相矛盾，此处采纳——提前到现在做。

**修复 3b（平仓侧异常，P0 起逐步）**
- **逻辑**：`_close_position` / 平仓重试链路的 160+ 处 `except: logger.debug` 中，**平仓失败/仓位卡住**类升级为结构化 WARN + 飞书 HIGH（复用 round3 已建的 push HIGH 通道 + ghost_cause 归因）。入场侧(3a)不动。
- **收益**：补上"为什么平仓失败"这一半黑暗——切实盘后最致命场景的可观测性。当下 SIM 也能先积累平仓失败画像。

### 【切 LIVE 前·不在样本积累期动】

**修复 1（broker stop-limit，P0 LIVE 阻断）**
- **逻辑**：`submit_order` 对有 SL 的开仓单追加富途 `aux_price`（触发价）+ STOP_LIMIT 单类型，作为轮询止损的**冗余底线**（非替代——`monitor_playbook_sim_exits` 仍保留处理 trailing/breakeven/factor exit）。
- **收益**：进程崩溃/断网/深夜时段止损不再真空；30s 轮询 gap 内的跳空由 broker 端兜底。**唯一能覆盖"系统离线时"的止损方案**。

**修复 6b（账户 fallback，P0 LIVE 阻断）**
- **逻辑**：移除 `dual_broker.py:132` 的 SIM→REAL acc_id fallback，或改为 SIM 无账户时**显式报错拒绝启动**，而非静默借用 real acc_id。
- **收益**：杜绝"一个字段之差在真账户跑测试单"。

### 【延后·P1-P2】

**修复 4（半日/EOD，P1）**：EOD `16*60` 硬编码改为读交易日历（含半日 13:00），补 `holiday_calendar` 半日表。收益：半日 13:00-16:00 持仓不再失管、PRE_CLOSE_EXIT 判定正确。

**修复 3a（P1）**：删两套死 AuditLogger 之一 + ObservabilityEngine 死基建（清洁度，非风险，exec_gate_audit 已覆盖入场追溯）。

**修复 2（P2）**：删 HSI 死逻辑（审计方 round-2 指出经 `pre_trade_check.py:626` 仍有接线但只看 HSI，需一并清理其 `except: return True` fail-open），并把"SPY 当日/盘中急跌"维度补进 `market_regime` 评分（现只有 SPY 趋势 MA + VIX 绝对值，缺急跌维度）。收益：覆盖"SPY 单日 -5% 但 VIX 未破 30"的快速下杀初期敞口。

## 最终一句话

参数归一化验证结论——**元风险 IC 侧免疫、PnL 侧成立**，故最优解不是停止探索而是**修复 M（config_version 打标 + 毕业按版本取样 + 退出探索走 shadow）**。当下 SIM 做 **M + 5 + 7 + 6a + 3b**（全部保护毕业数据质量或零成本防真金）；**修复 1 + 6b** 为切 LIVE 头号阻断；4/3a/2 延后。上一轮的修复 B（早期紧 SL）**收编为 shadow-only**，与 M 一致不实单化。

---

# 实施完成（Qoder，2026-07-20）

当下·SIM 阶段 5 项已全部实施 + 编译测试通过 + 文档同步。

| 修复 | 状态 | 代码锚点 | 验证 |
|---|---|---|---|
| M — config_version 打标 + 毕业按版本取样 + early_tight 收编 shadow | ✅ | `config.PlaybookConfig.get_exit_config_version`; `pipeline._close_trade_record` (metadata.config_version); `scripts/okr_status.py::graduation_sample`; `exit_shadow.EARLY_TIGHT_VARIANTS`; `unified_config.yaml` early_tight_sl_enabled→false | 哈希稳定 `exit-4bc8f6797088`; okr graduation_sample 窗口化输出正常; shadow 变体触发+反事实计算正确 |
| 5 — reconcile 幂等 | ✅ | `positions/reconciler.py` `_save_signatures` 防空覆盖 + `_load_signatures` 空/非法 WARN | 防覆盖分支实测跳过写入并 WARN |
| 7 — NULL 口径 | ✅ | 6 文件 (analytics/dashboard/risk/trade/data_service/thesis) 删冗余 NULL-unsafe `exit_reason NOT IN`, 保留 COALESCE 版 | 16 处 COALESCE 守卫全保留, 冗余子句 0 残留 |
| 6a — LIVE 启动断言 | ✅ | `core/di_container.py` real 分支断言 acb/preflight fail_mode==closed 否则 raise | SIM 不进分支 (零行为影响); py_compile 过 |
| 3b — 平仓失败可观测 | ✅ | `pipeline._alert_close_failure` 接入正常/SL/EOD 三链路卡住点 (BROKER_EXCEPTION/CLOSE_FAILED/ORDER_NOT_FILLED/SL_*/EOD_*) | 结构化 WARN + 飞书 HIGH, symbol+日+reason 去重 |

**测试**: py_compile 全过 (12 文件); pytest 9 项失败经 `git stash` 对照证实为基线遗留 (test fixture 缺 mfe_pct 列 + intraday trigger mock), 与本批无关, 零新回归。

**文档同步**: `handoff.md` (变更日志 + 退出旋钮地图 D + 下一步); `docs/trading-pipeline-flowchart.md` (SLTP early_tight shadow 化 + #3b 平仓失败节点 + 毕业窗口取样 + 图例, SSOT 参数块已重生成); `docs/OKR-SPEC.md` (第4条清洗口径 + 变更记录)。

**未做 (切 LIVE 前)**: 修复 1 (broker stop-limit) + 6b (账户 fallback)。**延后 (P1-P2)**: 4 / 3a / 2。
