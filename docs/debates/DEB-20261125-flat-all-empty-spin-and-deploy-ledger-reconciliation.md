# 议题：11-24 交易链路诊断落地 —— 组合止损空转降噪 + 已部署代码归账

## 状态

- [x] 进行中（修复与归账完成，待重启冒烟与自然日志验证）
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

## 1. 背景：11-24 全日诊断结论（只读实查）

2026-11-25 上午对 11-24（周一）美股交易链路做只读诊断（SQLite mode=ro + `logs/ftseek_us.log-20261125` + OKR 评分脚本），结论：

- **结果**：新开 6 笔全 SHORT（11:01 ET 后，RFC-216 部署生效窗口），名义 $22,638.62；当日平 6 笔毛 +$78.30 / 费 $25.41 / 净 +$52.89，退出全为保护性（4×SL_BREAKEVEN + 2×DEAD_SIGNAL_EXIT，零 TP）。在仓 4 SHORT 盘前浮亏 ≈ -$260，距止损 2.5%–4.4%。OKR whole n=142 净 -28.86bps，KR1 NOT_MET / KR2 MET，单日小正不改负期望结论。
- **链路**：09:45–10:07 ET 缓存饥饿（已修）→ 10:23–10:30 VIX 硬门 NO_TRADE（正常风控）→ 11:01 起 RFC-216 放行；outbox 21 全 BOOKED、退出账本 6/6 TERMINAL、零 Traceback。
- **三项异常**（本批处理前两项，第三项登记）：
  - **A 组合止损空转**：legacy PositionMonitorLoop 账户级回撤 3.23%>3% 持续 tripped，`positions=[]`（Playbook 自管仓经 R-7c 排除），全天 **1231 轮 × 2 条 CRITICAL**（「触发 FLAT_ALL」+「0 仓中 0 成功/0 失败」）+ 每轮 trigger WARNING / 账户级 INFO，无任何动作。
  - **B 提交/部署漂移**：入场链路核心 `app/playbook/entry_data_fetch.py` 未跟踪、`market_regime.py` 的 `publish_cache` 片段与 SIM 5% 键 `risk.account.sim.max_peak_drawdown: 0.05` 只在 worktree；对应提交 `fff29129a`（入场取数有界）与 `a365e64ca`（SIM 5% 预算）在分支 `sim5-budget-release-20261121`，**不是当前分支祖先**。从 HEAD 干净重部署会 ImportError 或 SIM 预算静默回退 3%。
  - **C 今晨 09:09 CST 重启无 handoff 记录、无冒烟日志**（journal 实证 PID 4106323）；本批不追责，补冒烟并登记，执行方自认。

## 2. 修复 A：空仓段告警去重（仅日志，零交易行为变更）

用户指令「请按照建议合理优化修复，确保可正常交易」。**不接 tripped 进 Playbook 入场门**（那会停新仓，与用户指令相悖，且属 DEB-20261122 F1 待裁政策），只消空转噪声：

`app/risk/position_exit_monitor.py` 三处：

1. `__init__` 新增 `_flat_all_empty_episode_notified` 段标志。
2. `scan_positions` FLAT_ALL 分支：`managed_positions` 为空时 → 每状态段一条 WARNING（非 CRITICAL）后 `return closed`（与旧实现空循环后 return [] 的净效果逐字一致）；回撤解除（action≠FLAT_ALL）或持仓恢复时标志复位，重新计段。有仓时 CRITICAL/平仓/冷却锁存语义不动。
3. `_check_portfolio_drawdown`：空 managed 集时账户级 INFO 降 debug、trigger WARNING 抑制（由执行分支段告警承载）；有仓时原样。

**不变量**：`_check_portfolio_drawdown` 返回值不变；冷却锁存条件不变（空仓本就不锁存）；REDUCE_50/WARN 路径不动；不新增/删除任何交易门。

**验证**：`tests/risk/test_portfolio_flat_all_empty_dedup.py` 6 例（段内只告警一次/解除后重新计段/空仓不锁存不阻塞后续有仓执行/有仓 CRITICAL+冷却保留/部分失败重试保留/空仓 trigger 日志抑制）+ 既有 `test_position_exit_monitor_flat_all.py` 3 例 = **9 passed**（正常 conftest，存量豁免 2 次 pdt_guard 主库写为既有登记项）。

**遗留（不在本批）**：tripped 状态对 Playbook 时代实质空转的结构性问题（账户级熔断看不到唯一活跃管线的仓位）仍归 DEB-20261122 F1 预算/政策待用户裁；本批只降噪，不改变「tripped 不重置、人工盯盘+其他闸门兜底」的 11-20 已裁现状。

## 3. 修复 B：已部署代码归账（零运行时变更）

**方法**：不 cherry-pick（共享 worktree 脏、两分支在同批文件上分叉），改为把**已在生产运行**的 worktree 内容精确提交归账。

**文件集**：`fff29129a ∪ a365e64ca` 的 app/scripts/config/tests 文件 + `app/playbook/entry_routing.py`（RFC-145 B1 纯函数 +25 行，auto_execute_flow 的 import 依赖）。共 39 文件。

**逐文件一致性验证**（三方比对 base=f296a3491 / ours=HEAD / theirs=a365e64ca）：
- 33 文件 worktree == a365e64ca 精确匹配；
- 2 文件（market_regime.py、test_futu_dailyk_quota.py）== 干净三方合并 union；
- 3 文件 worktree = union + 已部署的额外批次，逐行人工核：
  - `futu_lv3_provider.py`：+DEB-20261118 §R16 快照 'N/A' 脏行逐行隔离（`_safe_float`）；
  - `auto_execute_flow.py`：+RFC-145 B1 chase_cap_anchor_headroom record-only 影子；
  - `unified_config.yaml`：+11-20 恢复注释 4 行（纯注释）。
- **import 闭包 AST 检查**：归账集对 HEAD+集内模块的 from-import 全部可解析（初查 7 缺口均为检查器 AnnAssign/命名空间包误报，逐一实证排除）。

**验证**：归账集关键回归 `test_sim_entry_budget_wiring + test_account_circuit_breaker_sim_budget + test_entry_data_bounded_fetch + test_futu_dailyk_quota + test_entry_context_cache_boundary` = **278 passed**（正常 conftest）。

**边界**：其余脏生产文件（scheduler_core / shadow_registry / daily_review / lesson_injector / trailing_stop / trail_state_store / position_monitor / da_* 脚本等）属其他批次在途 WIP，**本批不带入**，归各自 owner 归账；position_exit_monitor.py 的 DEB-20261120 §R2 enforcing_stop 既有 WIP 同样保留未提交，本批 A 修复用 hunk 级分离只提交自己的改动。

## 4. 修复 C：补冒烟与登记

09:09 重启后无冒烟记录。本批 A 部署重启后按军规跑 `scripts/startup_smoke_check.py --wait 90`，同时覆盖当前进程健康验收；09:09 重启的执行方归属待自认，本批不重置任何风控状态。

## 5. 部署与回滚

- **已部署（2026-11-25 10:14 CST）**：提交 `4e364f319`（B 归账 39 文件）+ `0fc72268f`（A 修复 4 文件，hunk 级分离，DEB-20261120 既有 WIP 7 hunk 未带入）。重启前核：SIM 4 仓（BA/BABA/CAT/AVGO）/两退出账本未决 0/outbox 非 BOOKED 0/09:09 后源码改动仅 position_exit_monitor.py（本批 A）/无 KILL_SWITCH/auto_execute=true。`systemctl restart ftseek-us` 新 PID 4112220，冒烟 **7/7 exit0**（`/tmp/deb20261125-startup-smoke.log`）。重启后 4 仓原样、peak 99999.496/equity 96872.827/initial 77806.99 不变、退出快循环 10s 接管、零 Traceback。
- **自然验证窗口**：PositionMonitorLoop 在 `MarketPhase.OVERNIGHT/WEEKEND` 按设计跳扫（position_monitor.py 循环 phase 门），重启时值 21:2x ET 属 OVERNIGHT，故空转降噪的自然日志验证待下一活跃时段（盘前 04:00 ET = 17:00 CST）；预期段首一条「FLAT_ALL 触发但 managed 持仓为空」WARNING、后续轮静默、零 CRITICAL。
- 回滚：A 为纯日志改动，回滚 = revert `0fc72268f`；无配置开关、无交易行为可回滚。
- 一次一变量：本批运行时变化仅 A 的日志分支；B 归账零运行时差异（提交内容 == 运行中内容）。

