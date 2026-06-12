# 战前计划优化：提升胜率与盈利能力

## Context

当前作战计划由 `USPreMarketPlanner` 单次 LLM 调用生成，核心问题：
1. **LLM 幻觉技术指标** — RSI/MACD/支撑阻力位是编造的，导致入场区和止损不合理
2. **无板块轮动感知** — 不知道今日资金流向哪个板块，方向判断缺少大势参考
3. **无经济日历** — CPI/FOMC/非农等宏观事件造成剧烈波动时系统浑然不知
4. **无硬校验** — LLM 输出什么就用什么，ATR 止损过窄、R:R 不合理都无法拦截
5. **Dashboard 信息稀薄** — 只有入场/止损/止盈，无技术上下文

**目标**: 通过注入真实数据 + 硬校验规则，让每个 plan target 的方向、入场区、止损、止盈都有数据支撑。

---

## 实施步骤 (按依赖顺序)

### Step 1: 修复 Futu 日K线获取 (`app/data/futu_lv3_provider.py`)

**问题**: `get_kline(K_DAY)` 有 today-only 过滤器，只返回当天 1 根日线。

**修复**: 新增 `get_daily_kline(symbol, count=60)` 方法，直接调用 `request_history_kline`，不应用日期过滤，缓存 24h。~40 行新增代码。

### Step 2: 经济日历 (`app/data/economic_calendar.py` — 新建, ~200 行)

**数据源**: 内嵌 2026 年已知事件日历 (FOMC 日期由美联储公布，CPI/NFP 有固定规律)，不依赖外部 API。

```python
class EconomicCalendar:
    def get_todays_events() -> List[Dict]          # 今日事件 (名称、时间、影响级别)
    def get_no_trade_windows() -> List[Dict]       # 禁止开仓窗口 (事件前15min→后30min)
    def format_for_llm() -> str                    # LLM prompt 注入文本
    def get_upcoming_events(days=7) -> List[Dict]  # 未来 7 天事件预览
```

**追踪事件**: FOMC (8次/年), CPI (月度), NFP (月度), PPI, GDP, ISM PMI, 零售销售

**禁止交易窗口**: HIGH 影响事件前 15min → 后 30min 禁止开新仓。写入 `no_trade_windows` 字段。

### Step 3: 盘前扫描仪 (`app/us_pipeline/premarket_scanner.py` — 新建, ~350 行)

**3a. 日线技术指标** — 对 watchlist 每只标的:
- 数据: Futu K_DAY (60 根日线) → 复用 `LV3TechnicalCalculator.calculate_from_klines()`
- 输出: RSI(14), MACD(signal/hist), ATR(14)+ATR%, MA20/MA50, Bollinger位置, trend, volume_ratio
- 降级: TwelveData `get_time_series(daily, 60)` (仅在 Futu 失败时, 1 credit/次)

**3b. 板块轮动扫描** — 对 11 个 SPDR 板块 ETF:
- 数据: `MarketDataRouter.get_batch_quotes(["US.XLK", "US.XLF", ...])` (1 次 Futu 批量调用)
- 输出: 按涨跌幅排序, top 3 强/底 3 弱, 标的→板块映射
- 复用 `pre_market_ranker.py` 的 `SECTOR_ETFS` 映射

**3c. 格式化输出**:
```python
def run_full_scan(watchlist) -> Dict          # 汇总扫描结果
def format_technicals_for_llm() -> str        # 技术指标 prompt 文本
def format_sectors_for_llm() -> str           # 板块轮动 prompt 文本
def format_hard_constraints() -> str          # 硬约束规则 prompt 文本
```

扫描结果缓存到 `data/premarket_scan_cache.json` (供 Dashboard 读取)。

### Step 4: 计划校验器 (`app/us_pipeline/plan_validator.py` — 新建, ~200 行)

LLM 输出后，用真实数据硬校验。**Flag (标注) 为主, 仅极端情况 Reject。**

**校验规则** (每条输出 warning 或 reject):

| 规则 | 条件 | 动作 |
|------|------|------|
| RSI Guard | LONG + RSI>75 或 SHORT + RSI<25 | FLAG (标注风险) |
| 入场区偏离 | entry_zone 中点偏离现价 >5% | REJECT → CANCELLED |
| 入场区轻度偏离 | entry_zone 中点偏离现价 >3% | FLAG + 自动修正 |
| 止损过窄 | \|SL - entry\| < 0.5×ATR | FLAG + 自动扩展到 1.0×ATR |
| 风险收益比不足 | R:R < 1.5:1 | FLAG + 自动延伸 TP 到 2.0 R:R |
| 板块逆风 | LONG in 弱势板块 或 SHORT in 强势板块 | FLAG |
| 趋势矛盾 | LONG + trend=DOWN(STRONG) 或反之 | FLAG |

