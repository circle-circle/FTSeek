#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trading Strategy Health Gate (StrategyHealthGate)

Function:
  When the self-healing system performs a fix within the last 2 hours,
  automatically feedbacks "System Recovering" status to the trading strategy,
  triggering risk reduction measures such as position sizing reduction and threshold increase.

Integration Point:
  app/strategy/consensus_engine.py -> Before signal filtering / position sizing
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta

logger = logging.getLogger("ftseek.healing_gate")

HISTORY_FILE = "/opt/ftseek/FTSeek/data/self_healing_history.json"

# Recovery window in hours
RECOVERY_WINDOW_HOURS = 2


class StrategyHealthGate:
    """Self-healing status to trading constraints gateway"""

    def __init__(self, history_path: str = None):
        self.history_path = Path(history_path or HISTORY_FILE)
        self.last_check_time = 0

    def get_trading_constraints(self) -> dict:
        """
        Get current trading constraint parameters.
        
        Returns:
            {
                "status": "HEALTHY" | "RECOVERING",
                "position_size_multiplier": float (1.0 -> 0.5),
                "confidence_threshold": float,
                "reason": str
            }
        """
        if not self.history_path.exists():
            return self._healthy_state("No history file")

        try:
            with open(self.history_path) as f:
                data = json.load(f)
            reports = data.get("reports", [])
            
            # Filter successful fixes within the last 2 hours
            cutoff = datetime.now() - timedelta(hours=RECOVERY_WINDOW_HOURS)
            cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

            recent_fixes = [
                r for r in reports
                if r.get("fixed_at", "") >= cutoff_str
                and r.get("fix_success", False)
            ]

            if recent_fixes:
                # Classify by severity
                critical_fixes = [r for r in recent_fixes if r.get("severity") == "critical"]
                if critical_fixes:
                    return self._recovering_state(
                        f"Critical fix recently: {critical_fixes[-1].get('issue_name', 'Unknown')}"
                    )
                
                # General fix
                return self._recovering_state(
                    f"Fix recently: {recent_fixes[-1].get('issue_name', 'Unknown')}"
                )

            return self._healthy_state("No recent fixes")

        except Exception as e:
            logger.warning(f"Health gate check failed: {e}")
            return self._healthy_state("Check error")

    def _healthy_state(self, reason: str) -> dict:
        return {
            "status": "HEALTHY",
            "position_size_multiplier": 1.0,
            "confidence_threshold": 0.7,
            "reason": reason
        }

    def _recovering_state(self, reason: str) -> dict:
        return {
            "status": "RECOVERING",
            "position_size_multiplier": 0.5,  # Half position
            "confidence_threshold": 0.85,     # Higher signal threshold
            "max_concurrent_trades": 1,       # Limit concurrency
            "reason": reason
        }

# Singleton
_gate = None

def get_health_gate() -> StrategyHealthGate:
    global _gate
    if _gate is None:
        _gate = StrategyHealthGate()
    return _gate
