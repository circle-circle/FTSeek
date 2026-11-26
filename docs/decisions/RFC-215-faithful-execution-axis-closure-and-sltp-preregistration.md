---
title: RFC-215 — 忠实执行轴：四路封闭取证 + RFC-123 阶段0 处置预注册 + SL/TP 分歧轴判据预注册
status: active（2026-11-19 用户裁「按建议执行」⇒ §5 案A 已裁、§3 判据已生效、§4 D1/D2 已结清；D3/D4 另批）
created: 2026-11-19
driver: 用户 2026-11-19「请根据收益执行优化」→ 裁「忠实执行轴」+「落档 + 预注册（零代码）」→ 同日裁「按建议执行」
related: RFC-123（生成锚/执行锚生命周期分离，阶段0/阶段1/附A/§G）、RFC-144（chase_cap 锚归因）、RFC-145（锚漂移价值评估，§5 重开条件）、判据字典 C16/C17/C14/C11/C12、OKR-2026-11 §6/§7/§8/§12.3/KR4、DEB-20261117（qwen4-max 切换）、DEB-20261118（出场盈利全链审计）、shadow_registry `entry_anchor_dual` / `sltp_verify_shadow`
deploy_state: **本文自身零代码**（纯 Markdown）。§4 D1/D2/D3 的落地改了 `app/`（`shadow_registry.py`、`entry_routing.py` 25/0、`pipeline_parts/auto_execute_flow.py` 41/0；`entry_band.py` **零 diff** — 纯函数按 §6 第 1 条移出），D4 只改 `scripts/`（不在 G-02 监视面）。**已于 2026-11-19 22:12:56 CST 人工受控部署**：军规两核通过（未结算 `exit_intents` 0；`futu_lv3_provider.py` mtime < 原进程启动 ⇒ 不新部署他人 WIP）、`systemctl restart` rc=0、MainPID **3488322 → 3524086**、启动冒烟 **7/7 exit=0**、重启后 `ERROR|CRITICAL|Traceback` **0**、`[US Sync] Done | Futu: 6 | 一致性 ✓`、**H-12 前置预测 0 / 后置实测 0（逐仓逐字一致）**、**G-02 drift 归负**。部署前静态门禁 `check_undefined_names.py` 对 drift 全部 4 个 app 文件 **rc=0**（修掉初版 4 个未定义名之后）、`py_compile` OK、**90 passed**。**交易行为零变更**：`config_key` 未动、采集未停、D3 为 record-only 且异常隔离、不改 cap 判定。所有读数均来自只读复跑，命令见 §7；部署凭据见 handoff 同日 22:15 条目。
---

# RFC-215 — 忠实执行轴：封闭取证与处置预注册

## 0. 结论（四句）

1. **本文不提案任何入场侧行为变更。** 用户裁的「忠实执行轴」（让入场带/SL/TP 取自模型 `execution_plan` 的原始价位，而非 ATR 机械带）经核查**已被四路独立证据封闭**，且其中三路是**反向**证据——不是"攒样不足不可判"，是"强制执行会更差"。四路见 §2，重开条件写死在 §2.5。
2. **决定性的一路是今天 05:53 刚刷新的日更重放**（RFC-123 §G 第2层观察轨，窗 10-07→11-18、31 交易日、9,173 锚行、217 笔配对、bar 覆盖 **217/217**）：把模型原始 `planned_entry` 当限价锚执行 ⇒ **fill 112 / miss 105**，成交侧价格改善 **+$219.65**，而被跳过的 105 笔**实际合计 +$1,402.65（91 赢 / 14 亏）**。即用 +$219.65 换掉 +$1,402.65，**净 -$1,183**。模型那个价 66.4% 够不到（reach 73 / unreach 144），而"够不到"恰恰是赢家筛选器。
3. **还开着的唯一子轴是 SL/TP 分歧，不是入场价。** 在册影子 `sltp_verify_shadow`（8,994 行，末行 11-18，已有结算器）给出**单调**梯度且**已排除波动率混淆**：SL 一致 n=545 均 +0.0027 / 胜率 83% → 分歧 ≥2% n=268 均 **-0.0013** / 胜率 **63%**；`corr(sl_div_pct, atr_pct)=0.0665`，各 ATR 档内分歧≥1% 占比 8.3 / 8.9 / 11.0% ⇒ 分歧是独立维度，不是 ATR 代理。**§3 为它预注册判据（跑之前写死），本轮只读数、不接闸。**
4. **真正的欠账在测量面与 SSOT，不在机制面。** RFC-123 阶段0 结算器 11-12 已跑出 `verdict=MANUAL_REVIEW`（四条预注册判据 **3 PASS / ③ NOT_COMPUTABLE_COUNTERFACTUAL / ④ 无基线**），**至今 7 天无人裁**；注册表 `entry_anchor_dual` 的 `eval_script=None` 是**过期记载**（脚本实存且已跑）、`recheck_after=2026-11-05` **逾期 14 天**；RFC-145 §4 的 **B1（推荐、零行为影响）与 B3 均未落地**。§4 逐项结清，§5 预注册阶段0 的处置判据。

---

## 1. 事实：确实存在两份计划（已核，三例 + 机制）

用户裁这条轴的依据是我当日上报的三例。事实成立，如实落档：

