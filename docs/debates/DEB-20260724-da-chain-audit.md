# 议题：DA 策略链路全景审计 — 标的范围 / 筛选机制 / conf 配置的本质与断点

## 状态

- [x] 进行中（第 1 轮：全链路实证审计完成，四个断点定位，方案待裁决/辩论方攻击）
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

> **本文用途**：回答用户 2026-07-24 "DA 可用标的范围、筛选机制、conf 配置到底怎么工作的"——逐段实证 + 断点定位 + 替代方案。DA skill 在工作区外（`/root/.hermes/skills/deep-stock-analysis/`，v0.61），本文含跨域证据锚点。
> **作者**：Qoder　**日期**：2026-07-24
> **前置关联**：[DEB-20260723-score-discrimination-collapse](./DEB-20260723-score-discrimination-collapse.md)（score 恒 7）、[DEB-20260724-debate-divergence-consumption](./DEB-20260724-debate-divergence-consumption.md)（弃权/反向拆分）、[DEB-20260724-challenger-model-ab](./DEB-20260724-challenger-model-ab.md)（挑战者模型）

---

## 一、链路全景（七段，全部实证）

```
① 标的发现: 全市场 6 渠道榜单 (涨幅/跌幅/量榜/财报超预期/内部人异动/夜盘)
   scan_limit≈10/渠道 → 合并去重 → 硬过滤: 价格<$5 剔除 + 市值过滤
        ↓ 打分排序 top_n×2
② 深度分析: ~7-11 只/天进 LLM (18 数据源 → qwen3.7-max 分析师
   → deepseek-v4-pro 挑战者×3采样) → score/direction/thesis/invalidation
        ↓
③ signal_gate (DA门, skill 侧): quality_score 基础50±7维加减分(历史胜率/论据有效性…)
   → tier(优/良/中/差/SKIP) + 择时闸(动量冲突降2档 + 破位硬买直接SKIP)
   → 优/良=PASS · 中=WARN · 差/SKIP=BLOCK
        ↓ BLOCK → 不写库; WARN/PASS → save_instruction 直写 playbook_instructions
④ conf 合成 (主工程 BridgeVotingAdapter): 基准0.50; 挑战者同意+0.20→0.70;
   分歧/弃权 -0.15→0.35; clamp[0.10,0.95]
        ↓
⑤ 执行门: 无 conf 硬门 (2026-07-10 删除, 见断点 B) → 触发侧质量门
   (低ATR/连败/VWAP/量能/时间窗/财报) → 市价单
⑥ 仓位: confidence_sizing_enabled=false → conf 不定仓
⑦ 归因: 发现渠道不落库 (raw_instruction_json 无 discovery_channel 字段)
```

**近 4 日 gate 实测**（da_gate_decisions.jsonl）：BLOCK 33-37/日、WARN 13-18/日、**PASS 四天合计 1**——真实成交的 DA 指令几乎全部是 tier"中"（WARN/tiny）。

## 二、四个断点（按严重度）

### 断点 A：conf 是"死字段"——三重失效叠加

1. **语义污染**（已立案 P0.2）：挑战者弃权与真反向同罚 -0.15 → conf 0.35 一锅端；
2. **无硬门**：2026-07-10 删除执行层 conf<65 门，删除理由是"G6 (conf<70) 已覆盖"——**但 G6 在 `_build_instructions_from_votes` 内，DA 直写路径（skill→save_instruction）完全绕过它**。前提在 DA 切直写后漂移，无人回头检查 → conf=35 全部照常成交（07-23 十五笔的机制原因）；
3. **无仓位消费**：confidence_sizing_enabled=false（因 conf 反向已拍平）。
→ **DA 输出的"信心"在下游既不拦、又不缩、也不加——纯记录字段。**

### 断点 B：tier 体系被 conf 污染钉死为二值

