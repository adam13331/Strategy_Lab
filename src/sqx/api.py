from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .data import import_csv, list_csv_files, resample
from .indicators import INDICATORS, catalogue
from .mt5 import check_terminal, compare_with_sqx, export_strategies, run_in_tester
from .portfolio import compose as compose_portfolio
from .runner import manager
from .stress import StressConfig, default_stress_config
from .stress_runner import stress_manager
from .settings import WEB_DIST, bank_dir, m1_path, result_dir, tf_path
from .store import store
from .strategy import GenerateConfig, default_config, describe

app = FastAPI(title="SQX Strategy Lab", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


class ImportRequest(BaseModel):
    path: str
    symbol: str | None = None


class ResampleRequest(BaseModel):
    symbol: str
    timeframes: list[str]


class RunRequest(BaseModel):
    config: GenerateConfig
    name: str | None = None
    clear_banks: bool = False


class StressRequest(BaseModel):
    config: StressConfig
    name: str | None = None
    clear_banks: bool = False


class SettingsRequest(BaseModel):
    max_ram_mb: int = 0
    performance: str = "balanced"
    workers: int = 0
    clear_cache_after_run: bool = True


class Mt5PathRequest(BaseModel):
    path: str = ""


class Mt5BuildRequest(BaseModel):
    path: str = ""
    bank_id: str = ""
    strategy_ids: list[str] = []


class Mt5TesterRequest(BaseModel):
    path: str = ""
    bank_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    from_date: str = ""
    to_date: str = ""
    deposit: float = 10000
    leverage: str = "1:30"
    model: int = 0          # 0 every tick .. 4 every tick based on real ticks
    launch: bool = True


class Mt5CompareRequest(BaseModel):
    path: str = ""
    bank_id: str = ""
    strategy_id: str = ""
    threshold: float = 0.7


class PortfolioRequest(BaseModel):
    bank_id: str = ""
    strategy_ids: list[str] = []
    max_corr: float = 0.5
    vol_target: float = 0.12
    min_months: int = 6
    profile: str = "personal"
    profile_overrides: dict[str, float] = {}
    stages: dict[str, dict[str, Any]] = {}


def _total_ram_mb() -> int:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size / (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        return 0


def _settings_payload() -> dict[str, Any]:
    from .runtime import load_runtime
    from .engine import indicator_cache_stats
    rt = load_runtime()
    return {
        "settings": rt.__dict__,
        "system": {
            "cpu_count": os.cpu_count() or 1,
            "total_ram_mb": _total_ram_mb(),
        },
        "effective": {
            "workers": rt.effective_workers(),
            "cache_budget_mb": round(rt.cache_budget_bytes() / (1024 * 1024)),
        },
        "cache": indicator_cache_stats(),
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "indicators": len(INDICATORS)}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return _settings_payload()


@app.post("/api/settings")
def put_settings(req: SettingsRequest) -> dict[str, Any]:
    from .runtime import RuntimeSettings, save_runtime
    if req.performance not in ("low_memory", "balanced", "high_throughput"):
        raise HTTPException(400, "performance must be low_memory, balanced or high_throughput")
    save_runtime(RuntimeSettings(
        max_ram_mb=max(0, req.max_ram_mb),
        performance=req.performance,
        workers=max(0, req.workers),
        clear_cache_after_run=bool(req.clear_cache_after_run),
    ))
    return _settings_payload()


@app.post("/api/settings/clear-cache")
def clear_cache() -> dict[str, Any]:
    from .engine import clear_indicator_cache
    freed = clear_indicator_cache()
    return {"freed_bytes": freed, **_settings_payload()}


@app.get("/api/symbols")
def symbols() -> list[dict[str, Any]]:
    out = store.symbols()
    for s in out:
        s["bar_caches"] = store.bar_caches(s["symbol"])
    return out


@app.get("/api/data/quality")
def quality(symbol: str) -> dict[str, Any]:
    s = store.symbol(symbol)
    if not s:
        raise HTTPException(404, "Symbol not found")
    s["bar_caches"] = store.bar_caches(symbol)
    return s


@app.get("/api/data/files")
def data_files() -> dict[str, Any]:
    return list_csv_files()


@app.post("/api/data/import")
def data_import(req: ImportRequest) -> dict[str, Any]:
    try:
        return import_csv(req.path, req.symbol)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/data/resample")
def data_resample(req: ResampleRequest) -> list[dict[str, Any]]:
    try:
        return resample(req.symbol, req.timeframes)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/indicators")
def indicators() -> dict[str, Any]:
    return {"catalogue": catalogue(), "defaults": default_config().indicators}


@app.get("/api/indicators/export")
@app.get("/api/indicators/export/mql5")
@app.get("/api/indicators/mql5/export")
@app.post("/api/indicators/export")
def export_indicators() -> Response:
    from .mql5_export import build_indicator_zip

    data = build_indicator_zip()
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="SQX_indicators.zip"'},
    )

@app.get("/api/config/default")
def config_default() -> dict[str, Any]:
    syms = [s["symbol"] for s in store.symbols()]
    return default_config(syms).model_dump()


@app.post("/api/preflight")
def preflight(cfg: GenerateConfig) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    if not cfg.symbols:
        errors.append("Select at least one symbol")
    for sym in cfg.symbols:
        if not store.symbol(sym):
            errors.append(f"{sym} is not imported")
            continue
        p = m1_path(sym) if cfg.timeframe == "M1" else tf_path(sym, cfg.timeframe)
        if not p.exists():
            errors.append(f"{sym} {cfg.timeframe} bars are not built yet")
    enabled = [k for k, v in cfg.indicators.items() if v.enabled]
    if not enabled:
        errors.append("Enable at least one indicator")
    variants = 0
    for ind_id in enabled:
        ind = INDICATORS.get(ind_id)
        if not ind:
            continue
        st = cfg.indicators[ind_id]
        per_indicator = 1
        for p in ind.params:
            if p.choices:
                per_indicator *= max(1, len(p.choices))
                continue
            r = st.params.get(p.name)
            if r:
                per_indicator *= max(
                    1, int(round((r.max - r.min) / max(r.step, 0.0001))) + 1)
        variants += per_indicator
    if cfg.target_accepted <= 0:
        errors.append("Target must be positive")
    if cfg.oos_pct >= 70:
        warnings.append("Very large OOS share: IS may not have enough trades")
    if cfg.acceptance.oos_gates.min_trades > 50:
        warnings.append("High OOS min-trades gate will slow the search considerably")
    per_attempt = {"M5": 2.4, "M15": 1.0, "M30": 0.7, "H1": 0.7,
                   "H4": 0.3, "D1": 0.15}.get(cfg.timeframe, 1.5)
    est = int((cfg.target_accepted / 0.08) * per_attempt) if cfg.target_accepted else 0
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "enabled_indicators": len(enabled),
        "indicator_variants": variants,
        "rough_seconds": est,
        "oos_pct": cfg.oos_pct,
    }


