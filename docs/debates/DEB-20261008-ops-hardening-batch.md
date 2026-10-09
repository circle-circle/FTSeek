---
title: 运维加固批 — 双源开关持久化、LIVE/自动闸 fail-close、启动依赖与锁语义、冒烟 waiver、replay_cache retention
date: 2026-10-08
status: 已落地 + 单测/回归/当前态冒烟通过；Python 代码待下一合法重启窗口生效（不把并发 WIP 顺带上线）
trigger: 用户令「从运维、性能优化、框架逻辑等角度检查当前的项目，是否有值得优化和调整的问题点」→ 审查后令「按照建议执行优化」
---

# 运维加固批

## 0. TL;DR

本批修复 7 个运维缺陷：开关 YAML 写失败被忽略、LIVE 启动时 ExitPolicy 失败继续跑、
自动引擎开关读取异常继续放行、启动时每次网络安装依赖、systemd 预清 flock 锁文件击穿
单实例防线、探针窗口下通用冒烟必红、`data/replay_cache` 无 retention。

代码与环境均已落地，专项 + 合并回归 **434 passed**；当前运行态单次启动冒烟 **7/7 通过**。
由于仓库同时存在其他 agent 的在途 WIP，本批**未重启服务**，Python 代码待下个合法窗口生效。

## 1. 问题与决策

| # | 病根 | 决策 | 落地锚点 |
|---|---|---|---|
| 1 | Dashboard 开关先改内存、忽略 YAML 写返回值；「关闭」写盘失败后重启回 ON | YAML 先写，失败返回 `AUTO_TRADE_PERSIST_ERROR` 且内存不动 | `app/web_dashboard/routes/trade.py:631` |
| 2 | ExitPolicy 启动校验失败仅 CRITICAL 后继续；LIVE-capable 下等于止损保护缺失仍可交易 | SIM 维持降级；LIVE-capable 校验失败/异常 `SystemExit` | `main.py:227` |
| 3 | ExecutionEngine 自动开关读取异常 `pass` 放行，且不能由 order_executor 内存态兜住 | 异常时订单 `REJECTED`，`failure_reason` 带 fail-close，日志带栈 | `app/execution/engine.py:315` |
| 4 | `start.sh` 每次启动都执行 lock 安装，依赖网络/PyPI 可用 | `requirements.lock` sha256 marker + `pip check` 双闸；匹配则跳过安装 | `start.sh:64` |
| 5 | systemd 启动前 `rm /run/ftseek-us.lock`：旧持锁进程锁旧 inode，新进程新建 inode 可并存 | 删除 `ExecStartPre rm`；flock 文件残留本身不阻塞新锁 | `/etc/systemd/system/ftseek-us.service` |
| 6 | LIVE 探针 waiver 窗口内，AGENT.md 通用冒烟命令不带 `--live-probe` 必红 | 只读 waiver，且 fail-close 解析；活跃时接受「全锁闭」或「授权探针」两形态 | `scripts/startup_smoke_check.py:108` |
| 7 | `data/replay_cache` 约 1.1GB 可再生缓存无按龄清理 | v1 只清顶层 `*.json` mtime>90 天，默认 dry-run，`--apply` 才删 | `scripts/data_retention.py:34` |

边界保留：
- 不改 AGENT.md 红线中 3 处 `auto_trading_enabled` 裸 regex 直读；本批只修写入顺序与异常闸语义。
- 不清 `data/replay_bars*/` 与 `data/*.jsonl`：前者是历史输入，后者消费方未逐个审计。
- 不新增推送；冒烟失败仍走原 notifier 面。

## 2. 验证

### 2.1 专项

```bash
PYTHONPATH=. ./venv/bin/python -m pytest \
  tests/web_dashboard/test_auto_trade_toggle_persistence.py \
  tests/test_exit_policy_startup_gate.py \
  tests/test_startup_smoke_check.py \
  tests/scripts/test_data_retention.py \
  tests/execution/test_auto_gate_failclose.py -q
```

专项结果：`43 passed`（分别为 6/5/24/6/2）。

