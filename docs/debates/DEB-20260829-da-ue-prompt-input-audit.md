# 议题:DA/UE 链路 prompt 与信息输入增量审计 — "指令书要求 vs 实际供给"错位病灶

## 状态

- [x] 进行中
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

> 创建: 2026-08-29 (用户命题: "再检查分析 DA 和 UE 的逻辑, 包括 prompt、各类信息输入, 核查准确性合理性以及有效性" → "总结问题给出解决方案, 输出 DEB 文档供另一 agent 辩论")
> 审计对象: DA = Hermes skill `deep-stock-analysis` v0.71 (生产, `/root/.hermes/skills/deep-stock-analysis/scripts/`); UE = `scripts/llm_universe_scan.py` v2.6
> 方法: 以前置审计为基线只做增量 — UE 基线 DEB-20260825-ue-prompt-chain-audit (8 病灶/U1-U7/P1-P11), DA 基线 DEB-20260825-outcome-feedback-loop-revival (r11-r15) + DEB-20260826-da-chainaudit-review (v0.70)
> 实况样本: UE 8/28 scan log 全文 (clue brief 原文); DA gate 决策 8/22-8/28 n=124; DA 指令 8/24-8/28 n=73; llm_audit.db; playbook_instructions
> **本轮零代码变更, 全部方案待裁决。**

---

## 一、不复议清单 (前置审计已覆盖/已裁决, 防重复提案)

| 项 | 状态 |
|---|---|
| UE 版本戳/hash 落库 (U1)、invalidation 顶字段透传 (U2)、critique 删除 (U3)、契约观测模式 (U4)、streak 硬闸 (U7)、U11 确认门 | 已落地且实测健康 (8/28 hash 249ffab0da66 与代码实算一致; contract_stats clean; 确认门 3 天攒样正常) |
| UE conf 列 cap=50 塌缩 (病灶2)、tier 锚点未验证 (E3)、执行账本与影子口径并置 (U6) | 已知未决, 维持前置审计结论 |
| UE 模型降档 qwen3.8-flash (8/27 用户裁决)、挑战者换 deepseek-v4-pro-0813 (8/27 用户裁决) | 已裁决, 归因字段在位, 攒样中 |
| DA score_band 三档映射 {8,6,3}、契约 repair、claim-IC 负反馈拦截 | 实测健康 (HIGH 25/MED 63/LOW 10 无塌缩; repair 34/124; news_positive 拦 21 次) |
| DA v0.70 字段错配 (PE=0/市值=0 假象) | 已修复并有契约测试钉住 (test_da_fundamental_contract.py) |

---

## 二、新病灶台账 (全部本机复算, 可复核)

### D1. DA 幻觉校验器"狼来了" — 85% 报警率, 仪器已塌缩为背景噪声

**证据**:
- 8/22-8/28 gate 决策 n=124, **106 条 (85%)** reasons 含"来源验证: N个数字未在输入数据中溯源"。
- 矛盾结构: SYSTEM_PROMPT KO 示例**推崇**派生计算 ("26家机构目标价均值85.97仅余+2.2%" 作为质量标杆, llm_synth.py:124-126), 而 `_verify_sourced_numbers` 白名单 = prompt 全文数字 + K线派生 (llm_synth.py:1840-1930), **不覆盖模型按 prompt 要求做的派生算术** → signal_gate.py:349 扣分 (-10/-15) 并标"幻觉风险"。
- 实测例: 8/28 ESTC "PE(TTM) 23.9倍显著低于网络安全/软件Peer中枢" 被标未验证 — 23.9 在输入里, "中枢"是模型按 prompt 规则 D 自算的, 被判幻觉。
- **越遵守 prompt 越被罚** → 警示 firing rate 85% → 真幻觉混在噪声里不可辨 (M3 信息塌缩: 恒触发信号携带零区分度)。

### D2. DA prompt 要求 vs 输入供给错位 — "必须对比行业中枢"但中枢从不给

