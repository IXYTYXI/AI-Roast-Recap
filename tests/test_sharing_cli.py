"""CLI integration for opt-in sharing; never read real accounts or send data."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import scold


class SharingCLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'
        scold.initialize(self.data, 'codex', Path(self.tmp.name) / 'native')

    def cli(self, command, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / 'scold.py'), command,
                               '--data-dir', str(self.data), *args],
                              capture_output=True, text=True, timeout=10)

    def test_status_exposes_disabled_sharing_without_reading_identity(self):
        result = self.cli('status')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(json.loads(result.stdout)['sharing']['enabled'])
        self.assertFalse((self.data / 'sharing.json').exists())

    def test_sharing_cli_defaults_are_off_and_sync_never_needs_login(self):
        for command in ['sharing-status', 'sharing-disable', 'sync']:
            with self.subTest(command=command):
                result = self.cli(command)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertFalse(json.loads(result.stdout)['enabled'])

    def test_enable_requires_consent_before_any_identity_read(self):
        result = self.cli('sharing-enable', '--endpoint', 'https://receiver.example.invalid/events')
        self.assertNotEqual(0, result.returncode)
        self.assertIn('consent', json.loads(result.stderr)['error'].lower())
        self.assertFalse((self.data / 'sharing.json').exists())

    def test_upgrade_preserves_activation_and_existing_event_rows(self):
        config = (self.data / 'config.json').read_bytes()
        with closing(sqlite3.connect(self.data / 'ledger.sqlite')) as db:
            before = db.execute('SELECT * FROM events').fetchall()
        result = scold.initialize(self.data, 'codex', Path(self.tmp.name) / 'native')
        self.assertFalse(result['created'])
        self.assertEqual(config, (self.data / 'config.json').read_bytes())
        with closing(sqlite3.connect(self.data / 'ledger.sqlite')) as db:
            self.assertEqual(before, db.execute('SELECT * FROM events').fetchall())
        self.assertFalse(scold.status(self.data)['sharing']['enabled'])


if __name__ == '__main__':
    unittest.main()
