"""Portable checks for macOS bundle verification failure paths (no fake build claim)."""
import importlib.util
import json
from pathlib import Path
import plistlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_scanner import png_image

MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts/verify_macos_build.py'
SPEC = importlib.util.spec_from_file_location('verify_macos_build', MODULE_PATH)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class MacOSBuildVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'DeepRecover.app'
        (self.bundle / 'Contents/MacOS').mkdir(parents=True)
        (self.bundle / 'Contents/Resources').mkdir()
        self.executable = self.bundle / 'Contents/MacOS/DeepRecover'
        # This fixture exercises the verifier's branches, not native compilation.
        self.executable.write_bytes(b'\xcf\xfa\xed\xfe' + b'\0' * 64)
        self.executable.chmod(0o755)
        (self.bundle / 'Contents/Resources/DeepRecover.icns').write_bytes(b'icns-test-fixture')
        self.info = {'CFBundleIdentifier': verify.BUNDLE_ID, 'CFBundleExecutable': 'DeepRecover',
                     'CFBundleShortVersionString': '0.3.0', 'CFBundleVersion': '0.3.0',
                     'LSMinimumSystemVersion': '13.0', 'CFBundlePackageType': 'APPL',
                     'CFBundleIconFile': 'DeepRecover.icns'}
        self.write_info()
        self.report = self.root / 'report.json'
        self.payload = {'success': True, 'version': '0.3.0', 'schema_version': 3,
                        'navigation_pages': 8, 'verified_exports': 2,
                        'decoded_formats': ['PNG', 'JPEG'], 'qt_platform': 'cocoa',
                        'native_quit_verified': True}
        self.payload.update(computer_candidates=2, computer_verified_exports=2)

    def write_info(self):
        with (self.bundle / 'Contents/Info.plist').open('wb') as stream:
            plistlib.dump(self.info, stream)

    def runner(self, command, **kwargs):
        self.assertEqual(command, ['/usr/bin/lipo', '-archs', str(self.executable.resolve())])
        self.assertTrue(kwargs['check'])
        return SimpleNamespace(stdout='arm64\n')

    def test_bundle_metadata_and_architecture_are_both_checked(self):
        executable, info = verify.validate_bundle(self.bundle, '0.3.0', 'arm64', self.runner)
        self.assertEqual(executable, self.executable.resolve())
        self.assertEqual(info['LSMinimumSystemVersion'], '13.0')

    def test_wrong_architecture_and_universal_in_wrong_distribution_rejected(self):
        for archs in ('x86_64\n', 'x86_64 arm64\n'):
            with self.subTest(archs=archs), self.assertRaises(RuntimeError):
                verify.validate_bundle(self.bundle, '0.3.0', 'arm64', lambda *args, **kwargs: SimpleNamespace(stdout=archs))

    def test_wrong_version_minimum_os_identifier_and_entrypoint_rejected(self):
        for name, value in [('CFBundleShortVersionString', '0.2.0'), ('LSMinimumSystemVersion', '12.0'),
                            ('CFBundleIdentifier', 'wrong.id'), ('CFBundleExecutable', '../../external')]:
            original = self.info[name]; self.info[name] = value; self.write_info()
            with self.subTest(field=name), self.assertRaises(RuntimeError):
                verify.validate_bundle(self.bundle, '0.3.0', 'arm64', self.runner)
            self.info[name] = original

    def test_icon_traversal_and_missing_icon_are_rejected(self):
        for icon in ('../external.icns', 'Missing.icns'):
            self.info['CFBundleIconFile'] = icon; self.write_info()
            with self.subTest(icon=icon), self.assertRaises(RuntimeError):
                verify.validate_bundle(self.bundle, '0.3.0', 'arm64', self.runner)

    def test_non_mach_o_and_non_executable_entry_rejected(self):
        self.executable.write_bytes(b'MZnot-macos')
        with self.assertRaises(RuntimeError): verify.validate_bundle(self.bundle, '0.3.0', 'arm64', self.runner)
        self.executable.write_bytes(b'\xcf\xfa\xed\xfe')
        with patch.object(verify.os, 'access', return_value=False), self.assertRaises(RuntimeError):
            verify.validate_bundle(self.bundle, '0.3.0', 'arm64', self.runner)

    def test_pipeline_report_requires_correct_field_types_and_cocoa_plugin(self):
        self.report.write_text(json.dumps(self.payload))
        self.assertEqual(verify.validate_report(self.report, '0.3.0', 'cocoa'), self.payload)
        for name, value in [('success', 1), ('schema_version', True), ('verified_exports', '2'),
                            ('decoded_formats', ['PNG']), ('qt_platform', 'offscreen')]:
            self.report.write_text(json.dumps({**self.payload, name: value}))
            with self.subTest(field=name), self.assertRaises(RuntimeError):
                verify.validate_report(self.report, '0.3.0', 'cocoa')

    def test_missing_pipeline_fields_and_non_object_report_rejected(self):
        for field in self.payload:
            self.report.write_text(json.dumps({name: value for name, value in self.payload.items() if name != field}))
            with self.subTest(missing=field), self.assertRaises(RuntimeError):
                verify.validate_report(self.report, '0.3.0', 'cocoa')
        self.report.write_text('[]')
        with self.assertRaises(RuntimeError): verify.validate_report(self.report, '0.3.0', 'cocoa')

    def test_cocoa_screenshot_must_be_a_real_nonempty_png(self):
        screenshot = self.root / 'screenshot.png'
        screenshot.write_bytes(b'not a PNG')
        with self.assertRaises(RuntimeError): verify.verify_screenshot(screenshot)
        screenshot.write_bytes(png_image(width=10, height=10, raw=b''.join(b'\0' + bytes(range(30)) for _ in range(10))))
        # A small valid fixture may be highly compressed, so add a valid PNG
        # ancillary text chunk rather than claiming an empty signature is enough.
        if screenshot.stat().st_size < 100:
            from tests.test_scanner import chunk
            raw = screenshot.read_bytes()
            screenshot.write_bytes(raw[:-12] + chunk(b'tEXt', b'note\0synthetic screenshot verification fixture') + raw[-12:])
        verify.verify_screenshot(screenshot)
