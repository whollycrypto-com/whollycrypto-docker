"""Isolated maintenance, recovery and database-initialization regressions."""
import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'runtime'))
import common
import cli
import maintenance
import recovery
import supervisor
import test_runtime

spec = importlib.util.spec_from_file_location('safety_host', REPO / 'host/cli.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


def container(service, running=True):
    mounts = {'merchant': [('/root/whollycrypto', 'merchant'), ('/etc/nginx', 'nginx'), ('/etc/letsencrypt', 'certificates')],
              'database': [('/var/lib/postgresql', 'database')]}
    return {'Id': service + '-id', 'Image': 'old-image-' + service, 'State': {'Running': running},
            'Config': {'Labels': {'com.docker.compose.service': service}},
            'Mounts': [{'Destination': target, 'Type': 'volume', 'Name': name} for target, name in mounts[service]]}


class HostSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root_patch = patch.object(host, 'ROOT', self.root)
        self.root_patch.start()
        self.values = {name: container(name) for name in ('merchant', 'database')}

    def tearDown(self):
        self.root_patch.stop()
        self.directory.cleanup()

    def test_start_never_calls_compose_up_or_recreates(self):
        self.values['merchant']['State']['Running'] = False
        with patch.object(sys, 'argv', ['helper', 'start']), patch.object(host, 'prerequisites'), patch.object(host, 'validate_inputs'), patch.object(host, 'label_container_inputs'), patch.object(host, 'containers', return_value=self.values), patch.object(host, 'run') as run, patch.object(host, 'compose') as compose, patch.object(host, 'wait_ready') as ready, patch.object(host, 'wait_database') as database:
            host.main()
        compose.assert_not_called()
        run.assert_called_once_with(['docker', 'start', 'merchant-id'])
        ready.assert_called_once()
        database.assert_called_once_with('database-id')

    def test_running_unhealthy_database_prevents_starting_merchant(self):
        self.values['merchant']['State']['Running'] = False
        with patch.object(host, 'run') as run, patch.object(host, 'wait_database', side_effect=host.Error('not ready')):
            with self.assertRaises(host.Error): host.start_existing(self.values)
        run.assert_not_called()

    def test_start_does_not_create_missing_services(self):
        with patch.object(sys, 'argv', ['helper', 'start']), patch.object(host, 'prerequisites'), patch.object(host, 'validate_inputs'), patch.object(host, 'label_container_inputs'), patch.object(host, 'containers', return_value={}), patch.object(host, 'compose') as compose:
            with self.assertRaises(host.Error): host.main()
        compose.assert_not_called()

    def test_guard_release_failure_not_silently_ignored_on_live_container(self):
        process=Mock(stdin=io.BytesIO(), stdout=io.BytesIO(b'{"ok":true,"state":"drained"}\n'))
        process.wait.return_value=1
        with patch.object(host.subprocess, 'Popen', return_value=process), patch.object(host.selectors, 'DefaultSelector') as selector, patch.object(host.subprocess, 'run', return_value=SimpleNamespace(returncode=0,stdout=json.dumps([self.values['merchant']]))):
            selector.return_value.__enter__.return_value.select.return_value=[True]
            with self.assertRaisesRegex(host.Error,'release failed'):
                with host.maintenance(self.values['merchant']): pass

    def test_guard_termination_expected_when_original_container_stopped(self):
        process=Mock(stdin=io.BytesIO(), stdout=io.BytesIO(b'{"ok":true,"state":"drained"}\n'))
        process.wait.return_value=137
        with patch.object(host.subprocess, 'Popen', return_value=process), patch.object(host.selectors, 'DefaultSelector') as selector, patch.object(host.subprocess, 'run', return_value=SimpleNamespace(returncode=0,stdout=json.dumps([container('merchant',False)]))):
            selector.return_value.__enter__.return_value.select.return_value=[True]
            with host.maintenance(self.values['merchant']): pass

    def test_failed_backup_drain_never_stops_or_restarts_services(self):
        with patch.object(host, 'compose', return_value='merchant-id database-id') as compose, patch.object(host, 'run', return_value=json.dumps(list(self.values.values()))) as run, patch.object(host, 'maintenance', side_effect=host.Error('busy')), patch.object(host, 'wait_ready') as ready:
            with self.assertRaises(host.Error): host.backup()
        self.assertEqual(compose.call_args_list[0].args, ('ps', '--all', '--quiet'))
        self.assertEqual(compose.call_count, 1)
        run.assert_called_once_with(['docker', 'inspect', 'merchant-id', 'database-id'], capture=True)
        ready.assert_not_called()

    def test_start_existing_keeps_original_ids_and_order(self):
        stopped = {name: container(name, False) for name in ('merchant', 'database')}
        with patch.object(host, 'run') as run, patch.object(host, 'wait_database') as wait:
            host.start_existing(stopped)
        self.assertEqual([call.args[0] for call in run.call_args_list], [['docker', 'start', 'database-id'], ['docker', 'start', 'merchant-id']])
        wait.assert_called_once_with('database-id')

    def test_partial_stop_failure_restarts_only_successfully_stopped_container(self):
        with patch.object(host, 'run', side_effect=[None, host.Error('db stop failed')]), patch.object(host, 'start_existing') as start:
            with self.assertRaises(host.Error): host.stop_existing(self.values)
        self.assertEqual(set(start.call_args.args[0]), {'merchant'})

    def test_upgrade_holds_guard_after_build_until_replacement(self):
        calls = []
        @contextlib.contextmanager
        def guard(_):
            calls.append('guard-acquire')
            try: yield
            finally: calls.append('guard-release')
        with patch.object(host, 'validate_inputs'), patch.object(host, 'label_container_inputs'), patch.object(host, 'check_capacity'), patch.object(host, 'containers', return_value=self.values), patch.object(host, 'compose', side_effect=lambda *args: calls.append(args)), patch.object(host, 'maintenance', guard), patch.object(host, 'stop_existing', side_effect=lambda _: calls.append('stop')), patch.object(host, 'wait_ready', side_effect=lambda: calls.append('ready')):
            host.runtime_update()
        self.assertEqual(calls, [('build', '--pull'), 'guard-acquire', 'stop', ('up', '-d'), 'guard-release', 'ready'])
        self.assertFalse((self.root / '.runtime-update.json').exists())

    def test_failed_upgrade_guard_never_replaces_container(self):
        with patch.object(host, 'validate_inputs'), patch.object(host, 'label_container_inputs'), patch.object(host, 'check_capacity'), patch.object(host, 'containers', return_value=self.values), patch.object(host, 'compose') as compose, patch.object(host, 'maintenance', side_effect=host.Error('update running')), patch.object(host, 'stop_existing') as stop:
            with self.assertRaises(host.Error): host.runtime_update()
        compose.assert_called_once_with('build', '--pull')
        stop.assert_not_called()
        self.assertFalse((self.root / '.runtime-update.json').exists())

    def test_upgrade_rechecks_container_set_after_build(self):
        with patch.object(host, 'validate_inputs'), patch.object(host, 'label_container_inputs'), patch.object(host, 'check_capacity'), patch.object(host, 'containers', side_effect=[self.values, {}]), patch.object(host, 'compose'), patch.object(host, 'maintenance') as guard, patch.object(host, 'stop_existing') as stop:
            with self.assertRaises(host.Error): host.runtime_update()
        guard.assert_not_called()
        stop.assert_not_called()

    def test_failed_readiness_keeps_previous_image_record(self):
        with patch.object(host, 'validate_inputs'), patch.object(host, 'label_container_inputs'), patch.object(host, 'check_capacity'), patch.object(host, 'containers', return_value=self.values), patch.object(host, 'compose'), patch.object(host, 'maintenance', return_value=contextlib.nullcontext()), patch.object(host, 'stop_existing'), patch.object(host, 'wait_ready', side_effect=host.Error('failed')):
            with self.assertRaises(host.Error): host.runtime_update()
        self.assertEqual(json.loads((self.root / '.runtime-update.json').read_text())['previous_images']['merchant'], 'old-image-merchant')


class RecoveryTests(unittest.TestCase):
    signed = test_runtime.SignedBootstrapTests.signed

    def setUp(self):
        test_runtime.SignedBootstrapTests.setUp(self)
        self.root_patch = patch.object(recovery, 'ROOT', self.path)
        self.key_patch = patch.object(recovery.releases, 'HERE', self.path)
        self.root_patch.start()
        self.key_patch.start()
        import hashlib
        self.data = b'fixture = True\n'
        self.payload['installer'].update(size=len(self.data), sha256=hashlib.sha256(self.data).hexdigest())

    def tearDown(self):
        self.key_patch.stop()
        self.root_patch.stop()
        test_runtime.SignedBootstrapTests.tearDown(self)

    def test_cached_old_cli_available_without_installed_cli_or_network(self):
        path = recovery.cache(self.signed(), self.data)
        common.write(self.path / 'update-pending.json', json.dumps({'old_version': '3.5.0'}))
        with patch.object(recovery.releases, 'fetch', side_effect=AssertionError('offline')), patch.object(recovery, 'merchant', return_value='verified old cli') as load:
            self.assertEqual(recovery.load(), 'verified old cli')
        load.assert_called_once_with(path)
        self.assertFalse((self.path / 'scripts/whollycrypto-cli.py').exists())

    def test_tampered_recovery_cache_is_never_executed(self):
        path = recovery.cache(self.signed(), self.data)
        common.write(path, b'changed')
        with self.assertRaises(common.Error): recovery.prepare('3.5.0')

    def test_recovery_never_substitutes_latest_release(self):
        with patch.object(recovery.releases, 'fetch', return_value=json.dumps(self.signed()).encode()) as fetch:
            with self.assertRaises(common.Error): recovery.prepare('8.1.4')
        fetch.assert_called_once_with('/merchant/8.1.4/manifest.json', 65536)

    def test_invalid_recovery_version_rejected_before_network(self):
        for name in ('../x', 'https://example.com', '1.0.0/foo', None):
            with self.subTest(name=name), patch.object(recovery.releases, 'fetch') as fetch:
                with self.assertRaises(common.Error): recovery.prepare(name)
                fetch.assert_not_called()

    def test_update_preserves_original_signed_verifier_and_credit_checks(self):
        calls=[]
        original = Mock(side_effect=lambda args: calls.append('native-update'))
        module=SimpleNamespace(update=original, say=lambda _: None, Failure=RuntimeError, error_message=str)
        module.main=lambda: module.update(SimpleNamespace(check=False))
        with patch.object(sys, 'argv', ['whollycrypto', 'update']), patch.object(cli, 'merchant', return_value=module), patch.object(recovery, 'prepare_current', side_effect=lambda: calls.append('verified-cache')):
            cli.main()
        self.assertEqual(calls, ['verified-cache', 'native-update'])

    def test_cache_failure_prevents_file_switch(self):
        original=Mock()
        module=SimpleNamespace(update=original, say=lambda _: None, Failure=RuntimeError, error_message=str)
        module.main=lambda: module.update(SimpleNamespace(check=False))
        with patch.object(sys, 'argv', ['whollycrypto', 'update']), patch.object(cli, 'merchant', return_value=module), patch.object(recovery, 'prepare_current', side_effect=common.Error('bad signature')):
            with self.assertRaises(common.Error): cli.main()
        original.assert_not_called()


class SupervisorSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root_patch=patch.object(supervisor, 'ROOT', Path(self.directory.name))
        self.root_patch.start()
        self.manager=supervisor.Supervisor()

    async def asyncTearDown(self):
        self.root_patch.stop()
        self.directory.cleanup()

    async def test_maintenance_blocks_new_jobs_and_restores_only_previous_timers(self):
        timer='whollycrypto-domain-apply.timer'
        await self.manager.start(timer)
        state=await self.manager.request({'action':'maintenance-begin'})
        self.assertEqual(state['timers'], [timer])
        self.assertFalse(self.manager.timers)
        with self.assertRaises(common.Error): await self.manager.start('whollycrypto-backup.service')
        with self.assertRaises(common.Error): await self.manager.request({'action':'maintenance-begin'})
        await self.manager.request({'action':'maintenance-end'})
        self.assertEqual(self.manager.timers, {timer})

    async def test_shutdown_accepts_timer_restore_without_launching_work(self):
        self.manager.stopping=True
        await self.manager.start('whollycrypto-domain-apply.timer')
        self.assertFalse(self.manager.timers)
        with self.assertRaises(common.Error): await self.manager.start('whollycrypto-backup.service')

    async def test_control_socket_stays_available_until_active_worker_finishes(self):
        socket_path=str(Path(self.directory.name)/'control.sock')
        done=[]
        async def worker():
            await self.manager.shutdown.wait()
            await asyncio.sleep(.02)
            reader, writer=await asyncio.open_unix_connection(socket_path)
            writer.write(b'{"action":"ping"}\n')
            await writer.drain()
            response=json.loads(await reader.readline())
            done.append(response['ok'])
            writer.close()
            await writer.wait_closed()
        self.manager.tasks['whollycrypto-domain-apply.service']=asyncio.create_task(worker())
        with patch.object(supervisor, 'SOCKET', socket_path), patch.object(self.manager, 'start', new=AsyncMock()):
            task=asyncio.create_task(self.manager.run())
            while not Path(socket_path).exists(): await asyncio.sleep(.01)
            self.manager.shutdown.set()
            await asyncio.wait_for(task, 3)
        self.assertEqual(done, [True])


class DatabaseProvisionTests(unittest.TestCase):
    def shell(self, phase, role='0', database='0', valid=True):
        script='''set -Eeuo pipefail
source "$1"
DB_PHASE=$2; DB_PASSWORD=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
db_sql(){
 case "$*" in
  *'SELECT count(*) FROM pg_roles'*) echo "$3";;
  *'SELECT count(*) FROM pg_database'*) echo "$4";;
  *'CREATE DATABASE'*) echo CREATED_DATABASE >&2;;
  *'REVOKE ALL'*) echo REVOKED >&2;;
  '') echo CREATED_ROLE >&2; while IFS= read -r unused; do :; done;;
  *) exit 89;;
 esac
}
'''
        # Embed fixed fixture booleans, never use a real database or credentials.
        script=script.replace('echo "$3"', 'echo '+role).replace('echo "$4"','echo '+database)
        script+='\ndb_verify(){ '+('return 0' if valid else 'return 1')+'; }; db_checkpoint_write(){ echo CHECKPOINT_$1 >&2; }; db_provision\n'
        return subprocess.run(['bash','-c',script,'test',str(REPO/'runtime/db-provision.sh'),phase],text=True,capture_output=True)

    def test_fresh_database_completes_checkpoint(self):
        result=self.shell('pending')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('CREATED_ROLE',result.stderr)
        self.assertIn('CREATED_DATABASE',result.stderr)
        self.assertIn('CHECKPOINT_complete',result.stderr)

    def test_interrupted_after_role_creation_resumes_without_reset(self):
        result=self.shell('pending',role='1')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('CREATED_ROLE',result.stderr)
        self.assertIn('CREATED_DATABASE',result.stderr)

    def test_interrupted_after_database_creation_rechecks_and_finishes(self):
        result=self.shell('pending',role='1',database='1')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('CREATED_',result.stderr)
        self.assertIn('CHECKPOINT_complete',result.stderr)

    def test_legacy_database_is_validated_without_creating_objects(self):
        result=self.shell('legacy')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('CREATED_',result.stderr)
        self.assertIn('CHECKPOINT_complete',result.stderr)

    def test_invalid_existing_database_is_never_reset(self):
        for phase in ('legacy','complete'):
            result=self.shell(phase,valid=False)
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn('CREATED_',result.stderr)
            self.assertNotIn('CHECKPOINT_complete',result.stderr)

    def test_wrong_credentials_do_not_mark_pending_database_complete(self):
        result=self.shell('pending',role='1',database='1',valid=False)
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn('CHECKPOINT_complete',result.stderr)


if __name__ == '__main__':
    unittest.main()
