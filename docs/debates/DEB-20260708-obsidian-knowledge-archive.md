# DEB-20260708: 是否建立Obsidian知识存档持续积累每支标的的深度分析

## 背景

用户提出: "代码包括参数配置都是执行层面的,最核心的还是关于筛选、市场以及deep_analysis每支标的股票的深度解读,包括每支股票的战绩和分析以及结果沉淀,是否可以建立数据存档(如Obsidian),通过选择必要的内容,持续积累迭代。"

### 现有数据基础设施 (已验证)

| 存储层 | 位置 | 内容 | 消费者 |
|--------|------|------|--------|
| signal_snapshots.db | data/ | 838条信号快照, IC T1/T5/T20+alpha回填 | analytics, watchlist, regime_filter |
| strategy_memory.db | data/ | 67 pattern + 94 symbol_memory + 154 lesson_log | signal_gate.py → LLM prompt注入 |
| ftseek_us.db | data/ | 391 trade_records + 329 playbook_instructions + 74 active lessons | pipeline, daily_review, lesson_injector |
| us.json | ~/.hermes/ftseek/memory/ | 56 historical_lessons + 89 tagged_lessons + active_fixes | MemoryManager → 次日prompt注入 |
| claim_ic_cache.json | deep-stock-analysis/scripts/ | 215样本, 论据级IC反馈 | llm_synth.py → LLM prompt注入 |

### deep_analysis LLM产出 (已验证)

llm_synth.py:561 `synthesize()` 返回结构化JSON:
- bull_case[] (每条含强度+来源类型)
- bear_case[] (同上)
- verdict (方向判定)
- causal_chain (宏观→板块→个股)
- execution_plan (分批入场+止损+目标)
- risk_tiers, fundamental_snapshot, historical_reference

**关键: 这些分析产出目前写入signal_snapshots.db的factors_json字段,但从不以人类可读格式归档。每次分析是一次性的。**

### 用户设计哲学 (来自memory)

"最短路径——一个缓存文件+几个读取点,而不是新模块/新DB表/新服务。复杂度是敌人。"

## 目标

回答: Obsidian知识存档是否能为FTSeek增加实质价值,还是在不必要的复杂度上做加法?

## 可选方案

### 方案A: 建立Obsidian Vault
- 每支标的一个markdown页(US.AAPL.md),累积历次deep_analysis的narrative
- wikilink关联同板块/同主题标的
- 定期从SQLite同步关键数据(战绩、IC、pattern)
- 人类可读+可搜索+可交叉引用

### 方案B: 不建Obsidian,增强现有SQLite的报告层
- 加一个export_report.py: 从现有DB生成per-symbol markdown报告(按需生成,不持久化)
- 保持SQLite为唯一存储层
- 报告是view不是source of truth

### 方案C: 在strategy_memory.db加narrative字段
- symbol_memory表加latest_analysis_json字段
- 每次deep_analysis写入时更新
- LLM通过SQL读取(已有路径),不引入新工具

## 数据与实证

1. `sqlite3 data/signal_snapshots.db "SELECT COUNT(*) FROM signal_snapshots"` → 838条
2. `sqlite3 data/strategy_memory.db "SELECT COUNT(*) FROM symbol_memory"` → 94条
3. `sqlite3 data/strategy_memory.db "SELECT COUNT(*) FROM lesson_log"` → 154条
4. `sqlite3 data/ftseek_us.db "SELECT COUNT(*) FROM lessons WHERE status='active'"` → 74条
5. `python3 -c "import json; d=json.load(open('us.json')); print(len(d['historical_lessons']), len(d['tagged_lessons']))"` → 56, 89
6. claim_ic_cache.json: 215样本, overall_ic_t1=0.0123, overall_ic_t5=0.2962
7. llm_synth.py:561 synthesize()返回bull_case/bear_case/verdict/execution_plan JSON
8. strategy_memory.py:103 update_from_trade()已有per-symbol统计积累
9. 5层存储已全部接入LLM反馈闭环(claim_ic→prompt, symbol_memory→signal_gate, us.json→daily prompt)
10. OBSIDIAN_VAULT_PATH未设置(grep无结果), 系统无Obsidian集成

## 讨论记录

### 第 1 轮：hermes

**立场: 支持方案B(增强报告层),反对方案A(Obsidian Vault)**

