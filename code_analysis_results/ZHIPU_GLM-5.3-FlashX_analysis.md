# ZHIPU/GLM-5.3-FlashX - FTSeek-US Code Analysis

**Response Time**: 164.5s
**Output Tokens**: 19288
**Reasoning Tokens**: 11929
**Retries**: 0
**Request ID**: 9ec13991-59aa-92d5-b50a-4e37984d545f

## 1. Architecture Analysis

**Pattern**: Layered, event-driven trading monolith. An asyncio TradingScheduler (app/core/scheduler_core.py) drives a pipeline chain: signal_pipeline (scan -> _process_symbol_impl -> SignalScore) -> coordinator (signal_arbitrator, capital_allocator) -> pipeline/order_executor (risk gates, qty calc, TWAP/market routing) -> execution/broker_adapter (Futu OpenAPI). A 30s background thread (app/core/position_sync.py) reconciles local SQLite/PositionStore state against Futu as SSOT. Risk is layered: position_exit_monitor (rule-based exits), playbook/exit_engine (self-managed exit rule chain), RiskEngine/ExecutionEngine/ObservabilityEngine ABCs in app/core/engine.py. Persistence is SQLite (WAL) with trade_records/open_positions/playbook_instructions tables. Incident-driven evolution is visible everywhere: dated inline RFC/DEB/P0 annotations document post-mortems baked into code comments.

**Data Flow**: scan_loop -> _run_signal_scan_impl -> _process_symbol_impl: fetch quotes/context -> signal generation (technical intraday / LLM voting) -> index linkage + pyramid checks -> SignalScore -> pre-trade gates (VWAP deviation, PDTGuard, signal arbitrator, manual-hold registry; CapitalAllocator is NOT yet called here) -> order_executor builds Order and computes qty -> _submit_order_optimized routes to TWAPExecutor or market order -> FutuBrokerAdapter.submit_order (notional sanity check -> Futu API, remark=client_order_id for attribution) -> fill booking into PositionStore + trade_records -> position_exit_monitor and playbook exit_engine continuously evaluate exit rules against quotes -> _close_position (symbol-locked, broker position refresh, open-order wait) submits close order -> position_sync thread every 30s reconciles Futu positions vs local DB, detects external closes via remark whitelist, upserts trade records, and reconciles playbook instruction states.

**Main Components**:
- TradingScheduler + PositionStore (app/core/scheduler_core.py) — in-memory position SSOT with pending-set dedup
- signal_pipeline (app/pipeline/signal_pipeline.py) — market gating, signal generation, consensus/voting, pre-trade checks
- order_executor (app/pipeline/order_executor.py, 5685 lines) — order construction, PDT guard, symbol locks, close flows, notifications
- CapitalAllocator (app/coordinator/capital_allocator.py) — bucketed capital ledger with observe/enforce modes and SQLite persistence
- FutuBrokerAdapter (app/execution/broker_adapter.py) — order submission, cancellation, positions, heartbeat/reconnect, notional hard cap
- position_exit_monitor (app/risk/) + playbook exit_engine — multi-rule exit engine (ATR trailing, scale-out, pre-close, time stop)
- position_sync (app/core/position_sync.py) — 30s reconciliation thread, external-close detection, remark-based AUTO/MANUAL attribution
- EventStore / BaseEngine ABCs (app/core/engine.py) — event, alert, failure recording
- TradeStore / SQLite storage layer (app/storage/)
- web_dashboard, review, llm/agents subsystems

