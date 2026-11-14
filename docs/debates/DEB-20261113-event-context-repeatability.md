# DEB-20261113 · EventContext 翻向重复控制与不可归因结论

> 状态：**重复控制完成；不晋级、不换模型、不新增 Gate**。
> 目的：验证首夜 NVDA `SKIP→BUY` 与 CSCO `BUY→SHORT` 是否由 EventContext 稳定驱动。
> 输入：2026-11-13 raw checkpoint + `f23c760ecea071d25112`，control/treatment 各重复3次。

## 1. 实测

| 标的 | control 方向分布（3次） | treatment 方向分布（3次） | 结论 |
|---|---|---|---|
| NVDA | SKIP ×3 | **SKIP ×2 + SELL_SHORT ×1，BUY ×0** | 首夜 BUY 未复现，不能称稳定事件逻辑 |
| CSCO | SELL_SHORT ×2 + BUY ×1 | SELL_SHORT ×2 + BUY ×1 | 分布相同，不能归因 EventContext |

证据目录：

- `data/shadow/event_intelligence/nvda_repeat3_v1/summary.json`
- `data/shadow/event_intelligence/csco_repeat3_v1/summary.json`

两个系列的重复卡片均为 `DEGRADED`，原因包括模型引用了不在实际注入 facts 中的 event_id。
因此 `valid_cards=0`，本次重复结果**不是质量通过**，不得用来证明任何优化有效。

## 2. 对首夜结论的修正

首夜 32 张卡的结构、grounding、研究完整率与价格几何均通过，只能说明**单次生产注入没有破坏指令结构**。
它不能证明：

- NVDA 的 BUY 是稳定或正确的；
- CSCO 的 SHORT 是 EventContext 带来的；
- 其它 `event_role=NONE` 的翻向与 EventContext 有关。

NVDA 的真实 Gate 已因 bearish 动量+BUY 拦截，因此首夜 BUY 没有形成真实订单。
CSCO 的 SHORT 在 control 与 treatment 中同占主导，不能再作为 EventContext 改善案例。

## 3. 后续判据（不追溯改首夜数据）

1. 对任何 `control != treatment` 的方向变化，先做重复控制，不直接归因。
2. 候选稳定事件逻辑需 ≥10 次重复且稳定方向占比 ≥80%，且 `event_role != NONE`、
   引用 ID 全部可回指；不足只写“不可归因”，不写“有效”。
3. 出现 ungrounded event ID 时，整批只作异常分析，不参与收益或质量晋级。
4. 若仍无法稳定复现，保留现状但不转常态；`DA_EVENT_CONTEXT_INJECT=0` 可一行回滚。
5. 继续积累多日期、不同事件类型样本，禁止用单日/单股/单次结果替代日聚类与 MDE。

## 4. 必须补的观测与约束

- 对 unsupported indicator（如 KDJ）、估值口径冲突、event_role/event_id 失配做记录级告警；
- 不把 `event_role=PRIMARY` 的模型自述当成真实使用证据，必须核对实际注入 facts；
- 保留原始 Prompt、注入 Hash、卡片与挑战者反证，防止只读结论不看证据。
