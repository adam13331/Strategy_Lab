"""MetaTrader 5 integration.

Finds a local terminal installation (including Wine setups on Linux), checks the
MQL5 data folder and the pasted SQX indicators, exports strategy-bank strategies
as EAs, compiles them with MetaEditor and prepares strategy-tester runs with the
chosen symbol and timeframe.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import Counter
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .indicators import INDICATORS
from .mql5_ea import TF_ENUM, build_ea
from .store import store

TERMINAL_NAMES = ("terminal64.exe", "terminal.exe")
METAEDITOR_NAMES = ("metaeditor64.exe", "metaeditor.exe")

# Finer values of the ENUM_TIMEFRAMES members, for .set files.
TF_ENUM_VALUE = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H4": 16388, "D1": 16408, "W1": 32769, "MN1": 49153,
}

_LAST_LAUNCH: dict[str, Any] = {"proc": None, "strategy_id": None}


# --------------------------------------------------------------------- helpers
def _resolve(raw: str) -> Path:
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        raise ValueError(
            "Enter the MetaTrader 5 folder that contains terminal64.exe")
    p = Path(os.path.expandvars(os.path.expanduser(text)))
    try:
        p = p.resolve()
    except OSError:
        pass
    if p.is_file():
        p = p.parent
    return p


def _find_exe(folder: Path, names: tuple[str, ...]) -> Path | None:
    try:
        for entry in folder.iterdir():
            if entry.is_file() and entry.name.lower() in names:
                return entry
    except OSError:
        return None
    return None


def _mql5_candidates(root: Path) -> list[Path]:
    """Places MT5 may keep its MQL5 data folder (portable, Windows, Wine)."""
    out: list[Path] = []
    local = root / "MQL5"
    if local.is_dir():
        out.append(local)

    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata) / "MetaQuotes" / "Terminal"
        if base.is_dir():
            out.extend(sorted(base.glob("*/MQL5")))

    prefixes: list[Path] = []
    for parent in root.parents:
        if parent.name == "drive_c":
            prefixes.append(parent.parent)
            break
    wine = Path.home() / ".wine" / "drive_c"
    if wine.is_dir():
        prefixes.append(wine.parent)

    for prefix in prefixes:
        users = prefix / "drive_c" / "users"
        if not users.is_dir():
            continue
        for user in users.iterdir():
            base = user / "AppData" / "Roaming" / "MetaQuotes" / "Terminal"
            if base.is_dir():
                out.extend(sorted(base.glob("*/MQL5")))

    uniq: list[Path] = []
    seen: set[str] = set()
    for p in out:
        key = str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _pick_mql5(candidates: list[Path]) -> Path | None:
    if not candidates:
        return None

    def score(p: Path) -> tuple[int, int]:
        ind = p / "Indicators"
        files = len(list(ind.rglob("*.mq5"))) if ind.is_dir() else 0
        return (1 if ind.is_dir() else 0, files)

    return max(candidates, key=score)


def _scan_symbols(mql5: Path | None) -> list[str]:
    """Symbols the terminal knows about, from the downloaded history folders."""
    if mql5 is None:
        return []
    data = mql5.parent
    out: set[str] = set()
    for bases in (data / "bases", data / "Tester" / "bases"):
        if not bases.is_dir():
            continue
        for server in bases.iterdir():
            hist = server / "history"
            if not hist.is_dir():
                continue
            for entry in hist.iterdir():
                if entry.is_dir() and entry.name and not entry.name.startswith("."):
                    out.add(entry.name)
    return sorted(out)


def _scan_indicators(mql5: Path | None) -> dict[str, Any]:
    expected = sorted(INDICATORS)
    base = (mql5 / "Indicators") if mql5 else None
    found: dict[str, Path] = {}
    if base is not None and base.is_dir():
        for path in base.rglob("*.mq5"):
            stem = path.stem.lower()
            if stem in INDICATORS and stem not in found:
                found[stem] = path
    rel = []
    if base is not None:
        for path in found.values():
            parent = path.parent.relative_to(base)
            rel.append("" if str(parent) == "." else str(parent).replace("\\", "/"))
    folder = ""
    if rel:
        folder = Counter(rel).most_common(1)[0][0]
    return {
        "ok": bool(found),
        "path": str(base) if base else None,
        "folder": folder,
        "expected": len(expected),
        "found": len(found),
        "missing": [i for i in expected if i not in found],
    }


def check_terminal(raw_path: str) -> dict[str, Any]:
    """Inspect a terminal folder and everything needed to compile and test EAs."""
    folder = _resolve(raw_path)
    exists = folder.is_dir()
    notes: list[str] = []

    terminal = _find_exe(folder, TERMINAL_NAMES) if exists else None
    metaeditor = _find_exe(folder, METAEDITOR_NAMES) if exists else None
    candidates = _mql5_candidates(folder) if exists else []
    mql5 = _pick_mql5(candidates)

    if not exists:
        notes.append(f"Folder not found: {folder}")
    elif terminal is None:
        notes.append("terminal64.exe / terminal.exe is not in this folder")
    if exists and metaeditor is None:
        notes.append("MetaEditor is not next to the terminal; EAs cannot be compiled from here")
    if exists and mql5 is None:
        notes.append(
            "MQL5 data folder not found. Start the terminal once (or use portable "
            "mode with an MQL5 folder next to the exe) so it can be created.")

    indicators = _scan_indicators(mql5)
    experts = (mql5 / "Experts") if mql5 else None
    symbols = _scan_symbols(mql5)
    if indicators["ok"] and indicators["found"] < indicators["expected"]:
        notes.append(
            f"{len(indicators['missing'])} SQX indicators are missing from "
            f"{indicators['path']}; re-export and paste them again.")
    if exists and not symbols:
        notes.append(
            "No downloaded symbol history found, so no broker symbols could be "
            "detected. Type the tester symbol manually.")

    return {
        "path": str(folder),
        "exists": exists,
        "ok": bool(exists and terminal and mql5),
        "terminal": {"ok": bool(terminal), "path": str(terminal) if terminal else None},
        "metaeditor": {"ok": bool(metaeditor), "path": str(metaeditor) if metaeditor else None},
        "mql5": {
            "ok": bool(mql5),
            "path": str(mql5) if mql5 else None,
            "candidates": [str(c) for c in candidates],
        },
        "experts": {
            "ok": bool(experts),
            "path": str(experts) if experts else None,
            "target": str(experts / "SQX") if experts else None,
        },
        "indicators": indicators,
        "symbols": symbols,
        "notes": notes,
    }


# -------------------------------------------------------------- compile / run
def _win_path(p: Path) -> str:
    try:
        rp = p.resolve()
    except OSError:
        rp = p
    return "Z:" + str(rp).replace("/", "\\")


def _needs_win_paths(terminal_path: str | None) -> bool:
    return bool(terminal_path and Path(terminal_path).suffix.lower() == ".exe")


def _cfg_path(p: Path, windows: bool) -> str:
    return _win_path(p) if windows else str(p)


def _run_tool(exe: Path, args: list[str], timeout: int = 240) -> dict[str, Any]:
    if os.name == "nt" or exe.suffix.lower() != ".exe":
        cmd = [str(exe), *args]
    else:
        wine = shutil.which("wine64") or shutil.which("wine")
        if not wine:
            return {"ran": False, "returncode": None,
                    "error": "This is a Windows executable and Wine is not "
                             "installed, so it cannot be started from here."}
        cmd = [wine, str(exe), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=str(exe.parent))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ran": False, "returncode": None,
                "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ran": True,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-4000:],
        "error": None,
    }


def _read_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    # utf-16 is only trusted with a BOM; a BOM-less stream would decode to
    # garbage instead of falling through to the utf-8/cp1252 attempts.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _count(text: str, word: str) -> int | None:
    hits = re.findall(r"(\d+)\s+" + word, text, flags=re.IGNORECASE)
    return int(hits[-1]) if hits else None


def compile_ea(metaeditor_path: str | None, source: Path,
               mql5: Path) -> dict[str, Any]:
    if not metaeditor_path:
        return {"compiled": False, "errors": None, "warnings": None, "log": "",
                "error": "MetaEditor was not found next to the terminal"}
    exe = Path(metaeditor_path)
    log_path = source.with_suffix(".log")
    try:
        log_path.unlink()
    except OSError:
        pass
    windows = os.name == "nt" or exe.suffix.lower() == ".exe"
    args = [
        f"/compile:{_cfg_path(source, windows)}",
        f"/log:{_cfg_path(log_path, windows)}",
        f"/inc:{_cfg_path(mql5, windows)}",
    ]
    run = _run_tool(exe, args)
    text = _read_text(log_path) or ((run.get("stdout") or "")
                                    + (run.get("stderr") or ""))
    errors = _count(text, "error")
    warnings = _count(text, "warning")
    compiled = bool(run.get("ran")) and run.get("returncode") == 0 and errors in (0, None)
    return {
        "compiled": compiled,
        "errors": errors,
        "warnings": warnings,
        "log": text.strip()[-4000:],
        "error": run.get("error"),
    }


def _file_stem(strategy_id: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in strategy_id)
    return out or "sqx_strategy"


def export_strategies(raw_path: str, bank_id: str,
                      strategy_ids: list[str]) -> dict[str, Any]:
    """Write each selected bank strategy as an EA and compile it."""
    if not strategy_ids:
        raise ValueError("Select at least one strategy from the bank")
    chk = check_terminal(raw_path)
    if not chk["mql5"]["ok"] or not chk["experts"]["path"]:
        raise ValueError(
            "The MQL5 data folder was not found, so the EA cannot be installed. "
            "Start MetaTrader once and check the folder again.")

    mql5 = Path(chk["mql5"]["path"])
    experts_dir = Path(chk["experts"]["path"]) / "SQX"
    experts_dir.mkdir(parents=True, exist_ok=True)
    indicator_dir = chk["indicators"].get("folder") or ""
    metaeditor = chk["metaeditor"]["path"]

    results: list[dict[str, Any]] = []
    for sid in strategy_ids:
        rec = store.get_bank_strategy(bank_id, sid)
        if not rec:
            results.append({
                "strategy_id": sid, "file": None, "expert": None,
                "compiled": False, "errors": None, "warnings": None,
                "log": "", "error": "Strategy not found in this bank",
            })
            continue
        code = build_ea(rec.get("spec") or {}, sid, indicator_dir=indicator_dir)
        target = experts_dir / f"{_file_stem(sid)}.mq5"
        target.write_text(code)
        comp = compile_ea(metaeditor, target, mql5)
        results.append({
            "strategy_id": sid,
            "file": str(target),
            "expert": f"SQX\\{target.stem}",
            **comp,
        })

    return {"check": chk, "results": results}


# ---------------------------------------------------------- tester report
class _RowParser(HTMLParser):
    """Collect the text of every table row cell from an MT5 HTML report."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:  # noqa: ANN001
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _html_rows(text: str) -> list[list[str]]:
    parser = _RowParser()
    try:
        parser.feed(text)
    except Exception:  # noqa: BLE001
        pass
    return parser.rows


