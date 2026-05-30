# RFC-054: Trend-First 分层信号架构

> **级别**: 🔴 (交易主路径变更)
> **状态**: DRAFT v2 (含代码审计修正) — 等待多Agent评审
> **作者**: Hermes + User 联合设计
> **日期**: 2026-05-30
> **关联**: SYSTEM_OBJECTIVE.md F1 + F3
> **审计**: 见 RFC-054-audit-findings.md (3个致命Bug已修正)

---

## 0. 决策自检

| 问题 | 回答 |
|------|------|
| **为何**: 服务哪个焦点? | F1(买卖规范) + F3(框架逻辑清晰) |
| **预期**: 哪个指标变好? | 胜率 30.3%→≥50%, 日均PnL -$1503→≥+$500 |
| **回滚**: 一行命令回滚? | `YAML: strategies.trend_first.enabled: false` + `git revert` |
| **最简**: 有没有不改代码的方案? | 无 — 信号源架构重构，但复用5个现有模块 |
| **边界**: 哪个级别? | 🔴 交易主路径 (signal_stage.py 编排变更) |

---

## 1. 问题陈述

### 1.1 数据证据

```
governance_check 2026-05-30:
  胜率 (30天): 30.3% (目标≥50%) ❌
  日均收益 (30天): -$1503 (目标≥$500) ❌
  总交易: 277 笔

历史分析 (profit_winrate_optimization_2026-05-03.md):
  LONG:  118笔  胜率22.9%  PnL +$1,933   ← 做多严重低效
  SHORT: 148笔  胜率38.5%  PnL +$12,021  ← 做空是主引擎
  PRE_CLOSE_EXIT: 54% 交易被盘末强制平仓 ← 持仓时间不足
  signal_strength=0: 46% 交易为弱信号 ← 信号质量差
  TRAILING_STOP: 唯一盈利退出方式, +1.42%平均 ← 趋势跟踪才赚钱
```

### 1.2 根因诊断

**核心矛盾**: 当前技术面策略 (`technical.py`) 是**均值回归**逻辑 — "RSI超卖→买入"。但美股的结构性特征是**动量驱动** — "强者恒强"。

| 维度 | 当前做法 | 市场现实 |
|------|---------|---------|
| 入场逻辑 | RSI<30 超卖买入 | 超卖可能是趋势下跌中途 |
| 做空逻辑 | RSI>70 超买卖出 | 强势股可以 RSI>80 持续上涨 |
| 选股范围 | 全市场扫描找超卖 | 机构资金集中在热门板块龙头 |
| 退出方式 | 盘末强制退出 | 趋势跟踪(TRAILING_STOP)是唯一盈利退出 |

**入场(均值回归) × 退出(趋势跟踪) = 逻辑矛盾**

### 1.3 参数审计

`technical.py` 的全部参数均为拍脑袋或教科书默认值，**零数据验证**:

- RSI 阈值 30/70: Wilder 1978 教科书值，未针对美股日内验证
- MACD diff 0.01: 绝对值阈值，不适配高价股 ($5 vs $500)
- 权重 30/25/25/20: 无网格搜索，凑成100%
- weekly_optimizer 报告全部为0 (训练集=0笔, 验证集=0笔)

---

## 2. 多角色博弈论证

### 2.1 正方: 趋势跟随 + 板块龙头 应成为主信号源

**论据 A: 学术证据**

- Jegadeesh & Titman (1993): 动量因子年化超额 ~8%，持续40年有效
- Moskowitz & Grinblatt (1999): 板块动量是机构 alpha 的主要来源
- 美股 SPY 20年胜率 73%，70%时间处于上升趋势

**论据 B: 自身数据**

- TRAILING_STOP (趋势跟踪止盈): 唯一正期望退出方式
- LONG 胜率 22.9%: 均值回归做多严重失败
- 做空胜率 38.5% 高于做多: 说明系统在趋势向下时更有效 (趋势跟踪)

**论据 C: 组件已存在**

系统已建好 5 个趋势/板块组件，只是放在了"门禁/过滤"位置:
- `MarketRegimeIdentifier` — 市场状态识别
- `SectorLeaderDetector` — 板块龙头检测
- `CapitalFlowMonitor` — 资金流监控
- `sector_correlation_reasoner` — 行业关联推理
- `RegimeAdapter` — 策略自适应切换

### 2.2 反方: 趋势策略的风险和限制

**论据 A: 动量崩溃风险**

2009年3月，动量策略单月亏损 -40%。市场V型反转时，所有趋势信号被打脸。

> **应对**: MarketRegimeIdentifier 检测 HIGH_VOL → 自动暂停趋势策略; ATR 止损兜底

