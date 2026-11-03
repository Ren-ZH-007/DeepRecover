from dataclasses import dataclass
from pathlib import Path
import os
import plistlib
import sys
from xml.parsers.expat import ExpatError

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
    path = Path(path)
    try:
        path = path.resolve()
        manifest_path = _metadata_path(path, "Manifest.plist")
        with manifest_path.open("rb") as stream:
            manifest = plistlib.load(stream)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("IsEncrypted"), bool):
            raise ValueError("Missing encryption metadata")
        info = {}
        info_path = path / "Info.plist"
        if info_path.exists() or info_path.is_symlink():
            with _metadata_path(path, "Info.plist").open("rb") as stream:
                info = plistlib.load(stream)
        return Backup(path, str(info.get("Device Name", path.name)), str(info.get("Product Version", "")),
                      manifest["IsEncrypted"], str(info.get("Last Backup Date", "")))
    except PermissionError:
        return _unavailable_backup(path)
    except _MetadataOutsideBackup:
        return Backup(path, path.name, "", None, "", "备份元数据路径超出所选备份目录，已拒绝读取")
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException, ExpatError):
        return Backup(path, path.name, "", None, "", "备份元数据缺失或损坏")


class _MetadataOutsideBackup(ValueError):
    pass


def _metadata_path(root, name):
    path = (root / name).resolve(strict=True)
    if not path.is_relative_to(root):
        raise _MetadataOutsideBackup(name)
    return path


def _has_metadata(path):
    return any((path / name).exists() or (path / name).is_symlink()
               for name in ("Manifest.plist", "Manifest.db"))


def _unavailable_backup(path, error=None):
    message = ("备份目录或元数据访问被拒绝，请检查系统隐私与文件权限"
               if error is None or isinstance(error, PermissionError) else "备份目录无法访问或已变化")
    return Backup(path.absolute(), path.name, "", None, "", message)


def discover_backups(roots=None):
    found = {}
    for root in default_roots() if roots is None else roots:
        root = Path(root)
        try:
            if not root.is_dir():
                continue
            candidates = [root] if _has_metadata(root) else sorted(root.iterdir())
        except OSError as error:
            backup = _unavailable_backup(root, error)
            found[backup.path] = backup
            continue
        for path in candidates:
            try:
                if not path.is_dir() or not _has_metadata(path):
                    continue
                backup = inspect_backup(path)
            except OSError as error:
                backup = _unavailable_backup(path, error)
            found[backup.path] = backup
    return list(found.values())
