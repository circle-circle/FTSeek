# RFC-171 日K取数不撞配额 + 预筛轮 Finnhub 调用去重（TwelveData 回退与慢轮根治）

- 状态: 已落地（2026-10-27）
- 类型: L1 缺陷修复（数据获取路径；不改任何入场/出场判据、不改阈值）
- 触发人: 用户「请消除 twelvedata 回退和慢轮」
- 证据窗口: 2026-10-26 21:01:36–21:07:40 CST（= ET 09:01–09:07 playbook 生成轮）

## 1. 实测病灶（先量后修，防止按叙事下手）

| 观测 | 读数 | 出处 |
|---|---|---|
| Layer1 预筛耗时 | 21:02:57 → 21:06:36 = **219s**（69 只，`max_workers=5`） | `logs/ftseek_us.log-20261027` L697-978 |
| Finnhub 429 触发 | **62 次**（每次盲睡 5s） | 同上，窗口内 grep `429 限速` |
| SEC insider 调用 | 94 行（≈47 只 × 30d/40d 两查） | 同上 |
| 日K（Futu）失败 | **1 只**（US.ARM `ret=-1`），伴随配额等待超时 1 次 | L1016-1018 |
| TwelveData 回退 | **2 次调用**（ARM count=60 / count=30） | L1016/L1018 |
| 每标的 Finnhub 调用数 | **7**（profile2 / quote / company-news×2 / calendar-earnings / stock-earnings / VIX 分摊） | 活体计数探针（3 只样本，16→21 calls） |

结论（与上一轮口头归因相反，此处更正）：**"62 只批量取日K必然打穿配额"不成立** —— 日K只失败 1 只、回退 2 次；
**慢轮的主因是 Finnhub 免费档 60 calls/min × 每标 7 次调用**（69 只 ≈ 483 calls），439 次超额全部转化为
429 的 5s 盲等。日K配额虽小，但它是"零容错"的：进程重建后缓存必冷，62 连发必然把窗口打满，
末位 1-2 只永远回退 TwelveData（口径不同 + 更慢），这也是每次 09:0x 都会看到的那 2 行日志。

## 2. 改动

### A. 日K：从"必然回退"改为"结构上不回退"

1. **缓存落盘 + 相位感知新鲜度**（`app/data/futu_lv3_provider.py`）
   - 新增 `data/cache/daily_kline_v1.json`（原子写、30s 节流），进程启动首用时装载。
   - 新判据 `daily_cache_acceptable()` 取代"24h TTL + 10 天宽限"：
     - 盘前/盘中（ET < 16:15）：缓存末端 = 上一交易日即命中 → **盘前批次零历史K请求**；
     - 收盘后（ET ≥ 16:15）：要求"收盘后拉取且含当日 bar"——盘中拉到的当日 bar 是半截
       bar（close=盘中价），若直接复用会污染 IC/影子回填的当日收益，必须重拉；
     - 假日错位（末 bar 落后且 20h 内拉过）信任缓存，避免假日晨反复重拉；
     - 10 天硬底保留（反幻觉守卫不变）。
2. **配额等待 10s → 30s**（`_acquire_daily_kline_quota` 默认值，仅日K路径）。冷缓存日表现为排队而非回退。
3. **ret≠0 单次退避重试（2s）**：`ret=-1` 是 OpenD 账户级频控（分钟K/其它进程共享同一额度，
   应用侧 60/30s 只是近似），一次抖动不该把日K永久推到 TwelveData。

### B. 预筛轮：每标的 Finnhub 调用 7 → 5.3（去重，不改任何维度语义）

1. `get_sentiment_score`：原先再发一次 `company-news?days=2`，改为复用 `days=3` 载荷并按发布
   时刻本地截回 2 日窗（`datetime` 字段过滤）。语义等价，省 1 call/标的。
2. 新增 `get_earnings_calendar_index()`：市场级 `calendar/earnings`（1 call，6h 缓存）替代逐标的
   `calendar/earnings?symbol=`（69 calls）。取空/异常 → **回退逐标查询**（不静默丢维度）。
3. `FundamentalProvider` 缓存 TTL 1h → 12h（PE/市值/行业/ROE 季度级变化，盘前每标每天重拉是浪费）。

## 3. 验收（可复核）

- 单测：`tests/test_futu_dailyk_quota.py` 8 passed（新增"相位新鲜缓存零请求零配额"、
  "落盘→清内存→重装仍零请求"、假钟饱和门闸不误放行）。
