# 5/15 复盘 + O1-O6 优化交付

> **日期**: 2026-05-16
> **基于**: `data/daily_reviews/daily_review_2026-05-15.json` + `logs/ftseek_us.log`

---

## 1. 5/15 关键事实

| 指标 | 数值 |
|-----|-----|
| 交易 | 14 笔 (12 closed) |
| 胜率 | **16.67%** (2 win / 10 loss) |
| 总 PnL | +$891.57 (全靠 ARM SHORT +$1219 / SNDK SHORT +$195 两笔 TRAILING_STOP) |
| 平均持仓 | 0.5 小时 |
| 当前回撤 | -1.49% (max -6.07%) |
| 拒入场 | 131 次 (TOP: PRE_TRADE_CHECK 96) |
| 计划执行率 | **0%** (5/5 plan_target 全部 missed) |

---

## 2. 诊断: 6 个核心问题 (按影响排序)

### O1. ❌ Sandbox universe 完全失效
- yaml `mode.sandbox.universe = [NVDA, TSLA, AAPL, MSFT, AMD, META, SPY, QQQ]`
- 实际交易: BA, CRM, NFLX, RIVN, SNDK, TME, ARM, ORCL — **全部 universe 外**
- **根因**: `scheduler_core.py:304` 修改 `self.watchlist`, 但 `polling_controller` 在 `:552` 从 `watchlist_us.json` 注册 — 完全跳过 sandbox 过滤.
- **影响**: 实验聚焦失败, 8 universe 0 笔交易, 8 个非 universe 标的反而成交.

### O2. ❌ ShadowMirror 0 笔镜像
- `shadow_trades` 表 5/15 无任何行
- **根因 1**: 主线 `mirror_entry` 只在 `_execute_long` (line 1545) 调用, **`_execute_short` / `_execute_stock_short` 没有**.
- **根因 2**: `ScalpingEngine._execute_trade` 完全无 `mirror_entry/mirror_exit` 调用.
- **影响**: Z1 (5/14 关键修复) 失效, A/B variant 数据为 0.

### O3. ❌ time_stop 退出规则负预期值
- 30 天窗口数据 (来自 exit_reason_pnl_report):
  - `time_stop`: n=7, win_rate=29%, avg_pnl=-0.53%, **sharpe=-0.40** → 🚨 disable
  - `TRAILING_STOP`: n=80, win=50%, sharpe=+0.23 → ✅ keep (最佳)
  - `tp_hit`: n=5, win=100%, sharpe=+3.32 → ✅ keep
- **影响**: 在 72h 关口强平亏损单 vs 让 trailing_stop 自然退出.

### O4. ❌ Voting 数据写入断裂
- 5/15 trade_records: 4 个 MAIN_PIPELINE 笔 `final_strength=0, consensus_level=0.0`
- 16 个 SCALPING 笔 `final_strength=NULL, consensus_level=NULL`
- **根因**: 
  - 主线 fast-path `signal_pipeline.py:908` 传 `_submit_order(..., None)` → voting_result={} → 写 0
  - Scalping `UnifiedTradeRecord.to_dict()` 不输出 `final_strength`, `consensus_level`, `market_regime`, `mode`
- **影响**: 复盘看不到信号质量, 无法区分运气与策略.

### O5. ❌ owner_strategy 22/26 = NULL
- 仅 4 笔 MAIN_PIPELINE 写 `owner_strategy='main'`
- 16 笔 SCALPING 全 NULL (`coordinator_summary.owner_strategy_distribution = {"<NULL>": 22, "main": 4}`)
- **根因**: 同 O4 (UnifiedTradeRecord 不写 owner_strategy)
- **影响**: ExitDispatcher 路由 + daily_review 按策略分组失效.

### O6. ❌ Plan execution 0% — `open_check_status: NO_DATA` x5
- 5/15 review:
  ```
  planned_targets: 5 (NVDA/QQQ/AMD/META/TSLA)
  executed_targets: []
  missed_opportunities: [全部 5 个]
  unplanned_trades: 8
  ```
- **根因 1** (`open_checker.py:99`): `quote.get("last_price", 0) == 0` — 但 `MarketDataRouter.get_quote` 返回字典键是 `price`, **不是 `last_price`** → 永远判 NO_DATA → `confirmed_entry_price` 永 NULL.
- **根因 2** (`premarket_planner.py:179`): `_price_calibration` 从 scheduler 读, **但全代码库无人写入** → LLM 无实时价格锚 → entry_zone 从训练数据幻觉 (例: NVDA plan entry_zone=[132.5, 135.0], 5/15 实际 $200+).
- **影响**: 计划完全失效, 实际交易完全脱离 LLM 决策.

