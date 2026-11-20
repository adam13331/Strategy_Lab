from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .settings import DATA_DIR

SETTINGS_PATH = DATA_DIR / "runtime.json"


@dataclass
class RuntimeSettings:
    max_ram_mb: int = 0          # 0 = unlimited / auto
    performance: str = "balanced"  # low_memory | balanced | high_throughput
    workers: int = 0             # 0 = auto from performance profile
    clear_cache_after_run: bool = True

    def effective_workers(self) -> int:
        cpu = os.cpu_count() or 2
        if self.workers > 0:
            return max(1, self.workers)
        if self.performance == "low_memory":
            return max(1, min(2, cpu - 1))
        if self.performance == "high_throughput":
            return max(1, cpu)
        return max(1, min(cpu, max(2, cpu - 1)))

    def cache_budget_bytes(self) -> int:
        if self.max_ram_mb > 0:
            # Reserve most of the budget for contexts/bars; cap the indicator
            # cache at 35% of the user's limit (min 64 MB).
            return max(64, int(self.max_ram_mb * 0.35)) * 1024 * 1024
        if self.performance == "low_memory":
            return 256 * 1024 * 1024
        if self.performance == "high_throughput":
            return 2048 * 1024 * 1024
        return 768 * 1024 * 1024


def load_runtime() -> RuntimeSettings:
    try:
        if SETTINGS_PATH.exists():
            raw = json.loads(SETTINGS_PATH.read_text())
            return RuntimeSettings(**{k: raw[k] for k in RuntimeSettings.__dataclass_fields__ if k in raw})
    except Exception:  # noqa: BLE001
        pass
    return RuntimeSettings()


def save_runtime(settings: RuntimeSettings) -> RuntimeSettings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(asdict(settings), indent=2))
    return settings
