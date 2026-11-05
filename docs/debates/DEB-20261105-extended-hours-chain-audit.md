# DEB-20261105：盘前/盘后交易链路与结果整体审计

## 状态

- 日期：2026-11-05（审计快照 15:40 CST / 02:40 ET）
- 状态：**P1/P2 已实施；首个盘前 cron 活体观察与 P3 到期裁决待办**
- 范围：EXT 盘前/盘后入场、延长时段出场、RTH 接管、durable exit ledger、日报/绩效口径、研究臂 11-13 裁决准备
- 总结：**需要修工程可用性和报表口径；不需要放宽策略阈值，也没有证据支持 EXT 放大或转正。**

## 立案前强制检查

- 同轴既有事项已枚举：RFC-126（EXT admission 仅 record-only）、RFC-149（研究源截至 2026-11-13）、RFC-177（盘前报价可信度 record-only + 延长时段亏损侧不止损）。本 DEB 不新增影子、不重复采集同轴实验。
- 既有裁决已核对：`docs/OKR-2026-11.md` 明确 EXT 小样本只观察、不放大、不转正、不新建门；本审计不建议重开盘前止损、改涨幅/量/财报闸或为追样本延长死线。
- 工作区保护：`app/`、`scripts/`、`docs/CONTROL-LOOP-LEDGER.md`、`handoff.md` 等有其他 agent 在途改动；本审计只新增本 DEB 并追加索引/进度，不顺带部署任何 WIP。

## 1. 链路与账实结论

| 环节 | 当前载体 | 审计结论 |
|---|---|---|
| EXT 入场 | 系统 cron 每 5 分钟调用 `scripts/extended_hours_entry_executor.py --session premarket/afterhours` | 严格臂仍在扫描和成交；研究源批量快照存在真实断供，见 §2 |
| EXT 出场 | 每分钟 `extended_hours_exit_cron_gate.sh` → `extended_hours_exit_cron.py` → 各 collector/executor | durable intent 可跨重启续上；未发现未决出场 |
| RTH 接管 | 09:30 ET 起交 Playbook/RTH 全链 | 保持现状；不得把 RFC-177 已否决的盘前亏损侧止损重新加回 |
| 当前持仓 | `trade_records.exit_time IS NULL` | EXT：US.ALB SHORT 26 @95.48（盘后）；DA：US.COIN SHORT 39 @149.085（regular，不混入 EXT 绩效） |
| 出场账 | `data/playbook_exits.db.exit_intents` | 共 295 条，全部 `BOOKED`，non-BOOKED=0 |

原始 `extended_hours_exit_executions.jsonl` 中 TMUS/MSFT/AMD 的 `ok:false / awaiting_confirmation` 是当时的瞬态留痕；相同 `intent_id` 后续均在 durable ledger 落为 `BOOKED`，且 `trade_records` 已平仓。**报表应以 durable ledger + 成交 DB 为结果 SSOT，不应把原始瞬态行当最终失败，也不应回写篡改历史 JSONL。**

## 2. P1：研究源被单个 OTC 标的整批拖死

### 根因

`scripts/extended_hours_entry_executor.py:1763` 的 `_snapshot_candidates` 对每个批量调用 `get_market_snapshot`；`scripts/extended_hours_entry_executor.py:1777` 在 `ret != RET_OK` 时直接 `continue`：

- 整批候选被丢弃，没有坏票隔离；
- 非异常返回的失败没有错误日志；
- 没有 requested/succeeded/dropped 计数，因此外部只能看到 `research_pool=0`，无法区分“无候选”和“取数失败”。

### 只读实测

2026-11-05 15:40 CST 使用生产同源 Futu OpenD、只读行情接口复现：

| 检查 | 实测 |
|---|---|
| 研究源开关 | `_research_sources_active() == True`，配置死线仍为 2026-11-13 |
| 源供给 | 财报榜 66 只、自选 51 只，合并后按既有上限取 80 只 |
| 坏票 | 80 只中包含 `US.EBKDY` |
| 整批请求 | `ret=-1`，错误为“暂不提供美股 OTC 市场行情 EBKDY”；现有 helper 返回 **0 个候选** |
| 剔除坏票 | 其余 **79/79** 只快照成功，`ret=0` |

旁证：`data/ext_entry_research_funnel.jsonl` 最后一行是 `2026-10-21T20:01:13Z`，此后零产出；近期 `logs/ext_entry.log` 持续为 `research_pool=0`。这不是研究源被主动关闭，也不是源列表为空。

严格臂近期仍有扫描和成交，因此不是 EXT 全链停摆；但同一 helper 也服务其他候选补充路径，存在同类潜伏风险，不能只针对 EBKDY 写死过滤。

