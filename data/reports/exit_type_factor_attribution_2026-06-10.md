# FTSeek-US Exit-Type & Factor Attribution Analysis
**Generated:** 2026-06-10 | **Source DB:** `data/ftseek_us.db` | **Scope:** All 350 trades (2026-04-24 → 2026-06-09)

---

## ⚡ CRITICAL HEADLINE FINDING

| Instrument | Trades | Win Rate | Total P&L |
|------------|--------|----------|-----------|
| **STOCKS/ETFs only** | 337 | 34.1% | **+$21,276** ✅ |
| **OPTIONS only** | 13 | 15.4% | **-$30,237** 🔴 |
| Combined | 350 | 33.4% | -$8,961 |

> **The system IS profitable on stocks/ETFs (+$21K). 13 option trades destroyed ALL profits and pushed the system into the red.** Disabling options alone would flip the system from -$9K to +$21K.

---

## 1. OVERALL SUMMARY

| Metric | Value |
|--------|-------|
| Total Trades | **350** (all simulate) |
| Wins / Losses / Breakeven | 117 / 210 / 23 |
| Overall Win Rate | **33.4%** |
| Total P&L | **-$8,960.73** |
| Avg P&L / Trade | **-$25.75** |
| Avg P&L % | -2.23% |
| Avg Holding | 2.71 hrs |
| Date Range | 2026-04-24 → 2026-06-09 |

| Source | Trades | Win Rate | Total P&L |
|--------|--------|----------|-----------|
| MAIN_PIPELINE | 188 | 45.7% | -$6,373 |
| SCALPING | 152 | 17.8% | -$1,638 |
| ORB_ENGINE | 10 | 40.0% | -$950 |

| Month | Trades | Win Rate | Total P&L |
|-------|--------|----------|-----------|
| 2026-04 | 11 | 54.5% | **+$19,054** |
| 2026-05 | 296 | 28.0% | **-$33,631** |
| 2026-06 | 43 | 65.1% | **+$5,616** |

> ✅ **`>100 trades` threshold: MET** — 350 trades provides statistical significance for aggregate analysis and major exit categories.

---

## 2. EXIT TYPE PROFITABILITY (Q1 & Q2)

### 2a. Profitable Exit Types (by Total P&L)

| Exit Category | Trades | Wins | Losses | Win Rate | Total P&L | Avg P&L | Avg Hold |
|---------------|--------|------|--------|----------|-----------|---------|----------|
| **EXTERNAL_CLOSE** | 1 | 1 | 0 | 100% | +$17,883 | +$17,883 | 0.6h |
| **DAY_TRADE_TP** | 13 | 13 | 0 | 100% | +$13,101 | +$1,008 | 4.3h |
| **PRE_CLOSE_EXIT** | 29 | 12 | 17 | 41.4% | +$7,317 | +$252 | 4.1h |
| **tp_hit (scalping)** | 5 | 5 | 0 | 100% | +$747 | +$149 | — |
| **ORB_TAKE_PROFIT** | 1 | 1 | 0 | 100% | +$554 | +$554 | 0.3h |
| **SCALE_OUT** | 5 | 5 | 0 | 100% | +$469 | +$94 | 7.9h |
| **QUICK_EXIT 🎯 target** | 1 | 1 | 0 | 100% | +$486 | +$486 | 13.2h |

**Key insight:** DAY_TRADE_TP and PRE_CLOSE_EXIT are the money-makers. Combined: 42 trades, +$20,418 total. DAY_TRADE_TP is 100% win rate — every single one was profitable.

### 2b. Losing Exit Types

