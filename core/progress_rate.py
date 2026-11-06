"""Throughput and remaining-time estimates for task progress (no Qt dependency).

Estimates come from a sliding window of recent samples so a slow start or a
burst of fast sectors does not dominate. Nothing is shown until the window
holds enough elapsed time, and a stalled task reports no ETA rather than a
made-up one.
"""
from collections import deque
from dataclasses import dataclass
import math
from time import monotonic

WINDOW_SECONDS = 10.0
MIN_ELAPSED = 2.0
MAX_ETA_SECONDS = 99 * 3600


@dataclass(frozen=True)
class RateView:
    rate: float | None          # units per second; None while estimating
    remaining: float | None     # seconds; None while estimating or stalled
    stalled: bool = False


class RateEstimator:
    def __init__(self, clock=monotonic, window=WINDOW_SECONDS, min_elapsed=MIN_ELAPSED):
        if not 0 < min_elapsed <= window:
            raise ValueError('Invalid rate window')
        self._clock, self._window, self._min = clock, window, min_elapsed
        self._key = None
        self._samples = deque()

    def reset(self):
        self._key = None
        self._samples.clear()

    def update(self, key, progress):
        """Record ``progress`` for task ``key``; returns a RateView or None."""
        if progress is None or progress.total <= 0:
            return None
        now = self._clock()
        ident = (key, progress.phase, progress.total)
        if ident != self._key or (self._samples and progress.processed < self._samples[-1][1]):
            self._key = ident
            self._samples.clear()
        if not self._samples or self._samples[-1] != (now, progress.processed):
            self._samples.append((now, progress.processed))
        # Keep one sample at or before the window start so the span stays full.
        while len(self._samples) > 2 and self._samples[1][0] <= now - self._window:
            self._samples.popleft()
        first_t, first_p = self._samples[0]
        elapsed = now - first_t
        if elapsed < self._min:
            return RateView(None, None)
        rate = (progress.processed - first_p) / elapsed
        if rate <= 0:
            return RateView(0.0, None, stalled=progress.processed < progress.total)
        remaining = (progress.total - progress.processed) / rate
        return RateView(rate, min(remaining, MAX_ETA_SECONDS + 1))


def format_bytes(value):
    value = max(0, int(value))
    if value < 1024:
        return f'{value} B'
    for unit in ('KB', 'MB', 'GB', 'TB', 'PB'):
        value /= 1024
        if value < 1024 or unit == 'PB':
            return f'{value:.1f} {unit}' if value < 100 else f'{value:.0f} {unit}'


def format_duration(seconds):
    if seconds > MAX_ETA_SECONDS:
        return '超过 99 小时'
    seconds = max(1, math.ceil(seconds))
    if seconds < 60:
        return f'约 {seconds} 秒'
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f'约 {minutes} 分 {sec} 秒' if minutes < 10 and sec else f'约 {minutes + (sec >= 30)} 分钟'
    hours, minutes = divmod(minutes + (sec >= 30), 60)
    return f'约 {hours} 小时 {minutes} 分' if minutes else f'约 {hours} 小时'


def describe_rate(view, unit='bytes'):
    """Short Chinese suffix for a status line, or '' when there is nothing to say."""
    if view is None:
        return ''
    if view.rate is None:
        return '正在估算剩余时间…'
    if view.stalled:
        return '暂无进展，无法估算剩余时间'
    if view.remaining is None:
        return ''
    speed = f'{format_bytes(view.rate)}/s' if unit == 'bytes' else f'{view.rate:.1f} 项/秒'
    return f'速度 {speed} · 预计剩余{format_duration(view.remaining)}' if view.remaining >= 1 else f'速度 {speed} · 即将完成'
