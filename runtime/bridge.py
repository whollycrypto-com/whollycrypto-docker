#!/usr/bin/python3
"""Small, allowlisted compatibility bridge. This is NOT a systemd emulator."""
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Error, control


def main():
    name, args = Path(sys.argv[0]).name, sys.argv[1:]
    if name == 'systemd-run':
        expected = ['--unit=whollycrypto-console-update.service', '--collect', '--no-block',
                    '--property=Type=oneshot', '--property=UMask=0077', '--property=TimeoutStartSec=1800',
                    '--property=WorkingDirectory=/root/whollycrypto', '/usr/bin/python3',
                    '/root/whollycrypto/scripts/whollycrypto-cli.py', '_console-update-run']
        if len(args) != len(expected) + 1 or args[:-1] != expected or not re.fullmatch('[0-9a-f-]{36}', args[-1]):
            raise Error('Only the signed merchant console update worker is supported.')
        control('console-update', job=args[-1])
        return 0
    if name == 'journalctl':
        if args != ['--unit=whollycrypto.service', '--lines=80', '--no-pager']:
            raise Error('Use whollycrypto logs for the application log.')
        print(control('logs', unit='whollycrypto.service')['output'], end='')
        return 0
    if not args:
        raise Error('A supported container service action is required.')
    action = args.pop(0)
    if action == '--version':
        print('Wholly Crypto container service bridge 1 (not systemd)')
        return 0
    if action == 'daemon-reload' and not args:
        control('ping')
        return 0
    properties, units, quiet, value, now = [], [], False, False, False
    while args:
        item = args.pop(0)
        if item in ('--property', '-p') and args:
            properties.extend(args.pop(0).split(','))
        elif item.startswith('--property='):
            properties.extend(item.split('=', 1)[1].split(','))
        elif item == '--quiet':
            quiet = True
        elif item == '--value':
            value = True
        elif item == '--now':
            now = True
        elif item.startswith('-'):
            raise Error('Unsupported container service option.')
        else:
            units.append(item if '.' in item else item + '.service')
    if not units:
        raise Error('An explicit managed service is required.')
    result = control(action, units=units, properties=properties, now=now)
    if action == 'show':
        for values in result['units']:
            for key, item in values.items():
                print(str(item) if value else key + '=' + str(item))
    elif action in ('is-active', 'is-enabled'):
        if not quiet:
            print('\n'.join(result['states']))
        return 0 if result['active'] else 3
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Error as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('Container service request failed.', file=sys.stderr)
        sys.exit(1)
