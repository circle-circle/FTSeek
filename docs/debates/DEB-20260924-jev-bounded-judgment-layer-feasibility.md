# DEB-20260924: Jev（TypeSafe System One）作为 FTSeek「有界判断部件」— 可行性与 Phase 0 规格

> **状态**: 提案，**未落码**（本文 = L5 纯文档，零行为变更；Phase 0 若获裁，另立实现批次）
> **待裁**: 用户（§10 的 F1–F5，每条附推荐 + 保守替代）
> **类型**: 立项提案（若 Phase 0 落码则为 L1；本文本身零代码）
> **目标读者分层**:
> - **裁决者（用户）**: 读 §0 结论 / §10 待裁五问
> - **执行者（下一会话的本人）**: 读 §5 规格 / §8 步骤与回滚
> - **复查者（第二 agent）**: 读 §3 证据表 / §9 复查清单 / §11 自曝，按 §9 逐条跑命令
> **前置**: 承接 2026-09-21 的 Jev 一手资料核查轮；仓内 `grep -rli "typesafe\|\bjev\b" app/ scripts/ docs/ config/` = **0 命中** ⇒ 无既有实现、无既有裁决冲突（§3 E1）
> **复核日**: 2026-10-21（与注册表现有 `recheck_after` 同日，便于一次裁完）

---

## 0. 一句话目标 + 可判定子目标

**目标**：决定要不要把「有界语义判断」从生成式 LLM 里拆出来（交给 Jev 一类决策模型，或本地等价物），以及**先在哪一个判断上做零风险验证**。

| # | 子目标（可判真假） | 判定方式 |
|---|---|---|
| G1 | 厘清 FTSeek 的"判断面"缺口究竟是**成本/延迟**还是**裁决带宽** | §3 E4（7 天 LLM 成本 $0.87）vs E3（19 个达标影子未裁） |
| G2 | 给出**零外发、零行为变更、可回放**的 Phase 0 规格 | §5 规格可独立执行；§9 C7/C8 给反例判据 |
| G3 | 厂商声明与第三方实测**分开标注**，不混作证据 | §4 逐行标 `[厂商]` / `[第三方]` / `[未实跑]` |

**本提案的直接结论（供快速裁决）**:
1. Jev 在 FTSeek 的**经济价值 ≈ 0**（我们七天全部 LLM 花销 $0.8675），"便宜 1000×"不构成理由；
2. 唯一站得住的理由是 **70–500ms 可进热路径** + **概率作为显式不确定性口径**（三路分叉：自动 / 升级 / 转人工）；
3. 但**裁决带宽瓶颈（19 个 READY 影子）不是"缺一个裁判"，是缺消费方与结算面**——换更便宜的裁判不解决"没人裁"（§12 反方 §12.1）；
4. 因此推荐：**Phase 0 先做本地可回放基线（零外发、零依赖、当天可跑），用历史标签问"这个判断到底有没有可测信号空间"**；只有基线显示"有空间"，才值得付"数据外发 + 早期厂商依赖"的代价去测 Jev 本体。

---

## 1. 非目标（明确不做）

| ❌ 不做 | 理由 |
|---|---|
| 用 Jev 预测方向 / 收益（当 α） | 官方自己声明 API 能表达交易决策 ≠ 有预测力；第三方扑克实测显示它**不能从原始状态推结论**（须把结论喂进 state）⇒ 当预言机用即瞎猜 |
| 用 Jev 生成 thesis / 叙事 | 它不生成文本；叙事层仍归 DA（glm-5.3） |
| 用 Jev 做算术 / 费用 / SL 线 / 日期 | 官方 jaggedness 文档自曝算术、计数、日期不可靠 ⇒ 沿用既有纪律"触发判定代码算、禁 LLM 估" |
| 任何不经影子 + 真值回填就作用于真金白银的决策 | 承「死影子」先例（§3 E11/E12） |
| 本文改任何代码 / 配置 / cron | 本文是文档；Phase 0 另行立项 |

---

## 2. 问题陈述（含普查实测）

### 2.1 触发

用户问：FTSeek 的瓶颈之一是"既不想要死板的硬阈值，又怕 LLM 是瞎猜"，Jev 是否正好卡在中间。上一轮（9/24）已给出逐阶段映射；本轮要求把它落成可独立复查的 DEB。

### 2.2 普查：缺口到底在哪一层（先证明，不凭印象）

