---
title: 门误杀台账 (跨门案例累计 + 门级基率)
status: active
module: governance
related_code: scripts/settle_shadow_portfolio.py, scripts/gate_block_counterfactual.py, scripts/settle_entry_confirm_forward.py, scripts/cf_pullback_limit_replay.py
related_okr: OKR-2026-10
tags: [gate, miskill, ledger, governance]
---

# 门误杀台账 — 跨门案例累计 + 门级基率

> 缘起: 用户 2026-10-16 令「误杀写入, 累计此类问题, 后续做共性分析提升调整」。
> 定位: 本文件是**案例流水 + 门级基率的索引**, **不是新账本** —— 逐笔真值仍在 §2 的三个既有面
> (jsonl / DB), 本文只登记「谁在何时被哪道门拦、事后反事实是多少、判读是什么」。
> 纪律: 单日/单案例**只登记不调阈值** (误杀预算纪律, 见 §1)。

## 1. 口径 (先读)

- **误杀 (miskill)** = 被门拦下的单, 事后反事实为**盈利**。比例口径 = 误杀笔数 ÷ 被拦笔数。
- 两个已冻结的门限 (不可在本台账里放宽):
  - ①D 案裁决卡: 误杀率 ≤ **0.40** (expired 口径, 分母 RFC-081 §六 冻结)
  - ②B19 `eod_depth_gate`: `miskill ≤ 1` **绝对计数** (已满 ⇒ 任何新案例都不得当放宽理由)
- **两种量化口径不可混用**:

| 口径 | 定义 | 用在哪 |
|---|---|---|
| 影子结算净额 | 影子按**生产退出栈**重放被拦单 → 净额 > 0 即误杀 | `shadow_portfolio_outcomes` / D 案重放 |
| 收盘盯市 | 被拦价 → 当日收盘 (不含退出栈) | 本台账 §4 快速登记, **粗于上者** |

- **禁止**: 拿案例反推阈值; 拿轮询级**事件数**当样本 (实测放大 **46.7×**, 24853 事件 = 532 指令);
  用「影子组合」做单门归因 (它按 (inst_id,date) 先到先占, 跨原因去重)。

## 2. 数据面 (真值在这里, 不在本文)

| 面 | 文件 | 覆盖的门 | 结算/消费载体 | 去重口径 |
|---|---|---|---|---|
| 影子组合 | `data/shadow_portfolio_entries.jsonl` → `_outcomes.jsonl` | vwap_gate / falsification_vwap_slope / band / vol_gate | `ops/hermes/shadow_settle_daily.sh` (Hermes job `1a4a63fd3dae`, 08:10 CST) | (inst_id,date) **先到先占, 跨原因去重** |
| 触发未中遥测 | `data/trigger_miss_telemetry.jsonl` | 全部触发门 (streak_ban / low_atr / after_cutoff / thesis_drift …) | `scripts/gate_block_counterfactual.py` (**2026-10-17 起挂周更** crontab `50 6 * * 6`, 此前按需跑) | 轮询级, 按 inst_id 去重 |
| 入场确认门 | `data/shadow/entry_confirm_forward.jsonl` | entry_confirm 弃单 | `scripts/settle_entry_confirm_forward.py` (crontab `37 6 * * 2-6`) + `scripts/entry_confirm_postmortem_v1.py` | 一 (inst, 臂) 一行 |
| D 案重放 | `data/shadow/pullback_limit_replay_v2_latest.json` | chase_cap / 确认位等待 | crontab `47 5 * * 2-6` | 按 (inst,date) 配对臂 |

刷新配方 (全只读, 仓根执行):

```bash
PYTHONPATH=. venv/bin/python scripts/gate_block_counterfactual.py     # 门级 30d 拦截质量 → data/gate_block_counterfactual_latest.json
PYTHONPATH=. venv/bin/python scripts/settle_shadow_portfolio.py      # 影子组合结算 (已在 cron, 手动跑幂等)
PYTHONPATH=. venv/bin/python scripts/cf_pullback_limit_replay.py --from-date 2026-09-08
```

### 2.1 终态标签不可用 —— 归因黑洞 (2026-10-16 实证, 台账准入前置)

