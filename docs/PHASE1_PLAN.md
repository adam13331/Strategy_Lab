# SQX-like Strategy Lab — Phase 1 Plan

**Status:** draft for approval
**Scope:** data foundation, indicator library, strategy generator, 1-minute
execution backtester, IS/OOS acceptance gates, autocorrelation filter, web UI.
**Out of scope for Phase 1:** robustness suite (Phase 2), portfolio builder
(Phase 3), live trading, broker integration.

---

## 1. Goal

Build a local, reproducible pipeline that turns raw M1 broker CSVs into a
searchable database of fully specified trading strategies which have already
survived:

1. structural de-duplication,
2. in-sample acceptance gates,
3. out-of-sample acceptance gates,
4. return-correlation de-duplication.

The output of Phase 1 is a curated set of candidates that Phase 2 will stress
test and Phase 3 will assemble into a portfolio.

The web UI is the primary control surface: import data, choose instruments and
timeframes, toggle indicators, configure every generation rule and acceptance
threshold, run the search, watch it live, and explore the survivors.

---

## 2. What the sample data tells us (verified)

`USDJPY-M1-No Session.csv`:

| Property | Value |
|---|---|
| Rows | 4,392,425 |
| Range | 2015-01-01 22:00 -> 2026-10-15 23:59 (server clock) |
| Columns | `Date,Time,Open,High,Low,Close,Volume` |
| Duplicate timestamps | 0 |
| Non-monotonic timestamps | 0 |
| Invalid OHLC rows | 0 |
| Saturday bars | 0 |
| Sunday bars | 94,160 |
| Trading week | Sunday 22:00 -> Friday 21:59 |
| Weekend break | 48h01m typical (no DST shift observed) |

Conclusion: the file is clean and the server clock is almost certainly **UTC**
(week opens Sunday 22:00 UTC, closes Friday 22:00 UTC, constant across years).
The importer will still make the timezone explicit and configurable rather than
baking in the assumption.

Volume values are large (up to ~160M). They are usable for relative volume
indicators but must not be interpreted as contract counts.

---

## 3. High-level architecture

```
┌──────────────────────────── Browser (React SPA) ───────────────────────────┐
│ Data · Indicators · Generator · Acceptance · Run monitor · Results          │
└───────────────────────────────┬────────────────────────────────────────────┘
                                │ REST + SSE (localhost)
┌───────────────────────────────▼────────────────────────────────────────────┐
│ FastAPI backend                                                            │
│  - config validation (Pydantic models shared with the generator)           │
│  - run orchestration, progress fan-out, cancellation                       │
│  - owns the single DuckDB writer connection                                │
└───────────────┬───────────────────────────────┬────────────────────────────┘
                │ job queue (DuckDB tables)      │ static files (web/dist)
┌───────────────▼──────────────┐   ┌────────────▼───────────────────────────┐
│ Supervisor / worker pool     │   │ DuckDB + Parquet                       │
│  (numpy + numba, 2-3 procs)  │◄──┤  bars (read-only Parquet)              │
│  - sampler, backtester       │   │  indicator cache, results, metadata    │
└──────────────────────────────┘   └────────────────────────────────────────┘
```

Key decision: **DuckDB has a single-writer model.** The orchestrator process is
the only writer. Worker processes read bars and indicator caches directly from
Parquet (pyarrow) and stream result batches back over a queue. This avoids
multi-process database locking entirely.

Second key decision: **signals on the chosen timeframe, fills on M1.** Indicators
and entry/exit conditions are evaluated on closed TF bars; orders are filled on
the next M1 bar. This satisfies the "1-minute OHLC execution quality"
requirement without needing tick data.

---

## 4. Technology choices

| Layer | Choice | Why |
|---|---|---|
| Runtime | Python 3.12 + `uv` | already available, fast env/lock management |
| Analytics store | DuckDB + Parquet | columnar, zero-copy views over 4.4M rows, SQL for UI queries |
| Compute | numpy + numba | backtest inner loop and indicator math; 4 cores available |
| Indicators | TA-Lib 0.6 wheels + custom vectorized library | wide coverage; custom fallback where TA-Lib lacks an indicator |
| DataFrames | pandas for ingest/resample, numpy inside hot loops | convenience at boundaries only |
| API | FastAPI + Pydantic v2 + SSE | typed configs, OpenAPI -> generated TS client |
| Jobs | in-process supervisor + `ProcessPoolExecutor` (2 workers default on this machine) | no Redis/Celery service to operate |
| Frontend | React + TypeScript + Vite | rich forms/charts; static build served by FastAPI |
| UI kit | Tailwind + shadcn/ui, TanStack Query/Table, React Hook Form + zod | fast, consistent, no design work |
| Charts | TradingView `lightweight-charts` (candles/equity) + Recharts (bars/heatmap) | performant financial charts |
| CLI | Typer (`sqx data import`, `sqx run`, `sqx serve`) | automation + debugging without the UI |
| Tests | pytest, hypothesis (targeted), Playwright smoke (optional) | correctness of money math is the main risk |

