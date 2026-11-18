# 议题：现役出场、止损止盈与全链路净期望审计

## 状态

- [ ] 进行中
- [x] 待决策（仅执行正确性修复与既有实验的后续节奏；本批不建议改任何 SL/TP/BE/trail 参数）
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

- 审计时点：2026-11-18；最新完整已平仓交易日 2026-11-17。
- 范围：Playbook SIM 的入场、尺寸、执行、出场、费用、重启恢复与信号源；不评估港股手工仓。
- 本批性质：三路只读审计（代码路径 / 量化结果 / 历史裁决）。未改生产代码、配置或交易行为，未重启。

## 背景

用户要求从量化交易和整体盈利目标出发，主动检查当前出场、止损止盈及全链路是否还有有效优化空间。现行 OKR 主目标是 DA 净 bps/笔转正，而不是继续增加门或影子。

## 立案前强制检查（P0.5）

### 同轴既有影子与裁决枚举

- 出场参数：`exit_param_shadow`、`exit_disable_cf`、`be_half_at_arm_d`、`be_grace_replay`、`exit_conditional_audit`、`taillock_runner_cf`。
- 隔夜/左尾：RFC-155 `risk_size_tier_forward`、RFC-160 `eod_mfe_gate_shadow`（KILLED）、B19 `eod_depth_gate`（GATE_NOT_WORTH）、C11/C14/C17。
- 分段保护：RFC-188 `staged_lock`（record-only；历史回放多次未过保真门）。
- 准入/频率：RFC-209 `da_frequency_cap_pivot_shadow`、entry_confirm v2、C8/C9/C12/C16。
- 费用/执行：RFC-186b/RFC-186c、RFC-191（Option B/C 已撤回）、`execution_tca_spread_shadow`。
- 执行正确性：RFC-195 与 DEB-20261117 账本同源批；P3 合成 SL/TP SSOT 和 H-12 重启状态是独立正确性议题，不新建收益影子。

本 DEB 不新增门、不新增影子、不提出同轴换阈值重试。所有收益变更仅保留既有预注册轨道。

## 数据与实证

### 1. 当前权威 OKR 读数（DA，2026-11-01→11-17）

复跑：

```bash
PYTHONPATH=. ./venv/bin/python scripts/okr_2026_11_scoring.py --as-of 2026-11-17 --json
```

- n=109，名义 $498,284.81；毛 -$461.04（-9.2525bps），费 $462.00（9.2718bps），净 -$923.04（-18.5243bps）。
- 日聚类 t=-1.7581（12 日），MDE80=31.1457bps，80%CI=[-33.7926,-5.2972]；n<120，按预注册判 UNDERPOWERED，不作终局证伪。
- pre（qwen3.7-max）n=104，净 -21.2402bps；post（qwen4-max）n=5，净 +49.9151bps，n<30 不构成证据。
- 出场结构：SL_INITIAL 12 笔 -$1,219.99；OVERNIGHT_GAP_STOP 6 笔 -$1,186.68；其余 91 笔合计 +$1,483.63。SL_BREAKEVEN 47 笔 +$411.59，SL_TRAIL_LOCK 31 笔 +$894.77。
- EXT 11 月 n=46、净 -23.7098bps，不能再用 10 月 16 笔 +53.19bps 宣称 EXT 整体是正 edge。MOM n=2 不可判。

### 2. 现役出场栈窗口（2026-09-01→11-17，PLAYBOOK_SIM）

只读量化审计使用 `data/ftseek_us.db` 与既有 post-exit/反事实产物；494 笔有净值，净 -$2,309.31。全部 US 样本为 simulate，费用均为 `fees_source='est'`；净值按 `pnl_net`，不混用毛值 `realized_pnl`。

| exit_reason | n | 净额 | 笔均净额 |
|---|---:|---:|---:|
| SL_TRAIL_LOCK | 143 | +$6,082.99 | +$42.54 |
| SL_BREAKEVEN | 236 | +$1,421.89 | +$6.02 |
| PRE_CLOSE_EXIT | 13 | +$891.47 | +$68.57 |
| DEAD_SIGNAL_EXIT | 39 | +$114.46 | +$2.93 |
| EARLY_TIGHT_SL | 3 | -$451.58 | -$150.53 |
| SL_INITIAL | 34 | -$3,997.36 | -$117.57 |
| OVERNIGHT_GAP_STOP | 22 | -$6,520.13 | -$296.37 |

关键解释：

1. 主窗毛额仅约 +$621、费用约 $2,930，费用是毛利的约 4.7 倍；11 月更进一步变成毛亏。因此长期窗是“薄毛 edge 被费用吞掉”，当月则是“毛 edge 本身为负”，不能混成一句话。
2. 固定 TP 距离中位约 7.6%–7.8%；全 PLAYBOOK_SIM 815 笔中 MFE≥TP 仅 1 笔，主窗 TP 触发 0 笔。TP 不是当前盈亏驱动项。
3. SL_INITIAL 的票面初始止损中位约 4.97%，但出场时 effective SL 中位约 1.75%，可核行全部带 early-tight 杀伤标记。不能把 SL_INITIAL 直接解释成“原始 5% 止损太紧”。
4. SL_INITIAL 的 MFE 中位约 0.05%；OGS 的 MFE 中位约 0.24%，多数是入场后从未走对的单。实际出场相对一律持到收盘约少亏 $2,157，但日聚类不显著，只能作方向证据。
5. OGS 22 笔全为 LONG，约 48%–64% 损失来自前收至次日开盘的跳空，止损价无法保证跳空成交价格。但可匹配隔夜单的隔夜腿总体仍约 +$889，禁止隔夜会把大量正贡献一起删掉。
6. BE 组毛 $3,037、费 $1,615，费用吃掉约 53.2% 毛利；这支持 RFC-188 继续测量比例保护，但不支持未过门就抬 BE 或分批平仓。

### 3. 已经否定、不得重新包装上线的方案

- C11：跳空仓放宽 SL/补仓；8%/10%/15% 全更差。
- C14/C17：EOD 减半或取消隔夜，反事实均净负。
- RFC-160/B19：EOD MFE/深度门已 KILLED/GATE_NOT_WORTH；不得改切点复活。
- 固定 TP 重校：历史候选全部净 Δ 为负。
- ATR 放宽止损：现状已约 1×ATR，1.5/2/3×ATR 全更差。
- runner/scale-out/更宽 trail/BE grace：既有回放均为负或未过保真门。
- $80 绝对风险帽：费用升至 26.9bps、净 -$1,909，当日回退；不能泛化为“缩仓就安全”。
- RFC-191 抬单名帽 B/C 已撤回；RFC-192 作废。
- C12：confidence 等分析产物不能作为降低死单率的门；低 conf 净期望轴虽另有探索读数，但 t=1.29，不可直接接线。

### 4. 仍在轨道上的少数可决策实验

- **RFC-209 降频**：探索反事实 2–3 笔/日方向为正，但 t=1.24–1.58，且“取入场最早 N 笔”可能集中选中 09:35–09:45 亏损带。维持前瞻，不把探索值当上线依据。
- **RFC-155 R2 开盘 15 分钟 0.5×尺寸**：历史 +$401.95 是六臂事后最优，剔最佳日仅 +$74.46；新 cohort 当前 0/30，维持 record-only。
- **RFC-188 分段比例保护**：针对 BE 费用侵蚀有机制合理性，但历史回放四次未过保真门，只能等前向独立仓判据。
- **DEAD_SIGNAL_EXIT 关停反事实**：n=63、hold-better 82.5%、合计约 +$513，是唯一样本达标的关闭候选；但回放未完整覆盖 TIME_EXIT/隔夜，且 11 月 DEAD_SIGNAL 本身约打平。等 2026-11-21 重看并补口径，不直接关停。
- **EXT 盘前子集**：探索 n=36、+32.50bps、t=3.44；只限盘前，不外推 EXT 整体。等 n≥30 且 ≥15 ET 日前瞻确认，不放大。

## 方案与建议

### 代码路径核查补充

- 当前 RTH 快环 10s；BE 默认 0.4% 且 fee-aware，static trailing drawdown 0.8%，SIM adaptive 0.4%–1.2%；TP 还需 RR≥1.5。early-tight 在 30min 内把未证明方向的仓收紧到约 1%（ATR 变体上限 2.5%），所以票面 5% SL 不等于实际出场线。
- `overnight_hold_enabled=false` 与 W_WEEK 水下 5 日豁免并存是 2026-08 用户裁决的刻意设计（`app/playbook/overnight_decision.py:58-116`），不是本批认定的 bug。独立 OGS 分支不覆盖该臂，原始 SL 仍兜底；不得把配置注释简化成“全部当日平仓”，也不得更名为取消隔夜提案。
- 出场报价允许最长 120s 缓存且 30s 仅告警（`app/playbook/pipeline.py:3494-3531`）；未结算 intent 会冻结该 symbol 的后续规则（`app/playbook/pipeline_parts/exit_monitor_flow.py:267-279`）。这是确定的可达风险，实际损失频率未证，不与已发生 PnL 混写。
- BE 的 `fee+slip` 可超过 0.4% 武装线，当前只有 `be_geometry_degenerate` 遥测（`app/playbook/exit_engine.py:485-497`）。fee gate 当前只记不拦，因此该形态可真实进入；但历史 fee gate 也不具备直接转正证据，应并入 RFC-188 的前向几何研究，不临时加门。
- LIVE 券商 STOP 只在入场按初始 SL 挂一次，不随 BE/trailing 上移（`app/playbook/playbook_service.py:2571-2593`；`live_stop_guard.py` 与 drift shadow 均不修改 STOP）；EOD/ETH 扫描只覆盖 sim 库。当前 PLAYBOOK_LIVE=0，属 live 启用前硬阻断，不冒充今日 SIM 亏损原因。
- `pnl_net` 使用实际成交出入场价，滑点通常已体现在毛盈亏中，不能再额外扣一次；真正缺口是费用全估算、SHORT 借券成本未进入常规净账，以及计划→成交 TCA 尚需完整样本。

### 方案 A（推荐）：保留出场主干，先修执行正确性与测量

1. 当前不改 SL/TP/BE/trail/隔夜参数；qwen4-max 11-17 刚重置 cohort，再改出场会同时污染新模型与 RFC-155 的归因。
2. P3 恢复路径在 instruction_id 可反查时必须使用指令真实 SL/TP；只有无法反查时才允许 synthetic fallback，并记录来源。该问题历史触发 4 次/4 个月，预期收益小，但正确性确定。
3. live 启用前必须让券商 STOP 跟随 effective SL（或提供等价的离线灾难保护），并让 EOD/ETH 同时覆盖 sim/live 库；重启恢复须保证 trailing/冷却状态不丢，多头恢复后有效 SL 不得低于重启前，空头不得高于重启前。缺失/损坏状态要显式告警，不能静默从成本价重建锁盈进度。
4. 完成迟成交 TCA 回灌的连续 5 个 ET 日验证，并把估算费用与券商实收对账。计划价→成交价偏差必须先统一方向与基准，不直接当可节省成本。
5. 宿主内存/GC 停顿按执行可靠性治理处理；没有逐笔因果证据，不把它记成已量化 PnL 损失。

