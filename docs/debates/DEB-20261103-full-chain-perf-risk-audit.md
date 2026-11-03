# 议题：全链路性能与风险审计（2026-11-03）——开盘冻结触发源、决策面新鲜度、执行面阻塞

> 关联：`DEB-20261103-open-freeze-and-perf-hotfix-review.md`（11-02/11-03 四个 hotfix 的
> 事后复核）、`DEB-20260910-logging-observability-batch`、`docs/circuit-breaker-map.md`、
> `docs/OKR-2026-11.md`。
>
> 数据截止：2026-11-03 12:10 CST（休市）。审计窗口：调度器/LLM 管线、数据层、
> 入场出场 + SQLite、推送/监控/定时任务、内存/长命对象 五个层。
>
> 本文性质：**只读审计**。除 §5 标注的两处口径修正（本文作者的落档订正）外，
> **未修改任何生产代码或配置**。所有处置建议均**未执行**，等用户裁决。

**状态**：已决策（审计完成；P0 三项待用户授权实施；文中所有数字标注了实测/推测）

## 状态

- [x] 进行中（五层审计 + 独立复核完成）
- [x] 待决策（P0-1/P0-2/P0-3 需用户授权后实施）
- [x] 已决策（口径修正已落档，见 §5）
- [ ] 已验证（今晚开盘后的现场复核未做，见 §7）
- [ ] 已关闭

## 0. 结论先行

1. **11-02 冻结的最可疑触发源不在进程内，而在"开盘分钟的 cron 踩踏"**：该分钟 **13 个 cron
   任务同秒启动**（journal 实证），每个 Python 解释器仅 import 就吃 67–100MB ⇒ 瞬时 ~1.1GB
   需求砸向只剩 1453MB 可用的宿主 ⇒ 内核回收缓存并**把服务冷页换出 239MB**（sysstat 实证）
   ⇒ 服务下一次全堆 GC 缺页 ⇒ 3.6 分钟进程级静默。**上一批 hotfix 只加固了受害者
   （upb/GC 阈值/freeze/trim），没有移除触发器，今晚会原样重演。**
2. **另一条被误读的事实**：那批 hotfix 赖以立论的"进程 1.45GB"极可能是**虚拟地址空间而非
   驻留内存**。实测当前进程 **VmSize/VmPeak=1.71GB，而 RSS 仅 66–148MB**；`/proc/maps` 里
   **956MB 是 glibc malloc arena 的 PROT_NONE 预留（15 段 × 64MB）+ 264MB 线程栈
   （33×8MB）**。⇒ `malloc_trim` 与"RSS≥600MB 才 trim"这类判据打在一个并未膨胀的量上。
3. **决策面存在真实缺陷**：VIX 缓存的"盘中 60s / 盘外 600s"分档**从未生效**（参数被当成
   "最小年龄"，真实上限是 provider 的 900s），且实时代理 VIXY 与日频 FRED **写同一个
   cache key** ⇒ 盘中可拿到 15 分钟前的 VIX，甚至把上周五的 VIX 当真值。VIXGuard
   （入场否决）与 regime 闸都读这条。
4. **执行面阻塞未被"执行优先"解决**：入场单笔成交确认同步阻塞至多 30s（`entry_confirm_max_wait_s`），
   N 笔 = N×30s 串行；"执行优先"只是把它从队尾搬到队首，被推后的是同一轮的其他步骤。
5. **四处结构性风险**：AFTER_HOURS 分支首步无异常隔离（可致 EOD 平仓整段跳过 ⇒ 持仓静默过夜）；
   盘中心跳只靠主循环推进（长步骤 ⇒ watchdog 自愈 `systemctl restart` 盘中重启）；
   非盘中尾睡实为 119s（注释写 60s）⇒ 跨开盘动作最多晚 ~119s；出场扫描带关联全表子查询
   （EXPLAIN 实测 `SCAN TABLE t2`）。

## 1. 方法

- 五个只读探查 agent 分层扫（调度器/LLM、数据层、入场出场+SQLite、推送/监控/cron、内存/长命对象），
  产出候选问题。
- **每条高危结论由审计方独立复核**：读代码、跑 `EXPLAIN QUERY PLAN`、合成年龄实验、
  `/proc` 实测、sysstat 历史、journal 实证、离线测 import 成本。
- agent 结论与代码事实**不一致时以代码/实测为准**（本文 §5.2 记录了两处修正）。

