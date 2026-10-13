"""Verify the public installer BEFORE importing/executing it. No credentials sent."""
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from common import Error, HERE, write

ORIGIN = 'https://releases.whollycrypto.com'
REPOSITORY = 'whollycrypto-com/whollycrypto'
GITHUB = 'https://github.com/' + REPOSITORY + '/releases/'
ASSET_HOSTS = {'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        old, new = urllib.parse.urlsplit(request.full_url), urllib.parse.urlsplit(url)
        if (old.hostname == 'releases.whollycrypto.com' or new.scheme != 'https'
                or new.username or new.password or new.netloc != new.hostname or new.fragment
                or (new.hostname == 'github.com' and (not new.path.startswith('/' + REPOSITORY + '/releases/download/') or new.query))
                or new.hostname not in ASSET_HOSTS | {'github.com'}):
            raise Error('Unsafe release redirect. No installer was executed.')
        return super().redirect_request(request, fp, code, message, headers, url)


def fetch(path, maximum):
    if path == '/channels/stable.json':
        mirror = GITHUB + 'latest/download/manifest.json'
    elif re.fullmatch(r'/merchant/\d+\.\d+\.\d+/installer\.py', path):
        mirror = GITHUB + 'download/v' + path.split('/')[2] + '/installer.py'
    elif re.fullmatch(r'/merchant/\d+\.\d+\.\d+/manifest\.json', path):
        mirror = GITHUB + 'download/v' + path.split('/')[2] + '/manifest.json'
    else:
        raise Error('Unrecognized bootstrap download path.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())
    for url in (ORIGIN + path, mirror):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'WhollyCrypto-Docker/1', 'Accept-Encoding': 'identity'})
            with opener.open(request, timeout=30) as response:
                data = response.read(maximum + 1)
                if response.status != 200 or len(data) > maximum:
                    raise Error('Invalid or oversized release download.')
                return data
        except urllib.error.HTTPError as error:
            if error.code not in {404, 408, 429} and not 500 <= error.code <= 599:
                raise Error('Release download refused. No installer was executed.') from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        print('Release source unavailable; trying the official fallback.', flush=True)
    raise Error('Both signed release sources are unavailable. Run resume after checking connectivity.')


def verify(envelope, directory):
    if not isinstance(envelope, dict) or set(envelope) != {'signed', 'signature'}:
        raise Error('Invalid signed release manifest.')
    payload = envelope['signed']
    try:
        signature = base64.b64decode(envelope['signature'], validate=True)
        if not 32 <= len(signature) <= 512:
            raise ValueError()
        body = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    except (ValueError, TypeError):
        raise Error('Invalid release signature.') from None
    directory = Path(directory)
    write(directory / 'signed.json', body)
    write(directory / 'signature', signature)
    result = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(HERE / 'release-signing.pub'),
                             '-signature', str(directory / 'signature'), str(directory / 'signed.json')],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if result.returncode:
        raise Error('Release signature verification failed. No installer was executed.')
    version = payload.get('version', '')
    info = payload.get('installer', {})
    if (payload.get('format') != 1 or payload.get('channel') != 'stable'
            or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', version)
            or tuple(map(int, version.split('.'))) < (3, 5, 0)
            or info.get('path') != '/merchant/' + version + '/installer.py'
            or type(info.get('size')) is not int or not 1 <= info['size'] <= 1024 * 1024
            or not re.fullmatch('[0-9a-f]{64}', info.get('sha256', ''))):
        raise Error('No compatible signed installer is available.')
    return info


def installer(directory):
    envelope = json.loads(fetch('/channels/stable.json', 65536))
    info = verify(envelope, directory)
    data = fetch(info['path'], info['size'])
    if len(data) != info['size'] or hashlib.sha256(data).hexdigest() != info['sha256']:
        raise Error('Installer checksum verification failed. No installer was executed.')
    path = Path(directory) / 'installer.py'
    write(path, data)
    return path, envelope