## 6. 未验证

- A 的自然日志验证待下一 tripped 空转轮（预期：段首一条 WARNING，后续轮静默）；
- 收益/命中率与本批无关，不宣称任何盈利改善；
- 干净 checkout 全量重部署演练未做（闭包检查为 AST 级，非全仓 import 演练）。

## 7. 部署后 10.5 小时复查（2026-11-25 20:4x CST = 07:4x ET 盘前）

用户令「请再核查日志记录和过程」，只读实查结果：

- **进程连续**：PID 4112220 自 10:14 部署连续运行，NRestarts=0；窗口内 **0 Traceback / 0 CRITICAL**，error_alert 逐窗扫 261 文件均 0 ERROR/CRITICAL，healthcheck 每 10min 通过。
- **降噪自然验证——触发条件自然消失（非故障静默）**：盘前 CAT 04:50 ET `SL_TRAIL_LOCK` 净+$43.24、AVGO 07:13 ET `SL_CERT_LOCK` 净+$107.86 两笔锁利平仓（PlaybookExitFastLoop 执行，intent BOOKED/ETH、legs TERMINAL、未决 0、钉钉推送走正路），净值 96872.83→97300.25，回撤 3.13%→**2.70% < 3%**，组合止损不再触发 ⇒ 零日志为正确行为。**WARNING 路径本身尚未自然演练**（监控 17:00 CST 恢复扫描前条件已解除），由 9 例单测覆盖；若 RTH 回撤重返 ≥3% 预期段首一条 WARNING。
- **在仓**：BA/BABA SHORT 浮盈≈+$0.34（近平），SL 167.74/88.1 距离充裕；退出快循环心跳新鲜（watchdog 120s 阈值零 stale 告警 ⇒ 监控在线）。
- **盘前过程链**：FutuLV3 17:00 一次强制重连成功（LV3 可用，零重连失败）；premarket_index 20:32 准点；EXT 臂候选 57/四闸正常/ordered=0 常态；DA 主 carrier（ET-pinned 08:00，enabled）21:00 CST 发车在臂，盘前综合预检 EST 21:45 在臂；regime 部署后无评估属设计（入场循环盘前 idle，RTH 09:45 ET 恢复）。
- **结论**：部署无回归，交易链路就绪；今日 RTH 为归账 HEAD + 降噪代码的首个完整交易日，观察点=DA 准点发车、22:30 CST 起 regime 评估、回撤 ≥3% 时段首 WARNING。
- **RTH 开盘核查（22:5x CST = 09:5x ET，用户令「已开盘 请核查是否可以正常成交」）**：DA 准点产出 17 指令（15 SHORT/2 BUY/1 SKIP fee floor）；regime 每 ~3min 评估、日 K 热加载增量生效、**开盘后零 timeout/busy（昨日饥饿未复发）、零 Traceback/CRITICAL**；当前 score -6.7 < -5 ⇒ NO_TRADE（VIX 21.8、SPY 697.9 -2.8%，盘前独立快照 composite -2.711%/QQQ -2.913% 互证真实跌日非数据错误），不新开属硬门风控非故障，score 回 ≥-5 自动恢复（昨日 11:01 ET 已实证该路径 6 笔成交）。出场链正常锁利：BABA 09:33 ET SL_TRAIL_LOCK 净+$53.94，今日净已实现 **+$205.04**（3 笔全锁利）；仅余 BA SHORT 现价≈160.20 距 SL 4.71%，快循环监控在线。不放宽 VIX 门制造成交（11-24 handoff 已裁）。

## 8. NO_TRADE SHORT 直接验证实验（2026-11-25 用户裁，预注册）

**裁决**：用户「目前本身就是模拟交易 并没真实资金入场 我需要直接验证」——明确覆盖影子先行默认（仓内先例：11-20 auto_execute 恢复、重启按需等用户裁覆盖默认）。SIM 风险由既有硬边界兜底：SIM 5% 峰值回撤预算、日亏三档、逐仓止损、四锁（LIVE 切换仍需签字）。

**假设**：高 VIX 恐惧日（regime=NO_TRADE，当前权重下 NO_TRADE 必含 VIX>20 负分项）DA 产出的 SHORT 指令具有正的**成本后**期望。支持证据：11-17→23 SHORT n18 +$196.93 vs LONG n13 -$688.03（小样本）；昨日 risk_off 6 SHORT 全净正。缺口证据：no_trade 日放空的成本后样本≈0。

**单变量变更**（其余一切不变）：
- 配置键 `playbook.execution_parameters.no_trade_short_sim_experiment_enabled`（默认 false，回滚=改 false 热载即恢复 NO_TRADE 全禁）
- 开启时：preflight 放行 NO_TRADE 轮 → 主循环**仅 SELL_SHORT** 过方向滤（LONG 记 `NO_TRADE_EXP_LONG_BLOCKED`）→ sizing NO_TRADE 乘数对 SHORT 取 risk_off 同款 **0.3**（非 SHORT 恒 0.0，双层拦截）
- **SIM-only 代码闸**：`is_live_capable_env()` 为真恒不生效；配置读取失败=关（fail-close 回旧门）
- 触发/reprice/EntryConfirm/chase_cap/VWAP/费用地板/借券/日亏/SIM 预算/止损全部原样；top_n/min_strength 等其余 regime 参数不动

**cohort 隔离**：实验成交在指令 metadata 落 `no_trade_short_exp`（vix/score/at），经 entry_booking_outbox 关联 trade；**不与 RFC-216 risk_off 子 cohort、qwen post 混算**（RFC-216 handoff 已裁口径）。

**预注册判据**：
- 度量：净 bps/笔（fees_est 后）、死单率、退出原因分布、MFE/MAE；借券费未统一计入的口径缺口沿用 OKR 披露
- 结算：n≥30 或激活后 10 个交易日先到者；判「可交易」= 净 bps>0；n≥20 仍显著为负可提前裁停
- 硬停：既有 SIM 5%/日亏/止损闸任一触发即按原语义停新仓，实验不豁免
- 到期结算入注册表（军规），判据 FAIL≠自动停采集，先查消费方

**验证**：新契约测试 `tests/playbook/test_no_trade_short_sim_experiment.py` 11 例 + preflight/open_floor 既有 = **54 passed**；regime/f5_shadow/mom_flow/pipeline 回归 **134 passed**（正常 conftest）。

**未验证**：收益假设本身待前向样本；今日恰为 NO_TRADE 日，激活后可立即自然验证链路（SHORT 进触发评估、LONG 被拦）；LIVE 路径仅单测级证据（生产 LIVE 冻结，不可能自然触达）。

**部署与自然验证（2026-11-25 23:3x CST = 10:3x ET）**：代码批 `ad44b0d34`（默认关）+ 激活批 `cd604501d`（RFC-217，治理闸交互确认经用户 AskUserQuestion 明确授权代答；commit-msg 闸要求 ADR/RFC 引用，补 `docs/decisions/RFC-217-no-trade-short-sim-direct-verification.md`）。重启前核：仅 BA 1 仓/两账本未决 0/outbox 非 BOOKED 0/10:14 后改动仅本批 3 生产文件。重启冒烟 **7/7 exit0**（`/tmp/rfc217-startup-smoke.log`），BA 原样/peak 99999.496 不重置。**首轮自然验证（VIX 22.00，score≈-7 NO_TRADE）**：日志「NO_TRADE SHORT实验 (SIM-only, VIX=22.00)」+ auto_execute 开始 17 条（此前整轮跳过）；13 SHORT 进评估，5 条 triggered 全被 **chase_cap_enforced** 拦（CRWD/TSLA/GS/AMZN/BABA，跌日追空保护仍生效）、CAT vwap_gate_blocked、余者待触发；2 LONG（LLY/MRK）记 `NO_TRADE_EXP_LONG_BLOCKED`；零 Traceback/CRITICAL。**首轮零成交属既有追价/VWAP 门生效，非实验故障**——实验不豁免任何执行质量门，样本将从真正过闸的成交开始积累。

## 9. 11-25 收盘复查（2026-11-26 CST，只读；未调整策略）