## 2. 高危发现（含独立复核证据）

### H1 开盘分钟 cron 踩踏（触发源嫌疑最大）
| 证据 | 数值 |
|---|---|
| 同秒起进程 | `journalctl` 2026-11-02 **22:30:02**，13 个 CRON 条目同秒：tripwire_checker / freeze_sentinel / event_stream_ingest / business_heartbeat / error_alert / sector_pulse / extended_hours_entry_executor / orderflow_forward_capture / exit_intent_reaper / deploy_freshness_check / extended_hours_exit_cron_gate / health_check … |
| 单任务成本 | 实测 `import futu` 84MB / `import pandas` 67MB / `import futu,pandas,requests` 95MB / `import app.core.scheduler_core` 100MB，各 1.0–2.0s |
| 宿主余量 | 总 3563MB，**可用 1453MB**，`swappiness=0` |
| 内存后果 | sysstat 22:30→22:40：**buffcache 134MB→28MB**、**swap 多用 239MB**；22:50 `%memused=93.45%`、`memfree=233MB` |
| 时间吻合 | 冻结起点 22:30:53 = 齐发后 **51 秒**；SIGKILL 22:34:05 |
| 为何是开盘分钟 | `*/5`/`*/10`/`*/15`/`*/30` 全在 `:00/:30` 重合，开盘分钟重合度最高 |
| 机理闭环 | 与"gen1 停顿 7259ms 只回收 288 个对象（时间花在缺页）"一致：换出的冷页在下次全堆 GC 时被 fault 回来 |

### H2 虚拟地址空间被当成"堆泄漏"（对既有叙事的修正）
- 实测：当前进程 `VmPeak 1715616 kB / VmRSS 65948 kB`；`/proc/maps` 620 段中 **48 段 `---p`
  匿名共 956MB（其中 15 段 × 64MB = glibc arena 特征）+ 33 段 8MB 栈 = 264MB**，
  真正 anon rw 仅 362MB。
- 根因：glibc 默认 `MALLOC_ARENA_MAX = 8×ncores = 16`，每个新线程竞争时建 64MB arena，
  线程退出后 arena 进空闲表**不归还**；而仓内**每标的新建 ThreadPoolExecutor**：
  `app/plan/factor_engine.py:526,687`（默认 max_workers）、`app/pipeline/stages/signal_stage.py:630`、
  `app/playbook/data_collector.py:255`、`app/catalyst/catalyst_debate.py:240` ⇒ 每轮扫描 32 标的
  ⇒ 线程churn ⇒ arena 阶梯式增长。
- 可动杠杆：systemd 加 `MALLOC_ARENA_MAX=2`（一行、零代码、可回滚）+ 把 per-call pool 改模块级池。
- **未验证**：11-02 当时是否有 `/proc` 快照 ⇒ "1.45GB 是 RSS 还是 VmPeak"无法确证（§5.1 已订正落档口径）。

### H3 VIX 缓存 TTL 分档失效 + key 冲突（决策面）
- 代码：`app/data/alphavantage_provider.py:248-266`（`_is_cache_valid`）、`:535`（`cache_ttl = 60 if is_trading else 600`）、
  `:583`（VIXY 写入）、`:603`（FRED 写入）、`:248`（provider `cache_ttl_seconds=900`）。
- 机理：`min_age_seconds` 是**最小年龄**（防抖），真实上限 = `self.cache_ttl` = 900s；
  `:537` 注释"命中即为真值"不成立（FRED 的 T-1 值也写同一 key `"vix"`）。
- **合成年龄实测**：30/90/300/700/880s → 全部"命中缓存"；950s 才失效。
- 消费面：`app/risk/position_exit_monitor.py:2368`、`app/core/position_sync.py:2895`（走单例）、
  `app/pipeline/signal_pipeline.py:440` 等（走 router 的 AV 实例，TTL 同为 900s）；VIXGuard 与 regime 闸读这条。
- **与上一批复核的冲突**：另一 agent 提交称"provider 内部按交易时段 60s|600s…不会让 VIX 变旧" ⇒
  **不成立**；且单例改造让 position_sync/exit_monitor 的 VIX 从"每次新鲜"退化为"最长 900s 陈旧"。

### H4 AFTER_HOURS 分支首步无异常隔离（可致持仓静默过夜）
- `app/core/scheduler_core.py:2524`（`_process_deferred_audit_queue()`）、`:2537`（`run_after_hours_llm_scan()`）
  调用点**无 try/except**；异常落到最外层 `:2684` → 跳过 `run_playbook_track`（EOD 平仓）→ 120s 后重试。

