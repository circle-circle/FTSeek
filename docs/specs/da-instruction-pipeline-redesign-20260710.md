# Spec: DA 指令管线重构 — 连贯链路 · 单一真相源 · 分层兜底 (2026-07-10)

> 状态: **草案待评审** | 类型: 结构性重构(跨 DA skill + FTSeek) | 度量: `pnl_pct`(非 $)
> 一句话: 把"扫描→分析→生成指令→存储→执行"变成**一条连贯、可重试、兜底可见**的链,
> 明确"生成"与"DB 存储"的契约,兜底回落到**已有 technical 确定性策略**(非静默不交易)。

---

## 0. 现状健康度基线(执行前关键核实 · 2026-07-10)

> ⚠️ **交易当前是正常的,不是坏的。** 任何阶段的**第一约束 = 绝不破坏当前可用的 DB 执行路径。**

实测证据:
- 近14天成交 `strategy_id`:**`playbook_sim` 44 笔**(最新 07-10),`manual` 3,`auto_main` 3,`technical_fallback` **0 笔**。
- 今日 07-10 指令:5 BUY + 3 SHORT,**盘前 ET07:00 生成**,当日 09:51–10:25 成交,SL/TP/time-exit + pnl 正常。
- 结论:**DA → `playbook_instructions` DB 直写 → auto_execute** 这条链是活的、健康的;
  bridge 文件 / `voting_coordinator` / `TechnicalFallbackAdapter` 是**并行遗留路径,不喂执行**,
  故其"死代码 / ET16:00 cron"问题**未阻断交易**。

对本 spec 的影响:
1. 本重构是**改善(连贯/单源/新鲜度/告警)**,**非紧急修复**;开盘前不得赶工做难回退改动。
2. **所有阶段 shadow/flag 先行**,与现行 DB 路径**并存对照**,确认不劣化后才切换。
3. **开盘前唯一可做 = P0 观测(record-only,零 live 行为变更)**;P1–P4 一律走 sim 门,不设开盘前 deadline。

### 0.1 变更记录 · 2026-07-12 夜(指令来源收敛,已执行)

**背景实测**(周日预检,均已验证):
- **两个 DA cron 作业都是 agent 任务、都跑 `pipeline.py --mode full` → 都写 bridge 文件 + 直写 DB**(不是"一扫一写"的上下游)。证据:07-10 指令 `decision_id` 分两波 —— `DA-2026071018xx`=作业A(北京18:00/ET06:00盘前),`DA-2026071104xx`=作业B(北京04:00/ET16:00收盘后)。
- 作业A宇宙=watchlist(27)+动态top5,覆盖作业B硬编码15熟股中的13只(缺 NVDA/COIN;NVDA已被动态扫描抓到)。
- 链路预检:scan-only ✅、analyze `--synth rule` ✅、analyze `--synth llm` ✅(产出 entry/SL/TP 齐全指令)。
- gate 观测:07-10 trade_date 桶 107 信号 **BLOCK 51(48%)/WARN 56/PASS 0** —— 指令量骤降的机制是 signal_gate 强节流,非 LLM 不出。

**动作**:`hermes cron pause 13dde1f65703`(暂停作业B `deep-analysis-bridge-generation`,ET16:00 收盘后那个)。

**结果**:**作业A(盘前 ET06:00,`0 18 * * 1-5`)成为唯一写库源** —— 指令来源单一确定,消除 ET16:00 收盘后写入与生成时刻漂移。可逆:`hermes cron resume 13dde1f65703`。

**未动(守纪律,留待周一数据)**:作业A 的 prompt/逻辑、signal_gate、bridge 文件/reload(P1)、watchlist。指令量的杠杆是 gate 调参(用周一拦截率数据、走模拟盘门),不靠冗余作业凑量。观测项:NVDA/COIN 覆盖、单源后生成时刻(应仅 ET6-7 一波)、gate 拦截率 —— 由 P0 日检 `da_chain_health_report.py` 每日推送。

### 0.2 上线前体检 · 2026-07-12 夜(试运行前只读核查,均已实证)

> 目标:周一(07-13)开盘前确认核心链无阻断性风险。**全部为实测证据,非推断。**

