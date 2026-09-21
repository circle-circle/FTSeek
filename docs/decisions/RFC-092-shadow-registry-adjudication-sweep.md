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
| `vwap_gate_ab` | 批次 1 仅出均值/中位（判据要求按日聚类 MDE） | 见 §8.4 补齐后判定**不可分辨** |
| `futu_channel_ofi_baseline` | 产物仅给出 `rank_ic_incremental_vs_ofi=−0.1192`（capital_flow 相对 OFI 无增量）；**OFI 自身 IC 未被单独测得** | 缺独立读数 ⇒ **不构成 KILL 依据**（防"无证据裁决"） |
| `gap_chase_gate` | ~~0 样本（断供）~~ **本行诊断错误, 见 §8.3 更正**: 喂数方正常, 是注册表计数器对 `.db` 路径恒返 0 | ~~修采集~~ → **改计数锚** (不退役) |

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

---

## 8. 追记 (批次 2, 2026-09-21 同日晚) — 三项挂账的处置落实

用户对 §7.3 的三个待裁项下"按建议执行"。**三项全部建成可跑判据并实跑出读数**（无一估读），
其中两项同时修正了批次 1 的**仪器性误读**。

### 8.1 `futu_channel_iv_hv` — 建判据通路 + 读数

- 新增 `scripts/iv_hv_residual_eval.py`：落桶**逐字用已落地边界**
  `config/playbook_horizon.yaml short_horizon.vol_buckets`（atr_pct_max 0.015/0.035/0.08/999），
  `atr_pct = metadata.atr / entry_price`（零新采集线），结局 = `pnl_net`（实盘口径），
  统计 = 按 ET 日聚类 t + MDE(80%) + 单侧符号检验；n<30 只报数不结论。
- 读数（实跑）：实盘已结算 473 笔 join 上 317（67%）。
  `low` n=7（INSUFFICIENT_N）| `medium` n=41 HI +$4.66 / LO −$31.08 ⇒ pooled 差 +$35.74，
  但**日聚类 t=2.029 且 MDE±52.4 > 效应 ⇒ 不可分辨** | `high` n=269 差 +$4.68 t=−0.026 MDE±23.8
  ⇒ NO_RESIDUAL | `extreme` n=0。
- 处置：判据"桶内增量显著才毕业"**未达**（无桶达标）⇒ 维持观测；`data_path` 改指其产物
  （计数锚 n=317），`eval_script` 已接。

### 8.2 `futu_channel_top_movers` — 建判据通路 + 读数（⚠️ 结果反直觉）

- **bar 配对不可行**（实测 `replay_bars` 对 (mover, day) 覆盖率仅 **2%** = 87/5372：
  该目录只对"系统看过/交易过"的标的落盘，异动榜是全市场）。
- 改**自足口径**（`scripts/top_movers_eval.py`）：快照自带 `change_pct`(对昨收)
  ⇒ 入场 = 当日首次入榜观测、结局 = 同日末次观测、`fwd=(1+c_exit/100)/(1+c_entry/100)−1`。
- 读数（实跑）：14 个 ET 会话 3291 对；**逐日 IC 均值 −0.4435，t=−13.18，MDE±0.094（可分辨），
  14/14 天同号**；剔除 `change_pct` 全程未更新的冻结配对（11%）后仍 −0.422 / t=−10.74；
  时段切片 ≤10:00 t=−8.4、10:00–12:00 t=−17.1、>12:00 t=−6.0 全同向；
  榜内 rank 对照 top10 −0.19% vs 11–50 +0.52% vs 51–100 +0.73%（单调递减）。
- ⇒ 判据"IC 显著"**字面达标**，但**方向与"追涨幅榜发现候选"用途相反**（榜首续跌、榜尾续涨）。
  处置：**呈裁不自动毕业**（GRADUATE_CANDIDATE），选项 ①作追高排除/冷却信号
  ②反向用作回调入场候选池 ③并入既有追高族（同轴，勿新增硬门）——均需另立预注册。
- ⚠️ 已知选择偏差：只有仍在榜的标的被再次观测（出榜样本截断）；窗口仅 14 会话、单 regime。

### 8.3 `gap_chase_gate` — "0 样本断供"是**计数器假象**（批次 1 诊断更正）

- 批次 1 §5 最后一行写"0 样本（断供）⇒ 修采集而非退役"。**该诊断错误**：
  喂数方正常（415/864 笔含 `metadata_json.entry_gap.aligned_gap_pct`，单位即百分数，与门阈值 5.0 同轴）。
  真因：该注册项 `data_path` 指向 `.db`，而 `_count_samples` 对非 json/jsonl 路径返回 `None`
  ⇒ `current_n` 恒 0，被读成断供。
