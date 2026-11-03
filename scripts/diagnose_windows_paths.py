"""Read-only stat comparison on synthetic temporary files; no environment dump."""
import json
import os
from pathlib import Path
import platform
import tempfile


def fields(info):
    return {name: getattr(info, 'st_' + name, None) for name in
            ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns', 'birthtime_ns')}


def main():
    with tempfile.TemporaryDirectory(prefix='DeepRecover-path-probe-') as directory:
        source = Path(directory) / 'synthetic # unicode 数据.bin'
        source.write_bytes(b'only synthetic probe bytes')
        resolved = source.resolve()
        observations = {'python': platform.python_version(), 'platform': platform.system(),
                        'lexical_path': str(source), 'resolved_path': str(resolved),
                        'lexical_equal': source == resolved, 'same_file': source.samefile(resolved),
                        'path_stat_before': fields(source.stat())}
        with resolved.open('rb') as first, resolved.open('rb') as second:
            observations['first_fstat'] = fields(os.fstat(first.fileno()))
            observations['second_fstat'] = fields(os.fstat(second.fileno()))
            observations['resolved_stat'] = fields(resolved.stat())
            assert first.read() == b'only synthetic probe bytes'
            observations['first_fstat_after_read'] = fields(os.fstat(first.fileno()))
            observations['resolved_stat_after_read'] = fields(resolved.stat())
        observations['path_stat_after_close'] = fields(source.stat())
        initial = source.stat()
        os.utime(source, ns=(initial.st_atime_ns, initial.st_mtime_ns + 1_000_000_000))
        with resolved.open('rb') as updated:
            observations['path_after_metadata_change'] = fields(source.stat())
            observations['handle_after_metadata_change'] = fields(os.fstat(updated.fileno()))
        print(json.dumps(observations, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
