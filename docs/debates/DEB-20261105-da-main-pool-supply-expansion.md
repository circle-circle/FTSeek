# DEB-20261105 · DA 主池供给扩容与 entry_confirm 配套分账

- 状态: 首个 `--top 8` 生产批次已验收；后续 entry_confirm/净盈亏攒样
- 触发: 用户 2026-11-05「另外的AGENT已经启用了entry_confirm的逻辑策略，请基于此给出合理且可行的优化补充，并且合理提升供应交易标的量」
- 类型: 既有 DA 主链的候选预算变更 + record-only 血缘补强；不是新入场假设、不是新影子
- 关联: RFC-158、DEB-20261105-nomination-lift-counterfactual、OKR-2026-11 C1

## 1. 先界定不能做什么

现役 `entry_confirm` 保持 `120s + 20% holdout + mid/grace=3+2`，FADE_SHORT chase cap 继续关闭。
本批不改延迟、判尺、grace、holdout、signal_gate、ATR/价格资格、entry band、chase/admission、
仓位、组合预算、止损或出场，也不绕过任何一道门。扩容发生在它们的上游，新增候选必须走同一全链。

未提名反事实首跑的 T+5 提名−占位差为 **−0.6034pp**，95% CI **[−1.2514,+0.0303]**，并不支持
“把全部 `supply_placeholder` 放行”。因此本批只扩主池中已经通过相同资格筛选、按 conviction 排序的
后续三个名额，不把“标的少”误当成“放宽质量门”的理由。

## 2. 预算证据与变更

| 证据日 | 主池合格选择 | 主池预算外合格候选 | 含义 |
|---|---:|---:|---|
| 2026-10-30 | 7 | 13 | 存在可补位候选 |
| 2026-11-03 | 5 | 0 | 扩容不保证每日都有增量 |
| 2026-11-04 | 7 | 7 | 下一顺位确有真实截断 |

变更仅为 DA main carrier 的 `--top 5` → `--top 8`。现有选择公式是 `main_pool_budget=top_n+2`，
故主池深分析上限 **7→10**，每日新增 **0–3 只**。watchlist、UE≤3、盘前异动≤2 的既有规则不变。
2026-11-04 全批 33 只深分析耗时 1964.2s；固定 5 workers 下按同一吞吐线性估算，+3 只约增加
178s（+9.1%），仍低于既有 08:57 ET 截止与 finalize 兜底。该数是容量估算，不伪称实测。

## 3. 增量必须独立分账

DA skill 在候选选择后生成 `supply_selection`（schema=`da_supply_selection_v1`），并透传到：

1. `da_gate_decisions.jsonl`：覆盖 SKIP/BLOCK 等未形成交易指令的结局；
2. `opportunity_scan_<date>.json` 的 `candidates[]`；
3. `playbook_instructions.raw_instruction_json.supply_selection`；
4. checkpoint 命中分支：每轮重新挂载本轮批次/顺位，不复用旧归因。

主池第 1–7 顺位标为 `main_pool_base_r1_7`，第 8–10 顺位标为 `main_pool_expansion_r8_10`，
其他来源标为 `non_main_pool`。字段同时保存 `screen_batch_id/stage/selection_rank/main_pool_top_n`。
后续可按 decision_id 与既有 `entry_confirm_forward`、成交和净盈亏表连接，不需要新影子或平行交易链。

## 4. 验证与生产验收

- 聚焦回归: DA 候选选择 + main carrier **99 passed**；Python 编译与 diff 检查通过。
- 变异钉: 同一 fixture 下 top8 的前 7 顺位与 top5 逐字相同，新增恰为第 8–10 顺位且标签可分；
  去掉 checkpoint 重挂或三处持久化任一，契约测试失败。
- 部署: `ops/hermes/da_main_carrier.py` 与 `/root/.hermes/scripts/da_main_carrier.py` SHA256 一致；
  standalone 下一新进程生效，**不重启 `ftseek-us`**。
- 首个生产批次须核: 事件 `main_pool_top_n=8/main_pool_budget=10`；主池 selected≤10；
  新增 gate/bridge/DB 行的 cohort 与 batch/rank 完整；总批在既有截止前完成或走既有 finalize。
- 工程回滚条件: 扩容后发生新的截止违约、行情/LLM 配额故障或血缘缺失，先回退 top5 再归因，
  不靠提高 timeout 掩盖容量问题。

## 5. 经济读数边界