- 关联回归：`test_playbook_data_collector.py`+`test_playbook_screener.py` 34 passed；
  `test_playbook_ic_tracker.py`+`test_shadow_tracker.py`+`test_ic_tracker_horizon.py` 57 passed。
- 活体：3 只样本 collect 调用数 21 → 16（7→5.33 /标的），耗时 22.3s → 6.7s；
  日K冷取 3 只 3 请求 → 模拟重启后 **0 请求 / 12ms**。
- 生产首考（下一交易日 09:0x 窗口）：①`grep -c "走 TwelveData fallback"` 该窗口应为 **0**；
  ②`K线配额窗口满` 应为 0；③Layer1 耗时对比 219s 基线。

## 4. 回滚

- 日K：删除本 RFC 对 `futu_lv3_provider.py` 的三处改动（落盘装载/相位判据/重试）即可回到
  "24h TTL + 10s 等待 + TwelveData 回退"；`data/cache/daily_kline_v1.json` 可直接删除，无副作用。
- Finnhub：`get_sentiment_score` 恢复 `days=2`；collector 恢复逐标 `calendar/earnings`；
  fundamental TTL 恢复 3600。
- 无配置项、无冻结参数、无门禁语义变更 ⇒ 回滚不影响任何成交集合。

## 5. 未覆盖 / 遗留

- **Finnhub 免费档 60 calls/min 是硬天花板**：去重后 69 只仍 ≈ 370 calls ≈ 6 分钟理论上限
  （实测常因跨轮缓存命中而更低）。要把它压到秒级必须动数据面结构（把"仅影响排序"的
  news/beat_rate 移出 09:00 热路径），或升级付费档，或把生成轮前移——三项均属交易行为/
  成本决策，未在本 RFC 内擅动。
- SEC insider 每标的 2 查（30d/40d）未动（不同窗口语义，需单独评估）。

## 6. Hotfix 2026-11-27：上一交易日跳假日（感恩节次日入场零成交根因）

- 状态: 已落地（2026-11-27）
- 类型: **L1 缺陷修复**（数据获取路径；不改任何入场/出场判据、不改阈值、不改配额/预算/缓存回写策略）——与本 RFC 主体同类。
- 触发人: 用户「请修复确保可以正常交易」（承接诊断「美股已开盘，请检查日志记录，为什么一直没有成交」）。
- 单变量: 仅 `daily_cache_acceptable()` 的「上一交易日」计算一处。

### 6.1 病灶（先量后修）

| 观测 | 读数 | 出处 |
|---|---|---|
| 今日新入场 | **0 笔**；8 条指令全 `triggered=0/executed=0`（RIVN 写入即 SKIP 除外） | 只读 DB `playbook_instructions` date=2026-11-27 |
| 09:45 ET 起 auto_execute | **9/9 轮**「0 成功, 0 跳过」，MSFT 恒 `trigger_context: timeout` + 其余 6 只 `busy` | `logs/ftseek_us.log` 22:45:49→23:10 |
| 日K配额等待 | 每轮 `D::US.MSFT 等待 30.0s 才拿到窗口槽位 (60次/30s)` | 同上 |
| 卡死栈 | `playbook-entry-data` 线程 3023 样本卡在 `_acquire_daily_kline_quota(:1700)←get_daily_kline←get_daily_bars(router:217)←get_full_market_context(router:501)` | py-spy record 309k 采样 /tmp/pyspy_kline_raw.txt |
| 缓存被拒 | MSFT/BABA/ORCL/BA/GS `fetched_at=11-26 05:30 CST`，age≈41.8h **> 20h 信任窗**；末端 bar=11-25 | `data/cache/daily_kline_v1.json` |

根因链：`daily_cache_acceptable` 算上一交易日 **只跳周末、不跳假日**（`while prev.weekday()>=5`）→ 感恩节次日 11-27 的 `prev=11-26`（休市）恒无 bar → 含 11-25 定稿 bar 的缓存被误判过期 → 每轮重拉日K撞满 60/30s 配额等满 30s → 超 `entry_data_fetch` 3s 取数预算 → MSFT `timeout`、单飞槽被占 → 其余 6 只 `busy` → 迟到结果在 `DATA_CACHE_PUBLICATION_ALLOWED=False` 下不回写缓存（`futu_lv3_provider.py:2056`）→ 下一轮重复 ⇒ **入场死循环**。

### 6.2 改动（单变量）