**时区** ✅:核心执行路径 DST-aware —— `_now_et()` 取自 `scheduler_triggers.ET = pytz.timezone("America/New_York")`(pipeline.py L75-86)。实测 `is_trading_day(US, 2026-07-13)=True`(周一,交易日)、`2026-07-12=False`(周日)。今晚北京周日21:30=ET周一09:30 → `trade_date=2026-07-13`,与 DA `_get_et_trade_date`(周一不前推)对齐。潜在(非今晚):`order_executor`/`position_sync`/pipeline L442/新影子脚本用硬编码 `-4` 或月份近似 DST,仅 3月/11月切换周有 1h 偏差,7月无关,系全系统既有约定。

**退出分桶** ✅:生成侧 `instruction_generator.py` L130 与退出侧 `exit_policy_guardian.py` L259 / `position_exit_monitor.py` L1698 **同用 `<` 分桶约定**,一致。`playbook_horizon.yaml`(PLAYBOOK_SIM 用)与 `exit_policy.yaml`(ExitMonitor 主线/ORB 用)为 **SSOT 分离**(exit_policy.yaml L141-143/L170-172 注释:7/3 起 PLAYBOOK_SIM 归 pipeline.factor_exit_rules 管),互不干扰。边界点 measure-zero;有 extreme/None 兜底桶。

**筛选后 SL/TP** ✅(构造性验证):遍历 `playbook_horizon.yaml` 全部桶 × 多空 × 多价位,方向不变式(多 `SL<entry<TP` / 空 `TP<entry<SL`)**全部满足**。根因:reprice(pipeline.py L2300-L2325)的 `_sl_pct = max(sl_floor, min(sl_ceil, ...))` 被钉死在 **[2.0, 5.0]%**,永不为负/超100,`tp_mult` 恒正 → 不变式构造性成立;`per_share_risk ≥ 2%×entry` 不趋零(无爆仓),另有 `max_position_pct` 硬上限(L2352);reprice 异常兜底用 DB 原值(L2397)。`rr=999→tp=0` 为短线禁TP靠EOD的既定设计。

**指令来源单源终态** ✅:实测 `jobs.json` —— A `e0d574cc1a3c: enabled=True state=scheduled`;B `13dde1f65703: enabled=False state=paused paused_at=2026-07-13T00:32:18+08:00`。

**cron 时序** ✅:晨批错开且依赖有序 —— `0 6`(sltp日报)→`5 6`(DA日检)→`8 6`(分歧刷新)→`15 6`(影子看板推送,读分歧结果)。⚠️ 既有隐患(非本次引入、低风险、非阻断):北京 `35 5` 有两作业同分钟并发(`ic_health_check.py` + `backfill_voting_outcomes_v2.py`),可后续错开。

**结论**:核心链(A生成→写DB→auto_execute→reprice→SL/TP)**今晚试运行无阻断性风险**。取舍:代码审查代理曾建议在 reprice 加"方向不变式断言"作纵深防御 —— **不在试运行当晚加**(碰 live 执行=引入新变量,违背一次一变量;且不变式已构造性成立,此刻断言冗余),留作试运行后可选加固。

### 0.3 watchlist 调整 · 2026-07-13(数据驱动扩池 + 行业分散,已执行)

> 触发:review job A(DA)候选宇宙是否覆盖最适合量化交易的标的。**决策全部基于系统自身成交数据 + yaml sector 标签,非主观臆断。**

**SSOT & 链路**:job A prompt Step1 调 `app.utils.watchlist_manager.get_stock_codes()` → 只返回 `unified_config.yaml.watchlist.us_stocks` 中 `enabled:true` 项 → 作 `--symbols` 传入 `pipeline.py`(叠加扫描器 top 涨跌,合计约 30-40)。`watchlist_us.json` 已弃用(2026-05-03 P0)。**改名单 = 改 `unified_config.yaml`**。

**数据依据**(只读 `ftseek_us.db.playbook_instructions`,executed=1):历史真实成交仅 **83 笔、总胜率 34%、摊 35 标的**,每标的 1-5 笔 → **样本不足以排"个股 edge",不据此下个股结论**。可靠的**结构性事实**:①多个 watchlist 高波动名(MU/ARM/NIO/SNDK)近乎 0 成交 —— 属**执行侧过滤**(gate 48%+突破触发+5笔/日上限),非标的问题,故**不删**;②NVDA/COIN/NFLX/CAT 等"在交易却不(或停)在名单"。