| 层 | 现状（实测） | 结论 |
|---|---|---|
| **成本** | 7 天全部 `is_test=0` LLM 调用 56 次 / **$0.8675**（E4） | 成本不是问题；"便宜 1000×"在绝对额上省不到 $1/周 |
| **延迟** | `agent_bridge` 均 59.5s（max 241s）、`universe_expansion_reasoning` 均 **106.9s**（max 433s） | 真问题：现有 LLM **物理上进不了盘中热路径**（70–500ms 才能） |
| **判断数量** | 候选 70–79 条/日（`playbook_instructions`，E7）、成交 3–16 笔/日（E8） | 体量中等；"给每一行打一个判断"的边际成本≈0 才有意义 |
| **裁决带宽** | 影子注册表 47 项 / 活跃 34 / **样本达标未裁 19**（E3） | **最真实的缺口**——但这缺的是"消费方 + 结算面 + 人的时间"，不是"更便宜的裁判" |
| **不确定性口径** | 现有门全是硬阈值；`min_confidence: 60` 只做**仓位缩放**不做拦截（9/21 实测：conf<60 的 129 行里 57 行仍成交） | 灰区**没有显式出口**：没有"升级人工"这一档 |

### 2.3 被普查推翻的初始设想

初始映射把"新闻/催化剂分类"列为 A 类首选（体量大、昂贵 LLM）。**普查证伪**：
- `news_classifier` 开关现值为 **False**（`app/llm/features/feature_flags.py:24`），消费方 `app/news_v2/processor/llm_classifier.py:140` 因此**未启用**；
- 历史语料几乎为空：`data/news_quality.db::news_classifications` = **37 行**，`data/news_v2/` = **0 文件**（E5）。

⇒ 新闻分类链**既不在跑、也没有可回放的历史标签**，做它既无收益也无验证手段。**撤下首选，改列入"待有语料再议"。**

---

## 3. 数据与实证表（每行作者已实跑，期望列 = 真实输出）

工作目录统一 `/opt/ftseek/FTSeek-US`；解释器 `./venv/bin/python`。

| # | 声称 | 复现命令 | 实测输出（作者实跑值） |
|---|---|---|---|
| E1 | 仓内无 Jev/TypeSafe 既有实现或裁决 | `grep -rli "typesafe\|\bjev\b" app/ scripts/ config/ ops/ ~/.hermes/scripts/ \| head` | 空输出（`docs/` 下只有本提案与其索引行自引用） |
| E2 | 穿透链的生产者/消费者落点 | `grep -rln "penetration_pattern" app/ scripts/ tests/`；`grep -rln "penetration_shadow" app/ scripts/ tests/` | 前者仅 `scripts/penetration_llm_analysis.py`（+同目录 `.pyc`）；后者含写者 `app/playbook/penetration_shadow.py`、注册 `app/playbook/shadow_registry.py`、判据/结算 `scripts/penetration_llm_analysis.py` + `scripts/penetration_outcome_backfill.py`、测试 `tests/test_penetration_shadow.py` |
| E3 | 影子注册表现状（裁决带宽瓶颈） | 见下方代码块 ① | `注册总数=47 活跃=34 样本达标(READY)=19 状态变迁通知=18` |
| E4 | 7 天 LLM 基线（成本/延迟） | 见下方代码块 ② | `56 次 / $0.8675`；`agent_bridge n=36 均59460ms max241389ms $0.1558`；`universe_expansion_reasoning n=19 均106904ms max433330ms $0.7062`；`penetration_pattern_analysis n=1 4075ms`；同期 `is_test=1` = 1028 行 |
| E5 | 新闻链未启用且无语料 | `grep -n "news_classifier" app/llm/features/feature_flags.py`；`./venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/news_quality.db').execute('select count(*) from news_classifications').fetchone())"`；`ls data/news_v2/ \| wc -l` | `24:    "news_classifier": False,`；`(37,)`；`0` |
| E6 | 催化剂分类器路由 | `sed -n '340,342p' config/unified_config.yaml` | `catalyst_classifier:` / `fallback: deepseek` / `primary: qwen` |
| E7 | DA/UE 指令体量 | `./venv/bin/python -c "import sqlite3,collections;c=sqlite3.connect('data/ftseek_us.db');print(sorted(collections.Counter(r[0] for r in c.execute('select date from playbook_instructions')).items())[-8:])"` | `[('2026-09-15',79),('2026-09-16',71),('2026-09-17',72),('2026-09-18',75),('2026-09-21',72),('2026-09-22',68),('2026-09-23',71),('2026-09-24',72)]` |
| E8 | 成交体量（复盘/归因真值来源） | 同上换 `trade_records.trade_date` | `近 8 日 3–16 笔/日`；全表 894 行 |
| E9 | 穿透影子类别失衡（真读数） | 见代码块 ③ | `labeled=90`；`recovered_5m: {True: 88, False: 2}` ⇒ **88:2** |
| E10 | 穿透影子周分析读数 | `cat data/shadow/penetration_analysis_report.md` | `n_labeled: 42`；`v_rate_5m: 0.976`；`hypotheses_scored: []`（LLM 出题为空 ⇒ 不判）；`graduation_gate {n:20,matched:8,precision:0.7}`（源码常量回显） |
| E11 | 「死影子 / 读数虚增」先例 | `sed -n '229p' docs/debates/DEB-20260923-shadow-board-caliber-fixes.md` | `段10 近7天读数由 30min行1316 命中192 回到真实生产样本 30min行19 命中(修口径后)10 已结算9` |
| E12 | 结算面现状（补挂后已工作） | 见代码块 ④ | `early_dead_signal_shadow.jsonl 389 行 settled=381`；`high_atr_stall_shadow.jsonl 292 行（无 settled 字段）` |
| E13 | 现行裁决口径（必须引用，不许另立） | `sed -n '1,20p' docs/ADJUDICATED-CRITERIA.md` | `A1 影子攒样最小样本 n≥30 才评估; n≥60 才谈裁决`；`A7 促进/转正双窗口 双窗口 ≥40% 才采信`；`A8 转正四判据 OOS≥15日 ∧ 净均值>0 ∧ 毛利 t>1 ∧ ADF 平稳` |
| E14 | 新增调度/脚本的入账守卫 | `grep -n "def test_" tests/test_control_loop_ledger.py tests/test_ops_scripts_sync.py` | 台账文件 **10** 条，入账相关 4 条：`test_enabled_job_is_in_ledger` / `test_crontab_loops_are_ledgered_or_baselined` / `test_ledger_names_carrier_script_for_crontab_rows` / `test_crontab_baseline_has_no_ghost_entries`；同步文件 **3** 条（含 `test_repo_and_live_scripts_in_sync` 双向 sha256） |
| E15 | **本提案作者的一次口径事故（自曝）** | 见 §11 | 用 epoch 浮点与**文本时间列**比较，得到"近 7 天 `risk_blocked` = 5223"；实为**全历史** 5223，且最后一条在 `2026-05-22T03:40:54` ⇒ 该数字已作废，不再引用 |

