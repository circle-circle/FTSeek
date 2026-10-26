---
title: 冻结哨兵快速层 — 90s 侦测 + 双栈取证 + 硬停自愈
date: 2026-10-23
status: 已批准（用户裁「批准 P0 取证与自愈方案」）；代码已起草，待盘后重启生效
trigger: 10/22、10/23 连续两次开盘后进程僵死；10/22 哨兵 452s 才出手，且 SIGTERM 关停被忽略，120s 后 SIGKILL
related_okr: OKR-2026-11
---

# DEB-20261023 · 冻结哨兵快速层

## 状态

- [x] 进行中
- [x] 待决策
- [x] 已决策（用户裁「批准 P0 取证与自愈方案」）
- [ ] 已验证（待盘后重启 + 下次事件复验）
- [ ] 已关闭

## 0. 结论

冻结哨兵（`scripts/freeze_sentinel.py`，cron 每分钟）已有 420s 冻结层，但在 10/22 事件中
**端到端耗时 ~9.5 分钟**：心跳停滞 452s 才处置 → `systemctl restart` 的 SIGTERM 被僵死
进程忽略 → 120s 后 SIGKILL。10/23 同类事件中人工在 4 分钟时重启，同样只能 SIGKILL。

本批新增**快速层（90s）**：心跳停滞 >90s 且单元 active/running、TRADING、过启动宽限
→ py-spy + SIGUSR1（faulthandler）双栈取证 → SIGKILL 硬停 → systemd `Restart=always`
拉起 → critical 告警。420s 旧层保留为兜底。**不新增 cron、不改交易门/仓位/退出参数。**

## 1. 证据

### 1.1 10/22（哨兵已出手，仍慢）

- `freeze_sentinel.log-20261023`：`[20261022_214002] 🚨 进程冻结处置: 心跳停滞 452s
  (>420s 裁决阈值) | systemctl restart 成功`。
- systemd：`21:40:03 Stopping` → `21:42:04 stop-sigterm timed out. Killing` → SIGKILL。
- 应用日志：最后一条业务日志 `21:32:32`，**无 `收到停止信号`**（全文件仅 11:40 那次）。
- py-spy 栈 `logs/freeze_stacks/freeze_20261022_214002_pid3056646.txt`：

```
PlaybookExitFastLoop (idle)
  wait (open_context_base.py:52)
  sync_query_processor (open_context_base.py:302)
  request_history_kline (open_quote_context.py:386)
  get_kline (futu_lv3_provider.py:1458)
  _track_bar_peak_trough (app/playbook/pipeline.py:2521)
  monitor_playbook_exits (exit_monitor_flow.py:702)
```

即：**心跳源（出场快循环）卡在无超时的同步 K 线请求**；SIGTERM 无法让它完成关停。
网络线程 `Thread-2` 同时在 protobuf `InternalParse` 大消息解析中。

### 1.2 10/23（人工重启，同一形态）

- 最后业务日志 `21:31:28`（批量取价超时 >6s）；人工 `21:35:32` 重启 → `21:37:32` SIGKILL；
  同样无 `收到停止信号`。
- 当日 `21:50` 的正常关停耗时 **7s**（`scheduler.stop → 退订 → close → os._exit` 全链完整），
  证明关停代码本身不是瓶颈。

### 1.3 既有哨兵为何没兜住 10/23

420s 阈值 + 60s cron 粒度 → 最早 21:38 才处置；人工 21:35 已先重启。**检测延迟直接等于
退出监控停摆时长**，不可接受。

## 2. 方案（已实施代码）

- `app/observability/fault_dump.py`（新）：`faulthandler.enable(all_threads=True)` +
  `register(SIGUSR1, all_threads=True)`；main 启动即调用，失败只告警。
- `scripts/freeze_sentinel.py`：
  - `FAST_STALE_S=90`（env `FTSEEK_SENTINEL_FAST_S`，0=关闭快速层）。
  - `evaluate_fast()` 纯决策：心跳 >90s + `ActiveState=active/SubState=running` +
    TRADING + 启动宽限 180s + 冷却 900s。
  - `hard_restart()`：py-spy → SIGUSR1（仅当目标确实捕获，避免旧进程被默认动作杀死）→
    +3s → 复核单元仍 active/running（防与运维 stop 抢方向盘）→ SIGKILL → 等
    `Restart=always` 拉起（45s）→ 兜底 `reset-failed + start`。
  - cron 重叠保护：`/run/ftseek-freeze-sentinel.lock` 非阻塞 flock。
  - 420s 旧层保留，并改用 `hard_restart`（同样避免 SIGTERM 拖满 120s）。
- 心跳源不改：10/22 栈证明**出场快循环就是心跳源**，它卡住时心跳必然停滞；主循环心跳
  反而抓不到本次事件（主线程当时在 `time.sleep`，是健康的）。

## 3. 明确不做

- 不新增 cron（哨兵已在 `* * * * *`）。
- 不改 `STALE_S=420` 旧裁决值（快速层是增量；旧层兜底）。
- 不改任何交易门、仓位、止损止盈、执行频率。
- 不改 systemd unit（用 SIGKILL，不产生 core；无需 `LimitCORE`）。
- 不绕过 PushHub：告警沿用哨兵既有的 `send_critical_sync`（进程外通道，进程已僵死时
  进程内 PushHub 构造性不可达；与 `DEB-20260902 E0` 同先例）。

## 4. 回滚

- 快速层：在 crontab 行命令前加 `FTSEEK_SENTINEL_FAST_S=0`（cron 不读 `.env.us`；
  下一分钟生效，无需改代码/重启服务）。旧 420s 层立即接管。
- 代码：`git revert` 本批（`fault_dump` 启用行 + 哨兵快速层 + 测试 + 本文档）。
- 无新增 cron 行、无 unit 变更、无 DB 迁移。

> 生效时点：`scripts/freeze_sentinel.py` 由 cron 每分钟新起进程读取，**哨兵侧改动
> 在保存文件后的下一分钟即生效**（当前进程尚未加载 `faulthandler` 时，`SigCgt` 检查
> 会跳过 SIGUSR1，只走 py-spy + SIGKILL）。`faulthandler` 需下一次服务重启后生效。

## 5. 验证

- `pytest tests/test_freeze_sentinel.py`：**31 passed**（含快速层 S14–S22 与既有 S1–S13）。
- 只读干跑（2026-10-23 23:xx，TRADING）：heartbeat age 3.8s → `fast=ok / slow=ok`，零动作。
- 待办：盘后重启加载 `faulthandler`；用 `kill -USR1 <pid>` 验证全线程栈落到
  `logs/ftseek_us.log`；下次心跳停滞时核对 `logs/freeze_stacks/` + 应用日志双栈。

## 6. 后续（不在本批）

- P1 根因：审计出场路径所有**无超时的同步 Futu 调用**。10/22 栈已点名
  `request_history_kline`；`55e4331de` 已为 bar 路径加 `latency_sensitive=True`，但 10/23
  仍发生停滞，需覆盖 `get_positions`/订单状态/报价等其他同步入口，并评估为
  `request_history_kline` 增加有界等待（需单独 DEB/RFC，属行为变更）。
- `freeze_sentinel` 仍在 `docs/references/crontab-loop-baseline.txt` 的“台账回填待专项”
  基线中；本批不改台账，建议下批补登控制环路。
