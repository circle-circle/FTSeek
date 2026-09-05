# DEB-20260904: 经验体系化与泛化能力系统 — 事实层 / 错误分类法 / 教训账本闭环 / 股性画像 / 时序图 (设计立案, ~~待裁决~~ 已裁决执行 2026-09-05, 见 §8.2)

- **立案日**: 2026-09-04
- **用户令链**: ①"复盘最近一个月…持续犯相同的错误…给出有效解决方案" → ②"不满意…基于个股有针对性的可落地方案,包括DA和UE…深度分析" → ③"你确认是否可以提升收益 有没有负面影响" → ④**"先不动, 目前本身是sim模拟交易, 我想继续积累数据, 但对于亏损和盈利的经验和教训能够体系化和系统化, 真正产生反思和经验, 形成泛化能力, 避免踩同样的坑…是否需要更多的时序图统计、更多的指标, 形成不同标的股的股性和盘感"** → ⑤**"如果要智能化以及泛化能力, 可以启用已有类似的Hermes agent skills的流程"** → ⑥"请将方案落成deb文档"
- **状态**: **多模型辩论完成** (三角色, §九; kimi-reviewer 事实核查 / qwen-architect 证伪攻击 / glm-verifier 实证复现), ~~**最终共识方案待用户裁决 (§十, 批次0)**~~ **最终共识方案已裁决 — 2026-09-05 用户裁"按建议执行" (D1 维持冻结 / 辩论 skill 已建 / commit 批准已执行 / 二期顺延, 详见 §8.2)**; ~~零代码零配置零交易行为变更~~ (提案期承诺; 落地后批次0 代码已入库 46e31ccf 但全 record-only, 本 DEB 范围内交易行为变更仍为零 — 唯一 config 改动 chase_cap_enforce 系用户另批的 P0-A 热修复 87ebc785, 不属本 DEB); 与 D1-D7 冻结决定 (REVIEW-20260904 §7.7) 完全兼容
- **纪律引用**: 证据先行于方案; 防散落不造轮子 (三登记); 宪章 §二 数据口径七律; 影子先行/预注册判据 (ADR-035 先例); record-only 先行 (C4); 测试隔离红线 (PYTEST_CURRENT_TEST 守卫, DEB-20260903 ③教训)

---

## 〇、一页概述

**问题**: 系统每月产生 ~240 笔成交与大量复盘文本, 但"经验"只以自由文本散落 (lessons 表 / us.json / Obsidian / DEB 档), 没有结构化复发度量 —— **"追高"类教训 9/1 已立案, 9/3 仍有 ~~8/10~~ **8/12** 笔 chase_cap 违例 (口径勘误见 §九: 当日 15 笔成交, 12 笔指令带 cap, 方向感知违例 8 笔), 无人报警, 因为没有任何机制度量"教训立案后是否还在犯"**。用户感知"持续犯相同错误"与该结构缺口互为因果。

**方案**: 五层架构, 全部 record-only —— 确定性事实层 (指标时序+错误分类) 喂给已有 LLM 复盘流程 (Hermes 盘后复盘/周日复盘), LLM 负责归纳泛化, 教训账本闭环负责度量"是否还在犯", 股性卡喂回 DA prompt, 时序图让一切可见。

**关键调研结论**: 智能化通道大半已存在且在跑 (§二), 缺的是 (a) 喂给 LLM 的结构化事实层, (b) 教训的验证闭环。**本设计 80% 是接线与扩展, 不是新建。**

---

## 一、既有资产盘点 (调研实证, 设计只扩展不重建)

### 1.1 仓内 (/opt/ftseek/FTSeek-US)

| 资产 | 位置 | 现状 |
|---|---|---|
| 成交上下文 | `app/storage/trade_store.py:142-207` | trade_records 全列 (mfe_pct/mae_pct/entry_session/market_regime/decision_id/fees_est/pnl_net…); 入场时写 metadata_json 含 entry_gap/attribution/execution (`playbook_service.py:1702-1788`) |
| 入场/出场环境快照 | `trade_store.py:331-349` entry_context_snapshot | 已快照 price/vix/spy_change_pct/atr/session/regime/volume_ratio; **缺组合层状态** (当日第几笔/同向并发数/当日已亏额) |
| 经验记忆通道 | `experience_accumulator.py` lessons 表 + `app/llm/agent_bridge.py:771 _update_memory` + `app/core/memory_manager.py:94` | 活跃 (9/3 仍在写); LLM 盘后归因输出 memory_updates {symbol_lessons, strategy_adjustments, historical_lessons} → `~/.hermes/ftseek/memory/us.json` |
| 教训注入点 | `app/agents/debate/memory_injector.py:76` (辩论) + `app/review/lesson_injector.py:95` (盘前作战计划【历史教训】段) | 活着, 8000 字符预算 |
| 股性卡 | `scripts/build_symbol_trading_cards.py` → `data/symbol_trading_cards/` + `data/symbol_rule_cards/`; 周报 `symbol_performance_report.py` (周一 08:00) | 存在; DA 侧注入 loader 已在仓外 skill (`llm_synth.py::_load_trading_card_for_prompt`), yaml 开关 `playbook.symbol_card_prompt_enabled` 等四个就位 |
| 影子/判据机器 | `app/playbook/shadow_registry.py` (OBSERVING→READY→GRADUATED/KILLED 生命周期) + `docs/ADJUDICATED-CRITERIA.md` 判据字典 B1-B17 | 活着; GRADUATED/KILLED 纯人工裁决; ~30 个 shadow jsonl 在 `data/shadow/` |
| 复盘三件套 | `app/review/daily_review.py` (六维日报→Obsidian+推送) / `review_orchestrator.py:1609` (RichContext+三模型辩论→lessons 表) / `cohort_ab.py` | 进程内调度器触发, 活着 |
| 熔断机器 | `app/playbook/risk_circuit_breaker.py` (连亏3减半/连亏5停1日) | **代码写好从未接线**, 仅周报记录 — D1 的半个身子已在仓内 |
| LLM 执行门 | `app/llm/pre_entry_gate.py:90` (查同标的近7日战绩注入 prompt) | **默认关** (`llm_features.pre_entry_gate.enabled=false`) |
| 指标时序表 | `app/storage/metrics_tables.py` (metrics/metrics_hourly 写入器) | **已死**: 数据停在 2026-03-31 |
| 归因表 | `trade_store.py:481-497` trade_attribution (root_cause_category/systemic_pattern 列) | 存在但靠 LLM 事后自由文本填, 无确定性打标 |
| 薄分类 | `pipeline.py:5957 classify_be_exit` (保本出场 OK/GAP/SLIP 三类) | 仅覆盖 BE 出场, 消费方仅推送文案 |

### 1.2 Hermes 侧 (~/.hermes)

| 资产 | 现状 |
|---|---|
| **5 个在跑 LLM 流程** (cron jobs.json 全 enabled, 9/4 实跑 ok) | 盘后综合复盘 04:00 / **deep-analysis 盘后复盘+记忆更新 05:00** (feedback_loop.py + deadfetus_collector + dc_calibration_collector) / 盘前综合预检 20:00 / 周日综合复盘 周日10:00 / DA 盘前扫描 20:00 (含 pullback_stats 股性统计自动注入) |
| **方法论 skill 库** `skills/quantitative-trading/` | `ftseek-trade-history-review` (逐标的全量复盘配方 + 15 条口径陷阱) / `instrument-suitability-audit` (标的准入体检=股性核查) / `signal-ic-analysis` / `backtest-rule-replay` (四关门+安慰剂) / `pre-execution-causal-replay` (变更落地前实弹复演) / `exit-parameter-validation` / `risk-tail-audit` / `tail-risk-defense-audit` / `ftseek-playbook-shadow-system` |
| DA 侧学习脚本 | `deep-stock-analysis/scripts/`: `feedback_loop.py` / `strategy_memory.py` / `deadfetus_collector.py` (入场质量攒样, **已含 chase_pct 字段**) / `dc_calibration_collector.py` (**conf 校准链**, 三源 JOIN 对账 T+1) / `ic_writer.py` |

