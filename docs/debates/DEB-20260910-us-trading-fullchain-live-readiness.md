---
title: 美股交易全链路复盘与实盘就绪度辩论
date: 2026-09-10
status: 多模型审议完成，待用户裁决（未经裁决不得改交易行为）
trigger: 用户指令「请总结出deb文档，让多模型会话分析确认共识后再决策执行」
debaters: Qoder（主持/证据汇总）, Qwen3.8-Max（架构与根因）, Kimi-K3（独立审查与反幻觉）, GLM-5.3（实证与可执行性复核）
---

# 美股交易全链路复盘与实盘就绪度辩论

> 本文目标不是立即优化策略参数，而是确认现役交易链是否具备安全、可审计、可恢复的实盘条件。
> 本轮纪律：只读审议；多模型意见未收敛且用户未裁决前，不执行代码、配置、服务或数据库变更。

## 0. TL;DR（主持方待质证判断）

1. 现役 Playbook 链已经覆盖数据、筛选、决策、仓位、风控、下单、同步、退出和复盘，架构方向基本完整。
2. 当前首要矛盾不是增加策略，而是成交—账本一致性、运行模式真源、关键门禁 fail-open 和运行可审计性。
3. 228 笔记录毛收益 9.94bps、费用 9.00bps、净收益 0.94bps，费用吞噬约 90.5%；日聚类 IC 各周期均不可分辨，尚不足以证明可稳定实盘。
4. 主持方初判：模拟盘可继续采样；实盘切换 NO-GO，直到本文 P0 验收项闭合并重新裁决。

## 1. 现役全链路与边界

```text
main.py / TradingScheduler
  → 盘前数据聚合与 Playbook 筛选
  → DA / UE / reentry / voting writers
  → playbook_instructions
  → PlaybookService.execute_instruction
  → 仓位与风险门
  → DualBrokerAdapter.submit_order
  → 券商订单/成交
  → open_positions / trade_records / position sync / reconcile
  → Playbook 20s 退出快环 + position monitor
  → close_position
  → 日/周/月复盘、IC、归因、影子验证
```

边界确认：Playbook 是现役唯一自动新开仓主链；旧 signal_pipeline/order_executor 新开仓路径已冻结，但仍承担部分研究、快照、监控或兼容职责。

## 2. 已核实事实基线

### 2.1 已有有效防线

- 同一 instruction 使用条件 UPDATE 原子抢占：`app/playbook/db.py:721-736`。
- Playbook 执行统一进入 `PlaybookService.execute_instruction` / `_execute_via_dual_broker`。
- 存在 PDT、资金安全、绝对熔断、Kill Switch、仓位限制等多层门禁。
- broker 侧保护性止损可在主进程故障时提供最后防线。
- 已建立大量契约测试、影子模式与配置冻结/毕业纪律。

### 2.2 策略经济性基线

`data/okr_latest.json:822-833`：

| 指标 | 当前值 |
|---|---:|
| trade_records | 228 |
| 胜 / 负 | 145 / 82 |
| 总 PnL | $469.89 |
| 毛收益 | 9.94 bps |
| 费用 | 9.00 bps |
| 净收益 | 0.94 bps |
| 平均名义金额 | $5,527.59 |

`data/okr_latest.json:300-338`：按交易日聚类后 intraday/T1/T3/T5 均 `distinguishable=false`，Playbook graduation 仍为 `ACCUMULATING`。

## 3. 待辩论议题

## 议题 A：运行模式与数据库是否必须单一状态真源？

### 事实

- 券商执行环境主要由 `AUTO_TRADE_ACCOUNT` 决定。
- DB 路由由 `TRADE_ENV` 决定：`app/storage/db_pool.py:40-54` 中 `TRADE_ENV=0 → ftseek_us_live.db`，否则 `ftseek_us.db`。
- position sync 等模块存在分散读取环境变量的现象。

### 风险假设

两个控制量若不一致，可能造成实盘成交写模拟账本、模拟行为污染实盘账本、研究门禁读错库或风控状态与真实持仓分离。

### 选项

- **A1（主持方推荐）**：建立不可变 `TradingMode`（sim/live）单一真源，由其派生 broker、DB、通知和风险配置；启动时做组合断言，不合法即阻断。
- **A2**：保留双变量，仅增加一致性告警。
- **A3**：维持现状。

### 待模型回答