**证据**:
- prompt 硬规: "D. 估值锚定: PE/PB必须与行业中枢对比, 不能只说'PE 27.7'而不说'高于行业中位22.3'" (llm_synth.py:147)。
- 输入实况: 行业对比段只有 ≤5 个 peer 各自 PE (llm_synth.py:518-536), **无中枢/中位数行**。
- 后果: 8/28 指令文本实测多处自编中枢 — UBER "PE 15.43倍显著低于科技成长中枢"、ORCL "低于科技Peer中枢(86.5x)"、AFRM "标准化PE 502.6倍远超行业中枢"。中枢值无从溯源 (喂给 D1 校验器当幻觉罚)。
- 同族先例: score_band 锚点③"数据充分性"8/25 已改为"代码算好注入, 模型只读" (v0.69) — 本病灶是同一原则未覆盖到估值锚。

### D3. DA 方向塌缩 vs 盈亏结构倒挂 — 先测量, 不动 prompt

**证据**:
- 8/24-8/28 DA 指令 (decision_id LIKE 'DA-%'): **BUY 67 / SELL_SHORT 6**; 8/28 全天 **17 条全 BUY**, confidence 列恒 70。
- 供给端对称: market_scanner.py:107-122 涨幅榜+跌幅榜+量榜多渠道, bullish/bearish 双桶 — 偏斜发生在合成/门禁层, 不在候选供给。
- 账本对照 (6/1 起 playbook 执行账本): BUY 臂 -$4,253 (n=348) vs SHORT 臂 +$1,800 (n=73) — **产出方向结构与已实现盈亏方向相反**。
- 诚实边界: 8 月下旬市场反弹期, 单周 BUY 偏斜可能是 regime 产物而非 prompt 偏科; SHORT 三问是已裁决的结构性约束, 不为对称而对称。

### U1. UE 8-K clue 噪声 ≈50% — ETF 例行文件稀释硬事件段

**证据** (8/28 scan log clue brief 原文):
- 15 条 8-K 中 7 条为商品 ETF 例行 7.01 Reg FD (CPER/BNO/UNL/UGA/USL/UNG/USO — 此类 ETF 每日都有, 无事件性) + 1 条权证 (OSRHW); 真股权事件仅 ~5 条 (LH/STLE/UNF/SAIC + 微盘 IBIO/APRE)。
- 13DG 段同样有噪声: "举牌 PERFF: SC 13 (被动持仓)" — 优先股 + 表单名截断。
- 对照: RFC-061 (8/29) 已给盘前榜 clue 做了 ETF 剔除/质量门; 8-K clue 无同等过滤 (`_build_clue_brief` 752-763 行直取 fetch_recent_8k 前 15 条)。

### U2. UE 财报 clue 自指 — 扩圈模块的信息源画地为牢

**证据**:
- `_gather_earnings_events` (llm_universe_scan.py:517-537) 走 `get_recently_reported(days_back=2)` + `get_watchlist_earnings(days_ahead=3)`; 两者内部均过滤到 KEY_US_SYMBOLS watchlist (earnings_calendar.py `get_recently_reported`: `if ticker not in watchlist_futu_map: continue`)。
- 实测: 8/28 clue brief 健康头 "财报:0", 而同日 event_symbols 文件列出 8 家全市场财报 (BRID/BWLP/HAFN/MNSO/UI/ZONE 当日 + LX/SAIC 临近) — **宇宙外的财报事件一条都进不了发现层**, 与 UE 定位直接矛盾。
- 8/29 财报源序修复 (ca1b4748, 富途主源全市场 281 条/双窗口) 已把全市场日历备妥, 但消费端 watchlist 过滤仍在 — 修复红利未到 UE。

### U3. U5 悬空 — inject 拒绝仍不可结构化归因

- 8/28 STLE "无现价"被过滤只留 stdout; `data/shadow/llm_universe_inject.jsonl` 不存在。
- 前置审计病灶6 (SHORT 27→9 衰减黑箱) 的归因依赖项, 至今未落地。

### M 杂项 (三条, 随批处理成本低)

- M1: 本仓 `app/llm/prompts/deep_target_analysis.py` + `app/plan/deep_analysis_engine.py` 自 7/25 零调用 (llm_audit feature=deep_target_analysis 最后 2026-07-25), 仅 web dashboard battle_plan.py:637 引用 — 死仪器, 易误导后续 agent 改错地方。
- M2: DA 指令 `ok_dimensions`/`data_quality_json` 列 8/28 全空 — 数据覆盖统计在 prompt 头部算好了但没透传落库, 与 UE conf 列塌缩同族 (列级死)。
- M3: signal_gate.py:354-355 `flagged[:2]` 未去重 — 同一"未验证论据"行可重复出现 (8/28 ESTC 实测连打两遍)。

