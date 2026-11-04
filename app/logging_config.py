import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import stat


def check_log_targets(directory):
    directory = Path(directory).absolute()
    if directory.resolve() != directory or directory.is_symlink():
        raise OSError('日志目录不能重定向到其他位置。')
    for suffix in ('', '.1', '.2', '.3'):
        target = directory / ('app.log' + suffix)
        if (target.is_symlink() or target.resolve() != target or
                (target.exists() and (not target.is_file() or target.stat().st_nlink > 1))):
            raise OSError('日志文件必须是工作目录内的独立普通文件。')
    return directory


class WorkspaceLogHandler(RotatingFileHandler):
    def _open(self):
        directory = check_log_targets(Path(self.baseFilename).parent)
        def opener(path, flags):
            descriptor = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                os.close(descriptor)
                raise OSError('日志文件不是独立普通文件。')
            try:
                check_log_targets(directory)
            except Exception:
                os.close(descriptor)
                raise
            return descriptor
        return open(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors, opener=opener)

    def emit(self, record):
        try:
            check_log_targets(Path(self.baseFilename).parent)
            if self.stream is not None and os.fstat(self.stream.fileno()).st_nlink > 1:
                raise OSError('日志文件出现共享链接，已停止写入。')
        except OSError:
            self.handleError(record)
            return
        super().emit(record)

def configure_logging(directory):
    directory = check_log_targets(directory)
    logger = logging.getLogger("deeprecover")
    logger.setLevel(logging.INFO)
    target = (directory / 'app.log').resolve()
    current = [handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)]
    if not any(handler.baseFilename == str(target) and handler.stream is not None for handler in current):
        handler = WorkspaceLogHandler(target, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        for previous in current:
            logger.removeHandler(previous)
            previous.close()
        logger.addHandler(handler)
    return logger
