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
> - 双窗口采信纪律（DEB-20260919：60 日结论已被 180 日改写三例）
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
副轨「备刀」：RFC-090 P0-1..P0-6 + DEB-20260914 D-A..D-D（无 deadline，永不阻塞主轨）
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
     （`app/execution/engine.py:171`，7bps/边）只被 ExecutionEngine 调用，
     而 Playbook 是唯一下单管线 → **playbook 成交价大概率是 Futu SIM 零滑点撮合价，
     30bps haircut 是唯一滑点计费，无双重扣减**。此前「双重扣减」怀疑按更正留痕纪律记录，T1 定谳。
   - 定谳方法：抽查 `trade_records` 中 PLAYBOOK_SIM 行的 `metadata.sim_slippage_bps`
     存在性 + 成交价 vs 决策时点盘口的对拍。
2. 统一「净盈利」SSOT：**净 = 毛收益 − 费用 − 滑点（分时段标定值）**，
   bps 与美元双口径，单位声明入契约测试（军规：新字段必带量纲）。
3. 若发现漏扣/双扣 → 只修**判分面**（scorecard/瀑布产物），不碰交易行为。

**验收**：一页对账结论（含证据截图/SQL 读数），写入本文件附录；净盈利口径全仓唯一。

### T2 · 滑点标定（W1 启动，W3 收口）——「滑点标定没有重点关注过，按建议来」

**任务**：把门1那个独自决定生死的 15bps/边 假设，换成**分时段实测**。

**数据源（全部已存在）**：
- 60s tick 可成交价序列（回放器已在用）；
- Futu `pre_high_price` / `after_high_price` 时段高点字段；
- `premarket_exit_shadow.jsonl` / `afterhours_exit_shadow.jsonl`；
- 6 笔 `SL_TRAIL_LOCK` SIM 成交（ACN/INTC/QCOM/AMD/UMC/WBD）：价差+轮询延迟可测
  （ACN 实测：过冲 0.04% + 价差 0.13%），**但 SIM 撮合无排队/部分成交 → 只能当标定下界**；
- 盘中：交易宇宙的 LV1 报价抽样。

**产出**：`data/slippage_calibration_latest.json`——分「盘前 / 盘中 / 盘后」三层的
往返成本中位数 + IQR，及各层建议 haircut。

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

**落地方式（守 C4 影子先行）**：10/1 起门槛先**影子 1–2 周**（只记录不拦）→
10/11 用影子数据 + 双窗口回放做真门槛裁决。

**验收（判定卡 T3）**：`fee_bps` 14.45 → **≤10**；n 损失已量化且用户明示接受
（C 与「样本量优先」存在张力，n 损失数字必须摆在裁决桌上）。

### T4 · 退出回放（W2）

**任务**：用 9/19 刚扩完的回放器，按时段/路径分层裁决退出侧。

**纪律**：
- **双窗口同向 ≥ +0.3pp/笔** 的改动才准提生产，否则维持现状（沿用 9/19 纪律）；
- **分层判，不许混**：延长时段 180 日「rule 差于什么都不做 +1.07pp」与全链
  exit_scorecard「executed WR 79% vs held 49%」方向相反——不同宇宙，各自裁决；
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
| B | 「从不见肉」笔（MFE<1.5%）仍是最大负贡献（当前 31% 笔数贡献 −117pp） | **入场侧**，退出侧停止调参 | 候选：时段量 ≥20M 预筛（影子已在跑，n=34 小样本）/ negative-band 拦截 |
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
- **DEB-20260914 D-A**（capital_state 按臂读取，回切致命，P0）+ D-B/D-C/D-D；
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

