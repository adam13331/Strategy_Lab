from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .engine import Context, IndicatorCache, load_context, passes, run_backtest

try:
    import sqx_core as _sqx_core
    _HAS_RUST_CORR = True
except Exception:  # noqa: BLE001
    _sqx_core = None
    _HAS_RUST_CORR = False
from .settings import result_dir, tf_path
from .store import store
from .strategy import GenerateConfig, sample_strategy, spec_hash

LIVE_STATUSES = ("queued", "running", "stopping")

REASON_LABEL = {
    "min_trades": "too few trades",
    "min_sharpe": "Sharpe below gate",
    "stagnation": "stagnation too long",
    "return_dd": "return/DD below gate",
    "max_dd": "drawdown above gate",
    "profit_factor": "profit factor below gate",
    "win_rate": "win rate below gate",
    "structural_duplicate": "structural duplicate",
    "correlation": "correlated with accepted",
    "error": "backtest error",
    "no_trades": "no trades",
}


def _score(is_m: dict, oos_m: dict) -> float:
    return round(
        0.5 * (float(is_m.get("sharpe", 0)) + float(oos_m.get("sharpe", 0)))
        + 0.02 * min(float(is_m.get("return_dd", 0)), float(oos_m.get("return_dd", 0))),
        6,
    )


class RunState:
    def __init__(self, run_id: str, config: GenerateConfig) -> None:
        self.run_id = run_id
        self.config = config
        self.status = "queued"
        self.attempts = 0
        self.accepted = 0
        self.target = config.target_accepted
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.error: str | None = None
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.funnel = {
            "generated": 0, "structural_duplicate": 0, "error": 0,
            "is_fail": 0, "oos_fail": 0, "correlation": 0, "accepted": 0,
        }
        self.recent: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self._last_persist = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            elapsed = (self.finished_at or time.time()) - self.started_at
            return {
                "run_id": self.run_id,
                "status": self.status,
                "attempts": self.attempts,
                "accepted": self.accepted,
                "target": self.target,
                "elapsed": round(elapsed, 1),
                "throughput": round(self.attempts / elapsed, 2) if elapsed > 0 else 0,
                "funnel": dict(self.funnel),
                "recent": list(self.recent[-12:]),
                "current": self.current,
                "error": self.error,
                "started_at": datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(),
            }

    def stats(self) -> dict[str, Any]:
        s = self.snapshot()
        s.pop("recent", None)
        return s


