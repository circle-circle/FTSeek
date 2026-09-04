# RFC-075: 独立 ORB 辅线 (Opening Range Breakout Auxiliary Pipeline)

> **服务焦点**: F1 (买卖规范和止损止盈策略)
> **改什么指标**: 增加交易频率 + 利用 10:00-11:00 盈利时段窗口
> **验证方式**: shadow_only 模式先跑 2 周, 对比 shadow vs 主线表现
> **最坏情况**: shadow 模式不实际下单, 零风险
> **回滚**: 改 `orb.enabled: false` 重启

## 1. 设计原理

### 为什么需要独立管线 (不是扩展现有 ORB)

| 维度 | 现有 ORB (嵌入主线) | 独立 ORB 辅线 |
|------|---------------------|---------------|
| 扫描频率 | 10min (太慢, 错过突破) | **5s (捕捉实时突破)** |
| 标的池 | 与主线重叠 (TSLA/ORCL) | **ETF 专属 (SPY/QQQ/IWM/XLE/XLF)** |
| 防御层 | 12 层 (信号穿透率≈0) | **3 层 (区间宽度+量能+时间)** |
| 退出逻辑 | 走主线 exit_policy (复杂) | **独立 3 层退出 (简洁)** |
| 归因 | 混在 main 里 | **独立 "orb" bucket** |
| 历史交易 | **0 笔** | 目标: 日均 2-4 笔 |

### 数据支撑

- 10:00-11:00 ET 时段: **+$18,934** (系统唯一净正时段)
- 剔除 ORCL 单笔后: +$1,051 (edge 薄但存在)
- ETF 特征: 高流动性, 无财报风险, ATR 稳定, 开盘区间形态清晰

## 2. 架构设计

```
┌─ Futu LV3 (共享) ─────────────────────────────────────┐
│   1-min klines + snapshots                            │
└──┬──────────────────────────┬─────────────────────────┘
   │                          │
   ▼                          ▼
┌─ 主线 Pipeline ──┐   ┌─ ORB 辅线 Engine ──────────────┐
│ 10-min scan      │   │ 5-sec poll (09:30-11:00 ET)   │
│ TSLA/ORCL/ARM... │   │ SPY/QQQ/IWM/XLE/XLF (ETF)    │
│ 5 策略评估       │   │ 3 层防御 (宽度+量能+时间)      │
│ 主线 exit_policy │   │ 独立退出 (trail+floor+EOD)     │
│ owner=main       │   │ owner=orb                      │
└──┬───────────────┘   └──┬────────────────────────────┘
   │                      │
   └──────┬───────────────┘
          ▼
  _open_position_lock (RLock 共享)
  PositionStore (共享, 线程安全)
  ExecutionEngine (共享)
```

## 3. 模块清单 (6 文件, ~800 行)

### 新增文件

```
app/orb/
├── __init__.py           # 模块导出 + get_orb_engine() 单例
├── engine.py             # ORBEngine 主循环 (daemon thread, 5s poll)
├── exits.py              # ORBTrailingExit + ORBFloorStop + ORBEODExit
└── config.py             # ORBConfig 加载 (从 unified_config.yaml)
```

### 修改文件

```
config/unified_config.yaml          # 新增 orb 配置段
app/core/scheduler_core.py          # ORB 引擎初始化/启动/停止 (~20行)
```

## 4. 各模块详细设计

### 4.1 app/orb/engine.py — ORBEngine