---

## 3. 修复落地

### O1: Sandbox universe → polling_controller (`scheduler_core.py:565`)
```python
# 沙盒模式: polling_controller 也严格过滤 (universe ∩ watchlist 才注册)
_sandbox_universe = {f"US.{s}" if not s.startswith("US.") else s for s in _univ}
for stock in watchlist_with_tiers.get('us_stocks', []):
    if _sandbox_universe and symbol not in _sandbox_universe:
        _filtered_by_sandbox += 1
        continue
    polling_controller.register_symbol(symbol, tier)
logger.warning(f"[O1·SANDBOX] 严格过滤: 注册 {N}, 过滤 {M}, universe={...}")
```
**效果**: sandbox 启用时只轮询 8 universe, 其他 watchlist 标的不进入扫描.

### O2: ShadowMirror 全部入场点 (`order_executor.py:1206`, `scalping/engine.py:2161`)
- `_execute_stock_short` 入场后调 `mirror_entry(SHORT)`
- `ScalpingEngine._execute_trade` 入场后调 `mirror_entry(owner="scalping")`
- `ScalpingEngine._close_trade` 平仓后调 `mirror_exit` (与 entry 配对, 否则 baseline 永不闭环)

### O3: time_stop 禁用 (`exit_policy.yaml:108`, `position_exit_monitor.py:919`)
```yaml
time_stop:
  enabled: false   # O3 (2026-05-16): 30d sharpe=-0.40, win=29% 禁用
```
代码 fast-fail:
```python
if _ts_cfg.get("enabled", True) is False:
    return None
```
**回退**: 改回 `true` 即可重启.

### O4 + O5: 4 列回填 (`unified_trade_store.py:91`, `trade_store.py:117`, `order_executor.py:2383`, `scalping/engine.py:1955`)
- `UnifiedTradeRecord.__init__` 增加 4 个 kw: `owner_strategy`, `final_strength`, `market_regime`, `mode`
- `to_dict()` 输出对应键
- ScalpingEngine 传 `owner_strategy="scalping", strategy_id="scalping", mode="sandbox"|"normal", final_strength`
- 主线 `_close_position` voting 缺失时回退到 `signal_strength` (不再写 0)
- TradeStore SQL ALTER TABLE 增加 `market_regime` 列

### O6: open_checker 字段名 + premarket_planner 价格校准 (`open_checker.py:94`, `premarket_planner.py:172`)
- `open_checker._check_symbol` 兼容 `price` / `last_price` / `prev_close` / `previous_close` / `prev_close_price`
- `premarket_planner` 在 `_price_calibration` 为空时主动 fetch (sandbox universe 或 watchlist 前 15 只)

---

## 4. 验证

### 4.1 单元测试
```
135 passed in 0.55s
```
全部 coordinator 测试通过.

### 4.2 今晚开盘后验证 (~21:30)
```bash
# O1: sandbox 严格过滤
journalctl -u ftseek-us -n 200 | grep "O1·SANDBOX"

# O2: shadow_trades 应该有 5/16 入场
sqlite3 data/ftseek_us.db "SELECT COUNT(*), strategy FROM shadow_trades WHERE date(entry_time)='2026-05-16' GROUP BY strategy;"

# O3: time_stop 不应触发 (理论上)
journalctl -u ftseek-us | grep "TIME_STOP"   # 应为 0

# O4+O5: 新写入的 trade_records 不再 NULL
sqlite3 data/ftseek_us.db "SELECT owner_strategy, final_strength, consensus_level, market_regime, mode FROM trade_records WHERE date(entry_time)='2026-05-16' LIMIT 10;"

# O6: open_checker 应该看到真实价格
journalctl -u ftseek-us | grep "📊 \[US\." | head -10
# 应该输出 Price: 200.xx, PrevClose: ..., Gap: 0.xx%

# O6: premarket_planner 自动校准
journalctl -u ftseek-us | grep "盘前计划·O6"
# 应该 "自动校准价格: N 只标的"
```

### 4.3 收盘后 (~04:30) 决策
```bash
python scripts/coordinator_status.py --all
python scripts/shadow_compare.py --variant-diff
python scripts/exit_reason_pnl_report.py --days 1
```

期望:
- `executed_targets > 0` (plan 终于能匹配)
- `shadow_by_variant: [...]` 4 个 variant 都有数据
- `owner_strategy_distribution` 不再 22/26 NULL
- `time_stop` 不出现在 exit_reason_pnl

---

