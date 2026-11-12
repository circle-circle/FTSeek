# DEB-20261111 — 日志链路只读巡检与优化队列

- 状态：**只读审计完成；优化项待裁，未实施代码/配置/DB 变更**
- 状态更新：**Batch A cron 侧观测/效率项已实施（§7）；Batch B 批量报价反事实 record-only 已实施（§8）；Hermes 预检仍暂缓**
- 时点：2026-11-11 20:02 CST（2026-11-11 07:02 EST）；美股盘前。
- 范围：主服务日志、EXT 入场/出场 cron、事件流采集、error_alert、启动冒烟、只读账本。
- 纪律：不改交易规则、不重启、不调用券商下单、不新增 cron/推送；测量缺口不等于 edge 改善。

## 0. 一句话结论

当前交易守护面健康：启动冒烟 7/7，7 个未平仓全部 `PLAYBOOK_SIM` 且 `synced`，`exit_intents` 354 全 `BOOKED`。
值得优化的不是交易参数，而是 **EXT 研究臂流动性取数失败被误标为市值缺失、财报 watchlist 加载器仍读已弃用 JSON、Hermes Worker 启动器隔离模式丢依赖、事件流空跑仍连 OpenD**。

## 1. 已确认正常项

| 面 | 证据 | 判定 |
|---|---|---|
| 服务 | `systemctl` active，PID 2589190；`startup_smoke_check.py` 7/7 | OK |
| 持仓账本 | `trade_records` 未平 7 = `open_positions` 7；全 `PLAYBOOK_SIM` / `synced` | OK |
| 出场台账 | `playbook_exits.db exit_intents` 354 全 `BOOKED` | OK |
| EXT 出场 cron | orphan/closekey/rth_zero_fill ok；reaper 未检出账实不符 | OK |
| 主服务错误 | 当前档无 `] ERROR:` / `] CRITICAL:` / Traceback；error_alert 报 0 | OK |
| GC/内存 | 10min 汇总 RSS 192–198MB，最长停顿 103ms | OK |

## 2. 问题清单

### P1-1 · EXT 研究臂 `market_cap_missing` 实为流动性快照取数失败

- **证据**：
  - 本 UTC 日研究臂 pretrade 拒绝：premarket `market_cap_missing=604`、afterhours `229`；**833/833 全部 `quote=null`**。
  - 最新 premarket 轮：研究池 38，`research_market_cap_missing=27`；被拒含 TSLA/AMD/AVGO/QQQ/META/TSM 等大市值主流票。
  - 单票探针（cron 窗外）`US.TSLA` 返回 `bid=387.37 / ask=387.76 / total_market_val=1.50e12`，字段正常。
  - cron 窗口内探针触发 `获取市场快照频率太高，每30秒最多60次`。
- **代码锚点**：
  - `scripts/extended_hours_entry_executor.py:1254` `_liquidity_quote` 对 ret 错误/异常静默返回 `None`。
  - `scripts/extended_hours_entry_executor.py:721` `_entry_grade` 把 `quote=None → market_cap=0 → market_cap_missing`。
  - `scripts/extended_hours_entry_executor.py:1582` 研究臂逐票调 `_liquidity_quote`。
  - `scripts/extended_hours_entry_executor.py:1922` `_snapshot_candidates` 已批量取快照，但只解析 px/chg/vol/prev_close，未复用 bid/ask/market_cap。
- **判定**：高度疑似批量快照 + 逐票流动性快照叠加触发限流/取数失败，而非真实市值缺失。当前严格臂 `market_cap_missing=0`，主要扭曲研究臂供给与 RFC-149 样本。
- **建议**：
  1. 先零行为观测：把 `quote=None` 单列 `quote_unavailable`，记录 ret/error，不再混入 `market_cap_missing`。
  2. 数据修复：批量快照直接解析 bid/ask/total_market_val，研究臂复用批量结果，减少逐票请求。
  3. 若改变研究臂候选分布，按行为相邻项走影子/注册表，不直接放宽阈值。

### P1-2 · 财报日历 watchlist 加载器仍读已弃用 `watchlist_us.json`

- **证据**：
  - `config/watchlist_us.json` 不存在；`app/data/earnings_calendar.py:100` 每次 import 回退静态 36 票。
  - `unified_config.yaml watchlist.us_stocks` 现有 56 票 / 44 enabled；`watchlist_manager` 已迁移到 YAML。
  - 主服务与 EXT 每轮日志均出现 `[财报日历] 使用静态 KEY_US_SYMBOLS fallback`。
  - 富途主源仍返回 779 条日历，EXT `_earnings_calendar_rows` 消费全量行，**财报事件门本身未被静态 36 票限制**。
