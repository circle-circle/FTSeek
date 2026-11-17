# DEB-20261114 — MOM-ETF-V1 入场/出场生命周期评审与工程加固

- 状态：**工程修复已部署；V1 经济参数冻结；下一自然调仓簇复验**
- 用户请求：「mom 的入场和出场是否合理，是否符合预期设计，这类遇到的问题是否经典，是否有解决，它山之石可以攻玉，请发挥你的能力做修复」
- 纪律：区分工程正确性与策略经济假设；不借修 bug 改票池、SL/TP、预算、优先级或风控门。

## 0. 总结判

MOM-ETF-V1 的主设计是成立的：前一交易日 12-1 动量、QQQ MA200 现金过滤、四 ETF 取 top2 等名义、执行时 reprice/费用/预算复核、3% 初始 SL、隔夜 5% 失效线、21 交易日 MAX_HOLD、TP=0，以及独立于 DA 的 swing 出场 allowlist。此次发现的大部分生产问题不是“动量策略错了”，而是**调度、状态机、时间、模式匹配、证据链与测试接缝**偏离设计。

V1 的收益有效性仍未证明。R14 回放无 TP 点估计较好（198 笔、PF 1.27、净约 42.5bps），但存在 2026 选池前视、非 PIT、牛段集中、日线成交假设与调仓日簇相关性；所有 TP/追踪变体 95% CI 跨 0。它只能支持“继续 SIM 验证”，不能支持放大仓位或宣布策略合理盈利。

## 1. 入场/出场逐项判定

| 面 | 判定 | 证据/边界 |
|---|---|---|
| 12-1 / asof / 完整性 | 符合设计 | `close[-22]/close[-253]-1`；四 ETF 同日、上一交易日，缺数据 fail-closed |
| QQQ MA200 现金过滤 | 符合设计 | 严格 `close > MA200`；不满足应现金，不为攒样本放宽 |
| top2 等名义与费用 | 符合设计 | 每腿 ≤$4,920、日 ≤$9,840、最多 2 仓；最终股数复核未绕过 |
| 09:50–14:00 ET | 设计正确，调度曾有 DST 缺口 | 原 cron 仅 CST 22:35，EDT 到 10:35；本批已修 |
| MOM/DA 顺序 | 保留 DA 优先 | DEB §1.4 已明确 sleeve 排后；旧 handoff“DA 前处理”是过期实现描述，不改预算 |
| reprice / SL | reprice ask 起价，3% SL | 补账曾误用原始 SL，已修为 reprice_audit 优先并对齐生产两行 |
| 3% SL / 5% 隔夜 | 符合 V1 | 先 5% 后 3%，同日只走 3%；不宣称真实 gap 保护 |
| 21 交易日退出 | 符合 V1 | 本地交易日历、RTH 才退出、entry_time 异常不伪造 MAX_HOLD |
| TP / trailing / EOD | 符合 V1 | TP=0，不套 DA early-tight/BE/trailing/EOD；MOM 可持夜 |
| 恢复与幂等 | 已加固 | 券商成交≠本地簿记；补偿回执可重放且只记一次账 |

## 2. 这类问题是否经典、如何解决

这些问题是交易系统中的经典故障族，不需要发明新策略来解决：

1. **分布式状态不一致**：order accepted / filled / booked 是三件事。用 Saga、durable outbox、CAS 状态机、可重放收据和账实对账解决，不能靠 try/except 假装成功。
2. **幂等缺失**：网络重试、进程重启、回调乱序都会重复事件。decision_id/order_id/intent_id 必须全链透传，重复回执只能有一个会计结果。
3. **空值/模式契约**：`dict.get(key, default)` 不能处理键存在但值为 None。写入方提供机器可读契约，服务层仍做防御，首单必须走真实 service 而非 mock。
4. **跨策略共享表接管**：同一 date+symbol 的通用 UPSERT 会吞掉 sleeve。必须显式来源隔离与跨策略覆盖测试。
5. **暂时态污染终态**：报价过龄、旧仓退出在途应该 deferred/retry，只有 cutoff 才终止，否则一次暂时故障会静默丢整个 21 日周期。
6. **时间错误**：naive/aware、CST/ET、DST、交易日与自然日必须分开。cron 用 ET，写入方另做窗口 fail-closed，退出按本地交易日历。
7. **N+1 与限流**：逐票请求会撞 429。批量请求并保留独立时间样本，不能为省事复用反事实对照报价。
8. **测量污染**：DA 的 early-tight 影子不能混入 MOM swing 人群；缺少数据、主动现金、暂时 deferred 不能共用同一个 SKIP 文案。
9. **测试假绿**：FakeService 只测 policy 不够，必须有真实 SQLite + 真实服务编排 + 部分成交/重试/重启/身份冲突用例。

