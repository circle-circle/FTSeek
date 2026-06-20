# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠️ 治理规范 (必读，优先级最高)

`SYSTEM_OBJECTIVE.md` 定义了项目宪法（如存在）。

开始工作前必读:
1. `cat CLAUDE.md` — 本文件 (项目上下文)
2. `cat docs/ARCHITECTURE.md` — **真实在跑的链路** (yaml 声称 ≠ 实际代码)
3. `git log --oneline -5` — 最近提交
4. `cat app/risk/exit_policy.yaml` (前30行) — 当前退出策略 SSOT

## 🚧 Git Hooks（已启用）

- **pre-commit**: 拦截 .env 修改、yaml enabled→true、Python 语法错误
- **commit-msg**: 高风险改动必须引用 [ADR-XXX]/[RFC-XXX]/[hotfix]

---

## Overview

FTSeek-US 是美股实盘交易系统。双线路架构：自动交易（信号管线）+ 手动交易（Dashboard），共享统一风控层。

**当前状态 (2026-06-14):**
- 交易环境: **实盘** (TRADE_ENV=0, ALLOW_REAL_TRADING=true)
- 自动交易: **关闭** (auto_trading_enabled=false, 信号生成但不执行)
- 手动交易: **实盘运行** (Dashboard 下单 → Futu broker)
- 账户: 281756480166019917, ~$82K equity

## Commands

```bash
# Start/Stop
systemctl start ftseek-us           # Start US service
systemctl status ftseek-us          # Check status
systemctl restart ftseek-us         # Restart after config change
systemctl start ftseek-dashboard    # Dashboard (FastAPI:8901, nginx:8888)

# Logs
tail -f logs/ftseek_us.log          # Main service log

# Quick checks
curl http://localhost:8899/health   # Health endpoint
```

## Architecture

### 交易双线路

```
入场:
  自动线: signal_pipeline → voting → portfolio_constructor → _submit_order()
  手动线: Dashboard UI → ManualOrderService.submit_order()

共享风控层 (_submit_order / validate_order):
  [P0] Kill Switch (紧急制动, 双路统一)
  [P0] 同标的反向仓位硬拦截 (查 broker 真实持仓)
  [P0] 账户级回撤 3% (含手动仓, peak→current)
  [P1] 单标的敞口 ≥20% 禁止自动加仓 (auto+manual 合并)
  [P1] 手动平仓 → 清理 trail_state + position_store

自动线专属:
  opening_drive 时段门禁 (禁止入场)
  auto_trading_enabled 总开关
  信号冷却 / 标的日亏限额 / pre_trade_safety

手动线专属:
  单笔 $50K 上限 / 日限 20 次 / PDT Guard / Capital Safety BLACKOUT
```

### 退出策略 (极简三件套)

三层级联 (非竞争, 详见 docs/ARCHITECTURE.md §4):
  [1] ExitRuleDispatcher  ← 主路径 (dispatcher_enabled=true)
  [2] ExitMonitor+Registry ← 处理剩余仓位
  [3] legacy _check_via_legacy ← 仅 registry 出错兜底

LIVE 规则 (只3条):
  [1] ATR 硬止损: entry ± 4.0×ATR, max 3% → 全平
  [2] 目标止盈: profit ≥ 1.0 ATR → scale_out 分层 (SHORT|TECHNICAL 让利润跑, LONG 快平)
  [3] 盘末退出: 收盘前 8min → 盈利落袋 / 亏损保留过夜

保护:
  Grace Period: 入场后 45min 仅允许硬止损
  退出冷却: 120s
  手动仓位隔离: PROTECTED, ExitMonitor 不碰

⚠️ Landmine: trailing_stop 规则(即ATR硬止损)在 exit_rules 无 yaml key,
   仅靠 registry 内层路径存活。position_exit_monitor.py 的 registry 禁用逻辑
   必须保持"缺省=启用", 切勿改 fail-close, 否则关掉实盘 ATR 止损。

配置 SSOT: app/risk/exit_policy.yaml
```

### Core Modules

```
app/
├── main.py                       # Entry point
├── core/
│   ├── scheduler_core.py         # Main scheduler + PositionStore
│   ├── models.py                 # SignalScore, Position, Order
│   └── position_sync.py          # Broker ↔ PositionStore 同步
├── pipeline/
│   ├── signal_pipeline.py        # Signal generation + voting
│   ├── order_executor.py         # _submit_order (自动线入口)
│   └── portfolio_constructor.py  # Portfolio scoring + allocation
├── risk/
│   ├── position_exit_monitor.py  # ExitMonitor (退出三件套)
│   ├── exit_policy.yaml          # 退出策略 SSOT ★
│   ├── atr_stop_engine.py        # ATR 止损定价 (纯函数)
│   ├── exit_rules/               # 各退出规则实现
│   │   ├── trailing_stop.py      # ATR 止损 + Chandelier (已禁用)
│   │   ├── scale_out.py          # 分批止盈 (现全平)
│   │   └── pre_close.py          # 盘末退出
│   ├── capital_safety.py         # 资本安全水位
│   ├── circuit_breaker.py        # 熔断器
│   └── pdt_guard.py              # PDT 日内交易规则
├── web_dashboard/
│   ├── manual_order_service.py   # 手动下单服务 (手动线入口)
│   └── routes/                   # FastAPI routes
├── execution/
│   └── broker_adapter.py         # Futu broker 连接
├── positions/
│   ├── source.py                 # PositionSource + ProtectionState
│   ├── reconciler.py             # 启动对账
│   └── risk_aggregator.py        # 跨 source 风险聚合
└── positions/                    # 持仓生命周期管理
```

### Key Notes

- **配置 SSOT**: `config/unified_config.yaml` (全局), `app/risk/exit_policy.yaml` (退出策略)
- **信号 IC ≈ 0.02**: 信号对收益无预测力，自动交易关闭，手动观察为主
- **唯一 alpha**: SHORT + closing_drive (77.1% WR, +46.2%)
- **opening_drive 禁止入场**: 136 笔 -366.6%, 数据驱动
- **progressive_trailing 已禁用**: current_sl 写入 17 次但无任何退出规则读取
- **手动仓位无自动止损**: PROTECTED 状态, ExitMonitor 不干预 (P1-6 待做)

## Configuration

- **SSOT**: `config/unified_config.yaml`
- **退出策略**: `app/risk/exit_policy.yaml`
- **Env**: `.env.us` for secrets (TRADE_ENV=0 实盘)

## Testing

```bash
pytest tests/                    # All tests
```

## 开发原则

1. 不假设用户清楚自己想要什么，不清晰时停下来讨论
2. 目标清晰但路径不是最优时，直接建议更好的办法
3. 遇到问题追根因，不打补丁，每个决策要能回答"为什么"
4. 输出说重点，砍掉一切不改变决策的信息
5. 做减法优先: 删代码消除根因 > 加校验打补丁
