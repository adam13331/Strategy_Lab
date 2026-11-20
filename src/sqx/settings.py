from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PARQUET_DIR = DATA_DIR / "parquet"
CACHE_DIR = DATA_DIR / "cache"
INDICATOR_CACHE_DIR = CACHE_DIR / "indicators"
RESULTS_DIR = DATA_DIR / "results"
DB_PATH = DATA_DIR / "sqx.duckdb"
WEB_DIST = ROOT / "web" / "dist"

TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"]

TF_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
    "MN1": 2592000,
}

TF_PANDAS = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1D",
    "W1": "1W-MON",
    "MN1": "1MS",
}


def ensure_dirs() -> None:
    for p in (RAW_DIR, PARQUET_DIR, CACHE_DIR, INDICATOR_CACHE_DIR, RESULTS_DIR):
        p.mkdir(parents=True, exist_ok=True)


def m1_path(symbol: str) -> Path:
    return PARQUET_DIR / symbol / "m1.parquet"


def tf_path(symbol: str, timeframe: str) -> Path:
    return PARQUET_DIR / symbol / f"{timeframe.lower()}.parquet"


def result_dir(run_id: str) -> Path:
    p = RESULTS_DIR / run_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def bank_dir(bank_id: str) -> Path:
    p = RESULTS_DIR / "banks" / bank_id
    p.mkdir(parents=True, exist_ok=True)
    return p
