"""Container plumbing only; the merchant application is a separate signed download."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import tempfile

ROOT = Path('/root/whollycrypto')
SOCKET = '/run/whollycrypto-container/control.sock'
HERE = Path(__file__).resolve().parent


class Error(Exception):
    pass


def private(path, maximum=65536):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_size > maximum:
            raise Error('A required private file has unsafe ownership, permissions or size.')
        return stream.read(maximum + 1)


def write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise Error('Refusing to replace a symbolic link.')
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data.encode() if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def merchant(path=None):
    path = path or ROOT / 'scripts/whollycrypto-cli.py'
    private(path, 1024 * 1024)
    spec = importlib.util.spec_from_file_location('signed_merchant_cli', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def control(action, **values):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(1900)
        connection.connect(SOCKET)
        connection.sendall(json.dumps(dict(action=action, **values)).encode() + b'\n')
        response = connection.makefile('rb').readline(65537)
    if len(response) > 65536 or not response.endswith(b'\n'):
        raise Error('Invalid container supervisor response.')
    result = json.loads(response)
    if not result.get('ok'):
        raise Error(result.get('error', 'Container operation failed.'))
    return result
