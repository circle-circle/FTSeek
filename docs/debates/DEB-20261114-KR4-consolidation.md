# DEB-20261114 — KR4 实验组合盘点与聚焦（≤5）+ 降频 Pivot 立项

- 状态：**盘点已完成（只读）；降频影子已注册 record-only；退役按六面零引用 + 路由决策分批 staged，未 mass-delete**
- 用户请求：「按照建议执行」（2026-11-14，批准 DEB-20261114-weekly-pnl-diagnosis §9 方案A：KR4 盘点聚焦 ≤5 + 降频 RFC + record-only 前瞻）
- 纪律：六面零引用审计 skill（`.agents/skills/six-surface-zero-ref`）；做减法三步（证零消费→配置停→观察一周→代码退役，宪章 §四.1）；**命中活跃消费方=停，报路由不绕过删**。

## 1. 盘点读数（只读实测，2026-11-14）

注册表 REGISTRY=115 项；OBSERVING=61（含本批新登 1）。按性质三分：

| 类别 | 数 | 是否占 KR4 ≤5 实验名额 | 说明 |
|---|---:|---|---|
| PERMANENT_MEASURE（无毕业门测量基建） | 16 | **否** | post_exit_path/exec_gate_audit/edge_gate_shadow/shadow_portfolio/eth_held_exit_shadow/post_ca4487/ext_entry_risk_shadow/stop_invalidation_audit/wweek_peak_axis_shadow/entry_blackout_shadow/us_overnight_shadow/early_risk_shadow/da_event_context_shadow/ext_event_risk_live_shadow/da_instruction_quality_shadow/da_event_preexecute_shadow |
| **活跃实验（有毕业门）** | **~40** | **是 ⇒ 超编 ~8×** | KR4「≤5 个与净期望直接相关」的真正约束对象 |
| stale/挂账（含本批降频） | 7 | 视判据 | exit_t_arm_min_dd_08/risk_size_tier_shadow/da_true_reverse_shadow/ticket_size_breakeven/exit_session_window/ext_entry_earnings_criterion_shadow/da_frequency_cap_pivot_shadow |

- **关键更正**：KR4 ≤5 约束的是「有毕业门的实验」，不是全部 OBSERVING。16 条 PERMANENT_MEASURE 是测量基建，不占实验名额。**真正超编的是 ~40 条活跃实验。**
- `current_n` 在 import 时恒 0（须 `refresh_counts` 读数据文件刷新）；本盘点以数据文件实存/mtime 判活跃，多数 mt=11-13/11-14 ⇒ **仍在采集**，退役会断料 ⇒ 须先路由。

## 2. 建议 KEEP ≤5（净期望直接相关 + 开放轴；待用户路由确认）

围绕本轮真因「毛边盖不住费」（DEB-20261114-weekly-pnl-diagnosis §9.1）聚焦：

| 保留实验 | 轴 | 与净期望关系 |
|---|---|---|
| **da_frequency_cap_pivot_shadow**（本批新登，RFC-209） | 时间/频率（开放） | 降频提笔均净期望，直击费拖累 |
| fee_gate_shadow | 费地板成本守卫（开放） | 费是普适拖累 ~9.5bps |
| execution_tca_spread_shadow | 点差/滑点 TCA（开放） | 费/滑点测量，验证费模型是否过悲观 |
| risk_size_tier_forward | 仓位分层（开放） | 按亏损情形定仓位提期望 |
| budget_p3_sizing | 预算/仓位（开放） | 同上 |

> 注：EXT 财报判据影子（ext_entry_earnings_criterion_shadow）是 NVDA 型左尾的唯一开放数据面，但 stale_expected（429 停采）；若用户要推进须先迁富途日历主源（DEB-20261023），可作为第 6 项另裁，不挤占上述 5 项。

## 3. 退役候选分类（~40 活跃实验 → 聚焦；逐项须六面审计 + 路由）

