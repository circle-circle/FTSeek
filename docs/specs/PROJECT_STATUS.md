# Trading Playbook System - Project Status

## Meta
- **Project**: Trading Playbook System
- **Spec**: [trading-playbook-system.md](./trading-playbook-system.md) (v2.0)
- **Last Updated**: 2026-06-19T20:30:00Z (voting_llm P0 fixes: naming + signal_dedup)
- **Updated By**: hermes (current session)

## Current Status
- **Phase**: 2 (Pattern Iteration) — Shadow验证运行中 + 方案B最小实盘
- **Progress**: Phase 0/1/2 modules done + pipeline wired + 6/18首次运行成功 + 触发窗口优化 + scalping代码清理
- **Total Tests**: 232 (all passing)
- **运行模式**: `pipeline.master_switch=shadow_only` + `playbook.execute_mode=live` + `auto_execute=true`

## Phase 0: Shadow Validation (Week 0)

### Task 0.1: PlaybookDataCollector Orchestration Layer [DONE]
- **Assigned Agent**: claude-opus-4 (completed)
- **Input**: 
  - `FundamentalProvider` (app/data/providers/fundamental_provider.py)
  - `SECForm4Provider` (app/data/providers/sec_form4_provider.py)
  - `FinnhubProvider` (app/data/finnhub_provider.py)
  - `EarningsCalendar` (app/data/earnings_calendar.py)
- **Output**:
  - `app/playbook/data_collector.py` (624 lines)
  - `tests/test_playbook_data_collector.py` (19 unit tests)
- **Acceptance Criteria**:
  - [x] Can collect 6-dimension data for a single stock (fundamental, technical, insider, news, earnings, sentiment)
  - [x] Each dimension has `quality` field (ok/stale/partial/missing)
  - [x] Supports batch collection for multiple stocks
  - [x] Unit tests pass (19/19 passed)
  - [x] No lint errors
- **Blocked**: No
- **Depends On**: None

### Task 0.2: MarketRegimeGate [DONE]
- **Assigned Agent**: claude-opus-4 (completed)
- **Output**:
  - `app/playbook/market_regime.py` (251 lines)
  - `tests/test_market_regime.py` (14 unit tests)
- **Acceptance Criteria**:
  - [x] Returns `RISK_ON` / `CAUTIOUS` / `RISK_OFF` / `NO_TRADE`
  - [x] Based on SPY MA20/MA50 relationship + VIX level
  - [x] Unit tests cover all regimes
  - [x] No lint errors
- **Blocked**: No
- **Depends On**: Task 0.1

### Task 0.3: PlaybookScreener [DONE]
- **Assigned Agent**: claude-opus-4 (completed)
- **Output**:
  - `app/playbook/screener.py` (369 lines)
  - `config/playbook_screening.yaml` (74 lines, 13 factors)
  - `tests/test_playbook_screener.py` (15 unit tests)
- **Acceptance Criteria**:
  - [x] Filter Top N candidates from watchlist (N determined by regime)
  - [x] Exclude existing positions, PDT restrictions, earnings < 3 days
  - [x] Score and rank by configurable weights
  - [x] Industry concentration check (≥2 positions in same industry → -3 penalty)
  - [x] Unit tests pass (48/48 total for Tasks 0.1-0.3)
- **Key Features**:
  - 13 screening factors (9 positive, 4 negative)
  - Field-to-field condition comparison
  - Insider factors mutual exclusion
  - Earnings exclusion rule (< 3 days)
- **Blocked**: No
- **Depends On**: Task 0.1, Task 0.2

### Task 0.4: Batch LLM Instruction Generation [DONE]
- **Assigned Agent**: claude (current session)
- **Output**:
  - `app/playbook/instruction_generator.py` (584 lines, anti-hallucination architecture)
  - `app/playbook/condition_taxonomy.py` (103 lines, 32 fixed condition tags)
  - `tests/test_instruction_generator.py` (35 unit tests)
  - `tests/test_condition_taxonomy.py` (18 unit tests)
  - LLM feature route: `playbook_generator` (primary: qwen-max, fallback: deepseek-pro)
