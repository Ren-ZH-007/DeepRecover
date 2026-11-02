from typing import Protocol, Iterator
from pathlib import Path
from core.tasks import CancellationToken
from storage.models.candidate import Candidate

class Scanner(Protocol):
    """Reserved interface; signature carving is not implemented in v0.1.0."""
    def scan(self, source: Path, token: CancellationToken) -> Iterator[Candidate]: ...
