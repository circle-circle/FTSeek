---
title: 真钱闸五模型审计共性点：RFC-212 的六条限额在签署日是否真的能绑住真钱
status: active
module: playbook/live_arm
related_code: app/playbook/s1_arm.py, app/playbook/live_arm_gate.py, app/playbook/pipeline_parts/auto_execute_flow.py, app/playbook/pipeline_parts/risk_gates.py, app/playbook/playbook_service.py, app/core/paths.py, config/unified_config.yaml
related_rfc: RFC-212
tags: [llm, audit, multi-model, consensus, realmoney, live-arm, s1]
updated: 2026-11-16
---

# DEB-20261116 — 真钱闸（S1 空头臂 / LIVE 通道）多模型审计共性点

- 日期：2026-11-16 CST（ET 周日，休市）
- 状态：**只读归纳，生产零变更**（本文不含代码/配置改动，未重启，未写交易库，未连券商）
- 审计语料：`/root/bench-qwen4/`（同一 22.2k 上下文、同一 schema，五个模型各自独立跑）
  - 第一梯队：`qwen4-max`(13 条) / `deepseek-v4.1-flash`(14) / `kimi-k3.1-pro`(7) / `glm-5.3-prime`(8, effort=high)
  - 追加：`qwen3.8-max`(11)
  - 原始产物：`results/`（第一批）、`results2/`、`results3/`（无限制流式跑）、`results_cph/`（AtCoder）
- 审计对象：`s1_arm.py` → `auto_execute_flow.py`（S1 接线与 LIVE 镜像）→ `risk_gates.py` → `live_arm_gate.py` → `playbook_service.execute_instruction` → `paths.get_trade_records_db_for_arm`；附 `config.py` 与 `unified_config.yaml:1300-1336 / 1700-1748`
- 方法：跨模型按**机制**聚类 → 逐条读 `file:line` 复核 → 只读运行时探针 → 只把 **≥2 模型提出且复核成立** 的记为共性；单模型独家的单列；被证伪的单列在 §7。
- 前序：`DEB-20261110-cross-model-common-findings.md`（同一方法用于退出链）、`RFC-212-live-probe-s1-short-arm.md`（本臂的预注册）
- 复核：2026-11-16 第二轮只读复核（独立重跑探针 1 + 双库统计复算）——C1–C14/X1–X3 机制断言**全部成立**；修正 4 处行号漂移与 2 处数据漂移（D1/C9）。另实测破例文件当前 `waiver_expired` ⇒ 现阶段连 LONG 工程探针也被 waiver 门拒（与探针 2 判定一致）。

---

## 0. 摘要

五份审计措辞差异很大，但叠在一起只说明一件事：**RFC-212 授权的六条限额里，四条半在真钱链路上根本没有执行点。**

不是"某条阈值算错了"，而是三段契约断裂：

1. **闸装错路**：S1 闸（`cycle_state` / `check_entry` / `cap_size`）全仓只有一个调用点，且硬编码 `target_env="sim"`；真钱走的是另一条镜像路径，那条路径上的三道门**入参都没有股数与价格**。
2. **账本错位**：敞口与 kill switch 读的是 sim 研究账本，真钱成交落 `ftseek_us_live.db`，永不进入这两项计算；kill switch 的人群甚至不区分"是不是本臂的空单"（实测混入 `MAIN_PIPELINE 109 / SCALPING 72 / ORB 5` 笔）。
3. **口径自证**：S1 模块的 docstring 写着 P9 fail-close，但 `cap_size` 在价格≤0 时 fail-open、`count_open_live_positions` 在账本缺失时返回 0；配置层还留了一份 `DEFAULTS` 与 YAML 同值双写。

五家模型对 `limit_matrix` 的判定高度一致（见 §2），这是本轮最强的交叉验证：**独立上下文、独立推理，得出同一张"限额失效图"**。

---

## 1. 判据与取证

- 记号：`✅` 代码复核成立；`⚠️` 成立但影响有条件/当前无数据；`❌` 复核不成立。
- 覆盖判定按 **findings 实质内容**计数（同一模型多处提及只记 1），不按关键词命中。
- 只读探针（不改状态、不连券商，可任意重跑，命令见附录 C）：