**Architectural Weaknesses**:
- God modules: order_executor.py (5685 lines) and historically playbook/pipeline.py (9453 lines, now being split) concentrate gating, sizing, closing, notification in one file — the file header itself documents a cascading-failure incident caused by this coupling
- Scheduler passed as a god-object parameter into pipeline functions (_submit_order_optimized(scheduler, ...)), creating tight coupling and untestable seams
- Module-level lazy singletons (get_capital_allocator, _get_pdt_guard) bypass the existing DI container (di_container) and are not thread-safe in a mixed asyncio + raw-thread runtime
- Mixed concurrency models: asyncio event loop plus background threads (position_sync, heartbeats) sharing SQLite and in-memory stores; correctness relies on hand-documented lock ordering (e.g., PositionStore._lock -> DailyActivePool._lock)
- Order attribution depends on fragile string remark parsing (pb_/orb_/OVER_LIMIT_TRIM prefixes) as the sole AUTO-claim credential — acknowledged by RFC-080 as having previously caused real-money manual positions to be auto-closed
- Error states conflated with empty results in broker adapter (get_positions returns [] when disconnected; cancel_order returns True on unknown post-cancel status)
- Capital enforcement gap (documented): order path never calls try_reserve and signal.position_pct defaults to 0.0, so caps are advisory even in enforce mode (Phase A)
- Core engine ABCs (RiskEngine/ExecutionEngine) appear partially aspirational — actual logic lives in scheduler functions, so the abstractions add indirection without enforcement

## 2. Bugs Detected (12 issues)

### Bug 1: [HIGH] race_condition
- **File**: app/coordinator/capital_allocator.py (lines 620-645)
- **Description**: get_capital_allocator uses check-then-act lazy singleton (if _singleton is None: _singleton = CapitalAllocator(...)) with no lock. position_sync runs as a background thread while asyncio tasks, dashboard, and review modules may all first-touch the allocator concurrently. Two threads can both see None and each construct an allocator; only one wins the module global, and any try_reserve/release calls that landed on the loser instance are silently lost, splitting the capital ledger.
- **Impact**: Split-brain capital ledger: reservations recorded in an orphaned instance vanish, understating exposure. If/when enforce mode goes live, caps computed from a partially-populated ledger allow over-allocation of real capital.
- **Fix**: Add a module-level lock with double-checked locking: _singleton_lock = threading.Lock(); def get_capital_allocator(...): global _singleton; if _singleton is None: with _singleton_lock: if _singleton is None: ...validate caps...; _singleton = CapitalAllocator(...). Better: construct eagerly at startup inside the DI container and inject it.

### Bug 2: [HIGH] race_condition
- **File**: app/pipeline/order_executor.py (lines 36-43)
- **Description**: _get_pdt_guard() lazily constructs a module-global PDTGuard with no lock (same check-then-act pattern). The comment explicitly says lazy init exists because the constructor performs DB access, meaning construction is slow — widening the race window. Signal pipeline tasks and the position_sync/monitor threads can race the first call.
- **Impact**: Two PDTGuard instances each hold their own day-trade counters/state. Day-trade counting can be split across instances, so the guard under-counts trades and can permit Pattern Day Trader rule violations on a real account (broker restrictions, forced liquidations).
- **Fix**: def _get_pdt_guard(): global _pdt_guard; if _pdt_guard is None: with threading.Lock(): (double-check) _pdt_guard = PDTGuard(); return _pdt_guard — or initialize PDTGuard eagerly during scheduler startup and store it on the scheduler/DI container.

### Bug 3: [MEDIUM] concurrency
- **File**: app/coordinator/capital_allocator.py (lines 146-190, 192-199, 486-505, 558-617)
- **Description**: try_reserve and release call _record_event (which opens a new SQLite connection and inserts via _persist_event) and then _persist_state (another new SQLite connection + commit) while holding self._lock. Every reserve/release in the order path serializes behind up to two synchronous disk transactions; a slow disk or lock contention on the DB file stalls every capital check and therefore the order path.
- **Impact**: Order-path tail latency spikes during bursts or disk stalls; all strategies queue behind one strategy's persistence. In fast markets this delays submissions by an unbounded amount.
- **Fix**: Snapshot the values under self._lock, then persist after releasing it: with self._lock: ...update book...; pending_rows = [...]  / then self._persist_rows(pending_rows) outside the lock. Additionally reuse a single long-lived sqlite connection (check_same_thread=False) guarded by _db_lock, and move the ledger-prune DELETE to a periodic job instead of per-event.

