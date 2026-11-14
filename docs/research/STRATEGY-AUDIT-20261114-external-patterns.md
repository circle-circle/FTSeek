# 它山之石：外部成熟模式对照内部问题（2026-11-14）

> 上游：`RFC-210`（孤儿仓补录保真）、`DEB-20261114-da-kpi-backfill-stratification`（根因+分层）、
> `DEB-20261114-edge-discovery-premarket`（EXT 盘前 edge）、`DEB-20261114-weekly-pnl-diagnosis`（周盈亏+它山之石）、
> `RFC-209`（DA 降频）、`STRATEGY-AUDIT-20260913-*`（Kelly/费用/前视纪律）。
> 方法：外部模式取**权威一手来源**（已联网核实，见 §6）；每条模式**只映射到本仓已用 `data/ftseek_us.db` 独立复算的问题**，
> 数字一律引内部可复现证据，不采信外部未复算声称。纪律沿用 09-13 体检：结果变量当条件=前视(T9)、分桶未过月切片=时间混杂(T8)、n<30 不下结论、不承诺收益。

## 0. 一句话结论

本仓两个久拖不决的问题，**都不是新题，是经典题**：①孤儿仓=分布式系统经典的 **dual-write（双写）问题**，成熟解是 **Transactional Outbox / WAL**；②DA 不赚钱=**期望值（Expectancy）为负**，成熟解是**按期望值过滤频率/标的**。它山之石已找到，且**轻量版（B1）已上线**，根治版（方案A）与期望值过滤（RFC-209/盘前聚焦）按治理冻结待裁/待前瞻。**没有银弹，不承诺收益。**

## 1. 它山之石 #1 — Transactional Outbox / WAL：孤儿仓根因是经典「双写问题」

### 1.1 外部成熟模式（已核实）
- **问题定义（dual-write）**：一个业务动作要同时写「数据库状态」和「发消息/记台账」两件事，若中途崩溃，两者不一致。这是微服务/分布式系统的**教科书级经典问题**，不是本仓独有。
- **成熟解 Transactional Outbox**（microservices.io / AWS Prescriptive Guidance）：把「要记的台账/要发的消息」**和业务状态写进同一个数据库事务**（outbox 表），再由独立 relay 异步投递；消费方**幂等**（重复投递不重复记账）。崩溃后 relay 按 outbox 恢复，**不丢不重**。
- **同源数据库解 WAL/ARIES**（write-ahead logging）：先落「意图日志」再改状态，崩溃后按日志 redo/undo 恢复到一致态。本质与 outbox 同构——**先持久化意图，再恢复执行**。

### 1.2 本仓内部问题（已用 DB 复算，非外部声称）
- **根因**（`DEB-20261114-da-kpi-backfill-stratification` §6）：`playbook_service.py:2150` 成交即 `mark_execution_filled_unbooked` → `save_trade`(INSERT 台账) → 标 BOOKED。**若在写台账前重启/中断**，指令停 FILLED_UNBOOKED，`position_sync._reconcile_playbook_execution_states` 回收时**补录降级 record**（peak=entry、代理 ATR、硬编码 RANGE）并标 BOOKED。
- **规模/特征**：10 月 11 笔 → 11 月 **52 笔（~5×）**；52 笔**全部对应 BOOKED 指令**（no_instruction=0）；**81% 集中在 09:xx ET 开盘抢单高峰**；**重启相关**（11-12 重启最频繁=当日补录最多 9 笔）。⇒ 正是「双写中途被打断」的经典指纹，**不是** GHOST-RC1 拒记、**不是** quote_fallback 假价（11 月=0）。
- **度量失真**：补录 regime 保真度仅 **3.1%**（硬编码 RANGE vs 指令真值），而**指令真 regime/ATR 100% 可得**（`RFC-210` §2 探针 n=65）。