## 5. 还未做 (诚实清单)

1. **PnLOutlierMonitor 启动期 stale stats** (5/15 AM 误判 ORCL +$17K 10 次) — 当天 22:20 已自校到 n=247 mean=$59 std=$1278, 不再是问题. 历史污染数据可手动 DELETE 但不紧迫.
2. **LLM plan 实质质量** — entry_zone 是否合理 (O6 给了价格但 LLM 仍可能写错) — 需开盘后看一周.
3. **Scalping 信号质量** — 16/16 笔 `signal_strength=66, confidence=0.667` (硬编码 `len(entry_reasons)*33` 的 2/3), 没有真实差异化评分. 需结构性重构 (O8 候选).
4. **plan_target 的 strategy_id** 字段始终是 `BATTLE_PLAN_FAST_PATH`, 不分 main/scalping bucket — 这与 Phase A coordinator 模型不完全契合. 待 Phase B enforce 时一并修.

---

## 5.5 O7: Sandbox Universe 重平衡 (2026-05-16 追加)

### 问题
- 原 universe 8 只: `NVDA, TSLA, AAPL, MSFT, AMD, META, SPY, QQQ`
- AAPL/MSFT/META 属于低波动大盘 — 5/15 0 笔信号触发, ATR% 在 0.05-0.08% 之间, scalping/技术信号难触发
- 实际有信号触发的高波动小盘 (RIVN/LCID/SNDK/ARM/NIO/BWXT/MU) 被 O1 严格过滤后**全部排除**

### 修复 (`unified_config.yaml:21`)
**新 universe 12 只**, 三层分组:
```yaml
universe:
  # Anchor (大盘半导体 + 龙头, LLM 计划友好)
  - NVDA, TSLA, AMD
  # 高波动小盘 (Scalping 友好, 信号易触发)
  - RIVN     # ATR%≈0.20%
  - LCID     # ATR%≈0.19%, 与 TSLA 非完全同步
  - SNDK     # 高波存储芯片, 5/15 SHORT +$195
  - ARM      # 5/15 SHORT +$1219 (单日最大盈利)
  - NIO      # 中概 EV
  - BWXT     # 唯一 SHORT 信号高触发的工业股
  - MU       # 存储周期股
  # Beacon (大盘+科技基准)
  - SPY, QQQ
```

干掉: AAPL, MSFT, META (低波动 anchor, 沙盒下信号干涸)

### 预期 5/16 改善
| 维度 | 5/15 (旧 universe 但 O1 未生效) | 5/16 (新 universe + O1) |
|-----|-----|-----|
| 信号触发频率 | scalping 16 笔 (绕过 universe) | 应 > 0 (universe 内合规) |
| 计划匹配 | NVDA/QQQ/AMD/META/TSLA 计划 0 触发 | NVDA/TSLA/AMD 仍在, 但 entry_zone O6 已校准 |
| Scalping owner_strategy | NULL (O4 修复前) | "scalping" 填充 |
| 单日 PnL 来源 | ARM/SNDK SHORT 巨利 (universe 外) | 同样 ARM/SNDK 但合规 universe 内 |

---

## 6. 一键回滚

```bash
# 关沙盒 (O1 自动失效)
yq -i '.mode.sandbox.enabled = false' config/unified_config.yaml

# 重开 time_stop (O3)
yq -i '.exit_rules.time_stop.enabled = true' app/risk/exit_policy.yaml

# O2/O4/O5/O6 都用 try/except 包裹, 失败降级不影响实盘, 无需主动回滚

systemctl restart ftseek-us
```

---

## 7. 经验沉淀

1. **字段名漂移是隐 killer**: O6 `last_price` vs `price` 让整个 LLM 计划链路死了几个月.
2. **"应该写但没人写" 同样致命**: O6 `_price_calibration` 从 scheduler 读但全代码库无写入 — 单纯 grep 调用方就能发现.
3. **配置作用域要追到根**: O1 sandbox 改了 `self.watchlist` 但 `polling_controller` 是另一个数据源, 修补不彻底.
4. **退出原因有量化判据时, 听数据**: O3 sharpe=-0.40 是 30 天 7 个样本的明确信号, 不再用直觉.
5. **持久化要默认完整 schema**: O4/O5 4 列散落 (owner_strategy / final_strength / market_regime / mode) 各 writer 选择性填充 → 复盘全 NULL. Unified store 必须把所有列写齐.

---

**结论**: 6 项今晚全部上线, 135/135 测试通过. 明日收盘后可一次性验证 4 项数据落库 + sandbox 聚焦效果 + plan 执行率提升.