---

## 三、可选方案 (待裁决; 全部标注冻结纪律兼容性)

### A 级 — 低成本高收益, 建议优先

| # | 方案 | 动作 (最小实现) | 修病灶 | 成本 | 风险/回滚 | 冻结兼容 |
|---|---|---|---|---|---|---|
| **DA-A1** | **估值中枢代码化注入** | `_format_data_for_prompt` 行业对比段加一行 "行业PE中位数: X (由N家peer算得)" (peers≥3 才输出, 防两家伪中枢); 同步把该值纳入 `_extract_source_numbers` 白名单 | D2, 顺带降 D1 噪声 | ~30 行 (skill llm_synth) | 模型可见输入新增一行事实, 无门禁变更; 回滚=删该行 | ⚠️改模型所见输入, 需批准; 不动阈值 |
| **DA-A2** | **幻觉校验器去仪式化** (二选一, 见分歧1) | 方案a: 白名单扩"派生算术容差" — 输出数字若可表示为白名单成员的简单运算 (两两差/和/比率/均值 ±0.5% 容差) 则视为可溯源; 方案b: 校验器降级纯观测 (扣分改 0, 警示保留落盘), 攒样后再定新阈值 | D1 | a≈半天, b≈10分钟 | **两者都改变 quality_score → 影响 PASS/BLOCK 边界**, 须先影子双跑 (新旧分并算落盘不生效) ≥5 交易日再切换 | ⚠️改评分=改执行行为, 影子先行 |
| **UE-A3** | **8-K/13DG clue 过滤** | `_build_clue_brief` 8-K 段加 ETF/权证剔除 (复用 RFC-061 ETF 名单 + 权证后缀规则 W/R/WS + 商品信托列表), 过滤后不足 15 条不补凑 | U1 | ~20 行 | 纯输入降噪; 回滚=配置开关 clue_8k_filter: false | ✅输入层, 不动执行 |

### B 级 — 接通既有资产

| # | 方案 | 动作 | 修病灶 | 成本 | 风险/回滚 | 冻结兼容 |
|---|---|---|---|---|---|---|
| **UE-B1** | **财报 clue 全市场化** | `_gather_earnings_events` 改读 `get_earnings_calendar()` 全市场日历 (8/29 修复后 281 条在库), 按 | 距今日天数 | 升序取前 8, 标注 watchlist 内外; watchlist 段保留 | U2 | ~15 行 | clue brief 变长 ≤8 行; 回滚=改回旧调用 | ✅输入层 |
| **UE-B2** | **U5 落地: inject 拒绝落盘** | 新增 `data/shadow/llm_universe_inject.jsonl`: 每个 reject 分支一行 {date,symbol,direction,reject_reason,raw_conf,tier,streak} | U3 | 2h | 纯观测 | ✅测量层 |
| **DA-B3** | **指令列透传补齐** | bridge/adapter 把数据覆盖统计写入 ok_dimensions/data_quality_json 列 (值已在 skill 侧算好) | M2 | ~10 行 | 纯落库 | ✅测量层 |

### C 级 — 观察项 (只测量, 不动行为)

| # | 方案 | 动作 | 修病灶 | 预注册 |
|---|---|---|---|---|
| **DA-C1** | **方向结构测量** | 每日落盘 "candidate 池方向分布 (scanner 双桶) vs DA 输出方向分布" 两行, 攒 10 交易日后裁决: 供给偏斜 vs 判断偏斜 | D3 | n≥10 日后, 若供给对称而输出 BUY 占比仍 >75% → 立案prompt 级诊断; 否则归档为 regime 产物 |
| **UE-C2** | **streak 闸反事实跟踪** | 被 streak 闸拦掉的 HARD_EVENT 信号 (如 8/28 BABA conf0.85 fwd+1.93% 命中) 逐笔记反事实收益入 jsonl | 附带 | 攒 n≥20 后评估闸的机会成本 |

### X 级 — 死代码治理

