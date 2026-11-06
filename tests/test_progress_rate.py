import unittest

from core.progress_rate import (MAX_ETA_SECONDS, RateEstimator, RateView, describe_rate,
                                format_bytes, format_duration)
from core.tasks import Progress


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


MB = 1024 * 1024


class RateEstimatorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(); self.rate = RateEstimator(clock=self.clock)

    def feed(self, key, processed, total, at, phase='scan'):
        self.clock.now = at
        return self.rate.update(key, Progress(processed, total, 0, phase))

    def test_estimating_until_minimum_elapsed_time(self):
        self.assertEqual(self.feed('a', 0, 100 * MB, 100.0), RateView(None, None))
        self.assertEqual(self.feed('a', 5 * MB, 100 * MB, 101.0), RateView(None, None))
        view = self.feed('a', 20 * MB, 100 * MB, 102.0)
        self.assertAlmostEqual(view.rate, 10 * MB)
        self.assertAlmostEqual(view.remaining, 8.0)

    def test_window_tracks_recent_speed_not_whole_run_average(self):
        for second in range(0, 21):  # 1 MB/s for 20 s
            self.feed('a', second * MB, 1000 * MB, 100.0 + second)
        for second in range(1, 11):  # then 10 MB/s for 10 s
            view = self.feed('a', (20 + 10 * second) * MB, 1000 * MB, 120.0 + second)
        self.assertGreater(view.rate, 9 * MB)
        self.assertAlmostEqual(view.remaining, (1000 - 120) * MB / view.rate)

    def test_stalled_progress_gives_no_eta(self):
        self.feed('a', 10, 100, 100.0)
        view = self.feed('a', 10, 100, 105.0)
        self.assertTrue(view.stalled); self.assertIsNone(view.remaining)
        self.assertEqual(describe_rate(view), '暂无进展，无法估算剩余时间')

    def test_new_task_phase_total_or_backwards_progress_resets(self):
        self.feed('a', 0, 100, 100.0); self.feed('a', 50, 100, 105.0)
        self.assertEqual(self.feed('b', 60, 100, 106.0), RateView(None, None))
        self.feed('b', 80, 100, 110.0)
        self.assertEqual(self.feed('b', 80, 100, 110.5, phase='export'), RateView(None, None))
        self.assertEqual(self.feed('b', 10, 200, 111.0, phase='export'), RateView(None, None))
        self.feed('b', 50, 200, 115.0, phase='export')
        self.assertEqual(self.feed('b', 5, 200, 116.0, phase='export'), RateView(None, None))

    def test_missing_or_empty_progress_is_ignored(self):
        self.assertIsNone(self.rate.update('a', None))
        self.assertIsNone(self.rate.update('a', Progress(0, 0, 0)))
        self.assertEqual(describe_rate(None), '')

    def test_eta_is_capped_for_extremely_slow_sources(self):
        self.feed('a', 0, 10 ** 15, 100.0)
        view = self.feed('a', 1, 10 ** 15, 110.0)
        self.assertGreater(view.remaining, MAX_ETA_SECONDS)
        self.assertIn('超过 99 小时', describe_rate(view))

    def test_invalid_window_is_rejected(self):
        with self.assertRaises(ValueError): RateEstimator(window=1, min_elapsed=2)
        with self.assertRaises(ValueError): RateEstimator(min_elapsed=0)


class FormattingTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(format_bytes(0), '0 B')
        self.assertEqual(format_bytes(1023), '1023 B')
        self.assertEqual(format_bytes(1536), '1.5 KB')
        self.assertEqual(format_bytes(250 * MB), '250 MB')
        self.assertEqual(format_bytes(int(1.25 * 1024 ** 3)), '1.2 GB')
        self.assertEqual(format_bytes(2 * 1024 ** 4), '2.0 TB')

    def test_duration(self):
        self.assertEqual(format_duration(0.2), '约 1 秒')
        self.assertEqual(format_duration(42), '约 42 秒')
        self.assertEqual(format_duration(200), '约 3 分 20 秒')
        self.assertEqual(format_duration(180), '约 3 分钟')
        self.assertEqual(format_duration(25 * 60 + 40), '约 26 分钟')
        self.assertEqual(format_duration(2 * 3600 + 5 * 60), '约 2 小时 5 分')
        self.assertEqual(format_duration(3 * 3600), '约 3 小时')
        self.assertEqual(format_duration(MAX_ETA_SECONDS + 1), '超过 99 小时')

    def test_describe_units(self):
        self.assertEqual(describe_rate(RateView(None, None)), '正在估算剩余时间…')
        self.assertEqual(describe_rate(RateView(10 * MB, 90)), '速度 10.0 MB/s · 预计剩余约 1 分 30 秒')
        self.assertEqual(describe_rate(RateView(2.5, 8), 'items'), '速度 2.5 项/秒 · 预计剩余约 8 秒')
        self.assertEqual(describe_rate(RateView(10 * MB, 0.3)), '速度 10.0 MB/s · 即将完成')


if __name__ == '__main__':
    unittest.main()
