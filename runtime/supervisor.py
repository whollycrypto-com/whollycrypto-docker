"""PID 1 with explicit services, real job states and completion-based timers.

No arbitrary ExecStart parsing, host socket or privileged container is required.
Timers can be drained by the signed updater without killing active workers.
"""
import asyncio
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import sys
import time
import uuid

from common import Error, HERE, ROOT, SOCKET, merchant, private, write

APP = 'whollycrypto.service'
NGINX = 'nginx.service'
CONSOLE = 'whollycrypto-console-update.service'
COMMANDS = {
    APP: [str(ROOT / 'bin/whollycrypto')],
    NGINX: ['/usr/sbin/nginx', '-g', 'daemon off;'],
    'whollycrypto-domain-apply.service': [str(ROOT / 'bin/whollycrypto'), 'apply-domains'],
    'whollycrypto-icon-sync.service': ['/bin/bash', str(ROOT / 'scripts/sync-coingecko-icons.sh')],
    'whollycrypto-backup.service': ['/bin/bash', str(ROOT / 'scripts/backup-whollycrypto.sh')],
    'whollycrypto-software-worker.service': ['/usr/bin/python3', str(HERE / 'cli.py'), '_console-update-tick'],
    'whollycrypto-certbot.service': ['/usr/bin/certbot', 'renew', '--quiet'],
    CONSOLE: [],
    'bootstrap.service': ['/usr/bin/python3', str(HERE / 'bootstrap.py')],
}
TIMER_DELAYS = {'whollycrypto-domain-apply.timer': (30, 10),
                'whollycrypto-icon-sync.timer': (300, 21600),
                'whollycrypto-backup.timer': (3600, 86400),
                'whollycrypto-software-worker.timer': (45, 15),
                'whollycrypto-certbot.timer': (600, 43200)}
TIMEOUTS = {'whollycrypto-domain-apply.service': 18000, 'whollycrypto-icon-sync.service': 1800,
            'whollycrypto-backup.service': 1800, 'whollycrypto-software-worker.service': 300,
            'whollycrypto-certbot.service': 1800, CONSOLE: 1800, 'bootstrap.service': 7200}
PROPERTIES = {'Id', 'ActiveState', 'SubState', 'MainPID', 'LoadState', 'ExecMainStatus', 'UnitFileState'}
PERSISTENT_TIMERS = {'whollycrypto-icon-sync.timer', 'whollycrypto-backup.timer', 'whollycrypto-certbot.timer'}


