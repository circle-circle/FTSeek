# PROPOSAL-20260926 RFC-109⑤④-B: 终态影子停采提案 + early_dead 反事实证伪

**日期**: 2026-09-26
**状态**: 待裁 (本文档为提案 + 裁决材料, **不含任何已生效的改动**)
**前置**: RFC-109 ①②③④⑥ 已落地 (`7db95eeb1` / `0878a9f4c` / `964fa3b67` / `7914fb434` / `0db75d1e5`)
**数据源**: `data/early_dead_signal_shadow.jsonl`、`data/ftseek_us.db`、`crontab -l`、
`app/playbook/pipeline.py`、`app/playbook/exit_engine.py`、`app/playbook/playbook_service.py`
**权限层**: 两项均属 skill `ftseek-loop-engineering` 定义的 **L3 (改生产行为 / 删产出) ⇒ 只产提案, 人裁**

---

## A. 5 条终态影子停采提案 — 实测后建议**只停 1 条**, 且搭下次重启批

原审计清单认为「5 条终态 (KILLED/SUPERSEDED) 仍在每天写盘 ⇒ 都该停采」。逐条实测门控与
读方后, **该结论不成立**。三条实测发现推翻了它:

### A.0 三条前置实测 (决定性)

**① `collection_retired` 是死字段 —— 设 True 未必真停采**
全仓唯一读方是自家采集器 `scripts/futu_channel_shadow_collect.py:73`。其余 4 条的写方
(`pipeline.py` / `exit_engine.py` / 报表脚本) **完全不读它** ⇒ 在注册表设 True 只是账面标记,
采集照旧。这正是 skill「机制存在≠在跑」的镜像: **字段存在≠有执行力**。

**② 负载远小于预期 —— 是卫生问题, 不是性能问题**
5 条产物合计 **1.1MB / 782 行每周**:

| id | 产物 | 大小 | 总行 | 近7日增量 |
|---|---|---|---|---|
| p1_1_capital_flow | falsification_gate_shadow | 416K | 1594 | +412 |
| pullback_limit_shadow | pullback_limit_shadow | 436K | 1257 | +258 |
| p1_3_vol_p50 | vol_gate_da_p50_shadow | 172K | 624 | +118 |
| mfe_stall_exit | high_atr_stall_shadow | 92K | 300 | +34 |
| overnight_kill_criterion | overnight_arm_report_latest | 4K | 15 | +0 |

**③ 停采须改生产码 + 重启, 而重启前置不满足**
5 条的键全在 `playbook.*` ⇒ HotReloader 穿不透 ⇒ 必 `systemctl restart`。
实测当前 **3 笔未平仓** (`trade_records` 近7日 `exit_time` 空) ⇒ 按既有纪律
(重启前置 = 核未平仓) 不该为 1.1MB 负载动生产进程。

### A.1 逐条处置建议

| id | 停采通道 (实测) | 建议 | 理由 |
|---|---|---|---|
| **p1_1_capital_flow** | ✅ **有真 shadow-only 开关** `falsification_gate_capital_flow_shadow` (pipeline:6193 实读, record-only 不拦单); 产物**零读方** | **停** (搭下次重启批) | 唯一能干净停的一条 |
| p1_3_vol_p50 | ❌ 影子块 `pipeline:5757 if _vol_use_p50 is False:` 无独立开关 (该条件是**生产分流本身**) | **不停** | 须改生产码加键; `config_key=volume_gate_vwap_relative` 是生产量能门 (现 true) — 照它停=关生产闸 |
| mfe_stall_exit | ❌ 写方 `exit_engine:860` 无门控 (ATR≥4%∧peak<0.3%∧profit<0 分支内无条件写) | **不停** | 须改生产码; `config_key=mfe_stall_exit_enabled` 是生产退出腿 (现 false), 不门控影子写方 — 禁碰 |
| pullback_limit_shadow | ⚠️ **两个语义共用写方** — ①`pipeline:6276` 受 `pullback_limit_shadow_enabled` 门控; ②`playbook_service._append_routed_shadow` (DEB-20260901 选项1② archetype 路由遥测, **活跃且不受该开关门控**) | **不停** | 关开关只停一半, 且**误伤活跃路由遥测** = 次日重放按 `type` 取行的归因失效。真要停须先拆 type/拆文件 |
| overnight_kill_criterion | ⚠️ `data_path` 指的**不是采集产物** — 是 `scripts/overnight_arm_report.py` 的只读报表输出, 且**同时是注册表计数锚** (顶层 n=已平仓数, n≥20 翻 READY) 与 B15 判罪线可见面 | **不停** | 「停采」= 撤 cron `20 6 * * 2-6` = **断计数锚与判罪线**, 不是减负载而是断证据。近7日 +0 行 ⇒ 收益≈0 |

