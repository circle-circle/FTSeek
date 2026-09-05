# FTSeek-US — Trading Strategy Brief (condensed, for external model review)

> Everything a second model needs to critique and optimize this system.
> Figures recomputed read-only from `data/ftseek_us.db` + production config, 2026-09-05 (market closed).
> Full Chinese edition: `docs/STRATEGY-BRIEF-FOR-EXTERNAL-REVIEW.md`.
> Short on time? Read **§4 results**, **§6 already-falsified**, **§8 questions**.

---

## 1. Summary

Automated **US-equity intraday** system on a **Futu simulated account**.

- Equity **$98,754** (peak $98,980) · current DD −0.23% · max DD ever −3.33% · capital tier GREEN.
- Real money locked off by four independent locks; user decree: no live trading before "graduation".
- 447 closed playbook trades since 2026-06-24 · ~10.7 trades/day (246 over 23 sessions last month).
- Shape: **84.6% LONG**, intraday (mean hold 2.03h), **94% of entries within 90 min of the open**,
  mean notional $5,187, universe median ATR 3.36% (event-driven / momentum names).

**Pathology in one line:** gross expectancy is ≈ 0 (proven), so **100% of the bleed is fees**;
exits neither kill winners nor save losers, so no exit-parameter change can move gross expectancy
(optimal stopping). The only way out is an **entry-side edge larger than 9.4bps**.

**State as of 2026-09-05:** three pre-registered experiments (E1 bypass the LLM selector / E2 swing
3–5 days / E3 narrow the entry window) **all failed** → per the pre-registered stop-loss clause,
`auto_execute` was turned **off**. Only new entries stop; exit management and all signal/shadow/review
collection continue record-only.

### Operating philosophy (five rules, unchanged since 2026-07-22)

1. The LLM is an **information gatherer and evidence assessor, not a direction oracle**.
2. **Exits are the profit engine** — entry is assumed to be a coin flip; cut losers, let winners run.
3. Anything needing **intraday timing or discipline goes to code**, no manual override.
   (The operator self-identifies as unable to cut losers; the machine is his rational self.)
4. **Never stop trading to refactor** — improve on the live loop.
5. **Shadow before live** — new rules run record-only and must beat baseline statistically.

> ⚠️ Rule 2 now contradicts the evidence in §4. That tension is the core of question Q1.

---

## 2. Architecture and entry logic

```
DECISION BRAIN (pre-market cron 07:00 ET)
  full-market scan → top-N + watchlist → 19-dim data collection → LLM deep analysis (qwen, thinking on)
  → two-model debate (analyst vs challenger; challenger may not SKIP)
  → signal_gate (PASS/WARN/BLOCK, ~48% BLOCK) → rows in playbook_instructions
EXECUTION BRAIN (polled every 300s)
  invalidation gates → trigger confirmation → live reprice → capital guardrails
  → ~25 inline risk gates → order → fill confirmation → booked
EXIT BRAIN (separate 20s fast loop, ~25s end-to-end)
  breakeven ratchet / trailing / 8 prioritized exit rules → full lifecycle persisted
REVIEW BRAIN (post-market cron)
  daily/weekly/monthly review + IC tracking + shadow scoring → proposals (user must adjudicate)
```

The playbook is the **only** order path; the legacy main pipeline is frozen by two hard locks
(scan-side flag + executor source-type whitelist) and runs only for research/IC and housekeeping.
ORB engine disabled, top-up frozen. Three modes (shadow / simulate / live) coexist by env var and
convention with **no state machine** (known debt).

**Channels** (by `decision_id` prefix): `DA-*` pre-market deep analysis (~77% of fills) ·
`universe_expansion-*` (UE) emerging candidates from event clues — 8-K/13DG/earnings/insider clusters/
peer surges/pre-market movers/FRED macro (~12%) · `intraday_reentry-*` VWAP-band re-entry after a
stop-out, 10:45–13:55 ET (~8%).

### 2.1 Constraints the LLM is given (from the production prompt)

- Every claim must cite concrete data (number + date) and be **ticker-specific** (non-portable).
- **Bull/bear parity**: ≥2 arguments each side, tagged HIGH/MEDIUM/LOW + source type; direction follows
  evidence weight — if data supports short, give `SELL_SHORT` firmly.