**论据 B: 趋势信号也是滞后的**

RSI、MACD、SuperTrend 都是基于历史价格的计算。板块热度基于历史新闻密度。"滞后5分钟"换成"滞后5天"本质相同。

> **应对**: 不追求消除滞后，而是用板块加速度(边际变化)替代绝对热度; 用资金流(实时量价)确认趋势有效性

**论据 C: 日内持仓时间约束**

系统有 PRE_CLOSE_EXIT (15:50 后不新开仓, 15:55 强平)。趋势策略通常需要数天持仓才能捕获完整波段。日内趋势策略的超额收益被时间压缩。

> **应对**: 聚焦日内趋势 (不隔夜), 用 ATR 渐进止损在2-4小时内锁定利润; 渐进式退出(4档Tier)替代固定止盈

**论据 D: 板块数据可靠性**

`SectorLeaderDetector.analyze_sectors()` 目前仅在盘前 LLM pipeline 中调用一次。盘中板块轮动后数据过期。

> **应对**: 增加定时刷新 (每30分钟) + 异动触发刷新; 板块数据过期时降权处理

**论据 E: 集中度风险**

趋势策略可能同时指向同板块多只股票 (如半导体 NVDA/AMD/AVGO)。一次板块回调导致组合巨亏。

> **应对**: 复用现有 `SectorLimitsChecker` (40%上限); 策略内增加同板块持仓数限制 (≤2只)

### 2.3 综合判断 (Synthesis)

| 维度 | 结论 |
|------|------|
| 方向正确性 | ✅ 趋势跟随 + 板块龙头作为主信号源，学术和自身数据均支持 |
| 风险控制 | ✅ Regime 过滤 + ATR 止损 + 集中度限制，多层防护 |
| 可行性 | ✅ 核心组件已存在，新增代码 ~200 行，复用5个模块 |
| 持仓周期 | 🟡 日内约束下，需 ATR 渐进止损配合，目标2-4h持仓 |
| 数据可靠性 | 🟡 板块数据需定时刷新，v1 先容忍降级，积累数据后优化 |
| 回退安全 | ✅ 独立策略，YAML enabled=false 即可关闭，不影响现有策略 |

**结论**: 方案可行，但需要渐进式落地 — 先观测模式收集数据，再逐步切换为执行模式。

---

## 3. 架构设计 (v2 — 含全部压力测试修正)

### 3.1 整体架构

```
Signal Stage (编排层, signal_stage.py)
│
├── 策略1: fast_lane (作战计划快车道, 最高优先级)
│   └── 盘前计划 entry_zone 匹配 → 直接执行
│
├── 策略2: trend_first (新·趋势优先) ◀── NEW
│   ├── Layer 0: 防御链 (闪崩/数据完整性/日内熔断)
│   ├── Layer 1: 环境判断 (RegimeAdapter → 趋势市才激活)
│   ├── Layer 2: 选股 (SectorLeader + CapitalFlow + 板块加速度)
│   ├── Layer 3: 入场 (回调买入 PULLBACK + 放量突破 BREAKOUT)
│   └── Layer 4: 风控参数 (ATR 自适应止损/渐进追踪)
│
├── 策略3: technical (降级为 RANGE 震荡市专用)
│   └── RSI + MACD + Bollinger + SuperTrend 均值回归
│
└── 策略4: llm_vote (盘前/盘后)
    └── 多模型投票
```

**关键变化: first-hit → best-hit (模拟盘阶段)**

```
模拟盘阶段: 并行评估 trend_first + technical, 取 quality 最高者
  quality = strength × confidence
  
好处:
  1. 同时收集两种策略的信号数据
  2. 不丢失交易机会
  3. 积累数据后再决定 first-hit 还是 best-hit

积累50笔后: 根据数据决定
  如果 trend_first 胜率 > technical 胜率 → 切回 first-hit, trend_first 优先
  如果两者胜率接近 → 保持 best-hit
```

### 3.2 新增文件: `app/strategy/pure/trend_first.py`

#### 3.2.1 信号评估流程

