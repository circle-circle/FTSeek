---
title: 2026-11-10 美股交易日全链复核：仅报表幻影成交需修
status: fixed_deployed
module: playbook/review
related_code: app/review/daily_review.py, app/playbook/db.py, app/core/obsidian_writer.py
tags: [daily-review, playbook, execution-chain, phantom-execution, early-tight]
updated: 2026-11-11
---

# DEB-20261111 — 2026-11-10 美股交易日全链复核

- 复核时点：**2026-11-10 18:48 ET（2026-11-11 08:48 CST 前）**；盘后尚未结束，本文不是最终日终结算。
- 范围：DA/EXT 指令→入场→成交→退出→账本→日报；只读 DB/账本/日志/影子产物。
- 结论：**一项 P1 测量修复值得做；无新增交易规则修复依据，无 P0 链路故障。**
- 本轮零代码/配置/DB 写、零重启、零券商调用。

## 1. 分 cohort 经济快照（截至复核时点）

| 集合 | 新入场 | 已平仓 | 费后净额 | 未平仓 |
|---|---:|---:|---:|---:|
| 11-10 新 DA | 14 | 11 | **+$32.59** | 3 |
| 11-10 新 EXT | 8 | 6 | **+$68.74** | 2 |
| 旧 cohort 当日出场 | — | 2 | **+$85.86** | 另有 2 仓仍持有 |
| 合计 | 22 | 19 | **+$187.19** | 7 |

- 新 DA 11 笔已平仓：毛 **+$78.19**、估算费 **-$45.60**、净 **+$32.59**，
  费毛比 **58.3%**、按入场名义额净 **+7.81bps**；n=11，不做策略裁决。
- 16:00 ET 常规收盘时，新 DA 三个未平仓 PDD/HON/UBER 毛 MTM **-$76.29**；
  与已实现净额分栏，不合并进 A9/KR。旧 BA/GOOG 也仍是独立旧 cohort。
- 上述总额不含 7 个未平仓的最终结果；盘后后续成交必须另带 `as_of` 续结。

## 2. 当日链路验收

- **开盘供给通过**：09:01 ET 日志确认 `21/21` DA 候选已完整落库并跳过全量生成（RFC-196）；
  全交易时段未再出现 `trade_date` NameError。
- **09:37 首执不是新故障**：既有代码明确开盘 5 分钟不执行，调度约 3 分钟一轮；
  09:31/09:34 均按设计跳过，09:37 开始 `auto_execute`。11-09 的长步崩溃没有复发。
- **订单/账本收口正常**：无重复 `decision_id`、无同标的重复未平仓；当日 20 个 exit intent
  全部 `BOOKED`，创建→更新最长约 **58.8s**。MRK 的 `FILLED_UNBOOKED` 警告在 **17.2s** 内
  转为 `BOOKED`，不是卡单。最新 PositionSync 为账实一致。
- **日志健康**：所核主服务日志无 ERROR/CRITICAL/Traceback；09:37 有一次 6.122s GC 停顿，
  单日单点不足以另开性能变更。

## 3. 唯一明确修复：Obsidian 把已有持仓跳过计成成交

### 3.1 实证

- 严格 `decision_id` 归因和 `data/reviews/REVIEW_2026-11-10.md` 均为 **20 计划 / 14 成交 / 70%**。
- `obsidian_vault/daily/postmarket/2026-11-10_us_postmarket.md` 却写 **已成交 15**。
- 差异是 GOOG：当日新 DA 指令 `executed=1`，但
  `execution_state=SKIPPED_EXISTING_POSITION`、`outcome=already_held`、`execution_price=0`，
  且没有对应新 `trade_records`；这是防重复入场闩，不是成交。

### 3.2 根因与最小修法和边界

- `app/review/daily_review.py` 的 `detailed_rows` 查询只取 `executed/execution_state`，
  调 `app.playbook.db.is_phantom_execution()` 时却漏传其判据需要的 `outcome/execution_price`，
  因缺列回退为“非幻影”，最终进入 `EXECUTED`。