风险是否 P0？A1 是否会扩大切换爆炸半径？最小可落地边界是什么？

## 议题 B：订单已被券商接受后，是否允许释放执行 claim？

### 事实

- `app/playbook/playbook_service.py:1513-1531` 在下单前抢占 instruction，提交异常或明确失败时释放。
- 当成交价为零时进入状态确认；确认查询异常被降级为 `_confirmed=True`：`app/playbook/playbook_service.py:1567-1575`。
- 最终仍取不到成交价时释放 claim：`app/playbook/playbook_service.py:1622-1627`。
- 同一 instruction 的幂等键存在，但 broker 已受理、状态未知、账本未写的灰区仍可能存在。

### 风险假设

券商异步成交但内部释放 claim 后，下一轮可能重复下单，形成真实双仓或账实分裂。

### 选项

- **B1（主持方推荐）**：券商一旦返回订单 ID，claim 进入持久化 `SUBMITTED/UNKNOWN`，只有明确 `REJECTED/CANCELLED/EXPIRED` 才能释放；后台 reconciler 最终收敛成交与账本。
- **B2**：保留现状，只延长确认窗口并增加告警。
- **B3**：查询异常立即 cancel，再根据 cancel 终态决定是否释放。

### 待模型回答

B1 是否是最小根因修复？是否需要完整订单状态机，还是增加一列/一张 outbox 即可？

## 议题 C：新开仓行情与盘前供给异常应 fail-open 还是 fail-close？

### 事实

- arrival quote 获取失败不阻塞下单：`app/playbook/playbook_service.py:1396-1411`。
- 报价健康门仅在 `_arr_ctx` 存在时运行，门异常放行：`app/playbook/playbook_service.py:1434-1467`。
- 新鲜度门同样在无 quote 或异常时放行：`app/playbook/playbook_service.py:1469-1487`。
- 数据源和 DA bridge 异常在盘前预检中主要为 WARN：`scripts/pre_market_check.py:116-170`。
- 数据健康脚本发现红项后仍正常返回：`scripts/data_health_check.py:155-196`。

### 选项

- **C1（主持方推荐）**：新开仓 fail-close；减仓、平仓、保护单永远可执行；将 NO_DATA 作为独立原因码和指标。
- **C2**：实盘 fail-close、模拟盘继续 fail-open 收集样本。
- **C3**：维持 fail-open，仅增强告警。

### 待模型回答

应选 C1 还是 C2？哪些数据缺失必须阻断，哪些可安全降级？

## 议题 D：盘前生成与调度是否需要进程/线程隔离？

### 事实

`app/core/scheduler_core.py:1130-1143` 记录 2026-09-03 盘前 generate 持续 30.5 分钟并跨过开盘，导致开盘后 90 秒内 TRADING 执行循环一次未运行。当前步骤级跨界守卫无法中断正在运行的 generate。

### 选项

- **D1（主持方推荐）**：重任务外移 worker，调度主循环只消费带日期、生成时间和完整性标记的产物；设置硬截止时间。
- **D2**：保留单进程，给 generate 增加超时与取消检查。
- **D3**：仅提前 cron 时间。

### 待模型回答

最小可靠方案是什么？如何避免 worker 产物半写、过期或与交易日错配？

## 议题 E：退出链路是否必须统一配置与状态模型？

### 事实

- Playbook 仓由约 20 秒快环处理；其他仓位还存在 position monitor 路径。
- 两套路径读取不同退出配置，trailing stop 缺省语义存在相反风险。
- 组合级异常部分场景仅告警，人工接管 SLA 缺乏持久化证据。

### 选项

- **E1（主持方推荐）**：统一 ExitPolicy/PositionState SSOT，保留不同调度器但共享同一决策函数；broker 硬止损作为最终防线。
- **E2**：直接合并为单一退出循环。
- **E3**：维持双路径，仅加契约测试。

### 待模型回答

E1 与 E2 哪个爆炸半径更合理？应先统一配置还是先统一执行器？

## 议题 F：日复盘和守卫如何避免“假完成”？

### 事实

- 日复盘在真正生成前写入完成标记：`app/core/scheduler_triggers.py:461-480`。
- 后续步骤有大量“不影响主流程”的异常降级，最外层失败后当日不会自动重试：`app/core/scheduler_triggers.py:478-695`。
- 历史上存在守卫参数错误、导入错误、轮转日志漏读等“守卫自身失效”事件。

### 选项

