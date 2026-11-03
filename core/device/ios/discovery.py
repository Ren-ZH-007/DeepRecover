from dataclasses import dataclass
from pathlib import Path
import os
import plistlib
import sys

@dataclass(frozen=True)
class Backup:
    path: Path
    device_name: str
    product_version: str
    encrypted: bool | None
    date: str
    error: str = ""

def default_roots():
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    roots = [home / "Apple/MobileSync/Backup"]
    if os.environ.get("APPDATA"):
        roots.append(Path(os.environ["APPDATA"]) / "Apple Computer/MobileSync/Backup")
    finder_root = Path.home() / "Library/Application Support/MobileSync/Backup"
    if sys.platform == "darwin":
        roots.insert(0, finder_root)
    else:
        roots.append(finder_root)
    return roots

def inspect_backup(path):
    path = Path(path).resolve()
    try:
        with (path / "Manifest.plist").open("rb") as stream:
            manifest = plistlib.load(stream)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("IsEncrypted"), bool):
            raise ValueError("Missing encryption metadata")
        info = {}
        if (path / "Info.plist").is_file():
            with (path / "Info.plist").open("rb") as stream:
                info = plistlib.load(stream)
        return Backup(path, str(info.get("Device Name", path.name)), str(info.get("Product Version", "")),
                      manifest["IsEncrypted"], str(info.get("Last Backup Date", "")))
    except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
        return Backup(path, path.name, "", None, "", "备份元数据缺失或损坏")

def discover_backups(roots=None):
    found = {}
    for root in default_roots() if roots is None else roots:
        root = Path(root)
        if not root.is_dir():
            continue
        candidates = [root] if (root / "Manifest.plist").exists() else sorted(root.iterdir())
        for path in candidates:
            if path.is_dir() and ((path / "Manifest.plist").exists() or (path / "Manifest.db").exists()):
                backup = inspect_backup(path)
                found[backup.path] = backup
    return list(found.values())
