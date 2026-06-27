# 个股阶段驱动的动态 Horizon 决策框架

## 1. 背景与目标

FTSeek-US Playbook 系统的 OKR 目标：
- 胜率 > 55%
- 周收益 ≥ $1,000
- 毕业门禁：IC(T+3) > 0.05，样本 ≥ 30，无 5 次连续亏损

IC 实测揭示关键矛盾：
- T+1 IC = -0.114（反预测）
- T+5 IC = +0.265（强正 alpha）
- 当前系统用统一日内退出策略，导致周级 alpha 被系统性截断。

因此需要把"持有周期"从全局常量变为**个股级决策**：针对每只股票的阶段特征、市场环境、信号质量综合判断，决定该标的适合做短线还是长线。

## 2. 核心设计原则

1. **不预设方向决定周期**：不默认 SHORT=long、LONG=short。
2. **个股阶段特征驱动**：从标的当前状态提取特征。
3. **历史收益估计**：用历史数据估计不同 horizon 下的期望收益。
4. **可解释**：每个 horizon 决策都有明确理由和置信度。
5. **动态优化**：每周根据实际 outcome 更新模型和权重。
6. **OKR 对齐**：所有设计指向胜率、周收益、IC 三个目标。

## 3. 系统架构

```
PlaybookData + RegimeContext + ScreenResult
            ↓
    Feature Extractor
            ↓
    Horizon Return Model (历史收益估计)
            ↓
    Horizon Scorer
            ↓
    HorizonDecision {horizon, short_score, long_score, confidence, reasoning}
            ↓
    InstructionGenerator (按 horizon 取 R:R / SL / TP)
            ↓
    Execution (futu_simulate)
            ↓
    Exit Rules (按 horizon 路由)
            ↓
    Tracking + IC + Factor Analysis (按 horizon 分组)
            ↓
    Weekly Feedback Loop (更新模型)
```

## 4. 核心模块设计

### 4.1 Feature Extractor（特征工程）

#### 标的阶段特征

| 特征名 | 来源 | 类型 | 说明 |
|---|---|---|---|
| `trend_strength` | PlaybookData.technical | float | ADX 或价格斜率，0-1 |
| `price_vs_ma20` | PlaybookData.technical | float | 价格相对 MA20 的偏离 |
| `price_vs_ma50` | PlaybookData.technical | float | 价格相对 MA50 的偏离 |
| `volatility_atr_pct` | PlaybookData.technical | float | ATR% |
| `volume_ratio` | PlaybookData.technical | float | 当日量 / 5 日均量 |
| `bollinger_width` | PlaybookData.technical | float | 布林带宽度 |
| `rsi_14` | PlaybookData.technical | float | RSI |
| `macd_signal` | PlaybookData.technical | str | golden_cross / death_cross / neutral |

#### 信号特征

| 特征名 | 来源 | 类型 | 说明 |
|---|---|---|---|
| `triggered_factors` | ScreenResult.factors | list[str] | 触发的因子 |
| `factor_score` | ScreenResult.score | int | 因子总分 |
| `confidence` | Instruction.confidence | int | 0-100 |
| `data_quality_ok_count` | Instruction.data_quality | int | 0-6 |
| `direction` | Instruction.direction | str | BUY / SELL_SHORT |

#### 市场环境特征

| 特征名 | 来源 | 类型 | 说明 |
|---|---|---|---|
| `vix` | RegimeContext | float | VIX 指数 |
| `spy_regime` | RegimeContext | str | RISK_ON/CAUTIOUS/RISK_OFF/NO_TRADE |
| `spy_trend` | RegimeContext | str | above_ma20 / below_ma20 |
| `sector_momentum` | 需新增计算 | str | strong / weak / neutral |
| `earnings_distance` | PlaybookData.earnings | int | 距离财报天数 |

#### 标的基本面特征

| 特征名 | 来源 | 类型 | 说明 |
|---|---|---|---|
| `market_cap_tier` | fundamental_provider | str | large / mid / small |
| `sector` | PlaybookData.fundamental | str | 行业 |
| `pe_vs_industry` | PlaybookData.fundamental | float | PE 相对行业倍数 |

### 4.2 Horizon Return Model（历史收益估计）