- **影响**：`get_watchlist_earnings` 的 sector 映射对动态票落 `Unknown`，进而影响财报期 ATR 倍数/事件供料；不是当前断流。
- **建议**：把 `_load_key_us_symbols` 迁到 `unified_config.yaml`（与 `watchlist_manager` 同源），补 source/as_of 日志与契约测试。因可能改变财报期 sector ATR，按行为相邻项先影子或小批验证。

### P2-1 · Hermes Worker 预检持续失败：启动器 `-I` 隔离模式丢 venv 依赖

- **证据**：
  - 近 4 个主日志档每次重启均出现 `Worker 预检失败`；当前档 3 次。
  - `hermes_worker.py --once ping` rc=1，子进程 `ModuleNotFoundError: No module named 'yaml'`。
  - 同一 venv 直接 `python3 -c 'import yaml'` 成功（PyYAML 6.0.3）。
  - 根因：`hermes_cli/venv_sync.py:717` relaunch 使用 `-I`，子进程丢失 venv site-packages。
  - 仓内 `call_agent_with_tools` / `call_agent_debate` 在 `agent_bridge` 外零现役调用方；复盘/DA 走 direct/skill。
- **判定**：当前主要是启动告警噪音与潜在工具路径降级，不是交易 P0。
- **建议**：修复 Hermes 启动器（避免 `-I` 丢依赖 / 固定 worker 解释器 / 升级 Hermes），或经六面零引用后退役该预检。预检失败日志应附 worker stderr，避免只留“降级”结论。

### P2-2 · 事件流 cadence 空跑仍打开 OpenD 连接

- **证据**：当前档 `cadence_skip=148`、`_init_connect_sync=197`、实际 ingest=49；每次 skip 仍连接。
- **代码锚点**：`scripts/event_stream_ingest.py:30` `_is_us_trading_day` 调 `holiday_calendar.is_trading_day`，cron 新进程冷缓存触发 Futu `request_trading_days`。
- **建议**：cadence 判定改用无网络 `is_us_holiday` 取反，或持久化交易日缓存；保持现有 fail-open 语义。可减少空跑连接与日志噪音。

### P3-1 · backfill yfinance 退市噪音 + error_alert 格式盲区

- **证据**：`backfill.log` 本日 32 条 `[ERROR] $XXX: possibly delisted`；`error_alert` 报 0。
- **原因**：`error_alert.py:83` 只认 `] ERROR:` / `] CRITICAL:`；yfinance 裸 `[ERROR]` 不匹配。已有窄豁免要求 `[yfinance]` 标记，但当前行不带。
- **建议**：对退市/无数据票预过滤或降级 yfinance logger；若希望真实错误可告警，统一日志格式。回填最终统计正常，非数据断供。

### P3-2 · Google News RSS 偶发超时

- **证据**：本日 2 次 `GOOGLE_NEWS_RSS status=ERROR latency≈52s`，其余轮 OK。
- **建议**：先观察频率；若持续，再考虑查询并发/缩短 timeout/重试。当前不构成立案级故障。

## 3. 已知不重复立案项

- invariant W7×40 / I4×14：已在 `DEB-20261111-full-chain-accuracy-inspection` 登记。
- `config-diff` 旧强度死键、启动 bridge 不存在（DA 09:00 前）、Reconciler 空签名保护：已知噪音/安全降级。
- Watchdog 慢轮次护航：判据字典 D1，无丢入场不重开。

## 4. 优化队列（一次一变量）

1. **Batch A 观测/效率（零交易行为）**：EXT `quote_unavailable` 单列 + ret/error 日志；事件流 cadence 无网络判定；Hermes 预检附 stderr；backfill 噪音治理。
2. **Batch B 数据供给（行为相邻，须影子/注册表）**：EXT 批量快照复用 bid/ask/market_cap；财报 watchlist 加载器迁 YAML。
3. **Batch C 治理（六面零引用后）**：Hermes Worker 修复或退役。

## 5. 可复现检查

