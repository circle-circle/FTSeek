# FTSeek-US · HANDOFF

> **这是什么**: DA-FTSeek 链路迭代的**活文档**。每次调整/计划都追加到这里, 避免跨会话/跨 Agent 从头开始。
> **怎么用**: 每次改动后更新"变更日志"(最新在最上) + "下一步"。其他 Agent 先读本文件再动手。**多模型协作规范见 `docs/specs/multi-agent-collaboration-charter.md`（角色协议/数据口径七律/落档强制/验证期变更纪律，含用户在内人人遵守）。**
> **最后更新**: 2026-07-26 (校验清单同步修复批) · 环境: 富途**模拟盘** (v2 sim, 无真实资金) · venv: `./venv/bin/python`

---

## 🎯 当前北极星 (换过 KPI)
- **只追"每笔期望值 ≥ $0"**, 不追胜率/周收益数字。收益是期望转正后的自然结果, 直接追会逼出"加仓凑收益"的自杀行为。
- 活跃 OKR: **`docs/OKR-2026-08.md`** (2026-07-14→08-31)。上一轮 `docs/OKR-2026-07.md` 已结算 **MISSED**。
- 重心在 **FTSeek 退出侧**, 不在 DA 预测侧 (见下"核心诊断")。

## 🔬 核心诊断 (数据实证, 别再推翻重来)
用 `scripts/exit_scorecard.py` 实测 (污染窗口 n=61/71):
1. **信号 ≈ 掷硬币偏正, 不是问题**: 持到收盘命中 50.8%, 均值 +0.37%/笔 → DA 有薄弱正 edge, 达到">50%"的诉求。
2. **漏血在退出**: 退出把 8 笔本该收盘为正的赢家做成亏损, 只救回 2 笔 → 胜率 50.8%→41%。
3. **R:R 结构性不足**: avg赢 $118 ≈ avg亏 $110, R:R=1.07 < 盈亏平衡 1.29 → 每笔期望 **−$10.57**。
4. **confidence 反向**: 低信心桶反而更好 (Playbook IC 为负) → 按 confidence 加仓 = 给更差信号加钱。**已拍平** (见变更日志)。

一句话: **不需要更准的预测; 需要退出别砍赢家 + 把 R:R 拉过 1.29。**

## 🧭 每周迭代循环 (每周五盘后)
```
1. 跑 scripts/exit_scorecard.py            → 期望值 / 杀赢家数 / R:R 缺口
2. 只问: 赢家还在被砍吗? R:R 离 1.29 还差多少?
3. 只改一个退出参数 (其余全冻结)
4. 下周同一张表对比: 变好→保留, 变差→回滚
铁律: 一次一个变量、一个指标、可回滚。禁止在期望转正前加仓/放大 R:R 追收益。
```

## 🛠️ 工具
| 工具 | 用途 |
|---|---|
| `python scripts/exit_scorecard.py [--since 2026-07-11] [--days N]` | 退出纪律周度判分 (只读)。落 `data/exit_scorecard_latest.json`+history |
| `python scripts/okr_status.py` | OKR 总览 (胜率/PnL/IC/因子/void率趋势)。落 `data/okr_latest.json` |
| `python scripts/trigger_gate_report.py` | 触发门判分 (只读): executed vs missed 的 fwd_return 缺口/方向分层/机会成本。落 `data/trigger_gate_latest.json`+history |
| `python scripts/direction_hit_scorecard.py [--since D]` | 方向命中率记分牌 (只读, 2026-07-24): 指令台账 fwd_return 按 方向/来源/辩论共识/regime/周 切片 — "LLM看没看清方向"的信心仪表。落 `data/direction_scorecard_latest.json` |
| `python scripts/exit_replay_backtest.py [--since D] [--refresh-bars]` | 离线退出回放器 (只读, 2026-07-25): PLAYBOOK_SIM 历史交易 × 1min bar 反事实回放 dd/be/tiered/early_tight 网格, 双序区间+Δbaseline 口径。落 `data/exit_replay_latest.json`, bar 缓存 `data/replay_bars/`。详见 `docs/debates/DEB-20260725-exit-replay-backtest.md` |

## 🗺️ 退出旋钮地图 (`config/playbook_horizon.yaml`, DA 恒走 `short_horizon`)
**A. 赢家被锁太小 (R:R 缺口主因) — `factor_exit_rules.default`**
- `drawdown_from_peak_pct` = **0.8** (L49) ← 07-19 从 0.4 回归到 0.8 (handoff 处方)。给赢家 0.8% 呼吸空间, 不再 0.4% 就锁。
- `breakeven_at_pct` = 0.5 (L48) ← 不动, 仍早保本 (亏不了)。

**B. 赢家被噪声扫损 (杀赢家) — `short_horizon.vol_buckets`**
- low `sl_floor_pct`=2.0 (L370), medium=2.5 (L378) ← 多数标的落此, 实际止损≈2-2.5%
- ⚠️ 07-09 基于**日线回测**收紧过(“2-3%最优”); 与 live scorecard 冲突时**信 scorecard**。

**C. 已核实**: `short_horizon.rr_ratio=999` (L353) → 固定止盈**关闭**, 靠 trailing+EOD。所以封顶赢家的是 A 的 drawdown 锁, **不是** rr_ratio。

**D. 早期紧SL (方向全错快速脱身) — `unified_config.yaml::playbook.execution_parameters`**
- `early_tight_sl_enabled` = **false** ← 07-20 修复M 从 true 降级为 **shadow-only** (积累期实单退出参数冻结)。
- 逻辑保留供口径对齐: 入场后30min内 breakeven未激活 → SL收紧到1%. 但**不再实单化**, 改由 `exit_shadow.EARLY_TIGHT_VARIANTS` (`early_tight_30m_1pct`) record-only 采集反事实 `counterfactual_saved_pct` (正=紧SL更优)。
- **为什么降级**: 见变更日志 07-20 修复M — 积累期每改一次实单退出参数就断一次 config_version 窗口, 污染毕业(胜率/PnL)判定。新退出规则一律先走 shadow, 2 周数据选出赢家再一次性切换。
- 回滚/实单化: `early_tight_sl_enabled` 改 true (哈希变化会自然开新 config_version 窗口)。

## ➡️ 下一步 (按时间顺序)
0a. **07-25 04:20 CST (收盘后, 已排定时任务)**: 重启 `ftseek-us.service` 激活凌晨修复批 (5项, 见变更日志首节) + 启动验证三判据: ①[ConfigGov] 热重载/漂移检测启动 + [ConfigAudit] 审计通过 ②无启动期 stages Pipeline 扫描 ③Reconciler 无 playbook 仓 broker_only 补录。
0. **八月终裁框架已预注册** (`docs/specs/august-verdict-framework-20260831.md`, 2026-07-24 冻结): 08-29 盘后按 G1~G5 判据出 GO(切实盘)/PIVOT(降频高信念或更长持有期或纯事件驱动)/STOP 结论。8月只允许"修尺子、攒样本、做减法"三类动作，禁新机制。前置：P0.2 conf修复(X1赛道前提) + UE tier透传(事件赛道前提, 周末补)。
1. **07-25 晨**: 看两个定时任务产出 — 00:30 盘中链路验收 + 08:30 首夜报告 (`scripts/first_night_gate_report.py`)。判据异常 → 按 crash-day DEB 第8轮预案一键回滚 (三开关在 `playbook.execution_parameters`)。
1b. ~~**07-25 04:20 CST** (美股收盘后): 重启 `ftseek-us.service` 激活 topup 盘中窗~~ → 已合并入 0a (盘中窗 07-24 晚重启时已提前激活, 本次重启另携 yaml 盘中窗恢复 1130 + 凌晨修复批)。
1c. **07-27 周一 10:30-11:30 ET**: 盘中窗首日验证 — 日志 `[playbook·topup] 补量启动 (intraday)` + DB 新行 `raw.timing=intraday` + 定桶/regime 当轮补齐; 效果按 timing 切分 (盘前批 vs 盘中批)。回滚: `intraday_start/end` 设同值。
2. **07-24 scan 生成后**: 跑挑战者 A/B 第2轮 `./venv/bin/python scripts/challenger_model_ab.py 2026-07-24 15`, 两日合并裁决是否切 glm-5.2 (首轮裁决: 机械判据过/行为判据存疑, 维持 deepseek)。
3. **周末 (07-25/26)**: 已立项 P-1 名单合一(角色标签单真源, 非简单并集——watchlist 含低波防御股不可入 topup) + P-2 订阅动态化(指令+持仓+基准; 前置=隐性消费者 5 处定性)。方案见 da-chain-audit DEB §七。~~ConfigGov 失联~~/~~预热绕过门~~/~~补录守卫~~/~~解锁反模式 9 处~~/~~MFE shadow 死块~~ → 已于 07-25 凌晨提前修复 (见变更日志首节), 周末不再重复。
3b. **待用户裁决 (辩论已完成)**: `docs/debates/DEB-20260725-llm-cache-cross-symbol-pollution.md` — LLM 缓存跨标的污染。**辩论方 (主工程会话) 已逐 Q 实证作答 (§八)**: Q1 无隐蔽消费端 / Q2 死路由成立 (grep 命中全是注释) / Q3 日志零命中→方案B依据成立 / Q4 追加发现 audit 库 57% 是 test 污染。**双方一致推荐方案 B** (附失败提示验证条件)。
3c. **待用户裁决 (辩论已完成, 有分歧点)**: `docs/debates/DEB-20260725-llm-thinking-mode-tradeoff.md`。**辩论方作答 (§七)**: 事实链全部复核属实 (含 hermes config 0 thinking 键实锤); **分歧点 = B 的时机**: 作者倾向三前置后灰度, 辩论方指出 Q1 判据需 4-6 周样本与 08-29 终裁撞期, 建议 **D 立即做、B 延至终裁后**。待用户在"提前三前置串行" vs "终裁后再议"之间拍板。
3d. **待用户裁决 (辩论已完成)**: `docs/debates/DEB-20260725-llm-failure-semantics.md`。**辩论方作答 (§五)**: 事实链复核属实; **支持 B 三件套全做** (②重试分流加 flag + 双信号触发 + 比例制降幅修正), 作为 thinking 灰度硬前置且独立价值成立。双方无实质分歧。
3e. **新起案 (待辩论/裁决)**: `docs/debates/DEB-20260725-da-ue-pe-agent-optimization.md` — DA/UE 链路 PE 工程与 Agent 编排优化 (用户命题)。P1-P5 (PE层: 可证伪字段/rubric化conf/负反馈注入/信息饮食/SHORT高门槛) + A1-A4 (编排层: **分歧奖励反转**=最大错位/UE self-critique/裁判分离存储/时效复核) + E1-E2 (评估闭环)。修尺子五项可先行, A1 等 n≥100 (衍接 08-07), 编排重构终裁后。预注册 Q1-Q4。
4. **周五盘后**: 老动作不变 — `scripts/exit_scorecard.py --since 2026-07-21` 看 R:R。
5. **08-07 裁决点**: P0 shadow + 三门 go-forward 数据 → 阈值精调 / X1 降频是否启动。X2 若启动须满足**四前置**（新增第 4 条"SHORT 不隔夜"，依据主文档 §13.1，叙事已由 §14.2 修正）。

---

## 📝 变更日志 (最新在上)

### 2026-07-26 (周日晚·回归验证接线批) · 启动冒烟脚本 + CI 补面 + 重启 SOP 军规 (用户裁"需要")
**背景**: base.* 事故实锤回归体系缺口 — 980 测试全绿 + 人工重启 grep 都没拦住 ConfigGov 降级运行; 且 ci.yml 只监 master/main, 当前工作分支 rfc-060 本周 10+ commit CI 一次没跑。一次性接线, 之后零维护, 主战场回策略。
1. **`scripts/startup_smoke_check.py`** (新建): 重启后七条机器断言 (ConfigValidator 全过/漂移检测/热重载/SSOT 审计 0 BLOCKING/模拟盘 TRADE_ENV=1/退出快循环/零 Traceback), 以最后 [PreFlight] 为启动段界, 启动段过旧(>6h)判 exit=2; `--wait N` 轮询适配启动耗时, `--push` 失败飞书告警。**验证**: 正例当前启动段 7/7 ✓; 负例用 17:37 故障日志样式合成复现 — 若当时有此脚本会抓住 5 项失败; 过旧启动段 exit=2 ✓。
2. **ci.yml 补面**: 触发分支加 `rfc-*` (消"CI 从没跑过"盲区); 三子集升级为全量 `tests/` (扫尾批后 980/35s 首次具备当门禁成本)。注: 未推远端, GitHub runner 上首跑效果待 push 后观察 (requirements 安装 `|| true` 可能缺依赖)。
3. **AGENT.md 军规**: 重启 SOP = restart + smoke `--wait 90`, 禁临场 grep 代替; 删 yaml 段零引用验证须含隐性消费面 (REQUIRED_KEYS/_AUDIT_ITEMS/CRITICAL_KEYS)。
**不做**: 大规模补覆盖率/策略细粒度单测 — 本周末三次事故无一是"缺单测"造成, 且违背"聚焦策略"主方向。

### 2026-07-26 (周日晚·校验清单同步修复) · cleanup-r3 删 yaml 段但漏改校验清单 → 启动 4 假错误+2 假 BLOCKING, 已修并重启验证全绿
**根因**: 70cecbdb (cleanup-r3) 删了 yaml 无消费者段 (`base:`/`stop_loss:`/`take_profit:`), 但 validator REQUIRED_KEYS + config_audit CRITICAL 清单未同步 → 17:37 重启报 4 个 `base.*` 必需键缺失, raise 被外层吃成"降级运行" → **ConfigGov 整块中断 (漂移检测/热重载/审计均未启动)**; 且 Layer 1.7 还埋着 stop_loss/take_profit 2 个假 BLOCKING (被 validator 先挡住未暴露)。
**修复 (尺子同步减法, 零行为变更)**: ① validator REQUIRED_KEYS 移除 4 个 `base.*` 键 ② config_audit 审计项移除 scheduler.min_confidence + stop_loss/take_profit.default_* 3 项 ③ config_defaults 同步删 CRITICAL_KEYS 条目及死常量 SCHEDULER_MIN_CONFIDENCE/DEFAULT_STOP_LOSS_PCT/DEFAULT_TAKE_PROFIT_PCT (全库确认无其他消费者) ④ yaml 删废弃残留键 scheduler.min_confidence (消 DEPRECATED WARNING, 运行时早已走 MinConfidenceResolver)。
**验证**: 18:07 重启 — ConfigValidator ✅全部通过 / 漂移检测+热重载启动 / SSOT 审计 0 BLOCKING / 启动段 0 ERROR 0 Traceback / 模拟盘+Futu 连接正常。
**遗留 (非阻断)**: 审计尚有 3 条 INFO 级警告 `signal_fusion.us.*` 三权重键缺失走 CFG 兜底 — FUSION_* 同样无任何代码消费者, 同款死审计项, 建议下轮减法批一并清。**方法论教训**: 删 yaml 段的零引用验证必须含“校验/审计清单”这个隐性消费面 (validator REQUIRED_KEYS / config_audit _AUDIT_ITEMS / CRITICAL_KEYS)。回滚: git revert 本批。

