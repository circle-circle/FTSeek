# 议题：SIM/LIVE 双臂路由「半迁移」收口 — 开关面收敛与门控单点化

## 状态

- [ ] 进行中
- [ ] 待决策
- [x] 已决策
- [x] 已验证
- [ ] 已关闭

> **性质**: L2（路由收敛，`TRADE_ENV=1` 现网下**零行为变更**，等基测试证明）。
> **不改**: 任何开关现值、任何交易/退出参数、任何判据；不触碰 C2 红线（本案是强化其执行面）。
> **承接**: RFC-090（§3 七裂缝 + §8 待裁 + §17 双臂常驻）、RFC-130（arm 门）、RFC-131（C-lite 探针）。
> **证据基线**: 2026-10-03 主会话复查（全部结论实跑验证，commit `175c3b359` + 工作树在途批）。

## 背景

RFC-090 §17 裁决双臂常驻架构后，RFC-130/131 以**纯增量方式**落地：新增一族显式臂解析器（`route_playbook_db` / `get_trade_records_db_for_arm`），但 RFC-090 §3 点名的旧三族解析器一个未拆。实测开关面**比立案时更宽**：

| 开关面 | RFC-090 立案 (9/20) | 现在 (10/03) | 变化 |
|---|---|---|---|
| 裸 `TRADE_ENV` 读点（库路由族） | 6 处 | 6 处 | 未动（P0-1 未做） |
| `ALLOW_REAL_TRADING` 读点 | 7 处 | **9 处** | +2（本批新增 pipeline.py:3906 / playbook_service.py:1260，未走统一入口，P0-2 裂缝加宽） |
| `AUTO_TRADE_ACCOUNT` 裸读 | ~20 处 | ~20 处 | 未动 |
| DB 路径解析器族数 | 3 族 | **4 族** | +1（新臂族为正确方向，但旧族未拆） |
| LIVE 入场点火仪式 | 1 套 | **2 套并存，失败语义相反** | 见发现 #4 |

每新增一处裸读而不走统一入口，就是在 RFC-090 裂缝清单上反向施工。L5/L6 若按此趋势做完，开关面将比立案时更难收。

## 目标

裁决：是否把「臂路由半迁移」收口为单点解析；若收，裁决范围（探针前置子集 vs 全量）与顺序。

## 立案前强制检查（P0.5）

- [x] **同轴既有影子枚举**：对 `shadow_registry.py` 注册项语义复核未见同轴路由/开关面实验；但 2026-10-03 复核发现宽泛文本 grep `arm|live|lane|cutover|route|switch|dual` 实际有 56 处命中（多在 note/口径说明，如 overnight arm、live 样本数）。立案原写「零命中」不成立；结论「无攒样轴撞车」仍需以注册项语义而非文本 grep 为准。
- [x] **既有裁决撞车普查**：`ADJUDICATED-CRITERIA` C 区逐条核对 — 无路由/开关面相关冻结项；C2「打标前严格不切实盘」本案不触碰（见发现 #4 的治理等价物登记）。RFC-090 §8 六项（8-1~8-6）仍「待用户裁决」，本案与 8-1（TRADE_ENV 降级兼容字段）同向但不抢裁：8-1 裁的是**全局**判定权，本案裁的是 **playbook 臂内**的收口。

## 数据与实证（五个发现，均可复跑）

### #1 同一逻辑 lane 两条解析路径，只靠 `TRADE_ENV=1` 钉死才一致 ⚠️ 最高优先

执行臂已迁移，**生成臂未迁移**：

- 执行侧：`pipeline.py:4056` `route_playbook_db("sim")` → `PlaybookDB(db_name="ftseek_research")`（钉死 ftseek_us.db，不随 TRADE_ENV 漂移）
- 生成侧：`PlaybookDB()` 裸实例化 **~20 处**（pipeline.py:2250/2418/2448/2570/2667/2908/3056/3262/3301/3336/3470/3485、intraday_reentry.py:390/683/1232、weekly_report.py:41、factor_analyzer.py:208、feishu_push.py:31、horizon_return_model.py:53、unified_trade_metrics.py:60），默认 `db_name="ftseek_us"` = **TRADE_ENV 路由**（db_pool.py:45，import 时冻结）

**后果**（结构分析，非实测——现网 TRADE_ENV=1 下两路径同文件，行为一致）：若有人按 RFC-090 §5 旧 runbook（仍作为"历史设计"保留在文档中）翻 `TRADE_ENV=0` → 生成写 live 库、执行读 sim 库 → **指令静默永不执行，零报错**。且 RFC-131 批已删除 `_execute_via_dual_broker_inner` 内的 S2' 半翻互锁（原 broker 侧 fail-close 兜底），旧兜底已不在。

