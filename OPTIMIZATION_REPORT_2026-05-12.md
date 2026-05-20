# FTSeek-US 全链路优化与修复报告

> **生成日期**: 2026-05-12
> **目标读者**: 执行修复的下一个 agent
> **交付形式**: 每个 Fix 项包含「现状 / 问题 / 方案 / 验收」四要素，可直接照做
> **覆盖范围**: 盘前作战计划 → 盘中信号识别 → 止损止盈 → 盘后复盘 → 自动化进化 → 回测框架 → SSOT 治理

---

## 0. 阅读指南（必读）

### 0.1 报告结构
- 共 33 个 Fix 项（F1–F33），按功能模块分组
- 每项独立可修复，无强制顺序，但优先级标注清晰
- 优先级三档：**P0 立即（本周）** / **P1 重要（本月）** / **P2 系统性（本季）**

### 0.2 修复约定
- 所有「文件:行号」基于本次调研版本（2026-05-12 快照）
- **本项目不是 git 仓库**，所有改动不可回滚 — 改前必读相关文件，改后跑测试
- 已知缺失：`app/data/`、`app/shared/`、`app/analysis/` — 在本机 import 会失败，**只用静态分析方式验证**
- 所有 P0 修复必须配单测；P1 至少配 1 条集成测试

### 0.3 待完成的修复
- `battle_plan_manager.get_plan` 双重定义（删第一份）
-  `RiskEngine.pre_trade_check` 强制刷绿 breaker（已移除）
-  `app.core.config` import 收敛到 `app.core.config_container`
-  `RiskGateway` 已创建（`app/risk/risk_gateway.py`，含 14 个单测）
-  `RiskStateMachine` 单测覆盖（51 个）

### 0.4 系统现状速览

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          盘前 (PRE_MARKET)                              │
│  USPreMarketPlanner → daily_battle_plan_us.json → BattlePlanManager     │
│  ⚠ run_open_checker 永远不被调用 (D1) — CANCELLED 路径死链              │
└─────────────────────────────────────────────────────────────────────────┘
                                 ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                          盘中 (TRADING)                                 │
│  IntradayScorer (6 因子) → VotingCoordinator → RiskGateway → 下单       │
│  ⚠ 因子权重无回测背书 / min_confidence 14 处分散                        │
└─────────────────────────────────────────────────────────────────────────┘
                                 ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                       持仓 (POSITION_HOLD)                              │
│  PositionExitMonitor (6 规则: Flash/DayTrade/ScaleOut/Trail/Time/PreClose) │
│  ⚠ Day Trade Mode R/R = 0.6 (负 EV) / profit_lock 框死利润              │
│  ⚠ 6 规则按顺序「先到先得」, 无质量评分                                 │
└─────────────────────────────────────────────────────────────────────────┘
                                 ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                      盘后 (POST_MARKET)                                 │
│  daily_review → review_loop → ConfigWriter (仅 4 参数白名单)            │
│  ⚠ DB 路径硬编码 /opt/ftseek/ (D2) / trade_attribution 无 CREATE (D4)   │
└─────────────────────────────────────────────────────────────────────────┘
                                 ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                    进化 (EVOLUTION) — 大部分死代码                      │
│  app/deep_review/ 整个模块 死代码 (D5)                                  │
│  EnhancedDebate / disagreement_registry 默认关闭 (D6)                   │
│  ImprovementTracker / AutoTuner 占位实现                                │
│  ⚠ 唯一生效路径: review_loop → ConfigWriter (4 个白名单参数)            │
└─────────────────────────────────────────────────────────────────────────┘
                                 ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                    回测 (BACKTEST) — 完全缺失                           │
│  app/backtest/ 空目录, walk-forward 无任何代码                          │
│  ⚠ 所有阈值变更没有 OOS 验证 → 经典 overfitting 风险                    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 1. 优先级矩阵

| 模块 | P0（立即） | P1（本月） | P2（季度） |
|------|-----------|-----------|------------|
| 盘前 | F1 OpenChecker 死链 | F2 lesson_injector 路径统一 | F3 plan revalidate 强制 |
| 信号 | F5 追高对冲反模式 / F6 change_pct 用日累计 | F4 min_confidence 收敛 / F7 opening_drive 拆分 | F8 因子 IC 月度归因 / F9 walk-forward 调权重 |
| 止损止盈 | **F10 风险单位 R 统一** / F11 Day Trade TP 重设 / F15 trail_state latent bug | F12 profit_lock 调整 / F13 退出质量评分 / F14 PRE_CLOSE 简化 / F17 组合止损 | F16 入场→退出对称 thesis-driven |
| 盘后 | **F18 DB 路径** / **F19 trade_attribution CREATE** / F20 link 缺 decision_id | F21 suggestion 列对齐 / F22 exit_reason×pnl 报告 / F23 lessons DB 路径 | F24 deep_review 整改 |
| 进化 | F28 enhanced_debate 路径修正 | F25 ImprovementTracker 持久化 / F27 review_loop 扩白名单 | F26 AutoTuner 重写或删 |
| 回测 | — | **F29 minimal backtest framework** | **F30 walk-forward** |
| SSOT | F31 min_confidence 单源 | F32 exit_policy 硬编码消除 | F33 drift_detector 定时 |

---

# 第一部分：盘前作战计划（Pre-market Planning）

## 1.1 现状链路

```
ET 08:30          ET 09:15-09:25     ET 09:30
   │                    │                │
   ▼                    ▼                ▼
USPreMarketPlanner → USOpenChecker → BattlePlanManager.check_trade
   │                    │                │
   ├─ 拉新闻 (RSS/Finnhub)                ├─ Gap >3% → CANCELLED
   ├─ 取 VIX/SPY (Yahoo)                  ├─ 方向背离 → CANCELLED
   ├─ 调 Qwen-Max 生成 plan               └─ status="VALID" + entry_price
   ├─ 注入 lesson_injector            ⚠ 实际不被调用 (D1)
   └─ 落盘 daily_battle_plan_us.json
```

## F1 — OpenChecker 死链（CANCELLED 路径不可达）

**优先级**: P0
**文件**: `app/core/scheduler_core.py:1080-1124`、`app/us_pipeline/open_checker.py`

**现状**:
```python
# app/pipeline/llm_pipeline.py:1126-1165 — 函数已实现
def run_open_checker(scheduler):
    if not (now_et.hour == 9 and 15 <= now_et.minute <= 25):
        return
    # ... 调用 USOpenChecker._check_symbol() ...
```
但 `grep "run_open_checker(" app/` 全工程**只有 import，没有任何模块实际调用此函数**。`scheduler_core._llm_strategy_loop` 的 PRE_MARKET 分支只调用了 `run_premarket_planning`，未调用 `run_open_checker`。

**问题**: 所有 target 永远停留在 `status="PRE_MARKET"`，`USOpenChecker` 写入的 `confirmed_entry_price`、`gap_pct`、`open_check_status="CANCELLED"` 全部不会发生。下游 `BattlePlanManager.check_trade()` 永远拿不到 CANCELLED 信号，盘前过滤层完全失效。

**修复方案**:
1. 在 `app/core/scheduler_core.py` 的 `_llm_strategy_loop()` 增加 OpenChecker 触发分支：
```python
# scheduler_core.py — 在 PRE_MARKET 分支后追加
elif phase == MarketPhase.OPENING_AUCTION:  # 09:15-09:25 ET
    from app.pipeline.llm_pipeline import run_open_checker
    run_open_checker(self)
```
2. 或者，在 `run_premarket_planning` 完成后挂载一个延时触发器（推荐做法）：在 `scheduler_triggers.py` 增加新的 `check_open_checker()`，参考 `check_daily_review()` 的模式。
3. 必须保证 09:25 之前必须执行一次 — 否则开盘后 BattlePlanManager 仍然按 `PRE_MARKET` 状态过滤。

**验收标准**:
- 单测：mock 时间为 09:20 ET，启动 scheduler 主循环 1 轮，断言 `USBattlePlanManager()._plan["targets"][0]["status"]` 出现 `"VALID"` 或 `"CANCELLED"`
- 集成测试：构造一个 plan with 3 targets（一个无 gap、一个 gap=5%、一个方向背离），跑完 OpenChecker 后断言 `[VALID, CANCELLED, CANCELLED]`
- 实盘验证：09:30 开盘后查 `data/daily_battle_plan_us.json`，所有 target 不应再有 `status="PRE_MARKET"`

---

## F2 — daily_battle_plan_us.json 路径不一致

**优先级**: P1
**文件**: `app/us_pipeline/premarket_planner.py:599-604`、`app/llm/production_monitor.py:32`、`app/us_pipeline/battle_plan_manager.py:78-87`

**现状**:
```python
# premarket_planner.py:599-604 — 写
plan_path = os.path.join(os.path.dirname(project_root), 'data', 'daily_battle_plan_us.json')

# production_monitor.py:32 — 读 (硬编码生产路径)
PLAN_PATH = "/opt/ftseek/FTSeek-US/data/daily_battle_plan_us.json"

# battle_plan_manager.py:78-87 — 读 (相对路径)
return os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data', 'daily_battle_plan_us.json'
)
```

**问题**: 三个模块用三种路径计算方式。在不同部署环境（本机开发 / `/opt/ftseek` 生产）下，写入和读取可能指向不同文件，造成 plan 看起来"丢失"。

**修复方案**:
1. 在 `app/core/config_container.py` 增加路径常量：
```python
@property
def battle_plan_path(self) -> str:
    base = self.get_dotted("paths.data_dir", None)
    if not base:
        base = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'data')
    return os.path.join(base, 'daily_battle_plan_us.json')
```
2. 在 `unified_config.yaml` 增加 `paths.data_dir: data/`（相对项目根）
3. 替换上述 3 个文件中的硬编码路径为 `get_config().battle_plan_path`

**验收标准**:
- `grep "daily_battle_plan_us.json" app/` 应只出现在 `config_container.py` 一处
- 在不同 `cwd` 下启动 main.py，确认 plan 写入和读取路径一致
- 单测：mock `paths.data_dir` 为 `/tmp/test_data`，断言 plan 写入到该位置