### 2026-07-26 (周日·扫尾清零批) · 修数据+理逻辑收尾 (用户令"清干净后集中优策略")
1. **悬空键真 bug ×2**: `risk.blacklist`→`global_blacklist` (llm_pipeline 黑名单此前恒空形同虚设) / `main_pipeline.allow_live`→`strategy_budgets.` 真键 (值同 False 零行为变化, yaml 恢复真源)。
2. **实盘闸门 fail-open 反转** (order_executor ~3192): allow_live 检查异常时原逻辑**放行实盘** → 改 fail-closed (real 账户下异常即拒)。
3. **yaml 死段删除 12 段 133 行** (逐段 grep 验证零读取): OPTION_QUALITY_GATE/STRATEGY_ORTHOGONALIZATION/agents/base/debate_mode/debate_planner/llm_voting/logging/signal_fusion/顶层stop_loss+take_profit/trading_hours。yaml 48 顶层键, 解析+容器加载 ✓。
4. **测试大修 (代理执行, 341 工具调用)**: 修复 30 文件 ~95 测试 (断言追平当前语义, 禁弱化) + 删 66 个引用已退役目标的测试 + 密封化 (LLM/网络/wall-clock/env 泄漏全 monkeypatch — 其一 `TRADE_ENV=0` 进程泄漏曾级联炸 ~100 测试)。**164 failed → 4 failed**。
5. **TradeStore P0 双修** (剩余 4 失败全歼): ①全新 DB 引导缺 20 列 (save_trade/upsert 引用但 _init_db 从未建, 生产 DB 靠 ad-hoc ALTER 存活) → 补幂等 ALTER 清单 (=生产 schema 实测 diff) ②upsert row=None 时 row[8]/row[9] 无守卫 TypeError (新标的首次 upsert 必炸路径)。6/6 ✓。
6. **run_llm_backtest 死函数删** + trade_records.json 半截迁移收尾 (getter 删+暂存) + **watchdog cron 加 || 钉钉兜底** (监控者自己崩要有人知道) + **tests/test_no_archive_imports.py** (archive 零活 import 固化为 CI 断言, F1 教训) + 122s 纯睡眠测试修复 + 每次跑测试真调 LLM 的密封漏洞堵住。
7. **测试遗留 2 项已知**: P2 退出阶段非 daemon 线程挂死 (futu network_manager/PushAggregator, 跑测试用 timeout 兜) / P3 `_collect_fundamental` industry="" 通过质量检查 — 均记 DEB 待裁。
**全量测试终跑: 980 passed / 0 failed / 34.9s** (此前: 16 收集 ERROR + ~164 failed + risk 目录纯睡眠 122s + 退出挂死永不结束 → 现在 35 秒全绿, 测试套件首次恢复为可信回归工具)。

### 2026-07-26 (周日·重启验证+开盘就绪) · R2 删除首次重启全绿 + 复核修复/修尺子三项落地 + 周一就绪总检 (用户裁"重启后能修的修, 轻装上阵")
1. **重启验证 (145 项删除后首次)**: 09:00 CST 重启 — 0 Traceback/ImportError, ConfigGov 全绿, 退出快循环+Watchdog 启动, 模拟盘 ✓。R2 删除正式验证安全。
2. **复核修复已提交** (d0e8c759): Hermes 6 脚本移回 + api_limit_monitor 移回+import 修。
3. **修尺子三项**: Q2 price_fail_closed 清空为 [] (机制留, 备份 .bak_20260726) / 5e llm_features 5 隐身键补 yaml 显式化 (值=代码默认零行为变化) / Q6 三高危文件入 data_freshness_watchdog (干跑即捕获 SHORT 门控 2100h 停更×2 — 设计意图达成)。
4. **周一就绪总检全绿**: 三门 ON / topup 30/18+盘中窗 1130 / UE v2(v2.2 prompt)+负反馈+critique / thinking dr_debate / prompt_cache×2 / 主线静默; crontab 22 条无断链; Hermes 断链复查 3/4 为检查正则误报 (属 skill 自带目录)。
5. **新备忘 (既有损坏非 R2 造成)**: 盘后综合复盘 job (7a40dd7c) Step6 引用 `scripts/position_reconcile.py` — 本仓 git 历史从未有过此文件 (幻影引用), agent 该步失败但不阻塞其余; 下次修 job prompt 时删除或指向真实对账方式。
**周一观察清单**: 08:30 首夜报告 / 提量后成交量 (保险丝: 日亏>$150 回滚 25/12) / topup 盘中窗首验 10:30-11:30 ET / UE v2.2 首跑 21:10 (含 P1-P5+A2 全家桶, SHORT 量预期下降是设计意图) / thinking dr_debate 时延-empty率 / 两条 exit shadow 自动采集。

### 2026-07-26 (周末·减法审计R2) · 代码/配置/数据三维废物清算 145 对象 (用户四项全选: T1+T2+T3+DEB)
**审计**: 3 路并行 (scripts 死活/app 模块引用/配置键+data 产消对账), 详见 `docs/debates/DEB-20260726-subtraction-audit-round2.md`。
1. **T1 零风险**: atr_gate_shadow cron 停 (45KB/日零读者); yaml 删死 flag×3+shadow_mode 段+voting_* 三死路由+strategies.orb 死牌子; DEB-20260724 已裁决 3 模块删除。
2. **T2 死代码**: app/ 死模块 **73 文件 ~13,500 行** git rm (三重验证: 模块名+包级导入+类名; 分类器拦截后补强验证模式); 失效测试 16 个删除 (**pytest 收集从 16 ERROR → 1048 全过**); 孤儿脚本 58 个归档 scripts/archive/ (scripts 根 118→60)。4 个 `__init__.py` 断链已修。
3. **T3 隐身放行**: **SHORT 门控 88 天空文件静默 fail-open** (hard_to_borrow/short_interest) → 加 stale WARNING (实测触发, 行为零变化); 悬空配置键补齐 (risk.portfolio 回撤三键+max_per_sector, 值=代码默认零变化, yaml 恢复真源); event_log (常驻线程+零读者) 与 position_snapshot (每小时×3 取价+零读者) 停写。
**遗留裁决 (DEB §五)**: SHORT 门控数据源接线 vs 废弃 / price_fail_closed 65 天冻结 / api_limit_state 仓外生产者 / 推送 7 套+regime 7 套框架收敛 (留 08-29 后) / data 产消注册表进 watchdog。
**回滚**: 全部 git revert 级; crontab 备份 data/crontab.backup.20260726_*.txt。

### 2026-07-25 (周末·浪费清扫四项批) · 全项目定时任务审计落地 (用户四项全选)
**审计**: 3 路并行扫描 (Hermes cron / 系统 crontab / 仓内任务) — 仓内干净 (全部 LLM 已审计 ≤$1.10/天), 浪费集中在 Hermes+crontab。
1. **Hermes 重复任务暂停** ✅: `895169e95c50` ftseek-eod-feedback-loop 与孪生任务跑同一 `feedback_loop.py` 仅晚 30min (~30万 token/次重复) + 周一复盘周日数据 → `hermes cron pause` (可逆)。
2. **两个每周必崩脚本修复** ✅: ①`ic_weekly_report.py` ic_mean=None 时 `f"{None:.4f}"` TypeError → `_fnum` 守卫 + by_strategy/direction 空值防护, 实测跑通并推飞书 ②crontab L20 `symbol_performance_report.py` 补 `PYTHONPATH=.` (兄弟条目均有), 实测跑通。crontab 备份 `data/crontab.backup.20260725_225836.txt`。
3. **僵尸任务批清理** ✅: crontab 注释 4 条 (带日期+原因+恢复条件): `update_signal_lifecycle` (无 live 交易 2 周+, **转 LIVE 须恢复**) / `backfill_voting_outcomes_v2` (卡死行永真) / `factor_exit_pnl` (死消费者) / `time_exit_counterfactual` (无 TIME_EXIT 记录)。main.py Task#36 死 import 前批已移除。`/etc/cron.d/ftseek-tasks` L7 (*/5 curl :8901/health, 与 health_check.sh 重复) 已由用户手动注释 ✅ (备份 `ftseek-tasks.bak_20260725_zombie`, 已验证)。
4. **combined_monitor 停用** ✅: 两条 cron 注释 — 39 次/日检出 84 告警但**零推送代码** (检出即终点); 覆盖已由 error_alert.sh (ERROR 计数+钉钉) + intraday_heartbeat (静默失败检测+推送) 承担。

### 2026-07-25 (周末·LLM 成本堵漏) · Hermes 空转 cron 停用 (用户查账发现, 直接拍板执行)
**发现**: Hermes cron `81002dff9b1d` "FTSeek-US Agent 请求处理" `*/5 * * * *` 24/7 无窗轮询 `~/.hermes/ftseek_requests/` 队列 — 每发 2 次 qwen3.7-max 调用 × 38,370 输入 token (~$0.61/次), 产出 8 字符 `[SILENT]`。**~$177/天, 04-25 创建以来 24,487 次 ≈ $1.5 万量级**, 全部在仓外 (不进 llm_audit.db / 不受 $5/天预算守卫)。
**必要性核查 (结论: 无存在必要)**: ①仓内+`/opt/ftseek` 全局 grep 零生产者 (无任何代码写该队列) ②留存 50/50 次输出全是 [SILENT] ③队列目录 05-26 起为空 — 生产者早已退役, 消费者空转 3 个月。
**动作**: `hermes cron pause 81002dff9b1d` (可逆, 备份 `jobs.json.bak_20260725_disable_reqpoll`)。其余 enabled cron 不动 (盘中诊断等均有正确交易时段窗)。
**复活条件**: 若未来重建队列生产者, 须用 shell 门控形态 (script job: 空队列 0 成本退出, 非空才唤起 agent), 禁止恢复 LLM 直轮询。累计浪费金额计入 8 月复盘账本。

### 2026-07-25 (周末·P5/A2 落地) · SHORT 高门槛 + UE self-critique shadow (用户裁"提质量且不改交易流程可执行")
**双条件过滤裁决**: P5 ✅ (输出字段加法, SHORT 照常入库执行) / A2 ✅ (shadow 形态, 注入集合零变化) / **A1 ❌ 不动** (改 conf=改执行排序, 继续攒 n≥100) / **A4 ⚠️ 拆分** (字段已被 horizon_hours 覆盖; price-in 复核要挂盘前主循环侵入面大, 列下周)。
1. **P5-UE** (`llm_universe_scan.py` v2.2 规则8): SHORT 强制 short_check{borrow_feasible/downside_catalyst_expiry_hours/squeeze_risk} + "无具体下跌催化剂不得浮现"; normalize 解析含 provided 标记 (模型没交作业可切片) → inject 落 origin。
2. **P5-DA** (skill `llm_synth.py`, 备份 .bak_20260725_shortgate): 主分析师 prompt 补 SHORT 三问 (答不全改 SKIP) + 挑战者补 SHORT 加严审查项 (缺问标 missed_risks)。DA 是重灾区 (分歧SHORT 0%命中)。
3. **A2 self-critique shadow** (`llm_universe_scan.py` cmd_scan): surfaced 后同模型二次调用逐条攻击因果链最弱环, verdict(KEEP/DOWNGRADE/DROP) 只落库不拦截; 回滚 `universe_expansion.self_critique=false`。判分路径: scorecard 新增 critique 切片 → DROP 组若显著更差再裁升级实拦。
**端到端验证亮点**: critique 真调用中主动引用 P5 规则 DROP 了无催化剂 SHORT 桩 ("违反 SHORT 高门槛强制约束...") — 两新规则互锁。P5 冒烟 3/3 ✓; py_compile ✓。
**验证路径**: 周一首跑后 scorecard 看 short_check.provided 率/critique 分布; SHORT 浮现量预期下降 (设计意图: 宁缺毋滥)。

### 2026-07-25 (周末·UE v2.2 修尺子批落地) · PE 优化 P1-P4+E1 全部执行 (用户裁"明确的现在就做, 下周开盘验证")
**范围**: DEB-da-ue-pe-agent-optimization 修尺子先行五项, 全部 UE 侧 (`llm_universe_scan.py` → **PROMPT_VERSION v2.2**) + scorecard 读侧。零执行行为变更 (新字段纯落库, 排序/门禁不动)。
1. **P1 可证伪字段**: 非 SKIP 标的强制输出 invalidation/horizon_hours/expected_move_pct → normalize 解析 + inject 落 origin。
2. **P2 rubric 化**: evidence_checklist 四项 bool (催化具体/24h内/量价印证/非拥挤) → **rubric_score 代码侧计算** (校准从模型移到代码), 与 conf 并列落库供 Q2 相关性对比。
3. **P3 负反馈注入**: `_build_feedback_table()` 查本源近7日浮现→fwd 结果小表注入 brief + "连错同标的同方向须新增量证据"规则 — 验证时表内已含 GOOG SHORT 连两日 (-6.9%✗) 实弹。回滚: `universe_expansion.clue_feedback=false`。
4. **P4 信息饮食**: 每条新闻带时效小时数 (RFC2822 解析修复后 20/20 生效); premarket_movers 注入**放弃** (数据陈旧 06-26, 防腐守卫教训)。
5. **E1 切片**: scorecard 新增 prompt_ver (v2.1/v2.2) + rubric (≥3/≤2) 两组 — v2.2 效果自动 A/B 可判。
**端到端验证**: 真 LLM 调用 (v2.2 prompt + 真实 brief) → 浮现 7 标的 **v2.2 字段完整率 7/7** (含合理 invalidation 如 'XLI转跌或LMT放量跌破日内均线'); 冒烟 3 场景 ✓; py_compile ✓。
**验证路径 (下周)**: 周一 21:10 cron 首跑 v2.2 → 每日 scorecard 看 prompt_ver/rubric 切片 → n≥30 后按预注册 Q1/Q2/Q4 判 (重复推荐占比/rubric vs conf 相关性/命中是否脱离 29% 负 edge)。
**未动 (按分级)**: P5 SHORT 高门槛/A1 分歧奖励反转/A2 self-critique/A4 时效复核 — 待裁决或等样本; A3 裁判分离存储 (skill 侧) 排下周。

### 2026-07-25 (周末·LLM DEB 裁决落地批) · Cache B + 失败语义三件套 + Thinking D 实测/接线 (用户裁"无争议完成更新")
**裁决**: 用户定调信号产生/筛选优化属"修尺子"; 无争议三项立即落地。
1. **Cache 方案 B**: yaml 6 处 `cache_on_fail→false` (跨标的污染消毒, 历史零命中); 附带条件验证 ✓ (dashboard 失败路径有显式 WARNING 非静默)。回滚: 改回 true。
2. **失败语义 B 三件套** (`model_call.py` + `hermes_worker.py`): ①finish_reason 全链透传+落审计 (截断可观测) ②双信号截断降档自救 (parse失败且length → ×0.5 下限1024, flag `llm.retry_downshift_enabled` 回滚) ③hermes 空 verdict 质量门 (per-role 兜底+缺席标注+absent_roles 透出)。验证: py_compile+真调用回归 ✓。
3. **Thinking D 实测实锤**: 探针 A/B 对照 — DashScope 不传参时 **reasoning=117tok/2.8s** vs 显式False 0/0.8s → **外部 Hermes DA 链 (48标的) 一直 thinking ON 运行且系统正常** (18:05→19:31, 余量2h)。用户担忧的"全标的耗时"已是现状。
4. **Thinking 基建接线**: yaml 路由级 `extra_body` 透传+合并 (调用方优先, 无配置行为不变, 回归冒烟 ✓) — per-feature 灰度至此 = yaml 2 行。
**待用户确认**: thinking 分层方案首灰特征 (建议 dr_debate, 见下次回复方案表)。Q4 观测缺口 (audit 盲区+test 污染 57%) 另立案。

