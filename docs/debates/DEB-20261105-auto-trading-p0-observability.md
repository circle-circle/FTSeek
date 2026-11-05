---
title: 自动交易全链路 P0 观测、费用与 cohort 口径修复
status: active
module: cross
related_okr: OKR-2026-11
related_code: scripts/da_expectancy_report.py
tags: [observability, expectancy, fees, shadow, mfe]
updated: 2026-11-05
---

# DEB-20261105 · 自动交易全链路 P0 观测、费用与 cohort 口径修复

> 状态：已执行。用户先要求「对当前的自动交易在策略和逻辑全链路上进行分析」，随后裁「按照建议执行」。
> 本批只实施 P0 只读观测、报表和审计修复；**没有调整入场/出场参数、仓位、实盘锁或 RFC-158 判据**。

## 1. 执行边界

- 保留 Playbook 唯一下单/持仓管理管线，不新增交易门，不改 `config/unified_config.yaml`。
- 不把 R06 费用门接入拒单链；先验证当前路径和反事实覆盖率。
- 不批量退役影子。挂载异常只登记，任何归档/删除/停用另走零引用审计。
- 不改 RFC-158 的 M1–M4/E1–E2 阈值，不把 10 月归档样本混入 11 月 5 日新 cohort。
- 工作树中其他会话的 `docs/CONTROL-LOOP-LEDGER.md`、`obsidian_vault/` 和未跟踪 RFC 文档均未触碰。

## 2. 已落地修复

| 项 | 根因 | 修复 | 行为边界 |
|---|---|---|---|
| MFE 报表崩溃 | `report()` 读取 `mfe_t15_pct`，SELECT 却漏列，触发 `IndexError` | `scripts/mfe_caliber_report.py` 补列；新增三前瞻列契约测试 | 只修报表，不改 MFE 采集或交易门 |
| ERROR 告警漏报/误报 | 解析器只认主日志 `] ERROR:`，不认 Loguru `YYYY... | ERROR | ...`；泛匹配又可能吃 INFO 正文 | `scripts/error_alert.py` 增加时间戳锚定的 Loguru ERROR/CRITICAL 解析 | 仍走既有 `app/push/` hub，阈值和推送纪律不变 |
| 缺少净期望尺子 | 现有胜率无法回答扣费后是否有 edge，也缺日聚类功效 | 新增 `scripts/da_expectancy_report.py` | 只读、无 cron、无推送 |
| R06 现役路径不明 | 配置存在不等于 Playbook 路径消费 | 新增 `scripts/playbook_fee_gate_audit.py`，AST 查消费方并按实际成交/止损做反事实 | 只读，不接拒单链 |
| 新 cohort 假 READY | 活跃 JSONL 已清空，但 latest 仍含归档 `n=99`，注册表继续读旧键 | settler 新增 `current_cohort.sim/mechanism/holdout`；只收 2026-11-05 起行；空文件显式写零；注册表改读 `current_cohort.sim.n` | 归档原始文件不动；旧 `adjudication_source.all.n` 在空档也写 0，兼容未重载进程 |

同步修正 `tests/test_entry_confirm_gate.py` 与 `tests/test_entry_confirm_live_wiring.py` 中仍停留在「门关、holdout=0、追价帽开」的旧期望，使其对齐已批准且已在 HEAD 落地的 RFC-158 真值；本批没有再次修改这些生产配置。

## 3. DA/EXT 净期望实测

复现命令：

```bash
venv/bin/python scripts/da_expectancy_report.py --from 2026-10-06 --to 2026-11-04 --source DA --json
venv/bin/python scripts/da_expectancy_report.py --from 2026-11-01 --to 2026-11-04 --source DA --json
venv/bin/python scripts/da_expectancy_report.py --from 2026-10-06 --to 2026-11-04 --source EXT --json
```

报表同时输出 A9 clean 与含排除批双口径、A10 谓词、毛/费/净 bps、净胜率、盈亏平衡胜率、利润因子、日聚类 Student-t 80% CI 与 MDE80。本窗双口径样本相同。

| 人群/窗口 | n | 毛 bps | 费 bps | 净 bps | 净额 | 日聚类读数 | 判定 |
|---|---:|---:|---:|---:|---:|---|---|
| DA 10-06→11-04 | 141 | -3.6036 | 9.3586 | **-12.9622** | **-$1,019.42** | 22 日 mean=-7.5716bps；80% CI [-28.128, 12.9848]；MDE80=43.524bps | 毛边际已为负，费用不是唯一病灶；CI 含 0，不夸大统计结论 |
| DA 11-01→11-04 | 30 | 12.9894 | 7.5941 | **+5.3952** | **+$90.61** | 仅 3 日 mean=0.0972bps；80% CI [-16.0987, 16.2931]；MDE80=24.0634bps | **不能称转正**；n<120 且日等权近零 |
| EXT 10-06→11-04 | 24 | 62.3206 | 9.2569 | **+53.0636** | **+$583.78** | 9 日 mean=60.8376bps；MDE80=82.4893bps | 小样本且效应低于 MDE，不放大、不转正 |

