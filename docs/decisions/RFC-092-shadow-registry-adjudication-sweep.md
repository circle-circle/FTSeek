# RFC-092: 影子注册表清账批 — 19 个"样本量达标"事项的判据实测与状态收敛

> **状态**: 已执行 (2026-09-21, 用户裁"按建议执行")
> **类型**: L1 (注册表状态收敛 + 1 个新字段 + 1 处守卫; 零交易行为变更)
> **前置**: 2026-09-13 实锤「READY_TO_EVALUATE ≠ 判据达标」(5 个 READY 实测 0 个过门)
> **触发**: 用户提问「现有门禁和预注册是否过于叠加 / 有没有已存在的门禁能力」

## 1. 问题陈述

影子注册表（`app/playbook/shadow_registry.py`）是全部 record-only 事项的 SSOT，但它的生命周期
有 **入口无出口**：`refresh_counts()` 只在 `current_n >= min_n` 时机械迁移 `READY_TO_EVALUATE`，
而该状态的真实语义是"**样本量**达标"，不是"判据达标"。

清账前实测：注册表 **37 项** 中活跃 28 项，其中 **19 项已被机械迁移为 READY** —— 逐个跑
`eval_script` 拿判据实测值后，**0 项判据通过待转正**。其中：

- 4 项判据**已实测不过或反向**却仍在占位攒样；
- 3 项**根本没有可执行判据**（无 eval_script），READY 是虚假待裁；
- 1 项自述"无毕业门（永久测量项）"，min_n 只是呈报门槛，被误标为待裁。

"叠加感"的真身是**裁决欠账**，不是风控层堆叠。会改变行为的硬门本身很少：触发侧 14 道 +
执行侧 9 道，其中信号质量类只有 5 道（接飞刀 / 追价帽 / 低ATR / VWAP相位 / 极端跳空）。

## 2. 方法（可复现）

1. `refresh_counts()` 取样本达标集（只读，不改盘）；
2. 逐项跑 `eval_script`；
3. **含不可逆副作用的脚本绕开 main()**，只 importlib 调纯判据函数：
   - `kr3_runup_report.py` — main() 会推送 → 只调 `build_report()`
   - `intraday_loss_breaker_shadow.py` — main() 会 persist 写库 → 只调 `replay()+summarize()`
   - `penetration_llm_analysis.py` — main() 会调 LLM + 开 DEB 草稿 → 只调 `baseline_stats()/check_milestones()`
   - `da_prompt_distill_shadow_eval.py` — main() 会**自删 crontab 行** → 只调 `evaluate(load_rows())`
4. 每项把实测值与**该条预注册判据门槛**逐条比对，不看注册表状态、不看 🔔 横幅。

产物：`/root/.hermes/cache/scratch/ready_shadow_{scan,phase1,phase2}.json` +
`ready_shadow_adjudication_report.md`（含 19 项完整判决表）。

## 3. 判决与落地（本 RFC 实际改动）

| # | 事项 | 判据实测 | 处置 |
|---|---|---|---|
| 1 | `p1_1_capital_flow` | 四关门产物(8/22) `all_gates_pass=False`（inflow t=−0.48 / outflow t=−0.71 不显著、IS/OOS 异号、placebo p=0.85）；且用户已裁"不得拦单" | **KILLED**（已裁未归档 → 同步） |
| 2 | `da_prompt_distill_shadow` | 预注册判据逐字重跑：①违约率 v2 6.06% vs V1 1.52% ✗、④token 降幅 2.57%<15% ✗、②③✓ → 判据明文"任一不过 → ARCHIVE" | **KILLED (ARCHIVE)**（采集 cron 已于 9/17 用户裁注释摘除，本次只同步状态） |
| 3 | `pullback_limit_shadow` | ADR-035 判据 n_paired=49≥20 ✓、①净期望改善 84.2%≥30% ✓、②成交率 27.1%<50% ✗、③mfe=0 占比 0.0→0.0 未下降 ✗ ⇒ 2/4；另 D 误杀率 54.9%>40% ✗、日聚类 t=0.62 MDE±20.5 不可分辨 → `wire_routing=false` | **KILLED（撤回接线）** |
| 4 | `overnight_kill_criterion` | 判据 B15：w_week 已平 n=23≥20 ✓、修复率 **69.6% ≥ 40%** → 判据明文"维持，翻 SUPERSEDED 留痕" | **SUPERSEDED**（结案语义，无替代项） |
| 5 | `intraday_loss_breaker_a_prime` | 全期 n=204、被挡笔期望 **+45.0**（门 ≤−10，方向相反）、被挡笔累计净 +$9,180；last30d n=14 仍 +$146.75 同号 | **不裁**（forward 仅 14<30，按该条 2026-09-13 已记录规则"backfill 有前视偏差 ⇒ 期望符号不可作裁决依据"）→ 仅叠加 `verdict_history` 读数 |
| 6 | `futu_channel_top_movers` | 无 `eval_script`（判据自述"评估脚本待建"），却已机械 READY | **挂账** `no_adjudication_reason=NO_EVAL_SCRIPT`（处置二选一待裁） |
| 7 | `futu_channel_iv_hv` | 同上（判据可跑但无执行体） | **挂账** 同上 |
| 8 | `post_exit_path` | 判据字段自述"无毕业门（永久测量项）"，min_n=100 只是呈报门槛 | **挂账** `no_adjudication_reason=PERMANENT_MEASURE` |

