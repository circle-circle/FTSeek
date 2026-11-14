# DEB-20261113 — 开盘后全级别日志与 MOM 首个真实锚点核查

- 状态：**主链核查完成；MOM 首个真实锚点判 `PLUMBING_BROKEN`（簿记），仓位补偿已收口，代码/簿记状态待修**
- 核查时点：2026-11-13 09:30–10:18 ET（22:30–23:18 CST）
- 范围：主日志全部 INFO/WARNING/ERROR/CRITICAL、盘前与 MOM 供给日志、退出/PositionSync/健康/事件辅助日志、只读 SQLite 与影子评估器。
- 纪律：零下单、零生产 DB 写、零重启、零代码/配置/cron/推送变更；仅落审计文档。未开启 DEBUG，INFO 落盘不代表 DEBUG 事件不存在。

## 0. 一句话结论

DA 的黑窗、确认、风控、下单、恢复与退出链总体按设计运行，但存在三个不能判“全正常”的断点：MOM 的 XLE/QQQ 两腿均在真实 SIM 成交后因同一个 `thesis=None` 簿记 bug 被迫补偿退出；MOM 排在 DA 后，QQQ 先被组合热量拒绝、在 DA 平仓释放热量后才成交；EXT 盘前快照 429 导致研究臂多批全盲、盘前零单。XLE 76 股与 QQQ 7 股均已全量平掉、两个退出 intent 均 BOOKED、券商数量为 0，**没有裸仓**，但两条指令都为 `FILLED_UNBOOKED` 且没有当日 `trade_records`，所以不能把“仓位安全恢复”写成“管道验收通过”。此外 `edge_gate_shadow` 自 11-09 后断流，健康检查全绿不覆盖该项。

## 1. 主日志覆盖与总体时序

- 截止 23:18:07 CST，22:30 后主日志共 **1598 INFO / 138 WARNING / 2 ERROR / 2 CRITICAL**；两组 ERROR/CRITICAL 分别属于 XLE/QQQ 簿记失败及其补偿退出暂时待回报。
- 09:30:34–09:43:35：开盘禁入窗内不执行；`entry_blackout_shadow_eval --no-write` 为 **OK**，5 个 cycle、每轮报价完整、quote/eval 错误均为 0，20 条去重 hit。重启造成的 18 条重复 hit 按指令去重，属于已声明口径；hit 只是 band 几何上限，不是反事实成交。
- 09:33:14–09:33:35：服务在黑窗内重启。SIM/DB/ALLOW_REAL_TRADING 模式一致；BA/HON/ASML 三仓恢复，启动 `Done Futu:3 / playbook:3 / ✓`；Playbook 仓没有进入主线 PositionStore 被双重管理。
- 09:45:52：首次真实 `auto_execute`，21 条指令 = 19 DA + 2 MOM；09:30–09:45 无新入场。相比前一日 09:47:59，黑窗到点后的额外等待显著缩短，但本核查不据此计算收益反事实。
- 09:46–09:52：CSCO、AMZN、MRK、MU、CRM、LLY、LMT、COIN 共 8 笔 DA 新仓入簿；UBER/GS/ORCL 确认反转后放弃；MSFT/META/GOOG 等被 chase/VWAP/组合热量门按当时状态拦截。
- 09:53 起：AMZN/MU/MRK/CRM/LLY/LMT 到 5 分钟后武装早期紧 SL；MU/CRM/LLY 的 `basis=[?]` 是 recovery 元数据展示缺口，数值上紧 SL 距离恰为完整原始 ATR 距离的一半，符合 RFC-202 修复后的口径。
- 10:18 快照：正常未平 `trade_records` 7 个（AMZN/ASML/BA/CRM/HON/LLY/LMT），`playbook_exits.exit_intents` 非 BOOKED=0；服务 active。MRK/MU/COIN/CSCO 已分别按 SL_BREAKEVEN/SL_INITIAL/SL_TRAIL_LOCK/SL_TRAIL_LOCK 收口，净额 +$7.05/-$136.02/+$41.07/+$83.13；XLE/QQQ 均无持仓。

## 2. P0：MOM XLE/QQQ 两腿均在真实成交后簿记失败

### 2.1 事实链