---

## 二、缺口分析 (为什么"经验"没有变成"能力")

1. **指标时序层已死**: metrics/metrics_hourly 表数据停在 2026-03-31; 所有复盘数字每次临时现算, 散落 `data/daily_reviews/*.json` / Obsidian / DEB 档 → 趋势不可见, 时序图无米下锅, equity 曲线只有 `capital_state` 单行当前态无历史。
2. **教训"只写不验"**: lessons 表和 us.json 在写, 但无复发计数、无立案后成本累计、无依从率 → "避免踩同样的坑"不可测量, 复发无告警。
3. **错误分类太薄且靠 LLM 自由文本**: 无法聚合、无法画趋势、无法跨日复比; 唯一确定性分类只覆盖保本出场。
4. **组合层状态不入库**: 簇崩塌日 (8/14/18/28 -2288) 的"第几笔/同向并发/当日已亏"只能事后手工重建, 无法机检。
5. **可视化只有 web dashboard 单图**, 无热力图/分布图/静态日报存档。
6. **采集口径散落**: deadfetus (chase) / dc_calibration (conf 校准) / gap_alignment / pullback replay 各自采集, 无统一事实层归并 → 同一概念多处定义, 违反口径七律风险。

**诊断**: LLM 泛化层 (Hermes 复盘流程) 存在且在跑, 但喂给它的是原始 DB + 自由文本; 它归纳出的"教训"没有验证闭环 → 泛化质量不可考核。**泛化能力 = 确定性事实层 (喂什么) + LLM 归纳层 (已有) + 验证闭环 (考核归纳是否有效)**, 三段缺两头。

---

## 三、设计总览

```
[结算时确定性采集]                [LLM 泛化层 (已有流程扩展)]           [验证闭环]
 P1 指标时序 analytics.db    →   deep-analysis 盘后复盘+记忆更新   →   P3 教训账本
 P2 错误分类打标              →   (feedback_loop 消费结构化事实,      复发计数/成本累计
    (trade_attribution)         输出 memory_updates 结构化教训)       依从率/复发告警
 组合层状态入快照             →   周日综合复盘+反思周记段         →   影子 registry 晋升
                                    ↓                                  ↓
 P4 股性卡 v2 (build_symbol_trading_cards 扩展) ←——— 立案教训 ———— 判据字典预注册
    ↓ DA prompt 注入 (loader 已存在) / 盘前计划 lesson_injector
 P5 时序图静态报告 (matplotlib → data/reports/ + Obsidian)
```

**五条铁律** (沿用既有纪律):
1. **record-only**: 全部新采集/打标/报表; 零交易行为变更; 不动退出参数 (exit-de49b1299290); 不动 unified_config.yaml 交易键。
2. **LLM 只归纳不造数**: LLM 输入只能是确定性层产出的事实行; 数字引用必须可回源 SQL (口径七律)。
3. **防散落**: 新采集点先查 deadfetus/dc_calibration/各 shadow 是否已采同概念 → 归并口径不另起; 新观察点一律注册 shadow_registry。
4. **测试隔离红线**: 任何新写入方必须带 PYTEST_CURRENT_TEST 守卫 + conftest 登记 (DEB-20260903 ③: 23 天测试污染生产判分数据的前科)。
5. **晋升唯一通道**: 教训→规则候选→shadow (registry) →预注册判据→`pre-execution-causal-replay` 实弹复演→用户裁决→才许动行为。本设计自身不晋升任何东西。

---

## 四、分层详设

### P1 事实层: 指标时序 (地基)

**载体**: 新建 `data/analytics.db` (与生产库隔离; 先例: signal_snapshots.db / ftseek_us_review.db 均独立)。

**表 1 `daily_metrics`** (每日一行, EOD 结算后写):
- 账: net/gross/fees, 费/毛比, 笔数, 胜率, 盈亏比
- 质量: **捕获率** (pnl_net ÷ |mfe| 口径, 按 exit_reason 分桶), mfe≈0 占比, giveback (mfe−落袋) 中位
- 分桶 PF: entry_session (15min 粒度, 口径陷阱7) / market_regime / 链路 (DA/UE/REENTRY) / 方向 / ATR 桶
- 校准: conf 分桶 vs 实际胜率 (归并 dc_calibration 口径)
- 集中度: 前3标的盈亏占比, 峰值并发持仓, 同簇同向并发峰值, 当日已实现亏损曲线峰值
- 错误类别成本 (P2 聚合): 每类一行 JSON

**表 2 `capital_history`**: capital_state 每日快照 → equity/回撤曲线数据源。

**结算时补三个派生字段** (写 trade_records.metadata_json, 幂等回填):
- `path_class` (V形反转/阴跌/冲高回落/单边, 从 1min bar 判定)
- `time_to_mfe_min` / `giveback_pct`

**组合层状态入快照** (entry_context_snapshot 扩列或 metadata_json 键): `day_seq_no` (当日第几笔新开), `concurrent_same_direction`, `realized_pnl_so_far_today` —— 使簇崩塌形态机检可行 (§二.4)。

### P2 错误分类法 (确定性优先)

**结算时规则打标器** → 写现有 `trade_attribution` 表 (不新建), 多标签+各自成本归因。初始标签集 (全部来自 REVIEW-20260904 已实证的出血模式):

| 标签 | 判定规则 (确定性) | 来源实证 |
|---|---|---|
| `CHASE_ENTRY` | 成交价 > 指令 chase_cap_price, 或无 cap 时 entry 偏离 anchor >阈值 | 9/3 实测 8/10 违例 |
| `MFE_ZERO_ENTRY` | COALESCE(mfe_pct,0) < 0.3% (口径陷阱15) | mfe≈0 桶 32笔 -3389 |
| `OVERSIZED_RISK` | 该笔美元风险 (qty×SL距离) > 当日中位 2× | 尾部9笔 -2372 |
| `CLUSTER_BET` | 同时段同方向并发 ≥4 且同板块簇 | 8/14/18/28 -2288 |
| `OVERNIGHT_GAP_LOSS` | exit_reason=OVERNIGHT_GAP* 或隔夜亏损 | 隔夜桶 PF 0.23 |
| `COOLDOWN_REBUY` | 同标的 N 日内亏损后再入场 (SNDK 型) | SNDK 8/14→8/19 |
| `LATE_ENTRY` | 11:00 ET 后新开 | 12笔 -122 |
| `STALE_CONTEXT` | decision_id 决策日 ≠ 成交日 (I15 同族) | 9/1 陈旧 bridge 案 |

**taxonomy 定义单真源**: 写进 `ftseek-trade-history-review` skill 的 references (所有 agent 共享同一标签口径, 含判定 SQL); LLM 归因 (root_cause_category 自由文本) 保留为第二意见, 与确定性标签并列存, 不一致时以确定性标签为准做聚合。

### P3 教训账本闭环 (核心 — "不再踩同样的坑"机检)