核心论点: 系统已有5层数据存储且全部接入LLM反馈闭环。Obsidian引入第6层是为人类可读性付费,但FTSeek的消费者是LLM prompt不是人类。做减法原则下,报告生成器(virtual view)比持久化vault(physical copy)更短。

**证据1: 5层存储已覆盖全部需求**
- signal_snapshots.db: 信号+IC (838条, 回填率: realized≥1 = 718/838 = 85.7%)
- strategy_memory.db: per-symbol战绩 (94条symbol_memory, 含win_rate/best_win/worst_loss)
- ftseek_us.db lessons: 74条active教训
- us.json: 56+89条历史/tagged lessons
- claim_ic_cache.json: 论据级IC反馈(215样本)
→ grep验证: 5层全部有消费路径(signal_gate.py读strategy_memory, llm_synth.py读claim_ic, MemoryManager读us.json)

**证据2: LLM深度分析产出不是"丢失"的**
llm_synth.py:594-609显示分析结果写入result dict, pipeline.py:199-200调用ic_writer.write_signal_snapshot(analysis)写入signal_snapshots.db的factors_json字段。分析数据持久化了,只是不可读。

**证据3: Obsidian的增量价值=人类可读+交叉引用**
- 人类可读: export_report.py从SQLite生成markdown即可,不需要vault
- 交叉引用: SQLite的JOIN等价于wikilink (SELECT * FROM symbol_memory WHERE symbol IN (SELECT symbol FROM trade_records WHERE sector='tech'))
- 图谱视图: 炫酷但不产生交易alpha

**证据4: 用户设计哲学明确反对复杂度**
memory记录: "最短路径——一个缓存文件+几个读取点,而不是新模块/新DB表/新服务。复杂度是敌人。" Obsidian vault = 新存储层 + 同步层 + 维护层。

**证据5: 真正的gap不是存储,是分析质量**
claim_ic_cache.json显示overall_ic_t1=0.0123(几乎无预测力)。问题不在缺少存档,在分析本身不够好。Obsidian存档不提升IC。

**让步前提**: 如果用户本人经常需要人工阅读per-stock分析来做决策(而非纯系统自动交易),则Obsidian的人类可读性有真实价值。这取决于用户的运营模式。

### 第 1 轮：claude-code

**立场：反对 Hermes 的“反 Obsidian”立场；应复用已有 ObsidianWriter 做 per-symbol 深度存档。**

Hermes 的事实前提错误：系统已有 Obsidian 集成。`app/core/obsidian_writer.py:26` 已实现双写；`config/unified_config.yaml:1060` 配置了 `obsidian_vault`；`app/review/daily_review.py:1260` 和 `app/pipeline/order_executor.py:5340` 已写入每日复盘与交易笔记。说“系统无 Obsidian 集成”不成立。

Hermes 将“不要新存储层”偷换为“不要 Obsidian”。Obsidian 不是第 6 层新存储，而是已有 DB 的人类可读视图+索引层。现有 5 层以信号/交易/教训为中心，`signal_snapshots.factors_json` 只是单次信号快照，形不成 per-symbol 的 bull/bear 演进、执行计划对比和因果链修正。

用户明确要“每支股票的战绩和分析以及结果沉淀”。这正是 `ObsidianWriter` 缺失的 per-symbol note。扩展它（如 `symbols/US.AAPL.md`），把每次 `deep_analysis` 的 verdict、causal_chain、execution_plan、IC 反馈追加进去，才是“最短路径”——复用已有 vault，只新增一个 writer 方法。

结论：反对 Obsidian 是假问题；真问题是把 narrative 从 SQLite JSON 释放到可迭代、可搜索、可交叉引用的知识页。SQLite JOIN 替代不了 wikilink/MOC 带来的长期结构。

### 第 2 轮：hermes

**接受: Claude Code 的事实纠正——我的R1前提"系统无Obsidian集成"是错的。**

grep验证确认:
- `app/core/obsidian_writer.py:26` ObsidianWriter类已存在(553行,2026-05-04创建)
- `daily_review.py:1260` 盘后复盘双写vault
- `order_executor.py:5340` 平仓交易双写vault
- vault现有59个md文件(trades/41 + daily/18)
- config/unified_config.yaml:1060 `obsidian_vault: obsidian_vault`