1. 22:35 写入方生成 XLE/QQQ 两条指令，asof=2026-11-12，ID=5621/5622。
2. 09:52:04 XLE 触发；09:52:07 券商受理 76 股，订单 8369338；成交 64.50。
3. 09:52:09 `trade_record` 写入报错：`'NoneType' object is not subscriptable`，进入补偿退出（`logs/ftseek_us.log:5949-5956`）。
4. 09:52:15 补偿订单尚无成交证据，日志 CRITICAL 是暂时态而非最终失败；intent=`21bd5b51a1c04eebbc5825e2ac93e1ae` 由快循环接续。
5. 退出账本最终为 **BOOKED**：卖单 8369340，76/76，64.46，`broker_qty_after=0`，`order_filled=true`，`last_error=''`。
6. 但指令 5621 仍为 `FILLED_UNBOOKED / FILLED_ALL`，`execution_price/time=NULL`；当日 `trade_records` 无 XLE。PositionSync 从 09:52:37 起每约 31 秒重复 `terminal entry has no consistent broker position US.XLE`，说明补偿结果尚未被该恢复路径识别为已和解。
7. 23:01 `error_alert` 捕获 XLE 的 2 条 ERROR/CRITICAL 并成功推送，告警链没有漏报。
8. MRK/MU/COIN 在 10:02–10:05 平仓释放组合热量后，QQQ 于 10:04:55 被券商受理 7 股（订单 8369451），10:05:00 以相同 `'NoneType'` 错误进入补偿退出（`logs/ftseek_us.log:6334-6341`）。这从预期风险变成了第二次实锤，证明故障覆盖整个 MOM 写入形态，而非 XLE 偶发。
9. QQQ 补偿 intent=`a73da07d889b4c869d476c9bd4ceb822` 最终同样 **BOOKED**：卖单 8369455，7/7，679.28，`broker_qty_after=0`。指令 5622 同样停 `FILLED_UNBOOKED` 且无当日 trade_records；PositionSync 开始对 XLE/QQQ 双标的重复 terminal-entry 警告。23:16 `error_alert` 也捕获 QQQ 的 2 条严重日志并成功推送。

### 2.2 根因

`app/playbook/playbook_service.py:2332`：

```python
"thesis": inst.get("thesis", "")[:200],
```

MOM 写入方没有提供 thesis，SQLite 读出为 `NULL`，字典中键存在且值为 `None`。`dict.get(key, default)` 只在键缺失时使用默认值，因此此处对 `None` 切片必然报错。`app/playbook/playbook_service.py:2452` 的 `inst.get('thesis', '')[:60]` 也有同类风险，只是首笔已在前面先失败。

MOM 自动执行测试使用 `FakeService.execute_instruction`（`tests/playbook/test_mom_etf_v1_flow.py:201-204`），验证了 policy/分支/参数，却没有穿过真实 `PlaybookService` 的 `thesis=None` 簿记路径，故契约测试绿色不能覆盖这次真实首单。

### 2.3 验收判定与未收口面

- 按 RFC-199 的 L1 判据，首个真实锚点应记 **PLUMBING_BROKEN**：定时触发、3% reprice SL/TP=0、券商成交与补偿安全链有效，但正常 trade_record/open_position、持夜、V1 退出 allowlist 与报表腿未形成完整闭环。
- 常规 trade_records 绩效查询会漏掉这两次真实往返，需单独设计审计化补账/排除口径，不能直接把指令改成 BOOKED 了事。XLE 补偿平仓毛价差为 `(64.50-64.46)×76 = -$3.04`，QQQ 为 `(679.28-678.34)×7 = +$6.58`；费用与净额未进入当日 trade_records，不在本审计中虚构净 PnL。
- QQQ 在 DA 三笔平仓释放热量后也实际成交并复现同一故障；两腿最终都只有 exit-ledger 补偿证据，没有正常 MOM 持仓或 trade_record。**修复并做真实 service 契约复验前，应暂停后续 MOM 新仓入口；本核查未自行热重载或改单。**

## 3. DA 订单、确认与恢复是否符合逻辑

