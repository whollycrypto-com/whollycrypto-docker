import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import urllib.error

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'runtime'))
import common
import releases
import supervisor
import cli


class SignedBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        subprocess.run(['openssl', 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', str(self.path / 'key')], check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(self.path / 'key'), '-pubout', '-out', str(self.path / 'release-signing.pub')], check=True, capture_output=True)
        self.payload = dict(format=1, channel='stable', version='3.5.0', installer={
            'path': '/merchant/3.5.0/installer.py', 'size': 42, 'sha256': 'a' * 64})

    def tearDown(self):
        self.directory.cleanup()

    def signed(self):
        body = json.dumps(self.payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
        signed = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(self.path / 'key')], input=body, check=True, capture_output=True).stdout
        return {'signed': self.payload.copy(), 'signature': base64.b64encode(signed).decode()}

    def test_valid_signature(self):
        with patch.object(releases, 'HERE', self.path):
            self.assertEqual(releases.verify(self.signed(), self.path)['size'], 42)

    def test_tamper_rejected(self):
        envelope = self.signed()
        envelope['signed']['version'] = '9.0.0'
        with patch.object(releases, 'HERE', self.path), self.assertRaises(common.Error):
            releases.verify(envelope, self.path)

    def test_old_runtime_rejected(self):
        self.payload['version'] = '3.4.9'
        with patch.object(releases, 'HERE', self.path), self.assertRaises(common.Error):
            releases.verify(self.signed(), self.path)

    def test_unsafe_installer_path_rejected(self):
        self.payload['installer']['path'] = 'https://untrusted.example/installer.py'
        with patch.object(releases, 'HERE', self.path), self.assertRaises(common.Error):
            releases.verify(self.signed(), self.path)

    def test_private_key_never_in_context(self):
        self.assertIn('**\n!Dockerfile', (REPO / '.dockerignore').read_text())
        self.assertNotIn('PRIVATE KEY', (REPO / 'release-signing.pub').read_text())

    def test_fallback_for_outage_only(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'{}'
        for status, expected_calls in [(503, 2), (403, 1)]:
            error = urllib.error.HTTPError('https://releases.whollycrypto.com', status, 'fixture', {}, None)
            with patch('urllib.request.build_opener') as factory:
                factory.return_value.open.side_effect = [error, Response()]
                if status == 503:
                    self.assertEqual(releases.fetch('/channels/stable.json', 100), b'{}')
                else:
                    with self.assertRaises(common.Error):
                        releases.fetch('/channels/stable.json', 100)
                self.assertEqual(factory.return_value.open.call_count, expected_calls)

    def test_foreign_redirect_rejected(self):
        import urllib.request
        request = urllib.request.Request('https://github.com/whollycrypto-com/whollycrypto/releases/latest/download/manifest.json')
        for target in ['http://github.com/file', 'https://evil.example/file', 'https://github.com/another/repo/releases/download/file']:
            with self.assertRaises(common.Error):
                releases.Redirect().redirect_request(request, None, 302, '', {}, target)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = patch.object(supervisor, 'ROOT', Path(self.directory.name))
        self.root.start()
        self.module = patch.object(supervisor, 'merchant', lambda: SimpleNamespace(runtime_env=lambda: {}))
        self.module.start()
        self.manager = supervisor.Supervisor()

    async def asyncTearDown(self):
        for unit, process in self.manager.processes.items():
            if process.returncode is None:
                process.terminate()
        for task in self.manager.tasks.values():
            await task
        self.module.stop()
        self.root.stop()
        self.directory.cleanup()

    async def test_unknown_units_fail_closed(self):
        with self.assertRaises(common.Error):
            await self.manager.request({'action': 'start', 'units': ['ssh.service']})

    async def test_property_allowlist(self):
        with self.assertRaises(common.Error):
            await self.manager.request({'action': 'show', 'units': [supervisor.APP], 'properties': ['Environment']})

    async def test_timer_stop_does_not_kill_worker(self):
        timer = 'whollycrypto-domain-apply.timer'
        unit = timer.replace('.timer', '.service')
        await self.manager.start(timer)
        await self.manager.start(unit, [sys.executable, '-c', 'import time; time.sleep(.4)'], wait=False)
        await asyncio.sleep(.1)
        await self.manager.stop(timer)
        self.assertEqual(self.manager.state(timer)[0], 'inactive')
        self.assertEqual(self.manager.state(unit)[0], 'activating')
        with self.assertRaises(common.Error):
            await self.manager.stop(unit)
        await self.manager.tasks[unit]
        self.assertEqual(self.manager.state(unit)[0], 'inactive')

    async def test_failed_worker_is_not_healthy(self):
        unit = 'whollycrypto-backup.service'
        with self.assertRaises(common.Error):
            await self.manager.start(unit, [sys.executable, '-c', 'raise SystemExit(2)'])
        self.assertEqual(self.manager.state(unit)[0], 'failed')
        self.assertEqual(self.manager.exits[unit], 2)

    async def test_real_process_id_and_single_instance(self):
        command = [sys.executable, '-c', 'import time; time.sleep(10)']
        await self.manager.start(supervisor.APP, command)
        first = self.manager.state(supervisor.APP)[2]
        await self.manager.start(supervisor.APP, command)
        self.assertGreater(first, 0)
        self.assertEqual(self.manager.state(supervisor.APP)[2], first)
        await self.manager.stop(supervisor.APP)

    async def test_ready_requires_both_processes(self):
        await self.manager.request({'action': 'ready'})
        self.assertFalse((await self.manager.request({'action': 'ping'}))['ready'])

    async def test_overdue_backup_runs_after_restart(self):
        timer = 'whollycrypto-backup.timer'
        self.manager.completed[timer] = time.time() - 90000
        await self.manager.start(timer)
        self.assertLess(self.manager.next_run[timer] - time.monotonic(), 6)

    async def test_recent_backup_is_not_repeated_on_restart(self):
        timer = 'whollycrypto-backup.timer'
        self.manager.completed[timer] = time.time() - 120
        await self.manager.start(timer)
        self.assertGreater(self.manager.next_run[timer] - time.monotonic(), 86000)

    async def test_native_certificate_probes_are_read_only(self):
        result = await self.manager.request({'action': 'show', 'units': ['certbot.timer'], 'properties': ['LoadState']})
        self.assertEqual(result['units'], [{'LoadState': 'not-found'}])
        with self.assertRaises(common.Error):
            await self.manager.request({'action': 'start', 'units': ['certbot.timer']})


class DistributionTests(unittest.TestCase):
    def test_no_native_host_probe_or_payment_bypass(self):
        source = (REPO / 'runtime/cli.py').read_text()
        self.assertIn('module.check_platform = container_platform', source)
        for name in ['require_update_credit', 'verify_manifest', 'operation_lock', 'create_backup']:
            self.assertNotIn('module.' + name + ' =', source)
        self.assertIn('return original_update(args)', source)

    def test_no_privileged_or_host_socket(self):
        text = (REPO / 'compose.yaml').read_text()
        for forbidden in ['privileged:', 'docker.sock', 'network_mode: host', 'SYS_ADMIN', '/sys/fs/cgroup']:
            self.assertNotIn(forbidden, text)
        self.assertNotIn('5432:5432', text)
        self.assertIn('database_socket:/var/run/postgresql', text)

    def test_entrypoint_does_not_reinstall_completed_volumes(self):
        source = (REPO / 'runtime/bootstrap.py').read_text()
        self.assertLess(source.index("if state.get('complete'):"), source.index('installer(scratch)'))
        self.assertIn('update-pending.json', source)
        self.assertIn('if not wallet_key.exists():', source)

    def test_supported_service_contract(self):
        for timer in supervisor.TIMER_DELAYS:
            self.assertIn(timer.replace('.timer', '.service'), supervisor.COMMANDS)
        self.assertIn(supervisor.CONSOLE, supervisor.COMMANDS)


if __name__ == '__main__':
    unittest.main()