首个复盘日为 2026-11-30，分别报告 base/expansion 的深分析数、可交易指令数、触发数、entry_confirm
treatment/holdout 分配、成交数、毛/费/净 bps 和日聚类 MDE。增量成交 n<30 时只报描述性读数与功效，
不得据少数盈亏继续加到 top10，也不得据单日亏损回撤。扩容只增加合格研究供给，不承诺成交数或收益。

复现查询（替换日期）:

```sql
SELECT json_extract(raw_instruction_json, '$.supply_selection.cohort') AS cohort,
       COUNT(*) AS instructions,
       SUM(CASE WHEN direction IN ('BUY','SELL_SHORT') THEN 1 ELSE 0 END) AS actionable,
       SUM(executed) AS executed
FROM playbook_instructions
WHERE date='2026-11-05' AND decision_id LIKE 'DA-%'
GROUP BY cohort;
```

回滚: 两侧 carrier 的 `MAIN_POOL_TOP_N` 从 8 改回 5 并重新同步；skill 的血缘字段可保留，零交易语义。

## 6. entry_confirm 直接血缘与冬令时时间契约

`entry_confirm_gate` 已把同一个 `supply_selection` 继续写入 holdout、CONFIRMED、ABANDONED、
EXPIRED、弃单后 +5m/+10m 记录及重启恢复状态。旧指令或坏 JSON 回退 `{}`，不改变 arm、
确认、放弃、下单或 SKIP。由此 base/expansion 可直接在门内比较 treatment/holdout 分配、
确认率、弃单和后续净盈亏，不必等日后再猜批次。

本批没有调整 `delay/ruler/grace/holdout`：现役 cohort 仍处首窗，R1、mid 降级和 holdout 子群
失衡都应先看既有前瞻样本，不能再做阈值购物。确认后未成交继续由既有 resolution coverage
和成本报告暴露，不能把未成交伪装成零亏损样本。

冬令时的时间轴固定为：

| 环节 | ET（EST） | 北京（CST） | 作用 |
|---|---|---|---|
| DA 深分析首批 | 08:00 | 21:00 | 先扫描、分析并写 bridge/DB |
| DA finalize 截止 | 08:57 | 21:57 | 给 09:00 生成预留 bridge |
| Playbook 生成开始 | 09:00 | 22:00 | 二次过滤与最终指令生成 |
| 常规入场最早时刻 | 09:35 | 22:35 | 开盘 blackout 后触发，之后才 arm entry_confirm |

因此“09:00 首批”对应 **Playbook 生成**，不是 DA 深分析。若把 DA 推迟到 09:00，按近期
27–45 分钟耗时会让 bridge 落在 09:27–09:45，既错过 09:00 生成锚，也会压缩 entry_confirm
的开盘 cohort；当前不采纳这种改法。近期生产记录也显示冬令时 11-02/03/04 均为 08:00 EST
启动，并非根据月份粗略加一小时。

验证: 新增血缘覆盖 holdout/treatment/restore/expiry/abandon/post-watch；entry_confirm
三套聚焦测试 **95 passed**。按规程重启后冒烟 **7/7**，COIN/ALB 持仓不变、非 BOOKED
出场为 0，实读配置仍为 `120s/20%/mid/grace=3+2`。

## 7. 首个 top8 批次的早段纠偏

2026-11-05 08:00:26 EST 首个生产事件已实读 `main_pool_top_n=8/main_pool_budget=10`。
首只 LLM 落盘前的代码复核发现 gate 台账新增字段误写为 `a.get(...)`；该变量只属于后续
bridge/DB 循环，gate 当前作用域必须使用 `analysis.get(...)`，否则整行 gate JSONL 会因
NameError 被 best-effort 守卫跳过。原静态测试误把三处都钉成 `a`，没有识别这个作用域差异。

08:02 ET 处置时仍在 Stage 1 市场扫描，checkpoint=0、LLM 分析=0。通过 Hermes 正式
cancel 原 execution（03d2566118e04144be6df067713974f6）并 direct 重派，08:03:44 新事件
仍为 top8/budget10；没有取消或重启主服务，没有重复 LLM 成本。测试改为明确钉住
bridge/DB 两处 `a` + gate 一处 `analysis`，相关回归 **177 passed**。