def _row_value(rows: list[list[str]], labels: list[str]) -> str | None:
    for row in rows:
        for i, cell in enumerate(row):
            norm = cell.strip().lower().rstrip(":").strip()
            if any(norm == lab or norm.startswith(lab) for lab in labels):
                return " ".join(row[i + 1:]) if i + 1 < len(row) else ""
    return None


def _to_float(raw: str) -> float:
    """Parse a report number, accepting space/comma thousands and comma decimals."""
    raw = raw.replace(" ", "").replace("\u00a0", "")
    if "," in raw and "." in raw:
        raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    return float(raw)


def _numbers(text: str | None) -> list[float]:
    if not text:
        return []
    out: list[float] = []
    for m in re.finditer(r"[-+]?\d[\d\s]*(?:[.,]\d+)?", text):
        try:
            out.append(_to_float(m.group(0)))
        except ValueError:
            continue
    return out


def _percent(text: str | None) -> float | None:
    if not text or "%" not in text:
        return None
    hits = re.findall(r"([-+]?\d[\d\s]*(?:[.,]\d+)?)\s*%", text)
    if not hits:
        return None
    try:
        return _to_float(hits[-1])
    except ValueError:
        return None


def parse_tester_report(path: Path) -> dict[str, Any]:
    """Extract the metrics SQX compares against from an MT5 tester report."""
    rows = _html_rows(_read_text(path))
    trades_txt = _row_value(rows, ["total trades", "total trade"])
    sharpe_txt = _row_value(rows, ["sharpe ratio", "sharpe"])
    profit_txt = _row_value(rows, ["total net profit", "net profit"])
    deposit_txt = _row_value(rows, ["initial deposit", "deposit"])
    equity_dd = _row_value(rows, ["equity drawdown maximal"])
    balance_dd = _row_value(rows, ["balance drawdown maximal"])
    pf_txt = _row_value(rows, ["profit factor"])

    trades = _numbers(trades_txt)
    deposit = (_numbers(deposit_txt) or [None])[0]
    profit = (_numbers(profit_txt) or [None])[0]
    dd_pct = _percent(equity_dd) if _percent(equity_dd) is not None else _percent(balance_dd)
    net_return = None
    if profit is not None and deposit:
        net_return = profit / deposit * 100
    return_dd = None
    if net_return is not None and dd_pct:
        return_dd = net_return / dd_pct
    return {
        "report": str(path),
        "trades": int(trades[0]) if trades else None,
        "sharpe": (_numbers(sharpe_txt) or [None])[0],
        "net_profit": profit,
        "initial_deposit": deposit,
        "net_return": round(net_return, 4) if net_return is not None else None,
        "max_dd": dd_pct,
        "return_dd": round(return_dd, 4) if return_dd is not None else None,
        "profit_factor": (_numbers(pf_txt) or [None])[0],
    }


