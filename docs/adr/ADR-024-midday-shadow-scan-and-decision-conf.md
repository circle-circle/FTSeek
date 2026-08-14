# ADR-024: UE 午间影子扫描 + playbook_instructions.decision_conf 口径对齐 (DEB-20260814, 8/14 用户裁决)

- 状态: Accepted (午间=影子攒样期; P6=口径列已上线)
- 日期: 2026-08-14
- 关联: DEB-20260814-da-prompt-gate-bottleneck §九/§十/§十三/§十四, PROPOSAL-20260811-ue-streak-decay (判据同构)

## 背景

两项同属 DEB-20260814 的用户裁决落地, 记录于同一 ADR:

**① 午间影子扫描 (§十三)**: 现行事件扫描时刻表 DA 07:00-07:28 ET → UE scan+inject
09:10 ET → 开盘 09:30, 09:10 至次日早间约 7 个交易小时无任何事件扫描 — 午后 8-K
(实测 8/13 17:00 后仍有 ADIL CEO离任/VERI 重组等硬事件) 与午间新闻全部漏掉。
时序审计结论: DA/UE 现有时刻各有最优输入窗, 先后顺序不动, 唯一真实空白 = 盘中。

**② decision_conf 口径对齐 (§十 P6, 根因 §9.1-修正2)**:
`playbook_instructions.confidence` 列写的是 LLM 语义 conf 或被 WARN 降权后的值,
同 tier 落库 conf 不一致 (实证: 8/7 九条 gate=优/良指令落库 conf=50, 8/10-8/11
两条落库 70) → 下游按 confidence 分桶的任何分析都有测量误差。

## 决策

**① 午间影子扫描**: crontab `30 0 * * 2-6` (北京 00:30 = ET 12:30, 交易日) 运行
`llm_universe_scan.py scan --midday`。只 scan 不 inject, 零污染隔离:

- 独立落盘 `data/shadow/llm_universe_scan_midday.jsonl` (`scan_window="midday"`)
- 跳过 R5 signal_snapshots (decision_id 与早间同键, UPSERT 会覆盖污染早间快照)
- 主链路消费方全部只读主 jsonl (无 glob), clue brief/self-critique/streak 全复用
  保证与早间数据可比

判据预注册 (`scripts/ue_midday_shadow_eval.py`, 与 PROPOSAL-20260811 同构):
≥10 交易日 + tradeable n≥30 (symbol+date 去重), 12:30→当日收盘方向调整收益,
命中率 (≥+0.5%) ≥55% 且均值 ≥+0.5% → 开启午间 inject (带全套既有守卫:
$5 价格地板/借券检查/DROP 源头过滤); 否则退役 (删 crontab 行 + 开关 false)
零残留。裁决窗口 ~2026-08-28。

**② decision_conf 独立列**: `playbook_instructions` 新增 `decision_conf REAL`
列承接 gate 校准值 (0-1), `confidence` 列语义零改动 (下游 `_conf_factor` /
conf<50 分歧代理等活消费不受影响), UPSERT COALESCE 防老格式洗 NULL。
writer 在外部 DA 链 (`~/.hermes/skills/deep-stock-analysis`), Qoder §14.3 实证
DA-* 指令已带 decision_conf 落库; 仓内 UE/topup/generate 路径无 gate conf 可写,
列保持 NULL 属语义正确。

## 复查与事件处置 (Qoder §十四)

- 链路隔离经全消费方逐 grep 实证 = 结构保证; 时区链 ET 基准正确; 判据与 §13.3 逐字一致
- 事件①: 生产库 3 行错日期 DA 指令 (手动运行 DA 链, date=运行时 ET 日非目标交易日)
  已备份 `data/archive/phantom_instructions_1031_1033_backup_20260814.json` 后删除,
  根因移交 DA 链属主 (非盘前窗口运行应 date=下一交易日或拒写)
- 事件③: config 注释清扫误删 5 块证据/回滚注释, 已按 HEAD 逐字恢复
- 附带: 评估脚本加盘中守卫 (ET 16:00 前剔当日未成形 K 线样本)

## 回滚

- ① `universe_expansion.midday_shadow_enabled=false` 或删 crontab 行 (零残留)
- ② 列保留无副作用; 如需停写删外部 DA 链 decision_conf 传参即可