This machine: 4 cores, ~7 GB RAM (~4 GB available). The default worker count is
2, configurable to 3. Symbols are processed sequentially inside a worker; never
load all instruments at once.

Fallback if TA-Lib wheels fail on this host: ship pure-numpy implementations of
the Phase 1 indicator set and keep the exact same catalogue/registry API.
`pandas-ta` is optional and not a required dependency (it has had numpy/py3.12
compatibility churn).

---

## 5. Repository layout

```
SQX/
  data/
    raw/                       # user-supplied CSVs (sample stays in repo root)
    parquet/USDJPY/m1.parquet  # normalized, sorted, validated bars
    parquet/USDJPY/h1.parquet  # materialized resamples
    cache/indicators/          # (symbol, tf, indicator, params) -> .npy/.parquet
    results/<run_id>/          # equity, trades, config snapshot
    sqx.duckdb                 # metadata + result index (single writer)
  src/sqx/
    api/            main.py, routes/, schemas.py, events.py
    data/           ingest.py, resample.py, quality.py, store.py
    indicators/     catalog.py, registry.py, talib_impl.py, custom_impl.py, cache.py
    strategy/       dsl.py, sampler.py, generator.py, dedupe.py
    backtest/       engine.py, execution.py, sizing.py, sessions.py, metrics.py
    jobs/           supervisor.py, worker.py, queue.py
    cli.py
  web/              src/pages, components, api, charts
  tests/            unit, golden, integration
  configs/          example YAML configs
  docs/
  pyproject.toml
  Makefile
```

---

## 6. Data pipeline

### 6.1 Folder and file handling

- `data/raw/` is the drop folder for more CSVs (created).
- Filename pattern `{SYMBOL}-{TF}-{label}.csv`; importer also accepts an
  explicit symbol override in the import form.
- The existing sample remains where it is; the import form accepts any path.
- Raw files are never modified. A manifest records source path, SHA-256, row
  count, date range and detected timezone offset.

### 6.2 Import algorithm

1. Stream the CSV (do not load 4.4M rows through Python `csv` module in the
   hot path; use `duckdb.read_csv` with explicit column types).
2. Parse `Date` + `Time` -> naive `TIMESTAMP`; apply configured source offset
   (default: treat as UTC, justified above).
3. Validate `High >= max(Open, Close)`, `Low <= min(Open, Close)`,
   `High >= Low`, non-negative volume, non-null prices. Quarantine bad rows and
   report them; do not silently fix.
4. Deduplicate identical timestamps (keep first, record count).
5. Sort ascending, write ZSTD Parquet partitioned by symbol:
   `data/parquet/USDJPY/m1.parquet`.
6. Compute the quality report: coverage, per-year bar counts, duplicate count,
   invalid rows, largest gaps, missing-minute estimate, weekday distribution,
   session-open/close inference.
7. Upsert `symbols` and `bar_sources` rows in DuckDB.

Supported inputs: M1 and any higher base TF. If the source TF is not M1 the
importer records it and disables M1 execution until M1 data is supplied.

### 6.3 Resampling

`TFs: M1, M5, M15, M30, H1, H4, D1, W1, MN1` (Phase 1 can ship M1..D1; W1/MN1
are trivial once the rule exists).

- OHLCV aggregation: first open, max high, min low, last close, sum volume.
- Buckets are anchored at 00:00 UTC and floor-aligned to the TF duration.
  H4 therefore buckets 00/04/08/...; D1 at 00:00 UTC; W1 Monday 00:00.
- Incomplete trailing buckets are dropped.
- Weekend gaps are preserved (no forward filling). Indicators that need
  continuous time use bar index, not wall-clock duration.
- Materialize to `data/parquet/{symbol}/{tf}.parquet`; invalidate on re-import.
- Alternative anchors (broker day start, 22:00) are a config field, default
  floor-aligned UTC.

Duplicate-source rule: one active dataset per symbol. Re-importing the same
symbol replaces it only after a successful staged build.

---

## 7. Indicator catalogue (Phase 1)

Each indicator is registered with: id, label, category, parameter definitions
(name/type/min/max/step/default), outputs, warm-up bars, volume requirement,
and which comparison operators make sense.

