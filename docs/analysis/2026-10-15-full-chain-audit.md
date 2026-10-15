---
title: 2026-10-15 FTSeek-US 全链路审计报告
status: active
module: cross
related_code: app/core/scheduler_core.py
related_okr: OKR-2026-10
tags: [audit, governance, playbook, economics]
updated: 2026-10-15
---

# FTSeek-US 全链路审计报告（2026-10-15）

> **性质**：5 路并行只读审计 + 主会话复核的综合落档。
> **基线**：分支 `rfc-060-phase1-hotfixes`，HEAD=`4e2e072e6`，工作树干净，全仓 0 未 push 提交。
> **方法**：仓库分支 / Playbook 链路 / 棘轮防线 / 治理待裁 / 策略经济性 五路 general-purpose agent 并行；全程只读（禁改文件、禁 git 写、禁 systemctl、禁 pytest、SQLite 一律 `mode=ro`）。
> **局限**：①未跑 pytest（防活体数据假红），测试面结论均来自静态检查与既有产物；②其中两路审计的安全分类器超时，其单源发现已标注，两条最高风险单源发现（P0-2、P1-1 的告警盲区）由主会话亲自复核属实；③本报告只做发现，未做任何修复。
> **复核补记（2026-10-15）**：主会话确认 P0/P1 主线与结构性判断成立，并修正三处口径：`capital_flow_daily_aggregate.py` 自身 rc=0、崩的是子进程 `capital_flow_replay.py`；10 月 75 行 = 74 笔已平 + 1 笔在持，且全部 `source_type=PLAYBOOK_SIM`（70 regular / 5 premarket-EXT，不是 70+5 两个 source）；「吞毛比 44%」的算式是 `fee/(gross+fee)`，若按常规 `fee/gross` 则为 78.5%，原文必须显式声明口径。

---

## 〇、总判

工程底座真实健康：链路闭环逐笔可证、历史纸面防线（VIX fail-close、3% 回撤熔断）确认修复且无残留、棘轮只降不升纪律零违反、资本面安全（回撤 -0.42%）。

但三个面有结构性病灶：

1. **监控有构造性盲区** —— 两条生产证据链失败 21–25 天（funnel cron 非零退出；capital_flow 父 cron rc=0 但子回放每次失败），error_alert 按构造看不见（只认 `] ERROR:` 字面量前缀，裸 traceback 不可见）。
2. **治理回流断裂** —— DEB 在写、裁决在出，但裁决不回写告警/判据字典/OKR/注册表；队列"出队"机制比"入队"弱一个数量级。
3. **策略经济性不支持毕业** —— 费用单点改善被 IC 恶化、信心倒挂加剧、KR1 判决面 BROKEN 抵消。

**总判证据锚**：监控面见 `scripts/error_alert.py:79`、`logs/short_supply_funnel_report.log:1`、`logs/capital_flow_agg.log:6`；治理回流见 `data/invariant_check_latest.json:9`、`data/loops/meta_audit/STATE.md:8`、`app/playbook/shadow_registry.py:1681`；经济面见 `data/ftseek_us.db` §六 SQL、`data/ic_analysis_latest.json:282`。

---

## 一、确认健康的面（证据面，不再展开）

| 面 | 证据 |
|---|---|
| 工作树/分支 | `git status -uall` 0 行；`git log --branches --not --remotes` = 0；`git ls-remote` 实证远端=HEAD |
| handoff 档案完整性 | `tail -n +11 docs/archive/handoff-archive-pre-2026-10-01.md \| sha1sum` = `4bff26a1698f69e4487d4cdc25854c588b6c3297`（与声明逐位吻合；整文件 sha1 因 10 行表头不同，校验须剥表头）；切档移除 120 标题 = 档案收纳 120 标题，无误吞 |
| 服务运行 | PID 1333304 自 10-15 08:33:36 CST 在跑，NRestarts=0；`.env.us`/`unified_config.yaml`/启动横幅三方自洽（sim + ALLOW_REAL_TRADING=False + futu_simulate）；RFC-135 waiver 已按 `expires 2026-10-12T16:00:00-04:00` 正常过期 |
| 架构锁定 | 入场 `run_playbook_auto_execute`（scheduler_core.py:2335）→ `playbook_service.py:1400 _execute_via_dual_broker`；出场快循环（scheduler_core.py:2940，interval=10s）→ `DualBrokerAdapter.close_position`；`_skip_execution_in_trading=True` 在位（signal_pipeline.py:222-223） |
| 链路数据面 | 近 5 交易日 42 笔 playbook 记录零重复零缺失；全库仅 US.CAT 一笔未平（id=1670）；durable exit ledger 181 intent 全 BOOKED / 297 leg 全 TERMINAL；跨重启接管有 10-15 03:52 实测证据（US.RIVN HELD→BOOKED） |
| 防线可达性 | VIX `None`→拒单分支真实可达（market_state.py 恒 return 20.0 伪兜底已删）；3% 回撤熔断双账户分叉与 >5x 突变后门确认无残留（capital_safety.py:216-229 仅剩注释）；knife_gate 有 234 次实拦 + 15 次触发实证 |
| 棘轮纪律 | 8 类棘轮实测全部 ≤ 基线；git 历史 40+ 提交基线只降不升（naive-now 458→435、except-pass 957→921）；`test_baseline_not_tampered_upward` 钉死禁调大 |
| 时钟病灶 | 交易主路径抽样 10/10 干净（受保护兜底/自洽同钟/纯标注/死路径），与"变种 1 主路径已收口"台账一致 |
| 资本面 | equity ≈99.3k，drawdown -0.4157%，max_dd -3.3312%，water_level green，距各级熔断线均远 |

