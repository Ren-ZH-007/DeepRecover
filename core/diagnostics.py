"""Local readiness metadata; no device queries, source reads or network calls.

Saving requires the caller to check physical isolation from the selected raw
source first. This module only enforces the existing workspace Reports boundary.
Readiness observations never complete the separate manual acceptance cases.

Disk mapping and error statistics are supplied by the caller from what the app
already holds (the last disk detection and local task records); this module
never queries devices or reads history itself. Both are reduced to closed,
non-identifying fields: no device names, paths, serials, sizes or messages.
"""
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import sys
from uuid import uuid4

from app.version import VERSION
from core.device.android.adapter import AndroidAdapter


class DiagnosticsError(RuntimeError):
    pass


MANUAL_CASES = (
    ('M5-01', '下载与启动'),
    ('M5-02', '桌面与高 DPI'),
    ('M5-03', 'Android 授权'),
    ('M5-04', 'Android 普通文件采集'),
    ('M5-05', '真实 iPhone 备份'),
    ('M5-06', '真实磁盘与 U 盘'),
    ('M5-07', '同盘写入拒绝'),
    ('M5-08', '校验导出与历史'),
    ('M5-09', '安装与卸载体验'),
)
_PLATFORMS = {'win32', 'darwin', 'linux', 'unknown'}
_ARCHITECTURES = {'x86_64', 'arm64', 'x86', 'riscv64', 'unknown'}
_QT_PLATFORMS = {'windows', 'cocoa', 'offscreen', 'xcb', 'wayland', 'minimal', 'vnc', 'unknown'}
_DEVICE_KINDS = {'physical', 'partition', 'apfs_container', 'apfs_volume', 'snapshot', 'auxiliary', 'unknown'}
_FILE_SYSTEMS = {'NTFS', 'FAT32', 'exFAT', 'APFS', 'HFS+'}
_ROLES = {'System', 'Data', 'Preboot', 'Recovery', 'VM', 'Update', 'xART', 'Hardware', 'iSCPreboot', 'iSCRecovery'}
_TASK_KINDS = {'android_discovery', 'android_listing', 'android_file_listing', 'android_acquisition',
               'ios_discovery', 'ios_manifest', 'signature_scan', 'computer_scan', 'image_preview',
               'pdf_preview', 'video_preview', 'export'}
_PROBLEM_STATUSES = {'failed', 'interrupted', 'cancelled'}
_ERROR_CODE = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}', re.ASCII)
_MAPPINGS = {'unmapped', 'single', 'multiple', 'unknown'}
MAX_VOLUMES = 64
MAX_ERROR_ROWS = 40


def _checkpoint(token):
    if token is not None:
        token.checkpoint()


def _version(value):
    return value if isinstance(value, str) and re.fullmatch(r'\d+(?:\.\d+){0,4}', value, re.ASCII) else 'unknown'


def _count(value):
    return value if type(value) is int and 0 <= value <= 64 else None


def _bytes(value):
    return value if type(value) is int and 0 <= value < 2**80 else None


def _known(value, allowed):
    return value if isinstance(value, str) and value in allowed else 'unknown'


def _flag(value):
    return value if type(value) is bool else None


def _tally(value):
    return value if type(value) is int and 0 < value < 2**31 else None


def disk_snapshot(devices):
    """Copy only non-identifying fields from already-detected disk devices."""
    if devices is None:
        return None
    snapshot = []
    for device in list(devices)[:MAX_VOLUMES]:
        members = getattr(device, 'physical_disks', ())
        members = members if isinstance(members, (tuple, list)) else ()
        snapshot.append({'kind': getattr(device, 'device_kind', 'unknown'), 'file_system': getattr(device, 'file_system', ''),
                         'roles': list(getattr(device, 'roles', ()) or ()), 'internal': getattr(device, 'internal', None),
                         'removable': getattr(device, 'removable', None), 'system_disk': getattr(device, 'system_disk', None),
                         'physical_disks': len({m for m in members if isinstance(m, str) and m}),
                         '_members': sorted({m for m in members if isinstance(m, str) and m})})
    distinct = {m for item in snapshot for m in item.pop('_members')}
    return {'volumes': snapshot, 'physical_disk_count': len(distinct)}