## 3. 本批已实施的工程修复

### 3.1 指令、执行与预算

- MOM writer 新增机器可读 thesis/invalidation（12-1、QQQ MA200、21 日、3%/5%）。
- `save_instruction` 增加跨策略覆盖守卫：DA/UE 不得接管未执行 MOM 行；反向仍由 priority 拒绝，保持 DA 优先且不新建平行行。
- `stale_quote_gate` 对 MOM 改 `_record_mom_defer`，不再写终态 `skip_reason`。
- `MOM_MAX_OPEN_POSITIONS` 改暂时 deferred，旧仓退出后同窗口可复检；14:00 仍是唯一周期终止点，没有强制放行旧仓。
- `size_mom_order` 删除不可达的伪搜索增量，保留最大合法股数与费用/最小名义语义。
- 数据不足抛 `MomDataIncompleteError`，writer 返回 `fail_closed/data_incomplete`；QQQ<MA200 仍是 `cash`，不再混称。
- writer 在调仓日只允许 09:30–09:50 ET，错位返回非零 `fail_closed`，不再把调度错误写成普通 no_op。
- cron 改 `CRON_TZ=America/New_York` + `35 9 * * 1-5`，其后恢复 `CRON_TZ=Asia/Shanghai`；备份 `data/crontab.backup.20261114T010316Z_mom_dst.txt`。readiness 新增 `writer_cron_et_aware` 硬检查。

### 3.2 出场、补账与证据

- 补偿重建优先使用 `reprice_audit.reprice_stop_loss/take_profit`，不再误用原始指令值。生产 XLE 1895、QQQ 1896 已审计化对齐：SL 分别 **3.000% / 3.006%**，净额不变，复跑 ALREADY_CURRENT。
- MOM 出场前记录 `exit_gap`（prev_close/open/decision_vs_open），可区分真跳空与开盘后下杀；不改退出判据。
- `_mom_exit_price` 接入 decision，不再被静默丢弃。
- naive entry_time 按仓内 naive=CST 的 `to_et()` 约定换算，避免 MAX_HOLD/隔夜日期错一天。
- `early_risk_shadow.grace_candidate` 排除 MOM，避免 DA early-tight 人群被 swing 仓污染。
- `_MOM_DEFER_AUDIT_SEEN` 与 `_INVALID_ENTRY_TIME_LOGGED` 加上界，防长期无界增长。

## 4. 验证与部署

- 扩展 MOM 生命周期、跨策略隔离、补偿与 grace 契约：**49 passed**。
- 恢复、体积/config_version/时区/日志卫生/offline full-chain：**57 passed**。
- MOM readiness：**44 passed**，`writer_cron_et_aware=true`，产物 `data/readiness/mom_etf_v1_r18_lifecycle_hardening.json`。
- 部署 MainPID=2927502，启动冒烟 **7/7**；重启前后 9 仓不变、非 BOOKED=0。
- 控制环台账测试只报其他 `da_event_context_shadow.py`、`da_instruction_quality_shadow.py`、`ext_event_risk_live_shadow.py` 未登记，MOM 行已挂账，未把该既有问题算成本批回归。
- 未运行第二次全仓测试；上批全仓结果与其他既有失败归属见 DEB-20261113 §9，未宣称全绿。

## 5. 保留冻结、不能借修 bug 偷改的事项