### 2026-07-25 (周末·thinking 首灰 + 缓存探针实锤) · dr_debate 灰度上线 + 隐式缓存观测落地 (用户批)
1. **dr_debate thinking 首灰上线**: yaml `extra_body.enable_thinking: true` + 调用方 max_tokens 2500→5000 (防 07-06 empty_response 复现)。端到端真调用: 6.7s/输出完整/finish=stop ✓。观察指标: empty 率/时延/截断率 (finish_reason 已落审计)。回滚: 删 yaml 两行 + max_tokens 改回。效果**不设为 08-29 终裁判据** (样本周期 4-6 周不够)。
2. **缓存机制探针实锤 (用户问 cache_control ephemeral)**: DashScope 兼容模式实测 — ① **deepseek-v4-pro 隐式前缀缓存自动生效** (同前缀第2发 cached=3072/3298, 时延 -45%, 无需任何参数) ② qwen3.7-max 未见命中 (同条件 cached 恒=0) ③ Anthropic 式 `cache_control: ephemeral` 被**静默忽略** (接受不报错不生效) → 无需也无法显式标记, 降本靠"固定前缀 prompt 结构"+deepseek 自动缓存。
3. **cached_tokens 观测落地** (`model_call.py`): CallResult.cached_tokens + audit error_msg 拼接。生产链端到端: agent_bridge 第2发 cached=1024/1344 (76% 前缀命中) 已入库 ✓ — token 大头 (agent_bridge 4.5k×28次/日) 的真实成本从此可见。
4. **prompt 结构建议入帐 (不动手)**: 缓存友好 = 固定 system/规则前置 + 变化数据 (价格/时间戳) 后置; 待首周 cached_tokens 数据出来后再定向优化 prompt 拼接顺序 (避免盲改)。
5. **⚠️ 勘误 (用户指正, 2026-07-25 晚)**: 上述②的"cache_control 被静默忽略"结论**错误** — 我的探针只发了一次 (创建轮必然 cached=0) 就误判。重测 (创建轮→命中轮): **qwen 显式缓存完全有效** — 第1发 cache_creation=3719tok, 第2/3发 cached=3719 (99.4%), TTL 5min (ephemeral_5m)。**已落地**: `model_call.py` 新增 route 级 `prompt_cache` 开关 (仅 dashscope 启用数组格式, 自救重试同步透传); yaml 首批开 deep_target_analysis + universe_expansion_reasoning (qwen 主力+批量连发适配 5min TTL)。生产链端到端: **98% 命中, 时延 3559→762ms (-79%)**, audit 已记。回滚: 删 yaml prompt_cache 行。方法教训: **缓存类探针必须测"创建→命中"全周期, 单发判无效=方法错误**。

### 2026-07-25 (周末·离线退出回放器落地) · 路径1 建成 + 首轮网格结果 (只读工具, 零 live 变更)
**是什么**: `scripts/exit_replay_backtest.py` — PLAYBOOK_SIM 全历史 (n=145) × Futu 1min bar (深度实测≥2026-01) 离线反事实回放退出参数。直接 import 实盘 `_compute_dynamic_effective_sl`/`_get_factor_exit_config` 杜绝两份数学; pess/opt 双序报区间; 费用/滑点/冷却/TP门禁/MFE_STALL/TIME_EXIT 全链复刻。验证锚点: 回放MFE vs 记录 median差0.077%, 退出价差median 0.136%; 固有乐观偏差+$1.9/笔 (bar影线>轮询采样, 变体排序无偏, 紧dd类收益为保守低估)。详见 `docs/debates/DEB-20260725-exit-replay-backtest.md`。
**首轮结果 (Δbaseline 口径)**:
- 全史 (n=141): early_tight 巨量止血 +$1050~1170 但**集中6月崩盘regime** (近期仅+$40~78, 与live shadow互证); dd 0.4/0.5/0.8 处平台区 (差≤$2/笔)。
- **近期窗口 (n=62≥30) 一致指向"更早保护"**: be_0.3 (+$191~209) ≈ dd_0.4 (+$160~187) > tiered_dd0.4_tier1.2 (+$131~149) — 与07-19放宽处方有张力, 裁决路径=tiered_dd live shadow (07-25起采集) 2周后双源交叉, 一次一变量, 暂不行动。
- 所有变体期望仍负 → 退出调参是二阶杠杆, 与核心诊断一致。
**边界**: 三门/入场门不可回放 (路径2); sl_floor网格需ATR重建 (v2候选); 4笔时区修复前损坏时间戳交易已排除。

### 2026-07-25 (周末·提量裁决) · 用户裁: 验证期保持 UE 优先 (ADR-020 维持) + 提升每日交易量快速攒样本
**用户裁决**: ① ADR-020 "UE 最优先"维持不变 (信息源复盘提出的翻转议题否决 — 验证期以攒样本为重, UE 继续优先成交攒其判分样本) ② 提升每日交易量。
**漏斗诊断 (动手前实测)**: 执行侧上限全部未触顶 (max_daily_positions=30 昨日用 8 / notional cap 用 32% / 预算用 5%); 真瓶颈在供给侧 (近5日指令 15-21 条/日) + 三门拦截 (验证对象不动)。top_n 已废弃不截断, 非限量器。
**提量落地 (仅抬配额不降标准, 三门/质量门/退出参数零变更)**:
1. **topup 配额**: target_min_instructions 25→30, max_add_per_day 12→18 (`unified_config.yaml`, 备份 .bak_20260725_topup_quota)。
2. **topup 池扩容 16→24 只**: +ARM/MRVL/UBER/RIVN/SNOW/SHOP/DELL/CRWD — 选标口径 = ATR 甜蜜区 3-4% 画像 (07-24 复盘唯一正期望桶) + 避开低波防御股 (2.5-3.0% 次差桶)。仍经 Screener 质量门+三门。
3. **生效**: 热重载实战首秀 — 10:02 CST HotReloader 自动生效 (凌晨 ConfigGov 修复的直接收益, 零重启), 运行时确认 30/18/24。
**预期效果**: 盘前批 (08:45-09:30) + 盘中窗 (10:30-11:30, 周一首验) 双窗可补至 ≤18 条/日, 叠加 DA 13-18 + UE 2-5 → 供给上限 ~30-40, 经三门后预期成交 15-25 笔/日 (vs 昨日 8)。样本速度翻倍, 08-07 裁决点前可攒 ~150-250 笔 go-forward。
**风险提示 (已入帐)**: 提量 = 日亏损绝对值可能同步放大 (昨日 -$31 是 8 笔规模); 若日亏 > $150 (回到旧基线量级) → 配额回滚 25/12 一行恢复。回滚: yaml 两处改回 + 删池扩容段。

### 2026-07-25 (周末·信息源复盘) · UE/DA/topup 源头质量实证切片 — 3 项修尺子落地 (策略/退出逻辑冻结不动)
**背景**: 用户定调"现有策略逻辑不动, 单独分析信息源头质量"。近14天 134 条指令按源实证切片 (fwd_return 前向台账, 不受退出污染)。
**源头画像 (实测)**:
- **DA** (108指令/成交69%): 方向命中 52% 均fwd +0.31% (薄弱正edge, 与北极星诊断一致); 但 **conf 退化为常量** (89/95条<40, gate WARN 罚成 35 后分层能力=0); **debate_outcome 是隐藏分层器**: 分歧BUY 67%命中+0.9% > 弃权 47%+0.39% (反直觉: 有争议的 BUY 更好); **SHORT 系统性烂**: 全部 SHORT 相关 outcome 均负 (分歧SHORT T+1 0%命中 -4.5%, n=6; SELL_SHORT 整体 29% -2.2%, n=17)。
- **UE** (26指令/成交81%): **方向命中仅 29% 均fwd -0.67% (负edge)**; 高度重复推荐 (ORCL SHORT×5/EOG BUY×4, 无衰减机制); conf 库内恒=50 (07-16 cap 把 LLM 0.85-0.92 分层全抹); tier 写侧 07-22 已落但 v2 时代样本少且初样本全负 (HARD_EVENT n=2 0%命中 — n<30 不下结论, 八月继续攒)。
- **topup**: 零成交样本 (首跑+0 诚实), 周一盘中窗才有数据; 风险前瞻: 池源=watchlist 含低波防御股 = 昨日复盘 2.5-3.0% 次差桶主力 → 与 P-1 名单合一联动设计 ATR 过滤。
**修尺子落地 (3 项, 均纯归因数据, 零排序/交易行为变更)**:
1. **UE cap 前原始 conf 落库** (`llm_universe_scan.py` origin.raw_confidence): 排序仍用 capped=50, 归因恢复分层。
2. **DA confidence_semantic 落库** (Hermes skill `pipeline.py` raw_instruction 补字段, 备份 .bak_20260725_confsem): bridge cand 块早已双写但落库丢失 → P0.2 conf 校准修复 (X1 赛道前提) 的归因地基补齐。下次 DA 运行自动生效。
3. **判分工具补读侧** (`direction_hit_scorecard.py` 新增 2 组切片): debate_outcome 4 档 (弃权/分歧BUY/分歧SHORT/一致) + UE self_tier 4 档 — 八月终裁事件赛道判据消费端接通, 周度仪表化。
**观察入帐 (不动手, 攒样本/待裁)**: ① UE 重复推荐衰减 (同 symbol+direction 连续负 fwd → 冷却; 信息源侧的"连败封禁", 需 DEB) ② DA SHORT 质量门候选 (SHORT 需 HARD_EVENT 级证据, 与 X2 四前置"SHORT不隔夜"呼应; n=17 小样本先观察) ③ debate_outcome 分层若 2 周后仍稳定 → 候选替代 conf 做仲裁变量 (需 DEB)。
**⚠️ ADR-020 与实证冲突, 需用户重新裁决 (2026-07-25 勘误发现)**: auto_execute 预算排序主键 = `_src_priority` (ADR-020, 07-17 用户指定 **UE 最优先**、DA 次之), conf 仅同源内次键。当时逻辑"新资讯动态标的优先占预算"; 但如今指令层实证 UE 方向命中 29%/均fwd -0.67% (负 edge) vs DA 52%/+0.31% — **最弱源持有最高执行优先级** (07-24: UE 指令转化80% vs DA 31%, UE 占昨日一半仓位含 LMT/RTX 两大亏单)。候选处方: _src_priority 改 DA(2)>UE(1) 或按滚动 fwd 胜率动态定序 — 属 ADR 变更, 待用户确认后一行切换。(勘误过程: 曾试 UE conf cap 50→30 降优先级, 发现对跨源排序无效且污染 conf 分桶已回滚, cap 保持 50。)
**验证**: py_compile 3 文件 ✓; scorecard 新切片实跑出数 ✓; skill 侧 patch 带备份+锚点断言 ✓。无需重启 (llm_universe_scan/DA skill 均为每日新进程)。

### 2026-07-25 (周末·首夜复盘) · 07-24 交易链路实证复盘 — 防守已到位, 下一刀在赢家捕获率 (只读分析, 零改动)
**总评**: 三门砍掉大亏尾部 (-$31 vs 基线 -158/-164/-379), 但 R:R 倒挂创新低 (昨日 0.35 / 近14天 0.58, 北极星需 >1.29)。
**发现 A (最强, 双重确认)**: ATR 甜蜜区 3.0-4.0% — 昨日赢家 (NVDA/TSM/BABA/GOOG, atr 3.5-3.9, mfe≥0.95) vs 输家 (RTX/MRK/LMT/TMO, atr 2.58-3.00, mfe≤0.33) 按 ATR 完美二分; 近14天分桶: <2.5% 35%WR -$6.7/笔 | **2.5-3.0% 52%WR -$8.5/笔 (现在放行的次差桶)** | **3.0-4.0% 71%WR +$3.2/笔 (全仓唯一正期望桶)** | ≥4% 38%WR -$25/笔。
**发现 B (机制实锤)**: 赢家捕获率 15% — TSM/GOOG/BABA mfe≈1% 全部只落袋 0.14-0.15%; drawdown 0.8 对 mfe≈1% 是"利润归零锁" (公式 (mfe-0.8)/mfe: NVDA 1.9%→54% 实测吻合)。R:R 倒挂的直接现场。
**发现 C**: STALL 150min 反事实混合 (e_shadow: LMT/RTX 早45min砍各省~$15/$12, TMO/MRK 早砍更差) → 不动, 攒样本 08-07 裁。
**发现 D**: 8 笔全部 10:25-26 单点批量入场, LMT/TMO 入场即水下 (45min MAE 0.67-1.15%) → 周一 topup 盘中窗 = 入场分散化第一步, 先看它的数据。
**周末议题 (按数据强度)**: ① ATR 门 2.5→3.0 + ≥4% 上限门 (先 shadow 干跑, n=21/17/13 样本小禁直接切) ② 分层 drawdown lock (mfe<1.2 用 0.4-0.5 / ≥1.5 保持 0.8, exit_param_shadow 变体) ③ STALL 不动 ④ 入场分散化等 topup 数据 ⑤ 方向偏斜 16:2 继续观察。全部遵守验证期纪律: shadow-first, 零 live 参数变更。

**议题①② shadow 已搭建 (用户批准"同意", 当日落地)**:
- **② 分层 drawdown 变体** (`exit_shadow.py::EXIT_VARIANTS` 新增 2 个): `tiered_dd0.4_tier1.2` (peak<1.2% 用 dd0.4, 否则 0.8) + `tiered_dd0.5_tier1.5`。铁律合规: dd_eff 恒不松于现行 0.8 → 路径可观测。poll 循环内按 peak 切档; finalize 通用循环自动覆盖无需改。冒烟 3/3 (小peak用紧锁先触发/大peak回落 0.8 与 baseline 同步/SHORT 镜像)。周一开盘起随退出快循环自动采集, 落 `data/shadow/exit_param_shadow.jsonl`。
- **① ATR 门干跑判分** (`scripts/atr_gate_shadow.py` 新建, 只读): 6 变体 (lo2.8/lo3.0/hi4.0/lo3.0_hi4.0/lo3.0_hi5.0 + live 对照), 口径 = 拦"实际已成交"单的 saved=-pnl (均比现行严 → 完全可测)。首夜 go-forward: lo3.0 拦 4 笔 省 +$73.3 仅误杀 MRK(+$0.1)。⚠️ 14 天回看 (lo3.0 省 +$398 但杀赢家 24 笔含 NVDA+102) 混杂旧退出时代, **裁决只用 07-24 起 go-forward 样本**。cron 已加 (每交易日 05:58 CST `--since 2026-07-24` 累计判分, 落 `data/shadow/atr_gate_shadow_latest.json`)。
- 生效: 09:14 CST 重启 (active/0 Traceback/审计过)。裁决点: 与 08-07 合并, ≥2 周 go-forward 样本后提 DEB。回滚: 变体从 EXIT_VARIANTS 删除 + 删 cron 行 + 删脚本。

