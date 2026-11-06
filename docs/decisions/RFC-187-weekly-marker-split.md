# RFC-187 周报幂等标记移出 `playbook_regime_log`

**日期**: 2026-11-06 · **状态**: 已实施 · **触发**: DEB-20261106 复核衍生扫描 `scripts/vix_coverage_impact_scan.py`

## 1. 问题

`playbook_regime_log` 的**真实环境行**在 A10 窗口（90 个交易日）只有 **55 天**（缺 36 天 = 40%），
最大空档 **2026-07-09 → 2026-08-17 = 39 自然日**。消费方（推送、`build_symbol_rule_cards`、
`condition_expectancy_report`）按"最近一条"兜底 ⇒ 会用陈旧 regime 当今天，累计约 74 个自然日。

## 2. 根因（不是"任务没跑"而已）

缺失的 36 天里 **16 天是周五**；窗口内 18 个周五只有 **2 天**有环境行，其中 **11 天与周报幂等标记同日**。

```sql
-- playbook_regime_log
date TEXT NOT NULL UNIQUE, regime TEXT NOT NULL, ...
```

周报任务（`app/playbook/pipeline_parts/tracking_flow.py`）用 `save_regime_log()`
把幂等标记写成 `date = 周报周五`、`regime = 'weekly_report'`、`vix = 0` 的占位行。
`save_regime_log` 是 `INSERT ... ON CONFLICT(date) DO UPDATE` ⇒ **标记行与当日环境行互斥，谁后写谁赢**。
周报在周五 16:00 ET 之后触发，环境行通常更早写入 ⇒ 被覆盖，VIX 归零，regime 变成 `weekly_report`。

（7 月那段 5 周空档是每日任务本身没跑，与主键冲突叠加。）

## 3. 改动

| 文件 | 改动 |
|---|---|
| `app/playbook/db.py` | 新增表 `playbook_weekly_markers(week_end PK, created_at)`；新增 `mark_weekly_report()`；`has_weekly_report_marker()` 改查新表 |
| `app/playbook/pipeline_parts/tracking_flow.py` | 周报完成标记改调 `db.mark_weekly_report(week_end)`，不再写环境表 |
| `scripts/migrate_rfc187_weekly_markers.py` | 一次性迁移：备份 → 建表 → 搬 12 行 → 删旧行 → 校验 |
| `tests/test_rfc187_weekly_marker_isolation.py` | 5 例：标记不占环境表日期、两种写入顺序下环境行都存活、幂等、`get_regime_log` 永不返回标记 |

## 4. 验证（2026-11-06 23:46–23:50 CST）

- 迁移：`旧标记残留=0 新表=12 环境行 62->62` → `RESULT OK`，备份 `data/ftseek_us.db.bak-rfc187-20261106-234846`
- 测试：`tests/test_rfc187_weekly_marker_isolation.py` **5 passed**
- 线上（`23:49:31 CST` 重启后）：
  - `has_weekly_report_marker('2026-10-30')` → True，`('2026-10-31')` → False（精确匹配不变）
  - `get_regime_log(3)` → `risk_off / cautious / cautious`，无 `weekly_report` 行
  - 今日（`2026-11-06`，周五）环境行存在：`risk_off, VIX 18.04`
  - 重启后 2 分钟内 ERROR/traceback 计数 0；在仓 3 笔未受影响

## 5. 为什么必须今天上线

周报在**周五 16:00 ET+** 触发，今天就是周五。若仍跑旧代码，今晚的标记会以 `date=2026-11-06`
写进环境表，吃掉今天的环境行 —— 这正是 16/18 个周五缺失的同一个机制。

## 6. 回滚

`git revert` 本提交 + `systemctl restart ftseek-us`。旧行为数据侧无需回滚：
迁移只是把标记行搬了位置，`playbook_weekly_markers` 保留全部 12 条历史标记；
若要恢复旧表标记行，从备份 `ftseek_us.db.bak-rfc187-20261106-234846` 取。

## 7. 未解决（明确不做）

- **历史 36 天空档不补**：`regime` 由 `MarketRegimeDetector` 当场计算，历史 VIX 不在库内，
  无法可信重建。本 RFC 只保证**今后不再因为标记冲突丢周五**。
- 7 月那段"每日任务没跑"是另一条链（`regime_classifier` 调用次数 = 0），需要单独议题。
- `trade_records.entry_vix` 缺数据写 0.0 的问题（48 笔 playbook_sim）不在本 RFC 范围；
  改它会影响历史分桶，需预注册。