# UE thinking-eval A/B 归档说明（2026-09-21）

**状态：实验已结题（2026-09-17 用户裁），陈旧裁决提醒已于 2026-09-21 移除。生产未因此批改动任何思考开关。**

## 时间线

| 日期 | 事件 |
|---|---|
| 2026-09-05 | REVIEW-20260905 §10 预注册 UE thinking on/off 影子 A/B；采集 cron `12 21 * * 1-5 llm_universe_scan.py thinking-eval` |
| 2026-09-08 | 发现"文档声称已设 9/22 提醒"但 cron 无此 job → 补建 hermes job `1e96c03c945e`（once @ 2026-09-22 09:30 CST） |
| 2026-09-14 | 用户裁"DA 和 UE 都开思考"→ UE 侧因对照组=生产关思考而挂起（见 memory ue-thinking-ab-control-arm） |
| 2026-09-15 | 用户裁 UE 换模 glm-5.2→glm-5.3；glm-5.3 为**强制思考**模型（dashscope 对 enable_thinking=False 返回 HTTP 400） |
| 2026-09-16 | 最后一轮攒样（共 9 轮，<10 轮判据门槛） |
| 2026-09-17 | **用户裁「按建议执行」③：thinking_eval 收口归档 — glm-5.3 强制思考，off 臂物理消失**；采集 cron 注释停用 |
| 2026-09-21 | 全链路复查发现 9/22 提醒 job 仍 armed（实验已死但提醒会触发一次无对象裁决）→ 经用户批准"删除采集与提醒 cron"，`hermes cron remove 1e96c03c945e`（jobs.json 先备份 `.bak-20260921-thinking-eval-archive`） |

## 9 轮攒样数据摘要（data/shadow/thinking_eval.jsonl，9/05–9/16）

- **有效性**：轮次 9 < 10（判据门槛）；窗口内 prompt 模板 4 个版本（v2.9/v2.10/v2.11/ue_brief_v4）、
  双臂模型 3 种（glm-5.2 / deepseek-v4-pro / deepseek-v4.1-flash）——按预注册"先按
  brief_template_version 切段"则每段 1~6 轮，均不足；读数**不足以判胜负**（与 9/17 结题结论一致）。
- **方向性观察**（仅作参考，非裁决依据）：ON 臂延迟 147~612s vs OFF 臂 16~35s（10~20 倍）；
  ON 臂输出 tokens 约 +35%（样例 6142 vs 4550）；两臂 surfaced_brief 决策清单基本一致
  （同标的同方向，置信度差 ±0.02~0.08）——与既有实测「决策层在采样参数变化下已恒稳」
  （memory sampling-params-no-determinism）相符：思考模式主要改变延迟/成本，未观测到决策差异。

## 现状澄清（易误读点）

- `unified_config.yaml:513` `universe_expansion_reasoning.extra_body.enable_thinking: true`
  是 **glm-5.3 模型强制要求**，不是"生产开思考"的 A/B 开关；`llm_universe_scan.py` 的
  brief 生产调用点（~3707/~2303）思考开关仍为关。两者是不同调用路径，勿混淆。
- 若未来重开"UE 生产开思考"问题：glm-5.3 下 off 臂物理不可得，任何新实验须以
  **固定模型+固定模板**重新预注册，或改用可关思考的模型做对照。

## 关联

- 预注册与裁决方法：`docs/debates/DEB-20260914-premarket-data-validity-audit.md` §5.5/§17.6
- handoff.md 2026-09-21 下午增量（用户批准记录）
- memory: ue-thinking-ab-control-arm / sampling-params-no-determinism / full-chain-audit-fixes-20260921