1. MOM/DA 优先级与预算分配：当前 DA-first 是已批准设计。若未来要 MOM-first/预留子预算，必须预注册，评估对 DA KR 与 MOM 样本选择偏差的影响。
2. chase cap：当前没有有效 cap 字段，不能一边说“共享 chase”一边假装存在。是新增冻结阈值还是明示 V1 无 cap，另裁。
3. VWAP/量能：MOM scheduled 当前绕过整串日内 trigger，与旧 DEB 矩阵有文案冲突；保留或取消必须回写设计，不静默改变人群。
4. VIX/飞刀/同日冷却/gap_dive/风险缩放：当前共享安全门保留。尤其 DA 减半与 MOM 最小名义可能互锁成全拒，属策略/风控裁决，需明确豁免或重定尺，不可暗中绕过。
5. writer 时 PORTFOLIO_HEAT 与执行时 admission 的统一程度：执行时硬门仍在，写入时是 record-only 还是 deferred，另裁。
6. 3% SL、5% 隔夜线、TP=0、top2、四 ETF 票池、等名义与 21 日持有期，均无足够新证据修改。
7. `execution_admission_evaluate` 对 DA-only 的审计不对称、MOM 拒因细分、旧库 market_context_json 迁移与其他基线红，另列后续工程债。

## 6. 下一自然验证判据

- 下一调仓日若为 QQQ≤MA200，必须接受现金，不为验证制造交易。
- 若产生正常 MOM 首单：无 NoneType、无补偿退出、trade/open_position 正常建立、实际 SL 为执行 reprice 的 3%、decision_id 不被其他源接管。
- 旧仓到期退出在途时，新指令只能 deferred；旧仓 BOOKED 后同窗口可执行，14:00 后不得补跑。
- 持夜只走 `SL_INITIAL / OVERNIGHT_GAP_STOP / MAX_HOLD`，不得进入 DA early-tight/BE/trailing/EOD。
- 任何正常调仓收益只进入 L2/L3 观察，不能直接证明 alpha；样本评估用实际成交时间/价格，不拿 R14 的开盘假设替代。

## 7. 第 2 轮只读复查（2026-11-14 11:30–11:40 CST，Codex）

> 角色：独立复核方。只读复现 + 文档落档；不改代码/配置/cron，不重启，不新增影子或 Gate。
> 基线：HEAD `12607e995`；服务 `ftseek-us` active，MainPID `2934331`，ActiveEnter `2026-11-14 10:27:16 CST`。
> 方法：先证伪“已修复/已愈合/全绿”叙事，再按经典故障族归类；遥测实测优先于文档自述。

### 7.1 恢复路径 Row/dict 契约 bug（中，未修）

- 锚点：`app/core/position_sync.py:262` `_record_recovered_queue_candidate(instruction: Dict, …)`；
  `:271` 起用 `instruction.get(...)`；except `:296–299` 仍用 `instruction.get("id")`。
  调用方 `:856–858` 传入 `sqlite3.Row`（`:773` `conn.row_factory = sqlite3.Row`）。
- 复现：直接以 `sqlite3.Row` 调用 → `AttributeError: 'sqlite3.Row' object has no attribute 'get'`。
  集成 `tests/playbook/test_exit_compensation.py::test_recovery_rechecks_unbooked_and_uses_only_order_receipt`
  本轮复跑 **1 passed, 1 error**：captured log 显示 `:271` 与 `:299` 双重 AttributeError；
  teardown 边界封印捕获出站 socket（`173.223.87.182:443`、`172.66.170.11:443`）。
- 影响边界：簿记 `mark_execution_booked` 在 hook 之前已完成，故**不是丢账/重复下单**；
  损失的是 RFC-203 恢复成交的 queue 影子样本，并产生 `unresolved entry reconciliation failed` 噪声。
  RFC-203 §9 宣称覆盖 recovered-fill，但 `tests/test_position_sync_queue_shadow.py` 只用 dict，属测试假绿。
- 经典归类：适配器/模式契约不一致 + 幂等消费者边界。参考 microservices.io Saga / transactional outbox /
  idempotent consumer；Stripe idempotency keys。修复方向：边界处 `dict(instruction)` 归一化，
  except 用安全 id 读取，补 `sqlite3.Row` 集成测试；不借机改簿记/出场语义。

### 7.2 测试隔离与既有红（未修，不宣称全绿）