`app/data/futu_lv3_provider.py::daily_cache_acceptable`：上一交易日从「只跳周末」改为「跳周末+假日」，复用交易日历 SSOT 的**离线**判定 `get_holiday_calendar().is_us_holiday(prev)`（`_non_trading_day`：周末→只读热缓存→硬编码 `US_HOLIDAYS`→fail-open，**不发网络**，docstring 明确 `futu_lv3_provider` 为其热路径消费方）。`US_HOLIDAYS` 含 `2026-11-26`。修复后 `prev=11-25`，现有磁盘缓存末端 11-25≥prev → 立即命中，**无需刷数据、无需动配额/预算/回写**。10 天硬底、20h 信任窗、收盘后相位判据均不变。

### 6.3 验收（可复核）

- 契约：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./venv/bin/python -m pytest tests/test_futu_dailyk_quota.py -q` → **25 passed**（既有 19 + 新增 `TestDailyCacheHolidayAware` 6：感恩节次日命中/真陈旧仍拒/信任窗不变/周一跳周末不变/10天硬底不变/收盘后分支不变）。
- 离线功能验证（真实缓存）：修复后 MSFT/BABA/ORCL/SPY（age≈41.8h）`daily_cache_acceptable=True`；`is_us_holiday(2026-11-26)=True`、`prev_trading_day("US",11-27)=11-25`。
- 活体（重启后）：auto_execute 不再 `timeout/busy`，`triggered/executed` 推进（见 handoff 当日条）。

### 6.4 回滚

还原 `daily_cache_acceptable` 的 prev 循环为 `while prev.weekday()>=5`（删去 `is_us_holiday` 项与懒导入）即回到假日错位态；无配置项、无冻结参数、无门禁语义变更。

### 6.5 影子先行豁免说明

本批属 L1 数据面缺陷修复（与本 RFC 主体同类，不改入场/出场判据与阈值），由用户显式裁决「请修复确保可以正常交易」触发，且当下实盘入场被完全阻塞（0 成交死循环）。按 RFC-171 先例以契约测试 + 离线功能验证 + 重启冒烟 + 活体验证落地，不走多日影子 A/B。分钟K路径 `get_kline` 的同源假日缺陷（盘初 K_1M `ret=0` 空返 warning）非入场拦截主因，属独立变量，本批不动。

## §7 同源假日缺陷批（2026-11-27）：分钟K get_kline 上一交易日 + gate_liveness_probe 资金流门

> 承接 §6（用户裁「请修复确保可以正常交易」）后用户裁「请继续做根因和关联问题做修复」。
> 根因类＝「上一交易日/最近交易日只跳周末不跳假日」。本批修 2 处已实锤 L1 数据面站点，
> 其余同源疑似站点审计排队（§7.6），交易行为域一律不动、须另行裁决。

### 7.1 病灶（与 §6 同根因类）

1. **`app/data/futu_lv3_provider.py::get_kline` 日内路径**：内联 `Mon→-3 / Sun→-2 / 其余→-1`
   只跳周末 → 感恩节次日 11-27 的 `prev=11-26`（休市）恒无 bar → 日内 `start` 落在无 bar 日：
   盘前/盘初今日 bar 尚不存在 → K_1M 空返 `ret=0 msg=None` warning（今日 11 条：21:41 CST
   LLMStrategyLoop 7 只 CAT/ARM/MSFT/META/AMD/BABA/ORCL ＋ 22:30 FutuKlineRefresh/playbook-entry-data
   GS/CAT/ARM/MSFT）；且 2026-06-17 P0 修复本意的「前一交易日尾盘缓冲」在假日次日完全失效
   （技术指标需 ≥30 条连续 K 线，缓冲缺失 → quote_only 降级风险）。`_valid_prefixes` 过滤同源错位。
2. **`scripts/gate_liveness_probe.py::_most_recent_et_trading_day`**：`while d.weekday()>=5`
   只跳周末，原注释自认「节假日不识别, 假日次日会误报一天 stale」→ 工作日假日（感恩节 11-26 /
   圣诞 12-25）返回当日（无 bar）→ 资金流门把正常休市误报 stale/dead。

### 7.2 改动（单变量×2 站点，复用已验证 helper，零新日历实现）

- 新增模块级纯函数 `_kline_prev_trading_day(now_et)` ＝ `get_last_trade_date(now_et - timedelta(days=1))`
  （helper 语义＝≤给定日的最近交易日，传 today-1 即得**严格**上一交易日）；`get_kline` 日内
  `prev_trading_day` 三行内联替换为该函数，`_valid_prefixes` 随之自动校正。**K_DAY 路径
  （start=now-400d）不经此函数，零变化。**
- gate probe 改 `get_last_trade_date(now_et())`（语义＝≤今日的最近交易日，正是本函数本意）；
  删除随之不再使用的 `from datetime import timedelta`（grep 确认无其他使用点）。
- 复用 `app/utils/timezone.py::get_last_trade_date`（DEB-20260904 #135 已假日感知：离线
  `is_us_holiday`，周末→只读热缓存→硬编码 `US_HOLIDAYS`，10 次迭代防死循环，**不触网**）。
- 配额/预算/缓存回写策略/方向/阈值/仓位/live 权限全部未动；无新配置项、无冻结参数。

### 7.3 验收（可复核）

- 契约：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./venv/bin/python -m pytest tests/test_futu_dailyk_quota.py -q`
  → **35 passed**（既有 25 ＋ 新增 `TestMinuteKlineHolidayAware` 10：7 纯函数日期例〔感恩节次日
  11-27→11-25 / 正常周二 / 周一 / 周日 / 周六 / 总统日 02-17→02-13 / 独立日 observed 07-06→07-02〕
  ＋1 `get_kline` 接线 spy 例〔断言 hist_kwargs.start==helper 输出〕＋2 gate probe 例〔假日不误报
  stale / 交易日返回当日〕；autouse fixture 压 `_cached_trading_day=None` 保证离线确定性，同 §6）。
