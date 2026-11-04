"""Bounded read-only access to image files and explicitly selected OS disks.

Raw-device access never requests write access, elevation, locking or unmounting.
Identity checks detect replacement/geometry changes, not a live-disk snapshot.
"""
import ctypes
import os
from pathlib import Path
import re
import stat
import struct
import sys
from threading import Lock


class DiskError(RuntimeError):
    pass


MAX_READ = 32 * 1024 * 1024
WINDOWS_DEVICE = re.compile(r"\\\\\.\\(?:PhysicalDrive([0-9]+)|([A-Za-z]):)", re.IGNORECASE)
MAC_DEVICE = re.compile(r"/dev/(r?disk[0-9]+(?:s[0-9]+)*)")


def normalize_device(path):
    value = str(path)
    if sys.platform == 'win32':
        match = WINDOWS_DEVICE.fullmatch(value)
        if not match:
            raise DiskError('仅允许本机 PhysicalDrive 或盘符卷设备，不支持其他设备路径。')
        return (rf'\\.\PhysicalDrive{int(match.group(1))}' if match.group(1) is not None
                else rf'\\.\{match.group(2).upper()}:')
    if sys.platform == 'darwin':
        if not MAC_DEVICE.fullmatch(value):
            raise DiskError('仅允许 /dev/disk 或 /dev/rdisk 的本机磁盘设备。')
        resolved = str(Path(value).resolve())
        if not MAC_DEVICE.fullmatch(resolved):
            raise DiskError('磁盘路径解析后不是受支持的本机磁盘设备。')
        return resolved
    raise DiskError('此平台仅支持镜像文件；原始磁盘读取需要 Windows 或 macOS。')


