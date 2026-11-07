"""Release gate: preserve failures and allow only documented Windows skips."""
import argparse
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

WINDOWS_SKIPS = {
    'test_export.ExportTests.test_replaced_target_is_neither_verified_nor_deleted_as_owned_output',
    'test_qa_regressions.DetailedQaRegressions.test_manifest_symlink_cannot_escape_selected_backup',
    # Opens a macOS /dev path; the hint text itself is tested on every platform.
    'test_live_space.PermissionHintTests.test_denied_device_open_explains_what_to_do',
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover('tests')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    allowed = WINDOWS_SKIPS if sys.platform == 'win32' else set()
    skips = [{'test': case.id(), 'reason': reason} for case, reason in result.skipped]
    unexpected = [item for item in skips if item['test'] not in allowed]
    success = result.wasSuccessful() and result.testsRun >= 100 and not unexpected and not result.expectedFailures
    payload = {
        'success': success,
        'platform': sys.platform,
        'run': result.testsRun,
        'passed': result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped) - len(result.expectedFailures) - len(result.unexpectedSuccesses),
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skipped': skips,
        'unexpected_skips': unexpected,
        'expected_failures': len(result.expectedFailures),
        'unexpected_successes': len(result.unexpectedSuccesses),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
