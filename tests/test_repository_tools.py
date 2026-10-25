"""Installation and packaging contracts; no network or user configuration access."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install_tool = load('install')
package_tool = load('package')


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_install_and_backup_update(self):
        destination = self.root / 'skills' / 'relay-imagegen'
        path, backup, _ = install_tool.install(destination)
        self.assertEqual(path, destination)
        self.assertIsNone(backup)
        self.assertTrue((path / 'SKILL.md').is_file())
        self.assertTrue((path / 'assets' / 'studio.html').is_file())
        self.assertFalse(list(path.rglob('test_*.py')))
        self.assertFalse(list(path.rglob('__pycache__')))
        marker = path / 'local-marker.txt'
        marker.write_text('previous installation')
        with self.assertRaises(ValueError):
            install_tool.install(destination)
        self.assertTrue(marker.is_file())
        _, backup, _ = install_tool.install(destination, replace=True)
        self.assertEqual((backup / 'local-marker.txt').read_text(), 'previous installation')
        self.assertNotIn(destination.parent, backup.parents)
        self.assertFalse(marker.exists())

    def test_symlink_to_source_is_already_installed(self):
        destination = self.root / 'current'
        try:
            destination.symlink_to(install_tool.SOURCE, target_is_directory=True)
        except OSError:
            self.skipTest('Symlinks unavailable')
        _, backup, _ = install_tool.install(destination)
        self.assertIsNone(backup)
        self.assertTrue(destination.is_symlink())

    def test_foreign_symlink_not_overwritten(self):
        foreign = self.root / 'foreign'
        foreign.mkdir()
        destination = self.root / 'current'
        try:
            destination.symlink_to(foreign, target_is_directory=True)
        except OSError:
            self.skipTest('Symlinks unavailable')
        with self.assertRaises(ValueError):
            install_tool.install(destination, replace=True)
        self.assertTrue(destination.is_symlink())
        self.assertTrue(foreign.is_dir())

    def test_archive_is_complete_and_reproducible(self):
        first, second = self.root/'first.zip', self.root/'second.zip'
        count, checksum = package_tool.build(first)
        self.assertEqual((count, checksum), package_tool.build(second))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), count)
            self.assertIn('relay-imagegen/scripts/relay_imagegen.py', names)
            self.assertIn('relay-imagegen/assets/studio.html', names)
            self.assertNotIn('relay-imagegen/scripts/relay_cover.py', names)
            self.assertNotIn('relay-imagegen/references/covers.md', names)
            self.assertFalse(any('test_' in n or '__pycache__' in n or n.endswith('config.json') for n in names))
            for name in names:
                source = package_tool.SKILL / Path(name).relative_to('relay-imagegen')
                self.assertEqual(archive.read(name), source.read_bytes())

    def test_refuse_targets_inside_runtime_source(self):
        with self.assertRaises(ValueError):
            install_tool.install(install_tool.SOURCE/'nested-install')
        with self.assertRaises(ValueError):
            package_tool.build(package_tool.SKILL/'package.zip')


if __name__ == '__main__':
    unittest.main()
