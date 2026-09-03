# DEB-20260903: 盘中交易链路体检 — 三处已修缝 + 三项待裁（tripwire 量纲误杀 / watchdog 心跳饥饿误重启 / 每单新建 Router 泄漏）

- **立案日**: 2026-09-03（用户令"美股已经开盘，请检查整个交易链路和日志，确保相关的记录、数据录入和链路正常"；体检窗口 ET 09:55–10:30 / CST 21:55–22:30）
- **状态**: **§二 三项已修并实测验证（零交易行为变更）**；**§三 D1 已由用户裁决并执行止血（`tripwire_checker_enforce: true→false`，见 §3.1，判定照跑/审计照落/DB 不写，恢复条件=消费端守卫+影子≥10交易日）**，D1 的守卫修法本身仍待排期；**§四 D2 / §五 D3 已于同日 23:40 CST 按用户令"继续执行修复"落地（采纳 D2 (a)+(b) / D3 (b)，各带单测 + 端到端时序复现 + 回归 403 passed，见 §4.2-4.3 / §5.1-5.2）——两者均 `.py` 改动，`需重启生效`，盘中未重启（工作区另有并行 Agent 未提交的 7 个 `.py`），建议收盘后重启 + 跑启动冒烟**；**§六 D4(a) 同批已修**（盘前步骤级取消：跨开盘即跳过剩余非必需盘前步骤，回轮顶进 TRADING 分支恢复入场；零损失契约由测试钉住——被跳步骤在 TRADING 分支均有同名 catch-up）**，D4(b)(c) 仍待裁**（(b) 主动开盘重启不推荐；(c) 重活外移属 C4 影子先行）；**§七 遗留** conftest 硬闸扩 `sqlite3.connect`（收盘后专项）。
- **纪律引用**: C4 影子先行（行为变更一律影子先行）· C1 退出参数冻结（本档零触碰，`exit-de49b1299290` 不变）· 宪章二.7 遥测实测>文档自述 · 宪章四.2 一次一变量 · AGENT.md 军规"新增落库字段必带单位/量纲声明"（2026-09-02）· 军规"重启必跑启动冒烟"。
- **关联**: `docs/reviews/REVIEW-20260903-postmarket-chain-and-ruler-audit.md`（同日盘后批，§四.8 测试污染生产数据 — 本档 §二.2 是同一病灶的 **sqlite3 盲区**续集）、DEB-20260825（tripwire 结构化条件 r15 P10 接线，本档 §三 的上游）、RFC-072（KR3 预注册影子，本档 §二.1 的载体）、`docs/references/crontab-loop-baseline.txt:53`。

---

## 一、体检结论：链路在跑、记录在写，逐环实测

服务 `ftseek-us` active（PID 128545，**21:32:00 CST 由 watchdog 自愈重启**，见 §四）；启动冒烟 **7/7 全绿**（`PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py` → exit=0，启动段 21:32:07）。

| 环节 | 实测证据（ET） | 判定 |
|---|---|---|
| 行情接入 | `[FutuLV3] ✅ 连接成功 LV3 行情可用`；21:30 曾 `last_push_age=2527s` 触发重连并 25.1s 恢复健康 | ✅（重连机制有效） |
| 市场状态 | `[Regime] Assessment: risk_on (score=8) SPY=769.73 MA200=709.30 VIX=14.65`；`[VIX] 级别=NORMAL` | ✅ |
| 候选供给 | `[DA供给对账] bridge候选=17(ok) DB落库=17 signal_snapshots决策=29 → [判定] OK` | ✅ |
| 指令生成 | `playbook_instructions` date=2026-09-03：**75 行**，其中真候选 18 条，`triggered=13 executed=13` | ✅ |
| 入场执行 | 09:38–10:01 逐笔 `re-price → 触发 → DualBroker 下单 → broker_deal_confirm 真成交均价`；`trade_records` id 1438–1447 连续落库，`source_type=PLAYBOOK_SIM`，`futu_order_id` 齐 | ✅ |
| 出场监控 | `[PlaybookExit] 快循环已启动 (interval=10s)`；`退出监控: 9 个未平仓`；NVDA/BWXT/MU/AVGO/CRM/MRK/MDB/DDOG/SNOW/MAX/HPE 均有 `平仓 reason=... pnl=...` + 归因行 | ✅ |
| 记账/对账 | `[US Sync] Done | Futu: 9 | 主线DB: 0 | playbook自管: 9 | 一致性: ✓`；`capital_state` 每 30s 刷新（equity=$98,858 water=green is_locked=0 **trade_env=1 模拟盘**） | ✅ |
| 风控 | `[AccountCircuitBreaker] 账户正常 日盈亏=0.13% 回撤=0.25%`；guardrail 拦 `方向偏斜82%超限`/`sector Technology 47%超限` 各降级 2 条；PDT 计数 41/3（equity≥$25k 豁免） | ✅ |
| 推送 | 钉钉逐笔 `推送成功`（成交/平仓/watchdog 告警） | ✅ |
| 旁路影子 | `da_glm_shadow 成功17/失败0` · `universe_confirm 裁决9候选` · `borrow_rate 48只` · `premarket_data_check 27项 0 RED / 1 warning` · `futu_channel_shadow capital_flow+120行` | ✅ |
| 盘中诊断 | Hermes `ftseek-intraday-diagnose` 10:00:58 EDT 输出：18 指令 / 13 触发 13 执行 / 8 持仓 / **日志 ERROR·Traceback 面 0**（5 条 ERROR 全是 §四 重启前的 watchdog 心跳告警） | ✅ |
| 健康检查 | `logs/healthcheck.log` 每 10min ✅ 连续通过（含重启窗 21:30/21:40/21:50） | ✅ |

当日已实现盈亏（ET 10:20 快照，模拟盘）：HPE −153.95 · MAX +103.15 · MDB +21.11 · SNOW +20.92 · MRK +14.58 · AVGO +11.86 · NVDA +10.87 · BWXT +10.60 · CRM +8.58 · DDOG +6.20 · MU −7.85。

**结论**：下单/持仓/记账/对账/推送主链无断裂、无 Traceback、无孤儿仓；**发现的问题全部在"尺子与守卫"层（记录可信度）与"资源卫生"层，不在成交链路上**。

---

## 二、已修三项（本次会话落地，逐项带实测验证）

### 2.1 KR3 影子采集器跑完不退 → 每工作日泄漏一个 ~105MB 僵死进程