为每个特征组合估计：

```
E[return | feature_key, horizon=short]
E[return | feature_key, horizon=long]
```

#### 特征 key 分层设计

```
level_1: 单因子 + horizon
  e.g. "insider_cluster_buy:long"

level_2: 因子组合 + horizon
  e.g. "insider_cluster_buy+above_ma20:long"

level_3: 因子 + 市场环境 + horizon
  e.g. "insider_cluster_buy+vix_low+risk_on:long"

level_4: 因子 + 标的状态 + horizon
  e.g. "volume_expansion+high_trend+large_cap:long"
```

#### 收益估计方法

对每个特征 key 和 horizon：

```python
stats = {
    "n": 历史样本数,
    "mean_return": 平均收益,
    "win_rate": 胜率,
    "profit_factor": 盈亏比,
    "bayesian_lower": 95% CI 下界,
    "bayesian_upper": 95% CI 上界,
}
```

使用 `factor_analyzer.py` 已有的贝叶斯方法计算置信区间。

#### 数据表设计

新增表 `playbook_horizon_stats`：

```sql
CREATE TABLE IF NOT EXISTS playbook_horizon_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_key TEXT NOT NULL,
    horizon TEXT NOT NULL,
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_return REAL DEFAULT 0,
    avg_pnl REAL DEFAULT 0,
    profit_factor REAL DEFAULT 0,
    bayesian_win_rate REAL,
    bayesian_lower REAL,
    bayesian_upper REAL,
    last_updated TEXT,
    UNIQUE(feature_key, horizon)
);
```

### 4.3 Horizon Scorer（周期评分器）

#### 输入输出

```python
@dataclass
class HorizonDecision:
    horizon: str                    # "short" or "long"
    short_score: float               # 短线综合得分
    long_score: float                # 长线综合得分
    confidence: float                # |short_score - long_score|
    reasoning: Dict[str, Any]        # 可解释理由
    feature_weights: Dict[str, float] # 各特征贡献
```

#### 评分公式

```python
def score(features, return_model):
    # 对每个特征 key，查历史收益
    short_contributions = []
    long_contributions = []

    for feature_key in feature_keys:
        stats = return_model.get(feature_key)
        if stats and stats["n"] >= MIN_SAMPLES:
            short_contributions.append(stats["short"]["bayesian_win_rate"] * stats["short"]["avg_return"])
            long_contributions.append(stats["long"]["bayesian_win_rate"] * stats["long"]["avg_return"])

    # 如果历史数据不足，使用默认规则兜底
    if not short_contributions:
        return fallback_score(features)

    short_score = sum(short_contributions) / len(short_contributions)
    long_score = sum(long_contributions) / len(long_contributions)

    # 选择
    if long_score > short_score * (1 + MARGIN):
        chosen = "long"
    elif short_score > long_score * (1 + MARGIN):
        chosen = "short"
    else:
        chosen = "short"  # 默认保守

    return HorizonDecision(...)
```

#### 默认规则兜底（冷启动期）

当历史样本不足时，使用中性默认规则：

```yaml
horizon_fallback_rules:
  - condition: "volatility_atr_pct > 0.15 or vix > 22"
    horizon: short
    reason: "高波动环境，短线"

  - condition: "earnings_distance <= 3"
    horizon: short
    reason: "财报临近，短线"

  - condition: "trend_strength > 0.6 and volume_ratio > 1.2 and vix < 20"
    horizon: long
    reason: "强趋势+放量+低波动，长线"

  - condition: "insider_cluster_buy in factors or volume_expansion in factors"
    horizon: long
    reason: "基本面/资金面因子，需要时间发酵"

  - condition: "macd_signal == golden_cross and price_vs_ma20 > 0"
    horizon: long
    reason: "技术面趋势确认"

  default: short
```

**注意**：这些默认规则只是冷启动兜底，模型会快速用实际数据替代它们。

## 5. 配置层

### 5.1 `config/playbook_horizon.yaml`