The UI lists every indicator with an on/off switch plus editable parameter
ranges. Disabled indicators are excluded from generation.

### 7.1 Trend
| Indicator | Parameters (defaults) |
|---|---|
| SMA | period 20 (5-200) |
| EMA | period 20 (5-200) |
| WMA | period 20 |
| HMA | period 20 |
| DEMA / TEMA | period 20 |
| KAMA | period 10, fast 2, slow 30 |
| VWMA | period 20 |
| MACD | fast 12 (5-30), slow 26 (15-60), signal 9 |
| ADX / +DI / -DI | period 14 (7-30), threshold 20-30 |
| Aroon up/down | period 14 |
| Supertrend | period 10, multiplier 3.0 (1.0-5.0) |
| Parabolic SAR | step 0.02, max 0.2 |
| Ichimoku | tenkan 9, kijun 26, senkou 52 |
| Alligator | jaw 13, teeth 8, lips 5 |
| Vortex +/- | period 14 |
| Linear regression slope | period 20 |
| Price vs MA | MA type + period; distance in % or ATR |

### 7.2 Momentum
| Indicator | Parameters |
|---|---|
| RSI | period 14 (5-30), levels 20/30/70/80 |
| Stochastic %K/%D | k 14, d 3, smooth 3, levels 20/80 |
| StochRSI | rsi 14, stoch 14, k 3, d 3 |
| CCI | period 20, levels ±100/±200 |
| Williams %R | period 14, levels -20/-80 |
| MFI | period 14, levels 20/80 |
| ROC | period 10 |
| Momentum | period 10 |
| TSI | long 25, short 13 |
| Ultimate Oscillator | 7/14/28, levels 30/70 |
| Awesome Oscillator | fast 5, slow 34 |
| Fisher Transform | period 10 |
| DeMarker | period 14 |
| TRIX | period 15, signal 9 |

### 7.3 Volatility / channels
| Indicator | Parameters |
|---|---|
| ATR | period 14 (5-30) |
| Bollinger Bands | period 20, std 2.0; %B and bandwidth |
| Keltner Channels | EMA 20, ATR 10, mult 2.0 |
| Donchian Channels | period 20 (5-100) |
| Standard deviation | period 20 |
| Historical volatility | period 20, annualization 252 |
| Chaikin volatility | EMA 10, ROC 10 |

### 7.4 Volume
| Indicator | Parameters |
|---|---|
| OBV | optional MA period |
| Accumulation/Distribution | - |
| Chaikin Money Flow | period 20 |
| Volume SMA / ratio | period 20 |
| Force Index | period 13 |
| Ease of Movement | period 14 |

### 7.5 Price action / session
| Indicator | Parameters |
|---|---|
| N-bar breakout high/low | N 20 (5-100) |
| Higher-high / higher-low count | N 3 |
| Inside / outside bar | - |
| Engulfing, pin bar, doji, hammer/shooting star | body/wick ratios |
| Close vs open, range expansion | lookback 20 |
| Gap from previous close | ATR-normalized threshold |
| Time of day / day of week | handled globally, also usable as filter condition |
| Bars since session open | - |
| End-of-day proximity | minutes before session close |

Phase 1 target: ~30-35 implemented indicators with parameter ranges. Quality
beats quantity: every enabled indicator must have tests and a documented
comparison operand.

### 7.6 Condition model

A condition is one of:

- `indicator_output OP constant` (threshold sampled from the indicator's grid)
- `indicator_output OP price` (close/open/high/low)
- `indicator_output OP indicator_output` (second indicator, shared params)
- `indicator_output is rising/falling` over N bars

Operators: `>`, `<`, cross above, cross below, rising, falling, between.
Parameter values are sampled from the user's range on a discrete grid, so the
search space is enumerable and reproducible.

Warm-up is computed per indicator/parameter set; strategies whose IS segment
begins before warm-up completes are rejected for that run (or the data window is
trimmed automatically with a warning).

---

## 8. Strategy model (DSL)

A strategy is a versioned JSON document (canonicalized + hashed):

```jsonc
{
  "schema": 1,
  "symbol": "USDJPY",
  "timeframe": "H1",
  "direction": "both",              // long | short | both
  "entry": {
    "long":  [ /* conditions, AND-joined */ ],
    "short": [ /* conditions */ ],
    "max_conditions": 3
  },
  "exit": {
    "conditions": [ /* max_exit_conditions */ ],
    "opposite_signal": "ignore",    // ignore | reverse (per strategy)
    "stop_loss": {
      "mode": "use",                // use | none
      "type": "atr",                // fixed | atr
      "pips": null,
      "atr_period": 14,
      "atr_mult": 2.5
    },
    "take_profit": { "...same shape..." },
    "trailing": {
      "mode": "none",               // none | use
      "type": "atr",
      "atr_period": 14,
      "atr_mult": 2.0
    },
    "max_bars_in_trade": 240,
    "close_at_session_end": true
  },
  "risk": {
    "sizing": "risk_percent",       // risk_percent | fixed_lot
    "risk_percent": 1.0,
    "fixed_lot": 0.1
  },
  "meta": { "seed": 1234, "generator_version": "1.0.0" }
}
```

Human-readable rendering is generated from the DSL for the UI
(e.g. "RSI(14) crosses below 30 AND close > EMA(50) -> long").

### 8.1 Generator policy mapping

| UI setting | Behaviour |
|---|---|
| Direction | fixed per strategy: long, short, or sampled both |
| Opposite signal | `ignore` / `reverse` / `ignore or reverse` (sampled per strategy) |
| Stop loss: always / never / may | always -> use, never -> none, may -> 50/50 |
| SL type: fixed / ATR | sample pips in range, or sample ATR period + multiplier ranges |
| Take profit | identical policy semantics |
| Trailing: no / yes | if yes, ATR period + multiplier ranges |
| Max entry indicators | number of conditions per side, 1..N |
| Max exit indicators | number of exit conditions, 0..N |
| Indicator toggles | restrict the pool used for entries/exits |
| Timeframe | indicator/signal timeframe; fills always on M1 |
| OOS % | 10/20/30/40/50, time-ordered split |
| Session window | entries only inside window; force close at end hour |
| Days of week | entries only on selected weekdays |

Rules and guardrails:

- Exactly one position at a time per strategy (Phase 1). No pyramiding.
- If SL, TP, trailing, exit conditions and opposite-signal handling are all
  inactive, the generator must still add `max_bars_in_trade` or session-end
  exit. Strategies with no possible exit are invalid.
- Long and short rule sets must not be trivially identical unless direction is
  single-sided.
- SL/TP distances must be positive and inside sane bounds relative to ATR.
- Random sampling with a run seed; every sampled strategy hash is recorded so
  reruns are bit-for-bit reproducible.

### 8.2 Search loop

```
for attempt in 1..max_attempts:
    spec = sample_strategy(rng, config)
    if structural_duplicate(spec): reject("structural_duplicate"); continue
    if not backtest_valid(spec): reject(reason); continue        # no trades, no data
    if not passes_is(metrics_is): reject("is_gate:..."); continue
    if not passes_oos(metrics_oos): reject("oos_gate:..."); continue
    if correlated_with_accepted(metrics/equity): reject("autocorrelation"); continue
    accept(spec, metrics); if accepted_count == target: stop
```

The funnel counts and the last rejection reason are persisted for every attempt,
so the UI can show why strategies fail (useful for tuning thresholds).

Structural de-duplication runs before the expensive backtest. Return-correlation
de-duplication runs only after the strategy passes the gates.

---

## 9. Autocorrelation (de-duplication) filter

The requirement is "not the same strategy type with slightly modified
parameters", implemented in two layers:

1. **Structural fingerprint (always on).** Canonicalize the rule tree: indicator
   family, operator, operand type, direction, session, and parameter values
   snapped to a coarse grid (e.g. periods to the nearest 5, multipliers to 0.5).
   Compare against accepted fingerprints; reject at/above the structural
   similarity threshold (default: identical after snapping).
2. **Return correlation (toggle + threshold).** Align daily returns of the
   candidate and each accepted strategy over the IS window (OOS excluded to
   avoid leakage), compute Pearson correlation; reject when
   `max_corr >= threshold` (default 0.95). Spearman is available as an option.
   For large accepted sets, compare against a reservoir of representative
   strategies rather than every pair, with a full pass on the final set.

Also retained: correlation matrix and nearest-neighbour id for each accepted
strategy, so the results UI can show clusters.

Threshold UI: on/off, correlation threshold slider 0.50-0.999, method
(Pearson/Spearman), window (IS only / IS+OOS).

---

## 10. Backtester semantics

This is the most important part to get right and to document in the UI.

### 10.1 Data flow

1. Resolve symbol + TF bars and the M1 execution bars.
2. Precompute (or load from cache) every indicator array required by the
   strategy, on the full history with warm-up.
3. Build long/short entry signal at the close of each completed TF bar.
4. Map each signal to the first M1 bar whose timestamp is strictly after the
   signal bar close.
5. Walk M1 bars in numba, maintaining one position and an equity curve.
6. Split equity/trades into IS and OOS segments for metric calculation.

### 10.2 Fill and cost model

- Default fills at next M1 open.
- Spread: per-symbol, in pips (USDJPY default 0.8 pip round-trip distributed
  half on entry, half on exit), applied as a cost against the position.
- Slippage: per-side pips (default 0.0, configurable for robustness later).
- Commission: per lot, round-turn (default 0).
- Gaps through SL/TP fill at the bar open, not the level.
- If SL and TP are both touched inside one M1 bar, assume the **worst case
  (SL first)** by default; an "optimistic" mode exists only for comparison.
- Session-end and data-end exits use the next available price; mark-to-market
  closes the final position at the last M1 close.

### 10.3 Position sizing

- Account currency: USD (configurable).
- Contract size: 100,000 units per lot (FX).
- `fixed_lot` (**default 0.10**): each trade uses 0.10 lots regardless of equity.
- `risk_percent` (optional): lots = (equity * risk%) / (SL distance in pips *
  pip value per lot). SL is mandatory for this mode; if the sampled strategy has
  no SL, fall back to fixed lot with a UI warning.
- Leverage is a hard cap: required margin = notional / leverage must not exceed
  free equity; if it does, reduce lot size. Leverage default 30.
- USDJPY pip value per lot = 100,000 * 0.01 / USDJPY ≈ $6-7; computed from the
  fill-time rate, not hard-coded.
- No compounding surprises: equity updated per M1 bar from closed and open PnL.

### 10.4 Session / trading hours

- Trading window start/end hour (0-23), supports wrap-around (e.g. 22 -> 06).
- Days of week: entries permitted only on selected days.
- At the end hour, open positions are closed at the next M1 open.
- No entries within the last `N` minutes of the window (default 1 bar) and no
  entries on non-selected days, even if the window wraps.
- The same session definition applies to IS, OOS and future robustness runs.
- Time is interpreted in a configured **session clock** (default UTC). The data
  import timestamp is naive; the UI shows the assumption.

### 10.5 Exit precedence per M1 bar

1. Gap-open fill of a resting SL/TP.
2. Intrabar SL/TP (conservative ordering), trailing update.
3. Opposite signal close/reverse at next open (config per strategy).
4. Exit-condition signal at the next open.
5. Session-end forced close.
6. Max-bars-in-trade close.

### 10.6 Metrics (calculated separately for IS and OOS)

| Metric | Definition |
|---|---|
| Trades | closed trades in the segment |
| Net return % | final equity / initial equity - 1 |
| Max drawdown % | max peak-to-trough on the M1 mark-to-market equity |
| Return/DD | net return % / max drawdown % (0 if no DD) |
| Sharpe | mean/std of daily equity returns * sqrt(252), rf = 0 |
| Stagnation days | longest calendar-day gap between equity highs |
| Profit factor | gross profit / gross loss |
| Win rate | winning trades / closed trades |
| Avg trade / expectancy | in pips and in %, both stored |
| Exposure | % of session bars with an open position |

Acceptance gates (all must pass, configured for IS and OOS independently):

- min trades
- min Sharpe
- max stagnation days
- min return/DD
- optional: max drawdown %, min profit factor, min win rate
- optional consistency gate: OOS Sharpe must be >= X * IS Sharpe (default off)

OOS = last 10/20/30/40/50% of the time range (**default 40%**), set with both a
slider and a numeric edit field. A purge/embargo of `k` bars
(default: one TF bar + max indicator warm-up) is applied at the boundary so no
position or indicator straddles the split.

The UI preflight warns when the selected OOS % cannot plausibly produce the
minimum number of OOS trades (based on the IS trade frequency).

---

## 11. Run configuration (UI surface)

### Global
- initial balance (default 10,000 USD), account currency, leverage (30)
- sizing mode: fixed lot (default **0.10**) or risk % per trade (default 1.0%)
- spread/slippage/commission defaults per symbol
- random seed, worker count, target accepted strategies
- no max attempt cap: the run loops until the target is reached or the user stops it
- date range override (default: full imported history)

### Data / instrument
- symbols discovered from imported CSVs (checkbox list)
- timeframe: M1, M5, M15, M30, H1, H4, D1
- direction: long / short / both
- OOS: 10/20/30/40/50% (default 40%), slider + editable numeric field

### Generation
- opposite entry signal: ignore / reverse / ignore-or-reverse
- stop loss: always / never / may; type fixed (pips min-max) or ATR
  (period min-max, multiplier min-max)
- take profit: same shape
- trailing: none / ATR (period min-max, multiplier min-max)
- max entry indicators, max exit indicators (1..6)
- indicator on/off list with per-parameter ranges
- session start hour, end hour, close-at-end toggle, days-of-week checkboxes
- autocorrelation: on/off, threshold, method
- acceptance gates table (IS and OOS columns): min trades, Sharpe, stagnation
  days, return/DD, optional DD/PF/win rate

### Run control
- preflight estimate (active indicators, sampled space size, data coverage,
  OOS trade warning, estimated runtime at measured throughput)
- start / pause / cancel / resume
- live funnel: attempted -> unique -> traded -> IS pass -> OOS pass -> accepted
- throughput, ETA, worker status, recent accepts

Every run stores an immutable snapshot of the config, seed, data manifest hash
and code version. Same inputs -> same outputs.

---

## 12. Web UI

### Navigation
1. **Dashboard** — data coverage, recent runs, funnel summary, promoted counts.
2. **Data** — instruments, bar counts, range, gaps/quality report, import
   wizard, resample cache status.
3. **Indicators** — searchable catalogue grouped by category; per-indicator
   toggle, description, parameter ranges, inputs/outputs, warm-up.
4. **Generator** — all settings from section 11, with validation, presets,
   import/export YAML/JSON.
5. **Acceptance** — IS/OOS gate editor (shared component reused by Phase 2).
6. **Run monitor** — live progress, funnel chart, log tail, cancel/pause.
7. **Results** — filterable strategy table (symbol, TF, direction, IS/OOS
   metrics, status), sorting, column selection, CSV export.
8. **Strategy detail** — human-readable rules, equity + drawdown chart, monthly
   returns heatmap, trade list, IS vs OOS comparison, nearest correlated
   strategies.
9. **Settings** — data paths, defaults, worker count, theme.
10. **Robustness / Portfolio** — visible but disabled with "Phase 2/3" badges.

### UX details
- Range inputs are dual-handle min/max with step; showing the resulting grid
  cardinality prevents accidental combinatorial explosions.
- Session hours use a visual 24h timeline with selected days.
- Preflight panel updates live and blocks start on invalid configs.
- All numeric inputs validated server-side; the same Pydantic models are used
  by the CLI and job runner.
- Server-side pagination for results; no million-row downloads.
- Errors show the offending field and the rejection reason from the job log.

### API sketch
```
GET    /api/health
GET    /api/symbols
POST   /api/data/import            (path or upload)
GET    /api/data/quality?symbol=
POST   /api/data/resample
GET    /api/indicators             (catalogue + current selection)
PUT    /api/indicators/selection
GET    /api/configs   POST/PUT/DELETE /api/configs
POST   /api/preflight
POST   /api/runs                   (config or config_id + seed)
GET    /api/runs                   GET /api/runs/{id}
POST   /api/runs/{id}/cancel|pause|resume
GET    /api/runs/{id}/events       (SSE: progress, funnel, log)
GET    /api/strategies?run_id=...&filters...&page=...
GET    /api/strategies/{id}
GET    /api/strategies/{id}/equity|trades|correlations
GET    /api/runs/{id}/export       (CSV/JSON bundle)
```

---

## 13. Persistence schema (DuckDB)

```
symbols(symbol PK, source_file, timeframe, rows, start_ts, end_ts,
        tz_assumed, sha256, imported_at, quality_json)

bar_cache(symbol, timeframe, anchor, rows, start_ts, end_ts, path, built_at,
          PK(symbol,timeframe,anchor))

runs(run_id PK, name, config_json, config_hash, seed, code_version,
     data_hash, status, created_at, started_at, finished_at, stats_json)

strategies(strategy_id PK, run_id, spec_json, spec_hash, symbol, timeframe,
           direction, status, reject_reason, metrics_is_json,
           metrics_oos_json, score, correlation_max, nearest_id, created_at)

jobs(job_id PK, run_id, kind, status, progress, pid, error, updated_at)

robustness_results(...)   -- reserved for Phase 2
portfolio_runs(...)       -- reserved for Phase 3
```

Large artifacts live as Parquet under `data/results/<run_id>/`:
- `accepted/<strategy_id>.parquet` — equity (daily), trades, signal markers
- `rejected/` — metrics only (keeps disk bounded)
- `accepted_daily_returns.parquet` — matrix used for correlation filtering

Rejected candidates keep their reason and metrics but not their trade blobs.
A configurable retention setting can drop rejected metrics after N days.

Estimated disk for 200 accepted + 50k rejected attempts: well under 2 GB
(metrics-only rejects; daily equity ~2,900 rows per accepted strategy).

---

## 14. Job orchestration and performance

- A run is split into work units (strategy samples) handled by a supervisor.
- Worker pool: 2 processes by default (3 max), one symbol slice at a time.
- Progress is aggregated and flushed to DuckDB every ~1 s; SSE fans out to the
  browser.
- Cancellation is cooperative (checked between strategies), with a hard-stop
  fallback; partial results remain queryable.
- Pause stops dequeuing new work; running strategies finish.
- Resume reconstructs the queue from the persisted attempt counter + seed.
  Because the sampler is seeded and deterministic, resume reproduces the exact
  sequence.

Performance targets (benchmarked in M4, not assumed):
- import 4.4M rows < 60 s
- materialize all TFs < 30 s
- warm indicator cache hit rate ~100% for repeated runs
- aggregate throughput >= 10 strategies/s across 2 workers for a typical H1
  config; optimize the M1 loop with numba if below.
- For very large attempt budgets, an overnight run is acceptable; the UI shows
  ETA and checkpoints.

Likely bottlenecks: M1 mark-to-market equity, session scans, and correlation
against a large accepted set. Each has a targeted optimization (numa-friendly
flat arrays, sparse trade windows, incremental correlation matrix).

---

## 15. Testing and validation

1. **Data tests:** resample OHLCV against pandas reference; DST/gap handling;
   idempotent re-import; duplicate/bad-row quarantine.
2. **Indicator tests:** compare every TA-Lib-backed indicator with the TA-Lib
   reference on sample data; custom indicators against hand-computed fixtures.
   NaN/warm-up boundaries included.
3. **Backtest golden tests:** synthetic 1-minute paths for:
   - SL hit before TP, TP before SL, both in one bar (conservative)
   - gap through SL/TP
   - trailing stop ratchet
   - session-end forced close, wrap-around window, day filters
   - opposite-signal ignore vs reverse
   - sizing/leverage caps
4. **Look-ahead property tests:** appending future bars must not change any
   earlier signal or trade; shifting the OOS split must not leak.
5. **Metrics tests:** hand-computed Sharpe, stagnation, return/DD on tiny
   equity curves.
6. **Reproducibility test:** same seed/config/data -> identical accepted set and
   hashes across two runs.
7. **Integration:** import a truncated sample, run 100 strategies end-to-end,
   assert funnel counts and deterministic winners.
8. **UI smoke:** start run, see progress, inspect a strategy, cancel, filter.

A strategy is only trusted after the synthetic tests prove the fill logic.
Comparisons against MetaTrader or StrategyQuant itself are noted as a later
optional check; this project will not depend on them.

---

## 16. Work packages and milestones

| # | Package | Output | Est. |
|---|---|---|---|
| M0 | Scaffold | repo, uv env, deps, Typer CLI, Makefile, lint/test setup, CI | 1-2 d |
| M1 | Data layer | ingest, validation, quality report, Parquet store, resampler, CLI + API | 3-4 d |
| M2 | Indicator engine | catalogue, ~30 indicators, cache, tests vs TA-Lib | 5-7 d |
| M3 | Strategy DSL + sampler | Pydantic DSL, sampler with guardrails, canonical hash, structural dedupe | 4-5 d |
| M4 | Backtest + metrics | M1 execution engine, sizing, costs, sessions, IS/OOS, metrics, golden tests | 7-10 d |
| M5 | Orchestration | queue, supervisor/workers, progress/SSE, pause/cancel/resume, persistence | 3-4 d |
| M6 | Web UI | all 9 pages/components, API integration, charts, preflight | 8-10 d |
| M7 | Integration + hardening | end-to-end run, performance tuning, docs, example configs, retention | 4-5 d |

Total ≈ 5-7 weeks of focused work; M6 can start in parallel once M1's API is
stable. The critical path is M2 -> M4 (indicator semantics and fill logic).

Suggested first end-to-end target: **H1 signals on USDJPY, 20% OOS, 200 accepted
strategies** with a preflight estimate and a results page. Later configs can
move to M5/M15 and multiple symbols.

### Definition of done for Phase 1

- [ ] Sample CSV imported from the UI; 4,392,425 bars visible with quality report.
- [ ] M1 and higher TFs materialized and selectable.
- [ ] Indicator catalogue visible; every indicator toggleable with param ranges.
- [ ] All generation settings from the brief are configurable and persisted.
- [ ] Run produces reproducible IS/OOS accepted strategies with funnel reasons.
- [ ] Autocorrelation filter works (structural + return correlation) and is
      configurable.
- [ ] Results table filters/sorts; strategy detail shows rules, equity, DD,
      trades, IS/OOS comparison.
- [ ] Cancel/pause/resume verified on a long run.
- [ ] CLI parity for import/resample/run/export.
- [ ] Golden tests and look-ahead tests pass.

---

## 17. Phase 2 and 3 preview (design now, build later)

**Phase 2 — robustness and promotion.** Parameter perturbation (±10/20/25%),
walk-forward windows, Monte Carlo trade resampling, spread/slippage stress,
bootstrap confidence intervals, cross-timeframe re-test, alternate-symbol
validation, regime splits, incubation/forward reserve. Promotion state machine:
`generated -> is_oos_pass -> robust_pass -> portfolio_candidate`. Thresholds
reuse the Phase 1 acceptance editor.

**Phase 3 — portfolio.** Filter robust survivors, compute OOS return
correlation, hierarchical clustering, select with per-symbol and per-style caps,
weight via equal weight / inverse vol / risk parity, backtest the combined
portfolio with shared margin and one-position-per-symbol limits, report
portfolio equity, DD, Sharpe, exposure and allocation drift.

Phase 1 must therefore store stable strategy ids, immutable specs, OOS daily
returns and promotion status fields from day one.

---

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Overfitting / data snooping | OOS gates, autocorrelation filter, purge at split, funnel reporting, no parameter optimization on OOS |
| Combinatorial explosion | seeded randomized sampling, discrete grids, preflight cardinality, target count |
| Indicator semantics mismatch | TA-Lib reference tests + canonical registry; custom indicators documented |
| Look-ahead bias | signals on closed bars, next-bar fills, property tests, embargo |
| M1 backtest too slow | numba loops (nogil), indicator caching, 3-thread pool, benchmark gate |
| DuckDB write contention | single orchestrator writer; workers use Parquet |
| 4 cores / 7 GB RAM | max 2-3 workers, sequential symbols, float32 caches, bounded acceptance set |
| Timezone/session ambiguity | store UTC, configurable session clock, show assumption in UI, validate sample |
| TA-Lib installation | wheels first, pure-numpy fallback implementations |
| Results/disk growth | metrics-only rejects, daily equity only for accepted, retention settings |
| Ambiguous exit interactions | documented precedence + golden tests + conservative SL-first default |

---

## 19. Open decisions (defaults proposed)

1. **Scope split** — Phase 1 stops at IS/OOS acceptance. Robustness and
   portfolio follow in Phases 2-3. (Default: yes.)
2. **Stack** — FastAPI + React. (Default: yes.) A Streamlit-only UI would be
   faster to build but much weaker for long-running jobs and result exploration.
3. **Session clock** — data appears to be UTC; trading window hours are
   interpreted in UTC unless you tell me your broker's server offset. (Default:
   UTC.)
