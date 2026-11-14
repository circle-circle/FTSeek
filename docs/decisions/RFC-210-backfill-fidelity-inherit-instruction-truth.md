---
title: RFC-210 — 孤儿仓补录保真度修复（方案B：补录继承指令真值）前瞻验证
status: b1_annotation_deployed_b2_behavior_deferred
date: 2026-11-14
approved_by: 用户「同意」（2026-11-14，授权 KR4 盘点 + 方案B 影子先行）
related: DEB-20261114-da-kpi-backfill-stratification §6、DEB-20260727-entry-fill-price-reconciliation（修尺子+行为修复先例）、OKR-2026-11（流程成熟）、RFC-211（方案A 根治超集，待裁）
---

# RFC-210 — 孤儿仓补录保真度修复（方案B，record-only 预注册）

## 1. 授权边界

用户 2026-11-14「同意」批准：①KR4 盘点 KEEP≤5；②孤儿仓修复**方案B 影子先行**。

**本 RFC 当前只做 record-only 预注册 + 事前证据**：
- **不授权**本批改 `position_sync`/任何生产闸/下单/重启。修复属 behavior-touching，**影子验证 + 用户裁后才上线**。
- 事前证据尺 `scripts/backfill_fidelity_probe.py`（只读 mode=ro）+ 注册表 `backfill_fidelity_probe`（PERMANENT_MEASURE，不占 KR4 净期望名额）。
- 归类**流程成熟度/度量保真修复**（非净期望假设检验、非 C1 新入场假设）；类比 DEB-20260727 entry-fill「修尺子 + 连带行为修复」先例。

## 2. 问题陈述（根因 + 事前证据）

根因（DEB-20261114-da-kpi-backfill-stratification §6 查定）：成交确认 `mark FILLED_UNBOOKED` → 写 `trade_records` 之间被**重启/中断**打断 → 指令停中间态 → `position_sync._reconcile_playbook_execution_states` 补录**降级 record**（`peak=entry` 重置、`atr=_proxy_atr_from_stop` 代理、`market_regime="RANGE"` 硬编码）并标 BOOKED。11 月 57% DA 仓靠此降级安全网回收。

事前证据（`backfill_fidelity_probe.py --start 2026-10-01` 实跑）：
- n=65 补录，**100% 有关联指令**（join metadata.playbook_instruction_id）。
- **regime 保真度仅 3.1%**（补录硬编码 RANGE vs 指令真 regime）；**指令真 regime 100% 可得** ⇒ 方案B 可修正 **63 笔**。
- **指令真 ATR 100% 可得**；代理 ATR vs 真 ATR（atr_pct×entry_price）中位绝对差 **4.34**。
- 结论：指令真值完整可得，补录继承指令真值的事前价值与可行性成立。

## 3. 预注册判据（阈值先写，前瞻窗内禁改；改=重裁）

- **修复定义（方案B，待影子验证后实施）**：`_backfill_playbook_trade_record` 经 `instruction_id` 读关联指令真值——
  - **B1（regime，度量面）**：`market_regime` 取指令 `market_regime`（替硬编码 RANGE）；fail-safe：指令缺失/无 regime → 落 `"UNKNOWN"`（**不再假 RANGE**），不阻断补录。
  - **B2（ATR/SL，行为面）**：`atr` 取指令真 ATR（`_extract_atr_with_basis(inst)`，替 `_proxy_atr_from_stop`）；SL 锚随之用真 ATR。fail-safe：指令无真 ATR → 回退既有代理（不阻断）。
- **前瞻 cohort**：修复上线日（待裁）起；对照=修复前补录基线（10-01→上线日）。
- **样本门**：前瞻 **n≥30 补录事件**（补录由重启驱动，稀疏；最长观察 60 交易日，到期无结论自动 KILLED）。
- **毕业判据（同时满足）**：
  1. **保真度**：前瞻补录 regime 保真度=100%（==指令真值）∧ 真 ATR 采用率=100%（有真 ATR 者）；
  2. **结局守卫（B2 行为面）**：前瞻补录仓 `SL_INITIAL` 率与笔均净 bps **不劣于**修复前基线（容差：SL_INITIAL 率 +≤3pp ∧ 净 bps −≤5bps）；
  3. **零回归**：补录成功率不降（fail-safe 不阻断）、无新增孤儿/两不管仓。
- **决策**：达标 → 提请默认开启（**另立 DEB + 用户裁 + 重启前核**，影子不自动生效）；不达标 → 回退/迭代；到期无结论 → KILLED。
- **必报**：保真度（regime/ATR）、SL_INITIAL 率、笔均净 bps、补录成功率、n、fail-safe 触发数。

## 4. 分阶段（一次一变量）

- **B1 先行**（regime 度量面，低风险）：只改 regime 继承，验证保真度→100% 且零回归。
- **B2 后续**（ATR/SL 行为面，高风险）：B1 站稳后再改 ATR/SL 锚，单独验结局守卫。**B1/B2 不合并上线**（一次一变量）。

## 5. 非重叠声明（不重开已闭轴）

补录保真=**流程/度量修复**，不属 C8/C9/C12/C16（入场过滤）或 C14/C15/C17（隔夜/出场）已闭轴；不改入场决策、不改频率/仓位。恒真红线全维持。与 RFC-209（降频）正交。

## 6. 独立开关与回滚

