"""Diff the Rust port against the recorded Python behaviour (rust-anything `diff`).

Stage 1 — replay: Python-sampled specs evaluated by both engines.
Stage 2 — sampler round-trip: Rust-sampled specs evaluated by both engines.

Comparison classes
------------------
`exact`    identical trade list (bars, side, reason, prices), identical signal
           counts, daily equity to 1e-9, metrics to 5e-4.
`boundary` one or more rule comparisons landed on the other side of a threshold
           because pandas' arithmetic is order-dependent (its rolling variance
           alone differs from an exact two-pass by up to 5e-9).  The spec is
           still "the same strategy", but a tie broke the other way, and one
           flipped 30-bar signal can move later trades.

The gate requires: every indicator array within 1e-5 absolute / 1e-6 relative,
at least 80% of specs `exact`, and no `boundary` spec with |Δsharpe| > 0.25 or
more than 0.01% of M1 bars of signal difference (440 bars here).

Usage:
    uv run python bench/rust/parity.py
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

import sqx.settings as settings  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="sqx-parity-"))
settings.DB_PATH = _TMP / "parity.duckdb"

from sqx.engine import IndicatorCache, load_context, run_backtest  # noqa: E402

SESSION = {"start_hour": 0, "end_hour": 23, "days": [0, 1, 2, 3, 4],
           "close_at_end": True}

METRIC_KEYS = ["trades", "net_return", "max_dd", "return_dd", "sharpe",
               "stagnation_days", "profit_factor", "win_rate", "avg_trade"]
METRIC_TOL = 5e-4
# The Python result dict rounds trade prices to 5 dp and pnl to 2 dp before the
# caller ever sees them, so these tolerances are the rounding, not the engine.
PRICE_TOL = 1e-4
PNL_TOL = 1e-2
EQ_TOL = 1e-9
MAX_SHARPE_DELTA = 0.25
MAX_SIGNAL_FRACTION = 1e-4


def rust(args: list[str]) -> dict[str, Any]:
    exe = ROOT / "rust" / "sqx-gen" / "target" / "release" / "sqx-gen"
    out = subprocess.run([str(exe), *args], capture_output=True, text=True)
    if out.returncode != 0:
        print(out.stdout[-3000:])
        print(out.stderr[-3000:])
        raise SystemExit(f"sqx-gen failed: {out.returncode}")
    tail = [l for l in out.stdout.strip().splitlines() if l.startswith("{")]
    return json.loads(tail[-1]) if tail else {}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def classify(py: dict, rs: dict, trades_py: list, trades_rs: list, n_m1: int) -> dict:
    """Compare one spec: exact, boundary (float tie) or broken."""
    raw_eq = py["daily_eq"]
    eq_py = (np.frombuffer(base64.b64decode(raw_eq), dtype=np.float64)
             if isinstance(raw_eq, str) else np.asarray(raw_eq, dtype=np.float64))
    eq_rs = np.asarray(rs["daily_eq"], dtype=np.float64)
    eq_diff = float(np.max(np.abs(eq_py - eq_rs))) if len(eq_py) else 0.0
    dsig = sum(abs(int(py["signal_counts"][k]) - int(rs["signal_counts"][k]))
               for k in ("long", "short", "exit"))
    dsharpe = max(abs(float(py[s]["sharpe"]) - float(rs[s]["sharpe"]))
                  for s in ("metrics_is", "metrics_oos"))
    dret = max(abs(float(py[s]["net_return"]) - float(rs[s]["net_return"]))
               for s in ("metrics_is", "metrics_oos"))
    metrics_ok = all(abs(float(py[s][k]) - float(rs[s][k])) <= METRIC_TOL
                     for s in ("metrics_is", "metrics_oos", "metrics_all")
                     for k in METRIC_KEYS)
    trades_ok = len(trades_py) == len(trades_rs)
    if trades_ok:
        for a, b in zip(trades_py, trades_rs):
            if int(a[0]) != int(b[0]) or int(a[1]) != int(b[1]) \
                    or int(a[2]) != int(b[2]) or int(a[6]) != int(b[6]):
                trades_ok = False
                break
            if abs(a[3] - b[3]) > PRICE_TOL or abs(a[4] - b[4]) > PRICE_TOL \
                    or abs(a[5] - b[5]) > PNL_TOL:
                trades_ok = False
                break

    if trades_ok and dsig == 0 and eq_diff <= EQ_TOL and metrics_ok:
        kind = "exact"
    elif dsig <= MAX_SIGNAL_FRACTION * n_m1 and dsharpe <= MAX_SHARPE_DELTA:
        kind = "boundary"
    else:
        kind = "broken"
    return {
        "kind": kind,
        "trade_delta": len(trades_rs) - len(trades_py),
        "signal_delta": dsig,
        "equity_delta": eq_diff,
        "sharpe_delta": dsharpe,
        "net_return_delta": dret,
    }


def summarize(rows: list[dict]) -> dict:
    exact = sum(1 for r in rows if r["kind"] == "exact")
    boundary = [r for r in rows if r["kind"] == "boundary"]
    broken = [r for r in rows if r["kind"] == "broken"]
    return {
        "specs": len(rows),
        "exact": exact,
        "exact_share": round(exact / max(1, len(rows)), 4),
        "boundary": len(boundary),
        "broken": len(broken),
        "max_signal_delta": max((r["signal_delta"] for r in rows), default=0),
        "max_sharpe_delta": round(max((r["sharpe_delta"] for r in rows), default=0.0), 4),
        "max_equity_delta": round(max((r["equity_delta"] for r in rows), default=0.0), 6),
        "boundary_examples": [
            {k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()}
            for r in sorted(boundary + broken, key=lambda r: -r["sharpe_delta"])[:8]
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", default=str(ROOT / "bench" / "rust" / "cases"))
    ap.add_argument("--roundtrip", type=int, default=60,
                    help="Rust-sampled specs to evaluate in both engines (0 = skip)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    cases = Path(args.cases)
    base_args = ["--ctx", str(cases / "context.bin"),
                 "--catalogue", str(cases / "catalogue.txt")]
    ctx = load_context("USDJPY", "M30", 40, SESSION)
    n_m1 = len(ctx.m1_ts)

    print("== indicator gate ==")
    ind = rust(base_args + ["--check-indicators", str(cases / "ind_goldens.bin")])
    print(f"  {ind['arrays']} arrays from {ind['records']} param sets, "
          f"{'pass' if ind['pass'] else str(len(ind['failures'])) + ' failures'}, "
          f"worst |diff| {max(ind['worst_abs'].values(), default=0.0):.1e}")

    print("== stage 1: replay parity ==")
    results_path = cases / "results.jsonl"
    info = rust(base_args + ["--specs", str(cases / "specs.jsonl"), "--out", str(results_path)])
    print(f"  rust replayed {info['specs']} specs in {info['seconds']:.1f}s "
          f"({info['attempts_per_sec']} attempts/s)")
    golden = read_jsonl(cases / "golden.jsonl")
    rust_out = read_jsonl(results_path)
    rows1 = []
    for a, b in zip(golden, rust_out):
        r = classify(a, b, a["trades"], b["trades"], n_m1)
        r["idx"] = a["idx"]
        rows1.append(r)
    stage1 = summarize(rows1)

    stage2 = {"specs": 0}
    if args.roundtrip:
        print("== stage 2: Rust sampler round-trip ==")
        rs_specs = cases / "rust_specs.jsonl"
        s = rust(base_args + ["--sample", str(args.roundtrip), "--seed", str(args.seed),
                              "--out", str(rs_specs)])
        print(f"  sampled {s['specs']} specs ({s['unique']} unique)")
        rs_res = cases / "rust_results.jsonl"
        rust(base_args + ["--specs", str(rs_specs), "--out", str(rs_res)])
        rust_recs = read_jsonl(rs_res)
        rows2 = []
        for i, (spec, b) in enumerate(zip(read_jsonl(rs_specs), rust_recs)):
            r = run_backtest(spec, ctx, IndicatorCache())
            trades = []
            for t in r["trades"]:
                ets = int(np.datetime64(t["entry_ts"]).astype("datetime64[s]").astype("int64"))
                xts = int(np.datetime64(t["exit_ts"]).astype("datetime64[s]").astype("int64"))
                trades.append([int(np.searchsorted(ctx.m1_ts, ets)),
                               int(np.searchsorted(ctx.m1_ts, xts)),
                               1 if t["side"] == "long" else -1,
                               t["entry"], t["exit"], t["pnl"], t["reason"]])
            a = {
                "signal_counts": r["signal_counts"],
                "metrics_is": r["metrics_is"],
                "metrics_oos": r["metrics_oos"],
                "metrics_all": r["metrics_all"],
                "daily_eq": r["daily_eq"],
            }
            row = classify(a, b, trades, b["trades"], n_m1)
            row["idx"] = i
            rows2.append(row)
        stage2 = summarize(rows2)

    ok = (
        ind["pass"]
        and stage1["exact_share"] >= 0.8
        and stage1["broken"] == 0
        and (not stage2["specs"] or (stage2["exact_share"] >= 0.8 and stage2["broken"] == 0))
    )
    report = {
        "indicators": {"records": ind["records"], "arrays": ind["arrays"],
                       "pass": ind["pass"],
                       "max_abs": max(ind["worst_abs"].values(), default=0.0),
                       "failures": ind["failures"]},
        "stage1_replay": stage1,
        "stage2_sampler": stage2,
        "thresholds": {"exact_share": 0.8, "max_sharpe_delta": MAX_SHARPE_DELTA,
                       "max_signal_fraction": MAX_SIGNAL_FRACTION},
        "pass": ok,
    }
    (cases / "parity.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "stage2_sampler"}, indent=2)[:3000])
    if stage2["specs"]:
        print("stage2:", json.dumps({k: v for k, v in stage2.items() if k != "boundary_examples"}))
    print("\nGATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
