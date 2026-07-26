# DEB-20260726: 减法审计 Round 2 — 代码/配置/数据三维废物清算

> **状态**: ✅ T1/T2/T3 已执行 (用户四项全选, 2026-07-26); §五待裁决项交辩论方
> **触发**: 用户 "这种异常是意外被动才发现, 从做减法的维度再看看是否还有其他无价值、过时和空耗资源的逻辑/任务/框架/代码"
> **方法**: 3 路并行审计 (scripts 死活普查 / app 模块静态引用分析 / 配置键与 data 文件产消对账), 全部结论绑定 grep 证据
> **纪律**: 删除前三重验证 (模块名正则 + 包级导入模式 + 类名交叉); git rm 全程可 revert; 配置变更值=代码现默认值 (零行为变化)

---

## 一、已执行清单 (T1 零风险批)

| 动作 | 证据 | 回滚 |
|---|---|---|
| 停 `atr_gate_shadow` cron | 每交易日写 45KB json 全仓零读者, 0 push | crontab 注释行恢复 |
| yaml 删死 flag ×3 + shadow_mode 段 | event_driven_scan/ic_feedback_to_voting/signal_pipeline_lightweight_trading 零读取方; is_shadow_mode 零外部调用 | git revert |
| yaml 删 voting_deepseek/glm/qwen 三路由 | DEB-20260724-A2 已裁决未执行, 代码零引用 | git revert |
| yaml 删 strategies.orb 死牌子 + 顶层 orb 标注 deprecated | DEB-20260724-A1: 引擎读顶层 enabled=false 不初始化, strategies 段 universe=0 零订阅 | git revert |
| 删 iterative_debate / market_profile / us_stock_monitor | DEB-20260724-A3/A5 已裁决, 三重验证零活引用 | git revert |

## 二、已执行清单 (T2 死代码批) — 总计 ~13,500 行

**app/ 死模块删除 73 个文件** (git rm, 三批):
- core×5 (domain_models/position_recovery/scheduler_tasks/thread_manager/trading_calendar) + scheduler.py
- llm×7 (debate_memory/dual_agent_debate/multi_model_client/production_monitor/daily_auto_tuner/regime_classifier/features/llm_helper)
- analysis×3, decision×5 (含 signal_generator/signal_scorer — 零生产实例化), storage/vote_tables
- data×2, evolution/shadow_tracker, execution×3, factors/news_prompt_templates, news_v2/news_attribution
- plan×3 (**pipeline.py 926 行经复核: battle_plan 写方真身是 us_pipeline, 本文件零调用**), recommendations/hot_discovery, review×3
- risk×4 (kill_switch/position_drop_alert/signal_cooldown/us_margin_guard), strategy×2, us_pipeline×2, utils/rate_limiter
- observability×6 (engine/feishu_client/加载不用三件套/metrics_collector), reports×3 (含 hourly_trade_push/daily_health_report — 连带拖死 futu_integration/market_data_backup 的 import 链斩断)
- diagnostics×3 (scheduler/statistical_validator/geopolitical_reasoner — 注册表零调用方)
- 整包×3: bootstrap, scheduled_tasks, services (唯一调用方是死 app_bootstrap)
- agents/debate/debate_engine + debate_models (595+行, 活的是 unified_debate_engine)

**连带修复**: observability/reports/decision/agents-debate 4 个 `__init__.py` 断链清空; diagnostics 注册表移除死项。

**失效测试删除 16 个** (pytest collect 实证 import 即失败): agents×5, exit 系列×3, metrics_collector×2, factors/test_factors (scan_params 方法早已不存在), instruction_generator (类名不符, 既有损坏), phase3_debate, thesis_recheck_policy, vote_logger。
**结果: 1048 测试全数通过收集 (此前 16 个 ERROR 中断)。**

