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

---

## 9. 追记 (批次 3, 2026-09-21 深夜) — 选项① 被实测否决, 改判③ (同轴并入, 不新增)

用户裁"继续" ⇒ 按 §8.2 的处置建议 ①（追高排除/冷却信号）推进。但 ① 要能裁, 先得有**可量口径**：
异动榜是纯影子、**零消费者**, 没有"被它拦下的交易"。新增 `scripts/top_movers_trade_join.py`
把榜单与**已成交笔**对齐（与 iv_hv 同一套方法）：

- 对齐：入场时刻**之前最后一次**榜观测的 rank（快照 CST−12h→ET；`entry_time` 本身是 ET aware）；
- 分桶：rank≤10 / 11–50 / 51–100 / 入场时无榜观测；结局 = `pnl_net`（实盘口径）；
- 统计：按 ET 日聚类 + MDE(80%)；n<30 只报数。

### 9.1 读数（实跑, join 上 130/864 笔；榜窗 8/29–9/19 仅 18 会话）

| 分桶 | n | 笔均 | 累计 | WR | 日聚类 t | MDE(80%) |
|---|---|---|---|---|---|---|
| **rank≤10** | **11** | **+15.20** | +167.21 | 0.636 | 0.495 | ±101.16 |
| rank 11–50 | 7 | +18.46 | +129.23 | 0.714 | 0.850 | ±65.17 |
| rank 51–100 | 16 | −0.32 | −5.08 | 0.812 | −0.169 | ±107.17 |
| 入场时无榜观测 | 96 | −4.03 | −387.04 | 0.771 | −0.658 | ±25.45 |

**①的反事实不成立**：把 rank≤10 当冷却信号会躲掉 **+167.21 净盈利**（其中盈利笔 Σ+611.80、
亏损笔 Σ−444.59），日聚类差 +10.55 / t=0.263 / MDE±112 ⇒ 不可分辨，且 **n=11 < 30（§三.5 样本门未达）**。

### 9.2 关键结构发现：① ≡ kr3（同一批笔）

`top10` 的 11 笔中 **10 笔落在 kr3 `would_block` 面内**（kr3 覆盖内 11/11），
而 kr3 本身（RFC-072）的读数就是"LONG blocked 胜率 73%、均值受尾部支配 ⇒ 一刀切会误杀追涨多头"。
⇒ 落地 ① = 在同一批笔上再建一条同假设通道 ⇒ **重复且必然双转正打架**（同 2026-09-11 A_short_chase 事故）。

同时暴露一个**口径分叉**：榜级横截面 IC = **−0.44**（t=−13，榜首续跌）而**笔级 top10 = +15.2/笔**（正），
符号相反 ⇒ "追高吃亏"是**榜内成分股横截面**的性质, **不等于**"买榜首的实盘笔会亏"。
两者不可互相代入（榜级读数不能当交易信号证据）。

### 9.3 处置：做减法（③），并强制同轴声明

- **不新增门、不新增影子**；轴归 kr3。
- 注册表**强制同轴**：`futu_channel_top_movers` 与 `kr3_direction_runup_gate` 同标
  `intent="chase_high_ban"` 且**互指** `cross_ref` ⇒ `check_intent_uniqueness()` 实测 `[]`（零告警），
  二者不得独立转正（本项裁决路径并入 kr3）。
- 本项**特有待裁面**保留：榜内横截面 IC（盘中榜信息，kr3 不覆盖），继续观测。
- 新增 §9.1 口径脚本挂在 `notes`（`scripts/top_movers_trade_join.py`），产物
  `data/shadow/top_movers_trade_join_latest.json`。

### 9.4 撤回

**撤回 §8.2 中"我倾向 ①（最保守）"的建议** —— 实测表明 ① 既无正收益证据（n=11 不可分辨、反事实为负）
又与 kr3 重复。原推荐依据是"不新增硬门=保守"，但在**同轴**情形下"新增一条平行通道"本身即风险，
保守动作是**什么也不新增**（③）。

---

## 10. 追记 (批次 4, 2026-09-21) — 独立复查回执 + 待裁决触发通知落地

### 10.1 独立复查（另一会话对 §1–§9 的复核，逐条实跑，非读文档）

