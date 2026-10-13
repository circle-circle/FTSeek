# 2026-11-01 美国冬令时调度前置审计

## 结论

2026-11-01 02:00 ET 切入 EST 后, CST 相对 ET 右移 1 小时。2026-10-11 已将本批显式 ET 锚定任务整体右移 1 小时, 保证 EST 季 ET 语义不变; 11/01 前的 EDT 间歇期会晚 1 小时执行。

## 变更

| 任务 | ET 锚点 | 原 CST | 新 CST |
|---|---:|---:|---:|
| Hermes 盘后综合复盘 | 16:00 | 04:00 周二-周六 | 05:00 周二-周六 |
| ETH 阶段日报 | 20:30 | 08:30 周二-周六 | 09:30 周二-周六 |
| ETH 盘前计划 | 20:30 T-1 | 08:30 周二-周六 | 09:30 周二-周六 |
| ETH 盘后计划 | 15:45 T-0 | 03:45 周二-周六 | 04:45 周二-周六 |
| ETH 盘前刷新 1 | 04:00 | 16:00 周一-周五 | 17:00 周一-周五 |
| ETH 盘前刷新 2 | 07:00 | 19:00 周一-周五 | 20:00 周一-周五 |
| ETH 盘后刷新 | 17:00 | 05:00 周二-周六 | 06:00 周二-周六 |
| ETH premarket reconcile | 21:02 | 09:02 周二-周六 | 10:02 周二-周六 |
| ETH afterhours reconcile | 21:04 | 09:04 周二-周六 | 10:04 周二-周六 |
| ETH graduation check | 21:08 | 09:08 周二-周六 | 10:08 周二-周六 |
| ETH volume shadow premarket | 10:00 | 22:00 周一-周五 | 23:00 周一-周五 |
| ETH maker shadow premarket | 10:05 | 22:05 周一-周五 | 23:05 周一-周五 |
| ETH volume shadow afterhours | 21:20 T-1 | 09:20 周二-周六 | 10:20 周二-周六 |
| ETH maker shadow afterhours | 21:25 T-1 | 09:25 周二-周六 | 10:25 周二-周六 |
| Overnight known-event flag | 20:45 T-1 | 08:45 周二-周六 | 09:45 周二-周六 |

## 备份与回滚

- crontab 备份: `/opt/ftseek/backups/crontab.20261011_dst_exit_boundary.bak`
- Hermes jobs 备份: `/opt/ftseek/backups/hermes_jobs.20261011_dst.bak`
- crontab 回滚: `crontab /opt/ftseek/backups/crontab.20261011_dst_exit_boundary.bak`
- Hermes 回滚: 恢复备份文件后执行 `hermes gateway restart`

## 验证

- `crontab -l` 已确认 15 条新 CST 时刻。
- Hermes job `7a40dd7c9e94` 为 `0 5 * * 2-6`, `next_run_at=2026-10-13T05:00:00+08:00`。
- Hermes gateway 已平滑重启并处于 running。
- `ftseek-us`、`crond` 均 active; `startup_smoke_check.py --wait 120` 全部通过。