**现象**: 被门拦下且当日未成交的指令, DB `skip_reason` 绝大多数被写成
`未触发 (no_trigger: 盘中价格未突破入场确认条件)` —— 实测**按门统计 (30 日窗口内被该门拦过 且 当日 executed=0)**:

| 门 | 未成交 n | 终态写「未触发」| 占比 |
|---|---|---|---|
| streak_ban | 25 | 25 | **100%** |
| runaway_down | 9 | 9 | 100% |
| low_atr_block | 124 | 121 | 98% |
| runaway_up | 25 | 24 | 96% |
| extreme_gap | 49 | 43 | 88% |
| borrow_pool_gate | 8 | 7 | 88% |
| before_open_blackout | 21 | 18 | 86% |
| after_cutoff | 292 | 233 | 80% |
| vol_gate_block | 54 | 43 | 80% |
| vwap_gate_block | 70 | 23 | 33% |
| falsification_vwap_slope | 57 | 1 | 2% |
| thesis_drift_invalidate | 23 | 0 | 0% |

**两条当日反证 (日志实据 vs DB 标签)**:

- **US.COIN** (inst 4314, SELL_SHORT, entry 182.50): DB 终态 `未触发 (价格未到)`;
  实际日志有 **43 行** `连败封禁: US.COIN` (21:37→01:57 CST), 且当日 1m bar 有 **35 根** 触及/高于 182.50
  (当日高 183.40) ⇒ **价格条件已满足, 真因是连败封禁**。
- **US.TSM** (inst 4318, BUY, entry 415.00, DA 指令): DB 终态 `未触发 (价格未到)`;
  实际日志有 `低ATR门拦截: US.TSM atr=0.0299 < 0.03` + `#2 触发窗截止: US.TSM ET=14:00 >= 14:00, 不再新触发`,
  且当日 1m bar 有 **147/390 根** 触及/高于 415 ⇒ **真因是低ATR门 + 14:00 触发窗截止**。

**代码自认 (三处注释, 均为未修状态)**:
- `app/playbook/pipeline.py:80` — 「"未触发 (no_trigger)" **误导标签**, 诊断报告分不清"门拦截"与"价格未到"」
- `app/playbook/shadow_tracker.py:258` — 「护栏拦截的行落进 else 分支被误标 '未触发 (no_trigger)', **污染漏斗归因**」
- `app/playbook/pipeline_parts/trigger_flow.py:879` — 「**归因黑洞**: 门拦 13 轮 DB 全程空白 → EOD 兜底盖 '未触发 (no_trigger)'」

**后果**: 任何以终态 `skip_reason` 为分母/分组的读数 (门级避坑·杀机会、误杀率、漏斗) **归属未定**,
不能作为调阈依据。**修法 (零行为变更, 只加字段不改旧语义)**: EOD 写标签时把真实末次拦截原因写进新字段
(如 `terminal_block_reason`), 或从 `trigger_miss_telemetry` 末行派生; 落地后本台账 §3.2 判读列与
§4 全部案例口径需重算。

### 2.1.1 ✅ 已落地 (2026-10-17, 用户裁「同意」P0)

| 项 | 内容 |
|---|---|
| 生产接线 | `app/playbook/shadow_tracker.py` EOD 追踪处: 非成交行把当日拦截原因**分布**浅合并进 `playbook_instructions.metadata_json` (经 `db.merge_instruction_metadata`, 零 schema 迁移); **`skip_reason` 字面一字未改** (6+ 消费方按字面解析) |
| 落库键 | `terminal_block_reason` (主因 = 剔除 `after_cutoff` 后最高频; 只有 cutoff 时 = `after_cutoff`) / `terminal_block_reasons` (全原因轮次) / `terminal_block_reason_last` / `terminal_block_reason_ts` |
| 为什么存分布不存末次 | 末次恒为 `after_cutoff` (14:00 ET 后每轮短路); 实测有 cutoff 的 292 对里 **34% 主因 ≠ 末次** (COIN: streak_ban 85 + cutoff 39; TSM: low_atr 85 + cutoff 39) ⇒ 只存末次等于换个无区分度标签 |
| 历史回填 | `scripts/backfill_terminal_block_reason.py` (`--dry` 默认): since 9/1 非成交指令 **2118** 行, 有遥测者 **122** 行全部回填 (其中 **42 行**原本挂着通用"未触发"标签 = 本次修复的失真行); 幂等, 取证台账 `data/shadow/terminal_block_reason_backfill.jsonl`; 既有 metadata 注记 (phantom_repair 等) 实测零丢失 |
| 立即读数 | 122 行主因分布: `falsification_vwap_slope` 35 / `low_atr_block` 29 / `vwap_gate_block` 26 / `extreme_gap` 8 / `thesis_drift_invalidate` 8 / `borrow_pool_gate` 5 / `vol_gate_block` 4 / `streak_ban` 4 … |
| 查询 | `SELECT json_extract(metadata_json,'$.terminal_block_reason'), COUNT(*) FROM playbook_instructions WHERE date>='2026-09-01' GROUP BY 1` |
| 测试 | `tests/test_terminal_block_reason.py` (7 例: 分布/主因/跨日隔离/缺行/坏行/缓存失效 + **源码契约**: 接线仍在且通用字面未被改动) |
| **新的诚实边界** | since 9/1 非成交 2118 行中 **1996 行 (94%) 无任何门遥测** ⇒ 本面**无证据**说明它们死于门 (≠ 已证明"价格未到"); 122 指令日 vs 1525 轮询事件 ⇒ **轮询行绝不可当样本** |

