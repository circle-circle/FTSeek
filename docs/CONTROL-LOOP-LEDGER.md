# 控制环路台账 (Control Loop Ledger)

> 立档: 2026-08-28 · 用户裁决"按照建议执行" · 工程控制论造型落地第 1 步
> 性质: 活文档 — 新增/下线任何环路必须同步本台账, 守卫测试 `tests/test_control_loop_ledger.py` 强制 enabled cron job 入账
> 回滚: 删本文件 + 删守卫测试

---

## 0. 造型陈述 (Forming Statement)

**目标造型: 持续稳定的盈利, 经由工程化的受控改进逐步达成。**

按钱学森《工程控制论》的问题设定展开: 面对一个复杂、动态、受干扰、不确定的现实系统,
把意图持续转化为系统实现的结果。映射到本项目:

| 控制论概念 | 本项目实体 | 可控性 |
|-----------|-----------|--------|
| 扰动 w(t) | 市场 (不可控, 非平稳, 厚尾) | ✗ 不可控 |
| 作动器 u(t) | 入场/出场/仓位/敞口决策 | ✓ 唯一可控 |
| 输出 y(t) | PnL = f(扰动 × 作动) | 弱可控 |
| 给定值 | 见下"意图编译" | — |

**物理定律约束 (违背 = 振荡/发散, 均有本项目实证)**:
1. **稳定优先于精确**: 先保不死再谈跟踪。实证: 8/22 证伪"执行纪律可保底" — 机制救毛利≈0 但救不回费用 (费 = 毛利 42 倍), 稳态误差在费用线以下时任何反馈增益都救不回来。
2. **延迟 × 增益 < 稳定裕度**: 反馈延迟日/周级、样本几十笔 → 适应增益必须极小。实证: 每周改一个退出参数循环 7 周 7 版本 (每窗 46 笔), 输出与掷硬币不可区分 (DEB-20260823) → 参数冻结 (KR2) 是物理必然不是官僚。
3. **必要变异度**: 控制器状态数须覆盖扰动状态数 → regime 识别; 但 regime 不可观测时正确动作是降作动, 不是猜。

**意图编译 (核心)**: "30 天盈利"在物理上不是可跟踪的 setpoint — 作动器不耦合在日级
PnL 上, 拿 PnL 偏差当纠正信号只会引导对噪声调参。意图只能编译为两种可控形式:

- **(a) 稳定包络** (§3): 越界即改变行为的硬边界 — 敞口帽、单名帽、日亏门、四锁。
- **(b) 统计准入条件** (§4): 允许环路继续运转/毕业的条件 — n≥30、按日聚类 t+MDE、
  双窗口、预注册判据。

**盈利不是被追逐的量, 是包络内环路运转的渐近行为。** 这是"不预测, 识别"的控制论表述:
控制器只对可测状态作动, 永不对扰动的预测作动。

---

## 1. 分层环路总览

```
慢环 (周/月, 适应律层)   周六周检/周日复盘/影子毕业门/冻结核查
        │ 判据+裁决 (人工门: 用户签字)
中环 (日, 测量层)        盘前预检/盘后复盘/dc双口径/IC/影子采集
        │ 误差信号 (只观测, 不直接驱动改参)
快环 (分钟/秒, 执行层)   进程内退出监控/盘中诊断/硬止损监控
        │ 包络约束 (§3)
作动对象                  trade_records / open_positions (sim 通道)
─────────────────────────────────────────────────────────────
元环 (§2.5, 测量免疫层)  D1 载体活性 / D2 调度对账 / D3 反向配置普查 / D4 测量不变量
                          ↑ 包其余三环的"尺子本身", 只报警不动作
```

- 快环只负责"不死 + 按规则执行", 不做学习。
- 中环只负责"测得准", 输出成对口径 (pooled + 按日聚类), 不直接改参。
- 慢环负责"裁决", 遵循成文适应律 (`docs/ADAPTATION-CONTROL-LAW.md`: 触发→证据→增益上限→验证→回滚)。
- 元环不负责判断对错, 只负责"其余环路的失明必须可见" — 检测器自身异常一律产出 finding。

---

## 2. 环路明细

### 2.1 进程内快环 (ftseek-us.service, app/core/scheduler_core.py)

| # | 环路 | 间隔 | 给定值 | 传感器 | 已知失效模式 |
|---|------|------|--------|--------|-------------|
| P1 | SignalScanLoop | scan_interval_minutes=3 | 信号管线门禁阈值 (min_confidence=60 等) | 行情/新闻快照 | 无 |
| P2 | PositionMonitorLoop | monitor_interval_minutes=0.5 | SL/TP/trailing 参数 (冻结期) | 实时报价 | 静默 except 曾致止损跳过 (历史教训, 已修) |
| P3 | PlaybookExitFastLoop | 20s | 同 P2 (playbook 通道独立快循环) | 实时报价 | 单宿主互斥防并发平仓 |
| P4 | LLMStrategyLoop | 循环自带守卫 | DA/UE 决策规则 | 市场数据+历史 | 决策链断裂有 da_chain_health_report 兜底 |
| P5 | ManualGuardLoop | — | 手动仓保护规则 | 持仓快照 | — |
| P6 | NewsV2Loop / VIXMonitorLoop | 120-300s | 数据新鲜度阈值 | 外部源 | 外部源死亡 → data_freshness_watchdog (中环) 兜底 |
| P7 | InsiderScanLoop | 日级 | 集群买入阈值 | insider 数据 | — |
| P8 | ORB 辅线引擎 | 自管 | ORB 参数 | 开盘区间 | daemon 自管 EOD → scheduler 级兜底 (历史修复) |
| P9 | position_sync | ~30s | DB == Broker 镜像 | Broker API | 幽灵仓根因防线 (broker-first) |
| P10 | Coordinator reconcile | 300s | 多通道一致性 | 各引擎状态 | — |
| P11 | LiveDealPush (RFC-078 批E 实盘成交推送) | 事件驱动 (Futu deal push, 零轮询) | REAL-only 过滤 (sim 同账户 SIMULATE 成交不入实盘账本) | OpenD 成交推送 | 推送漏 (重启窗口/OpenD 断线) → 每日 live_ledger_sync cron 幂等兜底; 双 ctx 双收 → deal_id 幂等去重 |

### 2.2 盘中快环 (Hermes cron, ET 交易时段)

| job_id | 名称 | schedule | 角色 | 失效模式 |
|--------|------|----------|------|---------|
| 36cc015242ee | ftseek-intraday-diagnose | */30 21-23 * * 1-5 | 执行层体检 (交易日检查+全量诊断) | 非交易日静默; 已 no_agent 化零推理 |
| 5513c8e94b06 | ftseek-intraday-diagnose-late | */30 0-4 * * 2-6 | 同上 (尾盘段) | 同上 |
| 0104bac6e9fd | DEB-20260815 硬止损监控·上午 | */30 21-23 * * 1-5 | 硬止损执行验证 | no_agent 确定性脚本 |
| 582b61f0e1a4 | DEB-20260815 硬止损监控·下午 | */30 0-4 * * 2-6 | 同上 | 同上 |
| 2a0eb17a475e | 盘前趋势验证 (v0.64 B类攒样) | 40 4 * * 2-6 | 影子攒样 (中环采集前置) | — |
| 27c495f96fd5 | E1 前瞻影子臂采集 | 45 4 * * 2-6 | 影子攒样 (DEB-20260815) | — |
| 7d448f79c410 | SPY暴跌预注册观察器 | 55 6 * * 2-6 | 压力门回放否决后的替代攒样 (DEB-20260826) | 仅 SPY 日跌≤-2% 时落盘 |
| 45185c0ba742 | 9/8 开盘首小时三项在场核验 (M-7, 一次性) | once 2026-09-08 21:50 CST | DEB-20260905 M-7 预注册替代控制 (48h 影子期因劳动节取消): chase_cap 遥测/enforce + 退出快环心跳 + G-01 组合兜底聚合三项在场核验 + DA/UE 供给/隔夜仓只读报告; 只读不改配置, 缺位仅给回写 auto_execute=false 建议由人裁决 | 非交易日空输出静默; 跑完与 db0a79fec814 一并清档入一次性留痕行 |
| db0a79fec814 | 9/8 首小时尾复核 (M-7 第二次, 一次性) | once 2026-09-08 22:25 CST | 同上 (开盘首小时尾部第二次核验, 覆盖首小时内触发指令到达后的遥测在场判据) | 同上 |
| d630ce54fc84 | 9/14 周一开盘首小时双验证 (退出链+RFC-082, 一次性) | once 2026-09-14 22:40 CST | 生产首验: ①退出链修复(他agent工作区WIP)零 exit_identity_required 拒绝 + exit_intents 全BOOKED(账本硬核查, 非日志grep) ②RFC-082 伪证门 DA enforce 生效后首批拦截行核对 (decision_id 前缀分流, 区分真拦 vs 遥测死); 只读诊断不改码不重启(服务已挂除外) | 9/11 曾因半部署 identity 拒绝1562条/平仓卡39min/事故≈-$129; 本验证是该事故修复+RFC-082 转正的共同首验; 跑完清档入一次性留痕行 (同 45185c0ba742 先例) |