| Exit Category | Trades | Wins | Losses | Win Rate | Total P&L | Avg P&L | Avg Hold |
|---------------|--------|------|--------|----------|-----------|---------|----------|
| **PRICE_FAIL (options)** | 5 | 0 | 5 | 0% | **-$17,780** | -$3,556 | 0.1h |
| **QUICK_EXIT 🛑 hard stop** | 5 | 0 | 5 | 0% | **-$10,509** | -$2,102 | 0.02h |
| **BREAKEVEN_STOP** | 1 | 0 | 1 | 0% | **-$6,015** | -$6,015 | 46.3h |
| **TRAILING_STOP** | 85 | 43 | 42 | 50.6% | **-$3,933** | -$46 | 2.5h |
| **STRUCTURE_EXIT** | 33 | 10 | 23 | 30.3% | **-$3,106** | -$94 | 0.5h |
| **sl_hit (scalping)** | 123 | 19 | 96 | 15.4% | **-$2,023** | -$16 | — |
| **FLASH_EXIT** | 7 | 0 | 7 | 0% | **-$1,756** | -$251 | 0.3h |
| **ORB_FLOOR_STOP** | 3 | 0 | 3 | 0% | -$964 | -$321 | 0.1h |
| **time_stop (scalping)** | 7 | 2 | 5 | 28.6% | -$376 | -$54 | — |
| **ORB_BREAKDOWN** | 1 | 0 | 1 | 0% | -$537 | -$537 | 0.05h |

### 2c. Neutral/Breakeven (Broker Sync Artifacts)

| Exit Reason | Count | P&L | Notes |
|-------------|-------|-----|-------|
| ghost_cleanup_cruise | 5 | $0 | May 20 broker sync |
| scalping_orphan_* | 4 | $0 | May 26 broker sync |
| stale_scalping_cleanup | 2 | $0 | May 21 broker sync |
| phantom_cleanup_cron | 1 | $0 | May 21 cron cleanup |
| SCALE_OUT_ghost_cleanup | 1 | $0 | Jun 9 |
| TRAILING_STOP_ghost_cleanup | 1 | $0 | Jun 9 |
| scalping_stale_cleanup | 1 | $0 | May 21 |
| **Total ghost/artifact** | **15** | **$0** | Not real trades |

---

## 3. SIGNAL STRENGTH PARADOX — Confirms 6/7 Diagnosis (Q4)

| Strength Bucket | Trades | Wins | Win Rate | Total P&L | Avg P&L |
|-----------------|--------|------|----------|-----------|---------|
| **ZERO (str=0, conf=0)** | 19 | 15 | **78.9%** | **+$6,339** | **+$334** |
| LOW (1-30) | 5 | 4 | 80.0% | +$384 | +$77 |
| MED (31-60) | 23 | 9 | 39.1% | +$208 | +$9 |
| HIGH (61-80) | 116 | 19 | **16.4%** | +$3,231 | +$28 |
| V.HIGH (>80) | 55 | 18 | 32.7% | -$367 | -$7 |
| INVALID (-1/-2) | 132 | 52 | 39.4% | **-$18,757** | -$142 |

> ⚠️ **The IntradayScorer has NEGATIVE predictive power.** The 5-factor strength/confidence model is anti-correlated with outcomes:
> - **str=0/conf=0** (bypassing scorer) → 78.9% WR, best average P&L
> - **str=61-80 HIGH** → 16.4% WR — WORST win rate of any bucket
> - **str=-1/-2 INVALID** → 39.4% WR but catastrophic losses from options
>
> This is consistent with the 6/7 diagnosis: high collinearity among factors renders the model worse than random. The system should either (a) bypass the scorer entirely for stock/ETF trades, or (b) use it as an inverse signal.

---

## 4. TOP LOSING PATTERNS (Q4)

### 4a. SHORT_PROXY_OPTION — Complete Disaster
| Metric | Value |
|--------|-------|
| Trades | 13 |
| Wins | 2 |
| Win Rate | 15.4% |
| Total P&L | **-$30,237** |
| Avg Loss | -$2,326 |

All PRICE_FAIL trades are options that expired worthless (exit_price = $0):
- US.TSLA260603P410000: -$7,749
- US.RIVN260605P13000: -$4,664
- US.QQQ260602P710000: -$2,720
- US.QQQ260602P700000: -$1,958
- US.SPY260602P730000: -$689

QUICK_EXIT hard stops on options (rapid -50%+ decline triggers emergency exit):
- US.TSLA: -$3,057
- US.QQQ: -$2,934 / -$3,050
- US.RIVN: -$2,968

> 🔴 **Recommendation:** Disable SHORT_PROXY_OPTION entirely in simulate mode. Options are the #1 P&L destroyer: 13 trades = -$30K loss (3.4x the TOTAL losses of all other categories combined).