```
# 探针 1：按 RFC-212 授权参数调用真钱门
evaluate_live_arm_gate(mode="strategy_probe", probe.direction="SHORT",
                       max_order_qty=5, max_open_positions=2,
                       instruction_direction="SELL_SHORT", market_phase="TRADING",
                       open_live_positions=0, halted=False)
→ allowed = False
  LIVE_MODE_UNSUPPORTED          Unsupported LIVE mode: 'strategy_probe'
  LIVE_PROBE_DIRECTION_REJECTED  First engineering probe accepts LONG/BUY only
  LIVE_PROBE_LIMIT_INVALID       Engineering probe requires max_order_qty=1 and 1<=max_open_positions<=3
  LIVE_PREREQ_INCOMPLETE         ALLOW_REAL_TRADING / REAL_ACCOUNT_ID / lane incomplete

# 探针 2：按现行配置（engineering_probe + ALLOW_REAL_TRADING=true）发 SELL_SHORT
→ allowed = False
  LIVE_PROBE_WAIVER_INACTIVE / LIVE_PROBE_DIRECTION_REJECTED
```

---

## 2. 六条限额的判定（五模型一致 + 复核）

| 限额 | 判定 | 决定性证据 |
|---|---|---|
| 只做空（SELL_SHORT） | **真钱侧 OVER_BLOCKS** | `live_arm_gate.py:235` `if probe_direction != "LONG" or instruction_direction != "BUY"` |
| 入场 ET≥09:45 | **SILENTLY_DISABLED**（qwen3.8 判 PARTIAL：SIM 黑窗 09:45 可间接影响入口） | 唯一判定在 SIM 分支 `s1_arm.py:237`，经 `auto_execute_flow.py:1637` 调用；真钱门只有 `market_phase=="TRADING"`（09:30 起成立） |
| ≤2 笔/日 | **SILENTLY_DISABLED** | 计数来自 `_today_opened_count`（全流向），只在 SIM 循环里比对 `s1_arm.py:239-241`；真钱门无日计数 |
| 单笔 ≤$4,200 | **PARTIAL（见 §8 X1）** | `cap_size` 结果经 `_reprice_params["position_size"]` 传给真钱执行（`auto_execute_flow.py:2020 custom_params=_reprice_params`）；但非 reprice 路径回落到镜像行的 `1` 股（`risk_gates.py:502`） |
| 总敞口 ≤$8,400 | **SILENTLY_DISABLED** | `open_exposure_usd` 只被 `cycle_state("sim")` 调用；真钱门只有一个行数帽（`live_arm_gate.py:272`） |
| kill switch（−$300 / 前 15 笔） | **SILENTLY_DISABLED** | `cohort_state` 读传入 ledger（实际只传 "sim"）；真钱亏损不进该人群，且 `halt_live_new_positions` 在本臂无调用点 |

---

## 3. 四方以上共识（≥4/5，复核成立）

### C1 S1 闸只接在 SIM 循环，真钱路径完全不在闸内 — P0

- 机制：`cycle_state(` 全仓唯一调用点 `auto_execute_flow.py:450`，字面量 `"sim"`；`check_entry(`/`cap_size(` 只有 `:1637`/`:1641` 两处；SIM 执行 `:1905` 不带 `target_env`（落 `AUTO_TRADE_ACCOUNT=sim` 分支 `playbook_service.py:1546-1565`）；真钱唯一入口是 `:2021` 的镜像。
- 复核：✅ 全仓 grep 确认无第二处调用、无 `cycle_state(..., "real")`。
- 影响：六条限额只作用于研究账本；真钱单即使发得出去也不受约束。
- 最小修复：镜像前用 `cycle_state(today, count, "real")` 建臂态并对镜像指令跑 `check_entry`/`cap_size`。
- 备注：`:449` 注释自己写明"须另建 target_env='real' 的 cycle_state" —— 已知未落。

### C2 真钱门只接受 LONG/BUY，真钱空单结构上不可能 — P0（对应 RFC-212 C0）

- 机制：`live_arm_gate.py:235` 方向硬编码；`mode` 只接受 `engineering_probe`（`:193-195`）；`max_order_qty != 1 ⇒ LIVE_PROBE_LIMIT_INVALID`（`:242-258`）。
- 复核：✅ 探针 1 实测四条拒绝原因。
- 影响：$4,200 在现有门里无法表达；授权的方向被拒，未授权方向（LONG）反而是唯一能通过的。
- 最小修复：新增 `mode=strategy_probe` 分支（方向/单笔/日笔/敞口全部来自 config）；`engineering_probe` 分支逐字保留 + 契约测试钉住。

