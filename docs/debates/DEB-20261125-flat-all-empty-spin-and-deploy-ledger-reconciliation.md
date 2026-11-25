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