- EntryConfirm v2 的 mid ruler + 2/3 grace 实际生效：UBER/GS/ORCL 两次逆向后放弃，AMZN/CRM 首见逆向后恢复并通过，MU/MRK/COIN/LLY 通过。没有确认前成交这些 treatment 标的。
- 6 个确认后成交标的，按 instruction metadata 的 `confirmed_at` 到券商 `entry_time` 重算：AMZN 25.548s、COIN 40.292s、CRM 43.367s、LLY 45.044s、MRK 52.506s、MU 50.420s；中位 **44.205s**，范围 **25.548–52.506s**。这只是首日运行延迟，不是收益结论。
- AMZN/MRK 由主线程直接写 trade/open_position；CSCO/MU/CRM/LLY/COIN 先 `SUBMITTED`，随后由 PositionSync 各补录一条并转 Playbook 管理，未见重复 trade_records。LMT 一次 `DB 有多余持仓` 是本地写入与券商快照竞态，后续恢复一致。
- 早期紧 SL 只调整退出线，不是触发本身；后续 MRK/MU/COIN/CSCO 分别按 SL_BREAKEVEN/SL_INITIAL/SL_TRAIL_LOCK/SL_TRAIL_LOCK 正常平仓并入账，XLE/QQQ 则走独立的 ENTRY_BOOKKEEPING_COMPENSATION，两类退出不能混写。
- `chase_cap`、`PORTFOLIO_HEAT`、VWAP slope/VWAP gate 的 INFO/WARNING 均能看到后续跳过或未成功，不是只打印不执行。

### 容易误判但不是绕门的两条 INFO

1. LMT 的量能比 0.77 < 0.8 打印 `BLOCK` 后仍成交：它当时 price < entry，走 pullback；`app/playbook/entry_band.py:54-79` 明确只有 chase/near-miss 需要 `vol_ok`，pullback 不使用该门。逻辑符合现行规则，但 `BLOCK` 文案容易误导。
2. 多票 `conf=35<50, 降仓1.00`：实际乘数就是 1.00，并未降仓；属于既有采集期/系数设置下的文案问题，不应把它读成发生了仓位缩减。

## 4. P1：RFC-203 queue 影子覆盖不完整

`data/early_risk_shadow.jsonl` 今日只有 AMZN/MRK 两条 `queue_candidate`。原因是 `auto_execute_flow.py:1851-1917` 将记录钩子放在 `result.get("success")` 内；SUBMITTED 后由 PositionSync 补录的 MU/CRM/LLY/COIN 不经过该分支，`app/core/position_sync.py` 没有对应 queue 钩子。

所以正式影子样本覆盖为 **2/6**，不能用 AMZN/MRK 两条样本代表全部确认后成交。§3 的六笔延迟是本次只读交叉重算，不冒充影子原生产物。建议把补录路径也接入同一 record-only 钩子，并优先使用券商 entry_time；目前 instruction 的 recovery `execution_time` 是补录时刻，晚于真实成交，会高估排队时间。

## 5. 正常/已知降级项

- 启动时一次 Futu LV3 不健康发生在连接建立前，随后 LV3、Push、快照验证成功，不是持续行情中断。
- 财报日历先静态 KEY_US_SYMBOLS fallback，随后富途主源 546 条生效，最终不是只靠静态池。
- 09:30 pre_market 慢轮次、GC/RSS 台阶、auto_execute 长尾是性能告警；未看到由此导致的黑窗前下单或退出停摆，但 auto_execute 33–56s 会拉长同轮后续任务排队。
- HermesWorker 预检失败后降级 Bailian 直连，属于 Agent 功能降级；今日 DA 指令已在重启前生成，未构成当前 Playbook 执行断链，原因需另查。
- 账户级回撤约 1.06–1.12%，低于 3% FLAT_ALL；INFO 的 `FLAT_ALL用账户级` 是选择回撤口径，不是触发强平。
- 动态订阅随指令/持仓集合变化，MOM 的 XLE 在写入后被补订阅；主线扫描关闭、旧计划跳过、Playbook 自管仓跳过均符合架构约束。

## 5.1 盘前与 DA/MOM 供给链

