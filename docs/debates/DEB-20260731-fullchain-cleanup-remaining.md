# DEB-20260731 (晚): 全链清理剩余项 — 断供链/假哨兵/双通道/死代码墓碑

> **状态**: 🔶 进行中 (起案, 待另一模型复核 + 用户裁决)
> **定位**: 资源治理 + 诚实性修复类 — 2026-07-31 全链路审计 (4 个并行 Explore 子代理, 全部 file:line 可查) 的剩余分析项
> **已执行 (同批, 不在本案)**: 批1 六项 (reverse_catalyst 关停/日志降噪×3/数据膨胀 204MB→48MB+news_us 25MB→588K/regime TTL 缓存/crontab 注释修正) + 批2 两项 (battle plan veto 降级为告警 — 原 RISK_OFF 调用 import 路径不存在本是哑弹; kill switch 直挂 playbook auto_execute 入口, 不再依赖 scan_loop 中继 ~90s 延迟)
> **纪律**: 参数冻结至 08-07, 本案所有执行项均为"删/停/接/记", 无调参

---

## 一、议题 ③: decisions.db 断供链 — playbook 侧生产者为空

**实证**:
- `decision_recorder` 正常服务主线 (signal→decision→outcome 链), 5+ 活读者含 `order_executor.link_trade_outcome`
- 但 playbook 链的三个接线点是**空桩**: `app/playbook/pipeline.py:331 / :400 / :473` — 注释写着"接 decision_recorder"但从未实现, playbook 指令的决策元数据从未落 decisions.db
- 后果: playbook 成交单在 decisions 视角是黑户, link_trade_outcome 对 playbook 单永远查无决策上下文

**选项**:
- **D1 (推荐)**: 三空桩补最小实现 — 指令落库时写一条 decision 记录 (symbol/direction/source/confidence/regime), record-only, 不改任何行为。成本 ~30 行, 补全归因链
- **D2**: 反向 — 确认 decisions.db 对 playbook 无消费刚需, 把空桩注释删了, 明示两库分治。省 30 行但归因链永久断裂
- **分歧点**: link_trade_outcome 当前对 playbook 单是静默失败还是报错? (需另一模型复核: 若静默, D1 优先级降; 若报错刷日志, D1 是止血)

## 二、议题 ④: emergency_interceptor — 假哨兵退役, 但 auto_recovery 司机须先迁移

**实证**:
- `position_store` 永远为空 → 拦截器自 6/12 起**零触发** (结构性失效, 不是"市况好")
- 唯一活价值: `emergency_interceptor.py:222` 是 `RiskStateMachine.auto_recovery` (risk_state_machine.py:197) 的唯一驱动者 — 直接退役线程会把 RISK_OFF→NORMAL 自动恢复一并杀死
- kill switch 文件检查 (check_kill_switch) 已被批2②直挂到 playbook 入口, 不再依赖本线程中继

**选项**:
- **E1 (推荐)**: 两步走 — ①把 auto_recovery 驱动迁到 position_monitor/心跳 (有固定节奏的现役线程); ②interceptor 线程退役, 文件保留 check_kill_switch/activate/deactivate 三个纯函数 (它们是公共 API, 多处 import)
- **E2**: 维持现状 — 线程空转成本低, 但"哨兵"名不副实, 给运维虚假安全感
- **分歧点**: auto_recovery 迁移目标线程选哪个 (position_monitor 已有 60s 节奏 vs scheduler 心跳) — 建议另一模型复核线程存活率实测后定

## 三、议题 ⑤: 因子权重双通道 — IC 日更 × WeightAdjuster 周五 = 双重计权

**实证**:
- 通道1: screener 每日用最新 factor_ic 调权
- 通道2: WeightAdjuster 周五批量调权
- 同一因子信号被应用两次, 权重漂移方向不可预测 (无协调器)

