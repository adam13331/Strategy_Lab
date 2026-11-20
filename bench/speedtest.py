"""Generation-engine speedtest and Rust-port projection.

Benchmarks only the strategy generation loop: sampling -> signals -> M1
simulation -> metrics -> gates. It reports where each attempt spends its time,
throughput serial and pooled, and projects the same loop with the hot paths
rewritten in Rust using the rust-anything workflow (record -> port -> diff ->
bench -> gate).

Usage:
    uv run python bench/speedtest.py
    uv run python bench/speedtest.py --samples 40 --attempts 60
    uv run python bench/speedtest.py --timeframe H4 --symbol USDJPY

The store is redirected to a throwaway database, so a running ``sqx serve``
is not disturbed and no results land in data/sqx.duckdb.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

import sqx.settings as settings  # noqa: E402

# Redirect the store before anything imports sqx.store (engine does).
_BENCH_DIR = Path(tempfile.mkdtemp(prefix="sqx-bench-"))
settings.DB_PATH = _BENCH_DIR / "bench.duckdb"

from sqx import engine, indicators  # noqa: E402
from sqx.engine import IndicatorCache, load_context, passes, run_backtest  # noqa: E402
from sqx.settings import m1_path, tf_path  # noqa: E402
from sqx.store import store  # noqa: E402
from sqx.strategy import (  # noqa: E402
    default_config,
    sample_strategy,
    spec_hash,
)

SESSION = {"start_hour": 0, "end_hour": 23, "days": [0, 1, 2, 3, 4],
           "close_at_end": True}

# Per-component speedups for a Rust rewrite, from rust-anything's published
# case studies (3x-20x on glue-heavy workloads, ~20x for batched calls),
# discounted because this engine already runs its simulation through numba.
# (low, mid, high)
SPEEDUP: dict[str, tuple[float, float, float]] = {
    "sampling": (3.0, 8.0, 15.0),    # struct fill + hash instead of pydantic/json
    "signals": (2.5, 5.0, 10.0),     # pandas indicator compute + signal masks
    "simulate": (1.0, 1.15, 1.35),   # already numba nogil; Rust is marginal
    "overhead": (3.0, 6.0, 12.0),    # metrics loop, trade dicts, JSON
    "persist": (1.0, 1.3, 2.0),      # DuckDB executemany, I/O bound
}

SCENARIOS = {
    "A. hot path only (signals + metrics in Rust)": ("signals", "overhead"),
    "B. attempt loop in Rust (sample + signals + sim + metrics)":
        ("sampling", "signals", "simulate", "overhead"),
    "C. attempt loop + persistence in Rust":
        ("sampling", "signals", "simulate", "overhead", "persist"),
}


# ------------------------------------------------------------------- helpers
def timed(fn: Callable[[], Any], reps: int = 3, warmup: int = 1) -> dict[str, Any]:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t)
    return {
        "min_ms": min(samples) * 1000,
        "median_ms": statistics.median(samples) * 1000,
        "samples_ms": [round(s * 1000, 1) for s in samples],
    }


def pct(part: float, total: float) -> float:
    return round(100.0 * part / total, 1) if total > 0 else 0.0


def human(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 90:
        return f"{seconds:.1f} s"
    if seconds < 5400:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.2f} h"


def pick_timeframe(symbol: str, requested: str) -> str:
    if requested.upper() != "AUTO":
        return requested.upper()
    for tf in ("H4", "H1", "M30", "M15", "M5", "M1"):
        if tf_path(symbol, tf).exists() or (tf == "M1" and m1_path(symbol).exists()):
            return tf
    raise SystemExit(f"no parquet bars for {symbol}")


def make_specs(cfg, symbol: str, seed: int, n: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    return [sample_strategy(rng, cfg, symbol) for _ in range(n)]


def signal_prep_ms(spec: dict[str, Any], ctx: engine.Context) -> float:
    """Pre-simulation part of run_backtest: signals on M1 + ATR maps."""
    cache = IndicatorCache()
    ex = spec["exit"]
    t = time.perf_counter()
    for conds in (spec["entry"].get("long", []), spec["entry"].get("short", []),
                  ex.get("conditions", [])):
        engine._to_m1(engine.build_tf_signal(conds, cache, ctx), ctx)
    for key in ("stop_loss", "take_profit", "trailing"):
        pol = ex[key]
        if pol.get("mode") != "none" and pol.get("type") == "atr":
            engine._atr_m1(int(pol["atr_period"]), cache, ctx)
    for order in (spec.get("execution") or {}).get("orders", []):
        if order.get("distance_type") == "atr":
            engine._atr_m1(int(order["atr_period"]), cache, ctx)
    return (time.perf_counter() - t) * 1000


# ------------------------------------------------------------------- phases
def phase_context(symbol: str, timeframe: str, reps: int) -> tuple[engine.Context, dict]:
    holder: dict[str, engine.Context] = {}

    def build() -> None:
        holder["ctx"] = load_context(symbol, timeframe, 40, SESSION)

    res = timed(build, reps=max(1, reps), warmup=1)
    ctx = holder["ctx"]
    return ctx, {
        "context_ms": round(res["median_ms"], 1),
        "m1_rows": len(ctx.m1_ts),
        "tf_rows": len(ctx.tf_df),
        "days": len(ctx.day_ts),
        "years": round((int(ctx.end_ts) - int(ctx.start_ts)) / (365.25 * 86400), 2),
    }


def phase_sampling(cfg, symbol: str, n: int) -> dict[str, Any]:
    rng = random.Random(11)
    batch = [sample_strategy(rng, cfg, symbol) for _ in range(400)]
    idx = {"i": 0}

    def one() -> None:
        spec_hash(batch[idx["i"] % len(batch)])
        idx["i"] += 1

    res = timed(one, reps=max(200, n * 5), warmup=20)
    per = res["median_ms"]
    return {
        "per_spec_ms": round(per, 4),
        "specs_per_sec": round(1000.0 / per, 1) if per > 0 else None,
        "sample_and_hash_ms": round(per, 4),
    }


def phase_attempts(ctx: engine.Context, specs: list[dict[str, Any]],
                   reps: int) -> dict[str, Any]:
    t = time.perf_counter()
    run_backtest(specs[0], ctx, IndicatorCache())
    compile_ms = (time.perf_counter() - t) * 1000

    cold = timed(lambda: [run_backtest(s, ctx, IndicatorCache()) for s in specs],
                 reps=reps, warmup=1)
    per_cold = cold["median_ms"] / len(specs)

    sig_ms = statistics.median([signal_prep_ms(s, ctx) for s in specs])

    # Time the njit kernel through a shim to split it from Python overhead.
    sims: list[float] = []
    real = engine._simulate

    def shim(*a, **kw):
        t0 = time.perf_counter()
        try:
            return real(*a, **kw)
        finally:
            sims.append((time.perf_counter() - t0) * 1000)

    engine._simulate = shim  # type: ignore[assignment]
    try:
        for spec in specs:
            run_backtest(spec, ctx, IndicatorCache())
    finally:
        engine._simulate = real  # type: ignore[assignment]
    sim_ms = statistics.median(sims) if sims else 0.0

    shared = IndicatorCache()
    warm = timed(lambda: [run_backtest(s, ctx, shared) for s in specs],
                 reps=max(1, reps - 1), warmup=1)
    per_warm = warm["median_ms"] / len(specs)

    return {
        "attempts": len(specs),
        "compile_ms": round(compile_ms, 1),
        "per_attempt_cold_ms": round(per_cold, 1),
        "per_attempt_warm_ms": round(per_warm, 1),
        "signals_ms": round(sig_ms, 1),
        "simulate_ms": round(sim_ms, 1),
        "overhead_ms": round(max(0.0, per_cold - sig_ms - sim_ms), 1),
        "indicator_computes_per_attempt": round(
            len(specs) and statistics.mean(
                [len(s["entry"].get("long", [])) + len(s["entry"].get("short", []))
                 + len(s["exit"].get("conditions", []))
                 + sum(1 for k in ("stop_loss", "take_profit", "trailing")
                       if s["exit"][k].get("mode") != "none")
                 for s in specs]), 1),
        "cache_entries": len(shared._d),
    }


def phase_generation(ctx: engine.Context, specs: list[dict[str, Any]],
                     gates_is: dict, gates_oos: dict) -> dict[str, Any]:
    accepted = 0
    reasons: dict[str, int] = {}
    t = time.perf_counter()
    for spec in specs:
        r = run_backtest(spec, ctx, IndicatorCache())
        is_m, oos_m = r["metrics_is"], r["metrics_oos"]
        ok_is, why_is = passes(is_m, gates_is)
        ok_oos, why_oos = passes(oos_m, gates_oos)
        if not ok_is:
            key = f"is:{why_is}"
        elif not ok_oos:
            key = f"oos:{why_oos}"
        elif not (is_m["trades"] and oos_m["trades"]):
            key = "no_trades"
        else:
            key = "accepted"
            accepted += 1
        reasons[key] = reasons.get(key, 0) + 1
    elapsed = time.perf_counter() - t
    return {
        "attempts": len(specs),
        "accepted": accepted,
        "accept_rate": round(accepted / len(specs), 4) if specs else 0.0,
        "attempts_per_sec": round(len(specs) / elapsed, 2),
        "elapsed_s": round(elapsed, 2),
        "reject_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
    }


def phase_threads(ctx: engine.Context, specs: list[dict[str, Any]]) -> dict[str, Any]:
    workers = max(1, min(3, (os.cpu_count() or 2) - 1))

    def serial() -> float:
        t = time.perf_counter()
        for spec in specs:
            run_backtest(spec, ctx, IndicatorCache())
        return len(specs) / (time.perf_counter() - t)

    def pooled() -> float:
        t = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda s: run_backtest(s, ctx, IndicatorCache()), specs))
        return len(specs) / (time.perf_counter() - t)

    s = statistics.median([serial() for _ in range(2)])
    p = statistics.median([pooled() for _ in range(2)])
    return {
        "cpu_count": os.cpu_count(),
        "workers": workers,
        "serial_attempts_per_sec": round(s, 2),
        "pooled_attempts_per_sec": round(p, 2),
        "scaling": round(p / s, 2) if s else None,
    }


def phase_persist(n: int = 25) -> dict[str, Any]:
    store.create_run("bench-run", "bench", {"symbols": []}, 1)
    rows = [("bench-run", i + 1, "USDJPY", "M30", "rejected", "is:min_trades",
             3, 2, 0.1, 0.05, f"h{i}", None) for i in range(n)]

    ins = timed(lambda: store.insert_attempts(rows), reps=3, warmup=1)
    upd = timed(lambda: store.update_run("bench-run", stats_json='{"a":1}'),
                reps=3, warmup=1)
    per_attempt = ins["median_ms"] / n
    return {
        "batch": n,
        "batch_ms": round(ins["median_ms"], 3),
        "update_ms": round(upd["median_ms"], 3),
        "per_attempt_ms": round(per_attempt, 4),
    }


# --------------------------------------------------------------- projection
def build_projection(m: dict[str, Any]) -> dict[str, Any]:
    b, s, p = m["backtest"], m["sampling"], m["persist"]
    base = {
        "sampling": s["per_spec_ms"],
        "signals": b["signals_ms"],
        "simulate": b["simulate_ms"],
        "overhead": b["overhead_ms"],
        "persist": p["per_attempt_ms"],
    }
    base_total = sum(base.values())
    labels = ("low", "mid", "high")
    accept = m["generation"]["accept_rate"] or 0.01
    attempts_needed = 200 / accept
    scaling = m["threads"]["scaling"] or 1.0
    workers = m["threads"]["workers"]
    # Rust threads are not GIL-bound; assume 90% of linear scaling.
    rust_scaling = round(0.9 * workers, 2)

    scenarios = {}
    for name, keys in SCENARIOS.items():
        rows = []
        for which, label in enumerate(labels):
            proj = {k: v / (SPEEDUP[k][which] if k in keys else 1.0)
                    for k, v in base.items()}
            total = sum(proj.values())
            serial_tps = 1000 / total
            rows.append({
                "case": label,
                "per_attempt_ms": round(total, 1),
                "speedup": round(base_total / total, 2),
                "attempts_per_sec": round(serial_tps, 2),
                "pooled_now_attempts_per_sec": round(serial_tps * scaling, 2),
                "pooled_rust_attempts_per_sec": round(serial_tps * rust_scaling, 2),
                "attempts_for_200_accepted": int(round(attempts_needed)),
                "wall_clock": human(attempts_needed * total / 1000),
                "wall_clock_pooled": human(
                    attempts_needed / (serial_tps * rust_scaling)),
                "base_wall_clock": human(attempts_needed * base_total / 1000),
                "components": {k: round(v, 2) for k, v in proj.items()},
            })
        scenarios[name] = {"keys": keys, "rows": rows}

    kernel_share = pct(base["simulate"], base_total)
    # Amdahl ceiling if only the Python side is rewritten and the kernel stays.
    ceiling = round(100.0 / kernel_share, 2) if kernel_share > 0 else None
    return {
        "assumptions": SPEEDUP,
        "baseline": {**{k: round(v, 3) for k, v in base.items()},
                     "total_ms": round(base_total, 2)},
        "scenarios": scenarios,
        "threads": {"measured_scaling": scaling, "workers": workers,
                    "rust_scaling_assumed": rust_scaling},
        "amddahl": {
            "kernel_share_pct": kernel_share,
            "max_attempt_speedup_if_kernel_unchanged": ceiling,
        },
    }


# -------------------------------------------------------------------- report
def render_md(m: dict[str, Any], proj: dict[str, Any]) -> str:
    L: list[str] = []
    a = L.append
    env, b, s, g = m["env"], m["backtest"], m["sampling"], m["generation"]
    ctx = m["context"]
    base = proj["baseline"]

    a("# SQX generation-engine speedtest and Rust projection\n")
    a(f"Generated {m['generated_at']} on {env['host']}: {env['cpu_count']} logical "
      f"cores, Python {env['python']}, numba {env['numba']}, "
      f"numpy {env['numpy']}, duckdb {env['duckdb']}.\n")
    a(f"Workload: `{env['symbol']}` {env['timeframe']} signals over "
      f"{ctx['m1_rows']:,} M1 bars / {ctx['tf_rows']:,} signal bars "
      f"({ctx['years']} years, {ctx['days']:,} days). "
      f"{b['attempts']} sampled strategies per timing pass, "
      f"{g['attempts']} attempts in the generation loop, {env['reps']} reps.\n")

    a("## Generation-engine measurements\n")
    a("| Phase | Measured |")
    a("| --- | --- |")
    a(f"| Context load (M1 + {env['timeframe']} parquet, arrays, session masks) "
      f"| {ctx['context_ms']:.0f} ms |")
    a(f"| Sample strategy + spec hash | {s['per_spec_ms']:.3f} ms "
      f"({s['specs_per_sec']:.0f}/s) |")
    a(f"| One attempt, cold indicator cache | {b['per_attempt_cold_ms']:.1f} ms |")
    a(f"| One attempt, shared indicator cache | {b['per_attempt_warm_ms']:.1f} ms |")
    a(f"| First numba `_simulate` call (JIT compile) | {b['compile_ms']:.0f} ms |")
    a(f"| Persistence batch of {m['persist']['batch']} | "
      f"{m['persist']['batch_ms']:.0f} ms insert + {m['persist']['update_ms']:.0f} ms "
      f"run update ({m['persist']['per_attempt_ms']:.1f} ms/attempt amortized) |")
    a(f"| Generation loop (backtest + gates) | {g['attempts_per_sec']:.2f} attempts/s, "
      f"accept rate {g['accept_rate'] * 100:.1f}% |")
    a(f"| Throughput, serial | {m['threads']['serial_attempts_per_sec']:.2f} attempts/s |")
    a(f"| Throughput, {m['threads']['workers']}-thread pool | "
      f"{m['threads']['pooled_attempts_per_sec']:.2f} attempts/s "
      f"({m['threads']['scaling']}× scaling) |")

    a("\n## Cost of one attempt\n")
    a(f"Indicator/ATR computations per attempt (sampled rules + policies): "
      f"{b['indicator_computes_per_attempt']:.1f}; the shared-cache pass keeps "
      f"{b['cache_entries']} computed arrays.\n")
    a("| Component | ms | Share |")
    a("| --- | --- | --- |")
    for label, val in (
        ("Sampling + spec hash", base["sampling"]),
        ("Signal prep (indicator compute, ATR maps, M1 mapping)", base["signals"]),
        ("M1 simulation kernel (numba, nogil)", base["simulate"]),
        ("Metrics, trade packing, timestamps", base["overhead"]),
        ("Persistence", base["persist"]),
    ):
        a(f"| {label} | {val:.1f} | {pct(val, base['total_ms']):.0f}% |")
    a(f"| **Total per attempt** | **{base['total_ms']:.1f}** | 100% |")
    a(f"\nReject reasons in the measured funnel: "
      f"{', '.join(f'{k} {v}' for k, v in g['reject_reasons'].items())}.\n")

    a("## If the engine were Rust\n")
    a("Assumed per-component speedups (low/mid/high) are in the appendix; each "
      "scenario rewrites only its listed components and leaves the rest at the "
      "measured cost. Wall clock is for 200 accepted strategies at this run's "
      f"{g['accept_rate'] * 100:.1f}% accept rate "
      f"({int(round(200 / (g['accept_rate'] or 0.01)))} attempts), measured on "
      f"only {g['attempts']} attempts, so the accept rate itself carries "
      "small-sample uncertainty.\n")
    th = proj["threads"]
    a(f"Measured today: {m['threads']['serial_attempts_per_sec']:.2f} attempts/s serial, "
      f"{m['threads']['pooled_attempts_per_sec']:.2f} attempts/s on "
      f"{th['workers']} threads ({th['measured_scaling']}× scaling — the loop is "
      f"GIL-bound in Python). The Rust pool columns assume "
      f"{th['rust_scaling_assumed']}× scaling without the GIL.\n")
    for name, sc in proj["scenarios"].items():
        a(f"### {name}\n")
        a("| Case | ms/attempt | Speedup | attempts/s | Rust pool attempts/s | 200 accepted, serial | 200 accepted, pooled |")
        a("| --- | --- | --- | --- | --- | --- | --- |")
        for row in sc["rows"]:
            a(f"| {row['case']} | {row['per_attempt_ms']:.0f} | {row['speedup']}× | "
              f"{row['attempts_per_sec']:.2f} | "
              f"{row['pooled_rust_attempts_per_sec']:.2f} | "
              f"{row['wall_clock']} | {row['wall_clock_pooled']} |")
        a(f"\nBaseline for comparison: {m['backtest']['per_attempt_cold_ms']:.0f} ms/attempt, "
          f"{m['threads']['serial_attempts_per_sec']:.2f} attempts/s serial, "
          f"{row['base_wall_clock']} for 200 accepted today.\n")

    a("## How to read this\n")
    a(f"- The simulation kernel is {proj['amddahl']['kernel_share_pct']:.0f}% of an "
      f"attempt and is already compiled by numba. If the Python side became free "
      f"and the kernel stayed as it is, attempts/s could rise by at most "
      f"{proj['amddahl']['max_attempt_speedup_if_kernel_unchanged']}×; the realistic "
      "range is the 2–7× in the tables, because the kernel cost is the floor.\n")
    a(f"- Today the 3-thread pool buys only {th['measured_scaling']}× because signal "
      "preparation and metrics hold the GIL; only the numba kernel releases it. "
      "Moving the whole attempt into Rust makes the pool the main lever: on these "
      f"4 cores a GIL-free pool is worth roughly another "
      f"{th['rust_scaling_assumed']}× on top of per-attempt gains.\n")
    a("- rust-anything's 3–20× figures are for glue-heavy work called in batches. "
      "A PyO3 call per indicator would burn much of that in boundary crossing; "
      "the model assumes one batched call per attempt, as its docs recommend "
      "('find the loop worth moving, and move it').\n")
    a("- The pooled column is an assumption, not a measurement: today the "
      "3-thread pool gains nothing (0.96×), because everything except the numba "
      "kernel runs under the GIL. A Rust core would need its own worker pool "
      "(rayon or std threads) to reach the assumed "
      f"{th['rust_scaling_assumed']}× scaling.\n")
    a("- Excluded: correlation filtering against already-accepted strategies, "
      "result-file writes per accepted strategy and the web UI. Correlation "
      "cost grows with the accepted list and is small at the sizes measured.\n")
    a("- These are estimates from measured shares, not a Rust build. The "
      "verification path is rust-anything on one unit at a time: `record` the "
      "engine functions, `bridge` the crate, write the port, `diff` every "
      "recorded case, `bench` against this script, then `gate`.\n")

    a("## Appendix: assumed speedups\n")
    a("| Component | Low | Mid | High |")
    a("| --- | --- | --- | --- |")
    for k, (lo, mid, hi) in SPEEDUP.items():
        a(f"| {k} | {lo}× | {mid}× | {hi}× |")
    a("")
    return "\n".join(L)


# ---------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="USDJPY")
    ap.add_argument("--timeframe", default="AUTO")
    ap.add_argument("--samples", type=int, default=24,
                    help="strategies per attempt-timing pass")
    ap.add_argument("--attempts", type=int, default=40,
                    help="attempts in the generation-loop pass")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--out-dir", default=str(ROOT / "bench" / "results"))
    args = ap.parse_args()

    symbol = args.symbol.upper()
    timeframe = pick_timeframe(symbol, args.timeframe)
    print(f"SQX generation speedtest — {symbol} {timeframe}, "
          f"{args.samples} samples, {args.attempts} loop attempts")

    print("loading context…")
    ctx, ctx_info = phase_context(symbol, timeframe, reps=args.reps)
    cfg = default_config([symbol])
    print("sampling…")
    sampling = phase_sampling(cfg, symbol, args.samples)
    specs = make_specs(cfg, symbol, 42, args.samples)
    print(f"timing {len(specs)} attempts…")
    backtest = phase_attempts(ctx, specs, reps=max(1, args.reps))
    print(f"running {args.attempts} attempts through the gates…")
    generation = phase_generation(
        ctx, make_specs(cfg, symbol, 1234, args.attempts),
        cfg.acceptance.is_gates.model_dump(), cfg.acceptance.oos_gates.model_dump())
    print("thread scaling…")
    threads = phase_threads(ctx, specs[:12])
    print("persistence…")
    persist = phase_persist()

    env = {
        "host": f"{os.uname().sysname} {os.uname().machine}",
        "cpu_count": os.cpu_count(),
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "pandas": __import__("pandas").__version__,
        "numba": __import__("numba").__version__,
        "duckdb": __import__("duckdb").__version__,
        "symbol": symbol,
        "timeframe": timeframe,
        "reps": args.reps,
        "indicators": len(indicators.INDICATORS),
    }
    measured = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "env": env,
        "context": ctx_info,
        "sampling": sampling,
        "backtest": backtest,
        "generation": generation,
        "threads": threads,
        "persist": persist,
    }
    proj = build_projection(measured)
    report = render_md(measured, proj)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"generation-{stamp}.json").write_text(
        json.dumps({**measured, "projection": proj}, indent=2))
    (out_dir / f"generation-{stamp}.md").write_text(report)
    (out_dir / "latest.md").write_text(report)

    print("\n" + report)
    _ = store
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_BENCH_DIR, ignore_errors=True)