```python
class ORBEngine:
    """独立 ORB 引擎 — 5秒轮询, ETF 专属标的池"""
    
    # 核心参数 (从 unified_config.yaml::orb 加载)
    POLLING_INTERVAL = 5          # 秒
    RANGE_MINUTES = 15            # 开盘区间长度
    ENTRY_WINDOW = (09:45, 11:00) # 入场窗口
    EOD_EXIT = 15:55              # 盘末强制退出
    
    def __init__(self, scheduler, open_position_lock):
        self._scheduler = scheduler
        self._lock = open_position_lock  # 与主线共享
        self._positions = {}  # {symbol: ORBPosition}
        self._thread = None
        self._running = False
    
    def start(self):
        """启动 daemon thread + waiter thread"""
        # waiter: 等到 09:30 ET 再启动主循环
        # main loop: 5s poll, 09:45-11:00 活跃
    
    def _main_loop(self):
        """5秒轮询主循环"""
        while self._running:
            phase = _get_market_phase()
            if phase == "OPENING_RANGE":   # 09:30-09:45
                self._build_opening_ranges()
            elif phase == "ENTRY_WINDOW":  # 09:45-11:00
                self._evaluate_breakouts()
                self._monitor_exits()
            elif phase == "POST_ENTRY":    # 11:00-15:55
                self._monitor_exits()
            elif phase == "EOD_EXIT":      # 15:55-16:00
                self._force_close_all()
            time.sleep(self.POLLING_INTERVAL)
    
    def _evaluate_breakout(self, symbol, ctx):
        """评估突破 — 3 层防御"""
        # Layer 1: 区间宽度 0.3%-2.5%
        width = (ctx.orb_high - ctx.orb_low) / ctx.orb_low
        if not (0.003 <= width <= 0.025): return None
        
        # Layer 2: 量能确认 ≥ 1.1x (放宽, 原 1.3 太严)
        if ctx.volume_ratio < 1.1: return None
        
        # Layer 3: 突破方向
        if ctx.price > ctx.orb_high * 1.001:  # LONG + 0.1% buffer
            return ORBSignal("LONG", ctx)
        elif ctx.price < ctx.orb_low * 0.999: # SHORT - 0.1% buffer
            return ORBSignal("SHORT", ctx)
        return None
```

### 4.2 app/orb/exits.py — 独立退出栈

```python
class ORBTrailingExit:
    """追踪止损 — ORB 主退出方式"""
    activation_profit_atr = 1.0   # 盈利 1 ATR 后激活
    trail_multiplier = 1.2        # peak - 1.2×ATR
    ratchet = True                # 只往有利方向移
    
    def check(self, pos, current_price, atr) -> Optional[ExitDecision]:
        profit_atr = _signed_profit_atr(pos, current_price, atr)
        if profit_atr >= self.activation_profit_atr:
            trail_stop = self._compute_trail(pos, current_price, atr)
            if _breached(pos.direction, current_price, trail_stop):
                return ExitDecision("ORB_TRAILING_STOP", close_pct=100)
        return None

class ORBFloorStop:
    """硬止损 — 区间中点 或 ATR×1.0 (取近者)"""
    def check(self, pos, current_price, atr) -> Optional[ExitDecision]:
        stop = max(pos.metadata.get("orb_mid", 0),
                   pos.entry_price - atr * 1.0)  # LONG
        if pos.direction == "SHORT":
            stop = min(pos.metadata.get("orb_mid", 9999),
                       pos.entry_price + atr * 1.0)
        if _breached(pos.direction, current_price, stop):
            return ExitDecision("ORB_FLOOR_STOP", close_pct=100)
        return None

class ORBEODExit:
    """盘末退出 — 15:55 ET 强制全平"""
    def check(self, pos, current_time) -> Optional[ExitDecision]:
        if current_time >= time(15, 55):
            return ExitDecision("ORB_EOD_EXIT", close_pct=100)
        return None
```

### 4.3 app/orb/config.py — 配置加载

```python
@dataclass
class ORBConfig:
    enabled: bool = False
    mode: str = "observe"         # observe → shadow → active
    universe: List[str] = field(default_factory=lambda: [
        "US.SPY", "US.QQQ", "US.IWM", "US.DIA",
        "US.XLE", "US.XLF", "US.XLK", "US.XLV",
    ])
    range_minutes: int = 15
    min_range_width_pct: float = 0.3
    max_range_width_pct: float = 2.5
    breakout_buffer_pct: float = 0.1
    min_volume_ratio: float = 1.1
    stop_loss_atr_mult: float = 1.0
    take_profit_atr_mult: float = 2.0
    trail_activation_atr: float = 1.0
    trail_multiplier: float = 1.2
    max_positions: int = 2
    max_daily_trades: int = 4
    capital_pct: float = 0.10     # 10% 总资金
    position_risk_pct: float = 0.005  # 0.5% 单笔风险
    eod_exit_time: str = "15:55"
    polling_interval_seconds: int = 5
```

