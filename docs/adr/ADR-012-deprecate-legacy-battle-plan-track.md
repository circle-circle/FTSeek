---
title: 下线旧作战计划轨道 (daily_battle_plan_us.json) 的残留消费端
status: active
module: pipeline
related_code:
  - app/pipeline/llm_pipeline.py
  - app/core/scheduler_core.py
  - config/unified_config.yaml
related_doc: docs/adr/ADR-009-universe-expansion-execution-rail.md
tags: [pipeline, deprecation, battle_plan, dingtalk, config, cleanup]
updated: 2026-07-14
---

# ADR-012: 下线旧作战计划轨道 (daily_battle_plan_us.json) 的残留消费端

**日期**: 2026-07-14
**状态**: 已实施
**决策者**: 用户 + Hermes

## 背景

用户收到钉钉"美股开盘确认报告"（有效目标 2 / 取消目标 3：WDFC/META/JPM/BA/AAPL），发现与今天 DA 管线计划**完全不一致**，质疑该推送是否过时、是否需要清理。

### 实证诊断

系统存在两条并行的计划轨道：

| | 钉钉"开盘确认报告"轨道 | 今天真实执行的 DA 轨道 |
|---|---|---|
| 数据源 | `data/daily_battle_plan_us.json` | `playbook_instructions` 表 (ftseek_us.db) |
| 生成者 | 旧 US discovery 管线（**已停用**） | DA 投票 + universe_expansion |
| 日期 | **冻结在 `plan_date=2026-07-13`** | `2026-07-14`（13 条，最新） |
| 标的 | WDFC/META/JPM/BA/AAPL（core_pool 观察池） | COIN/NVDA/META/AMZN/MSFT/LLY/RIVN/CVE + EOG/ORCL/INTC/CHWY/APLD |

- **生成端已死**：cron 注释白纸黑字 `# 2026-07-13: 停用. 数据源 daily_battle_plan_us.json 已冻结…(旧US discovery管线不再再生)`。文件永久停在 07-13。
- **消费端仍活着**（僵尸轨道）：
  1. `run_open_checker`（每日 09:15–09:29 ET）读冻结计划 → 校验 gap/方向 → **钉钉推送误导性报告**。
  2. `_inject_battle_plan_to_pool`（启动时）把冻结 targets 注入 `daily_active_pool`（实测仅 WDFC/BA `source=PLAN` 是其独家贡献，且 `expires_at` 当日 16:28 自动过期）。
- **fast_lane 已不受影响**：`signal_stage.py:408 battle_plan_manager=None`，信号门已跳过。
- **隐蔽陷阱**：`battle_plan_manager.load()` 过期守卫仅在 `age_days > 1` 才降级；07-13 vs 07-14 恰为 1 天，逃过检测被当成"新鲜"照常推送；到 07-15（age=2）将触发"激活全部 watchlist"降级，只会越来越乱。

## 决策

方案 A（用户选定）：**下线该废弃轨道的两个残留消费端**，与已停用的 discovery cron 对齐。不迁移、不保留开盘 gap 校验（fast_lane 已不依赖）。

用统一配置开关 `feature_flags.legacy_battle_plan_enabled`（默认 `false`）门控，遵循"一键可调、不硬编码"原则：

- `run_open_checker`（`app/pipeline/llm_pipeline.py`）：函数入口读开关，`false` 直接 return，不再校验/推送。
- `_inject_battle_plan_to_pool`（`app/core/scheduler_core.py`）：入口读开关，`false` 记日志并 return，不再注入。
- `config/unified_config.yaml` `feature_flags` 新增 `legacy_battle_plan_enabled: false`。

**代码侧 `get_dotted(..., False)` 默认为 `False`**：当前运行进程（config 缓存无此新 key）取默认值即已下线——**明天 09:15 ET 那轮开盘确认无需重启即不再推送**。config 不可用时也保持下线（异常路径 return）。

## 后果

- ✅ 钉钉不再每日推送冻结的过时开盘确认报告，消除误导性噪声。
- ✅ 启动不再向扫描池注入冻结标的，避免过时 core_pool 标的污染。
- ✅ 零影响真实执行轨道 `playbook_instructions`（DA + universe_expansion）。
- ✅ 完全可逆：如需复活整条旧轨道，YAML 置 `legacy_battle_plan_enabled: true` + 重启即可。
- ⚠️ `daily_battle_plan_us.json` 文件本身保留（其他只读消费端如 dashboard/audit 仍可读历史），仅停用两个主动消费端。