### 1.3 模式 → 本仓修复映射
| 外部模式 | 本仓对应 | 状态 | 性质 |
|---|---|---|---|
| Outbox/WAL「先落意图、崩溃后按意图恢复干净记账」 | **方案A**：成交确认即落 booking 意图（含真成交价/regime/ATR），重启后**完成原始干净记账**而非降级补录 | **冻结待裁**（behavior-touching，更深更 invasive） | 根治 dual-write 窗口 |
| 恢复时「用已有真值而非占位符」 | **B1**：补录经 instruction_id **继承指令真 regime**（替硬编码 RANGE），fail-safe 落 UNKNOWN | **已上线**（2026-11-14，MainPID 2967178，smoke 7/7） | 轻量缓解·纯标注修尺子 |
| Outbox「消费方幂等」 | 补录不得重复记账（FILLED_UNBOOKED=0 守卫、BOOKED 唯一） | 既有守卫保持 | 一致性约束 |

### 1.4 诚实边界（不夸大）
- **B1 不消除双写窗口、不减少孤儿暴露**——它只让「恢复后的标注」诚实（regime 不再假 RANGE）。独立核实 `trade_records.market_regime` 存储列**零交易决策消费方**（出场用实时 MarketRegimeGate、执行读指令 regime），故 B1=纯标注修尺子，零下单/零出场/零仓位变更。
- **真正根治 dual-write 的是方案A**（outbox 同构），属 behavior-touching，须影子先行+一次一变量+重启前核+用户裁，**本批不上**。
- 11 月补录净 **−$86 量级小**，安全网本身有效（无失控孤儿仓）；本修复主要收益=**流程成熟+度量保真**，**不承诺直接盈利提升**。

## 2. 它山之石 #2 — Expectancy / Kelly：DA 不赚钱是「期望值为负」，解法是期望值过滤

### 2.1 外部成熟公式（已核实）
- **期望值 Expectancy = (胜率 × 均盈) − (败率 × 均亏)**。每笔交易的长期平均盈亏；**>0 才值得做，<0 做得越多亏越多**。这是交易风控的第一性公式，非新发明。
- **Kelly 准则**：f* = (p·b − (1−p))/b，期望为负时 f*<0（不该下注）。09-13 体检已用：本仓净口径 Kelly 在 **$5–8K 名义档才转正**，费用把 Kelly 从 +0.076 翻到 −0.057。

### 2.2 本仓内部问题（已用 DB 复算）
- **DA 略负**（`DEB-20261114-da-kpi-backfill-stratification` §4）：真实信号全史 n=718、死单率 **24.0%**、活单 +35.98bps / 死单 −152.54bps ⇒ **盈亏平衡死单率 19.1%**，实际 24.0% > 19.1% ⇒ 期望为负。死单率趋势 7月32%→9月16.7%→10月15.6%→11月15.8%（**改进保持，11 月无回升**；22.8%「回升」是补录污染，已修尺子纠正）。
- **唯一稳健正期望 = EXT 盘前**（`DEB-20261114-edge-discovery-premarket`）：n=36、**+32.50bps**、日聚类 **t=3.44**（越 Harvey-Liu-Zhu t>3.0 多重检验门）、留一日 t 全>0、top3=45%、10/11 月同号。**不是 EXT 整体**（top3=77%）、**不是 EXT 盘后**（被 NVDA 跳空主导）。DA regular 主通道 **−11.93bps**。

### 2.3 模式 → 本仓修复映射
| 外部公式 | 本仓对应 | 状态 |
|---|---|---|
| Expectancy<0 ⇒ 砍掉负期望的边际交易 | **RFC-209 DA 降频**：降到 2–3 笔/日，砍日内后段「烧费边际单」（探索：DA 日均 −$42.56→+7.8~8.6、笔均 +14.6~16.7bps，t<2 待前瞻） | 影子先行·前瞻 n≥30 待攒（~12-28） |
| 把资本集中到正期望子集 | **盘前聚焦 EXT**（催化剂+RVOL+VWAP 贴近+A/B 级+保本/锁盈），即实务「stocks in play / 盘前 RVOL+缺口验证」 | 前瞻确认窗 11-16 起，达标另立 DEB+用户裁 |
| 费用吃掉 edge ⇒ 费用闸 | **fee_gate_shadow**（KR4 KEEP≤5 之一） | 保留 |
| DA 期望为负但保留为 LLM 孵化器 | **DA 保留**（用户明确）：不靠 DA 赚钱，靠它练 LLM 信号/流程成熟 | 战略定位 |

