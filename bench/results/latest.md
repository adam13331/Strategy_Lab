# SQX generation-engine speedtest and Rust projection

Generated 2026-11-19T11:04:18+00:00 on Linux x86_64: 4 logical cores, Python 3.12.3, numba 0.68.1, numpy 2.5.5, duckdb 2.0.0.

Workload: `USDJPY` M30 signals over 4,392,425 M1 bars / 146,979 signal bars (11.79 years, 3,687 days). 24 sampled strategies per timing pass, 40 attempts in the generation loop, 2 reps.

## Generation-engine measurements

| Phase | Measured |
| --- | --- |
| Context load (M1 + M30 parquet, arrays, session masks) | 2822 ms |
| Sample strategy + spec hash | 0.056 ms (17852/s) |
| One attempt, cold indicator cache | 1281.9 ms |
| One attempt, shared indicator cache | 369.9 ms |
| First numba `_simulate` call (JIT compile) | 959 ms |
| Persistence batch of 25 | 1347 ms insert + 45 ms run update (53.9 ms/attempt amortized) |
| Generation loop (backtest + gates) | 0.72 attempts/s, accept rate 5.0% |
| Throughput, serial | 0.80 attempts/s |
| Throughput, 3-thread pool | 0.77 attempts/s (0.96× scaling) |

## Cost of one attempt

Indicator/ATR computations per attempt (sampled rules + policies): 6.8; the shared-cache pass keeps 138 computed arrays.

| Component | ms | Share |
| --- | --- | --- |
| Sampling + spec hash | 0.1 | 0% |
| Signal prep (indicator compute, ATR maps, M1 mapping) | 650.8 | 49% |
| M1 simulation kernel (numba, nogil) | 50.0 | 4% |
| Metrics, trade packing, timestamps | 581.1 | 44% |
| Persistence | 53.9 | 4% |
| **Total per attempt** | **1335.8** | 100% |

Reject reasons in the measured funnel: is:min_sharpe 22, is:min_trades 8, oos:min_sharpe 5, is:stagnation 3, accepted 2.

## If the engine were Rust

Assumed per-component speedups (low/mid/high) are in the appendix; each scenario rewrites only its listed components and leaves the rest at the measured cost. Wall clock is for 200 accepted strategies at this run's 5.0% accept rate (4000 attempts), measured on only 40 attempts, so the accept rate itself carries small-sample uncertainty.

Measured today: 0.80 attempts/s serial, 0.77 attempts/s on 3 threads (0.96× scaling — the loop is GIL-bound in Python). The Rust pool columns assume 2.7× scaling without the GIL.

### A. hot path only (signals + metrics in Rust)

| Case | ms/attempt | Speedup | attempts/s | Rust pool attempts/s | 200 accepted, serial | 200 accepted, pooled |
| --- | --- | --- | --- | --- | --- | --- |
| low | 558 | 2.39× | 1.79 | 4.84 | 37.2 min | 13.8 min |
| mid | 331 | 4.04× | 3.02 | 8.16 | 22.1 min | 8.2 min |
| high | 217 | 6.14× | 4.60 | 12.42 | 14.5 min | 5.4 min |

Baseline for comparison: 1282 ms/attempt, 0.80 attempts/s serial, 89.1 min for 200 accepted today.

### B. attempt loop in Rust (sample + signals + sim + metrics)

| Case | ms/attempt | Speedup | attempts/s | Rust pool attempts/s | 200 accepted, serial | 200 accepted, pooled |
| --- | --- | --- | --- | --- | --- | --- |
| low | 558 | 2.39× | 1.79 | 4.84 | 37.2 min | 13.8 min |
| mid | 324 | 4.12× | 3.08 | 8.32 | 21.6 min | 8.0 min |
| high | 204 | 6.53× | 4.89 | 13.21 | 13.6 min | 5.0 min |

Baseline for comparison: 1282 ms/attempt, 0.80 attempts/s serial, 89.1 min for 200 accepted today.

### C. attempt loop + persistence in Rust

| Case | ms/attempt | Speedup | attempts/s | Rust pool attempts/s | 200 accepted, serial | 200 accepted, pooled |
| --- | --- | --- | --- | --- | --- | --- |
| low | 558 | 2.39× | 1.79 | 4.84 | 37.2 min | 13.8 min |
| mid | 312 | 4.28× | 3.21 | 8.66 | 20.8 min | 7.7 min |
| high | 178 | 7.53× | 5.63 | 15.21 | 11.8 min | 4.4 min |

Baseline for comparison: 1282 ms/attempt, 0.80 attempts/s serial, 89.1 min for 200 accepted today.

## How to read this

- The simulation kernel is 4% of an attempt and is already compiled by numba. If the Python side became free and the kernel stayed as it is, attempts/s could rise by at most 27.03×; the realistic range is the 2–7× in the tables, because the kernel cost is the floor.

- Today the 3-thread pool buys only 0.96× because signal preparation and metrics hold the GIL; only the numba kernel releases it. Moving the whole attempt into Rust makes the pool the main lever: on these 4 cores a GIL-free pool is worth roughly another 2.7× on top of per-attempt gains.

- rust-anything's 3–20× figures are for glue-heavy work called in batches. A PyO3 call per indicator would burn much of that in boundary crossing; the model assumes one batched call per attempt, as its docs recommend ('find the loop worth moving, and move it').

- The pooled column is an assumption, not a measurement: today the 3-thread pool gains nothing (0.96×), because everything except the numba kernel runs under the GIL. A Rust core would need its own worker pool (rayon or std threads) to reach the assumed 2.7× scaling.

- Excluded: correlation filtering against already-accepted strategies, result-file writes per accepted strategy and the web UI. Correlation cost grows with the accepted list and is small at the sizes measured.

- These are estimates from measured shares, not a Rust build. The verification path is rust-anything on one unit at a time: `record` the engine functions, `bridge` the crate, write the port, `diff` every recorded case, `bench` against this script, then `gate`.

## Appendix: assumed speedups

| Component | Low | Mid | High |
| --- | --- | --- | --- |
| sampling | 3.0× | 8.0× | 15.0× |
| signals | 2.5× | 5.0× | 10.0× |
| simulate | 1.0× | 1.15× | 1.35× |
| overhead | 3.0× | 6.0× | 12.0× |
| persist | 1.0× | 1.3× | 2.0× |
