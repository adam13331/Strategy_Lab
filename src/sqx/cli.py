from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer
import uvicorn

from .data import import_csv, resample
from .settings import TIMEFRAMES
from .store import store
from .strategy import GenerateConfig, default_config

app = typer.Typer(add_completion=False, help="SQX Strategy Lab command line")
data_app = typer.Typer(help="Import and resample data")
app.add_typer(data_app, name="data")


@data_app.command("import")
def data_import(path: str, symbol: Optional[str] = None) -> None:
    res = import_csv(path, symbol)
    typer.echo(json.dumps({k: v for k, v in res.items()
                           if k not in ("years", "weekday_counts", "largest_gaps_seconds")},
                          indent=2))


@data_app.command("resample")
def data_resample(symbol: str, timeframes: str = "M5,M15,M30,H1,H4,D1") -> None:
    tfs = [t.strip().upper() for t in timeframes.split(",") if t.strip()]
    bad = [t for t in tfs if t not in TIMEFRAMES]
    if bad:
        raise typer.BadParameter(f"Unknown timeframes: {bad}")
    for r in resample(symbol, tfs):
        typer.echo(f"{r['timeframe']:>4}  {r['rows']:>9}  {r['start']} -> {r['end']}")


@app.command("run")
def run(config: str, name: Optional[str] = None) -> None:
    from .runner import manager
    cfg = GenerateConfig(**json.loads(Path(config).read_text()))
    info = manager.start(cfg, name)
    typer.echo(f"run {info['run_id']} started")
    run_id = info["run_id"]
    while True:
        st = manager.get(run_id)
        if not st:
            break
        snap = st.snapshot()
        typer.echo(
            f"{snap['status']:>9}  attempts={snap['attempts']:<7} "
            f"accepted={snap['accepted']}/{snap['target']:<4} "
            f"rate={snap['throughput']}/s"
        )
        if snap["status"] in ("completed", "stopped", "failed"):
            if snap.get("error"):
                typer.echo(snap["error"])
            break
        time.sleep(5)


@app.command("stress")
def stress(source_run_id: str, config: Optional[str] = None,
           parameter_rounds: int = 30, shuffle_rounds: int = 2000,
           mc_rounds: int = 500, name: Optional[str] = None) -> None:
    """Run the sequential five-stage robustness pipeline."""
    from .stress import StressConfig, default_stress_config
    from .stress_runner import stress_manager
    if config:
        cfg = StressConfig(**json.loads(Path(config).read_text()))
        cfg.source_run_id = source_run_id
    else:
        cfg = default_stress_config(source_run_id)
        cfg.parameter.rounds = parameter_rounds
        cfg.shuffle.rounds = shuffle_rounds
        cfg.monte_carlo.rounds = mc_rounds
    info = stress_manager.start(cfg, name)
    typer.echo(f"stress {info['stress_id']} started: {info['strategies']} strategies,"
               f" {info['tests_requested']} scenarios")
    stress_id = info["stress_id"]
    while True:
        st = stress_manager.get(stress_id)
        if not st:
            break
        snap = st.snapshot()
        cur = snap.get("current") or {}
        typer.echo(
            f"{snap['status']:>9}  strategies={snap['strategies_done']}/{snap['strategies_total']:<4} "
            f"survivors={snap['survivors']:<4} tests={snap['tests_run']}/{snap['tests_requested']:<8} "
            f"{cur.get('strategy_id') or ''} {cur.get('stage') or ''}"
        )
        if snap["status"] in ("completed", "stopped", "failed"):
            if snap.get("error"):
                typer.echo(snap["error"])
            if snap.get("bank_id"):
                typer.echo(f"bank {snap['bank_id']}")
            break
        time.sleep(3)


@data_app.command("list")
def data_list() -> None:
    for s in store.symbols():
        typer.echo(f"{s['symbol']:>10}  {s['rows']:>10}  {s['start_ts']} -> {s['end_ts']}")


@app.command("defaults")
def defaults(out: str = typer.Argument("configs/default.json")) -> None:
    syms = [s["symbol"] for s in store.symbols()]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(default_config(syms).model_dump(), indent=2))
    typer.echo(f"wrote {out}")


@app.command("serve")
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    uvicorn.run("sqx.api:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
