# 议题：技术债棘轮三红收口（W4/W6 日志卫生 + pipeline.py 行数 + except:pass）—— 清剿优先，改判据只此一处

## 状态

- [x] 已决策（用户 2026-10-28 裁「按建议执行」）
- [x] 已实施（两批提交）
- [x] 已验证（三守卫复绿 + tests/playbook 1158 全绿）
- [ ] 待观察（夜间 `daily_invariant_check` cron 首次带新基线跑通）

## 背景

2026-10-28 夜间守卫（`scripts/daily_invariant_check.py`，cron `5 5 * * 2-6`）首次照见三条棘轮同时漂移：

| 守卫 | 基线 | 实测 | 差 |
|---|---|---|---|
| `test_except_pass_ratchet` | 921 | 969 | +48 |
| `test_pipeline_size_ratchet` | 3,700 | 4,136 | +436 |
| `test_w456_log_hygiene_ratchet` | 基线 JSON | 4 项漂移 | +4 |

用户要求先出归因与处置方案，再决定动作。本 DEB 记录**归因证据**、**裁决**、**两批实施**、**边界声明**与**回滚**。

## 归因（实测，非推测）

### A. W4/W6 的 4 处漂移 —— 用"基线登记点取差集"定位，不靠 blame 猜

守卫每维只回填 3 条 `refs` 样本，`refs` 的"例"行**不是**新增行本身（首个样本行）。正确做法：取基线最后一次登记提交
`80d8817ba`（10-09 拆分）的四文件源码，与 HEAD 用**同一 AST 口径**扫描后取差集，得到"基线后真正新增"的 5 处：

| 文件 | 病灶 | 引入 |
|---|---|---|
| `app/playbook/db.py:486` | except 内 `logger.debug` 吞"仲裁留痕失败" | 1c841a66a（10-27） |
| `app/playbook/pipeline.py:1243` | except 内 `logger.debug` 吞"DA 归因索引读取失败" | 9e1ba3145 RFC-172（10-26） |
| `app/data/futu_lv3_provider.py:1451 / :1515` | 落盘缓存读/写失败无 `exc_info` | RFC-171（10-26） |
| `pipeline_parts/auto_execute_flow.py:1448` | `execution_state` 读取失败无 `exc_info` | RFC-169（10-26） |

### B. pipeline.py +436 —— 基线护栏把"调基线"这条路在代码层封死

`test_pipeline_size_ratchet.py::test_baseline_not_tampered_upward` 硬断言 `BASELINE ≤ 3_700`，
**任何 DEB 都无法上调**（调守卫本身 = 它要防的动作）⇒ 唯一出路是真抽代码。逐提交量得：

| 时点 | 行数 | 增量 |
|---|---|---|
| 10-09 拆分后 | 3,578 | 基线 3,700 由此设定 |
| 10-18 | 3,670 | +92 |
| 10-23 | 3,728 | +58 |
| **10-26 7ab8167bb 归档 WIP** | **4,056** | **+328** |
| 10-27 RFC-172 / cfaa425db | 4,135 | +79 |

+328 那批的构成是**单一内聚子系统**（LIVE 券商 STOP 保护族，见下表 9 个新函数 291 行），不是散落逻辑。

### C. except:pass +48 —— 40 处不是生产代码（前一版归因的更正）

按扫描面逐目录实测：`app 612 + scripts 282 + ops 27 + main 6 + config 2 + docs 40 = 969`。逐文件对基线提交取差集：

| 成分 | 处数 | 说明 |
|---|---|---|
| `docs/analysis/2026-10-27-da-ca-offline-replay/` 的 `baseline.py` / `variant.py` | **+40** | 一次性**离线回放程序**（各 20 处），DEB-20261027 证据归档包（README + SHA256SUMS），**无 import 引用、无调度载体、未入库** |
| 10-09 拆分搬家 | **0 净** | `pipeline.py` −51 / `pipeline_parts` +51 |
| 真实新增 24 处 − 其它批顺带清剿 16 处 | **+8** | 跨 10-08 基线后十余次提交累积 |

