"""Hold the native update lock while the host drains/stops this container.

The host may execute this public file in an older runtime during an upgrade.
Only fixed service names and the native release lock are used. EOF releases the
guard; no arbitrary command is accepted over its input pipe.
"""
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import time

sys.path.insert(0, '/opt/whollycrypto-docker')
from common import Error, ROOT, control

TIMERS = ['whollycrypto-' + name + '.timer' for name in
          ('domain-apply', 'icon-sync', 'backup', 'software-worker', 'certbot')]
WORKERS = [name.replace('.timer', '.service') for name in TIMERS] + ['whollycrypto-console-update.service']


def guard(timeout=180):
    descriptor = os.open('/run/whollycrypto-release.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise Error('Unsafe maintenance lock. No containers were stopped.')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error('A merchant update or CLI operation is running. Wait for it; no containers were stopped.') from None
        state = control('ping')
        if state.get('bootstrap_state') == 'activating':
            raise Error('Setup is still running. Follow its logs before stopping or replacing the container.')
        if state.get('recovery_required'):
            raise Error('Recover the interrupted merchant update before maintenance.')
        modern = 'maintenance' in state.get('capabilities', [])
        previous = []
        entered = False
        try:
            if modern:
                previous = control('maintenance-begin')['timers']
                entered = True
            else:
                values = control('show', units=TIMERS, properties=['Id', 'ActiveState'])['units']
                previous = [value['Id'] for value in values if value['ActiveState'] == 'active']
            control('stop', units=TIMERS)
            deadline = time.monotonic() + timeout
            while True:
                values = control('show', units=WORKERS, properties=['ActiveState'])['units']
                if len(values) != len(WORKERS):
                    raise Error('Could not verify every background worker. No containers were stopped.')
                if all(value.get('ActiveState') in ('inactive', 'failed') for value in values):
                    break
                if time.monotonic() >= deadline:
                    raise Error('A background worker is still running. Retry after it finishes; no containers were stopped.')
                time.sleep(1)
            if (ROOT / 'update-pending.json').exists():
                raise Error('An interrupted update requires recovery before maintenance.')
            print(json.dumps({'ok': True, 'state': 'drained'}), flush=True)
            # Keep the native lock until the host closes this pipe or stops the
            # container. Timer restoration never starts/recreates a container.
            sys.stdin.buffer.read(1)
        finally:
            if entered:
                control('maintenance-end')
            elif previous:
                control('start', units=previous)


if __name__ == '__main__':
    try:
        guard()
    except Exception as error:
        message = str(error) if isinstance(error, Error) else 'Maintenance could not finish. Inspect container status; existing data was retained.'
        print(json.dumps({'ok': False, 'error': message}), flush=True)
        sys.exit(1)
