# ZHIPU/GLM-5.3-Flash - FTSeek-US Code Analysis

**Response Time**: 538.9s
**Output Tokens**: 30131
**Reasoning Tokens**: 22536
**Retries**: 0
**Request ID**: a5875876-708b-914a-b7d6-cdf7d3b6ebc9

## 1. Architecture Analysis

**Pattern**: Layered orchestrator monolith. A central TradingScheduler (app/core/scheduler_core.py) coordinates domain modules: data providers (app/data) -> signal pipeline (app/pipeline/signal_pipeline.py) -> risk/coordinator gates (app/risk, app/coordinator/capital_allocator.py) -> order executor (app/pipeline/order_executor.py) -> Futu broker adapter (app/execution/broker_adapter.py) -> position reconciliation (app/core/position_sync.py) and exit monitoring (app/risk/position_exit_monitor.py, app/playbook/exit_engine.py). SQLite is the shared SSOT for trade records, playbook instructions and the capital ledger; an EventStore provides observability. Dependency wiring is a hybrid of a DI container (di_container) and ad-hoc module-level service-locator singletons (get_capital_allocator, get_symbol_lock_manager, get_rejection_logger, _get_pdt_guard). Concurrency is a hybrid of asyncio plus background threads (position_sync 30s loop, heartbeat threads) guarded by threading.RLock/Lock.

**Data Flow**: Market data (Futu via market_data_router/futu_lv3_provider) -> signal_pipeline._process_symbol_impl builds context (VWAP deviation, earnings, VIX regime, war-room fast lane) and produces a SignalScore -> coordinator arbitration + capital gates (theoretically CapitalAllocator.try_reserve) + RiskEngine.pre_trade_check -> order_executor builds Order, computes qty/price, applies PDT guard and symbol locks, then _submit_order_optimized routes to TWAPExecutor or market order -> FutuBrokerAdapter.submit_order (notional sanity check, remark tagging e.g. pb_*) -> fills update PositionStore.add (clears pending set, syncs daily_active_pool) and trade_records -> position_sync thread reconciles Futu SSOT vs local DB every 30s (external-close detection, AUTO claim via remark whitelist) -> position_exit_monitor / exit_engine evaluate exit rules -> _close_position (symbol-locked, decorated by _with_symbol_lock_release) -> CapitalAllocator.release + events/alerts -> dashboards and daily review.

**Main Components**:
- TradingScheduler + PositionStore (app/core/scheduler_core.py) - orchestration and in-memory position SSOT with pending-set duplicate-open guard
- Signal pipeline (app/pipeline/signal_pipeline.py) - scan loop, regime/context enrichment, consensus -> SignalScore
- Order executor (app/pipeline/order_executor.py, 5685 lines) - order construction, risk gates, qty calc, TWAP vs market routing, close logic with symbol locks
- CapitalAllocator (app/coordinator/capital_allocator.py) - bucket-based capital ledger with persist-to-SQLite, observe/enforce modes
- FutuBrokerAdapter (app/execution/broker_adapter.py) - order submission, notional backstop (30% equity), heartbeat/reconnect, currency-pinned position queries
- Position sync (app/core/position_sync.py) - 30s broker-vs-DB reconciliation, AUTO/MANUAL claim via remark whitelist, external-close detection
- Position exit monitor (app/risk/position_exit_monitor.py) + playbook exit engine (app/playbook/exit_engine.py) - ATR stops, scale-out, chandelier, pre-close layering
- PDT guard + symbol lock manager (app/risk/pdt_guard.py, app/execution/symbol_lock_manager.py)
- Engine base classes (app/core/engine.py) - RiskEngine/ExecutionEngine/ObservabilityEngine ABCs with EventStore
- Config container (app/core/config.py -> config_container) - consolidated SSOT after legacy loader removal
- Observability: EventStore, exit quality tracker, web dashboard, review/analytics modules