### Bug 4: [HIGH] logic_error
- **File**: app/core/position_sync.py (lines 40-90)
- **Description**: EXTERNAL_CLOSE_DUP_SQL = 'SELECT id, entry_time FROM trade_records WHERE symbol=? AND direction=? AND exit_price > 0' and the equivalent lookup inside upsert_external_close_trade_record dedup on symbol+direction+exit_price>0 with NO correlation to entry_time or the specific position being closed. After any completed round-trip of a symbol/direction, a subsequent genuine external close (re-entry cycle) matches the historical closed row and is classified as a duplicate. CONFIRM: the guard body at ~line 500 and upsert continuation are truncated, but both shown snippets use this uncorrelated predicate.
- **Impact**: Exactly the failure mode the 2026-08-28 fix comment describes: '真实券商平仓被跳过 INSERT 与通知, 而 open_positions 的 DELETE 照常执行 -> 该笔平仓在系统里彻底消失' — a real broker close is never booked (no PnL record, no notification) while the local position disappears, corrupting realized-PnL attribution and review.
- **Fix**: Correlate the dedup to the position identity: pass entry_time (and ideally quantity) into the check — 'WHERE symbol=? AND direction=? AND exit_price>0 AND entry_time=?' — or resolve the open position row id first and dedup on (open_position_id, close_order_id). Add a pinned test: same symbol+direction closed twice in one day must produce two trade_records rows.

### Bug 5: [HIGH] error_handling
- **File**: app/execution/broker_adapter.py (lines 785-795)
- **Description**: get_positions() returns [] when is_connected() is False ('未连接，返回空持仓'). This conflates 'cannot query broker' with 'broker has no positions'. Given position_sync treats Futu as SSOT for reconciliation and external-close detection, an empty list during a transient disconnect is indistinguishable from a full liquidation. CONFIRM: consumer code for this method is truncated; if every reconcile caller checks is_connected() first, downgrade to low.
- **Impact**: A transient OpenD disconnect during a reconcile cycle can make the sync layer believe all positions were externally closed: phantom close bookings, false realized-PnL records, spurious notifications, or (worse) reconcile 'corrections' that zero the capital ledger.
- **Fix**: Change the contract: return Optional[List[Dict]] (None when disconnected/unavailable) or raise BrokerUnavailableError, and make reconcile loops skip the cycle on None with a warning. Keep [] strictly meaning 'broker confirmed zero positions'.

### Bug 6: [MEDIUM] resource_leak
- **File**: app/core/engine.py (lines 126-137)
- **Description**: ExecutionEngine.shutdown() sets enabled=False and calls self.pending_orders.clear() without cancelling those orders at the broker or persisting them. Orders still live at Futu are dropped from local tracking entirely.
- **Impact**: On shutdown/restart, in-flight orders are orphaned: the engine no longer tracks fills/cancels, recovery depends entirely on position_sync's 30s remark-based reconciliation, and until then re-entry guards (e.g., '不支持交叉交易, 有待成交订单' incident in the order_executor header) can block or duplicate trading.
- **Fix**: In shutdown(): for each pending order, attempt broker_adapter.cancel_order(order.id) with a short timeout; for anything not reaching terminal state, persist (order_id, symbol, state=PENDING_ORPHAN) to the order table before clearing, and have startup recovery re-query those order ids via query_order.

### Bug 7: [LOW] logic_error
- **File**: app/coordinator/capital_allocator.py (lines 539-556)
- **Description**: _load_persist_state only restores persisted buckets where 'key in self._allocations' (i.e., keys present in the current caps config). If the caps config changed between runs, persisted allocations for removed/renamed buckets are silently discarded, while _by_strategy rows are restored unconditionally.
- **Impact**: After a config change + restart, total reserved capital is understated by the dropped buckets; reconcile-with-positions will eventually correct it, but in the window between restart and the first reconcile the ledger lies.
- **Fix**: Load all persisted bucket rows first; for keys not in caps, log a warning and either add them to the ledger as an 'orphan' bucket or refuse to start until reconciled. Assert sum(restored) matches expectations or schedule an immediate reconcile_with_positions at startup.