- 离线功能验证：`_kline_prev_trading_day(datetime(2026,11,27,8,41))=="2026-11-25"`；
  gate probe monkeypatch now_et=11-26 19:15 ET → `"2026-11-25"`。
- `py_compile` 两文件 exit0；`check_undefined_names.py` exit0。
- 重启 `systemctl restart ftseek-us` → PID **221732**（23:42:04 CST），启动冒烟 **7/7 exit0**；
  重启后日志段 0 Traceback/CRITICAL、0「K 线获取失败」；auto_execute 轮次正常完成（盘中量能门
  BLOCK＝半日市量稀的正常行情条件门）；出场侧 BA/GS/BABA 三仓 durable 续上（BABA 早期紧 SL /
  GS 死信号豁免规则正常评估）。
- **诚实边界**：活体验证时点已是盘中（10:42 ET），今日 bar 存在，`ret=0` 空返在盘中本就不复现
  ——盘前/盘初 warning 消失待下一盘前/假日次日自然观察；接线正确性由 spy 契约例钉死。

### 7.4 回滚

还原 `get_kline` 三行内联（Mon→-3/Sun→-2/else→-1）＋还原 gate probe `while weekday>=5` 循环
（补回 timedelta import）＋重启即回到旧态；gate probe 为 cron fresh-import，单独回滚无需重启。
无配置项、无冻结参数、无门禁语义变更。

### 7.5 影子先行豁免说明

同 §6.5 先例：L1 数据面缺陷修复（不改入场/出场判据与阈值），用户显式裁决「请继续做根因和
关联问题做修复」触发，以契约测试＋离线功能验证＋重启冒烟＋活体验证落地，不走多日影子 A/B。
§6.5 预留的「分钟K get_kline 同源缺陷独立变量另案」即本批，未与 §6 捆绑（§6 已先行提交）。

### 7.6 同源审计排队（本批发现、**未修**，须各自核口径/另行裁决）

| 站点 | 症状 | 域/处置 |
| --- | --- | --- |
| `scripts/daily_alert_summary.py:431 _prev_us_trade_date` | weekend-only 同类疑似；但带设计理由 docstring（「跳假日内权益不变」），且 snapshot_time tz 口径未核（capital_state_history 11-26 有样本、收盘边界后 15min 窗 0 样本 → 今日 prev_close=None，gap 11-26 22:53→11-27 10:08） | 报表域；先核口径再裁，不盲改设计注释 |
| `app/risk/position_exit_monitor.py:143 get_market_hours_us` | 持仓小时数只跳周末、固定 16:00 收盘（无半日市概念），喂 holding_hours 出场规则 | **交易行为域**：修复会改出场时点，须影子先行＋另行裁决，本批不动 |
| `app/utils/timezone.py:184 is_pre_market / :199 is_after_hours / :420 market_phase_now` | weekend-only 无假日判定；消费方 order_executor/order_tracker 交易邻近，上游可能自带守卫 | 逐消费方审计后另案 |
| `shadow_tracker` / `ue_quote_freshness_shadow` 硬编码 2026 假日表 | 2027+ 时间炸弹（已知独立问题） | 仅留痕，另案 |
| `shared_kernel` 会话标签 / `signal_pipeline` 扫描间隔 | weekend-only，良性方向 | 低优先留痕 |

