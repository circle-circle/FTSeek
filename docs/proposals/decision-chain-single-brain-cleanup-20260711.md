# 决策链路 · 双脑清理方案 (2026-07-11)

状态: APPROVED (架构=纯双脑, fallback=technical, 周末执行下周验证)
作者: 审查驱动 (只读审查 → 用户拍板 Option 1)

---

## 0. 目标 (一句话)

产出**一条稳定、干净、无并行冲突**的决策交易链路:
**DA(deep_analysis)= 唯一入场决策脑;FTSeek = 执行脑**(触发/仓位/退出)。
DA 缺席时,由确定性 `technical` 策略兜底(独立归因、可关停),而非全 SKIP、也非噪声投票。

## 1. 审查结论 (为什么这么改)

- **3 模型 LLM 投票 (VotingCoordinator) 已全死**:scheduler 永远把 `voting_coordinator`
  赋值成 Bridge/Null 适配器 (scheduler_core L254/275),盘前 playbook 与盘中 scan loop
  拿的是**同一个对象** (signal_pipeline L647)。di_container 的真·VotingCoordinator 工厂
  是 DEAD fallback,永不触发。
- 盘中 `eval_strategy("voting_llm")` (signal_stage L551) **只是 DA 的中继**
  (读 `ctx.metadata["vote_result"]` = Bridge 输出)。无 bridge 即空。
- **真正的并行在盘中 scan loop**:ORB/technical/insider/earnings_revision 各自能
  **独立产生入场**,不经 DA(只有 fast_lane 受 battle plan 门禁)。
  数据实证 (trade_records strategy_id): 独立入场脑早已自然停火 ——
  `voting_llm` 169笔止于 6/5、`scalping` 129笔止于 5/18、`orb_engine` 末笔 6/22;
  6/24 起成交 **100% `playbook_sim`**(playbook/DA 通道)。
  即这些独立入场**代码活着但实盘已休眠**;本次双脑门是把既成事实用配置固化(防复燃)。
- **deep_analysis 方向门禁 (pipeline L786-868) 是可证空转**:bridge 候选在 L676
  刚把自己写进 signal_snapshots,门禁 L799 又查到**自己刚写的行** → 永远自我背书;
  且 conditions 注入 (L844) 对 list 调 `.get()` 必抛异常 → 恒 no-op。删除零行为变化。
- `FTSEEK_ALLOW_VOTING_FALLBACK` **只是一句日志文案**,无对应代码路径(幻影)。

## 2. 变更清单

### P1 — 双脑成形 (本周末, 全部带开关可回滚)

**P1-1 premarket technical fallback**
- 新增 `TechnicalFallbackAdapter` (app/playbook/pipeline.py):`vote(symbol, ctx)` 跑
  `evaluate("technical", ctx)` → 确定性方向;`strategy_id="technical_fallback"`,
  `is_neutral=False`,`std_dev=0`。technical 无方向 → 返回 None (SKIP,不硬凑成交)。
- scheduler_core:无 bridge 时,按 `playbook.fallback_when_no_bridge` 选
  `technical` (→TechnicalFallbackAdapter) 或 `none` (→NullVotingAdapter 全SKIP)。
- 数值(entry/SL/TP)仍由 `compute_instruction_values(data)` 确定性计算 —— 与 DA 同路径。
- **陷阱护栏**:纪律/风控无法把随机入场变正期望 → fallback 必须有确定性依据(technical),
  独立归因、可关停。默认可设 `none` 保守起步。

**P1-2 盘中双脑门 (signal_stage.py)**
- 新增 `signal_stage.independent_entry_enabled` (默认 false = 双脑)。
- false 时:ORB(L462)/technical(L510)/voting_llm(L538)/insider(L574)/
  earnings_revision(L594) 五个独立入场块跳过;**只保留 fast_lane**(执行 DA 计划)。
- 持仓管理/退出不受影响(在 ExitMonitor/position_monitor)。

**P1-3 删死门禁 + 接线简化 (pipeline.py)**
- 删除 deep_analysis 方向门禁整块 (L786-868,可证空转)。
- `voting_coordinator.vote()` 传 `_market_ctx`(SymbolContext)而非 merged dict
  —— 让 TechnicalFallbackAdapter 能 eval technical;Bridge/Null 不受影响。
- 移除 `_merge_playbook_data` 调用与函数(唯一用途是喂已死的 LLM prompt)。