实际主池满额 10 只，第 8–10 顺位为 EPAM/WPM/TIC，整批深分析 36 只（上日 33），
资格拒绝与预算外规则未放宽。首只 HCA 已于 08:17:41 写出 gate：`selection_rank=1`、
`cohort=main_pool_base_r1_7`、batch/budget 完整，无 Gate/IC 写入错误；其 snapshot 同时保留
4 个结构化 bull/bear claims 与 debate。EPAM/WPM/TIC 三个 expansion gate 均已在
08:21–08:22 落盘，cohort 全为 `main_pool_expansion_r8_10`，分别含 5/4/3 个结构化 claims；
方向/结局为 BUY+PASS、SKIP+BLOCK、BUY+WARN，证明扩容不等于强制成交。
完整批次于 08:35:26 EST 完成，早于 08:57 截止：36/36 分析、18 条 bridge/DB、
missing/unexpected/gate_unknown 均为 0；36 条 signal snapshot 全为富对象、无数组退化。
EPAM 与 TIC 两只可发布 expansion 候选均以同一 decision_id 写入 bridge 和
`playbook_instructions.raw_instruction_json.supply_selection`；WPM 为 SKIP，未强行供单。
09:06:50 Playbook 自然生成完成 80/80；EPAM/TIC 的 claims 与 supply cohort 均继续保留。
该纠偏只恢复预期记录，不增加或改变任何交易判断。

## 8. 09:00 推送的 1/18 可交易如何解释

首批推送为 bridge 18、DB 可交易 1/18、guardrail SKIP 17；Playbook 总跳过 78 中，
61 是 `非当前通道候选` 熟股/观察占位（DA=0），不是 61 只 DA 被拦。其余 17 恰好全部为
DA 的 `admission: NOTIONAL_TOO_SMALL(<5500)`，没有 entry_confirm、双 LLM 或血缘故障。

该结果与现役 sizing 算术一致：实时权益约 $99.2k × risk_pct 0.5% × CAUTIOUS 0.5
≈ $248 风险预算；止损宽度 4.5–5.5%（中位约 5%）⇒ 名义约 $4,500–$5,500，
17 只实际为 $4,063–$5,418（中位 $4,944.96），均低于既有 $5,500 地板。
TIC 止损 4.49%、名义 $5,521.20，恰好过门；EPAM 为 $4,944.96，因此拒绝。

结论是**机械正常，但不等于供给目标已经转化为成交供给**：扩池增加了合格研究候选，
实际可交易数量仍受 `risk_pct × regime_mult × stop_distance × min_notional` 联合约束。
不得因单日 1/18 临时降低地板或改 CAUTIOUS/止损；P3 已有连续 10 个交易日日均 <3 的
回滚判据，应按既有结算评估，任何调整另立实验。

另需诚实披露：名义门不等于完整费用门。TIC 虽过 $5,500，但 516 股的估算往返费
$13.63/24.7bps；被拦 EPAM 为 $4.31/8.7bps。因此不能用本批宣称“名义门筛出了费用更优单”。
本批不新增 fee_bps 阈值或改 `min_profit_fee_ratio=0`，避免夹带第二个行为变量。

## 9. 用户复核纠偏与费用门替换的预注册口径

用户随后明确指出「显示是只有 1 只可交易的标的股吗？这个不正常」。该质疑成立：§8 的
「机械正常」只说明结果能被现行公式解释，不能说明它符合本批“合理提升实际交易供给”的目标。
准确表述应为：**公式可解释，但相对供给目标属于结构性异常**。当时只有 TIC 一条可交易指令，
`triggered=0/executed=0`，并非已有一只成交。若不扩容，base 候选会全部撞名义门，今日将是 0 条；
因此扩容不是病因，病因是 `risk_pct × CAUTIOUS × stop_distance` 的 sizing 落点与名义地板冲突。

不得通过抬高股数硬凑 `$5,500`：那会在止损不变时同步抬高单笔美元风险，且掩盖真正的费用问题。
本节的唯一候选行为变更是**把名义额代理门替换为事前真实费用率门**，不改仓位、止损、CAUTIOUS、
entry_confirm、候选资格或主池预算。阈值在读历史结局前固定为 **11.54bps**，直接引用已批准的
P3 经济门 J2（`docs/decisions/DEB-20261031-budget-design.md`），不按今日可放行只数选择 10/12/20。
这只是把既有车队费用约束转成逐笔可执行判据，不宣称 11.54 本身具有预测 IC。

回放前写死以下口径：

1. 人群仅为 `source_type=PLAYBOOK_SIM`、`raw.source=deep_analysis` 的已平仓交易；35/60 日两窗并列，
   不混 EXT/UNKNOWN/UE，不用今日尚未触发或成交的指令做盈亏证据。
2. 对照为现行 `notional>=5500`；候选为 `min_notional=0 + ex_ante fee_bps<=11.54`，费用与生产
   `admission` 同源调用 `fee_model.estimate_round_trip`。两窗均报告保留/剔除数、净额、笔均与费用率；
   净额只是描述性反事实，不作资金复用或盈利预测，显著性结论仍需日聚类与 MDE。