### 方案 B（不建议）：继续网格调 SL/TP/BE/trail

拒绝。当前 TP 几乎不可达，实际 SL 多由 early-tight 决定，且主要参数族已有反事实否定；继续调参更可能选择噪声并污染 cohort。

### 方案 C（条件性）：若现役 qwen4-max post 窗仍无正净期望，执行 Pivot 而非再修出场

按 OKR §12 用官方判分器等待 post n≥120 或 12 月开跑会（取先到）。若仍满足 Kill/Pivot 条件，优先在“降频保留正期望子集 / 换策略类别 / 承认无 edge 转研究”中裁决；不得再用新门延续同一信号源。任何放大仍须满足 DA 连续 40 个交易日日聚类 80%CI 下界>0。

## 关键分歧与边界

- “剔最差 5 笔即转正”只是尾部集中度描述，不是可执行反事实。
- “OGS 全 LONG”不等于砍 LONG 或砍隔夜；非 OGS 隔夜腿正贡献很大。
- “DEAD_SIGNAL 关闭后更好”尚未覆盖完整 TIME_EXIT/隔夜路径，只能等既有复核。
- 当前全部是美国 SIM，费用全估算；任何净 bps 结论都不是 live 盈利证明。
- KR4 要求并行实验 ≤5，但注册表仍有大量活跃消费项；这是结算治理风险，不应靠在本 DEB 新增实验继续恶化。

## 决策建议

- 采用方案 A；本批零交易行为变更。
- 用户只需裁执行正确性修复是否实施；收益参数不需要裁，因为建议是不动。
- RFC-155/RFC-188/RFC-209/DEAD_SIGNAL/EXT 盘前均按各自预注册日期结算，不提前转正。

## 验证项

- [ ] P3 可反查指令时，trade_records 与 open_positions 的 SL/TP 均等于指令真值；不可反查时 synthetic 来源显式可见。
- [ ] 多/空 trailing 中途重启后，有效止损只紧不松；冷却与在途出场不重复、不丢失。
- [ ] 连续 5 个 ET 日迟成交回灌后，执行账本与真实成交数一致，无重复、无生产审计旁写。
- [ ] 11-28/11-30 用官方判分器并列整窗/pre/post，n<120 明示功效不足。
- [ ] 各既有实验按预注册判据结算，不在窗内改阈值。

## 讨论记录

### 第 1 轮

- 代码审计：核查 Playbook 出场、重启恢复、价格与订单路径；结论以代码锚点为准。
- 量化审计：主窗与 11 月均显示赢家来自 trail/BE，左尾来自 early-tight/OGS；固定 TP 非驱动项，禁止隔夜不成立。
- 裁决审计：大部分朴素修法已终局否定；剩余可决策轨道为降频、开盘尺寸、比例保护、DEAD_SIGNAL 与 EXT 盘前，均未提前转正。
- 主会话综合：不把尾部集中度当因果，不把 SIM 估算费用当 live 证据，优先正确性与一次一变量。

### 第 2 轮：从旧参数优化升级为结构/论点驱动管理

用户进一步要求讨论缠论、利弗莫尔及其他经典理论，并让 LLM 承担更多数据分析与盯盘。形成 `docs/decisions/RFC-213-thesis-driven-position-management.md`：复用 DA 现有 archetype 与 invalidation Stage 2b，设计 TradePlan v1、多时间级别结构、持仓状态机和事件驱动 LLM 管理影子。

本轮不改变第 1 轮“旧 SL/TP 参数无可复现正期望改动”的结论；新方案不是换阈值，而是改变管理语义。先实现无未来函数结构特征与 schema，再做单一 record-only 影子；LLM 只能 HOLD/TIGHTEN/EXIT/ABSTAIN，硬风控和订单始终由确定性代码负责。新隔夜臂按论点有效性而非单纯盈亏决定，不直接替换 W_WEEK。

## R2 设计正文：结构识别与论点驱动的 TradePlan / 持仓状态机

> **本节是供另一 Agent 独立分析、攻击和补充的完整设计底稿。** 同步设计文件为 `docs/decisions/RFC-213-thesis-driven-position-management.md`；后续讨论和修订以本 DEB 为权威记录，另一 Agent 不需读取会话。
>
> 当前状态：**设计提案 / shadow-only / 未批准交易行为 / 未实现 / 无收益读数**。不得把下述 bar 数、ATR 缓冲或状态阈值当作已验证生产参数。

### R2.1 核心判断

当前问题不是缺少更多指标，而是突破、回踩、反转和趋势单共用偏固定百分比的管理方式。新方向为：

**多级别结构识别 + 结构化 TradePlan + 持仓状态机 + 事件驱动 LLM 管理。**

LLM 负责综合信息、结构解释和状态复核；确定性代码负责灾难止损、组合风险、仓位、价格合法性、订单状态和执行。Playbook 与 DualBrokerAdapter 仍是唯一管理与下单管线，不新建平行执行链。

### R2.2 理论到机制的映射

| 理论 | 借鉴内容 | 可计算表达 | 管理用途 |
|---|---|---|---|
| 利弗莫尔 | 领头羊、关键点、自然回踩、让利润奔跑 | 个股相对 SPY/板块 RS、pivot、突破后接受、最后确认的更高低点 | 趋势延续入场；结构完整时不被固定小回撤机械截断 |
| 简化缠论 | 多级别、中央重叠区、回踩、背驰 | 已确认 swing、HH/HL/LH/LL、重叠区、价格与量能/动量背离 | 区分正常回踩与结构破坏；背驰只作收紧复核，不单独反向 |
| Wyckoff / 拍卖理论 | spring/upthrust、接受与拒绝 | 扫过区间极值后收回、区间外停留时间、回踩再次被拒绝 | 假突破/反转入场与快速失效 |
| Elder 三屏 | 高阶定方向，中阶定形态，低阶定触发 | 60m regime → 15m setup → 5m trigger → 1m 执行 | 防止 1m 噪声直接推翻高阶论点 |
| Van Tharp | 以 R 衡量期望 | 初始结构风险 1R、MFE/MAE(R)、按状态统计净期望 | 判分与尺寸；LLM 不得改写风险预算 |

不实现“全自动标准缠论递归笔段”，避免定义分歧和 repaint；只实现无未来函数的 chan-lite 结构特征。

### R2.3 现有接点，不推倒重来

1. DA 已输出 `BREAKOUT / PULLBACK_LIMIT / FADE_SHORT`（Hermes `llm_synth.py:1340-1360`）。这些类型**不用于准入过滤或配额**（形态轴已有负证据），只用于确定管理语义。
2. DA 已有 `invalidation_triggers`，但指标目前只有收盘价、量比、5日涨跌和盘前涨跌，下游不实平。
3. `app/playbook/invalidation_shadow.py` 已预留 Stage 2b 结构化规则；本设计是其扩展。
4. `app/playbook/adaptive_trailing.py` 当前按 MFE 比例给回撤；结构 trailing 先与它配对影子比较，不直接替换。
5. `app/playbook/pipeline_parts/exit_monitor_flow.py` 已采集 peak/trough/MFE/MAE 并调用 invalidation shadow；新管理器在同一观察点旁路记录。

### R2.4 TradePlan v1 契约

```json
{
  "schema_version": "trade_plan_v1",
  "setup": "BREAKOUT",
  "as_of": "2026-11-18T09:35:00-05:00",
  "regime": {"daily": "UP", "m60": "UP", "sector_rs": "LEADING"},
  "structure": {
    "pivot": 101.20,
    "range_low": 98.40,
    "range_high": 101.20,
    "last_confirmed_swing_low": 99.10
  },
  "entry": {
    "trigger": "m5_close_above_pivot_then_retest_holds",
    "entry_zone": [101.20, 101.55],
    "ttl_bars_5m": 6
  },
  "invalidation": {
    "hard_stop": 98.90,
    "structural": "m5_close_back_in_range_and_failed_reclaim",
    "time": "no_acceptance_within_3_bars_5m",
    "event": "cached_event_risk_rule"
  },
  "management": {
    "trail_anchor": "last_confirmed_m15_swing_low_minus_0.2_atr",
    "on_momentum_divergence": "REVIEW_TIGHTEN_ONLY",
    "overnight": "ONLY_IF_STRUCTURE_INTACT_AND_EVENT_CLEAR"
  },
  "evidence_ids": ["bar:...", "news:...", "sector:..."],
  "uncertainty": "missing_level_2"
}
```

硬校验：

1. 价格=USD，pct=百分点，ATR 倍数无量纲；新增字段必须有基数自检与单位契约测试。
2. 每个条件包含 `metric/operator/value/timeframe/price_basis/known_at`，禁止只写“走弱”“企稳”。
3. re-price 后保存 `planned/effective/fill` 三时点结构锚，不得把旧 pivot 机械平移到新价。
4. LLM 新止损只能向有利方向移动；违反即丢弃动作并告警。
5. 数据不足只能 `ABSTAIN`，由现有确定性规则继续管理。

### R2.5 两类首发管理模板

#### A. BREAKOUT：趋势关键点突破—回踩

利弗莫尔关键点 + 缠论中央区突破/回踩 + 拍卖接受。

1. 60m 同向、板块不拖累、个股 RS 不落后。
2. 15m 已形成重叠区间与明确 pivot。
3. 5m 收盘突破，随后回踩 pivot 不失；不接受单纯瞬时穿越。
4. 初始灾难止损在回踩结构外侧；尺寸仍走现有风险预算。
5. 突破后重新被区间接受且 reclaim 失败，属 FAILED_BREAKOUT，应比统一 60min 死信号更早复核。
6. 若形成连续 HH/HL，进入 TREND，止损跟随最后确认的 15m higher low，而非始终固定 0.8% 回撤。

#### B. PULLBACK_LIMIT / FADE_SHORT：趋势回踩或假突破反转

- 趋势回踩：高阶趋势未坏，低阶回到结构区后出现承接；连续低级别低点下移说明不是正常回踩而是结构破坏。
- 假突破反转：扫过区间极值后重新回区，反弹/回踩再被拒绝才入场；不接尚未出现拒绝的飞刀。
- 失效锚是 sweep extreme 或承接结构，不使用统一百分比。
- 时间退出按“应该多久出现拒绝/承接”定义，而非所有 setup 共用 60min。

以上 bar 数和 0.2ATR 均为**影子初始假设**，不是已证明的生产参数。

### R2.6 持仓状态机

```text
WATCH
  → ARMED               结构条件满足，等待触发
  → WAIT_CONFIRMATION   已入场，等待市场证明
  → TREND               HH/HL 或 LH/LL 延续，使用结构 trailing
  → RANGE               高阶无方向，使用区间失效，不假装趋势
  → FAILED              突破/承接未被接受，快速复核退出
  → THESIS_BROKEN       结构或事件论点失效，确定性退出
```

- `CLIMAX / DIVERGENCE` 是风险标签，不直接等价反向信号。
- 每个 pivot 保存 `confirmed_at`，禁止右侧未来 bar 回填成当时已知。
- LLM 只在状态边界提供判断；10s 快环不调用 LLM。

状态语义：

