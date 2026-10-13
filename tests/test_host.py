"""Offline safety and interruption tests. Never install packages or start Docker."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('host_cli', REPO / 'host/cli.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class HostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root_patch = patch.object(host, 'ROOT', self.root)
        self.root_patch.start()
        self.values = dict(setup={'format': 1, 'accept_terms': True, 'domain': 'example.com',
                                 'subdomains': {'console': 'merchant', 'pay': 'pay', 'api': 'api'}},
                           secrets={name: 'a' * 64 for name in host.SECRET_NAMES})
    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()
    def journal(self):
        host.save(self.root / '.setup-pending.json', json.dumps(self.values))
    def configured(self):
        self.journal()
        host.finish_configuration()
    def test_configuration_private(self):
        self.configured()
        self.assertEqual(host.validate_inputs(), self.values['setup'])
        self.assertFalse((self.root / '.setup-pending.json').exists())
        self.assertEqual((self.root / 'secrets').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'setup.json').stat().st_mode & 0o777, 0o600)
    def test_configuration_replay_preserves_all_secrets(self):
        self.journal()
        original = host.save
        def interrupted(path, value):
            if Path(path).name == 'basic_password':
                raise OSError('simulated interruption')
            return original(path, value)
        with patch.object(host, 'save', interrupted), self.assertRaises(OSError):
            host.finish_configuration()
        before = (self.root / 'secrets/database_password').read_bytes()
        host.finish_configuration()
        self.assertEqual((self.root / 'secrets/database_password').read_bytes(), before)
        host.validate_inputs()
    def test_different_existing_secret_not_replaced(self):
        self.journal()
        host.save(self.root / 'secrets/database_password', 'b' * 64 + '\n')
        with self.assertRaises(host.Error):
            host.finish_configuration()
        self.assertEqual((self.root / 'secrets/database_password').read_text(), 'b' * 64 + '\n')
    def test_symlink_directory_rejected(self):
        self.journal()
        target = self.root / 'other'
        target.mkdir()
        (self.root / 'secrets').symlink_to(target, target_is_directory=True)
        with self.assertRaises(host.Error):
            host.finish_configuration()
        self.assertEqual(list(target.iterdir()), [])
    def test_symlink_file_rejected(self):
        self.configured()
        target = self.root / 'target'
        host.save(target, 'a' * 64)
        path = self.root / 'secrets/basic_password'
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(OSError):
            host.validate_inputs()
    def test_world_readable_inputs_rejected(self):
        self.configured()
        (self.root / 'setup.json').chmod(0o644)
        with self.assertRaises(host.Error):
            host.validate_inputs()
    def test_ready_returns_and_stops_only_log_follower(self):
        self.configured()
        with patch.object(host, 'progress', return_value={'ready': True}), patch.object(host.subprocess, 'Popen') as logs:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                host.wait_ready()
            self.assertIn('https://merchant.example.com', output.getvalue())
            logs.return_value.terminate.assert_called_once()
    def test_failed_setup_exits_without_restarting(self):
        with patch.object(host, 'progress', return_value={'bootstrap_state': 'failed'}), patch.object(host.subprocess, 'Popen') as logs:
            with self.assertRaises(host.Error), contextlib.redirect_stdout(io.StringIO()):
                host.wait_ready()
            logs.return_value.terminate.assert_called_once()
    def test_ready_resume_does_not_build_or_recreate(self):
        self.configured()
        with patch.object(host, 'progress', return_value={'ready': True}), patch.object(host, 'compose') as compose, patch.object(host, 'wait_ready') as wait:
            host.install(resume=True)
            compose.assert_not_called()
            wait.assert_called_once()
    def test_running_bootstrap_resume_only_follows(self):
        self.configured()
        with patch.object(host, 'progress', return_value={'bootstrap_state': 'activating'}), patch.object(host, 'compose') as compose, patch.object(host, 'wait_ready') as wait:
            host.install(resume=True)
            compose.assert_not_called()
            wait.assert_called_once()
    def test_recovery_or_active_job_refuses_recreate(self):
        self.configured()
        for state in ({'recovery_required': True}, {'busy': True}):
            with self.subTest(state=state), patch.object(host, 'progress', return_value=state), patch.object(host, 'compose') as compose:
                with self.assertRaises(host.Error):
                    host.install(resume=True)
                compose.assert_not_called()
    def test_invalid_domain(self):
        for value in ('http://example.com', '1.1.1.1', '*.example.com', 'example.com/path', 'x..com'):
            with self.subTest(value=value), self.assertRaises(host.Error):
                host.domain(value)
    def test_unreachable_live_supervisor_does_not_allow_recreate(self):
        with patch.object(host, 'compose', return_value='container'), patch.object(host, 'run', return_value='[{"State":{"Running":true}}]'):
            with self.assertRaises(host.Error):
                host.require_safe_recreate({})
    def test_recreate_checks_older_supervisor_workers(self):
        with patch.object(host, 'compose', side_effect=['container', 'activating inactive inactive inactive inactive inactive']), patch.object(host, 'run', return_value='[{"State":{"Running":true}}]'):
            with self.assertRaises(host.Error):
                host.require_safe_recreate({'protocol': 1, 'ready': False})
    def test_no_container_is_safe_to_create(self):
        with patch.object(host, 'compose', return_value=''):
            host.require_safe_recreate({})
    def test_existing_destination_never_overwritten(self):
        path = self.root / 'saved'
        host.save(path, 'first')
        with self.assertRaises(FileExistsError):
            host.save(path, 'second')
        self.assertEqual(path.read_text(), 'first')


class ShellTests(unittest.TestCase):
    def shell(self, body, success=True):
        result = subprocess.run(['bash', '-c', 'source "$1"; ' + body, 'test', str(REPO / 'install.sh')], text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result.stdout + result.stderr
    def test_syntax_and_help_without_python(self):
        subprocess.run(['bash', '-n', str(REPO / 'install.sh'), str(REPO / 'whollycrypto-docker')], check=True)
        self.assertIn('Usage:', self.shell('main --help'))
        result = subprocess.run(['bash', str(REPO / 'whollycrypto-docker'), '--help'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('1.1.0', result.stdout)
    def test_unknown_flags_fail(self):
        self.assertIn('Unknown option', self.shell('main --erase', False))
    def test_check_is_non_mutating(self):
        body = '''host_check(){ :; }; distro_plan(){ DISTRO=fixture; DISTRO_VERSION=1; }; python_command(){ echo python3; };
command(){ if [[ "$1" == -v ]]; then return 0; fi; builtin command "$@"; };
docker_context_check(){ :; }; docker(){ return 0; }; docker_ready_check(){ :; };
base_packages(){ exit 88; }; checkout(){ exit 89; }; confirm(){ exit 90; }; main --check'''
        self.assertIn('prerequisites ready', self.shell(body))
    def test_context_must_be_local(self):
        for endpoint in ('ssh://host', 'tcp://example.com:2376', 'unix:///run/user/1000/docker.sock'):
            with self.subTest(endpoint=endpoint):
                self.assertIn('local rootful', self.shell('docker(){ echo "' + endpoint + '"; }; docker_context_check', False))
    def test_local_context_accepted(self):
        self.shell('docker(){ echo unix:///var/run/docker.sock; }; docker_context_check')
    def test_plugin_plan_cannot_upgrade_engine(self):
        body = '''PACKAGE_KIND=apt; docker(){ return 1; }; apt-get(){ echo 'Inst docker-ce [old] (new)'; };
apt_install(){ echo UNSAFE; }; install_plugins'''
        output = self.shell(body, False)
        self.assertIn('replace or upgrade', output)
        self.assertNotIn('UNSAFE', output)
    def test_plugin_plan_installs_only_missing_plugins(self):
        body = '''PACKAGE_KIND=apt; docker(){ [[ "$1" == buildx ]]; }; apt-get(){ echo 'Inst docker-compose-plugin (new)'; };
apt_install(){ printf '%s\\n' "$*"; }; install_plugins'''
        self.assertEqual(self.shell(body).strip(), '--no-upgrade docker-compose-plugin')
    def test_missing_repo_plan_fails_without_changes(self):
        body = '''PACKAGE_KIND=apt; docker(){ return 1; }; apt-get(){ return 100; }; apt_install(){ echo UNSAFE; }; install_plugins'''
        self.assertNotIn('UNSAFE', self.shell(body, False))
    def test_unknown_distro_requires_manual_tools(self):
        self.assertIn('manually', self.shell('PACKAGE_KIND=manual; DISTRO=unknown; DISTRO_VERSION=1; base_packages', False))
    def test_rpm_keeps_existing_curl_minimal_and_python(self):
        body = '''PACKAGE_KIND=dnf; python_command(){ echo python3; };
command(){ [[ "$2" != git ]]; }; dnf(){ printf '%s\\n' "$*"; }; base_packages'''
        output = self.shell(body)
        self.assertIn('git', output)
        self.assertNotIn('curl', output)
        self.assertNotIn('python3', output)
    def test_distro_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'os-release'
            for distro, version, expected in [('ubuntu', '24.04', 'apt'), ('ubuntu', '22.04', 'apt'), ('debian', '13', 'apt'), ('debian', '12', 'apt'), ('fedora', '44', 'dnf'), ('rhel', '9.7', 'dnf'), ('rhel', '10.1', 'dnf'), ('opensuse-leap', '16', 'manual'), ('debian', '10', 'manual')]:
                path.write_text('ID=' + distro + '\nVERSION_ID=' + version + '\n')
                self.assertEqual(self.shell('distro_plan "' + str(path) + '"; echo "$PACKAGE_KIND"').strip(), expected)
    def test_signature_key_mismatch_fails(self):
        self.assertIn('fingerprint mismatch', self.shell('gpg(){ echo "fpr:::::::::WRONG:"; }; check_key /fixture EXPECTED', False))
    def test_no_destructive_runtime_commands(self):
        source = (REPO / 'install.sh').read_text()
        for value in ('apt-get remove', 'dnf remove', 'down -v', 'docker system prune', 'setenforce 0', 'git reset', 'git clean'):
            self.assertNotIn(value, source)


if __name__ == '__main__':
    unittest.main()