---

## 二、P0 — 今日到期

### P0-1 KR3 SHORT 腿 n≥30 终裁今日（10-15）到期
- 实测 `data/shadow/kr3_direction_runup_gate.jsonl`：已结算 SHORT would_block=True = **恰好 30**，OKR 门达标日即今日。
- OKR 明文"确认或干净证伪都算本 KR 达成"；闹钟 `recheck_after=2026-10-17`，OKR 窗口 10-31 关闭。
- 动作：重跑 `scripts/kr3_final_adjudication.py`（脚本注释明写判分窗尾须随裁决日延长，支持 `--window-end`）+ 用户裁决 + 注册表回写（当前 `kr3_direction_runup_gate` 仍 OBSERVING、`last_evaluated=2026-10-04`、`current_n=0`）。
- **这是唯一"今天不做就产生成本"的裁决。**
- **证据锚**：`data/shadow/kr3_direction_runup_gate.jsonl`（2026-10-15 06:10 采集；SHORT ∧ would_block=true ∧ 已结算 = 30）；`app/playbook/shadow_registry.py:235`（read_at=10-04 / recheck_after=10-17）与 `app/playbook/shadow_registry.py:1681`（OBSERVING / current_n=0）；`scripts/kr3_final_adjudication.py:42`、`:177`、`:236` 支持 `--window-end`。

### P0-2 手动实盘"能开不能平"单向门（主会话已复核属实）
- 平仓侧：`app/web_dashboard/order/executor.py:409-413` —— `if service._is_live(): allow=ALLOW_REAL_TRADING; if allow != "true": 拒绝`（注释自陈为 2026-06-12 P0-2 刻意设计，防切回模拟盘后 broker_ctx 误下实盘单）。
- 买入侧：`executor.py:224-270` 无该闸；`manual_order_service._is_live()` 恒 True（手动看板固定走 REAL）。
- 现状：`ALLOW_REAL_TRADING=false`（运行进程 `/proc/1333304/environ` 实证）⇒ 看板可买真实仓、不可平真实仓。`ftseek_us_live.db` 的 `live_deal_records` 有 123 条真钱成交（如 10-14 HK.02513、10-08 HK.00700/HK.00100），真实持仓存在。
- **待用户裁口径**：手动平仓是否只走富途 App？若是，建议把该口径写进运维文档；若否，需修对称性。
- **证据锚**：`app/web_dashboard/order/executor.py:409`（平仓闸）、`:224`–`:270`（买入侧无闸）、`app/web_dashboard/manual_order_service.py:105`（手动固定 REAL）；SQL：`sqlite3 -readonly data/ftseek_us_live.db "SELECT COUNT(*), MIN(deal_time), MAX(deal_time) FROM live_deal_records"` → 123 / 2026-07-10 / 2026-10-14。

---

## 三、P1 — 高（建议本周清）