⇒ 把 +48 整体当"技术债"会做错两件事：改那 40 处 = 破坏证据校验和；改"护栏太吵"= 掩盖真缺口。

## 裁决（用户 2026-10-28「按建议执行」）

| 项 | 处置 | 理由 |
|---|---|---|
| W4/W6 | 就地清剿 5 处（`debug→warning+exc_info` / 补 `exc_info`） | 不改任何控制流 |
| pipeline.py | 抽 **LIVE 券商 STOP 保护族** L3141-3630（490 行 / 11 函数 / 4 常量）→ `pipeline_parts/live_stop_guard.py` | 阶段 15，沿用 DEB-20261008 §9.5 拆分路线；抽完 3,664 ≤ 3,700，**不动基线** |
| except:pass | ① 扫描面收敛到生产可见面（排除 `docs/`，与 W4/W6 的 `_HYGIENE_ROOTS` 对齐）② 清剿基线后真实新增 29 处（剔除拆分搬家 51 处）③ `BASELINE 921→900` | 只降不升；判据变更仅此一处并在此落档 |

## 实施

### 批 1：`6b17007c6` W4/W6 清剿（4 文件 +8/−5）

`db.py:486` `debug→warning+exc_info`；`pipeline.py:1243` 同；`futu_lv3_provider.py:1451/:1515` 补 `exc_info=True`；
`auto_execute_flow.py:1448` 补 `exc_info=True`。零控制流变更。

### 批 2：`4f0ec6278` 阶段 15 抽模块（4 文件 +550/−494）

- 新文件 `app/playbook/pipeline_parts/live_stop_guard.py`（520 行）：`_reconcile_broker_stop_exit`、`_release_live_broker_stop_for_rule_exit`、
  `_alert_broker_stop_partial_fill`、`_halt_live_stop_protection_gap`、`_connection_db_path`、`_persist_live_stop_rehang_state`、
  `_stop_price_from_metadata`、`_position_matches_live_short`、`_live_close_failure_is_explicit`、`_maybe_rehang_live_broker_stop`、
  `_handle_live_rule_exit_failure` + 4 个仅本族使用的常量。
- `pipeline.py` 留 re-export（保 `pipeline.<name>` 与 kwargs 传参面）；`_close_trade_record` 仍走 pipeline 转发器
  （本体在 close_flow，转发器注入 `datetime/_now_et/logger/_sl_burst_alerted`），用函数内 lazy import 防循环。
- **锚点重钉 4 处**（本体已不在 pipeline，打 `pipeline.*` 补丁不再穿透）：
  `test_live_arm_p0_contracts.py:400`（`_reconcile_broker_stop_exit`）、
  `test_live_stop_rehang_contracts.py:96`（`_halt_live_stop_protection_gap`）、`:288`（`_alert_broker_stop_partial_fill`）、
  `:308`（`_LIVE_GUARD_ALERTED` 常量）。
- 结果：`pipeline.py 4,136 → 3,664`（余量 36 行）。

### 批 3：except:pass 清剿 + 判据收敛（本 DEB 所在批）

- `tests/test_except_pass_ratchet.py`：`_EXCLUDE_PARTS += {"docs"}`，`BASELINE 921→900`（含逐项说明）。
- 清剿 29 处（剔除拆分搬家）：`position_sync` 4（**SL/TP 真值降级 → warning**）、`push/health` 2、`finnhub_provider` 1、
  `order_executor` 1、`data_collector` 1、`pipeline` 1、`daily_review` 1、`trade_store` 1、`risk_state_machine` 1、
  `ops/intraday_diagnose` 1（首日无 state 改显式 `return {}`）、`scripts` 15（坏行/坏 bar/坏日期 → stderr 留痕）。
- **实施中自查发现的一处口径教训**：首版把"预期内坏行跳过"写成 `logger.debug`，被 W4 守卫当场抓住
  （except 内 debug = 不可见）。改为 `logger.info`（LOG_LEVEL=INFO 下可见，且不刷 WARNING）——
  三条棘轮互相咬合，清 A 债不能欠 B 债。