---

## F3 — BattlePlanManager.revalidate_on_restart 不被强制调用

**优先级**: P2
**文件**: `app/us_pipeline/battle_plan_manager.py:355-420`、`main.py:212-296`

**现状**: `revalidate_on_restart()` 实现完整（自动修复 CANCELLED targets / 移除冲突项 / 持久化），但 `main.py` 启动序列中**未发现强制调用**。

**问题**: 服务重启后如果加载到陈旧/损坏的 plan，会无声运行直到下次盘前重新生成（间隔可能 24h+）。

**修复方案**:
在 `main.py` 装载 BattlePlanManager 后立即调用：
```python
# main.py:_load_battle_plan() 末尾
plan_mgr = USBattlePlanManager()
plan_mgr.load(plan_path)
if not plan_mgr.revalidate_on_restart():  # 新增
    logger.error("[启动] battle plan 校验失败且自动修复未通过, 进入 RISK_OFF")
    RiskStateMachine().set_state(RiskState.RISK_OFF, "stale_battle_plan", "main")
```

**验收标准**:
- 启动日志出现 `[计划·重启校验]` 行
- 单测：构造一个含 entry_zone=None 的 plan，启动后断言该 target 被移除

---

# 第二部分：盘中信号识别与触发

## 2.1 现状链路

```
每 5min/扫描周期
  │
  ▼
MarketDataRouter.get_full_market_context(symbol)
  │
  ▼
IntradayScorer.score(context)
  │ ┌─ F1 trend (MA 位置 + 斜率 + SPY 共振)
  │ ├─ F2 momentum (RSI 水平 + 梯度 + MACD + change_pct)
  │ ├─ F3 volume (vol_ratio + 主力流入)
  │ ├─ F4 orderflow (LV3 盘口失衡)
  │ ├─ F5 lifecycle (二阶 EMA momentum + 拐点检测)
  │ └─ F6 volatility (VIX regime 调节器)
  │
  ▼
IntradaySignal {direction, strength, confidence, position_pct, stop_loss_pct}
  │
  ▼
to_vote_result() / decompose_to_votes()
  │
  ▼
VotingCoordinator → ConsensusEngine (盘前/盘后用 LLM 投票)
  │
  ▼
SignalScore → RiskGateway → _submit_order
```

## F4 — `min_confidence` 多源散布（14 处）

**优先级**: P1
**文件**: `config/unified_config.yaml`（14 处）、`app/voting/voting_coordinator.py`、`app/risk/pre_trade_check.py:54`、`app/pipeline/signal_pipeline.py:269-279`

**现状**:
```yaml
# unified_config.yaml — 14 个不同位置都叫 min_confidence
risk.signal_quality.min_confidence: 0.6      # ConfigWriter 唯一映射的 SSOT
signal_pipeline.profile.[level].min_confidence  # 各 profile
risk_limits.min_confidence: 0.7
scheduler.min_confidence: 0.5
... (共 14 处)
```
代码中混用 `cfg.get_dotted("scheduler.min_confidence")`、`cfg.get_dotted("risk.signal_quality.min_confidence")`，调用方各凭意愿读不同节点。

**问题**: 调参时改一处不一定生效；ConfigWriter 自动调参只改 `risk.signal_quality.min_confidence`，其他 13 处不动 → 最终生效门槛是哪个？无人能说清。

**修复方案**:
1. 在 `app/core/config_container.py` 新增 `MinConfidenceResolver`：
```python
class MinConfidenceResolver:
    """min_confidence 单一真相源 — 所有调用方只能从这里取"""
    def for_phase(self, phase: str) -> float:
        # phase = "intraday" / "premarket" / "earnings" / "shadow"
        # 内部按优先级：lesson_injector 动态约束 > risk.signal_quality.min_confidence > 0.6
        ...
```
2. 全工程替换：
```bash
# 所有 cfg.get_dotted("XXX.min_confidence") → resolver.for_phase("XXX")
```
3. `unified_config.yaml` 内删除 13 处冗余 `min_confidence`（或加注释 `# DEPRECATED, use risk.signal_quality.min_confidence`）

**验收标准**:
- `grep "min_confidence" app/` 调用点全部走 `MinConfidenceResolver`
- 单测：修改 `risk.signal_quality.min_confidence` 后，所有 phase 的有效门槛同步变化

---

## F5 — IntradayScorer 追高对冲反模式

**优先级**: P0
**文件**: `app/core/intraday_scorer.py:509-515`

**现状**:
```python
# 4) 短期价格变化率
if change_pct > 0:
    if change_pct < 4:
        bull_score += change_pct / 8  # 2% → 0.25
    else:
        bull_score += 0.3    # 涨幅过大, 降低追入意愿
        bear_score += 0.3    # 同时增加回调风险
else:
    bear_score += min(abs(change_pct) / 8, 1.0)
```

**问题**: 单日涨 4%+ 时同时给 bull 和 bear 各加 0.3 → 等于把"涨太多"变成零信号。但策略师视角：
- 财报后跳涨 → 大概率延续（Earnings Drift 文献支持）
- 突破历史新高 → 趋势加速（动量策略最赚的场景）
- 无新闻拉升 → 才有回调风险

一刀切对冲是**主动放弃 momentum tail**。

**修复方案**:
```python
if change_pct > 0:
    if change_pct < 4:
        bull_score += change_pct / 8
    else:
        # 区分催化剂上下文
        has_catalyst = (
            ctx.get("is_earnings_period", False) or
            ctx.get("news_sentiment", 0) > 0.5 or
            price > ctx.get("high_52w", float('inf')) * 0.99  # 突破新高
        )
        if has_catalyst:
            bull_score += 0.6  # 强催化下追入合理
        else:
            bull_score += 0.2  # 无催化保留中性偏多
else:
    bear_score += min(abs(change_pct) / 8, 1.0)
```

**验收标准**:
- 单测：`change_pct=5, is_earnings_period=True` → bull_score 增量 ≥ 0.5
- 单测：`change_pct=5, news_sentiment=0` → bull_score 增量 ≤ 0.3，bear_score 不再被加 0.3
- 历史回测（F29 完成后）：对比修改前后在过去 60 个交易日 momentum top10 标的的 PnL

---

## F6 — `change_pct` 用日累计而非短期变化

**优先级**: P0
**文件**: `app/core/intraday_scorer.py:468`

**现状**:
```python
def _score_momentum(self, ctx, price, weight):
    ...
    change_pct = float(ctx.get("change_percent", 0))  # 这是相对前日收盘的累计变化
```

**问题**: `change_percent` 在 14:00 决策时是"今天累计已经涨了 X%"，对**当下进场**毫无指导意义（早就涨完了）。日内 momentum 应该看 5min/15min 短期斜率。

**修复方案**:
1. 在 `MarketDataRouter.get_full_market_context()` 增加字段：
```python
context["slope_5m"] = (price - bars_5m[-5].close) / bars_5m[-5].close * 100  # 最近 25min 斜率
context["slope_15m"] = (price - bars_15m[-3].close) / bars_15m[-3].close * 100  # 最近 45min 斜率
context["distance_from_30m_high"] = (price - max(bars_5m[-6:].high)) / price  # 距 30min 高点
```
2. 在 `intraday_scorer._score_momentum` 用 `slope_5m` 替代 `change_pct`：
```python
slope_5m = float(ctx.get("slope_5m", 0))  # 替代 change_pct
distance_from_high = float(ctx.get("distance_from_30m_high", 0))

if slope_5m > 0:
    if slope_5m < 1.5:  # 25min 内涨 <1.5% — 温和向上
        bull_score += slope_5m / 3
    else:  # 25min 内涨 >1.5% — 加速
        bull_score += 0.5 if distance_from_high > -0.005 else 0.2  # 接近高点 vs 已远离
```
3. 保留 `change_pct` 字段供其他模块使用（不破坏兼容）

**验收标准**:
- 单测：input `slope_5m=2, change_pct=8` → momentum bull_score 反映 5m 而非 8%
- 集成验证：在 14:00 触发的信号中，对比修改前后的 LONG 入场胜率

---

## F7 — `opening_drive` session 一刀切压制

**优先级**: P1
**文件**: `app/core/intraday_scorer.py:217-224`

**现状**:
```python
multipliers = {
    "opening_drive":  (0.75, 0.85),   # 09:30-09:50 一刀切压低
    ...
}
```

**问题**: 09:30-09:50 是日内 alpha 最高的窗口，一刀切压制 = 主动放弃。应区分 setup 类型：
- Gap+Volume confirm → opening_drive 最强信号（保持 1.0×）
- Flat open + 窄幅震荡 → 才该谨慎（降到 0.7×）

**修复方案**:
```python
def _session_multiplier(ctx: Dict = None) -> Tuple[float, float]:
    session = get_shared_kernel().get_current_session()
    multipliers = {
        "morning":        (0.90, 0.95),
        "midday":         (0.85, 0.80),
        "afternoon":      (0.95, 1.00),
        "closing_drive":  (1.00, 1.10),
    }
    if session == "opening_drive":
        if ctx is None:
            return (0.85, 0.90)  # 默认中性
        # Gap + Volume 双重确认
        gap_pct = abs(ctx.get("gap_pct", 0))
        vol_ratio = ctx.get("vol_ratio", 1.0)
        if gap_pct > 0.5 and vol_ratio > 1.5:
            return (1.0, 1.0)   # 强 setup 不压制
        elif gap_pct < 0.2 and vol_ratio < 1.2:
            return (0.7, 0.8)   # 弱 setup 重压制
        else:
            return (0.85, 0.90) # 默认
    return multipliers.get(session, (0.80, 0.80))
```
Caller 改：`session_conf_mul, session_str_mul = _session_multiplier(context)`

**验收标准**:
- 单测：`session=opening_drive, gap_pct=1.5, vol_ratio=2` → 返回 (1.0, 1.0)
- 单测：`session=opening_drive, gap_pct=0, vol_ratio=1` → 返回 (0.7, 0.8)
- 回测：09:30-09:50 入场的 PnL 分布对比