- **F1（主持方推荐）**：按步骤持久化状态，只有 required steps 全成功才写 DONE；每个守卫记录最近一次真实成功凭据，并由独立元守卫检查新鲜度。
- **F2**：只把完成标记移动到函数末尾。
- **F3**：维持现状，失败人工补跑。

## 议题 G：跨进程同标的幂等是否已闭合？

### 事实

- instruction claim 仅保护相同 ID：`app/playbook/db.py:721-736`。
- per-symbol lock 为 Python 进程内锁：`app/playbook/playbook_service.py:1150-1165`。
- 主服务与 Dashboard 可能由不同进程发起执行。

### 选项

- **G1（主持方推荐）**：增加数据库级 account+symbol 开仓 reservation，查重—提交—记账由持久化状态协调。
- **G2**：Dashboard 禁止直接执行，只向主进程投递。
- **G3**：维持进程内锁。

### 待模型回答

应优先 G1 还是 G2？是否需要两者同时实施？

## 议题 H：策略是否具备扩大风险预算的证据？

### 事实

- 净收益只有 0.94bps，费用约占毛收益 90.5%。
- 日聚类 IC 无周期达到可分辨状态。
- Walk-forward 框架会计算退化率和参数稳定性，但当前证据不足以证明真实 OOS 门禁已经强制执行：`app/backtest/walk_forward.py:226-260`。

### 选项

- **H1（主持方推荐）**：继续模拟/极低风险采样；毕业条件改为扣费后 OOS、日聚类置信区间、账本完整率和执行质量共同通过。
- **H2**：维持当前风险预算，以胜率和累计 PnL 为主。
- **H3**：因净收益为正而扩大风险预算。

## 4. 主持方初步优先级（待多模型推翻或确认）

### P0：实盘切换前必须闭合

1. A1：单一 TradingMode 与启动阻断。
2. B1：订单接受后不可回到可重试状态，建立最终一致对账。
3. C1/C2：实盘新开仓数据异常 fail-close。
4. E1 第一阶段：退出配置与语义统一，确认保护单覆盖率。

### P1：随后一周

5. G1/G2：跨进程幂等闭环。
6. D1：盘前重任务隔离与产物原子发布。
7. F1：复盘步骤状态机与守卫元监控。

### P2：持续验证

8. H1：维持低风险采样，按净收益与 OOS 证据毕业。
9. 收敛静默异常、配置 fallback、时区与数据新鲜度契约。

## 5. 建议验收指标

| 领域 | 验收目标 |
|---|---:|
| 行情未知状态的新开仓 | 0 |
| broker 已接受但内部无订单状态 | 0 |
| broker 成交但未入账 | 0 |
| 重复订单 | 0 |
| 账实差异发现延迟 | ≤60 秒 |
| 有效 broker 保护单覆盖率 | 100% |
| 退出快环 P99 | ≤30 秒 |
| 盘前计划准时且完整发布 | 100% |
| 日复盘 required steps 完成率 | 100% |
| 无成功凭据的静默守卫 | 0 |
| 扣费后 OOS 置信区间 | 下界 > 0 后才允许扩大风险预算 |

## 6. 多模型独立审议结果

| 议题 | Qwen3.8-Max | Kimi-K3 | GLM-5.3 | 合并裁决 |
|---|---|---|---|---|
| A 模式/数据库真源 | A1-lite，高 | A1，高 | A1，高 | **CONFIRMED**：已有 `trading_mode.py` 和漂移告警，不重建完整体系；补危险组合启动阻断并收敛裸读 env |
| B 订单 claim | B1，高 | B1，高 | B1，高 | **CONFIRMED/P0**：broker 已受理或状态未知后不得释放为可重试；最小实现为持久化 order state + reconciler |
| C 数据异常门 | C2，高 | C1，高 | C1；C2 可过渡，高 | **CONFIRMED/P0**：实盘新开仓 fail-close；模拟盘可保留 fail-open 采样；减仓/平仓不受阻 |
| D 盘前阻塞 | 短期 D3+D2、中期 D1，中高 | D1，中高 | D1，高 | **CONFIRMED/P1**：最终采用 worker + 原子产物发布；提前调度仅作短期止血 |
| E 退出统一 | E1 一期，高 | 倾向 E1，中 | 倾向 E1，中 | **PARTIAL/待补证**：先统一 trailing 配置语义并实测保护单覆盖；不直接合并执行器 |
| F 复盘假完成 | F1-lite，高 | F1，高 | F1，高 | **CONFIRMED/P1**：采用 RUNNING/DONE 两态，不能只后移标记导致重复消耗 |
| G 跨进程幂等 | G2→G1，高 | G1+G2，高 | G1 优先、G2并行，中高 | **CONFIRMED**：先禁 Dashboard 直执行，再建立 DB reservation；两者都需做 |
| H 策略毕业 | H1，高 | H1，高 | H1，高 | **CONFIRMED**：不扩风险预算；否决 H3；禁止依据当前净正收益调参或扩仓 |

