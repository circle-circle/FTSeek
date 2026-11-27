# DEB-20261127 · DA 标的与方向审查建议复核及 P0 正确性修复验证

状态：**P0 正确性修复已验证并部署 · A2 内容绑定版本补修已部署 · 策略建议未实施**
创建：2026-11-27
代码提交：`7c5556ef2f48a3511e60434c94e5ff59a08bca85`
第二轮代码提交：`d5051134f98bd8b60588ec0775b0bc1655a9c853`（A2 内容绑定版本）
关联：`docs/debates/DEB-20260902-stale-bridge.md`、`docs/OKR-2026-11.md`、`docs/decisions/RFC-215-faithful-execution-axis-closure-and-sltp-preregistration.md`、`docs/decisions/RFC-217-no-trade-short-sim-direct-verification.md`

## 1. 背景与授权

用户要求复核另一 Agent 的 DA 优化建议，通俗解释变更逻辑，复查合理性与收益，并按建议执行优化、重启、部署、提交、推送到主线。

本轮只落地已隔离复现的 **P0 正确性修复**：bridge 启动刷新基线、OKR 评分费用/净额 fail-close、CI 文案读取真实上下界。不改方向配比、追价、隔夜强平、扩池、prompt、实验 cohort、live 权限。

另一 Agent 声称已落档 `docs/debates/DEB-20261127-da-selection-direction-review.md`，本地不存在；本文作为独立复核与部署凭据。另一 Agent 称本机无 `pytest` 不准确：`./venv/bin/python -m pytest` 为 8.3.5。

## 2. 通俗结论

- **bridge 缺口**：服务启动时如果已经加载今天的候选文件，也要记住“这是哪个版本”。否则同日 DA 重跑、文件更新后，内存可能继续用旧候选。修复后，启动加载和运行期重载使用同一版本基线；文件 mtime 变化才刷新，未变化不重复读盘。
- **评分缺口**：不知道费用或净额时，不能把费用当 0、把毛收益当净收益。修复后，缺失即不可测；日等权 CI 的说明必须读取真实上下界，不能把整体为负的区间写成“下界 >0”。
- **收益边界**：这些是“尺子和供给正确性”修复，不是 alpha。官方归档 DA 整窗净收益仍为负，修复不证明盈利改善。

## 3. 已实施 P0 修复

### 3.1 bridge 启动刷新基线

- `app/playbook/pipeline.py` 新增 `_record_bridge_loaded_baseline(bridge, mtime=None)`：仅对 exact `BridgeVotingAdapter` 且带 `_trade_date` 记录 `_bridge_reloaded_date/_mtime`。
- `_reload_bridge_if_landed` 成功路径复用该 helper。
- `app/core/scheduler_core.py` 启动加载前捕获 `_bridge_mtime_before`，加载并挂载成功后记录基线，避免已有当日文件在加载期间被重写时漏掉刷新。
- 契约测试：S8 启动基线后同日刷新、S9 mtime 不变不重读、S10 helper 忽略非 Bridge/缺日期、W4 scheduler 启动布线顺序。

### 3.2 OKR 评分 fail-close 与 CI 文案

- `scripts/okr_2026_11_scoring.py` 新增 `_row_net`：优先 `pnl_net`；否则仅当 gross/fee 均已知才算 gross-fee；否则 None。
- `window_stats` 增加 `gross_missing_n/fee_missing_n/net_missing_n`；缺失时 gross/fees/net/bps/net_per_trade/日聚类 fail-close。
- `kr1_verdict` CI 文案读取真实上下界，明确“日等权净 bps，非名义加权点估计”；保留正下界测试所需 substring `CI 下界 >0`。
- `_sum_net` 改 Optional；KR2 笔均净额、exit breakdown 对未知净额 fail-close。
- 额外修复：`exit_reason_breakdown` 原排序遇 `net=None` 会 TypeError；现 unknown 排最后。
- `collect_and_score` 对 DA fee/net 缺失加 KR5 warning。

## 4. 未实施建议与理由

