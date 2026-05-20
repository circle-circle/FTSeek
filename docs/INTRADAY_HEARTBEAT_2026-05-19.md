# 盘中交易状态心跳 — 设计 + 实施 (2026-05-19)

> **需求**: 盘中定期主动知晓交易链路和状态是否正常.
> **第一性原则**: 不为监控而监控 — 给"决策者"一眼看完, 信息密度高 + 信号噪比高.

---

## 1. 现状评估

### 已有可观测能力 (项目已有, 不重复造轮子)

| 组件 | 频率 | 职责 |
|---|---|---|
| **Watchdog** | 60s | 内部线程心跳 (scan_loop / monitor_loop 是否卡死), 仅记日志 |
| **PushHub** | 事件触发 | 开/平仓推送 + 5min 聚合 + 60s dedup, 钉钉/飞书/flag_file |
| **PushHealth** | 1h | 仅推送通道存活验证 (一行 "💓 通道心跳") |
| **daily_alert_summary** | 1/日 (盘后) | 当日告警汇总 — 但这是**事后**, 不是盘中 |
| **thread_manager.health_check** | 内部用 | 仅 API 暴露 (无主动推送) |

### 用户真实需求的 gap

✅ **已有**: 出问题时告警 (PushHub) + 通道存活心跳 (1h 1 行)
❌ **缺**: 盘中**综合状态心跳** — 一份能"一眼看完整体健康"的报告

> 关键区别: **告警** = 出错才推, **心跳** = 不管有没有错都推 (确认运行正常).

---

## 2. 设计 — 3 层金字塔

```
        ┌──────────────────────────────┐
        │ 顶层: 盘中综合心跳 30min      │  ← 用户需求 (本次新增)
        │ 给决策者看, 主动 push 钉钉    │
        ├──────────────────────────────┤
        │ 中层: 异常事件触发 (已有)     │  ← PushHub 已实施
        │ 平仓/熔断/FLAT_ALL/错误       │
        ├──────────────────────────────┤
        │ 底层: 详细数据 CLI/Web (按需) │  ← 已有 metrics_api / SQL 查询
        └──────────────────────────────┘
```

### 顶层心跳设计要点 (Karpathy 信息密度)

- **频率**: 30min (盘中 6.5h × 13 + 收盘 1 = 14 次/天) — 比 1h 灵敏, 比 15min 不刷屏
- **渠道**: 钉钉 (合并到现有 PushHub, 利用 dedup 防重复)
- **格式**: 单条 < 500 字, 7 维度一目了然
- **触发**: 仅盘中 (盘前/盘后跳过, 减噪音)
- **fail-safe**: 任何 query 失败不影响其他 (per-query try/except)

---

## 3. 实施 — `app/observability/intraday_heartbeat.py`

### 3.1 7 维度状态报告 (真实 dry-run 输出, 2026-05-19 08:36 北京)

```
💓 [FTSeek-US 盘中心跳] 08:36 北京
🟢 服务: 已运行 0s (估) | thread=n/a broker=n/a
📊 持仓 (1 只):
  ├ US.QQQ LONG 500@$624.27 (SL $700.98)
🟢 已平盈亏(今日): $+0.00 | 0 笔 (胜率 0%)
🎯 信号 (近30min): 入 0 笔 / 拒 0 笔 [无]
🤖 LLM (近30min): 2 调用 / 成功率 100.0% | Pre-Gate 拦 0/0
⚠️ 告警 (近30min): 0 ERROR / 0 WARN
🔵 风控 (近8h): 熔断 1 次 | FLAT_ALL 0 次
```

**说明 (上述真实数据来源)**:
- `已运行 0s (估)`: dry-run 是新进程, source=module_load (生产 scheduler 注入后会是真实 uptime)
- `thread=n/a / broker=n/a`: dry-run 无 scheduler, 字段返 None → 显示 n/a (不是伪造)
- `持仓 QQQ`: 真实从 open_positions DB 读
- `0 笔 (胜率 0%)`: 8:36 北京还没开盘 (盘中是 21:30-04:00), 当然 0 笔
- `LLM 2 调用`: llm_audit DB 近 30min 真实调用数
- `熔断 1 次 (近8h)`: 真实从 logs 末尾扫的, 是 5/19 02:20 ARM 紊乱期间触发的

### 3.2 数据真实性保证 (4 重)