已核**假日感知无缺陷**（不动）：trade_guard（get_trading_window_dates 用 is_trading_day）、
pdt_guard、position_exit_monitor.is_us_market_open、market_hours、timezone.is_market_open、
pilot_policy.previous_local_trading_day（纯本地日历）、shadow_tracker/ue_quote（自带 2026 表，
fail-closed，仅受上条时间炸弹约束）。

## §8 同源假日缺陷批续（2026-11-27）：daily_alert_summary 权益桥接前收跳假日 + 会话窗审计闭合

> 承接 §7 后用户裁「继续」，处理 §7.6 排队中的报表域站点（先核口径再裁）＋ timezone 会话窗
> 逐消费方审计（只读）。本批修 1 处报表域 L1 缺陷，闭合 1 项审计排队（无交易缺陷），新发现
> 1 项半日边界排队（不动）。交易行为域一律不动。

### 8.1 病灶（报表域，与 §6/§7 同根因类）

`scripts/daily_alert_summary.py:431 _prev_us_trade_date` `while d.weekday()>=5` 只跳周末 →
感恩节次日 11-27 的 prev=11-26（休市）收盘窗恒无样本（休市日采样停摆：实证 11-26 全天 726
样本末条 22:53 CST=09:53 ET；11-25 收盘印 CST 11-26 05:00:18=96992.388 存在，11-26 收盘窗
CST 11-27 05:00~05:15 **空**）→ `prev_close=None` → 权益桥接恒 `not_judgeable`（前收缺失）。
单消费方 :579 `prev_close = _equity_at(conn, _cst_close_boundary(_prev_us_trade_date(trade_date)), ...)`。

### 8.2 设计注释裁定（不盲改，先核口径）

旧 docstring「跳假日内权益不变, 取最近样本即正确前收」＋调用方注释「假日后的周一必然走到
这里…明示这是预期而非探针坏了」＝**当年无假日日历时的保守妥协，非安全要求**。核心安全纪律
在 `_equity_at`（只取收盘**后**首样本＝真实收盘印、窗内无样本即 None、**不兜底盘中样本**）
——本修复逐字保留。口径已核：`snapshot_time`/`last_update` = CST naive ISO（微秒），
`_cst_close_boundary` = 16:00 ET + EDT/EST offset（EST+13h）；`capital_state.trade_env=1`
单环境匹配 history。裁定：docstring 本意（权益休市日不变 → 真前收=上一**交易日**收盘印）
恰支持假日感知；修复是把查找边界从休市日（恒无样本）移到真上一交易日（有真实收盘印），
消除盲点而非推翻设计、不放宽采样窗。

### 8.3 改动（单变量，复用 §7 同款 helper）

`_prev_us_trade_date` 改 `get_last_trade_date(strptime(trade_date,"%Y-%m-%d") - timedelta(days=1))`
（离线 `is_us_holiday`，周末→热缓存→硬编码 US_HOLIDAYS，10 次防死循环，不触网；对 naive
输入直接当 ET naive、不做 CST→ET 转换 ⇒ 无跨日错位）。调用方注释更新（prev 已假日感知，
此分支现仅属真采样断流/重启）；**reason 标签「前收 (休市/采样停摆时属预期)」逐字保留**
（`test_missing_prev_close_sample_says_so` 断言含「休市」「预期」钉死）。`_equity_at` /
`_cst_close_boundary` / 采样窗 15min / 容差 $50 / 桥接恒等式 C≈G+U 全部未动。

### 8.4 验收（可复核）

- 契约 **31 passed**（24 既有 ＋ 7 新增 `TestPrevTradeDateHolidayAware`：感恩节次日 11-27→11-25 /
  感恩节当日 11-26→11-25 / 总统日 02-17→02-13 / 独立日 observed 07-06→07-02 / 正常周二 /
  周一跳周末 11-30→11-27 ＋ 端到端桥接闭合例〔真前收印落 11-25 边界、休市日无样本 → verdict=ok
  C=-500〕；autouse 压 `_cached_trading_day=None` 离线确定性同 §6/§7）
  `PYTHONPATH=. ./venv/bin/python -m pytest tests/test_daily_alert_equity_recon.py -q`
  → `/tmp/rfc171_s8_equity_contracts.log`。既有 `test_prev_trade_date_skips_weekend`
  （8-31→8-28、9-1→8-31）仍绿。