- **P1 DA 原计划/执行/成交血缘披露**：方向合理，但属审计/展示契约，需先冻结真实快照并明确消费方；本轮不改执行语义。
- **P1 波动风险与涨跌方向分离**：合理研究口径，不新增无人消费字段，不改 regime 门。
- **P1 输入真实被消费**：资金面 code、短历史 UNKNOWN、空财报/失败呈现、来源时效等仍挂账；正式输入调整会改变信号源，须按 R2/预注册队列推进，不能包装成零行为小修。
- **P2 供给漏斗与交易经济性**：先定位未成交停在哪一层，核真实点差/报价连续性/借券/完整成本/共同暴露；不机械扩池、不抬价格地板、不放大票面、不恢复 UE 打单。
- **明确不做**：严格原价执行（RFC-215/C16 关闭）、50/50 方向配额、小样本砍 LONG/放大 SHORT、隔夜强平（C17）、恢复 UE 打单（C18）。RFC-217 SHORT 实验仍按原 cohort/停止判据；未来 SHORT live-arm 前需补跨 regime 实验标记隔离。

## 5. 收益与证据边界

官方归档 `/tmp/ftseek-audit-okr-20261125.json` 与修复后复跑 `/tmp/ftseek-audit-da-20261127-okr-after.json` 核心读数一致：

| 人群 | n | 毛 bps | 费 bps | 净 bps | 判定 |
|---|---:|---:|---:|---:|---|
| DA 整窗 | 146 | −15.189 | 9.6479 | **−24.8369** | NOT_MET |
| post ≥11-17 | 39 | −6.4763 | 10.9915 | **−17.4678** | UNDERPOWERED |

KR2 死单率 21.2329% MET。当前 DA cohort fee/net/gross missing 均 0，因此评分 fail-close 修复不改变当前官方读数。post 子窗胜率 82.05%、均赢约 $19.40、均亏约 $125.81，一次亏损约抵消 6.5 次盈利；高胜率不等于正期望。

bridge 缺口已隔离复现，但生产发生次数和收益影响未测量。正式 DA 推理在仓外，本文不评价 prompt/challenger 本体。

## 6. 验证实录

- 针对性：`tests/test_stale_bridge_guard.py tests/test_okr_2026_11_scoring.py tests/playbook/test_no_trade_short_sim_experiment.py` → **109 passed**，日志 `/tmp/ftseek-audit-da-20261127-targeted-after.log`。
- 正确性相邻：DEB-20261126 §5 主命令 → **229 passed / 7 deselected / 2 warnings**，日志 `/tmp/ftseek-audit-da-20261127-correctness.log`。
- 行为/LIVE 相邻：DEB-20261126 §7.5 八文件 + daily_loss/live_arm P0/waiver/admission/entry_data_bounded_fetch → **455 passed**，日志 `/tmp/ftseek-audit-da-20261127-behavior.log`。
- 修复后探针：bridge 同日刷新/幂等、评分缺费缺净不可测、负 CI 不误标、unknown exit breakdown 安全 → `/tmp/ftseek-audit-da-20261127-probes-after.log`。
- 静态：5 个 touched py `py_compile`、`scripts/check_undefined_names.py`、`git diff --check` exit0 → `/tmp/ftseek-audit-da-20261127-static.log`。
- 已知测试污染豁免：生产库 `data/ftseek_us.db` 在测试中被写 5 次（capital_state 等），非本批新增。

## 7. 部署凭据

- 重启前预检：`/tmp/ftseek-audit-da-20261127-pre-restart.json`。SIM 未平 BA SHORT 23 / BABA SHORT 47 / GS SHORT 5；LIVE 无仓；两套出场账本非 BOOKED intent / 非 TERMINAL leg 均 0；`git status --porcelain -- app/ scripts/` 为空。
- `systemctl restart ftseek-us` 后 MainPID `144451`，启动 `2026-11-27 10:08:45 CST`，active/running，NRestarts=0。
- 启动冒烟：`PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90` → **7/7 exit0**，日志 `/tmp/ftseek-audit-da-20261127-startup-smoke.log`。
- 重启后复核：`/tmp/ftseek-audit-da-20261127-post-restart.json`。三笔 SIM 持仓 id/方向/数量/入价/入时不变，LIVE 仍无仓，出场账本仍 0；三处生产源码工作树 SHA256 与 HEAD blob 一致。
- 保留其他会话 WIP：`docs/debates/DEB-20261125-flat-all-empty-spin-and-deploy-ledger-reconciliation.md` 未提交。
- 推送：`git push origin main`  fast-forward `b914d65c9..e4e802a9c`，远端 main 与本地 HEAD 一致。

## 8. 未验证与下一步