用户令「美股已收盘，请复查过程和结果是否合理，是否需要调整交易策略」。过程统计截点为 11-25 17:21 ET；经济复算读取 11-26 06:22–06:35 CST，主线程随后只读复核实际交易/指令行。RTH=09:30–16:00 ET，盘前/盘后单列；CST=ET+13h。**结论：下单与已平退出实际完成，但不能判为“端到端全部正常”或“策略已有正期望”。优先处理计划消费语义、时点/报价/归因与日报口径，不凭单日锁利放大 SHORT、强配 LONG 或放宽追价/VWAP/VIX门。**

### 9.1 执行过程与方向来源

- DA 08:02 ET 发车，扫描53→分析35→桥接17（当时15 SHORT/2 LONG），采集成功不等于所有输入完整：35份 event_context 均 `brief_date_mismatch`。凭据 `logs/da_pipeline_stderr.log-20261126:1,64,82,434,447`。BABA raw 的 `.collected.market_context` 明示 VIX=18.95 为 VIXY×0.85 前收代理，SPY/QQQ 的 current/prev_close/pre_price 分开；不能把生成阶段代理当执行时实时现货 VIX。
- 09:45–10:29 ET，15轮 NO_TRADE 整轮跳过发生在 RFC-217 激活前；10:30 后107轮 auto_execute 起止配对，最慢18.12s、最大间隔204.43s，无 `trigger_context timeout/busy`。NO_TRADE SHORT 实验放行43轮，约12:44代理 VIX 回落后转 RISK_OFF；故不是全天停摆。15:59 ET最后轮代理19.80/score−4/SPY716.68，见 `logs/ftseek_us.log:790-798`，不是外部独立现货验证。早段证据 `logs/ftseek_us.log-20261126:5611,5733,8380` 及评估原行。
- 14条可评 SHORT（原15中一条费地板排除）→10条实验标记触发→4个确认臂（MSFT反转弃）→CAT/BABA/GS三笔成交。原2 LONG被实验方向门拦；最终指令方向已可变为SKIP，不能用日终SKIP倒推最初无LONG。方向集中先在DA供给形成，再被NO_TRADE权限强化，非下单侧凭空改成SHORT。
- 闸门事件计数 chase=138/8标的、VWAP=178/9、斜率=54/9、gap=87/2；同票反复评估，**不是独立机会样本或误杀率**，不能拿事件次数证明门过严。全部既有质量门仍在。
- CAT10:45:51、BABA11:17:21、GS12:25:39 ET 新开，全SHORT、指令executed=1/成交FILLED_ALL。CAT/BABA先SUBMITTED未确认，随后同步补录trade1949/1950并LateFill `audit=True tca=True`；GS走outbox BOOKED。接管证据 `logs/ftseek_us.log-20261126:6184-6187,7047-7050`，不凭缺outbox就判为漏成交或“两不管”。CAT退出一度因活腿拒绝释放，约5秒后完成净额推送，不能当死锁；`:7168-7183`。

### 9.2 净结果：退出日与入场日必须分开

均为 PLAYBOOK_SIM/DA，费用是 `fees_est` 模型估计，借券费用口径未统一，不冒充实际逐笔成本对账。

| 11-25 ET 已平 | 入场日 | 退出时点/原因 | 净USD |
|---|---|---|---:|
| CAT trade1945 | 11-24 | 04:50 / SL_TRAIL_LOCK | +43.24 |
| AVGO trade1946 | 11-24 | 07:13 / SL_CERT_LOCK | +107.86 |
| BABA trade1944 | 11-24 | 09:33 / SL_TRAIL_LOCK | +53.94 |
| CAT trade1949 | 11-25 | 11:22 / SL_BREAKEVEN | +9.96 |
| 合计 | 旧仓3+新仓1 | 毛231.70−费16.70 | **+215.00** |

- 盘前已实现+151.10，RTH已实现+63.90，未见本窗口盘后已平；全日已平名义约14,669、净约+146.57bps，仅是本日退出集合读数。
- 旧仓前三笔合计 **+205.04**，精确支持§7/handoff当时数字，但**不是RFC-217收益**。本日新开3笔中，仅CAT已平+9.96（毛14.07−费4.11，约+27.41bps，落库MFE0.6878%/MAE0.6261%）；BABA47股@81.88、GS5股@789.14未平，净损益未知，**不得按0计**。另旧BA23股@160.17仍开。当天EXT新开0。
- `open_positions` 的BABA/GS行updated_at约12:25 ET、BA行停在11-23；不能把这些行的旧SL/TP或价格当精确收盘权益/有效止损真值。日报16:32 ET快照净值96,976/回撤3.02%不是16:00快照，也不等于+215已实现代表净值全天增加215。

### 9.3 RFC-217 cohort 链接与样本现状

- 精确 `playbook_instructions.metadata_json.no_trade_short_exp` 在11-25有10条、全SHORT；3条executed=1，7条未执行（3 VWAP/3 thesis_drift/1 entry_confirm_reversed）。样本状态是**成交3，已平1净+9.96，未平2**，不再是首轮零成交，但原§8和旧handoff是早期截点，并非需要静默改写的错误。
- 成交记录 `market_regime=risk_off`，却经decision_id关联到NO_TRADE实验标记；**生成/首次实验标记/实际成交时点不等价**。不能只靠trade.market_regime统计实验或混进RFC-216 risk_off效应。GS标记10:30、成交12:25也说明标记at不等于fill时点。
- trade metadata及outbox intent均未直接携带该标记；仅GS/5766有outbox BOOKED，CAT/5765与BABA/5774走LateFill并靠decision_id可复原。运行接管有证据，但**§8“经outbox关联”的预注册载体未完整覆盖LateFill路径**，应另核并补归因闭环，不能宣称现成outbox三笔全覆盖。指令关联也不等于已经证明所有未来恢复/重试路径。
- n=1已平远不足预注册n≥30或10交易日；继续按原结算/硬停，不据此宣称空头edge或提前扩大权限。实验与旧仓锁利、qwen post、RFC-216因果结论隔离。

### 9.4 长窗结果：单日小正没有解决净期望

独立官方CLI运行与GLM按实际SQL逐行复算一致；官方命令见§9.7。主KR为DA-only，A9/A10分别含/不含四口径本窗排除均0；全Playbook样本基数195，不是DA的146（来源拆分DA146/EXT47/MOM2）。全组合净−2196.68仅并列，不替代DA主KR。含/不含A9/A10的DA显式对照如下（n / 净USD），其余统计与下方同窗表一致，排除数均0：

| A9/A10取数组合 | whole | pre | post |
|---|---|---|---|
| 启用A9、启用A10（主口径） | 146 / −1587.23 | 107 / −1327.49 | 39 / −259.74 |
| 不启用A9、启用A10（含测试对照） | 146 / −1587.23 | 107 / −1327.49 | 39 / −259.74 |
| 启用A9、不启用A10（含旧时代/质量对照） | 146 / −1587.23 | 107 / −1327.49 | 39 / −259.74 |
| 不启用A9、不启用A10 | 146 / −1587.23 | 107 / −1327.49 | 39 / −259.74 |

| DA窗 | 已平n/入场日聚类日数 | 名义USD | 毛/费/净USD | 毛/费/净bps |
|---|---:|---:|---|---|
| whole 11-01起 | 146 / 17 | 639,061.25 | −970.67 / 616.56 / **−1587.23** | −15.19 / 9.65 / **−24.8369** |
| pre 11-01→16 | 107 / 11 | 490,365.17 | −874.37 / 453.12 / −1327.49 | −17.83 / 9.24 / −27.0715 |
| post 11-17起 | 39 / 6 | 148,696.08 | −96.30 / 163.44 / **−259.74** | −6.48 / 10.99 / **−17.4678** |

| DA窗 | 日等权mean净bps | t | MDE80(bps) | 日等权80%CI(bps) | 落库A11死单 / KR2 |
|---|---:|---:|---:|---|---|
| whole | −22.92 | −1.83 | 35.10 | [−38.98,−6.86] | 31/146=21.23%，MET |
| pre | −30.72 | −2.52 | 34.13 | [−46.34,−15.11] | 27/107=25.23%，功效不足 |
| post | −8.62 | −0.30 | 80.04 | [−45.24,+27.99] | 4/39=10.26%，功效不足 |

