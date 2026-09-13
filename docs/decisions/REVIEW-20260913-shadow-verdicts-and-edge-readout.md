# REVIEW-20260913 — 5 个 READY_TO_EVALUATE 影子判据实测 + 周检经济边际读数

**日期**: 2026-09-13
**触发**: 用户裁「全部处理」(承接 9/12 周检 + 9/13 策略调研批)
**范围**: L4/L5 (纯测量 + 文档留痕 + cron 补挂), **零交易行为变更**
**关联**: `STRATEGY-AUDIT-20260913-internal.md` / `STRATEGY-AUDIT-20260913-external-crossval.md` / E2 退役 (`d635fc7d`)

---

## 〇、本文档最重要的部分: 纠正一个我自己的误读

9/13 我向用户建议「**裁 4 个已达标影子** (kr3 / extreme_gap_ban / penetration / exit_disable_cf),
队列 20→16」。**这个建议是错的**, 错因是概念混淆:

| 概念 | 实际含义 | 我的误读 |
|---|---|---|
| `ShadowStatus.READY_TO_EVALUATE` | `refresh_counts()` 里**纯样本量**判定: `current_n >= min_n` 即自动迁移 | 当成「判据通过, 达标待裁」 |
| 🔔 周检横幅「待用户裁决」 | 提示**样本够了可以跑判据**, 不是判据过了 | 当成「已达标」 |
| `graduation_criteria` | 各影子**独立的统计判据** (n 子集/t 值/误杀率/precision…) | 未逐条实测就下了结论 |

**实测结果: 5 个影子逐个跑 eval_script, 0 个判据过门。** 若按我建议执行「裁决」,
会在样本量达标但统计判据不成立的情况下强行结案 — 正是 OKR-2026-09 KR3 明令禁止的
链式 fishing, 也违反 `backtest-rule-replay` 的四关门纪律。

### 根因 (为什么这个误读容易发生)

注册表 `refresh_counts()` 的 🔔 文案原文是「样本已达 N/M → READY_TO_EVALUATE, **待用户裁决**」,
措辞把「样本量达标」和「可以裁决」压在一句话里, 读者 (包括我) 会跳过中间那步「跑判据」。
`kr3` 与 `extreme_gap_ban` 的 `notes` 里其实都写了「READY≠毕业」, 但**只有 notes 写了,
🔔 通知本身没写** — 而周检推送出来的正是 🔔 通知, notes 不在推送面。

### 源头修复 (做减法, 不打补丁)

改 `refresh_counts()` 的 🔔 文案 (`app/playbook/shadow_registry.py`), 把判据实测前置为
**通知里的显式动作要求**:

```
🔔 影子 [X] … 样本量已达 N/M → READY_TO_EVALUATE。⚠️ 仅**样本量**达标, 不代表判据通过 —
裁决前必须跑 eval_script 实测判据 (READY≠毕业, 2026-09-13 实锤: 5 个 READY 影子实测判据 0 个过门)。判据: …
```

`refresh_counts()` 只在周检 subprocess 内调用 (不在服务进程) ⇒ **改文案无需重启服务**。

---

## 一、5 个影子判据实测 (2026-09-13, 全部 KEEP_SAMPLING / NO_ADJUDICATION)

### 1. `kr3_direction_runup_gate` — 方向×涨幅证伪门 (RFC-072, 9 月唯一预注册假设)

判据 (RFC-072 §三 冻结): would-block 子集 **n≥30** + 日聚类 t>2 + MDE 覆盖 + OOS 同向

| 组 | n | mean | median | WR | tail_share |
|---|---|---|---|---|---|
| LONG\|blocked | **21** ✗ | +4.27 | +15.12 | 71% | 416% |
| LONG\|pass | 33 | +15.44 | +14.11 | 85% | 37% |
| SHORT\|blocked | **12** ✗ | −22.29 | +7.21 | 67% | 88% |
| SHORT\|pass | 8 | +59.53 | +18.72 | 100% | 3% |

**判定: NO_ADJUDICATION** — 两腿 would-block 子集均 n<30, §三.5 明令「不裁决, 延长观察」。

⚠️ **9/4 → 9/13 的关键变化**: LONG 腿由 `DIRECTIONAL` 转为 **`TAIL_ONLY`**
(n 15→21 后中位数由劣势翻为 +15.12 ≈ pass 组 +14.11, 均值劣势全靠 worst2=−373 两笔尾部拖累,
tail_share 416%)。这是「早读数是尾部噪声」的实证 — **样本门不可绕过**, 若 9/4 就裁会把
一个实际是「尾部风险」而非「方向错」的现象误做成入场门, 而入场门对尾部无效 (全拦会误杀 71% 胜率的多头)。

