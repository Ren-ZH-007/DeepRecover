"""File modification times from on-disk metadata, normalised for storage.

Every source is reduced to ``modified_at`` (``YYYY-MM-DDTHH:MM:SS``, no offset)
and ``modified_basis``: ``utc`` when the source records an absolute time, or
``local`` when it only records a wall-clock time with no zone (FAT32, exFAT
without a valid UTC offset). Implausible values from damaged or reused records
are dropped rather than shown as facts.
"""
from datetime import datetime, timedelta, timezone

EARLIEST = datetime(1980, 1, 1)
LATEST = datetime(2100, 1, 1)
_FILETIME_EPOCH = datetime(1601, 1, 1)


def _result(moment, basis):
    if moment is None or not EARLIEST <= moment < LATEST:
        return {}
    return {'modified_at': moment.replace(microsecond=0).isoformat(), 'modified_basis': basis}


def _dos(date, time, centiseconds=0):
    if not date:
        return None
    try:
        return datetime(1980 + (date >> 9), (date >> 5) & 0x0f, date & 0x1f,
                        time >> 11, (time >> 5) & 0x3f, (time & 0x1f) * 2) + timedelta(milliseconds=10 * centiseconds)
    except ValueError:
        return None


def fat_modified(date, time):
    """FAT directory entry write date/time (local wall clock, 2 s resolution)."""
    return _result(_dos(date, time), 'local')


def exfat_modified(timestamp, centiseconds=0, utc_offset=0):
    """exFAT LastModified timestamp, 10 ms increment and UTC offset byte."""
    moment = _dos(timestamp >> 16, timestamp & 0xffff, centiseconds if 0 <= centiseconds <= 199 else 0)
    if moment is None:
        return {}
    if utc_offset & 0x80:
        quarters = utc_offset & 0x7f
        quarters = quarters - 0x80 if quarters & 0x40 else quarters
        return _result(moment - timedelta(minutes=15 * quarters), 'utc')
    return _result(moment, 'local')


def filetime_modified(value):
    """Windows FILETIME (100 ns ticks since 1601, UTC)."""
    if not isinstance(value, int) or value <= 0:
        return {}
    try:
        return _result(_FILETIME_EPOCH + timedelta(microseconds=value // 10), 'utc')
    except OverflowError:
        return {}


def unix_modified(seconds):
    """Seconds since 1970 in UTC (iOS backup metadata, Android ``stat``)."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return {}
    try:
        return _result(datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None), 'utc')
    except (OverflowError, OSError, ValueError):
        return {}


def display_time(modified_at, basis, *, tz=None):
    """Human text: UTC shown in the viewer's zone, local times flagged as zoneless."""
    if not isinstance(modified_at, str):
        return ''
    try:
        moment = datetime.fromisoformat(modified_at)
    except ValueError:
        return ''
    if moment.tzinfo is not None:
        return ''
    if basis == 'utc':
        return moment.replace(tzinfo=timezone.utc).astimezone(tz).strftime('%Y-%m-%d %H:%M')
    if basis == 'local':
        return moment.strftime('%Y-%m-%d %H:%M') + '（本地，无时区）'
    return ''