### Bug 8: [MEDIUM] missing_validation
- **File**: app/coordinator/capital_allocator.py (lines 625-645)
- **Description**: get_capital_allocator silently ignores caps/max_total_pct/observe_only/db_path arguments after the first construction. A second call site (e.g., dashboard, review, tests, a second strategy group) requesting different caps or observe_only=False receives the first caller's configuration without any signal.
- **Impact**: A subsystem that believes it flipped enforce mode or tightened caps is actually still on the original config; enforce-mode rollout (the stated Phase B goal) can silently not happen for parts of the system.
- **Fix**: After the singleton exists, compare requested params to the live instance and raise or log a loud error on mismatch; or accept params only on first call and force later callers to use accessor/setter methods (set_observe_only already exists).

### Bug 9: [LOW] concurrency
- **File**: app/pipeline/signal_pipeline.py (lines 46-52)
- **Description**: _VIX_HISTORY and _SPY_HISTORY are module-level plain lists appended and trimmed (check len > _HISTORY_MAX_ENTRIES then re-slice). CONFIRM: writer call sites are truncated; with the scan loop being multi-batch and position_sync/monitor running as threads, writers may not all be on one event-loop thread, so append-then-trim sequences can interleave and drop or duplicate samples.
- **Impact**: check_rapid_regime_change (the P0 2026-06-17 fast-regime detector) can miss or misread a rapid VIX/SPY spike exactly when data is arriving fastest — the moment the check matters most.
- **Fix**: Use collections.deque(maxlen=_HISTORY_MAX_ENTRIES) for O(1) thread-safe appends, prune by timestamp on read, and if writers span threads, wrap mutations in a small lock or funnel samples through a single writer task.

### Bug 10: [LOW] missing_validation
- **File**: app/pipeline/order_executor.py (lines 45-64)
- **Description**: _with_symbol_lock_release releases with key (position.symbol, position.direction.value, owner or 'main'). CONFIRM: the acquisition-side key construction is truncated. If the lock was acquired with a different direction serialization (e.g., str(Direction.LONG) vs 'long') or the position object lost owner_strategy before close, the release targets a different key than the acquire, leaving the original lock held forever.
- **Impact**: A leaked symbol lock permanently blocks future entries/closes for that symbol (or, if the manager tolerates unknown keys silently, hides the leak until manual restart).
- **Fix**: Have get_symbol_lock_manager() return the exact key/token handed out at acquire time (context-manager or token-based release) and log a warning when releasing a key that is not currently held; add a periodic lock-leak detector that reports locks held > N minutes.

### Bug 11: [LOW] logic_error
- **File**: app/playbook/exit_engine.py (lines 36-38)
- **Description**: _EARLY_DEAD_SHADOW_PATH = Path("data/early_dead_signal_shadow.jsonl") is a CWD-relative path. The service is deployed under /opt/ftseek/FTSeek-US (per truncated absolute paths) likely via start.sh/systemd; any WorkingDirectory change silently redirects or breaks shadow-trace writes.
- **Impact**: P7 shadow-path evidence for early-dead signals is written to an unexpected location or write fails, degrading the post-mortem pipeline that this team demonstrably relies on (shadow_registry, daily_review).
- **Fix**: Anchor to the project root: DATA_DIR = Path(__file__).resolve().parents[2] / 'data' (or a config-provided absolute data dir), and create parent dirs on write.

### Bug 12: [LOW] logic_error
- **File**: app/core/engine.py (lines 31-45)
- **Description**: is_healthy() compares (datetime.now() - last_heartbeat).total_seconds() < 60 using naive wall-clock time, and last_heartbeat is only advanced if something calls heartbeat(). Nothing in the shown code owns a heartbeat schedule, so an engine that does real work but is never heartbeated flips to unhealthy 60s after construction; conversely NTP clock jumps distort the check.
- **Impact**: False 'engine unhealthy' states can trigger spurious alerts or, if any supervisor disables unhealthy engines, silently disable live risk/execution logic; wall-clock jumps (DST/NTP) create both false-healthy and false-unhealthy windows.
- **Fix**: Use time.monotonic() for last_heartbeat, and give BaseEngine an explicit heartbeat contract: either the scheduler loop calls heartbeat() on a fixed cadence for every engine, or is_healthy() falls back to subclass-provided liveness signals.