- **A 已闭轴（假设已终局证伪，优先退役）**：confidence_tier_gate / conf_tier_guard_shadow（C12 分析质量）；gap_chase_gate / gap_alignment / extreme_gap_ban_shadow（C16/C8 缺口形态）；eod_depth_gate（B19 GATE_NOT_WORTH）；winner_overnight（C17 隔夜族）；eth_premarket_exit_shadow / eth_afterhours_exit_shadow / eth_earnings_direction_path（B21 卖早了 KILLED）；entry_band_widen / vwap_gate_ab / entry_delay_confirm / entry_confirm_forward / entry_anchor_dual（C9/C16 入场定价/形态）；ue_confirm_gate / ue_quote_freshness_shadow / short_funnel_shadow / intraday_loss_breaker_a_prime（UE/intraday 侧通道已关，OKR §2.3）；exit_disable_cf / exit_param_shadow / be_half_at_arm_d / sltp_verify_shadow（C1 退出冻结/C14）；factor_neg_edge_shadow（负 edge 门 7/11 停用）。
- **B 测量/审计（转 PERMANENT_MEASURE 或保留）**：blocked_exit_opportunity / instruction_conflict_shadow / da_event_prompt_canary / futu_channel_capital_flow / futu_channel_ofi_baseline / ssr_shadow_daily / da_blocked_cf / penetration_shadow。
- **C 休眠/挂账（按预注册结算）**：exit_t_arm_min_dd_08 / risk_size_tier_shadow / da_true_reverse_shadow / ticket_size_breakeven / exit_session_window；broker_capacity_shadow / broker_stop_drift_shadow / exit_intent_reaper_shadow（dormant_reason：sim 永不写/出事才写，属设计内休眠，**非退役对象**）。

## 4. 六面零引用证据契约（代表性实审计：confidence_tier_gate）

| 面 | 命令 | 命中 | 分类 |
|---|---|---|---|
| 1 仓内引用 | `rg -n confidence_tier_gate --glob '!archive/**' -l` | shadow_lexicon.py:193 / shadow_registry.py / tests/test_shadow_mounting_batch_a.py / 多 docs+obsidian+handoff | **活跃消费方=shadow_lexicon.py:193**；registry=定义排除；docs/obsidian/handoff=历史；test=测试消费 |
| 2 crontab | `crontab -l \| rg confidence_tier` | 0 | 零（scope=用户 crontab） |
| 3 systemd | `systemctl cat ftseek-us \| rg confidence_tier_gate` | 0 | 零 |
| 4 Hermes | `rg confidence_tier /root/.hermes/cron/jobs.json` | 0（文件实存 147781B） | 零（非 unavailable） |
| 5 git hooks | `rg confidence_tier_gate .git/hooks/（排 .sample）` | 0 | 零 |
| 6 动态字符串 | `rg confidence_tier scripts/ app/ -l` | confidence_tier_eval.py（本体 eval）；voting_accuracy_tracker.py:211/324 与 exit_calibration_refresh.py:152 = `by_confidence_tier`/`confidence_tier_stats` **假阳性子串**（非本影子） | eval=本体；其余假阳性 |

- **结论=STOP（命中活跃消费方 shadow_lexicon.py:193）**：退役须先路由 lexicon 条目 + eval 脚本 + 测试引用，**不绕过删**。这正是「~40 实验多数仍在采集 ⇒ 退役须逐项路由」的实证。
- 字面搜索词：`confidence_tier_gate`（精确）/`confidence_tier`（子串，已剔假阳性）；外部路径实查 `/root/.hermes/cron/jobs.json`（实存）、用户 crontab、`systemctl cat ftseek-us`、`.git/hooks`。

## 5. 分批 staged 计划（不 mass-delete）

1. **用户路由确认** KEEP ≤5（§2）+ A 类退役名单（§3）。
2. 逐项六面审计 → 零活跃消费方者：配置停（冻结写方/cron）→ **观察一周** → 代码退役（宪章 §四.1）；命中者报路由（如 confidence_tier_gate 的 lexicon）。
3. C 类挂账项按各自预注册判据**当日结算入注册表**（军规），数据保留不删。
4. 降频影子（RFC-209）前瞻攒样 n≥30 日（约 2026-12-28）批量裁。