- 未跑全量 pytest/GitHub Actions；未做生产自然 bridge 同日重写命中统计、完整 tick/订单簿回放、收益显著性检验。
- ~~bridge 版本基线仍基于 mtime，不是内容哈希；同 mtime 重写不保证检测。~~ **已于第二轮 §9 补修为内容 sha 绑定。** 正式 DA 推理代码/prompt/模型参数仍需纳入可版本化发布清单。
- 下一步仅观察自然链路与官方评分；不人工造单、不清 UNKNOWN 锁、不重置峰值/累计止损、不调方向/阈值/live 权限。11-30 正式结算不延期。

## 9. A2 内容绑定版本补修（2026-11-27 第二轮）

### 9.1 触发与授权

用户要求读取 `docs/proposals/2026-11-27-da-profitability-agent-execution-handoff.md`，做验证检查分析并给出落地修复。提案 A0 步骤 3 明确：**若当前代码已修复，报告断言结果，停止重复修改**。据此本轮先独立复核三个问题：

- **问题 1/2（评分）已修复**：按提案 A0 原样合成复现（`/tmp/ftseek-handoff-20261127-a0-scoring-repro.log`）——缺费缺净 → `fees=None/net=None/verdict=NOT_MEASURABLE`；n120 名义加权 +9.0188bps 但日等权 CI=[−103.0976,−74.9024] → 文案如实写「日等权 CI 整体 <0…不得宣称稳定 edge」，`MISLABEL_LOWER_GT_ZERO=False`。**本轮未改评分代码**（A1 已在第一轮完成）。
- **问题 3（bridge）有残留缺口**：第一轮基线绑定的是**探测期 mtime**，不是实际读入内容。对照提案 A2 设计要求逐条核对，要求 1/4/5/6 与迁移项未满足（见 9.3）。这是真实正确性缺口，非重复修改，故实施最小补修。

### 9.2 通俗解释

第一轮修复让服务「记住加载过哪个版本」，但记的是**去看文件那一刻的 mtime**，不是**真正读进内存的那份内容**。三个后果：

1. **同 mtime 重写漏检**：DA 若保留 mtime 重写内容（或文件系统时间粒度内重写），mtime 不变 → 永远不刷新。
2. **探测/读取竞态错误归属**：先探 mtime，再读文件；若两者之间文件被替换，读进的是新内容，基线却记旧探测 mtime → 版本归属错误（虽偏安全侧，会多刷一次）。
3. **无稳定性校验**：探测、健康检查、加载三次独立读盘，并发写入时可能读到半截 JSON。

补修后：loader **一次读字节** → 读前后 `stat` 比对（mtime_ns+size 一致才算稳定）→ 解析**同一份字节** → 算 `sha256` → 校验通过后才注入动态池 → 把 `sha/mtime` 附在 adapter 上。基线记 **adapter 携带的读入版本**。运行期以 **内容 sha 为权威**：sha 未变不重读（即便 mtime 被 touch），sha 变才刷新；旧 adapter 缺 sha 时退回 mtime 判定，一旦刷新即建立 sha 绑定（迁移，不永远返回）。

### 9.3 A2 设计要求逐条对照

| A2 要求 | 第一轮 | 第二轮 |
|---|---|---|
| 1 版本绑定实际读入内容 | ✗ mtime 代理 | ✓ adapter `_source_sha256` |
| 2 启动/运行期同一加载语义 | ✓ 共享 loader/recorder | ✓ 同（scheduler_core 未改） |
| 3 禁「读旧内容采新 mtime 标已读」 | ~ 默认回退路径有隐患 | ✓ 基线取读入版本 |
| 4 探测≠读取不得错误归属 | ✗ 记探测 mtime | ✓ 记 adapter 读入 sha/mtime |
| 5 一致读取/稳定性校验 | ✗ 三次独立读 | ✓ `_read_bridge_raw` 单读+前后 stat |
| 6 稳定性校验在注入前 | ~ 仅 schema 校验 | ✓ stat 校验先于解析/注入 |
| 7 坏文件不消耗成功基线 | ✓ | ✓ 仅成功路径 record |
| 8 跨日/fallback/幂等/日期/schema | ✓ | ✓ 保留 |
| 9 完成标记/截止/尝试/已认领保护 | ✓ | ✓ 未触（supply gate 13 项绿） |
| 迁移：缺版本旧 adapter 不永远返回 | ✗ baseline None 恒返回 | ✓ 退回 mtime，刷新后建立绑定 |

### 9.4 修改范围

