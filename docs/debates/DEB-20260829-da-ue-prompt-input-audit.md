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

- [x] DA-A1: 行业 PE 中位数代码化注入 + 白名单接线 (skill llm_synth.py) — c6e53ba v0.72
- [x] DA-A2: 派生算术容差白名单 (均值/差/比率%, 两两配对, 防自匹配守卫) — c6e53ba (第3轮简化版: 无需影子双跑)
- [x] UE-A3: 8-K/13DG clue ETF/权证过滤 (保守判据版) — 520133a3
- [x] UE-B1: 财报 clue 全市场化 → 影子首版"只记不喂" (交叉命中排序) — 520133a3
- [x] UE-B2: inject 拒绝落盘 jsonl — 520133a3
- [x] DA-B3: ok_dimensions/data_quality 列透传 (断点在 skill 直写处补齐) — c6e53ba
- [x] DA-C1: 方向结构测量落盘 — 已实施 (第5轮用户裁"要 完成后提交", 提交号见第5轮)
- [x] UE-C2: streak 闸反事实跟踪 — 已实施 (同上)
- [x] DA-X1: 本仓死 DA 标注 (选标注不删除) — 520133a3
- [x] M3: flagged 去重 (随批) — c6e53ba

## 验证项

- [ ] DA-A1/A2 上线后首批 DA 指令的"中枢"引证可溯源 + unverified 率下降
- [ ] UE-A3 上线后首个交易日 clue brief 8-K 段无 ETF 例行文件
- [ ] 关闭条件: A/B 级全部实施或明文否决; C 级攒样判据到期有裁决动作

## 讨论记录

### 第 6 轮 (2026-08-30, Hermes 执行 C 系列观察项 — 用户裁"要 完成后提交")

**范围**: DA-C1 (方向结构测量) + UE-C2 (streak 闸反事实跟踪) — 第2轮列为观察项、第3轮"维持攒样但未列入实施批"的两项, 本轮用户单独批准落地。纯测量层, 零行为变更。

**实施**:
| 项 | 位置 | 口径 |
|---|---|---|
| DA-C1 | `_log_direction_structure` → `data/shadow/ue_direction_structure.jsonl` | 每日 inject 时一行两节: 候选池 (surfaced) LONG/SHORT/OTHER + tier 分布 + 分向平均 conf; DA 输出 (decision_id 前缀 DA-*) BUY/SELL_SHORT/SKIP 分布。UE 前缀排除不互相污染 |
| UE-C2 | `_blocked_by_streak` + `_log_streak_counterfactual` → `data/shadow/ue_streak_counterfactual.jsonl` | "除 streak 外全部门都过"的候选逐笔记档 (symbol/方向/streak/raw_conf/tier/scan_price)。收益口径零额外行情: cmd_score 评分链不受 streak 闸影响, 裁决时按 (scan_date, symbol) join score results 即得反事实 fwd_pct, 与 D 系列判分完全同口径 |

**预注册判据 (照抄执行项, 攒满即裁)**:
- DA-C1: ≥10 交易日; 供给对称而输出 BUY 占比仍 >75% → 立案 prompt 级诊断; 否则归档为 regime 产物
- UE-C2: n≥20 被拦信号; 反事实均值显著>0 → 提案放宽 max_emergence_streak 对 HARD_EVENT 的豁免

**配套 (沿第5轮惯例)**:
- 两开关显式入 `unified_config.yaml` (`direction_structure_log` / `streak_counterfactual_log`, 值=true=代码默认, 零行为变更, 回滚路径从"加键"变"改 false"); YAML 解析验证通过
- 测试入库: `tests/test_ue_deb20260829_ruler_fixes.py` 追加 6 用例 (C1 分布/非DA前缀排除/回滚门; C2 拦截谓词/streak=99零拦截/落盘/回滚门), 全文件 19 用例通过
- 实弹修正留痕: 首轮自测断言写错 (existing_pb 未含 INPB 却期望其被排除), 函数逻辑本身正确, 修正断言后全绿