- [ ] **共识门**：Hermes 审核回执（§10.4 契约）+ 用户终裁「可执行」
- [ ] T1 口径对账结论落档（含 sim_slippage_bps 存在性抽查 + 成交价对拍）
- [ ] T2 `slippage_calibration_latest.json` 三时段读数 + 偏差 >5bps 时提请门1重裁
- [ ] T3 双窗口回放 + 10/1 影子上线 + n-loss 量化
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
| C2 | 「毛期望=0 已证、440 候选 / 1,813 信号全零」 | `docs/OKR-2026-09.md` KR3 诚实先验 + DEB-20260823 三判决 | 引用断章或数字漂移 |
| C3 | 「31% 从不见肉笔贡献 −117pp」 | DEB-20260919 §12（180 日 n=89） | **口径只覆盖延长时段子集**——若被引用成全链结论即失真，须标注宇宙边界 |
| C4 | $1.99 地板 100% 触发 / ≤2bp 需 ≥$9,950 名义 | `docs/analysis/2026-08-24-fee-structure-diagnosis.md` §一/§四 | — |
| C5 | ~46 笔/周 / $210–256 周成本 | `docs/OKR-2026-09.md` KR5 | — |
| C6 | 6 笔 `SL_TRAIL_LOCK` + 价差/过冲读数 | `data/extended_hours_exit_executions.jsonl` + DEB-20260919 §一/§6.1 | 台账行数或字段不符 |

### 10.2 设计挑战（防错 + 防重复建设）

| # | 待挑战设计 | 打假方式（必须实跑） | 需 Hermes 给的答案 |
|---|---|---|---|
| C7 | **自纠 R-1 初判**：Playbook 成交价不含 `engine.py` 7bps 模拟滑点，30bps haircut 是唯一滑点计费 | ① `rg "_apply_sim_slippage"` 调用面；② `app/playbook/playbook_service.py:921` 起追 dual_broker 直连链；③ 抽查 `trade_records` PLAYBOOK_SIM 行 `metadata.sim_slippage_bps` 存在性；④ 成交价 vs 决策时点盘口对拍 | 定谳「双扣 / 漏扣 / 正确」；若非正确，T1 直接出修正结论 |
| C8 | 滑点标定数据源真实可用 | 核 60s tick 序列覆盖窗口与标的；`pre_high_price`/`after_high_price` 读取点；两个 exit_shadow jsonl 行数与字段 | 数据不足则 T2 改设计（不要硬跑） |
| C9 | 回放器支持「最低名义门槛」臂（费用 C） | 读 `scripts/replay_ext_entry_sources.py` 是否支持 min-notional 过滤 | 不支持则给**最小扩展面**评估（禁止临时硬塞） |
| C10 | 延长时段回放宇宙 vs 全链 scorecard 宇宙的可比性 | 两边样本定义/SQL 对照 | §四 T4「分层不许混」是否已守住；给出各自宇宙声明 |
| C11 | 期望瀑布与既有量具是否重复建设 | 对照 `exit_scorecard` size_normalized / `app/analysis/trade_attribution.py` / `docs/REPLAY-SCRIPT-LEDGER.md` | 瀑布应**复用**哪些件、只新增哪些件（口径 SSOT 只许一处） |
| C12 | 时间表与 cron / 冻结纪律冲突 | ① T3 在 9/30 前只回放不改参数；② 周六 09:05 周检时点；③ 06:35 时段拥堵（DEB-20260919 §20.2② 实测 4 作业同分钟） | 排期修正建议 |
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
`DEB-20260914`（graduated=False，G1-1 −$15.62/笔）、`OKR-2026-07`（$1,000/周 KR 失败史）、
`OKR-2026-09`（不设盈利目标 / 单假设 / $210–256 周成本）、`DEB-20260919`（180 日双窗口 / 三次自纠）、
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
| C6 | **数字成立 / 复跑方式不成立** | 6 笔确实存在，但**不在** `data/extended_hours_exit_executions.jsonl`：该文件 15 行、`SL_TRAIL_LOCK` 仅 **2** 行（ACN/WBD）。真源是 `trade_records.exit_reason='SL_TRAIL_LOCK'` 的 **6 行**：ACN/UMC/WBD(9-18)、AMD/INTC(9-17)、QCOM(9-16)；价差/过冲读数出处 `DEB-20260919 §6.1:126`「60s 轮询过冲 0.04% + 买一价成交 价差 0.13%」✅ | 复跑方式改用 `trade_records`（R-1） |
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

- **R-1（C6 复跑方式）**: `extended_hours_exit_executions.jsonl` 只有 2 行 `SL_TRAIL_LOCK`；6 笔在 `trade_records`。**原句「台账行数或字段不符」是真的，但被查的对象本身选错了** —— 若按原方式复跑会得「不成立」的假否定。
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