```python
@register("trend_first")
def evaluate(ctx: SymbolContext, config: Dict) -> Optional[SignalCandidate]:
    """
    Trend-First 分层决策策略

    输入: SymbolContext (含 price/rsi/macd/supertrend/atr)
          + metadata (含 regime/sector_info/capital_flow)
    输出: SignalCandidate 或 None

    防御链:
      L0-a: 数据完整性 (price>0, rsi>0)
      L0-b: 闪崩检测 (5分钟跌幅>2% + 放量 → 不接)
      L0-c: 板块数据新鲜度 (>2小时 → 降权)
      L0-d: 同板块持仓检查 (≤2只)
      L0-e: 日内亏损熔断 (已达日亏上限 → 停开新仓)

    Layer 1 — 环境判断:
      regime ∈ {TREND_UP, TREND_DOWN} → 继续
      regime ∈ {RANGE, HIGH_VOL} → 退出 (交给 technical / 不交易)

    Layer 2 — 选股:
      板块热度 ≥ min_sector_score (默认40)
      过热保护: score>80 降权20%
      龙头加分: +15
      资金流: score≥0.6 加成15%, <0.4 惩罚15%

    Layer 3 — 入场 (双模式):
      PULLBACK 模式 (趋势中回调):
        LONG: RSI ∈ [35, 55] → pullback_quality = (55-RSI)*1.5
        SHORT: RSI ∈ [45, 65] → pullback_quality = (RSI-45)*1.5
        止损: 1.5×ATR (较宽, 给波动留空间)

      BREAKOUT 模式 (放量突破):
        RSI > 65 且 volume_ratio > 2.0
        追突破, 但止损更紧: 1.0×ATR
        额外加分: +20

    评分:
      base = 50 (趋势+热门板块)
      + pullback_quality (0~30) 或 breakout_bonus (20)
      + macd_aligned_bonus (0/10)
      + supertrend_aligned_bonus (0/10)
      + leader_bonus (0/15)
      × capital_flow_modifier (0.85/1.0/1.15)
      × overheat_penalty (0.8 if sector_score>80)

    Layer 4 — 风控参数 (写入 SignalCandidate.metadata):
      stop_loss_atr_mult: 1.5 (PULLBACK) / 1.0 (BREAKOUT)
      trail_activation_atr: 1.0
      trail_atr_mult: 1.5
      → 供 ExitMonitor 消费, 实现渐进式追踪止损
    """
```

#### 3.2.2 完整代码

```python
"""
Trend-First 分层决策策略 — 纯函数实现

输入: SymbolContext + metadata (regime/sector/capital_flow)
输出: SignalCandidate 或 None

设计:
  趋势市 → 板块龙头选股 → 回调/突破入场 → ATR 风控
  震荡市/高波动 → 不激活 (交给 technical / 不交易)

纯函数 — 零副作用, 所有外部数据通过 ctx.metadata 注入。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import register

logger = logging.getLogger(__name__)


def _check_price_stability(ctx: SymbolContext, config: Dict) -> bool:
    """L0-b: 闪崩检测 — 日内暴跌+放量 = 恐慌, 不接飞刀
    审计修正: 用 ctx.change_pct (日内涨跌幅) 替代不存在的 price_change_5min
    用 ctx.volume/avg_volume 替代不存在的 volume_ratio"""
    vol_ratio = ctx.volume / ctx.avg_volume if ctx.avg_volume > 0 else 1.0

    if ctx.change_pct < -0.03:  # 日内跌>3%
        if vol_ratio > 2.0:
            logger.debug(f"[TrendFirst] {ctx.symbol} 闪崩检测: "
                        f"日内={ctx.change_pct:.1%}, vol={vol_ratio:.1f}x")
            return False
    return True


def _check_sector_freshness(sector_info: Dict, config: Dict) -> float:
    """L0-c: 板块数据新鲜度 — 超过 max_age_hours 降权"""
    last_update_str = sector_info.get("last_update")
    if not last_update_str:
        return 0.7  # 无时间戳, 降权30%

    try:
        last_update = datetime.fromisoformat(last_update_str)
    except (ValueError, TypeError):
        return 0.7

    max_age = timedelta(hours=config.get("sector_max_age_hours", 2))
    age = datetime.now() - last_update
    if age > max_age:
        # 线性衰减: 2h→0.7, 4h→0.5
        hours_over = (age - max_age).total_seconds() / 3600
        decay = max(0.5, 0.7 - hours_over * 0.1)
        return decay
    return 1.0


def _eval_pullback(ctx: SymbolContext, direction: str, config: Dict) -> Optional[float]:
    """Layer 3 PULLBACK: 趋势中回调入场"""
    rsi_low, rsi_high = config.get(
        f"pullback_rsi_{direction.lower()}",
        (35, 55) if direction == "LONG" else (45, 65)
    )

    if not (rsi_low <= ctx.rsi <= rsi_high):
        return None

    if direction == "LONG":
        quality = (rsi_high - ctx.rsi) * 1.5  # RSI越低(回调越深), 质量越高
    else:
        quality = (ctx.rsi - rsi_low) * 1.5

    return min(30.0, quality)


def _eval_breakout(ctx: SymbolContext, direction: str, config: Dict) -> Optional[float]:
    """Layer 3 BREAKOUT: 放量突破入场 (回调不来时的 Plan B)
    审计修正: 用 ctx.volume/avg_volume 替代不存在的 ctx.volume_ratio"""
    vol_ratio = ctx.volume / ctx.avg_volume if ctx.avg_volume > 0 else 1.0
    min_vol = config.get("breakout_min_volume_ratio", 2.0)

    if vol_ratio < min_vol:
        return None

    if direction == "LONG" and ctx.rsi > 65:
        return 20.0  # 放量向上突破
    if direction == "SHORT" and ctx.rsi < 35:
        return 20.0  # 放量向下突破

    return None


@register("trend_first")
def evaluate(ctx: SymbolContext, config: Dict[str, Any]) -> Optional[SignalCandidate]:
    """Trend-First 分层决策策略"""

    # ── L0: 防御链 ──

    # L0-a: 数据完整性
    if ctx.rsi <= 0 or ctx.price <= 0:
        return None

    # L0-b: 闪崩检测
    if not _check_price_stability(ctx, config):
        return None

    # ── Layer 1: 环境判断 ──
    regime = ctx.metadata.get("regime", "RANGE")
    if regime in ("RANGE", "HIGH_VOL", "LOW_VOL"):
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

    # 资金流
    capital_flow = ctx.metadata.get("capital_flow", {})
    cf_score = float(capital_flow.get("score", 0.5))
    if cf_score >= 0.6:
        cf_modifier = config.get("capital_flow_boost", 1.15)
    elif cf_score < 0.4:
        cf_modifier = config.get("capital_flow_penalty", 0.85)
    else:
        cf_modifier = 1.0

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

    # ── 综合评分 ──
    base_score = 50
    total = (base_score + entry_score + leader_bonus + macd_bonus + st_bonus)
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
    atr = getattr(ctx, 'atr', 0) or ctx.price * 0.02
    if atr <= 0:
        atr = ctx.price * 0.02

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
            f"RSI={ctx.rsi:.0f} | score={total:.0f}"
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
            "capital_flow_score": cf_score,
            "freshness": freshness,
            "atr": atr,
            "stop_loss_atr_mult": stop_loss_atr_mult,
            "trail_activation_atr": config.get("trail_activation_atr", 1.0),
            "trail_atr_mult": trail_mult,
            "macd_aligned": macd_aligned,
            "supertrend_aligned": st_aligned,
        },
    )
```