### 2.4 诚实边界
- EXT 盘前 n=36/16 日、策略 10 月才起、23 切片选 1 ⇒ **仍须前瞻确认**，不据探索窗放大/转正（OKR §7 守）。
- RFC-209 探索 t<2，**未过前瞻**，不承诺救正 DA。

## 3. 已采纳的它山之石 — 度量先行 / 反过拟合（本仓已在用，非新题）

- **Harvey-Liu-Zhu t>3.0 多重检验门**（学术）：本仓 edge 发现已用（t=3.44 越门），防「23 切片选 1」的运气。
- **前视偏差/时间混杂纪律**（回测过拟合文献）：本仓 09-13 体检已立 T8/T9 判据，「结果变量当条件一律判前视」。
- **数据质量先于策略**（data-quality-first）：本仓「**修尺子 before 修行为**」同构——B1/补录分层先把度量修诚实，再谈行为修复（B2/方案A）。**先修尺子，再修行为**正是外部「garbage-in-garbage-out」的工程共识。

## 4. 落地状态（live vs 待裁/待前瞻）

| 项 | 状态 | 凭据/门槛 |
|---|---|---|
| B1 补录继承指令真 regime | **已上线** | MainPID 2967178、smoke 7/7、13 测试过、64/67 继承真值；前瞻保真度=100% 需 n≥30 新补录（11-16 起攒） |
| 补录分层修尺子（4 尺） | **已落地** | mode=ro、契约测试钉死、零交易行为变更 |
| 方案A（outbox 根治） | **冻结待裁** | behavior-touching，影子先行+用户裁 |
| B2（真 ATR/SL 锚） | **冻结待裁** | B1 站稳后另裁，结局守卫 |
| RFC-209 DA 降频 | 影子·待前瞻 | n≥30（~12-28） |
| EXT 盘前聚焦 | 探索·待前瞻 | n≥30∧≥15 日∧t≥3.0（11-16 起） |
| KR4 盘点 KEEP≤5 | 待用户确认 | 超编 ~40→≤5；退役须 six-surface-zero-ref+观察 1 周 |

## 5. 诚实期望（给用户的实话）

- **找到的是「经典题的成熟解」，不是「保证赚钱的新策略」**。孤儿仓有解（outbox），DA 负期望有方向（期望值过滤+盘前聚焦），但**都须前瞻数据确认**，n<30 不下结论。
- **本批已兑现的**：B1 上线（度量更诚实）+ 根因定论 + 它山之石映射 + 修尺子。**未兑现/不承诺**：任何收益率、live readiness、DA 立即转正。
- **下一步靠数据不靠拍脑袋**：11-16 起攒 EXT 盘前前瞻 cohort 与 B1 新补录保真度；每周复跑 4 尺+探针；达标才提请转正（另立 DEB+用户裁）。

## 6. 参考（一手来源，已联网核实）

- Transactional Outbox 模式：microservices.io/patterns/data/transactional-outbox.html（Chris Richardson 模式目录；同事务写 outbox + relay 投递 + 幂等消费）。
- AWS Prescriptive Guidance — Transactional outbox pattern（docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html）。
- WAL/ARIES 恢复：Mohan et al., *ARIES: A Transaction Recovery Method Supporting Fine-Granularity Locking and Partial Rollbacks Using Write-Ahead Logging*（1992，WAL 恢复经典）。
- Expectancy 公式：Expectancy = (Win% × AvgWin) − (Loss% × AvgLoss)（交易风控第一性公式，多源一致）。
- 多重检验门：Harvey, Liu, Zhu (2016), *…and the Cross-Section of Expected Returns*（t>3.0）。
- 内部证据：`RFC-210`、`RFC-209`、`DEB-20261114-da-kpi-backfill-stratification`、`DEB-20261114-edge-discovery-premarket`、`DEB-20261114-weekly-pnl-diagnosis`、`STRATEGY-AUDIT-20260913-external-crossval.md`。