4. **Sizing** — default **fixed lot 0.10** (risk % optional), USD account, 30x
   leverage cap, 0.3 pip USDJPY spread, 0 commission. (Default: these values,
   all editable.)
5. **OOS** — last 40% default; 10/20/30/40/50 slider plus numeric field. (Default: 40%.)
6. **Autocorrelation** — on by default, Pearson daily-return correlation
   threshold 0.95, structural dedupe always on. (Default: yes.)
7. **Target run size** — target 200 accepted; the run continues until reached or
   stopped by the user (no max attempt cap).
8. **Indicator set** — 47 indicators shipped in Phase 1; add more on demand.

If these defaults are acceptable, implementation starts with M0-M1 (scaffold,
data import, resampling, quality report) and a clickable data page, then proceeds
down the milestone list.


---

## 20. Phase 1 implementation status

Implemented and verified end to end:

- Data: CSV import, quality report, M1 normalization and M5–D1 resampling.
- Indicators: 47 indicators with vectorized compute and parameter ranges.
- Generator: seeded sampler over direction, entry/exit conditions, SL/TP,
  trailing, cooldown, max bars and opposite-signal policy.
- Engine: M1 open fills, intrabar SL-before-TP, ATR/fixed distances, session
  close, purge at the IS/OOS boundary, IS and OOS metrics and gates.
- Search loop: 3-thread pool, structural hash de-duplication, optional Pearson
  or Spearman IS daily-return correlation, exact accepted target, cooperative
  stop, funnel counters.
- Persistence: DuckDB runs/attempts/strategies plus per-strategy spec, daily
  equity and trades; orphaned runs marked `interrupted` on restart.
- API and UI: FastAPI + SSE; React pages for Data, Indicators, Generator,
  Acceptance, Run monitor and Results with equity/trade inspection.
- Tests: 64 pytest checks covering indicators, sampling, resampling, engine,
  gates and store JSON handling.

Measured on the bundled USDJPY M1 file (4.39M bars, 4 logical cores):

- H4 attempt cost ~0.27 s at 3 threads; run throughput ~2.4 attempts/s.
- Default gates accepted ~8% of sampled strategies, so a 200-accepted demo
  needs roughly 17–20 minutes and can be stopped at any time.