## 6. 本批已落地（record-only，零交易行为变更）

- RFC-209 `docs/decisions/RFC-209-da-frequency-cap-pivot-shadow.md`（预注册：cohort 2026-11-16、N∈{2,3}、n≥30 日、日聚类 80% CI 下界>0 ∧ mean>MDE80）。
- 结算器 `scripts/settle_da_frequency_cap_pivot.py`（mode=ro，复用分析器）+ 注册表 `da_frequency_cap_pivot_shadow`（count_mode=json_key:uncapped.days，min_n=30，eval_on_demand，stale_expected）。
- 契约测试 `tests/test_settle_da_frequency_cap_pivot.py`（4）+ `tests/test_pnl_expectancy_pivot_analysis.py`（4）= **8 passed**；注册表相关 7 套件 **77 passed**；intent_uniqueness 无冲突；REGISTRY 115 无重 id。
- 首跑结算 verdict=**TOO_EARLY**（days=0，前瞻 cohort 未到）。

## 7. 未验证 / 下一步

- **未验证**：降频前瞻判别力（n<30）；任何收益/live readiness；A 类退役的逐项六面审计（本批仅 confidence_tier_gate 代表审计）。
- **下一步（待用户路由）**：确认 KEEP ≤5 + A 类退役名单 → 逐项六面审计 + 冻结 + 一周观察 → 退役；C 类挂账结算。
- **未做（明确）**：未 mass-delete 任何影子；未改任何交易行为；未重启服务。

## 8. 用户裁决（2026-11-14「同意」）+ 六面审计定论：A 类全部命中活跃消费方，不可 mass-retire

用户「同意」批准 **KEEP ≤5（§2）+ 方案B 影子先行**。KEEP≤5 定案：`da_frequency_cap_pivot_shadow` / `fee_gate_shadow` / `execution_tca_spread_shadow` / `risk_size_tier_forward` / `budget_p3_sizing`。

**逐项六面审计（surface 1/6 仓内+动态字符串；surface 2 crontab+/etc/cron.d；surface 3 systemd；surface 4 Hermes jobs.json 实存 147781B；surface 5 git hooks 零）定论：24 条 A 类候选全部命中活跃消费方 ⇒ 全部 STOP，须逐项路由，无一可干净退役。** 代表路由表：

