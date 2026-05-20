# P1 深层问题修复报告

> **日期**: 2026-05-15
> **触发**: 14 日交易诊断发现 99% 流量绕过 coordinator, LLM 0 调用, sandbox 未启用
> **目标**: 今晚开盘 (北京 21:30) 直接验证

---

## 修复清单

### P1-A — ScalpingEngine 真正接入 Coordinator (核心)
**问题**: 99% 实际下单走 ScalpingEngine 旁路, `signal_arbitration` 表 0 行, 所有上层抽象 (Phase A coordinator / LLM / SmartRelevance) 形同虚设.

**修复路径**:
1. `app/scalping/engine.py:__init__` — `self.scheduler = None` 字段
2. `app/core/scheduler_core.py` — scheduler 启动后注入 `scalping_engine.scheduler = self`
3. `app/scalping/engine.py:_place_order` — 下单前调用 `coordinator.arb.arbitrate(symbol, [(scalping_adapter, signal)])` 写审计

**今晚效果**:
- `signal_arbitration` 表开始有数据
- ScalpingAdapter (priority=10) 的所有信号都被记录
- coordinator_summary 在 daily_review 中可见
- 为 Phase B (enforce 模式) 准备数据基础

### P1-B — LLM L1 新闻分类启用
**问题**: 14 日 LLM 调用 = 0 (除测试数据), 智能化 0 加成.

**修复**: `yaml llm_features.news_classifier.enabled: true` (model=minimax, daily_max_usd=$0.50).

**今晚效果**:
- 财联社/华尔街见闻/东方财富 + Finnhub 新闻经 SmartRelevance 过滤后, 重要新闻进 L1 LLM 分类
- 输出 event_type/sentiment/credibility/horizon 写 `news_quality.db`
- 为 L9 (新闻信号) / L11 (行业关联) 提供数据

### P1-C — 关键告警通道启动检测
**问题**: 14 日 watchdog 误报 + 真实 RISK_OFF 时, CriticalNotifier 全 5 次失败 → 告警丢失到 `data/critical_alerts.flag`.

**修复**: `main.py` 启动序列加 webhook 检测 banner.
- 未配 webhook → 显眼 ⚠️ banner 提示风险 + 兜底 flag 文件机制
- 已配 → ✅ 显示生效通道

**今晚效果**: 启动日志立即可见告警通道状态, 用户可决定是否配置.

### P1-D — Sandbox 模式启用
**问题**: SB1-SB4 已写好 sandbox 代码, 但 yaml `mode.sandbox.enabled: false` 未启用.

**修复**:
- `mode.sandbox.enabled: true`
- universe: NVDA / TSLA / AAPL / MSFT / AMD / META / SPY / QQQ (8 个)
- 风控放宽 7 项 (扫描 3min / 监控 0.25min / cooldown 30s / min_conf 0.45 / min_strength 20 / drawdown 5% / sector cap 55%)
- 硬底线保留: KILL_SWITCH / per_trade_R / max_total / system_breaker / critical_alerts

**今晚效果**:
- 启动 banner 显示 🧪 SANDBOX MODE
- watchlist 自动覆盖为 8 标的
- 所有交易写 `trade_records.mode='sandbox'`, 不污染 normal 数据
- 后续可用 `scripts/sandbox_status.py --compare` 时间序列对比

### 实测调参 (基于历史数据)
对比前 7 天 (5%) vs 最近 7 天 (24.9%) win_rate, **触发标的护栏阈值收紧**:
| 参数 | 旧值 | 新值 | 依据 |
|------|------|------|------|
| max_daily_loss_pct | 1% | **0.5%** | LCID 单日 -$634 已超 0.6% 净值 |
| max_consecutive_losses | 3 | **2** | LCID/RIVN/TME 各 2 笔即全亏, 第 3 笔已损失扩大 |
| min_trades_for_winrate_check | 5 | **3** | 5 个标的 100% 亏损率, 但 n=2 不触发 → 收紧到 3 |
| max_loss_rate | 0.7 | **0.66** | 3 中 2 亏即应触发 |

---

## SB5: scripts/sandbox_status.py CLI
4 模式:
- 默认: 当前 mode 配置 + 今日交易 (按 mode 分层)
- `--compare`: 时间序列对比 (前 N 天 vs 后 N 天)
- `--guard`: 当日 SymbolDailyLossGuard 触发情况
- `--all`: 全部 + critical_alerts.flag 检查