### H5 盘中心跳只靠主循环 ⇒ watchdog 自愈重启（盘中重启交易服务）
- `app/observability/watchdog.py:143-176`：`llm_strategy_loop` 心跳停更 > 阈值×3（阈值 600s ⇒ 1800s）
  即 `subprocess.run(["systemctl","restart","ftseek-us"])`，日限次。
- `app/core/scheduler_core.py:1093`：TRADING 阶段 `_wd_open_round_heartbeat` 直接 return（不建护航线程），
  唯一 ping 在轮首 `:2322` ⇒ 盘中任一步 >30min（定桶实测 31.4min=1884s）即触发。

### H6 入场确认 30s 同步阻塞（"执行优先"未解决的量）
- `app/playbook/playbook_service.py:2010-2014` + 轮询 `:2625-2654`（`sleep(0.5)`），
  `config/unified_config.yaml:1233` `entry_confirm_max_wait_s: 30`；每笔在 auto_execute 内**同步**等终态。
- 轮次实测：今日 04:45:51 → 04:49:00 → 04:52:01 → 04:55:11 → 04:58:20 = **3min09s/轮**
  （与 H8 的 119s 尾睡吻合）。DB 实测每日未执行 BUY/SELL_SHORT 5–14 条 ⇒ 最坏 70–420s。

## 3. 中危清单（带定位）

| # | 问题 | 位置 |
|---|---|---|
| M1 | 出场扫描关联子查询逐行全表扫（EXPLAIN: `CORRELATED SCALAR SUBQUERY / SCAN t2`；`trade_records` 无 futu_order_id 索引） | `exit_monitor_flow.py:88-96` + `pipeline.py:2519` |
| M2 | position_sync 事务横跨 Futu 网络调用（DELETE:1377 → 网络:1513 → commit:1814），持写锁；**当前日志 `database is locked` 命中 0** | `position_sync.py` |
| M3 | 成交确认轮询每 0.5s 写一次库（30s 内 60 次 UPDATE+commit） | `db.py:1115-1123` |
| M4 | 出场取价失败 = fail-open（该仓本轮不评估 SL/TP，第 3 轮才告警） | `exit_monitor_flow.py:360-379` |
| M5 | `playbook_instructions` 无 symbol 索引（EXPLAIN: `SCAN TABLE playbook_instructions`；4685 行 ≈33/天） | `db.py:296-311` |
| M6 | `is_playbook_open_position` 仍有 5 处逐 symbol 新连接+全表扫 | `pipeline.py:2463` |
| M7 | Futu LV3 双实例（router 自建 vs `get_futu_lv3_provider()`）⇒ 实测 7 条 OpenD 连接 + 双守护线程 + 行情口径分裂 | `market_data_router.py:125` / `futu_lv3_provider.py:2672` |
| M8 | TwelveData 限速器**每实例一份**（Finnhub 已进程级化，TD 未同步）；批量取价 `acquire(n)` 最长阻塞 120s | `twelvedata_provider.py:110/452` |
| M9 | AV 配额未计账旁路（裸 `requests.get` 绕过 limiter） | `earnings_calendar.py:238/650` |
| M10 | PushHub 队列**无界**；单条最坏 42s（钉钉+飞书×重试）；无队列深度指标 | `push/hub.py:107-109/226` |
| M11 | 推送去重**仅进程内**（每个 cron 脚本自建 hub 单例）⇒ 跨进程重复投递 | `push/hub.py:89` + `dedup.py:37-51` |
| M12 | `critical_alerts.flag` 双写者冲突（append vs 覆盖 + `os.remove`）⇒ 未送达证据可被抹除 | `push/channels.py:111` vs `utils/critical_alerts.py:108-134` |
| M13 | DB 全量备份排在**盘中 14:00 ET**（03:00 CST），含 event_stream `VACUUM`；`set -e` ⇒ 单库失败当日全停 | `db_backup.sh` |
| M14 | `health_check.sh` 每 10min 对 43MB 主库跑**全库 integrity_check** | `health_check.sh:40-47` |
| M15 | 冻结检出最坏 ~240s（90s 阈值 + 60s cron 量化 + 15s 相位探针 + 60s py-spy + 15s kill wait），且告警重试期间哨兵有 ~2min 盲窗 | `freeze_sentinel.py:84/298` |
| M16 | 非盘中尾睡 **119s**（注释写"每分钟"）⇒ 跨开盘动作最多晚 ~119s | `scheduler_core.py:2679-2683` |
| M17 | `@timed_phase` 未覆盖的长尾步骤（宏观日历影子 ≈490s 上界；`premarket_invalidation` 内外层都只 debug ⇒ 完全静默） | `scheduler_core.py:2517/2354` |
| M18 | auto_execute 的 180s 间隔门用**轮首时刻**比较 ⇒ 慢轮时变成"每轮都执行" | `scheduler_core.py:2325/1238` |
| M19 | `config-diff` 死键：`min_strength=60` 是死键，实际生效 `min_effective_strength_floor=50` ⇒ 配置宣称与实际不符 | `scheduler_core.py:176` |
| M20 | HermesWorker 每次启动预检失败降级为 Bailian 直连，WARNING **不含原因**（原因在 debug） | `main.py:486-494` |
| M21 | `run_open_price_refresh` 单分钟窗口 + 唯一调用点在 PRE_MARKET（轮周期 ~189s ⇒ 命中率约 1/3） | `llm_pipeline.py:1213` / `scheduler_core.py:2454` |
| M22 | 影子脚本 10 个每轮全表重扫 `trade_records`，且硬编码读 `ftseek_us.db`（实盘主库是 `ftseek_us_live.db`） | `stall_entry_shadow_tag.py:120` 等 |
| M23 | 无界容器：factor_engine 挂在函数对象属性上的 per-symbol 缓存、futu `_kline_data_cache` 过期不删、新闻去重 set 无 TTL、web_dashboard `_login_attempts` 读即建 | `factor_engine.py:507/572/640`、`futu_lv3_provider.py:387`、`newsnow_us_news.py:117`、`web_dashboard/app.py:28` |
| M24 | 记录型影子 JSONL 无留存治理（`futu_capital_flow.jsonl` 73MB/33万行；`exec_gate_audit` 7111 行；`trigger_miss_telemetry` 28288 行） | `scripts/data_retention.py` 只管 `cache/*.json` |
| M25 | `logrotate` 未覆盖 `logs/freeze_stacks/`；crontab `MAILTO=root` 但 postfix/sendmail 均 inactive ⇒ **cron 失败无人知晓** | `/etc/logrotate.d/ftseek-us` |
| M26 | `exit_lifecycle.list_unsettled` 读也取写锁（BEGIN IMMEDIATE）、无 LIMIT、排序无索引 | `exit_lifecycle.py:125-138` |