- `WAIT_CONFIRMATION`：按 setup 时间窗观察接受/承接，不统一等待 60min。
- `TREND`：结构 trailing，让利润奔跑；LLM 识别高潮、背驰和板块退潮。
- `RANGE`：关注区间边界和假突破，不套用趋势单宽 trailing。
- `FAILED`：突破/承接未获接受，快速复核，不自动获得五天修复窗。
- `THESIS_BROKEN`：确定性退出，不等待 LLM 再解释。

### R2.7 LLM 事件驱动盯盘

触发事件：

- 新 confirmed swing、突破/回踩/假突破；
- 价格触及计划失效位或 trail anchor；
- 5m/15m 状态改变、量能异常、板块 RS 翻转；
- 固定 15min 持仓摘要与 EOD 隔夜复核；
- 重大新闻/财报事件（只引用已有事件链）。

30s 合并重复事件，同 symbol 非硬事件 5min 最多一次；灾难止损不等待模型。市场和板块上下文缓存复用，每 symbol 只发送增量 StatePacket。

StatePacket 包含：原 TradePlan、当前状态、confirmed swing、有限 bars、MFE/MAE(R)、板块/市场状态、事件风险、当前 effective SL、报价龄与数据缺口。

LLM 只输出：

```json
{
  "action": "HOLD",
  "state": "TREND",
  "reason_code": "RETEST_HELD",
  "new_stop": null,
  "ttl_bars_5m": 2,
  "evidence_ids": ["swing:...", "bar:..."],
  "confidence_note": "低级别量能不足，但结构未坏"
}
```

允许 `HOLD / TIGHTEN / EXIT / ABSTAIN`；禁止 `ADD / WIDEN / REVERSE`。`confidence_note` 只解释，不参与仓位或门控。LLM 不可用/超时/schema 失败时：`ABSTAIN + 现有规则继续`，不得变成无止损。

### R2.8 隔夜新影子臂

当前 W_WEEK 主要按是否水下决定多夜豁免。新臂比较：

- 不是“亏了所以继续拿”，而是“论点是否仍有效”；
- 不是“盈利所以必须收”，而是“趋势是否完整、事件是否可控”；
- 结构破坏即使亏损也不能借修复窗扛单；结构完整也不能仅因浮盈机械砍掉；
- 任何隔夜仍受组合风险、事件风险和硬止损约束。

该臂不直接替换 W_WEEK，不重开 C14/C17 的全砍/减半隔夜方案。

### R2.9 实施切片

#### Slice 0：纯确定性契约，零 LLM 成本

建议新增：

- `app/playbook/market_structure.py`：无未来函数 swing/区间/状态特征；
- `app/playbook/trade_plan.py`：schema、单位、几何和 only-tighten 校验；
- `app/playbook/position_state_machine.py`：确定性状态转换；
- `app/playbook/thesis_manager_shadow.py`：record-only 决策与反事实台账。

测试：pivot 确认时点、reprice 锚、LONG/SHORT、缺数据、LLM 超时、错误 JSON、放宽止损、重复事件、重启恢复。

#### Slice 1：现有仓位前向影子

- DA prompt 只增加 `management_plan`，不改方向、评分、候选池或入场参数。
- 现有 Playbook 仓仍按原规则出场；影子记录新状态机与 LLM 会做什么。
- 注册表只登记一个 `thesis_management_shadow`，合并失效契约/结构 trailing 研究，不繁衍同轴影子。

#### Slice 2：机制灰度（仍需用户另裁）

先仅允许 `TIGHTEN` 或结构性 `EXIT` 中一种，不同时改入场和尺寸；一键回到 `mode=shadow`。

#### Slice 3：入场触发状态机

只有出场侧机制与净结果均过门后，才把 BREAKOUT/PULLBACK 触发条件接入入场；不同时改仓位、模型和隔夜规则。

### R2.10 判据与回滚

工程门（不宣称 alpha）：

- schema 有效率 ≥99%，结构字段覆盖率 ≥95%；
- only-tighten 违规执行数=0，未来函数案例=0；
- LLM 故障全部落入旧规则，无无保护仓；
- 每次动作可由 `plan_hash + state_hash + evidence_ids` 重放。

机制门：

- FAILED_BREAKOUT 识别不晚于现行 60min DEAD_SIGNAL；
- early-tight 误杀与 OGS 损失分开报告，不能用赢家减少掩盖左尾扩大；
- TREND 组 MFE capture 改善时，净收益和最大单笔亏损不得显著恶化。

收益门：

- 机制灰度样本只证明行为，不证明盈利；
- 收益判断至少 n≥120 配对仓、≥20 ET 日，按日聚类报告 paired Δ、80%CI 与 MDE80；
- paired Δ 的 CI 下界>0 且均值>MDE，才讨论扩大；
- 同期不得改模型、入场、尺寸、费用门或其他退出参数。

回滚：关闭影子无行为；灰度期 `thesis_manager_mode=shadow` 即恢复当前全部出场。

### R2.11 明确不做

- LLM 每 10 秒看一次全市场或直接生成订单；
- 用未来确认的缠论笔段回测当时交易；
- 让 LLM 自由修改硬止损、加仓摊平或反手；
- 同时上线几十种理论/形态，或把 archetype 用作准入配额；
- 用最终赢家/输家文本训练“事后诸葛亮”提示词；
- 第一阶段替换 qwen4、同时重写入出场和尺寸。

## R2.12 请另一 Agent 分析、证伪与补充的问题

请另一 Agent 以**批判性复核**为主，不只写“同意”。至少逐项回答：

1. **未来函数**：怎样定义 confirmed swing、pivot、中央区，才能保证 `known_at/confirmed_at` 在实时与回放完全一致？请给出可测试算法或反例。
2. **同轴重复**：本设计与 C16 形态轴、RFC-188 比例保护、既有 invalidation_shadow 的增量究竟是什么？哪些部分应合并或删除？
3. **状态机必要性**：是否真需要六态？能否简化成更少状态而不损失管理语义？哪些转换会产生振荡/反复横跳？
4. **re-price 语义**：DA 计划价、执行价、实际成交价变化后，pivot/失效位/1R 应如何重锚？请指出本设计的缺失。
5. **多空与跳空**：SHORT 对称规则、隔夜跳空和 ETH 无完整止损时，结构管理会失效在哪里？
6. **W_WEEK 冲突**：论点驱动隔夜与既有用户裁决如何配对，怎样避免变相重开 C14/C17 或“亏损就扛”？
7. **反事实保真**：现有历史回放多次失真，前向影子应记录哪些最小字段，才能计算 paired Δ 而不捏造未成交价？
8. **LLM 价值与成本**：哪些判断确实需要 LLM，哪些应由确定性代码完成？事件频率、token 上限、超时、ABSTAIN 和故障回退还需什么约束？
9. **判据功效**：n≥120/20 日是否足以识别预期效应？应如何预注册主终点、机制终点、停止线和防过拟合约束？
10. **最小实施面**：请给出最小文件/字段/测试清单，并指出 Slice 0 中还应删掉什么，避免做成庞大新框架。

建议输出格式：

- A. 可证伪的批评（按严重度排序，引用代码/数据）；
- B. 对 TradePlan/schema/状态机的具体修订；
- C. 更小的实施切片与验收门；
- D. 仍无法回答、必须前向测量的问题；
- E. 修订后的设计直接追加为本 DEB「第 3 轮」，不要静默改写 R2 原文。

## R3 独立批判复核与修订建议（第 3 轮，2026-11-18，另一 Agent）

> 性质：只读复核。零代码/配置/交易行为变更，零重启，零 LLM 调用。按 R2.12 要求以证伪为主，
> 逐项覆盖 10 个开放问题（映射见文末）。本节为追加，R2 原文未改。所有引用的量化读数均出自
> 本 DEB 第 1/2 轮或 `docs/OKR-2026-11.md`，代码锚点已逐一实核。

### A. 可证伪的批评（按严重度排序）

**A1（高）设计前提与本 DEB 自己的数据错位：TREND 结构 trailing 不是主要矛盾。**
R2.1 的出发点是「固定小回撤机械截断赢家」，但第 1 轮量化显示：赢家本来就来自 trail/BE
（SL_TRAIL_LOCK 143 笔 +$6,083、SL_BREAKEVEN 236 笔 +$1,422），而 SL_INITIAL 34 笔的 MFE
中位仅 ~0.05%——这些单**从未走对过**，early-tight 总体是保护性的（相对持到收盘少亏 ~$2,157），
不是「误杀赢家」。可寻址亏损池在输家侧（SL_INITIAL+EARLY_TIGHT 37 笔 -$4,449、OGS 22 笔
-$6,520），不在赢家侧。修订：把 FAILED_BREAKOUT 提前复核、setup 专属时间止损、论点隔夜复核
列为 P1；TREND 结构 trailing 降为 P2，且必须先证明对现有 adaptive_trailing 赢家捕获非劣
（`app/playbook/adaptive_trailing.py` 按 MFE 比例给回撤，已在产）。否则是在给已经赚钱的
环节换几何，同时放着亏钱的环节不动。

**A2（高）收益门结构性欠功效：n≥120 配对仓大概率永远「不可分辨」。**
稀释算术：可寻址池 37–59 笔 / 494 笔 ≈ 7.5%–12%。即便结构管理把 SL_INITIAL+EARLY_TIGHT
亏损砍掉 30%（$1,335 / 78 个交易日），摊到全体 ITT 也只有 ~$2.7/笔；砍掉 100% 才 ~$9/笔。
OKR §风险节给的单笔噪声 ~$65、MDE80 ~$25–30/笔 ⇒ 要在 ITT 上分辨 $8/笔的效应需要
n≈(2.8×65/8)²≈520 配对仓（日聚类只会更多），约 55+ 个 ET 日。R2.10 的「n≥120 且 CI 下界>0
且均值>MDE」不是保守，是**结构性达不到**。修订：①预注册时如实写预期效应量（个位数 $/笔）
并承认收益门在合理窗口内不可达；②主终点改为机制终点（FAILED 识别提前量、左尾分组损失、
赢家捕获非劣），收益只作 ITT 护栏（不得显著恶化）；③「影子动作≠旧规则」的受影响子集读数
是内生选择（用了入场后信息），只能作机制描述，不得作盈利宣称——这条要写进预注册，防止
未来用子集 t 值冒充 ITT 证据（宪章第 3/9 律）。

**A3（高）多时间级别数据地基不存在，Slice 0 之前缺一个 Slice -1。**
实核：`app/playbook/` 全目录无任何 K_5M/K_15M/K_60M 消费方；盘中指标只聚合 K_1M
（`app/playbook/pipeline_parts/intraday_indicators.py:243,518`，count=800 ≈ 不到 2 个 RTH
日）。「60m regime → 15m setup」需要数日历史，只能走 `request_history_kline` 回填
（`app/data/futu_lv3_provider.py:1616,1729`），受配额闸约束（同文件 :1426 `_wait_kline_quota`、
:1566 日 K 配额）。两个后果：①配额耗尽时结构特征静默退化，覆盖率必须先测量；②历史回填的
bar 会被复权/修订（autype），与实时观测不一致，直接喂回放就是未来函数/口径漂移。修订：
Slice 0 之前先做**只读覆盖率审计**（DA 标的 × RTH 时段 × 5m/15m/60m 可得率、配额余量、
回填 vs 实时快照差异），覆盖率 <95% 则结构特征工程不开工；结构特征一律从**前向记录的
快照**计算，历史回填只许用于研究且显式打 `data_source=backfill` 标。