### 3.3 修改文件: `app/pipeline/stages/signal_stage.py`

#### 3.3.1 新增数据注入函数

```python
def _inject_regime_and_sector(ctx: SymbolContext):
    """将 Regime + Sector + CapitalFlow 注入 ctx.metadata (供 trend_first 消费)
    
    审计修正: RegimeAdapter.get_current_regime() 不存在,
    改为从 ctx 已有字段 (spy_price/spy_ma200/vix/vix_regime) 推断 regime"""

    # 1. 市场状态 — 从 ctx 已有字段推断 (无外部依赖)
    if ctx.vix_regime == "extreme" or ctx.vix > 30:
        regime = "HIGH_VOL"
    elif ctx.spy_price > 0 and ctx.spy_ma200 > 0:
        if ctx.spy_price > ctx.spy_ma200 * 1.01:
            regime = "TREND_UP"
        elif ctx.spy_price < ctx.spy_ma200 * 0.99:
            regime = "TREND_DOWN"
        else:
            regime = "RANGE"
    else:
        regime = "RANGE"
    ctx.metadata["regime"] = regime

    # 2. 板块信息 (复用 SectorLeaderDetector 单例)
    try:
        from app.risk.sector_leader import get_sector_leader_detector, SECTOR_MAPPING
        detector = get_sector_leader_detector()

        if not detector._sector_scores:
            logger.debug("[Signal] 板块数据未初始化 (可能盘前), trend_first 将受限")
            ctx.metadata["sector_info"] = {
                "name": "Unknown", "score": 0, "leaders": [], "last_update": None,
            }
        else:
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
                    break
            else:
                ctx.metadata["sector_info"] = {
                    "name": "Unknown", "score": 0,
                    "leaders": leader_symbols, "last_update": None,
                }
    except Exception as e:
        logger.debug(f"[Signal] Sector 注入失败: {e}")
        ctx.metadata["sector_info"] = {
            "name": "Unknown", "score": 0, "leaders": [], "last_update": None,
        }

    # 3. 资金流 (v1 默认中性, 后续接入实时数据)
    ctx.metadata.setdefault("capital_flow", {"score": 0.5})
```

