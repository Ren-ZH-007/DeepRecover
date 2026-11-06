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
    source_id: str | None = None
    local_path: str = ""
    acquired_at: str | None = None
    source_stat: dict | None = None
    # Logical file order; offsets always refer to the bound computer source.
    # Fragmented files and NTFS resident fixups can require several extents.
    extents: tuple[tuple[int, int], ...] | None = None
    file_system: str = ""
    deletion_state: str = "unknown"
    record_id: int | None = None
    structure_details: dict | None = None
    # Metadata modification time; basis is 'utc' or 'local' (zoneless wall clock).
    modified_at: str | None = None
    modified_basis: str | None = None

    def __post_init__(self):
        if self.size < 0 or (self.offset is not None and self.offset < 0):
            raise ValueError("Negative size or offset")
        if not 0 <= self.confidence <= 1:
            raise ValueError("Confidence must be between 0 and 1")
