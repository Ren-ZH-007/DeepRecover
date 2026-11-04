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
    # Presentation metadata only. Scans must still run the live physical-disk
    # guard; grouping or a removable/internal hint never grants access.
    physical_disks: tuple[str, ...] = ()
    device_kind: str = 'unknown'
    file_system: str = ''
    internal: bool | None = None
    removable: bool = False
    roles: tuple[str, ...] = ()


WINDOWS_INVENTORY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$disks = @(Get-Disk | Select-Object Number,FriendlyName,Size,IsBoot,IsSystem,BusType)
$partitions = @(Get-Partition | Select-Object DiskNumber,PartitionNumber,DriveLetter,Size,IsBoot,IsSystem,AccessPaths)
[pscustomobject]@{disks=$disks;partitions=$partitions} | ConvertTo-Json -Depth 6 -Compress
"""

WINDOWS_VOLUME_INVENTORY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$volumes = @(Get-Volume | Select-Object DriveLetter,FileSystemType,FileSystem,FileSystemLabel,Size)
[pscustomobject]@{volumes=$volumes} | ConvertTo-Json -Depth 4 -Compress
"""


def _run(arguments, *, timeout=20):
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=timeout,
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


def _windows_inventory(*, include_volumes=False):
    try:
        payload = json.loads(_run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', WINDOWS_INVENTORY]).decode('utf-8-sig'))
        if not isinstance(payload, dict):
            raise ValueError('Expected disk inventory')
    except (ValueError, UnicodeError, TypeError) as error:
        raise DiskError('系统磁盘列表格式无效，未访问磁盘内容。') from error
    if include_volumes:
        try:
            metadata = json.loads(_run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                WINDOWS_VOLUME_INVENTORY], timeout=5).decode('utf-8-sig'))
            payload['volumes'] = _array(metadata.get('volumes')) if isinstance(metadata, dict) else []
        except (DiskError, ValueError, UnicodeError, TypeError):
            # Filesystem names and labels are optional display metadata. A
            # disconnected volume or a permission failure must not suppress the
            # required physical inventory or weaken its separate live guard.
            payload['volumes'] = []
    return payload


def _array(value):
    return value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])


def _valid_size(value):
    return type(value) is int and value > 0


def _filesystem(value):
    if not isinstance(value, str):
        return ''
    return {'ntfs': 'NTFS', 'windows_ntfs': 'NTFS', 'fat32': 'FAT32',
            'dos_fat_32': 'FAT32', 'ms-dos fat32': 'FAT32', 'exfat': 'exFAT',
            'apfs': 'APFS', 'apple_apfs': 'APFS', 'hfs+': 'HFS+',
            'hfs': 'HFS+', 'hfsx': 'HFS+', 'apple_hfs': 'HFS+'}.get(value.casefold(), '')


def _explicit_internal(metadata):
    value = metadata.get('Internal', metadata.get('IsInternal'))
    return value if type(value) is bool else None


def _windows_removable(metadata):
    value = metadata.get('IsRemovable')
    if type(value) is bool:
        return value
    bus = metadata.get('BusType')
    return (type(bus) is int and bus == 7) or (isinstance(bus, str) and bus.casefold() == 'usb')


def _windows_bus_is_direct(bus):
    direct_types = {1, 2, 3, 4, 5, 7, 10, 11, 12, 13, 17, 18, 19}
    direct_names = {'scsi', 'atapi', 'ata', '1394', 'ssa', 'usb', 'sas', 'sata',
                    'sd', 'mmc', 'nvme', 'scm', 'ufs'}
    return ((type(bus) is int and bus in direct_types)
            or (isinstance(bus, str) and bus.casefold() in direct_names))


def _confirmed_windows_disks(numbers):
    # Disk extents name OS disk numbers. Storage Spaces, file-backed virtual
    # disks and RAID/network pools may hide shared underlying physical media.
    # Different numbers alone are not proof of physical separation.
    inventory = {disk.get('Number'): disk.get('BusType')
                 for disk in _array(_windows_inventory().get('disks'))
                 if type(disk.get('Number')) is int}
    for number in numbers:
        bus = inventory.get(number)
        if not _windows_bus_is_direct(bus):
            raise DiskError('虚拟盘、存储池或未知总线不能证明物理隔离，请使用稳定原始镜像或可确认的独立磁盘。')


