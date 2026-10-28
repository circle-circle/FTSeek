# RFC-173 盘前取数宽度收敛 + Finnhub 进程级共享配额（96 次 429 根治）

- 状态: 已落地（2026-10-28）
- 类型: L1 缺陷修复（**只动 enrichment 取数层**；不改任何入场/出场判据、不改阈值、不改评分权重）
- 触发人: 用户「回归触发源，要确认为什么会批量取数，是否有必要」
- 证据窗口: 2026-10-28 21:01:36–21:05:55 CST（= ET 09:01–09:05 playbook 盘前生成轮）
- 前序: RFC-171（每标的 Finnhub 调用 7→5.3、日K落盘、财报日历市场级）—— 本轮是它的续篇：
  **每标的成本降了，但"宽度"和"跨源放大"没治，所以仍然打爆。**

## 1. 实测病灶（先量后修）

| 观测 | 读数 | 出处 |
|---|---|---|
| 批次入口 | `SCREENER] Screening 76 symbols` → `collect_batch(76, max_workers=5)` | `logs/ftseek_us.log` L2309 |
| 池子 | static 44 + dynamic 62（重叠 30）= **76** | L2307 |
| 服务重启 | 19:23:32 CST（**进程内缓存全清** ⇒ RFC-171 的 12h 基本面缓存当日必然冷启动） | `ps -o lstart=` |
| 429 总数 | **96 次**（finnhub_provider 57 + fundamental_provider 39），21:02 **53** / 21:03 26 / 21:04 17 | 窗口内 grep |
| 429 按端点 | `stock/profile2` **28**、`stock/metric` 5、`calendar/earnings` 3、`company-news` 2、`recommendation` 1 | 同上 |
| Futu 侧同族 | `获取市场快照频率太高，每30秒最多60次` **9 次** | L2411-2429 |
| 过滤率 | 76 进 → `Layer1 筛选出 76 只候选` → **门槛一道没拦** | L2309 / L2688 |
| 批次耗时 | 21:02:17 → 21:05:55 = **4 分 19 秒**（其中 57 次 `sleep(5)` 在拖时间） | L2309 / L2844 |
| 监控可信度 | `record_finnhub_call()` 只在 `get_quote()` **成功后**记账 ⇒ 新闻/指标/429 全部漏记；全天只报 197 次 | `finnhub_provider.py` 原 L246 |

## 2. 根因（四条，缺一不可）

1. **宽度**：一天一次的批量筛选本身必要，但 `collect_batch` 对 76 只**逐只**取 6 维，没有一个走批量接口 ——
   而同一个仓里 `premarket_enrichment._fetch_mcap_map()` 早已在用 `get_market_snapshot(200 只/批)`。
2. **静态字段每日重取**：`stock/profile2` 只为拿 `finnhubIndustry`（行业分类永不 daily 变化），
   却在 Futu happy path 上逐只拉一次 ⇒ 76 次/天，本轮独占 28 次 429。
3. **跨源放大**：Futu 快照被 5 并发打限流 → 该只**立刻**向 Finnhub 连发 5 连击
   （metric / recommendation / profile2 / company-news / calendar-earnings）⇒ Futu 的限流变成 Finnhub 风暴。
4. **配额视图分裂**：配额按 **API key** 算，进程里有 3 套互不知情的额度视图
   （`finnhub_provider` 每实例桶 / `fundamental_provider` 裸 requests 无限速 / `premarket_enrichment` SDK 无限速），
   且撞 429 后只有撞墙线程 `sleep(5)`，其余线程继续轰。

## 3. 改动