**CI/MDE的对象是每日Σ净/Σ名义的日等权均值，不是名义加权净bps点估计的严格区间。** 费bps为正成本，不是净收益贡献；三窗mfe字段缺失0，死单阈值为落库百分数mfe_pct<0.3。whole仍KR1 NOT_MET；pre/post各n<120须报功效不足，不能把post死单率低就判KR2达标。post CI含0、n39<120，按已裁方案B本次不触发Kill/Pivot；11-30结算日未顺延。as_of只控制评估/外推，不限制SQL取数上界，本次真实最大trade_date=11-25；不当历史冻结快照。

post方向并列：LONG14笔/4日净−688.71（名义加权−127.87bps），SHORT25笔/6日净+428.97（+45.23bps）；SHORT中11-24入场已平贡献+222.08，且单侧均n<30。非预注册方向诊断的日等权CI/MDE：LONG [−260.88,−9.20]/275.09，SHORT [+23.03,+59.27]/39.61；日数仅4/6，近似区间与同日相关/行情/执行混杂均需保留，**不以这个探索性切片批准砍LONG、放大SHORT或重开已终裁假设**。

### 9.5 不能忽略的流程/度量问题

1. **DA“严格计划”仍不是机器承诺**：CAT原反弹533.50实际519.07；GS805实际789.14；BABA原反弹83.95、禁止81.90以下追空，实际81.88且持夜。BABA原文字要求日内不裸隔夜；原JSON与现行reprice/确认/统一EOD执行存在语义差异，沿用 `DEB-20261124-da-multiday-direction-and-instruction-audit.md` §4待裁问题。不能说这三笔一定绕过机器chase门，或若按原价一定少亏。BABA确认10:39:14、发单/成交11:17:21之间约38分钟，确认状态陈旧与后续质量门的关系应单独核，不把当时确认当成交时验证。
2. **日报“excellent”不是策略达标**：`data/daily_reviews/daily_review_2026-11-25.json` 8.3/10、4/17=23.53%混合旧开今平与今开今平作为分子，却用今日17条指令作分母。`app/review/daily_review.py:91-99,201-203,543-553` 证明此字段不是今日新开转化。正确披露可为新开3/全部指令17=17.65%，或既有plan_vs_actual标的级3/计划14=21.43%，二者仍不同口径，不能冒充独立可成交机会率。8.3是含胜率/教训数的启发式评分，不含OKR样本门/净bps判据；`:1440-1501` 的“今日交易执行正常，继续保持”是规则默认建议，不是全链路验证。
3. **主交易日志无ERROR不等于全系统无ERROR**：根262日志本窗口ERROR12（盘前BODYW四层报价失败4、盘后不变量1、日K回填失败7），CRITICAL/Traceback0。BODYW不在今日17候选；七条回填失败的符号以原日志为准，不把回填错误直接归为三笔交易失败。`logs/rsshub_us.log-20261126:218,221,278,370`、`logs/invariant_check.log:8`、`logs/backfill.log:252-288`；backfill日期由该文件头证实。盘前2类/盘后1类error_alert真告警，CAT活腿释放被阻也另有真实推送。主RTH458 WARNING多含正常反复门禁，不能一律当故障或全部静默掉。
4. **不变量与观测缺口仍在**：16:05 ET不变量62处包括存量W7分母/I4 MFE、W6新/增长无栈、W2新字段退化与slippage13/33缺失；既有BROKEN窗保持原裁决，不改基线或关检测器使其转绿。当前Phase A来源/时点审计仍仅本地未部署，不能说本日线上完整同调用审计已生效。
5. **盘后亏损侧无执行止损是明确政策边界，不是只剩猜测**：EOD降级日志BABA86.26净−5.46%、GS801.03净−1.61%、BA164.53净−2.84%（`logs/ftseek_us.log:1337-1345`）与后续本地盘后遥测分开。`data/afterhours_exit_shadow.jsonl:32298-32300` 的17:20 ET同轮三仓均 `decision_detail=underwater_no_extended_loss_stop / action=hold_no_stop`，BA/BABA/GS有效SL167.74/86.43/831.95，quote_age约72/37/72秒，使用SHORT回补ask164.81/86.28/807.97，估算毛浮亏分别−106.72/−206.80/−94.15，合计 **−407.67 USD**。这是按盘口ask的模型估值，非成交保证/已实现，GS价差0.74%，不能把−407.67与当日+215直接相加称“当日净值变化”（旧BA跨日成本、期初浮盈亏未对齐）。
   - `app/playbook/pipeline_parts/exit_monitor_flow.py:71-76` 非TRADING不提交；`app/playbook/shadow_registry.py:3298-3308` 明载现行延长时段只保护利润、亏损侧不执行止损。故快循环在线/零未决账本不代表延长时段亏损仓有止损执行保证。
   - BABA11:22日志紧SL83.927是早期持仓分钟/MFE条件下的局部有效线；`app/playbook/exit_engine.py:588-615` 有适用边界并记录meta，不保证该紧线永久持续到盘后。不能把早期紧线与盘后价格硬比后指控已发生止损实现故障；本次遥测也未显示三仓ask越过其当轮SL。
   - 11-26休市/11-27半日市使水下持夜与原DA“日内了结”更值得优先裁清。评估现有假日/亏损侧风险预算与严格计划语义，不凭单笔浮亏重新开启已终裁隔夜/止损轴或直接强平；改变现行政策需独立用户裁决。

### 9.6 建议与边界

- **优先工程/语义核查，而非追样本调门**：先核BABA确认态龄/有效止损与盘后报价、假日持夜、DA严格计划vs咨询模式、LateFill实验关联和event_context日期；度量批另修日报新开/当日退出两套分母与启发式分数说明。实际行为改动需独立裁决/预注册，不顺带新建经济实验或修改已裁退出政策。
- **现有方向/VIX/仓位/追价/VWAP保持**，不强制50/50、不因SHORT小样本正就长期只空、不因低成交率放宽门或提高频率。长期DA仍负，后续按OKR已裁时间与人群评估策略去留，而非用一天4胜覆盖负期望。
- RFC-217继续原SIM-only/SHORT0.3/费用与SIM5%日亏止损闸、n≥30或10交易日结算；本复查不改开关、不清peak/tripped/UNKNOWN、不重启、不下单、不改prompt。Phase A发布仍需单独授权，当前线上过程不等同本地539项测试证明。

### 9.7 复现与未验证

官方评分独立复跑（vix-claim-review，exit0；GLM未调用CLI而以stdlib只读复算SQL，两类证据不混称）：

```bash
env -C /tmp PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/ftseek/FTSeek-US \
  /opt/ftseek/FTSeek-US/venv/bin/python \
  /opt/ftseek/FTSeek-US/scripts/okr_2026_11_scoring.py --as-of 2026-11-25
```

经济/关联复算连接必须 `sqlite3.connect('file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro', uri=True)` 并 `PRAGMA query_only=ON`；SQL要点如下（净额优先pnl_net，未平不计）：

```sql
SELECT id,trade_date,symbol,entry_time,exit_time,entry_price,quantity,
       realized_pnl,fees_est,pnl_net,exit_reason
FROM trade_records
WHERE source_type='PLAYBOOK_SIM' AND exit_price>0
  AND substr(exit_time,1,10)='2026-11-25';
SELECT id,symbol,entry_time,exit_time,entry_price,quantity,pnl_net,decision_id
FROM trade_records
WHERE source_type='PLAYBOOK_SIM' AND trade_date='2026-11-25';
SELECT i.id,i.symbol,i.executed,i.skip_reason,i.execution_time,
       t.id AS trade_id,t.entry_time,t.exit_time,t.pnl_net
FROM playbook_instructions i LEFT JOIN trade_records t ON t.decision_id=i.decision_id
WHERE i.date='2026-11-25'
  AND json_type(i.metadata_json,'$.no_trade_short_exp')='object';
SELECT instruction_id,attempt,state FROM entry_booking_outbox
WHERE instruction_id IN (5765,5766,5774);
```

表现读数另拼A9 `COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''` 与A10 `trade_date>='2026-07-01' AND COALESCE(data_quality,'') IN ('clean','real') AND COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL')`，DA按decision_id前缀分来源（评分器`:99-110,556-589`，`app/core/db_utils.py:104-109`）。本窗含/不含A9/A10各读数一致，但不外推未来无排除。账户实现对账与表现判分不可混用。