### P1-1 两条生产证据链静默失败 21–25 天，告警链按构造看不见（两路审计独立发现；主会话已复核）
- **`scripts/short_supply_funnel_report.py`** 自 2026-09-20 起每跑必崩：`:137` `float(t["pnl_net"] ...)` 遇 None 抛 TypeError（同函数 `:136` 有 `or 0.0`、`:137` 没有，笔误）。触发数据实锤：trade_records 有 US.MRCY/US.SNDK（2026-09-16，PLAYBOOK_SIM）双盈亏列皆 NULL。
- **`scripts/capital_flow_replay.py`** 自 ≈2026-09-24 起每跑必崩：`capital_flow_daily_aggregate.py:134-138` 子进程用裸 `python3`（无 numpy），父进程是 venv——与 10-08 已修的 pre_market_check 事故**同缺陷类**，当时只修单点未横扫。
- **后果**：`data/capital_flow_replay_verdict.json`（四关门证据，shadow_registry.py:679/689/693 所引）自 09-24 未刷新，依赖它的毕业/停采决策链在用过期证据；SHORT 漏斗报表同步停更。
- **复核修正**：`short_supply_funnel_report` cron 本身非零退出；`capital_flow_daily_aggregate` cron 自身 rc=0，但子进程 `capital_flow_replay.py` 每次 import 失败并把 traceback 打进父日志，因此后者不是普通 cron 崩溃，而是「父作业绿、证据链红」的静默失败形态。
- **告警盲区根因**：`scripts/error_alert.py:79` `LEVEL_MARKERS = ("] ERROR:", "] CRITICAL:")` 字面量前缀匹配；cron 日志的裸 Python traceback 无此前缀 ⇒ 每 15 分钟报"0 条 ERROR"是假绿。文件头自陈 daily_invariant_check 的 ERROR 也曾漏扫——同类盲区第二次发作。
- **证据锚**：`scripts/short_supply_funnel_report.py:136`–`:137`、`logs/short_supply_funnel_report.log:4`–`:7`（9/20 与 10/15 同栈）、`scripts/capital_flow_daily_aggregate.py:134`–`:141`、`scripts/capital_flow_replay.py:33`、`logs/capital_flow_agg.log:6`–`:9`（9/24 与 10/15 同栈）、`logs/error_alert.log:69`–`:71`（08:30 后仍 0 条）、`data/capital_flow_replay_verdict.json`（mtime 2026-08-22）。
- 修复须带三件套：①两脚本修复；②error_alert 增加裸 Traceback 判据；③全仓"子进程裸 python3"横向扫描。

### P1-2 handoff.md（唯一进度 SSOT）缺 2026-10-12/13/14 三个日期段
- 18 个提交中 17 个在 handoff 零引用：含 RFC-142（多模型审计修复批）、RFC-143（A10 现役时代口径 SSOT）、DEB-20261014（几何轴终审关轴）、RFC-099（推送呈现层）。
- 非切档所致：切档前基线 `5f7755a73:handoff.md` 最新日期段同样只到 10-11。
- 另：顶条"未 push（用户未令）"与事实相反——reflog 记 08:36:13 update by push（写完 1.7 分钟后已推）。
- 动作：纯 .md 追补批，不触发重启。
- **证据锚**：审计基线 `git show 4e2e072e6:handoff.md` 的最新历史日期段为 10-11；`git log --since='2026-10-12 00:00' --until='2026-10-15 00:00' --format='%H' | wc -l` = 18；`git reflog --date=iso --all | rg 'update by push'` 显示 `4e2e072e6` 于 2026-10-15 08:36:13 已推。

### P1-3 影子注册表对账批（军规级）
- 实测 **79 项**（AST 计数：OBSERVING 48 / KILLED 25 / SUPERSEDED 4 / GRADUATED 2）；军规与 PROJECT-PORTRAIT 仍写 69（且 PORTRAIT 路径 `data/shadow_registry.py` 写错，实际在 `app/playbook/shadow_registry.py`）。
- **在采未注册 = 军规明文"禁止态"，4 项**：`eth_held_exit_shadow`（有真实生产事件行，含 10-01 ACN 卡死事件）、`post_ca4487`（daily_invariant_check.py:334-342 在采，OKR 关联，注册表与判据字典双零登记）、`ext_entry_earnings_criterion_shadow`（10-15 07:55 仍写入）、`ue_long_momentum_gate`（低置信候选）。
- **逾期未结算 3 项**（军规"到期必须当日结算"）：`ue_short_boost_shadow`（recheck 10-10，承诺的结算器从未建，`NO_EVAL_SCRIPT` 挂账恰好掩盖逾期机制）、`eth_earnings_direction_path`、`rfc104_eth_event_plan`（同逾期 5 天）。
- **终裁未回写**：`eod_depth_gate`（10-14 已终裁，注册表 last_evaluated 停 09-26）、`kr3_direction_runup_gate`、`entry_anchor_dual`（样本 1120 ≥ min_n=300，缺结算器）。
- **结构缺口**：注册表有 check_mounting/check_artifact_consumers/check_intent_uniqueness 三类正向校验，**没有"已接线在采但未登记"的反向校验**——这是 4 项禁止态长期逃逸的根因。
- 旁证腐化：`data/shadow/pending_adjudication_state.json` mtime 停 10-10（缺 entry_confirm_forward），与 `data/reviews/criteria_ready_state.json`（10-15 新鲜）两套状态并存不同步。
- **证据锚**：注册表 import 实测 79 = OBSERVING 48 / KILLED 25 / SUPERSEDED 4 / GRADUATED 2；未登记写方见 `app/playbook/pipeline.py:3330`、`scripts/daily_invariant_check.py:218`、`scripts/extended_hours_entry_executor.py:333`、`scripts/llm_universe_scan.py:425`；登记过期/漏回写见 `app/playbook/shadow_registry.py:1681`、`:2100`、`:2958`；旧口径见 `docs/PROJECT-PORTRAIT-20261004.md:339`。

