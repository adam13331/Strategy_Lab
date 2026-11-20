# MAP

Where things live. Read this first, then `README.md` for behaviour and defaults,
then the one module you actually need.

- Self-hosted strategy lab: FastAPI + DuckDB backend, React UI, M1-level backtests.
- Hot path: `strategy.sample_strategy` → `engine.build_tf_signal` → `engine._simulate`
  → `engine.segment_metrics` → `runner.RunManager`.
- `rust/sqx-gen/` is a benchmark Rust reimplementation of that hot path. It is not
  wired into the app; see `bench/RUST-PORT-REPORT.md`.

## Top level

| Path | What it is |
| --- | --- |
| `src/sqx/` | the whole backend (11 modules, ~6.0k lines) |
| `web/src/` | React + Vite UI (~3.2k lines); `web/dist/` is the build the API serves |
| `tests/` | pytest suite, 99 tests |
| `configs/default.json` | a complete `GenerateConfig`, accepted by `sqx run` |
| `data/` | `raw/` CSVs, `parquet/` normalised bars, `results/`, `sqx.duckdb` (all git-ignored) |
| `bench/` | Python speedtest, Rust port plan + report, record/parity/bench harness |
| `rust/sqx-gen/` | the Rust engine (`cargo build --release`) |
| `docs/PHASE1_PLAN.md` | original design notes |

## Backend modules

| File | Lines | Owns | Start reading at |
| --- | ---: | --- | --- |
| `settings.py` | 64 | paths, `TIMEFRAMES`, `TF_SECONDS`, `TF_PANDAS` (resample rules) | top |
| `store.py` | 518 | DuckDB schema and every read/write; `store` singleton | `_SCHEMA`, `class Store` |
| `data.py` | 243 | CSV import (DuckDB `read_csv`), quality report, resampling to parquet | `import_csv`, `resample` |
| `indicators.py` | 940 | `Param`/`Output`/`Indicator` types, 53 implementations, catalogue | `INDICATORS` (681), `compute` (919), `catalogue` (915) |
| `strategy.py` | 443 | `GenerateConfig` (pydantic), sampler, `spec_hash`, rule text | `sample_strategy` (304), `_condition`, `_resolve_policy` |
| `engine.py` | 808 | `load_context`, signal building, numba `_simulate`, metrics, gates | `load_context` (55), `_simulate` (213), `run_backtest` (667) |
| `runner.py` | 356 | generation loop, thread pool, funnel, de-dupe, persistence | `RunManager._run` |
| `stress.py` | 918 | five-stage sequential pipeline (params, price/cost, shuffle, MC, cross-market) | `run_pipeline` (753) |
| `stress_runner.py` | 335 | orchestration + banking for the stress pipeline | `StressManager._run` |
| `api.py` | 508 | FastAPI routes, SSE streams, static SPA serving | endpoint table below |
| `cli.py` | 122 | Typer commands: `data`, `run`, `stress`, `defaults`, `serve` | top |

## The generation loop

```
configs/*.json ── GenerateConfig ──┐
                                   ▼
data/parquet/<SYM>/{m1,m30,...}.parquet
        │
        └─ engine.load_context ──► Context (M1 arrays + TF frame + masks + split)
                                   │
   runner.RunManager._run:         │  per attempt, in a 2–3 thread pool
     strategy.sample_strategy ─────┤
     strategy.spec_hash ──► seen_hashes (structural de-dupe)
                                   ▼
                        engine.run_backtest
                          build_tf_signal  (indicator → bool mask)
                          _to_m1           (mask on the M1 grid)
                          _atr_m1          (ATR stop distances per M1 bar)
                          _simulate        (fills, SL/TP/trailing, equity)
                          segment_metrics  (IS / OOS / all)
                                   ▼
                     engine.passes  ──► rejected (funnel counter)
                                   ▼
              runner._correlation  ──► rejected if Pearson ≥ threshold (IS only)
                                   ▼
        store.insert_strategy + data/results/<run_id>/<sid>_{equity,trades,spec}
```

`Context` holds ~4.4M-bar numpy arrays per symbol (~200 MB per instrument); both
`runner` and `stress_runner` cache one per symbol for the life of the job.

## Strategy spec (the JSON that everything consumes)