class Supervisor:
    def __init__(self):
        self.processes, self.tasks, self.wanted, self.exits = {}, {}, set(), {}
        self.timers = set()
        self.next_run = {}
        self.completed = {}
        history = ROOT / 'config/container-jobs.json'
        if history.exists():
            try:
                values = json.loads(private(history))
                self.completed = {key: value for key, value in values.items()
                                  if key in PERSISTENT_TIMERS and type(value) in (int, float) and 0 <= value <= time.time() + 86400}
            except (Error, OSError, ValueError, AttributeError):
                print('[schedule] Could not read timer history; normal startup delays apply.', flush=True)
        self.ready = False
        self.maintenance = None
        self.stopping = False
        self.locks = {unit: asyncio.Lock() for unit in COMMANDS}
        self.shutdown = asyncio.Event()

    def state(self, unit):
        if unit in TIMER_DELAYS:
            active = unit in self.timers
            return ('active' if active else 'inactive', 'waiting' if active else 'dead', 0)
        process = self.processes.get(unit)
        if process and process.returncode is None:
            return ('active' if unit in (APP, NGINX) else 'activating', 'running' if unit in (APP, NGINX) else 'start', process.pid)
        if unit in self.tasks and not self.tasks[unit].done():
            return ('activating', 'start', 0)
        failed = self.exits.get(unit, 0) != 0
        return ('failed' if failed else 'inactive', 'failed' if failed else 'dead', 0)

    async def output(self, unit, stream):
        directory = ROOT / 'logs/container'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (unit + '.log')
        # Bounded private logs, separate from the Docker daemon's bounded logs.
        while data := await stream.read(16384):
            if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
                os.replace(path, path.with_suffix('.previous.log'))
            with open(path, 'ab') as output:
                output.write(data)
            if unit == 'bootstrap.service':
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()

    async def execute(self, unit, command):
        try:
            env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                   'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'}
            if unit not in (NGINX, 'bootstrap.service'):
                env.update(merchant().runtime_env())
            process = await asyncio.create_subprocess_exec(*command, env=env, cwd=str(ROOT),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True, umask=0o022 if unit == NGINX or 'domain-apply' in unit else 0o077)
            self.processes[unit] = process
            output = asyncio.create_task(self.output(unit, process.stdout))
            try:
                await asyncio.wait_for(process.wait(), TIMEOUTS.get(unit))
            except asyncio.TimeoutError:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 30)
                except asyncio.TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            await output
            self.exits[unit] = process.returncode
        except Exception:
            self.exits[unit] = 1
        finally:
            timer = unit.replace('.service', '.timer')
            if timer in TIMER_DELAYS:
                self.next_run[timer] = time.monotonic() + TIMER_DELAYS[timer][1]
                if timer in PERSISTENT_TIMERS:
                    if self.exits.get(unit, 1) == 0:
                        self.completed[timer] = time.time()
                        try:
                            write(ROOT / 'config/container-jobs.json', json.dumps(self.completed))
                        except (Error, OSError):
                            print('[schedule] Timer history could not be saved. Check disk space and permissions.', flush=True)
                    else:
                        self.next_run[timer] = time.monotonic() + 900
            print(f'[service] {unit}: exited {self.exits.get(unit, 1)}', flush=True)
            if unit == 'bootstrap.service' and self.exits.get(unit, 1):
                self.ready = False
                # Do not leave a partially configured signup/checkout online.
                await self.stop(APP)

    async def start(self, unit, command=None, wait=True):
        if self.stopping and unit in TIMER_DELAYS:
            # Finishing updaters may restore timers; acknowledge without new work.
            return
        if (self.stopping or self.maintenance is not None) and unit not in (APP, NGINX):
            raise Error('Container maintenance is active. No new background job was started.')
        if unit in TIMER_DELAYS:
            self.timers.add(unit)
            delay = TIMER_DELAYS[unit][0]
            if unit in self.completed:
                delay = max(5, TIMER_DELAYS[unit][1] - max(0, time.time() - self.completed[unit]))
            self.next_run.setdefault(unit, time.monotonic() + delay)
            return
        async with self.locks[unit]:
            existing = self.tasks.get(unit)
            if existing is None or existing.done():
                self.wanted.add(unit)
                self.tasks[unit] = asyncio.create_task(self.execute(unit, command or COMMANDS[unit]))
            task = self.tasks[unit]
        if unit in (APP, NGINX):
            # A start request succeeds only once the child exists, never just on queueing.
            for _ in range(100):
                await asyncio.sleep(.05)
                if task.done():
                    raise Error('The requested service did not start. Check its private log.')
                if self.state(unit)[2]:
                    return
            raise Error('Service start timed out.')
        elif wait:
            await asyncio.shield(task)
            if self.exits.get(unit, 1):
                raise Error('The requested worker failed. Check its private log.')

    async def stop(self, unit):
        if unit in TIMER_DELAYS:
            self.timers.discard(unit)
            return
        if unit not in (APP, NGINX) and self.state(unit)[0] in ('active', 'activating'):
            raise Error('An active worker cannot be interrupted. Stop its timer and wait for completion.')
        self.wanted.discard(unit)
        process = self.processes.get(unit)
        if process and process.returncode is None:
            os.killpg(process.pid, signal.SIGQUIT if unit == NGINX else signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 90)
            except asyncio.TimeoutError:
                raise Error('Graceful stop timed out; no force-stop was performed.') from None

    async def request(self, data):
        action = data.get('action')
        if action == 'ping':
            return {'protocol': 1, 'ready': self.ready and self.state(APP)[0] == self.state(NGINX)[0] == 'active',
                    'capabilities': ['maintenance'], 'maintenance': self.maintenance is not None,
                    'bootstrap_state': self.state('bootstrap.service')[0],
                    'recovery_required': (ROOT / 'update-pending.json').exists(),
                    'busy': any(self.state(unit)[0] in ('active', 'activating', 'deactivating')
                                for unit in COMMANDS if unit not in (APP, NGINX, 'bootstrap.service'))}
        if action == 'ready':
            self.ready = True
            return {}
        if action == 'maintenance-begin':
            if self.stopping or self.maintenance is not None:
                raise Error('Container maintenance is already active.')
            self.maintenance = set(self.timers)
            self.timers.clear()
            return {'timers': sorted(self.maintenance)}
        if action == 'maintenance-end':
            if self.maintenance is not None:
                previous, self.maintenance = self.maintenance, None
                if not self.stopping:
                    for timer in previous:
                        await self.start(timer)
            return {}
        if action == 'console-update':
            job = str(uuid.UUID(data.get('job', '')))
            if job != data.get('job') or self.state(CONSOLE)[0] in ('active', 'activating'):
                raise Error('An update is already running or its job ID is invalid.')
            await self.start(CONSOLE, ['/usr/bin/python3', str(HERE / 'cli.py'), '_console-update-run', job], wait=False)
            return {}
        if action == 'logs':
            if data.get('unit') != APP:
                raise Error('Only the application log is available through this command.')
            path = ROOT / 'logs/container' / (APP + '.log')
            if not path.is_file():
                return {'output': 'No application log yet.\n'}
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - 16000))
                return {'output': b'\n'.join(stream.read().splitlines()[-80:]).decode(errors='replace')}
        units = data.get('units', [])
        if (action == 'show' and units in [['certbot.timer'], ['certbot-renew.timer'], ['certbot-renewal.timer']]
                and data.get('properties') == ['LoadState']):
            return {'units': [{'LoadState': 'not-found'}]}
        if not isinstance(units, list) or not 1 <= len(units) <= 20 or any(u not in COMMANDS and u not in TIMER_DELAYS for u in units):
            raise Error('Unknown container service. Arbitrary units are not supported.')
        if action in ('is-active', 'is-enabled'):
            states = [self.state(unit)[0] for unit in units]
            return {'states': states, 'active': all(s == 'active' for s in states)}
        if action == 'show':
            properties = data.get('properties') or sorted(PROPERTIES)
            if not isinstance(properties, list) or any(p not in PROPERTIES for p in properties):
                raise Error('Unsupported service property.')
            values = []
            for unit in units:
                active, sub, pid = self.state(unit)
                all_values = dict(Id=unit, ActiveState=active, SubState=sub, MainPID=pid, LoadState='loaded',
                                  ExecMainStatus=self.exits.get(unit, 0), UnitFileState='enabled' if unit in self.timers or unit in self.wanted else 'disabled')
                values.append({key: all_values[key] for key in properties})
            return {'units': values}
        if action not in ('start', 'stop', 'restart', 'enable', 'disable', 'reload'):
            raise Error('Unsupported container service action.')
        for unit in units:
            if action in ('stop', 'restart', 'disable'):
                await self.stop(unit)
            if action in ('start', 'restart') or (action == 'enable' and data.get('now')):
                if unit == CONSOLE or unit == 'bootstrap.service':
                    raise Error('Use the explicit bootstrap or console-update command.')
                await self.start(unit)
            if action == 'reload':
                if unit != NGINX or self.state(unit)[0] != 'active':
                    raise Error('Only a running Nginx service supports reload.')
                process = await asyncio.create_subprocess_exec('/usr/sbin/nginx', '-t', stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                if await process.wait():
                    raise Error('Nginx configuration test failed; the running configuration was kept.')
                os.kill(self.processes[NGINX].pid, signal.SIGHUP)
        return {}

    async def connection(self, reader, writer):
        try:
            credentials = writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack('3i', credentials)[1] != 0:
                raise Error('Only the internal root user can control container services.')
            raw = await asyncio.wait_for(reader.readline(), 5)
            if len(raw) > 8192:
                raise Error('Oversized service request.')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise Error('Invalid service request.')
            result = dict(ok=True, **await self.request(data))
        except Error as error:
            result = {'ok': False, 'error': str(error)}
        except Exception:
            result = {'ok': False, 'error': 'Container service request failed.'}
        writer.write(json.dumps(result).encode() + b'\n')
        try:
            await writer.drain()
        finally:
            writer.close()

    async def scheduler(self):
        while not self.stopping:
            if self.ready:
                for timer in list(self.timers):
                    unit = timer.replace('.timer', '.service')
                    if time.monotonic() >= self.next_run.get(timer, 0) and self.state(unit)[0] not in ('active', 'activating'):
                        await self.start(unit, wait=False)
            for unit in (APP, NGINX):
                if unit in self.wanted and self.state(unit)[0] not in ('active', 'activating'):
                    if unit == APP and (ROOT / 'update-pending.json').exists():
                        continue
                    try:
                        await self.start(unit)
                    except Error:
                        pass
            await asyncio.sleep(3)

    async def run(self):
        os.umask(0o077)
        Path(SOCKET).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if Path(SOCKET).exists():
            Path(SOCKET).unlink()
        server = await asyncio.start_unix_server(self.connection, path=SOCKET, limit=8193)
        os.chmod(SOCKET, 0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.shutdown.set)
        await self.start('bootstrap.service', wait=False)
        scheduler = asyncio.create_task(self.scheduler())
        await self.shutdown.wait()
        self.stopping = True
        self.ready = False
        self.timers.clear()
        scheduler.cancel()
        # Complete running workers first. Docker's stop_grace_period bounds this.
        # Keep control requests available: workers need reload/start/stop during
        # normal completion and rollback. Only NEW work is rejected above.
        jobs = [task for unit, task in self.tasks.items() if unit not in (APP, NGINX) and not task.done()]
        if jobs:
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.gather(*jobs)), 75)
            except asyncio.TimeoutError:
                print('[shutdown] A worker is still running; preserve any update journal and recover before restarting.', flush=True)
        for unit in (APP, NGINX):
            try:
                await self.stop(unit)
            except Error:
                pass
        for process in self.processes.values():
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
        server.close()
        await server.wait_closed()


if __name__ == '__main__':
    asyncio.run(Supervisor().run())