> 修复后 `falsification_vwap_slope` 等门的主因计数才可信; 但**这仍不是裁决依据** —— 裁决走预注册判据
> (判据字典 B14/B19), 且 §3.2 的「判读」列语义 (按'至少被拦过一次'选人群) 不因本修复而变正确。

## 3. 门级基率 (2026-10-16 读数; 逐日漂, 引用须带日期)

### 3.1 影子组合 (全史 193 行, 已全结算)

| 门 | n | 净合计 $ | 净均 $ | 误杀率 (净>0) | 读法 |
|---|---|---|---|---|---|
| vwap_gate | 87 | −803.0 | −9.23 | 0.74 | 拦的多是小赢家, 但净额被少数大亏单拉负 ⇒ **净避坑** |
| falsification_vwap_slope | 85 | −530.4 | −6.24 | 0.72 | 同上 |
| band | 16 | +172.2 | +10.76 | 0.75 | **拦了赚钱的一群** (n 小, 只登记) |
| vol_gate | 5 | −24.8 | −4.95 | 0.40 | n<10 不判 |

> ⚠️ 「误杀率高 + 净额为负」不是矛盾: 这些小单的反事实是小赢家, 门避的是尾部大亏 ⇒
> 判门好坏**看净额, 误杀率只用来量化代价**。反向形态 (净额为正) 才是真问题。

### 3.2 触发门 30 日拦截质量 (`gate_block_counterfactual`, 基准 `fwd_return_t1_open`, 剔费 0.18%)

> ⚠️ **本表的「判读」列当前不可用 — 必须先读 §2.1。** 该表的 n 与 t1_open 均值可复算, 但
> 「避坑✓ / 杀机会✗」的**归属未定**: 拦死桶 80–100% 的终态标签是失真的 `未触发 (价格未到)`。
> 仅当 §2.1 的归因修复落地后, 本表才可作为调阈依据。

| 门 | 指令 n | t1_open 均 | 净均 (剔费) | 判读 (⚠️ 待归因) |
|---|---|---|---|---|
| vwap_gate_block | 121 | −1.16% | −1.34% | 避坑 ✓ |
| low_atr_block | 115 | +0.10% | −0.08% | 中性 |
| **falsification_vwap_slope** | 93 | **+0.39%** | **+0.21%** | **杀机会 ✗** |
| before_open_blackout | 50 | +0.06% | −0.12% | 避坑 ✓ |
| runaway_up | 38 | +0.50% | +0.32% | 杀机会 ✗ |
| extreme_gap | 30 | −0.96% | −1.14% | 避坑 ✓ |
| vol_gate_block | 22 | −0.02% | −0.20% | 避坑 ✓ |
| **streak_ban** (连败封禁) | 21 | −0.05% | −0.23% | 避坑 ✓ (盈利方向占比 60% ⇒ 拦的多是小赢家) |
| thesis_drift_invalidate | 11 | −4.51% | −4.69% | 避坑 ✓ (最强) |
| runaway_down | 10 | +1.03% | +0.85% | 杀机会 ✗ |

