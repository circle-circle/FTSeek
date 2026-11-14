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