```python
# ① 影子注册表活体读数（只读：refresh_counts 不写盘，仅内存置 current_n）
import sys, importlib; sys.path.insert(0, "/opt/ftseek/FTSeek-US")
m = importlib.import_module("app.playbook.shadow_registry"); m.refresh_counts()
live = [s for s in m.REGISTRY if s.status.value in ("OBSERVING", "READY_TO_EVALUATE")]
print(len(m.REGISTRY), len(live), len([s for s in live if s.current_n >= s.min_n]))

# ② 7 天 LLM 基线（llm_calls.timestamp 是 epoch 浮点，不是文本）
import sqlite3, time
l = sqlite3.connect("data/llm_audit.db"); w = time.time() - 7 * 86400
print(l.execute("select count(*), round(sum(cost_usd),4) from llm_calls where is_test=0 and timestamp>=?", (w,)).fetchone())
for r in l.execute("select feature,count(*),round(avg(latency_ms)),max(latency_ms),round(sum(cost_usd),4) "
                   "from llm_calls where is_test=0 and timestamp>=? group by feature order by 2 desc", (w,)): print(r)

# ③ 穿透影子类别失衡
import json, collections
rows = [json.loads(x) for x in open("data/shadow/penetration_shadow.jsonl", encoding="utf-8") if x.strip()]
lab = [r for r in rows if r.get("status") == "labeled"]
print(len(lab), collections.Counter(bool(r.get("recovered_5m")) for r in lab))

# ④ 结算面现状
for f in ["data/early_dead_signal_shadow.jsonl", "data/high_atr_stall_shadow.jsonl"]:
    rs = [json.loads(x) for x in open(f, encoding="utf-8") if x.strip()]
    print(f, len(rs), collections.Counter(str(r.get("settled")) for r in rs if "settled" in r))
```

---

## 4. Jev 是什么 / 不是什么（来源分层，**含"未实跑"声明）

**声明**：本提案作者**没有实跑过 Jev**（TypeSafe 为 2026-09-15 发布、当前 waitlist/早期接入）。以下每条按来源标注；`[厂商]` 与 `[第三方]` 不得混作证据。

