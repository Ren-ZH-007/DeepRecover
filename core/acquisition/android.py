"""User-selected ordinary-file copying. This is not deleted-data recovery."""
import hashlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import tempfile

from core.device.android.adapter import AdbError
from storage.models.candidate import Candidate
from core.file_times import unix_modified
from core.candidate_io import snapshot


def acquire_files(adapter, serial, paths, destination, token):
    # Validate the entire selection before any copying. Remote names never form
    # local destinations, including on Windows (reserved names, drives, etc.).
    paths = list(dict.fromkeys(adapter.shared_path(path) for path in paths))
    if not paths:
        raise AdbError("请先选择要采集的普通文件。")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for remote in paths:
        token.checkpoint()
        # Each file has an exclusive staging directory. Failed or cancelled
        # pulls leave no partial file that could be mistaken for a result.
        with tempfile.TemporaryDirectory(prefix='pull-', dir=destination) as staging:
            copied = Path(staging) / 'content'
            adapter.pull_file(serial, remote, copied, token)
            # Only after pull_file has authorised and validated the target.
            lookup = getattr(adapter, 'modified_time', None)
            modified = unix_modified(lookup(serial, remote)) if lookup else {}
            if copied.is_symlink() or not copied.is_file():
                raise AdbError("设备未返回普通文件，采集已停止。")
            digest = hashlib.sha256()
            with copied.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    token.checkpoint()
                    digest.update(chunk)
            token.checkpoint()
            # Retain the local bytes under a generated name; never overwrite.
            with tempfile.NamedTemporaryFile(prefix='file-', dir=destination, delete=False) as target:
                final = Path(target.name)
            try:
                copied.replace(final)
            except BaseException:
                final.unlink(missing_ok=True)
                raise
        name = PurePosixPath(remote).name
        yield Candidate(source='android_file', name=name,
                        file_type=PurePosixPath(name).suffix.lower().lstrip('.'),
                        size=final.stat().st_size, original_path=remote,
                        local_path=str(final.resolve()), sha256=digest.hexdigest(),
                        acquired_at=datetime.now(timezone.utc).isoformat(),
                        source_stat=snapshot(final.stat()),
                        integrity='not_checked', **modified)
