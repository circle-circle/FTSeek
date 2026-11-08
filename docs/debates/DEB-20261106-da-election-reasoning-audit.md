# DEB-20261106：DA 双 LLM 对美国中期选举后板块分化的捕捉与指令审计

## 状态

- 日期：2026-11-06 07:40 CST
- 状态：**只读复核完成；生产零变更，后续改进待用户裁决**
- 核心批次：2026-11-05 08:03–08:35 ET（11 月 4 日首个选后完整收盘后的 DA 盘前批）
- 范围：原始材料 → 主模型 → 挑战者/合并 → signal gate → bridge → 盘前报告及盘后结果
- 一句话结论：**信息被部分捕捉，个别标的形成了正确的宏观因果链；但整体未形成稳定的“共享事件 → 板块传导 → 双模型裁决 → 合理标的指令”闭环，最终避险有相当部分来自规则门而非双 LLM 自身。**
- 第二复核补注：§1 只校准 **11-04** 的选后首日结构分化，不能用来否认 **11-05** 的盘中普跌；两日口径及 DA 模型归因复核见 §9 与 `DEB-20261106-market-risk-switch-review.md` §14。

## 立案前强制检查

- 同轴历史：`/root/.hermes/skills/deep-stock-analysis/references/capability-boundary-cross-asset-reasoning-20260714.md` 已指出 per-stock、无共享宏观事件层的边界；当前代码已增加真实板块上下文，因此不能照搬旧文档“完全没有板块数据”的结论。
- 现行约束：`docs/OKR-2026-11.md` 要求不以“加深 DA/UE 分析质量”直接降死单、不新增未注册影子、行为变更一次一变量。本 DEB 不改 prompt、模型、门禁、候选预算、仓位或执行链。
- 工作区保护：`app/`、`scripts/`、`handoff.md`、索引等存在其他 agent 在途改动；本批只新增本 DEB，并在既有索引/handoff 顶部追加本事项，不覆盖或部署他人 WIP。
- 本文是单日链路审计，不是收益显著性检验；不用一日结果宣称宏观推理有 edge，也不据此新增 live 硬门。

## 1. 先校准市场前提

用户所述“昨日美大选后，大部分美股大跌”不符合本批 DA 自己收到的材料。`data/da_checkpoint/2026-11-05/US.AMZN.raw.json:1` 的社区材料写明 11 月 4 日：

- 标普 500 **+1.56%**、纳斯达克综合指数 **+1.44%**、道指 **+3.03%**；
- 美债收益率回落、美元走弱、原油大跌；
- 真正的重灾区是 **AI 硬件、存储、光通信、独立算力云/数据中心链**，软件、终端和现金流稳健的云平台相对受益。

`data/da_checkpoint/2026-11-05/US.GOOG.raw.json:1` 另记录 SOXX 约 **-3.14%**，并明确“大市上升，半导体却下跌”。内部扫描也不是普跌：`data/opportunity_scan_2026-11-05.json` 为 79 只中 31 涨、36 跌、12 平，平均 +0.3%，市场描述为“震荡分化”；SPY=`MIXED`、QQQ=`DOWN`。

因此本议题应审的是：**DA 是否识别了“指数反弹、AI 硬件链重挫、云平台/防御板块相对受益”的结构性轮动**。上述指数数字来自当日采集的社区材料，用于判断模型看到了什么，不冒充交易所官方收盘 SSOT。

## 2. 审计口径与逐层结果

### 2.1 口径

- 36 只 checkpoint 全部为主模型 `qwen3.7-max`；挑战者均为 `deepseek-v4.1-flash`、`primary`、非自辩。
- “原始材料含事件”只扫描 `US.*.raw.json` 的 `collected` 载荷，不扫描 `user_prompt`，避免把提示词或后续输出重复计作输入。
- “核心产物提及事件”扫描规范化结论及 `_llm_output`，剔除复制输入的 `snapshot/technical/capital/news/sentiment/plates` 等容器；同一标的的 `_ftseek` 副本只计一次。
- 词表包含 election/midterm/Democrat/Republican/Senate/Congress/Blue Wave 及简繁体选举、党派、国会词。**已剔除** `selection` 中的 election 子串、`in-house`、中期业绩、中期技术面、“中期底部”等误报。
- 22/36 是“存在相关文本”的覆盖上限，不代表每条都及时、真实或经事实核验；其中既有 Finnhub 新闻，也有未核实社区观点和选前旧稿。

### 2.2 漏斗