- 变异敏感性：周末-only 旧逻辑 → `not_judgeable`（红）/ 假日感知 → `ok` C=-500（绿）
  → `/tmp/rfc171_s8_mutation.log`（`MUTATION_RED_THEN_GREEN_OK`）。
- 真实数据（`mode=ro`+`query_only`）：`_prev_us_trade_date("2026-11-27")="2026-11-25"`，
  `_equity_at` 命中 `prev_close=96992.388`（修复前 None）。
- 相邻 daily_alert 簇（pnl/log_sources/hk_retired/log_prefix）**41 passed**
  → `/tmp/rfc171_s8_adj.log`。`py_compile` + `check_undefined_names.py` exit0。
- 调度＝**Hermes cron**（ET 17:00 → CST 次日 05:00）fresh-import；`app/` 无 import（仅
  `reconciler.py:559` 注释提及）→ **无需重启服务**；下一跑 CST 11-28 05:00 生效。

### 8.5 回滚

还原 `_prev_us_trade_date` 为 `while d.weekday()>=5` 循环（删懒导入）；Hermes cron
fresh-import 无需重启。无配置项、无冻结参数、无采样纪律变更。

### 8.6 影子先行豁免说明

同 §6.5/§7.5：L1 报表数据面缺陷修复（不改交易判据/阈值/采样纪律），用户裁「继续」承接
§7.6 报表域审计队列，以契约＋变异＋真实数据＋相邻回归落地，不走多日影子 A/B。

### 8.7 会话窗逐消费方审计闭合（§7.6 排队项，只读，**无交易缺陷，不修**）

复用既有 `scripts/deb20261009_session_caliber_probe.py`（含半日市 13:00 对拍）＋直接 grep 核验
（探针留痕 `/tmp/rfc171_s8_session_probe.log`）：

- `timezone.is_pre_market` / `is_after_hours`：**零消费方死代码**（探针普查 0 调用/0 文件；
  `order_executor.py:5030` 的 `is_after_hours` 是局部变量来自 `get_market_phase()`，
  `order_tracker.py:264` 是 bool 形参，均非 timezone 调用）。
- `timezone.market_phase_now`：消费方仅 `datetime_utils` re-export ＋ `web_dashboard/routes/trade.py:618`
  展示；其下游 `signal_expires_at` 亦零消费方。假日 weekday 误标 phase 仅影响展示/无人用的
  过期分钟，无交易消费。
- **真交易源 `scheduler_triggers.get_market_phase()`（59 调用/30 文件）已假日＋半日感知**：
  P0-D1 2026-05-14 :116 `is_us_holiday` 当 WEEKEND；R-8b 2026-08-23 :135 `get_us_close_minutes_et`
  动态收盘（`US_EARLY_CLOSE_DATES` 含 11-27 13:00）。order_executor 盘后路由经此，无假日/半日缺陷。
- `timezone.get_market_session`（2 消费方 `position_sync:304` / `order_tracker:82`）经
  DEB-20260903 #8 复用 `is_market_open` 动态收盘（§7 已核假日感知）。

结论：§7.6「timezone 会话窗」排队项**闭合＝无缺陷**；死代码清理（is_pre_market/is_after_hours）
属另案（删除须六面零引用审计，本批不动）。

### 8.8 新发现排队（本批不动，一次一变量）

- **半日市 `_cst_close_boundary` 固定 16:00 ET**：今日 11-27 半日 13:00 ET 收，今收边界仍
  CST 11-28 05:00（16:00 ET）；若采样随半日收盘停摆则今收窗空 → `today_close=None`（与假日
  prev 缺陷同族但属**半日变量**）。今日收盘（CST 11-28 02:00）后方可实证采样是否半日门控，
  须另核另裁，本批不动。
- §7.6 其余排队项不变：`exit_monitor.get_market_hours_us`（交易行为域须影子＋另裁）、
  shadow_tracker/ue_quote 2026 表（2027+ 时间炸弹）、shared_kernel/signal_pipeline 良性 weekend-only。

### 8.9 半日 `_cst_close_boundary` 排队项＝证伪闭合（代码层无缺陷，**不改 `_cst_close_boundary`**）

§8.8 曾把「半日市今收边界固定 16:00 ET → 若采样随半日收盘停摆则今收窗空 → `today_close=None`」
列为半日变量排队项。本批逐消费方取证后**证伪**：采样并不随半日收盘停摆，16:00 ET 边界窗必有样本。

- **相位真源已半日感知**：`scheduler_triggers.get_market_phase()`（R-8b 2026-08-23）半日
  13:00–20:00 ET 判 **AFTER_HOURS**（非 OVERNIGHT/WEEKEND）。
