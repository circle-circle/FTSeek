# RFC-150 推送治理 P0+P1 (2026-10-18)

**状态**: 已实施（应用代码 + crontab + Hermes job 文本；未改交易策略/阈值/仓位）
**触发**: 用户令核查钉钉/飞书推送内容和链路是否精准、合理、信噪比正常，并选择 **P0+P1**。

## 1. 证据（10/15–10/17 推送审计）

- 10/16 共 34 条推送：盘中主动心跳 13 条 + 通道心跳 5 条 + 服务启停 8 条 = 26 条
  低信噪/重复（约 76%）；10/15 41 条、10/17 35 条。
- 04:00–04:35 CST 复盘集群一条日子连推 5 条：日报 + Orchestrator「盘后复盘报告」+
  次日交易提示 + 教训验证 + Playbook 指令效果。
- `ReviewEffectTracker` 不过滤失败动作：一个「配置路径不在白名单」的失败提案
  10/13–10/16 连续 4 天进入 pending，每天重复推「⚠️ [自动回滚] ... 回滚失败」。
- Hermes job `7a40dd7c9e94`（盘后综合复盘）的最终回复投递飞书，而 Step 3
  `daily_alert_summary.py` 内部又直推一次飞书 = 同通道双推。
- 低频日报天天推：LLM token 成本、DA 链路日检；延长时段日报带 `--always`
  在无样本日也推“链路活着”。

## 2. P0 改动

### 2.1 盘中主动心跳降频（30→60min，可配置）

- `config/unified_config.yaml`: 新增 `push.intraday_heartbeat_interval_min: 60`。
- `main.py`: 启动 `HeartbeatLoop` 时从配置读取；读取失败回退 60。
- 预期：盘中心跳 13 条/日 → 约 7 条/日。心跳是状态播报，不是告警；
  ERROR/CRITICAL 仍由 `error_alert.py` 与通道熔断兜底。
- 回滚：配置改回 30 + 重启。

### 2.2 通道心跳跨进程共享（业务证据 + 全局 ping 时隙）

- `app/push/health.py`:
  - `note_success()` 把业务推送成功证据写入 `data/push_health_state.json`
    （按通道节流 30s），短命 cron/长跑脚本也能看到其它进程的活性证据。
  - `_claim_ping_slot()` 用 `flock` 每小时只放行一个进程真正 ping；
    其余进程跳过并计入 `ping_slot_skipped`（`get_status()` 可观测）。
  - 无共享路径时保持原进程内行为（单测/回退安全）。
- `app/push/hub.py` / `get_push_hub()`: 新增
  `push.health_check_interval_sec`（默认 3600）与
  `push.health_check_shared_state`（默认 true）。
- 回滚：`push.health_check_shared_state: false` + 重启（回到进程内抑制）。

### 2.3 复盘集群合并（一条日报为主）

- `ReviewOrchestrator.run_daily/run_weekly` 新增 `push_report: bool = True`；
  调度器日/周复盘调用时传 `push_report=False`，报告仍生成/返回，
  但不再独立推送。日报/周报由 `_push_review_text` 单点发出。
- 次日交易提示：只有 `top_risk` / `avoid_sessions` / `caution_symbols`
  真正有内容且内容指纹变化时才推；状态文件 `data/.last_review_hint.json`
  跨重启去重（不含 lesson_count，避免“77 条教训”每天触发）。
- Playbook 指令效果：不再单独推送，只落日志（同一指标已包含在 05:00 CST
  Hermes「计划 vs 执行」综合复盘）。
- 保留高信号：配置变更通知、自动回滚、交易辩论、教训验证（仅 updated>0）、
  信号质量高危策略告警。
- 回滚：去掉 `push_report=False` 调用（恢复 orchestrator 独立推送）；
  如需恢复提示/指令效果日推，按 git revert 本段。

### 2.4 自动回滚判据修复

- `ReviewEffectTracker.record_action()`：同一 `issue_id` 已有未检查记录时不重复追加；
  新成功动作刷新基线/回滚版本。
- `get_pending_checks()`：只返回 `success=True` 且 `rollback_version` 非空的记录，
  并按 `issue_id` 去重。失败动作/无配置变更动作不再进入效果检查，
  从源头消除“无法回滚”的每日重复告警。
- 保留：真实成功配置变更恶化后的回滚推送。

### 2.5 Hermes 飞书同通道双推

- `scripts/daily_alert_summary.py`: 新增 `--no-dingtalk` / `--no-feishu` /
  `--no-push`；默认行为不变。
- Hermes job `7a40dd7c9e94` Step 3 改为
  `scripts/daily_alert_summary.py --no-feishu`（备份
  `data/backups/hermes_jobs_backup_20261018_push_governance.json`）。
  DingTalk 保留为跨通道，飞书正文由 Hermes 最终回复唯一投递。

## 3. P1 低频日报降频

crontab（备份 `data/crontab.backup.20261018_push_governance.txt`）：

| 报表 | 原 | 新 | 理由 |
|---|---|---|---|
| DA 链路日检 | `5 6 * * 2-6 --push` | `5 6 * * 6 --push` | 日推低信噪；异常由 freshness watchdog / error_alert 兜底 |
| LLM token/成本 | `20 6 * * 2-6 --push` | `20 6 * * 6 --push` | 成本只作周度趋势；预算异常另有告警 |
| 延长时段阶段日报 | `--push --always` | `--push` | 恢复「无样本静默」；与台账 crontab-eth-stage-daily 的零噪音语义一致 |

`shadow_board` 已有可行动门 + episode 去重；`fee_gate_shadow_report` 已有状态门；
本批不动。`run_playbook_track` 的盘后推送已有持久指纹去重（10-15/10-16 修复），
本批不重复改。

## 4. 验证

- 新增契约 `tests/test_push_governance_20261018.py`（16 例）：跨进程证据、
  全局 ping 时隙、orchestrator push_report、提示指纹、pending 判据、
  逐通道开关、配置/接线。
- 合并回归（推送/复盘/日报/台账/tracking 共 24 个测试文件）：**363 passed**。
- `ConfigValidator(strict_mode=True)`：全部通过；新增配置键可正常读取。
- `py_compile` 全部改动文件通过。

## 5. 回滚

- 心跳频率：`push.intraday_heartbeat_interval_min: 30` + 重启。
- 通道共享：`push.health_check_shared_state: false` + 重启。
- 复盘合并/回滚判据/逐通道开关：git revert 本批代码；保留的状态文件
  （`data/.last_review_hint.json`、`data/push_health_state.json`）可删除。
- crontab：`crontab data/crontab.backup.20261018_push_governance.txt`。
- Hermes：恢复备份 jobs.json（或把 Step 3 的 `--no-feishu` 去掉）。

## 6. 未做（需另立裁决）

- 服务启停推送（10/16 实测 8 条）未动：属运维可见性，需确认是否只在异常重启时推。
- 周度三份报表（周复盘 / Playbook 周报 / edge 周报）未完全合并：
  内容互补，先观察一周再裁。
- 未改任何交易策略、阈值、仓位、入场/出场链路。