3. 采用条件：今日 18 条在**原股数/原止损**下可增加可交易供给，且被新增放行者费用率确不高于
   11.54bps；历史两窗不得出现候选相对现行门净额同时更差。若不满足，保留现行门并只交付审计。
4. 上线后按 base/expansion、entry_confirm treatment/holdout 分账；首个复盘日仍为 2026-11-30。
   工程回滚为 `max_fee_bps=0/min_notional_usd=5500`，一次恢复旧经济门，不动其他三键。

## 10. 回放结果：费用门替换未通过，不部署

按 §9 冻结口径实际回放，结果如下（PLAYBOOK_SIM + deep_analysis 已平仓；净额为描述性
反事实，不是盈利预测）：

| ET 窗口 | 现行名义门保留 n / 净额 / 笔均 | 费用门保留 n / 净额 / 笔均 | 新增放行 n / 净额 | 新增拦截 n / 净额 |
|---|---:|---:|---:|---:|
| 35 日 | 136 / −$98.18 / −$0.72 | 139 / −$719.93 / −$5.18 | 16 / −$405.74 | 13 / +$216.01 |
| 60 日 | 189 / +$252.86 / +$1.34 | 201 / −$685.58 / −$3.41 | 29 / −$576.69 | 17 / +$361.75 |

候选确实把平均事前费用率从 **9.06bps→7.55bps（35 日）**、
**8.85bps→7.56bps（60 日）**，说明“真实费用门降低费用”在算术上成立；但两个窗口的
保留净额都显著差于现行门，触发 §9 的不采用条件。此处不补做事后价格分桶或另选阈值来挽救
提案，也不把净额差解释为具有统计显著性的 Alpha；样本只足以否决本次直接替换。

今日 18 条纯描述性重算中，候选会保留 14 条、拦截 ADMA/RIVN/RUN/TIC 四条高费用指令；
**14 只是反事实数量，不是线上已放行数量，更不是成交数量**。线上仍为 1 条可交易、0 触发、
0 成交（截至 09:29 EST 查询）。生产未加入 `max_fee_bps`，`min_notional_usd` 仍为 5500，
未改仓位/止损/CAUTIOUS/entry_confirm，也无需为本只读回放重启。

保留的落地优化因此只有 §2 的上游主池 7→10 和 §6 的 entry_confirm 血缘；它们增加合格研究供给、
保证增量可审计，但不承诺每日可交易数。若 1/18 类情况持续，仍按 P3 已预注册的连续 10 日 R1
结算，不提前伪造“已触发回滚”。当前 `budget_p3_latest` 为 4 个 ET 日、34 笔、日均 8.5，
`rollback=none/state=KEEP_SAMPLING`；今日单日异常与累计未触发回滚可以同时为真。

复现入口：`scripts/admission_replay.py::load/blocked`；原始只读输出冻结于
`/tmp/da_fee_gate_replay_20261105.json`（临时证据，不是新增生产影子或注册表事项）。

## 11. 用户裁决恢复供给启用：知情破例与新 cohort

用户在阅读 §10 后进一步明确裁「能否恢复供给启用」。本轮据此执行**供给优先的知情破例**，
不再把 §9 的历史净额采用条件当作阻止上线的理由；同时完整保留 §10 的负面证据，绝不宣称
本次调整已经证明改善净盈亏。新旧 admission cohort 自 2026-11-05 部署点分开，后续报告不得混算。

最终启用的组合为 `min_notional_usd: 5500→2500` + `max_fee_bps: 0→11.54`。2500 是 P3 前既有
地板，11.54 仍是 §9 冻结值，不重新选参。相较 §9 的 `min_notional=0` 候选，2500 继续拦住极端小单；
在历史 35/60 日样本与今日 18 条上，该组合与已回放的费用门结果相同，因此没有隐藏新的回放口径。

真实费用率在生产 `admission.evaluate` 中与 `fee_model` 同源计算，命中记为
`FEE_BPS_TOO_HIGH(actual>11.54)`；LIVE 1 股工程探针仅豁免该项与既有名义经济性门，价格、止损、
股数及其他安全判据不豁免。执行前 re-price/re-size 仍过同一门，组合在险 4%、止损宽度 8%、
仓位公式、CAUTIOUS、entry_confirm 与出场均不变。