```yaml
horizon_selector:
  enabled: true
  min_samples_for_model: 5
  margin_threshold: 0.1            # 选择阈值 margin
  default_horizon: "short"          # 不确定时默认短线

  # 历史收益模型更新频率
  model_update:
    daily: true
    weekly_deep_update: true

  # 特征分层
  feature_levels:
    - single_factor
    - factor_pair
    - factor_plus_market
    - factor_plus_symbol_state

  # 风险预算分配
  risk_budget:
    short_pct: 0.5
    long_pct: 0.5
    rebalance_weekly: true
    min_pct: 0.1

# 短线参数
short_horizon:
  rr_ratio: 1.0
  fixed_stop_loss_pct: 2.0
  max_hold_days: 1
  skip_pre_close_if_profit: false
  time_exit_hours: 2.0

# 长线参数
long_horizon:
  rr_ratio: 2.5
  fixed_stop_loss_pct: 2.0
  max_hold_days: 5
  skip_pre_close_if_profit: true
  overnight_gap_stop_pct: 3.0
  time_exit_hours: 999
```

### 5.2 `config/unified_config.yaml`

在 `playbook:` 节增加引用：

```yaml
playbook:
  # ... 既有配置 ...
  horizon_strategy:
    enabled: true
    config_path: "config/playbook_horizon.yaml"
```

## 6. 代码改动清单

### 新增文件
- `app/playbook/horizon_selector.py`
- `app/playbook/horizon_return_model.py`
- `config/playbook_horizon.yaml`
- `tests/test_horizon_selector.py`
- `tests/test_horizon_return_model.py`
- `tests/test_instruction_generator_horizon.py`
- `tests/test_pipeline_horizon.py`
- `tests/test_ic_tracker_horizon.py`

### 修改文件
- `app/playbook/db.py`
- `app/playbook/config.py`
- `app/playbook/pipeline.py`
- `app/playbook/instruction_generator.py`
- `app/playbook/playbook_service.py`
- `app/playbook/shadow_tracker.py`
- `app/playbook/ic_tracker.py`
- `app/playbook/factor_analyzer.py`
- `app/playbook/feishu_push.py`
- `config/unified_config.yaml`

## 7. 数据闭环

### 7.1 每周自动更新

```
run_playbook_weekly:
  ├─ FactorAnalyzer.update_all_stats()  # 既有
  ├─ HorizonSelector.return_model.update()  # 新增
  │    └─ 基于最近 90 天 outcome 重新估计 horizon 收益
  └─ 调整 horizon 规则权重
```

### 7.2 追踪指标

新增 dashboard metric：
- `horizon_selection_accuracy`: 推荐 long 后 T+5 收益 > 0 的比例
- `short_horizon_win_rate`: 推荐 short 后的胜率
- `long_horizon_win_rate`: 推荐 long 后的胜率
- `short_horizon_ic`: short 分组 IC
- `long_horizon_ic`: long 分组 IC
- `horizon_confidence_calibration`: 置信度与实际胜率的相关性

## 8. 测试计划

### 新增测试文件

1. `tests/test_horizon_selector.py`
   - 特征提取正确性
   - 默认规则命中
   - 历史数据不足时 fallback
   - 同特征重复调用一致性

2. `tests/test_horizon_return_model.py`
   - 收益估计计算正确
   - 贝叶斯置信区间合理
   - 增量更新正确

3. `tests/test_instruction_generator_horizon.py`
   - short 的 take_profit = entry * 1.02
   - long 的 take_profit = entry * 1.05
   - 止损保持 2%

4. `tests/test_pipeline_horizon.py`
   - 生成指令时 horizon 字段正确
   - exit rules 按 horizon 路由

5. `tests/test_ic_tracker_horizon.py`
   - 按 horizon 分组 IC 正确
   - 毕业门禁读取 long-horizon IC

### 验证命令

```bash
# 配置校验
python -c "from app.playbook.horizon_selector import HorizonSelector; print(HorizonSelector().config)"

# 单测
python -m pytest tests/test_horizon_selector.py tests/test_horizon_return_model.py -x

# 端到端（模拟盘）
python -c "from app.playbook.pipeline import run_playbook_generate; run_playbook_generate(None, force=True)"
```

## 9. OKR 里程碑

### Week 1: 框架上线
- [x] `playbook_instructions` 增加 horizon 字段
- [x] `HorizonSelector` 模块上线
- [x] 生成首批 short/long 指令
- [x] 按 horizon 分组追踪

