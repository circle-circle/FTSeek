# RFC-127: ETH 出场 HELD → RTH 会话升级补腿 (影子先行)

- 日期: 2026-10-01
- 状态: **阶段1 已上线（变体语义）+ 2026-10-15 用户追认**（详见 §6 追认补记：实际部署语义为「归档释放移交盘中」而非 §3.2 预注册的「RTH MARKET 补腿」）
- 类型: L1 退出执行链修复候选 (先影子)
- 触发: 2026-10-01 US.ACN SHORT 23股 首例实弹卡死 (用户裁「按照建议执行」→ 本方案)
- 回滚: 阶段0 = revert 影子模块/接线/测试; 阶段1 = revert `pipeline.py::_release_eth_held_exit_for_rth_takeover` 调用点（单函数，2026-10-09 `80d8817ba` 携带上线）

## 1. 问题 (地面真值)

2026-10-01 09:25 ET, EXT premarket flatten 对 US.ACN SHORT 23股 (sim) 下达
LIMIT 23@215.62 (订单 8219159); 60 秒零成交被撤, durable intent
`7b149410fb274548b209659b822b9ba7` 进入 `exit_eth_partial_fill_held`
(ETH 窗内设计不追价, `exit_lifecycle.py` `if intent['session'] == 'ETH'` 分支)。

此后**所有自动出场路径被同一条 HELD 意图锁死**:
1. 新 RTH 意图同 close_key → `exit_session_conflict` (ledger.request 会话校验);
2. 其他 close_key → `exit_symbol_busy` (同 symbol 非BOOKED唯一索引);
3. fast loop 把该 symbol 加入 `blocked_exits` → 主退出循环 `continue`,
   **SL(223.16) 评估被整体跳过**; 22:14 现价 227.37 已越 SL, 每~10s 告警一次。

DEB-20260910 §356 早已预告该类别: "未决发送、查空、无价格、账本身份冲突可能
长期 HELD; 没有自动超时解锁或无证据重发。此为保守安全取舍, 仍需人工对账处置
流程。" 9/20 取证时该分支 0 次触发 — 今天是首例, 且证明了"人工流程未落地"的代价。

## 2. 根因

不是丢证据, 而是**会话语义过强**: ETH 窗内"不市价追价"是正确保守设计
(薄盘/滑点), 但该保守性被**永久化**到 RTH — 意图的 `session` 字段是出生属性,
不随市场时段演进。限价腿 TERMINAL 后, RTH 分支本有的"MARKET 补剩余量"机制
(与 RTH 语义完全同构) 对 ETH 意图不可达。

## 3. 方案

### 3.1 阶段0 — 影子 (本提交, record-only)

`app/playbook/eth_held_exit_shadow.py` + `_resume_playbook_exits` 接线:
- 触发条件: 意图为 ETH + `exit_eth_partial_fill_held` + `is_market_open()`
  (RTH, 含节假日/半日市 SSOT) + 剩余量 > 0;
- 每意图**只落一行** (`event_id = ETH-HELD-RTH:<sha256(intent_id)>`),
  记录 symbol/direction/数量/剩余/订单号/intent 年龄/建议动作
  (`escalate_to_rth_market_leg_for_remaining_qty`);
- 双层防护: emit 自吞错 (warning+栈) + 调用点保险丝; 恢复/告警行为零改动;
- 台账: `data/shadow/eth_held_exit_escalation.jsonl`; 已登记
  `tests/conftest.py` 生产数据缝 + `observe_nightly_supply.py` 段3 (今日有行 → P1)。

### 3.2 阶段1 — 行为变更 (预注册, 未实施)

在 `exit_lifecycle.resume` 的 ETH 分支把
`if intent['session'] == 'ETH': return held(...)` 放宽为:

```
ETH 且当前不在 RTH → 维持 HELD (原语义, 不追价)
ETH 且当前在 RTH 且剩余>0 → 走既有 RTH 补腿路径:
    reserve_leg(MARKET, remaining, deadline=30s) → send → query/result
```

安全性质 (设计约束, 阶段1实现时须逐条测试钉住):
1. **只补剩余量** — 已成交腿的 dealt_qty 求和后从 intent.quantity 扣除, 不重复平仓;
2. **durable ledger 单飞** — legs 的 reserve/send 闸防并发重复下单;
3. **只在 RTH 升级** — ETH 窗内零行为变化 (薄盘不市价追价的原设计保留);
4. **失败回退** — MARKET 腿失败仍回 HELD, 不丢证据, 不影响人工处置;
5. **可选宽限** — 若影子显示年龄分布离散, 可加 `escalation_grace_s`
    (默认0, 判据见§4), 但这是参数不是机制。