- **Acceptance Criteria**:
  - [x] 1 LLM call generates 1-5 trading instructions
  - [x] JSON Schema validation passes
  - [x] Condition tags must be from fixed enumeration (32 tags, 7 categories)
  - [x] Retry mechanism (max 2 times)
  - [x] Anti-hallucination: LLM only outputs decisions, Python computes all values
  - [x] Thesis templates (7 fixed templates, no free-text)
  - [x] Unit tests pass (53/53 for Task 0.4)
- **Key Architecture Decisions**:
  - LLM output minimized to: `{symbol, direction, selected_conditions, thesis_template}`
  - All numerical values computed deterministically by Python (entry_price, stop_loss, take_profit, position_size)
  - `selected_conditions` verified against Screener's `triggered_factors` — unverified conditions silently stripped
  - Confidence = data quality score (max 72) + condition verification score (max 28), capped at 90
- **Context**:
  - LLM feature: `playbook_generator` (primary: qwen-max, fallback: deepseek-pro)
  - max_tokens: 2048, temperature: 0
  - Fixed condition enumeration: 32 tags in 7 categories (spec said 27 but actual implementation has 32)
- **Depends On**: Task 0.1, Task 0.2, Task 0.3
- **Python 3.8 Fix**: `tuple[bool, list]` → `Tuple[bool, list]` (typing import)

### Task 0.5: playbook_instructions Database Table [DONE]
- **Assigned Agent**: claude (current session)
- **Output**:
  - `app/playbook/db.py` (655 lines)
  - `app/playbook/__init__.py` (package init)
  - `tests/test_playbook_db.py` (28 unit tests)
- **Acceptance Criteria**:
  - [x] Create 5 tables per spec 6.1:
    - `playbook_instructions` (main table, 40+ fields, UNIQUE(date, symbol))
    - `playbook_factor_stats` (UNIQUE(factor_key, analysis_level))
    - `playbook_patterns` (UNIQUE(pattern_key))
    - `playbook_user_feedback` (FK to instructions)
    - `playbook_regime_log` (UNIQUE(date))
  - [x] 4 indexes for query performance
  - [x] UPSERT support (ON CONFLICT DO UPDATE)
  - [x] Full CRUD operations for all 5 tables
  - [x] JSON field auto-serialization/deserialization
  - [x] Tracking field update (Layer 3 盘后填充)
  - [x] Summary statistics
  - [x] Unit tests pass (28/28)
- **Key Design**:
  - Uses `get_db("ftseek_us")` connection pool pattern
  - `_to_json()` helper for safe JSON serialization
  - `_row_to_dict()` auto-parses JSON fields on read
  - `_TRACKING_FIELDS` whitelist for safe update_instruction_tracking
  - Supports direct `conn` parameter for testing (bypass pool)
- **Depends On**: None (parallel with 0.4)

### Task 0.6: Shadow PnL Calculation [DONE]
- **Assigned Agent**: claude (current session)
- **Output**:
  - `app/playbook/shadow_tracker.py` (280 lines)
  - `tests/test_shadow_tracker.py` (29 unit tests)
- **Acceptance Criteria**:
  - [x] Calculate shadow PnL for each instruction (assume 100 shares at entry price)
  - [x] Calculate MFE/MAE (maximum favorable/adverse excursion) using daily high/low
  - [x] Holiday check (`is_trading_day()` with US_MARKET_HOLIDAYS_2026)
  - [x] Entry trigger detection (±2% zone)
  - [x] Trade execution matching (via TradeStore)
  - [x] Exit trade detection + PnL calculation
  - [x] MFE/MAE analysis (止盈设低了/止损设对了但运气好/合理)
  - [x] Holding position PnL tracking (close price)
  - [x] Unit tests pass (29/29)
- **Key Features**:
  - `ShadowTracker.daily_tracking(trade_date)` — main entry point
  - Graceful degradation: no market_data or trade_store → continues without errors
  - Supports both BUY and SELL_SHORT directions
  - MFE/MAE accumulates across days
  - `analyze_mfe_mae()` provides human-readable stop-loss/take-profit quality analysis
- **Depends On**: Task 0.4, Task 0.5

## Phase 1: Go Live (Week 1-2)

### Task 1.1: Dashboard Playbook View [DONE]
- **Assigned Agent**: claude (current session)
- **Output**:
  - `app/playbook/playbook_service.py` (336 lines, business orchestration)
  - `app/web_dashboard/routes/playbook.py` (183 lines, 11 API endpoints)
  - `app/web_dashboard/templates/playbook.html` (140 lines, dark theme UI)
  - `app/web_dashboard/static/js/playbook.js` (277 lines, client logic)
  - `tests/test_playbook_service.py` (15 unit tests)
