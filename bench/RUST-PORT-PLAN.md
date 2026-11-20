# Plan: rewrite the SQX generation engine in Rust

Status: executed. See `bench/RUST-PORT-REPORT.md` for measured results, and
`rust/sqx-gen/` for the port. This document is the plan the port followed;
where execution changed it, the change is noted inline.

Result in one line: **1259 ms → 120 ms per attempt (10.5×), 0.79 → 8.3
attempts/s serial, 0.77 → 15.0 attempts/s on 4 threads**, with 221/255 and
51/60 specs bit-identical and the rest differing only where a rule comparison
landed within pandas' own float noise.

Scope: the strategy **generation engine** only —
`sample_strategy` → indicator signals → M1 simulation → metrics → gates.
Excluded: DuckDB persistence, correlation filtering, result files, the API and
web UI, and context loading (parquet → arrays), which happens once per run.

## 1. Baseline the port has to beat

Measured on 2026-11-19 (`bench/results/generation-20261119-120418.json`),
USDJPY M30 signals over 4,392,425 M1 bars / 146,979 signal bars, 4 logical cores:

| Component | ms/attempt | Share |
| --- | ---: | ---: |
| Sampling + spec hash | 0.06 | 0% |
| Signal prep (indicator compute, ATR maps, M1 mapping) | 650.8 | 49% |
| M1 simulation kernel (numba, `nogil`) | 50.0 | 4% |
| Metrics, trade packing, timestamps | 581.1 | 44% |
| **Backtest + gates** | **1281.9** | 100% |

Throughput: 0.80 attempts/s serial, 0.77 attempts/s on a 3-thread pool (0.96×
scaling — the loop is GIL-bound). Persistence adds 53.9 ms/attempt amortized.
Accept rate 5%.

Two conclusions drive the plan:

1. The simulation kernel is **3.7%** of an attempt. Rewriting it alone cannot
   matter; the ceiling with the kernel untouched is 27×.
2. Signal prep and metrics are 93% of an attempt and are pandas/Python glue.
   That is exactly the shape of workload rust-anything reports 3–20× on.

Target: a Rust engine that reproduces the Python results and runs one attempt
in the low hundreds of milliseconds, with a GIL-free thread pool so the pool
scales instead of gaining 0.96×.

## 2. Architecture

A standalone Rust binary, not Python bindings.

```
rust/sqx-gen/
  Cargo.toml
  src/main.rs        CLI: --ctx --catalogue --specs --reps --threads, JSONL out
  src/ctx.rs         reads the context snapshot (raw LE arrays)
  src/catalogue.rs   reads indicator metadata, drives sampling
  src/hash.rs        engine-local structural spec hash (see §6)
  src/rng.rs         deterministic xorshift sampler (not MT19937 — see §6)
  src/sample.rs      sample_strategy port
  src/indicators.rs  the 53 catalogue indicators
  src/signals.rs     condition eval, ATR maps, M1 mapping
  src/kernel.rs      _simulate port
  src/metrics.rs     segment_metrics, _max_dd, passes
bench/rust/
  export_cases.py    dumps context + catalogue + specs + Python goldens
  parity.py          replays Rust results against the goldens
  run_bench.py       Python vs Rust throughput, writes the report
```