| # | 方案 | 动作 | 修病灶 |
|---|---|---|---|
| **DA-X1** | 本仓死 DA 标注或删除 | deep_target_analysis.py/deep_analysis_engine.py 头部加 "⚠️ 自 7/25 零调用, 生产 DA 在 skill 仓" 标注; 或连同 battle_plan 路由一起删除 (需先确认 dashboard 按钮无使用者) | M1 |

---

## 四、关键分歧 (供辩论)

1. **D1 修法二选一 (DA-A2)**:
   - 扩容派 (方案a): 派生算术白名单治本, 保留校验器对真幻觉的拦截力; 风险是实现复杂度和容差边界 (0.5% 容差会不会放过真编造?)。
   - 降级派 (方案b): 85% firing rate 的仪器留着也是噪声, 先归零攒样再重建; 风险是同期真幻觉失去软拦截。
   - 第三选项: a+b 组合 (先 b 归零, a 上线后直接以纯观测身份攒样, 双跑对比后再决定是否恢复扣分)。
2. **D3 是否够格立案**: 单周 67/6 + 账本方向倒挂, 是否足以启动 prompt 级诊断? 还是必须先等 C1 的 10 日测量? (本审计倾向后者 — 8 月反弹期混杂因子未排除。)
3. **UE-B1 全市场财报 clue 的信噪比**: 全市场每日财报数十家, 取前 8 的排序键是什么 (市值? 盘前异动幅度?) — 取错键会把 U1 的噪声问题换个入口搬回来。
4. **DA-A1 的 peer 口径**: peers 列表本身来自 Finnhub industry 分类, 若 peer 组不当 (如跨界 conglomerate), 算出的中位数是"精确的错误"。是否加 peer 数<3 不输出 + 与中位数偏离>3x 的离群 peer 剔除?

## 五、预注册判据 (挪门柱防护)

| 方案 | 判据 | 窗口 | 不达标动作 |
|---|---|---|---|
| DA-A1 | 上线后新 DA 指令中"中枢/中位"类引证数字 100% 可在输入文本中溯源 (被 D1 校验器 verified 而非 unverified) | 10 交易日 | <90% → 查白名单接线 |
| DA-A2 | 影子双跑期: 新旧评分分歧率 + 新白名单对 8/22-8/28 旧样本回放的 unverified 率从 85% 降至 <20%, 且**人工抽检 10 条降旗样本无真幻觉漏放** | 5 交易日影子 + 回放 | 降旗后仍 >40% → 白名单实现有误; 抽检发现真幻觉漏放 → 容差收紧或回滚 |
| UE-A3 | 过滤后 8-K 段 ETF/权证行占比 <10%; 真股权事件行数不减少 (过滤只删噪声) | 5 交易日 | 误删真事件 → 名单过宽, 收窄 |
| UE-B1 | 财报 clue 行数从实测 0-2/日 升至 ≥3/日 (有财报的日子), 且浮现标的引用财报 clue 的比例上升 | 10 交易日 | 行数升但零引用 → 该 clue 无信息价值, 回滚 |
| UE-B2 | 连续 10 交易日每笔 reject 可归因; SHORT 衰减分解首次成表 | 10 交易日 | 缺行 → 修落盘点 |
| DA-B3 | ok_dimensions 列非空率 100% (新 DA 指令) | 即时 | — |
| DA-C1 | 攒满 10 日出分布对比表, 结论唯一 (供给/判断/混杂) | 10 交易日 | 混杂 → 继续攒样不立案 |
| UE-C2 | 攒 n≥20 被拦信号反事实收益 | 不限期 | 均值显著>0 → 提案放宽 max_emergence_streak 对 HARD_EVENT 的豁免 |

## 六、不建议做的 (对抗性自审)

1. ❌ **不为对称而对称改 DA prompt** — D3 未分清供给/判断偏斜前, 任何 "加强 SHORT 义务" 的 prompt 手术都是对着影子开刀 (UE 曾有反向教训: 审计实测多空对称无病)。
2. ❌ **不给 DA/UE 加新数据维度** — 七轴零分离教训; DA-A1 只用已采集的 peers 现算, 零新源。
3. ❌ **不动 score_band 映射/streak 阈值/任何门禁数值** — 冻结期。
4. ❌ **不删幻觉校验器本体** — 它是唯一盯着"模型编造数字"的仪器, 病在恒触发不在存在; 删了就连攒样数据都没了。
5. ❌ **不动 UE 模型 (qwen3.8-flash) 与挑战者模型 (deepseek-v4-pro-0813)** — 均 8/27 用户裁决, 攒样期未到。
6. ❌ **不追溯修旧数据** — 8/28 前的指令列空、警示噪声等保持原样, 口径切分靠日期+prompt_hash。

