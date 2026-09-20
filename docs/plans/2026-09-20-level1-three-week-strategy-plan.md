# 一级阶梯三周行动计划：口径统一 → 滑点标定 → 费用 C → 退出回放 → 方向裁决

> **日期**: 2026-09-20
> **状态**: **待 Hermes 独立审核补充 → 用户终裁共识 → 交付执行**（共识门见 §十）。
> 共识达成前**零执行**——含 T1 只读审计亦暂缓。
> **性质**: 战略行动计划（L5 纯文档，零行为改动）。任何交易行为变更另行走 Gate + 影子先行（C4）。
> **触发**: 用户与战略参谋四轮问答（Q1–Q16），逻辑骨架闭合后落地。
> **2026-09-20 增**：用户令「先更新，交 Hermes 审核补充，达成共识后再交由你执行」→ 增设 §十。
> **红线（全部维持，本计划不解锁任何一条）**:
> - C2：打标前严格不切实盘（用户 8/23 终裁）
> - 禁止提频/加仓追盈亏（OKR-2026-09 guardrail 1/2）
> - 双窗口采信纪律（DEB-20260919-extended-hours-profit-lock-and-trail.md：60 日结论已被 180 日改写三例）
> - 单假设纪律（KR3：只许一个假设进检验，防链式 fishing）
> - C1：退出参数冻结至 9/30

---

## 一、裁决快照（本次对话锁定，后续引用以本表为准）

| # | 底牌 | 用户裁决 |
|---|---|---|
| 1 | 定位 | SIM 阶段、**备而不切**；目标是具备随时可切的能力，不是现在切 |
| 2 | 终局 | 实盘稳定盈利；北极星 = 周净 ≥ $1,000（**非本周期 KPI**） |
| 3 | 耐心 | 三级阶梯**每级 3 周**：① 净期望 > 0 → ② 周净 +$100 → ③ 周净 +$1,000 |
| 4 | 病灶押注 | 退出链 + 费用。验尸结论：**费用确认 / 退出半对 / 滑点假设是隐藏最大杠杆** |
| 5 | 费用路径 | **选 C**（最低名义门槛，砍小单保费用率）；否决 B（抬仓位摊薄佣金） |
| 6 | 判定卡 | 五条全数接受（口径对账 / 滑点标定 / 费用 C / 退出双窗口 / 终判 ≥ −$2） |
| 7 | 失败预案 | 若一级不达 → **必须由证据指出真正有用的改进方向**（§三 期望瀑布承担此职） |
| 8 | 门逻辑 | 已向用户解释门1/门2/毕业门禁；R:R 1.29 与实际 WR 77.8% 的错位 → 10 月开跑会重推（§六） |

---

## 二、战略框架：双轨制

```
主轨「磨刃」：期望瀑布 + 三级阶梯（有 deadline，本计划主体）
副轨「备刀」：RFC-090 P0-1..P0-6 + DEB-20260914-cutover-bidirectional-seamless.md D-A..D-D（无 deadline，永不阻塞主轨）
```

- **两轨永不混账**：备刀解决「切得安全」（工程），磨刃解决「该不该切」（证据）。
  工程做不完该切也切不了；工程做完证据不亮照样不该切。
- **时间观（诚实版）**：第一级 3–9 周可信；第二级以月计；第三级被入场侧锁死，**时间未知**。
  未知必须配 kill 线（§五），否则「未知」自己长成「无限」。
- **北极星纪律**：$1,000/周 = 方向灯塔，不是本周期考核。9 月 OKR 明写不设盈利目标；
  盈利目标只在入场侧 edge 被确认后才在 10 月 OKR 设定。

---

## 三、期望瀑布（回答「不知道当前优化有没有效果」）

**核心机制**：把每笔净期望拆成四层可归因贡献。任何优化提案必须声明攻击哪一层，
并用该层的量具证明移动。**无法归因的改动 = 没有证据的改动，不准进生产。**

```
净期望 = 信号毛期望 − 滑点(分时段实测) − 费用 − 退出低效(vs 最优退出反事实)
```

| 层 | 当前基线（n=221，2026-08-20 起） | 量具 | 状态 |
|---|---|---|---|
| 信号毛期望 | 21.04 bps | trade_records 毛/净口径 | 已测 |
| 费用 | **14.45 bps（吃掉毛利 69%）** | $1.99 地板模型（16 笔实盘金标准逐单核对过） | 已测，**C 待落地** |
| 滑点 | **30 bps 往返假设**（把 +$5.00 翻成 −$11.44） | 分时段价差+延迟标定 | **无主，T2 补** |
| 退出低效 | 延长时段「现役 rule 差于什么都不做 +1.07pp/笔」（180 日） | 回放器反事实 | 已测，**分层结论不许混** |
| 净期望（纸面） | **+$5.00/笔**（6.59 bps，扣费未扣滑点假设） | — | 日聚类 t=0.33，不可分辨 |

**瀑布的裁决功能**：3 周后无论一级达标与否，瀑布各层的贡献排序就是
「真正有用的改进方向」的答案——不再靠感觉调参。

---

## 四、三周作战表（W1 9/21–27 / W2 9/28–10/4 / W3 10/5–11）

### T1 · 口径对账（W1，纯只读）——「必须统一是净盈利」

**任务**：
1. 定谳一个悬案：`pnl_net` 的成交价是否已含模拟滑点？
   - 证据初判（**自纠 R-1**）：`app/playbook/playbook_service.py:921` 注释明写
     「Playbook 不经 order_executor（dual_broker 直连）」；`_apply_sim_slippage`
     （`app/execution/engine.py:171`，7bps/边 = `simulate_slippage_bps 5.0 +
     simulate_depth_cost_bps 2.0`）**只作用于 ExecutionEngine 使用者**（更正 R-3）；
     scorecard 宇宙（`PLAYBOOK_*` 四类）全部经 Playbook→dual_broker 直连，
     不经该引擎。**C7 已定谳（§11.1）：PLAYBOOK_SIM 528 行 `sim_slippage_bps` 命中 0
     ⇒ 30bps haircut 是该宇宙唯一被建模的滑点，无 engine 层双重扣减**。
     T1 剩余焦点转为 **N-2（§14.3）**：`exit_slippage_bps`（实测退出偏差，384/528 行）
     是否已含在 `pnl_net` 的成交价里——若是，分层 haircut 只准扣**未建模**部分。
2. 统一「净盈利」SSOT：**净 = 毛收益 − 费用 − 滑点（分时段标定值）**，
   bps 与美元双口径，单位声明入契约测试（军规：新字段必带量纲）。
3. 若发现漏扣/双扣 → 只修**判分面**（scorecard/瀑布产物），不碰交易行为。

**验收**：一页对账结论（含证据截图/SQL 读数），写入本文件附录；净盈利口径全仓唯一。

### T2 · 滑点标定（W1 启动，W3 收口）——「滑点标定没有重点关注过，按建议来」

**任务**：把门1那个独自决定生死的 15bps/边 假设，换成**分时段实测**。

**数据源（更正 R-4 + F-2 后的实况）**：
- ~~60s tick 可成交价序列~~ **不存在**——官方回放引擎 `exit_replay_backtest.fetch_bars()`
  取的是 Futu `K_1M` 1 分钟 OHLC（缓存 `data/replay_bars_1m/`）；
- Futu `pre_high_price` / `after_high_price` 为**全时段累计值、含入场前**，
  不得直接当「进场后可成交价」（`extended_hours_market_session.py:148`）；
- `afterhours_exit_shadow.jsonl`：**已有全套价差字段**
  （`bid/ask/mid/spread/spread_pct/executable_price`，263 行）；
- `premarket_exit_shadow.jsonl`：存量 330 行无 bid/ask，但
  `premarket_exit_shadow.py:447-448` **已写 `bid_price/ask_price`** ⇒ 盘前臂零开发，
  前向攒样至 n≥30（F-2）；
- 6 笔 `SL_TRAIL_LOCK` SIM 成交（ACN/INTC/QCOM/AMD/UMC/WBD）：价差+轮询延迟可测
  （ACN 实测：过冲 0.04% + 价差 0.13%），**但 SIM 撮合无排队/部分成交 → 只能当标定下界**；
- 盘中：`intraday_reentry_shadow.jsonl::spread_bps`（n=1621）+
  `entry_timing_shadow.jsonl::micro.spread_bps`（n=2422）两独立样本。

**产出**：`data/slippage_calibration_latest.json`——分「盘前 / 盘中 / 盘后」三层的
往返成本中位数 + IQR，及各层建议 haircut。
**首跑已完成（§12.3 / §13.2）**：盘中 5.94 / 交叉校 5.37 / 盘后 26.96 / 盘前 n=0 待前向攒样。

**验收**：若与 15bps/边 偏差 > 5bps → 提请门1判据重裁（**改判据须落档 + 用户签字**，
防「口径放水」；实测证据每时段 n≥30 才准提）。

**诚实边界**：本标定测的是「价差 + 延迟」= 实盘滑点的**下界**；
排队、部分成交、冲击只有 live 才能补——那本来就是门2 的职责，不越位。

### T3 · 费用路径 C（W2，9/30 冻结解除后裁决）

**用户已裁**：选 C（最低名义门槛，砍小单保费用率），否决 B。

**前置回放（W1–W2 可做，零行为风险）**：
- 候选门槛：~$9,950 名义额（佣金 ≤2bp/边 的盈亏平衡点，费用诊断实测）；
- 用 60 日 + 180 日双窗口回放：模拟门槛砍单后的 **n 损失 %、每笔期望变化、周净变化**；
- 重点验证费用诊断的警告「不误杀高胜率小单」：被砍单的 WR/期望 vs 保留单对照。

**落地方式（守 C4 影子先行）**：2026-09-20 挂环、9/21 起攒样至 10/11
（比原 10/1 提前，但零行为改动、不改冻结纪律）→ 10/11 用影子数据 + 双窗口回放做真门槛裁决。

**验收（判定卡 T3）**：`fee_bps` 14.45 → **≤10**；n 损失已量化且用户明示接受
（C 与「样本量优先」存在张力，n 损失数字必须摆在裁决桌上）。

### T4 · 退出回放（W2）

**任务**：用 9/19 刚扩完的回放器，按时段/路径分层裁决退出侧。

**纪律**：
- **双窗口同向 ≥ +0.3pp/笔** 的改动才准提生产，否则维持现状（沿用 9/19 纪律）；
- **分层判，不许混**：延长时段 180 日「rule 差于什么都不做 +1.07pp」与全链
  exit_scorecard「executed WR 79% vs held 49%」方向相反——不同宇宙，各自裁决；
- **口径关系（更正 R-6）**：全链 scorecard 宇宙
  （`PLAYBOOK_SOURCE_TYPES` 四类 + `trade_mode != 'live'`，实测 NULL 丢行 = 0）
  **⊃** 延长时段回放子集——子集结论不得直接外推到全链；
- 9 月冻结期内**只回放不改参数**（C1）。

**验收**：一份分层退出结论（延长时段 / 盘中 / 全链三个口径分列），含双窗口读数。

### T5 · 瀑布终算与分叉（W3，10/11）

**时点**：10/10（周六）周检跑出最新 scorecard → 10/11 汇总。

**终判（判定卡 T5，用户已同意）**：新口径重算后
**slip-adj 期望 ≥ −$2/笔**（机制级读数；诚实声明：414 笔测不出统计显著性，
MDE ~22bps，靠瀑布归因，不靠 P&L 置信度）。

**产出**：期望瀑布各层贡献排序 → 喂给 §五 分叉裁决。

---

## 五、10/11 分叉裁决菜单（Q16 的制度化答案）

若一级（≥ −$2/笔）未达，按瀑布残余的最大贡献层裁决方向——**换方向必须带证据，不许拍脑袋**：

| 情形 | 瀑布证据 | 裁决方向 | 下一个（唯一）假设 |
|---|---|---|---|
| A | 实测滑点 < 10bps/边（15bps 高估） | 重裁门1 haircut | 修改须预注册 + 用户签；每时段 n≥30 支持才准提 |
| B | 「从不见肉」笔（MFE<1.5%）仍是最大负贡献（当前 31% 笔数贡献 −117pp，**延长时段 180 日 n=89 口径**，更正 R-2） | **入场侧**，退出侧停止调参 | 候选：时段量 ≥20M 预筛（影子已在跑，n=34 小样本）/ negative-band 拦截 |
| C | 费用地板仍 >30% 毛利 | C 门槛收紧或复查 B | 回放先行，双窗口验证 |
| D | 退出反事实差距 > 滑点+费用之和 | 退出简化 | rule → hold-to-open / trail_floor 单臂 |

**通用纪律**：任何情形，下一个假设**只许一个**，预注册判据后攒样（KR3 纪律）。
连续 2 个入场侧假设被干净证伪 → 升级为**项目方向裁决**（用户主持，选项开放含 STOP）。

---

## 六、10 月开跑会议程（用户主持，9/30 结算后）

1. **9 月结算**：KR2 已破（换版一次，判据明写）→ 按文件走项目级裁决；
2. **门1 判据重裁**：G1-4 冻结阈值 R:R ≥ 1.29 是按 WR≈44% 设计的；
   实际 WR 77.8%，盈亏平衡 R:R 只需 0.28。菜单：维持 1.29（逼不对称）vs
   重推为「滑点折算净期望 ≥0 + 日聚类可分辨」；