### 2026-07-25 (盘后·隐患扫描) · 时区同款"跨模块隐性契约"全仓排查 → 用户裁"顺手修", 6 项全部修复落地
**背景**: 用户问"时区之外还有没有同类未发现问题"。按 6 类高危反模式扫描: 配置漂移/单位口径/cron季节错位/半日市/静默吞异常/PDT。用户裁决"那就顺手修复掉"。
**✅ 健康项 (实测, 无需动)**: 配置审计 0 条; mfe/profit 百分比口径无混用; PDT "51/3" 係合规放行 (equity $98k≥$25k 豁免); dual_broker 平仓失败有告警链非静默。
**修复清单 (6 项, 全部落地)**:
1. **半日市 13:00 ET 提前收盘支持**: `holiday_calendar.py` 新增 `US_EARLY_CLOSE_DATES` (2026-11-27/2026-12-24/2027-11-26) + `get_us_close_minutes_et()` (780/960); 接入 3 消费点 — `pipeline.py:5092` (sim 退出主循环 minutes_to_close) / `pre_close.py:48` (主线 pre_close 窗口) / `pipeline.py:3223` (量能门 elapsed_fraction 分母), 均 try/except 兜底 960。position_sync 同步频率档未接 (无害仅多同步 3h, 范围控制)。验证: 4 断言 + 模拟 11-27 12:53 ET → mtc=7 命中 PRE_CLOSE 窗口 ✓。
2. **监控 cron 冬令时盲区**: crontab `error_alert.sh` (*/15) + `combined_monitor.py` (*/10) 凌晨窗口 0-4 → **0-5** (冬令时收盘 05:00 CST 覆盖); 备份 `data/crontab.backup.20260725_*.txt`。
3. **Task#36 死引用移除** (`main.py`): migrate_trade_records 块删除留墓碑 (schema 早稳定, TradeStore 自建表); 重启后 Task#36 WARNING = 0 ✓。
4. **定桶落库日志降噪** (`pipeline.py`): 零工作轮 (幂等全跳) INFO→debug, 有工作仍 INFO。
5. **PDT 日志豁免标注** (`pdt_guard.py`): 计数行追加 "(equity≥$25k 豁免)"/"(PDT 受限)", 消 "51/3" 误读。
6. **遥测读侧去重展示** (`first_night_gate_report.py` + `early_acceptance_check.py`): 增去重标的数 — 重跑 07-24: "streak_ban=159 (3 标的) low_atr_block=265 (5 标的)" ✓ (写侧遥测语义不动)。
**验证**: py_compile 7 文件 ✓; 半日市 4 断言+窗口模拟 ✓; test_playbook_exit_rules 1 passed ✓; 两报告重跑全过且无度量回归 ✓; main.py AST ✓。
**生效**: 08:48 CST 重启 (盘后 0 持仓): active / Task#36 WARNING=0 / 0 Traceback / ConfigGov 全绿 / 模拟盘 ✓。回滚: 各项独立 git revert; cron 用备份还原。
**分类**: 全部"修尺子/修基建", 零退出参数变更, 零 config_version 断点。半日市改动在 11-27 前无行为差异 (普通日 960 不变)。

### 2026-07-25 (盘后·时区SSOT全仓统一) · 修尺子 — 5 Critical + 4 High 时区不一致系统修复, 零交易行为变更
**背景**: 07-24 观察⑤ 实锤 `_et_trade_date(now_et())` 双重转换 bug 后, 全仓时区审计发现 5C+4H 共 9 类不一致 (134 文件)。本批为系统性补全, 与 07-24 单点修复互补。
**分类**: 修尺子 (度量/校准修复)。零退出参数变更, 零 config_version 断点, 零信号分布影响。
**DEB**: `docs/debates/DEB-20260725-timezone-ssot-consolidation.md` (完整清单+验证+回滚)
**修复摘要**:
1. **C1** `trade_recorder.py` trade_date 二次转换 → `get_trade_date()` (ET 上午交易不再标到前一天)
2. **C2** `symbol_daily_loss_guard.py` 读写契约统一 (48h 宽窗口 + `_et_trade_date` 精确过滤, 消除 CST-cutoff vs ET-aware-ISO 12h 偏移)
3. **C3** `datetime_utils.py` 合并为 shim (消除两模块 `to_et()` naive 假设冲突: CST vs UTC 差 8-13h)
4. **C4/C5** 22 处硬编码 `timezone(timedelta(hours=-4))` + 月份 DST 判断 → SSOT `now_et()`/`ET` (冬令时不再差 1h)
5. **H1** 5 处 `entry_time=datetime.now()` → `now_et()` + scheduler_core `astimezone` 修正
6. **H2** `intraday_heartbeat.py` 信号漏斗 cutoff / 显示 / 推送统一 ET
7. **H3/H4** app/ 下 50+ 处 `utcnow()` → `now_utc()`/`now_et()` (**0 残留**, grep 确认)
8. **H6** pytz `replace(tzinfo=)` → `localize()` (timezone.py + risk 模块, 消除 LMT 偏移)
9. **额外** `scheduler_core.py` 裸 `import pytz` → SSOT ET (消除 pytz 缺失时启动崩溃)
**验证**: py_compile 134 文件 ✓; 核心模块 import ✓; `utcnow()`/硬编码偏移/`replace(tzinfo=ET)` 残留 = 0 (grep 确认)。
**对毕业数据**: 无影响 (不改退出参数/触发逻辑/config_version; 只修"读哪天的交易"的尺子)。
**生效**: 盘后 0 持仓窗口, 下次重启即生效。回滚: `git revert` 整批或按文件独立 revert。
**独立审查 (Qoder, 盘后)**: 批次完整合理 — 声明逐项实证复核全过 (残留 grep/C1・C2・C3・H2 diff 实核/运行时双路径语义/shim 消费者全查); **发现并补修 H6 漏网 2 处** (pipeline.py:3980 `replace(tzinfo=_et_tz)` + shadow_tracker.py:506-507 `replace(tzinfo=_ET)` — 别名逃过字面 grep, 运行时实锤 -04:56 LMT/56min 误差, 影响 holding_hours 度量; 同款 localize 处方补修)。to_et 语义变更 (naive UTC→CST) 查无真实调用方, 风险解除。first_night_gate_report 重跑无度量回归。**08:15 CST 已重启含补修 (active/0 Traceback/ConfigGov 全绿)**。详见 DEB §九。

### 2026-07-25 (盘后·首夜验证遗留处置) · 修尺子两项 — stall 埋点 + 判据容差, 零行为变更
**背景**: C0 首夜验证 MRK 被脚本误报 ❌ (触发时 cur=-0.03% 水下合法, 成交后落库 +$0.12 被落库PnL尺子误判)。两项均"修尺子"类, 不改任何触发/退出行为。
1. **stall 触发现场埋点** (`pipeline.py`): 规则 3.4 触发时写 `meta["stall_exit"]={stall_minutes, mfe_at_trigger, cur_at_trigger, threshold}`; 主退出落盘处从内存 meta 搬运入 `_meta_out` (必须显式搬 — `_meta_out` 基于开环旧行重建会丢字段)。SL 兜底路径在规则评估前且只产 SL_*, 无需搬; 部分平仓/ghost 路径由每轮 meta 写回 (L5432) 兜底。
2. **判据尺子修正** (`first_night_gate_report.py`): "水下"判定优先读 `stall_exit.cur_at_trigger` (触发现场); 无埋点存量单回退落库 PnL 容差 ±半tick×qty。重跑 07-24: MRK 转 ✅, **四判据全过 (结论行从"1项异常"转"全部通过")**。
**验证**: py_compile 2 文件 ✓; `test_playbook_exit_rules.py` 1 passed (存量 44 场景套件零回归); 定制冒烟 3 场景 (触发+埋点/水上无埋点/曾浮盈无埋点) ✓。
**生效**: 盘后 playbook 0 持仓零风险窗口, 07:49 CST 已重启 (ConfigGov 全绿/模拟盘/0 Traceback), 周一开盘即带埋点。回滚: 两文件各自 git revert, 不影响三门开关。

### 2026-07-25 (凌晨·现场修复批) · 巡检定性项提前修复 — 5 项落地, 04:20 重启生效 (用户授权"能现场修的不等周末"+重启权)
**范围纪律**: 只修巡检/日志已定性、有明确处方的小改动; 大项 (P-1 名单合一 / P-2 订阅动态化 / P0.2 conf修复 / UE tier透传) 仍留周末。零实单退出参数变更, 零 config_version 断点。
1. **启动预热绕过静默门 → ✅修** (`signal_pipeline.py::run_signal_scan`): 外部入口 (ImmediateScan/force_scan) 补同款 `strategy.main_scan_enabled` 门, 静默时直接 return。冒烟: mock 双场景 (静默不下钻/开启下钻) ✓。回滚: 删该块。
2. **Reconciler broker_only 补录缺 playbook 守卫 → ✅修** (`reconciler.py::_add_legacy_position`): 补录路径补 `is_playbook_open_position` 守卫 (与收养路径同款), playbook 仓不再被补录成 unknown+protected stub。冒烟: 双场景 (playbook跳过/非playbook照常) ✓。验收: 下次重启无 "broker_only 补录: <playbook持仓>" 日志。
3. **解锁 spam 反模式全量收敛 → ✅修**: 剩余 9 处 `ManualOrderService()` 直构全部改 `get_manual_order_service()` 单例 + 删 disconnect (thesis_challenge×3 / plan/pipeline×2 / llm_validator / compute_indicators_full / plan_feedback / premarket_enrichment)。与 07-24 夜 fundamental_provider 同款处方; 至此全仓直构仅剩单例工厂本体。
4. **ConfigGov 热重载失联 (周末必修项提前) → ✅修**, 双管齐下:
   - 根因实锤: main.py Layer 1.7 strict 审计在 Layer 2/3 之前, 存量 BLOCKING (`CFG.MIN_CONFIDENCE 0.7 ≠ YAML 0.8`, 全仓仅此一条) raise 被外层"降级运行"吞 → 漂移检测+热重载根本没启动。
   - (a) 消漂移: `config_defaults.py` MIN_CONFIDENCE 0.70→0.80 同步 YAML SSOT。影响面已查: 运行时真值由 resolver 读 YAML (intraday=strategies.defaults 0.7 / ssot 0.8) **不变**; 变化仅 resolver 异常兜底 + DEGRADED 降档地板 (0.7→0.8, 更保守, 且主线扫描已静默=休眠路径)。
   - (b) 结构加固: main.py Layer 2/3 启动前置、strict 审计后置 (告警/raise 语义不变) — 未来再有存量 BLOCKING 不再连坐热重载。
   - 验证: venv 实测 audit BLOCKING 1→0, resolver 各 phase 取值不变 ✓。验收: 重启后日志见 "[ConfigGov] ✅ 热重载已启动" + "[ConfigAudit] ✅ 审计通过"。
5. **🔪 减法: P0 MFE shadow 死路径移除** (`position_monitor.py` L1084-1142 整块删, 留墓碑注释指向 e_shadow_*): 无脚本消费者 (仅 DEB 文档引用且已同步标注), jsonl 从未产出。
**验证**: py_compile 11 文件 ✓; 定制冒烟 4 场景 ✓; pytest tests/risk+core+config 92 过/16 败 — git stash 对照基线同样 16 败 (含 test_exit_engine.py 引用已删模块, 均基线遗留) → **零新回归**。
**生效安排**: 盘中不重启 (守首夜三门+8持仓观察不受污染), **04:20 CST (收盘后) 定时重启 `ftseek-us.service`** 并验启动日志 (ConfigGov 全绿 / 无预热扫描 / reconciler 无 playbook stub)。回滚: 各项独立 git revert, 互不耦合。

**04:20 重启验证结果 (2026-07-25 04:21:46 CST 启动 / ET 16:21:46, 已过 16:00 收盘)**:
- 服务状态: `systemctl restart ftseek-us` 成功, PID 46905 运行中 ✅
- 运行模式: 模拟盘 `TRADE_ENV=1 | FUTU_TRADE_ENV=1` ✅
- 判据① [ConfigGov]: `[ConfigGov] ✅ 漂移检测已启动 (interval=4h)` + `[ConfigGov] ✅ 热重载已启动 (cooldown=60s)` + `[ConfigAudit] ✅ SSOT 配置一致性审计通过 — 无漂移检测` (0 BLOCKING) ✅
- 判据② 无启动期主线扫描: 重启后日志无「开始信号扫描」「自适应扫描」「run_signal_scan」等批量 stages Pipeline 痕迹 ✅
- 判据③ Reconciler 无 playbook 补录: `[M1] 启动对账完成: matched=0 broker_only=0 local_only=0 legacy_tagged=0`, 未见「broker_only 补录」或 playbook 持仓 stub ✅
- 附加: Futu 连接正常, OpenD 127.0.0.1:11111 连通; 启动后仅 1 次 `SubscriptionManager reconnect_failed_x1` 瞬时告警, 3ms 内重连成功并恢复 `FutuLV3·历史 盘后连接 OpenD 成功`; 无 Traceback, 无 ERROR 循环 ✅
- 重启后 `early_acceptance_check.py --phase OPEN`: 今日开仓 8 笔/已平 8 笔, 入场滑点 8/8, streak_ban=159 low_atr_block=265, 通过 ✅
- **结论: 三判据全绿, 无需回滚, 凌晨修复批全部生效。**

### 2026-07-24 (深夜·现场巡检) · 首夜实盘全面体检 — 8 绿 3 黄 1 减法发现
**✅ 健康项 (实测)**: ①解锁spam修复稳定(0次) ②主线扫描静默生效("开始信号扫描"0次) ③三门live实拦: 连败封禁拒 MSFT/PDD 新触发 + GOOG SHORT 由 SL_TRAIL_LOCK 锁利 +$5.5 ④**C1 往返滑点闭环首夜双样本**: GOOG 入场 2.03bps + 出场 2.19bps (往返 ~4bps 极低, S1 降级再坐实) ⑤今日 18 指令 regime/atr 覆盖 18/18 (方向4首夜达成) ⑥入场滑点 8/8 全录(中位~2.6bps) ⑦E shadow 45/60/90min 检查点 7/7 实采 ⑧Futu 启动抖动后 20s 退出循环稳定。
**⚠ 异常定性 (周末处置, 今夜均无实害)**:
1. **启动预热绕过静默门**: `_run_signal_scan_body`(:517) 启动期跑了一轮 stages Pipeline (44 标的, 23:41-42, 后零复发); 三重下单锁在(signal拦截+执行冻结+source白名单) → 预热路径补同款 main_scan_enabled 门。
2. **Reconciler broker_only 补录缺 playbook 守卫**: 每次重启把 7 个 playbook 仓补录成 unknown+protected stub (收养路径有 is_playbook_open_position 守卫, 补录路径没有) → 补同款守卫。
3. **TwelveData 429 自熔断 10min** (备源自恢复, 无害, 仅记录)。
**🔪 减法发现**: 今晨 P0 MFE shadow (position_monitor → mfe_early_exit.jsonl) 是**挂错宿主的死路径**(playbook 仓不进主线 store); 同功能由正确宿主的 e_shadow_45/60/90m (trade_records.metadata) 覆盖且首夜 7/7 实采 → 分析 SQL 改读 e_shadow_*, 死块周末移除。方法教训: **落 shadow 前先查正确宿主是否已有同功能埋点**。
**待观察 (今夜剩余)**: 首批 MFE_STALL_EXIT 资格约 00:55 CST (TSM/LMT/RTX/TMO/MRK 五候选若仍水下); EOD 处置; 00:30 检查点。