### C3 镜像行把股数写成 1 — P1（但强式断言被 §8 X1 推翻，见下）

- 机制：`risk_gates.py:502 mirror["position_size"] = 1`。
- 复核：✅ 代码事实成立；⚠️ 但"真钱一定 1 股"不成立 —— 执行时 `custom_params=_reprice_params` 会覆盖（§8 X1）。
- 影响：非 reprice 路径 = 1 股（额度远低于授权）；reprice 路径 = S1 缩股后的股数（可达 $4,200）。
- 最小修复：镜像处显式传 S1 帽、并在 metadata 打臂标记，不要依赖"恰好有 custom_params"。

### C4 真钱门签名里没有股数/价格/敞口/盈亏 — P0

- 机制：`evaluate_live_arm_gate(arm, *, allow_real_trading, real_account_id, lane_db_name, instruction_direction, market_phase, open_live_positions, halted)`；`max_order_qty` 校验的是**配置值**不是订单量（`:242-257`）。
- 复核：✅ 签名 + `_evaluate_live_execution_gate(direction)`（`playbook_service.py:315`）双重确认。
- 影响：结构上无法执行单笔名义、总敞口、累计亏损三条。
- 最小修复：新增 `order_qty / entry_price / arm_open_notional` 入参并在两处调用点传入。

### C5 kill switch 只读 sim 账本，真钱亏损永远进不了停止条件 — P0

- 机制：`s1_arm.py:209 st["cohort"] = cohort_state(target_env, ...)`，`target_env` 恒为 "sim"；`kill_reason` 的消费方只有 `check_entry`（SIM）。
- 复核：✅ 真钱成交写 `ftseek_us_live.db`（`paths.get_trade_records_db_for_arm`），与 sim 人群不相交。
- 影响：−$300 / 前 15 笔两个停止条件对真钱无效。
- 最小修复：真钱侧独立 `cohort_state("real", start_date)`；`kill_reason` 非空时 `halt_live_new_positions(...)` 持久化。

---

## 4. 三方共识（3/5，复核成立）

### C6 kill switch 人群混入非本臂空单 — P1

- 机制：`S1_WHERE`（`s1_arm.py:61-67`）仅排除 `POSITION_SYNC/MANUAL`，无臂/来源标记。
- 数据：`trade_records` 的 SHORT 来源实测 `PLAYBOOK_SIM 181 / MAIN_PIPELINE 109 / SCALPING 72 / ORB_ENGINE 5 / PLAYBOOK_SHADOW 5`（`data/ftseek_us.db`）。
- 影响：一旦开闸，停止条件由别的引擎盈亏驱动（可能早停，也可能被盈利行掩盖亏损）。
- 最小修复：按 `metadata.dual_arm.mode` 或 `source_type` 过滤到本臂。

### C7 轮内记账不分方向（敞口与日笔数）— P1

- 机制：`auto_execute_flow.py:1920-1922` 任何方向成交都执行 `_s1_st["today_count"] = _today_opened_count`（全流向）与 `_s1_st["exposure"] += _new_notional`。
- 数据：11 月指令 BUY 133 / SELL_SHORT 33 ⇒ 多头通常先吃光当日额度。
- 影响：方向是 fail-safe（只多拦不少拦），但足以让实验零样本；`today_count` 口径即 RFC-212 P13。
- 最小修复：两个累加都加 `if inst["direction"] == allowed` 守卫，计数改从臂账本查询。
- **进度（2026-11-16）**：日笔数半边已按用户授权的重设计落地（RFC-212 §5.8「一条账本、一个锚点、一个增量」）——`cycle_state` 删全流向计数器形参，日笔数=臂账本今日 SHORT 成交谓词，轮内唯一增量只计臂方向；契约 49 条含变异验证。**敞口半边故意不动**：全方向口径是已裁保守面（RFC-212 §5.4），与本条"最小修复"的差异属有意保留。

### C8 `cap_size` 在价格≤0 时 fail-open — P1

- 机制：`s1_arm.py:254-255` `if not st.get("enabled") or not size or entry_price <= 0: return size`，与同模块 degraded 分支返回 0 的纪律相反。
- 数据：当前 337 条 SHORT 指令全部有价（最高 $1,640），属**latent**，非现行越限。
- 最小修复：方向判断之后 `if entry_price <= 0: return 0`，由调用点记 `s1_arm_no_price` 跳过。

