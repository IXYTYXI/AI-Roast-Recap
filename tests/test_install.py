import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'install.py'


class InstallerTests(unittest.TestCase):
    def load(self):
        self.assertTrue(MODULE.exists(), 'installer has not been implemented')
        spec = importlib.util.spec_from_file_location('scold_installer', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_dry_run_never_creates_host_directory(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'host'
            result = module.install_one('codex', root, dry_run=True)
            self.assertEqual(result['action'], 'install')
            self.assertFalse(root.exists())

    def test_unmanaged_skill_is_not_overwritten(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'skills' / 'ai-scold-review'
            target.mkdir(parents=True)
            (target / 'SKILL.md').write_text('user-owned content')
            with self.assertRaises(ValueError):
                module.install_one('codex', root)
            self.assertEqual((target / 'SKILL.md').read_text(), 'user-owned content')

    def test_workspace_data_directory_can_be_selected(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'host'
            data = Path(tmp) / 'workspace-data'
            result = module.install_one('codex', root, dry_run=True, data_dir=data)
            self.assertEqual(Path(result['data_dir']), data.resolve())
            self.assertFalse(data.exists())

    def test_install_update_preserves_activation_and_uninstall_keeps_data(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sentinel = root / 'settings.json'
            sentinel.write_text('{"unrelated":"preserve"}')
            result = module.install_one('claude-code', root)
            data = Path(result['data_dir'])
            config_before = (data / 'config.json').read_text()
            target = Path(result['skill_dir'])
            self.assertTrue((target / 'SKILL.md').exists())
            self.assertEqual(json.loads((target / 'host.json').read_text())['platform'], 'claude-code')
            module.install_one('claude-code', root)
            self.assertEqual((data / 'config.json').read_text(), config_before)
            self.assertEqual(sentinel.read_text(), '{"unrelated":"preserve"}')
            module.uninstall_one('claude-code', root)
            self.assertFalse(target.exists())
            self.assertTrue((data / 'config.json').exists())

    def test_uninstall_rejects_unmanaged_directory(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'skills' / 'ai-scold-review'
            target.mkdir(parents=True)
            with self.assertRaises(ValueError):
                module.uninstall_one('codex', root)
            self.assertTrue(target.exists())

    def test_update_without_timezone_preserves_custom_timezone(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            first = module.install_one('codex', Path(tmp), timezone_name='UTC')
            config = Path(first['data_dir']) / 'config.json'
            before = config.read_text()
            module.install_one('codex', Path(tmp))
            self.assertEqual(config.read_text(), before)
            self.assertEqual(json.loads(config.read_text())['timezone'], 'UTC')


if __name__ == '__main__':
    unittest.main()