| 层级 | 实测 | 解读 |
|---|---:|---|
| 扫描 → 深分析 | 79 → 36 | 深分析对象已确定，宏观事件没有独立发现入口 |
| 原始材料含选举相关文本 | **22/36** | 捕捉不是零，但依赖 per-symbol 命中，覆盖不一致 |
| 双模型核心产物仍显式提及 | **7/36** | AMD、COIN、HCA、META、MRK、SNDK、TSLA |
| 主模型 `causal_chain` 明确使用选举 | **4/36** | AMD、HCA、SNDK、TSLA |
| 最终 bridge | **18 = 15 LONG + 3 SHORT** | 其中 11 只原始材料含选举文本 |
| bridge 核心产物仍提及选举 | **4/18** | COIN、META、MRK、TSLA；COIN/META 是反方风险，MRK 主要来自挑战者补风险 |
| bridge 主因果链由选举驱动 | **1/18** | 仅 TSLA |

22 只输入覆盖标的为：AMD、AMZN、ARM、AVGO、BA、BWXT、COIN、CRM、GOOG、HCA、LLY、LMT、META、MRK、MU、NVDA、ORCL、QCOM、RELY、SNDK、TSLA、TSM。

**关键归因边界**：7/36 不能写成“7 只由选举推理产出”。例如 COIN 的主线是财报、降级和内部人卖出，选举是逼空反方风险；META 的主线是债券认购和估值，选举只在 bear case；MRK 的选举内容主要是挑战者补充的宏观风险。

## 3. 代表性标的复核

| 标的 | 主模型/挑战者 | 最终处置 | 审计判断 |
|---|---|---|---|
| **AMD** | 主模型明确写“民主党胜选 → AI 监管 → 半导体 → AMD”，却判为恐慌错杀、BUY 8；挑战者指出监管是新增板块压制、SOXX 减持和资金净流出，SKIP | `破位硬买拦截` → BLOCK | **事件识别正确，方向推理未闭环**；回避最终靠技术规则，不是双模型一致否决 |
| **NVDA** | 输入有 Democratic sweep 和两院翻转，主模型未把选举写入核心链，仍 BUY 6；挑战者 `DISAGREE+SKIP` | 保留 BUY 方向后由 `破位硬买拦截` → BLOCK | **信息丢失 + 挑战者反证未直接改指令**；最终避跌不能全部记给双 LLM |
| **SNDK** | 主模型明确把选举和存储走弱归为“情绪错杀”，BUY 6；挑战者 `DISAGREE+SKIP` | 保留 BUY 后由 `破位硬买拦截` → BLOCK | 捕捉到了，但把板块冲击解释为低吸机会，挑战者仍未扭转主方向 |
| **HCA** | 主模型认为“民主党胜选利好已在昨日 7% 大涨中定价”，盘前无量不追；挑战者 AGREE | SKIP | **本批最完整的合理宏观定价样本**：事件、已定价程度、资金确认和指令一致 |
| **TSLA** | 主模型以“德州民主党胜选 → AI/自动驾驶审批 → 高估值承压”给 SHORT 8；挑战者同向但 PARTIAL | WARN 进入 bridge | **唯一选举主因果链进入最终候选的标的**，但挑战者指出 11/4 已定价、375.50 支撑、逆板块、止损/确认/chase cap 冲突，反证未改变 score=8 的发布结果 |
| **AMZN/GOOG/META/CRM** | 最终均为 LONG | 进入 bridge | 结果方向与“云平台/软件相对受益”相符，但主理由分别是评级/ETF、估值、债券认购、公司催化；**不能反推其入选是选举相对受益推理所致** |
| **ARM/MU/QCOM** | ARM SHORT 3；MU/QCOM SKIP | 均 BLOCK | 系统不是完全没出硬件空头或回避，但 ARM 低分未发布，最终没有 NVDA/MU/SNDK/TSM 等硬件空头 |

补充边界：AAPL/MSFT 在本批 `data/shadow/candidate_funnel.jsonl:57` 均因 `low_atr` 在深分析前被拒绝，不能把其他链路的 AAPL 成交归因给本批 DA。

## 4. 最终指令是否合理

### 合理部分

- NVDA、AMD、SNDK、AVGO、TSM、ORCL 的左侧 BUY 最终均被 `破位硬买拦截` 挡住，避免了在明确空头动量中硬接飞刀。
- 最终保留 AMZN、GOOG、META、CRM 等多头，以及 TSLA、COIN、CAT 三个空头，和当日“不是普跌、而是板块分化”的结果方向有一定一致性。
- HCA 没有因“选举受益”追高，体现了对“利好已定价”的正确警惕。

### 不合理或证据不足部分