### P1-4 OKR-2026-10 回写（距窗口关闭 16 天）
- KR1 `oct_cohort` 实测 BROKEN：exit 哈希 10/08 换版（exit-34c7153088a1 n=106 → exit-ca4487172a6f n=38，另有 null n=4），n_unique=2 ≠ 文件 L17 的"唯一值数=1"；DEB-20261009 §30 已于 10-10 裁方案 A（KR1 终局 BROKEN、开 post_ca4487 观测窗），**但 OKR 文件零回写**（post_ca4487 命中 0），变更记录表只有 10-04 一行。
- `daily_invariant_check.py:146-151` 注释明写"移动本常量=改判据面，必须先落 DEB + 改 OKR"——DEB 落了，OKR 没改。**机器自己写下的规矩，机器自己没执行。**
- 落地债：extended_hours_entry 链缺 config_version 打标 4 行；`okr_2026_10_scoring` 判分模块仅有文档引用、从未构建。
- **证据锚**：`data/invariant_check_latest.json:9`（oct_cohort broken=1）、`:11`（W4=1）、`:12`（W7=38）；`docs/OKR-2026-10.md:17`–`:28`（唯一值数=1 判据与 SQL）、`:73`（变更记录止于 10-04）；W1 方案 A 裁决见 `docs/debates/DEB-20261009-event-to-trade-pipeline.md:697`、`:1956`、`:1965`、`:2024`。

### P1-5 指针修复批
- `AGENT.md:3` 仍写"当前阶段（2026-08-24→09-30）按 OKR-2026-09.md"——过期 15 天，与 `AGENTS.md:4`（正确指向 OKR-2026-10）互相矛盾。
- `CLAUDE.md` 不存在（AGENTS.md 自认"本批未建"）；本会话实测 AGENTS.md 被 Claude Code 自动注入 ⇒ 接线通了但接到的权威目标内容过期。
- 宪章 `multi-agent-collaboration-charter.md:16` 标题"七律" vs 实际 9 条（AGENTS.md 用"九律"）。
- `docs/ADJUDICATED-CRITERIA.md` B19 行仍 KEEP_SAMPLING，而 10-14 DEB 已终裁 GATE_NOT_WORTH——字典对外提供过期判据。
- RFC-136 头 L4 状态与自身 §D 矛盾（"execute 未开" vs "10-14 转正"，crontab 已挂 --execute 两行）；RFC-123 状态"提案" vs 影子已跑 1120 行。

### P1-6 W4 棘轮当前是红的
- `app/review/daily_review.py:679` 新增一处 `except → logger.debug`（9→10）；独立 AST 复算与 10-15 05:05 夜间自检（`W4_debug_swallow_grew: 1`）双向证实。
- `test_w456_log_hygiene_ratchet.py:347-360` 对真仓库有断言 ⇒ 下次全量 pytest / CI 必红。
- 处置：一行修复（`logger.warning(..., exc_info=True)`）或落档后登记基线；**不要静默改基线**。

---

## 四、P2 — 切 REAL 前必清

| # | 事项 | 证据 |
|---|---|---|
| P2-1 | **探针假绿风险**：`scripts/probe_pl_val_currency.py:43-45` 读 `FUTU_REAL_ACCOUNT/FUTU_SIM_ACCOUNT`——两键在仓库与 .env.us 均不存在（真实键名 `REAL_ACCOUNT_ID`/`SIMULATE_ACCOUNT_ID`），acc_id 退化为 0，探的不是目标账户。防御本体（pre_trade_gate.py:222-235 非 USD fail-close）是好的，缺可信探针 | 全仓键名检索 |
| P2-2 | **日亏熔断 SIM 单例残留**：`app/playbook/pipeline_parts/risk_gates.py:31-40` 恒用 `get_capital_safety_system()`（=SIM 单例）判所有臂；同族 OKR 熔断已正确按臂切换（`app/playbook/playbook_service.py:1054-1060`）。当前零影响，切 REAL 前必须收口 | 与历史双账户 bug 同族 |
| P2-3 | `risk.preflight_fail_mode: open`（yaml:1897）在 LIVE 启动断言（di_container.py:171-179）里必须 closed——保持 open 会让服务**拒绝以实盘启动**。好事，但须进切换 SOP，否则"顺手改 AUTO_TRADE_ACCOUNT=real"直接把服务顶死 | |
| P2-4 | **推送纪律侵蚀**：8-29 后净新增 8 个绕开 app/push/ hub 的直连（app 层 1 个 `live_deal_push.py:147`=真钱成交秒级提醒，commit 6be1a00c7；scripts 层 7 个）；"存量 5 个旁路"口径失真（真实面 = 7 webhook sink + 3 shell curl + ~30 直连点）；3 个脚本无条件推（da_glm_shadow_eval / da_prompt_distill_shadow_eval / deb20260901_shadow_first_report） | git log + grep 复核 |
| P2-5 | **HTB 借券费敏感性从未跑**（9/24 承诺"裁决前须跑"未履行）；EXT 隔夜空单 US.ACN 无任何借券成本入账；BORROW_FEE_MIN_DAYS=0 ⇒ 日内 int(hold_days)=0 ⇒ $0 | metadata_json 无 borrow 字段 |
| P2-6 | **盘后追踪重复推送**：内存幂等（`app/playbook/pipeline_parts/tracking_flow.py:25`、`:43`–`:52`、`:150`）+ `app/push/dedup.py:15`–`:47` 仅 60s 纯内存 ⇒ 持夜仓+重启必重推；10-14 盘后追踪在 04:01/07:29/07:32 推了 3 次。需持久 dedup | 推送调用 `tracking_flow.py:145`–`:148`；`app/playbook/pipeline.py:3536` 是同族重跑/幂等注释，不是本问题的主证据 |
| P2-7 | RFC-127 阶段1（ETH held exit RTH 升级）仍未授权；阶段0 record-only 在跑（但未注册，见 P1-3） | |