主线程最终核对 `/tmp/regime-caliber-20261126-k1wfp6l1/final-scope.json` 的14个源/测试SHA，全部与Phase A完工内容相同；该检查仅覆盖清单14文件，不冒充全仓零变更证明。两份文档 `git diff --check` exit0。

未验证：未独立外联核现货VIX/新闻或券商实时持仓/成交报价；未做原计划价/持夜/各闸门反事实成本后结果；未证明收益改善、方向因果或所有恢复重试路径；未做Phase A部署或自然新字段验证；主仓库源码/config/prompt/交易数据本批零改动，仅本DEB与handoff归账，未提交/推送/重启，未跑代码测试（只读复查）。

## 10. 现有空仓反转亏损与双向适应复盘（2026-11-26 CST，只读）

用户进一步要求「再分析现有持仓反转亏损的问题，美股的波动性，只做空也不对，请对交易链路和策略复盘」。本轮重新取本地持仓与带时点遥测，不以§9的17:20盘口冒充最新。主线程核市场输入与收益分布，GLM独立复算持仓路径，Qwen核跨模块根因，Kimi核最终声明；不把讨论当调整方向/止损/持夜政策的授权，不下单、联网取价或重启。

### 10.1 日线偏空与盘内反弹可以同时成立

本地 `data/replay_bars_daily/US_SPY.json:1` 的11-25日K为 `[date,open,high,low,close]=[2026-11-25,703.20,718.43,695.45,716.70]`，前一日收718.78。该文件mtime为21:11:54 UTC（16:11:54 ET），写方 `scripts/refresh_daily_bars.py:199-211` 取Futu KL_DAY/QFQ并写OHLC。按该缓存，收盘较开盘**+1.92%**、较当日低点**+3.06%**、较昨收**−0.29%**；高低跨度/昨收为3.20%。这些是本地日K描述，不是逐时可成交收益或外部行情准确性验收，也不能用日低价当持仓MFE。

因此，“相对昨收仍弱”“在长期均线下”“开盘后明显反弹”不矛盾。§9已核最后RTH SPY716.68仍低于MA20/50/200；只按慢均线做方向许可，并不能证明盘内继续做空合适。VIX是波动风险指标，不是下跌方向承诺；当前数值还含VIXY×0.85代理，不应把代理降温等同于反弹已结束或空头必胜。

另有输入口径缺口：35份raw的SPY `current=718.78/current_asof=11-24`，但snapshot的 `prev_close=722.29`。BABA raw给 `pre_price=705.1957/pre_chg_pct=−1.889`；按snapshot昨收重算为**−2.3667%**，按daily current重算为**−1.8899%**。35份均存在超过0.05个百分点的这类分母不自洽（共同输入，不是35个独立市场样本）；QQQ同例两基准近似一致。不静默改历史数据，也不由此断言方向错误由该差额造成。凭据为 `.raw.json:1` 的 `.collected.market_context.spy`，主线程小投影见 `/tmp/ftseek-reversal-review-20261126/index-evidence.json`。

主线程重新数35个非raw checkpoint：15 SHORT、3 BUY、17 SKIP；三BUY为LLY/META/MRK，META的原 `_signal_gate.decision=BLOCK`，LLY=WARN、MRK=PASS。桥接17条的15SHORT/2LONG是下游另一个分母，不能把原DA写成只输出2BUY；本轮未独立复现桥接过滤META的精确分支，不能只凭BLOCK字段裁因果。三个现仓也不是UE直接打单：已读BABA/GS `_ue_origin=false`，不能凭方向同为空就归为UE恢复。

### 10.2 高胜率没有抵消大亏损尾部

主线程针对§9相同DA post窗只读追加盈亏分布投影（PLAYBOOK_SIM、DA-前缀、trade_date11-17至11-25、已平、A9排除干净、A10 clean/real）：**39笔中32赢、7亏，胜率82.05%，仍净−259.74USD**。盈利合计620.91、平均每赢19.40USD；亏损合计−880.65、平均每亏−125.81USD。较大亏损包括ELV LONG −326.68/MU LONG −260.79（OVERNIGHT_GAP_STOP）、AMD LONG −181.91及AMD SHORT −101.45（SL_INITIAL）。这是费用估计后的样本分布，不是按美元均笔裁策略edge，也不能据此推断某个锁利规则造成了所有亏损。

本窗A9/A10四取数组合仍沿用§9已复算的同一39笔；本投影未重跑官方评分，官方post日等权80%CI `[−45.24,+27.99]`、MDE80=80.04bps、n<120的功效不足结论不变。不能把82%胜率、某日4胜或日报excellent当作正期望证明，也不能据此直接放宽SL、取消BE或拉远TP。复现SQL与逐行小投影为 `/tmp/ftseek-reversal-review-20261126/closed-payoff-evidence.json`；连接均mode=ro及query_only。

### 10.3 三仓不是同一种“盈利回吐”

GLM独立复算后，主线程实际读取 `data/afterhours_exit_shadow.jsonl:32520-32522` 并逐项算 `(entry−ask)×qty`。冻结为**11-25 18:34 ET记录批次**（recorded_at=23:34:08 UTC），不是答复时刻实时价；三仓provider update_time分别18:33:44/18:33:19/18:32:08，非同步快照。

| SHORT现仓 | 实际股数×入场 | 该批bid/ask | 报价龄秒 | 当轮有效SL | ask估毛浮亏USD | 观测MFE% / BE状态 |
|---|---|---|---:|---:|---:|---|
| BA trade1939，11-23开 | 23×160.17 | 164.65 / 165.00 | 22.738 | 167.74 | −111.09 | 0.346507 / false |
| BABA trade1950，11-25开 | 47×81.88 | 86.06 / 86.19 | 49.244 | 86.43 | −202.57 | 0.000000 / false |
| GS trade1951，11-25开 | 5×789.14 | 804.00 / 809.33 | 120.316 | 831.95 | −100.95 | 0.058925 / false |
| 合计 | 入场名义11,477.97USD | 非同步、GS陈旧 | — | — | **−414.61** | 三仓均hold_no_stop/settled=false |

这里用ask而非last估SHORT回补侧损益；估费另为4.2041/4.3515/4.1013USD，毛损不是已实现净损，也不是当日账户净值变化。GS价差0.6607%、报价龄超过120秒，不能把该ask称为可靠可立即成交价或把−415写成精确账户损失。BABA距当轮SL仅0.24USD，但止损价也不保证可成交。三仓该批ask均未越SL，并不意味着整个盘后从未越过。

记录的有利波动只有BA约0.35%（按159.615观测峰值约12.77USD）、GS约0.059%（按788.675约2.33USD），BABA观测峰值仍是入场价；不能描述成三笔均“大赚后吐”。BABA日志可见入场后逐步走亏、GS只有极小有利波动，反弹与入场择时问题比“已激活锁利却失灵”更贴近现有证据。峰值是遥测而非完整tick路径，不能断言未观测瞬间绝无浮盈。未平trade的mfe列0/空不作已结算KR2；BA观测0.3465%已高于A11的0.3%，所以**不得称三仓全是A11死单**。三仓本批 `breakeven_triggered=false`，不代表所有早期紧SL规则从未生效。

同轮盘口还存在时点/字段口径问题：GS前后批有update_time相同而ask变化，BABA其他批可见executable_price与ask差异。用冻结行分别列数，不拼不同批的last/ask/有效SL；原始entry_time还混CST裸串与ET偏移串，持仓时长须用交易账的已核时区，不能直接相减。

### 10.4 止损边界：盘后有过ask越线，不等于已证RTH程序故障

主线程另读 `data/afterhours_exit_shadow.jsonl:32062`：11-25 **16:01 ET** BABA bid85.88/ask86.88、价差1.1577%、session_volume=21，ask高于SL86.43，而last86.1915低于SL；该行明确 `initial_loss_stop_deferred_to_rth / hold_no_stop`，settled=false。这补充§9的17:20结论，不改写那个时点三仓ask均未越线的历史事实。Qwen追到实际入口、主线程再读 `scripts/afterhours_exit_shadow.py:204-224` 与 `scripts/extended_hours_profit_exit_rules.py:142-167`：SHORT消费ask对应的executable_price，**已识别保护线越过，因尚未BE而明确延后亏损止损**，last并不参与这次判定。不是last/ask读错，也不能把盘后ask事件冒充RTH程序故障。