### 4.4 config/unified_config.yaml — 新增配置段

```yaml
# ── RFC-075: 独立 ORB 辅线 ──
orb:
  enabled: true
  mode: observe                    # observe → shadow → active (渐进上线)
  universe:
    - US.SPY                       # 大盘基准
    - US.QQQ                       # 科技基准
    - US.IWM                       # 小盘 (高波动, ORB 友好)
    - US.DIA                       # 道琼斯
    - US.XLE                       # 能源板块
    - US.XLF                       # 金融板块
    - US.XLK                       # 科技板块
    - US.XLV                       # 医疗板块
  # 入场参数
  range_minutes: 15
  min_range_width_pct: 0.3
  max_range_width_pct: 2.5
  breakout_buffer_pct: 0.1
  min_volume_ratio: 1.1            # 放宽 (原 1.3 导致零交易)
  # 止损止盈 (ATR-relative)
  stop_loss_atr_mult: 1.0          # SL = entry ± 1.0×ATR (紧, 日内)
  take_profit_atr_mult: 2.0        # TP = entry ± 2.0×ATR (R/R 2:1)
  trail_activation_atr: 1.0        # 盈利 1 ATR 后激活追踪
  trail_multiplier: 1.2            # peak - 1.2×ATR
  # 风控
  max_positions: 2
  max_daily_trades: 4
  capital_pct: 0.10                # 10% 总资金
  position_risk_pct: 0.005         # 0.5% 单笔风险
  eod_exit_time: "15:55"
  polling_interval_seconds: 5
```

### 4.5 scheduler_core.py — 集成 (~20 行)

```python
# __init__ 中 (scalping 初始化旁边):
orb_cfg = self._cfg.get_section("orb", {})
self.orb_engine = None
if orb_cfg.get("enabled", False):
    from app.orb.engine import get_orb_engine
    self.orb_engine = get_orb_engine(
        scheduler=self,
        open_position_lock=self._open_position_lock,
        config=orb_cfg,
    )

# run_forever 中 (scalping start 旁边):
if self.orb_engine:
    self.orb_engine.start()

# stop 中 (scalping stop 旁边):
if self.orb_engine:
    self.orb_engine.stop()
```

## 5. 上线策略 (3 阶段渐进)

| 阶段 | mode | 持续时间 | 验证 |
|------|------|----------|------|
| **Phase 1** | `observe` | 1 周 | 日志确认突破检测正常, 区间计算正确 |
| **Phase 2** | `shadow` | 2 周 | shadow 交易记录, 对比主线表现 |
| **Phase 3** | `active` | 持续 | 真实下单, 监控胜率和 PnL |

**当前 PR 实施 Phase 1 (observe)**, 只输出日志不执行交易。

## 6. 决策自检

```
1. 服务哪个焦点？ → F1 (止损止盈策略, 新增交易维度)
2. 改什么指标？ → 交易频率↑, 利用 10:00-11:00 盈利窗口
3. 怎么验证？ → SQL: SELECT * FROM trade_records WHERE owner_strategy='orb'
4. 最坏情况？ → observe 模式不下单, 零风险
5. 怎么回滚？ → orb.enabled: false, 重启
```

## 7. 文件变更清单

| 文件 | 类型 | 行数 | 级别 |
|------|------|------|------|
| `app/orb/__init__.py` | 新增 | ~30 | 🟢 |
| `app/orb/config.py` | 新增 | ~50 | 🟢 |
| `app/orb/engine.py` | 新增 | ~400 | 🔴 (交易路径) |
| `app/orb/exits.py` | 新增 | ~200 | 🔴 (风控路径) |
| `config/unified_config.yaml` | 修改 | ~30 | 🟡 |
| `app/core/scheduler_core.py` | 修改 | ~20 | 🔴 |
