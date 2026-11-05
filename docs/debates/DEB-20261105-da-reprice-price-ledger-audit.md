---
title: DA LLM 交易结果、reprice 盈亏比与价格台账审计
status: active
module: playbook
related_okr: OKR-2026-11
related_code: app/playbook/price_ledger.py
tags: [da, reprice, risk-reward, price-ledger, expectancy, observability]
updated: 2026-11-05
---

# DEB-20261105 · DA LLM 交易结果、reprice 盈亏比与价格台账审计

> 状态：**台账与展示修复已部署并完成重启验收；首笔新 reprice/成交留痕待观察，交易规则不变。**
> 用户要求复查 DA LLM 产出链路、交易结果、reprice 后 `risk_reward`、计划/锚定/成交价格与台账，并按建议执行。
> 快照：2026-11-05 16:49 CST / 03:49 ET；结果窗为 2026-10-06→2026-11-05，A9 clean + A10，费后 `pnl_net`。

## 1. 先给结论

1. **成交与盈亏账是闭合的**：142 笔 BOOKED 均能回连指令，141 笔已平仓加当前 COIN 未平仓与成交 DB 一致；没有发现“成交价记错、平仓价对不上、毛收益冒充净收益”。
2. **真正需要修的是口径**：reprice 已更新 entry/SL/TP/size/risk_amount，却长期保留计划 RR；最终减仓后也没有同步风险金额。这会让看板把计划 `4.44` 错当当前 `1.50`，或把减仓后的风险额看得接近两倍。
3. **已实施零门槛修复**：新台账并列计划、reprice 后有效、实际成交三套价格与 RR；新成交按实际成交价和数量写 `execution_risk`；历史成交通过 `trade_records` 只读补齐，142/142 均可计算成交风险额。
4. **不据此改策略**：DA 总体仍负，11 月样本尚不足以称转正；没有证据支持新增 RR 硬门、方向门、追价门或把结构化 invalidation 直接改成硬出场。
5. **已重启验收**：重启前非 BOOKED 出场为 0，联合回归 104 passed；`ftseek-us` 启动冒烟 7/7，`ftseek-dashboard` 与静态新 JS 均正常，启动段无 Traceback/CRITICAL/ERROR。未改配置、未回写历史 DB、未推送。

## 2. DA 结果：胜率不是盈亏平衡线

| 口径 | n | 毛收益 | 费用 | 费后净额 | 费后 bps |
|---|---:|---:|---:|---:|---:|
| 10-06→11-05 | 141 | -$283.41 / -3.60bps | $736.01 / 9.36bps | **-$1,019.42 / -12.96bps** | -12.96 |
| 2026-10 | 111 | -$501.56 / -8.11bps | $608.47 / 9.84bps | -$1,110.03 | -17.95 |
| 2026-11 | 30 | +$218.15 / +12.99bps | $127.54 / 7.59bps | +$90.61 | +5.40 |

- 全窗日聚类 22 日均值 **-7.57bps**，80%CI `[-28.13, 12.98]`，MDE80=43.52bps。
- 11 月日等权仅 **+0.097bps**，80%CI `[-16.10, 16.29]`，MDE80=24.06bps；n=30 小于 OKR 的 120，**只能称点估计改善，不能称转正或放大**。
- 全窗胜率 79.4%，但按平均赢 $21.93、平均亏 -$119.84 计算，盈亏平衡胜率是 **84.5%**；PF=0.71。高胜率没有覆盖少数大亏。

### 2.1 多空拆分不能变成新方向门

| 方向 | n | 净额 | 净 bps | 日聚类读数 |
|---|---:|---:|---:|---|
| LONG | 96 | -$870.24 | -16.58 | 日均 -6.90bps，80%CI `[-32.51, 18.72]` |
| SHORT | 45 | -$149.18 | -5.71 | 日均 +5.89bps，80%CI `[-24.85, 36.64]` |

SHORT 也不显著，且历史逐月符号翻转；本批不做“只做多/只做空”。

### 2.2 主要亏损集中在尾部，而不是所有小赢单

| 出场族 | n | 费后净额 |
|---|---:|---:|
| OVERNIGHT_GAP_STOP | 6 | **-$2,011.48** |
| SL_INITIAL | 11 | **-$1,421.14** |
| 其余 124 笔合计 | 124 | **+$2,413.20** |

六笔隔夜跳空为 LMT -$438.00、AMD -$357.49、CRM -$356.09、CAT -$302.52、SNDK -$293.03、MRK -$264.35。真正的收益杠杆仍是尾部早识别/出场研究；相关 early-tight、entry_confirm、penetration 影子继续按各自预注册结算，本批不叠加新出场变量。

所有 BOOKED 都经过 reprice，没有未 reprice 对照组；上述结果不能称为“纯 LLM 效果”，也不能据此声称 reprice 带来或损害了多少因果收益。

## 3. 产出与价格链：计划、有效、成交必须分开

### 3.1 链路核对