### 2.2 合并回归

```bash
PYTHONPATH=. ./venv/bin/python -m pytest \
  tests/web_dashboard/ \
  tests/execution/test_auto_gate_failclose.py \
  tests/test_exit_policy_startup_gate.py \
  tests/test_startup_smoke_check.py \
  tests/scripts/test_data_retention.py -q

PYTHONPATH=. ./venv/bin/python -m pytest \
  tests/execution/ tests/regression/test_config_governance.py -q
```

结果：`184 passed, 1 warning`；`250 passed`。合计 **434 passed**（两命令覆盖面有重叠）。

### 2.3 运维实物

```bash
bash -n start.sh
./venv/bin/pip check
PYTHONPATH=. ./venv/bin/python scripts/data_retention.py
PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py
```

结果：语法通过；`No broken requirements found`；dry-run `stale_files=0 stale_bytes=0`；
当前运行态冒烟 7/7 通过（waiver 自动识别为授权探针窗口）。

`requirements.lock` 与 `venv/.requirements.lock.sha256` 均为
`5ccef601660bf06997982e156a81dc43fde387a45682aa0ab0c09a6b7e98af8e`。

注意：`--wait 90` 是「紧跟 restart、等待新启动段」的用法。在未重启的既有进程上运行会按设计报
「启动段早于本次重启」，这不是冒烟断言失败；非重启态核当前服务时使用单次判定。

## 3. 部署与回滚

### 部署状态

- 仓库代码：已落地，未提交。
- systemd：已删除预清锁行，已 `daemon-reload`，服务仍 active。
- crontab：已新增 04:20 `data_retention.py --apply`。
- marker：已预置，启动依赖检查实测通过。
- **未重启**：`git status` 中仍有其他 agent 的在途 WIP（playbook/pipeline 等）。按 AGENT.md，
  不许把别人 WIP 顺带上线；本批 Python 改动待下个合法重启窗口生效。

下一合法重启 SOP：

```bash
git status --porcelain -- app/ scripts/
# 确认仅本批自己的改动需要上线
sqlite3 data/ftseek_us.sqlite "SELECT symbol FROM trade_records WHERE exit_time IS NULL;"
systemctl restart ftseek-us
PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90
```

无需 `--live-probe`：waiver 活跃期脚本自动识别；显式传 flag 时仍使用严格探针判据。

### 回滚

| 面 | 回滚 |
|---|---|
| 代码 | revert 本批 6 个生产/脚本文件；测试同步 revert |
| systemd | `cp /etc/systemd/system/ftseek-us.service.bak.20261008_lockrm /etc/systemd/system/ftseek-us.service && systemctl daemon-reload` |
| crontab | `crontab -l | grep -v 'scripts/data_retention.py --apply' | crontab -`（或从 `data/crontab.backup.20261008_data_retention.txt` 恢复） |
| 依赖 marker | 删除 `venv/.requirements.lock.sha256`，即回到每次安装 lock |

## 4. 复查清单

1. 复跑 §2.1–§2.3，报告真实通过数，不引用本文数字替代实测。
2. 用 systemd 备份与当前文件 diff，确认仅移除 `ExecStartPre=/bin/rm -f /run/ftseek-us.lock` 及注释更新。
3. 核 `crontab -l` 只新增 `20 4 * * * ... scripts/data_retention.py --apply` 一行。
4. 重启前核未平仓/在途出场与他人 WIP；重启后跑 `--wait 90` 冒烟。
5. 检查 `logs/data_retention.log` 首轮输出，确认 apply 与 dry-run 计算口径一致。

## 5. 遗留

- `health_check.sh` 告警旁路迁 `app/push/` hub。
- `disk_cleanup.sh` 移除 FTSeek-US 日志 `tail+mv` 截断段，避免与 logrotate 冲突。
- `data/*.jsonl`、`data/replay_bars*/` retention 需先逐个审计消费方。
- 死键 `strategies.defaults.min_strength` 清理需五面零引用验证。
- 组件启动 registry，替代启动冒烟中的日志字面量判定。