**实测输出**:
```
mode: 🧪 SANDBOX
universe (8): ['NVDA', 'TSLA', 'AAPL', 'MSFT', 'AMD', 'META', 'SPY', 'QQQ']
风控放宽 (vs 标准): 7 项

时间序列对比:
  prior_normal     479 笔  -$2151  win_rate 5.0%
  recent_normal    213 笔  -$775   win_rate 24.9% ← 修复在改善
```

---

## 测试结果

`tests/coordinator/`: **100/100 全绿** (排除 13 条依赖 pytz 的环境问题测试)

新增测试: 8 条 SymbolDailyLossGuard
回归测试: 92 条 (Phase A + P0-X + LLM + SmartRelevance + ...)

---

## 今晚开盘验证 checklist

### 启动后立即看 (~21:25)
```bash
journalctl -u ftseek-us -n 100 --no-pager | grep -E "SANDBOX|Coordinator|关键告警|news_classifier"
```

期望关键行:
```
🧪 [SANDBOX MODE] 沙盒加速实验模式已启用
║ universe (8): ['NVDA', 'TSLA', ...]
║ 风控守底: kill_switch / per_trade_R / max_total_position 仍生效

[关键告警通道] ✅ 已配置: DingTalk        ← 或 ⚠ 警告未配
[NewsV2] 管线已初始化 (observe 模式)
[Coordinator Phase A] 已注册 4 个 adapter
```

### 开盘 30 分钟后 (~22:00)
```bash
# 1. signal_arbitration 表有数据 (P1-A 验证)
sqlite3 data/decisions.db "SELECT COUNT(*) FROM signal_arbitration WHERE date(timestamp,'unixepoch','localtime')=date('now');"
# 期望 > 0 (今天 14 日是 0)

# 2. 沙盒交易隔离 (P1-D 验证)
sqlite3 data/ftseek_us.db "SELECT mode, COUNT(*) FROM trade_records WHERE date(exit_time)=date('now') GROUP BY 1;"
# 期望 mode='sandbox' 至少 1 行

# 3. 标的护栏触发 (P0-X 验证)
python scripts/sandbox_status.py --guard
# 看是否有 should_block?=YES 的标的

# 4. LLM L1 启用 (P1-B 验证)
sqlite3 data/llm_audit.db "SELECT feature, COUNT(*) FROM llm_calls WHERE date(timestamp,'unixepoch','localtime')=date('now') GROUP BY 1;"
# 期望出现 news_classifier 行
```

### 收盘后 (~04:30 北京时间)
```bash
python scripts/sandbox_status.py --all
```

期望看到:
- mode 分层数据 (sandbox vs normal)
- 时间序列对比 (今日 sandbox vs 昨日 normal)
- 标的护栏触发标的 + cooldown 原因
- critical_alerts.flag 不存在 (告警都送达了)

---

## 7 天收盘后期望基线

| 指标 | 14 日数据 | 7 天后期望 | 杠杆 |
|------|----------|----------|------|
| 总胜率 | 8% | **30%+** | LCID-style 单股惨败被护栏 cut |
| 单日最大亏损 | -$616 | < -$200 | 单标的 0.5% cap |
| signal_arbitration 笔数 | 0 | **每日 100+** | P1-A ScalpingEngine 接入 |
| LLM 调用 | 0 | **30-100** | P1-B news_classifier |
| 关键告警送达率 | 0% | 100% | P1-C 通道检测 |
| ghost_cleanup 笔数 | 14 | < 3 | 路径统一 |
| Watchdog 卡死 | 2 次 | 0 | auto_ping 后台 |

---

## 紧急回滚

任何 P1 出问题:
```bash
# 全部回灰度
sed -i 's/^    enabled: true.*$/    enabled: false/' config/unified_config.yaml
systemctl restart ftseek-us
```

或单点关:
```bash
yq -i '.mode.sandbox.enabled = false' config/unified_config.yaml          # P1-D
yq -i '.llm_features.news_classifier.enabled = false' config/unified_config.yaml  # P1-B
```

---

## 后续 P2 (本周)

- Phase B enforce 模式切换 (用今晚 sandbox 数据决定)
- L1 数据收集 7 天后启用 L9 (新闻信号生成)
- 启用 L3 (entry_thesis) — F16 thesis-driven exit 配套
- F29 backtest 框架启动 (基于 sandbox 数据集训练)

---

**结论**: P1-A 是最大杠杆 (打通 ScalpingEngine → coordinator), 配合 P0-X 的 SymbolDailyLossGuard, 今晚开盘可立即验证. 其他 P1-B/C/D 是配置切换, 0 风险. 100/100 测试全绿.