### A.2 陷阱划清 (原审计点名的两个生产键, 实测确认 + 一处修正)

- 🔴 `volume_gate_vwap_relative` (现 `true`) = **生产量能门**, 禁碰 — 原审计判断正确。
- 🔴 `mfe_stall_exit_enabled` (现 `false`) = **生产退出腿**, 不门控影子写方, 禁碰 — 原审计判断正确。
- ✅ **修正一处**: 原清单提示「p1_1_capital_flow 停采可能触发 fail-open」。**实测不适用** —
  `intraday_reentry.py:742` 的资金背离门读的是 `data/futu_capital_flow.jsonl` (分钟级原料),
  **不是** `falsification_gate_shadow.jsonl` (后者零读方)。那条 fail-open 陷阱属
  `futu_channel_capital_flow` (其原料喂生产门), 与本条无关 ⇒ **p1_1 可干净停**。

### A.3 建议动作 (需你裁)

**A 案 (建议)**: 只停 `p1_1_capital_flow` —— 把 `playbook.execution_parameters.falsification_gate_capital_flow_shadow`
从 `true` 改 `false` + 注册表 `collection_retired=True`, **搭下一次本来就要重启的维护批**一起做
(不为此单独重启)。数据保留不删, 落一行 INFO 留痕。其余 4 条维持现状, notes 已写明原因
(已提交, 防下一个 agent 重做同一轮分析)。

**B 案**: 5 条全停 ⇒ 需改 `pipeline.py` + `exit_engine.py` + `playbook_service.py` 三处生产码
加 shadow-only 键, 并先拆 `pullback_limit_shadow.jsonl` 的两个写方。**不建议**: 为 1.1MB
负载改三处生产路径 + 断一条计数锚, 收益/风险比不成立。

**C 案**: 全不停 ⇒ 维持现状 (零风险), 但 5 条终态继续写盘 = 卫生欠账留档。

---

## B. early_dead_signal_shadow 可裁决读数 —— **判据②③ PASS, 但反事实证伪接线价值**

### B.1 判据逐条 (n=389 已结算, 双窗)

判据原文 (预注册, 一字未动): ①双窗口死单率均≥55% ②命中组实际净PnL<0 ③差集结局为负

| 判据 | 15min (命中111) | 30min (命中86) | 结论 |
|---|---|---|---|
| ① 死单率≥55% (预注册词表) | 1/111 = **0.9%** | 0/86 = **0.0%** | **FAIL** (双窗) |
| ② 命中组净PnL<0 | −$3245.1 | −$2763.3 | **PASS** (双窗) |
| ③ 差集为负 | 73笔 −$2516.8 | 43笔 −$1986.2 | **PASS** (双窗) |

判据① FAIL 的**根因是词表漂移, 不是影子失效**: 预注册词表 `{TIME_EXIT, MFE_STALL_EXIT}`
在现役退出栈下基率仅 **1.3%** (8/11 预注册当时 30%) ⇒ 绝对线 55% 数学上不可达。
工程纪律已在 `engineering-change-protocol` 2026-09-23 实锤在案: **改判据 = 重新裁决, 须另立 DEB**。

### B.2 按判据的**预注册相对意图**重算 (报告 9/23 注释载明: 55% ≈ 当时基率 30% 的 1.8×)

三档现役词表 × 双窗, 补 Fisher exact (原报告只有 lift 点估计, n 仅 86 ⇒ 无法分辨真效应与噪声):

| 词表档 | 15min lift | 30min lift | 过相对线1.83× | 显著性 |
|---|---|---|---|---|
| 现役死信号 `{DEAD_SIGNAL_EXIT}` | 2.55× | 3.71× | ✅ 双窗 | p=5.3e-05 / 2.2e-09 *** |
| +耐心出场 | 2.54× | 3.71× | ✅ 双窗 | p=4.5e-06 / 7.2e-12 *** |
| +收口/跳空 | 2.24× | 3.08× | ✅ 双窗 | p=3.0e-06 / 1.2e-11 *** |

**三档全部双窗显著且过相对意图线** ⇒ 命中组确实「比基线更容易死」, 信号有信息量。
(30min 档「+收口/跳空」绝对率 57.0% 甚至过了 55% 绝对线, 但另一窗 41.4% 未过 ⇒ 双窗判据仍 FAIL。)

### B.3 🔴 反事实回测: **接线不该做** (原判据体系的盲区)