- 本轮复跑治理子集 `tests/test_db_connection_chokepoint.py tests/test_ops_scripts_sync.py`
  → **2 failed, 3 passed**：5 个字面 `sqlite3.connect` 脚本
  （`audit_full_chain_20261106.py`、`da_regime_divergence_check.py`、`p1_score_calibration.py`、
  `p2b_entry_confirm_settlement.py`、`p3_market_premise_probe.py`）；3 个未入库运维脚本
  （`gap_rank_daily.sh`、`nomination_lift_weekly.sh`、`verify_prior_move_194.py`）。
- 前轮已跑、本轮未重跑：MOM+补偿+EXT 合批 212 passed / 4 failed / 3 errors；
  隔离补偿 65 passed / 3 errors（合批失败消失 ⇒ 跨文件污染，疑似 quote-health/config stub）；
  `tests/playbook` 全量 **1228 passed, 3 errors**（约 91s）。错误含真实出站 socket 被封印捕获。
- `tests/conftest.py:94` `_KNOWN_PRODUCTION_WRITE_DBS` 为库级豁免，`:591–616` 会话末计数展示；
  豁免≠已解决，生产库在测试中仍被写入的事实必须可见。控制环台账测试通过。
- 经典归类：共享可变状态/测试顺序依赖/网络边界泄漏。解法：fixture 级隔离、真实 SQLite+真实服务编排、
  出站/生产写封印、按文件随机序复跑定位污染源；不把既有红算成本批回归，也不宣称全仓全绿。

### 7.3 edge 影子断流未愈合；心跳部分存在（待下一自然会话验证）

- `data/shadow/edge_gate_shadow.jsonl` 最后写入 **2026-11-09 23:40 CST**；`shadow_board.py` 报 producer stopped >4d。
  `app/playbook/pipeline_parts/risk_gates.py:393–402` 已改 `log=True`，但在周五盘后才部署，尚无自然样本。
- `scripts/business_heartbeat.py:47–78` `_shadow_coverage_check` 对“最近已完结 bridge 日无 edge 写入”告警；
  `data/shadow_coverage_latest.json`：`latest_bridge_day=2026-11-12` 未覆盖，`coverage_10d=0.7`，
  `checked_at_et=2026-11-13T15:07:01-05:00`。shell 走钉钉、30min 冷却，日志在 `logs/healthcheck.log`。
- `scripts/data_freshness_watchdog.py:84` 登记 edge 文件 72h，但 `:120–121` `_is_weekend()` 用**主机 CST**；
  周六 06:25 CST 运行时跳过所有 `weekday_only` 项，故 2026-11-14 06:25 日志只见 2 个 ok
  （`llm_audit.db`、`price_fail_closed.json`），edge 不可见。这不是“已愈合”，是周末盲区 + 自然样本未到。
- 经典归类：liveness ≠ readiness；生产者心跳 + 消费者覆盖率 + 四黄金信号。参考 Google SRE Monitoring。
  下一交易会话必须验证写方自然落盘与心跳告警闭环；不得提前宣称 edge shadow healed。

### 7.4 MOM 生命周期与 readiness 凭据（工程部分可核，未达 PLUMBING_OK）

- 只读 DB：MOM 指令 **5621 XLE / 5622 QQQ** 均 `BOOKED`（2026-11-13）；补偿 trade **1895/1896** 已平仓。
  出场台账在独立库 `data/playbook_exits.db`（`app.core.paths.get_exit_ledger_db()`）；
  `exit_intents` **397 全 BOOKED**，非 BOOKED=0。当前 9 个持仓无 MOM owner
  （4 个 scheduler/playbook_sim，5 个 extended_hours_entry）。
- readiness `data/readiness/mom_etf_v1_r18_lifecycle_hardening.json`：`writer_cron_et_aware=true`、
  focused 44 passed，但 `service_main_pid=2927502` / `active_enter=09:08:06` 已陈旧；
  当前服务为 `2934331` / `10:27:16`。MOM cron 已 ET-pinned `35 9 * * 1-5`。
- 判定：下一自然调仓/首笔干净订单前不写 `PLUMBING_OK`；经济 edge 仍未证明
  （R14 非 PIT、选池前视、牛段集中、日线成交假设、调仓日簇相关）。参考 Bailey et al. PBO（SSRN 2326253）、
  Harvey/Liu/Zhu 多重检验（SSRN 2249314）；按日聚类 CI/MDE，不拿单窗点估计晋级。