- **避险归因错位**：多个硬件股是主模型仍 BUY、规则门兜底，不应表述为双 LLM 已识别硬件风险并主动回避。
- **没有形成板块级空头组合**：破位 BUY 门只拦截、不翻空；ARM 空头又因 score=3 被挡。因此本批没有将 NVDA/MU/SNDK/TSM 等重挫链转成最终空头指令。
- **挑战者不是硬约束**：NVDA/SNDK 的 `DISAGREE+SKIP` 和 TSLA 的多项实质质疑都留下了文本，但没有相应改写最终方向或分数。
- **结果相符不等于原因正确**：AMZN/GOOG/META 等 LONG 不能被追认为“选后相对受益交易”，因为发布 thesis 没写这条主因。
- **盘前报告不可解释**：`obsidian_vault/daily/premarket/2026-11-05_us_premarket.md:12` 只给“震荡分化/中性”和标的清单，没有呈现事件、传导链、已定价程度或受益/受损分组。

盘后报告仅支持极小样本结果检查：DA 已平仓 COIN 空 +$20.43、CRM 多 +$11.86；`obsidian_vault/daily/postmarket/2026-11-05_us_postmarket.md:34` 记录原始方向 BUY 11、SELL_SHORT 3，最终成交 2。**n=2 不能证明宏观推理有效，更不能推出稳定 edge。**

## 5. 根因：不是单一 prompt 问题

### 5.1 信息层是 per-symbol 碰运气，不是共享事件层

- `/root/.hermes/skills/deep-stock-analysis/scripts/data_collector.py:218` 使用 Finnhub `company-news`，按单股取近 7 天新闻；`llm_synth.py:679` 再把这些材料注入各股 prompt。
- `/root/.hermes/skills/deep-stock-analysis/scripts/data_collector.py:1057` 的结构化 `market_context` 仍是 SPY/QQQ/VIX；没有统一的选举事件对象、事实/观点分层、事件时间和跨资产确认。
- 当前已经有真实板块上下文：`/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py:1042` 明确注入板块轮动数据。因此缺口不是“完全没有板块信息”，而是**同一宏观事件没有被标准化、去重并一致分发给所有相关标的**。

这解释了为什么 AMZN 材料里有完整跨资产复盘，NVDA 只有标题/社区片段，其他一些标的核心输出完全不再提选举。

### 5.2 双 LLM 合并规则保留主方向，质疑多落为风险文本

- `/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py:2908`：同向但 PARTIAL 时保留主模型 score，只记录挑战者低分影子和质疑。
- `/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py:2937`：一方 SKIP 时保留有观点方方向；即使 `DISAGREE+SKIP`，也只是不抬 confidence，而非自动 SKIP。
- `/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py:2983`：挑战者 missed risks 追加到 risks；这不等于指令已吸收反证。
- `/root/.hermes/skills/deep-stock-analysis/scripts/signal_gate.py:427`：score≥5 且 conf=0.50 最低可为“中/WARN”，所以明确反对也不必然硬 BLOCK。

因此当前“双 LLM”更准确的语义是：**主模型产指令，挑战者证伪并影响置信度/留痕**；不是两个模型共同对宏观传导链做一致裁决。

### 5.3 风控门会回避，但不会主动发现宏观空头

`/root/.hermes/skills/deep-stock-analysis/scripts/signal_gate.py:446` 的设计就是 bearish+BUY → SKIP，并明确“做空交由 UE 浮现”。所以本批硬件 BUY 被挡是合理的安全结果，但不能要求该门自动产 NVDA/SNDK 空头；这是架构分工，不是单次模型失误。

## 6. 关键分歧

1. **最终避开了部分下跌，是否说明双 LLM 推理成功？**
   - 否定。必须区分主模型判断、挑战者判断、合并规则和后续技术门；本批多个关键回避由最后一层完成。
2. **是否说明 DA 完全没有宏观推理能力？**
   - 也否定。AMD、HCA、SNDK、TSLA 都有显式因果链，HCA 尤其合理；问题是覆盖、稳定性和指令承接不足。
3. **是否应立即把挑战者 `DISAGREE` 或 validation fail 改成硬 BLOCK？**
   - 证据不足。单日回放无法回答净期望、误杀和供给影响；且这会与宏观信息层修复形成第二变量，不应顺带上线。
4. **是否只要 prompt 加一句“重视宏观新闻”即可？**
   - 不足以解决根因。当前 prompt 已要求“宏观→板块→个股”因果链（`llm_synth.py:228`），缺的是一致、结构化、可审计的共享事件输入和指令归因。

## 7. 决策与可选改进

### 本次决策

- **生产不变更**：不改 prompt、不改模型、不改 gate、不重启、不新增推送、不补写历史指令。
- 本批定性为：**捕捉部分成立；推理部分成立；最终风控部分合理；完整的双 LLM 宏观到标的能力尚未闭环。**