### 2026-07-25 (凌晨) · topup 路径今夜验证尝试 — 机制全绿, 策略诚实 +0, 完整路径待周一
**背景**: 用户要求今夜走通 topup 成交路径。双窗已过 → 临时延盘中窗至 1345 ET (不降质量标准, 仅加评估时点)。发现: yaml 热重载失效 (ConfigGov 因存量 MIN_CONFIDENCE BLOCKING 审计降级失联 — **存量告警升级为功能损失, 周末必修**) → 00:23 重启生效。
**结果**: 机制全绿 — 窗口门/D2退避(尝试 1/2 → 15min → 2/2 封顶, live 首验完美)/11 候选评估/无方向不硬凑, 全部按设计。**策略层两次实时价评估 (12:23/12:38 ET) 均 +0** — 池内今日确无"趋势+回调到位"setup, 宁缺毋滥是诚实答案。未验段 (写库→触发→成交→退出→归因) 需真 setup 日 — 周一 10:30-11:30 ET 窗口。盘中窗已恢复 1130 (yaml 已改, 下次重启生效, 周末批次会重启)。

### 2026-07-24 (夜·P0事故修复) · 实盘解锁 spam 止血 — FundamentalProvider 反模式
**现象**: 盘中无人手动下单, 但 manual_order_service 每 ~3s 一次"连接+实盘解锁", 撞富途限频 (30s/10次) 报 ERROR。
**根因链**: topup 盘中窗 (22:24 重启提前激活) 每 ~2min 扫 11 只 → Screener → `FundamentalProvider._fetch_futu` **每只标的 new 全新 ManualOrderService + connect()** (绕过单例) → connect() 对实盘无条件 unlock_trade → 解锁 spam。放大器 = 观察② +0 重试风暴。
**修复** (`app/data/providers/fundamental_provider.py`): 改用 `get_manual_order_service()` 单例 (connected 短路→零额外解锁) + 删 disconnect (单例连接常驻, 不可断 dashboard 共享连接)。23:01 重启, 8 持仓全部重新接管。
**验证**: 23:02 后 6.5min 内 topup/Screener 22 轮次活动, 解锁次数 **0** (此前同负载每轮 8-11 次); 预期未来每进程生命周期最多 1 次首连解锁。
**遗留** (周末收敛, 入减法第1批): 同款反模式共 **9 处** `ManualOrderService()` 直接构造 (thesis_challenge×3 / plan/×4 / premarket_enrichment) — 均低频路径但同存解锁副作用, 统一改单例。另: 行情快照类需求长期应脱离交易服务 (只建 quote_ctx 不碰解锁), 归入减法 B 类评估。

### 2026-07-24 (晚) · 指令链回溯 + topup 首跑验证 — 零代码改动, 四条计入观察

**指令链回溯 (全实测)**: 今晚 13 条指令全部来自 Hermes DA skill (v0.61) 直写 DB — 18:05→19:31 CST 分析 ~48 只 → 19:31:31 写 `opportunity_scan_2026-07-24.json` + 同秒批写 `playbook_instructions` (与 `daily_battle_plan_us.json` 无关, 那是 07-23 旧产物)。漏斗: 48 只 → gate 全部 SHORT/SKIP/tier差 BLOCK → 13 只 BUY tier中 WARN 放行。推送口径: 35% = gate WARN 罚 (0.50−0.15, 辩论弃权未罚, 语义置信度在 bridge `confidence_semantic`); 0股/风险$0 = sizing 执行时才算; ❌ = confidence<50 图标非未成交。

**topup 首跑验证 (设计路径自动触发, 非手动)**: 机制全过 — 时间门 (08:31 干跑 None / 08:45:02 首触发)、循环接线 (~2.5min/轮)、缺口计算 (13<25→补≤12)、池去重 (排 NVDA/MU/MSFT)、Screener 13/13 过质量门、无方向不硬凑 (+0 零写入)。+0 根因 (离线逐只复现, ctx 字段健康): 8 只 NEUTRAL 无方向 + 3 只有方向非回调窗 (TSLA/NFLX/QCOM) + 2 只回调质量 q=0.3<0.5 (GOOG/AAPL)。cautious 大盘下池内无"趋势明确+回调到位"标的, 是 technical 策略的诚实答案, 非数据/代码故障。

**计入观察 (验证期不动手, 9 月或用户裁决再议)**:
1. **过滤审计盲区 (修尺子候选)**: `_build_instructions_from_votes` 的 `vote_result is None` 分支 (pipeline.py:765) 无计数器 → 审计 breakdown 全 0 却 skips=13 无法自解释; `TechnicalFallbackAdapter` None 路径无 info 日志。一行计数+一行日志可补。
2. **topup +0 重试风暴 (行为变更, 排队)**: 幂等标记仅在 ≥1 条写入时落, +0 → 08:45–09:30 每轮重扫 13 只 (~60s/轮 ≈18 轮近连续)。候选方向: +0 也落标记 / 退避。
3. **gate RR<1 不 BLOCK (skill 侧)**: BABA 指令 R:R 0.65 仅 WARN 放行 — signal_gate 的 WARN 理由无 RR 拦截; 是否加 RR 下限硬门待裁决。
4. **双 adapter 弃权口径分叉 (休眠)**: skill 侧 adapter 07-18 已修"弃权不罚", FTSeek 侧 `BridgeVotingAdapter` (pipeline.py:414-428) 仍把 BUY↔SKIP 当分歧罚 0.15 (且 "BUY"≠"LONG" 字符串比较会误罚方向一致); 今晚直写 DB 路径未触发, 若 bridge 兜底路径复活需先对齐。

**topup 盘中窗实施 (知情破例已落地, 未激活)** — 用户裁决 2026-07-24 晚: 盘前批用昨收价评回调窗系统性偏陈旧 (首跑 +0 实证: 8 NEUTRAL/3 非回调窗/2 质量不足), 加盘中实时评估窗。
- 改动: `pipeline.py` 新增 `_topup_window()` (双窗口: 盘前 845-930 / 盘中 1030-1130 ET, 可配置) + `run_playbook_topup_supplement` 改分窗幂等 (raw.source+raw.timing, 历史无标记行视为 premarket) + 两窗共享 max_add_per_day=12 + 归因 `raw.timing=premarket/intraday`; `scheduler_core.py` TRADING 分支接线 (定桶 catch-up 前, 新行当轮补 vol_bucket/regime); `unified_config.yaml` `playbook.topup` 增 `premarket_end_et_hhmm:930 / intraday_start_et_hhmm:1030 / intraday_end_et_hhmm:1130`。
- 冒烟: py_compile ✓ + 窗口单测 13 例 ✓ + 桩写库全链 8 场景 ✓ (盘中首跑/分窗幂等/跨窗不阻塞/共享上限/窗外不触发/历史标记兼容), 零生产写入。
- **激活安排 (守一次一变量)**: 今日 (07-24) 已是退出三门首日 + 盘前 topup 已跑, 盘中窗**不在今日叠加** — 07-25 04:20 CST (美股收盘后) 重启 `ftseek-us.service`, 首个 live 盘中窗 = **周一 07-27 10:30-11:30 ET**。周一验证: 日志 `[playbook·topup] 补量启动 (intraday)` + DB 新行 `raw.timing=intraday` + 定桶/regime 当轮补齐 + 与盘前批效果切分。
- 回滚: `playbook.topup.enabled: false` (整体) 或把 `intraday_start/end` 设为相同值 (仅关盘中窗); 代码级 git revert 两 .py 文件。

**0成交诊断 (22:20 CST 实测, 用户问"还没有自动成交是否正常")** — 机制全部正常, 成交为零是 OKR 账户熔断按设计拦截:
- 链路实测: 18 条指令 (13 DA + **5 条 UE** `universe_expansion` 09:10 ET 写入: TSM/LMT/RTX BUY + GOOG/XLY SELL_SHORT — 非 topup 非 catch-up 生成); 8 条价格触发 (triggered=1: TMO/NVDA/BABA/MRK/TSM/LMT/RTX/GOOG); 触发检测/低ATR门 (拦 CSX/JPM/KO/MA/XLY)/连败封禁 (拦 MSFT/PDD/UNH)/量能门/预算门 全部按设计工作。
- **0 成交根因**: `capital_state` peak=101,263.65 (2026-06-25 创立) vs equity=98,067.69 (全现金无持仓) → 回撤 **3.156% ≥ 3%** → AccountCircuitBreaker 动作=**LIQUIDATE** → `playbook_service.execute_instruction` 入口拒绝 ("[OKR熔断] 暂停 Playbook 执行", 09:37 ET 首轮起 40+ 次)。06-25 上线的 OKR 风控规则在生效, 非故障。昨两日盘中权益高于 98,226 (3%线) 故判 NORMAL; 昨日 EOD 平仓后权益定死 98,068 → 今起全天越线。
- **⚠ 自锁状态 (需用户裁决, 不擅自动风控)**: 不能开仓 → 权益无法回到 peak → 熔断永不解除。选项: (a) 维持现状 ( playbook 停摆待自然变化) (b) 重置 peak 基准 (等于豁免历史亏损, 08 终裁框架外的破例) (c) 调 3% 阈值。在裁决前 playbook 新开仓持续为 0。
- 对今日验证影响: 触发侧两门 (连败/低ATR) 拦截日志已验证; MFE_STALL_EXIT (持仓门) 今日无新仓 → 无样本; topup 盘前批 +0 已验证; 盘中窗 07-27 激活计划不变。

5. **观察⑤ — `_et_trade_date(now_et())` 双重转换 bug → ✅ 已修复 (07-24 盘中, 用户授权"明确 bug 可以做修复")**: `_et_trade_date` 契约是"naive CST − 12h → ET 日期", 但读侧传入**已是 ET** 的 `now_et()` → ET 中午前日期=昨天。实锤: 熔断日志"日亏损=-0.39%"精确等于 07-23 已实现亏损 (−$379.21/98,068=−0.3867%)。**单点修复** (`app/utils/timezone.py`): tz-aware 输入走 `astimezone(ET).date()` 分支, naive 输入维持 CST−12h 契约 — 一处改动覆盖全部 13 处 `now_et()` 调用点 (account_circuit_breaker / absolute_circuit_breaker×2 / drawdown_control×2 / playbook risk_circuit_breaker / trade_store×2 / order_executor×2 / feishu_push×3 / weekly_report) + 6 处 aware `position.entry_time` 调用点 (order_executor×5 / position_monitor×2)。安全性论据: 写入侧实测本就用正确标签 (今日 8 笔成交 trade_date='2026-07-24'), 只修读侧无读写分裂; naive 分支零变化。验证: py_compile ✓ + 6 用例 (aware ET/UTC/CST + naive CST 边界) ✓ + 22:49 重启后 venv 生产直测 `_et_trade_date(now_et())='2026-07-24'`、熔断器 `NORMAL 日盈亏=0.00%` (真实当日, 修复前=-0.39%) + 零 Traceback。⚠ 口径注意: 修复后 HALT/REDUCE 档读**真实当日**日亏 — 今起若当日已实现亏损 ≥1%/2% 会按设计触发 (非误报)。回滚: git revert `app/utils/timezone.py` 该 hunk。

**心跳"今日"时区统一 (00:4x 用户问"是否还跨时区, 请统一时区统计") → ✅ 已修**:
- 对账结论: 00:23 北京 (12:23 ET) 心跳"已平盈亏 +$5.52/1笔"**未跨时区** — 与 trade_records 精确一致 (当时仅 GOOG 11:02 ET +5.52 已平; TSM 12:30/NVDA 12:43 在其后)。
- 但发现隐患: 心跳 SQL 用 `date('now','-4 hours')` (UTC−4h) 近似 ET — 夏令时碰巧正确, **冬季 EST 晚间 20:00-24:00 会把"今日"错成次日**, 且与系统唯一真源 `_et_trade_date(now_et())` 口径分裂。
- 修复: `intraday_heartbeat._query_today_trades` 改为 Python 侧 `_et_trade_date(now_et())` 作 SQL 参数 — 与熔断器/写入标签同一口径, EDT/EST 自动。venv 直测: 今日 3 笔 +$39.12 (GOOG+5.52/TSM+5.65/NVDA+27.95), 持仓 5 只 ✓ → 00:53 重启, 下一次心跳起生效。全仓扫描确认 `date('now','-4 hours')` 近似仅此一处。
- 6. **计入观察⑥ (心跳健康探测回退)**: 00:23 心跳"已运行 0s | thread=n/a broker=n/a" = `_query_service_health` 异常被 `_safe_query` 吞掉回退默认值 (服务实际已运行 1.5h)。探测失败原因待查, 非时区问题。

**熔断解锁执行 (22:20 用户裁决: "需要解锁, 在模拟账号中尽快实现交易收集数据" — 知情破例, 选 peak 重置方案)**:
- 操作: `capital_state.peak_equity` 101,263.65 → **98,067.69** (17,911 行全量更新防竞态) → 22:24 `systemctl restart ftseek-us` (加载 DB 峰值, 内存单例无他法)。旧 peak 已存档于本节上文。ACB 绝对熔断事前验证=放行 (07-23 EOD MRK/MA 盈利已清连败计数), 单锁无叠加。
- 结果 (22:26 首个执行周期, 全部 futu_simulate 真实模拟成交+钉钉推送): **8/8 触发指令全部成交** — DA 4 笔: TMO 4股@563.08 / NVDA 13股@207.88 / BABA 21股@112.24 / MRK 24股@131.06; UE 4 笔: TSM@406.59 / LMT@585.32 / RTX@214.24 / GOOG SHORT@320.63。熔断器恢复"账户正常" (回撤 0.00%)。连败封禁 (MSFT/PDD/UNH) 与低ATR门照常拦截, 风控其余维度未动。
- **副作用 (透明记录)**: 本次重启把 topup 盘中窗提前一天激活 — **首个 live 盘中窗=今日 10:30-11:30 ET (非周一)**。首两轮 22:30/22:32 各 +0 (池 11 只, 无方向/被门拦; 与盘前批同因: cautious 下池内无"趋势+回调"标的)。`raw.timing=intraday` 归因字段就位, 效果可按 timing 切分, 口径不受污染。观察② (+0 重试风暴) 在新窗口复现 (~2min/轮), 符合既有记录。原定 07-25 04:20 重启 cron 已取消 (激活目的达成)。
- 后续: 持仓 8 笔由 playbook 退出监控+独立快循环托管, MFE_STALL_EXIT 等三门首次有真实持仓可管; 明日盘前按常态。