---

## F8 — IntradayScorer 因子权重无任何回测背书

**优先级**: P2（依赖 F29 完成后才能做）
**文件**: `app/core/intraday_scorer.py:251-268`

**现状**:
```python
DEFAULT_WEIGHTS = {
    "trend":     0.25,
    "momentum":  0.25,
    "volume":    0.18,
    "orderflow": 0.17,
    "lifecycle": 0.15,
}
REGIME_WEIGHT_MODIFIERS = {
    "LOW_VOLATILITY":     {"trend": +0.05, "momentum": +0.05, ...},
    ...
}
```

**问题**: 所有权重系数（0.25/0.25/0.18/0.17/0.15、+0.05/-0.10）**完全是经验拍脑袋**。没有任何文件说明：
- 这些权重在什么数据集上验证过
- 因子 IC（Information Coefficient）是多少
- 哪个因子是"主 alpha 源"，哪个是"风险调节器"

**修复方案**:
1. **第一步（不依赖回测）**：在 `IntradayScorer` 增加因子贡献日志：
```python
# score() 末尾, 每个 signal 都记录
log_factor_attribution({
    "symbol": symbol,
    "timestamp": time.time(),
    "factor_contributions": {name: f.contribution for name, f in factors.items()},
    "final_strength": strength,
    "regime": regime,
})
```
   写入 `data/factor_attribution.parquet`（使用 pyarrow append 模式）
2. **第二步（F29 完成后）**：基于 30+ 天历史数据计算每个因子的 IC：
```python
# scripts/compute_factor_ic.py
ic = pearson_corr(factor.contribution, future_return_5d)
# 输出报告：trend IC=0.08, momentum IC=0.12, ... lifecycle IC=0.02
```
3. **第三步（每月）**：基于 IC 自动调整权重，写入 `risk.signal_quality.factor_weights`（需扩展 ConfigWriter PARAM_MAP）

**验收标准**:
- `data/factor_attribution.parquet` 每天有新行入库
- 月度报告 `reports/factor_ic_YYYYMM.md` 自动生成
- 任何因子 IC 连续 3 个月 < 0.02 → 自动告警建议下线

---

## F9 — 信号质量缺乏「事中校验」

**优先级**: P2
**文件**: 新增 `app/core/signal_quality_monitor.py`

**问题**: 系统只在生成时计算 confidence，但没有"信号生成后回看 N 分钟，看价格是否如预期"的校验。导致 IntradayScorer 自报告的 confidence 永远是个不可证伪的数字。

**修复方案**:
仿 `exit_quality_tracker` 模式：
```python
class SignalQualityTracker:
    """每个生成的信号 5/15/60 分钟后回看, 评估实际价格走向是否与预测一致"""
    def record_signal(self, signal: IntradaySignal, current_price: float):
        ...
    def evaluate_pending(self, get_price_fn):
        # 5min/15min/60min 后回看, 写入 signal_quality.db
        ...
```
DB schema:
```sql
CREATE TABLE signal_quality (
    signal_id TEXT PRIMARY KEY,
    symbol TEXT, direction TEXT, strength INT, confidence REAL,
    factor_dominant TEXT,  -- 最大贡献因子
    timestamp REAL,
    price_at_signal REAL,
    price_5m REAL, price_15m REAL, price_60m REAL,
    is_correct_5m INT, is_correct_15m INT, is_correct_60m INT,
    actual_pnl_5m REAL, actual_pnl_15m REAL, actual_pnl_60m REAL
);
```
日报追加 `signal_quality_5m_accuracy / 15m_accuracy / 60m_accuracy` 字段。

**验收标准**:
- DB 每个交易日新增 ≥ 100 条记录
- 周报出现"按因子分解的信号准确率"图表

---

# 第三部分：止损止盈策略

## 3.1 现状链路

```
入场后, 每 60s 扫描:
  │
  ▼
ExitMonitor.scan_positions() → 6 条规则按顺序检查 (先到先得)
  │
  ├─ Rule 0: 防振荡 cooldown (120s)
  ├─ Rule 1: Flash Exit (60s 内 >1.0×ATR 不利波动)
  ├─ Rule 1.5: Day Trade Mode (短线主动管理)
  │     ├─ Initial Stop (1.8x ATR / max 5% loss)
  │     ├─ TP Tier 1 (+3% 平 50% → 移保本)
  │     ├─ TP Tier 2 (+6% 平剩余)
  │     ├─ Profit Lock (+2% 激活, +0.8% 锁定)  ⚠ 框死利润
  │     └─ Time Decay (150min 无进展)
  ├─ Rule 2.5: Scale Out (8%/15%/25% 分批)
  ├─ Rule 2: Chandelier Trail (peak - 2.5× ATR)
  ├─ Rule 3: Time Stop (72h 持仓)
  └─ Rule 3.5: Pre-Close Exit (距收盘 <15min)
        ├─ 时间加权 (5min/10min/15min)
        ├─ 4 层 (亏损/微利/中利/大利)
        └─ min_hold 30min 保护
```

## F10 — 风险单位 R 未定义（最关键的策略问题）⭐

**优先级**: P0
**文件**: `app/core/intraday_scorer.py:1019-1063`、`app/risk/position_limits.py`、`unified_config.yaml`

**现状**:
```python
# intraday_scorer.py:1034-1057 — 仓位和止损独立计算
position_pct = confidence * base_position(0.10) * vol_scaler * vix_damp  # 仓位
stop_loss_pct = max(vix_stop_multiplier(vix) * atr / price, 0.02)        # 止损
```

**问题**: 仓位和止损是两条独立计算线，每笔交易的实际风险（仓位 × 止损距离）不可控：
- Trade A: position=0.20, stop=0.05 → 风险 = 1.00% 净值
- Trade B: position=0.05, stop=0.10 → 风险 = 0.50% 净值
- Trade C: position=0.15, stop=0.03 → 风险 = 0.45% 净值

**没有任何一笔交易的"R"（每笔最大可接受亏损）是被定义的**。这是策略系统最致命的缺陷之一。

**修复方案**:
1. 在 `unified_config.yaml` 增加：
```yaml
risk:
  per_trade_risk_pct: 0.005    # 每笔交易最大 0.5% 净值
  max_trade_risk_pct: 0.010    # 极限 1.0% (用于高 confidence setup)
```
2. 重写 `IntradayScorer._compute_position_and_stop`:
```python
def _compute_position_and_stop(self, confidence, atr, price, vix, direction):
    # Step 1: 先确定止损距离 (与仓位无关)
    stop_mult = vix_stop_multiplier(vix)
    stop_distance_pct = max(stop_mult * atr / price, 0.02) if atr > 0 else 0.02
    if vix > 35:
        stop_distance_pct *= 1.3

    # Step 2: 确定本笔风险预算 (按 confidence 缩放, 但有上下限)
    cfg = get_config()
    base_risk = cfg.get_dotted("risk.per_trade_risk_pct", 0.005)
    max_risk = cfg.get_dotted("risk.max_trade_risk_pct", 0.010)
    risk_scaler = 0.5 + min(confidence, 0.85) * 1.0  # confidence=0.5 → 1.0R, 0.85 → 1.35R
    trade_risk = min(base_risk * risk_scaler, max_risk)

    # Step 3: 仓位 = 风险预算 / 止损距离
    position_pct = trade_risk / stop_distance_pct

    # Step 4: VIX 阻尼 (高波动期主动降仓)
    vix_damp = 1.0 if vix < 22 else (0.8 if vix < 30 else 0.5)
    position_pct *= vix_damp

    # Step 5: 硬上下限
    position_pct = max(0.02, min(position_pct, 0.20))

    return round(position_pct, 3), round(stop_distance_pct, 3)
```
3. 必须同步在 `RiskGateway` 增加 `_check_trade_risk_within_budget()` 检查项：开仓前验证 `position * stop_distance` 不超过 `max_trade_risk_pct`

**验收标准**:
- 单测：confidence=0.5, atr=2, price=100, vix=20 → 止损 4%, 风险 0.5%, 仓位 = 12.5%
- 单测：confidence=0.85, atr=1, price=100, vix=18 → 止损 2%, 风险 0.85%（risk_scaler=1.35×0.5%≈0.68%, 但要 cap 到 0.85% 因为 max_risk=1%）, 仓位 ≈ 42.5% → 被 0.20 上限截断
- 集成测试：连续 100 笔交易，每笔实际亏损（被止损时）的 95% 分位数应 ≤ `max_trade_risk_pct × 1.2`（含滑点）

---

## F11 — Day Trade Mode TP 阈值结构性负 EV ⭐

**优先级**: P0
**文件**: `app/risk/exit_policy.yaml:226-253`

**现状**:
```yaml
day_trade_mode:
  initial_stop:
    max_loss_pct: 5.0
  take_profit:
    tier_1: { profit_pct: 3.0, close_pct: 50, move_sl_to: breakeven }
    tier_2: { profit_pct: 6.0, close_pct: 50 }
  profit_lock:
    activation_pct: 2.0
    lock_level_pct: 0.8
```

**问题（数学题）**:
- 止损 5% / 止盈 3%（先平 50%）→ R/R = 0.6
- 加成本 ~0.15%，期望值 = `胜率 × 1.5% - (1-胜率) × 5%`
- **盈亏平衡胜率 = 5/(5+1.5) = 77%** — 实战中达不到
- 即使胜率 60% 也亏：`0.6 × 1.5% - 0.4 × 5% = -1.1%`/笔

**更严重**: `profit_lock` 在 +2% 激活、+0.8% 锁定 → 任何超过 +2% 的盈利都被框死在 +0.8% ~ +3% 区间。**系统结构性赚不到大行情**。从 `exit_policy.yaml` 注释 "原 1ATR 阈值导致 54% 仓位被动强平" 可见已经被反复观察到。