- **修法**：查询补 `outcome, execution_price`；Obsidian 增加 `已有持仓跳过` 分类并从“其他闸门”排除；
  加 GOOG 同型契约测试，并断言 `direction_provenance.executed == 严格 decision_id executed_count`。
- **禁止修法**：不得把原始 `executed=1` 闩改成 0，也不得重试该指令；它是防覆盖/防重复语义。
  `app/review/daily_review.py` 当前已有其他在途改动，实施时只做上述小补丁，不覆盖 WIP。

## 4. 不做交易参数调整

- **BABA 不推翻 early-tight NO_DEPLOY**：入场 104.86，原 SL 100.13；持有约 5.1 分钟按既有规则
  紧至 103.6775，最终 103.455 出场，费后 **-$54.86**。既有 penetration 采集已记录其 2 分钟恢复、
  期间不触原硬 SL，说明它是有效验收样本，不是漏采或异常执行。
- 11-05 部署后同类 early-tight 事件目前仅 **3 笔 / 3 个 ET 日**，虽然 3 个 outcome 均为 2 分钟恢复，
  仍远低于预注册 **n≥30 且 ≥10 日**，且恢复率不是全因费后经济门；继续攒样，不改 120s/阈值。
- 费毛比 58.3% 与低 MFE 水下持夜值得在既有 KR/台账继续观察；单日 n<30 不支持抬票面、
  砍交易频率、放宽保本/跟踪、关闭 chase cap 或强平隔夜仓。

## 5. 对前一份跨模型退出链 DEB 的重新定级

- 当日全部成交均为 Playbook，未给 legacy C1/C5 增加任何线上实害证据，**不与本批捆绑修**。
- C10 共享 ExitLedger 当日全部 intent 正常收口，无初始化/索引故障，**不提升优先级**。
- 因而本交易日复盘后的行动队列应从“legacy 退出修复”改为：**先修 Obsidian 幻影成交统计；
  交易规则与 legacy 参数均暂缓**。

## 6. 下一步

1. 待用户批准后，仅实施 §3 的测量修复与回归测试，再重渲染 11-10 Obsidian 核对 **14/20**。
2. 盘后以新 `as_of` 结算剩余 EXT/DA 仓，不回写本文 18:48 ET 历史快照。
3. early-tight、费用与持夜只进入既有预注册样本；无新门、无新影子、无交易参数变更。

## 7. 2026-11-11 实施回执：幻影成交统计已修复并部署

- **代码**：`daily_review.detailed_rows` 补 `outcome/execution_price`；
  `direction_provenance` 新增 `existing_position_skipped` 且不再计入其他闸门；
  Obsidian 在该计数 >0 时单列“已有持仓跳过”。原始 `executed=1` 闩与下单/退出逻辑未改。
- **测试**：新增“1 笔真实成交 + 1 笔 GOOG already_held 幻影行”契约例，
  断言严格成交数与 provenance 一致；渲染例覆盖新增分类。聚焦 **7 passed**，
  合并相邻复盘测试 **21 passed**，`git diff --check` 通过。
- **生产只读复算**：20 计划 / **14 成交 / 70%** / 3 入场确认放弃 /
  **1 已有持仓跳过 / 2 其他闸门**；GOOG `final_state=SKIPPED_EXISTING_POSITION`。
- **日报**：调用既有 `refresh_postmarket('2026-11-10')`，无推送；Obsidian 已显示上述 14/20 与
  已有持仓跳过 1。刷新时点全量快照为 total=24、closed=19、net=+$187.19。
- **部署**：重启前 7 个未平仓、0 个在途出场，其他 app/scripts 修改均早于当前进程启动；
  `systemctl restart ftseek-us` 后启动冒烟 **7/7**，服务 active，PID=2557980，7 个未平仓保持不变。
- **边界**：本批仅修测量与展示，不宣称策略 edge 改善，不修改任何交易参数、数据库 schema 或交易记录。