def report_path(mql5: Path, strategy_id: str) -> Path:
    return mql5.parent / "SQX_reports" / f"{_file_stem(strategy_id)}.htm"


def _find_report(mql5: Path, strategy_id: str) -> Path | None:
    stem = _file_stem(strategy_id)
    for ext in (".htm", ".html"):
        p = mql5.parent / "SQX_reports" / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def _similarity(sqx: float | None, mt5: float | None) -> tuple[float | None, float | None]:
    """(mt5/sqx ratio, closeness in 0..1) for one metric."""
    if sqx in (None, 0) or mt5 is None:
        return None, None
    ratio = mt5 / sqx
    if ratio <= 0:
        return round(ratio, 4), 0.0
    return round(ratio, 4), round(min(ratio, 1.0 / ratio), 4)


def compare_with_sqx(raw_path: str, bank_id: str, strategy_id: str,
                     threshold: float = 0.7) -> dict[str, Any]:
    """Compare the last MT5 tester report with the strategy's SQX metrics."""
    chk = check_terminal(raw_path)
    if not chk["mql5"]["ok"]:
        raise ValueError("The MQL5 data folder was not found; check the folder first")
    rec = store.get_bank_strategy(bank_id, strategy_id)
    if not rec:
        raise ValueError("Strategy not found in this bank")
    mql5 = Path(chk["mql5"]["path"])

    found = _find_report(mql5, strategy_id)
    mt5 = parse_tester_report(found) if found else None
    report = found or report_path(mql5, strategy_id)
    if mt5 is None:
        return {
            "ready": False,
            "report": str(report),
            "message": "No MT5 tester report yet. Run the strategy in the tester "
                       "and it will appear here when the test finishes.",
            "baseline": "oos",
            "rows": [],
            "similar_count": 0,
            "compared": 0,
            "threshold": threshold,
            "mt5": {},
        }

    oos = rec.get("metrics_oos") or {}
    baseline = "oos" if (oos.get("trades") or 0) > 0 else "is"
    sqx = oos if baseline == "oos" else (rec.get("metrics_is") or {})

    rows: list[dict[str, Any]] = []
    for key, label in (
        ("trades", "Trades"),
        ("sharpe", "Sharpe ratio"),
        ("net_return", "Net return %"),
        ("return_dd", "Return / drawdown"),
    ):
        a = sqx.get(key)
        b = mt5.get(key)
        ratio, similarity = _similarity(
            float(a) if a is not None else None,
            float(b) if b is not None else None)
        rows.append({
            "key": key,
            "label": label,
            "sqx": a,
            "mt5": b,
            "ratio": ratio,
            "similarity": similarity,
            "within": bool(similarity is not None and similarity >= threshold),
        })
    compared = [r for r in rows if r["similarity"] is not None]
    return {
        "ready": True,
        "report": str(report),
        "message": None,
        "baseline": baseline,
        "rows": rows,
        "similar_count": sum(1 for r in compared if r["within"]),
        "compared": len(compared),
        "threshold": threshold,
        "mt5": mt5,
    }