- Causal chain ≥3 levels (macro → sector → name, or event → valuation → price).
- **Hard constraints, violation voids the instruction**: ① stop distance **2–5%** (>6% rejected at
  admission, <2% is the noise-sweep zone); ② **no falling knives** — no LONG when the day's cumulative
  decline (incl. pre/post-market, on "effective change") exceeds 5% without stabilization evidence, and a
  pre-market −10% crash is treated as repricing, not a dip; ③ **pullback preferred over breakout chase**;
  ④ pre-market movement must be classified as continuation or overreaction, and all prices anchor to the
  **effective current price**, never the prior close.
- **Calibration fed back to the model**: the prompt states literally *"your historical directional hit
  rate is ~50% (a coin flip)"*, so `score_band=HIGH` demands independently verifiable evidence.
- **Edge test**: not "how old is the news" but **"is it already in the price"**, judged from pre-market
  movement. Priced in → background only; if all arguments are of that kind → low score + SKIP.
- Same name recommended within 3 days without price confirmation → must justify "why this time differs".
- Data sufficiency (missing <6 of 19 dims) is computed by code and injected; missing spot price → SKIP only.

**Instruction contract**: `direction` · `score_band` (HIGH/MEDIUM/LOW → numeric; LOW must not encode a
bearish view) · `entry`/`stop_loss`/`take_profit` · `confidence` · `entry_archetype`
(BREAKOUT / PULLBACK_LIMIT / FADE_SHORT) · `confirmation` · `chase_cap_price` (do-not-chase line) ·
`invalidation` + ≥2 `invalidation_triggers` (ticker-local and code-decidable: metric ∈ {close, volume,
5-day change, pre-market change}, operator ∈ {<,>}, numeric) · `tripwires` · `conditions` (enum factors
that select the exit bucket).

### 2.2 Execution gate sequence (actual order)

```
auto_execute master switch (now false) → ≥5 min after the open → kill-switch file (fail-close)
→ regime NO_TRADE blocks all entries → capital water level GREEN ×1.0 / YELLOW(−10%) ×0.5
   / ORANGE(−20%) ×0.25 / RED(−35%) ×0.0 → daily-loss breaker: intraday equity ≤ −3% stops opening
→ invalidation gates (before 14:00 ET? earnings blackout? thesis drift ≤5%? not expired 24h?)
→ tripwire auto-void (enforce OFF since 9/3 — 45% false-void rate: volume unit mismatch, degenerate
   thresholds like `close>0`, cross-asset thresholds applied to the wrong ticker)
→ chase_cap enforce (live 9/4): reprice beyond the instruction's own do-not-chase line → terminal skip
→ entry band  LONG [entry×0.97, entry×1.05] · SHORT [entry×0.95, entry×1.03]
   (volatility-widened variant knife=min(6%,0.9×ATR%), chase=min(8%,1.5×ATR%) — OFF, in shadow)
→ trigger confirmation: break of the day's high/low (the chase-branch volume gate was removed;
   `_vol_ok` is constant True)
→ VWAP direction gate, tolerance 0.3% (fail-open when VWAP is unpopulated — i.e. exactly at the open)
→ reprice on live bid/ask (re-anchoring disabled)
→ gap-dive halving: counter-trend BUY with gap ≤ −3% → half size (history n=14, net −772, WR 21%)
→ guardrails: ≤8% of base per name · ≤80% daily notional · ≤16 opens/day · ≤3 per round
→ duplicate-position guard → fill confirmation (only FILLED is booked, else release the reservation)
→ ~25 inline gates in order_executor → Futu SIMULATE
```

---

## 3. Sizing, risk and exits

### 3.1 Sizing

```
risk_amount   = risk_base × (risk_pct/100) × regime_mult × water_mult
position_size = risk_amount / |entry − stop_loss|
clamped by: per-name notional cap 8% → market-state multiplier (VIX × regime) → max(1, size)
```

`risk_base` $82,000 (live equity when available) · `risk_pct` 0.5% · `max_position_pct` =
`max_per_symbol` = **8%** (tightened from 15%/25% after a real-money lesson: 86% single-name
concentration, one bag-held position −$65.9k) · sector 40% · per-direction 65% · total 80% ·
**confidence-based sizing disabled** (confidence is a contrarian indicator, §4.5).

VIX multipliers — position: <20 ×1.0 / <25 ×0.8 / <30 ×0.5 / extreme ×0.0; stop widening 0 / +0.3pp /
+0.8pp / +1.5pp; take-profit discount **all ×1.0** (the old 0.9/0.7/0.5 was removed: a global market
factor discounting per-symbol buckets violates the bucketing principle, and it fired 0 times in August).
Regime multipliers RISK_ON/CAUTIOUS/RISK_OFF all ×1.0, NO_TRADE ×0.0 — **regime is a switch, not a scaler**.