def _sanitized_disks(value):
    if not isinstance(value, dict) or not isinstance(value.get('volumes'), list):
        return {'status': 'not_detected', 'physical_disk_count': None, 'volumes': []}
    volumes = []
    for item in value['volumes'][:MAX_VOLUMES]:
        if not isinstance(item, dict):
            continue
        mapped = item.get('physical_disks')
        if 'physical_disks' not in item and item.get('physical_mapping') in _MAPPINGS:
            mapping = item['physical_mapping']   # already-sanitized report being saved
        elif type(mapped) is int and 0 <= mapped <= 64:
            mapping = 'unmapped' if mapped == 0 else 'single' if mapped == 1 else 'multiple'
        else:
            mapping = 'unknown'
        roles = item.get('roles')
        volumes.append({
            'kind': _known(item.get('kind'), _DEVICE_KINDS),
            'file_system': _known(item.get('file_system'), _FILE_SYSTEMS),
            'roles': sorted({role for role in roles if role in _ROLES}) if isinstance(roles, list) else [],
            'internal': _flag(item.get('internal')),
            'removable': _flag(item.get('removable')),
            'system_disk': _flag(item.get('system_disk')),
            'physical_mapping': mapping,
        })
    volumes.sort(key=lambda v: json.dumps(v, sort_keys=True))
    count = value.get('physical_disk_count')
    return {'status': 'detected', 'physical_disk_count': count if type(count) is int and 0 <= count <= 64 else None,
            'volumes': volumes}


def _sanitized_errors(value):
    if not isinstance(value, dict):
        return {'status': 'not_collected', 'tasks': [], 'exports': []}
    def rows(items, fields):
        merged = {}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or _tally(item.get('count')) is None:
                continue
            code = item.get('error')
            code = code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else ('none' if code is None else 'unknown')
            key = tuple(check(item.get(name)) for name, check in fields) + (code,)
            merged[key] = min(merged.get(key, 0) + item['count'], 2**31 - 1)
        names = [name for name, _ in fields] + ['error']
        ordered = sorted(merged.items(), key=lambda pair: (-pair[1], pair[0]))[:MAX_ERROR_ROWS]
        return [{**dict(zip(names, key)), 'count': count} for key, count in ordered]
    status = lambda v: _known(v, _PROBLEM_STATUSES)
    return {'status': 'collected',
            'tasks': rows(value.get('tasks'), (('kind', lambda v: _known(v, _TASK_KINDS)), ('status', status))),
            'exports': rows(value.get('exports'), (('status', status),))}


def _sanitized_report(report):
    """Rebuild a closed schema, including unconditionally pending M5 cases."""
    if not isinstance(report, dict):
        raise DiagnosticsError('诊断报告格式无效，请重新检查本机环境。')
    def section(name):
        value = report.get(name)
        return value if isinstance(value, dict) else {}
    system, qt, workspace, adb = (section(name) for name in ('system', 'qt', 'workspace', 'adb'))
    disks, errors = report.get('disks'), report.get('errors')
    screen_count = _count(qt.get('screen_count'))
    high_dpi = _count(qt.get('high_dpi_screens'))
    if screen_count is None or high_dpi is None or high_dpi > screen_count:
        screen_count = high_dpi = None
    free, total = _bytes(workspace.get('free_bytes')), _bytes(workspace.get('total_bytes'))
    available = workspace.get('status') == 'available' and free is not None and total is not None and free <= total
    return {
        'schema_version': 2,
        'kind': 'environment_readiness',
        'app_version': VERSION,
        'system': {
            'platform': _known(system.get('platform'), _PLATFORMS),
            'os_version': _version(system.get('os_version')),
            'architecture': _known(system.get('architecture'), _ARCHITECTURES),
            'python_version': _version(system.get('python_version')),
            'packaged': system.get('packaged') is True,
        },
        'qt': {
            'version': _version(qt.get('version')),
            'platform': _known(qt.get('platform'), _QT_PLATFORMS),
            'screen_count': screen_count,
            'high_dpi_screens': high_dpi,
        },
        'workspace': {
            'status': 'available' if available else 'unavailable',
            'free_bytes': free if available else None,
            'total_bytes': total if available else None,
        },
        'adb': {'located': adb.get('located') is True},
        'disks': _sanitized_disks(disks),
        'errors': _sanitized_errors(errors),
        'manual_acceptance': {
            'status': 'not_run',
            'cases': [{'id': case_id, 'status': 'not_run'} for case_id, _ in MANUAL_CASES],
        },
    }