- **Acceptance Criteria**:
  - [x] `/playbook` page visible in navigation (sidebar + top nav)
  - [x] Today's instructions displayed as cards with direction/confidence/conditions
  - [x] Summary bar shows regime, buy/short/skip/executed counts, win rate
  - [x] Execute button with confirmation modal
  - [x] Skip button with preset + custom reasons
  - [x] API endpoints: today, summary, history, instruction detail, factor-stats, patterns
- **Depends On**: Phase 0 complete ✅

### Task 1.2: Execute Button + ExitMonitor Integration [DONE]
- **Output**: Integrated in `playbook_service.py`
- **Acceptance Criteria**:
  - [x] Execute calls `ManualOrderService.submit_order()` with idempotency key
  - [x] Registers in `ManualHoldRegistry` to prevent auto-pipeline interference
  - [x] Updates DB: `executed=1`, `execution_price`, `execution_time`, `outcome=holding`
  - [x] Saves user feedback record (action=executed)
  - [x] Guards: SKIP instructions cannot be executed, already-executed returns error
  - [x] Default mode: simulate (live requires additional confirmation)
- **Depends On**: Task 1.1

### Task 1.3: User Feedback Collection [DONE]
- **Output**: Integrated in `playbook_service.py` + `routes/playbook.py`
- **Acceptance Criteria**:
  - [x] Skip with 4 preset reasons: risk_too_high, unfamiliar_sector, similar_position, insufficient_capital
  - [x] Skip with custom free-text reason
  - [x] Modify instruction parameters (stop_loss, take_profit, position_size) with original/modified tracking
  - [x] All feedback saved to `playbook_user_feedback` table
  - [x] API endpoints: `/api/playbook/skip`, `/api/playbook/modify`
- **Depends On**: Task 1.1

### Task 1.4: Feishu Push (Pre-market + Post-market) [DONE]
- **Assigned Agent**: claude (current session)
- **Output**:
  - `app/playbook/feishu_push.py` (233 lines)
  - `tests/test_playbook_feishu_push.py` (10 unit tests)
- **Acceptance Criteria**:
  - [x] Morning push: formatted instructions + regime + VIX + conditions (spec 7.2 format)
  - [x] Evening push: execution results + PnL + MFE/MAE + summary stats
  - [x] Holiday check: skip non-trading days
  - [x] PushHub integration with dedup (category=PLAN_PREMARKET)
  - [x] Fallback to FeishuNotifier.send_text() if PushHub unavailable
  - [x] Unit tests pass (10/10)
- **Cron schedule** (to be configured):
  - `30 8 * * 1-5` (12:30 UTC) — Morning push
  - `30 16 * * 1-5` (20:30 UTC) — Evening push
- **Depends On**: Task 1.1

## Phase 2: Pattern Iteration (Week 3-4)

### Task 2.1: Dual-Layer Factor Analysis + Bayesian Estimation [DONE]
- **Output**: `app/playbook/factor_analyzer.py` (310 lines), `tests/test_factor_analyzer.py` (29 tests)
- **Acceptance**: Layer A (single) + Layer B (pair, min 3) + Layer C (combo, min 5) + Bayesian + classify + report

### Task 2.2: MFE/MAE Tracking Enhancement [DONE]
- **Note**: Enhanced in shadow_tracker.py with multi-day accumulation, direction-aware MFE/MAE, quality analysis

### Task 2.3: Weekly Report Cron + LLM Factor Report [DONE]
- **Output**: `app/playbook/weekly_report.py` (195 lines), 5 tests
- **Acceptance**: Week stats + factor report formatting + skip analysis + Feishu push

### Task 2.4: Automatic Weight Adjustment [DONE]
- **Output**: `app/playbook/weight_adjuster.py` (115 lines), 7 tests
- **Acceptance**: Positive +1 (max 5), Negative -1 (min -5), YAML persistence, history tracking

### Task 2.5: Risk Circuit Breaker [DONE]
- **Output**: `app/playbook/risk_circuit_breaker.py` (145 lines), 8 tests
- **Acceptance**: Daily loss 1% limit, 3-loss half-position, 5-loss stop-1-day, alerts

