from typing import Protocol, Iterator
from pathlib import Path
from core.tasks import CancellationToken
from storage.models.candidate import Candidate

class Scanner(Protocol):
    """Read-only candidates; offsets/lengths do not imply successful recovery."""
    def scan(self, source: Path, token: CancellationToken) -> Iterator[Candidate]: ...