- **实证**: `scripts/kr3_runup_shadow.py`（RFC-072，cron `10 6 * * 2-6`）06:10:06 就打印"采集 29 笔"完成落盘，进程 **pid 55906 到 22:10 仍存活（16h05m，RSS 105MB）**，并占一条 OpenD 连接。本机内存仅 3.5Gi，Hermes 同期已在 `errors.log` 连报 `system memory pressure is elevated; limiting to at most 1 new worker`。
- **根因**: 脚本内 `router = MarketDataRouter()` → `FutuLV3Provider` → **Futu SDK 非守护线程阻解释器退出**。这是仓内已知病灶：`scripts/sector_pulse.py:95` 有 2026-07-29 的同款修复与注释（"不修则 Futu 非守护线程吊住解释器退出, cron 每 30min 泄漏一个进程"），`scripts/ssr_shadow_daily.py:120` 用 `os._exit` 硬退出。新脚本（09-02 挂 cron）两个惯例都没继承。
- **修**: `scripts/kr3_runup_shadow.py` 采集完成后 `router.close()`（sector_pulse 惯例）+ `__main__` 改 flush 后 `os._exit(rc)`（ssr_shadow_daily 惯例）。crontab 行**不动**（该行已登记在 `docs/references/crontab-loop-baseline.txt:53`，改行会触发棘轮）。
- **验证（不污染预注册样本的跑法）**: 直接跑脚本会把今日**未平仓**单提前写进 `data/shadow/kr3_direction_runup_gate.jsonl`（`exit_time/realized_pnl=NULL`），而脚本按 `trade_id` 幂等去重 → 明晨 06:10 会跳过它们，**KR3 冻结样本将永久缺出场字段**。故改用 OUT 重定向到 /tmp 的探针跑：`main() rc=0 用时=4.4s 落盘43行`，**main 返回后解释器 5.2s 内自然退出（exit=0，非 timeout 124）**；生产 jsonl mtime 仍为 06:10 未变。已 `kill 55906`（工作已 flush，零数据损失），`free` 回收 ~105MB。

### 2.2 conftest 生产数据沙箱的 **sqlite3 盲区** → regime_history 82.3% 是测试夹具

- **实证**: `tests/strategy/test_regime_strategy_integration.py` 每跑一次，经 `app/strategy/regime_filter.record_regime_change`（`sqlite3.connect` 直写）往**生产** `data/signal_snapshots.db` 的 `regime_history` 灌 **8 行夹具**（签名恒定：`spy_price∈{450,550} · spy_ma200=500 · vix∈{15,22,27,28,40}`）。本次实测 3036→3044（跑一次涨 8）。表内 **3044 行中 2504 行（82.3%）是该签名**，且**生产自 2026-07-29 起零真行**（真值应为 SPY≈769 / MA200≈709）。今日 02:03/02:28/02:35/07:37/08:xx/09:xx/10:12/10:30/12:24/12:33 共 104 行，全部夹具。
- **根因**: 同日盘后批（REVIEW-20260903 §四.8）加的硬闸只 monkeypatch `builtins.open` / `Path.open|write_text|write_bytes`，**`sqlite3.connect` 不在覆盖面** → DB 直写型埋点整体漏网。
- **修**: `tests/conftest.py` `_PRODUCTION_DATA_SEAMS` 登记 `("app.strategy.regime_filter", "_REGIME_HISTORY_DB", "path", "signal_snapshots.db")`（沙箱重定向，机制同既有三缝）+ 注释留证。
- **验证**: 重跑同一测试文件 `33 passed`，生产 `regime_history` **max(id) 保持 3044 不涨**（修前必涨到 3052）。
- **数据清洗（含可回滚备份）**: 先用 sqlite backup API 全库快照 → `data/backups/signal_snapshots.pre-regime-clean-20260903.db`（核对 3044 行），再按三条件签名 `DELETE`：**3044 → 540（删 2504，与计数预期一致）**。保留全部非签名行（2026-06/07 共 540 行，含 vix=0 的历史生产行）不做扩大解释。同库 `signal_snapshots` 表体检 `TEST` 行 **0**（未受污染）。

### 2.3 盘中诊断把"最后一行"当现行 regime → 长期播报夹具/旧值

- **实证**: `ops/hermes/intraday_diagnose_late.py:206` 取 `regime_history ORDER BY rowid DESC LIMIT 1` 直接当"盘面"，由 Hermes cron `ftseek-intraday-diagnose-late`（`*/30 0-4 * * 2-6` = ET 12:00–16:30，enabled）推送。清洗前今日最后一行是 12:33:47 的夹具 → 今晚该推送会写 `盘面: regime=BULL→BEAR trigger=safety` + rationale `BEAR: SPY<MA200($450<$500), VIX=28.0>25`，**而真实盘面是 risk_on / VIX 14.65 / SPY 769.73**。
- **修**: 加陈旧守卫 —— 行日期 ≠ 今日 ET 即 `regime_stale=True`，播报改为 `regime=无当日记录 (regime_history 最后一行 <日期>: …)` 并追加"关注点"条目（"勿据旧值判市"）。属"修尺子"（诚实化），不改任何交易路径。
- **验证**: 实跑脚本，输出 `• 盘面: regime=无当日记录 (regime_history 最后一行 2026-07-29: BULL→BEAR trigger=safety), VIX=14.65(trade_records)` + 关注点 2 条。修前同一条会呈现为当日盘面。

> **三项共同点**：都不改信号分布、不改退出参数、不改下单路径；两处是资源卫生，一处是尺子诚实化。符合宪章四.1"修尺子/做减法"白名单。

---

## 三、【D1 已止血 · 守卫修法待排期】tripwire enforce 门"量纲族"误杀：11 条强制失效中 5 条可证伪为假

`scripts/tripwire_checker.py`（cron，`enforce=True`）双轨判定 thesis 失效：文本轨（`evaluate_invalidation_text`）+ 结构化轨（`invalidation_triggers`，DEB-20260825 r15 P10 接线）。落库 `data/tripwire_checker.jsonl` **674 行，invalidated=true 11 条，全部 enforced=true**，其中 **5 条（45%）可逐条证伪为假失效**：

| # | 时间(ET) | 标的/方向 | 命中串 | 病灶 |
|---|---|---|---|---|
| 1 | 09-01 10:00 | GEV SELL_SHORT | `[HIT]成交量>2.0(obs=3.89e+05)` | **量纲**：阈值 2.0 是"放量倍数"语义，观测值取 `quote.volume` 当日累计**股数** → 恒真 |
| 2 | 09-02 15:55 | XLI SELL_SHORT | `[HIT]收盘价>0.0(obs=172.7)` | **退化阈值**：模型把"翻红/站上VWAP"写成 `收盘价>0` → 任何正价恒真 |
| 3 | 09-02 15:55 | XLY SELL_SHORT | `[HIT]收盘价>0.0(obs=114.9)` | 同上 |
| 4 | 09-02 15:55 | LLYVK SELL_SHORT | `[HIT]收盘价>0.0(obs=102.2)` | 同上 |
| 5 | 09-03 09:30 | COIN BUY | `[HIT] 价格 181.66 跌破阈值 55000.0` | **跨资产**：原文"若**BTC**跌破$55000**或**COIN放量跌破$170"，OR 拆分后把 BTC 的阈值套到 COIN 自身价格上 |