## 4. 正面确认（避免重复审计）

- 入场幂等链完整（CAS + per-symbol Lock + 跨进程 flock + `client_order_id` 带 attempt）；
  出场幂等由 `exit_intents` UNIQUE + 部分唯一索引兜底；快/慢循环单宿主互斥有配置硬保证。
- `SubscriptionManager.ensure_subscribed` 增量幂等，无重复订阅；主循环线程只建一次。
- 全仓 `deepcopy` 0 处；playbook 侧模块级缓存有上限或 TTL；`atr_trail_replay`/`invalidation_replay`
  仅 `scripts/` 调用，**不在生产路径**。
- `app/utils/timezone.py:_et_trade_date` 的固定 -12h 只在 pytz/zoneinfo 不可用时回落
  （zoneinfo 可用 ⇒ 主路径正确），**不是当前活缺陷**。
- `startup_smoke_check.py` 的"启动段早于本次重启"过期判据是**设计**（须在重启后 120s 内跑），非缺陷。

## 5. 本文对既有落档的修正

### 5.1 口径订正（已改 handoff + DEB-20261103-...-review）
"11-02 进程 RSS 峰值 1.45GB"在冻结时刻**无 `/proc` 快照留存**，无法确证是 RSS 还是
VmPeak/VmSize；当前进程实测 VmSize 1.71GB / RSS 66–148MB 且 956MB 为 arena 虚拟预留 ⇒
该数字**极可能是虚拟地址空间**。已在两份落档中改为"口径待核"，并指向本文。

### 5.2 对 agent 结论的两处修正（以代码为准）
1. agent 称"error_alert 相对基线阈值未实现" ⇒ **不准确**：`scripts/error_alert.py:81`
   `MIN_THRESHOLD = 1  # ERROR/CRITICAL 是 P0, 1 条即触发`，median 算了不用是**有意**；
   真问题是**cron 注释宣称"阈值改相对基线 max(3, 3×中位数)"与代码不符**（误导 + 告警疲劳）。