### 7.5 EXT 429 收口与 OTC 负缓存（缓解已在，自然验证待）

- `scripts/extended_hours_entry_executor.py:1281–1415` `_QuoteCache`：第二轮报价批量预取、
  失败/缺失缓存 None（fail-closed）、限频/网络错误整批放弃不二分、其余错误二分隔离、
  legacy 单票回退上限 5。最近 `logs/ext_entry.log` 多轮 `ext_open_position_limit`（5/5）跳过；
  `429` 命中多为价格串/连接 id，未见新 429 风暴。
- 重复 OTC 不支持：`US.STKXF/CNDCF`（流动性批量）、`US.UNCRY/SMERY`（快照 dropped）。
  建议对 unsupported OTC 做负缓存/预过滤，下一会话验证；不放宽价格/流动性门。
- 经典归类：N+1 请求/限流/负缓存。参考 AWS Builders' Library timeouts/retries/backoff with jitter。

### 7.6 注册表与 KR4 执行面（计数已校正，不妄称违规）

- `app/playbook/shadow_registry.py` 实载 **114** 项：OBSERVING 61 / KILLED 47 / SUPERSEDED 4 / GRADUATED 2。
  永久测量分类按 `no_adjudication_reason.startswith('PERMANENT_MEASURE')` → 16 项 observing；
  非永久 observing 45，其中 `adjudication_state(item)` 未登记 **16** 项。
- 过期 observing **4** 项：`entry_anchor_dual`（2026-11-05）、`entry_band_widen`（2026-11-04）、
  `eth_held_exit_shadow`（2026-10-30，PERMANENT_MEASURE）、`ext_entry_earnings_criterion_shadow`（2026-10-31）。
  `eth_exit_quote_age` 在当前计算下**不**过期。
- KR4 ≤5 并行实验没有“active experiment”机器视图；未定义 active vs permanent/terminal 前不宣称违规。
  解法：显式 active 视图 + 到期结算 + 每周批量裁决；新影子未注册=禁止态。

### 7.7 跨切面治理：OKR/仪表盘 SSOT 漂移（高价值治理债）

- `scripts/okr_status.py:372–427` 在 2026-11-14 仍打印 “OKR-2026-10 (现行)”，无 11 月块；
  宪章 §六仍指 10 月，`AGENT.md:3` 混合。`data/okr_latest.json`：`overall_score=60` 来自已作废 8 月 KR 集；
  pipeline `with_entry=212 / active=166 = 128%`（分子/分母口径不一致）。
- 经典归类：陈旧仪表盘/指标语义层缺失。参考 dbt Semantic Layer“一次定义、多处消费”。
  解法：补 11 月块或显式 stale banner；分子/分母同口径；不静默改写历史。

### 7.8 本轮验证与未验证

- 本轮复跑：Row 直接复现 AttributeError；EventContext 过期探针 injected=True；
  `pytest -q tests/test_position_sync_queue_shadow.py tests/test_da_prompt_injection.py` → **24 passed**；
  单条补偿恢复测试 → **1 passed, 1 error**；治理子集 → **2 failed, 3 passed**；只读 DB/注册表/readiness 核对。
- 未验证：下一自然 MOM 调仓/首单、edge 写方自然恢复、EventContext 新 06:30 ET 首轮、EXT 下一会话 429/OTC、
  任何收益/alpha/live readiness。未改代码/配置/cron，未重启。

## 8. 第 3 轮：有把握问题直接修复（2026-11-14 14:00–14:25 CST，Codex，用户裁「直接修复」）

> 用户明确授权「针对有把握的问题直接修复」。本轮修 3 个已确诊 bug，均属**恢复已批准契约**而非新行为：
> Row 契约（RFC-203 §9 设计覆盖）、EventContext 过期（RFC-208 §4.1/§5，详见该 DEB §6）、
> watchdog 周末口径（`_now_et` 2026-08-26 已修同类 `{today}` 混淆）。一次一变量：逐项独立补丁+独立测试。
> 不修项（需裁决或自然样本）：OKR 11 月块、KR4 active 视图、测试隔离、EXT OTC 负缓存、edge 写方自然恢复。

### 8.1 P0 Row/dict 恢复契约（已修）