def collect_environment(data_dir, *, display=None, disks=None, errors=None, token=None):
    """Inspect metadata only; ADB is located but is never executed.

    The supplied Qt display dictionary must already have been captured on the
    GUI thread. No screen names, device identifiers or filesystem paths survive
    the closed report schema. Capacity is an observation, not a write test.
    ``disks`` (from :func:`disk_snapshot`) and ``errors`` (from
    ``Repository.error_summary``) are optional caller-supplied observations.
    """
    _checkpoint(token)
    try:
        # Keep only the numeric release prefix, never a vendor/host suffix.
        raw_release = platform.mac_ver()[0] if sys.platform == 'darwin' else platform.release()
        match = re.match(r'\d+(?:\.\d+){0,4}', raw_release, re.ASCII)
        os_version = match[0] if match else 'unknown'
    except (OSError, ValueError, TypeError):
        os_version = 'unknown'
    try:
        machine = platform.machine().casefold()
    except (OSError, ValueError, AttributeError):
        machine = 'unknown'
    architecture = {'amd64': 'x86_64', 'aarch64': 'arm64', 'i386': 'x86', 'i686': 'x86'}.get(machine, machine)
    _checkpoint(token)
    workspace = {'status': 'unavailable', 'free_bytes': None, 'total_bytes': None}
    try:
        if Path(data_dir).is_dir():
            usage = shutil.disk_usage(data_dir)
            workspace = {'status': 'available', 'free_bytes': usage.free, 'total_bytes': usage.total}
    except (OSError, RuntimeError, ValueError, TypeError):
        pass
    _checkpoint(token)
    try:
        executable = AndroidAdapter().executable
        located = bool(executable and os.path.isfile(executable) and os.access(executable, os.X_OK))
    except (OSError, RuntimeError, ValueError, TypeError):
        located = False
    _checkpoint(token)
    return _sanitized_report({
        'system': {
            'platform': sys.platform,
            'os_version': os_version,
            'architecture': architecture,
            'python_version': '.'.join(str(part) for part in sys.version_info[:3]),
            'packaged': bool(getattr(sys, 'frozen', False)),
        },
        'qt': display,
        'workspace': workspace,
        'adb': {'located': located},
        'disks': disks,
        'errors': errors,
    })


def _identity(info):
    return info.st_dev, info.st_ino


def _directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or path.is_symlink() or path.resolve(strict=True) != path
            or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
        raise OSError('Unsafe report directory')
    return _identity(info)


def _same_directories(root, directory, identities):
    if (_directory(root), _directory(directory)) != identities:
        raise OSError('Report directory changed')


def write_environment_report(data_dir, report):
    """Save one new report to existing Reports; caller preflights raw sources.

    No custom paths or overwrite behavior are accepted. The closed report schema
    is rebuilt here as well, so arbitrary extra fields and claimed acceptance
    successes cannot enter a saved diagnostic report.
    """
    payload = (json.dumps(_sanitized_report(report), ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    descriptor = directory_descriptor = None
    target = None
    created_identity = None
    try:
        root = Path(data_dir).absolute()
        directory = root / 'Reports'
        identities = _directory(root), _directory(directory)
        filename = f'environment-{uuid4().hex}.json'
        target = directory / filename
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        if os.open in getattr(os, 'supports_dir_fd', set()):
            directory_descriptor = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
            if _identity(os.fstat(directory_descriptor)) != identities[1]:
                raise OSError('Report directory changed')
            _same_directories(root, directory, identities)
            descriptor = os.open(filename, flags, 0o600, dir_fd=directory_descriptor)
        else:
            _same_directories(root, directory, identities)
            descriptor = os.open(target, flags, 0o600)
        info = os.fstat(descriptor)
        created_identity = _identity(info)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError('Report must be an independent regular file')
        _same_directories(root, directory, identities)
        if _identity(target.lstat()) != created_identity or target.is_symlink():
            raise OSError('Report path changed')
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            final_info = os.fstat(stream.fileno())
            if _identity(final_info) != created_identity or final_info.st_nlink != 1:
                raise OSError('Report file changed')
        _same_directories(root, directory, identities)
        if _identity(target.lstat()) != created_identity:
            raise OSError('Report path changed')
        return target
    except (OSError, RuntimeError, ValueError, TypeError):
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        # Only remove a newly created file whose path still names our inode.
        if target is not None and created_identity is not None:
            try:
                if not target.is_symlink() and _identity(target.lstat()) == created_identity:
                    target.unlink()
            except OSError:
                pass
        raise DiagnosticsError('诊断报告未保存，请检查工作目录权限及 Reports 目录。') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