**P1-5 修正矛盾注释/文案**
- scheduler_core:去掉 `FTSEEK_ALLOW_VOTING_FALLBACK` 幻影文案。
- pipeline `_get_voting_coordinator` / di_container 的 "fallback=VotingCoordinator" 注释。

**P1-6 provenance**
- 确认 trade_records 能区分 `deep_analysis` / `technical_fallback` / `fast_lane`;不足则补标签。

### P2 — 死代码中和 (本周末, 保守)

**P2-1 断掉 3 模型投票的复活路径**
- `_get_voting_coordinator`:scheduler.vc 恒被赋值,去掉"fallback 到 di_container
  真·VotingCoordinator"分支;万一为 None → 返回 NullVotingAdapter(SKIP),不复活 3 模型。
- **不物理删除** `voting_coordinator.py` / `voting_aggregator.py`:后者仍被 screener/
  condition_taxonomy 引用,前者留作 quarantine(不被 live 路径引用)。物理删除留待基线后。

**P2-2 决策契约(即本文档)**
- 单一入场契约:`DA bridge → BridgeVotingAdapter → _build_instructions_from_votes →
  save_instruction → battle plan → 盘中 fast_lane 触发(含 Lever A 重锚)→ 仓位 → 退出`。
- DA 缺席:`technical_fallback`(同一转换器与执行,独立标签)。
- 盘中不再有第二入场脑。

## 3. 不做 (显式排除)

- 不物理删 voting_coordinator.py / voting_aggregator.py(有共享引用,风险高)。
- 不动 compute_confidence 语义(已知"数据完整=90"问题,非本次范围)。
- 不做 DA↔FTSeek 接口的大抽象重构(验证周前求稳)。

## 4. 验证

- 每步 `py_compile` + 定向单测。
- 场景 A(有 bridge):走 DA;deep 门禁已删不再空转;盘中只 fast_lane 触发。
- 场景 B(无 bridge + fallback=technical):technical_fallback 出指令,打对标签。
- 场景 C(无 bridge + fallback=none):全 SKIP(保守回退)。
- 重启 ftseek-us:active、零 error;日志确认盘中独立入场已静默。

## 5. 回滚开关

- `playbook.fallback_when_no_bridge: none` → 回到"无 bridge 全 SKIP"。
- `signal_stage.independent_entry_enabled: true` → 恢复盘中多策略独立入场。

## 6. 下周验证锚点 (一条命令看链路)

```bash
./venv/bin/python scripts/check_decision_chain.py            # 近 7 天
./venv/bin/python scripts/check_decision_chain.py --days 30    # 近 30 天
./venv/bin/python scripts/check_decision_chain.py 2026-07-13   # 指定单日
```

判别键 = `strategy_id`(独立入场才带自己的 id),decision_id 前缀仅作 playbook
内部溯源。分桶:
- ✅ **双脑正路** `playbook_sim` → 再拆 `DA bridge` / `TFB 兜底` / `DEC 旧命名`
- ⛔ **独立入场泄漏** `orb_engine/scalping/voting_llm/technical/insider/...`(应恒为 0)
- ○ **运维/手动** `manual/auto_main/*_sync/shadow`

**通过标准**:近 7 天窗口内 ⛔ 泄漏 = 0(脚本打 `✅ 无并行泄漏`)。
`DEC-` 不会归零 —— 它是 bridge 命名前的旧 playbook 决策,会被 `DA-` 逐步取代,**非泄漏**。

基线实测 (2026-07-11):近30天含 1 笔 6/22 的 `orb_engine`(门前遗留,正确被标 ⛔);
近7天 0 泄漏、`playbook_sim` 占 88%(DA 12% + DEC 76%)、运维 12%。

## 7. DA 决策契约上链 (LLM 结合诊断后的最高杠杆项)

### 背景 (三块诊断结论, 2026-07-11)
- **校准闭环**:`voting_predictions` 1941 条 = 已退休 3 模型投票器墓志铭(confidence
  对命中率零区分, 41-48% 全程走平); DA 只在 `signal_lifecycle.playbook_sim` 有 48 笔
  (36% 胜率), 样本不足且 confidence 有量纲 bug(3.99 vs 别的 0-1)。bridge confidence
  是手工启发式(0.50 ±0.20/0.15), 从未按结果校准。