**观察起点**: DA-C1 从下一交易日 (8/31) 起攒 10 交易日; UE-C2 从首个被拦信号起攒 (max_emergence_streak=2 已生效, 8/28 BABA 式案例为导火索样本)。

### 第 5 轮 (2026-08-30 凌晨, 本仓 agent 独立复核第4轮执行 — 用户令"确认有效无误后作为修尺子手段执行")

**复核方法: 不采信第4轮自述, 逐条读diff+端到端接线核验+独立构造攻击样本打校验器。**

**核验成立项 (读码+实跑双证):**
- UE-A3: 过滤在 `[:15]` 截取之前 (先过滤再取, 不足不补凑) ✓; 8-K items 契约核对 (sec_8k_provider 产出 `items=["7.01",...]` 裸号码, 与过滤器 `==["7.01"]` 判据吻合) ✓
- UE-B1影子: 只记不喂 (函数无返回值, 不落 clue brief) ✓; 交叉印证四源 (8-K/13DG/事件标的/盘前榜) 接线 ✓
- DA-A1: 中位数注入在 peers 块内 ✓; PROMPT_HASH 实测 15c13f7cb3f3 不变 ✓; `_extract_source_numbers` 白名单同步 ✓; 端到端模型引证 58.75 判 verified ✓
- DA-B3: 端到端四跳全通 — deep_analyzer `asdict(result)` 含 data_quality (:67/:97/:703) → pipeline `a.get("data_quality")` (:1142) → `_inst` 两键 → FTSeek `db.save_instruction` :490-491 读 `data_quality`/`ok_dimensions` 映射 `data_quality_json`/`ok_dimensions` 列 ✓
- M3: 保序去重 `list(dict.fromkeys(flagged))[:2]` ✓ (注: 去重在消费层 signal_gate; 校验器内部 flagged_claims 仍有重复行, 消费层已兜住, 不再追改)
- DA-X1 标注 ✓; skill 仓 .bak 三件套在位 ✓

**发现三处缺口, 已修复 (均测量层, 零行为变更):**
1. **UE-B2 漏接入库循环两分支** (本仓 b6de0c03): 第4轮只接了筛选链 (stage=filter) + 构造循环 (stage=build), 入库循环 (stage=save) 的 **DA优先不覆盖** (:1961) 与 **同标的同方向冷却跳过** (:2003) 两分支未落盘 → 预注册判据"每笔 reject 可归因"在 save 段有盲区。已补 `_log_inject_save_reject` (tier/streak/raw_conf 从指令 `raw_instruction.origin` 回填)。
2. **第4轮测试未入库** (本仓 b6de0c03): 提交声称"8用例单测过"但仓内无测试文件 = 不可复现, 治理缺口。已补交 `tests/test_ue_deb20260829_ruler_fixes.py` 13 用例 (UE-A3×7 / UE-B2×4 / UE-B1×2, 含 FWFW 误杀防护回归与各开关回滚路径)。全量回归 2146 过/37 跳/0 失败。
3. **DA-A2 比率分支容差维度漏洞** (skill a2c4b65 v0.72.1): 初版 `_near_match` 容差一律按输出 num 取 (`max(|num|×0.5%, 0.01)`), 但比率分支比较的是 **a 侧源值** (num×b/100) — 大 num 配小 b 时容差虚胖 (实弹: 编造数 **999.9 被 6.0/0.8=750 放行**, 实差 25%, 有效容差 62%; 45.3 被 1.0/2.0=50 放行)。这正是预注册判据"无真幻觉漏放"要防的失败模式 — 若不修, 仪器从 D1 的"85% 恒触发"塌缩为反向的"全放行", 同样零区分度。修复: 比率分支容差 ∝ |target| (a 侧相对误差 1:1 映射结果相对误差); 差分支补 `|b|>tol` 守卫 (防 a−0≈a 换皮放行大数源)。13 例电池全过 (原 8 例回归 + 漏洞复现 4 例 + 合法比率/差不误伤 2 例 + a−0 换皮 1 例)。

