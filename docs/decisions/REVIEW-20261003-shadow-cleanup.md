# REVIEW-20261003 · 影子/证伪门清理批: 梳理 + 到期裁决落地 + 推送收口

> **触发**: 用户 2026-10-03 令 —「影子组合和证伪门等攒样积累太多, 快速迭代中已忘记
> 初始背景和目标; 需要梳理清理, 不要过多空跑空耗和推送, 信噪比太差」。
> **权限层**: 本批全部为 **测量面/通知面/记账面** 动作 (config shadow-only 开关、
> cron 注释、注册表状态、推送门), **零交易行为变更、零退出参数变更、零 prompt 变更**;
> 其中两条判据处置 (ue_midday / reentry_quick_tp) 是**预注册判据的机械执行**, 判据
> 一字未改 (改判据 = 重新裁决, 须另立 DEB)。

---

## 一、初始背景与目标复述 (为什么有这些影子)

**项目目标 (OKR-2026-09, 承接 2026-08-23 三判决)**:
「让模拟链路产出统计上可用的证据, 并用修好的尺子预注册裁决一个入场侧假设。」
- 模拟照常 (~200 笔/月), 为单一参数版本窗攒样本; 打磨对象从退出参数转为测量装置+入场侧。
- **打标 (毕业) 前严格不切实盘**: 四锁 + 毕业门禁 + 用户签字。
- 已确立事实 (DEB-20260823 + 9/6 复核): DA 方向选择测不出 alpha (claim IC ≈ 0,
  n=1805 不再是样本问题); 系统里唯一被验证的优势是 **gate 负筛 + 退出锁利**;
  毛期望 ≈ 0 而净亏 = 费用; 每周调参循环 = 过拟合机器 (已废止)。
- 控制论造型 (CONTROL-LOOP-LEDGER): 盈利不是被追逐的量, 是包络内环路运转的渐近行为;
  适应律五段 (触发→证据→增益→验证→回滚), 人工门 = 用户签字。

**影子/证伪门在造型中的角色**: 中环测量层 — 行为变更一律影子先行 (record-only),
预注册判据攒满即裁: 过线 → 提案转正 (另走 DEB + 用户裁); 不过线 → 退役停采。
**本批要治的病灶**: 入口有了、出口失守 — 判据已死还在采、到期没人裁、写方未注册、
日报无料也推, 于是「攒样」变成无目标的空转。

---

## 二、清理前状态 (2026-10-03 盘点)

- **注册表**: 62 项 (OBSERVING 45 / KILLED 12 / GRADUATED 2 / SUPERSEDED 2 / READY 混在
  OBSERVING 计数内) + **5 条在采但未注册** (shadow_portfolio、cluster_concentration、
  conflict、ue_midday、ue_earnings_clue) ⇒ SSOT 失明。
- **待裁面**: 8 项标「需要你决定」, 其中 6 项**从未跑过 eval/无读数**; 另 21 项已读自动续观。
- **到期未裁实锤**: cluster (44 日判据门已过)、conflict (n=36≥30 已过)、
  ue_midday (判据双 FAIL 未执行处置)、reentry_quick_tp (窗 9/30 已闭未结算)、
  top_movers (9/30 闹钟已过 needs_user)。
- **每日推送**: shadow_board 全量日报 (无料也推)、D2' 入场确认门日报 (双面均自动续观
  仍日推) — 即用户投诉的「空跑空耗推送」。
- **已知但不动的终态写盘** (2026-09-26 RFC-109⑤ 已裁「5 不停」): p1_3_vol_p50 /
  pullback_limit / mfe_stall — 无干净 shadow-only 开关、负载 1.1MB/周, 停采须改生产码,
  风险>收益; 本批不重开该案。

---

## 三、本批落地动作

### A. 预注册判据的机械结算 (判据原文照抄执行, 零阈值改动)

