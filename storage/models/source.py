from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


@dataclass(frozen=True)
class DataSource:
    """Private source locators stay in the local index, never in logs."""
    kind: str
    locator: str
    label: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self):
        if self.kind not in {"android", "ios_backup"} or not self.locator:
            raise ValueError("Invalid data source")