- **采样循环非交易时段仍跑**（`position_monitor.py:1760 monitor_loop`，本批逐行复核）：
  :1769 仅 `phase in (WEEKEND, OVERNIGHT)` 才 sleep+continue 跳过 → 半日盘后 AFTER_HOURS **不跳**；
  :1857 `_is_market_open()`（TRADING）每 300s 更新资金，**:1866 else 非交易时段仍每 600s
  `_update_capital_safety`**（P2 2026-04-29「盘前/盘后持仓仍存在」）。`MONITOR_INTERVAL_MINUTES=0.5`
  （30s loop）→ else 分支单独保证资金样本间距 ≤630s < 900s（15min 今收窗）。`position_sync`（30s）亦更新。
- **实证类比**（11-24 常规日盘后，CST 11-25 05:00–09:00）：72 样本间距 max=331s，无一 >15min；
  收盘窗 [05:00,05:15] 稳定 4 样本（首+14s）。半日盘后同走 else 分支 → 同结构保证。
- **共时性**：16:00 ET 边界与券商 U 同刻（报表 CST 05:00 = ET 16:00）读取共时 → 无系统残差；
  若改 13:00 反而破坏 `test_daily_alert_equity_recon.py:273` 断言 + 共时性。`trade_date` 选择走
  `get_last_trade_date`（半日安全，§8 已核）。

结论：半日今收窗空＝**伪命题**，`_cst_close_boundary` 固定 16:00 ET 正确，**不修**。
今日 11-27 半日收盘（CST 11-28 02:00）尚未到，活体终证待 CST 11-28 05:00 Hermes 日报自然跑观察
（仍列观察项，但代码层已证无缺陷，不再作为待修排队项）。

## 9. 同源假日缺陷批 §9：`get_market_hours_us` 假日/半日感知（holding_hours 报表正确性，L1）

- 状态: 已落地（2026-11-28）
- 类型: **L1 数据/报表正确性修复**（持仓交易小时数计算；不改任何 live 下单/平仓判据、阈值、仓位、配额）
- 触发人: 用户「请继续做根因和关联问题做修复」→「继续」链（承接 §6/§7/§8 同源缺陷批）
- 根因类: 「上一交易日/最近交易日/收盘时刻/持仓小时数 只跳周末或固定 16:00，不识别假日/半日」

### 9.1 病灶

`app/risk/position_exit_monitor.py:143 get_market_hours_us(start_dt, end_dt)` 原实现**只跳周末 +
固定 16:00 收盘**（`day_close = current.replace(hour=16, ...)`），与 §6/§7/§8 同源：
- 假日（如感恩节 11-26）被当 6.5h 交易日计入；
- 半日市（11-27 13:00 ET 收）13:00–16:00 被多计 3h。

`models.py:417 holding_hours` property 复用本函数（P0 2026-05-11 改墙钟→交易时段），故跨假日/半日
持仓的 `holding_hours` 被系统性多计。离线算（now ET 11-27 11:20）：BA SHORT（entry 11-23 09:54 ET）
修复后 **20.93h**（旧≈27.4h，多计 6.5h 感恩节）；GS SHORT 5.42h；BABA LONG 0.88h。

### 9.2 L1 重分类证据（推翻 §7.6 旧「交易行为域」分类，关键）

§7.6 曾把本函数标「交易行为域须影子＋另裁」。本批逐消费方取证后**重分类为 L1 报表域**——
`holding_hours` **不驱动任何 live 平仓订单**：

- **Playbook（唯一下单管线）出场引擎 `exit_engine.py` 零 `holding_hours`**：TIME_EXIT/MFE_STALL/
  DEAD_SIGNAL 用墙钟 `held_minutes`（`exit_monitor_flow.py:402 = (now-entry)/60`）或「今日开盘以来
  分钟」`minutes_from_open`（RFC-064/073）；MAX_HOLD 按交易日数。
- **legacy `_check_time_exit`**（position_exit_monitor:1884）用墙钟 `held_minutes` 形参（非 holding_hours），
  且 `exit_policy.yaml time_exit.enabled=false`（默认 False 兜底）。