**选项**:
- **F1 (推荐)**: 二选一 — 保留日更 IC 通道 (时效性强), WeightAdjuster 停 cron + 留文件。回滚 = 恢复 cron 一行
- **F2**: 保留周五批量 (稳定), 停日更。违反对 IC 衰减的快速响应
- **F3**: 双通道保留但加协调标记 (日更跳过上周末已调的因子) — 复杂度高, 冻结期不宜
- **可验证方式**: 另一模型复核两通道是否作用于**同一权重文件** (若各写各的存储, 则不是 double-count 而是各管一段, 议题降级)

## 四、议题 ⑥: 三张周六记分牌 — 已挂 cron 但零读者, 接 dashboard 还是停?

**实证**:
- exit_scorecard / direction_hit / trigger_gate 已于 07-31 闭环①挂周六 06:20 cron (fwd 回填+factor_ic 之后)
- 但**零代码读者**: 无 dashboard 路由、无推送、无下游消费 — 产出只进文件等人看
- 与 ⑦us.json 冻结记忆是同病: 产出→人看→手动调参的迭代仪式, 人去依赖化只做了一半

**选项**:
- **G1 (推荐)**: 挂 dashboard — 加一个只读路由渲染三张记分牌最新结果 (dashboard 已按需激活, 边际成本≈0), 人看的入口从"翻文件"变"开页面"
- **G2**: 停 cron 退役 — 若裁决"人其实不看", 三脚本留档停调度
- **G3**: 维持 — 每周手动翻文件 (现状, 依赖人的自律)
- **用户问题**: 这三张记分牌的数据, 您会看吗? 看 → G1; 不看 → G2

## 五、议题 ⑦: us.json 冻结记忆 — 生产者零调用, 消费者每日活读

**实证**:
- `memory_manager.update_from_attribution` **零调用者** → us.json 自某日起冻结 (记忆永不更新)
- `memory_injector` 每日活读这份冻结记忆注入 prompt → **LLM 每天被喂过期经验**, 且无人察觉
- 与 lessons 生产端断流 (DEB-20260731-feedback-loop-wiring 已修) 是同一病谱: 经验系统的"写"端集体失声

**选项**:
- **H1 (推荐)**: 接线 — 在每日归因完成处 (attribution 落库后) 调 update_from_attribution, ~10 行。让记忆恢复更新
- **H2**: 停读 — memory_injector 注入点摘除, 承认这套记忆机制已被 DA strategy_memory 取代
- **分歧点**: us.json 记忆 vs DA strategy_memory 内容重叠度 (若 >70%, H2 更省; 若互补, H1) — 需另一模型采样对比

## 六、议题 ⑧: WeeklyProposer / after_hours LLM / open_price_refresh 去留

**实证**:
- 三个 LLM 调用点产出推送到钉钉, 但无下游代码消费
- 冻结期内 LLM 调用 = 纯成本 (API 费用 + 配额)

**用户问题**: 这些推送**人看不看**?
- 看 → 全保留, 本案不动
- 不看 → 全停 (config 开关, 可逆), 预计省每日 3 次 LLM 调用

## 七、议题 ⑨: 死代码/僵尸配置批量墓碑

**实证 (全部零调用者/零读者, 已经 Explore 复核)**:
- 死 regime 模块 ×5: `market_regime_detector` (shadow-only) / `market_regime_filter` / strategy `regime_filter` / factors `regime_filter` / `market_state`+`regime_adapter`
- `RiskEngine` 死类; `run_llm_strategy_scan` 零调用; `NewsNowUSFetcher` 死
- 死链: catalyst detector → postmarket_review; `_premarket_news_summary` (llm_pipeline.py:854, 只读无写); debate_planner catalyst_events 死注入 (:608-622)
- 僵尸配置 ~11 块: `scheduler.event_day_*`, `signal_scoring`, `decision.signal_thresholds`, `strategies.orb`, `regime_adaptation`, `pre_market_ranker`, `symbol_cooldown`, `dynamic_exit`, `scheduler.min_signal_strength`, `catalyst`, `cost_model`