class RunManager:
    def __init__(self) -> None:
        self.active: dict[str, RunState] = {}
        self.lock = threading.Lock()

    def get(self, run_id: str) -> RunState | None:
        return self.active.get(run_id)

    def _live_locked(self) -> RunState | None:
        for st in self.active.values():
            if st.status in LIVE_STATUSES:
                return st
        return None

    def live_run(self) -> RunState | None:
        """The one generation that is queued or running right now, if any."""
        with self.lock:
            return self._live_locked()

    @staticmethod
    def _busy_message(live: RunState) -> str:
        return (f"Generation {live.run_id} is still {live.status}. "
                "Only one generation can run at a time; stop it first.")

    def start(self, config: GenerateConfig, name: str | None = None,
              clear_banks: bool = False) -> dict[str, Any]:
        live = self.live_run()
        if live is not None:
            raise ValueError(self._busy_message(live))
        if not config.symbols:
            raise ValueError("Select at least one symbol")
        for sym in config.symbols:
            s = store.symbol(sym)
            if not s:
                raise ValueError(f"Symbol not imported: {sym}")
            p = tf_path(sym, config.timeframe) if config.timeframe != "M1" else (
                Path(s["quality"].get("path", "")))
            from .settings import m1_path
            p = m1_path(sym) if config.timeframe == "M1" else tf_path(sym, config.timeframe)
            if not p.exists():
                raise ValueError(
                    f"{sym} {config.timeframe} bars not built. Run resample first.")
        if not any(v.enabled for v in config.indicators.values()):
            raise ValueError("Enable at least one indicator")
        if clear_banks:
            store.delete_banks(group="initial_generation")
        with self.lock:
            # Re-check under the lock so two simultaneous requests cannot both start.
            live = self._live_locked()
            if live is not None:
                raise ValueError(self._busy_message(live))
            run_id = uuid.uuid4().hex[:12]
            run_name = name or f"{config.timeframe} {', '.join(config.symbols)} {datetime.now().strftime('%H:%M:%S')}"
            store.create_run(run_id, run_name, config.model_dump(), config.seed)
            st = RunState(run_id, config)
            self.active[run_id] = st
        t = threading.Thread(target=self._run, args=(st,), daemon=True, name=f"run-{run_id}")
        t.start()
        return {"run_id": run_id, "name": run_name}

    def stop(self, run_id: str) -> bool:
        st = self.active.get(run_id)
        if not st:
            return False
        st.stop.set()
        with st.lock:
            st.status = "stopping"
        return True

    # ------------------------------------------------------------------ worker
    def _run(self, st: RunState) -> None:
        cfg = st.config
        store.update_run(st.run_id, status="running", started_at=datetime.now())
        with st.lock:
            st.status = "running"
        try:
            contexts: dict[str, Context] = {}
            for sym in cfg.symbols:
                contexts[sym] = load_context(
                    sym, cfg.timeframe, cfg.oos_pct, cfg.session.model_dump(),
                    execution_timeframe=cfg.timeframe)
            rng = __import__("random").Random(cfg.seed)
            seen_hashes: set[str] = set()
            accepted: list[dict[str, Any]] = []
            attempt = 0
            pending_attempts: list[tuple] = []
            import queue
            import threading
            from concurrent.futures import ThreadPoolExecutor
            from .runtime import load_runtime
            from .engine import clear_indicator_cache

            rt = load_runtime()
            workers = rt.effective_workers()
            batch_size = workers * 16

            persist_q: "queue.Queue[Any]" = queue.Queue()

            def _persist_worker() -> None:
                while True:
                    task = persist_q.get()
                    if task is None:
                        persist_q.task_done()
                        break
                    try:
                        self._persist_strategy(*task)
                    except Exception as exc:  # noqa: BLE001
                        import traceback
                        traceback.print_exc()
                    persist_q.task_done()

            persist_thread = threading.Thread(target=_persist_worker, daemon=True)
            persist_thread.start()

            def evaluate(pair):
                sym, spec = pair
                return run_backtest(spec, contexts[sym], IndicatorCache())

            with ThreadPoolExecutor(max_workers=workers) as pool:
                while len(accepted) < cfg.target_accepted and not st.stop.is_set():
                    batch: list[tuple[str, dict[str, Any], str, bool]] = []
                    for _ in range(batch_size):
                        sym = rng.choice(cfg.symbols)
                        spec = sample_strategy(rng, cfg, sym)
                        h = spec_hash(spec)
                        dup = h in seen_hashes
                        if not dup:
                            seen_hashes.add(h)
                        batch.append((sym, spec, h, dup))
                    todo = [(sym, spec) for (sym, spec, h, dup) in batch if not dup]
                    futures = [pool.submit(evaluate, item) for item in todo]
                    results = []
                    for f in futures:
                        try:
                            results.append(f.result())
                        except Exception as exc:  # noqa: BLE001
                            results.append(exc)
                    ri = 0
                    for sym, spec, h, dup in batch:
                        if len(accepted) >= cfg.target_accepted:
                            break
                        attempt += 1
                        is_m: dict[str, Any] = {}
                        oos_m: dict[str, Any] = {}
                        reason = ""
                        status = "rejected"
                        result = None
                        corr_max = None
                        nearest = None
                        with st.lock:
                            st.attempts = attempt
                            st.funnel["generated"] += 1

                        if dup:
                            reason = "structural_duplicate"
                            with st.lock:
                                st.funnel["structural_duplicate"] += 1
                        else:
                            res = results[ri]
                            ri += 1
                            if isinstance(res, Exception):
                                reason = f"error:{type(res).__name__}: {res}"
                                with st.lock:
                                    st.funnel["error"] += 1
                            else:
                                result = res
                                is_m = result["metrics_is"]
                                oos_m = result["metrics_oos"]
                                ok_is, why_is = passes(is_m, cfg.acceptance.is_gates.model_dump())
                                if not ok_is:
                                    reason = f"is:{why_is}"
                                    with st.lock:
                                        st.funnel["is_fail"] += 1
                                else:
                                    ok_oos, why_oos = passes(oos_m, cfg.acceptance.oos_gates.model_dump())
                                    if not ok_oos:
                                        reason = f"oos:{why_oos}"
                                        with st.lock:
                                            st.funnel["oos_fail"] += 1
                                    elif is_m["trades"] == 0 or oos_m["trades"] == 0:
                                        reason = "no_trades"
                                        with st.lock:
                                            st.funnel["oos_fail"] += 1
                                    else:
                                        if cfg.autocorrelation.enabled and accepted:
                                            corr_max, nearest = self._correlation(
                                                result, accepted, cfg.autocorrelation.threshold,
                                                cfg.autocorrelation.method, cfg.risk.balance)
                                            if corr_max is not None and corr_max >= cfg.autocorrelation.threshold:
                                                reason = "correlation"
                                                with st.lock:
                                                    st.funnel["correlation"] += 1
                                        if not reason:
                                            sid = f"{st.run_id}-{len(accepted)+1:04d}"
                                            rets = self._returns(
                                                result["daily_eq"], result["split_day"],
                                                cfg.risk.balance)
                                            accepted.append({
                                                "id": sid,
                                                "day_ts": result["day_ts"][:result["split_day"]],
                                                "rets": rets,
                                                "symbol": sym,
                                            })
                                            persist_q.put((st, sid, spec, h, result,
                                                           corr_max, nearest))
                                            status = "accepted"
                                            with st.lock:
                                                st.accepted = len(accepted)
                                                st.funnel["accepted"] = len(accepted)
                                                st.recent.append({
                                                    "strategy_id": sid,
                                                    "symbol": sym,
                                                    "timeframe": cfg.timeframe,
                                                    "is_sharpe": is_m.get("sharpe"),
                                                    "oos_sharpe": oos_m.get("sharpe"),
                                                    "return_dd": oos_m.get("return_dd"),
                                                    "trades": oos_m.get("trades"),
                                                })

                        pending_attempts.append((
                            st.run_id, attempt, sym, cfg.timeframe, status, reason,
                            int(is_m.get("trades", 0)), int(oos_m.get("trades", 0)),
                            float(is_m.get("sharpe", 0)), float(oos_m.get("sharpe", 0)),
                            h, None,
                        ))
                        if len(pending_attempts) >= 25:
                            store.insert_attempts(pending_attempts)
                            pending_attempts = []
                        with st.lock:
                            st.current = {
                                "symbol": sym,
                                "is_sharpe": is_m.get("sharpe"),
                                "oos_sharpe": oos_m.get("sharpe"),
                                "reason": reason or None,
                            }
                    now = time.time()
                    st._last_persist = now
                    store.update_run(st.run_id, stats_json=json.dumps(st.stats()))
                    if st.stop.is_set():
                        break

            persist_q.put(None)
            persist_thread.join()
            if pending_attempts:
                store.insert_attempts(pending_attempts)
            # Bank the accepted strategies as an "initial generation" databank.
            try:
                if accepted:
                    import shutil
                    from .settings import bank_dir, result_dir
                    bank_id = "bank_" + uuid.uuid4().hex[:8]
                    store.create_bank(bank_id, f"Initial generation of {st.run_id}",
                                     st.run_id, None, cfg.model_dump(),
                                     group="initial_generation")
                    bank_dir(bank_id)
                    for a in accepted:
                        sid = a["id"]
                        rec = store.get_strategy(sid)
                        if not rec:
                            continue
                        src = result_dir(st.run_id)
                        tgt = bank_dir(bank_id)
                        for suffix in ("equity", "trades"):
                            sp = src / f"{sid}_{suffix}.parquet"
                            if sp.exists():
                                shutil.copy2(sp, tgt / f"{sid}_{suffix}.parquet")
                        spec = rec.get("spec")
                        if spec is not None:
                            (tgt / f"{sid}_spec.json").write_text(
                                json.dumps(spec, indent=2))
                        oos = rec.get("metrics_oos") or {}
                        store.add_bank_strategy(bank_id, {
                            "strategy_id": sid,
                            "source_run_id": st.run_id,
                            "spec": spec or {},
                            "symbol": rec.get("symbol"),
                            "timeframe": rec.get("timeframe"),
                            "direction": rec.get("direction"),
                            "metrics_is": rec.get("metrics_is", {}),
                            "metrics_oos": oos,
                            "metrics_all": {
                                "sharpe": {"median": oos.get("sharpe", 0),
                                           "p05": oos.get("sharpe", 0)},
                            },
                            "score": rec.get("score", 0.0),
                            "group": "initial_generation",
                        })
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
            final = "completed" if len(accepted) >= cfg.target_accepted else "stopped"
            with st.lock:
                st.status = final
                st.finished_at = time.time()
            store.update_run(st.run_id, status=final, finished_at=datetime.now(),
                             stats_json=json.dumps(st.stats()))
            if rt.clear_cache_after_run:
                clear_indicator_cache()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            with st.lock:
                st.status = "failed"
                st.error = f"{type(exc).__name__}: {exc}"
                st.finished_at = time.time()
            store.update_run(st.run_id, status="failed", finished_at=datetime.now(),
                             error=st.error, stats_json=json.dumps(st.stats()))
            try:
                from .runtime import load_runtime
                from .engine import clear_indicator_cache
                if load_runtime().clear_cache_after_run:
                    clear_indicator_cache()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _returns(daily_eq: np.ndarray, split_day: int, base: float) -> np.ndarray:
        eq = daily_eq[:split_day]
        if len(eq) == 0:
            return np.array([])
        aug = np.concatenate(([base], eq.astype(np.float64)))
        r = np.diff(aug) / np.where(aug[:-1] == 0, np.nan, aug[:-1])
        return r

    @staticmethod
    def _correlation(result: dict[str, Any], accepted: list[dict[str, Any]],
                     threshold: float, method: str, base: float) -> tuple[float | None, str | None]:
        rets = RunManager._returns(result["daily_eq"], result["split_day"], base)
        if not accepted:
            return None, None
        m = min(len(rets), min((len(a["rets"]) for a in accepted), default=len(rets)))
        if m < 30:
            return None, None
        x = np.ascontiguousarray(rets[:m], dtype=np.float64)
        mat = np.ascontiguousarray(
            np.array([a["rets"][:m] for a in accepted], dtype=np.float64))
        if _HAS_RUST_CORR:
            best, idx = _sqx_core.best_correlation(x, mat, method)
        else:
            best, idx = -2.0, -1
            for r, a in enumerate(accepted):
                y = mat[r]
                if np.std(y) == 0:
                    continue
                if method == "spearman":
                    c = float(pd.Series(x).corr(pd.Series(y), method="spearman"))
                else:
                    c = float(np.corrcoef(x, y)[0, 1])
                if np.isfinite(c) and c > best:
                    best, idx = c, r
        if idx < 0 or not np.isfinite(best):
            return None, None
        if best < -2.0 + 1e-12:
            return None, None
        return (best if best > -2.0 else None), accepted[idx]["id"]

    def _persist_strategy(self, st: RunState, sid: str, spec: dict[str, Any],
                          h: str, result: dict[str, Any], corr_max: float | None,
                          nearest: str | None) -> None:
        rdir = result_dir(st.run_id)
        split = result["split_day"]
        eq = pd.DataFrame({
            "ts": pd.to_datetime(result["day_ts"], unit="s"),
            "equity": result["daily_eq"],
            "segment": ["IS"] * split + ["OOS"] * (len(result["daily_eq"]) - split),
        })
        eq.to_parquet(rdir / f"{sid}_equity.parquet", index=False)
        pd.DataFrame(result["trades"]).to_parquet(rdir / f"{sid}_trades.parquet", index=False)
        (rdir / f"{sid}_spec.json").write_text(json.dumps(spec, indent=2))
        store.insert_strategy({
            "strategy_id": sid,
            "run_id": st.run_id,
            "spec": spec,
            "spec_hash": h,
            "symbol": spec["symbol"],
            "timeframe": spec["timeframe"],
            "direction": spec["direction"],
            "metrics_is": result["metrics_is"],
            "metrics_oos": result["metrics_oos"],
            "score": _score(result["metrics_is"], result["metrics_oos"]),
            "correlation_max": corr_max,
            "nearest_id": nearest,
            "trades": len(result["trades"]),
        })


manager = RunManager()