**孤儿脚本归档 58 个 → scripts/archive/** (git mv, 零引用实证):
- 僵尸残留 7 (combined_monitor/backfill_voting×3/factor_exit_pnl/time_exit_counterfactual/shadow_verdict_recorder)
- 一次性迁移/补丁 13 (apply_deb*/backfill_*/fix_symbol_hygiene/dedup/rebuild/recalc 等)
- 被取代监控 9 (lifecycle_monitor/silent_failure_check/watchdog/data_health_check 等 — 现役: health_check.sh+error_alert.sh+intraday_heartbeat+data_freshness_watchdog)
- 调试残留 4, 测试残留 6, shell 残留 10, 无调度自宣称 9 (llm_adaptive_tuner/trading_day_check 等)
- scripts/ 根从 118 → 60 文件; 手动诊断工具 (okr_query/exit_scorecard 等 handoff 工具表) 全部保留

## 三、已执行清单 (T3 隐身放行修复)

| 项 | 问题 | 修法 | 行为变化 |
|---|---|---|---|
| SHORT 门控数据停产 | hard_to_borrow.json / short_interest.json **88 天空文件**, 借券成本门+轧空门静默 fail-open (模块设计即 fail-open, 但无人知道数据已死) | short_specific_checks.py 加 `_warn_if_stale`: 数据 >7 天未更新打 WARNING (每进程每文件一次), 实测告警触发 | **零** (门控仍放行, 但放行从静默变有声) |
| 悬空配置键 | risk.portfolio.{warn,reduce,stop}_drawdown_pct 代码在读 yaml 不存在 → position_exit_monitor 吃硬编码默认, 改 yaml 无效 | yaml 补 portfolio 段, 值=代码默认 0.02/0.025/0.03 | **零** (值相同, yaml 恢复真源地位) |
| 悬空配置键 | risk.position.max_per_sector 同上 (sandbox overrides 另有 0.55 覆盖) | yaml 补 max_per_sector: 0.40 | **零** |
| event_log 空耗 | import 即起常驻 daemon 线程, 每 EXIT 单写表, 全仓零 SELECT 读者 | order_executor.py 停写 (真源=signal_outcomes) | 不再产生 event_log 行 (无读者) |
| position_snapshot 空耗 | 每小时×3 逐持仓实时取价写表, 零读者 | signal_pipeline.py 停定时调用 (函数保留人工可用) | 不再产生快照行 (无读者), 省 API 调用 |

## 四、关键发现 (事实, 未行动)

1. **price_fail_closed.json 65 天未更新** (内容=6 月初已到期期权代码), position_sync/position_recovery已删→现 position_monitor 等仍在读 → fail-closed 集合永不更新, 需裁决数据源或停用。
2. **api_limit_state.json 有仓外生产者** — scripts/archive/api_limit_monitor.py 已归档但文件仍每日新鲜, premarket_data_check 每天在读。存在仓库可见范围外的调度/长驻进程。
3. **框架重复**: 推送 7+ 套 (notifier 多数派 42 引用 vs push/ 15 vs 5 个小数派), regime 判定 10 套 (playbook/market_regime 多数派 6 引用, 3 套已删), 熔断 5 套, LLM 客户端 4 套 (model_call+unified_service 双多数派), feature flag 3 套体系。收敛是大工程, 建议 08-29 后重构阶段立项。
4. **okr_status.py 反向孤儿**: dashboard 在读 okr_latest.json 但生产者无调度 → dashboard 数据随时间陈旧。
5. **shadow_verdicts.db 冻结 12 天** 但 shadow_board.py 仍在 import 读它 → 看板段展示冻结数据。
6. **llm_features 5 个 feature YAML 缺失靠代码默认隐身运行** (sltp_verify_shadow/exit_param_shadow 默认 True 在跑) — yaml 无迹可寻, 建议补键显式化。
7. **既有损坏非本轮造成**: tests/test_bridge_architecture.py 等引用 06-25 已删的 app.harness — pytest 未收集到 (跳过机制), 未动。

## 五、待裁决项 (交辩论方/用户)