- **决策契约**:DA bridge 输出很好且可证伪(thesis + 时间约束 invalidation),
  但 FTSeek 在 playbook 边界把 thesis/invalidation/risks **全扔了**(`Instruction`
  无 invalidation 字段, thesis 被模板覆盖)。**最高杠杆缺口**。
- **Grounding**:初步以为缺口是 news_v2/sec_cache/factor_ic 未注入 prompt——
  **后经深查证伪** (见下 "Grounding 架构定论")。

### Stage 1 — 保留 + 落库 + 可见 (✅ 已完成 2026-07-11)
- `Instruction` 加 `invalidation` + `da_thesis` 字段(instruction_generator.py)。
- 构建指令时从 `vote_result` 带入(pipeline.py L1112);invalidation 入独立列,
  da_thesis 入 `raw_instruction_json`(不覆盖模板 thesis, 下游报表零影响)。
- `save_instruction` + 迁移加 `invalidation` 列(db.py)。
- **不动入场/出场逻辑**, 不需重启;bridge 缺席(TFB/Null)时字段为空串。
- 验证: py_compile + 往返/UPSERT 测试全过; 生产库缺列 → 周一盘前 ensure_tables 自动补。
- 核对: `./venv/bin/python scripts/check_decision_chain.py --invalidation`
  (DA 入场指令带 invalidation 的覆盖率, 目标 ~100%)。

### Stage 1 断点复核 (✅ 2026-07-11, 审阅 Agent 提 3 断点 → 逐条实证)

审阅 Agent 报告"DA→执行脑 3 个数据断裂"。逐条查代码 + DB + 内存库往返测试后定论:

| 断点 | 判定 | 实证 |
|---|---|---|
| **invalidation 全空** | 症状真, **根因误判** | DB `playbook_instructions.invalidation` 确 0/346 全空。但根因**不是**"藏在 `raw_instruction.invalidation`、`_inst` 顶层缺字段"—— `opportunity_scan_*.json` 候选的 `invalidation` **就在顶层**(值实存, 如 GOOG "若7/14前未回调至\$615…"),bridge L424 `cand.get("invalidation")` → vote L638 → Instruction L1113 → dict L1701 → `save_instruction` L180 全链正确。真因: **Stage 1 代码 07-11 落地, 最新指令是 07-10 → 尚无一次 DA 生成跑过新代码**。周一盘前 `ensure_tables` 补列 + 首次 DA 生成后自愈。往返测试已证 invalidation 落库正常。 |
| **position_size 全 0** | **夸大**, 但 DA 路径确有**审计断裂** | 非"全 0": `DEC-` 旧命名 169/169 有 size; 仅 `DA-` bridge 路径 13/16 审计列为 0。根因**部分误判**: re-price 块 (pipeline L2327-2372) **确实**兜底算了 size 且**确实**调了 `update_instruction_tracking`, 但 `_TRACKING_FIELDS` 白名单 (db.py L714) **漏了 `position_size`/`take_profit`** → 被静默丢弃。**仅审计断裂, 非功能断裂**: 成交单经 `reprice_params`→`execute_instruction` custom_params 正确带 size (近30天 playbook_sim 49/49 quantity>0, DA 单 NVDA 15/GOOG 9/NU 235)。**已修**: 白名单补 `position_size`/`take_profit`(reprice_from/to/original_sl 非 DB 列, 白名单正确忽略)。 |
| **invalidation_shadow 永不触发** | 真, 但为 claim1 下游 | trade_records.metadata 无 invalidation key 属实。但链路 (instruction→playbook_service L948 metadata→pipeline L3458 影子) 本身正确, **非代码断裂**; 上游 claim1 一自愈(周一 DA 带 invalidation), metadata 即有值, 影子开始评估。无需额外改动。 |

**修复**: `db.py _TRACKING_FIELDS` 加 `position_size`, `take_profit`。内存库往返测试证:
初始 save 落 invalidation; reprice 后 position_size 0→15、take_profit 195→196.5 持久化; 非 DB 列安全忽略。
**验证锚点 (周一 DA 生成后)**: `--invalidation` 覆盖率应升至 ~100%; DA 路径 `position_size>0` 审计列应随触发回填。

### Stage 2 — 结构化 invalidation + 影子触发 (待 Hermes 配合)
- DA 额外产出**结构化**字段, 例:
  `invalidation_struct: {exit_if_price_below: 615, valid_until: "2026-07-14",
  skip_entry_if_earnings_within_days: 3}`(Hermes 侧 bridge 契约改动, 工作区外)。
