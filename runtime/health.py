import json
import sys
import urllib.request
from common import control

try:
    status = control('ping')
    if not status.get('ready'):
        raise ValueError()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8080/healthz', timeout=3) as response:
        data = json.load(response)
    sys.exit(0 if data.get('status') == 'ok' and data.get('database') == 'ok' else 1)
except Exception:
    sys.exit(1)