| 标的 | 模型 `execution_plan` 原文约束 | 下游 `playbook_instructions` | 实际成交 | 偏差 |
|---|---|---|---|---|
| **ELV** 11-17 | 入场锚 **433.00**「回落至 MA5 附近企稳低吸」；**禁止 441.00 以上追涨**；不在 447.42–450.69 供给区追入 | 带 **420.01–454.65**，SL 420.28，TP 475.73 | **439.42**（09:50:33 ET） | 高于模型锚 **+$6.42 / +1.5%**；带子覆盖其整个禁入供给区 |
| **LLY** 11-18 | **仅当回落到 $1390** 执行首批；**$1418 以上禁止追涨**；收盘失守 MA20 **1371.00** 则「整段多头结构作废」 | 带 **1348.3–1459.5**，SL **1341.4** | **1410.64**（09:56:42 ET） | 高于模型锚 **+$20.64 / +1.5%**；**SL 在模型论点作废位之下 $29.60** |
| **APH** 11-18 | 回踩 **$76.70** 附近首批；**$78.10 以上禁止追涨**；跌破 **74.62** 双底则「本计划整体作废」 | 带 **74.399–80.535**，SL 73.04 | 77.02 | 成交价尚可，但带子同时跨越禁追线与作废线 |

**机制（源码级）**：`app/playbook/entry_band.py:13` 的带宽是 `knife = max(3%, min(6%, 0.9×ATR%))` / `chase = max(5%, min(8%, 1.5×ATR%))`；`app/playbook/instruction_generator.py:14-16` 是 `stop_loss = entry − 1.5×ATR`、`take_profit = entry + R:R×risk`；同文件 `:203-204` 注释自认 `invalidation`「仅用于归因/复盘/退出触发地基，**不参与数值计算**」；持仓侧契约 `app/playbook/invalidation_contract.py:1-17` 明确 `REVIEW_ONLY`；未执行侧 tripwire 核销器 `scripts/tripwire_checker.py` 本窗 **0 条**。

**定性（重要，防重复立案）**：这**不是新发现**。它是 RFC-123 已量化的 **E18 族**（「`entry_price` 一列两义：生成锚被 reprice 逐轮覆盖为执行锚，实测 **66.4% 指令已改写**，偏差中位 **1.01%** / p90 **3.65%**」）的三个新样例。本文把它们写下来的唯一目的，是**防止下一个 agent（或我自己）把它们当作新证据去复活 §2 已封闭的轴**——这正是本仓 `lessons` 表当前在犯的错（见 §6.3）。

---

## 2. 为什么不予立案：四路封闭取证

### 2.1 判据字典 C16 —— 轴关闭·**双侧禁止**（2026-10-30 终局，非攒样不足）

C16 原文：「不得以『入场形态轴』（追突破/回调/`entry_archetype` 分治）或『收紧追价帽』作为隔夜跳空或整体失血的解药；**反向同样禁止 —— 也不得据『回调入场更优』放宽追价上限**。」

其几何口径正是本文这条轴（`chase_bps = sign×(fill−planned)/planned×1e4`，且**明确要求 `planned` 取 `raw_instruction_json.planned_entry` 而非 `playbook_instructions.entry_price`，因后者已被 reprice 覆盖**）。A10 已平 721 笔、覆盖 498 笔：

| 桶 | n | Σ净 | 剔 12 笔跳空后单笔均值 |
|---|---|---|---|
| **A 贴/优于计划** | 124 | **-1,346** | +3.5 |
| B 追 0–50bps | 58 | -184 | -3.2 |
| C 追 50–150bps | 100 | **+538** | **+11.2** |
| D 追 >150bps | 216 | +102 | +5.1 |

⇒ **贴/优于计划（A 桶）是最差桶**；剔尾后追价桶(B+C+D) Σ **+2,002** vs A 桶 **+413** ⇒ 原文结论「**追价在剔尾后不是负 alpha，反而是主要正贡献 ⇒ 收紧追价帽 = 砍利润**」。且月度符号全翻转（A 桶 −15.0/+3.1/+15.7/+3.3）⇒ 无稳定判别力，**两个方向都不许动**。

C16 留下的唯一可读结论：「12 笔跳空的 −$3,828 单独决定全期符号，病在『隔夜携仓 × 入场即水下』，**不在入场价与计划位的关系**。」

### 2.2 RFC-145 §5 重开条件 —— 三条逐条核，**均不满足**

RFC-145（2026-10-17，status「评估完成；结论=**不建**重锚机制」）§5 写死「同时满足才允许重提」：

| # | 条件 | 10-17 立案时 | **2026-11-19 今日实测** | 判定 |
|---|---|---|---|---|
| ① | A10 期均净转正 **且** 费/毛 < 1.0 | 均净 −$2.38、费/毛 **1.48** | DA 整窗 11-01→11-18：n=115、均净 **−$7.48**、净 **−16.51bps**（毛 −7.16 / 费 9.35）；11-10→11-19：毛 **−$1,404.87** 而费 **+$373.41** ⇒ **毛为负，费/毛 无定义且劣于 1.48** | **✗ 更远** |
| ② | D 案尺子获许可（`reason_match ≥0.8`）且配对 n≥40/腿、同尺配对差双侧可分辨 | 0.688，未获许可 | 本轮**未复测**（不改判） | **✗ 未满足** |
| ③ | 锚漂移人群在**新**预注册面上显示**优于**系统平均质量 | 未测 | C16 与 §2.4 均给**反向**读数（A 桶最差 / miss 组是赢家） | **✗ 反向** |