- **唯一 holding_hours→CLOSE 消费方＝`MainPipelineExitPolicy` EVENT_HORIZON_48H**（exit_policies.py:98，
  `thesis=="event" and holding_hours>48`）——本批取证**当前惰性，三条独立理由**：
  ① DEM `scan_positions`（position_monitor:960 live）内 ExitDispatcher 用 `get_exit_dispatcher()`
  **单例从未注册任何 policy**（唯一 `.register` 在 scheduler_core 局部实例上，传 coordinator）→
  `policy_for(owner)` 恒返 None → CLOSE 分支死路；② coordinator `self.exit` 仅赋值从不 evaluate/dispatch，
  且 `observe_only=true`；③ 当前 3 仓全 `playbook_sim`，被 :994 owner 闸 + :1119 `_pb_check_em` 护栏
  双重跳过，DEM 主线退出根本不触及；主管线已冻结新仓（AGENT.md）。
- **其余消费方全为报表/记录/告警/分析**：trade_records/trade_outcomes/trade_attribution 记录、
  `observers.py:177`「止损参数过宽」告警（>5h）、`engine.py:315` time_stop **WARN Alert**（仅告警非订单）、
  `exit_monitor_flow.py:818 time_to_mfe_trading_min` record-only meta、`close_flow.py:579` 平仓后
  exit_shadow record-only、`position_exit_monitor:2192` 仅 `[ATR_EXIT]` 日志、review/analytics/dashboard/
  notifier 展示、`pre_entry_gate` LLM prompt 历史样本上下文（gate 默认 disabled）。

故本修复属 L1 数据/报表正确性（§6.5 先例），不改任何 live 下单/平仓行为。

### 9.3 改动（单变量，+31/-2）

`get_market_hours_us` 周末跳过后：
1. 加 `get_holiday_calendar().is_us_holiday(current.date())` 假日整休逐日跳过（与周末同处理保证收敛；
   只读缓存不触网，与 §6/§7/§8 同源 SSOT）；日历异常 fail-open 保留原行为（按工作日计，不阻断报表）。
2. `day_close` 改动态收盘：`_close_mins = get_us_close_minutes_et(current.date())`（半日返 780=13:00，
   常规 960=16:00），取值失败兜底 960；`day_close = current.replace(hour=0,...) + timedelta(minutes=_close_mins)`。
3. 懒导入 `holiday_calendar`（与 scheduler_triggers.get_market_phase R-8b 同源）。

正常日 6.5h、周末跳过、10 天硬底等其余路径逐字未动。配额/预算/缓存回写/方向/阈值/仓位/live 权限全未动。

### 9.4 验收

- 新契约 `tests/test_rfc171_s9_market_hours_holiday.py` **12 例**（autouse 离线夹具压 `_cached_trading_day=None`）：
  SSOT 锚点/正常日 6.5h 不变/周末跳/感恩节 0h/半日 3.5h/半日盘后 0h/跨假日 span 1.5h/跨半日+周末 span 2.5h/
  日历异常 fail-open 6.5h/收盘异常兜底 6.5h。
- 相邻回归（rfc078_batch_d_round_trips / playbook exit_recovery / pipeline_batch_e / playbook_pipeline /
  ledger_ts_scale_precision / review dead_review_patterns / fix_instruction_exit_writeback_gaps）
  合跑 **245 passed**（含本 12）。注：rfc078 holding_hours DST 走 `live_ledger.build_round_trips` 另一路径不受影响。
- **变异红绿**（假日跳过失效 `if False` + 固定 16:00）→ 5 条假日/半日契约红（5.5≠2.5 等）→ 还原（MUTATION=0，
  diff 仅 +31/-2）→ 复跑 12 绿。
- 重启冒烟 7/7（见 handoff 凭据）。

### 9.5 影子先行豁免说明

同 §6.5/§7.5 先例：L1 数据/报表面缺陷修复（不改入场/出场判据与阈值），用户显式裁决「请继续做根因和
关联问题做修复」→「继续」链触发。以契约测试 + 离线功能验证 + 变异红绿 + 相邻回归 + 重启冒烟落地，
不走多日影子 A/B。**收益增量未证（正确性≠盈利）**。

### 9.6 回滚

还原 `get_market_hours_us` 为「只跳周末 + 固定 `day_close=current.replace(hour=16,...)`」（删假日跳过块、
动态收盘块与懒导入）＋重启。无配置项、无冻结参数。

### 9.7 剩余排队（本批不动，一次一变量）

- shadow_tracker/ue_quote 硬编码 2026 假日表＝2027+ 时间炸弹（shadow infra 另案）。
- 死代码 `timezone.is_pre_market/is_after_hours` 清理＝须 six-surface-zero-ref skill（六面零引用审计）。
- shared_kernel/signal_pipeline weekend-only＝良性低优先。
- §8.9 半日边界已证伪闭合（无缺陷不修）；`_cst_close_boundary` 不改。