**爆炸半径已核**：无 `db_name == "ftseek_us"` 判别式代码；直连 `get_db("ftseek_us")` 的 5 个消费方（unified_trade_metrics / weekly_review / exit_scorecard / okr_status / trigger_gate_report）全是 SIM 研究侧，与 PlaybookDB 默认值无关。

### #2 LIVE 门控判定写了两遍且子集不重合，镜像先于完整门控 → 死行泄漏

- `_live_engineering_probe_context`（pipeline.py:3885）查 **7 项**前置
- service arm gate（playbook_service.py:1240+）查 **10 项**（多出 max_qty/max_open 配置校验、**open-cap 实查**、BUY 方向、lane 核对）

执行顺序：probe context 过 → **先镜像写 live 库**（`_mirror_instruction_to_live`）→ service 完整门控。当 open cap=1 被占（探针期间常态），每条 SIM 成功都会镜像一行**永不执行的指令**进 live 库。live 库 `playbook_instructions` 已有 16 行历史遗留，口径本就敏感。

同型病灶：RFC-090 §16-F-1（`live_trial` 三表示）——同一判定多处表达，子集漂移。

**2026-10-03 复核补充**: 时序实证成立：`pipeline.py` 在 SIM 成功后先调用
`_mirror_instruction_to_live`，随后才调用 LIVE service；service 的
`LIVE_PROBE_OPEN_CAP` 在 `try_claim_execution` 之前返回，因此 open cap 已占用时
镜像行会保持 `PENDING`。`_live_engineering_probe_context` 构造
`PlaybookService(live_db)` 时会执行 `ensure_tables()`，所以 LIVE 旧 schema 会先被
补列，随后 `save_instruction` 可写入；「schema 失配会自然阻断死行」不成立。
另发现 `session: RTH` 只由 pipeline 的 `get_market_phase()==TRADING` 前置保证，
service 权威门没有消费该键；若未来有绕过 pipeline 的显式 `target_env=real`
调用面，RTH 约束不闭环。

### #3 `_trade_records_db_for_arm` 包装器逐字复制两份 + 死分支

playbook_service.py:93-102 与 pipeline.py:55-66 是同一函数的两份拷贝，各含永不触发的 `except ImportError` 分支（paths.py 在仓内，import 不可能失败）。注释所称 "legacy test resolver seam" 不成立：`paths.get_trade_records_db_for_arm` 内部调 `get_trade_records_db()` 走模块属性查找，`monkeypatch.setattr("app.core.paths.get_trade_records_db", ...)` 本就拦截得到。

另：paths.py:99-106 的 `TRADE_ENV=0` 分支**内联重写了** `get_trade_records_db` 的 override+config 解析逻辑——同一解析两份实现，改 override 键名时必然漂移。

### #4 LIVE 点火仪式两套并存，失败语义相反

- **Path A（旧全局）**：`AUTO_TRADE_ACCOUNT=real` → 毕业门禁 → 未毕业**降级 sim**（playbook_service.py:1145-1171）
- **Path B（新臂）**：显式 `target_env=real` → arm gate → **HALT 不降级**

同一组配置键，走哪条取决于调用方传不传 `target_env`；失败语义一个"偷偷变 sim"、一个"硬停"。**注意：毕业门禁作为代码层强制已从 Path B 消失**——C2 红线的代码化身现在完全寄托在 `probe.authorization_id` 人工填写上。治理上成立（RFC-131 已裁 probe ≠ graduation），但须显式登记该等价物，防未来 `mode` 扩展时无人记得补回毕业门禁。

### #5 命名/注释陈旧（本仓有 INCIDENT-002 实锤教训）

- `monitor_playbook_sim_exits` 现扫双臂，名字仍说 sim
- `db_utils.py:126-128` 注释描述的"静默失去止损监控"路径，其结构根因已被双库扫描修掉一半，注释仍按现状描述
- `scheduler_triggers.py:681` 称 live 库"废弃同名单库"——它现在是 LIVE 臂唯一账本

## 可选方案

### 方案 A（推荐）：探针前置收口（最小批）