- DA 数量闭环正常：110→33 扫描候选、32 个 Instruction、19 条 bridge/DB；NVDA 被 Gate BLOCK，12 条 SKIP，bridge 文件与 DB 的 19 个 symbol 一致。`da_supply_reconcile` 的 19/19/32 OK 只覆盖 DA 文件→DB→snapshot，不覆盖 MOM，也不证明推送或执行成功。
- 盘前数据 25 标的日K均为 2026-11-12；SUMMARY OK 26 / YELLOW 1 / RED 0。MOM 四 ETF asof 均为上一交易日，QQQ close 688.17 > MA200 680.9372；12-1 momentum 排名前二为 XLE 0.510922、QQQ 0.178268，生成规则与费用/尺寸 precheck 均符合 RFC-199。
- **MOM 执行顺序与 handoff 描述相反**：`auto_execute_flow.py:441-451` 仅给 UE=2、DA=1，MOM 落入其他=0 并排在 DA 后；并不存在“DA 循环前处理 MOM”的预跑分支。首个 09:50 后可执行主循环 09:51:28 先处理 DA，XLE 到 09:52:04 才触发，QQQ 在 DA 已占组合热量后被拒。仍在 09:50–14:00 有效窗内，但“09:50 定时触发”只能算近似达成。MOM 是否应优先占预算属于独立策略/预算裁决，不在本审计擅自改排序。
- `mom_etf_v1_r13/r14` readiness 仍写 0 指令/0 成交，内嵌 8 个代码 hash 中 5 个已与当前文件不同；它们是历史凭据，不能充当今日部署或首单验收证明。

## 5.2 EXT 盘前快照 429：fail-closed 但供给失明

- `ext_entry.log` 当前文件出现 **827** 次“每30秒最多60次”，其中 793 次为 `liquidity quote failed`；前一日轮转文件为 164 次，约放大 5 倍。该比较只是一日对照，不据此声称长期基线。
- 多个研究池批次全部 `research_quote_unavailable`：例如 `ext_entry.log:3875/3928/3980/4032` 为 16/16、17/17、16/16、16/16，`:4684/4740/4795/4849` 为 12/12、21/21、20/20、19/19；09:27 ET 仍为 22/25。
- 今日盘前所见 EXT 下单为 0。报价缺失走 fail-closed，没有把无报价标的错放行；但盘前最佳流动性窗几乎全盲是实质供给故障，不能与“正常零信号”混为一谈。429 配额究竟由 EXT 自身、主进程还是其他并发消耗，当前日志不能单独定责，需做调用方/时间桶归因。

## 5.3 辅助循环与监控覆盖

- `extended_hours_exit_cron`：collector returncode=0，orphan/closekey/rth_zero_fill 未见异常；前一日 GOOG/QCOM/TSLA 的 evidence_pending 均在下一拍收口。当前文件 gate/collector JSON 缺自身时间戳，需从内嵌 stdout 反推，是可观测性缺口。
- `exit_intent_reaper` 每 5 分钟运行、未检出账实不符；独立只读库为非 BOOKED=0。它不打印扫描条数，单看日志无法区分“0 条可扫”与“N 条全对”。
- `healthcheck` 每 10 分钟通过，main.py/8901/OpenD/DB integrity/磁盘/内存均绿；**但不覆盖 edge shadow**。`data/shadow/edge_gate_shadow.jsonl` 最后写入停在 **2026-11-09 23:40 CST**，11-10 起多个桥日零写入；healthcheck.log:4,6 已有业务心跳 WARNING。今日未再报警只是 ET 告警窗未开启，不是已经愈合。
- `event_stream_ingest` 四个来源均 OK，cadence_skip 是 5 分钟 cron 对 15/20 分钟节流的正常结果；`ext_event_risk_live_shadow` 每 15 分钟运行，ASML=NO_CHANGE、errors=0、production_write=false。EXT 影子只有 ASML 是 `decision_id LIKE 'EXT-%'` 口径，和退出监控还覆盖 BA/HON 不矛盾。
- `signal_snapshots.created_at` 同列混用 DA 的 ET aware 与 UE 的 naive CST 墙钟，今日 UE 行显示 22:15 而 DA 行为 08:xx-05:00；按字符串排序/新鲜度计算会把 UE 当成“未来”，需另列时间口径修复。

## 6. 可复现核查