⇒ **§5 三条无一满足，P2 及任何同轴执行侧改动不得重提。**

### 2.3 RFC-123 附A —— 生成锚**本身**不合理（不是"没被忠实执行"）

RFC-123 附A（2026-10-11 只读诊断）：「LLM 拍的是『理想回踩/反抽价』，市场 **68~76% 不给这个价**」，并明写「**这不是阶段1（拆锚持久化）能治的** —— 阶段1 只保证生成锚不被 reprice 漂移，**不保证生成锚本身合理**」。

⇒ 用户裁的"忠实执行"若真落地，等于把一个 **2/3 概率不可达**的价格设为硬约束 ⇒ 结果是**大幅减少交易**，而不是改善交易。

### 2.4 第2层日更重放 —— **决定性**，且是今天的数据

`data/shadow/rfc123_l2_rule_anchor_latest.json`（`generated_at=2026-11-19T05:53:04`，窗 until 11-18、31 交易日、anchor_rows 9,173、matched_trades **217**、bar_coverage **217/217**、`adr035_overlap=217`）。实际基线：这 217 笔 `actual.total_pnl = **−2,070.49**`。

**可达性**：reach **73** / unreach **144** ⇒ **66.4% 市场不给模型那个价**（与附A 的 68~76% 在 10 倍样本上复现）。分解：`LONG_below_low 81`（价格从未跌到模型限价）、`SHORT_above_high 39`、`LONG_reach 56`、`SHORT_reach 17`。

**把模型原始价当限价锚执行（`llm_plan`）**：

| 锚方案 | fill | miss | 成交侧价格改善 Σ | 反事实 PnL | **被跳过那些笔的实际 PnL** | miss 中赢家/亏家 |
|---|---|---|---|---|---|---|
| **`llm_plan`（=忠实执行）** | **112** | **105** | **+$219.65** | −434.40 | **+$1,402.65** | **91 赢 / 14 亏** |
| `open`（当日开盘） | 178 | 39 | +$157.76 | −258.21 | +$617.15 | 33 / 6 |
| `or_mid`（开盘区间中点） | 190 | 27 | +$150.17 | −270.33 | +$268.62 | 23 / 4 |
| `vwap` | 193 | 24 | +$141.00 | −593.04 | +$458.62 | 22 / 2 |
| `atr_dip_0.5` | 29 | 188 | +$134.65 | −87.48 | +$170.59 | 150 / 38 |
| `atr_dip_1.0` | 2 | 215 | +$8.38 | −6.47 | −$1,884.19 | 166 / 49 |

⇒ **忠实执行的账**：成交侧只赚 **+$219.65** 的价格改善，代价是跳过 105 笔、其中 **91 笔是赢家、实际合计 +$1,402.65** ⇒ **净 −$1,183.00**，且机会成本集中在赢家侧（91:14 = 6.5:1）。**"够不到模型那个价"不是执行缺陷，它是当前组合里唯一的正向筛选器。**

同屏局限（脚本自述，逐字采纳）：`observation_discipline = "只筛方向不裁决, 无预注册阈值 (RFC-123 §G)"`；PnL 用 held-constant exit 近似，有路径选择偏差（成交子集=价格回踩过的非随机子集）；主稳健指标是 `fill_rate` 与 `entry_improvement`（纯 bar 几何），PnL 仅方向参考。

### 2.5 重开条件（写死，防以后凭感觉复活）

同时满足才允许重提「入场几何取自模型原始价位」：

1. §2.4 的 `llm_plan` 臂在**扩窗全史 + 逐笔 EOD 反事实 + 扫 k×ATR** 后，`miss_act`（被跳过笔的实际 PnL）**由正转负**，即证明"够不到模型价"不再是赢家筛选器；
2. C16 按其**自身**复活条件被解开（新论据 + 新数据源给出**事前可识别**的跳空子集 + 该子集内追价与结局单调且逐月同号 + 证明不侵蚀 C/D 桶的 +$2,185）；
3. RFC-145 §5 三条**全部**满足；
4. DA 主通道净 bps/笔 已按 OKR-2026-11 KR1 转正（当前 −16.51bps，CI80 [−29.83, −2.02] **不含 0**）。

**不得引用本轮 §1 的三例作为重开论据** —— 它们是 E18 族的样例，而 E18 族的经济后果已由 §2.1/§2.4 在 721 笔与 217 笔两个独立人群上测过，方向为负。

---

## 3. 预注册：SL/TP 分歧轴（**跑之前写死**，禁事后挑格子）

这是"忠实执行"里唯一没被封闭、且数据支持的子轴。它问的不是"入场价该不该听模型的"，而是"**代码算出的 SL/TP 与模型自述的 SL/TP 分歧越大，结局是否越差**"。

**立案时点的实测基线（只作参照，不入判据；2026-11-19 复跑 `scripts/sltp_shadow_pnl_report.py`）**