### C9 `cohort_state` 丢弃时间戳解析失败的行 — P2

- 机制：`s1_arm.py:159` `(_et_minute(...) or 0) >= 585`，解析失败 → 0 → 被过滤；注释自述库内混存多偏移。
- 数据：当前 177 条 B23 人群 `entry_time` 全部可解析（0 条坏行；首轮 178 条，复核日平 1 笔）⇒ latent。
- 影响方向：漏掉的若是亏损行，kill switch 会被低估（fail-open）。
- 最小修复：分区计数，出现丢弃行即 `degraded=True` + CRITICAL，而不是静默。

---

## 5. 两方及单模型共识（复核成立，影响较小）

| # | 内容 | 覆盖 | 等级 |
|---|---|---|---|
| C10 | `count_open_live_positions` 在账本文件不存在时返回 `0`（文档说返回 None 才 fail-close）——`live_arm_gate.py:35-36` | 2/5 | P2 |
| C11 | 授权限额在 `s1_arm.py:48-58 DEFAULTS` 与 `unified_config.yaml:1316-1324` 双写；`cfg()` 读失败回落 DEFAULTS | 2/5 | P2 |
| C12 | `s1_arm._cfg` 每次 `PlaybookConfig()`（`__init__` 内 `yaml.safe_load(playbook_horizon.yaml)`），未用现成单例 `get_playbook_config()`；每条指令 6+ 次 | 2/5 | P3 |
| C13 | 镜像 metadata 硬编码 `mode="engineering_probe"`（`risk_gates.py:495-500`），真钱单事后无法按臂归因 | 1/5 | P2 |
| C14 | S1 拒绝/缩股只在 SIM 侧落痕（`mark_instruction_guardrail_skip` / `_emit_exec_gate_audit`），真钱镜像只推通用拒绝 ⇒ "限额生效"与"限额被绕过"运维不可区分 | 1/5 | P2 |

---

## 6. 数据层共性（同一份真实统计被多家读出）

| # | 事实 | 含义 |
|---|---|---|
| D1 | 未平仓 8 笔全为 LONG、合计 $28,868，全部 `trade_date ≤ 2026-11-13`（首轮审计时 9 笔/$33,798，复核日已平 1 笔） | `s1_arm_start_date=2026-11-16` 的过滤让臂起跑敞口为 0（不会一开闸就被 $8,400 卡死）；但同一谓词无来源过滤，开闸后任何新多头都会吃臂额度 |
| D2 | SHORT 平仓来源 `PLAYBOOK_SIM 181 / MAIN_PIPELINE 109 / SCALPING 72 / ORB 5` | C6 的数据证据 |
| D3 | `exit_slippage_bps` 落库 727/1093 行；`trade_records` 55 列无"入场追价 bps"字段 | RFC-212 P11（S3 停止条件）今天无法实现，属数据前置缺失。**进度（2026-11-16）**：入场追价 bps 采集已落库（metadata `entry_chase_bps`，RFC-212 §5.11，record-only）；S3 接线仍待攒样+裁决 |
| D4 | 337 条 SHORT 指令均价 $250、最高 $1,640，`>4200` 为 0 | 单笔帽的缩股效应在小票上不明显；$4,200 只在少数高价标的上绑定 |

---

## 7. 共性误报模式（分析质量，同样值得归档）

- **M1 只看镜像行、不看执行覆盖**：五家都把 `mirror["position_size"]=1` 当成"真钱必然 1 股"，忽略 `:2020 custom_params=_reprice_params`。→ 结论必须追到**最终消费点**，不是中间载体。
- **M2 用"ABS 与无 ABS 不一致"推符号翻转**（qwen4 F9 / qwen3.8 F9 / kimi 类似）：实测 373 条 SHORT `quantity` 全为正 ⇒ ❌。
- **M3 把 `exit_time=''` 当现行漏洞**：两库 `exit_time=''` 均 0 行 ⇒ 只有口径不一致，无数据触发。
- **M4 把"已知未实现"当新发现**（glm-low F7 报 S3 未实现）：`kill_reason` 的 docstring 已明写 ⇒ 重报。
- **M5 上下文缺段导致共鸣式误判**：第一批上下文漏了 `:1918-1923` 的轮内记账，deepseek/qwen4 均报"跨指令不更新"；补齐后改报真实问题。→ 审状态机必须整段入包。
- **M6 JSON 越界引号**：qwen3.8 输出 11 条 finding 但含未转义 `"`，需一次机械修复才能解析；kimi 在 22k 上下文下必须流式（首字 870s）否则 900s/1800s 拿不到完整响应。

