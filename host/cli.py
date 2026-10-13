#!/usr/bin/env python3
"""Host helper for the optional Wholly Crypto Docker installation."""
import argparse
import contextlib
import datetime as dt
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ['docker', 'compose', '--project-name', 'whollycrypto', '--project-directory', str(ROOT), '-f', str(ROOT / 'compose.yaml')]
TIMERS = ['whollycrypto-' + name + '.timer' for name in ('domain-apply', 'icon-sync', 'backup', 'software-worker', 'certbot')]
SECRET_NAMES = ('database_password', 'postgres_password', 'basic_password')


class Error(Exception):
    pass


def run(args, capture=False, **options):
    result = subprocess.run([str(arg) for arg in args], check=False, text=True,
                            stdout=subprocess.PIPE if capture else None, **options)
    if result.returncode:
        raise Error('Command failed. Review the output above; existing data was not erased.')
    return result.stdout if capture else None


def compose(*args, **options):
    return run([*DOCKER, *args], **options)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.setup-write-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)  # Atomic, exclusive publication.
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def domain(value):
    value = value.strip().lower()
    if (len(value) > 190 or '.' not in value or
        any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in value.split('.'))):
        raise Error('Enter a base domain such as example.com, without a protocol, path or wildcard.')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise Error('Use a DNS domain, not an IP address.')


def private_read(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'r') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_size > 65536:
            raise Error('Setup and secret files must be root-owned, mode 600 (or stricter), regular files.')
        return stream.read()


def finish_configuration():
    """Replay the private journal; never regenerate a previously saved secret."""
    journal = ROOT / '.setup-pending.json'
    values = json.loads(private_read(journal))
    if set(values) != {'setup', 'secrets'} or set(values['secrets']) != set(SECRET_NAMES):
        raise Error('Incomplete setup journal; inspect it privately. Nothing was overwritten.')
    directory = ROOT / 'secrets'
    if directory.exists() or directory.is_symlink():
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise Error('The secrets directory must be root-owned, mode 700, and not a symbolic link.')
    else:
        directory.mkdir(mode=0o700)
    if any(path.name not in SECRET_NAMES for path in directory.iterdir()):
        raise Error('Unexpected files in the secrets directory. Nothing was replaced.')
    files = [(directory / name, values['secrets'][name] + '\n') for name in SECRET_NAMES]
    files.append((ROOT / 'setup.json', json.dumps(values['setup'], indent=2) + '\n'))
    for path, content in files:
        if path.exists() or path.is_symlink():
            if private_read(path) != content:
                raise Error('Saved setup differs from its recovery journal. Inspect privately; no values were overwritten.')
        else:
            save(path, content)
    for path in (directory, ROOT):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    journal.unlink()


def validate_inputs():
    setup = json.loads(private_read(ROOT / 'setup.json'))
    if setup.get('format') != 1 or setup.get('accept_terms') is not True:
        raise Error('Invalid setup choices. No containers were started.')
    info = (ROOT / 'secrets').lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise Error('The secrets directory must be root-owned, mode 700, and not a symbolic link.')
    for name in SECRET_NAMES:
        value = private_read(ROOT / 'secrets' / name).rstrip('\n')
        if (name != 'basic_password' and not re.fullmatch('[0-9a-f]{64}', value)) or (name == 'basic_password' and (len(value) < 12 or len(value.encode()) > 72 or any(ord(c) < 32 for c in value))):
            raise Error('A saved setup secret is invalid. It was not regenerated.')
    return setup


def ask(label, default=''):
    return input(label + (' [' + default + ']' if default else '') + ': ').strip() or default