n<10 不判: borrow_pool_gate(7) / reanchored(5) / knife_below(4) / earnings_blackout(3) / liquidity_gate(2)。
台账全表另见同脚本输出 (15 行)。

### 3.3 另两面

- **entry_confirm**: v2 判据 `INSUFFICIENT` (2026-10-17 06:37: n=4<60 ∧ 交易日 1<10, 不作 PASS/FAIL);
  v1 窗事后分解 (至 2026-10-14, n=33): 真避免 9 / **误杀 5** / 口径存疑 2 / 无数据 17。
- **D 案 (chase_cap / 确认位等待)**: 配对 n=163 → D 误杀 **87/148 = 58.8%** (>0.40 门 ❌),
  不成交腿净放弃 **+$1,391.75**; 配对差中位 −$10.61/笔, 日聚类 t=0.81 (MDE ±$11.22) ⇒ 与 0 不可分辨;
  成交率 0.33 (<0.50) ⇒ ADR-035 接线维持关闭。

### 3.4 追价帽终态杀死 39 条 —— 分类 (2026-10-17 新增, 口径: `skip_reason LIKE 'chase_cap_enforced%'` ∧ executed=0)

| 类别 | n | 均 fwd(T+1开盘) | 中位 | 正占比 | 含义 |
|---|---|---|---|---|---|
| 出生即违例 (cap 对**生成锚**已违例) | 10 | +0.31% | −0.02% | 50% | LLM 填反/自造 ⇒ RFC-120 (9/30) 已治住, **10 条全部 ≤9/30** |
| **锚漂移致违例** (cap 对生成锚合规, entry 被 reprice 上调后落到锚错误一侧) | **28** | **+3.31%** | +3.21% | **71%** | 出生合规、漂移后死锁 ⇒ 执行层锚语义问题, **主因** |
| 几何合规 (真追高被拦) | 1 | +0.35% | — | 100% | 闸工作正确 |

- **10/1 起 8 条全是锚漂移 (8/8)** ⇒ 归一化上线后, 帽终态致死**唯一**残留形态。
- ⚠️ 上表 fwd **不得作收益承诺**: ①从"开盘价"起算而非实际可成交价; ②"越过帽"筛出的本就是动量延续票 (选样偏差); ③是"被拦"人群非随机人群。
- **计划自相矛盾子类 (第2批触发价落在自设帽之外)**: 真实生产数据下界 **8 条** (`chase_cap_vs_batch2_conflict` 机检, 9/1–10/16; 判 229 行/跳过 157 行), 其中 **5 条终态死于帽** (TRMD 9-15 / TSLA 9-16 / FDS 9-25 / BE 9-30 / REAL 10-15) ⇒ 贡献 5/39 ≈ **13%** 的死因; 另 3 条: PANW 9-17 (别的门/未终态)、ARM 10-02 (`falsification_slope` 门)、CMC 10-16 (回落成交 ✓ 未死)。
- **处置 (RFC-144)**: prompt 三处加"第2批几何一致性" + cap=null 语义澄清 (治自造帽) + record-only 机检;
  标签侧帽终态 reason 追加 `gen=`(生成锚) ⇒ 两类死因按标签可分。**不放宽闸** (见 §6 末)。
- 抽取局限: 机检只在"第2批/第二批"之后找价位、抓不到数字不判 ⇒ 命中数为**下界**; 不可当精确比率引用。

## 4. 案例流水 (append-only; 新案例按日期追加在表尾)