---

## 8. 复核增量（模型未提出或提出得不准，本轮修正）

### X1 镜像的 1 股会被 `custom_params` 覆盖 —— C3 的强式断言不成立

- `auto_execute_flow.py:2017-2023` 真钱执行同样传 `custom_params=_reprice_params`；`execute_instruction` 读取顺序为 `_cp["position_size"] if not None else inst[...]`（`playbook_service.py:1340`）。
- reprice 路径**总是**写入 `position_size`（`:1131-1137`，注释自述"否则会降为 1 股"）；S1 缩股后会覆盖成 `cap_size` 结果（`:1663-1664`）。
- 结论：**单笔帽在 reprice 路径上实际会到达真钱单（上限 $4,200）**；非 reprice 路径才是 1 股。真正裸奔的是**敞口帽与 kill switch**，不是单笔帽。
- 这条改变了修复优先级：先补 C5（真钱 cohort / halt），再谈 C3 的显式化。

### X2 非 reprice 路径的缩股写回会丢

- `:1665-1668` 在 `_reprice_params` 缺 `position_size` 时写 `inst["position_size"]`（内存字典）；`execute_instruction` 内部 `_find_instruction_by_id()` **重新查库**（`playbook_service.py:2877-2883`）⇒ 写回被丢弃，订单按库中原股数提交。
- 影响：该分支下 `cap_size` 的缩股不生效（fail-open），且轮内敞口按缩小后的名义记账 ⇒ 账实不符。
- 最小修复：始终物化到 `custom_params`（`_cp["position_size"] = _s1_qty`），或先持久化再执行。
- **进度（2026-11-16）**：已按"始终物化"落地（见 §10 第 4 步）。连带效应：S1 开启后**非 reprice 路径的缩股也能到达执行**（此前该分支缩股被丢）；LIVE 镜像同传 `custom_params`，真钱侧同样受益（P12 的 dry-run 实证仍待签署日）。

### X3 执行侧还有第二道门，同样只有方向

- `playbook_service.py:315 _evaluate_live_execution_gate(direction)`，在 `:1156`（execute_instruction 入口）与 `:1650`（dual-broker 下单前）各调用一次。
- 位置正确、参数不足：与 C4 合并看，真钱链路上共有**三处 gate 调用点**（镜像前 / 执行入口 / 下单前），没有任何一处能看见股数或价格。
- 这条把 C4 从"某个函数少参数"升级为"整条真钱链没有金额关口"。

---

## 9. 与 RFC-212 既有前置门的关系

| RFC-212 项 | 本批复核 | 说明 |
|---|---|---|
| P8 运行时臂闸改读 live 库 | ✅ 已落地（`_ledger(target_env)` + 无默认形参） | 但调用点仍恒传 "sim"，P8 只解决了"能按臂解析"，未解决"有没有人按真钱臂调用" |
| P9 读库失败 fail-close | ✅ 已落地（exposure/cohort 抛异常 → degraded；**2026-11-16 补 C8/C10**：`cap_size` 无价返 0、`count_open_live_positions` 缺文件返 None，见 §10 第 5 步进度 / RFC-212 §5.7） | 原两处 fail-open（C8/C10）已修 |
| P10 方向门不连带停多头 | ✅ 已落地 | 与 C7 的"记账不分方向"是两件事：放行对了，记账仍错 |
| P11 S3 滑点停止条件 | ⚠️ 两段式：采集 ✅（2026-11-16，RFC-212 §5.11）/ S3 接线仍 ❌ | D3 数据前置已补；接线须攒样+中位数口径裁决 |
| P12 真钱单股数归属 | ⚠️ 本批给出机制答案 | reprice 路径 = `custom_params`（可达 $4,200）；非 reprice = 1 股。仍需 1 笔 dry-run 实证 |
| P13 日笔数计数口径 | ✅ 已裁已落地（2026-11-16 用户授权重设计，RFC-212 §5.8） | C7 日笔数半边已修（账本谓词+唯一增量）；敞口半边维持全方向保守面 |
| **新增** | C1/C2/C4/C5/C6/X1/X2/X3/C11-C14 | RFC-212 P1–P13 清单未覆盖 |

---

## 10. 建议顺序（未执行，待用户批准）

