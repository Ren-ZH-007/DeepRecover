from dataclasses import dataclass
import os
import re
import shutil
import subprocess
import shlex
import sys
from pathlib import PurePosixPath

class AdbError(RuntimeError):
    pass

@dataclass(frozen=True)
class AndroidDevice:
    serial: str
    state: str
    model: str = ""

    @property
    def authorized(self):
        return self.state == "device"

def parse_devices(output):
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        fields = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        state = "no permissions" if parts[1:3] == ["no", "permissions"] else parts[1]
        devices.append(AndroidDevice(parts[0], state, fields.get("model", "").replace("_", " ")))
    return devices

class AndroidAdapter:
    def __init__(self, executable=None, runner=subprocess.run):
        self.executable = executable or os.environ.get("DEEPRECOVER_ADB") or shutil.which("adb")
        if not self.executable and sys.platform == "darwin":
            # Finder-launched apps do not inherit the user's shell PATH.
            # Respect configured tools and PATH before trying Homebrew prefixes.
            self.executable = next((path for path in ("/opt/homebrew/bin/adb", "/usr/local/bin/adb")
                                    if os.path.isfile(path) and os.access(path, os.X_OK)), None)
        self.runner = runner

    def devices(self):
        if not self.executable:
            raise AdbError("未找到 ADB。安装 Android Platform Tools 后加入 PATH，或设置 DEEPRECOVER_ADB。")
        try:
            result = self.runner([str(self.executable), "devices", "-l"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as error:
            raise AdbError("ADB 检测超时，请检查连接后重试。") from error
        except OSError as error:
            raise AdbError("ADB 无法启动，请检查可执行文件路径。") from error
        if result.returncode:
            raise AdbError("ADB 返回异常，请检查驱动和 USB 连接。")
        return parse_devices(result.stdout)

    def require_authorized(self, serial):
        if not serial or any(c in serial for c in "\r\n\x00"):
            raise AdbError("请选择有效设备。")
        if not any(d.serial == serial and d.authorized for d in self.devices()):
            raise AdbError("所选设备未授权或已断开；请在手机上允许 USB 调试后重试。")

    @staticmethod
    def shared_path(path):
        if not isinstance(path, str) or any(ord(c) < 32 or ord(c) == 127 for c in path):
            raise AdbError("文件路径包含不支持的控制字符。")
        parsed = PurePosixPath(path)
        if not parsed.is_absolute() or '..' in parsed.parts or path.startswith('//'):
            raise AdbError("仅支持共享存储中的绝对路径。")
        if not any(parsed == root or root in parsed.parents for root in
                   (PurePosixPath('/sdcard'), PurePosixPath('/storage/emulated/0'))):
            raise AdbError("仅支持 /sdcard 或 /storage/emulated/0 下的普通文件。")
        return str(parsed)

    def _device_command(self, serial, arguments, timeout=15):
        try:
            result = self.runner([str(self.executable), '-s', serial, *arguments], capture_output=True,
                                 text=True, encoding='utf-8', errors='strict', timeout=timeout,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
            raise AdbError("ADB 操作未完成，请检查连接、路径权限或重试。") from error
        if result.returncode:
            raise AdbError("ADB 操作失败；请检查设备授权、文件及共享目录权限。")
        return result.stdout

    def _check_shared_target(self, serial, path):
        # /sdcard itself is commonly an alias. Resolve it on the device and
        # reject nested aliases escaping shared storage before reading bytes.
        resolved = self._device_command(serial, ['shell', f'readlink -f {shlex.quote(path)}']).rstrip('\r\n')
        self.shared_path(resolved)

    def list_files(self, serial, root, token=None, limit=10000):
        root = self.shared_path(root)
        self.require_authorized(serial)
        if token:
            token.checkpoint()
        self._check_shared_target(serial, root)
        # A single quoted remote command prevents adb's remote shell from
        # interpreting spaces/metacharacters. Never follow nested symlinks.
        output = self._device_command(serial, ['shell', f'find {shlex.quote(root + "/")} -type f -print0'])
        if token:
            token.checkpoint()
        if output and not output.endswith('\x00'):
            raise AdbError("设备返回的文件列表格式不受支持，未采集文件。")
        paths = []
        for item in output.split('\x00'):
            if not item:
                continue
            path = self.shared_path(item)
            if not PurePosixPath(path).is_relative_to(PurePosixPath(root)):
                raise AdbError("设备返回了所选目录以外的路径。")
            paths.append(path)
            if len(paths) > limit:
                raise AdbError("目录超过 10,000 个文件，请选择更小的子目录；未显示截断列表。")
        return sorted(set(paths))

    def modified_time(self, serial, remote_path):
        """Best-effort ``stat`` mtime in UTC seconds; never fails an acquisition."""
        try:
            quoted = shlex.quote(self.shared_path(remote_path))
            output = self._device_command(serial, ['shell', f'stat -c %Y {quoted}'])
        except AdbError:
            return None
        text = output.strip()
        return int(text) if re.fullmatch(r'\d{1,12}', text) else None

    def pull_file(self, serial, remote_path, destination, token=None):
        remote_path = self.shared_path(remote_path)
        self.require_authorized(serial)
        if token:
            token.checkpoint()
        quoted = shlex.quote(remote_path)
        self._check_shared_target(serial, remote_path)
        self._device_command(serial, ['shell', f'test -f {quoted} && test ! -L {quoted}'])
        self._device_command(serial, ['pull', remote_path, str(destination)], timeout=60)
        if token:
            token.checkpoint()