@app.post("/api/runs")
def create_run(req: RunRequest) -> dict[str, Any]:
    try:
        return manager.start(req.config, req.name, clear_banks=req.clear_banks)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/runs")
def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    return store.list_runs(limit)


@app.get("/api/runs/active")
def active_run() -> dict[str, Any] | None:
    """Live snapshot of the single generation that is running, if any."""
    st = manager.live_run()
    if not st:
        return None
    snap = st.snapshot()
    rec = store.get_run(st.run_id) or {}
    snap["name"] = rec.get("name")
    snap["config"] = rec.get("config")
    return snap


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    st = manager.get(run_id)
    if st:
        snap = st.snapshot()
        rec = store.get_run(run_id) or {}
        snap["name"] = rec.get("name")
        snap["config"] = rec.get("config")
        return snap
    rec = store.get_run(run_id)
    if not rec:
        raise HTTPException(404, "Run not found")
    rows = store.list_strategies(run_id, limit=8)
    rec["recent"] = [
        {
            "strategy_id": r["strategy_id"],
            "symbol": r["symbol"],
            "timeframe": r["timeframe"],
            "is_sharpe": r.get("metrics_is", {}).get("sharpe"),
            "oos_sharpe": r.get("metrics_oos", {}).get("sharpe"),
            "return_dd": r.get("metrics_oos", {}).get("return_dd"),
            "trades": r.get("metrics_oos", {}).get("trades"),
        }
        for r in rows
    ]
    stats = rec.get("stats") or {}
    for key in ("attempts", "accepted", "target", "elapsed", "throughput", "funnel"):
        rec.setdefault(key, stats.get(key))
    rec["funnel"] = rec.get("funnel") or {}
    rec["target"] = rec.get("target") or 0
    return rec


