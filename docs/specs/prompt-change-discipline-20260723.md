# Prompt 变更纪律 (P3.9 成文, 2026-07-23)

> 来源: DEB-20260723-da-ue-prompt-optimization 共识 (两轮辩论收敛)。
> 适用范围: DA (deep-stock-analysis skill) 与 UE (llm_universe_scan) 两链路的一切
> prompt / 输出契约 / 置信度映射变更。**违例先例: UE V2 凭 1 天 A/B (4 条记录) 直觉上 live。**

## 1. 版本戳前置 (硬门槛)

任何 prompt/skill 侧变更之前, 变更对象必须具备可归因的版本戳:
- UE: scan 落库 `prompt_version` (已有)。
- DA: bridge JSON 落库 `skill_version` (P0.2 执行项, 与 SKILL.md frontmatter 一致)。
无版本戳的变更 = 不可归因变更 = 禁止 (N3 实证: 07-18~22 漂移窗口 3 处工作区外变更零可见)。

## 2. 升级判据 — 样本计数制 (替代日历制)

低吞吐链路 (UE 每日 2-3 浮现) 日历门槛不适配, 改用样本计数:
- **≥30 个浮现候选, 且全部有 T+1 outcome** (约 10-15 交易日自然达成)。
- 分层比较: 新旧 prompt 的关键切片 (如 self_tier 命中率/前瞻收益) 差异显著 (符号检验)
  方可升级; 否则保持现状。

## 3. 并行 shadow 替代串行 A/B

同日双跑、只 live 一个 (同 regime 配对比较, 消除日间干扰)。
串行 A/B (先跑旧版 N 天再跑新版 N 天) 把 regime 差异误记为 prompt 差异, 禁止作为升级依据。

## 4. 回放测试 (INTC 原则推广到 prompt 层)

任何新 prompt/新门上线前, 必须回放最近一个"已知反例日"确认不误伤:
- 门禁类: 07-22 回放不得拦 INTC (当日唯一盈利单)。
- prompt 类: 对最近 5 个交易日的同 clue/同候选集离线重跑, 输出方向分布不得出现
  无法解释的断崖 (如单日全 LONG / 全 SKIP)。

## 5. 置信度语义

- 标量 confidence 是有损压缩, 下游不得当校准概率直接消费 (IC 已为负, 两链路实证)。
- 语义映射变更 (如弃权=分歧解耦) 必须先 record-only shadow 双写 (conf_semantic/conf_live),
  测量满两周且目标 cohort 实绩不劣, 方可切 live (防"给亏损 cohort 反向加仓")。
- 端到端验收断言: 语义层能区分的状态 (弃权 vs 真反向), 落库值必须可区分 ——
  单点修复被下游规则栈吹零的病理 (N1) 以端到端断言兜底, 不接受单点自证。
