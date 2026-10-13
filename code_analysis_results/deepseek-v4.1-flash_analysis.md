# deepseek-v4.1-flash - FTSeek-US Code Analysis

**Response Time**: 354.6s
**Output Tokens**: 26705
**Reasoning Tokens**: 14638
**Retries**: 0
**Request ID**: 44df730b-98df-92f4-89e7-11a9629d350f

## 1. Architecture Analysis

**Pattern**: Modular monolith with a layered pipeline: an asyncio-driven scheduler core (TradingScheduler) orchestrates a signal -> decision -> risk -> execution -> position-management chain. Cross-cutting concerns are expressed as ABC 'engines' (RiskEngine / ExecutionEngine / ObservabilityEngine over BaseEngine) with an EventStore as an event/trace sink, and a DI container provides singletons (config_container, get_capital_allocator, PositionStore, broker adapter). Persistence is SQLite (WAL) used ad-hoc from many modules; runtime state is split between an in-memory SSOT (PositionStore) and the broker (Futu) as the real SSOT, reconciled by a 30s background thread (position_sync). Threading and asyncio are mixed: asyncio scan/monitor loops plus dedicated threads for position sync, TWAP, heartbeat/reconnect, and symbol locks. Domain logic is further layered into strategy -> coordinator (capital_allocator / signal_arbitrator / exit_dispatcher) -> playbook (playbook_service / exit_engine) -> risk (position_exit_monitor / exit_rules registry).

**Data Flow**: 1) scan_loop() applies market gate + circuit breakers, then _run_signal_scan_impl() batches symbols by adaptive polling tier and calls _process_symbol_impl(). 2) Per symbol: market data (market_data_router/futu_lv3_provider) -> context enrichment (earnings, VIX regime, premarket) -> playbook fast-lane short-circuit OR technical signal/LLM vote -> consensus -> SignalScore (with position_pct). 3) Risk gate: engine pre_trade_check + execution_rules + PDT guard + notional sanity -> order built in order_executor (_submit_order / _submit_order_optimized). 4) Capital side: coordinator/capital_allocator.try_reserve(strategy, position_pct) is intended to debit the bucket ledger before submission (currently observe-only and NOT called on the order path). 5) Execution: broker_adapter.submit_order (or TWAP for notional > threshold) -> Futu OpenD -> fill returns (order_id, futu_order_id, filled price); simulated fills are synthesized when disconnected in SIMULATE mode. 6) Position management: PositionStore.add() writes the in-memory SSOT, registers with symbol_lock_manager, and mirrors into daily_active_pool; trade_store persists the record. 7) Exit path: risk/position_exit_monitor + playbook/exit_engine evaluate rules each tick and call _close_position (decorated with _with_symbol_lock_release) -> broker order -> PositionStore.remove() -> capital release. 8) Reconciliation: position_sync thread polls Futu positions/orders, detects external closes, reclassifies AUTO vs MANUAL by order remark, reconciles playbook execution states, and force-corrects the capital ledger and DB.

**Main Components**:
- app/core/scheduler_core.py::TradingScheduler + PositionStore (in-memory position SSOT, symbol-keyed dict guarded by RLock)
- app/core/engine.py::BaseEngine / RiskEngine / ExecutionEngine / ObservabilityEngine (abstract engine contracts) + EventStore
- app/core/config.py -> app/core/config_container.ConfigContainer (single config entry point, legacy ConfigLoader retained)
- app/core/position_sync.py (background reconciliation thread, 30s; external-close detection; playbook execution-state reconcile; Futu order-remark whitelist for AUTO claiming)
- app/pipeline/signal_pipeline.py (scan loop -> per-symbol decision chain: market data, LLM voting, execution_rules gate, consensus, SignalScore)
- app/pipeline/order_executor.py (order building, risk gate, qty computation, TWAP routing, close/re-entry handling, SymbolLock decorator)
- app/execution/broker_adapter.py::FutuBrokerAdapter (order submit/cancel/query, positions, heartbeat/reconnect) + twap_executor + symbol_lock_manager
- app/coordinator/capital_allocator.py (bucket-based capital ledger, observe/enforce modes, SQLite-persisted ledger)
- app/risk/position_exit_monitor.py (ATR stop / chandelier / scale-out / flash / pre-close / time-stop orchestration over exit_rules)
- app/playbook/exit_engine.py + overnight_decision (playbook self-managed exit chain, horizon/overnight exemptions)
- app/storage/trade_store.py, app/storage/rejection_logger.py, app/positions/source.py (owner classification: auto / legacy_protected / manual / manual_orphan)
- app/notifier, app/observability, app/web_dashboard, app/review (reporting/monitoring surfaces reading the ledger and trade store)