tier 判定要求：优 = score≥7 **且 decision_conf≥0.7**；良 = score≥6 且 ≥0.6。挑战者极少显式同意（弃权为主）→ decision_conf 常年 0.35~0.50 → **优/良几乎不可能出现**（四天 PASS 仅 1 笔实证）→ 实际有效档位只剩"中(WARN)/BLOCK"两值。score 恒 7 的坍缩（已立案）+ conf 污染，两个上游问题在 gate 层汇合，把七维打分体系压成了硬币。**P0.2 修复的收益比原以为的大：它同时解锁 conf 语义和 tier 分层。**

### 断点 C：标的范围本质是"当日热度榜"，且渠道级边缘不可度量

- 发现层以涨幅/跌幅/量榜为主力——**天然的追高杀跌选择偏差**，与已确诊的"回调接飞刀"入场形态耦合放大；财报超预期/内部人异动是事件驱动渠道，但**六个渠道谁贡献了指令、各自 IC 如何——不可回答**，因为 discovery_channel 不落库（raw_instruction_json 实查无此字段）。
- "标的范围够不够"是伪问题，真问题是**"哪个发现渠道有边缘"无法度量**。

### 断点 D：吞吐瓶颈在上游候选供给（SKILL v0.60 自认）

DA --top 5 → ~7 只/天进深度分析 → gate 81% BLOCK → 执行 4-9 笔。扩容的正确位置在②的入口（scan_limit/top_n），不在执行层（上限 30 从未打满）。当前 topup 用 technical 方向补量，是旁路不是 DA 增产。

## 三、判断与替代方案（不藏）

| # | 方案 | 治哪个断点 | 性质 | 判断 |
|---|---|---|---|---|
| A1 | **discovery_channel 落库**（skill 侧 raw_instruction 加一字段）+ direction_scorecard 加渠道切片 | C | 观测，零风险 | **最高优先**——"标的范围"问题的正确打开方式不是调大调小，是先量出六渠道各自的边缘，2-4 周后砍无边缘渠道 |
| A2 | conf 修复顺序：P0.2 弃权豁免 → conf 校准（C4）→ 再谈恢复 conf 门/sizing | A+B | 已立案链 | **顺序不能反**：在污染的 conf 上先加门 = 拿坏尺子量人。P0.2 的优先级因断点 B 上调 |
| A3 | score 区分度修复（强制排序配额，score-discrimination DEB + C2/S4） | B | 已立案 | tier 失效的另一半根因，与 A2 双管齐下 |
| A4 | 执行层 conf 兜底门恢复评估 | A-2 | 需辩论 | **不建议现在做**：conf 未修复前加门会按坏信号拦单（高 conf 桶 44.9% 命中的教训）；待 A2 完成后按校准后的 conf 重新评估 |
| A5 | 【架构级，仅提出不排期】DA 退居"分析器"、发现权交给"事件+榜单+IC 淘汰"的机械层 | C+D | 大辩题 | 长期方向：DA 方向 IC≈随机但其 thesis/invalidation/退出画像有价值——它更像"给定标的出论点"的分析器而非选股器。与 UE（事件推理发现）的分工应重划。证据未足，随渠道级 IC 数据积累后再议 |

## 四、诚实弱点

1. 本文基于当前代码/配置快照；DA skill 版本迭代频繁（v0.61），结论有时效性。
2. 断点 C 的"热度榜偏差"是机制推断——渠道占比无数据（正是 A1 要补的），可能事件渠道占比其实很高。
3. "PASS 四天仅 1"窗口只有 4 天，tier 钉死的定量程度需更长窗口确认（机制推导是硬的：0.35 < 0.6 恒成立）。
4. gate 的 7 维打分依据历史交易统计（strategy_memory），小样本下波动大，曾有历史污染（已修为软约束 30 天窗），其自身有效性未独立验证。
5. DA skill 在工作区外，A1 的落地需在 skill 侧改（跨域变更，须与 skill 迭代节奏协调）。

### 第 1 轮讨论记录

