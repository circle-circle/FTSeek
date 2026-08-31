# DEB-20260831 · DA 辩论质量与 decision_conf 常量退化 — 根因定案 + 预注册判据

> 状态: **观测中 (record-only)**。本档不动任何 live 路径, 符合 9 月冻结 (判据字典 C1/C7)。
> 起源: 2026-08-31 DA 链复查 (handoff 2026-08-31d) 发现三项关联病灶, 用户要求
> "预注册判据裁决 + decision_conf 根因彻底查清"。证据全部为 DB/代码实证, 无文档自述采信。

---

## 一、三个观测事实 (实证)

| # | 事实 | 数据 |
|---|---|---|
| O1 | 新挑战者 (deepseek-v4-pro-0813, 8/27 切换) AGREE 率趋近饱和 | 旧挑战者 8/25=16/22(73%) / 8/26=23/27(85%); 新挑战者 8/27=23/24(96%) / 8/28=25/25(100%)。DISAGREE: 8/25=4 → 8/27=1 → 8/28=0 (da_checkpoint `_debate.challenger_verdict` 统计) |
| O2 | `decision_conf` 恒 0.70 | playbook_instructions 8/24–8/28 全部 DA 行 (n=70, model_used=qwen3.7-max) decision_conf ∈ {0.7, NULL}, 0.7 占 100%。"常量退化"第四例 (前三例见 AGENT.md 军规#3) |
| O3 | 辩论后 confidence 文字档膨胀 | 8/28 最终 confidence: 高=18/中高=5/中=2 (72% 为"高"), 而 SYSTEM_PROMPT【校准原则】明示历史方向命中率≈50% |

## 二、decision_conf 根因全链 (已完全理清)

### 2.1 产生端 (唯一公式)
`adapter.py:_compute_confidence` (skill 仓):
```
conf = 0.50 (基准)
  同方向 + (agreed 且独立挑战者): +0.20 → 0.70
      └ Phase-0 衰减 (8/23): |analyst_score − challenger_score| > 3 时每超 1 点 −0.015
  一方弃权 (SKIP): 0.50 (7/18 根因修复后中性)
  真反向 (BUY↔SELL_SHORT): −0.15 → 0.35
  无辩论/fallback: 0.50
```
设计意图 (docstring): confidence = P(profitable | signal)。模型自评 conf 与数据质量分
已实证无预测力 (r=−0.211 / −0.073) 被弃用, **唯一保留信号 = 辩论一致度 (r=+0.102)**。

### 2.2 常量化的机制链 (三环叠加)
1. **AGREE 饱和 (O1)** → "+0.20" 对几乎全部有方向候选生效 → 输出塌到 0.70。
2. **score_band 量化杀死 Phase-0 衰减**: r14 (8/25) 把连续 score 量化为 band→{8,6,3}。
   衰减触发需 |Δ|>3 → 量化后 Δ∈{0,2,3,5}, 仅 HIGH-vs-LOW (Δ=5, 衰减 0.03) 可触发,
   且还需同方向+AGREE 并存 — 实测 8/24–28 零触发。**Phase-0 (8/23 为"打破恒0.70"
   专门设计) 在 r14 (8/25) 上线后重新失效** — 两次修复时序交错造成的设计互斥, 此前无人察觉。
3. **真反向近乎消失** (新挑战者 DISAGREE≈0) → 0.35 分支不可达。
→ 理论值域 {0.70, 0.67, 0.50, 0.35} 实测只剩 {0.70}。

### 2.3 消费端审计 (危害面定界)
| 消费点 | 状态 | 结论 |
|---|---|---|
| `decision_conf` 列 (DB) | 零 live 消费 (仅 `gate_switch_compare.py` 分析脚本读 gate jsonl) | 纯记录/分析字段 |
| `confidence` 列 (=conf×100) → 执行排序次键 (`pipeline.py:3539-3554`, source_priority 为主键) | **唯一 live 消费** | 恒 70 → DA 内部排序并列退化为稳定序, 无实际扭曲 (同日 DA 指令同质) |
| `admission.min_confidence_buy/short` | =0 刻意禁用 ("无 IC 证据前不应启用") | 不拦 |
| `strategy_registry.direction_filter` (BUY/SHORT_min_confidence=70) | 仅 `technical_intraday` enabled; DA 指令直写 DB 绕过 `_build_instructions_from_votes` | 不作用于 DA |
| Kelly confidence sizing (`pipeline.py:3907`) | `confidence_sizing_enabled=False` | 禁用 |

**定案**: decision_conf 常量退化当前**不扭曲任何 live 行为**; 危害限于 ①分析/切片维度
零区分度 (校准研究、scorecard 按 conf 分层失效) ②执行排序次键退化。它是温度计读数,
不是病灶本身。

### 2.4 根因归属
病根 = **辩论层信息量塌缩 (AGREE 饱和)** — 挑战者几乎恒证伪失败时, 一致度信号退化为
常量, 任何公式都无法从无信息输入中榨出区分度。故:
- **修温度计 (重构 conf 连续值) ≠ 修复区分度**, 且 conf 公式是 8/16 DEB 裁决产物
  (弃自评/留一致度), 单方面重构 = 翻已终裁判据, 禁止;
- 正确路径 = 先裁辩论质量 (§三 B11), 再按裁决结果决定 conf 通道处置 (§三 B12)。

## 三、预注册判据 (新增, 同步登记判据字典 B11/B12)

### B11 · 挑战者有效性裁决 (AGREE 饱和诊断)
- **观察窗**: 2026-09-01 → 09-12 共 10 交易日 (新挑战者 8/27 上线, 窗口全部为 0813 样本)。
- **度量** (均可从 da_checkpoint `_debate` + playbook_instructions + trade_records join):
  ① AGREE / PARTIAL_AGREE / DISAGREE 率 (按日序列);
  ② 挑战者 my_score_band 分布与分析师 band 的一致率;
  ③ independent_direction ≠ my_direction 比率 (防锚定观测, 当前基线 8/49=16%);
  ④ DA 已执行样本按 verdict 切片的 WR / 笔均 PnL。
- **裁决规则** (窗口结束一次性裁决, 样本底线遵循判据字典 A1/A6):
  - **a. 仪式化成立**: AGREE≥95% ∧ 切片 PnL 无区分度 (AGREE 放行样本 WR/笔均 与
    全样本差异不显著) → 辩论退化为仪式, 三选一提案 (均需另立 DEB + 影子先行):
    (i) 证伪官降阈 — 反证门槛从"≥2条有出处"降为"≥1条强反证";
    (ii) 挑战者角色回退"独立复现" (v1 人设, 但带防锚定先行);
    (iii) 废止辩论回单模型 (省 1 次 LLM/候选)。
  - **b. 辩论存活**: AGREE<95% ∨ 切片显示 PARTIAL/DISAGREE 对 PnL 有方向性区分
    (n≥20) → 维持现状, 转入月度常规观测。
  - **c. 样本不足**: 执行样本 n<30 → 仅凭 ①②③ 结构性指标裁, ④ 顺延。
- **禁止动作**: 观察窗内不得因本判据改挑战者 prompt/模型/合并规则 (污染样本)。

### B12 · decision_conf / confidence 通道处置 (条件于 B11)
- B11 裁"仪式化"且选 (iii) → decision_conf 语义消亡, 提案停写或改写 (另立 DEB);
- B11 裁"辩论存活" → 提案恢复 conf 连续性 (record-only 影子先行: 新增观测列记录
  band 对 + Δscore + verdict, 不动现有列与任何门), 攒样后再谈是否接入门禁;
- **冻结声明**: 在 B11 裁决前, decision_conf/confidence **禁止接入任何新 live 门**
  (常量字段接门 = 方向性禁令而非过滤器, 判据字典 C 类红线同构, 参照 8/16
  max_sl_pct=6.0 全杀空头事故)。

## 四、与在档判据的衔接
- B5/B6 (UE-D1/D2) 覆盖 UE 侧 invalidation_triggers 填充与 glm 首发观察 — 与本档正交;
- 本档 O1 的旧挑战者数据 (8/25-26) 属 deepseek-v4-pro 时代, 与 v0.71 切换归因
  (SKILL.md) 可切分, 不混窗;
- Phase-0 与 r14 的设计互斥 (§2.2-2) 为本次新发现的历史事实, 登记备查, 不追责不回滚。

## 五、诚实边界
- 新挑战者样本仅 2 天 (8/27-28), O1 的"饱和"判断待窗口确认, 本档不做提前裁决;
- 切片 PnL 受执行样本量限制 (8/24-28 DA 执行 ~50 笔), ④ 的统计功效有限, 已写入 c 条款;
- decision_conf 在 8/24 前有少量非 0.7 历史值 (旧口径时代), 本档只论 8/24 后统一管线窗。
