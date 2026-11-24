# 议题：开盘零成交 —— 日 K 冷取数与单flight starvation 修复

## 状态

- [x] 进行中（修复与启动冒烟完成；自然验证受实时 NO_TRADE 阻塞，待可交易窗口）
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

## 1. 现象与边界

2026-11-24 美东 09:45→10:07，18 条 DA 指令没有新开成交。除 LLY 因已有多仓被 `SKIPPED_EXISTING_POSITION` 外，其余指令一直保持 `triggered=0`、`execution_state=PENDING`，没有 reprice、entry-confirm 或 broker order。

本议题只修数据可用性与候选间故障隔离，不通过放宽价格、方向、风险预算或入场门来制造成交。DA 文本与 reprice/持夜语义另见 `DEB-20261124-da-multiday-direction-and-instruction-audit.md`，本批不处理。

## 2. 根因证据

每轮日志均重复同一形态：

```text
US.BABA 入场取数暂不可用 (下轮复评): trigger_context: timeout
US.GOOG/US.TSLA/.../US.CRWD 入场取数暂不可用 (下轮复评): trigger_context: busy
自动执行完成: 0 成功, 0 跳过
```

- `entry_data_fetch.py` 的单 flight 契约是刻意设计：超时返回给调用方，SDK 工作线程保留至完成；在途时其他查询得到 `busy`，迟到结果与缓存发布均被丢弃。既有测试明确禁止简单地另起 worker 或接纳迟到结果。
- BABA 每轮都在约 27–30 秒后继续打 `D::US.BABA 等待约30s才拿到窗口槽位`，说明前一个超时调用尚未结束；下一轮又从最高 confidence 的 BABA 开始，形成重复 starvation。
- Futu 日 K 配额等待上限是 30s，而 `DEFAULT_ENTRY_DATA_TIMEOUT_S=3s`。但简单把超时改为 35s 会让 18 个候选在单 flight 上长时间串行，且 entry 查询按契约不发布缓存，不能作为合理总修复。
- 本机 `data/cache/daily_kline_v1.json` 已有 BABA/GOOG/TSLA/COIN/AMD/MSFT 等标的的 **2026-11-23 完整日 bar**。BABA `fetched_at=2026-11-23T21:30:41Z`，属于收盘后定稿。
- 运行服务于 2026-11-22 21:35 启动；`_daily_disk_load()` 原来只在每个 provider 首次调用时读盘一次。盘后任务后来写入的新鲜快照对长寿进程不可见，于是进程放着本地定稿数据不用，反复等 OpenD 配额。

结论：直接故障是“单标的冷日 K 取数超时 + 单 flight 阻塞全部候选”，可持续根因是“磁盘日 K 缓存只启动时加载一次”。

## 3. 最小修复

仅修改 `app/data/futu_lv3_provider.py` 的磁盘 hydration 及对应回归：

1. `_daily_disk_load()` 按文件 mtime 每 30s 节流探测，不再进程生命周期只读一次；
2. 逐 symbol 比较 `fetched_at`，只接纳比内存更新的完整日 bar；较旧文件即使 mtime 更新也不能回写；
3. 保留 `_session_final_bar` 守卫，半截当日 bar 不装载；
4. 文件不存在/半截读取失败时下窗重试，不永久禁载；
5. 磁盘 hydration 继续复用既有本地快照，不属于接纳 SDK 迟到结果，不改 `DATA_CACHE_PUBLICATION_ALLOWED=False` 的 entry 安全契约。

刻意不改：候选排序、3s 默认超时、单 flight、Futu 60次/30s 配额、方向/入场/确认/reprice/预算/退出规则、prompt、DB 指令与订单。

## 4. 回归与真实缓存验证

实际运行：

```bash
env -C /tmp PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/ftseek/FTSeek-US \
  /opt/ftseek/FTSeek-US/venv/bin/python -m pytest --noconftest -p no:cacheprovider -q \
  /opt/ftseek/FTSeek-US/tests/test_futu_dailyk_quota.py
# 13 passed

env -C /tmp PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/ftseek/FTSeek-US \
  /opt/ftseek/FTSeek-US/venv/bin/python -m pytest --noconftest -p no:cacheprovider -q \
  /opt/ftseek/FTSeek-US/tests/playbook/test_entry_context_cache_boundary.py
# 57 passed
```

新增回归覆盖：外部进程推进文件 mtime/fetched_at 后，不重启即可读到新值；随后较旧 fetched_at 快照不得覆盖新值；OpenD 请求数为 0。

另以真实 `data/cache/daily_kline_v1.json` 调用补丁后的 `get_daily_kline('US.BABA')`，在配额路径被强制设为异常的情况下仍返回 60 根、末端 `2026-11-23`，输出 `HOT_DISK_READ_OK`，证明无需 SDK/配额。

扩展组合回归首轮为 **14 failed / 235 passed**。失败集中在既有 `test_entry_data_bounded_fetch.py` 的 40ms 时序夹具；`test_query_exception_is_typed_and_slot_released` 在全套中由预期 `query_failed` 变成 `timeout`，单例复跑通过；另四项 preflight/broker 失败单例复跑 4/4 通过。没有改这些测试或宣称全量绿；它们与本批修改文件无直接调用差异，但完整套件仍有负载下时序不稳定，需另账治理。

## 5. 部署前检查与启动冒烟

- 进程启动后源码 mtime 审查：只有本批 `futu_lv3_provider.py` 发生变化；其他 app WIP 不会因本次重启新增上线。
- 未平仓：SIM LLY LONG、BA SHORT；LIVE 无仓。
- 入场 `SUBMITTED/FILLED_UNBOOKED/UNKNOWN`：0。
- `exit_ledger.db` 两表 0 行；`playbook_exits.db` intents 全 BOOKED、legs 全 TERMINAL，无未决出场。

实际执行：

```bash
systemctl restart ftseek-us
PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90
```

新 PID `4060355`，ActiveEnter `2026-11-24 23:23:23 CST`；启动冒烟 **7/7、exit 0**：ConfigValidator、漂移、热载、SSOT 0 BLOCKING、双臂、退出快循环、零 Traceback/CRITICAL。重启后 LLY/BA 两仓原样保留。

## 6. 自然轮次验证（受实时 NO_TRADE 阻塞，待后续窗口）

重启后的自然日志显示，10:23 与 10:27 ET 两轮 preflight 均为：

```text
[Regime] Assessment: no_trade (score=-7) | SPY≈716 | VIX=20.17 | top_n=0 multiplier=0.0
[playbook] 当前 regime=NO_TRADE (VIX=20.17), 跳过 auto_execute
```

因此重启后尚未再次进入 BABA trigger_context，不能宣称自然成交链已恢复，更不能以新开仓证明修复。**当前仍不成交的直接原因是实时 NO_TRADE 风控硬门，属于正常行为；不得为验证而放宽 VIX/regime。**

已验证到代码与真实缓存层的修复有效性（§4），以及启动健康和持仓保持（§5）。待后续 regime 回到可交易档后，再观察 BABA 是否不再每轮冷等约30s、其他候选能否进入各自价格/风控判定；最终是否成交仍由既有闸门决定。