## 验证

| 项 | 结果 |
|---|---|
| 三守卫 | `test_except_pass_ratchet` / `test_pipeline_size_ratchet` / `test_w456_log_hygiene_ratchet` = **20 passed** |
| except:pass 实测 | **900**（`app 599 + scripts 267 + ops 26 + main 6 + config 2`），基线 900，tight 检查通过 |
| pipeline.py | **3,664** ≤ 3,700 |
| 回归 | `tests/playbook` **1,158 passed**（与改动前同数）；相关面 49 个测试文件（覆盖 19 个改动模块 + 6 条棘轮 + 4 个 LIVE STOP 契约）= **667 passed / 1 skipped**，同集合在 HEAD worktree 等基跑 = 663 passed + 4 failed，逐个查明：1 个是「旧基线 × 旧代码」的设计性红（`test_except_pass_not_growing`）、3 个是裸 worktree 缺 `data/` 生产库的环境性红（`test_open_position_sl_tp_contract` / `test_extended_hours_entry_executor` / `test_da_bridge_dingtalk_push`，均报 `no such table: open_positions`）⇒ **相关面新增失败 0**。另核一处既有红：`tests/coordinator/test_coordinator_phase_a.py::test_capital_allocator_persists_asynchronously_outside_hot_path` 在 HEAD 上同样失败 = 非本批引入 |
| 编译/静态 | 19 个改动文件 `py_compile` 全过；`pyflakes` 无 undefined name（仅存量 unused/f-string 提示） |

**未证（诚实边界）**：非 playbook 全量 6,892 例**没有**跑完等基对比 —— 单次 >10 min 被中止，两次并行还互相抢 I/O。
故本 DEB 只主张「相关面 49 文件新增失败 0 + 已核 1 处既有红」，**不主张**「全仓无新增失败」。
若后续要补这条，做法：`git worktree add --detach /tmp/wt_x <改动前提交>`，两侧同一 `pytest tests/ -q --ignore=tests/playbook`
各跑一次（**串行，勿并行**），`comm -13` 比 FAILED 列表；裸 worktree 缺 `data/` 的条目须能解释成环境原因。

## 边界声明（不许被后续批悄悄扩大）

1. `docs/` 排除的前提是**它只放文档与证据**。任何人把生产逻辑放进 `docs/` 就脱离本棘轮覆盖 —— 该前提写进基线注释，例外须另立 ADR。
2. `live_stop_guard.py` 的 4 个常量**只在该模块内可变**；tests 要重置状态必须 patch 真源模块（`_lsg.*`），patch `pipeline.*` 不再生效。
3. `test_baseline_not_tampered_upward` 仍是 3,700 硬顶：pipeline.py 后续只准继续降，不得靠改守卫放宽。
4. 本 DEB 不改变任何交易行为：W4/W6 是日志级别；抽模块是逐字搬家；except:pass 是 pass→留痕（`continue` 语义原样保留）。

## 回滚

| 批 | 回滚 |
|---|---|
| 1 | `git revert 6b17007c6` |
| 2 | `git revert 4f0ec6278`（等价：把 `live_stop_guard.py` 函数体粘回 pipeline.py L3141-3630 并删文件、恢复 4 处测试锚点） |
| 3 | `git revert <本批>`；基线可回到 921 + 保留 docs 扫描面（守卫会把 40 处重新计入，红但不影响交易） |

## 遗留（不在本批范围）

- except:pass 存量 900 处属**存量债**，按"分批清剿、每批降基线"继续，本批不动。
- `pipeline.py` 剩余最大块 `_build_instructions_from_votes`（451 行）是下一刀候选（需单独设计与辩论，L2）。
- 偶发"分析脚本染红棘轮"的根因已由扫描面收敛解决；若后续出现 `scripts/` 下的一次性脚本，仍会计入 —— 这是刻意的（`scripts/` 是生产可见面）。