def configure():
    if (ROOT / '.setup-pending.json').exists():
        raise Error('Interrupted configuration found. Run resume; existing secrets will be retained.')
    if (ROOT / 'setup.json').exists():
        raise Error('Setup choices already exist. Use resume. After installation, manage domains in Settings → System.')
    if (ROOT / 'secrets').exists():
        raise Error('A secrets directory already exists. Inspect it before configuring; no secrets were overwritten.')
    base = domain(ask('Primary base domain (example.com)'))
    email = ask('Email for HTTPS renewal notices')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise Error('Enter a valid renewal email address.')
    names = {}
    for role, default in [('console', 'merchant'), ('pay', 'pay'), ('api', 'api')]:
        name = ask(role.capitalize() + ' subdomain', default).lower()
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', name) or name in names.values():
            raise Error('Use three distinct, valid subdomain labels.')
        names[role] = name
    additional = [domain(item) for item in ask('Additional base domains (comma-separated, optional)').split(',') if item.strip()]
    if len(set([base, *additional])) != len(additional) + 1 or len(additional) > 19:
        raise Error('Use at most 20 unique base domains.')
    ips = []
    for item in ask('Public VPS IP address(es), comma-separated').split(','):
        address = ipaddress.ip_address(item.strip())
        if not address.is_global:
            raise Error('Use the public IP of the Docker host, not a Docker/private IP.')
        ips.append(str(address))
    currency = ask('Default fiat currency', 'USD').upper()
    if not re.fullmatch('[A-Z]{3}', currency):
        raise Error('Enter a three-letter fiat currency code. The signed installer validates supported currencies.')
    username = ask('HTTP Basic username', 'admin')
    if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', username):
        raise Error('Use a simple Basic Auth username without spaces or colons.')
    password = getpass.getpass('HTTP Basic password (12+ characters; Enter to generate): ')
    generated = not password
    if generated:
        password = secrets.token_urlsafe(24)
    elif password != getpass.getpass('Confirm HTTP Basic password: '):
        raise Error('Passwords do not match.')
    if not 12 <= len(password) or len(password.encode()) > 72 or any(ord(char) < 32 for char in password):
        raise Error('Use at least 12 characters and at most 72 UTF-8 bytes, without control characters.')
    print('\nKeep TCP 80/443 open in the VPS and provider firewalls; keep SSH allowed.')
    print('Point DNS directly to this VPS. Keep Cloudflare DNS-only during initial setup; proxying can be enabled afterwards.')
    print('This stack owns ports 80/443. It does not migrate another installation or alter host firewall rules.\n')
    for item in [base, *additional]:
        for role, name in names.items():
            print('  ' + role.capitalize() + ': https://' + name + '.' + item)
    print('\nSoftware terms: https://www.whollycrypto.com/terms/')
    print('Let’s Encrypt agreement: https://letsencrypt.org/repository/')
    if ask('Accept these agreements and install', 'no').lower() not in ('yes', 'y'):
        raise Error('Setup cancelled. No secrets or configuration were created.')
    setup = dict(format=1, domain=base, additional_domains=additional,
                 subdomains=names, email=email, public_ips=ips, currency=currency, basic_user=username, accept_terms=True)
    values = {name: secrets.token_hex(32) for name in SECRET_NAMES[:2]}
    values['basic_password'] = password
    save(ROOT / '.setup-pending.json', json.dumps({'setup': setup, 'secrets': values}))
    finish_configuration()
    print('\nPrivate setup saved. Back up setup.json and secrets/; never commit or share them.')
    if generated:
        print('Generated HTTP Basic password: ' + password)


def prerequisites():
    if sys.version_info < (3, 9):
        raise Error('Python 3.9+ is required. Run ./whollycrypto-docker prerequisites.')
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise Error('The current signed merchant package requires a Linux amd64 (x86-64) Docker host.')
    if os.geteuid() != 0:
        raise Error('Run this helper as root so host secrets remain root-owned and private.')
    if not shutil.which('docker'):
        raise Error('Run ./whollycrypto-docker prerequisites to install Docker and its plugins.')
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'COMPOSE_PROJECT_NAME', 'COMPOSE_FILE', 'COMPOSE_PROFILES'):
        if os.environ.get(key):
            raise Error('Unset ' + key + '; only the local, rootful Docker project is supported.')
    if ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o022:
        raise Error('The integration directory must be root-owned and not writable by other users.')
    endpoint = run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], capture=True).strip()
    if endpoint not in ('unix:///var/run/docker.sock', 'unix:///run/docker.sock'):
        raise Error('Select the local rootful Docker context; remote and rootless engines are unsupported.')
    info = json.loads(run(['docker', 'info', '--format', '{{json .}}'], capture=True))
    if info.get('OSType') != 'linux' or info.get('Architecture') not in ('x86_64', 'amd64') or 'rootless' in str(info.get('SecurityOptions')):
        raise Error('A local rootful Linux amd64 Docker Engine is required.')
    version = compose('version', '--short', capture=True).strip().lstrip('v').split('.')[0]
    if not version.isdigit() or int(version) < 2:
        raise Error('Docker Compose v2 or newer is required.')
    run(['docker', 'buildx', 'version'], capture=True)