### 2026-07-24 (复核批) · 第二模型独立复核 — X2 第四前置 + shadow 异常当日结案
**锚点**: 主文档 `docs/debates/DEB-20260724-monitoring-rigidity-premarket-intelligence.md` §十三 (第 5 轮) + §十四 (第 6 轮作者复核)。用户委托第二模型对当日四文档独立复核: 方法论与证据链通过, C0~C5 无推翻。
1. **X2 第四前置 — SHORT 不隔夜** (立案保留, **叙事已被第6轮同窗复核修正**): 原叙事"日内有效隔夜衰减"不成立 — 7月 SHORT 全时限无效 (日内39.0%/T+1 44.1%/T+3 37.9%, n=59), 65.2%WR 系 6 月旧时代样本。真命题 = SHORT 入场质量门 (待立案); "不隔夜"作为止损性约束保留且更强。
2. **shadow "+$903" 异常 — 立案当日结案** (第6轮对账确认): okr by_strategy 的 playbook_shadow +$903/21笔 实查为 2026-06-22~23 旧影子队列, 30d 滑窗滤掉 06-22 两笔 (−$660.95) 后数字逐项吻合; 非现存正期望口袋, 07-24 起自然 age out。
3. **三件事优先级** (第6轮修正后): ① C2 第二步 **非零风险** (prompt=信号分布源头, v2.0 相变前车之鉴) → 先 scan-eval 离线 A/B, 首夜+模型A/B第2轮收尾后再上 live ② exit_shadow 毕业**基线已失效** (三门改了退出栈) → 窗口从 07-24 新基线重起 ③ 真反向→SKIP 攒 ≥30 样本裁决。

### 2026-07-24 · 退出三门 live + 扩容补量 + 滑点裁决 + 模型A/B — 当日全部逻辑见两份 DEB
**主文档**: `docs/debates/DEB-20260724-monitoring-rigidity-premarket-intelligence.md` (诊断+共识C0~C5+落地记录+topup+提前验收, §十二有全索引) · `docs/debates/DEB-20260724-challenger-model-ab.md` (模型切换评估)
1. **三门 live** (crash-day DEB 第8轮, 绕过 shadow-first 经用户裁决): ① MFE_STALL_EXIT (持仓≥150min且峰值浮盈<0.5%且水下→提前离场, 历史 0/37 零例外) ② 同标的低MFE连败封禁 (触发侧) ③ 低ATR硬门 (atr_pct<2.5% 拒触发)。开关: `mfe_stall_exit_enabled`/`symbol_streak_ban_enabled`/`low_atr_gate_enabled`。
2. **regime/VIX/SPY 落库补全**: DA/UE 直写指令三字段恒 NULL → 盘前 `run_playbook_normalize_vol_buckets` 统一补写。
3. **滑点裁决**: 入场滑点已有基建+50笔数据, 中位 4.6bps < 5bps 预注册线 → 限价单方案降级不建; 补了**出场侧**滑点埋点 (`exit_slippage_bps`, 往返另一半)。
4. **论据三分类记分牌**: `scripts/factor_verdict_scorecard.py` → 16 个"方向错用"(信号真方向反, 如 valuation_expensive IC 0.20/α-0.57%) vs 仅 3 个真该剔 — 回灌 prompt 为第二步待裁决。
5. **扩容补量 topup** (`playbook.topup`, target 25/日): 指令不足时从 16 只流动性池走**与 DA 同链**补足 (质量门+technical确定性方向, 无方向不硬凑); 归因 `raw.source=topup_pool`, **评估必须按 source 切分**。注: 旧 ADR 的 fallback_pool 从未实现, 已由此替代。
6. **链路提前验收三阶段**: `scripts/early_acceptance_check.py` (PRE/OPEN/MID)。PRE 干跑: 三门将拦 11/21, 被拦组 07-23 实亏 -$311(1胜) vs 放行组 -$68(5胜)。
7. **挑战者模型 A/B** (deepseek-v4-pro vs glm-5.2, n=15 record-only): 机械判据 glm 全优 (快29%/零空响应) 但人格更保守 (93% SKIP vs deepseek 敢给 4 个反向) — 与 SHORT 恢复目标冲突, 首轮裁决不切。DA/UE 配置**不统一** (DA=skill硬编码, UE=统一路由表), 收敛待办。
8. **时区双轨坑** (分析时必踩): 遥测 jsonl 的 date=CST, trade_records=ET, 跨表 join 需双日兼容。
9. **方向信心体系** (回应"对标的/方向没底气"): 新建方向命中率记分牌 (工具表) — 7月全部 51.9%≈掷硬币, 但辩论共识分层单调有效 (分歧45.1% vs 一致58.1%)。细切后发现坑: conf≤40 桶 87% 是"弃权"(近中性不该杀), 真反向仅 n=10 (40%/-0.51%) — 立案 `docs/debates/DEB-20260724-debate-divergence-consumption.md`: 真反向→SKIP shadow + 弃权豁免(并入P0.2) + 辩论产物透传落库; 一刀切"分歧→SKIP"已自我否决。

### 2026-07-20 (四批) · 退出策略全链路审计修复 — 2P0 + 2P1

**背景**: 退出策略配置层→分桶匹配→动态SL→退出优先级链→影子评估 全链路交叉比对。

**P0-1 — 7/19 dd=0.8 处方被因子分桶架空** (`config/playbook_horizon.yaml`)
- 通俗: 7/19 把 default 的锁利回撤从 0.4 放宽到 0.8（释放赢家），但 15 个趋势类因子规则仍是 0.5 → 绝大多数带因子标签的单子实际还在用 0.5，处方没喂到病人嘴里。下周五 exit_scorecard 会得出"放宽没效果"的假阴性。
- 修复: 全部 15 个 `trailing_enabled: true` 因子的 `drawdown_from_peak_pct` 0.5→0.8 对齐 default。`trailing=false`（快速证伪类）不动（无 dd 字段，走 profit_override 语义）。
- 回滚: 改回 0.5。

**P0-2 — SHORT 配上多头退出规则（方向分桶漏洞）** (`app/playbook/pipeline.py` `_get_factor_exit_config`)
- 通俗: SHORT 指令的 conditions 若全是 bullish 因子（如逆向做空过热股），第一轮方向匹配空手后，第二轮原样取第一个有规则的 condition → SHORT 拿到 `trailing=true, max_hold=3天` 的多头规则。与 7/7 fix 目标直接矛盾。
- 修复: 第二轮回退增加方向过滤 — 跳过方向相反的 condition，全部跳过则落 default（max_hold=1天，对 SHORT 合理）。direction=None 兼容旧行为。
- 冒烟: 5 场景全过（SHORT+bullish→default / LONG+bullish→因子 / SHORT+bearish→空头规则 / LONG+bearish→default / None→兼容）。

**P1-1 — 影子 baseline 漂移 + fee_cushion 缺失** (`app/playbook/exit_shadow.py`)
- baseline_be1.0_dd0.5 注释称"对照=现行default"，但现行 default 已是 be0.5/dd0.8 → 对照组失真。
- 修复: ① 新增 `baseline_be0.5_dd0.8` 为正确对照，旧 baseline 标记 LEGACY 保留（不破坏 in-progress 数据键名）；② `_variant_effective_sl` 补 `fee_cushion_pct` 参数，保本线数学对齐实盘 pipeline L3961。
- 冒烟: LONG/SHORT fee_cushion 方向正确 + dd 主导时不干扰 + 新旧 baseline 共存。

**P1-2 — rr_ratio=999 与 vol_bucket TP 冲突（注释澄清）** (`config/playbook_horizon.yaml`)
- 999 哨兵（"去TP"决策）实际从未生效：instruction_generator L399 vol_bucket 路径覆写 rr_ratio=tp_multiplier(1.5~2.0)。配置声明与真实行为相反。
- 处理: 加注释澄清真实行为（每单有TP, RR 1.5-2.0），待数据裁决去留。不改代码。

**已生效: 2026-07-20 重启 `ftseek-us.service` (四批)。**

### 2026-07-20 (三批) · 实盘就绪 P0 预备 — B4 数据新鲜度门 + B3 交易环境横幅

**背景**: 富途平台架构深查后确定的实盘前 P0 清单中, 先落地模拟盘可验证的两项。

**B4 — 开仓数据新鲜度门 (降级/延迟行情禁开新仓)** (`app/risk/data_health_guard.py` + `playbook_service.py` + `playbook/config.py` + `futu_lv3_provider.py`)
- 通俗: 行情降级到 Finnhub 时报价延迟 15 分钟, 系统之前会照常按旧价开仓; 现在降级期间/报价过期 (>90s) 一律禁止开新仓, 平仓不受影响。
- 专业: 三处配合 — ① 根因: provider L850 曾把 fallback 报价 `timestamp` 覆写为抓取时间 (把 15min 延迟"洗白", 摧毁 07-19 finnhub C3 修复), 改为仅缺失时兜底; ② 新增 `check_quote_freshness()` (不依赖 bid/ask, 补 `check_quote_health` 盲区: 降级源恰好无盘口 → 整体绕过 health 门), 降级源集合 `DEGRADED_QUOTE_SOURCES` + 时间戳年龄双判定, 兼容 Unix float / ISO 字符串; ③ 接线 playbook 开仓汇合点 (health 门之后), 配置 `entry_freshness_gate_enabled: True` / `entry_max_quote_age_sec: 90.0`, 取价失败维持 fail-open 与既有语义一致。
- 冒烟: 7 场景全过 (finnhub 拦/fallback+兜底盘口拦[health门盲区实证]/futu过期拦/ISO过期拦/新鲜放行/无时间戳放行/配置键)。
- 回滚: `entry_freshness_gate_enabled: False` (一键) 或删 playbook_service 新鲜度门块。

**B3 — 启动交易环境横幅** (`app/core/scheduler_core.py` start 处)
- 启动时醒目打印 AUTO_TRADE_ACCOUNT / ALLOW_REAL_TRADING / execute_mode / acb·preflight_fail_mode; real 模式用 WARNING 级 + "实盘要求均为 closed" 提示。纯可见性, 硬拦截仍在 di_container LIVE 启动断言 (已存在, L158-179: real + fail-open → 拒绝启动)。
- 排查确认: 原计划的 "precheck_fail_mode 切换预案" 大部分已存在 — di_container 已有 LIVE 启动断言, 本批只补横幅可见性。

**已生效: 2026-07-20 重启 `ftseek-us.service` (三批)。**

### 2026-07-20 (二批) · 开盘前全链路隐藏 bug 排查 — 4 处实锤修复

**背景**: 由 `_track_bar_peak_trough` SHORT 方向语义 bug 触发的同类问题全链路深查 (方向语义/数据源语义/键名契约/截断行为), 目标: 消除开盘交易的不利因素。

**B1 — VWAP 硬门数据源语义错配 (P0 行为级)** (`app/data/market_data_router.py` + `pipeline.py` VWAP门)
- 通俗: 实盘启用的 VWAP 方向门, 一直在拿"60天平均价"当"今天的 VWAP"用 — 近期上涨的股票做空恒被拦, 下跌的股票做多恒被拦, 方向性系统误拦。
- 专业: `ctx["vwap"]` 主路径来自 `calculate_from_klines(daily_klines)` (60根日K 全量 cumsum), 07-16 修复的"当日累计 VWAP"假设只在日K不足的 1M fallback 路径成立。修复: router 用 1min K 过滤今日 bar 单独算 cum(tp×vol)/cum(vol), 新增 `ts_ctx["vwap_intraday"]` (门专用, 0=今日bar<5 视缺失) 并覆盖 `ts_ctx["vwap"]`; 门侧优先读 `vwap_intraday`, 缺失 fail-open。
- 回滚: 删 router 当日VWAP块 + 门侧改回 `ctx.get("vwap")`。

**B2 — get_kline HTTP 冷路径旧数据截断 (P0 数据完整性)** (router `count 200→800`, pipeline 两处 `240→800`)
- 通俗: 冷缓存时问券商要 K 线, 要少了会拿到"昨天最旧的一段"而不是"最新的", 导致今天的数据全为空。
- 专业: get_kline HTTP 路径 `start=T-1` 范围含 ~780 根 1min bar, OpenD 对 `max_count<范围总数` 返回**最旧N根** (provider 06-30 注释实证)。count=200/240 时冷路径拿纯 T-1 bar → 今日过滤恒空: 盘中量能门恒 fail-open、#3 bar 峰谷采集失效、`slope_5m` 算的是昨天中午的斜率。修复: 三处 count → 800 (覆盖 T-1+今日全时段, 仍单次请求不多耗配额)。已知残留: push 缓存仅存 300 根, 15:30 后 push 路径今日累计量略低估 (触发窗 14:00 截止, 影响可忽略)。

**B3 — ORB 开盘区间键名契约 (P1)** (router ORB 段)
- 专业: HTTP 路径 bar 键是 `time`, push 路径是 `time_key`; ORB 段只认 `time_key` → HTTP 冷路径下 `opening_high/low` 永不注入 → ORB 策略 `opening_range_ready` 恒 False。修复: `k.get("time_key") or k.get("time")`。

**B4 — 归因 peak/trough SHORT 塞错桶 (P1 归因失真)** (`app/analysis/trade_attribution.py`)
- 通俗: 与 bar 峰谷同款方向 bug — 做空单的"最有利价(最低)"被塞进高价桶、"最不利价(最高)"被塞进低价桶, 被 max/min 直接忽略。
- 专业: SHORT 时 `metadata.peak_price`=持仓期最低价/`trough_price`=最高价, 原实现不分方向 peak→all_highs/trough→all_lows → SHORT 的 D2入场/D3出场/D5锁利维度退化回 entry/exit 近似。修复: 按 direction 分桶。冒烟: SHORT 100→97 (peak95/trough102) 的 best_exit 修复前 97 (假"精准") → 修复后 95。
- 同批新增 (用户手写): `_calc_intraday_vol_ratio` 盘中实时量能比替代原日级 volume_ratio (审查通过, 契约/时区/fail-open 均正确; 已知偏差: elapsed_fraction 线性假设 vs 成交量 U 型分布 → 早盘门偏松/午后偏严, 14:00 截止后无影响)。
- 排查确认无问题: 触发链路方向语义 (漂移作废/SL穿越/VWAP侧向/miss分类)、MFE/MAE SHORT 反算、`_is_instruction_expired` timezone、entry_time 时区 (带-04:00 与 futu ET wall time 对齐)、volume/time 字段契约、#6 _tr/_meta 收敛。
- **已生效: 2026-07-20 14:16 CST 重启 `ftseek-us.service` 完成** (启动日志验证无导入错误, 对账/订阅/Watchdog 均正常; 代码已验证: py_compile 3 文件 + 量能比/归因双向冒烟全通过)。

### 2026-07-20 · 参数冻结下的结构性优化批次 (#1/#2/#3/#4/#6/#7, 全部不碰实单退出参数)

**背景**: 积累期参数冻结 (避免 config_version 断点), 本批只做归因补齐/测量精度/灾难兜底/可观测性, 零行为变更类改进一律 record-only。

