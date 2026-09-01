# DEB-20260901b: DA 思考预算影子实验 — qwen3.7-max enable_thinking 对分析质量的影响（提请裁决）

- **立案日**: 2026-09-01（2026-09-01 PE 审阅执行批衍生；用户令立正式提案"需要"）
- **裁决**: 2026-09-01 用户终裁**选项 3 变体——直接切生产**（原提案自评否决项，用户裁量覆盖；
  裁决理由:"从修尺子提升质量的角度"——以生产 go-forward 数据为证据, 配套观测与回滚开关,
  不再先跑影子）。
- **状态**: 已执行（2026-09-01 夜生效, skill v0.74 / 提交见仓外 a08251f→df 后续批）
- **纪律引用**: 影子先行（C4）；预注册单假设（KR3）；一次一个变量；防散落（裁决后注册 shadow_registry + 判据字典）；退出参数冻结不触碰
- **关联**: 2026-09-01 PE 审阅执行批（handoff 2026-09-01g）、DEB-20260831-da-debate-quality（挑战者质量，不同议题勿混）、DEB-20260901（同日，结构化入场语义）

---

## 一、问题定义

**DA 是全链最贵的推理任务，但推理模型的"思考"被关着。**

1. 生产现状：DA 分析师 = qwen3.7-max（DashScope 推理模型），`llm_synth._call_llm` 硬编码 `enable_thinking: False`（v0.70 时代沿用——当时网关侧实证推理模型开思考在小 max_tokens 投票场景造成 empty_response，故全局关闭）。
2. 校准锚（SYSTEM_PROMPT 自带）：DA 方向判断历史命中率约 50%（≈掷硬币，n=321 干净窗口）。
3. 任务性质：DA 要求三层因果链、独有事件挖掘、盘前异动归因、多空对等论证——是全链认知深度最高的调用，正是推理模式的设计场景。
4. 成本背景（本批审计实锤）：DA 占全链 LLM 成本 96%（8/31 实测 $2.97/$3.10），26 次分析师调用/日。若思考能提升方向质量，值得付思考 token 溢价；若不能，用数据关掉念想。

**诚实声明**: 本仓无"开思考提升 DA 质量"的直接证据，只有模型能力理论 + 任务适配推理。这正是需要影子实验而非直接切换的原因。

## 二、机制分析

| 维度 | 关思考（现状） | 开思考（实验） |
|---|---|---|
| 延迟 | 分析师 ~20-40s/候选（全轮 26 候选 20-30min） | 预估 2-4x（30-120s/候选），全轮可能 40-90min |
| 输出 | completion p50=2016 tok（max_tokens=3000 内） | 推理 token 计入输出预算 → 需抬 max_tokens 或设 thinking_budget |
| 成本 | $0.114/候选（8/31 口径） | +500~2000 推理 tok/候选 → 预估 +$0.3~1.5/日 |
| 已知坑 | — | 网关 7/6 P0 实证: 推理预算吃光 max_tokens → empty_response（必须配套抬预算） |

**窗口约束**: DA 07:00 ET 发车，桥接 07:19-07:30 ET 就绪（8/26-8/31 四日实测）。下游 UE 21:10 CST 独立、执行准备在开盘前——影子期零影响；若未来转正，全轮 ≤90min 是硬约束。

**基建先例**: `dr_debate` 已用 `extra_body.enable_thinking: true` 走通网关链路（unified_config.yaml:387）；DashScope qwen 系 thinking 参数生产可达。

## 三、方案设计（影子先行）

### 架构（仿 da_glm_shadow.py 纯旁路，零生产触碰）

1. **输入同构**: 读当晚生产落盘的 `da_checkpoint/{date}/{symbol}.raw.json`（da_raw_inputs/v1，含完整 collected+注入上下文）→ 重组与生产逐字节相同的 user_prompt。**生料落盘（今日落地）使本实验首次可做到输入严格同构**——此前影子实验只能重采集（数据会漂移）。
2. **实验臂**: 同一 raw 输入 → 分析师调用开 `enable_thinking=True` + `thinking_budget=2048`（若端点不支持预算参数则裸开）+ `max_tokens` 3000→6000（容纳推理+输出，防 empty_response 坑）。system/温度/其余参数与生产逐字节一致（单一变量=思考开关）。
3. **落盘**: `data/shadow/da_thinking_shadow.jsonl`（run_mode 区分 live/test；字段: 方向/评分档/论点摘要/推理token/延迟/finish_reason/与生产臂差异标记）。
4. **评分**: T+1 复用 da_glm_shadow 同款 join score results 口径。
5. **挂载**: 一次性实现 + 常驻 cron（20:30 CST，生产 DA 完成后），裁决前不进 shadow_registry 以外任何消费链。

