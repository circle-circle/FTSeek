# RFC-166: 周日复盘报告口径标注 (三处读数打架 + 阶段双口径)

- 日期: 2026-10-25
- 类型: L1 可观测性修复 (零交易行为变更: 不改任何入场/出场判断/参数/门禁)
- 触发: 2026-10-25 用户贴出 W42 周日综合复盘, 问「哪些可以保持发扬 / 哪些需要优化调整 / 盈利亏损的标的·趋势·阈值规律」。复核 DB 时发现**同一份报告里三处读数互相打架**, 且阶段只按入场时段分桶, 导致读者无法判断哪个数是真的。

## 1. 三处口径缺陷 (实测)

| # | 报告呈现 | 实测真相 | 后果 |
|---|---|---|---|
| 1 | "净额 **+$582.11**"(交易总览) 与 "W42 **+$234.38** (22W/5L)"(OKR/Performance) 同页并列 | 两个数**都对, 但口径不同**: $582.11 = `trade_records` 出场日 10-18~24 全源 40 笔; $234.38 = `playbook_instructions.date`(入场日) ∧ executed ∧ outcome∈{win,loss} ∧ **EXT 旁路不入表** ⇒ 27 笔 | 读者只能二选一, 或判"报告自相矛盾" |
| 2 | "盘后 5 笔 +$289 ← 本周亮点" / "延长时段合计 12 笔 +$343" | 两数按 `entry_session` 分桶正确; 但本周净额的 **80% 是盘前出场的 4 笔 (+$466.00)** —— 两笔最大利润 (TSLA +$236.81 @06:12、ARM +$192.16 @04:07) 都是**盘中建仓、次日盘前兑现**, 被记进"盘中" | 低估延长时段退出链的价值, 且看不见"利润何时落袋" |
| 3 | 建议①"universe_expansion 持续监控, 不调整配额" | 该源**末次成交 2026-10-05**, DEB-20261023 已于 10-23 关闭 (现状=已关闭); IC −0.3747 (n=66) 是停供前历史 | 把已退役源写成下周活行动项 |

## 2. 修法 (最小变更, 全部在渲染面)

1. `app/review/stage_stats.py`
   - 新增 `rows_by_exit_stage(rows)`: 只把 `stage` 换成按 `exit_time` 的 ET 时钟判定, 净额/退出原因/方向一律不动 ⇒ 两臂必然对账。盘外 20:00–03:59 归未标注 (不臆造), 与入场口径同律。
   - `render_stage_section(..., caliber=None)`: 口径行可覆盖 (否则出场臂会印成"entry_session 优先"的入场口径)。
2. `scripts/weekly_trade_summary.py`
   - 表头加**口径互斥提示**: 同页 OKR 段的「Wnn: $X」是指令表口径(入场日/仅主链), 与本段出场日全源口径不可互比、不可相加。
   - 入场时段表之后并列**出场时段对照臂** (同批成交、同金额口径)。
3. `scripts/okr_status.py`
   - `PERFORMANCE` 周度段加口径标签 (来源 = `playbook_instructions.date`, 非 trade_records)。
   - `SIGNAL SOURCE ATTRIBUTION` 每行带 `[末次成交 YYYY-MM-DD]`, 超过 7 天标 `⚪已停供 N 天 ⇒ 读数仅历史`。
   - 该段原 `except: pass` 改显性打印 (静默消失会让周报漏掉"出血口在哪"整段)。
4. Hermes job `1e9c597b0262` prompt: 总览必须两行口径标签原样带入; 阶段必须双口径并列且合计自洽; 已停供源禁写成活行动项; 已退役退出桶 (TIME_EXIT/MFE_STALL_EXIT/BROKER_GHOST_CLOSE/PATIENT_EXIT_*) 标"仅历史"。

## 3. 验证 (实跑, 非推断)

```
venv/bin/python scripts/weekly_trade_summary.py --end-date 2026-10-24
  → 总计 40笔 / 82.5% / 净$582.11
  → 分阶段成交统计·按入场时段:     盘前 7 +$54.10 | 盘中 28 +$238.54 | 盘后 5 +$289.47
  → 分出场时段成交统计·按出场时刻: 盘前 4 +$466.00 | 盘中 34 +$94.07 | 盘后 2 +$22.04  (合计均 40笔/+$582.11)
  → 表头出现「⚠️ 口径互斥」

venv/bin/python scripts/okr_status.py
  → 周度口径: playbook_instructions.date(入场日) … 2026-W42: $234.38 (22W/5L)
  → universe_expansion: 6 trades … [末次成交 2026-10-05, ⚪已停供 20 天 ⇒ 读数仅历史]
```

测试: `tests/test_stage_stats.py` + `tests/test_weekly_trade_summary.py` 42 passed;
`tests/test_ruler_bps_and_g4_reconstruction.py` + `tests/test_okr_2026_09_scoring.py` 73 passed。
新增钉子: 出场臂只换标签不换金额 / 盘外归未标注 / caliber 覆盖生效 / **两臂合计恒等** / 渲染含双口径与互斥提示。

## 4. 边界与回滚

- 不涉及 `app/playbook/*`、任何门禁、参数、下单路径 ⇒ 无需重启 `ftseek-us` (报告脚本按 cron 每次新起进程)。
- 回滚: `git revert` 本提交; job prompt 回滚 = 恢复 `~/.hermes/cron/jobs.json.bak-20261025-w42caliber`。
- 未做: 未改「延长时段只看入场口径」以外任何统计定义; 未改 OKR/IC 判据; 未新增门。

## 5. 已知局限

- 出场时段对照臂的 n 天然偏小 (盘前 4 笔 < `MIN_N_FOR_READING=5` ⇒ 会印 ⚠️样本少), 这是正确行为不是缺陷。
- `weekly_pnl` 仍用指令表 `pnl` 快照 (历史原因: 键被 `okr_query.py` 消费), 本次只加口径标注, 未换真源 —— 换源会断 `okr_history.jsonl` 时序, 属另一次裁决。