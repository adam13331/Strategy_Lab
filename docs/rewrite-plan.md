# SQX Major Rewrite Plan — High-Performance Strategy Engine

Goal: move the hot path from a Python/Numba loop to a compiled, parallel core
(Rust + PyO3) while keeping FastAPI + React + DuckDB/Parquet. M1 stays reserved
for robustness; initial generation runs on the selected timeframe (already done).

## 0. Baseline & guardrails
- Capture current throughput and correctness baseline: 108 tests, ~0.34s/attempt on H4.
- Add a golden-strategy fixture set (spec + expected IS/OOS metrics) used to
  validate every ported kernel.
- Add a benchmark harness (`scripts/bench.py`) reporting attempts/s and
  per-stage timings; run in CI.

## 1. Architecture target
```
web (React) -> FastAPI (Python) -> sqx_core (Rust via PyO3)
                                      |- context (Arrow arrays)
                                      |- indicators (Rust, cached)
                                      |- simulate (Rayon parallel)
                                      |- gates / dedup (Rust)
DuckDB + Parquet (unchanged storage)
```
- Python remains orchestration, config, API, persistence.
- Rust owns CPU-bound work: indicator compute, signal building, simulation,
  gating, correlation.
- Data crosses the boundary as NumPy/Arrow arrays (zero-copy where possible).

## 2. Phase A — Port the simulation kernel (highest impact)
- Create `rust/sqx_core` crate with PyO3 module `sqx_core`.
- Reimplement `_simulate` in Rust with:
  - the same bar-walk semantics (SL before TP, trailing, pending/OCO orders,
    force close, max bars, cooldown, session already encoded in arrays).
  - `rayon` parallelism at the strategy-batch level, not inside one backtest.
  - early exit hooks: stop the bar walk once a gate is provably failed
    (e.g. trades < min after split, or drawdown already exceeded).
- Expose `simulate(...) -> (final_eq, maxdd, trade_n, trades)` returning
  NumPy arrays.
- Validate against golden strategies (bit-for-bit within float tolerance).
- Replace `_simulate` call in `engine.run_backtest` with `sqx_core.simulate`.

## 3. Phase B — Indicator engine in Rust
- Port the indicator catalogue (`indicators.py`) to Rust, operating on
  `&[f64]` slices / Arrow arrays.
- Keep the Python catalogue as the source of truth for metadata (params,
  outputs) to avoid UI drift; generate Rust dispatch from it.
- Implement a shared LRU cache in Rust keyed by `(indicator_id, params, data ptr/len)`
  instead of per-attempt `IndicatorCache()`.
- Port `_eval_condition`, `build_tf_signal`, `_to_m1` (rename `_to_base`),
  `_atr_m1`.

## 4. Phase C — Context & data loading
- Move `load_context` array construction to Rust, reading Parquet via Arrow.
- Keep DuckDB for metadata; Parquet for bars.
- Pass `Context` to Python as a lightweight handle holding Rust-owned arrays
  exposed as NumPy views.

## 5. Phase D — Runner parallelism & pruning
- Replace `ThreadPoolExecutor(max_workers=3)` with a Rayon pool sized to
  `num_cpus`.
- Batch size = workers * 16; submit batches; collect results.
- Add early pruning in Rust: reject by trade count / drawdown before full
  equity is materialised.
- Keep structural-hash dedup in Python (cheap) or move to Rust set.

## 6. Phase E — Correlation & dedup at scale
- Move `_correlation` to Rust; maintain an incremental correlation matrix
  (or use Welford-style running stats) instead of O(A^2) intersect1d.
- Target: correlation check O(A) per candidate, not O(A^2).

## 7. Phase F — Persistence & I/O  (DONE)
- Attempts already batched; strategy persistence moved to a background queue
  (`persist_q` + worker thread) so the compute pool is not blocked by parquet/DB writes.
- DuckDB writes remain serialized in the persistence worker (single-writer safe).

## 8. Phase G — Optional acceleration  (STUBBED)
- GPU backend left as an optional, feature-gated future (no GPU available in
  default build).
- Distributed mode left as an optional future; worker count is configurable via
  CPU count and `SQX_WORKERS`.

## 9. Migration & compatibility
- Keep Python fallback (`SQX_ENGINE=python`) until Rust passes all golden tests.
- Feature flag in config; default to Rust once parity is proven.
- No DB schema changes; Parquet format unchanged.

## 10. Testing plan
- Unit: Rust kernels vs Python reference on golden fixtures.
- Property-based: random specs, compare metrics within tolerance.
- Performance: benchmark suite; require >= 5x throughput before switching default.
- Regression: full pytest suite (108+) must pass.

## 11. Risks
- Floating-point divergence between Numba/Python and Rust (mitigate with
  tolerance-based golden tests).
- PyO3 build complexity on user machines (ship prebuilt wheels).
- Threading + DuckDB single-writer (keep all DB writes in Python thread).

## Deliverables
- `rust/sqx_core` crate + Python wheel
- Updated `engine.py` to call Rust with Python fallback
- Benchmark report showing throughput improvement
- Documentation update in README