| 项 | 判据读数 (实跑) | 处置 |
|---|---|---|
| **ue_midday_shadow** | 10 交易日 n=41: 命中率 **22.5%** (门 55%), 均值 **−0.78%** (门 +0.5%) ⇒ 双 FAIL | **退役** (判据自带处置: 删 cron + 开关 false) — 停掉每日 12:30 ET LLM 扫描 (唯一停掉的日频 LLM 空耗) |
| **reentry_quick_tp_gate** | 窗 9/1–9/30 已闭; 13 行 reentry 实弹 **tp_touched=0/13** ⇒ 判据轴 n=0<10, 落「否则」分支 | **KILLED 停采**; 门禁维持 (零动作); quick_tp 参数物理零触发 = 死参数 (删除提案见 §五) |
| **cluster_concentration** | 44 日: cap3 blocked=29<30 ✗, 且被拦合计 **+$433** (拦的全是盈利单, 方向反) ✗ | **KILLED 停采** — 不立簇帽 (0/3 门) |
| **conflict_shadow** | 36 事件/25 日: 翻转仅 5 (成交 2, 结局不可判), 频率 ≈0.2/日 | **KILLED 停采** — 候选端排除无证据; 执行侧方向感知防御不动 |
| **ue_earnings_clue** | write-only 38+ 天: 零判据/零 eval/零读方 (grep 实测唯一引用=写方) | **KILLED 停采** — write-only 纪律 (数据留档, 重开须先立判据) |

### B. SSOT 补登记 (修复「在采但注册表查无」)

新增 5 条注册项 (62→**69**): `shadow_portfolio` (PERMANENT_MEASURE — 全门反事实 P&L
地基, 结算 Hermes 08:10, 消费方 ic_weekly_report)、`ue_midday_shadow` (KILLED)、
`cluster_concentration_shadow` (KILLED)、`conflict_shadow` (KILLED)、
`ue_earnings_clue_shadow` (KILLED)。

**premarket_peer_shadow 重归类** (不改采集): 判据面 (峰群确认→跟随) 7/22 已证伪
(−8.48%), 注册表从「NO_CRITERIA 待立 DEB」改为 **KILLED + 采集保留** — jsonl 现角色
= UE 盘前 clue `_gather_peer_surge` 的唯一数据源, 摘 cron = 改 UE 交易行为超授权
(与 universe_expansion_shadow 9/26 先例同型: 判据退役 ≠ 采集退役)。
`_CONSUMER_ACK` 四条新认领逐条写理由 (含 `eth_ext_entry_prefilter` 写方残留, 测试基线同步)。

### C. 待裁面清账 (7 项机械读数 + top_movers 终裁入 ADJUDICATED, 8 → 0)

当日实跑/直读产物 (判据未改, 只登记读数 + 重看窗):
- `ue_tier_gate`: 19/15 日, 被拦仅 7 (fwd 可评 2) ⇒ KEEP_SAMPLING。
- `ue_quote_freshness`: 14/10 日, P95=1283.9s ≫ 90s ⇒ NOT_READY (激活会误杀, 维持不激活)。
- `fee_gate_shadow`: n=64, blocked 子集 n=2–4 且更差 ⇒ 方向对但样本小, 续攒至 10/11 终裁窗。
- `trail_lock_pyramid`: 22/40, 中位 −0.28, Σ−$13.63 ⇒ KEEP_SAMPLING (负向但 n 未满)。
- `rfc104_eth_event_plan`: 7/7 日, 覆盖率全 PASS 仅 3/7 ⇒ NOT_READY, 阶段1 不立项。
- `ext_entry_exec_price`: 546 轮/5 日四点首读全出 (①中位 46/12/2; ②$5→$50 缩减
  73.9%; ③EXT 成交 1 笔 entry<$50 占 0; ④价池∩成交=ACN 10/01) ⇒ 续观至死线 11/30。
- `reentry_quick_tp_gate`: 见 §A (终态)。
- `futu_channel_top_movers`: 9/21b 已实测否掉「冷却榜前10」(top10 11 笔 +$15.20/笔,
  10/11 与 kr3 would_block 同批) ⇒ 2026-10-03 用户确认建议③ — **SUPERSEDED 并入
  kr3 轴**, 不新增门/影子; 横截面 IC 仅作解读佐证; 采集维持 true。

