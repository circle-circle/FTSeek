# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

FTSeek is a quantitative trading system for HK/US stock markets, built with Python 3.8+. Core features include signal generation, risk management, simulated/live trading, and DingTalk notifications.

## Commands

```bash
# Start/Stop system
./start.sh                    # Start via systemd
./stop.sh                     # Stop service
./restart.sh                  # Restart service

# Run tests
python -m pytest              # Run all tests
python -m pytest tests/       # Run tests in directory
python -m pytest -k "test_name"  # Run specific test

# Backtest
python backtest_example.py    # Run backtest example
python run_backtest.py        # Run backtest with custom config

# Parameter Optimization
python scripts/exit_param_gradient_test.py  # Grid search for optimal stop-loss/take-profit

# Quick checks
curl http://localhost:8899/api/v2/health    # Health check
python scripts/risk_monitor.py --test       # Test risk monitor
```

## Architecture

### Core Modules

```
app/
├── main.py                  # Entry point: OpenD → DataSubscriber → Strategy → FastAPI
├── server.py                # FastAPI callback server (port 8899)
├── config.py                # Configuration with environment variable overrides
├── data_subscriber.py       # Futu OpenD L2 data subscription
├── signal_store.py          # Signal state management (pending/confirmed/executed)
├── strategy/                # Strategy registry and implementations
│   ├── __init__.py          # STRATEGY_REGISTRY: confluence_v2, supertrend, sentiment_ai
│   ├── base.py              # BaseStrategy abstract class
│   ├── confluence_v2.py     # Main fusion strategy (RSI+MACD+BB+SuperTrend+MA+Volume)
│   └── sentiment_strategy.py # AI-powered sentiment analysis
├── trader/                  # Order execution
│   ├── executor.py          # TradeExecutor: Futu API wrapper
│   └── position_manager.py  # Position tracking (SQLite)
├── risk/                    # Risk management
│   ├── risk_manager.py      # Risk limits: max position, drawdown, VaR
│   ├── position_sizer.py    # 5 sizing methods (Kelly, Fixed Risk, Volatility-adjusted)
│   ├── stop_loss.py         # 6 stop-loss strategies (ATR, SuperTrend, trailing)
│   ├── realtime_monitor.py  # Real-time risk dashboard (2-hour DingTalk push)
│   └── dingtalk_alert.py    # Risk alert notifications
├── consensus/               # Multi-strategy voting engine
│   ├── vote_collector.py    # Collect strategy signals
│   ├── score_calculator.py  # Calculate fused probability
│   └── weight_calculator.py # Dynamic weight adjustment
├── factors/                 # AI/ML factors
│   ├── sentiment_ai.py      # News sentiment scoring
│   ├── news_integration.py  # News → signal integration
│   └── market_prediction_ai.py # Market direction prediction
├── news_push/               # Financial news collector
│   ├── collector.py         # HK/US news scraping
│   └── qwen_analyzer.py     # Qwen API sentiment analysis
├── reporters/               # Daily/weekly/monthly reports
└── monitoring/              # Performance/error monitoring
```

### Data Flow

```
Futu OpenD (L2) → DataSubscriber → Strategy Engine → Fusion Engine
                                              ↓
DingTalk ← Signal Output ← Signal Store ← Confirmation Callback (FastAPI)
                                              ↓
                                         TradeExecutor → Futu API
```

## Key Patterns

### Signal Lifecycle
1. **Generated** → stored in SignalStore (SQLite: `data/signals.db`)
2. **Pending** → DingTalk ActionCard sent with confirm/defer/ignore buttons
3. **Confirmed** → callback to `/api/confirm`, status updated
4. **Executed** → TradeExecutor places order via Futu OpenD
5. **Tracked** → position monitored for stop-loss/take-profit

### Configuration System

- **Primary**: `app/config.py` loads from environment variables
- **Unified config**: `config/unified_config.yaml` (v5.0.0+)
- **Security**: `API_TOKEN`, `DINGTALK_WEBHOOK_URL`, `DINGTALK_SECRET` must be env vars
- **Safety**: `TRADE_ENV=0` (live) requires `ALLOW_REAL_TRADING=true`

### Strategy Registry

Strategies are registered in `app/strategy/__init__.py`:
- `confluence_v2`: Primary multi-factor strategy
- `supertrend`: Trend following
- `sentiment_ai`: AI sentiment辅助

### Risk Limits (configurable)
- Max position per stock: 20%
- Max total exposure: 80%
- Max daily loss: 5%
- Max drawdown: 15%
- Signal cooldown: 120s per stock

## Testing

```bash
# 626 test cases available
pytest tests/                    # All tests
pytest tests/ai/                 # AI integration tests
pytest tests/test_strategies.py  # Strategy tests
pytest tests/test_trade_executor_v2.py  # Execution tests
```

## Documentation

- `docs/BACKTEST_GUIDE.md` - Backtest framework usage
- `docs/RISK_MANAGEMENT_MODULE.md` - Risk management API
- `docs/LINKAGE_FLOW.md` - DingTalk-Futu integration flow
- `docs/trading_decision_user_guide.md` - Trading engine user guide
- `CONFIG_SPEC.md` - Configuration specification (v5.0.0)
- `config/dynamic_exit_params.yaml` - Dynamic stop-loss/take-profit params (8 market regimes)
- `docs/backtest_output/exit_param_gradient_*.md` - Parameter optimization reports