Regime score (VIX, SPY vs MA20/50/200, SPY change, breadth): ≥5 RISK_ON (top_n 10) · 0–4 CAUTIOUS (8) ·
−5..−1 RISK_OFF (3) · <−5 NO_TRADE (0). A separate long-direction filter discounts longs by regime:
BULL/STRONG_BULL ×1.0, CAUTION ×0.85, BEAR ×0.7, CRISIS ×0.0.

Six orthogonal breaker layers: account daily DD 3% · portfolio warn 2% / reduce 2.5% / stop 3% ·
3 consecutive strategy losses · intraday equity −3% · per-symbol daily loss $500 · hourly breaker
(**dead key, no consumer**). Plus an out-of-process hard stop: cumulative playbook net loss hitting
**−$4,840.06** closes `auto_execute` same day. Now −$1,884.13, headroom +$2,955.93, never triggered.

### 3.2 Volatility-bucketed stops/targets

SL = clamp(sl_mult × ATR%, floor, ceiling); TP = SL × tp_mult. Invariant: LONG SL < entry < TP.

**SHORT_HORIZON (DA always uses this; max hold 1 day)**

| Bucket | ATR% max | SL mult | SL floor | SL ceil | TP mult | Short widen |
|---|---|---|---|---|---|---|
| low | <1.5% | 1.2 | 2.0% | 2.5% | 2.0 | +0.2pp |
| medium | <3.5% | 1.5 | 2.5% | 4.5% | 1.8 | +0.3pp |
| high | <8% | 1.5 | 3.5% | 5.0% | 1.5 | +0.5pp |
| extreme | rest | 1.2 | 3.5% | 5.0% | 1.5 | +0.8pp (size ×0.5) |

**LONG_HORIZON** (max hold 3 days, RR 2.0, overnight gap stop 3%): low 1.5–3.0% / TP×2.5 ·
medium 2.5–5.0% / TP×2.0 · high 3.5–5.0% / TP×1.8.

Basis: SL distance vs win-rate backtest — <2% → WR 47%, stop-hit 41%, −$1,352; **2–3% → WR 59%,
hit 9%, +$601 (best)**; >5% → WR 14%. Earlier 0.5–0.8×ATR multipliers put stops inside one ATR = pure noise.

### 3.3 Exit priority (evaluated every 20s)

```
[0]   opening cooldown: first 15 min SL relaxed to 4%
[0.5] early tight SL: MFE < 0.3% within 30 min → SL = min(1.0%, 0.5×ATR%), cap 2.5%
[1]   effective_sl hit → SL_INITIAL (breakeven never reached) | SL_BREAKEVEN (ratchet at cost+fee cushion)
                       | SL_TRAIL_LOCK (drawdown lock above net breakeven)
[1.5] TAKE_PROFIT (only if RR ≥ 1.5; `_MIN_TP_RR` is hardcoded)
[2]   OVERNIGHT_GAP_STOP: overnight position gaps > 3% at the next open
[3]   PRE_CLOSE_EXIT: 8 min before the close (15:52 ET)
[3.4] MFE_STALL_EXIT: ≥150 min held, peak profit <0.5%, underwater — DISABLED
[3.5] DEAD_SIGNAL_EXIT: ≥60 min held and MFE <0.2% (re-entry +60 min; overnight resets daily at the open)
[4]   TIME_EXIT: held > factor hours and losing (default 6h)
[5]   MAX_HOLD_T5: ≥5 trading days
```

| Parameter | Value | Basis |
|---|---|---|
| `breakeven_at_pct` | **0.4%** | 1.5→1.0→0.5→0.4; aligns the trigger with the fee line = true net breakeven. Replay n=265 positive in both pessimistic (+88) and optimistic (+161) orderings |
| `drawdown_from_peak_pct` | **0.8%** | 0.5→0.4→0.8; at 0.4% the lock fired inside the noise floor and truncated winners (TRAIL_LOCK avg win $27.59 while MFE was 1.48% — half the move captured) |
| `breakeven_fee_cushion_pct` | 0.4% | prevents a clean breakeven exit being mislabelled TRAIL_LOCK |
| `breakeven_slip_cushion_pct` | 0.2% | trigger = fee line + slippage pad so the limit order crosses and fills at once; the 3 exits that timed out at 8s and degraded to market were all net negative (slippage 6.6–43bps vs a ~7.5bps pad) |
| `breakeven_limit_timeout_s` | 3s | |
| `breakeven_passive_marketable_pad_bps` | 15bps | once the breakeven line is **already pierced**, post a marketable limit instead of "wait 3s → cancel → market" (~12s). Five observed cases, zero pullback fills, all negative |