| A 类候选 | 活跃消费方（STOP 依据） | 路由需求 |
|---|---|---|
| confidence_tier_gate | shadow_lexicon.py:193 + eval 本体 | lexicon 条目 + eval + 测试 |
| conf_tier_guard_shadow | shadow_board.py + cron 06:05 | cron + 看板 |
| gap_chase_gate | lexicon:34 + gap_chase_gate_eval.py + cron 06:10 | lexicon + cron |
| gap_alignment | lexicon:54 + gap_alignment_shadow.py + cron 05:52 + 多脚本 | lexicon + cron + 下游脚本 |
| extreme_gap_ban_shadow | lexicon:49 + deb20261002_cut_tail.py | lexicon |
| **eod_depth_gate** | **生产 exit_engine/overnight_decision/overnight_cleanup/trade_identity** + lexicon:107 + cron 06:30 | **生产消费方路由** |
| **winner_overnight** | **生产 exit_engine** + settle cron 06:25 | **生产消费方路由** |
| eth_premarket/afterhours_exit_shadow | lexicon:257/262 | lexicon |
| eth_earnings_direction_path | lexicon:247 + earnings_direction_path_eval.py + shadow_board | lexicon + eval |
| **entry_band_widen** | **生产 pipeline/trigger_flow** + criteria_spec/verdict_era + lexicon:272 + settle | **生产消费方路由** |
| **vwap_gate_ab** | **生产 pipeline/trigger_flow** + lexicon:95 | **生产消费方路由** |
| **entry_delay_confirm** | **生产 entry_confirm_gate** + lexicon:39 + cron 05:58 | **生产消费方路由** |
| **entry_confirm_forward** | **⚠️ 已重开（2026-11-05 用户裁「重开 entry_confirm v2」, OBSERVING, entry_confirm_enabled=true, RFC-158 预注册）** | **移出 A 类，不退** |
| **entry_anchor_dual** | **生产 trigger_flow** + entry_anchor_dual_eval.py + cron 05:53 | **生产消费方路由** |
| ue_confirm_gate | lexicon:208 + llm_universe_scan.py + watchdog + cron 06:06 | lexicon + 供料脚本 |
| **ue_quote_freshness_shadow** | **生产 futu_lv3_provider** + cron 21:15 | **生产消费方路由** |
| **short_funnel_shadow** | **生产 scheduler_core** + lexicon:158 + 多脚本 | **生产消费方路由** |
| intraday_loss_breaker_a_prime | lexicon:59 + cron 06:28 | lexicon + cron |
| exit_disable_cf | lexicon:44 + exit_disable_cf_shadow.py + cron 06:25 | lexicon + cron |
| **exit_param_shadow** | **生产 scheduler_triggers/trade_identity/feature_flags/exit_shadow** + lexicon:138 | **生产消费方路由** |
| **be_half_at_arm_d** | **⚠️ 活跃前瞻实验**：breakeven_shadow_settle.py D 节 + cron 06:38（**字面 id 搜索漏判，路径分派消费**） | **非零消费，不退** |
| **sltp_verify_shadow** | **生产 feature_flags/db/auto_execute_flow** + lexicon:128 | **生产消费方路由** |
| factor_neg_edge_shadow | lexicon:235 + factor_neg_edge_shadow.py | lexicon + 本体 |

**关键更正（两处 A 类误列）**：
- `entry_confirm_forward` 已于 2026-11-05 被用户裁**重开**（OBSERVING + entry_confirm_enabled=true + RFC-158 预注册）⇒ **不是已闭轴，移出退役名单**。
- `be_half_at_arm_d` 是**活跃前瞻实验**（D 臂 BE 减半，寄生 breakeven_shadow_settle.py，cron 06:38）⇒ 字面 id 搜索只命中注册表定义会**误判零消费**；六面第 6 面（路径分派）追踪 eval_script/data_path 才见真消费方 ⇒ **非退役对象**。

**定论与路径**：KR4 退役**不能 mass-batch**——10+ 条 A 类 woven into 生产代码（exit_engine/pipeline/trigger_flow/scheduler/auto_execute_flow），其余有 lexicon/cron/eval 消费。每项须：路由生产消费方/lexicon → 配置停（真停采机制，非 collection_retired 假停）→ 观察一周 → 代码退役。**本批不 mass-delete、不 mass-KILLED、不摘 cron**（命中活跃消费方=STOP，报路由不绕过删）。KR4 名额收敛走**逐项裁决**（闭轴假设路由后 KILLED）+ B 类转 PERMANENT_MEASURE，staged 推进。

## 9. 方案B 孤儿仓修复已立预注册（record-only，不占 KR4 名额）