- 作者（Qoder，2026-07-24）：完成七段链路实证与四断点定位。核心裁定：①"标的范围"的真问题是渠道级边缘不可度量（A1 零风险先行）；② conf 是三重失效的死字段，且其污染向上钉死了 gate tier 体系（P0.2 优先级上调）；③ 执行层 conf 硬门的删除依据（G6 覆盖）在 DA 直写后前提漂移——横向断言未复查的活案例；④ 架构级方向（DA 退居分析器）证据未足仅立观点。交辩论方攻击点：A1 字段设计、断点 B 的定量窗口、A5 是否过早。

---

## 五、第 2 轮补正与用户三问（Qoder，2026-07-24）

### 5.1 【作者自我修正】吞吐量数字过时：实际 ~51 只/日进评估，非 §一 所写 "~7-11 只"

§一② 的 "~7-11 只/天进 LLM" 引自 SKILL v0.60 旧记载，**已过时**。da_gate_decisions 实测 07-23：**51 个去重标的**全部过 LLM 分析+gate（盘前 ET 06:00-07:59 单批，无重复评估）→ 15 个 WARN 放行写库（与当日 15 条 DA 指令逐只吻合）。真实漏斗：**多渠道合并 ~51 → gate 放行 29% → 15 条指令**。断点 D（吞吐瓶颈在上游）的结论不变但量级修正；方法教训：SKILL 文档自述 ≠ 当前行为，吞吐类断言必须用遥测实测。

### 5.2 watchlist 的现存职能：在 playbook 链路里已退化为"备胎 + 基础设施"

| 职能 | 状态 |
|---|---|
| playbook 主生成路径（screener 筛 watchlist） | **平时不走**——DA 盘前直写库后 run_playbook_generate 幂等跳过 |
| bridge 全缺席日的 fallback | ✅ TechnicalFallbackAdapter 对 watchlist 出确定性方向（备胎） |
| 富途 LV3 订阅/行情基础设施 + 主线（非 playbook）引擎 | ✅ 真实职能所在 |
| DA/UE 标的发现 | ❌ 完全不经过 watchlist（DA=全市场榜单，UE=事件推理） |

### 5.3 DA/UE 作用范围：哲学不同、实测零重合、碰撞有仲裁

- 发现哲学：DA = 当日热度/异动榜（价量行为驱动）；UE = 事件因果推理（新闻/硬事件驱动，能想到榜外间接受益标的）。
- 实测重合（近 3 日）：DA 13/14/15 只 vs UE 浮现 2/3/8 只，**同日交集 = 0**。
- 碰撞仲裁机制：指令表 (date,symbol) UPSERT，UE 注入时主动排除当日已有 DA 指令的标的（DA 优先，`llm_universe_scan.py:739`）。
- 结论：**无冲突**；真正的风险不是重叠而是相反——两源零重合意味着它们看的是完全不同的宇宙，若某日真正的机会同时被两源命中（交叉验证信号），当前机制反而把 UE 的独立确认丢弃（被排除而非加分）——"双源共振加分"可作为 A1 之后的观测候选。

### 第 2 轮讨论记录

- 作者（Qoder，2026-07-24）：回应用户三问 + 自我修正吞吐量（~51 评估/日 → 29% 放行 → 15 指令，非旧记载 ~7-11）；watchlist 在 playbook 链已退化为备胎+基础设施；DA/UE 零重合无冲突，反向提出"双源共振加分"观测候选。

---

## 六、第 3 轮：龙头名单的信号源辩论 + 订阅必要性（Qoder，2026-07-24，回应用户两问）

### 6.1 事实底座

- watchlist 实体 = `unified_config.yaml → watchlist`：us_stocks 49 只（龙头/行业 top）+ etfs 8 只（多数 enabled:false 归 ORB）；盘前实际订阅 44 只（QUOTE+K_1M，ADR-013 已精简订阅类型）。
- 订阅集 = 静态 watchlist；**当日 DA/UE 指令标的（真正在交易的）基本不在订阅内**，盘中取价靠快照 API 兜底（耗配额+延迟高于 push 缓存）。近2日触发评估 151 轮中 vol_gate_ratio=None（fail-open）占 30%（含豁免窗/数据缺失混合，归因待拆）。