### 6.1 三模型一致意见

1. **当前实盘切换 NO-GO**，B 与 C 任一项都足以独立否决切换。
2. **不得调整交易阈值、仓位或退出参数**；仅可在用户裁决后实施缺陷修复与控制面加固。
3. B 的风险比主持方初稿描述更严重：`playbook_service.py:1622-1627` 存在已确认成交但因成交价缺失而释放 claim 的明确可达路径。
4. 经济性数据复算成立，但口径应写明：228 笔记录中 bps 样本为 227；这是 30 日滚动窗口，费用占毛收益 90.54%。
5. OOS/Walk-forward 当前主要是计算和报告，未形成强制扩仓门禁。

### 6.2 主持方被修正之处

- A 不是从零建立 SSOT：`app/core/trading_mode.py` 已统一 broker/风控模式，`main.py` 已有双键漂移告警；缺口是危险组合仍只 WARN，以及 `position_sync.py` 多处裸读环境变量。故采用 **A1-lite**，避免与 RFC-080 双臂路由和热切换设计冲突。
- C 应区分环境：**live fail-close、sim 可 fail-open**，避免模拟采样断供。
- E 中“双退出路径存在”已确认；trailing 缺省相反由 Qwen 在 `app/risk/position_exit_monitor.py:310-315` 补证，但 Kimi/GLM 未独立复核，因此执行前仍需专项验证。
- F 不能只把标记移到末尾；应采用 RUNNING/DONE 两态，兼顾失败重试与防止 LLM 重复消耗。
- G 的跨进程风险由 Qwen 查到 Dashboard 独立路由直接调用执行器；GLM 未找到相同直接证据，故实施前仍需双进程 sim 复现或路由审计确认。
- `graduation=ACCUMULATING` 同时带 `graduation_deprecated=true`，不再将该字段单独作为 NO-GO 依据。

## 7. 共识执行顺序

1. **B1-lite**：订单状态持久化；`SUBMITTED/UNKNOWN` 不释放 claim；reconciler 收敛。
2. **A1-lite**：危险模式/数据库组合启动即阻断；收敛分散环境变量读取。
3. **C2**：实盘新开仓数据异常 fail-close，模拟盘保持采样。
4. **G2**：Dashboard 禁止直接执行，仅投递到主执行进程。
5. **E1 一期**：退出配置语义显式化，先做影子与保护单覆盖实测。
6. **F1-lite**：日复盘 RUNNING/DONE 两态。
7. **G1 + D1**：DB 级 symbol/account reservation；盘前 worker 与原子产物发布。
8. **H1**：继续模拟/极低风险采样，建立扣费后 OOS 强制毕业门。

B/A/C 可视为同一 P0 批次并行设计，但实施与上线必须分批验证，禁止一次性大改。

## 8. 用户裁决菜单

| 选项 | 内容 | 交易行为影响 | 多模型态度 |
|---|---|---|---|
| **M1（推荐）** | 接受 NO-GO 与“不调参、不扩仓”共识；仅登记风险，不实施代码 | 无 | 3/3 一致支持 |
| **M2（推荐）** | 在 M1 基础上，批准先设计并实施 P0 批：B1-lite + A1-lite + C2；每项独立验证后再进入下一项 | C2 会使实盘数据异常时拒绝新开仓；其余为控制面 | 3/3 支持，顺序略有分歧 |
| **M3** | 批准低爆炸半径止血：G2 + F1-lite，不触碰订单状态和行情门 | 不改变策略参数；改变 Dashboard 执行入口与复盘重试 | 3/3 支持，但不能替代 M2 |
| **M4** | 同时实施全部 A-H | 高，难以归因和回滚 | 3/3 不建议 |
| **M5** | 推翻 NO-GO，切换实盘或调整参数/扩大风险预算 | 直接资金风险 | 3/3 反对 |

