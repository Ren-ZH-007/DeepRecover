"""Validate native macOS disk metadata only; never open or scan raw devices.

The report records schema names and counts, not disk identifiers, UUIDs, serials
or source paths. Same-disk rejection is checked against the root volume itself;
the runner's workspace may legitimately live on another disk.
"""
import argparse
import json
from pathlib import Path
import platform
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.computer import devices
from core.computer.io import DiskError


SAME_DEVICE_ERROR = '工作目录和导出目标必须位于另一块物理磁盘，不能写回来源磁盘。'


def _store_schema(stores):
    entries = devices._array(stores)
    keys = {'DeviceIdentifier', 'APFSPhysicalStore'}
    return {'entry_types': sorted({type(entry).__name__ for entry in entries}),
            'identifier_keys': sorted({key for entry in entries if isinstance(entry, dict)
                                       for key in keys if key in entry})}


def _info_schema(info):
    volume = info.get('APFSVolumeDisk')
    return {'physical_stores': _store_schema(info.get('APFSPhysicalStores')),
            'volume_disk_value_type': type(volume).__name__,
            'snapshot': info.get('APFSSnapshot') is True}


def verify_metadata(work_dir=None):
    if sys.platform != 'darwin':
        raise RuntimeError('Native disk metadata validation requires macOS.')
    apfs = devices._mac_apfs()
    root_info = devices._mac_info('/')
    if not (root_info.get('APFSPhysicalStores') or root_info.get('APFSContainerReference')
            or root_info.get('APFSVolumeDisk')
            or str(root_info.get('FilesystemType', '')).lower() == 'apfs'):
        raise RuntimeError('The native runner does not expose APFS root metadata.')
    root_ids = devices._mac_physical_ids(root_info, apfs)
    existing = devices._existing_ancestor(Path.cwd() if work_dir is None else work_dir)
    work_info = devices._mac_info(devices._mac_volume_path(existing))
    work_ids = devices._mac_physical_ids(work_info, apfs)
    if not root_ids or not work_ids:
        raise RuntimeError('Native disk metadata mapping is empty.')
    identifier = root_info.get('DeviceIdentifier')
    if not isinstance(identifier, str):
        raise RuntimeError('Native root volume identifier is missing.')
    try:
        devices.ensure_different_device('/dev/' + identifier, Path('/'))
    except DiskError as error:
        # A mapping/permission failure is not proof of same-disk protection.
        if str(error) != SAME_DEVICE_ERROR:
            raise RuntimeError('Native same-disk guard could not confirm the mapping.') from error
    else:
        raise RuntimeError('Native same-disk guard unexpectedly allowed the root volume.')
    stores = [store for container in devices._array(apfs.get('Containers'))
              if isinstance(container, dict)
              for store in devices._array(container.get('PhysicalStores'))]
    return {'success': True, 'platform': sys.platform, 'architecture': platform.machine(),
            'metadata_queries_only': True, 'raw_reads': False,
            'root_physical_disk_count': len(root_ids),
            'work_directory_physical_disk_count': len(work_ids),
            'root_and_work_directory_overlap': bool(root_ids & work_ids),
            'same_device_rejected': True,
            'schemas': {'root': _info_schema(root_info),
                        'work_directory': _info_schema(work_info),
                        'apfs_list_physical_stores': _store_schema(stores)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    report = verify_metadata()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