| 阶段 | 权威内容 | 审计结论 |
|---|---|---|
| DA LLM 计划 | Hermes `pipeline.py` 生成 planned entry/SL/TP，并按计划三价算原始 RR | 计划快照仍在 `raw_instruction_json`，可复原 |
| Playbook reprice | `auto_execute_flow.py` 按当时市场/ATR 更新三价、数量与风险额 | 最新三价几何整体可靠；旧 RR 不同步是本批修复点 |
| 执行与成交 | `playbook_service.py` → broker deal/backfill → `trade_records` | 142/142 指令执行价等于实际成交价；来源齐全 |
| 平仓与结果 | 出场引擎、exit ledger、`trade_records.pnl_net` | 141/141 指令平仓价与成交 DB 一致，指令 `pnl` 与费后净额一致，最大舍入差 $0.01 |

成交价格来源为 117 `broker_deal_confirm`、23 `sync_recovery_backfill`、1 retry、1 broker_deal；142/142 有来源。

### 3.2 漂移不是券商滑点

对 142 笔 BOOKED 按方向统一定义“不利漂移”：

| 区间 | 均值 | 中位数 | 补充 |
|---|---:|---:|---|
| 计划 entry → 最新 reprice entry | 116.70bps | 114.03bps | 主要是市场变化与执行重定价 |
| 最新 reprice entry → 实际成交 | **-4.12bps** | -2.15bps | 负值表示略有改善；p90 不利 8.05bps，最大 31.3bps |
| 计划 entry → 实际成交 | 112.58bps | — | **不能把这段全部称为券商滑点** |

142/142 成交均在原始 `entry_low/high` 内；当前 entry/SL/TP 几何合法，成交 DB 的 SL/TP 与指令一致。95 条非法几何全部是 PENDING 且计划阶段已非法，不是 reprice 引入；83 条 triggered、0 executed，78 条为 `entry_confirm_reversed`。

历史 chase cap 只有 BA +2.01bps、MU +11.17bps、TSM +0.46bps 三次轻微穿越，均由 reprice 锚未越 cap 后的成交滑点造成；11 月为零，不新增追价硬门。

## 4. 核心问题一：reprice 后 RR 陈旧

### 证据

- 10-06 起 271 条 reprice 中，**214 条**存储 RR 与当前三价有效 RR 的差异大于 0.02；11 月为 **42/42**。
- 最新三价与 `reprice_tp_mult` 仅 2 条不符，说明主要坏的是旧 RR 字段，而不是 reprice 的目标价公式。
- BOOKED 142 笔：存储 RR 均值 **2.17**，当前有效 RR 均值 **1.59**。
- 11 月 BOOKED 31 笔：存储 RR 均值 **2.46**，当前有效 RR 均值 **1.58**，实际主要为 1.5/1.8。

COIN 指令 5225 是最直观例子：

| 口径 | Entry | SL | TP | RR | 数量/风险额 |
|---|---:|---:|---:|---:|---|
| DA 计划 | 145.000 | 149.500 | 125.000 | **4.44** | — |
| reprice 后有效 | 149.000 | 157.190 | 136.710 | **1.5006** | 39 股 / $319.41 |
| 实际成交 | 149.085 | 157.190 | 136.710 | **1.5268** | 39 股 / $316.09 |

旧看板只显示 4.44，既看不到 reprice 后约 1.50，也看不到成交后约 1.53。DA 执行 admission 未以这个陈旧字段直接拒单，本批也没有把有效 RR 接成新门；因此它是**误导台账/展示的问题，不等于已经证明它造成亏损**。

### 已实施

- 新增 `app/playbook/price_ledger.py`：统一方向几何、RR、实际风险额与历史兼容推导。
- reprice 写 `reprice_audit_v2`：保存 `planned_risk_reward`、`effective_risk_reward`、reprice 后三价、数量和风险额；指令主字段 `risk_reward` 对新行同步为有效值。
- 历史 v1 行不回填：计划 RR 从旧主字段/计划快照恢复，有效与成交 RR 即时按三价计算。
- `get_today_instructions`、指令详情和 pipeline 视图提供 `price_ledger.planned/effective/fill`；看板并列计划、有效、成交口径。

## 5. 核心问题二：最终减仓后的风险额偏高

142 笔 BOOKED 中有 9 笔指令 `position_size` 与实际成交数量不同。根因是执行末段 `gap_dive_halve`，不是 reprice 下错数量，也不是当前 `buy_position_factor`（现为 1.0）。

| 指令 | 日期/标的 | 指令数量 → 成交数量 | 旧 risk_amount | 成交口径风险额 |
|---|---|---:|---:|---:|
| 5017 | 10-30 ORCL | 45 → 22 | $299.70 | $152.68 |
| 5092 | 11-02 ORCL | 47 → 23 | $293.28 | $138.57 |
| 5227 | 11-04 MU | 7 → 3 | $289.52 | $121.65 |

实际下单数量与成交数量正确，所以这是**风险台账虚高，不是实际风险超限**。

### 已实施

