"""Start the packaged PE executable and verify its actual Qt/SQLite smoke report."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def validate_report(path, expected_version):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    expected = {
        'success': True,
        'version': expected_version,
        'schema_version': 3,
        'navigation_pages': 8,
        'verified_exports': 2,
        'decoded_formats': ['PNG', 'JPEG'],
        'computer_candidates': 2,
        'computer_verified_exports': 2,
        'workspace_dialog_verified': True,
        'workspace_roundtrip_verified': True,
        'environment_diagnostics_verified': True,
        'video_scan_verified': True,
        'video_scan_exports': 1,
        'video_preview_verified': True,
        'video_muted': True,
        'video_buffer_released': True,
        'video_frame_dimensions': [96, 64],
    }
    for name, value in expected.items():
        if type(data.get(name)) is not type(value) or data.get(name) != value:
            raise RuntimeError(f'Executable smoke report mismatch: {name}')
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    executable = args.executable.resolve(strict=True)
    with executable.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise RuntimeError('Build output is not a Windows PE executable')
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    if report.exists():
        raise RuntimeError('Smoke report must not pre-exist; use a fresh build directory')
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    result = subprocess.run(
        [str(executable), '--smoke-test', '--smoke-report', str(report)],
        cwd=executable.parent, env=env, capture_output=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f'Packaged executable exited with status {result.returncode}')
    validate_report(report, args.version)
    print('PASS: packaged Windows executable, Qt navigation, SQLite schema, PNG/JPEG decode, 2 verified exports, clean exit')


if __name__ == '__main__':
    main()