- 影子记录 **8,994**，join 成交（非零 pnl）**1,692**；SL 一致 **545** / SL 分歧 **1,147** / 缺 LLM SL 无法判定 **0**。
- 一致组 mean **+0.0027**、median +0.0020、胜率 **83%**；分歧组 mean **+0.0001**、median +0.0018、胜率 **73%**。
- **分歧幅度分箱单调**：`[0,0.2%)` n=541 mean +0.0027 / 胜 83% → `[0.2,0.5)` n=394 +0.0016 / 82% → `[0.5,1.0)` n=241 +0.0007 / 77% → `[1.0,2.0)` n=248 **−0.0012** / 68% → `[2,+)` n=268 **−0.0013** / **63%**。
- **混淆已排除**：`corr(sl_div_pct, atr_pct) = 0.0665`；各 ATR 档内"分歧≥1%"占比 `atr2-4%` **8.3%** / `atr4-6%` **8.9%** / `atr≥6%` **11.0%** ⇒ 分歧幅度**不是**波动率代理。
- `sl_agree` 全量分布：True **5,674** / False **3,320**。

### 3.1 判据（预注册，窗内禁改；改=重裁）

| 项 | 规定 |
|---|---|
| **人群** | `sltp_verify_shadow` join `trade_records` 成功的已平单，A9 干净谓词（同 OKR-2026-11 §2.1）+ A10，**按 `inst_id`/`decision_id` 精确配对**，同 `decision_id` 双行按 `executed=1` 取行 |
| **主终点** | 高分歧臂（`sl_div_pct ≥ 1.0%`）vs 低分歧臂（`< 0.2%`）的**净 bps/笔**（A9 口径：`10000×Σpnl_net/Σ(entry_price×quantity)`） |
| **为什么必须换掉 `pnl_pct`** | 现成报告用的是 `playbook_instructions.pnl_pct` **原始存储值**，报告自注「关注相对差异，**非绝对量纲**」⇒ 不可用于裁决。主终点必须换成落库净额 + 名义额的 bps 口径 |
| **必报** | 两臂 n、名义额、毛/费/净三段 bps、**日聚类 80% CI**、**MDE80**、日数（宪章第 6/9 律）；A9 含/不含双口径并列，缺一即 KR5 WARNING |
| **样本门** | 每臂 **n ≥ 200**（立案时点低分歧臂 541 / 高分歧臂 516 ⇒ **已足**，无需攒样） |
| **单调性检验** | 五箱（`[0,0.2)/[0.2,0.5)/[0.5,1.0)/[1.0,2.0)/[2,+)`）的净 bps 必须**单调不增**，否则判「梯度不稳健」，不得只引两端 |
| **必查混淆（三项，逐项披露不得省略）** | ① `bucket`（vol 档）：立案时点分歧组内 `medium` mean −0.0018/胜 63%、`extreme` −0.0061/胜 67%、`high` +0.0014/胜 78% ⇒ vol 档自身与结局相关，必须做**档内分层**复算；② `regime`：分歧组内 `'?'` 桶 n=79 mean **−0.0037** / 胜率 **32%** 是**数据质量洞**，必须先归因或剔除并披露剔除笔数与金额；③ 方向：分歧组 `BUY` n=892 mean −0.0002/胜 71% vs `SELL_SHORT` n=255 +0.0012/胜 81% ⇒ 必须按方向分开报 |
| **尺子定义即判据** | `sl_agree` 的阈值（现 0.2%）与分箱边界**属判据本体**，窗内禁改；改阈值=改判据，须先落档 DEB + 用户裁 |
| **判成什么算什么** | 两臂差 **≥ MDE80 且日聚类 CI 下界 > 0** ⇒ `SIGNAL_CONFIRMED`（**仅**表示"分歧与差结局同现"，**不**表示因果）；差 < MDE80 ⇒ **`INDISTINGUISHABLE`（不得写"已证伪"，OKR §9/KR4 纪律）**；`regime='?'` 桶剔除后符号翻转 ⇒ `DATA_QUALITY_BLOCKED`，先修数据再判 |
| **决策日** | **2026-12-02**（与 thesis 影子复核同日），或样本门达标取先到者 |
| **明确不做** | 本轮**只读数、不接闸**。转硬闸（例如"分歧 >X% 则用 LLM 的 SL"）须**另立 RFC + 用户裁**，参照 B19/C16 先例「影子不自动生效」。且任何 SL 侧改动须先过 C11（缺口仓不放宽 SL）与 C16 双侧禁止的适用性检查 |

### 3.2 已知的口径陷阱（前置披露，防复算翻车）

- **价格改善类读数必须方向感知**：RFC-145 §2.4 自查更正记录过一次真实翻车——方向盲算法把 CAT(SHORT) 709→734.61 记成"+361bps 更贵"，实为**更优**，修正后 **32/40 例符号翻转**、结论反转。本轴凡涉及 SL/TP 距离比较，BUY 与 SHORT 必须分别定符号。
- **`trade_date` = 入场日，不是平仓日**：OXY 净 −$131.76 在库落 11-13 行、实际 11-18 09:33 平仓。按 `trade_date` 做日汇总会把隔夜亏损错记到入场日；日聚类必须按**平仓日**聚类并披露口径。

---

## 4. 欠账结清（四项，全部 record-only / 只读，零交易行为变更）