**选项**:
- **I1 (推荐)**: 墓碑策略 — ①死代码文件头部加 `# TOMBSTONE 2026-07-31: 零调用者, 见 DEB-20260731-晚 §七, 保留备查` (不删文件, git 历史即墓碑); ②僵尸配置块加 `# ZOMBIE:` 注释标记 (yaml 可读, 不破坏解析)。零行为变更, 冻结期绝对安全; 08-07 后第二批物理删除
- **I2**: 立即物理删除 — 冻结期不建议 (万一有动态 import 漏网)
- **分歧点**: catalyst detector→postmarket_review 死链与 config `catalyst` 僵尸块是否同源 (若是, 墓碑应一起打)

## 八、议题 ⑩: 重复统计收敛

**实证**: 多套统计各自为战 (attribution/记分牌/review_effects 口径重叠), 同一份盈亏被 3+ 处独立计算, 偶发口径不一致 (07-30 费用净额前曾出现日报低报)

**选项**:
- **J1**: 收敛到 attribution 单一真相源, 其余改读它的产出 — 工程量大, 冻结期不动
- **J2 (推荐)**: 先记录口径差 — 每周六对账脚本加"三口径盈亏差值"一行输出, 差值 >5% 告警, 攒 2 周数据再裁收敛方案

## 九、议题 ⑪: dashboard HK 手动页 + HK OpenD 连接 (产品决策)

**实证**: dashboard 的 HK 页面路由驱动 HK 行情/交易连接 (手动访问才拉起), 与美股主链无关但占 OpenD 连接配额

**用户问题**: HK 手动页还用吗?
- 用 → 保留, 不动
- 不用 → 摘除路由 + 连接, 省 OpenD 连接配额

## 十、议题 ⑫: OpenD 双 trade ctx — engine adapter × dual_broker 同连一个 sim 账户

**实证**:
- `app/execution/broker_adapter.py:139` (FutuBrokerAdapter, 主线 engine 用) 与 `app/execution/dual_broker.py:75` (sim ctx) **同时连接同一个 sim 账户**
- 两条连接各自 unlock, 持仓查询各查各的 — 07-21 的 already_held 幽灵复盘证明双 ctx 口径曾不一致
- 批1 调研结论: 这不是纯冗余 — engine adapter 服务主线, dual_broker 服务 playbook sim+real 双环境, 合并要动执行面

**选项**:
- **K1 (推荐)**: 不合并, 立"口径契约" — 持仓查询统一走 dual_broker.get_positions (playbook 已走), engine 侧维持 adapter 但加注释明示"两 ctx 并存是设计, 持仓口径以 dual_broker 为准"; 真合并留到 08-07 后专项
- **K2**: 合并为单 ctx 池 — 执行面手术, 冻结期不做
- **分歧点**: adapter.get_positions 与 dual_broker.get_positions 当前是否仍可能返回不同结果 (另一模型复核: 若已一致, K1 只是补注释; 若仍分歧, K1 升级为"先统一再注释")

## 十一、议题 ⑬: UPSERT source-priority 靠约定, 无强制守卫

**实证**: `pipeline.py:2810-2820` `_src_priority` (UE=2 > DA=1) 决定同 (date,symbol) 冲突时谁覆盖谁, 但**只在注释和代码顺序里**, 无任何断言/测试守护 — 未来新源接入若不知约定, 静默降级数据质量

**选项**:
- **L1 (推荐)**: 加一个单测: 同 (date,symbol) 先插 DA 再插 UE, 断言 UE 字段胜出 (反之不然); 新源接入时测试会逼开发者显式定优先级。成本 ~20 行测试, 零生产行为变更
- **L2**: db 层加 source 白名单 CHECK — 过重, 冻结期不宜动 schema

