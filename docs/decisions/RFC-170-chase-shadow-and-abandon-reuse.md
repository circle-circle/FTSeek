---
title: "RFC-170: 确认侧追价影子 + 弃单再生通道回放裁决 (P3 不建通道)"
status: implemented
module: playbook
related_code:
  - app/playbook/entry_confirm_gate.py
  - scripts/entry_second_chance_replay.py
  - scripts/entry_confirm_abandon_reuse_replay.py
  - tests/test_rfc170_chase_shadow.py
related_okr: OKR-2026-11
related_rfc: RFC-141 (entry_confirm), RFC-153 (FADE_SHORT 追价帽), RFC-066 (多轮指令台账)
tags: [entry-confirm, shadow, record-only, replay, reentry, pre-registered]
updated: 2026-10-26
---

# RFC-170: 确认侧追价影子 + 弃单再生通道回放裁决

> **状态**: 实施（用户 2026-10-26「按照性价比建议执行，弃单再生通道请重点分析方案」）
> **性质**: ①纯记录影子（零交易行为变更）②只读回放 + 预注册裁决 ③通道方案给出结论=不建。
> **日期**: 2026-10-26

## 一、动机：门在哪一半失效

独立复算 `data/shadow/entry_confirm_forward.jsonl`（`type=entry_confirm`，n=120 决策 / 63 弃单 / 57 确认）
与已结算确认单（n=53，join `trade_records.metadata.playbook_instruction_id`）：

| 观测 | 读数 |
|---|---|
| 弃单后 +5m 方向化收益（相对弃单价） | n=28，均 **−0.522%**，中位 −0.641%，**82% 继续逆向** |
| 弃单后 60m 同向净 drift（单日 bar，扣 10bps） | n=53，均 **−0.558%**，**t=−2.67**（30m 腿 −0.513%，t=−3.22） |
| 确认单追价 opp（正=入价比 arm 锚差） | 均 **+0.350%** / 中 +0.299%；mid-mid 尺 n=25 均 +0.365%（100% 为正） |
| 确认单里的 SL_INITIAL（n=3） | opp **+0.413% / +1.068% / +1.412%**（净 −141 / −149 / −148）—— **全在追价前三名** |
| Spearman(追价, 实现净额) | **−0.264，p=0.056** |

**结论**：弃单侧（filter）是有效的；失效在 pass 侧 —— 确认的必要条件就是"价格已经顺向走掉"，
于是系统性地在最伸展处进场，把它本该消灭的 SL_INITIAL 族从 pass 侧放了回来。
这也正是预注册判据 c3（`CONFIRMED opp_cost < 0.3%`）当前不达标的原因（0.3565%）。

## 二、本批执行的两件事

### A. 确认侧追价影子（record-only，实施）

`app/playbook/entry_confirm_gate.py`：
- 新常量 `CHASE_SHADOW_PCT = 0.5`（与 `opp_cost_pct` 同尺，**不新增 YAML 键** —— 避免
  `playbook.*` 热载盲区 + 配置漂移）。
- 新纯函数 `chase_shadow_flag(opp, thr)`（脏输入返回 False，影子异常不得影响交易）。
- `_judge_one` 里 `_chase_shadow = bool(still and chase_shadow_flag(opp))`；
  CONFIRMED 时打一行 `[EntryConfirm·追价影子] … would_abandon (record-only, 不拦)`；
  forward 行新增 `chase_shadow_pct` / `chase_shadow_would_abandon` 两字段。

**零行为变更的证据**：`tests/test_rfc170_chase_shadow.py` 断言 `_chase_shadow` 不出现在
判定块（`_fs_cap_hit = bool(...)` 到 `_PENDING.pop(...)` 之间），判定入口仍是
FADE_SHORT cap + grace 两条。阈值 0.5% 来自上面第 8 行分桶（>0.6% 桶净均 −27.84，且
3 笔 SL_INITIAL 全落在 ≥0.41%），**只作记录、不作判决**。

### B. 弃单再生通道：回放裁决（结论=不建）

#### B1 同向再生（延迟后按市价 / 回退限价再挂）

工具：`scripts/entry_second_chance_replay.py`（2026-10-18 已有，本批**修口径 bug**）。

⚠️ 口径 bug（本批修复）：bar 加载只按 symbol 找多日档（`US_<SYM>.json`），多日档止于某个历史日
⇒ 今天的弃单会被配成 3 天前的 bar；原 2% 校准拦不住（价格恰好接近）。
修复=单日档（`data/replay_bars/US_<SYM>_<date>.json`）优先 + **bar 日期必须等于弃单日**，
否则标 `stale_bars` 排除。修后 cohort：abandoned 64 → usable **53**（stale 7 / no_bars 4 / caliber 0）。