### 6.2 问一裁定：龙头名单"充分用起来"的正确姿势不是再造一个信号源

静态名单本身不含时变信息、无内生边缘；"core_pool 机械浮现"是 ADR 已废弃的老路。三条合规路径：
1. **已经在用（补量源）**：topup 池 16 只实质就是龙头名单子集，走正式链+全部质量门+独立归因可度量。**建议：watchlist 与 topup pool 两张名单合一（单一真源），防漂移。**
2. **若坚持当发现源**：作为 DA scanner 第 7 渠道（core_leaders）进 51 只评估漏斗，**必须带 discovery_channel 标签（A1 前置）**，由 gate+渠道级 IC 用数据裁决它有无边缘；无标签不加（又一个不可度量源）。
3. **基础设施角色（价值最高）**：(a) 板块相对强度基准股（S6 逆风加速器/X2 双确认的参考系，UE peer_surge groups 已这么用）；(b) 流动性白名单（滑点最差的 MNSB 类小票反衬）。

### 6.3 问二裁定：静态订阅的主消费者已消失，但不能一键全退——应改"动态订阅"

现状恰好颠倒：**订了 44 只没人交易的，没订每天真正需要 20s 高频取价的指令标的与持仓**。建议改造（下周基建项，非今晚）：
- 盘前指令生成后动态订阅：当日指令标的 + 持仓 + SPY/QQQ/VIX 基准（~25-35 只），收盘退订；
- 静态 watchlist 订阅降为可配置（默认关或仅留基准 ETF）；
- 收益：配额释放 + CPU 降（已有专项优化先例）+ 指令标的取价从快照升级 push 缓存（触发/退出延迟改善，量能/VWAP 门数据缺失率收敛）。
- **前置核查（防 ADR-013 同款盲区反向版）**：退订前必须盘点静态订阅的隐性消费者（主线引擎虽弃用但相关循环是否仍读 push、dashboard、news enrichment 等）；vol_gate None 30% 中未订阅归因占比先拆清。

### 第 3 轮讨论记录

- 作者（Qoder，2026-07-24）：龙头名单不应直接复活为信号源（core_pool 老路），合规路径＝topup 合一/带标签第7渠道/基准+白名单基础设施；订阅应从"静态 watchlist"改"动态（指令+持仓+基准）"，退订前须盘点隐性消费者。交裁决：名单合一与动态订阅是否立项。

---

## 七、立项决策与前置发现（用户裁决"同意"，2026-07-24）

### 7.1 决策

- ✅ **立项 P-1：名单合一（单一真源 + 角色标签）**
- ✅ **立项 P-2：订阅动态化（指令+持仓+基准，静态降为可配置）**
- 时序约束：**今晚（07-24 ET）首夜验证期管线冻结，不动任何 live 代码/配置**；周末实施，周一（07-27）盘前验收。

### 7.2 前置发现 ①：名单合一**不能简单并集**

实查两张名单差异：watchlist enabled 44 只 vs topup 16 只，交集 12；topup 独有 4（NFLX/PLTR/QCOM/SMCI）；watchlist 独有 32——**其中含大量低波防御股（KO/JNJ/PEP/VZ/WMT/ABBV…），正是低ATR门的拦截对象**。若直接并集进 topup，等于把刚被数据否决的低波票塞回补量池（虽会被门拦住，但白耗评估）。**合一方案 = 单文件多角色标签**：`role: [benchmark | liquidity_whitelist | topup_eligible | subscribe]`，topup_eligible 仅高ATR流动性子集，防御股保留 benchmark/白名单角色。

### 7.3 前置发现 ②：隐性消费者初筛 12 个文件，需逐个定性

