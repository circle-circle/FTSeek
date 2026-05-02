"""Event subscriber examples for decoupled module communication.

This module demonstrates how other modules can subscribe to events
from the IntradayCoordinator without direct imports.
"""
import logging
from typing import Dict, Any

logger = logging.getLogger("ftseek.event_subscribers")


def setup_event_subscribers():
    """Register event handlers for cross-module communication."""
    from app.core.event_bus import (
        EventBus, MarketEvent, StrategyEvent, RiskEvent
    )
    
    # Subscribe to market data events
    EventBus.subscribe(MarketEvent.KLINE_PROCESSED, on_kline_processed)
    EventBus.subscribe(MarketEvent.TREND_UPDATED, on_trend_updated)
    
    # Subscribe to strategy events
    EventBus.subscribe(StrategyEvent.SIGNAL_GENERATED, on_signal_generated)
    
    # Subscribe to risk events
    EventBus.subscribe(RiskEvent.STOP_LOSS_TRIGGERED, on_stop_loss_triggered)
    
    logger.info("Event subscribers registered")


def on_kline_processed(event):
    """Handle kline processed events."""
    payload = event.payload
    code = payload.get("code")
    k_type = payload.get("k_type")
    logger.debug(
        f"Kline processed: {code} {k_type} "
        f"close={payload.get('close')} trend_updated={payload.get('trend_updated')}"
    )


def on_trend_updated(event):
    """Handle trend update events."""
    payload = event.payload
    code = payload.get("code")
    new_trend = payload.get("new_trend")
    logger.info(f"Trend updated: {code} -> {new_trend}")


def on_signal_generated(event):
    """Handle signal generation events."""
    payload = event.payload
    code = payload.get("code")
    action = payload.get("action")
    logger.info(f"Signal generated: {code} {action}")


def on_stop_loss_triggered(event):
    """Handle stop loss events."""
    payload = payload = event.payload
    code = payload.get("code")
    price = payload.get("price")
    logger.warning(f"Stop loss triggered: {code} @ {price}")