def _windows_devices():
    inventory = _windows_inventory(include_volumes=True)
    partitions = [p for p in _array(inventory.get('partitions')) if isinstance(p, dict)]
    devices = []
    disk_flags, disk_metadata = {}, {}
    for disk in _array(inventory.get('disks')):
        if not isinstance(disk, dict):
            continue
        number, size = disk.get('Number'), disk.get('Size')
        if type(number) is not int or number < 0 or not _valid_size(size):
            continue
        system = disk.get('IsBoot') is True or disk.get('IsSystem') is True
        disk_flags[number] = system
        disk_metadata[number] = disk
        mounts = tuple(sorted({path for partition in partitions
            if partition.get('DiskNumber') == number
            for path in (partition.get('AccessPaths') if isinstance(partition.get('AccessPaths'), list) else [])
            if isinstance(path, str) and re.match(r'^[A-Za-z]:\\', path)}))
        path = rf'\\.\PhysicalDrive{number}'
        direct = _windows_bus_is_direct(disk.get('BusType'))
        devices.append(DiskDevice(path,
            str(disk.get('FriendlyName') or f'磁盘 {number}'), size, system, mounts,
            (path,) if direct else (), 'physical' if direct else 'unknown', internal=_explicit_internal(disk),
            removable=_windows_removable(disk)))
    volumes = {row['DriveLetter'].upper(): row for row in _array(inventory.get('volumes'))
               if isinstance(row, dict) and isinstance(row.get('DriveLetter'), str)
               and re.fullmatch('[A-Za-z]', row['DriveLetter'])}
    by_letter = {}
    for partition in partitions:
        letter, size = partition.get('DriveLetter'), partition.get('Size')
        if not isinstance(letter, str) or not re.fullmatch('[A-Za-z]', letter) or not _valid_size(size):
            continue
        letter = letter.upper()
        by_letter.setdefault(letter, []).append(partition)
    for letter, members in by_letter.items():
        volume = volumes.get(letter, {})
        numbers = {member.get('DiskNumber') for member in members
                   if type(member.get('DiskNumber')) is int and member['DiskNumber'] >= 0}
        confirmed = (len(numbers) > 0 and all(number in disk_metadata
                     and _windows_bus_is_direct(disk_metadata[number].get('BusType')) for number in numbers)
                     and all(type(member.get('DiskNumber')) is int and member['DiskNumber'] >= 0 for member in members))
        parents = tuple(rf'\\.\PhysicalDrive{number}' for number in sorted(numbers)) if confirmed else ()
        flags = [_explicit_internal(disk_metadata[number]) for number in numbers if number in disk_metadata]
        internal = flags[0] if flags and all(flag is flags[0] for flag in flags) and confirmed else None
        system = any(member.get('IsBoot') is True or member.get('IsSystem') is True
                     or (type(member.get('DiskNumber')) is int and disk_flags.get(member['DiskNumber'], False))
                     for member in members)
        size = volume.get('Size') if _valid_size(volume.get('Size')) else max(member['Size'] for member in members)
        label = volume.get('FileSystemLabel')
        label = f'{letter}: · {label}' if isinstance(label, str) and label else f'{letter}: 卷'
        devices.append(DiskDevice(rf'\\.\{letter}:', label, size, system, (letter + ':\\',),
            parents, 'partition' if confirmed else 'unknown',
            _filesystem(volume.get('FileSystemType')) or _filesystem(volume.get('FileSystem')),
            internal, any(_windows_removable(disk_metadata[number]) for number in numbers if number in disk_metadata)))
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


def _canonical_mac_identifier(value):
    _disk_identifier(value)
    return value.removeprefix('/dev/').removeprefix('r')


def _store_ids(stores):
    if not isinstance(stores, list) or not stores:
        raise DiskError('APFS 缺少物理存储映射，不能确认安全的写入位置。')
    identifiers = set()
    for store in stores:
        # diskutil info uses APFSPhysicalStore; apfs list uses DeviceIdentifier.
        # A present but malformed/conflicting field cannot be ignored in favour
        # of another descriptor or a guessed synthesized disk number.
        values = ([store[key] for key in ('DeviceIdentifier', 'APFSPhysicalStore') if key in store]
                  if isinstance(store, dict) else [store])
        normalized = {_canonical_mac_identifier(value) for value in values}
        if len(normalized) != 1:
            raise DiskError('APFS 物理存储标识缺失或冲突，不能确认安全的写入位置。')
        identifiers.add(_disk_identifier(normalized.pop()))
    return identifiers