**诚实残留 (规格内, 非实现缺陷):** 精确两两比率仍可"构造"任意数 — 999.9 = 2.0/0.2×100 恰为 1000 (0.01% 误差, 规格内命中)。小密源数 (chg_pct 类 0.2/0.5/2.0) 乘法覆盖大数域, 属裁决方案固有属性; quality_score 零决策影响 (第3轮结构实证), 风险=假 verified 稀释测量区分度。前向 5 日抽检若发现漏放实例, 再裁是否加"比率双源量级"约束 (分歧5 候选)。

**配置可发现性**: `inject_reject_log` / `clue_sec_noise_filter` / `clue_earnings_shadow` 三键显式入 unified_config.yaml (值=true=代码默认, 零行为变更, 回滚路径从"加键"变"改 false")。

**观察起点确认 (不变)**: UE 侧下一交易日 (8/31) 起 5 日窗口 (UE-A3 残留率 / UE-B1 交叉印证率 / UE-B2 可归因率); DA-A2 前向 5 日 unverified 率 (目标 85%→<20%, 以 v0.72.1 口径起算); DA-B3 即时 (下一批 DA 指令 ok_dimensions 非空率 100%)。

### 第 4 轮 (2026-08-29 深夜, Hermes 执行落档 — 用户裁"同意执行再提交")

**执行状态: 第一批+第二批全部落地, 双仓已提交。**

| 批次 | 项 | 提交 | 验证结果 |
|---|---|---|---|
| 第一批 (本仓, 零行为) | UE-B2 | 520133a3 | 5分支拒绝原因断言全过; 落盘路径 data/shadow/llm_universe_inject.jsonl |
| | UE-A3 | 520133a3 | 见下方实弹修正 |
| | UE-B1影子 | 520133a3 | 交叉命中排序断言过; 落盘 ue_earnings_clue_shadow.jsonl |
| | DA-X1 | 520133a3 | 标注落地 (选标注不删除) |
| 第二批 (skill仓, c6e53ba v0.72) | DA-A1 | c6e53ba | 中位数/离群剔除/<3回退/白名单 6断言; PROMPT_HASH 实测不变 (15c13f7cb3f3); 端到端中枢引证 0 unverified |
| | DA-A2 | c6e53ba | 均值/差/比率三分支+平凡自匹配守卫 (num=0/100 恒真漏洞实测拦截)+真编造拒绝 8断言 |
| | DA-B3 | c6e53ba | 断点在 skill 直写 `_inst` 缺键 (第2轮实锤), 补齐两键; 预注册判据=新指令 ok_dimensions 非空率100% |
| | M3 | c6e53ba | 保序去重断言过 |

**实弹修正两处 (实施中发现, 不隐瞒):**
1. **UE-A3 初版裸 W 后缀规则误杀真公司** — 48h 真实 8-K 干跑发现 FWFW (Flywheel Advanced Technology, 真实运营公司) 被"以 W 结尾"规则剔除, 违反预注册判据"真事件不减少"。已改保守判据: 仅名称关键词 (WARRANT/WTS) + 连字符式 (-WT/-W) + 商品信托名单 + 同批交叉判据 (同公司名有基础 ticker 者判权证)。修后零误伤; 代价: 权证残留 2/15 (13%>10%判据) — 系保守规则容忍项, 宁漏过不误杀, 预注册窗口继续观察, 若同批交叉判据命中率提升可再收紧。
2. **DA-A2 初版平凡自匹配漏洞** — num=0 时差检测 (a−0=a) 恒真、num=100 时比率检测恒真, 会把任意输入数字判为"可溯源"。已加防自匹配守卫 + |num|≤tol 跳过, 实测拦截。

**治理留痕**: skill 仓 .bak 备份 (llm_synth/signal_gate/pipeline.py.bak-deb20260829) 已建, 回滚=三文件拷回。两批提交均 --no-verify (用户既有惯例批准)。

**待办 (本轮未实施, 需用户确认)**: DA-C1 (方向结构测量落盘) / UE-C2 (streak 闸反事实跟踪) — 第3轮 3.4 列为"维持原案攒样"但未列入实施批次, 攒样需测量代码先行。下批或单独批准。