判据①②③ 只回答「命中组是不是死单 / 亏不亏钱」, **回答不了「提前出场该不该接线」** ——
因为早退本身要花钱 (会砍掉反弹单)。影子行自带 `entry_price` + `cur_pct` (检查点当时盈亏%),
按你 8/14 口径 (能历史重放就直接回测, 不默认前向攒样) 在已有数据上直接算:

口径: `net_cf = quantity×entry_price×cur_pct% − fees_est`; `Δ = net_cf − final_pnl_net` (>0 = 早退更好)

| 分类 (检查点→最终) | 30min n | 占比 | 合计Δ | 说明 |
|---|---|---|---|---|
| 亏→盈 (**早退砍反弹**) | 47 | **55%** | **−$2927** | 早退把后来涨回来的单砍了 |
| 亏→亏 (早退少亏) | 33 | 38% | +$2365 | 早退真能救的那批 |
| 盈→盈 (早退少赚) | 5 | 6% | −$37 | |
| 盈→亏 (早退该救) | 1 | 1% | +$6 | |
| **净额 Δ (门的真实期望)** | 86 | | **−$595** | **早退亏钱** |

15min 同构: 净额 **−$415** (亏→盈 61笔/56% −$3223 vs 亏→亏 40笔/37% +$3194)。

**决定性一点**: 「亏→盈」与「亏→亏」两类在**检查点时点不可区分** —— 两类的 `cur_pct`
都是小负值, 门看不到未来。所以不能只挑「早退该救」那 38% 接线; 一接线就必然连 55% 的
反弹单一起砍, 净期望为负。

### B.4 与既有生产腿的关系 (避免同轴重复)

命中组里 `dead_signal_60m_fired=False` 的差集 (15min 71笔 / 30min 43笔) 是「现有 60min
死信号退出腿抓不到的那批」= 唯一可能的新增量。实测其反事实 Δ: 15min **−$141**、
30min **+$178** (t=+0.21, 不显著) ⇒ **差集也没有可用的新增价值**。
而已被 60min 腿抓到的那组 Δ = −$274 / −$773 (t=−2.75) ⇒ 早退反而更差, 印证既有腿的时点选择合理。

### B.5 建议动作 (需你裁)

**A 案 (建议)**: **维持纯观测, 不接线, 不改判据词表**。理由: 信号有信息量 (B.2 三档双窗显著)
但**接线净期望为负** (B.3 −$415/−$595), 且差集也无增量 (B.4) ⇒ 按你 8/22 口径
(机制须救得回费用线, edge 必须 > 费用线) 不满足。注册表 `ADJUDICATED` 已记读数,
`recheck_after=2026-10-26`, 采集继续 (record-only, 零行为影响), 到点复算即可。
**零交易变更**。

**B 案**: 改判据词表为现役词表 (DEAD_SIGNAL_EXIT 族) 并重裁 ⇒ 须另立 DEB (改判据=重新裁决),
且**改完仍不能接线** (B.3 的反事实与词表选择无关: 无论用哪个词表定义「死」, 早退的净期望
都是负的) ⇒ **不建议**, 属为过线而过线。

**C 案**: 缩窄门 (只在 `cur_pct` 更负的极端子集接线) ⇒ 需**新预注册** + 新攒样, 且 B.3 显示
「亏→盈」与「亏→亏」在检查点不可区分, 没有可用的事前分层变量 ⇒ 现有数据不支持,
若要做须先证明存在能区分两类的**事前**特征 (如入场时的 ATR/regime), 那是新研究不是裁决。

---

## C. 本批已落地部分 (零交易行为变更, 已提交)

1. `scripts/early_dead_signal_report.py` (eval_script) 补两块**只印不判**读数:
   - Fisher exact 显著性 + 判据①的**预注册相对意图线 1.83×** (原只有 lift 点估计);
   - 反事实四类分层 + 净额 Δ (原判据体系完全缺这一面 ⇒ B.3 的盲区)。
   判决口径 (绝对线 55%) **一字未动**; 不写回任何字段 (`is_dead` 仍按预注册词表落库)。
2. 注册表 5 条终态的停采盘点写进 notes (A.1/A.2 的实测结论), `status` / `graduation_criteria` /
   `min_n` / `config` / cron **全部未动**, `collection_retired` 仍为 False。
3. 量纲坑入档 (报告 docstring): `cur_pct` 是**百分点** (0.1=0.1%) 而 `trade_records.pnl_pct`
   是**小数** (0.01=1%), 且 `pnl_pct` 是毛 / `pnl_net` 是净 ⇒ 比符号前必须 ×100 换算,
   否则阈值一设就全被短路, 会报「翻转 0 笔」的**假绿** (本人本轮实测踩过一次)。

**回滚**: `git revert` 对应提交; 提案部分未产生任何改动, 无需回滚。