push/K_1M 关键字初筛命中 12 文件，含假阳性（app/push/* 是飞书推送非行情 push）；真实待定性：signal_pipeline（主线，用户称已弃用——需配置实证）、order_executor、position_exit_monitor（主线退出，playbook 仓已排除）、degradation_monitor、playbook 量能门 K_1M 兜底。**P-2 实施第一步 = 逐个确认这五处在静态订阅移除后的行为（fail-open/降级/无感）**；另需拆清 vol_gate None 30% 中未订阅归因占比。

### 7.4 执行项（周末）

- [x] P-2a 隐性消费者逐个定性（只读，**2026-07-24 当日完成**，见 7.5）；vol_gate None 归因拆分待周末（需盘中数据）
- [ ] P-1 单文件角色标签名单（config 重构，topup/订阅/基准从同一真源派生）
- [ ] P-2b 动态订阅：盘前指令生成后订阅当日指令标的+持仓+基准；静态订阅降可配置（默认仅基准）；开关可回滚。**（2026-07-24 补：实施路径已明确——统一框架现成，只需扩展一个回调。** 现有机制：`subscription_manager.start_auto_loop(codes_callback=_get_subscription_codes, interval=60)` 每 60s 取"应订集合"增量订阅（去重+冷却），时段切换时 unsubscribe_all 全退；但回调当前 = watchlist ∪ 主线 PositionStore（playbook 仓不入 PositionStore，注释自认 playbook-only 场景下恒为空）。**P-2b 实施 = 扩展 `_get_subscription_codes`（scheduler_core:1078）加两个源：当日 playbook_instructions 有效标的 + playbook 未平仓标的；退订复用现有时段机制（收盘全退，次日回调自然重建）**。待确认：Futu LV3 订阅额度上限 vs watchlist+指令+持仓峰值（~70-80 只）。
- [x] **（P-2a 新增衍生项）主线 scan_loop 扫描本体静默化**（用户裁决，**2026-07-24 当日上线**）：外科手术式——只停 `_run_signal_scan_impl` 扫描本体（`strategy.main_scan_enabled=false`，`signal_pipeline.py` 扫描调用处加配置门）；**寄宿任务全部保留**（盘前/盘后巡检、日周月度复盘、持仓快照、watchdog 心跳——它们寄宿在同一循环内，整线程停掉会误伤，审计时发现的关键约束）。服务 16:25 CST 重启，PRE 验收通过（三门/topup/退出快循环全在线）。今晚验证点：盘中日志不再出现"[扫描] 开始信号扫描"。回滚：改 true + 重启。
- [ ] 验收判据（周一盘前/盘中）：订阅数≈指令+持仓+基准；vol_gate/vwap 数据缺失率下降；无隐性消费者告警；CPU/配额下降可观测

### 7.5 P-2a 定性结果（2026-07-24 当日完成，只读）

| 候选消费者 | 定性 | 依据 |
|---|---|---|
| position_exit_monitor | **假阳性** | 命中的 "push" 是飞书 PushHub 非行情 push |
| degradation_monitor | **假阳性** | 同上 |
| playbook 量能门 K_1M 兜底 | **不受影响** | 指令标的今天本就不在订阅内，已靠快照主路径运行；退订 watchlist 不改变现状 |
| order_executor | **休眠** | 近 4 日成交 100% PLAYBOOK_SIM（主线 0 笔） |
| signal_pipeline scan_loop | **唯一真实消费者，但产出为零** | 仍每 5min 运行（overnight 静默，TRADING 扫 watchlist），但 4 日零交易——它就是用户所述"已不使用的主线引擎"本体 |

**结论**：静态订阅的唯一真实依赖方恰是已弃用的主线扫描循环自身 → P-2 风险比预期低，周末实施无阻塞项；唯一新增决策点是 scan_loop 是否随 P-2 一并静默（可配置）。

### 第 4 轮讨论记录

- 作者（Qoder，2026-07-24）：用户批准 P-1/P-2 立项。前置发现：并集式合一会把低波防御股塞回补量池（改用角色标签单真源）；隐性消费者初筛 5 处待定性。今晚冻结，周末实施，周一验收。