def _mt5_date(value: str, default: date) -> str:
    text = (value or "").strip()
    if not text:
        d = default
    else:
        try:
            d = datetime.strptime(text.replace(".", "-"), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Invalid date: {value}") from exc
    return d.strftime("%Y.%m.%d")


def _launch_terminal(terminal_path: str | None, ini_path: Path,
                     windows: bool, strategy_id: str) -> dict[str, Any]:
    if not terminal_path:
        return {"launched": False, "pid": None,
                "note": "Terminal executable not found in the folder"}
    prior = _LAST_LAUNCH.get("proc")
    if prior is not None and prior.poll() is None:
        return {"launched": False, "pid": prior.pid,
                "note": "A terminal started from here is still running. Close it "
                        "before launching another tester run."}
    exe = Path(terminal_path)
    arg = "/config:" + _cfg_path(ini_path, windows)
    if os.name == "nt" or exe.suffix.lower() != ".exe":
        cmd = [str(exe), arg]
    else:
        wine = shutil.which("wine64") or shutil.which("wine")
        if not wine:
            return {"launched": False, "pid": None,
                    "note": "This is a Windows terminal and Wine is not installed, "
                            "so it cannot be started from here. Open the generated "
                            "config in MetaTrader manually."}
        cmd = [wine, str(exe), arg]
    try:
        proc = subprocess.Popen(cmd, cwd=str(exe.parent),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        return {"launched": False, "pid": None,
                "note": f"{type(exc).__name__}: {exc}"}
    _LAST_LAUNCH["proc"] = proc
    _LAST_LAUNCH["strategy_id"] = strategy_id
    return {"launched": True, "pid": proc.pid,
            "note": "Terminal started with the tester config. The report is "
                    "written next to the config file when the run finishes."}


def run_in_tester(raw_path: str, bank_id: str, strategy_id: str, symbol: str,
                  from_date: str = "", to_date: str = "",
                  deposit: float = 10000, leverage: str = "1:30",
                  model: int = 1, launch: bool = True) -> dict[str, Any]:
    """Prepare (and optionally start) a strategy-tester run for one strategy.

    The timeframe always comes from the strategy itself; only the symbol is a
    user choice (normally picked from the terminal's known symbols).
    """
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("Choose the symbol to test on")
    model = int(model)
    if model not in (0, 1, 2, 3, 4):
        raise ValueError(
            "Backtest quality must be 0 (every tick), 1 (1 minute OHLC), "
            "2 (open prices), 3 (math) or 4 (real ticks)")

    chk = check_terminal(raw_path)
    if not chk["mql5"]["ok"]:
        raise ValueError("The MQL5 data folder was not found; check the folder first")
    rec = store.get_bank_strategy(bank_id, strategy_id)
    if not rec:
        raise ValueError("Strategy not found in this bank")
    spec = rec.get("spec") or {}
    timeframe = (rec.get("timeframe") or spec.get("timeframe") or "").strip().upper()
    if timeframe not in TF_ENUM:
        raise ValueError(
            f"Strategy {strategy_id} has no usable timeframe, so it cannot be tested")

    mql5 = Path(chk["mql5"]["path"])
    stem = _file_stem(strategy_id)
    experts_dir = mql5 / "Experts" / "SQX"
    source = experts_dir / f"{stem}.mq5"
    if not source.exists():
        experts_dir.mkdir(parents=True, exist_ok=True)
        source.write_text(build_ea(spec, strategy_id,
                                   indicator_dir=chk["indicators"].get("folder") or ""))

    recompiled = None
    ex5 = source.with_suffix(".ex5")
    if not ex5.exists() and chk["metaeditor"]["path"]:
        recompiled = compile_ea(chk["metaeditor"]["path"], source, mql5)
    if not ex5.exists():
        detail = ""
        if recompiled:
            detail = recompiled.get("error") or (recompiled.get("log") or "")[-400:]
        raise ValueError(
            f"{stem}.ex5 is missing" + (f": {detail}" if detail else
            ". Export and compile the expert first, or check MetaEditor."))

    windows = _needs_win_paths(chk["terminal"]["path"])
    data_dir = mql5.parent
    tester_dir = mql5 / "Profiles" / "Tester"
    tester_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = data_dir / "SQX_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = data_dir / "SQX_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # MT5 resolves a relative ExpertParameters name under MQL5/Profiles/Tester
    # and a relative Report name under the terminal data folder.
    set_path = tester_dir / f"{stem}.set"
    set_path.write_text(
        f"InpSymbol={symbol}\n"
        f"InpTimeframe={TF_ENUM_VALUE[timeframe]}\n"
    )

    start = _mt5_date(from_date, date(2015, 1, 1))
    end = _mt5_date(to_date, date.today())
    report = reports_dir / f"{stem}.htm"
    for ext in (".htm", ".html", ".xml"):
        try:
            (reports_dir / f"{stem}{ext}").unlink()
        except OSError:
            pass
    report_name = (f"SQX_reports\\{stem}" if windows else f"SQX_reports/{stem}")
    ini_path = runs_dir / f"{stem}_tester.ini"
    lines = [
        "[Tester]",
        f"Expert=SQX\\{stem}",
        f"ExpertParameters={stem}.set",
        f"Symbol={symbol}",
        f"Period={timeframe}",
        f"Model={int(model)}",
        "ExecutionMode=0",
        "Optimization=0",
        f"FromDate={start}",
        f"ToDate={end}",
        "ForwardMode=0",
        f"Deposit={float(deposit):.0f}",
        "Currency=USD",
        f"Leverage={leverage}",
        "Visual=0",
        f"Report={report_name}",
        "ReplaceReport=1",
        "ShutdownTerminal=1",
    ]
    ini_path.write_text("\n".join(lines) + "\n")

    launched = {"launched": False, "pid": None, "note": None}
    if launch:
        launched = _launch_terminal(chk["terminal"]["path"], ini_path, windows,
                                    strategy_id)

    return {
        "strategy_id": strategy_id,
        "expert": f"SQX\\{stem}",
        "symbol": symbol,
        "symbol_known": symbol in chk.get("symbols", []),
        "timeframe": timeframe,
        "model": model,
        "from": start,
        "to": end,
        "config": str(ini_path),
        "set": str(set_path),
        "report": str(report),
        "report_ready": report.exists(),
        "compiled": bool(ex5.exists()),
        "recompiled": recompiled,
        **launched,
    }