| 事实 | 值 | 来源类型 |
|---|---|---|
| 定位 | "System One" 决策模型：输入 state，输出**类型化决策 + 概率**；不生成文本 | `[厂商]` + 多篇 `[第三方]` 一致 |
| 三原语 | `Choice`（N 选 1，选项上限 255）/ `Score`（按 rubric 打分）/ `Noul`（P(是)，0–1） | `[厂商]` docs 一致 |
| 延迟 | 端到端 70–500ms | `[厂商]`；`[第三方]` 评论"与其并行采样设计一致，但测自其自有区域" |
| 价格 | 输入 $0.042/M tok（=$42/B），**输出免费** ⇒ 约 $0.0004/次 | `[厂商]` 公布价 |
| 加速/降本声明 | 快 20–200×、便宜 40–400×（部分二手文转述为"1000×"） | `[厂商]`；转述有放大，**采信区间取 40–400×** |
| 准确率 | 67.8%（对比 GPT-5.6 Terra 67.9%） | `[厂商]` |
| 第三方实测（中文客服 50 题） | **64–65%**；便宜小模型组第 2、强模型组垫底；最快 0.73s/题；阈值边缘抖动（1.99 vs 2.00 翻判据） | `[第三方]` |
| 第三方实测（扑克 vs 求解器） | 与最优解吻合 **63%**；**置信度倒挂**（错得最狠处 0.86）；**必须把结论喂进 state 才能判对** | `[第三方]` |
| 第三方实测（agent 轨迹分类） | 79%（19/24） | `[第三方]` |
| 官方自曝弱点 | 算术/计数/日期不可靠、多跳推理弱、无关上下文掉准、易被注入 | `[厂商]` jaggedness doc |
| 复刻易度 | 发布 2 天内 ≥6 个开源复刻；社区判断为"BERT 类编码器 + 校准奖励 RL" | `[第三方]` |
| **注意（与上一轮口述的一处出入）** | `Noul` **没有独立 confidence 字段**（`Choice`/`Score` 才有）；Noul 的 0–1 输出本身即概率 | `[第三方]` 集成说明 |

**尖点**：它的护城河是 **速度 / 成本 / 类型安全 / 校准声明**，**不是智力**（准确率≈廉价小模型）。校准是**群体属性**，厂商亦自认"单条 0.8 不保证对" ⇒ **必须在我们自己的标签上验**。

---

## 5. 方案设计（Phase 0 规格）

### 5.1 判什么（三个原子问题，禁多因子）

| 编号 | 原语 | 原子问题（一问一件） | 输入（最小充分集） | 真值（可回填） | 现有规则结果（对照臂） |
|---|---|---|---|---|---|
| **Q1** | Noul | "这笔候选**入场后是否净亏**（含费）？" | 方向 / 计划入场价 / 现价 / 当日涨跌 / ATR% / regime / 时段 | `trade_records.pnl_net`（894 行，E8） | 现役门链实际判定（放行/拦） |
| **Q2** | Noul | "该持仓**此刻继续持有是否比收口更差**？" | 当前浮盈 / MFE / 持仓分钟 / 距 SL/TP / 时段 | 用离场后 1 分 bar 回放（沿用 `penetration_outcome_backfill.py` 同法） | 退出引擎实际动作 |
| **Q3** | Choice | "这条告警属于 **良性噪声 / 真故障 / 待观察**？" | 告警指纹 / 类别 / 近 N 次同指纹 / 是否触发业务动作 | `issue_registry` 的持久化标记（同日/次日是否再发、是否被 `mark_resolved`） | 现役指纹+冷却规则 |

**为什么是这三个**：Q1 体量与真值都最硬（候选 70+/日、`pnl_net` 权威）；Q2 有现成的 backfill 管线可照抄；Q3 有 `issue_registry` 既有指纹去重可作对照臂（§3 E14 之外的 `app/core/issue_registry.py::is_duplicate`）。
**为什么不是"影子裁决"**：见 §12.1——元环裁决**没有客观真值**，它属"确定性地把证据整理成表"，不该走概率判断（这是我上一轮映射里的错项，见 §11）。

### 5.2 落盘与消费（纪律：一个写者 + 一个消费者 + 一个真值回填器）

- 产物：`data/shadow/jev_judgment_shadow.jsonl`（追加，一条 = 一次判断）
  ```json
  {"ts":"2026-10-01T13:31:02","et_date":"2026-10-01","q":"Q1","subject_id":"<decision_id|trade_id>",
   "state_hash":"<sha1(最小充分集)>","answer":0/1,"p":0.73,"confidence":null,
   "rule_result":"PASS|BLOCK","truth":null,"settled":false,"model":"local-baseline|jev|llm"}
  ```