**#1 — missed 指令 skip_reason 归因黑洞补齐** (`app/playbook/shadow_tracker.py`)
- 通俗: 94 条"没做成的交易"不知道为什么没做成, 现在每条都有说法。
- 专业: shadow_tracker 两个写 `outcome='missed'` 的分支不写 skip_reason → 代码修复 (仅原 reason 为空时补标, 不覆盖 auto_execute 具体拒因) + DB 回填 94 条 (81 no_trigger + 13 trigger_no_execution)。剩余空归因 = 0。

**#2 — 触发门反向选择周度判分器** (新建 `scripts/trigger_gate_report.py`, 只读)
- 通俗: "没追到的车是不是比追到的更好"固化成周度报表。
- 首跑结论: missed 组 fwd_return +0.915% vs executed +0.249% (缺口 0.666pp), **集中在 BUY 侧** (+1.152pp), SHORT 侧健康 (−0.219pp); 机会成本累计 $12,819。内置判读: 缺口>0 且双向同号稳健 且持续≥2周 → 下一个 shadow 变体应为"触发条件放宽" (目前双向不同号, 继续观察)。

**#3 — peak/trough 1min bar 并行采集 (record-only)** (`app/playbook/pipeline.py::_track_bar_peak_trough`)
- 通俗: 峰谷价以前靠"每轮瑞一眼报价", 两眼之间的最高/最低点全丢 (MFE 系统性低估约一半)。现在同时用 1 分钟 K 线高低点并行测一份。
- 专业: futu LV3 `get_kline(K_1M)` (300s 缓存, 基本不耗配额) 取入场后 bar 的 max(high)/min(low), 单调更新 `metadata.peak_price_bar/trough_price_bar`, 并进 `exit_telemetry`。**不参与任何退出决策**; 周度对比 bar 版 vs quote 版差距后再评估切换 (切换=行为变更, 需开新 config_version 窗口另行批准)。
- **同日人工审阅修复 (方向语义)**: 初版不分方向一律 peak=max(highs)/trough=min(lows), SHORT 时与 quote 版既有语义 (peak=最有利价, SHORT 取最低; trough=最不利价, SHORT 取最高) 完全反转 → SHORT 的 bar 版 MFE 会符号反转, 周度对比产生"bar 版更差"假象。修复: helper 加 `direction` 参数 (默认 LONG 向后兼容), SHORT 分支 peak=min(lows)/trough=max(highs)。双向冒烟通过; 周日修复, 盘中未跑过, 无脏数据需清洗。
- 回滚: 删 monitor 循环里 `_bar_track_new` 一行 + helper。

**#4 — grace 5min 不再完全豁免, 保留 4% 灾难兜底** (`pipeline.py::monitor_playbook_sim_exits`)
- 通俗: 入场后 5 分钟内以前是"完全不看盘", 闪崩 10% 也不管; 现在闪崩超 4% 照样斩。
- 专业: grace 期 `continue` 前先查是否击穿 `cooldown_sl_pct` (4%) 兜底线, 与规则 0 "冷却期放宽而非豁免"语义对齐。非参数变更 (4% 是既有冷却参数), 属灾难兜底补洞。

**#6 — `_close_trade_record` 读写收敛 (行为零改变)** (`pipeline.py`)
- 通俗: 平仓时对同一笔交易反复查数据库 10 次、写 3 次, 现在查 1 次、写 2 次, 内容一模一样。
- 专业: 函数开头读一次行快照 `_tr` + 解析一份 `_meta`, 各块 (MFE反算/exit_shadow/holding_hours/pnl_pct兜底/telemetry/ghost_cause/归因/推送) 顺序加工同一 dict; telemetry+ghost 合并为主 UPDATE 前一次写回, 归因写回保留 (commit 时点不变)。消除多次读改写的互相覆盖风险面。
- 验证: 临时 DB 全链路冒烟 (MFE 反算 5%/MAE 2%/telemetry 含 bar 峰谷/instruction 回写 win+MFE美元/open_positions 删除/归因落盘) 全部通过。

**#7 — void 率趋势接入 okr_status** (`scripts/okr_status.py` 新增 `void_trend` 段)
- 口径: void/(win+loss+void) 周度分组, ≥10% 打 ⚠️。首跑: W25 25.0% / W26 14.3% / W27 25.9% / **W28 2.9%** — 幽灵仓根因修复生效的趋势证据; 若回升 = 执行/对账链路又在产幽灵仓。

**未做 (有意为之)**: #5 broker 侧止损底线单 — 切 LIVE 前头号阻断项, 保持在毕业 checklist, SIM 阶段不做。

### 2026-07-20 · DEB-20260720 盲区审计修复批次: 保护毕业数据质量 + 零成本防真金 (修复 M/5/7/6a/3b)

**辩论锚点**: `docs/debates/DEB-20260720-blindspot-risk-audit.md` (三轮收敛)。当下 SIM 阶段做 5 项 (M+5+7+6a+3b), 全部保护毕业判定数据质量或零成本防真金; 修复 1/6b 为切 LIVE 头号阻断 (本批不做); 4/3a/2 延后。

**关键洞察 (元风险拆两半)**: 毕业判定分两个维度, 对退出参数污染的敏感度不同 —
- **IC 侧 (`IC(T+3)>0.05`) 天然免疫**: IC 基于 forward return = 入场价→N日后日线价, 是**入场锚定、退出无关**的量。改 time_exit/drawdown 不动 forward return。
- **PnL/胜率侧 (cap_reduction 45%胜率+正PnL) 受污染**: 已实现 PnL = 入场 edge × 退出执行。退出参数一变, 同一批入场信号的胜率就变。
→ 最优解不是"停止探索", 而是**打标 + 按版本取样 + 探索走 shadow** (修复M)。

**修复 M (P0) — 退出参数 config_version 打标 + 毕业统计按版本窗口 + 探索走 shadow**
- 通俗解释: 以前"边积累数据边调退出参数"就像"边煮饭边换锅", 最后不知道哪锅饭是哪个参数煮的, 毕业判定的胜率被多套参数搅浑。现在给每笔平仓记录盖一个"退出参数版本印章", 毕业统计只数**当前印章**的样本, 参数探索全部放到"影子"里跑 (不动真单)。
- 专业解释: (a) `PlaybookConfig.get_exit_config_version()` 对会改变已实现 PnL 的退出参数 (factor_exit_rules / vol_buckets / cooldown / early_tight / fee_cushion) 算稳定短哈希; (b) `_close_trade_record` 平仓时把版本写进 `metadata_json.config_version`; (c) `scripts/okr_status.py` 新增 `graduation_sample` 段, 用 `json_extract` 只取当前版本窗口的已平仓样本算胜率/PnL; (d) 参数探索继续走 `exit_shadow` (record-only)。
- 收益: 既保护毕业判定不被多套参数污染 (PnL 侧可比), 又不牺牲探索速度 (shadow 并行)。复用现有 metadata_json + shadow 框架, 零新基建 (未加 DB 列)。
- **联动: 修复 B (早期紧SL) 收编为 shadow-only** — `early_tight_sl_enabled: true→false`, 改由 `exit_shadow.EARLY_TIGHT_VARIANTS` (`early_tight_30m_1pct`) 采集反事实 `counterfactual_saved_pct`。避免又引入一个 config_version 断点。
- 回滚: 打标不可逆但无害 (只多一个 metadata 字段); 窗口化统计改回全样本即删 `graduation_sample` 段; early_tight 实单化改回 `enabled: true`。

**修复 5 (P1) — reconcile 幂等失效**
- 通俗解释: 对账用的"上次状态存档"文件有时会被清空成 2 字节 `{}`, 导致每次重启都以为"全是新持仓"重复告警/重复对账。根因是写文件时如果对账中途异常, 会用空存档覆盖好存档, 且异常被 debug 日志吞了看不见。
- 专业解释 (`app/positions/reconciler.py`): `_save_signatures` 增加防覆盖 — 本轮签名为空但历史非空时跳过写入 (保留好状态); 异常从 `logger.debug` 升级 `logger.warning`; `_load_signatures` 对"文件存在但空/非法"显式 WARN (幂等失效可见)。
- 收益: 消除每次重启的重复告警/重复对账, 对账幂等恢复。回滚: 移除防覆盖分支。

**修复 7 (P1) — NULL 口径污染绩效**
- 通俗解释: 统计"真实成交笔数"的 SQL 有个 NULL 陷阱 —— `exit_reason NOT IN(...)` 遇到 NULL 值会判成"未知"从而**漏掉**该行, 幽灵单反而被算进绩效。而毕业判定就是吃这个绩效。
- 专业解释: 全仓 6 个 web_dashboard 文件 (analytics/dashboard/risk/trade/data_service/thesis) 都存在同一模式 —— 正确的 `COALESCE(exit_reason,'') NOT IN(...)` 后面又跟一个冗余且 NULL-unsafe 的 `AND exit_reason NOT IN(...)`。系统性删除冗余子句, 保留 COALESCE 版作为唯一真相 (16 处 COALESCE 守卫全部保留)。
- 收益: 毕业判定输入干净 (PHANTOM/ORPHAN 不进胜率分母), 与修复 M 同一目标 (保护毕业数据) 的两面。回滚: git revert。

**修复 6a (P0 提前到现在) — LIVE 启动断言 (零数据扰动)**
- 通俗解释: 现在风控检查异常时是"放行开仓"(fail-open, SIM 期合理)。但万一某次误切真账户时忘了改成"拒绝开仓", 就会在真金上裸奔。加一道启动断言: 真账户模式下如果 fail 模式不是 closed, **直接拒绝启动**。SIM 模式完全不受影响。
- 专业解释 (`app/core/di_container.py`): 仅在 `_auto_account=='real'` 分支内, 断言 `risk.acb_fail_mode==closed && risk.preflight_fail_mode==closed`, 否则 `raise RuntimeError` 拒绝启动 (配置读取失败也拒绝)。
- 收益: 零成本防止积累期误配置/误切 real 时 fail-open 裸奔。回滚: 移除断言块。

**修复 3b (P0 起逐步) — 平仓失败可观测性**
- 通俗解释: 以前"为什么平仓失败/仓位卡住"这一半是黑暗的 (异常被吞进 debug 日志)。平仓失败=仓位卡住无止损保护, 是切实盘后最致命的场景。现在平仓失败升级为结构化 error + 飞书 HIGH 告警。
- 专业解释 (`app/playbook/pipeline.py`): 新增 `_alert_close_failure(symbol, env, reason, detail)` (WARN 日志 + push HIGH, 按 symbol+日+reason 去重防刷屏), 接入正常退出/SL兜底/EOD兜底三条平仓链路的所有卡住点 (BROKER_EXCEPTION / CLOSE_FAILED / ORDER_NOT_FILLED / SL_* / EOD_* 连续失败≥3)。record-only, 不改平仓行为。
- 收益: 补上"为什么平仓失败"这一半黑暗; 当下 SIM 也能先积累平仓失败画像。回滚: 移除 helper 调用。

**验证**: py_compile 全过 (12 文件); `get_exit_config_version()` 实测产出稳定哈希 `exit-4bc8f6797088` 且缓存一致; reconciler 防覆盖分支实测跳过写入并 WARN; `okr_status.py` graduation_sample 窗口化实测输出 (Closed:0 = 打标从今起, 符合预期); exit_shadow early_tight 变体实测触发 + 反事实计算正确; pytest 9 项失败经 `git stash` 对照证实为**基线遗留** (test fixture 缺 `mfe_pct` 列 + intraday trigger mock 设置), 与本批改动无关, **零新回归**。

**本批不做 (切 LIVE 前)**: 修复 1 (broker stop-limit 冗余止损底线) + 修复 6b (移除 SIM→REAL 账户 fallback)。**延后 (P1-P2)**: 修复 4 (半日/EOD 硬编码读交易日历) / 3a (删死 AuditLogger) / 2 (删 HSI 死逻辑 + 补 SPY 急跌维度)。

### 2026-07-19 · 退出侧结构性修复: 释放赢家 + 方向全错快速脱身

**核心诊断** (数据实证, 与 7/12 handoff 一致):
- 信号≈摄硬币偏正 (50.8% 持到收盘命中, +0.37%/笔) → DA 不是问题。
- **漏血在退出**: SL_TRAIL_LOCK 15笔 100%WR 但 avg_win=$27.59 (MFE 1.48% 仅捕获一半) vs EOD 赢家 avg=$96.74。
- **根因**: `drawdown_from_peak_pct=0.4` 在日内噪声底附近触发, 系统性截断赢家 → avg_win 上不去 → R:R 被压。
- **TIME_EXIT 11笔 0%WR**: MFE 全部<0.5% (4笔=0), 方向从未兑现, 但等 4-6h 才触发 TIME_EXIT。

**修复 A: `drawdown_from_peak_pct` 0.4 → 0.8** (`config/playbook_horizon.yaml` L49)
- 通俗解释: 以前浮盈从峰值回撤 0.4% 就锁利平仓, 但美股日内波动随便就 0.5-1%, 0.4% 等于“刚赚一点就被噪声晃出来”。现在给 0.8% 呼吸空间, 让赢家跑完趋势再锁。
- 专业解释: trailing stop 的 drawdown trigger 从 40bp 放宽到 80bp, 降低 trail_lock 触发频率, 提升 avg_win, 从而抬 R:R。
- breakeven_at_pct=0.5 不动 (仍早保本, 亏不了), 只放宽锁利回撤容忍。
- 回滚: 改回 0.4。判分: 下周五 exit_scorecard R:R + avg_win 对比。

**修复 B: 早期紧SL** (`unified_config.yaml` + `pipeline.py` 规则0.5)
- 通俗解释: 以前方向做错了, 要等到亏 2% 才止损 (或者等 4-6h TIME_EXIT), 像“温水煮青蛙”。现在入场后 30 分钟内, 如果价格反向走 1% 就立即止损 — 快速认错, 小亏出场。
- 专业解释: 入场后 30min 内 breakeven 未激活 (peak_profit < 0.5%) → SL 从正常 2-2.5% 收紧到 1%. 方向对→浮盈→breakeven激活→本规则自动失效; 方向错→1%SL快速触发SL_INITIAL。
- 与 cooldown_sl_pct(4%) 互补: cooldown 防开盘瞬间秒杀(放宽); 本规则防方向全错慢性失血(收紧). cooldown 优先。
- 退出原因仍为 SL_INITIAL, 无新标签, 复用现有基建。
- 回滚: `early_tight_sl_enabled` 改 false。判分: TIME_EXIT 笔数下降 + SL_INITIAL 早期笔数/损失。

**为什么不用 15min 硬时间门 (DIRECTION_FAIL)?**
- 15min 太短, 无法区分“方向错”和“慢热型” (EOD 赢家前 15min 也可能浮亏)。
- 硬时间门 = 另一种“砍赢家”, 与 handoff 诊断方向相反。
- 早期紧SL 更智能: 用价格行为 (1% 反向) 而非时间来判断, 且方向对时自动失效。

**验证**: py_compile ✓; PlaybookConfig 加载确认 drawdown=0.8 / early_tight=true/30min/1% ✓。

---