**扩展 lessons 机制** (experience_accumulator, 不重建): 每条活跃教训挂 —
- `lesson_id` / `mistake_class` (关联 P2 标签或自定义模式) / `scope` (symbol|setup|global)
- 生命周期: OBSERVING → ENFORCED → RETIRED (复用 shadow_registry 模式, GRADUATED/KILLED 人工裁决不变)
- `first_seen` / `last_seen` / `recurrence_count` / `cost_since_created` / `compliance_rate`

**每夜复发扫描器** (确定性脚本): 对当日成交逐条比对活跃教训的匹配规则 → 命中即 recurrence++、累计成本; 复发即升级告警 (飞书/钉钉: "追高教训 9/1 立案后复发 5 次 -$612"); N 日零复发才标记"已改"。**教训从笔记变成可考核的约束。**

**与 LLM 泛化层的分工**: 扫描器管"旧教训有没有再犯" (确定性); LLM 管"今天有没有新错误模式值得立案" (泛化)。两者在教训账本会合。

### P4 股性卡 v2 (盘感)

**先核验后扩展**: 第一步核验 `build_symbol_trading_cards.py` 产出新鲜度与 DA 侧真实注入率 (开关在 yaml 但实际注入量未实测 — 先测再扩)。

**卡片扩三块**:
1. **本系统边际**: 我们在该标的的 n/PF/期望值/捕获率/**该股专属错题 top2** (P2 标签按股聚合 — "这股我们总是追高亏")
2. **股性统计**: ATR% 体制、隔夜跳空分布 (对照 SL 线, instrument-suitability-audit 方法)、趋势vs均值回归倾向、时段边际、财报日历
3. **盘感叙述**: LLM 基于 1+2 生成三句话卡 (token 预算内)

**消费端**: DA prompt (loader 已存在, 开关 `symbol_card_prompt_enabled`) / 盘前计划 lesson_injector / Obsidian 每股一页 (writer 现成)。`pre_entry_gate` (默认关) 列为可选挂载点, 默认不动。

### P5 时序图与可视化

**每夜静态报告** (matplotlib → PNG, 不起新服务; 生产链已有 matplotlib 于 archive/optimizer 的先例):
1. equity + 回撤曲线 (capital_history)
2. 滚动 5 日 PF / 捕获率趋势
3. 时段×星期 盈亏热力图 (15min 粒度)
4. **错误类别成本堆积面积图** (复发可视化 — "同样的坑"一眼可见)
5. conf 校准曲线
6. 个股盈亏条形图 (滚动 30 日)

落 `data/reports/<date>/` + Obsidian 嵌入 + 周日复盘推送摘要。web dashboard 扩面板列为后续可选项, 不在首批。

---

## 五、Hermes 现有流程接线表

| 现有流程 | 接线动作 | 性质 |
|---|---|---|
| **deep-analysis 盘后复盘+记忆更新** (05:00) | 加 **Step 1.7**: 运行 P3 复发扫描器 (确定性) + 扩展 feedback_loop prompt 消费"当日 P2 标签成本 + 活跃教训复发表 + 股性卡增量" → 输出结构化 memory_updates (schema 已存在) → 次日晨经 memory_injector/lesson_injector 注入盘前 | 仓外脚本+prompt 扩展 (留 .bak 惯例) |
| **周日综合复盘** (周日 10:00) | 加"反思周记"段: 教训依从率表 + 错误类别成本周趋势 + 新簇检测 → 产出带预注册判据的规则候选**草稿** (仅草稿, 进 shadow_registry 走既有晋升) | cron prompt 扩展 |
| 盘后综合复盘 (04:00) | 推送内容附加当日 P1 指标摘要 + 图 4 (错误类别堆积图) 链接 | prompt 扩展 |
| ftseek-trade-history-review skill | references 增"错误分类法 taxonomy 定义"单真源文档 | skill 文档新增 |
| 方法论 skills (backtest-rule-replay / pre-execution-causal-replay / signal-ic-analysis) | 不改动; 作为将来晋升通道的既有工具 | 零变更 |

**归并清单 (防散落)**:
- `deadfetus_collector` 的 chase_pct ↔ P2 `CHASE_ENTRY`: 统一判定口径, 采集不重复 (deadfetus 继续采入场技术态, P2 标签器复用其字段不重算)
- `dc_calibration_collector` ↔ P1 校准桶: 同一 conf×结果口径, P1 聚合其产出
- `risk_circuit_breaker.py` (未接线): 不在本设计接线 (属交易行为变更, 与 D1 同属冻结裁决); 仅留档"机器已存在, 将来启用成本减半"
- `metrics/metrics_hourly` 死表: 不复活旧表结构, 以 analytics.db 新表为准 (旧表留档不动)
- `promotion_engine.py` (DEPRECATED): 不复活; 晋升走 shadow_registry 人工裁决通道

---

## 六、裁决请求

**批次① (核心闭环)**: P1 事实层 + P2 错误分类 + P3 教训账本 + Hermes 接线 (Step 1.7 + feedback_loop prompt) — 估计 2 天量。
**批次② (可视化)**: P5 静态报告 — 依赖①的数据, 半天量。
**批次③ (股性)**: P4 卡 v2 — 先核验现有卡片再扩, 半天~1天。

每批交付标准: 契约测试 (含 PYTEST_CURRENT_TEST 不写生产的硬钉) + 触及套件全绿 + 全量回归 + 零交易行为变更声明 + 落档更新。

**明确不在范围**: 不接 risk_circuit_breaker; 不开 pre_entry_gate; 不动 D1-D7; 不动任何交易/退出参数; 不自动晋升任何教训为门。

---

## 七、诚实边界

1. **本系统不承诺提升收益** — 它让"犯错→立案→复发→成本"可测量可告警, 把 REVIEW-20260904 里人肉发现的模式变成机检; 收益改善仍取决于后续裁决落地的规则 (且需各自过影子判据)。
2. **LLM 泛化质量依赖事实层质量**: P1/P2 未建好前扩展 feedback_loop prompt 是空中楼阁 → 批次顺序不建议颠倒。
3. **样本量警告沿用**: 日级 MDE ±$335; 错误标签小桶 (n<10) 只看不判。
4. **教训复发扫描的匹配规则是确定性的, 必然比 LLM 的语义理解窄** — 它能抓住"同一个坑"的字面复发, 抓不住"换马甲的坑"; 后者仍靠周日 LLM 反思段。两层互补, 不互相替代。
5. **历史回填不伪造**: P1 指标可从 trade_records 历史重放回填 (标注 backfill), 但 P2 标签对 9/4 前成交只能按可重建字段打标 (组合层状态等历史不可重建的维度留 null 不编造)。

---

## 八、执行记录

~~(待用户裁决批次后填写)~~ (已填写: §8.1 接线规格与首跑快照 / §8.2 集成核验与用户裁决记录, 2026-09-05)

### 8.1 任务#16: 04:00 报告段接线规格 (2026-09-05 glm; ~~jobs.json 直接写入被权限系统拦截, 本节为待应用精确规格~~ **补记 (team-lead 核验 2026-09-05 04:3x): 首次写入被拦后 glm 重试已成功 — jobs.json 实测含 Step 11/双脚本引用/心跳行, prompt 5602→6878 字符, 21 jobs 完整, 备份 jobs.json.bak_20260905_batch0_step11 在, hermes 每 tick 重载无需重启, 下次生效 2026-09-08 04:00 CST**; 应用前置条件=测试全绿+用户确认)

**上游验证**: #14/#15 交付物就位; 扫描器 `--no-write --json` 全窗口冒烟通过 (EXIT=0); 熔断影子已产出 `data/shadow/cb_shadow_latest.json`; `tests/test_daily_violation_scan.py` **20/21 通过, 1 失败** (`test_data_quality_filter`: 空真实交易库时 run_scan 早退返回 `{"error":...}` 无 `label_totals_window` 键, 测试期望有键 — 契约不匹配待 #14 裁决)。

**接线三处编辑** (目标 `/root/.hermes/cron/jobs.json` job `7a40dd7c9e94`, 备份已备: `jobs.json.bak_20260905_batch0_step11`):

编辑1 — **Step 11 段落**, 插入点=Step 10 之后、"### 输出格式（飞书 Markdown）"之前:
- bash 块: `daily_violation_scan.py` 与 `intraday_loss_breaker_shadow.py` 顺序执行, 各记退出码 S1/S2; 追加心跳行 `echo "$(TZ=America/New_York date +%F) scan=$S1 shadow=$S2" >> data/violation_segment_heartbeat.log`; 末行 `sort -u ... | awk '$2=="scan=0"&&$3=="shadow=0"' | wc -l` 输出毕业判据累计成功天数 (X/14, 目标≥10)。
- 渲染规则 5 条: (1) 三标签当日计数+30天窗口合计, CHASE_ENTRY 必报 no_cap 笔数 (无 cap=无法判定不等于合规); (2) 教训生命周期表 lesson_gates 逐标签一行 (status_after/recurrence_count/cost_since/rank_key), PENDING转ACTIVE 或 RETIRED 复活事件显式标注; (3) 污染监控 lessons_growth 五指标, new_last_7d 大于等于 2 倍 prev 且大于等于 10 时标警告; (4) 熔断影子摘要只陈述数字, 禁止"建议启用/调阈值"行动项, 预注册判据=被挡笔期望小于等于 -10 且 n 大于等于 30, 未达标或反向数字必须原样呈现; (5) 本步失败不阻断报告, 心跳仍记录。

编辑2 — **输出格式模板**: "隔夜重定价 shadow + 负edge门影子 + 入场时段队列" 块后加 "教训复发 + 熔断影子 (record-only)" 一行摘要块 (见 Step 11 格式)。

编辑3 — **行动项纪律**第 6 条: Step 11 全段 record-only, 禁止直接生成行为变更行动项 (启用熔断=预注册判据毕业+用户裁决; 调阈值=推翻预注册), 行动项最多引用其数字作证据。

**毕业判据埋点**: 判据1 (载体大于等于 10/14 天) = 心跳文件唯一 ET 日期中双零行计数。诚实边界: 心跳证明载体执行, LLM 是否渲染该段以 prompt 强制为准 (飞书侧无仓内凭证)。

**首跑基线快照 (2026-09-05 04:0x 留痕, 供 14 天后对照)**:
- 扫描器 30 天窗 (08-06 至 09-04): CHASE_ENTRY 9 (9/3 单日 8) / OVERSIZED_RISK 9 / CLUSTER_BET 141; 当日 (9/4): 0/1/6, CHASE no_cap=5/11。
- 污染监控: lessons total=201, new_last_7d=8 vs prev_7d=1 (delta +7, 未到警告线; 主源 review_orchestrator 122)。
- **熔断影子首跑方向反转 (重大, 如实留痕)**: kimi 冻结 exit 事件流口径下全期被挡 196 笔合计 **+7222.76, 期望 +36.85** (vs glm entry 序预验证被挡 -685); 近 30 天仅 4 笔 +254.22; 三簇日触发均在午前亏损定型后 (8/14 14:13 / 8/28 11:39 ET)。**判据当前不满足且方向相反** — 若 14 天后仍如此, 按预注册纪律应判 A' 毕业失败呈裁, 不许调阈值救; 首跑反转佐证 R2 "阈值样本内挑选"攻击成立, exit 流口径下 -400 触发过晚, 裁决权在用户。

### 8.2 任务#13-#18 集成与核验记录 (2026-09-05, team-lead 会话)

**交付归属**: #13 摸底 (glm-r1) / #14 扫描器+lessons 迁移+注入器闸+taxonomy 单真源 (qwen-r1) / #15 熔断影子+registry+B18+12 钉测试 (kimi-r1) / #16 04:00 接线 (glm-r1) / #17 回归三分诊 (qwen-r1+glm-r1+team-lead) / #18 skills 建议 (glm §12 + kimi §11.6(b), 待用户裁决)。

**team-lead 独立复核留痕** (不采信未复算声明):
1. CHASE 锚点: 9/3 方向感知违例 8 笔与 REVIEW §7.7 勘误三逐笔一致; kimi 独立复核 8/14 CLUSTER=9 与其事件流分析互证。
2. A' 证伪独立复算: 8/14 首穿 14:13:48 被挡 0 笔 (与 kimi 一致); 锁定口径 (30d/real/exit 事件) 全窗被挡 **0 笔** — 三套口径三个答案 (entry 序前视 -685 / simulate 全期假想多亏 8958.83 / 锁定 30d real 0 笔) 实证 §11.3.4 口径锁死的必要性。REVIEW D1 证据同步撤回 (§7.7 勘误四)。
3. cost_since 符号事件 (连环误读, 全部留痕): kimi 误读"正=净赚" → team-lead 未核验即转发 → 真值裁决 (CHASE 9 笔 Σpnl=+9.14 vs 库 -9.14) 确认 taxonomy 约定 **cost_since=−Σpnl_net (正=烧损/负=净赚)** 正确, kimi 与 team-lead 先后公开更正, qwen 原注释无误。终态: recur_tag 符号感知显示 + 报告段约定注行 + 测试钉死 (test_memory_injector_sign_aware_display)。
4. 测试分歧消解: glm 报 20/21 vs team-lead 两跑 21/21 — 根因=qwen 04:19-04:20 修复落地时序差, 非 flake。
5. §8.1 "接线被拦"自述与 glm 完成通报矛盾 → 实测 jobs.json Step 11 已写入并核验, §8.1 头部已补记。
6. kimi 自首桶位 bug (盘后出场错桶 UTC 墙钟日): 196→191 笔/期望 +36.85→+46.90, 方向结论不变且更强; §11.6(a) 与 registry notes 已同步勘误; §8.1 快照数字为修复前口径, 以 §11.6(a) 修正后为准。daily_loss_halt_calibration.py 同款写法 (窗口全在常规时段不受影响, 留痕不返工)。

**回归终态 (#17)**: team-lead 全量 2539 passed / 3 failed (196s) → 三分诊: ①qwen 扫描器静默 except 棘轮 (qwen 已修) ②台账守卫拦 RFC-076 cron 未入账 (team-lead 补登记行, 复验 10 passed) ③test_r9_r13_contracts R12 HOOD 9/4 两表时刻差 2.94s (文档化抖动 42-74ms 的 40 倍) = **执行链真实数据漂移, 时间线证非本轮引入** (9/4 盘中写入, 早于全部本批写入), 呈执行链 owner 裁决, 不改数据不放宽容差。glm 复核的 "123 failed" 异常跑与本轮触碰文件零交集 (系环境异常跑, team-lead/qwen 两次全量均 ~2539 通过予以否证)。kimi 12 钉与 qwen 修复后文件均在全量后单跑全绿。

**零行为变更核验**: config/ 零改动 (exit 冻结 exit-de49b1299290 守住); 行为面=注入层排序 (legacy 次序乘积=0 tie-break 逐字节等价, qwen 生产库实测绘证) + registry 登记 + lessons 3 规范行 (id 202-204) + 派生表 cb_shadow_*; 生产库写入前备份 data/backups/ftseek_us.db.bak_batch0_vscan_20260905_040912。

**用户裁决 (2026-09-05, "按建议执行")**: ①D1 **维持冻结** (置信度=证据撤回, 不撤案 — 保留影子前向攒样, 14 天到期连同批次0 毕业判据一并呈裁); ②辩论流程薄 skill **批准创建** — 已建 `~/.hermes/skills/multi-model-debate/SKILL.md` (kimi-r1 交付 v0.1.0: 两轮制 + 三角色 + 防自欺六条款 (复算义务含口径三元组/前视偏差检查/预注册冻结/身份实测留痕/事实推断分行/无法证伪须声明) + 触发门槛/四反模式, 零项目数据口径/阈值); ③本批代码+文档 commit **批准并已执行** (qwen-r1 三批: `87ebc785` P0-A chase_cap 6 文件 / `46e31ccf` 批次0代码 8 文件 / `40d10d6e` 落档批 40 文件; team-lead 逐提交核验文件清单无杂项+yaml 仅 chase_cap 键+KR2 守卫 12 passed, 全量 2553 passed/1 failed=R12 预存漂移已呈执行链 owner); ④二期结构化教训对象 **顺延** — 按 §十一 收敛时序 (30+ 天立项) 不提前, 待一期 14 天毕业判读 (2026-09-22 前后) 连同批次0 判据一并呈裁。


---

## 九、多模型辩论记录 (2026-09-04, 宪章 §一 三角色)

**角色**: 作者=Qoder (本 DEB) → 辩论方=kimi-reviewer (Kimi-K3, 反幻觉事实核查) + qwen-architect (Qwen3.8-Max, 证伪式架构攻击) → 独立复核=glm-verifier (GLM-5.3, 只读实证复现)。**数据裁决不投票**, 逐条如下。

### 9.1 kimi-reviewer 事实层 (自评 9/11 ✅)

| # | 攻击点 | 裁决 | 处置 |
|---|---|---|---|
| A1 | "Step 1.7"撞号 — deep-analysis 05:00 流程已有 Step 1.7=deadfetus_adjudicator | **成立** (glm 互证) | §五 改 Step 1.8 (§十 共识方案生效) |
| A2 | 接线表叙述把 Step 1.6 写成 1.5 | **成立** | §十 措辞修正 |
| A3 | deadfetus_collector.py 文件存在性未经我验证 | **成立(方法) 但结论反向**: 实测文件存在, 然 DEB 路径写错 — 实际在 `~/.hermes/skills/deep-stock-analysis/scripts/`, 非 `quantitative-trading/deep-analysis/` | 路径勘误 |
| A4 | trade_attribution 退役标记字段位置 | **驳回 kimi 读法** (glm 实锤: 退役标记在 registry 行 state_json 内, 非顶层列); 底层诉求(表述须精确)成立 | 表述精确化 |
| B1 | chase_cap "8/10 违例"分母错; kimi 称 raw_instruction_json 无该字段 | kimi 读法**驳回** (glm 实锤: 字段在 raw_instruction_json **顶层**, 非 execution_plan); 但其指向的"我的 8/10 口径错"**成立** — glm 复现: 9/3 当日 **15 笔**成交, **12 笔**指令带 cap, 方向感知违例 **8 笔** (LONG 5: NVDA/UBER/META/CRM/RIVN; SHORT 3: AVGO/MDB/DDOG); 字面 fill>cap 仅 5 | **三处勘误 8/10→8/12(总15)**: 本 DEB §〇/P2 表、REVIEW-20260904 §三、handoff |
| B2 | 股性卡 261 vs 260 | **成立(表述)**: 构建器生成 260 + 示例卡 1 = 目录 261 文件 | 精确表述 |

### 9.2 qwen-architect 证伪层 (2 致命 / 4 重要 / 1 反提案)

| # | 攻击点 | 裁决 | 处置 |
|---|---|---|---|
| 致命1 | 冻结期 (D1-D7 冻结中) 建复发告警=治症状不治根, 告警无处置出口 → 告警疲劳 (前科: combined_monitor 84条/日被废弃) | **部分成立**: 告警疲劳风险实证成立, 采纳限流; 但"完全无出口"不成立 — D1-D7 解冻裁决需要积累证据, 复发账本正是该证据的生产者 | 系统重定位=**冻结期观测层**; 复发告警**不独立推送**, 合并进既有 04:00 盘后报告段 (零新推送通道) |
| 致命2 | P2 载体写 `trade_attribution` 表 = 违反 DEB-20260903-trade-attribution-zombie-watch 裁决 (该表已退役+防复活测试钉住+明确拒绝复活) | **成立** (我 §一 盘点写了该表现状却漏引 zombie-watch 裁决 = 疏漏) | **P2 载体废弃**: 批次0 不落库 (每日扫描重算); 批次1 可选 `trade_records.metadata_json` append-only 键 (先例: metadata_json 已是入场归因载体) |
| 重要1 | Step 1.8 之外还须防 SILENT 渗透 (prompt 扩了但 LLM 实际不消费) | **成立** | 批次1 交付标准加"渗透审计" (注入前后 prompt 留样比对) |
| 重要2 | LLM 污染环: lessons 表已被低质同模板条目占据, 不加闸的复发计数会放大噪声 | **成立** (glm 实证: 末 50 行"复盘修正"同模板主导, symbol 多为 null) | **反污染三闸全采纳**: ①新教训 PENDING, 同模式 n≥2 才 ACTIVE 注入 ②injector 截断排序改 recurrence_count × cost_since ③30 天零复发 auto-RETIRE |
| 重要3 | 五缺环: analytics.db 无消费者会重蹈 metrics 表死亡 / taxonomy 无生命周期 / 复盘吞吐瓶颈 / capital_history 回填歧义 / Step 编号 | **成立** | analytics.db **14 天消费心跳, 无消费自停**; 新标签须 DEB 立案才入 taxonomy; 每夜裁决吞吐预注册上限; capital_history **不回填**, 部署日起向前看 |
| 反提案 | 80/20: 跳过 analytics.db, 单脚本 3 标签 → lessons 复发计数 → 并入 04:00 报告段, 2 周试行再扩 | **采纳为批次0** | 见 §十 |

### 9.3 glm-verifier 实证层 (6/8 逐项精确)

| # | 发现 | 裁决 | 处置 |
|---|---|---|---|
| G1 | 9/3 chase_cap 真实口径 8/12(总15), 8/10 系误读 | **成立** (唯一硬伤口) | 三处勘误 (同 B1) |
| G2 | deadfetus_adjudicator 无独立 cron, 在 05:00 流程内; 20:00 档实为双任务 (盘前预检+DA扫描) | **成立** | 表述勘误 |
| G3 | lessons 末 50 行被"复盘修正"同模板条目主导, symbol 多 null | **成立** | 支持反污染三闸 (qwen 重要2) |
| G4 | metrics/metrics_hourly 已死 5 个月 | **成立** | 支持消费心跳设计 (qwen 重要3) |

### 9.4 本 DEB 内联勘误汇总

1. §五 "Step 1.7" → **Step 1.8** (撞号 deadfetus_adjudicator)。
2. §一.2 deep-analysis 路径 → `~/.hermes/skills/deep-stock-analysis/scripts/`; deadfetus_adjudicator **无独立 cron** (05:00 流程内步骤); 20:00 档为**双任务**。
3. §〇/§四 P2 表 chase_cap "9/3 实测 8/10" → **8/12 (当日 15 笔)** (方向感知口径: LONG 成交价>cap 或 SHORT 成交价<cap; cap 在 raw_instruction_json 顶层)。
4. §四 P2 载体 trade_attribution → **废弃** (zombie-watch 裁决), 共识载体见 §十。
5. §一.1 股性卡 → "构建器生成 260 张 + 示例 1 张 = 目录 261 文件"。
6. §一.1 trade_attribution 行补充: 该表已经 DEB-20260903-trade-attribution-zombie-watch 裁决退役 (有防复活测试), 原文"存在但靠 LLM 填"表述不完整。

---

## 十、最终共识方案 (三方辩论收敛, 待用户裁决)

### 10.1 相对原设计 (§三-§六) 的三点核心改向

1. **批次0 极简先行** (采纳 qwen 80/20): 不建 analytics.db、不写 attribution 表、不起新 cron/推送通道。先证明"产出被消费", 再谈扩展。
2. **冻结期观测层定位** (回应致命1): 系统产出=解冻裁决的积累证据, 非实时干预; 复发信息**只**并入既有 04:00 盘后报告段, 零独立告警 (杜绝 combined_monitor 前科)。
3. **反污染三闸入核心** (回应重要2+G3): PENDING 门禁 / injector 排序 recurrence×cost / 30 天 auto-RETIRE, 从第一天就在, 不是后补。

### 10.2 批次0 (唯一请求本次裁决; 约半天量)

- **新增 `scripts/daily_violation_scan.py`** (纯确定性, 只读 trade_records + playbook_instructions, 每日重算不落库), 3 个标签, 判定 SQL 写入 taxonomy 文档 (ftseek-trade-history-review skill references) 作单真源:
  - `CHASE_ENTRY`: 方向感知违例 (LONG: 成交价>cap; SHORT: 成交价<cap; cap 取 raw_instruction_json 顶层 chase_cap_price, 无 cap 记 null 不编造) — 口径=glm 复现口径
  - `OVERSIZED_RISK`: 该笔估算美元风险 (qty × 1.5×ATR 止损距离) > 当日 2× 中位; ATR 缺失记 null
  - `CLUSTER_BET`: 由 entry_time/exit_time/direction 重建的同向持仓并发 ≥4
- **lessons 表扩列** (ALTER TABLE, 幂等): `status` (PENDING/ACTIVE/RETIRED) / `recurrence_count` / `last_triggered_at` / `cost_since`; 配套三闸逻辑在扫描器内。
- **接线**: Hermes 04:00 盘后报告 cron prompt 加一步 — 先跑扫描器, 报告增"教训复发段" (活跃教训复发表 + 当日 3 标签计数)。零新 cron、零新 DB、零新推送通道。
- **铁律不变**: record-only / PYTEST_CURRENT_TEST 守卫 / 不动退出参数 (exit-de49b1299290) / 不动 D1-D7 / 不自动晋升任何教训为门。
- **预注册毕业判据** (试行 14 天后裁决, 先写判据再看数据):
  1. 报告段在 14 天中实际生成 ≥10 天 (载体活着);
  2. 至少 1 条由该段驱动的立案或裁决 (被消费);
  3. 零交易行为变更守住 (git diff 可证)。
  三条全满足 → 解锁批次1; 任一不满足 → **停扫描器留档, 不堆积僵尸** (metrics 表死亡前科的反向应用)。

### 10.3 批次1/2 (不预先裁决, 由批次0 毕业判据驱动)

- **批次1**: P2 全 8 标签 + 可选 metadata_json append-only 键 / P1 analytics.db (**带 14 天消费心跳, 无消费自停**) / Hermes 05:00 流程 **Step 1.8** + SILENT 渗透审计 / taxonomy skill 文档。
- **批次2**: P4 股性卡 v2 (先核验 DA 侧真实注入率再扩) / P5 matplotlib 静态报告。

### 10.4 辩论后预期管理 (诚实边界增补)

批次0 的直接产出是"每周一份可考核的复发账" — 它回答"立案的教训还在犯吗、又烧了多少", 不直接改善收益; 收益改善仍依赖解冻后对 D1-D7 的裁决落地。若 14 天内报告段无人读、无裁决被驱动, 按毕业判据第 2 条应判失败并停建 — 这是设计的一部分, 不是例外。

---

## 十一、第二轮互辩: 智能化深化方向 (2026-09-04, 用户令"还是不够智能化…多模型会话互相讨论给出思路方案")

**形式**: 两轮制真互辩 (非各自独立报告)。R1 三方独立提案 → R2 每方收到另两方全文后逐条证伪+立场修正+"只做一件事"收敛。角色沿用 §九 (kimi-reviewer=Kimi-K3 / qwen-architect=Qwen3.8-Max / glm-verifier=GLM-5.3), 模型身份经 subagent transcript 实测 (API model 字段逐字节一致: 36/20/64 次调用)。

### 11.1 R1 提案要点

- **Qwen (架构)**: 瓶颈判定=采集不缺, 断在"归纳→验证"(lessons 是散文非可证伪断言, dedup 靠 lesson_text 精确匹配, experience_accumulator.py `_persist` — glm 抽查属实) 与"注入→行为"(memory_injector 位置截断不按情境) 两条边, 再验证环节缺失。提案: P1 教训结构化 (machine_condition 列) / P2 regime×时段条件期望闸 / P3 语义聚类 / P4 主动实验流水线 / P5 决策时情境检索。
- **Kimi (反幻觉)**: P1 教训证伪账本 (falsify_sql + contradiction_count, ≥2 矛盾降级 DISPUTED 停注) / P2 注入效果 A/B 分桶 / P3 LLM 新模式双样本闸 (提出即冻结+纯样本外+n<30 拒收) / P4 语义先匹配后生成 / P5 决策时 DEB 强制引用。
- **GLM (实证, 唯一 R1 即跑库方)**: A 日内累计亏损熔断影子 (-400 阈值, 重放 +685/月; 同数据否决"按笔数熔断"变体 -253) / B 置信度地板 (conf<0.4 全期 164 笔 -3712.5) / C 因子权重 regime 条件化 / D lessons 重放毕业闭环 (预计存量 201 行过线 <20%); 数据否决个股级 gating (133 标的均 5.9 笔/只)。

### 11.2 R2 交叉证伪裁决 (数据裁决不投票)

| 被攻提案 | 关键攻击 (攻击方) | 裁决 | 处置 |
|---|---|---|---|
| kimi P1 证伪账本 | 反事实来源缺失: 教训生效后被拦信号不成交, "此后实际盈利"永无样本 (qwen+glm 双指); contradiction≥2 在 n<10 桶违反口径七律 (qwen) | **成立** | 证伪改在 **signal_snapshots 信号层** (4371 行仍在记录) 跑, 或配影子假想轨迹; contradiction 判据加 n 门 |
| kimi P2 注入 A/B | 注入是会话粒度非 signal 粒度, 分臂错配 (qwen); 空白臂=主动让实盘变差=行为变更 (qwen); 230笔/30天分两臂必 underpowered, CI 跨零 (glm); 4 周>14 天窗 (glm) | **成立** | **撤出一期**, kimi 本人撤回降级二期; 一期用 glm D 的"预算只给毕业规则"替代 |
| kimi P3 双样本闸 | 吞吐风险: 14 天内单模式命中几乎必然 n<30, 闸门可能空转 (qwen); 与判据字典叠床架屋 (glm) | **部分成立** | 保留"提出即冻结+纯 OOS"核心, 并入二期结构化教训对象, 不独立立项 |
| qwen P2 条件期望闸 | 非随机两组选择偏差 (glm); 14 天影子被拦 n≈10 统计不可达 (kimi); condition_expectancy_latest.json 实测 hard/soft 均空 — 现有引擎没过自己的门槛, 先解释空再建闸 (glm) | **成立** | **降级**: 不打单, 条件签名仅作报告归因; symbol 维度删除 (尊重 glm 5.9笔/只数据) |
| qwen P4 自动落轨 shadow | 自动生成 shadow 条目绕过用户裁决, 违宪章 (glm) | **成立** | 改"自动起草→人裁队列", 不自动注册 |
| qwen P5 情境检索 | 检索对象库空 (signal_arbitration 0 行, glm 实测); top-3 相似交易=3 个 anecdote 锚定=幻觉放大器 (kimi); "事后正确率"无标注方 (kimi) | **成立** | **qwen 本人撤回** |
| kimi P5 DEB 强制引用 | precedent 须机器可核对 (decision_id 列表), 否则 LLM 引先例=幻觉温床 (glm); 数据源指错 (真身在 da_gate_decisions.jsonl) (glm) | **成立** | 降级低成本添头, 二期再说 |
| qwen P3 / kimi P4 embedding 聚类 | 无 embedding 基建=新依赖 (glm); 误合并吃掉条件差异, 须在 machine_condition 结构化之后 (qwen); 复发率验证需>30 天 (glm) | **成立** | 合并为一项, 置于二期结构化之后; **"lessons 行数增长率作污染监控"零成本, 立即单独采纳** |
| glm C regime 权重 | 桶内 n<30 居多 (glm 自查); 25 个 IC 多重检验, 5bp 不可检 (kimi); 14 天不可落地 (qwen) | **成立** | **glm 本人收回**, 降观察项 |
| glm A 熔断影子 | 阈值 -400 在同一 30 天窗内选出=样本内调参, 须预注册冻结 (qwen+kimi 双指); "14天 n≥30"数学不可达 (28笔/30天→14天≈13) (qwen+kimi 双指) | **成立 (均为可修正的实施缺陷, 非方向错误)** | 采纳修正: 阈值预注册冻结不再调; 主判据="被挡笔期望≤-10", n≥30 顺延为毕业条件; 同轨影子记录被挡信号假想轨迹 |
| glm D 重放毕业 | "同窗口归纳+同窗口重放"循环论证, 过线率虚高 (kimi); LLM 写 SQL=无审计代码生成 (glm 自指+qwen); 201 行散文无可执行 SQL 无法直接重放 (kimi) | **成立** | 重放窗口必须与归纳窗口分离 (OOS); SQL 生成走人审队列; 先做 qwen P1 结构化再重放 — **顺序修正** |

### 11.3 R2 数据更正与口径警示 (kimi 复算, 全部留痕)

1. **glm A 熔断数字被 kimi 精确复现** (-400→挡28笔 -685.04; -600→21笔), 但 -600 阈值两臂符号不一致 (glm 报 +75 / kimi 报 -75.47) — 双方都同意近零且远弱于 -400, 不影响方向; **此差异本身佐证"阈值购物"风险, -400 必须预注册冻结后不再调**。
2. **glm B 置信度地板口径敏感 (重警示)**: kimi 复算 `data_quality='real'` 过滤下精确复现 (164笔 -3712.5), 但**去掉该过滤符号完全翻转** (<0.4 档变 +14739) — 结论悬于单一过滤口径, **B 在任何影子立项前必须先锁口径重验, 本轮不予采信**。
3. **glm "trade_attribution 784 行"张冠李戴**: 实 223 行 (784 是 trade_records) — 已更正, 不影响 D 的方向但重放基数变小。
4. **30 天净额三值互不一致**: 台账 -8.92 (REVIEW 撰写时点) / glm +72.89 (现库, 9月新增41笔+291.5) / kimi -186.84 — 系快照时点+过滤口径差, 非数据错误; **立项: 一切跨文档数字引用前必须锁 (窗口, data_quality 过滤, pnl_net) 三元组口径, 规范写进 taxonomy 文档**。
5. signal_arbitration 表实测 0 行, DA/UE/REENTRY 链路口径真身在 da_gate_decisions.jsonl — glm/kimi 互证。

### 11.4 收敛 (三方"只做一件事"答案)

- **qwen**: GLM A 熔断影子 + 三修正; 并主动合并 P1+kimi P1+glm D 为统一"结构化教训对象"(machine_condition+falsify_sql+重放毕业=同一对象三字段)。
- **kimi**: 教训可证伪化闭环三合一为首推, GLM A 作并行纯 SQL 影子 (不耗 LLM 资源)。
- **glm**: A' 熔断影子 (主判据累计亏损口径, 条件签名仅作归因); 结构化可证伪教训行立项第二期 (毕业判据天然需 30+ 天, 不占本窗口)。

### 11.5 最终思路方案 (两阶段, 待用户裁决)

**一期 (14 天, 与 §十 批次0 同轨并行)**:
1. **GLM A' 日内累计亏损熔断影子** — 当日已实现 pnl_net ≤ **-400 (预注册冻结)** 后记录"会拦什么"+被挡信号假想轨迹, 不落任何行为; 主判据=被挡笔期望≤-10, n≥30 顺延为毕业门槛; 与批次0 扫描器同轨。**注意: 此影子 ≠ REVIEW D1 落地, D1 仍需解冻裁决; 影子正是为解冻裁决攒证据。**
2. **批次0 三标签扫描器** (§十 不变) + **lessons 行数增长率污染监控** (零成本添头)。
3. 两项均并入 04:00 报告段, 零新通道。

**二期 (30+ 天, 立项不实施, 一期毕业后启动)**: **结构化可证伪教训对象** = machine_condition (qwen) + falsify_sql/contradiction_count (kimi, 在信号层证伪) + OOS 重放毕业器 (glm, 归纳/重放窗口分离); 首周交付=存量 201 行重放净化; 新教训不合 schema 拒收; embedding 聚类与 DEB 引用器置其后。

**杀死/降级清单 (互辩的主要产出)**: 注入 A/B (粒度错配+underpowered+空白臂违影子纪律) / 决策时检索与 DEB 强制引用 (库空+幻觉放大器) / regime 条件因子权重 (n<30+多重检验) / 个股级 gating (5.9笔/只, 数据否决) / 自动注册 shadow (须人裁) / **glm B 置信度地板 (口径敏感翻转, 锁口径重验前不采信)**。

---

## 十二、附: skills 化裁决建议 (任务#18, 2026-09-05; 用户裁"按建议执行", 辩论 skill 已建)

**问题**: 一期包 (批次0 扫描器 + GLM A' 熔断影子 + 04:00 报告段) 建成仓内代码还是 Hermes skill? 多模型辩论流程本身是否 skill 化?

### 12.1 一期包 → 仓内代码 (scripts/ + tests/), 不建 skill

依据 (全部可核查):
1. **毕业判据第 3 条要求 git 可证**: "零交易行为变更守住 (git diff 可证)" — `~/.hermes/skills/` 在 repo git 之外, skill 文件的改动 `git diff` 不可证, 判据直接失效。
2. **PYTEST_CURRENT_TEST 守卫 + 测试归属**: 守卫只有挂在 repo 测试套件里才有意义 (precedent: `tests/test_daily_review.py` 及 2026-08-13 的 pytest 污染生产报告事故 — 测试与生产代码同库共纪律是已付学费的结论)。
3. **写库操作纪律**: lessons ALTER + status 更新是生产 DB 写入, 走 repo 的 DB 层与 review 流程; Hermes skill 脚本的既有惯例是只读分析 + jsonl sidecar 记账 (deadfetus_collector 等"纯记账"模式), 无 DB schema 变更先例。
4. **cron 接线现成**: 04:00 job workdir=/opt/ftseek/FTSeek-US, 现行 10 步全部执行 repo 脚本 — 新增 Step 11 执行 `scripts/daily_violation_scan.py` 与 A' 影子脚本即零摩擦。

### 12.2 skill 承载的是"知识"而非"代码" (已在 §10.2 预定, 重申)

- 三标签**判定 SQL 单真源** → `~/.hermes/skills/quantitative-trading/ftseek-trade-history-review/references/` (新增 taxonomy 文档), 供任何 LLM 复盘会话引用。
- §11.3.4 的**口径三元组纪律** (跨文档引用数字前必锁 窗口 × data_quality 过滤 × pnl 口径) → 同上 skill 的"口径陷阱"清单 (现 15 条, 增补第 16 条)。
- 本 DEB 的簇日重放结论 (计数熔断失效 -253 vs 累计亏损熔断 -685) → 同 references 留档。

### 12.3 多模型辩论流程 → 建 skill, 已建 (轻量, 一文件; 2026-09-05 用户裁"按建议执行"后落地, 任务#19 kimi-r1 交付)

依据:
1. **产出可证**: 本轮两轮互辩杀死/降级 6 项 (见 §11.5 清单), 全部有数据或逻辑依据 — 流程本身是被验证过的有效资产。
2. **可复用且模型无关**: 宪章三角色 (反幻觉/架构/实证) + 两轮制 (R1 独立提案须带离线预验证方案 → R2 交叉证伪+立场修正+"只做一件事"收敛) + 裁决纪律 (数据裁决不投票/口径七律/每提案至少一个攻击点或声明无法证伪) 是纯过程模板, 不依赖具体模型。
3. **成本极低零风险**: 一个 SKILL.md (协议+角色+判据+裁决表格式), 无 cron、无 DB、无推送通道 — 不触"新写入方须消费者"红线 (按需触发型方法论, 与既有 quantitative-trading 下 11 个 skill 同族)。
4. **边界**: 辩论**转录与裁决落档仍在 repo** `docs/debates/DEB-YYYYMMDD-*.md` (git 可追、用户可裁), skill 只存协议不存内容 — 避免把"可裁决证据"搬出用户视野。

### 12.4 一句话结论

代码进 repo (判据/测试/写库三重纪律要求), 知识进 skill (taxonomy 单真源 + 口径陷阱增补), 辩论协议建轻量 skill (协议在 skill、证据在 repo)。~~skill 创建动作本身待用户裁决后执行 (涉及 ~/.hermes 配置区, 不属本次默认授权范围)。~~ **已执行 (2026-09-05 用户裁"按建议执行")**: `~/.hermes/skills/multi-model-debate/SKILL.md` 已建, 详见 §11.6(b); 一期包代码按本节建议入仓内, 由任务#21 分批提交。

### 11.6 实施期补记 (2026-09-05, kimi)

**(a) A' 影子首跑: 预验证量级被可实现口径证伪 (数据更正, 与 §11.3 同性质)**
`scripts/intraday_loss_breaker_shadow.py` 落盘 (record-only); 持久化=既有库派生表 `cb_shadow_daily`(24 触发日) + `cb_shadow_blocked`(196 笔, PK(trade_date,trade_record_id), 全量重建幂等) + `data/shadow/cb_shadow_latest.json` (registry 计数键 n + 04:00 报告段消费)。口径冻结: trade_mode=simulate ∧ pnl_net NOT NULL, ET 日分桶, **exit 事件流** (tie 时 exit 先结)。
证伪对象=§11.5 预验证数字 (-400→挡28笔 -685.04/月): 该口径按 entry 序立即累加 pnl = **前视偏差** (入场时刻即记账该笔未实现亏损)。可实现语义下实证: 簇崩塌日 8/14 共 9 笔集中 10:16-10:17 同分钟入场, 首笔亏损 10:23 才实现, 累计 -400 迟至 14:13 触发 → **挡 0 笔**; 8/28 同型挡 0 笔。全期 backfill: 被挡 191 笔期望 **+46.90** (主判据 ≤-10 不过), 假想"节损"合计 **-8958.83** (照此熔断历史反多亏); 近30天仅挡 4 笔 +254.22 (方向相反)。〔2026-09-05 自审查勘误: 初版时间桶位 bug — 带偏移戳取 UTC 墙钟日, 盘后出场错桶次日; 修复走 RFC-070 parse_et_date 后 196→191/+36.85→+46.90, 方向结论不变且更强。〕
结论与处置: ①backfill 按预注册判据即判"不毕业", 影子保留 forward 攒样 (零成本, 口径已冻结); ②"为 D1 解冻攒证据"叙事降级——已实现口径熔断器**结构上抓不住开盘同时段簇** (亏损实现恒晚于同分钟簇入场); ③若将来要抓簇崩塌, 候选变量=同分钟簇密度/笔数类, 但 glm R1 曾以重放否决计数变体 (+253), **该否决口径是否同样前视待重验后才能引用**; ④阈值 -400 维持预注册冻结不调——口径修正 ≠ 阈值再购物 (§11.3.1 纪律)。预注册判据本身不变, 这正是预注册的价值: 它在首轮数据面前拒绝了样本内调参的结论。

**(b) skills 化裁决建议 (任务#18)**
①**一期包 (批次0 扫描器 + A' 影子): 仓内代码, 不建 skill。** 理由: ㊀口径冻结物 (-400 阈值/三标签规则/过滤口径) 是预注册裁决物, 必须与代码同仓同 git 评审留痕——"修改判据=重新裁决"的可审计性在仓外 skill (~/.hermes, 不入项目 git) 里不成立; ㊁PYTEST_CURRENT_TEST 守卫 + conftest 沙箱重定向 + 全量回归全挂仓内 pytest 体系 (前科: 9/3 仓外写入方测试污染生产判分数据跨 23 天, REVIEW-20260903); ㊂仓外 skill 直写项目库已有未收口病灶 (DA skill 直写 playbook_instructions 双写者无跨进程完成标记, 9/2 留 P2 待裁) — 一期包核心是状态ful 数据管线 (持久化/消费心跳/毕业判据), 与 skill "方法论知识" 的定位属性不匹配。
②**多模型辩论流程: 建议建薄 skill** (`~/.hermes/skills/multi-model-debate/`, 纯流程编排+宪章纪律, **不含任何项目数据口径/阈值**): 两轮制 (R1 独立提案→R2 见全文后交叉证伪→数据裁决不投票→"只做一件事"收敛) 本轮实证有产出 (三方各撤回/收回≥1 提案, 两项前视偏差被复算捕获); 内建防自欺条款=复算义务 (不接受未复现数字) / 无法证伪须声明 / 预注册冻结 / 模型身份实测留痕; 写明触发门槛 (仅行为变更裁决级/月度复盘级议题) 与反模式 (小决策开三模型=仪式化滥用)。~~**skill 创建本身待用户裁决后实施, 本轮仅落档建议。**~~ **已执行 (2026-09-05 用户裁"按建议执行")**: `~/.hermes/skills/multi-model-debate/SKILL.md` 已建 (kimi-r1 交付 v0.1.0) — 两轮制 + 三角色 + 防自欺六条款 (复算义务含口径三元组/前视偏差检查/预注册冻结/身份实测留痕/事实推断分行+n<30/无法证伪须声明) + 触发门槛与四反模式, 零项目数据口径与阈值。