## 3. Logic Issues (8 issues)

### Issue 1: [CRITICAL] business_logic
- **File**: app/coordinator/capital_allocator.py (lines 105-190)
- **Description**: The module docstring states the enforce path is not wired: '_submit_order 下单前 尚未调用此方法' and 'signal.position_pct 默认为 0.0, 导致预留金额为 0'. So even with observe_only=False, every reserve is for 0% and caps constrain nothing; the ledger only becomes truthful via periodic reconcile_with_positions.
- **Impact**: There is currently no enforced per-strategy or total capital cap in the live order path — the single most important guard against runaway exposure in a multi-strategy real-money system is advisory-only. Any signal flood or strategy bug can allocate far beyond intended 80%/90% limits until a broker-side or reconcile correction.
- **Fix**: Phase B as documented, concretely: in order_executor's order build (before _submit_order_optimized), compute position_pct = (qty * price) / total_equity from the final qty (not the default signal field), call try_reserve(strategy, pct), and abort the order on False; call release(strategy, pct) in every close path (including _close_position finally and external-close handling); then run reset_and_reconcile(positions_by_strategy) once and flip observe_only=False. Add an integration test: 100 simultaneous max-size signals must not exceed max_total_pct.

### Issue 2: [HIGH] business_logic
- **File**: app/coordinator/capital_allocator.py (lines 160-186)
- **Description**: In observe_only mode, BLOCKED decisions log a warning and return True WITHOUT recording the reservation (H7). Combined with the unwired order path above, the system can run over caps indefinitely between 30s reconciles and the BLOCKED warnings are the only signal — no counter, no alert escalation, no time-box on Phase A.
- **Impact**: During the (possibly indefinite) Phase A window, real exposure can exceed max_total with only log-line visibility; if logs are not actively alerted on, over-allocation is discovered at reconcile or, worst case, at a margin call.
- **Fix**: Add a BLOCKED-event counter exposed via snapshot()/metrics and wire it to the alerting pipeline (AlertLevel threshold, e.g., >N blocks/hour); set an explicit deadline/kill-switch that flips observe_only to enforce after reset_and_reconcile, enforced at startup.

### Issue 3: [MEDIUM] edge_case
- **File**: app/execution/broker_adapter.py (lines 600-632)
- **Description**: cancel_order returns True when the post-cancel status query errors or returns a non-terminal state ('其余/查询异常 -> True+warning'), i.e., unconfirmed cancels are reported as success. The comment acknowledges this and defers terminal-state confirmation to position_sync's 30s sweep.
- **Impact**: In the cancel-vs-fill race, callers treat the order as cancelled and may immediately resubmit (e.g., replace-stop flows), briefly doubling exposure until the 30s reconcile notices the fill. The order_executor header documents a related live incident ('不支持交叉交易, 有待成交订单').
- **Fix**: Return a tri-state (CANCELLED / FAILED / UNKNOWN). On UNKNOWN, mark the order 'cancel_unconfirmed' and have the symbol lock / pending-set / re-entry guard for that symbol stay engaged until position_sync observes a terminal state, rather than optimistically allowing re-entry.

### Issue 4: [MEDIUM] missing_validation
- **File**: app/execution/broker_adapter.py (lines 78-100)
- **Description**: _notional_sanity_check unconditionally returns True when is_close=True (P0 2026-06-16, added so FLAT_ALL emergencies are not blocked). There is no verification that the order being passed actually closes an existing position — any caller bug that labels an open order is_close=True bypasses the 30% equity hard cap entirely.
- **Impact**: A single mislabeled flag on the 'last wall' guard converts the safety net into an always-open gate for arbitrary notional sizes on real money.
- **Fix**: Validate the close claim cheaply before bypassing: confirm the symbol has a broker/local position and that order.quantity <= held quantity (abs) before returning True for is_close; otherwise fall through to the normal notional check. Log every is_close bypass with position id for audit.

