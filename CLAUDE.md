# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠️ 治理规范 (必读，优先级最高)

开始任何工作前，先读 `SYSTEM_OBJECTIVE.md`。该文件定义了：
- 当前阶段和北极星指标
- 当前焦点 (Top 3，超范围需提案)
- Agent 自主边界 (🟢🟡🔴🚫)
- 决策铁律 (5 问检验)
- 反指标和历史教训

`SYSTEM_OBJECTIVE.md` 是本项目的宪法，所有 Agent (包括 Claude) 的工作必须在其框架内执行。

---

## Overview

FTSeek-US is the US market trading system, part of the FTSeek dual-market architecture. Independent pipeline (Planner → Gate → Interceptor) with RiskStateMachine V2 + idempotent interceptor. Uses Futu OpenD shared with HK market (global 1000 slot limit). Currently paper trading (TRADE_ENV=1).

## Commands

```bash
# Start/Stop
systemctl start ftseek-us           # Start US service
systemctl status ftseek-us          # Check status

# Quick checks
curl http://localhost:8899/health   # Shared health endpoint with HK
```

## Architecture

### Core Modules

```
app/
├── main.py                  # Entry point
├── core/
│   ├── scheduler_core.py    # Main scheduler: voting, risk, exit monitoring
│   ├── scheduler_tasks.py   # Signal scanning, position checks, exit monitoring
│   ├── scheduler_triggers.py # Time-based triggers
│   ├── models.py            # Data models (SignalScore, Position, Order, etc.)
│   ├── config.py            # Config loading from unified_config.yaml
│   └── position_recovery.py # Ghost position recovery
├── risk/
│   ├── engine.py            # RiskEngine: circuit breakers, drawdown, pre-trade checks
│   ├── position_exit_monitor.py # Exit monitoring (merged from stoploss_daemon)
│   ├── circuit_breaker.py   # Strategy/symbol/system circuit breakers
│   └── pre_trade_check.py   # Pre-trade validation
├── voting/                  # Multi-model voting system
│   ├── voting_coordinator.py # Coordinates model agents
│   ├── voting_config.py     # Voting configuration
│   └── model_agent.py       # Individual model agent
├── debate/                  # Model debate engine
├── agents/                  # Agent subsystem (voting only)
├── data/                    # Market data providers
├── llm/                     # LLM integration
├── push/                    # Feishu/DingTalk notifications
└── exit/                    # Option quick exit engine
```

### Key Notes

- **`app/risk/exit_engine.py`** — DELETED (deprecated, use `PositionExitMonitor`)
- **`app/llm/agent_processor.py`** — DELETED (no imports, dead code)
- **`app/core/signal_arbitrator.py`** — DELETED (deprecated, no imports)
- **`stoploss_daemon`** — RETIRED. Exit logic merged into main process `PositionExitMonitor`
- **Configuration SSOT**: `config/unified_config.yaml`

### Data Flow

```
Market Data → Signal Generation → VotingCoordinator → RiskEngine → Order Execution
                                                    ↓
                                        PositionExitMonitor (exit logic)
                                                    ↓
                                              Feishu/DingTalk
```

## Configuration

- **SSOT**: `config/unified_config.yaml`
- **Env**: `.env.us` for secrets
- **TRADE_ENV=1**: Paper trading (模拟盘)

## Testing

```bash
pytest tests/                    # All tests
```

## Documentation

Current docs in `docs/`. Archived docs (pre-2026-04-20) are in `docs/archive/`.
Archived scripts are in `scripts/archive/`.



1. 不要假设用户清楚自己想要什么，不清晰时停下来讨论
2. 目标清晰但路径不是最优时，直接建议更好的办法
3. 遇到问题追根因，不打补丁，每个决策要能回答"为什么"
4. 输出说重点，砍掉一切不改变决策的信息