| # | 欠账 | 现状（实测） | 动作 | 行为影响 |
|---|---|---|---|---|
| **D1** | RFC-123 阶段0 结算后**无人裁** | `data/shadow/entry_anchor_dual_eval_latest.json`（`generated_at=2026-11-12T21:58:28-05:00`）：`verdict=**MANUAL_REVIEW**`；四条预注册判据 = `period_days` 27≥10 **PASS** / `missing_rate` 0.0<0.01 **PASS** / `divergence_rate` **0.3787** ≥0.05 **PASS** / `quality` **NOT_COMPUTABLE_COUNTERFACTUAL** / `guardrail` 0.3484「无同窗前向基线；只报读数，不判异常」。分歧幅度 median **2.34%** / p90 **5.90%** / p95 **8.28%**；触发矩阵 `gen=True,live=False` **2,347** vs `gen=False,live=True` **726**；gate block 分歧组 **54.08%** vs 非分歧组 23.11% | 按 §5 预注册的处置判据**做一次裁决**（KILL 阶段1 或授权补采反事实）。**不需要新建结算器** —— 脚本实存且已跑 | **零** |
| **D2** | 注册表 **SSOT 记载过期 27 天** | `shadow_registry.py:388-393` 记 `eval_script=None`、「结算器仍缺」、「列 P2 建结算器，**建成前不判**」，但 `scripts/entry_anchor_dual_eval.py` **实存且 mtime=2026-10-23 14:15** ⇒ 注册表在写下这句话的**次日**就过期了，至今 **27 天**；该脚本自带预注册四条判据、只读、**11-12 已跑出产物**。`read_at=2026-10-22`、`recheck_after=**2026-11-05**` ⇒ **逾期 14 天未重看**。**后果是具体的**：注册表原文「建成前不判」是一道自我施加的阻断，而它依据的前提（结算器不存在）早已为假 ⇒ 阶段0 的裁决被一句过期记载多挡了 27 天。**更强的机制**：`no_adjudication_reason="PERIOD_GATE: 8/10 交易日 (截至 2026-10-16)"` 同样过期（实测影子期已 27 日），而该字段**非空 ⇒ `refresh_counts` 不迁移 `READY_TO_EVALUATE`**（注册表 :119-121 原文）⇒ 它把本项从待裁面上**主动摘掉**了。〔**本文初稿一处表述已被实测更正**：初稿称注册表 `n=2178` 与结算器 `anchor_rows=8115` 是「两个计数轴」；实测（2026-11-19）两者是**同一谓词、同一文件** `data/entry_timing_shadow.jsonl`，live `where:anchor_recorded=True` = **9,173**（全文件 17,276 行）、结算器 11-12 快照 8,115（total 16,218）、旧记 2,178 是 10-22 快照 ⇒ **数字差 = 快照时点差，不是口径差**。判据①–④ 的比率口径以结算器行为准。〕 | 更新该条：填 `eval_script`、`read_at/recheck_after` 归位、删掉过期的 `no_adjudication_reason`、登记快照时点差的对应关系（军规：注册表是唯一 SSOT；AGENT.md 已有"计数漂移成谎言"先例：2026-10-15 实测 79 项时正文仍写 69） | **零**（`config_key` 未动、采集未停；写方 `trigger_flow.py:205` 只读 YAML 键、不读注册表状态 ⇒ 转 KILLED 不影响采集） |
| **D3** | RFC-145 §4 **B1 未落地** | `chase_cap_anchor_headroom` 全仓（`app/`·`scripts/`·`data/shadow/`）**0 命中**；RFC-145 于 10-17 标 **B1 推荐**、「行为影响=零」、「成本极低」 ⇒ **逾期 33 天** | 按 RFC-145 B1 原文实现 record-only 机检（cap 对生成锚的余量 < 追价带宽 5% 时记一行"过紧帽"，含余量%/archetype/方向） | **零** |
| **D4** | RFC-145 §4 **B3 未落地** | 「同指令 ≥20 轮空转 且当日 `executed=0`」未成为 DA 生成质量指标；RFC-145 §1 实测空转足迹 **2,167 轮 / 89 个 (指令,日) 对**，中位 10 轮、最大 **71** 轮、≥20 轮 **40** 对 ⇒ 真实噪音成本无人看 | 加进**既有** DA 生成质量报表，不新增机制、不新增采集 | **零** |

**为什么这四项值得做而 §1 那条不值得**：D1–D4 全部是**已授权、已攒样、零行为影响**的欠账，且 D1 直接决定"阶段1 到底做不做"这个悬了 30 天的问题；而 §1 那条是被四路证据封闭的行为变更。做减法优先于做机制。

---

## 5. 预注册：RFC-123 阶段0 的处置判据（D1 用，跑之前写死）

阶段0 的四条判据里，①②③④ 的现状是 **3 PASS / ③ 结构性不可算 / ④ 无基线**。③ 不可算的原因（结算器自述）：「行内只有 `gen_triggered`/`gen_path`，**无生成锚成交 PnL**」⇒ 反事实从未被采集。

**处置判据（二选一，由用户裁；本文不代裁）**：