**变更**(启用 27→32;备份 `config/unified_config.yaml.bak.20260713_165252`):
- 🆕 新增 `COIN`(真缺失,已在交易)、`CAT`/`GS`/`CVX`(工业/金融/能源龙头,跨行业分散,无停用史)
- ♻️ 解禁 `NVDA`(停用旧因"低ATR 5-30"非硬亏;用户拍板;仍受 gate/波动筛选)

**按治理纪律主动未动**(均因真金白银亏损被停,盲目解禁=重蹈覆辙):`NFLX`(近30天-345,6-26停)、`QCOM`(44笔22.7%胜率亏$3.8K)、`TME`(0%-$913)、`PG`(防御双亏)、`LCID`(黑名单16%)。中概簇(BABA/PDD/NIO/NTES)按用户选择未动。

**行业分散效果**:科技+半导体 46%→44%(科技为当前热点,按用户意愿保留);金融 2→3、工业 3→4、能源 1→2;防御三板块(公用/必需消费/地产)主动不纳入(与"规避低波动"一致)。

**取舍**:名单=候选池 ≠ 交易 —— 进不进交易仍由 DA 深度分析 + 门禁决定,故扩池低风险(平淡日不触发)。生效于下次 job A(`0 18 * * 1-5` = 北京18:00/ET06:00)。回滚:`cp config/unified_config.yaml.bak.20260713_165252 config/unified_config.yaml`。

**遗留**(本次未修,记录):DB 存在 `US.US.MU`/`US.US.BA` 双前缀 + `GOOG`/`GOOGL` 割裂 —— **写入侧** bug,会打碎同标的数据,另行修。

### 0.4 符号数据卫生修复 · 2026-07-13(根因+迁移,已执行)

> 闭合 §0.3 遗留项。修两类写入侧污染:①双前缀 `US.US.MU`/`US.US.BA`(对已带 US. 的符号又拼一次);②别名割裂 `US.GOOGL` vs `US.GOOG`。

**根因修复**:新增中央工具 `app/utils/symbol_utils.py::normalize_symbol()`(折叠重复 US. 前缀 + 归并别名 `GOOGL→GOOG`,10 例单测全过),并在两个**写入咽喉**调用:`playbook/db.py::save_instruction`(指令源)、`voting_accuracy_tracker.py::record_prediction`(投票源)——下游派生表随之干净。

**历史数据迁移**:`scripts/fix_symbol_hygiene.py`(带逐库备份 `.bak_symhygiene_20260713_180727` + 碰撞处理 + dry-run)。覆盖 6 个 DB:改名 **63 行**、删变体 **15 行**(playbook_instructions 等有 UNIQUE(date,symbol) 的表,同日冲突删变体、留正名)。复扫全库 `US.US.*`/`GOOGL` 归零。

**回滚**:`.bak_symhygiene_20260713_180727` 逐库还原;代码改动为可逆。注:`GOOGL`(Alphabet A类) 与 `GOOG`(C类) 技术上为两支,本系统按单一标的追踪 Alphabet(watchlist 用 GOOG)故归并,别名表 `SYMBOL_ALIASES` 可随时调整。

### 0.5 signal_gate 个股历史去陈旧化 · 2026-07-13(已执行)

> 问题:07-13 DA 跑完 35 只分析(21 actionable)只落 2 条指令。根因之一——`signal_gate` 的个股历史扣分(-15,同方向3+笔且WR<30%且亏损)读自 `strategy_memory.symbol_memory` 的**全时段累计**(无窗口、无策略标签)。**旧策略/旧治理期的亏损把新策略信号永久按住**,形成"旧亏损→拦新信号→无新数据→无法翻身"死循环。用户裁决:过去时表现不能拦新策略,须给机会;且新老成交都要有过期时间;明显好的信号不能被历史否决。

**改动一(去陈旧化,源头)**:`strategy_memory.get_strategy_hint()` 个股历史改为读 `trade_records` **近 30 天滚动窗口**(`SYMBOL_HISTORY_WINDOW_DAYS=30`,排除 manual/sync 等非策略成交),与 `pattern_memory` 既有 30 天窗口对齐。新老成交一视同仁,超窗一律不计 → 新策略成交回补窗口,gate 用近期数据重新学。(注:`trade_records` 无 `deep_analysis` 标签,DA 指令执行后记为 auto_main/playbook_sim/shadow,故"策略优先"由窗口本身实现。)

