# TAKE_PROFIT 死规则重校观测影子 — 预注册文档

> **性质**: C 类策略影子预注册批 ① (2026-09-24 用户批准立项)
> **record-only, 零交易行为变更, 不改任何生产参数/阈值/权重**
> 分析脚本: `scripts/take_profit_counterfactual_replay.py`
> 产物路径: `data/shadow/take_profit_counterfactual_latest.json`

---

## 1. 立项依据

实测 77 日窗 (2026-07-09 ~ 2026-09-23) 共 498 笔已平仓 PLAYBOOK_SIM 交易,
其中仅 **2 笔** 触发 TAKE_PROFIT (0.40%), 而同期 MFE 分布显示:

| MFE 阈值 | 触达笔数 | 占比 |
|-----------|---------|------|
| ≥ 1% | 608 | 68.7% |
| ≥ 2% | 602 | 68.0% |
| ≥ 3% | 594 | 67.1% |
| ≥ 5% | 587 | 66.3% |

**矛盾**: 大多数交易的 MFE 远超当前 TP 阈值 (4.5%-25%, 按 exit profile 不同),
但实际 TP 触发率仅 0.4%。可能原因:
- `min_tp_rr=1.5` 门禁吞掉了再入场臂的快 TP (reentry_quick_tp_gate 已记录)
- Trailing stop 在 TP 触发前已锁利退出
- PRE_CLOSE_EXIT / TIME_EXIT 在 TP 触发前截断

本观测旨在回答: **若 TP 阈值调整为 X%, 有多少交易会改善净额?**

## 2. 预注册判据 (冻结于 2026-09-24, 修改=重新预注册)

### 主判据

对候选 TP 阈值 X% ∈ {1.0%, 1.5%, 2.0%, 2.5%, 3.0%, 4.0%, 5.0%}, 计算:

- **n_triggered**: MFE ≥ X% 的交易数
- **n_improved**: 假设 TP 在 X% 触发时的 hypothetical PnL > actual pnl_net 的交易数
- **net_delta**: Σ(hypothetical_pnl - actual_pnl_net)
- **pct_improved**: n_improved / n_triggered × 100%

### 裁决触发条件

| 条件 | 裁决 |
|------|------|
| 某阈值 X 使 pct_improved ≥ 15% 且 net_delta > 0 且 n_triggered ≥ 30 | **提请 DEB 重校** (另立 DEB + 用户裁) |
| 所有阈值 pct_improved < 10% 或 net_delta ≤ 0 | **维持现状**, 记录为"TP 阈值无改善空间" |
| 中间地带 (10% ≤ pct_improved < 15%) | **延长观察**, 下次数据刷新时重读 |

### 口径冻结

- hypothetical PnL = entry_price × X% × quantity × direction_sign - fees_est
  - direction_sign: LONG = +1, SHORT = -1
- actual PnL = pnl_net (净额铁律: pnl_net 优先否则 realized_pnl - fees_est)
- 数据源: trade_records WHERE trade_mode='simulate' AND exit_time IS NOT NULL AND pnl_net IS NOT NULL
- TP 触发假设: mfe_pct ≥ X% 即视为触发 (不考虑路径依赖 / 触发时机)

### 已知局限 (强制披露)

1. **路径盲区**: mfe_pct 只记录最大浮盈幅度, 不含时间信息; 实际 TP 可能在
   MFE 到达前已被 trailing/SL/TIME_EXIT 截断 → 本分析是 **上界估计**
2. **费用近似**: 假设 TP 触发的 fees_est 与实际相同 (实际费用随持有时间/成交量变化)
3. **min_tp_rr 交互**: 本分析不模拟 min_tp_rr=1.5 门禁效应; 若 DEB 重校, 须同时
   评估门禁调整 (与 reentry_quick_tp_gate 影子联动)

## 3. 基线读数 (2026-09-24 首次采集)

> 以下读数由 `scripts/take_profit_counterfactual_replay.py` 首跑产出。
> 数据: 564 笔已平仓 PLAYBOOK SIM 交易。
> 后续数据刷新只追加不改本段。

| TP% | 触发 | 改善 | 恶化 | 改善% | 净Δ($) |
|-----|------|------|------|-------|--------|
| 1.0% | 495 | 344 | 151 | 69.5% | +$3,724.88 |
| 1.5% | 495 | 361 | 134 | 72.9% | +$10,565.04 |
| 2.0% | 494 | 376 | 118 | 76.1% | +$17,270.60 |
| 2.5% | 491 | 392 | 99 | 79.8% | +$23,843.59 |
| 3.0% | 488 | 393 | 95 | 80.5% | +$30,178.22 |
| 4.0% | 486 | 398 | 88 | 81.9% | +$43,486.87 |
| 5.0% | 484 | 397 | 87 | 82.0% | +$56,236.16 |

### 基线读数解读

**判据对照**:
- 所有阈值 pct_improved 均 ≥ 69.5% (远超 15% 触发线)
- 所有阈值 net_delta 均 > 0 (全部正向)
- n_triggered 均 ≥ 484 (远超 30 门)

**但这不意味着应立即提请 DEB 重校**。原因:

1. **路径盲区**: 本分析假设 MFE 时刻可以精确触发 TP, 但实际上:
   - MFE 可能在 trailing stop 已锁利后才达到
   - MFE 可能在 PRE_CLOSE_EXIT / TIME_EXIT 之后才达到
   - 实际 TP 触发需要**实时价格监控**, 而非事后 MFE 回溯