DA 长窗净胜率 79.43%，盈亏平衡胜率 84.53%，利润因子 0.7067。结论不是「提高胜率」，而是先持续读净期望、尾部亏损与日聚类功效；KR6 的连续 40 交易日 DA 日聚类 80% CI 下界 >0 未满足前，不讨论放大。

## 4. R06：明确不接线

```bash
venv/bin/python scripts/playbook_fee_gate_audit.py --from 2026-10-06 --to 2026-11-04 --source DA --json
```

- 静态消费方：`app/playbook/` 中 `playbook.fee_gate` 消费方为空；现役提交路径仍是 `PlaybookService -> DualBrokerAdapter`。
- 配置中的 `enabled=true` 只对应遗留实现，不能宣称现役 Playbook 已执行 R06。
- DA 141 笔止损覆盖 141/141；按实际成交价、股数、re-price 止损和现行 `rr_mult=2 / max_fee_to_gross_ratio=0.25` 反事实，**拦截 0 笔**。
- 因此本批不接线：即使接线也不会改变这 141 笔，且会新增未经验证的拒单路径。后续若重提，须先有新 cohort 证据与独立预注册。

## 5. MFE 与影子注册表实跑

MFE 修复后报表不再崩溃；按设计因新列填充率告警返回 `rc=1`：

- 落库 MFE：165 行，死单率 17.58%。
- tick：3 行；t15：0 行；gate：2 行。
- 新列 11 月 4 日才上线，低填充主要反映新 cohort 尚未攒样；不得据此调门。

只读执行 `refresh_counts()` 与 `check_mounting()` 后：

- active 共 52 项：28 `OBSERVING` + 24 `READY_TO_EVALUATE`，44 项有样本。
- `entry_confirm_forward` 已从旧读数的假 READY 修正为 `OBSERVING, current_n=0/60`；latest verdict=`TOO_EARLY`，SIM/机制/holdout 均为 0。
- 生产结算前已备份旧 latest 到 `/tmp/entry_confirm_forward_latest.pre_cohort_fix.20261105T025830.json`；活跃 JSONL 与归档 JSONL 前后 SHA256 不变。
- 5 个挂载/产出异常只登记不退役：`budget_p3_sizing`、`confidence_tier_gate` eval 未挂载；`broker_capacity_shadow`、`broker_stop_drift_shadow`、`exit_intent_reaper_shadow` 产物缺失。

## 6. 验证与部署

- `tests/test_entry_confirm_gate.py tests/test_entry_confirm_live_wiring.py`：**83 passed**。
- MFE/error-alert/DA expectancy/R06 五文件：**58 passed**。
- `tests/test_shadow_registry_criteria_provenance.py tests/test_shadow_board_guard.py`：**14 passed**。
- `tests/test_criteria_registry_visibility.py tests/test_shadow_artifact_consumers.py tests/test_auto_adjudicate.py`：32 passed，1 个与本批 diff 无关的既有 A11 字典可见性失败；未顺手改 A11 或扩大范围。
- `venv/bin/python scripts/check_undefined_names.py`：exit 0；`py_compile` 与本批 `git diff --check` 通过。
- 重启前核：未平仓仅 US.COIN SHORT 1 笔，无在途 exit_reason；`app/ scripts/` 仅有本批改动。
- 2026-11-05 03:00:46 按军规执行 `systemctl restart ftseek-us`；`startup_smoke_check.py --wait 90` **7/7 全过**。重启后 US.COIN 仍为唯一未平仓，服务 active，启动段无 Traceback/CRITICAL/ERROR。

## 7. 后续纪律

1. 继续攒 RFC-158 与 MFE 新 cohort，不回填归档样本凑 n；每周按同一报表读数。
2. 5 个挂载异常另案核消费方、补挂或标记 `eval_on_demand`；不得批量删除/退役。
3. DA 未过 KR6 前不放大；EXT 小样本正收益不用于掩盖 DA 净期望。
4. 本批没有完成、也不声称完成「策略转正」「实盘优化」或「R06 上线」。任何参数、门或仓位变化仍须独立 DEB/RFC、影子先行和用户裁决。