**A4（高）未来函数的精确定义与可测试算法（答 Q1）。**
①confirmed swing：k 根 fractal（左右各 k=2 根已收 bar），`confirmed_at` = 右侧第 k 根 bar
的**收盘时刻 + 数据到达时延**，不是 pivot 所在 bar 的时刻。反例：10:00 的 5m pivot low 要到
10:10 才可知；回放若把 trail 锚在 10:00 生效即未来函数。②中央区 = 最近 3 个**已确认** swing
的重叠区，同样只随 confirmed_at 逐步可见。③实时侧 `known_at` 必须取本地观测时间：出场报价
允许 120s 缓存（`app/playbook/pipeline.py:3494-3531`），bar 时间戳 ≠ 可知时间戳。④可测试
判据：**前缀不变性**属性测试——对任意 bar 序列，追加未来 bar 不得改变任何历史
`(feature_value, confirmed_at)`；违反即拒收。⑤回放只用前向快照（含 quote age），禁止用
今日回填的复权 bar 重算昨日决策。

**A5（中）同轴重复比 R2.3 承认的更多（答 Q2）。**
①结构 trailing 与 adaptive_trailing/ATR trail 回放同属「trail 几何」轴，本 DEB 第 3 节已列
ATR 放宽全负——结构锚不是新轴，是同一决策面的新输入，必须与 adaptive_trailing **成对**影子
报告，且不得复用已否定候选的切点。②论点隔夜臂与 KILLED 的 RFC-160/B19 共享同一决策面
（EOD hold/drop），增量只在输入（结构 vs MFE/深度）；预注册必须写明「不复用已杀切点、
对 W_WEEK 与 `overnight_hold_shadow` 双双成对报告」，否则就是换皮复活。③TradePlan 契约
不应另起炉灶：`app/playbook/invalidation_contract.py` 已有 schema/单位/文本价解析 +
`REVIEW_ONLY` 惰性契约 + 消费者 `scripts/invalidation_contract_audit.py`，TradePlan v1 应
扩展它（新增 bar-close 类规则类型），而不是新建平行的 `trade_plan.py` 语法。④治理账：
OKR §5 并行实验 ≤5，当前 RFC-155/188/209/DEAD_SIGNAL/EXT 盘前已占满 5 个可决策轨道；
`thesis_management_shadow` 注册时必须声明预期效应量/所需 n/MDE/决策日期，并明确它计入
record-only 研究还是第 6 个实验——若计实验，须等 DEAD_SIGNAL 11-21 结算腾位或用户明示豁免。

**A6（中）状态机是 7 态不是 6 态，且两个态不属于持仓管理（答 Q3）。**
R2.6 列了 WATCH/ARMED/WAIT_CONFIRMATION/TREND/RANGE/FAILED/THESIS_BROKEN 共 7 个。
WATCH/ARMED 是入场前状态，属 Slice 3，混进持仓状态机只会扩大 Slice 0 面积；THESIS_BROKEN
是**终态事件**不是可持续状态。修订：持仓侧 4 态 WAIT_CONFIRMATION/TREND/RANGE/FAILED +
终态 EXITED；FAILED 单向不可回 WAIT（防 pivot 附近反复横跳）；TREND↔RANGE 加滞回：只在
15m 收 bar 确认、最小驻留 N 根 bar、每仓每日状态转换上限（超限遥测+ABSTAIN）。振荡本身
要作为机制门的负向指标落账。

**A7（中）re-price 缺「计划过期」语义（答 Q4）。**
11-17 TCA 实证成交偏离计划 +2.92 ~ +176.72bps（handoff 2026-11-17 批）。缺失：①成交落在
entry_zone 之外或 TTL 已过 ⇒ `plan_status=STALE`，整体 ABSTAIN 回落旧规则，**不许平移
pivot**；②1R 必须以实际成交价对 hard_stop 重算，计划 1R 只作研究字段；③绝对价合法性门：
`invalidation_contract.py` 的血案（文本价离入场锚 15%–833%）同样适用于 LLM 生成的结构价，
TradePlan 硬校验应加「结构价距入场锚 ≤30%」同款距离门；④公司行动（拆股/除息）使绝对价
失效，v1 直接规定计划仅当日 RTH 有效，跨日重生成，不做复权平移。

**A8（中）SHORT 与跳空的具体失效点（答 Q5）。**
①SHORT 借券成本未入常规净账（第 1 轮已认）：论点隔夜臂若让 SHORT 多持一夜，paired Δ 会
系统性高估——借券费率入账或 SHORT 隔夜臂显式排除，二选一先做；②借券召回/locate 失效必须
列为确定性 THESIS_BROKEN 输入，不等 LLM；③结构止损不改变跳空成交物理（OGS 48%–64% 损失
是 gap），对 OGS 的可寻址部分只有「结构坏了就不隔夜」，别把 OGS 全额记进预期收益；
④新管理器旁路记录必须尊重 `blocked_exits` 冻结（`exit_monitor_flow.py:267-279`），冻结窗
内的配对样本记 censored，不得假造出场。

**A9（中）W_WEEK 配对（答 Q6）：只许收紧，不许扩权。**
「结构完整就不因浮盈砍掉」若产生超出 W_WEEK 现有豁免的持仓夜数，就是行为扩权，须用户另裁。
影子期约束：论点臂的持有上限 = 现有 W_WEEK 上限，只测量「提前退出」方向的增量；
「延长持有」方向只记录、不计入任何机制门收益，直到用户明示裁决。亏损扛单由 A6 的 FAILED
单向 + 硬止损兜底堵死。

**A10（低）LLM 增量与结构增量混在一起，归因会被污染（答 Q8）。**
R2.7 的触发事件大半是确定性的（新 swing、触锚、状态改变）——这些 TIGHTEN 到结构锚根本
不需要 LLM。若 Slice 1 一上来就 LLM+结构一起上，过门/不过门都说不清是谁的功劳。修订：
Slice 1a 纯确定性结构影子（零 LLM 成本）先测机制覆盖；Slice 1b 只在确定性规则 ABSTAIN 的
事件上加 LLM，增量干净可归因。真正需要 LLM 的只有：背驰+板块退潮+新闻的多信号仲裁、
EOD 论点复核、FAILED vs 正常回踩的模糊分类。预算护栏：每仓每日调用上限（建议 ≤8）、
超时 ≤5s 硬 ABSTAIN、schema 失败率 >5% 当日自动降级纯确定性并经 `app/push/` hub 告警。

**A11（低）费用与信号现实给 R2 的天花板封顶。**
11 月毛本身为负（-9.25bps），费是长期窗毛利的 4.7 倍。出场管理修不了信号 edge，也修不了
费用；R2 全家桶即使全部过门，天花板也只是「输家侧少亏个位数 $/笔」（A2 算术）。因此：
方案 A 的正确性修复与方案 C 的 Kill/Pivot 裁决仍是关键路径，R2 必须限时限量（建议
Slice -1+0 合计 ≤1 个工程周），不得成为 OKR 明令禁止的「第 95 个影子」。

### B. 对 TradePlan / schema / 状态机的具体修订

1. TradePlan v1 最小字段：`setup / as_of / structure{pivot, range_low, range_high,
   last_confirmed_swing(+confirmed_at)} / invalidation{hard_stop, structural, time} /
   trail_anchor / plan_status / evidence_ids / data_source`。**删**：`entry.trigger`
   （Slice 3）、`management.overnight`（A9 另裁）、`on_momentum_divergence`（背驰检测是
   研究深坑，v1 不做）、`regime` 降为可选研究字段（60m 数据地基未证，A3）。
2. 结构价全部携带 `confirmed_at + known_at + data_source`；校验加 ±30% 距离门与
   only-tighten；`plan_status ∈ {ACTIVE, STALE, ABSTAIN}`，STALE 判定见 A7。
3. 语法复用 `invalidation_contract.py`：新增 `bar_close_below/above`（timeframe、
   price_basis、known_at 必填）规则类型，编译产物仍 REVIEW_ONLY，新动作类型另批另裁。
4. 状态机 4+1 态、FAILED 单向、15m 收 bar 确认 + 最小驻留 + 每日转换上限（A6）。
5. DA 侧 `management_plan` 只用**前一 RTH 日及以前**的 bar 生成结构（DA 是盘前产物；
   R2.4 示例 as_of=09:35 与 DA 实际生成时点不符）；盘前薄流动性打印不得参与 swing 确认。

### C. 更小的实施切片与验收门（答 Q9/Q10）

- **Slice -1（新增，只读）**：多时间级别数据覆盖率 + 配额 + 回填一致性审计；产出覆盖率
  报告；<95% 不开工 Slice 0。顺带结算 `blocked_exit_opportunity` 影子已攒的 N/M
  （同一观察点，先结旧账再开新账）。
- **Slice 0（收缩为 2 个新文件 + 1 处扩展）**：`app/playbook/market_structure.py`（纯函数
  + 前缀不变性属性测试）；`app/playbook/thesis_manager_shadow.py`（状态转换表内嵌，不单独
  建 `position_state_machine.py`）；扩展 `invalidation_contract.py`（不新建 `trade_plan.py`）。
  注册表恰 1 条 `thesis_management_shadow`，带消费者脚本、预期效应量、n、MDE、决策日期。
  测试清单：前缀不变性、LONG/SHORT 对称、reprice STALE、only-tighten、±30% 距离门、
  重启恢复（含 SHORT trail 状态只紧不松）、LLM 超时/坏 JSON→ABSTAIN、单位契约。
- **Slice 1a**：纯确定性 record-only 影子。机制门：FAILED 识别提前量 vs 60min DEAD_SIGNAL
  与 vs early-tight 触发时刻分别报告；early-tight 组与 OGS 组分账；赢家捕获（trail/BE 组
  MFE capture）非劣；状态振荡率上限。**收益门改为 ITT 护栏**：配对净 Δ 不得显著恶化，
  同时如实披露 A2 的功效算术。
- **Slice 1b**：仅在确定性 ABSTAIN 事件上加 LLM，测 LLM 增量；成本/故障率遥测先行。
- **Slice 2/3**：维持 R2 原案（用户另裁；一次一变量），但 Slice 2 首发只允许 FAILED/时间
  止损类 EXIT，不动 TREND trailing（A1）。
- 反事实保真最小字段（答 Q7）：每次决策落 `plan_hash/state_hash/feature 快照(含 known_at,
  quote_age_s, data_source)/effective_sl 前后值/旧规则同 tick 的对照动作`；后续价格路径用
  **前向连续记录的 1m 流**，禁止事后回填；模拟成交 touch≠fill（要求穿越或保守限价假设）；
  费用走同一估算器并带 `fees_source`；数据缺口/blocked 窗记 censored 不插补；配对在
  **决策级**对齐后再聚合到仓级。

### D. 仍无法回答、必须前向测量的问题

1. LLM 相对确定性结构规则的净增量（Slice 1b 之前无解）。
2. FAILED_BREAKOUT 实际能比 early-tight/60min 提前多少、提前是否省钱（MFE 0.05% 的单
   早退未必优于早收紧）。