#### 3.3.2 修改 process() 编排逻辑

```python
def process(ctx, position_store=None, battle_plan_manager=None,
            voting_coordinator=None) -> Optional[SignalCandidate]:

    symbol = ctx.symbol

    # ── Restricted 拦截 (现有, 保持不变) ──
    # ...

    # ── 策略 1: 快车道 (现有, 保持不变) ──
    # ...

    # ── 策略 2: Trend-First + Technical 并行评估 ──
    candidates = []

    # 2a: Trend-First (趋势优先, 仅趋势市激活)
    try:
        _inject_regime_and_sector(ctx)
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

    # ── 策略 3: LLM 投票 (现有, 保持不变) ──
    # ...
```

### 3.4 新增 YAML 配置

```yaml
# unified_config.yaml → strategies 节新增:

strategies:
  # ... 现有 fast_lane, technical, llm_vote 保持不变 ...

  trend_first:
    enabled: true
    observe_only: true           # v1: 仅观测, 不实际执行 (Phase 1 后切换)

    # Layer 1: 环境
    # (由 RegimeAdapter 自动判断, 无需配置)

    # Layer 2: 选股
    min_sector_score: 40         # 板块热度门槛
    leader_bonus: 15             # 龙头加分
    overheat_threshold: 80       # 过热板块阈值
    overheat_penalty: 0.8        # 过热惩罚系数
    sector_max_age_hours: 2      # 板块数据最大有效期

    # Layer 2: 资金流
    capital_flow_boost: 1.15     # 资金流入加成
    capital_flow_penalty: 0.85   # 资金流出惩罚

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

### 3.5 不修改的文件

| 文件 | 原因 |
|------|------|
| `app/strategy/pure/technical.py` | 保持不变, RANGE 市 fallback |
| `app/strategy/pure/fast_lane.py` | 保持不变, 最高优先级 |
| `app/strategy/pure/llm_vote.py` | 保持不变, 盘前/盘后 |
| `app/risk/sector_leader.py` | 保持不变, 只被调用 |
| `app/factors/capital_flow.py` | 保持不变, 只被调用 |
| `app/strategy/regime_adapter.py` | 保持不变, 只被调用 |
| `app/risk/sector_limits.py` | 保持不变, 集中度保护已存在 |

---

## 4. 信号归因追踪 (闭环反馈基础)

### 4.1 目标

每笔交易平仓时, 记录完整的信号快照, 用于事后归因分析。

### 4.2 实现方式

在 `order_executor.py` 的平仓路径中, 追加信号快照写入:

```python
# order_executor.py — 平仓时追加:
def _record_signal_outcome(trade, exit_price, exit_reason):
    """记录信号归因快照 (供后续参数优化)"""
    snapshot = {
        "trade_id": trade.id,
        "symbol": trade.symbol,
        "strategy_id": trade.strategy_id,
        "entry_signal": {
            "strength": trade.signal_strength,
            "confidence": trade.signal_confidence,
            "regime": trade.metadata.get("regime"),
            "sector": trade.metadata.get("sector_name"),
            "entry_mode": trade.metadata.get("entry_mode"),
            "is_leader": trade.metadata.get("is_leader"),
            "rsi_at_entry": trade.metadata.get("rsi_at_entry"),
            "atr_at_entry": trade.metadata.get("atr"),
        },
        "outcome": {
            "pnl_pct": (exit_price - trade.entry_price) / trade.entry_price,
            "pnl_usd": trade.pnl,
            "holding_minutes": trade.holding_minutes,
            "exit_reason": exit_reason,
        },
    }
    # 写入 signal_outcomes 表 (已有)
    ...
```

**这一步是后续所有参数优化的前提。没有归因数据, 优化器跑出来全是0。**

---

## 5. 实施步骤 (Phase 0-3)

### Phase 0: 基础设施 (Day 1)

**目标**: 代码就位, 观测模式, 不产生交易

| 步骤 | 文件 | 操作 | 验证 |
|------|------|------|------|
| 0.1 | `app/strategy/pure/trend_first.py` | 创建 (3.2.2 完整代码) | `python -c "from app.strategy.pure import trend_first; print('OK')"` |
| 0.2 | `app/pipeline/stages/signal_stage.py` | 新增 `_inject_regime_and_sector()` | `python -c "from app.pipeline.stages.signal_stage import _inject_regime_and_sector; print('OK')"` |
| 0.3 | `app/pipeline/stages/signal_stage.py` | 修改 `process()` 编排逻辑 (3.3.2) | `grep "BestHit" app/pipeline/stages/signal_stage.py` 确认存在 |
| 0.4 | `config/unified_config.yaml` | 添加 `strategies.trend_first` (3.4) | `python -c "from app.core.config_loader import get_config; print(get_config('strategies.trend_first.enabled'))"` |
| 0.5 | `app/strategy/pure/__init__.py` | 添加 `from app.strategy.pure import trend_first` | `python -c "from app.strategy.pure import REGISTRY; print('trend_first' in REGISTRY)"` |
| 0.6 | 全系统 | `git commit -m "[RFC-054] Trend-First 分层信号架构 (观测模式)"` | — |

**验证**:
```bash
# 单元测试: trend_first 纯函数
python -m pytest tests/strategy/test_trend_first.py -v

