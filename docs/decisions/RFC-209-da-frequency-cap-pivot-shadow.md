---
title: RFC-209 — DA 降频 Pivot 前瞻确认（record-only 反事实）
status: record_only_pending_kr4_slot
date: 2026-11-14
approved_by: 用户「按照建议执行」（2026-11-14，授权 DEB-20261114 §9 方案A）
related: DEB-20261114-weekly-pnl-diagnosis §9、OKR-2026-11 §6 Pivot、RFC-207 §3（经济验证另立 RFC 预注册）
---

# RFC-209 — DA 降频 Pivot 前瞻确认（record-only）

## 1. 授权边界

用户 2026-11-14「按照建议执行」批准 DEB-20261114 §9 方案A：①KR4 盘点聚焦 ≤5；②降频立独立 RFC + record-only 前瞻攒样。

**本 RFC 只做 record-only 反事实测量**：
- **不授权**任何真实频率帽/仓位/方向/退出变更；不设生产硬帽；不自动改交易行为。
- 归类**经济假设检验**（非 PERMANENT_MEASURE）：按 RFC-207 §3「后续经济验证另立 RFC 并预注册 bars、费用、日聚类与 MDE」执行。
- 占 KR4 ≤5 净期望名额之一；**前置=KR4 盘点腾名额**（见 DEB-20261114-KR4-consolidation）。
- 非 C1 新入场假设（降频=时间/频率轴，不新增入场源）；仍守 C1「至多一个新入场假设」。

## 2. 问题陈述（数据支撑）

DA 前进窗（10-06→11-13，A9 干净，n=203/29 日）：毛 **−2.23bps**、费 **9.47bps**、净 **−11.70bps**；死单率 20.2% 净 −$3192。
探索性降频反事实（`scripts/pnl_expectancy_pivot_analysis.py`）：限 2–3 笔/日，日均由 −$42.56 → +$7.8~8.6、笔均 +14.6~16.7bps，
**但日聚类 t=0.93~1.58<2、n=29<30 ⇒ 未证、CI 含 0**。本 RFC 把该探索假设转为**前瞻预注册确认**。

## 3. 预注册判据（阈值先写，前瞻窗内禁改；改=重裁）

- **cohort 起点**：2026-11-16（下一交易日，前瞻；不复用探索窗 10-06→11-13，防过拟合）。
- **人群/口径**：`owner_strategy='playbook_sim'` ∧ A9 干净（`exit_price>0 ∧ pnl_net 非空 ∧ exclusion_reason 空`）；bps=`pnl_net/(entry_price×qty)×1e4`（含费）。
- **人群精化（2026-11-14 前置澄清，cohort 未起 days=0，非窗内改）**：再剔除收养/补录记账行（`entry_reason∈{sync_recovery_backfill, playbook_entry_bookkeeping_compensation, adopt_to_playbook}`），只算真实信号入场。依据：补录行 `entry_time`/MFE 为收养时近似值（peak 重置=entry），非干净信号入场，混入会扭曲「取前 N 笔」反事实（`scripts/utils/da_sample.py`、DEB-20261114-da-kpi-backfill-stratification）。补录行真实盈亏另计入总账不抹账。阈值/N/样本门/毕业判据**均不变**。
- **动作（反事实，record-only）**：每日按 `entry_time` 排序取前 N 笔，N∈**{2,3}**（固定，禁窗内调 N）；对照=不限频。
- **样本门**：前瞻 **n≥30 交易日**；不足只观察，不下结论。
- **毕业判据**：对 N=2 与 N=3 各算「capped−uncapped 笔均净 bps」的**按日聚类 80% CI**；
  **任一 N 的 CI 下界 >0** 且 **MDE80 已报** → 提请设生产频帽（**另立 DEB + 用户裁，影子不自动生效**）；
  两 N 均 CI 含 0 → 记「不可分辨」；效应<MDE → 「不可分辨」非「证伪」。
- **决策日**：cohort 起第 30 个交易日（约 2026-12-28）批量裁；到期无结论 → 自动 KILLED（KR4）。
- **必报**：毛/费/净三段 bps、日聚类 80% CI、MDE80、capped/uncapped 日均净 $、保留笔数。

## 4. 非重叠声明（不重开已闭轴）

降频=**时间/频率轴**，不属 C8/C9/C12/C16（入场过滤）或 C14/C15/C17（隔夜/出场）已闭轴；
与 OKR §7「不用频率追样本」不冲突（降频=减样本提期望，非追样本）。恒真红线全维持。

## 5. 独立开关与回滚

- 载体：`scripts/settle_da_frequency_cap_pivot.py`（只读 `mode=ro`，复用 `pnl_expectancy_pivot_analysis` 函数）→ 产物 `data/shadow/da_frequency_cap_pivot_latest.json`。
- eval_on_demand（无 cron 默认）；注册表 `da_frequency_cap_pivot_shadow`，count_mode=`json_key:uncapped.days`，min_n=30。
- **零交易行为变更**：不读/不改任何生产闸、不下单、不推送；仅写只读分析产物。
- 回滚：删产物 + 注册表项；无需重启。

## 6. 验收门（接入验收，非收益裁决）

1. 只读：sqlite `mode=ro`，零 DB 写/零下单/零生产文件改。
2. 前瞻 cohort 起点固定 2026-11-16；n<30 日只观察。
3. 判据/阈值/N 窗内冻结；日聚类 CI + MDE80 必报。
4. 契约测试钉死反事实取前 N 笔逻辑与日聚类公式（`tests/test_pnl_expectancy_pivot_analysis.py`）。

## 7. 风险与反面

- **过拟合风险**：探索窗 t<2，前瞻可能证伪；这是刻意取舍——只买「能改变净期望」的信息。
- **降频减总利润**：笔均期望升但总笔数降，日均净 $ 可能仍负；必报 capped/uncapped 日均 $ 双读数。
- **KR4 前置**：名额未腾出前本影子不转正、不占生产资源。
- **不承诺收益**：本 RFC 只产证据，不宣称 DA 有 edge、不宣称 live readiness。