3. 结构锚 vs adaptive_trailing 的赢家捕获差异（历史回放已四次失真，只能前向）。
4. 论点隔夜臂对 W_WEEK 的成对增量（含 SHORT 借券成本入账后）。
5. 5m/15m/60m 在配额约束下的真实覆盖率与事件频率/LLM 成本（Slice -1 实测）。
6. 一切收益读数：qwen4 post cohort 未定型前，任何出场侧读数都带模型混杂。

### E. 落档声明与 R2.12 十问映射

- 本节即「第 3 轮」追加；R2 原文未动。状态不变：设计提案 / shadow-only / 未批准交易行为。
- 十问映射：Q1→A4；Q2→A5；Q3→A6；Q4→A7；Q5→A8；Q6→A9；Q7→C 保真段；Q8→A10；
  Q9→A2+C；Q10→B+C。
- R2 之外补充优化点（执行/测量，均 record-only 或属方案 A 既有项）：①出场决策落账补
  `quote_age_s` 字段，把 120s 陈旧报价的实际出场占比测出来（先测量后议门，不与已发生
  PnL 混写）；②LLM/结构 EXIT 若未来灰度，必须复用现有 fee-aware 限价地板机制
  （`exit_engine.py` BE 净保本线注释，2026-08-20 方案 A），不得绕开订单类型保净的既有设计；
  ③`be_geometry_degenerate` 遥测分组结算并入 RFC-188 前向研究，不临时加门（维持第 1 轮
  结论）；④注册表 ≤5 实验名额账（A5-④）在注册前先对用户明示。
- 待用户裁决项不变且仅一项：是否按收缩后的 Slice -1/0 开工（纯确定性、零交易行为）。

## R4 主会话独立复核：有条件确认 R3，追加三项更正与两项边界（第 4 轮）

> 性质：只读独立复核，零代码/配置/交易行为变更，零重启，零新增 LLM 调用。复核方式不是阅读后表示同意，而是重跑主窗 SQL、post-exit 反事实脚本，并核查多时间级别数据路径、`invalidation_contract.py` 与现役模型延迟证据。

### R4.1 独立复跑凭据

```bash
sqlite3 -readonly -header -column data/ftseek_us.db \
  "SELECT COUNT(*), COUNT(DISTINCT trade_date), MIN(trade_date), MAX(trade_date), SUM(pnl_net) \
   FROM trade_records \
   WHERE source_type='PLAYBOOK_SIM' AND trade_date>='2026-09-01' AND pnl_net IS NOT NULL;"

./venv/bin/python -I /tmp/exit_audit5.py
```

实测：

- 主窗 **n=494、55 个 ET 交易日、2026-09-01→2026-11-17、净 -$2,309.31**；R3 A2 的“78 个交易日”应为 **55**（78 是近似自然日跨度，不是交易日）。
- 左尾池复现：SL_INITIAL 34 笔 -$3,997.36、EARLY_TIGHT_SL 3 笔 -$451.58、OGS 22 笔 -$6,520.13。
- post-exit 可配对 478/494。hold-to-close delta：全体 **-$2,157**（负值=实际出场更好），SL_INITIAL **-$1,117**，EARLY_TIGHT_SL **+$192**，两类合计约 **-$925**；SL_TRAIL_LOCK -$1,572、SL_BREAKEVEN -$962、DEAD_SIGNAL +$532、OGS +$627。全体日聚类 t=-0.53，仍不显著。
- `app/playbook/` 确实没有现成 K_5M/K_15M/K_60M 消费方；当前盘中指标聚合 K_1M、count=800，Futu 历史 K 线有配额闸。R3 的 Slice -1 数据地基审计成立。
- `invalidation_contract.py` 已有 REVIEW_ONLY、单位、±30% 文本价距离门和审计消费者，复用而非新建平行语法成立。
- 当前 qwen4 全量 DA 探针延迟约 358.7–501.2s、生产 timeout=900s；这不是 compact 管理调用的直接延迟，但足以说明 R3 的“5s 硬超时”没有实测支撑。

### R4.2 予以确认的 R3 修正

1. **优先级重排成立**：FAILED/时间失效/论点隔夜为 P1，TREND 结构 trailing 降为 P2；先保护当前 trail/BE 利润引擎，不同时改赢家几何。
2. **Slice 1a → 1b 成立**：先纯确定性结构影子，再只在 ABSTAIN 事件上测 LLM 增量，避免“结构+LLM”无法归因。
3. **无未来函数定义成立**：confirmed_at 必须包含右侧 bar 确认与到达延迟，并做前缀不变性测试；历史回填不得冒充前向快照。
4. **4+1 态成立**：持仓侧 WAIT_CONFIRMATION/TREND/RANGE/FAILED + EXITED；WATCH/ARMED 属入场侧，THESIS_BROKEN 是终态事件。
5. **复用 invalidation_contract 成立**：扩展 bar-close 规则，不另建平行 TradePlan 语法。
6. **STALE 与 fill 1R 成立**：entry_zone/TTL 失效则 ABSTAIN，禁止平移 pivot；实际 1R 以 fill 对 hard_stop 重算。
7. **W_WEEK 只收紧不扩权成立**：影子先测提前退出，延长持有只记录、不进入机制收益。
8. **终点修正成立**：机制终点与 ITT 护栏分列；受影响子集用了入场后信息，只能作机制描述，不能宣称盈利。

### R4.3 三项更正（R4 优先于 R3 对应文字）

1. **交易日数**：R3 A2 的“78 个交易日”统一读作 **55 个 ET 交易日**。
2. **early-tight 归因金额**：R3 A1 的“early-tight 相对持到收盘少亏约 $2,157”不准确。-$2,157 是全部出场规则总效果；SL_INITIAL 子集为 -$1,117，EARLY_TIGHT_SL 为 +$192，合计约 -$925。早期紧 SL 具有保护性这一**方向结论仍成立且统计不显著**，但不得挪用总出场效果作其单独证据。
3. **LLM 5s 超时**：删除作为硬门的预定值。改为：Slice 1b 先用 compact StatePacket 做延迟探针，记录 p50/p95、schema 失败和事件 TTL；只有在 p95 延迟小于事件 TTL 时才允许动作生效，迟到动作按 `state_hash` 过期丢弃并 ABSTAIN。现役全量 DA 延迟只作风险参照，不冒充 compact 调用实测。

### R4.4 两项边界

1. R3 A2 的 `n≈520` 只可作**功效不足示意算术**，不能作为正式样本门：它用单笔 $65 噪声和假想 $8 效应估算，尚无 paired decision-level 方差，也不是日聚类 MDE。正式预注册必须先写最小可接受效应，再用 Slice 1a 的配对增量估计 MDE；不得事后用 120 或 520 挑一个好看的门。
2. Slice -1 的覆盖率 <95% 应阻止**真实数据结构影子接线**，不必阻止纯函数、合成数据与前缀不变性测试开发。这样可先完成确定性契约而不消耗生产数据配额，但只有覆盖率和回填一致性过门后才允许注册前向收益判分。

### R4.5 最终确认

**R3 设计方向与收缩方案确认可用，但必须连同 R4 更正一起阅读。** 当前仍仅为设计提案，未批准任何交易行为。建议下一步若获用户批准，只实施收缩后的 Slice -1/0：纯确定性、record-only、无 LLM 收费调用、无生产出入场参数变化；Slice 1b 的 LLM 调用和任何灰度均需另裁。

## R5 可落地垂直切片：先做 PULLBACK_LIMIT 承接失败，不造完整理论引擎（第 5 轮）

> 用户认可值得尝试，但要求说明“如何判断、如何决策、如何执行”，并避免造无法实现的轮子。本轮进一步核查现有字段覆盖与真实执行链，给出最小可实现版本。仍是设计/影子，未改代码或交易行为。

### R5.1 为什么首个实验不能选 BREAKOUT

只读 SQL 实查 `playbook_instructions.raw_instruction_json`（2026-09-01 起）：

| archetype | 指令数 | 已平仓可联接数 | 已平仓净额 |
|---|---:|---:|---:|
| PULLBACK_LIMIT | 597 | 272 | -$1,200.59 |
| FADE_SHORT | 193 | 86 | +$101.53 |
| BREAKOUT | 12 | 6 | -$58.29 |
| MISSING | — | 54 | -$734.11 |

BREAKOUT 虽有 pivot，但 55 个 ET 日仅 6 笔已平仓，收益影子在合理窗内无法攒样。**首个垂直切片改为覆盖率最高的 PULLBACK_LIMIT**，不是先做 R2 设想中的突破引擎。

PULLBACK_LIMIT 的 272 笔已平仓全部有 `confirmation.price_level`；实际 fill 距 pivot：≤1%=125 笔、≤2%=205、≤5%=267。主分析臂用既有 re-anchor 1% 尺作 ACTIVE，超出者 STALE/ABSTAIN；2%/5% 只作覆盖披露，不能事后挑更好看的臂。

### R5.2 第一版只做一件事

**识别 PULLBACK_LIMIT 的“承接失败”，暂不做 BREAKOUT、60m、完整缠论、TREND trailing、隔夜扩权或 LLM。**

输入全部已有：

- `entry_archetype` 与 `confirmation.price_level`：`playbook_instructions.raw_instruction_json`；
- 实际 fill、hard SL、方向：`trade_records`；
- 现有 K_1M：聚合为 5m，不新增 K_5M/K_15M/K_60M 订阅；
- 当前报价、quote source/age、blocked_exits：现有退出监控上下文。

不复用 SL/TP 反推出来的 `key_levels` 冒充 DA pivot；只使用原始 `confirmation.price_level`。缺失或畸形即 ABSTAIN。

### R5.3 判断规则（纯函数、LONG/SHORT 镜像）

主分析臂先满足：

1. `entry_archetype=PULLBACK_LIMIT`；
2. pivot 为正且距 fill ≤1%，计划仍属同一 RTH 日；
3. 只使用 fill 后已经收盘并实际到达的 1m bar 聚成的 5m bar；
4. hard SL、现有 SL 兜底和 blocked_exits 优先级高于新规则。

LONG 的承接失败候选：

- 第 1 根完整 5m bar 收在 pivot 下方；
- 第 2 根完整 5m bar 仍收在 pivot 下方，且 `high < pivot`、`low < 第1根 low`；
- 第 2 根的收盘+到达时刻才是 `known_at`，不得把动作回填到第 1 根或 pivot 所在时刻。

含义：价格不是瞬时穿价，而是连续在 pivot 下方被接受，第二次尝试也无法 reclaim，并创更低低点。

SHORT 完全镜像：第 1 根收在 pivot 上方；第 2 根仍收上方、`low > pivot`、`high > 第1根 high`。

输出只有：

- `FAILED_RECLAIM`：记录结构性 EXIT 影子；
- `WAIT`：证据不足，继续旧规则；
- `STALE/ABSTAIN`：计划过期、字段缺失、bar 不足、报价异常或状态不匹配，继续旧规则。

第一版不做“跌到 pivot 下方就卖”，不做 TIGHTEN，不猜后续趋势。该规则是否太紧只能由前向配对结果判定。

### R5.4 最小代码面（2 个新文件 + 1 处契约扩展 + 1 个旁路钩子）