| 项 | 文件 | 内容 | 语义影响 |
|---|---|---|---|
| T1 静态字段落盘缓存 | `app/data/providers/fundamental_provider.py` | 新增 `_get_profile()` + `data/finnhub_profile_cache.json`（原子写，TTL **30 天**，跨重启）。industry 命中缓存后**零请求** | 无（行业值不变） |
| T2 批量快照 | 同上 + `app/playbook/data_collector.py` | 新增 `prefetch_snapshots(symbols)`：一次 `get_market_snapshot(list)`（200/批、整批失败二分隔离坏票、类股转点号）；`collect_batch` 在起线程池**之前**预取，`_fetch_futu` 优先吃预取行 | 无（同一份 snapshot 字段） |
| T4 放大链切断 | 同上 | ① 快照撞限流 → 单只延迟 1.2s 重试一次，仍失败才降级；② `_fetch_finnhub` 先取 `stock/metric`，**失败即早退**（原来还会继续打 4 个端点） | 无（失败路径少发请求） |
| T5 共享配额桶 | 新增 `app/data/finnhub_limiter.py`；改 `finnhub_provider` / `premarket_enrichment` | 进程级单令牌桶（54/min ≈ 免费档 60 留 10% 余量），三个客户端全部 acquire 同一份额度；撞 429 → `on_429()` 清空令牌 + **全局**冻结 8s；`_get` 逐请求记账（含 429）；删除 `finnhub_provider._RateLimiter`（每实例桶） | 无（只改排队，不改结果） |

### 未做（有理由）

- **news 维度不砍**：`news_positive` base 1 + `weight_adjustments` −1 = **净 0 分**，但作为条件标签写进
  `conditions_json`（近 8 天 493 条指令里出现 52 次）供 feedback/IC 归因，砍掉会少一类归因标签。
  「改市场级 1 次 + `related` 映射」经**活体探针实测不可行**：`GET /news?category=general` 返回的 100 条里
  `related` 全为空串（字段确实叫 `related`；仓内 `get_market_news()` 读的是不存在的 `relatedSymbols`，
  该映射一直是空的）。故保留逐只 `company-news`，只让它排队（76 次 → 约 85s，可接受）。
  附带发现：`get_market_news()` 的 `relatedSymbols` 是错键，留作后续项，本轮不动（无消费方依赖它）。
- **`collect_batch` 并发未改**（5→2 会拉长批次）：T1/T2 落地后压力已降，先观察一轮再决定。

## 4. 验收（Gate 5）

1. **单元/契约**：`tests/test_finnhub_quota.py` 8 项 —— 桶排队、超时不放行、429 全局冻结、429 记账、
   profile 缓存跨实例、批量快照 1 批替代 N 次、预取失败回退逐只、metric 失败早退。
2. **等基反证**：把测试与 `finnhub_limiter.py` 拷到 `HEAD` worktree 跑 → **5 failed / 3 passed**
   （3 passed 是纯桶自身的单测），证明 T1/T2/T4/T5 的用例钉的是"改动前的错误行为"。
3. **活体探针**（真实 Futu + Finnhub，只读）：
   `profile2 首次 0.27s → 二次 0.000s（缓存命中）`；`批量快照预取 4/4 只 (4 只逐只 → 1 批)`；
   `_fetch_futu(AAPL) status=ok pe=39.629 industry=Technology`（吃预取行，未再发单只请求）；
   `company-news 131 条`，共享桶 `granted=2, 429=0`。
4. **回归面**：provider / collector / screener / premarket / 类股符号 / 财报日历 / 扩展时段 共 **165 passed**。
5. **生产验收（待次日 09:0x ET 盘前轮）**：三读数同时下降才算治本 ——
   窗口内 Finnhub 请求数、429 数、批次耗时。判据：`grep -c "429" logs` 窗口内 = 0；
   `批量快照预取 N/N 只 (N 只逐只调用 → 1 批)` 出现；`Layer1 筛选出 76 只候选` 数量不变。

## 5. 回滚

单 commit 回滚即可（`git revert`）：新增文件 `app/data/finnhub_limiter.py`、
`data/finnhub_profile_cache.json`（可留，重跑会重建）；其余为同文件内的旁路改动，无 schema/配置变更。