- 新成交成功返回实际 `execution_quantity`，成交审计新增 `execution_risk_v1`，记录实际价、量、SL/TP、`risk_reward_at_fill`、`risk_amount_at_fill`。
- 历史同步回填行若缺 `entry_fill.dealt_qty`，通过 `trade_records.metadata_json.playbook_instruction_id` 只读回连实际数量。
- 只读生产连接验证：142/142 BOOKED 都有成交价、数量与成交风险额；9 笔减仓差异全部识别。
- 看板成交后优先显示“成交风险”，同时保留“重定价风险”，不再覆盖计划值。

## 6. TP 是目标锚，不是保证出场价

当前固定 TP 仍受 `min_tp_rr=1.5` 门控，且判定使用**实际成交价、初始 SL、TP**计算；低于门槛则忽略固定 TP，交给 trailing/EOD。10-06 起有 38 笔 `_tp_rr_gate` 留痕，38 笔均因成交 RR<1.5 被吞固定 TP；141 笔已平仓的结果主要来自 breakeven/trailing、初始止损、隔夜跳空和收盘退出。

因此本批把页面“止盈”改为“目标 TP / 非保证出场价”。这也是必须同时展示成交 RR 的原因：reprice 时 1.50 只是估计，成交后的几何才决定固定 TP 是否启用。本批不改 `min_tp_rr`。

## 7. Invalidation 与硬止损：只披露，不直接自动硬切

- 已执行持仓的 invalidation 目前只有影子；A-LITE 记录穿越但不负责平仓，硬出场仍靠 stop/既有出场链。
- BOOKED 142 笔按当前几何分为 70 `stop_before_invalidation`、72 `invalidation_before_stop`；11 月 31 笔为 10 vs 21。
- invalidation-before 组净 -$1,877.01，其余组 +$857.59；但只有 8 笔有 A-LITE 穿越记录，**不能把分组相关当成提前平仓可改善收益的因果证据**。
- COIN 当前 trigger=150.51、stop=157.19，A-LITE 在 150.53 首次穿越后仍持仓，符合 record-only；其文本失效条件还包含“大摩撤回降级”，结构化 trigger 只有价格，不能简单自动转硬。
- ORCL 11-04 trigger=126.5，但首次记录穿越价已为 129.425；按 trigger 价假设成交会严重失真，后续反事实必须使用可执行价格。

后续只在报表披露 invalidation-vs-stop 分类及 reprice 前后变化；如需提前失效出场，必须另立预注册影子，以真实可成交价格计量。

## 8. 审计器本身的两个读数修正

- `da_chain_contract_audit.py --date 2026-11-04` 报出的 `memory_window/memory_era` 是对旧 `pattern_memory` 汇总表的检查；现役 `strategy_memory._window_patterns` 已直接读取 30 天窗口、净额与 A10 数据。**本次不据此误修 DA 提示词或记忆链**；审计器旧表检查留作独立技术债。
- `da_chain_health_report.py --days 7` 在 11-05 03:30 ET 显示 NO_PLAN，当时尚未到当日 DA 生成窗口，不作为断链证据。11-04 契约审计的 stages/memory/feedback_clock 均 ok，33 只有价格证据。

## 9. 验证、边界与下一步

### 验证

- 2026-11-05 16:56 CST 重启 `ftseek-us`，PID 1960314，启动冒烟 **7/7**；16:57 重启 `ftseek-dashboard`，PID 1960459，`/login` 与新 `playbook.js` 均 HTTP 200。重启后 COIN/ALB 持仓不变、非 BOOKED 出场仍为 0。
- 聚焦台账、reprice 契约、查询与成交风险测试：**44 passed**。
- 扩大至关联储件共 124 项：**117 passed**；另 7 项为 `test_playbook_feishu_push.py` 的旧 `$5,000` 夹具/既有文案断言失败，本批未修改推送链，也未为追绿改这些无关测试。
- `py_compile` 通过；`playbook.js` 与 battle plan 内联脚本均通过 `node --check`。
- COIN 5225 只读实库返回计划 4.44、有效 1.5006、成交 1.5268；142/142 成交风险覆盖。

### 明确不变

- SIM 四锁、Playbook 唯一管线、scan_loop、入场/出场阈值、仓位决策、`min_tp_rr`、chase cap、invalidation record-only 均不变。
- 无新硬门、无新实盘路径、无 schema 迁移、无历史 DB 回写、无 notifier 直连、无重启。

### 下一步

1. 首笔新 reprice 验收 `reprice_audit_v2`，首笔新成交验收 `execution_risk_v1` 与看板三口径；历史 COIN 只读接口已验收，不伪称已有新样本。
2. 新台账先观察，不把有效 RR 自动接成拒单门；11 月 DA 继续按 n≥120、日聚类 CI/MDE 结算。
3. `reprice_audit` 仍是 latest-wins，不是完整历史；若需要多次 reprice 全史，另做 append-only 历史，不能伪装当前 audit 已具备。
4. 修复 `da_chain_contract_audit.py` 对现役 30 天记忆窗的旧表误报，作为独立审计器技术债，不与交易行为变更同批。