2. agent 称"thread 未能退出 = 线程泄漏" ⇒ **不成立**：`scheduler_core.py:2044-2061` 是
   **关机路径**的 join 超时告警，进程随后退出，不构成运行期泄漏。

## 6. 建议处置顺序（**均未执行**，等用户裁决）

**P0-1 治触发器：cron 分钟错峰**（改 crontab，可回滚、无代码、不需重启）
把 `*/5`/`*/10`/`*/15`/`*/30` 改显式分钟列表、避开 `:00/:30`，并把 `event_stream_ingest`、
`orderflow_forward_capture`、`sector_pulse` 移出开盘 ±5 分钟窗 ⇒ 峰值并发 13 → 2–3，
瞬时内存需求 ~1.1GB → ~200MB。

**P0-2 治决策面：VIX 缓存**（两处：TTL 真按 60s/600s 失效 + VIXY/FRED 分离 key）

**P0-3 治执行面：入场确认阻塞**（`entry_confirm_max_wait_s` 30 → 异步或 5–10s）

**P1（排后但别忘）**：AFTER_HOURS 两处补 try/except → watchdog 盘中禁自愈重启 →
`MALLOC_ARENA_MAX=2` + per-call pool 改模块级 → `trade_records(futu_order_id)` 索引 + 出场子查询
一次性预计算 → Futu 双实例收敛 → TwelveData 限速器进程级化 → position_sync 事务外做网络调用。

## 7. 今晚（22:30 CST 开盘）现场复核项

| # | 复核项 | 判据 |
|---|---|---|
| 7.1 | cron 扎堆 | 22:30 分钟的 CRON 启动数（当前 13） |
| 7.2 | 宿主内存 | sysstat 22:30→22:50 的 buffcache/swap/%memused |
| 7.3 | 服务内存 | `[GC] 10min 汇总` 的 RSS 读数（当前 66–148MB） |
| 7.4 | 虚拟地址 | `VmPeak/VmSize`（当前 1.71GB）与 PROT_NONE 段数（当前 48 段/956MB） |
| 7.5 | 轮次间隔 | `auto_execute` 实际间隔（当前 3min09s） |
| 7.6 | VIX 新鲜度 | `[Finnhub] VIX (VIXY代理)` 日志的时间戳间隔（当前预期 ≤900s，实测偏长） |

## 8. 未验证项（明确声明）

- 11-02 冻结时刻的 `/proc/<pid>/status` 快照（决定 1.45GB 的口径）。
- `PRAGMA integrity_check` 单次耗时、PushHub 队列堆积规模、error_alert 实际推送频次、
  哨兵 py-spy dump 实际耗时。
- `get_kline` 实际 bar 数分布、新闻去重集日增量、web_dashboard 是否同进程。
- 任何"某步骤必然导致 X"的因果推断，除本文 H1（有 journal+sysstat+时序三重实证）外均标为推测。

## 9. 实施记录（2026-11-03，用户授权后执行）

> 用户裁：实施 **P0-1（cron 错峰）** 与 **P0-3（入场确认阻塞）**；**P0-2（VIX 缓存）本轮不做**
> （见 §9.4）。

### 9.1 P0-1 cron 分钟错峰（已装机，无需重启）

**改动**：17 行，**只改分钟字段，不改频率、不改小时窗、不改命令**。

| 任务 | 原 | 新 | 说明 |
|---|---|---|---|
| `extended_hours_entry_executor.py`（×2 行） | `*/5` | `2-59/5` | 余数类 2 |
| `event_stream_ingest.py` | `*/5` | `4-59/5` | 余数类 4（写 730MB 库） |
| `exit_intent_reaper.py` | `*/5` | `1-59/5` | 余数类 1 |
| `health_check.sh` | `*/10` | `7,17,27,37,47,57` | 全库 integrity_check 错开 |
| `deploy_freshness_check.py` | `*/10` | `9,19,29,39,49,59` | 与上错开 2min |
| `error_alert.py`（×4 行） | `*/15` | `4,19,34,49` | 避开 :00/:30 |
| `sector_pulse.py`（×2 行） | `*/30` | `13,43` | 避开 :00/:30 |
| `business_heartbeat.sh`（×2 行） | `*/30` | `7,37` | |
| `tripwire_checker.py` | `*/30` | `23,53` | |
| `orderflow_forward_capture.py`（×2 行） | `0,30` | `8,38` | 固定分钟族，原样正落在开盘分钟 |
| `freeze_sentinel.py` / `extended_hours_exit_cron_gate.sh` | `* * * * *` | **不动** | 前者是安全网、后者是出场链路 |
| 其余固定分钟行（如 `tripwire_checker 55 3,4`、`da_chain_health_report 5 6 * * 6`） | — | **不动** | 匹配时按"行尾注释剥离后的首个脚本"定位，避免误伤 |