**Architectural Weaknesses**:
- Dual SSOT for positions (PositionStore memory dict vs broker) with only a 30s periodic reconcile and no conflict resolution policy; broker_adapter.get_positions() returns [] on disconnect, which is indistinguishable from 'account is flat'.
- PositionStore keys state by symbol only, while symbol_lock_manager, position_sync and the order path are (symbol, direction, owner)-aware - LONG/SHORT legs can silently overwrite each other.
- Fail-open risk gates: multiple gates (VWAP deviation lookup, execution_rules, playbook classification, entry_time parsing) swallow exceptions and return 'allow', so a data outage silently disables risk controls.
- Capital control is architecturally inert: observe_only defaults to True and the order path never calls try_reserve (acknowledged in the code), so max_total/caps are not enforced on real submissions.
- Mixed threading + asyncio with module-level mutable globals (_VIX_HISTORY/_SPY_HISTORY, exit-engine log-dedup sets, capital_allocator singleton) that are not consistently lock-protected or per-scheduler scoped.
- No repository/port abstraction for persistence: every module opens its own sqlite3 connection with its own timeout/pragma assumptions; no single-writer discipline, making deadlock/DB-locked failures and drift likely.
- Very large modules (order_executor.py 5685 lines, pipeline.py 9453 lines historically, position_sync.py 2853 lines) with hidden coupling and duplicated constants (EXTERNAL_CLOSE_DUP_SQL defined but not used).
- Duplicated domain vocabularies (two ExitReason enums, two exit-rule dispatch mechanisms - legacy if-chain plus ExitRuleRegistry 'gradually introduced'), creating two sources of truth for exit decisions.
- Configuration history debt (ConfigLoader/UnifiedConfig/ConfigContainer) still leaves legacy accessors alive, so different call sites can read different config snapshots.

## 2. Bugs Detected (15 issues)

### Bug 1: [CRITICAL] error_handling
- **File**: app/execution/broker_adapter.py (lines 785-800 (get_positions, not-connected branch))
- **Description**: get_positions() returns an empty list when the adapter is not connected (only a debug log is emitted). The return type cannot express 'unknown', so callers (position_sync reconciliation, position_monitor, pre-trade equity checks) receive a value that is indistinguishable from 'the account holds nothing'.
- **Impact**: Any transient Futu/OpenD disconnect (heartbeat interval is 30s, reconnect backoff up to 30s) can make the reconciler conclude the account is flat: open positions can be deleted from the memory SSOT/DB, spurious 'external close' records can be written, and equity/notional gates can be evaluated against zero exposure. This is a silent, money-losing failure mode that only manifests during the exact moment the broker link is unstable.
- **Fix**: Never overload empty-with-error. Raise a typed BrokerUnavailableError (or return a snapshot object with .ok=False), and make every caller fail-closed on it. Minimal change: `if not self.is_connected(): raise BrokerUnavailableError('positions unavailable')`, plus an `is_connected()` guard at all reconcile entry points in position_sync before any delete/insert. Keep an explicit `get_positions_or_none()` for legacy call sites that truly want 'empty means empty'.

### Bug 2: [CRITICAL] logic_error
- **File**: app/execution/broker_adapter.py (lines 483-492 (submit_order disconnect branch))
- **Description**: When not connected and self.trade_env != 0, submit_order returns self._simulate_fill(order) - i.e. it fabricates a full fill. The guard added in 'DEB-20260903 #97' only protects the case where trade_env == 0. If the process is configured with a SIMULATE adapter (trade_env=1) while the deployment is meant to trade a real/simulated-but-authoritative account (env var AUTO_TRADE_ACCOUNT mismatch), every offline order becomes a phantom position.
- **Impact**: Phantom positions in PositionStore/DB that do not exist at the broker: the exit monitor will manage a non-existent position, capital is debited, realized PnL becomes fiction, and if the adapter is later replaced by a real one the close order is rejected or (worse) an unintended opening order is placed. Also masks broker outages entirely from operators.
- **Fix**: Make simulated fills opt-in and explicit: `if not self.is_connected(): if not self._allow_simulated_fill: raise RuntimeError(...); return self._simulate_fill(order)`, where _allow_simulated_fill is a config flag (default False) that is asserted False whenever the trading account is real or a live trading session is active. Additionally cross-check trade_env against AUTO_TRADE_ACCOUNT at startup and refuse to boot on mismatch.

### Bug 3: [CRITICAL] logic_error
- **File**: app/core/scheduler_core.py (lines 45-95 (PositionStore.__init__/add))
- **Description**: PositionStore keeps `self._positions: Dict[str, Position]` keyed by symbol only and `self._pending: Set[str]` keyed by symbol only. The rest of the system is (symbol, direction, owner)-aware: symbol_lock_manager.release(position.symbol, direction, owner), position_sync explicitly 'Handles both LONG and SHORT positions', and capital/owner classification is per-leg. add() does an unconditional `self._positions[symbol] = position`, so a second leg on the same symbol overwrites the first.
- **Impact**: If a LONG and a SHORT (or an auto leg and a manual/legacy leg) coexist on one symbol, the memory SSOT holds only one of them: the overwritten leg is never evaluated by the exit monitor, never released in the capital ledger, and never reconciled against the broker, producing unbounded unmanaged exposure. Because the exchange/broker still holds both legs, downside is not symmetric to the local book.
- **Fix**: Key positions and pending by a composite: `Key = Tuple[str, str, str]  # (symbol, direction, owner)` (or an internal `_key(symbol, direction, owner)` returning f'{symbol}|{direction}|{owner}'). add()/remove()/has()/get() take direction+owner and reject/raise on a real duplicate key instead of overwriting; expose `all_for_symbol(symbol)` for direction-agnostic queries. Migration: keep a `_by_symbol` index for O(1) lookups.