## Test Summary

| Test File | Tests | Status |
|-----------|-------|--------|
| `test_playbook_data_collector.py` | 19 | ✅ Pass |
| `test_market_regime.py` | 14 | ✅ Pass |
| `test_playbook_screener.py` | 15 | ✅ Pass |
| `test_condition_taxonomy.py` | 18 | ✅ Pass |
| `test_instruction_generator.py` | 35 | ✅ Pass |
| `test_playbook_db.py` | 28 | ✅ Pass |
| `test_shadow_tracker.py` | 29 | ✅ Pass |
| `test_playbook_service.py` | 15 | ✅ Pass |
| `test_playbook_feishu_push.py` | 10 | ✅ Pass |
| `test_factor_analyzer.py` | 29 | ✅ Pass |
| `test_phase2_modules.py` | 20 | ✅ Pass |
| **Total** | **232** | **✅ All Pass** |

## File Inventory

| File | Lines | Task | Status |
|------|-------|------|--------|
| `app/playbook/__init__.py` | 1 | 0.5 | ✅ |
| `app/playbook/data_collector.py` | 624 | 0.1 | ✅ |
| `app/playbook/market_regime.py` | 250 | 0.2 | ✅ |
| `app/playbook/screener.py` | 368 | 0.3 | ✅ |
| `app/playbook/instruction_generator.py` | 583 | 0.4 | ✅ |
| `app/playbook/condition_taxonomy.py` | 102 | 0.4 | ✅ |
| `app/playbook/db.py` | 668 | 0.5 | ✅ |
| `app/playbook/shadow_tracker.py` | 421 | 0.6 | ✅ |
| `app/playbook/playbook_service.py` | 336 | 1.1-1.3 | ✅ |
| `app/playbook/feishu_push.py` | 233 | 1.4 | ✅ |
| `app/playbook/factor_analyzer.py` | 310 | 2.1 | ✅ |
| `app/playbook/weekly_report.py` | 195 | 2.3 | ✅ |
| `app/playbook/weight_adjuster.py` | 115 | 2.4 | ✅ |
| `app/playbook/risk_circuit_breaker.py` | 145 | 2.5 | ✅ |
| `app/playbook/pipeline.py` | 340 | WIRING | ✅ (NEW — end-to-end orchestration) |
| `app/core/scheduler_core.py` | +11 | WIRING | ✅ (3 insertion points in `_run_llm_loop`) |
| `app/web_dashboard/routes/playbook.py` | 183 | 1.1 | ✅ |
| `app/web_dashboard/templates/playbook.html` | 140 | 1.1 | ✅ |
| `app/web_dashboard/static/js/playbook.js` | 277 | 1.1 | ✅ |
| `config/playbook_screening.yaml` | 73 | 0.3 | ✅ |
| `config/unified_config.yaml` (updated) | +7 | 0.4 | ✅ |
| **Total code** | **5,236** | | |

## Decision Log

### 2026-06-18: Choose Batch LLM Instead of Per-Stock Calls
- **Decision Maker**: claude-opus-4 + user
- **Reason**: 
  - Latency 90s → 15s
  - Cost reduced by 60%
  - Enables cross-stock comparison
- **Alternative**: Independent call per stock
- **Risk**: Single call has longer tokens, needs max_tokens=2048

### 2026-06-18: Use AlphaVantage for VIX
- **Decision Maker**: claude-opus-4
- **Reason**:
  - `AlphaVantageProvider.get_vix()` already has complete implementation
  - 3-level fallback: AlphaVantage → FRED → Finnhub VIXY
  - TwelveData free tier doesn't support VIX
- **Alternative**: Add TwelveData VIX endpoint

### 2026-06-18: Fixed Condition Enumeration (32 Tags, not 27)
- **Decision Maker**: claude-opus-4 + user
- **Note**: Spec v2.0 says 27 but actual implementation has 32 (7 categories fully counted)
- **Categories**: 趋势(6) + 动量(8) + 价值(6) + 内部人(3) + 情绪(4) + 量价(2) + 事件(3) = 32
- **Reason**: Ensure Pattern system consistency, each condition trackable