### 2.3 中环 — 日级测量 (Hermes cron)

| job_id | 名称 | schedule | 角色 | 误差信号 |
|--------|------|----------|------|---------|
| e0d574cc1a3c | deep-stock-analysis 盘前扫描 | 0 20 * * 1-5 | 信号生成侧 | — |
| 2d69370d5c91 | 盘前综合预检 | 0 20 * * 1-5 | 数据源健康+IC+OKR盘前验证 | 数据新鲜度/可用性 |
| 9ac3efd8c5e7 | Deepulse 科技情报日报 | 30 20 * * 1-5 | 外部情报 (开环输入, 非反馈) | — |
| 7a40dd7c9e94 | 盘后综合复盘 | 0 4 * * 2-6 | 归因/告警/方向分析 (合并5旧job) | 成对口径误差 |
| 3a6c83bbd84c | deep-analysis 盘后复盘+记忆更新 | 0 5 * * 2-6 | 记忆沉淀 | — |
| 6af0648e3eb3 | alpha-backfill (SPY alpha+论文级IC) | 30 6 * * 2-6 | IC 测量数据回填 | — |
| 2254de46fe64 | dc双口径日对比 | 40 5 * * 2-6 | 传感器标定: 收盘口径×策略退出配对 | 口径分歧 |
| 13705b524e2a | FTSeek Insider Signal Monitor | 0 6 * * 2-6 | insider 异常告警 | 异常即报 |
| 1a4a63fd3dae | 影子组合每日结算 (DEB-20260808) | 10 8 * * 2-6 | 门的边际价值: 被拦单反事实净贡献按门聚合 | 门错杀赢家 (拦钱) / 放过亏家 |
| crontab-pen-backfill | 保本线穿透影子·夜间结局回填 (`scripts/penetration_outcome_backfill.py`, 系统 crontab 非 Hermes) | 5 6 * * 2-6 | 影子标签回填: 用离场后 1 分 bar 判 recovered_1/3/5/10m (V型回摆) + continued_bps_10m (续跌) + cf_floor_fill_saved_bps (反事实地板成交节省); 幂等自愈, 可见窗不足落 null 非 False | 端标口径错→被动侧击穿被系统性误标 V 回 (9/3 §十一 自查已修); 写读契约断→落盘行缺 status 使判分/看板恒 0 (9/3 复盘批已修+钉死); 载体心跳断流 >14 天由注册表哨兵告 |
| crontab-post-exit-path | 出场后全天路径影子 (`scripts/post_exit_path_shadow.py --days 3`, 系统 crontab 非 Hermes, 2026-09-04 MU V反核查产物; 同日 P1 口径修正) | 45 5 * * 2-6 | 全量平仓单出场后路径记录, 状态四分类 (ok=全天bar收盘前出场判分用 / ah_exit=夜盘出场看隔夜gap / bar_truncated=窗口bar补拉失败 / no_bars=无bar): post_fav/post_adv/close_gap + 深回撤V反上下文 + next_open_gap (出场→次日开盘, 隔夜段量化); 与 penetration (10m短窗) / exit_disable_cf (禁用反事实) 边界互斥不重叠 | P1: replay_bars 是多写入者混杂目录 (fetch_bars全天缓存 + 窗口文件), 首版未区分致 31 笔窗口口径污染 + 19 笔误标 → 已全量重建; 截断 bar 自主补拉写回 (与 archetype_bars_ensure 同源同缓存, 05:35 跑完再到本环, 纯增益超集); 注册表 `post_exit_path` (min_n=100, 永久测量项); 幂等 trade_id 去重, 断流由注册表哨兵告 |
| crontab-ue-macro-brief | UE 宏观披露 brief 采集 (`scripts/ue_macro_brief_collect.py`, 系统 crontab 非 Hermes, RFC-076) | 5 21 * * 1-5 | FRED 新披露 (非农等) → clue brief 硬事件段采集, UE 21:10 消费; 无新披露=空跑; 采集面复用 macro_calendar_shadow_collect; 回滚=删 cron 行或 clue_macro_brief=false (commit d8a9a4d8) | 挂 cron 时未入账 → G4 棘轮守卫报红拦截 (2026-09-05 task #17 全量回归发现, 当场补行登记 — 入账提醒纪律同 crontab-oneshot-0831f 先例) |
| crontab-live-ledger-sync | 实盘券商台账回补 (`scripts/live_ledger_sync.py --days 30`, 系统 crontab 非 Hermes, RFC-078 批B) | 10 17 * * 1-5 + 10 9 * * 6 | **券商真值层**账本回补: 逐笔成交 `live_deal_records` + 每日持仓快照 `live_position_snapshots` → **live 库** `ftseek_us_live.db` (批A 后看板专属载体, 与 sim 数据收集仓物理隔离); 两市场各一腿**独立三态** (OK / NO_DEALS / UNAVAILABLE), 一条腿挂不得掩盖另一条; 幂等 upsert (成交 by `deal_id`, 快照 by `(snapshot_date, market, symbol)`) ⇒ 漏跑补跑安全; 退出码 3 = 有腿 UNAVAILABLE | 为什么必须有这层: 回合台账 `trade_records` 依赖下单代码当场写, 而它**断过链** (`_record_manual_close` 委派签名漂移 2026-06-19~09-08, 券商已平仓账本零记录, 33d5568c 才修); 港股此前**完全不进 DB** (只有 jsonl 的 submit/cancel, 无成交价量盈亏) 而券商侧真钱真有仓 (实测总资产 HK$715,975.88)。券商真值层是唯一不依赖本地代码是否记得写的载体。纪律: `fee` 存 NULL 不伪造 0 (富途 deal 级不返回手续费) ⇒ 全部金额是**毛/成交额**; `deal_id` 存 TEXT (19 位 >2^53, 经 float 静默变位则幂等键失效); 综合账户下一次查询返回**两市场**成交 (实测 99 笔 = HK 47 + US 52) ⇒ `market`/`currency` 由每行自带 `deal_market` 推导, 不由「哪个 ctx 返回的」推导; 必须 disconnect (futu OpenD 持非守护线程, 否则 cron 跑完不退出) |
| crontab-eod-depth-gate | EOD深水闸影子结算 (`scripts/settle_eod_depth_gate_shadow.py`, 系统 crontab 非 Hermes, RFC-079) | 30 6 * * 2-6 | 影子判分: 结算 `data/shadow/eod_depth_gate_shadow.jsonl` arm 行 (EOD 豁免成立时打点, poll_pre_close+sweeper 双面同日去重) → Δ=反事实EOD平−实际结局; verdict 落盘 eod_depth_gate_report_latest.json (GRADUATION_LINE_HIT 只提示提请裁决, 不自动生效) | 判据字典 B19; 打点在 app/playbook/overnight_decision.emit_eod_depth_gate_shadow (异常全收口, 零豁免行为变更); 未平仓 arm 行保留 unsettled 待补; 回滚=eod_depth_gate_shadow_enabled false+重启 |

| crontab-unit-cell-readout | 单元级盈亏每日读数 (`scripts/unit_cell_daily_readout.py`, 系统 crontab 非 Hermes, 2026-09-09 用户令「按ROI优先级给出方案和实施」ROI①; 同日扩 D1'/D3' 小节 — DEB-20260909 §5.2 用户裁「同意」) | 55 5 * * 2-6 (紧随 52 5 的 gap_alignment_shadow 同班) | **消费面补链**: 读 `data/shadow/gap_alignment_shadow.jsonl` (单一真源不重分桶) 出三窗 (全史 / 滚动20交易日 / 当月) 方向×桶×窗口净额表 + 预注册触发判读 (SHUTDOWN_PROPOSE / CONFLICT_WATCH), 落 `data/reports/unit_cells_YYYY-MM-DD.md` (同日期幂等覆写); **D1' 极端跳空禁入影子小节**: 全日窗×双向(ag 方向归一同判)×双阈值腿(−3/−3.5)×按源分层的被拦篮读数, 毕业判据逐字 DEB §5.2 (每腿 n≥30 ∧ 被拦净<0 ∧ 误杀率≤40% ∧ 两腿同向); **D3' 按源监控小节**: 每源 SL_INITIAL 率/mfe0 率 (UE 通道独立风险因子, 纯监控无毕业判据); **record-only, 零交易行为变更, 不读 DB** | 触发判据预注册 `docs/proposals/unit-cell-shutdown-criteria-20260909.md` §A: hist n≥30 且 net<0 ∧ roll20 n≥10 且 net<0 → SHUTDOWN 提案; 当月 n≥10 且 net>0 降级 CONFLICT_WATCH (regime 漂移豁免, 首日实跑即拦 flat_or_down_open 误触发 — 历史血池 9 月反阳 +219/90%WR); 钉 27 例 `tests/test_unit_cell_daily_readout.py` (含只读纪律钉); 回滚=删 cron 行 |
| crontab-cb-shadow-breaker | GLM A' 日内累计亏损熔断影子结算 (`scripts/intraday_loss_breaker_shadow.py`, 系统 crontab 非 Hermes, 2026-09-13 补挂 — 注册表悬空告警 B1 同型病灶) | 28 6 * * 6 (周六班, 09:05 周检前数据已新) | **forward 攒样补链**: 当日已实现 pnl_net 累计 ≤ −400 (预注册冻结阈值) 后, 记录当日余下新开仓为被挡 + 假想轨迹 (=模拟盘真实结局); 全量重建幂等写 ftseek_us.db 派生表 cb_shadow_daily/cb_shadow_blocked + data/shadow/cb_shadow_latest.json; 主判据 B18 = 被挡笔期望 ≤ −10 ∧ n≥30 → 提请裁决启用实盘熔断; **record-only 零交易行为变更** | 补挂根因: 9/5 创建起 eval_script 从未挂 cron ⇒ 注册表 current_n 读 cb_shadow_latest.json 永不更新, forward 样本卡在 3 笔 (9/8 单日) 永远攒不够, 而 notes 已明写 backfill 有前视偏差判不毕业、价值全在 forward ⇒ 影子等于停摆。2026-09-13 实测读数 n=194 期望 **+46.83** (191/194 来自 backfill, 被挡单净 +$9,084) — 该符号**不可作裁决依据**, 须 forward n≥30 后重测。周跑即足 (非日跑): 阈值触发日稀疏 (25 触发日/5 个月), 全量重建幂等无增量丢失风险; 回滚=删 cron 行 (纯观察件, 无行为变更需回滚) |

| crontab-entry-delay-confirm | D2' 触发延迟确认影子 (`scripts/entry_delay_confirm_shadow.py`, 系统 crontab 非 Hermes, DEB-20260909 §5.2 最高优先级, 2026-09-09 用户裁「同意」) | 58 5 * * 2-6 (post_exit 45 → gap_align 52 → unit_cell 55 同班) | 对每笔 PLAYBOOK_SIM 已平仓穿越触发单回放当日 1 分 bar: 「延迟 N∈{2,5}min 复核, 价格仍站触发方向则进场否则放弃」反事实 — 回避率 (延迟后不再触发) + 机会成本 (方向归一延迟价差, 只在仍触发笔上计) + 回避价值 (=−被回避笔 pnl_net); price_at(t)=首根 bar_time≥t 的 bar 开盘价; bar 加载/截断补拉写回复用 post_exit_path_shadow 同一实现; 逐笔落 `data/shadow/entry_delay_confirm.jsonl` (幂等键 (trade_id, delay_min)), 汇总+判据落 `entry_delay_confirm_latest.json`; **record-only, DB 只读 (mode=ro)** | 毕业判据预注册 DEB-20260909 §5.2 逐字: 回放 n≥20 ∧ 避免率≥30% ∧ 机会成本<0.3%; 不达标退役。人群注记 (DEB 未指定人群, 双人群并列呈裁不算挪门柱): 避免率主判读人群=SL_INITIAL 出血族, 机会成本在全体仍触发笔上计。强制披露随汇总输出: ①confirm_gate 反向先验 (+0.50%/笔, t=5.67, 口径差异=盘前注入趋势域 vs 开盘窗微结构域) ②E3 PULLBACK_LIMIT TTL=300s 互斥 (转正时二选一或 TTL 重议)。首日回补 60 天 408 笔×2 腿 (ok 407/腿, 1 笔夜盘入场 no_bar_at_target 正确分类); 钉 17 例 `tests/test_entry_delay_confirm_shadow.py` (含幂等/只读纪律钉); 回滚=删 cron 行+删两个 data/shadow 产物文件 |
| dac32b02d402 | D2'入场确认门影子日报 (Hermes cron no_agent, `ops/hermes/entry_delay_shadow_push.py`, 2026-09-11 用户裁「要」每日推送) | 10 6 * * 2-6 (紧随 58 5 结算行之后) | **消费面推送**: 读结算产物 (entry_delay_confirm.jsonl + latest.json, 零写入/DB ro) 出 5-7 行日报 — 昨日批回避省$ vs 照进多付$ (假想盈亏差) + 墓地捕获 (MFE<0.3% 当日笔/回避笔, join trade_records.mfe_pct) + forward 窗 (2026-09-09 起) 攒样进度 n/20 与回避率/机会成本对预注册门 + 全窗参照冻结判据; 结算文件 >26h 陈旧 → 推警告行 (传感器失效可见化) | 失效模式: 结算 cron 停跑则本推送报陈旧警告而非静默; 回滚 = 删 Hermes job dac32b02d402 + 删 ops/hermes 与线上脚本副本 |
| crontab-settle-premarket-invalidation | 盘前失效影子结算器 (`scripts/settle_premarket_invalidation.py`, 系统 crontab 非 Hermes, 2026-09-11 断头传感器修复批, 用户裁「做」) | 2 6 * * 2-6 (D2' 58 5 结算后同班) | **判分侧补链**: 8/9 立项的 A/B/C 盘前失效影子 (premarket_invalidation.py 采集) 31 天 310 行零结算 (无结算器无登记 = 8/28 静默零产出同款病灶)。本器 join trade_records (decision_id 精确匹配) 算每规则 executed 净额/禁入价值/误杀率, 预注册判据 (每规则 executed n≥20 ∧ 净<0 ∧ 误杀≤40% ∧ 全窗滚动窗同向 → READY_TO_ADJUDICATE 另裁不自动生效) 写死于脚本头先于裁决读数; 产物 `data/premarket_invalidation_report_latest.json`; 影子注册表已登记 (id=premarket_invalidation, count_mode=where_nonempty:would_invalidate); DB mode=ro 零写入 | 失效模式: 采集侧停产→flagged 停增 (watchdog 挂 premarket_invalidation_shadow.jsonl 50h); 回滚=删 cron 行+删结算器+删注册表条目 | **9/11 DEB-20260911 裁决加固 (用户裁「按建议执行」)**: ①退役闸 — A_short_chase/C_long_chase 恒判 RETIRED_NO_PROMOTION (settle 脚本 RETIRED_RULES, 历史行仍结算呈报, 退役=停采+禁转正不删数据); ②转正前冲突核对 — READY_TO_ADJUDICATE 前置 _conflict_check, 相关组件须在注册表/台账互引, 注册表不可读或未互引 → 降级 KEEP_SAMPLING + stderr 留痕 (契约钉 tests/test_settle_premarket_invalidation.py TestRetiredGate/TestConflictCheck) |
| crontab-settle-ue-confirm-gate | UE 开盘确认门(U11)影子结算器 (`scripts/settle_ue_confirm_gate.py`, 系统 crontab 非 Hermes, 2026-09-12 断头传感器修复批, 用户裁「按建议执行」) | 6 6 * * 2-6 (premarket_invalidation 2 6 结算后同班) | **判分侧补链**: 8/26 立项的 U11 确认门影子 (llm_universe_scan.py cmd_confirm 采集) 8/26-9/11 共 11 交易日 78 条裁决零前瞻收益 (verdict 行只有 signed_pre_move 盘前动量, 无 fwd 字段, 而预注册判据要的是前瞻 ⇒ 判据永远算不出 = 死影子, 与 9/11 premarket_invalidation 31天零结算同款病灶)。本器 join signal_snapshots.fwd_ret_t1 (decision_id=universe_expansion:{date}:{symbol}, 方向签名后指令收益 U1契约) 算 PRICE_IN/CONFIRM 桶前瞻均值, 预注册判据 (≥10交易日 ∧ n≥30 ∧ PRICE_IN前瞻≤0 ∧ CONFIRM前瞻>0 → READY_TO_ADJUDICATE 另裁不自动生效; 否则 KEEP_SAMPLING) 写死于脚本头 先于裁决读数; 强制披露①confirm_gate 反向先验(+0.50%/笔 t=5.67 延迟确认有成本前科) ②n<30 桶标 n_lt_30_flag 只作方向参考; 产物 `data/shadow/ue_confirm_gate_report_latest.json` (atomic write); DB mode=ro 零写入 | 失效模式: cmd_confirm 采集停产→verdict 停增 (watchdog 已挂 ue_confirm_gate.jsonl 74h); 首跑 9/12 实测 PRICE_IN 前瞻均值被 1 只仙股 (ADBT +400%) 拉爆至 +12.49% 但 median 仅 +0.20% — 判据按 mean 走会误判, 呈报须附 median/日聚类 (已在本行留痕, 结算器 buckets 内含 n_lt_30_flag; median 切片见 /tmp/v11_dist 复核); 回滚=删 cron 行+删脚本+删 report 产物 | **首跑读数 (2026-09-12, n=74 join)**: PRICE_IN n=31 mean +12.49%/median +0.20%/日聚类 t=0.94 (仙股 ADBT 单笔 +400% 主导均值); CONFIRM n=38 mean -0.36%/median -0.14%/日聚类 t=-1.56; 两桶均不满足预注册判据 ⇒ verdict=KEEP_SAMPLING。**判读警示**: 判据写的是 mean, 但 UE 浮现池含仙股 ⇒ mean 受单笔尾部主导, 裁决时须同时看 median 与日聚类 t, 不得只凭 mean 符号翻正提请转正 (与 skill signal-ic-analysis 方向率必配幅度分布同律) |
| crontab-ue-tier-gate-eval | UE tier 闸影子结算 (`scripts/ue_tier_gate_shadow_eval.py`, 系统 crontab 非 Hermes, 2026-09-11 同批) | 52 5 * * 6 (fwd 回填 05:30 后, ue_tier_shadow_eval 05:50 同窗) | **判分侧补链**: PROPOSAL-20260905 U3/P4 tier 闸影子 (写者 llm_universe_scan) 此前只有写者无读者。预注册判据 (unified_config 逐字): 攒样≥15交易日 ∧ 被影子化桶 fwd n≥30; 毕业=被拦桶 fwd≤0 ∧ 放行桶>0 → 用户裁决; 撤闸认错=被拦桶>+0.3% ∧ n≥30。**方向分层 (LONG/SHORT) 并列读数不另设门** (UE LONG 全史−$780 vs SHORT+$208 的裁决备证, 防多重比较挪门柱); 产物 `data/shadow/ue_tier_gate_eval_latest.json`; 注册表已登记 (id=ue_tier_gate, count_mode=distinct:date, min_n=15); DB mode=ro | 失效模式: 采集断流→days 停增 (watchdog 挂 ue_tier_gate_shadow.jsonl 74h, UE 扫描日批写); 回滚=删 cron 行+删结算器+删注册表条目 |
| crontab-exit-disable-cf | M2 禁用型退出反事实影子日更 (`scripts/exit_disable_cf_shadow.py`, 系统 crontab 非 Hermes, 2026-09-12 体检补挂: 9/4 落地后从未挂 cron → 停更 8 天, 9/11 全链体检发现) | 25 6 * * 2-6 (与 ssr_shadow_daily 同班) | 对 MFE_STALL_EXIT/DEAD_SIGNAL_EXIT 出场笔用当日 1min bar 回放「若禁用该规则按现行剩余链持有到收盘」反事实, saved=cf-live 方向感知 (双边同往返费用相消); 喂 DEB-20260904-dead-signal-exit 裁决 (三路互证误杀); 幂等 trade_id 去重可补跑; record-only DB 只读 | 毕业判据预注册: 每规则 n≥50 ∧ saved中位>0.15% (费用线) ∧ 持有更优率≥65% → 开 DEB 裁决; 当前 n=61 (MFE_STALL 31/DEAD_SIGNAL 30), DEAD_SIGNAL 持有更优率 80%/saved中位+0.213% (9/11 补跑后); 失效模式: cron 断→jsonl 停更 (注册表哨兵 14 天超龄告); 回滚=删 cron 行 |

### 2.4 慢环 — 周/月级裁决 (Hermes cron)

| job_id | 名称 | schedule | 角色 | 判据来源 |
|--------|------|----------|------|---------|
| 0e87d094dcdf | 周六周检 (四锁+参数冻结+证伪门) | 5 9 * * 6 | 适应律执行器 (机械判定) | OKR-2026-09 KR1-KR4 |
| 1e9c597b0262 | 周日综合复盘 | 0 10 * * 0 | 周级测量 (标的表现/IC/交易汇总) | scripts/weekly_trade_summary.py 等 |
| 025ea4e6c7c6 | dc双口径周对比 | 35 9 * * 6 | 传感器周级标定 | — |
| e282a7667ae2 | 每周 ADF 平稳性 + 转正四判据 | 20 9 * * 6 | 实盘化门槛机械判定 (OOS≥15日∧净均值>0∧毛均值t>1∧ADF平稳) | 随机游走伪装成有期望的策略 |
| 9ab9f1812dc4 | DEB-20260830 CAP/F2 容量结算 + UE-D2 观察结算 (一次性) | once 2026-09-14 09:00 CST | 方案A (帽不动+三层护栏自然限流) 10交易日结算: CAP-1影子放行量 / CAP-2单净额劣化→方案B预案 / CAP-3名义帽连续3日触顶 / CAP-4 UE执行率 + F2质量分配; **[8/31第9轮用户裁"1"追加]** UE-D2 四判据: ①延迟300s内 ②无length截断 ③填充率≥50%白名单(验模板+模型供料稳定性非模型切换) ④时代盈亏切分(样本<10笔报无法裁决) | DEB-20260830 第12轮预注册判据 (阈值冻结自该轮); 只读脚本不写文件; 2026-08-30 补登 (守卫测试抓出漏登); 2026-08-31 追加 UE-D2 段 (jobs.json.bak_20260831_deb20260830_ue_d2) |
| crontab-oneshot-0831f | DA/UE 8-31 首批晨间核对 (一次性, 系统 crontab 非 Hermes) | once 2026-09-01 07:30 CST | v0.72 DA 首轮 (指纹 BD93AF5B0570 (大写防守卫误判): skip_reason_missing 违约→0 / finish 全 stop / 挑战者 verdict 分布=B11 前哨) + v2.8 UE+glm-5 首轮 (hash EB75D45A72C2 / IT 填充率报数 / 延迟 B6①<300s) + playbook decision_conf 分布 (B12 观察); 只读脚本 `scripts/verify_da_ue_batch_20260831.py`, 跑完自删本行 (失败也删+HIGH 推送, 防 9/1 固定日期行年度复燃; 手动重跑 `--date`) | 判据字典 B6/B11/B12 + DEB-20260831-da-debate-quality-and-decision-conf + handoff 2026-08-31f; 挂行当场登记 (入账提醒纪律) |
| crontab-pen-analysis | 保本线穿透影子·周频出题判卷 (`scripts/penetration_llm_analysis.py`, 系统 crontab 非 Hermes, timeout 300) | 30 7 * * 6 | 慢环: 确定性基线统计永远可得 → n_labeled≥8 才调 LLM (`penetration_pattern_analysis`, qwen→deepseek) 按 schema 提假设 → 每假设确定性 replay 全样本判卷 → 毕业门 n≥20 ∧ matched≥8 ∧ precision≥0.7 自动开 DEB 草稿 + 推送呈裁 (仅立案, 写执行代码须用户裁) | 判据字典 B17 + DEB-20260903 §十/§十二 + 注册表 `penetration_shadow`; 基率诚实披露: 家族 5 案/12 交易日且 8/21→9/1 零案 → n≥8 约需 19-80 交易日, 本环是慢环, 不得当议题二判分前置 |
| crontab-trigger-gate-anchor-push | 触发门同口径锚点裁决推送 (`scripts/trigger_gate_report.py --push`, 系统 crontab 非 Hermes, timeout 300; 2026-09-06 修尺子批②追加, 用户裁「同意」) | 24 6 * * 6 | 慢环裁决**呈报面**: 冻结锚点 `ANCHOR_SINCE=2026-07-24` 的 executed vs missed 前向缺口 + 双向同号稳健 + 机会成本 + 「持续≥2周」观测点数 (按日历日去重) → 走 `app/push` hub (`Level.NORMAL` + `Category.REVIEW_WEEKLY`, **sync=True** 拿真实送达, `dedup_key=trigger_gate_anchor:<day>` 同日只推一次); 推送是 **opt-in** (`--push`) ⇒ 手动重跑只读报表仍静默 | 修尺子批② (锚点块 commit `1ab660ac`): 周六三张记分牌 cron (exit/direction/trigger_gate) 原本一律只 `>> logs/*.log` **零读者** → 锚点观测点每周仅 +1, 凑满 2 个不同日即成熟, 不推就会在文件里**静默成熟** (判据字典 B17 僵尸尺子病灶同族); 失败**显性**不静默吞 (承 except:pass 棘轮 955) 且全通道失败 durable 落 `data/push_failures.jsonl`; 只测量/通知, **不碰退出参数** (C7 冻结期, 退出标定版本未换 — 版本号不写本表: 12 位 hex 会被 `test_ledger_job_ids_are_real` 的 job_id 正则当幽灵环路) |
| crontab-knife-gate-weekly | B门(接飞刀硬门)拦截质量周报 (`scripts/knife_gate_weekly_report.py --days 7 --push`, 系统 crontab 非 Hermes, 2026-09-15 用户裁「都做」批①, MARKER: KNIFE-GATE-WEEKLY) | 26 6 * * 6 (周六班, 紧随 06:24 触发门锚点推送) | **B门转正后持续追踪呈报面**: RFC-068 硬门 8/29 转正只有一次性反事实验证 (拦4省$305), 无持续记账 — 9/14 实锤单日误杀 $333 (BE/SKHY/NOK 全天飞刀区被拦, 收盘 shadow_pnl 全正) 才暴露无读数。消费 exec_gate_audit jsonl `skipped_knife_gate`, **按 (date, inst_id) 去重** (轮询级 28x 放大: 9/14 161 事件=4 指令, 同 trigger_miss_telemetry streak_ban 49x 教训), JOIN playbook_instructions 取 fwd_return_t1_open (裁决基准, T+1 回填) + shadow_pnl (仅诊断列, anchor 纸面口径禁止裁决引用); executed=1 (回升复评后成交) 单列 recovered 不算拦截; n<10 打 small_sample 旗标; 推送 opt-in `--push` (Level.NORMAL + REVIEW_WEEKLY, dedup_key=knife_gate_weekly:周次, sync=True), 产物 data/knife_gate_weekly_latest.json。**record-only 零交易行为变更** | 为什么必须有读数: 门转正依据是一次性回放, 转正后无持续反事实记账 → 9/14 单日误杀无周度呈报面, 只能靠人工复盘偶然发现 (判据字典 B17 僵尸尺子病灶同族: 门在跑但没人看); fwd_t1_open T+1 回填所以周六报最近 1-2 天恒缺属正常, 禁止拿 shadow_pnl 顶替裁决; 手动重跑默认静默; 失效模式: jsonl 轮转/字段改名 → 空窗报"门未触发" (与真空窗不可区分, 看 per_inst 长度核); 回滚=删 cron 行+脚本+本行 |
| crontab-live-weekly-review | 实盘周报复盘推送 (`scripts/live_weekly_review.py --days 7 --sync --sync-days 14 --push`, 系统 crontab 非 Hermes, RFC-078 批C) | 40 7 * * 1 | **实盘(真钱)周报呈报面**: 美股段 + 港股段**分开渲染**, 各段含 券商真值层(成交笔数/开平仓额/净现金流/FIFO 毛已实现/仓位快照多空分列) + 回合台账层(exit_reason 交叉表) + 台账↔券商对账 (孤儿在仓行/未入账仓) + 手动单活动; 末尾固定「数据状态三态清单」与「已知缺口」两段 | 为什么单独一个脚本而不是往 `weekly_trade_summary.py` 加两段: 既有周报体系**全是模拟研究口径** (该脚本白名单 PLAYBOOK_SIM/LIVE、`symbol_performance_report.py` 同白名单、`exit_scorecard.py:244` 写 `AND trade_mode != 'live'` 显式排除实盘) ⇒ 用户真钱交易在任何周报里都不是一等公民。批C 同时给 `weekly_trade_summary.py` 加了「模拟研究口径 (非实盘)」自称 + 指向本脚本, 两份报告不得互相冒充。诚实性约束(均有测试钉): **不给跨币种合计** (USD+HKD 相加会被当一个数读); NO_DATA(没同步) 与 NO_TRADES_IN_WINDOW(真没交易) 分开说; 对账缺一侧时**不出差集** (缺券商快照的差集会把正常在仓全报成孤儿, 比不对账更坏); 港股手动单活动读 `hk_manual_orders.jsonl` 而非 `manual_order_audit` (港股单不进该表, 只查 DB 会得出「港股本周无手动下单」的假结论而同期券商有 18 笔成交); FIFO 成本基准不完整的标的**不计入**已实现合计 (不用现价编成本); 多空市值不混加 (券商对空仓返回负 market_val, 混加实测得 7,563.10 而真实毛敞口 27,991.90)。推送 opt-in (`--push`), 手动重跑默认影子只打印 |
| 1e96c03c945e | UE thinking on/off 攒样裁决提醒 (一次性) | once 2026-09-22 09:30 CST | 慢环: 预注册判据到期提醒 — 判 `data/shadow/thinking_eval.jsonl` **有效轮次**是否达 ≥10 (扣除假日/非盘前窗/臂失败; 且**跨 prompt_hash 不得混合** — 9/8 起 prompt v2.9→v2.10 使序列归零, v2.10 首轮=9/8, 9/8→9/21 恰 10 个交易日), 达则按 REVIEW-20260905 §10.3 四项判据 (schema通过率不降/时延分布/tier结构偏移稳定性/triggers回退是否持续) 给"开/不开/继续攒样"; 有效轮次<10 时**禁止建议切换** | REVIEW-20260905 §10.3 (2026-09-05 用户裁"同意建议"); 2026-09-08 盘中审计补建 — 文档声称"攒样裁决提醒已设 9/22 09:30"但 hermes cron 与 crontab 均无此 job, 本行为补账 (入账提醒纪律第三次触发, 见 §2.4 末段) |

迁移留痕 (2026-08-28): `1a4a63fd3dae` / `e282a7667ae2` 的前身是 agent-session 定时任务
(e68ae9ad / b460b9c5, permanent)。**该载体绑定创建会话进程** — 进程一死, 任务在注册表里
照列"已排定"却永不触发, 且无任何告警: 结算链藏了 12 个交易日 (30 条入场全可结算而 outcomes
文件从未存在), ADF 零实跑两个周六。故本台账所辖环路一律走 Hermes/crontab (有 last_status
与 incidents 可查), **不得再用 agent-session cron**; 新增环路同此规则。

一次性裁决 job (已完成/禁用, 留痕不入守卫 — 本行标记被守卫测试机器豁免, 勿写入活体环路): 7a9452ad7045 (UE streak 裁决), 5401548b2034 (富途通道裁决), 02447474014e (观察信号四关门复跑), 7056699aa649 (重启生效), 09c3d6932372 (RFC-070 cron对账哨兵, 2026-09-03 清档), ee246069b226 (RFC-071 收盘后重启验证, 2026-09-03 清档 — jobs.json 记录经 hermes cron rm 删除以释放 SQL 守卫红灯, 执行史在 executions.db; 判据语义已由各自 DEB/提交留档)。

**⚠️ 一次性 job 入账提醒 (2026-08-30, 同日两次守卫报警后立)**: 新建一次性
Hermes job 时**当场**在本表 §2.4 加一行 — 当天已两次因"建了没登"被
`test_control_loop_ledger` 拦截 (两例: 9ab9f1812dc4 仍在册; 另一例已清档入上方
留痕行)。守卫存在就是为了抓这个, 第三次不应再发生。

### 2.5 测量免疫系统 (Meta loop) — 监控"监控本身", 不产生交易决策

本环不参与下单/平仓, 职责是让**其余环路的失明变成告警**。共同的立起理由来自 2026-08-28
一天内顺手挖出的四类"日志正常但监控空转": 人工 WATCHLIST 登记不到的源死了永远无人知、
表载体永久零行 (earnings_calendar → 财报 ATR 上限门从未生效)、调度任务静默停摆、
了结行字段级消失。**规则: 检测器自身的异常必须产出一条 finding, 严禁 `except: pass`**
("零违反"与"守卫崩了"必须可区分)。

| 载体 | 检查 | 误差信号 | 立起的失效模式 |
|------|------|---------|---------------|
| scripts/data_freshness_watchdog.py `D1` (cron 25 6 * * 2-6) | 从 sqlite_master **自动枚举**主库表载体 + 仓内 `_repo_write_targets` 反查写入方 → 有写入方却零行/停更 >21天即告 | 载体空/停更 | WATCHLIST 是人工清单, 登记不到的源死了无人知; 基线 `watchdog_liveness_baseline.json` 棘轮静默既有墓地 (15 项) 防首跑淹没新信号 |
| 同上 `D2` | Hermes `jobs.json` cron 表达式 → 应跑槽位, 与 `executions.db` 实跑对账, 连续 ≥2 槽未跑即告 | 调度意图 ≠ 执行 | agent-session cron 载体绑定死进程 (影子组合结算藏 12 日 / ADF 零实跑两周六); naive vs aware 比较异常曾使整段恒报"全健康" → 现补 `D2:detector_error`。实测 20 启用作业: 16 判定健康 / 4 新建未到期, 零误报 |
| scripts/daily_invariant_check.py `I11~I14` (cron 5 5 * * 2-6) | **全部 source_type** 的了结行字段级守卫: realized_pnl NULL / 0 占位伪装盈亏 / 费用上线日后缺净额回落毛口径 / 缺 exit_reason | 字段级消失 | 出场日记账后, 缺字段不再是"算错"而是从当日数字里静默消失; I1~I10 只见 PLAYBOOK% 行 (实测了结面 64 行 vs 主样本 62)。豁免词表 duplicate/outlier/invalid/void/excluded, 首版"data_quality 非空即豁免"实测 62/62 全命中=守卫零覆盖 |
| scripts/config_key_reverse_census.py `D3` (cron 40 7 * * 0 与正向普查同班) | 反向: 代码里的 `get_dotted(...)` 读点 → 配置里是否真有该键, 字面量默认值即"隐形开关" | 声明与执行分叉 | 正向普查 (零消费配置键) 与反向是两件事: `capital.initial` 型默认值扛行为而配置无声明; 门 `--gate` 走基线棘轮。本轮已揪出 `strategies.orb` 错命名空间读 (真实键在 `orb:`), 风险门按代码默认 3 而非声明 2 放行 |
| tests/test_dead_db_literal_gate.py | 代码级引用废弃同名单 `ftseek_us_live.db` 的站点必须逐文件声明理由; 名单内文件不再引用=陈旧豁免同样报警 | 死库伪装 | sim 模式下该库零写入且"读得到、长得像真库" (实测冻结 2026-07-19), thesis_challenge/battle_plan/watchlist/plan_feedback 均硬编码过; 单向普查本身即缺陷故双向 |
| scripts/deploy_freshness_check.py `crontab-deploy-freshness` (cron */10 * * * *, 2026-09-05 G-02) | `drift = max(app/**/*.py, main.py mtime) − 进程启动时刻` (进程龄读 `/proc/PID/stat` field 22 + `/proc/uptime` 反推, 实测与 systemd `ActiveEnterTimestamp` 对齐); drift>120s 且在北京 09:00–12:00 安全窗内 → `systemctl restart` + critical; 窗外只告警不重启 (按 `newest_code_ts` 去重) | 部署意图 ≠ 进程内实际代码 | **YAML 热载而 Python 代码只在 import 时加载一次** ⇒ 2026-09-05 实锤: chase_cap enforce (87ebc785 07:19 落盘) 与经验泛化批 (46e31ccf) 均已入库, 而进程 PID 266035 起于 9/4 14:45 → 日志/配置/测试三绿而**风控闸是纸面的** (REPORT G-02 严重度 5)。此前无每日重启机制, 断层只能靠人工发现。两条自身失效模式已堵: ① 探测器坏了 (`undetermined`) 必须吼一次, 禁 `except: pass` (§6 规则5); ② 严禁 `stat -c %Y /proc/PID` 取进程龄 — 那是目录实时 mtime, 比代码 mtime 恒真 ⇒ 守卫恒报 fresh (§6 规则6② 同源教训)。代价诚实登记: 重启丢 `trail_state`/`_exit_cooldowns` 内存态 (REPORT H-12), 故只在代码真变时重启, 不做无条件每日重启 |
| scripts/daily_alert_summary.py 权益对账行 (随 Hermes `0 4 * * 2-6` 盘后综合复盘) | 恒等式 **C ≈ G + U**: C=capital_state 收盘印权益变动, G=当日已了结毛利 (出场日记账), U=券商未平仓仓当日波动 (`today_pl_val`, 毛额); 残差 R 超 $50 标 ⚠️ | 两口径间不可归因差额 | **纯观测, 不进任何判据**。立起理由: 2026-08-29 用户拷问"富途 App 今日盈亏 +5百 vs 日报 −338.76" — 两数本就不同口径 (App=账户全口径含未平仓波动, 且模拟盘 `today_pl_val` 是毛额; 真账户另有用户自有 SNDK/MU 仓 Σ+480.59), 差异必须每晚机器摊开而非等人问。失效模式两则: ①首版"取最接近 boundary 的样本"选中收盘前 13.5s 的盘中共价 (单项 $40.75 直接灌进残差) → 改取收盘**后**首样本, 窗内无样本即不判; ②任一输入缺失/异常/跨账户 → `not_judgeable` + 原因, 严禁以 0 冒充已对账 (§6 规则5/6 同源), 券商侧 `qty==0` 行必须排除否则与 G 双计→桥接假闭合 |
| scripts/daily_alert_summary.py `resolve_log_sources`/`parse_log_alerts_window` (随 Hermes `0 4 * * 2-6` 盘后综合复盘 Step 3; 2026-09-09) | 会话窗 (US 交易日 21:00 → 次日 06:00 CST) 跨 logrotate 档拼接: `dateext -%Y%m%d` 候选档 (含 `.gz`) 按日期升序 + 当前档殿后, 逐档复用 `parse_log_alerts` 的时间戳过滤, 跨档按 (time, level, content) 去重防轮转读写竞态 | **告警计数被日志轮转截断**; 读数面自带 `[覆盖]` 诊断行 (档数 + 逐档条数 + 告警跨度 + 窗口边界), **同时进 stdout 与推送正文总结区** (只进 stdout 时, 正文那句「✅ 盘中系统运行正常」在覆盖不全时仍会说谎 — 9/8 实证), 档数=1 且跨度明显短于会话窗即盲区复发 | `/etc/logrotate.d/ftseek-us` 用 `daily + copytruncate`, 由 `/etc/cron.daily/logrotate` 触发, 近 7 个交易日实测轮转时刻落在 **03:12–03:48 CST = 美股会话 (21:30–04:00 CST) 内部** ⇒ 原实现只读当前档 `logs/ftseek_us.log`, 会话前 ~5.5 小时全盲。实证 9/8: `chase_cap拦截` 轮转档 145 条 / 当前档 3 条, 推送报「0 错误 + 6 警告」= 真相的 4%; 9/4 同病 (轮转档 142 条 WARNING/ERROR / 报告 2 条, 且两条时间戳都是轮转后的 03:52)。修后 9/8 复算: 盘中 603 条 (3 ERROR + 600 WARN) / 138 组, 并首次暴露此前完全不可见的 `[Finnhub] 429 限速 ×61` 与 `[再入场] 反手拦截 ×27`。纯读数面修复, 不改判级/噪音过滤逻辑 (单一真源逐档复用)。钉子 `tests/test_daily_alert_summary_log_sources.py` (10 例) |
| scripts/daily_invariant_check.py `W7` (cron 5 5 * * 2-6, 2026-09-11 DEB-20260911 P0-3③ 用户裁「按建议执行」) | 两条**昨收取数路径**的 chg 读数对账: `data/premarket_invalidation_shadow.jsonl` (快照路径 `get_market_snapshot.prev_close_price` = 干净侧, SMTC 8/26 日线真值对照已证) × `data/entry_timing_shadow.jsonl` (ctx 路径 `get_quote` push 缓存 `change_percent` = 陈旧侧); join 键 inst_id 精确优先、缺失退 (symbol,date); 可比窗 ±10min 内取**最小** \|Δchg\|, >2pp 即判分叉 (分母一致 ⇒ 最接近时刻两读数必 ≈0; 分母分叉且全日冻结 ⇒ 窗内任意时刻必全发散)。棘轮基线 `data/chg_denominator_baseline.json` (首跑锁 90 存量键, BIDU 9/8 带 DEB 出处入 refs): 基线外新增当晚告 `W7_chg_denominator_divergence` (损失封顶当天), 存量计 silenced_known 不刷不可动作噪声, 愈合列 healed 提示**人工**收缩, 守卫绝不自动改写基线; 影子档缺失=skipped_no_shadow, 单行坏跳过计数, 整档不可读=`W7_detector_error` | 昨收**分母口径**分叉 — 两个影子各自"零违反" ≠ 两边口径一致 | 2026-09-08 BIDU 实锤 (DEB-20260911 C6): 同指令 7 秒内快照侧 −7.65% vs ctx 侧 +4.07% (差 11.72pp, 隐含分母 99.47 vs ≈88.26), ctx `change_percent`/`volume` 全日冻结 ⇒ 当日**所有** `ctx["change_percent"]` 消费者 (short_chase_ban_shadow / entry_timing_shadow / 记分牌 / chase 类门输入) 读数全部不可信, 而此前无任何守卫盯"分母"维度。首跑实测 paired=195 / unpaired=115 (窗外含日内漂移, 不可比不判) / divergent=90 ⇒ **46% 可比对分叉**, 非个例; 窗参数不敏感 (5/10/15min 窗发散数恒 90)。契约钉 `tests/test_w7_chg_denominator_sentinel.py` (12 例, 含 BIDU 形态当晚告警 / DDOG Δ0.17pp 健康对照零误报 / 窗外行与跨 inst_id 串染两个防假阳钉 / 棘轮只降不升); 回滚=删 W7 段 + main() 两行接线 + 基线文件 + 测试文件, 零生产代码依赖 |
| app/playbook/shadow_registry.py `check_intent_uniqueness` (随周六周检 `0e87d094dcdf` 推送, 2026-09-11 DEB-20260911 P0-3①) | 注册表**意图唯一性**五判定: ①同一非空 `intent` 的现役项 (OBSERVING/READY_TO_EVALUATE) 必须互指 `cross_ref`; ②互引目标必须在册 (悬空即报, 含终态留痕项); ③目标现役则必须回指 (单向即报 — 补①盲区: 本仓真实那对 intent 标签不同); ④`REGISTRY` 内 id 必须唯一; ⑤守卫自身异常产 `intent_uniqueness_detector_error` (§6 规则5)。纯内存判定, 告警经周检 `intents` 键与 mounts 并列呈报 | 同假设双影子独立裁决 (一个 FAIL 一个 PASS → 双转正打架) / 登记条目被静默吞掉 | 立起实锤: A_short_chase (09:35 快照口径) 与 short_chase_ban_shadow (触发时刻口径) 同阈值 +3% 测同一假设「逆势追空禁入」, 独立攒样独立裁决、台账零互引, 且已在真实数据给出矛盾读数 (BIDU 9/8 −7.65% vs +4.07%, 连分母都不同源) ⇒ 双转正打架非假想风险。现有守卫 (`test_control_loop_ledger` / `check_mounting` / D1 watchdog) 只强制**存在性登记**, 无机制强制**意图唯一性**。④系同日真实事故: 并发补登记产出同 id 两条, `summary()` 重复出行、互引/计数只认一条。9/11 处置: A 退役、假设归并 ban-shadow 作单一证据源 (A 快照行不得独立转正); 现网零告警。契约钉 `tests/test_shadow_intent_uniqueness.py`; 回滚=删函数 + 周检两处接线 + ShadowItem 的 intent/cross_ref 两字段 |

---

## 3. 稳定包络 (Stability Envelope) — 硬边界, 越界即改变行为

| 包络 | 实值 (2026-08-28 核实) | 配置源 | 消费点 |
|------|----------------------|--------|--------|
| 名义敞口帽 | daily_notional_cap_pct: 0.8 | unified_config.yaml:984 | playbook 通道日开仓总名义 |
| 单名帽 | max_position_pct: 0.08 | unified_config.yaml:60 | DEB-20260815 P0-A 收紧 (0.25→0.08) |
| 日开仓数帽 | max_daily_positions: 16 | unified_config.yaml:1010 | pipeline.py:3279 唯一消费 (SSOT) |
| 单笔风险 | risk_per_trade_pct: 0.008 | unified_config.yaml:1509 | sizing |
| 日亏门 | max_daily_drawdown: 0.03 | unified_config.yaml:1387 | pre_trade_gate Check 0 |
| 四锁 (实盘隔离) | sim/false/futu_simulate/毕业门禁 fail-close | .env.us + unified_config.yaml | 周六周检实测 + 毕业门禁三重闸 |
| 费用线 | ~10bps (edge 必须 > 费用线) | DEB-20260823 实证 | 准入判据, 非配置项 |

**冻结区 (主 cohort 窗 2026-09-03 → 09-30)**: 退出参数 (playbook_horizon.yaml 退出段) 冻结,
权威核查载体=日级守卫 `scripts/daily_invariant_check.py` `_check_config_freeze` 的 cohort 维
(config_freeze.cohort 块, 窗内退出哈希唯一值数=1); 周六周检 `check_freeze()` 同口径锚点并列核查;
例外仅限 P0 bug + DEB 落档 + 用户知会。(2026-09-06 KR2 判分单位修订: 判分单位从全窗 8/24 起改为主
cohort 窗 — 全窗已不可逆有 4 个哈希, 按全窗字面判必 BROKEN; 用户授权代裁 (a)+(b) 合流, 历史留痕见判据字典 C1。)

---

## 4. 统计准入条件 (Admission Gates) — 允许环路运转/毕业的条件

| 门禁 | 判据 (预注册) | 执行器 | 状态 |
|------|-------------|--------|------|
| 宪章第 9 律 | 按日聚类 + MDE=2.8×sd_daymean×√(2/n_days); \|效应\|≤MDE 一律"不可分辨" | 所有统计输出 | 已立 |
| KR3 入场侧假设检验 | 按日聚类 t>2 + MDE 覆盖声称效应 + 样本外验证; 只许一个假设 | 预注册+周六周检 | 9月开跑 |
| VWAP 斜率证伪门 | 预注册判据, 机械判定 | falsification_gate_weekly_review.py | 周检 §3 |
| 锚限价影子毕业主门 | 置换 p + 双条件 (δ̄≥$1, CI下界>0) + 符号共门; 停止规则只降级不重置 | limit_entry_graduation_gate.py | 周检 §4 |
| E2 动量确认毕业门 | **已退役 (2026-09-13 用户裁「无效就停止，退役」)**: 恒判 RETIRED_NO_PROMOTION, 读数留档; 采集 cron (e2_momentum_shadow_daily 20 5 * * 2-6) 已摘; 数据 e2_momentum_shadow.jsonl 242行保留; 证据: 统计不可分辨(\|diff\|=29.91<MDE=40.88) + OOS拦截组衰减归零(-$25.15→+$0.21, 现役门已覆盖人群) + 误杀率50.7% (70/138盈利单含trailing赢家21笔) + SHORT样本不足(6/9)方向对称无法验; 回滚须另开辩论 | e2_momentum_graduation_gate.py | 周检 §4b |
| 样本量红线 | n<30 永不硬拦 (只观察); ≥30 才可裁决 | 各攒样脚本 | 长期 |
| 双窗口裁决 | 双窗口 ≥40% 才采信 | 转正流程 | 长期 |
| 毕业门禁 (切实盘) | 旧判据已废 (测常量); 新判据待按日聚类尺子预注册 + 用户签字; 生效前保持 fail-close | _check_graduation_gate | 重定义中 |

---

## 5. 开环缺口 (Open-Loop Gaps)

| # | 缺口 | 性质 | 处置 |
|---|------|------|------|
| G1 | ~~OKR 以 PnL 为误差信号~~ | **已闭合** | OKR-2026-09 已重编译: 不设盈利目标, 尺子+冻结+预注册, PnL 仅观测 |
| G2 | ~~适应律未成文~~ | **已闭合** | 2026-08-28 立 `docs/ADAPTATION-CONTROL-LAW.md` (改参数转移函数五段, 编排宪章§二/§四+OKR KR2+config_version, 无新机制) |
| G3 | regime 传感器未独立验证: regime 条件化规则 (盘前失效 **B**; A/C 已于 2026-09-11 退役) 与 LONG 回调日的分类器本身 IC 无人测 | 半开环 (**9/11 缺口收窄**) | 与现有 record-only 攒样线合并, 不另起炉灶。**2026-09-11 DEB-20260911 进展**: 昨收轴 (chg vs prev_close) 首次拿到 IC 读数 — 310 行影子 × `playbook_instructions.fwd_return_t1_adj` Spearman: SELL_SHORT rho=−0.378 (p≈0.005, 轴有信息量且方向支持"逆势追空差") / BUY rho=+0.151 (**与 C_long_chase 的假设方向相反**) ⇒ 该轴已从"无人测"转为"已测并据此退役 A/C"; 但**regime 分类器本身** (HARD_EVENT/MARKET_CORROBORATED/THESIS_ONLY 的分档准确度) 仍未独立验证, 且延伸度轴 (chg/ATR, 实测比昨收轴强 1.7–2×) 尚无影子载体 → 缺口未闭合 |
| G8 | **昨收分母口径分裂 (DEB-20260911 N1)**: `prev_close` **三源并存** — ①futu 快照 `get_market_snapshot.prev_close_price` (干净, SMTC 8/26 =127.52 与日线真值对照已证); ②`get_quote` 推送缓存 `prev_close` → `ctx["change_percent"]` (**跨日冻结**, 污染源); ③RFC-068 日K自算注入 `ctx["prev_close"]` (`app/data/market_data_router.py:509-534`, 干净, 现役 `B_long_knife` 硬门走此侧)。①②同源不同值: BIDU 2026-09-08 实锤同指令 7 秒内 −7.65% vs +4.07% (隐含分母 99.47 vs ≈88.26), `change_percent`/`volume` 全日冻结; 全量对账 195 可比对中 **90 对 (46%) 差 >2pp** (辩论期 38 对同窗抽样中 21 对 >3pp) ⇒ 不是个例而是常态污染面 | 半开环 (**测量面 9/11 已闭合到"当日可见", 生产修复未排期**) | 已落: ①`W7` 分母哨兵 (§2.5) 每晚对账, 基线外新增当天告警, 存量 90 键锁 `data/chg_denominator_baseline.json`; ②注册表 `short_chase_ban_shadow` 条目把"分母修复前样本=污染期"写进转正前置条件, 是否计入 10/31 预注册 n 由裁决日显式裁, 不得默认全计。**未落 (DEB-20260911 列为 P2, 本轮明确未执行)**: `change_percent` 单一真源化 (改读快照昨收或强制刷新缓存) 属**生产代码**改动, 爆炸半径覆盖 short_chase_ban_shadow / entry_timing_shadow / 记分牌 / chase 类硬门输入 (注: 现役 `B_long_knife` 硬门走的是 `ctx["prev_close"]` 日线自算侧, RFC-068 8/29 审计已修, 不在污染面内) ⇒ 须另立 DEB 预注册 + 用户裁, 不得在测量批里顺手改。**附带登记**: ban-shadow 自 8/8 写盘 34 天而 registry/watchdog/台账三处零命中 =「建了没登」第 4 例 (前三例见 §2.4 入账提醒), 9/11 补登记 (id=`short_chase_ban_shadow`, eval_on_demand=True, 死线 2026-10-31); **不入** `data_freshness_watchdog` — 18 天仅 5 行, 小时级新鲜度阈值会永久误报, 断流改由注册表 `check_mounting` 的 14 天静默哨兵接管 |
| G4 | 台账漂移: 环路新增/下线后本文件不同步 | 由守卫测试闭合 (**2026-09-03 补系统 crontab 面**) | tests/test_control_loop_ledger.py 原先只核 Hermes `jobs.json` — 系统 crontab 是盲区 (实测 68 个 crontab 调用的仓内脚本仅 2 个在台账, 穿透影子两环 9/3 挂 cron 时无任何守卫拦截)。现加棘轮: 存量 66 个锁 `docs/references/crontab-loop-baseline.txt`, 新增环路必须入 §2 (推荐) 或显式加基线带理由, 基线内脚本若已下 crontab 同样报 (陈旧豁免=幽灵行)。存量 1:1 回填属专项 (REVIEW-20260903 §五) |
| G5 | 自调器 (自动改参) | **用户已裁不做** | 小样本+非平稳+大延迟 → 自动适应是噪声放大器; 现形态"自动观测+人工裁决+门控生效"即小样本控制正确解 |
| G6 | 元环只闭合"失明", 不闭合"该链是否该存在": earnings_calendar 永久零行 ⇒ 财报 ATR 上限门从未生效 | **已恢复在线 (2026-08-29 复核纠错)** | ~~已裁决下线~~ **裁决撤销 (2026-08-29 当日复核)**: 原裁决证据"earnings_calendar 表 0 行 ⇒ 门从未拦截"指向死表 (app/risk/earnings_guard.py SQLite 载体), 与本门实际读的活载体 (earnings_risk_guard.py 内存态, Alpha Vantage 经 scheduler_core→get_watchlist_earnings(7d)→update_earnings_calendar 每日喂数 103~109 只) 同名不同物。生产日志实锤: 8/19-8/26 拦截 85 次 ("[playbook] #2 财报门拦截", BABA 36 / CRM 49), 且 8/29 当日 11:17 仍在喂数 — 是正在工作的活门。键已置回 true + 守卫测试翻转 (test_g6_earnings_blackout_gate_stays_online 钉住键为 true)。教训入档: ①同名双载体 (死表/活内存) 是"表面证据掩盖真实行为"的典型陷阱, 审计门活性必须追到消费者实际读的载体; ②文本/表级断言测不出"数据流是否接通、行为是否发生", 门活性须以生产日志拦截计数为行为级证据; ③前置条件①"可靠财报日历源"实际已由 AV 满足 (非 0 行), 富途原生 get_earnings_calendar 可作冗余源待议。D1 看守的仍是死表, 不影响活门。证据登记 (08-29, DEB-20260817 第7轮): 47 笔跳空跌抄底中 33 笔带财报叙事, RBRK 08-28 实锤财报后承接回调 SL_INITIAL -$159 (门在线恰可覆盖此类事件风险) |
| G7 | `CapitalSafety._save_state` 的 2026-06-21 "P0 修复: INSERT OR REPLACE 用 trade_env 作唯一键 UPSERT, 保持 1 行/env" **从未成立** — 该列无 UNIQUE/无冲突可触发 ⇒ 纯 INSERT, 实测 capital_state 42146 行仍在涨 (注释声称要止的"无清理机制"原样存在) | 日志正常但逻辑失效 (注释与行为分叉) | **已裁决排期 (2026-08-29, 用户裁"按建议执行")**: 下个维护批三步一批做 — ①历史 4.2 万行挪归档表 ②建 UNIQUE + 改 UPSERT ③对账行查询改"归档+活表"合看; 拆开任何一步都会让权益对账行静默退化"样本缺失"。表小无性能压力, 不急修 |

---

## 6. 维护规则

1. 新增 cron job: 必须在 §2 对应层入账 (job_id + 角色), 否则守卫测试红。
   **系统 crontab 同此规则** (2026-09-03 复盘批补齐, 此前只 enforced 在 Hermes 面):
   非 Hermes 环路首列用 `crontab-<名>` (先例 `crontab-oneshot-0831f`); 存量锁
   `docs/references/crontab-loop-baseline.txt` 棘轮基线 — 从基线删行 = 已入台账 (欢迎),
   往基线加行 = 必须带理由与出处, 否则守卫红。
2. 包络/准入判据变更: 必须同步 §3/§4 并注明裁决来源 (DEB/RFC/用户裁)。
3. 一次性裁决 job 完成后移入 §2.4 留痕区, 从守卫范围移除 (enabled=false)。
4. 台账不记载任何"预测性"内容 — 只记结构、实值、判据、失效模式。
5. 元环新增探测器: **必须自带"我自己坏了"的输出路径** (findings/violations 里出现
   `*_detector_error`), 禁止 `except: pass` 或异常时返回空清单 — 2026-08-28 D2 实锤:
   naive/aware 时间比较抛异常被吞, 整段从此恒报"全健康"。基线棘轮
   (`watchdog_liveness_baseline.json` / 普查 baseline) 只能由脚本 `--update-*` 重生成,
   不手工加宽 (手工加宽=把墓地重新藏起来)。
6. 生效/自验脚本的每条判据必须**可证伪** (能报坏) 且**与生产告警链同源同形** —
   2026-08-29 盘中重启实跑证伪三例: ①`journalctl` 数 ERROR 恒 0 (本服务 stdout 几乎不进
   journal, 真通道是 `logs/ftseek_us.log`); ②`stat -c %Y /proc/PID` 是该目录实时 mtime,
   拿它比 commit 时刻恒真 (进程年龄须 `ps -o etimes` 反推); ③awk 模式 `/ ERROR /` 永不对上
   本日志 `] ERROR:` 形态 (LEVEL 后随冒号非空格) ⇒ ERROR 硬判恒 0, 而告警链
   `scripts/error_alert.sh` 用的是 `/ERROR|CRITICAL/` ⇒ **判据模式必须逐字与告警判据一致**,
   且改动后要注一行错样本反证 (旧=0 / 新=2) 才算立起。
   **2026-09-10 更新 (DEB-20260910 §4)**: 告警链已重写为 `scripts/error_alert.py`, 判级
   口径改为**字面量** `"] ERROR:"` / `"] CRITICAL:"` (与本日志形态、与 intraday_heartbeat
   同源单一真源) —— 旧 .sh 的 `/ERROR|CRITICAL/` 是级别盲匹配 (INFO 正文含 ERROR 即计入),
   且只读 `logs/ftseek_us.log` ⇒ `logs/invariant_check.log` 里的违反从未推送过。
   本条规则的现行含义: 新写探测器要对齐的是 `error_alert.py::LEVEL_MARKERS`, 不再是 awk 模式。
