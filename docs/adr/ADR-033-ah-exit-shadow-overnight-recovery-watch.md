# ADR-033: 水下隔夜仓盘后出场影子监控 (record-only)

**日期**: 2026-08-27
**状态**: 已采纳 (用户动议两次澄清后批准: "可以先实现，后面决策用不用可以设置开关，保持监控着…如果达到了也可以验证整个链路")
**关联**: scripts/ah_exit_replay.py (回放裁决) / scripts/ah_exit_shadow.py (影子监控) / ADR-031 (同族影子方法论)

## 背景与动议演化

用户动议 (2026-08-27): "如果有隔夜持股，是否可以基于夜盘或者盘后数据进行监控，如果有可以盈利的机会可以出仓"。
同日两次澄清后意图修正为: **"针对水下隔夜仓在盘后和夜盘能够延续，留足机会和时间净盈利乃至提前获得TP"** —
持有修复优先，非见好就收。初始"盘后浮盈落袋"方案 (green_take) 被回放数据否决后废弃。

## 回放裁决 (ah_exit_replay.py, 过夜仓 n=12, TP/SL 元数据齐全非 ghost)

| 臂 | 结果 | 裁决 |
|---|---|---|
| 静态 TP/SL 挂盘后 | 0/12 触发 (盘中已触发完, 死单) | 排除 |
| green_take 浮盈≥0.6%落袋 | -$113 | 否决 (与用户真实意图亦相反) |
| red_cut_1pct 深红≥1%收盘割 | +$251 / p=0.219 | 唯一候选 (**样本内推导警告**) |
| 水下入夜 11 夜盘后路径 | 触及 entry 18% (全横盘蹭线) / 净盈利垫 0 / TP 0 | 盘后是惯性时段非修复时段, 修复引擎=次日 RTH |

数据前置: Futu `request_history_kline(..., extended_time=True)` 可取盘后 (16:00-20:00 ET,
241 根/日)+盘前历史 1 分 K, 深度≥1 年。旧链路 0 根盘后 bar 系未传该参数, 非数据不可得。

## 决策

启用 record-only 影子监控 `playbook.ah_exit_shadow_enabled: true`:

- **观测臂** touch_entry / touch_be / touch_tp: 每夜独立反事实 ("触及即锁 vs 实际延续到 exit"),
  回答"修复了要不要抓"与"事件夜提前 TP 值多少";
- **决策臂** red_cut_1pct (唯一): 触发时构造 would_be_order 载荷 (LIMIT + fill_outside_rth +
  DAY, manual_order_service.close_position 契约) 验证链路, **不发单**;
- 时序: AFTER_HOURS 19:50+ 实时预评 (live_preliminary) → OVERNIGHT 20:05+ 完结终评 (final);
- 幂等 (symbol, entry_time[:19], night), 无活早退不空耗 Futu 连接。

**灰度判据** (DEB-20260805 预注册格式): n≥15 配对 + 符号置换双侧 p<0.05 + 净差>0 才议灰度。
达成前零执行路径改动、不碰订单。

## 验证

首夜 (2026-08-26 夜) ORCL/UBER 双触发: 载荷构造正确, 结算 delta=+9.79 精确匹配手工核算;
服务重启生效, 调度钩子异常隔离, 无 ERROR。

## 回滚

`playbook.ah_exit_shadow_enabled: false` + 重启。零数据迁移, 观测行留存 jsonl 不影响任何路径。