另有**未触发但已污染 detail 的同族误解析**：EOG 的"油价回落跌破**6周**高点支撑"被文本轨解析成 `价格阈值 6.0`（2 行）——把"6 周"当成 6 美元。今日未误杀只因 EOG 现价 148.2 > 6.0；换成低价股即恒真误杀。

- **影响面**：被误杀的指令写入 `playbook_instructions.skip_reason='TRIPWIRE_INVALIDATED'`（近 7 日 8 条），既**丢掉可成交机会**，又**污染拒绝归因台账**（skip_reason 分布是 UE/DA 供给质量判分的分母之一）。今日 COIN 恰已被方向偏斜 guardrail 降级为 SKIP，**无实际成交损失**，但这是巧合不是防护。
- **根因定位**：生产端 `scripts/llm_universe_scan.py:107` 的契约只写 `metric 限 收盘价/成交量/5日涨跌/盘前涨跌幅, operator 限 <或>, value 纯数字` —— **逐 metric 的单位/量纲从未声明**；消费端 `scripts/tripwire_checker.py:192` 也**无单位校验、无退化值校验**（`成交量` 直接吃原始股数，`收盘价` 接受 0.0）。这正是 AGENT.md 军规（2026-09-02）"新增落库字段必带单位/量纲声明 + 契约测试断言"点名的第四案同族。代码自称原则是"宁漏不误杀"，实际是"宁误杀"。
- **建议修法（三步，均需裁决后排期，不与其他变更同日叠加）**：
  1. **契约补单位**（生产端）：`收盘价=绝对价USD / 成交量=相对20日均量的倍数 / 5日涨跌=百分比 / 盘前涨跌幅=百分比`，写进 prompt 白名单说明 + JSON 示例（示例是模型依从的真因，见 E3-fix 结论）。属 prompt 改动 → 按宪章四.3 **必须 scan-eval 离线 A/B 先行**。
  2. **消费端守卫**（可先落，纯"宁漏"方向）：`成交量` 阈值 <50 视为倍数语义 → 观测值改取 `volume/avg_volume_20d`（或该条跳过并记 `[unit_ambiguous]`）；`收盘价` 阈值 ≤0 或 <0.1×entry_price 或 >10×entry_price → 判 `[degenerate]` 跳过；文本轨解析出的价格阈值同样做 entry_price 数量级合理性检查（挡住 55000/6.0 两族）。
  3. **影子先行（C4）**：新守卫先以 record-only 落 `tripwire_checker.jsonl` 新字段（如 `guard_note`），跑 ≥10 交易日，用"守卫拦下的 HIT 中有多少条事后被证明是假失效"作判据，再翻 enforce。回滚 = 一行配置/删守卫分支。
### 3.1 裁决与执行（2026-09-03 22:34 CST，用户裁"需要"）

**已执行的止血**：`config/unified_config.yaml` `playbook.execution_parameters.tripwire_checker_enforce: true → false`（带日期注释 + 根因 + 恢复条件就地落键）。这是脚本自身头注（`scripts/tripwire_checker.py:40`）与 yaml 注释（原 `:1206`）共同写明的既定回滚路径："false = 只落审计 jsonl 不写库"。

**改前治理面核查**：该键全仓仅被脚本自身引用（`tripwire_checker.py:274` 读、`:40` 头注写回滚路径），**无测试钉值、无 ConfigValidator REQUIRED_KEYS / config_audit _AUDIT_ITEMS / CRITICAL_KEYS 登记、不在 `data/config_freeze_baseline.json` 退出参数冻结面**（本键是入场侧核销门，非退出参数 → 不触 C1/KR2 的 `exit-de49b1299290`）。

**验证（逐项实测）**
1. YAML 解析 OK（顶层 57 键），`get_dotted(...) → False`。
2. 服务热重载正常吃下：`22:34:22 [HotReloader] 检测到文件变化 → ✅ 配置已热重载 (第 1 次) → ✅ ConfigContainer 已同步 → ✅ 已失效进程内快照: PlaybookConfig, FeatureFlags`；改后窗口 `ERROR/Traceback/CRITICAL = 0`，`systemctl is-active = active`。
3. 手动实跑核销器（`timeout 300` 包裹）：`=== tripwire 核销 2026-09-03 10:34 ET · 待检 2 条 · enforce=False === → 判定 2 条 · 失效 0 条 · 未写库 (dry/enforce=false)`；jsonl 新增 2 行（EOG/JNJ）**`enforced=false`** → **判定照跑、审计照落、DB 不写**，正是 D1 守卫需要的 record-only 影子供给。

**时序（诚实披露）**：今日 09:30 / 10:00 / 10:30 ET 三批 cron 仍是 `enforce=True`（配置 10:33 ET 才改），其中 09:30 批产生了 COIN 那条假失效；**11:00 ET 起的 cron 批次读到 False**。

**今日 DB 留痕实况**：`playbook_instructions` 中 `date='2026-09-03' AND skip_reason='TRIPWIRE_INVALIDATED'` = **0 行** —— 被 tripwire 判失效的 COIN（inst_id 2159）当前 skip_reason 是"方向偏斜超限 guardrail 降级 (多数方 BUY, 原size=26)"，即 guardrail 后续覆写了 tripwire 的写入，故今日假失效**未留下 DB 痕迹**（jsonl 有痕迹）；近 7 日 DB 留痕 8 条全在 08-27→09-02（08-27/08-28/08-31/09-01 各 1、09-02 4 条）。

**代价（明示，用户已知情选择）**：真失效也不再拦 —— "thesis 已死但价格回到触发带"的单会照样成交，即该门 2026-08 上线前的原状。审计 jsonl 继续记录 `invalidated` 判定，故这段时间既是止血窗也是 D1 守卫的天然对照窗（可回算"若守卫在场，这 N 条 HIT 里有几条是假"）。

**恢复条件**：§三 建议修法 2 的消费端守卫（成交量单位归一 + 退化/跨资产阈值跳过）落地 → record-only 影子 ≥10 交易日 → 假失效率实测下降 → 翻回 `true`（一行，无需重启，cron 独立进程）。

**附带观察（未裁，低置信度）**：手动实跑"待检 2 条"耗时约 4 分钟（按后台轮询节奏估算，非精确计时；主要成本在 router 构造与批量取价，非逐条判定），而 crontab 挂的是 `timeout 300` → **余量偏紧**。若未来某批超时被 SIGTERM，该批审计行会静默缺失（jsonl 无行、日志无汇总行）。建议后续给该 cron 行放宽 timeout 或给脚本加计时遥测；本次不动 crontab（该行已登记在 `docs/references/crontab-loop-baseline.txt`，改行触发棘轮）。

- **本档不代裁点**：①是否接受"消费端守卫先落 + 影子观察"这一顺序；②`成交量` 单位究竟定为倍数还是股数（影响历史 11 条的可比性，需按宪章二.2 新旧基线切分）。