该脚本虽名shadow，却在原盈利保护四种action命中、窗口内且非dry时调用真实退出executor（`afterhours_exit_shadow.py:238-251`）；不是全盘后零执行权。准确边界为**盈利侧可执行，亏损侧不执行初始止损**。app内RTH监控限定TRADING相，盘后程序在线不代表水下仓有连续亏损保护。盘后亏损侧不动作有既往用户裁决，本轮指出结构风险，不借复盘静默改执行权。

GLM核本地11-25 RTH日K的high为BA165.95/BABA86.31/GS806.16，分别低于当轮初始SL167.74/86.43/831.95；仅对这些已核RTH缓存而言未见初始止损穿线，不外推所有盘前、tick或降级报价都已验证。BABA曾有83.927早期紧线，不能与数小时后的盘口直接比后判失效；早期tight有持仓分钟/MFE适用窗，窗口结束可能恢复原线。若要求止损一经收紧不可回退，这是待明确的策略契约，不是本轮自动持久化紧线的授权。

当前三仓按实际股数、假定在初始SL精确成交的毛损情景分别为174.11/213.85/214.05USD，合计**602.01USD**；名义额分别3683.91/3848.36/3945.70USD。这是入场到SL的价差敞口，不是额外还会亏602，也不是gap/借券/滑点后的损失上限。原DA计划价/SL，在固定实际股数下对应110.40/105.75/87.50USD，不能冒充原计划实际分配股数后的风险预算。BABA从原83.95/86.20的2.68%距离变成81.88/86.43的5.56%，GS从805/822.50的2.17%变成789.14/831.95的5.42%；故不能用原文字里的2%–3%止损空间解释实际仓位。

### 10.5 源码与实账交叉后的机制结论

Qwen独立跨模块核查，主线程实际读关键函数、日志和指定trade/instruction；以下分清“规则确实如此”和“收益是否因此改善/恶化”。

1. **固定TP在出场侧被禁用，而非入场1.5门被绕过。** BABA出价81.92、SL86.43、TP75.16，取整几何RR约1.4989；成交81.88后RR=1.476923。`auto_execute_flow.py:1061-1072` 对分桶TP倍数生成两位小数价位；`exit_engine.py:748-783` 用成交价/初始SL重算，低于min_tp_rr=1.5−1e−9即忽略固定TP、交给trailing/EOD。实际日志 `ftseek_us.log-20261126:7181` 与trade1950 `_tp_rr_gate.swallowed=true` 同锚，已证不是仅metadata猜测。指令组合筛选默认min_rr=1.0（`pipeline.py:1774`、`portfolio_guardrails.py:107-115`，该RR筛选在方向闸开关前），不是同尺成交RR≥1.5的入场契约。**临界名义倍数、分钱取整、不利滑点与出场阈值未协调**，导致“计划有TP”和“实际固定TP可执行”不同；ε仅救浮点误差，不救取整/滑点。GS实际TP723.52/SL831.95、成交789.14重算RR=1.532819，本批未见吞TP标记。BABA本路径未达TP，不能称本次浮亏由TP禁用直接造成或恢复TP就可避免；也不称几何改变后仍永久禁用。源码注释所载82/223、36.8%是历史批次背景，本轮未重算当前30日发生率。
2. **确认新鲜度缺口与chase门正确工作并存。** 日志`:5902`为10:39:15 ET确认通过；`:5982-6810`后续chase_cap逐轮拒下帽价格；11:17下单81.92在81.90帽上，`:6994`受理、LateFill后81.88成交。`entry_confirm_gate.py:464-466` 对ST_CONFIRMED直接execute，pending TTL不是已确认态的最大消费年龄。等待约38分钟后仍消费旧确认，未重新验证确认本身的新鲜度；其他trigger/chase/预算仍逐轮评估。**不能写成追价硬门被绕过**，准确为下单价符合chase帽、成交随后滑回帽下，无同帽post-fill复核。补确认龄会改变所有确认指令节奏，须独立评审，不能借本轮顺手改TTL。
3. **方向集中来自供给与政策，不是VIX测出“只能做空”。** `market_regime.py:387-410` 在RISK_OFF给top_n=3/mult=0.3/min_strength=70，不作BUY/SHORT方向筛；NO_TRADE原全禁，RFC-217实验另只给SHORT0.3、LONG0。原DA35票15S/3B与桥接17票15S/2B是不同分母，不能说DA没有BUY。F1方向/行业裁剪开关false，既有would-skip影子不拦截（实日志`:6990`；配置`:1851`）；并非没有VWAP、chase或其他风险门。**不能用当日下午score−4倒推全部成交均非实验**：主线程核指令5766/5774原metadata均带 `no_trade_short_exp{vix:22,score:−7,at:15:30:5x UTC}`，trade1950/1951无同名顶层键，经decision_id对应已注册cohort。保留§9的3成交/1已平，不因混合生成/轮级/sizing/成交时点而移出人群；主线程另核旧sizing_delta：BABA10:30:52/GS10:30:55的gate_regime=NO_TRADE、gate_mult=0.3、size=47/5，而inst_regime_text=risk_off；这是原尺寸回执，不是fill当刻全部阶段的同快照证明。

   **还有授权持续性的非对称**：主线程读 `auto_execute_flow.py:600-607` / `db.py:1051-1067` 并只读核指令5770/5775，LLY/MRK raw方向BUY，实际列已是SKIP、reason=NO_TRADE_EXP_LONG_BLOCKED。这个分支把待执行指令写成终态SKIP，不只是本轮暂缓；后续循环先跳过SKIP，所以regime改善不会自动恢复同条BUY。SHORT的chase/VWAP暂缓则可逐轮重评。应明确裁决“NO_TRADE授权是本轮暂缓还是当日终态”，不能自动改回BUY或据此声称错过的LONG一定赚钱。VIX波动风险、慢日线趋势与快盘内方向应分别解释，而非把代理波动率当方向真值。
4. **earlytight与BE/TRAIL是不同层，前者非永久ratchet。** 生产early_tight_sl_minutes=30、sl_tighten_beta=1.0（配置`:1316/:1512`）；`exit_engine.py:561-611` 仅在未BE、非冷却、持仓分钟和MFE条件内局部改变_active_sl，没有把紧线永久写成初始SL。BABA日志`:7180`实紧至83.927，GS`:8445`实紧至808.8685；超窗可回到86.43/831.95。GS的盘后ask809.33超过旧紧线，不构成有效SL触发。BABA早窗内瞬时穿越无法靠日K或稀疏checkpoint排除，不能宣布所有时刻止损均已验证。三仓观测峰值尚未达0.5%BE阈值，未武装BE/TRAIL符合规则；“持仓曾有几分钱浮盈”不等于已获净保本承诺。
5. **DA的强语气不等于机器契约。** 原BABA“83.95反弹、禁止裸隔夜”、GS“805反弹、QQQ反转失效”和CAT“当日了结”，不能全按字面归因实际交易。chase_cap有硬消费者、entry/SL/TP被reprice重写、确认锚用reprice；valid_until不透传、adapter cohort=A、失效契约REVIEW_ONLY与禁裸隔夜文字无实盘消费者，见DEB-20261124§4及原适配器行257-322/380-389。入场archetype路由开关仍false（配置`:1814`）。它们解释语义差异，不证明缺消费者一定亏钱；不是再补一句prompt就已修好。
6. **持夜是退出政策敞口，不是“模拟就不需要”的风险。** RTH初始SL、窗口tight、BE/trailing、EOD豁免、ETH盈利保护、次日gap止损分别有条件；水下仓到盘后确实失去初始亏损止损执行，不可把快循环心跳当全天保护。BA前两夜EOD豁免日志分别见 `ftseek_us.log-20261125.gz:2496` / `ftseek_us.log-20261126:2046`；BABA/GS未平和ETHhold已实账证，主线程另核 `logs/ftseek_us.log:818-820`：11-25 16:01:11 ET三仓均有“EOD兜底豁免/用户三问Q3”持夜日志（GS−1.43%、BABA−5.35%、BA−2.73%，该日志毛百分比不是§10.3不同盘口时点的损益）。具体PRE_CLOSE/W_WEEK/cohort豁免全链未逐分支穷举，不用“overnight_hold_enabled=false”字面保证全部仓当天清零。这里不重开C14/C17或9/17亏损侧政策，不自动强平/反手/扩大SL。

### 10.6 策略复盘优先级与边界