复核方式：不读结论读证据 —— 看 diff + 重跑 eval + 重跑守卫测试。

| 复核项 | 方法 | 结果 |
|---|---|---|
| 状态迁移落在正确条目 | `git diff a8e2c5001^..5e68a2537 -- shadow_registry.py` 按 hunk 定位 id | ✓ `p1_1_capital_flow` / `da_prompt_distill_shadow` / `pullback_limit_shadow` → KILLED；`overnight_kill_criterion` → SUPERSEDED |
| **无静默判据改写**（治理红线） | diff 中 `graduation_criteria=` / `min_n=` 变更计数 | ✓ 0 处；改动仅 `data_path` / `eval_script` / `notes` / `intent` / `cross_ref` + 新字段 |
| 挂账守卫真生效 | `refresh_counts()` 实跑 + `no_adjudication_reason` 出现面 | ✓ 仅 `post_exit_path` 一处（PERMANENT_MEASURE），不再机械迁 READY |
| 强制同轴机制 | `check_intent_uniqueness()` 实跑 | ✓ 返回 `[]` |
| `.db 计数假象` 诊断 | 读 `_count_samples` 实现 | ✓ 非 json/jsonl 路径确实 `return None` ⇒ 原"0 样本断供"诊断纠正成立（§8.3） |
| `gap_chase_gate` 读数 | 重跑 `scripts/gap_chase_gate_eval.py` | ✓ n=31 / 被拦 −16.6 / 对照 −0.3 / p=0.9252 / 覆盖 80% 逐字一致 |
| `futu_channel_top_movers` 读数 | 重跑 `scripts/top_movers_eval.py` | ✓ 3291 对 / 逐日 IC −0.4435 / t=−13.176 / MDE 0.0943 一致 |
| 榜单×笔 join | 重跑 `scripts/top_movers_trade_join.py` | ✓ §9.1 表逐字一致；kr3 交集 10/11 一致 |
| `futu_channel_iv_hv` 残存效应 | 重跑 `scripts/iv_hv_residual_eval.py` | ✓ 473 结算/317 join；medium +35.74 t=2.029 MDE 52.4；high +4.68 t=−0.026 ⇒ NO_RESIDUAL |
| 守卫测试 | 定向 6 文件 | ✓ 58–59 passed（本 agent 实跑） |

**两处偏差（均已确认不影响结论，但要写下来）**：

1. **§4 的状态分布已过期**：文档写 `READY 12 / OBSERVING 12`；实测现在 `READY 15 / OBSERVING 9`
   （KILLED 9 / GRADUATED 2 / SUPERSEDED 2）。原因 = 批次 2/3 给三项建了 `eval_script` 并把
   `data_path` 改指 json 产物 ⇒ 计数锚恢复 ⇒ 三项重新迁回 READY（§8.6-2 已预告该副作用，但 §4 表未同步）。
   **读 §4 必须配 §8.6-2 一起读**，否则会以为待裁面是 12。
2. **"退役"≠"停采"**：§6 自述未动 cron 载体，实测确认 —— `p1_1_capital_flow` 注册表已 KILLED，
   但其采集脚本 `scripts/futu_channel_shadow_collect.py` 仍由 `futu_channels.shadow_collect_enabled: true`
   开启（该开关同时覆盖已 KILLED 的 derivative_unusual）。⇒ **39MB/日原料仍在按交易日增长**，
   与"该裁不裁"的原始问题只解决了一半（裁决面已清，采集面未停）。停采需 `shadow_collect_enabled=false`
   + 重启（属配置/治理动作，**待用户裁**，本批不动）。

### 10.2 批次 4：把"触发人"补上（§8.6-2 的下一批）

§8.6-2 记的缺口是"READY_TO_EVALUATE 仍是误名 ⇒ 周检会重复通知待裁"。本批**不动注册表状态机**
（避免扩大变更面），改从**通知面**收敛：

- 新增 `scripts/pending_adjudication_report.py`：把 READY 面蒸馏成三行白话 ——
  ①**需要你决定**（人工分类 `NEEDS_USER`）②**判据已读完/自动续观** ③**等预注册窗口到期**；
  外加 ④条目落终态时的"结论已出"段与"未收录的新条目不得被吞"路径。
  输出仅在**待裁面签名变化**时产生（state `data/shadow/pending_adjudication_state.json`）；
  无变化 = 零 stdout。
