# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

FTSeek is a quantitative trading system for HK/US stock markets, built with Python 3.8+. Core features include signal generation, risk management, simulated/live trading, and Feishu/DingTalk notifications.

## Commands

```bash
# Start/Stop system
systemctl start ftseek              # Start HK service
systemctl start ftseek-us           # Start US service
systemctl status ftseek ftseek-us   # Check both services

# Run tests
python -m pytest tests/             # Run all tests
python -m pytest -k "test_name"     # Run specific test

# Quick checks
curl http://localhost:8899/health   # Health check
```

## Architecture

### Core Modules

```
app/
├── main.py                  # Entry point: OpenD → DataSubscriber → Strategy → FastAPI
├── server.py                # FastAPI callback server (port 8899)
├── config.py                # Config loaded via ConfigCenter from unified_config.yaml
├── data_subscriber.py       # Futu OpenD data subscription + consensus engine
├── signal_store.py          # Signal state management (in-memory + SQLite persistence)
├── strategy/                # Strategy registry and implementations
│   ├── __init__.py          # STRATEGY_REGISTRY
│   ├── base.py              # BaseStrategy abstract class
│   ├── confluence_v2.py     # Primary multi-factor strategy (RSI+MACD+BB+SuperTrend+MA+Volume)
│   ├── sentiment_strategy.py # AI-powered sentiment analysis
│   └── consensus_engine.py  # Multi-strategy voting engine (in data_subscriber, NOT consensus/)
├── trader/                  # Order execution
│   ├── executor.py          # TradeExecutor: Futu API wrapper + dynamic lot_size query
│   └── position_manager.py  # Position tracking (SQLite)
├── risk/                    # Risk management
│   ├── risk_manager.py      # Risk limits: max position, drawdown, VaR
│   ├── risk_pipeline.py     # Signal risk pipeline (decision buffer, quality, circuit breaker)
│   ├── circuit_breaker.py   # Hard-coded circuit breaker guard
│   └── realtime_monitor.py  # Real-time risk dashboard
├── pipeline/                # News → strategy pipeline
│   ├── premarket_scheduler.py  # Pre-market pipeline scheduler (08:30/09:00 HKT)
│   ├── premarket_analyst.py    # LLM pre-market analyst
│   ├── battle_plan.py          # Daily battle plan manager
│   └── orchestrator.py         # Pre-market pipeline orchestrator
├── execution/               # Execution utilities
│   └── lot_size_manager.py  # Dynamic HK lot size: Futu API → cache → config → default
├── factors/                 # AI/ML factors
├── news_push/               # Financial news collector (legacy, superseded by V2)
├── reporters/               # Daily/weekly/monthly reports
└── self_healing/            # Auto-healing engine
```

### Key Notes

- **`consensus/` package** is NOT the main consensus engine. The real engine is `app/strategy/consensus_engine.py`. The `consensus/` package only contains `event_performance_tracker.py` and `weight_calculator.py`.
- **`app/core/trade_executor.py`** was DELETED (deprecated, use `app/trader/executor.py`).
- **Configuration SSOT**: `config/unified_config.yaml` loaded via `ConfigCenter` in `config/config_center.py`.
- **Lot size**: Active executor uses Futu API `get_market_snapshot` → `self._lot_size_cache` fallback. No hardcoded maps.

### Data Flow

```
Futu OpenD → DataSubscriber → ConsensusEngine → RiskPipeline → TradeExecutor → Futu API
                                    ↓
News V2 Pipeline → news_signals.db → _poll_news_signals → SignalStore
PreMarketScheduler (08:30/09:00 HKT) → BattlePlan → Trading decisions
```

## Configuration System

- **SSOT**: `config/unified_config.yaml` (loaded by `ConfigCenter`)
- **Env overrides**: `.env` file loaded before YAML
- **Security**: Webhook URLs, secrets via environment variables only
- **Safety**: `TRADE_ENV=0` (live) requires `ALLOW_REAL_TRADING=true`

## Strategy Registry

Registered in `app/strategy/__init__.py`. Only `confluence_v2` is currently enabled by default.

## Testing

```bash
pytest tests/                    # All tests
```

## Documentation

Current docs in `docs/`. Archived docs (pre-2026-04-20) are in `docs/archive/`.