def _mac_physical_ids(info, apfs):
    direct = _store_ids(info['APFSPhysicalStores']) if 'APFSPhysicalStores' in info else None
    identifier = info.get('DeviceIdentifier')
    container = info.get('APFSContainerReference')
    identifier = _canonical_mac_identifier(identifier) if identifier is not None else None
    container = _canonical_mac_identifier(container) if container is not None else None
    volume_marker = info.get('APFSVolumeDisk')
    volume = _canonical_mac_identifier(volume_marker) if isinstance(volume_marker, str) else None
    if volume_marker is not None and type(volume_marker) not in (str, bool):
        raise DiskError('APFS 卷标识无效，不能确认安全的写入位置。')
    if volume is not None and identifier is not None and _disk_identifier(volume) != _disk_identifier(identifier):
        raise DiskError('APFS 快照与卷标识冲突，不能确认安全的写入位置。')
    physical_source = (_disk_identifier(identifier)
                       if identifier is not None and info.get('VirtualOrPhysical') == 'Physical' else None)
    if container is not None:
        if (_disk_identifier(container) != container
                or (volume is not None and _disk_identifier(volume) != container)
                or (identifier is not None and _disk_identifier(identifier) != container
                    and physical_source is None)):
            raise DiskError('APFS 容器与卷标识冲突，不能确认安全的写入位置。')
    mapped = None
    for item in _array(apfs.get('Containers')):
        if not isinstance(item, dict):
            continue
        reference = item.get('ContainerReference') or item.get('APFSContainerReference')
        if not isinstance(reference, str):
            continue
        reference = _canonical_mac_identifier(reference)
        volumes = {_canonical_mac_identifier(v['DeviceIdentifier'])
                   for v in _array(item.get('Volumes'))
                   if isinstance(v, dict) and isinstance(v.get('DeviceIdentifier'), str)}
        # Snapshot identifiers (disk3s1s1) are linked via the explicit parent
        # APFSVolumeDisk (disk3s1), never by stripping their synthesized prefix.
        if reference in (identifier, container) or identifier in volumes or volume in volumes:
            found = _store_ids(item.get('PhysicalStores'))
            if ((container is not None and reference != container)
                    or (mapped is not None and mapped != found)):
                raise DiskError('APFS 容器与物理存储映射冲突，不能确认安全的写入位置。')
            mapped = found
    if direct is not None:
        if mapped is not None and direct != mapped:
            raise DiskError('APFS 物理存储映射不一致，请重新检测来源。')
    resolved = direct if direct is not None else mapped
    if resolved is not None:
        # A real physical-store partition may reference a differently numbered
        # synthesized container. Keep this case only when diskutil explicitly
        # marks it physical and the resolved stores contain that physical disk.
        if physical_source is not None and physical_source not in resolved:
            raise DiskError('APFS 来源物理盘与存储映射冲突，不能确认安全的写入位置。')
        return resolved
    if container or info.get('Virtual') is True or info.get('VirtualOrPhysical') == 'Virtual':
        raise DiskError('无法确认虚拟卷或 APFS 容器对应的全部物理磁盘。')
    # A physical partition belongs to its containing physical disk. Synthesized
    # APFS volumes must take the mapping branch above, never this fallback.
    if ((volume_marker is not None and volume_marker is not False)
            or info.get('APFSSnapshot') is True or str(info.get('FilesystemType', '')).lower() == 'apfs'
            or (info.get('Content') == 'Apple_APFS' and info.get('VirtualOrPhysical') != 'Physical')):
        raise DiskError('无法确认 APFS 卷所在的物理磁盘。')
    return {_disk_identifier(info.get('ParentWholeDisk') or info.get('PartOfWhole') or identifier)}


def _mac_roles(metadata):
    names = {'system': 'System', 'data': 'Data', 'preboot': 'Preboot',
             'recovery': 'Recovery', 'vm': 'VM', 'update': 'Update',
             'xart': 'xART', 'hardware': 'Hardware', 'iscpreboot': 'iSCPreboot',
             'iscrecovery': 'iSCRecovery'}
    roles = set()
    for key in ('Roles', 'Role'):
        if key not in metadata:
            continue
        values = metadata[key]
        values = [values] if isinstance(values, str) else values
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            # Malformed or unknown role metadata must not hide a data volume.
            return ()
        roles.update(names.get(value.casefold(), value) for value in values if value)
    return tuple(sorted(roles))