### 2. `extreme_gap_ban_shadow` (D1') — 极端跳空禁入

判据 (DEB-20260909 §5.2 逐字四关): 每腿 n≥30 ∧ 被拦净<0 ∧ 误杀率≤40% ∧ 两腿同向

| 腿 | n | 被拦净额$ | 误杀率 | 逐关 |
|---|---|---|---|---|
| ag≤−3.0 或 ≥+4.0 | 58 ✓ | −2029.06 ✓ | **50%** ✗ | n✓ 净✓ 误杀✗ |
| ag≤−3.5 或 ≥+4.0 | 52 ✓ | −2010.29 ✓ | **50%** ✗ | n✓ 净✓ 误杀✗ |

两腿同向 ✓。**判定: 四关三过, 仅误杀率一关不过 ⇒ 未达毕业线, 维持纯观测。**

分源误杀率: DA 48% / UE 56% / REENTRY 43% — **三源全部 >40%**, 非单源拖累,
不存在「只禁某源」的子集捷径。滚动 20 日 n=47 净−2035.75 与全历史几乎同值 ⇒ 出血集中近期,
但误杀率未随 regime 改善。

⚠️ **纠正我 9/13 早先的另一个数字**: 我曾说 D1' 「误杀率 76-87%、覆盖率仅 5%/14%」。
那是按 **mfe<0.05% 死单子集**算的另一口径, 与本影子注册判据 (被拦笔中盈利占比 = 50%) 不可混用。
两个数字都真, 但**只有注册判据口径的 50% 能用于裁决** — 已在 verdict_history 留痕标注。

### 3. `penetration_shadow` — 保本线穿透判别器

判据 (B17): n_labeled≥20 ∧ 某假设 matched≥8 ∧ precision≥0.7

- `n_labeled = 42` ✓
- **`hypotheses_scored = []`** — LLM 出题环零可判分假设 ⇒ matched/precision **无实测值**
- 基线: overall `v_rate_5m = 0.976` (active 1.000 / passive 0.750)

**判定: KEEP_SAMPLING** — 几乎全部穿透事件都 V 回 (97.6%), 判别器要分出的是「快跌不回」少数族,
当前无假设能分。

⚠️ **陷阱留痕**: `penetration_analysis_latest.json` 里的
`graduation_gate = {n: 20, matched: 8, precision: 0.7}` 是脚本 `GRAD_*` **常量回显**,
不是实测结果 (源码 `penetration_llm_analysis.py:265`)。读 JSON 判达标会误判 — 必须看
`hypotheses_scored` 是否非空。

### 4. `exit_disable_cf` (M2) — 禁用型退出规则反事实

判据: 每规则 n≥50 ∧ saved 中位>0.15% (费用线) ∧ 持有更优率≥65%

| 规则 | n | saved 中位 | 持有更优率 | 逐关 |
|---|---|---|---|---|
| MFE_STALL_EXIT | **31** ✗ | +0.331% ✓ | **61.3%** ✗ | 两关不过 |
| DEAD_SIGNAL_EXIT | **30** ✗ | +0.213% ✓ | 80.0% ✓ | 仅 n 不够 |

**判定: KEEP_SAMPLING** — 两规则均 n<50。

⚠️ **前瞻提示**: MFE_STALL_EXIT 已于 8/15 实盘禁用 (DEB-20260815), 本影子对它是**事后验证**。
其持有更优率 61.3% **低于 65% 门限** — 攒够 n 后若仍 <65%, 应如实记录
「禁用决策的反事实支持偏弱」, 不得因规则已禁用就停止判分 (确认偏误风险)。

### 5. `intraday_loss_breaker_a_prime` — GLM A' 日内累计亏损熔断

判据 (B18): 被挡笔期望 ≤ −10 ∧ n≥30

- `n = 194` ✓
- 被挡笔期望 = **+46.83** ✗ (被挡单净 **+$9,084**, 132 亏 / 62 盈)

**判定: KEEP_SAMPLING_FORWARD_ONLY** — 但这个读数**不可作裁决依据**:

| 分层 | 触发日 | 被挡 settled 笔 |
|---|---|---|
| backfill (<9/5 创建日) | 24 | **191** |
| forward (≥9/5) | 1 (9/8) | **3** |

注册表 notes 已明写 backfill 有前视偏差 (entry 序累加) ⇒ 判不毕业, **价值全在 forward 攒样**。
而 forward 仅 3 笔 ⇒ 期望符号无统计意义。

