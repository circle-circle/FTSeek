# RFC-196 盘前生成供给闸 + 长步护栏 (2026-11-10)

状态: **已落地** (代码 / 配置 / 测试同批)
分类: L1 缺陷修复 (让"本来不可能生效的动作"不再发生; 不改任何已生成指令的方向/入场/SL/TP/size)
关联: DEB-fullchain #22 (完成标记), RFC-195 (盘前生成 NameError), DEB-20260903 §六 (D4(a) 跨界兜底)

## 1. 事故 (2026-11-09, logs/ftseek_us.log-20261110)

| ET | 事件 |
|---|---|
| 08:33 | DA 完成供给, 直写 22 条 `playbook_instructions` (无完成标记) |
| 09:01 | 服务侧 generate 判 "已有 22 行但无完成标记 (UE 先写/上次中断), 继续生成" → 重跑全量 |
| 09:01–10:35 | 每轮先烧 ~300s 再崩 (`trade_date` NameError) → **共 18 轮 / 94 min** |
| 09:31 / 09:33 | watchdog: 跨开盘未收轮 → 停心跳 → 判真卡死 (入场执行被阻塞) |
| 09:40 | 首轮 auto_execute 才启动 (晚到 ⇒ chase_cap 拦掉 ABNB/GS/NEM/AMZN) |

历史同族: 11-05 每日 1 次误重生成 (`09:00:20 → 09:06:50`, 6m30s 天天顶到开盘);
09-03 generate 曾跑 30.5min; 11-02 进程挂起 SIGKILL。

## 2. 根因 (三层, 各自实证)

1. **点火源**: `compute_instruction_values()` 传了 `trade_date=` 但无该形参 ⇒ 每轮必崩。
   已由 RFC-195 (38e7827f3) 修复并部署。
2. **放大器**: 崩溃不写完成标记 ⇒ 下一轮再次判"上次中断" ⇒ 无上限、无截止地重做长步。
3. **白烧**: 即使不崩, DA 已供料的日子每天仍白跑一轮 300–400s 的全量生成
   (筛 69 只 + 基本面预取 + horizon LLM), 只产出 `supply_placeholder` 占位行。

## 3. 改法 (三处, 全是"少跑一步"的减法)

| # | 改动 | 判据/取值 | 作用 |
|---|---|---|---|
| 1 | `supply_gate.da_supply_state()` | 当日 bridge 候选 symbol **⊆** 当日 DA 指令行 ⇒ `complete`; 缺任一 ⇒ `partial` (仍重跑) | 供给已完整落库 ⇒ 写 `source='da_supply'` 标记并跳过全量生成 |
| 2 | `generate_latest_start_et_hhmm: 920` | `playbook.execution_parameters` | ET 09:20 后不再启动长步 (再启动必跨开盘) |
| 3 | `_GENERATE_MAX_ATTEMPTS = 2` | 进程内按日计数 | 崩溃循环最多 2 轮即停, 打 ERROR 一条 |

### 3.1 为什么必须用"落库事实"而不是"bridge 文件存在"
8/17 事故是 bridge 写成功、落库被覆盖归零 —— 当时唯一自愈路径正是重跑 generate
(BridgeVotingAdapter 把 bridge 候选重新落库)。故判据取 `bridge 候选 ⊆ 已落库 DA 行`,
缺行必重跑; `supply_placeholder` 行**不计入**已落库 (否则缺口被占位掩埋)。

### 3.2 显式登记的边界 (改配置前必读)
- **组合闸 (`playbook.portfolio_guardrail_enabled`, 方向偏斜 / sector 集中度) 开启时禁止跳过** ——
  该闸只在生成侧 `_apply_portfolio_guardrails` 执行 (9 月 45 条 / 10 月 20 条 DA 行被它降级),
  跳过 = 静默摘掉一层风控。代码里 `guardrail_blocks_skip()` 读失败按"开启"处理 (宁多跑一轮)。
- 跳过生成 ⇒ 当日不再产生 ~55–62 条 `supply_placeholder` (纯研究口径消费方:
  `scripts/non_nominated_counterfactual.py` / `observe_nightly_supply.py`),
  且不再由生成侧二次改写未执行 DA 行的 horizon / entry 锚点 (实测 11-09 10:40 改写 IBRX/LMT 两行)。
  DA 行本身 (方向/入场/SL/TP/size) 由 DA 直写 + `save_instruction` 仲裁决定, 不受本批影响。
- `force=True` 一律放行 (人工应急通道, 不被三道护栏拦)。

## 4. 验证

| 项 | 证据 |
|---|---|
| 新进程回读配置 | `latest_generate_start_hhmm() = 920` |
| 真实数据判据 | `da_supply_state`: 11-03 16/16, 11-05 18/18, 11-06 20/20, **11-09 22/22 complete** (也正是当日跳出死循环的充要条件); 11-10 (bridge 未产出) → `absent` ⇒ 生成照常 |
| 组合闸现状 | `guardrail_blocks_skip() = False` (配置 `portfolio_guardrail_enabled: false`) ⇒ 跳过路径当前生效 |
| 契约测试 | `tests/test_generate_supply_gate.py` 13 例: 标记跳过 / 完整跳过+写 source / 部分仍生成 / 组合闸开启不跳过 / 截止拦截 / 截止前放行 / 尝试上限 / force 放行 / 占位行不算落库 / 组合闸读失败保守 / 缺 bridge 视为 absent |
| 变异验证 | `_GENERATE_MAX_ATTEMPTS` 改 99 ⇒ `test_attempt_cap_stops_crash_loop` 变红 (证明该例真绑定) |
| 回归 | `test_morning_push_generation_gate` + `test_pipeline_batch_e_contracts` + `test_pipeline_size_ratchet` + 本批 = 29 passed; `pipeline.py` 仍 3700 行 (棘轮未破) |
| 活体接线 | **TOO_EARLY**: 部署时刻 16:0x ET, 闸只在 ET 09:00–09:20 盘前轮被调用。首个可判时点 = 11-10 09:00 ET, 应见 `DA供给已完整落库 N/N 候选 ⇒ 视作生成完成, 跳过全量生成 (RFC-196)` |

## 5. 回滚
- 供给跳过: 删 `generation_flow.py` 的供给判定块 (或让 `guardrail_blocks_skip()` 恒 True);
- 截止: `generate_latest_start_et_hhmm: 2359`;
- 上限: `_GENERATE_MAX_ATTEMPTS = 999`;
- `playbook_generate_state.source` 列留着无害 (只多一列留痕)。

## 6. 未纳入本批 (需另行裁决)
- **P1 D4(c)**: 把 generate 的重活移出调度轮 (worker/独立进程) —— 本批只是"不让它跨开盘",
  未改变"它仍在执行轮内同步跑"这一结构事实。
- **horizon 双作者**: DA 硬写 `short`, 生成侧每月仍会改写 2–10 行为 `long`。
  统一作者后再评估是否彻底关掉生成侧二次判定。
- VIX 代理口径 (17.88 vs 真实 ~28) / regime 不 gate 方向 / `exit_order_evidence_pending` 噪声:
  与本事故无关, 维持既有 DEB 待办。