| 臂 | n | 平均净 | 胜率 |
|---|---|---|---|
| 对照（无视门，弃单价立即入场，60m） | 53 | **−0.4658%** | 45.3% |
| 延迟 3/5/10/15min × 回退 0.3/0.5% 或 ATR0.2/0.3 限价 | 4-34 | 全负（最好 −0.1398%） | 25-50% |
| 唯一正读数：ATR0.3 × 15min | 11 | +0.0556% | 45.5% |
| 唯一正读数：ATR0.2 × 15min | 14 | +0.0178% | 50.0% |

⇒ **同向再生在 16 个组合里 14 个为负，唯二微正 n≤14 且量级 ≈0（< 费用线）**。

#### B2 反向 / 分层（是否藏着"真正的机会"）

工具：`scripts/entry_confirm_abandon_reuse_replay.py`（本批新建，预注册见脚本头）。

| 预注册判据（读数前写死） | 结果 |
|---|---|
| P1 反向可用：60m 净 ≥ +0.30%/笔 ∧ 顺向 ≥55% ∧ n≥40 | **不成立**（+0.358%，t=1.72，顺向 50.9%） |
| P2 可分层：某子集（n≥15）同向净 ≥ +0.15% 且优于补集 ≥0.50pp | **不存在**（全部 6 个维度×桶的同向净均为负） |
| P3 否则：弃单=纯负信号，只作 veto/质量特征，不建通道 | **成立** |

分层明细（60m 同向净）：逆向幅度 <0.15% −0.148(n=17) / 0.15-0.5% −0.585(n=25) / ≥0.5% **−1.132(n=11，顺向 18%)**；
archetype：FADE_SHORT −0.737(n=18) / PULLBACK_LIMIT −0.364(n=34)；session：**开盘窗<15m −0.639(n=45，t=−2.65)**
vs 之后 −0.102(n=8)。→ 门的判别力**只存在于开盘窗**，之后基本为 0（这也解释了 c2 为负：开盘窗整体就是差的，
门选得对也救不回那个窗口）。

反向（30m 腿）+0.313%/笔、顺向 64.2%、t=1.96（n=53）—— 这是**唯一**触到 P1 量级的读数，但它
①违反 60m 预注册腿 ②完全丢弃 DA 的分析结论、只借用"这个标的刚被打脸"的事件 ⇒ 属**新动量策略**，
不属"复用分析"，且与 playbook 的 fade/pullback 家族相反。**不建议现在建**；若要做，另立预注册影子。

#### B3 为什么"不建通道"不是"放弃机会"（供给侧口径）

2026-09-17 用户裁决关闭 `intraday_reentry` 时已量化：全期 26 笔净 −$349.7（剔 SNDK 隔夜缺口后
−$5.67/笔 = −10.3bps，低于费用线 7.7bps）；供给 0.7 笔/天；且 **DA/UE 每日生成 71-80 条指令
远超执行 4-13 笔，瓶颈是资金帽不是标的数**。⇒ 再开一条再入场通道不会增加总敞口，只会
在同一预算里**换掉**别的交易；而本批回放说这些被换进来的交易净 drift 为负（−0.46%）。

另已验证并**证伪**的一条候选：弃单率当日度 regime 温度计（弃单率 vs 当日净额
Spearman −0.159 p=0.64，n=11 天）—— 无信息，不采用。

## 三、不做什么（显式）

- 不动判定：`0.3%` c3 天花板、grace 2/3、方向判式、`entry_confirm_max_wait_s` 全部不动（预注册观察期）。
- 不新建交易通道：同向再生（回放 14/16 为负）与反向（P1 不成立）都不建。
- 不复活 `intraday_reentry`（9/17 已裁关闭，理由见 B3）。
- 不改 `open_positions.current_sl` 镜像（REAL 臂 broker 止损参考值，属行为变更，需另裁）。

## 四、验证

- `tests/test_rfc170_chase_shadow.py`（4 条）+ `tests/test_rfc169_reconcile_linkage.py`（3 条）
  + 存量 `test_entry_confirm_gate / test_entry_confirm_live_wiring / settle_ue_confirm_gate` 79 条 → 全绿。
- 活体验证：影子字段要等**下一次 CONFIRMED** 才出现（TOO_EARLY 不算失败）；届时
  `grep 'EntryConfirm·追价影子' logs/ftseek_us.log` 与 `data/shadow/entry_confirm_forward.jsonl`
  的 `chase_shadow_would_abandon` 应同步出现。
- 复算入口（只读）：`scripts/entry_second_chance_replay.py` / `scripts/entry_confirm_abandon_reuse_replay.py`，
  报告落 `data/reports/`。

## 五、回滚

`git revert <commit>` + `systemctl restart ftseek-us`（影子字段只增不改，回滚后旧行照常被结算器忽略）。