**修复方案**:
```yaml
day_trade_mode:
  initial_stop:
    atr_multiplier: 1.5    # 略收紧 (从 1.8 → 1.5)
    max_loss_pct: 3.0      # 收紧到 3% (从 5%)
  take_profit:
    tier_1:
      profit_pct: 4.0      # 提高到 +4% (从 3%)
      close_pct: 33        # 只平 1/3 (从 50%)
      move_sl_to: breakeven
    tier_2:
      profit_pct: 8.0      # 提高到 +8% (从 6%)
      close_pct: 33        # 平 1/3
      move_sl_to: chandelier_2x  # 切换到 trail
    tier_3:                # 新增
      profit_pct: 15.0
      close_pct: 100        # 剩余清仓 (或继续 chandelier)
  profit_lock:
    enabled: false         # 默认禁用 — 由 chandelier 接管
    # 或调整: activation_pct: 5.0, lock_level_pct: 2.5  # 给利润空间
```
新 R/R = `8/3 = 2.67`，盈亏平衡胜率 = 27% — 健康水平。

**验收标准**:
- 单测：买入 $100, 涨到 $108 → 触发 tier_2, 平 1/3, SL 升级到 chandelier
- 回测（F29 完成后）: 对比修改前后过去 60 天 day_trade_mode 仓位的总 PnL，新方案应有正期望
- 实盘验证（30 天）：触发 `DAYTRADE_TP1` 的仓位中，剩余 2/3 后续 PnL 中位数应 > 0

---

## F12 — `profit_lock` 与 `chandelier` 重复且互相冲突

**优先级**: P1
**文件**: `app/risk/exit_policy.yaml:62-80, 236-240`

**现状**: profit_lock（Day Trade Mode）和 chandelier_trail（Trend Mode）都是"浮盈保护"机制，但触发逻辑不同：
- profit_lock: 浮盈到 X% 激活, 回撤到 Y% 触发
- chandelier: peak - mult × ATR 动态跟踪

实战中两者会"竞速"，先到先触发，导致退出原因混乱。

**修复方案**: 二选一，建议保留 chandelier（学术验证更广），删除 profit_lock：
```yaml
day_trade_mode:
  profit_lock:
    enabled: false  # DEPRECATED 2026-05-12, 由 chandelier_trail 接管
```
对应代码 `position_exit_monitor.py:620-636` 块整体删除（保留 enabled=false 时跳过即可）。

**验收标准**:
- 修改后 `DAYTRADE_PROFIT_LOCK` 退出原因不再出现
- 单测：原本会触发 profit_lock 的场景（+2% 激活 → 回撤到 +0.8%）现在被 chandelier 处理（peak +2% - 2.5×ATR）

---

## F13 — 6 条退出规则无质量评分系统 ⭐

**优先级**: P1
**文件**: `app/storage/exit_quality_tracker.py`、新增 `app/review/exit_quality_report.py`

**现状**: 6 条规则按顺序检查，先触发先平仓，但没有「哪条规则触发的退出最赚」的统计。

`exit_quality.db` 已存在，但只记录"15min 后回看是 GOOD/BAD/NEUTRAL"，**没有按 exit_reason 维度的 PnL 分布报告**。

**修复方案**:
新增脚本 `scripts/exit_quality_report.py`：
```python
"""
按 exit_reason × session × direction 输出 PnL 分布报告

用法: python scripts/exit_quality_report.py --days=30 --output=reports/exit_quality_30d.md

输出表格示例:
| exit_reason          | n   | win_rate | avg_pnl | p25/p50/p75  | sharpe |
|----------------------|-----|----------|---------|--------------|--------|
| FLASH_EXIT           | 8   | 12.5%    | -2.1%   | -3.5/-2.0/-0.8 | -0.85 |  # 强烈建议禁用
| DAYTRADE_TP1         | 45  | 100%     | +2.8%   | +2.0/+2.5/+3.5 | 2.1   |
| DAYTRADE_PROFIT_LOCK | 23  | 100%     | +1.1%   | +0.8/+1.0/+1.5 | 0.9   |
| CHANDELIER           | 17  | 76%      | +4.5%   | +2.0/+4.0/+8.0 | 1.4   |
| PRE_CLOSE_EXIT       | 32  | 50%      | +0.1%   | -1.5/+0.2/+1.8 | 0.05  |  # 应该减少触发
```
查询 SQL（`ftseek_us.db.trade_records` JOIN `exit_quality.db.exit_quality`）:
```sql
SELECT exit_reason,
       COUNT(*) as n,
       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as win_rate,
       AVG(realized_pnl / quantity / entry_price) as avg_pnl_pct,
       ...
FROM trade_records
WHERE exit_time > date('now', '-30 days')
GROUP BY exit_reason
```

**配套行动**: 月度复盘后，连续 2 个月 `avg_pnl < 0` 的 exit_reason 自动加入 disable list（写入 `exit_policy.yaml` 的对应规则 `enabled: false`）。

**验收标准**:
- `python scripts/exit_quality_report.py --days=30` 输出 markdown 表格
- 集成到 `weekly_review`，自动嵌入报告
- 任何 exit_reason 30 天内胜率 < 30% 触发飞书告警

---

## F14 — `PRE_CLOSE_EXIT` 复杂度过高（4 层 + 时间加权 + min_hold）

**优先级**: P1
**文件**: `app/risk/exit_policy.yaml:84-105`、`app/risk/position_exit_monitor.py:821-`

**现状**: 当前 `pre_close_exit` 设计:
- 4 层（亏损/微利/中利/大利）
- 时间加权乘数 0.6/0.8/1.0
- min_hold_minutes_before_pre_close = 30
- close_ratio 字段控制减仓比例

来自 `exit_policy.yaml` 注释："原单一 1 ATR 阈值导致 54% 仓位被动强平, 平均利润仅 +0.08%" — 说明系统**入场后没有合适的退出，所以盘末才被迫清仓**。

**问题**: 修复方向不应该是"让 PRE_CLOSE 更聪明"，而应该是"让中途的 trailing/scale_out 更早接管"。复杂的 PRE_CLOSE 是症状，不是病因。

**修复方案**:
1. **简化 PRE_CLOSE 为 2 档**:
```yaml
pre_close_exit:
  enabled: true
  window_minutes: 15
  min_hold_minutes: 60                # 持仓 <60min 不触发盘末退出
  hold_overnight_threshold_atr: 1.0   # 盈利 >1 ATR 持有过夜
  loss_force_close: true              # 亏损强平 (避免隔夜跳空)
  # 删除 4 层、时间加权、close_ratio 等复杂度
```
2. **真正的修复在 chandelier**：把 `chandelier_trail.activation_profit_atr` 从 1.2 进一步降至 0.8，让 trailing 在小利润就接管：
```yaml
chandelier_trail:
  activation_profit_atr: 0.8   # 优化 (2026-05-12): 从 1.2 → 0.8
```
3. 监测指标：每月统计「被 PRE_CLOSE 触发但本可被 chandelier 触发的仓位占比」，目标 < 20%

**验收标准**:
- `position_exit_monitor.py:821-` 的 `_check_pre_close_exit` 方法行数从当前约 200 行降至 < 60 行
- 单测：持仓 +1.5×ATR, 距收盘 5min → 持有过夜
- 单测：持仓 -2%, 距收盘 5min → 强平
- 月度报告显示 PRE_CLOSE_EXIT 占总退出比例从当前 ~50% 降至 < 25%

---

## F15 — `trail_state` latent NameError bug

**优先级**: P0
**文件**: `app/risk/position_exit_monitor.py:237-258`

**现状**:
```python
# 第 237-243 行
if not isinstance(current_price, (int, float)):
    _prev_price = trail_state.get("prev_scan_price", entry_price)
    # ... 但 trail_state 在第 268 行才被赋值！
    ...
```

**问题**: 如果第一次扫描就遇到 type 异常，`trail_state` 未定义 → `NameError`，整个 scan_positions 循环挂掉。

**修复方案**: 把 trail_state 初始化提前：
```python
for position in positions:
    try:
        symbol = position.symbol
        direction = position.direction.value if hasattr(position.direction, 'value') else str(position.direction)
        entry_price = position.avg_entry_price
        if entry_price <= 0:
            continue

        # ↓ 提前初始化 trail_state, 避免 NameError
        trail_state = position.trail_state if position.trail_state else {}

        # 获取当前价格
        current_price = get_price_fn(symbol)
        if not isinstance(current_price, (int, float)):
            _prev_price = trail_state.get("prev_scan_price", entry_price)  # 现在安全
            ...
```

**验收标准**:
- 单测：mock get_price_fn 第一次返回 None，断言不抛 NameError
- 单测：mock get_price_fn 第一次返回 "ERR" 字符串，断言走 fallback 路径

---

## F16 — 入场→退出的 thesis 对称性缺失 ⭐

**优先级**: P2
**文件**: `app/core/models.py` (SignalScore)、`app/risk/position_exit_monitor.py`

**现状**: 入场时记录了因子 detail（`_factor_detail`），但**退出时不参考入场理由**。导致：
- 动量入场 → 用 chandelier 退出（合理）
- 突破入场 → 用 chandelier 退出（次优，应该用破位退出）
- 事件驱动入场 → 用 time_stop 退出（错误，应该用事件 horizon）
- 反转入场 → 用 chandelier 退出（错误，应该用紧 ATR）