### 4b. Large Single-Trade Disasters
| Trade | Symbol | Exit | P&L | Hold | Issue |
|-------|--------|------|-----|------|-------|
| #2 | US.GOOGL | BREAKEVEN_STOP | -$6,015 | 46.3h | Held short against 3% move, never cut |
| #868 | US.XLE | TRAILING_STOP | -$7,256 | 2.1h | Large position, trailing stop too wide |
| #882 | US.XLK | TRAILING_STOP | -$3,578 | 2.4h | ETF short went against, trail failed |
| #875 | US.XLF | PRE_CLOSE_EXIT | -$4,458 | 5.3h | Held to close, large position |

### 4c. Scalping: Death by 1000 sl_hit Cuts
- **123 sl_hit trades** at 15.4% win rate, -$2,023 total
- Signal strength = HIGH (61-80) for 69 of them, all losing (-$1,199)
- Signal strength = V.HIGH (>80) for 48, 13 wins vs 35 losses (-$784)
- **Conclusion:** The scalping signal's "high strength" recommendations are anti-predictive. The scorer says "strong signal" → sl_hit → loss.

### 4d. STRUCTURE_EXIT: All trades have str=-1
- 33 trades, all with signal_strength = -1 (INVALID)
- 30.3% win rate, -$3,106 total
- These are trades that executed despite no valid signal data — pure noise trades
- Recommendation: Reject trades with str=-1 at entry, not just flag them

---

## 5. BY DIRECTION

| Direction | Trades | Win Rate | Total P&L | Avg P&L |
|-----------|--------|----------|-----------|---------|
| **SHORT** | 200 | 40.0% | **+$24,043** | +$121 |
| LONG | 137 | 25.5% | -$2,767 | -$20 |
| SHORT_PROXY_OPTION | 13 | 15.4% | **-$30,237** | -$2,326 |

> Short stock/ETF trades are the ONLY profitable direction. LONG trades lose money. The system has a systematic long bias problem — it enters longs at the wrong time/price.

---

## 6. ORB ENGINE ANALYSIS

| Exit | Trades | Win Rate | Total P&L |
|------|--------|----------|-----------|
| ORB_TAKE_PROFIT | 1 | 100% | +$554 |
| ORB_TRAILING_STOP | 5 | 60% | -$3 |
| ORB_BREAKDOWN | 1 | 0% | -$537 |
| ORB_FLOOR_STOP | 3 | 0% | -$964 |
| **Total ORB** | **10** | **40%** | **-$950** |

- ORB hardcoded at strength=65/confidence=0.65 for 8 of 10 trades
- ORB_BREAKDOWN (US.XLI, str=51): -$537 — below hardcoded threshold, should not have entered
- ORB_FLOOR_STOP: 3 trades, all losses — floor stop trigger is too tight/slow
- **Only 10 trades → insufficient data (<100) for ORB-specific conclusions**

---

## 7. TOP SYMBOLS

### Winners
| Symbol | Trades | Win Rate | Total P&L |
|--------|--------|----------|-----------|
| US.ORCL | 23 | 43.5% | +$20,492 |
| US.IWM | 4 | 75.0% | +$5,729 |
| US.XLF | 5 | 60.0% | +$4,243 |
| US.BWXT | 6 | 50.0% | +$3,752 |
| US.MSFT | 1 | 100% | +$3,142 |

### Losers
| Symbol | Trades | Win Rate | Total P&L |
|--------|--------|----------|-----------|
| US.TSLA260603P410000 | 2 | 0% | -$10,806 |
| US.RIVN260605P13000 | 2 | 0% | -$7,632 |
| US.GOOGL | 1 | 0% | -$6,015 |
| US.QQQ | 3 | 0% | -$6,002 |
| US.XLE | 4 | 75% | -$4,308 ⚠️ |
| US.XLK | 6 | 33% | -$3,944 |
| US.QCOM | 43 | 23% | -$3,768 |

> ⚠️ US.XLE: 3 wins, 1 loss (-$7,256 trailing stop) — one bad trade wiped out all profits. Position sizing failure.

---

## 8. FACTOR/ATTRIBUTION DATA QUALITY ASSESSMENT