1. `app/playbook/pullback_failure.py`：纯函数，负责 1m→5m 聚合、STALE 和 FAILED_RECLAIM 判定、known_at；不访问 DB、不调用 LLM、不下单。
2. `app/playbook/thesis_manager_shadow.py`：按 trade_id 缓存原指令 pivot、构造决策行、写 record-only JSONL；无执行权限。
3. 扩展 `invalidation_contract.py`：登记 `bar_close_below/above` 和 `reclaim_failed` 的规则语义，仍编译为 REVIEW_ONLY；不新建 TradePlan 语法。
4. `exit_monitor_flow.py` 旁路：在现有报价和 blocked_exits 后调用影子；默认 off，异常只告警，绝不影响现有出场。

不需要：`position_state_machine.py`、完整 `market_structure.py`、新数据库、新券商适配器、新订单类型、新消息推送通道。

### R5.5 决策到执行的真实调用链

影子期：

```text
trade_records + playbook_instructions
  → 取 archetype/pivot/fill/hardSL
  → 现有 K_1M 聚合 5m
  → pullback_failure 纯函数
  → 影子决策 JSONL（FAILED_RECLAIM/WAIT/STALE）
  → 前向 1m 路径结算 paired Δ
  → 现行 Playbook 出场完全不变
```

若未来机制与收益均过门、且用户另裁灰度，执行仍只走：

```text
FAILED_RECLAIM
  → 校验 plan ACTIVE / state_hash 未变 / quote 可用 / 非 blocked / 非部分成交冲突
  → exit_reason=THESIS_INVALIDATED，reason_code=PULLBACK_RECLAIM_FAILED
  → 现有 _playbook_exit_decision(...)
  → 现有 DualBrokerAdapter.close_position(...)
  → 现有 ExitLifecycle durable intent/legs
  → 券商成交回报
  → 现有 _book_playbook_exit_result
```

**不让新模块直接调用 Futu，不另写平仓、部分成交、重启恢复或账本逻辑。** blocked/报价异常/状态已变化时 ABSTAIN 并记 censored，不重复发单。新 exit_reason 只用于归因，硬 SL 和现有规则优先级不变。

### R5.6 影子结算不捏造成交

每次决策至少记录：`trade_id/plan_hash/state_hash/pivot/fill/hardSL/direction/known_at/bar1/bar2/quote_price/quote_age_s/quote_source/effective_sl/旧规则同tick动作/blocked或stale原因`。

- 影子 EXIT 不以 5m 收盘价假装可成交；参考价用决策后下一根实际到达的 1m bar，并标记保守成交假设。
- 后续路径只用前向连续记录，blocked/缺报价窗记 censored。
- 费用走现有同一估算器；不把 touch 当 fill，不把子集 t 值当 ITT 盈利。
- 先报触发覆盖率、相对 early-tight/DEAD_SIGNAL 的提前量、误杀和左尾，再报 paired net Δ。

### R5.7 停止线与后续扩展

- 若 pivot ACTIVE 覆盖不足、5m bar 覆盖不足或 FAILED_RECLAIM 几乎不触发，则停止，不继续造完整结构引擎。
- 若提前量存在但 paired Δ 为负或误杀 winners，则停止，不通过改 pivot/连续 bar 数网格救方案。
- 只有机制方向成立且 ITT 不恶化，才进入 R3/R4 的收益评估。
- 后续扩展顺序仍为：PULLBACK_LIMIT 承接失败 → FADE_SHORT 镜像 → 其他 setup → 仅在 ABSTAIN 事件测 LLM；不回到一次性全家桶。

## R6 对 R4/R5 的再确认与一项改变切片排序的新证据（第 6 轮，2026-11-18，另一 Agent）

> 性质：只读复核，零代码/配置/交易行为变更，零重启。结论先行：**R4/R5 已收敛，值得补充的
> 只剩下列小点；其中 R6.2 的臂内亏损分解是新证据，建议据此修订 R5.7 的扩展顺序。不需要也不
> 应该再造任何新框架。**

### R6.1 独立复核凭据（可复跑）

- 重跑 R5.1 联接 SQL（`trade_records.metadata_json.playbook_instruction_id` →
  `playbook_instructions.raw_instruction_json`，2026-09-01 起、`pnl_net` 非空）：
  PULLBACK_LIMIT 272 笔 -$1,200.59 / has_pivot 272 / ≤1% 125；FADE_SHORT 86 笔 +$101.53 /
  ≤1% 19；BREAKOUT 6 笔 -$58.29 / ≤1% 2。**与 R5.1 逐值一致。**
- R5.5 执行链函数实存：`_playbook_exit_decision` / `_book_playbook_exit_result`
  （`app/playbook/pipeline_parts/exit_monitor_flow.py:468,490` 等）、durable intent 既有。
- K 线缓存实核：`_kline_cache_ttl=60`（`app/data/futu_lv3_provider.py:399`，M6 已从 300s
  缩到 60s）；push K 线缓存保留最近 300 根 1m（同文件 :762-764）。R5 用现有 K_1M 聚 5m
  的到达延迟上界 ≈60s+10s 环，**不是** 300s；known_at 按到达时刻记录的契约不变。
- 联接覆盖披露：494 笔已平仓中 418 笔可联接（-$1,891.46，与 R5.1 表合计一致），
  **76 笔无 instruction_id（净 -$417.85）**+ 54 笔可联接但 MISSING，均不在任何 archetype
  分析内。R5.1 的 MISSING=54 口径正确，但 DEB 应显式披露这 76 笔，防止未来把 418 当 494。

### R6.2 新证据：ACTIVE 臂（PULLBACK_LIMIT ≤1%，125 笔）亏损分解 ⇒ 扩展顺序应改

只读 SQL（同 R6.1 联接 + 臂过滤，按 exit_reason×direction 聚合）：

| exit_reason | n | 净额 | 平均持仓(分钟) |
|---|---:|---:|---:|
| SL_BREAKEVEN | 48 | +$379.09 | 531.6 |
| SL_TRAIL_LOCK | 41 | +$1,496.68 | 184.3 |
| DEAD_SIGNAL_EXIT | 13 | +$21.44 | 263.9 |
| OVERNIGHT_GAP_STOP | 10 | **-$2,538.46** | 4071.8 |
| SL_INITIAL | 10 | -$864.48 | 48.3 |
| PRE_CLOSE_EXIT | 3 | +$423.16 | 369.0 |

臂净 ≈ -$1,082.57，**全为 LONG**（SHORT 镜像在 PULLBACK_LIMIT 臂内零样本，只能作代码
对称性，不得宣称已测）。三个推论：

1. **首个切片的真实天花板比 R3 A2 算术更小**：臂内早期失败池只有 SL_INITIAL 10 笔
   -$864（EARLY_TIGHT_SL 0 笔）；FAILED_RECLAIM 需 fill 后两根完整 5m bar（最早 ~10–15min），
   救不了已存活到隔夜的单。即便砍掉该池 30%–50%，≈$5–8/日 ≈ ITT $0.6–0.9/笔。
   **预注册必须写明：本切片只可能过机制门，收益门在合理窗内不可达**（与 R4.4 一致，
   但现在有臂级数字支撑，不是示意算术）。
2. **臂内 74.6% 的毛亏损在 OGS**（10 笔 -$2,538，平均 ~68 小时 ≈ 多夜持有，形态与
   W_WEEK 水下豁免一致）。这正是 R3 A1 列为 P1 的「论点隔夜」问题，而 R5 把它推迟了。
   推迟本身没错（一次一变量），但 **R5.7 的下一步「FADE_SHORT 镜像」应让位**：FADE_SHORT
   ≤1% 臂仅 19 笔、整体 +$101，没有可寻址亏损池；数据支持的第二个切片是
   **PULLBACK_LIMIT 的 EOD 论点复核（record-only、只收紧不扩权，R4.2-7 约束不变）**，
   对 W_WEEK 与 overnight_hold_shadow 双配对。是否改序由用户裁，但排序依据应落档。
3. **机制门有了具体锚**：SL_INITIAL 平均 48.3min 出场 vs FAILED_RECLAIM 最早 ~10–15min
   + ≤60s 缓存 + 10s 环 ⇒ 提前量 ~30min 量级是可测的；预注册主终点建议直接定为
   「相对该臂 SL_INITIAL/DEAD_SIGNAL 实际出场时刻的提前量分布 + 对 89 笔 BE/trail 赢家的
   误杀率」，而不是任何净 Δ。

### R6.3 三项小补充（不改 R5 结构）

1. **触发基率风险要前置披露**：PULLBACK_LIMIT 是限价单，LONG fill 本就发生在 pivot 处或
   下方，「第 1 根 5m 收在 pivot 下方」基率天然偏高；真正的过滤器是第 2 根 `high<pivot`
   + 更低低点。前向第一周必须先报触发率与赢家误杀率，再谈任何机制结论；若触发率过高
   （如 >50% 活跃臂仓），按 R5.7 停止线停，不改 bar 数网格救。
2. **重启即 censored**：`thesis_manager_shadow` 的 bar1/bar2 按 trade_id 内存缓存，重启
   丢失后不得用事后回填 bar 冒充当时到达（H-12 教训）；要么从已落 JSONL 重建且保留原
   known_at，要么该仓记 censored。测试清单加这一条。
3. **报价异常分支复用既有事实**：120s 缓存/30s 告警（`pipeline.py:3494-3531`）与
   blocked_exits 冻结（`exit_monitor_flow.py:267-279`）照 R5.6 记 censored 即可；
   `quote_age_s` 字段随决策行落账，顺带把 R3-E① 的测量补上，不另立议题。

### R6.4 结论

- R4 三项更正、两项边界全部接受（55 交易日、-$925 归因、LLM 延迟探针替代 5s 硬门、
  n≈520 仅示意、覆盖率门只拦接线不拦纯函数开发）。
- R5 垂直切片**确认可实施且是最小面**；待用户裁的仍是同一件事：按 R5.4 的
  2 文件+1 扩展+1 旁路开工（默认 off、record-only、零 LLM 成本）。
- 唯一建议修订：R5.7 扩展顺序按 R6.2 数据改为「PULLBACK_LIMIT 承接失败 →
  PULLBACK_LIMIT EOD 论点复核（另裁）→ FADE_SHORT 镜像 → …」，并把 R6.2 的臂级天花板
  写进预注册，防止未来对本切片抱收益期望。
- 除此之外**不再补充**。继续加设计就是造第 95 个影子，违反 OKR §1。

## R7 主会话复核确认与最终执行顺序（第 7 轮）

> 性质：只读复核。独立重跑 R6 ACTIVE 臂 SQL、 winners 口径和无 instruction_id 披露，实读 K 线缓存代码。零代码/配置/交易行为变更。

### R7.1 对 R6 的确认

- ACTIVE 臂 125 笔，净 **-$1,082.57**；SL_INITIAL 10 笔 -$864.48、OGS 10 笔 -$2,538.46，OGS 占两类主要毛亏损 **74.6%**，全部为 LONG。
- 无 instruction_id 76 笔、净 **-$417.85**；418 笔可联接不等于全部 494 笔，披露必要。
- K_1M push 缓存保留最近 300 根，HTTP K 线缓存 TTL=60s；R5 用既有 1m 聚合 5m 的路径有现实数据基础。
- 因而确认 R6 的切片改序：**PULLBACK_LIMIT 承接失败 → PULLBACK_LIMIT EOD 论点复核（另裁）→ FADE_SHORT 镜像 → LLM ABSTAIN 增量**。每次只推进一个变量。

