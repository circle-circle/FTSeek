# RFC-054: Trend-First 分层信号架构 — 完整方案

> **级别**: 🔴 (交易主路径变更)
> **版本**: v3 FINAL (含代码审计修正 + 富途数据整合)
> **状态**: 等待多 Agent 评审
> **作者**: Hermes + User 联合设计
> **日期**: 2026-05-30
> **关联**: SYSTEM_OBJECTIVE.md F1(买卖规范) + F3(框架逻辑清晰)

---

## 目录

1. [决策自检 (5问)](#1-决策自检)
2. [问题陈述与数据证据](#2-问题陈述与数据证据)
3. [多角色博弈论证](#3-多角色博弈论证)
4. [富途数据能力审计](#4-富途数据能力审计)
5. [架构设计 v3](#5-架构设计-v3)
6. [完整代码](#6-完整代码)
7. [实施步骤 (Phase 0-3)](#7-实施步骤)
8. [测试计划](#8-测试计划)
9. [风险矩阵与应对](#9-风险矩阵与应对)
10. [回滚方案](#10-回滚方案)
11. [成功标准](#11-成功标准)
12. [文件变更清单](#12-文件变更清单)
13. [开放问题 (待评审)](#13-开放问题)
14. [附录: 代码审计发现的3个致命Bug](#14-附录代码审计发现的3个致命bug)

---

## 1. 决策自检

| 问题 | 回答 |
|------|------|
| **为何**: 服务哪个焦点? | F1(买卖规范) + F3(框架逻辑清晰) |
| **预期**: 哪个指标变好? | 胜率 30.3%→≥50%, 日均PnL -$1503→≥+$500 |
| **回滚**: 一行命令回滚? | `YAML: strategies.trend_first.enabled: false` + `git revert` |
| **最简**: 有没有不改代码的方案? | 无 — 信号源架构重构，但复用5个现有模块 |
| **边界**: 哪个级别? | 🔴 交易主路径 (signal_stage.py 编排变更) |

---

## 2. 问题陈述与数据证据

### 2.1 北极星指标现状

```
governance_check 2026-05-30:
  胜率 (30天): 30.3% (目标≥50%) ❌
  日均收益 (30天): -$1503 (目标≥$500) ❌
  总交易: 277 笔
```

### 2.2 历史数据诊断

```
按方向 (profit_winrate_optimization_2026-05-03.md):
  LONG:           118笔  胜率22.9%  PnL +$1,933   ← 做多严重低效
  SHORT:          148笔  胜率38.5%  PnL +$12,021  ← 做空是主引擎
  SHORT_OPTION:     2笔  胜率 0.0%  PnL -$5,984   (已下线)

按退出方式:
  PRE_CLOSE_EXIT:  54% 交易被盘末强制平仓 ← 持仓时间不足
  TRAILING_STOP:   唯一正期望退出, +1.42%平均 ← 趋势跟踪才赚钱
  FLASH_EXIT:      全亏, 0% 胜率

信号质量:
  signal_strength=0: 46% 交易为弱信号 ← 信号质量差
  weekly_optimizer:  训练集=0笔, 验证集=0笔 ← 参数优化全空
```

### 2.3 根因诊断

**核心矛盾**: 当前技术面策略 (`technical.py`) 是**均值回归**逻辑 — "RSI超卖→买入"。但美股的结构性特征是**动量驱动** — "强者恒强"。

| 维度 | 当前做法 | 市场现实 |
|------|---------|---------|
| 入场逻辑 | RSI<30 超卖买入 | 超卖可能是趋势下跌中途 |
| 做空逻辑 | RSI>70 超买卖出 | 强势股可以 RSI>80 持续上涨 |
| 选股范围 | 全市场扫描找超卖 | 机构资金集中在热门板块龙头 |
| 退出方式 | 盘末强制退出 | 趋势跟踪(TRAILING_STOP)是唯一盈利退出 |

**入场(均值回归) × 退出(趋势跟踪) = 逻辑矛盾**

### 2.4 参数审计

`technical.py` 的全部参数均为拍脑袋或教科书默认值，**零数据验证**:

- RSI 阈值 30/70: Wilder 1978 教科书值，未针对美股日内验证
- MACD diff 0.01: 绝对值阈值，不适配高价股 ($5 vs $500)
- 权重 30/25/25/20: 无网格搜索，凑成100%
- min_score=50 > 理论最高分24.25 → 技术面策略永远不会命中 (已通过hotfix降到10)

---

## 3. 多角色博弈论证

### 3.1 正方: 趋势跟随 + 板块龙头 应成为主信号源

**论据 A: 学术证据**
- Jegadeesh & Titman (1993): 动量因子年化超额 ~8%，持续40年有效
- Moskowitz & Grinblatt (1999): 板块动量是机构 alpha 的主要来源
- 美股 SPY 20年胜率 73%，70%时间处于上升趋势

**论据 B: 自身数据**
- TRAILING_STOP (趋势跟踪止盈): 唯一正期望退出方式
- LONG 胜率 22.9%: 均值回归做多严重失败
- 做空胜率 38.5% 高于做多: 说明系统在趋势向下时更有效 (趋势跟踪)

**论据 C: 组件已存在**
- `MarketRegimeIdentifier` — 市场状态识别
- `SectorLeaderDetector` — 板块龙头检测
- `CapitalFlowMonitor` — 资金流监控
- `sector_correlation_reasoner` — 行业关联推理
- `RegimeAdapter` — 策略自适应切换

### 3.2 反方: 趋势策略的风险和限制

**论据 A: 动量崩溃风险**
2009年3月，动量策略单月亏损 -40%。市场V型反转时，所有趋势信号被打脸。
> **应对**: MarketRegime 检测 HIGH_VOL → 自动暂停; ATR 止损兜底

**论据 B: 趋势信号也是滞后的**
RSI、MACD、SuperTrend 都是基于历史价格。板块热度基于历史新闻密度。
> **应对**: 用板块加速度替代绝对热度; 用资金流(实时量价)确认趋势有效性

**论据 C: 日内持仓时间约束**
PRE_CLOSE_EXIT (15:50 后强平)。趋势策略通常需数天持仓。
> **应对**: ATR 渐进止损在2-4h锁定利润; 渐进式4档Tier退出

**论据 D: 板块数据可靠性**
`SectorLeaderDetector.analyze_sectors()` 仅在盘前调用一次，盘中过期。
> **应对**: 过期降权处理; Phase 3 再实现定时刷新

**论据 E: 集中度风险**
趋势策略可能同时指向同板块多只股票。
> **应对**: 复用 `SectorLimitsChecker` (40%上限); 策略内同板块≤2只

**论据 F: 信号遮蔽 (first-hit vs best-hit)**
weak trend_first (score=56) 可能遮蔽 strong technical (score=90)。
> **应对**: 模拟盘阶段用 best-hit (并行评估取 quality 最高)

**论据 G: V型反转日 — 回调永远不来**
强势股可以一整天不回调 (RSI 始终>65)，trend_first 等待回调会错过全天行情。
> **应对**: 双模式入场 — PULLBACK(回调买入) + BREAKOUT(放量突破追入)

**论据 H: 板块热度 = 滞后指标 — 新闻最多时可能是顶部**
"新闻越多分越高" 但市场上 "新闻最多 = 共识最充分 = 边际收益最低"。
> **应对**: 过热保护 (score>80 降权20%); 后续用新闻加速度替代绝对量

**论据 I: 闪崩误判为"优质回调"**
大单砸盘导致价格闪跌，RSI 瞬间变低，被误判为回调买入机会。
> **应对**: 闪崩检测 (slope_5m<-2% + volume_ratio>2.0 → 拦截)

### 3.3 综合判断

| 维度 | 结论 |
|------|------|
| 方向正确性 | ✅ 趋势跟随+板块龙头，学术和自身数据均支持 |
| 风险控制 | ✅ Regime过滤 + ATR止损 + 集中度限制 + 闪崩检测 |
| 可行性 | ✅ 核心组件已存在，新增~180行，扩展SymbolContext+15行 |
| 持仓周期 | 🟡 日内约束下，ATR渐进止损配合，目标2-4h |
| 数据可靠性 | 🟡 板块数据盘前一次，v1容忍降级 |
| 回退安全 | ✅ YAML enabled=false 即可关闭 |

**结论**: 方案可行，渐进式落地 — 先观测模式收集数据，再逐步切换执行模式。

---

## 4. 富途数据能力审计

### 4.1 富途 LV3 已有能力

```
FutuLV3Provider (已有, 生产运行)
├── 实时报价 (Push Quote) — 毫秒级延迟
├── K线数据 (1M/5M/15M/30M/60M/DAY)
├── 逐笔成交 (Push Ticker) — 主买/主卖方向 + 量
├── 买卖盘深度 (Push OrderBook) — 10档盘口
└── 批量快照 (get_batch_quote)

MarketDataRouter.get_full_market_context() 输出字段:
├── price, bid, ask, volume, avg_volume, prev_close
├── rsi, macd, macd_signal, bollinger_upper/lower
├── supertrend_direction, atr, atr_pct
├── ma5, ma10, ma20, ma50, ma200
├── trend: UP/DOWN/NEUTRAL (均线排列)          ← ⚠️ from_raw 丢弃
├── volume_ratio: 当前量/5日均量                ← ⚠️ from_raw 丢弃
├── slope_5m: 5分钟价格斜率 (%)                 ← ⚠️ from_raw 丢弃
├── order_flow_imbalance: 买卖力量比 [-1,1]     ← ⚠️ from_raw 丢弃
├── ticker_buy_sell_ratio: 逐笔买卖比           ← ⚠️ from_raw 丢弃
├── market_regime: TREND_UP/DOWN/RANGE/HIGH_VOL ← ⚠️ from_raw 丢弃
├── spy_price, spy_ma20, spy_ma50
├── vix, vix_regime
└── high_distance_pct: 距日内高点距离           ← ⚠️ from_raw 丢弃
```

### 4.2 数据管道瓶颈

```
Futu LV3 → MarketDataRouter.get_full_market_context()
  ↓ raw dict (50+ 字段, 包含 market_regime/trend/volume_ratio/...)

SymbolContext.from_raw(raw_dict)
  ↓ dataclass (仅映射 ~25 字段)
  ↓ ⚠️ 7个关键字段被丢弃

trend_first.evaluate(ctx)
  ↓ ⚠️ 无法获取 regime/trend/volume_ratio
```

**根因**: `SymbolContext` dataclass 没有声明这些字段，`from_raw()` 没有映射。

### 4.3 解决方案: 扩展 SymbolContext (非补丁式)

| 方案 | 代码量 | 优雅度 | 可维护性 |
|------|--------|--------|---------|
| metadata hack 注入 | ~50行 | ❌ 低 | ❌ 差 |
| **扩展 SymbolContext + from_raw()** | **~15行** | **✅ 高** | **✅ 好** |

**选择**: 在 SymbolContext 中新增字段，from_raw() 中映射已有数据。

---

## 5. 架构设计 v3

### 5.1 整体架构

```
数据层 (已有, 不动):
  FutuLV3Provider → 原始行情
  LV3TechnicalCalculator → 技术指标
  MarketDataRouter → 数据融合 + regime判断
  SymbolContext.from_raw() → 类型化传递 ← 扩展7个字段

编排层 (修改 signal_stage.py):
  signal_stage.process(ctx)
    ├── 策略1: fast_lane (作战计划VIP, 最高优先级)
    ├── 策略2: trend_first (新·趋势优先) + technical 并行评估
    │   └── _inject_sector_info(ctx)  ← 仅板块数据需注入
    └── 策略4: llm_vote (盘前/盘后)
  best-hit: 取 quality = strength × confidence 最高者

策略层 (新增 trend_first.py):
  Layer 0: 防御链 (数据完整性/闪崩/板块新鲜度)
  Layer 1: 环境判断 (ctx.market_regime)
  Layer 2: 选股 (SectorLeader + ctx.order_flow_imbalance)
  Layer 3: 入场 (PULLBACK回调 + BREAKOUT突破)
  Layer 4: 风控参数 (ATR自适应)

风控层 (已有, 不动):
  SectorLimitsChecker → 板块集中度≤40%
  SignalGate → strength=0拦截
  RiskStateMachine → RISK_OFF/HALT拦截
  PriceMomentumGate → 动量确认
```

### 5.2 信号选择: best-hit (模拟盘阶段)

```
模拟盘: 并行评估 trend_first + technical, 取 quality 最高
  quality = strength × confidence

好处:
  1. 同时收集两种策略的信号数据
  2. 不丢失交易机会
  3. 积累数据后再决定 first-hit 还是 best-hit

积累50笔后: 根据数据决定
  trend_first 胜率 > technical → 切回 first-hit
  两者胜率接近 → 保持 best-hit
```

---

## 6. 完整代码

### 6.1 扩展 SymbolContext (`app/core/pipeline_contracts.py`)

**新增字段** (加在现有字段之后, 默认值保证向后兼容):

```python
@dataclass
class SymbolContext:
    # ... 现有所有字段保持不变 ...

    # ── 新增: 趋势与市场状态 (RFC-054) ──
    # 这些数据已在 MarketDataRouter.get_full_market_context() 中计算,
    # 之前 from_raw() 未映射, 导致下游策略无法获取。
    market_regime: str = ""              # "TREND_UP"/"TREND_DOWN"/"RANGE"/"HIGH_VOL"/"LOW_VOL"
    trend: str = ""                      # "UP"/"DOWN"/"NEUTRAL" (个股均线排列)
    volume_ratio: float = 1.0            # 当前成交量 / 5日均量
    slope_5m: float = 0.0               # 5分钟价格斜率 (%)
    order_flow_imbalance: float = 0.0    # 买卖力量失衡 [-1, 1]
    ticker_buy_sell_ratio: float = 1.0   # 逐笔买卖比
    high_distance_pct: float = 0.0       # 距日内高点距离 (%)
    spy_ma20: float = 0.0               # SPY 20日均线
    spy_ma50: float = 0.0               # SPY 50日均线
```

**扩展 from_raw()** (在现有映射之后追加):

```python
@staticmethod
def from_raw(symbol, context, earnings_status=None, vix_multipliers=None):
    ctx = SymbolContext(symbol=symbol)
    # ... 现有所有映射保持不变 ...

    # ── 新增: 趋势与市场状态映射 (RFC-054) ──
    ctx.market_regime = str(context.get("market_regime", "") or "")
    ctx.trend = str(context.get("trend", "") or "")
    ctx.volume_ratio = float(context.get("volume_ratio", 1.0) or 1.0)
    ctx.slope_5m = float(context.get("slope_5m", 0.0) or 0.0)
    ctx.order_flow_imbalance = float(context.get("order_flow_imbalance", 0.0) or 0.0)
    ctx.ticker_buy_sell_ratio = float(context.get("ticker_buy_sell_ratio", 1.0) or 1.0)
    ctx.high_distance_pct = float(context.get("high_distance_pct", 0.0) or 0.0)
    ctx.spy_ma20 = float(context.get("spy_ma20", 0) or 0)
    ctx.spy_ma50 = float(context.get("spy_ma50", 0) or 0)

    # ... earnings_status / vix_multipliers 映射保持不变 ...
    return ctx
```

### 6.2 新增 `app/strategy/pure/trend_first.py`

```python
"""
Trend-First 分层决策策略 — 纯函数实现

输入: SymbolContext (含 market_regime/trend/volume_ratio/order_flow 等富途数据)
输出: SignalCandidate 或 None

设计:
  趋势市 → 板块龙头选股 → 回调/突破入场 → ATR 风控
  震荡市/高波动 → 不激活 (交给 technical / 不交易)

纯函数 — 零副作用, 所有数据通过 SymbolContext 字段获取。
板块数据通过 ctx.metadata["sector_info"] 注入 (由 signal_stage 负责)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import register

logger = logging.getLogger(__name__)


# ─── Layer 0: 防御链 ─────────────────────────────────────────────


def _check_price_stability(ctx: SymbolContext, config: Dict) -> bool:
    """L0-b: 闪崩检测 — 用富途 slope_5m (5分钟价格斜率) 检测急跌

    比 change_pct (日内累计) 更灵敏: 能捕捉盘中瞬时闪崩。
    条件: slope_5m < -2% 且 volume_ratio > 2.0 → 放量急跌 = 恐慌
    """
    if ctx.slope_5m < -2.0:
        if ctx.volume_ratio > 2.0:
            logger.debug(
                f"[TrendFirst] {ctx.symbol} 闪崩检测: "
                f"slope_5m={ctx.slope_5m:.1f}%, vol_ratio={ctx.volume_ratio:.1f}x"
            )
            return False
    return True


def _check_sector_freshness(sector_info: Dict, config: Dict) -> float:
    """L0-c: 板块数据新鲜度 — 超过 max_age_hours 线性降权"""
    last_update_str = sector_info.get("last_update")
    if not last_update_str:
        return 0.7

    try:
        last_update = datetime.fromisoformat(last_update_str)
    except (ValueError, TypeError):
        return 0.7

    max_age = timedelta(hours=config.get("sector_max_age_hours", 2))
    age = datetime.now() - last_update
    if age > max_age:
        hours_over = (age - max_age).total_seconds() / 3600
        return max(0.5, 0.7 - hours_over * 0.1)
    return 1.0


# ─── Layer 3: 入场模式 ───────────────────────────────────────────


def _eval_pullback(ctx: SymbolContext, direction: str, config: Dict) -> Optional[float]:
    """Layer 3 PULLBACK: 趋势中回调入场

    LONG: RSI ∈ [35, 55], 越低(回调越深)质量越高
    SHORT: RSI ∈ [45, 65], 越高(反弹越多)做空质量越高
    """
    rsi_low, rsi_high = config.get(
        f"pullback_rsi_{direction.lower()}",
        (35, 55) if direction == "LONG" else (45, 65)
    )

    if not (rsi_low <= ctx.rsi <= rsi_high):
        return None

    if direction == "LONG":
        quality = (rsi_high - ctx.rsi) * 1.5
    else:
        quality = (ctx.rsi - rsi_low) * 1.5

    return min(30.0, quality)


def _eval_breakout(ctx: SymbolContext, direction: str, config: Dict) -> Optional[float]:
    """Layer 3 BREAKOUT: 放量突破入场 (回调不来时的 Plan B)

    使用 ctx.volume_ratio (富途已计算: 当前量/5日均量)
    """
    min_vol = config.get("breakout_min_volume_ratio", 2.0)
    if ctx.volume_ratio < min_vol:
        return None

    if direction == "LONG" and ctx.rsi > 65:
        return 20.0
    if direction == "SHORT" and ctx.rsi < 35:
        return 20.0

    return None


def _eval_capital_flow(ctx: SymbolContext) -> float:
    """从富途实时数据评估资金流方向

    使用:
      ctx.order_flow_imbalance: 盘口买卖力量比 [-1, 1]
      ctx.ticker_buy_sell_ratio: 逐笔成交买卖比 (1.0=均衡)

    返回: 资金流修正系数 (0.85~1.15)
    """
    ofi = ctx.order_flow_imbalance
    tbsr = ctx.ticker_buy_sell_ratio

    if ofi > 0.1 and tbsr > 1.2:
        return 1.15  # 强资金流入
    elif ofi < -0.1 and tbsr < 0.8:
        return 0.85  # 强资金流出
    else:
        return 1.0   # 中性


# ─── 主评估函数 ──────────────────────────────────────────────────


@register("trend_first")
def evaluate(ctx: SymbolContext, config: Dict[str, Any]) -> Optional[SignalCandidate]:
    """Trend-First 分层决策策略"""

    # ── L0: 防御链 ──

    # L0-a: 数据完整性
    if ctx.rsi <= 0 or ctx.price <= 0:
        return None

    # L0-b: 闪崩检测 (用 slope_5m + volume_ratio)
    if not _check_price_stability(ctx, config):
        return None

    # ── Layer 1: 环境判断 ──
    # 直接使用 ctx.market_regime (由 MarketDataRouter._calc_market_regime() 计算)
    regime = ctx.market_regime
    if not regime or regime in ("RANGE", "HIGH_VOL", "LOW_VOL", ""):
        return None  # 非趋势市不激活

    direction = "LONG" if regime == "TREND_UP" else "SHORT"

    # ── Layer 2: 选股 (板块+资金流) ──
    sector_info = ctx.metadata.get("sector_info", {})
    sector_score = float(sector_info.get("score", 0))
    min_sector_score = config.get("min_sector_score", 40)

    if sector_score < min_sector_score:
        return None

    # L0-c: 板块数据新鲜度
    freshness = _check_sector_freshness(sector_info, config)

    # 龙头判定
    symbol_clean = ctx.symbol.replace("US.", "")
    leaders = [l.replace("US.", "") for l in sector_info.get("leaders", [])]
    is_leader = symbol_clean in leaders
    leader_bonus = config.get("leader_bonus", 15) if is_leader else 0

    # 资金流 (从富途实时数据)
    cf_modifier = _eval_capital_flow(ctx)

    # ── Layer 3: 入场 (双模式) ──
    entry_mode = None
    entry_score = 0.0
    stop_loss_atr_mult = config.get("stop_loss_atr_mult", 1.5)

    # 模式 A: 回调买入
    pb_score = _eval_pullback(ctx, direction, config)
    if pb_score is not None:
        entry_mode = "PULLBACK"
        entry_score = pb_score

    # 模式 B: 放量突破
    if entry_mode is None:
        bo_score = _eval_breakout(ctx, direction, config)
        if bo_score is not None:
            entry_mode = "BREAKOUT"
            entry_score = bo_score
            stop_loss_atr_mult = config.get("breakout_stop_loss_atr_mult", 1.0)

    if entry_mode is None:
        return None

    # ── 指标确认 (加分, 不拦截) ──
    macd_aligned = (ctx.macd - ctx.macd_signal > 0) == (direction == "LONG")
    macd_bonus = config.get("macd_aligned_bonus", 10) if macd_aligned else 0

    st_dir = (ctx.supertrend_direction or "").upper()
    st_aligned = (st_dir == "UP") == (direction == "LONG")
    st_bonus = config.get("supertrend_aligned_bonus", 10) if st_aligned else 0

    # 均线趋势确认 (ctx.trend 由 LV3TechnicalCalculator 计算)
    trend_aligned = (ctx.trend == "UP") == (direction == "LONG")
    trend_bonus = 10 if trend_aligned else 0

    # ── 综合评分 ──
    base_score = 50
    total = (base_score + entry_score + leader_bonus
             + macd_bonus + st_bonus + trend_bonus)
    total *= cf_modifier
    total *= freshness

    # 过热保护
    overheat_threshold = config.get("overheat_threshold", 80)
    if sector_score > overheat_threshold:
        total *= config.get("overheat_penalty", 0.8)

    min_score = config.get("min_score", 55)
    if total < min_score:
        return None

    # ── Layer 4: ATR 风控参数 ──
    atr = ctx.atr if ctx.atr > 0 else ctx.price * 0.02

    strength = min(100, int(total))
    confidence = min(0.90, 0.50 + (total - min_score) / 100)

    trail_activation = atr * config.get("trail_activation_atr", 1.0)
    trail_mult = config.get("trail_atr_mult", 1.5)

    return SignalCandidate(
        symbol=ctx.symbol,
        direction=direction,
        strength=strength,
        confidence=round(confidence, 4),
        strategy_id="TREND_FIRST",
        source="trend_first",
        rationale=(
            f"{entry_mode} | {regime} | "
            f"板块{sector_info.get('name', '?')}(热度{sector_score:.0f}) | "
            f"{'龙头' if is_leader else '跟随'} | "
            f"RSI={ctx.rsi:.0f} vol_ratio={ctx.volume_ratio:.1f} "
            f"OFI={ctx.order_flow_imbalance:.2f} | score={total:.0f}"
        ),
        context=ctx,
        entry_zone=[
            ctx.price - atr * stop_loss_atr_mult * 0.3,
            ctx.price + atr * stop_loss_atr_mult * 0.1,
        ],
        metadata={
            "regime": regime,
            "entry_mode": entry_mode,
            "sector_name": sector_info.get("name"),
            "sector_score": sector_score,
            "is_leader": is_leader,
            "capital_flow_score": ctx.order_flow_imbalance,
            "freshness": freshness,
            "atr": atr,
            "stop_loss_atr_mult": stop_loss_atr_mult,
            "trail_activation_atr": config.get("trail_activation_atr", 1.0),
            "trail_atr_mult": trail_mult,
            "macd_aligned": macd_aligned,
            "supertrend_aligned": st_aligned,
            "trend_aligned": trend_aligned,
        },
    )
```

### 6.3 修改 `app/pipeline/stages/signal_stage.py`

**新增: 板块数据注入函数** (加在 process() 之前):

```python
def _inject_sector_info(ctx: SymbolContext):
    """将板块信息注入 ctx.metadata (供 trend_first 消费)

    仅板块数据需要注入 — 其他数据 (regime/trend/volume_ratio/order_flow)
    已通过扩展后的 SymbolContext.from_raw() 直接获取。
    """
    try:
        from app.risk.sector_leader import get_sector_leader_detector, SECTOR_MAPPING
        detector = get_sector_leader_detector()

        if not detector._sector_scores:
            logger.debug("[Signal] 板块数据未初始化 (可能盘前)")
            ctx.metadata["sector_info"] = {
                "name": "Unknown", "score": 0,
                "leaders": [], "last_update": None,
            }
            return

        leaders = detector.get_sector_leaders()
        leader_symbols = [l["symbol"].replace("US.", "") for l in leaders]

        symbol_clean = ctx.symbol.replace("US.", "")
        for sector_name, symbols in SECTOR_MAPPING.items():
            if symbol_clean in symbols:
                sector_data = detector._sector_scores.get(sector_name)
                ctx.metadata["sector_info"] = {
                    "name": sector_name,
                    "score": sector_data.score if sector_data else 0,
                    "leaders": leader_symbols,
                    "last_update": sector_data.last_update if sector_data else None,
                }
                return

        ctx.metadata["sector_info"] = {
            "name": "Unknown", "score": 0,
            "leaders": leader_symbols, "last_update": None,
        }
    except Exception as e:
        logger.debug(f"[Signal] Sector 注入失败: {e}")
        ctx.metadata["sector_info"] = {
            "name": "Unknown", "score": 0, "leaders": [], "last_update": None,
        }
```

**修改: process() 编排逻辑** (替换现有策略2和策略3之间):

```python
def process(ctx, position_store=None, battle_plan_manager=None,
            voting_coordinator=None) -> Optional[SignalCandidate]:

    symbol = ctx.symbol

    # ── Restricted 拦截 (现有, 保持不变) ──
    # ... (existing code)

    # ── 策略 1: 快车道 (现有, 保持不变) ──
    # ... (existing code)

    # ── 策略 2+3: Trend-First + Technical 并行评估 (best-hit) ──
    candidates = []

    # 2a: Trend-First (趋势优先, 仅趋势市激活)
    try:
        _inject_sector_info(ctx)
        tf_candidate = eval_strategy("trend_first", ctx)
        if tf_candidate:
            logger.info(f"[Signal] {symbol} TrendFirst: "
                       f"{tf_candidate.rationale}")
            candidates.append(tf_candidate)
    except Exception as e:
        logger.debug(f"[Signal] TrendFirst 异常 (放行): {e}")

    # 2b: Technical (均值回归, RANGE 市主力, 趋势市辅助)
    try:
        tech_candidate = eval_strategy("technical", ctx)
        if tech_candidate:
            logger.info(f"[Signal] {symbol} Technical: "
                       f"{tech_candidate.rationale}")
            candidates.append(tech_candidate)
    except Exception as e:
        logger.debug(f"[Signal] Technical 异常 (放行): {e}")

    # 2c: 选最优信号 (best-hit 模式, 模拟盘数据收集)
    if candidates:
        winner = max(candidates, key=lambda c: c.strength * c.confidence)
        if len(candidates) > 1:
            logger.info(
                f"[Signal·BestHit] {symbol} 选中 {winner.strategy_id} "
                f"(quality={winner.strength * winner.confidence:.1f}), "
                f"共 {len(candidates)} 个候选"
            )
        return winner

    # ── 策略 4: LLM 投票 (现有, 保持不变) ──
    # ... (existing code)
```

### 6.4 修改 `app/strategy/pure/__init__.py`

新增一行 import (在现有 import 区域):

```python
from app.strategy.pure import trend_first  # noqa: E402, F401
```

### 6.5 YAML 配置

```yaml
# unified_config.yaml → strategies 节新增:

strategies:
  # ... 现有 fast_lane, technical, llm_vote 保持不变 ...

  trend_first:
    enabled: true
    observe_only: true           # v1: 仅观测, 不实际执行

    # Layer 2: 选股
    min_sector_score: 40         # 板块热度门槛
    leader_bonus: 15             # 龙头加分
    overheat_threshold: 80       # 过热板块阈值
    overheat_penalty: 0.8        # 过热惩罚系数
    sector_max_age_hours: 2      # 板块数据最大有效期

    # Layer 3: 入场
    pullback_rsi_long: [35, 55]  # 做多回调 RSI 区间
    pullback_rsi_short: [45, 65] # 做空回调 RSI 区间
    breakout_min_volume_ratio: 2.0  # 突破最低量比
    breakout_stop_loss_atr_mult: 1.0  # 突破模式止损更紧

    # Layer 3: 指标确认
    macd_aligned_bonus: 10
    supertrend_aligned_bonus: 10

    # 评分
    min_score: 55                # 最低综合分

    # Layer 4: ATR 风控
    stop_loss_atr_mult: 1.5      # 回调模式止损 (1.5×ATR)
    trail_activation_atr: 1.0    # 盈利 1×ATR 激活追踪
    trail_atr_mult: 1.5          # 追踪距离 (1.5×ATR)
```

---

## 7. 实施步骤

### Phase 0: 基础设施 (Day 1)

| 步骤 | 文件 | 操作 | 验证 |
|------|------|------|------|
| 0.1 | `pipeline_contracts.py` | 扩展 SymbolContext 字段 + from_raw() | `python -c "from app.core.pipeline_contracts import SymbolContext; c=SymbolContext(); print(c.market_regime)"` → 输出空字符串 |
| 0.2 | `trend_first.py` | 创建完整代码 (§6.2) | `python -c "from app.strategy.pure import trend_first; print('OK')"` |
| 0.3 | `signal_stage.py` | 新增 `_inject_sector_info()` + 修改 `process()` | `grep "BestHit" signal_stage.py` 确认存在 |
| 0.4 | `__init__.py` | 添加 import | `python -c "from app.strategy.pure import REGISTRY; print('trend_first' in REGISTRY)"` |
| 0.5 | `unified_config.yaml` | 添加 strategies.trend_first | `python -c "from app.core.config_loader import get_config; print(get_config('strategies.trend_first.enabled'))"` |
| 0.6 | 全系统 | `git commit -m "[RFC-054] Trend-First v3 (扩展SymbolContext + 富途数据整合)"` | — |

### Phase 1: 观测验证 (Day 2-5)

**配置**: `observe_only: true` (已默认)

**每日检查清单**:
- [ ] trend_first 产出了多少信号?
- [ ] 信号分布: 哪些板块? PULLBACK vs BREAKOUT?
- [ ] 对比: technical 同时段产出了多少?
- [ ] BestHit 选择了谁? 比例如何?
- [ ] 板块热度数据是否准确? (人工对照)
- [ ] 有无异常: 闪崩误判? 板块数据过期?

**Phase 1 通过条件**:
- [ ] trend_first 信号产出 ≥ 3/天
- [ ] 板块数据与人工核对一致率 ≥ 80%
- [ ] 无 ERROR 级异常 (< 5/天)
- [ ] BestHit 逻辑人工 review 通过

### Phase 2: 开放执行 (Day 6-15)

**配置变更**: `observe_only: false`

**安全约束**:
```yaml
risk_limits:
  max_daily_trades_trend_first: 5
  max_positions_trend_first: 3
  trend_first_max_position_pct: 0.15
```

**快速失败条件**:
```
❌ 连续5笔全部亏损 → 暂停, 检查环境判断
❌ 胜率<25% (15笔后) → 回退到 observe_only
❌ 板块数据连续3次误判 → 提升 min_sector_score (40→60)
```

### Phase 3: 数据驱动优化 (Day 16+)

积累30+笔后:
- 根据胜率调整 pullback_rsi 区间
- 根据龙头 vs 非龙头胜率调整 leader_bonus
- 决定 first-hit vs best-hit
- 如需板块刷新 → 实现30分钟定时刷新
- 如需日内资金流 → 用5分钟K线量价替代

---

## 8. 测试计划

### 8.1 单元测试: `tests/strategy/test_trend_first.py`

```python
"""Trend-First 策略单元测试"""
import pytest
from app.core.pipeline_contracts import SymbolContext
from app.strategy.pure.trend_first import evaluate


def _make_ctx(**overrides) -> SymbolContext:
    """构造测试用 SymbolContext (v3: 使用原生字段)"""
    defaults = dict(
        symbol="US.NVDA", price=940.0, rsi=45.0,
        macd=1.5, macd_signal=1.0,
        bollinger_lower=900.0, bollinger_upper=980.0,
        supertrend_direction="UP", atr=18.0,
        volume=15_000_000, avg_volume=10_000_000,  # volume_ratio=1.5
        # v3 新增字段
        market_regime="TREND_UP",
        trend="UP",
        volume_ratio=1.5,
        slope_5m=0.3,
        order_flow_imbalance=0.15,
        ticker_buy_sell_ratio=1.1,
        metadata={
            "sector_info": {
                "name": "Semiconductors", "score": 60,
                "leaders": ["NVDA", "AMD", "AVGO"],
                "last_update": "2026-05-30T10:00:00",
            },
        },
    )
    defaults.update(overrides)
    return SymbolContext(**defaults)


# ── Layer 1: 环境判断 ──

def test_trend_up_produces_long():
    ctx = _make_ctx()
    sig = evaluate(ctx, {"min_score": 55})
    assert sig is not None
    assert sig.direction == "LONG"

def test_range_regime_returns_none():
    ctx = _make_ctx(market_regime="RANGE")
    assert evaluate(ctx, {"min_score": 55}) is None

def test_high_vol_returns_none():
    ctx = _make_ctx(market_regime="HIGH_VOL")
    assert evaluate(ctx, {"min_score": 55}) is None

def test_trend_down_produces_short():
    ctx = _make_ctx(
        rsi=55, supertrend_direction="DOWN",
        market_regime="TREND_DOWN", trend="DOWN",
    )
    sig = evaluate(ctx, {"min_score": 55, "pullback_rsi_short": [45, 65]})
    if sig:
        assert sig.direction == "SHORT"

# ── Layer 2: 选股 ──

def test_cold_sector_returns_none():
    ctx = _make_ctx(metadata={
        "sector_info": {"name": "Energy", "score": 20,
                       "leaders": [], "last_update": None}
    })
    assert evaluate(ctx, {"min_score": 55, "min_sector_score": 40}) is None

def test_leader_gets_bonus():
    ctx_leader = _make_ctx()     # NVDA in leaders
    ctx_follower = _make_ctx(symbol="US.MU")  # MU not in leaders
    cfg = {"min_score": 55, "leader_bonus": 15}
    sig_l = evaluate(ctx_leader, cfg)
    sig_f = evaluate(ctx_follower, cfg)
    if sig_l and sig_f:
        assert sig_l.strength >= sig_f.strength

# ── Layer 3: 入场 ──

def test_pullback_rsi_in_range():
    ctx = _make_ctx(rsi=42)  # 在 [35, 55] 内
    sig = evaluate(ctx, {"min_score": 55})
    assert sig is not None
    assert sig.metadata["entry_mode"] == "PULLBACK"

def test_breakout_high_volume():
    ctx = _make_ctx(rsi=72, volume_ratio=2.5, volume=25_000_000, avg_volume=10_000_000)
    sig = evaluate(ctx, {"min_score": 55, "breakout_min_volume_ratio": 2.0})
    if sig:
        assert sig.metadata["entry_mode"] == "BREAKOUT"

# ── Layer 0: 防御链 ──

def test_flash_crash_blocked():
    """闪崩检测: slope_5m < -2% + volume_ratio > 2.0 → 拦截"""
    ctx = _make_ctx(rsi=38, slope_5m=-3.0, volume_ratio=3.0,
                    volume=30_000_000, avg_volume=10_000_000)
    assert evaluate(ctx, {"min_score": 55}) is None

def test_zero_rsi_blocked():
    ctx = _make_ctx(rsi=0)
    assert evaluate(ctx, {"min_score": 55}) is None

def test_overheat_penalty():
    ctx = _make_ctx(metadata={
        "sector_info": {"name": "Semiconductors", "score": 90,
                       "leaders": ["NVDA"], "last_update": "2026-05-30T10:00:00"}
    })
    sig = evaluate(ctx, {"min_score": 55, "overheat_threshold": 80, "overheat_penalty": 0.8})
    if sig:
        assert sig.strength < 100  # 被降权

# ── v3: SymbolContext 字段使用 ──

def test_uses_ctx_market_regime():
    """确认使用 ctx.market_regime 而非 metadata hack"""
    ctx = _make_ctx(market_regime="TREND_UP")
    sig = evaluate(ctx, {"min_score": 55})
    assert sig is not None

def test_uses_ctx_volume_ratio():
    """确认 BREAKOUT 使用 ctx.volume_ratio"""
    ctx = _make_ctx(rsi=72, volume_ratio=2.5, volume=25_000_000, avg_volume=10_000_000)
    sig = evaluate(ctx, {"min_score": 55, "breakout_min_volume_ratio": 2.0})
    # volume_ratio=2.5 > 2.0, 应可触发 BREAKOUT
    if sig:
        assert sig.metadata["entry_mode"] == "BREAKOUT"

def test_uses_ctx_order_flow():
    """确认资金流评估使用 ctx.order_flow_imbalance"""
    ctx_strong = _make_ctx(order_flow_imbalance=0.2, ticker_buy_sell_ratio=1.3)
    ctx_weak = _make_ctx(order_flow_imbalance=-0.2, ticker_buy_sell_ratio=0.7)
    cfg = {"min_score": 55}
    sig_s = evaluate(ctx_strong, cfg)
    sig_w = evaluate(ctx_weak, cfg)
    if sig_s and sig_w:
        assert sig_s.strength >= sig_w.strength
```

### 8.2 集成测试

```python
def test_best_hit_selects_strongest():
    """trend_first score=90 vs technical score=60 → 选 trend_first"""

def test_best_hit_falls_back_to_technical():
    """trend_first 未命中 → 选 technical"""

def test_fast_lane_still_highest_priority():
    """fast_lane 命中 → 直接返回, 不评估 trend_first"""

def test_symbol_context_from_raw_maps_new_fields():
    """from_raw() 正确映射新增字段"""
    raw = {
        "price": 100, "market_regime": "TREND_UP", "trend": "UP",
        "volume_ratio": 1.8, "slope_5m": 0.5,
        "order_flow_imbalance": 0.12, "ticker_buy_sell_ratio": 1.15,
    }
    ctx = SymbolContext.from_raw("US.TEST", raw)
    assert ctx.market_regime == "TREND_UP"
    assert ctx.trend == "UP"
    assert ctx.volume_ratio == 1.8
    assert ctx.slope_5m == 0.5
    assert ctx.order_flow_imbalance == 0.12
```

---

## 9. 风险矩阵与应对

| # | 风险 | 概率 | 影响 | 应对 | 回滚方案 |
|---|------|------|------|------|---------|
| R1 | 板块数据盘后过期导致误判 | 高 | 中 | Phase 1 观测验证; 过期降权 | `sector_max_age_hours: 1` |
| R2 | 趋势拐点连续止损 | 中 | 高 | HIGH_VOL 自动暂停; ATR 止损兜底 | `min_score: 70` |
| R3 | Regime 判断频繁翻转 | 中 | 中 | _calc_market_regime 已有去抖; 持仓中重检 | 冻结 regime 30分钟 |
| R4 | best-hit 模式增加延迟 | 低 | 低 | 纯函数计算 <1ms | 回退 first-hit |
| R5 | 同板块集中度过高 | 中 | 高 | SectorLimitsChecker(40%); 策略内≤2只 | `max_positions_per_sector: 1` |
| R6 | order_flow_imbalance 不可靠(隐藏单) | 中 | 低 | v1 作辅助加分; Phase 3 用逐笔数据替代 | cf_modifier 固定 1.0 |
| R7 | 与 scalping 引擎冲突 | 低 | 中 | 独立引擎; 互不干扰 | 无 |
| R8 | SymbolContext 扩展不兼容 | 低 | 高 | 所有新字段有默认值; from_raw 用 .get() | git revert |

---

## 10. 回滚方案

### 10.1 最小回滚 (30秒)

```yaml
# unified_config.yaml
strategies:
  trend_first:
    enabled: false    # 一行关闭
```

### 10.2 代码回滚 (5分钟)

```bash
cd /opt/ftseek/FTSeek-US
git revert HEAD  # 回退 RFC-054 的 commit
systemctl restart ftseek-us
```

### 10.3 SymbolContext 扩展的兼容性

- 新增字段全部有默认值 (空字符串/0.0/1.0)
- 现有策略不使用这些字段，不受影响
- from_raw() 用 `context.get(key, default)` 安全读取
- 即使回滚 trend_first.py, SymbolContext 扩展也不需要回滚

---

## 11. 成功标准

### Phase 1 (观测) 通过条件:
- [ ] trend_first 日均信号 ≥ 3
- [ ] 板块数据准确率 ≥ 80%
- [ ] 无 ERROR 级异常
- [ ] BestHit 逻辑人工 review 通过

### Phase 2 (执行) 通过条件:
- [ ] 15笔交易后胜率 ≥ 40% (对比当前 30.3%)
- [ ] 无连续 5 笔亏损
- [ ] 龙头占比 ≥ 50%
- [ ] 平均持仓时间 1-4 小时

### 北极星贡献 (30天后):
- [ ] 系统整体胜率提升 ≥ 5pp (30.3% → 35%+)
- [ ] trend_first 子策略胜率 ≥ 50%
- [ ] 日均 PnL 转正

---

## 12. 文件变更清单

```
修改:
  app/core/pipeline_contracts.py       +15行 (SymbolContext 新字段 + from_raw 映射)
  app/pipeline/stages/signal_stage.py  +30行 (_inject_sector_info + best-hit 编排)
  app/strategy/pure/__init__.py        +2行 (import trend_first)
  config/unified_config.yaml           +30行 (strategies.trend_first)

新增:
  app/strategy/pure/trend_first.py     ~160行 (核心策略)
  tests/strategy/test_trend_first.py   ~120行 (单元测试)

不动:
  app/data/futu_lv3_provider.py        (数据已提供, 不需要修改)
  app/data/market_data_router.py       (数据已计算, 不需要修改)
  app/data/lv3_technical_calculator.py (指标已计算, 不需要修改)
  app/strategy/pure/technical.py       (保留, RANGE 市 fallback)
  app/strategy/pure/fast_lane.py       (保留, 最高优先级)
  app/strategy/pure/llm_vote.py        (保留, 盘前/盘后)
  app/risk/sector_leader.py            (保留, 只被调用)
  app/risk/sector_limits.py            (保留, 集中度保护)
  app/strategy/regime_adapter.py       (保留, 仓位缩放)
```

**总变更**: ~360行 (含测试), 2 新增 + 4 小改, 0 删除

---

## 13. 开放问题

### 架构层

| # | 问题 | 建议 | 需评审? |
|---|------|------|---------|
| Q1 | regime 统一: `_calc_market_regime()` (VIX+trend) vs RFC-054 提议 (SPY>MA200) — 用哪个? | 用现有的 `_calc_market_regime()`, 已在生产验证 | ✅ |
| Q2 | 板块数据是否走 SymbolContext? 还是保持策略层注入? | 保持策略层注入, 避免数据层复杂化 | ✅ |
| Q3 | observe_only 期间是否阻止 technical 执行? | 不阻止 — best-hit 并行 | 否 |
| Q4 | Phase 2 max_daily_trades 设多少? | 5笔 (保守) | 否 |
| Q5 | 是否允许 trend_first 隔夜持仓? | 不允许 — 保持当前约束 | 否 |

### 数据可靠性

| # | 问题 | 建议 | 需评审? |
|---|------|------|---------|
| Q6 | `order_flow_imbalance` 基于 Level 2 盘口, 美股隐藏单比例高, 是否可靠? | v1 作辅助加分, Phase 3 用逐笔数据 | ✅ |
| Q7 | `ctx.spy_price/spy_ma200` 更新频率? 实时 vs 盘前缓存? | MarketDataRouter 每次 get_full_market_context 都获取 SPY, 实时更新 | 否 |
| Q8 | 板块数据盘中不刷新是否风险过高? | v1 接受, 过期降权; Phase 3 再刷新 | ✅ |

### 策略层

| # | 问题 | 建议 | 需评审? |
|---|------|------|---------|
| Q9 | BREAKOUT 止损 1.0×ATR 在跳空高开时是否太紧? | 观察 Phase 1 数据, 可能需要放宽到 1.2×ATR | ✅ |
| Q10 | best-hit 是否会导致 LONG/SHORT 矛盾信号? | 记录日志分析; 如频繁发生, 改为 first-hit | ✅ |
| Q11 | SignalGate 对 trend_first 的兼容性? | trend_first strength=55-100, 不会触发 strength=0 拦截 | 否 |
| Q12 | ATR 止损参数是否被 ExitMonitor 消费? | 需要在 Phase 2 中对接 — 当前 ExitMonitor 的渐进式4档Tier已存在 | ✅ |

---

## 14. 附录: 代码审计发现的3个致命Bug

> 以下 Bug 在 v1 方案中存在, v3 方案已全部修正。记录在此供评审参考。

### Bug #1: `RegimeAdapter.get_current_regime()` 不存在

**v1 代码**: `adapter.get_current_regime()` → AttributeError → except → regime="RANGE" → Layer 1 拦截 → 策略永远不激活

**v3 修正**: 使用 `ctx.market_regime` (由 MarketDataRouter._calc_market_regime() 计算, 通过扩展后的 from_raw() 映射)

### Bug #2: `ctx.volume_ratio` 不存在于 SymbolContext

**v1 代码**: `getattr(ctx, 'volume_ratio', 1.0)` → 永远 1.0 → BREAKOUT 永不触发

**v3 修正**: 扩展 SymbolContext 新增 `volume_ratio` 字段, from_raw() 映射 LV3TechnicalCalculator 已计算的值

### Bug #3: `ctx.price_change_5min` 不存在于 SymbolContext

**v1 代码**: `getattr(ctx, 'price_change_5min', None)` → 永远 None → 闪崩检测无效

**v3 修正**: 使用 `ctx.slope_5m` (5分钟价格斜率, 由 MarketDataRouter 计算, 通过扩展后的 from_raw() 映射)

---

*— End of RFC-054 —*