🔴 **根因 (真故障, 已修)**: `eval_script` 自 9/5 创建起**从未挂 cron** ⇒
注册表 `current_n` 读 `cb_shadow_latest.json`, 该文件不跑脚本就永不更新 ⇒
forward 样本永远卡在 3 笔 ⇒ **影子等于停摆**, 同时周检持续报悬空告警 (B1 同型病灶)。

**处置**: 补挂 `28 6 * * 6` (周六班, 09:05 周检前数据已新; 全量重建幂等 ⇒ 周跑无增量丢失风险)
+ 台账 `crontab-cb-shadow-breaker` 行入账 (守卫测试 `test_crontab_loops_are_ledgered_or_baselined`
当场抓出漏登, 补齐后 29 守卫全绿) + 悬空告警消除 (`check_mounting()` 实测返回 `[]`)。

---

## 二、周检新增: 经济边际读数 (毛 edge vs 费率)

### 立起理由

9/13 策略调研的核心发现 (`STRATEGY-AUDIT-20260913-external-crossval.md` §十):
7-8 月亏损的真实机制是**双向挤压** — 毛 edge 掉一半 + 费率翻倍 (名义变小 ⇒ 固定佣金占比升高);
9 月已修复但**余量仅 +3.64bps**。这是系统生死线, 而此前周检**无任何读数盯它**。

### 口径 (纯测量, 只读 DB, 零行为变更)

```
notional   = entry_price × quantity        (派生量 — trade_records 无 notional 列)
毛edge_bps = sum(realized_pnl)/sum(notional) × 1e4
费率_bps   = sum(fees_est)/sum(notional) × 1e4
净_bps     = 毛 − 费
分桶       = trade_date (ET 日, RFC-070 公共尺子)
```

**公式已验证逐位复现** `STRATEGY-AUDIT-20260913` §十 表 (821 笔全期 / 470 笔 7月来 / 9月 79 笔):

| 期间 | n | 毛 edge | 费率 | 净 |
|---|---|---|---|---|
| 全期 | 821 | 14.47 bps | 5.46 bps | **+9.01 bps (+$13,106)** |
| 2026-07 | 184 | 0.12 bps | 12.22 bps | −12.10 bps |
| 2026-08 | 207 | 7.33 bps | 9.67 bps | −2.34 bps |
| **2026-09** | 79 | **13.10 bps** | 9.46 bps | **+3.64 bps (+$150)** |
| 7月以来 | 470 | 6.19 bps | 10.43 bps | −4.24 bps (−$891) |

### 实跑输出 (2026-09-13 验证)

```
经济边际(本月2026-09): n=79 毛edge +13.10bps | 费率 9.46bps | 净 +3.64bps
  滚动3月: n=470 毛edge +6.19bps | 费率 10.43bps | 净 -4.24bps
  余量 +3.64bps (净edge<0 即系统亏损; 9月修复后 +3.64bps)
```

告警条件: 本月 `net_bps < 0` → `⚠️ 告警: 本月净edge X bps < 0 — 毛edge 未覆盖费用线`。
**无阈值参数**, 不引入新的可调旋钮 (符合「做减法」纪律)。

---

## 三、cron 投递故障修复 (Feishu 99992402)

### 现象与根因

`deliver="origin"` 类任务 **4/4 次投递全部失败** (`[99992402] field validation failed`),
而显式 `feishu:oc_...` 的任务 **97/97 全部成功** (deliveries.db 实测)。
根因: `origin` 解析出的目标带失效 thread_id (`:omt_xxx`), 且 99992402 不在 fallback 码集合内 ⇒ 不回退主会话。
(见 `hermes-gateway-troubleshooting` skill §4a — 精确命中该故障模式。)

### 受影响任务 (全部已改为显式 `feishu:oc_a093be36f56463ad424bfb1c4b437229`)

| job_id | 名称 | 原状态 | 影响 |
|---|---|---|---|
| `d630ce54fc84` | 周一开盘首小时双验证 (退出链+RFC-082) | delivery_failed | 🔴 **周一验证结果会送不到** |
| `dac32b02d402` | D2' 入场确认门影子日报 | delivery_failed | 🔴 已静默丢报 2 天 (9/11, 9/12) |
| `7d448f79c410` | SPY 暴跌预注册观察器 | ok (未触发过推送) | 预防性修复 |
| `1e96c03c945e` | 9/22 UE thinking on/off 攒样裁决提醒 | 未到期 | 预防性修复 |

**实弹验证**: 手动触发 `dac32b02d402` → deliveries.db 新增记录 `delivered` (18:03:10), 修复前 4/4 全失败。

