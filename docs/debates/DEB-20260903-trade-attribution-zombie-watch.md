# DEB-20260903-trade-attribution-zombie-watch — invariant 日日看守已退役死表的僵尸观察

状态: **已裁决+实施** (2026-09-03 凌晨全链路基线审计产出, 用户裁"根据最新的策略和状态选择"立案; 同日用户裁"按建议执行"落地方案A, 见 §七)

## 一、缘起

9/3 凌晨基线审计 ("确保尺子是精准的") 发现: daily_invariant_check.py 的 caliber_diff 段
自 7 月末起每日输出 `status: "not_judgeable_attribution_stalled"`, 其注释承诺的
"差值观察 2 周后定收敛阈值" 悬置 7 周未决。追查后确认这不是待修故障, 而是已裁决退役
(2026-08-27 退役⑦) 的遗留守望 — 守望对象 trade_attribution 表自 7/14 起再无写入,
且**永远不会**再有写入。一把尺子在看一个已死之物, 黄灯永悬。

## 二、证据链

| # | 证据 | 出处 |
|---|------|------|
| 1 | 表止于 7/14: 末三日行数 7/14→4, 7/13→4, 7/9→1 (本就稀疏) | `sqlite3 data/ftseek_us.db "SELECT trade_date,count(*) FROM trade_attribution GROUP BY 1 ORDER BY 1 DESC LIMIT 3"` |
| 2 | 根因已裁: "trade_attribution 表自 2026-07-15 策略隔离后无新写入 (末行 07-14, 仅主线成交侧写入)" | app/core/review_loop.py:150 退役⑦ 注释 (2026-08-27) |
| 3 | 7/15 变更佐证: ADR-015 "close-side source_type 按持仓 owner 派生, 消除 playbook 仓被主线路径平仓时的归因误标" | git c6ec3d46 (2026-07-15) |
| 4 | invariant 自知停摆: 无行时不输出 gap 数字 "否则三口径对账会长期拿着恒定假差值自我安慰" (D1 实锤注释) | scripts/daily_invariant_check.py caliber_diff 段 |
| 5 | 每日悬置输出: `attribution_last_trade_date: "2026-07-14"` + note"观察 2 周后定收敛阈值" | data/invariant_check_latest.json (9/2) |
| 6 | 消费者① review_loop 归因规则: 8/27 退役⑦ 停用 (避免 3 天连发自动升 P0 的结构性误报), 恢复条件="playbook 链重接归因写入后再启用" | app/core/review_loop.py:145-162 |
| 7 | 消费者② weekly_proposer: 配置门硬关停 (`llm_pushes.weekly_proposer_enabled=false` → RuntimeError) | app/core/scheduler_triggers.py:818-819 |
| 8 | 消费者③ SystemicIssueAnalyzer 周五 16:00 ET 聚合: 读死表→total=0→不推送, 空转无假数据 (log 0 条"周度系统性问题报告") | app/core/scheduler_triggers.py:771 区块 |
| 9 | 消费者④ position_monitor / order_executor: trace_id 历史回读 (仅读旧行, 无害) | app/pipeline/position_monitor.py:1573, order_executor.py:3825 |
| 10 | 现代归因载体已在位: trade_records.attribution metadata 字段健康 56/56 + f1_trend~f5_lifecycle 5 列 + owner_strategy; 方向归因另轨走 signal_snapshots T+1 报告 (daily_direction_attribution.py, 8/29 竞态修复后锚定回填前沿) | data/invariant_check_latest.json field_health; 表结构 |
| 11 | 测试面: 无源级测试断言 caliber_diff / not_judgeable 状态 (仅陈旧 pycache 命中), 改动零存量测试风险, 按房例补钉住测试 | grep tests/ |

## 三、方案对比

| 方案 | 内容 | 判断 |
|------|------|------|
| **A (荐)** | caliber_diff 的 attribution 分支改**显式退役终态**: 停止每日死表查询, status/note 改为"retired (8/27 退役⑦, 表止于 2026-07-14, 现代载体=trade_records.attribution+f1~f5)" — 引用本 DEB; 同族项 SystemicIssueAnalyzer 周五空转触发一并退役 (证据⑧: 输出从未推送过, 周度汇总职能已由 shadow_board/周日综合复盘承担) | 消灭僵尸黄灯, 尺子只看活物; 改判据先落 DEB ✓ |
| B | 复活写入方 (playbook 链重接归因写入, 退役⑦ 的"恢复条件") | 重造冗余写入路径 — 现代载体 (证据⑩) 已全覆盖; 违反"不为假设需求设计"; 且 56/56 字段健康说明现行链路无归因缺口 |
| C | 不动 (not_judgeable 无害论) | 注释"观察 2 周后定"永不成真 = 存量黄灯, 与"红/黄=当次分诊"精神相悖, 且每轮审计都要重新分诊一次本项 (本轮已花掉的分诊成本会无限复发) |