**改动二(软约束,终判)**:`signal_gate.evaluate_signal` 追踪 `hist_penalty`,末尾加两条覆盖——① 剔除个股历史后信号本身够强(≥65)→ 不因过去时历史放过,强制 PASS(仍小仓);② 个股差历史**永不单独 BLOCK**:剔除后达 WARN 档(≥40)→ 降 WARN 小仓试错。固有质量项(RR/数据质量/claim-IC/regime)不受影响。

**验证**(只读,未碰 live):①今日 21 个 actionable 标的,触发 -15 的数量 全时段 **8** → 30天窗口 **0**(UBER/LLY/TSM/PDD/NTES/UNH/BA/COIN 解锁);②override 单测 3/3(强信号+差历史→PASS、弱信号+差历史→WARN、真差信号→仍 BLOCK)。真实效果待 07-14 DA 这轮指令数印证。

**回滚**:`data/backups/da_gate_20260713_203715/{strategy_memory,signal_gate}.py` 覆盖还原即可。

**观测缺口已闭合(2026-07-13)**:之前 gate 逐只 PASS/WARN/BLOCK 决策仅 print 到被 `2>/dev/null` 丢弃的 stderr。已在 `pipeline.py` gate 调用处加**专用落盘** `data/da_gate_decisions.jsonl`(嵌套 try/except 永不影响管线,不碰 trade_guard 的 `guard_decisions.jsonl`),逐只记 datetime/trade_date/symbol/direction/decision/quality_score/position_size_hint/reasons。自 07-14 DA 这轮起可直接审计"哪只为何被拦"。回滚:`data/backups/da_gate_20260713_203715/pipeline.py.bak_*`。

---

## 1. 背景与问题(证据)

当前链路(已核验):
- `run_full()` 内部连贯:Stage1 扫描 → Stage2 筛选 → Stage3 分析 → L214 `_write_bridge_file`
  (`~/.hermes/skills/deep-stock-analysis/scripts/pipeline.py` L34-214)。
- `_write_bridge_file`(L335)**双写**:① `opportunity_scan_{ET}.json` 文件 ② 直写
  `playbook_instructions` DB,且**故意绕过** `run_playbook_generate` 幂等/闸门(L342-344)。

四个真问题:
| # | 问题 | 证据 |
|---|---|---|
| P1 | **编排层不连贯**:扫描分析(ET06:00)与 bridge 生成(ET16:00 收盘后)是两个独立 cron,差 10h、逻辑不在同一交易日 | `~/.hermes/cron/jobs.json` 两作业 |
| P2 | **双输出,一路是死代码**:bridge 文件走 `_try_load_bridge`→`BridgeVotingAdapter`,仅在 `__init__` 加载一次、盘中不重载 → 实际读不到 | scheduler_core.py L259-260;审计[8] |
| P3 | **无重试**:每级 try/except 只降级(扫描失败→空;个股失败→SKIP),失败即静默 | pipeline.py L42-49, L119-123 |
| P4 | **两个 DB 写入者、职责靠 hack 协调**:DA 直写绕过 `run_playbook_generate` | pipeline.py L342-344 |

现状兜底(已存在,是本 spec 的落点):
- `playbook.fallback_when_no_bridge` 默认 `technical` → `TechnicalFallbackAdapter`(pipeline.py L312):
  跑 `app.strategy.pure.evaluate("technical")` 确定性策略,`strategy_id="technical_fallback"`,可关停。
- **但**该兜底仅在 scheduler `__init__` 按"bridge 文件是否加载"决定一次(scheduler_core.py L275-296),
  **无重试、无新鲜度校验、无显式告警、盘中不重估**。

---

## 2. 目标 / 非目标

**目标**
1. 扫描→分析→生成→存储→执行 成为**一条连贯链**,盘前一次跑完,开盘前留足 buffer。
2. **重试**:生成失败或产出 0 条有效指令 → 盘前窗口内重试。
3. **分层兜底**:DA(主)→ 重试 →(仍无)→ **TechnicalFallbackAdapter(确定性技术策略)**→
   仅当显式配置 `none` 才 NullVoting 全 SKIP。兜底**必须响亮告警**。