- `docs/decisions/RFC-210-backfill-fidelity-inherit-instruction-truth.md`（方案B：补录继承指令真 regime/ATR；B1 度量面/B2 行为面分阶段；前瞻 n≥30 补录事件、保真度 100% + 结局守卫 + 零回归才提请上线，另立 DEB+用户裁+重启前核）。
- 事前证据尺 `scripts/backfill_fidelity_probe.py`（mode=ro）+ 注册表 `backfill_fidelity_probe`（**PERMANENT_MEASURE，不占 KR4 ≤5 净期望名额**）+ 契约测试 3。
- 事前实跑：n=65 joined=65，regime 保真度 3.1%、指令真值 100% 可得、可修正 63 笔、代理 vs 真 ATR 中位差 4.34。
- **B1（regime 度量面）已上线（2026-11-14 20:26 CST，用户「按照建议执行」+ 广授权）**：独立核实 `trade_records.market_regime` 存储列零交易消费方（出场用实时 MarketRegimeGate、执行用指令 regime、存储列仅复盘/看板消费）⇒ B1 属白名单修尺子（写路径标注修复），非 behavior-touching。MainPID 2948878→2967178、冒烟 7/7、9 仓不变、非 BOOKED=0、定向测试 13 passed、resolver 64/67 继承指令真值。详见 RFC-210 §10。**B2（ATR/SL 行为面）仍冻结待裁**；前瞻保真度=100% 需 n≥30 新补录事件（11-16 起攒样）。

## 10. 第一批逐项六面审计证据契约（staged 路由起步，2026-11-14）

按 six-surface-zero-ref 技能对最安全的 lexicon-only 候选 `extreme_gap_ban_shadow`（D1'）做详细六面审计。**结论：STOP（命中活跃消费方），且 gap 轴三件耦合须同路由，不可单件退役。**

| 面 | 实跑命令 | 命中 | 分类 |
|---|---|---|---|
| S1 仓内引用 | `rg -n extreme_gap_ban --hidden -g '!.git' .` | shadow_lexicon.py:49；shadow_registry.py:324（recheck 排程 read_at 10-22/recheck_after **11-21**）；:1516/1521/1890/1896（gap_alignment/gap_chase_gate cross_ref）；:2320（本体定义，排除）；handoff/docs/archive/DEB（历史，排除） | **活跃消费方**：lexicon + recheck 排程 + 两条 cross_ref |
| S2 crontab | `crontab -l`＋`/etc/crontab`＋`/etc/cron.d/*` | 0 | 零（scope=用户+/etc 全量） |
| S3 systemd | `systemctl cat ftseek-us`＋`rg /etc/systemd/system` | 0 | 零 |
| S4 Hermes | `rg extreme_gap_ban /root/.hermes/cron/jobs.json`（实存 147781B） | 0 | 零（文件可读，非 unavailable） |
| S5 git hooks | `rg .git/hooks`（排 .sample） | 0 | 零 |
| S6 调度/动态串 | eval_script=`unit_cell_daily_readout.py`（D1' 小节）；data_path=`gap_alignment_shadow.jsonl` | unit_cell_daily_readout D1' 读数；**data_path 三件共用**（extreme_gap_ban/gap_alignment/gap_chase_gate，n 完全相同） | **活跃消费方**：eval 本体 + 共用 data_path |

**STOP 依据**：①假设虽已终局证伪（9/13 NO_ADJUDICATION_FAIL_ON_FALSE_KILL，误杀 50%>40%；10-22 重看 53% 仍不过），但 ②lexicon 条目 + recheck 排程（**recheck_after 2026-11-21**，下周到期）+ ③**与 gap_alignment/gap_chase_gate 共用 `gap_alignment_shadow.jsonl`**（registry:1890 cross_ref 明令「两条不得独立转正，归属先裁」）+ ④eval_script unit_cell_daily_readout D1' 仍消费 ⇒ 非零引用，禁删。

**路由决策（coupled，staged）**：gap 轴三件（D1' 禁入提案已证伪 / gap_alignment 分源观测 / gap_chase_gate B1 执行侧门 KEEP_SAMPLING）共用单一 data_path，**采集停不得早于三件全路由**。起步＝等 **2026-11-21 recheck** 到期复看 → 三件归属同裁（执行侧归 gap_chase_gate、禁入归 D1'、分源归 gap_alignment）→ 真停采 → 观察一周 → 代码/lexicon/cron 退役。**本批不删、不停采、不摘 cron**（命中活跃消费方=STOP，报路由）。下一候选（factor_neg_edge_shadow / confidence_tier_gate 等 lexicon-only）同法逐项推进。
