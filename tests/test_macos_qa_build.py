"""Adversarial build-gate checks; native app acceptance stays with macOS CI."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib


SPEC = importlib.util.spec_from_file_location(
    'verify_macos_qa_build', Path(__file__).resolve().parents[1] / 'scripts/verify_macos_build.py')
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def png_chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def png(width=1, height=1, pixels=None):
    pixels = pixels if pixels is not None else b'\0\x33\x66\x99'
    return (b'\x89PNG\r\n\x1a\n'
            + png_chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + png_chunk(b'IDAT', zlib.compress(pixels)) + png_chunk(b'IEND', b''))


def icns():
    image = png(128, 128, (b'\0' + b'\x33\x66\x99' * 128) * 128)
    return struct.pack('>4sI4sI', b'icns', 16 + len(image), b'ic07', 8 + len(image)) + image


class MacOSBuildQaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.screenshot = self.root / 'overview.png'
        self.report = self.root / 'smoke.json'
        self.bundle = self.root / 'DeepRecover.app'
        resources = self.bundle / 'Contents/Resources'
        resources.mkdir(parents=True)
        self.icon = resources / 'DeepRecover.icns'
        self.icon.write_bytes(icns())
        self.info = {'CFBundleIconFile': self.icon.name}
        self.executable = self.bundle / 'Contents/MacOS/DeepRecover'
        self.payload = {'success': True, 'version': '0.3.0', 'schema_version': 3,
                        'navigation_pages': 8, 'verified_exports': 2,
                        'decoded_formats': ['PNG', 'JPEG'], 'qt_platform': 'cocoa',
                        'native_quit_verified': True}
        self.payload.update(computer_candidates=2, computer_verified_exports=2)
        self.payload.update(workspace_dialog_verified=True, workspace_roundtrip_verified=True)
        self.exit_code = 0
        self.screenshot_bytes = png(10, 10, (b'\0' + bytes(range(30))) * 10)
        self.launches = []
        self.profile_directories = []

    def process(self, command, **kwargs):
        if command[0] == '/usr/sbin/sysctl':
            return SimpleNamespace(returncode=1, stdout='', stderr='')
        self.launches.append(command)
        # Model only the child-process boundary; no fixture is called a native app.
        Path(command[command.index('--smoke-report') + 1]).write_text(json.dumps(self.payload))
        Path(command[command.index('--smoke-screenshot') + 1]).write_bytes(self.screenshot_bytes)
        profile = Path(kwargs['cwd'])
        self.profile_directories.append(profile)
        self.assertTrue(profile.is_dir())
        for name in ('DEEPRECOVER_DATA_DIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                     'XDG_DATA_HOME', 'CFFIXED_USER_HOME'):
            self.assertTrue(Path(kwargs['env'][name]).is_relative_to(profile))
        return SimpleNamespace(returncode=self.exit_code, stdout='child output', stderr='')

    def run_child(self):
        with patch.object(verify.platform, 'system', return_value='Darwin'), \
                patch.object(verify.platform, 'machine', return_value='arm64'), \
                patch.object(verify, 'validate_bundle', return_value=(self.executable, self.info)), \
                patch.object(verify.subprocess, 'run', side_effect=self.process):
            return verify.run_smoke(self.bundle, self.report, '0.3.0', 'arm64',
                                    'cocoa', self.screenshot)

    def test_png_signature_and_file_size_cannot_substitute_for_a_screenshot(self):
        self.screenshot.write_bytes(b'\x89PNG\r\n\x1a\n' + b'not image data' * 20)
        with self.assertRaises(RuntimeError):
            verify.verify_screenshot(self.screenshot)

    def test_truncated_png_and_even_ignored_iend_crc_damage_are_rejected(self):
        image = self.screenshot_bytes
        for damaged in (image[:-12], image[:-1] + bytes([image[-1] ^ 1]), image[:50] + b'x' * 100):
            with self.subTest(damage=damaged[-12:]):
                self.screenshot.write_bytes(damaged)
                with self.assertRaises(RuntimeError):
                    verify.verify_screenshot(self.screenshot)

    def test_valid_chunk_checksums_do_not_substitute_for_pixel_decoding(self):
        self.screenshot.write_bytes(png(10, 10, b'\0\x33\x66\x99'))
        with self.assertRaises(RuntimeError):
            verify.verify_screenshot(self.screenshot)

    def test_small_valid_png_does_not_need_arbitrary_padding(self):
        self.screenshot.write_bytes(png())
        self.assertLess(self.screenshot.stat().st_size, 100)
        verify.verify_screenshot(self.screenshot)

    def test_corrupt_icon_does_not_pass_with_a_successful_child_report(self):
        # A corrupt resource can be ad-hoc signed and still leave the app runnable.
        self.icon.write_bytes(b'icns' + b'\0' * 128)
        with self.assertRaises(RuntimeError):
            self.run_child()
        self.assertEqual(self.launches, [])

    def test_icns_container_lengths_and_actual_image_data_are_checked(self):
        for image in (b'', b'garbage', struct.pack('>4sI', b'icns', 8),
                      struct.pack('>4sI4sI', b'icns', 16, b'ic07', 0), icns()[:-1]):
            with self.subTest(icon=image[:16]):
                self.icon.write_bytes(image)
                with self.assertRaises(RuntimeError):
                    verify.verify_icon(self.icon)
        self.icon.write_bytes(icns())
        verify.verify_icon(self.icon)

    def test_a_new_correct_report_cannot_hide_a_nonzero_child_exit(self):
        self.exit_code = 7
        with self.assertRaisesRegex(RuntimeError, 'exited with 7'):
            self.run_child()
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.report.with_suffix('.stdout.txt').read_text(), 'child output')

    def test_stale_success_report_is_rejected_without_launching_the_app(self):
        self.report.write_text(json.dumps(self.payload))
        with self.assertRaisesRegex(RuntimeError, 'must not pre-exist'):
            self.run_child()
        self.assertEqual(self.launches, [])

    def test_zero_exit_with_forged_fields_or_corrupt_screenshot_is_rejected(self):
        self.payload['verified_exports'] = True
        with self.assertRaisesRegex(RuntimeError, 'verified_exports'):
            self.run_child()
        self.report.unlink()
        self.screenshot.unlink()
        self.payload['verified_exports'] = 2
        self.screenshot_bytes = b'\x89PNG\r\n\x1a\n' + b'X' * 200
        with self.assertRaises(RuntimeError):
            self.run_child()

    def test_valid_outputs_are_accepted_and_temporary_profile_is_removed(self):
        result = self.run_child()
        self.assertEqual(result, self.payload)
        self.assertEqual(len(self.launches), 1)
        self.assertTrue(self.screenshot.is_file())
        self.assertTrue(all(not directory.exists() for directory in self.profile_directories))

    def test_wrong_runner_architecture_is_rejected_before_bundle_or_app_execution(self):
        with patch.object(verify.platform, 'system', return_value='Darwin'), \
                patch.object(verify.platform, 'machine', return_value='x86_64'), \
                patch.object(verify, 'validate_bundle') as bundle_check, \
                patch.object(verify.subprocess, 'run') as process:
            with self.assertRaisesRegex(RuntimeError, 'matching native macOS'):
                verify.run_smoke(self.bundle, self.report, '0.3.0', 'arm64', 'offscreen')
        bundle_check.assert_not_called()
        process.assert_not_called()

    def test_rosetta_is_rejected_before_bundle_or_app_execution(self):
        with patch.object(verify.platform, 'system', return_value='Darwin'), \
                patch.object(verify.platform, 'machine', return_value='x86_64'), \
                patch.object(verify, 'validate_bundle') as bundle_check, \
                patch.object(verify.subprocess, 'run',
                             return_value=SimpleNamespace(returncode=0, stdout='1\n')) as process:
            with self.assertRaisesRegex(RuntimeError, 'Rosetta'):
                verify.run_smoke(self.bundle, self.report, '0.3.0', 'x86_64', 'offscreen')
        bundle_check.assert_not_called()
        self.assertEqual(process.call_count, 1)


if __name__ == '__main__':
    unittest.main()