| 日期 | 标的 | 方向 | 门 | 被拦价 / 帽 | 反事实 (收盘盯市口径) | 判读 | 证据 |
|---|---|---|---|---|---|---|---|
| 2026-10-16 | US.NBIS | SHORT | entry_confirm 弃单 | 锚 178.69 | 收盘 165.47 → **+7.40% (+$436, 33 股)** | **误杀** | `entry_confirm_forward.jsonl` inst 4313 (ABANDONED, grace 2/2 persisted) |
| 2026-10-16 | US.COIN | SHORT | streak_ban (连败封禁) | 182.50 | 收盘 173.16 → **+5.12% (≈+$299, 32 股)**; 触发条件 (反弹≥入场价) 09:56 起满足 35 根 bar | **误杀** | `trigger_miss_telemetry.jsonl` streak_ban (当日 85 行) |
| 2026-10-16 | US.AEM | LONG | entry_confirm 弃单 | 锚 189.00 | 收盘 189.53 → +0.28% (+$16) | 轻度误杀 | inst 4311 |
| 2026-10-16 | US.TEAM | LONG | entry_confirm 弃单 | 锚 218.99 | SL 208.04 于 10:28 被打穿; 收盘 207.82 → −5.10% (−$296) | 真避免 | inst 4312 |
| 2026-10-16 | US.CRM | LONG | entry_confirm 弃单 | 锚 249.36 | SL 238.14 于 11:06 被打穿; 收盘 240.70 → −3.47% (−$258) | 真避免 | inst 4320 |
| 2026-10-16 | US.CMC | LONG | chase_cap (25 轮) | 帽 66.50 (被拦价 66.62→68.00) | 落帽后 12:06 成交 @66.38 → **净 +$12.26** | **非误杀** (延迟放行, 成交价优于首轮被拦价 0.36%) | `pullback_limit_shadow.jsonl` + `trade_records` id 1680 |
| 2026-10-16 | US.NOW | LONG | vwap_gate | 150.155 (锚 151.16) | 生产栈重放 −$298.21 | 真避免 | `shadow_portfolio_outcomes` |
| 2026-10-16 | US.META | LONG | vwap_gate | 731.02 (锚 736.5) | 生产栈重放 −$177.72 | 真避免 | 同上 |
| 2026-10-16 | US.RIVN | SHORT | vwap_gate | 13.13 (锚 13.37) | 生产栈重放 +$9.63 | 轻度误杀 (该单后经确认门成交并净 +$8.43) | 同上 |
| 2026-10-16 | US.AMD / AVGO / ADSK / CMC | LONG | falsification_vwap_slope | 502→… | 生产栈重放 +16.44 / +17.93 / +8.75 / +12.70 | **不宜计误杀** — 四单同日**后续实际成交** (影子行只记首次拦截) | 同上 |
| 2026-10-15 | US.REAL | LONG | chase_cap (锚漂移型) | 帽 9.50 (生成锚 9.30 合规, reprice 抬到 10.48) | T+1 开盘基准 +10.06%; 当日整日 0 成交 | **误杀 (类)** — 计划锚被 reprice 抛弃 ⇒ 结构性死锁 | `playbook_instructions` id 4307 (终态 `chase_cap_enforced`) |
| 2026-09-15 | US.TRMD | LONG | chase_cap (锚漂移 + 第2批几何冲突) | 帽 33.58 < 第2批触发价 33.60 | T+1 开盘基准 +12.30% | **误杀 (类)** | 同上 id 2689 |
| 2026-09-16 | US.TSLA | SHORT | chase_cap (第2批几何冲突) | 帽 365.00 > 第2批触发价 354.00 | T+1 开盘基准 −2.31% | 拦对 (同型但方向相反) — 登记为**同型反例** | 同上 id 2778 |

**当日合计 (仅 entry_confirm 门, 无门反事实)**: 混合臂毛 −$101.0 → 净 ≈ −$118.6
⇒ 该门当日净贡献 **≈ +$119** (避免 2 / 误杀 2; 误杀金额 $452.7 ≈ 避免金额 $553.8 的 **82%**)。

## 5. 待共性分析轴 (供后续调优, 各有载体)