### R7.2 两点小修正

1. R6 的“89 笔 BE/trail 赢家”应改为：**89 笔利润保护出场，其中 86 笔实际盈利**。ACTIVE 臂全部实际盈利仓为 **97 笔**（BE 45 + trail 41 + DEAD 8 + PRE_CLOSE 3）。机制门应以这 97 笔的实际 `pnl_net>0` 人群监测误伤，不以 exit_reason 代替赢家标签。
2. “60s 缓存 + 10s 环”只能称典型延迟预算，**不是严格到达上界**。网络、配额、bar 收盘和实际到达都可能增加延迟；决策仍以实际到达时间写 `known_at`，迟到或 state_hash 不匹配即 ABSTAIN。

### R7.3 最终执行计划（通俗版）

**第一步，只观察，不动仓。** 对 PULLBACK_LIMIT，取 DA 已存在的 pivot，把现有 1m 聚成 5m。LONG 第一根收在 pivot 下，第二根仍收下方、最高也没回 pivot 且创更低低点，就记 `FAILED_RECLAIM`；SHORT 只做代码镜像，ACTIVE 臂暂无 SHORT 实证。

**第二步，回答四个问题，不谈盈利神话。** 新规则覆盖多少仓、比 early-tight/DEAD_SIGNAL 早多少、会不会误伤 97 笔实际盈利仓、扣费 paired Δ 是否改善。触发过少/过多、覆盖不足、误伤或 paired Δ 为负即停止，不网格救参。

**第三步，若日内机制成立，再单开 EOD 论点复核。** ACTIVE 臂主要亏损在 OGS，因此第二个切片检查“EOD 时原论点是否已坏”，只测提前退出，不延长持有、不放宽止损、不直接替换 W_WEEK。EOD 结构没坏不等于次日不会跳空，不能把 OGS 全额记成可拯救收益。

**第四步，仍不直接让 LLM 交易。** 只有确定性规则无法判断的模糊事件才测试 LLM；超时、迟到、坏 JSON 都 ABSTAIN，旧规则继续。

**第五步，未来若另裁灰度，只复用现有执行。** `THESIS_INVALIDATED → _playbook_exit_decision → DualBrokerAdapter.close_position → ExitLifecycle → _book_playbook_exit_result`。不新写券商、订单、部分成交、重启恢复或账本。

本 R7 只是确认执行底稿，未批准灰度，未修改生产行为。

## R8 复核确认（第 8 轮，2026-11-18，另一 Agent）

- 只读复跑 ACTIVE 臂赢家口径 SQL：125 笔中 `pnl_net>0` 共 **97 笔**（BE 45/48、
  trail 41/41、DEAD 8/13、PRE_CLOSE 3/3；SL_INITIAL 与 OGS 各 0），净 -$1,082.57。
  **R7.2 两点修正逐值成立**：误伤监测以 97 笔实际盈利仓为准，不用 exit_reason 代标签；
  60s+10s 只是典型延迟预算，known_at 按实际到达、迟到/状态不匹配即 ABSTAIN。
- 确认 R7.1 改序为最终顺序（取代 R5.7 原文的 FADE_SHORT 先行）；R7.3 五步执行底稿无异议。
- **本轮无新增设计点，讨论收敛。** 待用户裁的仅一项：是否按 R5.4 最小面开工
  （默认 off、record-only、零 LLM 成本），预注册按 R6.2 臂级天花板 + R7.2 赢家口径写。

## R10 落地后查漏审计：2 处实锤问题 + 3 处缺口（第 10 轮，2026-11-18，另一 Agent）

> 性质：只读审计 + 纯函数离线复现，未改任何代码/配置，未重启。对象：R9 已部署批
> （未提交 WIP）。结论：**record-only 安全面合格，但判定语义有 2 处必须在下一 RTH
> 开盘（21:30 CST）前处理的问题**，否则首批前向数据带病。

### R10.1 核实合格项（简列）

- 无执行路径：`execution=RECORD_ONLY`、契约恒 REVIEW_ONLY、钩子异常隔离、不改 SL/TP/
  出场决策；`enabled` 代码默认 false、热重载可停采。
- 聚合只收完整 5m 窗口；前缀不变性、跨日/gap/超龄 ABSTAIN、SHORT 镜像、重启 metadata
  去重均有测试；`tests/test_thesis_management_shadow.py` 实跑 **12 passed**。
- K 线口径声明属实：`_track_bar_peak_trough`（`pipeline.py:2631`）确用 autype=NONE，
  影子同源同口径；`latency_sensitive=True` 只读缓存+后台刷新，不阻塞出场快环、不耗配额。
- 注册表 count_mode=distinct inst_id 有真计数测试；报表拒覆盖、无盈利宣称。
- 服务 active、MainPID=3374726 与 R9 一致；13:58 重启后主日志 ERROR/CRITICAL/Traceback
  计数 0；休市无影子行（符合预期）。

### R10.2 实锤问题（按严重度）

**P1-1 bar1 混入入场前数据（已离线复现）。**
`pullback_failure.py` 的 `first_allowed = entry+1min` 比较的是**窗口 end 标签**而非成员
bar 标签。复现：entry=09:36:10，窗口 end=09:40（成员 09:36–09:40）被接为 bar1——其中
09:36 标签 bar 覆盖 09:35:00–09:35:59，**完全在 fill 之前**；09:37 标签 bar 也含 fill 前
tick。违反 R5.3-3「只使用 fill 后已收盘的 bar」。后果：检测最多提前一个窗口（~5min）、
bar1 OHLC 被入场前价格污染，提前量与误伤读数失真。现有测试 entry=09:55 远离窗口边界，
恰好测不到。**最小修法**：沿用同一 end_minute 公式算 entry 所属窗口 end，要求
bar.end 严格大于它（即整窗排除含 fill 分钟的窗口）；补 entry 在窗中/恰在边界两个钉子测试。

**P1-2 判定语义偏离预注册：滚动最近两窗 vs fill 后第 1/2 根。**
R5.3/R7.3 写的是「第 1 根完整 5m bar…第 2 根…」，即 fill 后**头两根**完整窗；实现取
`windows[-2:]`（当日任意时刻最近两根，测试 entry 09:55 / 决策 10:12 即为此语义）。
滚动版会在入场数小时后、对曾走对的仓触发 FAILED_RECLAIM，误伤 97 笔盈利仓的暴露面
显著大于头两根版，且与「相对 early-tight 提前量」主终点不再同构。**此项须用户裁**：
a) 改回头两根（推荐，贴合预注册、误伤面最小）；b) 明示改为滚动语义并修订预注册与
主终点定义。不得静默保留现状。

### R10.3 缺口（不阻塞观测，阻塞结算/读数）

1. **前向价格路径无记录方**：paired Δ 结算需要决策后连续 1m 路径（R5.6），现无任何
   模块记录；首个 FAILED_RECLAIM 出现后再回填即违反保真契约。最小补法：影子每轮已拉
   K_1M，顺手把「新到达」的 1m bar 追加到按 trade_id 的路径 JSONL（零额外配额），
   须在首次结算宣称前落地。
2. **quote_age_s 仅 cached_fallback 有值**：batch/push/single_fallback 轮均为 None，
   R5.6 要求逐决策报价龄；可从报价时间戳补算，低优先。
3. **blocked 冻结窗无 censored 标记**：blocked_exits 在钩子前 continue，冻结期零行，
   报表无法区分「没触发」与「被冻结」；至少在报表 scope 注明按缺口 censored 处理。
4. 治理小项：注册表条目缺**决策/复核日期**（OKR §5 实验四要素之一）与 ≤5 实验名额
   归属说明（R3 A5-④）；建议补「首个 RTH 后 10 ET 日复核 ≈2026-12-02」与名额口径。

### R10.4 处置建议

- P1-1 属已批 record-only 范围内的正确性修复：纯函数 ~3 行 + 2 测试 + 重启两核 + 冒烟，
  建议下一 RTH 前完成；P1-2 须先裁语义再动代码。两者都只影响影子，不影响交易行为。
- 缺口 1 在首次结算前补齐；2/3/4 可随下一批顺手，不单独立项。
- 本批未改代码；R9 批 WIP 归属不变，修复授权待用户裁。

## R13 对 R12 修正批的再核查：两项实锤修复均验证通过，无新增阻塞问题（第 13 轮，2026-11-18，另一 Agent）

> 只读核查 + 纯函数离线复现，零代码改动。对象：R12 修正批（未提交 WIP，14:51:05 重启）。

### R13.1 逐项验证

- **P1-1 已修且复现通过**：`_entry_window_end` 整窗排除 fill 所属 5m。复现 entry=09:36:10
  ⇒ bar1.end=09:45、bar2.end=09:50（09:40 污染窗被排除）；新增边界钉子测试在案。
- **P1-2 已按用户裁改头两根**：`windows[0:2]`；复现「头两根干净 + 数小时后出现失败形态」
  ⇒ WAIT（滚动不再出信号）；`test_later_rolling_windows_never_replace_fill_first_two` 在案。
- **前向路径已落地**：`thesis_management_path.jsonl` 只追加 fill 窗口后、本次实际可见的
  1m bar，带 known_at/ingest 标记，metadata 游标跨重启去重，path_only 轮也持久化；
  决策不变仍续记路径。结算时按 (trade_id, time) 去重即可防 initial_snapshot 重复。
- **quote_age 真值**：优先 quote_age_ms，其次既有 SSOT `_measured_quote_age_ms`
  （`futu_lv3_provider.py:269` 实存），cached_fallback 兜底；不再伪填。
- **blocked CENSORED**：冻结分支显式落行、按 intent 去重；commit 为逐行模式
  （与 :472/:971 既有风格一致），无共享 conn 批量提交风险。
- **注册表**：复核日 2026-12-02 + KR4 名额归属已写，且如实声明存量超编、复核前须腾位
  或用户明示豁免——诚实，保留为待办。
- 测试实跑 **16 passed**（含边界/头两根/路径/censored 新钉子）；服务 MainPID=3377559、
  ActiveEnter=14:51:05 与 R12 一致；重启后主日志 ERROR/CRITICAL/Traceback=0；
  休市两个影子文件均无行，符合预期。

### R13.2 三条非阻塞观察（不需新批）

1. `record_censored` 无 archetype 过滤，任何被冻结仓都落 CENSORED——属更宽遥测，
   按 intent 去重、无行为影响，可接受；报表 decisions 计数天然含 CENSORED。
2. 头两根 + 15min 超龄门意味着错过观测窗（重启/冻结 >15min）的仓永久 ABSTAIN:stale_5m，
   属可测覆盖损失；复核时按 reason 计数披露即可，不改参数。
3. handoff 排序小疵：R10（14:1x）条目在 R12（14:51）之上，违反「最新在上」；
   下次写入时顺手归位，不单独立项。

### R13.3 结论