- 提出者：Claude Code（复查分析）
- 核心思路：把「启用 `live_manual_arm` 前的硬前置清单」里加入两项收口——#1 生成臂迁移 + #2 单一门控函数；#3/#4/#5 同批顺手收（都是低风险删改）；P0-1/P0-2 全量另行排期。
- 执行项（对应发现编号）：
1. **#2** 抽单一纯函数 `evaluate_live_arm_gate(cfg, env, direction, open_count) -> (allow, reason_code)`：pipeline 用它决定**是否镜像**，service 用它做**权威门**。单点定义，两侧消费，子集漂移结构性消失。
2. **#1** `PlaybookDB.__init__` 默认值 `"ftseek_us"` → `"ftseek_research"`（单点迁移，~20 处裸实例化一次收编）。
  3. **#3** 删两份 `_trade_records_db_for_arm` 包装，全仓直调 `paths.get_trade_records_db_for_arm`；其 TRADE_ENV=0 分支改调共享私有解析器。
  4. **#4** Path A 立墓碑：`AUTO_TRADE_ACCOUNT=real` 分支改为拒绝执行 + 日志指向 Path B（现状无人使用该路径：live 库 auto 臂 0 行）；同时在 Path B 门控注释登记「毕业门禁的治理等价物 = probe.authorization_id 用户签字」。
  5. **#5** `monitor_playbook_sim_exits` 改名 `monitor_playbook_exits`（留一版别名+墓碑）+ 清三处陈旧注释。
- 优点：消除探针期间唯一的持续污染源（#2 死行泄漏）和最隐蔽的静默失效路径（#1）；每步独立可回滚；现网下零行为变更可证（等基测试）。
- 缺点：#1 改默认值波及面 ~20 处，需全量 playbook 测试 + 四棘轮验证；不改 P0-1/P0-2 本体，裂缝①②仍在（但停止加宽）。
- 适用条件：`live_manual_arm.enabled=true` 之前必须完成。

**2026-10-03 复核修正（方案 A，未改变待决策状态）**:
1. `evaluate_live_arm_gate` 入参不能只含 `cfg/env/direction/open_count`；至少还要
   覆盖 instruction direction、LIVE lane db_name、REAL_ACCOUNT_ID、max qty/max open、
   open-count 查询结果、`session`/market phase，并保留 service 侧 quote fail-close。
   建议输出结构化 reasons 而非单字符串，便于镜像前、执行前、审计三处同语义。
2. 不建议简单把 `PlaybookDB` 默认值从 `"ftseek_us"` 改为 `"ftseek_research"` 后直接
   结案：`ftseek_research` 当前钉死 `data/ftseek_us.db`，会绕过既有
   `FTSEEK_DB_FILE` / `paths.trade_records_db` 覆盖语义。更安全的收口是新增单一
   SIM 生成臂解析器或机械替换裸实例化为显式 `route_playbook_db("sim")`，并加
   override 兼容测试；若仍改默认值，必须显式裁决 A/B 覆盖语义是否退役。
3. 复核实测 app 内 `PlaybookDB()` 裸实例化为 28 处（含少量文档示例/兜底），生产
   路径约 25 处，略高于立案的 ~20 处；统计需写明口径。
4. 当前定向测试 23 passed（`test_dual_arm_routing.py` + `test_m2_c2_live_quote_gate.py`），
   但 `test_dual_arm_routing.py` 主要是 source-string 契约，未覆盖 open cap 占用时
   “不写 LIVE 指令”的行为；不能以现有绿测证明 #2 已修。

### 方案 B：全量收敛（A + P0-1/P0-2 同批）

- 核心思路：A 的全部 + 6 处裸 TRADE_ENV 改统一派生器 + 9 处 ALLOW_REAL_TRADING 改 `allow_real_trading()` 唯一入口（含进程/文件一致性启动断言）。
- 优点：RFC-090 裂缝①②本体修复，开关面一次性收敛到目标三层架构。
- 缺点：改动面从 ~25 处扩到 ~40 处；启动期断言有误伤只读脚本风险（RFC-090 §10 已列缓解）；与 L5/L6 排队争窗口，探针启用延后。
- 适用条件：用户裁决探针不急、愿一次性付清收敛成本。

### 方案 C：维持现状 + 仅文档补丁

- 核心思路：不改代码；把五个发现写进 RFC-131 §6「启用前仍必须完成」清单，启用前人工核对。
- 优点：零改动风险。
- 缺点：人工核对不能替代代码保证（本仓纪律：注释不能替代断言——INCIDENT-002、db_utils.py:74 行号漂移均为此类）；#2 死行泄漏在探针期间持续发生；每加一个臂功能裂缝继续加宽。

