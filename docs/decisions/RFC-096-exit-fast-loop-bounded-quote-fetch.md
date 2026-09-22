# RFC-096: 退出快循环取价上界化 (复发停摆根因) + 退订聚合告警降噪

> **状态**: 已实施 (sim, 2026-09-22 盘中)
> **日期**: 2026-09-22
> **作者**: Hermes Agent
> **审批人**: 项目所有者 ("按建议做最优处理" = 全权委托先做 ①②)

---

## 摘要

退出快循环 (`playbook_exit_loop`, 现役 interval=10s, 是 SL/TP 的**唯一执行者**) 的取价
路径直调 `data_router.get_batch_quotes` → 批量 miss 后再直调 `data_router.get_quote`,
两层都**没有时间上界**。Futu/OpenD 连接半死 (TCP 在、push 停) 时取价调用在 SDK 内阻塞,
整轮退出监控停摆 — **全史 6 次, 最长 587s**。

本次改动: ① 取价一律走有界线程 (`_pb_exit_bounded_fetch`) + 最近成功价兜底
(`_pb_exit_price_cache_*`), 使快循环心跳恒 ≤ interval; ② 停机退订的聚合行区分
"良性拒退 / 真失败", 消除对 ERROR 计数的污染。

---

## 问题陈述 (取证)

### 1. 退出快循环停摆 = 复发缺陷 (不是首次)

`grep "playbook_exit_loop 已" logs/ftseek_us.log{,-*.gz}` 全史:

| 时刻 (CST) | ET | 停摆 | 同窗证据 |
|---|---|---|---|
| 08-28 21:33:36 | 08-28 09:33 | **204s** | `last_push_age=175.1s`; 同窗 `monitor_loop` 178s 同停 |
| 08-29 03:58:00 | 08-28 15:58 | 175s | — |
| 09-01 22:01:27 | 09-01 10:01 | **587s** | `last_push_age=586.9s`; `[Watchdog.heal] restart` |
| 09-02 04:03:07 | 09-01 16:03 | 194s | — |
| 09-10 21:32:28 | 09-10 09:32 | 146s | 停摆前 09:30:45 刚 `连接恢复健康 | 本次中断 17.4s` |
| 09-22 21:32:54 | 09-22 09:32 | 121s | OpenD 瞬断 (SDK `init connect fail`), 断 23.0s |

**3/6 落在开盘后 1–2 分钟**; 8/28 那次与 `monitor_loop` 同时停摆 ⇒ 阻塞点在**共享数据层**
(Futu 连接/SDK 内部), 不在退出监控自身逻辑。

### 2. 停摆与"连接半死"同窗 (关键)

- 09-01: `last_push_age=586.9s` — 587s 无任何 push, 而 `connected=True` (假活)。
  退出监控停摆时长 (587s) 与无 push 时长 (586.9s) **逐秒吻合** ⇒ 取价调用阻塞在
  半死连接上, 直到连接被判定不健康并重连才返回。
- 08-28: `last_push_age=175.1s` vs 停摆 204s，同族。
- 复核: 9/1 09:50–10:01 ET 期间**没有任何** `锁定/取价成功` 类日志; 停摆结束点
  (= 重连点) 之后立刻恢复。**无法归因到具体哪个子调用** (SDK 无栈取证), 但这正是
  调用方加上界即可覆盖的情形 — 不必知道是哪一层阻塞。

### 3. 勘误: 本会话早前的两条"首次"结论是错的

| 早前结论 | 实测 |
|---|---|
| "`Abnormal event timeout/init connect fail` 仅 2026-09-22 一次" | 轮转日志另有 **09-02 / 09-03 / 09-10** 三次 (grep `logs/ftseek_us.log-*.gz`) |
| "`playbook_exit_loop` 无心跳全历史仅此一次" | **6 次** (上表) |

错因: 只 grep 了当前未轮转的 `logs/ftseek_us.log`。**纪律**: 频次类结论必须
`zgrep` 覆盖 `logs/ftseek_us.log-*.gz`。