| 案 | 内容 | 代价 | 适用条件 |
|---|---|---|---|
| **案 A（推荐）** | 判 **阶段1 KILLED / 不立项**。理由：③ 是阶段1 唯一的经济性判据，而 §2.4 已用 217 笔独立人群给出**反向**读数（忠实执行净 −$1,183）；C16 已终局关闭同轴；OKR-2026-11 KR4 明写「**到期无结论 → 自动 KILLED，不再无限期观测**」，而本项自 10-20 影子期已满、11-12 出读数，**已悬 30 天** | 放弃"锚持久化分离"这一数据整洁性收益 | ③ 在可预见期内仍不可算，且 §2.4 读数不翻转 |
| **案 B** | 授权**补采生成锚反事实**（record-only 扩字段：为分歧行补生成锚下的 fill/exit 重放），扩窗全史后重判 ③ | 新采集 + 重放工程（RFC-123 附A 明写「裁决须扩窗全史 + 逐笔 EOD 反事实 + 扫 k×ATR」）；且**必须另立 RFC**，因为它是新测量面 | 用户认为"锚一列两义"的数据整洁性本身值得投入（C16 就是被迫绕过它：`planned` 必须取 `raw_instruction_json.planned_entry`，不能用 `entry_price`） |

**两案共同的硬约束**：
- 无论 A/B，**均不得改 `entry_band.py` / `instruction_generator.py` 的任何常量或公式**；
- 案 B 的补采**不得**与 §3 的 SL/TP 轴同批上线（一次一变量）；
- 案 A 落地时须在注册表把 `entry_anchor_dual` 转 `KILLED` 并记否决理由（军规：状态变迁必须在注册表更新，用户裁决结论作为审计留痕），**采集是否停另裁**（先例：判据 KILLED ≠ 采集自动停，须先查 `_CONSUMER_ACK`/消费方）。

---

## 6. 明确不做（防扩大解释）

1. 不改 `entry_band.py` / `instruction_generator.py` 的任何常量、公式或带宽。
2. **不收紧也不放宽追价帽**（C16 双侧禁止）。
3. 不动隔夜 / EOD（C17 恒真红线：取消隔夜 Δ **−$2,028.24**，n=79，四个月同号；C14：EOD 15:52 减半 **−$1,099.74**）。
4. 不把 SL/TP 分歧接成硬闸（§3 只预注册读数）。
5. **不与 `lessons` 注入判据闸同批**（那属"输入净化轴"，另批；一次一变量）。
6. 不改任何已预注册阈值；改 = 重裁。
7. 不新开影子（D1–D4 均为既有在册项的欠账结清）。

### 6.3 顺带登记（不属本文范围，另批）

本仓 `lessons` 表当前正在注入与 C17 直接冲突的建议，且带 `impact_score=0.9 / confidence=0.99 / status=active`：

- `lessons.id=578`：「`[P0|excellent]` 日内策略**必须增加强制收盘平仓机制**，以彻底规避隔夜跳空风险」
- `lessons.id=575`：「`[code]` 在交易执行代码中增加强制平仓逻辑，在预设收盘时间（如 **15:50**）前平掉所有当日开仓头寸」
- 注入路径：`app/review/lesson_injector.py:115` 取全部 `status IN ('active','ACTIVE')` 且未过期行 → `generate_battle_plan_injection()` 拼成「【历史教训】(从昨日复盘自动提取，请参考)」→ `app/pipeline/observers.py:155` 注入盘前作战计划。**注入前只过滤 `status` 与 `expiry_date`，无任何一道闸比对 `docs/ADJUDICATED-CRITERIA.md`**；这些行 `validation_count=0`、`recurrence_count=0`。

⇒ C17 于 **10-30** 以 n=79、Δ=−$2,028.24 终局证伪的动作，**19 天后**被自家学习环以 P0/0.99 的口气每天喂回模型。**这是与本文同族的病（推理产物绕过已裁证据影响输入），但属另一条轴，须另批处置，本文不动。**

---

## 7. 验证（全部可复跑，逐条对应上文数字）

