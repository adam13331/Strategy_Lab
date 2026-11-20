# SQX Strategy Lab

A local, self-hosted strategy research lab in the spirit of StrategyQuant X.
It ingests broker M1 CSVs, generates rule-based strategies from a configurable
indicator pool, backtests every fill on the original 1-minute bars, rejects
strategies that fail in-sample/out-of-sample gates, and removes statistical
duplicates before a strategy becomes a result.

Phase 1 scope: single instrument, single-process search, time-ordered IS/OOS
split, structural + return-correlation de-duplication, and a web UI.

## Quick start

Place broker CSVs in `data/raw/` (the UI scans this folder and detects the
symbol from the filename, e.g. `AUDJPY-M1-No Session.csv` → `AUDJPY`).

```bash
uv sync                       # backend
cd web && npm install && npm run build && cd ..

uv run sqx data import "USDJPY-M1-No Session.csv"
uv run sqx data resample USDJPY --timeframes M5,M15,M30,H1,H4,D1

uv run sqx serve              # http://127.0.0.1:8000
```

For UI development run `uv run sqx serve` and `npm run dev` in `web/`
(Vite proxies `/api` to port 8000).

## What a run does

1. Samples a strategy: direction, 1–N entry conditions per side, optional
   indicator exits, opposite-signal policy, SL/TP policy (fixed pips or ATR),
   optional ATR trailing, max bars in trade and cooldown.
2. Turns each entry signal into a fill. Market entries fill at the next M1
   open; limit and stop entries are placed as pending orders at a sampled
   fixed-pip or ATR distance and expire after a sampled number of bars. Several
   pending orders can be placed for one signal (OCO: the first fill cancels the
   rest).
3. Computes indicator signals on the chosen timeframe, maps them to M1 and
   fills at the next M1 open or when a pending order triggers.
4. Walks M1 highs/lows for SL/TP/trailing. Stop loss is evaluated before take
   profit inside one bar (conservative).
5. Splits each instrument's history by time (default 40% OOS, purge bar before
   the boundary) and applies the IS and OOS gates independently.
6. Rejects exact structural duplicates, then rejects strategies whose IS daily
   returns correlate at/above the threshold with an already accepted strategy.
7. Persists the accepted strategy, its metrics, daily equity and trades.

Higher-timeframe price-action indicators (`iHigh`, `iLow`, `iDailyHigh`,
`iDailyLow`, `iMonthlyHigh`, `iMonthlyLow`) group the base bars into the
requested period. Prior periods use their completed high/low; the active
period contributes only its running extreme up to the current base bar, so the
alignment is causal and introduces no lookahead.

The loop has no attempt cap: it runs until the accepted target is reached or
you stop it.

## Defaults

| Setting | Default |
| --- | --- |
| Timeframe | H4 signals, M1 execution |
| Direction | both |
| Sizing | fixed lot 0.10 (or risk % per trade) |
| Balance / leverage | 10,000 / 30× |
| Spread / slippage | 0.3 pips round-turn / 0 |
| Entry orders | market, limit, stop (up to 2 pending per signal) |
| Session | Mon–Fri, 00–23 UTC, close at end |
| OOS share | 40% |
| Accepted target | 200 |
| Structural de-dupe | always |
| Return correlation | Pearson ≥ 0.95 (toggle + threshold) |
| IS gates | ≥20 trades, Sharpe ≥0.1, return/DD ≥0.2, stagnation ≤1500 d |
| OOS gates | ≥10 trades, Sharpe ≥0.05, return/DD ≥0.1, stagnation ≤1700 d |

All of these are editable in the UI. `configs/default.json` is a complete
example config accepted by `sqx run`.

## CLI

```bash
uv run sqx data import <csv> [--symbol SYM]
uv run sqx data resample <symbol> --timeframes M5,H1,H4
uv run sqx data list
uv run sqx defaults configs/default.json
uv run sqx run configs/default.json
uv run sqx stress <run_id> [--parameter-rounds 30] [--shuffle-rounds 2000] [--mc-rounds 500]
uv run sqx serve [--host 127.0.0.1] [--port 8000] [--reload]
```

## Sequential stress pipeline

Accepted strategies can be promoted through a five-stage robustness pipeline.
Stages run in order and the first failure rejects the strategy:

1. **Parameter sensitivity** (15–50 rounds): perturb indicator, ATR, fixed-pip
   and trade-length parameters by ±10–20%; require the median Sharpe and profit
   factor to drop by no more than the configured tolerance and at least 80% of
   variants to stay profitable.
2. **Price & cost sensitivity** (1–50 rounds): add 0.05–0.1% Gaussian noise to
   H/L/C bars (Sharpe drop ≤25%) and re-run under 2–3× baseline slippage and
   commission (must remain profitable).
3. **Trade shuffling** (1,000–5,000 rounds): permute the trade return vector;
   require the 95th/99th percentile max drawdown to stay within 1.5–2× the
   historical backtest drawdown.
4. **Monte Carlo price permutation** (500–1,000 rounds): block-bootstrap or
   GARCH(1,1) synthetic M1 paths; require 70–80% of paths to be profitable.
5. **Cross-market validation**: run the unchanged rules on alternate imported
   instruments; require an average profit factor above one.

The defaults are editable in the **Stress test** UI page, through the
`/api/stress` endpoints, or with `sqx stress`. Survivors are written to a new
strategy bank.

## HTTP API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Liveness and indicator count |
| `GET /api/symbols` | Imported instruments, quality and bar caches |
| `GET /api/data/quality?symbol=` | One instrument's quality report |
| `GET /api/data/files` | CSV files in `data/raw` with detected symbols |
| `POST /api/data/import` | `{path, symbol?}` |
| `POST /api/data/resample` | `{symbol, timeframes[]}` |
| `GET /api/indicators` | Catalogue and default per-indicator ranges |
| `GET /api/config/default` | Full default `GenerateConfig` |
| `POST /api/preflight` | Validate a config before starting |
| `POST /api/runs` | `{config, name?}` → run id |
| `GET /api/runs` / `GET /api/runs/{id}` | List / snapshot |
| `POST /api/runs/{id}/stop` | Cooperative stop |
| `GET /api/runs/{id}/events` | SSE progress stream |
| `GET /api/strategies?run_id=&limit=&offset=` | Accepted strategies |
| `GET /api/strategies/{id}` | Spec, rules, IS/OOS metrics |
| `GET /api/strategies/{id}/equity` | Daily equity with IS/OOS segment |
| `GET /api/strategies/{id}/trades` | Trade list |
| `GET /api/stress/defaults?source_run_id=` | Default five-stage pipeline config |
| `POST /api/stress` | Start a sequential stress run |
| `GET /api/stress` / `GET /api/stress/{id}` | List / snapshot |
| `POST /api/stress/{id}/stop` | Cooperative stop |
| `GET /api/stress/{id}/events` | SSE progress stream |

The built UI is served from `web/dist` at `/`.

## Project layout

```
src/sqx/
  settings.py     paths, timeframe map, pip helpers
  store.py        DuckDB schema and read/write helpers (single writer)
  data.py         CSV import, quality report, resampling
  indicators.py   53-indicator catalogue and vectorized compute
  strategy.py     Pydantic config, sampler, structural hash, rule text
  engine.py       context loading, M1 simulation (numba), metrics, gates
  runner.py       background run loop, funnel, de-duplication, persistence
  stress.py       sequential five-stage robustness pipeline
  stress_runner.py background orchestration and survivor banking
  api.py          FastAPI app and SSE
  cli.py          Typer commands
web/              React + Vite UI
tests/            pytest suite
data/
  raw/            source CSVs (git-ignored)
  parquet/        normalized M1 + resampled bars
  results/        equity/trades per accepted strategy
  sqx.duckdb      metadata, runs, attempts, strategies
```

## Data format

CSV with header `Date,Time,Open,High,Low,Close,Volume`, e.g.
`20150101,220000,119.800,119.810,119.795,119.805,12`. Dates are treated as
UTC; the expected trading week opens Sunday 22:00 and closes Friday 22:00.

## Notes and limits

- The engine is single-process with a bounded thread pool; accepted throughput
  depends on timeframe and indicator cost (~2 attempts/s on a 4-thread laptop
  at H4).
- Correlation filtering uses IS daily returns only; OOS is never used for
  selection.
- Older result files without a `segment` column fall back to deriving the split
  from the stored run config.
- The sequential five-stage stress pipeline is implemented; portfolio
  construction and walk-forward optimisation remain Phase 3 work.

## Tests

```bash
uv run pytest -q
```

DuckDB is opened in single-writer mode, so stop `sqx serve` before running the
tests or a second CLI process against the same database.