```jsonc
{
  "schema": 1, "symbol": "USDJPY", "timeframe": "M30", "direction": "both",
  "entry":   { "long": [cond...], "short": [cond...] },
  "execution": {                      // orders, not "orders" in GenerateConfig
    "orders": [{"type": "market"} | {"type": "limit", "distance_type": "atr",
               "atr_period": 14, "atr_mult": 0.5}],
    "expiry_bars": 0, "oco": true
  },
  "exit": {
    "conditions": [cond...],          // shared by both sides
    "opposite_signal": "ignore|reverse",
    "stop_loss":   {"mode": "stop_loss", "type": "fixed", "pips": 40}
                 | {"mode": "stop_loss", "type": "atr", "atr_period": 14, "atr_mult": 2.5}
                 | {"mode": "none"},
    "take_profit":  same shape,
    "trailing":    {"mode": "none"} | {"mode": "use", "atr_period": .., "atr_mult": ..},
    "max_bars_in_trade": 240, "cooldown_bars": 0, "close_at_session_end": true
  },
  "session": {...}, "risk": {...}, "meta": {...}
}
cond = {"indicator": "rsi", "output": "rsi", "operator": "gt|lt|cross_above|cross_below|is_true",
        "params": {"period": 14}, "operand": {"kind": "level|price|none", "value": 50}}
```

## Fill simulation cheat-sheet (`engine._simulate`)

| Trade `reason` | Meaning |
| ---: | --- |
| 1 | stop loss |
| 2 | take profit |
| 3 | trailing stop |
| 4 | indicator exit condition |
| 5 | opposite signal (reverse mode) |
| 6 | session force-close |
| 7 | `max_bars_in_trade` |
| 8 | IS/OOS split bar |
| 9 | end of data |
| 10 | forced random close (robustness scenarios) |

Order of checks in one M1 bar: pending-order triggers → entry → SL → TP → trailing
(stop before target: conservative) → mark-to-market into `daily_eq`.
Costs: `half_spread + slippage` per side, commission per lot charged on entry and exit.

## Indicators in detail

- `compute(indicator_id, df, params, ts=None)` coerces params to the declared types
  and dispatches through the `COMPUTE` table; it always returns the *dict* of outputs.
- `ts` (epoch seconds) is required by the six price-action ids (`iHigh`, `iLow`,
  `iDailyHigh`, `iDailyLow`, `iMonthlyHigh`, `iMonthlyLow`, catalogue lines 873–899):
  they group bars into D1/MN1 buckets in `_ts_values` (533). `engine.run_backtest`
  passes `ctx.tf_ts`. Without it, `_ts_values` falls back to treating each bar as one
  day apart (`ts` column, else `arange * 86400`), so a bare
  `compute("iDailyHigh", df, {})` returns wrong buckets rather than failing.
- Rolling helpers at the top of the file (`_ema`, `_wma`, `_atr`, `_ratio`) are shared;
  changing one moves many indicators, so re-record the Rust goldens if you touch them.

## Adding an indicator

1. `indicators.py`: write `_name(df, **params) -> dict[str, Series]` and add an
   `Indicator` entry to the catalogue (`Param`, `Output` with `kind` = price / level / bool).
2. Nothing else is needed for generation: the sampler reads `INDICATORS`, the UI reads
   `/api/indicators`, and `configs/default.json` is only a saved example.
3. Keep outputs catalogue-ordered. `compute()` returns the dict the backtester indexes
   by `output` key, and order-dependent bugs here are invisible until a trade flips.
4. Mirror it in `rust/sqx-gen/src/indicators.rs` only if you want the Rust gate to stay green
   (`bench/rust/export_cases.py` re-records the goldens).

## HTTP API → code

| Endpoint | Handler | Touches |
| --- | --- | --- |
| `GET /api/health` | `health` | indicator count |
| `GET /api/symbols`, `/api/data/quality` | `symbols`, `quality` | `store.symbols`, `bar_caches` |
| `GET /api/data/files`, `POST /api/data/import`, `/resample` | `data_files`, `data_import`, `data_resample` | `data.py` |
| `GET /api/indicators`, `/api/config/default` | `indicators`, `config_default` | `indicators.catalogue`, `strategy.default_config` |
| `POST /api/preflight` | `preflight` | validation + rough duration estimate |
| `POST /api/runs`, `GET /api/runs[/{id}]`, `/{id}/stop`, `/{id}/events` | `create_run`, `list_runs`, `get_run`, `stop_run`, `run_events` | `runner.manager`, SSE |
| `GET /api/strategies[/{id}][/equity|/trades]` | `strategies`, `strategy`, `strategy_equity`, `strategy_trades` | `store` + `data/results` parquet |
| `/api/stress*` | same five shapes | `stress_runner.stress_manager` |
| `/api/banks*` | `banks`, `bank`, `bank_strategy`, `bank_equity`, `bank_trades` | `store` + `data/results/banks` |
| `GET /` and `/{path}` | `index`, `spa` | `web/dist` (only mounted if the build exists) |

