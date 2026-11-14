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

## 5. 第 2 轮只读复查（2026-11-14 11:30–11:40 CST，Codex）

> 只读复查：未改代码/配置/cron，未重启。本节只追加证据与修复方向，不改 §1–4 结论。
> 10:28 批次已修"按 26h 年龄接受昨日简报"（同 ET 日守卫已生效）；本轮发现的是其残留缺口。

### 5.1 新发现：过期契约缺口（中危 / 自然路径低触发）

- `app/event_stream/da_prompt_injection.py:244–325` `load_event_context` 校验
  status / record_only / validation_errors / brief_id / as_of 同 ET 日 / 年龄 ≤26h / 未来容忍，
  但**从不执行 `now <= valid_until`**。`valid_until_et()`（`:114–121`）只算出"当前 ET 日 16:00"
  回填到返回结构，不参与准入判定。
- 与 RFC-208 冲突：§4.1 要求"无过期/未校验简报注入"，§5 停止条件含"注入失效/过期简报"。
  当前实现下，同一 ET 日 16:00 收盘后、26h 年龄内的简报仍会被注入。

### 5.2 复现探针（只读）

- `now=2026-11-13 22:00 ET` + 生产 latest（status=OK、record_only=True、
  as_of=2026-11-13T12:20:31.317452+00:00、brief_id=`f23c760ecea071d25112`、doc 内 valid_until=None）
  → `injected=True, reason=injected, valid_until=2026-11-13T16:00:00-05:00`。
- 即：返回结构自报 16:00 过期，实际 22:00 仍注入。

### 5.3 触发面与现有护栏

- 自然路径暂安全：Hermes `DA main carrier (ET-pinned)` `0 8 * * 1-5` 启用（旧 EST/EDT carrier 已停用），
  EventContext cron `30 6 * * 1-5` ET；DA 主批次在盘中，同 ET 日守卫与年龄闸通常先生效。
- 风险面：手动重跑 / 盘后补跑 / 未来调度改动。现有测试 `NOW=14:00 UTC`（09:00 ET），
  无"同 ET 日 16:00 后"用例，缺口无回归保护。

### 5.4 修复方向（待裁，本轮不改）

1. 在 `load_event_context` 增加 `now <= valid_until` 硬校验（fail-closed，reason=`brief_expired`）；或
2. 若有意只作提示，则在 RFC-208 显式把 `valid_until` 重定义为 advisory 并同步改写 §4.1/§5 口径。
3. 无论哪条，补"同 ET 日 16:00 后"测试用例；一次一变量、影子先行。

### 5.5 经典归类与它山之石

- 经典问题：事件时间（event-time）vs 处理时间、有效期区间（validity interval）未进入判定。
- 参考：Flink event-time/watermarks（https://flink.apache.org/2021/01/11/event-time-watermarks/）、
  Google Cloud streaming pipelines（https://cloud.google.com/blog/products/data-analytics/streaming-pipelines）。
  成熟做法是把"数据有效期"作为一等公民进入准入判定，而不是只展示。

### 5.6 本轮验证与未验证

- `pytest -q tests/test_position_sync_queue_shadow.py tests/test_da_prompt_injection.py` → **24 passed**
  （现有用例全绿，恰恰说明缺 16:00 后用例）。探针为只读调用，未写任何产物。
- 未验证：新 06:30 ET 首轮自然运行、盘后手动重跑的真实触发、任何收益/质量晋级结论。

## 6. 修复轮：过期硬校验已落地（2026-11-14 14:25 CST，Codex，用户裁「直接修复」）

> 取 §5.4 方向 1（硬校验），不改 RFC-208 口径：§4.1「无过期/未校验简报注入」与 §5 停止条件
> 本就是已批准契约，本轮是补齐实现缺陷，不是新行为。advisory 重定义（方向 2）未采用。

- 实现：`app/event_stream/da_prompt_injection.py` `load_event_context` 在年龄闸后新增
  `now > valid_until → brief_expired`（fail-closed，返回结构与既有 `_load_error` 一致，
  `valid_until` 照常回填、失败 identity 哈希照常产出）；模块 docstring 安全契约补 expired。
- 测试 +2（`tests/test_da_prompt_injection.py`）：同 ET 日 22:00 ET → `brief_expired` 不注入；
  同 ET 日 16:00 整边界 → 仍注入（`now <= valid_until`）。套件 **23 passed**。
- 生产探针复跑：22:00 ET → `injected=False, reason=brief_expired`；09:00 ET → `injected=True` 不变。
  自然路径（Hermes 08:00 ET / EventContext 06:30 ET）不受影响；盘后手动重跑现在 fail-closed。
- 消费方兼容：`da_event_preexecute_shadow.py:361` 对 reason 为自由字符串透传，无枚举校验。
- 部署：随本批 `systemctl restart ftseek-us`（MainPID=2946809）生效，启动冒烟 7/7。
- 未验证：新 06:30 ET 首轮自然运行；任何收益/质量晋级结论（§3 判据不变）。