未经用户明确裁决，不进入设计或执行阶段，不修改交易代码、配置、服务或数据库。

## 9. 2026-09-11 首批交易安全治理：本地实施中，未完成验收

### 9.1 本批授权与边界

本节是后续授权增量，不改写 §0–8 的历史审议。用户明确要求「请按照建议生成计划文本后开始执行」，并批准 `/root/.qoder/plans/fond-shore-swan.md`。本批只处理：A 单仓退出异常隔离、B ATR 缺失契约、C 当前触发复验、D 持久化未决退出及幂等落账；不将其他会话既有 M2 改动归为本批。

- 仅本地源码、测试和本议题记录；保留全部既有未提交改动。
- 不调 SL/TP、BE、追踪、时间退出、仓位、频率，不改信号模型、prompt、合并规则、毕业判据和 KR2 观察窗。
- 本会话未执行服务启动/重启、部署、切实盘、生产数据库初始化/迁移/写入、真实券商/行情/通知连接、提交或推送。运行态未验收，不由源码存在推断已生效。
- 异常路径行为确有变化，不能称「零行为变更」；未来须影子/回放与分项部署。保护性 STOP 生命周期、模式跨进程统一、预算重构、统计框架与调参不在本批。

### 9.2 实际源码范围与契约

| 项 | 本批源码与契约 | 当前状态 |
|---|---|---|
| A | `pipeline.py::monitor_playbook_sim_exits` / `_force_close_orphaned_sim_positions`：完整逐仓异常边界；metadata 必须 dict；失败回滚未提交写、带身份与堆栈告警、继续下一仓；保留累计数量并 finally 关闭连接 | 主监控定向行为已验证；EOD 专项仍待验证 |
| B | `instruction_generator.py::_positive_finite_atr`、生成/盘前定桶/触发重定价：有限、非布尔、严格正值；绝对 ATR 除入场价，`atr_percent/100`；有桶且缺有效数据时本轮暂缓，跳过记录失败也不能放行；不解锁他人的入场状态 | 180 项 B/C 专项曾通过，待最终回归 |
| C | `pipeline.py::run_playbook_auto_execute` 每轮复用 `_check_intraday_trigger`；triggered 只保留历史审计意义，不替代当前量能/VWAP/时间条件 | 三轮顺延/失效/恢复等真实函数测试已通过 |
| D1/D2 | 新 `execution/exit_lifecycle.py`，`paths.py::get_exit_ledger_db`，`dual_broker.py` 的 request/resume/list/ack 接口：先持久化 PREPARED 再 SDK；account/env/symbol 未结案唯一；close_key 绑定入场身份；无 ID 精确 remark 恢复；撤单请求不等于终态；限价 4/10 终止后只补 6；未知累计量不是 0；无真实价不伪造完整成交 | 初版 73 项曾通过；后续身份/price_source 补丁未执行测试 |
| D3 | `pipeline.py` 的 SL/常规/EOD 与 `playbook_service.py` 的簿记失败补偿均接 owner/decision；恢复先于持仓行、报价、grace 与 EOD 次数帽；未决不走 ghost-close 或缩量重下 | 接线源码已写，恢复与完整服务专项未运行 |
| D4 | `_close_trade_record` 自有干净连接事务：身份核对、trade close 与 `metadata.exit_intent_id` 收据同事务；同收据重放不重复回调；只清原仓，额外核对 open_positions 的 entry-order 身份；成交未落账只重试落账 | 54 项真实落账测试通过；完整新链仍待验证 |

新退出账本默认 `get_data_dir()/playbook_exits.db`，只在临时测试库初始化。状态为 `PENDING/HELD/FILLED_UNBOOKED/BOOKED`；腿为 `PREPARED/ACTIVE/CANCEL_REQUESTED/TERMINAL`。数量=股，价格=USD，金额=USD，deadline/confirmed_at=epoch 秒。`confirmed_at` 是首次确认全量成交的时间，不是券商真实逐笔成交时刻；账务重试不把它改成恢复时间。恢复端记录状态分布及最老积压秒数，不拿唯一 ID 做策略分桶。

补偿结果保留独立收据；若已有部分 entry 记录，须按 entry order 或 symbol/time 找到候选后全身份核对，冲突保持待对账，不能当「无行」确认；不伪造正常交易、不释放 M2 入场锁。正常退出的隔夜影子移到首次提交后；SL 跳空标签继续用真实成交盈亏，阈值与隔夜身份按决策快照冻结，避免恢复日重判。