- `app/core/position_sync.py` `_record_recovered_queue_candidate`：入口 `dict(instruction)` 归一
  （签名 `Dict`→`Mapping`），except 取 id 加 isinstance 守卫，杜绝二次 AttributeError。
- 测试 +2（`tests/test_position_sync_queue_shadow.py`）：sqlite3.Row 集成样本字段全等；
  writer 故障时 Row 不再二次 AttributeError。注意 `early_risk_shadow._DEDUP` 为模块级内存去重，
  新用例换 inst_id 5611/5612 避免与首例冲突（此即文件内顺序依赖的一个实例）。
- 验证：本文件 **5 passed**；补偿恢复集成测试日志 AttributeError 计数 **0**（修复前双重）；
  残留 teardown socket seal 为既有 P2 隔离问题，非本批引入。

### 8.2 watchdog 周末判定改 ET（已修）

- `scripts/data_freshness_watchdog.py` `_is_weekend()`：`datetime.now()` → `_now_et()`，
  与 `{today}` 占位符同口径。cron `25 6 * * 2-6`（host CST）恰映射 ET 周一~周五盘后；
  周六 CST 06:25 = ET 周五 17:25 不再盲区，ET 周六/日照常跳过。
- 测试 +2（`tests/test_data_freshness_watchdog_static.py`）：冻结 host/ET 双时钟确定性复现
  （ET 周五必查 / ET 周六必跳过），任意宿主机日期可复跑。
- 验证：watchdog 三套件 **89 passed**；实跑 `--dry` 当前 ET 周六正确跳过 weekday_only；
  首次自然 exercised = 周二 06:25 CST（周一 ET 盘后），届时 edge 停更应可见。

### 8.3 本轮验证、部署与未验证

- 相关 10 套件 **175 passed**；补偿 **65 passed/3 errors**（既有 socket seal，同基线）；
  治理子集 **2 failed/3 passed**（既有已知红，同基线）；`check_undefined_names`/py_compile/diff-check exit 0。
- 部署：重启前核 9 仓不变、exit_intents 397 全 BOOKED、`git status -- app/ scripts/` 仅本批 3 文件；
  `systemctl restart ftseek-us` → MainPID=**2946809**（14:25:15），启动冒烟 **7/7**；
  重启后 9 仓不变、非 BOOKED=0。
- 未验证：下一自然 MOM 调仓/首单（仍不写 PLUMBING_OK）、edge 写方自然恢复、
  EventContext 新 06:30 ET 首轮、EXT 下一会话 429/OTC、watchdog 周二自然首跑、任何收益/alpha/live readiness。

## 9. 第 4 轮：深度根因复查与源头修复（2026-11-14 14:30–14:50 CST，Codex，用户裁「继续深度检查，确保可以从源头最优修复」）

### 9.1 前三项修复的根因最优性复核（结论：成立，无需返工）

- Row 契约：全仓 `row_factory=sqlite3.Row` 普查——position_sync 其余 `.get` 均在显式 dict
  （`db_positions` 逐列构造、snapshot `.to_dict()`、jsonl 行）；playbook_service/pipeline 的
  `row.get` 同为 jsonl/构造 dict；intraday_reentry 有 isinstance 守卫。同类 bug 仅已修一处；
  callee 边界归一覆盖未来调用方，为单点最优。
- EventContext 过期：写入方（market_brief/da_event_context_shadow）不写 valid_until，
  消费方计算是唯一口径；生产消费方 `da_event_preexecute_shadow.py:352` 走 `load_event_context`（过期闸生效）；
  离线探针 da_repeatability_probe/da_event_instruction_replay 显式 `--valid-until` + session-date 钉住，
  属确定性研究回放，非绕过。硬校验为契约最优。
- watchdog 周末：business_heartbeat 已 ET 口径；全仓 `datetime.now().weekday()` 模式清零。

### 9.2 新根因修复 ①：D2 忽略 Hermes 作业级 timezone（已修）

