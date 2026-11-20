"""DuckDB metadata store. One writer connection guarded by a lock.

The API and the run thread share this store. Workers are not used in phase 1;
the generation thread calls into the backtester directly, which keeps DuckDB's
single-writer model intact.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import duckdb

from .settings import DB_PATH, ensure_dirs

def _json(value: Any, default: Any = None) -> Any:
    """Decode a JSON column that may be NULL/NaN when the DataFrame has gaps."""
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except ValueError:
            return default if default is not None else {}
    return default if default is not None else {}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    symbol VARCHAR PRIMARY KEY,
    source_file VARCHAR,
    rows BIGINT,
    start_ts TIMESTAMP,
    end_ts TIMESTAMP,
    tz_assumed VARCHAR,
    sha256 VARCHAR,
    imported_at TIMESTAMP,
    quality_json VARCHAR
);
CREATE TABLE IF NOT EXISTS bar_cache (
    symbol VARCHAR,
    timeframe VARCHAR,
    anchor VARCHAR,
    rows BIGINT,
    start_ts TIMESTAMP,
    end_ts TIMESTAMP,
    path VARCHAR,
    built_at TIMESTAMP,
    PRIMARY KEY (symbol, timeframe, anchor)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY,
    name VARCHAR,
    config_json VARCHAR,
    seed BIGINT,
    status VARCHAR,
    created_at TIMESTAMP,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    stats_json VARCHAR,
    error VARCHAR
);
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id VARCHAR PRIMARY KEY,
    run_id VARCHAR,
    spec_json VARCHAR,
    spec_hash VARCHAR,
    symbol VARCHAR,
    timeframe VARCHAR,
    direction VARCHAR,
    metrics_is_json VARCHAR,
    metrics_oos_json VARCHAR,
    score DOUBLE,
    correlation_max DOUBLE,
    nearest_id VARCHAR,
    trades INTEGER,
    created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS attempts (
    run_id VARCHAR,
    attempt BIGINT,
    symbol VARCHAR,
    timeframe VARCHAR,
    status VARCHAR,
    reason VARCHAR,
    is_trades INTEGER,
    oos_trades INTEGER,
    is_sharpe DOUBLE,
    oos_sharpe DOUBLE,
    spec_hash VARCHAR,
    spec_json VARCHAR,
    created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS stress_runs (
    stress_id VARCHAR PRIMARY KEY,
    name VARCHAR,
    source_run_id VARCHAR,
    config_json VARCHAR,
    status VARCHAR,
    created_at TIMESTAMP,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    stats_json VARCHAR,
    bank_id VARCHAR,
    error VARCHAR
);
CREATE TABLE IF NOT EXISTS stress_results (
    stress_id VARCHAR,
    strategy_id VARCHAR,
    survival BOOLEAN,
    stages_json VARCHAR,
    tests_requested INTEGER,
    tests_run INTEGER,
    pass_rate DOUBLE,
    metrics_json VARCHAR,
    created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS banks (
    bank_id VARCHAR PRIMARY KEY,
    name VARCHAR,
    source_run_id VARCHAR,
    rob_id VARCHAR,
    config_json VARCHAR,
    bank_group VARCHAR,
    count INTEGER,
    created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bank_strategies (
    bank_id VARCHAR,
    strategy_id VARCHAR,
    source_run_id VARCHAR,
    spec_json VARCHAR,
    symbol VARCHAR,
    timeframe VARCHAR,
    direction VARCHAR,
    metrics_is_json VARCHAR,
    metrics_oos_json VARCHAR,
    metrics_all_json VARCHAR,
    score DOUBLE,
    created_at TIMESTAMP,
    PRIMARY KEY (bank_id, strategy_id)
);
"""