## Storage

| Table | Written by | Read by |
| --- | --- | --- |
| `symbols`, `bar_cache` | `data.import_csv`, `data.resample` | data/quality endpoints |
| `runs` | `runner` (create + stats every batch) | run list/detail |
| `strategies` | `runner._persist_strategy` | results, stress |
| `attempts` | `runner` (buffered, 25 rows) | funnel drill-down |
| `stress_runs` / `stress_results` | `stress_runner` | stress page |
| `banks` / `bank_strategies` | `stress_runner` | banks page |

Per-strategy artefacts: `data/results/<run_id>/<sid>_equity.parquet` (with `segment`),
`_trades.parquet`, `_spec.json`; survivors are copied to `data/results/banks/<bank_id>/`.

## Web UI

`web/src/App.tsx` holds the nav (no router): Overview, Data, Generation,
Stress test, Strategy Banks, Settings →
`web/src/pages/*.tsx`. `api.ts` is the fetch wrapper, `types.ts` mirrors the pydantic
models, `components.tsx` the shared widgets. `npm run dev` proxies `/api` to :8000.

## Benchmarks and the Rust port

| Path | Purpose |
| --- | --- |
| `bench/speedtest.py` | Python-only timing of loading and generation |
| `bench/RUST-PORT-PLAN.md` | the port plan and gates |
| `bench/RUST-PORT-REPORT.md` | measured result (≈10× per attempt, parity numbers) |
| `bench/rust/export_cases.py` | records context, catalogue, specs, goldens from Python |
| `bench/rust/parity.py` | diffs the Rust engine against the recording (the gate) |
| `bench/rust/run_bench.py` | Python-vs-Rust timing, writes the report |
| `bench/rust/cases/` | the 674 MB recording (git-ignored, regenerate with export_cases) |
| `rust/sqx-gen/src/` | `ctx`, `catalogue`, `rng`, `hash`, `sample`, `indicators`, `signals`, `kernel`, `metrics`, `main` |

## Tests

| File | Covers |
| --- | --- |
| `test_engine.py` | context shapes, backtest consistency, gates, limit/stop/OCO/expiry fills, ATR orders |
| `test_indicators.py` | every indicator computes, RSI bounds, band ordering, catalogue vs defaults |
| `test_resample.py` | OHLCV aggregation, trailing bucket drop |
| `test_strategy.py` | defaults, OOS validation, seeded sampling |
| `test_stress.py` | pipeline records, stage merge, parameter jitter, percentile summary |
| `test_store.py` | JSON columns, bank and stress round-trips |

## Gotchas

- **DuckDB is opened read-write at import.** `store.py` runs DDL *and* marks stale
  `queued/running` rows as `interrupted` in `Store.__init__`, so importing `sqx.store`
  takes the exclusive lock. While `sqx serve` runs, no other process (CLI, pytest,
  script) can open the database. Redirect it instead:
  ```bash
  uv run python -c "import pathlib,tempfile,sqx.settings as s; \
    s.DB_PATH=pathlib.Path(tempfile.mkdtemp())/'t.duckdb'; \
    import pytest; raise SystemExit(pytest.main(['-q']))"
  ```
- `_simulate` is `@njit(cache=True)`: the first attempt in a fresh process pays
  ~1 s of compilation. Never time a single attempt in a cold process.
- `data/raw`, `data/parquet`, `data/results`, `data/sqx.duckdb*`, `rust/sqx-gen/target`
  and `bench/rust/cases` are git-ignored; `web/dist` is a build artefact.
- The API mounts `web/dist` only if it exists; rebuild with `cd web && npm run build`.
- Run commands: `uv run sqx serve`, `uv run sqx run configs/default.json`,
  `uv run sqx stress <run_id>`, `cd rust/sqx-gen && cargo build --release`.
