from dataclasses import dataclass
from pathlib import Path
import os
import sys

@dataclass(frozen=True)
class AppConfig:
    data_dir: Path

    @classmethod
    def default(cls):
        override = os.environ.get("DEEPRECOVER_DATA_DIR")
        if override:
            return cls(Path(override).expanduser().resolve())
        if sys.platform == "darwin":
            return cls(Path.home() / "Library/Application Support/DeepRecover")
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        return cls(base / "DeepRecover_Data")

    @property
    def database(self):
        return self.data_dir / "index.db"

    def initialize(self):
        for name in ("Devices", "Backups", "Acquisitions", "ScanCache", "Recovered", "Reports", "Logs"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)
