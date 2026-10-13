"""Fresh container install/resume using only the verified public merchant runtime."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import sys
import tempfile
import time

from common import Error, HERE, ROOT, control, merchant, private, write
from releases import installer
import recovery

MARKER = ROOT / 'config/install-state.json'
CONTAINER_MARKER = ROOT / 'config/container-install.json'


def phase(text):
    print('[setup] ' + text, flush=True)


def configuration(module):
    setup = json.loads(private('/run/whollycrypto-setup.json'))
    if setup.get('format') != 1 or setup.get('accept_terms') is not True:
        raise Error('Run ./whollycrypto-docker setup and accept the installation agreements first.')
    setup['domain'] = module.validate_domain(setup['domain'])
    module.setup_subdomains(setup)
    module.setup_bases(setup)
    if (not isinstance(setup.get('email'), str) or not re.fullmatch(r'[^\s@\x00-\x1f]+@[^\s@\x00-\x1f]+\.[^\s@\x00-\x1f]+', setup['email'])
            or setup.get('currency') not in module.FIAT_CURRENCIES
            or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', setup.get('basic_user', ''))):
        raise Error('Invalid setup email, fiat currency or Basic Auth username.')
    ips = setup.get('public_ips', [])
    if not isinstance(ips, list) or not 1 <= len(ips) <= 16 or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise Error('Provide this VPS public IP addresses, not Docker/private addresses.')
    setup['nginx_layout'] = 'sites-enabled'
    return setup


def plumbing(module):
    for source in (ROOT / 'service').iterdir():
        if source.name not in {Path(name).name for name in module.SERVICE_FILES}:
            raise Error('Unexpected service definition in the installed release.')
        write(Path('/etc/systemd/system') / source.name, source.read_bytes(), 0o644)
    hook = Path('/etc/letsencrypt/renewal-hooks/deploy/whollycrypto-nginx')
    write(hook, (ROOT / 'deploy/certbot-deploy-hook').read_bytes(), 0o700)
    if not Path('/etc/letsencrypt/cli.ini').exists():
        write('/etc/letsencrypt/cli.ini', 'max-log-backups = 5\n', 0o644)
    module.install_cli_link()


def enable_workers(module):
    module.systemctl('enable', '--now', *module.TIMERS)
    module.install_certbot_timer()
    module.install_software_worker()


def main():
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = json.loads(private(CONTAINER_MARKER)) if CONTAINER_MARKER.exists() else {}
    if (ROOT / 'update-pending.json').exists():
        raise Error('Interrupted update found. Run ./whollycrypto-docker cli recover, then resume. Existing files and keys are preserved.')
    if state.get('complete'):
        phase('Starting the installed release; container rebuilds never replace merchant software.')
        module = merchant()
        if len(private(ROOT / 'config/wallet-master.key', 32)) != 32:
            raise Error('Wallet encryption key is invalid. It was not replaced.')
        plumbing(module)
        module.systemctl('start', 'nginx.service', module.SERVICE)
        module.wait_health(module.current_version())
        enable_workers(module)
        control('ready')
        phase('Merchant ready. Data, wallet keys and active domain settings were preserved.')
        return
    # Never adopt a native installation or regenerate an existing wallet key.
    if not state and any(ROOT.iterdir()):
        # The supervisor only creates its own bounded logs before bootstrap.
        if any(path.name != 'logs' for path in ROOT.iterdir()):
            raise Error('The merchant volume is not empty and is not a Docker installation. Use a fresh volume or an explicit recovery procedure.')
    if not state.get('package_installed'):
        phase('Download and verify the signed merchant installer and application.')
        write(CONTAINER_MARKER, json.dumps({'format': 1, 'complete': False, 'package_installed': False}))
        # Docker mounts /tmp noexec. Verify the signed executable in the private
        # application volume, as the native updater does, without weakening /tmp.
        with tempfile.TemporaryDirectory(prefix='.bootstrap-', dir=ROOT) as scratch:
            path, envelope = installer(scratch)
            module = merchant(path)
            manifest = module.verify_manifest(envelope, HERE / 'release-signing.pub', scratch)
            setup = configuration(module)
            # Persist an intent before installing any file; resumes retain the pinned release.
            package = module.stage_package(manifest, scratch)
            for source in module.tree_files(package):
                write(ROOT / source.relative_to(package), source.read_bytes(), stat.S_IMODE(source.stat().st_mode))
            write(ROOT / 'config/release-signing.pub', (HERE / 'release-signing.pub').read_bytes())
            write(ROOT / 'config/release-sequence.json', json.dumps({'sequence': manifest['sequence'], 'version': manifest['version']}))
            recovery.cache(envelope, path.read_bytes())
            state = {'format': 1, 'complete': False, 'package_installed': True, 'version': manifest['version']}
            write(CONTAINER_MARKER, json.dumps(state))
    module = merchant()
    setup = configuration(module)
    write(MARKER, json.dumps(dict(setup, complete=False)))
    phase('Check DNS and initialize private application configuration.')
    for base in module.setup_bases(setup):
        module.validate_dns(base, set(setup['public_ips']), setup['subdomains'])
    wallet_key = ROOT / 'config/wallet-master.key'
    if not wallet_key.exists():
        write(wallet_key, secrets.token_bytes(32), 0o400)
    if len(private(wallet_key, 32)) != 32:
        raise Error('Wallet encryption key is invalid. It was not replaced.')
    password = private('/run/secrets/database_password').decode().strip()
    if not re.fullmatch('[0-9a-f]{64}', password):
        raise Error('Invalid application database secret.')
    if not module.ENV.exists():
        env = {
            'DATABASE_URL': 'postgresql://whollycrypto:' + password + '@localhost/whollycrypto?host=/var/run/postgresql',
            'WHOLLY_BIND_ADDR': '127.0.0.1:8080', 'WHOLLY_STATIC_DIR': str(ROOT / 'static'),
            'WHOLLY_WALLET_MASTER_KEY_FILE': str(wallet_key), 'WHOLLY_PUBLIC_IPS': ','.join(setup['public_ips']),
            'WHOLLY_CERTBOT_EMAIL': setup['email'], 'WHOLLY_NGINX_LAYOUT': 'sites-enabled',
            'WHOLLY_TREASURY_BROADCAST_ENABLED': 'true', 'WHOLLYCRYPTO_BACKUP_RETENTION_COUNT': '3',
            'WHOLLYCRYPTO_BACKUP_PGHOST': '/var/run/postgresql', 'WHOLLYCRYPTO_BACKUP_PGPASSWORD': password,
            'RUST_LOG': 'whollycrypto=info,tower_http=warn',
        }
        write(module.ENV, ''.join(key + '=' + shlex.quote(value) + '\n' for key, value in env.items()))
    module.regular(module.ENV, private=True)
    if module.sql("SELECT current_user = 'whollycrypto' AND NOT rolsuper AND NOT rolcreaterole AND NOT rolcreatedb FROM pg_roles WHERE rolname=current_user;") != 't':
        raise Error('The merchant database role must not be a superuser or role/database administrator.')
    phase('Apply database migrations. Existing keys are never regenerated.')
    module.run([ROOT / 'bin/whollycrypto', 'migrate'], env=module.runtime_env(), timeout=600)
    if module.sql('SELECT (SELECT count(*) FROM console_users) + (SELECT count(*) FROM projects);') != '0':
        raise Error('This unfinished installation contains accounts or projects. Setup will not reconfigure an in-use installation.')
    if not module.BASIC_AUTH.exists():
        basic = private('/run/secrets/basic_password').decode().rstrip('\n')
        if len(basic) < 12 or len(basic.encode()) > 72 or any(ord(char) < 32 for char in basic):
            raise Error('The Basic Auth password needs 12+ characters, at most 72 UTF-8 bytes, and no control characters.')
        hashed = module.run(['htpasswd', '-niB', '-C', '12', setup['basic_user']], data=(basic + '\n').encode())
        write(module.BASIC_AUTH, hashed, 0o640)
        os.chown(module.BASIC_AUTH, 0, module.nginx_worker_gid())
    phase('Issue HTTPS certificates. Keep DNS-only records and inbound ports 80/443 open.')
    config = module.configure_https(setup)
    module.configure_service_domains(setup)
    module.configure_default_currency(setup)
    plumbing(module)
    phase('Start merchant and verify the protected HTTPS endpoints.')
    module.systemctl('start', module.SERVICE)
    module.wait_health(module.current_version())
    write(module.nginx_paths('sites-enabled')[0], config, 0o644)
    module.run(['nginx', '-t'])
    module.systemctl('reload', 'nginx')
    for role, host in module.setup_hosts(setup):
        route, expected = ('/', '401') if role == 'console' else ('/healthz', '200') if role == 'api' else ('/', '200')
        # Local routing avoids dependence on the host's NAT hairpin. TLS and the
        # requested hostname are still verified against the public CA certificate.
        status = ''
        for attempt in range(10):
            try:
                status = module.run(['curl', '--noproxy', '*', '--proto', '=https', '--silent', '--show-error',
                                     '--connect-timeout', '3', '--max-time', '5', '--resolve', host + ':443:127.0.0.1',
                                     '--output', '/dev/null', '--write-out', '%{http_code}', 'https://' + host + route]).decode()
                if status == expected:
                    break
            except module.Failure:
                pass
            time.sleep(.5)
        if status != expected:
            raise Error('A protected HTTPS endpoint failed validation. Resume after fixing DNS/TLS or reviewing the Nginx log.')
    phase('Create the first verified recovery backup and enable background jobs.')
    module.create_backup('docker-install')
    write(MARKER, json.dumps(dict(setup, complete=True, version=module.current_version())))
    write(CONTAINER_MARKER, json.dumps(dict(state, complete=True)))
    enable_workers(module)
    control('ready')
    phase('Ready: https://' + setup['subdomains']['console'] + '.' + setup['domain'])
    phase('Sign in through Basic Auth, then create your separate administrator account.')


if __name__ == '__main__':
    try:
        main()
    except Error as error:
        print('[ERROR] ' + str(error), file=sys.stderr, flush=True)
        sys.exit(1)
    except Exception as error:
        # Only explicitly safe merchant errors are shown. Never dump URLs,
        # database credentials, environment values or arbitrary tracebacks.
        try:
            module = merchant()
            message = module.error_message(error)
        except Exception:
            message = 'Setup could not finish. Check DNS, open ports, database readiness and available disk space.'
        print('[ERROR] ' + message + ' Fix the issue, then run ./whollycrypto-docker resume. Data and keys were preserved.', file=sys.stderr, flush=True)
        sys.exit(1)