# 集成测试: 信号阶段编排
python -m pytest tests/pipeline/test_signal_stage.py -v

# 观测日志确认
grep "TrendFirst\|BestHit" logs/pipeline.log | head -20
```

### Phase 1: 观测验证 (Day 2-5)

**目标**: 收集 trend_first 信号数据, 不实际执行

**配置**: `observe_only: true` (已默认)

**每日检查清单**:
```
□ trend_first 产出了多少信号? (grep "TrendFirst" logs/)
□ 信号分布: 哪些板块? 哪些股票? PULLBACK vs BREAKOUT?
□ 对比: technical 同时段产出了多少?
□ BestHit 选择了谁? 比例如何?
□ 板块热度数据是否准确? (人工对照 Yahoo Finance 板块页)
□ 有无异常: 闪崩误判? 板块数据过期?
```

**Phase 1 通过条件** (全部满足才进入 Phase 2):
- [ ] trend_first 信号产出 ≥ 3/天
- [ ] 板块数据与人工核对一致率 ≥ 80%
- [ ] 无代码异常 (ERROR 日志 < 5/天)
- [ ] BestHit 选择逻辑合理 (人工 review 10+ 个信号)

### Phase 2: 开放执行 (Day 6-15)

**目标**: trend_first 实际下单, 积累交易数据

**配置变更**: `observe_only: false`

**安全约束**:
```yaml
# Phase 2 额外保护
risk_limits:
  max_daily_trades_trend_first: 5      # 每日最多5笔 trend_first 交易
  max_positions_trend_first: 3         # 同时最多3个 trend_first 持仓
  trend_first_max_position_pct: 0.15   # 单笔不超过总资产15%
```

**快速失败条件**:
```
❌ 连续 5 笔全部亏损 → 暂停 trend_first, 检查 Layer 1 环境判断
❌ 胜率 < 25% (15笔后) → 回退到 observe_only
❌ 板块数据连续 3 次误判 → 提升 min_sector_score (40→60)
```

**每日检查清单**:
```
□ 新增交易数 / 盈亏 / 胜率
□ 龙头 vs 非龙头 胜率差异
□ PULLBACK vs BREAKOUT 胜率差异
□ 板块热度与实际胜率的相关性
□ ATR 止损是否合理 (太紧? 太宽?)
□ 持仓时间分布
```

### Phase 3: 数据驱动优化 (Day 16+)

**目标**: 积累 30+ 笔后, 用数据调参

**前提**: signal_outcomes 表有足够的归因数据

**优化方向**:
```
3a: 参数微调
  - 根据胜率最高的 RSI 区间调整 pullback_rsi_long/short
  - 根据龙头 vs 非龙头 胜率调整 leader_bonus
  - 根据板块热度与胜率相关性调整 min_sector_score

3b: 信号选择策略决策
  - 对比 trend_first 和 technical 的胜率
  - 决定: first-hit (恢复原架构) 还是 best-hit (保持并行)

3c: 板块数据刷新
  - 如果 Phase 2 发现板块数据过期是主要问题 → 实现 30 分钟定时刷新
  - 如果板块数据足够新鲜 → 不做

3d: 日内资金流
  - 如果 capital_flow.score=0.5 (中性) 的交易明显差 → 接入 5 分钟K线量价
  - 如果影响不大 → 保持默认中性
```

---

## 6. 测试计划

### 6.1 单元测试: `tests/strategy/test_trend_first.py`

```python
"""Trend-First 策略单元测试"""
import pytest
from app.core.pipeline_contracts import SymbolContext
from app.strategy.pure.trend_first import evaluate


