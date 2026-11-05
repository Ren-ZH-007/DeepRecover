"""Verify a native .app and execute isolated offscreen/Cocoa file-pipeline smoke."""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import struct
import subprocess
import tempfile
import zlib


BUNDLE_ID = 'com.deeprecover.workbench'
MINIMUM_OS = '13.0'
MACH_O_MAGIC = {b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
                b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'}


def validate_report(path, expected_version, qt_platform):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise RuntimeError('Packaged macOS smoke report must be a JSON object')
    expected = {'success': True, 'version': expected_version, 'schema_version': 3,
                'navigation_pages': 8, 'verified_exports': 2,
                'decoded_formats': ['PNG', 'JPEG'], 'qt_platform': qt_platform}
    expected.update(computer_candidates=2, computer_verified_exports=2)
    expected.update(workspace_dialog_verified=True, workspace_roundtrip_verified=True)
    expected.update(environment_diagnostics_verified=True)
    expected.update(video_scan_verified=True, video_scan_exports=1)
    expected.update(video_preview_verified=True, video_muted=True,
                    video_buffer_released=True, video_frame_dimensions=[96, 64])
    if qt_platform == 'cocoa':
        expected['native_quit_verified'] = True
    for name, value in expected.items():
        if type(data.get(name)) is not type(value) or data.get(name) != value:
            raise RuntimeError(f'Packaged macOS smoke report mismatch: {name}')
    return data


def validate_bundle(bundle, expected_version, expected_arch, runner=subprocess.run):
    if expected_arch not in ('arm64', 'x86_64'):
        raise RuntimeError('Unsupported macOS build architecture')
    bundle = Path(bundle).resolve(strict=True)
    if bundle.suffix != '.app' or not bundle.is_dir():
        raise RuntimeError('Expected a complete macOS .app directory')
    with (bundle / 'Contents/Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    required = {'CFBundleIdentifier': BUNDLE_ID,
                'CFBundleExecutable': 'DeepRecover',
                'CFBundleShortVersionString': expected_version,
                'CFBundleVersion': expected_version,
                'LSMinimumSystemVersion': MINIMUM_OS,
                'CFBundlePackageType': 'APPL'}
    for key, expected in required.items():
        if type(info.get(key)) is not type(expected) or info.get(key) != expected:
            raise RuntimeError(f'Bundle metadata mismatch: {key}')
    icon = info.get('CFBundleIconFile', '')
    if not isinstance(icon, str) or Path(icon).name != icon or not icon.endswith('.icns'):
        raise RuntimeError('Bundle icon metadata is invalid')
    if not (bundle / 'Contents/Resources' / icon).is_file():
        raise RuntimeError('Bundle icon is missing')
    executable = bundle / 'Contents/MacOS/DeepRecover'
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError('Bundle executable is missing or not executable')
    with executable.open('rb') as stream:
        if stream.read(4) not in MACH_O_MAGIC:
            raise RuntimeError('Bundle executable is not Mach-O')
    result = runner(['/usr/bin/lipo', '-archs', str(executable)], capture_output=True,
                    text=True, check=True, timeout=15)
    if set(result.stdout.split()) != {expected_arch}:
        raise RuntimeError('Mach-O architecture differs from requested native build')
    return executable, info


def verify_decoded_image(path, image_format, label):
    # Reading through Qt verifies pixels, not merely an extension or file header.
    from PySide6.QtGui import QImageReader
    reader = QImageReader(str(path), image_format)
    image = reader.read()
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        raise RuntimeError(f'{label} cannot be decoded: {reader.errorString()}')


def verify_icon(path):
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as stream:
        header = stream.read(8)
        if len(header) != 8 or header[:4] != b'icns' or struct.unpack('>I', header[4:])[0] != size:
            raise RuntimeError('Bundle icon has an invalid ICNS container')
        while stream.tell() < size:
            header = stream.read(8)
            if len(header) != 8:
                raise RuntimeError('Bundle icon has an incomplete ICNS element')
            length = struct.unpack('>I', header[4:])[0]
            if length < 8 or stream.tell() + length - 8 > size:
                raise RuntimeError('Bundle icon has an invalid ICNS element length')
            stream.seek(length - 8, os.SEEK_CUR)
    verify_decoded_image(path, b'icns', 'Bundle icon')


def verify_screenshot(path):
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as stream:
        if stream.read(8) != b'\x89PNG\r\n\x1a\n':
            raise RuntimeError('Cocoa window screenshot is not PNG')
        # libpng can decode some damaged containers with warnings (including a
        # bad IEND checksum). Verify every chunk before accepting decoded pixels.
        while True:
            header = stream.read(8)
            if len(header) != 8:
                raise RuntimeError('Cocoa window screenshot is an incomplete PNG')
            length, kind = struct.unpack('>I4s', header)
            if length > 0x7fffffff or stream.tell() + length + 4 > size:
                raise RuntimeError('Cocoa window screenshot has an invalid PNG chunk length')
            remaining, checksum = length, zlib.crc32(kind)
            while remaining:
                data = stream.read(min(remaining, 65536))
                if not data:
                    raise RuntimeError('Cocoa window screenshot is an incomplete PNG')
                remaining -= len(data)
                checksum = zlib.crc32(data, checksum)
            stored = stream.read(4)
            if len(stored) != 4 or checksum != struct.unpack('>I', stored)[0]:
                raise RuntimeError('Cocoa window screenshot has an invalid PNG checksum')
            if kind == b'IEND':
                if length or stream.read(1):
                    raise RuntimeError('Cocoa window screenshot has an invalid PNG ending')
                break
    verify_decoded_image(path, b'png', 'Cocoa window screenshot')


def run_smoke(bundle, report, expected_version, expected_arch, qt_platform, screenshot=None):
    if platform.system() != 'Darwin' or platform.machine() != expected_arch:
        raise RuntimeError('Packaged .app verification requires the matching native macOS runner')
    translated = subprocess.run(['/usr/sbin/sysctl', '-in', 'sysctl.proc_translated'],
                                capture_output=True, text=True, timeout=10)
    if translated.returncode == 0 and translated.stdout.strip() == '1':
        raise RuntimeError('Rosetta execution is not accepted as a native architecture build')
    executable, info = validate_bundle(bundle, expected_version, expected_arch)
    verify_icon(Path(bundle) / 'Contents/Resources' / info['CFBundleIconFile'])
    report = Path(report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    if report.exists():
        raise RuntimeError('Smoke report must not pre-exist')
    if qt_platform == 'cocoa' and screenshot is None:
        raise RuntimeError('Cocoa verification requires a real window screenshot')
    screenshot = Path(screenshot).resolve() if screenshot is not None else None
    if screenshot is not None:
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        if screenshot.exists():
            raise RuntimeError('Smoke screenshot must not pre-exist')
    with tempfile.TemporaryDirectory(prefix='deeprecover-macos-user-') as directory:
        isolated = Path(directory)
        for name in ('config', 'cache', 'data', 'cf-user'):
            (isolated / name).mkdir()
        env = os.environ.copy()
        env.update(QT_QPA_PLATFORM=qt_platform,
                   DEEPRECOVER_DATA_DIR=str(isolated / 'data'),
                   XDG_CONFIG_HOME=str(isolated / 'config'),
                   XDG_CACHE_HOME=str(isolated / 'cache'),
                   XDG_DATA_HOME=str(isolated / 'data'),
                   CFFIXED_USER_HOME=str(isolated / 'cf-user'))
        command = [str(executable), '--smoke-test', '--smoke-report', str(report)]
        if screenshot is not None:
            command.extend(['--smoke-screenshot', str(screenshot)])
        result = subprocess.run(command, cwd=isolated, env=env, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=90)
        report.with_suffix('.stdout.txt').write_text(result.stdout, encoding='utf-8')
        report.with_suffix('.stderr.txt').write_text(result.stderr, encoding='utf-8')
        if result.returncode != 0:
            raise RuntimeError(f'Packaged macOS executable exited with {result.returncode}; inspect smoke logs')
        data = validate_report(report, expected_version, qt_platform)
        if screenshot is not None:
            verify_screenshot(screenshot)
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--arch', choices=('arm64', 'x86_64'), required=True)
    parser.add_argument('--qt-platform', choices=('offscreen', 'cocoa'), default='offscreen')
    parser.add_argument('--screenshot', type=Path)
    args = parser.parse_args()
    run_smoke(args.bundle, args.report, args.version, args.arch, args.qt_platform, args.screenshot)
    print(f'PASS macOS {args.arch}: Mach-O/Info.plist, {args.qt_platform}, navigation, image decode, 2 verified exports')


if __name__ == '__main__':
    main()