### 4. 退订聚合行污染 ERROR 计数 (良性噪声)

09-22 09:38 停机: `退订部分失败: 成功=2, 失败=2` (ERROR) — 逐条 WARNING 实为 SDK
客户端侧拒退, **不占 OpenD 槽位**:
- `US.META的OrderBook订阅时间过短，至少需要订阅1分钟`
- `未订阅过US.NOK的Ticker，无法取消订阅`
而 QUOTE/K_1M 均退订成功。

---

## 提议方案 (已实施)

### 改动 1: 退出链取价上界化 (`app/playbook/pipeline.py`)

```python
_pb_exit_bounded_fetch(label, fn, timeout_s) -> (value, timed_out, error)
    # 有界线程执行取价; 超时即放弃本轮 (线程自行结束, 不再等待)
    # 单飞: 同名 label 上一轮仍在途 → 本轮直接返回, 不重复发起 (防阻塞线程堆积)
    # 上一轮超时线程已结束 → 先收编其结果入兜底缓存 (不白丢一次成功取价)
_pb_exit_price_cache_update(quotes) / _pb_exit_price_cache_view(symbols)
    # 最近成功价兜底, 上限 _PB_EXIT_PRICE_CACHE_MAX_AGE=120s (超龄按无价 = 旧行为)
```

调用点:
- 批量: `_pb_exit_bounded_fetch("batch", ...get_batch_quotes..., 6.0s)`
- 逐仓回落: `_pb_exit_bounded_fetch("single:<sym>", ...get_quote..., 2.0s)`
- 两处都并入/回落到兜底缓存; 兜底价最旧 >30s 时 WARNING 一次 (降级可见)
- 退出遥测 `quote_src` 新增 `cached_fallback` 取值 (降级轮次可审计)

**语义边界 (为什么兜底价不制造假触发)**: 兜底价是**真实观测价** (≤120s 前), 只在
"该价已跌破 SL/TP 线"时才触发 — 即"迟到"而非"捏造"。若无价可用则维持旧行为
(本轮跳过 + `R2 连续取价失败` 告警通路)。

### 改动 2: 退订聚合告警降噪 (`app/data/futu_lv3_provider.py`)

聚合行按失败**原因**分流: 命中 `未订阅过` / `订阅时间过短` (SDK 客户端侧拒退, 无槽位泄漏)
→ WARNING; 其余 (超时/OpenD 侧错误/未知文案) → 保持 ERROR。行内加
`其中良性拒退=N, 真失败=M` 计数, 未知文案按"真失败"保守处理。

### 影响范围

- 服务焦点: F1 (交易执行完整性) — 退出保护可用性
- 改动指标: `playbook_exit_loop` 心跳间隔 (p99 ≤ interval)、停摆告警次数、停机 ERROR 计数
- 预期变化: 停摆类告警 → 0 (连接故障期改为"用兜底价 + 降级 WARNING"); 停机 ERROR 计数 -1

---

## 风险评估

### 最坏情况

1. 兜底价用于 SL 判定 → 在价格快速反向时以**稍旧价格**触发退出 (本质 = 止损延迟, 方向正确)。
2. 有界线程被放弃后仍在后台运行 → 连接恢复后自行结束 (daemon); 单飞保证最多 1 个在途。
3. `_batch_quotes` 语义从"本轮新鲜"变为"新鲜 ∪ 未超龄兜底" → 组合级兜底
   (`_portfolio_backstop_evaluate`) 也是检测+告警 (不平仓), 且越线判据是
   `max(绝对下限, Σ逐仓SL距离×1.5)`, 结构上不会被 120s 内的旧价误触发。

### 缓解措施

- 兜底价硬上限 120s + 降级 WARNING + `quote_src=cached_fallback` 三处可见
- 回滚 = 恢复两处直调 (见下)

### 残留上界 (诚实声明)