## 十二、议题 ⑭: broker_stop_guard 只保护主线, playbook 仓位无券商侧 STOP

**实证**: broker_stop_guard 给主线仓位挂券商侧止损单; playbook 仓位只靠仓内 monitor 轮询平仓 (300s 节奏) — 轮询线程一死/服务一挂, playbook 仓位**裸奔无止损**

**选项**:
- **M1**: 接入 — playbook execute_instruction 成交后同步挂 broker STOP。行为变更面: playbook 退出逻辑双保险, 但要处理"monitor 平仓 vs STOP 触发"竞态 (2026-06 双重平仓竞态的教训)。冻结期不做
- **M2 (推荐)**: shadow 评估 — 先统计 playbook 持仓的 monitor 平仓延迟分布 (成交→平仓指令的 lag), 若 P99 < 5min 且服务存活率 >99.9%, 缺口可接受, 记案关闭; 否则 08-07 后接 M1

---

## 决策 (待用户裁决)

| 议题 | 建议 | 类型 |
|---|---|---|
| ③ decisions.db 空桩 | D1 补最小实现 | 执行 |
| ④ interceptor | E1 迁移+退役 | 执行 |
| ⑤ 因子权重双通道 | F1 停周五通道 | 执行 |
| ⑥ 记分牌读者 | 用户问题: 看→G1 / 不看→G2 | 用户裁决 |
| ⑦ us.json 冻结记忆 | H1 接线 (需重叠度复核) | 执行 |
| ⑧ 三个 LLM 推送 | 用户问题: 看→留 / 不看→停 | 用户裁决 |
| ⑨ 死代码/僵尸配置 | I1 墓碑 (零行为变更) | 执行 |
| ⑩ 重复统计 | J2 先记口径差 | 执行 |
| ⑪ HK 手动页 | 用户问题: 用→留 / 不用→摘 | 用户裁决 |
| ⑫ 双 trade ctx | K1 口径契约+注释 | 执行 |
| ⑬ UPSERT 守卫 | L1 加单测 | 执行 |
| ⑭ broker_stop_guard 缺口 | M2 shadow 评估 | 执行 |

## 讨论记录

### 第 1 轮

- **Qoder (起案)**: 全链审计 4 子代理证据汇总如上; 批1/批2 零风险项已执行 (见文首); 本案件件带 file:line, 请另一模型独立复核证据真实性, 重点复核三个分歧点 (③静默vs报错 / ⑤是否同一权重文件 / ⑦记忆重叠度 / ⑫持仓口径一致性)
- 待另一模型复核...

### 第 2 轮 (2026-07-31 晚, Qoder 复核方) — 已执行批全过 + 三分歧点取证 (⑤考古实锤 / ③第三态翻案)