- 新增 `ops/hermes/pending_adjudication_carrier.py`（Hermes no_agent 静默类契约：rc=0 空 stdout ⇒ 不推送；
  rc≠0 先插载体级异常行再以同 rc 退出；超时 rc2 / 子脚本缺失 rc3；取证落 `logs/pending_adjudication.log`），
  线上副本同 sha256，job `4378c93a89e6`，`15 9 * * 6`（周六 09:15 CST，紧随 09:05 周检），
  已按 §6.1 入账 `docs/CONTROL-LOOP-LEDGER.md`。
- 结论快照出处：本文件 §3/§5/§8/§9 的实测读数（本 agent 同日重跑复现 4 项，见 §10.1）。
- 实测：首跑输出全量 ✓ / 二跑 rc=0 且 **0 字节**（静默）✓ / 子脚本缺失 rc=3 ✓；
  `tests/test_pending_adjudication_report.py` 4 例钉住"静默契约 / 首跑全量 / 状态变化重抬 / `--dry` 不写 state"。
- 零交易行为变更：只读注册表 + 读产物 json + 写一份被 gitignore 的 state；未动门/参数/cron 载体。

**遗留（明写，避免被读成"已全清"）**：①停采 capital_flow 采集（§10.1-2，待裁）；
②`adjudicated_at`/`recheck_after` 字段级修法仍未做 —— 本批只治"人眼"这一面，
其他消费 `pending_adjudications()` 的读者仍会看到 15 项 READY。

### 10.3 批次 4b：把 §8.6-2 的字段级修法落到 SSOT（2026-09-21）

§10.2 只治了"人眼"这一面。本批补 SSOT：`READY_TO_EVALUATE`（= 样本量够）与"待裁"
（= 判据已出且需要人拍板）在代码里分开，不再靠通知脚本的人工分类。

- `app/playbook/shadow_registry.py` 新增**已读判据登记** `ADJUDICATED: dict[id, {read_at,
  recheck_after, needs_user, note}]` + 纯函数 `adjudication_state(item)`（返回
  `adjudicated / overdue / needs_user / read_at / recheck_after`）。15 项 READY 全部登记，
  出处 = 本文件 §3/§5/§8/§9 的实测读数（`read_at=2026-09-21`）；`recheck_after` 默认 +30 天
  （`2026-10-21`，纯重看闹钟），窗口类填窗口结束日（`reentry_quick_tp_gate` → `2026-09-30`），
  `futu_channel_top_movers` 为 `needs_user=True`（用途待裁，不设闹钟 = 立即待裁）。
- `app/review/criteria_status.py::pending_adjudications()` 每行新增
  `adjudicated_at` / `recheck_after` / `needs_adjudication`（= 样本量达标 ∧ 无挂账 ∧
  (从未读过 ∨ 待用户裁 ∨ 闹钟已到)）；**`ready` 字段语义与取值未动**（向后兼容既有消费方）。
- `scripts/pending_adjudication_report.py` 改为**以 SSOT 判定待裁面**（人工分类降级为旧 schema 的兜底），
  并在②段显示"重看 <日期>"；签名含 `needs_adjudication`/`recheck_after`
  ⇒ 闹钟到期当天会自动重新抬到①段。
- **纪律**（写进代码注释）：改判据 = 重新裁决；**不许靠改登记的日期把"待裁"改成"已裁"**，
  日期只是重看闹钟。登记不得出现幽灵 id（`tests/test_pending_adjudication_report.py` 新增守卫：
  `ADJUDICATED` 的键必须存在于 `REGISTRY`，缺 `read_at` 即红）。
- 实测：`pending_adjudications()` 现在 `READY(样本量达标)=15` 而 `真待裁=1`（仅
  `futu_channel_top_movers`）；所有 READY 项都已登记（无"未登记 = 静默吞掉"的漏项）。
  定向测试 4 文件 **48 passed**；通知脚本第 1 跑输出（签名升级触发一次）、第 2 跑 0 字节静默。