- 病灶：`_check_hermes_jobs` 按 host CST 算应跑槽，但 Hermes job 有 `timezone` 字段
  （DA main carrier = America/New_York，`next_run_at=2026-11-16T08:00:00-05:00` 实证）。
  ET 作业被按 CST 幻影槽对账：晚班 ET 作业真实槽尚未到期时幻影槽已计缺席 → 假阳性；
  告警「最近应跑」时刻也错挂 CST。与 `{today}`（8/26）、`_is_weekend`（本轮 §8.2）同一时区病灶族。
- 修复：`scripts/data_freshness_watchdog.py` 按 `j.get("timezone")` 逐作业转 ZoneInfo 算槽，
  槽位/实跑比较与告警显示统一作业时区；无 timezone 字段作业行为逐字节不变。
- 测试 +3（`tests/test_watchdog_liveness.py`，aware +08:00 时钟宿主无关）：晚班 ET 未到期不假告 /
  连缺真实 ET 槽告警且显示 ET / 早班 ET 准点健康。旧代码对照实验：Test A 场景旧码 findings=1（假阳性）、
  新码 0 —— 回归测试真实区分新旧。
- 验证：liveness **84 passed**；真实 Hermes 面 `--dry` 新告 0、33 作业对账不变。

### 9.3 新根因修复 ②：补偿测试真实外联（已修，测试侧）

- 病灶：3 个恢复测试 teardown socket seal 报错的根源 = 未 stub `_fetch_entry_vix_for_backfill`
  （`position_sync.py:353` 专为测试钉住设计的缝），VIX 真源 FRED/AlphaVantage 真实外联被 seal 记录。
- 修复：`compensation_case` fixture 补 `monkeypatch.setattr(_position_sync, "_fetch_entry_vix_for_backfill", lambda: None)`
  （与 `test_position_sync_backfill_clock` 既有钉法一致）；seal 不放宽，其它越界仍报错。
- 验证：`test_exit_compensation` **65 passed / 0 errors**（此前 65 passed + 3 teardown errors）。

### 9.4 本轮验证与部署边界

- 8 套件 **208 passed**；治理子集 2 failed/3 passed（既有已知红同基线）；
  `check_undefined_names`/py_compile/diff-check exit 0；watchdog 真实面 `--dry` 新告 0。
- 本轮仅 scripts/ + tests/ 改动：watchdog 由 cron 逐跑加载新码，**无需重启服务**；
  app/ 两处修复已于 14:25 重启（MainPID=2946809）生效。
- 未验证：D2 修复后首个自然 cron 跑（周二 06:25 CST）、edge 写方自然恢复、MOM 自然调仓/首单、
  EventContext 06:30 ET 首轮、任何收益/live readiness。

## 10. 第 5 轮：跨文件测试污染根治 + 治理指针时效校正（2026-11-14 15:01 CST，Codex，用户裁「继续」）

### 10.1 跨文件污染的真根因与根治（生产侧行为中性重构）

- 复现：MOM+补偿+EXT 组合子集 4 失败（全在补偿 real 臂），隔离即消失。症状=报价健康门
  「交易所报价时间…在未来」拒单 → halt 路径不可达。
- 根因链（探针实证）：`test_mom_etf_v1_flow._run_auto_execute` 用 `monkeypatch.setattr(datetime_module,
  "datetime", FrozenDateTime)` **patch 全局 datetime 类**；冻结窗口内懒加载的 5 个模块
  （`data_health_guard`/`circuit_breaker`/`absolute_circuit_breaker`/`drawdown_control`/`intraday_indicators`）
  把 FrozenDateTime 捕获为模块级 `datetime`，monkeypatch 还原后仍永久冻结（`dhg.now()` 停在 11-13）。
  补偿报价按真实 now（11-14）打戳 → 相对冻结时钟「在未来」→ 拒单。
- 真根修：aef 主时钟本就走懒加载全局 `_dt.now`（`auto_execute_flow.py:378/1492`），而函数签名早已设计
  `datetime_cls` 注入缝。改两处懒加载为 `datetime_cls.now(...)`（`:379`/`:1513`），测试遂可**删除全局 patch**，
  时钟纯经注入冻结。生产 4 处调用（pipeline.py:1903/2120/2429/3582）均传 `datetime_cls=datetime`（真类）
  ⇒ **可证行为中性**，非行为变更。