Exits are bucketed per factor from `conditions` (34 factors): trend/momentum → trailing on, time-exit
6–8h or disabled, max hold 3–5 days; fast-falsification factors → trailing **off**, time-exit 0.5–1h,
max hold 1 day; `default` → trailing on, breakeven 0.4%, drawdown 0.8%, time-exit 6h only when losing,
max hold 1 day. `profit_override`: once profit ≥1.5%, a tighter 0.5% drawdown lock applies.

### 3.4 Overnight policy (three coexisting tracks)

① general overnight hold — **off**. ② **W_WEEK underwater exemption — on, max 5 trading days**:
simulated underwater positions are exempt from EOD / DEAD_SIGNAL / TIME_EXIT / MAX_HOLD, with SL as the
only intraday killer. ③ re-entry first-night exemption — subsumed by ②.
A 2% depth cap on ② was tried and **revoked** (evidence base n=2, far below the n≥30 rule; on 8/21
AMZN was cleared at −2.04% and back to +1.1% thirty minutes later, the −5% SL never engaged).
Pre-registered kill criterion under observation: n≥20 closed W_WEEK positions with repair rate <40% →
propose removal. First reading n=6/20, repair 50%, net −$597.

Re-entry engine: 10:45–13:55 ET, ≤3 arms/day, prior loss ≤1.0%, cumulative ≤2.0×, VWAP band 0.3–1.0%,
5m slope ≥0, volume ratio ≥0.8, spread ≤25bps, ATR% ≥2.0, SL 1.5%, TP RR 1.6.

### 3.5 Governance discipline (please review this too)

Shadow-first · pre-registration (criteria frozen at registration; changing them = re-adjudicating) ·
n≥30 to evaluate, n≥60 to adjudicate · behaviour-changing shadows run ≥5 sessions ·
**no conclusion accepted without day-clustering + MDE** (pooled significance overstates ~8×) ·
significance alone never justifies a gate (false-kill cost must be computed) · one variable at a time ·
the phrase "let's optimize one more version" is banned during the mandate · every change logged in
`handoff.md` + a debate doc · new persisted fields must ship a **cardinality self-check** (against
constant degradation) and a **unit declaration with a contract test** (against dimension lies) —
both rules came from four real incidents each.

---

## 4. Results (recomputed read-only; `pnl_net` = after fees)

### 4.1 Headline: gross ≈ 0, fees eat everything

| Window | n | Gross | Fees | Net | Avg | WR | Avg win | Avg loss | R:R |
|---|---|---|---|---|---|---|---|---|---|
| **Playbook all history** (from 6/24) | 447 | **+$208.44** | **$2,092.50** | **−$1,884.13** | −$4.22 | 52.3% | +43.03 | −55.05 | 0.78 |
| Last month 8/04–9/04 | 246 | +$1,378.48 | $1,196.23 | **+$182.25** | +$0.74 | 60.2% | +35.79 | −52.20 | **0.69** |
| 9/01–9/04 | 52 | — | $251.19 | +$561.61 | +$10.80 | 80.8% | +28.06 | −61.69 | 0.45 |

**Fees are 10× gross profit over full history**; last month they took **86.8%** of gross.
Mean notional $5,187, mean fee $4.86 = **9.4bps round trip** → >9.4bps of ex-ante edge is needed to break even.
Independent clean-window computation (n=321): gross expectancy **+$0.10/trade, t=+0.03,
95% CI [−6.51, +6.70] covering zero** — indistinguishable from a coin flip.

### 4.2 Fee structure: a per-order floor punishes small tickets

| Notional | n | Avg notional | Avg fee | bps | Net | Avg |
|---|---|---|---|---|---|---|
| <$2k | 20 | $1,596 | $4.07 | **25.5** | −$128.97 | −$6.45 |
| $2–4k | 9 | $3,389 | $4.30 | 12.7 | −$457.62 | −$50.85 |
| $4–6k | 217 | $5,592 | $4.96 | **8.9** | +$768.84 | +$3.54 |

196 of 246 trades were <50 shares (avg fee $4.21); the 20 trades ≥199 shares averaged $11.64
→ a clear **~$4 per-order commission floor**.

### 4.3 Exit-reason decomposition (last month): all losses sit in three buckets