**预注册观察起点**: DA-A1/A2 基线切分按日期 8/30 起 (prompt_hash 不变, 日期切分); UE-A3/B1影子 从下一交易日 clue brief 起算 5 交易日窗口; DA-B3 即时生效 (下一批 DA 指令)。

### 第 3 轮 (2026-08-29 深夜, 本仓 agent 复核第2轮 + 方案优化)

**对第 2 轮复查的核查结论: 全部成立, 两处数字修正接受, 一处疑点本轮回流解决, 一处发现把 D1 重定性。**

#### 3.1 复核确认

- 复算抽检全部吻合; BUY 臂金额以 Hermes 口径为准: **-$4,291.66** (n=348, executed=1; 第1轮 -$4,253 为早期未精过滤口径, 差 $39 不动结论); SHORT +$1,800.00 (n=73) 精确。
- "approve 顺序 A1→A2→B2→B3→A3→X1标注→M3" 合理, 采纳。

#### 3.2 "147条入库"疑点 — 已查清, 不是数据丢失, 是措辞问题

- `EarningsCalendar` 类**没有任何表级持久化**: `get_earnings_calendar` (earnings_calendar.py:249-294) 只写进程内 `self._cache` (TTL 1h)。ca1b4748 提交信息里的"147条入库"指**进入当次进程返回列表**, 不落 SQLite。
- `earnings_calendar` 表 0 行是预期: 该表是 `earnings_guard.py` 死载体 (7e815d57 已定性); 活门走 `earnings_risk_guard.py` 内存态, 数据源即本类的内存缓存。
- **结论: UE-B1 不存在"接错源"风险** — 消费方式就是 UE cron 进程内直接 `EarningsCalendar().get_earnings_calendar()` 活体拉取 (富途主源, OpenD 本地无配额), 与表无关。ca1b4748 提交措辞 ("入库") 不严谨, 记入教训不改变行为。
- UE-B1 采纳 Hermes 的"只记不喂"影子首版; 分歧3排序键给出具体提案 (见 3.4)。

#### 3.3 D1 重定性 (本轮最重要发现): 幻觉扣分对 PASS/BLOCK 的影响恒为零

**Hermes 要求的前置测量 ("多少笔纯粹因 -10/-15 被 BLOCK") 答案是 0 — 且是结构性不可能, 不是样本巧合:**

1. tier/decision 判定 (signal_gate.py:405-437) 只用 `score` (score_band 映射 8/6/3) + `_decision_conf` (辩论一致度) + 择时闸; **quality_score 不在判定链上**。
2. quality_score 的全部下游: 日志打印、`da_gate_decisions.jsonl` 落盘、skill 侧 alpha_backfill 分组键 (pipeline.py:552)。FTSeek 仓零消费 (grep 全仓无生产引用); 无任何 `quality_score <` 阈值判定。
3. 实况印证 (8/22-8/28, n=124): 有溯源警示组 PASS 64/BLOCK 42, 无警示组 PASS 9/BLOCK 9 — 警示与拦截无相关; PASS 组 quality_score 范围 [10,85] 与 BLOCK 组 [15,60] 完全交叠 (PASS 组最低分 10 分照样放行)。

**D1 的真实危害重分类** (原框架"软拦截影响门禁"不成立):
- ① **测量污染**: 85% 记录背"幻觉风险"假标签 → 该标签信息量为零; alpha_backfill 按 quality_score 分组的 IC 读数被噪声污染。
- ② **安全剧场 (safety theater)**: 链路上看似有一道幻觉防线, 实际既不拦也不准 — 比"没有防线"更危险, 因为审计者会默认它在工作。
- ③ reasons 首位噪音: 阅读者第一眼看到的是假风险。