class _WinAPI:
    """Small ctypes boundary; all IOCTLs below are queries, never mutations."""
    def __init__(self):
        from ctypes import wintypes
        self.types = wintypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self.kernel.DeviceIoControl.restype = wintypes.BOOL
        self.kernel.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
        self.kernel.SetFilePointerEx.restype = wintypes.BOOL
        self.kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self.kernel.ReadFile.restype = wintypes.BOOL

    def open(self, path, *, read=True):
        # GENERIC_READ or metadata-only access; FILE_SHARE_READ|FILE_SHARE_WRITE
        # does not grant this process any write access to the device.
        handle = self.kernel.CreateFileW(str(path), 0x80000000 if read else 0,
            0x00000001 | 0x00000002, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def close(self, handle):
        self.kernel.CloseHandle(handle)

    def ioctl(self, handle, code, data=b'', size=65536):
        output = ctypes.create_string_buffer(size)
        source = ctypes.create_string_buffer(data) if data else None
        returned = self.types.DWORD()
        if not self.kernel.DeviceIoControl(handle, code, source, len(data), output,
                                          size, ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return output.raw[:returned.value]

    def read(self, handle, offset, length):
        if not self.kernel.SetFilePointerEx(handle, offset, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(length)
        returned = self.types.DWORD()
        if not self.kernel.ReadFile(handle, buffer, length, ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw[:returned.value]


class _WindowsBackend:
    def __init__(self, path):
        self.api = _WinAPI()
        self.handle = self.api.open(path, read=True)
        self.path = path
        try:
            self.identity = self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        # IOCTL_DISK_GET_DRIVE_GEOMETRY_EX: DISK_GEOMETRY (24 bytes), DiskSize.
        geometry = self.api.ioctl(self.handle, 0x000700A0)
        if len(geometry) < 32:
            raise DiskError('无法读取完整磁盘几何信息。')
        sector = struct.unpack_from('<I', geometry, 20)[0]
        size = struct.unpack_from('<Q', geometry, 24)[0]
        # Length can differ for a volume handle. GET_LENGTH_INFO is authoritative.
        length = self.api.ioctl(self.handle, 0x0007405C, size=8)
        if len(length) != 8:
            raise DiskError('无法读取磁盘或卷长度。')
        size = struct.unpack('<Q', length)[0]
        number = self.api.ioctl(self.handle, 0x002D1080, size=12)
        if len(number) != 12:
            raise DiskError('无法确认磁盘设备身份。')
        device_type, device_number, partition_number = struct.unpack('<III', number)
        return {'kind': 'device', 'path': self.path, 'size': size, 'sector_size': sector,
                'device_type': device_type, 'device_number': device_number,
                'partition_number': partition_number}

    def read_at(self, offset, size):
        return self.api.read(self.handle, offset, size)

    def close(self):
        if self.handle is not None:
            self.api.close(self.handle)
            self.handle = None


def windows_volume_disk_numbers(path):
    """Query every physical extent of a Windows volume, including spanned ones."""
    api = _WinAPI()
    handle = api.open(path, read=False)
    try:
        # IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, 8-byte aligned DISK_EXTENT array.
        payload = api.ioctl(handle, 0x00560000, size=65536)
        if len(payload) < 8:
            raise DiskError('无法确定卷所在的物理磁盘。')
        count = struct.unpack_from('<I', payload)[0]
        if not 1 <= count <= 256 or len(payload) < 8 + count * 24:
            raise DiskError('卷的物理磁盘映射不完整。')
        return {struct.unpack_from('<I', payload, 8 + index * 24)[0] for index in range(count)}
    finally:
        api.close(handle)


def _mac_geometry(descriptor):
    import fcntl
    # Darwin sys/disk.h: DKIOCGETBLOCKSIZE, DKIOCGETBLOCKCOUNT (read queries).
    block = bytearray(4)
    count = bytearray(8)
    fcntl.ioctl(descriptor, 0x40046418, block, True)
    fcntl.ioctl(descriptor, 0x40086419, count, True)
    sector = struct.unpack('=I', block)[0]
    blocks = struct.unpack('=Q', count)[0]
    return sector * blocks, sector


def _image_identity(info):
    # Lazy import avoids a cycle with source-bound candidate IO.
    from core.candidate_io import snapshot
    return {'kind': 'image', **snapshot(info)}


class DiskReader:
    def __init__(self, path, kind='image'):
        if kind not in ('image', 'device'):
            raise DiskError('未知电脑数据源类型。')
        self.kind = kind
        self.path = Path(path).expanduser().resolve() if kind == 'image' else normalize_device(path)
        self.size = self.sector_size = 0
        self.identity = None
        self._descriptor = None
        self._windows = None
        self._position = 0
        self._lock = Lock()

    def __enter__(self):
        if self._descriptor is not None or self._windows is not None:
            raise DiskError('磁盘读取器已打开。')
        try:
            if self.kind == 'device' and sys.platform == 'win32':
                self._windows = _WindowsBackend(self.path)
                current = self._windows.identity
            else:
                self._descriptor = os.open(self.path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
                info = os.fstat(self._descriptor)
                if self.kind == 'image':
                    if not stat.S_ISREG(info.st_mode):
                        raise DiskError('镜像来源必须是普通文件，不接受目录或原始设备。')
                    current = _image_identity(info)
                    current['sector_size'] = 512
                else:
                    if not (stat.S_ISBLK(info.st_mode) or stat.S_ISCHR(info.st_mode)):
                        raise DiskError('所选来源不是磁盘设备。')
                    size, sector = _mac_geometry(self._descriptor)
                    current = {'kind': 'device', 'path': str(self.path), 'size': size,
                               'sector_size': sector, 'rdev': info.st_rdev}
            size, sector = current['size'], current['sector_size']
            if type(size) is not int or size < 0 or type(sector) is not int or sector not in (512, 1024, 2048, 4096, 8192):
                raise DiskError('磁盘长度或扇区大小不受支持。')
            if self.kind == 'device' and (not size or size % sector):
                raise DiskError('磁盘几何信息不一致。')
            self.size, self.sector_size, self.identity = size, sector, current
            self.check()
            return self
        except BaseException as error:
            self.close()
            if isinstance(error, OSError):
                raise DiskError('无法只读打开来源；请检查路径、系统权限和设备连接。') from error
            raise

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._windows is not None:
            self._windows.close()
            self._windows = None
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def check(self, expected=None):
        if self._windows is not None:
            current = self._windows.check()
        elif self._descriptor is not None:
            info = os.fstat(self._descriptor)
            actual = os.stat(self.path)
            if self.kind == 'image':
                if not stat.S_ISREG(info.st_mode) or _image_identity(info) != _image_identity(actual):
                    raise DiskError('镜像路径或文件身份已变化。')
                current = {**_image_identity(info), 'sector_size': 512}
            else:
                size, sector = _mac_geometry(self._descriptor)
                if not (stat.S_ISBLK(actual.st_mode) or stat.S_ISCHR(actual.st_mode)) or actual.st_rdev != info.st_rdev:
                    raise DiskError('磁盘路径对应的设备已变化。')
                current = {'kind': 'device', 'path': str(self.path), 'size': size,
                           'sector_size': sector, 'rdev': info.st_rdev}
        else:
            raise DiskError('磁盘读取器尚未打开或已经关闭。')
        if current != self.identity or (expected is not None and current != expected):
            raise DiskError('来源身份或几何信息已变化，请重新扫描。')
        return dict(current)

    def read_at(self, offset, size):
        if type(offset) is not int or type(size) is not int or offset < 0 or size < 0 or offset + size > self.size:
            raise OSError('读取范围超出来源边界。')
        if size > MAX_READ:
            raise OSError('单次读取超过有界读取上限。')
        if self._descriptor is None and self._windows is None:
            raise OSError('磁盘读取器未打开。')
        if not size:
            return b''
        start, length = offset, size
        if self.kind == 'device':
            start = offset // self.sector_size * self.sector_size
            end = (offset + size + self.sector_size - 1) // self.sector_size * self.sector_size
            length = end - start
        with self._lock:
            if self._windows is not None:
                data = self._windows.read_at(start, length)
            elif hasattr(os, 'pread'):
                data = os.pread(self._descriptor, length, start)
            else:
                os.lseek(self._descriptor, start, os.SEEK_SET)
                data = os.read(self._descriptor, length)
        if len(data) != length:
            raise OSError('来源发生短读或已断开，未返回不完整范围。')
        begin = offset - start
        return data[begin:begin + size]

    def seek(self, offset, whence=os.SEEK_SET):
        if type(offset) is not int or whence not in (os.SEEK_SET, os.SEEK_CUR, os.SEEK_END):
            raise OSError('无效的读取位置。')
        position = offset if whence == os.SEEK_SET else (self._position if whence == os.SEEK_CUR else self.size) + offset
        if not 0 <= position <= self.size:
            raise OSError('读取位置超出来源边界。')
        self._position = position
        return position

    def tell(self):
        return self._position

    def read(self, size=-1):
        if type(size) is not int or size < -1:
            raise OSError('无效的读取长度。')
        length = self.size - self._position if size == -1 else min(size, self.size - self._position)
        data = self.read_at(self._position, length)
        self._position += len(data)
        return data