**但维持核心立场: 当前vault缺少用户要的per-symbol深度分析归档。**

验证vault实际内容:
- `ls obsidian_vault/symbols/` → NO symbols/ directory
- trade笔记是单笔快照(entry_price/exit_price/pnl),不是deep_analysis的bull_case/bear_case/verdict/execution_plan
- daily笔记是日报摘要,不含per-stock叙事
- deep_analysis产出(llm_synth.py:561 synthesize())写入signal_snapshots.factors_json,从不写入vault

**修正立场: 支持方案B变体——扩展现有ObsidianWriter,而非新建vault**

最短路径: 在ObsidianWriter加一个`write_symbol_analysis(symbol, analysis)`方法:
1. 创建`symbols/US.AAPL.md`(不存在时)
2. 追加`## 2026-07-08` section,含verdict/bull_case/bear_case/execution_plan
3. frontmatter记录latest_direction/latest_strength/total_trades/win_rate(从strategy_memory.db读)
4. wikilink关联`[[US.SPY]]`等同板块标的
5. 在pipeline.py:199 `ic_writer.write_signal_snapshot(analysis)`旁加一行`obsidian_writer.write_symbol_analysis(symbol, analysis)`

这是1个新方法+1行调用,不是新模块/新DB表/新服务。符合用户"一个缓存文件+几个读取点"原则。

**对Claude Code R1第3点的回应**: 同意"signal_snapshots.factors_json是单次快照形不成连续演进史"。这正是gap。但解决方案是扩展ObsidianWriter(per-symbol追加),不是引入新存储层。

**对Claude Code R1第4点的回应**: 同意"扩展已有ObsidianWriter新增per-symbol note是最短路径"。这就是我的修正立场。

**收敛点**: 双方同意扩展ObsidianWriter而非新建。分歧只剩实现细节。

### 第 2 轮：claude-code

**立场：同意收敛，但对 `write_symbol_analysis` 实现细节保留 4 点concern。**

1. **幂等与去重**：同一天同一标的若多次运行 deep_analysis，会重复追加。方法内部应做 `date + signal_id` 去重，或先检查同节标题是否已存在再 append。
2. **frontmatter 刷新**：Hermes 提到 frontmatter 放 latest_direction/win_rate，但追加新节时应同步更新 frontmatter，否则笔记内出现多个 latest_ 造成误导。
3. **wikilink 的 peer 来源**：同板块标的从哪来？若依赖 `sector` 字段，需确认 pipeline 传入 analysis 时已携带；否则退化为空，失去交叉引用价值。
4. **失败不应阻塞主链路**：Obsidian 写入应是 best-effort，失败时只记 warning 不抛异常，避免 deep_analysis 成功却因 vault IO 失败导致信号丢失。

建议接口：`write_symbol_analysis(symbol, analysis, overwrite_latest=False)`，内部追加节后更新 frontmatter，异常吞掉并 log。

**结论**：方案 B 变体可行，实现时注意上述边界即可落地。

## Action Items

### 收敛结论

双方一致同意: 扩展现有ObsidianWriter加`write_symbol_analysis`方法,不新建vault/模块/DB表。

### P0: 实现 write_symbol_analysis (1方法+1行调用)

- [ ] 在ObsidianWriter加`write_symbol_analysis(symbol, analysis)`方法
  - 创建`symbols/US.AAPL.md`(不存在时)
  - 追加`## YYYY-MM-DD` section: verdict/bull_case/bear_case/execution_plan/causal_chain
  - frontmatter同步更新: latest_direction/latest_strength/total_trades/win_rate(从strategy_memory.db)
  - 幂等: date+signal_id去重, 同日同标的不重复追加
  - best-effort: 失败只log warning不抛异常
- [ ] 在deep_analysis pipeline.py:199 `ic_writer.write_signal_snapshot(analysis)`旁加`obsidian_writer.write_symbol_analysis(symbol, analysis)`
- [ ] wikilink peer: 从analysis中已有的sector字段关联同板块标的(无sector则跳过)

### 验证

- [ ] 跑一次deep_analysis, 确认symbols/US.XXX.md被创建
- [ ] 确认同标的第二次运行不重复追加
- [ ] 确认vault写入失败不阻塞pipeline