### 实现注记（裁决后执行）
- `_call_llm` 增 `extra_body: Optional[dict] = None` 参数（默认 None = 与现行为逐字节一致），合并进 payload；影子脚本传 `{"enable_thinking": True, "thinking_budget": 2048}`。
- 成本: ~¥0.9/候选 × 15 候选均值 × 10 交易日 ≈ ¥135/窗口（上界）。

## 四、预注册判据（10 交易日窗口，攒满即裁，禁止中途改判据）

| # | 判据 | 阈值 | 不过的处置 |
|---|---|---|---|
| ① | JSON 格式成功率 | ≥95% | 思考破坏格式 → 结案归档 |
| ② | 单候选延迟 p90 | ≤120s | 超窗 → 结案或降 thinking_budget 重立（新 DEB） |
| ③ | 方向质量: 配对 T+1 alpha（思考臂 − 生产臂） | n≥20 对, 均值 >+0.15% 或方向翻转净改善 | 无改善 → 结案归档（关念想也是成果） |
| ④ | 单候选成本增量 | ≤+$0.08 | 超标 → 即使③过也转"成本效益再议"不直接转正 |

**转正路径（全过才谈）**: 另立执行 DEB，生产 `_call_llm` 接线 + 开关 + 基线切分锚点变更预案。**任一不过 → 结案归档，影子脚本按回滚条删。**

## 五、可选方案

- **选项 1（推荐）**: 批准影子实验，按 §三 实施，按 §四 裁决。
- **选项 2**: 维持现状不实验（理由: 成本敏感/窗口敏感）。
- **选项 3**: 直接生产切换思考模式——**提议方自评否决**: 违反影子先行（C4），且空转风险（延迟/格式/成本三未知）无兜底。

## 六、诚实边界与风险

1. 影子臂消耗真实 token（¥135 窗口上界），无交易暴露。
2. 思考臂的论点质量评估只量化"方向+T+1 alpha"，论证深度等软指标不在判据内（避免主观裁决）。
3. 若生产 DA 在窗口期内因其他原因改 prompt（如蒸馏 DEB 并行），配对判据受污染 → **与 DEB-20260901c（蒸馏）互斥，不得同窗运行**（见该档 §五）。
4. thinking_budget 参数在 DashScope compatible-mode 的 qwen3.7-max 上未实测——实施第一步先单发冒烟验证参数可达性，不支持则裸开 enable_thinking 并在判据②下重估延迟。

## 七、回滚

- 影子期: 删 cron 一行 + 删 `scripts/da_thinking_shadow.py`（kill switch 唯一性同 da_glm_shadow 先例）。
- 生产零触碰，无其他回滚面。

---

## 八、执行记录（2026-09-01，用户裁"直接切生产"）

**实施（仓外 skill，备份 `*.bak-thinking-20260901`）**:
1. `_call_llm` 增 `extra_body` 透传参数（默认 None = 逐字节原行为）。
2. 分析师调用开 `enable_thinking=True` + `thinking_budget=2048`，`max_tokens` 3000→6000（防推理 token 吃预算 → empty_response，网关 7/6 P0 教训）。
3. 观测落盘: `_llm_latency_ms` / `_thinking_enabled` 随 `_llm_output` 进 checkpoint → 每日日报全链路口径可核（延迟/成本/截断三指标）。
4. 开关: `config.py DA_THINKING_ENABLED`（默认开；`.env.us` 设 `DA_THINKING_ENABLED=0` 即回滚逐字节原行为）+ `DA_THINKING_BUDGET`。
5. 冒烟实弹: `thinking_budget` 参数 DashScope 可达 + json_mode 共存 + `finish=stop`；空壳候选端到端（DeepAnalyzer+思考）62.7s/候选, SKIP/LOW 路由正常。

**原预注册判据的命运（诚实留痕）**: §四四判据随直切生产不再作为"转正门"，转为 **go-forward 观测指标**——明日日报起逐日核: ①JSON 成功率（`_finish_reason` 分布）②延迟（`_llm_latency_ms` p90，预估全轮 35-45min，桥接惰性加载无硬截止）③方向质量以 DEB-20260901c 影子 V1 臂数据为对照间接度量 + 常规 IC/alpha 审计按 `_thinking_enabled` 切分 ④成本（日报 DA 直连段）。任一指标恶化 → 回滚开关即复。

**与 20260901c 的关系**: b 直切后不再是实验变量，c 影子双臂同思考态并跑，互斥条款自然解除（留痕于 c 档附一）。