## 关键分歧

1. **收口是探针前置还是后置？**
   - A 的观点：#1/#2 是探针**期间**就产生实际危害的（死行污染 live 账本、TRADE_ENV 误翻静默断供），前置成本低（零行为变更），后置成本高（探针数据口径被污染后再清）。
   - B 的观点：同 A，且认为收敛成本迟早要付，一次付清。
   - C 的观点：探针只跑 1 股/5 日窗，污染量小可事后清；收敛批与 L5/L6 并行会互相阻塞。
   - 可验证方式：#2 死行泄漏可在探针首日实测（armed 后占住 1 仓，观察次日 live 库指令增量）。

2. **`PlaybookDB` 默认值改 `ftseek_research` 的爆炸半径是否可控？**
   - 已核：无 `db_name=="ftseek_us"` 判别式；直连 `get_db("ftseek_us")` 消费方 5 个全是 SIM 研究侧不受默认值影响。残余风险 = 未发现的行为依赖（缓解：全量 playbook 测试 953 基线 + 等基对比 + 四棘轮）。

3. **Path B 删除毕业门禁代码强制的治理等价物是否足够？**
   - RFC-131 已裁 probe ≠ graduation；本案只是**登记**该等价物（probe.authorization_id = 用户签字），不重裁。若未来 `mode` 从 `engineering_probe` 扩展，须另立 DEB 重新引入门禁或显式记录替代。

## 决策

- 选择方案：**修正版 A（探针前置最小收口）**
- 决策依据：2026-10-03 用户裁「按照建议执行」。采纳复核修正：不直接盲改默认路径语义；
  `ftseek_research` 保留 `FTSEEK_DB_FILE` 覆盖；共享门覆盖 RTH/open cap/lane/env/auth/方向/限额。
- 保留空间：B 的 P0-1/P0-2 部分无论选 A/C 都建议排期（探针跑完后的第一批）
- 负责人：Codex
- 截止时间：LIVE arm 启用前

## 执行项（方案 A 被裁后）

- [x] `evaluate_live_arm_gate` 纯函数落地 + pipeline/service 两侧改调 + 门控子集矩阵测试
- [x] `PlaybookDB` 默认值迁移到 `ftseek_research`，且 `ftseek_research` 保留 `FTSEEK_DB_FILE` 覆盖
- [x] 删双份 `_trade_records_db_for_arm` 包装 + `paths.py` SIM 解析去重
- [x] Path A (`AUTO_TRADE_ACCOUNT=real` 无显式 target_env) 改 fail-close 墓碑；Path B 治理等价物已登记
- [x] `monitor_playbook_exits` 改名 + 兼容别名 + 陈旧注释清理
- [x] 四棘轮（except/naive-now/W456/doc_guard）+ `startup_smoke_check.py --wait 90`
- [x] 重启前核对：SIM/LIVE 未平仓均 0、无在途出场（沿用 RFC-131 重启纪律）
- [ ] worktree WIP 归属先收口（启用 LIVE 前必须完成）

## 验证项

- [x] `app/playbook` 运行时 `TRADE_ENV` 读点为 0（注释/审计快照键不计）
- [x] 构造 TRADE_ENV=0 单测：生成与执行均固定 SIM/research lane（钉 #1 修复）
- [x] armed + open cap 占满时，门控拒绝且不调用镜像写库函数（钉 #2 修复）
- [x] `AUTO_TRADE_ACCOUNT=real` 无 target_env 调用 → fail-close、broker 0 触达、instruction 未认领（钉 #4）
- [x] service 权威门非 RTH 拒绝、open cap 拒绝且先于 execution claim
- [x] `tests/playbook` 全量 967 passed；受影响核心/切换测试 45 passed
- [x] 复验：py_compile 通过；四棘轮 29 passed + doc_guard 通过；重启后 SIM/LIVE 均为
  0 未平仓/0 有效持仓，exit intents 仅 `BOOKED`
- [ ] 探针启用后 5 日窗：live 库无未执行死行（决策有效性终验）

### 实施记录（2026-10-03）

- 新增 `app/playbook/live_arm_gate.py`：`evaluate_live_arm_gate` 是 LIVE 工程探针
  前置条件的单一判定面；`count_open_live_positions` 读取失败返回 `None` 触发 fail-close。
- `pipeline` 在 `_mirror_instruction_to_live` 之前重新评估 open cap/RTH/方向/授权/lane；
  拒绝时不写 LIVE instruction，并走 `app.push` hub 通知。
