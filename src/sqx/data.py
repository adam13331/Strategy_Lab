from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from .settings import RAW_DIR, m1_path, tf_path
from .store import store

SYMBOL_RE = re.compile(r"^([A-Za-z0-9._]{3,20})-")


def detect_symbol(path: Path, override: str | None = None) -> str:
    if override:
        return override.upper().strip()
    m = SYMBOL_RE.match(path.name)
    if not m:
        raise ValueError(
            "Could not detect symbol from filename. Rename to SYMBOL-M1-...csv "
            "or pass a symbol explicitly."
        )
    return m.group(1).upper()


def list_csv_files() -> dict[str, Any]:
    """Scan the raw-data folder and report each CSV plus its detected symbol."""
    imported = {
        str(Path(s["source_file"]).expanduser().resolve()): s
        for s in store.symbols()
        if s.get("source_file")
    }
    imported_by_name: dict[str, dict[str, Any]] = {}
    for rec in imported.values():
        imported_by_name.setdefault(Path(rec["source_file"]).name, rec)
    files = []
    for path in sorted(RAW_DIR.glob("*.csv"), key=lambda p: p.name.lower()):
        try:
            symbol, error = detect_symbol(path), None
        except ValueError as exc:
            symbol, error = None, str(exc)
        rec = imported.get(str(path.resolve())) or imported_by_name.get(path.name)
        stat = path.stat()
        files.append({
            "name": path.name,
            "path": str(path),
            "symbol": symbol,
            "error": error,
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "imported": rec is not None,
            "imported_rows": rec["rows"] if rec else None,
            "imported_at": rec["imported_at"] if rec else None,
        })
    return {"folder": str(RAW_DIR), "files": files}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def import_csv(path: str | Path, symbol: str | None = None) -> dict[str, Any]:
    """Import a Date,Time,OHLCV CSV into a normalized M1 Parquet file."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        candidate = RAW_DIR / path
        path = candidate if candidate.exists() else path
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    sym = detect_symbol(path, symbol)
    out = m1_path(sym)
    out.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    src = str(path).replace("'", "''")
    tmp = str(out.with_suffix(".tmp.parquet")).replace("'", "''")

    con.execute(
        f"""
        CREATE TEMP TABLE raw AS
        SELECT strptime(Date || ' ' || Time, '%Y%m%d %H:%M:%S') AS ts,
               CAST(Open AS DOUBLE)  AS open,
               CAST(High AS DOUBLE)  AS high,
               CAST(Low AS DOUBLE)   AS low,
               CAST(Close AS DOUBLE) AS close,
               CAST(Volume AS DOUBLE) AS volume
        FROM read_csv('{src}', header=true,
            columns={{
                'Date':'VARCHAR','Time':'VARCHAR','Open':'DOUBLE',
                'High':'DOUBLE','Low':'DOUBLE','Close':'DOUBLE','Volume':'DOUBLE'
            }})
        WHERE Date IS NOT NULL AND Time IS NOT NULL
        """
    )
    rows = con.execute("SELECT count(*) FROM raw").fetchone()[0]
    bad = con.execute(
        """SELECT count(*) FROM raw WHERE ts IS NULL OR open IS NULL
           OR high IS NULL OR low IS NULL OR close IS NULL
           OR high < low OR high < greatest(open, close)
           OR low > least(open, close)"""
    ).fetchone()[0]
    con.execute(
        """CREATE TEMP TABLE clean AS
           SELECT ts, open, high, low, close, coalesce(volume, 0) AS volume
           FROM (SELECT *, row_number() OVER (PARTITION BY ts ORDER BY ts) AS rn
                 FROM raw
                 WHERE ts IS NOT NULL AND open IS NOT NULL AND high IS NOT NULL
                   AND low IS NOT NULL AND close IS NOT NULL
                   AND high >= low AND high >= greatest(open, close)
                   AND low <= least(open, close))
           WHERE rn = 1
           ORDER BY ts"""
    )
    clean_rows = con.execute("SELECT count(*) FROM clean").fetchone()[0]
    dupes = rows - clean_rows - bad
    con.execute(f"COPY clean TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    con.close()

    # Atomic-ish replace
    if out.exists():
        out.unlink()
    Path(tmp).replace(out)

    quality = quality_report(out)
    quality["source_rows"] = int(rows)
    quality["invalid_rows"] = int(bad)
    quality["duplicate_rows"] = int(dupes)

    store.upsert_symbol(
        symbol=sym,
        source_file=str(path),
        rows=quality["rows"],
        start_ts=quality["start"],
        end_ts=quality["end"],
        tz_assumed="UTC",
        sha256=sha256_file(path),
        quality=quality,
    )
    # old resamples are now stale
    for tf in ("M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"):
        p = tf_path(sym, tf)
        if p.exists():
            p.unlink()
    return {"symbol": sym, "path": str(out), **quality}


def quality_report(path: Path) -> dict[str, Any]:
    con = duckdb.connect()
    src = str(path).replace("'", "''")
    row = con.execute(
        f"""SELECT count(*) AS rows, min(ts) AS start_ts, max(ts) AS end_ts,
                   count(*) - count(DISTINCT ts) AS duplicate_rows
            FROM read_parquet('{src}')"""
    ).fetchone()
    rows, start, end, dupes = row
    invalid = con.execute(
        f"""SELECT count(*) FROM read_parquet('{src}')
            WHERE high < low OR open > high OR open < low
               OR close > high OR close < low OR volume < 0"""
    ).fetchone()[0]
    years = con.execute(
        f"""SELECT year(ts) AS y, count(*) AS n FROM read_parquet('{src}')
            GROUP BY 1 ORDER BY 1"""
    ).fetchall()
    weekday = con.execute(
        f"""SELECT dayofweek(ts) AS dow, count(*) AS n FROM read_parquet('{src}')
            GROUP BY 1 ORDER BY 1"""
    ).fetchall()
    ts = con.execute(
        f"SELECT epoch(ts)::BIGINT FROM read_parquet('{src}') ORDER BY ts"
    ).fetchnumpy()["CAST(epoch(ts) AS BIGINT)"]
    con.close()

    diffs = np.diff(ts)
    gaps = diffs[diffs > 60]
    top_gaps = sorted((int(g) for g in gaps), reverse=True)[:10]
    quality = {
        "rows": int(rows),
        "start": str(start),
        "end": str(end),
        "duplicate_rows": int(dupes),
        "invalid_rows": int(invalid),
        "years": {str(y): int(n) for y, n in years},
        "weekday_counts": {str(d): int(n) for d, n in weekday},
        "gaps_over_1min": int((diffs > 60).sum()),
        "gaps_over_1day": int((diffs > 86400).sum()),
        "largest_gaps_seconds": top_gaps,
        "inferred_tz": "UTC (week opens Sunday 22:00, closes Friday 22:00)",
    }
    return quality


def resample(symbol: str, timeframes: list[str], anchor: str = "UTC") -> list[dict[str, Any]]:
    import pandas as pd

    from .settings import TF_PANDAS

    src = m1_path(symbol)
    if not src.exists():
        raise FileNotFoundError(f"No M1 data for {symbol}. Import a CSV first.")
    df = pd.read_parquet(src)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts").sort_index()
    out = []
    for tf in timeframes:
        tf = tf.upper()
        if tf == "M1":
            path = src
            rows = len(df)
            start, end = df.index[0], df.index[-1]
        else:
            rule = TF_PANDAS[tf]
            agg = df.resample(rule, label="left", closed="left", origin="start_day").agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
            ).dropna()
            if len(agg):
                # drop a trailing bucket that is not complete yet
                dur = (agg.index[-1] + pd.tseries.frequencies.to_offset(rule)) - agg.index[-1]
                if agg.index[-1] + dur > df.index[-1] + pd.Timedelta(minutes=1):
                    agg = agg.iloc[:-1]
            agg = agg.reset_index()
            path = tf_path(symbol, tf)
            path.parent.mkdir(parents=True, exist_ok=True)
            agg.to_parquet(path, index=False, compression="zstd")
            rows = len(agg)
            start, end = (agg["ts"].iloc[0], agg["ts"].iloc[-1]) if rows else (None, None)
        store.set_bar_cache(symbol, tf, anchor, int(rows), start, end, str(path))
        out.append({"symbol": symbol, "timeframe": tf, "rows": int(rows),
                    "start": str(start), "end": str(end), "path": str(path)})
    return out