| exit_reason | n | Net | Avg | WR |
|---|---|---|---|---|
| SL_TRAIL_LOCK | 63 | **+$3,618.55** | +$57.44 | **93.7%** |
| PRE_CLOSE_EXIT | 10 | +$499.89 | +$49.99 | 60.0% |
| TAKE_PROFIT | 1 | +$444.33 | +$444.33 | 100% |
| SL_BREAKEVEN | 103 | +$368.77 | +$3.58 | 68.9% |
| TIME_EXIT | 3 | −$43.82 | −$14.61 | 0% |
| PATIENT_EXIT_* | 8 | −$489.95 | — | 0% |
| DEAD_SIGNAL_EXIT | 29 | −$319.08 | −$11.00 | 37.9% |
| OVERNIGHT_GAP_STOP | 1 | −$386.77 | −$386.77 | 0% |
| MFE_STALL_EXIT | 10 | −$531.97 | −$53.20 | 0% |
| **SL_INITIAL** | **18** | **−$2,977.70** | **−$165.43** | **0%** |

The three bleeding buckets (SL_INITIAL + MFE_STALL + OVERNIGHT_GAP) = 29 trades, **−$3,896.44**.
Excluding them, last month flips from +$182.25 to **+$4,078.69**.
**The profit engine works; losses are concentrated, not diffuse.**

### 4.4 Diagnostic slices (last month)

**① Wrong on arrival — the MFE≈0 bucket (heaviest finding).**
**32 of 246 trades (13.0%) never reached +0.05% profit at any point: −$3,547.76**, heavily overlapping
the three bleeding buckets. **84% of them (27/32) were opened within 90 minutes of the bell**
(16 at ~09:41 ET, 11 at ~10:01 ET). Behaviour: **chase strength at the open → never see profit →
grind into a wide stop** (median MAE 3.5%, deepest 5.3%). Sample averages: MFE 0.904% / MAE 0.861% / hold 2.03h.

**② Direction × regime mismatch.**

| regime × direction | n | WR | Net |
|---|---|---|---|
| risk_on LONG | 159 | 53.5% | **−$1,173.22** |
| risk_on SHORT | 23 | 52.2% | +$118.24 |
| cautious LONG | 49 | **77.6%** | **+$944.91** |
| cautious SHORT | 14 | 85.7% | +$114.40 |

In a choppy tape, **chasing strength long under risk_on = buying local tops**, while **buying dips long
under cautious is the only positive-expectancy cell**. The system is "a dip buyer wearing a momentum
chaser's schedule": the directional call (85% LONG) is not the problem — the **entry shape** is.

**③ Channels: DA earns, UE and REENTRY drag.**

| Channel | n | Net | WR | MFE≈0 |
|---|---|---|---|---|
| DA | 190 | **+$470.31** | 63.2% | 10.5% |
| UE | 30 | −$293.57 | 50.0% | **23.3%** |
| REENTRY | 19 | −$75.18 | 47.4% | 21.1% |

UE's `confidence` is a constant 50 across 118 instructions (zero discriminating power) and it chases
high-volatility event names inside the first 45 minutes. REENTRY is, trade by trade, revenge re-buying
after a stop-out. Also: DA SHORT 13 trades +$244 (WR 77%, small sample, better than LONG);
LLM selection 162 trades −$224 vs deterministic fallback 17 trades +$133 —
**the LLM selector has not beaten its own deterministic fallback** (selection bias present; not a conclusion).

**④ Overnight vs intraday — the cleanest natural experiment.**

| | n | Net | Avg | PF |
|---|---|---|---|---|
| Intraday | 225 | **+$1,570.09** | +$6.98 | 1.44 |
| Overnight | 21 | **−$1,387.84** | −$66.09 | 0.23 |

Overnight SL_INITIAL alone: 5 trades, −$1,262.93.

**⑤ Entry hour (ET).** 09:xx n=172 +$154.42 (avg +$0.90) · 10:xx n=59 −$63.88 (−$1.08) ·
11:xx n=9 −$54.66 (−$6.07) · 12:xx n=4 −$25.26 (−$6.32) · 13:xx n=1 −$6.29.
94% of entries land in the first 90 minutes; per-trade results degrade monotonically later (n shrinking).

**⑥ Holding duration.**

| Bucket | n | Net | Avg | WR |
|---|---|---|---|---|
| <30m | 89 | −$99.78 | −$1.12 | 66.3% |
| 30–60m | 37 | **+$989.85** | +$26.75 | **86.5%** |
| 1–2h | 43 | +$39.63 | +$0.92 | 51.2% |
| 2–4h | 31 | +$383.97 | +$12.39 | 48.4% |
| >4h | 46 | **−$1,131.42** | −$24.60 | 43.5% |