## 七、诚实边界

- 本审计全部数字本机复算, 但 DA 样本仍薄 (gate n=124/5 日); D3 的方向倒挂是真实现象, 因果未明。
- DA-A1/A2 改的是模型所见输入与评分器, **不承诺提升盈亏期望**; 核心收益是恢复测量仪器的区分度与消除 prompt-输入自相矛盾。
- skill 仓治理欠账提醒 (承 DEB-20260826): 该仓仅 6 文件在版本控制内, 本批若动 llm_synth.py/signal_gate.py, 落地时必须同步提交留痕 + .bak 备份, 回滚路径写进提交信息。

## 决策

- 维持**待用户裁决**; 推荐顺序: A 级三项 (DA-A1/DA-A2影子/UE-A3) → B 级 → C 级观察项随批。
- 负责人: 待指派 (实施 agent), 用户 (裁决)

## 执行项 (待批准后)

- [ ] DA-A1: 行业 PE 中位数代码化注入 + 白名单接线 (skill llm_synth.py)
- [ ] DA-A2: 幻觉校验器影子双跑 (方案由分歧1裁决)
- [ ] UE-A3: 8-K/13DG clue ETF/权证过滤 (scripts/llm_universe_scan.py)
- [ ] UE-B1: 财报 clue 全市场化
- [ ] UE-B2: inject 拒绝落盘 jsonl
- [ ] DA-B3: ok_dimensions/data_quality_json 列透传
- [ ] DA-C1: 方向结构测量落盘
- [ ] UE-C2: streak 闸反事实跟踪
- [ ] DA-X1: 本仓死 DA 标注/删除
- [ ] M3: flagged[:2] 去重 (随批)

## 验证项

- [ ] DA-A1/A2 上线后首批 DA 指令的"中枢"引证可溯源 + unverified 率下降
- [ ] UE-A3 上线后首个交易日 clue brief 8-K 段无 ETF 例行文件
- [ ] 关闭条件: A/B 级全部实施或明文否决; C 级攒样判据到期有裁决动作

## 讨论记录

### 第 2 轮 (2026-08-29 晚, Hermes 独立复查 — 逐条复算+方案裁决)

**复算结果 (全部本机独立重跑, 不采信第1轮自述):**

| 病灶 | 声称 | 独立实测 | 判定 |
|---|---|---|---|
| D1 幻觉警示率 | 8/22-8/28 n=124, 106条=85% | `data/da_gate_decisions.jsonl` 实测 **106/124=85%** | ✅ 精确 |
| D2 自编中枢 | prompt硬规D要求中枢/输入无中枢行 | `llm_synth.py:147` 硬规在; :518 行业段只有peer各自PE; UBER thesis 实测含"中枢"引证 | ✅ |
| D3 方向塌缩 | BUY 67/SHORT 6; 8/28全BUY conf恒70 | 8/24-28 含skip共73; 8/28 **17条全BUY**, confidence去重值=1 | ✅ |
| D3 账本 | BUY −$4,253/n348 vs SHORT +$1,800/n73 | executed=1口径: BUY **n=348✓/−$4,291.66**(差$39); SHORT **+$1,800.00/n=73精确** | ⚠️ 方向性结论成立, BUY金额小差 |
| U1 8-K噪声 | `_build_clue_brief` 直取前15条无过滤 | :752-763 实测 `fetch_recent_8k(...)[:15]` 无ETF/权证过滤 | ✅ |
| U2 财报自指 | 消费端过滤到watchlist | `_gather_earnings_events` 走 get_recently_reported, watchlist过滤在 | ✅ |
| U3 inject落盘缺失 | 文件不存在 | 实测不存在 | ✅ |
| M1 死仪器 | 7/25后零调用 | `llm_calls`表实测最后 2026-07-25 | ✅ |
| M2 列级死 | ok_dimensions 8/28全空 | 17条全空✓; **skill仓代码零命中该字段=透传断点实锤** | ✅ |
| D1 扣分锚点 | signal_gate.py:349 扣分−10/−15 | :349/:353警示文案在; :128 −10、:148 −15 在 | ✅ |