- 验证：污染探针 `POLLUTED MODULES: []`（修复前 5 个）；MOM 单文件 23 passed；组合子集 **107 passed/0 failed**
  （修复前 4 failed）；**全量 tests/playbook 1228 passed/0 errors**（修复前 +3 errors，含 §9.3 已修的 socket seal）；
  非 playbook 相关 9 套件 155 passed；check_undefined_names/py_compile exit 0。
- 方法论：全局 patch 内置类 + 懒加载 = 跨文件时钟污染的经典模式；根治=被测函数统一走注入时钟缝，
  而非在受害侧 reload/预导入打地鼠。参考 pytest monkeypatch 作用域纪律与依赖注入时钟（freezegun 亦仅模块级）。

### 10.2 治理指针时效校正（宪章 §六/§四 → OKR-2026-11）

- 沿用 2026-09-06 / 2026-10-17 先例（只改指针 + 追加变更记录，条文规则零改动）：战略层 OKR-2026-10 →
  `docs/OKR-2026-11.md`（2026-10-23 用户批准、2026-11-01 生效，主 KR=DA-only 净 bps/笔>0）；
  §四「现行红线」指针同步；战役层如实标注 `scripts/okr_status.py` 仍渲染 10 月块 = 11 月判分模块未建之落地债。
- **不擅自造 11 月判分指标**：`okr_status.py` 的 11 月块需 DA-only 净 bps 口径裁决，属 adjudication-gated，
  本轮仅在宪章诚实标注债务，未改推送脚本（避免表层 banner 冒充根修）。

### 10.3 本轮部署与未验证

- aef 为生产下单管线改动 → 重启前核 9 仓不变、exit_intents 397 全 BOOKED、app/scripts 仅本批；
  `systemctl restart ftseek-us` → MainPID=**2948878**（15:01:00），启动冒烟 **7/7**；重启后 9 仓不变、非 BOOKED=0。
- 未验证：aef 重构后首个自然交易日盘中实跑（行为中性已由 1228 用例背书，但自然盘口/网络路径未自然 exercised）；
  D2/edge/MOM/EventContext 自然首跑同前；任何收益/live readiness。

## 11. 第 6 轮：既有 DB/线上脚本治理收口（2026-11-14 22:45 CST，Claude，用户裁「按建议执行修复优化」）

### 11.1 必要性分诊与实施边界

- §7.2 两项既有红不再长期挂账，但不为全绿而扩 `GRANDFATHER` / `LEGACY_EXCLUDE`。
- 5 个分析脚本均为 `mode=ro`，不写交易库/不下单；只把默认主库路径收编到
  `app.core.paths.get_trade_records_db()`，保留 `--db` 显式覆盖。P3 事件库改走
  `get_data_dir()`，不实例化 EventStore，避免只读分析建表。SQL、样本口径与判据未改。
- 3 个线上脚本原样入 `ops/hermes/`：Lift 周报仍 enabled、11-14 最近成功、下次 11-21，必须纳入 git；
  gap 已 paused，不恢复调度；RFC-194 一次性验收已 completed，不新增 cron。未改 jobs.json/crontab/systemd。

### 11.2 验证与遗留观察

- `tests/test_db_connection_chokepoint.py tests/test_ops_scripts_sync.py`：**5 passed**；
  三个线上/仓内文件 SHA256 分别完全一致。完整棘轮家族 **70 passed / 0 failed**。
- 5 个脚本 py_compile + `--help`、两个 shell `bash -n` 均 exit 0；全链复盘、P1、P2b 和 RFC-194
  以当前 SIM 库只读实跑成功，未触网/未付费/未运行 gap 或 Lift。
- 首轮接入差异已修正而非豁免：`args.db` 变量名被字面连接棘轮命中，统一先解析 `db_path`；
  线上原文件均无末尾换行，仓内副本按原字节保留。
- **独立遗留观察**：RFC-194 当前数据复跑为 27 笔中有效 25 / stale 2（US.AAPL、US.XOM）。
  这是历史一次性脚本对当前扩展样本的新读数，不是本批引入；本批不改 EXT/prior_move 行为，
  后续应单独核 stale 原因，不能把 11-10 的 completed 当成当前持续健康证明。
- 本轮仅 scripts/ops/docs，脚本逐次加载新码，**无需重启服务**；任何策略收益/live readiness 不在本批范围。