### 4.5 Broken rulers and contrarian indicators (do not use as inputs)

- **LLM confidence is contrarian**: the ≥0.75 bucket wins 34.3% vs 51.9% for <0.6. Never size up on it.
- **confidence IC −0.32 was measuring a constant** (the field takes only six discrete values).
- **Headline IC +0.1265 is not reproducible** (same-day clustering uncorrected; pooled overstates ~8×).
- **Entry-side score / confidence / quality_score have no discriminating power on fill quality**
  (WR 50–60%, non-monotonic). Only regime discriminates.
- **Signal-layer and selection-layer IC have opposite signs**: raw signal t1 +0.0576 / t5 +0.124
  (positive, strengthening with horizon) vs playbook selection t1 −0.1489 / t5 −0.1962 (negative,
  significant). What is falsified is the **LLM selection layer** — it picks a worse subset out of a
  positive-IC pool — not the signal stack.
- **Power**: day-clustered n=23 gives MDE ≈ ±$335/day, so no single day or week is evidence of edge.

### 4.6 Recurring mistakes

SL_INITIAL clusters repeat weekly: 8/14 (SNDK/UCTT −271), 8/19 (MU/FIX/CAT/SNDK **−1,074 same day**),
8/28 (AFRM/RBRK −467), 8/31 (PCG/TTD −167). Same-name repetition: MU supplied on 12 days, filled 9 times,
−$479.40 at a 22% win rate — the same 4–5% ATR name bought repeatedly on momentum-cluster collapse days.
Ruler failures: 4 "constant degradation" + 4 "unit mismatch" incidents in 7 weeks; several shadows hit
their sample targets with **no adjudication path scheduled** (collection had a cron job, adjudication did not).

---

## 5. What has been tried and worked / not

**Live entry-side fixes:** `chase_cap_enforce` (9/4 — terminal skip when the do-not-chase line is breached) ·
`gap_dive_halve` (halve counter-trend BUYs on deep gaps) · `premarket_invalidation_hard_gate`
(reject entry when spot/prior-close < −5%).

**Built but switched off, waiting on shadow criteria:**
`entry_archetype_routing_enabled=false` — **PULLBACK_LIMIT dip-buying with TTL, never degraded to a market
order** (ADR-035, merged 9/1); paired shadow sample is **n=4 of the required 20**, so pre-registration
discipline forbids enabling it. · `entry_band_vol_widen_enabled=false` (widening only). ·
UE `confirm_gate` — open+10min price-in confirmation (in shadow, wiring criteria pre-registered).

---

## 6. ⛔ Already falsified — do not re-propose

| # | Attempt | Result | Why |
|---|---|---|---|
| 1 | **Tighten initial stop to cap@2.5%** | net **−$203** | Kills the TRAIL_LOCK engine: COIN +187→−86, ORCL +157→−88, MRK +63→−89 all **dove past 2.5% first, then rallied** |
| 2 | **Tighten to cap@1.5%** | net **−$792** | Same mechanism; negative and **monotonic — tighter is worse** |
| 3 | Weekly exit-parameter tuning loop | Abolished | 7 versions in 7 weeks (46 trades / 7.1 days per window), MDE ±$40 chasing $5–10 effects = chasing signal in noise, and "keep if better" imposes a selection direction = **an overfitting machine** |
| 4 | **E1: bypass LLM selection, use pure signal-strength top-3** | arm P n=30 **−$77.60**/trade vs arm D (actual fills) n=118 **−$16.05**; gap **−$61.55** (success gate ≥ +$10), same sign both weeks | **Failed** |
| 5 | **E2: 3–5 day swing hold, mechanical exits** | **−$8**/trade after stripping beta | **Failed** |
| 6 | **E3: restrict entries to 10:00–10:15 ET** | dead-signal rate difference **0.0pp** | **Failed** (historical basis n=10) |
| 7 | Anchored limit-order entry | permutation p=0.19, CI covering 0, sign test p=0.32, n=69, σ 6.5× the power estimate | **KILLED** (all three gates) |
| 8 | MFE_STALL_EXIT (leave at 150 min if peak <0.5%) | 8-variant replay p=0.17, capture 14% | **Disabled** |
| 9 | 2% depth cap on the overnight exemption | AMZN cleared at −2.04%, back to +1.1% 30 min later, the −5% SL never engaged | Revoked (n=2 evidence base) |
| 10 | VIX take-profit discount 0.9/0.7/0.5 | fired 0 times in August | Removed (violates per-symbol bucketing) |
| 11 | Tripwire auto-void enforcement | of 11 enforced voids, **45% provably false** (volume "multiple" vs cumulative shares; "turns green" written as `close>0.0`; cross-asset threshold on the wrong ticker) | Enforce off; detection still logs shadow samples |
| 12 | Confidence feeding sizing/add-ons | contrarian indicator (§4.5) | Disabled |
| 13 | Single-day concentration gate | a day >50% of volume is a one-day event | Criterion A3: do not add |
| 14 | Intraday cumulative-loss breaker (−$400) | backfill n=196 expectancy **+$36.85**; the earlier "−$685/month" was an **entry-order look-ahead bias** | Not graduated; forward sampling only |
| 15 | Early tight SL (1% within 30 min) on live orders | — | Demoted to shadow, later re-expressed as ATR-proportional (0.5×ATR, cap 2.5%) |