⚠️ **D2' 日报自带「结算陈旧 29h」警告 = 误报**: 陈旧阈值是**墙钟 26h**, 按工作日节奏
(settler 05:58 → push 06:10, 间隔 12 分钟) 校准; 周日不在调度面 (`2-6`), 手动触发才跨过周末墙钟。
settler 周六正常跑过 (jsonl mtime 9/12 05:58, 数据覆盖至 9/11 末交易日) ⇒ **传感器健康, 阈值不该改**
(它对真故障灵敏, 只在非调度日手动触发时假阳)。

---

## 四、本批不做的事 (与理由)

| 项 | 不做理由 |
|---|---|
| 裁决/退役任何影子 | **0 个判据过门** (§一), 无裁决前提 |
| 改 `min_n` 或判据阈值 | 预注册冻结 (RFC-072 §三 / DEB-20260909 §5.2 / B17 / B18), 改阈值=重新裁决 |
| 新增任何入场门 | 9/13 调研已证伪: 20 个候选入场门全部不合格 (死单入场时刻**不可分**, 见 external-crossval §六) |
| 把 loss_breaker 判为证伪 | backfill 前视偏差 + forward 仅 3 笔 ⇒ 符号无统计意义, 只能补挂攒样 |
| 改 D2' 陈旧阈值 | 误报源于非调度日手动触发, 阈值对真故障灵敏 (§三) |
| 动退出链参数 | 参数冻结窗口 (KR2), 且 9/13 16:17 另一 agent 已提交退出链整批 (`3957d85d`) |

---

## 五、验证清单

| 项 | 结果 |
|---|---|
| `refresh_counts()` + `check_mounting()` + `check_intent_uniqueness()` | ✓ 悬空告警消除 (`[]`), 意图告警 `[]` |
| 5 影子 `verdict_history` / `last_evaluated` 写入 | ✓ 实测回读 (vh=1/1/1/2/1) |
| 守卫测试 `test_control_loop_ledger` + `test_ops_scripts_sync` + `test_shadow_intent_uniqueness` | ✓ 29 passed |
| 相关影子测试 (gap_chase/da_blocked_cf/penetration/vwap_gate_ab/settle_ue_confirm/phase1) | ✓ 100 passed |
| 周检脚本实跑渲染 | ✓ 经济边际段正确输出, bps 逐位复现体检表 |
| ops 副本同步 (`/root/.hermes/scripts/`) | ✓ sha256 一致 (`a25a5594…`) |
| crontab 补挂零删除行 | ✓ `diff` 无 `<` 行 |
| cron 投递实弹 | ✓ `delivered` |
| **服务重启** | **不需要** — 改动面 = ops 周检脚本 (subprocess 独立进程) + 注册表文案 (仅周检 subprocess 调用) + crontab + 文档; 无服务进程内行为变更 |

---

## 六、回滚

| 改动 | 回滚方式 |
|---|---|
| 周检经济边际段 | `git revert` 本批 ops 提交 (纯新增函数 + 渲染段, 无既有逻辑改动) |
| 🔔 文案 | `git revert` (单字符串) |
| loss_breaker cron | `crontab -e` 删 `28 6 * * 6` 行 (record-only, 无行为变更) |
| cron deliver 目标 | `cronjob_manage update deliver=origin` (但会重现 99992402) |
| verdict_history 留痕 | `git revert` (纯数据留痕) |

---

## 七、教训 (已沉淀)

1. **`READY_TO_EVALUATE` 是样本量状态, 不是判据结论。** 裁决前必须跑 `eval_script` 实测 —
   本轮 5/5 全部不过门, 若按「达标」处理会制造 5 个错误裁决。已改 🔔 文案做源头预防。
2. **影子 JSON 里的 `graduation_gate` 可能是常量回显** (penetration 实例) —
   判达标要看**实测字段** (`hypotheses_scored` 非空), 不是看有没有 gate 结构。
3. **注册表 `current_n` 读的是产物文件, 不跑 eval_script 就永不更新** ⇒
   「影子在攒样」可能只是假象 (loss_breaker 停摆 8 天, forward 卡 3 笔)。
   新建影子必须**同批挂 cron + 同批入台账**, 守卫测试会抓漏登。
4. **不同口径的误杀率不可混用** — D1' 注册判据口径 50% vs 死单子集口径 76-87%,
   只有前者能用于裁决。已在 verdict_history 双向标注。
5. **早期读数是尾部噪声的风险真实存在** — kr3 LONG 腿 n 15→21 后由 DIRECTIONAL 转 TAIL_ONLY,
   印证 RFC-072 §三.5 样本门 (子集 n<30 不裁决) 的必要性。