class Store:
    def __init__(self, path=DB_PATH) -> None:
        ensure_dirs()
        self._lock = threading.RLock()
        self.conn = duckdb.connect(str(path))
        with self._lock:
            self.conn.execute(_SCHEMA)
            # A previous process may have died mid-run. Those runs can never
            # resume, so surface them as interrupted instead of "running".
            self.conn.execute(
                """UPDATE runs SET status = 'interrupted', finished_at = now()
                   WHERE status IN ('queued', 'running', 'stopping')"""
            )
            self.conn.execute(
                """UPDATE stress_runs SET status = 'interrupted', finished_at = now()
                   WHERE status IN ('queued', 'running', 'stopping')"""
            )
            try:
                self.conn.execute(
                    "ALTER TABLE banks ADD COLUMN IF NOT EXISTS bank_group VARCHAR")
            except Exception:  # noqa: BLE001
                pass
            try:
                self.conn.execute(
                    "ALTER TABLE bank_strategies ADD COLUMN IF NOT EXISTS bank_group VARCHAR")
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ symbols
    def upsert_symbol(self, **kw: Any) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO symbols
                   (symbol, source_file, rows, start_ts, end_ts, tz_assumed,
                    sha256, imported_at, quality_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, now(), ?)""",
                [
                    kw["symbol"], kw["source_file"], kw["rows"], kw["start_ts"],
                    kw["end_ts"], kw.get("tz_assumed", "UTC"), kw.get("sha256"),
                    json.dumps(kw.get("quality", {})),
                ],
            )

    def symbols(self) -> list[dict[str, Any]]:
        with self._lock:
            df = self.conn.execute(
                "SELECT symbol, source_file, rows, start_ts, end_ts, tz_assumed,"
                " imported_at, quality_json FROM symbols ORDER BY symbol"
            ).df()
        out = []
        for r in df.to_dict("records"):
            r["start_ts"] = str(r["start_ts"])
            r["end_ts"] = str(r["end_ts"])
            r["imported_at"] = str(r["imported_at"])
            r["quality"] = _json(r.pop("quality_json"))
            out.append(r)
        return out

    def symbol(self, symbol: str) -> dict[str, Any] | None:
        for s in self.symbols():
            if s["symbol"] == symbol:
                return s
        return None

    # --------------------------------------------------------------- bar cache
    def set_bar_cache(self, symbol: str, timeframe: str, anchor: str, rows: int,
                      start_ts, end_ts, path: str) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO bar_cache
                   (symbol, timeframe, anchor, rows, start_ts, end_ts, path, built_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, now())""",
                [symbol, timeframe, anchor, rows, start_ts, end_ts, path],
            )

    def bar_caches(self, symbol: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM bar_cache"
        args: list[Any] = []
        if symbol:
            q += " WHERE symbol = ?"
            args.append(symbol)
        with self._lock:
            df = self.conn.execute(q + " ORDER BY symbol, timeframe", args).df()
        return df.to_dict("records")

    # --------------------------------------------------------------------- runs
    def create_run(self, run_id: str, name: str, config: dict[str, Any],
                   seed: int) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO runs (run_id, name, config_json, seed, status,
                   created_at) VALUES (?, ?, ?, ?, 'queued', now())""",
                [run_id, name, json.dumps(config), seed],
            )

    def update_run(self, run_id: str, **kw: Any) -> None:
        sets, args = [], []
        for key in ("status", "started_at", "finished_at", "stats_json", "error"):
            if key in kw:
                sets.append(f"{key} = ?")
                args.append(kw[key])
        if not sets:
            return
        args.append(run_id)
        with self._lock:
            self.conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id = ?", args)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            df = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", [run_id]).df()
        if df.empty:
            return None
        r = df.to_dict("records")[0]
        r["config"] = _json(r.pop("config_json"))
        r["stats"] = _json(r.pop("stats_json"))
        for k in ("created_at", "started_at", "finished_at"):
            if r.get(k) is not None:
                r[k] = str(r[k])
        return r

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            df = self.conn.execute(
                "SELECT run_id, name, seed, status, created_at, started_at,"
                " finished_at, stats_json, error FROM runs ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).df()
        out = []
        for r in df.to_dict("records"):
            r["stats"] = _json(r.pop("stats_json"))
            for k in ("created_at", "started_at", "finished_at"):
                if r.get(k) is not None:
                    r[k] = str(r[k])
            out.append(r)
        return out

    # ------------------------------------------------------------------ results
    def insert_attempts(self, rows: list[tuple]) -> None:
        if not rows:
            return
        with self._lock:
            self.conn.executemany(
                """INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())""",
                rows,
            )

    def insert_strategy(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO strategies
                   (strategy_id, run_id, spec_json, spec_hash, symbol, timeframe,
                    direction, metrics_is_json, metrics_oos_json, score,
                    correlation_max, nearest_id, trades, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())""",
                [
                    row["strategy_id"], row["run_id"], json.dumps(row["spec"]),
                    row["spec_hash"], row["symbol"], row["timeframe"],
                    row["direction"], json.dumps(row["metrics_is"]),
                    json.dumps(row["metrics_oos"]), row["score"],
                    row.get("correlation_max"), row.get("nearest_id"),
                    row["trades"],
                ],
            )

    def list_strategies(self, run_id: str | None = None, limit: int = 500,
                        offset: int = 0) -> list[dict[str, Any]]:
        where, args = "", []
        if run_id:
            where = "WHERE run_id = ?"
            args.append(run_id)
        with self._lock:
            df = self.conn.execute(
                f"""SELECT strategy_id, run_id, spec_hash, symbol, timeframe,
                    direction, metrics_is_json, metrics_oos_json, score,
                    correlation_max, nearest_id, trades, created_at
                    FROM strategies {where}
                    ORDER BY score DESC NULLS LAST
                    LIMIT ? OFFSET ?""",
                args + [limit, offset],
            ).df()
        out = []
        for r in df.to_dict("records"):
            r["metrics_is"] = _json(r.pop("metrics_is_json"))
            r["metrics_oos"] = _json(r.pop("metrics_oos_json"))
            r["created_at"] = str(r["created_at"])
            out.append(r)
        return out

    def count_strategies(self, run_id: str | None = None) -> int:
        where, args = "", []
        if run_id:
            where, args = "WHERE run_id = ?", [run_id]
        with self._lock:
            return int(self.conn.execute(
                f"SELECT count(*) FROM strategies {where}", args
            ).fetchone()[0])

    # -------------------------------------------------------- stress pipeline
    def create_stress(self, stress_id: str, name: str, source_run_id: str,
                      config: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO stress_runs
                   (stress_id, name, source_run_id, config_json, status, created_at)
                   VALUES (?, ?, ?, ?, 'queued', now())""",
                [stress_id, name, source_run_id, json.dumps(config)],
            )

    def update_stress(self, stress_id: str, **kw: Any) -> None:
        sets, args = [], []
        for key in ("status", "started_at", "finished_at", "stats_json",
                    "bank_id", "error"):
            if key in kw:
                sets.append(f"{key} = ?")
                args.append(kw[key])
        if not sets:
            return
        args.append(stress_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE stress_runs SET {', '.join(sets)} WHERE stress_id = ?",
                args)

    @staticmethod
    def _stress_row(r: dict[str, Any]) -> dict[str, Any]:
        r["config"] = _json(r.pop("config_json", None))
        r["stats"] = _json(r.pop("stats_json", None))
        for k in ("created_at", "started_at", "finished_at"):
            if r.get(k) is not None:
                r[k] = str(r[k])
        return r

    def get_stress(self, stress_id: str) -> dict[str, Any] | None:
        with self._lock:
            df = self.conn.execute(
                "SELECT * FROM stress_runs WHERE stress_id = ?",
                [stress_id]).df()
        if df.empty:
            return None
        return self._stress_row(df.to_dict("records")[0])

    def list_stress(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            df = self.conn.execute(
                """SELECT stress_id, name, source_run_id, status, created_at,
                   started_at, finished_at, stats_json, bank_id, error
                   FROM stress_runs ORDER BY created_at DESC LIMIT ?""",
                [limit]).df()
        return [self._stress_row(r) for r in df.to_dict("records")]

    def insert_stress_result(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO stress_results
                   (stress_id, strategy_id, survival, stages_json,
                    tests_requested, tests_run, pass_rate, metrics_json,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())""",
                [row["stress_id"], row["strategy_id"], row["survival"],
                 json.dumps(row.get("stages", {})),
                 row.get("tests_requested", 0), row.get("tests_run", 0),
                 row.get("pass_rate", 0.0),
                 json.dumps(row.get("metrics", {}))],
            )

    def list_stress_results(self, stress_id: str) -> list[dict[str, Any]]:
        with self._lock:
            df = self.conn.execute(
                """SELECT * FROM stress_results WHERE stress_id = ?
                   ORDER BY survival DESC, pass_rate DESC""",
                [stress_id]).df()
        out = []
        for r in df.to_dict("records"):
            r["stages"] = _json(r.pop("stages_json"))
            r["metrics"] = _json(r.pop("metrics_json"))
            r["created_at"] = str(r["created_at"])
            out.append(r)
        return out

    # ------------------------------------------------------------------ banks
    def create_bank(self, bank_id: str, name: str, source_run_id: str,
                    rob_id: str | None = None, config: dict[str, Any] | None = None,
                    group: str | None = None, bank_type: str | None = None,
                    category: str | None = None, kind: str | None = None,
                    **kw: Any) -> None:
        if config is None:
            config = {}
        g = (group or bank_type or category or kind
             or kw.get("group") or kw.get("bank_type")
             or kw.get("category") or kw.get("kind")
             or kw.get("bank_group") or kw.get("type"))
        if isinstance(g, str):
            g = g.strip().lower().replace(" ", "_").replace("-", "_")
            if g in ("initial", "generation", "initial_gen"):
                g = "initial_generation"
            if g in ("stress", "testing", "stress_test"):
                g = "stress_testing"
        if not g:
            g = "stress_testing" if rob_id else "initial_generation"
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO banks
                   (bank_id, name, source_run_id, rob_id, config_json,
                    bank_group, count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, now())""",
                [bank_id, name, source_run_id, rob_id, json.dumps(config), g],
            )

    def add_bank_strategy(self, bank_id: str, row: dict[str, Any]) -> None:
        g = (row.get("group") or row.get("bank_group")
             or row.get("bank_type") or row.get("category")
             or row.get("kind"))
        if isinstance(g, str):
            g = g.strip().lower().replace(" ", "_").replace("-", "_")
            if g in ("initial", "generation", "initial_gen"):
                g = "initial_generation"
            if g in ("stress", "testing", "stress_test"):
                g = "stress_testing"
        if not g:
            try:
                with self._lock:
                    df = self.conn.execute(
                        "SELECT bank_group FROM banks WHERE bank_id = ?",
                        [bank_id]).df()
                if not df.empty:
                    g = df.to_dict("records")[0].get("bank_group")
            except Exception:  # noqa: BLE001
                g = None
        if not g:
            g = "stress_testing"
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO bank_strategies
                   (bank_id, strategy_id, source_run_id, spec_json, symbol,
                    timeframe, direction, metrics_is_json, metrics_oos_json,
                    metrics_all_json, score, bank_group, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())""",
                [bank_id, row["strategy_id"], row["source_run_id"],
                 json.dumps(row["spec"]), row["symbol"], row["timeframe"],
                 row["direction"], json.dumps(row.get("metrics_is", {})),
                 json.dumps(row.get("metrics_oos", {})),
                 json.dumps(row.get("metrics_all", {})),
                 row.get("score", 0.0), g],
            )
            self.conn.execute(
                "UPDATE banks SET count = (SELECT count(*) FROM bank_strategies"
                " WHERE bank_id = ?) WHERE bank_id = ?", [bank_id, bank_id])

    def ensure_hall_of_fame(self, name: str = "Hall of fame") -> str:
        banks = self.list_banks(group="hall_of_fame")
        if banks:
            return banks[0]["bank_id"]
        bank_id = "bank_hof"
        self.create_bank(bank_id, name, None, None, {}, group="hall_of_fame")
        return bank_id

    def delete_bank(self, bank_id: str) -> bool:
        with self._lock:
            self.conn.execute(
                "DELETE FROM bank_strategies WHERE bank_id = ?", [bank_id])
            self.conn.execute("DELETE FROM banks WHERE bank_id = ?", [bank_id])
        return True

    def delete_banks(self, group: str | None = None, bank_type: str | None = None,
                     category: str | None = None, kind: str | None = None,
                     bank_group: str | None = None, type: str | None = None,
                     source_run_id: str | None = None, **kw: Any) -> int:
        g = (group or bank_type or category or kind or bank_group or type
             or kw.get("group") or kw.get("bank_type") or kw.get("category")
             or kw.get("kind") or kw.get("bank_group") or kw.get("type"))
        if isinstance(g, str):
            g = g.strip().lower().replace(" ", "_").replace("-", "_")
        with self._lock:
            where = "1=1"
            args: list[Any] = []
            if g:
                where += " AND bank_group = ?"
                args.append(g)
            if source_run_id:
                where += " AND source_run_id = ?"
                args.append(source_run_id)
            ids = [r[0] for r in self.conn.execute(
                f"SELECT bank_id FROM banks WHERE {where}", args).fetchall()]
            for bid in ids:
                self.conn.execute(
                    "DELETE FROM bank_strategies WHERE bank_id = ?", [bid])
            self.conn.execute(f"DELETE FROM banks WHERE {where}", args)
        return len(ids)

    def clear_banks(self, group: str | None = None, **kw: Any) -> int:
        return self.delete_banks(group=group, **kw)

    def list_banks(self, group: str | None = None, bank_type: str | None = None,
                   category: str | None = None, kind: str | None = None,
                   bank_group: str | None = None, type: str | None = None,
                   limit: int | None = None, offset: int = 0,
                   **kw: Any) -> list[dict[str, Any]]:
        g = (group or bank_type or category or kind or bank_group or type
             or kw.get("group") or kw.get("bank_type") or kw.get("category")
             or kw.get("kind") or kw.get("bank_group") or kw.get("type"))
        if isinstance(g, str):
            g = g.strip().lower().replace(" ", "_").replace("-", "_")
            if g in ("initial", "generation", "initial_gen"):
                g = "initial_generation"
            if g in ("stress", "testing", "stress_test"):
                g = "stress_testing"
        with self._lock:
            where = ""
            args: list[Any] = []
            if g:
                where = "WHERE bank_group = ?"
                args.append(g)
            sql = (f"""SELECT bank_id, name, source_run_id, rob_id, bank_group,
                       count, created_at FROM banks {where}
                       ORDER BY created_at DESC""")
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                args = args + [int(limit), int(offset)]
            df = self.conn.execute(sql, args).df()
        out = []
        for r in df.to_dict("records"):
            r["created_at"] = str(r["created_at"])
            grp = r.get("bank_group")
            # expose the group under several common aliases
            r["group"] = grp
            r["bank_type"] = grp
            r["category"] = grp
            r["kind"] = grp
            out.append(r)
        return out

    def get_bank(self, bank_id: str) -> dict[str, Any] | None:
        with self._lock:
            df = self.conn.execute(
                "SELECT * FROM banks WHERE bank_id = ?", [bank_id]).df()
        if df.empty:
            return None
        r = df.to_dict("records")[0]
        r["config"] = _json(r.pop("config_json", None))
        r["created_at"] = str(r["created_at"])
        grp = r.get("bank_group")
        r["group"] = grp
        r["bank_type"] = grp
        r["category"] = grp
        r["kind"] = grp
        return r

    def list_bank_strategies(self, bank_id: str, limit: int = 500,
                             offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            df = self.conn.execute(
                """SELECT bank_id, strategy_id, source_run_id, symbol, timeframe,
                   direction, metrics_is_json, metrics_oos_json, metrics_all_json,
                   score, bank_group, created_at FROM bank_strategies
                   WHERE bank_id = ? ORDER BY score DESC NULLS LAST
                   LIMIT ? OFFSET ?""", [bank_id, limit, offset]).df()
        out = []
        for r in df.to_dict("records"):
            r["metrics_is"] = _json(r.pop("metrics_is_json"))
            r["metrics_oos"] = _json(r.pop("metrics_oos_json"))
            r["metrics_all"] = _json(r.pop("metrics_all_json"))
            r.pop("robustness_json", None)
            r.pop("robustness", None)
            grp = r.get("bank_group")
            r["group"] = grp
            r["bank_type"] = grp
            r["category"] = grp
            r["kind"] = grp
            r["created_at"] = str(r["created_at"])
            out.append(r)
        return out

    def get_bank_strategy(self, bank_id: str,
                          strategy_id: str) -> dict[str, Any] | None:
        with self._lock:
            df = self.conn.execute(
                "SELECT * FROM bank_strategies WHERE bank_id = ? AND strategy_id = ?",
                [bank_id, strategy_id]).df()
        if df.empty:
            return None
        r = df.to_dict("records")[0]
        r["spec"] = _json(r.pop("spec_json"))
        r["metrics_is"] = _json(r.pop("metrics_is_json"))
        r["metrics_oos"] = _json(r.pop("metrics_oos_json"))
        r["metrics_all"] = _json(r.pop("metrics_all_json"))
        r.pop("robustness_json", None)
        r.pop("robustness", None)
        grp = r.get("bank_group")
        r["group"] = grp
        r["bank_type"] = grp
        r["category"] = grp
        r["kind"] = grp
        r["created_at"] = str(r["created_at"])
        return r

    def get_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        with self._lock:
            df = self.conn.execute(
                "SELECT * FROM strategies WHERE strategy_id = ?", [strategy_id]
            ).df()
        if df.empty:
            return None
        r = df.to_dict("records")[0]
        r["spec"] = _json(r.pop("spec_json"))
        r["metrics_is"] = _json(r.pop("metrics_is_json"))
        r["metrics_oos"] = _json(r.pop("metrics_oos_json"))
        r["created_at"] = str(r["created_at"])
        return r


store = Store()
