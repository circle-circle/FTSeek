# DEB-20260916 · 全链路减法优化执行

> 状态：已执行 | 日期：2026-09-16 | 执行人：Codex | 用户裁决：按照建议执行修复

## 1. 结论

本轮只执行**确定性收益**的减法，不改 Playbook 判据、退出参数、下单路由、账户模式或 live 参数。系统仍在 SIM 姿势。已完成：

1. 删除零引用依赖 `PyMuPDF==1.24.11`，实际卸载约 46MB。
2. 退役已停用一周的 `settle_overnight_shadow` 调度挂载、死开关、脚本与测试，历史 JSONL 保留。
3. 删除已 KILLED 的 `deriv_unusual` 采集实现与死开关，历史 `data/futu_deriv_unusual.jsonl` 保留，评估脚本保留。
4. 删除 Phase A 仅元数据策略适配器及其死参数；coordinator 资金/锁账本保留。
5. 依赖 manifest 收敛为 `requirements.lock`，删除 `requirements.txt`、启动回退和 CI 回退。

## 2. 删除清单与影响

| 对象 | 删除依据 | 影响范围 | 回滚 |
|---|---|---|---|
| `requirements.lock:62` PyMuPDF | 仓内/外部调用面零 import；`pip check` 通过 | venv 少 46MB；无运行路径 | 恢复 lock 行并重装 |
| `scripts/settle_overnight_shadow.py`、两份测试、调度挂载、`overnight_hold_settler_enabled` | 2026-09-10 配置停，观察一周；产出零 live 消费方 | 盘后不再空转重算；历史 JSONL 与 `overnight_arm_report` 判罪线不变 | 恢复本次删除文件、调度块与开关后重启 |
| `deriv_unusual` 采集函数、常量、配置开关 | 2026-08-29 两轮预注册评估 KILLED；8/29 已停采 | 不再保留可达复采路径；通道 B/C/D/E 不变 | 恢复采集块与开关后重启 |
| `app/strategy/adapters/*`、`base_adapter.py`、priority/adapter-only 配置键 | 三个 adapter 只声明元数据；主线冻结新仓，Phase A 审计链无决策消费 | coordinator 保留资金/锁账本；删除旧主线 Phase A 仲裁审计 | 恢复文件、调用块与配置键后重启 |
| `requirements.txt`、`start.sh` 回退、CI `|| true` 安装 | 唯一真相源应为锁定 manifest；松散/容错安装会漂移 | 新环境只能按 lock 构建；安装失败显式终止 | 恢复文件与回退分支 |

## 3. 明确不删除

- **Playbook 现役门禁**：`exec_gate_audit.jsonl` 3,042 行中，notional cap 829、max positions 779、chase cap 501、fail 181、knife gate 163 等均非零；未计算 unique marginal contribution 前不动任何门。
- **`trigger_miss_telemetry` 门禁**：17,712 行中 low ATR 6,000、after cutoff 5,342、vol gate 1,638、extreme gap 1,605、VWAP gate 1,066 等均非零；同上。
- **top movers 采集**：现有 820 快照 / 82,000 条 / 1,187 个唯一标的，评估脚本尚未存在，未冻结、未删除。
- **ORB、legacy `scan_loop`、退出侧共享设施**：按 AGENT.md 约束保留。

## 4. 验证

- `pip check`：通过。
- `bash -n start.sh scripts/archive/test_deployment.sh`：通过。
- 定向 `pytest tests/test_config_key_reverse_census.py tests/coordinator/test_coordinator_phase_a.py -q`：30 passed。
- 全量 `pytest -q`：5,205 passed / 54 skipped / **7 failed**。失败项为既有未提交与外部 cron 变更：control-loop ledger 4 项、DB chokepoint 1 项、except-pass 棘轮 1 项、W6 日志棘轮 1 项；未列入本 DEB 的优化对象。
- 分阶段重启 + `scripts/startup_smoke_check.py --wait 90`：
  - 18:51 结算器删除后：7/7 通过。
  - 18:52 `deriv_unusual` 删除后：7/7 通过。
  - 18:55 Phase A 删除后：7/7 通过。
  - 19:03 使用收敛后的 `start.sh` 最终重启：7/7 通过，PID `1578531`，SIM 姿态确认。

## 5. 复核口径

```bash
./venv/bin/pip check
rg -n --hidden -g '!venv' -g '!node_modules' -g '!logs' -g '!data' \
  'PyMuPDF|pymupdf|import fitz|from fitz' . /root/.hermes/cron/jobs.json
rg -n 'overnight_hold_settler_enabled|settle_overnight_shadow' \
  app scripts tests config .git/hooks /root/.hermes/cron/jobs.json
rg -n 'deriv_collect_enabled|_collect_deriv_unusual|_DERIV_' \
  scripts/futu_channel_shadow_collect.py app/playbook/shadow_registry.py config/unified_config.yaml
rg -n 'strategy\.adapters|MainPipelineAdapter|FastLaneAdapter|LLMPlanAdapter|all_adapters|_strategy_adapters' \
  app scripts tests config .git/hooks /root/.hermes/cron/jobs.json
rg -n 'requirements\.txt' start.sh .github scripts/archive
systemctl restart ftseek-us
PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90
```

历史文档中的旧名不改写；它们是当时决策的证据。