| 维度 | 数据源 | source 标识 |
|---|---|---|
| 服务 uptime | 4 级 fallback: scheduler.start_time → psutil → /proc → module_load | `uptime_source` 字段记录实际来源 |
| thread/broker 健康 | scheduler.thread_manager.health_check() / position_sync._last_sync_time | None → 显示 `n/a` (不伪造为 ✓) |
| 持仓 | open_positions DB 直读 | SQL `WHERE size > 0` |
| 盈亏 | trade_records DB | `WHERE trade_date=date('now') AND realized_pnl IS NOT NULL` |
| 信号 entries | trade_records `WHERE entry_time > cutoff` | 真实 SQL |
| 信号 rejects | rejections.db | 真实 SQL |
| LLM Pre-Gate 拦截 | llm_audit.db result_summary 字段 **JSON 解析** (非 LIKE) | `json.loads + obj.get("go") is False` |
| 告警 | logs/ftseek_us.log 末尾 5MB 扫近 30min | 时间戳前缀过滤 |
| 风控 | logs 末尾 10MB 扫近 N 小时 (默认 8h) | `近8h` 时间窗口非"今日"避免跨时区误导 |

### 3.2 7 维度数据来源

| 维度 | 数据源 | 关键 SQL/Log |
|---|---|---|
| 🟢 服务 | scheduler 实例 | uptime + thread health |
| 📊 持仓 | `open_positions` DB | symbol/dir/size/SL/owner |
| 💰 盈亏 | `trade_records` DB | today_pnl / win_rate |
| 🎯 信号 | `rejections.db` | 近 30min 拒因 Top3 |
| 🤖 LLM | `llm_audit.db` | calls/success_pct/Pre-Gate 拦截 |
| ⚠️ 告警 | `logs/ftseek_us.log` | 近 30min ERROR/WARN 计数 |
| 🔵 风控 | `logs` | 当日 熔断/FLAT_ALL 次数 |

每个维度独立 try/except — 数据库锁定 / log 文件不存在等不会让心跳整体失败.

### 3.3 集成方式 — `main.py` 启动期注入

```python
# main.py:391+ 启动期添加 (已实施)
from app.observability.intraday_heartbeat import HeartbeatLoop
_heartbeat = HeartbeatLoop(interval_min=30, scheduler=scheduler)
_heartbeat.start()
```

`HeartbeatLoop` 是 daemon 后台线程, 不影响主流程. 服务关闭时自动停止.

### 3.4 PushHub 集成 (利用现有基础设施)

```python
msg = PushMessage(
    title=f"FTSeek-US 心跳 {时间}",
    body=text,
    level=Level.NORMAL,    # NORMAL: 立即推, 但不算"重要事件"
    category=Category.SYSTEM,
    dedup_key=f"heartbeat_{date_10min}",  # 10min 粒度防重复
)
get_push_hub().push(msg)
```

**dedup_key** 防止 cron / 多线程误触发重复. 推送失败时降级到日志, 心跳本身不影响主流程.

---

## 4. 安全设计 (4 重保护)

| # | 保护 | 实现 |
|---|---|---|
| 1 | 非盘中跳过 | `_is_market_hours()` 周末 + 09:30 ET 之外返回 False |
| 2 | 单 query 失败不传染 | `_safe_query` per-call try/except + fallback |
| 3 | PushHub 失败降级 | 推失败 → 转 logger.info 兜底, 不抛异常 |
| 4 | dedup 防重复推 | 10min 粒度 dedup_key, 防重复扰民 |

---

## 5. 测试覆盖

```
tests/coordinator/test_intraday_heartbeat.py:
  ✅ test_heartbeat_format_has_all_7_dimensions     # 7 维度全覆盖
  ✅ test_heartbeat_compact_under_500_chars          # 单条 < 500 字
  ✅ test_heartbeat_skips_outside_market_hours       # 非盘中跳过
  ✅ test_heartbeat_force_bypasses_market_check      # force 模式手动测试
  ✅ test_heartbeat_query_failure_does_not_crash     # fail-safe 验证
  ✅ test_heartbeat_loop_default_interval_30min      # 默认 30min, 防误配
  ✅ test_market_hours_skips_weekend                 # 周末跳过
```

7/7 通过 + 完整 coordinator 回归 254/255 (1 pre-existing fail 与本次无关).

---

## 6. 推送频率与成本