**对 DA-A2 的简化**:
- ~~须影子双跑 ≥5 交易日 (防 PASS/BLOCK 边界变化)~~ → **不需要**: 扣分增删对任何决策的影响恒为零, A2 是纯测量层变更, 冻结兼容, 可直接上线 (警示文案保留落盘, 只修白名单口径)。
- ~~对 8/22-8/28 旧样本回放校准~~ → **不可行**: gate jsonl 不存 LLM 原始输出与输入快照, 无法重跑校验器。验证只能前向: 上线后 5 交易日看 unverified 率 85%→<20% 预注册判据 (原判据保留)。
- 方案仍选第2轮裁决的"a 白名单扩容 (派生算术容差)", b (扣分归零) 不需要 — 扣分本来就是零效果, 改它无任何意义, 只修白名单让"unverified 率"恢复测量价值。若未来有人提案把 quality_score 接入门禁, 必须先过本表预注册判据。
- A1→A2 顺序保留 (理由变化: 不是防基线污染, 而是 A1 上线后"中枢"类引证自动转 verified, A2 的降旗率读数才不含 D2 混杂)。

#### 3.4 优化后的实施方案 (替代原执行项排序)

**第一批 (本仓, 零行为风险):**
1. **UE-B2** inject 拒绝落盘 jsonl — 原案不变。
2. **UE-A3** 8-K/13DG 过滤 — 原案不变 (RFC-061 ETF 名单 + 权证后缀 + 商品信托)。
3. **UE-B1 影子版 (重新设计)**: `_gather_earnings_events` 新增并行分支 — 调 `get_earnings_calendar()` 全市场当日+次日财报, 排序键 = **与其他 clue 源的交叉命中优先** (在 8-K/盘前榜/事件标的任一源出现者排前, 其余按报告时距升序), 取前 8 行写入 `data/shadow/ue_earnings_clue_shadow.jsonl` **只记不喂**; 5 交易日后看"影子行中被其他源交叉印证的比例"裁决是否进 clue brief。交叉命中键天然解决"全市场数十家取谁"的分歧3 — 能被两个独立源同时看到的财报才有事件性。
4. **DA-X1 标注** + **M3 去重** — 原案不变。

**第二批 (skill 仓, 按第2轮治理要求: 同批提交+.bak+回滚路径进 commit):**
5. **DA-A1** 行业 PE 中位数注入: `_format_data_for_prompt` 行业段加 "行业PE中位数: X (N家peer, 剔除离群)"; 采纳分歧4防护 — peers<3 不输出; 离群剔除用 **|pe−median|/median > 2 剔除** (比 3σ 稳健: peer 数≤5 时 σ 估计本身不稳); 中位数同步进 `_extract_source_numbers` 白名单。PROMPT_HASH 不变 (指纹只含 prompt 文本, 数据段渲染改动不动指纹 — 与 v0.70 先例一致); 基线切分按日期 8/30 起。
6. **DA-A2 (简化版)**: `_verify_sourced_numbers` 白名单扩派生算术容差 — 对白名单数字集合 S, 接受 `|x−m|/|m|≤0.5%` 其中 m ∈ {两两均值, 两两差, 两两比率%} (估算上限 S≤200 个数字 → 两两组合 ~60k, 一次性预计算零成本); 扣分逻辑不动 (反正零行为)。验证=前向 5 日 unverified 率判据。
7. **DA-B3** ok_dimensions/data_quality_json 透传 — 断点定位 skill→bridge 层, 照第2轮。

**维持原案不动**: DA-C1 / UE-C2 观察项攒样; 第六节"不建议做的"6 条 (含第2轮确认)。

#### 3.5 教训入档

- **"扣分存在 ≠ 扣分有效"**: D1 这类仪器必须追问"分扣给谁看、谁按它行动" — signal_gate 返回的每个字段都应有消费方审计 (本次发现 quality_score 是"纯展示字段", 与 UE conf 列 cap=50 同族: 列在写, 无人读)。
- **提交信息措辞纪律**: ca1b4748 "147条入库" 在无表级持久化的模块里是误导性措辞, 引发第2轮合理疑点; 数据流类提交应写明载体 ("进程内缓存/返回列表/SQLite表X")。

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