**修复方案**:
1. 在 `SignalScore` 增加 `entry_thesis` 字段:
```python
@dataclass
class SignalScore:
    # ... existing fields ...
    entry_thesis: str = "momentum"  # momentum/breakout/event/reversion
    entry_thesis_data: Dict = field(default_factory=dict)  # {breakout_level, event_horizon, etc.}
```
2. IntradayScorer.score() 末尾添加 thesis 推断:
```python
def _infer_thesis(self, factors, ctx) -> str:
    """从主导因子推断入场理由"""
    dominant = max(factors.values(), key=lambda f: abs(f.contribution))
    if dominant.name == "lifecycle" and "accelerating" in dominant.reasoning:
        return "momentum"
    if ctx.get("price", 0) > ctx.get("high_20d", float('inf')) * 0.99:
        return "breakout"
    if ctx.get("is_earnings_period") or ctx.get("news_sentiment", 0) > 0.7:
        return "event"
    if dominant.name == "momentum" and "RSI" in dominant.reasoning and abs(dominant.raw) < 30:
        return "reversion"
    return "momentum"  # 默认
```
3. 在 `Position` 持久化 entry_thesis（写入 `trade_records.metadata_json`）
4. ExitMonitor 按 thesis 选择退出策略：
```python
def _get_exit_rules_for_thesis(self, thesis: str) -> List[str]:
    rules = {
        "momentum":  ["lifecycle_fade", "chandelier", "time_stop", "pre_close"],
        "breakout":  ["technical_breach", "atr_stop", "pre_close"],  # 破位即止
        "event":     ["event_horizon_72h", "chandelier", "pre_close"],  # 事件 horizon
        "reversion": ["tight_atr_2x", "scale_out_3pct", "pre_close"],   # 短线紧止损
    }
    return rules.get(thesis, ["chandelier", "time_stop", "pre_close"])
```

**验收标准**:
- 数据：每笔 trade_records 都有非空 `entry_thesis`
- 单测：`thesis="breakout"` 入场 → 退出规则不含 chandelier
- 月度报告：按 thesis 分组的 PnL 表

---

## F17 — 组合层面止损缺失（只有单笔止损）

**优先级**: P1
**文件**: `app/risk/position_exit_monitor.py`、`app/risk/drawdown_control.py`

**现状**:
- `MAX_DAILY_DRAWDOWN: 0.03` 只阻止新开仓（pre_trade_check），不主动平仓
- `PortfolioConstructor` 在排序后无组合层面的"全平触发"
- 单只股票 max_loss 5%，但 8 只股票同时各亏 4% → 组合亏 4% 但每只都没触发个体止损

**修复方案**:
在 `ExitMonitor.scan_positions()` 开头增加 Rule 0.5（组合层面）:
```python
def _check_portfolio_drawdown(self, positions, equity) -> Optional[str]:
    """组合层面止损 - 优先级最高, 触发即全平"""
    cfg = get_config()
    portfolio_warn_pct = cfg.get_dotted("risk.portfolio.warn_drawdown_pct", 0.02)  # -2% 警告
    portfolio_stop_pct = cfg.get_dotted("risk.portfolio.stop_drawdown_pct", 0.03)  # -3% 全平
    portfolio_reduce_pct = cfg.get_dotted("risk.portfolio.reduce_drawdown_pct", 0.025)  # -2.5% 减半

    total_pnl = sum(p.unrealized_pnl for p in positions)
    drawdown = -total_pnl / equity if equity > 0 else 0

    if drawdown >= portfolio_stop_pct:
        return "FLAT_ALL"
    if drawdown >= portfolio_reduce_pct:
        return "REDUCE_50"
    if drawdown >= portfolio_warn_pct:
        return "WARN"
    return None

def scan_positions(self, positions, get_price_fn, execute_close_fn, ...):
    # ↓ 在循环前先检查组合层面
    portfolio_action = self._check_portfolio_drawdown(positions, equity)
    if portfolio_action == "FLAT_ALL":
        for p in positions:
            execute_close_fn(p, {"reason": "PORTFOLIO_DRAWDOWN_HARD_STOP", ...})
        return [...]
    elif portfolio_action == "REDUCE_50":
        for p in positions:
            close_qty = max(1, p.size // 2)
            execute_close_fn(p, {"reason": "PORTFOLIO_DRAWDOWN_REDUCE", "close_qty": close_qty})
    elif portfolio_action == "WARN":
        # 推送告警 + 通知 RiskStateMachine
        RiskStateMachine().set_state(RiskState.CAUTION, "portfolio_drawdown_warning", "ExitMonitor")

    # 原有单仓位扫描循环
    for position in positions:
        ...
```
对应配置：
```yaml
risk:
  portfolio:
    warn_drawdown_pct: 0.02
    reduce_drawdown_pct: 0.025
    stop_drawdown_pct: 0.03
```

**验收标准**:
- 单测：构造 8 只持仓共亏 3.5% → 触发 FLAT_ALL
- 单测：组合亏 2.6% → 触发 REDUCE_50
- 集成：触发 FLAT_ALL 后，断言 RiskStateMachine 状态变为 RISK_OFF

---

# 第四部分：盘后复盘与归因

## 4.1 现状链路

```
ET 16:00+
  │
  ▼
DailyReviewGenerator.generate(date)
  │ ┌─ 读 ftseek_us.db (trade_records) — 路径硬编码 /opt/ftseek/ ⚠
  │ ├─ 读 decisions.db (decisions, model_votes, trade_outcomes) — 同上
  │ ├─ 读 rejections.db
  │ ├─ 6 维度分析 (signal_quality / voting / execution / risk / funnel / attribution)
  │ └─ 输出 daily_reviews/daily_review_{date}.json
  │
  ▼
review_orchestrator.run_daily(review)
  │
  ▼
LessonInjector.validate_lessons(today_trades) — 调整置信度
  │
  ▼
agent_bridge.generate_eod_review() — 写到 ~/.hermes/ftseek/requests/
  │
  ▼
review_loop.IssueDetector → ActionExecutor → ConfigWriter (4 个白名单参数)
```

## F18 — 数据库路径硬编码 `/opt/ftseek/...` ⭐

**优先级**: P0
**文件**: `app/review/daily_review.py:40-45`、`app/review/weekly_review.py:34`、`app/storage/decision_recorder.py:32`、`app/llm/production_monitor.py:32-34`

**现状**:
```python
# daily_review.py:40-45
self.db_path = Path('/opt/ftseek/FTSeek-US/data/ftseek_us.db')
self.decisions_db = Path('/opt/ftseek/FTSeek-US/data/decisions.db')
self.rejections_db = Path('/opt/ftseek/FTSeek-US/data/rejections.db')
self.output_dir = Path('/opt/ftseek/FTSeek-US/data/daily_reviews')

# decision_recorder.py:32
DB_PATH = "/opt/ftseek/FTSeek-US/data/decisions.db"
```

**问题**: 在本机开发环境 `/Users/xuyuan.xy/Desktop/FTSeek-us-20260512` 下，所有读写都指向不存在的 `/opt/ftseek/FTSeek-US/data/`。**daily_review 在本机永远拿到空数据集**。

**修复方案**:
1. 在 `unified_config.yaml` 增加路径配置：
```yaml
paths:
  data_dir: "data"           # 相对项目根
  reviews_dir: "data/daily_reviews"
  decisions_db: "data/decisions.db"
  rejections_db: "data/rejections.db"
  trade_records_db: "data/ftseek_us.db"
  exit_quality_db: "data/exit_quality.db"
```
2. 创建 `app/core/paths.py`（路径解析的唯一入口）：
```python
def get_data_dir() -> Path:
    cfg = get_config()
    relative = cfg.get_dotted("paths.data_dir", "data")
    if os.path.isabs(relative):
        return Path(relative)
    project_root = Path(__file__).parent.parent.parent
    return project_root / relative

def get_decisions_db() -> Path: ...
def get_trade_records_db() -> Path: ...
def get_exit_quality_db() -> Path: ...
def get_battle_plan_path() -> Path: ...
```
3. 替换全工程所有硬编码路径（约 8 处）为 `app.core.paths.*` 的调用

**验收标准**:
- `grep "/opt/ftseek" app/` 应无任何匹配
- 在 `/tmp/test-ftseek/` 临时目录下启动，验证 DB 创建在该目录
- CI 跑 `pytest tests/`，daily_review 相关测试在本机能通过

---

## F19 — `trade_attribution` 表无 CREATE TABLE 语句 ⭐

**优先级**: P0
**文件**: `app/pipeline/order_executor.py:1767-1776`

**现状**:
```python
def _insert_trade_attribution(decision_id, symbol, ...):
    # 直接 INSERT, 但全工程无 CREATE TABLE trade_attribution
    sql = "INSERT INTO trade_attribution (...) VALUES (...)"
```
全工程 grep `CREATE TABLE.*trade_attribution` 无结果。

**问题**: 新部署或本机首次运行时，第一次 `_insert_trade_attribution` 会抛 `OperationalError: no such table: trade_attribution`。归因数据完全无法写入，daily_review 的 attribution 维度永远为空。

**修复方案**:
1. 在 `app/storage/trade_store.py` 的 `__init__` 中增加 schema 创建：
```python
def _ensure_trade_attribution_schema(self):
    self.conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_attribution (
            attribution_id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL,
            trace_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            entry_price REAL,
            exit_price REAL,
            quantity INTEGER,
            realized_pnl REAL,
            -- 入场归因 (5 因子)
            entry_thesis TEXT,
            factor_trend REAL,
            factor_momentum REAL,
            factor_volume REAL,
            factor_orderflow REAL,
            factor_lifecycle REAL,
            -- 退出归因
            exit_reason TEXT,
            exit_quality TEXT,
            holding_minutes INTEGER,
            session TEXT,
            vix_at_entry REAL,
            vix_at_exit REAL,
            -- metadata
            metadata_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    self.conn.execute("CREATE INDEX IF NOT EXISTS idx_attr_symbol ON trade_attribution(symbol)")
    self.conn.execute("CREATE INDEX IF NOT EXISTS idx_attr_decision ON trade_attribution(decision_id)")
    self.conn.execute("CREATE INDEX IF NOT EXISTS idx_attr_entry_time ON trade_attribution(entry_time)")
    self.conn.commit()
```
2. 在 `TradeStore.__init__` 末尾调 `self._ensure_trade_attribution_schema()`

**验收标准**:
- 删除 ftseek_us.db, 启动一次, 断言 `SELECT * FROM trade_attribution LIMIT 1` 不抛错
- 模拟一笔 trade 全流程, 断言 trade_attribution 有 1 行新数据

---

## F20 — `link_trade_outcome` 缺 decision_id 时静默跳过

**优先级**: P0
**文件**: `app/pipeline/order_executor.py:2095-2132`、`app/storage/decision_recorder.py:259-349`