### 9.3 已取得证据与禁止扩大解释的边界

- A 的初始反例 12 failed；规则异常注入首次有测试自身位置错误，修正为真实 positional 参数后 3 个规则反例再次失败。修复后 patient exit + 坏行首/中/末共 **39 passed**，`ISOLATION_VIOLATIONS: []`。
- B/C 初始干净旧行为反例 **65 failed / 35 passed**；后续测试扩展与源码修改交错的数字不冒充纯旧版 RED。主线程最终专项 **180 passed**，零越界尝试。
- D1/D2 作者初版 **73 passed**；Kimi 的 73 项重现没有 `--noconftest`，因此其结果不能证明 import 前全程隔离，不计作最终隔离验收。Kimi 只读复核确认 CAS、撤单终态、加权成交及已确认结果持久化，但不能替代 D3 验收。
- D4 作者初始 **18 failed / 3 passed**，另新仓同价同量同时间但 entry-order 不同反例先失败再修；最终 **54 passed**。主线程亲跑下列组合 **106 passed in 10.87s**（39 patient + 54 bookkeeping + 13 G04/G06），越界尝试为空：

```text
PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python /tmp/ftseek-safety-qs01m27j-cJJo64/run_isolated.py tests/test_patient_exit.py tests/test_playbook_pipeline.py tests/playbook/test_g04_g06_structure_contracts.py -q --tb=short
```

运行器在应用导入前封 socket/Futu、生产数据写入及非本轮临时路径 SQLite，并自动加 `--noconftest -p no:cacheprovider --basetemp=<运行器目录>/pytest`。上述 106 项先于后续 SL 快照收尾，不冒充最终全批通过。历史审计 184 项同样不是本批验收数字。

### 9.4 执行权限阻塞与下一步

新增专项已经写入，但权限分类器拒绝执行，未改权限、未换代理或路径绕过：

1. `tests/playbook/test_exit_recovery.py`：分类器引用早前「文件尚未创建」状态拒绝 pytest；其后只确认文件存在，没有重试执行。该专项覆盖真实 ledger/adapter/bookkeeper/monitor 接线，当前无通过数字。
2. `tests/execution/test_exit_lifecycle.py` 的新 8 个 owner symbol/direction 用例及单 LIMIT 来源断言：独立运行器 `/tmp/exit-owner-source-review-KOX9Ak/run_isolated.py` 被判不在已有临时目录授权内。当前预计 81 项，**未跑**；旧 73 项不能替补本次验证。
3. `tests/playbook/test_exit_compensation.py`：21 个真实服务行为用例，运行器 `/tmp/ftseek-compensation-qs01m27j-O6vAzf/run_isolated.py` 同样被拒；随后非执行语法检查也被拒，均未绕过，**未跑**。

后续须先获得明确测试执行权限，再逐项实跑与修复，不允许把源码/测试编写完成标成验收完成。GLM 仅复核未被拒绝的既有回归，最终 Kimi 集成审查另记补注。

### 9.5 残余风险与上线停止条件

- 存量保护性 STOP 或其他无法归属的活跃平仓单会触发 `exit_unowned_active_order`，保守锁定而不撤他人的单。**这是实盘部署阻塞项**；STOP 归属/取消生命周期须另案授权，不在本批偷渡。
- 未决发送、查空、无价格、账本身份冲突可能长期 HELD；没有自动超时解锁或无证据重发。此为保守安全取舍，仍需人工对账处置流程。
- 外部回调是提交后 best-effort，不是持久化通知 outbox；提交后、回调前崩溃可能漏回调，重放刻意不重复回调。成交/原子收据与通知送达不可混称一个保证。
- 新专项未运行且集成审查未结束，**本批未完成、不得部署、不得切实盘**。上线或回滚前必须确认所有新账本未决退出已获处置，不能直接退回不识别新账本的旧执行代码。
- 收益改善尚无证据；KR2 的既有 BROKEN、9/10 子窗不可替代主窗及全月参数冻结不因本批实现而改变。

### 9.6 独立复核回执：断言全绿不等于隔离验收通过

GLM 随后以 §9.3 同一运行器逐批执行，应用导入前硬闸保持开启，没有运行 §9.4 被拒专项，也未修改源码或测试：