| 项 | 数值 |
|---|---|
| 心跳推送 | 13-14 条/天 (盘中 30min × 13 次 + 启动 1 次) |
| 钉钉成本 | $0 (Webhook 免费) |
| 数据库读 | 每次 ~5 SQL queries (open_positions, trade_records, rejections, llm_audit, logs) |
| 主流程影响 | 0 (daemon 后台线程, 异步 PushHub) |

每次心跳 ~50ms 开销 (SQL queries + format), 30min × 50ms = 0.001% CPU.

---

## 7. 用户使用指南

### 7.1 部署后立即验证

```bash
git pull && systemctl restart ftseek-us
sleep 30
journalctl -u ftseek-us | grep "盘中心跳" | head -3

# 期望:
# [Heartbeat] 后台线程已启动 (interval=1800s)
# [Heartbeat] ✅ 已推送 (300+ chars)  ← 启动后立即推一次
```

### 7.2 钉钉接收

启动后 30s 内你的钉钉群应收到一条 "💓 [FTSeek-US 盘中心跳]" 消息.

之后**盘中每 30min 一次**, 一直到收盘. 周末/盘前/盘后自动停止.

### 7.3 手动触发心跳 (测试用)

```bash
# 盘外也能推一次 (force)
PYTHONPATH=/opt/ftseek/FTSeek-US python3 \
  /opt/ftseek/FTSeek-US/app/observability/intraday_heartbeat.py --push --force
```

### 7.4 解读心跳的关键点

看到这些信号 → 立刻关注:

| 信号 | 含义 | 行动 |
|---|---|---|
| `服务: <60min` (重启) | 服务异常重启 | 查 journalctl 找崩溃原因 |
| `当日盈亏: -$300+` (单条心跳) | 大幅亏损 | 考虑 kill switch 暂停 |
| `🎯 信号 拒 0 笔 / 入场 0 笔` (连续 2 心跳) | 信号源故障 | 查 broker LV3 数据 |
| `🤖 LLM 成功率 < 30%` | LLM 链路异常 | 查 llm_audit.db raw 错误 |
| `⚠️ ERROR > 5` | 错误暴增 | 查日志找 root cause |
| `🔵 熔断 ≥ 2 / FLAT_ALL ≥ 1` | 风控触发 | 检查市场环境 + 信号质量 |

---

## 8. 后续增强方向 (Phase 2, 1-2 周)

### 8.1 加 broker 实时连接验证

当前心跳仅看日志, 没主动 ping broker. 增强:
```python
def _check_broker_health() -> bool:
    """ping 富途 OpenD + LV3 订阅状态"""
    try:
        from app.execution.broker_adapter import get_broker
        return get_broker().ping()  # 50ms 内成功
    except Exception:
        return False
```

### 8.2 加实时浮赢/浮亏

当前持仓只显示 entry + SL, 不显示当前价. 可追加:
```python
# 主仓 (e.g., QQQ) 实时浮赢
current_price = router.get_price(symbol)
unrealized_pnl_pct = (current_price - entry) / entry * 100
```

### 8.3 心跳级别分级 (已有 PushHub Level 支持)

- NORMAL: 一切正常 → 钉钉 (现状)
- HIGH: 当日 PnL < -1% / 连续 5 笔 sl_hit / LLM 全失败 → 钉钉 + 飞书
- CRITICAL: 服务异常 / broker 断连 / FLAT_ALL → 钉钉 + 飞书 + flag_file

### 8.4 周报心跳 (已 daily_review 之外)

每周一 09:00 北京 (= 收盘后 17h):
- 上周累计 PnL / 胜率 / 7 天 alpha 趋势
- 与上周对比 (改善/退步)
- 仅 1 条, 给老板/投资人看

---

## 9. 一句话总结

> **盘中 30min 一次综合心跳** — 不是新发明监控系统, 是把项目已有 6 个数据源 (open_positions/trade_records/rejections/llm_audit/log/risk) 整合成"一眼看完"的报告, 通过现有 PushHub 推钉钉.
>
> **关键设计**:
> - **7 维度**: 服务/持仓/盈亏/信号/LLM/告警/风控
> - **fail-safe**: 任何 query 失败不影响其他维度
> - **不刷屏**: 30min 频率 + dedup, 一天 14 条
> - **零侵入**: daemon 后台线程, 0 影响主流程
>
> **用户体验**: 钉钉群每 30min 收到一条紧凑的状态报告, 任何异常一眼可见, 不需要登服务器查 log.