**清账后待裁面归零** (`needs_user=True` 项 0; `shadow_board --push` 判定无待裁/停摆)。

### D. 推送收口 (无料不推, 尺子坏了 fail-open)

- `scripts/shadow_board.py --push`: 新增推送门 — 仅当 ①待裁面非空
  (needs_adjudication) 或 ②看板守卫告警 (生产者停摆/缺产物/注册面缺失) 才推;
  否则全文照常落 `logs/shadow_board.log`, 飞书不推。查询异常 ⇒ fail-open 推送。
- `ops/hermes/entry_delay_shadow_push.py` (D2' 日报): 双面 (entry_delay_confirm 与
  D案 ADR-035 机械判决) 均处「已读·自动续观」且无陈旧 ⇒ 空输出静默 (Hermes 不推);
  任一面到期/待裁/传感器陈旧 ⇒ 照常全量推送。已同步部署 `/root/.hermes/scripts/`。
- `ue_short_boost_shadow` 停摆阈值 4→8 天校准 (事件驱动写方, 历史最大自然间隙 11 天,
  4 天阈会周期性误报 = 日推噪声来源)。

### E. cron×注册表审计 (写方≠评估方, 逐项核 data_path/消费链)

- 扫描口径: 活跃 crontab 中 shadow-ish 脚本 41 个; 按 `eval_script` 直配会得到
  23 个「未匹配」, 但多数是写方/采集方与评估方分离的假阳 (例如
  `capital_flow_daily_aggregate.py` 实为在册 `futu_channel_capital_flow` 的日级聚合
  采集/消费方; `ue_tier_shadow_eval.py`、`kr3_runup_shadow.py`、`rth_fade_shadow.py`
  同理)。
- **补登记 7 条, 注册表 69→76**:
  - **终态 5 条**: `premarket_reprice_shadow` (58 settled, 无可复现净改善)、
    `regime_divergence_shadow` (尺子失真且无消费者)、`reentry_gate_counterfactual`
    (输入源 9/17 已停)、`direction_counterfactual_ruler` (原问题已答且无读者)、
    `t5_hold_counterfactual` (507 笔 Δ−$22.48/笔不支持摆动且无读者)。
  - **留观 2 条**: `da_true_reverse_shadow` (RFC-125 阶段0, 2 事件, 健康轴另行核对)、
    `ue_confirm_gate` (26 日/135 裁决, PRICE_IN/CONFIRM 双方向未过, 10/31 重看)。
  - **同轮收口**: `eth_ext_entry_prefilter` 判 SUPERSEDED (5/30 且 9/18 后停采,
    同人群价格轴由 `ext_entry_exec_price_shadow` 接管); `ue_short_boost_shadow`
    登临时读数 54/60、10/10 重看, 避免到门前弹回待裁面。
- **注释 5 条空转 cron** (备份
  `data/crontab.backup.20261003_shadow_audit.txt`): regime 分歧率、盘前重定价、
  方向反事实尺、T+5 持有反事实尺、证伪门反事实尺。数据/脚本全留档, 仅停调度。
- 台账同步: `docs/CONTROL-LOOP-LEDGER.md` 新增 5 条冻结归档行;
  `docs/references/crontab-loop-baseline.txt` 同步删除 5 条陈旧豁免。

### F. 配置/cron 变更 (全部热重载或独立 cron, 无需重启)

- `config/unified_config.yaml` 5 键置 false (各带墓碑注释+回滚路径):
  `reentry_quick_tp_shadow_enabled`、`cluster_concentration_shadow_enabled`、
  `conflict_shadow_enabled`、`midday_shadow_enabled`、`clue_earnings_shadow`。
- crontab: 午间扫描行 `30 0 * * 2-6 ... scan --midday` 注释退役
  (备份 `data/crontab.backup.20261003_midday_retire.txt`); 另有 §E 的 5 条空转环
  注释退役 (备份 `data/crontab.backup.20261003_shadow_audit.txt`)。
