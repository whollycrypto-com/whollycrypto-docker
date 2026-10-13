"""A signature-verified recovery CLI outside the mutable application package."""
import hashlib
import json
from pathlib import Path
import re
import tempfile

from common import Error, ROOT, merchant, private, write
import releases

VERSION = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z')


def version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise Error('Invalid recovery release version. No application files were changed.')
    return value


def cache(envelope, data):
    """Reverify before durable publication, including when seeded at install."""
    name = version(envelope.get('signed', {}).get('version'))
    directory = ROOT / 'config/container-recovery' / name
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.verify-', dir=directory) as scratch:
        info = releases.verify(envelope, scratch)
    if len(data) != info['size'] or hashlib.sha256(data).hexdigest() != info['sha256']:
        raise Error('Recovery installer checksum failed. No unverified code was executed.')
    write(directory / 'installer.py', data)
    # Manifest is the commit marker. A partial cache is never executed.
    write(directory / 'manifest.json', json.dumps(envelope))
    return directory / 'installer.py'


def prepare(name):
    name = version(name)
    directory = ROOT / 'config/container-recovery' / name
    manifest, path = directory / 'manifest.json', directory / 'installer.py'
    if manifest.exists() and path.exists():
        envelope = json.loads(private(manifest))
        if envelope.get('signed', {}).get('version') != name:
            raise Error('Recovery cache belongs to a different release.')
        data = private(path, 1024 * 1024)
        with tempfile.TemporaryDirectory(prefix='.verify-', dir=directory) as scratch:
            info = releases.verify(envelope, scratch)
        if len(data) != info['size'] or hashlib.sha256(data).hexdigest() != info['sha256']:
            raise Error('Recovery cache verification failed. Restore a trusted copy; it was not executed.')
        return path
    # Older Docker installations do not have a recovery cache. Fetch only the
    # exact signed release, never an arbitrary URL or the current stable CLI.
    envelope = json.loads(releases.fetch('/merchant/' + name + '/manifest.json', 65536))
    if envelope.get('signed', {}).get('version') != name:
        raise Error('The signed recovery release does not match the interrupted update.')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.verify-', dir=directory) as scratch:
        info = releases.verify(envelope, scratch)
    return cache(envelope, releases.fetch(info['path'], info['size']))


def prepare_current():
    return prepare(version(json.loads(private(ROOT / 'release.json'))['version']))


def load():
    journal = json.loads(private(ROOT / 'update-pending.json', 2 * 1024 * 1024))
    name = version(journal.get('old_version'))
    # The signed native recovery procedure still enforces schema, journal,
    # path and confirmation checks. This module never rolls back a database.
    return merchant(prepare(name))