- 采样口径：**每条候选落一行 + 每条成交落一行**（不许只落"命中阈值"的行——否则无对照臂、判据永不可裁，承 RFC-084 教训）
- `count_mode`：该影子必须用 `distinct:<subject_id>`（防逐轮落行虚增 `current_n`）
- 真值回填：settler 与**同一批**入 `shadow_registry` + 台账（承 E11/E12 死影子教训）
- **仓库不新增第二套台账**：注册进既有 `app/playbook/shadow_registry.py`（唯一 SSOT），不另建清单文件

### 5.3 预注册判据（**先于读数写死**，承 RFC-104 先例）

| 判据 | 门限 | 出处 |
|---|---|---|
| 样本 | `n≥30` 才评估；`n≥60` 才谈裁决 | 判据字典 A1 |
| 窗口 | 双窗口（≥2 段历史期）同向 | A7（双窗口 ≥40% 才采信） |
| 判别力 | 命中组 vs 未命中组**净均差 lift ≥ 1.3×**，且**两窗同向** | 承 RFC-104 词语表漂移教训：**绝对率会随基率漂移，只认 lift** |
| 校准 | 分箱 reliability：`p∈[0.5,0.6)` 组实测频率落在 `[0.4,0.7)`；`p≥0.8` 组实测 ≥ `0.65` | 无先例 → **本提案首次预注册，需用户裁**（F3） |
| 误杀代价 | 若用于拦截：被拦组的净均**不得优于**放行组（否则门在筛更差的单） | A4（统计显著 ≠ 可上门禁，须算误杀代价） |
| 判决方向 | 方向性结论**由本窗实数推导**，禁在渲染里写死结论 | DEB-20260923 格式化硬结论事故 |

**未过门 = 写"证据不足"，不写"负期望"**（判据字典纪律）。

---

## 6. 方案对比（含"不做"）

| 方案 | 内容 | 赚什么 | 代价/风险 | 结论 |
|---|---|---|---|---|
| **A. 云 API 直连 Jev** | 用 TypeSafe/OpenRouter 走 3 个原子问题 | 立刻可测真机；70–500ms；类型安全 | **数据外发**（交易状态/候选外发第三方）；waitlist/无 SLA；早期厂商；中文弱 | **仅在 B 显示"有信号空间"后开** |
| **B. 本地可回放基线（零外发）** | 用**已有历史标签 + 传统分类器/规则集成**（不用 Jev）回答 Q1–Q3，算 lift/AUC/校准 | 当天可跑、零成本、零外发、可复跑；**先建立"这个判断到底有没有可测信号"的下限** | 不是 Jev 本体，测不到 Jev 的独有优势 | ✅ **推荐先做（Phase 0）** |
| **C. 本地自训校准头** | BERT 类编码器 / 梯度提升 + 校准，用 FTSeek 自己的标签训 | 无外发、无厂商依赖、可在自家数据上校准、无中文弱点 | 要标注+训练周期；当前标签量（894 笔）偏小 | 长期最优，**Phase 1 之后再议** |
| **D. 不做** | 维持硬阈值 + 人肉裁决 | 零成本 | "灰区无出口""裁决带宽"两个缺口原样保留 | 若 F1 选"不做"，须接受此后果并写进文档 |

**推荐路径**：`B →（有信号空间）→ A 小样对照 →（A 与 B 无差异且成本/延迟有真收益）→ C`。

---

## 7. 决策依据最小充分集（8 项，缺哪项就不得上产）

| # | 要求 | 本提案状态 |
|---|---|---|
| ① 固定样本来源 | ✅ 可复跑：`trade_records` / `penetration_shadow.jsonl` / `issue_registry` 均为固定窗口文件 |
| ② 多窗口 ≥2 段 | ✅ 规格已写（§5.3），实测待 Phase 0 |
| ③ 配对/对照臂（含更简替代） | ✅ 现役门结果作对照臂 + §6 方案 D 作更简替代 |
| ④ 尾部（最差 5% / 单笔集中度） | ⬜ Phase 0 必出：净均差之外必须给最差 5% 与单笔最大贡献 |
| ⑤ 费用后口径（净而非毛） | ✅ 用 `pnl_net` |
| ⑥ 机制诊断 | ⬜ 待定：需给出"为什么这组更差"的可解释量（如 MFE 分布 / 时段集中） |
| ⑦ 功效/显著性声明（n 与 MDE/t） | ⬜ **当前样本 894 笔、真值子集更小 ⇒ 大概率"证据不足"；须先算 MDE 再决定 n 目标**（不许用算术自洽的错 σ） |
| ⑧ 预注册判据 + 回滚 + 复现命令 | ✅ §5.3 / §8 |

⇒ **凑不齐 ⑦ ⇒ 只能作为"假设待攒样"进影子，不得上产**（本提案已如实标注）。