- 事前证据载体：`scripts/backfill_fidelity_probe.py`（mode=ro）→ 产物 `data/shadow/backfill_fidelity_latest.json`；注册表 `backfill_fidelity_probe`（eval_on_demand，stale_expected，PERMANENT_MEASURE）。
- 修复实施时须独立 config 开关（默认 off）+ fail-safe；**零交易行为变更直至影子验证+用户裁**。
- 回滚：删探针产物+注册表项（脚本留档）；修复上线后回滚=关开关/恢复备份（实施时另定）。

## 7. 验收门（接入验收，非收益裁决）

1. 只读：探针 sqlite mode=ro，零 DB 写/零下单/零生产文件改。
2. 契约测试钉死保真度计算与 join 逻辑（`tests/test_backfill_fidelity_probe.py`）。
3. 修复实施前：影子预注册阈值冻结；B1/B2 分阶段；重启前核（在途出场 SQL + git status）。

## 8. 风险与反面

- **补录稀疏**（重启驱动）：n≥30 可能慢，设 60 交易日上限自动 KILLED，不无限挂账。
- **B2 行为面**：真 ATR 改 SL 锚可能改变出场分布 ⇒ 结局守卫 + 一次一变量 + 影子验证后才上线。
- **指令真值缺失**：fail-safe 落 UNKNOWN/回退代理，不阻断补录（安全网首要职责是接管孤儿仓，保真度次之）。
- **不夸大收益**：本修复主要收益=流程成熟+度量保真+减少孤儿窗口暴露；11 月补录净 −$86 量级小，**不承诺直接盈利提升**。

## 9. KR4 分类

流程成熟度/度量保真修复（PERMANENT_MEASURE 探针 + 行为修复另裁），**非 KR4「与净期望直接相关」实验，不占 ≤5 名额**。KR4 净期望名额仍由 DEB-20261114-KR4-consolidation KEEP≤5 约束。

## 10. B1 部署记录（2026-11-14 20:26 CST，用户「按照建议执行」+ 广授权）

**B1（regime 度量面）已上线；B2（ATR/SL 行为面）仍冻结待裁。**

### 10.1 重新归类：B1 = 纯标注修尺子（零交易消费方，独立核实）

§1 原把方案B 整体归 behavior-touching 待裁。实施前独立核实 **B1 改的 `trade_records.market_regime` 存储列无任何交易决策消费方**，故 B1 属白名单修尺子（度量保真，类比 DEB-20260727 修尺子先例），B2 仍属 behavior-touching：
- **出场**：`exit_engine.py` / `exit_monitor_flow.py` / `exit_dispatcher.py` 零 `market_regime` 引用；出场 regime 闸用**实时** `MarketRegimeGate`（`playbook_service.py:2217-2220`、`auto_execute_flow.py:1003`），不读存储列。
- **执行/入场**：`auto_execute_flow.py:1100/1252`、`ic_tracker.py:526`、`compensation_repair.py`、`horizon_return_model.py` 读的是**指令** `inst.market_regime` / `vix_at_generation`（playbook_instructions 列），B1 不改指令。
- **存储列唯一消费方**：`app/review/daily_review.py:604 _detect_market_regime`（复盘归因，研究）；`signal_validator.py` 历史模式统计仅被 `web_dashboard/routes/battle_plan.py` try/except「失败不影响」消费（看板证据，非下单闸）。
- 结论：B1 改的是研究/复盘标注，**零下单/零出场/零仓位行为变更**；fail-safe 落 UNKNOWN（不再假 RANGE）只让标注更诚实。

### 10.2 部署凭据 / 验证 / 未验证

- **凭据**：HEAD 12607e995；改 `app/core/position_sync.py`（+`_resolve_backfill_regime`:564，wired :740/:750/:783）、`config/unified_config.yaml`（`backfill_inherit_instruction_regime: true`）、`tests/test_position_sync_backfill_regime.py`（8）；未提交。
- **验证**：
  - 重启前核：在途 9 仓（4 DA+5 EXT，全 LONG，周末持仓）`exit_time IS NULL`=9；`git status app/scripts/config` 全本次 WIP；FILLED_UNBOOKED=0。
  - `systemctl restart ftseek-us` → MainPID 2948878→**2967178**；startup_smoke_check **7/7**（ConfigValidator/漂移/热重载/SSOT 0 BLOCKING/双臂/快循环/零 Traceback）。
  - 重启后 9 仓不变、FILLED_UNBOOKED=0、config flat-merge `get()`=True。
  - 定向测试 `test_position_sync_backfill_regime.py + test_position_sync_queue_shadow.py` **13 passed**。
  - 功能级：直调 `_resolve_backfill_regime` 67 笔有指令补录 → **64 source=instruction**（COIN/LLY/CRM/MU/CSCO/DPC 真值 cautious 正确继承）、3 指令无真值正确落 UNKNOWN；探针基线 n=65 保真度 3.1%（历史 record 仍硬编码 RANGE，B1 只改新补录，不回溯改历史）。
- **未验证**：前瞻保真度=100% 需 **n≥30 新补录事件**（重启驱动，稀疏；最长 60 交易日，§3）——下一交易日 11-16 起攒样；B2（ATR/SL 行为面）未实施；任何收益/live readiness。

### 10.3 回滚

`backfill_inherit_instruction_regime: false` + 重启 → 恢复硬编码 RANGE 旧行为（行为中性，无交易影响）。

### 10.4 治理边界

B1 是**写路径标注修复**（非 mode=ro 只读尺），在用户广授权 + 独立核实零交易消费方前提下上线；前瞻保真度确认仍按 §3 攒样（n≥30），不夸大收益（11 月补录净 −$86 量级小）。B2 严格冻结，须影子验证 + 结局守卫 + 另裁。