- **首轮** (故障刚开始): 6s (批量) + 2s × 仓数 (逐仓回落) ⇒ 16 仓满配最长 ≈ 38s。
- **后续轮**: 单飞使同名取价在途时**直接返回**, 故每轮只耗 ~0s —— 停摆不会逐轮累积。
- 两者均 ≪ watchdog 120s 阈值 ⇒ 心跳恒活; 代价是取价延迟 (保护"迟到"而非消失)。
- 仍未加界: 退出链 broker 调用 (`dual.close_position`/`resume_close`) — 见下表 #3。

---

## 验证计划

```bash
# 1. 单测 (新增 tests/playbook/test_exit_price_bounded_fetch.py, 9 例)
venv/bin/python -m pytest tests/playbook/test_exit_price_bounded_fetch.py -q
#    锁: 超时不阻塞 / 异常不静默 / 单飞不重复发起 / 超时线程结果被收编 /
#        兜底价年龄界 / 无价不入缓存 / 调用点不得退回无上界直调 (源码契约)

# 2. 等基对比 (裸 worktree = 改动前 HEAD)
git worktree add --detach /tmp/wt_pb HEAD
#    同命令两侧跑 tests/playbook/{test_exit_recovery,test_portfolio_backstop,test_sl_fallback_gap_attribution}.py
#    期望: 新增失败 = 0 (基线 40 failed 为既有环境性失败)

# 3. 实弹: 重启后观察快循环节奏与心跳
grep "PlaybookExitFastLoop" logs/ftseek_us.log | tail -5      # 间隔 ~10s
grep "playbook_exit_loop 已" logs/ftseek_us.log               # 期望 0
```

---

## 回滚计划

```bash
# 改动 1: 把调用点还原为直调 (删 _pb_exit_* 块与两处 bounded 调用)
#   批量: _bq = data_router.get_batch_quotes([r["symbol"] for r in rows])
#   逐仓: quote = data_router.get_quote(symbol)
# 改动 2: 聚合行恢复无条件 logger.error
# 两者均为单文件纯代码改动 → git revert <commit> && systemctl restart ftseek-us
```

---

## 未采纳 / 待裁 (本 RFC 明确不做)

| # | 项 | 为什么不在这里做 |
|---|---|---|
| 1 | 盘前轮次瘦身 (把宏观日历/富途通道影子移出盘前步) | **证据不支持**: 9/22 轮次只跑 154s, 且入场本就有"开盘 5min 内不执行"门 → 首笔 auto_execute 09:36:57, **未耽误任何入场**。真问题(若有)是长步静默段, 属可观测性而非行为缺陷。需要时另开 RFC 并先补步骤级耗时遥测, 不凭推测动调度。 |
| 2 | watchdog 对"已知重连窗口"免误报 | 有界化后停摆已不该发生; 免误报会削弱唯一的"退出链停摆"告警。**保持告警真阳性**。 |
| 3 | 退出链 broker 调用 (`dual.close_position` / `resume_close`) 加界 | 加界 = 放弃线程内可能已提交的平仓单 → 双平/账实不符风险 > 收益。本次仅覆盖**取价**。 |
| 4 | Futu 半死连接检测延迟 (9/1 阈值 30s 却 587s 才发现) | 说明健康守护线程本身也被阻塞在共享数据层; 需单独设计独立活性探针, 另行提案。 |
| 5 | **盘中运行 OpenD 重查询脚本 (治理项)** | 取证发现 09-22 停摆窗口 (21:30–21:36 CST) 内, 一个旁路 ad-hoc Python 进程 (PID 2429324, **非主程序 PID 2392135**; 该进程 21:07:12–21:36:18 单连接跑了 3,006 次 `sync_query_processor`, 6 分钟窗口内 725 条 SDK 事件 ≈ 2 query/s) 正与主程序抢同一 OpenD 实例的历史 K 线配额; 另有 `backtest_overnight_carry.py` 同日在跑 (4h, 自带 0.6s 频控, 影响小)。**归因限度**: 无 OpenD 侧配额日志可证因果, 只能证"同窗在线"。属**会话/调度纪律**问题, 建议入 skill: 盘中禁止跑 OpenD/K线重查询脚本, 排到盘后窗口 (16:00 ET 后或 04:00 前)。 |