---

## 8. 实施步骤（每步独立可回滚；Phase 0 全程零交易行为变更）

| 阶段 | 动作 | 验证命令与期望 | 回滚 |
|---|---|---|---|
| **P0-a**（只读） | 复算 Q1 的历史 lift/AUC/校准（**不落新文件，先出一次性报告**） | `./venv/bin/python scripts/jev_q1_baseline.py --dry-run` → 打印 `n= / lift= / 分箱表`；若 `n<30` 直接停 | 无副作用（只读） |
| **P0-b** | 注册影子到 `shadow_registry.py`（`count_mode="distinct:subject_id"`、`min_n=60`、`graduation_criteria` 抄 §5.3 原文） | `./venv/bin/python -c "import app.playbook.shadow_registry as m;print([ (s.id,s.count_mode,s.min_n) for s in m.REGISTRY if 'jev' in s.id])"` | 删注册项 |
| **P0-c** | 采集器 + settler 同批落地；**同批入** `docs/CONTROL-LOOP-LEDGER.md` + 脚本回库 `ops/hermes/` | `./venv/bin/python -m pytest tests/test_control_loop_ledger.py tests/test_ops_scripts_sync.py -q` → 全绿 | 摘 cron + 删台账行 + `collection_retired=True` |
| **P0-d**（可选，需 F4 批） | 同一批 state 同时打 Jev 云 API（`model="jev"`）与本地基线（`model="local-baseline"`） | 同文件出现两种 `model` 值；对比 lift/一致率/延迟 | 关 config 开关（`llm_features.jev_shadow.enabled=false`） |

**回滚两层**：
- **法子**：删 `shadow_registry` 注册项 → 摘 cron → 删台账行 → `collection_retired=True`（数据一律保留不删）
- **触发条件（出现即回滚，不留给下次讨论）**：
 1. 出现任何**由该影子产物而非事实闸**导致的下单（生产日志/DB 可见）；
 2. `已结算 M/N` 中 `M=0` 连续 **2 个交易日**（结算面停摆 = 假活跃）；
 3. 影子文件被**非采集路径**写入（判据：`ts` 值域出现常量或 9 位小数合成价，承 9/21 污染事故）；
 4. 判断被用于**自动执行**（而非只记录）且未经用户裁。

---

## 9. 复查清单（请第二 agent 逐条挑战；"反例判据"列 = 什么输出说明我错）

| # | 声称 | 复查命令 | 反例判据 |
|---|---|---|---|
| C1 | 仓内无 Jev/TypeSafe 既有实现 | `grep -rli "typesafe\|\bjev\b" app/ scripts/ config/ ops/ ~/.hermes/scripts/` | 出现任何命中 ⇒ 声称不成立。注：命令**刻意不含 `docs/`**（`docs/` 下会命中本提案与其 README 索引行这两个自引用文件，实测只有这两个） |
| C2 | 7 天 LLM 成本 $0.8675、56 次 | §3 代码块 ② | 数字偏差 >5% ⇒ 我口径错（注意 `is_test` 过滤） |
| C3 | `news_classifier` 未启用 | `grep -rn "is_enabled(\"news_classifier\")" app/ -A2`；`grep -n "news_classifier" app/llm/features/feature_flags.py` | 任一消费点实际在跑（日志/DB 有别处产出）⇒ 声称不成立 |
| C4 | 新闻语料无可回放量 | E5 命令 | `news_classifications` 或 `data/news_v2/` 出现 ≥1000 行/文件 ⇒ 我漏了采集面 |
| C5 | 19 个影子样本达标未裁 | §3 代码块 ① | 数目 ≠19 ⇒ 我口径错；**并请复核 `min_n` 与判据 n 是否同轴**（不同轴 = 我误读） |
| C6 | 穿透失衡 88:2（非 84:2） | §3 代码块 ③ | `False` 计数明显 ≠2 ⇒ 我读错样本（注意只算 `status=labeled`） |
| C7 | Phase 0 规格零行为变更 | 读 §8：全部动作只写 `data/shadow/` 与注册表 | 规格里出现任何写生产 DB / 改 config 生效值 / 下单路径 ⇒ 声称不成立 |
| C8 | 元环裁决"无客观真值" | 读 `app/playbook/shadow_registry.py` 的 `graduation_criteria` 与 `eval_script` 产物 | 若存在可程序化回填的客观真值字段 ⇒ 我的判断过强 |
| C9 | 我的口径事故（§11 E15）确实作废 | `./venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/ftseek_us.db');print(c.execute(\"select count(*),max(timestamp) from events where event_type='risk_blocked'\").fetchone())"` | 若真存在近 7 天大量 `risk_blocked` ⇒ 我的"作废"结论过头 |
| C10 | §4 的 `[厂商]/[第三方]` 标注与来源一致 | 抽查 §4 任一行，用其来源类型复核原始出处 | 出现"厂商数字当第三方实测"或反之 ⇒ 标注体系失效 |