---

## 7. Current OKR and honest prior

Active OKR (8/24 → 9/30): **KR1** fix the rulers (day-clustering + MDE) · **KR2** freeze exit parameters
all month to accumulate the first statistically usable single-version window (19 sessions × 11.3 ≈ 215 trades) ·
**KR3** pre-register **exactly one** entry-side hypothesis test · **KR4** keep the four locks and redefine
the graduation gate · **KR5** cost awareness (~$210–245/week of fees as explicit tuition).
**The OKR sets no profit target** — zero gross expectancy is established fact; profit can only come from
entry-side edge. Honest prior: edge has already been searched across 440 candidates / 1,813 signals /
4,057 snapshots — **all zero**.

---

## 8. ❓ Questions (ordered by leverage)

> Constraints: ① simulated account, ~$98.7k; ② mean notional $5.2k, mean fee $4.86 (9.4bps round trip
> incl. a ~$4 commission floor); ③ ~10.7 trades/day of supply; ④ weak power (day-clustered MDE ≈ ±$335/day);
> ⑤ §6 is already falsified — do not repeat; ⑥ discipline requires shadow-first, pre-registration, one variable.

**Q1 — Cost structure (highest priority).** With gross ≈ 0 and 9.4bps round trip, what is mathematically
viable? Compare quantitatively on expectancy and required sample size: (a) raise mean notional to amortise
the commission floor ($5.2k → $15k/$30k = 9.4 → 3.3 → 1.7bps); (b) cut frequency to high-conviction only
(46/wk → 10/wk); (c) extend holding to 3–5 days (E2 failed at −$8/trade, but that was "same signal +
mechanical exit" — would a different signal and exit change the verdict?); (d) change universe (now median
ATR 3.4%, event-driven high vol); (e) concede no edge, stop trading, become a research platform.
**Say explicitly which of these changes the units of the problem and which merely rearranges noise.**

**Q2 — Entry shape (largest single lever).** risk_on LONG −$1,173 (n=159) vs cautious LONG +$945
(n=49, WR 77.6%) → "a dip buyer wearing a momentum chaser's schedule". How do you make dip-buying
executable **without look-ahead bias**? On hand: ADR-035 PULLBACK_LIMIT with TTL (shadow n=4/20).
Specify the limit-order placement formula, the TTL, how non-fill opportunity cost enters scoring, and how
many samples resolve a winner under ±$335/day of noise.

**Q3 — The stop-loss paradox.** SL_INITIAL is the largest bleeding bucket (18 trades, −$2,978, avg −$165),
yet tightening is counterfactually negative (−$203 at 2.5%, −$792 at 1.5%) because winners' path **must
pass through deep water**. Measured false-kill rate: 11 of 17 stops correct, 6 wrong.
**How do you distinguish at entry time — not during the hold — between "a winner that dives then rallies"
and "a trade that was wrong on arrival"?** Observables are limited to pre-market movement, MFE / volume
ratio / VWAP deviation in the first 30 min, ATR bucket, regime, channel, factor `conditions`.
Is the MFE≈0 bucket (13.0% of trades, −$3,548, 84% entered in the first 90 min) that separable criterion?

**Q4 — Channel triage.** DA +$470 / UE −$294 / REENTRY −$75; UE confidence is a constant 50 with 23%
MFE≈0. Shut UE and REENTRY down, gate UE (with what gate?), or redefine UE's signal? Separately, DA SHORT
is 13 trades +$244 (WR 77%) but short supply chronically collapses (the LLM layer turns 35 names into
1 short). How do you raise short supply **without distorting the prompt with a symmetry quota**
(criterion C6 forbids prompt changes for symmetry)?