---

## 五、P3 — 结构性 / 战略级（DEB 级议题，非补丁）

1. **策略经济性**（见第六节）。
2. **event_stream.db 无界增长**：复核时 913MiB / 3534 行；`raw_events` 947,013,776 bytes，其中 Google News RSS 1650 行、均值 523.4KiB、合计 843.4MB；近 4 个完整日新增 158.0–159.7MB/日；`store.py` 无保留策略；叠加 `db_backup.sh` 7 天全量 ⇒ 稳态约 6.4GiB 且无上界。需裁决保留窗/降采样。
3. **棘轮双执法面互不重叠**：naive-now/except-pass/utc4/pipeline-size 仅 CI；W2/W5/W6 仅夜间 cron（W2/W5/W6 对真仓库无 pytest 断言）——任一面停摆无人知。pipeline.py 行数棘轮基线 10207 vs 实际 3578（65% 余量，实质失效，拆分收口时应下调）。
4. **熔断地图失真**：circuit-breaker-map.md 的 L1 阈值写错（写 -2%/-3%，实际 green -5/yellow -10/orange -20/red -35）；VIX 面、playbook -3% 日亏闸、pre_trade_gate、symbol 级两道均未收录；六套日亏损权威并存（symbol_daily_loss_guard.py:22-38 已登记，合并属人工裁决）。另两处语义纸面：L1.5 "LIQUIDATE" 与 HALT 同效（只拒新仓）；`app/playbook/risk_circuit_breaker.py` 名如熔断实为周报 record-only。
5. **main 落后基线 51 提交/6 天**（复核时 52，差额为本报告提交；靠人工 reset 前移，末次 10-09）；rfc-060-phase1-hotfixes 实为事实主干且分支名是孤儿（RFC-060 文件已于 2026-07-03 随 77852d50f 删除）。merge-base==main ⇒ 可安全 FF。
6. **净口径双实现潜伏**：报表/风控侧 `COALESCE(pnl_net, realized_pnl)`（回退毛额，7d9a800fa 起）vs LLM/费用门侧 `COALESCE(pnl_net, realized_pnl - COALESCE(fees_est,0))`；当前全库 delta=0（仅 6 行 pnl_net NULL 且无费），一旦出现"有费但 pnl_net 为 NULL"即系统性高估净利。
7. **chase_cap enforce 存疑**：enforce=ON 下 10-13/10-14 有数笔越线遥测（如 US.CRM 239.25 vs 禁追线 238.00）——enforce 到底拦没拦需查。
8. **治理回流断裂 5 形态**（见第七节）。
9. 度量盲区清单：`playbook_horizon_stats` 停更 8 天（末 10-07）；`drawdown_state.db` 悬挂（末行 09-09，写方疑迁 capital_state_history）；`short_funnel_shadow` 停 7.1 天、`ue_short_boost_shadow` 停 16.4 天（空头族证据断流）；`long_expectancy_ledger` 停 6 天（末 10-09）；仓内无 DA dc 台账（在仓外 /root/.hermes）。
10. 卫生项：`.env.us` 残留废弃键 `FUTU_TRADE_ENV`（每次启动刷 WARNING）；`data/exit_ledger.db` 孤儿空库（10-08，零引用）；data/ 下 487 个 *.bak*（168MB）可回收。

---

## 六、策略经济性（对"毕业"问题的回答）

**结论：当前数据不支持加速 sim→live 毕业；证据比 9 月更弱，唯一变强的是费用。**

