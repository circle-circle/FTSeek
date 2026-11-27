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