### 2026-07-19 · DEB-round3 pool→trade 链路审计修复批次 (commit `74defd97` + `5ccad370`)
- **辩论锚点**: `docs/debates/DEB-20260719-pool-to-trade-chain-audit.md` (三方收敛执行清单 v2)。P0 5 项 + P1 5 项全量落地。
- **#1 bridge 三态告警**: `opportunity_scan_{date}.json` 缺失/空/schema非法 → 飞书 HIGH (当日去重)。新增 schema fail-safe (非法视作缺失不消费半损坏文件)。覆盖"启动后文件仍缺"盲区 — `run_generate` + `auto_execute` (每5min) 双入口直读文件, 不依赖启动时一次性加载。
- **#2 失效门组** (`_check_intraday_trigger`): ①触发窗收紧 **14:00 ET** 截止 (`entry_time_cutoff_enabled`, 默认True); ②财报禁建日拒触发 (`earnings_blackout_gate_enabled`, 复用 `scheduler.earnings_risk_guard`); ③漂移>5% **指令作废** (`thesis_drift_invalidate` 写 skip_reason 白名单落库, auto_execute+trigger 双保险跳过; trigger_miss 遥测即 shadow 记录)。
- **#3-B**: SCAN 注入 `expires_at=24h`; **消费端首轮遗漏只写不读, 本轮核查发现已补** (commit `5ccad370`: auto_execute 跳过 + trigger 拒触发双保险)。
- **GHOST 根因修复**: RC1 — `playbook_service._confirm_order_filled` 入场单 order_status 短轮询≤5s, 未成交拒记账+释放抢占 (与 close 路径共用 2026-07-07 确立的真相源; 查询异常降级维持旧兜底); RC2 — `_classify_ghost_cause` 写 `metadata.ghost_cause` (double_tracked/entry_unfilled_or_external, record-only 供 2 周复审)。
- **熔断 shadow 告警版**: 当日 ≥2 笔分钟级(holding≤0.1h)亏损平仓 → 飞书 HIGH, **不阻断** (`sl_burst_alert_enabled` 默认True)。注: holding_hours 落库 round(.,1), 故用 ≤0.1 近似 <5min 口径。
- **30min-abort shadow**: `exit_shadow.ABORT_30M_VARIANTS` 全路径采集 would_abort (MFE<0.1%且浮亏) + close 反事实 `counterfactual_saved_pct` (正=abort更优)。TIME_EXIT 11笔0%WR 根因=方向未兑现, 但实单 abort 反事实 -$160 (杀 EOD 慢热赢家) → 仅 shadow。
- **#11 IC 覆盖**: `ic_tracker` 未触发指令缺 entry_price → 当日 open 代理回填, `fwd_proxy=1` 落库标记可剔除; db 迁移列已手动触发落地 (lazy 迁移周日未跑, 明日前已补齐)。
- **#6-A**: factor_exit conditions 零命中 → WARN (进程级去重, default 兜底行为不变)。
- **#12**: `scripts/gate_cost_report.py` (stdlib 只读) — 各门拦截成本 + VWAP 门 blocked/allowed `fwd_return_intraday` 终审 + 分钟级亏损统计。实测: §4 近14天 56笔 胜率39.3% 总PnL+$40.11。
- **δ.6 清洗口径入 `docs/OKR-SPEC.md`**: void 不计 + 事件日双向对称剔除 + SHADOW 本就不计; 清洗后基线 80笔/45.0%WR/+$1422.40。
- **验证**: py_compile 全过; pytest 33+28 项通过 (4 项 pre-existing 失败经 stash 对照证实与本次无关); 服务重启 PID 408034 零 error; 新符号导入全通。
- **⏳ 明日开盘待办**: ①bridge 文件外部依赖 (Hermes ET06:53 前后产出, 周五 7-17 已正常; 若缺失 #1 告警兜底+TechnicalFallback 降级); ②#2 漂移门/熔断/bridge 告警首日上线, 关注飞书 HIGH 是否误报; ③2 周后跑 `gate_cost_report.py` + abort_30m/ghost_cause 样本复审。
- **P2 本批不做** (数据驱动后再议): #8 入场带 / #7 UE调参+DA提量。**不做**: #5/#9/#10/#6-B、30min-abort 实单版、实单熔断。

### 2026-07-12 · LLM edge 判分 (n=211) + P1 影子判决埋点 (零交易逻辑)
- **缘起**: 另一 Agent 主张"接线 LLM 擅长的质疑/环境判断"(7 项 37 行)。核心方向对 (LLM 做减法/veto, 不做加法/predict, 与北极星一致), 但它引用的支撑数字**统计无效**: conf<40 胜率 0%(n=8)、6/16 幻觉(单日 anecdote)、47% 方向(n 未知)。
- **想直接回测"挑战者质疑→盈亏" — 查完数据: 不成立**。`data/debate_verdicts/` **空**; `debates/` 是 5 月因子分箱非逐笔判决; `model_votes.model_role` **88% UNKNOWN**(无 CHALLENGER 角色); `trade_outcomes` 干净 join **n=13**; `llm_calls` 只有 hash 无正文。**当年数据根本没留**, 跑出来只能是编的。
- **唯一真实可用样本 = fuzzy (symbol, date) join `decisions`↔`trade_records` = n=211** (相关非因果)。落 **`scripts/llm_edge_scorecard.py`** → `data/llm_edge_scorecard.json` (可复现)。结果: **conviction/共识 与盈亏负相关 (r≈−0.21)** —— LLM 越自信/越一致, 结果越差。独立佐证 conf 拍平 + "LLM 不会预测"。⚠️ 置信边界: 模糊 join、时间混淆(早期 regime=None 且更赚)、混合 source_type → **强线索非因果**。
- **P1 埋点 (先造能测的数据, 再让数据决定接不接 SKIP-gate)**: DA 挑战者/校验产物**已持久化在 `data/daily_battle_plan_us.json`** (`targets[].verdict` 总判决 / `data_cited[]` 论据含 `[Futu]` 溯源标 / `factor_validation` 逐因子 `FALSE_POSITIVE` 伪信号标 / `catalyst.source_count`)。新增 **`scripts/shadow_verdict_recorder.py`** 逐日抽取 → **`data/shadow_verdicts.db`** (按 (plan_date,symbol) UPSERT 幂等)。字段: evidence_sourced/unsourced、factor_confirmed/weak/false_positive、catalyst_* 等。
- **关键工程判断**: 原计划要 `decision_id` 穿 `signal_pipeline` 做 clean trace_id 链 —— 那是**高风险治理路径**, **不做**。改用**与 scorecard 同款 fuzzy (symbol,date) join** (已验证 n=211 可行) → **零 pipeline 改动、零交易逻辑、零服务重启**。埋点仅读 JSON 产物 + 写独立库。
- **调度**: crontab 加 `30 21 * * 1-5` (服务器 CST 21:30 = ET 09:30, DA 计划生成后) 逐个交易日捕获。**回滚**: `crontab -e` 删该行即可; 删 `scripts/shadow_verdict_recorder.py` + `data/shadow_verdicts.db` 彻底清除。
- **首跑**: 落 5 条 (plan_date=07-11), 5/5 含 FALSE_POSITIVE 因子 + 未溯源论据; join 盈亏 0 行 (当日未平)。
- **⏳ 待办 (3-4 周后)**: `python scripts/shadow_verdict_recorder.py --report` 攒够 ≥30 闭环后, 比"被标记(伪信号/未溯源) vs 未标记"的期望值差 → **差显著为负才接 SKIP-gate** (阈值用该回测校准); 无差异则连质疑都无接线价值。严防像退出砍赢家那样在入场端 veto 掉赢家。

### 2026-07-12 · 交易页 Phase B+C (sim/live 隔离 + 轻量 UX, 不改交易逻辑)
- **调查结论**: 交易页数据源已基本正确 —— 实时路径 `DataService._get_env_ctx()` 因 `_is_live()==True` 恒查**真实账户** (TrdEnv.REAL/REAL_ACCOUNT_ID); 交易记录/决策质量 `slow-data` 早已从本页移除 (`trade.js` L1467)。**唯一 sim 泄漏 = DB 兜底**。
- **Phase B (后端, sim/live 隔离)**:
  - `data_service.py` `_fetch_positions_from_db`: SQL 加 `AND LOWER(COALESCE(account_type,'')) != 'simulate'` (排除模拟仓, 保留 real+NULL); SELECT 补 `account_type,owner` 写入 dict。
  - `dashboard.py` `_parse_positions`: 防御性 `continue` 跳过 `account_type=='simulate'`。
  - 根因: `open_positions` 表混存 sim+live, 券商宕机走 DB 兜底时会把**模拟仓当实盘仓**渲染 (实测当时表内唯一仓即 `account_type=simulate/owner_strategy=playbook_sim`)。
- **Phase C (前端, 轻量)**: `trade.html` `.trade-summary` 资金头条加 `position:sticky;top:0;z-index:50` → 滚动持仓/记录时资产/购买力/盈亏/新鲜度圆点常驻可见。
- 验证: py_compile ✓ / SQL 谓词自检 (sim+大写SIM 排除, real+NULL 保留) ✓ / `_parse_positions` 防御跳过 ✓ / 重启 dashboard (PID 355297) 无 error / 兜底路径实测**返回 0 真实仓** (原会返回 1 条 sim)。
- **回滚**: 删 SQL 谓词 + `_parse_positions` continue + sticky CSS 即恢复, 零交易逻辑。

### 2026-07-12 · 交易页 Phase A (数据诚实化, 不改交易逻辑)
- **背景**: web dashboard 交易页 (`/trade`) 是**手动真实实盘**界面 (非模拟盘, `manual_order_service._is_live()` 恒 True → `TrdEnv.REAL`), 要逐步替代富途 app。目标: ①数据准确无虚构 ②信噪比更好。
- **Phase A = 纯诚实化, 零交易逻辑/下单/sizing 改动, 可回滚**。核心: 券商实时不可用时**绝不把 $0/成本价伪装成真实盈亏**。
- 后端 (3 文件):
  - `data_service.py` `_fetch_positions_from_db`: DB 降级持仓打 `data_source="db_fallback"`。
  - `dashboard.py` `_parse_positions`: 透传 `price_basis`(live/cost/delayed/none) + `stale`; `_parse_account` 打 `fx_estimated`(7.8 汇率兜底路径为 True); `_calc_pnl` 加 `positions_stale` 聚合标记。
  - `trade.py` snapshot: 加 `stale` + `as_of` 快照级新鲜度。
- 前端 (`trade.js`+`trade.html`):
  - 持仓 stale → 浮盈/今日盈亏显示 `--` (不再是伪造 $0); 现价加时段徽标 (成本/延迟)。
  - 顶部持仓盈亏/今日盈亏: 降级时显示 `--`(黄)。购买力估算加 `≈` 标记。
  - 新鲜度圆点三态: **绿=实时券商 / 黄=降级·成本价 / 灰=离线** + 文字提示。
- 验证: py_compile ✓ / `node --check` ✓ / 逻辑自检 ✓ (fx_estimated 三态正确; db_fallback→stale/cost, Futu行内价→delayed)。
- **待办**: ①**重启 `ftseek-dashboard.service`** 才能生效后端改动 (前端需浏览器硬刷新)。②Phase B(sim/live 记录隔离)/Phase C(信噪比 UX) 尚未做。

### 2026-07-12
- **拍平 conf_factor** (数据支持, 可回滚): 新增 config `playbook.risk_parameters.confidence_sizing_enabled` (默认 **false** → conf_factor 恒 1.0)。改动仅 `pipeline.py` L2338-2345 reprice 块 (DA 路径); DEC 路径 `instruction_generator.py` L393 本就不 conf 加权。验证: flag 经 `get_playbook_config()` 实测=False。**开回来**: yaml 改 true。
- **新增 `scripts/exit_scorecard.py`**: 退出纪律周度判分器 (混淆矩阵 + expectancy + R:R + 杀赢家数)。只读。
- **OKR 换轨**: `OKR-2026-07.md` 结算为 MISSED (根因=退出漏血, 非预测); 新建 `OKR-2026-08.md` 以 expectancy 为核心 KR, 删除"加仓追收益"危险对策。
- **日内 IC 上线** (前序): `fwd_return_intraday` 列已迁移到线上库 `data/ftseek_us.db` (已备份 `.bak.20260712_intradayic`)。近30天覆盖 175/179。IC: intraday −0.14 / t1 −0.04 / t3 −0.09 / t5 −0.15 (污染窗口, 非定论)。
- **待办/未做**: 退出旋钮 A 的实际修改留到下周五循环; `US.US.` 双前缀脏 symbol 2 条 (id 245/323, 陈旧 DEC 行, 不影响交易) 暂不清洗, 自然 age out。
- **开盘前就绪检查 + 重启 (✅ 2026-07-12 11:24)**: 全链路体检通过 (模块导入/配置开关/DB schema+迁移幂等/sizing 模拟/服务/循环存活)。
  已**重启 `ftseek-us.service`** (新 PID 345299) 激活 conf 拍平 —— 冒烟检查全绿: 无 error、OpenD 连通、
  安全确认**模拟盘** (TRADE_ENV=1)、Futu 交易上下文连接、调度器配置加载。
  **⚠️ 周一盘前须确认**: DA 的 bridge 文件由 deep_analysis cron 在 **ET 09:00** 生成; 若缺失, 当天降级
  `TechnicalFallbackAdapter` (技术兜底), **不走 DA 信号** → 整条 DA-FTSeek 链路当天不生效。
- **源码锚点提交 (✅ 2026-07-12)**: `9c5cb2cc`(133源码/文档) + `3fb5d6c1`(补齐 2 源码)。含本会话全部改动 + 此前累积。
  **未入库(有意)**: `data/*.db`·`app/data/*.db`(live 数据) / `logs/*` / 一堆未跟踪 junk。
  **⚠️ `config/unified_config.yaml` 卡治理未提交**: 含 `entry_reanchor_enabled:true`·`trailing_enabled:true` 等
  `enabled→true` 项, pre-commit hook (SYSTEM_OBJECTIVE §5) 要求 **ADR/RFC 人审批**才能入库。
  这些值**已在运行时生效** (磁盘配置已加载), 只是没进 git 历史。要入库须先补 ADR/RFC 或经你确认 `--no-verify`。
  (提交时两个 governance hook 均触发: pre-commit 拦 yaml enabled→true; commit-msg 要求高风险改动带 `[ADR]/[RFC]/[hotfix]` 标签。)

### (更早) 2026-07-11 及之前 — 见 `docs/WEEKLY-SUMMARY-2026-07-11.md`
- 决策链路双脑清理、退出引擎批次1、校准闭环、dashboard 可视化、_TRACKING_FIELDS 审计断裂修复等。

---

## ⚠️ 长期 caveat (读之前先知道)
- **样本量**: ~49 笔/月, 干净窗口 (date≥07-11) 目前近 0 样本。多数结论 n<50, 要几周 go-forward 才显著。别把污染窗口的数字当定论。
- **模拟盘宽恕一切**: sim 不建模真实滑点/流动性; 即使 sim 正期望也未必活到实盘。
- **DA 是 repo 外的 Hermes skill**: 若信号在干净窗口被证伪 (期望仍<0), 修复点在 skill, 不在本 repo (见 OKR-2026-08 硬退出触发器 2026-08-10)。
- **纪律**: hard-to-reverse/线上写操作先备份+确认; 不为凑样本放松风控; 一次一个变量。