### 2026-06-18 (current session): Anti-Hallucination Architecture for LLM
- **Decision Maker**: claude (current session)
- **Reason**:
  - LLM only outputs {symbol, direction, selected_conditions, thesis_template}
  - All numerical values (entry_price, stop_loss, take_profit, position_size) computed deterministically by Python
  - `selected_conditions` verified against Screener's triggered_factors — hallucinated conditions stripped
  - Thesis uses 7 fixed templates, no free-text
- **Alternative**: LLM outputs full instruction with all values
- **Risk**: Reduced LLM flexibility, but eliminates numerical hallucination

### 2026-06-18 (current session): Shadow PnL Uses 100 Shares Default
- **Decision Maker**: claude (current session)
- **Reason**: Shadow mode doesn't have real position size, 100 shares provides comparable baseline
- **Alternative**: Use instruction's position_size
- **Risk**: Shadow PnL magnitude differs from real PnL

### 2026-06-18 (current session): End-to-End Wiring Pass — Pipeline Electrified
- **Decision Maker**: claude (current session)
- **Context**: Audit revealed all 14 modules + 232 tests existed, but **zero production callers** — `main.py` never imported playbook, `scheduler_tasks`/`scheduler_core` had no playbook cron, the L0→1→2→3 pipeline was a pile of disconnected parts. The "Phase 0/1/2 100% done" claim was true at unit level but **false at system-delivery level**.
- **What was added**:
  - `app/playbook/pipeline.py` (NEW, ~340 lines) — 3 orchestration entries: `run_playbook_generate` / `run_playbook_track` / `run_playbook_weekly`, each with holiday skip + daily/weekly idempotency guard + per-layer exception isolation.
  - `app/core/scheduler_core.py` — 3 insertion points in `_run_llm_loop`: `PRE_MARKET` → generate; `AFTER_HOURS` → track + weekly.
  - `_CallLLMAdapter` — bridges project's `call_llm(feature,prompt)→CallResult` to `InstructionGenerator`'s expected `.call(feature=,prompt=,max_tokens=,temperature=)→str`. No existing module was modified.
- **Key decisions**:
  - **Reuse the existing scheduler, not a new cron daemon**. The project already does cron via phase-polling + idempotency flags in `_run_llm_loop`; introducing APScheduler/a second daemon would create two competing schedulers. Playbook slots into the existing `PRE_MARKET`/`AFTER_HOURS` phases.
  - **All values still computed by Python** (anti-hallucination architecture preserved) — the adapter only carries LLM decisions, never numbers.
  - **Cold-start safe**: pattern injection wrapped in try/except; factor analysis returns empty when <20 trades; all degradation paths verified.
- **Verification**: Stub-driven smoke test ran generate→track→weekly end-to-end against a temp SQLite DB → 1 instruction persisted + 1 regime log + idempotency guard confirmed (2nd call with `force=False` → None). Existing 232 tests still all pass (zero regression).
- **Remaining**: spec §8 retry semantics (08:30/09:00 retry on generate failure) not yet wired — current behavior is "try once per PRE_MARKET tick, fail-isolated". Defer until first live run reveals whether retries are needed.

### 2026-06-18 (current session): Shadow-Mode Kill Switch — go-live gate
- **Decision Maker**: claude (current session)
- **Problem**: Wiring the pipeline meant restarting main.py would immediately start calling real Providers (Finnhub/SEC/AlphaVantage) + real LLM every trading day, with zero ability to stop it without code changes. Not safe for a first go-live.
- **What was added**:
  - `playbook:` block in `config/unified_config.yaml`: `enabled` (总闸门, default false), `execute_mode` (simulate|live, default simulate), `enable_weekly`.
  - `_playbook_enabled()` / `_playbook_cfg()` in `pipeline.py` — gate the 3 entries; `enabled=false` → early return None, zero side effects.
  - `execute_instruction` order mode changed from hardcoded `"simulate"` to read `playbook.execute_mode` (default unchanged = simulate).
- **Why default OFF**: go-live safety — the gate must be explicitly flipped to `true` only after review. Rollback is config-only (flip back + restart), no code edit. Verified: gate returns False by default, all three entries return None when closed, passes through when open.
- **Decision**: ship Phase 0 **shadow only** (enabled=true, execute_mode=simulate). Real execution (execute_mode=live) blocked until the first-day checklist + 3 acceptance thresholds pass — see "Shadow Mode Go-Live" section.