### 若用户批准继续，优先做一个变量的离线 A/B

先只做 **共享 `macro_event_brief` 离线重放**，不直接改 live：

1. 从同一批原始新闻构建一个带 `event_time/source/fact_vs_opinion` 的选后事件简报，包含指数、SOXX/板块、利率/油价，以及“AI 硬件受损、云/软件相对受益”等待证伪传导假设。
2. 将完全相同的简报注入 36 只冻结 checkpoint 的重放输入；主模型和挑战者模型、温度、预算、门禁全部保持不变。
3. 对照原版只比较：事件引用率、主因果链采用率、方向/score 变化、bridge 增减，以及反证是否被承接；不重写历史 checkpoint。
4. 若要新增 `event_role/event_id` 等 prompt 字段，必须同时登记 live 消费方和基数自检，禁止只写不读。

**合并规则是否修改必须另案**，待重放证明信息层确实改善后，再单独评估；不得与共享简报同日叠加。

### 验收不看“故事更像”，看可复现行为

- 36/36 收到同一事件简报，来源和时间可回溯；事实与社区推测分栏。
- 每个 bridge 标的能区分“宏观主因/确认因素/反方风险/未采用”，AMZN 等若被称为宏观受益，发布 thesis 必须显式写出。
- AMD/NVDA/SNDK 不预设必须翻空，HCA 不预设必须 BUY；要求模型对“已定价、板块传导、个股反证”给出一致解释。
- TSLA 同类情形必须处理挑战者指出的已定价、支撑、逆板块和执行几何冲突，而不是只追加 risks。
- 任何收益结论另按 DA-only、费后净 bps、日聚类、样本量/MDE 预注册评估；本批 n=2 仅作事实记录。

## 8. 执行项与验证

- [x] 复核 11 月 5 日 36 只原始输入、双模型输出、门禁和 18 只 bridge。
- [x] 区分“输入有事件”“输出提及事件”“主因果链采用事件”“进入最终候选”四种口径。
- [x] 核对主模型/挑战者、合并规则、破位 BUY 门和盘前/盘后报告。
- [x] 仅落审计文档、DEB 索引和 handoff；生产代码、配置、数据零修改。
- [ ] 待用户明确批准后，才进行共享宏观事件简报的离线 A/B；本批未执行。

验证命令示例：

```bash
# 最终候选、方向、thesis 与模型信息
python - <<'PY'
import json
from pathlib import Path
d = json.loads(Path('data/opportunity_scan_2026-11-05.json').read_text())
print(d['model_used'], d['candidate_count'])
for c in d['candidates']:
    print(c['symbol'], c['direction'], c['score'], c['thesis'])
PY

# 关键双模型争议与最终门处置
rg '2026-11-05' data/da_gate_decisions.jsonl | rg 'US.(AMD|NVDA|SNDK|TSLA|HCA)'
```

本批仅文档变更，未运行交易测试、未重启服务；计数通过独立遍历 36 份 raw/checkpoint 实跑复现。

## 9. 第二复核补注：日期与模型归因（2026-11-06 08:00 CST）

1. **日期**：§1 的“不是普跌”仅指 11-04 首个选后完整收盘。联合复核的 SPY 5m 路径显示，11-05 常规时段确实显著下跌（09:55 −1.59%、15:55 −4.05%）。若用户原话指 11-05，则不应使用 11-04 指数上涨反驳；本文研究的是 11-05 盘前如何响应此前已知信息。
2. **事件可见性**：事件回放必须以 `first_seen_at<=as_of` 为准，不能用 `event_time` 假装系统提前看到。联合复核发现 `Chip selloff…` 直到 11-05 18:49 UTC 才入库，盘前不可用；但 `Morning Bid…data centres` 与 `post-election rally…AI split` 在 11-04 已入库，支持继续做共享事件简报。
3. **模型归因**：本文 §2.1 的模型口径有 DA 直连源码和 36/36 API 回显支持：主模型 `qwen3.7-max`、挑战者 `deepseek-v4.1-flash`。`llm_audit.feature='agent_bridge'` 是另一调用链，不能用来把本批 DA 改记为 `deepseek-v4-pro`。
4. **实现路线更新**：共享简报、as-of 防未来数据、全局缓存、方向/指令字段和双模型仲裁见 `DEB-20261106-market-risk-switch-review.md` §14.5–§14.8；盘前全量构图、盘中增量刷新、日内指令卡和安全改单生命周期见 §15，第一 Agent 交叉核查及采集时延补充见 §16。本文原“共享 macro_event_brief 离线 A/B”由该方案承接，不另起平行项目。
