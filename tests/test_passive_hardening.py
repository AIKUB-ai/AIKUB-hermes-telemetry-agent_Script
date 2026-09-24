"""Safety regressions: temporary homes, synthetic credentials, local Git only."""
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class UpdaterSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.hermes = self.home / '.hermes'
        self.hermes.mkdir()
        self.profile = self.hermes / 'profiles/work'
        self.profile.mkdir(parents=True)
        self.custom = self.base / 'custom-hermes'
        self.custom.mkdir()
        self.origin = self.base / 'origin'
        self.make_agent(self.origin)
        self.git('init', '-b', 'main')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('HERMES_', 'AIKUB_', 'GIT_'))}
        self.env.update(HOME=str(self.home), HERMES_HOME=str(self.custom),
                        HERMES_REAL_HOME=str(self.hermes), AIKUB_TELEMETRY_REPO_URL=str(self.origin))
        self.sentinels = []
        for directory in (self.home, self.hermes, self.profile, self.custom):
            for name in ('auth.json', 'config.yaml'):
                p = directory / name
                p.write_text('synthetic sentinel ' + name)
                self.sentinels.append((p, p.read_bytes(), p.stat().st_ino))

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.origin), *args], check=True, capture_output=True)

    def make_agent(self, directory):
        (directory / 'scripts').mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / 'run_telemetry.sh', directory)
        for p in (ROOT / 'scripts').glob('*'):
            if p.is_file():
                shutil.copy2(p, directory / 'scripts')

    def update(self, target):
        return subprocess.run(['bash', str(ROOT / 'scripts/aikub_telemetry_self_update.sh')],
                              env=dict(self.env, AIKUB_TELEMETRY_AGENT_DIR=str(target)),
                              text=True, capture_output=True)

    def assert_sentinels(self):
        for p, data, inode in self.sentinels:
            self.assertEqual(p.read_bytes(), data)
            self.assertEqual(p.stat().st_ino, inode)
        self.assertEqual(list(self.base.rglob('*.old_*')), [])
        self.assertEqual(list(self.base.rglob('*.update_*')), [])

    def test_protected_paths_and_aliases_are_rejected_before_git(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.hermes, target_is_directory=True)
        targets = [self.home, self.hermes, self.profile, self.profile.parent, self.custom,
                   self.base, Path('/'), alias, alias / 'profiles/work', self.hermes / '../.hermes',
                   self.hermes / 'profiles/not-created-yet']
        for target in targets:
            with self.subTest(target=target):
                result = self.update(target)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('Unsafe telemetry target', result.stderr)
                self.assert_sentinels()

    def test_unidentified_directory_even_git_is_not_replaced(self):
        for git in (False, True):
            target = self.base / ('unrelated-git' if git else 'unrelated')
            target.mkdir()
            (target / 'unrelated.txt').write_text('synthetic unrelated')
            if git:
                subprocess.run(['git', '-C', str(target), 'init'], check=True, capture_output=True)
            result = self.update(target)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((target / 'unrelated.txt').read_text(), 'synthetic unrelated')
            self.assertFalse((target / 'run_telemetry.sh').exists())
            self.assert_sentinels()

    def test_identifiable_agent_with_auth_is_rejected(self):
        target = self.base / 'mixed'
        self.make_agent(target)
        (target / 'auth.json').write_text('synthetic auth')
        self.assertNotEqual(self.update(target).returncode, 0)
        self.assertEqual((target / 'auth.json').read_text(), 'synthetic auth')
        self.assert_sentinels()

    def test_linked_external_profile_and_external_git_metadata_rejected(self):
        external = self.base / 'external-profile'
        self.make_agent(external)
        (self.profile.parent / 'linked').symlink_to(external, target_is_directory=True)
        self.assertNotEqual(self.update(external).returncode, 0)
        target = self.base / 'git-link-agent'
        self.make_agent(target)
        (target / '.git').symlink_to(self.origin / '.git', target_is_directory=True)
        self.assertNotEqual(self.update(target).returncode, 0)
        self.assert_sentinels()

    def test_active_custom_profile_protects_siblings_and_base(self):
        base = self.base / 'custom-base'
        active = base / 'profiles/active'
        sibling = base / 'profiles/sibling'
        self.make_agent(active)
        self.make_agent(sibling)
        self.env['HERMES_HOME'] = str(active)
        for target in (base, active, sibling, base / 'profiles/new'):
            self.assertNotEqual(self.update(target).returncode, 0)
        self.assert_sentinels()

    def test_valid_symlink_repair_and_subsequent_git_update(self):
        target = self.profile / 'aikub_telemetry_agent'
        self.make_agent(target)
        (target / '.env').write_text('FIXTURE=preserved\n')
        alias = self.base / 'agent-alias'
        alias.symlink_to(target, target_is_directory=True)
        for _ in range(2):
            result = self.update(alias)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(alias.is_symlink())
            self.assertTrue((target / '.git').is_dir())
            self.assertEqual((target / '.env').read_text(), 'FIXTURE=preserved\n')
        for p, data, inode in self.sentinels:
            self.assertEqual(p.read_bytes(), data)
            self.assertEqual(p.stat().st_ino, inode)

    def test_fresh_clone_allowed_inside_hermes(self):
        target = self.hermes / 'aikub_telemetry_agent'
        result = self.update(target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((target / '.git').is_dir())
        self.assert_sentinels()


class PassiveReadersTests(unittest.TestCase):
    def test_skills_never_import_hermes_and_local_override_wins(self):
        logger = module('aikub_telemetry_logger')
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            tools = home / 'hermes-agent/tools'
            tools.mkdir(parents=True)
            (tools / '__init__.py').write_text('raise RuntimeError("runtime imported")')
            (tools / 'skills_tool.py').write_text('raise RuntimeError("runtime imported")')
            for root, desc in [('skills', 'local'), ('hermes-agent/skills', 'builtin')]:
                skill = home / root / 'category/demo/SKILL.md'
                skill.parent.mkdir(parents=True)
                skill.write_text('---\nname: demo\ndescription: ' + desc + '\n---\n')
            before = list(sys.path)
            # Any runtime import is forbidden, even one that gets caught internally.
            with patch('builtins.__import__', side_effect=AssertionError('unexpected import')) as imports:
                found = logger.discover_skills(home)
            imports.assert_not_called()
            self.assertEqual(sys.path, before)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]['description'], 'local')

    def test_sqlite_connection_rejects_writes_and_handles_uri_characters(self):
        sessions = module('aikub_telemetry_sessions_snapshot')
        with tempfile.TemporaryDirectory(prefix='sessions ?#') as tmp:
            home = Path(tmp)
            db = home / 'state.db'
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE fixture (value TEXT)')
                con.execute("INSERT INTO fixture VALUES ('synthetic')")
            before = db.read_bytes()
            con = sessions.connect_db(home)
            try:
                self.assertEqual(con.execute('SELECT value FROM fixture').fetchone()[0], 'synthetic')
                with self.assertRaises(sqlite3.OperationalError):
                    con.execute("INSERT INTO fixture VALUES ('must fail')")
            finally:
                con.close()
            self.assertEqual(db.read_bytes(), before)

    def test_readonly_sqlite_reads_committed_wal_data(self):
        sessions = module('aikub_telemetry_sessions_snapshot')
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            writer = sqlite3.connect(home / 'state.db')
            try:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('CREATE TABLE fixture (value TEXT)')
                writer.execute("INSERT INTO fixture VALUES ('wal fixture')")
                writer.commit()
                reader = sessions.connect_db(home)
                try:
                    self.assertEqual(reader.execute('SELECT value FROM fixture').fetchone()[0], 'wal fixture')
                finally:
                    reader.close()
            finally:
                writer.close()

    def test_missing_sqlite_db_not_created(self):
        sessions = module('aikub_telemetry_sessions_snapshot')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                sessions.connect_db(Path(tmp))
            self.assertFalse((Path(tmp) / 'state.db').exists())