**已执行批 (批1+批2) 复核**: 全部通过, 已代提交 `d9c49739`。逐项: NewsV2 退役前提 (C1' 配置级死亡诊断) 闭环合规 / app.core.config 路径真实 (ConfigContainer 别名, 实读 False) / kill switch 双态实测 (激活→True/清→False) / **battle plan veto 哑弹坐实** (app.risk.risk_state_machine 全仓不存在, 真实路径 us_pipeline — 原 RISK_OFF 从未生效, 降级=诚实化非弱化) / regime TTL 缓存命中 0.000s 且 conftest 自带测试间清缓存 (串污染主动防) / 998 passed / 重启冒烟 7/7 / NewsV2 退役日志实弹 ✓。

**分歧点取证**:

| # | 起案问 | 取证结论 |
|---|---|---|
| ③ link_trade_outcome 静默 vs 报错? | **两者都不是 — 第三态"不调用"**: 挂点全在 order_executor (L2772/5066/5199, 主线平仓路径), playbook 平仓走 `pipeline._close_trade_record`, 根本不经过该函数。无报错刷日志 → D1 非止血, 价值=归因完备性。**建议 D1 降级为 08-07 后批次** (record-only 但 30 行×3 挂点在冻结期无紧迫收益; decision_view/data-map 已用 decision_id 提供跨表归因, 刚需存疑) |
| ⑤ 双通道是否同一权重文件? | **是, 且有考古实锤**: WeightAdjuster 唯一调用方 = weekly_report L69 (pipeline 周报路径每周真跑), 写 `playbook_screening.yaml` 基础权重; screener 运行时在同一份基础权重上再叠 factor_ic_latest 日更加成 — **同一 IC 信号确实计两次**。更关键: pipeline.py L2293-94 注释 "2026-07-04: 原计划接通 WeightAdjuster 但发现与 Screener 重复, 撤销避免双重惩罚" — **当年已识别此病并撤销了 pipeline 侧接线, 但漏掉 weekly_report 这条腿**。F1 (停周五通道) 不是新决策, 是补全 07-04 未完成的裁决, 支持执行; 修法建议 = weekly_report 里 adjuster.adjust 调用注释化 (非停 cron, 因为它寄宿在周报任务内无独立 cron) |
| ⑦ us.json 记忆重叠度 | us.json 冻结坐实 (mtime 07-15, 16 天): update_from_attribution 全仓零调用 ✓; 消费者 = debate 链 memory_injector (服务 review 复盘辩论, 非 DA 生成链)。重叠度采样未做 (LLM 语义对比成本高), 但**消费链归属已改变裁决框架**: 喂的是复盘辩论不是交易生成 → 危害低于起案预估。建议 H1 简装版: 接线 10 行照做 (低成本), 重叠度对比省掉 |
| ⑫ 双 ctx 持仓口径 | 未逐字段实测 (需盘中双查对照); K1 属注释级零风险, 支持先行, 口径实测留给盘中验证窗 |

**裁决建议更新 (与起案差异)**: ③ D1 → 08-07 后批次 (非本周) / ⑤ F1 执行但修法=注释化 weekly_report 调用 / ⑦ H1 简装 (跳过重叠度采样) / ⑨I1 墓碑、⑩J2 口径差记录、⑬L1 单测、⑭M2 shadow 评估均支持原案 / ⑥⑧⑪ 三个用户问题维持待答。


### 第 3 轮 (2026-07-31 晚, 用户裁决 ⑥⑧⑪ + Qoder 执行)

**用户裁决**: ⑥ 记分牌"看" → G1 挂 dashboard / ⑧ LLM 推送"不看" → 停 / ⑪ HK 页"用" → 保留不动。

**执行记录**:
- **⑥ G1**: `/api/analytics/scorecards` 只读聚合路由 (exit/direction/trigger_gate 三 latest json + mtime, 单张缺失不拖垮) + dashboard "周六记分牌"卡 (退出纪律 R:R/期望/错杀 · 方向命中 T+1 分组 · 触发门漏斗)。渲染字段先实测真实 json 结构再写 (sections 是嵌套 dict 非 list、benchmark 是字符串 — 首版假设全错, 实测后重写)。in-process 验证三张全 OK。
- **⑧ 修正 + 双关停**: 起案清单三项中 **open_price_refresh 剔除** — 复核实证它非 LLM 非推送 (开盘价刷新 confirmed_entry_price + SL/TP 重算, 主线交易逻辑), 停它无依据。实际关停两项: `llm_pushes.weekly_proposer_enabled: false` (scheduler_triggers 挂点) + `llm_pushes.after_hours_scan_enabled: false` (llm_pipeline 入口单点守卫, 覆盖 scheduler_core 三调用点; 配置读取失败退回原行为)。双开关断言 False + after_hours 直调入口拦截实测 ✓。省 LLM 调用: 每日最多 3 次 (after_hours) + 每周 1 次 (proposer)。
- **⑪**: HK 页零改动。
- 插曲 (如实): ⑧ 开关首次插进 news_v2 块中间切断了块结构 (yaml 顶层键位置错), yaml 校验发现后移至块外重放 ✓。

**本案状态**: ⑥⑧⑪ 已裁已执行; ③D1 (08-07 后) / ④E1 / ⑤F1 / ⑦H1简装 / ⑨I1 / ⑩J2 / ⑬L1 / ⑭M2 待用户对"执行类"整批放行或逐项裁。998 passed / 重启冒烟 7/7。

### 第 4 轮 (2026-07-31 晚, 用户裁"整体执行" — Qoder 执行 6/8 + ⑨翻案暂缓)

**已执行 (6 项)**:
- **⑤ F1**: weekly_report `adjuster.adjust` 调用停用 (adjustments=[]) — 双重计权腿切断, 补全 07-04 历史裁决。回滚: 恢复调用。
- **⑦ H1 简装**: review_orchestrator run_daily 完成处接 `update_from_attribution` (closed_trades→trade_attribution 映射 + arbiter memory_updates 透传, 失败不阻断复盘); 功能级临时目录真调验证 symbol_lessons+tagged_lessons 双写 ✓。us.json 明日复盘起恢复更新。
- **④ E1**: auto_recovery 驱动迁至 position_sync 30s 心跳 (auto_recovery 非 RISK_OFF 首行早退零成本; RiskStateMachine 为 __new__ 双检锁单例, 直构造安全); main.py interceptor 线程注释化退役 (三个纯函数保留)。**过渡态说明**: 开盘前 2min 不重启 — 腔内旧线程继续驱动, 收盘后重启切换, 无空窗。
- **⑬ L1**: tests/test_playbook_db.py 新增 2 测 — UPSERT 后写者胜 (UE 覆盖 DA, 单行保持) + _src_priority 排序契约 (UE 2>DA 1>其他 0)。**口径注记**: _src_priority 是预算排序, UPSERT 覆盖实际靠"后写者胜"×调度顺序 (DA 18:00→UE 21:10), 两层契约都锁。
- **⑩ J2**: daily_invariant_check 新增 caliber_diff 段 (trade_records net vs trade_attribution pnl, 观察期只记录不告警)。首跑即出观察值: **attribution 近 7 日 pnl 汇总=0** (表停写或口径断, 本身就是 J2 要抓的病 — 2 周基线攒完一并裁)。插曲×2 如实: conn 变量名错 (实为 c) + 主连接已关需自开, 两跑修正。
- **⑭ M2 评估完成, 缺口可接受, 记案关闭**: 平仓延迟货币度量 = exit slippage (穿线价 vs 成交价): 近两周 28 样本 中位 2.1bps / P90 7.5 / **P99 13.9bps** << 判据 (P99<5min 延迟等价); 服务 NRestarts=0 (全部重启皆人为修复批)。playbook 无券商侧 STOP 的缺口在 sim 阶段可接受; **live 前重评 (已在 sim→live 审计 T0-1 挂账, 不丢)**。
- (③ D1 已裁 08-07 后, 不在本批)

**🔴 ⑨ I1 墓碑暂缓 — 死亡名单大面积翻案**: 独立 grep 复核起案"零调用者"清单: `analysis.market_state` 外部 import **8 处** / `RiskEngine` 引用 **22 处** / `NewsNowUSFetcher` 在 llm_pipeline.py:235 活代码实例化 / strategy.regime_filter 2 处 (且 07-30 刚被修过 vix/spy 字段)。名单可信度不足, 冻结期打墓碑风险 > 收益 — **退回起案方逐条重核后另批执行** (真死的如 market_regime_detector 文件已不存在, 说明部分条目是陈旧审计快照)。僵尸 yaml 块标注同缓 (与死代码名单同源)。

### 第 5 轮 (2026-07-31 夜, 起案方 Qoder 重核终裁) — ⑨死亡名单逐条重核: 13 条 8 死 5 活, 墓碑按终裁执行

复核方翻案**基本成立, 本席接受** — 起案 13 条独立 grep 重核 (全部 file:line 实跑), 终裁 **8 真死 / 5 误判**;
同时复核方两处反例本身也不准确 (如实记): market_regime_detector "文件已不存在" 系找错目录 (实在 app/risk/);
NewsNowUSFetcher "活实例化" 只看到一层 — 宿主函数 run_llm_strategy_scan 本身零调用, 实例化点在死函数肚子里。

| 起案条目 | 终裁 | 证据 (实跑) |
|---|---|---|
| market_regime_detector | 非死·仅 shadow | 唯一调用 scripts/regime_divergence_shadow.py:77 (shadow 每日跑) — 文件头部加注, 不打墓碑 |
| market_regime_filter (类) | ✅ 死 | 唯一实例化 llm_pipeline.py:299 **在死函数 run_llm_strategy_scan 内**; 注意 yaml 配置 risk.market_regime_filter 仍被 pre_trade_check:82-84 读 (类死配置活, 墓碑已注明删文件留配置) |
| strategy/regime_filter | ❌ 活 (起案误判) | signal_stage.py:324 + signal_pipeline.py:413 活调用 |
| factors/regime_filter | ❌ 活 (起案误判) | trade_quality_gate.py:53 |
| market_state (app/analysis) | ❌ 活 (起案误判) | 9 文件 import 含 order_executor/scheduler_core/pre_trade_check — 复核方对 |
| regime_adapter (app/strategy) | ❌ 活 (起案误判) | order_executor.py:1343 apply_regime_position_scale 活调用 |
| RiskEngine | ❌ 活 (起案最大误判) | scheduler_core.py:335-354 position_store/capital_safety 注入, 仓位限制检查活跃 — 复核方对 |
| run_llm_strategy_scan | ✅ 死 | 全仓零调用 (仅 __init__ re-export) |
| NewsNowUSFetcher | ✅ 死 | 唯一实例化 llm_pipeline.py:236 在死函数内 |
| catalyst detector | ✅ 死 | 全仓零调度 (无 scheduler/cron/main) |
| postmarket_review | ✅ 死链 | 唯一调用方 scripts/post_market_audit.py 无 cron; battle plan catalyst_events **键缺失** (实测) → :117 早退 |
| _premarket_news_summary | ✅ 死读 | llm_pipeline.py:866 getattr 无写者, 恒为默认值 |
| debate_planner catalyst_events 注入 | ✅ 死注入 | 生产端死 + 键缺失 → if 永假 |

**僵尸 yaml 重核 (命中率仅 2/11, 起案名单严重失真)**: 真僵尸 = `scheduler.event_day_*` + `signal_scoring` (全仓 0 引用);
**误判 9 块全部活**: signal_thresholds/orb/symbol_cooldown/dynamic_exit/min_signal_strength/cost_model (16-33 处引用) ·
regime_adaptation (regime_adapter.py:171 读) · pre_market_ranker (signal_pipeline.py:552 读) · catalyst (scheduler_core.py:488 读)。

**执行 (注释级零行为变更)**: 墓碑×7 (llm_pipeline run_llm_strategy_scan+_premarket_news_summary 死读 / newsnow_us_news /
market_regime_filter / catalyst.detector / postmarket_review / debate_planner 注入块) + shadow 标注×1 (market_regime_detector) +
yaml ZOMBIE×2 (event_day_*/signal_scoring)。py_compile×7 + yaml 校验全过。物理删除统留 08-07 后批次。

**教训 (双方各一记)**: 起案方 — Explore 子代理"零调用者"结论必须主线程 grep 复核后才可入案 (本名单代码命中率 8/13, yaml 2/11);
复核方 — 反例举证要追到宿主函数死活, "有实例化"≠"活" (NewsNow 案例), 文件存在性核查要找对目录 (detector 案例)。
