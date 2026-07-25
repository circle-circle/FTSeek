# 议题：分析师 score 区分度坍缩 — 0-10 评分退化为二值常数，tier 分档与仓位失去信息量

## 状态

- [x] 进行中（调查完成，待决策）
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

> **本文用途**：数据驱动的调查结论，供立项决策。所有结论附**文件/命令/数据**可复现。
> **作者**：Qoder（架构师视角自查）　**日期**：2026-07-23
> **前置关联**：[DEB-20260723-da-pipeline-efficiency.md](./DEB-20260723-da-pipeline-efficiency.md)（方案 E "score<5 跳挑战者今日节省为零" 的根因即本文）。

## 背景

在 DA 管线效率辩论中反复出现一个现象："analyst score 恒定 6~7，没有候选命中 score<5"。当时把它当作"方案 E 暂不生效"的旁注。从量化信号角度重新审视：**score 是 tier 分档（优/良/中/差）与仓位系数的核心输入，若它恒为常数，则这一整条信号维度对最终决策的贡献 alpha 接近于零。** 这不是成本问题，是信号质量问题，比任何管线优化都更影响收益。

## 目标

1. analyst score 的历史分布是否真的退化为常数？区分度有多低？
2. challenger score 是否也退化，还是保留了区分度？
3. 退化的根因是 prompt 诱导，还是打分发生在错误的位置（对已筛选样本再打分）？
4. 有无低成本的改进方向（如启用 challenger score 做分档）？

## 数据与实证（全部可复现）

```bash
cd /opt/ftseek/FTSeek-US
python3 -c "
import json, glob, statistics
from collections import Counter
files = sorted(glob.glob('data/opportunity_scan_*.json'))
a, ch = [], []
for f in files:
    d = json.load(open(f))
    for c in d.get('candidates', []):
        if c.get('score') is not None: a.append(float(c['score']))
        cs = c.get('debate',{}).get('challenger_score')
        if cs is not None: ch.append(float(cs))
print('analyst 分布:', sorted(Counter(a).items()), 'stdev', round(statistics.pstdev(a),3))
print('challenger 分布:', sorted(Counter(ch).items()), 'stdev', round(statistics.pstdev(ch),3))
"
```

### 实证 1：analyst score 只有 2 个值（n=106，07-08 ~ 07-23）

| score | 次数 | 占比 |
|---|---|---|
| 6.0 | 16 | 15% |
| 7.0 | 90 | 85% |

- **标准差仅 0.358，唯一值个数=2。** 07-08~07-17 期间**全部为 7.0**（零区分度）；07-20 起才偶现 6.0。
- 直接后果：`signal_gate.py:350-357` 的 tier 阶梯（score≥7=优、≥6=良、≥5=中、<5=差）**退化为二值**——几乎所有候选落入"优/良"，"中/差"档形同虚设。

### 实证 2：challenger score 反而有区分度（n=103）

| score | 次数 |
|---|---|
| 4.0 | 24 |
| 5.0 | 68 |
| 6.0 | 8 |
| 7.0 | 3 |

- **标准差 0.646（≈analyst 的 2 倍），唯一值个数=4，中心在 5.0。** 挑战者明显更严格、更愿意打低分，且对不同标的有实际区分。
- 关键对照：同一批标的，analyst 说"都值 7 分"，challenger 说"多数只值 4~5 分"。**区分信息一直存在，只是没被 live 采用。**

### 实证 3：退化根因指向"打分位置错误"而非 prompt

- 能进入 `opportunity_scan` 的候选已被前置漏斗（市场扫描 + 因子筛选 + gate）筛过，是"已经不错的一批"。分析师面对同质化的优质样本，自然趋同给高分。
- 即：**对已筛选样本再做绝对打分，天然缺乏区分度**。这是 pipeline 设计问题，改 prompt 难以根治（除非改为相对排序或提高打分位置的样本异质性）。

## 可选方案

### 方案 A：live 分档改用 challenger score（或 analyst×challenger 组合）

- 提出者：Qoder
- 核心思路：tier 分档的 score 输入从 analyst score 换成 challenger score，或取两者加权/取严。challenger 已证明有区分度。
- 优点：零新增 LLM 成本（challenger 已在调用）；立刻恢复分档区分度；与"挑战者天生偏空、适合做风险审查"的设计定位一致。
- 缺点：challenger score 的**预测有效性未验证**——区分度高 ≠ 预测准。必须先 shadow 对比 T+1 outcome，不能直接切 live（会改变仓位）。
- 适用条件：先 record-only shadow 两周，证明 challenger score 分档与实际胜率单调相关后再切。

### 方案 B：改打分位置 / 改为相对排序

- 提出者：Qoder
- 核心思路：不在"已筛选样本"上做绝对打分，而是让分析师对当日候选做**相对排序**（谁比谁更好），或把打分前移到样本更异质的阶段。
- 优点：根治区分度坍缩的设计根因。
- 缺点：改动大，涉及 pipeline 结构与 prompt 重构；相对排序在候选数少的日子意义有限。
- 适用条件：方案 A 若证明 challenger score 也不预测，则需回到此根本方案。

### 方案 C：接受现状，废弃 score 维度

- 核心思路：既然 analyst score 无区分度，干脆不让它参与 tier/仓位，改用其它已验证的信号（如因子分、confidence、outcome 回测权重）。
- 优点：诚实——不用一个假装有信息的常数。
- 缺点：需确认 tier/仓位改用什么替代输入；可能牵动下游多处。

## 关键分歧

1. **challenger score 高区分度是否等于高预测力？**
   - 乐观：它更严格、更接近"真实质量"。
   - 谨慎：区分度只是方差，不是准确度；偏空模型可能系统性低估。
   - 可验证方式：shadow 落库 challenger score 分档 vs T+1/T+5 outcome，算分档与胜率/收益的单调性（Spearman）。

2. **根因是位置错误还是模型能力？**
   - 若换 prompt 要求"必须用满 1-10"能拉开分布且预测改善 → 部分是 prompt。
   - 若强制拉开分布后预测反而变差 → 是"对已筛选样本打分"的信息论下限，只能改位置（方案 B）。

## 决策

- 选择方案：（待决策，建议先做方案 A 的 shadow 验证）
- 决策依据：
- 保留空间：
- 负责人：
- 截止时间：

## 执行项（决策后启动）

- [ ] （前置）shadow 落库：每候选记录 analyst_score / challenger_score / tier(现) / tier(若用challenger)，record-only 不改 live
- [ ] 积累两周 + T+1 outcome 后：算两种 score 的分档-胜率单调性对比
- [ ] 若 challenger score 预测更优 → 提案切 live（单独批准，属仓位行为变更）
- [ ] 若两者都不预测 → 升级到方案 B（改打分位置）或方案 C（废弃 score 维度）

## 验证项

- [ ] shadow 数据满两周且每候选有 T+1 outcome
- [ ] 分档-胜率 Spearman 相关：challenger 分档是否显著优于 analyst 分档
- [ ] 决策落地后：tier 分布不再是二值（"中/差"档有真实占比）

## 讨论记录

### 第 1 轮

- Qoder（作者，架构师自查）：提出实证 1-3。核心结论：analyst score 已坍缩为二值常数（stdev 0.358，85% 为 7.0），tier 分档退化为二值，该信号维度 alpha≈0；challenger score 保留区分度（stdev 0.646，4 个值）但预测力未验证；根因指向"对已筛选样本做绝对打分"的设计缺陷，非纯 prompt 问题。建议先 shadow 验证 challenger score 的预测力，不擅自改 live 仓位逻辑。
- （待辩论方）：
