from dataclasses import dataclass
from pathlib import Path
import os

@dataclass(frozen=True)
class AppConfig:
    data_dir: Path

    @classmethod
    def default(cls):
        override = os.environ.get("DEEPRECOVER_DATA_DIR")
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        return cls(Path(override).expanduser().resolve() if override else base / "DeepRecover_Data")

    @property
    def database(self):
        return self.data_dir / "index.db"

    def initialize(self):
        for name in ("Devices", "Backups", "ScanCache", "Recovered", "Reports", "Logs"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)