def label_container_inputs():
    # Keep SELinux enforcing. Label only these exact, read-only container inputs;
    # never relabel /root, the repository tree, a database or unrelated files.
    if shutil.which('getenforce'):
        state = run(['getenforce'], capture=True).strip()
        if state in ('Enforcing', 'Permissive'):
            if not shutil.which('chcon'):
                raise Error('SELinux is enabled. Install its chcon utility before continuing; enforcement was not changed.')
            files = [ROOT / 'setup.json', ROOT / 'runtime/db-entrypoint.sh', ROOT / 'runtime/db-init.sh',
                     *(ROOT / 'secrets' / name for name in ('database_password', 'postgres_password', 'basic_password'))]
            if any(path.is_symlink() or not path.is_file() for path in files):
                raise Error('An expected container input is missing or is a symbolic link.')
            print('Keep SELinux enabled: label the six container input files for shared read-only access.')
            run(['chcon', '-t', 'container_file_t', '-l', 's0', *files])


def progress():
    code = 'import sys,json; sys.path.insert(0,"/opt/whollycrypto-docker"); from common import control; print(json.dumps(control("ping")))'
    result = subprocess.run([*DOCKER, 'exec', '-T', 'merchant', 'python3', '-c', code],
                            text=True, capture_output=True, timeout=20)
    if result.returncode:
        return {}
    return json.loads(result.stdout)


def wait_ready(timeout=1800):
    deadline = time.monotonic() + timeout
    print('\nWaiting for verified application readiness. DNS and HTTPS can take several minutes.', flush=True)
    logs = subprocess.Popen([*DOCKER, 'logs', '--follow', '--tail=30', 'merchant'])
    try:
        while time.monotonic() < deadline:
            state = progress()
            if state.get('ready'):
                setup = validate_inputs()
                print('\n[OK] Wholly Crypto is ready: https://' + setup['subdomains']['console'] + '.' + setup['domain'], flush=True)
                print('Create your admin account there. Keep setup.json, secrets/ and full wallet backups private.')
                print('Health: ./whollycrypto-docker doctor | Full backup: ./whollycrypto-docker backup')
                return
            if state.get('bootstrap_state') == 'failed':
                raise Error('Application setup failed. Review the setup log, fix the cause, then run ./whollycrypto-docker resume. Wallets and saved choices were retained.')
            print('  Still working: ' + ('setup running' if state.get('bootstrap_state') == 'activating' else 'waiting for the container / application') + '…', flush=True)
            time.sleep(10)
        raise Error('Readiness was not confirmed within 30 minutes. Setup was not killed. Use status and logs before retrying.')
    finally:
        logs.terminate()  # Only the log follower, never a container or worker.
        try:
            logs.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logs.kill()
            logs.wait()


def check_capacity():
    directories = [ROOT]
    docker_root = run(['docker', 'info', '--format', '{{.DockerRootDir}}'], capture=True).strip()
    if docker_root and Path(docker_root).is_dir():
        directories.append(Path(docker_root))
    if any(shutil.disk_usage(path).free < 3 * 1024**3 for path in directories):
        raise Error('At least 3 GiB free space is required on both the integration and Docker data filesystems.')