3. **haircut 所有权移交**：标定值成为新基线；此后改它 = 改判据，须落档 + 用户签；
4. **KR3 单假设选择**：从 §五 分叉菜单选下一个（只选一个）；
5. **C 的 n-loss 复盘**：影子期读数 + 双窗口回放 → 真门槛裁决。

---

## 七、备刀轨道（并行，无 deadline，永不阻塞主轨）

- **RFC-090 P0-1..P0-6**（建议顺序 P0-3 → P0-2 → P0-1 → P0-6 → P0-5 → P0-4）；
- **DEB-20260914-cutover-bidirectional-seamless.md 的 D-A**（capital_state 按臂读取，回切致命，P0）+ D-B/D-C/D-D；
- **测量面（更正 R-9）**：`cutover_preflight --watch`（日级哨兵，cron `38 6 * * *`，
  P0–P9 已覆盖半翻检测 / capital_state 分臂 / open_positions 臂判别与 PK 碰撞 /
  `sl_broker_order_id` 语义碰撞 / STOP 兜底里程 / trade_mode 双向混同）——
  RFC-090 P0 项的验收读数**直接读该哨兵**，不新写体检脚本；D-A 优先级可读其 P3 基线；
- 全部走独立 Gate + 独立回滚；C2 红线不因备刀进度松动一毫米。

---

## 八、诚实边界（写给 3 周后的自己）

1. **统计上**：414 笔测不出 6.59bps 的 edge（需 ~4,500 笔 ≈ 98 周）；
   本周期一切结论都是**机制级**，靠瀑布归因，不靠 P&L 显著性。
2. **标定下界**：SIM 量不出排队/部分成交/冲击——那是门2 的活，别提前透支结论。
3. **窗口效应**：60 日结论已被 180 日改写三例（含「唯一实测增益」被撤回）；
   一切单窗口读数只作参考，双窗口同向才采信。
4. **阶梯是顺序，不是日历**：9 周最乐观摸到一级（≈0），摸不到三级（$21.7/笔）；
   三级被入场侧锁死，其时间线在一级达成后的复盘中单独定 kill 线。

---

## 九、验收清单

- [x] **共识门**：Hermes 审核回执（§十一）+ 用户终裁（§十三「按建议同意」）+ Codex 复查（§十四）
- [x] T1 口径对账结论落档（§十五：C7 无双扣定谳 + N-2 三问答 + 净盈利 SSOT + 复算命令）
- [ ] T2 `slippage_calibration_latest.json` 三时段读数 + 偏差 >5bps 时提请门1重裁
      （首跑已出：盘中 5.94 / 盘后 26.96 / 盘前 n=0 待攒样；重裁已进 10/11 议程 §13.2——
      勾选条件 = 盘前 n≥30 并并入提案）
- [ ] T3 双窗口回放 + 影子挂环 + n-loss 量化
      （前置回放已出 §十六：$9,950 证伪、价格门+$4k 名义门组合胜出、根因=交收费；
      影子已于 2026-09-20 挂环、9/21 起攒样；勾选条件 = 10/11 门槛终裁）
- [ ] T4 分层退出结论（延长时段 / 盘中 / 全链三分列）
- [ ] T5 瀑布终算 ≥ −$2/笔 或触发 §五 分叉
- [ ] 10 月开跑会五项议程全部带到裁决桌

---

## 十、Hermes 审核补充清单（共识门）

> **给 Hermes 的角色定位**：你不是背书者，是**打假者 + 补充者**。逐条实跑核验，
> 不许只读文档；结论三态（成立 / 不成立 / 待补），更正按 R-x 留痕，不悄悄改。
> 审核目标是把本计划从「参谋的纸面推演」升级为「可交执行的共识件」。

### 10.1 数字复核（每条必须独立复跑，读数入回执）

| # | 本计划声称 | 复跑方式 | 打假成功的判据 |
|---|---|---|---|
| C1 | WR 77.8% / R:R 0.35 / 毛 21.04 / 费 14.45 / 净 6.59bps / slip-adj −$11.44 | 读 `data/exit_scorecard_latest.json` 的 `payoff` + `size_normalized` 块 | 任一数值不符或口径标注失真 |
| C2 | 「毛期望=0 已证、440 候选 / 1,813 信号全零」 | `docs/OKR-2026-09.md` KR3 诚实先验 + `DEB-20260823-full-chain-review-and-safety-patches.md` 三判决（更正 R-5：4 个同名文件，须写全名） | 引用断章或数字漂移 |
| C3 | 「31% 从不见肉笔贡献 −117pp」 | DEB-20260919-extended-hours-profit-lock-and-trail.md §12（180 日 n=89） | **口径只覆盖延长时段子集**——若被引用成全链结论即失真，须标注宇宙边界 |
| C4 | $1.99 地板 100% 触发 / ≤2bp 需 ≥$9,950 名义 | `docs/analysis/2026-08-24-fee-structure-diagnosis.md` §一/§四 | — |
| C5 | ~46 笔/周 / $210–256 周成本 | `docs/OKR-2026-09.md` KR5 | — |
| C6 | 6 笔 `SL_TRAIL_LOCK` + 价差/过冲读数 | `trade_records` 按 `exit_reason='SL_TRAIL_LOCK' AND trade_date BETWEEN '2026-09-16' AND '2026-09-18'` 查 6 行（更正 R-1：执行台账只有 2 行；更正 R-12：不带窗口命中全历史 120 行）+ `DEB-20260919-extended-hours-profit-lock-and-trail.md` §一/§6.1 | 台账行数或字段不符 |

### 10.2 设计挑战（防错 + 防重复建设）

| # | 待挑战设计 | 打假方式（必须实跑） | 需 Hermes 给的答案 |
|---|---|---|---|
| C7 | **自纠 R-1 初判**：Playbook 成交价不含 `engine.py` 7bps 模拟滑点，30bps haircut 是唯一滑点计费 | ① `rg "_apply_sim_slippage"` 调用面；② `app/playbook/playbook_service.py:921` 起追 dual_broker 直连链；③ 抽查 `trade_records` PLAYBOOK_SIM 行 `metadata.sim_slippage_bps` 存在性；④ 成交价 vs 决策时点盘口对拍 | 定谳「双扣 / 漏扣 / 正确」；若非正确，T1 直接出修正结论 |
| C8 | 滑点标定数据源真实可用 | 核 60s tick 序列覆盖窗口与标的；`pre_high_price`/`after_high_price` 读取点；两个 exit_shadow jsonl 行数与字段 | 数据不足则 T2 改设计（不要硬跑） |
| C9 | 回放器支持「最低名义门槛」臂（费用 C） | 读 `scripts/replay_ext_entry_sources.py` 是否支持 min-notional 过滤 | 不支持则给**最小扩展面**评估（禁止临时硬塞） |
| C10 | 延长时段回放宇宙 vs 全链 scorecard 宇宙的可比性 | 两边样本定义/SQL 对照 | §四 T4「分层不许混」是否已守住；给出各自宇宙声明 |
| C11 | 期望瀑布与既有量具是否重复建设 | 对照 `exit_scorecard` size_normalized / `app/analysis/trade_attribution.py` / `docs/REPLAY-SCRIPT-LEDGER.md` | 瀑布应**复用**哪些件、只新增哪些件（口径 SSOT 只许一处） |
| C12 | 时间表与 cron / 冻结纪律冲突 | ① T3 在 9/30 前只回放不改参数；② 周六 09:05 周检时点；③ 拥堵分钟为 **06:25（4 作业）/ 06:30（3）**，06:35 无活跃作业（更正 R-7；出处 DEB-20260919-review-decision-support-system.md §20.2②） | 排期修正建议 |
| C13 | 判定卡阈值出处 | 逐个查：≥−$2/笔、fee_bps ≤10、双窗口 ≥+0.3pp、每时段 n≥30 | 哪些有既有出处、哪些是**新立**（新立须用户签认，不许默认生效） |
| C14 | C 门槛影子的注册方式 | 读 `shadow_registry.py` 现有形态是否适配「门槛影子」 | 落点建议 + 判据字典登记项 |
| C15 | 备刀轨道排序 | D-A（回切致命）vs 主轨无 deadline 的相对优先级 | 是否应把 D-A 提前，理由与风险 |

### 10.3 请 Hermes 补充的落点建议（不是打假，是填空）

1. **滑点标定脚本落点**：新建 `scripts/` 还是扩展既有回放器（防第五把尺子）；
2. **瀑布产物载体**：扩展 `exit_scorecard` 输出 vs 新 json——净盈利口径 SSOT 只许一处；
3. **C 门槛影子**：注册进 shadow_registry 的具体条目 + 攒样判据；
4. **10/11 终判读数的产出方**：周六周检扩展 vs 独立脚本（避免周末人工拼数）；
5. **「退出低效」层的反事实基线定义**：hold-to-open / trail_floor / 最优退出，哪个作参照系——
   这是瀑布里最容易被窗口效应污染的一层。

### 10.4 审核输出契约（Hermes 回执格式）

```
1. 每条 C1–C15：判定（成立/不成立/待补）+ 复跑命令 + 实测读数
2. 更正：R-x 留痕（错在哪、证据、改哪节），不悄悄改原文
3. 补充：§10.3 五项落点建议，各带最小实现面 + 验收命令
4. 终局结论（三选一）：可执行 / 改后执行 / 否决——各附一句话理由
5. 回执落档：追加为本文件 §十一，用户终裁后进入执行
```

**执行交接条款**：共识 + 用户终裁后，由 Codex 按本计划 T1→T5 顺序执行；
每步独立验收、独立留痕；任何交易行为变更仍走 Gate + 影子先行（C4），
本共识门不豁免任何既有军规。

---

## 附录 · 本计划的对话来源

四轮问答全文要点已浓缩进 §一裁决快照。关键实数出处：
`exit_scorecard_latest.json`（n=221，WR 77.8% / R:R 0.35 / 毛 21.04 / 费 14.45 / 净 6.59bps）、
`DEB-20260914-cutover-bidirectional-seamless.md`（graduated=False，G1-1 −$15.62/笔）、`OKR-2026-07`（$1,000/周 KR 失败史）、
`OKR-2026-09`（不设盈利目标 / 单假设 / $210–256 周成本）、`DEB-20260919-extended-hours-profit-lock-and-trail.md`（180 日双窗口 / 三次自纠）、
`2026-08-24-fee-structure-diagnosis.md`（$1.99 地板 / C 路径）。


---

## 十一、Hermes 独立审核回执（共识门 · 2026-09-20）

> **审核者**: Hermes（用户 9/20 令「Codex已更新 待审状态 请你核查分析补充」）
> **审核方式**: 逐条**实跑**（grep / sqlite3 / 读产物 JSON / 读 jobs.json 与 crontab），只读，零改动任何文件与开关。
> **三态**: 成立 / 不成立 / 待补。更正按 **R-x** 留痕（错在哪、实测证据、应改哪节），原文一字未动。
> **结论摘要**: C1–C15 中 **7 条成立、6 条需更正、2 条待补**；计划骨架（瀑布 → 阶梯 → 分叉菜单 → 共识门）**成立且可用**，
> 但 **T2 盘前臂的数据源在现网不成立**（必须改设计）、**2 个阈值是新立**（须用户签）、**1 处既有哨兵被漏引**。
> **终局结论: 改后执行**（见 §11.5，三个前置条件）。

### 11.1 C1–C15 逐条判定