4. **生成↔存储契约明确**:生成=纯函数出指令;存储=唯一幂等写入者;每条盖 `generation_id/generated_at/source/status`。
5. FTSeek 侧**盘前 reload + 新鲜度守卫**,替换 `__init__`-only 文件判断(顺带修审计[8])。

**非目标**
- 不改 DA 的 LLM 分析质量/prompt(另有轨道)。
- 不引入新的 LLM 运行时决策点(兜底必须确定性、可单测)。
- 不为凑成交放松风控:兜底"无方向 → None(SKIP)",绝不硬凑。

---

## 3. 设计原则(Ponytail)
- **单一真相源**:`playbook_instructions` DB 是唯一执行契约;bridge JSON 降级为可选调试产物。
- **单一写入者**:每日仅一个入口写 DB,取消"绕过幂等"的 hack。
- **最短路径**:一条盘前链,删冗余的 ET16:00 作业。
- **显式契约**:生成/存储/消费三方按 schema + 新鲜度窗口交接。
- **兜底可见可审计**:回落 technical 有独立归因 + 响亮告警,绝不静默;LLM 缺席用确定性策略,不用噪声。
- **一次一变量**:分阶段落地,每阶段独立 sim + 可回滚。

---

## 4. 目标架构

```
盘前管线作业 (单 cron, 时间按"跑批耗时 + 开盘前 buffer"倒推):
  generate()  纯函数: (scan, analyses) → List[Instruction]  (已过 gate/校验, 不碰 DB)
      │  失败或 0 条有效 → retry(N 次, 指数退避, 仍在盘前窗口内)
      ▼
  persist()  唯一 DB 写入者: UPSERT by (date, symbol),
             盖 generation_id / generated_at / source=deep_analysis / status
      ▼
FTSeek 盘前 reload (非 __init__-only):
  从 DB 读当日计划 + 新鲜度守卫 (generated_at ∈ 今日盘前窗口?)
      ├─ 新鲜且有指令 → 执行 DA 计划 (strategy_id=deep_analysis)
      └─ 无/过期 → 分层兜底 (见 §5) + 响亮告警
```

时序(北京/ET,EDT):
```
20:00 / 08:00  盘前管线 generate→persist(DB)  [含 retry]
09:15 前       完成 + FTSeek reload + 新鲜度校验
09:30 / 09:30  开盘;若无当日 DA 计划 → 已在开盘前告警 + 走 technical 兜底
```

---

## 5. 分层兜底(本 spec 核心)

```
Tier 0  DA 计划 (主)          : 新鲜 DA 指令存在 → 执行 (strategy_id=deep_analysis)
Tier 1  盘前重试              : generate 失败/0 条 → 重试 N 次 (窗口内), 成功则回 Tier 0
Tier 2  TechnicalFallback (兜底): 重试仍无 → app.strategy.pure "technical" 确定性出方向
                                 (strategy_id=technical_fallback, entry/SL/TP 走 compute_instruction_values)
                                 → 响亮告警 "今日无 DA 计划, 已回落 technical 策略"
Tier 3  NullVoting (仅显式)   : 仅当 playbook.fallback_when_no_bridge=none → 全 SKIP + 告警
```

约束:
- Tier 2/3 **与 DA 互斥**,任一时刻单链(避免双策略并发下单)。
- Tier 2 无方向时**返回 None(SKIP)**,不硬凑成交(护栏,见 pipeline.py L319-321)。
- 兜底归因独立(`strategy_id`),使 §8 能分别度量 DA vs technical 的 `pnl_pct`。

---

## 6. 契约定义

**Instruction(生成产物,内存态)**:`{date, symbol, direction, entry_price, entry_low, entry_high,
stop_loss, take_profit, confidence, thesis, conditions, risks, invalidation, sector,
market_regime, strategy_type, decision_id}` — 复用现有字段,不新建表。

**存储元数据(新增/明确)**:
- `generation_id`:一次盘前 run 的唯一 id(全批同 id)。
- `generated_at`:UTC 时间戳(判新鲜度)。
- `source`:`deep_analysis` | `technical_fallback`。
- `status`:`ACTIVE` | `SKIP`(BLOCK/无方向)。

**新鲜度窗口**:FTSeek 消费时,仅接受 `generated_at` 落在**当日盘前窗口**(如 ET 06:00–09:20)
的 `ACTIVE` 指令;否则视为无当日计划 → 分层兜底。