---

## 四、【已修 D2 · 待重启生效】watchdog 心跳饥饿 → **盘中 09:31:38 ET 误重启**（36 秒盲窗）

- **实证链**：`[LLM策略] 盘前 → 新闻采集 + LLM 推荐` 20:02:28 CST 起一轮跑了 ~59 分钟；21:01:09 才 ping 到下一次；随后 21:11/21:21/21:31 连报 `llm_strategy_loop 已 629s/1229s/1829s 无心跳 (阈值 600s)` → `21:31:38 [Watchdog.heal] restart (1/3 today)` → **21:32:00 systemd 重启，21:32:14 恢复**（ET 09:31:38→09:32:14，**开盘后 90 秒起 36 秒盲窗**）。关闭期还伴随 `⛔ 交易时段拒绝 stop_auto_loop`、`退订 TICKER/ORDER_BOOK 失败`（成功=2 失败=2）、`线程 LLMStrategyLoop 未能在 10s 内退出`。
- **根因**: `app/core/scheduler_core.py:2044` 的 `get_watchdog().ping("llm_strategy_loop")` **只在 while 循环顶部打一次**，而单轮迭代（盘前新闻采集 + 多标的 LLM 推荐）实测可达 ~1 小时 ≫ 阈值 600s → 只要跑长轮就必然误判"循环已死"。这不是循环卡死，是**心跳埋点粒度错配**。
- **历史面**：`Watchdog.heal restart` 近半月 4 次（08-22、08-23、09-02 = playbook_exit_loop、09-03 = llm_strategy_loop）；08-23 当日 llm_strategy_loop 告警 9 条。即"误重启"是复发病灶，不是偶发。
- **本次实际损失**：盲窗内无持仓出场触发（当时仅 1 个隔夜仓 NVDA，09:46 才以 SL_BREAKEVEN +$10.87 出场），**无成交/出场丢失**；但这是运气，不是设计。
- **候选修法（互斥，需裁决选一）**：
  - (a) **心跳内嵌**：在长轮内部（每个标的 LLM 调用后 / 每 N 秒）补 ping —— 最贴合"心跳=活着"语义，改动面在 `_llm_strategy_loop` 内部，零行为变更；
  - (b) **阈值上调**：`llm_strategy_loop` 600s → 例如 3600s（配置化）—— 一行可回滚，但真卡死的检出延迟同步放大 6×；
  - (c) **自愈动作降级**：盘中（ET 09:30–16:00）不做整服重启，改为只重启该线程 / 仅告警，盘后再整服 heal —— 消除盲窗，但要新增"线程级重启"能力（复杂度最高）。
  - 建议 **(a) 为主 + (b) 作保险**（两者都不改交易行为），(c) 另案。**本档不代裁**，且不热改：任何一项都要重启才生效，盘中不叠加。

---

### 4.1 归因勘误（实测推翻立案时的耗时归因，宪章二.7 遥测实测 > 文档自述）

立案时写"单轮迭代（盘前新闻采集 + 多标的 LLM 推荐）实测可达 ~1 小时" —— **证伪**：`llm_pushes.after_hours_scan_enabled=false`（`config/unified_config.yaml:804`）→ `run_after_hours_llm_scan` 进门即返回（`app/pipeline/llm_pipeline.py:591-597`），**LLM 新闻推荐一步耗时 ≈0**。真实的 57.7 分钟（2026-09-03 盘前轮 20:02:28→21:01:09 CST）逐段归因如下：

| 时段（CST） | 时长 | 归属步骤 | 证据 |
|---|---|---|---|
| 20:02:28 | ~0s | `[LLM策略] 盘前 → 新闻采集 + LLM 推荐` → llm_scan 立即返回（配置关停） | 日志行本身 |
| 20:02:28 | ~0s | `run_playbook_generate` 立即返回（ET 08:02 < 09:00 时间门） | `pipeline.py:2115` `_generate_hour < 9` |
| 20:02:28→20:34:25 | **31.4min** | `run_playbook_topup_supplement`（未到窗即返回）+ `run_playbook_normalize_vol_buckets` **逐标的 ATR** | 段末 20:34:25 `盘前定桶落库 (17 条)`；段内唯一日志是 20:08:14 财报日历 |
| 20:34:25→20:34:26 | 1s | `run_playbook_morning_push` | `regime落库补全 17/17` + `✅ 盘前指令推送成功 (17条)` |
| 20:34:26→21:01:09 | **26.7min** | `macro_calendar_shadow_collect._build_snapshot`（8 个宏观指标外部 API，**无逐指标日志**） | 段内唯一日志是 20:58:25 财报日历；段末即下一轮 ping |

- **同窗复现（非偶发）**：2026-09-01 盘前轮 20:02:28→21:00:12 CST = **57.7min，形状完全相同**（llm_scan 行 → 长静默 → 生成开始）。
- **盘后窗同族**：2026-08-22 03:55:35 `futu通道影子 (TRADING)` → 04:02:09 `futu通道影子 (AFTER_HOURS) 采集完成`（**6.5min**）→ 04:02:09 `盘后追踪开始` → 04:30:44 `[Watchdog.heal] restart: llm_strategy_loop stale 1829s`。
- **告警面统计（近半月全部日志）**：`llm_strategy_loop 已 Ns 无心跳` **24 条 = CST 21 时窗 12 条（盘前）+ CST 04 时窗 12 条（盘后）**；heal 整服重启 2 次（08-22 04:30:44 盘后 / 09-03 21:31:38 盘前）。→ 病灶跨两个阶段、复发，不是"LLM 慢"。
- **单步常态基准**：重启后 21:32:19→21:38:07 `✅ 生成完成: 72/72 条指令入库` = **5.8min**；而 21:01:09 那次 generate 跑了 **30.5min 仍未完成**即被 heal 打断 → 长尾非常态，归因未定（该轮零错误日志＝静默），留给 §六 D4 的轮次遥测继续观测。

### 4.2 修法（采纳 (a) 心跳内嵌 + (b) 阈值配置化；(c) 转 §六 D4）