| # | 判定 | 复跑命令与实测读数（原文摘要） | 处置 |
|---|---|---|---|
| C1 | **成立** | `python3 -c "import json;d=json.load(open('data/exit_scorecard_latest.json'))[payoff/size_normalized]"` → `n=221, win_rate=77.8, avg_win=33.69, avg_loss=-95.69, rr=0.35, breakeven_rr=0.28, expectancy_per_trade=5.0, slip_haircut_bps=15.0, expectancy_slip_adj=-11.44`; `gross_bps=21.04, fee_bps=14.45, net_bps=6.59, slip_haircut_roundtrip_bps=30.0, net_bps_after_slip_haircut=-23.41, day_clustered: t=0.33, n_days=21, mde_80pct=29.69, distinguishable=false` | 六项数字**逐字相符**，无需改 |
| C2 | **成立**（引用需精确） | `docs/OKR-2026-09.md:67`「已在 **440 候选 / 1,813 信号 / 4,057 快照**上找过，全为零」；`DEB-20260823-full-chain-review-and-safety-patches.md:730` 同数 | 数字成立；但 `DEB-20260823*` 有 **4 个同名文件**，计划 §10.1 C2 的「三判决」须写全名（R-5） |
| C3 | **成立**（宇宙标注只在 §10.1，未进 §五） | `DEB-20260919-extended-hours-profit-lock-and-trail.md:289` 标题 = 「入口特征 → 「能否摸到 1.5%」预测力分析 (**180 日 n=89**)」；`:292`「从未见肉 <0.4% 占 22%/−75.8pp；小肉 0.4~1.5% 占 9%/−40.9pp ⇒ 合计 **31% 的笔贡献 −117pp**」 | 数字成立且**该文件本身即延长时段专档** ⇒ §五 情形 B 的行内文字必须补宇宙边界（R-2） |
| C4 | **成立** | `docs/analysis/2026-08-24-fee-structure-diagnosis.md:19`「**100% 订单触发 $1.99 最低佣金地板**」；`:47`「≤2bp/边需 **≥$9,950** 名义额」 | 原文保留 |
| C5 | **成立** | `docs/OKR-2026-09.md:76`「~46 笔/周 × $4.54 ≈ **$210/周**」；`:98` KR5 实测「$4.96/笔 × 51.6 笔/周 ≈ **$256/周**」 | 「$210–256」区间即取自「预估 vs 实测」，原文保留 |
| C6 | **数字成立 / 复跑方式不成立** | 6 笔确实存在，但**不在** `data/extended_hours_exit_executions.jsonl`：该文件 15 行、`SL_TRAIL_LOCK` 仅 **2** 行（ACN/WBD）。真源是 `trade_records.exit_reason='SL_TRAIL_LOCK'` 的 **6 行**：ACN/UMC/WBD(9-18)、AMD/INTC(9-17)、QCOM(9-16)（**9/16–9/18 簇**；全历史该 exit_reason 共 120 行，复跑必须带窗口过滤——更正 R-12）；价差/过冲读数出处 `DEB-20260919 §6.1:126`「60s 轮询过冲 0.04% + 买一价成交 价差 0.13%」✅ | 复跑方式改用 `trade_records`（R-1） |
| C7 | **成立 = 定谳「正确（无双扣）」** | ① `_apply_sim_slippage` 仅定义于 `app/execution/engine.py:171`，**唯一写 `sim_slippage_bps` 的地方是 `:202`**；② 调用面仅 `engine.py:461/521/530`（ExecutionEngine 内部撮合路径）；③ Playbook 下单走 `playbook_service.py:1567 _dual.submit_order()`，全链路**无 ExecutionEngine 引用**（`grep ExecutionEngine app/playbook/` = 仅 `:921` 注释）；④ 实测 `trade_records` 全表按 source_type 统计 metadata 含 slippage 键的行：**PLAYBOOK_SIM 528 行中 436 行命中的是 `exit_slippage_bps`（实测退出偏差），`sim_slippage_bps` 命中 0 行**；MAIN_PIPELINE/SCALPING/ORB 亦 0 行 ⇒ **30bps haircut 是 PLAYBOOK 宇宙唯一被建模的滑点，无双重扣减** | 结论可用；两处措辞须精确（R-3） |
| C8 | **不成立（按现网数据源无法照原设计跑）** | ① 「60s tick 可成交价序列」**不存在**：`data/` 无任何 tick/quote 序列，官方回放引擎 `exit_replay_backtest.fetch_bars()` 取的是 **Futu `KLType.K_1M` 1 分钟 OHLC**（`autype=NONE`，缓存 `data/replay_bars_1m/` **45** 个标的 + `data/replay_bars/` **1981** 个标的日档）；② `pre_high_price/after_high_price` 读取点在 `manual_order_service.py:268/270`、`premarket_exit_shadow.py:328`，且 `extended_hours_market_session.py:148` 自述二者是**全时段累计值, 含入场前** ⇒ 直接当"进场后可成交价"会错；③ **盘前 shadow 无买卖价字段**（`premarket_exit_shadow.jsonl` 330 行 keys 只有 last_price/pre_high/pre_volume/prev_close…），**盘后 shadow 有**（`afterhours_exit_shadow.jsonl` 263 行含 `bid_price/ask_price/mid_price/spread/spread_pct/executable_price/quote_age_seconds/session_volume_delta`） | **T2 盘前臂必须改设计**（R-4）：盘后可用现成字段直测价差；盘前只能「前向攒样新增字段」或「1m bar 近似 + 声明为上/下界」 |
| C9 | **不成立（回放器不支持）** | `scripts/replay_ext_entry_sources.py` argparse 全部选项 = `--days/--source/--recent-days/--min-chg/--min-vol/--session/--end/--universe-from/--pair-base/--split-half/--out`，**无任何 notional/门槛过滤**；全文件 `grep notional` = 0 命中 | §10.3-3 已给最小扩展面（见 11.3） |
| C10 | **成立，但须补一条口径关系** | `exit_scorecard.py:45` `PLAYBOOK_SOURCE_TYPES = ('PLAYBOOK_SIM','PLAYBOOK_LIVE','PLAYBOOK_SHADOW','MANUAL_PLAYBOOK')`，主查询 `:243 source_type IN (...) AND trade_mode != 'live'`；延长时段回放宇宙 = 延长时段候选（`replay_ext_entry_sources`）。两宇宙**不是互斥而是包含**：延长时段成交在 `trade_records` 里就是 `PLAYBOOK_SIM` 行（实测 8/20 起 223 行全链 PLAYBOOK 宇宙中含大量延长时段行）⇒ 「分层不许混」成立，但必须显式写出**包含关系**，否则会把延长时段的病算到盘中 | 原文保留 + 补声明（R-6）。附：NULL 陷阱已实测排除（`trade_mode IS NULL` 的已平仓 PLAYBOOK 行 = **0/223**，`!= 'live'` 未静默丢行） |
| C11 | **成立（不重复建设）+ 一处既有风险** | 既有件：`exit_scorecard.size_normalized()`（毛/费/净 bps + haircut 入参，**已覆盖瀑布 1/2/5 层**）；`app/analysis/trade_attribution.py` 是**逐笔 5 维描述性归因**（方向/入场/出场/时长/盈亏比），**不是**期望分层 ⇒ 瀑布与它不重复，属互补；`docs/REPLAY-SCRIPT-LEDGER.md` 规定新回放脚本必须 `import exit_replay_backtest` 取原语（禁第五把尺）。**风险**：`scripts/okr_status.py:71-95` 已有一份**并列的 bps 算式**，其注释自述「与 exit_scorecard.size_normalized() **互为对照, 两边必须同值**」⇒ 全仓已有两处同口径实现，瀑布若自写第三处必然漂移 | 载体落点见 11.3-2 |
| C12 | **不成立（数字错位）** | 实测 crontab **活跃行**按「分 时」聚合最挤的是 **06:25 = 4 作业**、**06:30 = 3**、**06:20 = 3**、**05:50 = 3**、**05:35 = 3**；**06:35 无活跃作业**（`CONTROL-LOOP-LEDGER.md:153` 记的两个 06:35 一次性作业已消失，`cutover_preflight` 38 6 行即为其"避开"注记的残留）。周六周检 job 实测存在且 enabled：`FTSeek-US 周六周检` = `5 9 * * 6` | 排期避 **06:25/06:30**（R-7） |
| C13 | **2 条有出处 / 2 条新立** | ① `fee_bps ≤10`：**有出处** = 用户 8/22 证伪结论「费用线 ≈10bps」（`DEB-20260822-tn-mechanism-shadow.md:75/85`「费用 ~10bps/笔；$646 是毛利 $15 的 42 倍」、`DEB-20260917-extended-hours-auto-trading.md:387`「edge 未过费用线（约 10bps，见用户 8/22 证伪结论）」）✓；② `每时段 n≥30`：**有出处** = 全仓军规（多份 DEB/DA 提示词同款）✓；③ `双窗口同向`：**有出处**（`DEB-20260919 §11.1` 双窗口对照即采信门槛）但**数值 `≥+0.3pp/笔` 在文档中无原文**——该 DEB 出现的读数是 `+0.381pp`(60日)→`+0.164pp`(180日)、`+0.05~0.07pp`、以及「按既定采信门槛(双窗口)不达标」的判定，没有「≥0.3pp 才准提」这条阈值；仓库里另一处 `+0.3pp` 出自 `DEB-20260816` 的**方向 gap** 判据（不同宇宙）⇒ **新立**；④ `≥ −$2/笔`：全仓 **零命中**（仅存在于本计划）⇒ **新立** | ③④ 须用户签认（R-8）；不许默认生效 |
| C14 | **成立（现有形态适配）** | `app/playbook/shadow_registry.py:46 ShadowItem` = `id/name/status/description/data_path/config_key/min_n(默认30)/current_n/graduation_criteria/eval_script/created/last_evaluated/verdict_history/notes/**count_mode**("lines" / "distinct:<field>")/**eval_on_demand**/意图归组` ⇒ 「门槛影子」可直接登记：`data_path` 指 jsonl、`count_mode` 用去重口径、`eval_script` 指判据脚本、`eval_on_demand=True` 若按需触发 | 具体条目与判据见 11.3-3 |
| C15 | **成立，但引用文件名歧义 + 漏引既有哨兵** | `DEB-20260914` 有**两个**同名文件；D-A 真身在 `docs/debates/DEB-20260914-**cutover-bidirectional-seamless**.md:92`「D-A（P0·回切致命）`capital_state` 按臂读取」，且 `:275` 自述「待办从"一堆 schema 工程"收敛成**一处 6 行改动**」。**漏引**：该 DEB §8.2 已落地日级哨兵 `scripts/cutover_preflight.py --watch`（`38 6 * * *`，`CONTROL-LOOP-LEDGER.md:153` 在册）已覆盖 P0 半翻检测 / P3 `capital_state` 分臂基线 / P4 `open_positions` 臂判别与 PK 碰撞 / P5 `sl_broker_order_id` 语义碰撞 / **P6 broker STOP 兜底里程** / P7 `trade_mode` 双向混同 / P8 研究臂老化 / P9 台账连续性 ⇒ **备刀轨道的测量面已经存在**，D-A 的"是否提前"可以用它的读数决定，不必另建 | 见 11.4-①（R-9） |

### 11.2 更正（R-x，均经实测）

- **R-1（C6 复跑方式）**: `extended_hours_exit_executions.jsonl` 只有 2 行 `SL_TRAIL_LOCK`；6 笔在 `trade_records`。**原句「台账行数或字段不符」是真的，但被查的对象本身选错了** —— 若按原方式复跑会得「不成立」的假否定。**R-12 补充（第五轮复查）**: 复跑必须带窗口 `trade_date BETWEEN '2026-09-16' AND '2026-09-18'`——不带窗口的 `exit_reason='SL_TRAIL_LOCK'` 命中**全历史 120 行**（自 2026-07-14），照 R-1 原文复跑会把文档误判为造假；那 6 笔特指 9/16–9/18 簇（QCOM 9/16、AMD+INTC 9/17、ACN+UMC+WBD 9/18，与 §四 T2 标的清单一致）。
- **R-2（§五 情形 B 宇宙）**: 本计划 §10.1 C3 自己已写明该口径「只覆盖延长时段子集」，但 **§五 情形 B 的行内只有数字没有宇宙**。二者不一致时必须让**执行处**（§五）带上边界，否则 10/11 分叉会拿延长时段的读值去决定入场侧方向。应写：「（延长时段 180 日 n=89 口径）」。
- **R-3（C7 两处措辞精确化）**: ① 「`_apply_sim_slippage`（7bps/边）」正确（= `simulate_slippage_bps 5.0 + simulate_depth_cost_bps 2.0`，`unified_config.yaml:297-301`），但须注明**它只作用于 ExecutionEngine 使用者**；② 「**Playbook 是唯一下单管线**」**不成立** —— 实测 `trade_records` 还有 MAIN_PIPELINE 179 / SCALPING 129 / ORB_ENGINE 11 行也在下单。正确表述为：**scorecard 宇宙（`PLAYBOOK_*` 四类）全部经 Playbook→dual_broker 直连**，故该宇宙不受 engine 滑点模型影响。
- **R-4（T2 数据源）**: 见 C8 —— 「60s tick 可成交价序列（回放器已在用）」不成立（实为 **K_1M 1 分钟 OHLC**，无 tick 序列）；`pre_high_price/after_high_price` 是**全时段累计含入场前**；盘前 shadow **无 bid/ask**，盘后 shadow 有全套价差字段。T2 若照原设计跑盘前臂 = 无数据可测。
- **R-5（引用精确）**: `DEB-20260823*` 4 个文件、`DEB-20260914*` 2 个文件 → 计划内所有 DEB 引用应写全名（含短横线后缀），否则复查者无法定位（本次已实际踩到：C15 首查 `DEB-20260914-premarket-data-validity-audit.md` 零命中 D-A）。
- **R-6（C10 口径关系）**: 补「全链 scorecard 宇宙 ⊃ 延长时段子集」的显式声明（含 `PLAYBOOK_SOURCE_TYPES` 四类 + `trade_mode != 'live'` + 已实测 NULL 丢行 = 0）。
- **R-7（C12 拥堵分钟）**: 06:35 → **06:25(4)/06:30(3)**。
- **R-8（C13 新立阈值）**: `≥ −$2/笔`、`双窗口 ≥ +0.3pp/笔` 需用户签认（`fee_bps ≤10` 与 `n≥30` 有既有出处，可直接沿用）。
- **R-9（C15 漏引既有哨兵）**: 备刀轨道应把 `cutover_preflight`（日级、棘轮、只读钉桩、28 例 + 变异 8 刀）写成**测量面**，与 RFC-090 P0 清单并列；D-A 的 6 行改动优先级可直接读该哨兵 P3 基线。

### 11.3 §10.3 五项落点建议（各带最小实现面 + 验收命令）

