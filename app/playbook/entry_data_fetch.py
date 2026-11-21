"""Bound entry input waits without cancelling SDK calls or accepting late results."""
from __future__ import annotations

import math
import threading
import time
from typing import Any

from app.data.quote_cache_policy import DATA_CACHE_PUBLICATION_ALLOWED


DEFAULT_ENTRY_DATA_TIMEOUT_S = 3.0
_STATE_INIT_LOCK = threading.Lock()
_READ_ONLY_METHODS = frozenset({"get_full_market_context", "get_quote", "query_positions"})
_REGIME_FACTORY_METHOD = "_market_regime_assess"
_BROKER_POSITIONS_FACTORY_METHOD = "_broker_positions_query"


class EntryDataUnavailable(RuntimeError):
    """Temporary input failure; only the caller may decide/audit a deferral."""

    def __init__(self, query_label: str, reason: str):
        self.query_label = query_label
        self.reason = reason
        super().__init__(f"{query_label}: {reason}")


class _Flight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.abandoned = False
        self.value: Any = None
        self.error: BaseException | None = None


class _QueryState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.flight: _Flight | None = None


def _scheduler_state(scheduler: Any) -> _QueryState:
    with _STATE_INIT_LOCK:
        state = getattr(scheduler, "_entry_data_query_state", None)
        if state is None:
            state = _QueryState()
            scheduler._entry_data_query_state = state
        return state


def fetch_entry_data(scheduler: Any, query_label: str, source: Any, method: str,
                     *args: Any, timeout_s: float | None = None,
                     **kwargs: Any) -> Any:
    """Return an on-time input or defer while retaining any stalled worker's slot."""
    fixed_factory = (source is None and not args and not kwargs
                     and method in {_REGIME_FACTORY_METHOD, _BROKER_POSITIONS_FACTORY_METHOD})
    if method not in _READ_ONLY_METHODS and not fixed_factory:
        raise EntryDataUnavailable(query_label, "not_read_only")
    timeout = DEFAULT_ENTRY_DATA_TIMEOUT_S if timeout_s is None else timeout_s
    try:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
    except (TypeError, ValueError, OverflowError) as error:
        raise EntryDataUnavailable(query_label, "invalid_timeout") from error

    state = _scheduler_state(scheduler)
    flight = _Flight()
    with state.lock:
        if state.flight is not None:
            raise EntryDataUnavailable(query_label, "busy")
        state.flight = flight

    def run_query() -> None:
        value = None
        error = None
        # SDK completion can outlive this flight; cache cleanup would race ordinary reads.
        cache_policy_token = DATA_CACHE_PUBLICATION_ALLOWED.set(False)
        try:
            if fixed_factory and method == _REGIME_FACTORY_METHOD:
                # Lazy constructors can block before the first query.
                from app.playbook.market_regime import MarketRegimeGate
                value = MarketRegimeGate().assess(publish_cache=False)
            elif fixed_factory and method == _BROKER_POSITIONS_FACTORY_METHOD:
                from app.execution.dual_broker import get_dual_broker
                value = get_dual_broker().query_positions(target_env="sim")
            else:
                value = getattr(source, method)(*args, **kwargs)
        except BaseException as query_error:
            error = query_error
        finally:
            DATA_CACHE_PUBLICATION_ALLOWED.reset(cache_policy_token)
            with state.lock:
                if not flight.abandoned:
                    flight.value = value
                    flight.error = error
                flight.done.set()
                if state.flight is flight:
                    state.flight = None

    deadline = time.monotonic() + timeout
    try:
        worker = threading.Thread(target=run_query, name="playbook-entry-data", daemon=True)
        worker.start()
    except Exception as error:
        with state.lock:
            flight.abandoned = True
            if state.flight is flight:
                state.flight = None
        raise EntryDataUnavailable(query_label, "thread_start_failed") from error

    try:
        finished = flight.done.wait(max(0.0, deadline - time.monotonic()))
        with state.lock:
            if not finished or time.monotonic() > deadline:
                flight.abandoned = True
                flight.value = None
                flight.error = None
                raise EntryDataUnavailable(query_label, "timeout")
            value, error = flight.value, flight.error
            flight.value = None
            flight.error = None
        if error is not None:
            raise EntryDataUnavailable(query_label, "query_failed") from error
        return value
    except BaseException:
        with state.lock:
            flight.abandoned = True
            flight.value = None
            flight.error = None
        raise


def fetch_entry_market_regime(scheduler: Any, *, timeout_s: float | None = None) -> Any:
    """Bound constructor plus assessment; no other preflight work runs here."""
    return fetch_entry_data(scheduler, "entry_market_regime", None,
                            _REGIME_FACTORY_METHOD, timeout_s=timeout_s)


def fetch_entry_broker_positions(scheduler: Any, *, timeout_s: float | None = None) -> Any:
    """Bound broker initialization plus fixed SIM positions query, never orders."""
    return fetch_entry_data(scheduler, "broker_positions", None,
                            _BROKER_POSITIONS_FACTORY_METHOD, timeout_s=timeout_s)