### 3.3 不采纳的替代

- **无脑解锁 symbol busy**: 会让所有 HELD 原因 (identity 冲突等) 都放行新单,
  破坏 durable ledger 的防重复下单核心; 只针对本病灶 (ETH 限价腿 TERMINAL 且
  RTH 开盘) 做会话升级, 语义最小。
- **立即对 ACN 手动平仓 (选项A)**: 用户本轮裁走 B (影子先行); 该仓在阶段0
  期间维持现状 (HELD + 告警), 如需立即止血仍可随时改走 A + 对账。

## 4. 预注册判据 (阶段1 启用门)

影子期须同时满足才提请用户裁决:
1. **样本**: ≥5 个交易日 且 ≥3 个独立 HELD 意图命中影子 (或单意图 SL 越线
   暴露 ≥$500, 取先到);
2. **可升级性**: 命中意图 100% 满足"RTH 开盘 + 剩余>0 + 无并发 ACTIVE 腿"
   (影子行可机器复核);
3. **无害性反证**: 无一例"升级窗口内价格回落使原 HELD 反而更优"超过
   MARKET 补腿成本 (以影行 ts 后 5min 中间价近似);
4. **告警噪声**: 升级后 fast loop 对该类 HELD 的重复告警应消失 (单意图
   ≤2 次告警), 不新增告警类别。

## 5. 验证 (阶段0)

- 契约测试 `tests/test_eth_held_exit_shadow.py`: 触发条件四象限
  (error/session/RTH/剩余量)、字段完整性、event_id 幂等、写失败自吞错
  (warning+栈)、pipeline 接线源级钉 (emit 在 resume_close 之后、自带保险丝)、
  observer 段3 P1。
- 活体验证: 服务重启后, 当前 ACN 意图应在首个 fast loop 落一行影子
  (`event_id` 以 `ETH-HELD-RTH:` 开头), 且退出行为/告警不变。

## 6. 遗留

- 手动对账处置流程 (DEB-20260910 §356) 仍未落地 — 独立议题, 不并入本 RFC;
- `exit_final_quantity_unknown` 等其他 HELD 原因不在本方案覆盖面 (需人工)。

## 7. 追认补记 (2026-10-15, 用户裁「追认」— 知情破例留痕)

**事实链**（2026-10-15 全链路审计 P1-3 对账发现，主会话逐语句核实）:
1. 阶段1 语义实际于 **2026-10-09** 由 `80d8817ba`（pipeline 拆分批, commit message
   自述「零行为变更」）携带上线: `app/playbook/pipeline.py:3324`
   `_release_eth_held_exit_for_rth_takeover` — ETH 窗 HELD 零成交 intent 在 RTH 开盘
   自动 `release_exit_intent(reason="rth_intraday_takeover")` 归档释放 + 解除
   blocked + 恢复 SL/BE/trailing/EOD 评估。无开关、无 DEB/RFC 授权记录（docs 全域
   零命中）、§4 预注册判据从未满足（影子面仅 2 行）。
2. **部署语义 ≠ §3.2 预注册设计**: 预注册是「ETH 且在 RTH 且剩余>0 → reserve_leg
   MARKET 补腿」（会下新单）; 实际部署是「归档释放移交盘中策略」（零新订单,
   仓由常规 RTH 退出规则接管）——**更保守**, 但属未经裁决的机制变更。
3. 副作用: 移交成功时 `continue` 跳过影子发射 ⇒ 阶段0 影子面缩为「移交失败」
   子集（选择偏差, 10-09 后零新增）, §4 判据面永久失去原测量意义。

**裁决（2026-10-15 用户）**: **追认**——行为已在生产运行 6 天（10-09 起）且方向正确
（解决的正是 ACN 类卡死; 10-08 US.PEP 39 股 HELD 后再无同类卡死告警）; 按宪章
「用户裁决与预注册判据冲突须记录知情破例」落档: §4 判据门被绕过一事知情接受,
不复制为先例（后续行为变更仍须影子先行+预注册）。

**判据面重写（随追认生效）**: 影子 `eth_held_exit_shadow` 重新定位为
「**移交失败**监测面」——此后每行影子事件 = 一次 release 被拒/异常,
即「该移交而未移交」的残余风险; 出现新行即 P1 处置（observe_nightly_supply
段3 已是该语义）。§4 原判据（样本门/可升级性/无害性反证/告警噪声）随阶段1
已上线而失效, 留档不再使用。