**点名请复查者挑战这 3 点**（不点名 = 复查退化为确认作者叙事）：
1. **§12.1 的"换裁判不解决没人裁"**——如果我说错，Phase 0 的顺序就要反转（先上 Jev，先做元环）；
2. **§5.3 的 lift 口径与校准分箱门限**——这是我首次预注册，可能把一个行业惯例写成了过严/过松的门；
3. **§6 推荐路径 B→A→C**——是否存在更省的路（例如直接用现役 `qwen-flash` 打同一批 state 做对照，而不必引入任何新模型）。

---

## 10. 待裁问题（frontier，逐条给推荐 + 保守替代）

| # | 问题 | 推荐 | 一行理由 | 保守替代 |
|---|---|---|---|---|
| **F1** | Phase 0 载体选 **B（本地回放基线）** 还是直接 **A（云 API）**？ | **B 先做** | 零外发/零依赖，当天可跑；若连历史回放都无 lift，则 A 不必测 | 若用户希望直接看真机：A 的 Q1 单点小样（n=200，成本 ≈$0.08） |
| **F2** | 首个打点选 **Q1/Q2/Q3** 中的哪个？ | **Q1**（入场后是否净亏） | 体量 70+/日、真值 `pnl_net` 权威、现役门链天然作对照臂 | Q3（告警噪声，零交易风险，但真值弱） |
| **F3** | §5.3 的**校准门限**是否照此预注册？ | 照此预注册 | 校准是本模式唯一卖点，必须自己验，不许采信厂商 | 先只记录不判定（`[只印不判]`），攒够 60 再定门 |
| **F4** | 是否允许**数据外发**给第三方 API？ | **先不许**；仅 B 通过后按最小充分集字段外发 | 交易状态外发涉合规/ToS；先外发字段白名单 | 全程不外发（走 C：本地自训） |
| **F5** | 影子裁决带宽（19 READY）怎么办？ | **用确定性代码出"证据表"**，不引 Jev | 元环无客观真值，判断形状是"整理"不是"概率" | 人肉逐条读（维持现状） |

---

## 11. 自曝 / 更正记录（本提案推翻或不再引用）

| # | 我此前的说法 | 实测/更正 | 处置 |
|---|---|---|---|
| ① | "穿透判别 84:2 失衡" | 实测 `labeled=90`，`recovered_5m` = **88:2**；9/12 周报 `v_rate_5m=0.976`（n=42） | 全文改用 88:2；**此前的 84:2 不再引用** |
| ② | "新闻/催化剂分类 = 昂贵 LLM，体量大"（列为 A 类首选） | `news_classifier` 开关 **False**；语料 `news_classifications` = **37 行**、`data/news_v2/` = 0 文件 | **撤下首选**，改列"待有语料再议"（§2.3） |
| ③ | "近 7 天 `risk_blocked` 5223 条（高体量判断面）" | 5223 是**全历史**计数，最后一条 `2026-05-22T03:40:54` | 该数字**作废**；已改用字符串窗口重算（E15）。**根因**：`events.timestamp` 是 ISO **文本**列，我用 epoch 浮点比较 ⇒ 静默全表命中。教训：**报数前先验列类型与格式**（与"SQLite 时区三格式混存/字符串 date 比较虚增"同族） |
| ④ | "Jev 比 LLM 贵 ~1000×"、"Noul + 概率 + 校准置信度" | 厂商声明是 **40–400×**（二手转述才有 1000×）；`Noul` **没有独立 confidence 字段**（Choice/Score 才有） | §4 已按来源分层更正；采信区间取 40–400× |
| ⑤ | 上一轮映射把"影子裁决（元环）"列为 **✅✅ 最匹配** | 元环**缺客观真值回填**，不满足"每判断必配真值"纪律；其判断形状是"整理证据"而非"概率判断" | 降级为 F5：用确定性代码做，不引 Jev（§12.1） |
| ⑥ | "Jev 便宜 1000× ⇒ 值得引入"（隐含的成本理由） | 7 天全部 LLM 花销 **$0.8675** | 成本理由**删除**；改以延迟（70–500ms 进热路径）+ 显式不确定性为唯一理由（§0） |

---

## 12. 反方视角（自我对抗，三条必答）

### 12.1 这个改动会不会多出单 / 多赚钱？