### Week 2: 模型校准
- [ ] 累计 ≥30 笔 short + ≥30 笔 long 样本
- [ ] 计算 horizon 分组 IC
- [ ] 调整默认规则权重

### Week 3: 优化与聚焦
- [ ] 识别优胜 horizon 策略
- [ ] 动态调整 risk budget
- [ ] 达到胜率 > 50%

### Week 4: 毕业准备
- [ ] 胜率 > 55%
- [ ] 周收益 ≥ $1000
- [ ] long-horizon IC(T+3) > 0.05
- [ ] 准备 live 切换

## 10. 关键风险与缓解

| 风险 | 缓解 |
|---|---|
| 冷启动期历史数据不足 | 默认规则兜底 + 快速积累样本 |
| 特征过多导致 overfitting | 限制特征层级，使用贝叶斯置信区间 |
| 短线和长线仓位冲突 | 同一 symbol 同 direction 同时只允许一个 horizon |
| 模型推荐错误 | 保留置信度，低置信度默认 short |
| 高波动市场长线亏损 | 高 VIX 时默认 short |
| 毕业门禁样本不足 | short 和 long 分别积累，不相互污染 |

## 11. 待评估问题

以下问题需要 review 时重点检查：

1. **特征选择是否完整？**
   - 是否需要加入 order_flow、sentiment、options flow 等特征？

2. **模型复杂度是否合适？**
   - 当前是简单加权平均，是否需要 ML 模型（如 XGBoost/Logistic）？

3. **默认规则是否会引入方向先验？**
   - 需要确保默认规则不隐含"方向决定周期"的假设。

4. **风险预算动态调整是否合理？**
   - 是否应该让表现好的 horizon 获得更多仓位？

5. **与现有 graduation gate 的关系**
   - 毕业门禁是基于总体 IC，还是分别看 short/long？

## 12. 优化进展（2026-06-26 第二轮 review 后）

针对 Agent review 提出的数据/架构问题，已完成以下修复：

### 12.1 已修复问题

| 问题 | 修复内容 | 状态 |
|---|---|---|
| DB 无 horizon 列 | 运行 `scripts/migrate_playbook_horizon.py`，已为 **70** 条历史指令回填 `horizon='short'`（含 outcome NULL/holding） | ✅ 完成 |
| playbook_horizon_stats 表缺失 | 同上迁移脚本创建表 | ✅ 完成 |
| T+3/T+5 forward returns 回填窗口过短 | `ic_tracker.backfill_forward_returns` lookback 从 10 天扩到 30 天，limit 扩到 1000 | ✅ 完成 |
| `_score_with_model` feature_key/horizon 混用 bug | `HorizonSelector` 现在分别生成 short/long feature_key 并分别查询 | ✅ 完成 |
| fallback 规则方向先验 | MACD 金叉、强趋势+放量、基本面因子改为 BUY-only；SHORT+放量独立加分 | ✅ 完成 |
| HorizonSelector 每次新建 DB 连接 | `pipeline.py` 增加模块级单例 `_get_horizon_selector()` | ✅ 完成 |
| missed/void 污染 horizon 统计 | `HorizonReturnModel.update()` 只保留 outcome 为 win/loss 的记录 | ✅ 完成 |
| 15 条指令 horizon 仍为 NULL | 迁移脚本 UPDATE 范围扩大到 `WHERE horizon IS NULL`（不再限制 outcome） | ✅ 完成 |
| backfill 脚本 import 路径隐式依赖 | `backfill_playbook_forward_returns.py` 改为从 `app.utils.holiday_calendar` 直接 import，并加载 `.env.us`、禁用 FutuLV3 | ✅ 完成 |
| `HorizonReturnModel.update()` 样本不足仍空转 | `pipeline.py` 增加样本 guard（≥50 笔 win/loss 才执行） | ✅ 完成 |
| 长 horizon 路径无快速关闭能力 | `HorizonSelector` 增加 `enabled` 开关，config 中 `horizon_selector.enabled=false` 时全局 short | ✅ 完成 |

### 12.2 新增脚本/测试