2. **hypo_pnl 是上界**: `entry_price × X% × quantity × sign - fees_est` 假设:
   - TP 在精确 X% 触发 (无滑点)
   - 持有到 TP 触发期间无其他退出规则抢先
   - 实际中, trailing stop / TIME_EXIT / MFE_STALL_EXIT 会在 TP 触发前退出

3. **读数含义**: 本读数回答的是 "如果**只有** TP 一个退出规则, 且 TP 在精确 X% 触发, 净额会如何"。
   这是一个**纯理论上界**, 不可直接转化为参数调整。

4. **后续**: 需要更精细的路径回放 (1min bar 级别) 才能回答 "在现有退出规则栈上, 调整 TP 阈值能改善多少"。

## 4. 与既有影子/台账的关系

| 相关项 | 关系 |
|--------|------|
| `reentry_quick_tp_gate` (shadow_registry) | 互补: 本项测全量 TP 阈值, 彼项测再入场快 TP 门禁冲突 |
| `exit_calibration.json` | 数据源: 本项复用其 period_days=77 窗口 |
| `exit_profiles.yaml` | 参考: 各 exit profile 的 TP 阈值 (4.5%-25%) |
| `playbook_screening.yaml:min_tp_rr` | 约束: 门禁 1.5 是 TP 触发的下界 |

## 5. 回滚

- 删本文件
- 删 `scripts/take_profit_counterfactual_replay.py`
- 删 `data/shadow/take_profit_counterfactual_latest.json`
- 删 `tests/scripts/test_take_profit_counterfactual_replay.py`
- 零生产路径影响

---

## 附录 A: LONG T+5 IC 反预测线索登记 (④)

> **性质**: C 类策略影子预注册批 ④ (线索登记, 零交易行为变更)
> 登记日期: 2026-09-24
> 登记依据: 三个调查 Agent 实测结论 (2026-09-24)

### A.1 线索描述

| 指标 | 2026-06-24 | 2026-09-23 | 趋势 |
|------|-----------|-----------|------|
| LONG T+1 IC | −0.114 | — | 反预测 |
| LONG T+5 IC | — | −0.073 | 反预测持续 |
| SHORT T+5 IC | — | +0.054 | 正预测 |

**核心矛盾**: 多侧 T+5 反预测信号从 6/24 持续至 9/23 (−0.114 → −0.073,
未翻正), 同时空侧 T+5 正 (+0.054)。这意味着:
- LONG 入场信号在周级时间尺度上**方向错误** (买入后 5 天倾向跌)
- SHORT 入场信号在周级时间尺度上**方向正确** (卖出后 5 天倾向跌)
- 与 "LONG 是周级动物, SHORT 是日级动物" 的先验假设矛盾

### A.2 登记纪律

⚠️ **本条只是线索登记, 不构成任何预注册判据**:
- ❌ 不触发任何 LONG 侧入场门收紧
- ❌ 不触发任何 LONG 侧参数调整
- ❌ 不改变任何 config/unified_config.yaml 参数
- ❌ 不注册为 shadow_registry 条目 (非影子观测)
- ✅ 仅作为 10 月财报季观察项登记

### A.3 10 月财报季观察计划

**观察窗口**: 2026-10-01 ~ 2026-10-31 (Q3 财报季集中期)

**观察指标**:
1. LONG T+5 IC 是否继续为负 (若翻正 → 线索消退)
2. SHORT T+5 IC 是否维持为正 (若翻负 → 线索消退)
3. 分源 IC: DA LONG T+5 / UE LONG T+5 / REENTRY LONG T+5 是否分源同向

**触发条件** (观察期结束后):
- 若 LONG T+5 IC 连续 3 个测量窗口 < 0 → 提请立 LONG 收紧预注册 (另案)
- 若 SHORT T+5 IC 连续 3 个测量窗口 > 0 → 提请立 SHORT 增强预注册 (另案)
- 若双侧翻号 → 线索消退, 本附录归档

### A.4 任何 LONG 收紧须另行预注册+裁决

本线索登记**不授权**任何 LONG 侧行为变更。若 10 月观察期结束后证据指向
LONG 侧需收紧, 必须:
1. 另立预注册文档 (类似本文件)
2. 注册 shadow_registry 条目
3. 冻结判据
4. 经 DEB + 用户裁决后方可转正

### A.5 既有相关记录

- Memory `signal-ic-measurement`: "T+1 IC反预测(-0.114)、T+5正(+0.265); alpha是周级"
- DEB-20260816 §717: "DA LONG T+5 = -0.233%, T+1 不支持多头"
- DEB-20260921-factor-weight-verdict-reconciliation §259: "IC 通道收窄到三口径同号"
- CONTROL-LOOP-LEDGER §G3: SELL_SHORT rho=−0.378 vs BUY rho=+0.151

---

## 附录: 当前 TP 配置快照 (只读参考, 不改)

来源: `config/unified_config.yaml` (2026-09-24 快照)
- exit_profiles: patient=5%, standard=9%, tight=5%, scalping=4.5%, orb=25%
- min_tp_rr=1.5 (再入场门禁)
- take_profit_ratio=2.0 (默认)
- default_tp_pct=6.0 (全局默认)