- repo 侧: `meta` 带上它 → `_check_playbook_exit_rules` 加一条 `DA_INVALIDATION`,
  **只 shadow(记日志不平仓)**, 采集"若按 DA 失效条件会怎样"的对照数据。
- 现状 invalidation 是自由文本(NL), 机器无法直接当触发器 → 必须先有结构化字段。

### Stage 3 — 影子达标后转实盘触发 + 入场门
- shadow 数据证明 DA_INVALIDATION 有正 edge 后, 翻成真实退出触发 + 入场闸门。
- 配置开关可关停, 与现有 SL/TP 优先级明确排序。

### 校准闭环 (并行第二优先)

**量纲修复 + 校准工具 (✅ 已完成 2026-07-11)**
- 根因: `signal_lifecycle.signal_confidence` 混合量纲 (snapshot 0-1 / trade_records
  0-100 / decisions 0-1 三源汇聚), 4 行 0-100 脏值把 `playbook_sim` 均值拖到 3.99。
- 修复: `update_signal_lifecycle._norm_conf` 在汇聚点归一 (>1.5 除 100, 夹 [0,1]);
  存量 4 行一次性 UPDATE 修复。playbook_sim 均值 3.99 → 0.694, 全通道回 [0,1]。
- 工具: `./venv/bin/python scripts/check_decision_chain.py --calibration`
  (confidence 分桶 vs 胜率/收益%; 样本 <50 只看结构不下结论)。
  度量用 `pnl_pct` 收益率而非绝对 $ — 避免仓位规模制度混淆。
- 基线实测 (量纲修复后, n=44): 与已死 3 模型投票器 (全程 41-48% 走平) 不同,
  DA 路径隐约单调 (<50%框14%/-0.32% → 80%+框42%/+0.35%)。样本不足, ≥50 再判。

**假警复盘 (2026-07-11): 早期用绝对 $ 误报 65-80% 桶 均PnL -$210**
- 真因: -$210 几乎全由 6/24 的 3 笔 (ORCL/XOM/LMT) 造成, 均为 **旧仓位规模
  制度** (单笔 ~$30K), 遭正常 2-3% 开盘止损→绝对亏损大; 当前 DA 单 ~$3.2K (9x 小)。
- 处置: 已把度量改为收益率% (制度无关), 65-80% 桶降为温和 -0.51%。仓位规模
  早已缩小, 无待修复项。教训: 跨制度小样本必须看 %不看 $。

**待办 (样本足后)**
- DA 样本 ≥30-50 前不迷信手工 bridge confidence(0.50 ±0.20/0.15);足量后
  用实际胜率重校准 confidence → 胜率映射。

### Grounding 架构定论 (2026-07-11 深查)

**结论: DA 脑是 skill 侧自包含的, 不读 repo 分析文件 → grounding 无法纯 repo 注入。**
- 驱动交易的 `opportunity_scan_*.json` 由外部 `~/.hermes/skills/deep-stock-analysis/`
  的**自有 pipeline 栈** (pipeline.py/deep_analyzer.py/llm_synth.py/data_collector.py) 生成,
  非 repo 的 `app/plan`。
- 数据源: `data_collector.py` 直接打 Finnhub API (基本面/分析师/内部人 form4) +
  futu 新闻; IC grounding 用 skill 自己的 `claim_ic_cache.json`。从 repo 只读
  `ftseek_us.db`(状态) + `.env.us`(密钥), **不读任何分析 JSON**。
- 三个目标重估: news_v2 → 非缺口(skill 用 futu, repo 源休眠); sec → 非缺口
  (skill 已从 Finnhub 拉 form4); factor_ic → 唯一可能增量, 但需改 skill 侧
  `llm_synth.py`(外向/难回退, 同 Stage 2 边界)。
- 隐患记录: 现有**两套 IC** — repo `factor_ic_latest.json`(factor_engine) 与 skill
  `claim_ic_cache.json`(alpha_backfill), 口径可能分叉; 将来统一 grounding 需先对齐。

**顺手修的真问题 (✅ 2026-07-11): `premarket_enrichment.json` 孤儿文件**
- 根因: 该文件停在 6/26 — 全仓**无任何写方**(旧写入代码已重构掉),
  模块 `collect_premarket_data` 是内存缓存(1h TTL)不落盘。活链路(llm_pipeline)
  用内存版, 新鲜; 但 3 个顾问路径(plan/pipeline.py + battle_plan 两处)读死文件。