## 四、方案A 实施细节 (待裁后执行)

1. scripts/daily_invariant_check.py caliber_diff 段: 表行数为 0 分支改输出
   `{"status": "retired_attribution_20260827", "note": "表止于 2026-07-14 (ADR-015 策略隔离, 8/27 退役⑦); 现代载体 trade_records.attribution+f1~f5; 见 DEB-20260903"}`,
   删除对 trade_attribution 的 COUNT/SUM/MAX 查询 (死表零行, 查询本身也是每日空转)。
2. app/core/scheduler_triggers.py Task #30 区块 (SystemicIssueAnalyzer 周五聚合):
   显式禁用 + 注释引用本 DEB (输出从未存在, 无行为变更)。
3. 补测试: 钉住 ①caliber_diff 退役终态语义 ②Task#30 禁用声明 (防再上线绕过证据审查, 同 G6 门测试造型)。
4. 生效: 次日 05:05 invariant 首跑 (独立 cron 脚本, 运行中服务零接触)。

## 五、回滚

revert 对应提交即可; 表与数据零改动 (纯读侧退役), 无数据风险。

## 六、未做与诚实边界

- position_monitor / order_executor 的历史 trace_id 回读路径保留 (读旧行无害, 删了反伤历史反查)。
- 方向归因尺 (signal_snapshots T+1 报告) 不受本 DEB 影响 — 它是活链路, 与本表无依赖关系。
- scripts/phase_b_status.py 与 archive/ 旧脚本仍含 trade_attribution 历史读取
  (不在 cron/jobs.json 上, 属档案性读取) — 未在本次退役范围内。

## 七、执行记录 (2026-09-03, 用户裁"按建议执行")

- **daily_invariant_check.py**: J2 caliber_diff 三条死表查询 (COUNT/SUM/MAX) 移除,
  保留 trade_records_net (SSOT 净额快照, 今值 279.74); 输出终态
  `status: "retired_attribution_20260827"` + note 引用本 DEB;
  `not_judgeable_attribution_stalled` 分支与"观察 2 周定阈值"悬置注释一并删除。
- **scheduler_triggers.py**: 模块级 `_TASK30_SYSTEMIC_ANALYZER_ENABLED = False`
  kill switch (恢复条件同退役⑦: playbook 链重接归因写入后置 True) + Task #30 try 块
  首句 RuntimeError 门 (weekly_proposer 同款造型, except logger.debug 优雅吞掉,
  周五主流程零影响)。该文件属运行中服务 — 盘后编辑安全, 下次重启生效。
- **钉死测试** tests/test_attribution_watch_retirement.py 6 例: 退役终态语义
  (死表在/不在均输出 retired 终态, 死表残留数据不得泄入对账字段)、整表 DROP 不破报告、
  源契约 (无 "FROM trade_attribution"、DEB 引用、kill switch 显式 False)。
- **验证**: 新测试 6/6 passed; 相邻套件 (invariant D4/I10 + scheduler_triggers 相关
  R8/patient_exit/p0_audit) 91 passed; 真库手动跑 invariant — caliber_diff 即新终态
  (rc=1 仅 I4 AVGO 已知 9/5 自清边界行, 与本次无关)。
- **生效面**: invariant 侧立即生效 (手动跑已写新态入 invariant_check_latest.json,
  次日 05:05 cron 首跑延续); Task #30 侧待服务下次重启生效 (下次触发窗口为
  9/4 周五 16:00+ ET, gate 在场即拦, 即使服务未重启旧代码也只是对空表空转 — 双向零风险)。
- **另一 agent 检查** (用户令): 本会话期间 Codex 零活动 — 988b4701 (9/3 02:37) 后无新提交,
  本 DEB 无他人改动, 工作区仅 bench/ 未跟踪 (9/2 下午所留)。