- **(a) 轮内护航** — `app/core/scheduler_core.py` 新增 `_wd_ping()` / `_wd_round_cfg()` / `_wd_open_round_heartbeat(phase)` / `_wd_close_round_heartbeat()`：非 `TRADING`·非 `WEEKEND` 阶段的**整轮迭代**期间，每 120s 代循环补一次 ping；轮顶"结算上一轮（`stop.set()` + `join(5s)` + 落轮次耗时遥测）→ ping → 开本轮护航"，循环退出再结算一次 → **全程最多 1 个 `WdRoundHeartbeat` 线程**（同日 D3 泄漏教训）。
  - **为什么整轮护航而不是逐 callee 埋点**：4.1 表明长步分散在 ≥3 个 callee（定桶 ATR / 宏观影子 / 富途通道影子），且它们**自身零进度日志**；逐个埋点既漏（未枚举到的步骤照样饿死心跳）又要改 cron 共用的脚本。整轮护航对"哪一步慢"不敏感。
  - **两重收敛，防止把误报换成静默死亡**（DEB-20260831-silent-death-hardening 纪律）：① **预算** `scheduler.watchdog_long_round_budget_sec=2700`（实测最长静默单步 31.4min → 1.4× 余量），超预算即**停止补 ping** 并 `logger.error` → 判定权交回 watchdog；② **阶段**：轮次跨入 `TRADING` 立即停 ping + `send_critical`（"入场执行被阻塞"）。无脑定时 ping 会让真卡死永远检不出，故必须有这两条。
  - **TRADING 阶段不护航**：入场执行 `run_playbook_auto_execute` 在同一循环内，卡死要的是紧检出（600s）+ heal；出场另有独立快循环 `playbook_exit_loop`（阈值 120s，`scheduler_core.py:2670`），**完全不受本改动影响**。
- **(b) 阈值配置化，默认值不变** — `wd.register("llm_strategy_loop", stale_threshold_sec=_llm_loop_stale_sec)`，读 `scheduler.watchdog_llm_loop_stale_sec`，**默认仍 600s**。立案建议的"600s→3600s"**已否决**：heal 触发点 = 阈值 × 3（`watchdog._heal_multiplier=3.0`），3600s 会把真卡死的整服重启从 30min 推到 **3h**，而误报已由 (a) 消除，无需靠放宽阈值。只留旋钮（改后需重启，注册发生在启动期）。
- **回滚**：`scheduler.watchdog_long_round_enabled=false` + 重启 = 完全恢复原行为；阈值键删掉或写 600 = 原硬编码值。

### 4.3 验证

- **单测 `tests/test_watchdog_long_round_heartbeat.py` 16 passed**：盘前轮护航补 ping ≥2 次 · `TRADING`/`WEEKEND` 不开护航 · 预算耗尽后 ping 冻结且 `stopped_reason="预算耗尽"` · 跨入 TRADING 后 ping 冻结 + `send_critical` 收到"入场执行被阻塞" · `enabled=false` 时零 ping（原行为） · **连续开 5 轮任一时刻仅 1 个护航线程、结算后归零** · `running=False` 时线程自终止 · 重复结算不抛 · 源码契约（轮顶"结算→ping→开护航"顺序、循环退出必结算、阈值注册走配置键） · 运行期读的键名与 YAML 逐字一致（防拼写漂移导致"配置改了代码读不到"）。
- **端到端时序复现（真 Watchdog，1:1200 缩放）**：阈值 0.60s / 护航 0.12s / 单轮 2.90s ↔ 生产 600s / 120s / 58min，循环调 `Watchdog._check_all()`：**护航臂 0 条 stale 告警**，**对照臂（同一轮次关掉护航）复现 stale 告警** → 既证明误报消除，也证明测试确实压在事故路径上（不是空跑绿）。测试内 `_heal_enabled=False` + `WATCHDOG_AUTO_HEAL=0` + `send_critical` 打桩 → 绝不发推送、绝不 `systemctl restart`。
- **回归**：`pytest -k "scheduler or watchdog or playbook or heartbeat or private_router"` → **403 passed / 1 skipped / 0 failed**。
- **配置面**：`audit_config_consistency(yaml)` 只返回既有 3 条 `signal_fusion.us.*` INFO（与本次无关）；运行中服务 **23:27:50 热重载 ✅ / ConfigContainer 已同步 / PlaybookConfig·FeatureFlags 快照失效**，改后窗口 ERROR·Traceback = 0，`systemctl is-active = active`（新增 3 个键对旧码不可见 → 热重载无副作用）。
- **生效条件（重要）**：`.py` 改动 → **需重启生效，盘中未重启**。工作区另有并行 Agent 未提交的 `main.py` / `app/execution/broker_adapter.py` / `app/web_dashboard/order/executor.py` / `app/pipeline/order_executor.py` / `app/risk/position_exit_monitor.py` / `app/storage/daily_log_store.py` / `app/data/market_data_router.py`，重启会把它们一并带上 → 建议**收盘后（ET 16:00 / CST 04:00）**由持有那些改动的会话统一重启，并跑 `PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py`（期望 7/7）。

### 4.4 已知代价（诚实披露，转 §六 D4 裁决）

护航在**"盘前轮次跨开盘"**这一场景下会**推迟**恢复：09-03 实况是 09:01:09 起静默 → 09:31:38 heal（跨开盘后仅 98s，因为静默已从 09:01 累积）；修法后护航会补 ping 到 09:30 相变才停 → 告警 ~09:40 → heal ~10:10（跨开盘后 ~40min），**入场阻塞时间变长**。换来的是：轮次不跨开盘的日子（如 09-01，轮次 08:02→09:00 收尾）误重启**完全消除**（近半月 24 条告警 / 2 次 heal 的绝大部分属此类）。这条取舍涉自愈动作与入场时点，**本档不代裁** → §六 D4。

## 五、【已修 D3(b) · 待重启生效】服务内**每笔下单新建一套 MarketDataRouter** → OpenD 连接 + 守护线程泄漏