**结论：值得调整的是交易契约与阶段一致性，不能先靠强制多空比例掩盖。** 日线下行环境中的反弹fade与盘中继续下跌做空是不同setup；同一VIX档里既可能有空头延续，也可能有beta反弹/挤空。建议顺序如下，均是后续议题，不是已修改的运行规则：

- **先处理可证的契约冲突**：固定TP从planned/reprice/submit/fill到exit是否仍可执行、确认是否仍新鲜、计划究竟咨询还是严格。优先把取整/滑点跨TP阈值与旧confirmed消费的边界纳入现有契约审查；不能直接降min_tp_rr、拉远TP或放宽chase来制造样本。若选择改行为，独立RFC、影响面/存量仓政策、回放/影子及回滚一次只改一变量；本轮不新建同轴经济影子。
- **再裁持仓期限/失效/假日风险的一致性**：用同一策略horizon说明入场、SL、TP、失效与EOD/ETH窗口；列明水下仓无ETH亏损保护和gap尾部预算。过去“全禁隔夜更差”不证明每一笔应该持夜，也不授权一刀切取消隔夜。需要用户裁具体语义后才谈机器字段消费，不以“永不下牌桌”承诺零回撤。
- **双向机会按setup质量与净期望分层，而非配比**：保持可出现BUY/SHORT/不交易，分别复盘顺势下跌SHORT、上涨日fade SHORT与反弹LONG；盘前/盘内与日线时点、同源单位、确认龄、原锚→执行锚、TP实际启用状态及持夜分层不可混。现有VWAP/追价/质量门保留，不为凑LONG松门，不把F1影子自动重接硬闸，不以单个上涨日翻多。Phase A同调用/多阶段审计仍本地未部署，不冒充已修方向适应。
- **评判净期望与尾部**：当前39已平的82%胜率仍净负；固定报告均盈/均亏、成本后净bps、日聚类不确定性、最大亏损来源、未平风险敞口。不能用高胜率替代收益、用总浮亏替代日净值或用1笔RFC-217已平裁方向收益。实验维持原n≥30或10交易日/硬停；主OKR n≥120与11-30结算不动。

**本轮实施边界**：只读数据/源码与2个既有MD归档，零策略/config/prompt/DB/订单改动、零重启/提交/推送；Phase A未部署。未外联核Futu真实成交可行性、VIX现货或新闻；无完整逐tick路径，不做“若原计划成交必少亏多少”的反事实断言。新增源码机制证据不等于新收益实验完成。

### 10.7 验证与归档

- GLM复算后已更正BABA名义3848.36/合计11477.97、BABA风险555.69bps、未平A11强度及+215.00；主线程亲读原始三仓盘口、16:01越线行、日K、核心函数与日志，指定SQLite均mode=ro/query_only，不import app/SDK或发起行情请求。
- Kimi实际读§10.1–10.6、原始盘口/日志/相关源码及经济投影；严重/中等项无，仅建议将“合法下单”缩为“下单价符合chase帽”，主线程已更正。Kimi未跑测试/DB/网络；审查不是策略收益验证，也不授权提交或上线。
- 格式复核命令：`git diff --check -- docs/debates/DEB-20261125-flat-all-empty-spin-and-deploy-ledger-reconciliation.md handoff.md`。SHA复核基线=`/tmp/ftseek-reversal-review-20261126/baseline.json` 与 Phase A `final-scope.json.code_test_hashes`：14源码/测试及5保护文件差异空，2既有MD原内容保留（前像docs-before.zip）。只保护列明范围，不声称全部运行数据从未变化。
- 服务只读复核 `systemctl show ftseek-us -p MainPID -p NRestarts -p ExecMainStartTimestamp -p ActiveState -p SubState` 仍PID4164761/active/running/NRestarts0、11-25 23:30:26 CST启动；本批零重启、未部署Phase A。handoff头新增本批结论/凭据/验证/未验证/下一步，未改历史条目或现役实验权限。

## 11. 「不过度保守 · 找明显错误」复核与执行（2026-11-26 CST，用户令；一处代码修正，其余经数据否决）

### 11.1 上一轮四条建议的复核结果（只读，`mode=ro` + `PRAGMA query_only=ON`）

| 上轮建议 | 复核结论 | 证据 |
|---|---|---|
| ① 成交前复验确认新鲜度（BABA 旧确认） | **不成立，撤回** | 11-17 起 42 笔 DA 成交，确认年龄最大 38.6 分钟，全部落在 DA 自身 `confirmation.ttl_min`（60–390）内；BABA 38.1 分钟对 TTL 180 |
| ② 量化 TP 可执行 RR（BABA RR 1.477<1.5） | **无已实现影响，不动作** | 12/42 笔 `_tp_rr_gate.swallowed=true`，全部经 BE/trail lock 出场，无一触及 TP；吞 TP 的已实现损益差为 0 |
| ③ 审计「按盘内计划严格执行」 | **被既有判据否决** | RFC-215：忠实执行 LLM 计划净 −$1,183（217 笔），严格计划模式劣于机器 |
| ④ 复审 LONG 持夜授权 | **不稳健，不动作（C17 维持）** | 见 11.2 |

### 11.2 LONG 持夜取消按方向拆分重放（`/tmp/ftseek-opt-20261126/c17_split.py`，算法同 `scripts/deb20261030_overnight_cancel_replay.py`）

- LONG 取消持夜全期 Δ=**+1,549.0**（n=97），但逐笔中位数 Δ=**−25.2**、38 笔变好/59 笔变差——总和为正靠 10–11 月少数几笔；按月 Δ：Jul −163.0 / Aug −175.1 / Sep −963.3 / Oct +799.8 / Nov +2050.7（符号不稳）；样本内（≤10-30）日K收盘臂 −501.7 对 15:52 臂 +478.5，**两臂异号**。
- SHORT 取消持夜 Δ=**−1,874.6**，凡 n>1 的月份全负。
- LONG 入场日收盘时浮亏的 90 笔：实际 −7,901.5，改当日收盘平 −6,557.3——亏损主要在入场日盘中就已发生，不是隔夜才产生。
- 结论：C17 复活条件（事前可识别子集 + Δ>0 + 不侵蚀修复腿）三条均不满足，**不重开**。

### 11.3 损益结构（2026-06-24..11-25 已平 928 笔；09-01 起按 regime）

- 引擎为正的部分：SL_TRAIL_LOCK LONG 盘中 171 笔 +6,646.04、SL_BREAKEVEN LONG 盘中 235 笔 +1,137.69、SHORT 盘中 186 笔 +1,084.03、SHORT 隔夜 24 笔 +1,046.70。
- 尾部：LONG 隔夜 101 笔 **−7,899.05**；其中 OVERNIGHT_GAP_STOP LONG 28 笔、胜率 0%、Σ −7,976.99；SL_INITIAL LONG 29 笔 −3,599.39；SL_ATR_STOP LONG 3 笔 −2,416.20。
- 09-01 起 LONG 在每个 regime 都为负、SHORT 在 risk_off 为正：

| regime | LONG n / Σ | SHORT n / Σ |
|---|---|---|
| risk_off | 37 / −1,366.58（≈ −36.9/笔） | 26 / +534.27（≈ +20.4/笔，胜率 96.2%） |
| cautious | 130 / −763.65 | 48 / −209.02 |
| risk_on | 107 / −448.61 | 36 / +88.93 |

  这是描述性统计，n 多数低于 A1 的 n≥60 成规线，**不据此改乘数**（C15 禁方向硬护栏；§5 禁强制多空配比）。留作裁决项，见 11.6。

### 11.4 已执行：RFC-217 LONG 首层拦截由「当日终态」改回「按轮暂缓」（一处代码，SIM-only 实验闸内）

