"""Background orchestration for the sequential five-stage stress pipeline."""

from __future__ import annotations

import json
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from .engine import Context, load_context
from .settings import bank_dir, m1_path, result_dir, tf_path
from .store import store
from .stress import (
    STAGES, StressConfig, run_pipeline,
)


class StressState:
    def __init__(self, stress_id: str, config: StressConfig) -> None:
        self.stress_id = stress_id
        self.config = config
        self.status = "queued"
        self.strategies_total = 0
        self.strategies_done = 0
        self.survivors = 0
        self.tests_requested = 0
        self.tests_run = 0
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.error: str | None = None
        self.bank_id: str | None = None
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.current: dict[str, Any] | None = None
        self.recent: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            elapsed = (self.finished_at or time.time()) - self.started_at
            return {
                "stress_id": self.stress_id,
                "source_run_id": self.config.source_run_id,
                "status": self.status,
                "strategies_total": self.strategies_total,
                "strategies_done": self.strategies_done,
                "survivors": self.survivors,
                "tests_requested": self.tests_requested,
                "tests_run": self.tests_run,
                "stages_requested": len(self.config.enabled_stages()),
                "elapsed": round(elapsed, 1),
                "throughput": round(self.tests_run / elapsed, 2)
                if elapsed > 0 else 0,
                "current": self.current,
                "recent": list(self.recent[-20:]),
                "bank_id": self.bank_id,
                "error": self.error,
                "started_at": datetime.fromtimestamp(
                    self.started_at).astimezone().isoformat(),
            }

    def stats(self) -> dict[str, Any]:
        s = self.snapshot()
        s.pop("recent", None)
        return s


def planned_tests(cfg: StressConfig, alternates: int) -> int:
    total = 0
    if cfg.parameter.enabled:
        total += cfg.parameter.rounds
    if cfg.price_cost.enabled:
        total += cfg.price_cost.rounds * 2
    if cfg.shuffle.enabled:
        total += cfg.shuffle.rounds
    if cfg.monte_carlo.enabled:
        total += cfg.monte_carlo.rounds
    if cfg.cross_market.enabled:
        total += alternates
    return total


