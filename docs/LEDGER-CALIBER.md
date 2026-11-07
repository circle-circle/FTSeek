# 账本口径契约 (Ledger Caliber SSOT)

> 常驻文档 — 任何基于交易结果的复盘/IC/WR/OKR 分析, 动手算之前先读本文件。
> 立项根因: 2026-10-01 全链路审计发现"两本 PnL 账对不上"(9月 trade_records 毛 +920
> vs playbook_instructions −222), 且分析者(含本轮 Hermes 自己)反复踩同一批口径陷阱。
> 目的: 把"哪本账是权威 / 哪个字段是什么量纲 / 哪些行必须过滤"钉死, 不再靠口口相传。

---

## 1. 两本账: 谁是权威

| 账本 | 表 | 语义 | 权威性 |
|------|----|------|--------|
| **成交账** | `trade_records` | 券商回报驱动的**已实现**流水; 每行一笔真实开平仓 | ✅ **SSOT** — 一切盈亏/IC/WR 分析以此为准 |
| 指令账 | `playbook_instructions` | DA/UE/bridge 生成的**指令**及其**结算快照**; 一行一指令 | ⚠️ 派生账, 含未平仓快照污染, **不可直接 SUM(pnl)** |

**铁律**: `playbook_instructions.pnl` 是**结算时点写入的快照**, 不是权威实现值。
分析已实现盈亏一律走 `trade_records`, 通过 `metadata_json.playbook_instruction_id`
反向关联指令。

---

## 2. trade_records 字段口径

| 字段 | 口径 | 陷阱 |
|------|------|------|
| `realized_pnl` | **毛** (gross, 未扣费) | 不是净! 净额 = `realized_pnl - fees_est` |
| `fees_est` | 往返费用估算 (fee_model SSOT, 佣金+结算+SEC+TAF+CAT) | `fees_source='est'` 为估算; 实测与 fee_model 差 <$0.5 |
| `mfe_pct` / `mae_pct` | **百分比** (%, 权威) | 见 §4 量纲陷阱 |
| `source_type` | `PLAYBOOK_SIM`(主链+EXT) / `POSITION_SYNC`(券商对账补录) | 分析策略绩效只取 `PLAYBOOK_SIM` |
| `strategy_id` | `playbook_sim`(主链) / `extended_hours_entry`(EXT旁路) / NULL | **必须按此拆分**, 见 §5 |
| `exit_reason` | 退出词表, 见 §6 | 两套退出链词表不同 |

**已结算过滤** (只统计真正平掉的仓):
```sql
WHERE source_type='PLAYBOOK_SIM'
  AND realized_pnl IS NOT NULL
  AND COALESCE(exit_time,'') != ''
  AND COALESCE(exit_price,0) > 0
```
`realized_pnl IS NOT NULL` 不够 — 未平仓行 exit_time 为空但 realized_pnl 可能为 0。

**净额 SSOT 公式**: `net = realized_pnl - COALESCE(fees_est,0)`

---

## 3. playbook_instructions 字段口径

| 字段 | 口径 | 陷阱 |
|------|------|------|
| `outcome` | `win`/`loss`/`void`(幽灵仓)/`holding`(未平)/`already_held`(已持仓跳过)/`closed`(旧schema) | **SUM(pnl) 前必须过滤** `outcome IN ('win','loss','void')`; 否则 holding 快照污染 |
| `pnl` | 结算快照, **净**口径 (2026-08-20 净语义切换) | holding 行是**未实现**陈旧值; already_held 行恒 0 占位。**⚠️ 已净 ⇒ 求和后禁止再减 fees** (2026-11-06 实例: 真实净 +$53.55 被二次扣费报成 +$3.63) |
| `max_favorable_excursion` / `max_adverse_excursion` | **美元 ($)** | ⚠️ 不是百分比! 权威 % 用 `trade_records.mfe_pct` |
| `pnl_pct` | ratio (小数, 非百分数) | 0.02 = 2%, 不是 0.02% |
| `confidence` | LLM/因子置信度 | **无方向预测力** (IC≈0), 见 §7; 不可当绩效分层依据 |
| `model_used` | `deterministic`(占位行标记, 非LLM fallback) / `glm-5.3` / `qwen3.7-max` 等 | `deterministic` 多数是"非当前通道候选"占位行 |

**指令账已结算过滤**:
```sql
WHERE executed=1 AND pnl IS NOT NULL AND outcome IN ('win','loss','void')
```

---

## 4. 量纲陷阱 (实测踩坑, 2026-10-01)

同名字段在两表量纲**不同**:

| 概念 | trade_records | playbook_instructions |
|------|--------------|----------------------|
| MFE | `mfe_pct` = **百分比** (中位 0.57%) | `max_favorable_excursion` = **美元** (中位 34.06) |

本轮审计初版误把 pi.max_favorable_excursion 当 %, 得出"SL_BREAKEVEN 单平均浮盈
34% 被回吐"的**错误结论**; 换权威 mfe_pct 后证伪(实际中位 0.57%, 从未超 1.63%)。
**凡涉及 MFE/MAE 百分比, 一律用 trade_records.mfe_pct / mae_pct。**

---

## 5. strategy_id 必须拆分 (2026-10-01 关键发现)

混合统计会掩盖结构性黑洞。9月实测:

| strategy_id | n | 毛 | 费 | 净 |
|-------------|---|-----|-----|-----|
| `playbook_sim` (主链) | 174 | +882.48 | −880.23 | **+2.25** (盈亏平衡) |
| `extended_hours_entry` (EXT旁路) | 6 | +38.09 | −576.20 | **−538.11** |

EXT 旁路 6 笔把主链的盈亏平衡拖成全系统 −536。其中 CTNT 单笔费用 $451
(NSCC 结算费 $0.003/股 × 68493 股, 与价格无关 → 低价仙股 notional 定仓 = 结算费爆炸)。
**EXT 旁路不过 admission gate** (直接 place_order, 不写 playbook_instructions),
故 admission 的 min_entry_price/min_notional 费用门对其无效。

**规则**: 任何绩效统计先 `GROUP BY strategy_id`, 不混算。

---

## 6. 两套退出链词表

| 退出链 | exit_reason 词表 | 回写 pi? |
|--------|-----------------|---------|
| 主链 (monitor_playbook_sim_exits → _book_playbook_exit_result) | `SL_INITIAL` / `SL_BREAKEVEN` / `SL_TRAIL_LOCK` / `DEAD_SIGNAL_EXIT` / `OVERNIGHT_GAP_STOP` / `PRE_CLOSE_EXIT` | ✅ 带 instruction_id |
| 延长时段 (premarket/afterhours exit, 9/18 前临时阈值) | `tighten` / `lock_in` / `breakeven_lock` / `P3_DUP_CLEANUP` | ⚠️ 9/18 前不带 instruction_id → 回写断链 |
| 券商对账补录 (position_sync) | `external_close` | 经 upsert_external_close |
| 幽灵仓清理 | 含 `GHOST`/`ORPHAN`/`PHANTOM` → outcome=`void` | ✅ |

**回写断链修复**: `scripts/fix_instruction_exit_writeback_gaps.py` (一次性, 已 --apply,
修复 pi.id 2550/2771 两行 9/17 tighten/lock_in 平仓未回写; 现行链路已带 instruction_id, 零新增)。

---

## 7. confidence 无方向预测力 (IC 分层复核, 2026-10-01)

admission.py 自认 conf IC=−0.021(t=−0.33)。本轮按通道分层复核 (trade_records 权威账):

| 通道 | n | IC(conf, 净$) | t |
|------|---|--------------|---|
| deep_analysis | 249 | 0.006 | 0.10 |
| universe_expansion | 53 | 0.072 | 0.51 |
| bridge_scanner* | 46 | −0.028 | −0.19 |
| intraday_reentry | 26 | 0.0 | 0.0 |
| 混合 | 379 | 0.047 | 0.92 |

> \* 通道名义有误 (2026-10-01 复核): 该桶按 raw_instruction_json 字段分类, 但被
> generate 重建批同 did 覆盖过的 DA 直写行 raw 字段已换成重建版 (RFC-122 附录B),
> 实测这些行 decision_id 全为 `DA-` 前缀 = DA 直写。IC 数值不受影响 (算的是同一批
> 交易), 但"bridge_scanner 独立通道"不存在 — 归属一律以 decision_id 前缀为准。

所有通道 |t| < 1.1 → **conf 无方向预测力**。conf≥70 vs conf<55 的 WR(81% vs 82%)
和净%中位(0.194% vs 0.213%)无差异。净$ 随 conf 上升是**仓位效应**(高 conf→大仓→大$波动),
非预测力。

**含义**: 偏斜门 `_trim_majority_direction` 按 conf 砍单 ≈ 随机砍; E8 回放显示被砍者
反事实更好(+1,822) = 反向选择。conf 不应作为绩效分层或砍单优先级的依据。

---

## 8. 数据卫生陷阱清单

1. **占位行污染**: skip_reason="非当前通道候选(bridge未提名/无方向)" 1651 行
   (db.py:993 为未提名 symbol 写的 SKIP 占位, model_used='deterministic')。
   指令表 2471 行里真实候选不足 300。**任何 count 类指标必须先过滤占位行。**
2. **updated_at 非事件时刻**: tracking 每轮覆盖 updated_at, 与 created_at 可差 29 天。
   时序结论必须用 created_at (首插) + 带时区日志双源交叉, 禁用 updated_at 当事件时刻。
3. **僵尸 PENDING**: 17 行终态作废(thesis_drift/TRIPWIRE)但 execution_state 仍 PENDING;
   436 行 >3 天陈旧 PENDING。执行循环按 date 取当日单不会真轮询, 但监控口径会被误导。
4. **SQLite 时区三格式混存**: 字符串 date() 比较会虚增; 用 utils/et_time (脚本) /
   app.utils.timezone.to_et (应用), 禁手写 −12h。open_positions 为 CST。

---

## 9. 快速对照: 9月全系统真实净

```
主链 playbook_sim:      174 笔  净   +2.25   (盈亏平衡, 毛利≈费用)
EXT extended_hours:       6 笔  净 −538.11   (CTNT 结算费爆炸, 已影子化停用)
─────────────────────────────────────────────
全系统 (权威 trade_records 已结算): 净 −535.86
```

主链当前**卡在费用线上** (毛利 $5.07/笔 ≈ 费用 $5.06/笔)。这是第一瓶颈:
修门(RFC-120/122)只解除错误拦截、放行更多交易; 若放行单毛 edge 仍 <$8/笔,
疏通通道 = 放大出血。edge 必须 > 费用线 (用户 8/22 结论, 至今未跨过)。