- **实证**: 日志中每次 `re-price/盘中触发` 后必现 `TwelveData 初始化完成 → Finnhub 初始化完成 → [MarketDataRouter] Futu LV3 提供商已初始化 → [MarketDataRouter] 初始化完成 → [FutuLV3] 正在连接 OpenD → ✅ 连接成功 → [FutuLV3][Watchdog] 后台健康守护已启动`。计数：**21:32 重启后 50 分钟内 17 次**（今日全日 19 次；09-02 全日 26 次）。进程侧实测 90 秒内 `sockets 33→34 / threads 71→73`；体检时点 **32 条 OpenD 连接 + 73 线程**，fd 61/65535（暂无耗尽风险），无 OpenD 拒绝连接记录。
- **根因**: `app/playbook/playbook_service.py:1257` 抓 arrival price 用 `_MDR()`（**每单新建**），`:1458` 成交价兜底同样 `MarketDataRouter()`。而 `app/data/market_data_router.py:1157-1180` 早有 2026-07-11 的进程级共享单例 `get_market_data_router()` / `set_market_data_router()`，注释写明"此前 market_regime/factor_engine 直接 new MarketDataRouter()，导致每个 playbook 周期都新建一个 Futu LV3 连接 + 30s watchdog 线程（旧实例被线程引用无法回收 → 线程泄漏 + OpenD 连接 churn）"。`_start_health_watchdog` 的 `_watchdog_started` 只是**实例级**去重，挡不住跨实例累积；守护线程 `while True` 无停止位且强引用 self → **实例与其 socket 永不回收**。
- **为何本次不热修**：该 `_arr_ctx` 不只喂遥测，还喂两条**会改变行为**的下游 —— ①`gap_dive_halve_enabled=true`（config:1256）的 P1 深跳空降仓（改 `position_size`）②`pretrade_quote_gate_enabled` 报价健康门（可直接 `return success=False` **拒单**）。而共享 router 走**订阅推送**路径，`futu_lv3_provider.py:426-431/791-795` 明示推送路径无真盘口、兜底 `bid=ask=last`；新建 router 走**快照**路径可能拿到真 bid/ask。换源即同时动"滑点尺子（arrival=(bid+ask)/2）+ 报价门严格度 + 降仓触发"，属行为变更 → **C4 影子先行 + 一次一变量**，不在盘中顺手改。
- **候选修法（需裁决选一）**：
  - (a) **SSOT 归位**：两处改 `get_market_data_router()`（符合 2026-07-11 既定规矩，零新连接）。代价：arrival price / gap 探针 / 报价门的**报价来源改变**（快照→推送，bid=ask 退化路径），须按宪章二.2 做**新旧基线切分**并给 `slippage_bps` 加来源标记，否则滑点尺子静默混基。
  - (b) **语义保持的私有单例**：模块级缓存一个与今日构造参数完全相同的 router（`MarketDataRouter()` 默认参数），只把"N 个实例"收敛成"1 个实例"，报价口径逐字不变。代价：多留 1 条 OpenD 连接 + 1 个守护线程，且与 SSOT 规矩并存两套入口（需注释说明为何不用共享单例）。
  - (c) **只堵线程/连接不堵实例**：给 `FutuLV3Provider` 加停止位 + `__del__`/弱引用守护，让废弃实例可回收。代价：改 SDK 封装层，风险面最大。
  - 建议 **(b) 先止血（语义零变更）→ 再按 (a) 做带基线切分的归位**。**本档不代裁**。

---

### 5.1 修法（采纳 (b) 语义保持的私有单例；(a) SSOT 归位仍属行为变更，不在本批）

- `app/playbook/playbook_service.py` 新增模块级 `_private_router` + `_private_router_lock` + `_get_private_router()`（懒创建、双重检查锁）+ `_private_router_quote(symbol)`；两处下单路径（`:1257` arrival price、`:1458` 成交价 quote 兜底）改走它 → **"N 个实例"收敛成"1 个实例"**（1 条 OpenD 连接 + 1 个健康守护线程，而非每单一条）。
- **报价口径零变更的机器判据**：构造参数与原先逐字相同（`MarketDataRouter()` 全默认）→ 私有实例**从不 `subscribe()`**，其 `_subscribed_symbols` 恒空 → `futu_lv3_provider.get_quote` 的 push 分支（`:807-812`）与订阅读分支（`:824-851`，`bid=ask=last` 退化）都不会命中 → **永远走快照分支 `get_market_snapshot`（带真 `bid_price`/`ask_price`）**，与今日逐单新建同源同口径 → 滑点尺子（`arrival=(bid+ask)/2`）、报价健康门、P1 深跳空降仓三者严格度均不变。
- **补一处立案时未识别的语义差（新鲜度）**：长生命周期实例会带 provider 侧报价缓存（Futu 5s / Finnhub·TwelveData 300s），而"每单新建"永远空缓存 → 同一次下单内（arrival price → 确认窗最快 <5s 返回 → 成交价 quote 兜底）会复用 ≤5s 旧价，给记账/滑点尺子引入无法归因的新鲜度差。修法：每次取价前按三家 provider 的**真键格式**（`quote:{sym}` / `fh_quote_{sym}` / `quote_{sym}`，出处 `futu_lv3_provider.py:815` / `finnhub_provider.py:215` / `twelvedata_provider.py:271`）**精确清除该 symbol** 的报价缓存（只清本标的，不动其它 symbol 的缓存；provider 缺 `_cache`/`_cache_time` 或为 `None` 时 `getattr` 守卫跳过）→ 与"每单新建"逐字等价。
- **明知故犯的第二入口，已注明理由**：`market_data_router.py:1157-1180` 的进程级共享单例（2026-07-11 立的规矩）本次**不用**，因为共享实例的监控池标的都在 `_subscribed_symbols` 内 → 换过去即换源（快照→推送、`bid=ask=last`）＝C4 行为变更，须按宪章二.2 做新旧基线切分 + 给 `slippage_bps` 加来源标记（＝原文 (a)）。代码注释与 `tests/test_playbook_private_router.py::test_helper_never_touches_the_shared_push_router` 共同钉住这条边界。

### 5.2 验证

- **单测 `tests/test_playbook_private_router.py` 10 passed**：3 次取价只建 **1** 个实例（原实现建 3 个）· 构造 `args==() and kwargs=={}`（口径不变的机器判据）· **4 线程并发首用只建 1 条连接**（双检锁有效，对应"不同标的并发下单"）· 缓存清除生效 · **清除是外科手术式**（无关 symbol 的缓存保留，不是 flush）· provider 缺 `_cache`/`_cache_time`/整个为 `None` 时取价不受影响 · 取价异常照旧上抛，由下单路径既有 `try/except` fail-open（行为同前）· **AST 棘轮**：全文件仅 1 处 `MarketDataRouter` 构造且在 `_get_private_router` 内（`_MDR()` 已消失，注释里提到类名不计入）· 两处调用点都改走 `_private_router_quote` · 助手源码不引用 `get_market_data_router`。
- **回归**：与 D2 同一批 `403 passed / 1 skipped`。
- **泄漏面现状（修码未生效，未重启）**：PID 128545 uptime 2h04m，`sockets 35 / threads 78 / fd 65 of 65535 / RSS 258MB`；重启后 router 新建 **18 次**，socket 数 22:20 → 32、23:04 → 35、23:41 → 35（增速随下单频次收敛），近 1 小时 Futu 连接类错误（`连接失败` / `init connect fail` / `所有数据源均失败` / `ret=-1`）**0 条** → **收盘前无 fd/连接耗尽风险**，修法随收盘后重启生效。

### 5.3 未做（转原文 (a) 与 §七 遗留）

SSOT 归位（两处改用 `get_market_data_router()`、删掉第二入口）仍需 slippage **新旧基线切分 + 来源标记**，属 C4 行为变更 → 不在本批；(c)（给 `FutuLV3Provider` 加停止位让废弃实例可回收）改 SDK 封装层、风险面最大，同样不动。本批只止血"每单新建"这一处增长源；其余历史新建点（`market_regime` / `factor_engine` 等）已在 2026-07-11 走共享单例，不在本档范围。

## 六、【D4(a) 已修 · 待重启生效；(b)(c) 待裁】盘前轮次跨开盘 → 入场执行被阻塞（D2 修法的边界，09-03 实况）