---

## 7. 分阶段落地(一次一变量 · 每阶段 sim + 回滚)

| 阶段 | 内容 | 域 | 可回退 | sim 门 | 状态 |
|---|---|---|---|---|---|
| **P0 观测** | 埋点记录"当日实际跑的是 DA 还是 technical_fallback"+ 指令新鲜度,攒 baseline(record-only) | FTSeek repo | 秒级(删埋点) | 无需(只读) | ✅ **已落地 2026-07-10**: `scripts/da_chain_health_report.py`(只读日检 + 飞书推送, cron `5 6 * * 2-6`) |
| **P1 reload+新鲜度守卫** | FTSeek 盘前 reload 从 DB 读 + `generated_at` 窗口校验,替换 `__init__`-only 文件判断(修审计[8]) | FTSeek repo | flag 关 | ✅ 模拟盘 1 周,对比"读到 DA vs 误用旧计划" |
| **P2 编排+重试** | 合并两 cron 为单盘前作业 + generate 失败/0 条重试 N 次 | DA skill(`~/.hermes`,难回退) | 恢复 jobs.json | ✅ 先 dry-run 观察产出时刻/重试触发 |
| **P3 单真相源** | DB 为唯一契约;bridge 文件降级调试产物;取消绕过 `run_playbook_generate` 的双写 | 两侧 | 中(改回双写) | ✅ 模拟盘对比成交集合一致性 |
| **P4 显式告警+兜底固化** | 回落 technical 时飞书响亮告警;分层兜底纪律化 | FTSeek repo | flag 关 | ✅ 注入"DA 缺席"演练,验证告警+兜底路径 |

**落地顺序理由**:P1 是"让任何时间修正真正生效"的前提,且在 repo 内可测 → 先做;
P2 触碰工作区外 skill、难回退 → 需 dry-run + 确认;P3/P4 依赖前两阶段稳定。

---

## 8. 验证(度量口径)
- 主指标:**按 `source`/`strategy_id` 分组的 `pnl_pct`**(DA vs technical_fallback),回答"兜底是否拖累"。
- P1:模拟盘日志出现盘前 reload,且新鲜度守卫拦下过期计划的次数;无"隔夜旧 bridge 误用"。
- P2:bridge/指令产出时刻落在盘前窗口;注入失败观察重试次数与最终态。
- P4:演练"DA 全失败"→ 确认 ①开盘前收到告警 ②`source=technical_fallback` 成交 ③无双链并发。
- 全程:回归测试绿;`pnl_pct` 度量,不用绝对 $。

---

## 9. 风险与回滚
- **跨系统难回退**:P2/P3 触碰 `~/.hermes`(我只读、live 交易触发)→ 必须 dry-run + 显式确认 + 保留 jobs.json 原值。
- **兜底陷阱**:纪律/风控无法把随机方向变正期望 → Tier 2 必须确定性 + 独立归因 + 可关停(已具备)。
- **双链并发**:兜底与 DA 必须互斥(§5 约束)。
- 每阶段独立 flag/配置回退,秒级恢复。

---

## 10. 待确认(评审时定)
1. **盘前窗口边界**:reload 与新鲜度窗口的确切时刻(需结合 DA 跑批耗时实测)。
2. **重试次数/退避**:N 与间隔(受开盘前剩余时间约束)。
3. **technical 兜底是否默认开**:保持 `fallback_when_no_bridge=technical` 还是评审后定。
4. **FTSeek 每日重启机制**:若采用"重启触发 reload"而非 hook,需新增重启 cron;否则走 P1 的 hook。
5. **DST**:Hermes cron 固定北京时,换季 ET 漂移 ±1h,expr 是否季节微调。

---

## 附:证据文件
- DA 生成/双写:`~/.hermes/skills/deep-stock-analysis/scripts/pipeline.py` L34-214, L335-470
- 兜底适配器:`app/playbook/pipeline.py` L294(Null)/L312(Technical)/L363(Bridge)
- 兜底选择:`app/core/scheduler_core.py` L253-296
- 技术策略:`app/strategy/pure.py` `evaluate("technical", ctx)`
- 相关:`docs/proposals/bridge-cron-time-fix-20260710.md`;审计 `~/.hermes/skills/deep-stock-analysis/references/da-to-executor-consistency-audit-20260712.md` 断点[7][8][9]