def _mac_devices():
    inventory = _plist(['/usr/sbin/diskutil', 'list', '-plist'])
    apfs = _mac_apfs()
    root_info, system_ids = {}, set()
    try:
        root_info = _mac_info('/')
        system_ids = _mac_physical_ids(root_info, apfs)
    except DiskError:
        pass
    nodes, contexts, stores = {}, {}, set()

    def valid_identifier(value):
        return isinstance(value, str) and re.fullmatch(r'disk[0-9]+(?:s[0-9]+)*', value)

    def collect(node, container=None):
        if not isinstance(node, dict):
            return
        identifier = node.get('DeviceIdentifier')
        if valid_identifier(identifier):
            nodes.setdefault(identifier, {}).update(node)
            if container is not None:
                contexts.setdefault(identifier, set()).add(container)
        for key in ('Partitions', 'APFSVolumes', 'Volumes'):
            for child in _array(node.get(key)):
                collect(child, identifier if key == 'APFSVolumes' and valid_identifier(identifier) else container)

    for node in _array(inventory.get('AllDisksAndPartitions')):
        collect(node)
    container_ids = set()
    for container in _array(apfs.get('Containers')):
        if not isinstance(container, dict):
            continue
        reference = container.get('ContainerReference') or container.get('APFSContainerReference')
        if not valid_identifier(reference):
            continue
        container_ids.add(reference)
        ceiling = container.get('CapacityCeiling')
        if reference not in nodes and _valid_size(ceiling):
            nodes[reference] = {'DeviceIdentifier': reference, 'Size': ceiling, 'Content': 'Apple_APFS'}
        for store in _array(container.get('PhysicalStores')):
            if not isinstance(store, dict):
                continue
            try:
                values = { _canonical_mac_identifier(store[key])
                           for key in ('DeviceIdentifier', 'APFSPhysicalStore') if key in store }
            except DiskError:
                continue
            if len(values) == 1:
                stores.update(values)
        for volume in _array(container.get('Volumes')):
            if not isinstance(volume, dict) or not valid_identifier(volume.get('DeviceIdentifier')):
                continue
            identifier = volume['DeviceIdentifier']
            contexts.setdefault(identifier, set()).add(reference)
            metadata = {**volume, **nodes.get(identifier, {})}
            for key in ('Roles', 'Role'):
                if key in volume:
                    metadata[key] = volume[key]
            metadata.setdefault('VolumeName', volume.get('Name'))
            metadata.setdefault('Size', nodes.get(reference, {}).get('Size', ceiling))
            nodes[identifier] = metadata
    root_identifier = root_info.get('DeviceIdentifier')
    if valid_identifier(root_identifier):
        if root_identifier in nodes:
            nodes[root_identifier].update(root_info)
        elif root_info.get('APFSSnapshot') is True:
            reference = root_info.get('APFSContainerReference')
            size = root_info.get('Size') or root_info.get('TotalSize') or nodes.get(reference, {}).get('Size')
            if _valid_size(size):
                nodes[root_identifier] = {**root_info, 'Size': size}

    auxiliary_roles = {'Preboot', 'Recovery', 'VM', 'Update', 'xART', 'Hardware', 'iSCPreboot', 'iSCRecovery'}
    whole_nodes = {identifier: node for identifier, node in nodes.items() if re.fullmatch('disk[0-9]+', identifier)}
    if len(system_ids) == 1 and _explicit_internal(root_info) is not None:
        whole = whole_nodes.get(next(iter(system_ids)))
        if whole is not None and _explicit_internal(whole) is None:
            whole['Internal'] = _explicit_internal(root_info)
    devices = []
    for identifier, original in nodes.items():
        node = dict(original)
        size = node.get('Size')
        if not _valid_size(size):
            continue
        references = contexts.get(identifier, set())
        if len(references) == 1:
            node.setdefault('APFSContainerReference', next(iter(references)))
        if identifier in container_ids:
            node.setdefault('APFSContainerReference', identifier)
        try:
            if len(references) > 1:
                raise DiskError('Ambiguous presentation container')
            physical_ids = ({_disk_identifier(identifier)} if identifier in stores
                            else _mac_physical_ids(node, apfs))
        except DiskError:
            physical_ids = set()
        roles = _mac_roles(node)
        fs = (_filesystem(node.get('FilesystemType')) or _filesystem(node.get('FileSystemType'))
              or _filesystem(node.get('Content')) or ('APFS' if references or identifier in container_ids
                  or 'APFSPhysicalStores' in node or node.get('APFSSnapshot') is True else ''))
        if not physical_ids:
            kind = 'unknown'
        elif node.get('APFSSnapshot') is True:
            kind = 'snapshot'
        elif identifier in container_ids:
            kind = 'apfs_container'
        elif references:
            kind = 'auxiliary' if roles and set(roles) <= auxiliary_roles else 'apfs_volume'
        elif re.fullmatch('disk[0-9]+', identifier):
            kind = 'physical'
        else:
            kind = 'partition'
        internal_flags = [_explicit_internal(node)] + [_explicit_internal(whole_nodes.get(parent, {})) for parent in physical_ids]
        internal_flags = [flag for flag in internal_flags if flag is not None]
        internal = (internal_flags[0] if internal_flags and all(flag is internal_flags[0] for flag in internal_flags) else None)
        removable = any(item.get('Ejectable') is True or item.get('RemovableMedia') is True
                        for item in [node] + [whole_nodes.get(parent, {}) for parent in physical_ids])
        mount = node.get('MountPoint')
        devices.append(DiskDevice('/dev/' + identifier,
            str(node.get('VolumeName') or node.get('Name') or node.get('Content') or identifier), size,
            identifier == root_identifier or bool(physical_ids & system_ids),
            (mount,) if isinstance(mount, str) else (),
            tuple('/dev/' + parent for parent in sorted(physical_ids)), kind, fs, internal, removable, roles))
    return devices


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