### 2026-06-19: Trigger Window ±3% → ±5%
- **Decision Maker**: hermes + user
- **Root cause**: AVGO entry=$392.9, actual price=$409.25 → exceeded ±3% upper bound ($404.69), not triggered despite positive shadow PnL (+$1,845)
- **Fix**: `shadow_tracker.py` trigger window widened to ±5% (entry_low=0.95, entry_high=1.05)
- **Risk**: Wider window may trigger at less favorable prices; acceptable since ±5% is still reasonable for swing trades

### 2026-06-19: Scalping Code Cleanup (evolution/shadow_tracker + intraday_attribution)
- **Decision Maker**: hermes
- **What**: Removed scalping-specific branches in `_get_real_pnl_stats()` (unified to single query) and `_get_default_params()` (unified to main config). Removed scalping_positions display in `intraday_attribution.py`.
- **Why**: Scalping disabled since 2026-06-14; residual code had active branching that could misclassify data
- **Remaining**: ~30 other files have scalping references — all are comments, deprecated no-op methods, or historical DB queries. Left as-is (historical analysis still needs strategy_id='scalping' filter)

## Known Pitfalls

1. **Finnhub API Rate Limit**: 60 calls/min, batch calls need intervals
2. **SEC EDGAR Stability**: Sometimes returns 503, needs retry + backoff
3. **Futu K-line Delay**: May delay 5-10 minutes in pre-market
4. **AlphaVantage Daily Limit**: 500 calls/day, VIX queries consume little but need attention
5. **Python 3.8 Compatibility**: `tuple[bool, list]` syntax not supported, use `Tuple[bool, list]` from typing
6. **SQLite UPSERT**: `last_insert_rowid()` returns 0 on UPDATE path, must query by UNIQUE key
7. **DB Pool Singleton**: In tests, use direct `conn` parameter to avoid cross-test data pollution

## Next Session Starting Point

If you take over this project:
1. Read `docs/specs/trading-playbook-system.md` (spec v2.0)
2. Read this file (`PROJECT_STATUS.md`)
3. **Modules COMPLETE (Phase 0/1/2) + Pipeline WIRED (2026-06-18)** — all 15 module tasks done (232 tests passing) AND the end-to-end orchestration is now live in `scheduler_core._run_llm_loop` (see Decision Log "End-to-End Wiring Pass"). The system now actually runs on cron; it is no longer just a pile of unit-tested parts.
4. **What still needs doing (post-wiring)**:
   - **First live run / dry-run**: trigger `run_playbook_generate(scheduler, force=True)` once (or restart the app on a trading day) and confirm: Feishu morning message fires → `playbook_instructions` has today's rows → next day `run_playbook_track` fills outcome/shadow_pnl/MFE/MAE. This is the real acceptance test — wiring passed only under stubs.
   - **spec §8 retry semantics**: generate-failure retries (08:30 / 09:00) not yet wired — currently one attempt per PRE_MARKET tick, fail-isolated. Add if first live runs show flakiness.
   - **`PlaybookService.generate()` + `POST /api/playbook/generate` route** (optional): lets the Dashboard trigger a manual re-run without restarting the process.
   - **Phase 3 — Closed-Loop Validation (Month 2-3)**: accumulate 30+ trades → factor analysis becomes actionable → validate weight-adjustment improves win rate → Bayesian threshold tuning from real data.
5. **Cron jobs are now wired** (no separate config needed — they live inside `_run_llm_loop`):
   - `PRE_MARKET` phase → generate + morning push (idempotent per-day)
   - `AFTER_HOURS` phase → track/evening push (daily) + weekly report (Friday-only via week-level idempotency)
6. After completing a task:
   - Update this file (change status to DONE, check acceptance criteria)
   - Write change log
   - Git commit (message: "Task X.Y: completion description")
7. Continue to next `TODO` task

## Shadow Mode Go-Live (2026-06-18) — Phase 0 启动验收

**结论**: 系统满足"进入 Phase 0 shadow 验证"的上线条件, **不满足**"Phase 1 真实交易"。隔着 spec §9 Phase 0 要求的那一周冷启动验证 —— 这一周是唯一能暴露"真实 Provider 限流 / 真实 LLM 幻觉残留 / 真实跨天追踪 bug"的窗口, 代码 review 替代不了。