Why a binary and not a PyO3 extension: the measured cost is *inside* the
attempt, not at the boundary. One call per attempt (~1,300 ms today) makes
boundary crossing irrelevant, and a binary avoids maturin/abi3 build coupling
and keeps the existing Python install path untouched. If the engine later needs
to be called from `runner.py` per attempt, a PyO3 wrapper around the same core
is the follow-up, not the first step (rust-anything's `bridge` step).

Interchange is one-shot, from Python:

- `context.bin` — the `Context` arrays as raw little-endian data behind an
  `SQXCTX01` header (fixed field order; see `ctx.rs`). Avoids an arrow/parquet
  dependency and guarantees both engines read bit-identical inputs.
- `catalogue.txt` — 53 indicators with param ranges, output kinds and levels,
  exported from `sqx.indicators.INDICATORS` so the Rust sampler cannot drift
  from the Python catalogue.
- `specs.jsonl` — the Python-sampled specs to replay (parity) plus
  Rust-sampled specs written back (round-trip).
- `golden.jsonl` — Python's `run_backtest` output per spec: IS/OOS/all
  metrics, final equity, trade count and per-trade `(entry_i, exit_i, side,
  entry_px, exit_px, pnl, reason)`.

## 3. Phases and gates

Each phase ends in a check that must pass before the next one starts — the
rust-anything `record → port → diff → bench → gate` loop applied per unit.

| Phase | Work | Gate |
| --- | --- | --- |
| 0. Record | `export_cases.py`: context snapshot, catalogue, 300 Python specs, goldens, plus per-indicator golden arrays for all 53 indicators with 3 random param sets each | goldens load; arrays finite where expected |
| 1. Scaffold | crate, ctx reader, catalogue parser, JSONL in/out | reads the snapshot, prints row counts matching Python |
| 2. Indicators | port all 53 | per-indicator max abs diff ≤ 1e-5 (see the note below: 1e-9 is unreachable) |
| 3. Signals | condition eval, `_to_m1`, `_atr_m1`, order-distance maps | signal counts and M1 masks identical to Python per spec |
| 4. Kernel | `_simulate` port; metrics; `passes` | 300-spec replay: trade count exact, `final_equity` rel ≤ 1e-9, per-trade pnl abs ≤ 1e-6, every IS/OOS metric within rounding of Python's 4 dp |
| 5. Sampling | sampler + `spec_hash` | Rust-sampled specs hash to distinct ids, evaluate in Python without error, and produce the same metrics in both engines |
| 6. Bench | 300 specs serial and on 1/2/3/4 threads, cold cache; compare with the Python numbers | report speedup per component and pooled scaling |

Measured after execution: 261 arrays from 159 param sets, worst |diff| 3.5e-7
(`bbands` %B). The original 1e-9 gate is **not attainable**: pandas' own
rolling variance differs from an exact two-pass by up to 5e-9 (measured), so
any port that computes variance correctly disagrees with pandas at that scale.
The port therefore reproduces pandas' Welford add/remove accumulation, which
brought the worst case from 2.7e-6 to 3.5e-7, and the gate sits at 1e-5.

Fidelity rules the port must respect (each is a real divergence risk):

- `ewm(span=n, adjust=False, min_periods=n)` → `y[0]=x[0]`,
  `y[i]=a*x[i]+(1-a)*y[i-1]`, then mask the first `n-1` outputs as NaN.
  `_atr` and the DI lines use `alpha=1/n` instead of `span`, so `a=1/n`.
- `rolling(n)` → NaN for the first `n-1` outputs; `std(ddof=0)`;
  `_ratio` uses `min_periods=max(5, n//2)` on a rolling **median**.
- `pandas` max/min skip NaN (`tr[0] = high-low`, not NaN), but `mean`/`std`/
  `sum` propagate NaN, and `.replace(0, nan)` turns a zero denominator into NaN.
- Signal masks go to M1 with `np.maximum.at` (an OR, idempotent); the
  `entry_ok`, `force_close`, `skip_*` masks are `uint8`.
- Comparison semantics: NaN compares False everywhere; `cross_*` only fires on
  the transition bar and needs both bars finite.
- `segment_metrics` runs its stagnation loop over day-aligned equity and
  divides by `base`/`oos_base`; Sharpe uses `ddof=1` and `sqrt(252)`.

## 4. Risks

| Risk | Mitigation |
| --- | --- |
| Silent numeric drift between pandas and hand-written loops | Phase 2 per-indicator gate with 53×3 recorded cases before any strategy work |
| Sliding sums drifting where a window sum must be exactly zero (`mfi`, `cmf` divide by one) | `roll_sum_exact` recomputes those windows; without it `mfi` reported 100 instead of NaN and 747 signal bars moved |
| Sliding weighted sum drifting on `wma`/`hma` | Reseed from the window every 512 bars (2e-7 → 1e-11) |
| Indicator catalogue drift when Python changes | Rust has no hard-coded ranges; it reads `catalogue.txt` |
| Reed differences in sampling order | RNG parity is out of scope by design; specs are exchanged, not seeds (§6) |
| NaN/short-circuit divergence in the kernel | Port branch order literally, including the `reason` codes |
| Rust toolchain absent on this machine | Installed via `rustup` (user-local, no sudo) into `~/.cargo` |

## 5. Out of scope

Persistence (DuckDB), correlation filtering against accepted strategies,
result parquet writes, context loading from parquet, the API/web layer, and any
change to the Python engine. The Python engine stays the reference
implementation and is untouched by this port.

## 6. Deliberate deviation: sampling RNG

Python's sampler is `random.Random(seed)` (Mersenne Twister). Reproducing MT19937
streams in Rust just to replay seeds is meaningless for throughput: sampling is
0.06 ms/attempt (0.004%), and what has to match is the *distribution*, not the
draw order. The Rust sampler therefore uses its own deterministic PRNG and the
same catalogue-driven ranges, and the parity gate runs Rust-sampled specs
through **both** engines. Seeds stay Python-side for the production runner.