按今日旧 batch 原股数/原止损只读重算，新规则为 **14 PASS / 4 BLOCK**：ADMA 21.2bps、RUN 30.0bps、
TIC 24.7bps、RIVN 19.1bps 被拦，其余 14 只费用率为 7.8–10.5bps。该清单只验证新规则，不回写数据库。
今日已落库的 17 条 SKIP 不追溯改成 BUY/SELL_SHORT；现有 TIC 如后续进入执行复核，则按新费用门处理。
因此本次启用恢复的是**后续新写入指令的供给**，不是伪造今日已经恢复 14 条或已经成交。

配套口径：`budget_p3_eval` 增加 `FEE_BPS_TOO_HIGH` 与新旧 cohort 警示，原 P3 J3 不沿用于新 cohort；
`fee_gate_shadow_report` 同步现行费用门；一次性 Hermes 读数任务 `2a80c016ffd6` 已加前置口径，
要求 10-31～11-04 与 11-05 起分账。回滚为 `min_notional_usd=5500/max_fee_bps=0` 并重启，
其他预算键不回滚。部署测试结果与冒烟回执追加于本节末尾。

部署回执（22:49 CST）：统一聚焦回归 **579 passed**，registry provenance **4 passed**；
日志卫生另有 1 个并行 exit 文件基线红，失败点均不在本批文件，未顺带修改。重启前 MCD 一仓、
`exit_intents` 300/300 BOOKED，最新提交早于当时运行进程。`systemctl restart ftseek-us` 后
PID 2006777、active，启动冒烟 **7/7**，启动段无 ERROR/CRITICAL/Traceback；实读
`min_notional=2500/max_fee_bps=11.54/max_open_risk=4.0/max_sl=8.0`。MCD 持仓不变、非 BOOKED=0，
今日旧 DA 行仍为 1 actionable/17 SKIP、triggered/executed=0，证明没有追溯改写。

## 12. 用户明确授权今晚恢复旧批落库（已执行并经自然交易链验收）

用户随后明确要求「我需要今晚恢复落库的指令，可以不做新推送，确保今晚可以正常按最新链路交易」。
这项新指令取代 §11 的“不回写旧 batch”边界，授权对今晚既有行做一次性恢复；不新增恢复推送、
不重跑生成器、不手动强制下单。

使用 `scripts/restore_da_admission_instructions.py` 先 dry-run、后 `--apply`。脚本仅处理当日 DA 真指令，
且每行必须同时满足 `triggered=0/executed=0/PENDING/execution_attempt=0/无 broker_order_id/
无 entry_confirm 状态`；`BEGIN IMMEDIATE` 内重算并 CAS 更新，任何竞态或行数异常整批回滚。
原始方向来自 `raw.admission.original_direction`，不是把 SKIP 一律改成 BUY。每条保留
`admission_history` 与 `metadata.admission_restore`，claims、debate、supply_selection、created_at、
触发/成交/执行尝试字段均不改。

09:58:54 EST dry-run 为 18 条变更、14 actionable（11 BUY/3 SELL_SHORT）、4 高费用 SKIP；
apply 实际结果完全一致。ADMA/RIVN/RUN/TIC 记录 `FEE_BPS_TOO_HIGH`，其余 14 条恢复。
完整行前 18 行备份：`data/audits/da_admission_restore_2026-11-05_2026-11-05T095854.664964-0500_before.json`
（SHA256 `4652858295ce186d1c45bbfdd657c28ad5cafaec96d961f12f77b3f9ba4b5640`）；
结果审计：同目录 `..._result.json`。脚本及幂等/安全回归与 admission 契约合计 **26 passed**。

恢复后 14 条全部通过执行前 admission 只读复检，entry_confirm 均未 arm、血缘无缺失。没有重启服务，
后续自然调度直接读取 DB：截至 10:06 EST 已有 7 条触发，COIN/CAT/LLY/LMT/MRK 等接受既有
chase/VWAP 门复评。GOOG 于 10:01:57 arm，10:04:23 因反转持续 `ABANDONED`，
`entry_confirm_forward` 含完整 `supply_selection=non_main_pool`，未下单；CRM 随后自然 arm。
这证明“落库→触发→re-price→chase/VWAP→entry_confirm→forward 血缘”真实运行，不是只改展示计数。

截至 10:06 EST 动态状态为 10 BUY/3 SELL_SHORT/5 SKIP、triggered=7、executed=0；GOOG 从恢复的
14 条中按策略正常弃单，不把当前 13 条可交易误报成恢复失败。服务 active、启动后无 ERROR/CRITICAL/
Traceback，MCD 持仓不变、`exit_intents` 300/300 BOOKED。未等待或伪造一笔成交来证明链路可用。