**⚠️ 必须修正的方案前提 (第1轮写作后的当天新事实):**

- **UE-B1 前提已部分过时**: 第1轮隐含引用了 8/28 d03b3cde "earnings_calendar 0行⇒门从未拦截⇒下线" 的结论; 但当天 13:35 **7e815d57 已纠错**: 下线依据指向的是死表 (`earnings_guard.py` 的 SQLite 载体), 真门在 `earnings_risk_guard.py` 内存活载体, **实际已拦截 85 次**, G6 已恢复在线 (yaml 现值 `earnings_blackout_gate_enabled: true`); 随后 **ca1b4748** 财报源序反转 (富途主源/双窗口, 声称 281 条/过去段 147 条入库)。
- **残留疑点**: 复查时实测 `earnings_calendar` 表仍为 **0 行**, 与 ca1b4748 声称"147条入库"矛盾 — 可能写入内存态/其他载体。**UE-B1 实施前必须先查清"147条"落在哪个表**, 否则消费端改造会接错源 (跨层断点同族陷阱)。

**方案裁决 (对抗性审查后):**

✅ **批准本批 (按序):**
1. **DA-A1** (估值中枢代码化注入) — 最优先, 同时治 D2+降 D1 噪声。**必须先于 A2**, 否则 A2 降旗率基线被自编中枢污染。采纳分歧4防护: peers<3 不输出 + 偏离中位数 >3σ 的离群 peer 剔除。
2. **UE-B2** (inject 拒绝落盘) — 纯测量, 历史"SHORT 27→9 衰减黑箱"归因前置。
3. **DA-B3** (ok_dimensions 透传) — 断点定位在 skill→bridge/adapter 层, 纯落库。
4. **UE-A3** (8-K ETF/权证过滤) — 纯输入降噪, 复用 RFC-061 名单, 不足15条不补凑。
5. **DA-X1 选"标注"不选"删除"** — 删除需先确认 dashboard 按钮无使用者, 标注零风险先做。
6. **M3** (flagged 去重) — 随批。

✅ **批准但影子先行 (不可直接生效):**
7. **DA-A2 选第三方案 (b归零 + a影子)**, 强制双跑 ≥5 交易日才切换 — 改 quality_score = 改 PASS/BLOCK 边界, 冻结期内只能影子。**补前置测量**: 8/22-8/28 有多少笔指令纯粹因 −10/−15 分被 BLOCK (扣分前后分档翻转数), 定仪器真实伤害面 + A2 回放校准基线。

🔄 **暂缓待澄清:**
8. **UE-B1** — 先查"147条"落点再实施; 建议首版"只记不喂"影子版, 全市场财报信噪比未验证 (分歧3排序键问题未决前不直接进 LLM 输入)。

✅ **维持不动:**
- C1/C2 观察项按预注册攒样;
- 第六节"不建议做的"6条全部正确 (尤其"不为对称改prompt"/"不删校验器本体": 85%是恒触发问题非存在错误)。

**治理要求**: skill 仓动 `llm_synth.py`/`signal_gate.py` 必须同批提交 + .bak 备份 + 回滚路径写进 commit。

### 第 1 轮 (2026-08-29, 本仓 agent 独立审计)

- 方法: 以前置审计 (8/25 UE 8 病灶 / 8/26 DA v0.70) 为基线, 只找增量; 实况核验取 8/28 UE scan log 全文 (clue brief 原文逐行) + DA gate 决策 n=124 + DA 指令 n=73 + llm_audit 全表。
- 关键实测: D1 的 85% firing rate (106/124); D2 的自编中枢文本证据 (UBER/ORCL/AFRM 指令原文); D3 的 67/6 与 17/17; U1 的 8-K 段 ETF 占比 7/15; U2 的 "财报:0" vs event_symbols 8 家对照。
- 附带确认健康项: UE hash 落库与代码一致 (249ffab0da66); DA score_band 无塌缩; 确认门/契约观测/claim-IC 拦截均在轨。
- 产出: 六病灶 + 三杂项 + 九方案, 全部待裁决, 零代码变更。