class StressManager:
    def __init__(self) -> None:
        self.active: dict[str, StressState] = {}
        self.lock = threading.Lock()

    def get(self, stress_id: str) -> StressState | None:
        return self.active.get(stress_id)

    def start(self, config: StressConfig, name: str | None = None,
              clear_banks: bool = False) -> dict[str, Any]:
        run = store.get_run(config.source_run_id)
        if not run:
            raise ValueError("Source run not found")
        rows = store.list_strategies(config.source_run_id, limit=5000)
        if config.strategy_ids:
            wanted = set(config.strategy_ids)
            rows = [r for r in rows if r["strategy_id"] in wanted]
        if not rows:
            raise ValueError("The source run has no accepted strategies to test")
        if not config.enabled_stages():
            raise ValueError("Enable at least one pipeline stage")

        configured = config.cross_market.alternate_symbols
        alternate_count = (len(configured) if configured else
                           max(0, len(store.symbols()) - 1))
        if clear_banks:
            store.delete_banks(group="stress_testing",
                               source_run_id=config.source_run_id)
        stress_id = uuid.uuid4().hex[:12]
        stress_name = name or f"Stress pipeline {config.source_run_id}"
        store.create_stress(stress_id, stress_name, config.source_run_id,
                            config.model_dump())
        st = StressState(stress_id, config)
        st.strategies_total = len(rows)
        st.tests_requested = len(rows) * planned_tests(config, alternate_count)
        with self.lock:
            self.active[stress_id] = st
        threading.Thread(target=self._run, args=(st, rows), daemon=True,
                         name=f"stress-{stress_id}").start()
        return {"stress_id": stress_id, "name": stress_name,
                "strategies": len(rows),
                "tests_requested": st.tests_requested}

    def stop(self, stress_id: str) -> bool:
        st = self.active.get(stress_id)
        if not st:
            return False
        st.stop.set()
        with st.lock:
            st.status = "stopping"
            if st.current:
                st.current = {**st.current, "stopping": True}
        return True

    # ------------------------------------------------------------------ worker
    def _alternates(self, cfg: StressConfig, primary: str, timeframe: str,
                    oos_pct: int, session: dict[str, Any],
                    cache: dict[tuple[str, str], Context]
                    ) -> list[tuple[str, Context]]:
        wanted = cfg.cross_market.alternate_symbols
        if wanted:
            symbols = list(wanted)
        else:
            symbols = [s["symbol"] for s in store.symbols()
                       if s["symbol"] != primary]
        out: list[tuple[str, Context]] = []
        for symbol in symbols:
            if symbol == primary:
                continue
            path = m1_path(symbol) if timeframe == "M1" else tf_path(
                symbol, timeframe)
            if not path.exists():
                continue
            key = (symbol, timeframe)
            if key not in cache:
                cache[key] = load_context(symbol, timeframe, oos_pct, session)
            out.append((symbol, cache[key]))
        return out

    def _run(self, st: StressState, rows: list[dict[str, Any]]) -> None:
        cfg = st.config
        store.update_stress(st.stress_id, status="running",
                            started_at=datetime.now())
        with st.lock:
            st.status = "running"
        try:
            source = store.get_run(cfg.source_run_id) or {}
            src_cfg = source.get("config") or {}
            timeframe = src_cfg.get("timeframe", "H4")
            oos_pct = int(src_cfg.get("oos_pct", 40))
            session = src_cfg.get("session") or {}
            contexts: dict[tuple[str, str], Context] = {}
            from .runtime import load_runtime
            workers = load_runtime().effective_workers()
            bank_id = None
            survivors = 0

            with ThreadPoolExecutor(max_workers=workers) as pool:
                for index, row in enumerate(rows, start=1):
                    if st.stop.is_set():
                        break
                    sid = row["strategy_id"]
                    spec_rec = store.get_strategy(sid)
                    if not spec_rec:
                        continue
                    spec = spec_rec.get("spec") or {}
                    symbol = row.get("symbol") or spec.get("symbol", "")
                    key = (symbol, timeframe)
                    if key not in contexts:
                        contexts[key] = load_context(
                            symbol, timeframe, oos_pct, session)
                    ctx = contexts[key]
                    alternates = self._alternates(
                        cfg, symbol, timeframe, oos_pct, session, contexts)

                    with st.lock:
                        st.current = {
                            "strategy_id": sid, "index": index,
                            "stage": None, "stage_index": 0,
                            "stage_tests_run": 0,
                            "stage_tests_requested": 0,
                            "stages_done": 0,
                            "tests_requested": planned_tests(
                                cfg, len(alternates)),
                        }

                    def make_progress(strategy_id: str):
                        def progress(stage_key: str, n: int) -> None:
                            with st.lock:
                                st.tests_run += int(n)
                                if (st.current
                                        and st.current.get("strategy_id")
                                        == strategy_id):
                                    cur = dict(st.current)
                                    if cur.get("stage") != stage_key:
                                        cur["stage"] = stage_key
                                        cur["stage_index"] = (
                                            STAGES.index(stage_key) + 1)
                                        cur["stage_tests_run"] = 0
                                        cur["stage_tests_requested"] = 0
                                    cur["stage_tests_run"] = int(
                                        cur.get("stage_tests_run", 0)) + int(n)
                                    st.current = cur
                        return progress

                    result = run_pipeline(
                        spec, ctx, cfg, alternates=alternates, pool=pool,
                        progress=make_progress(sid), stop_event=st.stop)
                    stages = result.get("stages", {})
                    survival = bool(result.get("survival"))
                    total_run = int(result.get("tests_run", 0))
                    record = {
                        "stress_id": st.stress_id,
                        "strategy_id": sid,
                        "survival": survival,
                        "stages": stages,
                        "tests_requested": planned_tests(cfg, len(alternates)),
                        "tests_run": total_run,
                        "pass_rate": result.get("pass_rate", 0.0),
                        "metrics": result.get("metrics", {}),
                    }
                    store.insert_stress_result(record)

                    if survival:
                        survivors += 1
                        if bank_id is None:
                            bank_id = self._create_bank(st, cfg)
                        self._bank_strategy(bank_id, row, spec, stages,
                                            result.get("metrics", {}))

                    with st.lock:
                        st.strategies_done = index
                        st.survivors = survivors
                        st.recent.append({
                            "strategy_id": sid,
                            "survival": survival,
                            "pass_rate": record["pass_rate"],
                            "tests_run": total_run,
                            "is_sharpe": (row.get("metrics_is") or {}).get(
                                "sharpe"),
                            "oos_sharpe": (row.get("metrics_oos") or {}).get(
                                "sharpe"),
                            "stages": {
                                k: {"status": v.get("status"),
                                    "passed": v.get("passed")}
                                for k, v in stages.items()
                            },
                        })
                        st.current = None
                    store.update_stress(
                        st.stress_id, stats_json=json.dumps(st.stats()),
                        bank_id=bank_id)

            final = "completed" if not st.stop.is_set() else "stopped"
            if (not st.stop.is_set()
                    and st.strategies_done < st.strategies_total):
                final = "stopped"
            with st.lock:
                st.status = final
                st.finished_at = time.time()
                st.current = None
            store.update_stress(st.stress_id, status=final,
                                finished_at=datetime.now(),
                                stats_json=json.dumps(st.stats()),
                                bank_id=bank_id)
            self._clear_cache_if_configured()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            with st.lock:
                st.status = "failed"
                st.error = f"{type(exc).__name__}: {exc}"
                st.finished_at = time.time()
            store.update_stress(st.stress_id, status="failed",
                                finished_at=datetime.now(), error=st.error,
                                stats_json=json.dumps(st.stats()))

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _clear_cache_if_configured() -> None:
        try:
            from .runtime import load_runtime
            from .engine import clear_indicator_cache
            if load_runtime().clear_cache_after_run:
                clear_indicator_cache()
        except Exception:  # noqa: BLE001
            pass

    def _create_bank(self, st: StressState, cfg: StressConfig) -> str:
        bank_id = "bank_" + uuid.uuid4().hex[:8]
        name = cfg.bank_name or f"Stress survivors of {cfg.source_run_id}"
        store.create_bank(bank_id, name, cfg.source_run_id, st.stress_id,
                          cfg.model_dump(), group="stress_testing")
        bank_dir(bank_id)
        with st.lock:
            st.bank_id = bank_id
        return bank_id

    def _bank_strategy(self, bank_id: str, row: dict[str, Any],
                       spec: dict[str, Any], stages: dict[str, Any],
                       metrics: dict[str, Any]) -> None:
        sid = row["strategy_id"]
        source = result_dir(row["run_id"])
        target = bank_dir(bank_id)
        for suffix in ("equity", "trades"):
            src = source / f"{sid}_{suffix}.parquet"
            if src.exists():
                shutil.copy2(src, target / f"{sid}_{suffix}.parquet")
        (target / f"{sid}_spec.json").write_text(json.dumps(spec, indent=2))
        store.add_bank_strategy(bank_id, {
            "strategy_id": sid,
            "source_run_id": row["run_id"],
            "spec": spec,
            "symbol": row.get("symbol") or spec.get("symbol"),
            "timeframe": row.get("timeframe") or spec.get("timeframe"),
            "direction": row.get("direction") or spec.get("direction"),
            "metrics_is": row.get("metrics_is", {}),
            "metrics_oos": row.get("metrics_oos", {}),
            "metrics_all": metrics,
            "score": row.get("score", 0.0),
        })


stress_manager = StressManager()