- `app/playbook/pipeline.py`：新增 `_bridge_reloaded_sha` 全局、`_bridge_content_sha`/`_read_bridge_raw` helper；`_try_load_bridge` 一致读取+稳定性校验+附 sha/mtime；`_record_bridge_loaded_baseline` 内容绑定；`_reload_bridge_if_landed` sha 权威守卫+迁移。
- `tests/test_stale_bridge_guard.py`：新增 `TestBridgeContentBoundVersion`（S11-S15）与 `TestBridgeConsistentRead`（S16-S18）；夹具补 `_bridge_reloaded_sha` 重置。**现有 S1-S10/D/W/I 钉零改动。**
- `app/core/scheduler_core.py` **未改**（adapter 自带版本，启动接线无需改；W4 源码顺序断言仍通过）。`scripts/okr_2026_11_scoring.py` **未改**（A1 已修）。
- 不在本批：强制重生成、改 complete 判定、同步全部 DB 指令、改写已认领/已成交、清 UNKNOWN/释锁（提案 A2 明列排除）。

### 9.5 验证实录

- 针对性三件：`tests/test_stale_bridge_guard.py tests/test_okr_2026_11_scoring.py tests/playbook/test_no_trade_short_sim_experiment.py` → **117 passed**（较第一轮 109 增 8 项新钉），`/tmp/ftseek-handoff-20261127-targeted.log`。
- 桥接+供给门：`tests/test_stale_bridge_guard.py tests/test_generate_supply_gate.py` → **57 passed**，`/tmp/ftseek-handoff-20261127-a2-bridge-supply.log`。
- 正确性相邻（DEB-20261126 §5 主命令）→ **229 passed / 7 deselected / 2 warnings**，`/tmp/ftseek-handoff-20261127-correctness.log`。
- 行为相邻（DEB-20261126 §7.5 八文件）→ **240 passed**，`/tmp/ftseek-handoff-20261127-behavior.log`。
- **变异验证**：临时撤回内容绑定（recorder sha 置 None）+稳定性校验 → S11/S12/S14/S18 **4 failed**（`/tmp/ftseek-handoff-20261127-a2-mutation-red.log`）；恢复后全绿。证明内容绑定与稳定性校验为承重改动。
- A0 评分复现（问题 1/2 已修断言）：`/tmp/ftseek-handoff-20261127-a0-scoring-repro.log`。
- 静态：`py_compile` pipeline+test、`git diff --check` exit0；真实 loader 调用方仅启动（scheduler_core）+运行期（pipeline）两处。
- 已知测试污染豁免：生产库 `data/ftseek_us.db` 测试中被写 5 次（capital_state 等），非本批新增。

### 9.6 部署凭据

- 重启前预检 `/tmp/ftseek-handoff-20261127-pre-restart.log`：HEAD `45bd4b9a3`，PID 147680，SIM BA SHORT 23 / BABA SHORT 47 / GS SHORT 5；`git status --porcelain -- app/ scripts/` 仅本批 pipeline.py；未结算已执行指令 0。
- `systemctl restart ftseek-us` → MainPID **147844**，启动 `2026-11-27 11:00:23 CST`，active/running。
- 启动冒烟 `PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90` → **7/7 exit0**，`/tmp/ftseek-handoff-20261127-startup-smoke.log`。
- 重启后复核 `/tmp/ftseek-handoff-20261127-post-restart.log`：三笔 SIM 持仓方向/数量不变，LIVE 无仓。当日 bridge 文件尚未产出 → TechnicalFallback 兜底，新 loader 缺失分支干净运行，无 Traceback/稳定性告警。
- 保留其他会话 WIP：`DEB-20261125-flat-all-empty-spin-and-deploy-ledger-reconciliation.md` 未提交。

### 9.7 未验证与收益边界

- **未验证**：生产自然「DA 同日重写 → 内存刷新」命中（本轮当日 bridge 未产出，无法实测自然链路）；全量 pytest/GitHub Actions；完整 tick/订单簿回放；收益显著性检验。契约由 S11-S18 隔离证明，不等于生产命中统计。
- **收益边界**：本批是候选版本正确性恢复（行为相邻），**不是 alpha**。官方 DA 整窗净 −24.8369bps NOT_MET、post −17.4678bps UNDERPOWERED 不变；刷新修复不证明盈利改善。提案阶段 B（冻结真实快照查收益损耗）/C（资金分布输入实验）需真实资料与预注册授权，本轮未做。
- 下一步仅观察自然链路与官方评分；不人工造单、不清 UNKNOWN 锁、不重置峰值/累计止损、不调方向/阈值/live 权限。11-30 正式结算不延期。