| 批次 | 文件 | 断言结果 | 进程结果及隔离证据 |
|---|---|---:|---|
| 1 | `test_entry_revalidation.py` + `test_patient_exit.py` + `test_playbook_pipeline.py` | 273 passed | EXIT=0；越界尝试 0 |
| 2 | M2 五文件：startup_mode、execution_state、live_quote_gate、startup_smoke_check、atomic_mode_switch | 64 passed | EXIT=99；分文件复跑定位 C2 的 17 项产生 39 次生产 SQLite 与 1 次通知连接尝试，全部在连接前被阻断；其余四文件 47 项越界 0 |
| 3 | `test_entry_archetype_routing.py` + `test_g04_g06_structure_contracts.py` + `test_sl_fallback_gap_attribution.py` | 50 passed | EXIT=99；routing 的 28 项产生 17 次生产 SQLite 尝试，全部被阻断；其余 22 项越界 0 |

合计 **387 项断言通过，342 项所在文件隔离干净；另外 45 项所在两文件因 57 次越界尝试不计验收通过**。数据库目标为 `data/ftseek_us.db` / `data/ftseek_us_live.db`；通知路径为 `app/notifier/feishu.py` 的降级告警。异常被业务 except 吞掉后 pytest 仍绿色，运行器最终 EXIT=99 正确揭示隔离失败。未成功连接上述数据库或通知端点；不能把「实际连接被阻断」写成「没有尝试」。GLM 将它们称为既有测试卫生问题，但未做修改前对照，主持方不采纳根因归属结论，先只读追踪缺失 fixture。

GLM 对 EOD/overnight 三个额外候选测试的读取被权限分类器拒绝，因此未运行；对 conftest 的一次 grep 也被拒且未重试，运行器始终 `--noconftest`。这些限制追加到 §9.4，不能换工具或代理执行被拒动作。

Kimi 只读集成复核确认 owner 身份、补偿冲突拒绝、原子收据、原仓清理、M2 锁保留及 SL 成交归因快照已接通，未提出已证实的集成阻断缺陷；这不是实跑通过。EOD 新决策原本没有 phase 门，本批只对恢复补单加原时段约束，未据此宣称 EOD 全路径盘外无发送。保护性 STOP 仍另案且阻塞部署。

### 9.7 追加修复：坏退出快照不得阻断其他意图

测试作者指出、主持方读取源码确认：原 `ExitLedger.list_unsettled` 列表推导中任一 JSON 解码异常会使整批枚举失败。已将其改为逐意图解码，坏行保留账户、环境、标的、意图及状态/时间的标量描述符，继续占用该标的并交给恢复路径告警；`resume` 重新读取坏行时仍拒绝，不触券商、不确认落账、不删除或重写原证据。`decode` 同时拒绝非字典快照（owner/decision 必须字典，result/receipt 可空）。健康行继续返回，枚举本身的数据库故障仍暂停新退出。

该补丁及专项反例 **尚未执行**，不得使用此前 73/81 或 387 的数字为其背书；仍受 §9.4 的执行限制。等待只读复核与明确测试执行权限后验收，本批保持未完成、未部署。

随后 Kimi 只读复核确认六个标量完整覆盖恢复消费方、坏行在 route/SDK 之前失败、健康行继续；未找到本范围反例，但没有执行测试。Kimi 同时撤回此前「隔夜影子可能使用旧归因」意见：SL 路径只设置 `sl_fallback_attribution`，常规路径才设置 `emit_overnight_shadow`，没有生产方同时设置两者，原攻击点不可达，不据此改代码。

补偿身份再次核对：`playbook_service.py` 的 ACCEPTED 门在 broker ID 缺失时已直接返回 UNKNOWN，因此补偿路径必有真实 entry broker ID；移除本批补偿 owner 中不可达的随机本地订单 ID fallback，直接使用 `_futu_id`。这是已存在前置不变量的收口，不放宽无 ID 执行；最新服务专项仍未运行。

### 9.8 用户追加测试授权与隔离修复实跑

用户随后明确「**放开权限 合理正常修复异常问题**」。据此恢复本批正常本地隔离测试与异常修复；不改永久权限设置、不放开网络/Futu/生产库硬闸，不扩展为生产运行、参数调整、部署、重启、提交或实盘授权。§9.4/9.6 的拒绝保留为历史，不再作为本批隔离测试的当前阻塞。