- **实证**：盘前轮次 21:01:09 CST（ET 09:01:09）进入 `run_playbook_generate`，到 21:31:38（ET 09:31:38）仍未完成 → **开盘后 90 秒内 `_llm_strategy_loop` 一直停在盘前分支，TRADING 分支的 `run_playbook_auto_execute`（入场触发 / re-price / 下单）一次都没跑**。今日的误重启恰好打断了它：09:32:14 重启完成 → 09:38 起逐笔入场（`trade_records` 1438-1447）。即"入场按时恢复"这件事今天是**靠一次误报的整服重启救回来的**。
- **常态基准**：同一个 generate 在重启后 21:32:19→21:38:07 只用 **5.8min**（72/72 条）→ 09:01 那次的 **30.5min 未完成**是长尾；该轮**零错误日志**（静默），归因未定（20:48:39 那条 `init connect fail: Abnormal event timeout` 属上一轮，不在本轮）。
- **与 D2 修法的关系（取舍明示）**：护航在相变（PRE_MARKET→TRADING）时停 ping + critical 告警，判定权交回 watchdog → 恢复动作仍是 heal 整服重启，但时刻从"跨开盘后 ~98s"变成"跨开盘后 ~40min"（告警 600s + heal 3×600s）。**D2 消除了不跨开盘日子的误重启（近半月 24 条告警 / 2 次 heal 的绝大多数），代价是跨开盘日子的入场恢复变慢。**
- **候选修法（可叠加）**：
  - (a) **步骤级取消**：盘前分支每步前查阶段，一旦已 TRADING 就跳过剩余盘前步骤（定桶 / 盘前推送 / 宏观影子 / 补量在 TRADING 分支都有同名 catch-up 调用，跳过不丢功能）→ 把跨界阻塞收敛到"当前正在跑的那一步"。改动小、风险低，但对 09-03 这种"单步 generate 跑 30min"**无效**。
  - (b) **跨开盘即主动 heal**：相变瞬间直接触发自愈重启（把今日的意外救援变成设计）。恢复最快（~40s），代价=**主动在开盘时刻重启**（今日实测伴随 `⛔ 交易时段拒绝 stop_auto_loop`、退订 TICKER/ORDER_BOOK 失败 2 项、`LLMStrategyLoop 未能 10s 内退出`）。
  - (c) **重活外移**：盘前数据采集链（定桶逐标的 ATR / 宏观日历影子 / 富途通道影子全量拉取）移到 worker 线程，循环线程永不被它阻塞 → 根治，但要处理并发 / 幂等 / 停机 join，改动面最大，属 C4 影子先行范围。
  - **建议**：(a) 可立即做（低风险、收敛多步跨界）+ (c) 另案影子先行；(b) 不推荐（主动在开盘时刻重启的风险面大于收益）。**前置**：先用 D2 已落的轮次遥测（`[Watchdog·护航] <phase> 轮次耗时 Ns (补心跳 M 次)`）攒 3-5 个交易日，量清"哪一步"长尾，再决定 (c) 的切分点。

### 6.1 (a) 已落地（2026-09-03 23:55 CST，用户令"继续"）——盘前步骤级取消

- **修法**：`app/core/scheduler_core.py` 新增 `_premarket_overrun(step_name)`（判据：`get_market_phase() == TRADING` → True；**阶段探测异常 → False**，保守不误跳步骤），并在盘前分支四个**非必需**步骤前各插一处 `if self._premarket_overrun(...): continue` → 回轮顶重新取 phase 进 TRADING 分支，把"跨界阻塞 = 剩余四步之和"收敛成"= 当前正在跑的那一步"。`run_playbook_generate`（供给、入场前提）**不守卫** —— 跳了只是换到 TRADING 分支跑同一件事，不省时间。
- **零损失契约（逐个核过调用点，并由测试钉住）**：`generate` / `morning_push` / `topup_supplement` / `normalize_vol_buckets` 在 **TRADING 分支都有同名 catch-up 调用**且各自幂等；宏观日历影子是 record-only 且自带 `06:00-09:29 ET` 窗口门。**每轮至多触发一次**（触发即 continue → 下一轮进 TRADING 分支，不再回盘前分支），故告警不会刷屏。
- **诚实标注收益分布**：真正有实测收益的是 `normalize_vol_buckets`（**无窗口门**，09-03 实测 31.4min）与 `morning_push`（未推送时需数秒）；`topup_supplement`（08:45-09:30/10:30-11:30 双窗口门）与 `macro_calendar_shadow`（06:00-09:29 窗口门）跨开盘**本就立即返回** → 这两处守卫属**防御性**（窗口日后被放宽时仍护住入场），不是今日收益点，已写进代码注释而不是只写在文档里。
- **残余（转 (c)）**：单步内部不可中断 → 若长尾出在"跨开盘那一刻正在跑的步骤"（09-03 正是 `generate` 跑 30.5min 未完成），(a) 救不了，仍需 (c) 重活外移 worker 线程。此期间 D2 的相变告警（`⚠️ [Watchdog·护航] ... 跨开盘仍未结束 → 入场执行被阻塞`）+ D4(a) 的 `[盘前跨界]` 告警共同保证**可见**，不再是静默。
- **验证**：新增 `tests/test_premarket_overrun_cancel.py` **8 passed** —— 判据（未开盘 False 且零告警 / 已开盘 True + 一条含步骤名与"入场"的可操作告警 / **阶段探测抛异常 → False** / 推送通道故障不影响返回值）· 覆盖面（四处守卫且都以 `continue` 收尾、**generate 之前无守卫且不被守卫**）· **零损失契约**（被跳过的 playbook 步骤在 TRADING 分支都有 `fn(self)` catch-up，缺一个当场红）· TRADING 分支确有 `run_playbook_auto_execute(self)`（否则取消毫无意义）。回归 `-k "scheduler or watchdog or playbook or heartbeat or private_router or premarket or overrun"` → **448 passed / 1 skipped / 0 failed**。
- **生效条件**：同 D2/D3，`.py` 改动 → **需重启生效，盘中未重启**。
- **(b)(c) 本档仍不代裁**：(b) 跨开盘即主动 heal（把今日的意外救援变成设计）—— 不推荐，主动在开盘时刻重启的风险面（今日实测伴 `⛔ 交易时段拒绝 stop_auto_loop` + 退订失败 2 项 + 线程 10s 未退出）大于收益；(c) 重活外移 worker 线程 —— 根治但涉并发/幂等/停机 join，属 C4 影子先行范围，建议先攒 3-5 交易日轮次遥测再定切分点。

## 七、遗留：conftest 硬闸应扩到 `sqlite3.connect`（需全量套件验证后再落）