```bash
systemctl show ftseek-us -p ActiveState -p MainPID --no-pager
PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py
rg -n 'Worker 预检失败' logs/ftseek_us.log
timeout 25 /root/.hermes/hermes-agent/venv/bin/python3 scripts/hermes_worker.py --once '{"id":"probe","method":"ping"}'
rg -c 'cadence_skip|_init_connect_sync' logs/event_stream_ingest.log
tail -1 data/extended_hours_pretrade_gate_rejects.jsonl
rg -n '使用静态 KEY_US_SYMBOLS fallback' logs/ext_entry.log logs/ftseek_us.log
```

账本只读：`sqlite3.connect('file:data/ftseek_us.db?mode=ro', uri=True)` / `file:data/playbook_exits.db?mode=ro`。

## 6. 验证与未验证

- 已验证：启动冒烟 7/7；7 仓 synced；354 intent BOOKED；EXT 拒绝台账 quote 全 null；TSLA 单票探针正常；Hermes worker ping 复现失败；事件流 skip/connect 计数。
- 未验证：未改代码/配置/DB；未重启；未调用券商下单；EXT 限流根因需补 ret/error 日志最终确认；财报 sector ATR 影响未量化；Hermes 修复/退役未实施；未跑全仓测试。

## 7. 2026-11-11 20:14 Batch A cron 侧实施回执（用户裁「继续」）

- **已实施（零交易行为）**：
  1. EXT：新增 `_grade_from_quote`，`quote=None` 单列 `quote_unavailable`；`_liquidity_quote` 对 ret 错误/异常/空快照打日志，invalid book 走 debug；严格臂主闸计数行契约不动，异常另起 `取数异常` 行。
  2. 事件流：`_is_us_trading_day` 改用无网络 `is_us_holiday` 取反，cadence 空跑不再触发 Futu 交易日 API。
  3. backfill：窄过滤 yfinance `possibly delisted; no price data found` 噪音，保留其它 yfinance 错误。
- **live 确认**：20:12 EXT tick 日志直接打出 `liquidity quote failed ... ret=-1 error=获取市场快照频率太高，每30秒最多60次`；研究臂拒绝细分变为 `research_quote_unavailable=26`，pretrade 台账 `quote_unavailable=26`。P1-1 根因由“高度疑似”升级为**已确认限流/取数失败误标**。
- **事件流验证**：手动 `--cadence` 探针 rc=0，stderr 仅 `cadence_skip`，无 `_init_connect_sync`。
- **测试**：`tests/test_extended_hours_entry_executor.py + test_event_stream_ingest_cadence.py + test_backfill_yfinance_noise.py` = **52 passed**；`test_ext_entry_research_arm.py + test_ext_entry_research_sources.py` = **24 passed**；三脚本 `py_compile` OK；`git diff --check` OK。
- **已知存量**：研究臂测试触发已登记生产库写入豁免 5 次（capital_state 等），非本轮新增写入点。
- **未实施 / 暂缓**：Hermes 预检附 stderr 需改 app/main 并重启，受在途 WIP 约束暂缓；EXT 批量快照复用 bid/ask/market_cap、财报 watchlist 加载器迁 YAML 属 Batch B 行为相邻项，未动。
- **边界**：未改下单/出场阈值、票面、cron、推送、DB schema；EXT/事件流/backfill 为独立 cron 脚本，下轮自动生效，未重启主服务。

## 8. 2026-11-11 20:37 Batch B 批量报价反事实 record-only 回执（用户裁「继续」）