1. **滑点标定脚本落点** → **不新建独立尺子**，两个落点：① 盘后臂**直接复用** `scripts/ah_exit_shadow.py` / `afterhours_exit_shadow.jsonl` 现有 `bid/ask/spread/executable_price/quote_age_seconds` 字段做聚合（零新增采集）；② 盘前臂若必须测价差 → 在 `scripts/premarket_exit_shadow.py` **加 4 个字段**（`bid_price/ask_price/spread_pct/quote_age_seconds`，与盘后同名同义），**只写不拦**（影子语义），攒样 n≥30 前不出结论。**验收**: `python3 -c "import json,collections;rs=[json.loads(l) for l in open('data/afterhours_exit_shadow.jsonl')];print(sum('spread_pct' in r for r in rs), len(rs))"` 应 >0；产物 `data/slippage_calibration_latest.json` 必须含三时段中位数 + IQR + n + 单位声明（军规：新字段带量纲）。
2. **瀑布产物载体** → **扩展 `scripts/exit_scorecard.py` 的 `size_normalized` 块**（毛/费/净 bps 已在此），只新增两键 `slippage_bps_layered`（来自 T2 标定）与 `exit_counterfactual_bps`（来自回放反事实）；**不新开 json**，且顺手把 `scripts/okr_status.py:71-95` 那份并列算式改为「读 exit_scorecard 产物 + 保留同值断言」，否则全仓三处同口径。（C11 结论）
3. **C 门槛影子** → 登记进 `shadow_registry`：`id="fee_floor_min_notional"`、`data_path="data/fee_floor_shadow.jsonl"`、`count_mode="distinct_where:...=True"`（按「指令/决策」去重，**不可用 lines**，防逐轮灌水虚增 n）、`min_n=30`、`eval_script=<新脚本>`、`grad_criteria="被拦单 vs 保留单 双窗口同向且费用率降幅 ≥ 目标"`；**同批挂 cron + 同批入 `CONTROL-LOOP-LEDGER.md`**（守卫测试 `test_crontab_loops_are_ledgered_or_baselined` 会抓漏登）。另：回放器的最小扩展面 = 在 `replay_ext_entry_sources.py` 加 `--min-notional`（1 个参数 + 过滤条件，**不动原语**；新回放脚本仍须 `import exit_replay_backtest`）。
4. **10/11 终判读数产出方** → **扩周六周检**（`FTSeek-US 周六周检` `5 9 * * 6`，enabled 已实测）加一节「瀑布分片」，**不新建独立脚本**：理由是终判必须在同一时点、同一口径、同一版本下产出，跨脚本拼数正是窗口效应的载体。**验收**: 10/11 当日该 job 的推送里含四层贡献排序 + n + 口径标签。
5. **「退出低效」层的反事实基线** → **三参照系并列，不选唯一**：`hold-to-open`（最保守、可直接从 1m bar 算）、`trail_floor`（现役地板口径）、`最优退出`（事后最优，**只作上界，不进结论**）。**漏斗纪律**：报告必须打印 `n` 与「该层读数的窗口（60/180 日）」两列，且**双窗口同向才进结论**——这层是全瀑布最易被窗口效应污染处（本仓已有 3 例 60 日被 180 日改写的先例）。

### 11.4 计划未提但审核中实锤、必须知道的三件事

① **备刀轨道已有日级哨兵（勿另建）**: `cutover_preflight.py --watch`（`38 6 * * *`）的 P0–P9 探针里，**P5 `sl_broker_order_id` 语义碰撞、P6 broker STOP 兜底里程、P7 `trade_mode` 双向混同** 已覆盖 RFC-090 §3.5（live STOP 无补偿）/§3.3（戳混用）两处最硬的裂缝 ⇒ RFC-090 的 P0 项落地时，**验收可直接读该哨兵读数**，不必新写体检脚本；反之若两处各写一份，就是新的漂移源。
② **工作区有未提交的行为改动（9/19 批次）**: `scripts/extended_hours_entry_executor.py`(+38/−15 含 fcntl 去重锁 + 原子 replace)、`scripts/afterhours_exit_shadow.py`、`scripts/premarket_exit_shadow.py`、`tests/test_afterhours_hold_to_open.py`(+28) 四个文件**至今未 commit**，而 `docs/debates/DEB-20260919-extended-hours-profit-lock-and-trail.md` 未提交的 §14.4 已声称「本批零交易行为变更…31 passed…重启后启动冒烟 7/7」⇒ **文档声称的验证对象不在库里**。本计划「共识前零执行」不受影响（这批 mtime 09-19，早于本计划），但**必须先定归属并提交或撤回**，否则 T3 的 10/1 影子上线会叠在一份未入库的基线之上。
③ **同口径实现已有两处**: `exit_scorecard.size_normalized()` 与 `okr_status.py` 并列算式（注释自述「两边必须同值」）。瀑布落地时若不收敛，就会变成**第三处**——这与本计划 §三「无法归因的改动不准进生产」同一条纪律。

### 11.5 终局结论：**改后执行**

骨架（期望瀑布 → 三级阶梯 → 10/11 分叉菜单 → 共识门）**成立、可用、且与本仓既有纪律不冲突**；C1–C5 的数字逐字相符，C7 的定谳方向正确（无双重扣减）。

**三个前置条件（不改完不许进入 T1）**：
1. **T2 盘前臂改设计**（R-4/C8 不成立项）：盘前价差今日不可测（无字段、无 tick、`pre_high` 含入场前）⇒ 要么改为「盘前前向攒样 + 盘后先行」，要么把盘前臂降级为「1m bar 近似 + 明确声明为上/下界」。按原设计跑 = 无数据硬跑。
2. **两个新立阈值须用户签认**（R-8/C13）：`≥ −$2/笔`、`双窗口 ≥ +0.3pp/笔`（`fee_bps ≤10`、`n≥30` 有出处，可直接沿用）。
3. **R-1/R-2/R-3/R-5/R-6/R-7/R-9 七处措辞与引用更正**落进对应小节（§五 B 补宇宙、§四 T2 改数据源、C6 换复跑源、DEB 引用写全名、C12 改 06:25/06:30、补 cutover_preflight 为备刀测量面）。

**不构成否决的理由**：C1–C5 的实数逐字对得上，说明"验尸结论"这一层是干净的（费用确认 / 退出半对 / 滑点假设是隐藏杠杆）——计划押注的病灶没有错；错的是**执行面的几处工具与引用**，且都在 T1 之前可一次改完。

> 落档: 本回执即为共识门材料。用户终裁「可执行」后，按 §十 交接条款交 Codex 执行 T1→T5；**每步独立验收、独立留痕**，交易行为变更仍走 Gate + 影子先行（C4）。


---

## 十二、用户五条裁决 + 本轮执行 / 分析 / 更新（2026-09-20 第二轮）

> 用户原话（逐条对号）：
> ①「需要让 playbook 成为唯一自动交易下单管线，其他管线都是历史」
> ②「T2 先跑 看看是否有值得优化提升的问题点，如若行不通再申明边界」
> ③「我无法确定 内容不够清晰」（针对 §11.5 前置 2 的两个新立阈值）
> ④「避免冲突 先提交」
> ⑤「注意不要重复造轮子」
> 「以上执行检查分析更新」

### 12.1 ④ 先提交 —— 已完成（`0cd6ab9af`）

代提交 9/19 延长时段批次（他 agent WIP，原样入库，未改一字）：

| 文件 | 内容 | 出处 |
|---|---|---|
| `scripts/extended_hours_entry_executor.py` | `_dedup_reserve` 加 `fcntl.flock` 独占 + `mkstemp` 原子 replace（防并发双读/双写） | `DEB-20260919-review-decision-support-system` §18.5 及其 §743 归位建议 |
| `scripts/premarket_exit_shadow.py` / `scripts/afterhours_exit_shadow.py` | 两处 9/17 旧注释语义订正（盘后 carry 次日盘前保留利润保护） | `DEB-20260919-extended-hours-profit-lock-and-trail` §14.4 |
| `tests/test_afterhours_hold_to_open.py` | +1 回归（carry 仓 09:20 触发 `TAKE_PROFIT` 必须走 `--session premarket`） | 同上 |
| 两个 DEB | §14.4 新增 + §23.11 段序归位（docs-only） | — |

**提交前实跑**：`py_compile` 4/4 OK；`pytest test_afterhours_hold_to_open + test_extended_hours_entry_executor + test_ext_take_profit_only` **33 passed**；`pytest -k "extended_hours or afterhours or premarket or push_hub"` **10 passed**。
**未覆盖项（如实登记）**：`_dedup_reserve` 的 flock 行为**无专测**（`grep tests/` 零命中），仅由周边套件兜底 ⇒ 该项应补一个并发回归钉子（列入 §12.5 P1）。
**未入库清单（闭包外，留在工作区）**：`obsidian_vault/*`、`handoff.md`。
**F-1 修正**：§11.4-② 那条「未提交即风险」现已消解，本节取代其结论。

### 12.2 ① Playbook 成为唯一自动下单管线 —— 实测：**行为上已经是，缺的是把死枝与"可复活路径"一起收掉**

实测（`sqlite3 data/ftseek_us.db`，2026-09-20）：

| 来源 | 行数 | 最近成交 | 现状判据 |
|---|---|---|---|
| `PLAYBOOK_SIM` | 528 | **2026-09-18** | 唯一活跃入场管线 |
| `POSITION_SYNC` | 5 | 2026-09-17 | 对账回补，非入场管线 |
| `MAIN_PIPELINE` | 179 | **2026-06-30** | 已冻结：`order_executor.py:3392-3403` 硬闸「非 playbook 路径下单被拦截」（2026-07-02 冻结期） |
| `ORB_ENGINE` | 11 | **2026-06-22** | `config.unified_config.yaml::orb.enabled=false`（`scheduler_core.py:697` 判该键才构造引擎） |
| `PLAYBOOK_SHADOW` | 23 | 2026-06-23 | 影子 |
| `SCALPING` | 129 | **2026-05-18** | 已停 |

⇒ **今天的实际行为 = 只有 Playbook 在开新仓**。所以「让 playbook 成为唯一下单管线」的剩余工作是**消除"能把它复活"的路径 + 清理死枝**，而不是改造执行链：

**P0（1 行，零行为变化）**：`app/pipeline/order_executor.py:3402` 的允许清单 `('PLAYBOOK_SIM','PLAYBOOK_LIVE','ORB_ENGINE')` **收窄为只剩两个 PLAYBOOK**。今天行为零变化（ORB 已 `enabled=false`），但它把「有人把 `orb.enabled` 翻回 true 就能绕开统一链路」这条后门一并关掉——这正是用户裁决 ① 的字面意思。
**P1（删 ORB 模块，2735 行）**：`app/orb/{config,engine,exits,__init__}.py` + `scheduler_core.py:692-711/1885/1974` 接线 + `orb` 配置段（25 键 / universe 10 只）+ 台账与文档引用。
**删前必查清单（已实测）**：在仓 ORB 仓 = **0**（`open_positions` 命中 0）；读点共 5 处：`pipeline/position_monitor.py:1072`（退出监控 IN 列表，需同批去掉 ORB）、`pipeline/stages/risk_stage.py:143-157`（ORB 持仓上限计数，随模块删）、`order_executor.py:125`（owner→source 映射）、`risk/position_exit_monitor.py:687`（`orb_` 前缀判定）、`scheduler_core.py` 三处接线；EOD 孤儿兜底函数**未引用 ORB**（grep 零命中）⇒ 无需改兜底。
**P2（主线死枝）**：`order_executor` 内部仍有大量 `'MAIN_PIPELINE'` 默认标签（`:1234/1311/1348/5298/5424` 等）与 `signal_outcomes` 学习闭环分支（`:5546-5606`）。这些是**默认值/兜底标签**而非活路径 ⇒ 建议**只改默认值语义与文档定调，不删分支**（删默认值会牵动数十处赋值，风险 > 收益）。这与用户「做减法」不冲突：减法优先砍**能复活管线的路径**（P0/P1），标签类死枝留到最后。

### 12.3 ② T2 先跑 —— 已跑，且**跑出了本轮最值得优化的那一处**

**产物**：`data/slippage_calibration_latest.json`（首次生成；本次为 inline 只读聚合，**未新建脚本**，遵裁决 ⑤）。

实测读数（报价价差 = 一次穿越成本下界；往返成本 ≈ 报价价差，以 mid 为参照）：

| 层 | 数据源 | n | 往返中位 (bps) | IQR | 对 30bps 现役假设的偏差 | 是否触发门1 重裁（>5bps） |
|---|---|---|---|---|---|---|
| **盘中 (RTH)** | `data/intraday_reentry_shadow.jsonl::spread_bps` | 1621 | **5.94** | 5.94 | **−24.06** | ✅ **触发**（假设偏悲观） |
| 盘中 (交叉校) | `data/entry_timing_shadow.jsonl::micro.spread_bps` | 2422 | **5.37** | — | −24.6 | ✅（两独立样本同向） |
| 盘后 (16–20 ET) | `data/afterhours_exit_shadow.jsonl::spread_pct×100` | 114 | **26.96** | 34.15 | −3.04 | ❌ 落在 ±5bps 带内 |
| 盘前 (04–09:30 ET) | `data/premarket_exit_shadow.jsonl::bid_price/ask_price` | **0**（330 行存量早于该字段） | — | — | — | 前向攒样（**代码已备字段**，见 F-2） |