### 改善面
- 费用占比（复核口径 `fee/(gross+fee)`）：99.9%（9/17–9/30）→ **44.0%**（10 月）；若按常规 `fee/gross`，10 月为 **78.5%**，不能混称"费用占毛利"。单笔费用均值（notional bps）：9 月 15.6 → 10 月 10.2。（旧口径"75.4%"可复现：7/09–9/24 = 73.9%）
- 10 月已平仓净额转正：**75 行 = 74 笔已平 + 1 笔在持**，已平合计毛 +539.15 / 费 +423.33 / **净 +115.82**。所有行均为 `PLAYBOOK_SIM`；拆分是 70 regular 净 +36.73、5 premarket/EXT 净 +79.09，不是 70+5 两个 source_type。
- OKR bps 尺（`scripts/okr_status.py:87`–`:102` 同口径）：9 月 **-5.52** → 10 月 **+1.32** 净 bps/笔。
- premarket/EXT 空头臂 2/2 净正（ACN +19.44 / GS +12.77）。按 `trade_records` 复算，10 月 LONG 为 7 月以来首个净正月：n=56、净 +40.24、胜率 0.768；原报告的 `long_expectancy_ledger n=41 / +469 / 0.902` 未能在现存 `data/long_expectancy_ledger_latest.json` 复现（该产物无月度拆分），降级为待证。

### 恶化面
- **拆窗即穿帮**：改善全在 10/08 前；10/09 出口哈希换版后毛 **-17.13** / 净 **-27.69** bps/笔（n=28）。
- **信心倒挂加剧**：按 `trade_records.signal_confidence` 可复算口径，0.7 档从 9 月最佳（+15.31 bps）反转为 10 月最差（-7.66 bps）；0.5 档持续为正（+14.95→+15.21）。原报告的 9 月 +20.24 未能从当前库/产物复算，降级为待证。
- **IC 继续侵蚀**：月度 ic_t1 7 月 -0.0321 → 8 月 +0.0311 → 9 月 +0.0828 → **10 月 -0.1228**（多 -0.0974/空 -0.1551）；`technical_breakdown` 当前 IC=0.0934。原报告的 9/23=0.191 未在现存产物复现，降级为待证。
- **KR1 判决面 BROKEN**：n=200 单版本毕业证据链中断重计；干净观测面 post_ca4487 仅 n=28。
- **两个闸门拦反**：loss_breaker 被拦单 expectancy **+43.78**（毕业门 ≤-10，近 30 天 +240.16）；edge 门 7d BLOCK +$62.42 vs PASS -$10.52（30d 同向）——数据不支持实装 edge 门。
- **TP 死规则维持**：10 月 TAKE_PROFIT 触发 0 次。
- 失血点集中：OVERNIGHT_GAP 3 笔净 -609.4bps（毛 -981.12，10 月最大失血）；SL_INITIAL -252.2bps。
- 金字塔影子：n_executable 63（≥40 门过）但 leg_net_median **-0.28** ≤0 ⇒ KEEP_SAMPLING（预注册 MDE≈效应量，样本远不够）。

### 判语
费用这个第一杠杆确实被压下去了，但换来的是 10/09 后负毛利 + 信心分层反转 + 出口口径重开 + IC 恶化。建议在 post_ca4487 面攒满可判决样本、且月度 IC T+1 停止恶化之前，不把"10 月已平仓净 +115.82"当作毕业证据。

**经济性复算 SQL（`sqlite3 -readonly data/ftseek_us.db`）**：
```sql
-- 10 月总量：rows=75, closed=74, gross=539.15, fees=423.33, net=115.82
SELECT COUNT(*) AS rows,
       SUM(CASE WHEN realized_pnl IS NOT NULL THEN 1 ELSE 0 END) AS closed_n,
       SUM(realized_pnl) AS gross,
       SUM(COALESCE(fees_est,0)) AS fees,
       SUM(COALESCE(pnl_net,realized_pnl)) AS net
FROM trade_records
WHERE trade_date>='2026-10-01' AND source_type='PLAYBOOK_SIM';

-- 10 月 regular / premarket-EXT 拆分：36.73 / 79.09
SELECT entry_session, COUNT(*) AS n,
       SUM(COALESCE(pnl_net,realized_pnl)) AS net
FROM trade_records
WHERE trade_date>='2026-10-01' AND realized_pnl IS NOT NULL
GROUP BY entry_session;

-- OKR bps：9 月 -5.52 → 10 月 +1.32
SELECT AVG(COALESCE(pnl_net,realized_pnl)/(entry_price*quantity)*10000) AS net_bps
FROM trade_records
WHERE trade_date BETWEEN '2026-09-01' AND '2026-09-30'
  AND realized_pnl IS NOT NULL AND entry_price*quantity>0;
SELECT AVG(COALESCE(pnl_net,realized_pnl)/(entry_price*quantity)*10000) AS net_bps
FROM trade_records
WHERE trade_date>='2026-10-01'
  AND realized_pnl IS NOT NULL AND entry_price*quantity>0;

-- 10/09 后：n=28, gross_bps=-17.13, net_bps=-27.69
SELECT COUNT(*) AS n,
       AVG(realized_pnl/(entry_price*quantity)*10000) AS gross_bps,
       AVG(COALESCE(pnl_net,realized_pnl)/(entry_price*quantity)*10000) AS net_bps
FROM trade_records
WHERE trade_date>='2026-10-09'
  AND realized_pnl IS NOT NULL AND entry_price*quantity>0;

-- 信心档：9/10 月 0.7 = +15.31 / -7.66；0.5 = +14.95 / +15.21
SELECT ROUND(signal_confidence,1) AS tier, COUNT(*) AS n,
       AVG(COALESCE(pnl_net,realized_pnl)/(entry_price*quantity)*10000) AS net_bps
FROM trade_records
WHERE trade_date BETWEEN '2026-09-01' AND '2026-09-30'
  AND realized_pnl IS NOT NULL AND entry_price*quantity>0
GROUP BY ROUND(signal_confidence,1);
SELECT ROUND(signal_confidence,1) AS tier, COUNT(*) AS n,
       AVG(COALESCE(pnl_net,realized_pnl)/(entry_price*quantity)*10000) AS net_bps
FROM trade_records
WHERE trade_date>='2026-10-01'
  AND realized_pnl IS NOT NULL AND entry_price*quantity>0
GROUP BY ROUND(signal_confidence,1);
```
IC 证据：`data/ic_analysis_latest.json:282`（monthly_ic_trend）、`data/factor_ic_latest.json:102`（technical_breakdown）。

