# 议题：DA/UE 模型配置统一性核查 + 挑战者模型 deepseek-v4-pro → glm-5.2 切换评估

## 状态

- [x] 进行中（第 1 轮：配置普查 + A/B 首轮完成，机械判据通过、行为判据存疑）
- [ ] 待决策（等第 2 轮正常交易日 A/B 数据合并裁决）
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

> **本文用途**：回答用户 2026-07-24 两问——① DA、UE 使用的模型配置是否统一？② 能否将 deepseek-v4-pro 换成 glm-5.2？供其他 Agent/模型接手时了解完整逻辑与证据链。
> **作者**：Qoder　**日期**：2026-07-24
> **背景约束**：当日为三门（stall/连败封禁/低ATR）+ topup 扩容的首夜验证日，**同夜切模型会破坏归因**（变量隔离原则）。用户已裁决"先 A/B 对照再切"。

---

## 一、Q1 答案：配置不统一 — 两套体系 + 多处硬编码

| | DA（deep-stock-analysis，Hermes 外部 skill） | UE（llm_universe_scan，主工程） |
|---|---|---|
| 配置位置 | `/root/.hermes/skills/deep-stock-analysis/scripts/config.py:59`（主模型）+ `llm_synth.py:29`（挑战者**硬编码**） | `unified_config.yaml → llm.features.universe_expansion_reasoning`（统一路由表：逻辑名→注册表） |
| 分析师/主模型 | qwen3.7-max | qwen-max（→qwen3.7-max，恰好一致） |
| deepseek-v4-pro 角色 | **挑战者**（Pass2 风险审查官，v0.39 设计"天生偏空的魔鬼代言人"→ 后已改为对称审查 prompt） | fallback（llm_audit 实查近 5 日 **0 次触发**） |

主工程另有 4 处 deepseek-v4-pro 残留引用（历史欠账）：`agents.decision_agent`、`debate_planner.agents.claude_code`、`app/llm/multi_model_client.py:37`、`app/llm/dual_agent_debate.py:232`（硬编码）。

**glm-5.2 就绪状态**：已在模型注册表注册（逻辑名 `glm-5` → model_name `glm-5.2`，DashScope 同 key，cost_in 0.006/cost_out 0.02 = deepseek-pro 的 3 倍）；已在 `stock_analyst`、`voting_glm` 两个 feature 当 primary 运行；`model_call.py:135-137` 有 reasoning-token 兼容处理（实测 max_tokens=800 时 reasoning 吃光配额 → 空响应，**但 DA skill 用自己的 `_call_llm` 不走这层兼容**——这正是 A/B 必测项）。

## 二、Q2 答案：技术上可换，但三个风险先摆出来

1. **换的是辩论对手的人格，不只是模型**：挑战者的同意/反对直接进 confidence 合成（±0.20 / -0.15，`pipeline.py:416-428`），该链路已知被"弃权→WARN→0.35"污染（DEB-20260723 P0.2 待修，C4 硬前置）。换挑战者 = conf 分布再变一次，两变量叠加难归因。
2. **兼容性有已知陷阱**：glm-5.2 reasoning 吃配额风险（见上），DA skill 的调用路径未经该验证。
3. **成本 3 倍** + 当日为三门/topup 首夜，同夜切换破坏归因。

## 三、A/B 对照设计（预注册，`scripts/challenger_model_ab.py`）

- **输入复刻**：07-23 opportunity_scan 全部 15 只候选重建 initial_analysis（**剥离旧挑战者辩论产物防泄漏**）；data_text 取 `playbook_signal_context.raw_data_json`（与生产同源）。
- **调用复刻**：直接 import skill 的 `llm_synth._call_llm`（同 HTTP 路径/JSON 解析/key）；prompt 逐字复刻 `_run_challenger` 构造。⚠ skill 后续改 prompt 需同步本脚本。
- **record-only**：结果落 `data/shadow/challenger_model_ab.jsonl`，零 live 改动。
- **预注册判据（切换前全部达标）**：① glm 成功率 ≥ deepseek 且 0 空响应；② verdict 不退化为全 AGREE（挑战者独立性）；③ 均延迟 <120s；④ 成本可接受（用户判）。

## 四、首轮结果（2026-07-24，n=15，07-23 崩盘日候选，全 BUY 单边样本）

| 判据 | deepseek-v4-pro | glm-5.2 | 判定 |
|---|---|---|---|
| JSON 成功/空响应 | 15/15 / 0 | 15/15 / 0 | ✅（reasoning 空响应担忧未发生） |
| verdict 独立性 | 14 DISAGREE + 1 PARTIAL | 15 DISAGREE | ✅ |
| 均延迟 | 17.0s | **12.1s（快 29%）** | ✅ |
| 成本估算/日（含3次采样） | ~$0.8 | ~$2.3（占 $5 日预算近半） | ⚠️ 待用户判 |

**行为差异（关键发现，非判据但影响决策）**：两模型都正确反对崩盘日全 BUY，但反对方式不同——
- deepseek：4 × SELL_SHORT + 11 × SKIP → **敢给可操作的反向方向**；
- glm-5.2：1 × SELL_SHORT + 14 × SKIP → **保守守门员型**。

与系统当前目标的冲突：方向灵活性恢复（SHORT 占比目标 20%+，v2.1 刚修复）依赖挑战者的反向判断；glm 的高 SKIP 倾向可能压制 SHORT 恢复 + 进一步降交易量（与同日 topup 扩容方向相反）。

## 五、当前裁决与待办

- **首轮裁决**：机械判据通过（切换是安全的），行为证据不支持切（单日、单边样本，说服力有限）。**维持 deepseek-v4-pro 不动**。
- **待办（第 2 轮）**：正常交易日候选再跑一轮 `./venv/bin/python scripts/challenger_model_ab.py 2026-07-24 15`（07-24 scan 生成后），两日数据合并裁决。若 glm 仍高 SKIP 低反向 → 建议不切；若用户偏好其速度/严格性 → 只切 DA 挑战者一处，UE fallback 与 4 处硬编码分批跟进。
- **与切换无关的正确动作（任何时候可做）**：DA 模型配置从 skill 硬编码收敛到统一配置体系（读 config，一处管理），零风险，随下次 DA skill 改动顺手做。

### 第 1 轮讨论记录

- 作者（Qoder，2026-07-24）：完成配置普查（不统一，两套体系+4处硬编码残留）与 A/B 首轮。核心发现：glm-5.2 机械指标全优（更快、零空响应）但挑战者人格更保守（93% SKIP），与方向灵活性恢复目标冲突。裁决暂不切，待正常日第 2 轮。交下一轮核验点：正常日 verdict 分布、glm 高 SKIP 是否复现、合并样本的反向意愿差异是否显著。
- 第二模型复核（2026-07-24，用户委托，跨 DEB 强化）：首轮裁决（维持 deepseek）获 debate-divergence DEB 独立强化——该文已立案"真反向→SKIP/flip"消化机制，deepseek 敢给可操作反向（首轮 4×SELL_SHORT）的产出在新机制下是**有价信息**，glm 的 93% SKIP 仅为弃权信息；切换的隐性成本 = 放弃反向信号供给，与本文"方向灵活性恢复"论点相互独立、同向叠加。维持原裁决，第 2 轮正常日判据不变。