- 处置: 3 处改为直接调 `collect_premarket_data()`, 顾问深研也拿新鲜盘前数据。
  不影响实盘(那三处均非交易驱动)。残留的 6/26 文件已无读写方, 可删。

## 8. 验证基线 (cutoff = 2026-07-11) — 哪些数据能用来验证

**核心纪律: 修复打时间戳, 验证卡在修复之后。不得用 cutoff 前的旧口径数据判修复成效。**

### 修复生效时间线
| 修复 | 代码落地 | 实际生效 | 验证起点 |
|---|---|---|---|
| 量纲统一 `_norm_conf` | 07-11 | 下次盘后 sync + 已回填存量 | 历史可见(已回填) |
| Stage 1 invalidation 落库 | 07-11 | **周一盘前** ensure_tables 自动迁移列后 | ≥ 周一的 DA 指令 |
| premarket 孤儿文件修复 | 07-11 | 即时(顾问路径) | 下次 dashboard 深研 |
| confidence 来源修复 | **未做** | — | 待实现后 go-forward |

### 数据可用性判定
- **只能当历史参考 (不用于验证修复)**: 所有 ≤ 2026-07-10 的成交/指令。
  无 invalidation 列; confidence 为旧来源(snapshot/consensus, 与指令不一致)。
- **可用于验证**: ≥ 2026-07-11 且派生自当日及以后 DA 重新生成的单。

### 验证方法 (周一及以后)
- 隔离旧数据: 所有校准/覆盖率查询加 `date >= '2026-07-11'`, 避免新旧口径
  混用(同 -$210 假象的跨制度污染坑)。
- 命令: `check_decision_chain.py --calibration` / `--invalidation`。
- 前提: 新单累积 ≥50 前只看结构不下结论。

### 时区备注 (防再误判)
- `decision_id` 内嵌时间戳 = **CST**(skill 分析时刻, cron 16:00 CST 启动);
  `created_at` = **UTC**(FTSeek 入库); `execution_time`/`exit_time` = **ET**(带显式 -04:00)。
- 跨表比时间必须先统一时区; GOOG 案例: DA 06:44 ET → 入库 07:08 ET →
  成交 09:59 ET, 决策确在交易之前(曾因 CST/ET 混淆误判为盘后补记)。

## 9. Dashboard 决策链路可视化 (✅ 2026-07-11)

**目标: 让 DA决策脑 → playbook执行脑 的单链路在 UI 上"感知进度 + 感知策略逻辑"。**
背景: 原 dashboard 把链路切碎在 作战计划/Playbook/交易/指挥中心 四页,`decision_id`
主线在 UI 上是断的; 决策脑健康/退出画像/校准 均无展示 (静默失败而非报警失败)。

**本轮落地 P0 两块 (新增 `app/web_dashboard/routes/chain.py`, 3 端点, 不动 dashboard.js):**
- `/api/chain/funnel` — 今日漏斗: 候选→指令→触发→成交→持仓→退出 (计数+转化率+指令列表)。
  候选=当日 bridge 候选数; 其余=playbook_instructions 按 date 聚合。
- `/api/chain/trace/{decision_id}` — 单笔时间线: 按 **decision_id**(唯一可靠联动键)
  拼 bridge候选→指令→成交→退出→归因 逐跳; 自动挂 **confidence 跨表一致性告警**
  (指令值 vs 归因值, 即 §校准闭环发现的 35 vs 0.70)。
- `/api/brain/health` — 决策脑健康: bridge 新鲜度/候选数 + 投票适配器推断
  (bridge/technical/null, 与 scheduler_core 同逻辑) + 降级告警。让"静默降级"可见。

**前端**: 指挥中心首屏"决策摘要"下方加 决策脑健康条 + 今日链路漏斗(指令可点击下钻
单笔时间线弹层)。自包含 HTML + 内联脚本, 60s 刷新, 复用 apiFetch。

**实测 (TestClient)**: 07-10 漏斗 4→8→3→3→0→3; GOOG 追踪正确串起 指令→成交→
退出(TIME_EXIT,亏)→归因 并触发 confidence 分歧告警; 07-11 无 bridge → 脑健康
正确报 degraded/technical 降级。

**待续 (P1/P2, 本轮未做)**: 退出画像模块(exit_reason 分布/时钟类占比/幽灵平仓)、
策略逻辑贯穿(指令卡显 thesis+invalidation)、校准视图(分桶 vs 胜率, cutoff 过滤)、
指挥中心减负(离线复盘类收进归因页)。