```bash
# §0.1/§2.2① OKR 权威读数（判分器为唯一取数入口，禁手算 — OKR-2026-11 §12.7）
PYTHONPATH=. ./venv/bin/python scripts/okr_2026_11_scoring.py --as-of 2026-11-18

# §2.1 C16 与 §2.2 RFC-145 原文
grep -n "C16" docs/ADJUDICATED-CRITERIA.md
sed -n '91,97p' docs/decisions/RFC-145-chase-cap-anchor-drift-value-assessment.md

# §2.4 第2层日更重放（今日 05:53 产物，只读）
./venv/bin/python -c "import json;d=json.load(open('data/shadow/rfc123_l2_rule_anchor_latest.json'));print(d['generated_at'],d['window']['matched_trades'],d['window']['bar_coverage']);print(d['reachability']);print(d['limit_backtest']['llm_plan']);print(d['actual'])"

# §1 三例：模型原文 vs 下游指令 vs 实际成交
./venv/bin/python -c "import json;d=json.load(open('data/da_checkpoint/2026-11-17/US.ELV.json'));print(json.dumps(d,ensure_ascii=False)[:1200])"
sqlite3 'file:data/ftseek_us.db?mode=ro' "SELECT date,decision_id,symbol,entry_low,entry_high,stop_loss,take_profit,execution_state FROM playbook_instructions WHERE symbol IN ('US.ELV','US.APH','US.LLY') AND date>='2026-11-17' AND row_kind='instruction'"
sqlite3 'file:data/ftseek_us.db?mode=ro' "SELECT id,symbol,entry_price,quantity,entry_time FROM trade_records WHERE symbol IN ('US.ELV','US.APH','US.LLY') AND trade_date>='2026-11-17'"

# §3 SL/TP 分歧轴（既有结算器，只读）
PYTHONPATH=. ./venv/bin/python scripts/sltp_shadow_pnl_report.py
# §3 混淆排除（corr 与档内分布）
PYTHONPATH=. ./venv/bin/python - <<'PY'
import json,statistics
rows=[json.loads(l) for l in open('data/shadow/sltp_verify_shadow.jsonl',encoding='utf-8') if l.strip()]
rows=[r for r in rows if r.get('sl_div_pct') is not None and r.get('atr_pct')]
dv=[r['sl_div_pct'] for r in rows]; at=[r['atr_pct']*100 for r in rows]
def p(a,b):
    ma,mb=statistics.mean(a),statistics.mean(b)
    return sum((x-ma)*(y-mb) for x,y in zip(a,b))/((sum((x-ma)**2 for x in a)*sum((y-mb)**2 for y in b))**0.5)
print('n=',len(rows),'corr=',round(p(dv,at),4))
PY

# §4 D1 阶段0 结算器既有产物（11-12 跑的，无需重跑）
./venv/bin/python -c "import json;d=json.load(open('data/shadow/entry_anchor_dual_eval_latest.json'));print(d['generated_at'],d['verdict']);print(json.dumps(d['criteria'],ensure_ascii=False,indent=1))"
# §4 D2 注册表记载 vs 实存脚本
grep -n "entry_anchor_dual" -A 10 app/playbook/shadow_registry.py | sed -n '1,14p'; ls -la scripts/entry_anchor_dual_eval.py
# §4 D3/D4 B1/B3 未落地
grep -rn "chase_cap_anchor_headroom" app/ scripts/ data/shadow/ | wc -l
```

---

## 8. 未验证 / 局限（如实列出）

1. **RFC-145 §5 条件②（`reason_match ≥0.8`）本轮未复测** —— 沿用 10-17 记载的 0.688。若它已愈合，§2.2 的②仍不满足①③，结论不变，但应如实更新记载。
2. **§2.4 的 `llm_plan` 反事实是 held-constant exit 近似** —— 脚本自述有路径选择偏差（成交子集=价格回踩过的非随机子集），PnL 仅方向参考；稳健的是 `fill 112/miss 105` 与 `miss_act +$1,402.65`（纯 bar 几何 + 落库实际 PnL，不含近似）。**净 −$1,183 这个数含近似成分**，方向不依赖它。
3. **§3 的 SL/TP 梯度尚未做 vol 档内分层复算** —— `bucket` 自身与结局相关（`medium` −0.0018/63% vs `high` +0.0014/78%），§3.1 已把档内分层列为必查混淆，**本轮未做**，故 §3 目前只是"立案时点基线"，不是判据读数。
4. **§3 的 `regime='?'` 桶（n=79、mean −0.0037、胜率 32%）未归因** —— 这是数据质量洞，判据里已规定必须先归因或剔除并披露。
5. **§1 的 ELV/APH/LLY 三例是抽样，不是全量** —— 全量偏差面以 E18 族读数为准（66.4% 指令被改写、中位 1.01%/p90 3.65%），本文未逐笔复核 11 月全部 DA 成交。
6. **D1–D4 均未实施** —— 本文零代码。四项的落地须另批，且 D3 涉及 `app/` ⇒ 改它就是延迟 ≤10 分钟的部署（G-02 安全窗 09:00–12:00 CST 自动重启），须走军规两核 + 启动冒烟。
7. **KR4 超编另账** —— 注册表现状 **OBSERVING 67** / KILLED 47 / SUPERSEDED 4 / GRADUATED 2（共 120），对 OKR-2026-11 KR4「并行实验 ≤5」仍**结构性超编**；OKR §4 KR4 要求的「11/07 前完成一次 94 项注册表盘点」**未见落档**。本文不处理，仅如实登记。
8. **本文未触碰他人 WIP** —— `app/data/futu_lv3_provider.py`、`docs/debates/DEB-20261118-*`、`docs/debates/README.md`、`scripts/verdict_outcomes_ledger.py`、`obsidian_vault/*` 均未读改。

---

## 9. 批准记录

- **2026-11-19**：用户裁「本轮优化走**忠实执行轴**」+「交付到**落档 + 预注册（零代码）**」。本文据此立案；立案过程中发现该轴已被四路证据封闭（§2），故本文**不提案该轴的行为变更**，改为：①封闭取证 + 写死重开条件（§2.5）；②预注册唯一还开着的 SL/TP 分歧子轴（§3）；③预注册 RFC-123 阶段0 的处置判据（§5）；④登记四项零行为欠账（§4）。
- **待用户裁三项**（立案时原文，按纪律逐字保留）：
  1. **§5 案 A（判阶段1 KILLED，推荐）还是案 B（授权补采生成锚反事实）**；
  2. **§3 的 SL/TP 分歧轴判据是否按本文预注册生效**（生效后窗内禁改，12-02 出读数）；
  3. **§4 的 D1–D4 是否排期**（D1/D2/D4 零风险，D3 改 `app/` 须走部署纪律）。
- **§6.3 的 `lessons` 注入判据闸**属另一条轴，须另批，本文不动。

### 9.1 用户裁决（2026-11-19 同日，「按建议执行」）