- **病灶**：`auto_execute_flow.py` LONG 首层拦截调用 `db.mark_instruction_guardrail_skip(..., "NO_TRADE_EXP_LONG_BLOCKED")`，把 `direction` 写成 `SKIP` 并写 `skip_reason`。该列是终态字段（`db.py:1051`），后续循环先跳过 SKIP，于是 **regime 盘中回落后同条 LONG 仍当日死透**；开关回滚（改 false 热载）也救不回已写 SKIP 的指令。§10.6 已指出该不对称、留待裁决。
- **为何按「不改 LONG 权限」修**：RFC-217 §1 明写不改 LONG 权限。实验关时 NO_TRADE 是 preflight **整轮**拦截、指令保持 PENDING、regime 回落后 LONG 按 RISK_OFF 0.3 正常评估；实验开后 LONG 反而更严。同一病根仓库 2026-11-12 已修过一次（`_record_mom_defer` 注释：「暂时态写进终态字段」，下游 `premarket_invalidation` / `shadow_tracker` / `ue_batch` 把非空 `skip_reason` 当已跳过）。该测试夹具默认即把「永久 SKIP」列为禁止边界。
- **修法**：改用 `_record_mom_defer`（新增可选 `label` 参数，默认值不变，MOM 调用方零影响）：审计 `exec_gate_audit`（`gate=no_trade_short_exp_long`, `transient=True`，按 指令/日/原因 去重）+ warning「下轮复检」；指令保持 PENDING，不写 `direction`/`skip_reason`。`skipped_count` 计数与 `continue` 不变。sizing 第二层（NO_TRADE 下非 SHORT 乘数 0.0）原样，SHORT 路径、cohort 标记、LIVE 闸原样。
- **效果边界**：NO_TRADE 期间 LONG 仍完全不能开仓（两层都在）。唯一差别是 regime 回落后 LONG 回到实验前的基线评估。按 11.3，弱市 LONG 的历史期望为负——所以这一处是**保真修正而非增收项**，不应被解读为「多放 LONG 能赚钱」。
- **未动的 DB**：11-25 已被写 SKIP 的 LLY/MRK（指令 5770/5775）不回改（日期已过，改回无意义）。

### 11.5 验证

- 新增行为用例（`tests/playbook/test_entry_data_bounded_fetch.py`）：① 实验开、preflight RISK_ON、轮级 NO_TRADE：BUY 本轮不执行、不触发永久 SKIP、`direction` 仍 BUY、`skip_reason` 空、留下一条 `transient=True` 审计，sizing 与下单均未调用；② NO_TRADE 暂缓后 regime 回落 RISK_OFF：同条指令第二轮按乘数 0.3 执行，暂缓审计仍只一条（去重生效）。原参数化用例去掉对「必须写终态 SKIP」的断言。
- 顺手校正：Phase A 的 VIX 实时缓存新增 `quote_time` 字段，`tests/playbook/test_entry_context_cache_boundary.py::…[av_live]` 的精确字典断言未跟上（539 隔离集未含该文件），已补期望值；缓存消费方只读 `vix` 键，无行为回归。
- 命令与结果：`./venv/bin/python -m pytest tests/playbook/test_entry_data_bounded_fetch.py tests/playbook/test_no_trade_short_sim_experiment.py tests/playbook/test_mom_etf_v1_flow.py tests/test_auto_execute_open_floor.py -q -p no:cacheprovider` → **222 passed**。
- 扩大回归 `tests/playbook tests/test_playbook*.py tests/test_auto_execute*.py` → 1780 passed / 26 failed。26 例失败集中在 `_check_intraday_trigger` 入场取数路径（`test_low_atr_gate_screening_basis` / `test_m2_c2_live_quote_gate` / `test_playbook_service::TestDirectionGateC` / `test_offline_full_chain` / `test_risk_scale_wiring` / `test_falsification_capital_flow_shadow`，日志「trigger_context: busy」）。**因果对照**：把本批 LONG 拦截临时还原为旧写法后重跑同 6 个文件，仍 **26 failed**，随后按 SHA 复原 ⇒ 与本批无关，属既有失败（根因未诊断；`trigger_flow.py` / `entry_data_fetch.py` 均无未提交改动），本批不认领也不修。另 `test_valid_broker_rows…[2.5_1-BUY]` 在全量并发下超时失败一次、单跑通过，属 0.04s 超时夹具的时序抖动。
- 状态：**未提交、未推送、未重启**。现役进程 PID 36694 仍是旧写法。

### 11.6 归账更正与留裁

- **更正**：本会话此前记录「Phase A 未部署」。实查 `systemctl show ftseek-us`：服务于 **2026-11-26 09:09:18 CST** 被另一会话重启（PID 36694，NRestarts 0，无 handoff 条目、无对应 commit），全部 WIP 的 mtime 均早于该时刻 ⇒ **运行中的进程已加载 Phase A 14 个源码/测试文件及其他会话的在途改动**。Phase A「已上线」是既成事实，但不是本会话部署的。
- **留裁（不代答）**：弱市 LONG 授权。数据见 11.3（risk_off LONG −36.9/笔 n=37，SHORT +20.4/笔 n=26）。可选方向是另立预注册实验（如 risk_off 档 LONG 乘数下调），但 C15 禁方向硬护栏、样本量低于成规线，且 11.4 的修正本身会让 LONG 在弱市多一点评估机会。是否立项由用户裁。
- **不动作清单**：确认 TTL 复验、TP 吞咽、严格盘内计划、LONG 持夜取消、SL/TP 轴新影子（RFC-215 §6）、lessons 575/578 的「强制收盘平仓」（与 C17 冲突，仍待另批处置）。

### 11.7 用户授权重启与部署证据（2026-11-26 14:57 CST）

- **授权/提交范围**：用户明确「重启」，另交互允许仅提交本次修正。本地新commit `9532275dc`共6文件：`auto_execute_flow.py`仅暂缓修正、`test_entry_data_bounded_fetch.py`必要夹具和恢复用例、实验契约注释、DEB§11、RFC§6及handoff当条；用index局部内容暂存，未提交既有Phase A代码/审计测试、VIX缓存测试修正或其他会话WIP。未推送。较HEAD整体diff中的+25/−6是此前Phase A与本修正合并量，不能当作本次独立commit统计。部署续记只改既有MD，随本文档批提交推送。
- **测试续查（工作树，非纯commit镜像）**：新增恢复例改为连续两轮NO_TRADE保持PENDING、写库为空、审计仅一次，第三轮RISK_OFF按0.3执行。本轮4文件回归第一次221 passed/1 failed（signed-quantity `[-2.5_0-BUY]`），第二次221 passed/1 failed（`[query_error-knife]`边界未到达）；0.04s取数夹具存在时序抖动，与生产默认3s不同。两个失败分别单跑通过；针对恢复/独立stage读取/knife用例最终4 passed。保留两次失败，不以重跑覆盖或宣称全绿；日志 `/tmp/ftseek-opt-20261126/restart-regression.log`、`restart-regression-final.log`、`restart-targeted-regression.log`、`restart-targeted-final.log`。本轮未改生产超时或测试地板，既有全量26失败未认领。
- **前核**：`git status --porcelain -- app/ scripts/`仍列既有未提交改动；相对服务09:09:18启动mtime核，只新增本批 `auto_execute_flow.py`。两个出场账本 `data/exit_ledger.db` / `data/playbook_exits.db` 未BOOKED=0，entry outbox未BOOKED=0；未平BA23/BABA47/GS5股SHORT。mode=ro/query_only冻结id/方向/数量/入价/入时、持久化stoploss完整JSON、app/scripts Python SHA与两配置SHA，快照 `/tmp/ftseek-opt-20261126/restart-precheck.json`。
- **实际重启/冒烟**：`systemctl restart ftseek-us` → 服务14:57:37 CST启动，PID36694→55482，active/running，NRestarts0。随即 `PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90` → **7/7 exit0**（启动段14:57:41），日志 `/tmp/ftseek-opt-20261126/startup-smoke.log`；ConfigValidator/漂移检测/热重载/SSOT/双臂安全/退出快循环/零Traceback-CRITICAL均通过。
- **后核**：三仓id/方向/股数/入价/入时完全一致，stoploss JSON完整内容一致；出场/入账未决仍0，重启期间全量源码/配置SHA无变化。快照 `/tmp/ftseek-opt-20261126/restart-postcheck.json`。Kimi只读独立核服务与7项冒烟、复跑exit0，静态确认首层改为无终态DB写入的transient审计、第二层非SHORT乘数仍0.0。
- **未验证/口径限度**：休市无自然NO_TRADE执行/反转后的LONG成交，不声称已自然验证或收益改善；mtime仅证明在途源码早于旧启动、此重启未新增它们，不足以证明全部懒加载模块或测试由旧进程执行，纠正11.6中过宽的「14源码/测试已加载」措辞。冒烟证明启动设施/模式/快循环，不证明各策略完整保护。后续11-27半日市按原实验cohort与判据观察，不改参数或补回历史SKIP。