1. **先补真钱侧停止条件**（C5 + C6）：`cycle_state("real")` + 臂标记过滤 + `halt_live_new_positions` 持久化。理由：X1 表明单笔帽已偶然生效，漏的是"累计亏损停不下来"，那是唯一会造成超出授权金额的路径。
   **进度（2026-11-16）**：record-only 半批已落 —— `arm_cohort_state`（source_type 臂属过滤）+ `record_real_kill_shadow` 每轮观测落 `data/shadow/s1_real_kill_shadow.jsonl`，注册项 `s1_real_kill_shadow`，契约 38 条含变异验证（RFC-212 §5.6）。**执行接线（kill→halt 持久化 + 镜像前 real 臂闸）未做**，归签署日批；halt 的全通道作用域是否限本臂须裁（P10 同族）。
2. **C0 / C2**：`strategy_probe` 分支，方向与额度全部来自 config，旧分支逐字保留 + 契约测试。
3. **C1**：镜像前跑 `check_entry`/`cap_size`，把结果显式写进镜像行（同时解掉 X1 的"偶然依赖"与 C3）。
4. **X2**：缩股一律物化到 `custom_params`，并加"库内股数 = 参数股数"断言。
   **进度（2026-11-16）**：物化半批已落 —— 缩股无条件写 `_reprice_params["position_size"]`（缺省时新建部分 dict，`execute_instruction` 逐键回退保 entry/SL/TP；下游消费点逐一核过均为 `(x or {}).get(key) or inst` 回退模式），inst 内存写回分支已删；契约 51 条含变异验证。**"库内股数=参数股数"断言未做**（需执行前读库，另批）；inert（enabled=false 不触发缩股块）。
5. **C8/C10**：两处 fail-open 改 fail-closed。
   **进度（2026-11-16）**：已落地（P9 族提前批，RFC-212 §5.7）—— `cap_size` 无价（≤0）对臂方向返 0、调用点落痕 `s1_arm_no_price`（与 `cap_zero_room` 可区分）；`count_open_live_positions` 缺文件返 None ⇒ 门拒 `LIVE_OPEN_COUNT_UNAVAILABLE`（对齐 docstring 契约）。非臂方向/关闸语义不变，契约 + 变异验证齐。
6. **C11/C12**：授权值单源（缺键即拒），配置读取走单例。
   **进度（2026-11-16）**：已落地（RFC-212 §5.9）—— DEFAULTS 双写删除，九键单源=YAML，缺键抛 `S1ConfigError` 各消费点 fail-close（degraded/拒/0/停）；配置全断时方向用 B23 冻结谓词结构身份（SHORT）兜底保 P10 不连带。**C12 修正原提案**：不用 `get_playbook_config()` 盲单例（只在 horizon mtime 变更时重建，unified 键热载不穿透 ⇒ 丢 ~60s 热载回滚），改 10s TTL 缓存构造（每指令 6+ 次构造 → ~1 次/10s）。契约 57 条含变异验证；历史参照测试加尾界防前瞻窗漂移。
7. **C13/C14**：镜像打 `mode="s1_arm"`；真钱侧落 S1 拒绝/缩股审计与告警。
   **进度（2026-11-16）**：臂归因半批已落（RFC-212 §5.10）—— 镜像 `dual_arm.mode` 改读 `live_manual_arm.mode` 单源（原硬编码 engineering_probe；读失败诚实标签 `config_unreadable`，不阻断镜像），附 `s1_arm_enabled` 快照供事后辨"镜像时限额是否生效"；门决策附 `arm_mode`，拒绝日志/推送带臂模式归因。契约 +5 条含变异验证。**后半已落（§5.12）**：dual_arm 标记传播到 trade_records 簿记层（extract_dual_arm_mark 纯函数 + 簿记 metadata 键，簿记级集成测试钉住，sim 恒 None）。**剩余归签署日**：真钱侧 S1 拒绝/缩股专属审计（依赖 C1 镜像 real 臂闸）；臂属过滤谓词升级（source_type→dual_arm.mode）另裁。

每条都建议"一次一变量 + 影子先行 + 冒烟"，并先跑 RFC-212 §5 的顺序红线（C5 最后翻）。

---

## 11. 落位与索引

- 本文落在 `docs/debates/`（跨模块决策类）。
- `docs/debates/README.md` 的索引**已陈旧**：表内 48 行但缺 `DEB-20261110`、`DEB-20261114-*` 等近期条目，因此本文**未**只插入一行（避免制造"索引是完整的"错觉）；如需索引，应整批重建。