**代码改动**：`ShadowItem` 新增字段 `no_adjudication_reason: str = ""`；
`refresh_counts()` 增加守卫 `not s.no_adjudication_reason` ⇒ 挂账项不再机械迁移 READY
（治"虚假待裁"通知，不动其采集）。
**联动测试改动**：`tests/test_da_blocked_cf_eval.py::test_registry_pins_phase_c` 把
`pullback_limit_shadow` 的状态**钉到具体值 KILLED**（比原"活跃态白名单"更严：未来任何漂移即红），
其余两项仍要求活跃态。

## 4. 清账后状态

- 状态分布：OBSERVING 12 / READY 12 / KILLED 9 / GRADUATED 2 / SUPERSEDED 2（清账前 READY 19、KILLED 6）
- 待裁队列 19 → 12（−3 退役 −1 结案 −3 挂账）
- 挂账 3 项：`futu_channel_top_movers`(1043) / `futu_channel_iv_hv`(87) / `post_exit_path`(511)

## 5. 未改动项（实测读数留痕，状态不动）

| 事项 | 清账读数 | 为何不动 |
|---|---|---|
| `kr3_direction_runup_gate` | LONG blocked n=36 mean_gap −1.9 / 中位 +1.1 → TAIL_ONLY；SHORT blocked **n=16<30** | 判据⑤"子集 n<30 → 不裁决，延长观察"；阈值冻结禁改 |
| `vwap_gate_ab` | 被拦组反事实均值 **+0.176%**(n=130) vs 放行组已实现 +0.001% → 倾向"拦错" | 判据要求"双臂净差 t-test + 按日聚类 MDE"，脚本未出该检验 → 先补检验再呈裁 |
| `extreme_gap_ban_shadow` | 被拦 n=58 净 −1826.61 ✓、n ✓，但**误杀率 53% > 40%** ✗ | 判据未过 → 维持纯观测 |
| `entry_delay_confirm` | 全样本 回避 51%/机会成本 0.187% 过线，但 forward 子样本全 ✗ + 日聚类 t=0.07 MDE±7.95 ⇒ 不可分辨 | 判据不可分辨 → 不呈裁 |
| `da_blocked_cf` | T+1 mean −0.051% 未越 ±0.15% 线 ⇒ KEEP_SAMPLING（T+5 −0.793% p=0.0006 仅一致性披露） | 判据明文"T+3/T+5 不参与裁决（防多窗拣选）" |
| `exit_disable_cf` | 每规则 MFE n=31<50 / DEAD n=33<50（saved 中位已过费用线） | 每规则 n 门未达 |
| `gap_alignment` | 各格 n=5/10/22/9… 多 <30 | 格内 n 门未达 |
| `premarket_invalidation` | A/C 恒 `RETIRED_NO_PROMOTION`（已按 DEB-20260911 处置）；B exec n=4<20 | B 攒样中 |
| `penetration_shadow` | 基线穿透率 97.4%；milestones=None（假设集需 LLM 生成） | 未调 LLM 不判；不自动开 DEB |
| `futu_channel_ofi_baseline` | 产物仅给出 `rank_ic_incremental_vs_ofi=−0.1192`（capital_flow 相对 OFI 无增量）；**OFI 自身 IC 未被单独测得** | 缺独立读数 ⇒ **不构成 KILL 依据**（防"无证据裁决"） |
| `gap_chase_gate` | 0 样本（断供） | 属喂数方/挂载问题（T15），非判据问题 → 修采集而非退役 |

## 6. 影响面与验证

- **零交易行为变更**：只改注册表状态/元数据 + 通知迁移条件。**未动任何 cron 载体** ⇒
  被判退役的项其采集仍在跑（数据保留、可回滚）；若要停采需另立变更（关开关/摘 cron 行）。
- 验证：受影响测试面 11 个文件 **基线(HEAD worktree) 152 passed / 现在 152 passed**，零新增失败；
  `py_compile` 通过；`refresh_counts()` 实跑确认挂账项不再迁移 READY、状态分布如 §4。
- 回滚：`git revert` 本提交即可；状态字段为声明式，无迁移脚本。

## 7. 局限（必须随本 RFC 一起读）

1. 单次清账是**快照**：判据未过的项在更大样本上仍可能翻盘，本批只处理"已实测 + 判据明文允许结案"的情形。
2. `p1_1_capital_flow` / `da_prompt_distill_shadow` 的"退役"是**把注册表同步到既成决定**，
   不是本批新做的裁决。
3. `futu_channel_top_movers` / `futu_channel_iv_hv` 的挂账**不等于判死**：二者无直接实测，
   处置（建脚本 vs 退役）待用户裁。
4. 结构性缺口仍在：`futu_channel_top_movers` / `futu_channel_iv_hv` / `short_chase_ban_shadow`
   等活跃项**无 eval_script**；建议周检加机械规则——每项必须有 `eval_script` 或显式挂账原因，
   且"判据实测不过 → 当月结案，不许继续攒样"。