@app.post("/api/runs/{run_id}/stop")
def stop_run(run_id: str) -> dict[str, Any]:
    if not manager.stop(run_id):
        raise HTTPException(404, "Run is not active")
    return {"ok": True}


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str):
    if not manager.get(run_id):
        rec = store.get_run(run_id)
        if not rec:
            raise HTTPException(404, "Run not found")

        def once():
            yield f"data: {json.dumps({'run_id': run_id, 'status': rec['status'], 'done': True})}\n\n"
        return StreamingResponse(once(), media_type="text/event-stream")

    def stream():
        last = None
        while True:
            st = manager.get(run_id)
            if not st:
                yield f"data: {json.dumps({'run_id': run_id, 'status': 'unknown', 'done': True})}\n\n"
                return
            snap = st.snapshot()
            payload = dict(snap)
            payload["done"] = snap["status"] in ("completed", "stopped", "failed")
            blob = json.dumps(payload)
            if blob != last:
                yield f"data: {blob}\n\n"
                last = blob
            if payload["done"]:
                return
            time.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ----------------------------------------------------------- stress pipeline
@app.get("/api/stress/defaults")
def stress_defaults(source_run_id: str = "") -> dict[str, Any]:
    cfg = default_stress_config(source_run_id)
    symbols = [s["symbol"] for s in store.symbols()]
    run = store.get_run(source_run_id) if source_run_id else None
    primary = set((run or {}).get("config", {}).get("symbols") or [])
    alternates = [s for s in symbols if s not in primary]
    cfg.cross_market.alternate_symbols = alternates or None
    return cfg.model_dump()


@app.post("/api/stress")
def create_stress(req: StressRequest) -> dict[str, Any]:
    try:
        return stress_manager.start(req.config, req.name, clear_banks=req.clear_banks)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/stress")
def list_stress(limit: int = 50) -> list[dict[str, Any]]:
    return store.list_stress(limit)


@app.get("/api/stress/{stress_id}")
def get_stress(stress_id: str) -> dict[str, Any]:
    st = stress_manager.get(stress_id)
    if st:
        snap = st.snapshot()
        rec = store.get_stress(stress_id) or {}
        snap["name"] = rec.get("name")
        snap["config"] = rec.get("config")
        snap["results"] = store.list_stress_results(stress_id)
        return snap
    rec = store.get_stress(stress_id)
    if not rec:
        raise HTTPException(404, "Stress run not found")
    stats = rec.get("stats") or {}
    for key in ("strategies_total", "strategies_done", "survivors",
                "tests_requested", "tests_run", "elapsed", "throughput",
                "stages_requested", "bank_id"):
        rec.setdefault(key, stats.get(key))
    rec["results"] = store.list_stress_results(stress_id)
    return rec


@app.post("/api/stress/{stress_id}/stop")
def stop_stress(stress_id: str) -> dict[str, Any]:
    if not stress_manager.stop(stress_id):
        raise HTTPException(404, "Stress run is not active")
    return {"ok": True}