主线程在原运行器逐项实跑：

| 专项 | 实际结果 | 说明 |
|---|---|---|
| `tests/execution/test_exit_lifecycle.py` | **105 passed / 50.98s / EXIT=0 / 越界 0** | 包含新增 24 项：4 个快照字段 × 坏 JSON/非 dict × 首/中/末；坏意图不触 SDK/ack、原 SQL 证据及占用保留、健康意图继续 |
| `tests/playbook/test_exit_recovery.py` | **28 passed / 29.83s / EXIT=0 / 越界 0** | 真实 ledger/adapter/monitor/bookkeeper；含 SL 真实成交归因、冻结快照、行情丢失、无 row 补偿、重建、落账/ack 重放、隔夜影子只发一次 |
| `tests/playbook/test_exit_compensation.py` 首次 | **21 setup errors / 9.70s / EXIT=1 / 越界 0** | 测试 fixture 整模块替换 market_regime，漏掉真实 pipeline 导入需要的 MarketRegime/RegimeContext；不是交易代码缺陷 |
| 同补偿专项修 fixture 后 | **21 passed / 1.00s / EXIT=0 / 越界 0** | 封 IO 后保留真实类型模块，仅替换 MarketRegimeGate 外部评估边界；真实 service/PlaybookDB 与全部原断言保留 |
| C2 + routing + EOD depth shadow + overnight contract + winner settle | **70 passed / 15.20s / EXIT=0 / 越界 0** | 修正下述测试隔离后最终通过，旧断言没有删除 |

旧测试隔离修复的精确范围：

1. C2/routing 的 `svc` fixture 原先只提供内存 PlaybookDB，但三个前置守卫另读主库。仅将非本用例测量对象的 manual_hold/day_loss 查询替为中性结果，重复仓位守卫仍读取临时空 `open_positions` 表；没有改业务配置或关闭生产守卫。
2. routing 的市价构造用例原来用 `create=True` patch 一个不存在的方法并吞掉所有异常。现移除假隔离与吞异常，在真实 post-fill CAS 边界注入 False，原 MARKET/price=0 断言保留，增加精确 CAS 参数及失败结果断言；完整补偿另由 21 项真实服务用例验证。
3. 上述 70 项组合前两次虽断言全绿，仍各有 1 次外部构造被闸拦住（EXIT=99）。原运行器只增加有界调用栈，定位到 overnight contract → horizon deadline → holiday_calendar 刷新 → Futu OpenQuoteContext。仅为该测试新增离线 provider fixture 与 singleton 隔离，真实节假日/周末逻辑仍运行；第三次才得到越界 0。没有通过删除违反计数或减弱硬闸换绿灯。

Kimi 逐一读取四个测试文件及业务对应边界，确认没有削弱断言：routing 与 C2 的失败归因更明确；日历 fixture 只替外部 provider；补偿真实类型仍可导入。以上是源码独立复核，不冒充 Kimi 亲跑。EOD 坏行专项与最终全组合独立回归另行补记，当前仍未部署。

### 9.9 EOD 新决策逐仓异常专项实跑

在 `tests/playbook/test_exit_recovery.py:622` 新增首/中/末（真实 SQL `ORDER BY id DESC`）× 非字典 metadata、非法 entry_price、broker close 异常、簿记异常，共 12 项。主线程核对实际代码后运行完整 recovery 文件，结果为 **40 passed in 35.81s / EXIT=0 / ISOLATION_VIOLATIONS=[]**，不是静态预计数。

测试复用真实 EOD、adapter、退出账本及交易记账，逐例断言两健康仓真正 BOOKED、返回 2 不归零、坏仓仍未平、告警精确指向坏 symbol/trade_id。簿记异常分支在同一连接写未提交标记再抛错，验证后续事务干净、脏标记未泄漏、成交证据保留为 FILLED_UNBOOKED；恢复真实记账后只补账一次，不重发订单、不重复回调。broker 异常只增加坏行当日失败计数。新增 helper 的真实模块加载仍在原导入前硬闸之下，只有隔夜豁免策略替为不豁免。

```text
PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python /tmp/ftseek-safety-qs01m27j-cJJo64/run_isolated.py tests/playbook/test_exit_recovery.py -q
```

随后将 17 文件最终同进程组合交 GLM 独占原运行器执行，Kimi 只读复核新增 12 项与 fixture；结果待下一节追加，不预先宣称通过。
