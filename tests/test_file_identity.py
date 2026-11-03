import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.candidate_io import snapshot
from core.scanner.signature.scanner import SignatureScanner


class FileIdentityTests(unittest.TestCase):
    def test_path_and_read_handle_have_same_canonical_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.bin'; path.write_bytes(b'original data')
            with path.open('rb') as stream:
                expected = snapshot(path.stat())
                actual = snapshot(os.fstat(stream.fileno()))
                self.assertEqual(actual, expected)
                self.assertEqual(SignatureScanner.fingerprint(os.fstat(stream.fileno())), expected)
                self.assertEqual(set(actual), {'dev','ino','size','mtime_ns','ctime_ns'})

    def test_windows_normalizes_creation_time_without_discarding_file_id(self):
        info = SimpleNamespace(st_dev=11, st_ino=2**80+17, st_size=30, st_mtime_ns=40,
                               st_ctime_ns=50, st_birthtime_ns=60)
        with patch('core.candidate_io.os.name','nt'):
            windows = snapshot(info)
        self.assertEqual(windows,{'dev':11,'ino':2**80+17,'size':30,'mtime_ns':40,'ctime_ns':60})
        with patch('core.candidate_io.os.name','posix'):
            self.assertEqual(snapshot(info)['ctime_ns'],50)