| 轴 | 观察 | 需要的数据 |
|---|---|---|
| **A. 方向 (✅ 已预注册 2026-10-17)** | 既有 B14 面 `data/shadow/da_blocked_cf_latest.json`: 全史 **BUY n=185 T+1 −0.118% (WR 41.1%) / SELL_SHORT n=44 T+1 +2.064% (WR 70.5%)** —— 门在 BUY 侧拦对了方向, 在 **SHORT 侧拦掉的是赚钱单**; 10-16 两条误杀 (NBIS/COIN) **都是 SHORT** | **已做**: `scripts/da_blocked_signal_cf_eval.py` 新增 `preregistered_direction_split` (判据窗 `>= 2026-10-17` 全新样本, 每侧独立 n≥50, 同一 ±0.15% 费用线 + 单侧符号检验, **两侧同报**); 全史桶标 `EXPLORATORY` 只披露不判; 主判据 (B14 全体) 逐字不变 (实测改前后 6 个键全等) |
| B. **"等待型"门同源?** | entry_confirm (等 2min 反转) + chase_cap (等回落帽下) + D 案 (等确认位) 都是"等价格回来才放行" ⇒ 天然错过已启动行情。CMC 是活标本: 帽的放行点落在**峰值后 1.6%** | D 案已配对重放; entry_confirm 需 n≥60 (v2 窗现 n=4) |
| C. **封禁类门** | streak_ban 拦死 25/25 全被误标 `未触发` (§2.1) ⇒ 现有读数不可用; 需按 MFE 分层 + 与 `mfe<0.5%` 判据对齐 | 归因修复后重算 |
| D. **"整日空转"形态** | 同一指令被拦 ≥20 轮且当日 `executed=0` 是最稳的可观测签名 (不需解析文本): CMC 25 轮 / US.REAL 10-15 整日 / 9-8 SKHY 76 轮 | `trigger_miss_telemetry` 按 (inst,date) 聚合轮次 → 日报加一列 |
| E. **四象限定位** | 门级 (净额, 误杀率) 二维: 净<0 且误杀高 = 正常 (小赢家换大亏); **净>0 = 真问题**。**须在 §2.1 归因修复后重算**, 现读数存疑 (如 `falsification_vwap_slope` 的 "+0.21%" 曾被我引作"杀机会", 现已撤回) | §3.2 全表 + 分方向细化 |
| F. **锚漂移 (帽锚计划锚 / entry 被 reprice 抛弃)** | §3.4 主因 (28/39 = 72%); 10/1 起 8/8。机制: `normalize_chase_cap` 的锚**按设计**用生成价 (用活价会让 cap 追价 = 门恒放行) ⇒ 生成后漂移不在其救治范围 | **裁决数据尚不存在**: 现役预注册面自报 `NOT_EVALUABLE_FIDELITY` (fidelity reason_match 0.688 < 0.8) ⇒ L0 逐笔/L1b 聚合"不许可", 口径敏感性"不可判读" ⇒ **不得**用该面切子群 (`RFC-144`)。待尺子修好或另立 B14 式预注册面 |

## 6. 局限

- 影子组合「先到先占」去重 ⇒ 单门归因必须走遥测面 (§3.2); 用 §3.1 分组会系统性漏计后发原因。
- §4「收盘盯市」粗于生产退出栈 (CMC 一例: 盯市 −$146 / 生产栈 +$72 界内), 逐笔判读须回落 §2 的口径。
- 案例 n 均为个位数, **只登记不裁决**; 门级判据仍以各自预注册文本 (判据字典 B14/B19、RFC-141 §4) 为准。
- **§3.2 判读列与 §4 的"被拦门"字段依赖 DB 终态标签, 而该标签已实证失真 (§2.1)** ⇒ §4 的"门"列
  应读作"当日至少被此门拦过一次", 不是"死于此门"; 修正口径须等归因修复后重算。
- `gate_block_counterfactual.py` **已于 2026-10-17 挂周更** (crontab `50 6 * * 6` = ET 周五 18:50 盘后; 台账 §2.4 `crontab-gate-block-cf-weekly`); 引用前仍看产物 mtime。
- **归因修复改变了可用的分母**: 本台账 §3.2/§4 的门级判读建立在"至少被该门拦过一次"的人群上, 而修复后可知 since 9/1 仅 **122 指令日**有门遥测 (§2.1.1) ⇒ 任何"某门 n=121"的读数都是**轮询放大 + 选样**的产物, 重算须用 `terminal_block_reason` 主因口径。
- **"放宽闸" (§5 轴 F 的锚漂移重锚) 目前无裁决数据**: 现役预注册面 `pullback_limit_replay_v2` 自报
  `NOT_EVALUABLE_FIDELITY` (reason_match 0.688 < 0.8) ⇒ L0 逐笔/L1b 聚合"不许可", 且口径敏感性明写
  "本窗结论由观测模型决定而非入场方式 ⇒ **不可判读**" ⇒ 在该面上做"锚漂移子群"切分 = 用未获许可的尺子造假读数。
  要放宽须先修尺子 (reason_match ≥0.8) 或另立 B14 式预注册面 (`RFC-144` §2.3)。
- **§3.4 是描述性人口统计, 不是效应估计**: 分类表回答"死因构成", 不回答"放行后能赚多少" (后者需预注册面)。