- 修复：`scripts/gap_chase_gate_eval.py` 落一份带 `n` 的产物
  `data/shadow/gap_chase_gate_latest.json`，注册表 `data_path` 改指它。
- 读数（实跑）：覆盖率 80%，拦截组 **n=31**（门 30 ✓）笔均 **−$16.6**（门 <0 ✓）
  vs 对照组 n=384 笔均 −$0.3；单侧符号检验 p=0.9252（门 ≤0.05 ✗）⇒ 判据 2/3，继续攒样。

### 8.4 顺带补齐：`vwap_gate_ab` 的按日聚类统计

判据原文要求"双臂净差 t-test + 按日聚类 MDE"，原 eval 只出均值/中位。补齐后（只读，复用脚本纯函数）：

| 臂 | n | 笔均 | 日聚类 day_mean | t | MDE(80%) | 判定 |
|---|---|---|---|---|---|---|
| 处理组（被拦 `wrong_side` 反事实） | 130 | +0.1765% | +0.1403% | 1.012 | ±0.3882 | **不可分辨** |
| 对照组（放行，已实现） | 359 | −0.0051% | +0.0237% | 0.304 | ±0.2178 | 不可分辨 |
| 双臂日聚类差 | — | +0.1816% | +0.1586% | 1.029 | ±0.4313 | 不可分辨（口径不同，仅方向参考） |

⇒ 原读数"门倾向拦错（被拦组 +0.176%）"在本标准下**不成立**（点估计为正但 MDE 是效应的 2.2 倍，
最大单日占比 6.2% 非单日驱动，drop-3-largest 后 +0.1553% 稳健）。维持观测，不改门。

### 8.5 本批验证与影响面

- 变更集：`app/playbook/shadow_registry.py`、`scripts/gap_chase_gate_eval.py`（改）、
  `scripts/iv_hv_residual_eval.py`、`scripts/top_movers_eval.py`（新）、
  本 RFC、`docs/CONTROL-LOOP-LEDGER.md`、`ops/hermes/ftseek_post_restart_observe.py`（补入库，见下）。
- 测试：受影响方位 10 个测试文件 **154 passed**（含 `test_control_loop_ledger`、
  `test_ops_scripts_sync`、`test_da_blocked_cf_eval`、`test_shadow_mount_sentinel`、
  `test_shadow_intent_uniqueness`、`test_gap_chase_gate` 等）。全量套件在本环境**本就大面积红**
  （如 `test_suggestion_engine` 5 errors，与本次改动零关联），且 HEAD worktree 因缺 ignored 文件
  无法 collect ⇒ 本批以**定向守卫集**为验证依据，不声称全量绿。
- 顺带修两处**本 agent 自身债务**（非新环路）：①重启后观察一次性 job
  `6753c4014e99`/`cd7bd81a1c89` 未当场登账 → 补入 §2.5 台账（`test_control_loop_ledger` 拦截提示）；
  ②其载体 `ftseek_post_restart_observe.py` 线上有、库内无 → `cp` 回 `ops/hermes/` 入库
  （`test_ops_scripts_sync` 拦截提示）。两者均为守卫按设计抓到，非新增环路。
- 零交易行为变更：未动 cron 载体、未改门/参数；三个待裁项均只新增**读数通路**与记账。

### 8.6 新增机制性发现（建议后续处理）

1. **注册表计数与判据不同轴会制造假故障**：`data_path` 指向非 json/jsonl（如 `.db`）时
   `_count_samples` 返回 `None` ⇒ `current_n` 恒 0 ⇒ 被读成"断供/0 样本"。
   建议：注册表启动自检"凡 `eval_script` 非空者必须能被计数"（否则当场报错），
   并普查既有条目是否还有同类（`ssr_shadow_daily` 备注已自述"注册表不计数, eval 现算"，属同类）。
2. **READY_TO_EVALUATE 仍为误名**：`refresh_counts` 只看 `current_n ≥ min_n`。本批已实测三项
   样本达标并**当场出读数**，但状态仍是 READY ⇒ 周检会重复通知"待裁"。
   建议加一个显式字段（如 `adjudicated_at` / `recheck_after`）区分"待裁"与"已读数待攒样"，
   使通知面等于真实待裁面——本次未实现，属下一批（避免越权扩大变更面）。
