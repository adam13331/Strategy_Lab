"""Record the SQX generation engine's behaviour for the Rust port.

Dumps everything the Rust engine needs to (a) replay identical work and
(b) be diffed against Python:

    context.bin      Context arrays (raw little-endian, see rust/sqx-gen/src/ctx.rs)
    catalogue.txt    Indicator metadata (params, outputs) for the Rust sampler
    gates.json       Default acceptance gates
    specs.jsonl      Python-sampled strategy specs to replay
    golden.jsonl     Python run_backtest output per spec
    ind_goldens.bin  Per-indicator golden arrays for all 52 indicators

Usage:
    uv run python bench/rust/export_cases.py --specs 300
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

import sqx.settings as settings  # noqa: E402

_BENCH_DIR = Path(tempfile.mkdtemp(prefix="sqx-export-"))
settings.DB_PATH = _BENCH_DIR / "export.duckdb"

from sqx import engine, indicators  # noqa: E402
from sqx.engine import IndicatorCache, load_context, run_backtest  # noqa: E402
from sqx.strategy import default_config, sample_strategy, spec_hash  # noqa: E402

SESSION = {"start_hour": 0, "end_hour": 23, "days": [0, 1, 2, 3, 4],
           "close_at_end": True}

DTYPE = {"i8": 2, "f8": 1, "i64": 0}

# Order and dtypes are mirrored in rust/sqx-gen/src/ctx.rs.
CONTEXT_ARRAYS: list[tuple[str, str]] = [
    ("tf_ts", "i8"), ("tf_close_ts", "i8"), ("tf_to_m1", "i8"),
    ("m1_prev_tf", "i8"), ("m1_ts", "i8"), ("m1_open", "f8"),
    ("m1_high", "f8"), ("m1_low", "f8"), ("m1_close", "f8"),
    ("m1_day_id", "i8"), ("day_ts", "i8"), ("entry_ok", "i8_u8"),
    ("force_close", "i8_u8"), ("m1_tf_idx", "i8"), ("tf_open", "f8"),
    ("tf_high", "f8"), ("tf_low", "f8"), ("tf_close", "f8"),
    ("tf_volume", "f8"),
]


def write_context(ctx: engine.Context, path: Path) -> None:
    arrays: list[tuple[int, np.ndarray]] = []
    tf_columns = {"tf_open": "open", "tf_high": "high", "tf_low": "low",
                  "tf_close": "close", "tf_volume": "volume"}
    for name, kind in CONTEXT_ARRAYS:
        if kind == "i8_u8":
            arrays.append((2, np.ascontiguousarray(getattr(ctx, name), dtype=np.uint8)))
            continue
        if name in tf_columns:
            arr = ctx.tf_df[tf_columns[name]].to_numpy(dtype=np.float64)
            arrays.append((1, np.ascontiguousarray(arr, dtype=np.float64)))
            continue
        arr = getattr(ctx, name)
        if kind == "i8":
            arrays.append((0, np.ascontiguousarray(arr, dtype=np.int64)))
        else:
            arrays.append((1, np.ascontiguousarray(arr, dtype=np.float64)))

    with path.open("wb") as fh:
        fh.write(b"SQXCTX01")
        fh.write(struct.pack("<I", len(arrays)))
        for dtype, arr in arrays:
            fh.write(struct.pack("<BQ", dtype, arr.size))
            fh.write(arr.tobytes())
        scalars = [ctx.split_idx, ctx.split_day, ctx.tf_seconds,
                   ctx.start_ts, ctx.end_ts]
        fh.write(struct.pack("<5q", *[int(s) for s in scalars]))
        fh.write(struct.pack("<d", engine.pip_size(ctx.symbol)))


def write_catalogue(path: Path) -> None:
    lines: list[str] = []
    for ind in indicators.INDICATORS.values():
        lines.append(f"IND {ind.id}")
        for p in ind.params:
            choices = "|".join(p.choices or [])
            if p.choices:
                dflt = float(p.choices.index(p.default)) if p.default in p.choices else 0.0
            else:
                dflt = float(p.default)
            lines.append(
                f"PARAM {p.name} {p.min!r} {p.max!r} {p.step!r} "
                f"{1 if p.integer else 0} {dflt!r} {choices or '-'}")
        for o in ind.outputs:
            levels = "|".join(repr(float(x)) for x in o.levels) or "-"
            lines.append(f"OUT {o.key} {o.kind} {levels}")
        lines.append("END")
    path.write_text("\n".join(lines) + "\n")


def trade_rows(result: dict[str, Any], ctx: engine.Context) -> list[list[float]]:
    rows: list[list[float]] = []
    for t in result["trades"]:
        entry_ts = int(np.datetime64(t["entry_ts"]).astype("datetime64[s]").astype("int64"))
        exit_ts = int(np.datetime64(t["exit_ts"]).astype("datetime64[s]").astype("int64"))
        entry_i = int(np.searchsorted(ctx.m1_ts, entry_ts))
        exit_i = int(np.searchsorted(ctx.m1_ts, exit_ts))
        rows.append([entry_i, exit_i,
                     1 if t["side"] == "long" else -1, t["entry"], t["exit"],
                     t["pnl"], t["reason"]])
    return rows


def b64(arr: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(arr, dtype=np.float64).tobytes()).decode()


def write_indicator_goldens(ctx: engine.Context, path: Path, param_sets: int,
                            seed: int = 7) -> int:
    rng = random.Random(seed)
    records: list[tuple[str, list[float], dict[str, np.ndarray]]] = []
    for ind in indicators.INDICATORS.values():
        seen: set[tuple] = set()
        for _ in range(param_sets * 4):
            if len(seen) >= param_sets:
                break
            args: list[float] = []
            for p in ind.params:
                if p.choices:
                    args.append(float(rng.randrange(len(p.choices))))
                elif p.integer:
                    n = int(round((p.max - p.min) / (p.step or 1)))
                    args.append(float(p.min + rng.randint(0, max(0, n)) * (p.step or 1)))
                else:
                    n = int(round((p.max - p.min) / (p.step or 1)))
                    args.append(float(p.min + rng.randint(0, max(0, n)) * (p.step or 1)))
            key = tuple(round(a, 6) for a in args)
            if key in seen:
                continue
            seen.add(key)
            params = {}
            for p, v in zip(ind.params, args):
                params[p.name] = p.choices[int(v)] if p.choices else (
                    int(round(v)) if p.integer else float(v))
            out = indicators.compute(ind.id, ctx.tf_df, params, ts=ctx.tf_ts)
            records.append((ind.id, list(args), out))

    with path.open("wb") as fh:
        fh.write(b"SQXIND01")
        fh.write(struct.pack("<I", len(records)))
        for ind_id, args, outs in records:
            raw = ind_id.encode()
            fh.write(struct.pack("<H", len(raw)))
            fh.write(raw)
            fh.write(struct.pack("<H", len(args)))
            fh.write(np.asarray(args, dtype=np.float64).tobytes())
            keys = list(outs.keys())
            fh.write(struct.pack("<H", len(keys)))
            for k in keys:
                kb = k.encode()
                fh.write(struct.pack("<H", len(kb)))
                fh.write(kb)
                arr = np.ascontiguousarray(outs[k], dtype=np.float64)
                fh.write(struct.pack("<Q", arr.size))
                fh.write(arr.tobytes())
    return len(records)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="USDJPY")
    ap.add_argument("--timeframe", default="M30")
    ap.add_argument("--specs", type=int, default=300)
    ap.add_argument("--indicator-params", type=int, default=3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out-dir", default=str(ROOT / "bench" / "rust" / "cases"))
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    print("loading context…")
    ctx = load_context(args.symbol, args.timeframe, 40, SESSION)
    write_context(ctx, out / "context.bin")
    write_catalogue(out / "catalogue.txt")
    print(f"  {len(ctx.m1_ts):,} M1 bars, {len(ctx.tf_df):,} TF bars")

    cfg = default_config([args.symbol])
    (out / "gates.json").write_text(json.dumps({
        "is": cfg.acceptance.is_gates.model_dump(),
        "oos": cfg.acceptance.oos_gates.model_dump(),
    }, indent=2))

    print(f"sampling {args.specs} specs and recording goldens…")
    rng = random.Random(args.seed)
    specs = [sample_strategy(rng, cfg, args.symbol) for _ in range(args.specs)]
    with (out / "specs.jsonl").open("w") as fs, (out / "golden.jsonl").open("w") as fg:
        for i, spec in enumerate(specs):
            fs.write(json.dumps(spec) + "\n")
        for i, spec in enumerate(specs):
            r = run_backtest(spec, ctx, IndicatorCache())
            rec = {
                "idx": i,
                "hash": spec_hash(spec),
                "metrics_is": r["metrics_is"],
                "metrics_oos": r["metrics_oos"],
                "metrics_all": r["metrics_all"],
                "final_equity": r["final_equity"],
                "max_dd_m1": r["max_dd_m1"],
                "split_day": r["split_day"],
                "signal_counts": r["signal_counts"],
                "trades": trade_rows(r, ctx),
                "daily_eq": b64(r["daily_eq"]),
            }
            fg.write(json.dumps(rec) + "\n")
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(specs)}")

    print("recording indicator goldens…")
    n = write_indicator_goldens(ctx, out / "ind_goldens.bin", args.indicator_params)
    print(f"  {n} indicator records")

    sizes = {p.name: p.stat().st_size for p in out.iterdir()}
    print("wrote:", ", ".join(f"{k} {v / 1e6:.1f} MB" for k, v in sorted(sizes.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