### Issue 5: [MEDIUM] concurrency
- **File**: app/core/scheduler_core.py (lines 90-130)
- **Description**: PositionStore uses a _pending: Set[str] to prevent duplicate entries during the broker_sync 5s debounce window; add() discards the symbol from _pending on success. CONFIRM: the failure-path removal (order rejected/exception after pending insertion) is not shown in the truncated file. If any submission path adds to _pending and then raises before add() or an explicit discard, the symbol is stuck in _pending with no TTL.
- **Impact**: A single failed submission permanently (until process restart) blocks all new signals for that symbol — silent loss of trading opportunities, and because _pending is in-memory, the symptom disappears on restart, making it hard to diagnose.
- **Fix**: Guarantee release with try/finally around the entire submit path: pending_add(symbol); try: submit; finally: pending_discard(symbol) (add() also discards, idempotent). Add a timestamp to pending entries and a sweeper that discards entries older than e.g. 60s with a warning log.

### Issue 6: [LOW] edge_case
- **File**: app/coordinator/capital_allocator.py (lines 192-199)
- **Description**: release() clamps with max(0.0, current - position_pct) per bucket and per strategy with no validation. A double-release (e.g., close path executed twice due to the race the order_executor header documents) silently clamps to 0 instead of revealing the accounting error.
- **Impact**: Repeated/duplicate close paths understate the bucket by the clamped excess relative to truth-by-events; the ledger can read lower than real exposure until the next reconcile, masking the very double-execution bugs the team has already hit.
- **Fix**: When position_pct > current reserved, log a warning (or record a NEGATIVE_RELEASE event with the excess) instead of silently clamping; reconcile_with_positions remains the correction mechanism, but the anomaly becomes visible.

### Issue 7: [LOW] edge_case
- **File**: app/pipeline/order_executor.py (lines 712-726)
- **Description**: _submit_order_optimized computes notional = quantity * (order.price or signal_price or 0.0). If both are None/0 (e.g., pure market order constructed without reference price), notional=0, so should_use_twap(0, threshold) is always False and any downstream notional-based checks see zero.
- **Impact**: Large orders with missing reference prices silently skip TWAP optimization and execute as single market prints, increasing slippage precisely when price data was unavailable.
- **Fix**: Fail closed or fetch a fallback: if notional cannot be computed (both prices falsy), pull last quote from market_data_router; if still unavailable, either reject the order or route to a conservative default (treat as above-threshold). Log the fallback.

### Issue 8: [LOW] edge_case
- **File**: app/core/position_sync.py (lines 55-75)
- **Description**: AUTO-claim credential is the remark prefix whitelist; header notes the whitelist list ('pb_', 'orb_', 'ext_entry', 'ext_exit_', 'OVER_LIMIT_TRIM') must NEVER be applied to external-close detection, and fail-safe maps unknown remarks to MANUAL. The invariant lives only in comments; the two classification points are deep in truncated code.
- **Impact**: A future refactor that applies is_system_order_remark to the external-close path would recreate the RFC-080 class of incident (manual/APP closes of AUTO positions being misclassified, or AUTO closes dropped).
- **Fix**: Encode the invariant structurally: expose two separately-named predicates (is_auto_entry_remark / is_external_close_candidate) where the latter has no whitelist filter, and pin both behaviors with tests named after the RFC-080 incident.

## 4. Optimization Suggestions (8 suggestions)

### 1. [performance] Replace per-event SQLite connect/insert/commit (up to 2 new connections per try_
- **File**: app/coordinator/capital_allocator.py
- **Expected Impact**: Removes disk I/O from the capital-gate critical section; cuts order-path tail latency and eliminates cross-strategy serialization during bursts.

### 2. [maintainability] Decompose the 5685-line module along its own documented seams: OrderGate (PDT/VW
- **File**: app/pipeline/order_executor.py
- **Expected Impact**: Smaller, independently testable units; the header's cascading-failure post-mortem (position-shortage -> no backoff -> 8h lockout) shows the cost of the current monolith; reduces regression surface for the highest-risk code path.