**现状**:
```python
# order_executor.py:2127
if not decision_id:
    logger.warning(f"无法找到 decision_id, 跳过 link_trade_outcome")
    return
```

**问题**: 如果 decision_id 找不到（trace_id 也丢失），平仓数据永远不写入 `trade_outcomes` → `model_performance` 永远不更新 → 模型权重调整失效。这是**进化层最常见的断裂**。

**修复方案**:
1. 改用 fallback 写入策略（即使 decision_id 缺失也要保存数据）:
```python
# order_executor.py:2127
if not decision_id:
    # P0 fix: 不能丢数据, 写到 orphan_outcomes 表用于后续手动归因
    logger.warning(f"[link_trade_outcome] {symbol} decision_id 缺失, 写入 orphan_outcomes")
    decision_recorder.save_orphan_outcome({
        "symbol": symbol,
        "direction": direction,
        "exit_price": exit_price,
        "realized_pnl": realized_pnl,
        "exit_reason": exit_reason,
        "exit_time": exit_time,
        "trace_id": getattr(position, "trace_id", None),
        "size": position.size,
    })
    return
```
2. 在 `decision_recorder.py` 增加：
```python
def save_orphan_outcome(self, data: Dict):
    self.conn.execute("""
        CREATE TABLE IF NOT EXISTS orphan_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, direction TEXT, exit_price REAL, realized_pnl REAL,
            exit_reason TEXT, exit_time TEXT, trace_id TEXT, size INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    self.conn.execute("INSERT INTO orphan_outcomes (...) VALUES (...)", ...)
```
3. daily_review 增加监控指标 `orphan_outcomes_count`，> 5% 总交易数 → 告警
4. 长期：分析 orphan_outcomes 找规律，反向修复 decision_id 丢失的根因

**验收标准**:
- 单测：mock 一笔 trade with `decision_id=None` → 写入 orphan_outcomes
- 集成：daily_review 报告含 `orphan_outcomes_count` 字段
- 实盘 30 天：`orphan_outcomes_count / total_trades < 5%`

---

## F21 — `suggestion_engine` 读不存在的列

**优先级**: P1
**文件**: `app/review/suggestion_engine.py:116`

**现状**:
```python
# suggestion_engine.py:116
sl_count = sum(1 for t in trades if t.get('stop_loss_planned'))  # 列不存在
```
`trade_records` schema (`trade_store.py:62-91`) 无 `stop_loss_planned` 列。

**问题**: 规则 `stop_loss_execution` 永远返回默认值，无效。

**修复方案**:
1. 在 `trade_store.py` 增加 `stop_loss_planned` 和 `stop_loss_executed` 列:
```python
self.conn.execute("ALTER TABLE trade_records ADD COLUMN stop_loss_planned REAL DEFAULT 0")
self.conn.execute("ALTER TABLE trade_records ADD COLUMN stop_loss_executed REAL DEFAULT 0")
```
2. `order_executor.save_trade()` 时填充：
- `stop_loss_planned` = 入场时计算的 stop_loss_pct × entry_price
- `stop_loss_executed` = 平仓时实际触发的 stop_price (如果 exit_reason in [DAYTRADE_STOP, ATR_STOP, ...])
3. `suggestion_engine.py:116` 的查询改为：
```python
sl_planned = sum(1 for t in trades if t.get('stop_loss_planned', 0) > 0)
sl_executed = sum(1 for t in trades if t.get('stop_loss_executed', 0) > 0)
sl_execution_rate = sl_executed / sl_planned if sl_planned > 0 else 1.0
```

**验收标准**:
- 单测：构造 10 笔 trades，5 笔触及止损，断言 sl_execution_rate = 0.5
- daily_review 报告 `stop_loss_execution_rate` 字段非默认值

---

## F22 — 无 `exit_reason × pnl` 分布报告

**优先级**: P1
**文件**: 新增 `scripts/exit_reason_pnl_report.py`、`app/review/daily_review.py`

见 F13 修复方案 — 二者紧密相关。本项关注**报告生成**。

**新增脚本** `scripts/exit_reason_pnl_report.py`:
```python
"""
按 exit_reason 维度生成 PnL 分布报告

用法:
  python scripts/exit_reason_pnl_report.py --days=30
  python scripts/exit_reason_pnl_report.py --days=7 --output=daily

输出: reports/exit_reason_pnl_{period}_{date}.md
"""
import sqlite3
from app.core.paths import get_trade_records_db

def generate_report(days: int):
    conn = sqlite3.connect(get_trade_records_db())
    rows = conn.execute(f"""
        SELECT
            exit_reason,
            COUNT(*) as n,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as win_rate,
            AVG(realized_pnl) as avg_pnl,
            AVG(realized_pnl / NULLIF(quantity * entry_price, 0)) as avg_pnl_pct,
            MIN(realized_pnl) as min_pnl,
            MAX(realized_pnl) as max_pnl,
            -- p25/p50/p75 用 ntile + window function
            ...
        FROM trade_records
        WHERE exit_time > date('now', '-{days} days')
            AND exit_reason IS NOT NULL
        GROUP BY exit_reason
        ORDER BY avg_pnl_pct DESC
    """).fetchall()

    # 输出 markdown 表
    output = "# Exit Reason PnL Distribution Report\n\n"
    output += "| exit_reason | n | win_rate | avg_pnl_pct | sharpe_proxy | recommendation |\n"
    output += "|---|---|---|---|---|---|\n"
    for r in rows:
        recommendation = "✅ keep" if r["avg_pnl_pct"] > 0.005 else "⚠ review" if r["avg_pnl_pct"] > 0 else "🚨 disable"
        output += f"| {r['exit_reason']} | {r['n']} | {r['win_rate']:.1%} | ..."
    return output
```

集成到 `daily_review`：在 `_save_report()` 之前调用，把表嵌入 review JSON。
集成到 `weekly_review`：每周输出独立 markdown 文件 `reports/exit_reason_pnl_weekly_YYYYMMDD.md`。

**验收标准**:
- 手工运行脚本能产出 markdown 表格
- 周报中出现该表
- 任何 reason 的 sharpe_proxy 连续 4 周 < 0 → 自动飞书告警

---

## F23 — `lessons` 表 DB 路径不一致（双源）

**优先级**: P1
**文件**: `app/review/lesson_injector.py:36`、`app/review/daily_review.py:41`

**现状**:
- `lesson_injector.py:36` 用 `Path(__file__).parent.parent.parent / 'data' / 'ftseek_us.db'` (项目根 data/)
- `daily_review.py:41` 用 `/opt/ftseek/FTSeek-US/data/ftseek_us.db`

**问题**: lessons 表会被写到不同 DB，盘前读取和盘后写入分离 → 进化链断裂。

**修复方案**: 见 F18，统一用 `app.core.paths.get_trade_records_db()`。

**验收标准**: F18 完成后自动通过。

---

# 第五部分：自动化进化层

## 5.1 现状（大部分死代码）

```
入口                              状态
───────────────────────────────────────────
app/deep_review/auto_tuner.py       占位 (return deprecated)
app/deep_review/improvement_tracker  内存模式, should_tune() 永远 False
app/deep_review/engine.py            未实例化, 所有方法返回空
app/llm/disagreement_registry.py    硬编码 /opt/ftseek/, 默认关闭
app/llm/empirical_verifier.py       硬编码 /opt/ftseek/, 默认关闭
app/llm/production_monitor.py       硬编码 /opt/ftseek/, 默认关闭
app/llm/enhanced_debate.py          FTSEEK_DEBATE_MODE=false 默认

唯一真正生效的进化路径:
review_loop.IssueDetector → ActionExecutor → ConfigWriter (4 参数白名单)
```

## F24 — `app/deep_review/` 整个模块死代码

**优先级**: P2
**文件**: `app/deep_review/{auto_tuner,improvement_tracker,engine}.py`

**现状**:
```python
# auto_tuner.py
class AutoTuner:
    def optimize(self, *args, **kwargs):
        return {"status": "deprecated", "message": "Use review_loop instead"}

# engine.py
class DeepReviewEngine:
    def _collect_trades(self, ...): pass  # P1 2026-04-19: 未实现
    def _analyze_dimensions(self, ...): pass  # P1: 未实现
    # ... 所有方法都是 pass
```

**问题**: 死代码占用 import 路径，迷惑人。`grep "DeepReviewEngine(" app/` 无匹配 — 从未实例化。

**修复方案**:
**两选一**:

A. **删除（推荐）**: 整个 `app/deep_review/` 目录移到 `.trash/`
B. **重写**: 把 `DeepReviewEngine` 改造成真正的"超复盘引擎"，跑深度归因（按 thesis 分类、按 session 分组、按 regime 分组）

如果选 A，需要：
1. 移除所有 import: `grep "from app.deep_review" app/` 找到引用点（应该没有，因为从未实例化）
2. `mv app/deep_review .trash/deep_review_2026-05-12`

**验收标准**:
- `grep "from app.deep_review" app/` 无匹配
- `pytest tests/` 通过

---

## F25 — `ImprovementTracker` 未持久化

**优先级**: P2
**文件**: `app/deep_review/improvement_tracker.py`

**现状**: 全部内存模式，重启即丢失。`should_tune()` 永远 return False，从未被实例化。

**修复方案**:
1. 把 `improvements` dict 改为 SQLite 持久化（新表 `improvements`）
2. `should_tune` 改为基于真实数据的判断：
```python
def should_tune(self, param_name: str) -> bool:
    """如果某参数最近 10 笔修改后效果都是负向 → 暂停调整"""
    recent = self.get_recent_changes(param_name, n=10)
    negative_count = sum(1 for r in recent if r["effect"] < 0)
    return negative_count < 5  # 超过半数负向 → 暂停
```
3. 接入 `review_loop.ActionExecutor`：每次 ConfigWriter.update_param 之前 query `should_tune`

或者**直接删除**（更简单），因为 ConfigWriter 已经有 `max_delta` safety bound。