### 建议修复

1. 在共享 `_snapshot_candidates` 内做**坏票二分隔离**，可参考 `app/data/providers/fundamental_provider.py:185` 的 `_snapshot_bisect`，不要硬编码 EBKDY。
2. 每批记录 `requested/succeeded/dropped`、失败标的和精简错误；网络/限流类失败不得因二分重试放大请求风暴。
3. 增加 OTC 混合批回归：含 EBKDY 时保留全部正常票，单只失败只丢单只；同时覆盖整批异常、空返回和坏行。
4. 验收以快照成功数恢复为准，不预设 `research_pool` 必须非零；后续涨幅/量/财报闸仍按原规则裁决。

**行为边界**：该修复会恢复研究臂真实入场供给，可能产生新 SIM 成交，属于行为恢复而非纯观测修补。部署前需用户明确批准，一次只改这一变量；不得借修复同时放宽任何闸或延长 11-13 死线。

## 3. 结果：方向性为正，但样本和尾部均不支持放大

绩效统一采用 `PLAYBOOK_SIM + A9 clean + A10`、费后 `pnl_net`，日聚类为“逐笔净 bps 的日等权均值”；MDE80 与 80%CI 同源。费用是模型估计，不是券商逐笔对账。

| 窗口/人群 | n / 交易日 | 净额 | 加权净 bps | 日聚类读数 | 审计判断 |
|---|---:|---:|---:|---|---|
| EXT，2026-10-06→11-05 | 24 / 9 | +$583.78 | +53.06 | 均值 +60.84bps；80%CI [19.71,101.97]；MDE80 82.49bps | 仅描述性读数；n<30 且尾部集中，不放大 |
| 严格臂，2026-09-20→11-05 | 14 / 8 | +$546.65 | +86.63 | 80%CI [28.25,116.43]；MDE80 87.30bps | 相对较好，但仅 14 笔，不能称稳定 edge |
| 研究臂，同期实际样本 | 11 / 2 | +$56.57 | +10.92 | 剔最大单笔后 **-$50.07** | 不满足 RFC-149 转正方向 |
| EXT，2026-11 月 | 2 / 2 | +$9.63 | +13.10 | 无统计意义 | 只报告，不裁决 |
| DA，2026-11 月（对照） | 30 / 3 | +$90.61 | +5.40 | 日等权 +0.097bps；80%CI [-16.10,16.29]；MDE80 24.06bps | n<120，不称 DA 转正，不让 EXT 尾部掩盖主 KR |

尾部与分源复核：

- EXT 最大两笔为 TSLA +$236.81、PEP +$106.64，合计 **$343.45，占总净额 58.8%**。
- 严格臂剔最大两笔后仍 +$251.43，但仍只有 14 笔/8 日，CI/MDE 很宽。
- 研究臂最大单笔 PEP +$106.64；watchlist n=9、净 +$40.37，剔最佳后 **-$66.27**；beat 仅 n=2、净 +$16.20，不能分源转正。
- 11-13 必须按 RFC-149 的 **全样本、剔最大单笔、分 source** 三轴裁决；断供造成的样本不足不得被解读为策略证伪，也不得为凑样本事后改阈值或静默续期。

近期运营背景：11-02→11-04 盘前/盘后文档连续生成；11-04 postmarket 全组合为 total=13、closed=11、已平净 **-$527.60**，ALB/COIN 两笔过夜。该全组合数与 EXT-only 表必须分开，不拼接叙事。

## 4. P2：报表口径需要修正

1. **漏斗把“票次×轮次”当候选数。** `scripts/eth_stage_daily_report.py:102` 直接累加每轮 `n`。11-04 盘前价格拦截 2074 次，实际 117 个独立标的；盘后 1799 次，实际 128 个。应并列展示 `ticket_rounds` 与 `unique_symbols`，真实转化率用独立标的，不用重复轮次。
2. **历史 `--date` 混入当前快照。** `scripts/eth_stage_daily_report.py:261` 读当前持仓，`:293` 读最近日志轮次；查询 11-03 仍显示 11-04 开仓的 ALB。最小修复是明确标注“当前快照，与 --date 无关”；没有 as-of 数据时不得伪装成历史持仓。
3. **小样本警告过宽。** `app/review/stage_stats.py:28` 当前 `MIN_N_FOR_READING=5`，与全仓 n<30 不下结论纪律不一致。应调整为 30，并同步测试；OKR 的 n<120 功效门继续由专门报表承担。
4. **行级样本膨胀。** `data/extended_hours_settle_latest.json` 当前 B21 为 357 行 vs 36 持仓（9.9x）。分钟行只作路径诊断；绩效和策略有效性必须读 `trade_records` 或持仓级聚合。
5. **瞬态出场失败需对账。** 报表若消费原始 exit JSONL，应按 `intent_id` 联接 durable ledger 的最终状态，避免把已恢复的 `awaiting_confirmation` 算成永久失败。
6. **控制台账已陈旧。** `docs/CONTROL-LOOP-LEDGER.md:100` 仍描述盘前/盘后 `tighten` 亏损侧止损在役，与 RFC-177 的 `hold_no_stop` 裁决冲突；应修文档，不改交易行为。