**F-2 修正（我 §11 的一处过头话）**：§11 的 R-4 说「盘前 shadow 无买卖价字段」——对**存量数据**成立，但 `scripts/premarket_exit_shadow.py:447-448` **在 HEAD 里已经写 `bid_price`/`ask_price`**（只是 330 行历史行落在这段代码之前）⇒ 盘前臂**零新增开发**，只需前向攒样至 n≥30。R-4 的「必须改设计」应降级为「等新行」。

**这为什么是"值得优化提升的问题点"**：现役 haircut 15bps/边（往返 30bps）用在**盘中**偏悲观约 24bps。按 `exit_scorecard.payoff` 的口径折算（名义中位 $5,481）：

```
现役: 期望 +$5.00 − 30bps×$5,481 = 5.00 − 16.44 = −$11.44/笔   (scorecard 实测值 ✓)
若盘中取实测 5.94bps:          5.00 −  3.26 = +$1.74/笔
若盘后取实测 26.96bps:         5.00 − 14.78 = −$9.78/笔
```

⇒ **同一批 221 笔的 slip-adj 期望会因"盘中/盘后占比"而反号**。这不是"让数字变好看"，而是**门1 判据 G1-1/G1-4 的输入口径问题**：现在全池统一按 30bps 罚，等于给盘中仓多罚 ~24bps。
**处置（按 T2 自己的验收条款）**：偏差 >5bps ⇒ **提请门1 判据重裁，须落档 + 用户签字**（不许执行 agent 自己改）。本轮只产出读数与提请，不改任何判据。
**边界声明（不许越位）**：本读数 = 报价价差下界，**不含**排队/部分成交/冲击/SSR 拒单/券商 STOP 触发质量；盘中两样本均为影子宇宙（再入场候选 / 入场时点影子），n 虽大但不是实际成交标的；盘后样本来自真实持仓决策点但盘后薄量、价差结构性宽。⇒ 真要替换 haircut，须先在 §六 议程上把"分层 haircut"作为**判据变更**表决。

### 12.4 ③ 两个新立阈值 —— 重写成"能拍板"的形态（这是 §11.5 前置 2 的澄清版）

上一版只给了名字，没给"签了会改变什么"。现在按**行为后果**拆开：

| 阈值 | 它实际管什么 | 不签 / 缓签的默认行为 | 签字后改变的行为 | 建议 |
|---|---|---|---|---|
| `≥ −$2/笔`（T5 终判） | 10/11 **"一级是否达成"的判定线** | 没有这条线 ⇒ T5 只能报读数，不能判"过/不过"，§五 分叉菜单也没有触发条件 | 读数 ≥ −$2 ⇒ 进二级（周净 +$100）；< −$2 ⇒ 按瀑布残余最大层走 §五 四选一 | **签，但建议同时钉住它的口径**：`slip-adj 期望（分时段实测 haircut）+ 日聚类 t/MDE 一并报`，防"改 haircut 把线刷过去" |
| `双窗口 ≥ +0.3pp/笔`（提出生产改动的门槛） | **准入门槛**：任何退出侧改动要进生产，必须 60 日+180 日两窗口同向且 ≥ +0.3pp | 没有这条线 ⇒ 每次回放都能找出某窗口"更好"的版本（本仓已有 3 例 60 日被 180 日改写）；9 月 C1 冻结期本来就只回放不改参数，所以**缓签 3 周无成本** | 线定死后，10 月起只有过线的改动才进提案 | **可缓签**：先把 9/19 既有"双窗口同向"纪律用着（该纪律有出处），数值阈值等 T4 第一批双窗口读数出来后再定——**先看数、后定线**，避免先定线再找数 |
| （有出处，无需签） | `fee_bps ≤10`（用户 8/22 费用线）✓；`每时段 n≥30`（全仓军规）✓ | — | — | 沿用 |

**一句话版**：第一条（−$2/笔）建议签，但把"分时段实测 haircut"写进同一条，否则它会变成可以被口径搬动的门柱；第二条（+0.3pp）建议**等 T4 的数再定**，9 月冻结期不需要它。

### 12.5 ⑤ 不要重复造轮子 —— 落成三条硬约束（写进执行交接条款）

1. **备刀轨道复用既有哨兵**：RFC-090 P0 项的验收读数直接读 `cutover_preflight --watch`（`38 6 * * *`，P0–P9 已覆盖半翻检测 / `capital_state` 分臂 / `open_positions` 臂判别与 PK 碰撞 / `sl_broker_order_id` 语义碰撞 / STOP 兜底里程 / `trade_mode` 双向混同），**不新写体检脚本**（§11.4-① / R-9）。
2. **T2 不新建尺子**：本次首跑用 inline 只读聚合出产物；后续若要周期化，落点仍是「复用 `exit_scorecard.size_normalized()` + 扩展其入参」而不是新脚本，且回放类脚本必须 `import exit_replay_backtest` 取原语（`docs/REPLAY-SCRIPT-LEDGER.md` 公约）。
3. **同口径只许一处**：`scripts/okr_status.py:71-95` 的并列 bps 算式改为读 `exit_scorecard` 产物（保留同值断言），防瀑布成为第三处（§11.3-2 / C11）。
4. （补）**本次代提交暴露的一处欠账**：`_dedup_reserve` 的 flock 无专测 ⇒ 补一个并发回归钉子（同批：`cutover_preflight` 已有 28 例 + 变异 8 刀的做法可照抄）。

### 12.6 更新后的执行接口（取代 §11.5 的三前置）

| 原前置 | 现状 |
|---|---|
| 1. T2 盘前臂改设计 | **已消解**（裁决 ② + F-2）：代码已写 bid/ask，前向攒样即可；盘后/盘中已出读数 |
| 2. 两个新立阈值须签 | **重写见 §12.4**：−$2/笔 建议签（附口径钉）；+0.3pp 建议等 T4 数再定 |
| 3. 七处措辞/引用更正 | 待 Codex 落笔（R-1…R-9 + F-1/F-2）；其中 **R-4 降级**（见 F-2） |
| — | **新增**：门1 haircut 分层重裁提案（§12.3，须用户签） |
| — | **新增**：管线退休三步（§12.2 P0/P1/P2），P0 为 1 行、今天行为零变化 |

**终局结论维持：改后执行**——骨架不变；工作量的形状已从"改设计"收敛为"1 行收窄 + 一批删除 + 一次判据表决 + 补齐引用"。


---

## 十三、用户终裁落档 + 执行记录（2026-09-20 第三轮 · 用户令「按建议同意」）

> 用户对 §12.6 / 上一轮三问的原话：**「按建议同意」** ⇒ 视为对以下三项的终裁：
> ① 管线退休按 §12.2 的 P0→P1→P2 建议执行；② 门1 haircut 分层重裁**进 10/11 议程**（本条即用户签字）；
> ③ T5 终判线 `≥ −$2/笔` **签字采纳**，但按建议附加口径钉（见 §13.3）。
> 未签项：退出改动准入门槛 `双窗口 ≥ +0.3pp/笔` —— 按 §12.4 建议**缓签**，等 T4 第一批双窗口读数出来再定。

### 13.1 ① 管线退休：P0 + P1 已完成（commit `90de5a1c7`）

| 项 | 内容 | 状态 |
|---|---|---|
| P0 | `order_executor._submit_order` 允许清单由 `(PLAYBOOK_SIM, PLAYBOOK_LIVE, ORB_ENGINE)` 收窄为**仅两个 PLAYBOOK 来源** | ✅ 已入库（今天行为零变化） |
| P1 | 删 `app/orb/{__init__,config,engine,exits}.py`（**2735 行**）+ `scheduler_core` 三处接线与 ORB ETF 订阅块 + `risk_stage` ORB 上限闸 + `orb` 配置整节（26 键）+ 台账 P8 行退役标注 + 两处契约测试扫描面同步 | ✅ 已入库 |
| P2 | 主线 `MAIN_PIPELINE` 死枝：**按建议不动**（那些是兜底默认值与标签，删会牵动数十处赋值） | ⏸️ 维持现状 |

**如实标注的两处边界（不得被后续引用成"闸已全封"）**：
1. `order_executor` 那道闸只覆盖**经该路径**的下单；ORB 当年是**直连** `execution_engine.submit_order`（`app/orb/engine.py:2341`，其 `:2276` 注释自述"不受 RiskStateMachine/PDT/cooldown 保护"）⇒ 真正的关闭靠**模块退役**，不靠改闸。
2. **刻意保留**：`source_type` 标签映射（`order_executor:125` / `metrics.NON_PLAYBOOK` / `positions.source.AUTO_ORB`）、`position_monitor:1072` 的 `ORB_ENGINE` 读取面、`cohort_ab` SQL、`position_exit_monitor._is_orb_position` —— 历史行仍需可读，删它们会让老账本读不出归属。

**验证（等基对比，两侧同命令）**：
- `py_compile` 3/3；导入链 `scheduler_core` / `order_executor` / `risk_stage` / `position_monitor` 全通
- YAML：`orb` 节消失、其余 **56 键**完整；`config_validate` 输出**不含** orb 相关项（其 2 项失败为既有，与本次无关）
- 窄选择回归（同基线命令）**337 passed**，与改动前逐条一致
- 宽回归 `tests/{execution,playbook,risk,coordinator}`：基线 **55 failed → 现 45 failed**，差集 **0 条新增失败**（10 条差异为裸 worktree 缺 `data/` 的环境性失败，已逐条列出核对）
- 残留扫描：`app.orb` / `self.orb_engine` / `get_dotted('orb')` / `orb.enabled` 在执行面**零命中**（仅 tombstone 注释）
- 事实核查：ORB 在仓 **0**；最后成交 **2026-06-22**（`ORB_ENGINE` 11 行）
- **Gate 6**：重启前在仓 sim=0 / live=8（均为 `size=0` 陈旧手动行，不受影响）→ `systemctl restart ftseek-us` → `active`（09:20:57 CST）→ 启动冒烟：全线程正常拉起、`ImportError|ModuleNotFoundError` 计数 **0**

### 13.2 ② 门1 haircut 分层重裁 —— 已进议程，**本文件即签字留痕**

**提案（生效时点 = 10/11 议程表决后，在此之前一切读数只作 record-only）**：

| 项 | 现役 | 拟改为 | 依据 |
|---|---|---|---|
| 滑点 haircut | 全池统一 **15 bps/边（往返 30）**，`exit_scorecard.payoff.slip_haircut_bps` | **分层**：盘中 ≈ **5.94**（交叉校 5.37）/ 盘后 ≈ **26.96** / 盘前 待前向攒样 | `data/slippage_calibration_latest.json`（T2 首跑，2026-09-20） |

**为什么必须走议程而不是直接改**：该 haircut 是门1 G1-1/G1-4 的**输入**，直接改等于搬动门柱。折算（名义中位 $5,481）：
`现役 +$5.00 − 30bps = −$11.44/笔`；`盘中按实测 = +$1.74`；`盘后按实测 = −$9.78` ⇒ **同一批 221 笔的 slip-adj 期望随盘中/盘后占比反号**。
**生效条件（三条同时满足）**：① 10/11 议程表决通过；② 盘中读数补到**实际成交标的**口径（现为影子宇宙：`intraday_reentry_shadow` n=1621 / `entry_timing_shadow` n=2422）；③ 盘后口径注明其样本为真实持仓决策点但薄量、价差结构性宽。
**边界声明（不许越位）**：报价价差 = 往返成本**下界**，不含排队/部分成交/冲击/SSR 拒单/券商 STOP 触发质量；盘前臂当前 n=0（代码已写 `bid_price/ask_price`，等新行）。
**回滚**：判据入参为单点（`slip_haircut_bps`）⇒ 恢复 15 即回滚，须留痕。

**复算命令（产物 `data/slippage_calibration_latest.json` 属 runtime 目录不入库 ⇒ 命令必须自含）**：
```bash
cd /opt/ftseek/FTSeek-US
# 盘中（两独立样本）
python3 -c "import json,statistics as st;r=[json.loads(l) for l in open('data/intraday_reentry_shadow.jsonl') if l.strip()];v=sorted(x['spread_bps'] for x in r if isinstance(x.get('spread_bps'),(int,float)));print('n=%d median=%.2f p25=%.2f p75=%.2f'%(len(v),st.median(v),v[len(v)//4],v[3*len(v)//4]))"
python3 -c "import json,statistics as st;r=[json.loads(l) for l in open('data/entry_timing_shadow.jsonl') if l.strip()];v=sorted(m['spread_bps'] for m in (x.get('micro') for x in r) if isinstance(m,dict) and isinstance(m.get('spread_bps'),(int,float)));print('n=%d median=%.2f'%(len(v),st.median(v)))"
# 盘后
python3 -c "import json,statistics as st;r=[json.loads(l) for l in open('data/afterhours_exit_shadow.jsonl') if l.strip()];v=sorted(x['spread_pct']*100 for x in r if isinstance(x.get('spread_pct'),(int,float)));print('n=%d median=%.2f bps'%(len(v),st.median(v)))"
# 盘前（应为 0 行含买卖价 ⇒ 前向攒样）
grep -c '"bid_price"' data/premarket_exit_shadow.jsonl
```
期望读数：`1621 / 5.94`、`2422 / 5.37`、`114 / 26.96`、`0`。

### 13.3 ③ T5 终判线 `≥ −$2/笔` —— 已签，附口径钉

