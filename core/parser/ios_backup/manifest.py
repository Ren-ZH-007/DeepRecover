from contextlib import closing
from pathlib import Path
import plistlib
import re
import sqlite3
from core.device.ios.discovery import inspect_backup
from storage.models.candidate import Candidate
from core.candidate_io import snapshot
from core.file_times import unix_modified

MAX_FILE_INFO = 64 * 1024

class BackupError(RuntimeError):
    pass

def read_manifest(path, token=None, limit=10000):
    return list(iter_manifest(path, token, limit))


def iter_manifest(path, token=None, limit=None):
    """Read up to limit regular file entries; missing blobs remain visible as candidates.

    No file contents are decoded and no SQLite writes are permitted.
    Relative paths are display metadata only, never export destinations.
    """
    backup = inspect_backup(path)
    if backup.error or backup.encrypted is None:
        raise BackupError("备份元数据不可用，无法确认加密状态。")
    if backup.encrypted:
        raise BackupError("此备份已加密；当前工程版仅支持未加密备份索引。")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    database = backup.path / "Manifest.db"
    if not database.is_file():
        raise BackupError("未找到 Manifest.db。")
    try:
        canonical = database.resolve(strict=True)
        if not canonical.is_relative_to(backup.path):
            raise BackupError('Manifest.db 指向源备份目录以外，已拒绝读取。')
        _require_stable_index(database, canonical)
        initial = snapshot(canonical.stat())
        # Plain mode=ro still creates/updates WAL shared-memory sidecars.
        # immutable avoids journal/locking writes; refuse pending journals
        # rather than silently ignoring data that exists only in those files.
        with closing(sqlite3.connect(canonical.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            if token:
                db.set_progress_handler(lambda: _progress(token), 1000)
            columns = {row[1] for row in db.execute("PRAGMA table_info(Files)")}
            metadata = "file" if "file" in columns else "NULL"
            for file_id, domain, relative_path, flags, file_info in db.execute(
                f"SELECT fileID,domain,relativePath,flags,{metadata} FROM Files WHERE flags=1 LIMIT ?", (limit if limit is not None else -1,)
            ):
                if token:
                    token.checkpoint()
                if not isinstance(file_id, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", file_id):
                    continue
                blob = backup.path / file_id[:2] / file_id
                if not blob.is_file():
                    blob = backup.path / file_id
                exists = blob.is_file() and blob.resolve().is_relative_to(backup.path)
                info = blob.stat() if exists else None
                name = str(relative_path or file_id).replace("\\", "/").rsplit("/", 1)[-1]
                yield Candidate(source="ios_backup", name=name, file_type=Path(name).suffix.lower().lstrip("."),
                    size=info.st_size if info else 0,
                    original_path=f"{domain}/{relative_path}", local_path=str(blob) if exists else "",
                    source_stat=snapshot(info) if info else None,
                    integrity="not_checked" if exists else "missing", **_last_modified(file_info))
        _require_stable_index(database, canonical)
        if database.resolve(strict=True) != canonical or snapshot(canonical.stat()) != initial:
            raise BackupError('Manifest.db 在读取期间已变化，索引结果不完整，请使用稳定备份重试。')
    except sqlite3.Error as error:
        if token:
            token.checkpoint()
        raise BackupError("Manifest.db 无法读取或索引结构不受支持。") from error
    except OSError as error:
        raise BackupError('备份索引路径不可用或在读取期间变化，请检查稳定备份。') from error


def _last_modified(blob):
    """LastModified from a Files.file NSKeyedArchiver blob; binary plists only."""
    if not isinstance(blob, bytes) or not blob.startswith(b"bplist00") or len(blob) > MAX_FILE_INFO:
        return {}
    try:
        archive = plistlib.loads(blob, fmt=plistlib.FMT_BINARY)
    except Exception:
        return {}
    objects = archive.get("$objects") if isinstance(archive, dict) else None
    if not isinstance(objects, list):
        return {}
    for item in objects[:64]:
        if isinstance(item, dict) and "LastModified" in item:
            return unix_modified(item["LastModified"])
    return {}


def _require_stable_index(database, canonical):
    for base in {database, canonical}:
        for suffix in ('-wal', '-journal'):
            sidecar = Path(str(base) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise BackupError('备份包含未合并的 WAL/事务日志，已拒绝索引；请在备份工具中完成备份后重试。')

def _progress(token):
    try:
        token.checkpoint()
        return 0
    except Exception:
        return 1
