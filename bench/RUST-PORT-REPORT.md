# SQX generation engine: Rust port report

Generated 2026-11-19T13:20:43+00:00 on Linux x86_64: 4 logical cores, Rust rustc 1.100.0 (0ef1bc386 2026-11-09). Workload: `USDJPY` `M30` signals over 4,392,425 M1 bars / 146,979 signal bars (3,687 days). 60 sampled strategies, fresh indicator cache per attempt, same context snapshot and the same specs in both engines.

## 1. Behavioural gate (rust-anything `diff`)

| Stage | Specs | Bit-identical | Boundary ties | Broken |
| --- | ---: | ---: | ---: | ---: |
| Replay of Python-sampled specs | 255 | 221 (87%) | 34 | 0 |
| Rust-sampled specs through both engines | 60 | 51 (85%) | 9 | 0 |
| Indicator arrays vs pandas (159 param sets) | 261 | 261 | 0 | 0 |

Bit-identical means the same trade list (bars, side, reason, prices), the same signal counts, daily equity within 1e-9 and every IS/OOS/all metric within 5e-4. 'Boundary ties' are specs where one rule comparison landed on the other side of a threshold because pandas' arithmetic is order-dependent: its rolling variance alone differs from an exact two-pass by up to 5e-9, and the port's arrays match pandas to 3.5e-07 (worst, bbands %B). A single flipped 30-bar signal then moves later trades. Worst case in this set: |ΔSharpe| 0.1205, 32 M1 signal bars out of 4,392,425 (gate: <0.01% per spec).

## 2. One attempt

| | Python | Rust | Speedup |
| --- | ---: | ---: | ---: |
| ms per attempt | 1296.7 | 134.5 | 9.64× |
| attempts/s, serial | 0.77 | 7.51 | 9.74× |
| attempts/s, 3 threads | 0.77 | 14.57 | 18.92× |

### Where the time goes inside an attempt

| Component | Python (recorded) | Rust (measured) |
| --- | ---: | ---: |
| Signal prep (indicator compute, ATR maps, M1 mapping) | 650.8 ms | 62.6 ms |
| M1 simulation kernel | 50.0 ms | 57.6 ms |
| Metrics, trade packing, timestamps | 581.1 ms | 0.3 ms |
| Indicator computations per attempt | 6.8 | 7.1 |

The per-component Python column is from `bench/results/generation-20261119-120418.json` (24 specs, cold indicator cache, fresh process) because the Python run above only measures the total. Rust numbers are the best of 3 passes over the same 60 specs, phase-split by the engine itself. Same machine, same context snapshot, fresh indicator cache per attempt on both sides.

## 3. Thread scaling

| Workers | attempts/s | scaling vs serial |
| ---: | ---: | ---: |
| 1 | 7.51 | 1.00× |
| 2 | 11.92 | 1.59× |
| 3 | 14.57 | 1.94× |
| 4 | 14.56 | 1.94× |

Python today: 0.80 attempts/s serial, 0.77 on 3 threads (0.96×) — the GIL removes the pool's value.

## 4. Gates agree

| | Python | Rust |
| --- | ---: | ---: |
| accepted | 5 | 5 |
| accept rate | 8.3% | 8.3% |

Reject reasons, Python: `{"is:min_sharpe": 42, "is:min_trades": 8, "accepted": 5, "oos:min_sharpe": 3, "is:stagnation": 2}`

Reject reasons, Rust: `{"is:min_sharpe": 42, "is:min_trades": 8, "accepted": 5, "oos:min_sharpe": 3, "is:stagnation": 2}`

## 5. What this port does not include

- DuckDB persistence (54 ms/attempt amortized in Python) and correlation filtering against accepted strategies are still Python-side; the runner would call the Rust core per attempt or batch.

- The sampler uses its own PRNG, so spec ids and draw order differ from Python's. The gate therefore exchanges specs, not seeds, and evaluates Rust-sampled specs through both engines.

- Context loading from parquet (2.8 s once per run) is Python-side; the Rust engine reads the exported snapshot.

## 6. Reproduce

```bash
uv run python bench/rust/export_cases.py --specs 300   # record Python behaviour
uv run python bench/rust/parity.py                    # diff the port, run the gate
uv run python bench/rust/run_bench.py --specs 60 --threads 1,2,3,4
```

The crate is `rust/sqx-gen` (`cargo build --release`); `cases/` holds the recorded snapshot and is git-ignored (674 MB).