---

## 七、治理回流断裂（"半空转"的 5 种形态）

生成侧是活的（DEB 在写、不变量守卫在跑、meta_audit 10-15 07:20 仍 LOOP_DONE=FALSE 未决 8 项）。系统性失败的是回流侧：

1. **裁决出了，告警不关**：W1_oct_cohort_broken 10-10 已裁方案 A，P0 告警 5 天后仍 OPEN；W1_kr2_cohort_broken 实质已结（OKR §9 A1 已裁）但告警条目无人回写关闭；W7_chg_denominator P0/P1 逾期 11 天（责任人=用户）。
2. **裁决出了，判据字典不改**：B19 终裁 GATE_NOT_WORTH，ADJUDICATED-CRITERIA.md 仍 KEEP_SAMPLING。
3. **裁决出了，OKR 不改**：见 P1-4。
4. **裁决出了，注册表不更**：eod_depth_gate / kr3 / entry_anchor_dual（见 P1-3）。
5. **承诺出了，无人验**：ue_short_boost_shadow 结算器承诺跳票，NO_EVAL_SCRIPT 挂账恰好使逾期机制对它永久失效——挂账机制从保护变成掩护。

量化：6 项跨面回写失败（registry×3、criteria×1、OKR×1、alert×1）+ 3 项逾期 shadow + 4 项在采未注册。

另：F12（红盘日反事实）登记 09-24、原定 ≈10-01 触发，**全仓无任何执行记录，静默逾期 20 天**；F11/T3 解锁计时约 14/15 交易日，无跟踪。

**回流断裂证据锚**：

| 形态 | 证据 |
|---|---|
| 告警不关 | `data/invariant_check_latest.json:9`（W1 oct）、`:16`（W1 kr2）、`:20`（W7）；`data/loops/meta_audit/STATE.md:8`（LOOP_DONE=FALSE） |
| 判据字典不改 | `docs/ADJUDICATED-CRITERIA.md:47`（B19 仍保留 KEEP_SAMPLING 历史读数）；终裁 `docs/debates/DEB-20261009-event-to-trade-pipeline.md:2901` |
| OKR 不改 | `docs/OKR-2026-10.md:17`、`:73`；裁决 `docs/debates/DEB-20261009-event-to-trade-pipeline.md:697`、`:2024` |
| 注册表不更 | `app/playbook/shadow_registry.py:1681`（kr3）、`:2100`（eod_depth_gate）、`:911`（entry_anchor_dual）、`:2958`（ue_short_boost_shadow） |
| 承诺无人验 | `app/playbook/shadow_registry.py:2969`（eval_script=None） |
| F12 逾期 | 登记 `docs/debates/DEB-20260924-premarket-index-caliber-and-down-day-regime.md:298`、`:537`；全仓 `rg -n 'F12' scripts app data` 无执行产物 |

---

## 八、待用户裁决清单（汇总）