- 未动：`app/review/bundle_render.py` 仍按 `ready` 显示 READY/OBSERVING
  （它已可取 `needs_adjudication` 改显示，属另一处消费面，留给该面负责人）。

## 11. 追记 (2026-10-11): tier 影子去重 — conf-only A/B 走回溯, 不新增 emitter

> 触发呼应本 RFC 原始问题「现有门禁和预注册是否过于叠加 / 有没有已存在的门禁能力」。
> 用户 2026-10-11 Q2: 「预注册 tier 影子是否重复造影子 / 如何基于现有检查和推送」。
> 背景: DEB-20261009 §37 实测 signal_gate 真驱动 tier_score 零预测力 (Spearman −0.000)、
> decision_conf 弱但真 (+0.159) ⇒ 提议预注册「conf-only vs score+conf」tier A/B 验证 score 轴是否该退役。

### 11.1 判定: 新 live tier 影子 = 重复造, 应走回溯

- **现有能力已足够**: `data/da_gate_decisions.jsonl` 已逐候选记录 `score(0-10) + decision_conf + decision`,
  实测覆盖 **92% (1855/2017)、59 天 (2026-07-20~10-09)** ⇒ conf-only vs score+conf 的决策翻转 + 已成交结局差异
  可**纯回溯重算** (读现有日志 + trade_records), **零新 emitter、零新 jsonl、零新载体**。
- **减法先例 (本仓既有纪律)**: crontab `[2026-07-26 减法R2] atr_gate_shadow 产出零消费(45KB/日无人读), 停`
  ⇒ 造了无人消费的新影子必被停。tier live 影子若无人读, 同命; 回溯法复用既有消费面, 不增此风险。
- 与本 RFC §1 一致: 影子注册表是 record-only 事项 SSOT, 但**「能回溯既有日志就不新开 record-only 事项」**
  是清账批的同轴精神 (不叠加)。

### 11.2 预注册 (回溯分析器, 无阈值购物)

- **假设**: ① conf-only tier (score 轴退役) 会翻转多少决策? ② 翻转的决策与已成交结局 (pnl_net) 是否相关
  (验证 §37「score 零预测力 / conf 有预测力」)? ③ 翻转是否系统性放进/挡掉某类标的 (供应冲击)?
- **方法**: 回溯 replay — 对每条 da_gate_decisions 行, 用 conf-only 规则 (conf≥0.5→中, ≥0.6→良, ≥0.7→优)
  重判 tier, 与实际 (score+conf) tier 比对; 翻转集 join trade_records 看结局。
- **判据阈值**: **现在不设** (防阈值购物); 攒够回溯样本 + 出翻转分布后, 于裁决日按预注册程序定 (仿本 RFC §8-§10)。
- **产物归宿**: 写 `data/loops/` 或作为 meta_audit 源 (RFC-091 §15), **不新建影子 jsonl**。

### 11.3 推送/通知: 复用现有, 但接前必须活体验证

- 复用 **PushHub (`app/push`) + 钉钉 webhook** (`da_bridge_dingtalk_push.py`) + meta_audit digest, 不新建推送面。
- ⚠️ **已知断裂 (接前必须活体探针, 否则纸面告警)**:
  ① cron 自曝「不变量自检违反→推钉钉 自 7/28 是纸面的」(daily_invariant_check 写 `invariant_check.log`,
  告警链只读 `ftseek_us.log`); ② PushHub dedup 是进程内 60s 表、不跨进程 (实测 05:15 钉钉两条同文);
  ③ 冷却键全局小时级, 压制第二类错误。⇒ 分析器/台账 finding 接推送前, 先发测试 finding 验证真到钉钉。

### 11.4 边界

- **L1 只读**: 回溯分析器纯读 da_gate_decisions + trade_records (mode=ro), 不改 signal_gate、不翻 tier 逻辑、不新 emitter。
- 实装 conf-only tier (若回溯支持退役 score 轴) 是**另一独立预注册**, 须开跑会裁 + 影子攒样, 不在本追记范围。
- 关联: DEB-20261009 §37 (预测效度) / §39 (治理决策); RFC-091 §15 (台账接入); `scripts/da_gate_block_audit.py`。
