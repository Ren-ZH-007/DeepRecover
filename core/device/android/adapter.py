from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess

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