**签字文本（按 §12.4 建议）**：T5 终判采用 `slip-adj 期望 ≥ −$2/笔`（机制级读数），**并同时报出**：分时段实测 haircut、日聚类 `t` 与 `MDE`（`size_normalized.net_bps_day_clustered` 现成键）。
**理由（为什么要那三样一起报）**：`−$2/笔` 是**单一数字门**，而它的分母/输入（haircut、样本窗口、聚类口径）都可由上游选择改变 ⇒ 只报单值就能"用口径把线刷过去"。把三项钉在同一张判据卡上，等于给这条线装上**不可单独搬动的输入**。
**诚实边界（沿用 §八）**：414 笔（口径待统一，见 §11 R-x）测不出 6.59bps 的 edge，需约 4,500 笔量级；本线是机制级判据，不是 P&L 显著性判据。
**未签项**：`双窗口 ≥ +0.3pp/笔` —— 缓签（9 月冻结期只回放不改参数，缓签三周零成本；先看 T4 的数、后定线）。

### 13.4 本轮遗留（按归属分派，均不影响 T1 启动）

| # | 事项 | 归属 | 说明 |
|---|---|---|---|
| L1 | §11 的 R-1/R-2/R-3/R-5/R-6/R-7/R-9 措辞与引用更正落进正文（R-4 已按 F-2 降级） | Codex（执行前） | 含 §五 B 补"延长时段 180 日 n=89"宇宙标注、C6 换 `trade_records` 复跑源、DEB 引用写全名、C12 拥堵分钟改 06:25/06:30、补 `cutover_preflight` 为备刀测量面 |
| L2 | `_dedup_reserve` 的 flock 并发回归钉子 | 任一 agent | 本次代提交暴露：该锁无专测 |
| L3 | `exit_scorecard` 分层读数的落点实现（挂 `size_normalized` 旁，record-only） | 10/11 生效前 | **在判据表决前不改判据输入**，故只落 artifact、不动 scorecard 入参 |
| L4 | `scripts/okr_status.py:71-95` 并列 bps 算式收敛为读 `exit_scorecard` 产物 | 后续 | 防瀑布成为第三处同口径实现（反重复造轮子 ③） |
| L5 | 414 笔口径统一（现 scorecard n=221；414 无出处） | T1 口径对账时 | 功效声明的输入必须同轴，否则"需 4,500 笔"跟着错 |

---

## 十四、Codex 复查回执（对 §十一–§十三 · 2026-09-20 第四轮）

> **复查者**: Codex（本计划 §一–§十 作者；本节只复核 Hermes 增量，身份对调）。
> **方式**: 实跑核验（git / sqlite / JSON 复算），另做两项独立读数；零交易行为改动。

### 14.1 Hermes 增量逐项核验（全部复跑通过）