```bash
# 主链严重级别与 XLE 全生命周期
rg -n 'ERROR|CRITICAL|US.XLE|21bd5b51a1c04eebbc5825e2ac93e1ae' logs/ftseek_us.log

# 黑窗只读评估
PYTHONPATH=. ./venv/bin/python scripts/entry_blackout_shadow_eval.py \
  --date 2026-11-13 --json --no-write

# 指令与正常未平仓（只读）
sqlite3 -header -column 'file:data/ftseek_us.db?mode=ro' \
  "SELECT id,symbol,executed,execution_state,execution_price,execution_time,skip_reason FROM playbook_instructions WHERE date='2026-11-13' ORDER BY id;"
sqlite3 -header -column 'file:data/ftseek_us.db?mode=ro' \
  "SELECT symbol,entry_price,entry_time,decision_id FROM trade_records WHERE exit_time IS NULL OR trim(exit_time)='' ORDER BY symbol;"

# 补偿退出最终证据（只读）
sqlite3 -header -column 'file:data/playbook_exits.db?mode=ro' \
  "SELECT intent_id,state,json_extract(result_json,'$.dealt_qty'),json_extract(result_json,'$.filled_price'),json_extract(result_json,'$.broker_qty_after') FROM exit_intents WHERE intent_id IN ('21bd5b51a1c04eebbc5825e2ac93e1ae','a73da07d889b4c869d476c9bd4ceb822');"
```

## 7. 未验证 / 未做

- 未跑全仓或聚焦 pytest；本结论是生产日志 + 只读 DB + 独立黑窗评估器核查，不冒充测试通过。
- 未观察完整交易日、正常 MOM 持夜或 21 交易日退出；未评估任何策略净 alpha。
- 未修改 XLE/QQQ 指令或 trade_records，未手工补 PnL，未暂停 MOM 入口，未重启。
- 未逐笔目检富途 App；券商数量与成交证据来自日志及本地只读账本。

## 8. 建议顺序

1. 先暂停 MOM 新仓入口，修 `(inst.get('thesis') or '')` 两处切片，并增加穿过真实 PlaybookService 的 `thesis=None` 首单/补偿不回触发测试。
2. 为 XLE 的指令、trade_records、exit intent 设计幂等、可审计的补账/排除口径；让 PositionSync 识别已 BOOKED 的补偿退出，消除每 31 秒重复报警。
3. 把 PositionSync 补录纳入 RFC-203 queue 影子，使用券商 entry_time；覆盖率未补齐前，不拿 2/6 样本做整体延迟裁决。
4. 完成真实 service 复验后再判 MOM L1；QQQ 的 PORTFOLIO_HEAT 拦截是有效风控，不应为了完成管道样本而放宽。MOM/DA 执行优先级如需改变，另起预算语义裁决并修正 handoff 与实现的一致性。
5. 对 EXT 429 按调用方和时间桶归因，先恢复报价配额与全盲告警，再评价“盘前零单”是否有策略意义；不得把 quote_unavailable 当成正常无信号。
6. 排查 `edge_gate_shadow` 11-10 起断流并补覆盖哨兵；重生成当日 MOM readiness，另修 `signal_snapshots.created_at` 混合时区与辅助日志缺时间戳问题。

## 9. 优化修复实施与部署（2026-11-13 用户「请执行优化修复」）

### 9.1 已实施