### P1 追加: 退出画像模块 (✅ 2026-07-11)

`/api/chain/exit-profile?days=30` — 把 exit_reason 归成 5 类可视化, 让"退出脱钩"可见:
- 归类: 时钟(TIME/PRE_CLOSE/EOD) / 止损(SL_*) / 逻辑(TAKE_PROFIT·信号) /
  券商(BROKER_GHOST_CLOSE·对账漂移) / 其他。
- 头条指标 + 三条自动告警: 时钟类占比≥40%、止盈占比<5%、幽灵平仓计数。
- 前端: 指挥中心占比条 + 分类图例(均收益/胜率) + exit_reason 明细表。

**实测 (近30天, 57笔)**: 时钟类 **50.9%**(均+0.11%/胜37.9%)、止损 21.1%(均-1.9%/胜0%)、
券商 19.3%、**逻辑类仅 7.0%**(均+1.07%/胜75%)。铁证: 唯一真正盈利的退出类别(逻辑)
最小, 而时钟+券商主导 → 退出与入场论点脱钩, 盈利被到点/兜底截断。

### 新增后的旧模块整合 (✅ 2026-07-11)

加了新模块后审重叠, 处理两处:
- **今日已平 → 并入今日漏斗**: 原独立卡与漏斗"退出环+指令列表"重叠。做法: 保留
  全部 id (todayClosedCard/tcCount/tcWl/tcPnl/todayClosedList), 整块搬进漏斗卡内
  当子区 "今日已平·全部来源" → dashboard.js 与 snapshot 零改动, 且保留非 playbook
  平仓的全来源视图。指挥中心少一张卡。
- **退出画像 → 下沉复盘区**: 原放在实时模块中间(时间尺度错位), 移到 因子性能 与
  完整分析 之间, 与资金曲线/深度复盘归为回顾类。实时(漏斗/脑健康) 与 复盘(退出画像/
  分析) 时间尺度分离。
- 其余(决策脑健康/单笔追踪/决策摘要/集中度/毕业门禁/因子)与新模块不冲突, 保留。

### P2 追加: confidence 来源修复 + 校准视图 (✅ 2026-07-11)

**根因终结**: `signal_lifecycle.signal_confidence` 此前取 `snap_conf or trade_conf or
consensus`(snapshot/trade/decisions), **不是 DA 指令原值** → 追 GOOG 时发现指令 conf=35
但归因 conf=0.70, 近 20 笔 19 笔不一致。校准分析用的根本不是决策脑真给的分数。

**修复**: `update_signal_lifecycle.py` 两条路径(`full_rebuild` + `incremental_update`)
都改为**优先取 playbook_instructions 的 DA confidence**(同主库同 conn 可直接 JOIN
decision_id), 再回退旧三源。`--full` 重建 397 行回填, GOOG/NU 归因 conf 0.70 → **0.35**
(DA 原值) 验证通过。go-forward 增量新单也走同源, 不再回退。

**校准视图**: `/api/chain/calibration?days=120&owner=` — signal_confidence 分桶
(<50%/50-65%/65-80%/80%+) vs 胜率 + 均 pnl_pct%, 判**单调性**(胜率是否随 confidence 递增)。
前端复盘区卡片(退出画像下方), 制度无关用收益率%不用绝对$。

**实测 (近120天, 256笔, 修复后 DA 原值)**: 胜率 **非单调** — <50%→47.4% / 50-65%→17.1% /
65-80%→25.3% / 80%+→41.0%, 呈 U 型。结论: **DA confidence 尚未校准**(高信心≠高胜率),
但这次是拿"决策脑真给的分数"在评, 校准闭环第一次能对着正确的量在观测。注意此窗口跨
cutoff/多制度, 属结构性观察; go-forward 干净样本需按 §8 卡 date≥07-11 单独累积。

### 本轮 Dashboard 交付总览 (P0+P1+P2)

新增 `app/web_dashboard/routes/chain.py`(5 端点: funnel/trace/brain-health/exit-profile/
calibration), 指挥中心实时区(脑健康条+今日漏斗+单笔下钻)+ 复盘区(退出画像+信心校准),
不动 dashboard.js/dashboard.py。`ftseek-dashboard.service`(:8901)已重启, 5 端点均 200/401
(与既有端点同为 auth-gated), 启动日志无异常。