| # | 声称 | 复跑方式 | 判定 |
|---|---|---|---|
| R-A | commits `0cd6ab9af`（代提交 9/19 批次）与 `90de5a1c7`（ORB 退役）存在且内容相符 | `git log --oneline -8` + `git show --stat` | 成立 |
| R-B | C7 无双重扣减：PLAYBOOK_SIM 528 行 `sim_slippage_bps` 命中 **0** | `SELECT source_type, SUM(metadata_json LIKE '%sim_slippage_bps%')` → 0/528 | 成立（N-2 补充，见 §14.3） |
| R-C | T2 读数：盘中 1621/**5.94**（IQR 3.37–9.31）、交叉校 2422/**5.37**、盘后 114/**26.96**、盘前 bid_price **0** | 逐位复现 | 成立 |
| R-D | ORB 退役：`app/orb/` 不存在；执行面残留仅 3 处 tombstone 注释；允许清单 `order_executor.py:3410` 仅两 PLAYBOOK | `rg` + `ls` | 成立 |
| R-E | 判据未越权：haircut 只进 10/11 议程、`slippage_calibration_latest.json` 只读产物、T5 线签字附口径钉 | 文本核 | 成立 |

### 14.2 新读数 N-1（把 §13.2 的「随占比反号」量化到账）

按 scorecard 同口径（since 2026-08-20、PLAYBOOK 宇宙、`trade_mode != 'live'`、`exit_reason` 非空）实跑：

| entry_session | n | 平均净 pnl |
|---|---|---|
| regular | **215（96.4%）** | +$7.65 |
| premarket | 4（1.8%） | −$162.21 |
| afterhours | 2（0.9%） | +$55.38 |
| （剔除：P3_DUP_CLEANUP 清理行） | 2 | — |

（合计 223 行；scorecard 缓存 n=221，差 2 笔为缓存生成后新增平仓。）

> **更正 R-10（第五轮复查，2026-09-20，Claude Code 实跑）**: 上表为**修正版**。原版
> （regular 213/95.5%/+$6.89、空 4/+$88.57）的分桶把 4 行 `entry_time` 为 **+08:00 偏移格式**
> 的行归入「空」——其中 BULL（`2026-08-20T21:47+08:00` → ET 09:47）与 NOK
> （`2026-09-17T23:04+08:00` → ET 11:04）**实为盘中单**，另 2 行（MRCY/SNDK）是
> `P3_DUP_CLEANUP` 清理行（§15.2 已裁"读数时剔除"，原版将其计入「空」桶且均值分母只算
> 非 NULL 的 2 行，n=4 与分母不一致）。根因 = **`trade_records.entry_time` 同列混存
> `−04:00` 与 `+08:00` 两种时区偏移**（新时钟病灶，已记台账）；修正版按 ET 换算后分桶，
> 清理行独立剔除。**结论方向不变且更强**（盘中占比 95.5%→96.4%）。

**推论**：窗口内 **96.4% 是盘中入场**。若按 §13.2 提案分层（盘中 5.94 / 盘后 26.96 / 盘前待攒样），
混合往返 haircut ≈ 0.964×5.94 + 少量盘前盘后 ≈ **6–7bps**，对应 slip-adj 期望 ≈ **+$4.5/笔**
（现役统一 30bps ⇒ −$11.44/笔）。
⇒ 门1 当前负读数的**主要构成**是统一 30bps 对 95% 盘中仓的过度惩罚——这不是「让数字变好看」，
是判据输入与宇宙结构的错配实锤。
**仍不构成已改判据**：§13.2 三条生效条件（10/11 表决 / 盘中补实际成交标的口径 / 盘前攒样）原样有效；
本读数只把「为什么值得排队表决」算清楚了。

### 14.3 新读数 N-2（T1 验收必须补的口径钉）

- PLAYBOOK_SIM 528 行中 **384 行** `metadata_json` 含 `exit_slippage_bps`（实测退出偏差）；
  窗口内 215 行：中位 **0.00bps**、均值 **−2.12bps**、IQR [−4.41, +2.50]。
- 若 `pnl_net` 的成交价已含这层实测偏差（需 T1 定谳），则分层 haircut 落地时必须声明
  「quote-spread haircut 只扣**未建模**部分」——否则会造出**新的双重扣减**（量级 ~2–4bps，小但必须钉死）。
- **处置：并入 T1 验收条款**——对账结论必须同时回答三件事：
  ① `pnl_net` 已含哪些执行偏差（入场/出场/两腿）；
  ② 分层 haircut 应扣的**剩余**部分是多少；
  ③ 修正后的口径写进 §13.2 提案，10/11 表决时一并呈交。

### 14.4 复查结论

- **§十一–§十三 合格**：数字逐位复现、执行留痕完整、边界声明诚实、判据零越权。
- **执行就绪度**：T1 可启动。前置仅剩两项——
  1. §13.4-L1 措辞/引用更正（Codex 自留作业，doc-only）；
  2. N-2 并入 T1 验收（本节已写入，执行时照此验收）。
- **共识闭环**：Hermes 回执（改后执行）→ 用户终裁（按建议同意）→ 本复查回执（合格）。
  按 §十 交接条款，Codex 下一步 = **L1 更正 → T1 口径对账（含 N-2）→ T2 盘前前向攒样监控**。

---

## 十五、T1 口径对账执行记录（2026-09-20 · Codex，纯只读）

> L1 措辞更正（§13.4-L1：R-1/R-2/R-3/R-5/R-6/R-7/R-9，R-4 已按 F-2 降级）已同步落进
> §四/§五/§七/§十与附录；本节为 T1 执行留痕。零交易行为改动。

### 15.1 定谳结论（N-2 三问三答，全部实跑）

**① `pnl_net` 的成交价里已含哪些执行偏差？**

代码链（逐环锚点）：
- `pnl_net = realized_pnl − fees_est`（`app/playbook/pipeline.py:6717-6722`，注释自述
  「2026-07-30 DEB-fee-net-pnl」；日级不变量 I8 强制 ±0.02）；
- `realized_pnl` 两腿均为券商真实回报成交价：入场 = `filled_price`
  （`playbook_service.py:1607-1620`），出场 = close result 成交价（`pipeline.py:6690-6710`）；
- engine 7bps/边模拟滑点在 PLAYBOOK 宇宙 0/528 命中（C7，已复核）⇒ **无双扣**；
- **已含的执行偏差只有一层**：决策→成交的时延漂移——`exit_slippage_bps` 是它的量化
  （`pipeline.py:7828-7832`：`(reference − fill)/reference`，reference = 决策时点
  `quote.last`，`pipeline.py:8127`），**它是测量、不是费用**，天然已在 `realized_pnl` 里。

实测（窗口内 215 行带 `exit_slippage_bps`）：中位 **0.00bps**、均值 −2.12、IQR [−4.41, +2.50]；
按 `exit_price_source` 分组：`broker_deal` 151 行均值 **+0.33**（≈零不利偏差）、
`broker_weighted_deal` 33 行 −5.11、`broker_limit_deal` 31 行 −10.88（限价等更优价，方向合理）。
⇒ **Futu SIM 撮合 ≈ 决策时点 last 价，无系统性价差穿越。**

**② 分层 haircut 应扣的剩余部分是多少？**

- **未实现的报价价差**（SIM 没付、live 要付）：盘中实测 5.94bps 往返（quote-spread，下界）；
- **SIM 无法建模的排队/部分成交/冲击**：报价测不到，live-only（门2 职责）；
- **已含的决策→成交漂移（IQR ±4bps）禁止重复扣**。

**③ 修正后的口径写进 §13.2 提案（见 §15.3）。**

### 15.2 统一「净盈利」SSOT（判分面唯一口径）

```
净盈利 = realized_pnl − fees_est − 分层滑点折算
分层滑点折算 = 未实现报价价差（分时段实测，声明为下界）
             + 排队/冲击（不可测，显式声明缺席）
单位: bps（1bp = 0.01%）与 $ 双报；realized_pnl 已含决策→成交漂移，禁止二次扣减
```

- 现役 scorecard `COALESCE(pnl_net, realized_pnl)` 保留；窗口内 223 行 I8 违例 **0**，
  `pnl_net` NULL 仅 2 行（`P3_DUP_CLEANUP` 清理行，非真实成交，读数时剔除）。

### 15.3 对 §13.2 提案的一处精确化

- 分层 haircut 定义从「分时段实测值」精确为「**未实现**报价价差 + 声明缺席的排队/冲击」；
- 由于 SIM 撮合 ≈ last 价（无价差穿越），**quote-spread 不构成双重扣减**——N-2 的担忧
  被边界化到 ±4bps 漂移层，已由「禁止二次扣减」条款钉死；
- 盘中 5.94 仍是**下界**：30bps 与 5.94 之间约 24bps 的差，其中多少属于排队/冲击不可知
  ——这正是 10/11 表决时要摆在桌上的诚实边界（不是「改小 haircut」，是「拆开 haircut」）。

### 15.4 T2 盘前前向攒样监控（现状确认）

- `premarket_exit_shadow.py` 由 `extended_hours_exit_cron.py`（每分钟、会话门控）驱动，
  在册且 enabled（`CONTROL-LOOP-LEDGER.md` crontab-ext-exit-cron 行）；
- 代码已写 `bid_price/ask_price`（F-2）⇒ **下个交易日盘前窗（ET 04:00–09:25）自动开始攒样**，
  无需新增开发；攒样至 n≥30 后并入 §13.2 提案。

### 15.5 复算命令

```bash
cd /opt/ftseek/FTSeek-US
# I8 不变量 + NULL 行识别（期望: 223 / 0 违例 / 2 清理行）
sqlite3 data/ftseek_us.db "SELECT COUNT(*), SUM(CASE WHEN ABS(COALESCE(pnl_net,realized_pnl)-(realized_pnl-COALESCE(fees_est,0)))>0.02 THEN 1 ELSE 0 END), SUM(CASE WHEN pnl_net IS NULL THEN 1 ELSE 0 END) FROM trade_records WHERE trade_date>='2026-08-20' AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE','PLAYBOOK_SHADOW','MANUAL_PLAYBOOK') AND trade_mode!='live' AND exit_reason IS NOT NULL AND exit_reason!='';"
# exit_slippage 分布（期望: 215 行 / 中位 0.00 / IQR −4.41..+2.50）
python3 -c "import sqlite3,json,statistics;c=sqlite3.connect('data/ftseek_us.db');v=[json.loads(r[0])['exit_slippage_bps'] for r in c.execute(\"SELECT metadata_json FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND trade_date>='2026-08-20' AND metadata_json LIKE '%exit_slippage_bps%'\")];v=[x for x in v if isinstance(x,(int,float))];s=sorted(v);print(len(v),statistics.median(v),s[len(v)//4],s[3*len(v)//4])"
# 按 price_source 分组（期望: broker_deal 151/+0.33, weighted 33/−5.11, limit 31/−10.88）
sqlite3 data/ftseek_us.db "SELECT json_extract(metadata_json,'$.exit_price_source'),COUNT(*),ROUND(AVG(json_extract(metadata_json,'$.exit_slippage_bps')),2) FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND trade_date>='2026-08-20' AND metadata_json LIKE '%exit_slippage_bps%' GROUP BY 1;"
```

---

## 十六、T3 费用路径 C 前置回放执行记录（2026-09-20 · Codex，纯只读）

### 16.1 回放设定

- **宇宙**：PLAYBOOK 四类、`trade_mode != 'live'`、已平仓、剔除 2 行 `P3_DUP_CLEANUP`；
- **窗口**：
  - 全窗 2026-06-22→09-18（n=549）——**只作参考，不作裁决**：8/6 sizing 抬升使名义额与时间
    强混杂（全窗分半检验中，前半段 thr=4k 的保留单反而比被砍单更差 ⇒ 全窗结论不可用）；
  - **裁决窗 = 现行 regime（since 2026-08-06）**：n=306、31 交易日、~49 笔/周；
  - 180 日窗口不存在（playbook 首笔 2026-06-22）⇒ 以 **regime 内时间对半切分**替代双窗口；
- **门槛变量**：名义额（`entry_price×quantity`）/ 入场价 / 费用率（`fees_est/notional`）。

### 16.2 主结果（现行 regime，基线 exp +$0.22、WR 65.4%、fee 13.24bps）

| 门 | kept | n 损失 | exp_kept | fee_bps | WR_kept vs WR_cut | 分半稳定性 |
|---|---|---|---|---|---|---|
| 基线 | 306 | 0% | +$0.22 | 13.24 | — | — |
| 名义 ≥ **$9,950**（费用诊断建议值） | **0** | **100%** | —（无单可留） | — | — | ✗ regime 窗内**零幸存**（更正 R-11，见下注） |
| 名义 ≥ $5,000 | 250 | 18.3% | +$2.78 | **8.92** | 65.2% ≈ 66.1% | ✓ 双半同向 |
| **价格 ≥ $1 + 名义 ≥ $4,000** | 288 | **5.9%** | **+$2.87** | 9.04 | 66.0% > 55.6% | ✓ 双半同向 |
| 费用率 > 10bps 拦截 | 258 | 15.7% | +$1.88 | — | 65.9% vs 62.5% | ✗ 分半翻转（前半被拦单 exp **+22.62** 反而更好） |

> **更正 R-11（第五轮复查，2026-09-20，Claude Code 实跑）**: `$9,950` 行原版读数
> （kept 7 / 98.7% / −$282.29 / fee 2.34 / WR 28.6% vs 55.9%）经复跑证实是**全窗（n=549）**
> 数字——那 7 笔全部是 6–7 月的大名义单（TSM/PG/LMT/XOM/ORCL/UNH/MSFT，$12k–$30k），
> 混入了声明为「现行 regime（n=306）」的本表，违反 §16.1 自家规则「全窗只作参考不作裁决」
> （且原行内部不自洽：7/306 ⇒ 97.7%≠98.7%）。**regime 窗内实测 kept=0**：8/6 sizing
> 变更后没有任何一笔名义额达到 $9,950 ⇒ 「一票否决」结论在裁决窗口径下**更强**
> （该门在现行 regime 等于关停全部交易）。表内其余四行 regime 数字逐位复现无误。

组合门分半读数（价格≥$1 + 名义≥$4,000）：前半 kept **+5.29** vs cut **−16.60**；
后半 kept **+0.39** vs cut **−58.44** —— 方向两半一致。

### 16.3 根因定谳（比费用诊断更深一层）

**真怪物是交收费 $0.003/股，不是 $1.99 佣金地板**：

| 标的 | 入场价 | 股数 | 交收费/价 | 往返费用 | 费用率 | pnl |
|---|---|---|---|---|---|---|
| US.CTNT | $0.072 | 68,493 | **417bps/边** | $451.26 | 915.1bps | **−$314.27** |
| US.GIPR | $0.431 | 11,494 | 70bps/边 | $100.86 | 203.6bps | **−$181.32** |

- 佣金 0.3% 上限正常生效（`fee_model.py:95-96` 先 cap 后 floor）——但**交收费无上限**，
  「低股价 × 大股数」是结构性毒；
- $1.99 地板只影响小名义单（<4k 桶 fee 20.6bps），量级温和；
- 费用诊断的 $9,950 建议值只算了佣金地板、没看成交分布——**回放证伪**。

### 16.4 裁决建议（进前向影子，10/11 定门槛）

1. **必上：价格门 `entry_price ≥ $1`**——杀 settlement 怪物（n 损失 0.7%，exp +0.22→+1.85，
   fee 13.24→9.65；机制先验成立：settlement/price）；
2. **候选：名义门 $4,000 或 $5,000**（4k 组合：exp +2.87 / fee 9.04 / n 损失 5.9%；
   5k：exp +2.78 / fee 8.92 / n 损失 18.3%）——影子期用真实决策点数据定；
3. **不上：费用率门**（分半不稳定，且会误杀 BULL/NOK 赢家 +$80~+$178）；
4. **$9,950 一票否决**。

周净估计：4k 组合 ≈ **+$133/周** vs 基线 +$11/周（×12；诚实声明：仍远离 $1,000/周，
与阶梯判断一致——费用门是止血，不是 edge）。

### 16.5 诚实边界

1. 过滤历史 ≠ 因果保证（门槛改的是未来入场分布）；
2. 被拦单释放的资金/槽位（二阶效应）未建模；
3. 本回放已扫多个门槛——**最优点不可直接当判据**，防回放过拟合：最终阈值必须由
   2026-09-21–10/11 影子期的真实决策点数据裁决；
4. 8/6 sizing 变更本身是混杂；现行 regime 分析已控制。

### 16.6 复算命令

```bash
cd /opt/ftseek/FTSeek-US
python3 - <<'PY'
import sqlite3, statistics as st
conn = sqlite3.connect('data/ftseek_us.db'); conn.row_factory = sqlite3.Row
rows = conn.execute("""
SELECT trade_date, symbol, entry_price, entry_price*quantity AS notional,
       COALESCE(pnl_net, realized_pnl) AS pnl, fees_est,
       fees_est/(entry_price*quantity)*10000 AS fee_bps
FROM trade_records
WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE','PLAYBOOK_SHADOW','MANUAL_PLAYBOOK')
  AND trade_mode != 'live' AND exit_reason IS NOT NULL AND exit_reason != ''
  AND pnl_net IS NOT NULL AND exit_reason != 'P3_DUP_CLEANUP'
  AND trade_date >= '2026-08-06' ORDER BY trade_date, exit_time""").fetchall()
n=len(rows); weeks=len(set(r['trade_date'] for r in rows))/5.0
def report(label, pred):
    kept=[r for r in rows if pred(r)]; cut=[r for r in rows if not pred(r)]
    wk=st.mean(r['pnl'] for r in kept); fc=st.mean(r['fees_est']/r['notional']*10000 for r in kept)
    print(f"{label:22s} kept={len(kept):3d} cut={len(cut):3d} loss={100*len(cut)/n:5.1f}% "
          f"exp={wk:+7.2f} fee={fc:6.2f}")
report("baseline", lambda r: True)
report("price>=1", lambda r: r['entry_price']>=1)
report("notional>=5000", lambda r: r['notional']>=5000)
report("price>=1+not>=4000", lambda r: r['entry_price']>=1 and r['notional']>=4000)
PY
```

## 十七、T3 影子挂环执行记录（2026-09-20 · Codex，record-only）

### 17.1 落地面

- **采集器**：`scripts/fee_gate_shadow.py`（只读 DB，`trade_id=trade_records.id`；
  幂等按 `distinct:trade_id` 去重追加）；
- **数据**：`data/fee_gate_shadow.jsonl`，前向汇总 `data/fee_gate_shadow_latest.json`；
- **注册**：`app/playbook/shadow_registry.py::fee_gate_shadow`，`min_n=30`，
  `count_mode=distinct:trade_id`，`intent=fee_floor_cost_guard`；
- **挂载**：cron `45 6 * * 2-6`（避开 06:25/06:30 拥堵分钟）；
- **台账**：`docs/CONTROL-LOOP-LEDGER.md::crontab-fee-gate-shadow`。

### 17.2 采集与判读口径

- **宇宙**：四类 PLAYBOOK、`trade_mode != 'live'`、已平仓、剔除 `P3_DUP_CLEANUP`、
  `trade_date >= 2026-09-20`；
- **收益口径**：逐笔 `pnl_net = realized_pnl − fees_est`，不混毛收益；
- **候选门**：`price < $1`、`notional < $4,000`、`notional < $5,000`、
  组合门 `price < $1 or notional < $4,000`；
- **汇总**：各门 blocked/kept 的 n、净期望、胜率、加权费用 bps 与 n-loss；
- **裁决**：样本不足 30 禁提转正；$9,950 维持一票否决；最终阈值须与双窗口回放同看。

### 17.3 行为边界

- **零交易行为改动**：不进 `app/push/`、不下单、不改配置、不改参数；
- scripts/ 侧为纯新增；`shadow_registry.py` 注册项需按军规重启并跑启动冒烟；
- 首次实跑会产出 n=0 汇总；9/21 起（下一交易日）由 cron 自动攒样。

---

## 十八、第五轮独立复查回执（对 Codex §十四–§十七 落地批 · 2026-09-20 · Claude Code）

> **方式**: 全部关键声称由复查者本人实跑复核（sqlite / grep / crontab / systemctl / pytest），不采信自报告；沿用 §15-C15 常设纪律（搜作者未列出的面）。
> **总裁决**: **工程面合格，影子挂环可直接投产攒样；判读面 4 处口径问题** —— 均已就地修正（R-10..R-13），**结论方向全部不受影响**（$9,950 否决在 regime 口径下更强、盘中占比更高、6 笔簇仍在）。

### 18.1 复跑通过项（逐位一致）

| 项 | 复核结果 |
|---|---|
| §15 T1 全部读数 | I8 不变量 **223/0/2** ✓；exit_slippage **384 行全史 / 窗口 215 行 / 中位 0.00 / IQR −4.41..+2.50** ✓；price_source 分组 **broker_deal 151/+0.33、weighted 33/−5.11、limit 31/−10.88** ✓ |
| §15 代码锚点 | `pipeline.py:6717-6722`（pnl_net=毛−费）、`:7828-7832`（(reference−fill)/reference×10⁴ 含方向修正）、`:8127`（reference=quote.last）、`premarket_exit_shadow.py:447-448`（bid/ask 已写）全部属实 ✓ |
| §16 回放主表 | regime n=306、基线 +0.22/13.24/WR65.4、价格门 0.7%/+1.85/9.65、5k 门 18.3%/+2.78/8.92、组合门 5.9%/+2.87/9.04、费用率门 15.7%/+1.88/65.9vs62.5 —— **除 $9,950 行外逐位复现** ✓；周净 +10.9≈+$11 ✓；4k 组合 +$133/周算术核对 ✓（2.87×288/6.2） |
| §16.3 根因两行 | CTNT 915.1bps/−$314.27、GIPR 203.6bps/−$181.32 直查一致；交收费无上限机制核对（$0.003×68,493=$205.48/边=417bps）✓ |
| 影子脚本 | `mode=ro` URI 只读、**零 futu 依赖**（守"非交易路径不抢 OpenD 槽位"纪律）、幂等 distinct:trade_id、tmp+replace 原子写、UTC 时钟、sort_keys 惯例；**测试 5/5 通过** ✓ |
| 挂环三件套 | crontab 299 行 `45 6 * * 2-6`（PYTHONPATH + timeout 300 + 日志）✓；06:45 无碰撞、06:25=4 作业/06:30=3/06:35=0 与 R-7 一致 ✓；注册契约合法（`distinct:trade_id` 有既有先例）✓ |
| 重启时序 | 服务 ActiveEnter 10:15:22 **晚于** registry mtime 10:11:38 ⇒ 哨兵已生效；首跑产物 `fee_gate_shadow.jsonl`(0B) + `fee_gate_shadow_latest.json`(n=0, schema 正常) 与 §17.3 声明一致 ✓ |
| 过滤安全 | PLAYBOOK 宇宙 `trade_mode IS NULL` = **0 行** ⇒ `!= 'live'` 当前无静默丢行 ✓ |

### 18.2 更正（R-10..R-13，均已就地落档）

- **R-10（§14.2 N-1 分桶伪影）**: 原「空」桶 4 行 = `entry_time` 为 +08:00 格式的行；按 ET 换算 BULL/NOK 实为盘中单、MRCY/SNDK 为清理行。修正后 **regular 215（96.4%）/+$7.65**（原 213/95.5%/+$6.89），结论更强。
- **R-11（§16.2 $9,950 行窗口污染）**: 原行读数（7/98.7%/−$282.29）是**全窗 n=549** 数字，regime 窗内 **kept=0（损失 100%）**；且原行内部不自洽（7/306⇒97.7%≠98.7%）。违反 §16.1「全窗只作参考」自家规则。否决结论在裁决窗口径下更强（该门=关停全部交易）。
- **R-12（C6/R-1 复跑命令缺窗口）**: 见 §11.2 R-1 补充与 §10.1 C6 行，均已加 `trade_date BETWEEN '2026-09-16' AND '2026-09-18'`。
- **R-13（注册表描述措辞）**: `trade_date` 实测 = **入场日**（306/306 等于 entry 日、仅 270/306 等于 exit 日）；过滤器按入场日选样对"入场门反事实"是**正确设计**，但 registry 描述「新平仓」应改「入场且已平仓」。已改 `shadow_registry.py` 描述，并给 `fee_gate_shadow.py` 补 excluded-rows 诊断日志（静默跳过的 NULL pnl/fees 行计数，当前为 0）。

### 18.3 新事实（超出 Codex 自报面）

1. **`trade_records.entry_time` 同列混存 `−04:00` 与 `+08:00` 两种时区偏移**（实测 id 1320/1511/1513/1536 为 +08:00，其余为 −04:00）——R-10 的根因，**新时钟病灶变种**，已记台账（**写入侧 2026-09-20 已拆弹，见 §19.6**）。任何按 entry_time 分时段/分时区的下游必须先做 ET 归一，禁止裸取 wall-clock。
2. **handoff.md 的 RFC-089「四判据变五判据」条目在工作区被删**（该内容属已提交 `e34423ae4`，全文无迁移痕迹，删除使周检口径印象回退到旧四判据）——已恢复；若属有意精简，需用户裁决后再删。

### 18.4 遗留（不阻塞攒样，10/11 前处理）

- 影子 jsonl 追加无 flock（cron 单实例 + 哨兵按 trade_id 去重兜底，风险可接受）；
- 影子行在追加时刻冻结（后续 fees_est 修正不回写）——对 record-only 语义可接受，判读时知悉；
- 盘前臂 bid/ask 攒样自 9/21（周一）盘前窗自动开始（§15.4 已核，零开发）。

## 十九、T3 → 系统化复盘 / 精准推送接入（2026-09-20 · Codex）

### 19.1 关联逻辑

- **诊断总线**：`trade_records` → `fee_gate_shadow.py` → `fee_gate_shadow_report.py` →
  PushHub / 周六周检；
- **净利口径**：所有推送只读 `pnl_net`，不允许用毛收益刷结果；
- **现行门对照**：生产门已是 `price≥$5 & notional≥$2,500`
  (`config/unified_config.yaml::playbook.admission`)；
- **安全增量语义**：候选不是“把价格门从 $5 放宽到 $1”，而是
  **保持现行价格门，只测试名义额 $2,500→$4,000/$5,000 两档**；
- **链路诊断**：低价绕行 → admission/enforcement；名义额不足 →
  `admission.min_notional_usd`；费用不再是主漏损 → 转向退出/信号质量。

### 19.2 推送治理

- **每日**：06:47 采集后运行，经 PushHub 推 `SHADOW_COMPARE`；
- **触发**：新样本、状态跃迁、断流/坏行、现行门绕过、候选门会放宽现行门；
  无新样本且状态不变时不推，避免噪声；
- **周检**：周六周检并列 T3 摘要，与经济边际、影子注册表同场呈报；
- **级别**：链路/数据质量/安全风险用 HIGH；普通攒样进度用 NORMAL；
- **禁止自动生效**：报告最多给 `READY_TO_EVALUATE` / 诊断方向，参数变更仍等 10/11 用户终裁。

### 19.3 回滚

- 删除 cron `47 6 * * 2-6` 与台账行；
- 删除 `scripts/fee_gate_shadow_report.py`、推送状态文件和本节；
- 周检删除 `check_fee_gate_shadow()` 调用；采集器本身无需回滚。

### 19.4 独立核查回执（2026-09-20 · 第 6 轮，用户令「请核查」；纪律同 §18：全量独立重跑）

**复跑通过项**：

- 测试：`test_fee_gate_shadow.py` + `test_fee_gate_shadow_report.py` **13/13 绿**（0.44s）；
- 干跑（无 `--push`）：现行门读数 `price≥$5 & notional≥$2500 (active)` 与 `unified_config.yaml::playbook.admission` 实值一致；状态 `INSUFFICIENT_N`（n=0/30，excluded=0），方向「继续攒样，不改参数」✓；
- **R-13 端到端接线成立**：采集器 `count_excluded_rows` → `summarize(excluded_rows=…)` → `fee_gate_shadow_latest.json` → 报告 `DATA_QUALITY_WARNING` 状态位 → 周检 `excluded=` 显示；NULL pnl/fees 静默跳行从此可观测；
- 采集脚本实跑（11:09 版）`total=0 appended=0 excluded=0`，只读（`mode=ro`）、幂等 ✓；
- 周检消费端 `check_fee_gate_shadow()` 实测返回 `{status,n,min_n,excluded_rows,lines}`，`lines[1:3]` 切片=样本行+现行门行 ✓；周检脚本 stdlib-only，报告脚本依赖=yaml+app.push，**全链无 futu/OpenD**（干跑实证）✓；
- 台账两行（06:45 采集 / 06:47 报告）与实现一致，风险列含「候选 price≥$1 误读为放宽现行 $5 门」告警，与 §19.1 安全增量语义互为印证 ✓；
- `should_push` 语义与 §19.2 逐条对应：n 变或状态变才推；URGENT 集（断链/断流/门未执法/门绕过/数据质量/候选放宽现行门）跃迁即推 HIGH ✓；
- cron 06:47 无拥塞（06:41/06:44/06:45/06:47 各 1 行，错峰成立）；下次触发周二 06:47 CST=周一美股收盘后 ✓；
- `_entry_session` 用 `fromisoformat`+`astimezone(ET)`，对 `entry_time` 混存 +08:00（时钟台账变种 4）免疫 ✓。

**R-14（= DEB S-1 复发，已结案：用户确认收到 2026-09-20）**：验证路径**又真实推送了一次**。证据：`data/fee_gate_shadow_push_state.json` 实录 `last_pushed_at=2026-09-20T03:15:26+00:00`（11:15:26 CST）、`last_push_key=fee_gate_shadow:0:INSUFFICIENT_N`——状态文件仅在 `pushed=True` 时落盘，故推送已成功送达通道，用户端应收到一条「T3 费用门影子诊断 · 样本 0/30」NORMAL 级通知，~~请按 S-1 同流程确认收到~~ **用户已确认收到（2026-09-20，S-1 同流程结案）**。副作用评估：state 被预置 ⇒ 首次 cron 推送顺延至首个 n/状态变化（预期周二 06:47），语义无损；但 DEB §25.2 S-1 的教训（验证路径与生产状态文件未隔离）在新脚本上原样复发——脚本的无 `--push` 模式即天然干跑（打印且不落 state），本次是直接带 `--push` 验证所致。改进建议（P5 前）：`--push` 路径加显式确认旗标，或验证时用 `--state /tmp/...` 隔离。

**R-15（ops-sync SSOT 红灯，已修复）**：repo `ops/hermes/ftseek_weekly_checkpoint.py` 领先线上 `/root/.hermes/scripts/` 副本 **+30 行**（即 §19 的 `check_fee_gate_shadow()` + main 消费段），`tests/test_ops_scripts_sync.py::test_repo_and_live_scripts_in_sync` **FAILED**（本轮 114 项超集回归唯一红）。影响：今日 10:00「周日综合复盘」用旧线上副本执行（无 T3 段，损失限一周展示）；下次周六周检前须部署。修复=单向 `cp` repo→live + 复跑该测试至绿。**已修复（2026-09-20，用户授权）：cp 部署一致，`test_ops_scripts_sync` 3/3 复跑绿**；下次周六周检（9/26）起线上副本含 T3 段。

**R-16（小注记，不阻塞）**：① push 无 flock——state 文件守卫 + `sync=True` 绕过 hub 去重，cron 每日单实例风险低，P5 建议对齐 review_bundle 的 flock+push.json 模式；② `from scripts.fee_gate_shadow import _arm_stats` 跨脚本引用私有函数（签名变更会双爆，建议转正为公共 API 或内联）；③ 攒样期每日晨推为 §19.2 明示设计（n 每日变⇒每日推），用户应有「每天一条 T3 通知」的预期，若嫌噪可把非 URGENT 触发从「n 变」收紧为「n 跨过 min_n 或周级聚合」。

**核查副产物说明**：本轮复跑覆盖/新增了 `data/reviews/` 下 9/18 三产物与 `coverage_2026-09-20.json`（均为可再生日常产物，未推送、未写 vault）；bundle hash 跨世界态不稳的发现与 P5 golden 约束记录于 DEB §29.2 N-9。

### 19.5 R-16 后续执行（2026-09-20，用户令「继续」）

- **R-16① 已清（flock 守卫）**：推送路径加 `flock` + **锁内重读 state**（对齐 review_bundle `_push_once` 模式），`LOCK_PATH=data/fee_gate_shadow_push.lock`；新增竞态测试 `test_push_guard_rereads_state_under_lock`（模拟锁外陈旧读取→锁内重读挡住双推）。插曲：旧 push 测试因默认 lock 路径落生产 `data/` 被 **conftest 生产数据写守卫当场拦截**（REVIEW-20260903 §四.8 机制实证生效），按守卫指路修法 (a) 参数化传 `tmp_path` 修复；生产目录零泄漏（lock 文件实测不存在，仅真实 `--push` 时创建）。
- **R-16② 已清（私有引用转正）**：`_arm_stats` → 公共 `arm_stats`（采集器/报告两脚本 14 处引用同步更新，消除跨脚本私有导入）。
- **R-16③ 不动**：每日晨推维持 §19.2 明示设计；降噪选项（非 URGENT 触发改为 n 跨 min_n 或周级聚合）留待用户偏好，不擅自更改。
- **验证**：`tests/test_fee_gate_shadow*.py` 14/14 绿（5 采集 + 9 报告含新竞态测试）+ `tests/review` 31/31 绿 + `test_ops_scripts_sync` 3/3 绿；报告干跑读数不变（现行门 $5/$2500 active、INSUFFICIENT_N、excluded=0）。

### 19.6 变种 4 写入侧拆弹（2026-09-20，用户令「继续」）

§18.3-1（entry_time 混存 +08:00）写入侧溯源收口。全仓扫 `INSERT INTO trade_records` 共 7 点，逐点定时钟来源：

- **真凶实锤**：`position_sync.py::_backfill_playbook_trade_record`（P3 sync_recovery_backfill）原 `datetime.now().astimezone()` = 服务器 CST。DB 取证：4 行 +08:00（id 1320/1511/1513/1536）**全部** `entry_reason='sync_recovery_backfill'`、trace 前缀 `sync-`/`playbook-reconcile-`；同路径 7 月更早形态为 naive CST（id 1092/1094/1095）且 `trade_date` 跨日 +1 错位（违反「trade_date=入场 ET 日」不变量）。修复 → `now_et()`（兜底 `ZoneInfo("America/New_York")`，不回退服务器本地钟）；`trade_date=now_iso[:10]` 随之恢复 ET 日。
- **同族 #1**：`audit.py:443` 兜底 `entry_time = now(naive CST)`，与同函数 R-12 注释（aware-ET 入账本列）自相矛盾 → `et_now`。
- **同族 #2（T4 钩子局部拆弹）**：`audit.py:311` 将 `order.timestamp`（naive 北京，executor.py:253 约定）原样写 entry_time → 经 `_earliest_ledger_iso`（`parse_ledger_ts` naive=CST 契约）归一 aware-ET，兜底 `et_now`。手动单路径休眠 ⇒ 生产零风险。
- **同族 #3**：`position_sync.py:1046` `_oexit` naive CST——`orphan_autoclose_enabled=true` 回滚路径下写 `trade_records.exit_time`（台账「exit_time 327 naive」生产者之一）→ `now_et()`。
- **余点排查免改**：`trade_recorder.py:199`（主路径 now_et ✓）、`extended_hours_entry_executor.py:1239`（`_now_et` ✓）、`trade_store.py:750`（passthrough，尺度随上游）、`position_sync.py:103`（`upsert_external_close_trade_record` 全仓无调用方，死路径）、`demix2_broker_verified.py`（一次性迁移搬行，不造时钟）。`open_positions` 写点（1246/1396/2007 等）naive-CST 属已登记约定（消费端 `_to_et_ghost` naive=北京归一 + updated_at 混存三尺度已在册），不动。
- **既有红灯同车修**：`test_external_close_book_reconciliation` fixture 缺 `config_version` 列（helper INSERT 已含该列；生产靠历史 ALTER，`TradeStore._init_db:403` 走迁移）→ fixture 补列。**stash 实证**该红灯在干净 HEAD 上同样复现，与本批改动无关。
- **验证**：新增 `tests/test_position_sync_backfill_clock.py` 3/3 绿（ET 偏移断言 + trade_date=ET 日不变量 + 防重幂等 + 源码契约「不得再现 `datetime.now().astimezone().isoformat()`」）；相邻套件 133/133 绿（含 ratchet/ledger-scale/ops-sync）+ audit 覆盖套件 27/27 绿；import 健全。
- **历史坏行不回改**（留痕纪律）：7 行（4 aware-CST + 3 naive-July）保持原样，读侧 ET 归一纪律（§18.3-1 下游规则）已覆盖；时钟台账变种 4 更新为**已拆弹**。