**验收标准**: 选择"删除" → `app/deep_review/` 整个目录消失（与 F24 合并）

---

## F26 — `AutoTuner` 占位实现

**优先级**: P2
**文件**: `app/deep_review/auto_tuner.py`、`app/strategy/auto_tuner.py`

同 F24/F25 — 删除或重写。建议**直接删除**，让 `review_loop` 成为唯一的进化入口。

---

## F27 — `review_loop` 仅 4 参数白名单（覆盖太窄）

**优先级**: P1
**文件**: `app/core/config_writer.py:60-85`

**现状**:
```python
PARAM_MAP = {
    "MIN_CONFIDENCE": {"yaml_path": "risk.signal_quality.min_confidence", "min":0.3, "max":0.8, "max_delta":0.1},
    "SIGNAL_MIN_SCORE": {"yaml_path": "risk.signal_quality.min_signal_strength", ...},
    "MAX_POSITION_PER_SYMBOL": {...},
    "cooldown_hours": {...},
}
```

**问题**: 只能调 4 个参数。real-world 复盘提出的优化建议（如调整 ATR multiplier、profit_lock activation、TP tier 阈值）无法被自动应用。

**修复方案**:
扩展 PARAM_MAP（每个新增项必须有 min/max/max_delta，避免恶意调整）:
```python
PARAM_MAP = {
    # 现有 4 个 ...
    # 新增:
    "PER_TRADE_RISK_PCT": {
        "yaml_path": "risk.per_trade_risk_pct",
        "min": 0.002, "max": 0.015, "max_delta": 0.002,
    },
    "DAYTRADE_TP1_PCT": {
        "yaml_file": "app/risk/exit_policy.yaml",  # 不同 YAML
        "yaml_path": "day_trade_mode.take_profit.tier_1.profit_pct",
        "min": 2.0, "max": 8.0, "max_delta": 1.0,
    },
    "DAYTRADE_TP2_PCT": {
        "yaml_file": "app/risk/exit_policy.yaml",
        "yaml_path": "day_trade_mode.take_profit.tier_2.profit_pct",
        "min": 5.0, "max": 15.0, "max_delta": 2.0,
    },
    "CHANDELIER_ACTIVATION_ATR": {
        "yaml_file": "app/risk/exit_policy.yaml",
        "yaml_path": "exit_rules.chandelier_trail.activation_profit_atr",
        "min": 0.5, "max": 2.5, "max_delta": 0.3,
    },
    "PORTFOLIO_STOP_DRAWDOWN_PCT": {
        "yaml_path": "risk.portfolio.stop_drawdown_pct",
        "min": 0.02, "max": 0.05, "max_delta": 0.005,
    },
}
```
注意 `yaml_file` 字段支持多 YAML 写入（默认 `unified_config.yaml`，可指定 `exit_policy.yaml`）。

**配套**: `IssueDetector` 增加新规则：
- `low_chandelier_trigger_rate` → 触发 `CHANDELIER_ACTIVATION_ATR` 调整
- `pre_close_dominance` → 触发 `CHANDELIER_ACTIVATION_ATR` 降低（让 chandelier 更早接管）

**验收标准**:
- 单测：每个新增参数能被 ConfigWriter.update_param 写入对应 YAML
- IssueDetector 新规则单测覆盖

---

## F28 — `enhanced_debate` 子系统硬编码 `/opt/ftseek/`

**优先级**: P0
**文件**: `app/llm/disagreement_registry.py:34`、`app/llm/empirical_verifier.py:31-32`、`app/llm/production_monitor.py:32-34`

**现状**: 三个文件路径都硬编码 `/opt/ftseek/...`，本机运行必报错。

**修复方案**:
全部改用 `app.core.paths.*`（同 F18）:
```python
# disagreement_registry.py:34
LOG_PATH = get_data_dir() / "debate" / "disagreements.jsonl"

# empirical_verifier.py:31-32
PROJECT_PATH = get_project_root()
TEST_OUTPUT_DIR = get_data_dir() / "verification_results"

# production_monitor.py:32-34
PLAN_PATH = get_battle_plan_path()
LOG_PATH = get_logs_dir() / "ftseek_us.log"
```

**验收标准**: F18 完成后，这三个文件不再有 `/opt/ftseek/` 字面量。

---

# 第六部分：回测与 walk-forward 框架（必建）

## F29 — 建立最小回测框架 ⭐

**优先级**: P1（最高优先级 P1，因 F8/F11 都依赖此）
**文件**: 新建 `app/backtest/`

**现状**: `app/backtest/` 是空目录。所有阈值优化都是"实盘试错"，没有任何 OOS（out-of-sample）验证。

**修复方案**:

### Step 1: 数据接口
```python
# app/backtest/data_loader.py
class HistoricalDataLoader:
    """从 Yahoo / Polygon 等加载历史 1min/5min/daily 数据"""
    def load_bars(self, symbol: str, start: str, end: str, interval: str) -> pd.DataFrame: ...
    def load_vix(self, start: str, end: str) -> pd.Series: ...
    def load_spy(self, start: str, end: str) -> pd.DataFrame: ...
```

### Step 2: 回测引擎
```python
# app/backtest/backtest_engine.py
class BacktestEngine:
    """事件驱动回测引擎 - 模拟实盘 scheduler 调度逻辑"""
    def __init__(self, scorer, exit_monitor, risk_gateway, initial_capital=100000):
        self.scorer = scorer  # IntradayScorer
        self.exit_monitor = exit_monitor  # ExitMonitor
        self.risk_gateway = risk_gateway

    def run(self, symbols: List[str], start: str, end: str) -> BacktestResult:
        for date in date_range(start, end):
            for time_slot in ["09:30", "10:00", "11:00", "13:30", "15:00"]:
                for symbol in symbols:
                    context = self._build_context(symbol, date, time_slot)
                    signal = self.scorer.score(context)
                    if signal.confidence > 0.5:
                        self._open_position(symbol, signal, context)
                # 每分钟扫描持仓退出
                self.exit_monitor.scan_positions(self.positions, ...)
        return self._compute_results()
```

### Step 3: 指标计算
```python
# app/backtest/metrics.py
@dataclass
class BacktestResult:
    total_return: float
    sharpe: float
    sortino: float
    max_drawdown: float
    win_rate: float
    avg_pnl_per_trade: float
    pnl_by_exit_reason: Dict[str, float]
    pnl_by_thesis: Dict[str, float]
    pnl_by_session: Dict[str, float]
    trades: List[Dict]
```

### Step 4: CLI
```python
# app/backtest/__main__.py
"""
Usage:
  python -m app.backtest run --symbols=NVDA,TSLA --start=2025-01-01 --end=2026-04-30
  python -m app.backtest sweep --param=DAYTRADE_TP1_PCT --range=2,8,0.5 --start=... --end=...
"""
```

### Step 5: 集成到优化流程
```python
# scripts/optimize_param.py
def optimize_param(param_name, train_start, train_end, oos_start, oos_end, candidates):
    train_results = {}
    for value in candidates:
        result = backtest(symbols=watchlist, start=train_start, end=train_end, param={param_name: value})
        train_results[value] = result.sharpe

    best_value = max(train_results, key=train_results.get)
    oos_result = backtest(symbols=watchlist, start=oos_start, end=oos_end, param={param_name: best_value})

    if oos_result.sharpe < train_results[best_value] * 0.7:
        print(f"⚠ Overfitting risk: train Sharpe={train_results[best_value]:.2f}, OOS Sharpe={oos_result.sharpe:.2f}")
        return None  # 拒绝调整
    return best_value
```

**验收标准**:
- `python -m app.backtest run --symbols=SPY --start=2025-01-01 --end=2025-12-31` 能跑通
- 输出 `BacktestResult.sharpe` 等指标
- 单测：用一段已知正收益的数据回测，断言 PnL > 0

---

## F30 — Walk-Forward 调参框架

**优先级**: P2（依赖 F29 完成）
**文件**: `scripts/walk_forward_optimize.py`

**修复方案**:
```python
"""
Walk-Forward Optimization

将 2024-2025 数据按月切分:
  Train: 2024-01 ~ 2024-12 (12 个月)
  Test:  2025-01 (1 个月 OOS)
然后滚动:
  Train: 2024-02 ~ 2025-01 → Test: 2025-02
  Train: 2024-03 ~ 2025-02 → Test: 2025-03
  ...

每次 Train 阶段优化参数, 在 Test 阶段验证。
所有 Test 结果拼接 = 真实可信的 OOS 表现。
"""
def walk_forward(param_name, candidates, symbols, total_start, total_end, train_months=12, test_months=1):
    results = []
    for train_start, train_end, test_start, test_end in rolling_windows(...):
        best = optimize_on_train(param_name, candidates, symbols, train_start, train_end)
        oos = backtest(symbols, test_start, test_end, param={param_name: best})
        results.append({
            "train_window": (train_start, train_end),
            "test_window": (test_start, test_end),
            "best_value": best,
            "train_sharpe": ...,
            "oos_sharpe": oos.sharpe,
            "oos_return": oos.total_return,
        })
    return aggregate_results(results)
```

**强制规则**: 任何参数变更必须经过 walk-forward 验证，OOS Sharpe 至少为 train Sharpe 的 70%，否则**禁止应用**。

**验收标准**:
- 跑一次 NVDA 30 天 walk-forward，输出报告
- 集成到 `review_loop.ActionExecutor`：调参前必须 query walk-forward 结果

---

# 第七部分：配置 SSOT 与漂移检测

## F31 — `min_confidence` 14 处冗余（详见 F4）

见 F4 修复方案。

## F32 — `exit_policy.yaml` 与代码硬编码并存

**优先级**: P1
**文件**: `app/risk/exit_policy.yaml`、`app/pipeline/order_executor.py:380`、`app/core/position_sync.py:431,534`

**现状**:
```python
# order_executor.py:380
atr_mult = 2.5  # exit_policy.yaml atr_stop.initial = 2.5  ← 注释提示但实际硬编码
```
约 5 处类似情况。

**问题**: 改 `exit_policy.yaml` 不一定生效，调参陷阱。