- **范围**：只把 `_snapshot_candidates` 已取回的批量快照解析出 `_batch_quote`（bid/ask/bid_vol/ask_vol/total_market_val），在研究臂下游闸后追加反事实诊断；**不接入 live 门控、不改严格臂、不新增网络请求**。
- **落点**：复用既有 `data/extended_hours_pretrade_gate_rejects.jsonl` 拒绝明细 `batch_quote_cf` + 既有 `data/ext_entry_research_funnel.jsonl` 可选 `batch_quote_cf` + 研究臂细分计数。**不新增独立 shadow 产物 / 注册表实验项 / env / cron / 推送 / DB schema**；若后续要把批量报价接入 live 门控，另立 RFC 并按 KR4 注册。
- **反事实口径**：`scope=grade_window_pretrade`，只对照市值/换手档位、入场窗、价差/深度/漂移/费用闸；**不评估 structure/VWAP、不模拟下单**。`would_change` 仅表示 quote-gate 层会推进/拦截，不等于最终成交变化。
- **live 确认（20:32 CST tick）**：研究池 37，`batch_quote_cf` 33 条，`research_batch_quote_available=33`，`research_quote_unavailable=27`，`research_batch_quote_reject_would_advance=7`，`research_batch_quote_pass_would_block=0`，`ordered=0 / research_ordered=0`。7 条限流拒绝可被批量报价推进到 structure 前（AMD/SNDK/AVGO/MU/ASML 等）；其余仍被 turnover/depth/window/market_cap 正常拦下，不是盲目放宽。live quote 成功行与 batch quote 字段逐字段一致（mismatch=0）。
- **live 复核（20:37 CST tick）**：研究池 34，`batch_quote_cf` 30 条，`research_batch_quote_available=30`，`research_quote_unavailable=24`，`research_batch_quote_reject_would_advance=6`，`research_batch_quote_pass_would_block=0`，`ordered=0 / research_ordered=0`；研究漏斗末 40 行中 30 行带 `batch_quote_cf`，可按票追溯（如 INTC quote_unavailable → batch pretrade pass；META quote_unavailable → batch insufficient_ask_depth）。主服务 PID 2589190 未变，7 仓未平。
- **测试**：`tests/test_extended_hours_entry_executor.py + test_ext_entry_research_arm.py + test_ext_entry_research_sources.py + test_rfc193_order_query_poll_cache.py + test_event_stream_ingest_cadence.py + test_backfill_yfinance_noise.py` = **95 passed**；`py_compile` OK；`git diff --check` OK。相邻 EXT 入口子集 93 passed / 2 failed，失败为 HEAD 已存在的 `test_ext_entry_short_arm.py` stub 缺 `prior_move` 签名失配，非本轮引入。
- **已知存量**：测试框架生产库写入豁免 5 次（capital_state 等），非本轮新增写入点。
- **未实施 / 暂缓**：批量报价 live 复用未毕业；财报 watchlist 加载器迁 YAML、Hermes 预检修复/退役仍暂缓。未重启主服务；EXT cron 已自动加载。
- **下一步**：观察至 2026-11-13 source 臂死线，按票-日-时段去重复核 `pass_would_block=0`、batch 覆盖率与限流恢复率；任何 live 复用须另案 RFC/影子裁决，不据本诊断直接改门。

## 9. 2026-11-11 22:28 · ERROR 突增核查：TwelveData HTTP 429 未熔断且误记 ERROR

- **触发**: 用户贴 error_alert：22:00→22:15 CST `rsshub_us.log` 5 条 ERROR，Top 为 `[TwelveData] 请求失败 time_series: HTTPError HTTP 429`。
- **源头**: `rsshub_us.log` 是 `rsshub_us_news.py` 给 root logger 挂的共享 FileHandler，UE scan / news 进程内的 MarketDataRouter/TwelveData 日志也落这里；不是 RSSHub 新闻源本身报错。22:10 UE premarket scan 对事件小票取日 K，Futu `ret=-1` 后 Router 回退 TwelveData，免费档 8/min 被短窗打穿。
- **根因**: `TwelveDataProvider._get` 的 `requests.HTTPError` 分支只处理 404，其余一律 `logger.error`；HTTP 429 在 `raise_for_status()` 阶段抛出，未进入 JSON-body 429 熔断分支，也未设置 `_rate_limited_until`。结果：预期限流被记成 ERROR 触发告警，且同实例继续请求。
- **影响**: UE scan 仍完成（22:10 浮现 7 标的），DA 主链早已 complete；部分小票日 K 缺失，未影响 Playbook 执行/下单。服务 active，8 仓在持。
- **落地**: `app/data/twelvedata_provider.py` HTTP 429 分支设置 10 分钟熔断并改 `logger.warning`；非 429 HTTPError 仍 ERROR。新增 `tests/test_twelvedata_429_breaker.py` 锁住“429 熔断 + 不再请求 + 非 ERROR”和“500 仍 ERROR”。
- **验证**: `pytest -q tests/test_twelvedata_429_breaker.py tests/test_class_share_symbol_form.py` = **12 passed**；`py_compile` OK；`git diff --check` OK。
- **未做**: 未重启主服务（在途 WIP 约束）；cron 下轮新进程自动生效，主服务内旧代码待重启窗口。未改跨进程令牌桶/日限额状态、Futu 日 K 配额、UE 候选选择或交易门。
- **下一步**: 观察下一轮 UE scan / error_alert 是否不再出现 429 ERROR；若主服务需生效，按重启前核 SQL + WIP 后统一重启。
