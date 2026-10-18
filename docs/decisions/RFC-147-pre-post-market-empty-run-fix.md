# RFC-147 盘前/盘后链路空跑治理 (2026-10-18)

**状态**: 已实施 (安全修复 + 运维节流)
**触发**: 用户令核查盘前/盘后链路 "是否值得优化...避免空跑", 复核后令按建议落地。
**范围**: 只做工程链路修复与采频节流; 不改策略方向、评分阈值、止损/出场规则、仓位参数。

## 1. 证据 (生产日志/DB, 10/14–10/16)

| 现象 | 实测 |
|---|---|
| 盘前推送早于最终生成 | 10/16 08:47 推送 13 条并写幂等; 09:01–09:07 才生成; 生成后 AEM/TEAM/NBIS/CRM 终态变 SKIP, 不会补推 |
| 09:00 全市场生成不新增可执行方向 | 近 9 个交易日所有 `BUY/SELL_SHORT` 的 `source` 均为 `deep_analysis`; app 内生成新增可执行 0 条 |
| 生成窗口成本 | 10/16 66 候选 / 5m05s / 64 次 Finnhub 429 等待 / 98 条 SEC insider / 5 次实盘解锁 |
| 延长时段退出 cron 空跑 | 10/16 1,465 次运行中 895 次窗外; 570 次 session 运行全部无持仓; 10/15 有仓时 570 次 session 运行 |
| EventStream 零产出 | 10/16 1,168 个 source-run 中 809 个 events=0 (69%); FR 源长期 0 |
| 盘后主循环阻塞 | 10/16 AFTER_HOURS 单轮 658s, 日K刷新扫描 400 文件 |

## 2. 本批落地

1. **盘前推送顺序门** (`app/playbook/pipeline_parts/generation_flow.py`)
   - 09:00 前不推; 当日 `playbook_generate_state` 完成才推; 09:15 ET 兜底放行 (WARNING)。
   - 保留日期幂等; 生成异常时仍能保证开盘前有推送。
2. **实盘单例并发锁** (`app/web_dashboard/manual_order_service.py`)
   - `get_manual_order_service()` 双检锁, 防止 5 线程筛选池并发建多个实例、重复解锁实盘。
3. **ETH 退出无仓跳过 + 窗口闸**
   - `scripts/extended_hours_exit_cron.py`: session 内先按 collector 同口径探持仓, 无仓跳过子进程; 探针失败 fail-open。
   - 新增 `scripts/extended_hours_exit_cron_gate.sh`: 工作日 ET 04:00–20:05 每分钟, 其余时段/周末 5 分钟; 保留孤儿单/close_key 守卫。
   - 结果 JSON 增加 `collector` / `open_positions_n` 字段供指标消费。
4. **EventStream 分时采频** (`scripts/event_stream_ingest.py`)
   - `--cadence`: 交易日 07:30–09:35 ET 5 分钟; 交易日 04:00–20:00 15 分钟; 其余/周末 20 分钟。
   - 仅在全部源 OK 时写 `data/.event_stream_ingest_state.json`; 失败下个 5 分钟 tick 重试。`--force` 可绕过。
5. **日K刷新移出盘后主循环**
   - `scripts/refresh_daily_bars.py` 新增 `--cron` + ET 16:00 窗口守卫; 执行级日门/失败重试语义不变。
   - `scheduler_core.py` AFTER_HOURS 调用块移除; crontab 双季 04:05/05:05 CST 发车。
   - `data_freshness_watchdog.py` 新增 `data/replay_bars_daily` 80h 新鲜度兜底。
6. **日志降噪与指标**
   - `run_after_hours_llm_scan` 关停时不再由调度器打 INFO; 启用时函数内打一次。
   - WEEKEND 分支不再每 60s 重写 `llm_scan_state.json`。
   - 新增只读 `scripts/empty_run_metrics.py` (可执行单来源/DA 余量/ETH 空跑/零事件率)。

## 3. 明确未做 (需影子/DEB)

- 未把 09:00 生成缩成 "只验证 bridge 候选"; 全市场筛选路径暂不改 (需 10–20 日并行影子)。
- 未改 DA/UE 发车时刻、峰群采集时刻、DA 等待握手。
- 未合并 16:00/17:00 两条复盘推送; 未改任何策略/阈值/仓位。

## 4. 验证

- 定向回归: `tests/test_morning_push_generation_gate.py`、`test_manual_order_service_singleton.py`、
  `test_extended_hours_exit_cron*.py`、`test_event_stream_ingest_cadence.py`、
  `test_refresh_daily_bars_gate.py` 等 287 passed。
- 全量 pytest: 见 handoff 记录。
- 重启后 `startup_smoke_check.py --wait 90` 必须 7/7。

## 5. 回滚

- 推送门: revert `generation_flow.py` 对应 hunk; 恢复 08:47 早推行为。
- 单例锁: revert `manual_order_service.py` 单例段。
- ETH: crontab 行改回直接 Python; revert wrapper 的持仓探针 hunk。
- EventStream: crontab 去掉 `--cadence` (代码保留, 不生效)。
- 日K: crontab 删两行 + 恢复 `scheduler_core.py` 调用块; watchdog 条目可保留。