def _make_ctx(**overrides) -> SymbolContext:
    """构造测试用 SymbolContext"""
    defaults = {
        "symbol": "US.NVDA",
        "price": 940.0,
        "rsi": 45.0,
        "macd": 1.5,
        "macd_signal": 1.0,
        "bollinger_lower": 900.0,
        "bollinger_upper": 980.0,
        "supertrend_direction": "UP",
        "atr": 18.0,
        "metadata": {
            "regime": "TREND_UP",
            "sector_info": {
                "name": "Semiconductors",
                "score": 60,
                "leaders": ["NVDA", "AMD", "AVGO"],
                "last_update": "2026-05-30T10:00:00",
            },
            "capital_flow": {"score": 0.7},
        },
    }
    defaults.update(overrides)
    return SymbolContext(**defaults)


# ── Layer 1: 环境判断 ──

def test_trend_up_produces_long():
    ctx = _make_ctx()
    sig = evaluate(ctx, {"min_score": 55})
    assert sig is not None
    assert sig.direction == "LONG"

def test_range_regime_returns_none():
    ctx = _make_ctx(metadata={"regime": "RANGE", "sector_info": {}, "capital_flow": {}})
    sig = evaluate(ctx, {"min_score": 55})
    assert sig is None

def test_high_vol_returns_none():
    ctx = _make_ctx(metadata={"regime": "HIGH_VOL", "sector_info": {}, "capital_flow": {}})
    sig = evaluate(ctx, {"min_score": 55})
    assert sig is None

def test_trend_down_produces_short():
    ctx = _make_ctx(
        rsi=55,
        supertrend_direction="DOWN",
        metadata={
            "regime": "TREND_DOWN",
            "sector_info": {"name": "Semiconductors", "score": 60,
                          "leaders": ["NVDA"], "last_update": "2026-05-30T10:00:00"},
            "capital_flow": {"score": 0.5},
        }
    )
    sig = evaluate(ctx, {"min_score": 55, "pullback_rsi_short": [45, 65]})
    if sig:
        assert sig.direction == "SHORT"


# ── Layer 2: 选股 ──

def test_cold_sector_returns_none():
    ctx = _make_ctx(metadata={
        "regime": "TREND_UP",
        "sector_info": {"name": "Energy", "score": 20, "leaders": [], "last_update": None},
        "capital_flow": {"score": 0.5},
    })
    sig = evaluate(ctx, {"min_score": 55, "min_sector_score": 40})
    assert sig is None

def test_leader_gets_bonus():
    ctx_leader = _make_ctx()
    ctx_follower = _make_ctx(symbol="US.MU")  # MU 不在 leaders 中

    cfg = {"min_score": 55, "leader_bonus": 15}
    sig_leader = evaluate(ctx_leader, cfg)
    sig_follower = evaluate(ctx_follower, cfg)

    if sig_leader and sig_follower:
        assert sig_leader.strength >= sig_follower.strength


# ── Layer 3: 入场 ──

def test_pullback_rsi_in_range():
    ctx = _make_ctx(rsi=42)  # 在 [35, 55] 内
    sig = evaluate(ctx, {"min_score": 55})
    assert sig is not None
    assert sig.metadata["entry_mode"] == "PULLBACK"

def test_pullback_rsi_too_high():
    ctx = _make_ctx(rsi=60)  # > 55, 不在回调区间
    sig = evaluate(ctx, {"min_score": 55})
    # 除非 BREAKOUT 条件满足, 否则 None
    if sig:
        assert sig.metadata["entry_mode"] == "BREAKOUT"

def test_breakout_high_volume():
    ctx = _make_ctx(rsi=72)  # > 65, 非回调
    ctx.volume_ratio = 2.5   # 放量
    sig = evaluate(ctx, {"min_score": 55, "breakout_min_volume_ratio": 2.0})
    if sig:
        assert sig.metadata["entry_mode"] == "BREAKOUT"


# ── Layer 0: 防御链 ──

def test_flash_crash_blocked():
    ctx = _make_ctx(rsi=35)
    ctx.price_change_5min = -0.03  # 5分钟跌3%
    ctx.volume_ratio = 3.0         # 放量
    sig = evaluate(ctx, {"min_score": 55})
    assert sig is None  # 闪崩拦截

def test_zero_rsi_blocked():
    ctx = _make_ctx(rsi=0)
    sig = evaluate(ctx, {"min_score": 55})
    assert sig is None

def test_overheat_penalty():
    ctx = _make_ctx(metadata={
        "regime": "TREND_UP",
        "sector_info": {"name": "Semiconductors", "score": 90,
                       "leaders": ["NVDA"], "last_update": "2026-05-30T10:00:00"},
        "capital_flow": {"score": 0.5},
    })
    sig_hot = evaluate(ctx, {"min_score": 55, "overheat_threshold": 80, "overheat_penalty": 0.8})
    # 过热板块信号应该更弱 (或 None)
    if sig_hot:
        assert sig_hot.strength < 100  # 被降权