**改动前踩坑（已修）**：首版按"命令串含脚本名"匹配，误伤了 `da_chain_health_report.py`
（其**行尾注释**里提到 `error_alert.py`）与 `tripwire_checker.py 55 3,4`（固定分钟族）
⇒ 改为「剥离行尾注释 + 取首个真实脚本 + 仅命中 `*/N` 族」双重守卫后才装机。

**效果（按小时窗展开模拟，22 时 = 美股开盘小时）**：
开盘分钟 `:30` 并发 **12 → 3**（只剩两个轻量每分钟族 + 一次 orderflow）；
小时内峰值 **12 → 5**（`:49`）。模型与 journal 实证吻合（改前 :30 = 12 个 = journal 的 13 条 CRON）。

**回滚**：`crontab /opt/ftseek/backups/crontab-<ts>.txt`（装机前已备份全量）。

### 9.2 P0-3 入场成交确认窗 30s → 10s（已上线，需重启，已重启）

- 改动：`config/unified_config.yaml` → `playbook.execution_parameters.entry_confirm_max_wait_s: 30 → 10`。
  **路径已验证**（该键在 `execution_parameters` 扁平层，`PlaybookConfig._load` 只拍平
  `risk_`/`execution_` 两节；实测 `get_playbook_config().get("entry_confirm_max_wait_s")` = **10** ✓）。
- 消费点唯一：`app/playbook/playbook_service.py:2012`（`_confirm_order_filled`，每 0.5s 轮询 broker）。
- **安全性论证（已核代码，超时 ≠ 放弃订单）**：① 提交后立即 `mark_execution_submitted` 持久化
  broker 订单号 ⇒ 崩溃/超时都不丢身份；② 超时后 `unlock_execution_if_zero_fill_terminal`
  **仅在 broker 报终态零成交时**解锁（可重试），否则 `mark_execution_unknown` +
  `_halt_unresolved_live_entry` 挂起待对账；③ 部分成交会 `cancel_order` 撤剩余。
  ⇒ 缩短窗口**不产生静默孤儿**，只是"等 30s"变成"等 10s 然后挂起对账"。
- **预注册判据（今晚开盘后核，任一触发即回滚 30 + `systemctl restart`）**：
  - R1 `auto_execute` 单轮最长 ≤ 30s
  - R2 轮次间隔 p50 ≤ 100s（改前实测 3min09s）
  - R3 UNCONFIRMED/挂起数 > 当日订单数 20% ⇒ 回滚
  - R4 入场成交率较 11 月均值下降 > 20% ⇒ 回滚

### 9.3 部署

- 重启前核①：未平仓 3 笔（US.MRK / US.LMT / US.ACN，非在途出场）；
  核② `git status --porcelain -- app/ scripts/` 为空（本批唯一上线改动是 config）。
- `systemctl restart ftseek-us`（13:02:45 起进程）→ `startup_smoke_check.py --wait 90`
  ⇒ **7/7 全过（exit=0）**（ConfigValidator / 漂移检测 / 热重载 / SSOT 审计 0 BLOCKING /
  双臂安全确认 / 退出快循环 / 零 Traceback-CRITICAL）。
- `scripts/config_validate.py` 的 2 项失败（343 未用键 / 135 缺失键）是**既有结构性问题**，
  与本改动无关（该键有代码引用，不影响两项计数）。

### 9.4 P0-2（VIX 缓存 TTL + key 分离）本轮未做 —— 登记为待办

用户裁"本轮不做"。风险仍在：VIXGuard（入场否决）与 regime 闸当前可读到**最长 900s 前的 VIX**，
且 VIXY 实时值与 FRED 的 T-1 值写同一 cache key（一次代理失败可把 T-1 值当真值缓存 900s）。
建议下一批处理（涉及交易与风控取值路径，须先写死判据 + 影子对照）。
