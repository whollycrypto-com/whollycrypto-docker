#!/usr/bin/python3
"""Run the signed merchant CLI with an explicit container platform adapter.

Only the host/platform probe and its doctor label are adapted. Signature checks,
credit checks, backup, migration, rollback and update locks remain unmodified.
"""
import os
import platform
import sys
from pathlib import Path

# This launcher can be reached through /usr/local/sbin/whollycrypto.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Error, control, merchant


def container_platform():
    if os.geteuid() != 0 or platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise Error('This container requires Linux amd64 and its internal root user.')
    if control('ping').get('protocol') != 1:
        raise Error('The merchant container supervisor is unavailable.')
    return {'name': 'Debian container', 'id': 'debian', 'family': 'apt'}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'install':
        raise Error('Use ./whollycrypto-docker setup on the Docker host, not the native VPS installer.')
    module = merchant()
    module.check_platform = container_platform
    original_say = module.say
    def say(message):
        if 'Platform:' in str(message) or ('Platform' in str(message) and '/ systemd' in str(message)):
            message = str(message).replace('/ systemd', '/ container supervisor')
        original_say(message)
    module.say = say
    try:
        return module.main()
    except module.Failure as error:
        raise Error(module.error_message(error)) from None


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Error as error:
        print('[ERROR] ' + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('[ERROR] Container command failed. Check status and private service logs; credentials are not printed.', file=sys.stderr)
        sys.exit(1)