§2.2 只登记了已知的那一条缝。**同类盲区仍在**：任何经 `sqlite3.connect(生产 data/*.db)` 的测试写入都不会被拦（硬闸只覆盖 `open`/`Path.write_text|write_bytes`）。建议实现：包裹 `sqlite3.connect`，路径落在生产 `data/` 下时检查后续 `execute/executemany` 的语句动词（INSERT/UPDATE/DELETE/REPLACE/DDL）→ 未登记缝即报错指路，只读连接放行。**未在本次落地**的原因：需跑全量套件（~110s，2452 例）枚举既有违例并逐条登记/改造，而盘中不宜跑全量（CPU 争用 + 可能新建 OpenD 连接 + 可能触发推送）；未经验证的硬闸入库会把下一次套件运行变成大面积红。→ 列入"下一步"，收盘后专项。

---

## 八、复现命令（可只读复核）

```bash
# 链路总览 + 冒烟
systemctl status ftseek-us --no-pager | head -12
PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py          # 期望 7/7 exit=0
# §2.1 僵死进程面
ps -eo pid,etimes,rss,cmd --sort=-etimes | grep -E "scripts/.*shadow" | grep -v grep
# §2.2 夹具签名计数（清洗后应为 0）
./venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/signal_snapshots.db');print(c.execute('select count(*) from regime_history where spy_ma200=500.0 and spy_price in (450.0,550.0)').fetchone())"
# §三 tripwire 误杀面
./venv/bin/python -c "import json;r=[json.loads(l) for l in open('data/tripwire_checker.jsonl')];i=[x for x in r if x.get('invalidated')];print(len(r),len(i));[print(x['ts'][:16],x['symbol'],x['detail'][:80]) for x in i[-6:]]"
# §四 误重启面
grep -nE "Watchdog.heal|无心跳" logs/ftseek_us.log | tail -12
# §五 泄漏面（连续采样应看到 sockets/threads 随下单递增）
echo "sockets=$(( $(ss -tn | grep -c '127.0.0.1:11111') / 2 )) threads=$(ls /proc/$(systemctl show -p MainPID --value ftseek-us)/task | wc -l)"
grep -c "MarketDataRouter] 初始化完成" logs/ftseek_us.log

# §四 D2 修法（重启后生效）：轮次遥测 + 护航线程数（应恒 ≤1）+ 阈值配置面
grep -nE "Watchdog·护航|无心跳|Watchdog.heal" logs/ftseek_us.log | tail -12
ps -T -p $(systemctl show -p MainPID --value ftseek-us) | grep -c WdRoundHeartbeat
./venv/bin/python -c "import yaml;s=yaml.safe_load(open('config/unified_config.yaml'))['scheduler'];print({k:v for k,v in s.items() if k.startswith('watchdog')})"
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_watchdog_long_round_heartbeat.py -q   # 期望 16 passed

# §五 D3 修法（重启后生效）：新建次数应停止增长（重启后仅 1 次 = 私有单例首次懒创建）
grep -c "MarketDataRouter] 初始化完成" logs/ftseek_us.log
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_playbook_private_router.py -q          # 期望 10 passed

# §六 D4 观测面：盘前轮次是否跨开盘（重启后看轮次耗时遥测 + [盘前跨界] + 首笔入场时刻）
grep -nE "轮次耗时|盘前跨界|auto_execute 开始" logs/ftseek_us.log | tail -10
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_premarket_overrun_cancel.py -q        # 期望 8 passed
./venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/ftseek_us.db');print(c.execute(\"select min(created_at) from trade_records where date(created_at)=date('now')\").fetchone())"
```

---

### 变更记录

- 2026-09-03 v3（Codex，用户令"继续"）：**§六 D4(a) 落地** —— 盘前步骤级取消 `_premarket_overrun()` + 四处 `continue` 守卫（`generate` 不守卫），把跨界阻塞从"剩余四步之和"收敛到"当前正在跑的那一步"；零损失契约（被跳步骤在 TRADING 分支均有同名幂等 catch-up）与"阶段探测异常 → False"的保守方向由 `tests/test_premarket_overrun_cancel.py`（8 passed）钉住；**诚实标注收益分布**：实测有效的是 `normalize_vol_buckets`（无窗口门，31.4min）与 `morning_push`，`topup`/`macro` 自带窗口门 → 守卫属防御性（已写进代码注释）；**残余转 (c)**：单步内部不可中断，09-03 那种"generate 单步跑 30.5min 跨开盘"仍救不了，但已由 D2 相变告警 + `[盘前跨界]` 告警保证可见不再静默。回归 448 passed / 1 skipped。(b)(c) 仍不代裁。需重启生效，盘中未重启。
- 2026-09-03 v2（Codex，用户令"继续执行修复"）：**§四 D2 与 §五 D3 落地**（采纳 D2 (a)+(b) / D3 (b)）。D2：新增轮内护航（非 TRADING/WEEKEND 阶段每 120s 补 ping，预算 2700s + 相变即停两重收敛；TRADING 不护航以保紧检出；全程 ≤1 线程）+ 阈值配置化（默认仍 600s，**否决**立案建议的 3600s，因 heal=阈值×3 会把真卡死重启推到 3h）；**并勘误立案归因**（4.1）：耗时的不是 LLM 新闻推荐（`after_hours_scan_enabled=false` → 立即返回），而是盘前数据采集链（定桶逐标的 ATR 31.4min + 宏观日历影子 26.7min），09-01 同窗同形复现、08-22 盘后窗同族 heal，近半月告警 24 条（盘前 12 / 盘后 12）。D3：下单路径两处改走模块级私有单例 + **每次取价前精确清除该 symbol 的 provider 报价缓存**（补立案未识别的新鲜度差），构造参数全默认 → 恒走快照路径（真 bid/ask），滑点尺子/报价门/降仓触发口径零变更；明知故犯的第二入口在注释与测试里注明理由与切回路径。验证：新增 `tests/test_watchdog_long_round_heartbeat.py`（16 passed，含**真 Watchdog 1:1200 缩放端到端复现** — 护航臂 0 告警 / 对照臂复现告警）+ `tests/test_playbook_private_router.py`（10 passed，含 AST 棘轮与并发首用）；回归 `-k "scheduler or watchdog or playbook or heartbeat or private_router"` **403 passed / 1 skipped**；配置热重载 23:27:50 ✅、`audit_config_consistency` 无新增项、改后 ERROR=0。**新暴露 §六 D4 待裁**（盘前轮次跨开盘 → 入场阻塞；D2 使该场景恢复时刻从跨开盘后 ~98s 变 ~40min，取舍不代裁，建议 (a) 步骤级取消 + (c) 重活外移影子先行，不推荐 (b) 主动开盘重启）。**两者均 `.py` 改动，需重启生效，盘中未重启**（工作区另有并行 Agent 未提交 7 个 `.py`）→ 建议收盘后统一重启 + 冒烟 7/7。零交易行为变更、零退出参数变更（`exit-de49b1299290` 不变）、未提交 git。
- 2026-09-03 v1（Codex）：立案。§二 三项已修并实测验证；§三/§四/§五 三项待裁，均附实证与建议修法，**未热改任何 enforce 门 / 自愈动作 / 报价口径**。