- 开关读取路径实测: 全部经 `ConfigContainer`/`PlaybookConfig` (热重载失效链已覆盖),
  初判无需重启 (app/ 内有其他 agent 在途 WIP, 按军规不顺带上线)。
- 11:20 用户令重启观察; 已先核零未平仓/在途出场且 app/ 在途改动只剩本批
  `shadow_registry.py` 与 `scripts/shadow_board.py`, 遂按军规执行
  `systemctl restart ftseek-us` (11:19:38 停、11:20:08 起, PID `3776834`)。

---

## 四、不动清单 (明确不碰, 防下一个 agent 重做)

| 对象 | 不动理由 |
|---|---|
| universe_expansion capture / premarket_peer 采集 / top_movers 采集 | UE 生产输入 (摘=改交易行为超授权); top_movers 另有 9/21 用户裁「继续采」(覆盖是 ③ 轴瓶颈) |
| p1_3_vol_p50 / pullback_limit / mfe_stall 终态写盘 | 9/26 RFC-109⑤ 已裁「不停」(无干净开关/负载小); 重开须新证据 |
| exit_param / early_dead / penetration 等 10/21–26 重看窗项 | 判据已读未到期, 到期自动回待裁面; 提前裁 = 改判据 |
| 影子组合 (shadow_portfolio) 本体 | 全门反事实地基, 消费链活; 本批只补登记不加门 |

---

## 五、剩余提案 (无待裁欠账)

1. **quick_tp 参数删除提案 (L3)**: reentry 影子窗满结算 0/13 触达 ⇒ 该参数在现行
   min_tp_rr=1.5 门禁下物理零触发 (死参数)。建议走 DEB 删除 (清 dead code); 本批
   仅提案未执行 (见 `docs/debates/DEB-20261003-reentry-quick-tp-deletion.md`)。

---

## 六、验证与守卫

- 测试: `test_shadow_board_guard` / `test_shadow_artifact_consumers` /
  `test_shadow_intent_uniqueness` / `test_ops_scripts_sync` /
  `test_reentry_quick_tp_shadow` / `test_adjudicated_note_anchor` /
  `test_pending_adjudication_report` / `test_criteria_registry_visibility` /
  `test_ue_evidence_citation` / `test_control_loop_ledger` /
  `test_shadow_mount_sentinel` / `test_true_reverse_shadow` /
  `test_settle_ue_confirm_gate` / `test_ue_confirm_gate` /
  `test_ext_entry_prefilter_shadow` / `test_ext_entry_exec_price_shadow` —
  **178 passed**。
- 注册表哨兵: `check_mounting` 1 条存量告警 (ticket_size 产物 26 天未更 — read-only
  按需计算器, 本批已挂 eval_on_demand, 超龄告警为已知口径债); `check_artifact_consumers`
  / `check_intent_uniqueness` 全绿。
- 重启观察: `startup_smoke_check.py --wait 90` 7/7 全绿 (启动段 11:20:13;
  ConfigValidator/漂移/热重载/SSOT/模拟盘/退出快循环/零 Traceback-CRITICAL);
  服务 `active`, PID `3776834`, `NRestarts=0`; 未平仓 `exit_time IS NULL` = 0;
  重启窗口零 `ERROR/CRITICAL/Traceback/Exception`。
- 终态复核: 注册表 76 项, `pending_adjudications=76` 且
  `needs_adjudication=0`; `check_mounting` / `check_artifact_consumers` /
  `check_intent_uniqueness` 均 0; `shadow_board` actionable 推送门为 False。
- cron×注册表审计后: `check_artifact_consumers=0`,
  `check_intent_uniqueness=0`, `test_control_loop_ledger` 10 passed。
- 小额欠账: D2' `entry_delay_shadow_push.py` 完整实跑 exit=0 且 stdout/stderr 均
  0 字节; `ticket_size_breakeven.py` 已重跑刷新产物, `check_mounting` 归零。
