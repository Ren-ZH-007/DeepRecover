"""Local disk discovery and conservative same-physical-disk write protection."""
from dataclasses import dataclass
import ctypes
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from xml.parsers.expat import ExpatError

from core.computer.io import DiskError, normalize_device, windows_volume_disk_numbers


@dataclass(frozen=True)
class DiskDevice:
    path: str
    label: str
    size: int
    system_disk: bool = False
    mountpoints: tuple[str, ...] = ()


WINDOWS_INVENTORY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$disks = @(Get-Disk | Select-Object Number,FriendlyName,Size,IsBoot,IsSystem,BusType)
$partitions = @(Get-Partition | Select-Object DiskNumber,PartitionNumber,DriveLetter,Size,IsBoot,IsSystem,AccessPaths)
[pscustomobject]@{disks=$disks;partitions=$partitions} | ConvertTo-Json -Depth 6 -Compress
"""


def _run(arguments):
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=20,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DiskError('无法读取本机磁盘信息，请检查系统权限或改选镜像。') from error
    if result.returncode:
        raise DiskError('系统未返回可用的磁盘信息，请检查权限和设备连接。')
    return result.stdout


def _plist(arguments):
    try:
        payload = plistlib.loads(_run(arguments))
        if not isinstance(payload, dict):
            raise ValueError('Expected a disk metadata dictionary')
        return payload
    except (ValueError, TypeError, plistlib.InvalidFileException, ExpatError) as error:
        raise DiskError('系统磁盘信息格式无效，未访问磁盘内容。') from error


def _windows_inventory():
    try:
        payload = json.loads(_run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', WINDOWS_INVENTORY]).decode('utf-8-sig'))
        if not isinstance(payload, dict):
            raise ValueError('Expected disk inventory')
        return payload
    except (ValueError, UnicodeError, TypeError) as error:
        raise DiskError('系统磁盘列表格式无效，未访问磁盘内容。') from error


def _array(value):
    return value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])


def _valid_size(value):
    return type(value) is int and value > 0


def _confirmed_windows_disks(numbers):
    # Disk extents name OS disk numbers. Storage Spaces, file-backed virtual
    # disks and RAID/network pools may hide shared underlying physical media.
    # Different numbers alone are not proof of physical separation.
    direct_types = {1, 2, 3, 4, 5, 7, 10, 11, 12, 13, 17, 18, 19}
    direct_names = {'scsi', 'atapi', 'ata', '1394', 'ssa', 'usb', 'sas', 'sata',
                    'sd', 'mmc', 'nvme', 'scm', 'ufs'}
    inventory = {disk.get('Number'): disk.get('BusType')
                 for disk in _array(_windows_inventory().get('disks'))
                 if type(disk.get('Number')) is int}
    for number in numbers:
        bus = inventory.get(number)
        known = (type(bus) is int and bus in direct_types) or (isinstance(bus, str) and bus.casefold() in direct_names)
        if not known:
            raise DiskError('虚拟盘、存储池或未知总线不能证明物理隔离，请使用稳定原始镜像或可确认的独立磁盘。')


def _windows_devices():
    inventory = _windows_inventory()
    partitions = [p for p in _array(inventory.get('partitions')) if isinstance(p, dict)]
    devices = []
    disk_flags = {}
    for disk in _array(inventory.get('disks')):
        if not isinstance(disk, dict):
            continue
        number, size = disk.get('Number'), disk.get('Size')
        if type(number) is not int or number < 0 or not _valid_size(size):
            continue
        system = disk.get('IsBoot') is True or disk.get('IsSystem') is True
        disk_flags[number] = system
        mounts = tuple(sorted({path for partition in partitions
            if partition.get('DiskNumber') == number
            for path in (partition.get('AccessPaths') if isinstance(partition.get('AccessPaths'), list) else [])
            if isinstance(path, str) and re.match(r'^[A-Za-z]:\\', path)}))
        devices.append(DiskDevice(rf'\\.\PhysicalDrive{number}',
            str(disk.get('FriendlyName') or f'磁盘 {number}'), size, system, mounts))
    for partition in partitions:
        letter, size = partition.get('DriveLetter'), partition.get('Size')
        if not isinstance(letter, str) or not re.fullmatch('[A-Za-z]', letter) or not _valid_size(size):
            continue
        letter = letter.upper()
        system = (partition.get('IsBoot') is True or partition.get('IsSystem') is True
                  or disk_flags.get(partition.get('DiskNumber'), False))
        devices.append(DiskDevice(rf'\\.\{letter}:', f'{letter}: 卷', size, system, (letter + ':\\',)))
    return devices


def _mac_info(path):
    return _plist(['/usr/sbin/diskutil', 'info', '-plist', str(path).replace('/dev/rdisk', '/dev/disk', 1)])


def _mac_apfs():
    try:
        return _plist(['/usr/sbin/diskutil', 'apfs', 'list', '-plist'])
    except DiskError:
        # Non-APFS physical devices can still be mapped. APFS/virtual identities
        # without a confirmed physical-store mapping are rejected below.
        return {}


def _disk_identifier(value):
    if not isinstance(value, str):
        raise DiskError('缺少物理磁盘标识，不能确认安全的写入位置。')
    value = value.removeprefix('/dev/').removeprefix('r')
    match = re.fullmatch(r'(disk[0-9]+)(?:s[0-9]+)*', value)
    if not match:
        raise DiskError('物理磁盘映射无效，不能确认安全的写入位置。')
    return match.group(1)


def _store_ids(stores):
    if not isinstance(stores, list) or not stores:
        raise DiskError('APFS 缺少物理存储映射，不能确认安全的写入位置。')
    identifiers = set()
    for store in stores:
        value = store.get('DeviceIdentifier') if isinstance(store, dict) else store
        identifiers.add(_disk_identifier(value))
    return identifiers


def _mac_physical_ids(info, apfs):
    stores = info.get('APFSPhysicalStores')
    if stores is not None:
        return _store_ids(stores)
    identifier = info.get('DeviceIdentifier')
    container = info.get('APFSContainerReference')
    for item in _array(apfs.get('Containers')):
        if not isinstance(item, dict):
            continue
        reference = item.get('ContainerReference') or item.get('APFSContainerReference')
        volumes = {v.get('DeviceIdentifier') for v in _array(item.get('Volumes')) if isinstance(v, dict)}
        if (reference and (reference == identifier or reference == container or identifier in volumes)):
            return _store_ids(item.get('PhysicalStores'))
    if container or info.get('Virtual') is True or info.get('VirtualOrPhysical') == 'Virtual':
        raise DiskError('无法确认虚拟卷或 APFS 容器对应的全部物理磁盘。')
    # A physical partition belongs to its containing physical disk. Synthesized
    # APFS volumes must take the mapping branch above, never this fallback.
    if (info.get('APFSVolumeDisk') is True or str(info.get('FilesystemType', '')).lower() == 'apfs'
            or (info.get('Content') == 'Apple_APFS' and info.get('VirtualOrPhysical') != 'Physical')):
        raise DiskError('无法确认 APFS 卷所在的物理磁盘。')
    return {_disk_identifier(info.get('ParentWholeDisk') or info.get('PartOfWhole') or identifier)}


def _mac_devices():
    inventory = _plist(['/usr/sbin/diskutil', 'list', '-plist'])
    apfs = _mac_apfs()
    try:
        system_ids = _mac_physical_ids(_mac_info('/'), apfs)
    except DiskError:
        system_ids = set()
    devices = {}
    def collect(node):
        if not isinstance(node, dict):
            return
        identifier, size = node.get('DeviceIdentifier'), node.get('Size')
        if isinstance(identifier, str) and re.fullmatch(r'disk[0-9]+(?:s[0-9]+)*', identifier) and _valid_size(size):
            try:
                system = bool(_mac_physical_ids(node, apfs) & system_ids)
            except DiskError:
                system = False
            mount = node.get('MountPoint')
            devices[identifier] = DiskDevice('/dev/' + identifier,
                str(node.get('VolumeName') or node.get('Content') or identifier),
                size, system, (mount,) if isinstance(mount, str) else ())
        for key in ('Partitions', 'APFSVolumes', 'Volumes'):
            for child in _array(node.get(key)):
                collect(child)
    for node in _array(inventory.get('AllDisksAndPartitions')):
        collect(node)
    return list(devices.values())


def list_devices():
    if sys.platform == 'win32':
        return _windows_devices()
    if sys.platform == 'darwin':
        return _mac_devices()
    raise DiskError('此平台不枚举原始磁盘；请选择已有的磁盘镜像文件。')


def _existing_ancestor(destination):
    path = Path(destination).expanduser().resolve()
    while not path.exists():
        if path.parent == path:
            raise DiskError('无法找到目标目录所在的文件系统。')
        path = path.parent
    return path


def _windows_volume_path(path):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumePathNameW.restype = wintypes.BOOL
    kernel.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
    mount = ctypes.create_unicode_buffer(32768)
    volume = ctypes.create_unicode_buffer(1024)
    if not kernel.GetVolumePathNameW(str(path), mount, len(mount)):
        raise DiskError('无法确认目标目录所在的 Windows 卷。')
    if not kernel.GetVolumeNameForVolumeMountPointW(mount.value, volume, len(volume)):
        raise DiskError('无法确认目标卷的唯一标识。')
    return volume.value.rstrip('\\')


def _mac_volume_path(path):
    output = _run(['/bin/df', '-P', str(path)])
    try:
        lines = output.decode('utf-8').splitlines()
        value = lines[-1].split()[0]
    except (UnicodeError, IndexError) as error:
        raise DiskError('无法确认目标目录所在的 macOS 卷。') from error
    if not re.fullmatch(r'/dev/disk[0-9]+(?:s[0-9]+)*', value):
        raise DiskError('目标目录不是可确认物理磁盘的本机卷。')
    return value


def ensure_different_device(source_path, destination):
    """Reject writes unless source and destination physical disks are disjoint.

    Call before creating work directories or export batches. Mount points,
    APFS synthesized identifiers and spanned Windows volumes are not treated as
    proof of physically separate storage.
    """
    source = normalize_device(source_path)
    try:
        existing = _existing_ancestor(destination)
        if sys.platform == 'win32':
            match = re.fullmatch(r'\\\\\.\\PhysicalDrive([0-9]+)', source, re.IGNORECASE)
            source_ids = {int(match.group(1))} if match else windows_volume_disk_numbers(source)
            target_ids = windows_volume_disk_numbers(_windows_volume_path(existing))
        elif sys.platform == 'darwin':
            apfs = _mac_apfs()
            source_ids = _mac_physical_ids(_mac_info(source), apfs)
            target_ids = _mac_physical_ids(_mac_info(_mac_volume_path(existing)), apfs)
        else:
            raise DiskError('此平台不能确认原始磁盘的安全写入目标。')
    except (OSError, ValueError, TypeError) as error:
        raise DiskError('无法确认目标与来源位于不同物理磁盘，已拒绝写入。') from error
    if not source_ids or not target_ids:
        raise DiskError('物理磁盘映射为空，已拒绝写入。')
    if source_ids & target_ids:
        raise DiskError('工作目录和导出目标必须位于另一块物理磁盘，不能写回来源磁盘。')
    if sys.platform == 'win32':
        _confirmed_windows_disks(source_ids | target_ids)
    return True