def require_safe_recreate(state):
    """Never mistake an unreachable/older supervisor for a stopped worker."""
    identities = compose('ps', '--all', '--quiet', 'merchant', capture=True).split()
    if not identities:
        return
    details = json.loads(run(['docker', 'inspect', *identities], capture=True))
    if any(item.get('State', {}).get('Restarting') for item in details):
        raise Error('The merchant container is restarting. Inspect its logs before retrying; it was not replaced.')
    if not any(item.get('State', {}).get('Running') for item in details):
        return
    if not state:
        raise Error('Cannot verify the running container state. Inspect logs/status; it was not replaced.')
    if state.get('recovery_required') or state.get('busy'):
        raise Error('An update or worker needs to finish/recover first. No container was replaced.')
    workers = [timer.replace('.timer', '.service') for timer in TIMERS] + ['whollycrypto-console-update.service']
    states = compose('exec', '-T', 'merchant', 'systemctl', 'show', *workers,
                     '--property=ActiveState', '--value', capture=True).split()
    if len(states) != len(workers) or any(value not in ('inactive', 'failed') for value in states):
        raise Error('A worker is running or could not be verified. Retry when idle; no container was replaced.')


def install(resume=False):
    if not resume:
        if any(Path('/root/whollycrypto', name).exists() for name in ('config/whollycrypto.env', '.env')):
            raise Error('A native installation exists. Use a separate VPS; this is not a migration command.')
        for port in (80, 443):
            for family, address in [(socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')]:
                try:
                    with socket.socket(family) as probe:
                        if family == socket.AF_INET6:
                            probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                        probe.bind((address, port))
                except OSError as error:
                    if family == socket.AF_INET6 and error.errno in (97, 99):
                        continue
                    raise Error('TCP ' + str(port) + ' is unavailable. Use a fresh VPS; no existing web server was stopped.') from None
        check_capacity()
        configure()
    else:
        if (ROOT / '.setup-pending.json').exists():
            finish_configuration()
        if not (ROOT / 'setup.json').is_file():
            raise Error('No setup choices found. Run setup first.')
    validate_inputs()
    if resume:
        state = progress()
        if state.get('recovery_required'):
            raise Error('An interrupted update requires cli recover first. No container was restarted.')
        if state.get('ready') or state.get('bootstrap_state') == 'activating':
            wait_ready()
            return
        if state and state.get('busy'):
            raise Error('An application worker is still running. Wait for it; no container was restarted.')
        require_safe_recreate(state)
        check_capacity()
    label_container_inputs()
    print('\n[1/3] Build the runtime image (no merchant source or credentials in the image).', flush=True)
    compose('build', '--pull')
    print('\n[2/3] Start the private database and merchant containers.', flush=True)
    compose('up', '-d', '--force-recreate', 'merchant') if resume else compose('up', '-d')
    print('\n[3/3] Follow setup progress. Ctrl+C closes this view; installation continues.', flush=True)
    wait_ready()


def backup():
    """Cold, consistent snapshot of all four durable volumes plus host secrets."""
    directory = ROOT / 'backups' / dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    ids = compose('ps', '--all', '--quiet', capture=True).split()
    if len(ids) != 2:
        raise Error('Both configured service containers must exist before taking a full backup.')
    details = json.loads(run(['docker', 'inspect', *ids], capture=True))
    mounts, running, image = {}, [], None
    mapping = {'/root/whollycrypto': 'merchant', '/etc/nginx': 'nginx', '/etc/letsencrypt': 'certificates', '/var/lib/postgresql': 'database'}
    for container in details:
        service = container['Config']['Labels']['com.docker.compose.service']
        if service not in ('merchant', 'database'):
            raise Error('Unexpected service in this Compose project.')
        if container['State']['Running']:
            running.append(service)
        if service == 'merchant':
            image = container['Image']
        for mount in container['Mounts']:
            target = mapping.get(mount['Destination'])
            if mount['Type'] == 'volume' and target and (target != 'database' or service == 'database'):
                mounts[target] = mount['Name']
    if set(mounts) != set(mapping.values()) or not image:
        raise Error('The four required durable volumes were not found. No data was changed.')
    print('Creating a private full backup. Checkout briefly pauses while both services are stopped.', flush=True)
    merchant_stopped = False
    try:
        if 'merchant' in running:
            compose('exec', '-T', 'merchant', 'systemctl', 'stop', *TIMERS)
            deadline = time.monotonic() + 180
            workers = [timer.replace('.timer', '.service') for timer in TIMERS] + ['whollycrypto-console-update.service']
            while True:
                states = compose('exec', '-T', 'merchant', 'systemctl', 'show', *workers, '--property=ActiveState', '--value', capture=True).split()
                if not any(state in ('active', 'activating', 'deactivating', 'reloading') for state in states):
                    break
                if time.monotonic() >= deadline:
                    raise Error('A worker is still running. No services were stopped; retry after it finishes.')
                time.sleep(2)
        compose('stop', 'merchant')
        merchant_stopped = True
        compose('stop', 'database')
        payload = directory / 'data.tar.gz'
        command = ['docker', 'run', '--rm', '--network=none', '--read-only', '--entrypoint=tar']
        for target, name in sorted(mounts.items()):
            command += ['--mount', 'type=volume,src=' + name + ',dst=/snapshot/' + target + ',readonly']
        command += [image, '-C', '/snapshot', '-czf', '-', *sorted(mounts)]
        with payload.open('xb') as stream:
            result = subprocess.run(command, stdout=stream)
            if result.returncode:
                raise Error('Snapshot failed. Partial files were retained for diagnosis.')
            stream.flush()
            os.fsync(stream.fileno())
        with tarfile.open(payload, 'r:gz') as archive:
            for member in archive:
                if member.isfile():
                    with archive.extractfile(member) as stream:
                        while stream.read(1024 * 1024):
                            pass
        for source in [ROOT / 'setup.json', *sorted((ROOT / 'secrets').iterdir())]:
            if source.is_symlink() or not source.is_file():
                raise Error('Unexpected setup/secret file; snapshot was not marked complete.')
            target = directory / source.relative_to(ROOT)
            target.parent.mkdir(mode=0o700, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o600)
        digest = hashlib.sha256()
        with payload.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        save(directory / 'data.tar.gz.sha256', digest.hexdigest() + '  data.tar.gz\n')
        save(directory / 'backup.json', json.dumps({'format': 1, 'postgres_major': 18, 'image': image,
                'complete': True, 'created_at': directory.name, 'volumes': sorted(mounts)}, indent=2) + '\n')
        print('Verified backup: ' + str(directory))
        print('Contains unencrypted private keys and database credentials. Copy the WHOLE directory to secure offline storage.')
    finally:
        if 'database' in running:
            compose('up', '-d', 'database')
        if 'merchant' in running:
            compose('up', '-d', 'merchant')
            # A pre-stop failure may have left the existing merchant running.
            if not merchant_stopped:
                compose('exec', '-T', 'merchant', 'systemctl', 'enable', '--now', *TIMERS)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'resume', 'status', 'logs', 'update', 'backup', 'doctor', 'cli', 'stop', 'start'])
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    prerequisites()
    # Serialize mutating host operations; the merchant also keeps its own locks.
    descriptor = os.open(ROOT / '.operation.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        if args.command in ('setup', 'resume', 'update', 'backup', 'stop', 'start'):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command in ('setup', 'resume'):
            install(args.command == 'resume')
        elif args.command == 'status':
            compose('ps', '--all')
        elif args.command == 'logs':
            compose('logs', '--follow', '--tail=80', 'merchant')
        elif args.command == 'backup':
            backup()
        elif args.command in ('start', 'stop'):
            if args.command == 'start':
                validate_inputs()
                label_container_inputs()
                compose('up', '-d')
            else:
                compose('stop')
        else:
            command = args.arguments if args.command == 'cli' else [args.command, *args.arguments]
            if not command:
                raise Error('Provide a merchant CLI command, for example cli htaccess or cli 2fa-reset.')
            compose('exec', 'merchant', 'whollycrypto', *command)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nLog view closed. Use status to see whether the containers are still running.')
        sys.exit(130)
    except (Error, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print('[ERROR] ' + (str(error) if isinstance(error, Error) else 'The operation could not finish. Check permissions, input values and other running operations.'), file=sys.stderr)
        sys.exit(1)
