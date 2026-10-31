import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('workflow', Path(__file__).resolve().parents[1] / 'workflow.py')
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.area = Path(self.temp.name)
        self.source = self.area / 'shared-package'
        (self.source / 'rules').mkdir(parents=True)
        (self.source / 'VERSION').write_text('1.0.0\n')
        (self.source / 'rules' / 'COMMON.md').write_text('# Common\nUse {{EXECUTION_MODEL}} / {{EXECUTION_EFFORT}}.\n')
        self.root = self.area / 'workspace'
        self.root.mkdir()

    def plan(self, **kwargs):
        return w.make_plan(self.root, self.source, **kwargs)

    def install(self):
        return w.apply_plan(self.plan())

    def upgrade(self):
        (self.source / 'VERSION').write_text('1.1.0\n')
        (self.source / 'rules' / 'COMMON.md').write_text('# Common\nNew confirmed rule.\n')

    def test_preview_writes_nothing(self):
        plan = self.plan()
        self.assertTrue(plan['after'].startswith(w.BEGIN))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_first_install_preserves_original_and_backup(self):
        original = '# Existing local rules\nKeep my build command.\n'
        (self.root / 'AGENTS.md').write_text(original)
        self.install()
        self.assertTrue(w.read_text(self.root / 'AGENTS.md').startswith(original))
        backups = list((self.root / '.assistant-workflow' / 'backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(w.read_text(backups[0]), original)

    def test_repeat_is_idempotent(self):
        (self.root / 'AGENTS.md').write_text('Local\n')
        self.install()
        before = (self.root / 'AGENTS.md').read_bytes()
        result = self.install()
        self.assertFalse(result['changed'])
        self.assertEqual((self.root / 'AGENTS.md').read_bytes(), before)
        self.assertEqual(len(list((self.root / '.assistant-workflow' / 'backups').iterdir())), 1)

    def test_upgrade_preserves_prefix_suffix_and_local_notes(self):
        entry = self.root / 'AGENTS.md'
        entry.write_text('Local prefix\n')
        self.install()
        entry.write_text(w.read_text(entry) + '\nLocal suffix\n')
        (self.root / '.assistant-workflow' / 'local.md').write_text('Use my local tool.\n')
        self.upgrade()
        self.install()
        updated = w.read_text(entry)
        self.assertTrue(updated.startswith('Local prefix\n'))
        self.assertTrue(updated.endswith('\nLocal suffix\n'))
        self.assertNotIn('Use my local tool.', updated)
        self.assertEqual((self.root / '.assistant-workflow' / 'local.md').read_text(), 'Use my local tool.\n')
        self.assertIn('New confirmed rule.', updated)

    def test_managed_edit_is_rejected_without_overwrite(self):
        self.install()
        entry = self.root / 'AGENTS.md'
        entry.write_text(w.read_text(entry).replace('# Common', '# Edited locally'))
        before = entry.read_bytes()
        with self.assertRaisesRegex(w.WorkflowError, 'Managed block was edited'):
            self.install()
        self.assertEqual(before, entry.read_bytes())

    def test_unversioned_change_and_downgrade_rejected(self):
        self.install()
        (self.source / 'rules' / 'COMMON.md').write_text('Changed without version.\n')
        with self.assertRaisesRegex(w.WorkflowError, 'VERSION increment'):
            self.install()
        (self.source / 'VERSION').write_text('0.9.0\n')
        with self.assertRaisesRegex(w.WorkflowError, 'downgrade'):
            self.install()

    def test_override_is_actual_target(self):
        (self.root / 'AGENTS.md').write_text('Inactive original\n')
        (self.root / 'AGENTS.override.md').write_text('Active override\n')
        result = self.install()
        self.assertTrue(result['entry'].endswith('AGENTS.override.md'))
        self.assertEqual((self.root / 'AGENTS.md').read_text(), 'Inactive original\n')
        self.assertIn(w.BEGIN, (self.root / 'AGENTS.override.md').read_text())

    def test_empty_override_cannot_hide_existing_rules(self):
        (self.root / 'AGENTS.md').write_text('Existing local rule\n')
        (self.root / 'AGENTS.override.md').write_text('  \n')
        with self.assertRaisesRegex(w.WorkflowError, 'hide existing AGENTS'):
            self.install()
        self.assertEqual((self.root / 'AGENTS.md').read_text(), 'Existing local rule\n')
        self.assertEqual((self.root / 'AGENTS.override.md').read_text(), '  \n')
        self.assertFalse((self.root / '.assistant-workflow').exists())

    def test_generated_entry_contains_no_local_identity_or_private_notes(self):
        local = self.root / '.assistant-workflow'
        local.mkdir()
        (local / 'local.json').write_text(json.dumps({'device_id': 'private-device-example'}))
        (local / 'local.md').write_text('Private local-only note\n')
        self.install()
        entry = (self.root / 'AGENTS.md').read_text()
        for value in [str(self.root), str(self.root.resolve()), str(self.source), str(self.source.resolve()), 'private-device-example', 'Private local-only note']:
            self.assertNotIn(value, entry)
        stored = json.loads((local / 'local.json').read_text())
        self.assertEqual(stored['workspace_root'], str(self.root.resolve()))
        self.assertEqual(stored['device_id'], 'private-device-example')

    def test_entry_change_requires_migration(self):
        self.install()
        (self.root / 'AGENTS.override.md').write_text('New override\n')
        with self.assertRaisesRegex(w.WorkflowError, 'entry or workspace changed'):
            self.install()

    def test_missing_state_or_marker_is_rejected(self):
        self.install()
        state = self.root / '.assistant-workflow' / 'state.json'
        state.unlink()
        with self.assertRaisesRegex(w.WorkflowError, 'without state'):
            self.install()

    def test_original_crlf_is_preserved(self):
        original = b'# Local\r\nKeep CRLF.\r\n'
        (self.root / 'AGENTS.md').write_bytes(original)
        self.install()
        self.assertTrue((self.root / 'AGENTS.md').read_bytes().startswith(original))

    def test_symlink_entry_and_state_refused(self):
        other = self.area / 'elsewhere'
        other.mkdir()
        file = other / 'notes.md'
        file.write_text('Do not replace\n')
        (self.root / 'AGENTS.md').symlink_to(file)
        with self.assertRaisesRegex(w.WorkflowError, 'symlinked instruction'):
            self.install()
        self.assertEqual(file.read_text(), 'Do not replace\n')
        (self.root / 'AGENTS.md').unlink()
        (self.root / '.assistant-workflow').symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(w.WorkflowError, 'symlinked local state'):
            self.install()

    def test_size_budget_refused_before_write(self):
        with self.assertRaisesRegex(w.WorkflowError, 'byte budget'):
            self.plan(max_bytes=10)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_concurrent_edit_after_preview_is_preserved(self):
        plan = self.plan()
        (self.root / 'AGENTS.md').write_text('Another task wrote this\n')
        with self.assertRaisesRegex(w.WorkflowError, 'changed since preview'):
            w.apply_plan(plan)
        self.assertEqual((self.root / 'AGENTS.md').read_text(), 'Another task wrote this\n')

    def test_model_parameters_and_unknown_fields_are_local(self):
        local_root = self.root / '.assistant-workflow'
        local_root.mkdir()
        (local_root / 'local.json').write_text(json.dumps({
            'device_id': 'device-b', 'execution_model': 'supported-model',
            'execution_effort': 'high', 'local_only_note': 'keep-local'}))
        self.install()
        self.assertIn('supported-model / high', (self.root / 'AGENTS.md').read_text())
        self.assertEqual(json.loads((local_root / 'local.json').read_text())['local_only_note'], 'keep-local')
        self.assertNotIn('device-b', (self.source / 'rules' / 'COMMON.md').read_text())

    def test_status_does_not_claim_runtime_loading(self):
        self.install()
        value, code = w.status(self.root, self.source)
        self.assertEqual(code, 0)
        self.assertEqual(value['runtime_loaded'], 'not_verified')
        self.upgrade()
        value, code = w.status(self.root, self.source)
        self.assertEqual(code, 1)
        self.assertEqual(value['status'], 'update_available')

    @unittest.skipUnless(shutil.which('git'), 'Git unavailable')
    def test_local_state_ignored_in_unrelated_repository(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.area / 'empty-excludes').write_text('')
        self.install()
        result = subprocess.run(['git', '-c', 'core.excludesFile=' + str(self.area / 'empty-excludes'), '-C', str(self.root), 'status', '--porcelain', '--untracked-files=all'],
                                check=True, text=True, stdout=subprocess.PIPE)
        self.assertNotIn('.assistant-workflow/', result.stdout)
        self.assertIn('AGENTS.md', result.stdout)

    @unittest.skipUnless(shutil.which('git'), 'Git unavailable')
    def test_two_machine_fast_forward_preserves_private_local_rules(self):
        def git(cwd, *args):
            result = subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', 'user.name=Example',
                                     '-c', 'user.email=example@example.invalid', '-C', str(cwd), *args],
                                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return result.stdout
        remote = self.area / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        git(self.source, 'init', '-q')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-qm', 'Initial generic rules')
        git(self.source, 'remote', 'add', 'origin', str(remote))
        branch = git(self.source, 'branch', '--show-current').strip()
        git(self.source, 'push', '-q', '-u', 'origin', branch)
        second = self.area / 'second-clone'
        subprocess.run(['git', 'clone', '-q', '--branch', branch, str(remote), str(second)], check=True)
        other_workspace = self.area / 'workspace-b'
        other_workspace.mkdir()
        (other_workspace / 'AGENTS.md').write_text('Device B private command\n')
        w.apply_plan(w.make_plan(other_workspace, second))
        self.upgrade()
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-qm', 'Confirmed common improvement')
        git(self.source, 'push', '-q')
        git(second, 'pull', '--ff-only', '-q')
        w.apply_plan(w.make_plan(other_workspace, second))
        value = (other_workspace / 'AGENTS.md').read_text()
        self.assertIn('New confirmed rule.', value)
        self.assertIn('Device B private command', value)
        self.assertNotIn('Device B private command', (second / 'rules' / 'COMMON.md').read_text())


if __name__ == '__main__':
    unittest.main()