- `PlaybookDB()` 默认改为 `ftseek_research`；`db_pool.ftseek_research` 显式保留
  `FTSEEK_DB_FILE` 覆盖，且不随 `TRADE_ENV` 漂移。
- `AUTO_TRADE_ACCOUNT=real` 且未显式 `target_env=real` 的旧 Path A 返回
  `LIVE_PATH_A_RETIRED`，不再降级 SIM。
- `monitor_playbook_sim_exits` 更名 `monitor_playbook_exits`，旧名保留兼容别名；
  scheduler 活跃调用点已切换新名。
- 2026-10-03 22:41:14 CST 按 `systemctl restart ftseek-us` 加载本批；
  `startup_smoke_check.py --wait 90` 7/7 通过，服务 `active/running`（PID 3817330），
  重启窗口无 warning/error。`cutover_preflight.py` 仍为 `block=1 warn=6`，硬阻塞为
  P1 毕业门禁；LIVE 开关保持全关。

### P0 硬化补充（2026-10-03 深夜，承接 RFC-132）

用户令「执行修复」后，复查确认路由收口仍未消除四条分臂状态串读风险，本批补齐：

- [x] LIVE capital 独立实例：`get_capital_safety_system_for_arm("real")` 固定
      LIVE ledger + `trade_env=0`；执行前从 REAL broker 刷新资金，失败 fail-close。
- [x] AccountCircuitBreaker / AbsoluteCircuitBreaker / symbol day loss /
      DualBroker notional cap 全部显式按 `target_env` 取台账与资金。
- [x] ManualHoldRegistry 内部键改为 `(account, symbol)`；SSOT fallback 用
      `get_trade_records_db_for_arm(target_env)`。SIM 与 LIVE 同 symbol 互不覆盖。
- [x] LIVE broker STOP 强验证：必须 `success=true + futu_order_id`，并写
      trade/open position receipt；失败 HIGH 推送 + 持久化 HALT_NEW_LIVE，
      不自动平既有仓。
- [x] `prepare_live_probe.py --apply` 完成 LIVE schema 迁移、8 条 size=0 manual
      僵尸行清理、REAL 资金基线预置（$73,057.35，trade_env=0，green/unlocked）。
      写前备份：`data/backups/ftseek_us_live_before_probe_prep_20261003_234848.db`。
- [x] `cutover_rehearse.py --to live` 达成「DDL 干净 + capital 双向 roundtrip 无缝」。
- [x] `cutover_preflight.py --to live` 从 `block=1 warn=6` 收敛为
      `block=1 warn=4`；唯一 BLOCK 仍为 P1 毕业门禁
      `graduated=false / ic_t3=-0.0403`。
- [x] 本 P0 硬化批按重启纪律加载并跑 `startup_smoke_check.py --wait 90`：
      2026-10-04 00:10:44 CST 最终 `systemctl restart ftseek-us`（含 STOP
      失败推送语义修正），PID 3826815，冒烟 7/7 通过，重启窗口
      warning/error 为 0。复跑 preflight 仍为 `block=1 warn=4`。
- [x] 2026-10-04 用户裁决接受毕业门禁知情破例：仅限 1 股 LONG 工程探针；
      LIVE 仍全关，`probe.authorization_id` 未填写，本裁决不构成启用授权。

## 回滚

每执行项独立 commit，逐项 `git revert` 即可；全部改动在 `TRADE_ENV=1` 下为零行为变更，回滚无运行时风险。若用户裁 C：仅将本 DEB 状态改 REJECTED，五发现转入 RFC-131 §6 清单人工核对。

## 讨论记录

### 第 1 轮（2026-10-03，复查分析 → 立案）

- **Claude Code（主会话复查）**：RFC-130/131 双臂落地架构方向正确（显式 target_env、HALT 语义、decision_id 对照键、per-arm 组合兜底、测试翻面+墓碑纪律均值得保留），但落地是"加法"非"收敛"——新增解析族而旧族未拆，开关面净加宽（ALLOW_REAL_TRADING 7→9 处）。五发现按严重度排序如上，其中 #1（生成/执行臂双解析路径）与 #2（镜像先于完整门控）是探针期间就会产生实际后果的两条，建议列启用硬前置。量化表与 file:line 证据均经实跑复核（含两库 `source_type` 分布实测：sim 库 0 行 PLAYBOOK_LIVE、live 库 auto 臂 0 行，证明 LIVE 臂从未运行、收口改动零存量影响）。