### Bug 4: [HIGH] race_condition
- **File**: app/coordinator/capital_allocator.py (lines ≈580-596 (get_capital_allocator))
- **Description**: The module-level singleton is created with an unguarded check-then-act: `global _singleton; if _singleton is None: ... _singleton = CapitalAllocator(...)`. CapitalAllocator itself is lock-protected, but instantiation is not, and the process runs multiple threads (position_sync thread, TWAP/heartbeat threads, DI-container warm-up).
- **Impact**: Two racing callers can each construct a CapitalAllocator. The loser's instance is discarded but already returned to its caller, so two components hold different ledgers: caps and max_total are effectively doubled, persisted state is written from two objects, and `reset_and_reconcile` on one has no effect on the other. Capital isolation (Task #35 auto/legacy/manual pools) silently fails.
- **Fix**: Use double-checked locking with a module lock:
```python
_singleton_lock = threading.Lock()

def get_capital_allocator(caps=None, max_total_pct=0.90, observe_only=True, db_path=None):
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            if caps is None:
                raise ValueError("CapitalAllocator 第一次构造必须传 caps")
            _singleton = CapitalAllocator(caps=caps, max_total_pct=max_total_pct,
                                         observe_only=observe_only, db_path=db_path)
    return _singleton
```
Additionally log a warning (not silently ignore) when a later call passes non-default caps/db_path that differ from the constructed singleton.

### Bug 5: [HIGH] missing_validation
- **File**: app/coordinator/capital_allocator.py (lines ≈145-270 (try_reserve / release))
- **Description**: Neither try_reserve nor release validates the sign or finiteness of position_pct. In try_reserve a negative value can never trip `current + position_pct > cap` or `total + position_pct > max_total`, so it is classified ALLOWED and executed as `self._allocations[bucket] = current + position_pct` - i.e. a negative reservation silently CREDITS the ledger. Symmetrically, release() computes `max(0.0, current - position_pct)`, so a negative pct inflates the bucket.
- **Impact**: A single sign error upstream (short leg, unsigned conversion, mis-scaled pct) permanently corrupts the ledger: it either frees capital that is actually deployed (over-allocation once enforce mode is enabled) or blocks all trading by inflating usage. Because the corruption is persisted to capital_state, it survives restarts and is only repaired by reconcile_with_positions.
- **Fix**: Validate at the boundary and fail loudly:
```python
if position_pct is None or not math.isfinite(position_pct) or position_pct < 0:
    logger.error(f"[CapitalAllocator] invalid position_pct={position_pct!r} strategy={strategy} -> reject")
    return False  # or raise ValueError for programmer errors
```
and in release(): `if position_pct is None or position_pct <= 0: log+return`. Also guard `strategy` being empty/None, and clamp release to the strategy's own outstanding amount (see logic_issues entry on release drift).