| # | 裁决 | 落地状态 |
|---|---|---|
| 1 | **§5 = 案 A**：RFC-123 阶段1（锚持久化分离）**KILLED / 不立项**；重开条件 = §2.5 四条同时满足 | **已落地**。`shadow_registry.py` 的 `REGISTRY` 条目 `status` OBSERVING→**KILLED**、`verdict_history` 追加 2026-11-19 条目（四块否决理由逐条带凭据）、删除过期的 `no_adjudication_reason`（PERIOD_GATE 假前提）；`ADJUDICATED` 条目 `read_at` 10-22→11-19、`recheck_after` 11-05（逾期 14 天）→**11-26**（= 「采集停/不停」另裁闹钟）。**`graduation_criteria` / `min_n` 一字未动**（改判据=重裁）。 |
| 2 | **§3 SL/TP 分歧轴判据生效**：本轮**只读数、不接闸**，窗内禁改口径，决策日 **2026-12-02** | **已生效**（本文 §3.1 即判据本体）。**未实施任何采集/脚本改动** ⇒ 12-02 出读数时按 §3.1 逐条执行；转硬闸须另立 RFC + 用户裁。 |
| 3 | **§4 D1/D2 已结清**；**D4 另批**；**D3 单独排**（改 `app/`，须走部署纪律） | D1/D2 见上。**D4 本批未做**（用户裁「只动自己的文件」，避免与并发批撞 `scripts/da_chain_health_report.py`）。**D3 未做**（RFC-145 §4 B1，改 `app/`，另批）。 |
| — | **采集不停** | `config_key` `playbook.execution_parameters.entry_anchor_dual_shadow_enabled` **维持 true**。实测依据：写方 `app/playbook/pipeline_parts/trigger_flow.py:205` 只读该 YAML 键、**不读注册表状态** ⇒ 转 KILLED 不影响采集。且 `rfc123_planned_entry_anchor_backtest.py`（cron `53 5 * * 2-6 --daily`）**正是 §2.4 那 217 笔否决证据的复跑链 ⇒ 停采等于断自家否决证据**，故停采须另裁。 |
| — | **`_CONSUMER_ACK` 补登** | 转 KILLED 后守卫 `test_baseline_is_clean` 如实报红「终态产物仍有生产消费者」⇒ 逐条实测认领两个**活跃只读**消费者：①`scripts/daily_invariant_check.py` W7（cron `5 5`，昨收分母对账的 ctx 侧原料，审计面非交易链）；②`scripts/rfc123_planned_entry_anchor_backtest.py`（cron `53 5 --daily`）。同 `premarket_invalidation` 10-23 先例写入 `_CONSUMER_ACK`，并同步 `tests/test_shadow_artifact_consumers.py` 的认领集合（+1 键）。两轮报红均为守卫按设计工作，非回归。 |

### 9.2 本批更正与遗留（如实登记）

1. **本文 §4 D2 初稿一处表述已被实测更正**：初稿称注册表 `n=2178` 与结算器 `anchor_rows=8115` 是「两个计数轴」；实测为**同一谓词、同一文件**，差异 = 快照时点（见 §4 D2 行内更正）。
2. **`last_evaluated` 显示 11-19 而非 11-12 属仓内既定机制**：模块自带 `ADJUDICATED → ShadowItem` 的 `last_evaluated` 同步钩子（`shadow_registry.py:5154`）⇒ 运行时显示裁决落档日，不是结算器跑日。非异常。
3. **【本批之外的更正，须另立 DEB】KR2 死单率的口径与人群未定清** —— 本轮审计曾报「落库 `mfe_pct` 21.3% 复算不符 ⇒ KR2 假过线」，**该诊断已被全量归类推翻**：11-01→今 已平且 `mfe_pct` 非空的 **164 笔逐笔归类 ⇒ quote 口径 `peak_price` 解释 82 / bar 口径 `peak_price_bar` 解释 17 / 收养补录行 65 / 无法解释 0**，落库数据**无损坏**。真正的三件事：①夜检 `I4_mfe_recalc`（`daily_invariant_check.py:1112-1115`）只拿 quote 口径复算、不认 bar 口径 ⇒ **是检查器比错口径**，每晚为口径差异报警（11-18 窗 13 处）；②落库字段**混用两把口径**（82 行 quote / 17 行 bar）⇒ 写入方不一致；③KR2 人群里 **56.5%（65/115）是收养补录行**，而 `scripts/da_entry_quality_trend.py` 头部**早已文档化**此类行「peak 重置=entry、regime 硬编码 RANGE，混入会虚高死单率」。四口径实测（门 ≤23.0%）：落库全人群 **22.61%**（判分器 A11 口径，PASS，余量 0.45 笔）/ 落库剔收养行 **20.00%** / 统一 quote 口径全人群 **25.22%**（破线）/ 统一 quote 剔收养行 **24.00%**（破线）⇒ **KR2 的过与破随口径翻转，而其人群过半是 artifact 行**。处置：另立 DEB 定 `mfe_pct` 的权威口径 + KR2 人群是否剔收养行；**不属本批**（一次一变量），且改口径=改判据须用户裁。
4. **§6.3 的 `lessons` 注入判据闸仍未处置** —— 本批未动（另一条轴）。它是我今日审计里唯一**正在主动污染模型输入**的缺陷，建议下一批优先。