**Q5 — Overnight policy.** Intraday +$1,570 (PF 1.44) vs overnight −$1,388 (PF 0.23). W_WEEK currently
lets underwater positions sit up to 5 sessions (kill criterion n≥20 with repair <40%; first reading n=6,
50%, −$597). Options: (a) only TRAIL_LOCK-secured winners stay overnight; (b) risk_on LONGs need ≥0.5%
profit; (c) ban overnight. Design the criterion and sample size, and say whether the "underwater repair
window" should exist at all.

**Q6 — Holding-period structure.** 30–60m is +$990 (n=37, WR 86.5%) while >4h is −$1,131 (n=46, WR 43.5%).
Does this argue for compressing holds under an hour? But <30m (n=89) is −$100 with a 66.3% win rate —
small, frequent, fee-sensitive churn. Is the "30–60 minute sweet spot" real structure or **survivorship
bias** (trades that reach 30–60 min were already working)? **Give a design that separates the two.**

**Q7 — Role of the LLM.** Three facts hold at once: selection-layer IC significantly negative
(t1 −0.149 / t5 −0.196) while raw-signal IC is positive (t1 +0.058 / t5 +0.124); LLM selection
underperformed the deterministic fallback (−$224/162 vs +$133/17); **and** E1, replacing LLM selection
with pure signal strength, still lost −$61.55/trade. **How can all three be true?** Is the problem not
"LLM vs no LLM" but the quality or time-alignment of the signal pool itself? Where should an LLM sit —
candidate generation, veto, situational judgement, or out of the main path entirely?

**Q8 — Governance and statistical method.** At ~10.7 trades/day, ±$335/day MDE, ~246 trades/month:
(a) what design reaches a credible conclusion in reasonable time? (b) is "shadow-first + pre-registration +
one variable" so conservative the system **can never converge** (four shadows already hit sample targets
with no adjudication path)? (c) should we move to coarser criteria (monthly portfolio-level) instead of
per-trade? (d) what systemic problem do 4 constant-degradation + 4 unit-mismatch incidents reveal?

**Q9 — The meta question.** Five months, 792 closed trades, cumulative playbook net −$1,884, 100+ debate
and post-mortem documents, 2,560 tests. From the outside: **is this a polishing phase approaching a
breakthrough, or continued investment driven by sunk cost?** Give your most honest judgement — and if it
is the latter, describe a dignified exit or pivot path.

---

## 9. Data conventions (verify anything above yourself)

- `data/ftseek_us.db`, table `trade_records`; read-only: `sqlite3 'file:data/ftseek_us.db?mode=ro'`.
- Closed filter `exit_price > 0`; net `pnl_net` (after fees); gross `realized_pnl`; fees `fees_est`.
- Channel = `decision_id` prefix (`DA-` / `universe_expansion` / `intraday_reentry` / `TFB-`).
- `entry_time`/`exit_time` are ISO strings **with ET offset** (`2026-09-04T09:37:54-04:00`).
- Overnight = `date(entry_time) <> date(exit_time)`. Peak/trough = `mfe_pct`/`mae_pct` (percent).
- `max_drawdown` is in **percentage points** (`100*(mfe+mae)/(100+mfe)`) — a different dimension from
  `pnl_pct` (decimal). Do not mix.
- ⚠️ Exclude when recomputing: `MAIN_PIPELINE` (Apr–Jun legacy, n=179; April 2026 alone is +$16,528 of
  early test data), `SCALPING` (May, n=129, −$3,511), `ORB_ENGINE` (Jun, n=11, −$1,499).
  **Every conclusion here uses `PLAYBOOK_SIM` only.**
- Read-only tools: `scripts/exit_scorecard.py` · `okr_status.py` · `exit_replay_backtest.py`
  (1-min-bar counterfactual replay) · `entry_gate_replay.py` · `historical_robustness_check.py`
  (78 symbols × 5 years daily) · `fee_structure_analysis.py` · `direction_hit_scorecard.py`.
- Documents: `docs/STRATEGY-ROADMAP.md` · `docs/OKR-2026-09.md` · `docs/ARCHITECTURE.md` (running code
  paths with file:line) · `docs/ADJUDICATED-CRITERIA.md` ·
  `docs/reviews/REVIEW-20260904-monthly-trading-review.md` · `handoff.md` (handoff source of truth) ·
  `docs/debates/` (100+ records).