### 3. [maintainability] Make the engine ABCs real or remove them: register engines in the scheduler loop
- **File**: app/core/engine.py
- **Expected Impact**: One liveness/health mechanism instead of implicit contracts; less indirection; new engineers can locate enforcement points instead of rediscovering them in scheduler internals.

### 4. [performance] Swap _VIX_HISTORY/_SPY_HISTORY lists for collections.deque(maxlen=30) with times
- **File**: app/pipeline/signal_pipeline.py
- **Expected Impact**: O(1) allocation-free updates per tick; removes trim races; negligible CPU on the hot scan path.

### 5. [performance] Inside PositionStore.add(), the daily_active_pool update (potential JSON file wr
- **File**: app/core/scheduler_core.py
- **Expected Impact**: has()/add() from scan/monitor/sync threads no longer queue behind file I/O; lower latency on the position-mutation path.

### 6. [maintainability] Centralize the external-close dedup and remark classification into a small repos
- **File**: app/core/position_sync.py
- **Expected Impact**: Directly prevents recurrence of the 2026-08-28 'close disappears from the system' class of bug; makes the SSOT reconciliation logic reviewable and testable in isolation.

### 7. [scalability] Return richer error semantics across the adapter surface: Optional results or ty
- **File**: app/execution/broker_adapter.py
- **Expected Impact**: Downstream reconcile/exit logic can distinguish 'empty' from 'unavailable', eliminating the phantom-close class of failure and reducing defensive is_connected() checks scattered across callers.

### 8. [scalability] Unify instantiation through the existing DI container (di_container) instead of 
- **File**: app/coordinator/capital_allocator.py
- **Expected Impact**: Single construction path (no split-brain instances), honest documentation, easier enforce-mode rollout since config is injected and visible in one place.

## 5. Overall Assessment

- **Code Quality Score**: 6/10
- **Architecture Score**: 6/10
- **Risk Level**: high

**Top Priorities**:
1. Wire CapitalAllocator enforcement into the live order path: compute position_pct from final order qty, call try_reserve before submission and release on every close, then reset_and_reconcile + flip observe_only=False — today the capital caps are advisory no-ops on real money.
1. Make lazy singletons thread-safe (double-checked locking) for get_capital_allocator and _get_pdt_guard, or construct them eagerly in the DI container — duplicate instances split the PDT day-trade counters and the capital ledger across threads.
1. Harden close booking semantics: correlate the external-close dedup on entry_time/position identity (not just symbol+direction+exit_price>0), make broker_adapter.get_positions distinguish 'disconnected' from 'empty', and give _pending/cancel-unconfirmed states a guaranteed release path — these jointly prevent missed and phantom close bookings.

**Summary**: FTSeek-US is a mature, incident-hardened system: the dated RFC/DEB/P0 annotations show disciplined post-mortem culture, and many past failures (remark-based misattribution, dedup-caused vanished closes, disconnect-time fake fills) have already been found and patched with fail-safe defaults. However, the system's most important financial guard — the capital allocator — is explicitly still in observe/Phase A and not wired into the order path, and its surrounding plumbing (lazy singletons, persist-inside-lock, silent arg-swallowing) is not yet production-grade for the enforce mode it is heading toward. The mixed asyncio + thread runtime makes the two racy singletons and the shared in-memory history buffers more than theoretical: the codebase's own incident history proves these windows get hit. Real-money risk remains HIGH until the capital enforce path is integrated and the guard singletons are construction-safe. The close-booking chain (external-close dedup correlation, empty-on-disconnect positions, pending-set lifecycle) is the second cluster requiring attention, since the project's own comments record that bugs here cause trades to 'completely disappear' from the ledger. Finally, the monolithic order_executor and the partially-aspirational engine ABCs concentrate the most dangerous logic in the least testable shapes; continuing the playbook-style decomposition (as done with exit_engine.py) is the highest-leverage maintainability investment. Note: several files (order_executor body, position_sync guard body, exit rule implementations) were truncated; issues flagged CONFIRM should be verified against the full sources before fixing.