### Bug 6: [HIGH] logic_error
- **File**: app/coordinator/capital_allocator.py (lines ≈218-250 (try_reserve cap check) and ≈258-282 (compute_scale_factor))
- **Description**: Both the per-bucket cap check (`if cap > 0 and current + position_pct > cap`) and compute_scale_factor (`bucket_headroom = ... if cap > 0 else float('inf')`) treat a bucket whose configured cap is 0.0 - and, more importantly, any bucket absent from `self._caps` (get() default 0.0, e.g. a new/renamed strategy or a typo'd bucket_map target) - as UNLIMITED rather than as 'no budget'. The module docstring itself gives `{"main": 0.80, "llm_plan": 0.0}` as a legitimate configuration, where 0.0 reads as 'disabled'.
- **Impact**: Any strategy/bucket not present in the caps dict gets an unlimited per-strategy budget and is bounded only by max_total. A strategy rename, a bucket_map typo, or an intentional 0.0 allocation silently removes the per-strategy ceiling - exactly the '软约束' problem Phase A was created to eliminate. Losses can concentrate in a single unconfigured strategy up to the global cap.
- **Fix**: Make the policy explicit instead of inferring it from a numeric default. Suggested: reserve -1.0 (or a sentinel `UNLIMITED`) for 'no per-strategy cap' and treat 0.0 as 'blocked', plus fail-closed on unknown buckets:
```python
UNLIMITED = -1.0
cap = self._caps.get(bucket, 0.0)
if cap != UNLIMITED and current + position_pct > cap:
    decision = "BLOCKED_STRATEGY_CAP"
```
and warn once per unknown bucket at first use (`if bucket not in self._caps: logger.warning(...)`). Apply the identical rule in compute_scale_factor (headroom = inf only when cap == UNLIMITED). Document the chosen semantics in the class docstring since reconciliation/snapshot code must agree.

### Bug 7: [MEDIUM] concurrency
- **File**: app/coordinator/capital_allocator.py (lines ≈480-520 (_record_event -> _persist_event), ≈190-215 (call sites under self._lock))
- **Description**: Every reserve/release/force_set/reconcile calls _record_event -> _persist_event (and _persist_state) WHILE holding the non-reentrant `self._lock`. _persist_event opens a fresh sqlite3 connection, executes an INSERT plus `DELETE ... WHERE id <= (SELECT MAX(id) FROM capital_ledger) - 5000`, and commits. All of that I/O happens inside the ledger's critical section.
- **Impact**: The ledger lock is held for the duration of a SQLite connect+commit (milliseconds to tens of milliseconds under WAL write contention) on the hot order path. With several scheduler threads, reserve/release serializes on disk I/O; a stuck/locked DB (busy_timeout up to 5s) blocks every capital decision for seconds. Symptoms will look like random trading latency spikes with no obvious cause.
- **Fix**: Move persistence out of the critical section: build an immutable event record under the lock, append to an in-memory queue, and have a single writer thread/drain task persist asynchronously (or use `asyncio.to_thread`). If synchronous persistence must stay, reuse one long-lived connection per process guarded by _db_lock and drop per-call connect/commit. Also replace the per-insert MAX/DELETE pruning with a periodic (e.g. every N events or on a timer) retention task - the current subquery runs a table scan on every single ledger row.

### Bug 8: [HIGH] logic_error
- **File**: app/core/position_sync.py (lines 28-34 (EXTERNAL_CLOSE_DUP_SQL) vs 105-123 (upsert_external_close_trade_record))
- **Description**: The module defines EXTERNAL_CLOSE_DUP_SQL as the single source of truth for the external-close duplicate predicate, and the comment states the guard body is untestable so 'tests 直接对该 SQL 钉不变量'. However upsert_external_close_trade_record does NOT use the constant - it inlines a different query: `SELECT id FROM trade_records WHERE symbol=? AND direction=? AND exit_price > 0 ORDER BY id DESC LIMIT 1` (different column list, extra ORDER BY/LIMIT).
- **Impact**: The invariant pinned by tests applies to a string that production never executes. The real predicate can be changed (or has already diverged) without any test failing, which is precisely how the previously documented bug class - 'real broker close skipped INSERT + notification while open_positions DELETE still ran, so the close vanished' - recurs. Additionally the query selects (symbol, direction, exit_price>0) with no entry_time linkage despite entry_time being selected in the constant.
- **Fix**: Single-source it: `closed = conn.execute(EXTERNAL_CLOSE_DUP_SQL, (symbol, direction)).fetchone()` and make the constant return exactly the columns the caller needs (`SELECT id, entry_time FROM trade_records WHERE ...`). Add a unit test asserting no other module inlines a `trade_records ... exit_price > 0` predicate (grep-based guard) so the invariant cannot silently fork again.

### Bug 9: [MEDIUM] logic_error
- **File**: app/core/position_sync.py (lines 370-376 (_reconcile_playbook_execution_states))
- **Description**: Environment filtering normalizes only one side: `env = str(row["broker_env"] or broker_env).lower()` then `if env != broker_env: continue`. The comparison is against the caller-supplied broker_env verbatim, so it only works if the caller always passes a lowercase value ('simulate'/'real'). Any caller passing 'SIMULATE', 'REAL', or a differently formatted env string ('Simulate') skips every row.
- **Impact**: Reconciliation silently becomes a no-op: playbook_instructions stuck in UNKNOWN/SUBMITTED/FILLED_UNBOOKED are never resolved, holding slots and (per the B1-lite design) blocking re-entry for the symbol. The failure is invisible - the function returns normally having done nothing - and matches the '不支持交叉交易, 有待成交订单' symptom class described in order_executor's header.
- **Fix**: Normalize both sides once at function entry: `broker_env = str(broker_env or '').strip().lower()` and compare `env == broker_env`. Log at INFO how many rows were considered vs skipped per env so a permanent zero-work pass is observable. Also add a metric/counter for rows examined by state.

### Bug 10: [HIGH] error_handling
- **File**: app/execution/broker_adapter.py (lines 600-632 (cancel_order post-verify branch))
- **Description**: After a successful RET_OK cancel request the code re-queries order state, but the documented contract is 'CANCELLING_*/CANCELLED_* -> True; 其余/查询异常 -> True+warning' - i.e. an unverifiable or non-terminal state is reported to the caller as a successful cancellation.
- **Impact**: Callers treat the order as dead and may place a replacement (close/re-entry/trim) while the original is still live at the broker. That produces the exact production failure the module comments describe ('不支持交叉交易, 有待成交订单') and can double the intended position or leave a resting order that fills hours later against a position the system believes is closed. The 'terminal confirmation is left to position_sync (30s)' fallback is far too slow for a close-then-reenter path.
- **Fix**: Do not conflate 'requested' with 'dead'. Return a tri-state (CANCELLED / PENDING / UNKNOWN) or raise for UNKNOWN, and make order_executor wait for a real terminal state (bounded poll with backoff, e.g. 5 attempts over 10-20s) before submitting a replacement. On UNKNOWN, block the replacement order for that (symbol, direction, owner) and raise an alert instead of proceeding.

### Bug 11: [HIGH] concurrency
- **File**: app/pipeline/order_executor.py (lines 55-75 (_with_symbol_lock_release))
- **Description**: The decorator unconditionally releases the SymbolLock in a finally block: `finally: get_symbol_lock_manager().release(position.symbol, _dir_str, _owner)`. It does not know whether the wrapped _close_position actually acquired the lock, nor whether the acquire key matched the release key (release uses `owner_strategy or 'main'` and a direction string derived at release time; the acquire site may have used a different owner/direction). It also fixes the positional signature `wrapper(scheduler, position, *args, **kwargs)`, so a kwarg call `_close_position(scheduler, position=pos)` raises TypeError.
- **Impact**: Two distinct failures: (a) releasing a lock never held can clear a DIFFERENT holder's symbol lock, allowing a concurrent open/close on the same symbol+direction -> duplicated orders, over-sized positions, or an exit canceling a fresh entry; (b) if the acquire key differs from the release key, the real lock is never freed, permanently blocking that symbol (a silent trading halt until restart). Rationale is not confirmable from the provided excerpt because _close_position's body is truncated - verify whether acquire lives inside _close_position or in the caller.
- **Fix**: Make acquisition and release explicit and symmetric: acquire inside the wrapper (or have _close_position return/own a SymbolLock handle), track `acquired = False; ... acquired = True` and only release when acquired; build the key once (`lock_key = (symbol, direction_value, owner or 'main')`) and pass the same tuple to acquire and release. Define the signature as `wrapper(scheduler, position=None, *args, position_kw=None, **kwargs)` or bind args with inspect.signature. Add a debug assertion that release() finds a matching held lock.

### Bug 12: [MEDIUM] concurrency
- **File**: app/pipeline/signal_pipeline.py (lines 53-62 (_VIX_HISTORY / _SPY_HISTORY module globals))
- **Description**: Rapid-regime-change detection keeps its samples in module-level mutable lists shared by every importer, appended and trimmed from the scan loop (`_HISTORY_MAX_SECONDS`, `_HISTORY_MAX_ENTRIES`). There is no lock and no per-scheduler scoping; trimming is done by rebinding/slicing the list.
- **Impact**: Two concrete failures: (1) a reader iterating _VIX_HISTORY concurrently with the trim can raise 'list changed size during iteration' / ValueError, aborting the regime check at the worst possible moment (a rapid regime change); (2) in any process hosting more than one scheduler (live + backtest/replay, or two accounts), the histories cross-contaminate, so the rapid-change trigger fires on samples from an unrelated run - a false circuit-breaker/regime switch or a missed one.
- **Fix**: Move the buffers onto the scheduler/regime-detector instance (or a small `RegimeHistory` class holding `collections.deque(maxlen=N)`), guard mutations with the class's own lock or confine access to a single asyncio task, and evict by timestamp inside the same critical section. As an immediate stopgap, replace slice-rebinding with deque(maxlen) which makes append/evict atomic w.r.t. iteration semantics and cannot change size unexpectedly.

### Bug 13: [MEDIUM] race_condition
- **File**: app/core/scheduler_core.py (lines 75-95 (PositionStore.add, _pending.discard))
- **Description**: add() clears the duplicate-open guard for the symbol (`self._pending.discard(symbol)`) for ANY caller. The pending set exists precisely to cover the broker_sync 5s debounce window described in the __init__ comment; but the background position_sync thread also calls add() when it discovers a broker position, so an unrelated adoption/echo path clears the guard while two signal threads are still in flight.
- **Impact**: The duplicate-open window reopens: within the debounce interval, another signal for the same symbol sees has()==False and pending cleared -> a second order is submitted. Result is over-sized positions, doubled commission, and a position larger than the capital ledger believes - the exact failure the pending set was introduced to fix.
- **Fix**: Only the code path that actually created the reservation should clear it: pass an explicit token, e.g. `add(symbol, position, pending_token=...)` and `self._pending.discard(token)`; or make pending entries carry an owner/timestamp with TTL and let a janitor expire them instead of clearing on any add. Never clear pending from the reconciliation/adoption path.

### Bug 14: [MEDIUM] logic_error
- **File**: app/core/engine.py (lines 29-35 and 59-63 (BaseEngine.heartbeat/is_healthy; RiskEngine.initialize))
- **Description**: is_healthy() requires `(datetime.now() - self.last_heartbeat).total_seconds() < 60`, but last_heartbeat is set only in BaseEngine.__init__ and in heartbeat(). None of the subclasses' initialize()/shutdown() call super().initialize()/heartbeat(), and nothing in the provided file calls heartbeat().
- **Impact**: After 60 seconds of uptime every engine reports unhealthy regardless of actual state. Any supervisor, dashboard, or auto-restart logic keyed on is_healthy() will generate a permanent false alarm, either masking a real failure inside the noise or triggering an unnecessary restart during trading hours. This also contradicts the intent of the heartbeat API.
- **Fix**: Have initialize() call self.heartbeat() and require subclasses to call super().initialize(); alternatively derive health from real work signals (last successful pre_trade_check / last submitted order / last metrics collection) rather than a manual ping nobody sends. If a supervisor is intended, add a scheduled `heartbeat()` call per engine and an explicit degraded state with hysteresis instead of a bare 60s hard cutoff.

### Bug 15: [MEDIUM] missing_validation
- **File**: app/execution/broker_adapter.py (lines 82-110 (_notional_sanity_check))
- **Description**: The 'last wall' 30% notional cap is bypassed by the caller-supplied boolean is_close: `if is_close: return True` (added 2026-06-16 because a $43K SNDK close was rejected). The flag is trusted from the order construction path without verifying that the order actually reduces an existing position, and the equity fetch path is described as fail-closed but the close path skips it entirely.
- **Impact**: Any upstream misclassification of an opening order as a close (direction inference bug, FLAT_ALL handling, or a caller passing is_close=True defensively) completely disables the final notional guard, permitting an unbounded single order at the broker. Because this guard sits below the capital allocator (which is currently observe-only), it is the only hard limit on notional.
- **Fix**: Verify intent instead of trusting the flag: for is_close=True, require that a matching position exists with sufficient quantity net of open reducing orders (`position_store.get(symbol, direction)` size >= order.quantity, or broker order_list_query showing a filled open leg). If verification is impossible, degrade to a much higher but finite cap (e.g. 2x equity) rather than infinity, and emit a WARNING with trace_id so the bypass is auditable.

## 3. Logic Issues (8 issues)

### Issue 1: [CRITICAL] business_logic
- **File**: app/coordinator/capital_allocator.py (lines ≈145-205 (try_reserve docstring) + app/pipeline/order_executor.py ≈694-726)
- **Description**: The capital ledger is not connected to the order path. try_reserve's own docstring records: observe_only=True always returns True, `order_executor._submit_order` does not call try_reserve before submitting, and signal.position_pct defaults to 0.0 so the pre-trade amount is 0. The enforcement code, the AutoCapacityReport, the owner pools and the persistence layer are all therefore decorative with respect to real orders.
- **Impact**: Every capital guarantee marketed in the code (per-strategy cap, max_total 90%, auto/legacy/manual isolation, headroom reporting on the dashboard and in daily_review) is unenforced: the system can reach 100%+ notional exposure in a single bucket while the dashboard reports healthy headroom. Reviewers reading CapitalSnapshot would be misled about real risk.
- **Fix**: (1) In order_executor, compute the intended notional from the final quantity/price (_get_order_qty result) and call `allocator.try_reserve(strategy, notional/equity)` before submit; on False, reject the signal with a rejection_logger entry. (2) On any close (including partial/scale-out/external close), call release with the same strategy key. (3) Make observe_only=false a prerequisite for any 'capital enforced' claim, and expose the current mode on the dashboard/health endpoint so the enforcement state is never ambiguous. (4) Populate signal.position_pct from the sizing layer instead of leaving it 0.0.

### Issue 2: [HIGH] edge_case
- **File**: app/core/position_sync.py (lines 105-123 (upsert_external_close_trade_record dedupe predicate))
- **Description**: External-close dedupe matches only (symbol, direction, exit_price > 0), ignoring entry_time even though the constant selects it. For a symbol traded more than once in the same direction (close then re-enter, or a second cohort), the second, genuine external close matches the FIRST row and is treated as a duplicate.
- **Impact**: The second close is silently skipped - no trade_records INSERT, no notification - while the open_positions DELETE path still runs. The trade disappears from the books: realized PnL understated, capital release possibly doubled/omitted, and the daily review unable to explain the position. This is the same failure shape as the 2026-08-28 bug the comment says was fixed by changing the predicate; the fix is incomplete because the dedupe key is still too coarse.
- **Fix**: Tie the duplicate check to the position instance being closed: match on (symbol, direction, entry_time within tolerance) of the record being created, e.g. `... WHERE symbol=? AND direction=? AND exit_price > 0 AND ABS(entry_time - ?) < 1` (entry_time already selected). Additionally, when a duplicate is detected, log at WARNING with both ids and the incoming entry_time/exit_price so an over-broad dedupe can never be silent.

### Issue 3: [HIGH] business_logic
- **File**: app/playbook/exit_engine.py (lines 59-75 (_is_overnight_position))
- **Description**: Naive entry timestamps are treated as if they were already ET dates: `if tzinfo is not None: astimezone(_et).date() else: _entry.date()`, then compared to `datetime.now(_et).date()`. If entry_time strings are stored in UTC (common across this codebase), an entry at e.g. 2026-09-24T01:00:00Z is 2026-09-23 21:00 ET but is classified as 2026-09-24 - i.e. 'today', not overnight. The `except Exception: return False` fallback compounds this: malformed input disables the rule.
- **Impact**: Positions that are genuinely held overnight are misclassified as intraday, so OVERNIGHT_GAP_STOP never fires for them: deep overnight losers are held past the open, trading the exact gap risk the rule exists to cut. The loss is silent (no alert, no log) and biased toward the most volatile part of the session. Conversely, depending on storage convention, some intraday positions could be classified as overnight and force-closed prematurely.
- **Fix**: Make the storage timezone explicit and never guess: `_entry = datetime.fromisoformat(entry_time_str); if _entry.tzinfo is None: _entry = _entry.replace(tzinfo=timezone.utc)` (if UTC is the storage contract) then `_entry.astimezone(_et).date()`. Assert the convention with a unit test covering 20:00-23:59 ET entries. Replace the bare `except: return False` with a logged WARNING and a metric counter so unparsable entry_time is visible rather than silently disabling a protective exit.

### Issue 4: [HIGH] missing_validation
- **File**: app/pipeline/signal_pipeline.py (lines 24-35 (_get_vwap_deviation) and 66-90 (_check_execution_rules))
- **Description**: Multiple risk gates fail OPEN by construction. _get_vwap_deviation swallows every exception and returns None, with the docstring stating 'pre_trade_check 将软放行'. _check_execution_rules returns True for missing/non-dict rules, for unparsable context (`except (TypeError, ValueError): return True`), and for any case it cannot decide.
- **Impact**: A market-data outage (market_data_router error, missing ticker_vwap_deviation key), a config-loading failure, or a malformed context silently disables the corresponding protection precisely when the environment is degraded - the correlation between data outages and adverse market conditions is exactly when the gates matter most. There is no counter of how often a gate soft-passed, so nobody can tell enforcement from vacuum.
- **Fix**: Introduce an explicit three-valued gate result (PASS / FAIL / UNKNOWN) and a policy per gate: UNKNOWN should block new entries (or at minimum downsize), not silently allow. Emit a metric + WARNING (rate-limited) on every UNKNOWN with symbol and gate name, and add a daily-review surface reporting soft-pass counts so fail-open behavior is measurable rather than invisible.

### Issue 5: [HIGH] business_logic
- **File**: app/coordinator/capital_allocator.py (lines ≈350-366 (release))
- **Description**: release() decrements the BUCKET ledger unconditionally and clamps at 0 (`new_val = max(0.0, current - position_pct)`). In observe mode, reservations that were BLOCKED are deliberately not booked (H7 fix), yet the matching close still calls release() with the full amount. Worked example with caps {'main': 0.8}, bucket_map {'fast_lane': 'main'}: fast_lane reserves 0.05 (booked -> main=0.05); a big 'main' signal reserves 0.90 (BLOCKED, not booked); when that big position closes, release('main', 0.90) drives main to 0.0 and silently wipes fast_lane's 0.05.
- **Impact**: The ledger under-counts real exposure. While observe_only=True this looks harmless, but the moment enforcement is enabled the under-counted ledger authorizes over-allocation - the classic 'capital gate passes, book is already full' scenario. reset_and_reconcile only repairs the state at the observe->enforce flip, so drift accumulated in the meantime grants a one-time over-allocation window.
- **Fix**: Track per-strategy outstanding reservations (`self._reserved_by_strategy`) and clamp release to that amount: `releasable = min(position_pct, self._reserved_by_strategy.get(strategy, 0.0))`; when position_pct > releasable, log a WARNING with both numbers and the decision history, and reconcile the excess from positions rather than crediting the bucket. Never let a release reduce another strategy's share of a shared bucket below its own outstanding amount.

### Issue 6: [MEDIUM] business_logic
- **File**: app/risk/position_exit_monitor.py (lines ≈66-80 (imports) and header 'Task #48 优先级注册表 (渐进式引入)')
- **Description**: Two sources of truth for exit decisions. (a) ExitReason is imported from `.exit_rules` here while order_executor.py imports ExitReason from `app.core.models` - if these are distinct enum classes, comparisons/serialization across the boundary can silently mismatch. (b) The header states the legacy rule if-chain (rules 0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 5) still runs while ExitRuleRegistry is 'progressively introduced' as an auxiliary entry, and playbook/exit_engine.py duplicates a rule chain migrated verbatim from pipeline.py with same-name forwarders on both sides.
- **Impact**: Exit policy can diverge: a rule tightened in the registry but not in the legacy chain (or vice versa) yields inconsistent behavior by which path happens to fire first. Duplicate ExitReason enums risk exit reasons being recorded as unknown/None in trade_records, breaking attribution, exit-quality scoring and the daily review - or, worse, an equals-comparison returning False so a stop that should match does not.
- **Fix**: Pick one exit dispatch and delete the other: either make ExitRuleRegistry the sole dispatcher and reduce the legacy chain to thin wrappers, or freeze the registry. Consolidate ExitReason into exactly one module (app/core/models) and re-export from exit_rules; add an assertion/test that all enum members and their string values are identical across any re-export, and that DB writes only ever receive the canonical type. Add a startup log listing the active rule chain with priorities so the effective policy is observable.

### Issue 7: [LOW] edge_case
- **File**: app/core/position_sync.py (lines 350-382 (_reconcile_playbook_execution_states, LIMIT 25))
- **Description**: Each reconcile pass processes at most 25 unresolved playbook instructions (ORDER BY updated_at, id) and, because rows that cannot be resolved keep their state and their updated_at may be refreshed on each attempt, a small set of permanently-unresolvable rows can occupy the window.
- **Impact**: If more than 25 instructions are stuck (e.g. after a broker outage that left many SUBMITTED/FILLED_UNBOOKED rows), reconciliation throughput can lag behind arrival, and slots stay locked longer than necessary - blocking re-entry on those symbols. Under an outage-recovery burst this stretches hours.
- **Fix**: Make the LIMIT configurable and raise it for the recovery pass; order by updated_at ASC with a 'last_attempt_at' column so rows rotate fairly (no starvation), and skip rows whose backoff window has not elapsed. Emit a metric of pending-row depth so backlog is visible.

### Issue 8: [LOW] missing_validation
- **File**: app/pipeline/order_executor.py (lines 694-726 (_submit_order_optimized))
- **Description**: TWAP routing uses an approximation that can silently misroute: `notional = float(order.quantity) * float(order.price or signal_price or 0.0)`. For MARKET orders (price 0.0/None) the notional is estimated from an arbitrary signal_price, and if price is None and signal_price is 0.0 the notional collapses to 0 -> never TWAP; if signal_price is stale/high the order can be routed to TWAP unnecessarily. `float(order.quantity)` also raises TypeError if quantity is unset instead of producing a validation error.
- **Impact**: Execution-quality decisions (market vs TWAP) become non-deterministic for market/edge-price orders: large orders silently take market impact, or small orders are needlessly sliced and lose timing. The failure is invisible in logs because no validation error is raised on the price path.
- **Fix**: Validate inputs first (`if order.quantity is None or order.quantity <= 0: raise ValueError`), then resolve the reference price once via a helper that falls back to the last quote (not the signal price) and logs which source was used: `ref_price = order.price or last_quote or signal_price`. Route to TWAP only when the resolved notional is trustworthy; otherwise log the reason and use the market path explicitly.

## 4. Optimization Suggestions (8 suggestions)

### 1. [performance] SQLite access pattern: a brand-new sqlite3 connection + PRAGMA-less connect + co
- **File**: app/coordinator/capital_allocator.py
- **Expected Impact**: Removing per-call connect and commits from the hot path cuts capital-decision latency from milliseconds to microseconds and eliminates order-path stalls during DB contention. A single reused connection or an async writer with batched commits should reduce ledger write overhead by an order of magnitude and remove the lock-held-across-IO hazard.

### 2. [maintainability] order_executor.py is 5685 lines and position_sync.py is 2853 lines, with symbols
- **File**: app/pipeline/order_executor.py
- **Expected Impact**: Splitting into focused modules (order_builder, risk_gate, qty_sizing, twap_routing, close_flow, symbol_lock) with explicit interfaces reduces the blast radius of changes and makes the lock-acquisition/release contract testable in isolation - directly preventing the class of bug found in the decorator. Expect measurably faster review and a lower regression rate per change.

### 3. [performance] PositionStore.add() performs daily_active_pool JSON/disk synchronization inside 
- **File**: app/core/scheduler_core.py
- **Expected Impact**: Moving pool persistence to a coalesced async flush (dirty-set + interval writer) shortens the store's critical section from disk-write latency to a set insert, reducing contention for scan_loop/monitor_loop/position_sync and removing a whole class of 'slow lock' stalls during high signal volume.

### 4. [scalability] Rapid-regime-change state lives in module-level globals, so it cannot be scoped 
- **File**: app/pipeline/signal_pipeline.py
- **Expected Impact**: Moving to a per-scheduler RegimeHistory object enables safe concurrent live+backtest execution in one process, makes backtests deterministic, and removes iterator-invalidation risk. Also allows persisting/snapshotting the window for post-mortems.

### 5. [scalability] Connection lifecycle is managed with a dedicated heartbeat thread plus manual re
- **File**: app/execution/broker_adapter.py
- **Expected Impact**: Introducing a broker connection state machine (CONNECTED / DEGRADED / UNAVAILABLE) with a single failure policy consumed by all callers removes the divergent handling that produced the []-means-flat and simulated-fill-on-disconnect defects, and lets trading pause deterministically instead of degrading silently.

### 6. [maintainability] Reconciliation logic (external-close detection, AUTO/MANUAL classification by re
- **File**: app/core/position_sync.py
- **Expected Impact**: Extracting the predicates into named, unit-tested functions (dedupe predicate, remark classification, env normalization) removes the need for the SQL-string invariant pinning, closes the divergence found in EXTERNAL_CLOSE_DUP_SQL, and makes each reconciliation decision individually testable and observable.

### 7. [maintainability] Persistence is ad hoc: every module opens its own sqlite3 connection with its ow
- **File**: app/storage/trade_store.py (repository layer across app/storage/*)
- **Expected Impact**: A single writer-service/repository abstraction (one connection pool, one busy_timeout/WAL policy, one serialization point) eliminates DB-locked failures and cross-module write races, and is a prerequisite for any future move to Postgres without touching trading logic.

### 8. [maintainability] Rule priority is implicit in code order across three modules (position_exit_moni
- **File**: app/risk/position_exit_monitor.py
- **Expected Impact**: A single declarative rule table (id, priority, enabled, params) loaded once and evaluated in a deterministic order makes the effective exit policy auditable and backtestable, and anchoring data paths to the project root (Path(__file__).parents[n]) removes silent write-to-wrong-CWD failures under systemd/cron launch.

## 5. Overall Assessment

- **Code Quality Score**: 6/10
- **Architecture Score**: 6/10
- **Risk Level**: high

**Top Priorities**:
1. Fail-closed broker boundary: stop returning [] for 'unknown' positions (broker_adapter.get_positions) and stop fabricating fills on disconnect (submit_order/_simulate_fill); require explicit opt-in for simulated fills and validate trade_env against the live-account flag at startup.
1. Fix the position SSOT key: PositionStore must be keyed by (symbol, direction, owner) like the symbol lock manager and position_sync, and _pending must not be cleared by the reconciliation/adoption path - otherwise LONG/SHORT legs and duplicate orders silently corrupt state.
1. Close the capital-control loop and its validation gaps: wire capital_allocator.try_reserve/release into order_executor with a real notional, fix the negative-pct and cap==0/unknown-bucket fail-open semantics, and guard the singleton with a lock.

**Summary**: FTSeek-US is a well-instrumented modular monolith with a genuinely thoughtful domain layer: an explicit engine contract, an event store for replay, an owner-classified capital ledger, and volume of hard-won production comments that document prior incidents (re-entry cascade failures, remark-based AUTO claiming, currency-defensive pl_val). The design intent is sound, but the reviewed code shows a recurring pattern where safety mechanisms are present, documented and observably NOT effective at the boundary: capital caps are observe-only and never called from the order path, per-strategy bucket caps treat 0.0/unknown buckets as unlimited, the broker adapter reports 'flat' when disconnected and manufactures fills offline, the last-line notional cap is bypassed by a caller-supplied flag, and several risk gates fail open on exception. Two structural defects concentrate most of the production risk: (1) the position SSOT is symbol-keyed while every other component is (symbol, direction, owner)-aware, so multi-leg state can be silently overwritten; and (2) external-close dedupe and the symbol-lock release are both keyed more coarsely than the entities they guard, which is precisely how a close can vanish from the books or a lock can be released for a holder that never took it. Concurrency hygiene is inconsistent: the capital ledger's own lock is used correctly, but the module-level singleton, the VIX/SPY history lists, and the engine heartbeat are unprotected or unsynchronized, and SQLite I/O is executed while holding a hot lock. Verification limits: only app/core/engine.py, app/core/config.py and app/coordinator/capital_allocator.py were provided in full; order_executor.py, position_sync.py and broker_adapter.py were truncated to headers/selected ranges. Findings in truncated files are based on visible code plus the modules' own comments - notably the symbol-lock decorator (needs the body of _close_position to confirm lock acquisition ownership), the broker get_positions empty-list callers, and the position_sync reconcile connection's commit/close handling should be confirmed against the full sources before fixing. Recommended sequencing: close the broker fail-open paths and the position key first (money-losing, hard to detect), then wire and validate capital enforcement, then address the duplicated exit-rule/enum authorities and the SQLite I/O-in-lock pattern.