**R12 核查通过，无新增阻塞问题。** 下一 RTH（21:30 CST）只观察真实落行并跑报表；
首次结算宣称前按 R5.6 保真契约执行。待用户事项不变：12-02 复核前的实验名额腾位/豁免。

## R14 用户裁决落档：名额等 11-21 自然腾位，不豁免不停采；后续关卡预注册（第 14 轮，2026-11-18）

> 用户 2026-11-18 对 R13 后建议裁「按建议执行」。本节为该裁决的 SSOT 落档；
> 纯 .md，零代码/配置/行为变更。注册表 notes（R12 WIP 归属）不改，其「腾位或豁免」
> 要求由本裁决的「等自然腾位」路径满足。

### R14.1 裁决内容

1. **实验名额：不豁免、不停采。** thesis_management_shadow 继续 record-only 观测，
   等 2026-11-21 DEAD_SIGNAL 结算（关停或证伪均腾位）后顺位补入 ≤5 名额。
2. **预注册 fallback（防卡死）**：若 11-21 因覆盖缺口/口径问题无法结算，二选一——
   a) 限时豁免至 2026-12-02 复核日；b) 退役最弱轨道 RFC-155（历史证据为六臂事后最优、
   剔最佳日仅 +$74.46，新 cohort 0/30）。届时看结算报告再裁并落档，今日不动作。
3. **EOD 第二切片：12-02 复核前不开。** thesis 机制方向成立 ⇒ EOD 作为延伸另裁；
   KILLED ⇒ EOD 继承其数据基础重新评估。一次一变量，且 EOD 另占名额。
4. **LLM：零预算。** 确定性 ABSTAIN 事件未攒出样本前不测 LLM 增量。
5. **灰度定位：机制研究，不是利润引擎。** ITT 天花板 $0.6–0.9/笔，过机制门也不必然
   值得灰度；不为它预留收益期望。
6. **主线不变**：方案 A 正确性修复与 OKR §12.4 Kill/Pivot 裁决（post n≥120，
   约 12-08~12-21）仍是关键路径，影子工作不得挤占。

### R14.2 检查点

- 2026-11-18 21:30 CST：首个 RTH 观察决策+前向路径落行，跑报表（无需人工干预）。
- 2026-11-21：DEAD_SIGNAL 结算 ⇒ 名额腾位；结算不了则触发 R14.1-2 fallback 裁决。
- 2026-12-02：thesis 影子复核（机制门读数 + 名额归属确认）。

## R9 实施记录：PULLBACK_LIMIT 承接失败 record-only 已部署（第 9 轮，2026-11-18）

> 用户裁「核查更新的DEB，请执行」。实施范围仅限 R5/R7 的观测切片，未批准/未实现任何真实 EXIT、TIGHTEN、EOD 复核或 LLM 调用。

### R9.1 实际落地

- `app/playbook/pullback_failure.py:63`：1m→完整 5m 聚合；`:117` 纯函数 ACTIVE/STALE/WAIT/FAILED_RECLAIM，LONG/SHORT 镜像、1% 计划偏离、跨日/超龄/缺 bar ABSTAIN、observed_at 前缀隔离。
- `app/playbook/thesis_manager_shadow.py:123`：读取原指令 pivot、拉取同源 unadjusted K_1M、生成 plan/state hash、quote/source/旧规则同 tick 动作、只追加 JSONL；无券商依赖。
- `app/playbook/invalidation_contract.py`：新增 `compile_pullback_failure_rule`，输出始终 REVIEW_ONLY，旧 contract hash 语义不变。
- `app/playbook/pipeline_parts/exit_monitor_flow.py:871`：现役快环旁路，影子失败不影响原退出；去重状态随 metadata_json 持久化。
- `config/unified_config.yaml:1854`：`thesis_management_shadow.enabled=true`（仅观测）；代码默认 false，停采置 false。
- `app/playbook/shadow_registry.py:4415`：唯一 `thesis_management_shadow` 注册，count_mode=`distinct_where:decision=FAILED_RECLAIM`，min_n=30，机制门与收益不可达声明已写。
- `scripts/thesis_management_shadow_report.py`：只读覆盖/触发报表，未结算 paired Δ 时明确禁止盈利宣称。
- 测试隔离双登记：`tests/conftest.py` + `tests/playbook/offline_chain_driver.py`；哨兵登记 `docs/CONTROL-LOOP-LEDGER.md` P13。

### R9.2 验证

- 新影子+治理契约：41 passed；出场/注册表相邻回归：92 passed；离线全链隔离：18 passed；最终部署前联合回归 **111 passed**。
- 新文件自身：**12 passed**（LONG/SHORT、完整/缺 bar、STALE、前缀不变性、metadata 重启去重、注册表真实计数、REVIEW_ONLY、报表无盈利宣称）。
- 真实 DB 指令临时演练：UBER trade_id=1915，pivot=68.60、fill=68.62，输出 FAILED_RECLAIM；文件 `/tmp/thesis-rehearsal-9_j8kq47.jsonl`，生产影子文件未创建。
- 棘轮家族实跑：60 passed / 2 failed。修复本批一处 W6 缺栈后，剩余 `late_fill_evidence.py`/`position_sync.py` 日志栈与 except:pass 899→900 仍红；本批未新增 except:pass，不改写基线、不借本批顺手改他人模块。**不得写成“棘轮全绿”。**
- `check_undefined_names` 与 `py_compile` 全部通过；`git diff --check` 通过。

### R9.3 部署与当前边界

- 重启前：5 笔 SIM、0 PLAYBOOK_LIVE、durable `exit_intents` 未结算=0；运行时改动仅本批，其他 `scripts/verdict_outcomes_ledger.py` WIP 未触碰。
- `systemctl restart ftseek-us`：MainPID=3374726，ActiveEnterTimestamp=2026-11-18 13:58:12 CST；`startup_smoke_check.py --wait 90` **7/7 exit=0**。
- 运行配置实读：`enabled=True / max_plan_gap_pct=1.0 / max_bar_age_minutes=15.0`；5 笔持仓重启后未变。
- 当前休市，`data/shadow/thesis_management_shadow.jsonl` 尚无行，**没有真实触发率、提前量、误杀或 paired Δ，不能宣称策略改善或收益**。
- 下一 RTH 只观察真实落行；运行 `scripts/thesis_management_shadow_report.py` 读覆盖/触发。任何结构性 EXIT 灰度、EOD 第二切片或 LLM 均需用户另裁。

## R11 主会话确认 R10：两项判定问题均复现，首批语义待裁（第 11 轮）

> 性质：独立边界复现，未改代码/配置，未重启。当前生产影子仍 0 行，尚无真实样本被污染。

### R11.1 两项实锤均确认

**P1-1 入场窗口污染**：以 entry=09:36:10、pivot=100、fill=100.4 复现，当前实现接受 bar1.end=09:40、bar2.end=09:45 并输出 FAILED_RECLAIM。09:40 窗口的 09:36 成员完全早于 fill，09:37 成员也含 fill 前 tick。R10 修法正确：用同一 end_minute 公式求 entry 所属窗口 end，只允许 `bar.end > entry_window_end`，并补窗中/边界测试。

**P1-2 滚动语义偏离**：以 entry=09:36:10、观察=15:47、最近两窗 15:40/15:45 复现，当前实现数小时后仍输出 FAILED_RECLAIM。它确实是 `windows[-2:]` 的滚动语义，不是 R5.3 的 fill 后头两根。此项影响实验人群、误伤暴露与主终点，不能静默当普通代码 bug 改掉。

### R11.2 四项缺口核对

1. 当前只写决策行，没有按 trade_id 的决策后连续 1m 路径；首次 paired Δ 前必须补齐，不得事后回填冒充前向。
2. batch/push/single_fallback 的 `quote_age_s` 当前确实只能为 None，需从实际报价时间戳补算，不能伪填 0。
3. blocked_exits 在钩子前 continue，冻结窗零行，报表需显式 censored/缺口说明。
4. 注册表需补决策日期与 KR4 名额归属；建议首个 RTH 后 10 ET 日复核，不能只有 min_n 没有决策日。

### R11.3 主会话建议

- P1-1 属已批准 record-only 的正确性修复，可按 R10 最小修法实施。
- P1-2 推荐选 **fill 后头两根完整窗口**：贴合预注册、误伤面最小、与 early-tight 提前量同构；若保留滚动语义，必须重写预注册人群与主终点后另裁。
- 首批补齐：前向 1m 路径、真实 quote age、blocked/censored 说明、注册表决策日/KR4 归属；仍只 record-only，不开放真实交易行为。
- 在用户裁 P1-2 前不改代码，当前服务保持 active，影子文件 0 行。

## R12 修正部署：用户裁 fill 后头两根；P1-1/P1-2 与四项结算缺口已落地（第 12 轮）

> 用户在 R11 选择题中明确裁「改为头两根（推荐）」。本批仍只 record-only，未开放真实 EXIT/TIGHTEN。

### R12.1 判定修复

- P1-1：`pullback_failure.py` 新增统一 `_entry_window_end`，**整窗排除 fill 所属窗口**，只允许后续窗口；窗中与边界测试已补。
- P1-2：由 `windows[-2:]` 改为 `windows[0:2]`，只取 fill 后头两根完整 5m；后续滚动窗口不得替代，非连续/超龄即 ABSTAIN。
- 新测试覆盖：entry=09:36:10 时 09:40 整窗不可用；entry 恰在边界；数小时后最近两窗不能产生新信号；未来 bar 前缀不变。

### R12.2 结算缺口修复

1. 新增 `data/shadow/thesis_management_path.jsonl`：按 trade_id 持续追加 fill 窗口之后**新到达**的 1m bar，含实际 known_at/ingest；`_thesis_path_last_bar` 随 metadata 去重，决策不变也记 path_only，重启不重复。
2. `quote_age_s` 优先读现有 `quote_age_ms`，否则复用 `_measured_quote_age_ms(update_time)`；cached_fallback 才用本地缓存龄，未知仍 None，不伪填 0。
3. blocked_exits 分支新增 `record_censored`：`decision=CENSORED/reason=blocked_exits`，记录 intent 并持久去重，不再与无触发混淆；报表 scope 同步声明。
4. 注册表补 `决策/复核日期=2026-12-02` 与 KR4 归属：本项计 1 个实验名额，存量超编不被掩盖，复核前须腾位或用户明示豁免。报表新增 forward_path bars/trades/last_known_at。

### R12.3 验证与部署

- 新影子测试 **16 passed**；最终相邻+治理+离线联合回归 **115 passed**；`check_undefined_names`、py_compile、diff-check 全通过。
- 重启前 5 SIM/0 PLAYBOOK_LIVE/未结算 exit_intents=0；2026-11-18 14:51:05 CST 重启，MainPID=3377559；启动冒烟 **7/7 exit=0**。
- 运行配置仍为 `enabled=True / gap=1% / bar_age=15m`；5 仓不变；决策与前向路径文件当前均 0 行（休市，符合预期）。
- 棘轮既有红点不因本批消失：late_fill/position_sync 日志栈与 except:pass 899→900 仍另账，不宣称全绿。
- 当前仍无真实触发/提前量/误伤/paired Δ。下一 RTH 只观察；任何真实 EXIT、EOD 或 LLM 仍须另裁。