- `scripts/migrate_playbook_horizon.py` — 生产 DB 迁移 + 已有记录回填
- `scripts/backfill_playbook_forward_returns.py` — 一次性全量回填 T+1/T+3/T+5（注意：运行依赖行情 API， dry-run 需在实盘环境验证）
- 新增测试覆盖方向感知 fallback、模型 key 分离、missed/void 过滤

### 12.3 测试结果

项目 venv 下：

```bash
python -m pytest tests/test_horizon_selector.py tests/test_horizon_return_model.py \
                 tests/test_instruction_generator_horizon.py tests/test_pipeline_horizon.py \
                 tests/test_ic_tracker_horizon.py -v
# 18 passed
```

> 注：之前文档中 "31 passed" 包含了 `tests/test_playbook_pipeline.py` 和 `tests/test_playbook_ic_tracker.py`，这两个文件在当前 hermes venv 下因 protobuf 冲突无法运行。核心 horizon 相关 18 个测试在项目 venv 下全部通过。

### 12.4 仍待验证/后续工作

1. **一次性全量回填 T+3/T+5**：`scripts/backfill_playbook_forward_returns.py` 已修复（加载 `.env.us`、禁用 FutuLV3、按 symbol 分组、显式 disconnect、`os._exit`），运行不再挂死；但受 TwelveData 免费版 8 calls/min 限速，28 个 symbol 回填约需 4-5 分钟。
2. **样本量积累**：当前 70 条指令、36 条 win/loss，且全部 horizon='short'。模型路径仍无法激活，long 对照组需要未来新指令积累。
3. **IC 前提复现**：T+5 IC=+0.265 的前提需在全量回填后重新计算，目前仍不可证伪/证实。
4. **动态 risk budget**：未在本次改动中实现，待模型路径激活后再做。

### 12.5 设计决策保留

未采纳 review 中"删除 HorizonReturnModel"的建议，理由：
- 删除会彻底放弃"数据驱动 horizon"的原始目标；
- 当前模型路径已修复 key 分离 bug，并在样本不足时安全地 fallback；
- `pipeline.py` 已加样本 guard，避免 CPU 空转；
- 保留模型路径可在样本达标后无缝启用，避免推倒重来。

### 12.6 关键发现：Plan 前提被推翻

第三轮 Audit Agent 回填 T+1 后的实测数据：

| 维度 | IC | n | 判读 |
|---|---|---|---|
| 整体 T+1 | **+0.3408** | 53 | 正预测力 |
| BUY T+1 | +0.2427 | 36 | 正 |
| SHORT T+1 | +0.0074 | 17 | 接近 0 |

这与 Plan 声称的 T+1 IC = -0.114 完全相反。

**结论**：短线策略本身已有正 alpha，"短线截断周级 alpha" 的逻辑链断裂。动态 horizon 框架的必要性需要等 T+3 数据出来后重新评估。

### 12.7 仍待验证/后续工作

1. **等待 T+3 数据积累**：当前 6/23-6/24 指令的 T+3 需要到 6/27、7/1 才能回填。已设置 **2026-07-01 09:00** 定时提醒重新计算 IC。
2. **重新评估 horizon 框架**：
   - 如果 T+3 IC 也为正 → 建议砍掉 long horizon，专注优化短线
   - 如果 T+3 IC 为负或弱于正 → 保留 horizon 分层
3. **修复 MarketDataRouter 的 FutuLV3Provider 挂死**：Auditor 验证 Futu `request_history_kline` 本身可用，但 `MarketDataRouter.get_daily_bars` 通过 FutuLV3Provider 时会挂死。这是系统级 bug，不是本轮引入。
4. **动态 risk budget**：未在本次改动中实现，待模型路径激活后再做。

### 12.8 设计决策调整

- **保留 HorizonReturnModel**，但理由从"数据驱动 horizon"降级为"等 T+3 数据出来后作为决策工具"
- **增加 kill-switch**：`config/playbook_horizon.yaml` 中 `horizon_selector.enabled` 现在真正生效，可一键关闭 long horizon 路径
- **不再扩展**：在 T+3 IC 明确前，不再新增 horizon 相关功能

---

*文件路径: `/opt/ftseek/FTSeek-US/docs/plans/horizon_dynamic_selection_plan.md`*