**自动修正**: SL 过窄 → 扩展到 1.0 ATR; R:R 不足 → 延伸 TP; 入场区偏离 → 调整到现价 ±1%。不改变方向。

### Step 5: 集成到 premarket_planner.py (修改, ~80 行新增 / ~30 行修改)

**修改 `run()` 流程** (在现有步骤之间插入):

```
现有: context → news → watchlist → earnings → price_calibration → lesson
新增: ──→ PreMarketScanner.run_full_scan()     (Step 3, 在 LLM 前)
      ──→ EconomicCalendar.get_todays_events() (Step 2, 在 LLM 前)
      ──→ PlanValidator.validate_and_annotate() (Step 4, 在 LLM 后)
```

**重构 `user_input` prompt** — 新增 4 个区块:
1. `【技术指标】` — 每只标的的 RSI/MACD/ATR/趋势/关键位
2. `【板块轮动】` — 强弱板块排名 + 标的板块归属
3. `【经济日历】` — 今日事件 + 禁止交易窗口
4. `【硬约束规则】` — LLM 必须遵守的规则 (ATR 止损公式、R:R ≥ 2.0、RSI 限制等)

**增强 `US_SYSTEM_PROMPT`** — JSON 输出格式新增字段:
- 每个 target: `technical_summary`, `risk_reward_ratio`, `atr_risk{}`, `sector`, `sector_strength`
- 顶层: `sector_rotation{}`, `economic_events[]`, `no_trade_windows[]`

**保存扫描缓存**: `_save_plan()` 同时写入 `data/premarket_scan_cache.json`。

### Step 6: BattlePlanManager 禁止交易窗口 (`app/us_pipeline/battle_plan_manager.py`, ~15 行)

在 `check_trade()` 中，HALT_TRADING 检查之后新增:
- 读取 `plan["no_trade_windows"]`
- 检查当前 ET 时间是否在任何窗口内
- 在窗口内 → 拒绝开新仓

### Step 7: OpenChecker 增强 (`app/us_pipeline/open_checker.py`, ~35 行)

在现有 gap/direction 检查之后新增:
- **经济事件检查**: HIGH 影响事件 30 分钟内 → CANCEL target
- **技术检查**: RSI>75 的 LONG 或 RSI<25 的 SHORT → CANCEL target

### Step 8: Dashboard 增强

**8a. API 路由** (`app/web_dashboard/routes/battle_plan.py`, ~20 行):
- 读取 `premarket_scan_cache.json`，将技术指标和板块数据注入 API 响应
- 每个 target 新增 `_technical` 字段
- 顶层新增 `_sectors`, `_economic_events`, `_no_trade_windows`

**8b. 前端** (`app/web_dashboard/templates/dashboard.html`, ~100 行新增):

新增显示元素:
- **R:R 列** — 风险收益比, 颜色编码 (≥2 绿, ≥1.5 黄, <1.5 红)
- **技术列** — RSI 数值 + 趋势徽章 (UP/DOWN/NEUTRAL)
- **板块条** — 强弱板块 pill 标签 (绿色=强, 红色=弱)
- **经济事件时间线** — 今日事件列表 (HIGH=红边, MEDIUM=黄边)
- **禁止交易窗口告警** — 当前处于窗口内时红色告警条
- **距入场区%** — 现价到入场区的距离百分比 (便于盘中判断)

移动端卡片同步更新以上字段。

---

## 文件清单

| 文件 | 类型 | 工作量 |
|------|------|--------|
| `app/data/futu_lv3_provider.py` | 修改 | +40 行 |
| `app/data/economic_calendar.py` | **新建** | ~200 行 |
| `app/us_pipeline/premarket_scanner.py` | **新建** | ~350 行 |
| `app/us_pipeline/plan_validator.py` | **新建** | ~200 行 |
| `app/us_pipeline/premarket_planner.py` | 修改 | +80/~30 行 |
| `app/us_pipeline/battle_plan_manager.py` | 修改 | +15 行 |
| `app/us_pipeline/open_checker.py` | 修改 | +35 行 |
| `app/web_dashboard/routes/battle_plan.py` | 修改 | +20 行 |
| `app/web_dashboard/templates/dashboard.html` | 修改 | +100/~20 行 |

**总计**: 3 个新文件, 6 个修改文件, ~1065 行新增代码

---

## 验证方法

1. **单元验证**: `PreMarketScanner` 对已知标的计算 RSI/ATR 是否合理 (与 TradingView 对比)
2. **集成验证**: 手动触发 `USPreMarketPlanner.run()` → 检查生成的 JSON 包含新字段且通过 PlanValidator
3. **Dashboard 验证**: 访问 `/` → 确认战前计划卡片显示 R:R、技术指标、板块条、经济事件
4. **向后兼容**: 加载旧格式 plan (无新字段) → `check_trade()` 和 `validate_plan()` 正常工作
5. **禁止交易窗口**: 在有经济事件的日期验证 `check_trade()` 正确拒绝开仓