| # | 事项 | 急迫度 |
|---|---|---|
| 1 | KR3 SHORT n≥30 终裁（重跑 kr3_final_adjudication.py --window-end） | **今日** |
| 2 | 手动平仓口径（是否只走富途 App） | 高 |
| 3 | P1 修复批授权范围（cron×2 + error_alert + 裸 python3 横扫 / handoff 追补 / 注册表对账 / OKR 回写 / 指针批 / W4 一行修） | 高 |
| 4 | event_stream.db 保留策略 | 中 |
| 5 | main FF 合并 + 事实主干分支命名 | 中 |
| 6 | 六套日亏损权威合并/裁剪（symbol_daily_loss_guard.py:22-38 登记待裁） | 中 |
| 7 | RFC-104 阶段1 放行 / RFC-123 阶段1 / RFC-127 阶段1 授权 | 中 |
| 8 | F12 逾期 20 天的处置（补做 or 撤销） | 中 |
| 9 | edge 门数据不支持实装（7d/30d 同向拦更赚）——是否正式终止该方向 | 低 |
| 10 | C 类后续：MFE<0.3% 墓地族入场质量与费用结构两杠杆是否立项（DEB-20261014 §10.4 遗留） | 低 |

---

## 九、交叉验证矩阵（证据强度）

| 发现 | 来源 | 强度 | 复核证据 |
|---|---|---|---|
| 两条生产证据链失败 + error_alert 盲区 | 仓库路 + 链路路独立发现；主会话复核 | **强（多源+亲核）** | `scripts/error_alert.py:79`；`logs/short_supply_funnel_report.log:4`–`:7`；`logs/capital_flow_agg.log:6`–`:9`；`logs/error_alert.log:69`–`:71` |
| 手动实盘单向门 | 防线路发现；主会话复核 | **强（亲核）** | `app/web_dashboard/order/executor.py:409`–`:413`、`:224`–`:270`；`app/web_dashboard/manual_order_service.py:105`–`:111` |
| KR1 oct_cohort BROKEN / OKR 未回写 | 治理路 + 经济性路独立确认 | **强（多源）** | `data/invariant_check_latest.json:9`、`:17`；`docs/OKR-2026-10.md:17`、`:73`；§六 SQL |
| 金字塔 KEEP_SAMPLING | 治理路 + 经济性路读同一产物 | **强（多源）** | `data/shadow/trail_lock_pyramid_latest.json`（10-14 18:41；n_executable=63 / leg_net_median=-0.28） |
| ue_short_boost_shadow 停采/结算器跳票 | 治理路 + 经济性路 | **强（多源）** | `app/playbook/shadow_registry.py:2958`、`:2969`；`data/ue_short_boost_shadow.jsonl` mtime 2026-09-28 |
| 推送旁路 8 个新增 | 链路路 + 主审 grep | 中强 | `app/web_dashboard/live_deal_push.py:147`；scripts 层 grep / `git log` 复核（见 P2-4） |
| W4 棘轮红 | 防线路独立复算 + 夜间自检产物双向一致 | **强** | `app/review/daily_review.py:678`–`:679`；`data/invariant_check_latest.json:11`、`:19`；`tests/test_w456_log_hygiene_ratchet.py:347`–`:360` |
| 棘轮实测值（432/435、921/921 等） | 防线路独立 grep/AST 复算脚本 | 中强（未跑 pytest） | `data/bare_logger_baseline.json`、`data/except_stack_baseline.json`、`data/debug_swallow_baseline.json` |
| IC 月度 -0.1228 / tier 反转 / post-10/09 -27.69bps | 经济性路单源 | 中（单源，可复算） | `data/ic_analysis_latest.json:282`；§六复算 SQL（IC、tier、post-10/09 三段） |
| 熔断地图失真 / 六权威并存 | 防线路单源 | 中（单源） | `docs/circuit-breaker-map.md`；`app/risk/capital_safety.py:239`–`:276`；`app/playbook/pipeline_parts/risk_gates.py:21`–`:48` |

---

## 十、审计过程声明

- 5 路 agent 全程只读：零文件修改、零 git 写、零 systemctl、零 pytest、SQLite 全 `mode=ro`；临时复算脚本仅落 /tmp。
- 确认轮主会话额外复核：P0-2（手动平仓门）、P1-1（error_alert 与两条证据链）、P1-3/P1-4（注册表与 OKR 回流）、§六（SQL 复算）、§九（证据强度）。复核仍为只读：未改代码/配置/cron/DB，未 systemctl，未 pytest；SQLite 一律 `-readonly`。
- 确认轮修正保留在正文（不静默改写原判断）：cron 失败形态、10 月 75/74/1 与 PLAYBOOK_SIM/entry_session 口径、费用比公式、post-10/09 n=28、0.7 档 +15.31、event_stream 当前读数、main 落后提交数；`+20.24`、`0.191`、`long_expectancy +469` 三个无法复现的单源读数降级为待证。
- 本报告落档即 handoff 条目同步；**未修复任何发现**，所有处置待用户裁决。当前分支相对远端 ahead 2（报告提交 + 顶条 KR3 终裁提交）；审计基线仍为 `4e2e072e6`。