@app.get("/api/stress/{stress_id}/events")
def stress_events(stress_id: str):
    if not stress_manager.get(stress_id):
        rec = store.get_stress(stress_id)
        if not rec:
            raise HTTPException(404, "Stress run not found")

        def once():
            yield f"data: {json.dumps({'stress_id': stress_id, 'status': rec['status'], 'done': True})}\n\n"
        return StreamingResponse(once(), media_type="text/event-stream")

    def stream():
        last = None
        while True:
            st = stress_manager.get(stress_id)
            if not st:
                yield f"data: {json.dumps({'stress_id': stress_id, 'status': 'unknown', 'done': True})}\n\n"
                return
            snap = st.snapshot()
            payload = dict(snap)
            payload["done"] = snap["status"] in ("completed", "stopped", "failed")
            blob = json.dumps(payload)
            if blob != last:
                yield f"data: {blob}\n\n"
                last = blob
            if payload["done"]:
                return
            time.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# -------------------------------------------------------------------- banks
@app.get("/api/banks")
def banks(group: str | None = None, bank_type: str | None = None,
          category: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
    return store.list_banks(group=group, bank_type=bank_type,
                            category=category, kind=kind)


@app.get("/api/banks/{bank_id}")
def bank(bank_id: str) -> dict[str, Any]:
    rec = store.get_bank(bank_id)
    if not rec:
        raise HTTPException(404, "Bank not found")
    rec["strategies"] = store.list_bank_strategies(bank_id, limit=1000)
    return rec


@app.get("/api/banks/{bank_id}/strategies/{strategy_id}")
def bank_strategy(bank_id: str, strategy_id: str) -> dict[str, Any]:
    rec = store.get_bank_strategy(bank_id, strategy_id)
    if not rec:
        raise HTTPException(404, "Bank strategy not found")
    rec["rules"] = describe(rec["spec"])
    return rec


def _bank_file(bank_id: str, strategy_id: str, suffix: str) -> pd.DataFrame | None:
    p = bank_dir(bank_id) / f"{strategy_id}_{suffix}.parquet"
    if not p.exists():
        return None
    return pd.read_parquet(p)


@app.get("/api/banks/{bank_id}/strategies/{strategy_id}/equity")
def bank_equity(bank_id: str, strategy_id: str) -> list[dict[str, Any]]:
    df = _bank_file(bank_id, strategy_id, "equity")
    if df is None:
        raise HTTPException(404, "Equity file missing")
    segs = df["segment"].astype(str).tolist() if "segment" in df.columns else ["IS"] * len(df)
    return [{"ts": str(t), "equity": round(float(e), 2), "segment": g}
            for t, e, g in zip(df["ts"], df["equity"], segs)]


@app.get("/api/banks/{bank_id}/strategies/{strategy_id}/trades")
def bank_trades(bank_id: str, strategy_id: str, limit: int = 1000) -> list[dict[str, Any]]:
    df = _bank_file(bank_id, strategy_id, "trades")
    if df is None:
        return []
    return df.head(limit).to_dict("records")


@app.get("/api/banks/{bank_id}/strategies/{strategy_id}/export-ea")
@app.get("/api/strategies/{strategy_id}/export-ea")
def export_strategy_ea(bank_id: str | None = None, strategy_id: str = "") -> Response:
    rec = None
    if bank_id:
        rec = store.get_bank_strategy(bank_id, strategy_id)
    if rec is None:
        rec = store.get_strategy(strategy_id)
    if not rec:
        raise HTTPException(404, "Strategy not found")
    from .mql5_ea import build_ea

    code = build_ea(rec.get("spec", {}) or {}, strategy_id)
    return Response(
        content=code,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{strategy_id}.mq5"'},
    )


@app.delete("/api/banks/{bank_id}")
def delete_bank(bank_id: str) -> dict[str, Any]:
    ok = store.delete_bank(bank_id)
    try:
        import shutil
        from .settings import bank_dir
        d = bank_dir(bank_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": ok}


@app.post("/api/banks/clear")
def clear_banks(group: str | None = None, source_run_id: str | None = None) -> dict[str, Any]:
    n = store.delete_banks(group=group, source_run_id=source_run_id)
    return {"deleted": n}


@app.post("/api/banks/hall-of-fame/add")
def add_to_hall_of_fame(strategy_id: str, source: str = "run") -> dict[str, Any]:
    # source: "run" (from strategies) or "bank:<bank_id>"
    if source.startswith("bank:"):
        bank_id_src = source.split(":", 1)[1]
        rec = store.get_bank_strategy(bank_id_src, strategy_id)
        if not rec:
            raise HTTPException(404, "Strategy not found in bank")
        src_run = rec.get("source_run_id")
        spec = rec.get("spec")
    else:
        rec = store.get_strategy(strategy_id)
        if not rec:
            raise HTTPException(404, "Strategy not found")
        src_run = rec.get("run_id")
        spec = rec.get("spec")
    hof = store.ensure_hall_of_fame()
    # copy files if present
    import shutil
    from .settings import bank_dir, result_dir
    src_dir = (bank_dir(source.split(":",1)[1]) if source.startswith("bank:")
               else result_dir(src_run))
    tgt = bank_dir(hof)
    for suffix in ("equity", "trades"):
        sp = src_dir / f"{strategy_id}_{suffix}.parquet"
        if sp.exists():
            shutil.copy2(sp, tgt / f"{strategy_id}_{suffix}.parquet")
    (tgt / f"{strategy_id}_spec.json").write_text(
        __import__("json").dumps(spec, indent=2))
    store.add_bank_strategy(hof, {
        "strategy_id": strategy_id,
        "source_run_id": src_run,
        "spec": spec,
        "symbol": rec.get("symbol"),
        "timeframe": rec.get("timeframe"),
        "direction": rec.get("direction"),
        "metrics_is": rec.get("metrics_is", {}),
        "metrics_oos": rec.get("metrics_oos", {}),
        "metrics_all": rec.get("metrics_all", {}),
        "score": rec.get("score", 0.0),
        "group": "hall_of_fame",
    })
    return {"bank_id": hof, "strategy_id": strategy_id}


@app.get("/api/strategies")
def strategies(run_id: str | None = None, limit: int = 100,
               offset: int = 0) -> dict[str, Any]:
    rows = store.list_strategies(run_id, limit=limit, offset=offset)
    return {"items": rows, "total": store.count_strategies(run_id),
            "limit": limit, "offset": offset}


@app.get("/api/strategies/{strategy_id}")
def strategy(strategy_id: str) -> dict[str, Any]:
    rec = store.get_strategy(strategy_id)
    if not rec:
        raise HTTPException(404, "Strategy not found")
    rec["rules"] = describe(rec["spec"])
    return rec


@app.get("/api/strategies/{strategy_id}/export")
def export_strategy(strategy_id: str) -> dict[str, Any]:
    rec = store.get_strategy(strategy_id)
    if not rec:
        # try banks
        for b in store.list_banks():
            r = store.get_bank_strategy(b["bank_id"], strategy_id)
            if r:
                rec = r
                break
    if not rec:
        raise HTTPException(404, "Strategy not found")
    return {
        "strategy_id": rec.get("strategy_id", strategy_id),
        "spec": rec.get("spec"),
        "symbol": rec.get("symbol"),
        "timeframe": rec.get("timeframe"),
        "direction": rec.get("direction"),
        "metrics_is": rec.get("metrics_is", {}),
        "metrics_oos": rec.get("metrics_oos", {}),
        "metrics_all": rec.get("metrics_all", {}),
        "score": rec.get("score", 0.0),
    }


class ImportStrategyRequest(BaseModel):
    bank_id: str | None = None
    strategy: dict[str, Any]


@app.post("/api/strategies/import")
def import_strategy(req: ImportStrategyRequest) -> dict[str, Any]:
    s = req.strategy
    sid = s.get("strategy_id") or ("imported-" + __import__("uuid").uuid4().hex[:8])
    bank_id = req.bank_id
    if not bank_id:
        bank_id = store.ensure_hall_of_fame()
    store.add_bank_strategy(bank_id, {
        "strategy_id": sid,
        "source_run_id": s.get("source_run_id", "imported"),
        "spec": s.get("spec", {}),
        "symbol": s.get("symbol"),
        "timeframe": s.get("timeframe"),
        "direction": s.get("direction"),
        "metrics_is": s.get("metrics_is", {}),
        "metrics_oos": s.get("metrics_oos", {}),
        "metrics_all": s.get("metrics_all", {}),
        "score": s.get("score", 0.0),
        "group": "hall_of_fame" if bank_id.startswith("bank_hof") else None,
    })
    return {"bank_id": bank_id, "strategy_id": sid}


@app.get("/api/strategies/{strategy_id}/equity")
def strategy_equity(strategy_id: str) -> list[dict[str, Any]]:
    rec = store.get_strategy(strategy_id)
    if not rec:
        raise HTTPException(404, "Strategy not found")
    p = result_dir(rec["run_id"]) / f"{strategy_id}_equity.parquet"
    if not p.exists():
        raise HTTPException(404, "Equity file missing")
    df = pd.read_parquet(p)
    if "segment" in df.columns:
        segs = df["segment"].astype(str).tolist()
    else:
        # Older files predate the segment column; derive it from the run config.
        segs = ["IS"] * len(df)
        rec_run = store.get_run(rec["run_id"]) or {}
        oos = float(rec_run.get("config", {}).get("oos_pct", 0))
        if oos > 0 and len(df):
            cut = int(round(len(df) * (1 - oos / 100.0)))
            segs = ["IS"] * cut + ["OOS"] * (len(df) - cut)
    return [{"ts": str(t), "equity": round(float(e), 2), "segment": g}
            for t, e, g in zip(df["ts"], df["equity"], segs)]


@app.get("/api/strategies/{strategy_id}/trades")
def strategy_trades(strategy_id: str, limit: int = 1000) -> list[dict[str, Any]]:
    rec = store.get_strategy(strategy_id)
    if not rec:
        raise HTTPException(404, "Strategy not found")
    p = result_dir(rec["run_id"]) / f"{strategy_id}_trades.parquet"
    if not p.exists():
        return []
    df = pd.read_parquet(p).head(limit)
    return df.to_dict("records")


# ----------------------------------------------------------------------- mt5
@app.post("/api/mt5/check")
def mt5_check(req: Mt5PathRequest) -> dict[str, Any]:
    try:
        return check_terminal(req.path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/mt5/build")
def mt5_build(req: Mt5BuildRequest) -> dict[str, Any]:
    try:
        return export_strategies(req.path, req.bank_id, req.strategy_ids)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


# --------------------------------------------------------------- portfolio
@app.get("/api/portfolio/profiles")
def portfolio_profiles() -> list[dict[str, Any]]:
    from .profiles import list_profiles
    return list_profiles()


@app.post("/api/portfolio/compose")
def portfolio_compose(req: PortfolioRequest) -> dict[str, Any]:
    try:
        return compose_portfolio(
            req.bank_id, req.strategy_ids,
            max_corr=min(0.99, max(-0.99, req.max_corr)),
            vol_target=min(1.0, max(0.01, req.vol_target)),
            min_months=max(2, req.min_months),
            profile=req.profile,
            profile_overrides=req.profile_overrides,
            stages=req.stages,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/mt5/compare")
def mt5_compare(req: Mt5CompareRequest) -> dict[str, Any]:
    try:
        return compare_with_sqx(req.path, req.bank_id, req.strategy_id,
                               req.threshold)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/mt5/tester")
def mt5_tester(req: Mt5TesterRequest) -> dict[str, Any]:
    try:
        return run_in_tester(
            req.path, req.bank_id, req.strategy_id, req.symbol,
            req.from_date, req.to_date, req.deposit, req.leverage, req.model,
            req.launch,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


# ------------------------------------------------------------------- frontend
if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(WEB_DIST / "index.html")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "Not found")
        f = WEB_DIST / full_path
        if f.exists() and f.is_file():
            return FileResponse(f)
        return FileResponse(WEB_DIST / "index.html")
