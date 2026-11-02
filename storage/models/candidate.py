from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

class CandidateStatus(StrEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    PARTIAL = "partial"
    EXPORTED = "exported"
    FAILED = "failed"

@dataclass(frozen=True)
class Candidate:
    source: str
    name: str
    file_type: str
    size: int
    original_path: str = ""
    offset: int | None = None
    status: CandidateStatus = CandidateStatus.CANDIDATE
    integrity: str = "unknown"
    confidence: float = 0.0
    sha256: str | None = None
    is_thumbnail: bool = False
    is_cache: bool = False
    is_duplicate: bool = False
    id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self):
        if self.size < 0 or (self.offset is not None and self.offset < 0):
            raise ValueError("Negative size or offset")
        if not 0 <= self.confidence <= 1:
            raise ValueError("Confidence must be between 0 and 1")