### 安全开关 (已实施, `config/unified_config.yaml`)

```yaml
playbook:
  enabled: false            # ← 总闸门, false 时三个 pipeline 入口第一行早退, 零副作用
  execute_mode: simulate    # ← 执行按钮下单模式, shadow 阶段保持 simulate (execute_instruction 已改为读此配置)
  enable_weekly: true       # 周度复盘 (冷启动期可关省 LLM)
```

- `enabled=false` (默认): `run_playbook_generate/track/weekly` 在首行 `return None`, 不调 Provider/LLM/DB/飞书。重启 main.py 不会突然猛调 API。
- 回滚无需改代码: `enabled` 改回 `false` + 重启即可, 已实测回滚路径干净。
- `execute_instruction` 下单模式从硬编码 `simulate` 改为读 `playbook.execute_mode`, **默认 simulate 不变** (Phase 0 安全)。

### 上线步骤

1. 把 `playbook.enabled` 改为 `true` (`execute_mode` 保持 `simulate`)。
2. 重启 main.py。
3. 按下面清单逐段验证 (明美股前/盘后)。

### 首日验证清单 (每段链路一条证据)

| # | 检查段 | 验证方式 | 通过标准 |
|---|--------|----------|----------|
| 1 | 闸门已开 | 日志 grep `playbook.enabled=false` | **不应出现** → 说明放行 |
| 2 | Layer0 门禁跑了 | 日志 grep `Layer0 regime=` | 有 regime + VIX 值 |
| 3 | regime 入库 | `sqlite3 data/ftseek_us.db "SELECT date,regime,vix FROM playbook_regime_log ORDER BY date DESC LIMIT 1"` | 今天有一行 |
| 4 | Provider 联网没炸 | 日志 grep `collect_batch\|Failed to collect\|Rate Limit\|503` | 无 Provider 崩盘堆栈 (降级 stale/missing 允许) |
| 5 | LLM 真实输出解析成功 | 日志 grep `LLM 输出校验失败\|生成完成` | 出现 `✅ 生成完成: N/M 条指令入库`, JSON 解析失败率低 |
| 6 | 指令入库 | `sqlite3 data/ftseek_us.db "SELECT symbol,direction,confidence,market_regime FROM playbook_instructions WHERE date='<今天ET日期>'"` | 有 BUY/SELL_SHORT/SKIP 行, entry/stop/tp **非 0** |
| 7 | 飞书盘前推送 | 查飞书 PLAN_PREMARKET 消息 | 收到含 regime+指令的格式化消息 |
| 8 | 盘后追踪(AFTER_HOURS) | 日志 grep `盘后追踪开始`; 查 `playbook_instructions` 的 `outcome`/`shadow_pnl` | 有非 NULL 追踪字段 (持仓有浮动PnL、已退出有 MFE/MAE) |
| 9 | 执行仍是 simulate | 点 Dashboard 执行按钮看日志 `mode=` | **必须 simulate**, 绝不能 live |

### Phase 0 验收门槛 (跑满 1 完整交易日 + 跨天后)

- **数据管道**: Provider 调用成功率 > 80% (清单 4)
- **LLM 输出**: JSON 解析成功率 > 90% (清单 5); condition tag 无幻觉 (LLM 给的条件均在 `triggered_factors` 内, 反幻觉架构已剥但需确认未误删)
- **Shadow PnL**: 跨天后清单 8 的 outcome/shadow_pnl 被正确填充

**三项全过才考虑改 `execute_mode=live` 进 Phase 1。** 任一不过就停在 shadow 修问题 (spec §10 防烂尾)。

### 诚实补充

这份清单能验证"链路通不通", 但**验证不了"shadow PnL 算得对不对"** —— 那需要真实跨多天、有真实 entry/exit 价比对, 得跑够几天。首日清单全过只代表"可进入 shadow 观察", 不代表"shadow 数据可信", 首日就改 live 是过早的。

## Collaboration Rules

1. **Read → Do → Write**: Each session reads status → executes task → updates status
2. **Task Locking**: Change to `IN_PROGRESS` + write agent_id before starting
3. **Atomic Commit**: Commit immediately after each task, don't accumulate
4. **Decision Recording**: Any architectural decision goes to decision log, record WHY