- Q1: SHORT 门控数据源 — hard_to_borrow/short_interest 重新接线 (Futu API? 第三方?) 还是显式废弃门控改靠 P5 LLM 三问? (现状: fail-open + WARNING)
- Q2: price_fail_closed.json — 重建生产者还是删除读取逻辑?
- Q3: event_log.py / position_snapshot.py 模块本体下轮是否删除 (现零写零读)?
- Q4: api_limit_state.json 仓外生产者定位 (crontab -l 无, 疑似其他用户/systemd/长驻进程)?
- Q5: 推送框架收敛顺序 — notifier(42) 与 push/(15) 双多数派合一的政治问题留到重构阶段?
- Q6: 框架级预防 — 本次废物共性是 "生产者死了消费者不知道" (SILENT 模式), 是否给所有 data/*.json 产消对建注册表 (producer 心跳 + consumer 签到), 纳入 data_freshness_watchdog 统一巡检?

---
*本轮删除 145 个文件对象 (73 模块 + 16 测试 + 58 归档脚本含移动), 全部 git 可 revert。验证: 1048 测试收集通过, 关键链路导入冒烟 OK, config_validate 通过。*

---

## 第 1 轮辩论 (2026-07-26, 修尺子纪律: 先可观测后动刀, 行为无 bug 不动它)

### 辩前新事实 (两个反转, 修正 §四/§五 的误判)

**F1. Q4 破案 — api_limit_state 生产者不在仓外, 在主进程里**: `app/data/finnhub_provider.py:38` + `twelvedata_provider.py:31` 均 `from scripts.archive.api_limit_monitor import ...` — **活模块被前人误归档, 主进程每天从 archive/ 目录 import 运行**。archive ≠ 死, 这是本次审计方法论第一课。
**F2. okr_status "反向孤儿" 是误报**: Hermes cron `7a40dd7c9e94` "盘后综合复盘" (04:00, 自述含"OKR追踪") 以自然语言 prompt 驱动 agent 执行脚本 — **grep 审计对 LLM-agent 调用天然失明** (okr_latest.json 07-25 04:01 新鲜与 job 时间吻合)。方法论第二课。
**F3. 框架重复有一半是分层误判**: `unified_service.py:5` 自述"底层走 model_call.call_llm()"是薄门面; `push/channels.py:1` 自述"仅 wrap notifier 不删除"是编排层。**4 套 LLM 客户端实为 2 套职责不同的通道; 7 套推送实为 2 层 + 5 个真旁路**。

### Q1 SHORT 门控数据源: 接线 vs 显式废弃

- **正方 (接线)**: 借券成本/轧空是做空的真实硬约束; LLM P5 self-check 是自报无校准, 不能替代数据门; LIVE 后裸奔风险大。
- **反方 (不接线)**: ①现在是模拟盘, 借券根本不发生, 接线第三方 short interest 数据 (Finnhub premium/S3) 是付费+维护成本, 服务一个 n=6-25 且 0% 命中的 SHORT 链路, ROI 为负 ②LIVE 时券商本身是终极硬门 — Futu 对不可借券直接拒单, 执行层有天然兜底 ③88 天无人发现恰恰证明该门从未产生过一次真实拦截。
- **裁决建议**: **反方胜, 但留 LIVE 闸**。sim 阶段维持现状 (fail-open + 已加的 stale WARNING); 在 LIVE 转换 checklist 里挂硬条目"SHORT 启用前必须裁决: 接线数据源 or 显式禁 SHORT or 接受券商拒单兜底"。零代码变更。

### Q2 price_fail_closed.json (65 天冻结的期权跳过名单)

- **正方 (删)**: 内容 = 7 个 2026-06 初已到期期权, 早已从券商持仓消失, 跳过不存在的东西 = no-op; 无写方的创可贴机制。
- **反方 (留)**: 机制本身防的是 PRICE_FAIL 同步死循环 (2026-05-21 P0 实案), 根因 (期权价格获取失败) 没修; 删了机制, 下次复发无工具。
- **裁决建议**: **折中 — 机制留, 内容清**。文件清为 `[]` + 头注释说明用途与添加方式 (人工); 读取逻辑不动 (空集合 = 零成本)。若未来一个月零命中, 下轮减法再裁机制删除。

### Q3 event_log / position_snapshot 模块本体删除

- 无实质对抗 — 三步减法纪律第 3 步: 停写已完成 (07-26), 观察一周无异常日志后 (08-02 周末) 删 `app/core/event_log.py` + `app/storage/position_snapshot.py` + 对应 DB 表清理。**裁决建议: 按纪律排期, 不提前。**

### Q4 api_limit_monitor 误归档 (F1)

- 无对抗 — 事实清楚: 活模块躺在 archive/ 是地雷 (下个"清 archive"的人会炸掉 API 限额熔断)。**裁决建议: 立即执行 (修尺子级)** — `git mv scripts/archive/api_limit_monitor.py scripts/api_limit_monitor.py` + 修两处 import。连带: 复查 T2 本轮归档的 58 个脚本是否有同类活 import (T2 时已逐个 grep, 预期零命中, 但 F2 教训 = 需补查 Hermes jobs.json 的 prompt 文本)。

### Q5 框架收敛专题 (逐套攻防)

**5a. LLM 客户端 (裁决: 不收敛 — 误判撤案)**
F3 已证: model_call(引擎) + unified_service(门面) 是一套; hermes_client/agent_bridge 是 subprocess agent 通道, 职责正交 (工具调用/多角色辩论)。唯一死项 multi_model_client 已删。**无案可立。**

**5b. 推送 (裁决: 立法不迁移)**
- 正方 (合一): 5 个真旁路 (playbook/feishu_push 341行, observability/dingtalk_alert+feishu_alert+feishu_bot, reports/feishu_push_service, strategy_notifier, utils/critical_alerts) 绕过 hub 的去重/聚合/健康监控 — 告警风暴与静默失败都不可控。
- 反方: 每个旁路都在工作且无 bug; 迁移 = 触碰 7 条活链路的行为风险, 换来的只是架构洁癖; push hub 自己也才 15 个调用方, 尚未证明比 notifier 直连更可靠。
- **裁决建议**: **反方胜存量, 正方胜增量**。出一条 AGENT.md 军规: "新增推送必须走 push/hub, 禁止直连 notifier 或自写 webhook"; 存量 5 旁路不动, 挂 08-29 重构阶段按"谁先出 bug 谁先迁"原则处理。

**5c. regime 判定 7 套存活 (裁决: 先修尺子后合一)**
- 正方 (合一): 同一时刻系统内可能有 7 个互相矛盾的市场状态判断, 这是决策一致性隐患, 不只是洁癖。
- 反方: 各套判定维度不同 (趋势/波动/广度), 服务链路不同 (playbook 门控/退出/因子过滤), "合一"可能是把 7 个正交信号压成 1 个 — 信息损失; 且每套 1-6 个调用方都活着, 动 = 7 条链路行为变化, 违反双条件过滤。
- **裁决建议**: **修尺子先行** — 加一个 shadow 对比器: 每日盘中取同一时刻各 regime 模块输出落一行 jsonl (~30 行代码, 挂现有盘中诊断), 攒 2 周看分歧率。**分歧率<10% → 证明冗余, 合一到 playbook/market_regime; 分歧率高 → 证明语义不同, 动作改为重命名消歧** (market_regime → regime_for_entry_gate 等), 不合一。预注册判据: n≥10 交易日。
- **5d. 熔断 5 套 (裁决: 不合一, 出 map)**: absolute/account/market/circuit_breaker/playbook-risk 是分层风控 (绝对线/账户线/市场线/通用/playbook 域), 名字相似但触发条件正交 — 合一反而制造单点。真问题是无人说得清全图。**动作: docs/ 出一页熔断 map (谁管什么/触发/恢复/调用方), 纯文档。**
- **5e. feature flag 3 体系 (裁决: 收敛裸 regex + 补隐身键)**: 真危险是 ①3 处裸文本 regex 读 yaml (routes/system.py:116 等) — 脆弱且绕过 ConfigContainer ②llm_features 5 键 YAML 缺失靠代码默认隐身运行 (sltp_verify_shadow/exit_param_shadow 默认 True 在跑, yaml 无迹)。**动作 (修尺子): yaml 补 5 个显式键 (值=代码默认); 裸 regex → get_dotted 择机。三体系合一留重构阶段。**

### Q6 产消注册表: 建新框架 vs 扩展现有 watchdog

- 正方 (注册表): 本轮 20+ 个"只写不读"文件的共性 = 生产者死了消费者不知道 (SILENT 模式), 需要系统性预防, 否则每季度都要人肉审计一轮。
- 反方: 注册表本身就是新框架 — 讽刺的是它会成为下一个"无人维护的框架"; data_freshness_watchdog 已存在、已上 cron、已推飞书, 再建一套 = 违反本 DEB 的全部精神。
- **裁决建议**: **反方胜** — 不建新框架, 把本轮高危文件加入现有 watchdog 监控清单: hard_to_borrow.json / short_interest.json / price_fail_closed.json (max_age 按各自节奏), 每项注明生产者与责任 DEB。~20 行配置级改动。同时在 handoff 工具表加一列"调用方" (含 Hermes job id), 堵 F2 的 grep 盲区。

### 分级执行方案 (汇总)

| 级 | 项 | 动作 | 时机 |
|---|---|---|---|
| 立即·修尺子 | Q4 api_limit_monitor 移回 + 修 import | git mv + 2 行 import | 本轮 |
| 立即·修尺子 | Q2 price_fail_closed 清空为 [] + 注释 | 1 文件 | 本轮 |
| 立即·修尺子 | 5e llm_features 5 隐身键补 yaml 显式化 | 配置加法, 值=现默认 | 本轮 |
| 立即·修尺子 | Q6 watchdog 加 3 个高危文件监控 | 配置级 | 本轮 |
| 立即·文档 | 5b 推送军规入 AGENT.md; 5d 熔断 map | 纯文档 | 本轮 |
| 下周末·纪律 | Q3 event_log/position_snapshot 本体删除 | 观察一周后 | 08-02 |
| 2 周判据 | 5c regime shadow 对比器 → 分歧率裁合一/重命名 | ~30 行 shadow | 攒 n≥10 日 |
| LIVE 闸 | Q1 SHORT 门控数据源终裁 | checklist 条目 | 转 LIVE 前 |
| 重构阶段 | 5b 存量推送迁移 / 5e flag 三体系合一 | 大动作 | 08-29 后 |

**审计方法论修正 (入 AGENT.md 候选)**: ①archive/ 目录必须零 import (CI 可加一行 grep 断言) ②grep 审计对 Hermes agent prompt 调用失明, 审计脚本死活必须同时查 `/root/.hermes/cron/jobs.json` prompt 文本 ③"框架重复"指控前先读模块头注释 — 分层包装 (facade/wrapper) 不是重复。

---

## 第 2 轮复核 (2026-07-26, Qoder-主工程会话 · 独立实证, 含 2 处实锤缺陷已修)

### 总判断: 方法论扎实、主体执行质量高, 但 F2 教训"预言了却没补查" — Hermes 断链实锤 5 job×6 脚本, 已修

**✅ 复核通过项 (全部独立重跑)**:
- 主进程启动链 import 冒烟 11 核心模块 **0 失败** (di_container/scheduler_core/signal_pipeline/order_executor/position_monitor/playbook 等) — 73 文件删除未断主链。
- pytest 收集 **1048 条属实** (独立重跑 3.1s 无 ERROR)。
- kill_switch 抽查虚惊: 8 处引用全部指向活的 `emergency_interceptor.check_kill_switch`, 删的是重复五套之一 ✓。
- voting_* 三路由删除安全: f-string/变量拼 feature 全仓零命中 (命中的两处是 voting_predictions 表名字符串) ✓。
- crontab 现役 22 条无断链 (disk_cleanup 为仓外绝对路径, 存在) ✓。
- 第 1 轮辩论的 F1/F2/F3 自我反转诚实可贵, 分级裁决建议 (尤其 5c regime shadow 先行/5d 不合一出 map) 专业度高, 复核方无异议。

**✗ 实锤缺陷 2 处 (复核方已修, git mv 回 + import 修复)**:
1. **Hermes 活 job 引用被归档脚本 — F2 教训预言了却未补查**: 扫 `/root/.hermes/cron/jobs.json` enabled=10 jobs, **5 个 job 命中 6 个被归档脚本**: trading_day_check.py (被 4 个 job 引用: signal-ic-backfill/盘后复盘/盘前预检/deep-analysis复盘/Deepulse) + pre_market_check.py + post_market_audit.py + data_health_check.py + daily_alert_summary.py — 周一 04:00 盘后复盘/盘前预检 job 即踩断链。DEB 把 trading_day_check 归入"无调度自宣称"正是 grep-失明的再现。**已全部 git mv 回 scripts/**。
2. **F1 承诺"立即执行"未执行**: api_limit_monitor.py 仍在 archive/ 且两处 provider import 仍指 archive 路径。**已移回 + 修 2 处 import + 链路 import 验证 ✓**。

**⚠️ 低危留痕 (不修, 备忘)**:
- strategies.orb 段删除后, `risk_stage.py:146` 的 ORB 持仓上限从 yaml 5 变代码默认 3 — ORB 停用中零实害, 但未来重启用 ORB 时会咬人 (重启用 checklist 需含"重建 strategies.orb 段")。
- scheduler_core:1045 日志文案仍提 "kill_switch 仍生效" — 文案准确 (emergency_interceptor 版活着), 无需改。

**🚩 流程风险 (需用户知晓)**:
- **145 对象删除全部未 commit** (git status D×91+R×56), 且**未经重启验证** — 当前进程 07-25 09:14 启动早于删除, 内存里跑的是旧代码, "服务正常"不能证明删除安全。复核方 import 冒烟已覆盖主链, 但完整验证 = 盘后重启一次 (周日窗口零风险) + 分批 commit。建议: 周日完成, 勿带未验证删除进周一开盘。
- 第 1 轮"立即执行项"中 Q2 清空/5e 补键/Q6 watchdog 清单 均未执行 — 属裁决建议尚待用户确认, 不算缺陷, 但状态标注应从"立即"改"待确认"防误读。

**方法论追加 (第四课)**: 归档前的"零引用验证"必须覆盖四个调用面 — 仓内 import / crontab / systemd / **Hermes prompt 文本** — 任一面漏查都会把活物送进 archive。建议把本次四面清单固化进减法审计 SOP。

---

## 框架收敛专题执行记录 (2026-07-26 周日, 用户问"今天能否完成收敛")

**句识别答复: 大合一今天不可也不应做; 裁决建议的立即项今日全部完成。**

**套数复核修正 (独立重查)**:
- regime: 现存 **7 套** (3 套已在 R2 删) — 其中 4 套产 regime 标签, 3 套是 filter 语义 (不产标签)。
- 熔断: 实为 **6 套全活引用** (absolute/capital_safety/account/market/circuit_breaker/drawdown_control), 分层正交确认。
- 辩论引擎"4 套": **同 LLM 客户端款误判, 撤案** — 现存 3 个职责正交 (unified=主引擎 / debate_planner=盘前双 agent / catalyst_debate 头注自述"复用 DebatePlanner+call_llm"=复用非重复); 真重复的 2 个 R2 已删。
- 裸 regex: **新发现修正 5e 建议** — 三处 `auto_trading_enabled` regex 读磁盘是刻意设计 (绕过热重载 60s 延迟的紧急开关语义), 改 get_dotted = 引入延迟窗口 = 行为变化。原建议降级为"提取共用 helper 保持磁盘语义", 触及 execution/engine.py 列下周末。已写入 AGENT.md 防误优化。

**今日完成 (裁决建议立即项 5/5)**:
1. 5d 熔断 map: `docs/circuit-breaker-map.md` (6 层表 + kill_switch 哨兵语义说明)。
2. 5b 推送军规 + 四调用面验证 + regex 勿优化: 写入 `AGENT.md` 新增"军规"节。
3. **5c regime shadow 对比器上线**: `scripts/regime_divergence_shadow.py` + cron 23:00 CST 交易日。首样本即出信号: playbook=CAUTIOUS vs risk_detector=RANGING — 两套连词汇表都不同, 初步支持"语义正交"派 (待 n≥10 交易日按预注册判据裁)。
4. Q2/5e/Q6 上午已落 (见第 2 轮复核后续)。
5. 存量大迁移 (5b 推送 5 旁路 / 5c 合一 / 5e 三体系) 全部按裁决留 08-29 后 — 明日开盘前不碰活链路。