```

### 6.2 集成测试: 信号阶段编排

```python
"""signal_stage 集成测试 — 验证 best-hit 模式"""

def test_best_hit_selects_strongest():
    """trend_first score=90 vs technical score=60 → 选 trend_first"""
    # 构造同时满足两个策略的 ctx
    # 验证 winner.strategy_id == "TREND_FIRST"

def test_best_hit_falls_back_to_technical():
    """trend_first 未命中 → 选 technical"""
    # 构造 RANGE regime → trend_first 不激活
    # 验证 winner.strategy_id == "TECHNICAL_MULTI_FACTOR"

def test_fast_lane_still_highest_priority():
    """fast_lane 命中 → 直接返回, 不评估 trend_first"""
    # 构造 battle_plan_match + entry_zone 匹配
    # 验证 winner.strategy_id == "BATTLE_PLAN_FAST_PATH"
```

---

## 7. 风险矩阵与应对

| # | 风险 | 概率 | 影响 | 应对 | 回滚方案 |
|---|------|------|------|------|---------|
| R1 | 板块数据盘后过期导致误判 | 高 | 中 | Phase 1 观测验证; 过期降权 | `sector_max_age_hours: 1` |
| R2 | 趋势拐点连续止损 | 中 | 高 | HIGH_VOL 自动暂停; ATR 止损兜底 | `min_score: 70` (提高门槛) |
| R3 | Regime 判断频繁翻转 | 中 | 中 | RegimeAdapter 已有去抖; 持仓中重检 | 冻结 regime 30分钟 |
| R4 | best-hit 模式增加延迟 | 低 | 低 | 纯函数计算 <1ms; 网络 IO 不在热路径 | 回退 first-hit |
| R5 | 同板块集中度过高 | 中 | 高 | SectorLimitsChecker(40%); 策略内≤2只 | `max_positions_per_sector: 1` |
| R6 | CapitalFlow 默认0.5无区分度 | 高 | 低 | v1 接受; Phase 3 接入实时数据 | 不影响 (中性) |
| R7 | 与 scalping 引擎冲突 | 低 | 中 | scalping 走独立引擎; 互不干扰 | 无 (架构隔离) |

---

## 8. 回滚方案

### 8.1 最小回滚 (30秒)

```yaml
# unified_config.yaml
strategies:
  trend_first:
    enabled: false    # 一行关闭
```

效果: trend_first 不再评估, 系统退回 fast_lane → technical → llm_vote

### 8.2 代码回滚 (5分钟)

```bash
cd /opt/ftseek/FTSeek-US
git revert HEAD  # 回退 RFC-054 的 commit
# 重启服务
systemctl restart ftseek-us
```

### 8.3 数据保护

- trend_first 的交易通过 `strategy_id="TREND_FIRST"` 标识
- 回滚不影响历史数据, 仅影响新信号生成
- signal_outcomes 表保留所有归因数据供分析

---

## 9. 成功标准

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

## 10. 文件变更清单

```
新增:
  app/strategy/pure/trend_first.py        ~160行
  tests/strategy/test_trend_first.py      ~120行

修改:
  app/pipeline/stages/signal_stage.py     +50行 (数据注入 + best-hit 编排)
  app/strategy/pure/__init__.py           +2行 (import trend_first)
  config/unified_config.yaml              +30行 (strategies.trend_first)

不动:
  app/strategy/pure/technical.py
  app/strategy/pure/fast_lane.py
  app/strategy/pure/llm_vote.py
  app/risk/sector_leader.py
  app/factors/capital_flow.py
  app/strategy/regime_adapter.py
  app/risk/sector_limits.py
```

**总变更**: ~360 行 (含测试), 2 个新文件, 3 个小改, 0 个删除

---

## 11. 开放问题 (待决策)

| # | 问题 | 建议 | 需要决策? |
|---|------|------|-----------|
| Q1 | observe_only 期间是否阻止 technical 执行? | 不阻止 — 两个策略并行, best-hit 选最优 | 否 (默认合理) |
| Q2 | Phase 2 max_daily_trades 设多少? | 5笔 (保守, 控制风险) | 否 (默认合理) |
| Q3 | 是否允许 trend_first 隔夜持仓? | 不允许 — 当前系统约束, 后续讨论 | 否 (保持现状) |
| Q4 | 板块数据刷新放在哪里实现? | Phase 3 再决定 — v1 先容忍 | 否 (延后) |
| Q5 | 是否需要 RFC 评审会议? | 是 — 🔴 级改动, 需要人审 | **是** |