---

## 附录 A：模型 × 共性点覆盖矩阵

| 点 | qwen4-max | deepseek-v4.1-flash | kimi-k3.1-pro | glm-5.3-prime | qwen3.8-max |
|---|---|---|---|---|---|
| C1 | ✓ | ✓ | ✓ | ✓ | ✓ |
| C2 | ✓ | ✓ | ✓ | ✓ | ✓ |
| C3 | ✓(LM) | ✓ | ✓(LM) | ✓ | ✓ |
| C4 | ✓ | ✓ | ✓ | ✓ | ✓ |
| C5 | ✓ | ✓ | ✓ | ✓ | ✓ |
| C6 | — | ✓ | ✓ | ✓ | 部分 |
| C7 | ✓ | ✓ | ✓ | ✓ | 部分 |
| C8 | ✓ | ✓ | — | — | ✓ |
| C9 | ✓ | — | — | ✓ | ✓ |
| C10 | ✓ | ✓ | — | — | — |
| C11 | — | — | ✓ | ✓ | — |
| C12 | ✓ | — | — | — | ✓ |
| C13 | — | ✓ | — | — | — |
| C14 | — | — | — | — | — |

（`LM` = 只在 limit_matrix 证据里出现；`—` = 未提出；`部分` = 提到同一现象但未落在该机制上。C14 仅 glm-5.3-prime(effort=low) 提出，未在主跑中出现。）

## 附录 B：证据索引（file:line）

- `app/playbook/pipeline_parts/auto_execute_flow.py:450` — `cycle_state(today, _today_opened_count, "sim")`
- `:1635-1668` — `check_entry` / `cap_size` / 缩股写回
- `:1905-1909` — SIM `execute_instruction`（无 target_env）
- `:1920-1922` — 轮内 `today_count` / `exposure` 累加（不分方向）
- `:2017-2023` — LIVE 镜像执行（`custom_params=_reprice_params`, `target_env="real"`）
- `app/playbook/s1_arm.py:61-67` — `S1_WHERE`（无臂标记）
- `:114-136` — `open_exposure_usd`（只 `exit_time IS NULL`）
- `:159` — cohort 时间戳过滤
- `:209-218` — `cohort_state` / `kill_reason`
- `:254-255` — `cap_size` 价格≤0 fail-open
- `app/playbook/live_arm_gate.py:193-258` — mode/方向/数量三把硬锁
- `:35-36` — `count_open_live_positions` 缺文件返 0
- `app/playbook/pipeline_parts/risk_gates.py:495-502` — 镜像 metadata + `position_size = 1`
- `app/playbook/playbook_service.py:315` — `_evaluate_live_execution_gate(direction)`
- `:1156` / `:1650` — 两处调用点
- `:1340` — `custom_params` 优先读取
- `:2877-2883` — `_find_instruction_by_id` 重查库（X2 成因）
- `config/unified_config.yaml:1316-1324` — 授权限额；`:1727-1739` — `live_manual_arm`（LONG / RFC-132 授权号）

## 附录 C：只读探针复现

```bash
cd /opt/ftseek/FTSeek-US && ./venv/bin/python - <<'PY'
import os
os.environ.setdefault("ALLOW_REAL_TRADING","false")
from app.playbook.live_arm_gate import evaluate_live_arm_gate
arm = {"enabled": True, "mode": "strategy_probe", "max_order_qty": 5, "max_open_positions": 2,
       "session": "RTH", "probe": {"authorized": True, "authorization_id": "AUTH-20261116-2POS-4200USD-SHORT-RFC212",
       "direction": "SHORT", "single_notional_cap_usd": 4200, "max_daily_entries": 2, "max_open_exposure_usd": 8400}}
r = evaluate_live_arm_gate(arm, allow_real_trading=False, real_account_id="281756480166019917",
        lane_db_name="ftseek_us_live", instruction_direction="SELL_SHORT",
        market_phase="TRADING", open_live_positions=0, halted=False)
print(r["allowed"]); [print(" -", x["code"]) for x in r["reasons"]]
PY
```

预期：`False` + `LIVE_MODE_UNSUPPORTED / LIVE_PROBE_DIRECTION_REJECTED / LIVE_PROBE_LIMIT_INVALID / LIVE_PREREQ_INCOMPLETE`。