**不会。** 已实测：因子 verdict 全负、毛期望 ≈0.2bps vs 费用地板 ~10bps。判定层换更快的裁判**不产生 edge**，只改变成本/延迟/不确定性口径。
**收益必须如实写成**："enables X"（显式灰区出口、可加判断数量、热路径可用）——**不许写 "improves WR"**。
**更要命的一击**：19 个达标影子未裁，缺的不是"裁判"而是"消费方 + 结算面 + 人的时间"。**把裁判换便宜，裁决欠账一条不会少** ⇒ 这是本提案最可能的失败点，也是 F5 独立于 Jev 单独裁决的理由。

### 12.2 新增的失败模式是什么？

| 新失败模式 | 机制 | 防线 |
|---|---|---|
| **概率被当硬阈值用** | 把 `p>0.8` 直接当门 → 用"有权威感的错误信心"替代"一眼可见的硬阈值"，比原来更危险 | §5.3 判据先验校准；上产前必须过误杀代价关（A4） |
| **又一个无结算影子** | 建了产物、settler 没挂 cron ⇒ 天天刷行、永远裁不了（E11/E12 前车之鉴） | §8 P0-c 同批入账 + 回滚触发条件 2 |
| **数据外发 / 厂商绑定** | 交易状态外发第三方；早期厂商无 SLA、waitlist | F4 默认不外发；路径 B/C 不依赖厂商 |
| **输入错配（喂太多）** | 官方自曝"无关上下文掉准确率" | 最小充分集（§5.1 输入列）+ `state_hash` 可回放 |
| **算术/日期被当判断** | 官方自曝算术/计数/日期不可靠 | §1 非目标已列为禁入 |

### 12.3 与已回滚 / 已证伪方案的关系（防重走）

| 既有结论 | 与本提案关系 |
|---|---|
| 三模型投票已废；`debate_engine/` 从未落地 | 本提案**不复活多模型辩论**；不加投票层 |
| 因子权重 verdict 全负；`premarket_invalidation` 仍 record-only | 本提案**不声称能救胜率**；不碰方向 |
| "缓存器造完实测收益 0 又回滚" | 本提案的 Phase 0 **先出读数再决定建不建**，不为设施而设施 |
| RFC-092 影子清账批（已执行，19 READY 收敛） | 本提案**不重裁** RFC-092；只补"待裁队列的消费者形态"一节（F5） |
| RFC-104（判据先于读数写死）/ DEB-20260923（死影子、污染、词语表漂移） | 本提案 §5.2/§5.3/§8 直接继承其纪律，不新立口径 |

---

## 13. 讨论记录

### 第 1 轮（2026-09-24，用户 + Hermes）

- **用户**: 要求把 Jev 逐阶段映射落成 DEB 文件，供独立复查后再动手。
- **Hermes（本提案）**: 落码前先做普查；普查推翻初始映射的三处（新闻链首选、84:2 读数、元环最匹配），并把"成本理由"整条删除（7 天 LLM 花销 $0.8675）。给出 F1–F5 待裁，推荐 B（本地回放基线）先行。
- **待补**: 用户裁决 F1–F5；如选 Phase 0 落码，另立实现批次并按 §8 分步提交。

---

## 附: 与既有文件的关系（防第二套账本）

| 既有文件 | 它覆盖什么 | 本提案**不重裁**什么 | 本提案依赖它哪一段 |
|---|---|---|---|
| `docs/ADJUDICATED-CRITERIA.md` | 已终裁判据字典（A1–A9/B*/C*/D*） | 不改任何既有判据；新增门限须用户裁（F3） | A1/A4/A7/A8 |
| `docs/decisions/RFC-092-*.md` | 影子注册表清账（19 READY 状态收敛） | 不重裁其收敛结论 | §2.2 裁决带宽现状；F5 |
| `docs/debates/DEB-20260923-shadow-board-caliber-fixes.md` | 影子看板口径纠偏、结算环补挂、污染隔离 | 不重裁其口径结论 | E11/E12、§5.3 判决方向纪律 |
| `docs/decisions/RFC-104-*.md` | ETH 微观结构三影子（判据先于读数） | 不重裁其判据 | §5.3 "判据先于读数写死" |
| `docs/PROMPT-CLAIM-REGISTRY.md` | Prompt 断言 ↔ 真实代码消费方登记表 | 不重裁 | §12.1：该表即"新闻链无消费方"同类问题的既有人工机制 |
| `docs/CONTROL-LOOP-LEDGER.md` + `tests/test_control_loop_ledger.py` | 调度载体入账 SSOT | 不新增第二套台账 | §8 P0-c 入账步骤 |