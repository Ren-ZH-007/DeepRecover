from contextlib import closing
from pathlib import Path
import re
import sqlite3
from core.device.ios.discovery import inspect_backup
from storage.models.candidate import Candidate

class BackupError(RuntimeError):
    pass

def read_manifest(path, token=None, limit=10000):
    """Read up to limit regular file entries; missing blobs remain visible as candidates.

    No file contents are decoded and no SQLite writes are permitted.
    Relative paths are display metadata only, never export destinations.
    """
    backup = inspect_backup(path)
    if backup.error or backup.encrypted is None:
        raise BackupError("备份元数据不可用，无法确认加密状态。")
    if backup.encrypted:
        raise BackupError("此备份已加密；当前工程版仅支持未加密备份索引。")
    if limit < 1:
        raise ValueError("limit must be positive")
    database = backup.path / "Manifest.db"
    if not database.is_file():
        raise BackupError("未找到 Manifest.db。")
    results = []
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            if token:
                db.set_progress_handler(lambda: _progress(token), 1000)
            for file_id, domain, relative_path, flags in db.execute(
                "SELECT fileID,domain,relativePath,flags FROM Files WHERE flags=1 LIMIT ?", (limit,)
            ):
                if token:
                    token.checkpoint()
                if not isinstance(file_id, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", file_id):
                    continue
                blob = backup.path / file_id[:2] / file_id
                if not blob.is_file():
                    blob = backup.path / file_id
                exists = blob.is_file() and blob.resolve().is_relative_to(backup.path)
                name = str(relative_path or file_id).replace("\\", "/").rsplit("/", 1)[-1]
                results.append(Candidate(source="ios_backup", name=name, file_type=Path(name).suffix.lower().lstrip("."),
                    size=blob.stat().st_size if exists else 0,
                    original_path=f"{domain}/{relative_path}", integrity="not_checked" if exists else "missing"))
    except sqlite3.Error as error:
        if token:
            token.checkpoint()
        raise BackupError("Manifest.db 无法读取或索引结构不受支持。") from error
    return results

def _progress(token):
    try:
        token.checkpoint()
        return 0
    except Exception:
        return 1