**修复方案**:
1. 找到所有标注 `# exit_policy.yaml ...` 的硬编码（grep 查找）
2. 全部替换为 `ExitPolicyGuardian().get_atr_multiplier(...)` 等真实读取
3. 在启动时调 `ExitPolicyGuardian.validate_coverage()`：扫描代码中的硬编码注释，断言对应 YAML 路径已被代码读取

**验收标准**:
- 改 `exit_policy.yaml` 中 `atr_stop.initial` 从 2.0 → 1.5，重启后实际 atr_mult = 1.5
- `pytest tests/risk/test_exit_policy_coverage.py` 通过

---

## F33 — `drift_detector` 无定时调度

**优先级**: P2
**文件**: `app/config_mgmt/drift_detector.py`、`main.py:212-296`

**现状**: drift_detector 文件存在，但 main.py 启动序列中虽有"装载"动作，**无定期 4h 巡检**。

**修复方案**:
在 `app/core/scheduler_triggers.py` 增加：
```python
def check_config_drift(scheduler):
    """每 4 小时检查一次配置漂移 (内存中的配置 vs YAML 文件)"""
    if not hasattr(scheduler, '_last_drift_check'):
        scheduler._last_drift_check = 0
    now = time.time()
    if now - scheduler._last_drift_check < 4 * 3600:
        return
    scheduler._last_drift_check = now

    from app.config_mgmt.drift_detector import DriftDetector
    detector = DriftDetector()
    drifts = detector.scan()
    if drifts:
        logger.warning(f"[配置漂移] 发现 {len(drifts)} 处漂移: {drifts}")
        for d in drifts:
            notifier.send_alert(f"配置漂移: {d}")
```
在 main.py 主循环挂载 `check_config_drift`。

**验收标准**:
- 启动后 4h 检查一次，日志出现 `[配置漂移]` 行
- 修改 yaml 后下次检查应能检测到

---

# 优先级路线图

## 本周（P0 — 止血与必修）
1. **F1** OpenChecker 死链修复
2. **F5** 追高对冲反模式（intraday_scorer.py:509）
3. **F6** change_pct 用日累计（改用 slope_5m）
4. **F10** 风险单位 R 统一 ⭐ 最关键
5. **F11** Day Trade Mode TP 阈值重设 ⭐
6. **F15** trail_state latent NameError
7. **F18** DB 路径硬编码 `/opt/ftseek/` 全部清除
8. **F19** trade_attribution 表 CREATE TABLE
9. **F20** link_trade_outcome 缺 decision_id 时 fallback
10. **F28** enhanced_debate 子系统路径修正

## 本月（P1 — 设计性优化）
11. **F2** daily_battle_plan_us.json 路径统一
12. **F4** min_confidence 14 处收敛
13. **F7** opening_drive session 拆分
14. **F12** profit_lock 删除（chandelier 接管）
15. **F13** 6 条退出规则质量评分
16. **F14** PRE_CLOSE_EXIT 简化为 2 档
17. **F17** 组合层面止损
18. **F21** suggestion_engine 列对齐
19. **F22** exit_reason × pnl 报告
20. **F23** lessons DB 路径统一
21. **F27** review_loop 扩白名单
22. **F29** Minimal backtest framework ⭐ 季度优化的依赖
23. **F32** exit_policy 硬编码消除

## 本季度（P2 — 系统性升级）
24. **F3** plan revalidate_on_restart 强制调用
25. **F8** 因子 IC 月度归因
26. **F9** signal_quality_monitor (事中校验)
27. **F16** 入场→退出 thesis 对称性
28. **F24** deep_review 模块整改/删除
29. **F25** ImprovementTracker 持久化
30. **F26** AutoTuner 删除
31. **F30** Walk-Forward 调参框架 ⭐
32. **F33** drift_detector 4h 巡检

---

# 验收检查清单

## 修复后每周必跑
- [ ] `pytest tests/` 全绿（除已知 pytz/psutil 缺失环境问题）
- [ ] `python -m app.backtest run --symbols=SPY,QQQ --start=last_30_days` 通过
- [ ] `python scripts/exit_reason_pnl_report.py --days=7` 输出报告
- [ ] daily_review JSON 含所有新增字段（attribution, exit_quality, signal_quality）
- [ ] `grep "/opt/ftseek" app/` 无匹配（除 test_simulate_realistic_flow.py）
- [ ] `grep "min_confidence" app/` 调用点 < 5 处

## 每月必查
- [ ] 因子 IC 报告 `reports/factor_ic_YYYYMM.md`
- [ ] exit_reason × pnl 月度汇总
- [ ] orphan_outcomes_count / total_trades < 5%
- [ ] 任何 exit_reason 30 天 sharpe_proxy < 0 → 评估禁用
- [ ] walk-forward 结果一览，OOS / Train Sharpe ratio > 0.7

## 每季度必查
- [ ] 所有死代码（auto_tuner / improvement_tracker / deep_review/engine）已删除
- [ ] 新参数加入 PARAM_MAP，覆盖率 > 80% 的 exit_policy.yaml 节点
- [ ] thesis-driven exit 完成度，按 thesis 分组的 PnL 表
- [ ] portfolio drawdown 控制有效性（实盘 max drawdown 不超过 stop_drawdown_pct × 1.2）

---

# 附录 A：关键文件路径速查

| 模块 | 文件 |
|------|------|
| 盘前生成 | `app/us_pipeline/premarket_planner.py` |
| 盘前触发 | `app/pipeline/llm_pipeline.py:849-1124` |
| OpenChecker | `app/us_pipeline/open_checker.py` |
| BattlePlanManager | `app/us_pipeline/battle_plan_manager.py` |
| IntradayScorer | `app/core/intraday_scorer.py` |
| 信号管线 | `app/pipeline/signal_pipeline.py` |
| RiskGateway (新) | `app/risk/risk_gateway.py` |
| ExitMonitor | `app/risk/position_exit_monitor.py` |
| ExitPolicy YAML | `app/risk/exit_policy.yaml` |
| 日报 | `app/review/daily_review.py` |
| 周报 | `app/review/weekly_review.py` |
| 复盘触发 | `app/core/scheduler_triggers.py:251-440` |
| review_loop | `app/core/review_loop.py` |
| ConfigWriter | `app/core/config_writer.py` |
| lesson_injector | `app/review/lesson_injector.py` |
| suggestion_engine | `app/review/suggestion_engine.py` |
| agent_bridge | `app/llm/agent_bridge.py` |
| decision_recorder | `app/storage/decision_recorder.py` |
| trade_store | `app/storage/trade_store.py` |
| exit_quality_tracker | `app/storage/exit_quality_tracker.py` |
| SSOT YAML | `config/unified_config.yaml` |

# 附录 B：建议新增文件清单

```
app/backtest/
  __init__.py
  data_loader.py             # F29
  backtest_engine.py         # F29
  metrics.py                 # F29
  __main__.py                # F29 CLI
app/core/
  paths.py                   # F18 — 路径解析单一入口
  signal_quality_monitor.py  # F9
scripts/
  exit_reason_pnl_report.py  # F22
  optimize_param.py          # F29 → F30
  walk_forward_optimize.py   # F30
  compute_factor_ic.py       # F8
tests/
  backtest/
    test_backtest_engine.py
    test_walk_forward.py
  risk/
    test_portfolio_drawdown.py  # F17
    test_risk_unit_R.py         # F10
  pipeline/
    test_thesis_driven_exit.py  # F16
reports/
  (自动生成)
  factor_ic_YYYYMM.md        # F8
  exit_reason_pnl_*.md       # F22
  walk_forward_*.md          # F30
```

# 附录 C：建议修改 unified_config.yaml 节点

```yaml
# 新增
paths:
  data_dir: "data"
  reviews_dir: "data/daily_reviews"
  decisions_db: "data/decisions.db"
  rejections_db: "data/rejections.db"
  trade_records_db: "data/ftseek_us.db"
  exit_quality_db: "data/exit_quality.db"

risk:
  per_trade_risk_pct: 0.005    # F10
  max_trade_risk_pct: 0.010    # F10
  portfolio:                    # F17
    warn_drawdown_pct: 0.02
    reduce_drawdown_pct: 0.025
    stop_drawdown_pct: 0.03
  signal_quality:
    min_confidence: 0.6         # 唯一 SSOT (F4)
    min_signal_strength: 50

# 标记 DEPRECATED 但保留兼容
scheduler:
  min_confidence: 0.5  # DEPRECATED 2026-05-12, use risk.signal_quality.min_confidence
risk_limits:
  min_confidence: 0.7  # DEPRECATED 2026-05-12
```

# 附录 D：建议修改 exit_policy.yaml 节点

```yaml
# 由 F11 修改
day_trade_mode:
  initial_stop:
    atr_multiplier: 1.5     # 1.8 → 1.5
    max_loss_pct: 3.0       # 5.0 → 3.0
  take_profit:
    tier_1:
      profit_pct: 4.0       # 3.0 → 4.0
      close_pct: 33         # 50 → 33
    tier_2:
      profit_pct: 8.0       # 6.0 → 8.0
      close_pct: 33
      move_sl_to: chandelier_2x
    tier_3:                 # 新增
      profit_pct: 15.0
      close_pct: 100
  profit_lock:
    enabled: false          # F12: 由 chandelier 接管

# 由 F14 修改
exit_rules:
  pre_close_exit:
    window_minutes: 15
    min_hold_minutes: 60      # 30 → 60
    hold_overnight_threshold_atr: 1.0
    loss_force_close: true
    # 删除 4 层、time_weighted、close_ratio 等
  chandelier_trail:
    activation_profit_atr: 0.8  # 1.2 → 0.8 (F14)
```

---

**报告结束。** 任何项目实施过程中如有歧义，参考本报告的「现状代码引用」+「问题诊断」+「方案 + 验收」四段式即可定位。

执行顺序建议：**P0 全部完成（本周内） → 启动 F29 backtest 框架 → 用 backtest 验证 P1 参数变更**。