以上均为修尺子/文档语义，不应伴随任何交易阈值、仓位或出场规则调整。

## 5. 不是新 bug，但必须明确政策边界

- **EXT admission**：RFC-126 明确 record-only。PFE/AMD/ALB 出现 `REJECT(NOTIONAL_TOO_SMALL <5500)` 仍成交，符合当前 EXT 独立设计，不是新发现的绕过。应另裁“继续豁免还是转硬”，不得静默把主链 $5,500 下限套进 EXT；转硬会改变 cohort。
- **SHORT 的 earnings 空字段**：ALB 空头只要求事件存在，不要求 beat/miss，空 `earnings={}` 本身不是故障；可补事件日期/相位审计字段，但不改变入场判定。
- **盘前/盘后亏损侧不止损**：RFC-177 已裁决，09:30 起交 RTH；本轮不建议复活止损参数或新增夜数闸。

## 6. 分级执行建议

### P1（需用户批准后实施）

- [x] 修共享批量快照坏票隔离、错误日志和 requested/succeeded/dropped 指标。
- [x] 增加 OTC 混合批及失败路径回归；生产只读探针验证正常票不再被连坐。
- [ ] 单独观察恢复后的研究源供给，不与任何闸值/死线变更同批上线。

### P2（零交易行为变更，可单独实施）

- [x] 日报漏斗增加独立标的口径；历史报告标注当前快照；小样本阈值与测试对齐。
- [x] 绩效只读成交/持仓级 SSOT；原始出场瞬态沿用既有 reconciliation，不重复造报表。
- [x] 修正控制台账陈旧止损描述，不改 RFC-177 行为。

### P3（到期裁决）

- [ ] 明确 EXT admission 继续 record-only 或另立 RFC 转硬，默认不在本批转硬。
- [ ] 2026-11-13 按 RFC-149 三轴结算研究源；若样本不足/剔最佳为负，按预注册处置，不追样本。

## 7. 审计阶段验证与当时未做事项

- 已做：代码/cron/日志/JSONL/SQLite 只读核查；EXT 与 DA 报表复算；11-03/11-04 历史报告对照；Futu 80 只含/剔 EBKDY 只读探针；durable intent 与成交持仓对账。
- 审计当时未做：未修改生产脚本、配置、阈值、数据库或 cron；未重启服务；未运行单元测试（无代码变更）；未切 REAL，SIM 四锁保持不变。

## 8. 执行记录（2026-11-05，用户裁“按建议执行”）

- **P1 代码**：`scripts/extended_hours_entry_executor.py` 的共享 `_snapshot_candidates` 增加坏票二分隔离；连接/限流/异常整批失败不二分放大；每批输出 requested/succeeded/dropped、失败标的和精简错误。未改任何价格、涨幅、量、财报、仓位或死线参数。
- **P1 验证**：新增 OTC 混合批、限流不二分、异常不二分 3 项契约；EXT/日报/周报相关 **328 passed**，目标文件 `py_compile` 通过。生产同源只读探针从修复前 helper=0 恢复为 **requested=80 / succeeded=79 / dropped=1（US.EBKDY）**。
- **P2 代码**：日报并列票次/独立票，历史报告的在场与最近轮次明确标为当前快照；阶段统计小样本门 5→30 并动态渲染；控制台账改回 RFC-177 `hold_no_stop` 描述。11-04 实测盘前 **2074/117、643/25**，盘后 **1799/128、652/25**。
- **既有能力复用**：绩效继续以 `trade_records`/持仓级为准；`scripts/reconcile_extended_hours_exits.py` 已承担券商成交、审计和本地账对账，未新增平行链路。
- **生效范围**：两个 standalone cron 脚本由下一新进程自动生效，无需 `ftseek-us` 重启即可进入 EXT 入场/日报链路。当前 `app/`、`scripts/` 存在其他 agent WIP，为避免顺带部署未重启驻留服务；若驻留复盘模块已加载，小样本文案在下一次安全重启后完全对齐，交易行为不受影响。
- **待观察**：首个盘前 cron 检查 `快照指标` 与研究源日志；只确认取数恢复，不要求研究池或成交必须非零。11-13 仍按 RFC-149 原判据结算，不延长死线。