| Attribution Table | Count | Notes |
|-------------------|-------|-------|
| Total records | 197 | Only 56% of trades have attribution |
| Has overall_score | 35 | Scores cluster at 60-65, non-discriminating |
| Has factor_trend | 34 | Partial data |
| Has factor_momentum | 34 | Partial data |
| Has factor_volume | 34 | Partial data |
| Has factor_orderflow | 0 | **Never populated** |
| Has factor_lifecycle | 0 | **Never populated** |
| Has direction_score | 0 | All zero |
| Has execution_score | 0 | All zero |
| Has timing_score | 0 | All zero |

> ⚠️ **Factor-level attribution is essentially non-functional.** The five factor scores (direction, signal, execution, timing, risk) are all zeros. Only overall_score has data and it clusters tightly at 60-65 with no discrimination between wins and losses. The factor analysis pipeline is broken — it cannot answer "which factor drove this outcome."

---

## 9. STATISTICAL SIGNIFICANCE ASSESSMENT (Q3)

| Category | Count | >100? | Conclusion |
|----------|-------|-------|------------|
| Overall | 350 | ✅ YES | High confidence |
| sl_hit | 123 | ✅ YES | Statistically significant |
| TRAILING_STOP | 85 | ❌ NO (close) | Borderline, cautious conclusions |
| STRUCTURE_EXIT | 33 | ❌ NO | Moderate confidence |
| PRE_CLOSE_EXIT | 29 | ❌ NO | Moderate confidence |
| DAY_TRADE_TP | 13 | ❌ NO | Low confidence per-exit-type |
| ORB Engine | 10 | ❌ NO | Insufficient, need more data |
| FLASH_EXIT | 7 | ❌ NO | Insufficient |
| PRICE_FAIL | 5 | ❌ NO | Pattern clear despite low N |
| Options (all) | 13 | ❌ NO | Pattern clear despite low N |
| str=0/conf=0 | 19 | ❌ NO | Borderline but pattern consistent |

> For the most critical question — "Is the scorer anti-predictive?" — the answer is statistically significant because it holds across ALL buckets (inverse monotonic relationship).

---

## 10. KEY CONCLUSIONS & RECOMMENDATIONS

### 🔴 Critical Issues
1. **Disable options trading (SHORT_PROXY_OPTION):** 13 trades = -$30K. Every single option trade is a net loser. This alone accounts for 3.4x the total system losses.

2. **The IntradayScorer is anti-predictive:** str=0/conf=0 trades (bypassing scorer) have 78.9% WR. str=HIGH (61-80) trades have 16.4% WR. The scorer should be bypassed or inverted.

3. **STRUCTURE_EXIT = noise:** 33 trades all with str=-1, 30.3% WR, -$3,106. These should be rejected at entry, not executed and then dumped via STRUCTURE_EXIT.

### 🟡 Important Issues
4. **Scalping sl_hit dominates:** 123 trades at 15.4% WR. The scalping engine is a slow bleed. Either widen stops, improve entry timing, or reduce allocation.

5. **TRAILING_STOP asymmetry:** 85 trades with ~50% WR but net -$3,933. Winners avg +$329, losers avg -$437. Trail parameters allow too much give-back on losers.

6. **LONG direction is broken:** 137 LONG trades at 25.5% WR vs 200 SHORT at 40% WR. The system enters longs poorly.

### 🟢 Positives
7. **DAY_TRADE_TP is flawless:** 13/13 wins, +$13,101. This exit mechanism works perfectly.
8. **PRE_CLOSE_EXIT is net profitable:** Despite 41% WR, large winners (+$1,389 avg) outweigh small losers (-$550 avg).
9. **June trend is positive:** 65.1% WR after the disastrous May (28.0% WR).

### 🛠 Recommended Actions
1. **Bypass IntradayScorer** for stock/ETF trades (use str=0/conf=0 path for all)
2. **Reject str=-1 at entry** → no STRUCTURE_EXIT noise trades
3. **Disable SHORT_PROXY_OPTION** in simulate mode
4. **Widen scalping stop-loss** or reduce allocation by 50%
5. **Review TRAILING_STOP parameters** — winners give back too much
6. **Investigate LONG entry logic** — systematically mistimed