1. **MOM NULL thesis**：`playbook_service.py:2332,2452` 改为 `(inst.get('thesis') or '')`，不改信号、尺寸、预算或退出参数。新增真实 PlaybookService 首单回归，完整经过 MOM 费用/名义/预算复核，验证 NULL thesis 正常 BOOKED 且不触发补偿平仓。
2. **补偿闭环**：新增 `pipeline_parts/compensation_repair.py`。完整 schema 下，已验证的 compensation owner + 退出成交证据会重建已平仓 trade_record，复用统一费用/净额/outcome 落账，并按原 CAS 字段收口指令；旧/最小 schema 保留独立回执、绝不虚构交易。身份冲突 fail-closed，回执重放幂等，后续平仓失败只删除未平补建行。
3. **XLE/QQQ 生产补账**：用户另行明确授权后，`repair_compensation_entry_ledger.py --apply` 仅处理两个 intent。XLE trade_id=1895（毛 -3.04 / 费 4.55 / 净 **-7.59**，指令 loss）；QQQ trade_id=1896（毛 +6.58 / 费 4.13 / 净 **+2.45**，指令 win）。两者均 BOOKED、MOM 未平=0；复跑=ALREADY_CURRENT。审计：`data/audits/compensation_entry_repair_20261113T234547Z.jsonl`、`*234548Z.jsonl`。
4. **queue 覆盖**：`position_sync._record_recovered_queue_candidate` 覆盖 SUBMITTED→补录路径，使用券商 updated/create_time，标记 `execution_time_source=broker_order_updated_or_create_time`；缺券商时间不造样本，历史 4 条不补写，后续自然成交开始覆盖。
5. **edge 断流**：执行审计恢复 `log=True`，EdgeShadowGate 按（真实 ET 日, decision_id）进程内去重，写失败释放去重键供重试；旧生成侧与新执行侧不会同进程双写。
6. **EXT 429**：新增第二轮 `_QuoteCache` 批量预取/本轮缓存，保留第一波与第二波独立时间样本，不直接复用 `_batch_quote`；失败缓存 None 且 fail-closed，限频/网络错误不二分不逐票重试，形状回退有界。严格臂/研究臂 dry/live 均接入。
7. **快照时间**：`record_signal_snapshot` 默认 `created_at/created_at_snap` 改 ET-aware `now_et()`；显式 DA 时间原样保留。另只修测试夹具的实际沙盒路径和幂等 schema 初始化，不改生产迁移行为。

### 9.2 验证

- 最终恢复/补偿/快照/edge/queue/体积/config_version 联合回归 **58 passed**；MOM readiness 聚焦 **37 passed**；EXT 新缓存+既有 executor **66 passed**。
- 棘轮与 offline full-chain 主体通过；`pipeline.py` 3700→**3682**（净下降），未改任何棘轮基线。DB chokepoint 其他 5 脚本、线上其他 3 运维脚本仍是既有基线红。
- 全仓一次实测 **9064 passed / 14 failed / 3 errors**。其中本批直接相关的 3 个 recovery 失败与 2 个 snapshot 夹具失败已修复并复跑通过；其余为已核既有问题：config census、crontab 台账、DA prompt injection、DB 直连 5 脚本、archive 文案、线上 3 脚本、early_dead 历史污染、weekly row_kind，以及 3 个 recovery 外网边界错误。未宣称全仓全绿。
- 生产补账前两次数据库副本模拟均成功，apply 后逐行净额/指令状态核验，复跑幂等。
- 部署：重启前 broker=internal=9、drift=[]、external=[]、非 BOOKED=0；重启后 MainPID=2920876，启动冒烟 **7/7**，9 仓不变、非 BOOKED=0。readiness=`data/readiness/mom_etf_v1_r16_compensation_repair.json`（2 指令/2 已平仓 MOM trade/0 持仓）。
- 07:46 起未再出现 XLE/QQQ terminal-entry 重复警告；23:01/23:16 告警均已有成功外发记录。

### 9.3 未验证 / 不改

- 未改 MOM/DA 优先级、PORTFOLIO_HEAT、任何入场/退出阈值或四锁；MOM entries 保持原开关，不为攒样本放宽。
- 尚未出现下一次正常 MOM 首单与持夜，故不能把历史补账等同于 V1 `PLUMBING_OK`；须下个自然调仓簇复验。
- EXT 最新 cron 因 `ext_open_position_limit=5` 未进入实际二次报价闸，生产 429 降幅尚待有候选/空位时的自然日志；不以单测冒充实盘配额恢复。
- edge 文件尚无重启后的自然 DA 新行，待下一生产执行漏斗验证；不人工写假样本。
- 未处理既有新库 `market_context_json` 迁移缺口、其他 WIP/基线红或 MOM/DA 预算优先级。

### 9.4 后续判据

- 下一次正常 MOM 首单须无 `NoneType`、无补偿退出、trade_record/open_position 正常建立；持夜后仅走 V1 allowlist，才可重判 L1。
- EXT 自然候选进入流动性闸时，第二次报价请求应批量完成，不再出现逐票 429 风暴；若仍全盲，先查主进程/其他调用方配额，不放宽 fail-closed。
- 下一自然 DA 执行日后，`edge_gate_shadow` 应有当日行且同 decision_id 不重复；queue 新补录样本应带 `broker_order_updated_or_create_time`。