**Architectural Weaknesses**:
- Hybrid asyncio + threading model with blocking calls: synchronous SQLite commits and broker RPCs can run on the event loop thread or hold locks for seconds (e.g., CapitalAllocator persists under self._lock); no clear ownership of which thread executes which stage.
- Capital enforcement is documented as NOT wired into the main order path (try_reserve never called by order_executor._submit_order; signal.position_pct defaults to 0.0) and allocator defaults to observe_only=True - the entire capital-governance layer is currently advisory.
- Several risk gates fail open by design (VWAP deviation soft-pass, _check_execution_rules pass-on-unparseable, paper-mode simulate-fill on broker disconnect) with no failure counters or alerts.
- Service-locator singletons with unsynchronized lazy init (get_capital_allocator, _get_pdt_guard) - races and silent config mismatch on second call.
- Abstraction-reality drift: app/core/engine.py defines ExecutionEngine/RiskEngine ABCs, but the real executor is module-level functions taking a raw scheduler object; order_book/pending_orders in the base class are unsynchronized and shutdown() drops pending orders without broker cancel.
- God modules: order_executor (5685 lines), position_sync (2853), scheduler_core (3136), position_exit_monitor (2556) - high coupling, large blast radius per change (Task #34 incident originated in this area).
- SQLite as shared SSOT with many writers and per-call connections; WAL/busy_timeout configured only in some modules (capital_allocator) not uniformly via safe_sqlite_connect.
- Relative paths in production code (exit_engine 'data/early_dead_signal_shadow.jsonl' is CWD-dependent while scheduler_core correctly anchors to __file__).
- Reservations in CapitalAllocator have no TTL/expiry; correctness relies on every failure path calling release() (not yet exercised since try_reserve is unwired).

## 2. Bugs Detected (15 issues)

### Bug 1: [MEDIUM] race_condition
- **File**: app/coordinator/capital_allocator.py (lines 636-645 (get_capital_allocator))
- **Description**: Module-level singleton uses an unsynchronized check-then-set on a global. Two threads calling get_capital_allocator concurrently on first use can each construct a CapitalAllocator (one wins the global, both are returned to different callers). Additionally, later callers passing different caps/max_total_pct are silently ignored - the first caller's config wins.
- **Impact**: Two allocators with divergent ledgers could split capital accounting (orders gated by one instance, dashboard/review reading the other), and config drift (e.g., a tightened max_total_pct) is silently dropped.
- **Fix**: Add a module-level _init_lock = threading.Lock() with double-checked locking, and store the first-call kwargs; subsequent calls with conflicting caps/max_total_pct should raise or log an error instead of silently returning the original instance.

### Bug 2: [HIGH] concurrency
- **File**: app/coordinator/capital_allocator.py (lines 163-201, 203-219, 499-525, 580-620 (approx.; try_reserve/release/_record_event hold _lock while calling _persist_state/_persist_state_all/_persist_event))
- **Description**: try_reserve, release, force_set, reset_and_reconcile and reconcile_with_positions mutate the ledger under self._lock and then call _persist_state*, which acquires self._db_lock, opens a NEW sqlite3 connection, and commits (busy timeout 5s). _record_event additionally opens a second connection per event (via _persist_event). Lock ordering is consistent (_lock -> _db_lock, no deadlock), but the capital lock is held across synchronous disk I/O.
- **Impact**: Every capital check on the order hot path can stall for the duration of SQLite I/O/contingion (up to ~5s under lock contention). If invoked from the asyncio loop thread, the entire event loop (quotes, exits, monitors) freezes; watchdog heartbeats and 60s exit-scan cadence can be violated during bursts of orders.
- **Fix**: Keep only the in-memory mutation inside self._lock; persist after releasing it (capture the rows, then write), or push persistence to a single background writer thread / queue. Reuse one long-lived WAL connection instead of per-call connect+commit.

### Bug 3: [HIGH] logic_error
- **File**: app/coordinator/capital_allocator.py (lines 172-176 (try_reserve) and 271-290 (compute_scale_factor))
- **Description**: Bucket cap check is guarded by 'if cap > 0 and current + position_pct > cap'. The module's own docstring defines zero-cap buckets as disabled (example: {"llm_plan": 0.0}), but cap == 0.0 skips the check entirely - a strategy mapped to a 0.0 bucket (or an unknown strategy, since cap defaults to self._caps.get(bucket, 0.0)) faces NO bucket limit, only the global 0.90 total cap. compute_scale_factor has the same inversion: cap == 0 yields bucket_headroom = float('inf').
- **Impact**: Latent today (allocator is observe_only by default), but the moment enforce mode is enabled (planned Phase B), 'disabled' buckets and unmapped strategies can reserve capital freely, silently defeating per-bucket isolation - the exact scenario the allocator exists to prevent.
- **Fix**: Fail closed: treat cap == 0 as 'no capacity' (block any positive reserve) and unknown buckets as cap=0 with a warning log. In compute_scale_factor, use bucket_headroom = max(0.0, cap - current) for cap >= 0 and 0.0 for unknown buckets. Add a unit test pinning zero-cap semantics.

### Bug 4: [MEDIUM] resource_leak
- **File**: app/core/engine.py (lines 125-128)
- **Description**: ExecutionEngine.shutdown() sets enabled=False and calls self.pending_orders.clear() without cancelling those orders at the broker and without persisting order_book.
- **Impact**: On shutdown/restart, orders can remain live at Futu and fill unattended; the restarted process has no in-memory record of them, so fills are only discovered later by the 30s position_sync reconciliation - exactly the class of 'unknown pending order' incidents documented in order_executor Task #34 ('不支持交叉交易, 有待成交订单').
- **Fix**: In shutdown(), iterate pending_orders and call broker cancel_order for each (with timeout), log any that fail to reach a terminal state, and persist a snapshot of order_book for recovery-on-start reconciliation.

### Bug 5: [MEDIUM] concurrency
- **File**: app/core/engine.py (lines 119-128)
- **Description**: order_book (dict) and pending_orders (list) are plain mutable structures with no locking anywhere in the base class, while the surrounding system is explicitly multi-threaded (position_sync thread, scheduler loops, symbol lock manager, heartbeat threads).
- **Impact**: Concurrent submit/query/cancel can lose updates or corrupt the book, producing intermittent, hard-to-reproduce reconciliation anomalies.
- **Fix**: Guard both structures with threading.RLock (or make the execution engine single-threaded on the event loop with asyncio.Lock and document the owning thread).

### Bug 6: [LOW] logic_error
- **File**: app/core/engine.py (lines 36-42)
- **Description**: is_healthy() compares datetime.now() against last_heartbeat, which is only advanced by an external heartbeat() call; BaseEngine never updates it itself, and the 60s threshold is hardcoded (self.config ignored). If no caller pumps heartbeat(), every engine reads as unhealthy 60s after construction; if nothing polls is_healthy(), the check is dead code. (Call sites are outside the provided excerpt - verify before changing.)
- **Impact**: Either false 'unhealthy' alarms that get ignored (alarm fatigue) or a health check that never fires when it should during a stalled loop.
- **Fix**: Have the engine refresh last_heartbeat inside its own periodic work, or have the scheduler ping engines on a timer; make the timeout configurable via self.config.

### Bug 7: [LOW] race_condition
- **File**: app/pipeline/order_executor.py (lines 33-39)
- **Description**: _get_pdt_guard() lazily initializes the module-global _pdt_guard without a lock; two threads entering simultaneously can construct two PDTGuard instances.
- **Impact**: If PDTGuard caches day-trade counters in memory, two instances can disagree and allow a PDT-violating day trade; benign only if it re-reads the DB on every check (PDTGuard source not in the provided context - verify).
- **Fix**: Double-checked locking with a module-level threading.Lock, or construct the guard eagerly at scheduler startup and inject it.

### Bug 8: [MEDIUM] missing_validation
- **File**: app/pipeline/order_executor.py (lines 713-725)
- **Description**: _submit_order_optimized computes notional = float(order.quantity) * float(order.price or signal_price or 0.0). For MARKET orders price is None/0, so the notional falls back to signal_price, which may be minutes stale (or 0.0, making notional 0). The TWAP-vs-market decision (should_use_twap) and any notional-dependent behavior therefore run on a stale or zero estimate. Within the shown excerpt there is also no validation that quantity > 0 or notional > 0 before submission.
- **Impact**: Large orders can silently take the wrong execution path (market instead of TWAP, or vice versa) based on a stale quote; notional=0 bypasses size-based routing entirely; a zero/negative quantity would propagate toward the broker call.
- **Fix**: Re-quote the current price at submission time for the notional estimate and record the quote used in exec_metadata (slippage must be measured against a fresh reference); raise/log a rejection when quantity <= 0 or (market order and notional <= 0).

### Bug 9: [MEDIUM] error_handling
- **File**: app/pipeline/signal_pipeline.py (lines 21-36)
- **Description**: _get_vwap_deviation swallows all exceptions and returns None; per its own docstring, pre_trade_check then 'soft-passes' (软放行). A data-provider outage silently disables this risk gate, and nothing distinguishes 'gate passed' from 'gate unavailable'.
- **Impact**: During quote/context outages - precisely when prices are most disorderly - signals proceed without the VWAP-deviation check, with no telemetry or alert.
- **Fix**: Keep the soft-pass only with explicit instrumentation: increment a 'gate_unavailable' counter, emit an alert after N consecutive misses per symbol, and consider fail-closed for high-notional or volatile-regime signals.

### Bug 10: [MEDIUM] concurrency
- **File**: app/pipeline/signal_pipeline.py (lines 47-56)
- **Description**: _VIX_HISTORY and _SPY_HISTORY are module-level lists shared across scan threads with no lock (mutation sites are beyond the shown excerpt - verify). Even though list.append is GIL-atomic, the typical windowed prune-vs-iterate pattern can drop samples or raise during concurrent pruning.
- **Impact**: The Rapid Regime Change detector (P0 2026-06-17) can miss or misread samples during fast markets, delaying de-risking triggers exactly when they matter.
- **Fix**: Replace with collections.deque(maxlen=_HISTORY_MAX_ENTRIES) per series and prune by timestamp at read time, or wrap mutations in a small threading.Lock; add a unit test with concurrent producers.

### Bug 11: [MEDIUM] missing_validation
- **File**: app/execution/broker_adapter.py (lines 36-42)
- **Description**: FUTU_UNLOCK_PWD plaintext fallback is read from the environment at init and kept as an instance attribute. Env vars leak via /proc/<pid>/environ, crash dumps and are easy to log accidentally; there is also no visible validation that the unlock actually succeeded before live order submission.
- **Impact**: Real-money trading credential exposure; a stale/missing unlock can also surface only mid-day as order rejections at the worst time.
- **Fix**: Drop the plaintext fallback (MD5-only), load the secret from a secret manager or a root-readable file, never log either value, and verify unlock at connect time with an explicit health flag surfaced to the dashboard.

### Bug 12: [MEDIUM] error_handling
- **File**: app/execution/broker_adapter.py (lines 474-486)
- **Description**: In SIMULATE env, submit_order returns self._simulate_fill(order) when is_connected() is False. The DEB-20260903 #97 fix correctly protects the REAL env, but in paper mode a disconnect now fabricates fills: strategies 'fill', exits trigger, and slippage/latency metadata is synthetic.
- **Impact**: Connectivity incidents are masked during paper trading (the staging environment for go-live); if AUTO_TRADE_ACCOUNT/trade_env configuration drifts, this path silently consumes signals with fake fills and no alert.
- **Fix**: Return a rejected Order with reason BROKER_DISCONNECTED and emit an alert instead of simulating on disconnect; keep simulated fills only behind an explicit dry-run flag, and add a startup assertion binding trade_env to AUTO_TRADE_ACCOUNT.

### Bug 13: [LOW] resource_leak
- **File**: app/core/position_sync.py (lines 20-21)
- **Description**: _SELF_MANAGED_SKIP_LOGGED is a module-level set that grows with every distinct symbol seen and is never pruned; its dedup semantics also span the whole process lifetime rather than per day.
- **Impact**: Slow unbounded memory growth in a long-lived process, and 'first occurrence' log dedup that suppresses context days later after a restart-free week.
- **Fix**: Key entries by (trade_date, symbol) and prune dates older than today each sync cycle, or use a bounded LRU.

### Bug 14: [MEDIUM] missing_validation
- **File**: app/coordinator/capital_allocator.py (lines 395-435 (auto_capacity_report position loop))
- **Description**: The position loop uses defensive getattr chains with 0 defaults: getattr(p, 'size', 0), getattr(p, 'current_price', 0), getattr(p, 'avg_entry_price', 0). If the caller passes broker dicts (which per get_positions use 'qty'/'cost_price' keys) or Position objects with different field names, size and price silently evaluate to 0 and market value becomes 0. (Caller types are outside the provided excerpt - verify.)
- **Impact**: auto exposure is undercounted and auto_headroom overstated, so enforce-mode decisions and dashboard reports look healthy while the book is concentrated - silent over-allocation.
- **Fix**: Accept an explicit protocol/dataclass for positions (or normalize broker dicts before the call), log-and-count positions that lack required attributes instead of silently zeroing, and cross-check report totals against total_equity (raise if current_auto_exposure > total_equity).

### Bug 15: [LOW] logic_error
- **File**: app/playbook/exit_engine.py (lines 30-33)
- **Description**: _EARLY_DEAD_SHADOW_PATH = Path("data/early_dead_signal_shadow.jsonl") resolves against the process CWD, while other modules (scheduler_core) correctly anchor paths to __file__-derived BASE_DIR/PROJECT_ROOT.
- **Impact**: Depending on launch directory (start.sh, systemd, manual runs), the P7 shadow telemetry is written to the wrong location or fails, silently corrupting the early-dead-signal analysis the seam was built for.
- **Fix**: Anchor to the project root: Path(__file__).resolve().parents[2] / "data" / "early_dead_signal_shadow.jsonl" (mirroring scheduler_core's BASE_DIR pattern), and audit other relative 'data/' paths for the same issue.

## 3. Logic Issues (8 issues)

### Issue 1: [CRITICAL] business_logic
- **File**: app/coordinator/capital_allocator.py (lines 137-166 (try_reserve docstring) + app/pipeline/order_executor.py)
- **Description**: The code itself documents that the main order path (order_executor._submit_order) never calls try_reserve, and signal.position_pct defaults to 0.0, so reserved amounts are 0. Combined with observe_only=True as the default, every bucket cap and the 0.90 total cap are advisory only in production - the capital-governance layer is effectively non-functional.
- **Impact**: No hard limit on strategy concentration: a runaway signal loop or strategy bug can commit the whole account; the only effective backstops are the broker adapter's 30% notional sanity check and upstream heuristics.
- **Fix**: Execute the documented Phase B plan: compute order qty first, derive position_pct from notional/equity, call try_reserve inside try/except before broker submission, release in finally on rejection/failure, run reset_and_reconcile + reconcile_with_positions, then flip observe_only=False; add a reserved-vs-actual drift metric.

### Issue 2: [MEDIUM] edge_case
- **File**: app/coordinator/capital_allocator.py (lines 203-212 (release))
- **Description**: release() clamps negative results with max(0.0, ...) silently. A double-release or a release with the wrong magnitude never surfaces as an anomaly.
- **Impact**: Ledger drift accumulates quietly until the periodic reconcile; in enforce mode drift causes false rejections (blocking good trades) or false headroom (over-allocation).
- **Fix**: When the un-clamped value would go negative, log a warning and record an event with decision='RELEASE_OVERFLOW' (and feed it into reconcile alerting) instead of silently clamping.

### Issue 3: [MEDIUM] business_logic
- **File**: app/coordinator/capital_allocator.py (lines 137-212 (try_reserve/release contract))
- **Description**: Reservations have no id, no TTL and no automatic expiry; correctness depends on every failure/retry/TWAP-cancel path remembering to call release(). The order path involves exceptions, retries without backoff (per Task #34 history) and cancel_active_twap flows - a single missed release permanently consumes bucket capacity.
- **Impact**: Silent capacity leak: a strategy gradually loses allocation until the next reconcile, or reconcile over-corrects and creates a transient over-allocation window.
- **Fix**: Return a reservation handle with a TTL (auto-release after N minutes unless confirmed filled), hook release to order terminal states (FILLED_ALL/CANCELLED/REJECTED), and unit-test exception paths for reserve/release pairing.

### Issue 4: [MEDIUM] missing_validation
- **File**: app/pipeline/signal_pipeline.py (lines 63-100 (_check_execution_rules))
- **Description**: The gate returns True when rules are missing OR when context values are unparseable ('规则缺失或无法判定时一律放行'). 'No rules defined' and 'rules present but unevaluable due to bad data' are treated identically.
- **Impact**: Malformed playbook/plan payloads silently skip execution constraints (e.g., price-band checks), producing entries outside the intended conditions with no signal that a gate was skipped.
- **Fix**: Distinguish the two cases: missing rules -> pass (log debug); rules present but context invalid -> block and alert (or at minimum count and alert after N occurrences); validate context value types at context-build time.

### Issue 5: [LOW] business_logic
- **File**: app/playbook/exit_engine.py (lines 46-66 (_is_overnight_position) and OVERNIGHT_GAP_STOP rule)
- **Description**: As the authors' own 2026-09-24 erratum notes, OVERNIGHT_GAP_STOP never verifies an actual gap: the predicate is only 'entry date < today AND loss >= threshold'. note_exit_gap (RFC-106 §5.2) is record-only. Additionally, parse failures return False (conservative), so a malformed entry_time silently disables the rule for that position.
- **Impact**: Overnight positions can be force-closed on ordinary intraday drift as if it were a gap event; malformed timestamps silently opt positions out of the fast-loss exit.
- **Fix**: Once note_exit_gap has collected evidence, add an explicit gap predicate (today_open vs prev_close beyond k x ATR) or rename the rule to OVERNIGHT_LOSS_STOP to align semantics; log (once per symbol) when entry_time parsing fails.

### Issue 6: [LOW] edge_case
- **File**: app/coordinator/capital_allocator.py (lines 117-127 + 560-578)
- **Description**: On restart, persisted bucket/strategy allocations are restored immediately in __init__, but manual/legacy pools start at 0.0 until StrategyCoordinator.reconcile() runs. In enforce mode, any reserve processed in that window computes auto_pool too large (max_total - 0 - 0).
- **Impact**: A brief post-restart over-allocation window in enforce mode (currently latent while observe_only=True, but will matter after Phase B).
- **Fix**: Gate enforce-mode try_reserve on a 'reconciled' flag set after the first reconcile_with_positions/set_owner_pools call (fail-closed or observe-behavior until then), or persist and restore manual/legacy pools too.

### Issue 7: [LOW] business_logic
- **File**: app/coordinator/capital_allocator.py (lines 221-248 (snapshot))
- **Description**: snapshot().by_strategy actually returns bucket-level allocations (dict(self._allocations)) - the docstring admits it for backward compatibility. Dashboard/daily_review consumers reading 'by_strategy' will misattribute exposure when bucket_map merges strategies (e.g., fast_lane -> main).
- **Impact**: Misleading dashboards and reviews during incidents; operators may manually intervene based on the wrong strategy-level exposure.
- **Fix**: Expose both views explicitly (by_bucket and by_strategy via by_strategy_snapshot()), migrate consumers, and deprecate the ambiguous field.

### Issue 8: [LOW] missing_validation
- **File**: app/core/position_sync.py (lines 350-382 (_reconcile_playbook_execution_states))
- **Description**: NEEDS MORE CONTEXT: the excerpt opens safe_sqlite_connect and enters try, but is truncated before the finally block - it cannot be confirmed that conn.close() runs on all exception paths, and PlaybookDB is constructed over the same raw conn (ownership of closing is unclear). Flagged for verification, not asserted as a definite leak.
- **Impact**: If the connection is not closed on exceptions, the long-running 30s sync thread leaks file descriptors/locks over time.
- **Fix**: Ensure try/finally conn.close() (or a context manager) and make PlaybookDB explicitly not own/close a caller-supplied connection; verify with a long-run soak test.

## 4. Optimization Suggestions (8 suggestions)

### 1. [performance] Move SQLite persistence out of the self._lock critical section: capture rows und
- **File**: app/coordinator/capital_allocator.py
- **Expected Impact**: p99 try_reserve latency drops from potentially seconds (SQLite busy/contended) to microseconds; eliminates event-loop stall risk on the order hot path.

### 2. [performance] Throttle _persist_event's pruning DELETE ('WHERE id <= MAX(id) - 5000') which cu
- **File**: app/coordinator/capital_allocator.py
- **Expected Impact**: Roughly halves write transactions per reserve and reduces WAL churn/vacuum pressure on ftseek_us.db.

### 3. [maintainability] Encapsulate _VIX_HISTORY/_SPY_HISTORY in a small thread-safe ring-buffer utility
- **File**: app/pipeline/signal_pipeline.py
- **Expected Impact**: Removes a class of concurrency bugs in the rapid-regime-change detector and bounds memory; makes the buffer unit-testable in isolation.

### 4. [maintainability] Split the 5685-line module into order construction, risk-gate orchestration, sub
- **File**: app/pipeline/order_executor.py
- **Expected Impact**: Smaller review surface per change, independently testable units, fewer cross-path regressions in the highest-risk money path.

### 5. [maintainability] Replace Dict[str, Any] config with typed dataclasses/pydantic models in BaseEngi
- **File**: app/core/engine.py
- **Expected Impact**: Catches config-key typos at startup, reduces abstraction-reality drift that currently misleads contributors about where execution logic lives.

### 6. [scalability] Unify SQLite access policy: WAL + busy_timeout + bounded retry for ALL connectio
- **File**: app/core/db_utils.py + app/storage/
- **Expected Impact**: Fewer SQLITE_BUSY/'database is locked' errors during concurrent position_sync / exit-monitor / playbook / dashboard write cycles.

### 7. [maintainability] Migrate ad-hoc lazy singletons (get_capital_allocator, get_symbol_lock_manager, 
- **File**: app/core/di_container.py
- **Expected Impact**: Deterministic initialization order, elimination of lazy-init races, and easier test isolation for the money-path components.

### 8. [maintainability] Extend the 'pinned invariant' test pattern (already used for EXTERNAL_CLOSE_DUP_
- **File**: tests/
- **Expected Impact**: Prevents regressions in the highest-risk guards; the pattern has already proven its worth by catching the external-close data-loss bug class.

## 5. Overall Assessment

- **Code Quality Score**: 6/10
- **Architecture Score**: 6/10
- **Risk Level**: high

**Top Priorities**:
1. Wire capital enforcement into the real order path (Phase B): compute position_pct from actual qty/notional, call try_reserve before broker submission with guaranteed release-on-failure, reconcile, then flip observe_only=False - and fix the zero-cap/unknown-bucket guard inversion (capital_allocator.py) BEFORE flipping to enforce, otherwise disabled buckets become unlimited.
1. Remove blocking SQLite I/O from CapitalAllocator's self._lock critical section (write-behind or dedicated writer connection) and make its singleton thread-safe - capital checks sit on the order hot path and can currently stall the loop/lock for seconds.
1. Audit and remediate fail-open behavior across risk gates for live trading: VWAP-deviation soft-pass and _check_execution_rules pass-on-unparseable (signal_pipeline), and paper-mode simulate-fill on broker disconnect (broker_adapter) - make them fail-closed or at minimum loudly instrumented with counters and alerts.

**Summary**: FTSeek-US shows strong incident-driven maturity: the code is annotated with concrete post-mortems (Task #34 close-path cascade, DEB-20260903 #97 disconnect safety, RFC-080 remark whitelist with an explicitly fail-safe direction, H7 ledger-booking fix), and good defensive patterns are in place (symbol-lock release decorator, PositionStore pending-set against duplicate opens, pinned SQL invariants in tests, currency pinning on position queries, fail-closed real-env disconnect handling). However, the system's capital-governance layer is currently decorative: the allocator's own docstring admits the main order path never calls try_reserve and position_pct defaults to 0.0, with observe_only=True on top - so no bucket or total cap is actually enforced on real orders. Combined with several fail-open risk gates, this means the effective safety net in production is thinner than the architecture suggests. Structurally, the hybrid asyncio-plus-threads model with blocking SQLite I/O held under locks (capital_allocator persists inside its critical section) is the most likely source of the next latency incident, and god-modules (order_executor 5685 lines, position_sync 2853) concentrate blast radius. Caveat: several key files were provided only as excerpts (order_executor, position_sync, position_exit_monitor, broker_adapter internals); findings above are grounded in the provided code, and the two entries explicitly marked 'needs more context' (PositionStore pending cleanup on exception paths, _reconcile_playbook_execution_states conn lifecycle) require the full source to confirm.
